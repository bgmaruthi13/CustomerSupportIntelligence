# Deploying Correlate AI on Windows Server 2022

Production deployment: **waitress** (WSGI server) run as a **Windows Service via
NSSM** (auto-start, auto-restart-on-crash — the supervisord equivalent), sitting
behind **IIS + Application Request Routing** (reverse proxy + TLS termination,
the nginx equivalent). Single worker process — this app currently runs on
SQLite, which is fine for one worker but not safe for multiple concurrent
writers; don't scale to multiple waitress workers without first switching to
PostgreSQL (`DATABASE_URL`, already supported — see `.env.example`).

This mirrors `install.bat`'s dev setup (same venv, same `manage.py migrate` /
`collectstatic` steps) but skips `runserver` and `DEBUG=True` — this is the
production path, not the quick-start one.

**Quick paths — two scripts, pick based on what you actually need:**

- **`run-as-service.ps1`** — just "runs as a Windows Service, reachable at
  `http://hostname:port/`." No IIS, no ARR, no TLS. Registers the service bound
  to every network interface (not just loopback) so other machines can reach it
  directly. This is almost always the right starting point.
  ```powershell
  cd deploy\windows
  .\run-as-service.ps1                          # -> http://<this machine's hostname>:8000/
  .\run-as-service.ps1 -Hostname reports -Port 80  # -> http://reports/  (no port suffix)
  ```
- **`deploy-all.ps1`** — the full production path below (IIS + URL Rewrite +
  ARR + a real hostname on port 80/443 + optional TLS), bundled into one
  script. Reach for this only once you actually need HTTPS or a clean hostname
  with no port number — it's meaningfully more moving parts (installs IIS
  itself if missing, detects Windows Server vs. a client OS automatically since
  `Install-WindowsFeature` doesn't exist on client Windows at all).
  ```powershell
  cd deploy\windows
  .\deploy-all.ps1 -Hostname correlate.yourcompany.local
  # or, with a real certificate already installed:
  .\deploy-all.ps1 -Hostname correlate.yourcompany.com -UseHttps -CertThumbprint AB12CD34...
  ```

