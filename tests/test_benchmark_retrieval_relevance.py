"""Focused proofs for the deterministic retrieval-relevance benchmark."""
from __future__ import annotations

import importlib.util
import copy
import json
import math
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from conftest import TOOLKIT_ROOT


SCRIPT = TOOLKIT_ROOT / "scripts" / "benchmark_retrieval_relevance.py"


def _module():
    spec = importlib.util.spec_from_file_location("benchmark_retrieval_relevance", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def recorded_evidence(tmp_path_factory):
    """Exercise the real report verifier with current code and local history only.

    This is synthetic before/after evidence, not a refresh of the historical
    experiment. Only the experiment's selected baseline revision is substituted;
    both runtimes, source hashing, reconstruction and scoring remain real.
    """
    benchmark = _module()
    checkout = tmp_path_factory.mktemp("relevance-source")
    (checkout / ".gitignore").write_text("__pycache__/\n")
    for relative in benchmark.measured_source_paths(TOOLKIT_ROOT):
        destination = checkout / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TOOLKIT_ROOT / relative, destination)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Proof",
                    "-c", "user.email=proof@example.invalid", "commit", "-qm", "source"], check=True)
    benchmark.BASELINE_REVISION = benchmark._git_output(checkout, "rev-parse", "HEAD")
    assert subprocess.run(
        ["git", "-C", str(checkout), "cat-file", "-e", _module().BASELINE_REVISION],
        capture_output=True,
    ).returncode != 0  # No historical objects are available to these tests.
    before, live, portable, _bundle, context = benchmark._reconstruct_material(checkout)
    try:
        assert portable["status"] == "measured", portable
        command = ("scripts/benchmark_retrieval_relevance.py "
                   f"--baseline-checkout <clean-baseline@{benchmark.BASELINE_REVISION}> "
                   "--current-checkout <current-worktree> "
                   "--output docs/design/gold-standard-relevance-2026.json")
        report = benchmark.build_report(before, live, command=command, portable_raw=portable,
                                        provenance=benchmark._source_provenance(checkout))
    finally:
        context.cleanup()
    benchmark.verify_report(report, checkout)
    return benchmark, checkout, report


def test_fixture_contract_is_complete_and_frozen() -> None:
    benchmark = _module()

    assert benchmark.fixture_digest() == "da6f2c2691515ce65d9ea20c7470d321413d8abd9820f6879b5b43b2fba9deee"
    intents = {case["intent"] for case in benchmark.QUERY_FIXTURE}
    assert intents == {
        "lexical", "semantic", "hybrid", "tag", "entity", "relation",
        "section-disambiguation",
    }
    for case in benchmark.QUERY_FIXTURE:
        assert case["expected_concepts"]
        assert case["expected_heading"]
        assert case["citation_expectation"]["required"] is True
        assert case["hard_negatives"]
        assert not set(case["expected_concepts"]) & set(case["hard_negatives"])


def test_precision_metrics_and_unavailable_evidence_are_explicit() -> None:
    benchmark = _module()
    case = {
        "id": "metric-proof",
        "expected_concepts": ["good/a", "good/b"],
        "expected_heading": "Proof",
        "citation_expectation": {"required": True, "path": "good/a.md"},
        "hard_negatives": ["bad/distractor"],
    }
    rows = [
        {"concept_id": "good/a", "heading": "Proof", "citation": "[good/a.md#L4-L6]",
         "citation_valid": True},
        {"concept_id": "bad/distractor", "heading": "Noise", "citation": "[bad/distractor.md#L4-L5]",
         "citation_valid": True},
        {"concept_id": "good/b", "heading": "Other", "citation": "[good/b.md#L4-L5]",
         "citation_valid": True},
    ]

    measured = benchmark.score_query(case, rows, k=3, evidence_available=True)
    assert measured["precision_at_k"] == 2 / 3
    assert measured["mrr"] == 1.0
    assert math.isclose(measured["ndcg_at_k"], (1 + 0.5) / (1 + 1 / math.log2(3)))
    assert measured["expected_section_hit"] == 1.0
    assert measured["citation_validity"] == 1.0
    assert measured["hard_negative_in_top_k"] is True

    unavailable = benchmark.score_query(case, rows, k=3, evidence_available=False)
    assert unavailable["expected_section_hit"] is None
    assert unavailable["citation_validity"] is None
    assert unavailable["evidence_status"] == "unavailable"


