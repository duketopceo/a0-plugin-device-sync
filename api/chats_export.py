"""Export chats as a bounded ZIP pack.

POST /api/plugins/device_sync/chats_export — body: {"ctxids": [...]?}
Returns the ZIP as a binary attachment (Response passthrough); manifest
inside carries format/version/counts. All USER chats are exported when
ctxids is omitted — peers pull the full set and dedupe by ctxid.
"""

from __future__ import annotations

from helpers.api import ApiHandler


class ChatsExport(ApiHandler):
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
        ctxids = input.get("ctxids") if isinstance(input, dict) else None
        if ctxids is not None and not isinstance(ctxids, list):
            return {"ok": False, "error": "ctxids must be a list"}
        try:
            zip_bytes, manifest = packs.build_chats_zip_bytes(ctxids)
            path = packs.write_temp_file(zip_bytes, ".zip")
            return packs.send_temp_file(
                path,
                download_name=f"chats-{manifest['chat_count']}.zip",
                mimetype="application/zip",
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
