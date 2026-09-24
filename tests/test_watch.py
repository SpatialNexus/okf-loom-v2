"""Tests for the headless ``okf watch`` change feed (§10).

Exercises the pure emit/replay helpers directly and runs ``run_watch`` briefly
in a daemon thread to confirm a real disk edit produces a ``changed`` event on
stdout + in the durable ``events.jsonl``.
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.watch import _concept_revs, _print_event, run_watch

from conftest import okf_module_argv, okf_subprocess_env


def test_print_event_jsonl_emits_compact_json_line() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_event({"type": "changed", "ids": ["tables/orders"], "origin": "disk"},
                     emit="jsonl")
    line = buf.getvalue().strip()
    obj = json.loads(line)
    assert obj["type"] == "changed" and obj["ids"] == ["tables/orders"]


def test_print_event_text_emits_human_readable() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_event({"type": "changed", "ids": ["tables/orders"]}, emit="text")
        _print_event({"type": "graph", "ids": []}, emit="text")
    lines = buf.getvalue().strip().splitlines()
    assert "changed tables/orders" in lines
    assert any("graph" in ln for ln in lines)


def test_concept_revs_maps_ids_to_hashes(tiny_good_bundle: Path) -> None:
    bundle = Bundle.load(tiny_good_bundle)
    revs = _concept_revs(bundle)
    assert revs
    for cid, rev in revs.items():
        assert isinstance(cid, str) and len(rev) == 12


def _wait_for(predicate, *, timeout: float = 6.0, interval: float = 0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_run_watch_emits_changed_on_disk_edit(
    tiny_good_bundle: Path, monkeypatch,
) -> None:
    """A real disk edit during a live watch produces a changed event in the
    durable events.jsonl feed (run_watch appends via the shared Studio)."""
    bundle = Bundle.load(tiny_good_bundle)
    target = next(iter(bundle.concepts.values()))
    original = target.path.read_text()
    events_jsonl = tiny_good_bundle / ".okf-loom" / "session" / "events.jsonl"
    if events_jsonl.is_file():
        events_jsonl.unlink()

    done = threading.Event()
    err: list[str] = []
    watchers = _track_bundle_watchers(monkeypatch)

    def _run() -> None:
        try:
            run_watch(tiny_good_bundle, emit="text", debounce_ms=50)
        except Exception as e:  # capture any crash for diagnostics
            err.append(repr(e))
        finally:
            done.set()

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    try:
        # Let the watcher take its initial snapshot, then edit a concept file.
        time.sleep(1.2)
        with target.path.open("a") as fh:
            fh.write("\n\n<!-- watch-test edit -->\n")
        # The watcher polls every ~1s; wait for a changed event in the feed.
        def _saw_changed() -> bool:
            if not events_jsonl.is_file():
                return False
            return '"changed"' in events_jsonl.read_text(encoding="utf-8")
        assert _wait_for(_saw_changed, timeout=8.0), (
            f"no changed event in feed; thread err={err}; "
            f"feed={events_jsonl.read_text() if events_jsonl.is_file() else 'MISSING'!r}"
        )
    finally:
        watchers[0].stop()
        th.join(3)
        assert done.is_set()
        target.path.write_text(original)


def test_since_replay_prints_missed_events(tiny_good_bundle: Path, monkeypatch) -> None:
    """``--since <id>`` replays events after that id before tailing live."""
    from okf_loom.studio import Studio
    studio = Studio.for_bundle(tiny_good_bundle)
    studio.ensure_session()
    e1 = studio.append_event({"type": "changed", "ids": ["a"], "origin": "disk"})
    e2 = studio.append_event({"type": "changed", "ids": ["b"], "origin": "disk"})

    stdout_buf = io.StringIO()
    watchers = _track_bundle_watchers(monkeypatch)

    def _run_replay() -> None:
        with redirect_stdout(stdout_buf):
            run_watch(tiny_good_bundle, emit="jsonl", since=e1["id"])

    th = threading.Thread(target=_run_replay, daemon=True)
    th.start()
    try:
        # The replay happens synchronously before the blocking live tail, so
        # e2's line should appear quickly.
        assert _wait_for(
            lambda: '"b"' in stdout_buf.getvalue() or "tables" in stdout_buf.getvalue(),
            timeout=4.0,
        )
        # e1 (the `since` anchor) must NOT be replayed; only rows after it.
        lines = [ln for ln in stdout_buf.getvalue().splitlines() if ln.strip().startswith("{")]
        for ln in lines:
            obj = json.loads(ln)
            assert obj.get("ids") != ["a"], "the since-anchor event was replayed"
    finally:
        watchers[0].stop()
        th.join(3)
        studio.close()


# ---------------------------------------------------------------------------
# P1-18 (ARCH-012) — okf wait --for change CLI exit codes
# ---------------------------------------------------------------------------
# `okf wait <bundle> --for change --timeout 2` exits 1 with no change present;
# exits 0 with a change event after a `.md` edit. Closes the current spec §12 surface
# that was previously library-only.


@pytest.mark.parametrize("kind", ["comment", "change"])
def test_wait_fresh_bundle_exits_1_on_timeout(
    tiny_good_bundle: Path, kind: str,
) -> None:
    """The real CLI treats a missing comment/change feed as a timeout."""
    import subprocess
    # Make sure the command starts with no session authority or feed.
    session_dir = tiny_good_bundle / ".okf-loom" / "session"
    if session_dir.exists():
        import shutil
        shutil.rmtree(session_dir)

    proc = subprocess.run(
        okf_module_argv(
            "wait", str(tiny_good_bundle), "--for", kind, "--timeout", "0.1",
            "--interval", "0.5",
        ),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(tiny_good_bundle.parent),
        env=okf_subprocess_env(),
        timeout=15,
    )
    assert proc.returncode == 1, (
        f"okf wait --for {kind} (no work present) should exit 1; "
        f"got rc={proc.returncode}"
    )


def test_wait_for_change_exits_0_on_change_event(tiny_good_bundle: Path) -> None:
    """``okf wait --for change --timeout 5`` exits 0 after a .md edit lands
    a change event in events.jsonl. A background thread edits a file under
    the bundle's session so the wait picks it up."""
    import subprocess
    import threading

    from okf_loom.studio import Studio

    events_jsonl = tiny_good_bundle / ".okf-loom" / "session" / "events.jsonl"
    if events_jsonl.is_file():
        events_jsonl.unlink()

    studio = Studio.for_bundle(tiny_good_bundle)
    studio.ensure_session()
    anchor = studio.append_event(
        {"type": "changed", "ids": ["test/wait-anchor"], "origin": "test"}
    )

    def _emit_change_after_delay() -> None:
        # Wait for the wait process to be running, then append a change.
        time.sleep(1.0)
        studio.emit_change(kind="changed", ids=["tables/users"], origin="disk")

    threading.Thread(target=_emit_change_after_delay, daemon=True).start()
    proc = subprocess.run(
        okf_module_argv(
            "wait", str(tiny_good_bundle), "--for", "change", "--timeout", "8",
            "--interval", "0.3", "--since", anchor["id"],
        ),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(tiny_good_bundle.parent),
        env=okf_subprocess_env(),
        timeout=20,
    )
    assert proc.returncode == 0, (
        f"okf wait --for change should exit 0 after a change event; "
        f"got rc={proc.returncode}"
    )


