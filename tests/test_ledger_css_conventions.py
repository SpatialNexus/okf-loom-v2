"""Ledger CSS conventions (acceptance C1/C2), enforced on the source text.

Every viewer stylesheet keeps the Ledger visual system:

* no colour literal (hex, rgb[a](), hsl[a]()) outside the palette token
  blocks, the print palette and forced-colours blocks — every colour comes
  from a token, which is what makes the four palettes free;
* square corners — ``border-radius`` is 0 (or a radius token that is 0);
* no elevation — ``box-shadow`` is ``none``, the floating-layer ring
  ``var(--shadow-pop)`` (or its bridge token), or an unblurred ``inset``
  bar (the 2px state bar / hairline Ledger draws with an inset shadow);
* no gradients.

wiki.css and shell.css are owned by the core lane and must pass. graph.css,
studio.css and feedback-import.css are ported by the graph and Studio lanes
in parallel; their cases are xfail(strict=False) until integration.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "scripts" / "okf_loom" / "viewer" / "static"

CORE = ("wiki.css", "shell.css")
PENDING = ("graph.css", "studio.css", "feedback-import.css")
_PENDING_MARK = pytest.mark.xfail(strict=False, reason="lane C/D in progress")
FILES = [*CORE, *(pytest.param(name, marks=_PENDING_MARK) for name in PENDING)]

_COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(")
_GRADIENT = re.compile(r"\b(?:repeating-)?(?:linear|radial|conic)-gradient\(")
# Selectors whose blocks may define palette colours: the :root / data-theme
# palette blocks (custom properties only).
_TOKEN_SELECTOR = re.compile(r'^(?::root|\[data-theme(?:="[a-z]+")?\])(?:\s*,\s*(?::root|\[data-theme(?:="[a-z]+")?\]))*$')
_ZERO_RADIUS = re.compile(r"^(?:0(?:px)?|var\(--(?:r-[a-z]+|okf-radius(?:-sm|-pill)?)\))(?:\s+(?:0(?:px)?|var\(--[a-z-]+\)))*$")
_ALLOWED_SHADOW_TOKENS = {"none", "var(--shadow-pop)", "var(--okf-shadow-overlay)",
                          "var(--okf-shadow)", "var(--okf-shadow-hover)"}


@dataclass(frozen=True)
class Decl:
    context: tuple[str, ...]  # enclosing at-rule preludes, outermost first
    selector: str
    prop: str
    value: str
    line: int


def _strip_comments(css: str) -> str:
    # Keep newlines so reported line numbers match the source.
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), css, flags=re.S)


def _declarations(css: str) -> list[Decl]:
    """A small CSS walker: enough structure for convention checks."""
    text = _strip_comments(css)
    out: list[Decl] = []
    stack: list[tuple[str, bool]] = []  # (prelude, is_at_rule)
    buf = ""
    line = 1
    buf_line = 1
    in_string: str | None = None
    for ch in text:
        if ch == "\n":
            line += 1
        if in_string:
            buf += ch
            if ch == in_string:
                in_string = None
            continue
        if ch in "\"'":
            in_string = ch
            buf += ch
            continue
        if ch == "{":
            prelude = " ".join(buf.split())
            stack.append((prelude, prelude.startswith("@")))
            buf, buf_line = "", line
        elif ch in ";}":
            decl = buf.strip()
            if decl and stack and not stack[-1][1] and ":" in decl:
                prop, _, value = decl.partition(":")
                at = tuple(p for p, is_at in stack if is_at)
                selector = next((p for p, is_at in reversed(stack) if not is_at), "")
                out.append(Decl(at, selector, prop.strip().lower(), " ".join(value.split()), buf_line))
            buf, buf_line = "", line
            if ch == "}" and stack:
                stack.pop()
        else:
            if not buf.strip():
                buf_line = line
            buf += ch
    return out


def _decls(name: str) -> list[Decl]:
    return _declarations((STATIC / name).read_text(encoding="utf-8"))


def _colour_exempt(decl: Decl) -> bool:
    if any("print" in at or "forced-colors" in at for at in decl.context):
        return True
    return bool(_TOKEN_SELECTOR.match(decl.selector)) and decl.prop.startswith("--")


def test_walker_reads_the_real_stylesheets() -> None:
    decls = _decls("wiki.css")
    assert len(decls) > 1500
    bone = [d for d in decls if d.selector == ':root, [data-theme="bone"]' and d.prop == "--bg"]
    assert bone and bone[0].value.lower() == "#f6f3ed"
    assert any(d.context and "print" in d.context[0] for d in decls)


@pytest.mark.parametrize("name", FILES)
def test_no_colour_literals_outside_token_blocks(name: str) -> None:
    offenders = [f"{name}:{d.line} {d.selector} {{ {d.prop}: {d.value} }}"
                 for d in _decls(name) if _COLOUR.search(d.value) and not _colour_exempt(d)]
    assert not offenders, "colour literals outside palette tokens:\n" + "\n".join(offenders[:40])


@pytest.mark.parametrize("name", FILES)
def test_corners_are_square(name: str) -> None:
    offenders = []
    for d in _decls(name):
        value = d.value.replace("!important", "").strip()
        if d.prop == "border-radius" or d.prop.startswith("border-") and d.prop.endswith("-radius"):
            if not _ZERO_RADIUS.match(value):
                offenders.append(f"{name}:{d.line} {d.selector} {{ {d.prop}: {d.value} }}")
        elif re.fullmatch(r"--(?:r-[a-z]+|okf-radius(?:-sm|-pill)?)", d.prop) and value not in {"0", "0px"}:
            offenders.append(f"{name}:{d.line} {d.selector} {{ {d.prop}: {d.value} }}")
    assert not offenders, "rounded corners:\n" + "\n".join(offenders[:40])


def _shadow_ok(value: str) -> bool:
    value = value.replace("!important", "").strip()
    if value in _ALLOWED_SHADOW_TOKENS:
        return True
    for layer in re.split(r",(?![^(]*\))", value):
        parts = layer.split()
        if not parts or parts[0] != "inset":
            return False
        lengths = [p for p in parts[1:] if re.fullmatch(r"-?[\d.]+(?:px)?", p)]
        # x, y, blur(, spread): an inset bar or hairline never blurs.
        if len(lengths) >= 3 and float(lengths[2].rstrip("px") or 0) != 0:
            return False
    return True


@pytest.mark.parametrize("name", FILES)
def test_no_elevation_shadows(name: str) -> None:
    offenders = [f"{name}:{d.line} {d.selector} {{ box-shadow: {d.value} }}"
                 for d in _decls(name) if d.prop == "box-shadow" and not _shadow_ok(d.value)]
    assert not offenders, "shadows other than --shadow-pop / inset bars:\n" + "\n".join(offenders[:40])


@pytest.mark.parametrize("name", FILES)
def test_no_gradients(name: str) -> None:
    offenders = [f"{name}:{d.line} {d.selector} {{ {d.prop}: {d.value} }}"
                 for d in _decls(name) if _GRADIENT.search(d.value)]
    assert not offenders, "gradients:\n" + "\n".join(offenders[:40])


@pytest.mark.parametrize("name", CORE)
def test_type_colours_are_not_cancelled_with_important(name: str) -> None:
    """C5 at the source: no `!important` override of a type colour remains
    (the renderer no longer emits one to cancel)."""
    css = _strip_comments((STATIC / name).read_text(encoding="utf-8"))
    assert "--okf-type-accent" not in css.replace("--okf-type-accent: var(--text-3)", "")
    assert not re.search(r"\[style\*=", css)
    for d in _decls(name):
        if "!important" in d.value:
            assert d.prop in {"display", "outline", "animation", "transition", "grid-template-columns",
                              "max-width", "grid-column", "grid-row", "position", "max-height",
                              "overflow"}, f"{name}:{d.line} {d.prop}: {d.value}"


def test_no_removed_theme_modifiers_remain() -> None:
    """Contrast/border/density modifiers and family-mode themes are gone."""
    for name in CORE:
        css = _strip_comments((STATIC / name).read_text(encoding="utf-8"))
        assert not re.search(r"data-okf-(?:contrast|border|density)", css), name
        assert not re.search(r'data-theme[$*^]?="?[a-z-]*-dark', css), name
        assert not re.search(r"(?:technical|swiss|purple|coffee)-(?:light|dark)", css), name
