"""Studio reading, retrieval, authority and plot-bounds regression journeys.

Use public controls and measured output, not test-only state setters. Public
ingress is the real dedicated listener (no tunnel or exposed credentials).
"""
from contextlib import contextmanager, nullcontext
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

from browser_support import copy_bundle, expect, launch_chromium, live_server, sync_playwright, managed_server, free_port
from okf_loom import Bundle
from okf_loom.render import build_site
from okf_loom.server import _start_public_ingress, _stop_public_ingress, _shutdown_retrieval_runtime
from test_tunnel import _app_server

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.browser


@pytest.fixture
def bundle_root(tmp_path):
    root = copy_bundle(ROOT / "samples/showcase", tmp_path / "bundle")
    (root / "reading.md").write_text(
        "---\ntype: Guide\ntitle: Reading guide\ndescription: A concise purpose.\n---\n\n"
        + "\n\n".join(f"## Section {n}\n\n" + (f"Readable paragraph {n}. " * 80) for n in range(1, 11))
    )
    return root


@contextmanager
def public_server(root, *, edit=False):
    server = _app_server(root)
    ingress = _start_public_ingress(server, allow_public_edit=edit)
    try:
        yield f"http://127.0.0.1:{ingress.server_address[1]}"
    finally:
        server.studio.bus.close()
        _stop_public_ingress(server)
        _shutdown_retrieval_runtime(server)
        server.server_close()


@pytest.fixture
def browser():
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        yield browser
        browser.close()


def test_search_submission_omits_unused_fields_and_error_keeps_url_state(bundle_root, browser):
    with live_server(bundle_root, "--no-watch") as base:
        page = browser.new_page()
        page.goto(base + "/__search")
        form = page.locator(".okf-search-workbench")
        form.locator('[name="q"]').fill("monthly revenue")
        form.locator('[name="mode"][value="hybrid"]').check()
        form.get_by_role("button", name="Search", exact=True).click()
        expect(page.locator(".okf-search-result").first).to_be_visible()
        values = parse_qs(urlsplit(page.url).query, keep_blank_values=True)
        assert values == {"q": ["monthly revenue"], "mode": ["hybrid"], "limit": ["20"]}
        # U4: only non-default advisory states ride on the result's meta
        # line. The showcase computation's stale_after (2026-09-23) is
        # calendar-bound, so Stale is deliberately not asserted either way.
        computation = page.locator(".okf-search-result").filter(
            has=page.get_by_role("heading", name="Demonstrative monthly revenue computation"))
        meta = computation.locator(".okf-search-result__meta")
        expect(meta.locator(".okf-search-advisory")).to_be_visible()
        expect(meta).to_contain_text("Advisory · Trust: machine-confirmed · Lifecycle: draft")
        results = page.locator(".okf-search-results-list")
        expect(results).not_to_contain_text("Trust: unverified")
        expect(results).not_to_contain_text("Lifecycle: stable")
        assert page.locator(".okf-search-source").first.is_hidden()
        form.locator('[name="mode"][value="entity"]').check()
        form.get_by_role("button", name="Search", exact=True).click()
        assert "entity=" not in page.url
        assert page.locator(".okf-search-error").count() == 0
        response = page.goto(base + "/__search?q=monthly+revenue&mode=hybrid&type=metric&tag=finance&limit=101")
        assert response.status == 400
        expect(form.locator('[name="q"]')).to_have_value("monthly revenue")
        expect(form.locator('[name="mode"][value="hybrid"]')).to_be_checked()
        expect(form.locator('[name="type"]')).to_have_value("metric")
        expect(form.locator('[name="tag"]')).to_have_value("finance")
        expect(form.locator('[name="limit"]')).to_have_value("101")
        assert page.request.get(base + "/__search?q=revenue&mode=hybrid&type=&format=json").status == 400
        page.close()


