"""Pinned-directory directive authority regression proofs."""
from __future__ import annotations

import json
import multiprocessing
import os
import stat
import threading
import time
from pathlib import Path

import pytest

import okf_loom.directive_store as module
from okf_loom.directive_store import DirectiveStore, DirectiveStoreError


def _row(comment_id: str) -> dict[str, object]:
    return {"id": comment_id, "concept": "tables/orders", "state": "open",
            "archived": False}


def _seed(session: Path, comment_id: str) -> None:
    session.mkdir(parents=True)
    (session / "..directives-transaction.okf-lock").write_bytes(b"")
    (session / "directives.jsonl").write_text(json.dumps(_row(comment_id)) + "\n")


def test_reader_parent_substitution_never_reads_replacement(tmp_path: Path) -> None:
    session = tmp_path / "session"
    original = tmp_path / "original"
    _seed(session, "ORIGINAL")
    with DirectiveStore(session).reader() as transaction:
        session.rename(original)
        _seed(session, "REPLACEMENT")
        assert [row["id"] for row in transaction.rows()] == ["ORIGINAL"]


def test_writer_append_checkpoint_rotation_stay_on_pinned_parent(tmp_path: Path) -> None:
    session = tmp_path / "session"
    original = tmp_path / "original"
    _seed(session, "ORIGINAL")
    with DirectiveStore(session).writer() as transaction:
        session.rename(original)
        _seed(session, "SENTINEL")
        sentinel = (session / "directives.jsonl").read_bytes()
        transaction.append(_row("APPENDED"), max_bytes=1, keep=1)
        transaction.checkpoint()
        assert (session / "directives.jsonl").read_bytes() == sentinel
        assert not (session / "directives-checkpoint.json").exists()
    assert b"APPENDED" in (original / "directives.jsonl").read_bytes()
    assert (original / "directives-checkpoint.json").is_file()
    assert list(original.glob("directives-*-*.jsonl"))


def _child_swap(session: str, original: str) -> None:
    path = Path(session)
    path.rename(original)
    _seed(path, "CHILD-REPLACEMENT")


def test_cross_process_substitution_retains_original_authority(tmp_path: Path) -> None:
    session, original = tmp_path / "session", tmp_path / "original"
    _seed(session, "ORIGINAL")
    with DirectiveStore(session).reader() as transaction:
        process = multiprocessing.Process(target=_child_swap, args=(str(session), str(original)))
        process.start(); process.join(10)
        assert process.exitcode == 0
        assert [row["id"] for row in transaction.rows()] == ["ORIGINAL"]


@pytest.mark.parametrize("name", [
    "directives.jsonl", "directives-checkpoint.json",
    "directives-20260717-000000000001.jsonl", ".directives.jsonl.rotation-seq",
])
@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_every_directive_leaf_type_fails_closed(
        tmp_path: Path, name: str, kind: str) -> None:
    session = tmp_path / "session"
    session.mkdir()
    (session / "..directives-transaction.okf-lock").write_bytes(b"")
    leaf = session / name
    if kind == "symlink":
        sentinel = tmp_path / "sentinel"
        sentinel.write_text("7")
        leaf.symlink_to(sentinel)
    else:
        leaf.mkdir()
    with DirectiveStore(session).writer() as transaction:
        with pytest.raises((DirectiveStoreError, OSError, ValueError)):
            if name == ".directives.jsonl.rotation-seq":
                (session / "directives.jsonl").write_bytes(b"{}\n")
                transaction.append(_row("A"), max_bytes=1, keep=1)
            else:
                transaction.rows(fail_on_torn=True)


def test_missing_reader_and_unsupported_platform_fail_without_creation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(OSError):
        with DirectiveStore(missing).reader():
            pass
    assert not missing.exists()
    unsupported = tmp_path / "unsupported"
    monkeypatch.delattr(os, "O_NOFOLLOW")
    assert module._capable() is False
    with pytest.raises(DirectiveStoreError):
        with DirectiveStore(unsupported).reader():
            pass
    with pytest.raises(DirectiveStoreError):
        with DirectiveStore(unsupported).writer():
            pass
    assert not unsupported.exists()