def test_report_serialization_is_byte_deterministic(tmp_path: Path) -> None:
    benchmark = _module()
    raw = {
        "revision": "fixed",
        "section_evidence_available": False,
        "queries": [
            {"id": case["id"], "results": []}
            for case in benchmark.QUERY_FIXTURE
        ],
    }
    report = benchmark.build_report(raw, raw, command="reproduce exactly")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    benchmark.write_report(first, report)
    benchmark.write_report(second, report)

    assert first.read_bytes() == second.read_bytes()
    decoded = json.loads(first.read_text(encoding="utf-8"))
    assert decoded["schema_version"] == 1
    assert decoded["command"] == "reproduce exactly"
    assert decoded["fixture"]["sha256"] == benchmark.fixture_digest()
    assert decoded["aggregate"]["before"]["expected_section_hit_rate"] is None


def test_work_directory_fails_closed_inside_checkout_or_when_nonempty(tmp_path: Path) -> None:
    benchmark = _module()

    try:
        benchmark.validate_work_dir(TOOLKIT_ROOT / "generated-benchmark", (TOOLKIT_ROOT,))
    except ValueError as exc:
        assert "outside checkout" in str(exc)
    else:
        raise AssertionError("checkout-local benchmark workspace was accepted")

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("owner data", encoding="utf-8")
    try:
        benchmark.validate_work_dir(occupied, (TOOLKIT_ROOT,))
    except ValueError as exc:
        assert "new or empty" in str(exc)
    else:
        raise AssertionError("non-empty benchmark workspace was accepted")
    assert (occupied / "keep.txt").read_text(encoding="utf-8") == "owner data"


def test_fixture_portable_live_top_result_parity_is_complete(tmp_path: Path) -> None:
    benchmark = _module()
    bundle = tmp_path / "bundle"
    fixture_file = tmp_path / "queries.json"
    benchmark.generate_fixture(bundle)
    fixture_file.write_bytes(benchmark._stable_bytes(benchmark.QUERY_FIXTURE))
    live_raw = benchmark.run_checkout(TOOLKIT_ROOT, bundle, fixture_file, worktree_label=True)
    portable_raw = benchmark.run_portable(TOOLKIT_ROOT, bundle, fixture_file, tmp_path / "portable")
    assert portable_raw.get("status") == "measured", portable_raw
    scored = benchmark._score_side(live_raw)
    parity = benchmark._portable_parity(scored, portable_raw)
    assert parity["after"] == 1.0
    assert len(parity["per_query"]) == 7
    assert all(row["match"] for row in parity["per_query"])
    assert all(row["live_order"] == row["portable_order"] for row in parity["per_query"])
    tag = next(row for row in parity["per_query"] if row["id"] == "tag-critical-path")
    assert tag["live_top"] == tag["portable_top"] == "services/checkout"
    contexts = benchmark._portable_context_parity(live_raw, portable_raw)
    assert contexts["after"] == 1.0
    assert len(contexts["per_query"]) == 7
    assert all(row["field_match"] and row["markdown_byte_match"] and row["match"]
               for row in contexts["per_query"])
    for side in (live_raw, portable_raw):
        for row in side["contexts"]:
            pack = row["pack"]
            assert pack["budget"]["estimated_tokens"] <= pack["budget"]["requested_tokens"]
            assert pack["budget"]["estimated_tokens"] == sum(
                item["estimated_tokens"] for item in pack["items"])
    report = benchmark.build_report(
        live_raw, live_raw, command="sanitized reproduction", portable_raw=portable_raw,
        provenance={"fixture_sha256": benchmark.fixture_digest()},
    )
    benchmark.enforce_thresholds(report)
    assert report["aggregate"]["portable_live_parity"]["after"] == 1.0
    assert report["aggregate"]["portable_context_parity"]["after"] == 1.0


