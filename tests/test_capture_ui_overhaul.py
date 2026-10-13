from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
import threading
import json
import tempfile
import urllib.request
import hashlib
import shutil
import socket
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("capture_ui_overhaul", ROOT / "scripts/capture_ui_overhaul.py")
assert SPEC and SPEC.loader
capture = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(capture)
_REAL_PUBLICATION_VERIFIER = capture._verify_publication_provenance

_TEST_PUBLICATION_PROVENANCE = capture.PublicationProvenance(
    "a" * 40, "b" * 40, True, "clean",
)


@pytest.fixture(autouse=True)
def _stable_publication_provenance(monkeypatch):
    """Filesystem publication tests isolate replacement mechanics from real Git."""
    monkeypatch.setattr(capture, "_verify_publication_provenance", lambda _expected: None)


def _publish(stage: Path, destination: Path) -> None:
    capture._publish_generation(
        stage, destination, expected_provenance=_TEST_PUBLICATION_PROVENANCE,
    )


def _production_shaped_row(
    out: Path, spec, revisions: dict[str, str], inventory: list[dict], digest: str,
) -> dict:
    path = out / spec.artifact
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (1).to_bytes(4, "big") + (1).to_bytes(4, "big"))
    family, mode = capture._public_theme(spec)
    theme_state = {
        "resolved_theme": spec.resolved_theme, "runtime_resolved_theme": spec.resolved_theme,
        "resolved_mode": capture._palette_scheme(spec.resolved_theme),
        "family": family, "mode": mode, "ready": True, "resolution": "resolved",
        "contrast": spec.contrast, "border": spec.border,
        "storageAccess": "denied" if spec.storage == "denied" else "available",
        "storageKeys": [],
    }
    metadata = {"revision": revisions[spec.bundle], "pageRevision": revisions[spec.bundle]}
    readiness = "capture_target" if spec.javascript else "no-js-dom"
    if spec.action in {"boot-blocked", "boot-failure"}:
        readiness = "fallback-dom"
        metadata.update({"captureKind": "intentional-fallback",
                         "failureCode": capture._FALLBACK_SCENARIO_CODES[spec.action],
                         "honestFallback": True})
    zoom = None
    if spec.zoom_percent:
        zoom = {"mechanism": "chrome-page-zoom-keyboard", "zoomPercent": spec.zoom_percent,
                "cssViewportWidth": 1440 * 100 / spec.zoom_percent,
                "expectedCssViewportWidth": 1440 * 100 / spec.zoom_percent,
                "devicePixelRatio": spec.zoom_percent / 100, "devicePixelRatioBefore": 1,
                "baselinePercent": 100, "baselineVerified": True,
                "sessionBinding": spec.id, "sessionBindingVerified": True,
                "journey": {"restored": True, "outline_opened": True, "escape_closed": True,
                            "section_activated": "#section", "no_horizontal_overflow": True}}
    event = {"sequence": 1, "key": "Escape", "pre": {"role": "listbox", "name": "Graph concepts"},
             "post": {"role": "listbox", "name": "Graph concepts"}, "ownership": "component",
             "active_descendant": "node-b", "aria_state": {}, "native_editable": False,
             "default_prevented": True, "restoration": True,
             "observed": {"trusted": True, "targetRole": "listbox"}}
    journey = ({"selectedId": "b", "listTarget": "b", "pathKind": "found", "pathStart": "a",
                "pathEnd": "b", "pathStepCount": 2, "events": [event]}
               if spec.id == "f06-graph-keyboard" else None)
    return capture._artifact_record(
        path, spec, provenance={"head": "head", "tree": "tree", "clean": True,
                               "worktree_digest": "clean", "worktree_status": []},
        expected_revision=revisions[spec.bundle], input_inventory=inventory,
        input_inventory_digest=digest, runtime={"generation": 1, "metadata": metadata},
        theme_state=theme_state,
        browser={"zoom_percent": spec.zoom_percent or 100, "zoom_evidence": zoom,
                  "graph_at_journey": journey,
                  "observer_provenance": {"installed": True, "scenario": spec.id,
                      "events": ("console", "requestfailed", "response>=400"), "observed": 0}},
        readiness=readiness,
    )


def generation(path: Path, value: str) -> Path:
    path.mkdir(); (path / "manifest.json").write_text(value); (path / "proof.png").write_bytes(b"PNG-" + value.encode())
    return path


def assert_complete(path: Path, values=("old", "new")) -> None:
    value = (path / "manifest.json").read_text()
    assert value in values
    assert (path / "proof.png").read_bytes() == b"PNG-" + value.encode()


def _assert_sanitized_tree(path: Path) -> None:
    assert path.is_dir()
    assert all(candidate.is_dir() and not candidate.is_symlink()
               for candidate in path.rglob("*"))


class _PathOraclePage:
    def __init__(self, oracle: dict) -> None:
        self.oracle = oracle
        self.reset_clicked = False
        self.waits = 0

    class _Reset:
        def __init__(self, owner) -> None: self.owner = owner
        def click(self, **_kwargs) -> None: self.owner.reset_clicked = True

    def locator(self, selector: str):
        assert selector == "#okf-reset"
        return self._Reset(self)

    def wait_for_function(self, *_args, **_kwargs) -> None:
        self.waits += 1

    def evaluate(self, script: str):
        if "const edge=G.cy.edges()" in script:
            return {"from": "a", "to": "b"}
        return self.oracle


def test_successful_path_capture_precondition_resets_and_accepts_only_typed_found_path() -> None:
    page = _PathOraclePage({"kind": "found", "message": "Shortest visible path with 1 connection.",
                            "relations": [{"label": "derived_from", "orientation": "forward"}],
                            "panelText": "Connection path derived_from", "visible": True})
    result = capture._prepare_successful_graph_path(page)
    assert page.reset_clicked and page.waits == 2
    assert result["from"] == "a" and result["to"] == "b" and result["kind"] == "found"


@pytest.mark.parametrize("oracle", [
    {"kind": "filtered", "message": "Path unavailable because an endpoint is hidden.", "relations": [], "panelText": "Path unavailable", "visible": True},
    {"kind": "found", "message": "Shortest visible path.", "relations": [], "panelText": "Connection path", "visible": True},
])
def test_successful_path_capture_precondition_rejects_unavailable_or_untyped_state(oracle: dict) -> None:
    with pytest.raises(RuntimeError, match="oracle rejected non-success state"):
        capture._prepare_successful_graph_path(_PathOraclePage(oracle))


@pytest.mark.parametrize("failure", ["journal", "stage-rename", "backup-cleanup", "journal-cleanup"])
def test_publication_filesystem_interruptions_never_return_missing_or_mixed(
    tmp_path: Path, monkeypatch, failure: str,
) -> None:
    destination = generation(tmp_path / "out", "old"); stage = generation(tmp_path / ".out.capture-stage-test", "new")
    real_replace, real_unlink, real_rmtree = capture.os.replace, capture.os.unlink, capture._rmtree_at
    def replace(source, target, *args, **kwargs):
        if failure == "journal" and str(target) == "journal.json": raise OSError(failure)
        if failure == "stage-rename" and str(source) == stage.name: raise OSError(failure)
        return real_replace(source, target, *args, **kwargs)
    def unlink(path, *args, **kwargs):
        if failure == "journal-cleanup" and str(path) == "journal.json": raise OSError(failure)
        return real_unlink(path, *args, **kwargs)
    def rmtree_at(parent_fd, name):
        if failure == "backup-cleanup" and name == "backup": raise OSError(failure)
        return real_rmtree(parent_fd, name)
    monkeypatch.setattr(capture.os, "replace", replace)
    monkeypatch.setattr(capture.os, "unlink", unlink)
    monkeypatch.setattr(capture, "_rmtree_at", rmtree_at)
    with pytest.raises(OSError): _publish(stage, destination)
    assert_complete(destination)
    if failure != "backup-cleanup":
        assert not (tmp_path / ".out.capture-authority" / "backup").exists()


def test_publication_rollback_restores_old_after_stage_rename_failure(tmp_path: Path, monkeypatch) -> None:
    destination = generation(tmp_path / "out", "old"); stage = generation(tmp_path / ".out.capture-stage-test", "new")
    real = capture.os.replace
    def replace(source, target, *args, **kwargs):
        if Path(source).name == stage.name: raise OSError("stage publish failed")
        return real(source, target, *args, **kwargs)
    monkeypatch.setattr(capture.os, "replace", replace)
    with pytest.raises(OSError): _publish(stage, destination)
    assert_complete(destination, ("old",)); assert not (tmp_path / ".out.capture-authority" / "backup").exists()


def test_head_movement_after_immutable_materialization_does_not_block_publication(
    tmp_path: Path, monkeypatch,
) -> None:
    destination = generation(tmp_path / "out", "old")
    stage = generation(tmp_path / ".out.capture-stage-new", "new")
    expected = capture.PublicationProvenance("a" * 40, "b" * 40, True, "clean")
    observed = {
        "head": "c" * 40, "tree": "d" * 40, "observed_head": "c" * 40,
        "clean": True, "worktree_digest": "clean", "worktree_status": [],
    }
    monkeypatch.setattr(capture, "_verify_publication_provenance", _REAL_PUBLICATION_VERIFIER)
    monkeypatch.setattr(capture, "_git_provenance", lambda **_kwargs: observed)

    capture._publish_generation(stage, destination, expected_provenance=expected)

    assert_complete(destination, ("new",))
    assert not stage.exists()
    assert not (tmp_path / ".out.capture-authority" / "backup").exists()


def test_partial_png_and_manifest_prepublication_leave_old_unchanged(tmp_path: Path) -> None:
    destination = generation(tmp_path / "out", "old")
    partial = tmp_path / ".out.capture-stage-partial"; partial.mkdir(); (partial / "proof.png").write_bytes(b"partial")
    # Validation/failure occurs before publication; cleanup owns only private stage.
    partial.rename(tmp_path / ".discarded"); capture.shutil.rmtree(tmp_path / ".discarded")
    assert_complete(destination, ("old",))
    manifest_stage = generation(tmp_path / ".out.capture-stage-manifest", "new")
    capture.shutil.rmtree(manifest_stage)  # interruption after manifest, before publish
    assert_complete(destination, ("old",))


def test_input_drift_aborts_before_publication(tmp_path: Path) -> None:
    destination = generation(tmp_path / "out", "old"); source = tmp_path / "input"; source.write_text("a")
    before = capture._digest([source.read_text()]); source.write_text("b")
    assert capture._digest([source.read_text()]) != before
    assert_complete(destination, ("old",))


def test_tracked_runtime_inventory_drift_is_a_release_precondition_failure() -> None:
    before = [{"path": "scripts/runtime.py", "type": "regular", "sha256": "a"}]
    after = [{"path": "scripts/runtime.py", "type": "regular", "sha256": "b"}]
    with pytest.raises(capture.CapturePreconditionError, match="input inventory drifted"):
        capture._assert_input_inventory_stable(before, after)


def test_concurrent_publishers_are_serialized_and_complete(tmp_path: Path) -> None:
    destination = generation(tmp_path / "out", "old")
    stages = [generation(tmp_path / f".out.capture-stage-{n}", f"new{n}") for n in (1, 2)]
    errors = []
    threads = [threading.Thread(target=lambda s=s: _publish(s, destination), daemon=True) for s in stages]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=5)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert_complete(destination, ("new1", "new2")); assert not list(tmp_path.glob(".out.capture-stage-*"))


def test_stale_backup_recovery_and_cleanup(tmp_path: Path) -> None:
    authority = tmp_path / ".out.capture-authority"; authority.mkdir(mode=0o700); authority.chmod(0o700)
    backup = generation(authority / "backup", "old")
    stage = generation(tmp_path / ".out.capture-stage-new", "new")
    _publish(stage, tmp_path / "out")
    assert_complete(tmp_path / "out", ("new",)); assert not backup.exists()


@pytest.mark.parametrize("leaf", ["owner.lock", "journal.json"])
def test_coordination_symlink_rejected_without_outside_mutation(tmp_path: Path, leaf: str) -> None:
    destination = generation(tmp_path / "out", "old"); stage = generation(tmp_path / ".out.capture-stage-new", "new")
    authority = tmp_path / ".out.capture-authority"; authority.mkdir(mode=0o700); authority.chmod(0o700)
    outside = tmp_path / "outside"; outside.write_text("sentinel")
    (authority / leaf).symlink_to(outside)
    with pytest.raises((RuntimeError, OSError)): _publish(stage, destination)
    assert outside.read_text() == "sentinel"; assert_complete(destination, ("old",))


def test_coordination_authority_symlink_rejected(tmp_path: Path) -> None:
    destination = generation(tmp_path / "out", "old"); stage = generation(tmp_path / ".out.capture-stage-new", "new")
    outside = tmp_path / "outside"; outside.mkdir(mode=0o700)
    (tmp_path / ".out.capture-authority").symlink_to(outside, target_is_directory=True)
    with pytest.raises((RuntimeError, OSError)): _publish(stage, destination)
    assert list(outside.iterdir()) == []; assert_complete(destination, ("old",))


def test_concurrent_reader_observes_complete_or_transient_absence_never_mixed(tmp_path: Path) -> None:
    destination = generation(tmp_path / "out", "old"); stage = generation(tmp_path / ".out.capture-stage-new", "new")
    observations = []; stop = threading.Event()
    def reader():
        while not stop.is_set():
            try:
                fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    mfd = os.open("manifest.json", os.O_RDONLY, dir_fd=fd); pfd = os.open("proof.png", os.O_RDONLY, dir_fd=fd)
                    try: value = os.read(mfd, 64).decode(); png = os.read(pfd, 64)
                    finally: os.close(mfd); os.close(pfd)
                finally: os.close(fd)
                observations.append("complete" if png == b"PNG-" + value.encode() else "mixed")
            except FileNotFoundError: observations.append("absent")
    thread = threading.Thread(target=reader); thread.start()
    _publish(stage, destination)
    stop.set(); thread.join()
    assert "mixed" not in observations; assert_complete(destination, ("new",))


@pytest.mark.parametrize("phase", ["after-dest-to-backup", "after-stage-to-dest"])
def test_sigkill_documents_transient_absence_and_next_run_recovers(tmp_path: Path, phase: str) -> None:
    destination = generation(tmp_path / "out", "old"); stage = generation(tmp_path / ".out.capture-stage-kill", "killed")
    code = f"""import importlib.util,os
from pathlib import Path
s=importlib.util.spec_from_file_location('c',{str(ROOT / 'scripts/capture_ui_overhaul.py')!r});m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
real=m.os.replace
def replace(source,target,*a,**k):
  result=real(source,target,*a,**k)
  source_name=str(source); target_name=str(target)
  if ({phase!r}=='after-dest-to-backup' and target_name=='backup') or ({phase!r}=='after-stage-to-dest' and source_name=={stage.name!r}): os.kill(os.getpid(),9)
  return result
m.os.replace=replace
m._verify_publication_provenance=lambda expected:None
m._publish_generation(Path({str(stage)!r}),Path({str(destination)!r}),expected_provenance=m.PublicationProvenance('a'*40,'b'*40,True,'clean'))
"""
    result = subprocess.run([sys.executable, "-c", code]); assert result.returncode < 0
    if destination.exists(): assert_complete(destination, ("old", "killed"))
    recovery = generation(tmp_path / ".out.capture-stage-recovery", "recovered")
    _publish(recovery, destination)
    assert_complete(destination, ("recovered",)); assert not (tmp_path / ".out.capture-authority" / "backup").exists()


def test_cross_process_publishers_flock_and_finish_complete(tmp_path: Path) -> None:
    destination = generation(tmp_path / "out", "old")
    stages = [generation(tmp_path / f".out.capture-stage-p{n}", f"p{n}") for n in (1, 2)]
    processes = []
    for stage in stages:
        code = f"""import importlib.util
from pathlib import Path
s=importlib.util.spec_from_file_location('c',{str(ROOT / 'scripts/capture_ui_overhaul.py')!r});m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m._verify_publication_provenance=lambda expected:None;m._publish_generation(Path({str(stage)!r}),Path({str(destination)!r}),expected_provenance=m.PublicationProvenance('a'*40,'b'*40,True,'clean'))"""
        processes.append(subprocess.Popen([sys.executable, "-c", code]))
    assert [p.wait(timeout=10) for p in processes] == [0, 0]
    assert_complete(destination, ("p1", "p2")); assert not (tmp_path / ".out.capture-authority" / "backup").exists()


def test_post_open_authority_substitution_fails_closed_without_outside_mutation(tmp_path: Path, monkeypatch) -> None:
    destination = generation(tmp_path / "out", "old"); stage = generation(tmp_path / ".out.capture-stage-new", "new")
    outside = tmp_path / "outside"; outside.mkdir()
    quarantined = tmp_path / ".out.capture-authority-pinned"
    real_open = capture.os.open
    substituted = False
    def open_path(path, *args, **kwargs):
        nonlocal substituted
        fd = real_open(path, *args, **kwargs)
        if str(path) == ".out.capture-authority" and not substituted:
            substituted = True
            os.rename(tmp_path / ".out.capture-authority", quarantined)
            (tmp_path / ".out.capture-authority").symlink_to(outside, target_is_directory=True)
        return fd
    monkeypatch.setattr(capture.os, "open", open_path)
    with pytest.raises(RuntimeError, match="authority identity changed"):
        _publish(stage, destination)
    assert_complete(destination, ("old",)); assert list(outside.iterdir()) == []
    assert not (quarantined / "journal.json").exists() and not (quarantined / "backup").exists()
    assert stage.exists()


def test_unsafe_authority_reject_repair_retry_has_stable_fds_and_no_deadlock(tmp_path: Path) -> None:
    destination = generation(tmp_path / "out", "old"); stage = generation(tmp_path / ".out.capture-stage-new", "new")
    authority = tmp_path / ".out.capture-authority"; authority.mkdir(); authority.chmod(0o755)
    before = len(list(Path("/proc/self/fd").iterdir()))
    with pytest.raises(RuntimeError): _publish(stage, destination)
    assert len(list(Path("/proc/self/fd").iterdir())) == before
    authority.chmod(0o700)
    _publish(stage, destination)
    assert_complete(destination, ("new",))
    assert len(list(Path("/proc/self/fd").iterdir())) == before


def test_release_scenario_manifest_covers_the_final_theme_and_surface_matrix() -> None:
    manifest = capture.release_scenario_manifest()
    scenarios = manifest["scenarios"]
    assert len(scenarios) == len({row["id"] for row in scenarios})
    assert {row["resolved_theme"] for row in scenarios if row["resolved_theme"]} >= {
        "bone", "slate", "clay", "plum",
    }
    covered = {coverage for row in scenarios for coverage in row["coverage"]}
    assert {
        "appearance-palettes", "studio-rail", "studio-overlays",
        "studio-focus", "studio-split", "studio-mobile",
        "graph-map", "graph-lenses", "graph-typed-path", "graph-focus", "graph-mobile",
        "search-result", "search-no-result", "search-error", "search-manual-copy",
        "live", "static", "spa", "single-file", "no-js",
        "boot-success", "boot-blocked", "boot-failure",
        "forced-colors", "reduced-motion", "zoom-400",
    } <= covered


def test_release_scenario_manifest_exposes_complete_f01_f24_capture_contract() -> None:
    manifest = capture.release_scenario_manifest()
    rows = manifest["scenarios"]
    covered = {item for row in rows for item in row["coverage"]}
    filenames = {row["artifact"] for row in rows}

    assert {f"F{number:02d}" for number in range(1, 25)} <= covered
    assert {row["bundle"] for row in rows} >= {"demo", "dense-docs", "empty-okf"}
    assert {row["viewport"]["width"] for row in rows} >= {390, 1280, 1440}
    assert {
        "index-empty", "index-filter-mismatch", "concept-outline", "concept-rendered",
        "concept-source", "concept-split", "concept-focus-view", "search-expanded",
        "graph-keyboard-journey", "clay-showcase", "ledger-spacing",
        "studio-idle", "studio-error", "studio-retry", "studio-read-only-authority",
        "studio-proposal-enabled", "studio-unavailable",
    } <= covered
    assert {"zoom-200.png", "zoom-400.png"} <= filenames
    assert manifest["supporting_artifacts"] == [
        "keyboard-focus-trace.json", "graph-at-journey.json", "zoom-evidence.json",
        "manual-screen-reader-session.md",
    ]
    assert manifest["screen_reader_proof"]["status"] == "PENDING HUMAN"
    assert manifest["screen_reader_proof"]["automated"] is False


