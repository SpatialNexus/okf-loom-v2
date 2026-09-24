from __future__ import annotations

import base64
import copy
from pathlib import Path
import threading
import time

import pytest

from okf_loom.model import Bundle
from okf_loom.io_utils import bundle_transaction_lock
from okf_loom.proposal_review.canonical import canonical_digest, sha256_hex
from okf_loom.proposal_snapshot import (SnapshotCaptureError, capture_bundle,
                                        frozen_validation_policy)
from okf_loom.proposal_snapshot import DEFAULT_RUNTIME_IDENTITIES
from okf_loom.proposal_review.ports import RuntimeIdentities


NOW = "2026-07-14T00:00:00.000Z"


def _concept(title: str = "A") -> str:
    return f"---\ntype: Note\ntitle: {title}\ndescription: d\ntags: [x]\ntimestamp: '2026-01-01'\n---\n\nbody\n"


def test_capture_visibility_hashes_and_context_are_exact(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(_concept())
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "b.md").write_text(_concept("B"))
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "asset.bin").write_bytes(b"asset")
    captured = capture_bundle(tmp_path, captured_at=NOW)
    live = Bundle.load(tmp_path)
    assert set(captured.concept_bytes) == {"/".join(c) for c in live.concepts} == {"a"}
    assert captured.concept_revisions["a"] == sha256_hex(captured.concept_bytes["a"])
    projection = captured.facts.validation_context_projection()
    assert captured.validation_context_digest == canonical_digest(projection)
    assert base64.b64decode(projection["scan_controls"][".gitignore"]["bytes_base64"]) == b"ignored/\n"
    assert frozen_validation_policy("producer")["profile"] == "producer"


def test_lock_implementation_artifacts_do_not_change_facts_or_context(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(_concept())
    first = capture_bundle(tmp_path, captured_at=NOW)
    (tmp_path / ".okf-loom-bundle.lock").write_bytes(b"different lock bytes")
    (tmp_path / ".a.md.okf-lock").write_bytes(b"editor lock")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "x.okf-lock").write_bytes(b"nested lock")
    second = capture_bundle(tmp_path, captured_at=NOW)
    assert first.facts.validation_context_projection() == second.facts.validation_context_projection()
    assert first.validation_context_digest == second.validation_context_digest
    assert all("okf-lock" not in fact.path for fact in second.facts.visible_non_concept_files)


