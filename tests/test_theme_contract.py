"""Ledger allow-list, output/first-paint, ownership and accessible-control contracts."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from okf_loom import config, render
from okf_loom.viewer import assets

STATIC = Path(assets.__file__).parent / "static"
PALETTES = ("bone", "slate", "clay", "plum")
CHOICES = (*PALETTES, "system")


def read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def test_allowlists_defaults_and_css_parity():
    js = read("theme.js")
    for name, expected in [("PALETTES", PALETTES), ("CHOICES", CHOICES)]:
        match = re.search(rf"var {name} = (\[[^;]+\]);", js)
        assert tuple(json.loads(match[1])) == expected
    assert render._THEMES == PALETTES and render._THEME_CHOICES == CHOICES
    assert config._ALLOWED_STUDIO_THEMES == assets._ALLOWED_THEMES == set(CHOICES)
    assert config.StudioConfig().theme == assets._DEFAULT_CONFIG["theme"] == render._DEFAULT_THEME == "bone"
    for palette in PALETTES:
        assert f'[data-theme="{palette}"]' in read("wiki.css")
    assert 'var DEFAULT_CHOICE = "bone"' in js


@pytest.mark.parametrize("old,new", config._LEGACY_THEME_MIGRATIONS.items())
def test_alias_parity(old, new):
    assert config.StudioConfig.from_dict({"theme": old}).theme == new
    assert render._normalise_theme_choice(old) == new
    assert render._resolve_initial_theme(old) == ("bone" if new == "system" else new)
    # JS execution proves its mirror, including unquoted object keys.
    from test_theme_js_logic import run
    assert run(storage={"okf-theme": old})[0]["choice"] == new


@pytest.mark.parametrize("value", ["bogus", "Bone", [], 2, "__proto__"])
def test_config_fails_closed(value):
    with pytest.raises(ValueError, match="theme"):
        config.StudioConfig.from_dict({"theme": value})


def test_first_paint_and_non_executable_config():
    for choice in CHOICES:
        html = render._theme_config_html(choice)
        assert 'type="application/json"' in html
        assert json.loads(re.search(r">(.*?)</script>", html)[1]) == {"theme": choice}
        assert render._resolve_initial_theme(choice) == ("bone" if choice == "system" else choice)
    assert render._resolve_initial_theme(None) == "bone"
    templates = Path(assets.__file__).parent / "templates"
    for name in ("concept_page", "index_page", "search_page", "graph_page", "single_file"):
        text = (templates / f"{name}.html").read_text()
        assert text.rindex("__THEME_CONFIG__") < text.rindex("__THEME_HEAD_JS__") < text.index("</head>")
        assert 'font-src' in text and 'data:' in text.split('font-src', 1)[1].split(';', 1)[0]


def test_one_native_radio_group_five_options():
    html = render._appearance_control_html("system")
    assert html.count('type="radio"') == 5
    names = re.findall(r'name="([^"]+)"', html)
    assert len(set(names)) == 1
    for choice in CHOICES:
        assert f'data-okf-val="{choice}"' in html
    assert html.count('class="okf-appearance__swatch') == 5
    assert 'role="dialog"' in html and ' hidden' in html
    assert 'data-okf-set="family"' not in html and 'Advanced' not in html


def test_single_writer_and_palette_consumers():
    for path in STATIC.glob("*.js"):
        if path.name == "theme.js":
            continue
        text = path.read_text()
        assert not re.search(r'localStorage\.(?:setItem|removeItem)\([\"\'](?:okf-theme|okf-loom.appearance)', text), path
        assert not re.search(r'setAttribute\([\"\']data-theme[\"\']', text), path
    js = read("theme.js")
    assert 'okf-loom:themeChanged' in js and 'okf-theme-ready' in js
    assert 'OKFOverlayStack' in js and 'if (!_isTopmost()) return' in js
    for consumer in ("graph.js", "renderers.js"):
        assert 'okf-loom:themeChanged' in read(consumer)
    assert 'getResolvedTheme' in read("capture-readiness.js")


def test_generated_config_and_docs_choices():
    comment = re.search(r'^\s*theme:.*#\s*(.+)$', config.DEFAULT_CONFIG_YAML, re.M)
    assert {v.strip() for v in comment[1].split('|')} == set(CHOICES)
    for choice in CHOICES:
        assert config.StudioConfig.from_dict({"theme": choice}).theme == choice
    for name in ("index_page.html", "search_page.html"):
        text = (Path(assets.__file__).parent / "templates" / name).read_text()
        line = next(line for line in text.splitlines() if "__THEME_ATTR__" in line)
        assert all(p in line for p in PALETTES)
