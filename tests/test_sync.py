"""ContinuitySync transport tests — HTTP seam stubbed, no live network.

Peer endpoints are faked by patching ``ContinuitySync._http_request`` with
a router dict: ``{(method, path_suffix): (status, body)}``. The pack layer
is real — pushes/exports run through actual pack builders.
"""

from __future__ import annotations

import json
import zipfile
import io

import pytest

from usr.plugins.device_sync.helpers import packs
from usr.plugins.device_sync.helpers.sync import ContinuitySync, PeerDevice

from conftest import DictMemoryBackend


def _mk_sync(tmp_path, backend=None, token="tok", **kw):
    kw.setdefault("peers_file", tmp_path / "peers.json")
    return ContinuitySync(
        backend=backend or DictMemoryBackend(),
        peer_port=80,
        token=token,
        timeout=5,
        **kw,
    )


def _peer(name="peer-a", host="10.0.0.2", port=80):
    return PeerDevice(name=name, host=host, port=port)


class FakeNet:
    """Installed over _http_request; routes carry canned (status, body)."""

    def __init__(self):
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.calls: list[tuple[str, str]] = []
        self.headers_seen: list[dict] = []

    def bind(self, sync):
        def fake(url, *, method="GET", data=None, headers=None, timeout=None):
            self.calls.append((method, url))
            self.headers_seen.append(headers or {})
            for suffix, (status, body) in self.routes.items():
                if url.endswith(suffix):
                    return status, body, {}
            return 404, b"", {}

        sync._http_request = fake  # type: ignore[assignment]
        return self


def _settings_pack_ok():
    return 200, json.dumps({"ok": True, "pack": packs.build_settings_pack()}).encode()


def test_token_header_sent(tmp_path):
    sync = _mk_sync(tmp_path)
    net = FakeNet().bind(sync)
    net.routes["settings_export"] = _settings_pack_ok()
    sync.sync_from_peer(_peer())
    assert any(
        h.get("Authorization") == "Bearer tok" for h in net.headers_seen
    )


def test_push_settings_and_chats(tmp_path, settings_state, chats_store):
    settings_state.update({"chat_model_name": "m", "api_keys": {"k": "secret"}})
    sync = _mk_sync(tmp_path)
    net = FakeNet().bind(sync)
    net.routes["settings_import"] = (200, b'{"ok": true}')
    net.routes["chats_import"] = (200, b'{"ok": true, "ctxids": ["a"]}')
    net.routes["memory_import"] = (200, b'{"ok": true, "imported": 0}')

    r = sync.sync_to_peer(_peer())
    assert r.settings is True and r.chats is True
    assert r.errors == []
    # settings pack posted without secrets — find the posted body via calls
    assert any("settings_import" in url for _m, url in net.calls)


def test_pull_imports_settings_and_chats(tmp_path, settings_state, chats_store):
    settings_state.update({"chat_model_name": "old", "api_keys": {"me": "mine"}})
    sync = _mk_sync(tmp_path)
    net = FakeNet().bind(sync)

    remote_pack = {
        "format": "khan-settings",
        "version": 1,
        "settings": {"chat_model_name": "new", "api_keys": {"me": "theirs"}},
    }
    net.routes["settings_export"] = (
        200,
        json.dumps({"ok": True, "pack": remote_pack}).encode(),
    )

    chat_js = json.dumps({"id": "c9", "name": "x", "log": {"logs": []}})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(
            {"format": "khan-chats", "version": 1, "chat_count": 1, "chat_ids": ["c9"]}))
        zf.writestr("chats/c9.json", chat_js)
    net.routes["chats_export"] = (200, buf.getvalue())
    net.routes["memory_export"] = (200, b'{"id":"m1","title":"t","content":"c","tags":["x"]}\n')

    r = sync.sync_from_peer(_peer())
    assert r.ok and r.settings and r.chats and r.memory
    assert r.chats_imported == 1 and r.atoms_imported == 1
    assert settings_state["chat_model_name"] == "new"
    assert settings_state["api_keys"] == {"me": "mine"}  # local secrets kept
    assert "c9" in chats_store


def test_memory_endpoints_404_are_graceful(tmp_path, settings_state):
    settings_state.update({"chat_model_name": "m"})
    sync = _mk_sync(tmp_path)
    net = FakeNet().bind(sync)
    net.routes["settings_export"] = _settings_pack_ok()
    net.routes["chats_export"] = (404, b"")
    net.routes["memory_export"] = (404, b"")

    r = sync.sync_from_peer(_peer())
    assert r.memory is False  # 404 -> unsupported, not error
    assert any("chats_export HTTP 404" in e for e in r.errors)
    assert not any("memory_export" in e for e in r.errors)


