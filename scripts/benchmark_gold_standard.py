#!/usr/bin/env python3
"""Deterministic, dependency-light benchmark harness for okf-loom.

The default path uses only Python's standard library and the checkout-local
``okf_loom`` runtime.  Browser graph mounting is explicitly opt-in and is
measured only when both Playwright and a system Chrome/Chromium executable are
available.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from okf_loom import Bundle, LOOM_VERSION, SPEC_VERSION
from okf_loom.render import build_graph_data, build_site
from okf_loom.search import SearchMode, search_bundle


SCHEMA_VERSION = 1
DEFAULT_SIZES = (100, 1_000)
MAX_REPEATS = 20
OWNERSHIP_MARKER = ".okf-gold-standard-benchmark"
REPO_ROOT = Path(__file__).resolve().parent.parent

SEARCH_CASES: tuple[tuple[str, SearchMode, str, dict[str, str]], ...] = (
    ("lexical", SearchMode.LEXICAL, "deterministic benchmark", {}),
    ("semantic", SearchMode.SEMANTIC, "deterministic benchmark", {}),
    ("hybrid", SearchMode.HYBRID, "deterministic benchmark", {}),
    ("tag", SearchMode.TAG, "benchmark", {}),
    ("entity", SearchMode.ENTITY, "Benchmark Entity 00", {}),
    ("relation", SearchMode.RELATION, "", {"relation": "depends_on"}),
)


class UnsafePathError(ValueError):
    """Raised when a caller-selected path crosses the harness boundary."""


def _atomic_write(path: Path, data: bytes) -> None:
    """Write bytes through a sibling temporary file and atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_paths(work_dir: Path, output: Path) -> tuple[Path, Path]:
    """Normalize and fail closed on dangerous work/output path selections."""
    work = work_dir.expanduser().resolve()
    out = output.expanduser().resolve()
    home = Path.home().resolve()
    forbidden = {Path(work.anchor).resolve(), home, REPO_ROOT.resolve()}
    if work in forbidden:
        raise UnsafePathError(
            f"refusing dangerous --work-dir {work}; choose a dedicated directory"
        )
    if _is_relative_to(REPO_ROOT.resolve(), work):
        raise UnsafePathError(
            f"refusing --work-dir {work} because it contains the repository root"
        )
    if out == work or (out.exists() and out.is_dir()):
        raise UnsafePathError("--output must name a JSON file, not a directory")
    if out == work / ".okf-gold-standard-benchmark.lock":
        raise UnsafePathError("--output cannot replace the benchmark lock file")
    if _is_relative_to(out, work):
        rel = out.relative_to(work)
        if rel.parts and rel.parts[0].startswith("size-"):
            raise UnsafePathError(
                "--output cannot be inside a size-specific directory that is cleaned"
            )
    return work, out


def _safe_reset_owned_dir(path: Path, work_root: Path) -> None:
    """Reset one marker-owned direct child without deleting outside work_root."""
    root = work_root.resolve()
    resolved = path.resolve()
    if resolved.parent != root or not path.name.startswith("size-"):
        raise UnsafePathError(f"refusing cleanup outside benchmark work root: {path}")
    if path.exists():
        marker = path / OWNERSHIP_MARKER
        if path.is_symlink() or not marker.is_file():
            raise UnsafePathError(
                f"refusing to replace unowned benchmark directory: {path}"
            )
        shutil.rmtree(path)
    path.mkdir(parents=False)
    _atomic_write(path / OWNERSHIP_MARKER, b"schema_version=1\n")


def _concept_text(index: int, size: int) -> str:
    next_index = (index + 1) % size
    shard = index % 16
    return (
        "---\n"
        "type: Benchmark Concept\n"
        f"title: Benchmark Concept {index:08d}\n"
        f"description: Deterministic benchmark concept {index:08d} in shard {shard:02d}.\n"
        f"tags: [benchmark, shard-{shard:02d}]\n"
        'timestamp: "2026-01-01T00:00:00Z"\n'
        "entities:\n"
        f"  - label: Benchmark Entity {shard:02d}\n"
        "    kind: synthetic\n"
        "relations:\n"
        f"  - target: concepts/c{next_index:08d}\n"
        "    type: depends_on\n"
        "---\n\n"
        "# Deterministic Benchmark Baseline\n\n"
        "This synthetic concept provides deterministic benchmark baseline text "
        f"for shard {shard:02d} and concept {index:08d}.\n\n"
        f"See [the next concept](/concepts/c{next_index:08d}.md).\n"
    )