def test_initializer_pins_session_before_lock_creation_on_substitution(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = tmp_path / "session"
    displaced = tmp_path / "displaced"
    session.mkdir()
    original_fstat = module.os.fstat
    swapped = False

    def substitute_after_pin(fd: int):
        nonlocal swapped
        result = original_fstat(fd)
        if not swapped and stat.S_ISDIR(result.st_mode):
            swapped = True
            session.rename(displaced)
            session.mkdir()
        return result

    monkeypatch.setattr(module.os, "fstat", substitute_after_pin)
    with pytest.raises(DirectiveStoreError, match="substituted"):
        with DirectiveStore(session).initialize():
            pass

    assert swapped
    assert not (session / "..directives-transaction.okf-lock").exists()
    assert not (displaced / "..directives-transaction.okf-lock").exists()


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_initializer_rejects_non_regular_lock_without_mutation(
        tmp_path: Path, kind: str) -> None:
    session = tmp_path / "session"
    session.mkdir()
    lock = session / "..directives-transaction.okf-lock"
    outside = tmp_path / "outside"
    outside.write_bytes(b"sentinel")
    if kind == "symlink":
        lock.symlink_to(outside)
    else:
        lock.mkdir()
    before = outside.read_bytes()

    with pytest.raises(DirectiveStoreError, match="lock"):
        with DirectiveStore(session).initialize():
            pass

    assert outside.read_bytes() == before
    assert lock.is_symlink() if kind == "symlink" else lock.is_dir()


def test_initializer_rejects_multiply_linked_lock(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    lock = session / "..directives-transaction.okf-lock"
    alias = tmp_path / "lock-alias"
    lock.write_bytes(b"")
    os.link(lock, alias)

    with pytest.raises(DirectiveStoreError, match="stable regular file"):
        with DirectiveStore(session).initialize():
            pass

    assert lock.stat().st_nlink == 2
    assert alias.stat().st_nlink == 2


@pytest.mark.parametrize("name,bad", [
    ("directives.jsonl", b"{bad\n"),
    ("directives-checkpoint.json", b"{bad"),
])
def test_initializer_rejects_corrupt_unlocked_authority_without_mutation(
        tmp_path: Path, name: str, bad: bytes) -> None:
    session = tmp_path / "session"
    session.mkdir()
    authority = session / name
    authority.write_bytes(bad)
    before = {path.name: path.read_bytes() for path in session.iterdir()}

    with pytest.raises((DirectiveStoreError, ValueError)):
        with DirectiveStore(session).initialize():
            pass

    assert not (session / "..directives-transaction.okf-lock").exists()
    assert {path.name: path.read_bytes() for path in session.iterdir()} == before


def test_concurrent_fresh_initializers_share_one_pinned_lock(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    barrier = threading.Barrier(8)
    identities: list[tuple[int, int]] = []
    errors: list[BaseException] = []

    def initialize() -> None:
        try:
            barrier.wait(timeout=5)
            with DirectiveStore(session).initialize():
                pass
            lock_info = (session / "..directives-transaction.okf-lock").stat()
            identities.append((lock_info.st_dev, lock_info.st_ino))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=initialize) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert len(set(identities)) == 1


def test_initializer_accepts_valid_legacy_data_without_lock(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    active = session / "directives.jsonl"
    active.write_text(json.dumps(_row("LEGACY")) + "\n", encoding="utf-8")

    with DirectiveStore(session).initialize() as transaction:
        assert [row["id"] for row in transaction.rows()] == ["LEGACY"]

    assert (session / "..directives-transaction.okf-lock").is_file()


def test_initializer_tolerates_torn_active_tail_without_rewriting_journal(
        tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    active = session / "directives.jsonl"
    raw = (json.dumps(_row("LEGACY")) + "\n{torn").encode()
    active.write_bytes(raw)

    with DirectiveStore(session).initialize() as transaction:
        assert [row["id"] for row in transaction.rows()] == ["LEGACY"]

    assert active.read_bytes() == raw
    assert not (session / "directives-checkpoint.json").exists()
    assert not list(session.glob("directives-*-*.jsonl"))


def test_initializer_rejects_ambiguous_legacy_chronology_before_lock_creation(
        tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    checkpoint = session / "directives-checkpoint.json"
    rotation = session / "directives-20260717-000000000001.jsonl"
    active = session / "directives.jsonl"
    checkpoint.write_text(json.dumps({"version": 1, "rows": [_row("CHECKPOINT")]}))
    rotation.write_text(json.dumps(_row("ROTATION")) + "\n")
    active.write_text(json.dumps(_row("ACTIVE")) + "\n")
    before = {path.name: path.read_bytes() for path in session.iterdir()}

    with pytest.raises(DirectiveStoreError, match="chronology is unprovable"):
        with DirectiveStore(session).initialize():
            pass

    assert not (session / "..directives-transaction.okf-lock").exists()
    assert {path.name: path.read_bytes() for path in session.iterdir()} == before


def test_lock_replacement_immediately_before_flock_never_enters_transaction(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl

    session = tmp_path / "session"
    _seed(session, "ORIGINAL")
    lock = session / "..directives-transaction.okf-lock"
    original_flock = fcntl.flock
    replacement_fd = -1
    replaced = False
    entered = False
    before_fds = len(os.listdir("/proc/self/fd"))

    def replace_then_flock(fd: int, operation: int) -> None:
        nonlocal replacement_fd, replaced
        if (not replaced and operation == fcntl.LOCK_EX
                and stat.S_ISREG(os.fstat(fd).st_mode)):
            replaced = True
            lock.unlink()
            replacement_fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        original_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", replace_then_flock)
    try:
        with pytest.raises(DirectiveStoreError, match="stable regular file"):
            with DirectiveStore(session).writer():
                entered = True
        assert replaced
        assert not entered
        # The canonical replacement is independent and remains lockable; the
        # stale inode lock must not have authorized the transaction.
        original_flock(replacement_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original_flock(replacement_fd, fcntl.LOCK_UN)
        assert json.loads((session / "directives.jsonl").read_text())["id"] == "ORIGINAL"
    finally:
        if replacement_fd >= 0:
            os.close(replacement_fd)
    assert len(os.listdir("/proc/self/fd")) == before_fds


def test_directory_flock_prevents_split_brain_after_final_leaf_validation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = tmp_path / "session"
    _seed(session, "ORIGINAL")
    lock = session / "..directives-transaction.okf-lock"
    validated = threading.Event()
    release_validation = threading.Event()
    a_entered = threading.Event()
    release_a = threading.Event()
    b_entered = threading.Event()
    errors: list[BaseException] = []
    original_validate = DirectiveStore._validate_canonical_lock
    a_ident: list[int] = []
    calls = 0

    def pause_after_final_validation(dir_fd: int, lock_fd: int) -> None:
        nonlocal calls
        original_validate(dir_fd, lock_fd)
        if threading.get_ident() == a_ident[0]:
            calls += 1
            if calls == 2:
                validated.set()
                assert release_validation.wait(10)

    monkeypatch.setattr(
        DirectiveStore, "_validate_canonical_lock",
        staticmethod(pause_after_final_validation),
    )

    def transaction_a() -> None:
        try:
            a_ident.append(threading.get_ident())
            with DirectiveStore(session).writer() as transaction:
                a_entered.set()
                transaction.append(_row("A"), max_bytes=1_000_000, keep=1)
                assert release_a.wait(10)
        except BaseException as exc:
            errors.append(exc)

    def transaction_b() -> None:
        try:
            with DirectiveStore(session).writer() as transaction:
                b_entered.set()
                transaction.append(_row("B"), max_bytes=1_000_000, keep=1)
        except BaseException as exc:
            errors.append(exc)

    a = threading.Thread(target=transaction_a)
    a.start()
    assert validated.wait(10)
    lock.unlink()
    lock.write_bytes(b"")
    b = threading.Thread(target=transaction_b)
    b.start()
    time.sleep(0.05)
    assert not b_entered.is_set()

    release_validation.set()
    assert a_entered.wait(10)
    time.sleep(0.05)
    assert not b_entered.is_set()
    release_a.set()
    a.join(10)
    b.join(10)

    assert not errors
    assert not a.is_alive() and not b.is_alive()
    assert b_entered.is_set()
    rows = [json.loads(line) for line in
            (session / "directives.jsonl").read_text().splitlines()]
    assert [row["id"] for row in rows] == ["ORIGINAL", "A", "B"]


def _process_initialize(session: str, ready, release, result) -> None:
    try:
        with DirectiveStore(session).initialize():
            ready.set()
            release.wait(10)
        result.put("ok")
    except BaseException as exc:
        result.put(f"error:{type(exc).__name__}:{exc}")


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_process_and_thread_initializers_serialize_on_canonical_lock(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    context = multiprocessing.get_context("fork")
    ready, release = context.Event(), context.Event()
    result = context.Queue()
    process = context.Process(
        target=_process_initialize,
        args=(str(session), ready, release, result),
    )
    process.start()
    assert ready.wait(10)
    entered = threading.Event()
    errors: list[BaseException] = []

    def thread_initialize() -> None:
        try:
            with DirectiveStore(session).initialize():
                entered.set()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=thread_initialize)
    thread.start()
    time.sleep(0.05)
    assert not entered.is_set()
    release.set()
    process.join(10)
    thread.join(10)

    assert process.exitcode == 0
    assert result.get(timeout=2) == "ok"
    assert not errors
    assert entered.is_set()
    assert not thread.is_alive()


def test_transaction_fds_close_on_success_and_failure(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _seed(session, "A")
    before = len(os.listdir("/proc/self/fd"))
    with DirectiveStore(session).reader() as transaction:
        assert transaction.rows()
    with pytest.raises(RuntimeError):
        with DirectiveStore(session).reader():
            raise RuntimeError("boom")
    assert len(os.listdir("/proc/self/fd")) == before


def test_transaction_is_inactive_after_exit_even_when_fd_number_is_reused(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _seed(session, "A")
    with DirectiveStore(session).writer() as transaction:
        assert transaction.rows()
    # Encourage reuse of one of the just-closed low descriptor numbers.
    reused = [os.open("/dev/null", os.O_RDONLY) for _ in range(8)]
    try:
        for operation in (
            lambda: transaction.rows(),
            lambda: transaction.physical_rows("id", "A"),
            transaction.checkpoint,
            lambda: transaction.append(_row("B"), max_bytes=100, keep=1),
        ):
            with pytest.raises(DirectiveStoreError, match="inactive"):
                operation()
    finally:
        for fd in reused:
            os.close(fd)


def test_same_thread_nested_transaction_fails_fast(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _seed(session, "A")
    started = time.monotonic()
    with DirectiveStore(session).reader():
        with pytest.raises(DirectiveStoreError, match="nested"):
            with DirectiveStore(session).writer():
                pass
    assert time.monotonic() - started < 1


def test_writer_ownership_is_mode_scoped_and_thread_local(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _seed(session, "A")
    store = DirectiveStore(session)
    observed: list[bool] = []

    with store.reader():
        assert store.writer_owned_by_current_thread() is False
    with store.writer():
        assert store.writer_owned_by_current_thread() is True
        thread = threading.Thread(
            target=lambda: observed.append(store.writer_owned_by_current_thread()))
        thread.start(); thread.join(10)
        assert not thread.is_alive()
        assert observed == [False]
    assert store.writer_owned_by_current_thread() is False


def test_writer_ownership_rejects_substituted_session_path(tmp_path: Path) -> None:
    session = tmp_path / "session"
    displaced = tmp_path / "displaced"
    _seed(session, "A")
    store = DirectiveStore(session)
    with store.writer():
        assert store.writer_owned_by_current_thread() is True
        session.rename(displaced)
        _seed(session, "REPLACEMENT")
        assert store.writer_owned_by_current_thread() is False


def test_checkpoint_rejects_malformed_complete_row_without_replacement_or_prune(
        tmp_path: Path) -> None:
    session = tmp_path / "session"
    _seed(session, "A")
    checkpoint = session / "directives-checkpoint.json"
    checkpoint.write_text(json.dumps({"version": 1, "through_rotation": 0,
                                      "rows": [_row("CHECKPOINT")]}) + "\n")
    rotation = session / "directives-20260717-000000000001.jsonl"
    rotation.write_text(json.dumps({"id": "", "concept": "tables/orders",
                                    "state": "open", "archived": False}) + "\n")
    before_checkpoint = checkpoint.read_bytes()
    before_rotation = rotation.read_bytes()
    with DirectiveStore(session).writer() as transaction:
        with pytest.raises(DirectiveStoreError, match="corrupt directive row"):
            transaction.checkpoint()
    assert checkpoint.read_bytes() == before_checkpoint
    assert rotation.read_bytes() == before_rotation


def test_rotation_preflight_preserves_all_authority_on_malformed_complete_row(
        tmp_path: Path) -> None:
    session = tmp_path / "session"
    _seed(session, "A")
    checkpoint = session / "directives-checkpoint.json"
    checkpoint.write_text(json.dumps({"version": 1, "through_rotation": 0,
                                      "rows": [_row("CHECKPOINT")]}) + "\n")
    rotation = session / "directives-20260717-000000000001.jsonl"
    rotation.write_text(json.dumps({"id": "BAD", "concept": "",
                                    "state": "open", "archived": False}) + "\n")
    before = {path.name: path.read_bytes() for path in session.iterdir() if path.is_file()}
    with DirectiveStore(session).writer() as transaction:
        with pytest.raises(DirectiveStoreError, match="corrupt directive row"):
            transaction.append(_row("NEW"), max_bytes=1, keep=0)
    after = {path.name: path.read_bytes() for path in session.iterdir() if path.is_file()}
    assert after == before


def test_prune_directory_fsync_failure_is_not_false_success(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = tmp_path / "session"
    _seed(session, "OLD")
    old_rotation = session / "directives-20260717-000000000001.jsonl"
    old_rotation.write_text(json.dumps(_row("OLDER")) + "\n")
    real_unlink = os.unlink
    real_fsync = os.fsync
    pruned = False

    def tracking_unlink(path, *args, **kwargs):
        nonlocal pruned
        result = real_unlink(path, *args, **kwargs)
        if path == old_rotation.name:
            pruned = True
        return result

    def failing_fsync(fd: int) -> None:
        if pruned and stat_is_directory(fd):
            raise OSError("injected directory fsync failure after prune")
        real_fsync(fd)

    def stat_is_directory(fd: int) -> bool:
        import stat
        return stat.S_ISDIR(os.fstat(fd).st_mode)

    monkeypatch.setattr(module, "_unlink", tracking_unlink)
    monkeypatch.setattr(module, "_fsync", failing_fsync)
    with DirectiveStore(session).writer() as transaction:
        with pytest.raises(OSError, match="injected"):
            transaction.append(_row("NEW"), max_bytes=1, keep=1)
    # Ordering is rotate -> append -> checkpoint -> prune. The committed row
    # and checkpoint exist, but failed prune durability is never acknowledged.
    assert b"NEW" in (session / "directives.jsonl").read_bytes()
    assert (session / "directives-checkpoint.json").is_file()
    assert not old_rotation.exists()
