"""Import a chats ZIP pack — bounded extraction, ctxid-deduped
(plugin-side: the host's load_json_chats mints new ids).

POST /api/plugins/device_sync/chats_import — raw ZIP body
(application/zip). The archive is validated before any chat is written:
upload cap, entry count, per-entry and total uncompressed sizes, and a
compression-ratio guard. Chat JSONs import through stock
persist_chat.load_json_chats, which dedupes on ctxid — re-importing the
same pack is a no-op. The embedded kurultai NDJSON is written to the
inbox when the pack carries it.
"""

from __future__ import annotations

import asyncio
import os

from helpers.api import ApiHandler

from usr.plugins.device_sync.helpers.auth import PeerEndpoint


class ChatsImport(PeerEndpoint, ApiHandler):
    async def process(self, input, request):
        from usr.plugins.device_sync.helpers import auth, packs

        denial = self.deny(request)
        if denial is not None:
            return denial
        # Reject oversized uploads BEFORE buffering the body — a0 reads
        # request.data for JSON bodies pre-auth, but raw posts arrive here
        # unread; don't materialize a multi-GB body just to refuse it.
        # Content-Length is advisory (absent under chunked, or lying), so
        # the stream itself is bounded.
        try:
            raw = auth.read_body_capped(request, packs.MAX_PACK_UPLOAD_BYTES)
        except ValueError:
            return {"ok": False, "error": "chats pack exceeds the upload cap"}

        def _import():
            _manifest, chat_jsons, ndjson = packs.extract_chats_from_zip(raw)
            ctxids = packs.import_chat_jsons(chat_jsons)
            inbox_name = None
            if ndjson.strip():
                inbox_path = packs.write_kurultai_ndjson(ndjson)
                inbox_name = os.path.basename(inbox_path)  # never echo abs paths
            return ctxids, len(chat_jsons), inbox_name

        try:
            # zip parse + host deserialization is blocking work — keep it
            # off the host's request-handling loop.
            ctxids, count, inbox_name = await asyncio.to_thread(_import)
            return {"ok": True, "ctxids": ctxids, "chats": count, "inbox": inbox_name}
        except Exception as e:
            return {"ok": False, "error": auth.safe_error(e)}
