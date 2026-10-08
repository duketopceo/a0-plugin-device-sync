"""Manual sync trigger.

POST /api/plugins/device_sync/sync_now — body:
  {"peer": "<name>"?, "direction": "push"|"pull"|"bidirectional"?}
Omitting peer syncs every discovered peer. Returns per-peer results —
individual peer failures degrade to entries in results[].errors, not a
request failure.
"""

from __future__ import annotations

import asyncio

from helpers.api import ApiHandler

from usr.plugins.device_sync.helpers.auth import PeerEndpoint


class SyncNow(PeerEndpoint, ApiHandler):
    async def process(self, input, request):
        from usr.plugins.device_sync.helpers import runtime

        denial = self.deny(request)
        if denial is not None:
            return denial
        input = input if isinstance(input, dict) else {}
        # sync does blocking urllib work (seconds-to-minutes per peer) —
        # run it off the host's request loop.
        return await asyncio.to_thread(
            runtime.sync_now,
            peer=input.get("peer") or None,
            direction=input.get("direction") or "bidirectional",
        )
