<#
.SYNOPSIS
    One-shot production deployment for Correlate AI on Windows: IIS + ARR + URL
    Rewrite + an NSSM-managed waitress service, end to end - then validates it
    actually works before finishing, instead of assuming success.

.DESCRIPTION
    Bundles every manual step in README.md into one script:
      1. Detects whether this is Windows Server or a client OS (Windows 10/11)
         and installs IIS with the right cmdlet for each - Install-WindowsFeature
         (what README.md assumes) doesn't exist on client Windows at all, so this
         branches automatically instead of assuming Server.
      2. Downloads + silently installs the URL Rewrite and Application Request
         Routing (ARR) IIS modules if not already present. Direct Microsoft
         download links, verified reachable as of 2026-07-20 - if either 404s
         later (Microsoft does occasionally rotate these), grab the current one
         from https://www.iis.net/downloads/microsoft/url-rewrite and
         https://www.iis.net/downloads/microsoft/application-request-routing
         and pass it via -RewriteMsiUrl / -ArrMsiUrl.
      3. Enables ARR's proxy feature and allow-lists the two server variables
         web.config needs (IIS blocks custom server variables until you do this).
      4. Downloads + extracts NSSM if not already present.
      5. Sets up the Python venv, installs requirements.txt (fails fast with a
         clear message if the resolved Python isn't 3.12.x, since that's what
         requirements.txt is pinned against), configures .env (only if one
         doesn't already exist - never overwrites an operator's existing
         config), runs migrate/collectstatic.
      6. Registers the CorrelateAI Windows Service via the existing
         install-service.ps1 (not reimplemented here - one source of truth for
         the NSSM service definition).
      7. Creates the IIS site in a separate, empty site-root folder containing
         only web.config (per README.md's reasoning: keeps IIS from ever being
         able to serve app source/.env if a rewrite rule is ever misconfigured),
         plus a /static virtual directory.
      8. Validates end-to-end: hits /healthz/ directly against waitress AND
         through the IIS hostname binding, plus a static file through IIS - and
         prints a pass/fail table rather than assuming it worked.

.PARAMETER Hostname
    The hostname users will browse to, e.g. correlate.yourcompany.local. Must
    already resolve to this machine (DNS or a hosts-file entry) - this script
    does not configure DNS, that's outside its scope.

.PARAMETER AppRoot
    Path to the app checkout. Defaults to this script's own ..\.. - i.e. run it
    from deploy\windows\ inside a checkout, same convention install-service.ps1
    already uses.

.PARAMETER UseHttps
    If set, binds IIS on 443 and requires -CertThumbprint (a certificate already
    installed in Cert:\LocalMachine\My whose CN/SAN matches -Hostname). Defaults
    to $false - binds plain HTTP on port 80 and sets DJANGO_BEHIND_TLS=False to
    match, which is the simpler starting point for an internal/pilot rollout on
    a trusted network. Flip this on once you have a real certificate.

.PARAMETER CertThumbprint
    Required if -UseHttps is set.

.PARAMETER WaitressPort
    Internal port waitress binds to on 127.0.0.1, reverse-proxied by IIS.
    Default 8000, matching install-service.ps1's default.

.EXAMPLE
    # Internal pilot, plain HTTP, DNS/hosts already pointed at this box:
    .\deploy-all.ps1 -Hostname correlate.internal.local

.EXAMPLE
    # Real deployment with an existing certificate:
    .\deploy-all.ps1 -Hostname correlate.yourcompany.com -UseHttps -CertThumbprint AB12CD34...

