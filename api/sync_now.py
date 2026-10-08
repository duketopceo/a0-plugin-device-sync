"""Manual sync trigger.

POST /api/plugins/device_sync/sync_now — body:
  {"peer": "<name>"?, "direction": "push"|"pull"|"bidirectional"?}
Omitting peer syncs every discovered peer. Returns per-peer results —
individual peer failures degrade to entries in results[].errors, not a
request failure.
"""

from __future__ import annotations

from helpers.api import ApiHandler


class SyncNow(ApiHandler):
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
        input = input if isinstance(input, dict) else {}
        return runtime.sync_now(
            peer=input.get("peer") or None,
            direction=input.get("direction") or "bidirectional",
        )
