"""Lint all viewer JavaScript files for syntax errors.

Catches the class of bug where a stray brace / missing close breaks the
entire studio.js IIFE, making the live studio unavailable in the browser
with no visible error until the user opens the console.

Run via: ``python3 -m pytest tests/test_js_lint.py -q``
"""
import subprocess
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).parent.parent / "scripts" / "okf_loom" / "viewer" / "static"


def _js_files():
    return sorted(STATIC_DIR.glob("*.js"))


@pytest.mark.parametrize("js_file", _js_files(), ids=lambda p: p.name)
def test_js_syntax_valid(js_file):
    """Every viewer JS file must pass ``node --check`` (no syntax errors)."""
    result = subprocess.run(
        ["node", "--check", str(js_file)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, (
        f"Syntax error in {js_file.name}:\n{result.stderr}"
    )
