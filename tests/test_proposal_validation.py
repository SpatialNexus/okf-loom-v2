from __future__ import annotations

import pytest

from okf_loom.proposal_review.canonical import canonical_digest
from okf_loom.proposal_review.errors import SchemaError, ValidationProfileError
from okf_loom.proposal_review.ports import FrozenValidationReport, FrozenValidatorFinding
from okf_loom.proposal_review.schemas import Validation
from okf_loom.proposal_review.validation import build_validation


class Validator:
    def installed_profiles(self): return ("producer",)
    def validate(self, snapshot, *, profile):
        findings = () if b"bad" not in next(iter(snapshot.values())) else (
            FrozenValidatorFinding("bad", "error", "docs/a", 1, "bad content"),)
        return FrozenValidationReport(findings, not findings)


CONTEXT = "sha256:" + "4" * 64


def test_validation_normalization_delta_status_and_digest():
    result = build_validation(
        {"docs/a": b"ok"}, {"docs/a": b"bad"}, Validator(),
        validation_context_digest=CONTEXT,
    )
    assert result.status == "fail" and len(result.added) == 1 and not result.removed
    assert result.validation_context_digest == CONTEXT
    assert result.digest == canonical_digest({k: v for k, v in result.to_mapping().items() if k != "digest"})


def test_unknown_profile_and_mixed_bundle_concept_finding_order_delta():
    with pytest.raises(ValidationProfileError):
        build_validation({"docs/a": b"ok"}, {"docs/a": b"ok"}, Validator(),
                         validation_context_digest=CONTEXT, profile="unknown")

    class MixedValidator(Validator):
        def validate(self, snapshot, *, profile):
            values = [FrozenValidatorFinding("bundle", "warning", None, 0, "bundle", scope="bundle")]
            if b"bad" in next(iter(snapshot.values())):
                values.insert(0, FrozenValidatorFinding("concept", "error", "docs/a", 2, "bad"))
            return FrozenValidationReport(tuple(values), not any(v.severity == "error" for v in values))

    result = build_validation(
        {"docs/a": b"ok"}, {"docs/a": b"bad"}, MixedValidator(),
        validation_context_digest=CONTEXT,
    )
    assert [f.concept for f in result.after_findings] == [None, "docs/a"]
    assert [f.concept for f in result.unchanged] == [None]
    assert [f.concept for f in result.added] == ["docs/a"]
    assert result.after_findings[0].line == 0
    malformed = result.to_mapping()
    malformed["after_findings"] = list(reversed(malformed["after_findings"]))
    malformed["digest"] = canonical_digest({k: v for k, v in malformed.items() if k != "digest"})
    with pytest.raises(SchemaError):
        Validation.from_mapping(malformed)
