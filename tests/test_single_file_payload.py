"""Single-file wire deduplication; the in-memory BUNDLE contract is unchanged."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from okf_loom.model import Bundle
from okf_loom.render import (
    _browser_passage_corpus,
    _json_for_script,
    build_graph_data,
    render_single_file,
)


def embedded(html: str) -> dict:
    return json.JSONDecoder().raw_decode(html.split("window.BUNDLE = ", 1)[1])[0]


@pytest.mark.parametrize("bundle_path", ["docs-bundle", "samples/demo_bundle"])
def test_single_file_stores_bodies_and_canonical_passages_once(tmp_path: Path, bundle_path: str) -> None:
    bundle = Bundle.load(Path(__file__).resolve().parents[1] / bundle_path)
    graph = build_graph_data(bundle)
    corpus = _browser_passage_corpus(bundle, graph["edges"])
    out = tmp_path / "index.html"
    render_single_file(bundle, out)
    html = out.read_text(encoding="utf-8")
    data = embedded(html)
    assert data["bodies"] == graph["bodies"]
    for body in set(graph["bodies"].values()):
        assert html.count(_json_for_script(body)) == 1
    assert data["retrieval_corpus"]["manifest"] == corpus["manifest"]
    for wire, original in zip(data["retrieval_corpus"]["chunks"], corpus["chunks"], strict=True):
        assert set(wire) == {"canonical_chunk"}
        assert wire["canonical_chunk"] == original["canonical_chunk"]
        assert json.loads(wire["canonical_chunk"]) == original["payload"]
        assert html.count(_json_for_script(wire["canonical_chunk"])) == 1


def test_single_file_size_growth_tracks_unique_bodies(tmp_path: Path) -> None:
    """Assets cancel: growth permits HTML + search text + one passage copy.

    A second serialized passage payload adds another source-sized copy and
    fails this relative guard, regardless of future JS/font asset sizes.
    """
    root = tmp_path / "bundle"
    root.mkdir()
    measurements = []
    for count in (100, 1000):
        body = "\n\n".join(f"Unique paragraph {i}: durable evidence for portable reading." for i in range(count))
        (root / "concept.md").write_text("---\ntype: concept\ntitle: Evidence\n---\n\n" + body, encoding="utf-8")
        bundle = Bundle.load(root)
        bodies = set(build_graph_data(bundle)["bodies"].values())
        stats = render_single_file(bundle, tmp_path / "index.html")
        measurements.append((stats["bytes"], sum(len(_json_for_script(b).encode()) for b in bodies),
                             len(_json_for_script(body).encode())))
    growth, rendered_growth, source_growth = (b - a for a, b in zip(*measurements))
    assert growth < rendered_growth + 2.5 * source_growth


def test_legacy_single_file_template_retains_serialized_payload(tmp_path: Path, monkeypatch) -> None:
    from okf_loom import render

    root = tmp_path / "bundle"
    root.mkdir()
    (root / "concept.md").write_text("---\ntype: concept\n---\n\nLegacy reader evidence.\n", encoding="utf-8")
    bundle = Bundle.load(root)
    original = render.load_template
    legacy = '<!doctype html><html><head></head><body><script>window.BUNDLE = __BUNDLE_DATA__;</script></body></html>'
    monkeypatch.setattr(render, "load_template", lambda name, owner=None: legacy if owner else original(name))
    out = tmp_path / "index.html"
    render_single_file(bundle, out)
    data = embedded(out.read_text(encoding="utf-8"))
    assert data["retrieval_corpus"] == _browser_passage_corpus(bundle, data["edges"])


def test_single_file_bootstrap_preserves_legacy_payload_access(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node, "Node is required to prove the single-file bootstrap contract"
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "concept.md").write_text(
        '---\ntype: concept\n---\n\n# Evidence\n\nUnicode 🧭 café \u2028 \u2029 & </script> <!-- safely quoted.\n',
        encoding="utf-8",
    )
    bundle = Bundle.load(root)
    out = tmp_path / "index.html"
    render_single_file(bundle, out)
    html = out.read_text(encoding="utf-8")
    bootstrap = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S)
                     if "window.BUNDLE_NAME = " in script)
    expected = build_graph_data(bundle)
    expected["retrieval_corpus"] = _browser_passage_corpus(bundle, expected["edges"])
    program = "var window = {};\n" + bootstrap + """
const assert = require('node:assert/strict');
const chunks = window.BUNDLE.retrieval_corpus.chunks;
assert.equal(typeof Object.getOwnPropertyDescriptor(chunks[0], 'payload').get, 'function');
const payload = chunks[0].payload;
assert.strictEqual(chunks[0].payload, payload);
assert.equal(Object.getOwnPropertyDescriptor(chunks[0], 'payload').writable, true);
console.log(JSON.stringify(window.BUNDLE));
// Assignment, enumeration and subsequent mutation retain the old object API.
assert.deepEqual(Object.keys(chunks[0]), ['canonical_chunk', 'payload']);
chunks[0].payload = {replacement: true};
assert.equal(chunks[0].payload.replacement, true);
""" + bootstrap + """
const fresh = window.BUNDLE.retrieval_corpus.chunks[0];
assert.equal(typeof Object.getOwnPropertyDescriptor(fresh, 'payload').get, 'function');
fresh.payload = {assignedBeforeFirstRead: true};
assert.deepEqual(fresh.payload, {assignedBeforeFirstRead: true});
"""
    result = subprocess.run([node], input=program, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == expected
