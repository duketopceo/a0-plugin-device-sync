"""Config parsing: defaults, validation, clamps, env overrides."""

from __future__ import annotations

from usr.plugins.device_sync.helpers.config import get_config


def test_defaults_safe():
    c = get_config({})
    assert c.enabled is False
    assert c.sync_token == ""
    assert c.auto_sync_interval_s == 0
    assert c.memory_backend == "none"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("DEVICE_SYNC_ENABLED", "true")
    monkeypatch.setenv("DEVICE_SYNC_TOKEN", "envtok")
    monkeypatch.setenv("DEVICE_SYNC_PEER_PORT", "9999")
    c = get_config({})
    assert c.enabled is True and c.sync_token == "envtok" and c.peer_port == 9999


def test_clamps_and_bad_types():
    c = get_config({"peer_port": -5, "http_timeout_s": "junk", "auto_sync_interval_s": -3})
    assert c.peer_port == 1
    assert c.http_timeout_s == 60.0
    assert c.auto_sync_interval_s == 0


def test_to_dict_never_includes_token():
    c = get_config({"sync_token": "sekret"})
    import json

    assert "sekret" not in json.dumps(c.to_dict())
    assert c.to_dict()["token_set"] is True


def test_non_dict_config_falls_back():
    c = get_config("garbage")
    assert c.enabled is False