# ---------------------------------------------------------------------------
# P2-20 (§3) — bring-up watch-question regression
# ---------------------------------------------------------------------------
# §3 documents the bring-up flow: the agent asks the user "watch it and keep
# it enriched?" — yes → ``okf watch --auto-repair`` runs and proactively
# enriches; no → ``okf watch`` (plain) does NOT auto-enrich. Pin both halves
# so a regression in the auto-repair wiring surfaces immediately.


def _watch_question_bundle(tmp_path: Path) -> Path:
    """A bundle with a known mechanical gap (mirror_relation candidate) so
    auto-repair has observable work to do.

    Layout: ``orders.md`` has a typed ``relations:`` entry to ``customers``
    WITHOUT a markdown body link, so :func:`build_plan` emits a
    ``mirror_relation`` action for it. Auto-repair should add the body link.
    """
    root = tmp_path / "watch_question"
    root.mkdir()
    (root / "index.md").write_text(
        "---\nokf_version: '0.1'\n---\n# Bundle\n", encoding="utf-8",
    )
    (root / "orders.md").write_text(
        "---\ntype: Table\ntitle: Orders\n"
        "relations:\n  - target: customers\n    type: references\n"
        "---\n# Orders\n\nOrders body has no body link to customers.\n",
        encoding="utf-8",
    )
    (root / "customers.md").write_text(
        "---\ntype: Table\ntitle: Customers\n---\nCustomers body.\n",
        encoding="utf-8",
    )
    return root


def _wait_for(predicate, *, timeout: float = 8.0, interval: float = 0.2) -> bool:
    """Reuse the module-level helper signature (defined above for the
    existing tests). Polled wait so the test does not flake on slow CI."""
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if predicate():
            return True
        _time.sleep(interval)
    return False


