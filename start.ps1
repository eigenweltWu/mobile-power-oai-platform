$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$AdbPath = Join-Path $ProjectRoot ".tools\platform-tools\adb.exe"

if (-not (Test-Path -LiteralPath $AdbPath)) {
    $ToolsDirectory = Join-Path $ProjectRoot ".tools"
    $ArchivePath = Join-Path $ToolsDirectory "platform-tools-windows.zip"
    New-Item -ItemType Directory -Force -Path $ToolsDirectory | Out-Null
    Write-Host "Downloading Android Platform Tools from Google..."
    Invoke-WebRequest -Uri "https://dl.google.com/android/repository/platform-tools-latest-windows.zip" -OutFile $ArchivePath
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $ToolsDirectory -Force
    Remove-Item -LiteralPath $ArchivePath
}

$PythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCommand) {
    $PythonCommand = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $PythonCommand) {
    throw "Python 3 was not found. Install Python 3.10 or newer."
}

Set-Location $ProjectRoot
$PythonExe = $PythonCommand.Source
& $PythonExe (Join-Path $ProjectRoot "monitor.py") --adb $AdbPath --open
