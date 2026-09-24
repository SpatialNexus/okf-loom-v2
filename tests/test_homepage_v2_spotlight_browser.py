"""Browser e2e proof for the v2 homepage prominence/balance spotlight (P1).

The 'New in Annotations v2' hero module must make the v2 hands-on demo the
PRIMARY above-the-fold CTA and 'Open graph' its secondary, instead of
the graph being the strongest hero button while v2 is only inline intro text.

These tests serve the real ``docs-bundle`` (the homepage that actually carries
the ``demo/annotations_v2_showcase`` concept) and assert the user-visible
prominence/layout/keyboard contracts via Playwright. They skip cleanly when
Playwright or a Chrome channel is unavailable.
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from browser_support import sync_playwright, launch_chromium, copy_bundle, expect, live_server

from conftest import okf_module_argv, okf_subprocess_env

pytestmark = pytest.mark.browser

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
DOCS_BUNDLE = TOOLKIT_ROOT / "docs-bundle"
_SERVER_STARTUP_TIMEOUT = 20.0


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_server(proc: subprocess.Popen, base: str) -> None:
    deadline = time.monotonic() + _SERVER_STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"okf serve exited early (rc={proc.returncode})")
        try:
            with urllib.request.urlopen(f"{base}/", timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.15)
    pytest.fail(f"okf serve not ready within {_SERVER_STARTUP_TIMEOUT:g}s")


@pytest.fixture(scope="module")
def server_url(tmp_path_factory) -> str:
    bundle = copy_bundle(DOCS_BUNDLE, tmp_path_factory.mktemp("spotlight") / "bundle")
    with live_server(bundle) as base:
        yield base


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = launch_chromium(p)
        try:
            context = browser.new_context()
            pg = context.new_page()
            yield pg
            context.close()
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Prominence + balance
# ---------------------------------------------------------------------------


def test_v2_spotlight_is_primary_above_the_fold(server_url: str, page) -> None:
    """On a desktop viewport the spotlight sits in the right hero column, fully
    above the fold, and the v2 demo is the FIRST focusable CTA in the hero."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(server_url + "/")
    cta = page.locator(".okf-v2-spotlight__cta")
    cta.wait_for(state="visible", timeout=8000)
    # Spotlight is above the fold.
    box = page.locator(".okf-v2-spotlight").bounding_box()
    assert box is not None and box["y"] >= 0 and box["y"] < 900, (
        "spotlight must render above the fold on desktop"
    )
    overview = page.locator(".okf-index__overview").bounding_box()
    assert overview is not None and overview["y"] >= 0, (
        "the expanded homepage overview must precede the type sections"
    )
    # Primary CTA links the v2 demo.
    assert cta.get_attribute("href") == "/demo/annotations_v2_showcase"
    assert cta.inner_text().strip() == "Try the v2 hands-on demo"
    # v2 demo is the first focusable link inside the hero; 'Open graph'
    # is the second (graph demoted to secondary).
    hero_links = page.eval_on_selector_all(
        '.okf-hero a[href]',
        "els => els.map(e => ({text: e.textContent.trim(), href: e.getAttribute('href')}))",
    )
    assert hero_links[0]["href"] == "/demo/annotations_v2_showcase", (
        "v2 demo must be the first (primary) hero link"
    )
    assert hero_links[1]["href"] == "/__graph", (
        "'Open graph' must be the second (secondary) hero link"
    )
    # F13: the two graph actions are distinctly named — the navigating
    # secondary reads 'Open graph'; the inline thumbnail disclosure reads
    # 'Preview graph' (previously both were 'Explore the graph').
    assert hero_links[1]["text"].endswith("Open graph"), (
        f"secondary hero link must read 'Open graph', got {hero_links[1]['text']!r}"
    )
    summary = page.locator(".okf-home-graph > summary")
    assert summary.inner_text().strip() == "Preview graph", (
        "inline graph disclosure must read 'Preview graph'"
    )
    # Tertiary 'Read what changed' is the THIRD spotlight action and reaches the
    # honest release-notes page in one click (closes the user-outcome gap).
    assert hero_links[2]["href"] == "/explanation/v2_release_notes", (
        "'Read what changed' must be the third spotlight link -> release notes"
    )
    # No competing filled graph button in the stats row.
    assert page.locator(".okf-hero__stats .okf-hero__graph-link").count() == 0

    # F13 behaviours, not only labels. 'Preview graph' is an in-page
    # disclosure: keyboard activation reveals the thumbnail and never
    # navigates (same URL, same document — the marker survives).
    home_url = page.url
    page.evaluate("() => { window.__okfPreviewStayed = true; }")
    preview = page.locator(".okf-home-graph")
    thumbnail = preview.locator(".okf-graph-thumb svg").first
    expect(preview).to_have_js_property("open", False)
    expect(thumbnail).to_be_hidden()
    summary.focus()
    page.keyboard.press("Enter")
    expect(preview).to_have_js_property("open", True)
    expect(thumbnail).to_be_visible()
    thumb_box = thumbnail.bounding_box()
    assert thumb_box is not None and thumb_box["width"] > 0 and thumb_box["height"] > 0, thumb_box
    assert preview.locator(".okf-graph-thumb__nodes > *").count() > 0, (
        "the preview must draw the bundle's concepts, not an empty frame"
    )
    assert page.url == home_url, "'Preview graph' must not change the URL"
    assert page.evaluate("() => window.__okfPreviewStayed === true"), (
        "'Preview graph' must not reload or replace the document"
    )

    # 'Open graph' is the navigating action: it lands on the graph route with
    # an initialized, laid-out and visible graph.
    open_graph = page.locator('.okf-v2-spotlight a[href="/__graph"]')
    expect(open_graph).to_have_count(1)
    expect(open_graph).to_contain_text("Open graph")
    open_graph.click()
    page.wait_for_url(f"{server_url}/__graph", timeout=15000)
    page.wait_for_function(
        "() => !!window.__okfLoomGraph && !!window.__okfLoomGraph.cy"
        " && window.__okfLoomGraph.cy.nodes().length > 0"
        " && window.__okfLoomGraph.layoutStats.lastAppliedSeq >= 1",
        timeout=15000,
    )
    canvas = page.locator("#okf-graph canvas").first
    expect(canvas).to_be_visible()
    canvas_box = canvas.bounding_box()
    assert canvas_box is not None and canvas_box["width"] > 0 and canvas_box["height"] > 0, canvas_box


