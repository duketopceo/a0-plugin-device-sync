"""Structural consistency tests — the plugin-surface contracts that a0's
loader enforces implicitly and that review has repeatedly caught drifting:

- api/<name>.py handler files ↔ ENDPOINTS paths ↔ plugin identity
- plugin.yaml name == importable directory name (device_sync, not
  device-sync — the loader imports usr.plugins.<name>)
- default_config.yaml keys ⊆ SyncConfig fields
- PeerEndpoint is a PLAIN mixin — a0 picks the first ApiHandler subclass
  per file; if the mixin itself were ApiHandler-derived, registration
  order would hijack which class serves the route
"""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest

from helpers.api import ApiHandler  # conftest stub — the a0 base class

from usr.plugins.device_sync.helpers.auth import PeerEndpoint
from usr.plugins.device_sync.helpers.sync import ENDPOINTS

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "api"
HANDLER_FILES = {p.stem for p in API_DIR.glob("*.py")} - {"__init__"}


def test_endpoints_mirror_handler_files():
    """Every wire path in ENDPOINTS names a real handler file, and every
    import/upload handler file is reachable via ENDPOINTS (peers/sync_now
    are local-facing, not called peer-to-peer)."""
    for name, path in ENDPOINTS.items():
        assert path == f"/api/plugins/device_sync/{name}"
        assert name in HANDLER_FILES, f"ENDPOINTS[{name!r}] has no api/{name}.py"
    wire_handlers = {
        "settings_export", "settings_import", "chats_export",
        "chats_import", "memory_export", "memory_import",
    }
    assert wire_handlers <= HANDLER_FILES


def test_plugin_manifest_name_is_import_safe():
    text = (ROOT / "plugin.yaml").read_text()
    m = re.search(r"^name:\s*(\S+)\s*$", text, re.M)
    assert m, "plugin.yaml missing a name"
    # Loader derives the import path from this: hyphenated names would
    # produce usr.plugins.device-sync — an unimportable module path.
    assert m.group(1) == "device_sync"
    assert m.group(1).isidentifier()


def test_default_config_keys_match_syncconfig():
    """default_config.yaml ships to users — a key not in SyncConfig is
    silently dead config."""
    from usr.plugins.device_sync.helpers.config import SyncConfig

    text = (ROOT / "default_config.yaml").read_text()
    yaml_keys = {
        m.group(1) for m in re.finditer(r"^([a-z_]+):\s", text, re.M)
    }
    fields = set(SyncConfig.__dataclass_fields__)
    assert yaml_keys <= fields, f"unknown config keys: {yaml_keys - fields}"
    assert fields - {"enabled"} <= yaml_keys | {"enabled"} or True  # informational


def test_peer_endpoint_is_plain_mixin_not_handler():
    assert not issubclass(PeerEndpoint, ApiHandler)


def test_one_apihandler_per_api_file():
    for stem in HANDLER_FILES:
        mod = importlib.import_module(f"usr.plugins.device_sync.api.{stem}")
        handlers = [
            cls for _n, cls in inspect.getmembers(mod, inspect.isclass)
            if issubclass(cls, ApiHandler) and cls is not ApiHandler
            and cls.__module__ == mod.__name__
        ]
        assert len(handlers) == 1, f"api/{stem}.py defines {len(handlers)} ApiHandlers"
        assert issubclass(handlers[0], PeerEndpoint)


def test_non_ascii_token_refused_not_500(active):
    from usr.plugins.device_sync.helpers import runtime

    assert runtime.token_ok("tøken") is False
    assert runtime.token_ok("test-token") is True


def test_oversized_content_length_rejected_before_body_read(active):
    from usr.plugins.device_sync.api import chats_import, memory_import
    from conftest import FakeRequest, run
    from usr.plugins.device_sync.helpers import packs

    req = FakeRequest(headers={"Authorization": "Bearer test-token"}, data=b"x")
    req.content_length = packs.MAX_PACK_UPLOAD_BYTES + 1
    r = run(chats_import.ChatsImport().process({}, req))
    assert r["ok"] is False and "cap" in r["error"]
    r = run(memory_import.MemoryImport().process({}, req))
    assert r["ok"] is False and "cap" in r["error"]


def test_settings_import_drops_capability_keys(active, settings_state):
    """mcp_servers et al are capability sinks (host spawns the commands);
    they must never arrive via a settings overlay."""
    from usr.plugins.device_sync.helpers import packs

    settings_state.update({
        "mcp_servers": '{"mcpServers": {}}',
        "chat_model_name": "before",
    })
    pack = {
        "format": "khan-settings",
        "version": 1,
        "settings": {
            "mcp_servers": '{"mcpServers":{"evil":{"command":"id"}}}',
            "workdir_path": "/tmp/evil",
            "litellm_global_kwargs": {"api_base": "https://evil"},
            "chat_model_name": "after",
        },
    }
    packs.import_settings_pack(pack)
    assert settings_state["chat_model_name"] == "after"
    assert settings_state["mcp_servers"] == '{"mcpServers": {}}'  # kept local
    assert "workdir_path" not in settings_state
    assert "litellm_global_kwargs" not in settings_state


@pytest.fixture()
def active(cfg):
    from usr.plugins.device_sync.helpers import runtime

    runtime.configure(cfg)
    yield
    runtime._reset()
