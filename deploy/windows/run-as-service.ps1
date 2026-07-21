<#
.SYNOPSIS
    Simplest path to "Correlate AI runs as a Windows Service, reachable at
    http://hostname:port/" - no IIS, no ARR, no TLS. For that, see deploy-all.ps1
    instead.

.DESCRIPTION
    Sets up the venv/dependencies/.env/migrate/collectstatic (skipped if already
    done), fetches NSSM if it isn't already present, then registers the app as a
    Windows Service via install-service.ps1 bound to 0.0.0.0 instead of that
    script's default 127.0.0.1-only - so it's reachable from other machines on
    the network at http://<Hostname>:<Port>/, not just from this box.

    What you get: auto-start on boot, auto-restart on crash (NSSM), served by
    waitress directly on the port you choose. What you don't get, on purpose,
    because this is the "simple" path: TLS/HTTPS, a reverse proxy, or hiding the
    port number behind a plain hostname with no ":port" suffix - all of that
    needs IIS+ARR+a certificate, which is what deploy-all.ps1 sets up instead.

.PARAMETER Hostname
    What you'll type in a browser: http://<Hostname>:<Port>/. Defaults to this
    machine's own computer name. Added to DJANGO_ALLOWED_HOSTS along with
    localhost/127.0.0.1 - Django rejects any request whose Host header isn't
    allow-listed, so this has to match what people actually browse to.

.PARAMETER Port
    Defaults to 8000. Ports below 1024 (e.g. 80) work fine too - a Windows
    Service running as LocalSystem can bind privileged ports directly, unlike a
    plain user process, so you don't need administrator tricks to use port 80
    here; you still need to run this script elevated to register the service
    itself.

.EXAMPLE
    .\run-as-service.ps1
    # -> http://<this-machine's-hostname>:8000/

.EXAMPLE
    .\run-as-service.ps1 -Hostname reports -Port 80
    # -> http://reports/  (no port suffix)

.NOTES
    Run as Administrator (registering a Windows Service requires it). Safe to
    re-run - re-registers the service and never overwrites an existing .env.
#>

[CmdletBinding()]
param(
    [string]$Hostname = $env:COMPUTERNAME,

    [int]$Port = 8000,

    [string]$ServiceName = "CorrelateAI",

    [string]$NssmZipUrl = "https://nssm.cc/release/nssm-2.24.zip"
)

$ErrorActionPreference = "Stop"

