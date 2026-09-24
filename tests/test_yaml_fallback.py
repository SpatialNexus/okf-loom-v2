"""Regression tests for the built-in YAML fallback.

The fallback is intentionally a conservative OKF subset, not a full YAML
implementation.  These tests run in a subprocess with
``OKF_LOOM_FORCE_BUILTIN_YAML=1`` so they exercise the fallback even when PyYAML is
installed in the test environment.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from conftest import TOOLKIT_ROOT


def test_builtin_reads_native_okf02_sequences() -> None:
    pytest.importorskip("yaml")
    code = '''
import yaml as native
from okf_loom import _yaml_compat as builtin
data = {
    "type": "Computation",
    "generated": {"by": "agent:writer", "at": "2026-09-09T00:00:00Z"},
    "verified": [{"by": "human:reviewer", "at": "2026-09-09T00:00:00Z",
                  "sources": ["evidence", "other"]}],
    "sources": [{"id": "evidence", "url": "https://example.org",
                 "valid": {"from": "2026-01-01"}},
                {"id": "other", "members": [{"sources": ["nested"]}]}],
    "deprecated": {"reason": "old", "replaced_by": ["new"]},
    "inputs": [{"sources": ["evidence"], "name": "input"}],
    "parameters": {"window": 7},
    "output": "number",
}
text = native.safe_dump(data, sort_keys=False)
assert "verified:\\n- by:" in text
assert builtin.safe_load(text) == data
'''
    _run_builtin(code)


def _run_builtin(code: str) -> None:
    env = dict(os.environ, OKF_LOOM_FORCE_BUILTIN_YAML="1",
               PYTHONPATH=str(TOOLKIT_ROOT / "scripts"))
    result = subprocess.run([sys.executable, "-c", textwrap.dedent(code)],
                            cwd=TOOLKIT_ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_builtin_mapping_keys_preserve_native_and_builtin_writer_identity() -> None:
    pytest.importorskip("yaml")
    _run_builtin(r'''
import yaml as native
from okf_loom import _yaml_compat as builtin
data = {"custom_namespace:key": "value", "colon: spaced": "kept",
        "reviewer's:key": "apostrophe", 'a"b:key': "quote",
        "true": "string", "123": "string",
        "nested": [{"source:id": "evidence"}], "empty:map": {}}
for flow in (False, True):
    for writer in (native, builtin):
        text = writer.safe_dump(data, sort_keys=False, default_flow_style=flow, width=10000)
        assert builtin.safe_load(text) == data, text
        assert native.safe_load(text) == data, text
assert builtin.safe_load('meta: {custom_namespace:key: value, "other:key":"v"}') == {
    "meta": {"custom_namespace:key": "value", "other:key": "v"}}
assert builtin.safe_load("'reviewer''s:key': value") == {"reviewer's:key": "value"}
assert builtin.safe_load("meta: {'reviewer''s, #key': value}") == {
    "meta": {"reviewer's, #key": "value"}}
assert builtin.safe_load("reviewer's:key: value # comment") == {"reviewer's:key": "value"}
assert builtin.safe_load('meta: {"namespace:key":}') == {"meta": {"namespace:key": None}}
assert builtin.safe_load("items: [human:reviewer, https://example.org]") == {
    "items": ["human:reviewer", "https://example.org"]}
for text in ('meta: {"x": 1, x: 2}', '[a, b]: value',
             'verified:\n- by: human:r\n   at: wrong-indent',
             'custom_namespace:key: value\ncustom_namespace:key: other'):
    try:
        builtin.safe_load(text)
    except builtin.YAMLError:
        pass
    else:
        raise AssertionError(text)
''')


def test_builtin_double_quoted_yaml_escapes_match_native_writer() -> None:
    pytest.importorskip("yaml")
    _run_builtin(r'''
import yaml as native
from okf_loom import _yaml_compat as builtin
data = {"title": "café 🧭", "café:🧭": "evidence",
        "escapes": "\0\a\b\t\n\v\f\r\x1b\x85\xa0\u2028\u2029",
        "literal": r"\xE9 \U0001F9ED", "quote": '"\\/'}
for flow in (False, True):
    text = native.safe_dump(data, allow_unicode=False, default_flow_style=flow, width=10000)
    assert r'\xE9' in text and r'\U0001F9ED' in text
    assert builtin.safe_load(text) == native.safe_load(text) == data
escapes = {
    r'\0': '\0', r'\a': '\a', r'\b': '\b', r'\t': '\t',
    r'\n': '\n', r'\v': '\v', r'\f': '\f', r'\r': '\r',
    r'\e': '\x1b', r'\ ': ' ', r'\"': '"', r'\/': '/', r'\\': '\\',
    r'\N': '\x85', r'\_': '\xa0', r'\L': '\u2028', r'\P': '\u2029',
    r'\xE9': 'é', r'\u263A': '☺', r'\U0001F9ED': '🧭',
    '\\\t': '\t', '': '', '  ': '  ',
}
for encoded, decoded in escapes.items():
    for text, expected in (
        ('value: "' + encoded + '"', {'value': decoded}),
        ('"key:' + encoded + '": value', {'key:' + decoded: 'value'}),
        ('meta: {"key:' + encoded + '": "' + encoded + '"}',
         {'meta': {'key:' + decoded: decoded}}),
    ):
        assert builtin.safe_load(text) == native.safe_load(text) == expected, repr(text)
assert builtin.safe_load(r"value: '\xE9'") == {'value': r'\xE9'}
''')


def test_builtin_double_quoted_invalid_escapes_fail_closed() -> None:
    _run_builtin(r'''
from okf_loom import _yaml_compat as builtin
for encoded in (r'\q', r'\8', r'\x', r'\xE', r'\xGG', r'\u123',
                r'\uZZZZ', r'\U0001F9E', r'\U00110000', r'\UFFFFFFFF',
                r'\uD800', r'\U0000DFFF', 'trailing\\', 'a"b', '\x01'):
    for text in ('value: "' + encoded + '"',
                 '"key:' + encoded + '": value',
                 'meta: {"key:' + encoded + '": "' + encoded + '"}'):
        try:
            builtin.safe_load(text)
        except builtin.YAMLError:
            pass
        else:
            raise AssertionError(repr(text))
for text in (r'"caf\xE9": one' + '\n' + r'"caf\u00E9": two',
             r'meta: {"caf\xE9": one, "caf\u00E9": two}'):
    try:
        builtin.safe_load(text)
    except builtin.YAMLError:
        pass
    else:
        raise AssertionError('decoded duplicate accepted: ' + text)
''')


def test_builtin_yaml_fallback_rejects_unsupported_and_malformed_yaml() -> None:
    """Fallback YAML rejects advanced or ambiguous YAML instead of guessing."""
    code = r'''
from okf_loom import _yaml_compat as yaml

cases = {
    "anchor": "base: &base {x: 1}\n",
    "alias": "copy: *base\n",
    "tag": "x: !foo bar\n",
    "directive": "%YAML 1.2\n---\nx: 1\n",
    "merge": "<<: {x: 1}\n",
    "multi_doc_start": "---\nx: 1\n---\ny: 2\n",
    "multi_doc_end": "x: 1\n...\n",
    "flow_alias": "tags: [*base]\n",
    "flow_anchor": "tags: [&base x]\n",
    "flow_tag": "tags: [!foo]\n",
    "flow_map_alias": "meta: {type: *base}\n",
    "flow_map_tag": "meta: {type: !foo}\n",
    "duplicate_key": "type: Reference\ntype: Tutorial\n",
    "flow_duplicate_key": "meta: {type: Reference, type: Tutorial}\n",
    "unclosed_quote": "title: \"unterminated\n",
    "unbalanced_flow": "tags: [a, {b: c]\n",
}

for name, text in cases.items():
    try:
        yaml.safe_load(text)
    except yaml.YAMLError:
        continue
    raise AssertionError(f"{name} was accepted")

assert yaml.safe_load("type: Reference\ntags: [cli, reference]\n") == {
    "type": "Reference",
    "tags": ["cli", "reference"],
}
assert yaml.safe_load('title: "*accepted*"\n') == {"title": "*accepted*"}
assert yaml.safe_load("title: '*accepted*'\n") == {"title": "*accepted*"}
assert yaml.safe_load('tags: ["*accepted*"]\n') == {"tags": ["*accepted*"]}
'''
    env = os.environ.copy()
    env["OKF_LOOM_FORCE_BUILTIN_YAML"] = "1"
    env["PYTHONPATH"] = str(TOOLKIT_ROOT / "scripts")
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=TOOLKIT_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
