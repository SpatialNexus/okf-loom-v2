"""Public read contracts for OKF 0.2; fixtures never touch real bundles."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from okf_loom.model import Bundle
from okf_loom.search import search_bundle
from okf_loom.retrieval import ContextRequest, RetrievalService, build_passage_corpus, load_passage_corpus
from okf_loom.validate import CheckSpec, validate_bundle, validate_bundle_with_profile
from conftest import okf_subprocess_env


def load(tmp_path, frontmatter, body="Revenue evidence."):
    (tmp_path / "concept.md").write_text(
        f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8"
    )
    return Bundle.load(tmp_path)


def test_normalized_generation_and_verification_reach_search(tmp_path):
    bundle = load(tmp_path, """type: Unknown producer type
generated: {by: agent/v2, at: 2026-06-20T22:53:05Z}
verified: {by: human:reviewer, at: 2026-06-25T09:00:00Z}
stale_after: 2026-09-23T00:00:00Z
custom: {keep: untouched}""")
    concept = bundle.concept_at("concept")
    metadata = concept.metadata
    assert metadata["generated"]["at"] == "2026-06-20T22:53:05+00:00"
    assert metadata["verified"] == [{"by": "human:reviewer", "at": "2026-06-25T09:00:00+00:00"}]
    assert metadata["trust_tier"] == "human-reviewed"
    assert metadata["status"] == "stable"
    assert concept.is_stale(datetime(2026, 9, 23, tzinfo=timezone.utc)) is True
    assert concept.frontmatter["custom"] == {"keep": "untouched"}
    assert search_bundle(bundle, "Revenue")[0].as_dict()["metadata"] == metadata
    assert validate_bundle(bundle).ok


@pytest.mark.parametrize("value", [
    "2026-02-30T12:00:00Z", "2026-13-01T12:00:00Z",
    "2026-06-20T25:00:00Z", "2026-06-20T12:00:00+24:00",
    "2026-06-20T12:00:00+01:60", "2026-06-20T12:00:00+00:99",
    "2026-06-20", "2026-06-20T12:00:00", "not-a-date",
])
@pytest.mark.parametrize("quoted", [False, True])
def test_invalid_generation_is_soft_and_never_uses_legacy_timestamp(tmp_path, value, quoted):
    scalar = f'"{value}"' if quoted else value
    bundle = load(tmp_path, f"""type: Note
