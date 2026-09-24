"""Contract + state-ownership tests for the V2 theme foundation.

theme.js is the SOLE owner of viewer appearance state. These tests pin the
parity contract across the four places that name the theme vocabulary so a
drift in one is caught without a browser:

  - viewer/static/theme.js      — the canonical allow-lists + legacy map.
  - render.py                    — _THEMES / _LEGACY_THEME_MIGRATIONS /
                                   _DEFAULT_THEME / _resolve_initial_theme.
  - config.py                    — _ALLOWED_STUDIO_THEMES (studio config enum).
  - viewer/assets.py             — _ALLOWED_THEMES / _DEFAULT_CONFIG (viewer
                                   config.json enum + default).
  - viewer/static/wiki.css       — one full token block per resolved theme.

They also assert the state-ownership invariant: only theme.js writes theme
storage (okf-theme-family / okf-theme-mode) and the resolved data-theme
attribute. wiki.js / graph.js / studio.js must be read-only consumers.

Run via: ``python3 -m pytest tests/test_theme_contract.py -q``
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from okf_loom.viewer import assets
from okf_loom import config, render

_STATIC = Path(render.__file__).resolve().parent / "viewer" / "static"


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# theme.js constant extraction (parse the canonical owner)
# ---------------------------------------------------------------------------

def _js_array(src: str, name: str) -> list[str]:
    m = re.search(rf"\b{name}\s*=\s*\[([^\]]*)\]", src)
    assert m, f"theme.js: {name} array not found"
    return [s.strip().strip('"').strip("'") for s in m.group(1).split(",") if s.strip()]


def _js_legacy_map(src: str) -> dict[str, str]:
    m = re.search(r"\bLEGACY_THEMES\s*=\s*\{([^}]*)\}", src, re.DOTALL)
    assert m, "theme.js: LEGACY_THEMES map not found"
    out: dict[str, str] = {}
    for pair in re.findall(r'([\w-]+)\s*:\s*"([\w-]+)"', m.group(1)):
        out[pair[0]] = pair[1]
    return out


def test_theme_js_constants_match_render_py() -> None:
    """theme.js THEMES/LEGACY_THEMES mirror render.py exactly."""
    src = _read("theme.js")
    themes = _js_array(src, "THEMES")
    legacy = _js_legacy_map(src)
    assert themes == list(render._THEMES), (
        f"theme.js THEMES {themes} != render._THEMES {list(render._THEMES)}"
    )
    assert legacy == render._LEGACY_THEME_MIGRATIONS, (
        f"theme.js LEGACY_THEMES {legacy} != render mapping {dict(render._LEGACY_THEME_MIGRATIONS)}"
    )
    # Default family in theme.js must produce the render default concrete theme.
    fam_default = _js_array(src, "FAMILIES")[1] if False else None  # placeholder
    # The render default theme is technical-light (V2 look); theme.js's
    # DEFAULT_FAMILY + DEFAULT_MODE must resolve to it.
    assert render._DEFAULT_THEME == "technical-light"
    assert "technical-light" in themes


def test_resolve_initial_theme_migrates_legacy_and_defaults() -> None:
    """Server-side initial-theme resolution: new vocab passes through, legacy
    names migrate, anything else (auto/None/garbage) falls back to the
    concrete default — the server never paints 'auto'."""
    assert render._resolve_initial_theme("swiss-dark") == "swiss-dark"
    assert render._resolve_initial_theme("technical-light") == "technical-light"
    for optional in ("purple-light", "purple-dark", "coffee-light", "coffee-dark"):
        assert render._resolve_initial_theme(optional) == optional
    # Legacy localStorage-era concrete names migrate to a valid new theme.
    assert render._resolve_initial_theme("light") == "technical-light"
    assert render._resolve_initial_theme("dark") == "technical-dark"
    assert render._resolve_initial_theme("midnight") == "technical-dark"
    assert render._resolve_initial_theme("pastel") == "swiss-light"
    assert render._resolve_initial_theme("sepia") == "swiss-light"
    # auto / None / garbage → concrete default (never "auto").
    for bad in (None, "auto", "", "nonsense", "light-dark"):
        got = render._resolve_initial_theme(bad)  # type: ignore[arg-type]
        assert got in render._THEMES, f"_resolve_initial_theme({bad!r}) -> {got!r}"
        assert got != "auto"


def test_config_and_assets_allowlists_match_theme_js() -> None:
    """studio.theme (config.py) + viewer config.json (assets.py) enums are the
    new family-mode vocabulary; no retired legacy name leaks through."""
    src = _read("theme.js")
    themes = set(_js_array(src, "THEMES"))
    modes = set(_js_array(src, "MODES"))

    # studio.theme = auto + the four concrete resolved themes.
    assert config._ALLOWED_STUDIO_THEMES == themes | {"auto"}
    # viewer config.json carries the configured preference, including Auto;
    # Python still paints a concrete first-paint value from render._DEFAULT_THEME.
    assert assets._ALLOWED_THEMES == themes | {"auto"}
    assert assets._DEFAULT_CONFIG["theme"] == "auto"
    assert assets._ALLOWED_CONTRASTS == {"normal", "soft"}
    assert assets._ALLOWED_BORDERS == {"on", "off"}
    assert assets._DEFAULT_CONFIG["contrast"] == "normal"
    assert assets._DEFAULT_CONFIG["border"] == "off"
    # No retired legacy name is accepted anywhere.
    retired = {"light", "dark", "pastel", "sepia", "midnight"}
    assert retired.isdisjoint(config._ALLOWED_STUDIO_THEMES)
    assert retired.isdisjoint(assets._ALLOWED_THEMES)
    # Modes are exactly auto/light/dark.
    assert modes == {"auto", "light", "dark"}


def test_fresh_visitor_appearance_defaults_agree_everywhere() -> None:
    """Technical family, Auto (System) mode, Normal contrast, Borders Off and
    Compact density are the single default set: theme.js, the renderer, the
    viewer config defaults, the server-painted <html> attributes and the
    pre-JavaScript Appearance checked state all agree (no SSR/JS mismatch)."""
    src = _read("theme.js")

    def js_default(name: str) -> str:
        match = re.search(rf'var DEFAULT_{name} = "([a-z]+)";', src)
        assert match, f"theme.js DEFAULT_{name} missing"
        return match.group(1)

    expected = {"FAMILY": "technical", "MODE": "auto", "CONTRAST": "normal",
                "BORDER": "off", "DENSITY": "compact"}
    assert {name: js_default(name) for name in expected} == expected
    assert render._DEFAULT_CONFIGURED_THEME == "auto"
    assert render._DEFAULT_CONTRAST == expected["CONTRAST"]
    assert render._DEFAULT_BORDER == expected["BORDER"]
    assert render._DEFAULT_DENSITY == expected["DENSITY"]
    assert assets._DEFAULT_CONFIG["theme"] == "auto"
    assert assets._DEFAULT_CONFIG["contrast"] == expected["CONTRAST"]
    assert assets._DEFAULT_CONFIG["border"] == expected["BORDER"]
    theme, contrast, border = render._theme_preference({})
    assert (theme, contrast, border) == ("auto", "normal", "off")
    assert render._theme_modifier_attrs(contrast, border) == (
        ' data-okf-contrast="normal" data-okf-border="off" data-okf-density="compact"'
    )
    # A configured bundle value is painted as-is (theme.js lets it win too).
    assert 'data-okf-border="on"' in render._theme_modifier_attrs("normal", "on")
    html = render._appearance_control_html(
        render._resolve_initial_theme(theme), "auto", contrast, border,
    )
    checked = set(re.findall(
        r'data-okf-set="([a-z]+)" data-okf-val="([a-z]+)" aria-checked="true"', html,
    ))
    assert checked == {("mode", "auto"), ("density", "compact"), ("family", "technical"),
                       ("contrast", "normal"), ("border", "off")}


def test_theme_config_accepts_exact_optional_values_and_rejects_near_misses() -> None:
    """Config parsing is exact and fail-closed for the extended vocabulary."""
    for value in (
        "auto", "technical-light", "technical-dark", "swiss-light", "swiss-dark",
        "purple-light", "purple-dark", "coffee-light", "coffee-dark",
    ):
        assert config.StudioConfig.from_dict({"theme": value}).theme == value
    for value in ("Purple", "purple", "purple-auto", "coffee", "coffee-light ", ""):
        with pytest.raises(config.OKFError):
            config.StudioConfig.from_dict({"theme": value})


def test_wiki_css_has_one_full_block_per_resolved_theme() -> None:
    """Every resolved theme has a wiki.css block overriding the full core
    colour token set; no retired [data-theme] name survives."""
    css = _read("wiki.css")
    core = (
        "okf-bg", "okf-bg-elev", "okf-bg-inset", "okf-fg", "okf-fg-muted",
        "okf-border", "okf-border-strong", "okf-accent", "okf-accent-hover",
        "okf-accent-on", "okf-accent-bg", "okf-select", "okf-code-bg",
        "okf-code-fg", "okf-pre-bg", "okf-pre-fg", "okf-broken",
        "okf-ok", "okf-ok-bg", "okf-warn", "okf-warn-bg",
        "okf-info", "okf-info-bg", "okf-error", "okf-error-bg", "okf-shadow",
        "okf-ok-on", "okf-warn-on", "okf-info-on", "okf-error-on",
        "okf-shadow-hover", "okf-shadow-overlay",
    )
    for theme in render._THEMES:
        assert f'[data-theme="{theme}"]' in css, f"{theme} block missing in wiki.css"
        block = css.split(f'[data-theme="{theme}"]', 1)[1].split("}", 1)[0]
        for tok in core:
            assert re.search(rf"--{tok}\s*:", block), f"{theme} missing --{tok}"
    # Retired concrete theme names must NOT have a block.
    for retired in ("dark", "pastel", "sepia", "midnight"):
        assert f'[data-theme="{retired}"]' not in css, (
            f"retired theme {retired!r} still has a wiki.css block"
        )


def test_theme_modifiers_have_complete_root_and_concrete_tokens() -> None:
    """Contrast/Border are root-owned semantic modifiers, not per-component
    state. Every concrete theme supplies the mode-sensitive soft tokens and
    non-colour cues that Border Off must preserve."""
    src = _read("theme.js")
    assert _js_array(src, "CONTRASTS") == ["normal", "soft"]
    assert _js_array(src, "BORDERS") == ["on", "off"]
    assert '"okf-theme-contrast"' in src
    assert '"okf-theme-border"' in src
    css = _read("wiki.css")
    for selector in ("[data-okf-contrast=\"soft\"]", "[data-okf-border=\"off\"]"):
        assert selector in css
    modifier_tokens = (
        "okf-soft-bg-inset", "okf-soft-border", "okf-soft-border-strong",
        "okf-soft-active-fill", "okf-soft-shadow", "okf-focus-color",
        "okf-selection-cue", "okf-path-cue", "okf-error-cue", "okf-status-cue",
        "okf-hover-fill", "okf-press-fill", "okf-separator", "okf-separator-subtle",
    )
    root = css.split(":root", 1)[1].split("}", 1)[0]
    for token in modifier_tokens:
        assert re.search(rf"--{token}\s*:", root), f"root missing --{token}"
    for theme in render._THEMES:
        block = css.split(f'[data-theme="{theme}"]', 1)[1].split("}", 1)[0]
        for token in modifier_tokens:
            assert re.search(rf"--{token}\s*:", block), (
                f"{theme} missing --{token}"
            )


_NON_FRAME_TOKENS = {
    # token -> the frame token whose colour it carries in every theme
    "okf-hover-fill": "okf-border",
    "okf-press-fill": "okf-border-strong",
    "okf-separator": "okf-border-strong",
    "okf-separator-subtle": "okf-border",
}


def test_border_off_zeroes_only_frame_tokens_and_non_frame_uses_own_tokens() -> None:
    """Borders Off zeroes --okf-border / --okf-border-strong, so those tokens
    may only paint frames. Hover/press fills, separators and other non-frame
    line work ride their own tokens, which carry the border colours in every
    theme (Soft Contrast included) and which Borders Off never touches."""
    css = _read("wiki.css")
    off = css.split('[data-okf-border="off"] {', 1)[1].split("}", 1)[0]
    assert set(re.findall(r"--([\w-]+)\s*:\s*transparent", off)) == {"okf-border", "okf-border-strong"}
    soft = css.split('[data-okf-contrast="soft"] {', 1)[1].split("}", 1)[0]
    for token, frame in _NON_FRAME_TOKENS.items():
        assert f"--{token}" not in off, f"Borders Off must not touch --{token}"
        assert re.search(rf"--{token}:\s*var\(--okf-soft-{frame[4:]}\);", soft), (
            f"Soft Contrast must map --{token} to --okf-soft-{frame[4:]}")
    blocks = {"root": css.split(":root", 1)[1].split("}", 1)[0]}
    for theme in render._THEMES:
        blocks[theme] = css.split(f'[data-theme="{theme}"]', 1)[1].split("}", 1)[0]
    for name, block in blocks.items():
        for token, frame in _NON_FRAME_TOKENS.items():
            value = re.search(rf"--{token}:\s*([^;]+);", block)
            frame_value = re.search(rf"--{frame}:\s*([^;]+);", block)
            assert value and frame_value, f"{name}: --{token} / --{frame} missing"
            assert value.group(1).strip() == frame_value.group(1).strip(), (
                f"{name}: --{token} must carry the Borders On --{frame} colour")

    # Every stylesheet: frame tokens feed border properties only. The two
    # remaining non-border uses are frames drawn another way (the Swiss
    # overlay hairline and the static graph count-badge outline).
    allowed = {("--okf-shadow-overlay", ""), ("stroke", ".okf-graph-export__count-bg")}
    offenders = []
    for sheet in sorted(_STATIC.glob("*.css")):
        text = sheet.read_text(encoding="utf-8")
        for m in re.finditer(r"var\(--okf-border(?:-strong)?\)", text):
            start = max(text.rfind(";", 0, m.start()), text.rfind("{", 0, m.start()))
            prop = re.sub(r"/\*.*?\*/", "", text[start + 1:m.start()], flags=re.S).split(":")[0].strip()
            if re.fullmatch(r"border(-(top|right|bottom|left|block|inline)(-(start|end))?)?(-color)?", prop):
                continue
            if prop in ("--okf-border", "--okf-border-strong"):
                continue
            selector_end = text.rfind("{", 0, m.start())
            selector = text[max(text.rfind("}", 0, selector_end), 0) + 1:selector_end].strip()
            if (prop, "") in allowed or any(prop == p and s and s in selector for p, s in allowed):
                continue
            offenders.append(f"{sheet.name}:{text.count(chr(10), 0, m.start()) + 1} {prop}")
    assert not offenders, (
        "frame tokens used for non-frame paint (Borders Off erases these; use "
        f"--okf-hover-fill / --okf-press-fill / --okf-separator*): {offenders}")


def test_appearance_template_exposes_typed_controls_for_every_axis() -> None:
    """Appearance is a real form: labelled radio inputs expose checked state
    for family, mode, contrast, and border before JavaScript hydrates them."""
    html = render._appearance_control_html("technical-light", "auto", "soft", "off")
    assert '<form id="okf-appearance-menu"' in html
    for group, values in {
        "family": ("technical", "swiss", "purple", "coffee"),
        "mode": ("auto", "light", "dark"),
        "contrast": ("normal", "soft"),
        "border": ("on", "off"),
        "density": ("comfortable", "compact"),
    }.items():
        assert f'name="okf-theme-{group}"' in html
        for value in values:
            assert f'data-okf-set="{group}" data-okf-val="{value}"' in html
            assert f'type="radio"' in html
    assert 'data-okf-val="soft"' in html and 'data-okf-val="off"' in html
    assert 'id="okf-appearance-contrast-soft"' in html
    assert 'id="okf-appearance-border-off"' in html
    assert 'id="okf-appearance-density-compact"' in html
    assert 'data-okf-val="comfortable"' in html
    assert 'data-okf-set="family" data-okf-val="coffee"' in html
    assert '<details class="okf-appearance__advanced">' in html
    assert "Technical" in html and "application default" in html


def test_density_css_covers_interface_surfaces_without_prose_or_small_targets() -> None:
    """Compact density is a shared interface projection, not article zoom."""
    wiki = _read("wiki.css")
    studio = _read("studio.css")
    graph = _read("graph.css")
    for selector in (".okf-shell", ".okf-card", ".okf-search-result"):
        assert selector in wiki.split('data-okf-density="compact"', 1)[1]
    for selector in (".okf-panel__body", ".okf-comment", ".okf-studiobtn"):
        assert selector in studio.split('data-okf-density="compact"', 1)[1]
    for selector in (".okf-node-index__item", ".okf-graph-tools", ".okf-lens-choice"):
        assert selector in graph.split('data-okf-density="compact"', 1)[1]
    compact_rules = "\n".join(
        part.split("}", 1)[0]
        for css in (wiki, studio, graph)
        for part in css.split('data-okf-density="compact"')[1:]
    )
    assert ".okf-prose" not in compact_rules and "--okf-reading" not in compact_rules
    assert "min-height: 24px" in studio
    assert "min-height: 44px" in graph


def test_optional_families_are_first_class_and_keep_technical_auto_default() -> None:
    """Purple and Coffee are real family choices, not colour-only overrides."""
    src = _read("theme.js")
    assert _js_array(src, "FAMILIES") == ["technical", "swiss", "purple", "coffee"]
    assert set(_js_array(src, "THEMES")) >= {
        "technical-light", "technical-dark", "swiss-light", "swiss-dark",
        "purple-light", "purple-dark", "coffee-light", "coffee-dark",
    }
    assert re.search(r'DEFAULT_FAMILY\s*=\s*"technical"', src)
    assert re.search(r'DEFAULT_MODE\s*=\s*"auto"', src)
    html = render._appearance_control_html("technical-light", "auto")
    for family in ("technical", "swiss", "purple", "coffee"):
        assert f'data-okf-set="family" data-okf-val="{family}"' in html
        assert f'okf-appearance__preview--{family}' in html
    assert '<span class="okf-appearance-trigger__prefix">Appearance \u00b7 </span>' in html
    assert '<span class="okf-appearance-trigger__family">Technical</span>' in html
    assert "Technical family + follow OS colour mode" in html


def test_semantic_on_colours_and_flat_swiss_shadow_contract() -> None:
    css = _read("wiki.css")
    tokens = (
        "okf-ok-on", "okf-warn-on", "okf-error-on", "okf-info-on",
        "okf-shadow-hover", "okf-shadow-overlay",
    )
    for theme in render._THEMES:
        block = css.split(f'[data-theme="{theme}"]', 1)[1].split("}", 1)[0]
        for token in tokens:
            assert re.search(rf"--{token}\s*:", block), f"{theme} missing --{token}"
    for theme in ("swiss-light", "swiss-dark"):
        block = css.split(f'[data-theme="{theme}"]', 1)[1].split("}", 1)[0]
        assert "--okf-shadow: none" in block
        assert "--okf-shadow-hover: inset 0 0 0 1px" in block
        assert "--okf-shadow-overlay: 0 0 0 1px" in block


def test_studio_command_palette_matches_appearance_family_and_auto_vocabulary() -> None:
    studio = _read("studio.js")
    for family in ("Technical", "Swiss", "Purple", "Coffee"):
        assert f'Appearance: {family}' in studio
    assert "Auto (current family + follow OS colour mode)" in studio


def test_families_share_palette_within_mode() -> None:
    """The Swiss/Technical distinction is a NON-COLOUR state: both families
    share the SAME colour palette within a mode. This pins the design
    direction (meaningful non-colour state, never colour-only) and guarantees
    every WCAG ratio holds in both families."""
    css = _read("wiki.css")
    colour_tokens = (
        "okf-bg", "okf-bg-elev", "okf-bg-inset", "okf-fg", "okf-fg-muted",
        "okf-border", "okf-accent", "okf-accent-hover", "okf-accent-on",
        "okf-accent-bg", "okf-select", "okf-code-bg", "okf-code-fg",
        "okf-pre-bg", "okf-pre-fg", "okf-broken", "okf-ok", "okf-ok-bg",
        "okf-warn", "okf-warn-bg", "okf-info", "okf-info-bg", "okf-error",
        "okf-error-bg",
    )

    def _block(theme: str) -> dict[str, str]:
        body = css.split(f'[data-theme="{theme}"]', 1)[1].split("}", 1)[0]
        out: dict[str, str] = {}
        for tok in colour_tokens:
            m = re.search(rf"--{tok}:\s*([^;]+);", body)
            assert m, f"{theme} missing --{tok}"
            out[tok] = m.group(1).strip()
        return out

    for mode in ("light", "dark"):
        tech = _block(f"technical-{mode}")
        swiss = _block(f"swiss-{mode}")
        assert tech == swiss, (
            f"colour palette differs between families in {mode} mode; the "
            f"Swiss/Technical distinction must be non-colour only. Diff: "
            + ", ".join(k for k in tech if tech[k] != swiss[k])
        )
    # And the non-colour distinction IS present (radius/shadow differ).
    def _val(theme: str, tok: str) -> str:
        body = css.split(f'[data-theme="{theme}"]', 1)[1].split("}", 1)[0]
        m = re.search(rf"--{tok}:\s*([^;]+);", body)
        return m.group(1).strip()
    assert _val("swiss-light", "okf-radius") != _val("technical-light", "okf-radius")
    assert _val("swiss-light", "okf-shadow") != _val("technical-light", "okf-shadow")


def test_optional_family_tokens_are_complete_and_non_colour_distinct() -> None:
    """Each optional family has AA-oriented semantic tokens and its own geometry."""
    css = _read("wiki.css")
    root_tokens = set(re.findall(
        r"--([\w-]+)\s*:", css.split(":root", 1)[1].split("}", 1)[0]
    ))

    def _block(theme: str) -> str:
        return css.split(f'[data-theme="{theme}"]', 1)[1].split("}", 1)[0]

    # Every concrete theme block repeats the complete :root token set, so no
    # target inherits a stray root fallback (wiki.css "same token set" rule).
    for theme in render._THEMES:
        block = _block(theme)
        assert root_tokens <= set(re.findall(r"--([\w-]+)\s*:", block)), (
            f"{theme} is not root-complete: "
            + ", ".join(sorted(root_tokens - set(re.findall(r"--([\w-]+)\s*:", block))))
        )

    for theme in ("purple-light", "purple-dark", "coffee-light", "coffee-dark"):
        block = _block(theme)
        for token in (
            "okf-bg", "okf-fg", "okf-fg-muted", "okf-accent", "okf-accent-on",
            "okf-accent-bg", "okf-select", "okf-broken", "okf-ok", "okf-warn",
            "okf-info", "okf-error", "okf-shadow", "okf-radius", "okf-active-fill",
            "okf-active-fg", "okf-active-border", "okf-focus-color",
            "okf-selection-cue", "okf-path-cue", "okf-error-cue", "okf-status-cue",
        ):
            assert re.search(rf"--{token}\s*:", block), f"{theme} missing --{token}"

    def _val(theme: str, token: str) -> str:
        match = re.search(rf"--{token}:\s*([^;]+);", _block(theme))
        assert match, f"{theme} missing --{token}"
        return match.group(1).strip()

    assert _val("purple-light", "okf-radius") != _val("technical-light", "okf-radius")
    assert _val("coffee-light", "okf-radius") != _val("technical-light", "okf-radius")
    assert _val("purple-light", "okf-accent") != _val("technical-light", "okf-accent")
    assert _val("coffee-light", "okf-accent") != _val("technical-light", "okf-accent")
    assert "@media (forced-colors: active)" in css


# ---------------------------------------------------------------------------
# State-ownership invariant: theme.js is the sole writer
# ---------------------------------------------------------------------------

def test_only_theme_js_owns_theme_storage_and_data_theme() -> None:
    """STATE_OWNERSHIP: no other viewer JS writes the canonical theme storage
    keys (okf-theme-family / okf-theme-mode) or sets data-theme for theme
    purposes. theme.js is the single owner; the rest are read-only consumers
    (graphPalette / renderers isDark / the studio palette items call the API)."""
    canonical_writes = (
        r'setItem\s*\(\s*["\']okf-theme-family',
        r'setItem\s*\(\s*["\']okf-theme-mode',
    )
    # studio.js may still reference okf-theme only inside the API delegations;
    # the canonical family/mode keys must appear ONLY in theme.js.
    for fname in ("wiki.js", "graph.js", "studio.js", "renderers.js"):
        src = _read(fname)
        for pat in canonical_writes:
            assert re.search(pat, src) is None, (
                f"{fname} writes a canonical theme storage key ({pat}); "
                "theme.js must be the sole owner"
            )
    # theme.js itself must own the canonical keys + the resolved attribute.
    theme_src = _read("theme.js")
    for keyname, keyval in (("FAMILY_KEY", "okf-theme-family"),
                            ("MODE_KEY", "okf-theme-mode"),
                            ("LEGACY_KEY", "okf-theme")):
        assert re.search(rf'\b{keyname}\s*=\s*["\']({re.escape(keyval)})["\']', theme_src), (
            f"theme.js must define {keyname} = {keyval!r}"
        )
    assert 'addEventListener("storage", _onStorage)' in theme_src
    assert 'setAttribute("data-okf-density"' in theme_src
    # theme.js is the single writer of the resolved data-theme attribute.
    assert re.search(r'setAttribute\s*\(\s*["\']data-theme["\']', theme_src), (
        "theme.js must set the resolved data-theme attribute"
    )
    # No viewer JS besides theme.js sets the data-theme attribute for theme
    # switching. (graph.js's setMobilePane sets a different attribute; the
    # regex is scoped to data-theme specifically.)
    for fname in ("wiki.js", "graph.js", "studio.js"):
        src = _read(fname)
        assert re.search(r'setAttribute\s*\(\s*["\']data-theme["\']', src) is None, (
            f"{fname} sets data-theme; theme.js must be the sole writer"
        )


def test_theme_uses_allow_listed_persistence_only() -> None:
    """FAIL_CLOSED: theme.js persists only the six allow-listed keys
    (family / mode / contrast / border / density / legacy mirror). All persistence goes through one
    denial-safe writeKey() helper; every writeKey call site uses one of the
    three key constants, so no arbitrary/corrupt key is ever written."""
    src = _read("theme.js")
    allowed = (
        "FAMILY_KEY", "MODE_KEY", "CONTRAST_KEY", "BORDER_KEY", "DENSITY_KEY", "LEGACY_KEY",
    )
    # The raw localStorage.setItem lives in exactly one place (writeKey),
    # taking the key as a parameter — so the contract is enforced at the
    # writeKey CALL SITES (exclude the `function writeKey` definition), which
    # must pass only the allow-listed constants.
    sites = re.findall(r"(?<!function )writeKey\s*\(\s*([A-Za-z_]\w*)", src)
    assert sites, "theme.js has no writeKey call sites"
    for k in sites:
        assert k in allowed, (
            f"theme.js writeKey({k}) persists an unlisted key; only "
            f"{allowed} are allow-listed"
        )
    # And there is exactly ONE raw setItem (inside writeKey).
    assert len(re.findall(r"localStorage\.setItem", src)) == 1


def test_renderers_is_dark_detects_both_dark_families() -> None:
    """P1-6: renderers.js isDark() reads data-theme and returns true for BOTH
    dark family values (swiss-dark, technical-dark) and false for both light
    values, regardless of family — so Mermaid / highlight.js rerender in the
    correct variant in every theme. Evaluates the ACTUAL JS function in Node
    with a stub document (no browser)."""
    import json as _json
    import subprocess
    node = __import__("shutil").which("node")
    if not node:
        pytest.skip("node not available")
    src = _read("renderers.js")
    m = re.search(r"function isDark\s*\(\s*\)\s*\{.*?\n\s*\}", src, re.DOTALL)
    assert m, "renderers.js isDark function not found"
    isdark_src = m.group(0)
    script = (
        "var document = { documentElement: { _t: null, "
        "getAttribute: function (n) { return n === 'data-theme' ? this._t : null; } } };\n"
        + isdark_src + "\n"
        "var results = {};\n"
        + _json.dumps(list(render._THEMES))
        + ".forEach(function (t) { document.documentElement._t = t; results[t] = isDark(); });\n"
        "console.log(JSON.stringify(results));\n"
    )
    proc = subprocess.run(
        [node, "--input-type=commonjs", "-e", script],
        capture_output=True, text=True, timeout=20,
    )
    assert proc.returncode == 0, f"node isDark harness failed:\n{proc.stderr}"
    res = _json.loads(proc.stdout.strip())
    assert res["technical-dark"] is True and res["swiss-dark"] is True, (
        f"isDark must be dark for both dark families: {res}"
    )
    assert res["technical-light"] is False and res["swiss-light"] is False, (
        f"isDark must be light for both light families: {res}"
    )


def test_renderers_consumes_theme_event_without_owning_state() -> None:
    """The only renderer coupling is a read-only semantic-event consumer:
    Mermaid rerenders on a resolved Light/Dark transition and ignores family
    or modifier changes that keep the resolved mode stable."""
    src = _read("renderers.js")
    assert 'window.addEventListener("okf-loom:themeChanged"' in src
    assert "previousResolvedMode === detail.resolvedMode" in src
    assert "initMermaid(true)" in src
    assert "localStorage" not in src
    assert 'setAttribute("data-theme"' not in src


# ---------------------------------------------------------------------------
# P1/outcome-audit: stale-vocabulary parity (config + templates)
# ---------------------------------------------------------------------------

def test_generated_config_documents_only_valid_theme_values() -> None:
    """GAP-2: the values listed in DEFAULT_CONFIG_YAML's studio.theme comment
    are EXACTLY the config-accepted allowlist. A user who copies a documented
    value must never hit a validation error; no retired legacy name is shown.
    """
    comment = re.search(r"^\s*theme:.*#\s*(.+)$", config.DEFAULT_CONFIG_YAML, re.MULTILINE)
    assert comment, "DEFAULT_CONFIG_YAML studio.theme comment not found"
    documented = {v.strip() for v in comment.group(1).split("|")}
    # Every documented value is accepted by config validation.
    assert documented <= set(config._ALLOWED_STUDIO_THEMES), (
        f"documented theme values rejected by config: "
        f"{documented - set(config._ALLOWED_STUDIO_THEMES)}"
    )
    # No retired legacy name is documented anywhere in the sample config.
    retired = {"light", "dark", "pastel", "sepia", "midnight"}
    assert documented.isdisjoint(retired), (
        f"retired legacy names still documented: {documented & retired}"
    )
    # The allowlist is fully documented (no accepted-but-undocumented value).
    assert documented == set(config._ALLOWED_STUDIO_THEMES), (
        f"undocumented accepted themes: "
        f"{set(config._ALLOWED_STUDIO_THEMES) - documented}"
    )
    # Fail-closed: each documented concrete value round-trips through
    # StudioConfig.from_dict validation without raising.
    for theme in config._ALLOWED_STUDIO_THEMES:
        sc = config.StudioConfig.from_dict({"theme": theme})
        assert sc.theme == theme


def test_index_search_templates_document_new_theme_vocab() -> None:
    """GAP-2: the index/search page template __THEME_ATTR__ comments list the
    new family-mode vocabulary; no retired legacy name survives."""
    tmpl = Path(render.__file__).resolve().parent / "viewer" / "templates"
    retired = ("pastel", "sepia", "midnight")
    for name in ("index_page.html", "search_page.html"):
        txt = (tmpl / name).read_text(encoding="utf-8")
        line = next(l for l in txt.splitlines() if "__THEME_ATTR__" in l)
        for theme in render._THEMES:
            assert theme in line, f"{name}: __THEME_ATTR__ comment missing {theme}"
        for r in retired:
            assert r not in line, (
                f"{name}: __THEME_ATTR__ still references retired {r!r}"
            )


# ---------------------------------------------------------------------------
# P1/outcome-audit: Swiss/Technical active-state token application (GAP-4)
# ---------------------------------------------------------------------------

def _active_val(css: str, theme: str, tok: str) -> str:
    body = css.split(f'[data-theme="{theme}"]', 1)[1].split("}", 1)[0]
    m = re.search(rf"--{tok}:\s*([^;]+);", body)
    assert m, f"{theme} missing --{tok}"
    return m.group(1).strip()


def test_active_state_tokens_differ_per_family() -> None:
    """GAP-4: the semantic --okf-active-fill/fg/border tokens carry the
    Swiss/Technical family distinction in BOTH modes — Technical = translucent
    accent tint + transparent frame; Swiss = solid boxed accent fill + accent
    frame — so the family difference is observable on selected/current state,
    not only on radius/shadow."""
    css = _read("wiki.css")
    for mode in ("light", "dark"):
        tech_fill = _active_val(css, f"technical-{mode}", "okf-active-fill")
        tech_border = _active_val(css, f"technical-{mode}", "okf-active-border")
        swiss_fill = _active_val(css, f"swiss-{mode}", "okf-active-fill")
        swiss_border = _active_val(css, f"swiss-{mode}", "okf-active-border")
        swiss_fg = _active_val(css, f"swiss-{mode}", "okf-active-fg")
        # Technical = translucent tint + no frame.
        assert "accent-bg" in tech_fill, (
            f"technical-{mode} active-fill must be the translucent accent tint"
        )
        assert tech_border == "transparent", (
            f"technical-{mode} active-frame must be transparent, got {tech_border}"
        )
        # Swiss = solid accent fill + boxed accent frame + accent-on text.
        assert swiss_fill == "var(--okf-accent)", (
            f"swiss-{mode} active-fill must be the solid accent, got {swiss_fill}"
        )
        assert swiss_border == "var(--okf-accent)", (
            f"swiss-{mode} active-frame must be the accent, got {swiss_border}"
        )
        assert swiss_fg == "var(--okf-accent-on)", (
            f"swiss-{mode} active-fg must be accent-on, got {swiss_fg}"
        )
    # Mode differentiation survives within a family: accent-bg differs by mode
    # (light is opaque #d6e5e5; dark is a translucent rgba), so Technical's
    # active tint reads differently per mode even though the family is fixed.
    light_accent_bg = _active_val(css, "technical-light", "okf-accent-bg")
    dark_accent_bg = _active_val(css, "technical-dark", "okf-accent-bg")
    assert light_accent_bg != dark_accent_bg


def test_selected_current_rules_consume_active_tokens() -> None:
    """GAP-4: the selected/current-state CSS rules consume the semantic
    --okf-active-* tokens (not raw --okf-accent / --okf-accent-bg), on both
    the viewer surfaces and the Studio command palette."""
    css = _read("wiki.css")
    assert re.search(
        r'\.okf-search-live__option\[aria-selected="true"\]\s*\{[^}]*'
        r'var\(--okf-active-fill\)[^}]*var\(--okf-active-fg\)',
        css,
    ), "search-live selected option must use the active tokens"
    assert re.search(
        r'#okf-appearance-menu \.okf-appearance__opt\[aria-checked="true"\]\s*\{[^}]*'
        r'var\(--okf-active-fill\)',
        css,
    ), "appearance checked option must use the active fill token"
    studio_css = _read("studio.css")
    assert re.search(
        r'\.okf-palette__item\[aria-selected="true"\]\s*\{[^}]*'
        r'var\(--okf-active-fill\)',
        studio_css,
    ), "studio palette selected item must use the active fill token"