def test_acceptance_thresholds_reject_ordered_or_metric_regression() -> None:
    benchmark = _module()
    metrics = {
        "precision_at_3": 1.0, "mrr": 1.0, "ndcg_at_3": 1.0,
        "expected_section_hit_rate": 1.0, "citation_validity": 1.0,
    }
    report = {"aggregate": {
        "before": dict(metrics), "after": dict(metrics),
        "portable_live_parity": {"after": 1.0},
        "portable_context_parity": {"after": 1.0},
    }}
    benchmark.enforce_thresholds(report)
    report["aggregate"]["after"]["precision_at_3"] = 0.5
    with pytest.raises(ValueError, match="precision_at_3 regressed"):
        benchmark.enforce_thresholds(report)


def test_provenance_manifest_binds_measured_sources_and_ignores_aic(tmp_path: Path) -> None:
    benchmark = _module()
    paths = benchmark.measured_source_paths(TOOLKIT_ROOT)
    checkout = tmp_path / "checkout"
    for relative in paths:
        destination = checkout / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TOOLKIT_ROOT / relative, destination)

    original = benchmark._source_manifest(checkout)
    assert "scripts/okf_loom/retrieval.py" in original["files"]
    assert "scripts/okf_loom/search.py" in original["files"]
    assert all(not Path(path).is_absolute() for path in original["files"])

    ignored = checkout / ".aic" / "tmp" / "unmeasured.txt"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("must not affect proof identity", encoding="utf-8")
    assert benchmark._source_manifest(checkout)["sha256"] == original["sha256"]

    retrieval = checkout / "scripts/okf_loom/retrieval.py"
    retrieval.write_bytes(retrieval.read_bytes() + b"\n# measured mutation\n")
    changed = benchmark._source_manifest(checkout)
    assert changed["sha256"] != original["sha256"]
    assert changed["files"]["scripts/okf_loom/retrieval.py"] != original["files"]["scripts/okf_loom/retrieval.py"]


def test_manifest_ignores_local_viewer_artifacts_but_binds_runtime_source(tmp_path: Path) -> None:
    benchmark = _module()
    checkout = tmp_path / "checkout"
    shutil.copytree(TOOLKIT_ROOT / "scripts", checkout / "scripts")
    original = benchmark._source_manifest(checkout)

    generated = checkout / "scripts/okf_loom/viewer/static/coverage.json"
    generated.write_text('{"local": true}\n', encoding="utf-8")
    assert benchmark._source_manifest(checkout) == original

    runtime = checkout / "scripts/okf_loom/viewer/static/portable-retrieval.js"
    runtime.write_bytes(runtime.read_bytes() + b"\n// measured mutation\n")
    changed = benchmark._source_manifest(checkout)
    assert changed["sha256"] != original["sha256"]
    assert changed["files"][runtime.relative_to(checkout).as_posix()] != original["files"][runtime.relative_to(checkout).as_posix()]


def test_current_report_has_sanitized_complete_provenance_and_enforced_results(recorded_evidence) -> None:
    benchmark, checkout, report = recorded_evidence
    provenance = report["provenance"]

    assert provenance["source_manifest"]["paths"] == list(benchmark.measured_source_paths(checkout))
    assert "scripts/okf_loom/retrieval.py" in provenance["source_manifest"]["files"]
    assert "scripts/okf_loom/search.py" in provenance["source_manifest"]["files"]
    assert str(TOOLKIT_ROOT) not in json.dumps(report, sort_keys=True)
    assert report["aggregate"]["after"]["precision_at_3"] == pytest.approx(0.38095238095238093)
    for metric in ("mrr", "ndcg_at_3", "expected_section_hit_rate", "citation_validity"):
        assert report["aggregate"]["after"][metric] == 1.0
    assert report["aggregate"]["portable_live_parity"]["after"] == 1.0
    assert report["aggregate"]["portable_context_parity"]["after"] == 1.0
    assert len(report["aggregate"]["portable_live_parity"]["per_query"]) == 7
    assert len(report["aggregate"]["portable_context_parity"]["per_query"]) == 7
    benchmark.verify_report(report, checkout)


