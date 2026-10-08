# a0-plugin-device-sync

Cross-instance sync packs for [Agent Zero](https://github.com/agent0ai/agent-zero):
secret-free settings, chats, and memory atoms between a0 boxes over Tailscale
(or a manual peers file). Ported from the Khan fork's `device_sync` +
`continuity_sync` helpers onto stock a0's plugin surface — no upstream edits.

## What it syncs

| Pack | Format | Notes |
|---|---|---|
| settings | JSON `khan-settings` v1 | secrets scrubbed on export AND dropped on import — local secrets always win |
| chats | ZIP `khan-chats` v1 | manifest + `chats/<ctxid>.json` + kurultai NDJSON; import dedupes by `ctxid` |
| memory | NDJSON atoms | pluggable backend (`MemoryBackend` protocol); import idempotent by atom `id` |

Pack format tags keep the `khan-` prefix on purpose — a Khan box and a stock-a0
box running this plugin can sync with each other.

## How sync works

Peers are other a0 instances reachable on your Tailscale tailnet. Discovery
tries `tailscale status --json` first, then `~/.a0-device-sync/peers.json`.
Each peer is probed on `peer_port` (default 80 — the plugin's endpoints ride
a0's normal HTTP port). Sync directions: `push`, `pull`, `bidirectional`.

Endpoints (all POST, all gated by a shared bearer token):

```
/api/plugins/device_sync/settings_export   settings_import
/api/plugins/device_sync/chats_export      chats_import
/api/plugins/device_sync/memory_export     memory_import
/api/plugins/device_sync/peers             sync_now
```

Peer auth: `Authorization: Bearer <sync_token>` (or `X-Device-Sync-Token`).
This is machine-to-machine traffic — session auth can't ride a urllib client
and CSRF is meaningless without cookies, so the handlers declare both off and
enforce the token themselves. **Empty token = every endpoint refuses.**

Settings never auto-merge in a bidirectional sync — differing keys are
returned in `settings_conflicts` for manual review. Chats and memory atoms
converge by idempotent id-based import.

## Config

`default_config.yaml` shows every key; env overrides: `DEVICE_SYNC_ENABLED`,
`DEVICE_SYNC_TOKEN`, `DEVICE_SYNC_PEERS_FILE`, `DEVICE_SYNC_PEER_PORT`,
`DEVICE_SYNC_INTERVAL_S`, `DEVICE_SYNC_TIMEOUT_S`, `DEVICE_SYNC_MEMORY_BACKEND`,
`DEVICE_SYNC_MEMORY_DIR`.

- `enabled: false` by default — the plugin is inert until you turn it on.
- `sync_token` empty by default — required before any endpoint answers.
- `auto_sync_interval_s: 0` — manual sync only; set e.g. `300` for a
  background thread that bidirectional-syncs every discovered peer.

## Memory backends

`memory_backend: none` (default) — memory sync reports "unsupported", the
same graceful state a peer without memory endpoints shows.

`memory_backend: git` — MemFS-style: `memory_dir` is a git repo of
`<atom_id>.json` files. Import writes + commits; push/pull are real git
operations (`backend.pull()` / `backend.push()`), so backup, `git diff`,
and `git revert` rollback all come free. Missing git binary or repo dir
degrades to an empty store — never raises into a sync.

Custom: a sibling plugin (e.g. a Kurultai store) can fill the seam:

```python
from usr.plugins.device_sync.helpers import runtime
runtime.register_memory_backend("kurultai", lambda cfg: MyStore(cfg))
```

Implement `export_atoms() -> list[dict]`, `has_atom(id) -> bool`,
`import_atoms(atoms) -> int`. Register before `configure()` runs (e.g. in
your plugin's earlier-numbered `startup_migration` extension).

## Safety

- ZIP extraction is bounded: upload cap (50 MiB), entry count (5000),
  per-entry (20 MiB) and total (200 MiB) uncompressed sizes, compression
  ratio guard. Entries are read to memory only — member names never reach
  a filesystem path, so traversal is structurally impossible.
- Secrets never leave the box: `SENSITIVE_SETTINGS_KEYS` are scrubbed from
  exports and dropped from imports.
- Peer failures degrade per-step: one unreachable endpoint fails that pack
  type, not the whole sync; a 404 memory endpoint = "peer doesn't support
  memory yet", not an error.

## Tests

```bash
python -m pytest tests/ -q
```

No external services needed — the HTTP seam is stubbed in tests.

## Status / limits

- Peer detection is a TCP probe on `peer_port` — it proves "something
  answers", and sync calls then validate by pack format.
- No pack encryption — the threat model is tailnet transport security plus
  the shared token. Secrets don't cross the wire regardless.
- Settings conflict resolution is manual by design.
