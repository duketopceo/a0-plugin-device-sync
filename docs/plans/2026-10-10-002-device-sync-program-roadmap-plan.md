# Plan: a0-plugin-device-sync program roadmap (2026-10-10)

Written by hand in the shape of `2026-10-08-001-feat-device-sync-plan.md`: the `ce-plan` skill was invoked with `mode:pipeline confirm:auto` but its multi-reference workflow was not followed to completion in this session. Docs only; no application code changes.

## Source / intent

Program-level roadmap for the Patchbay pass. Evidence: `docs/research/2026-10-10-landscape.md` (plugin section), `README.md`, `AGENTS.md`, the 0.1.0 plan. Repo state: 2 merged PRs, no open issues or PRs. About 2.6k lines of plugin code, 1.9k lines of tests; CI runs `python -m pytest tests/ -q`.

## Now / Next / Later

| Bucket | Units |
|---|---|
| Now | U1 reconcile docs with code; U2 measure Syncthing + a0 Backup/Restore |
| Next | U3 file-in/file-out pack CLI; U4 per-peer tokens and transport enforcement; U5 settings panel |
| Later | U6 pack encryption/signing; U7 split or delete the transport (gated on U2) |

## Units

### U1. Reconcile docs with code (Now)
- Status: partly done in this PR. README rewritten; its old "static 403 identity probe" text contradicted `helpers/auth.py` (HMAC nonce proof). Threat model is in the landscape doc, not yet its own file.
- Evidence: `helpers/auth.py` `peer_proof`, `helpers/sync.py` `_is_peer`, old README.
- Dependencies: none.
- Verification: README statements each map to a function; `tests/test_consistency.py` still passes.
- **Difficulty**: easy. Reading and editing prose.
- **Feasibility**: nothing blocks. Risk is the docs drifting again.
- **Simpler alternative**: fold the threat model into AGENTS.md instead of a new file.

### U2. Measure whether Syncthing + a0 Backup/Restore covers chats and memory (Now)
- Status: not started. The landscape doc marks "a running a0 may not reload synced files" as an inference.
- Evidence needed: read agent0ai/agent-zero source for where chats and memory live and when they are loaded; try two boxes with Syncthing on those folders.
- Dependencies: none. Gates U7.
- Verification: a written result (works / does not, with the a0 version) in `docs/research/`.
- **Difficulty**: medium. Needs two a0 instances and reading upstream.
- **Feasibility**: upstream layout may change between a0 versions; a0 plugin API is documented only via third-party mirrors (landscape section 1).
- **Simpler alternative**: skip the experiment and read a0's Backup/Restore code path only.

### U3. File-in/file-out pack CLI (Next)
- Status: not started. `helpers/packs.py` already builds and imports packs; only the HTTP handlers expose them.
- Evidence: `api/*_export.py`, `api/*_import.py` are thin wrappers.
- Dependencies: U2 (shows whether this is the main path).
- Verification: round-trip test file to file with no HTTP stub; existing pack tests pass unchanged.
- **Difficulty**: medium. Importing chats needs a live a0 context (`AgentContext.all()`), so a standalone CLI cannot do it; it must run inside a0.
- **Feasibility**: the in-a0 constraint above is the real blocker. Exports can be standalone; imports cannot.
- **Simpler alternative**: expose the existing endpoints on localhost and use `curl`; no new code. Packs then travel by Syncthing, Taildrop or scp.

### U4. Per-peer tokens and transport enforcement (Next)
- Status: not started. One shared token for all peers (`helpers/config.py`, `helpers/runtime.py` `token_ok`); `peer_port` defaults to 80 (plain HTTP).
- Evidence: landscape security notes 1, 2, 3.
- Dependencies: none; superseded by U7 if the transport is removed.
- Verification: tests that a revoked peer token is refused and that a non-tailnet peers-file host is refused or warned.
- **Difficulty**: medium. Changes the peers file format and the wire handshake; must keep old/new peers mixing (AGENTS.md graceful-404 rule).
- **Feasibility**: Tailscale identity headers are not available to a plain a0 route as far as the sources show; per-peer tokens are the realistic path.
- **Simpler alternative**: document "tailnet only, one operator" and refuse peers-file hosts outside the tailnet range; touches only `helpers/sync.py` `_load_peers_file`.

### U5. Settings panel with peers and status (Next)
- Status: not started. `plugin.yaml` declares `settings_sections: external` but there is no webui; `api/peers.py` already returns status.
- Evidence: DESIGN.md surfaces section.
- Dependencies: none.
- Verification: screenshot from a running a0 (TODO in README).
- **Difficulty**: medium. Needs a0's webui component conventions, which are known here only from mirrors.
- **Feasibility**: depends on a0 plugin UI hooks staying stable.
- **Simpler alternative**: rely on the auto-generated settings view from `default_config.yaml`; add nothing.

### U6. Optional pack encryption or signing (Later)
- Status: not started. README: "No pack encryption".
- Evidence: restic and chezmoi comparison in the landscape doc.
- Dependencies: U3 helps (file packs are where it matters).
- Verification: tampered pack refused; wrong key refused.
- **Difficulty**: medium. Key distribution is the hard part, not the crypto.
- **Feasibility**: stdlib-only is a family rule; age or libsodium would be an optional lazy import.
- **Simpler alternative**: encrypt at the carrier (Syncthing, Tailscale, restic) and add nothing here.

### U7. Split or delete the transport (Later, gated on U2)
- Status: decision pending.
- Evidence: `helpers/sync.py` (970 lines), `helpers/auth.py` (136), `api/peers.py`, `api/sync_now.py`, `helpers/memory_backend.py` git backend (240).
- Dependencies: U2 must show the carrier tools work; U3 must exist so packs still move.
- Verification: remaining tests pass; README states the new manual flow.
- **Difficulty**: hard. Removes the largest module and changes the public surface (`/api/plugins/device_sync/*`), and breaks wire compatibility with Khan peers that expect the HTTP endpoints.
- **Feasibility**: blocked if users depend on auto-sync; the project has a single known operator, so low blast radius.
- **Simpler alternative**: keep everything but mark transport "legacy" in docs and stop adding features to it.

## Merge / retire assessment

- Merge into another family plugin: no. The other four (durable, glitchtip, notion, jev-compact) share no code or concern with pack transfer; merging would only move 2.6k lines.
- Retire in favour of Syncthing plus a0 Backup and Restore: not yet. They cover files and whole-instance backups, but not secret-free settings merge or id-level chat dedupe into a live instance, which is the unique part (`helpers/packs.py`). Whether the rest is redundant depends on U2.
- Recommended: keep standalone, shrink. Treat `packs.py` as the product and the HTTP transport (`sync.py`, `auth.py`, two API handlers, the git memory backend) as deletable after U2 and U3. Biggest simplification: about 1.3k of 2.6k lines.

## Out of scope
Application code changes in this PR; any change to the `khan-*` format tags.
