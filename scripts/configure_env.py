#!/usr/bin/env python
"""Creates .env for Correlate AI with local-development/first-run defaults:
DJANGO_SECRET_KEY, DJANGO_DEBUG, DJANGO_ALLOWED_HOSTS, DJANGO_BEHIND_TLS, and
(if given) DJANGO_CSRF_TRUSTED_ORIGINS.

This is the one place that logic lives — install.bat, deploy/redhat/install.sh,
deploy/windows/deploy-all.ps1, and deploy/windows/run-as-service.ps1 all call
this instead of each carrying its own copy of the same env-file-writing code.

The database is always SQLite (a single db.sqlite3 file, see
correlate/settings.py) — nothing here, or anywhere else in this repo, talks to
a database server.

Never overwrites an existing .env — every installer already gates this call on
".env doesn't exist yet", and this script re-checks that itself so it's safe
to invoke directly too.

Usage:
    python scripts/configure_env.py [--allowed-hosts H1,H2] [--debug true|false]
                                     [--behind-tls true|false]
                                     [--csrf-trusted-origins ORIGIN]
"""
import argparse
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = APP_ROOT / ".env"


def generate_secret_key():
    from django.core.management.utils import get_random_secret_key
    return get_random_secret_key()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowed-hosts", default="localhost,127.0.0.1")
    parser.add_argument("--debug", default="True")
    parser.add_argument("--behind-tls", default="False")
    parser.add_argument("--csrf-trusted-origins", default=None)
    return parser.parse_args()


def main():
    args = parse_args()

    if ENV_PATH.exists():
        print(f".env already exists at {ENV_PATH} - leaving it untouched.")
        return

    lines = [
        f"DJANGO_SECRET_KEY={generate_secret_key()}",
        f"DJANGO_DEBUG={args.debug}",
        f"DJANGO_ALLOWED_HOSTS={args.allowed_hosts}",
        f"DJANGO_BEHIND_TLS={args.behind_tls}",
    ]
    if args.csrf_trusted_origins:
        lines.append(f"DJANGO_CSRF_TRUSTED_ORIGINS={args.csrf_trusted_origins}")

    ENV_PATH.write_text("\n".join(lines) + "\n")
    print(f".env created at {ENV_PATH}")


if __name__ == "__main__":
    main()
