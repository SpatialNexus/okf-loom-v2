#!/usr/bin/env python3
"""Browser screenshot capture for the OKF viewer (current spec §17).

Renders the demo bundle BOTH live (``scripts/okf-loom serve``) AND as a static build
(``scripts/okf-loom build --target static``), then captures full-page screenshots of:

* the full-page Cytoscape graph view,
* a concept page (``tables/orders``),
* the search results page (query ``orders`` on the live server; the
  static build has no search backend, so we capture the empty-results
  page that documents the degraded state),

writing them under ``docs/screenshots/<YYYY-MM-DD>-viewer/``.

Requires explicit Playwright browser-proof dependencies + Chromium::

    python -m pip install playwright
    playwright install chromium
    python scripts/capture_viewer_proof.py [--bundle samples/demo_bundle] \\
        [--out-dir docs/screenshots]

Design notes
------------
* Import-safe: Playwright is imported lazily inside :func:`main`, so the
  module has no side effects on import (mirrors the pattern in
  ``scripts/build_skill_archive.py``).
* The live server is started on an ephemeral port via ``subprocess`` and
  is always torn down in a ``finally`` block (SIGTERM, then SIGKILL).
* The static build is served from a tmp dir via the stdlib
  ``http.server.ThreadingHTTPServer`` so the script does not depend on
  ``scripts/okf-loom serve`` for static artefacts.
* Readiness is polled, not slept on (no ``time.sleep`` busy-waits past
  the poll interval); the script exits non-zero if Playwright or the
  Chromium binary is unavailable, or if either server fails to start.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import hashlib
import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

__all__ = ["main"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Resolved repo root (the directory containing pyproject.toml). Lets the
# script run from any CWD.
_SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = _SCRIPT_DIR.parent
SCRIPT_PATH = REPO_ROOT / "scripts"
DEFAULT_BUNDLE = REPO_ROOT / "samples" / "demo_bundle"
DEFAULT_OUT_DIR = REPO_ROOT / "docs" / "screenshots"

# Live-server readiness budget (seconds). The OKF server boots in well
# under a second; this covers slow runners.
_SERVER_STARTUP_TIMEOUT = 20.0
_SERVER_POLL_INTERVAL = 0.15
_POLL_HTTP_TIMEOUT = 1.0

# Per-shot navigation + settle budget for Playwright.
_CAPTURE_BUDGET_MS = 30_000
_SYSTEM_CHROME_PATHS = (
    "/opt/google/chrome/google-chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)
_capture_authority_lock = threading.Lock()
_capture_authority: dict[str, object] = {}
_LOCK_ROOT = Path(tempfile.gettempdir()) / "okf-loom-capture-locks"


def _destination_state(path: Path) -> tuple[Path, Path]:
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    _LOCK_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _LOCK_ROOT / f"{digest}.lock", _LOCK_ROOT / f"{digest}.seq"


def _claim_destination(path: Path) -> int:
    lock_path, seq_path = _destination_state(path)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                current = int(seq_path.read_text())
            except (FileNotFoundError, ValueError):
                current = 0
            claimed = current + 1
            fd, name = tempfile.mkstemp(prefix=f".{seq_path.name}.", dir=seq_path.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write(str(claimed)); stream.flush(); os.fsync(stream.fileno())
                os.replace(name, seq_path)
            finally:
                Path(name).unlink(missing_ok=True)
            return claimed
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _publish_if_current(tmp: Path, destination: Path, claim: int) -> bool:
    lock_path, seq_path = _destination_state(destination)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                current = int(seq_path.read_text())
            except (FileNotFoundError, ValueError):
                return False
            if current != claim:
                return False
            os.replace(tmp, destination)
            return True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def launch_chromium(playwright, explicit: str | None = None, *, headed: bool | None = None):
    """Launch Chrome using stable checkout-wide precedence and diagnostics.

    ``headed`` is propagated by release publishers that own browser-chrome
    interactions. Environment inference remains for compatibility consumers.
    """
    if headed is None:
        headed = os.environ.get("OKF_CAPTURE_REAL_BROWSER_ZOOM") == "1"
    launch_mode = {"headless": False} if headed else {}
    candidates: list[tuple[str, dict]] = []
    if explicit:
        candidates.append(("--chrome", {"executable_path": explicit, **launch_mode}))
    for env_name in ("OKF_CHROME", "AIC_PLAYWRIGHT_CHROME_PATH"):
        value = os.environ.get(env_name)
        if value:
            candidates.append((env_name, {"executable_path": value, **launch_mode}))
    for value in _SYSTEM_CHROME_PATHS:
        if Path(value).is_file():
            candidates.append(("system", {"executable_path": value, **launch_mode}))
    candidates.extend((("chrome channel", {"channel": "chrome", **launch_mode}),
                       ("Playwright-managed Chromium", dict(launch_mode))))

    attempts: list[str] = []
    seen: set[tuple] = set()
    for source, kwargs in candidates:
        key = tuple(sorted(kwargs.items()))
        if key in seen:
            continue
        seen.add(key)
        description = kwargs.get("executable_path") or kwargs.get("channel") or "managed chromium"
        try:
            return playwright.chromium.launch(**kwargs)
        except Exception as exc:
            attempts.append(f"{source} ({description}): {exc}")
    raise RuntimeError("No usable Chromium-family browser was found. Resolution attempts:\n- "
                       + "\n- ".join(attempts))


class CaptureFailure(RuntimeError):
    """A fail-closed capture failure with stable, machine-readable details."""

    def __init__(self, diagnostic: dict) -> None:
        self.diagnostic = diagnostic
        super().__init__(json.dumps(diagnostic, sort_keys=True))


class _PhaseTimeout(TimeoutError):
    pass


def _run_phase(call, timeout_ms: int):
    """Run one synchronous Playwright phase under the remaining wall budget."""
    if timeout_ms <= 0:
        raise _PhaseTimeout("capture deadline expired")
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("capture phases require the owning main thread")

    previous = signal.getsignal(signal.SIGALRM)
    outer_delay, outer_interval = signal.getitimer(signal.ITIMER_REAL)
    began = time.monotonic()
    phase_deadline = began + timeout_ms / 1000
    next_outer = began + outer_delay if outer_delay > 0 else None

    def arm(now):
        deadlines = [phase_deadline]
        if next_outer is not None:
            deadlines.append(next_outer)
        signal.setitimer(signal.ITIMER_REAL, max(0.000001, min(deadlines) - now))

    def expired(_signum, _frame):
        nonlocal next_outer
        now = time.monotonic()
        if next_outer is not None and now >= next_outer - 0.0005:
            if outer_interval > 0:
                while next_outer <= now:
                    next_outer += outer_interval
            else:
                next_outer = None
            if previous == signal.SIG_DFL:
                signal.signal(signal.SIGALRM, signal.SIG_DFL)
                os.kill(os.getpid(), signal.SIGALRM)
            elif previous == signal.SIG_IGN:
                # The inherited alarm has fired and is intentionally consumed.
                # Keep scheduling the capture deadline (and the next inherited
                # periodic tick, when present) rather than treating the ignored
                # signal as a capture timeout.
                pass
            elif callable(previous):
                previous(_signum, _frame)
            if time.monotonic() < phase_deadline:
                signal.signal(signal.SIGALRM, expired)
                arm(time.monotonic())
                return
        raise _PhaseTimeout("capture phase exceeded remaining budget")
    signal.signal(signal.SIGALRM, expired)
    arm(began)
    try:
        return call()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if next_outer is not None:
            signal.setitimer(signal.ITIMER_REAL,
                             max(0.000001, next_outer - time.monotonic()),
                             outer_interval)


def capture_target(page, url: str, target: str, path: Path, *,
                   expected_revision: str | None = None,
                   full_page: bool = True, locator: str | None = None,
                   navigate: bool = True, prepare=None) -> dict:
    """Navigate, await the production readiness owner, then atomically capture.

    The monotonic budget begins before navigation and is shared by navigation,
    semantic readiness, and the two-frame paint boundary.
    """
    if threading.current_thread() is not threading.main_thread():
        raise CaptureFailure({
            "code": "thread-ownership", "generation": None,
            "message": "Synchronous Playwright capture requires its owning main thread.",
            "target": target, "phase": "ownership", "elapsedMs": 0,
            "remainingMs": _CAPTURE_BUDGET_MS, "url": url,
            "revision": expected_revision, "metadata": {},
        })
    started = time.monotonic()
    path_key = str(path.resolve())
    authority = object()
    process_claim = _claim_destination(path)
    with _capture_authority_lock:
        _capture_authority[path_key] = authority
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=f".capture-tmp{path.suffix}", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    suffix = path.suffix.lower()
    screenshot_type = "png" if suffix == ".png" else (
        "jpeg" if suffix in (".jpg", ".jpeg") else None
    )

    def timing() -> tuple[int, int]:
        elapsed = max(0, int((time.monotonic() - started) * 1000))
        return elapsed, max(0, _CAPTURE_BUDGET_MS - elapsed)

    def diagnostic(phase: str, *, code: str, runtime: dict | None = None,
                   message: str | None = None) -> dict:
        elapsed, remaining = timing()
        metadata = (runtime or {}).get("metadata") or {}
        return {
            "code": code, "generation": (runtime or {}).get("generation"),
            "message": message, "target": target, "phase": phase,
            "elapsedMs": elapsed, "remainingMs": remaining, "url": url,
            "revision": metadata.get("revision", expected_revision),
            "metadata": metadata,
        }

    current_phase = "starting"
    def run(phase: str, call):
        nonlocal current_phase
        current_phase = phase
        _, remaining = timing()
        if remaining <= 0:
            raise CaptureFailure(diagnostic(phase, code="timeout"))
        try:
            return _run_phase(call, remaining)
        except _PhaseTimeout as exc:
            raise CaptureFailure(diagnostic(phase, code="timeout", message=str(exc))) from exc

    def abort_owner() -> None:
        # Best effort only: the JS readiness race also aborts synchronously
        # when its own consumer timer wins. A frozen/destroyed page must not
        # be allowed to extend cleanup beyond the already-expired deadline.
        try:
            _run_phase(lambda: page.evaluate(
                "() => { const c=window.__okfCaptureConsumerAbort; if(c) c.abort(); }"), 100)
        except Exception:
            pass

    try:
        if navigate:
            run("navigation", lambda: page.goto(
                url, wait_until="domcontentloaded", timeout=timing()[1]))
        run("prepare-owner", lambda: page.evaluate(
            "() => { window.__okfCaptureConsumerAbort = new AbortController(); }"))
        if prepare is not None:
            run("prepare", lambda: prepare(page))
        _, remaining = timing()
        if remaining <= 0:
            raise CaptureFailure(diagnostic("readiness", code="timeout"))
        result = run("readiness", lambda: page.evaluate(
            """async ([options, remainingMs]) => {
              if (!window.okfCaptureReadiness || typeof window.okfCaptureReadiness.start !== 'function')
                return {ok:false, code:'unavailable', generation:null, screenshotAllowed:false,
                  metadata:{readinessPhase:'consumer-api-missing', url:location.href}};
              const controller = window.__okfCaptureConsumerAbort;
              options.signal = controller.signal;
              const owner = window.okfCaptureReadiness.start(options);
              let timer;
              try {
                const value = await Promise.race([owner.promise, new Promise((_, reject) => {
                  timer = setTimeout(() => { controller.abort(); reject(Object.assign(new Error('Consumer capture budget expired.'),
                    {code:'timeout', generation:owner.generation, screenshotAllowed:false,
                     metadata:{readinessPhase:'consumer-timeout', url:location.href}})); }, remainingMs);
                })]);
                return {ok:true, generation:value.generation, screenshotAllowed:value.screenshotAllowed,
                        metadata:value.metadata || {}};
              } catch (error) {
                return {ok:false, code:error.code || 'not-ready', generation:error.generation || owner.generation,
                        screenshotAllowed:false, message:String(error.message || error), metadata:error.metadata || {}};
              } finally { clearTimeout(timer); }
            }""",
            [{"target": target, **({"expectedRevision": expected_revision} if expected_revision else {})}, remaining],
        ))
        if not result.get("ok") or result.get("screenshotAllowed") is not True:
            raise CaptureFailure(diagnostic(
                "readiness", code=result.get("code") or "not-ready",
                runtime=result, message=result.get("message")))
        _, remaining = timing()
        if remaining <= 0:
            raise CaptureFailure(diagnostic("paint-boundary", code="timeout", runtime=result))
        run("paint-boundary", lambda: page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"))
        _, remaining = timing()
        if remaining <= 0:
            raise CaptureFailure(diagnostic("screenshot", code="timeout", runtime=result))
        edge_flow_lease = None
        try:
            # Acquire only at the screenshot boundary. Readiness inspection is
            # pure; the token-qualified lease is released on every terminal
            # path so selected-edge motion resumes after success or failure.
            edge_flow_lease = page.evaluate("""() => {
              const flow=window.__okfLoomGraph&&window.__okfLoomGraph.edgeFlow;
              return flow&&typeof flow.acquireCaptureLease==='function' ? flow.acquireCaptureLease() : null;
            }""")
            if screenshot_type is None:
                raise ValueError(f"unsupported screenshot extension: {path.suffix or '<none>'}")
            if locator:
                run("screenshot", lambda: page.locator(locator).screenshot(
                    path=str(tmp), type=screenshot_type, timeout=timing()[1]))
            else:
                run("screenshot", lambda: page.screenshot(
                    path=str(tmp), type=screenshot_type,
                    full_page=full_page, timeout=timing()[1]))
            with _capture_authority_lock:
                if _capture_authority.get(path_key) is not authority:
                    raise CaptureFailure(diagnostic(
                        "publication", code="superseded",
                        runtime=result,
                        message="A newer capture invocation owns publication."))
                if not _publish_if_current(tmp, path, process_claim):
                    raise CaptureFailure(diagnostic(
                        "publication", code="superseded", runtime=result,
                        message="A newer cross-process capture owns publication."))
                _capture_authority.pop(path_key, None)
        except CaptureFailure:
            raise
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise CaptureFailure(diagnostic(
                "screenshot", code="screenshot-write-failure", runtime=result,
                message=str(exc))) from exc
        finally:
            if edge_flow_lease is not None:
                try:
                    page.evaluate("""token => {
                      const flow=window.__okfLoomGraph&&window.__okfLoomGraph.edgeFlow;
                      return !!(flow&&typeof flow.releaseCaptureLease==='function'&&flow.releaseCaptureLease(token));
                    }""", edge_flow_lease)
                except Exception:
                    # A departed page invalidates the generation in pagehide;
                    # no stale lease can survive or resume its replacement.
                    pass
        return result
    except CaptureFailure:
        abort_owner()
        tmp.unlink(missing_ok=True)
        with _capture_authority_lock:
            if _capture_authority.get(path_key) is authority:
                _capture_authority.pop(path_key, None)
        raise
    except Exception as exc:
        abort_owner()
        tmp.unlink(missing_ok=True)
        with _capture_authority_lock:
            if _capture_authority.get(path_key) is authority:
                _capture_authority.pop(path_key, None)
        timed_out = timing()[0] >= _CAPTURE_BUDGET_MS
        raise CaptureFailure(diagnostic(
            current_phase, code="timeout" if timed_out else f"{current_phase}-failure",
            message=str(exc))) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Reserve and immediately release an ephemeral port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _log(msg: str) -> None:
    print(f"[capture_viewer_proof] {msg}", file=sys.stderr)


def _script_env() -> dict[str, str]:
    """Return an environment that can import checkout-local scripts."""
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{SCRIPT_PATH}{os.pathsep}{existing}" if existing else str(SCRIPT_PATH)
    )
    return env


def _wait_for_http_ok(base: str, timeout: float = _SERVER_STARTUP_TIMEOUT) -> bool:
    """Return True once ``GET base/`` answers 200; False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/", timeout=_POLL_HTTP_TIMEOUT) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(_SERVER_POLL_INTERVAL)
    return False


