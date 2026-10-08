"""Export chats as a bounded ZIP pack.

POST /api/plugins/device_sync/chats_export — body: {"ctxids": [...]?}
Returns the ZIP as a binary attachment (Response passthrough); manifest
inside carries format/version/counts. The archive streams straight to a
temp file — the full ZIP is never resident in memory. All USER chats are
exported when ctxids is omitted — peers pull the full set and dedupe by
ctxid.
"""

from __future__ import annotations

import os
import tempfile

from helpers.api import ApiHandler

from usr.plugins.device_sync.helpers.auth import PeerEndpoint


class ChatsExport(PeerEndpoint, ApiHandler):
    async def process(self, input, request):
        from usr.plugins.device_sync.helpers import auth, packs

        denial = self.deny(request)
        if denial is not None:
            return denial
        ctxids = input.get("ctxids") if isinstance(input, dict) else None
        if ctxids is not None and not isinstance(ctxids, list):
            return {"ok": False, "error": "ctxids must be a list"}
        path = None
        try:
            fd, path = tempfile.mkstemp(suffix=".zip")
            os.close(fd)
            manifest = packs.build_chats_zip_file(path, ctxids)
            return packs.send_temp_file(
                path,
                download_name=f"chats-{manifest['chat_count']}.zip",
                mimetype="application/zip",
            )
        except Exception as e:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            return {"ok": False, "error": auth.safe_error(e)}
