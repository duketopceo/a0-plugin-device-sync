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

import json
from typing import Any

from helpers.api import Response

from usr.plugins.device_sync.helpers import runtime


def extract_token(request: Any) -> str:
    """Bearer token from Authorization or X-Device-Sync-Token."""
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("X-Device-Sync-Token") or "").strip()


def forbidden() -> Response:
    return Response(
        json.dumps({"ok": False, "error": "forbidden"}),
        status=403,
        mimetype="application/json",
    )


def check_peer_request(request: Any) -> Response | None:
    """Return a 403 Response when the request fails token auth, else None.
    Also refuses when the plugin is disabled (inactive)."""
    if not runtime.is_active():
        return forbidden()
    if not runtime.token_ok(extract_token(request)):
        return forbidden()
    return None
