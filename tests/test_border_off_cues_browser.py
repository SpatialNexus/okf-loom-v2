"""Borders Off removes frames and nothing else — measured in a real browser.

Borders Off (the Appearance default) zeroes ``--okf-border`` and
``--okf-border-strong``. Hover and press fills, separator glyphs and other
non-frame line work used to read those same tokens, so they vanished with the
frames: a hovered topbar button went from its resting fill to *transparent*,
the rail's Back control lost both its hover and pressed feedback and the
breadcrumb "/" disappeared. They now ride ``--okf-hover-fill``,
``--okf-press-fill``, ``--okf-separator`` and ``--okf-separator-subtle``,
which carry the border colours in every theme and contrast level but are
never touched by Borders Off.

For every theme this proves, with transitions disabled so each read is the
settled state:

* Borders Off is the default and really removes the frames (topbar, topbar
  button, Back control, concept header band);
* hover, press and separator colours are opaque, differ visibly from the
  surface they sit on, and are pixel-identical to the Borders On look.
"""
from __future__ import annotations

import pytest

from browser_support import copy_bundle, launch_chromium, live_server, sync_playwright
from test_viewer_browser import DEMO_BUNDLE

pytestmark = pytest.mark.browser

THEMES = (
    "technical-light", "technical-dark", "swiss-light", "swiss-dark",
    "purple-light", "purple-dark", "coffee-light", "coffee-dark",
)
CASES = [(theme, "normal") for theme in THEMES] + [
    ("technical-light", "soft"), ("coffee-dark", "soft"),
]
CONCEPT = "/tables/orders"  # nested: the breadcrumb has "/" separators

# Normalise any computed CSS colour (rgb(), color(srgb …), oklch()) to RGBA
# through a 1x1 canvas, and composite translucent layers over the nearest
# opaque ancestor so "effective" means what is actually painted.
_PROBE_JS = r"""
(() => {
  const ctx = document.createElement('canvas').getContext('2d', {willReadFrequently: true});
  const rgba = (css) => {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = 'rgba(0, 0, 0, 0)';
    ctx.fillStyle = css;
    ctx.fillRect(0, 0, 1, 1);
    const d = ctx.getImageData(0, 0, 1, 1).data;
    return [d[0], d[1], d[2], d[3] / 255];
  };
  const effectiveBg = (el) => {
    const layers = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const c = rgba(getComputedStyle(n).backgroundColor);
      if (c[3] > 0) layers.push(c);
      if (c[3] >= 1) break;
    }
    let out = [255, 255, 255];
    for (const c of layers.reverse()) {
      out = out.map((v, i) => Math.round(c[i] * c[3] + v * (1 - c[3])));
    }
    return out;
  };
  window.__okfProbe = {rgba, effectiveBg};
})();
"""


def _luminance(rgb) -> float:
    def channel(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(float(v)) for v in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a, b) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.fixture(scope="module")
def demo_url(tmp_path_factory):
    bundle = copy_bundle(DEMO_BUNDLE, tmp_path_factory.mktemp("border-off") / "bundle")
    with live_server(bundle, "--no-watch") as base:
        yield base


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        chromium = launch_chromium(playwright)
        yield chromium
        chromium.close()


def _open(browser, base, theme, contrast, border):
    family, mode = theme.split("-")
    seed = {"okf-theme-family": family, "okf-theme-mode": mode, "okf-theme-contrast": contrast}
    if border is not None:
        seed["okf-theme-border"] = border
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.add_init_script(
        "(() => { const v = %r; for (const k in v) localStorage.setItem(k, v[k]); })()" % seed)
    page = context.new_page()
    page.console_errors = []
    page.on("console", lambda m: page.console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.console_errors.append(str(e)))
    page.goto(base + CONCEPT, wait_until="load")
    page.wait_for_function("() => document.documentElement.classList.contains('okf-theme-ready')")
    page.add_style_tag(content="*, *::before, *::after { transition: none !important; animation: none !important; }")
    page.evaluate(_PROBE_JS)
    return page


