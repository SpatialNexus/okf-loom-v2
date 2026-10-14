"""Lazy optional browser tools and owned, diagnostic local test servers."""
from contextlib import contextmanager
import importlib
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

from conftest import TOOLKIT_ROOT, active_test_item, okf_module_argv, okf_subprocess_env


def unavailable(reason):
    if os.environ.get("OKF_REQUIRE_BROWSER") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


def playwright_api():
    if importlib.util.find_spec("playwright") is None:
        unavailable("Playwright is not installed; see CONTRIBUTING.md developer tests")
    # A broken installation is an error, not an absent optional dependency.
    return importlib.import_module("playwright.sync_api")


def require_browser_marker(entry):
    """Fail the running test when it starts a browser without the browser marker.

    Lane selection trusts the marker: an unmarked browser start runs (or
    skips) in the core lane and is deselected from the browser lane. Outside
    a pytest test protocol, for example in capture scripts, this does nothing.
    """
    item = active_test_item()
    if item is None or item.get_closest_marker("browser"):
        return
    pytest.fail(
        f"{item.nodeid} starts a browser through browser_support.{entry} without "
        "the browser marker, so -m browser deselects it and the core lane runs it. "
        "Mark the test with @pytest.mark.browser (or its module with "
        "pytestmark = pytest.mark.browser when every test there starts a browser)."
    )


def _is_real_playwright(owner):
    """True for a live Playwright driver; synthetic owners cannot start Chromium."""
    api = sys.modules.get("playwright.sync_api")
    return api is not None and isinstance(owner, api.Playwright)


def sync_playwright():
    require_browser_marker("sync_playwright")
    return playwright_api().sync_playwright()


def expect(*args, **kwargs):
    return playwright_api().expect(*args, **kwargs)


def reveal_control(page, control):
    """Reach an optional control through its real pane and native disclosures.

    This does not force visibility or mutate application state. Callers still
    assert the control's behavior after navigating the approved hierarchy.
    """
    control.wait_for(state="attached")
    if control.evaluate("el => !!el.closest('#okf-detail')"):
        details = page.get_by_role("navigation", name="Graph panes").get_by_role("button", name="Details", exact=True)
        if details.is_visible() and details.get_attribute("aria-pressed") != "true":
            details.click()
    ancestors = control.locator("xpath=ancestor::details").all()
    for owner in ancestors:
        if not owner.evaluate("el => el.open"):
            owner.locator(":scope > summary").click()
    return control


def show_graph_canvas(page):
    """Choose the optional canvas before making any plot geometry assertions."""
    page.wait_for_function("() => !!window.__okfLoomGraph?.cy")
    if page.locator('body.okf-viewer--singlefile').count():
        page.locator('[data-okf-shell-item="graph"] a').click()
    pane = page.get_by_role('navigation', name='Graph panes').get_by_role('button', name='Graph', exact=True)
    if pane.is_visible() and pane.get_attribute('aria-pressed') != 'true':
        pane.click()
    page.locator('#okf-graph canvas').first.wait_for(state='visible')
    if page.viewport_size and page.viewport_size['height'] <= 640:
        page.locator('#okf-graph').scroll_into_view_if_needed()
    page.wait_for_function("""() => {const cy=window.__okfLoomGraph.cy,c=document.getElementById('okf-graph');
        return Math.abs(cy.width()-c.clientWidth)<2&&Math.abs(cy.height()-c.clientHeight)<2;}""")


