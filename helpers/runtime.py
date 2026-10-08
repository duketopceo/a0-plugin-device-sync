"""Runtime facade — owns the configured ContinuitySync instance.

Single configured instance per plugin (like the sibling plugins' runtime
pattern): ``startup_migration`` calls ``configure()``; API handlers and
the host call ``is_active()`` / ``sync_now()`` / ``status()``. All public
methods are failure-contained — a broken plugin must never abort a0's
extension sweep or handler dispatch.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from usr.plugins.device_sync.helpers import LOG_NAME

log = logging.getLogger(LOG_NAME)

_sync = None  # ContinuitySync | None
_cfg = None  # SyncConfig | None
_lock = threading.Lock()


def configure(config: dict[str, Any] | None = None) -> Any:
    """Build/rebuild the sync engine from a raw config dict (None = host
    lookup via get_plugin_config). Returns the ContinuitySync, or None when
    disabled. Safe to call repeatedly."""
    global _sync, _cfg
    from usr.plugins.device_sync.helpers import memory_backend
    from usr.plugins.device_sync.helpers.config import get_config
    from usr.plugins.device_sync.helpers.sync import ContinuitySync

    cfg = get_config(config)
    with _lock:
        old = _sync
        _sync = None
        _cfg = cfg
    if old is not None:
        try:
            old.stop()
        except Exception as e:
            log.warning("device-sync: stopping previous engine failed: %s", e)

    if not cfg.enabled:
        log.info("device-sync: disabled")
        return None

    backend = memory_backend.make_backend(
        cfg.memory_backend, {**cfg.to_dict(), "memory_dir": cfg.memory_dir}
    )
    sync = ContinuitySync(
        backend=backend,
        peers_file=cfg.peers_file or None,
        peer_port=cfg.peer_port,
        token=cfg.sync_token,
        timeout=cfg.http_timeout_s,
    )
    with _lock:
        _sync = sync

    if cfg.auto_sync_interval_s > 0:
        sync.start(interval_seconds=cfg.auto_sync_interval_s)
        log.info(
            "device-sync: auto-sync every %ss (backend=%s)",
            cfg.auto_sync_interval_s,
            type(backend).__name__,
        )
    else:
        log.info("device-sync: manual sync only (backend=%s)", type(backend).__name__)
    return sync


def is_active() -> bool:
    """True when enabled with a live engine AND a sync token — without a
    token the engine exists but every endpoint refuses traffic."""
    return _sync is not None and bool(_cfg and _cfg.sync_token)


def engine() -> Any:
    return _sync


def token_ok(token: str | None) -> bool:
    """Constant-time token check for API handlers. No token configured ->
    refuse everything (secure default)."""
    import secrets

    configured = _cfg.sync_token if _cfg else ""
    return bool(configured) and secrets.compare_digest(token or "", configured)


def sync_now(peer: str | None = None, direction: str = "bidirectional") -> dict[str, Any]:
    """Manual sync trigger. ``peer`` names one peer; None = all discovered."""
    sync = _sync
    if sync is None:
        return {"ok": False, "error": "device-sync is disabled"}
    if direction not in ("push", "pull", "bidirectional"):
        return {"ok": False, "error": f"unknown direction {direction!r}"}

    try:
        targets = [sync.peer_by_name(peer)] if peer else sync.discover_peers()
        targets = [t for t in targets if t is not None]
        if peer and not targets:
            return {"ok": False, "error": f"peer {peer!r} not found/unreachable"}
        if not targets:
            return {"ok": True, "results": [], "note": "no peers discovered"}

        # Push packs are byte-identical across peers — build once when
        # pushing to several. (bidirectional rebuilds settings per peer by
        # design: the pack doubles as the pre-pull conflict snapshot.)
        prebuilt: dict[str, Any] = {}
        if direction == "push":
            from usr.plugins.device_sync.helpers import packs

            try:
                prebuilt["settings_pack"] = packs.build_settings_pack()
                prebuilt["chats_zip"], _ = packs.build_chats_zip_bytes()
                prebuilt["memory_ndjson"] = sync.export_memory_pack()
            except Exception as e:
                log.warning("device-sync: prebuild failed, peers build own packs: %s", e)
                prebuilt = {}

        results = []
        for p in targets:
            if direction == "push":
                r = sync.sync_to_peer(p, **prebuilt)
            elif direction == "pull":
                r = sync.sync_from_peer(p)
            else:
                r = sync.bidirectional_sync(p)
            results.append(r.to_dict())
        return {"ok": all(r["ok"] for r in results), "results": results}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def status() -> dict[str, Any]:
    sync = _sync
    out: dict[str, Any] = {
        "enabled": _cfg.enabled if _cfg else False,
        "active": is_active(),
    }
    if _cfg is not None:
        out["config"] = _cfg.to_dict()
    if sync is not None:
        try:
            out.update(sync.status())
        except Exception as e:
            out["status_error"] = str(e)
    return out


def peers() -> list[dict[str, Any]]:
    sync = _sync
    if sync is None:
        return []
    try:
        from dataclasses import asdict

        return [
            {k: v for k, v in asdict(p).items() if k != "ts_hostname"}
            for p in sync.discover_peers()
        ]
    except Exception as e:
        log.warning("device-sync: peer discovery failed: %s", e)
        return []


def stop() -> None:
    """Tear down the engine (auto-sync loop + runners). Idempotent."""
    global _sync
    sync = _sync
    with _lock:
        _sync = None
    if sync is not None:
        try:
            sync.stop()
        except Exception as e:
            log.warning("device-sync: stop failed: %s", e)


def register_memory_backend(name: str, factory) -> None:
    """Public seam for hosts/sibling plugins (e.g. Kurultai) to register a
    memory backend factory before configure() runs."""
    from usr.plugins.device_sync.helpers import memory_backend

    memory_backend.register_backend(name, factory)


def _reset() -> None:
    """Test/uninstall hook — clears all module state."""
    global _sync, _cfg
    stop()
    with _lock:
        _cfg = None
