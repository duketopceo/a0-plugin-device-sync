"""Peer discovery + engine status.

POST /api/plugins/device_sync/peers — body: {}
Returns the peer list (TTL-cached discovery) with per-peer last-sync
bookkeeping plus engine status. "peers" is the fresh discovered view;
"status.peers" is the engine's cached map — same data, different window.
"""

from __future__ import annotations

from helpers.api import ApiHandler

from usr.plugins.device_sync.helpers.auth import PeerEndpoint


class Peers(PeerEndpoint, ApiHandler):
    async def process(self, input, request):
        from usr.plugins.device_sync.helpers import runtime

        denial = self.deny(request)
        if denial is not None:
            return denial
        return {"ok": True, "peers": runtime.peers(), "status": runtime.status()}