def launch_chromium(owner):
    """Choose an installed executable before launch; never swallow launch errors."""
    if _is_real_playwright(owner):
        require_browser_marker("launch_chromium")
    options = {"headless": False} if os.environ.get("OKF_CAPTURE_REAL_BROWSER_ZOOM") == "1" else {}
    explicit = os.environ.get("OKF_CHROME") or os.environ.get("AIC_PLAYWRIGHT_CHROME_PATH")
    if options.get("headless") is False:
        return _launch_owned_headed_chrome(owner, explicit or shutil.which("google-chrome"))
    if explicit:
        if not Path(explicit).is_file():
            pytest.fail(f"Configured browser does not exist: {explicit}")
        return owner.chromium.launch(executable_path=explicit, **options)
    if Path(owner.chromium.executable_path).is_file():
        return owner.chromium.launch(**options)
    for executable in ("google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(executable)
        if path:
            return owner.chromium.launch(executable_path=path, **options)
    unavailable("Chromium is not installed; run python -m playwright install chromium")


def _signal_owned_group(pgid, sig):
    """Signal one owned process group, tolerating an already-empty group."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass  # every group member already exited


def _live_group_writers(pgid):
    """Return live process-space members of the owned group.

    A zombie holds no file descriptors and cannot write, so zombies are not
    writers; only running/sleeping group members block profile deletion.
    """
    writers = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                raw = handle.read()
        except OSError:
            continue  # exited between listing and reading
        try:
            # comm may contain spaces and parens; split after the final ')'.
            fields = raw[raw.rindex(b")") + 2:].split()
            if int(fields[2]) != pgid or fields[0] == b"Z":
                continue
        except (ValueError, IndexError):
            continue
        writers.append(int(entry))
    return writers


def _wait_group_quiescent(pgid, timeout):
    """Bounded wait until the owned group has no live writer left."""
    deadline = time.monotonic() + timeout
    while True:
        writers = _live_group_writers(pgid)
        if not writers or time.monotonic() >= deadline:
            return writers
        time.sleep(0.01)


def _stop_owned_browser_group(process, profile, log, *,
                              term_timeout=5.0, kill_timeout=5.0):
    """Stop the owned browser process group, then delete its profile.

    The leader is started with ``start_new_session=True``, so its pid owns
    the group and group signals can never reach the test runner. Chrome
    descendants may outlive the leader and keep writing the profile, so
    cleanup stops the whole group even when the leader already exited,
    escalates TERM → KILL within bounded waits, reaps the direct child, and
    deletes the profile only after no live writer remains. If quiescence
    cannot be proven the profile is retained and the failure is reported;
    deletion failures are surfaced once, never retried or suppressed.

    ``profile`` is a manually owned directory path (``tempfile.mkdtemp``).
    Its whole lifetime is the explicit ``shutil.rmtree`` below: nothing
    removes it automatically, so a retained profile survives both release
    of every reference and interpreter exit until an operator deletes it.
    """
    errors: list[str] = []
    escalated = False
    pgid = process.pid
    _signal_owned_group(pgid, signal.SIGTERM)
    try:
        process.wait(timeout=term_timeout)
    except subprocess.TimeoutExpired:
        errors.append(f"owned leader survived TERM for {term_timeout}s")
    if _wait_group_quiescent(pgid, term_timeout):
        escalated = True
        _signal_owned_group(pgid, signal.SIGKILL)
        try:
            process.wait(timeout=kill_timeout)
        except subprocess.TimeoutExpired:
            errors.append(f"owned leader survived KILL for {kill_timeout}s")
        writers = _wait_group_quiescent(pgid, kill_timeout)
    else:
        writers = []
    if writers:
        errors.append(
            f"owned group {pgid} still has live writers {writers} after "
            f"TERM and KILL; profile retained for diagnosis at {profile}")
        log.close()
        return {"ok": False, "errors": errors, "escalated": escalated,
                "removed": False, "exitcode": process.poll()}
    log.close()
    try:
        shutil.rmtree(profile)
    except OSError as exc:
        errors.append(f"profile cleanup failed: {exc}")
        return {"ok": False, "errors": errors, "escalated": escalated,
                "removed": False, "exitcode": process.poll()}
    return {"ok": True, "errors": errors, "escalated": escalated,
            "removed": True, "exitcode": process.returncode}


def _owned_profile_dir():
    """Allocate the headed-browser profile directory with explicit lifetime.

    A plain ``tempfile.mkdtemp`` path, deliberately NOT a
    ``TemporaryDirectory``: it carries no automatic finalizer, so a profile
    retained for diagnosis (unproven writer quiescence) survives both
    release of every reference and interpreter exit. Its only removal is
    the proven-quiescent ``shutil.rmtree`` in ``_stop_owned_browser_group``.
    """
    return tempfile.mkdtemp(prefix="okf-headed-", dir="/tmp")


def _launch_owned_headed_chrome(owner, executable):
    """Start the permitted system Chrome, connect over loopback CDP, own cleanup.

    AIC's older Playwright launch-options adapter drops headed options with
    current Playwright. Direct system-Chrome startup avoids that API mismatch
    without disabling its executable policy or faking page zoom.
    """
    if _is_real_playwright(owner):
        require_browser_marker("_launch_owned_headed_chrome")
    if not executable or not os.environ.get("DISPLAY"):
        pytest.fail("Real browser zoom requires system Chrome and DISPLAY (for example Xvfb)")
    profile = _owned_profile_dir()
    port = free_port()
    log = open(Path(profile) / "chrome.log", "w+")
    # start_new_session gives the leader its own process group (pgid == pid)
    # BEFORE any group signal exists: Chrome otherwise shares this caller's
    # group, where a group signal would hit the test runner itself.
    try:
        process = subprocess.Popen([executable, "--no-sandbox", "--disable-dev-shm-usage",
            "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
            "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "about:blank"], stdout=log, stderr=log,
            env={**os.environ, "TMPDIR": "/tmp", "TMP": "/tmp", "TEMP": "/tmp"},
            start_new_session=True)
    except BaseException:
        # Popen failure means no browser process exists, so the freshly
        # made profile has no writers; remove it without masking the
        # launch error. The shared cleanup below owns every post-spawn path.
        shutil.rmtree(profile, ignore_errors=True)
        log.close()
        raise
    try:
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None or time.monotonic() >= deadline:
                log.seek(0)
                raise RuntimeError("Headed Chrome did not start: " + log.read()[-4000:])
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=.5):
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(.05)
        browser = owner.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except BaseException as startup_error:
        # The same owned-group cleanup as the normal path: a failed startup
        # still leaves descendants that may write the profile. The original
        # startup error stays primary; a cleanup failure is chained, never
        # swapped in or suppressed.
        outcome = _stop_owned_browser_group(process, profile, log)
        if outcome["ok"]:
            raise
        raise RuntimeError(
            "Headed Chrome startup cleanup failed: " + "; ".join(outcome["errors"])
        ) from startup_error

    class OwnedBrowser:
        def __getattr__(self, name):
            return getattr(browser, name)

        def close(self):
            browser_error = None
            try:
                browser.close()
            except BaseException as exc:  # re-raised below after cleanup
                browser_error = exc
            outcome = _stop_owned_browser_group(process, profile, log)
            cleanup_error = (
                RuntimeError("owned browser cleanup failed: " + "; ".join(outcome["errors"]))
                if not outcome["ok"] else None)
            if browser_error is not None:
                if cleanup_error is not None:
                    raise cleanup_error from browser_error
                raise browser_error
            if cleanup_error is not None:
                raise cleanup_error

    return OwnedBrowser()


def copy_bundle(source, destination):
    """Copy authored inputs without importing an operator's live session state."""
    return Path(shutil.copytree(source, destination, ignore=shutil.ignore_patterns(
        ".okf-loom", ".git", "__pycache__")))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PROBE_TIMEOUT = 5  # seconds per readiness request


def wait_for_server(proc, base, *, timeout=20, log_path=None):
    deadline = time.monotonic() + timeout
    reason = "startup deadline exceeded"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            reason = f"server exited before readiness (rc={proc.returncode})"
            break
        try:
            # A cold first render of a large bundle (docs-bundle) on a loaded
            # host can take several seconds; a 1s probe abandoned it every
            # time. Each probe may wait up to 5s, still bounded by the deadline.
            with urllib.request.urlopen(base + "/", timeout=min(PROBE_TIMEOUT, max(.01, deadline - time.monotonic()))) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(min(.05, max(0, deadline - time.monotonic())))
    # Logs belong to disposable test processes, not to an operator's session.
    diagnostic = Path(log_path).read_text(errors="replace")[-8000:] if log_path else "(no captured log)"
    pytest.fail(f"{reason}; {base}\n{diagnostic}")


@contextmanager
def managed_server(argv, base, log_path, *, timeout=20):
    with Path(log_path).open("w") as log:
        proc = subprocess.Popen(argv, cwd=TOOLKIT_ROOT, env=okf_subprocess_env(),
                                stdout=log, stderr=subprocess.STDOUT)
        try:
            wait_for_server(proc, base, timeout=timeout, log_path=log_path)
            yield base
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@contextmanager
def live_server(bundle, *flags):
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with managed_server(
        okf_module_argv("serve", str(bundle), "--host", "127.0.0.1", "--port",
                        str(port), "--no-open", *flags), base,
        Path(bundle).parent / f"server-{port}.log",
    ):
        yield base
