"""Real-process handshake proofs for owned headed-browser group cleanup.

These tests never launch a real browser: they exercise the exact cleanup
contract of ``browser_support._stop_owned_browser_group`` and the startup
path of ``_launch_owned_headed_chrome`` with small deterministic synthetic
process trees, because natural Chrome descendant timing is not reliably
reproducible. Every scenario the production cleanup must survive is proved
against real kernel process groups:

- a leader that already exited while a TERM-resistant descendant keeps
  writing the profile (the historical ENOTEMPTY teardown race),
- a dead leader with no descendants,
- startup failure running the same shared cleanup,
- an unrelated process in the caller's own group surviving every owned
  group signal,
- quiescence that cannot be proven retaining the profile with a visible
  error, and a visible one-shot profile deletion failure,
- retention that is durable, not reference-bound: the retained profile
  survives both release of its owner's reference and exit of the
  interpreter that ran the helper, and the launcher's profile allocation
  carries no automatic lifetime at all.
"""
from __future__ import annotations

import gc
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest

import browser_support


_WRITER_CODE = """
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
profile = sys.argv[1]
late = os.path.join(profile, "late")
stop = os.path.join(profile, "stop")
deadline = time.time() + 60
while time.time() < deadline and not os.path.exists(stop):
    try:
        with open(late, "a") as handle:
            handle.write("tick %.6f\\n" % time.time())
    except OSError:
        pass
    time.sleep(0.02)
"""

# The leader spawns the TERM-resistant writer, waits until the writer has
# really written into the profile, then exits: exactly the "descendants may
# write the profile after the leader exits" race, made deterministic.
_DEAD_LEADER_SPAWNING_WRITER = """
import os, subprocess, sys, time
profile = sys.argv[1]
child = subprocess.Popen([sys.executable, "-c", sys.argv[2], profile])
deadline = time.time() + 10
while not os.path.exists(os.path.join(profile, "late")) and time.time() < deadline:
    time.sleep(0.01)
"""

# Runs in a separate owned child interpreter: it exercises the real helper
# against a real (already-reaped) owned leader with an unquiescent census,
# then EXITS while the profile is still supposed to be retained. The parent
# proves the retained profile outlived that interpreter.
_RETENTION_ACROSS_INTERPRETER_EXIT = textwrap.dedent("""
    import subprocess, sys, tempfile
    sys.path.insert(0, sys.argv[1])  # the tests directory: browser_support + conftest
    import browser_support

    profile = tempfile.mkdtemp(prefix="okf-exit-", dir=sys.argv[2])
    log = open(profile + "/chrome.log", "w+")
    # A real owned leader that exits on its own; the shared helper reaps it.
    # Only the census boundary reports an unquiescent group, exactly like
    # the in-process reporting test.
    leader = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"],
                              start_new_session=True)
    leader.wait(timeout=15)
    browser_support._live_group_writers = lambda pgid: [4_000_000]
    outcome = browser_support._stop_owned_browser_group(
        leader, profile, log, term_timeout=0.2, kill_timeout=0.2)
    assert outcome["ok"] is False and outcome["removed"] is False, outcome
    assert any("profile retained" in error for error in outcome["errors"]), outcome
    print(profile)
    """)


def _open_owned_process(tmp_path: Path, code: str, *args: str):
    """Start a synthetic owned leader with its own process group and log.

    The profile is the same explicitly owned directory path the production
    launcher hands to the helper (``tempfile.mkdtemp``): no wrapper, no
    automatic finalizer.
    """
    profile = tempfile.mkdtemp(prefix="okf-handshake-", dir=str(tmp_path))
    log = open(Path(profile) / "chrome.log", "w+")
    process = subprocess.Popen(
        [sys.executable, "-c", code, profile, *args],
        stdout=log, stderr=log, start_new_session=True)
    return process, profile, log


def _stop_stray_group(pgid: int | None) -> None:
    if pgid is None:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_cleanup_escalates_past_term_resistant_descendant_after_dead_leader(
    tmp_path: Path,
) -> None:
    process, profile, log = _open_owned_process(
        tmp_path, _DEAD_LEADER_SPAWNING_WRITER, _WRITER_CODE)
    try:
        # The leader exits on its own after the writer's first profile write;
        # the writer stays alive and TERM-resistant in the owned group.
        process.wait(timeout=15)
        outcome = browser_support._stop_owned_browser_group(
            process, profile, log, term_timeout=1.0, kill_timeout=3.0)
        assert outcome["exitcode"] == 0
        # The descendant survived the group TERM, so cleanup escalated to a
        # group KILL, proved quiescence, and only then deleted the profile.
        assert outcome["escalated"] is True, outcome
        assert outcome["ok"] is True and outcome["removed"] is True, outcome
        assert outcome["errors"] == [], outcome
        assert not Path(profile).exists()
        assert browser_support._live_group_writers(process.pid) == []
    finally:
        _stop_stray_group(process.pid)


def test_cleanup_reaps_dead_leader_deletes_profile_and_spares_caller_group(
    tmp_path: Path,
) -> None:
    # An unrelated process in the CALLER's own process group: the owned
    # group signals must never reach it (or the test runner).
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"])
    process, profile, log = _open_owned_process(tmp_path, "import sys; sys.exit(0)")
    try:
        process.wait(timeout=15)
        assert process.poll() == 0
        outcome = browser_support._stop_owned_browser_group(
            process, profile, log, term_timeout=1.0, kill_timeout=3.0)
        # The already-dead leader is reaped without escalation and the
        # profile is still deleted after the empty group proves quiescent.
        assert outcome == {"ok": True, "errors": [], "escalated": False,
                           "removed": True, "exitcode": 0}, outcome
        assert not Path(profile).exists()
        assert sentinel.poll() is None, "an owned group signal hit the caller's group"
    finally:
        _stop_stray_group(process.pid)
        sentinel.terminate()
        sentinel.wait(timeout=10)


