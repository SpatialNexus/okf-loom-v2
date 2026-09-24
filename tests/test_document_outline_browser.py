"""Focused F03/F04/F18 document traversal and local-navigation proof."""
from __future__ import annotations

from pathlib import Path
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request

import pytest
from okf_loom import Bundle
from okf_loom.render import build_site, render_single_file
from conftest import okf_module_argv, okf_subprocess_env

from browser_support import sync_playwright, launch_chromium, live_server


ROOT = Path(__file__).resolve().parents[1]


def _long_bundle(tmp_path: Path) -> Bundle:
    root = tmp_path / "bundle"
    root.mkdir()
    sections = "\n\n".join(
        "## Section {index}\n\n{prose}".format(
            index=index,
            prose="\n\n".join(
                f"Paragraph {paragraph} of section {index} keeps this document long enough that its later sections sit well below the fold."
                for paragraph in range(1, 16)
            ),
        )
        for index in range(1, 11)
    )
    (root / "guide.md").write_text(
        "---\ntype: Guide\ntitle: Long guide\ndescription: Traverse this document.\n---\n\n"
        + sections + "\n",
        encoding="utf-8",
    )
    (root / "index.md").write_text("# Index\n\n- [Long guide](guide.md)\n", encoding="utf-8")
    return Bundle.load(root)


def test_static_concept_has_one_ssr_outline_owner_and_end_paths(tmp_path: Path) -> None:
    bundle = _long_bundle(tmp_path)
    out = tmp_path / "site"
    build_site(bundle, out, target="static")
    html = (out / "guide.html").read_text(encoding="utf-8")

    assert "document-outline.js" in html
    assert html.count('data-okf-document-outline') == 1
    assert html.count('href="#section-') == 10
    assert 'aria-label="Document end"' in html
    assert 'href="#okf-document-details"' in html
    assert '>Return to Index<' in html


def test_outline_contract_survives_spa_single_file_and_nojs_markup(tmp_path: Path) -> None:
    bundle = _long_bundle(tmp_path)
    static = tmp_path / "static"
    spa = tmp_path / "spa"
    single = tmp_path / "single.html"
    build_site(bundle, static, target="static")
    build_site(bundle, spa, target="spa")
    render_single_file(bundle, single)

    for html in (
        (static / "guide.html").read_text(encoding="utf-8"),
        (spa / "guide.html").read_text(encoding="utf-8"),
    ):
        assert 'data-okf-document-outline' in html
        assert html.count('href="#section-') == 10
        assert 'data-okf-long-document="true" data-okf-heading-threshold="8"' in html
    single_html = single.read_text(encoding="utf-8")
    assert "One document-local owner for concept heading traversal" in single_html


def test_large_connection_inventory_collapses_with_complete_count(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    links = "\n".join(f"- [Related {index}](related-{index}.md)" for index in range(1, 10))
    (root / "guide.md").write_text(
        "---\ntype: Guide\ntitle: Guide\n---\n\n# Guide\n\n" + links + "\n",
        encoding="utf-8",
    )
    for index in range(1, 10):
        (root / f"related-{index}.md").write_text(
            f"---\ntype: Guide\ntitle: Related {index}\n---\n\nBody.\n",
            encoding="utf-8",
        )
    bundle = Bundle.load(root)
    out = tmp_path / "site"
    build_site(bundle, out, target="static")
    html = (out / "guide.html").read_text(encoding="utf-8")
    assert '<details class="okf-connections-disclosure">' in html
    assert '<summary>Connections (9)</summary>' in html
    assert html.count('class="okf-conn"') == 9


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _headingless_bundle(tmp_path: Path) -> None:
    """A genuinely headingless concept (the F8 starting condition)."""
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "note.md").write_text(
        "---\ntype: Note\ntitle: Field note\ndescription: Starts headingless.\n---\n\n"
        "Plain observation prose with no headings at all.\n",
        encoding="utf-8",
    )
    (root / "index.md").write_text("# Index\n\n- [Field note](note.md)\n", encoding="utf-8")
    return Bundle.load(root)


def _outline_selection_bundle(tmp_path: Path):
    """Concepts with different heading shapes for selected-document binding."""
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "alpha.md").write_text(
        "---\ntype: Guide\ntitle: Alpha\ndescription: Two sections.\n---\n\n"
        "## Alpha one\n\nAlpha first section.\n\n## Alpha two\n\nAlpha second section.\n",
        encoding="utf-8",
    )
    (root / "beta.md").write_text(
        "---\ntype: Guide\ntitle: Beta\ndescription: One section.\n---\n\n"
        "## Beta one\n\nBeta only section.\n",
        encoding="utf-8",
    )
    (root / "gamma.md").write_text(
        "---\ntype: Note\ntitle: Gamma\ndescription: Headingless.\n---\n\n"
        "Gamma has no headings.\n",
        encoding="utf-8",
    )
    (root / "index.md").write_text(
        "# Index\n\n- [Alpha](alpha.md)\n- [Beta](beta.md)\n- [Gamma](gamma.md)\n",
        encoding="utf-8",
    )
    return Bundle.load(root)