def test_manifest_uses_selected_checkout_and_complete_source_closure(tmp_path: Path) -> None:
    benchmark = _module()
    alternate = tmp_path / "alternate"
    shutil.copytree(TOOLKIT_ROOT / "scripts", alternate / "scripts")
    extra = alternate / "scripts/okf_loom/alternate_only.py"
    extra.write_text("SELECTED_CHECKOUT = True\n", encoding="utf-8")

    paths = benchmark.measured_source_paths(alternate)
    assert "scripts/okf_loom/alternate_only.py" in paths
    for required in ("scripts/okf_loom/_yaml_compat.py", "scripts/okf_loom/config.py",
                     "scripts/okf_loom/ignore.py"):
        assert required in paths
    manifest = benchmark._source_manifest(alternate)
    assert str(TOOLKIT_ROOT) not in json.dumps(manifest, sort_keys=True)
    assert manifest["paths"] == list(paths)


@pytest.mark.parametrize("tamper", [
    "file-hash", "aggregate-digest", "harness", "fixture", "per-query-metric",
    "search-parity-total", "context-parity-total", "extra-source", "missing-source",
])
def test_report_verifier_rejects_tampering(tamper: str, recorded_evidence) -> None:
    benchmark, checkout, source = recorded_evidence
    report = copy.deepcopy(source)
    manifest = report["provenance"]["source_manifest"]
    if tamper == "file-hash":
        manifest["files"]["scripts/okf_loom/retrieval.py"] = "0" * 64
    elif tamper == "aggregate-digest":
        manifest["sha256"] = "0" * 64
    elif tamper == "harness":
        report["provenance"]["harness_sha256"] = "0" * 64
    elif tamper == "fixture":
        report["provenance"]["fixture_sha256"] = "0" * 64
    elif tamper == "per-query-metric":
        report["per_query"]["after"][0]["mrr"] = 0.5
    elif tamper == "search-parity-total":
        report["aggregate"]["portable_live_parity"]["after"] = 0.5
    elif tamper == "context-parity-total":
        report["aggregate"]["portable_context_parity"]["after"] = 0.5
    elif tamper == "extra-source":
        manifest["paths"].append("scripts/okf_loom/not-measured.py")
        manifest["files"]["scripts/okf_loom/not-measured.py"] = "0" * 64
    elif tamper == "missing-source":
        path = "scripts/okf_loom/search.py"
        manifest["paths"].remove(path)
        manifest["files"].pop(path)
    with pytest.raises(ValueError, match="report verification failed"):
        benchmark.verify_report(report, checkout)


def test_verifier_reconstructs_parity_instead_of_trusting_coherent_flags(recorded_evidence) -> None:
    benchmark, checkout, source = recorded_evidence

    order = copy.deepcopy(source)
    row = order["aggregate"]["portable_live_parity"]["per_query"][0]
    row["live_order"] = row["portable_order"] = list(reversed(row["live_order"]))
    row["live_top"] = row["portable_top"] = row["live_order"][0]
    row["contract_match"] = row["match"] = True
    with pytest.raises(ValueError, match="report verification failed"):
        benchmark.verify_report(order, checkout)

    context = copy.deepcopy(source)
    row = context["aggregate"]["portable_context_parity"]["per_query"][0]
    row["live_item_ids"] = row["portable_item_ids"] = ["coherent:tamper"]
    row["field_match"] = row["markdown_byte_match"] = row["match"] = True
    with pytest.raises(ValueError, match="report verification failed"):
        benchmark.verify_report(context, checkout)

    markdown = copy.deepcopy(source)
    markdown["verification_material"]["portable"]["contexts"][0]["pack"]["representations"]["markdown"] += "\ntamper"
    with pytest.raises(ValueError, match="report verification failed"):
        benchmark.verify_report(markdown, checkout)


