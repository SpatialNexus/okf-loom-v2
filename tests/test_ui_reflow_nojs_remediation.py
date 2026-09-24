"""Focused browser proof for the final-v2 reflow and no-JS remediation.

The release capture matrix is intentionally strict about clean provenance, so
this test exercises the same current-branch surfaces without publishing a
release bundle.  It leaves fresh screenshots in the directory named by
``OKF_UI_REMEDIATION_PROOF_DIR`` when that variable is set.
"""
from __future__ import annotations

from contextlib import contextmanager
import http.server
import json
import os
import re
import tempfile
from pathlib import Path
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import okf_module_argv, okf_subprocess_env
from okf_loom import Bundle
from okf_loom.render import render_single_file


ROOT = Path(__file__).resolve().parents[1]
CAPTURE_SPEC = ROOT / "scripts" / "capture_ui_overhaul.py"
DEMO_BUNDLE = ROOT / "samples" / "demo_bundle"

from browser_support import sync_playwright, launch_chromium, copy_bundle, live_server
from browser_zoom import zoom_runtime

pytestmark = pytest.mark.browser

# Browser-owned page zoom levels proved here. Both start from one 1440px-wide
# headed window at 100%; Chrome's own zoom then yields 720 / 360 CSS pixels.
ZOOM_NOMINAL_WIDTH = 1440
ZOOM_LEVELS = (200, 400)
# Only these proofs launch headed Chrome (OKF_TEST_HEADED_ZOOM=1 opts in).
HEADED_ZOOM_TESTS = frozenset({
    "test_zoom_capture_uses_real_chrome_page_zoom_not_viewport_or_compositor_proxy",
    "test_authored_scroll_regions_follow_measured_runtime_geometry",
    "test_studio_workspace_keeps_draft_focus_and_local_scrolling_at_real_page_zoom",
})


def _load_capture_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("capture_ui_overhaul_remediation", CAPTURE_SPEC)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def chromium(request, monkeypatch):
    # Parameterized ids ("...[200]") share one opt-in through the function name.
    test_name = getattr(request.node, "originalname", None) or request.node.name
    if os.environ.get("OKF_TEST_HEADED_ZOOM") == "1" and test_name in HEADED_ZOOM_TESTS:
        monkeypatch.setenv("OKF_CAPTURE_REAL_BROWSER_ZOOM", "1")
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        try:
            yield browser
        finally:
            browser.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(base: str, process: subprocess.Popen[object]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"okf serve exited early (rc={process.returncode})")
        try:
            with urllib.request.urlopen(base + "/", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.05)
    raise RuntimeError("okf serve did not become ready")


@contextmanager
def _live_server(bundle: Path):
    with tempfile.TemporaryDirectory(prefix="okf-reflow-") as temp:
        isolated = copy_bundle(bundle, Path(temp) / "bundle")
        with live_server(isolated) as base:
            yield base


