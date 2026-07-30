#!/usr/bin/env bash
# Install / launch script for Red Hat Enterprise Linux 8 & 9 (and compatible
# derivatives — Rocky, AlmaLinux, CentOS Stream, same `dnf` package set).
# Linux counterpart to ../../install.bat's dev quick-start: create a venv,
# install dependencies, set up a database, migrate, and launch. Unlike
# install.bat, Postgres is the default database here, not SQLite — see
# "Choosing the database" below and .env.example's DATABASE_URL comment.
#
# Safe to re-run: every step below is idempotent (skips work already done),
# same convention install.bat already uses.
#
# Usage:
#   ./install.sh              # sets up local Postgres automatically (default)
#   ./install.sh --sqlite     # skip Postgres entirely, use the SQLite fallback
#   DB_MODE=sqlite ./install.sh   # same as --sqlite, via env var
#
# NOTE: this script has been reviewed for correctness and syntax-checked
# (bash -n), but not run against a live RHEL box in this environment — no RHEL
# 8/9 machine was available to test against. Exact package/module-stream names
# for postgresql-server do shift between RHEL 8.x/9.x minor releases; this
# script tries the common cases and prints a clear, specific error rather than
# failing silently if none match your exact release. Please report back if a
# step needs adjusting for your specific minor version.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$APP_DIR"

DB_MODE="${DB_MODE:-postgres}"
for arg in "$@"; do
  case "$arg" in
    --sqlite) DB_MODE="sqlite" ;;
    --postgres) DB_MODE="postgres" ;;
    *) echo "Unknown argument: $arg (expected --sqlite or --postgres)"; exit 1 ;;
  esac
done

echo "============================================================"
echo " Correlate AI - Install / Launch script for RHEL 8 & 9"
echo " Database mode: $DB_MODE"
echo "============================================================"
echo

# --- 0. OS sanity check (informational, not a hard gate) -------------------
if [ -r /etc/os-release ]; then
  . /etc/os-release
  MAJOR_VER="${VERSION_ID%%.*}"
  echo "Detected OS: ${PRETTY_NAME:-unknown} (major version: ${MAJOR_VER:-unknown})"
  case "${ID:-}:${ID_LIKE:-}" in
    *rhel*|*fedora*|*centos*)
      if [ "$MAJOR_VER" != "8" ] && [ "$MAJOR_VER" != "9" ]; then
        echo "[WARNING] This script targets RHEL 8/9 (and Rocky/Alma/CentOS Stream"
        echo "equivalents). Detected major version $MAJOR_VER - package names below"
        echo "(dnf module streams especially) may not match exactly. Continuing anyway."
      fi
      ;;
    *)
      echo "[WARNING] This doesn't look like a RHEL-family distro (no rhel/fedora/centos"
      echo "in ID/ID_LIKE) - this script assumes 'dnf' is available. Continuing anyway."
      ;;
  esac
else
  echo "[WARNING] /etc/os-release not found - can't confirm this is RHEL 8/9. Continuing anyway."
fi
echo

if ! command -v dnf >/dev/null 2>&1; then
  echo "[ERROR] 'dnf' was not found on PATH - this script requires a dnf-based"
  echo "RHEL-family system (RHEL, Rocky, AlmaLinux, CentOS Stream)."
  exit 1
fi

# --- 1. Find a Python 3.12 interpreter --------------------------------------
# requirements.txt is version-pinned against Python 3.12.10 (including the
# PyTorch CPU wheel) - a different Python 3.x will very likely fail to resolve
# that pin, same caveat install.bat prints on Windows.
PYEXE=""
for candidate in python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYEXE="$candidate"
    break
  fi
done

if [ -z "$PYEXE" ]; then
  echo "[ERROR] No python3 interpreter found on PATH."
  echo "Install Python 3.12, e.g.:"
  echo "  sudo dnf install -y python3.12          # if available in your AppStream/EPEL repos"
  echo "  # or, if not available: sudo dnf module enable -y python3.12 && sudo dnf install -y python3.12"
  echo "Then re-run this script."
  exit 1
