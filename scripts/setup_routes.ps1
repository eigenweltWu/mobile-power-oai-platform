# setup_routes.ps1
# 用户直接输入 OAI/USRP 主机 IP -> 清理旧路由并添加 UE 网段路由。
# UE 网段（10.0.0.0/24, 10.0.1.0/24, 10.0.9.0/24）固定不变；下一跳是用户输入的 OAI/USRP 主机 IP。
# 用法：以管理员身份运行：powershell -ExecutionPolicy Bypass -File setup_routes.ps1

$ErrorActionPreference = 'SilentlyContinue'
$UE_NETS = @('10.0.0.0', '10.0.1.0', '10.0.9.0')

# ---- 0. 管理员检查 ---------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host '[!] 添加/删除路由需要管理员权限，请以管理员身份重新运行。' -ForegroundColor Red
    exit 1
}

# ---- 1. 清理旧路由 ---------------------------------------------------------
function Clear-UeRoutes {
    Write-Host '== 清理旧的 UE 网段路由 ==' -ForegroundColor Yellow
    foreach ($net in $UE_NETS) {
        cmd /c "route delete $net" 2>$null | Out-Null
        cmd /c "route delete $net mask 255.255.255.0" 2>$null | Out-Null
    }
    foreach ($net in $UE_NETS) {
        Get-NetRoute -DestinationPrefix "$net/24" -ErrorAction SilentlyContinue |
            Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    }
    Write-Host '  完成' -ForegroundColor Green
}

# ---- 2. 探测本机网段（仅作提示）-----------------------------------------------
function Get-CurrentSubnet {
    $cfg = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address }
    if (-not $cfg) { return $null }
    $ip = $cfg.IPv4Address.IPAddress | Select-Object -First 1
    if ($ip -notmatch '^(\d+\.\d+\.\d+)\.\d+$') { return $null }
    return $Matches[1]
}

# ---- 3. 主流程 ---------------------------------------------------------------
$subnet = Get-CurrentSubnet
$hint = if ($subnet) { "$subnet.x" } else { "例如 192.168.31.119" }
Write-Host "当前网段提示：$hint" -ForegroundColor DarkGray
$target = Read-Host '输入 OAI/USRP 主机 IP'

if (-not $target -or $target -notmatch '^\d+\.\d+\.\d+\.\d+$') {
    Write-Host 'IP 格式无效，已取消。' -ForegroundColor Yellow
    exit 0
}

Clear-UeRoutes
Write-Host "== 添加 UE 网段路由，下一跳 $target ==" -ForegroundColor Cyan
foreach ($net in $UE_NETS) {
    cmd /c "route -p add $net mask 255.255.255.0 $target" | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "  + $net/24 -> $target" -ForegroundColor Green }
    else { Write-Host "  x $net/24 添加失败" -ForegroundColor Red }
}

Write-Host ''
Write-Host '== 当前 UE 网段路由 ==' -ForegroundColor Cyan
route print -4 | Select-String '10\.0\.[019]\.'
Write-Host ''
Write-Host '完成。可用 `ping 10.0.1.24`（或手机实际 PDU IP）验证 5G 链路。' -ForegroundColor Green
