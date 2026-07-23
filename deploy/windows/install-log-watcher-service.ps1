<#
.SYNOPSIS
    Installs the log-tailing watcher (manage.py tail_log_sources) as its own
    Windows Service via NSSM - separate from the main CorrelateAI service
    install-service.ps1 registers, so restarting/updating one never affects
    the other. Only needed if you have at least one Log Source configured
    with trigger mode "Continuous (tailing)" - on-demand and scheduled
    sources don't need this at all.

.DESCRIPTION
    Same NSSM pattern as install-service.ps1 (auto-start, auto-restart-on-
    crash, log rotation), just pointed at a different entrypoint: instead of
    waitress serving HTTP, this runs an indefinite loop (tail_log_sources)
    that watches every active continuous Log Source and scans new bytes as
    they're appended. Run this ONCE per environment, as Administrator, after
    the same prerequisites as install-service.ps1 (venv, .env, migrate,
    collectstatic) - and after at least one Log Source has trigger_mode set
    to "continuous" in the app, otherwise this service will just poll and
    find nothing to do.

.NOTES
    NSSM is a third-party tool - see install-service.ps1's notes for where to
    get it. This script assumes it's already on PATH from setting up the main
    service.
#>

param(
    [string]$ServiceName = "CorrelateAI-LogWatcher",
    [string]$NssmExe = "nssm.exe",
    [int]$PollSeconds = 5
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$PythonExe = Join-Path $AppRoot "venv\Scripts\python.exe"
$LogDir = Join-Path $AppRoot "logs"

if (-not (Test-Path $PythonExe)) {
    throw "venv not found at $PythonExe - create it and run 'pip install -r requirements.txt' first."
}
if (-not (Test-Path (Join-Path $AppRoot ".env"))) {
    throw ".env not found at $AppRoot\.env - copy .env.example to .env and configure it first (see .env.example)."
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$nssmCmd = Get-Command $NssmExe -ErrorAction SilentlyContinue
if (-not $nssmCmd) {
    throw "'$NssmExe' was not found on PATH. Download NSSM from https://nssm.cc/download, " +
          "extract nssm.exe (win64 build) somewhere on PATH, then re-run this script."
}

Write-Host "Installing '$ServiceName' as a Windows Service (polling every ${PollSeconds}s)..." -ForegroundColor Cyan

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Service '$ServiceName' already exists - stopping and removing it first." -ForegroundColor Yellow
    & $NssmExe stop $ServiceName confirm | Out-Null
    & $NssmExe remove $ServiceName confirm | Out-Null
}

& $NssmExe install $ServiceName $PythonExe "manage.py tail_log_sources --poll-seconds=$PollSeconds"
& $NssmExe set $ServiceName AppDirectory $AppRoot
& $NssmExe set $ServiceName DisplayName "Correlate AI (log watcher)"
& $NssmExe set $ServiceName Description "Watches active continuous Log Sources and scans new bytes as they're appended. Separate process from the main web service."

& $NssmExe set $ServiceName AppExit Default Restart
& $NssmExe set $ServiceName AppRestartDelay 3000
& $NssmExe set $ServiceName Start SERVICE_AUTO_START

& $NssmExe set $ServiceName AppStdout (Join-Path $LogDir "logwatcher-stdout.log")
& $NssmExe set $ServiceName AppStderr (Join-Path $LogDir "logwatcher-stderr.log")
& $NssmExe set $ServiceName AppRotateFiles 1
& $NssmExe set $ServiceName AppRotateOnline 1
& $NssmExe set $ServiceName AppRotateBytes 5242880

& $NssmExe start $ServiceName

Start-Sleep -Seconds 2
$svc = Get-Service -Name $ServiceName
Write-Host ""
Write-Host "Service '$ServiceName' status: $($svc.Status)" -ForegroundColor $(if ($svc.Status -eq 'Running') { 'Green' } else { 'Red' })
Write-Host "Verify with:  Get-Content (Join-Path '$LogDir' 'logwatcher-stdout.log') -Tail 20"
Write-Host "              (or check the Scan Jobs page in the app - triggered_by=Continuous)"
Write-Host "Logs:         $LogDir"
