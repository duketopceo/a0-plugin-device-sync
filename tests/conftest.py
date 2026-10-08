"""Make `usr.plugins.device_sync.*` imports resolvable under pytest — the
same qualified path the a0 runtime uses — and stub the framework modules
the plugin touches (helpers.settings / persist_chat / files / api /
plugins / extension, agent) so everything runs standalone, offline.

The stubs model a0's real contracts:

- settings: one in-memory dict; set_settings replaces and returns it.
- persist_chat: the REAL a0 contract — load_json_chats DELETES `id` and
  mints fresh ctxids (import is a copy, no dedupe); _deserialize_context
  honors a preserved `id`; saved_chat_ids() reports persisted ids. Chat
  dedupe is the PLUGIN's job — these stubs make a regression visible.
- agent: AgentContext registry with USER/BACKGROUND types.
- helpers.api: ApiHandler + Response (status/mimetype/call_on_close) +
  send_file returning a Response; Request with headers + get_data().
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pkg(name, path=None):
    mod = types.ModuleType(name)
    mod.__path__ = [str(path)] if path else []
    return mod


_usr = _pkg("usr")
_plugins = _pkg("usr.plugins")
_ds = _pkg("usr.plugins.device_sync", ROOT)
_usr.plugins = _plugins
_plugins.device_sync = _ds
sys.modules.setdefault("usr", _usr)
sys.modules.setdefault("usr.plugins", _plugins)
sys.modules["usr.plugins.device_sync"] = _ds


# --- minimal a0 framework stubs ---------------------------------------------


class Extension:
    def __init__(self, agent=None, **kw):
        self.agent = agent


class Response:
    """Flask-Response stand-in: body bytes + status + mimetype +
    call_on_close cleanup hooks."""

    def __init__(self, response=b"", status=200, mimetype="text/plain", headers=None):
        self.response = response
        self.status = status
        self.status_code = status
        self.mimetype = mimetype
        self.headers = dict(headers or {})
        self._on_close = []

    def call_on_close(self, fn):
        self._on_close.append(fn)

    def close(self):
        for fn in self._on_close:
            fn()
        self._on_close.clear()

    def get_data(self):
        return self.response if isinstance(self.response, bytes) else str(self.response).encode()

    def get_json(self):
        return json.loads(self.get_data().decode())


class ApiHandler:
    def __init__(self, app=None, thread_lock=None):
        self.app = app
        self.thread_lock = thread_lock

    @classmethod
    def requires_auth(cls) -> bool:
        return True

    @classmethod
    def requires_csrf(cls) -> bool:
        return cls.requires_auth()

    @classmethod
    def get_methods(cls):
        return ["POST"]

    async def process(self, input, request):
        raise NotImplementedError


class _Headers(dict):
    """Case-insensitive header lookup, like werkzeug's Headers."""

    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == str(key).lower():
                return v
        return default


class FakeRequest:
    """werkzeug Request stand-in: headers dict + raw body."""

    def __init__(self, path="/api/x", method="POST", headers=None, data=b""):
        self.path = path
        self.method = method
        self.headers = _Headers(headers or {})
        self._data = data
        self.content_length = len(data)

    def get_data(self):
        return self._data


def send_file(path, as_attachment=False, download_name=None, mimetype=None):
    body = Path(path).read_bytes()
    resp = Response(body, status=200, mimetype=mimetype or "application/octet-stream")
    resp._path = path
    resp._download_name = download_name
    return resp


_settings_state: dict = {}


def _get_settings():
    return dict(_settings_state)


def _set_settings(new):
    _settings_state.clear()
    _settings_state.update(new)
    return dict(_settings_state)


_chats_store: dict[str, str] = {}  # ctxid -> chat json


def _save_tmp_chats():
    # a0 flushes in-memory logs to tmp storage before export; stub is a no-op.
    return None


def _export_json_chat(context):
    return _chats_store.get(context.id) or json.dumps(
        {"id": context.id, "name": f"chat-{context.id}", "log": {"logs": []}}
    )


def _load_json_chats(chat_jsons):
    """a0's REAL contract: `del data["id"]` then _deserialize_context —
    every chat gets a FRESH ctxid. No dedupe exists here; callers that
    need idempotency must dedupe upstream (packs.import_chat_jsons does)."""
    out = []
    for js in chat_jsons:
        data = json.loads(js)
        data.pop("id", None)  # remove id to get new — verbatim host behavior
        ctxid = data["id"] = f"fresh-{len(_chats_store):04d}-{len(out)}"
        _chats_store[ctxid] = json.dumps(data)
        AgentContext(ctxid)
        out.append(ctxid)
    return out


def _deserialize_context(data):
    """Host's context deserializer — honors data["id"] when present."""
    ctxid = str(data.get("id") or f"fresh-{len(_chats_store):04d}")
    ctx = AgentContext(ctxid)
    _chats_store[ctxid] = json.dumps(data)
    return ctx


def _save_tmp_chat(ctx):
    _chats_store[ctx.id] = json.dumps({"id": ctx.id, "name": f"chat-{ctx.id}"})


def _saved_chat_ids():
    return set(_chats_store)


class AgentContextType:
    USER = "user"
    BACKGROUND = "background"


