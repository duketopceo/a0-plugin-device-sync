"""Peer discovery + engine status.

POST /api/plugins/device_sync/peers — body: {}
Runs discovery (Tailscale status probe + manual peers file) and returns
the live peer list with per-peer last-sync bookkeeping plus engine status.
"""

from __future__ import annotations

from helpers.api import ApiHandler


class Peers(ApiHandler):
    @classmethod
    def get_methods(cls):
        return ["POST"]

    @classmethod
    def requires_auth(cls):
        return False  # bearer-token gated in process()

    @classmethod
    def requires_csrf(cls):
        return False  # machine-to-machine; no ambient credential

    async def process(self, input, request):
        from usr.plugins.device_sync.helpers import auth, runtime

        denial = auth.check_peer_request(request)
        if denial is not None:
            return denial
        return {"ok": True, "peers": runtime.peers(), "status": runtime.status()}
