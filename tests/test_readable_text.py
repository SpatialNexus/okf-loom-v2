"""Reader-only Markdown previews agree in Python and every browser surface."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from okf_loom.viewer.text import readable_text

VECTORS = [
    ("| **theme** | enum | `bone` |", "theme · enum · bone"),
    ("| Field | Type |\n| :--- | ---: |\n| `studio.*` | *enum* |", "Field · Type studio.* · enum"),
    ("## **Heading**\n> 1. [a link](/a(b).md) and ![image](x)\n- [x] ~~old~~ __new__ snake_case_names", "Heading a link and image old new snake_case_names"),
    ("```python\nprint('hello')\n```", "print('hello')"),
    ("`__init__` and \\*literal\\*", "__init__ and *literal*"),
    ("Every `scripts/okf-loom` subcommand &amp; **flag**.", "Every scripts/okf-loom subcommand & flag."),
    ('<img src=x onerror=alert(1)> <script>evil()</script> &lt;svg onload=x&gt;', 'evil() <svg onload=x>'),
    ("[reference][id] <!-- hidden -->[^note] &mdash; &#x1f600;", "reference — 😀"),
    ("\uE000999\uE001 and 42", "\uE000999\uE001 and 42"),
]


@pytest.mark.parametrize("source,expected", VECTORS)
def test_readable_text_is_plain_and_preserves_code(source, expected):
    assert readable_text(source) == expected


def test_browser_highlights_only_complete_non_stopword_terms():
    asset = Path(__file__).resolve().parents[1] / "scripts/okf_loom/viewer/static/readable-text.js"
    result = subprocess.run(["node", "-e", """
const fs=require('fs'); global.window={}; eval(fs.readFileSync(process.argv[1],'utf8'));
const pattern=window.okfHighlightPattern('spec and the café snake_case');
console.log(JSON.stringify('Special specification inspection spec SPEC and command the themes café cafés snake_case'.match(pattern)));
""", str(asset)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["spec", "SPEC", "café", "snake_case"]


def test_browser_projection_matches_python_vectors():
    asset = Path(__file__).resolve().parents[1] / "scripts/okf_loom/viewer/static/readable-text.js"
    result = subprocess.run(["node", "-e", """
const fs=require('fs'); global.window={}; eval(fs.readFileSync(process.argv[1],'utf8'));
console.log(JSON.stringify(JSON.parse(fs.readFileSync(0,'utf8')).map(window.okfReadableText)));
""", str(asset)], input=json.dumps([v[0] for v in VECTORS]), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [v[1] for v in VECTORS]
