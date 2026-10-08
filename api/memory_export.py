"""Export the memory pack (NDJSON atoms).

POST /api/plugins/device_sync/memory_export — body: {}
Returns raw ``application/x-ndjson`` bytes (Response passthrough). With
no memory backend configured this box behaves like a peer without memory
support: an empty-but-valid body, so peers stay graceful.
"""

from __future__ import annotations

import json

from helpers.api import ApiHandler, Response


class MemoryExport(ApiHandler):
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
        sync = runtime.engine()
        try:
            body = sync.export_memory_pack() if sync is not None else b""
            return Response(body, status=200, mimetype="application/x-ndjson")
        except Exception as e:
            return Response(
                json.dumps({"ok": False, "error": str(e)}),
                status=500,
                mimetype="application/json",
            )