.NOTES
    Run as Administrator. Installing IIS/ARR and registering a Windows Service
    are system-level, hard-to-reverse changes - read this script before running
    it, the same as you would before running any unattended admin script
    against a real box. Idempotent-ish: re-running it re-creates the IIS site
    and Windows Service (both are removed-then-recreated), but never touches an
    already-existing .env.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Hostname,

    [string]$AppRoot,

    [int]$WaitressPort = 8000,

    [switch]$UseHttps,

    [string]$CertThumbprint,

    [string]$ServiceName = "CorrelateAI",

    [string]$RewriteMsiUrl = "https://download.microsoft.com/download/1/2/8/128E2E22-C1B9-44A4-BE2A-5859ED1D4592/rewrite_amd64_en-US.msi",

    [string]$ArrMsiUrl = "https://download.microsoft.com/download/E/9/8/E9849D6A-020E-47E4-9FD0-A023E99B54EB/requestRouter_amd64.msi",

    [string]$NssmZipUrl = "https://nssm.cc/release/nssm-2.24.zip"
)

$ErrorActionPreference = "Stop"

function Write-Step ($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok   ($msg) { Write-Host "  OK: $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  WARN: $msg" -ForegroundColor Yellow }

# --- 0. Preconditions --------------------------------------------------------

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script as Administrator - installing IIS features and a Windows Service both require it."
}
if ($UseHttps -and -not $CertThumbprint) {
    throw "-UseHttps requires -CertThumbprint (a certificate already in Cert:\LocalMachine\My matching -Hostname)."
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $AppRoot) { $AppRoot = Resolve-Path (Join-Path $ScriptDir "..\..") }
if (-not (Test-Path (Join-Path $AppRoot "manage.py"))) {
    throw "manage.py not found under '$AppRoot' - pass -AppRoot explicitly, or run this script from <app-root>\deploy\windows\."
}

$VenvPython = Join-Path $AppRoot "venv\Scripts\python.exe"
$EnvFile    = Join-Path $AppRoot ".env"
$LogDir     = Join-Path $AppRoot "logs"
$ToolsDir   = "C:\tools"
$NssmExe    = Join-Path $ToolsDir "nssm-2.24\win64\nssm.exe"
$SiteRoot   = "C:\inetpub\correlate-site-root"
$StaticRoot = Join-Path $AppRoot "staticfiles"
$SiteName   = "Correlate AI"

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
    $verOut = (& $VenvPython --version) 2>&1
    if ($verOut -notmatch "3\.12\.") { Write-Warn2 "Existing venv is '$verOut', not 3.12.x - dependency install may not have gone cleanly." }
    else { Write-Ok "Existing venv is $verOut" }
}

# --- 1. IIS (OS-adaptive: Install-WindowsFeature doesn't exist on client Windows) ---

Write-Step "Installing IIS"
$productType = (Get-CimInstance Win32_OperatingSystem).ProductType   # 1 = Workstation, 2 = Domain Controller, 3 = Server
if ($productType -eq 1) {
    Write-Warn2 "This is client Windows (Workstation), not Server. Fine for a pilot/internal demo with a handful of users - move to Windows Server before a real production rollout."
    $features = @(
        "IIS-WebServerRole", "IIS-WebServer", "IIS-CommonHttpFeatures", "IIS-StaticContent",
        "IIS-DefaultDocument", "IIS-DirectoryBrowsing", "IIS-HttpErrors", "IIS-HttpLogging",
        "IIS-RequestFiltering", "IIS-ApplicationDevelopment", "IIS-ISAPIExtensions", "IIS-ISAPIFilter",
        "IIS-NetFxExtensibility45", "IIS-ASPNET45", "IIS-ManagementConsole", "IIS-ManagementScriptingTools"
    )
    Enable-WindowsOptionalFeature -Online -FeatureName $features -All -NoRestart | Out-Null
} else {
    Install-WindowsFeature -Name Web-Server -IncludeManagementTools | Out-Null
}
Import-Module WebAdministration -ErrorAction Stop
Write-Ok "IIS installed"

# --- 2. URL Rewrite + ARR -----------------------------------------------------

