"""Pack-layer tests: settings scrub/import, NDJSON, bounded zip handling.

The acceptance criterion — "settings + memory round-trip between two stock
a0 instances" — is covered here by exporting from one stub state and
importing into a swapped stub state (each stub = one instance)."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from usr.plugins.device_sync.helpers import packs
from usr.plugins.device_sync.helpers.packs import (
    MAX_PACK_UPLOAD_BYTES,
    SENSITIVE_SETTINGS_KEYS,
)


# ------------------------------- settings ----------------------------------


def _seed_settings(state):
    state.clear()
    state.update(
        {
            "chat_model_provider": "openrouter",
            "chat_model_name": "model-a",
            "api_keys": {"openrouter": "sk-secret-1"},
            "auth_password": "hunter2",
            "secrets": {"token": "abc"},
            "rfc_password": "pw",
        }
    )
    return state


def test_build_settings_pack_scrubs_secrets(settings_state):
    _seed_settings(settings_state)
    pack = packs.build_settings_pack()

    assert pack["format"] == "khan-settings"
    assert pack["version"] == packs.SETTINGS_PACK_VERSION
    s = pack["settings"]
    assert s["chat_model_provider"] == "openrouter"
    for key in SENSITIVE_SETTINGS_KEYS:
        assert s[key] == ({} if key == "api_keys" else "")
    blob = json.dumps(pack)
    assert "sk-secret-1" not in blob
    assert "hunter2" not in blob


def test_import_settings_pack_overlays_and_preserves_secrets(settings_state):
    _seed_settings(settings_state)
    pack = {
        "format": "khan-settings",
        "version": 1,
        "settings": {
            "chat_model_name": "model-b",  # allowed -> overlays
            "api_keys": {"openrouter": "REMOTE-KEY"},  # secret -> dropped
            "auth_password": "REMOTE-PW",  # secret -> dropped
            "secrets": {"token": "REMOTE"},
        },
    }
    packs.import_settings_pack(pack)

    assert settings_state["chat_model_name"] == "model-b"
    # locally-owned secrets survive untouched
    assert settings_state["api_keys"] == {"openrouter": "sk-secret-1"}
    assert settings_state["auth_password"] == "hunter2"
    assert settings_state["secrets"] == {"token": "abc"}


def test_settings_pack_round_trip_between_instances(settings_state):
    """Instance A exports -> instance B imports; B's secrets survive, its
    prefs take A's."""
    _seed_settings(settings_state)
    pack_a = packs.build_settings_pack()

    # swap to instance B state
    settings_state.clear()
    settings_state.update(
        {
            "chat_model_name": "model-z",
            "api_keys": {"anthropic": "B-KEY"},
            "auth_password": "B-pw",
        }
    )
    packs.import_settings_pack(pack_a)

    assert settings_state["chat_model_name"] == "model-a"  # A's pref won
    assert settings_state["api_keys"] == {"anthropic": "B-KEY"}  # B's secret kept
    assert settings_state["auth_password"] == "B-pw"


def test_import_settings_pack_rejects_bad_shape(settings_state):
    for bad in (
        "nope",
        {"format": "other"},
        {"format": "khan-settings"},  # no version
        {"format": "khan-settings", "version": 99},
        {"format": "khan-settings", "version": 1, "settings": "x"},
    ):
        with pytest.raises(ValueError):
            packs.import_settings_pack(bad)


# -------------------------------- NDJSON -----------------------------------


def test_chat_to_atoms_and_ndjson_round():
    chat = {
        "id": "c1",
        "name": "Research run",
        "log": {
            "logs": [
                {"id": "m1", "type": "user", "heading": "Q", "content": "hello"},
                {"type": "agent", "content": "answer"},
                {"type": "tool", "heading": "", "content": ""},  # skipped
            ]
        },
    }
    atoms = packs.chat_json_to_kurultai_atoms(chat)
    assert [a["id"] for a in atoms] == ["m1", "khan-chat-c1-log-1"]
    assert all(a["chat_id"] == "c1" for a in atoms)

    ndjson = packs.build_chats_ndjson([json.dumps(chat)])
    assert packs.validate_kurultai_ndjson(ndjson) == 2


def test_chat_to_atoms_falls_back_to_whole_chat():
    atoms = packs.chat_json_to_kurultai_atoms({"id": "c9", "log": {"logs": []}})
    assert len(atoms) == 1
    assert atoms[0]["id"] == "khan-chat-c9"


def test_validate_ndjson_rejects_bad_lines():
    with pytest.raises(ValueError, match="line 1"):
        packs.validate_kurultai_ndjson("{not json}")
    with pytest.raises(ValueError, match="missing required field"):
        packs.validate_kurultai_ndjson(json.dumps({"id": "x"}))
    with pytest.raises(ValueError, match="tags"):
        packs.validate_kurultai_ndjson(
            json.dumps({"id": "x", "title": "t", "content": "c", "tags": []})
        )
    with pytest.raises(ValueError, match="No kurultai atoms"):
        packs.validate_kurultai_ndjson("\n\n")


def test_write_kurultai_ndjson_atomic_and_unique(files_root):
    p1 = packs.write_kurultai_ndjson(
        json.dumps({"id": "a", "title": "t", "content": "c", "tags": ["x"]})
    )
    p2 = packs.write_kurultai_ndjson(
        json.dumps({"id": "b", "title": "t", "content": "c", "tags": ["x"]})
    )
    assert p1 != p2 and p1.endswith(".ndjson")
    assert "kurultai-inbox" in p1


# ------------------------------ chats zip ----------------------------------