timestamp: 2020-01-01T00:00:00Z
generated: {{by: agent/v2, at: {scalar}}}""")
    concept = bundle.concept_at("concept")
    assert concept is not None
    assert concept.generated_at is None
    report = validate_bundle(bundle)
    assert report.ok
    assert any(f.code == "concept.bad_timestamp" and f.detail["field"] == "generated.at"
               for f in report.warnings)


@pytest.mark.parametrize("generated", ["null", "true", "{}", "[]", "{at: 2026-01-01T00:00:00Z}"])
def test_present_generation_requires_by_without_falling_back(tmp_path, generated):
    bundle = load(tmp_path, f"type: Note\ntimestamp: 2020-01-01T00:00:00Z\ngenerated: {generated}")
    concept = bundle.concept_at("concept")
    assert concept.generated_at != "2020-01-01T00:00:00+00:00"
    report = validate_bundle(bundle)
    assert report.ok
    assert "concept.bad_generated" in {f.code for f in report.warnings}


# F7: the recency ordering key projects the SAME ownership rules as
# normalize_metadata (generated.at with nonempty by wins; legacy timestamp
# only when generated is absent; invalid/empty never becomes an instant) and
# canonicalizes to UTC so mixed authored offsets compare as real instants.
def test_instant_key_orders_real_instants_across_offsets():
    from okf_loom.metadata import instant_key
    assert instant_key("2026-09-02T00:00:00+00:00") == "2026-09-02T00:00:00+00:00"
    # 2026-09-01T23:00-05:00 IS later than 2026-09-02T00:00+00:00 even though
    # the authored strings sort the other way.
    assert instant_key("2026-09-01T23:00:00-05:00") == "2026-09-02T04:00:00+00:00"
    assert instant_key("2026-09-02T00:00:00+00:00") < instant_key("2026-09-01T23:00:00-05:00")
    # Equivalent instants in different authored offsets share one key.
    assert instant_key("2026-09-01T12:00:00+02:00") == instant_key("2026-09-01T10:00:00Z")
    for bad in (None, "", "not-a-date", "2026-06-20T12:00:00", "2026-06-20"):
        assert instant_key(bad) == ""


def test_effective_instant_key_follows_generation_ownership():
    from okf_loom.metadata import effective_instant_key
    # Generation-only: no legacy key to fall back to.
    assert effective_instant_key(
        {"generated": {"by": "agent/v2", "at": "2026-09-01T12:00:00+02:00"}},
    ) == "2026-09-01T10:00:00+00:00"
    # Conflicting legacy: generated wins.
    assert effective_instant_key({
        "generated": {"by": "agent/v2", "at": "2026-09-01T10:00:00Z"},
        "timestamp": "2020-01-01T00:00:00Z",
    }) == "2026-09-01T10:00:00+00:00"
    # Legacy fallback applies only when generated is absent.
    assert effective_instant_key({"timestamp": "2020-01-01T00:00:00Z"}) == \
        "2020-01-01T00:00:00+00:00"
    # Empty by / invalid at / malformed mapping: unknown, never legacy.
    for generated in (
        {"by": "", "at": "2026-09-01T10:00:00Z"},
        {"by": "agent/v2", "at": "not-a-date"},
        {"by": "   ", "at": "2026-09-01T10:00:00Z"},
    ):
        assert effective_instant_key(
            {"generated": generated, "timestamp": "2020-01-01T00:00:00Z"},
        ) == "", generated
    assert effective_instant_key({}) == ""
    assert effective_instant_key({"generated": {"by": "agent/v2"}}) == ""


def test_sources_link_graph_and_context_without_conflating_citations(tmp_path):
    (tmp_path / "policy.md").write_text("---\ntype: Policy\n---\n\nPolicy evidence.\n")
    bundle = load(tmp_path, """type: Metric
generated: {by: agent/v2, at: 2026-06-20T22:53:05Z}
sources:
  - id: policy
    resource: /policy.md
    author: human:owner
    usage_count: 100
    last_modified: 2026-05-01T00:00:00Z
  - id: scope
    resource: all queries in BigQuery project X
    usage_count: 7
    usage_window: {from: 2026-05-01T00:00:00Z, to: 2026-05-02T00:00:00Z}
  - id: external
    resource: https://example.test/evidence.md
  - id: pending
    resource: /missing.md