fi

PYVER="$("$PYEXE" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "Using $PYEXE (Python $PYVER)"

if [ "$PYVER" != "3.12" ]; then
  echo
  echo "[WARNING] This app is tested and pinned against Python 3.12 (e.g. 3.12.10)."
  echo "Found Python $PYVER on PATH. requirements.txt - especially the PyTorch CPU"
  echo "wheel - is pinned for 3.12 and may fail to install, or install a mismatched"
  echo "build, on a different version."
  echo
  echo "RHEL 8/9's default 'python3' is commonly 3.9 - if that's what was just found,"
  echo "install Python 3.12 separately (it does NOT need to replace the system"
  echo "python3): try 'sudo dnf install -y python3.12' first; if that package isn't"
  echo "in your repos, check 'dnf module list python312' or enable EPEL"
  echo "('sudo dnf install -y epel-release') and retry."
  echo
  echo "Continuing with Python $PYVER in 5 seconds anyway..."
  sleep 5
fi
echo

# --- 2. Create the virtual environment + install dependencies --------------
if [ ! -x "venv/bin/python" ]; then
  echo "[1/5] Creating virtual environment..."
  "$PYEXE" -m venv venv
  if [ ! -x "venv/bin/python" ]; then
    echo "[ERROR] Failed to create the virtual environment."
    echo "On RHEL, the venv module sometimes needs a separate package:"
    echo "  sudo dnf install -y python3.12-pip python3.12-devel  (or the python3-* equivalents for whichever interpreter was used above)"
    exit 1
  fi

  echo "[2/5] Upgrading pip..."
  venv/bin/python -m pip install --upgrade pip --quiet

  echo "[3/5] Installing dependencies from requirements.txt..."
  echo "This includes the PyTorch CPU build - the biggest download - please be patient."
  if ! venv/bin/pip install --no-cache-dir -r requirements.txt; then
    echo
    echo "[ERROR] Failed to install dependencies. See the error above."
    echo "Common causes: no internet connection, a Python version other than 3.12"
    echo "(you have $PYVER) with no matching PyTorch wheel, or a missing build"
    echo "toolchain (sudo dnf groupinstall -y 'Development Tools' as a general fix)."
    echo "Delete the 'venv' folder after fixing the issue, then re-run this script."
    exit 1
  fi
else
  echo "Virtual environment already exists - skipping base dependency install."
  echo "Delete the 'venv' folder and re-run this script to force a clean reinstall."
fi

if [ "$DB_MODE" = "postgres" ]; then
  echo
  echo "Installing the Postgres driver (requirements-postgres.txt)..."
  venv/bin/pip install --no-cache-dir -r requirements-postgres.txt
fi
echo

# --- 3. Database setup -------------------------------------------------
DATABASE_URL_VALUE=""

