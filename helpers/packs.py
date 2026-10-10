"""Sync pack machinery — ported from Khan ``helpers/device_sync.py``.

Builds and validates three pack types:

- **settings pack** — the on-disk settings shape minus every secret-bearing
  key. Import overlays non-sensitive keys onto local settings; secret keys
  are never written (locally-owned always wins).
- **chats pack** — a ZIP: ``manifest.json`` + ``chats/<ctxid>.json`` +
  ``kurultai/chats.ndjson`` (chat logs flattened into connector atoms).
- **memory pack** — NDJSON, one atom dict per line; the backend that
  produces/consumes atoms lives in ``memory_backend.py``.

Pack ``format`` tags keep the ``khan-`` prefix — wire compatibility with
existing Khan packs is a feature (a Khan box and an a0 box can sync).

Host seams are imported lazily inside the functions that use them so the
module imports cleanly in tests (conftest stubs ``helpers.settings``,
``helpers.persist_chat``, ``agent``) and a broken host import can never
abort a0's plugin/extension sweep.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SETTINGS_PACK_VERSION = 1
CHATS_PACK_VERSION = 1
SETTINGS_PACK_FORMAT = "khan-settings"
CHATS_PACK_FORMAT = "khan-chats"
KURULTAI_INBOX_REL = "usr/kurultai-inbox/chats"

# Keys that never leave the box and are never overwritten by an import —
# credentials, secret blobs, and capability pivots (settings that make the
# host DO something: spawn MCP stdio servers, pivot filesystem roots,
# inject prompt variables, redirect model traffic).
SENSITIVE_SETTINGS_KEYS = (
    "api_keys",
    "auth_login",
    "auth_password",
    "rfc_password",
    "root_password",
    "mcp_server_token",
    "mcp_servers",          # RCE sink: import -> MCPConfig.update spawns stdio commands
    "litellm_global_kwargs",  # merged into every model call: api_base/api_key hijack
    "variables",            # rendered into the system prompt -> injection
    "agent_profile",        # selects prompt/profile set on deserialize
    "agent_knowledge_subdir",
    "secrets",
)

# Key-shape rules: these classes carry capabilities, not preferences.
# Pattern-matched so NEW dangerous keys a0 adds fail closed instead of
# silently syncing.
_SENSITIVE_SUFFIXES = (
    "_path",
    "_dir",
    "_subdir",
    "_url",
    "_uri",
    "_addr",
    "_cmd",
    "_command",
    "_kwargs",   # *_model_kwargs can carry api_base/api_key overrides
    "_servers",
    "_token",
    "_password",
    "_secret",
    "_key",
    "_keys",
    "_headers",
    "_server_enabled",
)
_SENSITIVE_PREFIXES = ("rfc_", "a2a_")


def is_sensitive_key(key: Any) -> bool:
    """True when a settings key carries a credential or a capability —
    excluded from export, dropped on import, skipped by the conflict diff."""
    k = str(key).lower()
    return (
        k in SENSITIVE_SETTINGS_KEYS
        or k.startswith(_SENSITIVE_PREFIXES)
        or k.endswith(_SENSITIVE_SUFFIXES)
    )

# Continuity pack limits (authenticated callers still get bounded processing)
MAX_PACK_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_ZIP_ENTRIES = 5000
MAX_ZIP_ENTRY_UNCOMPRESSED = 20 * 1024 * 1024
MAX_ZIP_TOTAL_UNCOMPRESSED = 200 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 100.0
REQUIRED_ATOM_FIELDS = ("id", "title", "content", "tags")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _blank(value: Any) -> Any:
    """Type-preserving empty value for a stripped setting."""
    if isinstance(value, dict):
        return {}
    if isinstance(value, list):
        return []
    return ""


def strip_sensitive_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Blank sensitive/capability keys in place-value-preserving form —
    the pack keeps the key name so diffs see its presence, never its
    value."""
    cleaned = dict(data)
    for key, value in list(cleaned.items()):
        if is_sensitive_key(key):
            cleaned[key] = _blank(value)
    for key in SENSITIVE_SETTINGS_KEYS:
        if key not in cleaned:
            cleaned[key] = _blank(None)
    return cleaned


