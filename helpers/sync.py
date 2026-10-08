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
import random
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from usr.plugins.device_sync.helpers import packs
from usr.plugins.device_sync.helpers.memory_backend import MemoryBackend, NullMemoryBackend

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
# Discovery (tailscale status + TCP probes) is expensive — peers()/_peer_by_name
# callers share one fresh result for this window instead of re-probing.
DISCOVERY_TTL_S = 60.0
PROBE_WORKERS = 8
# An interval under this is a retry storm (tailscale subprocess + probes +
# full pack exchange per peer per tick).
MIN_AUTO_SYNC_INTERVAL_S = 60

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
    # tailnet hostname — operator-facing identity in status()/peers output;
    # may equal `name` for manual (non-tailscale) peers.
    ts_hostname: str = ""
    last_sync: str | None = None
    last_sync_status: str = "never"  # "success" | "failed" | "never"

    @property
    def base_url(self) -> str:
        host = self.host
        scheme = "http://"
        if "://" in host:
            scheme, host = host.split("://", 1)
            scheme = f"{scheme}://"
        host = host.rstrip("/")
        # A host that already carries a port ("x:8080", "[::1]:8080") is
        # used as-is; a bare IPv6 literal stays bracketed.
        if ":" in host and not host.startswith("[") or "]:" in host:
            return f"{scheme}{host}"
        return f"{scheme}{host}:{self.port}"


@dataclass
class SyncResult:
    """Outcome of one push/pull/bidirectional operation against one peer."""

    peer: str
    direction: "SyncDirection"
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

