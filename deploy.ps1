# deploy.ps1 — 一键构建并部署平台(前端+后端)与手机 App
# 用法：
#   powershell -ExecutionPolicy Bypass -File deploy.ps1                 # 全部：前端+安卓+装手机+重启后端
#   powershell -ExecutionPolicy Bypass -File deploy.ps1 -SkipAndroid    # 只更新平台
#   powershell -ExecutionPolicy Bypass -File deploy.ps1 -SkipFrontend   # 只更新手机 App
#   powershell -ExecutionPolicy Bypass -File deploy.ps1 -NoInstall      # 构建但不装手机
#   powershell -ExecutionPolicy Bypass -File deploy.ps1 -NoBackendRestart

param(
    [switch]$SkipFrontend,     # 跳过前端构建
    [switch]$SkipAndroid,      # 跳过安卓构建
    [switch]$NoInstall,        # 构建但不安装到手机
    [switch]$NoBackendRestart, # 不重启后端
    [string]$Serial = ''       # 指定手机序列号（留空自动检测）
)

$ErrorActionPreference = 'Stop'
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Step([string]$msg) {
    Write-Host ""
    Write-Host "=== $msg ===" -ForegroundColor Cyan
}

# ---- 0. 定位 Java ----------------------------------------------------------
# 优先使用现有 JAVA_HOME；否则查找仓库内免安装 JDK（.tools/jdk-*）和常见
# 系统安装位置。这样新机器无需管理员权限也能通过 Gradle wrapper 自举。
function Find-JavaHome {
    $cands = @()
    if ($env:JAVA_HOME) { $cands += $env:JAVA_HOME }
    $cands += Get-ChildItem (Join-Path $ROOT '.tools') -Directory -Filter 'jdk-*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Select-Object -ExpandProperty FullName
    $cands += Get-ChildItem 'C:\Program Files\Microsoft' -Directory -Filter 'jdk-*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Select-Object -ExpandProperty FullName
    $cands += Get-ChildItem 'C:\Program Files\Java' -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Select-Object -ExpandProperty FullName
    foreach ($c in $cands) {
        if ($c -and (Test-Path (Join-Path $c 'bin\java.exe'))) { return $c }
    }
    return $null
}

$javaHome = Find-JavaHome
if ($javaHome) {
    $env:JAVA_HOME = $javaHome
    $env:Path = "$(Join-Path $javaHome 'bin');$env:Path"
}

function Find-AndroidSdk {
    $cands = @()
    if ($env:ANDROID_SDK_ROOT) { $cands += $env:ANDROID_SDK_ROOT }
    if ($env:ANDROID_HOME) { $cands += $env:ANDROID_HOME }
    $cands += Join-Path $ROOT '.tools\android-sdk'
    $cands += "$env:LOCALAPPDATA\Android\Sdk"
    foreach ($c in $cands) {
        if ($c -and (Test-Path (Join-Path $c 'platforms'))) { return $c }
    }
    return $null
}

$androidSdk = Find-AndroidSdk
if ($androidSdk) {
    $env:ANDROID_HOME = $androidSdk
    $env:ANDROID_SDK_ROOT = $androidSdk
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

# ---- 1. 定位 gradle 发行版 lib 目录（用 java -classpath 直接启动主类）--------
# 本机 gradle.bat / gradlew.bat 在 Oracle javapath 下有 "-classpath requires
# class path specification" 问题，故绕过脚本直接用 java 启动 GradleMain。
function Find-GradleLib {
    $dists = Get-ChildItem "$env:USERPROFILE\.gradle\wrapper\dists" -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Get-ChildItem $_.FullName -Directory -ErrorAction SilentlyContinue } |
        ForEach-Object { Get-ChildItem $_.FullName -Directory -ErrorAction SilentlyContinue } |
        Where-Object { Test-Path (Join-Path $_.FullName 'lib\gradle-launcher-*.jar') } |
        Sort-Object Name -Descending
    if ($dists) { return (Join-Path $dists[0].FullName 'lib') }
    return $null
}

