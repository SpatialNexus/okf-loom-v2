"""Focused contract tests for the gold-standard benchmark harness."""
from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import TOOLKIT_ROOT


SCRIPT = TOOLKIT_ROOT / "scripts" / "benchmark_gold_standard.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("benchmark_gold_standard", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(work_dir: Path, output: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--work-dir",
            str(work_dir),
            "--output",
            str(output),
            *extra,
        ],
        cwd=TOOLKIT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_tiny_end_to_end_report_has_versioned_metrics(tmp_path: Path) -> None:
    work_dir = tmp_path / "benchmark-work"
    output = work_dir / "report.json"

    result = _run(
        work_dir, output,
        "--sizes", "3", "--repeats", "1", "--static-build",
        "--skip-static-above", "3",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["units"] == {"bytes": "bytes", "latency": "milliseconds"}
    assert len(report["results"]) == 1
    size_result = report["results"][0]
    assert size_result["size"] == 3
    assert size_result["generated_bundle"]["concepts"] == 3
    assert size_result["generated_bundle"]["bytes"] > 0
    assert size_result["generated_bundle"]["edge_topology"] == {
        "kind": "directed_ring",
        "markdown_edges": 3,
        "typed_relation_edges": 3,
        "raw_edges": 6,
        "unique_source_target_pairs": 3,
        "outgoing_edges_per_concept": 2,
    }
    assert set(size_result["search"]) == {
        "lexical", "semantic", "hybrid", "tag", "entity", "relation"
    }
    for metric in size_result["search"].values():
        for cache_state in ("cold", "warm"):
            measured = metric[cache_state]
            assert set(measured) == {
                "hit_count", "median_ms", "p95_ms", "samples_ms"
            }
            assert measured["median_ms"] >= 0
            assert len(measured["samples_ms"]) == 1
    incremental = size_result["incremental_one_file_reload"]
    assert incremental["loaded_concepts"] == 3
    assert "full synchronous Bundle.load" in incremental["semantics"]
    assert "does not measure watcher latency" in incremental["semantics"]
    assert size_result["graph"]["nodes"] == 3
    assert size_result["graph"]["serialized_payload_bytes"] > 0
    assert size_result["static_build"]["status"] == "measured"
    assert size_result["static_build"]["file_count"] > 0
    assert size_result["static_build"]["output_bytes"] > 0
    assert size_result["browser_graph_mount"]["latency_ms"] is None
    assert "not requested" in size_result["browser_graph_mount"]["unavailable_reason"]
    assert size_result["browser_graph_mount"]["phase"] == "canvas_attachment"
    assert size_result["browser_graph_mount"]["semantic_ready"] is False
    assert report["environment"]["repository_revision"]
    assert report["harness_options"]["sizes"] == [3]
    assert output.read_text(encoding="utf-8") == (
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )


def test_parser_defaults_are_safe() -> None:
    harness = _load_harness()
    args = harness.build_parser().parse_args(["--work-dir", "/tmp/work", "--output", "/tmp/out"])

    assert args.sizes == (100, 1000)
    assert args.large_scales is False
    assert args.static_build is False


def test_large_scales_require_explicit_opt_in_before_running(monkeypatch, capsys) -> None:
    harness = _load_harness()
    called = []
    monkeypatch.setattr(harness, "run_benchmark", lambda args: called.append(args))

    with pytest.raises(SystemExit) as rejected:
        harness.main(["--work-dir", "/tmp/work", "--output", "/tmp/out", "--sizes", "1001"])
    assert rejected.value.code == 2
    assert "--sizes above 1000 require --large-scales" in capsys.readouterr().err
    assert called == []

    assert harness.main([
        "--work-dir", "/tmp/work", "--output", "/tmp/out", "--sizes", "1001",
        "--large-scales",
    ]) == 0
    assert len(called) == 1


def test_default_tiny_run_skips_static_build(tmp_path: Path) -> None:
    work_dir = tmp_path / "benchmark-work"
    output = work_dir / "report.json"

    result = _run(work_dir, output, "--sizes", "3", "--repeats", "1")

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    static = report["results"][0]["static_build"]
    assert static == {
        "status": "skipped",
        "reason": "not requested; pass --static-build to opt in",
        "latency_ms": None,
        "file_count": None,
        "output_bytes": None,
    }
    assert report["harness_options"]["large_scales"] is False
    assert report["harness_options"]["static_build"] is False
    assert report["metric_semantics"]["browser_graph_mount"] == (
        "navigation start until a Cytoscape canvas is attached; canvas attachment only, "
        "not semantic readiness, layout completion, node availability, or interactions"
    )


def test_generation_is_byte_deterministic_across_work_directories(tmp_path: Path) -> None:
    roots = [tmp_path / "run-a", tmp_path / "run-b"]
    reports = []
    snapshots = []
    for root in roots:
        output = root / "report.json"
        result = _run(
            root, output,
            "--sizes", "2", "--repeats", "1", "--skip-static-above", "0",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        reports.append(json.loads(output.read_text(encoding="utf-8")))
        bundle = root / "size-00000002" / "bundle"
        snapshots.append({
            path.relative_to(bundle).as_posix(): path.read_bytes()
            for path in sorted(bundle.rglob("*"))
            if path.is_file()
        })

    first = reports[0]["results"][0]["generated_bundle"]
    second = reports[1]["results"][0]["generated_bundle"]
    assert first == second
    assert snapshots[0] == snapshots[1]


def test_refuses_repo_root_and_does_not_clean_unowned_directory(tmp_path: Path) -> None:
    refused = _run(
        TOOLKIT_ROOT,
        tmp_path / "repo-root-report.json",
        "--sizes", "2", "--repeats", "1",
    )
    assert refused.returncode == 2
    assert "refusing dangerous --work-dir" in refused.stderr

    work_dir = tmp_path / "contained"
    occupied = work_dir / "size-00000002"
    occupied.mkdir(parents=True)
    sentinel = occupied / "caller-owned.txt"
    sentinel.write_text("keep", encoding="utf-8")
    output = work_dir / "report.json"
    unowned = _run(
        work_dir, output,
        "--sizes", "2", "--repeats", "1", "--skip-static-above", "0",
    )
    assert unowned.returncode == 2
    assert "unowned benchmark directory" in unowned.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not output.exists()