def test_native_nojs_form_redirects_to_strict_canonical_search(bundle_root, browser):
    with live_server(bundle_root, "--no-watch") as base:
        page = browser.new_page(java_script_enabled=False)
        page.goto(base + "/__search")
        form = page.locator(".okf-search-workbench")
        form.locator('[name="q"]').fill("monthly revenue")
        form.locator('[value="hybrid"]').check()
        form.get_by_role("button", name="Search", exact=True).click()
        expect(page.locator(".okf-search-result").first).to_be_visible()
        assert urlsplit(page.url).path == "/__search"
        assert not any(k in parse_qs(urlsplit(page.url).query) for k in ("type", "tag", "entity", "relation"))
        page.close()


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("mode", ["live", "static"])
def test_reading_anchor_focus_history_and_appearance(bundle_root, browser, tmp_path, width, mode):
    if mode == "static":
        site = tmp_path / "site"
        build_site(Bundle.load(bundle_root), site, target="static")
        owner = nullcontext("")
    else:
        owner = live_server(bundle_root, "--no-watch")
    # Static output uses file://, which also proves no live Studio requirement.
    with owner as base:
        page = browser.new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce")
        url = (site / "reading.html").as_uri() if mode == "static" else base + "/reading"
        page.goto(url)
        expect(page.locator(".okf-page__body #section-1")).to_be_visible()
        assert page.locator(".okf-page__body").bounding_box()["y"] < 720
        outline = page.locator('[data-okf-document-outline]')
        expect(outline).not_to_have_attribute("open", "")
        for section in (3, 6):
            if not outline.get_attribute("open") == "":
                outline.locator("summary").click()
            outline.locator(f'a[href="#section-{section}"]').click()
            expect(page.locator(f"#section-{section}")).to_be_focused()
            geometry = page.evaluate(f"""() => ({{
                heading: document.getElementById('section-{section}').getBoundingClientRect().top,
                shell: document.querySelector('.okf-topbar').getBoundingClientRect().bottom
            }})""")
            assert geometry["heading"] >= geometry["shell"] + 8, geometry
        page.go_back()
        expect(page.locator("#section-3")).to_be_focused()
        assert page.url.endswith("#section-3")
        page.locator("#okf-theme").click()
        menu = page.get_by_role("dialog", name="Appearance", exact=True)
        expect(menu.locator('[data-okf-set="theme"]:checked')).to_be_focused()
        menu.locator('[data-okf-set="theme"][value="slate"]').check()
        expect(page.locator("html")).to_have_attribute("data-theme", "slate")
        menu.locator('[data-okf-set="theme"][value="clay"]').check()
        page.keyboard.press("Escape")
        expect(page.locator("#okf-theme")).to_be_focused()
        page.reload()
        expect(page.locator("html")).to_have_attribute("data-theme", "clay")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.close()


@pytest.mark.parametrize("edit", [False, True])
def test_public_capability_never_polls_presence_or_offers_session_control(bundle_root, browser, edit):
    with public_server(bundle_root, edit=edit) as base:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        requests, errors = [], []
        page.on("request", lambda request: requests.append(urlsplit(request.url).path))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/__graph")
        expect(page.locator("body")).to_have_attribute("data-okf-studio-status", "ready")
        expect(page.locator(".okf-layout")).to_have_attribute("data-mobile-pane", "concepts")
        page.locator('[data-graph-pane="graph"]').click()
        page.locator("#okf-graph").hover()
        page.get_by_role("button", name="Studio", exact=False).first.click()
        expect(page.get_by_text("Authoring unavailable here", exact=True)).to_be_visible()
        page.get_by_text("Authoring unavailable here", exact=True).click()
        expect(page.get_by_text("An operator must open a local editable Studio", exact=False)).to_be_visible()
        assert not page.get_by_role("button", name="Agent watching", exact=False).is_visible()
        if not edit:
            expect(page.locator(".okf-studio-launcher__summary")).to_contain_text("Read-only")
            assert not page.get_by_text("Ask the agent to improve this concept", exact=True).is_visible()
        page.reload()
        expect(page.locator("body")).to_have_attribute("data-okf-studio-status", "ready")
        assert "/__presence" not in requests
        assert not errors
        page.close()


