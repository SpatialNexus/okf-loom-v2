from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from okf_loom.proposal_review.canonical import dumps_canonical
from okf_loom.proposal_runtime_identity import (
    OPERATION_RUNTIME_ID, SERIALIZER_ID, SOURCE_MANIFESTS, VALIDATOR_ID,
    VALIDATOR_RULE_CODES,
)
from review_runtime_identities import manifest_digest, review_candidates


def test_manifest_digest_keeps_canonical_path_order_and_exact_bytes(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"abc")
    (tmp_path / "b.py").write_bytes(b"")
    # Fixed SHA-256 vectors bind the pre-existing canonical algorithm, not inventory.
    entries = [
        ["a.py", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"],
        ["b.py", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"],
    ]
    expected = hashlib.sha256(dumps_canonical(entries)).hexdigest()
    assert manifest_digest(tmp_path, ("a.py", "b.py")) == expected
    assert manifest_digest(tmp_path, ("b.py", "a.py")) != expected
    (tmp_path / "a.py").write_bytes(b"abc\r\n")
    crlf = manifest_digest(tmp_path, ("a.py", "b.py"))
    (tmp_path / "a.py").write_bytes(b"abc\n")
    assert manifest_digest(tmp_path, ("a.py", "b.py")) != crlf


@pytest.mark.parametrize(("kind", "dependency"), [
    ("validator", "scripts/okf_loom/_yaml_compat.py"),
    ("serializer", "scripts/okf_loom/_yaml_compat.py"),
    ("operation", "scripts/okf_loom/session_authority.py"),
    ("validator", "scripts/okf_loom/__init__.py"),
    ("validator", "scripts/okf_loom/ignore.py"),
    ("validator", "scripts/okf_loom/documents.py"),
    ("validator", "scripts/okf_loom/navigation.py"),
    ("validator", "scripts/okf_loom/markdown_blocks.py"),
    ("validator", "scripts/okf_loom/viewer/markdown.py"),
    ("validator", "scripts/okf_loom/metadata.py"),
    ("validator", "scripts/okf_loom/proposal_review/canonical.py"),
    ("serializer", "scripts/okf_loom/metadata.py"),
    ("operation", "scripts/okf_loom/proposal_review/canonical.py"),
    ("operation", "scripts/okf_loom/proposal_review/validation.py"),
])
def test_dependency_byte_change_invalidates_reviewed_digest(
    tmp_path: Path, kind: str, dependency: str,
) -> None:
    # Disposable source bytes: no edits to the checkout, and no second inventory.
    for relative in {*SOURCE_MANIFESTS[kind], dependency}:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original source\n")
    reviewed = manifest_digest(tmp_path, SOURCE_MANIFESTS[kind])
    (tmp_path / dependency).write_bytes(b"changed source\n")
    assert manifest_digest(tmp_path, SOURCE_MANIFESTS[kind]) != reviewed


def test_candidate_command_is_deterministic_and_read_only(tmp_path: Path) -> None:
    from okf_loom.proposal_snapshot import DEFAULT_RUNTIME_IDENTITIES, capture_bundle

    sources = {path for paths in SOURCE_MANIFESTS.values() for path in paths}
    for relative in sources:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture source\r\n")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    approved = {
        "validator": DEFAULT_RUNTIME_IDENTITIES.validator_id,
        "serializer": DEFAULT_RUNTIME_IDENTITIES.serializer_id,
        "operation": DEFAULT_RUNTIME_IDENTITIES.operation_runtime_id,
    }
    command = [sys.executable, "-B", str(Path(__file__).resolve().parents[1] /
               "scripts/review_runtime_identities.py"), "--source-root", str(tmp_path)]
    first = subprocess.run(command, cwd=tmp_path, capture_output=True, check=False)
    assert first.returncode == 0, first.stderr.decode()
    second = subprocess.run(command, cwd=tmp_path, capture_output=True, check=False)
    assert second.returncode == 0, second.stderr.decode()
    assert first.stdout == second.stdout
    candidates = json.loads(first.stdout)
    assert set(candidates) == set(SOURCE_MANIFESTS)
    for kind, row in candidates.items():
        assert row == {
            "current": approved[kind],
            "candidate": approved[kind].rsplit(":", 1)[0] + ":" +
                         manifest_digest(tmp_path, SOURCE_MANIFESTS[kind]),
        }
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after
    # In-process derivation must not install candidates into production capture.
    assert review_candidates(tmp_path) == candidates
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "a.md").write_text("---\ntype: Note\n---\nBody\n")
    captured = capture_bundle(bundle, captured_at="2026-09-08T00:00:00.000Z")
    assert (captured.snapshot.validator_id, captured.snapshot.serializer_id,
            captured.snapshot.operation_runtime_id) == (VALIDATOR_ID, SERIALIZER_ID, OPERATION_RUNTIME_ID)

    dependency = tmp_path / "scripts/okf_loom/_yaml_compat.py"
    dependency.write_bytes(b"fixture source\n")
    changed = subprocess.run(command, cwd=tmp_path, capture_output=True, check=False)
    assert changed.returncode == 0, changed.stderr.decode()
    changed_candidates = json.loads(changed.stdout)
    for kind in ("validator", "serializer"):
        assert changed_candidates[kind]["current"] == approved[kind]
        assert changed_candidates[kind]["candidate"] != candidates[kind]["candidate"]

    # A missing listed source must fail, not print partial or fabricated candidates.
    (tmp_path / min(sources)).unlink()
    missing = subprocess.run(command, cwd=tmp_path, capture_output=True, check=False)
    assert missing.returncode != 0
    assert missing.stdout == b""


def test_reviewed_validator_rule_codes_match_registry() -> None:
    from okf_loom.validate import VALIDATOR_RULE_REGISTRY
    assert tuple(sorted(VALIDATOR_RULE_REGISTRY)) == VALIDATOR_RULE_CODES


@pytest.mark.parametrize("kind", SOURCE_MANIFESTS)
def test_release_runtime_identities_match_reviewed_source_manifests(kind: str) -> None:
    root = Path(__file__).resolve().parents[1]
    row = review_candidates(root)[kind]
    assert row["current"] == row["candidate"], (
        f"{kind} output source changed without a reviewed runtime identity bump; "
        "print candidates with python -B scripts/review_runtime_identities.py"
    )