def build_settings_pack() -> dict[str, Any]:
    """Secret-free prefs snapshot (same shape as on-disk settings.json)."""
    from helpers import settings

    current = settings.get_settings()
    prefs = strip_sensitive_settings(dict(current))
    return {
        "format": SETTINGS_PACK_FORMAT,
        "version": SETTINGS_PACK_VERSION,
        "exported_at": utc_now_iso(),
        "settings": prefs,
    }


def import_settings_pack(pack: dict[str, Any]) -> Any:
    """Overlay pack prefs onto current settings; never touch secrets stores."""
    from helpers import settings

    if not isinstance(pack, dict):
        raise ValueError("Settings pack must be a JSON object")
    if pack.get("format") != SETTINGS_PACK_FORMAT:
        raise ValueError(f"Unsupported settings pack format: {pack.get('format')!r}")
    version = pack.get("version")
    # strict int compare — `int(1.9)` truncating to a pass is a compat hole
    if not isinstance(version, int) or isinstance(version, bool) or version != SETTINGS_PACK_VERSION:
        raise ValueError(f"Unsupported settings pack version: {version!r}")
    incoming = pack.get("settings")
    if not isinstance(incoming, dict):
        raise ValueError("Settings pack missing 'settings' object")

    current = settings.get_settings()
    overlay = {k: v for k, v in incoming.items() if not is_sensitive_key(k)}

    merged = {**current, **overlay}
    for key in current:
        if is_sensitive_key(key):
            merged[key] = current[key]  # local values always win

    return settings.set_settings(merged)  # type: ignore[arg-type]


def _chat_title(data: dict[str, Any]) -> str:
    name = (data.get("name") or "").strip()
    if name:
        return name
    return f"chat-{data.get('id') or 'unknown'}"


def chat_json_to_kurultai_atoms(chat_json: str | dict[str, Any]) -> list[dict[str, Any]]:
    """Map one exported chat JSON into kurultai json-connector records."""
    data = json.loads(chat_json) if isinstance(chat_json, str) else chat_json
    chat_id = str(data.get("id") or "unknown")
    title = _chat_title(data)
    atoms: list[dict[str, Any]] = []

    logs = (data.get("log") or {}).get("logs") or []
    for index, item in enumerate(logs):
        if not isinstance(item, dict):
            continue
        content = (item.get("content") or "").strip()
        heading = (item.get("heading") or "").strip()
        if not content and not heading:
            continue
        msg_type = str(item.get("type") or "message")
        body_parts = [p for p in (heading, content) if p]
        body = "\n\n".join(body_parts)
        atom_id = str(item.get("id") or f"khan-chat-{chat_id}-log-{index}")
        atoms.append(
            {
                "id": atom_id,
                "title": f"{title} ({msg_type})",
                "content": body,
                "tags": ["khan", "chat", msg_type],
                "chat_id": chat_id,
                "chat_title": title,
                "message_type": msg_type,
            }
        )

    if atoms:
        return atoms

    atoms.append(
        {
            "id": f"khan-chat-{chat_id}",
            "title": title,
            "content": json.dumps(data, ensure_ascii=False)[:50000],
            "tags": ["khan", "chat"],
            "chat_id": chat_id,
            "chat_title": title,
        }
    )
    return atoms


