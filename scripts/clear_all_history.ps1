[CmdletBinding()]
param(
    [string]$PlatformUrl = 'http://127.0.0.1:8900',
    [string]$Serial = '',
    [switch]$Execute,
    [string]$Confirmation = ''
)

$ErrorActionPreference = 'Stop'
$packageName = 'com.xjtlu.energyagent'
$requiredConfirmation = 'DELETE-ALL-HISTORY'
$activeRunStates = @('PREPARING', 'ARMED', 'RUNNING', 'STOPPING')
$baseUrl = $PlatformUrl.TrimEnd('/')

function Invoke-PlatformApi {
    param(
        [Parameter(Mandatory)][ValidateSet('GET', 'DELETE')][string]$Method,
        [Parameter(Mandatory)][string]$Path
    )
    # Large experiments can take minutes to remove from both SQLite and disk.
    $timeoutSec = if ($Method -eq 'DELETE') { 0 } else { 30 }
    Invoke-RestMethod -Uri "$baseUrl$Path" -Method $Method -TimeoutSec $timeoutSec
}

Write-Host 'Checking PC platform history...'
try {
    $platformStatus = Invoke-PlatformApi -Method GET -Path '/api/platform/status'
    $experiments = @(Invoke-PlatformApi -Method GET -Path '/api/experiments' | Write-Output)
} catch {
    throw "Cannot reach the platform at $baseUrl. Start the platform first. $($_.Exception.Message)"
}

$latestState = [string]$platformStatus.experiment.latest_run.state
if ($activeRunStates -contains $latestState) {
    throw "Refusing to clear history while a Run is $latestState. Stop the Run first."
}
$phoneAgentState = [string]$platformStatus.phone.status.state
$phoneMonitoring = [bool]$platformStatus.phone.status.monitoring
if (($phoneAgentState -and $phoneAgentState -ne 'IDLE') -or $phoneMonitoring) {
    throw "Refusing to clear history while the phone Agent is active ($phoneAgentState). Stop monitoring and the Run first."
}

$runCount = 0
foreach ($experiment in $experiments) {
    $runCount += [int]$experiment.run_count
}
Write-Host ("PC: {0} Experiment(s), {1} Run(s)" -f $experiments.Count, $runCount)

$adbCommand = Get-Command adb -ErrorAction SilentlyContinue
$adbPath = if ($adbCommand) { $adbCommand.Source } else { Join-Path $PSScriptRoot '..\.tools\android-sdk\platform-tools\adb.exe' }
if (-not (Test-Path -LiteralPath $adbPath)) { $adbPath = '' }
$phone = $null
if ($adbPath) {
    $deviceRows = @(& $adbPath devices 2>&1 | Select-Object -Skip 1 | Where-Object { [string]$_ -match '\S' })
    $devices = @($deviceRows | ForEach-Object {
        if ([string]$_ -match '^(\S+)\s+(\S+)') {
            [pscustomobject]@{ Serial = $Matches[1]; State = $Matches[2] }
        }
    })
    $readyDevices = @($devices | Where-Object State -eq 'device')

    if ($Serial) {
        $phone = $readyDevices | Where-Object Serial -eq $Serial | Select-Object -First 1
        if (-not $phone) {
            throw "USB device '$Serial' is not connected and authorized."
        }
    } elseif ($readyDevices.Count -gt 1) {
        throw "Multiple authorized USB devices found. Re-run with -Serial <device serial>."
    } elseif ($readyDevices.Count -eq 1) {
        $phone = $readyDevices[0]
    }

    foreach ($device in $devices | Where-Object State -ne 'device') {
        Write-Warning ("USB device {0} is {1}; its history cannot be cleared." -f $device.Serial, $device.State)
    }
}

$phoneHasApp = $false
if ($phone) {
    $packagePath = (& $adbPath -s $phone.Serial shell pm path $packageName 2>&1) -join "`n"
    $phoneHasApp = $LASTEXITCODE -eq 0 -and $packagePath -match '^package:'
    if ($phoneHasApp) {
        Write-Host ("Phone: {0}; app data will be completely reset." -f $phone.Serial)
    } else {
        Write-Host ("Phone: {0}; {1} is not installed, nothing to clear." -f $phone.Serial, $packageName)
    }
} elseif (-not $adbPath) {
    Write-Host 'Phone: adb not found; phone cleanup will be skipped.'
} else {
    Write-Host 'Phone: no authorized USB device; phone cleanup will be skipped.'
}

if (-not $Execute) {
    Write-Host ''
    Write-Host 'PREVIEW ONLY - nothing was deleted.' -ForegroundColor Yellow
    Write-Host "Execute with: .\scripts\clear_all_history.ps1 -Execute -Confirmation $requiredConfirmation"
    exit 0
}

if ($Confirmation -cne $requiredConfirmation) {
    throw "Deletion requires -Confirmation $requiredConfirmation"
}

Write-Warning 'This permanently deletes every PC Experiment/Run/artifact and resets all Energy Agent app data on the connected phone.'

# Clear the phone first so a phone failure cannot leave a clean PC paired with stale phone history.
if ($phone -and $phoneHasApp) {
    $clearResult = (& $adbPath -s $phone.Serial shell pm clear $packageName 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0 -or $clearResult -notmatch 'Success') {
        throw "Phone cleanup failed for $($phone.Serial): $clearResult"
    }
    Write-Host ("Phone {0}: app data cleared." -f $phone.Serial) -ForegroundColor Green
}

foreach ($experiment in $experiments) {
    $experimentId = [string]$experiment.experiment_id
    $escapedId = [Uri]::EscapeDataString($experimentId)
    Write-Host ("PC: deleting Experiment {0}..." -f $experimentId)
    $null = Invoke-PlatformApi -Method DELETE -Path "/api/experiments/$escapedId"
    Write-Host ("PC: deleted Experiment {0}" -f $experimentId)
}

$historyResult = Invoke-PlatformApi -Method DELETE -Path '/api/history'
$remaining = @(Invoke-PlatformApi -Method GET -Path '/api/experiments' | Write-Output)
if ($remaining.Count -ne 0) {
    throw "PC cleanup verification failed: $($remaining.Count) Experiment(s) remain."
}

Write-Host ("PC: history cleared; {0} orphaned file(s) removed." -f ([int]$historyResult.removed_files)) -ForegroundColor Green
Write-Host 'All reachable history has been deleted.' -ForegroundColor Green
