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
        self.bodies: list[bytes] = []

    def bind(self, sync):
        def fake(url, *, method="GET", data=None, headers=None, timeout=None):
            self.calls.append((method, url))
            self.headers_seen.append(headers or {})
            self.bodies.append(data or b"")
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
    # the settings pack posted on the wire is scrubbed — verify the body,
    # not just that a call happened
    post = json.loads(net.bodies[0])
    assert post["pack"]["settings"]["api_keys"] == {}
    assert post["pack"]["settings"]["chat_model_name"] == "m"


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


def test_push_import_rejection_is_an_error(tmp_path):
    """{ok:false} at HTTP 200 from an import endpoint must not read as
    success — handlers report domain errors in-band."""
    sync = _mk_sync(tmp_path, backend=DictMemoryBackend([{"id": "a1"}]))
    net = FakeNet().bind(sync)
    net.routes["settings_import"] = (200, b'{"ok": false, "error": "bad pack"}')
    net.routes["chats_import"] = (200, b'{"ok": false, "error": "zip rejected"}')
    net.routes["memory_import"] = (200, b'{"ok": false, "error": "device-sync inactive"}')

    r = sync.sync_to_peer(_peer())
    assert r.ok is False
    assert r.settings is False and r.chats is False and r.memory is False
    assert any("bad pack" in e for e in r.errors)
    assert any("device-sync inactive" in e for e in r.errors)


def test_bidirectional_never_writes_settings_either_side(tmp_path, settings_state):
    """Settings are diff-only in bidirectional: no settings_import POST to
    the peer (would clobber it before the diff sees it) and no local
    apply. Pure push/pull remain the merge directions."""
    settings_state.update({"chat_model_name": "local-a"})
    sync = _mk_sync(tmp_path)
    net = FakeNet().bind(sync)
    remote_pack = {
        "format": "khan-settings",
        "version": 1,
        "settings": {"chat_model_name": "remote-b"},
    }
    net.routes["settings_export"] = (
        200, json.dumps({"pack": remote_pack}).encode())
    net.routes["chats_import"] = (200, b'{"ok": true, "ctxids": []}')
    net.routes["memory_import"] = (200, b'{"ok": true, "imported": 0}')
    net.routes["chats_export"] = (404, b"")
    net.routes["memory_export"] = (404, b"")

    r = sync.bidirectional_sync(_peer())
    assert not any("settings_import" in u for _m, u in net.calls)
    assert settings_state["chat_model_name"] == "local-a"  # not applied
    assert [c["key"] for c in r.settings_conflicts] == ["chat_model_name"]


def test_bidirectional_no_false_conflicts_on_failed_fetch(tmp_path, settings_state):
    """A failed settings fetch is NOT "every key diverged" — the diff is
    gated on pull success."""
    settings_state.update({"chat_model_name": "local-a", "other_pref": "x"})
    sync = _mk_sync(tmp_path)
    net = FakeNet().bind(sync)
    net.routes["settings_export"] = (500, b"boom")
    net.routes["chats_import"] = (200, b'{"ok": true}')
    net.routes["chats_export"] = (404, b"")
    net.routes["memory_export"] = (404, b"")

    r = sync.bidirectional_sync(_peer())
    assert r.settings_conflicts == []
    assert any("settings" in e for e in r.errors)


def test_pull_zero_chat_pack_is_success(tmp_path, settings_state):
    """A peer with no chats produces a valid chat_count:0 pack — import is
    a no-op, not an error."""
    settings_state.update({"chat_model_name": "m"})
    sync = _mk_sync(tmp_path)
    net = FakeNet().bind(sync)
    net.routes["settings_export"] = _settings_pack_ok()
    zip_bytes, _manifest = packs.build_chats_zip_bytes(ctxids=[])
    net.routes["chats_export"] = (200, zip_bytes)
    net.routes["memory_export"] = (404, b"")

    r = sync.sync_from_peer(_peer())
    assert r.chats is True and r.chats_imported == 0
    assert not any("chats" in e for e in r.errors)


def test_null_backend_skips_memory_pull(tmp_path, settings_state):
    """No memory store -> don't even fetch the pack (would report success
    while dropping every atom on the floor)."""
    from usr.plugins.device_sync.helpers.memory_backend import NullMemoryBackend

    settings_state.update({"chat_model_name": "m"})
    sync = _mk_sync(tmp_path, backend=NullMemoryBackend())
    net = FakeNet().bind(sync)
    net.routes["settings_export"] = _settings_pack_ok()
    net.routes["chats_export"] = (404, b"")

    r = sync.sync_from_peer(_peer())
    assert r.memory is False
    assert not any("memory_export" in u for _m, u in net.calls)


