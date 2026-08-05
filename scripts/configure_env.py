#!/usr/bin/env python
"""Creates or updates .env for Correlate AI: DJANGO_* bootstrap defaults, plus
the SQLite/PostgreSQL choice for DATABASE_URL.

This is the one place that logic lives — install.bat, deploy/redhat/install.sh,
deploy/windows/deploy-all.ps1, and deploy/windows/run-as-service.ps1 all call
this instead of each carrying its own copy of the same env-file-writing and
database-prompt code.

Never installs, initializes, or starts a PostgreSQL *server*. Postgres mode
only ever connects to a server the operator already has running (local or
remote) — it installs the psycopg2 client driver into the venv and, if asked
interactively, prompts for that server's connection details.

Only ever touches the specific keys it's responsible for (DJANGO_SECRET_KEY,
DJANGO_DEBUG, DJANGO_ALLOWED_HOSTS, DJANGO_BEHIND_TLS,
DJANGO_CSRF_TRUSTED_ORIGINS, DATABASE_URL). Every other line in an existing
.env — comments, email settings, anything an operator added — is left
untouched.

Usage:
    python scripts/configure_env.py [--db sqlite|postgres] [--database-url URL]
                                     [--allowed-hosts H1,H2] [--debug true|false]
                                     [--behind-tls true|false]
                                     [--csrf-trusted-origins ORIGIN]
                                     [--non-interactive]

DB_MODE=sqlite|postgres in the environment is equivalent to --db, for scripts
that already export it (kept for install.sh's existing --sqlite/--postgres/
DB_MODE convention).
"""
import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

APP_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = APP_ROOT / ".env"


def read_existing():
    if not ENV_PATH.exists():
        return {}
    values = {}
    for line in ENV_PATH.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            values[key.strip()] = value
    return values


def upsert_env_vars(pairs):
    """Updates/inserts the given key=value pairs, preserving every other
    existing line (comments, blank lines, unrelated vars) untouched."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    remaining = dict(pairs)
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n")


def generate_secret_key():
    from django.core.management.utils import get_random_secret_key
    return get_random_secret_key()


def prompt_db_choice():
    print()
    print("Choose a database backend:")
    print("  [1] SQLite (default) - zero-config, single file, fine for a pilot or local dev")
    print("  [2] PostgreSQL - point this at a server you already have running;")
    print("      this script will NOT install or start a PostgreSQL server for you")
    try:
        choice = input("Enter 1 or 2 [1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nNo input available - defaulting to SQLite.")
        return "sqlite"
    return "postgres" if choice == "2" else "sqlite"


def prompt_postgres_url():
    print()
    print("Enter the connection details for your existing PostgreSQL server.")
    print("(Nothing here installs, initializes, or starts PostgreSQL — it only")
    print(" connects to a server you already have running.)")
    try:
        host = input("  Host [localhost]: ").strip() or "localhost"
        port = input("  Port [5432]: ").strip() or "5432"
        dbname = input("  Database name [correlate]: ").strip() or "correlate"
        user = input("  Username [correlate_user]: ").strip() or "correlate_user"
        password = getpass.getpass("  Password: ")
    except (EOFError, KeyboardInterrupt):
        print("\n[ERROR] No input available to complete the PostgreSQL prompt.")
        print("Re-run with --database-url '<postgres://user:pass@host:port/db>' instead.")
        sys.exit(1)
    return f"postgres://{quote(user)}:{quote(password)}@{host}:{port}/{dbname}"


def ensure_postgres_driver():
    try:
        import psycopg2  # noqa: F401
        return
    except ImportError:
        pass
    print("Installing the PostgreSQL client driver (psycopg2-binary)...")
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "--no-cache-dir",
        "-r", str(APP_ROOT / "requirements-postgres.txt"),
    ])


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", choices=["sqlite", "postgres"], default=os.environ.get("DB_MODE"))
    parser.add_argument("--database-url", default=None,
                         help="Skip the Postgres prompts and use this DATABASE_URL directly.")
    parser.add_argument("--allowed-hosts", default="localhost,127.0.0.1")
    parser.add_argument("--debug", default="True")
    parser.add_argument("--behind-tls", default="False")
    parser.add_argument("--csrf-trusted-origins", default=None)
    parser.add_argument("--non-interactive", action="store_true",
                         help="Never prompt; default to SQLite unless --db/--database-url is given.")
    return parser.parse_args()


def main():
    args = parse_args()
    existing = read_existing()
    is_new = not ENV_PATH.exists()
    interactive = sys.stdin.isatty() and not args.non_interactive

    pairs = {}
    if is_new:
        pairs["DJANGO_SECRET_KEY"] = generate_secret_key()
        pairs["DJANGO_DEBUG"] = args.debug
        pairs["DJANGO_ALLOWED_HOSTS"] = args.allowed_hosts
        pairs["DJANGO_BEHIND_TLS"] = args.behind_tls
        if args.csrf_trusted_origins:
            pairs["DJANGO_CSRF_TRUSTED_ORIGINS"] = args.csrf_trusted_origins

    # Database choice: skip re-prompting if an existing .env already has a
    # DATABASE_URL and nothing explicitly overrides it — same "never silently
    # touch an operator's existing config" convention every installer already
    # follows for the rest of .env.
    has_existing_db_url = bool(existing.get("DATABASE_URL", "").strip())
    if not is_new and has_existing_db_url and args.db is None and args.database_url is None:
        print("Existing DATABASE_URL found in .env - leaving database configuration unchanged.")
    else:
        database_url = args.database_url
        db_mode = args.db
        if database_url:
            db_mode = "postgres"
        elif db_mode is None:
            db_mode = prompt_db_choice() if interactive else "sqlite"

        if db_mode == "postgres":
            ensure_postgres_driver()
            if not database_url:
                if not interactive:
                    print("[ERROR] --db postgres requires --database-url when running non-interactively.")
                    sys.exit(1)
                database_url = prompt_postgres_url()
            pairs["DATABASE_URL"] = database_url
            print("\nUsing PostgreSQL - make sure the server above is reachable before starting the app.")
        else:
            pairs["DATABASE_URL"] = ""
            print("\nUsing SQLite (db.sqlite3) - no server needed.")

    if pairs:
        upsert_env_vars(pairs)
        print(f".env updated at {ENV_PATH}")
    else:
        print(f".env already configured at {ENV_PATH} - nothing to change.")


if __name__ == "__main__":
    main()
