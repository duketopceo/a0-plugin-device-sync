"""Export the secret-free settings pack.

POST /api/plugins/device_sync/settings_export — body: {}
Returns {"ok": true, "pack": {...}} — peers unwrap "pack" and feed it to
their own settings_import.
"""

from __future__ import annotations

from helpers.api import ApiHandler


class SettingsExport(ApiHandler):
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
        try:
            return {"ok": True, "pack": packs.build_settings_pack()}
        except Exception as e:
            return {"ok": False, "error": str(e)}