def test_zoom_contract_requires_real_chrome_page_zoom_and_fails_closed_on_proxy() -> None:
    rows = capture.release_scenario_manifest()["scenarios"]
    zoom = [row for row in rows if row["zoom_percent"]]
    assert {row["zoom_percent"] for row in zoom} == {200, 400}
    assert all(row["zoom_mechanism"] == "chrome-page-zoom-keyboard" for row in zoom)

    with pytest.raises(capture.CaptureFailure) as caught:
        capture._assert_zoom_evidence(
            {"mechanism": "effective-css-viewport", "zoomPercent": 400},
            expected_percent=400,
        )
    assert caught.value.diagnostic["code"] == "zoom-mechanism-mismatch"


def test_zoom_identity_owns_inner_width_and_keeps_client_width_geometry() -> None:
    """Zoom identity is innerWidth; a scrollbar-reduced clientWidth is geometry.

    At genuine 200% on a page with a classic vertical scrollbar the layout
    clientWidth reads 712 (≈8 CSS px of scrollbar), while innerWidth halves
    exactly to 720. The oracle must therefore compare innerWidth and keep
    clientWidth as recorded geometry, or every genuine 200% capture is
    rejected as zoom-unsupported.
    """

    def evidence(inner: int, client: int) -> dict:
        return {"mechanism": "chrome-page-zoom-keyboard", "zoomPercent": 200,
                "innerWidth": inner, "cssViewportWidth": client,
                "expectedCssViewportWidth": 720, "devicePixelRatio": 2,
                "devicePixelRatioBefore": 1, "baselinePercent": 100,
                "baselineVerified": True, "sessionBinding": "bound-page",
                "sessionBindingVerified": True}

    # Genuine 200% with a vertical scrollbar passes on the exact innerWidth.
    capture._assert_zoom_evidence(evidence(720, 712), expected_percent=200)

    # innerWidth is the identity: a viewport that did not halve fails closed.
    with pytest.raises(capture.CaptureFailure) as caught:
        capture._assert_zoom_evidence(evidence(712, 712), expected_percent=200)
    assert caught.value.diagnostic["code"] == "zoom-mechanism-mismatch"


def test_manual_screen_reader_template_is_truthful_and_never_automated_proof(tmp_path: Path) -> None:
    path = tmp_path / "manual-screen-reader-session.md"
    capture._write_manual_screen_reader_template(path)
    text = path.read_text()
    assert "NOT EXECUTED" in text and "PENDING HUMAN" in text
    assert "does not count as executed screen-reader proof" in text


def test_supporting_artifacts_are_required_by_final_capture_validation(tmp_path: Path) -> None:
    for name in capture._SUPPORTING_ARTIFACTS:
        assert name not in {spec.artifact for spec in capture.SCENARIO_SPECS}
    with pytest.raises(capture.CapturePreconditionError, match="supporting evidence"):
        capture._validate_supporting_artifacts(tmp_path)


def test_every_release_action_is_dispatched() -> None:
    assert {spec.action for spec in capture.SCENARIO_SPECS} <= capture._IMPLEMENTED_ACTIONS


def test_live_server_cleans_start_failure_normal_exit_and_kill_fallback(monkeypatch, tmp_path: Path) -> None:
    class Process:
        def __init__(self, *, timeout=False, exited=False):
            self.timeout = timeout; self.returncode = 9 if exited else None
            self.terminated = self.killed = False
        def poll(self): return self.returncode
        def terminate(self): self.terminated = True
        def kill(self): self.killed = True; self.returncode = -9
        def wait(self, timeout=None):
            if self.timeout and not self.killed: raise subprocess.TimeoutExpired("serve", timeout)
            self.returncode = self.returncode if self.returncode is not None else 0
            return self.returncode

    state_roots = []
    popen_envs = []
    for process in (Process(), Process(timeout=True)):
        def popen(*_a, **kwargs):
            popen_envs.append(kwargs["env"])
            state_roots.append(Path(kwargs["env"]["OKF_LOOM_SESSION_ROOT"]))
            assert state_roots[-1].is_dir()
            assert stat.S_IMODE(state_roots[-1].stat().st_mode) == 0o700
            return process
        monkeypatch.setattr(capture.subprocess, "Popen", popen)
        monkeypatch.setattr(capture, "_wait", lambda *_a, **_k: None)
        runtime = capture.ImmutableRuntimeWorkspace.for_test(tmp_path, tmp_path)
        server = capture._LiveServer(tmp_path, runtime=runtime)
        with server as base:
            assert base.startswith("http://127.0.0.1:")
            server.assert_running()
        assert process.terminated and server.process is None
        assert process.killed is process.timeout
        _assert_sanitized_tree(state_roots[-1])
        assert server.cleanup_result.disposition == "sanitized-retained-tree"
        assert server.retention_record["contentState"] == "sanitized-empty-tree"
        shutil.rmtree(state_roots[-1])

    assert state_roots[0] != state_roots[1]
    assert all(env["OKF_LOOM_IMMUTABLE_ROOT"] == str(tmp_path) for env in popen_envs)

    exited = Process(exited=True)
    exited_roots = []
    def exited_popen(*_a, **kwargs):
        exited_roots.append(Path(kwargs["env"]["OKF_LOOM_SESSION_ROOT"]))
        return exited
    monkeypatch.setattr(capture.subprocess, "Popen", exited_popen)
    monkeypatch.setattr(capture, "_wait", lambda *_a, **_k: None)
    runtime = capture.ImmutableRuntimeWorkspace.for_test(tmp_path, tmp_path)
    server = capture._LiveServer(tmp_path, runtime=runtime)
    with pytest.raises(capture.CaptureFailure, match="server-exit"):
        with server:
            server.assert_running()
    assert server.process is None
    assert exited_roots; _assert_sanitized_tree(exited_roots[0])
    assert server.cleanup_result.disposition == "sanitized-retained-tree"
    shutil.rmtree(exited_roots[0])

    failed = Process()
    failed_roots = []
    def failed_popen(*_a, **kwargs):
        failed_roots.append(Path(kwargs["env"]["OKF_LOOM_SESSION_ROOT"]))
        return failed
    monkeypatch.setattr(capture.subprocess, "Popen", failed_popen)
    monkeypatch.setattr(capture, "_wait", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("not ready")))
    failed_server = capture._LiveServer(tmp_path, runtime=runtime)
    with pytest.raises(RuntimeError, match="not ready"):
        with failed_server: pass
    assert failed.terminated
    assert failed_roots; _assert_sanitized_tree(failed_roots[0])
    assert failed_server.cleanup_result.disposition == "sanitized-retained-tree"
    shutil.rmtree(failed_roots[0])


def test_sealed_server_rejects_stale_listener_before_spawn(monkeypatch, tmp_path: Path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    monkeypatch.setattr(capture.subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("spawned")))
    runtime = capture.ImmutableRuntimeWorkspace.for_test(tmp_path, tmp_path)
    try:
        with pytest.raises(capture.CapturePreconditionError, match="already has a listener"):
            capture._LiveServer(tmp_path, runtime=runtime, port=port).__enter__()
    finally:
        listener.close()


def test_server_provenance_rejects_wrong_cwd_and_wrong_assets(tmp_path: Path) -> None:
    runtime_file = tmp_path / "app.js"
    runtime_file.write_text("console.log('sealed')")
    runtime = capture.ImmutableRuntimeWorkspace.for_test(tmp_path, tmp_path)
    asset = runtime.inventory[0]
    base = {
        "id": "live-test", "kind": "sealed-live-studio", "started_by_runner": True,
        "command": ["serve"], "cwd": str(runtime.root), "pid": 123, "port": 54321,
        "head": runtime.head, "tree": runtime.tree, "readiness": "http-200-owned-process",
        "served_bytes": [
            {"path": "/", "role": "entrypoint", "bytes": 10, "sha256": "a" * 64},
            {"path": "/app.js", "role": "critical-asset", "bytes": asset["bytes"],
             "sha256": asset["sha256"]},
        ],
    }
    capture._verify_server_provenance(base, runtime=runtime)
    wrong_cwd = {**base, "cwd": str(tmp_path / "other")}
    with pytest.raises(capture.CapturePreconditionError, match="wrong source"):
        capture._verify_server_provenance(wrong_cwd, runtime=runtime)
    wrong_asset = {**base, "served_bytes": [base["served_bytes"][0],
                   {**base["served_bytes"][1], "sha256": "b" * 64}]}
    with pytest.raises(capture.CapturePreconditionError, match="served live assets"):
        capture._verify_server_provenance(wrong_asset, runtime=runtime)


def test_server_readiness_reports_owned_process_failure() -> None:
    class Failed:
        returncode = 73
        def poll(self): return self.returncode
    with pytest.raises(capture.CaptureFailure) as caught:
        capture._wait("http://127.0.0.1:1", Failed(), timeout=.01)
    assert caught.value.diagnostic == {
        "code": "server-start-exit", "phase": "server-start", "returncode": 73,
        "base": "http://127.0.0.1:1",
    }


def test_static_server_launches_verifies_and_cleans_owned_process(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    build_root = tmp_path / "build"
    build_root.mkdir()
    (build_root / "index.html").write_text('<!doctype html><script src="app.js"></script>')
    (build_root / "app.js").write_text("console.log('sealed')")
    runtime = capture.ImmutableRuntimeWorkspace.for_test(runtime_root, runtime_root)
    inventory = capture._tree_inventory(build_root)
    build = {"target": "static", "command": ["build"], "cwd": str(runtime.root),
             "head": runtime.head, "tree": runtime.tree, "inventory": inventory,
             "inventory_digest": capture._digest(inventory)}
    server = capture._StaticServer(build_root, runtime=runtime, target="static", build=build)
    with server as base:
        port = int(base.rsplit(":", 1)[1])
        assert server.process is not None and server.process.poll() is None
        assert server.provenance and {row["role"] for row in server.provenance["served_bytes"]} == {
            "entrypoint", "critical-asset",
        }
    assert server.process is None
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0


def test_live_server_real_event_write_stays_outside_immutable_runtime(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    shutil.copytree(ROOT / "scripts", runtime_root / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    bundle = runtime_root / "bundle"
    bundle.mkdir()
    (bundle / "index.md").write_text("# Immutable live fixture\n")
    (bundle / "proof.md").write_text("---\ntype: Note\ntitle: Proof\n---\n\nProof\n")
    rows = []
    for path in sorted(runtime_root.rglob("*")):
        if path.is_file():
            relative = path.relative_to(runtime_root).as_posix()
            rows.append({"path": relative, "git_mode": "100755" if os.access(path, os.X_OK) else "100644",
                         "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    runtime = capture.ImmutableRuntimeWorkspace.for_test(runtime_root, tmp_path)
    runtime.inventory = tuple(rows)
    before = capture._runtime_inventory(runtime)

    server = capture._LiveServer(bundle, mode="editable", runtime=runtime)
    with server as base:
        assert server.state_root is not None
        state_root = server.state_root
        request = urllib.request.Request(
            base + "/__presence", method="POST",
            data=json.dumps({"actor": "agent", "state": "watching"}).encode(),
            headers={"Content-Type": "application/json", "X-OKF-Token": (state_root / ".token").read_text(),
                     "Origin": base},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
        assert (state_root / "events.jsonl").is_file()
        assert not state_root.is_relative_to(runtime_root)
        assert not (bundle / ".okf-loom/session").exists()
        assert capture._runtime_inventory(runtime) == before
    _assert_sanitized_tree(state_root)
    assert server.cleanup_result.disposition == "sanitized-retained-tree"
    assert server.retention_record["contentState"] == "sanitized-empty-tree"
    shutil.rmtree(state_root)
    assert capture._runtime_inventory(runtime) == before


def test_session_root_override_rejects_unsafe_authorities(monkeypatch, tmp_path: Path) -> None:
    from okf_loom.studio import Studio
    bundle = tmp_path / "bundle"; bundle.mkdir()
    outside = tmp_path / "outside"; outside.mkdir(mode=0o700)

    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", "relative")
    with pytest.raises(ValueError, match="absolute"):
        Studio.for_configured_bundle(bundle)
    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(bundle / "state"))
    with pytest.raises(ValueError, match="outside"):
        Studio.for_configured_bundle(bundle)
    outside.chmod(0o755)
    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(outside))
    with pytest.raises(ValueError, match="private"):
        Studio.for_configured_bundle(bundle)
    outside.chmod(0o700)
    alias = tmp_path / "alias"; alias.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(alias))
    with pytest.raises(ValueError, match="private"):
        Studio.for_configured_bundle(bundle)


def test_session_root_rejections_have_zero_mutation_and_created_suffix_rolls_back(
    monkeypatch, tmp_path: Path,
) -> None:
    from okf_loom.studio import Studio
    bundle = tmp_path / "bundle"; bundle.mkdir(mode=0o700)
    immutable = tmp_path / "immutable"; immutable.mkdir(mode=0o700)
    monkeypatch.setenv("OKF_LOOM_IMMUTABLE_ROOT", str(immutable))

    for candidate in (bundle / "nested" / "state", immutable / "nested" / "state"):
        monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(candidate))
        with pytest.raises(ValueError, match="outside"):
            Studio.for_configured_bundle(bundle)
        assert not candidate.parent.exists()

    external = tmp_path / "external"; external.mkdir(mode=0o700)
    target = tmp_path / "target"; target.mkdir(mode=0o700)
    alias = external / "alias"; alias.symlink_to(target, target_is_directory=True)
    candidate = alias / "missing"
    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(candidate))
    with pytest.raises(ValueError, match="private"):
        Studio.for_configured_bundle(bundle)
    assert not (target / "missing").exists()

    candidate = external / "first" / "second"
    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(candidate))
    real_mkdir = os.mkdir
    def fail_second(path, mode=0o777, *, dir_fd=None):
        if path == "second":
            raise OSError("injected create failure")
        return real_mkdir(path, mode, dir_fd=dir_fd)
    monkeypatch.setattr(os, "mkdir", fail_second)
    with pytest.raises(ValueError, match="private"):
        Studio.for_configured_bundle(bundle)
    assert not (external / "first").exists()


def test_session_root_rejects_wrong_uid_and_modes_directly(monkeypatch, tmp_path: Path) -> None:
    from okf_loom import studio
    bundle = tmp_path / "bundle"; bundle.mkdir(mode=0o700)
    external = tmp_path / "external"; external.mkdir(mode=0o700)
    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(external))

    external.chmod(0o750)
    with pytest.raises(ValueError, match="private"):
        studio.Studio.for_configured_bundle(bundle)
    external.chmod(0o700)

    real_fstat = studio.os.fstat
    final_identity = external.stat()
    def wrong_uid(fd):
        value = real_fstat(fd)
        if (value.st_dev, value.st_ino) == (final_identity.st_dev, final_identity.st_ino):
            values = list(value); values[4] = value.st_uid + 1
            return os.stat_result(values)
        return value
    monkeypatch.setattr(studio.os, "fstat", wrong_uid)
    with pytest.raises(ValueError, match="private"):
        studio.Studio.for_configured_bundle(bundle)


@pytest.mark.parametrize("failure", ["popen", "terminate", "wait", "kill", "final-wait"])
def test_live_server_exceptional_cleanup_sanitizes_only_after_confirmed_exit(
    monkeypatch, tmp_path: Path, failure: str,
) -> None:
    class Process:
        returncode = None
        def __init__(self): self.killed = False
        def poll(self): return self.returncode
        def terminate(self):
            if failure == "terminate": raise OSError("terminate")
        def wait(self, timeout=None):
            if not self.killed:
                if failure == "wait": raise OSError("wait")
                raise subprocess.TimeoutExpired("serve", timeout)
            if failure == "final-wait": raise OSError("final wait")
            self.returncode = -9; return self.returncode
        def kill(self):
            if failure == "kill": raise OSError("kill")
            self.killed = True

    roots = []
    def popen(*_args, **kwargs):
        roots.append(Path(kwargs["env"]["OKF_LOOM_SESSION_ROOT"]))
        if failure == "popen": raise OSError("popen")
        return Process()
    monkeypatch.setattr(capture.subprocess, "Popen", popen)
    monkeypatch.setattr(capture, "_wait", lambda *_a, **_k: None)
    runtime = capture.ImmutableRuntimeWorkspace.for_test(tmp_path, tmp_path)
    server = capture._LiveServer(tmp_path, runtime=runtime)
    with pytest.raises(capture.CaptureFailure) as caught:
        with server: pass
    assert caught.value.diagnostic["code"] == (
        "server-spawn-failure" if failure == "popen" else "server-cleanup-failure")
    assert server.process is None and server._state_owner is None and server.state_root is None
    if failure in {"kill", "final-wait"}:
        assert roots[0].exists()
        retained = capture._RETAINED_LIVE_STATE.pop()
        result = retained.dispose()
        assert result.disposition == "sanitized-retained-tree"
        retained.close()
    else:
        _assert_sanitized_tree(roots[0])
        assert server.cleanup_result.disposition == "sanitized-retained-tree"
    shutil.rmtree(roots[0])


def test_live_server_processlookup_uses_poll_confirmation(monkeypatch, tmp_path: Path) -> None:
    class Process:
        returncode = None
        def poll(self): return self.returncode
        def terminate(self): self.returncode = 0; raise ProcessLookupError()
        def wait(self, timeout=None): return self.returncode
    roots = []
    monkeypatch.setattr(capture.subprocess, "Popen", lambda *_a, **k: (roots.append(Path(k["env"]["OKF_LOOM_SESSION_ROOT"])) or Process()))
    monkeypatch.setattr(capture, "_wait", lambda *_a, **_k: None)
    runtime = capture.ImmutableRuntimeWorkspace.for_test(tmp_path, tmp_path)
    server = capture._LiveServer(tmp_path, runtime=runtime)
    with server: pass
    _assert_sanitized_tree(roots[0])
    assert server.cleanup_result.disposition == "sanitized-retained-tree"
    shutil.rmtree(roots[0])


def test_unconfirmed_live_state_survives_parent_exit_and_child_can_finish(tmp_path: Path) -> None:
    root_file = tmp_path / "root.txt"
    child_file = tmp_path / "child.txt"
    code = f"""import importlib.util,subprocess,sys,time
from pathlib import Path
s=importlib.util.spec_from_file_location('capture',{str(ROOT / 'scripts/capture_ui_overhaul.py')!r})
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
root=Path({str(tmp_path)!r})/'retained';root.mkdir(mode=0o700)
Path({str(root_file)!r}).write_text(str(root))
subprocess.Popen([sys.executable,'-c',"import time;from pathlib import Path;time.sleep(.3);Path(r'{str(child_file)}').write_text((Path(r'{str(tmp_path / 'retained')}')/'state').read_text());(Path(r'{str(tmp_path / 'retained')}')/'late').write_text('late')"])
(root/'state').write_text('durable')
sys.path.insert(0, {str(ROOT / 'scripts')!r})
from okf_loom.session_authority import resolve_session_authority
m._retain_live_state(resolve_session_authority(root, approved_root=Path({str(tmp_path)!r})), [])
"""
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
    deadline = time.monotonic() + 5
    while not child_file.exists() and time.monotonic() < deadline:
        time.sleep(.05)
    retained = Path(root_file.read_text())
    assert child_file.read_text() == "durable"
    assert (retained / "late").read_text() == "late"
    shutil.rmtree(retained)


def test_live_server_confirmed_cleanup_ignores_replacement_after_substitution(
    monkeypatch, tmp_path: Path,
) -> None:
    class Process:
        returncode = None
        def poll(self): return self.returncode
        def terminate(self): self.returncode = 0
        def wait(self, timeout=None): return self.returncode
    monkeypatch.setattr(capture.subprocess, "Popen", lambda *_a, **_k: Process())
    monkeypatch.setattr(capture, "_wait", lambda *_a, **_k: None)
    runtime = capture.ImmutableRuntimeWorkspace.for_test(tmp_path, tmp_path)
    server = capture._LiveServer(tmp_path, runtime=runtime)
    with server:
        original = server.state_root
        assert original is not None
        owned = original.with_name(original.name + "-owned")
        original.rename(owned)
        original.mkdir(mode=0o700)
        (original / "replacement").write_text("untouched")
    _assert_sanitized_tree(owned)
    assert server.cleanup_result.disposition == "sanitized-retained-tree"
    assert server.cleanup_result.reachable_path == owned
    assert (original / "replacement").read_text() == "untouched"
    shutil.rmtree(owned)
    shutil.rmtree(original)


