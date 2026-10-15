"""Atomic content writes must not turn published or authored files private."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest

from okf_loom import io_utils


ROOT = Path(__file__).resolve().parents[1]
WRITERS = ("atomic_write_text", "atomic_write_bytes", "durable_atomic_write_text")


@pytest.fixture
def mode_dir():
    # Stay outside the checkout's inherited default ACL and never change the
    # pytest process's umask: server tests can have live threads in this process.
    directory = Path(tempfile.mkdtemp(prefix="okf-modes-"))
    directory.chmod(0o700)
    if hasattr(os, "removexattr"):
        try:
            os.removexattr(directory, "system.posix_acl_default")
        except OSError:
            pass
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def _child(code: str, *args: object) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(ROOT / "scripts"), PYTHON=sys.executable)
    return subprocess.run(
        [sys.executable, "-c", code, *map(str, args)], cwd=ROOT,
        env=env, text=True, capture_output=True, check=True,
    )


def _cli_with_umask(mask: int, *args: object) -> None:
    _child(
        "import os, sys; os.umask(int(sys.argv[1])); "
        "os.execv('scripts/okf-loom', ['scripts/okf-loom', *sys.argv[2:]])",
        mask, *args,
    )


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("mask", (0o022, 0o077))
def test_new_content_uses_umask(mode_dir, writer, mask):
    target = mode_dir / "new"
    _child(
        "import os, sys; from okf_loom import io_utils; "
        "os.umask(int(sys.argv[1])); "
        "getattr(io_utils, sys.argv[2])(sys.argv[3], "
        "b'content' if sys.argv[2] == 'atomic_write_bytes' else 'content')",
        mask, writer, target,
    )
    assert stat.S_IMODE(target.stat().st_mode) == 0o666 & ~mask


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("mode", (0o644, 0o640, 0o755))
def test_replace_preserves_permissions_and_owner(mode_dir, writer, mode):
    target = mode_dir / "existing"
    target.write_text("before")
    target.chmod(mode)
    before = target.stat()
    payload = b"after" if writer == "atomic_write_bytes" else "after"
    getattr(io_utils, writer)(target, payload)
    after = target.stat()
    assert target.read_text() == "after"
    assert stat.S_IMODE(after.st_mode) == mode
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert after.st_ino != before.st_ino


def test_replace_text_keeps_concept_readable(mode_dir):
    concept = mode_dir / "note.md"
    concept.write_text("---\ntype: note\n---\nBefore sentence.\n")
    concept.chmod(0o644)
    _cli_with_umask(0o022, "replace-text", "--bundle", mode_dir, "--id", "note",
                    "--old", "Before sentence.", "--new", "After sentence.")
    assert "After sentence." in concept.read_text()
    assert stat.S_IMODE(concept.stat().st_mode) == 0o644


@pytest.mark.parametrize("target", ("static", "single-file"))
def test_docs_build_files_readable_by_others(mode_dir, target):
    output = mode_dir / ("site" if target == "static" else "site.html")
    _cli_with_umask(0o022, "build", "docs-bundle", "--target", target, "--out", output)
    files = list(output.rglob("*")) if output.is_dir() else [output]
    files = [path for path in files if path.is_file()]
    assert files
    unreadable = [str(path.relative_to(mode_dir)) for path in files
                  if not path.stat().st_mode & stat.S_IROTH]
    assert unreadable == []
