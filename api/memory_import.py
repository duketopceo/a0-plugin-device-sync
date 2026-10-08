"""Import a memory pack (NDJSON atoms) — idempotent by atom id.

POST /api/plugins/device_sync/memory_import — raw NDJSON body
(application/x-ndjson). Malformed lines reject the whole pack
(ValueError -> ok:false); well-formed atoms already known locally are
skipped, so re-delivery is safe.
"""

from __future__ import annotations

from helpers.api import ApiHandler


class MemoryImport(ApiHandler):
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
        from usr.plugins.device_sync.helpers import auth, packs, runtime

        denial = auth.check_peer_request(request)
        if denial is not None:
            return denial
        sync = runtime.engine()
        if sync is None:
            return {"ok": False, "error": "device-sync inactive"}
        raw = request.get_data()
        try:
            packs.assert_upload_size(raw, label="memory pack")
            imported = sync.import_memory_pack(raw)
            return {"ok": True, "imported": imported}
        except Exception as e:
            return {"ok": False, "error": str(e)}