function Invoke-Gradle([string[]]$GradleArgs) {
    $lib = Find-GradleLib
    if ($lib) {
        java -classpath "$lib\*" org.gradle.launcher.GradleMain @GradleArgs
        return
    }
    $g = Get-Command gradle -ErrorAction SilentlyContinue
    if ($g) {
        & gradle @GradleArgs
        return
    }
    $wrapper = Join-Path $ROOT 'android\gradlew.bat'
    if (Test-Path $wrapper) {
        & $wrapper @GradleArgs
        return
    }
    throw "未找到 gradle/gradle wrapper；请安装 Gradle 或用 Android Studio 打开 android/"
}

$adb = Find-Adb
$gradleLib = Find-GradleLib

Write-Host "adb       : $adb" -ForegroundColor DarkGray
Write-Host "java home : $javaHome" -ForegroundColor DarkGray
Write-Host "android sdk: $androidSdk" -ForegroundColor DarkGray
Write-Host "gradle lib: $gradleLib" -ForegroundColor DarkGray

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
    Push-Location (Join-Path $ROOT 'android')
    try {
        Invoke-Gradle @('assembleDebug', '--no-daemon')
        if ($LASTEXITCODE -ne 0) { throw "APK 构建失败（exit=$LASTEXITCODE）" }
    } finally { Pop-Location }
}

$apk = Join-Path $ROOT 'android\app\build\outputs\apk\debug\app-debug.apk'

# ---- 4. 安装到手机 -----------------------------------------------------------
if (-not $NoInstall -and -not $SkipAndroid) {
    if (-not $adb) { Write-Host "未找到 adb，跳过安装" -ForegroundColor Yellow }
    else {
        $serial = $Serial
        if (-not $serial) {
            # 强制数组（单设备时避免字符串下标取到首字符）
            $devs = @(& $adb devices | Select-Object -Skip 1 | Where-Object { $_ -match 'device' } | ForEach-Object { ($_ -split '\s+')[0] } | Where-Object { $_ })
            if ($devs.Count -eq 1) { $serial = $devs[0] }
            elseif ($devs.Count -gt 1) { $serial = $devs[0]; Write-Host "多设备，使用 $serial（用 -Serial 指定）" -ForegroundColor Yellow }
            else { Write-Host "未检测到手机，跳过安装" -ForegroundColor Yellow }
        }
        if ($serial) {
            Write-Step "安装 APK 到 $serial"
            & $adb -s $serial install -r $apk
            if ($LASTEXITCODE -ne 0) {
                throw "APK 安装失败（exit=$LASTEXITCODE）；如果是签名不匹配，请先导出手机数据，再手动卸载旧 App"
            }
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
    $logDir = Join-Path $ROOT 'experiment_platform\data\logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $backend = Start-Process -FilePath 'python' -ArgumentList @(
        '-m', 'uvicorn', 'experiment_platform.backend.api:app',
        '--host', '127.0.0.1', '--port', '8900', '--log-level', 'warning'
    ) -WorkingDirectory $ROOT -WindowStyle Hidden -PassThru `
      -RedirectStandardOutput (Join-Path $logDir 'backend.stdout.log') `
      -RedirectStandardError (Join-Path $logDir 'backend.stderr.log')
    $ready = $false
    foreach ($i in 1..30) {
        Start-Sleep -Milliseconds 500
        if (Get-NetTCPConnection -LocalPort 8900 -State Listen -ErrorAction SilentlyContinue) {
            $ready = $true
            break
        }
        if ($backend.HasExited) { break }
    }
    if (-not $ready) { throw "后端未在端口 8900 就绪，请查看 $logDir" }
    Write-Host "后端已在后台启动: http://127.0.0.1:8900 (PID $($backend.Id))" -ForegroundColor Green
}

Write-Host ""
Write-Host "部署完成。" -ForegroundColor Green
if (-not $SkipAndroid -and -not $NoInstall) { Write-Host "  - 手机 App 已更新（TaskListActivity）" -ForegroundColor Green }
if (-not $SkipFrontend) { Write-Host "  - 前端已重新构建" -ForegroundColor Green }
if (-not $NoBackendRestart) { Write-Host "  - 后端已重启" -ForegroundColor Green }
