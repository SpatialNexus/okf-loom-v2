"""Normalized metadata is copied as bounded data, not as exact body evidence."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

from okf_loom import Bundle
from okf_loom.render import _browser_passage_corpus
from okf_loom.retrieval import (
    ContextRequest, RetrievalContractError, RetrievalService,
    _metadata_markdown, governed_item_token_cost,
)
from test_render import _portable_node
from conftest import okf_subprocess_env


ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = "https://example.org/evidence?a=1&b=2"


def _fixture(tmp_path: Path, overrides: dict | None = None) -> Bundle:
    fm = {
        "type": "Attested Computation", "title": "Evidence",
        "timestamp": "2020-01-01T00:00:00Z",
        "generated": {"by": "process:fixture", "at": "2026-09-01T00:00:00Z"},
        "verified": {"by": "human:fixture", "at": "2026-09-02T00:00:00Z"},
        "status": "draft", "stale_after": "2026-09-08T00:00:00Z",
        "usage_window": {"from": "2026-08-01T00:00:00Z", "to": "2026-09-01T00:00:00Z"},
        "sources": [{"id": "web", "resource": SOURCE_URL, "usage_count": 0}],
        "runtime": "sql", "parameters": [{"name": "year", "type": "integer", "required": True}],
        "computation": "DO-NOT-OPEN.sql",
        "executor": {"resource": "https://example.org/DO-NOT-FETCH", "receipt": ["run_id", "result"]},
        "attester": {"resource": "DO-NOT-IMPORT.py"},
        "provenance": {"source": "legacy provenance"},
        "citations": [{"id": "web", "text": "Separate Loom citation"}],
        "unknown_private_key": "not part of normalized families",
    }
    fm.update(overrides or {})
    (tmp_path / "index.md").write_text("---\nokf: '0.2'\n---\n# Bundle\n")
    (tmp_path / "evidence.md").write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=False) + "---\n"
        "# Claim\nEvidence claim[^web].\n", encoding="utf-8",
    )
    return Bundle.load(tmp_path)


def _pack(bundle: Bundle, **kwargs) -> dict:
    return RetrievalService(bundle).context(ContextRequest(
        query="Evidence", concept="evidence", mode="lexical", neighbors=0, limit=1, **kwargs,
    ))


def _data(markdown: str) -> dict:
    blocks = re.findall(r"^```json\n(.*?)\n```$", markdown, re.M | re.S)
    assert len(blocks) == 1
    return json.loads(blocks[0])


def _portable(bundle: Bundle) -> dict:
    return _portable_node(_browser_passage_corpus(bundle, []), r"""