def test_verifier_reconstructs_pinned_baseline_and_rejects_coherent_tamper(recorded_evidence) -> None:
    benchmark, checkout, report = recorded_evidence
    report = copy.deepcopy(report)
    material_row = report["verification_material"]["baseline"]["queries"][0]["results"][0]
    scored_row = report["per_query"]["before"][0]["retrieved"][0]
    material_row["concept_id"] = scored_row["concept_id"] = "coherent/baseline-tamper"
    report["per_query"]["before"][0]["mrr"] = 0.0
    report["aggregate"]["before"]["mrr"] = sum(
        row["mrr"] for row in report["per_query"]["before"]
    ) / len(report["per_query"]["before"])
    with pytest.raises(ValueError, match="report verification failed"):
        benchmark.verify_report(report, checkout)


def test_release_identity_survives_clean_commit_and_unmeasured_metadata(tmp_path: Path, recorded_evidence) -> None:
    benchmark, source, report = recorded_evidence
    checkout = tmp_path / "release-checkout"
    subprocess.run(["git", "clone", "--quiet", str(source), str(checkout)], check=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "Proof", "GIT_AUTHOR_EMAIL": "proof@example.invalid",
           "GIT_COMMITTER_NAME": "Proof", "GIT_COMMITTER_EMAIL": "proof@example.invalid"}
    release_manifest = benchmark._source_manifest(checkout)
    subprocess.run(["git", "-C", str(checkout), "commit", "--quiet", "--allow-empty",
                    "-m", "release bytes"], check=True, env=env)
    assert subprocess.run(["git", "-C", str(checkout), "status", "--porcelain"], check=True,
                          text=True, stdout=subprocess.PIPE).stdout == ""
    assert benchmark._source_manifest(checkout) == release_manifest

    subprocess.run(["git", "-C", str(checkout), "commit", "--quiet", "--allow-empty",
                    "-m", "unmeasured metadata"], check=True, env=env)
    assert benchmark._source_manifest(checkout) == release_manifest

    (checkout / "notes.txt").write_text("unrelated dirty workspace\n")
    benchmark.verify_report(report, checkout)
    retrieval = checkout / "scripts/okf_loom/retrieval.py"
    retrieval.write_bytes(retrieval.read_bytes() + b"\n# measured drift\n")
    assert benchmark._source_manifest(checkout) != release_manifest
    with pytest.raises(ValueError, match="source manifest mismatch"):
        benchmark.verify_report(report, checkout)


@pytest.mark.parametrize("field,value", [
    ("path", "../escape.md"), ("line_end", 9999), ("char_start", -1),
    ("excerpt", "forged excerpt"), ("citation_valid", True),
])
def test_verifier_reconstructs_citations_from_fixture_bytes(field: str, value: object, recorded_evidence) -> None:
    benchmark, checkout, report = recorded_evidence
    report = copy.deepcopy(report)
    material_row = report["verification_material"]["live_after"]["queries"][0]["results"][0]
    scored_row = report["per_query"]["after"][0]["retrieved"][0]
    original = material_row.get(field)
    material_row[field] = value
    if field in scored_row:
        scored_row[field] = value
    if field != "citation_valid":
        material_row["citation_valid"] = scored_row["citation_valid"] = True
    else:
        material_row[field] = scored_row[field] = not bool(original)
    with pytest.raises(ValueError, match="report verification failed"):
        benchmark.verify_report(report, checkout)
