"""API handler tests — token gate, binary passthrough, round trips.

Every handler is instantiated directly (a0 loads classes from files); the
auth model under test: requires_auth/requires_csrf are OFF because these
are machine-to-machine endpoints gated by the shared sync token.
"""

from __future__ import annotations

import json

import pytest

from usr.plugins.device_sync.helpers import runtime

from conftest import DictMemoryBackend, FakeRequest, run


def _req(token=None, data=b""):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return FakeRequest(headers=headers, data=data)


@pytest.fixture()
def active(cfg):
    runtime.configure(cfg)
    yield
    runtime._reset()


# ------------------------------- auth gate ---------------------------------


def test_endpoints_refuse_without_token(active, anon_request):
    from usr.plugins.device_sync.api import peers, settings_export

    r1 = run(settings_export.SettingsExport().process({}, anon_request))
    assert r1.status == 403
    r2 = run(peers.Peers().process({}, anon_request))
    assert r2.status == 403


def test_endpoints_refuse_when_plugin_disabled(cfg, authed_request):
    runtime.configure({**cfg, "enabled": False})
    from usr.plugins.device_sync.api import settings_export

    r = run(settings_export.SettingsExport().process({}, authed_request))
    assert r.status == 403


def test_x_token_header_alternative(active):
    from usr.plugins.device_sync.api import settings_export

    req = FakeRequest(headers={"X-Device-Sync-Token": "test-token"})
    out = run(settings_export.SettingsExport().process({}, req))
    assert isinstance(out, dict) and out["ok"] is True


def test_handlers_disable_session_auth_and_csrf():
    """M2M contract: token auth replaces session auth; CSRF is meaningless
    without cookies. Every handler declares both off and gates itself."""
    from usr.plugins.device_sync.api import (
        chats_export, chats_import, memory_export, memory_import, peers,
        settings_export, settings_import, sync_now,
    )

    for mod in (
        settings_export, settings_import, chats_export, chats_import,
        memory_export, memory_import, sync_now, peers,
    ):
        for cls in vars(mod).values():
            if isinstance(cls, type) and cls.__module__ == mod.__name__:
                assert cls.requires_auth() is False, cls
                assert cls.requires_csrf() is False, cls
                assert cls.get_methods() == ["POST"]


# ------------------------------ settings -----------------------------------


def test_settings_export_import_round_trip_via_handlers(active, settings_state):
    settings_state.update({"chat_model_name": "m1", "api_keys": {"k": "s"}})
    from usr.plugins.device_sync.api import settings_export, settings_import

    out = run(settings_export.SettingsExport().process({}, _req("test-token")))
    assert out["ok"] is True
    pack = out["pack"]
    assert "s" not in json.dumps(pack["settings"]["api_keys"])

    settings_state.clear()
    settings_state.update({"chat_model_name": "z", "api_keys": {"k": "MINE"}})
    res = run(
        settings_import.SettingsImport().process({"pack": pack}, _req("test-token"))
    )
    assert res == {"ok": True}
    assert settings_state["chat_model_name"] == "m1"
    assert settings_state["api_keys"] == {"k": "MINE"}


def test_settings_import_validates_body(active):
    from usr.plugins.device_sync.api import settings_import

    out = run(settings_import.SettingsImport().process({"pack": "x"}, _req("test-token")))
    assert out["ok"] is False
    out = run(settings_import.SettingsImport().process({}, _req("test-token")))
    assert out["ok"] is False


# -------------------------------- chats ------------------------------------


def test_chats_export_returns_binary_response(active, chats_store):
    from agent import AgentContext

    AgentContext("c1")
    chats_store["c1"] = json.dumps({"id": "c1", "name": "x", "log": {"logs": []}})
    from usr.plugins.device_sync.api import chats_export

    resp = run(chats_export.ChatsExport().process({}, _req("test-token")))
    assert resp.status == 200 and resp.mimetype == "application/zip"
    assert resp.get_data()[:2] == b"PK"  # real zip bytes
    resp.close()


def test_chats_import_accepts_raw_zip(active, chats_store):
    chat = json.dumps({"id": "zz", "name": "n", "log": {"logs": []}})
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(
            {"format": "khan-chats", "version": 1, "chat_count": 1}))
        zf.writestr("chats/zz.json", chat)
    from usr.plugins.device_sync.api import chats_import

    out = run(chats_import.ChatsImport().process({}, _req("test-token", buf.getvalue())))
    assert out["ok"] is True and out["ctxids"] == ["zz"]
    # re-import dedupes
    out2 = run(chats_import.ChatsImport().process({}, _req("test-token", buf.getvalue())))
    assert out2["ok"] is True and out2["ctxids"] == []


def test_chats_import_rejects_garbage(active):
    from usr.plugins.device_sync.api import chats_import

    out = run(chats_import.ChatsImport().process({}, _req("test-token", b"not a zip")))
    assert out["ok"] is False


# -------------------------------- memory -----------------------------------


def test_memory_export_import_via_handlers(active, tmp_path):
    runtime.register_memory_backend("dicts", lambda c: DictMemoryBackend([{"id": "a1"}]))
    runtime.configure({**runtime._cfg.to_dict(), "memory_backend": "dicts",
                       "enabled": True, "sync_token": "test-token"})

    from usr.plugins.device_sync.api import memory_export, memory_import

    resp = run(memory_export.MemoryExport().process({}, _req("test-token")))
    assert resp.status == 200 and resp.mimetype == "application/x-ndjson"
    body = resp.get_data()
    assert b'"a1"' in body

    # import into a fresh backend
    runtime.register_memory_backend("dicts", lambda c: DictMemoryBackend())
    runtime.configure({"enabled": True, "sync_token": "test-token",
                       "memory_backend": "dicts"})
    out = run(memory_import.MemoryImport().process({}, _req("test-token", body)))
    assert out == {"ok": True, "imported": 1}
    out2 = run(memory_import.MemoryImport().process({}, _req("test-token", body)))
    assert out2 == {"ok": True, "imported": 0}


def test_memory_export_empty_when_no_backend(active):
    from usr.plugins.device_sync.api import memory_export

    resp = run(memory_export.MemoryExport().process({}, _req("test-token")))
    assert resp.status == 200 and resp.get_data() == b""


# ------------------------------ peers/now ----------------------------------


def test_peers_endpoint_shape(active):
    from usr.plugins.device_sync.api import peers

    eng = runtime.engine()
    eng._run_tailscale_status = lambda: {}
    out = run(peers.Peers().process({}, _req("test-token")))
    assert out["ok"] is True
    assert "peers" in out and "status" in out
    assert out["status"]["active"] is True


def test_sync_now_endpoint(active):
    from usr.plugins.device_sync.api import sync_now

    eng = runtime.engine()
    eng._run_tailscale_status = lambda: {}
    out = run(sync_now.SyncNow().process({"direction": "push"}, _req("test-token")))
    assert out["ok"] is True and out["results"] == []