if [ "$DB_MODE" = "postgres" ]; then
  echo "[4/5] Setting up local PostgreSQL..."

  if ! command -v psql >/dev/null 2>&1; then
    echo "postgresql-server not found - installing..."
    if ! sudo dnf install -y postgresql-server; then
      echo "[ERROR] 'dnf install postgresql-server' failed. On some RHEL releases"
      echo "Postgres is delivered as a versioned module stream - try:"
      echo "  sudo dnf module list postgresql"
      echo "  sudo dnf module enable -y postgresql:<stream>   # e.g. postgresql:15"
      echo "  sudo dnf install -y postgresql-server"
      echo "then re-run this script, or re-run with --sqlite to skip Postgres entirely."
      exit 1
    fi
  fi

  PGDATA_DEFAULT="/var/lib/pgsql/data"
  if [ ! -f "$PGDATA_DEFAULT/PG_VERSION" ]; then
    echo "Initializing the PostgreSQL data directory..."
    sudo postgresql-setup --initdb
  fi

  echo "Enabling and starting the postgresql service..."
  sudo systemctl enable --now postgresql

  # Idempotent role/db creation - safe to re-run, only creates what's missing.
  DB_NAME="correlate"
  DB_USER="correlate_user"
  DB_PASS="$(venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(18))')"

  ROLE_EXISTS="$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" || true)"
  if [ "$ROLE_EXISTS" != "1" ]; then
    echo "Creating database role '${DB_USER}'..."
    sudo -u postgres psql -c "CREATE ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASS}';"
  else
    echo "Database role '${DB_USER}' already exists - leaving its password unchanged."
    echo "If you need the DATABASE_URL password to match, reset it manually:"
    echo "  sudo -u postgres psql -c \"ALTER ROLE ${DB_USER} WITH PASSWORD '<new password>';\""
    DB_PASS="<unchanged - see .env's existing DATABASE_URL, or reset it as shown above>"
  fi

  DB_EXISTS="$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" || true)"
  if [ "$DB_EXISTS" != "1" ]; then
    echo "Creating database '${DB_NAME}'..."
    sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"
  else
    echo "Database '${DB_NAME}' already exists - leaving it as-is."
  fi

  DATABASE_URL_VALUE="postgres://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}"
  echo "Postgres ready: database '${DB_NAME}', role '${DB_USER}'."
else
  echo "[4/5] Skipping Postgres setup (--sqlite) - using the zero-config SQLite file (db.sqlite3)."
fi
echo

# --- 4. Local .env bootstrap (first run only) -------------------------------
if [ ! -f ".env" ]; then
  echo "Creating .env with local-development defaults..."
  {
    echo "DJANGO_DEBUG=True"
    echo "DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1"
    echo "DJANGO_BEHIND_TLS=False"
    venv/bin/python -c "from django.core.management.utils import get_random_secret_key; print('DJANGO_SECRET_KEY=' + get_random_secret_key())"
    if [ -n "$DATABASE_URL_VALUE" ]; then
      echo "DATABASE_URL=${DATABASE_URL_VALUE}"
    fi
  } > ".env"
  echo "Created .env - see .env.example for production settings (DEBUG=False etc.)"
elif [ -n "$DATABASE_URL_VALUE" ] && ! grep -q '^DATABASE_URL=' .env; then
  echo "DATABASE_URL=${DATABASE_URL_VALUE}" >> ".env"
  echo "Appended the new DATABASE_URL to your existing .env."
elif [ -n "$DATABASE_URL_VALUE" ]; then
  echo ".env already has a DATABASE_URL set - leaving it unchanged."
  echo "(The Postgres role/db above were still created/verified either way.)"
fi
echo

# --- 5. Migrate + collectstatic ---------------------------------------------
echo "[5/5] Applying database migrations and collecting static files..."
venv/bin/python manage.py migrate --noinput
venv/bin/python manage.py collectstatic --noinput >/dev/null

echo
echo "============================================================"
echo " Correlate is ready."
echo
echo " No account exists yet. Create one with:"
echo "   venv/bin/python manage.py createsuperuser"
echo
echo " Quick-start (dev server, foreground):"
echo "   venv/bin/python manage.py runserver 0.0.0.0:8000"
echo
echo " For a real deployment (waitress + systemd), see deploy/redhat/README.md."
echo "============================================================"
echo

if command -v firewall-cmd >/dev/null 2>&1 && sudo firewall-cmd --state >/dev/null 2>&1; then
  echo "[NOTE] firewalld is active. To reach this app from another machine, open"
  echo "the port you'll run it on, e.g.:"
  echo "  sudo firewall-cmd --permanent --add-port=8000/tcp && sudo firewall-cmd --reload"
  echo
fi

if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = "Enforcing" ]; then
  echo "[NOTE] SELinux is Enforcing. If the app fails to bind its port or reach"
  echo "Postgres in ways that look like a permissions problem with no clear Python"
  echo "traceback, check 'sudo ausearch -m avc -ts recent' for denials before"
  echo "assuming it's an application bug."
  echo
fi
