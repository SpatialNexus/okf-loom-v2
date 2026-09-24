"""Data-safety proofs for ordinary exports; all mutations use temporary trees."""
from pathlib import Path
import os

import pytest

from okf_loom.model import Bundle
from okf_loom.render import build_site
from okf_loom.io_utils import atomic_replace_tree


def tree_bytes(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None
            for p in root.rglob("*")}


@pytest.mark.parametrize("target", ["static", "spa", "single-file"])
@pytest.mark.parametrize("destination", ["equal", "ancestor", "normalized"])
def test_build_source_overlap_refuses_without_writes(tmp_path, target, destination):
    source = tmp_path / "bundle"
    source.mkdir()
    (source / "note.md").write_text("---\ntype: Note\n---\nOriginal bytes\n")
    bundle = Bundle.load(source)
    out = {"equal": source, "ancestor": tmp_path,
           "normalized": source / "missing" / ".."}[destination]
    before = tree_bytes(tmp_path)
    with pytest.raises(ValueError, match="source"):
        build_site(bundle, out, target=target)
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("old_kind", ["directory", "file", "symlink", "dangling"])
def test_rebuild_preserves_unrelated_old(tmp_path, old_kind):
    src, dst, old = (tmp_path / n for n in ("stage", "site", "site.old"))
    src.mkdir()
    dst.mkdir()
    (src / "new").write_text("new")
    (dst / "old").write_text("old")
    if old_kind == "directory":
        old.mkdir()
        (old / "unrelated").write_text("keep")
    elif old_kind == "file":
        old.write_text("keep")
    else:
        old.symlink_to(tmp_path / ("site" if old_kind == "symlink" else "missing"))
    old_inode = old.lstat().st_ino
    assert atomic_replace_tree(src, dst) is None
    assert (dst / "new").read_text() == "new"
    assert not (dst / "old").exists()
    assert old.lstat().st_ino == old_inode
    assert not src.exists()
    assert set(p.name for p in tmp_path.iterdir()) == {"site", "site.old"}


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_publish_failure_preserves_previous_output(tmp_path, monkeypatch, rollback_fails):
    from okf_loom.io_utils import TreePublicationRecoveryError

    src, dst = tmp_path / "stage", tmp_path / "site"
    src.mkdir()
    dst.mkdir()
    (src / "new").write_text("new")
    (dst / "old").write_text("irreplaceable")
    rename = os.rename

    def fail_publish(source, destination, **kwargs):
        if str(source) == src.name or (rollback_fails and str(source) == "previous"):
            raise OSError("injected rename failure")
        return rename(source, destination, **kwargs)

    monkeypatch.setattr(os, "rename", fail_publish)
    with pytest.raises(OSError) as error:
        atomic_replace_tree(src, dst)
    assert (src / "new").read_text() == "new"
    if rollback_fails:
        assert isinstance(error.value, TreePublicationRecoveryError)
        recovery = error.value.recovery_path
        assert (recovery / "old").read_text() == "irreplaceable"
        assert str(recovery) in str(error.value)
        assert error.value.rollback_error is not None
        assert not dst.exists()
        # A later invocation neither adopts nor deletes retained recovery.
        monkeypatch.setattr(os, "rename", rename)
        atomic_replace_tree(src, dst)
        assert (recovery / "old").read_text() == "irreplaceable"
    else:
        assert not isinstance(error.value, TreePublicationRecoveryError)
        assert (dst / "old").read_text() == "irreplaceable"
        assert set(p.name for p in tmp_path.iterdir()) == {"site", "stage"}


def test_build_missing_parent_stages_locally(tiny_good_bundle, tmp_path, monkeypatch):
    import tempfile

    out = tmp_path / "missing" / "parents" / "site"
    mkdtemp = tempfile.mkdtemp
    stages = []

    def observe_stage(suffix=None, prefix=None, dir=None):
        assert dir is not None, "staging must not fall back to system temp"
        assert Path(dir) == out.parent
        assert out.parent.is_dir()
        stage = Path(mkdtemp(suffix=suffix, prefix=prefix, dir=dir))
        assert stage.stat().st_dev == out.parent.stat().st_dev
        stages.append(stage)
        return str(stage)

    monkeypatch.setattr(tempfile, "mkdtemp", observe_stage)
    result = build_site(Bundle.load(tiny_good_bundle), out, target="static")
    assert result["out_dir"] == str(out)
    assert (out / "index.html").is_file()
    assert stages and all(not p.exists() for p in stages)