usage_window: {from: 2026-06-01T00:00:00Z, to: 2026-06-30T00:00:00Z}
citations: [{id: loom, text: Legacy loom attribution}]
provenance: [{source: old-system}]""", "Revenue fact.[^policy]\n\n[^policy]: Policy")
    graph = bundle.graph()
    edge = next(e for e in graph.edges if e.target == ("policy",))
    assert edge.origin == "sources" and edge.source_id == "policy"
    assert edge.label == "derived_from" and edge.line == 0
    assert len(graph.unresolved) == 1
    assert graph.unresolved[0].target == ("missing",)
    findings = validate_bundle(bundle)
    assert findings.ok
    assert [f.detail["target_raw"] for f in findings.warnings if f.code == "link.broken"] == ["/missing.md"]
    sources = bundle.concept_at("concept").metadata["sources"]
    assert sources[0]["usage_window"]["from"] == "2026-06-01T00:00:00+00:00"
    assert sources[1]["usage_window"]["from"] == "2026-05-01T00:00:00+00:00"
    hits = search_bundle(bundle, "", mode="relation", relation="derived_from", target="policy")
    assert hits[0].as_dict()["detail"]["edges"][0]["via"] == "sources"
    service = RetrievalService(bundle)
    pack = service.context(ContextRequest(concept="concept", query="Policy", neighbors=1))
    item = next(item for item in pack["items"] if item["concept_id"] == "concept")
    assert item["metadata"]["sources"] == sources
    assert item["freshness"] == "2026-06-20T22:53:05+00:00"
    assert "policy" in {item["concept_id"] for item in pack["items"]}
    assert item["citation"].startswith("[concept.md#L")
    passage = next(p for p in service.passages if p.concept_id == ("concept",)).public_dict()
    assert passage["citations"] == [{"id": "loom", "text": "Legacy loom attribution"}]
    assert passage["provenance"] == [{"source": "old-system"}]
    assert passage["metadata"]["sources"] == sources
    lines = bundle.concept_at("concept").raw_text.splitlines()
    assert passage["excerpt"] == "\n".join(lines[passage["line_start"] - 1:passage["line_end"]])
    assert load_passage_corpus(build_passage_corpus(bundle))


def test_absent_families_legacy_fallback_and_root_marker_ownership(tmp_path):
    (tmp_path / "index.md").write_text("---\ngenerated: true\n---\n# Index\n")
    bundle = load(tmp_path, "type: Attested Computation")
    c = bundle.concept_at("concept")
    assert c.metadata == {"generated_at": None, "generated_at_source": None,
                          "trust_tier": "unverified", "status": "stable"}
    assert c.is_stale(datetime.now(timezone.utc)) is None
    assert validate_bundle(bundle).ok
    assert "concept.missing_runtime" in {f.code for f in validate_bundle(bundle).warnings}
    assert bundle.root_index().is_generated is True
    assert not any(f.code == "concept.bad_generated" for f in validate_bundle(bundle).findings)
    bundle = load(tmp_path, "type: Note\ntimestamp: 2020-01-01T00:00:00Z")
    assert bundle.concept_at("concept").metadata["generated_at"] == "2020-01-01T00:00:00+00:00"
    assert bundle.concept_at("concept").metadata["generated_at_source"] == "timestamp"
    (tmp_path / "index.md").write_text("---\ngenerated: {by: human:writer}\n---\n# Authored\n")
    assert Bundle.load(tmp_path).root_index().is_generated is False


def test_generation_by_alone_is_valid_and_never_reads_legacy_timestamp(tmp_path):
    bundle = load(tmp_path, "type: Note\ngenerated: {by: human:writer}\ntimestamp: invalid")
    assert bundle.concept_at("concept").generated_at is None
    assert not any(f.code in ("concept.bad_timestamp", "concept.bad_generated")
                   for f in validate_bundle(bundle).findings)


def test_whitespace_type_is_not_a_conformant_unknown_type(tmp_path):
    assert not validate_bundle(load(tmp_path, "type: ' '")).ok


def test_metadata_findings_use_registered_soft_rules_and_selectable_checks(tmp_path):
    bundle = load(tmp_path, "type: Note\ngenerated: {at: invalid}")
    shapes = validate_bundle(bundle, checks=CheckSpec.only("concept.bad_generated"))
    assert {f.code for f in shapes.findings} == {"concept.bad_generated"}
    times = validate_bundle(bundle, checks=CheckSpec.only("concept.bad_timestamp"))
    assert {f.code for f in times.findings} == {"concept.bad_timestamp"}
    assert validate_bundle_with_profile(bundle).ok
    assert not validate_bundle_with_profile(bundle, strict=True).ok  # existing opt-in policy


def test_normalized_metadata_is_detached_and_refreshes_with_source(tmp_path):
    bundle = load(tmp_path, "type: Note\nverified: {by: human:reviewer, at: 2026-06-01T00:00:00Z}")
    concept = bundle.concept_at("concept")
    detached = concept.metadata
    detached["verified"][0]["by"] = "process:edited"
    assert concept.metadata["trust_tier"] == "human-reviewed"
    assert search_bundle(bundle, "Revenue")[0].metadata["trust_tier"] == "human-reviewed"
    service = RetrievalService(bundle)
    before_revision = service.revision
    assert service.passages[0].metadata["trust_tier"] == "human-reviewed"
    concept.raw_text = "---\ntype: Note\n---\n\nRevenue evidence.\n"
    bundle.invalidate()
    assert concept.metadata["trust_tier"] == "unverified"
    assert "verified" not in concept.metadata
    assert search_bundle(bundle, "Revenue")[0].metadata["trust_tier"] == "unverified"
    assert service.passages[0].metadata["trust_tier"] == "unverified"
    assert service.revision != before_revision


def test_native_cyclic_optional_metadata_stays_bounded_and_does_not_gain_trust(tmp_path):
    bundle = load(tmp_path, "type: Note")
    cyclic = {"by": "human:reviewer"}
    cyclic["at"] = cyclic
    concept = bundle.concept_at("concept")
    concept.frontmatter["verified"] = cyclic
    metadata = concept.metadata
    assert metadata["trust_tier"] == "unverified"
    assert "cyclic_metadata" in json.dumps(metadata)
    assert len(json.dumps(metadata)) < 1000


@pytest.mark.parametrize(("verified", "tier"), [
    ("[]", "unverified"), ("null", "unverified"),
    ("{by: process:nightly, at: 2026-06-01T00:00:00Z}", "machine-confirmed"),
    ("[{by: agent/v1, at: 2026-06-01T00:00:00Z}, {by: human:reviewer, at: 2026-06-02T00:00:00Z}]", "human-reviewed"),
    ("{by: human:reviewer, at: invalid}", "unverified"),
    ("{by: ' ', at: 2026-06-01T00:00:00Z}", "unverified"),
    ("{by: human:reviewer}", "unverified"),
])
def test_trust_tiers_are_advisory_and_require_well_formed_events(tmp_path, verified, tier):
    bundle = load(tmp_path, f"type: Note\nverified: {verified}")
    assert bundle.concept_at("concept").metadata["trust_tier"] == tier
    assert validate_bundle(bundle).ok
    assert search_bundle(bundle, "Revenue")  # no trust-based consumption gate


@pytest.mark.parametrize("status", ["draft", "stable", "deprecated"])
def test_lifecycle_retains_all_states_and_stale_boundary_uses_instant(tmp_path, status):
    bundle = load(tmp_path, f"type: Note\nstatus: {status}\nstale_after: 2026-09-23T02:00:00+02:00")
    concept = bundle.concept_at("concept")
    assert concept.metadata["status"] == status
    assert concept.is_stale(datetime(2026, 9, 22, 23, 59, 59, tzinfo=timezone.utc)) is False
    assert concept.is_stale(datetime(2026, 9, 23, tzinfo=timezone.utc)) is True
    assert concept.is_stale(datetime(2026, 9, 24, tzinfo=timezone.utc)) is True
    with pytest.raises(ValueError, match="timezone-aware"):
        concept.is_stale(datetime(2026, 9, 23))
    assert search_bundle(bundle, "Revenue")  # no lifecycle-based hiding


@pytest.mark.parametrize(("frontmatter", "code"), [
    ("generated: []", "concept.bad_generated"),
    ("verified: true", "concept.bad_verified"),
    ("verified: [null]", "concept.bad_verified"),
    ("status: []", "concept.bad_status"),
    ("status: .nan", "concept.bad_status"),
    ("sources: {}", "concept.bad_sources"),
    ("sources: [{id: source}]", "concept.bad_sources"),
    ("sources: [{resource: ' '}]", "concept.bad_sources"),
    ("sources: [{resource: scope, usage_count: true}]", "concept.bad_sources"),
    ("sources: [{resource: scope, usage_count: -1}]", "concept.bad_sources"),
    ("sources: [{resource: scope, id: a}, {resource: other, id: a}]", "concept.bad_sources"),
    ("usage_window: null", "concept.bad_usage_window"),
    ("usage_window: {from: 2026-06-02T00:00:00Z, to: 2026-06-01T00:00:00Z}", "concept.bad_usage_window"),
    ("stale_after: 2026-02-30T00:00:00Z", "concept.bad_timestamp"),
    ("parameters: [{name: year, type: integer, required: 'true'}]", "concept.bad_computation"),
    ("executor: {resource: https://example.test/code, receipt: {run_id: string}}", "concept.bad_computation"),
    ("attester: []", "concept.bad_computation"),
])
def test_malformed_optional_families_warn_without_rejecting_concepts(tmp_path, frontmatter, code):
    bundle = load(tmp_path, "type: Unregistered\n" + frontmatter)
    report = validate_bundle(bundle)
    assert report.ok
    assert code in {f.code for f in report.warnings}
    assert bundle.concept_at("concept").type == "Unregistered"
    json.dumps(bundle.concept_at("concept").metadata, allow_nan=False)


def test_source_relative_paths_stable_ids_and_override_presence(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "policy.md").write_text("---\ntype: Policy\n---\nPolicy\n")
    raw = """---