def generate_bundle(bundle_root: Path, size: int) -> dict[str, Any]:
    """Generate a deterministic ring-shaped OKF bundle and return metadata."""
    if size < 1:
        raise ValueError("bundle size must be at least 1")
    bundle_root.mkdir(parents=True, exist_ok=False)
    concepts_dir = bundle_root / "concepts"
    concepts_dir.mkdir()
    index = (
        "---\n"
        'okf_version: "0.1"\n'
        "okf_extensions: [okf.cap.entities, okf.cap.typed_relations]\n"
        "---\n\n"
        "# Gold-standard synthetic benchmark bundle\n"
    ).encode("utf-8")
    _atomic_write(bundle_root / "index.md", index)
    for concept_index in range(size):
        _atomic_write(
            concepts_dir / f"c{concept_index:08d}.md",
            _concept_text(concept_index, size).encode("utf-8"),
        )

    digest = hashlib.sha256()
    total_bytes = 0
    files = sorted(p for p in bundle_root.rglob("*") if p.is_file())
    for path in files:
        rel = path.relative_to(bundle_root).as_posix().encode("utf-8")
        data = path.read_bytes()
        total_bytes += len(data)
        digest.update(rel + b"\0" + data)
    return {
        "bytes": total_bytes,
        "concepts": size,
        "files": len(files),
        "sha256": digest.hexdigest(),
        "edge_topology": {
            "kind": "directed_ring",
            "markdown_edges": size,
            "typed_relation_edges": size,
            "raw_edges": size * 2,
            "unique_source_target_pairs": size,
            "outgoing_edges_per_concept": 2,
        },
        "determinism": {
            "randomness": "none",
            "timestamp": "2026-01-01T00:00:00Z",
            "id_format": "concepts/cNNNNNNNN",
        },
    }


def _latency_stats(samples_ms: list[float]) -> dict[str, Any]:
    ordered = sorted(samples_ms)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "median_ms": round(statistics.median(ordered), 6),
        "p95_ms": round(ordered[p95_index], 6),
        "samples_ms": [round(value, 6) for value in samples_ms],
    }


def _measure(repeats: int, operation: Callable[[], Any]) -> tuple[dict[str, Any], Any]:
    samples: list[float] = []
    last_result: Any = None
    for _ in range(repeats):
        started = time.perf_counter_ns()
        last_result = operation()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return _latency_stats(samples), last_result


def _measure_search(bundle_root: Path, repeats: int) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for label, mode, query, extra in SEARCH_CASES:
        cold_samples: list[float] = []
        cold_hits = 0
        for _ in range(repeats):
            cold_bundle = Bundle.load(bundle_root)
            started = time.perf_counter_ns()
            results = search_bundle(
                cold_bundle, query, mode=mode, limit=20, **extra
            )
            cold_samples.append((time.perf_counter_ns() - started) / 1_000_000)
            cold_hits = len(results)

        warm_bundle = Bundle.load(bundle_root)
        search_bundle(warm_bundle, query, mode=mode, limit=20, **extra)
        warm_samples: list[float] = []
        warm_hits = 0
        for _ in range(repeats):
            started = time.perf_counter_ns()
            results = search_bundle(
                warm_bundle, query, mode=mode, limit=20, **extra
            )
            warm_samples.append((time.perf_counter_ns() - started) / 1_000_000)
            warm_hits = len(results)

        metrics[label] = {
            "query": query,
            "options": extra,
            "limit": 20,
            "cold": {**_latency_stats(cold_samples), "hit_count": cold_hits},
            "warm": {**_latency_stats(warm_samples), "hit_count": warm_hits},
            "semantics": {
                "cold": "search_bundle on a newly loaded Bundle; Bundle.load time excluded",
                "warm": "search_bundle after one untimed identical priming query",
            },
        }
    return metrics


def _measure_incremental_reload(
    bundle_root: Path, size: int, repeats: int
) -> dict[str, Any]:
    target = bundle_root / "concepts" / "c00000000.md"
    original = target.read_bytes()
    samples: list[float] = []
    loaded_concepts = 0
    try:
        for repeat in range(repeats):
            changed = original + f"\nBenchmark change marker {repeat:02d}.\n".encode()
            started = time.perf_counter_ns()
            _atomic_write(target, changed)
            loaded_concepts = len(Bundle.load(bundle_root))
            samples.append((time.perf_counter_ns() - started) / 1_000_000)
            _atomic_write(target, original)
    finally:
        _atomic_write(target, original)
    return {
        **_latency_stats(samples),
        "changed_file": "concepts/c00000000.md",
        "loaded_concepts": loaded_concepts,
        "semantics": (
            "one atomic file replacement plus a full synchronous Bundle.load; "
            "this does not measure watcher latency or an incremental parser"
        ),
    }


