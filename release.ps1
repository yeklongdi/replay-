[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$')]
    [string]$Version,

    [string]$Notes = '常规功能更新与问题修复。',
    [string]$Python = 'python',
    [switch]$SkipTests,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$tag = "v$Version"
$versionFile = Join-Path $PSScriptRoot 'replaystudio\version.py'
$outputDir = Join-Path $PSScriptRoot '发布'
$zipPath = Join-Path $outputDir '回放工坊-Windows-x64.zip'
$checksumPath = Join-Path $outputDir 'SHA256SUMS.txt'

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw '未找到 GitHub CLI (gh)，请先安装并执行 gh auth login。'
}

$existingTag = git tag -l $tag
if ($existingTag) {
    throw "本地已存在标签 $tag，请使用新的版本号。"
}

if ($DryRun) {
    Write-Host "[DryRun] 将更新 $versionFile 为 $Version"
    if (-not $SkipTests) { Write-Host "[DryRun] 将运行 $Python -B -m unittest discover -s tests -v" }
    Write-Host "[DryRun] 将运行 .\build.ps1 -Python `"$Python`" -OutputDirectory `"$outputDir`""
    Write-Host "[DryRun] 将提交版本变更并创建/推送标签 $tag"
    Write-Host "[DryRun] 将调用 gh release create $tag 上传 $zipPath 与 $checksumPath"
    exit 0
}

$content = [IO.File]::ReadAllText($versionFile, [Text.Encoding]::UTF8)
$updated = [regex]::Replace($content, 'APP_VERSION\s*=\s*"[^"]+"', "APP_VERSION = `"$Version`"")
if ($updated -eq $content -and $content -notmatch ('APP_VERSION\s*=\s*"' + [regex]::Escape($Version) + '"')) {
    throw "未能更新 $versionFile 中的 APP_VERSION。"
}
[IO.File]::WriteAllText($versionFile, $updated, [Text.UTF8Encoding]::new($false))

if (-not $SkipTests) {
    $env:PYTHONUTF8 = '1'
    $env:QT_QPA_PLATFORM = 'offscreen'
    & $Python -B -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw '单元测试未通过，已中止发版。'
    }
}

$env:PYTHONDONTWRITEBYTECODE = '1'
& (Join-Path $PSScriptRoot 'build.ps1') -Python $Python -OutputDirectory $outputDir
if ($LASTEXITCODE -ne 0) {
    throw '便携版构建失败，已中止发版。'
}

if (-not (Test-Path -LiteralPath $zipPath -PathType Leaf) -or -not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
    throw '缺少打包产物或 SHA256SUMS.txt。'
}

git add -- replaystudio/version.py
$stagedDiff = git diff --cached --name-only
if ($stagedDiff) {
    git commit -m "发布 $tag"
    if ($LASTEXITCODE -ne 0) { throw 'git commit 失败。' }
}

git tag -a $tag -m "回放工坊 $tag"
if ($LASTEXITCODE -ne 0) { throw '创建标签失败。' }

git push origin HEAD
if ($LASTEXITCODE -ne 0) { throw '推送分支失败。' }
git push origin $tag
if ($LASTEXITCODE -ne 0) { throw '推送标签失败。' }

gh release create $tag $zipPath $checksumPath --title "回放工坊 $tag" --notes $Notes
if ($LASTEXITCODE -ne 0) { throw '创建 GitHub Release 失败。' }

Write-Host "已成功发布回放工坊 $tag 到 GitHub Releases。"
