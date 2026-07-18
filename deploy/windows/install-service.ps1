<#
.SYNOPSIS
    Installs Correlate AI as a Windows Service using NSSM, running under waitress.
    This is the Windows equivalent of a supervisord program block - it gives the
    app auto-start-on-boot and auto-restart-on-crash, which "python manage.py
    runserver" or a bare "waitress-serve" console window does not.

.DESCRIPTION
    Run this ONCE per environment, as Administrator, after:
      1. The app is deployed to its target folder (this script assumes it's being
         run from <app-root>\deploy\windows\).
      2. The venv exists and `pip install -r requirements.txt` has been run.
      3. .env is configured (DJANGO_DEBUG=False, DJANGO_SECRET_KEY set, etc. -
         see .env.example).
      4. `manage.py migrate` and `manage.py collectstatic` have been run.
      5. NSSM (https://nssm.cc/download) is installed and nssm.exe is on PATH,
         or NSSM_EXE below points at it directly.

.NOTES
    NSSM is a third-party tool, not bundled with Windows - download the release
    zip from https://nssm.cc/download and put nssm.exe somewhere on PATH (or
    point NSSM_EXE at its full path) before running this script.
#>

param(
    [string]$ServiceName = "CorrelateAI",
    [string]$NssmExe = "nssm.exe",
    [int]$Port = 8000,
    [int]$Threads = 4
)

$ErrorActionPreference = "Stop"

# Resolve paths relative to this script's location (deploy\windows\), not the
# caller's current directory, so this works regardless of where it's invoked from.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$PythonExe = Join-Path $AppRoot "venv\Scripts\python.exe"
$ServeScript = Join-Path $ScriptDir "serve.py"
$LogDir = Join-Path $AppRoot "logs"

if (-not (Test-Path $PythonExe)) {
    throw "venv not found at $PythonExe - create it and run 'pip install -r requirements.txt' first."
}
if (-not (Test-Path (Join-Path $AppRoot ".env"))) {
    throw ".env not found at $AppRoot\.env - copy .env.example to .env and configure it first (see .env.example)."
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Fail fast with a clear message if nssm isn't actually reachable, rather than a
# cryptic error partway through service registration.
$nssmCmd = Get-Command $NssmExe -ErrorAction SilentlyContinue
if (-not $nssmCmd) {
    throw "'$NssmExe' was not found on PATH. Download NSSM from https://nssm.cc/download, " +
          "extract nssm.exe (win64 build) somewhere on PATH, then re-run this script."
}

Write-Host "Installing '$ServiceName' as a Windows Service (waitress on 127.0.0.1:$Port)..." -ForegroundColor Cyan

# Remove a prior install of the same name so re-running this script is safe
# (idempotent), matching how the rest of this app's deploy tooling behaves.
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Service '$ServiceName' already exists - stopping and removing it first." -ForegroundColor Yellow
    & $NssmExe stop $ServiceName confirm | Out-Null
    & $NssmExe remove $ServiceName confirm | Out-Null
}

& $NssmExe install $ServiceName $PythonExe $ServeScript
& $NssmExe set $ServiceName AppDirectory $AppRoot
& $NssmExe set $ServiceName DisplayName "Correlate AI (waitress)"
& $NssmExe set $ServiceName Description "Correlate AI ITSM ticket-clustering app, served by waitress. Reverse-proxied by IIS/ARR - do not expose this port directly."
& $NssmExe set $ServiceName AppEnvironmentExtra "WAITRESS_HOST=127.0.0.1" "WAITRESS_PORT=$Port" "WAITRESS_THREADS=$Threads"

# Auto-restart on crash, but don't hot-loop-restart if it's crashing immediately
# on every launch - NSSM's default throttle (1500ms) already covers that; this
# just makes the restart behavior explicit rather than relying on defaults.
& $NssmExe set $ServiceName AppExit Default Restart
& $NssmExe set $ServiceName AppRestartDelay 3000
& $NssmExe set $ServiceName Start SERVICE_AUTO_START

# Redirect stdout/stderr to rotating-by-restart log files under logs\ - Django's
# own app-level logging already writes to logs\correlate.log (see settings.py);
# these capture waitress/service-level output (startup, crashes) separately.
& $NssmExe set $ServiceName AppStdout (Join-Path $LogDir "service-stdout.log")
& $NssmExe set $ServiceName AppStderr (Join-Path $LogDir "service-stderr.log")
& $NssmExe set $ServiceName AppRotateFiles 1
& $NssmExe set $ServiceName AppRotateOnline 1
& $NssmExe set $ServiceName AppRotateBytes 5242880

& $NssmExe start $ServiceName

Start-Sleep -Seconds 2
$svc = Get-Service -Name $ServiceName
Write-Host ""
Write-Host "Service '$ServiceName' status: $($svc.Status)" -ForegroundColor $(if ($svc.Status -eq 'Running') { 'Green' } else { 'Red' })
Write-Host "Verify with:  Invoke-WebRequest http://127.0.0.1:$Port/healthz/ -UseBasicParsing"
Write-Host "Logs:         $LogDir"
Write-Host ""
Write-Host "Next: configure IIS + ARR to reverse-proxy to 127.0.0.1:$Port - see web.config and README.md in this folder."