function Write-Step ($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok   ($msg) { Write-Host "  OK: $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  WARN: $msg" -ForegroundColor Yellow }

# --- 0. Preconditions --------------------------------------------------------

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script as Administrator - registering a Windows Service requires it."
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppRoot   = Resolve-Path (Join-Path $ScriptDir "..\..")
if (-not (Test-Path (Join-Path $AppRoot "manage.py"))) {
    throw "manage.py not found under '$AppRoot' - run this script from <app-root>\deploy\windows\."
}

$VenvPython = Join-Path $AppRoot "venv\Scripts\python.exe"
$EnvFile    = Join-Path $AppRoot ".env"
$ToolsDir   = "C:\tools"
$NssmExe    = Join-Path $ToolsDir "nssm-2.24\win64\nssm.exe"

Write-Step "Checking Python version (requirements.txt is pinned to 3.12.x)"
if (-not (Test-Path $VenvPython)) {
    $sysPy = Get-Command python -ErrorAction SilentlyContinue
    if (-not $sysPy) { throw "No 'python' found on PATH. Install Python 3.12 first (pyenv or python.org) before re-running this script." }
    $verOut = (& python --version) 2>&1
    if ($verOut -notmatch "3\.12\.") {
        throw "System Python is '$verOut' - this app needs 3.12.x. Install 3.12 and make sure it's what 'python' resolves to on PATH."
    }
    Write-Ok "System Python is $verOut"
} else {
    Write-Ok "venv already exists"
}

# --- 1. App: venv, deps, .env, migrate, collectstatic -------------------------

Write-Step "Python venv + dependencies"
if (-not (Test-Path $VenvPython)) {
    & python -m venv (Join-Path $AppRoot "venv")
}
& $VenvPython -m pip install --no-cache-dir -r (Join-Path $AppRoot "requirements.txt")
Write-Ok "Dependencies installed"

Write-Step "Configuring .env"
if (Test-Path $EnvFile) {
    Write-Warn2 ".env already exists - leaving it untouched. Verify DJANGO_ALLOWED_HOSTS includes '$Hostname' yourself."
} else {
    Copy-Item (Join-Path $AppRoot ".env.example") $EnvFile
    $secretKey = & $VenvPython -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
    # No reverse proxy in front here, so no TLS/CSRF-origin complications - plain
    # HTTP end to end, DJANGO_BEHIND_TLS stays False (its default).
    (Get-Content $EnvFile) | ForEach-Object {
        $_ -replace '^DJANGO_SECRET_KEY=.*', "DJANGO_SECRET_KEY=$secretKey" `
           -replace '^DJANGO_DEBUG=.*', "DJANGO_DEBUG=False" `
           -replace '^DJANGO_ALLOWED_HOSTS=.*', "DJANGO_ALLOWED_HOSTS=$Hostname,localhost,127.0.0.1" `
           -replace '^DJANGO_BEHIND_TLS=.*', "DJANGO_BEHIND_TLS=False"
    } | Set-Content $EnvFile
    Write-Ok ".env created and configured for '$Hostname'"
}

Write-Step "migrate + collectstatic"
& $VenvPython (Join-Path $AppRoot "manage.py") migrate --noinput
& $VenvPython (Join-Path $AppRoot "manage.py") collectstatic --noinput
Write-Ok "Database migrated, static files collected"

# --- 2. NSSM -------------------------------------------------------------------

Write-Step "Getting NSSM"
if (-not (Test-Path $NssmExe)) {
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    $zipPath = Join-Path $env:TEMP "nssm.zip"
    Invoke-WebRequest -Uri $NssmZipUrl -OutFile $zipPath -UseBasicParsing
    Expand-Archive -Path $zipPath -DestinationPath $ToolsDir -Force
    if (-not (Test-Path $NssmExe)) { throw "NSSM extracted but '$NssmExe' wasn't found - check $ToolsDir for the actual extracted path." }
}
Write-Ok "NSSM at $NssmExe"

# --- 3. Register the service, bound to every interface (not just loopback) ----

Write-Step "Registering Windows Service on 0.0.0.0:$Port"
& (Join-Path $ScriptDir "install-service.ps1") -ServiceName $ServiceName -NssmExe $NssmExe -Port $Port -BindHost "0.0.0.0"
Start-Sleep -Seconds 2

# --- 4. Validate - don't just assume it worked ---------------------------------

Write-Step "Validating"
$results = @()

try {
    $r = Invoke-WebRequest "http://127.0.0.1:$Port/healthz/" -UseBasicParsing -TimeoutSec 10
    $results += [PSCustomObject]@{ Check = "service, via loopback"; Status = $r.StatusCode; Pass = ($r.StatusCode -eq 200) }
} catch {
    $results += [PSCustomObject]@{ Check = "service, via loopback"; Status = "ERROR: $($_.Exception.Message)"; Pass = $false }
}

try {
    $r = Invoke-WebRequest "http://${Hostname}:${Port}/healthz/" -UseBasicParsing -TimeoutSec 10
    $results += [PSCustomObject]@{ Check = "service, via hostname (what a browser would use)"; Status = $r.StatusCode; Pass = ($r.StatusCode -eq 200) }
} catch {
    $results += [PSCustomObject]@{ Check = "service, via hostname (what a browser would use)"; Status = "ERROR: $($_.Exception.Message)"; Pass = $false }
}

$results | Format-Table -AutoSize

if ($results | Where-Object { -not $_.Pass }) {
    Write-Warn2 "One or more checks failed. If the hostname check failed but loopback passed, '$Hostname' likely isn't resolving to this machine from wherever you're checking from - try the machine's IP address instead, or fix DNS. Also check logs\service-stderr.log and logs\correlate.log under $AppRoot."
} else {
    Write-Host "`nAll checks passed - http://${Hostname}:${Port}/ is live." -ForegroundColor Green
    Write-Host "(Plain HTTP, no TLS - that's expected for this simple path. See deploy-all.ps1 if you need HTTPS.)" -ForegroundColor DarkGray
}
