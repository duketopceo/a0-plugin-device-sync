"""Export the memory pack (NDJSON atoms).

POST /api/plugins/device_sync/memory_export — body: {}
Returns raw ``application/x-ndjson`` bytes (Response passthrough). With
no memory backend configured this box behaves like a peer without memory
support: an empty-but-valid body, so peers stay graceful.
"""

from __future__ import annotations

from helpers.api import ApiHandler, Response

from usr.plugins.device_sync.helpers.auth import PeerEndpoint


class MemoryExport(PeerEndpoint, ApiHandler):
    async def process(self, input, request):
        from usr.plugins.device_sync.helpers import auth, runtime

        denial = self.deny(request)
        if denial is not None:
            return denial
        sync = runtime.engine()
        try:
            body = sync.export_memory_pack() if sync is not None else b""
            return Response(body, status=200, mimetype="application/x-ndjson")
        except Exception as e:
            return auth.json_error(auth.safe_error(e), 500)