def _measure(page) -> dict:
    """Rest/hover/press colours and frame colours for one loaded page."""
    probe = "window.__okfProbe"
    out = {"root": page.evaluate(
        "() => ({theme: document.documentElement.dataset.theme,"
        " border: document.documentElement.dataset.okfBorder,"
        " contrast: document.documentElement.dataset.okfContrast})")}

    # Topbar button (".okf-btn", not the current-page link): rest vs hover.
    button = page.locator(".okf-topbar .okf-btn:not([aria-current])").first
    button.scroll_into_view_if_needed()
    page.mouse.move(1, 899)
    out["btn_rest"] = button.evaluate(f"e => {probe}.effectiveBg(e)")
    out["btn_border"] = button.evaluate(f"e => {probe}.rgba(getComputedStyle(e).borderTopColor)")
    button.hover()
    out["btn_hover"] = button.evaluate(f"e => {probe}.effectiveBg(e)")
    page.mouse.move(1, 899)
    out["topbar_border"] = page.evaluate(
        f"() => {probe}.rgba(getComputedStyle(document.querySelector('.okf-topbar')).borderBottomColor)")

    # Concept header band: its type-tinted frame is a frame too.
    out["header_border"] = page.evaluate(
        f"() => {probe}.rgba(getComputedStyle(document.querySelector('.okf-page__header')).borderTopColor)")

    # Breadcrumb "/" separator against the surface it is painted on.
    sep = page.locator(".okf-breadcrumb__sep").first
    out["sep_color"] = sep.evaluate(f"e => {probe}.rgba(getComputedStyle(e).color)")
    out["sep_bg"] = sep.evaluate(f"e => {probe}.effectiveBg(e)")

    # Split-view divider bar: non-frame line work (probe; no split layout needed).
    out["divider"] = page.evaluate(
        f"""() => {{ const d = document.createElement('div'); d.className = 'okf-split__divider';
             document.body.appendChild(d); const c = {probe}.rgba(getComputedStyle(d).backgroundColor);
             d.remove(); return c; }}""")

    # Rail Back control: only exists after a real Studio -> child step.
    page.locator(".okf-studio-launcher").click()
    page.locator("#okf-panel").get_by_role("button", name="Comments", exact=False).click()
    back = page.locator(".okf-panel__back")
    back.wait_for(state="visible")
    page.mouse.move(1, 899)
    out["back_rest"] = back.evaluate(f"e => {probe}.effectiveBg(e)")
    out["back_border"] = back.evaluate(f"e => {probe}.rgba(getComputedStyle(e).borderTopColor)")
    back.hover()
    out["back_hover"] = back.evaluate(f"e => {probe}.effectiveBg(e)")
    page.mouse.down()
    out["back_press"] = back.evaluate(f"e => {probe}.effectiveBg(e)")
    page.mouse.up()
    out["errors"] = list(page.console_errors)
    return out


@pytest.mark.parametrize("theme,contrast", CASES, ids=[f"{t}-{c}" for t, c in CASES])
def test_border_off_keeps_hover_press_and_separator_cues(demo_url, browser, theme, contrast):
    off_page = _open(browser, demo_url, theme, contrast, border=None)
    off = _measure(off_page)
    off_page.context.close()
    on_page = _open(browser, demo_url, theme, contrast, border="on")
    on = _measure(on_page)
    on_page.context.close()

    assert off["root"] == {"theme": theme, "border": "off", "contrast": contrast}, off["root"]
    assert on["root"] == {"theme": theme, "border": "on", "contrast": contrast}, on["root"]
    assert not off["errors"] and not on["errors"], (off["errors"], on["errors"])

    # Borders Off really removes the frames ...
    for key in ("btn_border", "topbar_border", "header_border", "back_border"):
        assert off[key][3] == 0, f"{theme}/{contrast}: {key} still painted under Borders Off: {off[key]}"
    # ... which Borders On paints.
    for key in ("btn_border", "back_border"):
        assert on[key][3] > 0, f"{theme}/{contrast}: {key} missing under Borders On: {on[key]}"

    # Hover and press feedback survive and read against the resting surface.
    assert _ratio(off["btn_rest"], off["btn_hover"]) >= 1.05, (
        f"{theme}/{contrast}: topbar button hover invisible: {off['btn_rest']} -> {off['btn_hover']}")
    assert _ratio(off["back_rest"], off["back_hover"]) >= 1.05, (
        f"{theme}/{contrast}: Back hover invisible: {off['back_rest']} -> {off['back_hover']}")
    assert _ratio(off["back_hover"], off["back_press"]) >= 1.05, (
        f"{theme}/{contrast}: Back press invisible: {off['back_hover']} -> {off['back_press']}")

    # The breadcrumb separator and the split divider are still painted.
    assert off["sep_color"][3] == 1, f"{theme}/{contrast}: breadcrumb separator transparent: {off['sep_color']}"
    assert _ratio(off["sep_color"], off["sep_bg"]) >= 1.2, (
        f"{theme}/{contrast}: breadcrumb separator lost against {off['sep_bg']}: {off['sep_color']}")
    assert off["divider"][3] == 1, f"{theme}/{contrast}: split divider transparent: {off['divider']}"

    # Borders Off touches none of these: they match the Borders On look.
    for key in ("btn_rest", "btn_hover", "back_rest", "back_hover", "back_press", "sep_color", "divider"):
        assert off[key] == on[key], f"{theme}/{contrast}: {key} changed by Borders Off: {on[key]} -> {off[key]}"
