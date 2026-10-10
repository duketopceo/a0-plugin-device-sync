"""Peer-token auth for the sync endpoints.

These endpoints serve machine-to-machine traffic (other a0 boxes), not
browser sessions — a0's cookie/session auth can't ride a urllib client
without a login dance, and CSRF is meaningless without an ambient
credential. The plugin substitutes a shared bearer token
(``DEVICE_SYNC_TOKEN`` / ``sync_token`` config):

- handlers set ``requires_auth() = False`` and ``requires_csrf() = False``
  and gate on ``check_peer_request`` instead;
- no configured token -> every endpoint refuses (secure default);
- the token is never logged or echoed (``config.to_dict`` reports only
  ``token_set``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from usr.plugins.device_sync.helpers import runtime

# Identity-probe challenge header — see check_peer_request/peer_proof.
NONCE_HEADER = "X-Device-Sync-Nonce"
_PROOF_DOMAIN = "a0-device-sync-peer-v1:"


def extract_token(request: Any) -> str:
    """Bearer token from Authorization or X-Device-Sync-Token."""
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("X-Device-Sync-Token") or "").strip()


def peer_proof(nonce: str, token: str) -> str:
    """HMAC-SHA256 proof of shared-token possession bound to a fresh nonce —
    the identity probe's challenge response. The token itself never rides
    the probe, so a hostile port can't harvest it; the domain prefix keeps
    proofs unusable for any other purpose."""
    return hmac.new(
        token.encode(), (_PROOF_DOMAIN + nonce).encode(), hashlib.sha256
    ).hexdigest()


def json_error(message: str, status: int) -> Any:
    from helpers.api import Response

    return Response(
        json.dumps({"ok": False, "error": message}),
        status=status,
        mimetype="application/json",
    )


def forbidden(request: Any = None) -> Any:
    """403 for failed peer auth. When the request carries the
    X-Device-Sync-Nonce challenge AND a token is configured, the body
    includes the HMAC proof — that's what lets a remote _is_peer probe
    verify the endpoint shares the token without ever sending it."""
    body: dict[str, Any] = {"ok": False, "error": "forbidden"}
    if request is not None:
        nonce = (request.headers.get(NONCE_HEADER) or "").strip()
        proof = runtime.hmac_proof(nonce) if nonce else ""
        if proof:
            body["proof"] = proof
    from helpers.api import Response

    return Response(
        json.dumps(body), status=403, mimetype="application/json"
    )


def read_body_capped(request: Any, cap: int) -> bytes:
    """Read a raw request body with a HARD size cap. Content-Length is
    advisory — absent under chunked encoding, or a malicious peer can
    under-report it — so the cap is enforced on the stream itself, not
    the header. Raises ValueError past the cap."""
    length = getattr(request, "content_length", None)
    if length is not None and length > cap:
        raise ValueError(f"upload exceeds the {cap}-byte cap")
    stream = getattr(request, "stream", None)
    if stream is not None:
        raw = stream.read(cap + 1)
    else:
        raw = request.get_data()
    if len(raw) > cap:
        raise ValueError(f"upload exceeds the {cap}-byte cap")
    return raw


def safe_error(e: Exception) -> str:
    """Peer-facing error text: ValueError messages are deliberate
    validation details; anything else gets a generic body (log holds the
    traceback, not the response)."""
    if isinstance(e, ValueError):
        return str(e)
    import logging

    logging.getLogger("a0.device_sync").exception("device-sync handler error")
    return "internal error"


class PeerEndpoint:
    """Shared m2m contract for the sync api handlers — NOT an ApiHandler
    subclass. a0's per-file loader scans module members and takes the
    first ApiHandler subclass it finds; an imported ApiHandler-derived
    base would hijack every handler file. A plain mixin is invisible to
    the loader while still providing the classmethods."""

    @classmethod
    def get_methods(cls):
        return ["POST"]

    @classmethod
    def requires_auth(cls):
        return False  # bearer-token gated in process()

    @classmethod
    def requires_csrf(cls):
        return False  # machine-to-machine; no ambient credential

    def deny(self, request: Any):
        return check_peer_request(request)


def check_peer_request(request: Any):
    """Return a 403 Response when the request fails token auth, else None.
    Also refuses when the plugin is disabled (inactive)."""
    if not runtime.is_active():
        return forbidden(request)
    if not runtime.token_ok(extract_token(request)):
        return forbidden(request)
    return None
