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
@pytest.mark.parametrize("mode", (0o644, 0o640, 0o755, 0o4644, 0o2640, 0o1755))
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


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize("previous_mode", (None, 0o644, 0o660, 0o755))
def test_explicit_private_mode_overrides_preservation(mode_dir, writer, previous_mode):
    target = mode_dir / "secret"
    if previous_mode is not None:
        target.write_text("old secret")
        target.chmod(previous_mode)
    payload = b"secret" if writer == "atomic_write_bytes" else "secret"
    getattr(io_utils, writer)(target, payload, mode=0o600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize("mask", (0o022, 0o077))
@pytest.mark.parametrize("procfs", (True, False))
def test_umask_read_never_changes_parent_mask(mode_dir, mask, procfs):
    _child(
        "import os, sys; from pathlib import Path; from okf_loom import io_utils; "
        "mask = int(sys.argv[1]); os.umask(mask); "
        "os.umask = lambda *_: (_ for _ in ()).throw(AssertionError('parent umask changed')); "
        "Path.read_text = Path.read_text if sys.argv[2] == 'True' else "
        "lambda *_a, **_kw: (_ for _ in ()).throw(OSError('procfs hidden')); "
        "assert io_utils._process_umask() == mask; "
        "io_utils.atomic_write_text(sys.argv[3], 'content')",
        mask, procfs, mode_dir / "new",
    )
    assert stat.S_IMODE((mode_dir / "new").stat().st_mode) == 0o666 & ~mask


@pytest.mark.parametrize("writer", WRITERS)
def test_metadata_failure_keeps_target_and_cleans_closed_temp(mode_dir, monkeypatch, writer):
    target = mode_dir / "existing"
    target.write_text("before")
    target.chmod(0o640)
    observed = []

    def fail(fd, path, mode):
        observed.append(fd)
        # Content was flushed, but remains private until metadata is applied.
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        assert Path(path).read_text() == "before"
        raise PermissionError("cannot preserve metadata")

    monkeypatch.setattr(io_utils, "_set_atomic_metadata", fail)
    with pytest.raises(PermissionError, match="preserve metadata"):
        getattr(io_utils, writer)(target, b"after" if writer == "atomic_write_bytes" else "after")
    assert target.read_text() == "before"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert not list(mode_dir.glob(".okf-*.tmp"))
    with pytest.raises(OSError):
        os.fstat(observed[0])


def test_durable_metadata_is_synced_before_replace(mode_dir, monkeypatch):
    target = mode_dir / "content"
    events = []
    replace = os.replace

    def sync(fd):
        info = os.fstat(fd)
        events.append("directory sync" if stat.S_ISDIR(info.st_mode) else "file sync")
        if stat.S_ISREG(info.st_mode):
            assert stat.S_IMODE(info.st_mode) == 0o640

    def publish(source, destination):
        events.append("replace")
        replace(source, destination)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", publish)
    io_utils.durable_atomic_write_text(target, "content", mode=0o640)
    assert events == ["file sync", "replace", "directory sync"]


def test_replacement_retains_different_owner_before_mode(mode_dir, monkeypatch):
    from types import SimpleNamespace

    target = mode_dir / "content"
    target.write_text("before")
    previous = target.stat()
    real_stat = Path.stat
    events = []

    def target_stat(path, *args, **kwargs):
        if path == target:
            return SimpleNamespace(st_uid=previous.st_uid + 1,
                                   st_gid=previous.st_gid + 1, st_mode=0o106440)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", target_stat)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: events.append(("owner", uid, gid)))
    monkeypatch.setattr(os, "fchmod", lambda fd, mode: events.append(("mode", mode)))
    io_utils.atomic_write_text(target, "after")
    assert events == [("owner", previous.st_uid + 1, previous.st_gid + 1), ("mode", 0o6440)]


def test_studio_private_atomic_writers_restrict_replacements(mode_dir):
    from okf_loom.studio import Studio

    studio = Studio.for_bundle(mode_dir)
    studio.ensure_session()

    def write_state():
        studio.idempotency_store("mode-test", body={"x": 1}, response={"ok": True})
        studio._save_last_logged()
        studio.append_event({"type": "changed", "ids": ["note"]})
        studio.audit_public_denial(route="/__apply", method="POST")
        studio.snapshot_for_undo(concept_id="note", raw="before", group_id="mode-single")
        studio.snapshot_many_for_undo([("note", "batch")], group_id="mode-batch")

    write_state()
    paths = [*studio.idempotency_dir.glob("*.json"), studio.last_logged_path,
             studio.event_sequence_path, studio._session_io_dir / "public-denials.json",
             *studio.history_dir.rglob("*.md"), *studio.history_dir.rglob("manifest.json")]
    assert len(paths) >= 8
    for path in paths:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
        path.chmod(0o664)
    write_state()
    for path in paths:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


@pytest.mark.parametrize("relative", (".token", "server.json"))
def test_descriptor_session_tokens_restrict_replacements(mode_dir, relative):
    from okf_loom.studio import Studio

    studio = Studio.for_bundle(mode_dir)
    studio.write_session_text(relative, "first secret")
    path = studio._session_io_dir / relative
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o666)
    studio.write_session_text(relative, "rotated secret")
    assert path.read_text() == "rotated secret"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_presence_and_comment_receipts_restrict_replacements(mode_dir):
    from okf_loom.studio import Studio, _write_receipt_at

    studio = Studio.for_bundle(mode_dir)
    studio.ensure_session()
    studio.set_presence(actor="agent", state="editing")
    path = studio.presence_path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o664)
    studio.set_presence(actor="agent", state="idle")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    directory_fd = studio.session_authority.fd
    _write_receipt_at(directory_fd, "receipt.json", {"ok": True})
    receipt = studio._session_io_dir / "receipt.json"
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    receipt.chmod(0o664)
    _write_receipt_at(directory_fd, "receipt.json", {"ok": False})
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600


