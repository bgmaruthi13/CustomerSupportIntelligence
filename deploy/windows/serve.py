"""Production entrypoint — runs the app under waitress (a pure-Python,
Windows-native WSGI server) instead of the Django dev server.

Intended to be wrapped as a Windows Service (see install-service.ps1) and to
sit behind IIS + Application Request Routing, which terminates TLS and
reverse-proxies to this process on localhost. This is why it binds to
127.0.0.1 by default, never 0.0.0.0 — the app process itself should never be
directly reachable from outside the box, only through IIS.
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "correlate.settings")

from waitress import serve  # noqa: E402
from correlate.wsgi import application  # noqa: E402

if __name__ == "__main__":
    host = os.environ.get("WAITRESS_HOST", "127.0.0.1")
    port = int(os.environ.get("WAITRESS_PORT", "8000"))
    threads = int(os.environ.get("WAITRESS_THREADS", "4"))
    print(f"Starting Correlate AI under waitress on {host}:{port} ({threads} threads)...", flush=True)
    serve(application, host=host, port=port, threads=threads, ident="Correlate AI")
