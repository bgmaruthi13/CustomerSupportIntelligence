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
| `install-service.ps1` | Registers `serve.py` as an auto-restarting Windows Service via NSSM. |
| `web.config` | IIS + ARR reverse-proxy rule (goes in the IIS site root, not the app folder). |
