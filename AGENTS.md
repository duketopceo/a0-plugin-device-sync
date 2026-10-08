# a0-plugin-device-sync — Agent Contract

Cross-instance sync plugin for stock Agent Zero (Khan `device_sync` +
`continuity_sync` port). Plugin surface only — **no upstream edits**.

## Layout

- `helpers/packs.py` — pack build/validate/import. Settings scrub,
  bounded ZIP handling, kurultai NDJSON, temp-file responses.
- `helpers/sync.py` — `ContinuitySync`: peer discovery (tailscale JSON ∪
  peers file, identity-probed), push/pull/bidirectional, auto-sync thread,
  stdlib HTTP (no redirects, no ambient proxies, capped reads).
- `helpers/memory_backend.py` — `MemoryBackend` protocol (`is_null` flag),
  `null` + `git` impls, `register_backend`/`make_backend` seam.
- `helpers/config.py` — `SyncConfig` dataclass, env overrides, clamps
  (interval ≥ 60s floor when nonzero, port range).
- `helpers/auth.py` — `PeerEndpoint` bearer-token mixin. **Plain mixin,
  never ApiHandler-derived** — a0's loader takes the first ApiHandler
  subclass per file; a subclassed mixin would hijack registration.
- `helpers/runtime.py` — configured-engine facade; `configure()`,
  `is_active()`, `token_ok()`, `sync_now()`, `register_memory_backend()`.
- `api/` — 8 POST handlers under `/api/plugins/device_sync/`.
- `extensions/python/startup_migration/_70_` — sync `execute`, owns
  `configure()` (late number so siblings can register backends first).
- `hooks.py` — install log / uninstall teardown. Never raises.

## Conventions

- **Host seams lazy**: `helpers.settings`, `helpers.persist_chat`,
  `helpers.files`, `helpers.api`, `agent` import inside the functions that
  use them — a broken host import can never abort a0's sweep.
- **Plugin self-imports** are fully qualified: `usr.plugins.device_sync.*`.
- **Auth model**: handlers set `requires_auth()/requires_csrf()` = False
  and gate on `auth.check_peer_request` (shared bearer token). Empty
  configured token = 403 everything. Do not reintroduce session auth
  here — it cannot work for m2m clients.
- **Secrets**: `SENSITIVE_SETTINGS_KEYS` + capability patterns
  (`mcp_servers`, `*_kwargs`, `*_path`, `*_url`, `*_profile`, `variables`)
  scrubbed on export, dropped on import — a settings overlay is an RCE
  sink otherwise (host spawns MCP server commands from it).
  `SyncConfig.to_dict` never emits `sync_token` (`token_set` only).
- **ZIP safety**: `extract_chats_from_zip` reads entries into memory only
  (no filesystem write = no traversal) behind entry/size/ratio caps.
  Uploads are refused pre-body on `Content-Length` > cap.
- **Idempotency is plugin-owned**: stock `load_json_chats` DELETES `id`
  and mints fresh ctxids — copy-import, zero dedupe, and echo growth on
  A→B→A. `packs.import_chat_jsons` parses each chat's original id, skips
  ids in `AgentContext.all()` ∪ `persist_chat.saved_chat_ids()`, and
  deserializes novel chats with the id preserved via
  `_deserialize_context` (a live-id construct would kill the context's
  task — never re-feed a known id). Atoms dedupe by `id`.
- **Bidirectional settings are diff-only**: `_push(push_settings=False)` +
  `_pull(apply_settings=False)` — the remote pack is fetched read-only for
  the conflict report. Writing first would clobber the peer AND make the
  diff vacuous. Push/pull remain the merge directions.
- **In-band errors**: import endpoints return `{"ok": false}` at HTTP 200;
  `_import_response_ok` requires `ok is True` or the leg fails.
- **Transport trust**: `_opener` refuses redirects and ambient proxies
  (bearer-token leak sinks); responses read capped at cap+1; `_is_peer`
  demands the 403-`forbidden` signature, not an open port.
- **Lifecycle**: `start`/`stop` under `_lifecycle_lock`; a `stop` landing
  mid-`start` still wins; captured stop event means a later `start` can't
  revive an orphaned loop. No token → no loop. `runtime.configure`
  serializes stop-old → build → start → publish.
- **Graceful-404**: a missing memory endpoint = unsupported peer, not a
  failed sync. Keep this contract — it's what lets old/new peers mix.
- **Never-raise**: extensions, hooks, `auto_sync_loop`, `stop` all catch
  broadly; transport failures land in `SyncResult.errors`.

## Tests

`python -m pytest tests/ -q` — standalone (conftest stubs a0's host
modules). No tailscale/git/network needed; the git-backend test skips
when `git` is absent.

## Provenance

Port of Khan `helpers/device_sync.py` + `helpers/continuity_sync.py`
(MIT, same author). `khan-*` pack format strings are wire-compatible by
design — don't rename them.