def test_graph_fit_uses_only_canvas_geometry_and_mobile_find_inspect_open(bundle_root, browser):
    with live_server(bundle_root, "--no-watch") as base:
        page = browser.new_page(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
        page.goto(base + "/__graph")
        expect(page.locator("body")).to_have_attribute("data-okf-capture-ready", "ready")
        def geometry():
            return page.evaluate("""() => {const cy=window.__okfLoomGraph.cy;
                const r=cy.nodes().renderedBoundingBox(); const c=document.getElementById('okf-graph');
                return {y:r.y1,h:r.h,ch:c.clientHeight,w:r.w,cw:c.clientWidth};}""")
        before = geometry()
        assert before["h"] > before["ch"] * .48, before
        assert before["y"] < before["ch"] * .3, before
        page.get_by_text("Graph settings", exact=True).click()
        page.get_by_role("button", name="Fit", exact=False).first.click()
        after = geometry()
        assert abs(after["h"] - before["h"]) < 2, (before, after)
        page.set_viewport_size({"width": 390, "height": 844})
        page.reload()
        expect(page.locator(".okf-layout")).to_have_attribute("data-mobile-pane", "concepts")
        page.get_by_role("searchbox", name="Search graph concepts").fill("monthly revenue")
        row = page.locator('.okf-node-index__item[data-target="metrics/monthly_revenue"]')
        expect(row).to_be_visible()
        row.click()
        # Selection exposes the textual inspection; the optional canvas is not
        # required to open or understand the selected concept.
        expect(page.locator(".okf-layout")).to_have_attribute("data-mobile-pane", "details")
        expect(page.locator("#detail-title")).to_contain_text("Monthly Revenue")
        expect(page.locator("#detail-title")).to_be_in_viewport()
        position = page.evaluate("""() => ({title:document.getElementById('detail-title').getBoundingClientRect().top,
            pane:document.getElementById('okf-detail').getBoundingClientRect().top,
            layout:document.querySelector('.okf-layout').getBoundingClientRect().top})""")
        assert position["title"] >= position["pane"] >= position["layout"], position
        page.locator("#detail-open-link").click()
        expect(page.locator(".okf-page__title")).to_contain_text("Monthly Revenue")
        page.close()


def test_local_readonly_observes_presence_without_publishing(bundle_root, browser):
    with live_server(bundle_root, "--no-watch", "--no-edit") as base:
        page = browser.new_page()
        writes = []
        page.on("request", lambda request: writes.append(request.method) if urlsplit(request.url).path == "/__presence" and request.method != "GET" else None)
        page.goto(base + "/__graph")
        page.wait_for_function("() => window.okfLoomLive?.currentPresenceSnapshot().presence_rev >= 0")
        page.locator("#okf-graph").hover()
        expect(page.locator(".okf-studio-launcher__summary")).to_contain_text("Read-only")
        page.reload()
        page.wait_for_function("() => window.okfLoomLive?.currentPresenceSnapshot().presence_rev >= 0")
        assert not writes
        page.close()


def test_home_purpose_and_context_preview_are_reader_facing(bundle_root, browser):
    with live_server(bundle_root, "--no-watch") as base:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(base + "/")
        expect(page.locator(".okf-index__title")).to_have_text("Northwind Coffee Knowledge Base")
        purpose = page.locator(".okf-index__intro")
        expect(purpose).to_contain_text("fictional specialty-coffee company")
        assert purpose.bounding_box()["y"] < page.locator(".okf-hero__visual").bounding_box()["y"]
        assert page.locator(".okf-card").count() == len(Bundle.load(bundle_root).concepts)
        page.goto(base + "/__search?q=monthly+revenue&mode=hybrid")
        page.locator(".okf-page-context > details > summary").click()
        page.locator("[data-context-prepare]").click()
        expect(page.get_by_role("region", name="Selected cited passages")).to_be_visible()
        expect(page.locator("[data-context-copy-prepared]")).to_be_enabled()
        assert page.locator("[data-context-preview]").locator("pre").count() > 0
        assert "#L" in page.locator("[data-context-preview]").inner_text()
        page.locator('.okf-search-workbench [name="q"]').fill("changed question")
        expect(page.locator("[data-context-copy-prepared]")).to_be_disabled()
        assert page.locator("[data-context-preview]").count() == 0
        page.close()


@pytest.mark.parametrize("mode", ["static", "single-file"])
def test_portable_cited_preview_copy_failure_and_invalidation(bundle_root, browser, tmp_path, mode):
    out = tmp_path / "portable"
    build_site(Bundle.load(bundle_root), out, target=mode)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    owner = managed_server([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(out)], base, tmp_path / "static.log") if mode == "static" else nullcontext()
    with owner:
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.add_init_script("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>Promise.reject(new DOMException('Copy denied','NotAllowedError'))}})")
        if mode == "static":
            page.goto(base + "/__search.html?q=monthly+revenue&mode=hybrid")
            expect(page.locator(".okf-search-result").first).to_be_visible()
            page.locator(".okf-page-context > details > summary").click()
            page.locator("[data-context-prepare]").click()
            preview = page.locator("[data-context-preview]")
            copy = page.locator("[data-context-copy-prepared]")
            fallback = page.locator("#okf-copy-fallback-text")
            budget = page.locator("[data-context-budget]")
        else:
            page.goto((out / "index.html").as_uri())
            page.locator("#okf-single-search-toggle").click()
            panel = page.locator("#okf-single-search")
            panel.locator('[name="q"]').fill("monthly revenue")
            panel.locator('[name="mode"][value="hybrid"]').check()
            panel.locator('button[type="submit"]').click()
            expect(panel.locator(".okf-search-result").first).to_be_visible()
            panel.locator("[data-single-page-context]").click()
            preview = panel.locator("[data-single-context-preview]")
            copy = panel.locator("[data-single-copy-pack]")
            fallback = panel.locator("#okf-single-copy-text")
            budget = panel.locator("[data-single-budget]")
        expect(preview).to_be_visible()
        assert "#L" in preview.inner_text()
        expect(copy).to_be_enabled()
        copy.click()
        expect(fallback).to_be_visible()
        assert "#L" in fallback.input_value()
        assert preview.locator("pre").first.inner_text() in fallback.input_value()
        page.get_by_text("Advanced: budget and related passages", exact=True).click()
        budget.fill("128")
        expect(copy).to_be_disabled()
        assert not preview.is_visible()
        assert fallback.input_value() == ""
        page.close()


@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("settlement", ["resolve", "reject"])
def test_single_clear_filters_revokes_preview_fallback_and_late_copy(
    bundle_root, browser, tmp_path, width, settlement,
):
    out = tmp_path / "single"
    build_site(Bundle.load(bundle_root), out, target="single-file")
    page = browser.new_page(viewport={"width": width, "height": 844})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>Promise.reject(new DOMException('Copy denied','NotAllowedError'))}})")
    try:
        page.goto((out / "index.html").as_uri())
        page.locator("#okf-single-search-toggle").click()
        panel = page.locator("#okf-single-search")
        panel.locator('[name="q"]').fill("monthly revenue")
        panel.locator('[name="mode"][value="hybrid"]').check()
        panel.locator(".okf-search-filters > summary").click()
        panel.locator('[name="type"]').fill("Metric")
        panel.locator('[name="tag"]').fill("finance")
        submit = panel.get_by_role("button", name="Search", exact=True)
        prepare = panel.locator("[data-single-page-context]")
        copy = panel.get_by_role("button", name="Copy context pack", exact=True)
        preview = panel.get_by_role("region", name="Selected cited passages", include_hidden=True)
        fallback = panel.locator("[data-single-copy-fallback]")
        text = panel.locator("#okf-single-copy-text")
        status = panel.locator("[data-single-copy-status]")
        clear = panel.get_by_role("button", name="Clear filters", exact=True)

        def assert_revoked():
            expect(preview).to_be_hidden()
            expect(preview).to_be_empty()
            expect(fallback).to_be_hidden()
            expect(text).to_have_value("")
            expect(copy).to_be_disabled()
            expect(copy).not_to_have_attribute("aria-busy", "true")
            expect(status).to_have_text("")

        submit.click()
        expect(panel.locator(".okf-search-result").first).to_be_visible()
        expect(panel.locator("[data-single-filter-count]")).to_have_text("2")
        prepare.click()
        expect(preview).to_be_visible()
        assert "#L" in preview.inner_text()
        expect(copy).to_be_enabled()
        preview.scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / "prepared-preview.png"))
        copy.click()
        expect(fallback).to_be_visible()
        assert preview.locator("pre").first.inner_text() in text.input_value()
        assert "#L" in text.input_value()
        text.scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / "denied-copy.png"))
        # A real keyboard reset must invalidate even though its synthetic form
        # change is not an input/select event and does not bubble to the panel.
        clear.focus()
        clear.press("Enter")
        expect(clear).to_be_focused()
        assert_revoked()
        expect(panel.locator('[name="type"]')).to_have_value("")
        expect(panel.locator('[name="tag"]')).to_have_value("")
        expect(panel.locator("[data-single-filter-count]")).to_have_text("0")
        expect(panel.locator('[name="q"]')).to_have_value("monthly revenue")
        expect(panel.locator('[name="mode"][value="hybrid"]')).to_be_checked()
        submit.click()
        expect(panel.locator(".okf-search-result").first).to_be_visible()
        assert_revoked()
        copy.scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / "clear-resubmitted.png"))

        # Hold only the external clipboard boundary; prepare/search/reset stay real.
        panel.locator('[name="type"]').fill("Metric")
        submit.click()
        prepare.click()
        expect(preview).to_be_visible()
        page.evaluate("""() => {
            window.__lateSingle = {};
            Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{
                writeText: () => new Promise((resolve, reject) => {
                    window.__lateSingle.resolve = resolve;
                    window.__lateSingle.reject = reject;
                })
            }});
        }""")
        copy.click()
        busy_copy = panel.locator("[data-single-copy-pack]")
        expect(busy_copy).to_have_attribute("aria-busy", "true")
        clear.click()
        assert_revoked()
        submit.click()
        page.evaluate("outcome => { window.__lateSingle[outcome](); }", settlement)
        assert_revoked()
        expect(clear).to_be_enabled()
        expect(submit).to_be_focused()
        assert not errors
    finally:
        page.close()