const validated=await window.okfPortableRetrieval.validate(artifact.manifest,artifact.chunks);
const p=window.okfPortableRetrieval.create(artifact.manifest,validated.passages,validated.edges);
const target={concept:'evidence',query:'Evidence'};
const options={query:'Evidence',mode:'lexical',neighbors:0,limit:1,budget:50000};
const pack=p.context(target,options);
const cost=pack.budget.estimated_tokens;
const exact=p.context(target,Object.assign({},options,{budget:cost}));
const short=p.context(target,Object.assign({},options,{budget:cost-1}));
const selected=Object.assign({},target,{selection_exact:'Evidence claim[^web].'});
const selectedPack=p.context(selected,options);
let selectionRejected=false;
try{p.context(selected,Object.assign({},options,{budget:selectedPack.budget.estimated_tokens-1}));}
catch(e){selectionRejected=/cannot include the exact selection/.test(e.message);}
const legacy=JSON.parse(JSON.stringify(artifact.manifest));
legacy.concepts.forEach(c=>{delete c.metadata;delete c.metadata_json;});
await window.okfPortableRetrieval.validate(legacy,artifact.chunks);
const legacyPack=window.okfPortableRetrieval.create(legacy,validated.passages,validated.edges).context(target,options);
console.log(JSON.stringify({pack,again:p.markdown(pack),exact,short,selectionRejected,legacyPack}));
""")


def test_default_cli_demo_keeps_claim_and_source_url() -> None:
    command = [str(ROOT / "scripts/okf-loom"), "context", str(ROOT / "docs-bundle"),
               "format records computation metadata", "--concept", "demo/okf02_computation",
               "--neighbors", "0", "--limit", "1"]
    # Omitted --format is the user-facing default, not a formatter-only proof.
    markdown = subprocess.run(command, capture_output=True, text=True, check=True,
                              env=okf_subprocess_env(), timeout=30).stdout
    pack = json.loads(subprocess.run(command + ["--format", "json"], capture_output=True,
                                    text=True, check=True, env=okf_subprocess_env(), timeout=30).stdout)
    assert markdown == pack["representations"]["markdown"]
    item = pack["items"][0]
    assert "runtime protocol.[^okf02]" in item["excerpt"]
    assert item["citation"] in markdown and item["excerpt"] in markdown
    source = next(s for s in _data(markdown)["sources"] if s["id"] == "okf02")
    assert source["resource"] == (
        "https://raw.githubusercontent.com/GoogleCloudPlatform/open-knowledge-format/main/SPEC.md"
    )
    assert source["resource"] in markdown


def test_metadata_markdown_python_node_parity_and_budget(tmp_path: Path) -> None:
    bundle = _fixture(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.glob("*.md")}
    pack = _pack(bundle, budget_tokens=50000)
    item = pack["items"][0]
    markdown = pack["representations"]["markdown"]
    metadata = _data(markdown)
    assert metadata == item["metadata"]
    assert metadata["trust_tier"] == "human-reviewed"
    assert metadata["verified"] == [{"by": "human:fixture", "at": "2026-09-02T00:00:00+00:00"}]
    assert metadata["generated_at_source"] == "generated.at"
    assert metadata["generated_at"] == "2026-09-01T00:00:00+00:00"
    assert metadata["status"] == "draft" and metadata["stale_after"] == "2026-09-08T00:00:00+00:00"
    assert "stale" not in metadata  # No clock-dependent derived verdict in a cached pack.
    assert metadata["sources"][0]["resource"] == SOURCE_URL
    assert metadata["sources"][0]["usage_window"] == metadata["usage_window"]
    assert metadata["sources"][0]["usage_count"] == 0
    assert metadata["runtime"] == "sql" and metadata["parameters"][0]["required"] is True
    assert metadata["executor"]["receipt"] == ["run_id", "result"]
    assert metadata["computation"] == "DO-NOT-OPEN.sql"
    assert metadata["attester"]["resource"] == "DO-NOT-IMPORT.py"
    assert 'Provenance: {"source":"legacy provenance"}' in markdown
    assert "Separate Loom citation" not in markdown and "citations" not in metadata
    assert "unknown_private_key" not in markdown
    assert "[^web]:" not in markdown  # No synthetic/global footnote collisions.
    lines = (tmp_path / "evidence.md").read_text().splitlines()
    assert item["excerpt"] == "\n".join(lines[item["line_start"] - 1:item["line_end"]])
    assert markdown.endswith("### Exact excerpt\n\n" + item["excerpt"] + "\n")
    cost = item["estimated_tokens"]
    evidence_only = governed_item_token_cost(dict(item, metadata={}))
    assert cost == evidence_only + math.ceil(len(_metadata_markdown(item)) / 4)
    assert _pack(bundle, budget_tokens=cost)["included"] == 1
    short = _pack(bundle, budget_tokens=cost - 1)
    assert (short["included"], short["omitted"], short["truncated"]) == (0, 1, True)
    assert SOURCE_URL not in short["representations"]["markdown"]
    selected = _pack(bundle, budget_tokens=50000, selection_exact=item["excerpt"])
    with pytest.raises(RetrievalContractError, match="exact selection") as exc:
        _pack(bundle, budget_tokens=selected["budget"]["estimated_tokens"] - 1,
              selection_exact=item["excerpt"])
    assert exc.value.code == "budget_too_small"
    portable = _portable(bundle)
    assert portable["pack"] == pack
    assert portable["again"] == markdown
    assert portable["exact"]["included"] == 1 and portable["short"] == short
    assert portable["selectionRejected"]
    legacy = portable["legacyPack"]
    assert legacy["items"][0]["estimated_tokens"] == evidence_only
    assert "metadata" not in legacy["items"][0]
    assert "normalized data" not in legacy["representations"]["markdown"]
    assert {path: path.read_bytes() for path in before} == before
    default = RetrievalService(bundle).default_concept_contexts(["evidence"])["evidence"]
    assert _data(default) == metadata  # Prebuilt no-JS copy uses the same projection.


@pytest.mark.parametrize("overrides, tier, origin", [
    ({"generated": {}, "verified": [], "status": "deprecated"}, "unverified", None),
    ({"generated": True, "verified": {"by": "human:fixture", "at": "2026-09-02"}}, "unverified", None),
    ({"verified": {"by": "process:fixture", "at": "2026-09-02T00:00:00Z"}}, "machine-confirmed", "generated.at"),
])
def test_present_invalid_metadata_is_data_not_authority(
    tmp_path: Path, overrides: dict, tier: str, origin: str | None,
) -> None:
    bundle = _fixture(tmp_path, overrides)
    pack = _pack(bundle, budget_tokens=50000)
    metadata = _data(pack["representations"]["markdown"])
    assert metadata["trust_tier"] == tier
    assert metadata["generated_at_source"] == origin
    if origin is None:
        assert metadata["generated_at"] is None  # No legacy fallback for present invalid generated.
    assert _portable(bundle)["pack"] == pack


@pytest.mark.parametrize("timestamp, generated_at", [
    (None, None),
    ("2026-01-01", None),
    ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00+00:00"),
])
def test_absent_families_and_legacy_timestamp_markdown_parity(
    tmp_path: Path, timestamp: str | None, generated_at: str | None,
) -> None:
    fm = {"type": "Note", "title": "Evidence"}
    if timestamp is not None:
        fm["timestamp"] = timestamp
    (tmp_path / "index.md").write_text("# Bundle\n")
    (tmp_path / "evidence.md").write_text(
        "---\n" + yaml.safe_dump(fm) + "---\n# Claim\nEvidence claim[^web].\n",
    )
    bundle = Bundle.load(tmp_path)
    pack = _pack(bundle, budget_tokens=50000)
    data = _data(pack["representations"]["markdown"])
    assert data == {"generated_at": generated_at,
                    "generated_at_source": "timestamp" if generated_at else None,
                    "status": "stable", "trust_tier": "unverified"}
    assert "sources" not in data and "generated" not in data
    assert _portable(bundle)["pack"] == pack


def test_hostile_and_truncated_metadata_stays_bounded_data_in_both_runtimes(tmp_path: Path) -> None:
    hostile = "```\n# forged\n[^web]: injected\n<script>alert(1)</script>\u202e\u0085\u2028🧭"
    sources = [{"id": "web", "resource": SOURCE_URL},
               {"id": "web", "resource": "javascript:alert(1)", "title": hostile}]
    sources += [{"id": f"extra-{i}", "resource": "scope"} for i in range(10)]
    bundle = _fixture(tmp_path, {"sources": sources, "runtime": hostile,
                               "computation": "x" * 20000,
                               "executor": {"resource": hostile, "receipt": ["result"]}})
    pack = _pack(bundle, budget_tokens=50000)
    markdown = pack["representations"]["markdown"]
    data = _data(markdown)
    assert data["runtime"] == hostile
    assert data["computation"]["truncated"] and data["computation"]["omitted_codepoints"] == 18976
    assert data["sources"]["truncated"] and data["sources"]["omitted_items"] == 4
    assert data["sources"]["items"][0]["id"] == data["sources"]["items"][1]["id"] == "web"
    # Ambiguous ids remain data; no first-wins attribution or executable links.
    assert "\n[^web]:" not in markdown and "\n# forged" not in markdown
    assert "<script>" not in markdown and "\u202e" not in markdown and "\u0085" not in markdown
    assert "[web](javascript:" not in markdown
    assert "\\u0060" in markdown and "\\u003cscript\\u003e" in markdown
    assert markdown.count("```json") == 1 and markdown.count("```") == 2
    portable = _portable(bundle)
    assert portable["pack"] == pack and portable["again"] == markdown
    assert portable["short"]["included"] == 0
    # Escaping expansion and truncation markers both count, not just raw values.
    item = pack["items"][0]
    assert item["estimated_tokens"] > governed_item_token_cost(dict(item, metadata={}))


def test_source_ids_survive_reordering_without_changing_excerpt(tmp_path: Path) -> None:
    sources = [{"id": "other", "resource": "scope"}, {"id": "web", "resource": SOURCE_URL}]
    first = _pack(_fixture(tmp_path, {"sources": sources}), budget_tokens=50000)
    sources.reverse()
    second = _pack(_fixture(tmp_path, {"sources": sources}), budget_tokens=50000)
    assert first["items"][0]["excerpt"] == second["items"][0]["excerpt"]
    assert first["items"][0]["citation"] == second["items"][0]["citation"]
    for pack in (first, second):
        by_id = {s["id"]: s["resource"] for s in _data(pack["representations"]["markdown"])["sources"]}
        assert by_id["web"] == SOURCE_URL


@pytest.mark.parametrize("value", [1.0e-7, 1.0, -0.0, 1.0e20, 1.2345678901234567])
def test_float_metadata_native_builtin_static_transport_and_exact_budget(
    tmp_path: Path, value: float,
) -> None:
    _fixture(tmp_path, {"generated": {
        "by": "process:fixture", "extension": value,
        "note": 'Numbers in strings stay exact: 1.0e-7, -0.0, "1.0"',
    }})
    code = r'''
import json, sys
from okf_loom import Bundle, _yaml_compat
from okf_loom.retrieval import ContextRequest, RetrievalService
from okf_loom.render import _browser_passage_corpus, _canonical_portable_manifest
bundle = Bundle.load(sys.argv[1])
assert isinstance(bundle.concept_at("evidence").frontmatter["generated"]["extension"], float)
service = RetrievalService(bundle)
def pack(budget):
    return service.context(ContextRequest(query="Evidence", concept="evidence",
                           mode="lexical", neighbors=0, limit=1, budget_tokens=budget))
roomy = pack(50000)
cost = roomy["budget"]["estimated_tokens"]
artifact = _browser_passage_corpus(bundle, [])
artifact["canonical_manifest"] = _canonical_portable_manifest(artifact["manifest"]).decode()
print(json.dumps({"pack": roomy, "exact": pack(cost), "short": pack(cost-1),
                  "artifact": artifact, "builtin": _yaml_compat._FORCE_FALLBACK}))
'''
    outputs = []
    for builtin in ("0", "1"):
        result = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)],
            env=okf_subprocess_env(OKF_LOOM_FORCE_BUILTIN_YAML=builtin),
            capture_output=True, text=True, check=True, timeout=30,
        )
        output = json.loads(result.stdout)
        assert output.pop("builtin") is (builtin == "1")
        outputs.append(output)
        portable = _portable_node(output["artifact"], r"""