def test_auto_repair_applies_mechanical_actions_on_change(
    tmp_path: Path, monkeypatch,
) -> None:
    """§3 'yes' branch: ``okf watch --auto-repair`` proactively applies the
    mechanical mirror_relation on a disk change. After the watcher observes
    an edit, ``orders.md`` gains a body link to ``customers``."""
    bundle = _watch_question_bundle(tmp_path)
    orders_path = bundle / "orders.md"
    original_orders = orders_path.read_text(encoding="utf-8")

    # Sanity: the bundle has a mirror_relation candidate at plan time.
    from okf_loom.plan import build_plan
    from okf_loom.model import Bundle as _Bundle
    plan = build_plan(_Bundle.load(bundle))
    assert any(a.action == "mirror_relation" for a in plan.actions), (
        "test prelude: bundle should have a mirror_relation candidate"
    )

    # Start run_watch with auto_repair=True in a daemon thread.
    done = threading.Event()
    err: list[str] = []
    watchers = _track_bundle_watchers(monkeypatch)

    def _run():
        try:
            run_watch(bundle, emit="text", auto_repair=True, debounce_ms=50)
        except Exception as e:
            err.append(repr(e))
        finally:
            done.set()

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    try:
        # Let the watcher take its initial snapshot, then trigger a disk edit
        # so the watcher reloads and arms the debounced repair.
        time.sleep(1.2)
        with orders_path.open("a") as fh:
            fh.write("\n\n<!-- watch-question-yes-edit -->\n")
        # The repair is debounced; wait for the body link to land.
        def _repaired() -> bool:
            current = orders_path.read_text(encoding="utf-8")
            # The mirror adds a markdown link to customers (absolute form).
            return "customers.md" in current or "/customers" in current
        assert _wait_for(_repaired, timeout=10.0), (
            f"auto-repair did not add a body link to customers within 10s; "
            f"orders.md = {orders_path.read_text()!r}; err={err}"
        )
    finally:
        watchers[0].stop()
        th.join(3)
        assert done.is_set()
        orders_path.write_text(original_orders)


def test_no_auto_repair_does_not_mutate_files_on_change(
    tmp_path: Path, monkeypatch,
) -> None:
    """§3 'no' branch: ``okf watch`` (plain, no --auto-repair) emits change
    events on a disk edit but does NOT proactively enrich. The
    mirror_relation gap stays open."""
    bundle = _watch_question_bundle(tmp_path)
    orders_path = bundle / "orders.md"
    events_jsonl = bundle / ".okf-loom" / "session" / "events.jsonl"
    if events_jsonl.is_file():
        events_jsonl.unlink()
    original_orders = orders_path.read_text(encoding="utf-8")

    done = threading.Event()
    err: list[str] = []
    watchers = _track_bundle_watchers(monkeypatch)

    def _run():
        try:
            run_watch(bundle, emit="text", auto_repair=False, debounce_ms=50)
        except Exception as e:
            err.append(repr(e))
        finally:
            done.set()

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    try:
        time.sleep(1.2)
        with orders_path.open("a") as fh:
            fh.write("\n\n<!-- watch-question-no-edit -->\n")
        # Wait for the watcher to notice the change (changed event lands).
        def _saw_changed() -> bool:
            if not events_jsonl.is_file():
                return False
            return '"changed"' in events_jsonl.read_text(encoding="utf-8")
        assert _wait_for(_saw_changed, timeout=8.0), (
            f"plain watch did not emit a changed event; err={err}"
        )
        # Wait a bit longer so any debounced repair (there is NONE here)
        # would have had time to run.
        time.sleep(0.8)
        # The orders.md content must still NOT contain a body link to
        # customers (the mirror gap stays open without --auto-repair).
        # Our own edit is the only delta; the mirror would have added
        # `customers.md` somewhere in the body.
        body_now = orders_path.read_text(encoding="utf-8")
        # Strip our own edit marker so the check is precise.
        body_without_edit = body_now.replace(
            "\n\n<!-- watch-question-no-edit -->\n", ""
        )
        assert "customers.md" not in body_without_edit and "/customers" not in body_without_edit, (
            "plain watch (no --auto-repair) unexpectedly enriched the bundle"
        )
    finally:
        watchers[0].stop()
        th.join(3)
        assert done.is_set()
        orders_path.write_text(original_orders)


def _track_bundle_watchers(monkeypatch):
    """Keep real polling semantics while giving tests a clean stop handle."""
    from okf_loom import server

    instances = []
    real_watcher = server._BundleWatcher

    class _TrackedBundleWatcher(real_watcher):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

    monkeypatch.setattr(server, "_BundleWatcher", _TrackedBundleWatcher)
    return instances