def test_startup_failure_uses_shared_cleanup_and_preserves_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPLAY", ":97")  # presence is the launch precondition
    before = set(Path("/tmp").glob("okf-headed-*"))
    # /bin/true exits immediately: the readiness loop raises the original
    # startup RuntimeError, and the shared owned-group cleanup must run —
    # no profile directories may survive it.
    owner = object()
    with pytest.raises(RuntimeError, match="Headed Chrome did not start"):
        browser_support._launch_owned_headed_chrome(owner, "/bin/true")
    leftover = set(Path("/tmp").glob("okf-headed-*")) - before
    assert leftover == set(), leftover


def test_cleanup_retains_profile_with_visible_error_when_quiescence_unproven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, profile, log = _open_owned_process(tmp_path, "import sys; sys.exit(0)")
    process.wait(timeout=15)
    try:
        # No real process resists SIGKILL, so the unprovable-quiescence
        # reporting contract is proven at the scan boundary: the group
        # census reports a live writer and cleanup must retain the profile
        # and surface the failure instead of deleting or retrying.
        monkeypatch.setattr(
            browser_support, "_live_group_writers", lambda pgid: [4_000_000])
        outcome = browser_support._stop_owned_browser_group(
            process, profile, log, term_timeout=0.2, kill_timeout=0.2)
        assert outcome["ok"] is False and outcome["removed"] is False, outcome
        assert outcome["escalated"] is True, outcome
        assert any("live writers" in error and "profile retained" in error
                   for error in outcome["errors"]), outcome
        named = any(str(profile) in error for error in outcome["errors"])
        assert named, f"diagnostic must name the retained path: {outcome['errors']}"
        assert Path(profile).exists(), "profile was deleted without quiescence"
    finally:
        _stop_stray_group(process.pid)


def test_cleanup_retention_survives_profile_owner_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, profile, log = _open_owned_process(tmp_path, "import sys; sys.exit(0)")
    process.wait(timeout=15)
    try:
        # Retention is a filesystem outcome, not a reference-bound promise:
        # after the helper reports an unproven census, releasing every
        # reference to the owned profile name (and any finalizer a wrapper
        # object could hold) must NOT delete it.
        monkeypatch.setattr(
            browser_support, "_live_group_writers", lambda pgid: [4_000_000])
        outcome = browser_support._stop_owned_browser_group(
            process, profile, log, term_timeout=0.2, kill_timeout=0.2)
        assert outcome["ok"] is False and outcome["removed"] is False, outcome
        retained = Path(profile)
        del profile
        gc.collect()
        assert retained.is_dir(), (
            "retained profile was removed by automatic finalization after "
            "owner release")
    finally:
        _stop_stray_group(process.pid)


def test_owned_profile_allocation_has_no_automatic_lifetime() -> None:
    """The launcher's profile allocation is explicit for its whole lifetime.

    The reviewed defect allocated the profile as a ``TemporaryDirectory``:
    its automatic finalizer deleted a profile that the helper had just
    reported as retained for diagnosis, as soon as the reference was
    released or the interpreter exited. The allocation must be a plain
    owned path with no hidden removal — proven here through the public
    allocation helper and filesystem outcomes, not finalizer internals.
    """
    before = set(Path("/tmp").glob("okf-headed-*"))
    path = Path(browser_support._owned_profile_dir())
    gc.collect()
    try:
        assert path.is_dir(), "profile allocation disappeared without an owner"
    finally:
        # This test is the proven-quiescent owner now: reclaim the state.
        shutil.rmtree(path)
    assert set(Path("/tmp").glob("okf-headed-*")) - before == set()


def test_cleanup_retention_survives_helper_interpreter_exit(tmp_path: Path) -> None:
    tests_dir = str(Path(__file__).resolve().parent)
    # Owned child process: plain child of this test runner, reaped via
    # subprocess.run's timeout kill+wait. No group signals are involved.
    result = subprocess.run(
        [sys.executable, "-c", _RETENTION_ACROSS_INTERPRETER_EXIT,
         tests_dir, str(tmp_path)],
        capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    profile = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    assert profile and Path(profile).is_dir(), (
        "the retained profile was deleted by the helper interpreter's "
        "automatic exit cleanup")
    # The exited child proved the census uncertainty; this parent now owns
    # the diagnostic state with proven quiescence, so reclaim it instead of
    # leaking it past the test run.
    shutil.rmtree(profile)


def test_cleanup_reports_profile_deletion_failure_once_and_retains_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, profile, log = _open_owned_process(tmp_path, "import sys; sys.exit(0)")
    process.wait(timeout=15)
    try:
        # The profile's whole lifetime is the helper's single shutil.rmtree
        # call, so the deletion-failure boundary this contract owns is that
        # one call: make it fail once and require one visible failure, no
        # retry, no suppression, and no deletion. (The patch is reverted by
        # monkeypatch before pytest's own tmp_path teardown runs.)
        def failing_rmtree(path, *args, **kwargs):
            raise OSError("injected deletion failure (simulated ENOTEMPTY/EBUSY)")

        monkeypatch.setattr(browser_support.shutil, "rmtree", failing_rmtree)
        outcome = browser_support._stop_owned_browser_group(
            process, profile, log, term_timeout=1.0, kill_timeout=3.0)
        assert outcome["ok"] is False and outcome["removed"] is False, outcome
        assert any("profile cleanup failed" in error
                   for error in outcome["errors"]), outcome
        assert len(outcome["errors"]) == 1, "cleanup failures must not be retried"
        assert Path(profile).exists(), "a failed deletion must retain the profile"
    finally:
        _stop_stray_group(process.pid)