def build_chats_ndjson(chat_jsons: list[str]) -> str:
    lines: list[str] = []
    for chat_js in chat_jsons:
        for atom in chat_json_to_kurultai_atoms(chat_js):
            lines.append(json.dumps(atom, ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")


def validate_kurultai_ndjson(ndjson: str) -> int:
    """Parse every non-blank line as a Kurultai atom. Returns atom count."""
    count = 0
    for line_no, raw in enumerate(ndjson.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            atom = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid NDJSON on line {line_no}: {exc}") from exc
        if not isinstance(atom, dict):
            raise ValueError(f"NDJSON line {line_no} must be a JSON object")
        for field in REQUIRED_ATOM_FIELDS:
            if field not in atom:
                raise ValueError(f"NDJSON line {line_no} missing required field {field!r}")
        tags = atom.get("tags")
        if not isinstance(tags, list) or not tags:
            raise ValueError(f"NDJSON line {line_no} requires non-empty tags list")
        count += 1
    if count == 0:
        raise ValueError("No kurultai atoms to write")
    return count


def collect_exportable_chat_jsons(ctxids: list[str] | None = None) -> list[tuple[str, str]]:
    """Return [(ctxid, json_string), ...] for USER chats only — BACKGROUND
    housekeeping and TASK (subagent) chats stay local: a task's transcript
    can carry delegated work the owner never reviewed."""
    from agent import AgentContext, AgentContextType
    from helpers import persist_chat

    persist_chat.save_tmp_chats()
    wanted = set(ctxids) if ctxids else None
    out: list[tuple[str, str]] = []
    for context in AgentContext.all():
        if context.type != AgentContextType.USER:
            continue
        if wanted is not None and context.id not in wanted:
            continue
        out.append((context.id, persist_chat.export_json_chat(context)))
    return out


def build_chats_zip_bytes(ctxids: list[str] | None = None) -> tuple[bytes, dict[str, Any]]:
    pairs = collect_exportable_chat_jsons(ctxids)
    chat_jsons = [js for _, js in pairs]
    ndjson = build_chats_ndjson(chat_jsons)
    manifest = {
        "format": CHATS_PACK_FORMAT,
        "version": CHATS_PACK_VERSION,
        "exported_at": utc_now_iso(),
        "chat_count": len(pairs),
        "chat_ids": [cid for cid, _ in pairs],
        "kurultai_atoms": ndjson.count("\n") if ndjson else 0,
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for ctxid, js in pairs:
            zf.writestr(f"chats/{_safe_chat_name(ctxid)}.json", js)
        zf.writestr("kurultai/chats.ndjson", ndjson)
    return buffer.getvalue(), manifest


def _safe_chat_name(ctxid: str) -> str:
    """Sanitize a ctxid for a zip member name; append a short hash so that
    ctxids colliding after sanitization ('a:b' vs 'a_b') produce distinct
    members — otherwise the pack fails its own count check on import."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in ctxid)
    if safe != ctxid:
        safe = f"{safe}-{hashlib.sha256(ctxid.encode()).hexdigest()[:8]}"
    return safe or "chat"


def build_memory_pack(atoms: list[dict[str, Any]]) -> bytes:
    """NDJSON memory pack — one atom dict per line, canonical: atoms sorted
    by id and keys sorted, so the same set serializes identically on any
    host (byte-diffable in the git backend)."""
    valid = [a for a in atoms if isinstance(a, dict) and a.get("id")]
    valid.sort(key=lambda a: str(a["id"]))
    lines = [json.dumps(a, ensure_ascii=False, sort_keys=True) for a in valid]
    text = "\n".join(lines)
    return (text + "\n").encode("utf-8") if text else b""


def iter_memory_pack(ndjson_data: bytes):
    """Yield atom dicts from an NDJSON memory pack. Raises ValueError on a
    malformed line (line-numbered, like the kurultai validator). Streams
    one line at a time — a 50MB pack never triples in memory."""
    for line_no, raw_bytes in enumerate(io.BytesIO(ndjson_data), start=1):
        line = raw_bytes.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"memory pack invalid NDJSON on line {line_no}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"memory pack line {line_no} is not a JSON object")
        yield data


def assert_upload_size(raw: bytes, *, label: str = "upload") -> None:
    if len(raw) > MAX_PACK_UPLOAD_BYTES:
        raise ValueError(
            f"{label} exceeds {MAX_PACK_UPLOAD_BYTES} byte limit ({len(raw)} bytes)"
        )


def _assert_zip_bounds(zf: zipfile.ZipFile) -> None:
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ValueError(f"ZIP has too many entries ({len(infos)} > {MAX_ZIP_ENTRIES})")
    total_uncompressed = 0
    for info in infos:
        if info.file_size > MAX_ZIP_ENTRY_UNCOMPRESSED:
            raise ValueError(
                f"ZIP entry {info.filename!r} uncompressed size "
                f"{info.file_size} exceeds {MAX_ZIP_ENTRY_UNCOMPRESSED}"
            )
        total_uncompressed += info.file_size
        if (
            info.compress_size > 0
            and info.file_size > 1024 * 1024
            and info.file_size / info.compress_size > MAX_ZIP_COMPRESSION_RATIO
        ):
            raise ValueError(
                f"ZIP entry {info.filename!r} compression ratio "
                f"{info.file_size / info.compress_size:.1f} exceeds "
                f"{MAX_ZIP_COMPRESSION_RATIO}"
            )
    if total_uncompressed > MAX_ZIP_TOTAL_UNCOMPRESSED:
        raise ValueError(
            f"ZIP total uncompressed size {total_uncompressed} exceeds "
            f"{MAX_ZIP_TOTAL_UNCOMPRESSED}"
        )


def extract_chats_from_zip(zip_bytes: bytes) -> tuple[dict[str, Any], list[str], str]:
    """Return (manifest, chat_json_strings, ndjson_text). Entries are read
    to memory only — member names never reach a filesystem path."""
    assert_upload_size(zip_bytes, label="chats pack")
    try:
        zf_ctx = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"chats pack is not a ZIP: {exc}") from exc
    with zf_ctx as zf:
        _assert_zip_bounds(zf)
        names = set(zf.namelist())
        if "manifest.json" not in names:
            raise ValueError("Chats pack missing required manifest.json")
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        if manifest.get("format") != "khan-chats":
            raise ValueError(f"Unsupported chats pack format: {manifest.get('format')!r}")
        if "version" not in manifest:
            raise ValueError("Chats pack missing version")
        if int(manifest["version"]) != CHATS_PACK_VERSION:
            raise ValueError(f"Unsupported chats pack version: {manifest.get('version')}")

        chat_jsons: list[str] = []
        for name in sorted(names):
            if name.startswith("chats/") and name.endswith(".json"):
                chat_jsons.append(zf.read(name).decode("utf-8"))

        declared = manifest.get("chat_count")
        if declared is not None and int(declared) != len(chat_jsons):
            raise ValueError(
                f"manifest chat_count {declared} does not match chats/*.json count "
                f"{len(chat_jsons)}"
            )

        if "kurultai/chats.ndjson" in names:
            ndjson = zf.read("kurultai/chats.ndjson").decode("utf-8")
        elif chat_jsons:
            ndjson = build_chats_ndjson(chat_jsons)
        else:
            ndjson = ""

    return manifest, chat_jsons, ndjson


# ctxid allowlist — the id crosses the plugin->host trust boundary: it
# becomes a filesystem path in get_chat_folder_path()/save_tmp_chat() and a
# delete_dir() target in remove_chat(). a0 ids are 8-char alnum; -_ allowed
# for other sources. Anything else (/, ., \0, traversal) is skipped, not
# sanitized — a pack chat with a hostile id is rejected whole.
_CHAT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def _scrub_agent_profile(data: dict[str, Any]) -> None:
    """Drop agent_profile from an imported chat — _deserialize_context and
    _deserialize_agent_config feed it to initialize_agent(override_settings=
    {"agent_profile": ...}), and an attacker-chosen profile is a settings/
    prompt injection across the trust boundary (the same reason it's a
    SENSITIVE_SETTINGS_KEYS entry). Imported chats land on the default
    profile."""
    data.pop("agent_profile", None)
    agents = data.get("agents")
    if isinstance(agents, list):
        for ag in agents:
            if isinstance(ag, dict):
                ag.pop("agent_profile", None)


def import_chat_jsons(chat_jsons: list[str]) -> list[str]:
    """Import chat JSONs with real ctxid dedupe — returns imported ctxids.

    ``persist_chat.load_json_chats`` deletes ``id`` and mints a fresh
    ctxid per chat — a0's import is a COPY, not a sync. Naively calling it
    duplicates every chat each round, and worse: the copies re-export
    under their new ids, so A→B→A echo grows forever. We therefore parse
    each pack chat's original ``id``, skip ids the host already knows
    (live ``AgentContext.all()`` ∪ persisted ``saved_chat_ids()``), and
    deserialize the novel ones with the original id preserved
    (``_deserialize_context`` honors ``data["id"]`` — never re-feed a
    known id: constructing a context over a live id kills its task).

    An empty list is a successful no-op — a zero-chat peer produces a
    valid pack."""
    if not chat_jsons:
        return []
    from agent import AgentContext
    from helpers import persist_chat

    known = {c.id for c in AgentContext.all()}
    try:
        known |= set(persist_chat.saved_chat_ids())
    except Exception:
        pass

    # parse once; only novel, well-formed, id-bearing chats proceed
    novel: list[tuple[str, dict[str, Any]]] = []
    for js in chat_jsons:
        try:
            data = json.loads(js)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        cid = str(data.get("id") or "")
        if not _CHAT_ID_RE.fullmatch(cid):
            continue  # id becomes a filesystem path downstream — allowlist or skip
        if cid not in known:
            _scrub_agent_profile(data)
            novel.append((js, data))
            known.add(cid)

    deserialize = getattr(persist_chat, "_deserialize_context", None)
    if deserialize is None:
        # Host moved the seam — copy-import still dedupes by ORIGINAL id
        # (fresh local ids, but the same pack never re-imports).
        return persist_chat.load_json_chats([js for js, _ in novel])

    imported: list[str] = []
    for _js, data in novel:
        try:
            ctx = deserialize(data)
        except Exception:
            continue  # one malformed chat must not fail the pack
        imported.append(ctx.id)
        try:
            persist_chat.save_tmp_chat(ctx)
        except Exception:
            pass
    return imported


def kurultai_inbox_dir() -> str:
    from helpers import files

    path = files.get_abs_path(KURULTAI_INBOX_REL)
    os.makedirs(path, exist_ok=True)
    return path


def write_kurultai_ndjson(ndjson: str, filename: str | None = None) -> str:
    """Atomically drop a validated NDJSON file into the kurultai inbox —
    the json-connector indexing path. mkstemp gives 0600 + O_EXCL-unique
    names; nothing else links the file, so no retry loop is needed."""
    validate_kurultai_ndjson(ndjson)
    inbox = kurultai_inbox_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = Path(filename).name if filename else f"{CHATS_PACK_FORMAT}-{stamp}"
    prefix = Path(prefix).stem
    prefix = "".join(c if c.isalnum() or c in "-_" else "_" for c in prefix) or "khan-chats"

    payload = ndjson if ndjson.endswith("\n") else ndjson + "\n"
    # tmp + os.replace: a scanning consumer must never see a partial file.
    fd, tmp = tempfile.mkstemp(dir=inbox, prefix=f".{prefix}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        import secrets

        dest = os.path.join(inbox, f"{prefix}-{secrets.token_hex(4)}.ndjson")
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def build_chats_zip_file(path: str, ctxids: list[str] | None = None) -> dict[str, Any]:
    """Stream the chats pack ZIP straight to a temp file — avoids holding
    the full archive in memory alongside every chat JSON."""
    pairs = collect_exportable_chat_jsons(ctxids)
    chat_jsons = [js for _, js in pairs]
    ndjson = build_chats_ndjson(chat_jsons)
    manifest = {
        "format": CHATS_PACK_FORMAT,
        "version": CHATS_PACK_VERSION,
        "exported_at": utc_now_iso(),
        "chat_count": len(pairs),
        "chat_ids": [cid for cid, _ in pairs],
        "kurultai_atoms": ndjson.count("\n") if ndjson else 0,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for ctxid, js in pairs:
            zf.writestr(f"chats/{_safe_chat_name(ctxid)}.json", js)
        zf.writestr("kurultai/chats.ndjson", ndjson)
    return manifest


def send_temp_file(
    path: str,
    *,
    download_name: str,
    mimetype: str,
):
    """send_file a temp path and unlink it after the response finishes."""
    from helpers.api import send_file

    try:
        response = send_file(
            path,
            as_attachment=True,
            download_name=download_name,
            mimetype=mimetype,
        )
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise

    def _cleanup() -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    response.call_on_close(_cleanup)
    return response