@pytest.mark.parametrize("include_all", [False, True])
def test_build_descendant_rebuild_does_not_reconsume_media(tiny_good_bundle, include_all):
    source = tiny_good_bundle
    if include_all:
        (source / "okf-loom.config.yaml").write_text('bundle:\n  include: ["**"]\n')
    (source / "picture.png").write_bytes(b"source media")
    before = tree_bytes(source)
    out = source / "dist"
    for _ in range(3):
        stats = build_site(Bundle.load(source), out, target="static")
        assert stats["out_dir"] == str(out)
        assert (out / "picture.png").read_bytes() == b"source media"
        assert not (out / "dist").exists()
        assert all(not p.name.startswith(".okf-build-") for p in out.iterdir())
    assert {name: value for name, value in tree_bytes(source).items()
            if name != "dist" and not name.startswith("dist/")} == before


def test_build_refuses_descendant_containing_source_documents(tiny_good_bundle):
    before = tree_bytes(tiny_good_bundle)
    bundle = Bundle.load(tiny_good_bundle)
    out = next(c.path.parent for c in bundle.concepts.values()
               if c.path.parent != tiny_good_bundle)
    with pytest.raises(ValueError, match="source"):
        build_site(bundle, out, target="static")
    assert tree_bytes(tiny_good_bundle) == before