class _OneChangeWatcher:
    """Deterministic watcher double: deliver one change, then terminate."""

    instances: list["_OneChangeWatcher"] = []
    mutate = staticmethod(lambda: None)
    after_reload = staticmethod(lambda: None)

    def __init__(self, bundle_root, reload_fn, *, interval):
        self.reload_fn = reload_fn
        self.alive = True
        self.stop_calls = 0
        self.__class__.instances.append(self)

    def start(self):
        self.__class__.mutate()
        self.reload_fn()
        self.__class__.after_reload()
        self.alive = False

    def is_alive(self):
        return self.alive

    def stop(self):
        self.stop_calls += 1
        self.alive = False

    def join(self, timeout=None):
        return None


def _install_one_change_watcher(monkeypatch, target: Path) -> None:
    from okf_loom import server

    _OneChangeWatcher.instances = []
    _OneChangeWatcher.after_reload = staticmethod(lambda: None)
    _OneChangeWatcher.mutate = lambda: target.write_text(
        target.read_text(encoding="utf-8") + "\n<!-- lifecycle change -->\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "_BundleWatcher", _OneChangeWatcher)


def test_shutdown_joins_inflight_auto_repair_before_studio_close(
    tiny_good_bundle: Path, monkeypatch, capsys,
) -> None:
    """A repair that has started owns Studio until publication completes."""
    import okf_loom.watch as watch_module

    target = next(iter(Bundle.load(tiny_good_bundle).concepts.values())).path
    _install_one_change_watcher(monkeypatch, target)
    repair_started = threading.Event()
    release_repair = threading.Event()
    repair_finished = threading.Event()

    def _blocked_repair(_root):
        repair_started.set()
        assert release_repair.wait(5)
        repair_finished.set()
        return ["tables/orders"]

    monkeypatch.setattr(watch_module, "_run_auto_repair", _blocked_repair)
    _OneChangeWatcher.after_reload = staticmethod(
        lambda: repair_started.wait(2) or pytest.fail("repair did not start")
    )
    fd_baseline = len(os.listdir("/proc/self/fd"))
    thread_baseline = set(threading.enumerate())
    errors: list[BaseException] = []
    watch_thread = threading.Thread(
        target=lambda: _run_capturing_errors(
            errors,
            tiny_good_bundle,
            auto_repair=True,
            debounce_ms=0,
        ),
        name="watch-lifecycle-test",
    )
    watch_thread.start()
    assert repair_started.wait(2)
    watch_thread.join(0.1)
    assert watch_thread.is_alive(), "run_watch returned while repair still owned Studio"
    release_repair.set()
    watch_thread.join(5)

    assert not watch_thread.is_alive()
    assert repair_finished.is_set()
    assert errors == []
    assert "auto_repair" in (
        tiny_good_bundle / ".okf-loom" / "session" / "events.jsonl"
    ).read_text(encoding="utf-8")
    assert set(threading.enumerate()) <= thread_baseline
    assert len(os.listdir("/proc/self/fd")) == fd_baseline
    assert "bundle session authority is closed" not in capsys.readouterr().err
    watcher = _OneChangeWatcher.instances[0]
    watcher.stop()
    watcher.stop()
    assert watcher.stop_calls == 3


def _run_capturing_errors(errors, bundle, **kwargs) -> None:
    try:
        run_watch(bundle, emit="text", **kwargs)
    except BaseException as exc:
        errors.append(exc)


def test_shutdown_cancels_pending_auto_repair(
    tiny_good_bundle: Path, monkeypatch,
) -> None:
    import okf_loom.watch as watch_module

    target = next(iter(Bundle.load(tiny_good_bundle).concepts.values())).path
    _install_one_change_watcher(monkeypatch, target)
    repair_called = threading.Event()
    monkeypatch.setattr(
        watch_module,
        "_run_auto_repair",
        lambda _root: repair_called.set() or [],
    )
    thread_baseline = set(threading.enumerate())

    assert run_watch(
        tiny_good_bundle, emit="text", auto_repair=True, debounce_ms=60_000,
    ) == 0
    assert not repair_called.is_set()
    assert set(threading.enumerate()) <= thread_baseline


def test_auto_repair_exception_is_reported_and_joined(
    tiny_good_bundle: Path, monkeypatch, capsys,
) -> None:
    import okf_loom.watch as watch_module

    target = next(iter(Bundle.load(tiny_good_bundle).concepts.values())).path
    _install_one_change_watcher(monkeypatch, target)
    repair_started = threading.Event()

    def _failed_repair(_root):
        repair_started.set()
        raise ValueError("repair exploded")

    monkeypatch.setattr(watch_module, "_run_auto_repair", _failed_repair)
    _OneChangeWatcher.after_reload = staticmethod(
        lambda: repair_started.wait(2) or pytest.fail("repair did not start")
    )

    assert run_watch(
        tiny_good_bundle, emit="text", auto_repair=True, debounce_ms=0,
    ) == 0
    assert "okf: auto-repair failed: repair exploded" in capsys.readouterr().err
