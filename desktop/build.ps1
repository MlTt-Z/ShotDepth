#requires -Version 5.1
[CmdletBinding()]
param(
    [ValidatePattern('^3\.13\.\d+$')][string]$PythonVersion = '3.13.12',
    [string]$Dotnet = '',
    [string]$BuildPython = '',
    [switch]$Zip
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$root = Split-Path -Parent $PSScriptRoot
$build = Join-Path $PSScriptRoot '.build'
$artifacts = Join-Path $PSScriptRoot 'artifacts'
New-Item -ItemType Directory -Force $build, $artifacts | Out-Null

if (-not $Dotnet) {
    $localDotnet = Join-Path $build 'dotnet\dotnet.exe'
    if (Test-Path -LiteralPath $localDotnet) { $Dotnet = $localDotnet } else { $Dotnet = 'dotnet.exe' }
}
if (-not $BuildPython) {
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        $BuildPython = 'py.exe'
        $pyPrefix = @('-3')
    } else {
        $BuildPython = 'python.exe'
        $pyPrefix = @()
    }
} else { $pyPrefix = @() }

function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable exited with code $LASTEXITCODE." }
}

$name = 'ShotDepth-win-x64-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
$stage = Join-Path $build $name
$server = Join-Path $stage 'server'
$python = Join-Path $server 'python'
$site = Join-Path $python 'Lib\site-packages'
$project = Join-Path $PSScriptRoot 'ShotDepth.Desktop.csproj'
Invoke-Checked $Dotnet @('publish', $project, '-c', 'Release', '-r', 'win-x64', '--self-contained', 'true', '-o', $stage)
New-Item -ItemType Directory -Force $server | Out-Null

$archiveName = "python-$PythonVersion-embed-amd64.zip"
$archive = Join-Path $build $archiveName
if (-not (Test-Path -LiteralPath $archive)) {
    $partial = "$archive.partial"
    Invoke-WebRequest -UseBasicParsing "https://www.python.org/ftp/python/$PythonVersion/$archiveName" -OutFile $partial
    Move-Item -LiteralPath $partial -Destination $archive -Force
}
Expand-Archive -LiteralPath $archive -DestinationPath $python
# _pth keeps the runtime isolated. Enable site to process pywin32's bundled .pth for MCP.
Set-Content -LiteralPath (Join-Path $python 'python313._pth') -Encoding ASCII -Value @('python313.zip', '.', '..', 'Lib\site-packages', 'import site')

# Use build-machine pip only to install wheels for the bundled runtime.
Invoke-Checked $BuildPython ($pyPrefix + @('-m', 'pip', '--disable-pip-version-check', 'install', '--no-compile',
    '--only-binary=:all:', '--platform', 'win_amd64', '--implementation', 'cp', '--python-version', '3.13',
    '--abi', 'cp313', '--target', $site, '-r', (Join-Path $root 'requirements.txt')))

$files = 'editor.py', 'editor.html', 'editor3d.js', 'scene.py', 'scene.json', 'shotdepth.py', 'depth_export.py', 'mcp_server.py', 'requirements.txt', 'LICENSE', 'vendor'
foreach ($file in $files) {
    Copy-Item -LiteralPath (Join-Path $root $file) -Destination $server -Recurse
}
foreach ($file in 'README.md', 'AI_INTEGRATION.md') {
    Copy-Item -LiteralPath (Join-Path $root $file) -Destination $stage
}

# Use the packaged runtime and decode both image formats before publishing the stage.
$smoke = @'
from io import BytesIO
from pathlib import Path

import editor, mcp_server, numpy, PIL
from depth_export import export_depth
from PIL import Image, ImageSequence
from scene import load_scene

scene = load_scene(Path(editor.__file__).with_name('scene.json'))
scene['resolution'] = [160, 96]
png, mime = export_depth(scene, 'vace-png')
with Image.open(BytesIO(png)) as image:
    assert (mime, image.format, image.size) == ('image/png', 'PNG', (160, 96))
    assert image.getextrema()[0][0] < image.getextrema()[0][1], 'PNG has no depth contrast'
    pixels = image.tobytes()
webp, mime = export_depth(scene, 'fun-webp', frames=5)
with Image.open(BytesIO(webp)) as image:
    assert (mime, image.format, image.size, image.n_frames) == ('image/webp', 'WEBP', (160, 96), 5)
    assert all(frame.convert('RGB').tobytes() == pixels for frame in ImageSequence.Iterator(image))
print(f'Embedded Python smoke OK: NumPy {numpy.__version__}, Pillow {PIL.__version__}, PNG and 5-frame WebP')
'@
$smoke | & (Join-Path $python 'python.exe') -B -
if ($LASTEXITCODE -ne 0) { throw "Embedded Python smoke failed (exit code $LASTEXITCODE)." }

$release = Join-Path $artifacts $name
Move-Item -LiteralPath $stage -Destination $release
if ($Zip) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory($release, "$release.zip", [IO.Compression.CompressionLevel]::Optimal, $true)
    Write-Output "$release.zip"
}
Write-Output (Join-Path $release 'ShotDepth.exe')