def test_runtime_identity_drift_changes_snapshot_freshness_authority(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(_concept())
    first = capture_bundle(tmp_path, captured_at=NOW)
    changed = RuntimeIdentities(
        validator_id="okf-validator@2.0+sha256:" + "a" * 64,
        serializer_id=DEFAULT_RUNTIME_IDENTITIES.serializer_id,
        operation_runtime_id=DEFAULT_RUNTIME_IDENTITIES.operation_runtime_id,
    )
    second = capture_bundle(tmp_path, captured_at=NOW, identities=changed)
    assert first.validation_context_digest == second.validation_context_digest
    assert first.snapshot_digest != second.snapshot_digest


def test_unsafe_backslash_name_and_excessive_depth_fail_typed(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(_concept())
    (tmp_path / "bad\\name.bin").write_bytes(b"x")
    with pytest.raises(SnapshotCaptureError) as unsafe:
        capture_bundle(tmp_path, captured_at=NOW)
    assert unsafe.value.code == "snapshot_unsafe"
    (tmp_path / "bad\\name.bin").unlink()
    current = tmp_path
    for _ in range(66):
        current = current / "d"
        current.mkdir()
    with pytest.raises(SnapshotCaptureError) as deep:
        capture_bundle(tmp_path, captured_at=NOW)
    assert deep.value.code == "snapshot_limit_exceeded"


def test_visible_symlink_and_root_symlink_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(_concept())
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text(_concept("outside"))
    (tmp_path / "bad.md").symlink_to(outside)
    with pytest.raises(SnapshotCaptureError) as error:
        capture_bundle(tmp_path, captured_at=NOW)
    assert error.value.code == "snapshot_unsafe"
    (tmp_path / "bad.md").unlink()
    root_link = tmp_path.parent / f"{tmp_path.name}-link"
    root_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(SnapshotCaptureError):
        capture_bundle(root_link, captured_at=NOW)


def test_shared_capture_timeout_is_retryable_bundle_busy(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(_concept())
    held = threading.Event()
    release = threading.Event()

    def writer() -> None:
        with bundle_transaction_lock(tmp_path, "exclusive"):
            held.set()
            release.wait(5)

    thread = threading.Thread(target=writer)
    thread.start()
    assert held.wait(2)
    try:
        with pytest.raises(SnapshotCaptureError) as error:
            capture_bundle(tmp_path, captured_at=NOW, lock_timeout_seconds=0)
        assert error.value.code == "bundle_busy" and error.value.retryable
    finally:
        release.set()
        thread.join(2)


def test_unlocked_edit_between_scans_retries_from_scratch(tmp_path: Path, monkeypatch) -> None:
    import okf_loom.proposal_snapshot as adapter

    (tmp_path / "a.md").write_text(_concept())
    original = adapter._scan
    calls = 0

    def racing(fd: int):
        nonlocal calls
        calls += 1
        result = original(fd)
        if calls == 1:
            (tmp_path / "b.md").write_text(_concept("B"))
        return result

    monkeypatch.setattr(adapter, "_scan", racing)
    captured = capture_bundle(tmp_path, captured_at=NOW)
    assert calls == 4
    assert set(captured.concept_bytes) == {"a", "b"}


def test_instability_retry_uses_one_lock_deadline_and_leaks_no_fds(tmp_path: Path, monkeypatch) -> None:
    import os
    import okf_loom.proposal_snapshot as adapter

    (tmp_path / "a.md").write_text(_concept())
    original = adapter._scan
    calls = 0
    before_fds = len(tuple(Path("/proc/self/fd").iterdir()))

    def slow_unstable(fd: int):
        nonlocal calls
        calls += 1
        result = original(fd)
        time.sleep(0.03)
        if calls == 1:
            (tmp_path / "b.md").write_text(_concept("B"))
        return result

    monkeypatch.setattr(adapter, "_scan", slow_unstable)
    started = time.monotonic()
    with pytest.raises(SnapshotCaptureError) as error:
        capture_bundle(tmp_path, captured_at=NOW, lock_timeout_seconds=0.04)
    elapsed = time.monotonic() - started
    assert error.value.code == "bundle_busy" and error.value.retryable
    assert elapsed < 0.20 and calls == 2
    assert len(tuple(Path("/proc/self/fd").iterdir())) == before_fds


def test_nested_git_marker_identity_is_bound_and_replacement_retries(tmp_path: Path, monkeypatch) -> None:
    import okf_loom.proposal_snapshot as adapter

    (tmp_path / "a.md").write_text(_concept())
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    original = adapter._scan
    calls = 0

    def replace_marker(fd: int):
        nonlocal calls
        calls += 1
        result = original(fd)
        if calls == 1:
            (nested / ".git").rename(nested / ".git-retained")
            (nested / ".git").mkdir()
        return result

    monkeypatch.setattr(adapter, "_scan", replace_marker)
    captured = capture_bundle(tmp_path, captured_at=NOW)
    assert calls == 4
    marker = captured.facts.scan_controls["nested/.git#manifest"]
    assert all(key in marker for key in (b'"device"', b'"inode"', b'"mode"', b'"type"', b'"size"'))


def test_validation_context_digest_binds_every_normative_input_field(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("---\ntitle: A\n---\n")
    (tmp_path / "index.md").write_text("# Index\n")
    (tmp_path / "asset.bin").write_bytes(b"asset")
    (tmp_path / ".gitignore").write_text("ignored/\n")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    captured = capture_bundle(tmp_path, captured_at=NOW, profile="producer")
    base = captured.facts.validation_context_projection()
    baseline = canonical_digest(base)

    mutations = []
    def changed(mutator):
        value = copy.deepcopy(base)
        mutator(value)
        mutations.append(canonical_digest(value))

    changed(lambda v: v["reserved_files"]["index.md"].__setitem__("bytes_base64", "WA=="))
    for field, value in (("path", "other.bin"), ("kind", "asset"),
                         ("size", 6), ("revision", "sha256:" + "1" * 64)):
        changed(lambda v, f=field, x=value: v["visible_non_concept_files"][0].__setitem__(f, x))
    changed(lambda v: v["scan_controls"][".gitignore"].__setitem__("bytes_base64", "eA=="))
    changed(lambda v: v["scan_controls"]["nested/.git#manifest"].__setitem__("bytes_base64", "eA=="))
    warning_fields = (("code", "concept.parse_failed"), ("path", None), ("line", 7),
                      ("message", "changed"), ("fingerprint", "sha256:" + "2" * 64))
    for field, value in warning_fields:
        changed(lambda v, f=field, x=value: v["load_warnings"][0].__setitem__(f, x))
    changed(lambda v: v["validation_policy"].__setitem__("policy_id", "p@1.0+sha256:" + "3" * 64))
    changed(lambda v: v["validation_policy"].__setitem__("profile", "spec"))
    for field, value in (("scope", "bundle"), ("base_severity", "info"),
                         ("severity", "info"), ("fails_profile", True),
                         ("parameters", {"x": 1})):
        changed(lambda v, f=field, x=value: v["validation_policy"]["rules"][0].__setitem__(f, x))
    assert mutations and all(digest != baseline for digest in mutations)

    # Regular-file inode/mode are scan-stability evidence, not frozen context.
    (tmp_path / "asset.bin").chmod(0o600)
    again = capture_bundle(tmp_path, captured_at=NOW, profile="producer")
    assert again.validation_context_digest == captured.validation_context_digest
    (tmp_path / "okf-loom.config.yaml").write_text("validate:\n  fail_on_broken_links: true\n")
    configured = capture_bundle(tmp_path, captured_at=NOW, profile="producer")
    assert configured.config_digest != captured.config_digest
    assert configured.validation_context_digest != captured.validation_context_digest
