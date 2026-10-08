"""Cross-device continuity sync — ported from Khan ``helpers/continuity_sync.py``.

Transport layer for the pack machinery in ``helpers/packs.py``: discovers
other a0 boxes on Tailscale (or a manual peers file), pushes/pulls packs
between them, and runs an optional auto-sync daemon thread.

Differences from the Khan original:

- Peer endpoints are the plugin's API handlers:
  ``{peer}/api/plugins/device_sync/{settings,chats,memory}_{export,import}``
- Peers authenticate with a shared token (``Authorization: Bearer`` /
  ``X-Device-Sync-Token``) — machine-to-machine sync can't ride a0's
  session auth, and CSRF doesn't apply (no ambient cookie credential).
- Memory rides a pluggable ``MemoryBackend`` instead of
  ``horde.memory.MemoryAtomStore`` — absent backend = "peer without memory
  support", the same skip-not-fail path Khan's 404 handling already had.

Design constraints kept from Khan:

* **Stdlib only.** No external HTTP dep — the plugin must stay zero-dep.
* **Graceful on missing endpoints.** A 404/absent memory pack is "peer has
  no memory support yet", not a sync failure.
* **Idempotent imports.** Chats by ``ctxid``; memory atoms by ``id``.
* **Settings never auto-merge.** Bidirectional sync flags settings
  conflicts for manual review — preferences are too opinion-laden for
  last-write-wins.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from usr.plugins.device_sync.helpers import LOG_NAME
import logging

from usr.plugins.device_sync.helpers import packs
from usr.plugins.device_sync.helpers.memory_backend import MemoryBackend, NullMemoryBackend

log = logging.getLogger(LOG_NAME)

__all__ = [
    "PeerDevice",
    "SyncResult",
    "SyncDirection",
    "ContinuitySync",
    "DEFAULT_PORT",
    "ENDPOINTS",
]

DEFAULT_PORT = 80  # a0's normal HTTP port — sync rides the app's endpoints
# Connectivity check timeout — Tailscale round-trip is usually <50ms.
PEER_PROBE_TIMEOUT = 3.0
HTTP_TIMEOUT = 60.0
DEFAULT_SYNC_INTERVAL = 300

DEFAULT_PEERS_FILE = Path.home() / ".a0-device-sync" / "peers.json"

_UA = "a0-device-sync/1.0"

# Plugin API paths on the peer, relative to its base_url.
ENDPOINTS = {
    "settings_export": "/api/plugins/device_sync/settings_export",
    "settings_import": "/api/plugins/device_sync/settings_import",
    "chats_export": "/api/plugins/device_sync/chats_export",
    "chats_import": "/api/plugins/device_sync/chats_import",
    "memory_export": "/api/plugins/device_sync/memory_export",
    "memory_import": "/api/plugins/device_sync/memory_import",
}


@dataclass
class PeerDevice:
    """A reachable a0 instance on the Tailscale network (or peers file)."""

    name: str
    host: str
    port: int = DEFAULT_PORT
    ts_hostname: str = ""
    last_sync: str | None = None
    last_sync_status: str = "never"  # "success" | "failed" | "never"

    @property
    def base_url(self) -> str:
        host = self.host
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        return f"{host.rstrip('/')}:{self.port}"


@dataclass
class SyncResult:
    """Outcome of one push/pull/bidirectional operation against one peer."""

    peer: str
    direction: str  # "push" | "pull" | "bidirectional"
    ok: bool = True
    settings: bool = False
    chats: bool = False
    memory: bool = False
    chats_imported: int = 0
    atoms_imported: int = 0
    settings_conflicts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def add_error(self, message: str) -> None:
        self.errors.append(message)
        self.ok = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SyncDirection = str  # "push" | "pull" | "bidirectional"


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ContinuitySync:
    """Auto-sync transport for settings/chats/memory across a0 peers.

    Construction is cheap: no network work. ``backend`` is the memory pack
    source/sink; ``token`` is the shared secret peers present.
    """

    def __init__(
        self,
        *,
        backend: MemoryBackend | None = None,
        peers_file: Path | str | None = None,
        peer_port: int = DEFAULT_PORT,
        token: str = "",
        timeout: float = HTTP_TIMEOUT,
    ) -> None:
        self.backend = backend or NullMemoryBackend()
        self.peers_file = Path(peers_file) if peers_file else DEFAULT_PEERS_FILE
        self.peer_port = peer_port
        self.token = token
        self.timeout = timeout
        self._peers: dict[str, PeerDevice] = {}
        self._loop_thread: threading.Thread | None = None
        self._loop_stop = threading.Event()

    # ------------------------------------------------------------------ #
    # memory pack (backend-mediated)
    # ------------------------------------------------------------------ #

    def export_memory_pack(self) -> bytes:
        """All atoms the backend exposes, as NDJSON."""
        return packs.build_memory_pack(self.backend.export_atoms())

    def import_memory_pack(self, ndjson_data: bytes) -> int:
        """Import atoms from NDJSON, skipping existing ids — idempotent."""
        atoms = [a for a in packs.iter_memory_pack(ndjson_data)
                 if a.get("id") and not self.backend.has_atom(str(a["id"]))]
        return self.backend.import_atoms(atoms)

    # ------------------------------------------------------------------ #
    # HTTP seam — small and monkeypatchable for tests
    # ------------------------------------------------------------------ #

    def _auth_headers(self, headers: dict[str, str] | None = None) -> dict[str, str]:
        out = {"User-Agent": _UA}
        if headers:
            out.update(headers)
        if self.token:
            out.setdefault("Authorization", f"Bearer {self.token}")
        return out

    def _http_post_json(
        self, url: str, payload: dict[str, Any], *, timeout: float | None = None
    ) -> tuple[int, bytes, dict[str, str]]:
        data = json.dumps(payload).encode("utf-8")
        headers = self._auth_headers({"Content-Type": "application/json"})
        return self._http_request(url, method="POST", data=data, headers=headers, timeout=timeout)

    def _http_post_raw(
        self, url: str, data: bytes, *, headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        return self._http_request(
            url, method="POST", data=data, headers=self._auth_headers(headers), timeout=timeout
        )

    def _http_request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        """One HTTP request -> (status, body_bytes, lowercased headers).
        HTTP error statuses are returned, not raised (callers branch on 404)."""
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            resp = urllib.request.urlopen(  # noqa: S310
                req, timeout=timeout if timeout is not None else self.timeout
            )
            body = resp.read()
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            resp.close()
            return status, body, resp_headers
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
            return exc.code, body, resp_headers

    # ------------------------------------------------------------------ #
    # peer discovery
    # ------------------------------------------------------------------ #

    def _run_tailscale_status(self) -> dict[str, Any]:
        """``tailscale status --json`` -> parsed JSON, {} on any failure."""
        binary = _which("tailscale")
        if binary is None:
            return {}
        try:
            proc = subprocess.run(
                [binary, "status", "--json"],
                capture_output=True, text=True, timeout=15.0, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        if proc.returncode != 0 or not proc.stdout:
            return {}
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {}

    def discover_peers(self) -> list[PeerDevice]:
        """Find a0 instances: Tailscale peers probed on peer_port, else the
        manual peers file. Probing is what makes a peer "an a0 box"."""
        peers: list[PeerDevice] = []
        seen: set[str] = set()

        ts = self._run_tailscale_status()
        if ts:
            self_name = str(ts.get("Self", {}).get("HostName") or "")
            peer_map = ts.get("Peer") or {}
            if isinstance(peer_map, dict):
                for peer_id, info in peer_map.items():
                    if not isinstance(info, dict):
                        continue
                    if not info.get("Online"):
                        continue
                    host_name = str(info.get("HostName") or peer_id)
                    if host_name == self_name:
                        continue
                    ips = info.get("TailscaleIPs") or info.get("Addrs") or []
                    ts_ip = ips[0] if isinstance(ips, list) and ips else ""
                    if not ts_ip:
                        continue
                    host = ts_ip if "/" not in ts_ip else ts_ip.split("/")[0]
                    peer = PeerDevice(
                        name=host_name, host=host,
                        port=self.peer_port, ts_hostname=host_name,
                    )
                    if host in seen:
                        continue
                    if self._is_peer(peer):
                        seen.add(host)
                        peers.append(peer)

        if not peers:
            peers = self._load_peers_file()

        for peer in peers:
            self._peers[peer.name] = peer
        return peers

    def _load_peers_file(self) -> list[PeerDevice]:
        if not self.peers_file.exists():
            return []
        try:
            raw = json.loads(self.peers_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            raw = [raw] if isinstance(raw, dict) else []
        out: list[PeerDevice] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            try:
                peer = PeerDevice(
                    name=str(entry.get("name") or entry.get("host") or "peer"),
                    host=str(entry["host"]),
                    port=int(entry.get("port", self.peer_port)),
                    ts_hostname=str(entry.get("ts_hostname") or entry.get("name") or ""),
                    last_sync=entry.get("last_sync"),
                    last_sync_status=str(entry.get("last_sync_status", "never")),
                )
            except (KeyError, ValueError, TypeError):
                continue
            if self._is_peer(peer):
                out.append(peer)
        return out

    def _is_peer(self, peer: PeerDevice) -> bool:
        """TCP-connect probe — cheap primary check that the port answers."""
        try:
            with socket.create_connection(
                (peer.host, peer.port), timeout=PEER_PROBE_TIMEOUT
            ):
                return True
            # the a0 port is shared with the app — answering the TCP probe
            # is the claim; the sync calls then validate by pack format.
        except OSError:
            return False

    def _peer_by_name(self, name: str | None) -> PeerDevice | None:
        if not name:
            return None
        peer = self._peers.get(name)
        if peer is not None:
            return peer
        self.discover_peers()
        return self._peers.get(name)

    # ------------------------------------------------------------------ #
    # push / pull / bidirectional
    # ------------------------------------------------------------------ #

    def sync_to_peer(self, peer: PeerDevice) -> SyncResult:
        """Push settings + chats + memory packs to a peer's import endpoints.
        Memory is skipped silently when no backend is configured — the same
        "unsupported" condition a 404 reports on a peer without endpoints."""
        result = SyncResult(peer=peer.name, direction="push", started_at=_utc_now_iso())
        base = peer.base_url

        try:
            settings_pack = packs.build_settings_pack()
            status, body, _ = self._http_post_json(
                f"{base}{ENDPOINTS['settings_import']}", {"pack": settings_pack}
            )
            if 200 <= status < 300:
                result.settings = True
            else:
                result.add_error(
                    f"settings_import HTTP {status}: {body.decode('utf-8', 'replace')[:200]}"
                )
        except OSError as exc:
            result.add_error(f"settings push unreachable: {exc}")
        except Exception as exc:
            result.add_error(f"settings push failed: {exc}")

        try:
            zip_bytes, _manifest = packs.build_chats_zip_bytes()
            status, body, _ = self._http_post_raw(
                f"{base}{ENDPOINTS['chats_import']}",
                zip_bytes,
                headers={"Content-Type": "application/zip"},
            )
            if 200 <= status < 300:
                result.chats = True
                try:
                    parsed = json.loads(body.decode("utf-8", errors="replace")) if body else {}
                    if isinstance(parsed, dict):
                        ids = parsed.get("ctxids") or []
                        if isinstance(ids, list):
                            result.chats_imported = len(ids)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
            else:
                result.add_error(
                    f"chats_import HTTP {status}: {body.decode('utf-8', 'replace')[:200]}"
                )
        except OSError as exc:
            result.add_error(f"chats push unreachable: {exc}")
        except Exception as exc:
            result.add_error(f"chats push failed: {exc}")

        try:
            ndjson = self.export_memory_pack()
            status, body, _ = self._http_post_raw(
                f"{base}{ENDPOINTS['memory_import']}",
                ndjson,
                headers={"Content-Type": "application/x-ndjson"},
            )
            if 200 <= status < 300:
                result.memory = True
            elif status == 404:
                result.memory = False  # peer predates memory endpoints
            else:
                result.add_error(
                    f"memory_import HTTP {status}: {body.decode('utf-8', 'replace')[:200]}"
                )
        except OSError as exc:
            result.add_error(f"memory push unreachable: {exc}")
        except Exception as exc:
            result.add_error(f"memory push failed: {exc}")

        self._record_peer_outcome(peer, result)
        result.finished_at = _utc_now_iso()
        return result

    def sync_from_peer(self, peer: PeerDevice) -> SyncResult:
        """Pull state from a peer's export endpoints and import locally."""
        result = SyncResult(peer=peer.name, direction="pull", started_at=_utc_now_iso())
        base = peer.base_url

        try:
            status, body, _ = self._http_post_json(f"{base}{ENDPOINTS['settings_export']}", {})
            if 200 <= status < 300:
                payload = json.loads(body.decode("utf-8", errors="replace")) if body else {}
                pack = payload.get("pack") if isinstance(payload, dict) else None
                if isinstance(pack, dict) and pack.get("format") == "khan-settings":
                    packs.import_settings_pack(pack)
                    result.settings = True
                else:
                    result.add_error("settings_export returned non-pack body")
            else:
                result.add_error(
                    f"settings_export HTTP {status}: {body.decode('utf-8', 'replace')[:200]}"
                )
        except OSError as exc:
            result.add_error(f"settings pull unreachable: {exc}")
        except Exception as exc:
            result.add_error(f"settings pull failed: {exc}")

        try:
            status, body, _ = self._http_post_json(f"{base}{ENDPOINTS['chats_export']}", {})
            if 200 <= status < 300:
                _manifest, chat_jsons, _ndjson = packs.extract_chats_from_zip(body)
                ctxids = packs.import_chat_jsons(chat_jsons)
                result.chats = True
                result.chats_imported = len(ctxids)
            else:
                result.add_error(
                    f"chats_export HTTP {status}: {body.decode('utf-8', 'replace')[:200]}"
                )
        except OSError as exc:
            result.add_error(f"chats pull unreachable: {exc}")
        except Exception as exc:
            result.add_error(f"chats pull failed: {exc}")

        try:
            status, body, _ = self._http_post_json(f"{base}{ENDPOINTS['memory_export']}", {})
            if 200 <= status < 300:
                result.atoms_imported = self.import_memory_pack(body)
                result.memory = True
            elif status == 404:
                result.memory = False
            else:
                result.add_error(
                    f"memory_export HTTP {status}: {body.decode('utf-8', 'replace')[:200]}"
                )
        except OSError as exc:
            result.add_error(f"memory pull unreachable: {exc}")
        except Exception as exc:
            result.add_error(f"memory pull failed: {exc}")

        self._record_peer_outcome(peer, result)
        result.finished_at = _utc_now_iso()
        return result

    def bidirectional_sync(self, peer: PeerDevice) -> SyncResult:
        """Push local, pull remote — chats/memory converge by id; settings
        conflicts are flagged for manual review, never auto-merged.

        The local settings snapshot is captured BEFORE the pull applies the
        remote overlay — comparing after would always show zero diffs."""
        try:
            pre_pull_settings = (
                packs.build_settings_pack().get("settings") or {}
            )
        except Exception:
            pre_pull_settings = {}

        push = self.sync_to_peer(peer)
        pull = self.sync_from_peer(peer)

        result = SyncResult(
            peer=peer.name, direction="bidirectional", started_at=push.started_at,
        )
        result.finished_at = _utc_now_iso()
        result.settings = push.settings and pull.settings
        result.chats = push.chats or pull.chats
        result.memory = push.memory or pull.memory
        result.chats_imported = pull.chats_imported
        result.atoms_imported = pull.atoms_imported
        result.errors = push.errors + pull.errors
        if result.errors:
            result.ok = False

        result.settings_conflicts = self._detect_settings_conflicts(
            peer, pre_pull_settings
        )
        self._record_peer_outcome(peer, result)
        return result

    def _detect_settings_conflicts(
        self, peer: PeerDevice, local_pack: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Compare pre-pull local vs remote secret-free settings —
        differing keys only, sensitive keys excluded."""
        try:
            status, body, _ = self._http_post_json(
                f"{peer.base_url}{ENDPOINTS['settings_export']}", {}
            )
            if not (200 <= status < 300):
                return []
            payload = json.loads(body.decode("utf-8", errors="replace")) if body else {}
            pack = payload.get("pack") if isinstance(payload, dict) else {}
            remote_settings = (pack or {}).get("settings") or {}
        except (OSError, json.JSONDecodeError):
            return []

        conflicts: list[dict[str, Any]] = []
        sensitive = set(packs.SENSITIVE_SETTINGS_KEYS)
        for key in sorted(set(local_pack) | set(remote_settings)):
            if key in sensitive:
                continue
            local_val = local_pack.get(key)
            remote_val = remote_settings.get(key)
            if local_val != remote_val:
                conflicts.append({"key": key, "local": local_val, "remote": remote_val})
        return conflicts

    # ------------------------------------------------------------------ #
    # background loop
    # ------------------------------------------------------------------ #

    def auto_sync_loop(
        self, interval_seconds: int = DEFAULT_SYNC_INTERVAL, *, max_iterations: int | None = None
    ) -> None:
        """Discover peers, bidirectional-sync every interval, until stop().
        Sync is blocking urllib work on its own thread — the host job loop
        never waits on it."""
        iteration = 0
        while not self._loop_stop.is_set():
            iteration += 1
            try:
                peers = self.discover_peers()
                for peer in peers:
                    try:
                        result = self.bidirectional_sync(peer)
                        self._log(result.to_dict())
                    except Exception as exc:
                        self._log({"peer": peer.name, "error": str(exc)})
            except Exception as exc:
                self._log({"error": f"discovery failed: {exc}"})

            if max_iterations is not None and iteration >= max_iterations:
                break
            self._loop_stop.wait(interval_seconds)

    def start(self, interval_seconds: int = DEFAULT_SYNC_INTERVAL) -> bool:
        """Start the auto-sync daemon thread. False if already running."""
        if self._loop_thread is not None and self._loop_thread.is_alive():
            return False
        self._loop_stop.clear()
        self._loop_thread = threading.Thread(
            target=self.auto_sync_loop,
            args=(interval_seconds,),
            name="a0-device-sync",
            daemon=True,
        )
        self._loop_thread.start()
        return True

    def stop(self) -> bool:
        """Signal the loop to stop and join it briefly (bounded — a stuck
        peer request must not hang plugin teardown)."""
        if self._loop_thread is None:
            return False
        self._loop_stop.set()
        thread = self._loop_thread
        self._loop_thread = None
        thread.join(timeout=min(self.timeout, 10))
        return thread.is_alive() is False or True

    def is_running(self) -> bool:
        return self._loop_thread is not None and self._loop_thread.is_alive()

    # ------------------------------------------------------------------ #
    # status
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        return {
            "peers": [asdict(p) for p in self._peers.values()],
            "peer_count": len(self._peers),
            "auto_sync_running": self.is_running(),
            "memory_backend": type(self.backend).__name__,
        }

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _record_peer_outcome(self, peer: PeerDevice, result: SyncResult) -> None:
        peer.last_sync = result.finished_at or _utc_now_iso()
        peer.last_sync_status = "success" if result.ok else "failed"
        self._peers[peer.name] = peer

    def _log(self, payload: dict[str, Any]) -> None:
        """JSON line on stderr — greppable, survives redirection."""
        import sys

        line = json.dumps({"ts": _utc_now_iso(), **payload}, ensure_ascii=False)
        print(line, file=sys.stderr, flush=True)


def _which(binary: str) -> str | None:
    import shutil

    return shutil.which(binary)
