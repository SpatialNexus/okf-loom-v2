from __future__ import annotations

from pathlib import Path

import pytest

from okf_loom.proposal_review.errors import ValidationProfileError
from okf_loom.proposal_snapshot import (ProductionFrozenSnapshotValidator,
                                        capture_bundle)


NOW = "2026-07-14T00:00:00.000Z"


def test_frozen_validation_survives_deleted_root_and_performs_no_path_io(tmp_path: Path, monkeypatch) -> None:
    import os
    (tmp_path / "a.md").write_text(
        "---\ntype: Note\ntitle: A\ndescription: d\ntags: [x]\ntimestamp: '2026-01-01'\n---\n\n![x](../outside.png)\n"
    )
    captured = capture_bundle(tmp_path, captured_at=NOW, profile="spec")
    validator = ProductionFrozenSnapshotValidator(captured.facts)
    before = validator.validate(captured.concept_bytes, profile="spec")
    for child in tmp_path.iterdir():
        child.unlink()
    tmp_path.rmdir()

    def denied(*args, **kwargs):
        raise AssertionError("live filesystem access after capture")

    monkeypatch.setattr(Path, "resolve", denied)
    monkeypatch.setattr(Path, "is_file", denied)
    monkeypatch.setattr(Path, "exists", denied)
    for name in ("open", "stat", "listdir", "scandir", "readlink"):
        monkeypatch.setattr(os, name, denied)
    after = validator.validate(captured.concept_bytes, profile="spec")
    assert before == after
    assert any(f.code == "asset.missing" and f.concept == "a" for f in after.findings)
    with pytest.raises(ValidationProfileError):
        validator.validate(captured.concept_bytes, profile="unknown")


