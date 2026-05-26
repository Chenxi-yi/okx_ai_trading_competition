$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Resolve-Path (Join-Path $ScriptDir "..\..")
Set-Location $RootDir

$LogDir = Join-Path $RootDir "engine\logs"
$ControlDir = Join-Path $RootDir "engine\control"
New-Item -ItemType Directory -Force -Path $LogDir, $ControlDir | Out-Null

$Port = if ($env:OKX_TRADING_SYSTEM_PORT) { $env:OKX_TRADING_SYSTEM_PORT } else { "8788" }
$Url = "http://127.0.0.1:$Port/"

if ($env:OKX_TRADING_SYSTEM_PYTHON) {
    $PythonBin = $env:OKX_TRADING_SYSTEM_PYTHON
} else {
    $PythonBin = (Get-Command python -ErrorAction Stop).Source
}

$env:OKX_TRADING_SYSTEM_PYTHON = $PythonBin
$env:Path = "$env:APPDATA\npm;C:\Program Files\nodejs;$env:Path"

function Stop-PidFile {
    param(
        [string]$Path,
        [string]$Label
    )
    if (-not (Test-Path $Path)) {
        return
    }
    $Text = (Get-Content -Path $Path -ErrorAction SilentlyContinue | Select-Object -First 1)
    $PidValue = 0
    if (-not [int]::TryParse([string]$Text, [ref]$PidValue)) {
        return
    }
    $Proc = Get-Process -Id $PidValue -ErrorAction SilentlyContinue
    if ($Proc) {
        Write-Host "Stopping old $Label process pid=$PidValue"
        Stop-Process -Id $PidValue -Force
        Start-Sleep -Seconds 1
    }
}

function Start-HiddenPython {
    param(
        [string[]]$ArgList,
        [string]$Name,
        [string]$PidPath
    )
    $Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $OutLog = Join-Path $LogDir "$Name`_$Stamp.out.log"
    $ErrLog = Join-Path $LogDir "$Name`_$Stamp.err.log"
    $Proc = Start-Process `
        -FilePath $PythonBin `
        -ArgumentList $ArgList `
        -WorkingDirectory $RootDir `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog `
        -WindowStyle Hidden `
        -PassThru
    Set-Content -Path $PidPath -Value $Proc.Id
    [pscustomobject]@{
        pid = $Proc.Id
        out_log = $OutLog
        err_log = $ErrLog
    }
}

Stop-PidFile -Path (Join-Path $ControlDir "launcher.pid") -Label "launcher"
Stop-PidFile -Path (Join-Path $ControlDir "data_refresh.pid") -Label "data refresh"
Stop-PidFile -Path (Join-Path $ControlDir "system_watchdog.pid") -Label "system watchdog"

Write-Host "Starting OKX trading launcher on $Url"
$Launcher = Start-HiddenPython `
    -Name "launcher_windows" `
    -PidPath (Join-Path $ControlDir "launcher.pid") `
    -ArgList @("launcher\launcher_server.py", "--port", $Port)

Write-Host "Starting unified data refresh scheduler."
$Refresh = Start-HiddenPython `
    -Name "data_refresh_windows" `
    -PidPath (Join-Path $ControlDir "data_refresh.pid") `
    -ArgList @(
        "engine\data\refresh_scheduler.py",
        "--interval-sec", "900",
        "--max-symbols", "150",
        "--extra-symbols", "XAU/USDT",
        "--timeframes", "5m,15m,1h,4h,1d",
        "--lookback-days", "3",
        "--sleep-sec", "0.2",
        "--derivatives-max-symbols", "150",
        "--derivatives-run-id", "c_auto_live_derivatives_5m",
        "--derivatives-kinds", "funding,open_interest,long_short",
        "--derivatives-timeframe", "5m",
        "--derivatives-lookback-days", "3"
    )

Write-Host "Starting system watchdog."
$Watchdog = Start-HiddenPython `
    -Name "system_watchdog_windows" `
    -PidPath (Join-Path $ControlDir "system_watchdog.pid") `
    -ArgList @(
        "scripts\run_system_watchdog.py",
        "--loop",
        "--interval-sec", "60",
        "--max-runner-rss-mb", "1400",
        "--max-service-rss-mb", "900"
    )

Start-Sleep -Seconds 2
Start-Process $Url

Write-Host ""
Write-Host "OKX Trading System started."
Write-Host "Frontend: $Url"
Write-Host "Python: $PythonBin"
Write-Host "Launcher pid: $($Launcher.pid)"
Write-Host "Launcher logs: $($Launcher.out_log) / $($Launcher.err_log)"
Write-Host "Data refresh pid: $($Refresh.pid)"
Write-Host "Data refresh logs: $($Refresh.out_log) / $($Refresh.err_log)"
Write-Host "Watchdog pid: $($Watchdog.pid)"
Write-Host "Watchdog logs: $($Watchdog.out_log) / $($Watchdog.err_log)"
