# a0-plugin-device-sync — Agent Contract

Cross-instance sync plugin for stock Agent Zero (Khan `device_sync` +
`continuity_sync` port). Plugin surface only — **no upstream edits**.

## Layout

- `helpers/packs.py` — pack build/validate/import. Settings scrub,
  bounded ZIP handling, kurultai NDJSON, temp-file responses.
- `helpers/sync.py` — `ContinuitySync`: peer discovery (tailscale JSON →
  peers file), push/pull/bidirectional, auto-sync thread, stdlib HTTP.
- `helpers/memory_backend.py` — `MemoryBackend` protocol, `null` + `git`
  impls, `register_backend`/`make_backend` seam.
- `helpers/config.py` — `SyncConfig` dataclass, env overrides, clamps.
- `helpers/auth.py` — bearer-token gate for the m2m endpoints.
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
- **Secrets**: `SENSITIVE_SETTINGS_KEYS` scrubbed on export, dropped on
  import; `SyncConfig.to_dict` never emits `sync_token` (`token_set` only).
- **ZIP safety**: `extract_chats_from_zip` reads entries into memory only
  (no filesystem write = no traversal) behind entry/size/ratio caps.
- **Idempotency**: chats dedupe by `ctxid` (stock `load_json_chats`);
  atoms dedupe by `id` (`backend.has_atom` before write).
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
