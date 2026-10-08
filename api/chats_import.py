"""Import a chats ZIP pack — bounded extraction, ctxid-deduped.

POST /api/plugins/device_sync/chats_import — raw ZIP body
(application/zip). The archive is validated before any chat is written:
upload cap, entry count, per-entry and total uncompressed sizes, and a
compression-ratio guard. Chat JSONs import through stock
persist_chat.load_json_chats, which dedupes on ctxid — re-importing the
same pack is a no-op. The embedded kurultai NDJSON is written to the
inbox when the pack carries it.
"""

from __future__ import annotations

import os

from helpers.api import ApiHandler

from usr.plugins.device_sync.helpers.auth import PeerEndpoint


class ChatsImport(PeerEndpoint, ApiHandler):
    async def process(self, input, request):
        from usr.plugins.device_sync.helpers import auth, packs

        denial = self.deny(request)
        if denial is not None:
            return denial
        raw = request.get_data()
        try:
            _manifest, chat_jsons, ndjson = packs.extract_chats_from_zip(raw)
            ctxids = packs.import_chat_jsons(chat_jsons)
            inbox_name = None
            if ndjson.strip():
                inbox_path = packs.write_kurultai_ndjson(ndjson)
                inbox_name = os.path.basename(inbox_path)  # never echo abs paths
            return {
                "ok": True,
                "ctxids": ctxids,
                "chats": len(chat_jsons),
                "inbox": inbox_name,
            }
        except Exception as e:
            return {"ok": False, "error": auth.safe_error(e)}
