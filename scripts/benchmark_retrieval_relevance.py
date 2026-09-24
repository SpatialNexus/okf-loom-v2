#!/usr/bin/env python3
"""Deterministic precision benchmark for the A+B retrieval release.

The harness generates one small OKF bundle outside the repository and invokes
the checkout-local Python search API in two checkouts.  A compatibility adapter
keeps baseline handling isolated: revisions without section evidence report it
as unavailable rather than synthesizing headings or citations.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_K = 3
BASELINE_REVISION = "c167909505da9abbac61e2d3889ebf51f4f1d947"
REPO_ROOT = Path(__file__).resolve().parent.parent
_CITATION = re.compile(r"^\[(?P<path>[^\]#]+)#L(?P<start>[1-9]\d*)-L(?P<end>[1-9]\d*)(?:@C\d+-C\d+)?\]$")

def measured_source_paths(checkout: Path) -> tuple[str, ...]:
    """Discover the complete measured runtime source closure in *checkout* only.

    Restrict viewer inputs to executable/template source types so editor,
    coverage, and capture artifacts created beside them cannot alter release
    identity between a dirty worktree and a clean committed checkout.
    """
    root = checkout.resolve()
    candidates = [root / "scripts/benchmark_retrieval_relevance.py"]
    candidates.extend((root / "scripts/okf_loom").rglob("*.py"))
    runtime_viewer_sources = {
        root / "scripts/okf_loom/viewer/templates": {".html"},
        root / "scripts/okf_loom/viewer/static": {".css", ".js"},
    }
    for directory, suffixes in runtime_viewer_sources.items():
        candidates.extend(
            path for path in directory.rglob("*")
            if path.is_file() and path.suffix.lower() in suffixes
        )
    paths = tuple(sorted({path.relative_to(root).as_posix() for path in candidates if path.is_file()}))
    if "scripts/benchmark_retrieval_relevance.py" not in paths:
        raise ValueError("benchmark harness is missing from selected checkout")
    return paths


CONCEPT_FIXTURE: dict[str, str] = {
    "policies/retention": """---
type: Policy
title: Data Retention Policy
description: Canonical retention requirements for analytics records.
tags: [governance, retention]
---

# Scope

Quartz archives are mentioned here, but no duration is defined.

## Retention Windows

The quartz event ledger has a ninety day retention window.
""",
    "glossary/quartz": """---
type: Glossary Term
title: Quartz
description: A mineral and an internal dashboard codename.
tags: [glossary]
---

# Meaning

Quartz retention window is a phrase used in examples, not a policy.
""",
    "entities/customer-identity": """---
type: Data Product
title: Customer Identity Resolution
description: Resolves customer identity records despite spelling variation.
tags: [identity]
entities:
  - label: Atlas Person Graph
    aliases: [APG]
---

# Resolution

Customer identity resolution joins account aliases into one profile.
""",
    "entities/identity-noise": """---
type: Note
title: Customer Resolution Meeting
description: Scheduling notes, not the identity data product.
tags: [meeting]
---

# Agenda

Discuss customer support and room resolution.
""",
    "runbooks/payment-retries": """---
type: Runbook
title: Payment Retry Safety
description: Safe replay guidance for payment operations.
tags: [payments, critical-path]
---

# Symptoms

Repeated gateway failures can create duplicate payment attempts.

## Idempotent Retry

Use the payment idempotency key before retrying a timed out charge.
""",
    "notes/payment-noise": """---
type: Note
title: Payment Retry Brainstorm
description: Unapproved ideas about payment retries.
tags: [draft]
---

# Ideas

Payment retry idempotency is listed here without operational guidance.
""",
    "services/checkout": """---
type: Service
title: Checkout Service
description: Customer checkout orchestration.
tags: [critical-path, commerce]
relations:
  - target: systems/ledger
    type: depends_on
---

# Dependencies

Checkout writes completed charges to the ledger.
""",
    "notes/critical-path": """---
type: Note
title: Critical Path Phrase
description: Contains the phrase but does not carry the tag.
tags: [draft]
---

# Text

This note says critical-path only in body text.
""",
    "systems/ledger": """---
type: System
title: Ledger System
description: Immutable accounting journal.
tags: [finance]
entities:
  - label: Orion Ledger
    aliases: [OL]
---

# Journal

Orion Ledger records settled charge entries.
""",
    "systems/orion-noise": """---
type: System
title: Orion Dashboard
description: Operational charts unrelated to accounting.
tags: [observability]
entities:
  - label: Orion Dashboard
---

# Charts

The dashboard visualizes service latency.
""",
    "runbooks/deploy": """---
type: Runbook
title: Deployment Recovery
description: Deployment and rollback procedures.
tags: [operations]
---

# Preflight Verification

Verify the release checksum before deployment.

## Rollback Verification

During rollback, compare the recovery checksum before restoring traffic.
""",
    "notes/rollback-noise": """---
type: Note
title: Rollback Verification Draft
description: An obsolete checklist.
tags: [draft]
---

# Draft

