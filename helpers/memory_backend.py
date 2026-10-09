"""Memory-pack backend seam — replaces Khan's horde.memory coupling.

Khan's continuity sync reads/writes ``horde.memory.store.MemoryAtomStore``
directly. Stock a0 has no such store, so the plugin defines a minimal
backend protocol — atoms are plain dicts with an ``id`` field — and a
registry the host (or a sibling plugin like Kurultai) fills at configure
time via ``runtime.register_memory_backend``.

Shipped impls:

- ``null`` (default) — memory sync reports "unsupported". A peer without
  memory endpoints is a normal condition (Khan already treats a 404 as
  skip-not-fail); with no backend this box IS that peer.
- ``git`` — the MemFS-style option: ``memory_dir`` is a git repo of
  ``<atom_id>.json`` files; ``import_atoms`` writes + commits; ``push``/
  ``pull`` are real git ops. Backup/diff/rollback come free. ``git``
  binary absence fails safe to an empty store.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Protocol

from usr.plugins.device_sync.helpers import LOG_NAME

log = logging.getLogger(LOG_NAME)


class MemoryBackend(Protocol):
    """The contract the sync engine calls — it only ever invokes
    ``export_atoms``/``import_atoms`` (+ reads ``is_null``). Impls may
    raise; the engine failure-contains. ``has_atom`` is an impl-internal
    dedupe helper, not an engine-facing method."""

    is_null: bool = False  # True = "no memory store" — pull skips fetching

    def export_atoms(self) -> list[dict[str, Any]]:
        """All syncable atoms (expired/atoms the impl wants skipped are
        its own business — the transport ships what it gets)."""
        ...

    def has_atom(self, atom_id: str) -> bool:
        """True when the atom already exists locally — import is
        idempotent by id, supersession is the atom's own job."""
        ...

    def import_atoms(self, atoms: list[dict[str, Any]]) -> int:
        """Store atoms that are new; return count actually imported."""
        ...


class NullMemoryBackend:
    """Default — no memory store. Export returns [], import returns 0."""

    is_null = True

    def export_atoms(self) -> list[dict[str, Any]]:
        return []

    def has_atom(self, atom_id: str) -> bool:
        return False

    def import_atoms(self, atoms: list[dict[str, Any]]) -> int:
        return 0


class GitMemoryBackend:
    """Atom store = a git repo of JSON files. Import writes files and
    commits; push/pull run the real git ops. Falls back to an empty store
    when git or the repo is unavailable — never raises out of sync paths."""

    is_null = False

    def __init__(self, repo_dir: str | Path) -> None:
        self.dir = Path(repo_dir)
        self._git = self._find_git()
        # import_atoms runs from both the sync engine and the memory_import
        # API handler — serialize so concurrent imports can't tear files
        # or race git's index.lock.
        self._import_lock = threading.Lock()

    @staticmethod
    def _find_git() -> str | None:
        return shutil.which("git")

    @property
    def available(self) -> bool:
        return self._git is not None and self.dir.is_dir()

    def _atom_path(self, atom_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in atom_id)
        if safe != atom_id:
            # ids differing only in stripped chars must not collide — a
            # short content hash keeps the mapping injective.
            safe = f"{safe or 'atom'}-{hashlib.blake2s(atom_id.encode(), digest_size=6).hexdigest()}"
        return self.dir / f"{safe or 'atom'}.json"

    def _run_git(self, *args: str, timeout: float = 60) -> bool:
        if not self.available:
            return False
        try:
            proc = subprocess.run(
                [self._git, "-C", str(self.dir), *args],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
            if proc.returncode != 0:
                log.warning(
                    "device-sync git backend: git %s exited %s: %s",
                    args[0], proc.returncode, (proc.stderr or "").strip()[:300],
                )
                return False
            return True
        except Exception as e:
            log.warning("device-sync git backend: git %s failed: %s", args[0], e)
            return False

    def export_atoms(self) -> list[dict[str, Any]]:
        if not self.dir.is_dir():
            return []
        atoms: list[dict[str, Any]] = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning(
                    "device-sync git backend: skipping unreadable atom file %s: %s",
                    path,
                    e,
                )
                continue
            if isinstance(data, dict) and data.get("id"):
                atoms.append(data)
        return atoms

    def has_atom(self, atom_id: str) -> bool:
        return self._atom_path(str(atom_id)).is_file()

    def _write_atom(self, atom: dict[str, Any], path: Path) -> None:
        """tmp + os.replace — a crash mid-write must not leave a corrupt
        file that has_atom then treats as present forever."""
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(atom, ensure_ascii=False, indent=2) + "\n")
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def import_atoms(self, atoms: list[dict[str, Any]]) -> int:
        imported = 0
        with self._import_lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                log.warning("device-sync git backend: mkdir %s failed: %s", self.dir, e)
                return 0
            for atom in atoms:
                atom_id = str(atom.get("id") or "").strip()
                if not atom_id or self.has_atom(atom_id):
                    continue
                try:
                    self._write_atom(atom, self._atom_path(atom_id))
                except OSError as e:
                    log.warning(
                        "device-sync git backend: write %r failed: %s", atom_id, e
                    )
                    continue
                imported += 1
            if imported and self.available:
                self._run_git("add", "-A", ".")
                self._run_git(
                    "-c", "user.name=a0-device-sync",
                    "-c", "user.email=device-sync@local",
                    "commit", "-q", "-m", f"device-sync import: {imported} atoms",
                )
        return imported

    # MemFS extras — the transport doesn't call these; operators/other
    # plugins can (rollback = `git revert`, backup = the remote).
    def pull(self) -> bool:
        return self._run_git("pull", "--ff-only")

    def push(self) -> bool:
        return self._run_git("push")


# Backend registry — populated by runtime.configure from the
# `memory_backend` config key; sibling plugins can register richer stores.
_factories: dict[str, Any] = {}


def register_backend(name: str, factory) -> None:
    """Register a backend factory ``(config: dict) -> MemoryBackend``."""
    if name and callable(factory):
        _factories[str(name)] = factory


def make_backend(name: str, config: dict[str, Any]) -> MemoryBackend:
    """Resolve a backend by name; unknown/absent -> null (never raises)."""
    name = (name or "none").strip().lower()
    if name in ("none", "null", "off", ""):
        return NullMemoryBackend()
    try:
        if name == "git":
            return GitMemoryBackend(config.get("memory_dir") or _default_dir())
        factory = _factories.get(name)
        if factory is not None:
            backend = factory(config)
            if backend is not None:
                return backend
    except Exception as e:
        log.warning("device-sync memory backend %r failed: %s", name, e)
    return NullMemoryBackend()


def _default_dir() -> str:
    try:
        from helpers import files

        return str(files.get_abs_path("usr/plugins/device_sync/data/memory-atoms"))
    except Exception:
        return str(Path.home() / ".a0-device-sync" / "memory-atoms")


def reset_backends() -> None:
    """Clear custom registrations (test/uninstall hook)."""
    _factories.clear()
