from __future__ import annotations

import importlib
import multiprocessing
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
capture = importlib.import_module("capture_viewer_proof")
media = importlib.import_module("capture_readme_media")


def assert_no_capture_tmp(path: Path):
    assert not list(path.glob("*.capture-tmp.*"))


class FakeLocator:
    def __init__(self, page):
        self.page = page

    def screenshot(self, **kwargs):
        self.page._write(**kwargs)


class FakePage:
    def __init__(self, result=None, screenshot_error=None, *, goto_error=None,
                 evaluate_hang=None, screenshot_hang=False):
        self.result = result or {
            "ok": True,
            "generation": 7,
            "screenshotAllowed": True,
            "metadata": {"revision": "rev-1", "readinessPhase": "ready"},
        }
        self.screenshot_error = screenshot_error
        self.goto_error = goto_error
        self.evaluate_hang = evaluate_hang
        self.screenshot_hang = screenshot_hang
        self.goto_calls = []
        self.readiness_args = None
        self.readiness_script = None
        self.screenshot_kwargs = None
        self.abort_calls = 0
        self.lease_acquires = 0
        self.lease_releases = []

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        if self.goto_error:
            raise self.goto_error

    def evaluate(self, script, arg=None):
        if "__okfCaptureConsumerAbort; if(c) c.abort" in script:
            self.abort_calls += 1
            return None
        if "okfCaptureReadiness.start" in script:
            self.readiness_script = script
            self.readiness_args = arg
        if self.evaluate_hang and self.evaluate_hang in script:
            time.sleep(1)
        if "okfCaptureReadiness.start" in script:
            return self.result
        if "acquireCaptureLease" in script:
            self.lease_acquires += 1
            return f"lease-{self.lease_acquires}"
        if "releaseCaptureLease" in script:
            self.lease_releases.append(arg)
            return True
        return None

    def _write(self, path, **kwargs):
        self.screenshot_kwargs = {"path": path, **kwargs}
        if self.screenshot_error:
            raise self.screenshot_error
        if self.screenshot_hang:
            time.sleep(1)
        Path(path).write_bytes(b"png")

    def screenshot(self, **kwargs):
        self._write(**kwargs)

    def locator(self, _selector):
        return FakeLocator(self)


def test_capture_success_uses_production_owner_and_atomic_artifact(tmp_path):
    page = FakePage()
    out = tmp_path / "proof.png"

    result = capture.capture_target(page, "http://viewer/__graph", "graph", out)

    assert result["generation"] == 7
    assert out.read_bytes() == b"png"
    assert page.readiness_args[0] == {"target": "graph"}
    assert 0 < page.readiness_args[1] <= 30_000
    assert page.goto_calls[0][1]["wait_until"] == "domcontentloaded"
    assert page.screenshot_kwargs["type"] == "png"
    assert page.screenshot_kwargs["path"].endswith(".capture-tmp.png")
    assert "/.proof." in page.screenshot_kwargs["path"]
    assert page.lease_acquires == 1 and page.lease_releases == ["lease-1"]


def test_stable_runtime_error_is_preserved_and_writes_no_artifact(tmp_path):
    page = FakePage({
        "ok": False, "code": "revision-mismatch", "generation": 11,
        "screenshotAllowed": False, "message": "wrong revision",
        "metadata": {"revision": "actual", "readinessPhase": "revision-mismatch"},
    })
    out = tmp_path / "proof.png"

    with pytest.raises(capture.CaptureFailure) as caught:
        capture.capture_target(page, "http://viewer/concept", "document", out,
                               expected_revision="wanted")

    assert caught.value.diagnostic["code"] == "revision-mismatch"
    assert caught.value.diagnostic["generation"] == 11
    assert caught.value.diagnostic["revision"] == "actual"
    assert not out.exists()