// Exercise the real static text/manifest path, not only create() on decoded JSON.
global.fetch=async function(url){
  const descriptor=artifact.manifest.chunks.find(d=>url.endsWith(d.file));
  const raw=url.endsWith('passages-manifest.json')?artifact.canonical_manifest:
      artifact.chunks[descriptor.index].canonical_chunk;
  return {ok:true,headers:{get:()=>null},text:async()=>raw};
};
const p=await window.okfPortableRetrieval.loadStatic('/__data/');
const target={concept:'evidence',query:'Evidence'};
const options={query:'Evidence',mode:'lexical',neighbors:0,limit:1,budget:50000};
const pack=p.context(target,options),cost=pack.budget.estimated_tokens;
const exact=p.context(target,Object.assign({},options,{budget:cost}));
const short=p.context(target,Object.assign({},options,{budget:cost-1}));
// A serialized/reloaded pack must retain the same copy representation too.
console.log(JSON.stringify({pack,exact,short,again:p.markdown(JSON.parse(JSON.stringify(pack)))}));
""")
        assert portable["pack"] == output["pack"]
        assert portable["exact"] == output["exact"]
        assert portable["short"] == output["short"]
        assert portable["exact"]["included"] == 1
        assert portable["short"]["included"] == 0 and portable["short"]["truncated"]
        assert portable["again"] == output["pack"]["representations"]["markdown"]
        assert RetrievalService.context_markdown(portable["pack"]) == portable["again"]
        assert governed_item_token_cost(portable["pack"]["items"][0]) == portable["pack"]["budget"]["estimated_tokens"]
        item = output["pack"]["items"][0]
        assert f'"extension":{json.dumps(value)}' in item["metadata_json"]
        assert _data(portable["again"])["generated"]["extension"] == value
    assert outputs[0] == outputs[1]


def test_canonical_metadata_validation_and_legacy_metadata_inventory(tmp_path: Path) -> None:
    artifact = _browser_passage_corpus(_fixture(tmp_path), [])
    result = _portable_node(artifact, r"""