Rollback checksum verification was proposed but never approved.
""",
}


QUERY_FIXTURE: tuple[dict[str, Any], ...] = (
    {"id": "lexical-retention", "intent": "lexical", "query": "quartz ninety day retention",
     "mode": "lexical", "options": {}, "expected_concepts": ["policies/retention"],
     "context": {"budget": 128, "neighbors": 0, "limit": 3},
     "expected_heading": "Retention Windows", "citation_expectation": {"required": True, "path": "policies/retention.md"},
     "hard_negatives": ["glossary/quartz"]},
    {"id": "semantic-identity-typo", "intent": "semantic", "query": "custmer identty resoluton",
     "mode": "semantic", "options": {}, "expected_concepts": ["entities/customer-identity"],
     "expected_heading": "Resolution", "citation_expectation": {"required": True, "path": "entities/customer-identity.md"},
     "hard_negatives": ["entities/identity-noise"]},
    {"id": "hybrid-payment", "intent": "hybrid", "query": "payment retry idempotency key",
     "mode": "hybrid", "options": {}, "expected_concepts": ["runbooks/payment-retries"],
     "expected_heading": "Idempotent Retry", "citation_expectation": {"required": True, "path": "runbooks/payment-retries.md"},
     "hard_negatives": ["notes/payment-noise"]},
    {"id": "tag-critical-path", "intent": "tag", "query": "critical-path", "mode": "tag", "options": {},
     "expected_concepts": ["runbooks/payment-retries", "services/checkout"], "expected_heading": "Dependencies",
     "citation_expectation": {"required": True, "path": "services/checkout.md"},
     "hard_negatives": ["notes/critical-path"]},
    {"id": "entity-orion-ledger", "intent": "entity", "query": "Orion Ledger", "mode": "entity", "options": {},
     "expected_concepts": ["systems/ledger"], "expected_heading": "Journal",
     "citation_expectation": {"required": True, "path": "systems/ledger.md"},
     "hard_negatives": ["systems/orion-noise"]},
    {"id": "relation-ledger-dependency", "intent": "relation", "query": "", "mode": "relation",
     "options": {"relation": "depends_on", "target": "systems/ledger"},
     "expected_concepts": ["services/checkout"], "expected_heading": "Dependencies",
     "citation_expectation": {"required": True, "path": "services/checkout.md"},
     "hard_negatives": ["systems/orion-noise"]},
    {"id": "section-rollback", "intent": "section-disambiguation", "query": "rollback recovery checksum traffic",
     "mode": "lexical", "options": {}, "expected_concepts": ["runbooks/deploy"],
     "expected_heading": "Rollback Verification", "citation_expectation": {"required": True, "path": "runbooks/deploy.md"},
     "hard_negatives": ["notes/rollback-noise"]},
)


def _stable_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def fixture_digest() -> str:
    return hashlib.sha256(_stable_bytes({"concepts": CONCEPT_FIXTURE, "queries": QUERY_FIXTURE})).hexdigest()


def generate_fixture(root: Path) -> None:
    if root.exists():
        raise ValueError(f"fixture directory already exists: {root}")
    root.mkdir(parents=True)
    (root / "index.md").write_text(
        "---\nokf_version: \"0.1\"\nokf_extensions: [okf.cap.entities, okf.cap.typed_relations]\n---\n\n# Relevance fixture\n",
        encoding="utf-8",
    )
    for concept_id, text in sorted(CONCEPT_FIXTURE.items()):
        path = root.joinpath(*concept_id.split("/")).with_suffix(".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


_ADAPTER = r'''
import json, re, sys
from pathlib import Path
checkout, bundle_path, fixture_path = map(Path, sys.argv[1:4])
sys.path.insert(0, str(checkout / "scripts"))
from okf_loom import Bundle
from okf_loom.search import SearchMode, search_bundle
fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
bundle = Bundle.load(bundle_path)
try:
    from okf_loom.retrieval import ContextRequest, RetrievalService, SearchRequest
except (ImportError, ModuleNotFoundError):
    ContextRequest = RetrievalService = SearchRequest = None

def citation_valid(p):
    if not isinstance(p, dict): return False
    citation = p.get("citation", "")
    match = re.match(r"^\[([^\]#]+)#L([1-9]\d*)-L([1-9]\d*)(?:@C\d+-C\d+)?\]$", citation)
    if not match or match.group(1) != p.get("path"): return False
    start, end = int(match.group(2)), int(match.group(3))
    root = bundle_path.resolve()
    path = (bundle_path / match.group(1)).resolve()
    try: path.relative_to(root)
    except ValueError: return False
    if not path.is_file() or start != p.get("line_start") or end != p.get("line_end"): return False
    lines = path.read_text(encoding="utf-8").splitlines()
    if end > len(lines): return False
    excerpt = "\n".join(lines[start-1:end])
    if p.get("char_start") is not None:
        excerpt = excerpt[p["char_start"]:p["char_end"]]
    return excerpt == p.get("excerpt")

queries = []
contexts = []
for case in fixture:
    kwargs = dict(case.get("options", {}))
    if RetrievalService is not None:
        service = RetrievalService(bundle)
        request = SearchRequest(query=case["query"], mode=case["mode"], limit=3, **kwargs)
        results = service.search(request)
        context_options = {"budget": 6000, "neighbors": 0, "limit": 3}
        context_options.update(case.get("context", {}))
        contexts.append({"id": case["id"], "pack": service.context(ContextRequest(
            query=case["query"], mode=case["mode"], limit=context_options["limit"],
            neighbors=context_options["neighbors"], budget_tokens=context_options["budget"],
            **kwargs))})
    else:
        results = search_bundle(bundle, case["query"], mode=SearchMode(case["mode"]), limit=3, **kwargs)
    rows = []
    for result in results:
        data = result.as_dict()
        passage = ((data.get("evidence") or {}).get("passage") or {})
        rows.append({"concept_id": data["concept_id"], "score": data["score"],
                     "source_backend": data["source_backend"],
                     "passage_id": passage.get("passage_id"),
                     "backends": data.get("backends", []), "reasons": data.get("reasons", []),
                     "heading": passage.get("heading"), "citation": passage.get("citation"),
                     "path": passage.get("path"), "line_start": passage.get("line_start"),
                     "line_end": passage.get("line_end"), "char_start": passage.get("char_start"),
                     "char_end": passage.get("char_end"), "excerpt": passage.get("excerpt"),
                     "citation_valid": citation_valid(passage) if passage else None})
    queries.append({"id": case["id"], "results": rows})
print(json.dumps({"section_evidence_available": RetrievalService is not None, "queries": queries,
                  "contexts": contexts}, sort_keys=True))
'''


def _revision(checkout: Path, *, worktree_label: bool) -> str:
    result = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    revision = result.stdout.strip()
    if worktree_label:
        dirty = subprocess.run(["git", "-C", str(checkout), "status", "--porcelain"], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
        if dirty:
            revision += "+worktree"
    return revision


def _git_output(checkout: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(checkout), *args], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=True).stdout.strip()


def _source_manifest(current: Path) -> dict[str, Any]:
    """Return a deterministic, host-path-free digest of measured sources."""
    hashes: dict[str, str] = {}
    root = current.resolve()
    paths = measured_source_paths(root)
    for relative in paths:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe measured source path: {relative}")
        source = (root / path).resolve()
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"measured source escapes checkout: {relative}") from exc
        if not source.is_file():
            raise ValueError(f"measured source is missing: {relative}")
        hashes[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
    digest_input = b"".join(
        relative.encode("utf-8") + b"\0" + hashes[relative].encode("ascii") + b"\n"
        for relative in paths
    )
    return {
        "algorithm": "sha256(path\\0sha256\\n)",
        "paths": list(paths),
        "files": hashes,
        "sha256": hashlib.sha256(digest_input).hexdigest(),
    }


def _source_provenance(current: Path) -> dict[str, Any]:
    manifest = _source_manifest(current)
    patch = subprocess.run(
        ["git", "-C", str(current), "diff", "--binary", "--no-ext-diff", "HEAD", "--",
         *manifest["paths"]],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout
    harness = manifest["files"]["scripts/benchmark_retrieval_relevance.py"]
    fixture = fixture_digest()
    release_content = hashlib.sha256(_stable_bytes({
        "source_manifest_sha256": manifest["sha256"],
        "harness_sha256": harness,
        "fixture_sha256": fixture,
    })).hexdigest()
    return {
        "head_revision": _git_output(current, "rev-parse", "HEAD"),
        "head_tree": _git_output(current, "rev-parse", "HEAD^{tree}"),
        "source_manifest": manifest,
        "measured_patch_sha256": hashlib.sha256(patch).hexdigest(),
        "harness_sha256": harness,
        "fixture_sha256": fixture,
        "release_content_sha256": release_content,
    }


def _verify_source_provenance(stored: Any, current: Path) -> None:
    """Verify immutable measured content; Git state is informational only."""
    if not isinstance(stored, dict) or set(stored) != {
        "head_revision", "head_tree", "source_manifest", "measured_patch_sha256",
        "harness_sha256", "fixture_sha256", "release_content_sha256",
    }:
        raise ValueError("source provenance fields mismatch")
    manifest = _source_manifest(current)
    harness = manifest["files"]["scripts/benchmark_retrieval_relevance.py"]
    fixture = fixture_digest()
    release_content = hashlib.sha256(_stable_bytes({
        "source_manifest_sha256": manifest["sha256"],
        "harness_sha256": harness,
        "fixture_sha256": fixture,
    })).hexdigest()
    if not _equivalent_contract(stored["source_manifest"], manifest):
        raise ValueError("source manifest mismatch")
    if stored["harness_sha256"] != harness or stored["fixture_sha256"] != fixture:
        raise ValueError("harness or fixture provenance mismatch")
    if stored["release_content_sha256"] != release_content:
        raise ValueError("release content identity mismatch")
    if (not re.fullmatch(r"[0-9a-f]{40}", stored["head_revision"] or "") or
            not re.fullmatch(r"[0-9a-f]{40}", stored["head_tree"] or "") or
            not re.fullmatch(r"[0-9a-f]{64}", stored["measured_patch_sha256"] or "")):
        raise ValueError("informational Git identity is malformed")


def run_checkout(checkout: Path, bundle: Path, fixture_file: Path, *, worktree_label: bool) -> dict[str, Any]:
    process = subprocess.run(
        [sys.executable, "-c", _ADAPTER, str(checkout), str(bundle), str(fixture_file)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if process.returncode:
        raise RuntimeError(f"checkout adapter failed for {checkout}: {process.stderr.strip()}")
    payload = json.loads(process.stdout)
    payload["revision"] = _revision(checkout, worktree_label=worktree_label)
    return payload


_PORTABLE_ADAPTER = r'''
const fs=require("fs"),vm=require("vm"),crypto=require("crypto");
const source=fs.readFileSync(process.argv[1],"utf8"),data=process.argv[2],fixture=JSON.parse(fs.readFileSync(process.argv[3],"utf8"));
const manifest=JSON.parse(fs.readFileSync(data+"/passages-manifest.json","utf8"));
const chunks=manifest.chunks.map(d=>fs.readFileSync(data+"/"+d.file,"utf8"));
const context={window:{crypto:crypto.webcrypto,TextEncoder:TextEncoder},TextEncoder:TextEncoder,console:console};
vm.createContext(context);vm.runInContext(source,context);
context.window.okfPortableRetrieval.validate(manifest,chunks).then(validated=>{
  const runtime=context.window.okfPortableRetrieval.create(manifest,validated.passages,validated.edges);
  const contexts=[];const queries=fixture.map(c=>{
    const options=Object.assign({query:c.query,mode:c.mode,limit:3},c.options||{}),seen=new Set(),results=[];
    runtime.search(options).rows.forEach(row=>{const p=row.passage||row.concept||{},id=p.concept_id||p.id;if(id&&!seen.has(id)){seen.add(id);results.push({concept_id:id,score:row.score,source_backend:c.mode==='semantic'?'semantic-lite':c.mode,passage_id:row.passage&&row.passage.passage_id||null,backends:row.backends||[],reasons:row.reasons||[],heading:row.passage&&row.passage.heading||null,citation:row.passage&&row.passage.citation||null});}});
    contexts.push({id:c.id,pack:runtime.context({},Object.assign({},options,{budget:6000,neighbors:0},c.context||{}),[])});
    return {id:c.id,results:results.slice(0,3)};
  });
  process.stdout.write(JSON.stringify({status:"measured",queries:queries,contexts:contexts}));
}).catch(error=>{process.stderr.write(String(error&&error.stack||error));process.exitCode=1;});
'''


def run_portable(checkout: Path, bundle: Path, fixture_file: Path, output: Path) -> dict[str, Any]:
    """Run the checked-in portable JavaScript when Node is available."""
    node = shutil.which("node")
    if node is None:
        return {"status": "unavailable", "reason": "Node.js is not installed", "queries": []}
    build = subprocess.run(
        [sys.executable, "-c",
         "import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
         "from okf_loom import Bundle;from okf_loom.render import build_site;"
         "build_site(Bundle.load(Path(sys.argv[2])),Path(sys.argv[3]),target='static')",
         str(checkout / "scripts"), str(bundle), str(output)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if build.returncode:
        return {"status": "unavailable", "reason": f"portable static build failed: {build.stderr.strip()}", "queries": []}
    process = subprocess.run(
        [node, "-e", _PORTABLE_ADAPTER,
         str(checkout / "scripts" / "okf_loom" / "viewer" / "static" / "portable-retrieval.js"),
         str(output / "__data"), str(fixture_file)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if process.returncode:
        return {"status": "unavailable", "reason": f"portable runtime failed: {process.stderr.strip()}", "queries": []}
    return json.loads(process.stdout)


def score_query(case: dict[str, Any], rows: list[dict[str, Any]], *, k: int = DEFAULT_K,
                evidence_available: bool) -> dict[str, Any]:
    expected = set(case["expected_concepts"])
    top = rows[:k]
    relevance = [1 if row.get("concept_id") in expected else 0 for row in top]
    precision = sum(relevance) / k
    first = next((index for index, relevant in enumerate(relevance, 1) if relevant), None)
    mrr = 1.0 / first if first else 0.0
    dcg = sum(rel / math.log2(index + 1) for index, rel in enumerate(relevance, 1))
    ideal_count = min(len(expected), k)
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_count + 1))
    section = citation = None
    if evidence_available:
        expected_rows = [row for row in top if row.get("concept_id") in expected]
        section = float(any(row.get("heading") == case["expected_heading"] for row in expected_rows))
        expected_path = case["citation_expectation"]["path"]
        citation = float(any(
            row.get("citation_valid") is True
            and (match := _CITATION.match(row.get("citation") or "")) is not None
            and match.group("path") == expected_path
            for row in expected_rows
        ))
    return {
        "precision_at_k": precision, "mrr": mrr, "ndcg_at_k": dcg / idcg if idcg else 0.0,
        "expected_section_hit": section, "citation_validity": citation,
        "evidence_status": "available" if evidence_available else "unavailable",
        "hard_negative_in_top_k": any(row.get("concept_id") in set(case["hard_negatives"]) for row in top),
        "retrieved": rows,
    }


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _score_side(raw: dict[str, Any]) -> dict[str, Any]:
    by_id = {item["id"]: item["results"] for item in raw["queries"]}
    evidence = bool(raw["section_evidence_available"])
    per_query = []
    for case in QUERY_FIXTURE:
        if case["id"] not in by_id:
            raise ValueError(f"missing query result: {case['id']}")
        per_query.append({"id": case["id"], **score_query(case, by_id[case["id"]], evidence_available=evidence)})
    return {"revision": raw["revision"], "section_evidence_available": evidence, "queries": per_query,
            "metrics": {
                "precision_at_3": _mean([q["precision_at_k"] for q in per_query]),
                "mrr": _mean([q["mrr"] for q in per_query]),
                "ndcg_at_3": _mean([q["ndcg_at_k"] for q in per_query]),
                "expected_section_hit_rate": _mean([q["expected_section_hit"] for q in per_query if q["expected_section_hit"] is not None]),
                "citation_validity": _mean([q["citation_validity"] for q in per_query if q["citation_validity"] is not None]),
                "hard_negative_query_rate_at_3": _mean([float(q["hard_negative_in_top_k"]) for q in per_query]),
            }}


def _portable_parity(after: dict[str, Any], portable: dict[str, Any] | None) -> dict[str, Any]:
    if not portable or portable.get("status") != "measured":
        return {"before": None, "after": None, "status": "unavailable",
                "reason": (portable or {}).get("reason", "Portable runtime was not requested.")}
    live_rows = {query["id"]: query["retrieved"] for query in after["queries"]}
    portable_contracts = {query["id"]: query["results"] for query in portable["queries"]}
    comparisons = []
    keys = ("concept_id", "score", "source_backend", "passage_id", "backends",
            "reasons", "heading", "citation")
    for case in QUERY_FIXTURE:
        live_contract = [{key: row.get(key) for key in keys}
                         for row in live_rows.get(case["id"], [])]
        portable_contract = [{key: row.get(key) for key in keys}
                             for row in portable_contracts.get(case["id"], [])]
        live_order = [row["concept_id"] for row in live_contract]
        portable_order = [row["concept_id"] for row in portable_contract]
        contract_match = _equivalent_contract(live_contract, portable_contract)
        comparisons.append({"id": case["id"],
                            "live_top": (live_order or [None])[0],
                            "portable_top": (portable_order or [None])[0],
                            "live_order": live_order, "portable_order": portable_order,
                            "contract_match": contract_match,
                            "match": live_order == portable_order and contract_match})
    values = [float(row["match"]) for row in comparisons]
    return {"before": None, "after": _mean(values), "status": "measured",
            "semantics": "Fraction of queries with the same complete ordered top-k concepts in portable JavaScript and live Python retrieval.",
            "per_query": comparisons}


def _equivalent_contract(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isfinite(float(left)) and math.isfinite(float(right)) and math.isclose(
            float(left), float(right), rel_tol=1e-12, abs_tol=1e-12,
        )
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _equivalent_contract(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _equivalent_contract(a, b) for a, b in zip(left, right)
        )
    return left == right


def _portable_context_parity(after_raw: dict[str, Any], portable: dict[str, Any] | None) -> dict[str, Any]:
    if not portable or portable.get("status") != "measured":
        return {"after": None, "status": "unavailable",
                "reason": (portable or {}).get("reason", "Portable runtime was not requested.")}
    live = {row["id"]: row["pack"] for row in after_raw.get("contexts", [])}
    browser = {row["id"]: row["pack"] for row in portable.get("contexts", [])}
    rows = []
    for case in QUERY_FIXTURE:
        left, right = live.get(case["id"]), browser.get(case["id"])
        live_ids = [item.get("passage_id") for item in (left or {}).get("items", [])]
        portable_ids = [item.get("passage_id") for item in (right or {}).get("items", [])]
        markdown_equal = bool(left and right and
            left.get("representations", {}).get("markdown") == right.get("representations", {}).get("markdown"))
        field_equal = bool(left and right and _equivalent_contract(left, right))
        rows.append({"id": case["id"], "field_match": field_equal,
                     "markdown_byte_match": markdown_equal,
                     "live_item_ids": live_ids, "portable_item_ids": portable_ids,
                     "match": field_equal and markdown_equal and live_ids == portable_ids})
    return {"after": _mean([float(row["match"]) for row in rows]), "status": "measured",
            "semantics": "Complete pack fields (numeric values compared as JSON numbers), ordered item IDs, and canonical Markdown bytes.",
            "per_query": rows}


def build_report(before_raw: dict[str, Any], after_raw: dict[str, Any], *, command: str,
                 portable_raw: dict[str, Any] | None = None,
                 provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    before, after = _score_side(before_raw), _score_side(after_raw)
    return {
        "schema_version": SCHEMA_VERSION,
        "release_slice": "A+B retrieval relevance",
        "revisions": {"before": before["revision"], "after": after["revision"]},
        "provenance": provenance or {},
        "fixture": {"sha256": fixture_digest(), "concept_count": len(CONCEPT_FIXTURE),
                    "queries": list(QUERY_FIXTURE)},
        "metric_definitions": {
            "precision_at_3": "Relevant concepts in the first three results divided by three.",
            "mrr": "Reciprocal rank of the first expected concept, or zero when absent.",
            "ndcg_at_3": "Binary normalized discounted cumulative gain over expected concepts.",
            "expected_section_hit_rate": "Queries whose expected concept has evidence under the expected heading; null when the runtime emits no section evidence.",
            "citation_validity": "Expected-result citations whose path, physical lines, and excerpt agree with the generated Markdown; null when unavailable.",
            "hard_negative_query_rate_at_3": "Fraction of queries with a declared distractor in the first three; lower is better.",
            "portable_live_parity": "Fraction of queries with the same complete ordered top-k concepts in checked-in portable JavaScript and live Python retrieval; null when unavailable.",
            "portable_context_parity": "Fraction of queries with equivalent complete Context Pack fields, ordered item IDs, and byte-identical canonical Markdown.",
        },
          "per_query": {"before": before["queries"], "after": after["queries"]},
          "verification_material": {"baseline": before_raw, "live_after": after_raw,
                                    "portable": portable_raw},
        "aggregate": {"before": before["metrics"], "after": after["metrics"],
                      "portable_live_parity": _portable_parity(after, portable_raw),
                      "portable_context_parity": _portable_context_parity(after_raw, portable_raw)},
        "caveats": [
            "The fixture is synthetic and intentionally small; it is a regression proof, not a production traffic estimate.",
            "Semantic mode is dependency-free semantic-lite fuzzy matching, not embeddings.",
            "Baseline section/citation metrics are null when the baseline API cannot emit passage evidence.",
            "No improvement is inferred from revision labels; each metric must be read independently.",
            "Portable/live parity is measured only when Node can execute the checked-in portable runtime; otherwise it is null.",
        ],
        "command": command,
    }


def enforce_thresholds(report: dict[str, Any]) -> None:
    before = report["aggregate"]["before"]
    after = report["aggregate"]["after"]
    failures = []
    for metric in ("precision_at_3", "mrr", "ndcg_at_3"):
        if after[metric] + 1e-12 < before[metric]:
            failures.append(f"{metric} regressed")
    for metric in ("expected_section_hit_rate", "citation_validity"):
        if after[metric] != 1.0:
            failures.append(f"{metric} must equal 1.0")
    if report["aggregate"]["portable_live_parity"].get("after") != 1.0:
        failures.append("portable ordered search parity must equal 1.0")
    if report["aggregate"]["portable_context_parity"].get("after") != 1.0:
        failures.append("portable context parity must equal 1.0")
    if failures:
        raise ValueError("relevance acceptance thresholds failed: " + "; ".join(failures))


def _verified_side(rows: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(rows, list) or len(rows) != len(QUERY_FIXTURE):
        raise ValueError("per-query row count mismatch")
    by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
    if set(by_id) != {case["id"] for case in QUERY_FIXTURE} or len(by_id) != len(rows):
        raise ValueError("per-query IDs mismatch")
    verified = []
    for case in QUERY_FIXTURE:
        stored = by_id[case["id"]]
        status = stored.get("evidence_status")
        if status not in {"available", "unavailable"}:
            raise ValueError(f"invalid evidence status for {case['id']}")
        expected = {"id": case["id"], **score_query(
            case, stored.get("retrieved"), evidence_available=status == "available",
        )}
        if not _equivalent_contract(stored, expected):
            raise ValueError(f"per-query metrics mismatch for {case['id']}")
        verified.append(expected)
    metrics = {
        "precision_at_3": _mean([row["precision_at_k"] for row in verified]),
        "mrr": _mean([row["mrr"] for row in verified]),
        "ndcg_at_3": _mean([row["ndcg_at_k"] for row in verified]),
        "expected_section_hit_rate": _mean([row["expected_section_hit"] for row in verified
                                             if row["expected_section_hit"] is not None]),
        "citation_validity": _mean([row["citation_validity"] for row in verified
                                     if row["citation_validity"] is not None]),
        "hard_negative_query_rate_at_3": _mean([float(row["hard_negative_in_top_k"])
                                                  for row in verified]),
    }
    return verified, metrics


def _physical_citation_valid(row: dict[str, Any], bundle_root: Path) -> bool:
    """Reconstruct citation validity from regenerated fixture bytes."""
    match = _CITATION.match(row.get("citation") or "")
    if match is None or match.group("path") != row.get("path"):
        return False
    start, end = int(match.group("start")), int(match.group("end"))
    if type(row.get("line_start")) is not int or type(row.get("line_end")) is not int:
        return False
    if start != row["line_start"] or end != row["line_end"] or start > end:
        return False
    root = bundle_root.resolve()
    candidate = (root / match.group("path")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    if not candidate.is_file():
        return False
    lines = candidate.read_text(encoding="utf-8").splitlines()
    if end > len(lines):
        return False
    excerpt = "\n".join(lines[start - 1:end])
    char_start, char_end = row.get("char_start"), row.get("char_end")
    if char_start is None and char_end is None:
        pass
    elif (type(char_start) is not int or type(char_end) is not int or
          char_start < 0 or char_end < char_start or char_end > len(excerpt)):
        return False
    else:
        excerpt = excerpt[char_start:char_end]
    return isinstance(row.get("excerpt"), str) and row["excerpt"] == excerpt


def _reconstruct_material(current: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path, tempfile.TemporaryDirectory[str]]:
    context = tempfile.TemporaryDirectory(prefix="okf-relevance-verify-")
    root = Path(context.name)
    baseline = root / "baseline"
    clone = subprocess.run(
        ["git", "clone", "--quiet", "--no-checkout", "--shared", str(current), str(baseline)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if clone.returncode:
        context.cleanup()
        raise ValueError(f"baseline clone failed: {clone.stderr.strip()}")
    checkout = subprocess.run(
        ["git", "-C", str(baseline), "checkout", "--quiet", "--detach", BASELINE_REVISION],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if checkout.returncode:
        context.cleanup()
        raise ValueError(f"baseline checkout failed: {checkout.stderr.strip()}")
    if _git_output(baseline, "rev-parse", "HEAD") != BASELINE_REVISION:
        context.cleanup()
        raise ValueError("reconstructed baseline revision mismatch")
    if _git_output(baseline, "status", "--porcelain"):
        context.cleanup()
        raise ValueError("reconstructed baseline is not clean")
    bundle = root / "bundle"
    fixture_file = root / "queries.json"
    generate_fixture(bundle)
    fixture_file.write_bytes(_stable_bytes(QUERY_FIXTURE))
    before = run_checkout(baseline, bundle, fixture_file, worktree_label=False)
    if _git_output(baseline, "rev-parse", "HEAD") != BASELINE_REVISION or _git_output(
            baseline, "status", "--porcelain"):
        context.cleanup()
        raise ValueError("baseline adapter changed reconstructed checkout identity")
    live = run_checkout(current, bundle, fixture_file, worktree_label=True)
    portable = run_portable(current, bundle, fixture_file, root / "portable-site")
    return before, live, portable, bundle, context


def _runtime_contract_without_revision(value: dict[str, Any]) -> dict[str, Any]:
    contract = copy.deepcopy(value)
    contract.pop("revision", None)
    return contract


def _verify_parity(section: Any, *, context: bool) -> None:
    if not isinstance(section, dict) or set(section) != ({"after", "status", "semantics", "per_query"}
                                                       if context else
                                                       {"before", "after", "status", "semantics", "per_query"}):
        raise ValueError("parity fields mismatch")
    if section["status"] != "measured" or not isinstance(section["per_query"], list):
        raise ValueError("parity must be measured")
    expected_semantics = ("Complete pack fields (numeric values compared as JSON numbers), ordered item IDs, and canonical Markdown bytes."
                          if context else
                          "Fraction of queries with the same complete ordered top-k concepts in portable JavaScript and live Python retrieval.")
    if section["semantics"] != expected_semantics:
        raise ValueError("parity semantics mismatch")
    rows = section["per_query"]
    if len(rows) != len(QUERY_FIXTURE) or [row.get("id") for row in rows] != [case["id"] for case in QUERY_FIXTURE]:
        raise ValueError("parity query IDs mismatch")
    matches = []
    for row in rows:
        if context:
            expected_keys = {"id", "field_match", "markdown_byte_match", "live_item_ids",
                             "portable_item_ids", "match"}
            if set(row) != expected_keys:
                raise ValueError("context parity row fields mismatch")
            expected = (row["field_match"] is True and row["markdown_byte_match"] is True and
                        row["live_item_ids"] == row["portable_item_ids"])
        else:
            expected_keys = {"id", "live_top", "portable_top", "live_order", "portable_order",
                             "contract_match", "match"}
            if set(row) != expected_keys:
                raise ValueError("search parity row fields mismatch")
            expected = row["contract_match"] is True and row["live_order"] == row["portable_order"]
            if row["live_top"] != (row["live_order"] or [None])[0] or row["portable_top"] != (row["portable_order"] or [None])[0]:
                raise ValueError("parity top result mismatch")
        if type(row["match"]) is not bool or row["match"] != expected:
            raise ValueError("parity row result mismatch")
        matches.append(float(row["match"]))
    if not _equivalent_contract(section["after"], _mean(matches)):
        raise ValueError("parity aggregate mismatch")
    if not context and section["before"] is not None:
        raise ValueError("portable baseline parity must be null")


def verify_report(report: Any, current: Path) -> None:
    """Independently verify a stored report against its selected checkout."""
    try:
        if not isinstance(report, dict) or set(report) != {
            "schema_version", "release_slice", "revisions", "provenance", "fixture",
            "metric_definitions", "per_query", "verification_material", "aggregate", "caveats", "command",
        }:
            raise ValueError("top-level fields mismatch")
        if report["schema_version"] != SCHEMA_VERSION:
            raise ValueError("schema version mismatch")
        expected_command = ("scripts/benchmark_retrieval_relevance.py "
                            f"--baseline-checkout <clean-baseline@{BASELINE_REVISION}> "
                            "--current-checkout <current-worktree> "
                            "--output docs/design/gold-standard-relevance-2026.json")
        expected_definitions = {
            "precision_at_3": "Relevant concepts in the first three results divided by three.",
            "mrr": "Reciprocal rank of the first expected concept, or zero when absent.",
            "ndcg_at_3": "Binary normalized discounted cumulative gain over expected concepts.",
            "expected_section_hit_rate": "Queries whose expected concept has evidence under the expected heading; null when the runtime emits no section evidence.",
            "citation_validity": "Expected-result citations whose path, physical lines, and excerpt agree with the generated Markdown; null when unavailable.",
            "hard_negative_query_rate_at_3": "Fraction of queries with a declared distractor in the first three; lower is better.",
            "portable_live_parity": "Fraction of queries with the same complete ordered top-k concepts in checked-in portable JavaScript and live Python retrieval; null when unavailable.",
            "portable_context_parity": "Fraction of queries with equivalent complete Context Pack fields, ordered item IDs, and byte-identical canonical Markdown.",
        }
        expected_caveats = [
            "The fixture is synthetic and intentionally small; it is a regression proof, not a production traffic estimate.",
            "Semantic mode is dependency-free semantic-lite fuzzy matching, not embeddings.",
            "Baseline section/citation metrics are null when the baseline API cannot emit passage evidence.",
            "No improvement is inferred from revision labels; each metric must be read independently.",
            "Portable/live parity is measured only when Node can execute the checked-in portable runtime; otherwise it is null.",
        ]
        if (report["release_slice"] != "A+B retrieval relevance" or report["command"] != expected_command or
                report["metric_definitions"] != expected_definitions or report["caveats"] != expected_caveats):
            raise ValueError("report metadata mismatch")
        expected_fixture = {"sha256": fixture_digest(), "concept_count": len(CONCEPT_FIXTURE),
                            "queries": list(QUERY_FIXTURE)}
        if not _equivalent_contract(report["fixture"], expected_fixture):
            raise ValueError("fixture mismatch")
        _verify_source_provenance(report["provenance"], current.resolve())
        if not isinstance(report["revisions"], dict) or set(report["revisions"]) != {"before", "after"}:
            raise ValueError("revision fields mismatch")
        if not isinstance(report["revisions"]["after"], str) or not report["revisions"]["after"]:
            raise ValueError("informational current revision is malformed")
        if report["revisions"]["before"] != BASELINE_REVISION:
            raise ValueError("baseline revision mismatch")
        if not isinstance(report["per_query"], dict) or set(report["per_query"]) != {"before", "after"}:
            raise ValueError("per-query sides mismatch")
        if not isinstance(report["verification_material"], dict) or set(report["verification_material"]) != {
                "baseline", "live_after", "portable"}:
            raise ValueError("verification material fields mismatch")
        baseline, live, portable, fixture_root, fixture_context = _reconstruct_material(current.resolve())
        try:
            if not _equivalent_contract(report["verification_material"]["baseline"], baseline):
                raise ValueError("baseline verification material mismatch")
            if not _equivalent_contract(
                    _runtime_contract_without_revision(report["verification_material"]["live_after"]),
                    _runtime_contract_without_revision(live)):
                raise ValueError("live verification material mismatch")
            if not _equivalent_contract(report["verification_material"]["portable"], portable):
                raise ValueError("portable verification material mismatch")
            canonical_live = copy.deepcopy(live)
            for query in canonical_live["queries"]:
                for row in query["results"]:
                    row["citation_valid"] = _physical_citation_valid(row, fixture_root)
            canonical_after = _score_side(canonical_live)
            canonical_before = _score_side(baseline)
            if not _equivalent_contract(report["per_query"]["before"], canonical_before["queries"]):
                raise ValueError("baseline per-query reconstruction mismatch")
            if not _equivalent_contract(report["per_query"]["after"], canonical_after["queries"]):
                raise ValueError("current per-query reconstruction mismatch")
            before_metrics = canonical_before["metrics"]
            after_metrics = canonical_after["metrics"]
            expected_search_parity = _portable_parity(canonical_after, portable)
            expected_context_parity = _portable_context_parity(canonical_live, portable)
        finally:
            fixture_context.cleanup()
        aggregate = report["aggregate"]
        if not isinstance(aggregate, dict) or set(aggregate) != {
            "before", "after", "portable_live_parity", "portable_context_parity",
        }:
            raise ValueError("aggregate fields mismatch")
        if not _equivalent_contract(aggregate["before"], before_metrics) or not _equivalent_contract(
                aggregate["after"], after_metrics):
            raise ValueError("aggregate metrics mismatch")
        if not _equivalent_contract(aggregate["portable_live_parity"], expected_search_parity):
            raise ValueError("search parity reconstruction mismatch")
        if not _equivalent_contract(aggregate["portable_context_parity"], expected_context_parity):
            raise ValueError("context parity reconstruction mismatch")
        _verify_parity(aggregate["portable_live_parity"], context=False)
        _verify_parity(aggregate["portable_context_parity"], context=True)
        if report["provenance"]["fixture_sha256"] != report["fixture"]["sha256"]:
            raise ValueError("fixture provenance mismatch")
        enforce_thresholds(report)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"report verification failed: {exc}") from exc


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            Path(name).unlink()
        except FileNotFoundError:
            pass


def validate_work_dir(path: Path, checkouts: tuple[Path, ...]) -> Path:
    """Require an empty external workspace; never generate into a checkout."""
    work = path.expanduser().resolve()
    for checkout in checkouts:
        root = checkout.resolve()
        try:
            work.relative_to(root)
        except ValueError:
            continue
        try:
            work.relative_to(root / ".aic" / "tmp")
        except ValueError:
            pass
        else:
            continue
        raise ValueError(f"--work-dir must be outside checkout {root}")
    if work.exists():
        if not work.is_dir() or any(work.iterdir()):
            raise ValueError("--work-dir must be a new or empty directory")
    else:
        work.mkdir(parents=True)
    return work


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-checkout", type=Path)
    parser.add_argument("--current-checkout", default=REPO_ROOT, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify-report", type=Path)
    parser.add_argument("--work-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    current = args.current_checkout.resolve()
    if args.verify_report:
        if args.baseline_checkout or args.output or args.work_dir:
            raise SystemExit("benchmark error: --verify-report cannot be combined with generation options")
        try:
            report = json.loads(args.verify_report.read_text(encoding="utf-8"))
            verify_report(report, current)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise SystemExit(f"benchmark error: {exc}") from exc
        print(args.verify_report.resolve())
        return 0
    if args.baseline_checkout is None or args.output is None:
        raise SystemExit("benchmark error: --baseline-checkout and --output are required for generation")
    baseline = args.baseline_checkout.resolve()
    for checkout in (baseline, current):
        if not (checkout / "scripts" / "okf_loom" / "search.py").is_file():
            raise SystemExit(f"benchmark error: not an okf-loom checkout: {checkout}")
    baseline_revision = _git_output(baseline, "rev-parse", "HEAD")
    if baseline_revision != BASELINE_REVISION:
        raise SystemExit(f"benchmark error: baseline must be exact revision {BASELINE_REVISION}")
    if _git_output(baseline, "status", "--porcelain"):
        raise SystemExit("benchmark error: baseline checkout must be clean")
    command = ("scripts/benchmark_retrieval_relevance.py "
               f"--baseline-checkout <clean-baseline@{BASELINE_REVISION}> "
               "--current-checkout <current-worktree> "
               "--output docs/design/gold-standard-relevance-2026.json")
    if args.work_dir:
        try:
            work = validate_work_dir(args.work_dir, (baseline, current))
        except ValueError as exc:
            raise SystemExit(f"benchmark error: {exc}") from exc
        context = None
    else:
        context = tempfile.TemporaryDirectory(prefix="okf-relevance-")
        work = Path(context.name)
    try:
        bundle = work / "bundle"
        fixture_file = work / "queries.json"
        generate_fixture(bundle)
        fixture_file.write_bytes(_stable_bytes(QUERY_FIXTURE))
        before = run_checkout(baseline, bundle, fixture_file, worktree_label=False)
        after = run_checkout(current, bundle, fixture_file, worktree_label=True)
        portable = run_portable(current, bundle, fixture_file, work / "portable-site")
        report = build_report(before, after, command=command, portable_raw=portable,
                              provenance=_source_provenance(current))
        try:
            verify_report(report, current)
        except ValueError as exc:
            raise SystemExit(f"benchmark error: {exc}") from exc
        write_report(args.output.resolve(), report)
    finally:
        if context is not None:
            context.cleanup()
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