# ---------------------------------------------------------------------------
# Live server (scripts/okf-loom serve)
# ---------------------------------------------------------------------------


class _LiveServer:
    """Context manager that runs ``scripts/okf-loom serve <bundle>`` on an ephemeral port."""

    def __init__(self, bundle: Path) -> None:
        self.bundle = bundle
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> str:
        self._proc = subprocess.Popen(
            [
                str(REPO_ROOT / "scripts" / "okf-loom"), "serve", str(self.bundle),
                "--host", "127.0.0.1", "--port", str(self.port),
                "--no-watch", "--no-open",
            ],
            cwd=str(REPO_ROOT),
            env=_script_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not _wait_for_http_ok(self.base):
            self.__exit__()
            raise RuntimeError(
                f"scripts/okf-loom serve did not become ready at {self.base} within "
                f"{_SERVER_STARTUP_TIMEOUT:g}s"
            )
        _log(f"live server ready at {self.base}")
        return self.base

    def __exit__(self, *exc) -> None:
        proc = self._proc
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        self._proc = None


# ---------------------------------------------------------------------------
# Static-build server (stdlib http.server)
# ---------------------------------------------------------------------------


class _StaticServer:
    """Context manager that serves a static build dir on an ephemeral port."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> str:
        handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
            *a, directory=str(self.root), **kw
        )
        self._server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", self.port), handler
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        if not _wait_for_http_ok(self.base):
            self.__exit__()
            raise RuntimeError(
                f"static-build server did not become ready at {self.base} "
                f"within {_SERVER_STARTUP_TIMEOUT:g}s"
            )
        _log(f"static server ready at {self.base} (root={self.root})")
        return self.base

    def __exit__(self, *exc) -> None:
        srv = self._server
        if srv is not None:
            srv.shutdown()
            srv.server_close()
        self._server = None
        self._thread = None


# ---------------------------------------------------------------------------
# Static build (scripts/okf-loom build --target static)
# ---------------------------------------------------------------------------


def _build_static_site(bundle: Path, out_dir: Path) -> None:
    """Run ``scripts/okf-loom build <bundle> --out <out_dir> --target static``.

    Raises ``RuntimeError`` on non-zero exit so callers can surface a
    clear diagnostic.

    Note: the CLI's ``build`` subcommand takes the output path via
    ``--out`` (see ``scripts/okf_loom/cli.py``), not positionally.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            str(REPO_ROOT / "scripts" / "okf-loom"), "build", str(bundle),
            "--out", str(out_dir), "--target", "static",
        ],
        cwd=str(REPO_ROOT),
        env=_script_env(),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"scripts/okf-loom build failed (rc={proc.returncode}):\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    _log(f"static site built at {out_dir}")