def test_proposal_authority_stays_private_and_refuses_wider_existing_file(mode_dir):
    from okf_loom.proposal_review.errors import StorageSafetyError
    from okf_loom.proposal_review.ports import LinuxDirFdFilesystem

    directory_fd = os.open(mode_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        authority = LinuxDirFdFilesystem.adopt(directory_fd)
    finally:
        os.close(directory_fd)
    try:
        authority.write_atomic(".proposal-http-token", b"secret")
        path = mode_dir / ".proposal-http-token"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        authority.write_atomic(".proposal-http-token", b"rotated secret")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.chmod(0o644)
        # Existing proposal integrity rules reject unsafe state, rather than
        # adopting/repairing it or preserving its wider permissions.
        with pytest.raises(StorageSafetyError):
            authority.write_atomic(".proposal-http-token", b"not published")
        assert path.read_bytes() == b"rotated secret"
    finally:
        authority.close()


def test_derived_caches_stay_private(mode_dir):
    from okf_loom.index import emit_derived_json
    from okf_loom.model import Bundle

    (mode_dir / "note.md").write_text("---\ntype: note\n---\nBody\n")
    bundle = Bundle.load(mode_dir)
    paths = emit_derived_json(bundle)
    for path in paths:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.chmod(0o664)
    emit_derived_json(bundle)
    for path in paths:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((paths[0].parent / ".gitignore").stat().st_mode) == 0o600


def test_replace_text_keeps_concept_readable(mode_dir):
    concept = mode_dir / "note.md"
    concept.write_text("---\ntype: note\n---\nBefore sentence.\n")
    concept.chmod(0o644)
    _cli_with_umask(0o022, "replace-text", "--bundle", mode_dir, "--id", "note",
                    "--old", "Before sentence.", "--new", "After sentence.")
    assert "After sentence." in concept.read_text()
    assert stat.S_IMODE(concept.stat().st_mode) == 0o644


@pytest.mark.parametrize("target", ("static", "spa", "single-file"))
@pytest.mark.parametrize("mask", (0o022, 0o027))
def test_docs_build_tree_uses_umask(mode_dir, target, mask):
    output = mode_dir / "site"
    _cli_with_umask(mask, "build", "docs-bundle", "--target", target, "--out", output)
    paths = [output, *output.rglob("*")]
    assert (output / "index.html").is_file()
    wrong_modes = {
        str(path.relative_to(mode_dir)): oct(stat.S_IMODE(path.stat().st_mode))
        for path in paths
        if stat.S_IMODE(path.stat().st_mode) != (0o777 if path.is_dir() else 0o666) & ~mask
    }
    assert wrong_modes == {}
