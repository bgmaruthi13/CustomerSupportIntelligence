# Installing Correlate AI on Red Hat Enterprise Linux 8 & 9

Linux counterpart to `install.bat` (the Windows dev quick-start). One
difference in default posture: **Postgres is the primary/recommended
database here, not SQLite** — `install.sh` sets up a local Postgres instance
automatically unless you pass `--sqlite`. See `.env.example`'s `DATABASE_URL`
comment for why: SQLite is fine for a single-machine pilot but isn't safe for
more than one concurrent writer.

> **Not yet verified against a live RHEL box** — this was written and
> reviewed carefully (including a `bash -n` syntax check), but no RHEL 8/9
> machine was available in the environment this was built in. Exact package
> names/module streams for `postgresql-server` do shift between RHEL minor
> releases; `install.sh` tries the common case and prints specific next steps
> if it doesn't match your exact release, rather than failing silently. Please
> report back anything that needs adjusting for your specific version.

## Quick start

```bash
cd /opt/correlate-ai   # wherever you cloned/copied the app
chmod +x deploy/redhat/install.sh
./deploy/redhat/install.sh
```

This will, in order:

1. Sanity-check the OS (RHEL/Rocky/Alma 8 or 9 — warns but doesn't block on
   anything else, in case you're on a close-enough derivative).
2. Find a Python 3.12 interpreter (`requirements.txt` is version-pinned
   against 3.12.10, including the PyTorch CPU wheel) — warns if only an older
   `python3` is found (RHEL 8/9's default system Python is commonly 3.9) and
   tells you how to install 3.12 alongside it, without replacing the system
   interpreter.
3. Create a venv, install `requirements.txt`.
4. **Set up Postgres**: install `postgresql-server` if missing, initialize
   and start it, create a `correlate_user` role and `correlate` database
   (idempotent — safe to re-run, won't touch an existing role's password).
   Skip this whole step with `./deploy/redhat/install.sh --sqlite`.
5. Bootstrap `.env` (secret key, allowed hosts, `DATABASE_URL` pointed at the
   Postgres instance just created) if one doesn't already exist.
6. Run migrations and `collectstatic`.

At the end it prints the dev quick-start command (`manage.py runserver`,
foreground, matches `install.bat`'s scope) and flags two things worth
checking on a fresh RHEL box specifically:

- **firewalld** — if active, you'll need to open whichever port you actually
  run the app on for another machine to reach it.
- **SELinux** — if `Enforcing`, a permission-shaped failure with no Python
  traceback (port bind, Postgres connection) is worth checking
  `ausearch -m avc -ts recent` for before assuming it's an app bug.

## Verification checklist

Since this script hasn't been run against a live RHEL box in the environment
it was built in, treat this as the acceptance check the first time you
actually run it for real — each item is a concrete command with an expected
result, not just "make sure it works":

- [ ] **Python version** — `venv/bin/python --version` → `3.12.x`. If it
      printed a warning about a different version during install, this is
      where that gets confirmed one way or the other.
- [ ] **Dependencies installed cleanly** — `venv/bin/pip check` → no output
      (no broken/conflicting requirements).
- [ ] **(Postgres mode only) service running** —
      `sudo systemctl status postgresql` → `active (running)`.
- [ ] **(Postgres mode only) role + database exist** —
      `sudo -u postgres psql -c '\du'` lists `correlate_user`;
      `sudo -u postgres psql -c '\l'` lists `correlate`.
- [ ] **`.env` populated correctly** —
      `grep -E 'DJANGO_SECRET_KEY|DATABASE_URL' .env`: secret key is a long
      random string (not the `django-insecure-...` placeholder), and
      `DATABASE_URL` is present for Postgres mode / absent for `--sqlite`.
- [ ] **Migrations fully applied** —
      `venv/bin/python manage.py showmigrations | grep '\[ \]'` → no output
      (an unchecked `[ ]` line means a migration didn't apply).
- [ ] **Static files collected** — `ls staticfiles/css/app.css` → file exists.
- [ ] **App actually serves AND the database is genuinely reachable** — this
      is the one end-to-end check that catches a wrong Postgres password or
      an unreachable DB, not just "the process started": start the app
      (`venv/bin/python manage.py runserver 0.0.0.0:8000`, or via the
      systemd service below), then from the same box:
      `curl http://127.0.0.1:8000/healthz/` → `{"status": "ok"}`.
      `core.views.healthz` runs a real `SELECT 1` against whatever database
      is configured, so a `{"status": "error", ...}` response here means the
      DB connection itself is broken, not the web server.
- [ ] **Can actually log in** — `venv/bin/python manage.py createsuperuser`,
      then sign in through a browser (or `curl -c cookies.txt` the login
      flow) — confirms sessions/auth work against whichever DB is configured,
      not just that migrations ran.
- [ ] **(If reachable beyond localhost) firewalld port open** — from a
      *different* machine: `curl http://<this-host>:8000/healthz/` →
      `{"status": "ok"}`. If it hangs/refuses, re-check the
      `firewall-cmd --add-port` step the script printed.
- [ ] **(If SELinux is Enforcing) no relevant denials** —
      `sudo ausearch -m avc -ts recent` → no entries referencing this app's
      port, the venv's Python binary, or `postgresql`. A permission-shaped
      failure with no Python traceback is the usual symptom of one.

## Choosing the database

| | `./install.sh` (default) | `./install.sh --sqlite` |
|---|---|---|
| Database | Local PostgreSQL, auto-installed/configured | SQLite file (`db.sqlite3`) |
| Extra dependency | `requirements-postgres.txt` (`psycopg2-binary`) | None |
| Good for | Any real deployment, multiple workers/writers | Single-machine pilot, local dev |
| `DATABASE_URL` | Set automatically in `.env` | Left unset — SQLite is the zero-config fallback |

Switching later is just changing `DATABASE_URL` in `.env` and re-running
`manage.py migrate` against the new target — the app code itself doesn't
change either way (`dj_database_url` already handles both).

## Running it for real: waitress + systemd

`install.sh` gets you to a working dev server (`manage.py runserver`) — for
an actual deployment, run the same production WSGI entrypoint the Windows
path uses (`deploy/windows/serve.py` — it's plain Python/waitress, nothing
Windows-specific despite the folder name) under `systemd` instead of NSSM:

```bash
sudo useradd -r -s /sbin/nologin correlate
sudo cp deploy/redhat/correlate-ai.service /etc/systemd/system/
sudo $EDITOR /etc/systemd/system/correlate-ai.service
# Update the WorkingDirectory/ExecStart paths and User/Group to match your
# actual install location and the user you just created, then:
sudo chown -R correlate:correlate /opt/correlate-ai
sudo systemctl daemon-reload
sudo systemctl enable --now correlate-ai
sudo systemctl status correlate-ai
```

Binds to `127.0.0.1:8000` by default (same as the Windows service) — put
nginx or Apache in front to terminate TLS and reverse-proxy, the Linux
equivalent of the Windows path's IIS + ARR. That reverse-proxy config isn't
included here (out of scope for this pass) — `deploy/windows/web.config`
shows the shape of the rewrite rule an nginx `proxy_pass` block would mirror
if you need a reference.

Set `DJANGO_DEBUG=False` and a real `DJANGO_ALLOWED_HOSTS`/`DJANGO_SECRET_KEY`
in `.env` before doing this — `install.sh` bootstraps `.env` with dev
defaults (`DJANGO_DEBUG=True`), same as `install.bat` does on Windows.

## Log scanning: scheduled scans and continuous tailing

Same two pieces the Windows deployment needs (see `deploy/windows/README.md`
for the full explanation of each trigger mode) — on RHEL, use `cron` and a
second `systemd` unit instead of Task Scheduler/NSSM:

**Scheduled** — a cron entry calling `run_scheduled_scans`:

```bash
# crontab -e (as whichever user should run it)
0 2 * * * /opt/correlate-ai/venv/bin/python /opt/correlate-ai/manage.py run_scheduled_scans
```

**Continuous (tailing)** — `tail_log_sources` as its own always-on systemd
service, same shape as `correlate-ai.service` above but with
`ExecStart=.../venv/bin/python manage.py tail_log_sources` and no
`WAITRESS_*` environment lines — copy `correlate-ai.service`, adjust those
two lines, install it as `correlate-ai-logwatcher.service` alongside the main
one.

## Updating the app later

```bash
sudo systemctl stop correlate-ai
git pull   # or however you deploy new code
venv/bin/pip install --no-cache-dir -r requirements.txt
[ -f requirements-postgres.txt ] && venv/bin/pip install --no-cache-dir -r requirements-postgres.txt
venv/bin/python manage.py migrate --noinput
venv/bin/python manage.py collectstatic --noinput
sudo systemctl start correlate-ai
```

## Files in this folder

| File | Purpose |
|---|---|
| `install.sh` | RHEL 8/9 install/quick-start script — venv, deps, Postgres setup, `.env` bootstrap, migrate, collectstatic. |
| `correlate-ai.service` | systemd unit template for running `deploy/windows/serve.py` (waitress) as a managed service — edit the paths/user before installing. |