class AgentContext:
    _contexts: dict[str, "AgentContext"] = {}

    def __init__(self, ctxid, ctx_type=AgentContextType.USER):
        self.id = ctxid
        self.type = ctx_type
        AgentContext._contexts[ctxid] = self

    @classmethod
    def all(cls):
        return list(cls._contexts.values())

    @classmethod
    def get(cls, ctxid):
        return cls._contexts.get(ctxid)

    @classmethod
    def _clear(cls):
        cls._contexts.clear()


_files_root = Path("/tmp/a0-device-sync-test-root")


def _get_abs_path(rel):
    return str(_files_root / rel)


_helpers = _pkg("helpers")

_ext = types.ModuleType("helpers.extension")
_ext.Extension = Extension
_helpers.extension = _ext

_plugins_mod = types.ModuleType("helpers.plugins")
_plugins_mod.get_plugin_config = lambda name: {}
_helpers.plugins = _plugins_mod

_api_mod = types.ModuleType("helpers.api")
_api_mod.ApiHandler = ApiHandler
_api_mod.Response = Response
_api_mod.Request = FakeRequest
_api_mod.send_file = send_file
_helpers.api = _api_mod

_settings_mod = types.ModuleType("helpers.settings")
_settings_mod.get_settings = _get_settings
_settings_mod.set_settings = _set_settings
_settings_mod._state = _settings_state
_helpers.settings = _settings_mod

_persist_mod = types.ModuleType("helpers.persist_chat")
_persist_mod.save_tmp_chats = _save_tmp_chats
_persist_mod.export_json_chat = _export_json_chat
_persist_mod.load_json_chats = _load_json_chats
_persist_mod._deserialize_context = _deserialize_context
_persist_mod.save_tmp_chat = _save_tmp_chat
_persist_mod.saved_chat_ids = _saved_chat_ids
_persist_mod._store = _chats_store
_helpers.persist_chat = _persist_mod

_files_mod = types.ModuleType("helpers.files")
_files_mod.get_abs_path = _get_abs_path
_files_mod._root = _files_root
_helpers.files = _files_mod

_agent_mod = types.ModuleType("agent")
_agent_mod.AgentContext = AgentContext
_agent_mod.AgentContextType = AgentContextType

sys.modules.setdefault("helpers", _helpers)
sys.modules["helpers.extension"] = _ext
sys.modules["helpers.plugins"] = _plugins_mod
sys.modules["helpers.api"] = _api_mod
sys.modules["helpers.settings"] = _settings_mod
sys.modules["helpers.persist_chat"] = _persist_mod
sys.modules["helpers.files"] = _files_mod
sys.modules["agent"] = _agent_mod


def run(coro):
    return asyncio.run(coro)


import pytest  # noqa: E402


@pytest.fixture()
def settings_state():
    _settings_state.clear()
    yield _settings_state
    _settings_state.clear()


@pytest.fixture()
def chats_store():
    _chats_store.clear()
    AgentContext._clear()
    yield _chats_store
    _chats_store.clear()
    AgentContext._clear()


@pytest.fixture()
def files_root(tmp_path, monkeypatch):
    monkeypatch.setattr(_files_mod, "get_abs_path", lambda rel: str(tmp_path / rel))
    return tmp_path


@pytest.fixture()
def cfg(tmp_path):
    """Enabled config with a token — the secure-default opposite of the
    shipped default (which is disabled + empty token)."""
    return {
        "enabled": True,
        "sync_token": "test-token",
        "peers_file": str(tmp_path / "peers.json"),
        "peer_port": 50081,
        "auto_sync_interval_s": 0,
        "http_timeout_s": 5,
        "memory_backend": "none",
    }


@pytest.fixture()
def authed_request():
    return FakeRequest(headers={"Authorization": "Bearer test-token"})


@pytest.fixture()
def anon_request():
    return FakeRequest()


class DictMemoryBackend:
    """In-memory backend — the seam a Kurultai-style plugin would fill."""

    def __init__(self, atoms=None):
        self.atoms = {a["id"]: dict(a) for a in (atoms or [])}

    def export_atoms(self):
        return [dict(a) for a in self.atoms.values()]

    def has_atom(self, atom_id):
        return str(atom_id) in self.atoms

    def import_atoms(self, atoms):
        n = 0
        for a in atoms:
            if not self.has_atom(a.get("id")):
                self.atoms[str(a["id"])] = dict(a)
                n += 1
        return n


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Ambient DEVICE_SYNC_* env vars and real filesystem paths must never
    reach tests — _merge_env applies env even over explicit config dicts,
    and helpers.files would default to a shared /tmp root."""
    for var in (
        "DEVICE_SYNC_ENABLED",
        "DEVICE_SYNC_TOKEN",
        "DEVICE_SYNC_PEERS_FILE",
        "DEVICE_SYNC_PEER_PORT",
        "DEVICE_SYNC_INTERVAL_S",
        "DEVICE_SYNC_TIMEOUT_S",
        "DEVICE_SYNC_MEMORY_BACKEND",
        "DEVICE_SYNC_MEMORY_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        _files_mod, "get_abs_path", lambda rel: str(tmp_path / rel)
    )


@pytest.fixture(autouse=True)
def _clean_runtime():
    """Every test starts unconfigured: no engine, no config, no custom
    backend registrations, clean host state."""
    from usr.plugins.device_sync.helpers import memory_backend, runtime

    runtime._reset()
    memory_backend.reset_backends()
    _settings_state.clear()
    _chats_store.clear()
    AgentContext._clear()
    yield
    runtime._reset()
    memory_backend.reset_backends()
    _settings_state.clear()
    _chats_store.clear()
    AgentContext._clear()