# ---------------------------------------------------------------------------
# Screenshot capture
# ---------------------------------------------------------------------------


def _capture_targets(base: str, page, *, label: str, out_dir: Path) -> list[Path]:
    """Capture the three browser-proof pages from ``base``; return the file paths.

    ``label`` is "live" or "static" and is folded into each filename so a
    single dated output directory can hold both sets side by side.
    """
    shots: list[Path] = []

    # 1. Graph page. Live server route: /__graph. Static file: /__graph.html.
    graph_url = f"{base}/__graph.html" if label == "static" else f"{base}/__graph"
    p = out_dir / f"{label}-graph.png"
    capture_target(page, graph_url, "graph", p)
    shots.append(p)

    # 2. Concept page (tables/orders).
    concept_url = (
        f"{base}/tables/orders.html" if label == "static" else f"{base}/tables/orders"
    )
    p = out_dir / f"{label}-concept-orders.png"
    capture_target(page, concept_url, "document", p)
    shots.append(p)

    # 3. Search page. Live server: /__search?q=orders. Static build has no
    # search backend; the static search page renders with zero results,
    # which is itself the documented degraded-state artefact.
    search_url = (
        f"{base}/__search.html" if label == "static" else f"{base}/__search?q=orders"
    )
    p = out_dir / f"{label}-search.png"
    capture_target(page, search_url, "search", p)
    shots.append(p)

    return shots