type: Metric
sources:
  - {id: relative, resource: ../policy.md}
  - {id: extensionless, resource: /policy}
  - {id: scope, resource: 'all queries in project X', usage_window: null}
usage_window: {from: 2026-06-01T00:00:00Z, to: 2026-06-30T00:00:00Z}
---
Revenue
"""
    path = tmp_path / "nested" / "metric.md"
    path.write_text(raw)
    bundle = Bundle.load(tmp_path)
    edges = bundle.graph().edges
    assert {(edge.source_id, edge.target) for edge in edges} == {
        ("relative", ("policy",)), ("extensionless", ("policy",)),
    }
    assert bundle.concept_at("nested/metric").metadata["sources"][2]["usage_window"] is None
    before = {edge.source_id: edge.target for edge in edges}
    path.write_text(raw.replace(
        "  - {id: relative, resource: ../policy.md}\n  - {id: extensionless, resource: /policy}",
        "  - {id: extensionless, resource: /policy}\n  - {id: relative, resource: ../policy.md}",
    ))
    assert {edge.source_id: edge.target for edge in bundle.reload().graph().edges} == before


def test_computation_is_metadata_only_and_unknowns_round_trip(tmp_path, monkeypatch):
    import socket
    from okf_loom.parse import parse_document
    from okf_loom.roundtrip import serialize_document_round_trip

    (tmp_path / "untrusted.py").write_text("raise AssertionError('must never run')\n")
    source = """type: Attested Computation