def _measure_graph(bundle_root: Path, repeats: int) -> dict[str, Any]:
    model_samples: list[float] = []
    payload_samples: list[float] = []
    payload: dict[str, Any] = {}
    graph = None
    for _ in range(repeats):
        bundle = Bundle.load(bundle_root)
        started = time.perf_counter_ns()
        graph = bundle.graph()
        model_samples.append((time.perf_counter_ns() - started) / 1_000_000)
        started = time.perf_counter_ns()
        payload = build_graph_data(bundle)
        payload_samples.append((time.perf_counter_ns() - started) / 1_000_000)
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "model_build_latency_ms": _latency_stats(model_samples),
        "viewer_payload_build_latency_ms": _latency_stats(payload_samples),
        "serialized_payload_bytes": len(serialized),
        "nodes": len(payload.get("nodes", [])),
        "raw_edges": len(graph.edges) if graph is not None else 0,
        "serialized_edges": len(payload.get("edges", [])),
        "serialization": "UTF-8 JSON, sorted keys, compact separators",
    }


def _measure_static(
    bundle_root: Path, out_dir: Path, size: int, repeats: int, threshold: int
) -> dict[str, Any]:
    if size > threshold:
        return {
            "status": "skipped",
            "reason": f"size {size} exceeds --skip-static-above {threshold}",
            "latency_ms": None,
            "file_count": None,
            "output_bytes": None,
        }
    samples: list[float] = []
    stats: dict[str, Any] = {}
    for _ in range(repeats):
        bundle = Bundle.load(bundle_root)
        started = time.perf_counter_ns()
        stats = build_site(bundle, out_dir, target="static")
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return {
        "status": "measured",
        "reason": None,
        "latency_ms": _latency_stats(samples),
        "file_count": stats["files"],
        "output_bytes": stats["bytes"],
        "target": "static",
    }


def _system_chrome() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _measure_browser_mount(
    requested: bool, bundle_root: Path, out_dir: Path, repeats: int
) -> dict[str, Any]:
    semantics = {
        "phase": "canvas_attachment",
        "semantic_ready": False,
    }
    if not requested:
        return {
            **semantics,
            "latency_ms": None,
            "unavailable_reason": "not requested; pass --browser-graph-mount to opt in",
        }
    chrome = _system_chrome()
    if chrome is None:
        return {
            **semantics,
            "latency_ms": None,
            "unavailable_reason": "no system Chrome/Chromium executable found",
        }
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            **semantics,
            "latency_ms": None,
            "unavailable_reason": "Python Playwright is not installed",
        }

    try:
        build_site(Bundle.load(bundle_root), out_dir, target="single-file")
        samples: list[float] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=chrome, headless=True)
            try:
                for _ in range(repeats):
                    page = browser.new_page()
                    started = time.perf_counter_ns()
                    page.goto((out_dir / "index.html").as_uri(), wait_until="domcontentloaded")
                    page.wait_for_selector("#okf-graph canvas", state="attached", timeout=30_000)
                    samples.append((time.perf_counter_ns() - started) / 1_000_000)
                    page.close()
            finally:
                browser.close()
        return {
            **semantics,
            "latency_ms": _latency_stats(samples),
            "unavailable_reason": None,
            "semantics": "file navigation start until Cytoscape canvas is attached",
            "browser_executable": chrome,
        }
    except Exception as exc:  # optional probe must report failure, never disappear
        return {
            **semantics,
            "latency_ms": None,
            "unavailable_reason": f"browser mount failed: {type(exc).__name__}: {exc}",
        }