@pytest.mark.parametrize("target", ["static", "spa", "single-file"])
@pytest.mark.parametrize("link_kind", ["leaf", "dangling", "parent", "normalized_parent"])
def test_build_rejects_symlink_destination_without_writes(
    tiny_good_bundle, tmp_path, target, link_kind,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("unrelated")
    alias = tmp_path / "alias"
    alias.symlink_to(outside if link_kind != "dangling" else tmp_path / "absent")
    out = alias
    if link_kind == "parent":
        out = alias / "site"
    if link_kind == "normalized_parent":
        out = alias / ".." / "site"
    before = tree_bytes(tiny_good_bundle), tree_bytes(outside), alias.lstat().st_ino
    with pytest.raises(ValueError, match="symlink"):
        build_site(Bundle.load(tiny_good_bundle), out, target=target)
    assert (tree_bytes(tiny_good_bundle), tree_bytes(outside), alias.lstat().st_ino) == before
    assert not (tmp_path / "site").exists()
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("which", ["source", "destination"])
def test_helper_rejects_symlink_leaf(tmp_path, which):
    real = tmp_path / "real"
    real.mkdir()
    (real / "keep").write_text("unchanged")
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    other = tmp_path / "other"
    other.mkdir()
    src, dst = (alias, other) if which == "source" else (other, alias)
    with pytest.raises(ValueError, match="symlink"):
        atomic_replace_tree(src, dst)
    assert (real / "keep").read_text() == "unchanged"
    assert alias.is_symlink()
    assert other.is_dir()


def test_backup_name_collision_never_adopts_or_deletes(tmp_path, monkeypatch):
    import uuid

    value = uuid.uuid4()
    backup = tmp_path / f".okf-recovery-{value.hex}"
    backup.mkdir()
    (backup / "unrelated").write_text("keep")
    src, dst = tmp_path / "stage", tmp_path / "site"
    src.mkdir()
    dst.mkdir()
    before = tree_bytes(tmp_path)
    monkeypatch.setattr(uuid, "uuid4", lambda: value)
    with pytest.raises(FileExistsError):
        atomic_replace_tree(src, dst)
    assert tree_bytes(tmp_path) == before


def test_failed_build_rollback_retains_recovery_after_stage_cleanup(
    tiny_good_bundle, tmp_path, monkeypatch,
):
    from okf_loom.io_utils import TreePublicationRecoveryError

    out = tmp_path / "site"
    out.mkdir()
    (out / "keep").write_text("previous site")
    rename = os.rename

    def fail_publish_and_restore(src, dst, **kwargs):
        if str(src).startswith(".okf-build-") or str(src) == "previous":
            raise OSError("injected failure")
        return rename(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", fail_publish_and_restore)
    before = tree_bytes(tiny_good_bundle)
    with pytest.raises(TreePublicationRecoveryError) as error:
        build_site(Bundle.load(tiny_good_bundle), out, target="static")
    assert (error.value.recovery_path / "keep").read_text() == "previous site"
    assert not list(tmp_path.glob(".okf-build-*"))
    assert tree_bytes(tiny_good_bundle) == before


def test_publication_waits_for_cross_process_parent_lock(tmp_path):
    import fcntl
    import subprocess
    import sys
    from conftest import okf_subprocess_env

    src, dst = tmp_path / "stage", tmp_path / "site"
    src.mkdir()
    dst.mkdir()
    (src / "new").write_text("new")
    (dst / "old").write_text("old")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    process = None
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        program = """
import sys
from okf_loom.io_utils import atomic_replace_tree
print('ready', flush=True)
atomic_replace_tree(sys.argv[1], sys.argv[2])
"""
        process = subprocess.Popen(
            [sys.executable, "-c", program, str(src), str(dst)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=okf_subprocess_env(),
        )
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.2)
        assert (dst / "old").read_text() == "old"
        fcntl.flock(parent_fd, fcntl.LOCK_UN)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, (stdout, stderr)
        assert (dst / "new").read_text() == "new"
        assert not src.exists()
    finally:
        os.close(parent_fd)
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def test_rollback_never_overwrites_intervening_destination(tmp_path, monkeypatch):
    from okf_loom.io_utils import TreePublicationRecoveryError

    src, dst = tmp_path / "stage", tmp_path / "site"
    src.mkdir()
    dst.mkdir()
    (dst / "old").write_text("old")
    rename = os.rename
    replacement_inode = None

    def intervene(source, destination, **kwargs):
        nonlocal replacement_inode
        if str(source) == src.name:
            dst.mkdir()
            replacement_inode = dst.stat().st_ino
            raise OSError("publication interrupted by a non-cooperating writer")
        return rename(source, destination, **kwargs)

    monkeypatch.setattr(os, "rename", intervene)
    with pytest.raises(TreePublicationRecoveryError) as error:
        atomic_replace_tree(src, dst)
    assert dst.stat().st_ino == replacement_inode
    assert (error.value.recovery_path / "old").read_text() == "old"


def test_first_rename_failure_leaves_existing_site(tmp_path, monkeypatch):
    src, dst = tmp_path / "stage", tmp_path / "site"
    src.mkdir()
    dst.mkdir()
    (dst / "old").write_text("old")
    before = tree_bytes(tmp_path)

    def fail(*args, **kwargs):
        raise OSError("first rename failed")

    monkeypatch.setattr(os, "rename", fail)
    with pytest.raises(OSError, match="first rename failed"):
        atomic_replace_tree(src, dst)
    assert tree_bytes(tmp_path) == before


@pytest.mark.parametrize("relation", ["equal", "ancestor", "descendant"])
def test_helper_overlap_refuses_without_writes(tmp_path, relation):
    src = tmp_path / "stage"
    src.mkdir()
    (src / "keep").write_text("keep")
    dst = {"equal": src, "ancestor": tmp_path, "descendant": src / "child"}[relation]
    before = tree_bytes(tmp_path)
    with pytest.raises(ValueError, match="overlap"):
        atomic_replace_tree(src, dst)
    assert tree_bytes(tmp_path) == before


def test_concurrent_thread_publishers_do_not_mix_generations(tmp_path, monkeypatch):
    import fcntl
    import threading
    from concurrent.futures import ThreadPoolExecutor

    first, second, dst = (tmp_path / n for n in ("first", "second", "site"))
    for path in (first, second, dst):
        path.mkdir()
        (path / path.name).write_text(path.name)
    moved_old = threading.Event()
    second_at_lock = threading.Event()
    release_first = threading.Event()
    rename, flock = os.rename, fcntl.flock

    def pause_first(source, destination, **kwargs):
        if str(source) == "first":
            moved_old.set()
            assert release_first.wait(5)
        return rename(source, destination, **kwargs)

    def observe_lock(fd, operation):
        if moved_old.is_set():
            second_at_lock.set()
        return flock(fd, operation)

    monkeypatch.setattr(os, "rename", pause_first)
    monkeypatch.setattr(fcntl, "flock", observe_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(atomic_replace_tree, first, dst)
        try:
            assert moved_old.wait(5)
            two = pool.submit(atomic_replace_tree, second, dst)
            assert second_at_lock.wait(5)
            assert not two.done()
            assert not dst.exists()
        finally:
            release_first.set()
        assert one.result(timeout=5) is None
        assert two.result(timeout=5) is None
    assert tree_bytes(dst) == {"second": b"second"}
    assert set(p.name for p in tmp_path.iterdir()) == {"site"}


def test_interruption_after_first_rename_restores_previous(tmp_path, monkeypatch):
    src, dst = tmp_path / "stage", tmp_path / "site"
    src.mkdir()
    dst.mkdir()
    (dst / "old").write_text("old")
    rename = os.rename

    def interrupt(source, destination, **kwargs):
        result = rename(source, destination, **kwargs)
        if str(source) == "site":
            raise KeyboardInterrupt("after moving old output")
        return result

    monkeypatch.setattr(os, "rename", interrupt)
    with pytest.raises(KeyboardInterrupt):
        atomic_replace_tree(src, dst)
    assert (dst / "old").read_text() == "old"
    assert set(p.name for p in tmp_path.iterdir()) == {"stage", "site"}


def test_backup_cleanup_failure_is_explicit(tmp_path, monkeypatch):
    import shutil
    from okf_loom.io_utils import TreePublicationRecoveryError

    src, dst = tmp_path / "stage", tmp_path / "site"
    src.mkdir()
    dst.mkdir()
    (src / "new").write_text("new")
    (dst / "old").write_text("old")

    def fail_cleanup(*args, **kwargs):
        raise PermissionError("injected cleanup failure")

    monkeypatch.setattr(shutil, "rmtree", fail_cleanup)
    with pytest.raises(TreePublicationRecoveryError) as error:
        atomic_replace_tree(src, dst)
    assert (error.value.recovery_path / "previous" / "old").read_text() == "old"
    assert (dst / "new").read_text() == "new"


def test_verify_previous_called_under_lock_before_recovery_mkdir(tmp_path, monkeypatch):
    import fcntl

    src, dst = tmp_path / "stage", tmp_path / "site"
    src.mkdir()
    dst.mkdir()
    (src / "new").write_text("new")
    (dst / "old").write_text("old")
    calls = []
    flock, mkdir, rename = fcntl.flock, os.mkdir, os.rename

    def spy_flock(fd, op):
        calls.append(("flock", op))
        return flock(fd, op)

    def spy_mkdir(path, *args, **kwargs):
        calls.append(("mkdir", str(path).startswith(".okf-recovery-")))
        return mkdir(path, *args, **kwargs)

    def spy_rename(source, destination, **kwargs):
        calls.append(("rename", str(source), str(destination)))
        return rename(source, destination, **kwargs)

    def verifier(parent_fd, name, phase):
        calls.append(("verify", name, phase))
        assert os.stat(name, dir_fd=parent_fd, follow_symlinks=False)

    monkeypatch.setattr(fcntl, "flock", spy_flock)
    monkeypatch.setattr(os, "mkdir", spy_mkdir)
    monkeypatch.setattr(os, "rename", spy_rename)
    atomic_replace_tree(src, dst, verify_previous=verifier)
    assert calls == [
        ("flock", fcntl.LOCK_EX),
        ("verify", "site", "locked"),
        ("mkdir", True),
        ("rename", "site", "previous"),
        ("verify", "previous", "quarantined"),
        ("rename", "stage", "site"),
    ]
    assert (dst / "new").read_text() == "new"


def test_verify_previous_quarantine_failure_restores_destination(tmp_path):
    src, dst = tmp_path / "stage", tmp_path / "site"
    src.mkdir()
    dst.mkdir()
    (src / "new").write_text("new")
    (dst / "old").write_text("old")

    def verifier(parent_fd, name, phase):
        if phase == "quarantined":
            raise FileExistsError("changed while quarantined")

    with pytest.raises(FileExistsError, match="quarantined"):
        atomic_replace_tree(src, dst, verify_previous=verifier)
    assert (dst / "old").read_text() == "old"
    assert (src / "new").read_text() == "new"
    assert set(p.name for p in tmp_path.iterdir()) == {"stage", "site"}
