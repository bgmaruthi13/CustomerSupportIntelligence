"""Client for the Copilot HTTP Bridge (BACKLOG.md Theme A) — a local VS Code
extension that exposes the user's signed-in GitHub Copilot chat model over
plain HTTP while its Extension Development Host window stays open. This is
the prototyping-tier LLM provider for this app, not assumed to be the
permanent production one — see BACKLOG.md Theme A for the real caveats (no
auth on the bridge itself, not an always-on service, Copilot license scope,
single-seat rate limits).

Every caller should treat LLMBridgeUnavailable as a normal, expected outcome
(the VS Code window isn't open right now) and fail visibly — same "clear
error instead of silent fallback" convention clustering.pipelines already
uses for the embedding model path (EmbeddingModelUnavailable /
embedding_model_status()) — never save an empty/placeholder AI field on
failure.
"""

import os

import requests

DEFAULT_BRIDGE_URL = "http://127.0.0.1:3939"
DEFAULT_TIMEOUT_SECONDS = 30


class LLMBridgeUnavailable(Exception):
    pass


def _bridge_url():
    """Resolution order: SiteSettings.llm_bridge_url (live-editable from the
    Clustering Settings page, no restart needed) → LLM_BRIDGE_URL env var →
    DEFAULT_BRIDGE_URL. The DB setting is checked first so the in-app config
    page is authoritative once someone's actually used it, while the env var
    keeps working unchanged for anyone who set it up before this existed."""
    from core.models import SiteSettings
    configured = SiteSettings.load().llm_bridge_url.strip()
    if configured:
        return configured.rstrip("/")
    return os.environ.get("LLM_BRIDGE_URL", DEFAULT_BRIDGE_URL).rstrip("/")


def ask(prompt, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Sends `prompt` to the bridge's /ask endpoint, returns the reply text
    (stripped, guaranteed non-empty). Raises LLMBridgeUnavailable with a
    specific, actionable message on any failure — connection refused, a
    timeout, a non-200 response, or a malformed/empty reply — rather than
    ever returning a placeholder."""
    if not prompt or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    url = f"{_bridge_url()}/ask"
    try:
        response = requests.post(url, json={"prompt": prompt}, timeout=timeout)
    except requests.exceptions.ConnectionError as exc:
        raise LLMBridgeUnavailable(
            f"Couldn't reach the Copilot HTTP Bridge at {url} — is the VS Code Extension "
            "Development Host window running? (see copilot-http-bridge/README.md)"
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise LLMBridgeUnavailable(f"Copilot HTTP Bridge at {url} timed out after {timeout}s.") from exc
    except requests.exceptions.RequestException as exc:
        raise LLMBridgeUnavailable(f"Copilot HTTP Bridge request to {url} failed: {exc}") from exc

    try:
        body = response.json()
    except ValueError as exc:
        raise LLMBridgeUnavailable(f"Copilot HTTP Bridge at {url} returned a non-JSON response (status {response.status_code}).") from exc

    if response.status_code != 200:
        raise LLMBridgeUnavailable(f"Copilot HTTP Bridge returned {response.status_code}: {body.get('error', body)}")

    reply = body.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        raise LLMBridgeUnavailable(f"Copilot HTTP Bridge at {url} returned an empty reply.")
    return reply.strip()


def bridge_status():
    """Returns (available: bool, message: str) without raising — for a live
    status indicator, mirroring clustering.pipelines.embedding_model_status().
    Makes a real (cheap, short-timeout) round-trip rather than just checking
    the URL is well-formed, since "the bridge process isn't running" is the
    normal failure mode this exists to catch."""
    try:
        ask("Reply with exactly one word: ok", timeout=10)
        return True, f"Reachable at {_bridge_url()}"
    except LLMBridgeUnavailable as exc:
        return False, str(exc)
