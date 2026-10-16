"""Portable evidence must get identical integrity verdicts without WebCrypto."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from okf_loom import Bundle
from okf_loom.render import _browser_passage_corpus, build_graph_data

RUNTIME = Path(__file__).resolve().parents[1] / "scripts/okf_loom/viewer/static/portable-retrieval.js"


def _node(program: str, value):
    result = subprocess.run(["node", "-e", """
const fs = require('fs'), crypto = require('crypto');
global.window = {TextEncoder: global.TextEncoder};
// Instrument the private digest seam; no test-only public runtime API.
eval(fs.readFileSync(process.argv[1].replace('portable-retrieval.js', 'readable-text.js'), 'utf8'));
const source = fs.readFileSync(process.argv[1], 'utf8');
if (!source.includes('window.okfPortableRetrieval={')) throw Error('missing export');
eval(source.replace('window.okfPortableRetrieval={', 'window.testDigest=digestText;window.okfPortableRetrieval={'));
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => { PROGRAM })().catch(error => { console.error(error); process.exit(1); });
""".replace("PROGRAM", program), str(RUNTIME)], input=json.dumps(value), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_sha256_fallback_known_answers_padding_unicode_and_native_parity():
    # FIPS examples, million-a, both sides of every padding/block boundary,
    # non-BMP UTF-8, NUL, and TextEncoder's replacement for lone surrogates.
    values = ["", "abc", "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq", "a" * 1000000]
    values += ["x" * size for size in (1, 55, 56, 57, 63, 64, 65, 119, 120, 127, 128, 129, 4096)]
    values += ["雪😀 café\u0000e\u0301", "\ud800"]
    output = _node("""
const fallback = []; for (const value of input) fallback.push(await window.testDigest(value));
window.crypto = crypto.webcrypto;
const native = []; for (const value of input) native.push(await window.testDigest(value));
window.crypto = {subtle: {digest() { return Promise.reject(Error('native refused')); }}};
let refused = false; try { await window.testDigest('abc'); } catch (e) { refused = e.message === 'native refused'; }
console.log(JSON.stringify({fallback, native, refused}));
""", values)
    expected = [hashlib.sha256((v if v != "\ud800" else "\ufffd").encode()).hexdigest() for v in values]
    assert output == {"fallback": expected, "native": expected, "refused": True}
    assert expected[0] == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert expected[1] == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert expected[3] == "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"


def test_portable_fallback_accepts_corpus_and_rejects_same_tampering(tiny_good_bundle: Path):
    bundle = Bundle.load(tiny_good_bundle)
    corpus = _browser_passage_corpus(bundle, build_graph_data(bundle)["edges"])
    output = _node("""
const verdicts = [];
for (const native of [false, true]) {
  window.crypto = native ? crypto.webcrypto : undefined;
  const good = await window.okfPortableRetrieval.validate(input.manifest, input.chunks);
  const failures = [];
  for (const kind of ['chunk', 'graph']) {
    const bad = JSON.parse(JSON.stringify(input));
    if (kind === 'chunk') bad.manifest.chunks[0].hash = '0'.repeat(64);
    else bad.manifest.graph.hash = '0'.repeat(64);
    try { await window.okfPortableRetrieval.validate(bad.manifest, bad.chunks); failures.push('accepted'); }
    catch (e) { failures.push(e.message); }
  }
  const provider = window.okfPortableRetrieval.create(input.manifest, good.passages, good.edges);
  verdicts.push({count: good.passages.length, failures, pack: provider.context({concept: input.manifest.concept_ids[0]}, {})});
}
console.log(JSON.stringify(verdicts));
""", corpus)
    assert output[0] == output[1]
    assert output[0]["count"] == corpus["manifest"]["passage_count"]
    assert output[0]["failures"] == ["Portable chunk integrity check failed.", "Portable graph integrity check failed."]
