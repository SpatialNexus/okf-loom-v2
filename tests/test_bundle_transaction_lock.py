from __future__ import annotations

import os
import inspect
import subprocess
import sys
import threading
import time
from argparse import Namespace
from pathlib import Path

import pytest

from okf_loom.bootstrap import bootstrap_bundle, import_directory
from okf_loom.cli import cmd_init
from okf_loom.index import regenerate_indexes
from okf_loom.io_utils import (
    BUNDLE_LOCK_FILENAME,
    BundleBusyError,
    BundleLockUpgradeError,
    assert_bundle_transaction_owned,
    bundle_transaction_lock,
)
from okf_loom.model import Bundle
from okf_loom.log import append_log_entry
from okf_loom.studio import Studio
from okf_loom.update import write_concept


def _run_in_thread(callback):
    started = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        started.set()
        try:
            callback()
        except BaseException as exc:  # surfaced in the owning test thread
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(1)
    return thread, finished, errors


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    scripts = str(Path(__file__).resolve().parents[1] / "scripts")
    env["PYTHONPATH"] = scripts + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_shared_owners_overlap(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    entered = threading.Event()

    def reader() -> None:
        with bundle_transaction_lock(root, "shared"):
            entered.set()

    with bundle_transaction_lock(root, "shared"):
        thread, finished, errors = _run_in_thread(reader)
        assert entered.wait(1)
        assert finished.wait(1)
    thread.join(1)
    assert not errors


@pytest.mark.parametrize("held,waiting", [("shared", "exclusive"),
                                            ("exclusive", "exclusive")])
def test_incompatible_modes_block_until_release(
    tmp_path: Path, held: str, waiting: str
) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    entered = threading.Event()

    def waiter() -> None:
        with bundle_transaction_lock(root, waiting):  # type: ignore[arg-type]
            entered.set()

    with bundle_transaction_lock(root, held):  # type: ignore[arg-type]
        thread, finished, errors = _run_in_thread(waiter)
        assert not entered.wait(0.1)
        assert not finished.is_set()
    assert entered.wait(1)
    thread.join(1)
    assert not errors


def test_nested_modes_and_upgrade_rejection(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    with bundle_transaction_lock(root, "exclusive"):
        assert_bundle_transaction_owned(root, "exclusive")
        with bundle_transaction_lock(root, "exclusive"):
            with bundle_transaction_lock(root, "shared"):
                assert_bundle_transaction_owned(root, "exclusive")
    with bundle_transaction_lock(root, "shared"):
        with bundle_transaction_lock(root, "shared"):
            assert_bundle_transaction_owned(root, "shared")
        with pytest.raises(BundleLockUpgradeError) as caught:
            with bundle_transaction_lock(root, "exclusive"):
                pass
    assert caught.value.code == "bundle_lock_upgrade_forbidden"


def test_timeout_is_exact_retryable_bundle_busy(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    caught: list[BaseException] = []

    def reader() -> None:
        try:
            with bundle_transaction_lock(root, "shared", timeout_seconds=0.02):
                pass
        except BaseException as exc:
            caught.append(exc)

    with bundle_transaction_lock(root, "exclusive"):
        thread, finished, errors = _run_in_thread(reader)
        assert finished.wait(1)
        thread.join(1)
        assert not errors
    assert len(caught) == 1
    assert isinstance(caught[0], BundleBusyError)
    assert caught[0].code == "bundle_busy"


def test_cross_process_exclusive_waits_for_shared_release(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    marker = tmp_path / "entered"
    code = (
        "from pathlib import Path; "
        "from okf_loom.io_utils import bundle_transaction_lock; "
        f"r=Path({str(root)!r}); m=Path({str(marker)!r}); "
        "lock=bundle_transaction_lock(r,'exclusive'); lock.__enter__(); "
        "m.write_text('entered'); lock.__exit__(None,None,None)"
    )
    with bundle_transaction_lock(root, "shared"):
        process = subprocess.Popen([sys.executable, "-c", code], env=_subprocess_env())
        time.sleep(0.15)
        assert not marker.exists()
    assert process.wait(timeout=3) == 0
    assert marker.read_text() == "entered"


def test_process_death_releases_bundle_lock(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    ready = tmp_path / "ready"
    code = (
        "import time; from pathlib import Path; "
        "from okf_loom.io_utils import bundle_transaction_lock; "
        f"r=Path({str(root)!r}); ready=Path({str(ready)!r}); "
        "lock=bundle_transaction_lock(r,'exclusive'); lock.__enter__(); "
        "ready.write_text('ready'); time.sleep(30)"
    )
    process = subprocess.Popen([sys.executable, "-c", code], env=_subprocess_env())
    for _ in range(100):
        if ready.exists():
            break
        time.sleep(0.01)
    assert ready.exists()
    process.kill()
    assert process.wait(timeout=3) != 0
    with bundle_transaction_lock(root, "shared", timeout_seconds=1):
        assert_bundle_transaction_owned(root, "shared")


def test_fork_child_drops_inherited_owner_fd(tmp_path: Path) -> None:
    """Parent lock -> fork child lives -> parent dies -> third process acquires."""
    root = tmp_path / "bundle"
    root.mkdir()
    parent_ready = tmp_path / "parent-ready"
    child_pid_path = tmp_path / "child-pid"
    code = (
        "import os,time; from pathlib import Path; "
        "from okf_loom.io_utils import bundle_transaction_lock; "
        f"r=Path({str(root)!r}); pr=Path({str(parent_ready)!r}); "
        f"cp=Path({str(child_pid_path)!r}); "
        "lock=bundle_transaction_lock(r,'exclusive'); lock.__enter__(); "
        "pid=os.fork(); "
        "(cp.write_text(str(os.getpid())),time.sleep(30),os._exit(0)) "
        "if pid==0 else (pr.write_text('ready'),time.sleep(30))"
    )
    parent = subprocess.Popen([sys.executable, "-c", code], env=_subprocess_env())
    for _ in range(200):
        if parent_ready.exists() and child_pid_path.exists():
            break
        time.sleep(0.01)
    assert parent_ready.exists() and child_pid_path.exists()
    child_pid = int(child_pid_path.read_text())
    parent.kill()
    parent.wait(timeout=3)
    try:
        probe = (
            "from pathlib import Path; "
            "from okf_loom.io_utils import bundle_transaction_lock; "
            f"r=Path({str(root)!r}); "
            "lock=bundle_transaction_lock(r,'exclusive',timeout_seconds=1); "
            "lock.__enter__(); lock.__exit__(None,None,None)"
        )
        assert subprocess.run(
            [sys.executable, "-c", probe], env=_subprocess_env(), timeout=3
        ).returncode == 0
        os.kill(child_pid, 0)  # child was still alive during third-party acquire
    finally:
        try:
            os.kill(child_pid, 9)
        except ProcessLookupError:
            pass


def test_lock_leaf_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("unchanged")
    (root / BUNDLE_LOCK_FILENAME).symlink_to(victim)
    with pytest.raises(OSError):
        with bundle_transaction_lock(root, "shared"):
            pass
    assert victim.read_text() == "unchanged"


@pytest.mark.parametrize("root_kind", ["symlink", "file"])
def test_invalid_bundle_root_is_rejected_unchanged(
    tmp_path: Path, root_kind: str
) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    sentinel = victim / "sentinel"
    sentinel.write_text("unchanged")
    root = tmp_path / "root"
    if root_kind == "symlink":
        root.symlink_to(victim, target_is_directory=True)
    else:
        root.write_text("not a directory")
    with pytest.raises(OSError):
        with bundle_transaction_lock(root, "shared"):
            pass
    assert sentinel.read_text() == "unchanged"
    assert not (victim / BUNDLE_LOCK_FILENAME).exists()


def test_non_regular_lock_leaf_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    leaf = root / BUNDLE_LOCK_FILENAME
    leaf.mkdir()
    with pytest.raises(OSError):
        with bundle_transaction_lock(root, "exclusive"):
            pass
    assert leaf.is_dir()


def test_thread_writer_priority_over_later_reader(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    order: list[str] = []
    release_writer = threading.Event()

    def writer() -> None:
        with bundle_transaction_lock(root, "exclusive"):
            order.append("writer")
            release_writer.wait(1)

    def later_reader() -> None:
        with bundle_transaction_lock(root, "shared"):
            order.append("reader")

    with bundle_transaction_lock(root, "shared"):
        writer_thread, _, writer_errors = _run_in_thread(writer)
        # Deterministically wait until the local condition records the writer.
        from okf_loom import io_utils
        _canonical, root_fd, identity = io_utils._pin_bundle_root(root)
        os.close(root_fd)
        state = io_utils._bundle_state(identity)
        for _ in range(100):
            with state.condition:
                if state.waiting_writers:
                    break
            time.sleep(0.005)
        else:
            pytest.fail("writer did not enter local wait queue")
        reader_thread, _, reader_errors = _run_in_thread(later_reader)
    for _ in range(100):
        if order:
            break
        time.sleep(0.005)
    assert order == ["writer"]
    release_writer.set()
    writer_thread.join(1)
    reader_thread.join(1)
    assert order == ["writer", "reader"]
    assert not writer_errors and not reader_errors


def test_cross_process_writer_priority_over_later_reader(tmp_path: Path) -> None:
    import fcntl

    root = tmp_path / "bundle"
    root.mkdir()
    order = tmp_path / "order"
    worker = (
        "import sys,time; from pathlib import Path; "
        "from okf_loom.io_utils import bundle_transaction_lock; "
        f"r=Path({str(root)!r}); out=Path({str(order)!r}); "
        "mode=sys.argv[1]; lock=bundle_transaction_lock(r,mode); "
        "lock.__enter__(); "
        "f=open(out,'a'); f.write('W' if mode=='exclusive' else 'R'); f.close(); "
        "time.sleep(.1 if mode=='exclusive' else 0); lock.__exit__(None,None,None)"
    )
    with bundle_transaction_lock(root, "shared"):
        writer = subprocess.Popen(
            [sys.executable, "-c", worker, "exclusive"], env=_subprocess_env()
        )
        # Writer owns byte-zero turnstile while blocked on our shared flock.
        fd = os.open(root / BUNDLE_LOCK_FILENAME, os.O_RDWR)
        for _ in range(200):
            try:
                fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB, 1, 0, os.SEEK_SET)
                fcntl.lockf(fd, fcntl.LOCK_UN, 1, 0, os.SEEK_SET)
            except BlockingIOError:
                break
            time.sleep(0.005)
        else:
            os.close(fd)
            pytest.fail("writer did not acquire process turnstile")
        os.close(fd)
        reader = subprocess.Popen(
            [sys.executable, "-c", worker, "shared"], env=_subprocess_env()
        )
    assert writer.wait(timeout=3) == 0
    assert reader.wait(timeout=3) == 0
    assert order.read_text() == "WR"


def test_timeout_while_blocked_on_kernel_flock_has_no_late_acquire(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    # Deliberately emulate a flock-only interoperating process so the shared
    # waiter passes byte-zero and times out specifically at kernel flock.
    holder = (
        "import fcntl,os,time; from pathlib import Path; "
        f"r=Path({str(root)!r}); ready=Path({str(ready)!r}); rel=Path({str(release)!r}); "
        f"fd=os.open(r/{BUNDLE_LOCK_FILENAME!r},os.O_RDWR|os.O_CREAT,0o600); "
        "fcntl.flock(fd,fcntl.LOCK_EX); ready.write_text('ready'); "
        "\nwhile not rel.exists(): time.sleep(.01)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", holder])
    for _ in range(100):
        if ready.exists():
            break
        time.sleep(0.01)
    assert ready.exists()
    with pytest.raises(BundleBusyError):
        with bundle_transaction_lock(root, "shared", timeout_seconds=0.03):
            pytest.fail("timed-out waiter acquired late")
    release.write_text("release")
    assert process.wait(timeout=3) == 0
    with bundle_transaction_lock(root, "exclusive", timeout_seconds=1):
        pass


def test_cancelled_kernel_acquisition_cleans_reservation_and_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okf_loom import io_utils

    root = tmp_path / "bundle"
    root.mkdir()
    original = io_utils._acquire_cross_process

    def cancel(_fd, _mode, _deadline):
        raise KeyboardInterrupt()

    monkeypatch.setattr(io_utils, "_acquire_cross_process", cancel)
    with pytest.raises(KeyboardInterrupt):
        with bundle_transaction_lock(root, "shared", timeout_seconds=1):
            pass
    monkeypatch.setattr(io_utils, "_acquire_cross_process", original)
    with bundle_transaction_lock(root, "exclusive", timeout_seconds=1):
        assert_bundle_transaction_owned(root, "exclusive")


def _assert_writer_waits(root: Path, writer, published: Path) -> None:
    with bundle_transaction_lock(root, "shared"):
        thread, finished, errors = _run_in_thread(writer)
        time.sleep(0.1)
        assert not published.exists()
        assert not finished.is_set()
    # This is a lock-ownership proof, not a three-second storage-latency SLA.
    # A diagnostic run on slow /tmp storage found the released writer inside
    # os.fsync, not waiting for the lock. Keep the real durable write, with a
    # separate bounded completion budget (no retry or mocked-away fsync).
    thread.join(30)
    if thread.is_alive():
        import traceback
        frame = sys._current_frames().get(thread.ident)
        stack = "".join(traceback.format_stack(frame)) if frame else "unavailable"
        pytest.fail(f"writer did not settle after lock release:\n{stack}")
    assert not errors
    assert published.exists()


def test_writer_funnels_take_exclusive_ownership(tmp_path: Path) -> None:
    # Root-only write_concept.
    direct = tmp_path / "direct"
    direct.mkdir()
    _assert_writer_waits(
        direct,
        lambda: write_concept(direct, "new", type="Reference", defaults=False),
        direct / "new.md",
    )

    # Studio save and content-bearing group transaction.
    studio_root = tmp_path / "studio"
    studio_root.mkdir()
    studio = Studio.for_bundle(studio_root)
    _assert_writer_waits(
        studio_root,
        lambda: studio.save_concept(concept_id="saved", raw="---\ntype: Reference\n---\n"),
        studio_root / "saved.md",
    )
    grouped = studio_root / "grouped.md"
    _assert_writer_waits(
        studio_root,
        lambda: studio.run_group_transaction(
            "g", lambda: grouped.write_text("published"), content_paths=[grouped]
        ),
        grouped,
    )

    # Retained BundleMutation/apply seam.
    retained_root = tmp_path / "retained"
    retained_root.mkdir()
    retained = Bundle.load(retained_root)
    retained_marker = retained_root / "mutation-entered"
    _assert_writer_waits(
        retained_root,
        lambda: _enter_bundle_mutation(retained, retained_marker),
        retained_marker,
    )


def _enter_bundle_mutation(bundle: Bundle, marker: Path) -> None:
    with bundle.mutation():
        marker.write_text("entered")


def test_bootstrap_import_and_init_publications_wait(tmp_path: Path) -> None:
    bootstrap_root = tmp_path / "bootstrap"
    bootstrap_root.mkdir()
    _assert_writer_waits(
        bootstrap_root, lambda: bootstrap_bundle(bootstrap_root),
        bootstrap_root / "index.md",
    )

    source = tmp_path / "source"
    source.mkdir()
    (source / "imported.md").write_text("# Imported\n")
    import_root = tmp_path / "import"
    bootstrap_bundle(import_root)
    _assert_writer_waits(
        import_root, lambda: import_directory(source, import_root),
        import_root / "imported.md",
    )

    init_root = tmp_path / "init"
    init_root.mkdir()
    _assert_writer_waits(
        init_root,
        lambda: cmd_init(Namespace(bundle=str(init_root), name=None, format="json")),
        init_root / "okf-loom.config.yaml",
    )


def test_direct_index_and_log_writers_wait_for_shared_owner(tmp_path: Path) -> None:
    index_root = tmp_path / "index"
    index_root.mkdir()
    (index_root / "concept.md").write_text("---\ntype: Reference\n---\n# Concept\n")
    index_bundle = Bundle.load(index_root)
    _assert_writer_waits(
        index_root, lambda: regenerate_indexes(index_bundle), index_root / "index.md"
    )

    log_root = tmp_path / "log"
    log_root.mkdir()
    log_bundle = Bundle.load(log_root)
    _assert_writer_waits(
        log_root,
        lambda: append_log_entry(log_bundle, kind="Update", entry="locked"),
        log_root / "log.md",
    )


def test_sidecar_event_and_comment_flows_do_not_request_exclusive_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "concept.md").write_text("---\ntype: Reference\n---\n# Concept\n")
    studio = Studio.for_bundle(root)
    with bundle_transaction_lock(root, "shared"):
        event = studio.append_event({"kind": "sidecar-proof"})
        comment = studio.post_comment(concept="concept", body="sidecar only")
    assert event["kind"] == "sidecar-proof"
    assert comment["concept"] == "concept"


@pytest.mark.parametrize("second_is_init", [False, True])
def test_concurrent_bootstrap_publication_has_one_cas_winner(
    tmp_path: Path, second_is_init: bool
) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []

    def run(label: str, init: bool) -> None:
        barrier.wait()
        try:
            if init:
                cmd_init(Namespace(bundle=str(root), name=label, format="json"))
            else:
                bootstrap_bundle(root, name=label)
            outcomes.append((label, "won"))
        except FileExistsError:
            outcomes.append((label, "lost"))

    first = threading.Thread(target=run, args=("alpha", False))
    second = threading.Thread(target=run, args=("beta", second_is_init))
    first.start()
    second.start()
    first.join(3)
    second.join(3)
    assert sorted(status for _label, status in outcomes) == ["lost", "won"]
    winner = next(label for label, status in outcomes if status == "won")
    index = (root / "index.md").read_text()
    assert f"# {winner}\n" in index
    assert f"# {'beta' if winner == 'alpha' else 'alpha'}\n" not in index
    assert (root / "okf-loom.config.yaml").exists() == (
        second_is_init and winner == "beta"
    )


def test_concurrent_import_target_cas_has_one_writer(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "same.md").write_text("# Complete source\n")
    root = tmp_path / "bundle"
    bootstrap_bundle(root)
    barrier = threading.Barrier(2)
    results: list[dict] = []

    def run() -> None:
        barrier.wait()
        results.append(import_directory(source, root, overwrite=False))

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    second.start()
    first.join(3)
    second.join(3)
    assert sorted(result["imported"] for result in results) == [0, 1]
    assert (root / "same.md").read_text().endswith("# Complete source\n")


def test_source_config_writer_inventory_remains_integrated() -> None:
    """Maintained inventory: new publication funnels must be added here."""
    from okf_loom import bootstrap, model, studio, update

    inventory = {
        "BundleMutation/apply/index/log": (model.BundleMutation.__enter__,
                                             "bundle_transaction_lock"),
        "Studio.save_concept": (studio.Studio.save_concept,
                                  "bundle_transaction_lock"),
        "Studio.content_group": (studio.Studio.run_group_transaction,
                                   "bundle_transaction_lock"),
        "root write_concept": (update.write_concept, "bundle_transaction_lock"),
        "bootstrap/init": (bootstrap._bootstrap_bundle,
                            "bundle_transaction_lock"),
        "import batch": (bootstrap.import_directory,
                          "bundle_transaction_lock"),
    }
    assert set(inventory) == {
        "BundleMutation/apply/index/log", "Studio.save_concept",
        "Studio.content_group", "root write_concept", "bootstrap/init",
        "import batch",
    }
    for _name, (writer, ownership_token) in inventory.items():
        assert ownership_token in inspect.getsource(writer)


# ASYNC_LIFECYCLE_MATRIX (executable rows above):
# - capture/shared 15s timeout -> retryable bundle_busy, no state, retry allowed;
# - direct writer exclusive wait -> normal blocking, unchanged write semantics;
# - fork/process death -> inherited child fds close and kernel ownership releases;
# - cancellation/timeout -> local queue reservation and opened fd are cleaned;
# - a timed-out kernel waiter cannot acquire late after its context has failed.