def test_live_server_unconfirmed_retention_records_owned_identity_not_replacement(
    monkeypatch, tmp_path: Path,
) -> None:
    class Process:
        returncode = None
        def poll(self): return None
        def terminate(self): raise OSError("still live")
        def wait(self, timeout=None): raise subprocess.TimeoutExpired("serve", timeout)
        def kill(self): raise OSError("still live")
    monkeypatch.setattr(capture.subprocess, "Popen", lambda *_a, **_k: Process())
    monkeypatch.setattr(capture, "_wait", lambda *_a, **_k: None)
    runtime = capture.ImmutableRuntimeWorkspace.for_test(tmp_path, tmp_path)
    server = capture._LiveServer(tmp_path, runtime=runtime)
    with pytest.raises(capture.CaptureFailure) as caught:
        with server:
            original = server.state_root
            assert original is not None
            owned = original.with_name(original.name + "-owned")
            original.rename(owned)
            original.mkdir(mode=0o700)
            (original / "replacement").write_text("untouched")
    authority = capture._RETAINED_LIVE_STATE.pop()
    record = authority.retention_record()
    assert caught.value.diagnostic["stateDisposition"] == "active-retained"
    assert server.retention_record["contentState"] == "potentially-secret-bearing"
    assert (record["device"], record["inode"]) == (owned.stat().st_dev, owned.stat().st_ino)
    assert record["reachablePath"] == str(owned)
    assert (original / "replacement").read_text() == "untouched"
    result = authority.dispose()
    assert result.disposition == "sanitized-retained-tree"
    authority.close()
    shutil.rmtree(owned)
    shutil.rmtree(original)


def test_resolved_session_authority_stays_pinned_after_final_substitution(monkeypatch, tmp_path: Path) -> None:
    from okf_loom.studio import Studio
    bundle = tmp_path / "bundle"; bundle.mkdir()
    external = tmp_path / "external"; external.mkdir(mode=0o700)
    tmp_path.chmod(0o700)
    monkeypatch.setenv("OKF_LOOM_SESSION_TRUSTED_ROOT", str(tmp_path))
    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(external))
    studio = Studio.for_configured_bundle(bundle)
    pinned = tmp_path / "pinned"
    external.rename(pinned)
    external.mkdir(mode=0o700)

    studio.ensure_session()

    assert (pinned / "..directives-transaction.okf-lock").is_file()
    assert list(external.iterdir()) == []


def test_session_authority_descriptor_api_never_redirects_after_substitution(
    monkeypatch, tmp_path: Path,
) -> None:
    from okf_loom.session_authority import resolve_session_authority
    tmp_path.chmod(0o700)
    original = tmp_path / "original"; original.mkdir(mode=0o700)
    authority = resolve_session_authority(original, approved_root=tmp_path)
    pinned = tmp_path / "pinned"
    original.rename(pinned)
    original.mkdir(mode=0o700)
    try:
        authority.mkdir("nested/leaf", parents=True)
        fd = authority.open("nested/leaf/value.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            os.write(fd, b"pinned")
        finally:
            os.close(fd)
        authority.replace("nested/leaf/value.tmp", "nested/leaf/value")

        assert authority.list("nested/leaf") == ("value",)
        assert (pinned / "nested/leaf/value").read_bytes() == b"pinned"
        assert list(original.iterdir()) == []
    finally:
        authority.close()


@pytest.mark.parametrize("candidate", [Path("/tmp"), Path("/var/tmp")])
def test_system_temp_ancestor_cannot_be_the_session_authority(candidate: Path) -> None:
    from okf_loom.session_authority import resolve_session_authority
    before = candidate.stat()
    with pytest.raises(ValueError, match="child"):
        resolve_session_authority(candidate)
    after = candidate.stat()
    assert (after.st_dev, after.st_ino, after.st_mtime_ns) == (
        before.st_dev, before.st_ino, before.st_mtime_ns)


def test_private_child_below_system_temp_is_accepted_and_sanitized_with_root_retained() -> None:
    from okf_loom.session_authority import temporary_session_authority
    authority = temporary_session_authority(prefix="okf-authority-proof-", parent=Path("/tmp"))
    root = authority.display_path
    assert authority.mode == 0o700
    try:
        result = authority.dispose()
        assert result.disposition == "sanitized-retained-tree"
        assert authority.list() == ()
        assert root.is_dir()
        assert (result.device, result.inode) == (root.stat().st_dev, root.stat().st_ino)
    finally:
        authority.close()
        shutil.rmtree(root)


def test_approved_root_cannot_itself_be_the_session_authority(tmp_path: Path) -> None:
    from okf_loom.session_authority import resolve_session_authority
    tmp_path.chmod(0o700)
    before = tuple(tmp_path.iterdir())
    with pytest.raises(ValueError, match="child"):
        resolve_session_authority(tmp_path, approved_root=tmp_path)
    assert tuple(tmp_path.iterdir()) == before


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o777])
def test_session_authority_mkdir_rejects_non_private_mode_without_creation(
    tmp_path: Path, mode: int,
) -> None:
    from okf_loom.session_authority import resolve_session_authority
    tmp_path.chmod(0o700)
    root = tmp_path / "root"; root.mkdir(mode=0o700)
    authority = resolve_session_authority(root, approved_root=tmp_path)
    try:
        with pytest.raises(ValueError, match="0700"):
            authority.mkdir("unsafe", mode=mode)
        assert not (root / "unsafe").exists()
    finally:
        authority.close()


