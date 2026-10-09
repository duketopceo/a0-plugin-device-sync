"""Memory backend seam: null default, git impl, registry, idempotency."""

from __future__ import annotations

import json
import shutil

import pytest

from usr.plugins.device_sync.helpers import memory_backend
from usr.plugins.device_sync.helpers.memory_backend import (
    GitMemoryBackend,
    NullMemoryBackend,
)

HAS_GIT = shutil.which("git") is not None


def test_null_backend_is_safe_noop():
    b = NullMemoryBackend()
    assert b.export_atoms() == []
    assert b.has_atom("x") is False
    assert b.import_atoms([{"id": "x"}]) == 0


def test_make_backend_defaults_and_unknown():
    assert isinstance(memory_backend.make_backend("", {}), NullMemoryBackend)
    assert isinstance(memory_backend.make_backend("none", {}), NullMemoryBackend)
    assert isinstance(memory_backend.make_backend("bogus", {}), NullMemoryBackend)


def test_make_backend_factory_raises_falls_back():
    memory_backend.register_backend("exploding", lambda cfg: 1 / 0)
    assert isinstance(memory_backend.make_backend("exploding", {}), NullMemoryBackend)


def test_registered_backend_used():
    memory_backend.register_backend("dicts", lambda cfg: _DictB())
    b = memory_backend.make_backend("dicts", {})
    assert isinstance(b, _DictB)


class _DictB:
    def export_atoms(self):
        return [{"id": "x"}]

    def has_atom(self, i):
        return i == "x"

    def import_atoms(self, atoms):
        return len(atoms)


@pytest.mark.skipif(not HAS_GIT, reason="git binary absent")
def test_git_backend_round_trip(tmp_path):
    import subprocess

    repo = tmp_path / "atoms"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)

    b = GitMemoryBackend(repo)
    assert b.available
    atoms = [{"id": "a1", "title": "t", "content": "c", "tags": ["x"]}]
    assert b.import_atoms(atoms) == 1
    assert b.import_atoms(atoms) == 0  # idempotent
    assert b.has_atom("a1")
    assert b.export_atoms() == atoms

    # a commit landed
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    )
    assert "device-sync import" in log.stdout


def test_git_backend_unavailable_dir_is_safe(tmp_path):
    b = GitMemoryBackend(tmp_path / "missing")
    assert b.available is False
    assert b.export_atoms() == []
    # import still writes files (dir created on demand), just skips commit
    n = b.import_atoms([{"id": "z"}])
    assert n == 1
    assert json.loads((tmp_path / "missing" / "z.json").read_text())["id"] == "z"


def test_git_backend_skips_corrupt_files(tmp_path):
    repo = tmp_path
    (repo / "bad.json").write_text("{nope")
    (repo / "good.json").write_text('{"id": "g"}')
    b = GitMemoryBackend(repo)
    assert b.export_atoms() == [{"id": "g"}]
