"""Conservative migration through the public CLI and filesystem boundary."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from okf_loom.parse import parse_document
from okf_loom.upgrade import UpgradeError, apply_upgrade, plan_upgrade

CLI = Path(__file__).resolve().parents[1] / "scripts/okf-loom"


def inventory(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() if p.is_file() else None
            for p in root.rglob("*")}


def legacy(root):
    (root / "index.md").write_bytes(
        b'---\r\nokf_version: "0.1" # keep\r\ncustom: [a, b]\r\n---\r\n# Authored\r\n')
    (root / "a.md").write_bytes(
        b'---\r\ntype: Note\r\ntimestamp: "2026-01-02T03:04:05Z" # evidence\r\n'
        b'custom: {v: "001"} # keep\r\n---\r\n\r\n# Citations\r\n- ambiguous\r\n\r\n')
    return root


def cli(root, *options):
    return subprocess.run([str(CLI), "upgrade", str(root), *options, "--format", "json"],
                          capture_output=True, text=True, timeout=30)


def test_preview_and_check_inventory_unchanged(tmp_path):
    root = legacy(tmp_path)
    runtime = root / ".okf-loom/session"
    runtime.mkdir(parents=True)
    (runtime / ".token").write_bytes(b"PRIVATE_RUNTIME_TOKEN")
    before = inventory(root)
    for mode in ([], ["--preview"], ["--check"], ["--check", "--actor", "process:exporter"]):
        result = cli(root, *mode)
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["status"] == "partial"
        assert [c["path"] for c in report["changes"]] == (
            ["a.md", "index.md"] if "--actor" in mode else ["index.md"])
        assert report["revision"]
        assert inventory(root) == before
        assert "PRIVATE_RUNTIME_TOKEN" not in result.stdout + result.stderr


def test_fresh_bundle_preview_creates_no_runtime_or_lock_files(tmp_path):
    root = legacy(tmp_path)
    before = inventory(root)
    for mode in ("--preview", "--check"):
        assert cli(root, mode).returncode == 0
        assert inventory(root) == before


@pytest.mark.parametrize("actor", [None, "process:original-exporter", "human:explicit-author"])
def test_apply_is_additive_preserves_evidence_and_is_idempotent(tmp_path, actor):
    root = legacy(tmp_path)
    original = inventory(root)
    options = ["--actor", actor] if actor else []
    preview = cli(root, "--preview", *options)
    revision = json.loads(preview.stdout)["revision"]
    applied = cli(root, "--apply", "--expected-revision", revision, *options)
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["applied"] is True
    assert (root / "index.md").read_bytes() == original["index.md"].replace(b'"0.1"', b'"0.2"')
    raw = (root / "a.md").read_bytes()
    if actor is None:
        assert raw == original["a.md"]
    else:
        fm, _ = parse_document(raw.decode())
        assert fm["generated"] == {"by": actor, "at": "2026-01-02T03:04:05+00:00"}
        # Removing only the addition reconstructs every original byte.
        addition = raw[raw.index(b"generated:"):raw.index(b"---\r\n", 5)]
        assert raw.replace(addition, b"") == original["a.md"]
        assert fm["timestamp"] == "2026-01-02T03:04:05Z"
    before = inventory(root)
    mtimes = {p: p.stat().st_mtime_ns for p in root.rglob("*")}
    again = cli(root, "--apply", *options)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["changes"] == []
    assert inventory(root) == before
    assert {p: p.stat().st_mtime_ns for p in root.rglob("*")} == mtimes


def test_preview_revision_conflict_is_readonly(tmp_path):
    root = legacy(tmp_path)
    revision = json.loads(cli(root).stdout)["revision"]
    (root / "a.md").write_bytes((root / "a.md").read_bytes() + b"New user edit")
    before = inventory(root)
    result = cli(root, "--apply", "--actor", "process:exporter", "--expected-revision", revision)
    assert result.returncode == 1 and "rev_conflict" in result.stderr
    assert inventory(root) == before


def test_preflight_checks_other_files_and_caught_failure_rolls_back(tmp_path, monkeypatch):
    import okf_loom.upgrade as migration

    root = legacy(tmp_path)
    original = inventory(root)
    plan = plan_upgrade(root, actor="process:exporter")
    (root / "new.md").write_text("---\ntype: Note\n---\nConcurrent addition\n")
    with pytest.raises(UpgradeError, match="rev_conflict"):
        apply_upgrade(plan)
    assert (root / "a.md").read_bytes() == original["a.md"]
    (root / "new.md").unlink()
    write = migration._Publication.publish

    def fail_last(target):
        if target.path.name == "index.md":
            raise OSError("injected write failure")
        write(target)

    monkeypatch.setattr(migration._Publication, "publish", fail_last)
    with pytest.raises(OSError, match="injected"):
        apply_upgrade(plan_upgrade(root, actor="process:exporter"))
    for name, raw in original.items():
        assert (root / name).read_bytes() == raw


def test_rollback_preserves_intervening_edit(tmp_path, monkeypatch):
    import okf_loom.upgrade as migration

    root = legacy(tmp_path)
    index = (root / "index.md").read_bytes()
    write = migration._Publication.publish

    def edit_then_fail(target):
        if target.path.name == "index.md":
            (root / "a.md").write_bytes(b"newer external edit")
            raise OSError("injected failure")
        write(target)

    monkeypatch.setattr(migration._Publication, "publish", edit_then_fail)
    with pytest.raises(UpgradeError, match="rollback conflict"):
        apply_upgrade(plan_upgrade(root, actor="process:exporter"))
    assert (root / "a.md").read_bytes() == b"newer external edit"
    assert (root / "index.md").read_bytes() == index


def test_concurrent_appliers_serialize_and_stale_one_refuses(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    root = legacy(tmp_path)
    plans = [plan_upgrade(root, actor="process:exporter") for _ in range(2)]

    def apply(plan):
        try:
            return apply_upgrade(plan)["applied"]
        except UpgradeError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(apply, plans)) == [False, True]
    assert plan_upgrade(root, actor="process:exporter").changes == {}


def test_public_cli_concurrent_appliers_share_revision_cas(tmp_path):
    root = legacy(tmp_path)
    revision = json.loads(cli(root).stdout)["revision"]
    command = [str(CLI), "upgrade", str(root), "--apply", "--actor", "process:exporter",
               "--expected-revision", revision, "--format", "json"]
    processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for _ in range(2)]
    outputs = [process.communicate(timeout=30) for process in processes]
    assert sorted(process.returncode for process in processes) == [0, 1], outputs
    loser = next(output for process, output in zip(processes, outputs) if process.returncode == 1)
    assert "rev_conflict" in loser[1]
    assert plan_upgrade(root, actor="process:exporter").changes == {}


def test_process_death_after_file_publication_is_forward_recoverable(tmp_path):
    import os
    import sys

    root = legacy(tmp_path)
    (root / "b.md").write_bytes((root / "a.md").read_bytes())
    original = inventory(root)
    code = """
