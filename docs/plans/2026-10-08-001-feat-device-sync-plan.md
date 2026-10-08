# Plan: a0-plugin-device-sync — cross-instance sync packs (Khan#195)

## Source / intent

Khan issue [#195](https://github.com/duketopceo/Khan/issues/195) — extract
`helpers/device_sync.py` (408 LOC, manual pack machinery) +
`helpers/continuity_sync.py` (789 LOC, Tailscale transport loop) into a
standalone plugin. Browser session sync persists in-instance only; this
fills the gap — settings/chats/memory packs moving between a0 boxes.

## Settled decisions (from issue + recon)

- **Plugin surface only — no upstream edits.** All seams exist in stock a0
  (verified against `upstream/main`): `settings.get_settings/set_settings`,
  `persist_chat.{save_tmp_chats,export_json_chat,load_json_chats}`,
  `AgentContext.all()`, `files.get_abs_path`, `helpers.api.send_file`,
  `ApiHandler` (auth+CSRF default on; `handle_request` passes a `Response`
  through, so binary downloads work; non-JSON bodies read via
  `request.get_data()`).
- **Endpoints mount at `/api/plugins/device_sync/<handler>`** — a0's
  dispatch maps `plugins/<plugin>/<handler>` to `usr/plugins/<plugin>/api/`.
- **Memory is a pluggable backend, not a horde port.** Khan couples to
  `horde.memory.store.MemoryAtomStore`; a0 has no such thing. The plugin
  defines `MemoryBackend` (export_atoms/import_atoms/has_atom) with two
  shipped impls: `null` (default — memory packs report unsupported, peer
  sees the 404-equivalent path Khan already handles) and `git` (the
  issue's MemFS-style option — the atoms dir IS a git repo: sync =
  commit+pull+push, backup/diff/rollback free). Kurultai (#200) can
  register the real store later via `runtime.register_memory_backend`.
- **Transport stays Tailscale-first** — `tailscale status --json` peer
  discovery + port probe, with a manual peers file fallback
  (`DEVICE_SYNC_PEERS_FILE`). All stdlib urllib, like Khan.
- **Auto-sync keeps the daemon thread** (not a job_loop tick) — sync is
  minutes-long blocking urllib work; a thread with `Event.wait` cadence is
  simpler and doesn't hold the job loop. Thread only starts when
  `enabled` + `auto_sync_interval_s > 0`.
- **Format tags stay `khan-*`** — pack format compatibility with existing
  Khan packs is a feature (a Khan box and an a0 box can sync).

## What ships

```
helpers/packs.py          device_sync.py port: settings pack, chats zip,
                          bounded extract, kurultai ndjson, atoms builder
helpers/sync.py           continuity_sync.py port: PeerDevice, SyncResult,
                          ContinuitySync (discover/push/pull/bidi/loop)
helpers/memory_backend.py MemoryBackend protocol + null/git impls
helpers/config.py         DEFAULTS < get_plugin_config < env
helpers/runtime.py        facade: configure/stop/sync_now/status,
                          memory-backend registry
api/                      8 handlers: settings_export/import,
                          chats_export/import, memory_export/import,
                          sync_now, peers (status folded into peers)
extensions/python/startup_migration/_70_device_sync_init.py  configure+start
hooks.py                  install log-only; uninstall stops the loop thread
```

## Acceptance mapping

- Export/import round-trip on two stock instances → `packs` round-trip
  tests + peer-sync tests over loopback HTTP stubs (monkeypatched
  `_http_request`), no real Tailscale needed.
- Optional git-backed backend documented → `helpers/memory_backend.py`
  `GitMemoryBackend` + README section.
- No upstream file edits → entire repo is the plugin tree.

## Out of scope

- WebUI components (the Khan `webui/` pair stays Khan-side; the API is
  stable enough for them to keep working).
- Horde `MemoryAtom` fidelity — the backend protocol syncs atom *dicts*;
  horde-specific fields round-trip as data.
- `send_temp_file` keeps its cleanup semantics but lives in `packs.py`.