def test_screenshot_allowed_false_fails_closed(tmp_path):
    page = FakePage({"ok": True, "generation": 2, "screenshotAllowed": False,
                     "metadata": {"readinessPhase": "ready"}})
    out = tmp_path / "proof.png"
    with pytest.raises(capture.CaptureFailure) as caught:
        capture.capture_target(page, "http://viewer/", "document", out)
    assert caught.value.diagnostic["phase"] == "readiness"
    assert not out.exists()


def test_remaining_budget_is_passed_to_runtime_and_timeout_is_terminal(tmp_path):
    page = FakePage({"ok": False, "code": "timeout", "generation": 3,
                     "screenshotAllowed": False,
                     "metadata": {"readinessPhase": "consumer-timeout"}})
    with pytest.raises(capture.CaptureFailure) as caught:
        capture.capture_target(page, "http://viewer/search", "search", tmp_path / "x.png")
    assert 0 < page.readiness_args[1] <= 30_000
    assert caught.value.diagnostic["code"] == "timeout"
    assert page.abort_calls == 1
    assert page.lease_acquires == 0 and page.lease_releases == []


def test_screenshot_write_failure_is_distinct_and_cleans_partial_files(tmp_path):
    page = FakePage(screenshot_error=OSError("disk full"))
    out = tmp_path / "proof.png"
    out.write_bytes(b"stale")

    with pytest.raises(capture.CaptureFailure) as caught:
        capture.capture_target(page, "http://viewer/", "document", out)

    assert caught.value.diagnostic["code"] == "screenshot-write-failure"
    assert caught.value.diagnostic["phase"] == "screenshot"
    assert out.read_bytes() == b"stale"
    assert_no_capture_tmp(tmp_path)
    assert page.lease_acquires == 1 and page.lease_releases == ["lease-1"]


def test_capture_retry_gets_fresh_edge_flow_lease_and_releases_each_attempt(tmp_path):
    page = FakePage(screenshot_error=OSError("first failure"))
    with pytest.raises(capture.CaptureFailure):
        capture.capture_target(page, "http://viewer/__graph", "graph", tmp_path / "proof.png")
    page.screenshot_error = None
    capture.capture_target(page, "http://viewer/__graph", "graph", tmp_path / "proof.png")
    assert page.lease_acquires == 2
    assert page.lease_releases == ["lease-1", "lease-2"]


@pytest.mark.parametrize("failure", ["navigation", "readiness", "write"])
def test_failed_recapture_preserves_existing_destination(tmp_path, failure):
    out = tmp_path / "proof.png"
    original = b"previous-valid-image"
    out.write_bytes(original)
    if failure == "navigation":
        page = FakePage(goto_error=RuntimeError("navigation failed"))
    elif failure == "readiness":
        page = FakePage({"ok": False, "code": "hidden", "generation": 4,
                         "screenshotAllowed": False, "metadata": {}})
    else:
        page = FakePage(screenshot_error=OSError("disk full"))
    with pytest.raises(capture.CaptureFailure):
        capture.capture_target(page, "http://viewer/", "document", out)
    assert out.read_bytes() == original
    assert_no_capture_tmp(tmp_path)


def test_prepare_hang_obeys_total_deadline_and_aborts(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "_CAPTURE_BUDGET_MS", 25)
    page = FakePage()
    with pytest.raises(capture.CaptureFailure) as caught:
        capture.capture_target(page, "http://viewer/", "document", tmp_path / "x.png",
                               prepare=lambda _page: time.sleep(1))
    assert caught.value.diagnostic["phase"] == "prepare"
    assert caught.value.diagnostic["code"] == "timeout"
    assert page.abort_calls == 1


def test_frozen_raf_obeys_deadline_and_leaves_no_temp(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "_CAPTURE_BUDGET_MS", 25)
    page = FakePage(evaluate_hang="requestAnimationFrame")
    out = tmp_path / "x.png"
    with pytest.raises(capture.CaptureFailure) as caught:
        capture.capture_target(page, "http://viewer/", "document", out)
    assert caught.value.diagnostic["phase"] == "paint-boundary"
    assert page.abort_calls == 1
    assert not out.exists()
    assert_no_capture_tmp(tmp_path)