def _git_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    work_root, output = validate_paths(args.work_dir, args.output)
    work_root.mkdir(parents=True, exist_ok=True)
    lock_path = work_root / ".okf-gold-standard-benchmark.lock"
    try:
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise UnsafePathError(
            f"benchmark work directory is already locked: {work_root}"
        ) from exc
    try:
        os.write(lock_fd, f"pid={os.getpid()}\n".encode("ascii"))
        return _run_benchmark_locked(args, work_root, output)
    finally:
        os.close(lock_fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _run_benchmark_locked(
    args: argparse.Namespace, work_root: Path, output: Path
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for size in args.sizes:
        size_root = work_root / f"size-{size:08d}"
        _safe_reset_owned_dir(size_root, work_root)
        bundle_root = size_root / "bundle"
        generated = generate_bundle(bundle_root, size)
        load_latency, loaded = _measure(args.repeats, lambda: Bundle.load(bundle_root))
        results.append({
            "size": size,
            "generated_bundle": generated,
            "bundle_load": {
                **load_latency,
                "loaded_concepts": len(loaded),
                "semantics": "full synchronous Bundle.load from the generated directory",
            },
            "search": _measure_search(bundle_root, args.repeats),
            "incremental_one_file_reload": _measure_incremental_reload(
                bundle_root, size, args.repeats
            ),
            "graph": _measure_graph(bundle_root, args.repeats),
            "static_build": (
                _measure_static(
                    bundle_root,
                    size_root / "static-output",
                    size,
                    args.repeats,
                    args.skip_static_above,
                )
                if args.static_build
                else {
                    "status": "skipped",
                    "reason": "not requested; pass --static-build to opt in",
                    "latency_ms": None,
                    "file_count": None,
                    "output_bytes": None,
                }
            ),
            "browser_graph_mount": _measure_browser_mount(
                args.browser_graph_mount,
                bundle_root,
                size_root / "browser-output",
                args.repeats,
            ),
        })

    report = {
        "schema_version": SCHEMA_VERSION,
        "units": {"bytes": "bytes", "latency": "milliseconds"},
        "environment": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "repository_revision": _git_revision(),
            "okf_loom_version": LOOM_VERSION,
            "okf_spec_version": SPEC_VERSION,
        },
        "harness_options": {
            "work_dir": str(work_root),
            "output": str(output),
            "sizes": list(args.sizes),
            "repeats": args.repeats,
            "skip_static_above": args.skip_static_above,
            "browser_graph_mount": args.browser_graph_mount,
            "large_scales": args.large_scales,
            "static_build": args.static_build,
        },
        "metric_semantics": {
            "bundle_load": (
                "full synchronous Bundle.load; filesystem cache state is not controlled"
            ),
            "graph_model_build": "first bundle.graph() call on a newly loaded Bundle",
            "graph_viewer_payload_build": "build_graph_data after graph model construction",
            "static_build": (
                "build_site(target='static') from call start through atomic "
                "output-tree replacement; "
                "Bundle.load is excluded"
            ),
            "browser_graph_mount": (
                "navigation start until a Cytoscape canvas is attached; canvas "
                "attachment only, not semantic readiness, layout completion, node "
                "availability, or interactions"
            ),
        },
        "results": results,
    }
    encoded = (
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    _atomic_write(output, encoded)
    return report


def _parse_sizes(raw: str) -> tuple[int, ...]:
    try:
        sizes = tuple(int(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--sizes must be comma-separated integers") from exc
    if not sizes or any(size < 1 for size in sizes):
        raise argparse.ArgumentTypeError("--sizes values must all be positive")
    if len(set(sizes)) != len(sizes):
        raise argparse.ArgumentTypeError("--sizes values must be unique")
    return sizes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--sizes", type=_parse_sizes, default=DEFAULT_SIZES,
        help="comma-separated concept counts (default: 100,1000; above 1000 requires --large-scales)",
    )
    parser.add_argument(
        "--large-scales", action="store_true",
        help="allow explicitly requested sizes above 1000",
    )
    parser.add_argument(
        "--repeats", type=int, default=3,
        help=f"samples per repeated metric, from 1 to {MAX_REPEATS} (default: 3)",
    )
    parser.add_argument(
        "--skip-static-above", type=int, default=50_000,
        help="with --static-build, skip static builds above this concept count (default: 50000)",
    )
    parser.add_argument(
        "--static-build", action="store_true",
        help="opt in to static-build measurement (large static builds also require --large-scales)",
    )
    parser.add_argument(
        "--browser-graph-mount", action="store_true",
        help=(
            "opt in to navigation-to-Cytoscape-canvas-attachment timing only; "
            "does not measure semantic readiness, layout, nodes, or interactions"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if any(size > 1_000 for size in args.sizes) and not args.large_scales:
        parser.error("--sizes above 1000 require --large-scales")
    if not 1 <= args.repeats <= MAX_REPEATS:
        parser.error(f"--repeats must be between 1 and {MAX_REPEATS}")
    if args.skip_static_above < 0:
        parser.error("--skip-static-above must be non-negative")
    try:
        run_benchmark(args)
    except (UnsafePathError, FileExistsError, OSError, ValueError) as exc:
        parser.exit(2, f"benchmark error: {exc}\n")
    print(Path(args.output).expanduser().resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
