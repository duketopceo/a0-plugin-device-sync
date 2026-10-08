"""Import a settings pack — non-sensitive keys overlay local prefs.

POST /api/plugins/device_sync/settings_import — body: {"pack": {...}}
Secret-bearing keys are dropped before the overlay (locally-owned secrets
always win); imported values land via settings.set_settings so the normal
a0 persistence/notification path runs.
"""

from __future__ import annotations

from helpers.api import ApiHandler

from usr.plugins.device_sync.helpers.auth import PeerEndpoint


class SettingsImport(PeerEndpoint, ApiHandler):
    async def process(self, input, request):
        from usr.plugins.device_sync.helpers import auth, packs

        denial = self.deny(request)
        if denial is not None:
            return denial
        if not isinstance(input, dict) or not isinstance(input.get("pack"), dict):
            return {"ok": False, "error": "object body with 'pack' required"}
        try:
            packs.import_settings_pack(input["pack"])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": auth.safe_error(e)}