def test_late_readiness_is_aborted_by_consumer_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "_CAPTURE_BUDGET_MS", 25)
    page = FakePage(evaluate_hang="okfCaptureReadiness.start")
    with pytest.raises(capture.CaptureFailure) as caught:
        capture.capture_target(page, "http://viewer/", "document", tmp_path / "x.png")
    assert caught.value.diagnostic["phase"] == "readiness"
    assert page.abort_calls == 1
    assert "options.signal = controller.signal" in page.readiness_script


def test_screenshot_hang_times_out_preserving_destination(tmp_path, monkeypatch):
    # Leave headroom for real destination-authority fsync before the injected
    # one-second screenshot stall. The production shared deadline is unchanged.
    monkeypatch.setattr(capture, "_CAPTURE_BUDGET_MS", 250)
    page = FakePage(screenshot_hang=True)
    out = tmp_path / "x.png"
    out.write_bytes(b"old")
    with pytest.raises(capture.CaptureFailure) as caught:
        capture.capture_target(page, "http://viewer/", "document", out)
    assert caught.value.diagnostic["phase"] == "screenshot"
    assert caught.value.diagnostic["code"] == "timeout"
    assert out.read_bytes() == b"old"
    assert_no_capture_tmp(tmp_path)


def test_jpeg_capture_preserves_mime_suffix_and_output_name(tmp_path):
    page = FakePage()
    out = tmp_path / "proof.jpeg"

    capture.capture_target(page, "http://viewer/", "document", out)

    assert out.read_bytes() == b"png"
    assert page.screenshot_kwargs["type"] == "jpeg"
    assert page.screenshot_kwargs["path"].endswith(".capture-tmp.jpeg")
    assert "/.proof." in page.screenshot_kwargs["path"]
    assert_no_capture_tmp(tmp_path)


def _capture_error(errors, page, out):
    try:
        capture.capture_target(page, "http://viewer/", "document", out)
    except capture.CaptureFailure as exc:
        errors.append(exc)


def test_run_phase_preserves_outer_alarm_handler_and_remaining_timer():
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal timer proof requires main thread")
    fired = []
    def outer(_signum, _frame):
        fired.append(True)
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, outer)
    signal.setitimer(signal.ITIMER_REAL, 0.25)
    try:
        capture._run_phase(lambda: time.sleep(0.02), 100)
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        assert signal.getsignal(signal.SIGALRM) is outer
        assert 0.15 < remaining < 0.25
        assert interval == 0
        assert not fired
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_run_phase_restores_periodic_outer_alarm_after_it_fires():
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal timer proof requires main thread")
    fired = []
    def outer(_signum, _frame):
        fired.append(time.monotonic())
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, outer)
    signal.setitimer(signal.ITIMER_REAL, 0.02, 0.02)
    try:
        capture._run_phase(lambda: time.sleep(0.05), 200)
        count_after_phase = len(fired)
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        assert count_after_phase >= 1
        assert 0 < remaining <= 0.02
        assert interval == pytest.approx(0.02, abs=0.005)
        time.sleep(0.05)
        assert len(fired) > count_after_phase
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_run_phase_preserves_sig_ign_disposition():
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
    signal.setitimer(signal.ITIMER_REAL, 0.01)
    try:
        began = time.monotonic()
        assert capture._run_phase(lambda: (time.sleep(0.06), "ok")[1], 200) == "ok"
        assert time.monotonic() - began >= 0.05
        assert signal.getsignal(signal.SIGALRM) == signal.SIG_IGN
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        assert remaining == 0
        assert interval == 0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_run_phase_preserves_ignored_periodic_outer_schedule():
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
    signal.setitimer(signal.ITIMER_REAL, 0.01, 0.02)
    try:
        assert capture._run_phase(lambda: (time.sleep(0.07), "ok")[1], 250) == "ok"
        assert signal.getsignal(signal.SIGALRM) == signal.SIG_IGN
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        assert 0 < remaining <= 0.02
        assert interval == pytest.approx(0.02, abs=0.003)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_ignored_outer_alarm_does_not_mask_capture_deadline():
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
    signal.setitimer(signal.ITIMER_REAL, 0.01, 0.01)
    began = time.monotonic()
    try:
        with pytest.raises(capture._PhaseTimeout):
            capture._run_phase(lambda: time.sleep(0.2), 50)
        assert 0.03 <= time.monotonic() - began < 0.15
        assert signal.getsignal(signal.SIGALRM) == signal.SIG_IGN
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_run_phase_preserves_sig_dfl_in_subprocess():
    code = (
        "import sys,signal,time; sys.path.insert(0,'scripts'); "
        "import capture_viewer_proof as c; "
        "signal.signal(signal.SIGALRM, signal.SIG_DFL); "
        "signal.setitimer(signal.ITIMER_REAL,.02); "
        "c._run_phase(lambda: time.sleep(.2),500)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=SCRIPTS.parent,
                            timeout=3)
    assert result.returncode == -signal.SIGALRM