@pytest.mark.parametrize("substitution_point", ["before-dispose", "before-reachable-report"])
def test_session_authority_dispose_does_not_delete_substituted_path(
    tmp_path: Path, monkeypatch, substitution_point: str,
) -> None:
    from okf_loom.session_authority import resolve_session_authority
    tmp_path.chmod(0o700)
    original = tmp_path / "original"; original.mkdir(mode=0o700)
    authority = resolve_session_authority(original, approved_root=tmp_path)
    authority.mkdir("nested")
    fd = authority.open("nested/value", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    os.close(fd)
    owned = tmp_path / "owned-renamed"

    def substitute() -> None:
        original.rename(owned)
        original.mkdir(mode=0o700)
        (original / "replacement").write_text("untouched")

    if substitution_point == "before-dispose":
        substitute()
    else:
        real_reachable = authority.reachable_path
        def reachable_after_substitution():
            substitute()
            return real_reachable()
        monkeypatch.setattr(authority, "reachable_path", reachable_after_substitution)

    result = authority.dispose()

    assert result.disposition == "sanitized-retained-tree"
    _assert_sanitized_tree(owned)
    assert result.reachable_path == owned
    assert (original / "replacement").read_text() == "untouched"
    authority.close()
    shutil.rmtree(owned)


def test_session_authority_nested_cleanup_never_deletes_mutated_namespace(
    tmp_path: Path, monkeypatch,
) -> None:
    from okf_loom import session_authority

    tmp_path.chmod(0o700)
    root = tmp_path / "root"; root.mkdir(mode=0o700)
    nested = root / "nested"; nested.mkdir(mode=0o700)
    (nested / "owned-secret").write_text("secret")
    authority = session_authority.resolve_session_authority(root, approved_root=tmp_path)
    monkeypatch.setattr(capture, "_LIVE_STATE_REGISTRY", tmp_path / "registry" / "records.jsonl")
    nested_identity = nested.stat()
    real_empty = session_authority._empty_directory
    substituted = False

    def substitute_after_nested_sanitization(fd: int) -> None:
        nonlocal substituted
        real_empty(fd)
        info = os.fstat(fd)
        if not substituted and (info.st_dev, info.st_ino) == (
            nested_identity.st_dev, nested_identity.st_ino
        ):
            substituted = True
            nested.rename(root / "owned-nested")
            nested.mkdir(mode=0o700)
            (nested / "replacement").write_text("untouched")

    monkeypatch.setattr(session_authority, "_empty_directory", substitute_after_nested_sanitization)
    server = _cleanup_server_with_authority(authority)
    try:
        with pytest.raises(capture.CaptureFailure) as caught:
            server.__exit__(None, None, None)
        assert caught.value.diagnostic["stateDisposition"] == "cleanup-failed-retained"
        assert server.retention_record["contentState"] == "potentially-secret-bearing"
        assert tuple((root / "owned-nested").iterdir()) == ()
        assert (nested / "replacement").read_text() == "untouched"
        assert set(authority.list()) == {"owned-nested", "nested"}
    finally:
        capture._RETAINED_LIVE_STATE.remove(authority)
        authority.close()


def test_session_authority_nested_cleanup_rejects_substitution_between_stat_and_open(
    tmp_path: Path, monkeypatch,
) -> None:
    from okf_loom import session_authority

    tmp_path.chmod(0o700)
    root = tmp_path / "root"; root.mkdir(mode=0o700)
    nested = root / "nested"; nested.mkdir(mode=0o700)
    (nested / "owned-secret").write_text("secret")
    authority = session_authority.resolve_session_authority(root, approved_root=tmp_path)
    real_open = session_authority.os.open
    substituted = False

    def substitute_at_open(path, flags, *args, **kwargs):
        nonlocal substituted
        if path == "nested" and kwargs.get("dir_fd") == authority.fd and not substituted:
            substituted = True
            nested.rename(root / "owned-nested")
            nested.mkdir(mode=0o700)
            (nested / "replacement").write_text("untouched")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(session_authority.os, "open", substitute_at_open)
    try:
        with pytest.raises(OSError, match="changed before cleanup"):
            authority.dispose()
        assert (nested / "replacement").read_text() == "untouched"
        assert (root / "owned-nested" / "owned-secret").read_text() == "secret"
        assert authority.fd >= 0
    finally:
        authority.close()


def test_live_server_recursive_cleanup_failure_retains_descriptor_and_reports_truthfully(
    monkeypatch, tmp_path: Path,
) -> None:
    from okf_loom import session_authority

    class Process:
        returncode = None
        def poll(self): return self.returncode
        def terminate(self): self.returncode = 0
        def wait(self, timeout=None): return self.returncode

    monkeypatch.setattr(capture.subprocess, "Popen", lambda *_a, **_k: Process())
    monkeypatch.setattr(capture, "_wait", lambda *_a, **_k: None)
    monkeypatch.setattr(capture, "_LIVE_STATE_REGISTRY", tmp_path / "registry.jsonl")
    real_empty = session_authority._empty_directory
    def fail_after_partial_cleanup(fd):
        real_empty(fd)
        raise OSError("injected recursive cleanup failure")
    monkeypatch.setattr(session_authority, "_empty_directory", fail_after_partial_cleanup)

    runtime = capture.ImmutableRuntimeWorkspace.for_test(tmp_path, tmp_path)
    server = capture._LiveServer(tmp_path, runtime=runtime)
    with pytest.raises(capture.CaptureFailure) as caught:
        with server:
            root = server.state_root
            assert root is not None
            (root / "secret").write_text("secret")

    assert caught.value.diagnostic["code"] == "server-cleanup-failure"
    assert caught.value.diagnostic["processExitConfirmed"] is True
    assert caught.value.diagnostic["stateDisposition"] == "cleanup-failed-retained"
    assert server._state_authority is not None and server._state_authority.fd >= 0
    assert server.retention_record["contentState"] == "potentially-secret-bearing"
    registry = json.loads((tmp_path / "registry.jsonl").read_text().splitlines()[-1])
    assert registry["stateDisposition"] == "cleanup-failed-retained"
    assert registry["contentState"] == "potentially-secret-bearing"
    assert "secret\"" not in json.dumps(registry)
    retained = capture._RETAINED_LIVE_STATE.pop()
    monkeypatch.setattr(session_authority, "_empty_directory", real_empty)
    result = retained.dispose()
    retained.close()
    assert result.disposition == "sanitized-retained-tree"
    shutil.rmtree(retained.display_path)


def _cleanup_server_with_authority(authority):
    class DeadProcess:
        returncode = 0
        def poll(self): return 0

    runtime = capture.ImmutableRuntimeWorkspace.for_test(ROOT, ROOT)
    server = capture._LiveServer(ROOT, runtime=runtime)
    server.process = DeadProcess()
    server._state_authority = authority
    server.state_root = authority.display_path
    return server


def test_retention_registry_completes_short_writes_and_syncs_new_parent(
    monkeypatch, tmp_path: Path,
) -> None:
    from okf_loom.session_authority import resolve_session_authority

    tmp_path.chmod(0o700)
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    authority = resolve_session_authority(state, approved_root=tmp_path)
    registry_parent = tmp_path / "registry"
    monkeypatch.setattr(capture, "_LIVE_STATE_REGISTRY", registry_parent / "records.jsonl")
    real_write = os.write
    writes = []
    sync_modes = []

    def short_write(fd, payload):
        writes.append(len(payload))
        return real_write(fd, payload[:max(1, len(payload) // 3)])

    real_fsync = os.fsync
    def observed_fsync(fd):
        sync_modes.append(stat.S_IFMT(os.fstat(fd).st_mode))
        return real_fsync(fd)

    monkeypatch.setattr(capture.os, "write", short_write)
    monkeypatch.setattr(capture.os, "fsync", observed_fsync)
    server = _cleanup_server_with_authority(authority)
    server.__exit__(None, None, None)

    rows = [json.loads(line) for line in (registry_parent / "records.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["stateDisposition"] == "sanitized-retained-tree"
    assert rows[0]["contentState"] == "sanitized-empty-tree"
    assert len(writes) > 1
    assert stat.S_IFREG in sync_modes and stat.S_IFDIR in sync_modes
    assert authority.fd == -1
    assert "secret" not in json.dumps(rows[0]).lower()
    shutil.rmtree(state)


def test_retention_registry_rolls_back_partial_row_before_later_success(
    monkeypatch, tmp_path: Path,
) -> None:
    from okf_loom.session_authority import resolve_session_authority

    tmp_path.chmod(0o700)
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    authority = resolve_session_authority(state, approved_root=tmp_path)
    registry_parent = tmp_path / "registry"; registry_parent.mkdir(mode=0o700)
    registry = registry_parent / "records.jsonl"
    registry.write_text(json.dumps({"existing": True}) + "\n")
    registry.chmod(0o600)
    monkeypatch.setattr(capture, "_LIVE_STATE_REGISTRY", registry)
    real_write = os.write
    writes = 0

    def partial_then_fail(fd, payload):
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(fd, payload[:7])
        raise OSError("injected partial append failure")

    monkeypatch.setattr(capture.os, "write", partial_then_fail)
    failed = _cleanup_server_with_authority(authority)
    with pytest.raises(capture.CaptureFailure):
        failed.__exit__(None, None, None)
    assert registry.read_bytes().endswith(b"\n")
    assert [json.loads(line) for line in registry.read_text().splitlines()] == [{"existing": True}]

    monkeypatch.setattr(capture.os, "write", real_write)
    succeeded = _cleanup_server_with_authority(authority)
    succeeded.__exit__(None, None, None)
    rows = [json.loads(line) for line in registry.read_text().splitlines()]
    assert rows[0] == {"existing": True}
    assert rows[1]["stateDisposition"] == "sanitized-retained-tree"
    assert authority.fd == -1
    capture._RETAINED_LIVE_STATE.remove(authority)
    shutil.rmtree(state)


def test_retention_registry_rejects_corrupt_tail_without_mutation_and_retains_authority(
    monkeypatch, tmp_path: Path,
) -> None:
    from okf_loom.session_authority import resolve_session_authority

    tmp_path.chmod(0o700)
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    authority = resolve_session_authority(state, approved_root=tmp_path)
    registry_parent = tmp_path / "registry"; registry_parent.mkdir(mode=0o700)
    registry = registry_parent / "records.jsonl"
    original = json.dumps({"existing": True}).encode() + b"\n" + b'\xff{"torn":'
    registry.write_bytes(original)
    registry.chmod(0o600)
    monkeypatch.setattr(capture, "_LIVE_STATE_REGISTRY", registry)

    for attempt in range(2):
        errors = []
        result = capture._record_state_retention(
            authority, errors, disposition="cleanup-failed-retained",
            reason=f"corrupt-tail-{attempt}", retain_descriptor=True,
        )
        assert result["stateDisposition"] == "registry-corrupt"
        assert result["durable"] is False
        assert errors == ["state-registry:OSError"]
        assert registry.read_bytes() == original
        assert authority.fd >= 0

    capture._RETAINED_LIVE_STATE.remove(authority)
    capture._RETAINED_LIVE_STATE.remove(authority)
    authority.close()
    shutil.rmtree(state)


def test_new_retention_registry_cleanup_identity_mismatch_preserves_substitute_and_original(
    monkeypatch, tmp_path: Path,
) -> None:
    from okf_loom.session_authority import resolve_session_authority

    tmp_path.chmod(0o700)
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    authority = resolve_session_authority(state, approved_root=tmp_path)
    registry_parent = tmp_path / "registry"
    registry = registry_parent / "records.jsonl"
    monkeypatch.setattr(capture, "_LIVE_STATE_REGISTRY", registry)
    real_write = os.write
    writes = 0

    def partial_then_fail(fd, payload):
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(fd, payload[:11])
        raise OSError("injected new-registry append failure")

    real_stat = os.stat
    substituted = False
    uncertain = registry_parent / "uncertain-original"
    sentinel = registry_parent / "safe-sentinel"

    def substitute_before_identity(name, *args, **kwargs):
        nonlocal substituted
        if name == registry.name and kwargs.get("dir_fd") is not None and not substituted:
            substituted = True
            os.rename(registry, uncertain)
            sentinel.write_bytes(b"safe sentinel")
            sentinel.chmod(0o600)
            os.rename(sentinel, registry)
        return real_stat(name, *args, **kwargs)

    monkeypatch.setattr(capture.os, "write", partial_then_fail)
    monkeypatch.setattr(capture.os, "stat", substitute_before_identity)
    result = capture._record_state_retention(
        authority, [], disposition="cleanup-failed-retained",
        reason="new-registry-identity-mismatch", retain_descriptor=True,
    )

    assert substituted is True
    assert result["stateDisposition"] == "registry-corrupt"
    assert result["durable"] is False
    assert registry.read_bytes() == b"safe sentinel"
    # Rollback truncated the still-open original inode before discovering that
    # its pathname had been substituted; the uncertain inode remains available
    # for operator inspection instead of being removed through the stale name.
    assert uncertain.is_file() and uncertain.stat().st_ino != registry.stat().st_ino
    assert authority.fd >= 0
    capture._RETAINED_LIVE_STATE.remove(authority)
    authority.close()
    shutil.rmtree(state)


def test_new_retention_registry_matching_cleanup_removes_file_and_syncs_parent(
    monkeypatch, tmp_path: Path,
) -> None:
    from okf_loom.session_authority import resolve_session_authority

    tmp_path.chmod(0o700)
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    authority = resolve_session_authority(state, approved_root=tmp_path)
    registry_parent = tmp_path / "registry"
    registry = registry_parent / "records.jsonl"
    monkeypatch.setattr(capture, "_LIVE_STATE_REGISTRY", registry)
    real_write = os.write
    writes = 0

    def partial_then_fail(fd, payload):
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(fd, payload[:9])
        raise OSError("injected new-registry append failure")

    real_fsync = os.fsync
    parent_syncs = 0

    def observed_fsync(fd):
        nonlocal parent_syncs
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            parent_syncs += 1
        return real_fsync(fd)

    monkeypatch.setattr(capture.os, "write", partial_then_fail)
    monkeypatch.setattr(capture.os, "fsync", observed_fsync)
    result = capture._record_state_retention(
        authority, [], disposition="cleanup-failed-retained",
        reason="new-registry-matching-cleanup", retain_descriptor=True,
    )

    assert result["stateDisposition"] == "cleanup-failed-retained"
    assert result["durable"] is False
    assert not registry.exists()
    assert parent_syncs == 1
    assert authority.fd >= 0
    capture._RETAINED_LIVE_STATE.remove(authority)
    authority.close()
    shutil.rmtree(state)


def test_retention_registry_rollback_failure_reports_corrupt_and_retains_authority(
    monkeypatch, tmp_path: Path,
) -> None:
    from okf_loom.session_authority import resolve_session_authority

    tmp_path.chmod(0o700)
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    authority = resolve_session_authority(state, approved_root=tmp_path)
    registry_parent = tmp_path / "registry"; registry_parent.mkdir(mode=0o700)
    registry = registry_parent / "records.jsonl"
    registry.write_text(json.dumps({"existing": True}) + "\n")
    registry.chmod(0o600)
    monkeypatch.setattr(capture, "_LIVE_STATE_REGISTRY", registry)
    real_write = os.write
    writes = 0

    def partial_then_fail(fd, payload):
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(fd, payload[:7])
        raise OSError("injected partial append failure")

    monkeypatch.setattr(capture.os, "write", partial_then_fail)
    monkeypatch.setattr(capture.os, "ftruncate",
                        lambda _fd, _offset: (_ for _ in ()).throw(OSError("rollback failed")))
    server = _cleanup_server_with_authority(authority)
    with pytest.raises(capture.CaptureFailure) as caught:
        server.__exit__(None, None, None)

    assert caught.value.diagnostic["stateDisposition"] == "registry-corrupt"
    assert server.retention_record["contentState"] == "potentially-secret-bearing"
    assert server.retention_record["durable"] is False
    assert authority.fd >= 0
    torn = registry.read_bytes()
    monkeypatch.setattr(capture.os, "write", real_write)
    monkeypatch.setattr(capture.os, "ftruncate", os.ftruncate)
    retry_errors = []
    retry = capture._record_state_retention(
        authority, retry_errors, disposition="cleanup-failed-retained",
        reason="retry-after-corrupt-registry", retain_descriptor=True,
    )
    assert retry["stateDisposition"] == "registry-corrupt"
    assert retry["durable"] is False
    assert registry.read_bytes() == torn
    assert retry_errors == ["state-registry:OSError"]
    capture._RETAINED_LIVE_STATE.remove(authority)
    capture._RETAINED_LIVE_STATE.remove(authority)
    authority.close()
    shutil.rmtree(state)


@pytest.mark.parametrize("failure", ["zero-write", "file-fsync", "parent-fsync", "metadata"])
def test_retention_registry_failures_are_typed_conservative_and_retain_authority(
    monkeypatch, tmp_path: Path, failure: str,
) -> None:
    from okf_loom.session_authority import resolve_session_authority

    tmp_path.chmod(0o700)
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    authority = resolve_session_authority(state, approved_root=tmp_path)
    registry_parent = tmp_path / "registry"
    monkeypatch.setattr(capture, "_LIVE_STATE_REGISTRY", registry_parent / "records.jsonl")
    if failure == "zero-write":
        monkeypatch.setattr(capture.os, "write", lambda _fd, _payload: 0)
    elif failure in {"file-fsync", "parent-fsync"}:
        real_fsync = os.fsync
        def fail_selected(fd):
            is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            if is_directory == (failure == "parent-fsync"):
                raise OSError("injected sync failure")
            return real_fsync(fd)
        monkeypatch.setattr(capture.os, "fsync", fail_selected)
    else:
        monkeypatch.setattr(authority, "retention_record",
                            lambda: (_ for _ in ()).throw(OSError("secret-path-value")))

    server = _cleanup_server_with_authority(authority)
    with pytest.raises(capture.CaptureFailure) as caught:
        server.__exit__(None, None, None)

    diagnostic = caught.value.diagnostic
    assert diagnostic["code"] == "server-cleanup-failure"
    assert diagnostic["phase"] == "server-cleanup"
    assert diagnostic["processExitConfirmed"] is True
    expected_disposition = (
        "registry-corrupt" if failure in {"file-fsync", "parent-fsync"}
        else "cleanup-failed-retained"
    )
    assert diagnostic["stateDisposition"] == expected_disposition
    assert set(diagnostic["stateIdentity"]) == {"device", "inode"}
    assert server.process is None and server.state_root is None
    assert authority.fd >= 0 and server._state_authority is authority
    assert server.retention_record["contentState"] == "potentially-secret-bearing"
    assert server.retention_record["durable"] is False
    assert "secret-path-value" not in str(caught.value)
    capture._RETAINED_LIVE_STATE.remove(authority)
    authority.close()
    shutil.rmtree(state)


@pytest.mark.parametrize("unsafe", ["symlink", "mode", "owner"])
def test_retention_registry_rejects_unsafe_existing_file(
    monkeypatch, tmp_path: Path, unsafe: str,
) -> None:
    from okf_loom.session_authority import resolve_session_authority

    tmp_path.chmod(0o700)
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    authority = resolve_session_authority(state, approved_root=tmp_path)
    registry_parent = tmp_path / "registry"; registry_parent.mkdir(mode=0o700)
    registry = registry_parent / "records.jsonl"
    if unsafe == "symlink":
        target = tmp_path / "target"; target.write_text("untouched")
        registry.symlink_to(target)
    else:
        registry.write_text("")
        registry.chmod(0o644 if unsafe == "mode" else 0o600)
    monkeypatch.setattr(capture, "_LIVE_STATE_REGISTRY", registry)
    if unsafe == "owner":
        real_fstat = capture.os.fstat
        registry_identity = registry.stat()
        def wrong_owner(fd):
            value = real_fstat(fd)
            if (value.st_dev, value.st_ino) == (registry_identity.st_dev, registry_identity.st_ino):
                fields = list(value); fields[4] = value.st_uid + 1
                return os.stat_result(fields)
            return value
        monkeypatch.setattr(capture.os, "fstat", wrong_owner)

    server = _cleanup_server_with_authority(authority)
    with pytest.raises(capture.CaptureFailure) as caught:
        server.__exit__(None, None, None)
    assert caught.value.diagnostic["stateDisposition"] == "cleanup-failed-retained"
    assert authority.fd >= 0
    if unsafe == "symlink":
        assert target.read_text() == "untouched"
    capture._RETAINED_LIVE_STATE.remove(authority)
    authority.close()
    shutil.rmtree(state)


def test_live_state_authority_uses_finalizer_free_mkdtemp(monkeypatch, tmp_path: Path) -> None:
    from okf_loom import session_authority
    calls = []
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        calls.append((args, kwargs))
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(session_authority.tempfile, "mkdtemp", mkdtemp)
    authority = session_authority.temporary_session_authority(
        prefix="okf-live-proof-", parent=Path("/tmp"))
    root = authority.display_path
    try:
        assert calls and calls[0][1]["prefix"] == "okf-live-proof-"
        assert not hasattr(authority, "_finalizer")
        assert root.is_dir() and stat.S_IMODE(root.stat().st_mode) == 0o700
    finally:
        authority.close()
        shutil.rmtree(root)


@pytest.mark.parametrize("component", ["first", "second", "final"])
def test_session_root_rejects_permissive_mode_at_every_existing_component(
    monkeypatch, tmp_path: Path, component: str,
) -> None:
    from okf_loom.studio import Studio
    bundle = tmp_path / "bundle"; bundle.mkdir()
    tmp_path.chmod(0o700)
    monkeypatch.setenv("OKF_LOOM_SESSION_TRUSTED_ROOT", str(tmp_path))
    first = tmp_path / "first"; first.mkdir(mode=0o700)
    second = first / "second"; second.mkdir(mode=0o700)
    final = second / "final"; final.mkdir(mode=0o700)
    {"first": first, "second": second, "final": final}[component].chmod(0o720)
    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(final))
    with pytest.raises(ValueError, match="private"):
        Studio.for_configured_bundle(bundle)


@pytest.mark.parametrize("component", ["first", "second", "final"])
def test_session_root_rejects_wrong_uid_at_every_existing_component(
    monkeypatch, tmp_path: Path, component: str,
) -> None:
    from okf_loom import session_authority
    from okf_loom.studio import Studio
    bundle = tmp_path / "bundle"; bundle.mkdir()
    tmp_path.chmod(0o700)
    first = tmp_path / "first"; first.mkdir(mode=0o700)
    second = first / "second"; second.mkdir(mode=0o700)
    final = second / "final"; final.mkdir(mode=0o700)
    selected = {"first": first, "second": second, "final": final}[component].stat()
    real_fstat = session_authority.os.fstat

    def wrong_uid(fd):
        value = real_fstat(fd)
        if (value.st_dev, value.st_ino) == (selected.st_dev, selected.st_ino):
            fields = list(value); fields[4] = value.st_uid + 1
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(session_authority.os, "fstat", wrong_uid)
    monkeypatch.setenv("OKF_LOOM_SESSION_TRUSTED_ROOT", str(tmp_path))
    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(final))
    with pytest.raises(ValueError, match="private"):
        Studio.for_configured_bundle(bundle)


def test_parallel_live_servers_receive_isolated_state_roots(monkeypatch, tmp_path: Path) -> None:
    class Process:
        returncode = None
        def poll(self): return self.returncode
        def terminate(self): self.returncode = 0
        def wait(self, timeout=None): return self.returncode

    roots = []
    def popen(*_a, **kwargs):
        roots.append(Path(kwargs["env"]["OKF_LOOM_SESSION_ROOT"]))
        return Process()
    monkeypatch.setattr(capture.subprocess, "Popen", popen)
    monkeypatch.setattr(capture, "_wait", lambda *_a, **_k: None)
    runtime = capture.ImmutableRuntimeWorkspace.for_test(tmp_path, tmp_path)
    first = capture._LiveServer(tmp_path, mode="editable", runtime=runtime)
    second = capture._LiveServer(tmp_path, mode="read-only", runtime=runtime)
    with first, second:
        assert roots[0] != roots[1]
        assert all(path.is_dir() for path in roots)
    for path in roots:
        _assert_sanitized_tree(path)
    assert first.cleanup_result.disposition == second.cleanup_result.disposition == "sanitized-retained-tree"
    for path in roots:
        shutil.rmtree(path)


def test_support_json_is_schema_valid_revision_bound_and_hash_inventory_is_typed(tmp_path: Path) -> None:
    revisions = {"demo": "d1", "dense-docs": "d2", "empty-okf": "e1"}
    (tmp_path / "keyboard-focus-trace.json").write_text(json.dumps({
        "schema": 2, "automated": True, "source_revisions": revisions,
        "scenarios": [{"scenario": "f06-graph-keyboard", "artifact": "f06-graph-keyboard-journey-1440.png",
                       "events": [{"sequence": 1, "key": "ArrowDown", "pre": {"role": "listbox", "name": "Concepts"},
                                   "post": {"role": "listbox", "name": "Concepts"}, "ownership": "component",
                                   "active_descendant": "node-a", "aria_state": {"expanded": None},
                                   "native_editable": False, "default_prevented": True, "restoration": True,
                                   "observed": {"targetRole": "listbox", "editable": False, "defaultPrevented": True, "trusted": True}}]}]}))
    (tmp_path / "graph-at-journey.json").write_text(json.dumps({
        "schema": 2, "automated": True, "source_revision": "d2", "artifact": "f06-graph-keyboard-journey-1440.png",
        "scenario": "f06-graph-keyboard", "journey": {"selectedId": "b", "listTarget": "b", "pathKind": "found",
        "pathStart": "a", "pathEnd": "b", "pathStepCount": 2, "events": [1]}}))
    (tmp_path / "zoom-evidence.json").write_text(json.dumps({
        "schema": 2, "automated": True, "source_revisions": revisions,
        "required_mechanism": "chrome-page-zoom-keyboard", "scenarios": [
            {"scenario": f"zoom-{zoom}", "artifact": f"zoom-{zoom}.png", "zoom_percent": zoom,
             "evidence": {"mechanism": "chrome-page-zoom-keyboard", "zoomPercent": zoom,
                          "innerWidth": 1440*100/zoom,
                          "cssViewportWidth": 1440*100/zoom - (8 if zoom == 200 else 4),
                          "expectedCssViewportWidth": 1440*100/zoom,
                          "devicePixelRatio": zoom/100, "devicePixelRatioBefore": 1, "baselinePercent": 100,
                          "baselineVerified": True, "sessionBinding": "bound-page", "sessionBindingVerified": True},
             "journey": {"restored": True, "outline_opened": True, "escape_closed": True,
                         "section_activated": "#schema", "no_horizontal_overflow": True}}
            for zoom in (200, 400)
        ]}))
    capture._write_manual_screen_reader_template(tmp_path / "manual-screen-reader-session.md")
    inventory = capture._validate_supporting_artifacts(tmp_path, expected_revisions=revisions)
    assert {row["path"] for row in inventory} == set(capture._SUPPORTING_ARTIFACTS)
    assert all(row["bytes"] > 0 and len(row["sha256"]) == 64 for row in inventory)

    graph = json.loads((tmp_path / "graph-at-journey.json").read_text()); graph["journey"] = None
    (tmp_path / "graph-at-journey.json").write_text(json.dumps(graph))
    with pytest.raises(capture.CapturePreconditionError, match="graph journey"):
        capture._validate_supporting_artifacts(tmp_path, expected_revisions=revisions)


def test_zoom_without_explicit_headed_integration_gate_is_typed_unsupported(monkeypatch) -> None:
    monkeypatch.delenv("OKF_CAPTURE_REAL_BROWSER_ZOOM", raising=False)
    with pytest.raises(capture.CaptureFailure) as caught:
        capture._apply_browser_zoom(object(), 200)
    assert caught.value.diagnostic["code"] == "zoom-unsupported"


def test_zoom_headed_gate_without_browser_chrome_driver_is_typed_unsupported(monkeypatch) -> None:
    class Page:
        def evaluate(self, _script):
            return {"dpr": 1, "width": 1440}
    monkeypatch.setenv("OKF_CAPTURE_REAL_BROWSER_ZOOM", "1")
    monkeypatch.delenv("OKF_CAPTURE_BROWSER_ZOOM_DRIVER", raising=False)
    with pytest.raises(capture.CaptureFailure) as caught:
        capture._apply_browser_zoom(Page(), 400)
    assert caught.value.diagnostic["code"] == "zoom-unsupported"
    assert caught.value.diagnostic["phase"] == "browser-owned-zoom"


def test_zoom_driver_subprocess_is_bound_to_immutable_workspace_after_authority_aliases_rebound(
    monkeypatch, tmp_path: Path,
) -> None:
    immutable = tmp_path / "immutable"
    (immutable / "tools").mkdir(parents=True)
    driver = immutable / "tools/zoom-driver"
    driver.write_text("#!/bin/sh\n")
    driver.chmod(0o700)
    runtime = capture.ImmutableRuntimeWorkspace.for_test(immutable, ROOT)
    calls = []

    class Page:
        viewport_size = {"width": 1440, "height": 900}

        def evaluate(self, script, *_args):
            if "document.title=binding" in script:
                return None
            if "devicePixelRatio" in script and "bound:" in script:
                return {"dpr": 1, "width": 1440, "bound": True}
            raise RuntimeError("stop after actual driver invocation")

        def wait_for_function(self, *_args, **_kwargs):
            return None

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("OKF_CAPTURE_REAL_BROWSER_ZOOM", "1")
    monkeypatch.setenv("OKF_CAPTURE_BROWSER_ZOOM_DRIVER", "tools/zoom-driver")
    monkeypatch.setattr(capture.subprocess, "run", run)
    for name in (
        "ROOT", "DEFAULT_BUNDLE", "DEFAULT_DENSE_BUNDLE", "DEFAULT_EMPTY_BUNDLE",
        "_AUTHORITY", "_repo_root", "REPOSITORY_ROOT", "_sealed_repository_root",
        "_sealed_checkout_identity", "_seal_repository_authority",
    ):
        monkeypatch.setattr(capture, name, lambda: tmp_path, raising=False)
    with pytest.raises(RuntimeError, match="stop after actual driver invocation"):
        capture._apply_browser_zoom(Page(), 200, runtime=runtime)

    assert calls and calls[0][0][0] == str(driver)
    assert calls[0][1]["cwd"] == immutable
    assert calls[0][1]["env"] == runtime.subprocess_env()
    assert runtime.accesses[-1]["operation"] == "browser-zoom"


def test_server_readiness_failures_are_typed_public_results(monkeypatch) -> None:
    class Exited:
        returncode = 7
        def poll(self): return 7
    with pytest.raises(capture.CaptureFailure) as exited:
        capture._wait("http://127.0.0.1:1", Exited(), timeout=0.01)
    assert exited.value.diagnostic["code"] == "server-start-exit"

    class Running:
        returncode = None
        def poll(self): return None
    monkeypatch.setattr(capture.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(capture.time, "sleep", lambda _delay: None)
    with pytest.raises(capture.CaptureFailure) as timed_out:
        capture._wait("http://127.0.0.1:1", Running(), timeout=0)
    assert timed_out.value.diagnostic["code"] == "server-start-timeout"


def _candidate_paths(root: Path) -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True,
    ).stdout.split(b"\0")
    paths = {Path(raw.decode()) for raw in tracked if raw and raw.decode() != ".gitignore"}
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, check=True, capture_output=True,
    ).stdout.split(b"\0")
    paths.update(Path(raw.decode()) for raw in untracked if raw and not raw.decode().startswith(".aic/"))
    return sorted(path for path in paths if (root / path).is_file())


def _candidate_digest(root: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        digest.update(relative.as_posix().encode() + b"\0")
        digest.update(hashlib.sha256((root / relative).read_bytes()).digest())
    return digest.hexdigest()


def test_exact_clean_candidate_runner_uses_own_root_and_public_validation(tmp_path: Path) -> None:
    primary_ignore = (ROOT / ".gitignore").read_bytes() if (ROOT / ".gitignore").exists() else None
    primary_status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=ROOT,
        check=True, text=True, capture_output=True,
    ).stdout
    paths = _candidate_paths(ROOT)
    reviewed_digest = _candidate_digest(ROOT, paths)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for relative in paths:
        target = candidate / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    subprocess.run(["git", "init", "--quiet"], cwd=candidate, check=True)
    subprocess.run(["git", "config", "user.name", "Capture Candidate Test"], cwd=candidate, check=True)
    subprocess.run(["git", "config", "user.email", "capture-candidate@example.invalid"], cwd=candidate, check=True)
    subprocess.run(["git", "add", "-A"], cwd=candidate, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "test candidate"], cwd=candidate, check=True)
    assert "samples/empty_bundle/index.md" in subprocess.run(
        ["git", "ls-files"], cwd=candidate, check=True, text=True, capture_output=True,
    ).stdout.splitlines()
    assert "scripts/okf_loom/session_authority.py" in subprocess.run(
        ["git", "ls-files"], cwd=candidate, check=True, text=True, capture_output=True,
    ).stdout.splitlines()
    assert subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=candidate,
                          check=True, text=True, capture_output=True).stdout == ""
    assert not (candidate / ".gitignore").exists() and not (candidate / ".aic").exists()
    assert _candidate_digest(candidate, paths) == reviewed_digest

    head_archive = tmp_path / "head"
    head_archive.mkdir()
    archive = subprocess.Popen(["git", "archive", "HEAD"], cwd=ROOT, stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(head_archive)], stdin=archive.stdout, check=True)
    assert archive.wait() == 0
    head_paths = [path for path in paths if (head_archive / path).is_file()]
    candidate_changed = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=ROOT,
        check=True, text=True, capture_output=True,
    ).stdout
    if any(line and not line[3:].startswith((".gitignore", ".aic/")) for line in candidate_changed.splitlines()):
        assert _candidate_digest(head_archive, head_paths) != reviewed_digest

    runner = candidate / "scripts/capture_ui_overhaul.py"
    result = subprocess.run(
        [sys.executable, str(runner), "--manifest-only"], cwd=candidate,
        text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["name"] == "okf-loom-v2-ui-overhaul-final"
    assert runner.resolve().parents[1] == candidate.resolve()
    for bundle in ("docs-bundle", "samples/empty_bundle"):
        validated = subprocess.run(
            [str(candidate / "scripts/okf-loom"), "validate", bundle, "--strict"],
            cwd=candidate, text=True, capture_output=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert validated.returncode == 0, validated.stdout + validated.stderr
    boundary = subprocess.run(
        [sys.executable, str(runner), "--source-root", str(ROOT), "--manifest-only"],
        cwd=candidate, text=True, capture_output=True,
    )
    assert boundary.returncode == 2 and json.loads(boundary.stderr)["code"] == "workspace-mismatch"
    assert subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=candidate,
                          check=True, text=True, capture_output=True).stdout == ""
    assert ((ROOT / ".gitignore").read_bytes() if (ROOT / ".gitignore").exists() else None) == primary_ignore
    assert subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=ROOT,
        check=True, text=True, capture_output=True,
    ).stdout == primary_status


def test_default_matrix_covers_storage_and_os_for_every_target() -> None:
    rows = capture.release_scenario_manifest()["scenarios"]
    defaults = [row for row in rows if "bone-default" in row["coverage"]]
    assert len(defaults) >= 16
    assert {row["source"] for row in defaults} == {"live", "static", "spa", "single-file"}
    assert {row["storage"] for row in defaults} == {"none", "denied"}
    assert {row["color_scheme"] for row in defaults} == {"light", "dark"}
    assert all(row["resolved_theme"] == "bone" for row in defaults)
    for row in defaults:
        spec = next(item for item in capture.SCENARIO_SPECS if item.id == row["id"])
        assert capture._theme_seed_script(spec) is None


