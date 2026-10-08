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

from helpers.api import ApiHandler


class ChatsImport(ApiHandler):
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
        raw = request.get_data()
        try:
            manifest, chat_jsons, ndjson = packs.extract_chats_from_zip(raw)
            ctxids = packs.import_chat_jsons(chat_jsons)
            inbox_path = None
            if ndjson.strip():
                inbox_path = packs.write_kurultai_ndjson(ndjson)
            return {
                "ok": True,
                "ctxids": ctxids,
                "chats": len(chat_jsons),
                "inbox": inbox_path,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}