utc_now_iso = packs.utc_now_iso  # single implementation, same package


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects — a redirect surfaces as its 3xx status (an error
    path for callers) instead of re-sending auth headers elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


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
        # Redirects are refused: urllib would re-send the Authorization
        # header to the redirect target, and a hostile peers-file entry
        # could bounce a sync POST to an arbitrary host — token leak.
        # ProxyHandler({}) disables ambient http_proxy env vars for the
        # same reason — a proxy would otherwise receive the bearer token.
        self._opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.ProxyHandler({})
        )
        self._loop_thread: threading.Thread | None = None
        self._loop_stop = threading.Event()
        # start/stop mutate the thread+event pair under one lock — a
        # racing stop() can otherwise signal the wrong Event or miss a
        # not-yet-registered thread, orphaning a live daemon.
        self._lifecycle_lock = threading.Lock()
        # one sync operation at a time (auto-sync loop vs manual sync_now)
        self._sync_lock = threading.Lock()
        self._discovered_at = 0.0  # monotonic; discovery TTL cache

    # ------------------------------------------------------------------ #
    # memory pack (backend-mediated)
    # ------------------------------------------------------------------ #

    def export_memory_pack(self) -> bytes:
        """All atoms the backend exposes, as NDJSON."""
        return packs.build_memory_pack(self.backend.export_atoms())

    def import_memory_pack(self, ndjson_data: bytes) -> int:
        """Import atoms from NDJSON — the backend dedupes by id per its
        contract ("store atoms that are new"), so this stays idempotent
        without a second has_atom stat per atom."""
        atoms = [a for a in packs.iter_memory_pack(ndjson_data) if a.get("id")]
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
        # Read capped at the upload bound — every legitimate pack is bounded,
        # and a misbehaving peer can't force unbounded memory on the pull path.
        read_cap = packs.MAX_PACK_UPLOAD_BYTES + 1
        try:
            resp = self._opener.open(
                req, timeout=timeout if timeout is not None else self.timeout
            )
            try:
                body = resp.read(read_cap)
            finally:
                resp.close()
            return resp.status, body, {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(read_cap) if exc.fp else b""
            finally:
                exc.close()
            resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
            return exc.code, body, resp_headers

    @staticmethod
    def _truncated(body: bytes) -> bool:
        """Response hit the read cap — pack is incomplete and must not be
        imported (a boundary-cut NDJSON could otherwise import silently)."""
        return len(body) > packs.MAX_PACK_UPLOAD_BYTES

    @staticmethod
    def _server_error(body: bytes) -> str | None:
        """Best-effort extraction of a peer's ``error`` field — import
        handlers return ``{"ok": false, "error": ...}`` at HTTP 200."""
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace")) if body else {}
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            return str(parsed.get("error") or "peer rejected the pack")
        return None

    def _import_response_ok(
        self, status: int, body: bytes, label: str, result: SyncResult
    ) -> tuple[bool, dict[str, Any]]:
        """Check an import endpoint's response. Handlers report domain
        errors as ``{"ok": false}`` at HTTP 200 — status alone is not
        success. Returns (ok, parsed_body)."""
        if not 200 <= status < 300:
            result.add_error(
                f"{label} HTTP {status}: {body[:200].decode('utf-8', 'replace')}"
            )
            return False, {}
        parsed: dict[str, Any] = {}
        try:
            decoded = json.loads(body.decode("utf-8", errors="replace")) if body else {}
            if isinstance(decoded, dict):
                parsed = decoded
        except json.JSONDecodeError:
            pass
        if parsed.get("ok") is not True:
            err = parsed.get("error") or "peer did not confirm the import"
            result.add_error(f"{label}: {err}")
            return False, parsed
        return True, parsed

    # ------------------------------------------------------------------ #
    # peer discovery
    # ------------------------------------------------------------------ #

    def _run_tailscale_status(self) -> dict[str, Any]:
        """``tailscale status --json`` -> parsed JSON, {} on any failure."""
        binary = shutil.which("tailscale")
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

    def discover_peers(self, *, force: bool = False) -> list[PeerDevice]:
        """Find a0 instances: Tailscale peers probed on peer_port, else the
        manual peers file. Probing is what makes a peer "an a0 box".
        Results are TTL-cached — repeated callers within DISCOVERY_TTL_S
        reuse the last discovery instead of re-probing every device."""
        # 0.0 sentinel: monotonic() is seconds-since-boot — inside the first
        # TTL window of uptime an empty cache would otherwise read "fresh".
        if (
            not force
            and self._discovered_at
            and time.monotonic() - self._discovered_at < DISCOVERY_TTL_S
        ):
            return list(self._peers.values())

        candidates: list[PeerDevice] = []
        seen: set[str] = set()
        seen_names: set[str] = set()

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
                    # Prefer a v4 address — base_url bracket-formats v6 but
                    # mixed stacks reach v4 more reliably.
                    ts_ip = ""
                    if isinstance(ips, list):
                        v4 = [i for i in ips if i and ":" not in str(i)]
                        pick = v4[0] if v4 else (ips[0] if ips else "")
                        ts_ip = str(pick)
                    if not ts_ip:
                        continue
                    host = ts_ip if "/" not in ts_ip else ts_ip.split("/")[0]
                    if host in seen or host_name in seen_names:
                        continue
                    seen.add(host)
                    seen_names.add(host_name)
                    candidates.append(
                        PeerDevice(
                            name=host_name, host=host,
                            port=self.peer_port, ts_hostname=host_name,
                        )
                    )

        # Union, not fallback: manual peers coexist with tailscale peers —
        # mixed tailnet + manual setups must see both sets.
        for p in self._load_peers_file():
            if p.host not in seen and p.name not in seen_names:
                seen.add(p.host)
                seen_names.add(p.name)
                candidates.append(p)

        # Probe concurrently — a 20-device tailnet of non-a0 boxes would
        # otherwise cost N x PEER_PROBE_TIMEOUT serially.
        if candidates:
            with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
                alive = list(pool.map(self._is_peer, candidates))
            peers = [p for p, ok in zip(candidates, alive) if ok]
        else:
            peers = []

        # Rebuild the cache from the fresh list (stale names expire);
        # merge prior last_sync bookkeeping onto surviving peers. Build
        # privately then swap — status() iterates _peers concurrently.
        old = self._peers
        new: dict[str, PeerDevice] = {}
        for peer in peers:
            prev = old.get(peer.name)
            if prev is not None:
                peer.last_sync = prev.last_sync
                peer.last_sync_status = prev.last_sync_status
            new[peer.name] = peer
        self._peers = new
        # Only stamp the TTL cache when discovery actually ran — a
        # transient `tailscale status` failure must not cache an empty
        # result for the whole window.
        if ts or self.peers_file.exists():
            self._discovered_at = time.monotonic()
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
                port = int(entry.get("port", self.peer_port))
                if not 1 <= port <= 65535:
                    continue
                out.append(
                    PeerDevice(
                        name=str(entry.get("name") or entry.get("host") or "peer"),
                        host=str(entry["host"]),
                        port=port,
                        ts_hostname=str(
                            entry.get("ts_hostname") or entry.get("name") or ""
                        ),
                        last_sync=entry.get("last_sync"),
                        last_sync_status=str(entry.get("last_sync_status", "never")),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        return out

    def _is_peer(self, peer: PeerDevice) -> bool:
        """Identity probe — POST the peers endpoint WITHOUT credentials and
        require the plugin's 403 {"ok": false, "forbidden"} signature. A
        bare TCP check only proves SOMETHING answers on the port — without
        identity verification, every listening device on the tailnet
        (including shared-in and compromised devices) would receive the
        bearer token and full packs on the next sync."""
        url = f"{peer.base_url}/api/plugins/device_sync/peers"
        req = urllib.request.Request(
            url,
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": _UA},
        )
        try:
            resp = self._opener.open(req, timeout=PEER_PROBE_TIMEOUT)
            resp.close()
            return False  # 2xx without a token — not the plugin's shape
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(4096)
            finally:
                exc.close()
            return exc.code == 403 and b"forbidden" in body
        except Exception:
            return False

    def peer_by_name(self, name: str | None) -> PeerDevice | None:
        """Resolve a named peer; a cache miss triggers a FORCED discovery —
        an explicit name is operator-driven, so the TTL's "just-online"
        blind spot is worth paying a probe round to avoid."""
        if not name:
            return None
        peer = self._peers.get(name)
        if peer is not None:
            return peer
        self.discover_peers(force=True)
        return self._peers.get(name)

    # ------------------------------------------------------------------ #
    # push / pull / bidirectional
    # ------------------------------------------------------------------ #

    def sync_to_peer(
        self,
        peer: PeerDevice,
        *,
        settings_pack: dict[str, Any] | None = None,
        chats_zip: bytes | None = None,
        memory_ndjson: bytes | None = None,
    ) -> SyncResult:
        """Guarded push — reports failure rather than overlapping an
        in-progress sync (auto-sync loop vs manual trigger). Callers
        syncing N peers may pass prebuilt packs so identical artifacts
        aren't rebuilt per peer."""
        if not self._sync_lock.acquire(blocking=False):
            result = SyncResult(
                peer=peer.name, direction="push", started_at=utc_now_iso()
            )
            result.add_error("another sync is already running")
            result.finished_at = utc_now_iso()
            return result
        try:
            return self._push(
                peer,
                settings_pack=settings_pack,
                chats_zip=chats_zip,
                memory_ndjson=memory_ndjson,
            )
        finally:
            self._sync_lock.release()

    def _push(
        self,
        peer: PeerDevice,
        *,
        push_settings: bool = True,
        settings_pack: dict[str, Any] | None = None,
        chats_zip: bytes | None = None,
        memory_ndjson: bytes | None = None,
    ) -> SyncResult:
        """Push settings + chats + memory packs to a peer's import endpoints.
        ``push_settings=False`` skips the settings leg — bidirectional sync
        never writes settings to either side (diff-only contract).

        Import handlers report domain errors as ``{"ok": false}`` at HTTP
        200 — status alone is NOT success, so every response's envelope is
        checked. Memory is skipped when nothing is exportable — the same
        "unsupported" condition a 404 reports on a peer without endpoints.
        """
        result = SyncResult(peer=peer.name, direction="push", started_at=utc_now_iso())
        base = peer.base_url

        if push_settings:
            try:
                if settings_pack is None:
                    settings_pack = packs.build_settings_pack()
                status, body, _ = self._http_post_json(
                    f"{base}{ENDPOINTS['settings_import']}", {"pack": settings_pack}
                )
                ok, _parsed = self._import_response_ok(
                    status, body, "settings_import", result
                )
                result.settings = ok
            except OSError as exc:
                result.add_error(f"settings push unreachable: {exc}")
            except Exception as exc:
                result.add_error(f"settings push failed: {exc}")

        try:
            if chats_zip is None:
                chats_zip, _manifest = packs.build_chats_zip_bytes()
            status, body, _ = self._http_post_raw(
                f"{base}{ENDPOINTS['chats_import']}",
                chats_zip,
                headers={"Content-Type": "application/zip"},
            )
            ok, parsed = self._import_response_ok(
                status, body, "chats_import", result
            )
            if ok:
                result.chats = True
                ids = parsed.get("ctxids") or []
                if isinstance(ids, list):
                    result.chats_imported = len(ids)
        except OSError as exc:
            result.add_error(f"chats push unreachable: {exc}")
        except Exception as exc:
            result.add_error(f"chats push failed: {exc}")

        try:
            if memory_ndjson is None:
                memory_ndjson = self.export_memory_pack()
            if not memory_ndjson:
                # nothing to push (e.g. null backend) — same graceful state
                # as a peer without memory endpoints, without a wasted POST.
                result.memory = False
            else:
                status, body, _ = self._http_post_raw(
                    f"{base}{ENDPOINTS['memory_import']}",
                    memory_ndjson,
                    headers={"Content-Type": "application/x-ndjson"},
                )
                if status == 404:
                    result.memory = False  # peer predates memory endpoints
                else:
                    ok, parsed = self._import_response_ok(
                        status, body, "memory_import", result
                    )
                    if ok:
                        result.memory = True
                        imported = parsed.get("imported")
                        if isinstance(imported, int):
                            result.atoms_imported = imported
        except OSError as exc:
            result.add_error(f"memory push unreachable: {exc}")
        except Exception as exc:
            result.add_error(f"memory push failed: {exc}")

        result.finished_at = utc_now_iso()
        self._record_peer_outcome(peer, result)
        return result

    def sync_from_peer(self, peer: PeerDevice) -> SyncResult:
        """Guarded pull — reports failure rather than overlapping an
        in-progress sync (auto-sync loop vs manual trigger)."""
        if not self._sync_lock.acquire(blocking=False):
            result = SyncResult(
                peer=peer.name, direction="pull", started_at=utc_now_iso()
            )
            result.add_error("another sync is already running")
            result.finished_at = utc_now_iso()
            return result
        try:
            result, _remote = self._pull(peer)
            return result
        finally:
            self._sync_lock.release()

    def _pull(
        self, peer: PeerDevice, *, apply_settings: bool = True
    ) -> tuple[SyncResult, dict[str, Any]]:
        """Pull + return the remote settings dict so bidirectional_sync can
        diff it against the local snapshot without a second fetch.
        ``apply_settings=False`` fetches and validates the remote pack but
        does NOT import it — bidirectional sync is settings-diff-only."""
        result = SyncResult(peer=peer.name, direction="pull", started_at=utc_now_iso())
        base = peer.base_url
        remote_settings: dict[str, Any] = {}

        try:
            status, body, _ = self._http_post_json(f"{base}{ENDPOINTS['settings_export']}", {})
            if 200 <= status < 300:
                if self._truncated(body):
                    result.add_error("settings_export response exceeds size limit")
                else:
                    payload = (
                        json.loads(body.decode("utf-8", errors="replace"))
                        if body
                        else {}
                    )
                    pack = payload.get("pack") if isinstance(payload, dict) else None
                    if (
                        isinstance(pack, dict)
                        and pack.get("format") == packs.SETTINGS_PACK_FORMAT
                    ):
                        if apply_settings:
                            packs.import_settings_pack(pack)
                        result.settings = True
                        if isinstance(pack.get("settings"), dict):
                            remote_settings = pack["settings"]
                    else:
                        server_err = self._server_error(body)
                        result.add_error(
                            server_err or "settings_export returned non-pack body"
                        )
            else:
                result.add_error(
                    f"settings_export HTTP {status}: {body[:200].decode('utf-8', 'replace')}"
                )
        except OSError as exc:
            result.add_error(f"settings pull unreachable: {exc}")
        except Exception as exc:
            result.add_error(f"settings pull failed: {exc}")

        try:
            status, body, _ = self._http_post_json(f"{base}{ENDPOINTS['chats_export']}", {})
            if 200 <= status < 300:
                if self._truncated(body):
                    result.add_error("chats_export response exceeds size limit")
                elif not body.startswith(b"PK"):
                    server_err = self._server_error(body)
                    result.add_error(
                        server_err or "chats_export returned non-zip body"
                    )
                else:
                    _manifest, chat_jsons, ndjson = packs.extract_chats_from_zip(body)
                    ctxids = packs.import_chat_jsons(chat_jsons)
                    result.chats = True
                    result.chats_imported = len(ctxids)
                    try:
                        if ndjson.strip():
                            # pulled chats get kurultai-indexed locally too —
                            # continuity means remote chat memory is searchable.
                            packs.write_kurultai_ndjson(ndjson)
                    except Exception as exc:
                        # Inbox write must not retro-fail a completed import.
                        self._log({"peer": peer.name, "error": f"kurultai inbox: {exc}"})
            else:
                result.add_error(
                    f"chats_export HTTP {status}: {body[:200].decode('utf-8', 'replace')}"
                )
        except OSError as exc:
            result.add_error(f"chats pull unreachable: {exc}")
        except Exception as exc:
            result.add_error(f"chats pull failed: {exc}")

        if getattr(self.backend, "is_null", False):
            # No memory store on this box — fetching would drop every atom
            # on the floor while reporting success.
            result.memory = False
        else:
            try:
                status, body, _ = self._http_post_json(
                    f"{base}{ENDPOINTS['memory_export']}", {}
                )
                if 200 <= status < 300:
                    if self._truncated(body):
                        result.add_error("memory_export response exceeds size limit")
                    else:
                        result.atoms_imported = self.import_memory_pack(body)
                        result.memory = True
                elif status == 404:
                    result.memory = False
                else:
                    result.add_error(
                        f"memory_export HTTP {status}: {body[:200].decode('utf-8', 'replace')}"
                    )
            except OSError as exc:
                result.add_error(f"memory pull unreachable: {exc}")
            except Exception as exc:
                result.add_error(f"memory pull failed: {exc}")

        result.finished_at = utc_now_iso()
        self._record_peer_outcome(peer, result)
        return result, remote_settings

    def bidirectional_sync(
        self,
        peer: PeerDevice,
        *,
        chats_zip: bytes | None = None,
        memory_ndjson: bytes | None = None,
    ) -> SyncResult:
        """Chats/memory converge by id in both directions; settings are
        DIFF-ONLY — fetched from the peer, compared against local, flagged
        in ``settings_conflicts`` for manual review. Neither side's
        settings are written: a bidirectional push would clobber the
        peer's values before the diff could see them, and a bidirectional
        apply would be last-write-wins — both violate never-auto-merge.
        (Pure ``push``/``pull`` directions still apply settings — an
        explicit direction is the operator's merge decision.)

        Runs under the sync lock (internals _push/_pull, so no self-
        deadlock); a contended call reports failure. Optional prebuilt
        chats/memory packs skip the per-peer rebuild — the trade: atoms or
        chats pulled from peer A land in peer B's push next round instead
        of this one (convergence unchanged, one extra hop)."""
        if not self._sync_lock.acquire(blocking=False):
            result = SyncResult(
                peer=peer.name, direction="bidirectional", started_at=utc_now_iso()
            )
            result.add_error("another sync is already running")
            result.finished_at = utc_now_iso()
            return result
        try:
            push = self._push(
                peer,
                push_settings=False,
                chats_zip=chats_zip,
                memory_ndjson=memory_ndjson,
            )
            pull, remote_settings = self._pull(peer, apply_settings=False)

            result = SyncResult(
                peer=peer.name, direction="bidirectional", started_at=push.started_at,
            )
            result.finished_at = utc_now_iso()
            result.settings = pull.settings
            result.chats = push.chats or pull.chats
            result.memory = push.memory or pull.memory
            result.chats_imported = push.chats_imported + pull.chats_imported
            result.atoms_imported = pull.atoms_imported
            result.errors = push.errors + pull.errors
            if result.errors:
                result.ok = False

            # Diff only when the remote fetch succeeded — a failed pull
            # must not fabricate "every key diverged" conflicts.
            if pull.settings:
                try:
                    local_pack = packs.build_settings_pack()
                    local_settings = local_pack.get("settings") or {}
                except Exception:
                    local_settings = {}
                result.settings_conflicts = self._diff_settings(
                    local_settings, remote_settings
                )
            self._record_peer_outcome(peer, result)
            return result
        finally:
            self._sync_lock.release()

    @staticmethod
    def _diff_settings(
        local_settings: dict[str, Any], remote_settings: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Diff pre-pull local vs remote secret-free settings — differing
        keys only, sensitive keys excluded."""
        conflicts: list[dict[str, Any]] = []
        for key in sorted(set(local_settings) | set(remote_settings)):
            if packs.is_sensitive_key(key):
                continue
            local_val = local_settings.get(key)
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
        never waits on it. The stop event is captured once so a later
        start() (fresh Event) can't revive an orphaned loop."""
        stop_event = self._loop_stop
        iteration = 0
        while not stop_event.is_set():
            iteration += 1
            try:
                peers = self.discover_peers(force=True)
                # chats/memory packs are byte-identical across peers in one
                # round — build once (tens of MB each; N× rebuild is waste).
                # Settings stays per-peer: it's also the conflict snapshot.
                try:
                    chats_zip, _ = packs.build_chats_zip_bytes()
                    memory_ndjson = self.export_memory_pack()
                except Exception as exc:
                    chats_zip, memory_ndjson = None, None
                    self._log({"error": f"pack prebuild failed: {exc}"})
                for peer in peers:
                    if stop_event.is_set():
                        return
                    try:
                        result = self.bidirectional_sync(
                            peer, chats_zip=chats_zip, memory_ndjson=memory_ndjson
                        )
                        self._log(result.to_dict())
                    except Exception as exc:
                        self._log({"peer": peer.name, "error": str(exc)})
            except Exception as exc:
                self._log({"error": f"discovery failed: {exc}"})

            if max_iterations is not None and iteration >= max_iterations:
                break
            # ±10% jitter — boxes booted together would otherwise sync in
            # lockstep forever.
            stop_event.wait(interval_seconds * random.uniform(0.9, 1.1))

    def start(self, interval_seconds: int = DEFAULT_SYNC_INTERVAL) -> bool:
        """Start the auto-sync daemon thread. False if already running.
        A fresh Event each start prevents a still-dying previous loop from
        resurrecting on a cleared shared event."""
        with self._lifecycle_lock:
            if self._loop_thread is not None and self._loop_thread.is_alive():
                return False
            interval_seconds = max(interval_seconds, MIN_AUTO_SYNC_INTERVAL_S)
            self._loop_stop = threading.Event()
            thread = threading.Thread(
                target=self.auto_sync_loop,
                args=(interval_seconds,),
                name="a0-device-sync",
                daemon=True,
            )
            self._loop_thread = thread
            thread.start()
            return True

    def stop(self) -> bool:
        """Signal the loop to stop and join it briefly (bounded — a stuck
        peer request must not hang plugin teardown). Returns True when the
        thread actually stopped; False when nothing ran or the join timed
        out (the event stays set — the zombie still exits its next wait)."""
        with self._lifecycle_lock:
            thread = self._loop_thread
            self._loop_thread = None
            if thread is None:
                return False
            self._loop_stop.set()
        thread.join(timeout=min(self.timeout, 10))
        return not thread.is_alive()

    def is_running(self) -> bool:
        thread = self._loop_thread
        return thread is not None and thread.is_alive()

    # ------------------------------------------------------------------ #
    # status
    # ------------------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        return {
            # snapshot — discovery/_record_peer_outcome mutate concurrently
            "peers": [asdict(p) for p in list(self._peers.values())],
            "peer_count": len(self._peers),
            "auto_sync_running": self.is_running(),
            "memory_backend": type(self.backend).__name__,
        }

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _record_peer_outcome(self, peer: PeerDevice, result: SyncResult) -> None:
        peer.last_sync = result.finished_at or utc_now_iso()
        peer.last_sync_status = "success" if result.ok else "failed"
        self._peers[peer.name] = peer

    def _log(self, payload: dict[str, Any]) -> None:
        """JSON line on stderr — greppable, survives redirection. Conflict
        values stay out of logs (keys only — settings blobs are large and
        re-logged every interval otherwise)."""
        safe = dict(payload)
        conflicts = safe.get("settings_conflicts")
        if isinstance(conflicts, list):
            safe["settings_conflicts"] = [
                c.get("key") for c in conflicts if isinstance(c, dict)
            ]
        line = json.dumps({"ts": utc_now_iso(), **safe}, ensure_ascii=False)
        try:
            print(line, file=sys.stderr, flush=True)
        except Exception:
            pass  # a broken stderr must not kill the auto-sync loop