@pytest.mark.parametrize("storage,os_mode", [("none", "light"), ("denied", "dark")])
def test_default_runtime_assertion_checks_storage_and_palette(storage: str, os_mode: str) -> None:
    spec = capture._default_theme_scenario("live", os_mode, storage)

    class ThemePage:
        def evaluate(self, _script, *_args):
            return {"resolved_theme": "bone", "runtime_resolved_theme": "bone",
                    "resolved_mode": "light", "family": "bone", "mode": "light",
                    "ready": True, "resolution": "resolved", "contrast": None, "border": None,
                    "storageAccess": "denied" if storage == "denied" else "available",
                    "storageKeys": [] if storage == "none" else None}

    assert capture._assert_theme_state(ThemePage(), spec)["resolved_theme"] == "bone"


def test_failure_matrix_routes_to_typed_fallback_and_success_never_downgrades(
    tmp_path: Path, monkeypatch,
) -> None:
    failure_spec = next(item for item in capture.SCENARIO_SPECS if item.id == "boot-failure")
    monkeypatch.setattr(
        capture, "_assert_theme_state",
        lambda _page, _spec: {
            "resolved_theme": "bone", "resolution": "resolved",
            "family": "bone", "mode": "auto", "contrast": "normal", "border": "on",
            "ready": True,
        },
    )
    monkeypatch.setattr(
        capture, "_browser_metadata",
        lambda _page, _spec: {"engine": "chromium", "javascript": True},
    )
    row = capture._capture_spec(
        _FallbackPage(), "http://viewer", failure_spec, tmp_path,
        provenance={"head": "h", "tree": "t", "clean": True, "worktree_digest": "w"},
        expected_revision="bundle-1",
        input_inventory=[{"path": "source.py", "type": "regular", "tracked": True}],
        input_inventory_digest="i", capture_fn=lambda *_args, **_kwargs: pytest.fail("success path used"),
    )
    assert row["readiness"] == "fallback-dom"
    assert row["runtime"]["failureCode"] == "boot-failure"
    assert row["runtime"]["honestFallback"] is True


@pytest.mark.parametrize("relative", ["", "captures", "captures/release"])
def test_release_destination_precondition_rejects_repo_or_historical_screenshot_paths(relative: str) -> None:
    destination = ROOT / relative if relative else ROOT
    with pytest.raises(capture.CapturePreconditionError, match="protected release output"):
        capture._validate_release_destination(destination)


def test_release_bundle_precondition_rejects_historical_screenshot_inputs(tmp_path: Path) -> None:
    # captures/ is git-ignored local proof output, so build it in a tmp checkout.
    root = tmp_path.resolve()
    (root / "captures" / "release").mkdir(parents=True)
    for historical in (root / "captures", root / "captures" / "release"):
        with pytest.raises(capture.CapturePreconditionError, match="historical bundle inputs"):
            capture._validate_release_bundle(historical, _root=lambda: root)


def test_artifact_provenance_binds_revision_tree_inventory_viewport_target_theme_and_assertion(tmp_path: Path) -> None:
    image = tmp_path / "theme-bone.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (1).to_bytes(4, "big") + (1).to_bytes(4, "big"))
    spec = next(item for item in capture.SCENARIO_SPECS if item.id == "theme-bone")
    row = capture._artifact_record(
        image,
        spec,
        provenance={"head": "head-1", "tree": "tree-1", "clean": True, "worktree_digest": "work-1"},
        expected_revision="bundle-1",
        input_inventory=[{"path": "scripts/capture_ui_overhaul.py", "type": "regular", "tracked": True}],
        input_inventory_digest="inventory-1",
        runtime={"generation": 4, "metadata": {"revision": "bundle-1", "pageRevision": "bundle-1", "readinessPhase": "ready"}},
        theme_state={"resolved_theme": "bone", "contrast": "normal", "border": "on", "resolution": "resolved"},
        browser={"engine": "chromium", "javascript": True, "zoom_percent": 100},
    )
    assert row["head"] == "head-1" and row["tree"] == "tree-1" and row["clean"] is True
    assert row["input_inventory_digest"] == "inventory-1"
    assert row["viewport"] == {"width": 1440, "height": 1000}
    assert row["target"] == "document" and row["scenario"] == spec.id
    assert row["resolved_theme"] == "bone"
    assert row["theme_resolution"] == "resolved"
    assert row["modifiers"] == {"contrast": "normal", "border": "on"}
    assert row["browser_assertion"] == spec.assertion
    assert row["runtime"]["revision"] == "bundle-1"


def test_release_provenance_fails_closed_on_dirty_tree_before_capture(monkeypatch) -> None:
    head = "a" * 40
    tree = "b" * 40
    values = {
        ("status", "--porcelain=v1", "--untracked-files=all"): " M scripts/theme.js\n",
        ("diff", "--binary", head): "diff --git a/scripts/theme.js b/scripts/theme.js\n",
    }
    git_values = {
        ("rev-parse", "--show-object-format"): b"sha1\n",
        ("rev-parse", "--verify", "HEAD"): (head + "\n").encode(),
        ("cat-file", "-t", head): b"commit\n",
        ("rev-parse", "--verify", f"{head}^{{tree}}"): (tree + "\n").encode(),
        ("rev-parse", "HEAD"): (head + "\n").encode(),
    }
    monkeypatch.setattr(capture, "_git_bytes", lambda args, **_kwargs: git_values[tuple(args)])
    monkeypatch.setattr(capture, "_git_output", lambda args, **_kwargs: values[tuple(args)])
    with pytest.raises(capture.CapturePreconditionError, match="clean worktree"):
        capture._git_provenance(require_clean=True)

    provenance = capture._git_provenance(require_clean=False)
    assert provenance["clean"] is False
    assert provenance["head"] == head and provenance["tree"] == tree
    assert provenance["worktree_digest"] == capture._digest({"status": values[("status", "--porcelain=v1", "--untracked-files=all")], "diff": values[("diff", "--binary", head)]})


def test_release_provenance_drift_is_rejected_before_publication() -> None:
    before = {"head": "head-1", "tree": "tree-1", "clean": True, "worktree_digest": "work-1"}
    after = {"head": "head-2", "tree": "tree-2", "clean": True, "worktree_digest": "work-2"}
    with pytest.raises(capture.CapturePreconditionError, match="git provenance drift"):
        capture._assert_provenance_stable(before, after)


def test_initial_revision_pair_derives_tree_from_captured_commit_after_head_moves(monkeypatch) -> None:
    captured = "a" * 40
    moved = "b" * 40
    captured_tree = "c" * 40
    moved_tree = "d" * 40
    calls: list[tuple[str, ...]] = []

    def git(args, **_kwargs):
        args = tuple(args)
        calls.append(args)
        values = {
            ("rev-parse", "--show-object-format"): b"sha1\n",
            ("rev-parse", "--verify", "HEAD"): (captured + "\n").encode(),
            ("cat-file", "-t", captured): b"commit\n",
            ("rev-parse", "--verify", f"{captured}^{{tree}}"): (captured_tree + "\n").encode(),
            ("status", "--porcelain=v1", "--untracked-files=all"): b"",
            ("diff", "--binary", captured): b"",
            ("rev-parse", "HEAD"): (moved + "\n").encode(),
        }
        return values[args]

    monkeypatch.setattr(capture, "_git_bytes", git)
    provenance = capture._git_provenance(require_clean=True)

    assert provenance["head"] == captured
    assert provenance["tree"] == captured_tree
    assert ("rev-parse", "HEAD^{tree}") not in calls
    assert ("rev-parse", "--verify", f"{captured}^{{tree}}") in calls
    with pytest.raises(capture.CapturePreconditionError, match="moved"):
        capture._assert_bound_revision(captured, captured_tree, provenance)
    assert provenance["clean"] is True and provenance["worktree_status"] == []


@pytest.mark.parametrize("raw", [
    b"", b"a" * 39 + b"\n", b"a" * 41 + b"\n", b"A" * 40 + b"\n",
    b"g" * 40 + b"\n", b" " + b"a" * 40 + b"\n", b"a" * 40 + b" \n",
    b"a" * 40 + b"\r\n", b"a" * 40, b"a" * 40 + b"\n\n",
    b"a" * 40 + b"\n" + b"b" * 40 + b"\n", b"a" * 40 + b"\t\n",
])
def test_strict_git_oid_rejects_every_noncanonical_sha1_record(raw: bytes) -> None:
    with pytest.raises(capture.CapturePreconditionError):
        capture._strict_git_oid(raw, object_format="sha1", label="test OID")


def test_strict_git_oid_accepts_only_canonical_framed_full_oid_and_supported_format() -> None:
    assert capture._strict_git_oid(b"a" * 40 + b"\n", object_format="sha1", label="oid") == "a" * 40
    assert capture._strict_git_oid(b"b" * 64 + b"\n", object_format="sha256", label="oid") == "b" * 64
    with pytest.raises(capture.CapturePreconditionError, match="unsupported"):
        capture._strict_git_oid(b"a" * 40 + b"\n", object_format="md5", label="oid")


def test_provenance_rejects_non_commit_head(monkeypatch) -> None:
    head = "a" * 40
    values = {
        ("rev-parse", "--show-object-format"): b"sha1\n",
        ("rev-parse", "--verify", "HEAD"): (head + "\n").encode(),
        ("cat-file", "-t", head): b"tree\n",
    }
    monkeypatch.setattr(capture, "_git_bytes", lambda args, **_kwargs: values[tuple(args)])
    with pytest.raises(capture.CapturePreconditionError, match="not a commit"):
        capture._git_provenance(require_clean=True)


def test_capture_source_has_no_symbolic_head_tree_expression() -> None:
    source = (ROOT / "scripts/capture_ui_overhaul.py").read_text()
    assert "HEAD^{tree}" not in source


def test_inventory_is_conservative_tracked_code_plus_complete_bundle_inputs(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "concept.md").write_text("---\ntype: Note\n---\nbody\n")
    rows = capture._inventory(bundle)
    by_path = {row["path"]: row for row in rows}
    assert by_path["scripts/capture_ui_overhaul.py"]["tracked"] is True
    concept = bundle / "concept.md"
    bundle_label = str(concept.relative_to(ROOT)) if concept.is_relative_to(ROOT) else str(concept)
    assert bundle_label in by_path
    assert by_path[bundle_label]["tracked"] is False
    assert all({"path", "type", "tracked", "git_mode", "bytes", "sha256", "symlink_target"} <= set(row) for row in rows)
    assert capture._digest(rows) == capture._digest(list(rows))


def test_inventory_ignores_bundle_local_okf_runtime_lock_creation_and_removal(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "concept.md").write_text("---\ntype: Note\n---\nbody\n")
    (bundle / ".concept.md.lock").write_text("authored lock input\n")
    (bundle / ".hidden-input").write_text("authored hidden input\n")
    monkeypatch.setattr(capture, "_tracked_paths", lambda: [])

    before = capture._inventory(bundle)
    before_paths = {row["path"] for row in before}
    assert any(path.endswith("/bundle/.concept.md.lock") for path in before_paths)
    assert any(path.endswith("/bundle/.hidden-input") for path in before_paths)
    runtime_lock = bundle / ".concept.md.okf-lock"
    runtime_lock.write_text("runtime coordination\n")
    during = capture._inventory(bundle)
    runtime_lock.unlink()

    assert during == before
    assert capture._inventory(bundle) == before


def test_authored_bundle_file_drift_remains_release_fatal(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    concept = bundle / "concept.md"
    concept.write_text("---\ntype: Note\n---\nbefore\n")
    monkeypatch.setattr(capture, "ROOT", tmp_path)
    monkeypatch.setattr(capture, "_tracked_paths", lambda: [])

    before = capture._inventory(bundle)
    concept.write_text("---\ntype: Note\n---\nafter\n")
    after = capture._inventory(bundle)

    assert after != before
    with pytest.raises(capture.CapturePreconditionError, match="input inventory drifted"):
        capture._assert_input_inventory_stable(before, after)


def test_final_capture_validation_rejects_missing_or_mismatched_expected_revision(tmp_path: Path) -> None:
    inventory = [{"path": "source.py", "type": "regular", "tracked": True}]
    digest = capture._digest(inventory)
    spec = next(item for item in capture.SCENARIO_SPECS if item.id == "theme-bone")
    image = tmp_path / spec.artifact
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (1).to_bytes(4, "big") + (1).to_bytes(4, "big"))
    row = capture._artifact_record(
        image, spec,
        provenance={"head": "h", "tree": "t", "clean": True, "worktree_digest": "w"},
        expected_revision="bundle-1", input_inventory=inventory,
        input_inventory_digest=digest,
            runtime={"generation": 1, "metadata": {"revision": "bundle-1", "pageRevision": "bundle-1"}},
        theme_state={"resolved_theme": "bone", "resolution": "resolved", "contrast": "normal", "border": "on"},
        browser={"engine": "chromium", "javascript": True},
    )
    row["file"] = spec.artifact
    rows = []
    for scenario in capture.SCENARIO_SPECS:
        clone = dict(row)
        clone["file"] = scenario.artifact
        clone["scenario"] = scenario.id
        clone["coverage"] = list(scenario.coverage)
        clone["browser_assertion"] = scenario.assertion
        family, mode = capture._public_theme(scenario)
        clone["theme_family"] = family or "unresolved"
        clone["theme_mode"] = mode or "unresolved"
        clone["theme_preference"] = family or "unresolved"
        clone["runtime"] = dict(row["runtime"])
        if scenario.action in {"boot-blocked", "boot-failure"}:
            clone["readiness"] = "fallback-dom"
            clone["runtime"].update({
                "captureKind": "intentional-fallback",
                "failureCode": capture._FALLBACK_SCENARIO_CODES[scenario.action],
                "honestFallback": True,
            })
        rows.append(clone)
        (tmp_path / scenario.artifact).write_bytes(image.read_bytes())
    with pytest.raises(capture.CapturePreconditionError, match="expected source revision"):
        capture._validate_capture_set(
            tmp_path, rows,
            input_inventory=inventory, input_inventory_digest=digest,
        )
    with pytest.raises(capture.CapturePreconditionError, match="expected source revision"):
        capture._validate_capture_set(
            tmp_path, rows, expected_revision="bundle-2",
            input_inventory=inventory, input_inventory_digest=digest,
        )


@pytest.mark.parametrize("mutation", [
    "swapped-files", "wrong-fixture", "wrong-action", "duplicate-binding",
    "missing-binding", "cross-scenario-contract", "wrong-revision",
    "missing-observer-provenance",
])
def test_capture_admission_binds_each_row_exactly_to_its_scenario(
    tmp_path: Path, monkeypatch, mutation: str,
) -> None:
    revisions = {"demo": "demo-r", "dense-docs": "dense-r", "empty-okf": "empty-okf-r"}
    inventory = [{"path": "source.py", "type": "regular", "tracked": True}]
    digest = capture._digest(inventory)
    rows = [_production_shaped_row(tmp_path, spec, revisions, inventory, digest)
            for spec in capture.SCENARIO_SPECS]
    monkeypatch.setattr(capture, "_validate_supporting_artifacts", lambda *_a, **_k: [])

    if mutation == "swapped-files":
        rows[0]["file"], rows[1]["file"] = rows[1]["file"], rows[0]["file"]
    elif mutation == "wrong-fixture":
        rows[0]["bundle_fixture"] = "dense-docs"
    elif mutation == "wrong-action":
        rows[0]["action"] = "parity"
    elif mutation == "duplicate-binding":
        rows[1]["scenario"] = rows[0]["scenario"]
    elif mutation == "missing-binding":
        rows.pop()
    elif mutation == "cross-scenario-contract":
        rows[0]["browser_assertion"] = rows[1]["browser_assertion"]
    elif mutation == "wrong-revision":
        rows[0]["expected_revision"] = "other"
    elif mutation == "missing-observer-provenance":
        rows[0]["browser"].pop("observer_provenance")

    with pytest.raises(capture.CapturePreconditionError):
        capture._validate_capture_set(
            tmp_path, rows, expected_revision=revisions,
            input_inventory=inventory, input_inventory_digest=digest,
        )


class _NoJsLocator:
    def __init__(self, page) -> None:
        self.page = page

    def wait_for(self, **_kwargs) -> None:
        self.page.locator_waited = True

    def inner_text(self) -> str:
        return "JavaScript is off; authored content remains readable."


class _NoJsPage:
    def __init__(self) -> None:
        self.locator_waited = False
        self.goto_calls = []

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        return type("Response", (), {"status": 200, "headers": {"x-okf-bundle-revision": "bundle-1"}})()

    def locator(self, _selector: str):
        return _NoJsLocator(self)

    def screenshot(self, *, path: str, **_kwargs) -> None:
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (1).to_bytes(4, "big") + (1).to_bytes(4, "big"))


def test_no_js_capture_uses_dom_readiness_and_records_noninteractive_runtime(tmp_path: Path) -> None:
    page = _NoJsPage()
    result = capture._capture_nojs_target(
        page, "http://viewer/tables/orders", "document", tmp_path / "nojs.png",
        expected_revision="bundle-1",
    )
    assert result["metadata"]["javascript"] is False
    assert result["metadata"]["readinessPhase"] == "no-js-dom"
    assert page.locator_waited and page.goto_calls[0][1]["wait_until"] == "domcontentloaded"
    assert (tmp_path / "nojs.png").exists()


def test_manifest_rows_are_executable_and_do_not_use_sleep_or_network_idle_readiness() -> None:
    source = (ROOT / "scripts/capture_ui_overhaul.py").read_text()
    manifest = capture.release_scenario_manifest()
    for row in manifest["scenarios"]:
        assert row["id"] and row["artifact"] and row["route"] and row["target"]
        assert row["assertion"] and row["coverage"] and row["action"]
        assert row["viewport"]["width"] > 0 and row["viewport"]["height"] > 0
    assert "wait_for_timeout" not in source
    assert "networkidle" not in source


def test_revision_derivation_uses_a_deterministic_valid_context_query(
    tmp_path: Path, monkeypatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    runtime_root = tmp_path / "runtime"
    (runtime_root / "scripts").mkdir(parents=True)
    (runtime_root / "scripts" / "okf-loom").write_text("#!/bin/sh\n", encoding="utf-8")
    calls: list[list[str]] = []

    class Runtime:
        root = runtime_root
        def audit(self, *_args, **_kwargs):
            return None
        def subprocess_env(self):
            return {"PATH": os.environ.get("PATH", "")}

    def run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"bundle_revision": "r" * 64}), "")

    monkeypatch.setattr(capture.subprocess, "run", run)
    assert capture._derive_expected_revision(bundle, runtime=Runtime()) == "r" * 64
    assert calls == [[
        str(runtime_root / "scripts/okf-loom"), "context", str(bundle),
        "bundle revision", "--mode", "lexical", "--format", "json", "--limit", "1",
    ]]


def test_closeout_inventory_covers_graph_thumbnail_name_at_desktop_and_mobile() -> None:
    manifest = capture.release_scenario_manifest()
    rows = manifest["scenarios"]
    assert any("index-graph-thumbnail-desktop" in row["coverage"] for row in rows)
    assert any(
        "index-graph-thumbnail-mobile" in row["coverage"]
        and row["viewport"]["width"] == 390
        for row in rows
    )
    source = (ROOT / "scripts/capture_ui_overhaul.py").read_text(encoding="utf-8")
    assert "document.querySelector('.okf-graph-thumb__link')" in source
    assert "const a=q('.okf-graph-thumb__link')" in source
    assert "text(a.querySelector('.sr-only'))" in source
    assert "text(a.querySelector('figcaption'))" in source


@pytest.mark.browser
def test_graph_focus_preparation_is_action_only_and_reaches_capture_readiness(tmp_path: Path) -> None:
    from browser_support import playwright_api, launch_chromium, copy_bundle
    playwright = playwright_api()
    from capture_viewer_proof import capture_target

    bundle = copy_bundle(ROOT / "samples/demo_bundle", tmp_path / "bundle")
    spec = next(item for item in capture.SCENARIO_SPECS if item.id == "graph-focus")
    expected_revision = capture._derive_expected_revision(bundle)
    port = capture._port()
    base = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            str(ROOT / "scripts" / "okf-loom"), "serve", str(bundle),
            "--host", "127.0.0.1", "--port", str(port), "--no-watch", "--no-open",
        ],
        cwd=ROOT,
            env=capture.ImmutableRuntimeWorkspace.for_test(ROOT, ROOT).subprocess_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    class RecordingPage:
        def __init__(self, page) -> None:
            self._page = page
            self.graph_tap_results = []

        def __getattr__(self, name):
            return getattr(self._page, name)

        def evaluate(self, script, *args):
            result = self._page.evaluate(script, *args)
            if "cy.nodes().first().trigger('tap')" in script:
                self.graph_tap_results.append(result)
            return result

    try:
        capture._wait(base, process)
        with playwright.sync_playwright() as owner:
            browser = launch_chromium(owner)
            try:
                page = RecordingPage(browser.new_page(viewport={"width": 1440, "height": 1000}))
                runtime = capture_target(
                    page,
                    base + spec.route,
                    spec.target,
                    tmp_path / spec.artifact,
                    expected_revision=expected_revision,
                    full_page=spec.full_page,
                    prepare=lambda current: capture._prepare_scenario(current, spec),
                )
                focus = page.evaluate("""() => {
                  const graph=window.__okfLoomGraph;
                  const state=graph.interaction.snapshot();
                  return {lens:state.lens, root:graph.focusRoot(), ready:window.okfGraphCaptureState()};
                }""")
            finally:
                browser.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    assert page.graph_tap_results == [None]
    assert focus["lens"] == "focus" or focus["root"]
    assert focus["ready"]["layoutSettled"] is True
    assert focus["ready"]["transitionPending"] is False
    assert runtime["screenshotAllowed"] is True
    assert runtime["metadata"]["readinessPhase"] == "graph-ready"


