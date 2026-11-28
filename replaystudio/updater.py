from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Callable

from .model import ReplayError
from .version import (
    APP_VERSION, CHECKSUM_ASSET_NAME, GITHUB_REPO, PORTABLE_ASSET_NAME, RELEASES_PAGE_URL,
)

GITHUB_API_BASE = "https://api.github.com"
TRUSTED_HOSTS = frozenset({
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
    "codeload.github.com",
})
EXECUTABLE_NAME = "ReplayStudio.exe"
_VERSION_RE = re.compile(r"^\s*[vV]?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?\s*$")
_BODY_SHA256_RE = re.compile(r"SHA-?256\s*[:：]\s*([0-9a-fA-F]{64})", re.IGNORECASE)
_SHA256_LINE_RE = re.compile(r"^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")


class UpdateCancelled(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    version: str
    tag: str
    title: str
    notes: str
    html_url: str
    asset_name: str
    asset_url: str
    asset_size: int
    sha256: str = ""


@dataclass(frozen=True, slots=True)
class InstallTarget:
    directory: Path | None
    executable: Path | None
    is_frozen: bool
    writable: bool
    reason: str = ""


def parse_version(text: str) -> tuple[tuple[int, ...], bool, str] | None:
    match = _VERSION_RE.match(text or "")
    if not match:
        return None
    numbers = tuple(int(part) for part in match.group(1).split("."))
    prerelease = match.group(2) or ""
    return numbers, bool(prerelease), prerelease


def _padded(numbers: tuple[int, ...], length: int) -> tuple[int, ...]:
    return numbers + (0,) * (length - len(numbers))


def is_newer(remote: str, current: str = APP_VERSION) -> bool:
    parsed_remote = parse_version(remote)
    parsed_current = parse_version(current)
    if parsed_remote is None or parsed_current is None:
        return False
    r_nums, r_pre, r_tag = parsed_remote
    c_nums, c_pre, c_tag = parsed_current
    size = max(len(r_nums), len(c_nums))
    r_pad, c_pad = _padded(r_nums, size), _padded(c_nums, size)
    if r_pad != c_pad:
        return r_pad > c_pad
    if r_pre != c_pre:
        # Final release (is_pre=False) is newer than pre-release of the same core version.
        return not r_pre
    return r_tag > c_tag


def format_size(byte_count: int) -> str:
    if byte_count <= 0:
        return "未知大小"
    mb = byte_count / (1024 * 1024)
    if mb >= 1:
        return f"{mb:.1f} MB"
    return f"{max(1, round(byte_count / 1024))} KB"


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def validate_url(url: str, *, allow_local_http: bool = False) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if allow_local_http and parsed.scheme in ("http", "https") and host in ("127.0.0.1", "localhost"):
        return url
    if parsed.scheme != "https" or host not in TRUSTED_HOSTS:
        raise ReplayError(f"不受信任的更新地址：{url}")
    return url


def _open_url(url: str, *, timeout: float = 15.0, headers: dict[str, str] | None = None,
              allow_local_http: bool = False):
    validate_url(url, allow_local_http=allow_local_http)
    req_headers = {"User-Agent": f"ReplayStudio/{APP_VERSION}"}
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(url, headers=req_headers)
    parsed = urllib.parse.urlparse(url)
    context = _ssl_context() if parsed.scheme == "https" else None
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def _pick_portable_asset(assets: list[dict]) -> dict | None:
    if not assets:
        return None
    for asset in assets:
        if asset.get("name") == PORTABLE_ASSET_NAME:
            return asset
    zips = [asset for asset in assets if str(asset.get("name", "")).lower().endswith(".zip")]
    for asset in zips:
        name = str(asset.get("name", "")).lower()
        if "windows" in name or "win" in name:
            return asset
    return zips[0] if len(zips) == 1 else None


def _parse_checksum_text(text: str, asset_name: str) -> str:
    target = asset_name.strip().lower()
    fallback = ""
    for line in text.splitlines():
        match = _SHA256_LINE_RE.match(line)
        if not match:
            continue
        digest, name = match.group(1).lower(), Path(match.group(2).strip()).name.lower()
        if name == target:
            return digest
        if not fallback:
            fallback = digest
    return fallback if len(text.strip().splitlines()) == 1 else ""


def _resolve_sha256(asset: dict, assets: list[dict], notes: str, *,
                    timeout: float, allow_local_http: bool) -> str:
    digest = str(asset.get("digest") or "").strip()
    if digest.lower().startswith("sha256:"):
        candidate = digest.split(":", 1)[1].strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", candidate):
            return candidate
    checksum_asset = next(
        (item for item in assets if str(item.get("name", "")).lower() == CHECKSUM_ASSET_NAME.lower()),
        None,
    )
    if checksum_asset and checksum_asset.get("browser_download_url"):
        try:
            with _open_url(
                str(checksum_asset["browser_download_url"]),
                timeout=timeout,
                allow_local_http=allow_local_http,
            ) as response:
                text = response.read(65536).decode("utf-8-sig", errors="replace")
            found = _parse_checksum_text(text, str(asset.get("name", "")))
            if found:
                return found
        except Exception:
            pass
    if match := _BODY_SHA256_RE.search(notes or ""):
        return match.group(1).lower()
    return ""


def fetch_latest_release(
    *,
    repo: str = GITHUB_REPO,
    api_base: str = GITHUB_API_BASE,
    timeout: float = 15.0,
    allow_local_http: bool = False,
) -> ReleaseInfo:
    url = f"{api_base.rstrip('/')}/repos/{repo}/releases/latest"
    try:
        with _open_url(
            url,
            timeout=timeout,
            headers={"Accept": "application/vnd.github+json"},
            allow_local_http=allow_local_http,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ReplayError("仓库目前还没有发布正式版本（Release）。") from error
        if error.code == 403:
            raise ReplayError("GitHub 接口请求次数暂时受限，请稍后再试或直接打开发布页。") from error
        raise ReplayError(f"检查更新失败（HTTP {error.code}），请稍后重试或打开发布页。") from error
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise ReplayError("无法连接 GitHub，请检查网络或代理后重试，也可手动打开发布页。") from error

    tag = str(payload.get("tag_name") or "").strip()
    if not tag or parse_version(tag) is None:
        raise ReplayError("无法识别最新版本号，请前往发布页手动查看。")
    version = tag.lstrip("vV")
    html_url = str(payload.get("html_url") or RELEASES_PAGE_URL)
    notes = str(payload.get("body") or "").strip()
    title = str(payload.get("name") or tag).strip()
    assets = payload.get("assets") or []
    asset = _pick_portable_asset(assets)
    asset_name = str(asset.get("name") or "") if asset else ""
    asset_url = str(asset.get("browser_download_url") or "") if asset else ""
    asset_size = int(asset.get("size") or 0) if asset else 0
    if asset_url:
        validate_url(asset_url, allow_local_http=allow_local_http)
    sha256 = _resolve_sha256(
        asset, assets, notes, timeout=timeout, allow_local_http=allow_local_http
    ) if asset else ""
    return ReleaseInfo(
        version=version,
        tag=tag,
        title=title,
        notes=notes,
        html_url=html_url,
        asset_name=asset_name,
        asset_url=asset_url,
        asset_size=asset_size,
        sha256=sha256,
    )


def download_release(
    release: ReleaseInfo,
    staging_dir: Path,
    *,
    cancel: Event | None = None,
    progress: Callable[[int, int], None] | None = None,
    timeout: float = 30.0,
    allow_local_http: bool = False,
) -> Path:
    if not release.asset_url:
        raise ReplayError("该版本未附带 Windows 便携版安装包，请前往发布页查看。")
    staging_dir.mkdir(parents=True, exist_ok=True)
    part_path = staging_dir / "package.zip.part"
    final_path = staging_dir / "package.zip"
    hasher = hashlib.sha256()
    downloaded = 0
    total = release.asset_size

    try:
        with _open_url(release.asset_url, timeout=timeout, allow_local_http=allow_local_http) as response:
            header_len = response.headers.get("Content-Length")
            if header_len and header_len.isdigit():
                total = int(header_len)
            if progress:
                progress(0, total)
            with part_path.open("wb") as output:
                while True:
                    if cancel and cancel.is_set():
                        raise UpdateCancelled()
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    output.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded, total)
    except UpdateCancelled:
        part_path.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        part_path.unlink(missing_ok=True)
        raise ReplayError("下载安装包时网络中断，请重试或前往发布页手动下载。") from error

    actual_sha256 = hasher.hexdigest().lower()
    if release.sha256 and actual_sha256 != release.sha256.lower():
        part_path.unlink(missing_ok=True)
        raise ReplayError("安装包完整性校验失败（SHA-256 不匹配），已取消更新。")
    part_path.replace(final_path)
    return final_path


def stage_update_bundle(zip_path: Path, staging_dir: Path) -> Path:
    extract_root = staging_dir / "extracted"
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as bundle:
            if bundle.testzip() is not None:
                raise ReplayError("下载的安装包已损坏（CRC 校验失败），请重试。")
            names = [name.replace("\\", "/") for name in bundle.namelist()]
            safe_entries: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
            for info, normalized in zip(bundle.infolist(), names):
                if not normalized or normalized.startswith("/") or ":" in normalized:
                    raise ReplayError("安装包包含不安全的路径，已拒绝更新。")
                rel = PurePosixPath(normalized)
                if ".." in rel.parts:
                    raise ReplayError("安装包包含越界路径，已拒绝更新。")
                safe_entries.append((info, rel))

            for info, rel in safe_entries:
                target = extract_root.joinpath(*rel.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(info) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as error:
        raise ReplayError("下载的文件不是有效的 ZIP 安装包，请重试。") from error

    nested = extract_root / "ReplayStudio"
    bundle_root = nested if (nested / EXECUTABLE_NAME).is_file() else extract_root
    if not (bundle_root / EXECUTABLE_NAME).is_file() or not (bundle_root / "_internal").is_dir():
        raise ReplayError("安装包结构不完整（缺少 ReplayStudio.exe 或 _internal），已取消更新。")
    return bundle_root


def detect_install_target(
    *,
    frozen: bool | None = None,
    executable: Path | None = None,
) -> InstallTarget:
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    exe_path = Path(sys.executable).resolve() if executable is None else executable.resolve()
    if not is_frozen:
        return InstallTarget(
            None, None, False, False,
            "当前以源码方式运行，请通过 git pull 更新源码，或打开发布页下载便携版。",
        )
    install_dir = exe_path.parent
    if exe_path.name.lower() != EXECUTABLE_NAME.lower() or not (install_dir / "_internal").is_dir():
        return InstallTarget(
            install_dir, exe_path, True, False,
            "当前运行目录不是标准便携版结构，请前往发布页手动下载替换。",
        )
    probe = install_dir / f".replay-update-probe-{uuid.uuid4().hex[:8]}"
    try:
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
    except OSError:
        return InstallTarget(
            install_dir, exe_path, True, False,
            f"当前程序目录没有写入权限（{install_dir}），请移出受保护目录或前往发布页手动更新。",
        )
    return InstallTarget(install_dir, exe_path, True, True, "")


_UPDATER_SCRIPT = r"""[CmdletBinding()]
param(
    [int]$WaitPid = 0,
    [Parameter(Mandatory = $true)][string]$TargetDir,
    [Parameter(Mandatory = $true)][string]$SourceDir,
    [string]$ExeName = 'ReplayStudio.exe',
    [string]$ReopenFile = '',
    [string]$StagingDir = '',
    [switch]$NoRelaunch
)

$ErrorActionPreference = 'Stop'

function Write-UpdateLog([string]$Message) {
    $logPath = Join-Path ([IO.Path]::GetTempPath()) 'replay-studio-update-error.log'
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    [IO.File]::AppendAllText($logPath, "[$stamp] $Message`r`n", [Text.UTF8Encoding]::new($true))
}

function Move-WithRetry([string]$From, [string]$To) {
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        try {
            Move-Item -LiteralPath $From -Destination $To -Force
            return
        }
        catch {
            if ($attempt -eq 20) { throw }
            Start-Sleep -Milliseconds 300
        }
    }
}

if ($WaitPid -gt 0) {
    $deadline = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue)) {
            break
        }
        Start-Sleep -Milliseconds 250
    }
    Start-Sleep -Milliseconds 600
}

$backupDir = Join-Path $TargetDir ('.update-backup-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
$backedUp = [System.Collections.Generic.List[string]]::new()
$installed = [System.Collections.Generic.List[string]]::new()

try {
    if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
        throw "找不到更新源目录：$SourceDir"
    }
    $items = Get-ChildItem -LiteralPath $SourceDir -Force
    if (-not $items) {
        throw "更新源目录为空：$SourceDir"
    }
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

    foreach ($item in $items) {
        $existing = Join-Path $TargetDir $item.Name
        if (Test-Path -LiteralPath $existing) {
            Move-WithRetry $existing (Join-Path $backupDir $item.Name)
            $backedUp.Add($item.Name)
        }
    }

    foreach ($item in $items) {
        $destination = Join-Path $TargetDir $item.Name
        Copy-Item -LiteralPath $item.FullName -Destination $destination -Recurse -Force
        $installed.Add($item.Name)
    }

    $newExe = Join-Path $TargetDir $ExeName
    if (-not ($installed -contains $ExeName) -or -not (Test-Path -LiteralPath $newExe -PathType Leaf)) {
        throw "更新后缺少主程序：$newExe"
    }

    if (Test-Path -LiteralPath $backupDir) {
        Remove-Item -LiteralPath $backupDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    if (-not $NoRelaunch) {
        if ($ReopenFile -and (Test-Path -LiteralPath $ReopenFile -PathType Leaf)) {
            Start-Process -FilePath $newExe -ArgumentList "`"$ReopenFile`"" -WorkingDirectory $TargetDir
        }
        else {
            Start-Process -FilePath $newExe -WorkingDirectory $TargetDir
        }
    }

    if ($StagingDir -and (Test-Path -LiteralPath $StagingDir)) {
        Remove-Item -LiteralPath $StagingDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    exit 0
}
catch {
    Write-UpdateLog "更新失败，准备回滚：$($_.Exception.Message)"
    foreach ($name in $installed) {
        $partial = Join-Path $TargetDir $name
        if (Test-Path -LiteralPath $partial) {
            Remove-Item -LiteralPath $partial -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    foreach ($name in $backedUp) {
        $saved = Join-Path $backupDir $name
        $origin = Join-Path $TargetDir $name
        if (Test-Path -LiteralPath $saved) {
            Move-Item -LiteralPath $saved -Destination $origin -Force -ErrorAction SilentlyContinue
        }
    }
    if (Test-Path -LiteralPath $backupDir) {
        Remove-Item -LiteralPath $backupDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not $NoRelaunch) {
        $fallbackExe = Join-Path $TargetDir $ExeName
        if (Test-Path -LiteralPath $fallbackExe -PathType Leaf) {
            Start-Process -FilePath $fallbackExe -WorkingDirectory $TargetDir -ErrorAction SilentlyContinue
        }
    }
    exit 1
}
"""


def write_updater_script(staging_dir: Path) -> Path:
    staging_dir.mkdir(parents=True, exist_ok=True)
    script = staging_dir / "apply-update.ps1"
    script.write_text(_UPDATER_SCRIPT, encoding="utf-8-sig")
    return script


def make_staging_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="replay-studio-update-"))


def launch_windows_updater(
    *,
    target_dir: Path,
    source_dir: Path,
    staging_dir: Path,
    wait_pid: int | None = None,
    reopen_file: Path | None = None,
    no_relaunch: bool = False,
) -> subprocess.Popen:
    script = write_updater_script(staging_dir)
    pid = os.getpid() if wait_pid is None else wait_pid
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-File", str(script),
        "-WaitPid", str(pid),
        "-TargetDir", str(target_dir),
        "-SourceDir", str(source_dir),
        "-ExeName", EXECUTABLE_NAME,
        "-StagingDir", str(staging_dir),
    ]
    if reopen_file and reopen_file.is_file():
        command.extend(["-ReopenFile", str(reopen_file)])
    if no_relaunch:
        command.append("-NoRelaunch")
    flags = 0
    if sys.platform == "win32":
        # Do not combine DETACHED_PROCESS with CREATE_NO_WINDOW; Windows PowerShell 5.1 exits silently without a console.
        flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    return subprocess.Popen(
        command,
        close_fds=True,
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