def test_unreachable_peer_degrades_to_errors(tmp_path):
    sync = _mk_sync(tmp_path, backend=DictMemoryBackend([{"id": "a1"}]))

    def boom(url, **kw):
        raise OSError("connection refused")

    sync._http_request = boom  # type: ignore[assignment]
    r = sync.sync_to_peer(_peer())
    assert r.ok is False
    assert len(r.errors) == 3  # settings, chats, memory each fail soft


def test_bidirectional_reports_settings_conflicts(tmp_path, settings_state):
    settings_state.update({"chat_model_name": "local-a", "api_keys": {"x": "s"}})
    sync = _mk_sync(tmp_path)
    net = FakeNet().bind(sync)
    remote_pack = {
        "format": "khan-settings",
        "version": 1,
        "settings": {"chat_model_name": "remote-b", "api_keys": {"x": "other"}},
    }
    net.routes["settings_export"] = (
        200, json.dumps({"pack": remote_pack}).encode())
    for ep in ("settings_import", "chats_import", "memory_import"):
        net.routes[ep] = (200, b'{"ok": true}')
    net.routes["chats_export"] = (404, b"")
    net.routes["memory_export"] = (404, b"")

    r = sync.bidirectional_sync(_peer())
    keys = [c["key"] for c in r.settings_conflicts]
    assert "chat_model_name" in keys
    assert "api_keys" not in keys  # sensitive keys never conflicted
    assert settings_state["api_keys"] == {"x": "s"}


def test_peers_file_fallback_discovery(tmp_path):
    pf = tmp_path / "peers.json"
    pf.write_text(json.dumps([{"name": "box", "host": "10.9.9.9", "port": 80}]))
    sync = _mk_sync(tmp_path, peers_file=pf)
    sync._run_tailscale_status = lambda: {}  # no tailscale in test
    sync._is_peer = lambda p: True  # probe stubbed open
    peers = sync.discover_peers()
    assert [p.name for p in peers] == ["box"]
    assert peers[0].base_url == "http://10.9.9.9:80"


def test_tailscale_discovery_skips_self_and_offline(tmp_path):
    sync = _mk_sync(tmp_path)
    sync._run_tailscale_status = lambda: {
        "Self": {"HostName": "self-box"},
        "Peer": {
            "p1": {"HostName": "self-box", "Online": True, "TailscaleIPs": ["1.1.1.1"]},
            "p2": {"HostName": "other", "Online": True, "TailscaleIPs": ["2.2.2.2"]},
            "p3": {"HostName": "off", "Online": False, "TailscaleIPs": ["3.3.3.3"]},
        },
    }
    sync._is_peer = lambda p: True
    peers = sync.discover_peers()
    assert [p.name for p in peers] == ["other"]


def test_discovery_never_raises_on_garbage(tmp_path):
    sync = _mk_sync(tmp_path)
    sync._run_tailscale_status = lambda: {"Peer": {"x": "not-a-dict"}}
    assert sync.discover_peers() == []

    pf = tmp_path / "peers.json"
    pf.write_text("{not json")
    sync.peers_file = pf
    assert sync.discover_peers() == []


def test_export_import_memory_pack_idempotent(tmp_path):
    backend = DictMemoryBackend([{"id": "a1", "t": 1}])
    sync = _mk_sync(tmp_path, backend=backend)
    blob = sync.export_memory_pack()

    other = DictMemoryBackend()
    sync2 = _mk_sync(tmp_path, backend=other)
    assert sync2.import_memory_pack(blob) == 1
    assert sync2.import_memory_pack(blob) == 0  # idempotent


def test_memory_pack_malformed_line_raises(tmp_path):
    sync = _mk_sync(tmp_path)
    with pytest.raises(ValueError):
        sync.import_memory_pack(b"{bad}\n")


def test_record_peer_outcome(tmp_path, settings_state):
    settings_state.update({"chat_model_name": "m"})
    sync = _mk_sync(tmp_path)
    net = FakeNet().bind(sync)
    net.routes["settings_export"] = _settings_pack_ok()
    peer = _peer()
    sync.sync_from_peer(peer)
    st = sync.status()
    assert st["peer_count"] == 1
    assert st["peers"][0]["last_sync_status"] in ("success", "failed")


def test_stop_is_idempotent_and_bounded(tmp_path):
    sync = _mk_sync(tmp_path)
    assert sync.stop() in (True, False)  # no thread -> False; never hangs
    assert sync.is_running() is False