def test_discovery_unions_tailscale_and_peers_file(tmp_path):
    """Manual peers coexist with tailscale peers — the file is a union
    source, not a tailscale-failure fallback."""
    pf = tmp_path / "peers.json"
    pf.write_text(json.dumps([{"name": "manual", "host": "10.9.9.9", "port": 80}]))
    sync = _mk_sync(tmp_path, peers_file=pf)
    sync._run_tailscale_status = lambda: {
        "Self": {"HostName": "self"},
        "Peer": {"p": {"HostName": "ts-peer", "Online": True, "TailscaleIPs": ["1.1.1.1"]}},
    }
    sync._is_peer = lambda p: True
    assert sorted(p.name for p in sync.discover_peers()) == ["manual", "ts-peer"]


def test_peers_file_bad_port_skipped_not_fatal(tmp_path):
    """A malformed port must not wedge all discovery."""
    pf = tmp_path / "peers.json"
    pf.write_text(json.dumps([
        {"name": "bad", "host": "10.9.9.9", "port": 70000},
        {"name": "good", "host": "10.9.9.8", "port": 80},
    ]))
    sync = _mk_sync(tmp_path, peers_file=pf)
    sync._run_tailscale_status = lambda: {}
    sync._is_peer = lambda p: True
    assert [p.name for p in sync.discover_peers()] == ["good"]


def test_is_peer_requires_forbidden_signature(tmp_path):
    """Identity probe: unauthenticated POST must yield the plugin's 403
    'forbidden' — a bare open port is NOT a peer (token-theft guard)."""
    import urllib.error

    sync = _mk_sync(tmp_path)
    peer = _peer()

    class RealPluginPeer:
        def open(self, req, timeout=None):
            assert not dict(req.header_items()).get("authorization")
            raise urllib.error.HTTPError(
                req.full_url, 403, "Forbidden", {},
                io.BytesIO(b'{"ok": false, "error": "forbidden"}'),
            )

    sync._opener = RealPluginPeer()
    assert sync._is_peer(peer) is True

    class RandomService:
        def open(self, req, timeout=None):
            return io.BytesIO(b"hello")

    sync._opener = RandomService()
    assert sync._is_peer(peer) is False

    class NotA0:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 404, "Not Found", {}, io.BytesIO(b"")
            )

    sync._opener = NotA0()
    assert sync._is_peer(peer) is False

    class Dead:
        def open(self, req, timeout=None):
            raise OSError("connection refused")

    sync._opener = Dead()
    assert sync._is_peer(peer) is False


def test_sync_lock_contention_reports_busy(tmp_path):
    """A contended sync reports failure rather than queueing behind the
    in-progress one."""
    sync = _mk_sync(tmp_path)
    sync._sync_lock.acquire()
    try:
        for call in (sync.sync_to_peer, sync.sync_from_peer, sync.bidirectional_sync):
            r = call(_peer())
            assert r.ok is False
            assert any("already running" in e for e in r.errors)
    finally:
        sync._sync_lock.release()


def test_start_stop_lifecycle(tmp_path):
    sync = _mk_sync(tmp_path)
    assert sync.stop() is False  # nothing running
    sync._run_tailscale_status = lambda: {}
    assert sync.start(interval_seconds=60) is True
    assert sync.is_running()
    assert sync.start(60) is False  # already running
    assert sync.stop() is True
    assert not sync.is_running()
    assert sync.stop() is False  # idempotent


def test_auto_sync_loop_iterations(tmp_path):
    sync = _mk_sync(tmp_path)
    rounds: list[int] = []
    sync.discover_peers = lambda force=False: rounds.append(1) or []  # type: ignore
    sync.auto_sync_loop(0, max_iterations=2)  # interval 0 -> no wait between rounds
    assert rounds == [1, 1]


def test_peer_base_url_normalization():
    assert PeerDevice(name="p", host="10.0.0.1", port=80).base_url == "http://10.0.0.1:80"
    assert PeerDevice(name="p", host="10.0.0.1:8080").base_url == "http://10.0.0.1:8080"
    assert PeerDevice(name="p", host="https://box.example:8443/").base_url == "https://box.example:8443"
    assert PeerDevice(name="p", host="[fd7a::1]", port=80).base_url == "http://[fd7a::1]:80"


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