def test_capture_spec_preserves_production_typed_diagnostics(tmp_path: Path) -> None:
    spec = next(item for item in capture.SCENARIO_SPECS if item.id == "theme-bone")

    class ProductionFailure(RuntimeError):
        diagnostic = {"code": "hidden", "phase": "readiness", "target": "document"}

    def fail(*_args, **_kwargs):
        raise ProductionFailure("hidden")

    with pytest.raises(capture.CaptureFailure) as caught:
        capture._capture_spec(
            object(), "http://viewer", spec, tmp_path,
                provenance={"head": "h", "tree": "t", "clean": True, "worktree_digest": "w"},
                expected_revision="bundle-1",
                input_inventory=[{"path": "source.py", "type": "regular", "tracked": True}],
                input_inventory_digest="i", capture_fn=fail,
        )
    assert caught.value.diagnostic == ProductionFailure.diagnostic


class _FallbackLocator:
    def __init__(self, page: "_FallbackPage", selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def wait_for(self, **_kwargs) -> None:
        self.page.waited_selectors.append(self.selector)

    def inner_text(self) -> str:
        return self.page.fallback_text

    def get_attribute(self, name: str):
        return self.page.attributes.get(name)


class _FallbackPage:
    def __init__(self, *, revision: str = "bundle-1") -> None:
        self.revision = revision
        self.fallback_text = (
            "JavaScript is off or Studio failed to start. "
            "Live editing and annotation controls are unavailable; authored content remains readable."
        )
        self.attributes = {"data-okf-capture-revision": revision}
        self.waited_selectors: list[str] = []
        self.goto_calls = []

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        return type("Response", (), {"status": 200, "headers": {"x-okf-bundle-revision": self.revision}})()

    def locator(self, selector: str):
        return _FallbackLocator(self, selector)

    def evaluate(self, script: str, *_args):
        if "okf-studio-fallback-banner" in script:
            return {
                "success": False,
                "status": "failed",
                "code": "boot-failure",
                "fallbackVisible": True,
            }
        raise AssertionError(f"unexpected fallback evaluation: {script}")

    def screenshot(self, *, path: str, **_kwargs) -> None:
        Path(path).write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
        )


def test_failure_scenario_uses_bounded_honest_fallback_capture_not_success_readiness(
    tmp_path: Path,
) -> None:
    page = _FallbackPage()
    result = capture._capture_fallback_target(
        page,
        "http://viewer/tables/orders",
        "document",
        tmp_path / "boot-failure.png",
        expected_revision="bundle-1",
        failure_code="boot-failure",
    )

    assert result["screenshotAllowed"] is True
    assert result["metadata"]["readinessPhase"] == "fallback-dom"
    assert result["metadata"]["failureCode"] == "boot-failure"
    assert result["metadata"]["honestFallback"] is True
    assert page.waited_selectors == [".okf-studio-fallback-banner"]
    assert (tmp_path / "boot-failure.png").exists()


def test_fallback_supersession_cannot_replace_existing_artifact(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "boot-failure.png"
    destination.write_bytes(b"previous-valid-image")
    monkeypatch.setattr(capture, "_capture_claim_is_current", lambda *_args: False)
    with pytest.raises(capture.CaptureFailure) as caught:
        capture._capture_fallback_target(
            _FallbackPage(), "http://viewer", "document", destination,
            expected_revision="bundle-1", failure_code="boot-failure",
        )
    assert caught.value.diagnostic["code"] == "superseded"
    assert destination.read_bytes() == b"previous-valid-image"
    assert not list(tmp_path.glob("*.capture-tmp.*"))


def test_no_js_capture_failure_preserves_prior_artifact_and_discards_private_temp(tmp_path: Path) -> None:
    class FailingPage(_NoJsPage):
        def locator(self, _selector: str):
            class FailingLocator:
                def wait_for(self, **_kwargs):
                    raise RuntimeError("hidden no-JS target")
            return FailingLocator()

    destination = tmp_path / "nojs.png"
    destination.write_bytes(b"previous-valid-image")
    with pytest.raises(capture.CaptureFailure) as caught:
        capture._capture_nojs_target(
            FailingPage(), "http://viewer", "document", destination,
            expected_revision="bundle-1",
        )
    assert caught.value.diagnostic["code"] == "no-js-not-ready"
    assert destination.read_bytes() == b"previous-valid-image"
    assert not list(tmp_path.glob("*.capture-tmp.*"))


def test_no_js_capture_rejects_source_revision_mismatch_before_publication(tmp_path: Path) -> None:
    destination = tmp_path / "nojs.png"
    destination.write_bytes(b"previous-valid-image")
    with pytest.raises(capture.CaptureFailure) as caught:
        capture._capture_nojs_target(
            _NoJsPage(), "http://viewer", "document", destination,
            expected_revision="expected-revision",
        )
    assert caught.value.diagnostic["code"] == "revision-mismatch"
    assert destination.read_bytes() == b"previous-valid-image"


def test_no_js_capture_rejects_missing_expected_source_revision(tmp_path: Path) -> None:
    with pytest.raises(capture.CaptureFailure) as caught:
        capture._capture_nojs_target(_NoJsPage(), "http://viewer", "document", tmp_path / "nojs.png")
    assert caught.value.diagnostic["code"] == "revision-missing"


def test_js_capture_spec_passes_and_requires_expected_source_revision(tmp_path: Path) -> None:
    spec = next(item for item in capture.SCENARIO_SPECS if item.id == "theme-bone")

    class RuntimePage:
        def evaluate(self, _script, *_args):
            if "okfCaptureRevision" in _script:
                return {"body": "bundle-1", "root": "bundle-1"}
            return {
                "resolved_theme": "bone", "contrast": "normal", "border": "on",
                "runtime_resolved_theme": "bone", "resolved_mode": "light",
                "family": "bone", "mode": "light", "ready": True,
                "resolution": "resolved", "storageAccess": "available", "storageKeys": [],
            }

    seen = {}

    def capture_fn(_page, _url, _target, path, **kwargs):
        seen.update(kwargs)
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
        )
        return {
            "generation": 7, "screenshotAllowed": True,
            "metadata": {"revision": "bundle-1", "readinessPhase": "ready"},
        }

    row = capture._capture_spec(
        RuntimePage(), "http://viewer", spec, tmp_path,
        provenance={"head": "h", "tree": "t", "clean": True, "worktree_digest": "w"},
        expected_revision="bundle-1",
        input_inventory=[{"path": "source.py", "type": "regular", "tracked": True}],
        input_inventory_digest="i", capture_fn=capture_fn,
    )
    assert seen["expected_revision"] == "bundle-1"
    assert row["runtime"]["revision"] == "bundle-1"
    assert row["theme_state"]["mode"] == "light"
    assert row["theme_state"]["runtime_resolved_theme"] == "bone"


def test_js_capture_does_not_accept_recorded_revision_without_matching_runtime_page(tmp_path: Path) -> None:
    spec = next(item for item in capture.SCENARIO_SPECS if item.id == "theme-bone")

    class WrongRuntimePage:
        def evaluate(self, script, *_args):
            if "okfCaptureRevision" in script:
                return {"body": "bundle-other", "root": "bundle-other"}
            return {
                "resolved_theme": "bone", "runtime_resolved_theme": "bone",
                "resolved_mode": "light", "contrast": "normal", "border": "on",
                "family": "bone", "mode": "light", "ready": True,
                "resolution": "resolved", "storageAccess": "available", "storageKeys": [],
            }

    def capture_fn(_page, _url, _target, path, **_kwargs):
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
        )
        return {"generation": 1, "screenshotAllowed": True, "metadata": {"revision": "bundle-1"}}

    with pytest.raises(capture.CaptureFailure) as caught:
        capture._capture_spec(
            WrongRuntimePage(), "http://viewer", spec, tmp_path,
            provenance={"head": "h", "tree": "t", "clean": True, "worktree_digest": "w"},
            expected_revision="bundle-1",
            input_inventory=[{"path": "source.py", "type": "regular", "tracked": True}],
            input_inventory_digest="i", capture_fn=capture_fn,
        )
    assert caught.value.diagnostic["code"] == "revision-mismatch"


def test_artifact_record_rejects_revision_that_was_only_recorded(tmp_path: Path) -> None:
    spec = next(item for item in capture.SCENARIO_SPECS if item.id == "theme-bone")
    image = tmp_path / spec.artifact
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (1).to_bytes(4, "big") + (1).to_bytes(4, "big"))
    with pytest.raises(capture.CaptureFailure) as caught:
        capture._artifact_record(
            image, spec,
            provenance={"head": "h", "tree": "t", "clean": True, "worktree_digest": "w"},
            expected_revision="bundle-1",
            input_inventory=[{"path": "source.py", "type": "regular", "tracked": True}],
            input_inventory_digest="i",
            runtime={"generation": 1, "metadata": {"revision": "bundle-1"}},
            theme_state={"resolved_theme": "bone", "resolution": "resolved", "contrast": "normal", "border": "on"},
            browser={"engine": "chromium", "javascript": True},
        )
    assert caught.value.diagnostic["code"] == "revision-missing"


@pytest.mark.parametrize("revisions", [
    {}, {"demo": "d", "dense-docs": "x"},
    {"demo": "d", "dense-docs": "x", "empty-okf": None},
    {"demo": "d", "dense-docs": "x", "empty-okf": ""},
    {"demo": "d", "dense-docs": "x", "empty-okf": "e", "unrelated": "u"},
])
def test_fixture_revision_map_is_exact_complete_and_nonempty(revisions) -> None:
    with pytest.raises(capture.CapturePreconditionError, match="fixture revision"):
        capture._validate_fixture_revisions(revisions)


@pytest.mark.parametrize("field", ["head", "tree"])
@pytest.mark.parametrize("value", [None, "", "   "])
def test_artifact_rejects_empty_source_identity(tmp_path: Path, field: str, value) -> None:
    spec = capture.SCENARIO_SPECS[0]
    image = tmp_path / spec.artifact
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (1).to_bytes(4, "big") + (1).to_bytes(4, "big"))
    provenance = {"head": "h", "tree": "t", "clean": True, "worktree_digest": "w"}
    provenance[field] = value
    with pytest.raises(capture.CapturePreconditionError, match="source identity"):
        capture._artifact_record(
            image, spec, provenance=provenance, expected_revision="r",
            input_inventory=[{"path": "x"}], input_inventory_digest="i",
            runtime={"generation": 1, "metadata": {"revision": "r", "pageRevision": "r"}},
            theme_state={}, browser={},
        )


@pytest.mark.parametrize("field", ["head", "tree"])
@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_final_capture_set_rejects_noncanonical_source_identity(
    tmp_path: Path, monkeypatch, field: str, value,
) -> None:
    revisions = {"demo": "demo-r", "dense-docs": "dense-r", "empty-okf": "empty-okf-r"}
    inventory = [{"path": "source.py", "type": "regular", "tracked": True}]
    digest = capture._digest(inventory)
    rows = [_production_shaped_row(tmp_path, spec, revisions, inventory, digest)
            for spec in capture.SCENARIO_SPECS]
    rows[0][field] = value
    monkeypatch.setattr(capture, "_validate_supporting_artifacts", lambda *_a, **_k: [])

    with pytest.raises(capture.CapturePreconditionError, match="source identity"):
        capture._validate_capture_set(
            tmp_path, rows, expected_revision=revisions,
            input_inventory=inventory, input_inventory_digest=digest,
        )


@pytest.mark.parametrize("flag", ["--workspace", "--source-root"])
def test_cross_root_flags_fail_with_typed_workspace_mismatch(tmp_path: Path, flag: str) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/capture_ui_overhaul.py"), flag, str(tmp_path), "--manifest-only"],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["code"] == "workspace-mismatch"


def test_source_root_environment_cannot_select_another_root(tmp_path: Path) -> None:
    env = {**os.environ, "OKF_CAPTURE_SOURCE_ROOT": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/capture_ui_overhaul.py"), "--manifest-only"],
        cwd=ROOT, text=True, capture_output=True, env=env,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["code"] == "workspace-mismatch"


def test_exact_root_compatibility_values_do_not_change_runner_behavior() -> None:
    baseline = subprocess.run(
        [sys.executable, str(ROOT / "scripts/capture_ui_overhaul.py"), "--manifest-only"],
        cwd=ROOT, text=True, capture_output=True, check=True,
    ).stdout
    for argv, env in (
        (["--workspace", str(ROOT), "--manifest-only"], os.environ.copy()),
        (["--source-root", str(ROOT), "--manifest-only"], os.environ.copy()),
        (["--manifest-only"], {**os.environ, "OKF_CAPTURE_SOURCE_ROOT": str(ROOT)}),
    ):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/capture_ui_overhaul.py"), *argv],
            cwd=ROOT, text=True, capture_output=True, env=env, check=True,
        )
        assert result.stdout == baseline


def test_bundle_admission_is_strictly_root_confined(tmp_path: Path) -> None:
    assert capture._validate_release_bundle(Path("samples/demo_bundle")) == (ROOT / "samples/demo_bundle").resolve()
    with tempfile.TemporaryDirectory(prefix="okf-bundle-outside-", dir="/tmp") as name:
        outside = Path(name)
        for unsafe in (outside.resolve(), Path("../outside"), Path("nested/../samples/demo_bundle")):
            with pytest.raises(capture.CapturePreconditionError, match="inside sealed checkout"):
                capture._validate_release_bundle(unsafe)


def test_bundle_admission_rejects_symlink_escape(tmp_path: Path) -> None:
    link = ROOT / "capture-root-escape-test"
    with tempfile.TemporaryDirectory(prefix="okf-symlink-outside-", dir="/tmp") as name:
        try:
            link.symlink_to(name, target_is_directory=True)
            with pytest.raises(capture.CapturePreconditionError, match="inside sealed checkout"):
                capture._validate_release_bundle(Path(link.name))
        finally:
            link.unlink(missing_ok=True)


def test_import_global_mutation_cannot_redirect_repository_authority(monkeypatch, tmp_path: Path) -> None:
    physical = ROOT.resolve()
    with tempfile.TemporaryDirectory(prefix="okf-root-mutation-", dir="/tmp") as outside_name:
      outside = Path(outside_name)
      for name in ("ROOT", "DEFAULT_BUNDLE", "DEFAULT_DENSE_BUNDLE", "DEFAULT_EMPTY_BUNDLE",
                   "_AUTHORITY", "_repo_root", "REPOSITORY_ROOT", "_sealed_repository_root",
                   "_sealed_checkout_identity", "_seal_repository_authority"):
          monkeypatch.setattr(capture, name, outside, raising=False)

      calls = []
      def run(command, **kwargs):
          calls.append((command, kwargs))
          return subprocess.CompletedProcess(command, 0, stdout="physical\n", stderr="")
      monkeypatch.setattr(capture.subprocess, "run", run)
      assert capture._git_output(("rev-parse", "HEAD")) == "physical\n"
      assert calls[-1][1]["cwd"] == physical
      assert capture._validate_release_bundle(physical / "samples/demo_bundle") == physical / "samples/demo_bundle"
      with pytest.raises(capture.CapturePreconditionError, match="inside sealed checkout"):
          capture._validate_release_bundle(outside)
      assert capture._git_output.__kwdefaults__["_root"]() == physical


def _git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def _selected_repo(tmp_path: Path):
    """Tiny repository: the selected driver is observably not the caller driver."""
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repo / "scripts").mkdir()
    (repo / "selected-only").mkdir()
    (repo / "selected-only/product.txt").write_text("PRODUCT-A")
    driver = '''import json
from pathlib import Path
SELECTED_CAPTURE_PROTOCOL = "okf-ui-selected-v1"
def selected_capture_v1(request):
    root = Path(__file__).resolve().parents[1]
    assert root == Path(request["runtime_root"])
    product = (root / request["bundle"] / "product.txt").read_text()
    manifest = {"schema": 4, "source_provenance": request["provenance"],
        "input_inventory": request["inventory"],
        "input_inventory_digest": request["inventory_digest"],
        "input_inventory_digest_after": request["inventory_digest"],
        "driver_observed": "DRIVER-A", "product_observed": product}
    (Path(request["stage"]) / "manifest.json").write_text(json.dumps(manifest))
    return 0
'''
    (repo / "scripts/capture_ui_overhaul.py").write_text(driver)
    git("add", ".")
    git("commit", "-qm", "A")
    selected = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    shutil.rmtree(repo / "selected-only")
    (repo / "scripts/capture_ui_overhaul.py").write_text("raise RuntimeError('DRIVER-B MUST NOT RUN')\n")
    git("add", "-A")
    git("commit", "-qm", "B")
    return repo, git, selected, tree


def test_selected_public_dispatch_executes_selected_driver_and_product(tmp_path: Path) -> None:
    repo, git, selected, tree = _selected_repo(tmp_path)
    git("checkout", "--detach", "-q")
    (repo / "scripts/capture_ui_overhaul.py").write_text("DIRTY DRIVER SECRET")
    secret = repo / "credential-secret-untracked.txt"
    secret.write_text("PRIVATE")
    before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    destination = tmp_path / "out"
    assert capture.main(["--revision", selected, "--bundle", "selected-only", "--out", str(destination)],
                        _root=lambda: repo) == 0
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["driver_observed"] == "DRIVER-A"
    assert manifest["product_observed"] == "PRODUCT-A"
    assert manifest["source_provenance"]["commit"] == selected
    assert manifest["source_provenance"]["tree"] == tree
    assert manifest["source_provenance"]["kind"] == "selected-commit"
    assert "clean" not in manifest["source_provenance"]
    assert secret.name not in json.dumps(manifest) and "PRIVATE" not in json.dumps(manifest)
    assert before == {p.relative_to(repo): p.read_bytes() for p in repo.rglob("*") if p.is_file()}


