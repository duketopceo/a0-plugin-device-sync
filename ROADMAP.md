# Roadmap

## Program roadmap, 2026-10-10

Full plan: [docs/plans/2026-10-10-002-device-sync-program-roadmap-plan.md](docs/plans/2026-10-10-002-device-sync-program-roadmap-plan.md). Research: [docs/research/2026-10-10-landscape.md](docs/research/2026-10-10-landscape.md).

| Bucket | Unit | Difficulty |
|---|---|---|
| Now | U1 reconcile docs with code | easy |
| Now | U2 measure Syncthing + a0 Backup/Restore against chats and memory | medium |
| Next | U3 file-in/file-out pack CLI | medium |
| Next | U4 per-peer tokens and transport enforcement | medium |
| Next | U5 settings panel with peers and status | medium |
| Later | U6 optional pack encryption or signing | medium |
| Later | U7 split or delete the HTTP transport (gated on U2) | hard |

Verdict: keep standalone, and shrink. `helpers/packs.py` is the unique part; the transport is the deletable part.

## Earlier work

0.1.0 shipped from [docs/plans/2026-10-08-001-feat-device-sync-plan.md](docs/plans/2026-10-08-001-feat-device-sync-plan.md) (Khan#195 extraction).
