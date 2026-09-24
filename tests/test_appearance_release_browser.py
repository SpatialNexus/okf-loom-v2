"""Dedicated v2 Appearance cross-target accessibility proof.

This file intentionally owns no runtime or shared-fixture changes.  It uses
the existing browser fixture/server helpers from ``test_viewer_browser`` and
builds isolated temporary bundles only to make the configured live, SPA,
static, and single-file contracts observable in a real browser.

QUALITY_RISK_CLASSIFICATION
===========================
state-ownership: high
contract-runtime-parity: high
keyboard-focus: high
async-lifecycle: high
rendering-capture: medium (no screenshots are release evidence here)

QUALITY_PROOF_MATRIX
====================
state-ownership ->
  * ``test_live_technical_auto_bootstrap_precedence_and_os_resolution``
  * ``test_live_saved_preferences_outrank_bootstrap``
  * ``test_live_corrupt_preferences_fall_back_per_dimension``
  * ``test_live_storage_denial_keeps_resolution_and_switching_usable``
contract-runtime-parity ->
  * ``test_live_technical_auto_bootstrap_precedence_and_os_resolution``
  * ``test_portable_targets_expose_configured_technical_auto``
keyboard-focus ->
  * ``test_appearance_four_group_keyboard_matrix``
async-lifecycle ->
  * OS media transition assertions in
    ``test_live_technical_auto_bootstrap_precedence_and_os_resolution``
  * target-load/theme-ready terminal assertions in
    ``test_portable_targets_expose_configured_technical_auto``
rendering-capture ->
  * runtime ``okf-theme-ready`` and resolved ``data-theme`` assertions in
    the live and portable target tests; no image/screenshot claim is made
density (layout/targets) ->
  * ``test_density_appearance_switch_keeps_reading_metadata_and_targets``
  * ``test_compact_density_keeps_phone_studio_touch_floor``
  * ``test_compact_density_graph_controls_keep_targets_names_and_focus_tooltips``
  These are automated readability/layout proxies, not human readability or
  assistive-technology certification.

ASYNC_LIFECYCLE_MATRIX
======================
Scenario | deadline/owner | mutation phase | public result | late-result rule | proof
navigation/theme bootstrap | Playwright wait / theme.js owner | initial resolve | theme-ready + resolved root | no assertion proceeds from server paint alone | live + portable target tests
Auto OS media change | theme.js matchMedia listener | derived root theme | final resolved light/dark | final media state owns the root; stale prior state is rejected by wait | live Auto test
forced-colors theme event | graph.js consumer after theme.js event | graph repaint | system palette remains on graph cues | theme family/mode change cannot replace system colors | forced-colors graph test
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from browser_support import expect, live_server, reveal_control

from conftest import okf_module_argv, okf_subprocess_env  # noqa: E402
from test_okf02_viewer import write_fixture  # noqa: E402
from test_viewer_browser import (  # noqa: E402
    DEMO_BUNDLE,
    _free_port,
    _open_graph,
    _wait_for_server,
    page,
    server_url,
)

pytestmark = pytest.mark.browser

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
_CONCEPT_PATH = "/tables/orders"
_APPEARANCE_GROUPS = ("family", "mode", "contrast", "border", "density")
_STORAGE_KEYS = (
    "okf-theme-family",
    "okf-theme-mode",
    "okf-theme-contrast",
    "okf-theme-border",
    "okf-theme-density",
)


def _prepare_bundle(root: Path, *, viewer_theme: str) -> Path:
    """Create a disposable bundle with deliberately different config sources.

    The live case uses ``studio.theme: auto`` plus a conflicting viewer
    ``swiss-dark`` preference.  That makes the served bootstrap precedence
    observable instead of merely asserting two identical defaults.  Portable
    targets use the viewer config directly with Technical Auto.
    """
    bundle = root / "bundle"
    shutil.copytree(
        DEMO_BUNDLE,
        bundle,
        ignore=shutil.ignore_patterns(".okf-loom"),
    )
    (bundle / "okf-loom.config.yaml").write_text(
        "viewer:\n"
        "  title: Appearance browser proof\n"
        "studio:\n"
        "  edit: true\n"
        "  live: true\n"
        "  theme: auto\n",
        encoding="utf-8",
    )
    viewer_dir = bundle / ".okf-loom" / "viewer"
    viewer_dir.mkdir(parents=True, exist_ok=True)
    (viewer_dir / "config.json").write_text(
        json.dumps(
            {
                "theme": viewer_theme,
                "contrast": "soft",
                "border": "off",
                # Portable graph boot is not part of this Appearance proof;
                # keeping CDN off makes the browser target deterministic.
                "cdn": False,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return bundle


@pytest.fixture(scope="module")
def configured_live_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Serve a live bundle whose studio bootstrap is configured to Auto."""
    bundle = _prepare_bundle(
        tmp_path_factory.mktemp("appearance-live"),
        viewer_theme="swiss-dark",
    )
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        okf_module_argv(
            "serve",
            str(bundle),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-watch",
            "--no-open",
        ),
        cwd=str(TOOLKIT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=okf_subprocess_env(),
    )
    try:
        _wait_for_server(proc, base)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture(scope="module")
