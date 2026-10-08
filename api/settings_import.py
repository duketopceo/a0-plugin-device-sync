"""Import a settings pack — non-sensitive keys overlay local prefs.

POST /api/plugins/device_sync/settings_import — body: {"pack": {...}}
Secret-bearing keys are dropped before the overlay (locally-owned secrets
always win); imported values land via settings.set_settings so the normal
a0 persistence/notification path runs.
"""

from __future__ import annotations

from helpers.api import ApiHandler


class SettingsImport(ApiHandler):
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
        from usr.plugins.device_sync.helpers import auth, packs

        denial = auth.check_peer_request(request)
        if denial is not None:
            return denial
        if not isinstance(input, dict) or not isinstance(input.get("pack"), dict):
            return {"ok": False, "error": "object body with 'pack' required"}
        try:
            packs.import_settings_pack(input["pack"])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