def test_selected_cli_from_shallow_dirty_caller_uses_locally_complete_tree(tmp_path: Path) -> None:
    repo, git, selected, tree = _selected_repo(tmp_path)
    clone = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth=2", repo.as_uri(), str(clone)], check=True)
    assert (clone / ".git/shallow").is_file()
    # The public launcher is intentionally uncommitted B; A's stub must execute.
    shutil.copyfile(ROOT / "scripts/capture_ui_overhaul.py", clone / "scripts/capture_ui_overhaul.py")
    (clone / "dirty-token-private.txt").write_text("SECRET")
    before = {p.relative_to(clone): p.read_bytes() for p in clone.rglob("*") if p.is_file()}
    out = tmp_path / "out"
    result = subprocess.run([sys.executable, "-B", str(clone / "scripts/capture_ui_overhaul.py"),
                             "--revision", selected, "--bundle", "selected-only", "--out", str(out)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((out / "manifest.json").read_text())
    assert (manifest["driver_observed"], manifest["product_observed"]) == ("DRIVER-A", "PRODUCT-A")
    assert manifest["source_provenance"]["commit"] == selected
    assert manifest["source_provenance"]["tree"] == tree
    assert "dirty-token-private" not in result.stdout + result.stderr + json.dumps(manifest)
    assert before == {p.relative_to(clone): p.read_bytes() for p in clone.rglob("*") if p.is_file()}


@pytest.mark.parametrize("invalid", ["", " ", "HEAD", "a" * 39, "A" * 40, "a" * 40 + "\n", "--help", "é" * 40,
                                     "missing", "blob", "tree", "tag"])
def test_selected_revision_requires_full_available_commit(tmp_path: Path, capsys, invalid: str) -> None:
    repo, git, selected, tree = _selected_repo(tmp_path)
    if invalid == "missing": invalid = "0" * 40
    if invalid == "blob": invalid = git("rev-parse", selected + ":selected-only/product.txt")
    if invalid == "tree": invalid = tree
    if invalid == "tag":
        git("tag", "-a", "fixture-tag", selected, "-m", "tag")
        invalid = git("rev-parse", "fixture-tag")
    out = generation(tmp_path / "out", "old")
    assert capture.main(["--revision=" + invalid, "--out", str(out)], _root=lambda: repo) == 2
    assert_complete(out, ("old",))
    assert not list(tmp_path.glob(".out.capture-stage-*"))
    assert "selected" in capsys.readouterr().err


@pytest.mark.parametrize("fault", ["missing-blob", "missing-tree", "corrupt-blob", "corrupt-tree", "corrupt-commit",
                                   "symlink", "gitlink", "grafts", "alternates", "packed-replace",
                                   "promisor", "unsafe-shallow", "unsupported", "missing-driver"])
def test_selected_object_and_driver_faults_preserve_destination(tmp_path: Path, fault: str, capsys) -> None:
    import zlib
    repo, git, selected, tree = _selected_repo(tmp_path)
    if fault in {"missing-blob", "missing-tree", "corrupt-blob", "corrupt-tree", "corrupt-commit"}:
        oid = (tree if fault in {"corrupt-tree", "missing-tree"} else selected if fault == "corrupt-commit"
               else git("rev-parse", selected + ":selected-only/product.txt"))
        obj = repo / ".git/objects" / oid[:2] / oid[2:]
        if fault.startswith("missing-"): obj.unlink()
        else:
            kind = "tree" if fault == "corrupt-tree" else "commit" if fault == "corrupt-commit" else "blob"
            original = subprocess.check_output(["git", "-C", str(repo), "cat-file", kind, oid])
            # Preserve syntactic validity: the hash check, not merely Git's
            # parser, must detect these corrupt responses under the old OID.
            changed = (original.replace(b"selected-only", b"selected-fake") if kind == "tree" else
                       original.replace(tree.encode(), git("rev-parse", "HEAD^{tree}").encode()) if kind == "commit" else b"BAD")
            obj.chmod(0o600)
            obj.write_bytes(zlib.compress(kind.encode() + b" " + str(len(changed)).encode() + b"\0" + changed))
    elif fault in {"symlink", "gitlink"}:
        oid = selected if fault == "gitlink" else git("rev-parse", selected + ":selected-only/product.txt")
        git("update-index", "--add", "--cacheinfo", "160000" if fault == "gitlink" else "120000", oid, "unsafe")
        git("commit", "-qm", "unsafe mode")
        selected = git("rev-parse", "HEAD")
    elif fault == "grafts": (repo / ".git/info/grafts").write_text(selected + "\n")
    elif fault == "alternates":
        (repo / ".git/objects/info/alternates").write_text(str(repo / ".git/objects") + "\n")
    elif fault == "packed-replace":
        git("replace", selected, "HEAD")
        git("pack-refs", "--all", "--prune")
    elif fault == "promisor": git("config", "remote.origin.promisor", "true")
    elif fault == "unsafe-shallow":
        (repo / ".git/shallow").symlink_to(repo / ".git/HEAD")
    elif fault == "unsupported": selected = git("rev-parse", "HEAD")
    elif fault == "missing-driver":
        git("rm", "scripts/capture_ui_overhaul.py")
        (repo / "sentinel").write_text("nonempty tree")
        git("add", "sentinel")
        git("commit", "-qm", "missing driver")
        selected = git("rev-parse", "HEAD")
    out = generation(tmp_path / "out", "old")
    assert capture.main(["--revision", selected, "--bundle", "selected-only", "--out", str(out)],
                        _root=lambda: repo) == 2
    assert_complete(out, ("old",))
    assert not list(tmp_path.glob(".out.capture-stage-*"))
    if fault in {"unsupported", "missing-driver"}: assert "unsupported selected capture driver" in capsys.readouterr().err
    if fault in {"corrupt-tree", "corrupt-commit"}: assert "does not match" in capsys.readouterr().err


@pytest.mark.parametrize("mutation", ["product", "symlink-directory", "broken-link", "unknown-schema", "unknown-source", "legacy-clean", "protocol", "signature"])
def test_selected_driver_or_snapshot_mutation_cannot_publish(tmp_path: Path, mutation: str, monkeypatch) -> None:
    repo, git, selected, _ = _selected_repo(tmp_path)
    # Modify A's stub in a new selected commit without changing the launcher.
    driver = git("show", selected + ":scripts/capture_ui_overhaul.py")
    changes = {
        "product": '(root / "selected-only/product.txt").write_text("TAMPERED")',
        "symlink-directory": '(root / "linked").symlink_to(root / "selected-only", target_is_directory=True)',
        "broken-link": '(root / "broken").symlink_to(root / "absent")',
        "unknown-schema": 'manifest["schema"] = 999',
        "unknown-source": 'manifest["source_provenance"]["extra"] = "unknown"',
        "legacy-clean": 'manifest["clean"] = True',
    }
    if mutation == "protocol": driver = driver.replace('"okf-ui-selected-v1"', '"future-v99"')
    elif mutation == "signature": driver = driver.replace("selected_capture_v1(request)", "selected_capture_v1(request, extra)")
    else: driver = driver.replace('    (Path(request["stage"])', '    ' + changes[mutation] + '\n    (Path(request["stage"])')
    (repo / "scripts/capture_ui_overhaul.py").write_text(driver + "\n")
    (repo / "selected-only").mkdir()
    (repo / "selected-only/product.txt").write_text("PRODUCT-A")
    git("add", "-A"); git("commit", "-qm", "fault driver")
    out = generation(tmp_path / "out", "old")
    monkeypatch.setattr(capture, "_verify_publication_provenance", _REAL_PUBLICATION_VERIFIER)
    assert capture.main(["--revision", git("rev-parse", "HEAD"), "--bundle", "selected-only", "--out", str(out)],
                        _root=lambda: repo) == 2
    assert_complete(out, ("old",))
    assert not list(tmp_path.glob(".out.capture-stage-*"))


@pytest.mark.parametrize("wrong_page", [False, True])
def test_selected_production_entrypoint_and_content_revision_fence(tmp_path: Path, wrong_page: bool, monkeypatch) -> None:
    repo, git, _, _ = _selected_repo(tmp_path)
    # Exercise the real selected entrypoint, inventory checks and artifact
    # revision fence. Only the heavyweight browser consumer is replaced.
    consumer = '''
def _run_capture(args, *, root, admitted):
    runtime, provenance, stage = admitted
    bundle = runtime.admit_bundle(args.bundle)
    assert (bundle.path / "product.txt").read_text() == "PRODUCT-A"
    inventory = _runtime_inventory(runtime)
    digest = _digest(inventory)
    path = stage / "proof.png"
    path.write_bytes(b"\\x89PNG\\r\\n\\x1a\\n" + b"\\0" * 8 + (1).to_bytes(4,"big") * 2)
    row = _artifact_record(path, SCENARIO_SPECS[0], provenance=provenance,
        expected_revision="CONTENT-A", input_inventory=inventory, input_inventory_digest=digest,
        runtime={"metadata": {"revision": "CONTENT-A", "pageRevision": PAGE_REVISION}},
        theme_state={}, browser={})
    assert "clean" not in row and "worktree" not in row
    assert row["source_provenance"]["commit"] != row["source_revision"]
    _write_json_atomic(stage / "manifest.json", {"schema": 4, **_source_record(provenance),
        "input_inventory": inventory, "input_inventory_digest": digest,
        "input_inventory_digest_after": digest, "artifacts": [row],
        "driver_observed": "PRODUCTION-ENTRY-A"})
    return 0
'''.replace("PAGE_REVISION", repr("WRONG" if wrong_page else "CONTENT-A"))
    (repo / "scripts/capture_ui_overhaul.py").write_text((ROOT / "scripts/capture_ui_overhaul.py").read_text() + consumer)
    (repo / "selected-only").mkdir()
    (repo / "selected-only/product.txt").write_text("PRODUCT-A")
    git("add", "-A"); git("commit", "-qm", "compatible driver")
    selected = git("rev-parse", "HEAD")
    (repo / "scripts/capture_ui_overhaul.py").write_text("DIRTY DRIVER B")
    out = generation(tmp_path / "out", "old")
    monkeypatch.setattr(capture, "_verify_publication_provenance", _REAL_PUBLICATION_VERIFIER)
    assert capture.main(["--revision", selected, "--bundle", "selected-only", "--out", str(out)],
                        _root=lambda: repo) == (2 if wrong_page else 0)
    if wrong_page: assert_complete(out, ("old",))
    else:
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["driver_observed"] == "PRODUCTION-ENTRY-A"
        assert manifest["artifacts"][0]["source_revision"] == "CONTENT-A"
        assert manifest["source_provenance"]["commit"] == selected


@pytest.mark.parametrize("mode", ["dirty", "shallow"])
def test_default_cli_still_rejects_dirty_and_shallow_callers(tmp_path: Path, mode: str) -> None:
    repo, git, _, _ = _selected_repo(tmp_path)
    shutil.copyfile(ROOT / "scripts/capture_ui_overhaul.py", repo / "scripts/capture_ui_overhaul.py")
    (repo / "samples/demo_bundle").mkdir(parents=True)
    (repo / "samples/demo_bundle/concept.md").write_text("---\ntype: Test\n---\nTest\n")
    git("add", "-A"); git("commit", "-qm", "launcher")
    if mode == "shallow":
        clone = tmp_path / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth=1", repo.as_uri(), str(clone)], check=True)
        repo = clone
    else: (repo / "private-secret-filename").write_text("SECRET")
    out = generation(tmp_path / "out", "old")
    result = subprocess.run([sys.executable, "-B", str(repo / "scripts/capture_ui_overhaul.py"), "--out", str(out)],
                            text=True, capture_output=True)
    assert result.returncode == 2
    assert ("clean worktree" if mode == "dirty" else "shallow metadata") in result.stderr
    assert "private-secret-filename" not in result.stdout + result.stderr
    assert_complete(out, ("old",))


def test_selected_capture_set_requires_selected_source_and_observed_content_revision(tmp_path: Path, monkeypatch) -> None:
    revisions = {"demo": "bundle-1", "dense-docs": "bundle-2", "empty-okf": "bundle-3"}
    inventory = [{"path": "scripts/capture_ui_overhaul.py", "oid": "a" * 40, "sha256": "b" * 64}]
    digest = capture._digest(inventory)
    provenance = {"kind": "selected-commit", "protocol": capture.SELECTED_CAPTURE_PROTOCOL,
                  "commit": "c" * 40, "tree": "d" * 40, "object_format": "sha1",
                  "driver": inventory[0], "inventory_digest": digest}
    rows = [_production_shaped_row(tmp_path, spec, revisions, inventory, digest) for spec in capture.SCENARIO_SPECS]
    for row in rows:
        for key in ("head", "tree", "clean", "worktree", "worktree_status", "worktree_digest"): row.pop(key)
        row["source_provenance"] = provenance
    monkeypatch.setattr(capture, "_validate_supporting_artifacts", lambda *_a, **_k: [])
    def validate():
        capture._validate_capture_set(tmp_path, rows, expected_revision=revisions, input_inventory=inventory,
                                      input_inventory_digest=digest, selected_provenance=provenance)
    validate()
    rows[0]["observed_revision"] = provenance["commit"]
    with pytest.raises(capture.CapturePreconditionError, match="expected source revision"): validate()
    rows[0]["observed_revision"] = revisions[capture.SCENARIO_SPECS[0].bundle]
    rows[0]["clean"] = True
    with pytest.raises(capture.CapturePreconditionError, match="selected-source"): validate()
    rows[0].pop("clean")
    provenance["extra"] = "unknown"
    with pytest.raises(capture.CapturePreconditionError, match="invalid selected-source"): validate()


def test_selected_publication_rechecks_inventory_under_existing_lock(tmp_path: Path, monkeypatch) -> None:
    repo, _, selected, _ = _selected_repo(tmp_path)
    out = generation(tmp_path / "out", "old")
    def verify(expected):
        assert expected.source_kind == "selected-commit" and not expected.clean
        (expected.runtime.root / "selected-only/product.txt").write_text("LATE CHANGE")
        _REAL_PUBLICATION_VERIFIER(expected)
    monkeypatch.setattr(capture, "_verify_publication_provenance", verify)
    assert capture.main(["--revision", selected, "--bundle", "selected-only", "--out", str(out)],
                        _root=lambda: repo) == 2
    assert_complete(out, ("old",))
    assert not list(tmp_path.glob(".out.capture-stage-*"))


@pytest.mark.parametrize("destination", ["inside", "symlink", "file"])
def test_selected_dispatch_retains_external_destination_admission(tmp_path: Path, destination: str) -> None:
    repo, _, selected, _ = _selected_repo(tmp_path)
    out = repo / "out" if destination == "inside" else tmp_path / "out"
    if destination == "symlink": out.symlink_to(repo, target_is_directory=True)
    if destination == "file": out.write_text("OLD")
    assert capture.main(["--revision", selected, "--out", str(out)], _root=lambda: repo) == 2
    if destination == "symlink": assert out.is_symlink()
    if destination == "file": assert out.read_text() == "OLD"
    assert not (repo / "out").exists()


@pytest.mark.parametrize("payload", [None, {}, {"protocol": "unknown"}, {"protocol": capture.SELECTED_CAPTURE_PROTOCOL, "unknown": True}])
def test_selected_driver_rejects_unknown_request_metadata(payload) -> None:
    with pytest.raises(capture.CapturePreconditionError, match="unsupported selected driver request"):
        capture.selected_capture_v1(payload)


def _fake_git_snapshot(root: Path, bundle: Path, files: dict[str, tuple[bytes, str]], *, hook=None):
    head, tree = "a" * 40, "b" * 40
    (root / ".git" / "objects" / "info").mkdir(parents=True, exist_ok=True)
    relative_root = bundle.relative_to(root).as_posix()
    blobs = {_git_blob(data): data for data, _mode in files.values()}
    rows = b"".join(
        mode.encode() + b" blob " + _git_blob(data).encode() + b"\t" +
        f"{relative_root}/{path}".encode() + b"\0"
        for path, (data, mode) in files.items()
    )
    calls = []
    def git(args):
        calls.append(tuple(args))
        if args[:3] == ("ls-files", "-z", "--others"): return b""
        if args[:3] == ("ls-tree", "-z", "--full-tree"):
            return b"040000 tree " + tree.encode() + b"\t" + relative_root.encode() + b"\0"
        if args[:3] == ("ls-tree", "-rz", "--full-tree"): return rows
        if args[:2] == ("cat-file", "blob"):
            data = blobs[args[2]]
            if hook: hook(args[2], data)
            return data
        if args == ("rev-parse", "HEAD"): return (head + "\n").encode()
        if args == ("rev-parse", "HEAD^{tree}"): return (tree + "\n").encode()
        if args == ("rev-parse", "--git-common-dir"): return b".git\n"
        if args == ("rev-parse", "--git-path", "objects"): return b".git/objects\n"
        if args == ("rev-parse", "--show-object-format"): return b"sha1\n"
        raise AssertionError(args)
    provenance = lambda **_kwargs: {"head": head, "tree": tree, "object_format": "sha1", "clean": True}
    return git, provenance, calls


def _admit_git_fixture(root: Path, bundle: Path, files, *, hook=None):
    git, provenance, calls = _fake_git_snapshot(root, bundle, files, hook=hook)
    admitted = capture._admit_release_bundle(
        bundle, _validate=lambda supplied: Path(supplied), _root=lambda: root,
        _git=git, _provenance=provenance,
    )
    return admitted, calls


def test_git_object_snapshot_preserves_nested_exact_blob_bytes_and_modes(tmp_path: Path) -> None:
    root = tmp_path / "repo"; bundle = root / "bundle"; bundle.mkdir(parents=True)
    files = {"index.md": (b"root\x00bytes\n", "100644"),
             "nested/tool": (b"#!/bin/sh\n", "100755")}
    admitted, calls = _admit_git_fixture(root, bundle, files)
    with admitted:
        assert (admitted.path / "index.md").read_bytes() == files["index.md"][0]
        assert (admitted.path / "nested/tool").read_bytes() == files["nested/tool"][0]
        assert (admitted.path / "nested/tool").stat().st_mode & stat.S_IXUSR
        assert [row["path"] for row in admitted.inventory] == ["index.md", "nested/tool"]
    assert all(call[:2] == ("cat-file", "blob") for call in calls if call[0] == "cat-file")


def test_complete_runtime_workspace_materializes_and_verifies_entire_commit_tree(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    files = {
        "scripts/okf-loom": (b"#!/bin/sh\necho committed\n", "100755"),
        "scripts/okf_loom/viewer/templates/wiki.html": (b"COMMITTED-TEMPLATE", "100644"),
        "scripts/okf_loom/viewer/static/wiki.css": (b"COMMITTED-ASSET", "100644"),
        "samples/demo_bundle/index.md": (b"# committed bundle\n", "100644"),
    }
    head, tree = "a" * 40, "b" * 40
    (root / ".git" / "objects" / "info").mkdir(parents=True)
    blobs = {_git_blob(data): data for data, _mode in files.values()}
    rows = b"".join(
        mode.encode() + b" blob " + _git_blob(data).encode() + b"\t" +
        path.encode() + b"\0" for path, (data, mode) in files.items()
    )
    def git(args):
        if args == ("rev-parse", "--git-common-dir"): return b".git\n"
        if args == ("rev-parse", "--git-path", "objects"): return b".git/objects\n"
        if args == ("rev-parse", "--show-object-format"): return b"sha1\n"
        if args == ("ls-tree", "-rz", "--full-tree", head): return rows
        if args[:2] == ("cat-file", "blob"): return blobs[args[2]]
        if args == ("rev-parse", "HEAD"): return (head + "\n").encode()
        raise AssertionError(args)

    workspace = capture._materialize_runtime_workspace(
        {"head": head, "tree": tree, "object_format": "sha1", "clean": True},
        _root=lambda: root, _git=git,
    )
    workspace_root = workspace.root
    with workspace:
        assert (workspace.root / "scripts/okf_loom/viewer/templates/wiki.html").read_bytes() == b"COMMITTED-TEMPLATE"
        assert (workspace.root / "scripts/okf_loom/viewer/static/wiki.css").read_bytes() == b"COMMITTED-ASSET"
        assert (workspace.root / "scripts/okf-loom").stat().st_mode & stat.S_IXUSR
        assert {row["path"] for row in workspace.inventory} == set(files)
        workspace.verify()
    assert not workspace_root.exists()


def test_runtime_consumers_are_explicitly_workspace_bound_after_materialization(
    tmp_path: Path, monkeypatch,
) -> None:
    physical = tmp_path / "physical"
    immutable = tmp_path / "immutable"
    for root, marker in ((physical, "PHYSICAL-B"), (immutable, "COMMITTED-A")):
        (root / "scripts").mkdir(parents=True)
        (root / "bundle").mkdir()
        helper = root / "scripts/okf-loom"
        helper.write_text("#!/usr/bin/env python3\nimport pathlib,sys\n"
                          "bundle=pathlib.Path(sys.argv[2]); out=pathlib.Path(sys.argv[-1]); out.mkdir(parents=True,exist_ok=True); "
                          "(out/'proof.txt').write_text(pathlib.Path(__file__).read_text()+bundle.joinpath('index.md').read_text())\n")
        helper.chmod(0o700)
        (root / "bundle/index.md").write_text(marker)
    workspace = capture.ImmutableRuntimeWorkspace.for_test(immutable, physical)
    bundle = workspace.admit_bundle(physical / "bundle")
    monkeypatch.setattr(capture, "_sealed_repository_root", lambda: physical)

    out = tmp_path / "out"
    capture._build_target(bundle, "static", out, runtime=workspace)

    proof = (out / "proof.txt").read_text()
    assert "COMMITTED-A" in proof and "PHYSICAL-B" not in proof
    assert workspace.accesses[-1]["operation"] == "build"
    assert workspace.accesses[-1]["root"] == str(immutable)


def test_commit_a_workspace_survives_physical_runtime_replacement_for_real_build_and_serve(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for relative in ("scripts", "samples/demo_bundle"):
        source = ROOT / relative
        target = candidate / relative
        shutil.copytree(
            source, target,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".okf-loom", "*.okf-lock"),
        )
    subprocess.run(["git", "init", "--quiet"], cwd=candidate, check=True)
    subprocess.run(["git", "config", "user.name", "Runtime Workspace Test"], cwd=candidate, check=True)
    subprocess.run(["git", "config", "user.email", "runtime@example.invalid"], cwd=candidate, check=True)
    subprocess.run(["git", "add", "-A"], cwd=candidate, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "commit A"], cwd=candidate, check=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    probe = """import importlib.util,json,sys
from pathlib import Path
root=Path(sys.argv[1]); spec=importlib.util.spec_from_file_location('capture',root/'scripts/capture_ui_overhaul.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
p=m._git_provenance(require_clean=True); w=m._materialize_runtime_workspace(p)
print(json.dumps({'root':str(w.root),'head':w.head,'tree':w.tree,'source':str(w.source_root)}))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(candidate)], cwd=candidate,
        env=env, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    # A materialized workspace is process-owned; production proves its lifetime
    # in-process. This subprocess only proves the exact candidate can initialize.
    initialized = json.loads(result.stdout)
    assert initialized["source"] == str(candidate)

    spec = importlib.util.spec_from_file_location("candidate_capture", candidate / "scripts/capture_ui_overhaul.py")
    assert spec and spec.loader
    previous = sys.dont_write_bytecode; sys.dont_write_bytecode = True
    try:
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    provenance = module._git_provenance(require_clean=True)
    workspace = module._materialize_runtime_workspace(provenance)
    try:
        bundle = workspace.admit_bundle(candidate / "samples/demo_bundle")
        marker_b = "PHYSICAL-RUNTIME-MARKER-B"
        (candidate / "scripts/okf_loom/viewer/templates/wiki.html").write_text(marker_b)
        (candidate / "scripts/okf_loom/viewer/static/wiki.css").unlink()
        (candidate / "scripts/okf-loom").write_text("#!/bin/sh\necho " + marker_b + " >&2\nexit 91\n")
        (candidate / "scripts/okf-loom").chmod(0o700)

        built = tmp_path / "built"
        module._build_target(bundle, "static", built, runtime=workspace)
        built_bytes = b"".join(path.read_bytes() for path in built.rglob("*") if path.is_file())
        assert marker_b.encode() not in built_bytes

        immutable_before = module._runtime_inventory(workspace)
        server = module._LiveServer(bundle, runtime=workspace)
        with server as base:
            with urllib.request.urlopen(base + "/tables/orders", timeout=5) as response:
                served = response.read()
            assert server.state_root is not None
            mutable_state = server.state_root
            token = (mutable_state / ".token").read_text()
            request = urllib.request.Request(
                base + "/__presence", method="POST",
                data=json.dumps({"actor": "agent", "state": "watching"}).encode(),
                headers={"Content-Type": "application/json", "X-OKF-Token": token,
                         "Origin": base},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 200
            assert (mutable_state / "events.jsonl").is_file()
            assert not mutable_state.is_relative_to(workspace.root)
            assert not (bundle.path / ".okf-loom/session").exists()
            assert module._runtime_inventory(workspace) == immutable_before
        assert marker_b.encode() not in served
        _assert_sanitized_tree(mutable_state)
        assert server.cleanup_result.disposition == "sanitized-retained-tree"
        assert server.retention_record["contentState"] == "sanitized-empty-tree"
        shutil.rmtree(mutable_state)
        assert module._runtime_inventory(workspace) == immutable_before
        assert workspace.head == provenance["head"] and workspace.tree == provenance["tree"]
        assert {row["operation"] for row in workspace.accesses} >= {"bundle", "build", "serve"}
    finally:
        workspace.__exit__(None, None, None)
    assert not workspace.root.exists()


def test_direct_inventory_revision_build_and_live_consumers_use_only_admitted_bytes(
    tmp_path: Path, monkeypatch,
) -> None:
    original = tmp_path / "original"; runtime_root = tmp_path / "runtime"; snapshot = runtime_root / "snapshot"
    original.mkdir(); snapshot.mkdir(parents=True)
    (runtime_root / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/okf-loom", runtime_root / "scripts/okf-loom")
    shutil.copytree(ROOT / "scripts/okf_loom", runtime_root / "scripts/okf_loom")
    divergent = "ORIGINAL-DIVERGENT-MARKER"
    admitted_marker = "ADMITTED-SNAPSHOT-MARKER"
    (original / "index.md").write_text(f"# {divergent}\n")
    concept = "---\ntype: Note\ntitle: Snapshot proof\n---\n\n" + admitted_marker + "\n"
    (snapshot / "index.md").write_text("# Snapshot index\n")
    (snapshot / "proof.md").write_text(concept)
    owner = tempfile.TemporaryDirectory()
    admitted = capture.AdmittedBundle(
        original, snapshot, (0, 0), (
            {"path": "index.md", "git_mode": "100644"},
            {"path": "proof.md", "git_mode": "100644"},
        ), owner, "a" * 40, "b" * 40,
    )
    monkeypatch.setattr(capture, "_tracked_paths", lambda: [])

    inventory = capture._inventory(admitted, _root=lambda: tmp_path)
    revision = capture._derive_expected_revision(admitted)
    assert revision and {row["path"] for row in inventory} == {
        "original/index.md", "original/proof.md",
    }
    assert all(row["sha256"] != hashlib.sha256(divergent.encode()).hexdigest() for row in inventory)

    built = tmp_path / "built"
    runtime = capture.ImmutableRuntimeWorkspace.for_test(runtime_root, tmp_path)
    capture._build_target(admitted, "static", built, runtime=runtime)
    built_bytes = b"".join(path.read_bytes() for path in built.rglob("*") if path.is_file())
    assert admitted_marker.encode() in built_bytes and divergent.encode() not in built_bytes

    with capture._LiveServer(admitted, runtime=runtime) as base:
        with urllib.request.urlopen(base + "/proof", timeout=5) as response:
            live = response.read()
        assert admitted_marker.encode() in live and divergent.encode() not in live


def test_live_source_mutation_after_each_blob_read_cannot_affect_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "repo"; bundle = root / "bundle"; bundle.mkdir(parents=True)
    source = bundle / "index.md"; source.write_bytes(b"WORKTREE")
    def mutate(_oid, _data):
        source.write_bytes(b"MUTATED")
        swap = root / "outside"; swap.mkdir(exist_ok=True)
        source.unlink(missing_ok=True); source.symlink_to(swap, target_is_directory=True)
    admitted, _calls = _admit_git_fixture(root, bundle, {"index.md": (b"COMMITTED", "100644")}, hook=mutate)
    with admitted:
        assert (admitted.path / "index.md").read_bytes() == b"COMMITTED"
    assert not any(b"MUTATED" in row.get("sha256", "").encode() for row in admitted.inventory)


@pytest.mark.parametrize("raw", [
    b"bad\0",
    b"100644 blob " + b"a" * 40 + b"\tbundle/index.md",
    b"100644 blob " + b"a" * 40 + b"\tbundle/index.md\0\0",
    b"100644 blob " + b"a" * 40 + b"\tbundle/a\0\0" + b"100644 blob " + b"a" * 40 + b"\tbundle/b\0",
    b"100644 blob " + b"a" * 40 + b"bundle/index.md\0",
    b"100644 blob nope\tbundle/index.md\0",
    b"100644 blob " + b"a" * 40 + b"\tbundle/../index.md\0",
    b"100644 blob " + b"a" * 40 + b"\tbundle//index.md\0",
])
def test_ls_tree_parser_rejects_malformed_rows_and_unsafe_paths(raw: bytes) -> None:
    with pytest.raises(capture.CapturePreconditionError):
        capture._parse_ls_tree(raw, "bundle", object_format="sha1")


def test_ls_tree_parser_rejects_duplicate_path_conflict_symlink_gitlink_and_tree() -> None:
    oid = b"a" * 40
    rows = [
        b"100644 blob " + oid + b"\tbundle/a\0" + b"100644 blob " + oid + b"\tbundle/a\0",
        b"100644 blob " + oid + b"\tbundle/a\0" + b"100644 blob " + oid + b"\tbundle/a/b\0",
        b"120000 blob " + oid + b"\tbundle/link\0",
        b"160000 commit " + oid + b"\tbundle/submodule\0",
        b"040000 tree " + oid + b"\tbundle/dir\0",
    ]
    for raw in rows:
        with pytest.raises(capture.CapturePreconditionError):
            capture._parse_ls_tree(raw, "bundle", object_format="sha1")


@pytest.mark.parametrize("raw", [
    b"",
    b"040000 tree " + b"a" * 40 + b"\tbundle",
    b"040000 tree " + b"a" * 40 + b"\tother\0",
    b"040000 tree " + b"a" * 40 + b"\tbundle\0extra\0",
    b"100644 blob " + b"a" * 40 + b"\tbundle\0",
])
def test_bundle_root_tree_parser_rejects_missing_or_ambiguous_rows(raw: bytes) -> None:
    with pytest.raises(capture.CapturePreconditionError):
        capture._parse_bundle_root_tree(raw, "bundle", object_format="sha1")


def test_admission_rejects_head_movement_between_bundle_transactions_and_cleans_snapshots(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    first_bundle = root / "first"; second_bundle = root / "second"
    first_bundle.mkdir(parents=True); second_bundle.mkdir()
    first, _ = _admit_git_fixture(root, first_bundle, {"index.md": (b"FIRST", "100644")})
    first_snapshot = first.path
    moved_git, moved_provenance, _ = _fake_git_snapshot(
        root, second_bundle, {"index.md": (b"SECOND", "100644")})
    moved = {**moved_provenance(require_clean=True), "head": "c" * 40}
    try:
        with pytest.raises(capture.CapturePreconditionError, match="moved before bundle admission"):
            capture._admit_release_bundle(
                second_bundle, expected_head=first.head, expected_tree=first.tree,
                _validate=lambda p: Path(p), _root=lambda: root, _git=moved_git,
                _provenance=lambda **_kwargs: moved,
            )
    finally:
        first.owner.cleanup()
    assert not first_snapshot.exists()


def test_final_provenance_must_match_initial_transaction_revision() -> None:
    with pytest.raises(capture.CapturePreconditionError, match="transaction-bound revision"):
        capture._assert_bound_revision("a" * 40, "b" * 40, {
            "head": "c" * 40, "tree": "b" * 40,
        })


def test_git_snapshot_rejects_corrupt_blob_and_head_movement(tmp_path: Path) -> None:
    root = tmp_path / "repo"; bundle = root / "bundle"; bundle.mkdir(parents=True)
    git, provenance, _ = _fake_git_snapshot(root, bundle, {"index.md": (b"GOOD", "100644")})
    def corrupt(args):
        value = git(args)
        return b"CORRUPT" if args[:2] == ("cat-file", "blob") else value
    with pytest.raises(capture.CapturePreconditionError, match="does not match"):
        capture._admit_release_bundle(bundle, _validate=lambda p: Path(p), _root=lambda: root,
                                        _git=corrupt, _provenance=provenance)
    def moved(args):
        if args == ("rev-parse", "HEAD"): return ("c" * 40 + "\n").encode()
        return git(args)
    with pytest.raises(capture.CapturePreconditionError, match="moved"):
        capture._admit_release_bundle(bundle, _validate=lambda p: Path(p), _root=lambda: root,
                                        _git=moved, _provenance=provenance)

    def missing(args):
        if args[:2] == ("cat-file", "blob"):
            raise capture.CapturePreconditionError("sealed Git command failed (cat-file): missing")
        return git(args)
    with pytest.raises(capture.CapturePreconditionError, match="missing"):
        capture._admit_release_bundle(bundle, _validate=lambda p: Path(p), _root=lambda: root,
                                        _git=missing, _provenance=provenance)


def test_git_snapshot_rejects_untracked_or_ignored_and_missing_tree(tmp_path: Path) -> None:
    root = tmp_path / "repo"; bundle = root / "bundle"; bundle.mkdir(parents=True)
    git, provenance, _ = _fake_git_snapshot(root, bundle, {"index.md": (b"GOOD", "100644")})
    def ignored(args):
        if args[:3] == ("ls-files", "-z", "--others"): return b"bundle/secret\0"
        return git(args)
    with pytest.raises(capture.CapturePreconditionError, match="ignored"):
        capture._admit_release_bundle(bundle, _validate=lambda p: Path(p), _root=lambda: root,
                                        _git=ignored, _provenance=provenance)
    def missing(args):
        if args[:3] == ("ls-tree", "-z", "--full-tree"): return b""
        return git(args)
    with pytest.raises(capture.CapturePreconditionError, match="root tree metadata"):
        capture._admit_release_bundle(bundle, _validate=lambda p: Path(p), _root=lambda: root,
                                        _git=missing, _provenance=provenance)

    def reject_dirty(*, require_clean):
        assert require_clean
        raise capture.CapturePreconditionError("release capture requires a clean worktree")
    with pytest.raises(capture.CapturePreconditionError, match="clean worktree"):
        capture._admit_release_bundle(bundle, _validate=lambda p: Path(p), _root=lambda: root,
                                        _git=git, _provenance=reject_dirty)


def test_common_dir_alternates_reject_linked_worktree_object_authority(tmp_path: Path) -> None:
    root = tmp_path / "worktree"; bundle = root / "bundle"; bundle.mkdir(parents=True)
    common = tmp_path / "common.git"
    objects = common / "objects"
    (objects / "info").mkdir(parents=True)
    alternate = tmp_path / "alternate-objects"; alternate.mkdir()
    (objects / "info" / "alternates").write_text(str(alternate) + "\n")
    git, provenance, _ = _fake_git_snapshot(root, bundle, {"index.md": (b"ONLY-ALTERNATE", "100644")})
    def linked(args):
        if args == ("rev-parse", "--git-common-dir"): return (str(common) + "\n").encode()
        if args == ("rev-parse", "--git-path", "objects"): return (str(objects) + "\n").encode()
        return git(args)
    with pytest.raises(capture.CapturePreconditionError, match="alternates"):
        capture._admit_release_bundle(
            bundle, _validate=lambda p: Path(p), _root=lambda: root,
            _git=linked, _provenance=provenance,
        )


def test_sealed_git_command_ignores_mutable_git_environment(monkeypatch) -> None:
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs)); return subprocess.CompletedProcess(command, 0, stdout=b"ok", stderr=b"")
    monkeypatch.setenv("GIT_DIR", "/outside")
    monkeypatch.setenv("GIT_WORK_TREE", "/outside")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/outside")
    monkeypatch.setattr(capture.subprocess, "run", run)
    assert capture._git_bytes(("status",)) == b"ok"
    command, kwargs = calls[0]
    assert command[0] == capture._SEALED_GIT and command[1:3] == ["-C", str(ROOT)]
    assert "GIT_DIR" not in kwargs["env"] and "GIT_WORK_TREE" not in kwargs["env"]
    assert kwargs["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert kwargs["env"]["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == ""

def test_old_internal_flags_are_ordinary_unknown_cli_and_env_has_no_hook_role(tmp_path: Path) -> None:
    env = {**os.environ, "OKF_CAPTURE_INTERNAL_REEXEC_GUARD": "forged",
           "OKF_CAPTURE_INTERNAL_TEST_HOOK": str(tmp_path / "hook.py"),
           "OKF_CAPTURE_INTENDED_IDENTITY": "forged", "PYTEST_CURRENT_TEST": "forged"}
    public = subprocess.run(
        [sys.executable, str(ROOT / "scripts/capture_ui_overhaul.py"), "--manifest-only"],
        cwd=ROOT, text=True, capture_output=True, env=env, check=True,
    )
    assert json.loads(public.stdout)["name"] == "okf-loom-v2-ui-overhaul-final"
    for flag in ("--internal-clone-capability-fd", "--internal-test-mode", "--reexec-guard"):
        rejected = subprocess.run(
            [sys.executable, str(ROOT / "scripts/capture_ui_overhaul.py"), flag, "9", "--manifest-only"],
            cwd=ROOT, text=True, capture_output=True, env=env,
        )
        assert rejected.returncode == 2
        assert "unrecognized arguments" in rejected.stderr


def test_all_acceptance_labels_have_one_executable_oracle_with_rejecting_mutation() -> None:
    assert set(capture.ACCEPTANCE_ORACLE_REGISTRY) == capture._ACCEPTANCE_CRITERIA
    for label, oracle in capture.ACCEPTANCE_ORACLE_REGISTRY.items():
        passing, rejecting = capture._oracle_contract_examples(label)
        assert oracle(passing) is True, label
        assert oracle(rejecting) is False, label


def test_studio_retry_capture_releases_held_first_completion_only_after_canonical_settle() -> None:
    sequence: list[str] = []
    canonical = {"commentsStatus": "ready", "commentsReady": True, "eventsReady": True,
                 "conceptContentRev": "revision-2", "commentIds": ["new"], "eventIds": ["event-2"],
                 "focusedRole": "complementary", "focusedName": "Comments", "panelText": "New canonical comment",
                 "alertAbsent": True}
    class Locator:
        @property
        def first(self): return self
        def click(self, **_kwargs): sequence.append("retry-click")
        def wait_for(self, **_kwargs): sequence.append("retry-visible")
        def focus(self): sequence.append("retry-focus")
    class Page:
        def get_by_role(self, role, **_kwargs): return Locator()
        def wait_for_function(self, script, **_kwargs):
            if "__okfStudioRetryLifecycle?.held" in script: sequence.append("first-held")
            if "commentsReady&&S.state.eventsReady" in script: sequence.append("canonical-settled")
            if "fetchContinuationCompleted&&L.loadCompleted" in script: sequence.append("stale-completed")
        def evaluate(self, script, *args):
            if "window.__okfStudioRetryLifecycle={held:false" in script: sequence.append("start-held-load")
            if "window.__okfStudioRetryRelease()" in script: sequence.append("release-stale")
            if "const S=window.okfLoomStudio,a=document.activeElement" in script:
                sequence.append("observe")
                if "persisted:window.__okfStudioRetryCanonicalEvidence" in script:
                    return {"state": dict(canonical), "persisted": dict(canonical),
                            "lifecycle": {"fetchContinuationCompleted": True, "loadCompleted": True}}
                return dict(canonical)
            return None
    evidence = capture._prepare_studio_retry_stale_fence(Page())
    assert sequence.index("canonical-settled") < sequence.index("release-stale") < sequence.index("stale-completed")
    assert sequence.count("observe") == 2
    assert evidence["staleFenced"] is True and evidence["staleLoadCompleted"] is True
    assert evidence["conceptContentRev"] == "revision-2"


@pytest.mark.browser
def test_real_studio_retry_holds_stale_request_until_canonical_success_then_fences_completion(tmp_path) -> None:
    from browser_support import playwright_api, launch_chromium, copy_bundle, live_server
    playwright = playwright_api()

    bundle = copy_bundle(ROOT / "samples/demo_bundle", tmp_path / "bundle")
    with live_server(bundle) as base:
        with playwright.sync_playwright() as owner:
            browser = launch_chromium(owner)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.goto(base + "/tables/orders", wait_until="domcontentloaded")
                page.wait_for_function(
                    "() => window.okfLoomStudio?.state?.commentsReady && window.okfLoomStudio.state.eventsReady",
                    timeout=10_000,
                )
                evidence = capture._prepare_studio_retry_stale_fence(page)
                persisted = page.evaluate("() => window.__okfStudioRetryCanonicalEvidence")
                observed = page.evaluate("""() => { const S=window.okfLoomStudio,a=document.activeElement;
                  return {commentsStatus:S.state.commentsStatus,commentsReady:S.state.commentsReady,
                    eventsReady:S.state.eventsReady,conceptContentRev:S.state.doc.concept_content_rev,
                    commentIds:S.state.comments.map(c=>c.id),eventIds:S.state.events.map(e=>e.event_id||e.id),
                    focusedRole:a&&a.getAttribute('role'),focusedName:a&&(a.getAttribute('aria-label')||a.textContent||'').trim(),
                    panelText:document.querySelector('.okf-panel')&&document.querySelector('.okf-panel').innerText,
                    alertAbsent:!document.querySelector('[role=alert] .okf-studiobtn')}; }""")
                assert evidence["errorProjection"] == "manual-independent-while-request-held"
                assert evidence["staleFetchContinuationCompleted"] and evidence["staleLoadCompleted"]
                assert evidence["staleFenced"] and observed == persisted
            finally:
                browser.close()


def test_system_capture_matrix_uses_only_the_new_storage_choice():
    rows = [spec for spec in capture.SCENARIO_SPECS if spec.theme_preference == "system"]
    assert len(rows) == 8
    for spec in rows:
        script = capture._theme_seed_script(spec)
        assert "okf-loom.appearance" in script and '"system"' in script
        assert "okf-theme-family" not in script and "okf-theme-mode" not in script
        assert spec.resolved_theme == ("slate" if spec.color_scheme == "dark" else "bone")