def test_bundle_scoped_findings_have_null_concept(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("---\ntype: Note\n---\n")
    captured = capture_bundle(tmp_path, captured_at=NOW, profile="spec")
    report = ProductionFrozenSnapshotValidator(captured.facts).validate(
        captured.concept_bytes, profile="spec"
    )
    version = next(f for f in report.findings if f.code == "spec.version_undeclared")
    assert version.concept is None and version.line == 0 and version.scope == "bundle"


@pytest.mark.parametrize("dotfile_exists", [False, True])
def test_root_dotfile_asset_live_frozen_parity(tmp_path: Path, dotfile_exists: bool) -> None:
    from okf_loom.model import Bundle
    from okf_loom.validate import validate_bundle_with_profile

    (tmp_path / "a.md").write_text(
        "---\ntype: Note\ntitle: A\ndescription: d\ntags: [x]\n"
        "timestamp: '2026-01-01'\n---\n\n![secret](.secret.png)\n"
    )
    # This similarly named non-dotfile must never satisfy the dotfile target.
    (tmp_path / "secret.png").write_bytes(b"public")
    if dotfile_exists:
        (tmp_path / ".secret.png").write_bytes(b"secret")

    captured = capture_bundle(tmp_path, captured_at=NOW, profile="spec")
    frozen = ProductionFrozenSnapshotValidator(captured.facts).validate(
        captured.concept_bytes, profile="spec"
    )
    live = validate_bundle_with_profile(Bundle.load(tmp_path), profile="spec")
    frozen_missing = [(f.code, f.concept, f.line, f.message)
                      for f in frozen.findings if f.code == "asset.missing"]
    live_missing = [(f.code,
                     "/".join(f.concept_id) if f.concept_id else None,
                     f.line or 0, f.message)
                    for f in live.findings if f.code == "asset.missing"]
    assert frozen_missing == live_missing
    assert bool(frozen_missing) is (not dotfile_exists)


def test_frozen_policy_is_sole_normalization_authority(tmp_path: Path, monkeypatch) -> None:
    import okf_loom.proposal_snapshot as adapter
    import okf_loom.validate as live

    (tmp_path / "a.md").write_text("---\ntype: Note\n---\n")
    captured = capture_bundle(tmp_path, captured_at=NOW, profile="producer")
    validator = ProductionFrozenSnapshotValidator(captured.facts)
    expected = validator.validate(captured.concept_bytes, profile="producer")
    monkeypatch.setattr(live, "VALIDATOR_RULE_REGISTRY", {})
    monkeypatch.setitem(live._PROFILE_REMAPS["producer"], "concept.missing_type", live.Severity.INFO)
    monkeypatch.setattr(adapter, "VALIDATOR_RULE_REGISTRY", {})
    actual = validator.validate(captured.concept_bytes, profile="producer")
    assert actual == expected


def test_rule_registry_drift_requires_runtime_identity_bump(tmp_path: Path, monkeypatch) -> None:
    import okf_loom.proposal_snapshot as adapter
    (tmp_path / "a.md").write_text("---\ntype: Note\n---\n")
    monkeypatch.setattr(adapter, "VALIDATOR_RULE_REGISTRY", {})
    with pytest.raises(adapter.SnapshotCaptureError) as error:
        capture_bundle(tmp_path, captured_at=NOW, profile="spec")
    assert error.value.code == "runtime_identity_mismatch"


def test_concept_scoped_finding_without_owner_fails_closed(tmp_path: Path, monkeypatch) -> None:
    import okf_loom.proposal_snapshot as adapter
    from okf_loom.validate import Finding, Severity, ValidationReport
    from okf_loom.proposal_review.errors import SchemaError

    (tmp_path / "a.md").write_text("---\ntype: Note\n---\n")
    captured = capture_bundle(tmp_path, captured_at=NOW, profile="spec")
    validator = ProductionFrozenSnapshotValidator(captured.facts)

    def malformed(bundle, *, file_facts, _normalize_rules):
        return ValidationReport(bundle.root, findings=[Finding(
            "concept.missing_type", Severity.ERROR, "missing owner"
        )])

    monkeypatch.setattr(adapter, "validate_bundle", malformed)
    with pytest.raises(SchemaError, match="no canonical concept owner"):
        validator.validate(captured.concept_bytes, profile="spec")

    def wrong_path(bundle, *, file_facts, _normalize_rules):
        return ValidationReport(bundle.root, findings=[Finding(
            "concept.missing_type", Severity.ERROR, "wrong path",
            path=bundle.root / "other.md", concept_id=("a",),
        )])

    monkeypatch.setattr(adapter, "validate_bundle", wrong_path)
    with pytest.raises(SchemaError, match="path and owner disagree"):
        validator.validate(captured.concept_bytes, profile="spec")


@pytest.mark.parametrize("profile", ["spec", "producer", "loose"])
def test_live_and_frozen_finding_parity_matrix(tmp_path: Path, profile: str) -> None:
    from okf_loom.model import Bundle
    from okf_loom.validate import VALIDATOR_RULE_REGISTRY, validate_bundle_with_profile
    from okf_loom.paths import concept_id_to_str
    from okf_loom.proposal_review.canonical import canonical_digest

    (tmp_path / "index.md").write_text(
        "---\nokf_version: '0.1'\nokf_extensions: [okf.cap.entities]\n---\n# Root\n"
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text("# Docs\n")
    (docs / "a.md").write_text(
        "---\ntype: Note\ntitle: A\ndescription: d\ntags: [X]\ntimestamp: bad\n---\n\n"
        "[missing](missing.md)\n![local](asset.png)\n![escape](../../outside.png)\n"
    )
    (docs / "asset.png").write_bytes(b"asset")
    (docs / "broken.md").write_text("---\n: bad yaml\n---\n")
    (tmp_path / "log.md").write_text("# not-a-date\n")
    captured = capture_bundle(tmp_path, captured_at=NOW, profile=profile)
    frozen = ProductionFrozenSnapshotValidator(captured.facts).validate(
        captured.concept_bytes, profile=profile
    )
    live = validate_bundle_with_profile(Bundle.load(tmp_path), profile=profile)
    expected = []
    for finding in live.findings:
        rule = VALIDATOR_RULE_REGISTRY[finding.code]
        concept = concept_id_to_str(finding.concept_id) if finding.concept_id else None
        if rule.scope == "concept" and concept is None and finding.path is not None:
            rel = finding.path.relative_to(tmp_path).as_posix()
            if rel.endswith(".md"):
                concept = rel[:-3]
        body = {"code": finding.code, "severity": finding.severity.value,
                "concept": concept, "line": finding.line or 0,
                "message": finding.message}
        expected.append((finding.code, finding.severity.value, concept,
                         finding.line or 0, finding.message, rule.scope,
                         canonical_digest(body)))
    actual = [(f.code, f.severity, f.concept, f.line, f.message, f.scope,
               f.fingerprint) for f in frozen.findings]
    key = lambda value: (0 if value[2] is None else 1, value[2] or "",
                         value[3], value[1], value[0], value[6])
    assert actual == sorted(expected, key=key)
    assert frozen.passed == live.ok