def portable_targets(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Build and serve actual ``spa``, ``static``, and ``single-file`` output."""
    root = tmp_path_factory.mktemp("appearance-portable")
    bundle = _prepare_bundle(root, viewer_theme="auto")
    output_root = root / "targets"
    output_root.mkdir()
    for target in ("spa", "static", "single-file"):
        result = subprocess.run(
            okf_module_argv(
                "build",
                str(bundle),
                "--target",
                target,
                "--out",
                str(output_root / target),
            ),
            cwd=str(TOOLKIT_ROOT),
            env=okf_subprocess_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(
                f"{target} target build failed (rc={result.returncode}):\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )

    procs: list[subprocess.Popen] = []
    urls: dict[str, str] = {}
    try:
        for target in ("spa", "static", "single-file"):
            port = _free_port()
            base = f"http://127.0.0.1:{port}"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "http.server",
                    str(port),
                    "--bind",
                    "127.0.0.1",
                    "--directory",
                    str(output_root / target),
                ],
                cwd=str(TOOLKIT_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            procs.append(proc)
            _wait_for_server(proc, base)
            path = "/index.html" if target == "single-file" else "/tables/orders.html"
            urls[target] = f"{base}{path}"
        yield urls
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _deny_storage_init_script() -> str:
    """Return an init script that models browser storage permission denial."""
    return """
      (() => {
        const denied = {
          getItem() { throw new DOMException('Storage denied', 'SecurityError'); },
          setItem() { throw new DOMException('Storage denied', 'SecurityError'); },
          removeItem() { throw new DOMException('Storage denied', 'SecurityError'); },
          clear() { throw new DOMException('Storage denied', 'SecurityError'); },
          key() { throw new DOMException('Storage denied', 'SecurityError'); },
          get length() { throw new DOMException('Storage denied', 'SecurityError'); },
        };
        Object.defineProperty(window, 'localStorage', {
          configurable: true,
          get: () => denied,
        });
      })();
    """


def _seed_storage_init_script(values: dict[str, str]) -> str:
    payload = json.dumps(values, separators=(",", ":"))
    return f"""
      (() => {{
        const values = {payload};
        for (const [key, value] of Object.entries(values)) localStorage.setItem(key, value);
      }})();
    """


def _wait_for_theme(page) -> None:
    """Require the runtime owner and resolved root state, not server paint."""
    page.wait_for_function(
        """() => Boolean(
          document.documentElement.classList.contains('okf-theme-ready') &&
          window.OKFLoomTheme &&
          typeof window.OKFLoomTheme.getResolvedTheme === 'function' &&
          document.documentElement.dataset.theme === window.OKFLoomTheme.getResolvedTheme()
        )""",
        timeout=5000,
    )


def _theme_state(page) -> dict:
    return page.evaluate(
        """() => {
          const api = window.OKFLoomTheme;
          const menu = document.getElementById('okf-appearance-menu');
          const groups = {};
          for (const group of menu ? menu.querySelectorAll('[role="group"]') : []) {
            const key = group.querySelector('input[data-okf-set]')?.dataset.okfSet;
            groups[key] = Array.from(group.querySelectorAll('input[data-okf-set]')).map(input => ({
              value: input.dataset.okfVal,
              checked: input.checked,
              ariaChecked: input.getAttribute('aria-checked'),
              wrapperAriaChecked: input.closest('.okf-appearance__opt')?.getAttribute('aria-checked'),
              tabIndex: input.tabIndex,
            }));
          }
          return {
            family: api?.getFamily(),
            mode: api?.getMode(),
            resolvedMode: api?.getResolvedMode(),
            contrast: api?.getContrast(),
            border: api?.getBorder(),
            density: api?.getDensity(),
            resolvedTheme: api?.getResolvedTheme(),
            rootTheme: document.documentElement.dataset.theme,
            rootContrast: document.documentElement.dataset.okfContrast,
            rootBorder: document.documentElement.dataset.okfBorder,
            rootDensity: document.documentElement.dataset.okfDensity,
            ready: document.documentElement.classList.contains('okf-theme-ready'),
            groups,
          };
        }"""
    )


def _assert_checked_matrix(state: dict, expected: dict[str, str]) -> None:
    assert state["ready"] is True
    assert state["rootTheme"] == state["resolvedTheme"]
    for group in _APPEARANCE_GROUPS:
        options = state["groups"][group]
        checked = [option for option in options if option["checked"]]
        roving = [option for option in options if option["tabIndex"] == 0]
        assert len(checked) == 1, (group, options)
        assert len(roving) == 1, (group, options)
        assert checked[0]["value"] == expected[group], (group, options)
        assert checked[0]["ariaChecked"] == "true", (group, options)
        assert checked[0]["wrapperAriaChecked"] == "true", (group, options)
        assert roving[0]["value"] == checked[0]["value"], (group, options)


def _bootstrap_blocks(page) -> dict[str, dict]:
    return page.evaluate(
        """() => Object.fromEntries(
          ['okf-studio-bootstrap', 'okf-theme-config'].map(id => {
            const node = document.getElementById(id);
            return [id, node ? JSON.parse(node.textContent || 'null') : null];
          })
        )"""
    )


def test_live_technical_auto_bootstrap_precedence_and_os_resolution(
    configured_live_url: str, page
) -> None:
    """A live bootstrap Auto wins a conflicting viewer config and follows OS."""
    page.emulate_media(color_scheme="light")
    page.goto(f"{configured_live_url}{_CONCEPT_PATH}", wait_until="domcontentloaded")
    _wait_for_theme(page)

    blocks = _bootstrap_blocks(page)
    assert blocks["okf-studio-bootstrap"]["theme"] == "auto"
    # The viewer config deliberately says swiss-dark; only studio bootstrap
    # can explain the Technical + Auto result on this live page.
    assert blocks["okf-theme-config"] == {
        "theme": "swiss-dark",
        "contrast": "soft",
        "border": "off",
    }
    state = _theme_state(page)
    _assert_checked_matrix(
        state,
        {"family": "technical", "mode": "auto", "contrast": "soft", "border": "off", "density": "compact"},
    )
    assert state["rootTheme"] == "technical-light"
    assert state["resolvedMode"] == "light"
    assert state["rootContrast"] == "soft"
    assert state["rootBorder"] == "off"
    assert page.evaluate(
        """() => {
          try {
            return Object.fromEntries([
              'okf-theme-family', 'okf-theme-mode', 'okf-theme-contrast', 'okf-theme-border', 'okf-theme-density'
            ].map(key => [key, localStorage.getItem(key)]));
          } catch (_) { return null; }
        }"""
    ) == {key: None for key in _STORAGE_KEYS}

    # This is a real OS media transition, not a direct data-theme mutation.
    page.emulate_media(color_scheme="dark")
    page.wait_for_function(
        """() => document.documentElement.dataset.theme === 'technical-dark' &&
          window.OKFLoomTheme.getResolvedMode() === 'dark'""",
        timeout=3000,
    )
    page.emulate_media(color_scheme="light")
    page.wait_for_function(
        """() => document.documentElement.dataset.theme === 'technical-light' &&
          window.OKFLoomTheme.getResolvedMode() === 'light'""",
        timeout=3000,
    )


def test_live_saved_preferences_outrank_bootstrap(configured_live_url: str, page) -> None:
    """Valid saved values own every Appearance dimension over configured values."""
    page.emulate_media(color_scheme="dark")
    page.add_init_script(
        _seed_storage_init_script(
            {
                "okf-theme-family": "swiss",
                "okf-theme-mode": "light",
                "okf-theme-contrast": "normal",
                "okf-theme-border": "on",
                "okf-theme-density": "compact",
            }
        )
    )
    page.goto(f"{configured_live_url}{_CONCEPT_PATH}", wait_until="domcontentloaded")
    _wait_for_theme(page)
    state = _theme_state(page)
    _assert_checked_matrix(
        state,
        {"family": "swiss", "mode": "light", "contrast": "normal", "border": "on", "density": "compact"},
    )
    assert state["rootTheme"] == "swiss-light"
    assert state["rootDensity"] == "compact"
    assert state["resolvedMode"] == "light"
    assert page.evaluate(
        """() => Object.fromEntries([
          'okf-theme-family', 'okf-theme-mode', 'okf-theme-contrast', 'okf-theme-border', 'okf-theme-density'
        ].map(key => [key, localStorage.getItem(key)]))"""
    ) == {
        "okf-theme-family": "swiss",
        "okf-theme-mode": "light",
        "okf-theme-contrast": "normal",
        "okf-theme-border": "on",
        "okf-theme-density": "compact",
    }


def test_live_corrupt_preferences_fall_back_per_dimension(
    configured_live_url: str, page
) -> None:
    """Corrupt canonical values are ignored without re-reading legacy state."""
    page.emulate_media(color_scheme="dark")
    page.add_init_script(
        _seed_storage_init_script({key: "corrupt" for key in _STORAGE_KEYS})
    )
    page.goto(f"{configured_live_url}{_CONCEPT_PATH}", wait_until="domcontentloaded")
    _wait_for_theme(page)
    state = _theme_state(page)
    _assert_checked_matrix(
        state,
        {"family": "technical", "mode": "auto", "contrast": "soft", "border": "off", "density": "compact"},
    )
    assert state["rootTheme"] == "technical-dark"
    assert state["resolvedMode"] == "dark"
    assert page.evaluate(
        """() => Object.fromEntries([
          'okf-theme-family', 'okf-theme-mode', 'okf-theme-contrast', 'okf-theme-border', 'okf-theme-density'
        ].map(key => [key, localStorage.getItem(key)]))"""
    ) == {key: "corrupt" for key in _STORAGE_KEYS}


def test_live_storage_denial_keeps_resolution_and_switching_usable(
    configured_live_url: str, page
) -> None:
    """Denied storage disables persistence only; configured and session state work."""
    page.emulate_media(color_scheme="dark")
    page.add_init_script(_deny_storage_init_script())
    page.goto(f"{configured_live_url}{_CONCEPT_PATH}", wait_until="domcontentloaded")
    _wait_for_theme(page)
    initial = _theme_state(page)
    _assert_checked_matrix(
        initial,
        {"family": "technical", "mode": "auto", "contrast": "soft", "border": "off", "density": "compact"},
    )
    assert initial["rootTheme"] == "technical-dark"
    assert page.evaluate(
        """() => {
          try { localStorage.getItem('okf-theme-family'); return 'not-denied'; }
          catch (_) { return 'denied'; }
        }"""
    ) == "denied"

    switched = page.evaluate(
        """() => {
          try {
            window.OKFLoomTheme.setFamily('swiss');
            window.OKFLoomTheme.setMode('dark');
            window.OKFLoomTheme.setContrast('normal');
            window.OKFLoomTheme.setBorder('on');
            return {error: null, state: {
              family: window.OKFLoomTheme.getFamily(),
              mode: window.OKFLoomTheme.getMode(),
              contrast: window.OKFLoomTheme.getContrast(),
              border: window.OKFLoomTheme.getBorder(),
              theme: document.documentElement.dataset.theme,
            }};
          } catch (error) { return {error: String(error), state: null}; }
        }"""
    )
    assert switched["error"] is None
    assert switched["state"] == {
        "family": "swiss",
        "mode": "dark",
        "contrast": "normal",
        "border": "on",
        "theme": "swiss-dark",
    }


@pytest.mark.parametrize(
    "viewport",
    [(1280, 900), (390, 844)],
    ids=["desktop", "mobile"],
)
def test_appearance_five_group_keyboard_matrix(
    configured_live_url: str, page, viewport: tuple[int, int]
) -> None:
    """Desktop/mobile proof for all five groups, roving focus, and dismissal."""
    width, height = viewport
    page.set_viewport_size({"width": width, "height": height})
    page.goto(f"{configured_live_url}{_CONCEPT_PATH}", wait_until="domcontentloaded")
    _wait_for_theme(page)
    trigger = page.locator("#okf-theme")
    menu = page.locator("#okf-appearance-menu")
    expect(trigger).to_be_visible()
    expect(trigger).to_contain_text("Appearance · Technical")
    expect(trigger.locator(".okf-appearance-trigger__swatch--technical")).to_have_count(1)
    trigger.focus()
    page.keyboard.press("Enter")
    expect(menu).to_be_visible()
    expect(trigger).to_have_attribute("aria-expanded", "true")

    assert menu.locator('[role="group"]').count() == 5
    assert menu.locator('input[data-okf-set="family"]').count() == 4
    assert menu.locator(".okf-appearance__preview").count() == 4
    expect(menu.locator('#okf-appearance-mode-auto').locator("..")).to_contain_text(
        "Technical family + follow OS colour mode"
    )
    assert menu.get_attribute("role") == "dialog"
    assert menu.get_attribute("aria-label") == "Appearance"
    semantics = page.evaluate(
        """() => {
          const menu = document.getElementById('okf-appearance-menu');
          const groups = Array.from(menu.querySelectorAll('[role="group"]'));
          return {
            groupLabels: groups.map(group => {
              const id = group.getAttribute('aria-labelledby');
              return id && document.getElementById(id)?.textContent.trim();
            }),
            labelledInputs: Array.from(menu.querySelectorAll('input[data-okf-set]')).every(input =>
              input.id && document.querySelector(`label[for="${CSS.escape(input.id)}"]`)
            ),
          };
        }"""
    )
    assert semantics["groupLabels"] == ["Colour mode", "Density", "Family", "Contrast", "Borders"]
    assert semantics["labelledInputs"] is True
    geometry = menu.bounding_box()
    assert geometry is not None, "Appearance menu has no layout box"
    assert geometry["x"] >= -0.5
    assert geometry["y"] >= -0.5
    assert geometry["x"] + geometry["width"] <= width + 0.5
    assert geometry["y"] + geometry["height"] <= height + 0.5

    _assert_checked_matrix(
        _theme_state(page),
        {"family": "technical", "mode": "auto", "contrast": "soft", "border": "off", "density": "compact"},
    )
    assert page.evaluate("() => document.activeElement?.dataset.okfVal") == "auto"

    # Arrow/Home/End own selection within a group and expose checked state.
    page.keyboard.press("End")
    assert page.evaluate("() => document.activeElement?.dataset.okfVal") == "dark"
    page.keyboard.press("Home")
    assert page.evaluate("() => document.activeElement?.dataset.okfVal") == "auto"

    # Tab enters each group once; Arrow/Home/End then exercise each group.
    page.keyboard.press("Tab")
    assert page.evaluate("() => document.activeElement?.dataset.okfSet") == "density"
    page.keyboard.press("End")
    assert page.evaluate("() => document.activeElement?.dataset.okfVal") == "compact"
    page.keyboard.press("Home")
    assert page.evaluate("() => document.activeElement?.dataset.okfVal") == "comfortable"

    # Advanced is a native disclosure, not a hidden tab stop into Family.
    page.keyboard.press("Tab")
    expect(menu.locator('.okf-appearance__advanced > summary')).to_be_focused()
    page.keyboard.press("Enter")
    page.keyboard.press("Tab")
    assert page.evaluate("() => document.activeElement?.dataset.okfSet") == "family"
    page.keyboard.press("End")
    assert page.evaluate("() => document.activeElement?.dataset.okfVal") == "coffee"
    page.keyboard.press("Home")
    assert page.evaluate("() => document.activeElement?.dataset.okfVal") == "technical"

    page.keyboard.press("Tab")
    assert page.evaluate("() => document.activeElement?.dataset.okfSet") == "contrast"
    page.keyboard.press("Home")
    assert page.evaluate("() => window.OKFLoomTheme.getContrast()") == "normal"
    expect(
        page.locator('input[data-okf-set="contrast"][data-okf-val="normal"]')
    ).to_be_checked()
    page.keyboard.press("End")
    assert page.evaluate("() => document.activeElement?.dataset.okfVal") == "soft"
    assert page.evaluate("() => window.OKFLoomTheme.getContrast()") == "soft"
    expect(
        page.locator('input[data-okf-set="contrast"][data-okf-val="soft"]')
    ).to_be_checked()

    page.keyboard.press("Tab")
    assert page.evaluate("() => document.activeElement?.dataset.okfSet") == "border"
    page.keyboard.press("Home")
    assert page.evaluate("() => window.OKFLoomTheme.getBorder()") == "on"
    expect(page.locator('input[data-okf-set="border"][data-okf-val="on"]')).to_be_checked()
    page.keyboard.press("End")
    assert page.evaluate("() => document.activeElement?.dataset.okfVal") == "off"
    assert page.evaluate("() => window.OKFLoomTheme.getBorder()") == "off"
    expect(page.locator('input[data-okf-set="border"][data-okf-val="off"]')).to_be_checked()
    _assert_checked_matrix(
        _theme_state(page),
        {"family": "technical", "mode": "auto", "contrast": "soft", "border": "off", "density": "comfortable"},
    )

    # Shift+Tab reverses the same five-group order and remains within the
    # menu until its boundary, where focus returns to the trigger.
    for group in ("contrast", "family"):
        page.keyboard.press("Shift+Tab")
        assert page.evaluate("() => document.activeElement?.dataset.okfSet") == group
    page.keyboard.press("Shift+Tab")
    expect(menu.locator('.okf-appearance__advanced > summary')).to_be_focused()
    for group in ("density", "mode"):
        page.keyboard.press("Shift+Tab")
        assert page.evaluate("() => document.activeElement?.dataset.okfSet") == group
    page.keyboard.press("Shift+Tab")
    expect(trigger).to_be_focused()
    expect(menu).to_be_visible()

    # Escape closes exactly this layer and restores the trigger focus.
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    expect(trigger).to_have_attribute("aria-expanded", "false")
    expect(trigger).to_be_focused()

    # Outside click closes without stealing focus from its destination.
    trigger.click()
    expect(menu).to_be_visible()
    search = page.locator('input[type="search"][name="q"]').first
    # The fifth group makes the fixed menu intentionally scrollable on phones;
    # dispatch the destination click even when the menu geometrically overlaps
    # the search row, then verify the destination (not the trigger) owns focus.
    search.dispatch_event("click")
    search.focus()
    expect(menu).to_be_hidden()
    search.focus()
    expect(search).to_be_focused()


def test_optional_families_are_keyboard_selectable_and_persisted(
    configured_live_url: str, page
) -> None:
    """Purple/Coffee are normal Appearance choices, including persistence."""
    page.emulate_media(color_scheme="light")
    page.goto(f"{configured_live_url}{_CONCEPT_PATH}", wait_until="domcontentloaded")
    _wait_for_theme(page)
    trigger = page.locator("#okf-theme")
    trigger.click()
    menu = page.locator("#okf-appearance-menu")
    expect(menu).to_be_visible()

    purple = menu.locator('input[data-okf-set="family"][data-okf-val="purple"]')
    coffee = menu.locator('input[data-okf-set="family"][data-okf-val="coffee"]')
    reveal_control(page, purple)
    expect(menu.get_by_text("Purple", exact=True)).to_be_visible()
    expect(menu.get_by_text("Coffee", exact=True)).to_be_visible()
    purple.check(force=True)
    expect(page.locator("html")).to_have_attribute("data-theme", "purple-light")
    expect(purple).to_be_checked()
    expect(trigger).to_contain_text("Appearance · Purple")
    expect(trigger.locator(".okf-appearance-trigger__swatch--purple")).to_have_count(1)

    menu.locator('input[data-okf-set="mode"][data-okf-val="dark"]').check(force=True)
    expect(page.locator("html")).to_have_attribute("data-theme", "purple-dark")
    coffee.check(force=True)
    expect(page.locator("html")).to_have_attribute("data-theme", "coffee-dark")
    expect(trigger).to_contain_text("Appearance · Coffee")
    expect(menu.locator('#okf-appearance-mode-auto').locator("..")).to_contain_text(
        "Coffee family + follow OS colour mode"
    )
    menu.locator('input[data-okf-set="mode"][data-okf-val="auto"]').check(force=True)
    expect(page.locator("html")).to_have_attribute("data-theme", "coffee-light")
    page.emulate_media(color_scheme="light")
    page.wait_for_function(
        "() => document.documentElement.dataset.theme === 'coffee-light'",
        timeout=3000,
    )
    page.keyboard.press("Escape")
    expect(trigger).to_be_focused()

    assert page.evaluate(
        """() => ({
          family: localStorage.getItem('okf-theme-family'),
          mode: localStorage.getItem('okf-theme-mode'),
          resolved: document.documentElement.dataset.theme,
        })"""
    ) == {"family": "coffee", "mode": "auto", "resolved": "coffee-light"}


@pytest.mark.parametrize("target", ["spa", "static", "single-file"])
def test_portable_targets_expose_configured_technical_auto(
    portable_targets: dict[str, str], page, target: str
) -> None:
    """Actual built targets expose Appearance config and resolve OS-dark safely."""
    page.emulate_media(color_scheme="dark")
    page.add_init_script(_deny_storage_init_script())
    page.goto(portable_targets[target], wait_until="domcontentloaded")
    _wait_for_theme(page)

    target_identity = page.evaluate(
        """() => ({
          bodyMode: document.body.dataset.okfMode || null,
          enhance: document.body.dataset.okfEnhance || null,
          singleFile: document.body.classList.contains('okf-viewer--singlefile'),
          externalThemeScript: !!document.querySelector('script[src*="theme.js"]'),
          config: JSON.parse(document.getElementById('okf-theme-config').textContent || 'null'),
        })"""
    )
    assert target_identity["config"] == {
        "theme": "auto",
        "contrast": "soft",
        "border": "off",
    }
    if target == "spa":
        assert target_identity["bodyMode"] == "spa"
        assert target_identity["enhance"] == "1"
        assert target_identity["singleFile"] is False
        assert target_identity["externalThemeScript"] is True
    elif target == "static":
        assert target_identity["bodyMode"] == "static"
        assert target_identity["enhance"] == "0"
        assert target_identity["singleFile"] is False
        assert target_identity["externalThemeScript"] is True
    else:
        assert target_identity["singleFile"] is True
        assert target_identity["externalThemeScript"] is False

    state = _theme_state(page)
    _assert_checked_matrix(
        state,
        {"family": "technical", "mode": "auto", "contrast": "soft", "border": "off", "density": "compact"},
    )
    assert state["rootTheme"] == "technical-dark"
    assert page.locator("#okf-theme").is_visible()
    page.locator("#okf-theme").click()
    expect(page.locator("#okf-appearance-menu")).to_be_visible()
    assert page.locator('#okf-appearance-menu [role="group"]').count() == 5


def _forced_graph_snapshot(page) -> dict:
    return page.evaluate(
        """() => {
          const graph = window.__okfLoomGraph;
          const node = graph?.cy?.nodes().first();
          const edge = graph?.cy?.edges().first();
          const resolve = (value) => {
            const probe = document.createElement('span');
            probe.style.color = value;
            probe.style.position = 'fixed';
            probe.style.visibility = 'hidden';
            document.body.appendChild(probe);
            const result = getComputedStyle(probe).color;
            probe.remove();
            return result;
          };
          const rgb = (value) => {
            const match = String(value || '').match(/rgba?\\(([^)]+)/);
            return match ? match[1].split(',').slice(0, 3).map(v => v.trim()).join(',') : null;
          };
          const style = (element, name) => element && element.length
            ? { raw: element.style(name), rawRgb: rgb(element.style(name)), resolved: resolve(element.style(name)) }
            : null;
          return {
            forced: !!window.matchMedia('(forced-colors: active)').matches,
            systemText: resolve('CanvasText'),
            systemCanvas: resolve('Canvas'),
            systemHighlight: resolve('Highlight'),
            systemTextRgb: rgb(resolve('CanvasText')),
            systemCanvasRgb: rgb(resolve('Canvas')),
            systemHighlightRgb: rgb(resolve('Highlight')),
            nodeText: style(node, 'color'),
            nodeBorder: style(node, 'border-color'),
            nodeBackground: style(node, 'text-background-color'),
            selectedBorder: style(node, 'border-color'),
            edgeLine: style(edge, 'line-color'),
            edgeArrow: style(edge, 'target-arrow-color'),
            edgeBackground: style(edge, 'text-background-color'),
          };
        }"""
    )


def test_forced_colors_graph_preserves_system_palette_on_theme_change(
    server_url: str, page
) -> None:
    """A supported forced-colors browser keeps system graph colors after theme events."""
    try:
        page.emulate_media(forced_colors="active", color_scheme="light")
    except Exception as exc:  # platform/browser inability only
        pytest.fail(f"forced-colors emulation failed on provisioned browser: {exc}")
    if not page.evaluate("() => window.matchMedia('(forced-colors: active)').matches"):
        pytest.fail("browser accepted forced-colors request but did not expose the media state")

    _open_graph(page, server_url)
    page.evaluate("() => { window.__okfLoomGraph.cy.nodes().first().select(); return undefined; }")
    before = _forced_graph_snapshot(page)
    assert before["forced"] is True
    assert before["nodeText"]["rawRgb"] == before["systemTextRgb"], before
    assert before["nodeBorder"]["rawRgb"] == before["systemHighlightRgb"], before
    assert before["nodeBackground"]["rawRgb"] == before["systemCanvasRgb"], before
    assert before["edgeLine"]["rawRgb"] == before["systemTextRgb"], before
    assert before["edgeArrow"]["rawRgb"] == before["systemTextRgb"], before
    assert before["edgeBackground"]["rawRgb"] == before["systemCanvasRgb"], before

    page.evaluate("() => window.OKFLoomTheme.setTheme('technical-dark')")
    page.wait_for_function(
        "() => document.documentElement.dataset.theme === 'technical-dark'",
        timeout=3000,
    )
    after = _forced_graph_snapshot(page)
    assert after["forced"] is True
    for key in (
        "nodeText",
        "nodeBorder",
        "nodeBackground",
        "edgeLine",
        "edgeArrow",
        "edgeBackground",
    ):
        assert after[key]["rawRgb"] == before[key]["rawRgb"], (key, before, after)
        assert after[key]["resolved"] == before[key]["resolved"], (key, before, after)

    # Optional families must take the same system-colour route; ordinary
    # Purple/Coffee tokens may never leak into a forced-colours canvas.
    for theme in ("purple-light", "coffee-dark"):
        page.evaluate("theme => window.OKFLoomTheme.setTheme(theme)", theme)
        page.wait_for_function(
            "theme => document.documentElement.dataset.theme === theme", arg=theme,
            timeout=3000,
        )
        optional = _forced_graph_snapshot(page)
        assert optional["forced"] is True
        for key in (
            "nodeText", "nodeBorder", "nodeBackground", "edgeLine",
            "edgeArrow", "edgeBackground",
        ):
            assert optional[key]["rawRgb"] == before[key]["rawRgb"], (key, theme, before, optional)
            assert optional[key]["resolved"] == before[key]["resolved"], (key, theme, before, optional)


def test_compact_density_graph_controls_keep_targets_names_and_focus_tooltips(
    server_url: str, page
) -> None:
    """Compact graph chrome stays named, unclipped, target-sized, and explained."""
    page.set_viewport_size({"width": 390, "height": 844})
    _open_graph(page, server_url)
    # Select Compact the way a reader does: through the Appearance control.
    # Compact is the default, so this re-clicks the *checked* option: the
    # dialog must stay open with focus on it (its sub-label once stacked
    # above the radio's hit area, dropping focus to <body> and the click).
    _choose_density(page, "compact")
    controls = page.locator(".okf-graph-zoom button")
    expect(controls.first).to_be_visible()
    proof = page.evaluate(
        """() => ({
          overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          controls: Array.from(document.querySelectorAll('.okf-graph-zoom button')).map(button => {
            const rect = button.getBoundingClientRect();
            return {
              name: button.getAttribute('aria-label'),
              tooltip: button.dataset.tooltip,
              width: rect.width,
              height: rect.height,
            };
          }),
        })"""
    )
    assert proof["overflow"] <= 1, proof
    assert proof["controls"]
    assert all(item["name"] and item["tooltip"] for item in proof["controls"]), proof
    assert all(item["width"] >= 44 and item["height"] >= 44 for item in proof["controls"]), proof
    controls.first.focus()
    tooltip = page.evaluate(
        """() => {
          const button = document.querySelector('.okf-graph-zoom button');
          const style = getComputedStyle(button, '::after');
          return {content: style.content, visibility: style.visibility, opacity: style.opacity};
        }"""
    )
    assert tooltip["content"] not in ("none", '""')
    # Phones expose persistent visible action labels instead of a tooltip
    # that would collide with adjacent controls. The accessible name persists.
    expect(controls.first.locator('.okf-viewport-label')).to_be_visible()
    assert controls.first.get_attribute('aria-label')


# ---------------------------------------------------------------------------
# Density audit on a real concept page with trust/lifecycle/source metadata
# ---------------------------------------------------------------------------
#
# Contract: spec.md keeps advisory metadata compact with advanced details
# behind a native disclosure, and wiki.css (F23) states density is
# interface-only: it must not change article typography, measure, minimum
# targets or mobile touch geometry. Everything below is driven through the
# shipped Appearance control and the native <summary>; nothing is forced.

_DENSITY_REVIEW_PATH = "/notes/review"
# ``write_fixture``'s review body minus its mermaid/math fences: those pull CDN
# renderers that have nothing to do with density and would make the audit
# network-bound. The source-joined claim and the ordinary footnote stay.
_DENSITY_REVIEW_BODY = (
    "\n# Findings\n\nClaim with source[^alpha] and hostile source[^beta].\n\n"
    "A normal footnote[^ordinary].\n\n[^ordinary]: Authored footnote.\n"
)

_OPTION_BOX = """node => {
  const style = getComputedStyle(node);
  return {
    paddingTop: parseFloat(style.paddingTop),
    paddingBottom: parseFloat(style.paddingBottom),
    height: node.getBoundingClientRect().height,
  };
}"""

_DENSITY_LAYOUT = """() => {
  const root = document.documentElement;
  const box = node => {
    const r = node.getBoundingClientRect();
    return {x: r.x, right: r.right, width: r.width, height: r.height};
  };
  const shown = node => {
    const r = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const type = selector => {
    const node = document.querySelector(selector);
    const style = getComputedStyle(node);
    return {
      fontSize: style.fontSize,
      lineHeight: style.lineHeight,
      maxWidth: style.maxWidth,
      width: node.getBoundingClientRect().width,
    };
  };
  const meta = document.querySelector('section.okf-metadata');
  const details = meta.querySelector('details');
  return {
    density: root.dataset.okfDensity,
    stored: localStorage.getItem('okf-theme-density'),
    shellGap: parseFloat(getComputedStyle(document.querySelector('.okf-shell')).columnGap),
    viewport: root.clientWidth,
    overflow: root.scrollWidth - root.clientWidth,
    metaOverflow: meta.scrollWidth - meta.clientWidth,
    metaOpen: details.open,
    body: type('.okf-page__body'),
    paragraph: type('.okf-page__body p'),
    metadata: type('section.okf-metadata'),
    summary: box(details.querySelector(':scope > summary')),
    shellControls: Array.from(
      document.querySelectorAll('.okf-shell :is(a[href], button, input:not([type="hidden"]))')
    ).filter(shown).map(node => ({
      name: (node.getAttribute('aria-label') || node.getAttribute('placeholder') ||
             node.textContent || '').trim().replace(/\\s+/g, ' '),
      brand: node.classList.contains('okf-brand-link'),
      ...box(node),
    })),
  };
}"""

# Centre-point hit test after bringing the link into view: proves the evidence
# link is painted and reachable, not clipped by an overflow ancestor or covered
# by sticky chrome.
_LINK_UNCLIPPED = """link => {
  link.scrollIntoView({block: 'center', inline: 'nearest'});
  const r = link.getClientRects()[0] || link.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return {
    left: r.left, right: r.right, width: r.width, height: r.height,
    viewport: document.documentElement.clientWidth,
    hit: !!hit && (hit === link || link.contains(hit)),
  };
}"""

_STUDIO_ACTION_TARGETS = """() => Array.from(document.querySelectorAll('.okf-studiobtn, .okf-iconbtn'))
  .filter(node => {
    const r = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden';
  })
  .map(node => ({
    name: (node.getAttribute('aria-label') || node.textContent || '').trim().replace(/\\s+/g, ' '),
    height: Math.round(node.getBoundingClientRect().height * 10) / 10,
  }))"""


@pytest.fixture(scope="module")
def metadata_live_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Serve a live concept page carrying trust, lifecycle and source metadata."""
    fm, _body, path = write_fixture(tmp_path_factory.mktemp("appearance-density") / "bundle")
    path.write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + _DENSITY_REVIEW_BODY,
        encoding="utf-8",
    )
    with live_server(path.parents[1]) as base:
        yield base


def _choose_density(page, value: str) -> tuple[dict, dict]:
    """Select a density through the real Appearance control, never the JS API.

    Returns the chosen option's box before and after the switch so callers can
    prove the compact spacing is visibly consumed, not merely an attribute.
    """
    trigger = page.locator("#okf-theme")
    menu = page.locator("#okf-appearance-menu")
    trigger.click()
    expect(menu).to_be_visible()
    expect(trigger).to_have_attribute("aria-expanded", "true")
    option = menu.locator(f'label.okf-appearance__opt[for="okf-appearance-density-{value}"]')
    before = option.evaluate(_OPTION_BOX)
    option.click()
    expect(page.locator("html")).to_have_attribute("data-okf-density", value)
    expect(menu.locator(f'input[data-okf-set="density"][data-okf-val="{value}"]')).to_be_checked()
    expect(option).to_have_attribute("aria-checked", "true")
    after = option.evaluate(_OPTION_BOX)
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    expect(trigger).to_be_focused()
    return before, after


def _assert_density_layout(layout: dict, width: int) -> None:
    """Width-independent layout, disclosure and target invariants."""
    phone = width <= 640
    assert layout["overflow"] <= 1, layout
    assert layout["metaOpen"] is True, layout
    assert layout["metaOverflow"] <= 1, layout
    summary = layout["summary"]
    assert summary["x"] >= -0.5 and summary["right"] <= layout["viewport"] + 0.5, summary
    # The native metadata disclosure promises a 2.75rem (44px) target.
    assert summary["height"] >= 43.5, summary
    # Desktop standalone controls keep the WCAG 2.5.8 24px minimum; the <=640px
    # shell promises 44px touch targets. The phone floor is owned by the brand
    # row container, not the inline wordmark link, so that link is exempt.
    floor = 44 if phone else 24
    controls = [item for item in layout["shellControls"] if not (phone and item["brand"])]
    names = [item["name"] for item in controls]
    assert any(name.startswith("Appearance") for name in names), names
    assert "Index" in names and "Graph" in names, names
    assert len(controls) >= 6, controls
    small = [
        item for item in controls
        if item["width"] < floor - 0.5 or item["height"] < floor - 0.5
    ]
    assert not small, (floor, small)


def _assert_reading_unchanged(before: dict, after: dict) -> None:
    """Density never changes article typography/measure or metadata text."""
    for key in ("body", "paragraph", "metadata"):
        for prop in ("fontSize", "lineHeight", "maxWidth"):
            assert after[key][prop] == before[key][prop], (key, prop, before[key], after[key])
        assert after[key]["width"] == pytest.approx(before[key]["width"], abs=0.5), (
            key, before[key], after[key],
        )


def _assert_evidence_links_unclipped(metadata) -> None:
    for name in ("Local evidence", "External evidence"):
        link = metadata.get_by_role("link", name=name, exact=True)
        expect(link).to_be_visible()
        geometry = link.evaluate(_LINK_UNCLIPPED)
        assert geometry["width"] > 0 and geometry["height"] > 0, (name, geometry)
        assert geometry["left"] >= -0.5, (name, geometry)
        assert geometry["right"] <= geometry["viewport"] + 0.5, (name, geometry)
        assert geometry["hit"] is True, (name, geometry)


@pytest.mark.parametrize("width", [1280, 640, 390], ids=["desktop", "compact-shell", "phone"])
def test_density_appearance_switch_keeps_reading_metadata_and_targets(
    metadata_live_url: str, page, width: int
) -> None:
    """Default Compact -> Comfortable -> Compact -> Comfortable through the
    Appearance control on a metadata-bearing concept page.

    Compact (the fresh-visitor default) must visibly tighten interface spacing
    while the article's
    typography and measure, the open metadata disclosure, evidence links,
    control targets and keyboard/source navigation all stay intact.
    """
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.goto(f"{metadata_live_url}{_DENSITY_REVIEW_PATH}", wait_until="domcontentloaded")
    _wait_for_theme(page)

    metadata = page.get_by_role("region", name="Trust, lifecycle and sources")
    expect(metadata).to_be_visible()
    details = metadata.locator("details").first
    summary = details.locator(":scope > summary")
    expect(summary).to_have_text("Generation, sources and contract")
    # Advanced metadata starts behind its disclosure; open it natively.
    expect(details).to_have_js_property("open", False)
    summary.click()
    expect(details).to_have_js_property("open", True)

    initial = page.evaluate(_DENSITY_LAYOUT)
    # Compact is the fresh-visitor default; booting never persists it.
    assert initial["density"] == "compact"
    assert initial["stored"] is None
    _assert_density_layout(initial, width)
    _assert_evidence_links_unclipped(metadata)

    # Comfortable through the real control: its own option visibly loosens.
    option_before, option_after = _choose_density(page, "comfortable")
    assert option_after["paddingTop"] > option_before["paddingTop"], (option_before, option_after)
    assert option_after["height"] > option_before["height"], (option_before, option_after)
    comfortable = page.evaluate(_DENSITY_LAYOUT)
    assert comfortable["density"] == "comfortable"
    assert comfortable["stored"] == "comfortable"
    if width > 640:
        assert initial["shellGap"] < comfortable["shellGap"], (initial["shellGap"], comfortable["shellGap"])
    _assert_density_layout(comfortable, width)
    _assert_reading_unchanged(initial, comfortable)
    _assert_evidence_links_unclipped(metadata)

    # Compact through the real control: its own option visibly tightens.
    option_before, option_after = _choose_density(page, "compact")
    assert option_after["paddingTop"] < option_before["paddingTop"], (option_before, option_after)
    assert option_after["paddingBottom"] < option_before["paddingBottom"], (option_before, option_after)
    assert option_after["height"] < option_before["height"], (option_before, option_after)
    compact = page.evaluate(_DENSITY_LAYOUT)
    assert compact["density"] == "compact"
    assert compact["stored"] == "compact"
    if width > 640:
        # Desktop chrome consumes the compact spacing too, not only the menu.
        assert compact["shellGap"] < comfortable["shellGap"], (comfortable["shellGap"], compact["shellGap"])
    # The explicit Compact choice is exactly the default projection.
    assert compact["shellGap"] == initial["shellGap"]
    _assert_density_layout(compact, width)
    _assert_reading_unchanged(comfortable, compact)
    assert compact["summary"]["height"] == pytest.approx(comfortable["summary"]["height"], abs=0.5)
    if width <= 640:
        # Mobile touch geometry of the shell is density-invariant.
        assert [round(item["height"], 1) for item in compact["shellControls"]] == [
            round(item["height"], 1) for item in comfortable["shellControls"]
        ], (comfortable["shellControls"], compact["shellControls"])
    _assert_evidence_links_unclipped(metadata)

    # Keyboard disclosure activation still works in compact mode.
    summary.focus()
    page.keyboard.press("Enter")
    expect(details).to_have_js_property("open", False)
    page.keyboard.press("Space")
    expect(details).to_have_js_property("open", True)
    page.keyboard.press("Enter")
    expect(details).to_have_js_property("open", False)

    # Keyboard source navigation reaches its entry through the collapsed
    # compact disclosure, in the same viewer tab.
    ref = page.get_by_role("link", name="Source alpha", exact=True)
    target = ref.get_attribute("href")
    assert target and target.startswith("#"), target
    ref.focus()
    page.keyboard.press("Enter")
    expect(page.locator(target)).to_be_focused()
    expect(page.locator(target).locator("strong").first).to_be_in_viewport()
    expect(details).to_have_js_property("open", True)
    assert len(page.context.pages) == 1

    # Back to Comfortable through the same control: spacing and state restore.
    option_before, option_after = _choose_density(page, "comfortable")
    assert option_after["paddingTop"] > option_before["paddingTop"], (option_before, option_after)
    assert option_after["height"] > option_before["height"], (option_before, option_after)
    restored = page.evaluate(_DENSITY_LAYOUT)
    assert restored["density"] == "comfortable"
    assert restored["stored"] == "comfortable"
    assert restored["shellGap"] == comfortable["shellGap"]
    _assert_density_layout(restored, width)
    _assert_reading_unchanged(comfortable, restored)
    assert errors == []


def test_compact_density_keeps_phone_studio_touch_floor(
    metadata_live_url: str, page
) -> None:
    """At phone width Compact must keep the studio action touch floor.

    Compact's 24px WCAG minimum for studio actions is a floor at single-class
    specificity, so the later <=600px 40px touch floor still wins and the
    phone view bar 'Sections' toggle keeps its height (F23: density never
    alters minimum targets or mobile touch geometry).
    """
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{metadata_live_url}{_DENSITY_REVIEW_PATH}", wait_until="domcontentloaded")
    _wait_for_theme(page)
    # Compact is the default; measure Comfortable first, then return to Compact.
    try:
        _choose_density(page, "comfortable")
    except AssertionError as exc:
        pytest.fail(f"precondition: Appearance must select Comfortable: {exc}")
    comfortable = page.evaluate(_STUDIO_ACTION_TARGETS)
    if not comfortable or any(item["height"] < 39.5 for item in comfortable):
        pytest.fail(f"precondition: phone studio actions start at the 40px floor: {comfortable}")
    try:
        _choose_density(page, "compact")
    except AssertionError as exc:
        pytest.fail(f"precondition: Appearance must select Compact: {exc}")
    compact = page.evaluate(_STUDIO_ACTION_TARGETS)
    assert compact == comfortable, {"comfortable": comfortable, "compact": compact}
