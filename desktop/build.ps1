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

$name = 'SpikeMark-win-x64-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
$stage = Join-Path $build $name
$server = Join-Path $stage 'server'
$python = Join-Path $server 'python'
$site = Join-Path $python 'Lib\site-packages'
$project = Join-Path $PSScriptRoot 'SpikeMark.Desktop.csproj'
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

$files = 'editor.py', 'editor.html', 'editor3d.js', 'scene.py', 'scene.json', 'spikemark.py', 'mcp_server.py', 'requirements.txt', 'LICENSE', 'vendor'
foreach ($file in $files) {
    Copy-Item -LiteralPath (Join-Path $root $file) -Destination $server -Recurse
}
Invoke-Checked (Join-Path $python 'python.exe') @('-B', '-c', 'import editor, mcp_server, numpy, PIL; print(numpy.__version__, PIL.__version__)')

$release = Join-Path $artifacts $name
Move-Item -LiteralPath $stage -Destination $release
if ($Zip) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory($release, "$release.zip", [IO.Compression.CompressionLevel]::Optimal, $true)
    Write-Output "$release.zip"
}
Write-Output (Join-Path $release 'SpikeMark.exe')
