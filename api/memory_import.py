"""Import a memory pack (NDJSON atoms) — idempotent by atom id.

POST /api/plugins/device_sync/memory_import — raw NDJSON body
(application/x-ndjson). Malformed lines reject the whole pack
(ValueError -> ok:false); well-formed atoms already known locally are
skipped, so re-delivery is safe.
"""

from __future__ import annotations

import asyncio

from helpers.api import ApiHandler

from usr.plugins.device_sync.helpers.auth import PeerEndpoint


class MemoryImport(PeerEndpoint, ApiHandler):
    async def process(self, input, request):
        from usr.plugins.device_sync.helpers import auth, packs, runtime

        denial = self.deny(request)
        if denial is not None:
            return denial
        sync = runtime.engine()
        if sync is None:
            return {"ok": False, "error": "device-sync inactive"}
        try:
            raw = auth.read_body_capped(request, packs.MAX_PACK_UPLOAD_BYTES)
            imported = await asyncio.to_thread(sync.import_memory_pack, raw)
            return {"ok": True, "imported": imported}
        except Exception as e:
            return {"ok": False, "error": auth.safe_error(e)}