Both end with an actual pass/fail check (hitting `/healthz/` through the real
URL you'd browse to) rather than assuming success. Read the steps below anyway
before running either against a real box — registering a Windows Service (and,
for `deploy-all.ps1`, installing IIS/ARR) are system-level, hard-to-reverse
changes.

## Development vs. production — both work, same codebase

Nothing about this deployment setup replaces or breaks `manage.py runserver`.
They're two different ways to run the exact same app — pick whichever matches
what you're doing:

| | Development (`install.bat` / `runserver`) | Production (this folder) |
|---|---|---|
| **Command** | `venv\Scripts\python manage.py runserver 8000` | Windows Service running `serve.py` (waitress) |
| **`.env`** | `DJANGO_DEBUG=True` | `DJANGO_DEBUG=False`, real `DJANGO_SECRET_KEY` |
| **Server** | Django's dev server — single-threaded, auto-reloads on code changes, shows in-browser tracebacks | waitress — production WSGI server, no auto-reload, no debug tracebacks |
| **Fronted by** | Nothing — hit `http://127.0.0.1:8000/` directly | IIS + ARR (TLS termination, reverse proxy) |
| **Process management** | None — Ctrl+C in the console window stops it | NSSM-managed Windows Service — auto-restart, auto-start on boot |
| **When to use** | Local iteration, testing changes | Actually running the app for real users |

Both were verified working back-to-back against the same `.env`/database — you
can run `manage.py runserver` for local dev today and deploy the exact same
checkout to a server with this folder's tooling tomorrow, no code changes
required either way.

## 1. Prerequisites (one-time, per server)

- **Python 3.12** installed, on PATH (`requirements.txt` is version-pinned
  against 3.12.10 specifically, including the PyTorch CPU wheel — a different
  Python 3.x will very likely fail to resolve that pin).
- **IIS** — enable via Server Manager > Add Roles and Features > Web Server (IIS),
  or PowerShell: `Install-WindowsFeature -Name Web-Server -IncludeManagementTools`.
- **URL Rewrite module** — download and install:
  https://www.iis.net/downloads/microsoft/url-rewrite
- **Application Request Routing (ARR)** — download and install:
  https://www.iis.net/downloads/microsoft/application-request-routing
- **NSSM** — download the release zip from https://nssm.cc/download, extract
  `nssm.exe` (the `win64` build) somewhere on PATH (e.g. `C:\Windows\System32`
  or a dedicated `C:\tools\` folder added to PATH).

After installing ARR, enable its proxy feature (off by default):
IIS Manager > click the **server** node (not a site) > **Application Request
Routing Cache** > **Server Proxy Settings...** (right panel) > check **Enable
proxy** > Apply.

Allow-list the two custom server variables `web.config` sets, since IIS blocks
rewrite rules from setting arbitrary server variables until you do this:
IIS Manager > click the **server** node > **URL Rewrite** > **View Server
Variables** (right panel) > **Add...** > add `HTTP_X_FORWARDED_PROTO` and
`HTTP_X_FORWARDED_HOST`.

(Both of the above can also be done via `appcmd.exe` if you're scripting a
repeatable server build — see Microsoft's ARR/URL Rewrite docs for the exact
`appcmd` invocations; the GUI steps above are the one-time, easier path for a
single server.)

## 2. Deploy the app

```powershell
# Copy/clone the app to its target folder, e.g. C:\apps\correlate-ai\, then,
# using Python 3.12 (requirements.txt is pinned against 3.12.10 - see note below):
cd C:\apps\correlate-ai
python -m venv venv
venv\Scripts\pip install --no-cache-dir -r requirements.txt
# ^ this one command installs everything, including the PyTorch CPU build -
# requirements.txt's --extra-index-url line handles that, no separate step needed.

copy .env.example .env
notepad .env
# Set at minimum: DJANGO_DEBUG=False, a real DJANGO_SECRET_KEY (generate one
# per the comment in .env.example), DJANGO_ALLOWED_HOSTS to your real
# hostname, DJANGO_BEHIND_TLS=True (IIS is terminating TLS in front of this).

venv\Scripts\python manage.py migrate --noinput
venv\Scripts\python manage.py collectstatic --noinput
```

## 3. Install the Windows Service (NSSM + waitress)

Run as **Administrator**:

```powershell
cd C:\apps\correlate-ai\deploy\windows
.\install-service.ps1
```

This registers a `CorrelateAI` service running `serve.py` (waitress, bound to
`127.0.0.1:8000` — never exposed directly, only reachable through IIS),
auto-start on boot, auto-restart on crash. Re-running the script is safe — it
stops/removes any prior install first.

Verify it's actually serving before moving to IIS:

```powershell
Invoke-WebRequest http://127.0.0.1:8000/healthz/ -UseBasicParsing
# Expect: {"status": "ok"}
```

If it's not running, check `logs\service-stderr.log` and `logs\correlate.log`
in the app folder.

## 4. Configure the IIS site

1. IIS Manager > **Sites** > **Add Website...**
   - Site name: `Correlate AI`
   - Physical path: a folder that contains **only** `web.config` (copy
     `deploy\windows\web.config` there) — **not** the Django app folder itself.
     A separate, minimal site root keeps IIS from ever serving app source/`.env`
     if reverse-proxy rules are ever misconfigured.
   - Binding: port 443 with your TLS certificate (or 80 for an internal/trusted
     network — if so, also set `DJANGO_BEHIND_TLS=False` in `.env` and drop the
     `HTTP_X_FORWARDED_PROTO` server variable in `web.config`, otherwise Django
     will force-redirect every request to HTTPS that doesn't exist).

2. Add the static files virtual directory: right-click the new site > **Add
   Virtual Directory...**
   - Alias: `static`
   - Physical path: `C:\apps\correlate-ai\staticfiles`

   This lets IIS serve `/static/*` directly — faster than round-tripping
   through waitress/whitenoise for every CSS/JS file — while `web.config`'s
   rewrite rule explicitly excludes `/static/` from the proxy rule so IIS's
   own static handler takes it instead.

3. Copy `web.config` into the site's physical root (step 1) if you haven't
   already.

## 5. Verify end-to-end

```powershell
Invoke-WebRequest https://your-hostname/healthz/ -UseBasicParsing
Invoke-WebRequest https://your-hostname/static/css/app.css -UseBasicParsing
```

Then open `https://your-hostname/` in a browser and log in.

## 6. Log scanning: scheduled scans and continuous tailing

Log sources configured in the app (Log Sources page) with trigger mode
**On-demand** need nothing further — "Scan Now" launches a scan directly.
**Scheduled** and **Continuous** sources need one more piece registered
outside the app, same "this app doesn't schedule itself, an external
mechanism does" pattern as everything else in this file:

**Scheduled** — register `run_scheduled_scans` as a Windows Scheduled Task
(no NSSM needed here; a periodic task, not a standing service, is the right
tool — see the Task Scheduler vs. NSSM trade-off note earlier in this file
under the "Quick paths" section):

```powershell
$action = New-ScheduledTaskAction -Execute "C:\apps\correlate-ai\venv\Scripts\python.exe" `
  -Argument "manage.py run_scheduled_scans" -WorkingDirectory "C:\apps\correlate-ai"
$trigger = New-ScheduledTaskTrigger -Daily -At 2am
Register-ScheduledTask -TaskName "CorrelateAI-ScheduledLogScans" -Action $action -Trigger $trigger -User "SYSTEM" -RunLevel Highest
```

Adjust `-Daily -At 2am` to whatever cadence fits your log volume — this only
scans the bytes added since each source's last scan, not the whole file every
time, so more frequent runs stay cheap.

**Continuous (tailing)** — needs `tail_log_sources` running as its own
always-on Windows Service, exactly like `install-service.ps1` registers
`serve.py`, just pointed at a different entrypoint:

```powershell
cd deploy\windows
.\install-log-watcher-service.ps1
```

This registers a separate `CorrelateAI-LogWatcher` service (auto-start,
auto-restart-on-crash, same NSSM pattern as the main app service) that loops
indefinitely, scanning new bytes appended to any active continuous source as
they're written. It runs alongside the main `CorrelateAI` service, not
instead of it — stopping/updating one doesn't affect the other.

## Updating the app later

```powershell
# Stop the service, pull/copy new code, reinstall deps if requirements.txt
# changed, re-migrate, re-collectstatic, restart:
nssm stop CorrelateAI
venv\Scripts\pip install --no-cache-dir -r requirements.txt
venv\Scripts\python manage.py migrate --noinput
venv\Scripts\python manage.py collectstatic --noinput
nssm start CorrelateAI
```

## Files in this folder

| File | Purpose |
|---|---|
| `serve.py` | Production WSGI entrypoint — runs the app under waitress. |
| `install-service.ps1` | Registers `serve.py` as an auto-restarting Windows Service via NSSM. `-BindHost` controls whether it's loopback-only (default, for the IIS path) or open to the network (`0.0.0.0`, for the direct `hostname:port` path). |
| `run-as-service.ps1` | Quick path: app setup + Windows Service, bound to `0.0.0.0`, no IIS. |
| `deploy-all.ps1` | Quick path: everything, including IIS + ARR + URL Rewrite + TLS. |
| `web.config` | IIS + ARR reverse-proxy rule (goes in the IIS site root, not the app folder). |