def _reserved_round_trip_bundle(tmp_path: Path):
    """Concept plus reserved index document for presented-document binding.

    ``alpha`` links to the reserved root index (the supported single-file
    route into the sibling reserved pane), and the index carries two headings
    so the reserved pane owns a distinct outline."""
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "alpha.md").write_text(
        "---\ntype: Guide\ntitle: Alpha\ndescription: Two sections.\n---\n\n"
        "## Alpha one\n\nAlpha first section.\n\n## Alpha two\n\n"
        "See the [bundle index](index.md).\n",
        encoding="utf-8",
    )
    (root / "gamma.md").write_text(
        "---\ntype: Note\ntitle: Gamma\ndescription: Headingless.\n---\n\n"
        "Gamma has no headings.\n",
        encoding="utf-8",
    )
    (root / "index.md").write_text(
        "# Bundle index\n\n- [Alpha](alpha.md)\n\n"
        "## Reserved sections\n\nThe reserved index carries its own headings.\n",
        encoding="utf-8",
    )
    return Bundle.load(root)


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """Run one supported CLI mutation against a scratch served bundle."""
    return subprocess.run(
        okf_module_argv(*args),
        cwd=ROOT,
        env=okf_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _focus_by_tabbing(page, target_selector: str, limit: int = 40) -> None:
    """Reach a control with real Tab traversal from the shell brand link."""
    page.evaluate("() => { const b = document.querySelector('.okf-brand-link'); if (b) b.focus(); }")
    for _ in range(limit):
        if page.evaluate(
            "() => !!document.activeElement && !!document.activeElement.closest('"
            + target_selector
            + "')"
        ):
            return
        page.keyboard.press("Tab")
    page.evaluate("() => { throw new Error('Tab never reached " + target_selector + "'); }")


@pytest.fixture
def long_document_page(tmp_path: Path):
    bundle = _long_bundle(tmp_path)
    with sync_playwright() as playwright, live_server(bundle.root, "--no-watch") as base:
        browser = launch_chromium(playwright)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.goto(base + "/guide", wait_until="load")
            yield page
        finally:
            browser.close()


@pytest.mark.browser
def test_outline_filter_roving_keys_and_compact_escape(long_document_page) -> None:
    page = long_document_page
    outline = page.locator("[data-okf-document-outline]")
    assert outline.count() == 1
    outline.locator("summary").click()
    filter_input = outline.locator("input[type=search]")
    assert filter_input.count() == 1
    links = outline.locator("a.okf-onthispage__link")
    assert links.count() == 10
    assert page.evaluate(
        "() => Array.from(document.querySelectorAll('[data-okf-document-outline] a')).filter(a => a.tabIndex === 0).length"
    ) == 1

    links.first.focus()
    page.keyboard.press("End")
    assert page.evaluate("() => document.activeElement.textContent.trim()") == "Section 10"
    page.keyboard.press("Home")
    assert page.evaluate("() => document.activeElement.textContent.trim()") == "Section 1"
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => document.activeElement.textContent.trim()") == "Section 2"
    page.keyboard.press("Enter")
    assert links.nth(1).get_attribute("aria-current") == "location"

    page.set_viewport_size({"width": 360, "height": 700})
    page.wait_for_timeout(50)
    page.evaluate("() => { const d=document.querySelector('[data-okf-document-outline]'); d.open=true; }")
    filter_input.fill("Section 9")
    filter_input.press("Escape")
    assert filter_input.input_value() == ""
    assert outline.get_attribute("open") == ""
    filter_input.press("Escape")
    assert outline.get_attribute("open") is None
    assert page.evaluate("() => document.activeElement.tagName") == "SUMMARY"


@pytest.mark.browser
def test_persisted_bfcache_restore_rebuilds_fresh_outline_owner(long_document_page) -> None:
    page = long_document_page
    outline = page.locator("[data-okf-document-outline]")
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")
    page.set_viewport_size({"width": 360, "height": 700})
    page.wait_for_timeout(50)
    page.evaluate("() => { const d=document.querySelector('[data-okf-document-outline]'); d.open=true; }")
    field = outline.locator("input[type=search]")
    field.fill("Section 3")
    target = outline.locator('a[href="#section-3"]')
    target.focus()
    before = page.evaluate("() => window.okfDocumentOutline.generation")

    frozen = page.evaluate("""() => {
      dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));
      return {generation:window.okfDocumentOutline.generation, observer:window.okfDocumentOutline.observerActive};
    }""")
    assert frozen == {"generation": before + 1, "observer": False}

    restored = page.evaluate("""() => {
      dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
      const d=document.querySelector('[data-okf-document-outline]');
      return {generation:window.okfDocumentOutline.generation,observer:window.okfDocumentOutline.observerActive,
        open:d.open,filter:d.querySelector('input[type=search]').value,
        focused:document.activeElement&&document.activeElement.getAttribute('href'),
        current:d.querySelector('[aria-current=location]')?.getAttribute('href')};
    }""")
    assert restored == {
        "generation": before + 2,
        "observer": True,
        "open": True,
        "filter": "Section 3",
        "focused": "#section-3",
        "current": "#section-1",
    }


@pytest.mark.browser
def test_body_patch_replaces_outline_lifecycle_and_preserves_view_owner(long_document_page) -> None:
    page = long_document_page
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")
    outline = page.locator("[data-okf-document-outline]")
    outline.locator("summary").click()
    target = outline.locator('a[href="#section-3"]')
    target.focus()
    before = page.evaluate("() => window.okfDocumentOutline.generation")

    survived = page.evaluate(
        """() => {
          const body = document.querySelector('.okf-page__body');
          const added = document.createElement('h2');
          added.id = 'patched-section';
          added.textContent = 'Patched section';
          body.appendChild(added);
          window.dispatchEvent(new CustomEvent('okf-loom:bodyPatched'));
          return {
            generation: window.okfDocumentOutline.generation,
            observer: window.okfDocumentOutline.observerActive,
            focused: document.activeElement && document.activeElement.getAttribute('href'),
            patched: !!document.querySelector('[data-okf-document-outline] a[href="#patched-section"]'),
          };
        }"""
    )
    assert survived == {
        "generation": before + 1,
        "observer": True,
        "focused": "#section-3",
        "patched": True,
    }

    fallback = page.evaluate(
        """() => {
          document.getElementById('section-3').remove();
          window.dispatchEvent(new CustomEvent('okf-loom:bodyPatched'));
          return {
            tag: document.activeElement && document.activeElement.tagName,
            inside: document.querySelector('[data-okf-document-outline]').contains(document.activeElement),
          };
        }"""
    )
    assert fallback == {"tag": "SUMMARY", "inside": True}

    page.locator("[data-okf-live-utilities] button", has_text="Source").click()
    assert "view=source" in page.url
    assert page.locator(".okf-source").is_visible()
    page.evaluate("() => window.dispatchEvent(new CustomEvent('okf-loom:bodyPatched'))")
    assert "view=source" in page.url
    assert page.locator(".okf-source").is_visible()
    assert page.locator('.okf-sidebar-panel[data-panel-id="sections"]').count() == 0

    zero = page.evaluate(
        """() => {
          document.querySelector('.okf-page__body').innerHTML = '<p>No headings.</p>';
          window.dispatchEvent(new CustomEvent('okf-loom:bodyPatched'));
          return {
            hidden: document.querySelector('[data-okf-document-outline]').hidden,
            observer: window.okfDocumentOutline.observerActive,
          };
        }"""
    )
    assert zero == {"hidden": True, "observer": False}


@pytest.mark.browser
def test_outline_reflows_without_overlap_at_desktop_and_zoom_equivalents(long_document_page) -> None:
    page = long_document_page
    page.emulate_media(reduced_motion="reduce")
    for width in (1440, 1280, 720, 360):
        page.set_viewport_size({"width": width, "height": 800})
        page.wait_for_timeout(30)
        geometry = page.evaluate(
            """() => {
              const outline = document.querySelector('[data-okf-document-outline]');
              const header = document.querySelector('.okf-page__header');
              const utilities = document.querySelector('.okf-page-utilities');
              const root = document.documentElement;
              const o = outline.getBoundingClientRect();
              const h = header.getBoundingClientRect();
              const u = utilities.getBoundingClientRect();
              return {
                outlineVisible: getComputedStyle(outline).display !== 'none',
                afterHeader: o.top >= h.top,
                overflow: root.scrollWidth > root.clientWidth + 1,
                utilityOverlap: !(u.bottom <= o.top || u.top >= o.bottom || u.right <= o.left || u.left >= o.right),
                reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
              };
            }"""
        )
        assert geometry["outlineVisible"] is True
        assert geometry["afterHeader"] is True
        assert geometry["overflow"] is False
        assert geometry["utilityOverlap"] is False
        assert geometry["reduced"] is True


@pytest.mark.browser
def test_related_outline_and_proposal_operations_have_separate_owners(long_document_page) -> None:
    page = long_document_page
    assert page.locator('.okf-sidebar-panel[data-panel-id="related"]').count() == 1
    assert page.locator('.okf-sidebar-panel[data-panel-id="sections"]').count() == 0
    assert page.locator('.okf-sidebar-panel[data-panel-id="intents"]').count() == 0
    page.locator(".okf-studio-launcher").click()
    panel = page.locator("#okf-panel")
    assert panel.get_by_role("heading", name="Ask the agent to improve this concept").count() == 1
    assert panel.get_by_role("button", name="Draft a new section").count() == 1


@pytest.mark.browser
def test_headingless_outline_gains_links_after_real_cli_mutation_without_reload(tmp_path: Path) -> None:
    """F8 end-to-end: a genuinely headingless document carries the stable
    hidden host (no chrome, no observer); a REAL supported CLI mutation adds
    headings; the terminal body patch is verified to have arrived; and the
    outline (host, links, traversal) appears WITHOUT a reload. Repeated
    0 -> many -> 0 -> many transitions keep one owner, one nav, and one
    observer, and a reload reproduces the same outline."""
    bundle = _headingless_bundle(tmp_path)
    with sync_playwright() as playwright, live_server(bundle.root) as base:
        browser = launch_chromium(playwright)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.goto(base + "/note", wait_until="load")
            page.wait_for_function(
                "() => window.okfStudioReadiness && window.okfStudioReadiness.ready"
            )

            initial = page.evaluate("""() => {
                  const host = document.querySelector('[data-okf-document-outline]');
                  const style = getComputedStyle(host);
                  return {
                    hidden: host.hidden,
                    display: style.display,
                    empty: host.hasAttribute('data-okf-outline-empty'),
                    links: host.querySelectorAll('a.okf-onthispage__link').length,
                    navs: host.querySelectorAll('.okf-onthispage__nav').length,
                    observer: window.okfDocumentOutline.observerActive,
                    viewbarTop: document.documentElement.style.getPropertyValue('--okf-viewbar-top'),
                  };
                }""")
            assert initial == {
                "hidden": True,
                "display": "none",
                "empty": True,
                "links": 0,
                "navs": 0,
                "observer": False,
                "viewbarTop": initial["viewbarTop"],
            }
            assert initial["viewbarTop"] != "", "geometry must stay published while headingless"
            page.evaluate("() => { window.__outlineNoReload = true; }")

            mutation = _run_cli(
                "update-section", "--bundle", str(bundle.root), "--id", "note",
                "--heading", "Findings", "--create-if-missing",
                "--body", "First recorded finding.\n\n## Next steps\n\nPlanned work.",
            )
            assert mutation.returncode == 0, str(mutation.stderr)
            page.wait_for_function(
                "() => !!document.querySelector('.okf-page__body #next-steps')",
                timeout=30000,
            )
            page.wait_for_function("() => window.okfDocumentOutline.observerActive", timeout=10000)
            populated = page.evaluate("""() => {
                  const host = document.querySelector('[data-okf-document-outline]');
                  const links = Array.from(host.querySelectorAll('a.okf-onthispage__link'));
                  return {
                    hidden: host.hidden,
                    empty: host.hasAttribute('data-okf-outline-empty'),
                    hrefs: links.map(a => a.getAttribute('href')),
                    navs: host.querySelectorAll('.okf-onthispage__nav').length,
                    noReload: window.__outlineNoReload === true,
                  };
                }""")
            assert populated == {
                "hidden": False,
                "empty": False,
                "hrefs": ["#findings", "#next-steps"],
                "navs": 1,
                "noReload": True,
            }

            page.locator("[data-okf-document-outline] summary").click()
            page.locator('[data-okf-document-outline] a[href="#next-steps"]').click()
            traversal = page.evaluate("""() => {
                  const target = document.getElementById('next-steps');
                  const bar = document.querySelector('.okf-concept-viewbar');
                  return {
                    focused: document.activeElement === target,
                    outsideChrome: target.getBoundingClientRect().top >= bar.getBoundingClientRect().bottom - 1,
                  };
                }""")
            assert traversal == {"focused": True, "outsideChrome": True}

            removal = _run_cli(
                "write-concept", "--bundle", str(bundle.root), "--id", "note",
                "--type", "Note", "--title", "Field note", "--force",
                "--body", "Headingless once more.",
            )
            assert removal.returncode == 0, str(removal.stderr)
            page.wait_for_function(
                "() => !window.okfDocumentOutline.observerActive", timeout=30000
            )
            emptied = page.evaluate("""() => {
                  const host = document.querySelector('[data-okf-document-outline]');
                  return {
                    hidden: host.hidden,
                    links: host.querySelectorAll('a.okf-onthispage__link').length,
                    navs: host.querySelectorAll('.okf-onthispage__nav').length,
                    hosts: document.querySelectorAll('[data-okf-document-outline]').length,
                  };
                }""")
            assert emptied == {"hidden": True, "links": 0, "navs": 0, "hosts": 1}

            revival = _run_cli(
                "update-section", "--bundle", str(bundle.root), "--id", "note",
                "--heading", "Findings", "--create-if-missing",
                "--body", "Revived finding.",
            )
            assert revival.returncode == 0, str(revival.stderr)
            page.wait_for_function(
                "() => window.okfDocumentOutline.observerActive && document.querySelectorAll('[data-okf-document-outline] a.okf-onthispage__link').length === 1",
                timeout=30000,
            )
            revived = page.evaluate("""() => {
                  const host = document.querySelector('[data-okf-document-outline]');
                  const links = Array.from(host.querySelectorAll('a.okf-onthispage__link'));
                  return {
                    hidden: host.hidden,
                    hrefs: links.map(a => a.getAttribute('href')),
                    navs: host.querySelectorAll('.okf-onthispage__nav').length,
                    noReload: window.__outlineNoReload === true,
                  };
                }""")
            assert revived == {
                "hidden": False,
                "hrefs": ["#findings"],
                "navs": 1,
                "noReload": True,
            }

            page.reload(wait_until="load")
            page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")
            reloaded = page.evaluate("""() => {
                  const host = document.querySelector('[data-okf-document-outline]');
                  return {
                    hidden: host.hidden,
                    links: host.querySelectorAll('a.okf-onthispage__link').length,
                    observer: window.okfDocumentOutline.observerActive,
                  };
                }""")
            assert reloaded == {"hidden": False, "links": 1, "observer": True}
        finally:
            browser.close()


@pytest.mark.browser
def test_viewbar_sections_popover_reaches_below_fold_and_lands_outside_chrome(long_document_page) -> None:
    """U3: on a long document the pinned viewbar carries ONE 'Sections'
    control (default collapsed). Far below the fold it stays keyboard
    reachable, shows the current section, opens a popover list driven by the
    outline's own state, and Escape closes it with focus returned to the
    toggle. Activating a section lands its heading outside the sticky chrome.
    Compact widths keep one control, a viewport-fitting panel, and no
    document overflow."""
    page = long_document_page
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")
    jumps = page.locator("[data-okf-outline-jump]")
    assert jumps.count() == 1, "exactly one viewbar Sections control"
    toggle = page.locator("[data-okf-outline-jump] button")
    panel = page.locator("#okf-outline-jump-panel")
    assert toggle.get_attribute("aria-expanded") == "false"
    assert panel.is_hidden()

    def open_and_check_current():
        page.evaluate("() => document.getElementById('section-8').scrollIntoView({block: 'start'})")
        page.wait_for_function("""() => {
              const rect = document.querySelector('[data-okf-document-outline]').getBoundingClientRect();
              return rect.bottom < 0;
            }""", timeout=5000)
        _focus_by_tabbing(page, "[data-okf-outline-jump]")
        page.keyboard.press("Enter")
        assert panel.is_visible()
        assert toggle.get_attribute("aria-expanded") == "true"
        page.wait_for_function("""() => {
              const link = document.querySelector('#okf-outline-jump-panel a[aria-current="location"]');
              return link && link.getAttribute('href') === '#section-8';
            }""", timeout=5000)
        current = page.evaluate("() => document.querySelector('[data-okf-outline-jump] .okf-outline-jump__current').textContent")
        assert current == "Section 8"

    open_and_check_current()
    page.keyboard.press("Escape")
    assert panel.is_hidden()
    assert toggle.get_attribute("aria-expanded") == "false"
    assert page.evaluate(
        "() => document.activeElement === document.querySelector('[data-okf-outline-jump] button')"
    )
    assert page.locator("[data-okf-document-outline]").get_attribute("open") is None

    page.evaluate("() => window.scrollTo(0, 0)")
    _focus_by_tabbing(page, "[data-okf-outline-jump]")
    page.keyboard.press("Enter")
    assert panel.is_visible()
    first = page.evaluate("() => document.activeElement.getAttribute('href')")
    assert first == "#section-1"
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => document.activeElement.getAttribute('href')") == "#section-2"
    page.keyboard.press("End")
    assert page.evaluate("() => document.activeElement.getAttribute('href')") == "#section-10"
    page.evaluate("() => document.querySelector('#okf-outline-jump-panel a[href=\\'#section-8\\']').focus()")
    page.keyboard.press("Enter")
    assert panel.is_hidden()
    landing = page.evaluate("""() => {
          const target = document.getElementById('section-8');
          const bar = document.querySelector('.okf-concept-viewbar');
          return {
            focused: document.activeElement === target,
            outsideChrome: target.getBoundingClientRect().top >= bar.getBoundingClientRect().bottom - 1,
            hash: location.hash,
          };
        }""")
    assert landing == {"focused": True, "outsideChrome": True, "hash": "#section-8"}

    page.set_viewport_size({"width": 360, "height": 700})
    page.wait_for_timeout(80)
    assert jumps.count() == 1
    page.evaluate("() => document.getElementById('section-9').scrollIntoView({block: 'start'})")
    _focus_by_tabbing(page, "[data-okf-outline-jump]")
    page.keyboard.press("Enter")
    assert panel.is_visible()
    compact = page.evaluate("""() => {
          const root = document.documentElement;
          const panel = document.getElementById('okf-outline-jump-panel');
          const rect = panel.getBoundingClientRect();
          return {
            overflow: root.scrollWidth > root.clientWidth + 1,
            insideViewport: rect.left >= 0 && rect.right <= window.innerWidth + 1,
            current: document.querySelector('#okf-outline-jump-panel a[aria-current="location"]'),
          };
        }""")
    assert compact["overflow"] is False
    assert compact["insideViewport"] is True
    assert compact["current"] is not None


@pytest.mark.browser
def test_disclosure_filter_leaves_sections_popover_complete(long_document_page) -> None:
    """U3 filter scoping: the near-header disclosure filter narrows ONLY the
    disclosure list. With a subset and with zero matches, the persistent
    viewbar Sections popover below the fold still presents every section,
    keeps its own roving stop, and activates a real destination; the
    disclosure filter stays locally recoverable."""
    page = long_document_page
    page.wait_for_function("() => window.okfStudioReadiness && window.okfStudioReadiness.ready")
    outline = page.locator("[data-okf-document-outline]")
    outline.locator("summary").click()
    filter_input = outline.locator("input[type=search]")
    panel = page.locator("#okf-outline-jump-panel")

    def visible_hrefs(scope: str):
        return page.evaluate(
            "() => Array.from(document.querySelectorAll('"
            + scope
            + " a.okf-onthispage__link')).filter(a => !a.closest('li').hidden).map(a => a.getAttribute('href'))"
        )

    all_sections = ["#section-%d" % index for index in range(1, 11)]

    filter_input.fill("Section 3")
    assert visible_hrefs("[data-okf-document-outline]") == ["#section-3"]
    assert visible_hrefs("#okf-outline-jump-panel") == all_sections

    filter_input.fill("no such section")
    assert visible_hrefs("[data-okf-document-outline]") == []
    assert outline.locator(".okf-onthispage__empty").is_visible()
    assert visible_hrefs("#okf-outline-jump-panel") == all_sections

    # Collapse the filtered disclosure and drop way below the fold: the
    # popover is the only wayfinding surface left, so it must be complete.
    outline.locator("summary").click()
    assert outline.get_attribute("open") is None
    page.evaluate("() => document.getElementById('section-8').scrollIntoView({block: 'start'})")
    page.wait_for_function("""() => {
          const rect = document.querySelector('[data-okf-document-outline]').getBoundingClientRect();
          return rect.bottom < 0;
        }""", timeout=5000)
    _focus_by_tabbing(page, "[data-okf-outline-jump]")
    page.keyboard.press("Enter")
    assert panel.is_visible()
    popover = page.evaluate("""() => {
          const links = Array.from(document.querySelectorAll('#okf-outline-jump-panel a.okf-onthispage__link'));
          return {
            total: links.length,
            visible: links.filter(a => !a.closest('li').hidden).length,
            roving: links.filter(a => a.tabIndex === 0).length,
          };
        }""")
    assert popover == {"total": 10, "visible": 10, "roving": 1}
    page.keyboard.press("End")
    assert page.evaluate("() => document.activeElement.getAttribute('href')") == "#section-10"
    page.evaluate("() => document.querySelector('#okf-outline-jump-panel a[href=\\'#section-5\\']').focus()")
    page.keyboard.press("Enter")
    assert panel.is_hidden()
    landing = page.evaluate("""() => {
          const target = document.getElementById('section-5');
          const bar = document.querySelector('.okf-concept-viewbar');
          return {
            focused: document.activeElement === target,
            outsideChrome: target.getBoundingClientRect().top >= bar.getBoundingClientRect().bottom - 1,
          };
        }""")
    assert landing == {"focused": True, "outsideChrome": True}
    # The popover never inherited (nor cleared) the disclosure's filter.
    assert filter_input.input_value() == "no such section"


@pytest.mark.browser
def test_graph_detail_outline_tracks_selected_documents(tmp_path: Path) -> None:
    """U3 selected-document binding: the graph detail presents the outline of
    whichever document is selected. A -> B -> headingless -> A keeps the
    current targets, one nav, and a live observer — no stale links or old
    callbacks from the previously selected document. The host must stay
    VISIBLE beside the presented body across every transition (clearing the
    selection included), and outline links must actually traverse."""
    bundle = _outline_selection_bundle(tmp_path)
    with sync_playwright() as playwright, live_server(bundle.root) as base:
        browser = launch_chromium(playwright)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.goto(base + "/__graph", wait_until="load")
            page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.interaction")
            select = lambda concept_id: page.evaluate(
                "(id) => window.__okfLoomGraph.interaction.dispatch({type: 'SELECT', id, source: 'test'})",
                concept_id,
            )
            select("alpha")
            page.wait_for_function("""() => {
                  const host = document.querySelector('#detail-content [data-okf-document-outline]');
                  return !!host && !host.hidden &&
                    host.querySelectorAll('a.okf-onthispage__link').length === 2;
                }""", timeout=10000)
            alpha_state = page.evaluate("""() => {
                  const host = document.querySelector('#detail-content [data-okf-document-outline]');
                  return {
                    hrefs: Array.from(host.querySelectorAll('a.okf-onthispage__link')).map(a => a.getAttribute('href')),
                    navs: host.querySelectorAll('.okf-onthispage__nav').length,
                    observer: window.okfDocumentOutline.observerActive,
                  };
                }""")
            assert alpha_state == {
                "hrefs": ["#alpha-one", "#alpha-two"],
                "navs": 1,
                "observer": True,
            }
            assert page.locator("[data-okf-document-outline]").is_visible()
            assert page.evaluate(
                """() => {
                  const host = document.querySelector('[data-okf-document-outline]');
                  const displayed = document.getElementById('detail-body');
                  return host.parentNode === displayed.parentNode &&
                    host.nextElementSibling === displayed && !displayed.closest('[hidden]');
                }"""
            )

            page.locator("#detail-content [data-okf-document-outline] summary").click()
            page.locator('#detail-content a[href="#alpha-two"]').click()
            assert page.evaluate("() => document.activeElement.id") == "alpha-two"

            select("beta")
            page.wait_for_function("""() => {
                  const links = document.querySelectorAll('#detail-content a.okf-onthispage__link');
                  return links.length === 1 && links[0].getAttribute('href') === '#beta-one';
                }""")
            beta_navs = page.evaluate("() => document.querySelectorAll('#detail-content [data-okf-document-outline] .okf-onthispage__nav').length")
            assert beta_navs == 1
            assert page.locator("[data-okf-document-outline]").is_visible()

            select("gamma")
            page.wait_for_function("""() => {
                  const host = document.querySelector('#detail-content [data-okf-document-outline]');
                  return !!host && !!host.hidden && !window.okfDocumentOutline.observerActive;
                }""")
            assert not page.locator("[data-okf-document-outline]").is_visible()

            select("alpha")
            page.wait_for_function("""() => {
                  const links = document.querySelectorAll('#detail-content a.okf-onthispage__link');
                  return links.length === 2 &&
                    links[0].getAttribute('href') === '#alpha-one' &&
                    links[1].getAttribute('href') === '#alpha-two';
                }""")
            revived = page.evaluate("""() => {
                  const host = document.querySelector('#detail-content [data-okf-document-outline]');
                  const navs = document.querySelectorAll('#detail-content [data-okf-document-outline]').length;
                  return {hidden: host.hidden, hosts: navs, observer: window.okfDocumentOutline.observerActive};
                }""")
            assert revived == {"hidden": False, "hosts": 1, "observer": True}
            # The revived outline is genuinely presented again and traverses.
            assert page.locator("[data-okf-document-outline]").is_visible()
            page.locator("#detail-content [data-okf-document-outline] summary").click()
            page.locator('#detail-content a[href="#alpha-two"]').click()
            assert page.evaluate("() => document.activeElement.id") == "alpha-two"

            # Clearing the selection presents no document: the outline must
            # not stay visible, and the next selection must revive it beside
            # the newly presented body.
            page.evaluate(
                "() => window.__okfLoomGraph.interaction.dispatch({type: 'CLEAR', quietUrl: true, source: 'test'})"
            )
            assert page.locator("#detail-content").is_hidden()
            assert not page.locator("[data-okf-document-outline]").is_visible()
            select("beta")
            page.wait_for_function("""() => {
                  const links = document.querySelectorAll('#detail-content a.okf-onthispage__link');
                  return links.length === 1 && links[0].getAttribute('href') === '#beta-one';
                }""")
            assert page.locator("[data-okf-document-outline]").is_visible()
            assert page.evaluate(
                """() => {
                  const host = document.querySelector('[data-okf-document-outline]');
                  const displayed = document.getElementById('detail-body');
                  return host.parentNode === displayed.parentNode &&
                    host.nextElementSibling === displayed && !displayed.closest('[hidden]');
                }"""
            )
        finally:
            browser.close()


@pytest.mark.browser
def test_single_file_outline_tracks_selected_documents(tmp_path: Path) -> None:
    """U3 selected-document binding in the self-contained target: concept
    selection through the public index seam presents the selected document's
    outline, and A -> headingless -> A keeps targets current."""
    bundle = _outline_selection_bundle(tmp_path)
    single = tmp_path / "single.html"
    render_single_file(bundle, single)
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.goto(single.as_uri(), wait_until="load")
            page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.interaction")
            page.locator('a[data-okf-single-concept="alpha"]').click()
            page.wait_for_function("""() => {
                  const host = document.querySelector('#detail-content [data-okf-document-outline]');
                  return !!host && !host.hidden &&
                    host.querySelectorAll('a.okf-onthispage__link').length === 2;
                }""", timeout=15000)
            assert page.evaluate("() => Array.from(document.querySelectorAll('#detail-content a.okf-onthispage__link')).map(a => a.getAttribute('href'))") == [
                "#alpha-one",
                "#alpha-two",
            ]

            page.locator("#detail-content [data-okf-document-outline] summary").click()
            page.locator('#detail-content a[href="#alpha-two"]').click()
            assert page.evaluate("() => document.activeElement.id") == "alpha-two"

            page.evaluate("() => window.__okfLoomGraph.interaction.dispatch({type: 'SELECT', id: 'gamma', source: 'test'})")
            page.wait_for_function("""() => {
                  const host = document.querySelector('#detail-content [data-okf-document-outline]');
                  return !!host && !!host.hidden && !window.okfDocumentOutline.observerActive;
                }""", timeout=10000)

            page.evaluate("() => window.__okfLoomGraph.interaction.dispatch({type: 'SELECT', id: 'alpha', source: 'test'})")
            page.wait_for_function("""() => {
                  const links = document.querySelectorAll('#detail-content a.okf-onthispage__link');
                  return links.length === 2 && links[1].getAttribute('href') === '#alpha-two';
                }""")
        finally:
            browser.close()


@pytest.mark.browser
def test_single_file_outline_follows_reserved_document_round_trips(tmp_path: Path) -> None:
    """U3 reserved-document binding (single-file): the presented document —
    a selected concept OR the reserved index pane — always owns a VISIBLE
    outline beside it, whichever order the reader opens them in. Concept ->
    reserved -> headingless -> concept keeps one owner, the presented
    document's targets, and real traversal in both documents."""
    bundle = _reserved_round_trip_bundle(tmp_path)
    single = tmp_path / "single.html"
    render_single_file(bundle, single)
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.goto(single.as_uri(), wait_until="load")
            page.wait_for_function("() => window.__okfLoomGraph && window.__okfLoomGraph.interaction")

            page.locator('a[data-okf-single-concept="alpha"]').click()
            page.wait_for_function("""() => {
                  const host = document.querySelector('#detail-content [data-okf-document-outline]');
                  return !!host && !host.hidden &&
                    host.querySelectorAll('a.okf-onthispage__link').length === 2;
                }""", timeout=15000)
            assert page.locator("[data-okf-document-outline]").is_visible()

            # Concept -> reserved: the supported in-pane route is the
            # rewritten reserved link inside the presented concept body.
            page.locator("#detail-body a[data-okf-link-document]").click()
            page.wait_for_function("""() => {
                  const pane = document.getElementById('detail-document');
                  return !!pane && !pane.hidden &&
                    !!pane.querySelector('#detail-document-body h1#bundle-index');
                }""", timeout=10000)
            # The RESERVED document now owns the visible outline beside its
            # own body — not the hidden concept body earlier in the DOM.
            reserved = page.evaluate("""() => {
                  const host = document.querySelector('[data-okf-document-outline]');
                  const displayed = document.getElementById('detail-document-body');
                  return {
                    conceptHidden: document.getElementById('detail-content').hidden,
                    outlineVisible: !!host && !host.closest('[hidden]') &&
                      !!(host.offsetWidth || host.offsetHeight || host.getClientRects().length),
                    besideDisplayed: !!host && !!displayed &&
                      host.parentNode === displayed.parentNode &&
                      host.nextElementSibling === displayed,
                    hrefs: host ? Array.from(host.querySelectorAll('a.okf-onthispage__link'))
                      .map(a => a.getAttribute('href')) : [],
                  };
                }""")
            assert reserved == {
                "conceptHidden": True,
                "outlineVisible": True,
                "besideDisplayed": True,
                "hrefs": ["#bundle-index", "#reserved-sections"],
            }
            # Real traversal inside the reserved document's outline.
            page.locator("[data-okf-document-outline] summary").click()
            page.locator('[data-okf-document-outline] a[href="#reserved-sections"]').click()
            traversal = page.evaluate("""() => {
                  const target = document.getElementById('reserved-sections');
                  return {
                    focused: document.activeElement === target,
                    insideReservedPane: !!target.closest('#detail-document'),
                  };
                }""")
            assert traversal == {"focused": True, "insideReservedPane": True}

            # Reserved -> headingless concept: nothing headinged is
            # presented, so the outline retires (hidden, observer down)
            # instead of showing stale links.
            page.evaluate("() => window.__okfLoomGraph.interaction.dispatch({type: 'SELECT', id: 'gamma', source: 'test'})")
            page.wait_for_function("""() => {
                  const pane = document.getElementById('detail-document');
                  return !!pane && !!pane.hidden &&
                    document.getElementById('detail-content').hidden === false &&
                    !window.okfDocumentOutline.observerActive;
                }""", timeout=10000)
            assert not page.locator("[data-okf-document-outline]").is_visible()

            # Back to the concept: the outline must reappear beside the
            # presented concept body and traverse again.
            page.evaluate("() => window.__okfLoomGraph.interaction.dispatch({type: 'SELECT', id: 'alpha', source: 'test'})")
            page.wait_for_function("""() => {
                  const links = document.querySelectorAll('#detail-content a.okf-onthispage__link');
                  return links.length === 2 && links[1].getAttribute('href') === '#alpha-two';
                }""", timeout=10000)
            revived = page.evaluate("""() => {
                  const host = document.querySelector('[data-okf-document-outline]');
                  const displayed = document.getElementById('detail-body');
                  return {
                    outlineVisible: !!host && !host.closest('[hidden]') &&
                      !!(host.offsetWidth || host.offsetHeight || host.getClientRects().length),
                    besideDisplayed: !!host && !!displayed &&
                      host.parentNode === displayed.parentNode &&
                      host.nextElementSibling === displayed,
                    paneHidden: document.getElementById('detail-document').hidden,
                  };
                }""")
            assert revived == {"outlineVisible": True, "besideDisplayed": True, "paneHidden": True}
            page.locator("#detail-content [data-okf-document-outline] summary").click()
            page.locator('#detail-content a[href="#alpha-two"]').click()
            assert page.evaluate("() => document.activeElement.id") == "alpha-two"
        finally:
            browser.close()