def _mk_chat(ctxid, name=""):
    return json.dumps(
        {
            "id": ctxid,
            "name": name or f"Chat {ctxid}",
            "log": {"logs": [{"id": f"{ctxid}-m", "type": "user", "content": "hi"}]},
        }
    )


def test_chats_zip_round_trip(chats_store):
    from agent import AgentContext

    AgentContext("c1")
    AgentContext("c2")
    AgentContext("bg", "background")  # excluded from export
    chats_store["c1"] = _mk_chat("c1", "One")
    chats_store["c2"] = _mk_chat("c2")
    chats_store["bg"] = _mk_chat("bg")

    zip_bytes, manifest = packs.build_chats_zip_bytes()
    assert manifest["format"] == "khan-chats"
    assert manifest["chat_count"] == 2
    assert sorted(manifest["chat_ids"]) == ["c1", "c2"]

    # importing into a fresh instance: both chats land; re-import dedupes
    chats_store.clear()
    _m, chat_jsons, ndjson = packs.extract_chats_from_zip(zip_bytes)
    assert len(chat_jsons) == 2
    first = packs.import_chat_jsons(chat_jsons)
    second = packs.import_chat_jsons(chat_jsons)
    assert sorted(first) == ["c1", "c2"]
    assert second == []
    assert ndjson.strip()


def test_chats_zip_ctxids_filter(chats_store):
    from agent import AgentContext

    AgentContext("c1")
    AgentContext("c2")
    chats_store["c1"] = _mk_chat("c1")
    chats_store["c2"] = _mk_chat("c2")
    _b, manifest = packs.build_chats_zip_bytes(["c1"])
    assert manifest["chat_ids"] == ["c1"]


def _zip_of(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_extract_rejects_missing_manifest():
    bad = _zip_of({"chats/x.json": b"{}"})
    with pytest.raises(ValueError, match="manifest"):
        packs.extract_chats_from_zip(bad)


def test_extract_rejects_wrong_format_and_count_mismatch():
    bad = _zip_of(
        {"manifest.json": json.dumps({"format": "other", "version": 1}).encode()}
    )
    with pytest.raises(ValueError, match="format"):
        packs.extract_chats_from_zip(bad)

    bad2 = _zip_of(
        {
            "manifest.json": json.dumps(
                {"format": "khan-chats", "version": 1, "chat_count": 5}
            ).encode(),
            "chats/a.json": _mk_chat("a").encode(),
        }
    )
    with pytest.raises(ValueError, match="chat_count"):
        packs.extract_chats_from_zip(bad2)


def test_extract_zip_bomb_guards():
    # too many entries
    many = _zip_of({f"f{i}.txt": b"x" for i in range(5001)})
    with pytest.raises(ValueError, match="entries"):
        packs.extract_chats_from_zip(many)

    # oversized single entry (sparse > per-entry cap)
    big = _zip_of(
        {
            "manifest.json": json.dumps(
                {"format": "khan-chats", "version": 1, "chat_count": 0}
            ).encode(),
            "chats/big.json": b"x" * (21 * 1024 * 1024),
        }
    )
    with pytest.raises(ValueError, match="uncompressed"):
        packs.extract_chats_from_zip(big)

    # compression ratio: ~1000:1 on 2MB of zeros
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(
            {"format": "khan-chats", "version": 1, "chat_count": 0}))
        zf.writestr("chats/bomb.json", b"\x00" * (2 * 1024 * 1024))
    with pytest.raises(ValueError, match="ratio"):
        packs.extract_chats_from_zip(buf.getvalue())


def test_extract_traversal_names_safe(chats_store):
    """Traversal-shaped member names never reach disk — entries are read
    to memory only; odd names under chats/ parse-or-fail as chat JSON."""
    z = _zip_of(
        {
            "manifest.json": json.dumps(
                {"format": "khan-chats", "version": 1, "chat_count": 1}
            ).encode(),
            "chats/../evil.json": _mk_chat("evil").encode(),
        }
    )
    _m, chat_jsons, _n = packs.extract_chats_from_zip(z)
    # the odd name IS under chats/ per string prefix — it imports as a
    # chat keyed by its embedded id, not by path: no path traversal exists.
    packs.import_chat_jsons(chat_jsons)
    assert "evil" in chats_store


def test_upload_size_cap():
    with pytest.raises(ValueError, match="limit"):
        packs.assert_upload_size(b"x" * (MAX_PACK_UPLOAD_BYTES + 1))


# ----------------------------- memory pack ---------------------------------


def test_memory_pack_round_trip_and_ids():
    atoms = [
        {"id": "a2", "title": "t", "content": "c", "tags": ["x"], "extra": 1},
        {"id": "a1", "title": "t", "content": "c", "tags": ["x"]},
        {"title": "no id -> dropped"},
    ]
    blob = packs.build_memory_pack(atoms)
    out = list(packs.iter_memory_pack(blob))
    assert [a["id"] for a in out] == ["a1", "a2"]  # canonical: sorted by id
    assert out[1]["extra"] == 1

    # same atom SET serializes identically regardless of input order
    assert packs.build_memory_pack(list(reversed(atoms))) == blob


def test_iter_memory_pack_rejects_malformed():
    with pytest.raises(ValueError, match="line 2"):
        list(packs.iter_memory_pack(b'{"id":"ok"}\n{bad}\n'))


def test_send_temp_file_cleans_up():
    p = packs.write_temp_file(b"zip", ".zip")
    resp = packs.send_temp_file(p, download_name="x.zip", mimetype="application/zip")
    assert resp.get_data() == b"zip"
    resp.close()
    import os

    assert not os.path.exists(p)