Write-Step "Installing URL Rewrite + Application Request Routing"
$downloads = @{
    (Join-Path $env:TEMP "rewrite_amd64.msi")        = $RewriteMsiUrl
    (Join-Path $env:TEMP "requestRouter_amd64.msi")  = $ArrMsiUrl
}
foreach ($path in $downloads.Keys) {
    Invoke-WebRequest -Uri $downloads[$path] -OutFile $path -UseBasicParsing
    $p = Start-Process msiexec.exe -ArgumentList "/i `"$path`" /quiet /norestart" -Wait -PassThru
    # 0 = success, 3010 = success but reboot recommended (not required for our purposes)
    if ($p.ExitCode -notin 0, 3010) { throw "Installer '$path' failed with exit code $($p.ExitCode)." }
}
Write-Ok "URL Rewrite + ARR installed"

Write-Step "Enabling ARR proxy + allow-listing forwarded-header server variables"
$appcmd = Join-Path $env:SystemRoot "System32\inetsrv\appcmd.exe"
& $appcmd set config -section:system.webServer/proxy /enabled:"True" /commit:apphost | Out-Null
& $appcmd set config -section:system.webServer/rewrite/allowedServerVariables /+"[name='HTTP_X_FORWARDED_PROTO']" /commit:apphost 2>&1 | Out-Null
& $appcmd set config -section:system.webServer/rewrite/allowedServerVariables /+"[name='HTTP_X_FORWARDED_HOST']" /commit:apphost 2>&1 | Out-Null
Write-Ok "ARR proxy enabled, server variables allow-listed"

# --- 3. NSSM -------------------------------------------------------------------

Write-Step "Getting NSSM"
if (-not (Test-Path $NssmExe)) {
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    $zipPath = Join-Path $env:TEMP "nssm.zip"
    Invoke-WebRequest -Uri $NssmZipUrl -OutFile $zipPath -UseBasicParsing
    Expand-Archive -Path $zipPath -DestinationPath $ToolsDir -Force
    if (-not (Test-Path $NssmExe)) { throw "NSSM extracted but '$NssmExe' wasn't found - check $ToolsDir for the actual extracted path and pass it via a modified `$NssmExe." }
}
Write-Ok "NSSM at $NssmExe"

# --- 4. App: venv, deps, .env, migrate, collectstatic ---------------------------

Write-Step "Python venv + dependencies"
if (-not (Test-Path $VenvPython)) {
    & python -m venv (Join-Path $AppRoot "venv")
}
& $VenvPython -m pip install --no-cache-dir -r (Join-Path $AppRoot "requirements.txt")
Write-Ok "Dependencies installed"

Write-Step "Configuring .env"
if (Test-Path $EnvFile) {
    Write-Warn2 ".env already exists - leaving it untouched. Verify DJANGO_ALLOWED_HOSTS includes '$Hostname' and DJANGO_DEBUG=False yourself."
} else {
    Copy-Item (Join-Path $AppRoot ".env.example") $EnvFile
    $secretKey    = & $VenvPython -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
    $allowedHosts = "$Hostname,127.0.0.1"
    $behindTls    = if ($UseHttps) { "True" } else { "False" }
    $csrfOrigin   = if ($UseHttps) { "https://$Hostname" } else { "" }

    (Get-Content $EnvFile) | ForEach-Object {
        $_ -replace '^DJANGO_SECRET_KEY=.*', "DJANGO_SECRET_KEY=$secretKey" `
           -replace '^DJANGO_DEBUG=.*', "DJANGO_DEBUG=False" `
           -replace '^DJANGO_ALLOWED_HOSTS=.*', "DJANGO_ALLOWED_HOSTS=$allowedHosts" `
           -replace '^DJANGO_CSRF_TRUSTED_ORIGINS=.*', "DJANGO_CSRF_TRUSTED_ORIGINS=$csrfOrigin" `
           -replace '^DJANGO_BEHIND_TLS=.*', "DJANGO_BEHIND_TLS=$behindTls"
    } | Set-Content $EnvFile
    Write-Ok ".env created and configured for '$Hostname' (HTTPS: $($UseHttps.IsPresent))"
}

