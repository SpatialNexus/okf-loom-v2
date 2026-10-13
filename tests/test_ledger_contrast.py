"""WCAG contrast of the Ledger palettes, computed from wiki.css (acceptance D1).

The ratios are read from the shipped palette token blocks (not from the
design package), so a token edit that breaks a pair fails here. Pairs: every
text-role/surface pair in the design package's CONTRAST.md, the extra pairs
the viewer actually paints, the four status hues on every surface they can
sit on (AA), and the focus ring / accent bar as non-text indicators (3:1).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_ledger_css_conventions import _TOKEN_SELECTOR, _declarations

WIKI = Path(__file__).resolve().parents[1] / "scripts" / "okf_loom" / "viewer" / "static" / "wiki.css"
PALETTES = ("bone", "slate", "clay", "plum")
SURFACES = ("--bg", "--bg-2", "--surface", "--surface-2")


def _palettes() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for decl in _declarations(WIKI.read_text(encoding="utf-8")):
        if decl.context and any("print" in at for at in decl.context):
            key = "print"
        elif not decl.context and _TOKEN_SELECTOR.match(decl.selector):
            names = re.findall(r'data-theme="([a-z]+)"', decl.selector)
            if not names:
                continue
            key = names[-1]
        else:
            continue
        if re.fullmatch(r"#[0-9a-fA-F]{6}", decl.value):
            out.setdefault(key, {})[decl.prop] = decl.value.lower()
    return out


PAL = _palettes()


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def ratio(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# (foreground, background, minimum) — CONTRAST.md first, then viewer pairs.
TEXT_PAIRS = [
    ("--text", "--bg", 7.0),                 # body text: AAA
    ("--text-2", "--bg", 4.5),
    ("--text-3", "--bg", 4.5),
    ("--text-3", "--bg-2", 4.5),
    ("--text-2", "--surface-2", 4.5),
    ("--accent-text", "--bg", 4.5),
    ("--accent-text", "--surface", 4.5),
    ("--primary-text", "--primary-bg", 4.5),
    ("--dochead-text", "--dochead-bg", 4.5),
    ("--code-text", "--code-bg", 4.5),
    ("--syn-1", "--code-bg", 4.5),
    ("--syn-2", "--code-bg", 4.5),
    ("--syn-3", "--code-bg", 4.5),           # punctuation, comments, language label
    # Pairs the viewer paints beyond the design atlas.
    ("--text", "--surface-2", 4.5),          # hovered rows, raised controls
    ("--text-2", "--bg-2", 4.5),             # header band, view bar, fallback banner
    ("--text-3", "--surface", 4.5),          # spotlight, menus, popovers
    ("--text-2", "--surface", 4.5),
    ("--accent-text", "--bg-2", 4.5),        # links in the header band
    ("--accent-text", "--surface-2", 4.5),   # hovered row number
    ("--link", "--bg", 4.5),
    ("--input-text", "--input-bg", 4.5),
    ("--btn-text", "--btn-hover-bg", 4.5),
    ("--nav-text", "--nav-bg", 7.0),
    ("--nav-text-2", "--nav-bg", 4.5),
    ("--icode-text", "--icode-bg", 4.5),
    ("--hero-text", "--hero-bg", 4.5),
]
STATUS = ("--status-ok", "--status-warn", "--status-error", "--status-info")
# Non-text indicators (WCAG 1.4.11): the focus ring and the 2px accent state
# bar against every surface they are drawn on.
INDICATORS = [(fg, bg) for fg in ("--focus", "--accent") for bg in (*SURFACES, "--dochead-bg")]


def test_every_palette_block_is_complete() -> None:
    assert set(PALETTES) <= set(PAL), sorted(PAL)
    names = {name for fg, bg, _ in TEXT_PAIRS for name in (fg, bg)} | set(STATUS) | {"--focus", "--accent"}
    for palette in PALETTES:
        missing = sorted(names - set(PAL[palette]))
        assert not missing, f"{palette} lacks {missing}"
    # Every palette defines the same token set (no palette inherits Bone).
    reference = set(PAL["bone"])
    for palette in PALETTES[1:]:
        assert set(PAL[palette]) == reference, (palette, sorted(reference ^ set(PAL[palette])))


@pytest.mark.parametrize("palette", PALETTES)
@pytest.mark.parametrize("fg,bg,minimum", TEXT_PAIRS)
def test_text_roles_clear_their_minimum(palette: str, fg: str, bg: str, minimum: float) -> None:
    tokens = PAL[palette]
    measured = ratio(tokens[fg], tokens[bg])
    assert measured >= minimum, f"{palette}: {fg} {tokens[fg]} on {bg} {tokens[bg]} = {measured:.2f} < {minimum}"


@pytest.mark.parametrize("palette", PALETTES)
@pytest.mark.parametrize("status", STATUS)
@pytest.mark.parametrize("surface", SURFACES)
def test_status_hues_clear_aa_on_every_surface(palette: str, status: str, surface: str) -> None:
    tokens = PAL[palette]
    measured = ratio(tokens[status], tokens[surface])
    assert measured >= 4.5, f"{palette}: {status} on {surface} = {measured:.2f}"


@pytest.mark.parametrize("palette", PALETTES)
@pytest.mark.parametrize("fg,bg", INDICATORS)
def test_focus_ring_and_state_bar_clear_3_to_1(palette: str, fg: str, bg: str) -> None:
    tokens = PAL[palette]
    measured = ratio(tokens[fg], tokens[bg])
    assert measured >= 3.0, f"{palette}: {fg} on {bg} = {measured:.2f}"


def test_tertiary_text_on_raised_hover_is_lifted() -> None:
    """Bone's --text-3 is below AA on --surface-2, so every surface that turns
    --surface-2 on hover re-maps --text-3 to --text-2 (which clears it)."""
    assert ratio(PAL["bone"]["--text-3"], PAL["bone"]["--surface-2"]) < 4.5  # why the remap exists
    css = WIKI.read_text(encoding="utf-8")
    remap = re.search(r"([^{}]*)\{\s*--text-3:\s*var\(--text-2\);\s*\}", css)
    assert remap, "hover remap of --text-3 missing"
    for surface in (".okf-card", ".okf-search-results-list > li", ".okf-type-nav__link",
                    ".okf-appearance__opt", ".okf-search-live__option", ".okf-conn"):
        assert surface in remap.group(1), surface
    for palette in PALETTES:
        assert ratio(PAL[palette]["--text-2"], PAL[palette]["--surface-2"]) >= 4.5


def test_print_palette_is_light_paper_with_readable_code() -> None:
    tokens = PAL["print"]
    assert _luminance(tokens["--bg"]) > 0.9, "print paper must be light"
    for fg, bg in (("--text", "--bg"), ("--text-2", "--bg"), ("--text-3", "--bg-2"),
                   ("--code-text", "--code-bg"), ("--syn-1", "--code-bg"), ("--syn-3", "--code-bg"),
                   ("--accent-text", "--bg")):
        assert ratio(tokens[fg], tokens[bg]) >= 4.5, (fg, bg)