class _QuietStaticHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _static_server(root: Path):
    handler = lambda *args, **kwargs: _QuietStaticHandler(  # noqa: E731
        *args, directory=str(root), **kwargs
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _proof_dir(tmp_path: Path) -> Path:
    path = Path(os.environ.get("OKF_UI_REMEDIATION_PROOF_DIR", str(tmp_path / "proof")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _concept_path(bundle: Bundle) -> str:
    target = ("tables", "orders")
    assert target in bundle.concepts, "demo bundle must retain the capture target tables/orders"
    return "/".join(target)


def _reflow_state(page):
    return page.evaluate(
        """() => {
          const root = document.documentElement;
          const body = document.body;
          const cssWidth = root.clientWidth;
          const horizontalScrollports = Array.from(document.querySelectorAll('*')).filter(el => {
            const style = getComputedStyle(el);
            return el.scrollWidth > el.clientWidth + 1 && /(auto|scroll)/.test(style.overflowX);
          }).map(el => ({
            tag: el.tagName.toLowerCase(),
            role: el.getAttribute('role'),
            label: el.getAttribute('aria-label'),
            labelledby: el.getAttribute('aria-labelledby'),
            tabindex: el.getAttribute('tabindex'),
            content: el.matches('.okf-tablewrap, .okf-content-scrollport, .okf-source'),
          }));
          const scrollports = Array.from(document.querySelectorAll('*')).filter(el => {
            const style = getComputedStyle(el);
            return el.scrollWidth > el.clientWidth + 1 &&
              el.scrollHeight > el.clientHeight + 1 &&
              /(auto|scroll)/.test(style.overflowX) &&
              /(auto|scroll)/.test(style.overflowY);
          }).map(el => ({
            tag: el.tagName.toLowerCase(),
            role: el.getAttribute('role'),
            label: el.getAttribute('aria-label'),
            labelledby: el.getAttribute('aria-labelledby'),
            width: el.clientWidth,
            height: el.clientHeight,
            scrollWidth: el.scrollWidth,
            scrollHeight: el.scrollHeight,
          }));
          const pageShell = document.querySelector('.okf-page');
          const shellStyle = pageShell && getComputedStyle(pageShell);
          return {
            cssWidth,
            documentScrollWidth: Math.max(root.scrollWidth, body ? body.scrollWidth : 0),
            documentClientWidth: cssWidth,
            pageGrid: shellStyle ? shellStyle.gridTemplateColumns : null,
            pageGridTracks: shellStyle ? shellStyle.gridTemplateColumns.split(/\\s+/).filter(Boolean).length : 0,
            horizontalScrollports,
            scrollports,
          };
        }"""
    )


def _apply_real_zoom(capture, page, percent: int, tmp_path: Path, monkeypatch):
    """Drive Chrome's own page zoom, or fail: no viewport/CSS stand-in exists."""
    if os.environ.get("OKF_CAPTURE_REAL_BROWSER_ZOOM") != "1":
        with pytest.raises(capture.CaptureFailure) as caught:
            capture._apply_browser_zoom(page, percent)
        assert caught.value.diagnostic["code"] == "zoom-unsupported"
        assert page.evaluate("() => innerWidth") == ZOOM_NOMINAL_WIDTH
        pytest.fail("this proof requires OKF_CAPTURE_REAL_BROWSER_ZOOM=1 and headed Chrome")
    runtime = zoom_runtime(capture, tmp_path, monkeypatch)
    return capture._apply_browser_zoom(page, percent, runtime=runtime)


def _assert_browser_owned_zoom(page, evidence: dict, percent: int) -> dict:
    """Oracle for genuine page zoom: DPR 1 -> percent/100 and the CSS viewport
    shrinks by the same factor, while pinch scale and CSS ``zoom`` stay untouched.
    A narrow viewport keeps DPR 1; device-scale emulation keeps the CSS width;
    CSS zoom shows up in computed style. None of them can satisfy all of this.
    """
    expected_width = ZOOM_NOMINAL_WIDTH * 100 // percent
    assert evidence["mechanism"] == "chrome-page-zoom-keyboard", evidence
    assert evidence["zoomPercent"] == percent, evidence
    assert evidence["baselineVerified"] is True and evidence["sessionBindingVerified"] is True, evidence
    assert evidence["nominalViewportWidth"] == ZOOM_NOMINAL_WIDTH, evidence
    assert evidence["devicePixelRatioBefore"] == 1, evidence
    assert evidence["devicePixelRatio"] == percent / 100, evidence
    assert evidence["innerWidth"] == expected_width, evidence
    assert evidence["visualViewportScale"] == 1, evidence
    assert evidence["cssZoom"] is None, evidence
    live = page.evaluate(
        """() => ({
          devicePixelRatio: Number(devicePixelRatio), innerWidth, innerHeight,
          clientWidth: document.documentElement.clientWidth,
          visualViewportScale: Number(visualViewport.scale),
          rootZoom: getComputedStyle(document.documentElement).zoom,
          bodyZoom: getComputedStyle(document.body).zoom,
          inlineZoom: document.documentElement.style.zoom || document.body.style.zoom || null,
        })"""
    )
    assert live["devicePixelRatio"] == percent / 100 and live["innerWidth"] == expected_width, live
    # clientWidth excludes a classic vertical scrollbar; innerWidth does not.
    assert expected_width - 20 <= live["clientWidth"] <= expected_width, live
    assert live["visualViewportScale"] == 1, live
    assert live["rootZoom"] == "1" and live["bodyZoom"] == "1" and live["inlineZoom"] is None, live
    return live


def _assert_content_scrollports_are_named_keyboard_regions(state: dict) -> None:
    for port in state["horizontalScrollports"]:
        if port["content"]:
            assert port["role"] == "region" and port["tabindex"] == "0", state
            assert port["label"] or port["labelledby"], state
    assert all(item["role"] == "region" and (item["label"] or item["labelledby"])
               for item in state["scrollports"]), state


@pytest.mark.parametrize("percent", ZOOM_LEVELS)
def test_zoom_capture_uses_real_chrome_page_zoom_not_viewport_or_compositor_proxy(
    chromium, tmp_path: Path, monkeypatch, percent: int
) -> None:
    capture = _load_capture_module()
    bundle = Bundle.load(DEMO_BUNDLE)
    proof = _proof_dir(tmp_path)
    expected_width = ZOOM_NOMINAL_WIDTH * 100 // percent
    with _live_server(DEMO_BUNDLE) as base:
        context = chromium.new_context(viewport={"width": ZOOM_NOMINAL_WIDTH, "height": 1000})
        page = context.new_page()
        try:
            page.goto(base + "/" + _concept_path(bundle), wait_until="load")
            evidence = _apply_real_zoom(capture, page, percent, tmp_path, monkeypatch)
            live = _assert_browser_owned_zoom(page, evidence, percent)
            page.evaluate("evidence => { window.__okfZoomEvidence = evidence; }", evidence)
            zoom_spec = next(item for item in capture.SCENARIO_SPECS if item.id == f"zoom-{percent}")
            metadata = capture._browser_metadata(page, zoom_spec)
            state = _reflow_state(page)
            assert metadata["zoom_percent"] == percent
            assert metadata["reflow"]["pageGridTracks"] == 1
            assert evidence["journey"]["restored"] is True, evidence
            assert evidence["journey"]["no_horizontal_overflow"] is True, evidence
            assert expected_width - 20 <= state["cssWidth"] <= expected_width, state
            assert state["documentScrollWidth"] <= state["documentClientWidth"] + 1, state
            assert state["pageGridTracks"] == 1, state
            if percent == 400:
                # 360 CSS px cannot hold the schema table or the SQL line.
                assert any(item["content"] for item in state["horizontalScrollports"]), state
            _assert_content_scrollports_are_named_keyboard_regions(state)
            search = page.locator(".okf-topbar input[type=search]")
            search.focus()
            focus = page.evaluate(
                """() => {
                  const active = document.activeElement;
                  const style = active && getComputedStyle(active);
                  return {id: active && active.id, outline: style && style.outlineStyle,
                          outlineWidth: style && parseFloat(style.outlineWidth || '0')};
                }"""
            )
            assert focus["id"] == "" and focus["outline"] == "solid" and focus["outlineWidth"] >= 2, focus
            page.screenshot(path=str(proof / f"zoom-{percent}.png"), full_page=True)
            (proof / f"zoom-{percent}.json").write_text(
                json.dumps({"browser": evidence, "live": live, "capture_metadata": metadata, "reflow": state},
                           indent=2, sort_keys=True) + "\n"
            )
        finally:
            context.close()


def test_authored_scroll_regions_follow_measured_runtime_geometry(
    chromium, tmp_path, monkeypatch,
) -> None:
    capture = _load_capture_module()
    bundle = Bundle.load(DEMO_BUNDLE)
    with _live_server(DEMO_BUNDLE) as base:
        context = chromium.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        try:
            page.goto(base + "/" + _concept_path(bundle), wait_until="load")
            code_port = page.locator(".okf-content-scrollport--code").first
            assert code_port.count() == 1
            assert code_port.get_attribute("role") is None
            assert code_port.get_attribute("tabindex") is None
            assert code_port.locator(".okf-code-copy").count() == 1

            evidence = _apply_real_zoom(capture, page, 400, tmp_path, monkeypatch)
            _assert_browser_owned_zoom(page, evidence, 400)
            page.wait_for_function(
                """() => {
                  const port = document.querySelector('.okf-content-scrollport--code');
                  return port && port.scrollWidth > port.clientWidth + 1 && port.tabIndex === 0;
                }"""
            )
            geometry = code_port.evaluate(
                """port => {
                  const pre = port.querySelector('pre');
                  const nestedScrollers = Array.from(port.querySelectorAll('*')).filter(el => {
                    const style = getComputedStyle(el);
                    return el.scrollWidth > el.clientWidth + 1 && /(auto|scroll)/.test(style.overflowX);
                  });
                  return {
                    outerOverflows: port.scrollWidth > port.clientWidth + 1,
                    role: port.getAttribute('role'),
                    label: port.getAttribute('aria-label'),
                    tabindex: port.getAttribute('tabindex'),
                    preOverflowX: getComputedStyle(pre).overflowX,
                    nestedScrollerCount: nestedScrollers.length,
                  };
                }"""
            )
            label = geometry.pop("label")
            assert label and label.endswith(" code block. Scroll horizontally to view all content.")
            assert geometry == {
                "outerOverflows": True,
                "role": "region",
                "tabindex": "0",
                "preOverflowX": "visible",
                "nestedScrollerCount": 0,
            }
        finally:
            context.close()


# --- Studio workspace under browser-owned zoom -----------------------------
# Width-only layout is proven at set viewports in test_studio_layout_real_browser;
# what only real zoom can show is the live transition: a reader who is already
# writing a comment zooms in, and the rail becomes a modal overlay under them.

_ZOOM_LONG_LINE = "okf-loom " + " ".join(f"--flag-{n}=value-{n}" for n in range(60))
_ZOOM_DOCS = {
    "short": "---\ntype: Note\ntitle: Short\n---\n\n# Short\n\nTwo words.\n",
    "rich": f"""---
type: Reference
title: Rich reference
description: Running text, a wide table and a long code line.
---

# Rich reference

{"Running text that is long enough to reach the reading measure. " * 12}

## A section heading

{"More running text under a heading, again long enough to fill the lane. " * 8}

## Wide things

| {" | ".join(f"Column {n} heading" for n in range(14))} |
|{"---|" * 14}
| {" | ".join(f"cell value {n}" for n in range(14))} |

```bash
{_ZOOM_LONG_LINE}
```
""",
}
_ZOOM_DRAFT = "Draft typed before zooming in"

_STUDIO_ZOOM_STATE = """() => {
  const root = document.documentElement, panel = document.getElementById('okf-panel');
  const box = node => { if (!node || !node.getClientRects().length) return null;
    const r = node.getBoundingClientRect(); return {left: Math.round(r.left), right: Math.round(r.right), width: Math.round(r.width)}; };
  const active = document.activeElement, source = document.querySelector('.okf-source');
  const view = document.querySelector('.okf-view'), split = document.querySelector('.okf-viewswitch__btn[data-mode="split"]');
  return {
    innerWidth, clientWidth: root.clientWidth,
    documentOverflow: Math.max(root.scrollWidth, document.body.scrollWidth) - root.clientWidth,
    rail: document.body.classList.contains('okf-panel-rail'),
    panel: panel.hidden ? null : box(panel), panelRole: panel.getAttribute('role'), panelModal: panel.getAttribute('aria-modal'),
    active: active && {tag: active.tagName.toLowerCase(), label: active.getAttribute('aria-label'),
      text: (active.textContent || '').trim().slice(0, 60), value: 'value' in active ? active.value : null, box: box(active)},
    mode: view && view.dataset.okfView, article: box(document.querySelector('.okf-page__main')), view: box(view),
    splitDisabled: split.getAttribute('aria-disabled'), splitTitle: split.getAttribute('title'),
    source: source && source.getClientRects().length ? {whiteSpace: getComputedStyle(source).whiteSpace,
      scrolls: source.scrollWidth > source.clientWidth + 1, tabindex: source.getAttribute('tabindex'), box: box(source)} : null,
    status: (document.getElementById('okf-annotation-status') || {}).textContent || '',
  };
}"""


def _scroll_region_by_keyboard(page, name) -> None:
    """The region is found by its computed accessible name, then scrolled with arrows only."""
    region = page.get_by_role("region", name=name)
    assert region.count() == 1, name
    assert region.get_attribute("tabindex") == "0", name
    region.focus()
    assert region.evaluate("el => el === document.activeElement && el.scrollLeft === 0"), name
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    handle = region.element_handle()
    page.wait_for_function("el => el.scrollLeft > 0", arg=handle, timeout=5_000)
    assert page.evaluate(
        "() => Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)"
        " - document.documentElement.clientWidth") <= 1, name


@pytest.mark.parametrize("doc", ["short", "rich"])
@pytest.mark.parametrize("percent", ZOOM_LEVELS)
def test_studio_workspace_keeps_draft_focus_and_local_scrolling_at_real_page_zoom(
    chromium, tmp_path: Path, monkeypatch, percent: int, doc: str
) -> None:
    capture = _load_capture_module()
    # The capture outline journey (proved above) moves focus and presses Escape.
    # Here focus must stay exactly where the reader left it across the zoom.
    monkeypatch.setattr(capture, "_prepare_zoom_keyboard_journey", lambda _page: {"skipped": "studio-focus-proof"})
    proof = _proof_dir(tmp_path)
    bundle = tmp_path / "studio-zoom-bundle"
    bundle.mkdir()
    (bundle / "index.md").write_text("# Home\n", encoding="utf-8")
    for name, text in _ZOOM_DOCS.items():
        (bundle / f"{name}.md").write_text(text, encoding="utf-8")
    expected_width = ZOOM_NOMINAL_WIDTH * 100 // percent
    record: dict[str, object] = {"percent": percent, "doc": doc}
    with _live_server(bundle) as base:
        context = chromium.new_context(viewport={"width": ZOOM_NOMINAL_WIDTH, "height": 1000})
        page = context.new_page()
        errors: list[str] = []
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: errors.append(str(error)))

        def state(label: str) -> dict:
            record[label] = page.evaluate(_STUDIO_ZOOM_STATE)
            return record[label]

        try:
            page.goto(f"{base}/{doc}", wait_until="load")
            page.wait_for_function(
                "() => window.okfLoomStudio && window.okfLoomStudio.state.commentsStatus === 'ready'")

            # 100%: Comments is a rail beside the article and the reader is mid-draft.
            page.get_by_role("button", name="Comment on this page", exact=True).click()
            composer = page.get_by_role("textbox", name="Comment for agent")
            composer.wait_for(state="visible")
            page.wait_for_function("() => document.body.classList.contains('okf-panel-rail')")
            # Focus moves into the composer after the panel renders; type only then.
            page.wait_for_function(
                "() => document.activeElement && document.activeElement.getAttribute('aria-label') === 'Comment for agent'")
            page.keyboard.type(_ZOOM_DRAFT)
            before = state("100-comments-open")
            assert before["panelRole"] == "complementary" and before["active"]["value"] == _ZOOM_DRAFT, before

            evidence = _apply_real_zoom(capture, page, percent, tmp_path, monkeypatch)
            record["browser"] = evidence
            record["live"] = _assert_browser_owned_zoom(page, evidence, percent)

            # Comments open at zoom: the rail yields to a modal overlay without
            # dropping the draft, the caret, or anything off the side of the page.
            page.wait_for_function("() => document.getElementById('okf-panel').getAttribute('role') === 'dialog'")
            opened = state("zoom-comments-open")
            assert opened["rail"] is False and opened["panelModal"] == "true", opened
            assert opened["active"]["tag"] == "textarea" and opened["active"]["label"] == "Comment for agent", opened
            assert opened["active"]["value"] == _ZOOM_DRAFT, opened
            assert opened["panel"]["left"] >= 0 and opened["panel"]["right"] <= expected_width, opened
            assert opened["active"]["box"]["left"] >= 0 and opened["active"]["box"]["right"] <= expected_width, opened
            assert opened["documentOverflow"] <= 1, opened
            page.keyboard.type(" and after")
            assert composer.input_value() == _ZOOM_DRAFT + " and after"
            page.screenshot(path=str(proof / f"studio-zoom-{percent}-{doc}-comments-open.png"))

            # Comments closed at zoom: Escape returns focus to where the comment began.
            page.keyboard.press("Escape")
            page.wait_for_function("() => document.getElementById('okf-panel').hidden")
            closed = state("zoom-comments-closed")
            assert closed["active"]["tag"] == "button" and closed["active"]["text"] == "Comment on this page", closed
            assert closed["mode"] == "rendered" and closed["documentOverflow"] <= 1, closed
            assert abs(closed["view"]["width"] - closed["article"]["width"]) <= 1, closed
            rendered = _reflow_state(page)
            record["zoom-rendered-reflow"] = rendered
            _assert_content_scrollports_are_named_keyboard_regions(rendered)
            content_ports = [item for item in rendered["horizontalScrollports"] if item["content"]]
            if doc == "rich":
                assert len(content_ports) == 2, rendered
                _scroll_region_by_keyboard(
                    page, re.compile(r", 14 columns\. Scroll horizontally to view all columns\.$"))
                _scroll_region_by_keyboard(page, "bash code block. Scroll horizontally to view all content.")
            else:
                assert content_ports == [], rendered
            page.screenshot(path=str(proof / f"studio-zoom-{percent}-{doc}-rendered.png"))

            # Source at zoom, keyboard only: wrapped by default, local scrolling on request.
            switch = page.get_by_role("group", name="View mode", exact=True)
            switch.get_by_role("button", name="Source", exact=True).focus()
            page.keyboard.press("Enter")
            page.wait_for_function("() => document.querySelector('.okf-source').dataset.loaded === '1'")
            wrapped = state("zoom-source-wrapped")
            assert wrapped["mode"] == "source" and wrapped["documentOverflow"] <= 1, wrapped
            assert wrapped["source"]["whiteSpace"] == "pre-wrap" and wrapped["source"]["scrolls"] is False, wrapped
            assert wrapped["source"]["tabindex"] is None, wrapped
            assert abs(wrapped["source"]["box"]["width"] - wrapped["article"]["width"]) <= 1, wrapped
            wrap = page.get_by_role("button", name="Wrap lines")
            assert wrap.get_attribute("aria-pressed") == "true"
            wrap.focus()
            page.keyboard.press("Enter")
            page.wait_for_function(
                "() => getComputedStyle(document.querySelector('.okf-source')).whiteSpace === 'pre'")
            assert wrap.get_attribute("aria-pressed") == "false"
            if doc == "rich":
                page.wait_for_function("() => document.querySelector('.okf-source').getAttribute('tabindex') === '0'")
                _scroll_region_by_keyboard(page, "Source markdown (read-only). Scroll to view all lines.")
            unwrapped = state("zoom-source-unwrapped")
            assert unwrapped["source"]["scrolls"] is (doc == "rich"), unwrapped
            assert unwrapped["documentOverflow"] <= 1, unwrapped
            page.screenshot(path=str(proof / f"studio-zoom-{percent}-{doc}-source.png"))

            # Split at zoom: two 300px panes cannot fit 720 or 360 CSS px, so the
            # control stays operable, says why, and never crushes two panes in.
            split = switch.get_by_role("button", name="Split", exact=True)
            assert split.get_attribute("aria-disabled") == "true"
            assert "wider workspace" in (split.get_attribute("title") or "")
            split.focus()
            page.keyboard.press("Enter")
            page.wait_for_function(
                "() => /Split needs a wider workspace/.test(document.getElementById('okf-annotation-status').textContent)")
            refused = state("zoom-split-refused")
            assert refused["mode"] == "rendered" and refused["splitDisabled"] == "true", refused
            assert refused["active"]["text"] == "Split" and refused["documentOverflow"] <= 1, refused

            # The draft outlives the close: reopening by keyboard resumes it.
            reopen = page.get_by_role("button", name="Comment on this page", exact=True)
            reopen.focus()
            page.keyboard.press("Enter")
            page.wait_for_function("() => !document.getElementById('okf-panel').hidden")
            page.wait_for_function(
                "() => document.activeElement && document.activeElement.getAttribute('aria-label') === 'Comment for agent'")
            resumed = state("zoom-comments-reopened")
            assert resumed["active"]["value"] == _ZOOM_DRAFT + " and after", resumed
            assert resumed["panelRole"] == "dialog" and resumed["documentOverflow"] <= 1, resumed
            _assert_browser_owned_zoom(page, evidence, percent)
            assert errors == [], errors
        finally:
            record["console_errors"] = errors
            (proof / f"studio-zoom-{percent}-{doc}.json").write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n")
            context.close()


def test_nojs_single_file_hides_inert_controls_and_keeps_one_notice(
    chromium, tmp_path: Path
) -> None:
    bundle = Bundle.load(DEMO_BUNDLE)
    output = tmp_path / "single"
    output.mkdir()
    render_single_file(bundle, output / "index.html")
    proof = _proof_dir(tmp_path)
    with _static_server(tmp_path) as base:
        context = chromium.new_context(java_script_enabled=False, viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        try:
            page.goto(base + "/single/index.html", wait_until="domcontentloaded")
            assert page.locator(".okf-studio-fallback-banner:visible").count() == 1
            assert page.locator('[data-okf-shell-item="index"] a').get_attribute("aria-current") == "page"
            assert page.locator('[data-okf-shell-item="graph"] a').get_attribute("aria-current") is None
            assert page.get_by_role("heading", name="demo_bundle index", level=1).is_visible()
            assert page.get_by_role("heading", name="Resolved in-bundle relationships", level=2).is_visible()
            assert page.locator(".okf-graph-nojs:visible").count() == 1
            skip = page.get_by_role("link", name="Skip to content")
            brand = page.locator('[data-okf-shell-item="bundle"]')
            graph_link = page.locator('[data-okf-shell-item="graph"] a')
            assert skip.get_attribute("href") == "#okf-single-index"
            assert brand.get_attribute("href") == "#okf-single-index"
            assert graph_link.get_attribute("href") == "#okf-single-graph-fallback"
            graph_link.press("Enter")
            page.wait_for_url("**#okf-single-graph-fallback")
            fallback = page.locator("#okf-single-graph-fallback")
            assert fallback.is_visible() and fallback.evaluate("el => el.getBoundingClientRect().height > 0")
            assert page.evaluate("() => document.activeElement.id") == "okf-single-graph-fallback"
            brand.press("Enter")
            page.wait_for_url("**#okf-single-index")
            assert page.evaluate("() => document.activeElement.id") == "okf-single-index"
            skip.focus()
            skip.press("Enter")
            assert page.evaluate("() => document.activeElement.id") == "okf-single-index"
            for selector in ("#okf-search", "#okf-filter-type", "#okf-layout", "#okf-reset"):
                assert page.locator(selector + ":visible").count() == 0, selector
            assert "JavaScript is off." in page.locator("body").inner_text()
            aria = page.locator("body").aria_snapshot()
            assert aria.count("note:") == 1 and "JavaScript is off." in aria
            page.screenshot(path=str(proof / "no-js-single-file.png"), full_page=True)
        finally:
            context.close()


def test_scripted_single_file_root_keeps_index_as_the_current_destination(
    chromium, tmp_path: Path
) -> None:
    output = tmp_path / "single"
    output.mkdir()
    render_single_file(Bundle.load(DEMO_BUNDLE), output / "index.html")
    with _static_server(tmp_path) as base:
        context = chromium.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()
        try:
            page.goto(base + "/single/index.html", wait_until="load")
            assert page.locator('[data-okf-shell-item="index"] a').get_attribute("aria-current") == "page"
            assert page.locator('[data-okf-shell-item="graph"] a').get_attribute("aria-current") is None
            assert page.locator("#okf-single-index").is_visible()
            assert page.locator("main#okf-single-index").count() == 1
            assert page.locator("#okf-main").is_hidden()
            skip = page.get_by_role("link", name="Skip to content")
            brand = page.locator('[data-okf-shell-item="bundle"]')
            graph_link = page.locator('[data-okf-shell-item="graph"] a')
            assert skip.get_attribute("href") == brand.get_attribute("href") == "#okf-single-index"
            assert graph_link.get_attribute("href") == "#okf-main"
            skip.focus()
            skip.press("Enter")
            assert page.evaluate("() => document.activeElement.id") == "okf-single-index"
            readiness = page.evaluate("() => window.okfCaptureReadiness.start({target:'document'}).promise")
            assert readiness["screenshotAllowed"] is True
            assert readiness["metadata"]["kind"] == "document"
            graph_link.click()
            page.wait_for_url("**#okf-graph")
            assert page.locator('[data-okf-shell-item="graph"] a').get_attribute("aria-current") == "page"
            assert page.locator("#okf-main").is_visible()
            assert page.evaluate("() => document.activeElement.id") == "okf-graph"
            assert skip.get_attribute("href") == brand.get_attribute("href") == "#okf-main"
            assert graph_link.get_attribute("href") == "#okf-graph"

            assert page.evaluate("() => window.okfActivateGraphConcept('tables/orders', 'keyboard')") is True
            page.wait_for_url("**?selected=tables%2Forders#okf-graph")
            page.wait_for_function("() => !document.getElementById('detail-content').hidden")
            assert skip.get_attribute("href") == brand.get_attribute("href") == "#detail-content"
            skip.focus()
            skip.press("Enter")
            assert page.evaluate("() => document.activeElement.id") == "detail-content"

            brand.click()
            page.wait_for_url("**#okf-single-index")
            assert page.locator("#okf-single-index").is_visible()
            assert "selected=" not in page.url
            page.go_back()
            page.wait_for_function("() => new URL(location.href).searchParams.get('selected') === 'tables/orders'")
            assert page.evaluate("() => location.hash") == "#detail-content"
            assert page.locator("#okf-main").is_visible()
            assert skip.get_attribute("href") == "#detail-content"
        finally:
            context.close()


def test_nojs_live_keeps_native_links_and_form_but_hides_theme_control(
    chromium, tmp_path: Path
) -> None:
    bundle = Bundle.load(DEMO_BUNDLE)
    proof = _proof_dir(tmp_path)
    with _live_server(DEMO_BUNDLE) as base:
        context = chromium.new_context(java_script_enabled=False, viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        try:
            concept_url = base + "/" + _concept_path(bundle)
            page.goto(concept_url, wait_until="domcontentloaded")
            assert page.locator(".okf-studio-fallback-banner:visible").count() == 1
            assert page.locator(".okf-topbar .okf-search-form:visible").count() == 1
            assert page.locator(".okf-topbar .okf-search-form").get_attribute("method") == "get"
            context_link = page.locator(".okf-context-actions a[href]:visible")
            assert context_link.count() == 1
            context_href = context_link.get_attribute("href") or ""
            assert "/__context" in context_href
            assert page.locator("#okf-theme:visible").count() == 0
            assert page.locator(".okf-page__sidebar:visible").count() == 0

            # These are the two native no-JS escape hatches: submit the GET
            # search form and follow the canonical context link.  They must
            # work without any client runtime, unlike Appearance/Studio UI.
            page.locator(".okf-topbar .okf-search-form input[name=q]").fill("users")
            page.locator(".okf-topbar .okf-search-form input[name=q]").press("Enter")
            page.wait_for_url("**/__search?q=users", timeout=5_000)
            assert page.locator("h1").filter(has_text="Search this bundle").count() == 1
            page.goto(concept_url, wait_until="domcontentloaded")
            response = page.goto(base + context_href, wait_until="domcontentloaded")
            assert response is not None and response.status == 200
            assert "orders" in page.locator("body").inner_text().lower()
            page.goto(concept_url, wait_until="domcontentloaded")
            page.screenshot(path=str(proof / "no-js-live.png"), full_page=True)
        finally:
            context.close()