def test_off_main_capture_is_rejected_without_worker_or_artifact(tmp_path):
    out = tmp_path / "proof.png"
    out.write_bytes(b"valid-old")
    errors = []
    def invoke():
        _capture_error(errors, FakePage(), out)
    caller = threading.Thread(target=invoke)
    caller.start()
    caller.join(1)
    assert not caller.is_alive()
    assert errors[0].diagnostic["code"] == "thread-ownership"
    assert errors[0].diagnostic["phase"] == "ownership"
    assert out.read_bytes() == b"valid-old"
    assert_no_capture_tmp(tmp_path)


@pytest.mark.browser
def test_real_playwright_page_is_rejected_from_embedded_worker_thread(tmp_path):
    from browser_support import playwright_api, launch_chromium
    playwright = playwright_api()
    with playwright.sync_playwright() as owner:
        browser = launch_chromium(owner)
        try:
            page = browser.new_page()
            errors = []
            worker = threading.Thread(target=lambda: _capture_error(
                errors, page, tmp_path / "real.png"))
            worker.start(); worker.join(2)
            assert not worker.is_alive()
            assert errors[0].diagnostic["code"] == "thread-ownership"
            assert not (tmp_path / "real.png").exists()
        finally:
            browser.close()


def test_permanently_blocked_off_main_phase_cannot_keep_process_alive():
    code = (
        "import sys,threading,time; sys.path.insert(0,'scripts'); "
        "import capture_viewer_proof as c; "
        "t=threading.Thread(target=lambda: _call(c), daemon=False); "
        "exec('def _call(c):\\n try: c._run_phase(lambda: time.sleep(99),10)\\n except RuntimeError: pass'); "
        "t.start(); t.join(1); print('exited', not t.is_alive())"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=SCRIPTS.parent,
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    assert "exited True" in result.stdout


def _process_publish(path_text, payload, claimed, proceed, results):
    path = Path(path_text)
    claim = capture._claim_destination(path)
    tmp = path.parent / f".{payload.decode()}.{claim}.capture-tmp{path.suffix}"
    tmp.write_bytes(payload)
    claimed.put(claim)
    proceed.wait(5)
    results.put(capture._publish_if_current(tmp, path, claim))
    tmp.unlink(missing_ok=True)


@pytest.mark.parametrize("name", ["proof.png", "comment-loop.gif"])
def test_cross_process_newest_claim_fences_late_finisher(tmp_path, name):
    ctx = multiprocessing.get_context("fork")
    out = tmp_path / name
    out.write_bytes(b"initial")
    claimed = ctx.Queue(); results = ctx.Queue()
    go_old = ctx.Event(); go_new = ctx.Event()
    old = ctx.Process(target=_process_publish,
                      args=(str(out), b"old", claimed, go_old, results))
    old.start(); old_claim = claimed.get(timeout=3)
    new = ctx.Process(target=_process_publish,
                      args=(str(out), b"new", claimed, go_new, results))
    new.start(); new_claim = claimed.get(timeout=3)
    assert new_claim > old_claim
    go_new.set(); assert results.get(timeout=3) is True
    go_old.set(); assert results.get(timeout=3) is False
    old.join(3); new.join(3)
    assert old.exitcode == new.exitcode == 0
    assert out.read_bytes() == b"new"
    assert_no_capture_tmp(tmp_path)


def test_readme_media_serves_only_git_tracked_bundle_files(tmp_path):
    """Ignored session state in a working checkout must never reach README media."""
    repo = tmp_path / "repo"
    bundle = repo / "docs-bundle"
    (bundle / "concept").mkdir(parents=True)
    (bundle / "index.md").write_text("# Index\n")
    (bundle / "concept" / "a.md").write_text("---\ntype: Explanation\n---\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "docs-bundle"], check=True)
    (bundle / ".okf-loom").mkdir()
    (bundle / ".okf-loom" / "comments.jsonl").write_text("{}\n")
    (bundle / "concept" / "a.md.lock").write_text("")

    snapshot = media._tracked_bundle_snapshot(bundle, tmp_path / "out")

    assert snapshot == tmp_path / "out" / "docs-bundle"
    copied = sorted(p.relative_to(snapshot).as_posix()
                    for p in snapshot.rglob("*") if p.is_file())
    assert copied == ["concept/a.md", "index.md"]


def test_readme_stills_publish_nothing_when_the_browser_reported_a_fault(tmp_path, monkeypatch):
    (tmp_path / "graph-map.png").write_bytes(b"previous")

    def stage_with_fault(_browser, _base, stage, diagnostics):
        (stage / "graph-map.png").write_bytes(b"new")
        diagnostics.failures.append("graph-map: console error: boom")
        return ["graph-map.png"]

    monkeypatch.setattr(media, "_stage_stills", stage_with_fault)
    with pytest.raises(RuntimeError, match="console error: boom"):
        media._capture_stills(None, "http://unused", tmp_path, media._BrowserDiagnostics())
    assert (tmp_path / "graph-map.png").read_bytes() == b"previous"
    assert not list(tmp_path.glob(".okf-media-stills-*"))


def test_readme_stills_publish_the_whole_set_world_readable(tmp_path, monkeypatch):
    def stage_private(_browser, _base, stage, _diagnostics):
        for name in ("studio-home.png", "social-preview.png"):
            (stage / name).write_bytes(name.encode())
            os.chmod(stage / name, 0o600)
        return ["studio-home.png", "social-preview.png"]

    monkeypatch.setattr(media, "_stage_stills", stage_private)
    published = media._capture_stills(None, "http://unused", tmp_path, media._BrowserDiagnostics())
    assert [p.name for p in published] == ["studio-home.png", "social-preview.png"]
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o644 for p in published)
    assert (tmp_path / "social-preview.png").read_bytes() == b"social-preview.png"
    assert not list(tmp_path.glob(".okf-media-stills-*"))


class _FakeProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


@pytest.mark.parametrize("server_module", [capture, media])
def test_live_server_enter_failure_terminates_started_process(monkeypatch, tmp_path, server_module):
    proc = _FakeProcess()
    monkeypatch.setattr(server_module.subprocess, "Popen", lambda *_a, **_k: proc)
    monkeypatch.setattr(server_module, "_wait_for_http_ok", lambda *_a, **_k: False)
    server = server_module._LiveServer(tmp_path)
    with pytest.raises(RuntimeError, match="did not become ready"):
        server.__enter__()
    assert proc.terminated
    assert server._proc is None


def test_static_server_enter_failure_closes_socket_and_thread(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "_wait_for_http_ok", lambda *_a, **_k: False)
    server = capture._StaticServer(tmp_path)
    with pytest.raises(RuntimeError, match="did not become ready"):
        server.__enter__()
    assert server._server is None
    assert server._thread is None


def test_all_shipped_helpers_route_capture_through_production_consumer():
    readme = (SCRIPTS / "capture_readme_media.py").read_text()
    signals = (SCRIPTS / "capture_signal_controls.py").read_text()
    viewer = (SCRIPTS / "capture_viewer_proof.py").read_text()
    assert "okfCaptureReadiness.start" in viewer
    assert "capture_target, launch_chromium" in readme
    assert "from capture_viewer_proof import capture_target" in signals
    assert "__okfLoomGraph" not in signals


def test_capture_consumers_use_canonical_dark_theme_setup():
    """Capture adapters must not use the retired click-to-cycle control."""
    capture_sources = [p.read_text() for p in (SCRIPTS / "").glob("capture*.py")]
    assert all('click("#okf-theme")' not in source for source in capture_sources)

    signals = (SCRIPTS / "capture_signal_controls.py").read_text()
    assert 'window.OKFLoomTheme.setTheme("technical-dark")' in signals
    assert 'document.documentElement.dataset.theme' in signals
    assert '== "technical-dark"' in signals
    assert 'locator("#okf-appearance-menu").is_hidden()' in signals
    assert signals.index('== "technical-dark"') < signals.index('shot(page, "06-overview-dark-fullpage.png"')


class FakeChromium:
    def __init__(self, succeeds_on=None):
        self.calls = []
        self.succeeds_on = succeeds_on

    def launch(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs == self.succeeds_on:
            return "browser"
        raise RuntimeError("not installed")


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium


def test_chrome_resolution_precedence_explicit_then_environment(monkeypatch):
    monkeypatch.setenv("OKF_CHROME", "/env/chrome")
    monkeypatch.setenv("AIC_PLAYWRIGHT_CHROME_PATH", "/aic/chrome")
    chromium = FakeChromium({"executable_path": "/env/chrome"})

    assert capture.launch_chromium(FakePlaywright(chromium), "/explicit/chrome") == "browser"
    assert chromium.calls == [
        {"executable_path": "/explicit/chrome"},
        {"executable_path": "/env/chrome"},
    ]


def test_chrome_resolution_reports_every_unavailable_attempt(monkeypatch):
    monkeypatch.setenv("OKF_CHROME", "/missing/env-chrome")
    monkeypatch.delenv("AIC_PLAYWRIGHT_CHROME_PATH", raising=False)
    monkeypatch.setattr(capture, "_SYSTEM_CHROME_PATHS", ())
    chromium = FakeChromium()

    with pytest.raises(RuntimeError) as caught:
        capture.launch_chromium(FakePlaywright(chromium), "/missing/explicit")

    message = str(caught.value)
    assert "--chrome (/missing/explicit)" in message
    assert "OKF_CHROME (/missing/env-chrome)" in message
    assert "chrome channel (chrome)" in message
    assert "Playwright-managed Chromium (managed chromium)" in message


def test_readme_media_scripts_never_write_appearance_storage():
    """theme.js is the one owner of appearance state. The README media scripts
    show the shipped defaults (stills) or change appearance through the real
    menu (showcase clips), so neither may seed the canonical
    ``okf-theme-family`` / ``okf-theme-mode`` keys nor the legacy ``okf-theme``
    key that theme.js keeps for migration only."""
    for name in ("capture_readme_media.py", "record_showcase_media.py"):
        src = (SCRIPTS / name).read_text()
        assert "localStorage" not in src, f"{name} touches localStorage"
        assert "setItem('okf-theme" not in src and 'setItem("okf-theme' not in src, (
            f"{name} writes appearance storage directly")