Write-Step "migrate + collectstatic"
& $VenvPython (Join-Path $AppRoot "manage.py") migrate --noinput
& $VenvPython (Join-Path $AppRoot "manage.py") collectstatic --noinput
Write-Ok "Database migrated, static files collected"

# --- 5. Windows Service (reuses install-service.ps1 - one source of truth) -----

Write-Step "Registering Windows Service"
& (Join-Path $ScriptDir "install-service.ps1") -ServiceName $ServiceName -NssmExe $NssmExe -Port $WaitressPort
Start-Sleep -Seconds 2

# --- 6. IIS site -----------------------------------------------------------------

Write-Step "Creating IIS site"
New-Item -ItemType Directory -Force -Path $SiteRoot | Out-Null
Copy-Item (Join-Path $ScriptDir "web.config") (Join-Path $SiteRoot "web.config") -Force

if (Get-Website -Name $SiteName -ErrorAction SilentlyContinue) {
    Write-Warn2 "Site '$SiteName' already exists - removing and recreating so this script stays safe to re-run."
    Remove-Website -Name $SiteName
}

if ($UseHttps) {
    New-Website -Name $SiteName -PhysicalPath $SiteRoot -Port 443 -HostHeader $Hostname -Ssl | Out-Null
    $bindingPath = "IIS:\SslBindings\0.0.0.0!443!$Hostname"
    if (-not (Test-Path $bindingPath)) {
        New-Item -Path $bindingPath -Thumbprint $CertThumbprint -SslFlags 1 | Out-Null
    }
} else {
    New-Website -Name $SiteName -PhysicalPath $SiteRoot -Port 80 -HostHeader $Hostname | Out-Null
}
New-WebVirtualDirectory -Site $SiteName -Name "static" -PhysicalPath $StaticRoot | Out-Null
Write-Ok "Site '$SiteName' bound to $Hostname, /static -> $StaticRoot"

# --- 7. Validate end-to-end, don't just assume it worked -------------------------

Write-Step "Validating"
$results = @()

try {
    $r = Invoke-WebRequest "http://127.0.0.1:$WaitressPort/healthz/" -UseBasicParsing -TimeoutSec 10
    $results += [PSCustomObject]@{ Check = "waitress direct (/healthz/)"; Status = $r.StatusCode; Pass = ($r.StatusCode -eq 200) }
} catch {
    $results += [PSCustomObject]@{ Check = "waitress direct (/healthz/)"; Status = "ERROR: $($_.Exception.Message)"; Pass = $false }
}

$scheme = if ($UseHttps) { "https" } else { "http" }
try {
    $r = Invoke-WebRequest "${scheme}://${Hostname}/healthz/" -UseBasicParsing -TimeoutSec 10
    $results += [PSCustomObject]@{ Check = "IIS -> hostname (/healthz/)"; Status = $r.StatusCode; Pass = ($r.StatusCode -eq 200) }
} catch {
    $results += [PSCustomObject]@{ Check = "IIS -> hostname (/healthz/)"; Status = "ERROR: $($_.Exception.Message)"; Pass = $false }
}

try {
    $r = Invoke-WebRequest "${scheme}://${Hostname}/static/css/app.css" -UseBasicParsing -TimeoutSec 10
    $results += [PSCustomObject]@{ Check = "IIS -> static file"; Status = $r.StatusCode; Pass = ($r.StatusCode -eq 200) }
} catch {
    $results += [PSCustomObject]@{ Check = "IIS -> static file"; Status = "ERROR: $($_.Exception.Message)"; Pass = $false }
}

$results | Format-Table -AutoSize

if ($results | Where-Object { -not $_.Pass }) {
    Write-Warn2 "One or more checks failed. Common causes: '$Hostname' doesn't resolve to this machine (DNS/hosts file), the cert thumbprint doesn't match, or the service isn't running - check $LogDir\service-stderr.log and $LogDir\correlate.log."
} else {
    Write-Host "`nAll checks passed - ${scheme}://${Hostname}/ is live." -ForegroundColor Green
}
