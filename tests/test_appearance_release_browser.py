"""Ledger cross-target appearance, keyboard, denial and overlay browser proofs."""
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
def _prepare_bundle(root: Path, *, viewer_theme: str) -> Path:
    """Create a disposable bundle with deliberately different config sources.

    The live case uses ``studio.theme: auto`` plus a conflicting viewer
    ``swiss-dark`` preference. Both are deliberately legacy inputs: they
    normalize to System and Slate. Portable targets use the viewer's System
    configuration; the conflict makes live bootstrap precedence observable.
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


def _ready(page):
    page.wait_for_function("window.OKFLoomTheme && document.documentElement.classList.contains('okf-theme-ready')")


def _choice(page, choice):
    page.evaluate("choice => window.OKFLoomTheme.setTheme(choice)", choice)


def test_live_precedence_system_os_and_saved_choice(configured_live_url, page):
    page.emulate_media(color_scheme="dark")
    page.goto(configured_live_url + _CONCEPT_PATH)
    _ready(page)
    assert page.evaluate("OKFLoomTheme.getChoice()") == "system"
    expect(page.locator("html")).to_have_attribute("data-theme", "slate")
    assert page.evaluate("localStorage.getItem('okf-loom.appearance')") is None
    page.emulate_media(color_scheme="light")
    expect(page.locator("html")).to_have_attribute("data-theme", "bone")
    _choice(page, "clay")
    page.reload()
    _ready(page)
    expect(page.locator("html")).to_have_attribute("data-theme", "clay")
    page.emulate_media(color_scheme="dark")
    expect(page.locator("html")).to_have_attribute("data-theme", "clay")


@pytest.mark.parametrize("target", ["static", "spa", "single-file"])
def test_portable_system_before_first_animation_frame(portable_targets, page, target):
    page.emulate_media(color_scheme="dark")
    page.add_init_script("requestAnimationFrame(() => {window.firstPalette = document.documentElement.dataset.theme;})")
    page.goto(portable_targets[target])
    _ready(page)
    page.wait_for_function("window.firstPalette")
    assert page.evaluate("firstPalette") == "slate"
    assert page.evaluate("OKFLoomTheme.getChoice()") == "system"
    _choice(page, "plum")
    page.reload()
    _ready(page)
    expect(page.locator("html")).to_have_attribute("data-theme", "plum")


def test_five_options_keyboard_tab_escape_and_overlay_stack(configured_live_url, page):
    page.goto(configured_live_url + _CONCEPT_PATH)
    _ready(page)
    trigger = page.locator("#okf-theme")
    trigger.click()
    menu = page.get_by_role("dialog", name="Appearance", exact=True)
    assert menu.get_by_role("radio").count() == 5
    assert menu.locator("fieldset").count() == 1
    expect(menu.locator('[value="system"]')).to_be_focused()
    for key, choice in [("Home", "bone"), ("ArrowDown", "slate"), ("ArrowRight", "clay"),
                        ("ArrowDown", "plum"), ("End", "system"), ("ArrowDown", "bone"),
                        ("ArrowUp", "system")]:
        page.keyboard.press(key)
        radio = menu.locator(f'[value="{choice}"]')
        expect(radio).to_be_checked()
        expect(radio).to_be_focused()
        assert page.evaluate("OKFLoomTheme.getChoice()") == choice
    page.evaluate("window.higherOverlay={close(){}}; OKFOverlayStack.push(higherOverlay)")
    page.keyboard.press("Escape")
    expect(menu).to_be_visible()
    page.evaluate("OKFOverlayStack.remove(higherOverlay)")
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    expect(trigger).to_be_focused()
    trigger.click()
    page.keyboard.press("Tab")
    expect(menu).to_be_hidden()


def test_storage_denial(configured_live_url, page):
    page.add_init_script("Object.defineProperty(window, 'localStorage', {get(){throw new DOMException('denied','SecurityError')}})")
    page.goto(configured_live_url + _CONCEPT_PATH)
    _ready(page)
    _choice(page, "plum")
    expect(page.locator("html")).to_have_attribute("data-theme", "plum")
    page.evaluate("OKFLoomTheme.reset()")
    assert page.evaluate("OKFLoomTheme.getChoice()") == "system"


def test_real_cross_tab_sync(configured_live_url, page):
    page.goto(configured_live_url + _CONCEPT_PATH)
    _ready(page)
    other = page.context.new_page()
    try:
        other.goto(configured_live_url + _CONCEPT_PATH)
        _ready(other)
        _choice(other, "clay")
        expect(page.locator("html")).to_have_attribute("data-theme", "clay")
        other.evaluate("OKFLoomTheme.reset()")
        page.wait_for_function("OKFLoomTheme.getChoice() === 'system'")
    finally:
        other.close()


@pytest.mark.parametrize("theme", ["bone", "slate", "clay", "plum"])
def test_menu_mobile_forced_colors_and_visible_state_cues(configured_live_url, page, theme):
    page.set_viewport_size({"width": 320, "height": 720})
    page.goto(configured_live_url + _CONCEPT_PATH)
    _ready(page)
    _choice(page, theme)
    page.locator("#okf-theme").click()
    menu = page.locator("#okf-appearance-menu")
    expect(menu).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    radio = menu.locator(f'[value="{theme}"]')
    expect(radio).to_be_checked()
    label = menu.locator(f'label[for="okf-appearance-theme-{theme}"]')
    idle = label.evaluate("el => getComputedStyle(el).backgroundColor")
    label.hover()
    hovered = label.evaluate("el => getComputedStyle(el).backgroundColor")
    assert idle != hovered, (theme, idle, hovered)
    cue = label.evaluate("""el => {
      const css = getComputedStyle(el);
      return {shadow: css.boxShadow, border: parseFloat(css.borderLeftWidth),
        separator: parseFloat(css.borderBottomWidth)};
    }""")
    assert cue['separator'] >= 1, cue
    # Accept the approved mockup's inset bar or its border-based equivalent;
    # the selected choice must have a real 2px mark, not just a colour change.
    assert '2px' in cue['shadow'] or cue['border'] >= 2, cue
    page.emulate_media(forced_colors="active")
    expect(radio).to_be_visible()
    expect(radio).to_be_checked()
    page.keyboard.press("Escape")
    expect(page.locator("#okf-theme")).to_be_focused()
