[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '发布')
)

$ErrorActionPreference = 'Stop'
$replayTempRoot = (Get-Item -LiteralPath ([IO.Path]::GetTempPath())).FullName.TrimEnd('\')
$replayWork = Join-Path $replayTempRoot ('replay-studio-package-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $replayWork | Out-Null
$replayStagedZip = $null

try {
    $replayEncoder = & $Python -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())'
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $replayEncoder -PathType Leaf)) {
        throw '请先安装 imageio-ffmpeg==0.6.0。'
    }
    $replayEncoderCopy = Join-Path $replayWork 'ffmpeg.exe'
    Copy-Item -LiteralPath $replayEncoder -Destination $replayEncoderCopy
    $replayResources = Join-Path $PSScriptRoot 'resources'
    $replayFont = Join-Path $replayResources 'NotoSansSC.ttf'
    $replayEntry = Join-Path $PSScriptRoot 'app.py'
    $replayDist = Join-Path $replayWork 'dist'
    $replayBuild = Join-Path $replayWork 'build'
    & $Python -m PyInstaller --onedir --windowed --name ReplayStudio --hidden-import edge_tts --collect-data certifi --distpath $replayDist --workpath $replayBuild --specpath $replayWork --add-data ($replayFont + ':resources') --add-binary ($replayEncoderCopy + ':bin') $replayEntry
    if ($LASTEXITCODE -ne 0) {
        throw '便携版打包失败。'
    }

    $replayBundle = Join-Path $replayDist 'ReplayStudio'
    # Qt 使用 Windows 的 ICU 接口，避免打包器从 PATH 收集的其他 ICU 覆盖系统 DLL。
    $replayInternal = Join-Path $replayBundle '_internal'
    foreach ($replayIcuName in 'icu.dll','icuuc.dll','icuin.dll' ) {
        $replayIcu = Join-Path $replayInternal $replayIcuName
        if (Test-Path -LiteralPath $replayIcu -PathType Leaf) {
            Remove-Item -LiteralPath $replayIcu -Force
        }
    }
    Get-ChildItem -LiteralPath $replayInternal -Filter 'icudt*.dll' -File | ForEach-Object {
        Remove-Item -LiteralPath $_.FullName -Force
    }
    $replayLicenses = Join-Path $replayBundle '第三方许可'
    New-Item -ItemType Directory -Path $replayLicenses | Out-Null
    foreach ($replayLicenseName in 'OFL-NotoSansSC.txt','GPL-3.0.txt','LGPL-3.0.txt') {
        Copy-Item -LiteralPath (Join-Path $replayResources $replayLicenseName) -Destination $replayLicenses
    }
    $replayBasePrefix = & $Python -c 'import sys; print(sys.base_prefix)'
    $replayPythonLicense = Join-Path $replayBasePrefix 'LICENSE.txt'
    Copy-Item -LiteralPath $replayPythonLicense -Destination (Join-Path $replayLicenses 'Python-LICENSE.txt')
    $replayComponents = @'
Python
https://www.python.org/downloads/source/

Qt for Python 6.10.2 / Qt 6.10.2
https://doc.qt.io/qtforpython-6/licenses.html
https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.10.2
https://download.qt.io/archive/qt/6.10/6.10.2/single/

FFmpeg 7.1 essentials build by Gyan, supplied by imageio-ffmpeg 0.6.0
https://github.com/imageio/imageio-ffmpeg/tree/v0.6.0
https://github.com/GyanD/codexffmpeg/releases/tag/7.1
https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz
https://code.videolan.org/videolan/x264/

Noto Sans SC
https://github.com/google/fonts/tree/a85815a42757630ce188fdad368c2dfc444d4773/ofl/notosanssc

edge-tts
https://github.com/rany2/edge-tts
'@
    [IO.File]::WriteAllText((Join-Path $replayLicenses '组件来源.txt'), $replayComponents, [Text.UTF8Encoding]::new($true))
    $replayQuickStart = @'
回放工坊 · Windows x64

1. 完整解压到任意文件夹，双击 ReplayStudio.exe。
2. 在左上角「设置」中导入 UTF-8 TXT 或 DOCX 跑团日志、打开工程或切换界面色调。也可直接拖入日志文件。
3. 在「台词」工作台编辑台词、角色、模板与战斗面板。
4. 切换到「剪辑」，导入图片、音乐和音效并拖到对应轨道。点击左侧轨道名可拖动调整顺序或右键改名、复制、删除；点击独立台词块可定位，右侧「书签管理」可跳到书签。
5. 切换到「声音」，为各角色和旁白选择静音、离线 UT 对话音效或免费 AI 人声，也可点「API 接口…」接入本机或云端部署的语音服务。
6. 在「设置」中选择「保存」或「打包工程」，生成可交接的 .replay 文件。
7. 在「设置」中选择「导出 MP4」。需要导出片段时，先用「标记起点」和「标记终点」选好范围。

请把 ReplayStudio.exe 和同目录的 _internal 文件夹放在一起。
'@
    [IO.File]::WriteAllText((Join-Path $replayBundle '使用说明.txt'), $replayQuickStart, [Text.UTF8Encoding]::new($true))

    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    $replayOutput = Join-Path $OutputDirectory '回放工坊-Windows-x64.zip'
    $replayStagedZip = Join-Path $OutputDirectory ('.ReplayStudio-' + [Guid]::NewGuid().ToString('N') + '.zip')
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory($replayBundle, $replayStagedZip, [IO.Compression.CompressionLevel]::Optimal, $true)
    Move-Item -LiteralPath $replayStagedZip -Destination $replayOutput -Force
    Get-Item -LiteralPath $replayOutput | Select-Object FullName, Length
}
finally {
    if ($replayStagedZip -and (Test-Path -LiteralPath $replayStagedZip)) {
        Remove-Item -LiteralPath $replayStagedZip -Force
    }
    $replayResolved = (Resolve-Path -LiteralPath $replayWork).Path
    $replayLeaf = Split-Path -Leaf $replayResolved
    if ((Split-Path -Parent $replayResolved) -eq $replayTempRoot -and
            $replayLeaf -match '^replay-studio-package-[0-9a-f]{32}$') {
        Remove-Item -LiteralPath $replayResolved -Recurse -Force
    }
}