import os, sys
import okf_loom.upgrade as migration
write = migration._Publication.publish
def die(target):
    write(target)
    os._exit(77)
migration._Publication.publish = die
migration.apply_upgrade(migration.plan_upgrade(sys.argv[1], actor='process:exporter'))
"""
    env = {**os.environ, "PYTHONPATH": str(CLI.parent)}
    killed = subprocess.run([sys.executable, "-c", code, str(root)], env=env, timeout=30)
    assert killed.returncode == 77
    assert (root / "index.md").read_bytes() == original["index.md"]
    assert "generated" in parse_document((root / "a.md").read_text())[0]
    assert (root / "b.md").read_bytes() == original["b.md"]
    result = cli(root, "--apply", "--actor", "process:exporter")
    assert result.returncode == 0, result.stderr
    assert (root / "a.md").read_bytes() == (root / "b.md").read_bytes()
    assert plan_upgrade(root, actor="process:exporter").changes == {}


@pytest.mark.parametrize("metadata", [
    'timestamp: "not a time"',
    'timestamp: "2026-01-01T00:00:00"',
    'timestamp: "2026-01-01T00:00:00Z"\ngenerated: null',
    'timestamp: "2026-01-01T00:00:00Z"\ngenerated: {at: "2026-01-01T00:00:00Z"}',
])
def test_never_repairs_missing_actor_or_invalid_legacy_time(tmp_path, metadata):
    root = legacy(tmp_path)
    raw = f"---\ntype: Note\n{metadata}\n---\nBody\n".encode()
    (root / "a.md").write_bytes(raw)
    result = cli(root, "--apply", "--actor", "process:not-inferred")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "partial"
    assert (root / "a.md").read_bytes() == raw


def test_unparseable_concept_refuses_without_exposing_content(tmp_path):
    root = legacy(tmp_path)
    (root / "b.md").write_bytes(b"---\ntype: [SECRET\n---\n")
    before = inventory(root)
    result = cli(root, "--apply")
    assert result.returncode == 1 and "parse_failed" in result.stderr
    assert "SECRET" not in result.stderr
    assert inventory(root) == before


def test_config_failure_refuses_without_exposing_content(tmp_path):
    root = legacy(tmp_path)
    (root / "okf-loom.config.yaml").write_text("private: [SECRET\n")
    before = inventory(root)
    result = cli(root, "--apply")
    assert result.returncode == 1 and "config_invalid" in result.stderr
    assert "SECRET" not in result.stderr
    assert inventory(root) == before


def test_symlink_source_is_not_adopted(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    legacy(root)
    outside = tmp_path / "external.md"
    outside.write_bytes((root / "a.md").read_bytes())
    (root / "a.md").unlink()
    (root / "a.md").symlink_to(outside)
    before = inventory(root)
    external = outside.read_bytes()
    result = cli(root, "--apply", "--actor", "process:exporter")
    assert result.returncode == 1 and "unsafe_path" in result.stderr
    assert inventory(root) == before and outside.read_bytes() == external


def test_ambiguous_root_header_is_not_reserialized(tmp_path):
    root = legacy(tmp_path)
    (root / "index.md").write_bytes(b'---\n"okf_version": "0.1"\ncustom: [a, b]\n---\nAuthored\n')
    before = inventory(root)
    result = cli(root, "--apply", "--actor", "process:exporter")
    assert result.returncode == 1
    # The built-in parser rejects quoted mapping keys at admission; PyYAML
    # accepts them but the conservative header editor refuses to reserialize.
    assert "header_ambiguous" in result.stderr or "index.parse_failed" in result.stderr
    assert inventory(root) == before


def source_state(root):
    return {name: ((root / name).stat().st_ino, (root / name).read_bytes())
            for name in ('a.md', 'index.md')}


@pytest.mark.parametrize('leaf', ['a.md', 'index.md'])
@pytest.mark.parametrize('failure', [OSError, KeyboardInterrupt])
def test_exchange_completed_then_writer_raises_restores_every_inode(tmp_path, monkeypatch, leaf, failure):
    """Record attempted publication before call, including declaration rollback."""
    import okf_loom.upgrade as migration
    root = legacy(tmp_path); before = source_state(root)
    exchange = migration._exchange
    fired = False

    def interrupted(stage, entry, parent, name):
        nonlocal fired
        exchange(stage, entry, parent, name)
        if not fired and name == leaf:
            fired = True
            raise failure('after successful exchange')

    monkeypatch.setattr(migration, '_exchange', interrupted)
    with pytest.raises(failure, match='after successful exchange'):
        apply_upgrade(plan_upgrade(root, actor='process:exporter'))
    assert fired and source_state(root) == before
    assert not list(root.glob('.okf-upgrade-*'))


@pytest.mark.parametrize('leaf', ['a.md', 'index.md'])
@pytest.mark.parametrize('same_bytes', [True, False])
@pytest.mark.parametrize('seam', ['writer_entry', 'exchange_syscall'])
def test_leaf_substitution_preserves_actual_new_inode_and_bytes(tmp_path, monkeypatch, leaf, same_bytes, seam):
    import os
    import okf_loom.upgrade as migration
    root = legacy(tmp_path); before = source_state(root)
    plan = plan_upgrade(root, actor='process:exporter')
    newer = before[leaf][1] if same_bytes else b'NEWER NONCOOPERATING WRITER BYTES'
    replacement_inode = None
    exchange, publish = migration._exchange, migration._Publication.publish

    def substitute():
        nonlocal replacement_inode
        path = root / 'replacement'
        path.write_bytes(newer)
        replacement_inode = path.stat().st_ino
        os.replace(path, root / leaf)

    def at_syscall(stage, entry, parent, name):
        if name == leaf and replacement_inode is None:
            substitute()
        return exchange(stage, entry, parent, name)

    def at_entry(target):
        if target.path.name == leaf and replacement_inode is None:
            substitute()
        return publish(target)

    monkeypatch.setattr(migration, '_exchange', at_syscall if seam == 'exchange_syscall' else exchange)
    monkeypatch.setattr(migration._Publication, 'publish', at_entry if seam == 'writer_entry' else publish)
    with pytest.raises(UpgradeError, match='rev_conflict'):
        apply_upgrade(plan)
    assert source_state(root)[leaf] == (replacement_inode, newer)
    untouched = 'index.md' if leaf == 'a.md' else 'a.md'
    assert source_state(root)[untouched] == before[untouched]
    assert not list(root.glob('.okf-upgrade-*'))


@pytest.mark.parametrize('seam', ['writer_entry', 'exchange_syscall'])
def test_parent_symlink_substitution_never_writes_external_source(tmp_path, monkeypatch, seam):
    import okf_loom.upgrade as migration
    root, external = tmp_path / 'bundle', tmp_path / 'external'
    root.mkdir(); external.mkdir(); legacy(root)
    (root / 'notes').mkdir(); (root / 'a.md').rename(root / 'notes/a.md')
    original = (root / 'notes/a.md').read_bytes(); index = (root / 'index.md').read_bytes()
    (external / 'a.md').write_bytes(b'EXTERNAL FILE MUST SURVIVE')
    external_inode = (external / 'a.md').stat().st_ino
    plan = plan_upgrade(root, actor='process:exporter')
    moved = tmp_path / 'original-parent'
    exchange, publish = migration._exchange, migration._Publication.publish

    def substitute():
        (root / 'notes').rename(moved)
        (root / 'notes').symlink_to(external, target_is_directory=True)

    def at_syscall(stage, entry, parent, name):
        if name == 'a.md' and not moved.exists():
            substitute()
        return exchange(stage, entry, parent, name)

    def at_entry(target):
        if target.path.name == 'a.md' and not moved.exists():
            substitute()
        return publish(target)

    monkeypatch.setattr(migration, '_exchange', at_syscall if seam == 'exchange_syscall' else exchange)
    monkeypatch.setattr(migration._Publication, 'publish', at_entry if seam == 'writer_entry' else publish)
    with pytest.raises((UpgradeError, OSError)):
        apply_upgrade(plan)
    assert (external / 'a.md').read_bytes() == b'EXTERNAL FILE MUST SURVIVE'
    assert (external / 'a.md').stat().st_ino == external_inode
    assert (moved / 'a.md').read_bytes() == original
    assert (root / 'index.md').read_bytes() == index
    assert not list(moved.glob('.okf-upgrade-*'))


@pytest.mark.parametrize('leaf', ['a.md', 'index.md'])
@pytest.mark.parametrize('boundary', ['directory_open', 'directory_fsync'])
def test_post_exchange_directory_failure_rolls_back_including_current_target(tmp_path, monkeypatch, leaf, boundary):
    import os
    import stat
    import okf_loom.upgrade as migration
    root = legacy(tmp_path); before = source_state(root)
    exchange, real_open, real_fsync = migration._exchange, os.open, os.fsync
    armed = False
    fired = False

    def arm(stage, entry, parent, name):
        nonlocal armed
        exchange(stage, entry, parent, name)
        if name == leaf and not fired:
            armed = True

    def fail_open(path, flags, *args, **kwargs):
        nonlocal fired
        if armed and not fired and flags & os.O_DIRECTORY:
            fired = True
            raise OSError('directory open after exchange')
        return real_open(path, flags, *args, **kwargs)

    def fail_fsync(fd):
        nonlocal fired
        if armed and not fired and stat.S_ISDIR(os.fstat(fd).st_mode):
            fired = True
            raise OSError('directory fsync after exchange')
        return real_fsync(fd)

    monkeypatch.setattr(migration, '_exchange', arm)
    monkeypatch.setattr(os, 'open' if boundary == 'directory_open' else 'fsync',
                        fail_open if boundary == 'directory_open' else fail_fsync)
    with pytest.raises(OSError, match='after exchange'):
        apply_upgrade(plan_upgrade(root, actor='process:exporter'))
    assert fired and source_state(root) == before
    assert not list(root.glob('.okf-upgrade-*'))


def test_persistent_directory_fsync_failure_reports_uncertainty_and_retains_evidence(tmp_path, monkeypatch):
    import os
    import stat
    import okf_loom.upgrade as migration
    root = legacy(tmp_path); before = source_state(root)
    exchange, fsync = migration._exchange, os.fsync
    armed = False

    def arm(*args):
        nonlocal armed
        exchange(*args); armed = True

    def fail(fd):
        if armed and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('persistent directory fsync failure')
        return fsync(fd)

    monkeypatch.setattr(migration, '_exchange', arm)
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(UpgradeError, match='rollback_uncertain') as caught:
        apply_upgrade(plan_upgrade(root, actor='process:exporter'))
    assert source_state(root) == before  # bytes restored, durability not certified
    assert list(root.glob('.okf-upgrade-*/entry'))
    assert 'ambiguous' not in str(caught.value)  # no source-content error dump


def test_writer_at_rollback_exchange_preserved_with_original_recovery_evidence(tmp_path, monkeypatch):
    import os
    import okf_loom.upgrade as migration
    root = legacy(tmp_path); before = source_state(root)
    exchange, publish = migration._exchange, migration._Publication.publish
    calls = 0
    newer_inode = None

    def fail_index(target):
        if target.path.name == 'index.md':
            raise OSError('request rollback')
        publish(target)

    def race(stage, entry, parent, name):
        nonlocal calls, newer_inode
        calls += 1
        if calls == 2:  # reverse exchange for a.md
            (root / 'newer').write_bytes(b'LATEST USER EDIT')
            newer_inode = (root / 'newer').stat().st_ino
            os.replace(root / 'newer', root / 'a.md')
        exchange(stage, entry, parent, name)

    monkeypatch.setattr(migration._Publication, 'publish', fail_index)
    monkeypatch.setattr(migration, '_exchange', race)
    with pytest.raises(UpgradeError, match='rollback_uncertain'):
        apply_upgrade(plan_upgrade(root, actor='process:exporter'))
    assert source_state(root)['a.md'] == (newer_inode, b'LATEST USER EDIT')
    assert source_state(root)['index.md'] == before['index.md']
    assert any(p.read_bytes() == before['a.md'][1] for p in root.glob('.okf-upgrade-*/entry'))


def test_no_ordinary_rename_fallback_when_exchange_unavailable(tmp_path, monkeypatch):
    import okf_loom.upgrade as migration
    root = legacy(tmp_path); before = source_state(root)

    def unsupported(*_):
        raise OSError('RENAME_EXCHANGE unavailable')

    monkeypatch.setattr(migration, '_exchange', unsupported)
    with pytest.raises(OSError, match='unavailable'):
        apply_upgrade(plan_upgrade(root, actor='process:exporter'))
    assert source_state(root) == before
    assert not list(root.glob('.okf-upgrade-*'))


def test_leaf_symlink_at_exchange_is_restored_without_dereferencing(tmp_path, monkeypatch):
    import okf_loom.upgrade as migration
    root = tmp_path / 'bundle'; root.mkdir(); legacy(root)
    outside = tmp_path / 'external.md'; outside.write_bytes(b'EXTERNAL PRIVATE CONTENT')
    before_index = (root / 'index.md').read_bytes()
    exchange = migration._exchange
    fired = False

    def swap(stage, entry, parent, name):
        nonlocal fired
        if name == 'a.md' and not fired:
            fired = True
            (root / 'a.md').unlink()
            (root / 'a.md').symlink_to(outside)
        exchange(stage, entry, parent, name)

    monkeypatch.setattr(migration, '_exchange', swap)
    with pytest.raises(UpgradeError, match='rev_conflict') as caught:
        apply_upgrade(plan_upgrade(root, actor='process:exporter'))
    assert fired and (root / 'a.md').is_symlink()
    assert outside.read_bytes() == b'EXTERNAL PRIVATE CONTENT'
    assert (root / 'index.md').read_bytes() == before_index
    assert 'EXTERNAL PRIVATE CONTENT' not in str(caught.value)
    assert not list(root.glob('.okf-upgrade-*'))
