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

import io
import json
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SETTINGS_PACK_VERSION = 1
CHATS_PACK_VERSION = 1
MEMORY_PACK_VERSION = 1
SETTINGS_PACK_FORMAT = "khan-settings"
CHATS_PACK_FORMAT = "khan-chats"
KURULTAI_INBOX_REL = "usr/kurultai-inbox/chats"

# Keys that never leave the box and are never overwritten by an import.
SENSITIVE_SETTINGS_KEYS = (
    "api_keys",
    "auth_login",
    "auth_password",
    "rfc_password",
    "root_password",
    "mcp_server_token",
    "secrets",
)

# Continuity pack limits (authenticated callers still get bounded processing)
MAX_PACK_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_ZIP_ENTRIES = 5000
MAX_ZIP_ENTRY_UNCOMPRESSED = 20 * 1024 * 1024
MAX_ZIP_TOTAL_UNCOMPRESSED = 200 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 100.0
REQUIRED_ATOM_FIELDS = ("id", "title", "content", "tags")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def strip_sensitive_settings(data: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(data)
    for key in SENSITIVE_SETTINGS_KEYS:
        if key == "api_keys":
            cleaned[key] = {}
        else:
            cleaned[key] = ""
    return cleaned


def build_settings_pack() -> dict[str, Any]:
    """Secret-free prefs snapshot (same shape as on-disk settings.json)."""
    from helpers import settings

    current = settings.get_settings()
    prefs = strip_sensitive_settings(dict(current))
    return {
        "format": SETTINGS_PACK_FORMAT,
        "version": SETTINGS_PACK_VERSION,
        "exported_at": _utc_now_iso(),
        "settings": prefs,
    }


def import_settings_pack(pack: dict[str, Any]) -> Any:
    """Overlay pack prefs onto current settings; never touch secrets stores."""
    from helpers import settings

    if not isinstance(pack, dict):
        raise ValueError("Settings pack must be a JSON object")
    if pack.get("format") != SETTINGS_PACK_FORMAT:
        raise ValueError(f"Unsupported settings pack format: {pack.get('format')!r}")
    if "version" not in pack:
        raise ValueError("Settings pack missing version")
    if int(pack["version"]) != SETTINGS_PACK_VERSION:
        raise ValueError(f"Unsupported settings pack version: {pack.get('version')}")
    incoming = pack.get("settings")
    if not isinstance(incoming, dict):
        raise ValueError("Settings pack missing 'settings' object")

    current = settings.get_settings()
    overlay = {k: v for k, v in incoming.items() if k not in SENSITIVE_SETTINGS_KEYS}

    merged = {**current, **overlay}
    for key in SENSITIVE_SETTINGS_KEYS:
        merged[key] = current.get(key, {} if key == "api_keys" else "")

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
    """Return [(ctxid, json_string), ...] for USER chats (not BACKGROUND)."""
    from agent import AgentContext, AgentContextType
    from helpers import persist_chat

    persist_chat.save_tmp_chats()
    wanted = set(ctxids) if ctxids else None
    out: list[tuple[str, str]] = []
    for context in AgentContext.all():
        if context.type == AgentContextType.BACKGROUND:
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
        "exported_at": _utc_now_iso(),
        "chat_count": len(pairs),
        "chat_ids": [cid for cid, _ in pairs],
        "kurultai_atoms": ndjson.count("\n") if ndjson else 0,
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for ctxid, js in pairs:
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in ctxid)
            zf.writestr(f"chats/{safe}.json", js)
        zf.writestr("kurultai/chats.ndjson", ndjson)
    return buffer.getvalue(), manifest


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
    """Return (manifest, chat_json_strings, ndjson_text)."""
    assert_upload_size(zip_bytes, label="chats pack")
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
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


def import_chat_jsons(chat_jsons: list[str]) -> list[str]:
    if not chat_jsons:
        raise ValueError("No chats to import")
    from helpers import persist_chat

    return persist_chat.load_json_chats(chat_jsons)


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
    prefix = Path(filename).name if filename else f"khan-chats-{stamp}"
    prefix = Path(prefix).stem
    prefix = "".join(c if c.isalnum() or c in "-_" else "_" for c in prefix) or "khan-chats"

    payload = ndjson if ndjson.endswith("\n") else ndjson + "\n"
    fd, dest = tempfile.mkstemp(dir=inbox, prefix=f"{prefix}-", suffix=".ndjson")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except Exception:
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise
    return dest


def write_temp_file(content: bytes, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def build_chats_zip_file(path: str, ctxids: list[str] | None = None) -> dict[str, Any]:
    """Stream the chats pack ZIP straight to a temp file — avoids holding
    the full archive in memory alongside every chat JSON."""
    pairs = collect_exportable_chat_jsons(ctxids)
    chat_jsons = [js for _, js in pairs]
    ndjson = build_chats_ndjson(chat_jsons)
    manifest = {
        "format": CHATS_PACK_FORMAT,
        "version": CHATS_PACK_VERSION,
        "exported_at": _utc_now_iso(),
        "chat_count": len(pairs),
        "chat_ids": [cid for cid, _ in pairs],
        "kurultai_atoms": ndjson.count("\n") if ndjson else 0,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for ctxid, js in pairs:
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in ctxid)
            zf.writestr(f"chats/{safe}.json", js)
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