runtime: unknown-runtime
parameters: [{name: year, type: future-type, required: true, custom: retained}]
computation: untrusted.py
executor: {resource: https://example.test/code.py, receipt: [job_id, result], custom: retained}
attester: {resource: untrusted.py}
generated: {by: agent/v2}
sources: [{id: source, resource: all queries, custom: retained}]
unknown_family: {opaque: [one, two]}"""
    original_read = Path.read_text

    def guarded_read(path, *args, **kwargs):
        assert path.name != "untrusted.py", "metadata consumer opened computation code"
        return original_read(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("metadata consumer attempted network or execution")

    monkeypatch.setattr(Path, "read_text", guarded_read)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    bundle = load(tmp_path, source, "# Computation\n\n```python\nraise AssertionError('never run')\n```")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert validate_bundle(bundle).ok
    concept = bundle.concept_at("concept")
    assert concept.metadata["executor"]["receipt"] == ["job_id", "result"]
    hit = search_bundle(bundle, "", mode="tag")[0].as_dict()
    assert hit["metadata"]["parameters"][0]["custom"] == "retained"
    assert RetrievalService(bundle).context(ContextRequest(concept="concept"))["items"][0]["metadata"]["runtime"] == "unknown-runtime"
    json.dumps(hit, allow_nan=False)
    serialized = serialize_document_round_trip(concept.raw_text, concept.frontmatter, concept.body)
    fm, _ = parse_document(serialized)
    assert fm["unknown_family"] == {"opaque": ["one", "two"]}
    assert fm["sources"][0]["custom"] == "retained"
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}


def test_native_datetime_and_builtin_yaml_have_equal_public_semantics(tmp_path):
    source = """type: Attested Computation
runtime: python
generated: {by: agent/v2, at: 2026-06-20T22:53:05Z}
verified: {by: human:reviewer, at: 2026-06-21T12:00:00+02:30}
stale_after: 2026-09-23T00:00:00Z
sources:
  - id: scope
    resource: all queries in BigQuery project X
    author: team:analytics
    usage_count: 30
    last_modified: 2026-02-30T00:00:00Z
usage_window: {from: 2026-06-01T00:00:00Z, to: 2026-06-30T00:00:00Z}
parameters: [{name: year, type: integer, required: true}]
executor: {resource: references/run.py, receipt: [run_id]}
attester: {resource: references/check.py}"""
    load(tmp_path, source)
    code = """
import json, sys
from datetime import datetime, timezone
from okf_loom.model import Bundle
from okf_loom.validate import validate_bundle
from okf_loom.retrieval import RetrievalService, ContextRequest
b = Bundle.load(sys.argv[1]); c = b.concept_at('concept')
print(json.dumps({'metadata': c.metadata, 'stale': c.is_stale(datetime(2026, 9, 23, tzinfo=timezone.utc)),
 'findings': validate_bundle(b).as_dict(),
 'context': RetrievalService(b).context(ContextRequest(concept='concept'))}, sort_keys=True))
"""
    outputs = []
    for fallback in ("0", "1"):
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)],
            env=okf_subprocess_env(OKF_LOOM_FORCE_BUILTIN_YAML=fallback),
            text=True, capture_output=True, check=True)
        outputs.append(json.loads(result.stdout))
    assert outputs[0] == outputs[1]
    # Native Python datetime values are also accepted at the public model seam.
    c = Bundle.load(tmp_path).concept_at("concept")
    c.frontmatter["generated"]["at"] = datetime(2026, 6, 20, 22, 53, 5, tzinfo=timezone.utc)
    assert c.generated_at == outputs[0]["metadata"]["generated_at"]
    c.frontmatter["generated"]["at"] = datetime(2026, 6, 20, 22, 53, 5)
    assert c.generated_at is None


def test_cli_json_consumes_optional_metadata_without_version_relabeling(tmp_path):
    load(tmp_path, """type: Attested Computation
generated: {by: human:writer, at: 2026-06-01T00:00:00Z}
verified: {by: process:nightly, at: 2026-06-02T00:00:00Z}
runtime: custom-runtime
parameters: [{name: input, type: string, required: true}]
executor: {resource: https://example.test/run.py, receipt: [result]}
attester: {resource: https://example.test/check.py}""")
    helper = Path(__file__).resolve().parents[1] / "scripts" / "okf-loom"
    for arguments in (
        ["validate", str(tmp_path), "--format", "json"],
        ["search", str(tmp_path), "Revenue", "--format", "json"],
        ["context", str(tmp_path), "--concept", "concept", "--format", "json"],
    ):
        result = subprocess.run([str(helper), *arguments], capture_output=True,
                                text=True, check=True)
        value = json.loads(result.stdout)
        if arguments[0] == "validate":
            assert value["ok"] is True
        else:
            row = value[0] if isinstance(value, list) else value["items"][0]
            assert row["metadata"]["runtime"] == "custom-runtime"
            assert row["metadata"]["trust_tier"] == "machine-confirmed"
