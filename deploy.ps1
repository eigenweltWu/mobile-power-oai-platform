# deploy.ps1 — 一键构建并部署平台(前端+后端)与手机 App
# 用法：
#   powershell -ExecutionPolicy Bypass -File deploy.ps1                 # 全部：前端+安卓+装手机+重启后端
#   powershell -ExecutionPolicy Bypass -File deploy.ps1 -SkipAndroid    # 只更新平台
#   powershell -ExecutionPolicy Bypass -File deploy.ps1 -SkipFrontend   # 只更新手机 App
#   powershell -ExecutionPolicy Bypass -File deploy.ps1 -NoInstall      # 构建但不装手机
#   powershell -ExecutionPolicy Bypass -File deploy.ps1 -NoBackendRestart

param(
    [switch]$SkipFrontend,   # 跳过前端构建
    [switch]$SkipAndroid,    # 跳过安卓构建
    [switch]$NoInstall,      # 构建但不安装到手机
    [switch]$NoBackendRestart, # 不重启后端
    [string]$Serial = ''     # 指定手机序列号（留空自动检测）
)

$ErrorActionPreference = 'Stop'
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Step([string]$msg) {
    Write-Host ""
    Write-Host "=== $msg ===" -ForegroundColor Cyan
}

# ---- 0. 定位 adb -----------------------------------------------------------
function Find-Adb {
    $cands = @()
    if ($env:ANDROID_HOME) { $cands += Join-Path $env:ANDROID_HOME 'platform-tools\adb.exe' }
    if ($env:ANDROID_SDK_ROOT) { $cands += Join-Path $env:ANDROID_SDK_ROOT 'platform-tools\adb.exe' }
    $cands += "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
    $cands += Join-Path $ROOT '.tools\platform-tools\adb.exe'
    foreach ($c in $cands) { if (Test-Path $c) { return $c } }
    $w = Get-Command adb -ErrorAction SilentlyContinue
    if ($w) { return $w.Source }
    return $null
}

# ---- 1. 定位 gradle（优先 PATH，再 wrapper，再缓存发行版）----------------------
function Find-Gradle {
    $g = Get-Command gradle -ErrorAction SilentlyContinue
    if ($g) { return "gradle" }
    $wrapper = Join-Path $ROOT 'android\gradlew.bat'
    if (Test-Path $wrapper) { return $wrapper }
    $dist = Get-ChildItem "$env:USERPROFILE\.gradle\wrapper\dists" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Get-ChildItem $_.FullName -Directory -ErrorAction SilentlyContinue } |
        ForEach-Object { Get-ChildItem $_.FullName -Directory -ErrorAction SilentlyContinue } |
        Where-Object { Test-Path (Join-Path $_.FullName 'bin\gradle.bat') } |
        Sort-Object Name -Descending | Select-Object -First 1
    if ($dist) { return (Join-Path $dist.FullName 'bin\gradle.bat') }
    return $null
}

$adb = Find-Adb
$gradle = Find-Gradle

Write-Host "adb    : $adb" -ForegroundColor DarkGray
Write-Host "gradle : $gradle" -ForegroundColor DarkGray

# ---- 2. 构建前端 -----------------------------------------------------------
if (-not $SkipFrontend) {
    Write-Step "构建前端 (npm run build)"
    Push-Location (Join-Path $ROOT 'experiment_platform\web')
    try { npm run build; if ($LASTEXITCODE -ne 0) { throw "前端构建失败" } }
    finally { Pop-Location }
}

# ---- 3. 构建安卓 APK ---------------------------------------------------------
if (-not $SkipAndroid) {
    Write-Step "构建 Android APK"
    if (-not $gradle) { throw "未找到 gradle；请安装或用 Android Studio 打开 android/" }
    Push-Location (Join-Path $ROOT 'android')
    try {
        if ($gradle -eq 'gradle') { & gradle assembleDebug --no-daemon }
        else { & $gradle assembleDebug --no-daemon }
        if ($LASTEXITCODE -ne 0) { throw "APK 构建失败" }
    } finally { Pop-Location }
}

$apk = Join-Path $ROOT 'android\app\build\outputs\apk\debug\app-debug.apk'

# ---- 4. 安装到手机 -----------------------------------------------------------
if (-not $NoInstall -and -not $SkipAndroid) {
    if (-not $adb) { Write-Host "未找到 adb，跳过安装" -ForegroundColor Yellow }
    else {
        $serial = $Serial
        if (-not $serial) {
            $devs = & $adb devices | Select-Object -Skip 1 | Where-Object { $_ -match 'device$' } | ForEach-Object { ($_ -split '\s+')[0] }
            if ($devs.Count -eq 1) { $serial = $devs[0] }
            elseif ($devs.Count -gt 1) { $serial = $devs | Select-Object -First 1; Write-Host "多设备，使用 $serial（用 -Serial 指定）" -ForegroundColor Yellow }
            else { Write-Host "未检测到手机，跳过安装" -ForegroundColor Yellow }
        }
        if ($serial) {
            Write-Step "安装 APK 到 $serial"
            & $adb -s $serial install -r $apk
            # 运行时权限
            & $adb -s $serial shell pm grant com.xjtlu.energyagent android.permission.READ_PHONE_STATE 2>$null | Out-Null
            & $adb -s $serial shell pm grant com.xjtlu.energyagent android.permission.WRITE_SECURE_SETTINGS 2>$null | Out-Null
            & $adb -s $serial shell pm grant com.xjtlu.energyagent android.permission.ACCESS_FINE_LOCATION 2>$null | Out-Null
            # 重启 App（入口 TaskListActivity）
            & $adb -s $serial shell am force-stop com.xjtlu.energyagent 2>$null | Out-Null
            Start-Sleep -Seconds 2
            & $adb -s $serial shell am start -n com.xjtlu.energyagent/.TaskListActivity 2>$null | Out-Null
            # USB 通道
            & $adb -s $serial forward --remove-all 2>$null | Out-Null
            & $adb -s $serial forward tcp:8420 tcp:8420 2>$null | Out-Null
            Write-Host "已安装并启动，adb forward tcp:8420 已建立" -ForegroundColor Green
        }
    }
}

# ---- 5. 重启后端 --------------------------------------------------------------
if (-not $NoBackendRestart) {
    Write-Step "重启后端 (端口 8900)"
    $pids = Get-NetTCPConnection -LocalPort 8900 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($p in $pids) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    Start-Process powershell -ArgumentList @(
        '-NoExit', '-Command',
        "cd '$ROOT'; python -m uvicorn experiment_platform.backend.api:app --host 127.0.0.1 --port 8900 --log-level warning"
    )
    Write-Host "后端已在新窗口启动: http://127.0.0.1:8900" -ForegroundColor Green
}

Write-Host ""
Write-Host "部署完成。" -ForegroundColor Green
if (-not $SkipAndroid -and -not $NoInstall) { Write-Host "  - 手机 App 已更新（TaskListActivity）" -ForegroundColor Green }
if (-not $SkipFrontend) { Write-Host "  - 前端已重新构建" -ForegroundColor Green }
if (-not $NoBackendRestart) { Write-Host "  - 后端已重启" -ForegroundColor Green }
