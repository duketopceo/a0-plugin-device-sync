"""Typed plugin config + validation — loaded via a0's plugin-config seam.

``get_config`` calls the host's ``get_plugin_config`` when running inside
a0 (deep-merges ``default_config.yaml``); standalone/tests pass a raw dict
or rely on these defaults. Env vars mirror the yaml keys (DEVICE_SYNC_*).
Never raises out of callers — validation errors clamp to safe defaults.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Any

from usr.plugins.device_sync.helpers import LOG_NAME

log = logging.getLogger(LOG_NAME)

_ENV_MAP = {
    "enabled": "DEVICE_SYNC_ENABLED",
    "sync_token": "DEVICE_SYNC_TOKEN",
    "peers_file": "DEVICE_SYNC_PEERS_FILE",
    "peer_port": "DEVICE_SYNC_PEER_PORT",
    "auto_sync_interval_s": "DEVICE_SYNC_INTERVAL_S",
    "http_timeout_s": "DEVICE_SYNC_TIMEOUT_S",
    "memory_backend": "DEVICE_SYNC_MEMORY_BACKEND",
    "memory_dir": "DEVICE_SYNC_MEMORY_DIR",
}


@dataclass(frozen=True)
class SyncConfig:
    enabled: bool = False
    sync_token: str = ""
    peers_file: str = ""
    peer_port: int = 80
    auto_sync_interval_s: int = 0
    http_timeout_s: float = 60.0
    memory_backend: str = "none"
    memory_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        # token deliberately excluded — never echo secrets into status/logs.
        return {
            "enabled": self.enabled,
            "peers_file": self.peers_file,
            "peer_port": self.peer_port,
            "auto_sync_interval_s": self.auto_sync_interval_s,
            "http_timeout_s": self.http_timeout_s,
            "memory_backend": self.memory_backend,
            "memory_dir": self.memory_dir,
            "token_set": bool(self.sync_token),
        }


_DEFAULTS = SyncConfig()


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _as_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(out):
        return default
    return max(minimum, out)


def _merge_env(raw: dict[str, Any]) -> dict[str, Any]:
    merged = dict(raw)
    for key, env_name in _ENV_MAP.items():
        if env_name in os.environ:
            merged[key] = os.environ[env_name]
    return merged


def _parse(raw: dict[str, Any]) -> SyncConfig:
    return SyncConfig(
        enabled=_as_bool(raw.get("enabled"), _DEFAULTS.enabled),
        sync_token=str(raw.get("sync_token") or _DEFAULTS.sync_token),
        peers_file=str(raw.get("peers_file") or _DEFAULTS.peers_file),
        peer_port=_as_int(raw.get("peer_port"), _DEFAULTS.peer_port, minimum=1),
        auto_sync_interval_s=_as_int(
            raw.get("auto_sync_interval_s"), _DEFAULTS.auto_sync_interval_s
        ),
        http_timeout_s=_as_float(
            raw.get("http_timeout_s"), _DEFAULTS.http_timeout_s, minimum=1.0
        ),
        memory_backend=str(raw.get("memory_backend") or _DEFAULTS.memory_backend)
        .strip()
        .lower(),
        memory_dir=str(raw.get("memory_dir") or _DEFAULTS.memory_dir),
    )


def get_config(config: dict[str, Any] | None = None) -> SyncConfig:
    """Return validated config. ``config`` overrides the host lookup."""
    raw: dict[str, Any] = config if isinstance(config, dict) else {}
    if config is None:
        try:
            from helpers.plugins import get_plugin_config

            loaded = get_plugin_config("device_sync")
            if isinstance(loaded, dict):
                raw = loaded
        except ImportError:
            pass  # standalone/test context — defaults + env
        except Exception as e:
            log.warning("device-sync: get_plugin_config failed, using defaults: %s", e)
    return _parse(_merge_env(raw))