def _run_captures(bundle: Path, out_dir: Path, chrome_path: str | None = None) -> list[Path]:
    """Drive Playwright once for both live + static sources."""
    # Lazy import: this module stays import-safe (no top-level playwright
    # import) so tests / tooling can introspect it without the extra.
    from playwright.sync_api import sync_playwright

    captured: list[Path] = []

    with sync_playwright() as p:
        # Launch once and reuse the browser across both sources.
        try:
            browser = launch_chromium(p, chrome_path)
        except Exception as exc:
            raise SystemExit(str(exc)) from exc
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                device_scale_factor=2,  # crisper screenshots for docs
            )
            page = context.new_page()

            # --- live server ---
            with _LiveServer(bundle) as base:
                captured.extend(_capture_targets(base, page, label="live", out_dir=out_dir))

            # --- static build + stdlib http server ---
            static_dir = out_dir.parent / f"{out_dir.name}-static-src"
            _build_static_site(bundle, static_dir)
            with _StaticServer(static_dir) as base:
                captured.extend(
                    _capture_targets(base, page, label="static", out_dir=out_dir)
                )

            context.close()
        finally:
            browser.close()

    return captured


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="capture_viewer_proof.py",
        description=(
            "Capture browser screenshots of the OKF viewer for the demo "
            "bundle, both live (scripts/okf-loom serve) and static (scripts/okf-loom build --target "
            "static), under docs/screenshots/<date>-viewer/ "
            "(current spec §17)."
        ),
    )
    p.add_argument(
        "--bundle",
        type=Path,
        default=DEFAULT_BUNDLE,
        help=f"OKF bundle to render (default: {DEFAULT_BUNDLE})",
    )
    p.add_argument(
        "--chrome",
        default=None,
        help="path to Chrome/Chromium (then OKF_CHROME, AIC_PLAYWRIGHT_CHROME_PATH, system Chrome, managed Chromium)",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=(
            "parent output directory; a dated subdirectory "
            "<YYYY-MM-DD>-viewer/ is created inside it "
            f"(default: {DEFAULT_OUT_DIR})"
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    # Parse argv FIRST so --help / --version exit cleanly without requiring
    # the optional browser-proof dependencies. Only then probe for Playwright.
    args = _build_arg_parser().parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print(
            "ERROR: playwright is not installed.\n"
            "  python -m pip install playwright\n"
            "  playwright install chromium",
            file=sys.stderr,
        )
        return 1

    bundle = args.bundle.resolve()
    out_parent = args.out_dir.resolve()

    if not bundle.is_dir():
        print(f"ERROR: bundle directory not found: {bundle}", file=sys.stderr)
        return 1

    today = _dt.date.today().isoformat()
    out_dir = out_parent / f"{today}-viewer"
    out_dir.mkdir(parents=True, exist_ok=True)

    _log(f"bundle={bundle}")
    _log(f"out_dir={out_dir}")

    try:
        captured = _run_captures(bundle, out_dir, args.chrome)
    except (RuntimeError, SystemExit) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"# wrote {len(captured)} screenshots to {out_dir}")
    for path in captured:
        rel = path.relative_to(out_parent.parent) if out_parent.parent in path.parents else path
        size = path.stat().st_size if path.exists() else 0
        print(f"  {rel}  ({size} bytes)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
