# Landscape: the Agent Zero plugin ecosystem and comparable agent-plugin systems

Date: 2026-10-10. Shared by `a0-plugin-durable`, `a0-plugin-glitchtip`, `a0-plugin-notion`, `a0-plugin-device-sync`, `a0-plugin-jev-compact`. Each repo's per-plugin section follows this shared text. Research used web search only; no paid model calls. Search results were mostly third-party mirrors, so every claim is tagged with how solid its source is. Where a source disagreed or was thin, that is said.

## 1. How Agent Zero plugins work (the host contract)

- Agent Zero looks for plugins in `usr/plugins/<name>/` (user) and `plugins/<name>/` (core). A folder is a plugin when it has a `plugin.yaml` at its root. Fields include `name`, `title`, `description`, `version`, `settings_sections` and per-project/per-agent flags. Valid `settings_sections` values reported: agent, external, mcp, developer, backup. Source: third-party mirrors of Agent Zero's own skill docs ([tessl a0-create-plugin](https://tessl.io/registry/skills/github/agent0ai/agent-zero/a0-create-plugin), [openskillindex a0-create-plugin](https://openskillindex.com/skills/agent0ai-agent-zero-a0-create-plugin)). Confidence: medium. The official repo was not read directly.
- Community plugins live in their own GitHub repo and are listed by a pull request to the `a0-plugins` index repo. The index is a generated `index.json` keyed by plugin name (title, description, github, tags, thumbnail). A `LICENSE` at the repo root is required, and CI requires `name` to match `^[a-z0-9_]+$`. Source: [openskillindex a0-contribute-plugin](https://openskillindex.com/skills/agent0ai-agent-zero-a0-contribute-plugin) and the mirrors above. Confidence: medium.
- Implication for this family: the `a0-plugins` index is the only discovery channel, and its card is a title, a description, tags and a thumbnail. A good thumbnail (our social card or mark) and a plain description do more for discovery than any feature. Our own plugins carry a `LICENSE` already; `a0-plugin-jev-compact` is the only one with `author`, `license`, `homepage` and `min_a0_version` in its manifest.

## 2. Comparable plugin systems

| System | Unit of extension | Distribution | What it does better than an a0 plugin | What it does worse |
|---|---|---|---|---|
| Claude Code plugins | Directory with `.claude-plugin/plugin.json`; bundles slash commands, subagents, hooks, skills, MCP servers ([guide](https://www.morphllm.com/claude-code-marketplace), third-party) | A git repo with `.claude-plugin/marketplace.json`; `/plugin marketplace add owner/repo`, `/plugin install` | Install is one command, from any git repo. Components are declarative (no Python to run). | Not a Python runtime: plugins cannot hold long-lived server code except via MCP. |
| Model Context Protocol (MCP) servers | A process speaking MCP over stdio or HTTP | Registries and per-client config | Client-neutral: one server works in many agents. Notion ships an official hosted one ([Notion docs](https://developers.notion.com/docs/hosting-open-source-mcp), [ToolHive guide](https://docs.stacklok.com/toolhive/guides-mcp/notion-remote)). | Every server is a separate process with its own auth and lifecycle. |
| Agent Zero plugins | Directory with `plugin.yaml`, `api/`, `extensions/`, `helpers/`, optional webui | One GitHub repo each + PR to `a0-plugins` index | Can hook the agent loop in-process (extensions), add API routes and settings panels, and ship an MCP server inside. | Discovery is one index; no versioned install command was found in the sources; hosts are Python-only. |

What users expect from a plugin in any of these systems: install in one step, a visible settings surface, a no-op default when disabled, a clear failure message instead of a crash at host startup, and a licence file.

## 3. Cross-cutting findings

1. **MCP is the neutral layer.** Where a capability already exists as an MCP server, a plugin that re-implements it competes with the vendor. A plugin earns its place by doing what MCP cannot: hooking the agent loop or host lifecycle in-process.
2. **Zero-dependency is a real differentiator in a host plugin.** All five plugins here are stdlib-only or lazy-import optional deps, so they cannot break a0 startup. That is a design choice worth keeping and stating in each README.
3. **Disabled-by-default and inert-when-off** is the convention that makes a plugin safe to install. Keep it.
4. **Discovery needs a card.** The `a0-plugins` index shows title, description, tags, thumbnail. Per-plugin tags and a thumbnail are cheap.

## 4. Adjacent domains (used by the per-plugin sections)

- **Durable execution** (for durable): Temporal records an event history and replays deterministic workflow code; Inngest checkpoints each `step.run()` and charges per run and per step, with a BSL self-host option; DBOS is MIT, database-native, TypeScript-centric; Restate is a single binary with sub-50ms latency claims from vendor pieces; Hatchet, Trigger.dev and LittleHorse also position as agent orchestrators. Sources are vendor-authored and flagged as directional by the search: [ZenML on Inngest alternatives](https://www.zenml.io/blog/inngest-alternatives), [Diagrid FAQ](https://www.diagrid.io/faq/alternatives-dbos-inngest/what-are-the-underappreciated-integration-costs-when-self-building-a-durable-execution-layer-on-temporal-c), [Spheron comparison](https://www.spheron.network/blog/ai-agent-workflow-orchestration-temporal-inngest-restate-gpu-cloud/), [noqta](https://www.noqta.tn/blog/durable-execution-ai-agents-inngest-trigger-temporal-2026).
- **Error tracking** (for glitchtip): GlitchTip accepts Sentry SDKs by changing one DSN string, self-hosts on roughly three services (app, Postgres, Valkey/Redis), and added incremental OpenTelemetry support and span views in 6.2, with an MCP server for AI-assisted debugging ([GlitchTip 6.2 release](https://glitchtip.com/blog/2026-06-22-glitchtip-6-2-released), [SDK docs](https://glitchtip.com/sdkdocs/all-sdks), [Sentry alternatives roundup](https://oneuptime.com/blog/post/2026-03-31-10-best-sentry-alternatives/markdown)). A competitor's roundup says it focuses on error and uptime, not full-stack observability. Langfuse trace-id correlation was not found in any source.
- **Notion access** (for notion): Notion runs an official hosted MCP server at `mcp.notion.com/mcp` (tools such as `notion-search`, `notion-fetch`, `notion-create-pages`); the open-source `makenotion/notion-mcp-server` is described as lower priority and possibly deprecated by two third-party sources. Sources disagree on whether the hosted server is OAuth-only. Verify on Notion's docs before acting: [StackOne deep dive](https://stackone.com/blog/notion-mcp-deep-dive), [ToolHive](https://docs.stacklok.com/toolhive/guides-mcp/notion-remote), [x-cmd](https://x-cmd.com/install/notion-mcp-server).
- **Device sync** (for device-sync): Syncthing is continuous two-way folder sync that needs both devices online and a daemon; Taildrop is ad hoc file transfer over a tailnet, not sync; rsync is one-way and has no memory between runs; Tailscale carries any of them. Sources are community posts and low quality: [Syncthing vs Tailscale thread](https://jisaku.com/glossary/network-nas-sync-tailscale-syncthing-remote), [rsync alternatives](https://alternativeto.net/software/rsync), [Taildrop forum](https://forum.tailscale.com/t/taildrop-alpha-futures-speculation/689).
- **Context compaction** (for jev-compact): Claude Code compacts manually with `/compact` and automatically near full context (reported near 95% of a 200K window); one critique says that timing degrades context first. A 2026 guide groups methods as LLM summarisation, opaque compression, and verbatim compaction (drop low-value lines, keep the rest word for word) and warns that 90%+ compression forces rewriting and hallucination risk. Mem0 markets a memory compression engine (vendor claim of up to 80% fewer prompt tokens, treat as marketing). Letta (MemGPT) pages data in and out like virtual memory. LLMLingua is token pruning. Sources: [morphllm guide](https://www.morphllm.com/context-compression), [Claude Code compaction explainer](https://techie007.substack.com/p/how-memory-compaction-works-in-agents), [Mem0 guide](https://guide.mem0.ai/faq/tools-reduce-context-llm-calls), [Self-Compacting Language Model Agents](https://arxiv.org/pdf/2606.23525).

## 5. What is unique about this family

- In-process agent-loop hooks with a stdlib-only footprint, which neither MCP servers nor marketplace plugins offer.
- A shared engineering standard across five repos: opt-in, inert when disabled, secrets never in config, `{ok, error}` API envelope, offline tests.
- Honest scale: these are single-host tools for one operator's Agent Zero boxes. None claims multi-tenant or multi-region.

## 6. Open risks to the whole family

- Host-contract drift: every claim in section 1 comes from third-party mirrors. Re-verify against the official `agent0ai/agent-zero` repo before the next release of any plugin.
- Five repos, five CI files and five near-identical `helpers/config.py`/`runtime.py` pairs. A shared helper package is the obvious simplification, but a0 plugins are standalone directories, so sharing means a vendored copy or a 6th plugin. See each ROADMAP.

## Plugin section: device-sync

Scope of this section: could existing tools deliver most of what `a0-plugin-device-sync` does (about 2.6k lines of plugin code plus 1.9k lines of tests, 0.1.0), and what is left that only a plugin can do. Research used web fetches of vendor docs on 2026-10-10. No source was found that describes Agent Zero's on-disk chat or memory layout, so claims about "just sync the folder" are inferences and are marked as such.

### What the plugin does (from the code)

Three pack types (settings JSON, chats ZIP, memory NDJSON atoms) move between a0 boxes over HTTP on a0's own port, behind one shared bearer token. Peers come from `tailscale status --json` plus `~/.a0-device-sync/peers.json`, and are identity-probed with an HMAC challenge. Settings are scrubbed of secrets and capability keys (MCP specs, paths, URLs) on export and again on import. Chat import dedupes by original context id inside a running a0. Optional git-backed memory store. Optional background loop (60 s floor).

### Alternatives and comparables

| Project | What it does | Better than the plugin | Worse than the plugin | Source |
|---|---|---|---|---|
| Syncthing | Continuous sync of shared folders between devices; devices connect only when each is configured with the other's device ID (an ID is part of a public key) | Mature, many years of use, no custom wire format to maintain, works with any file. Conflicts are handled: the older copy is renamed to `.sync-conflict-<date>-<time>-<modifiedBy>` | Knows nothing about a0: it moves files, not "settings without secrets", and a running a0 may not reload files that change on disk (inference, not verified). Does not scrub secrets. Needs a daemon on every box | [getting started](https://docs.syncthing.net/intro/getting-started.html), [syncing and conflicts](https://docs.syncthing.net/users/syncing.html) |
| Agent Zero built-in Backup and Restore | Settings, Backup & Restore in the a0 UI; the guide says it covers chats, projects, knowledge, memory, settings, skills and workspace files, and that secrets may not always be included | First-party, no extra install, covers more than the plugin (projects, skills, workspace) | Manual and whole-instance: no peer discovery, no scheduled sync, no merge or id-level dedupe (the guide does not describe one). Archive format not specified in the source | [a0 usage guide](https://raw.githubusercontent.com/agent0ai/agent-zero/main/docs/guides/usage.md) |
| Taildrop (Tailscale) | Send files between your own devices over encrypted peer-to-peer connections | Zero config on a tailnet, nothing to run besides Tailscale | Alpha; needs "Send Files" enabled; own devices only; no tagged nodes; Linux received files belong to root; it is transfer, not sync. Could carry an exported pack by hand | [Taildrop docs](https://tailscale.com/kb/1106/taildrop) |
| Tailscale SSH plus rsync | SSH between tailnet nodes using tailnet identity and ACLs instead of SSH keys; revoking policy ends sessions | Identity and revocation come from the tailnet ACL, so there is no shared secret to rotate or leak. Standard tools, well understood | rsync is one-way and has no memory between runs (landscape section 4, low-quality source); no knowledge of a0 settings, secrets or chat ids | [Tailscale SSH](https://tailscale.com/kb/1193/tailscale-ssh) |
| chezmoi | Manages config files across machines from a git repo; integrates password managers and supports age/gpg encryption; templates per machine | The best-known answer to "same config, many machines, secrets kept out of plaintext". Templates handle per-machine differences, which the plugin handles only by dropping keys | Files only, no chats or live memory; pull-based and manual | [chezmoi](https://www.chezmoi.io/) |
| restic | Single-binary encrypted, deduplicated backup to many storage backends, built so restores can be verified | Strong encryption and verification of what was stored, which the plugin lacks (README: no pack encryption) | A backup tool, not a sync tool; no merge into a live instance | [restic](https://restic.net/) |
| git (already inside the plugin) | Optional `memory_backend: git` makes the atom directory a repo | Free history, diff and revert | Only used for memory atoms today | repo `helpers/memory_backend.py` |

Source quality: the first five rows are vendor or first-party docs fetched directly. The Syncthing page did not state encryption details or whether devices must be online at the same time, so neither is claimed here.

### What users expect

- Install and forget: a sync tool that needs a token, a port and a peers file is more setup than Syncthing's device-ID exchange.
- A visible state: which peers, last sync, what conflicted. The plugin exposes this only through POST endpoints; there is no settings panel code in the repo (`settings_sections: external` is declared, no webui).
- No secret leakage: users of dotfile and backup tools expect secrets to stay out of the transported data or to be encrypted. The plugin scrubs but does not encrypt.

### What is unique here

1. A secret-free settings pack with capability-key scrubbing on import (`mcp_servers`, `*_path`, `*_url` and similar). None of the tools above do this, because none know what an a0 settings overlay can do.
2. Chat import that preserves the original context id and skips ids already live or saved, so A to B to A does not grow. Stock `load_json_chats` mints fresh ids (per the repo's AGENTS.md; not independently verified against a0 source).
3. A running-instance import path: it changes the live a0, not just files on disk.
4. Wire compatibility with Khan packs (`khan-*` format tags).

### Honest assessment: would Syncthing or rsync plus exports deliver most of the value?

For one operator with two or three boxes, probably yes for about two thirds of it. Syncthing on the chats and memory folders, plus a manual settings export (the plugin's own `settings_export` or a0 Backup and Restore), covers chats and memory without any of the peer discovery, probing, token and loop code. This is an inference: it depends on a0 reading those folders after start, which no source here confirms. What those tools cannot do is the scrub-and-merge of settings and id-level chat dedupe into a live instance. That is `helpers/packs.py` (593 lines) and a small part of `helpers/sync.py`.

What could be deleted if the transport were handed to Syncthing, Taildrop or SSH: `helpers/sync.py` peer discovery, identity probe and HTTP client (the bulk of 970 lines), the auto-sync loop and its lifecycle locks, `api/peers.py`, `api/sync_now.py`, `helpers/auth.py` (136 lines, only needed because of the HTTP endpoints), and the `git` memory backend (Syncthing or git themselves cover it). The pack build and import code would remain, driven by file in, file out.

### Security notes (documentation only; no code was changed)

These are observations from reading the code, not tested attacks.

1. One shared bearer token for every peer. Compromise of any box exposes the token for all, and there is no rotation or per-peer revocation. Tailscale SSH shows the alternative (identity and ACL based).
2. Default `peer_port` is 80, plain HTTP. On a tailnet the transport is WireGuard-encrypted, but a peers-file entry on a non-tailnet host would carry the bearer token and packs in clear text.
3. Residual risk, also noted in AGENTS.md: the HMAC proof shows a peer shares the token, not that it is the claimed host. A relay through a real peer can answer the challenge.
4. A trusted peer can still change behaviour. Scrubbing removes secrets and capability keys, but a settings pack can change other keys (models, prompts), and imported chats and memory atoms enter the agent's context. A compromised peer is therefore a prompt-injection channel. Treat all peers as fully trusted.
5. The token authorises `sync_now`, which makes this box call out to every discovered peer. It is a trigger with a side effect, not only a read.
6. No pack encryption or signature; integrity rests on the transport and the token.
7. The old README described the identity probe as a static 403 check. The code now uses an HMAC nonce challenge; the README was updated in this change.