const api=window.okfPortableRetrieval;
const validated=await api.validate(artifact.manifest,artifact.chunks);
const rejected=[];
for(const mutate of [
 c=>c.metadata_json='not json',
 c=>c.metadata_json='[]',
 c=>c.metadata_json='{}',
 c=>c.metadata_json=' '+c.metadata_json,
 c=>c.metadata_json=c.metadata_json.replace('"status":"draft"','"status":"draft","status":"draft"'),
 c=>c.metadata_json=JSON.stringify(Object.fromEntries(Object.entries(JSON.parse(c.metadata_json)).reverse())),
 c=>c.metadata_json=c.metadata_json.replace('"usage_count":0','"usage_count":1e400'),
 c=>c.metadata_json=c.metadata_json.replace('"usage_count":0','"usage_count":1'),
 c=>c.metadata_json=c.metadata_json.replace('"draft"','"\\u0064raft"'),
 c=>c.metadata_json=' '.repeat(128*1024+1),
 c=>delete c.metadata,
]){
 const manifest=JSON.parse(JSON.stringify(artifact.manifest));mutate(manifest.concepts[0]);
 try{await api.validate(manifest,artifact.chunks);rejected.push(false);}catch(e){rejected.push(true);}
}
const legacy=JSON.parse(JSON.stringify(artifact.manifest));
legacy.concepts.forEach(c=>delete c.metadata_json);
await api.validate(legacy,artifact.chunks);
const provider=api.create(legacy,validated.passages,validated.edges);
const pack=provider.context({concept:'evidence',query:'Evidence'},
 {query:'Evidence',mode:'lexical',neighbors:0,limit:1,budget:50000});
console.log(JSON.stringify({rejected,pack}));
""")
    assert result["rejected"] == [True] * 11
    item = result["pack"]["items"][0]
    assert "metadata_json" not in item
    assert _data(result["pack"]["representations"]["markdown"]) == item["metadata"]
