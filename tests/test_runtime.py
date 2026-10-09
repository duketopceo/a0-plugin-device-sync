"""Runtime facade: configure/activate/token gate, sync_now, status, stop."""

from __future__ import annotations

import json

from usr.plugins.device_sync.helpers import runtime
from usr.plugins.device_sync.helpers.sync import PeerDevice

from conftest import DictMemoryBackend, FakeRequest


def test_disabled_by_default():
    runtime.configure({})
    assert runtime.is_active() is False
    assert runtime.engine() is None


def test_enabled_without_token_configures_but_stays_inactive(cfg):
    cfg["sync_token"] = ""
    runtime.configure(cfg)
    assert runtime.engine() is not None
    assert runtime.is_active() is False  # no token = endpoints all refuse
    assert runtime.token_ok("anything") is False


def test_enabled_with_token_is_active(cfg):
    runtime.configure(cfg)
    assert runtime.is_active() is True
    assert runtime.token_ok("test-token") is True
    assert runtime.token_ok("wrong") is False
    assert runtime.token_ok("") is False


def test_configure_reconfigure_replaces_engine(cfg):
    a = runtime.configure(cfg)
    b = runtime.configure(cfg)
    assert a is not b
    assert runtime.engine() is b


def test_status_never_echoes_token(cfg):
    runtime.configure(cfg)
    blob = json.dumps(runtime.status())
    assert "test-token" not in blob
    assert '"token_set": true' in blob


def test_sync_now_inactive(cfg):
    runtime.configure({**cfg, "enabled": False})
    out = runtime.sync_now()
    assert out["ok"] is False and "disabled" in out["error"]


def test_sync_now_bad_direction(cfg):
    runtime.configure(cfg)
    out = runtime.sync_now(direction="sideways")
    assert out["ok"] is False


def test_sync_now_no_peers_ok(cfg, tmp_path):
    cfg["peers_file"] = str(tmp_path / "none.json")
    runtime.configure(cfg)
    eng = runtime.engine()
    eng._run_tailscale_status = lambda: {}
    out = runtime.sync_now()
    assert out["ok"] is True and out["results"] == []


def test_sync_now_named_peer(cfg):
    runtime.configure(cfg)
    eng = runtime.engine()
    peer = PeerDevice(name="box", host="10.1.1.1", port=80)
    eng._peers["box"] = peer
    calls = []
    eng.bidirectional_sync = lambda p, **kw: calls.append(p.name) or _r()

    def _r():
        from usr.plugins.device_sync.helpers.sync import SyncResult

        return SyncResult(peer="box", direction="bidirectional")

    out = runtime.sync_now(peer="box")
    assert out["ok"] is True and calls == ["box"]

    out2 = runtime.sync_now(peer="ghost")
    assert out2["ok"] is False and "not found" in out2["error"]


def test_register_memory_backend_seam(cfg):
    runtime.register_memory_backend("dicts", lambda c: DictMemoryBackend())
    cfg["memory_backend"] = "dicts"
    runtime.configure(cfg)
    eng = runtime.engine()
    assert isinstance(eng.backend, DictMemoryBackend)


def test_git_backend_from_config(cfg, tmp_path):
    cfg["memory_backend"] = "git"
    cfg["memory_dir"] = str(tmp_path / "atoms")
    runtime.configure(cfg)
    eng = runtime.engine()
    from usr.plugins.device_sync.helpers.memory_backend import GitMemoryBackend

    assert isinstance(eng.backend, GitMemoryBackend)


def test_stop_clears_engine(cfg):
    runtime.configure(cfg)
    runtime.stop()
    assert runtime.engine() is None
    runtime.stop()  # idempotent
