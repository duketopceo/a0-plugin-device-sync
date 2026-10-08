"""Export the secret-free settings pack.

POST /api/plugins/device_sync/settings_export — body: {}
Returns {"ok": true, "pack": {...}} — peers unwrap "pack" and feed it to
their own settings_import.
"""

from __future__ import annotations

import asyncio

from helpers.api import ApiHandler

from usr.plugins.device_sync.helpers.auth import PeerEndpoint


class SettingsExport(PeerEndpoint, ApiHandler):
    async def process(self, input, request):
        from usr.plugins.device_sync.helpers import auth, packs

        denial = self.deny(request)
        if denial is not None:
            return denial
        try:
            return {"ok": True, "pack": await asyncio.to_thread(packs.build_settings_pack)}
        except Exception as e:
            return {"ok": False, "error": auth.safe_error(e)}