def test_v2_spotlight_tertiary_reaches_release_notes(server_url: str, page) -> None:
    """The tertiary 'Read what changed' action links the release-notes concept,
    is keyboard-reachable, and the target page actually renders in the Studio."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(server_url + "/")
    tertiary = page.locator(".okf-v2-spotlight__tertiary")
    tertiary.wait_for(state="visible", timeout=8000)
    assert tertiary.get_attribute("href") == "/explanation/v2_release_notes"
    assert tertiary.inner_text().strip() == "Read what changed →"
    # The tertiary inherits the global :focus-visible accent ring.
    tertiary.focus()
    outline = page.eval_on_selector(
        ".okf-v2-spotlight__tertiary",
        "el => { const s = getComputedStyle(el); "
        "return {style: s.outlineStyle, width: s.outlineWidth}; }",
    )
    assert outline["style"] == "solid" and outline["width"] == "2px", (
        f"tertiary link must show a 2px solid focus ring, got {outline}"
    )
    # Navigating reaches a real, rendered page with the authored title.
    # (Avoid networkidle: the Studio keeps an SSE stream open, so it never
    # settles. Wait for the SPA URL change + the rendered h1 instead.)
    tertiary.click()
    page.wait_for_url("**/explanation/v2_release_notes**", timeout=10000)
    page.locator("h1").wait_for(state="visible", timeout=10000)
    assert "What changed in okf-loom v2" in page.locator("h1").inner_text()


def test_v2_spotlight_no_horizontal_overflow_on_mobile(server_url: str, page) -> None:
    """At phone width (and 200%-zoom-equivalent) the hero stacks with no
    horizontal overflow and the module stays above the fold."""
    for width in (390, 640):
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(server_url + "/")
        page.locator(".okf-v2-spotlight").wait_for(state="visible", timeout=8000)
        scroll_width = page.evaluate("document.documentElement.scrollWidth")
        assert scroll_width <= width, (
            f"horizontal overflow at {width}px: scrollWidth={scroll_width}"
        )
        box = page.locator(".okf-v2-spotlight").bounding_box()
        assert box is not None and box["y"] < 900, (
            f"spotlight must be above the fold at {width}px"
        )
        # Hero is NOT a two-column grid on mobile.
        display = page.eval_on_selector(
            ".okf-hero", "el => getComputedStyle(el).display"
        )
        assert display == "block", (
            f"hero must stack (block) at {width}px, got {display}"
        )


def test_v2_spotlight_cta_has_visible_focus_ring(server_url: str, page) -> None:
    """Keyboard focus on the primary CTA shows a visible focus ring."""
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(server_url + "/")
    cta = page.locator(".okf-v2-spotlight__cta")
    cta.wait_for(state="visible", timeout=8000)
    cta.focus()
    outline = page.eval_on_selector(
        ".okf-v2-spotlight__cta",
        "el => { const s = getComputedStyle(el); "
        "return {style: s.outlineStyle, width: s.outlineWidth, color: s.outlineColor}; }",
    )
    assert outline["style"] == "solid" and outline["width"] == "2px", (
        f"primary CTA must show a 2px solid focus ring, got {outline}"
    )
