"""Pure frozen-snapshot validation normalization and delta construction."""
from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from . import ids
from .canonical import canonical_digest
from .errors import SchemaError, ValidationProfileError
from .ports import FrozenSnapshotValidator, FrozenValidatorFinding
from .schemas import Validation, ValidationFinding

DEFAULT_PROFILE = "producer"
_SEVERITIES = frozenset({"error", "warning", "info"})


def _normalize_finding(raw: FrozenValidatorFinding) -> ValidationFinding:
    non_concept_scopes = frozenset({"bundle", "reserved_file", "config", "capability"})
    if raw.scope == "concept" and raw.concept is None:
        raise SchemaError("concept-scoped validator finding requires a concept")
    if raw.scope in non_concept_scopes and raw.concept is not None:
        raise SchemaError(f"{raw.scope}-scoped validator finding must not claim a concept")
    if raw.scope not in non_concept_scopes | {"concept"}:
        raise SchemaError(f"validator emitted unsupported finding scope {raw.scope!r}")
    if raw.severity not in _SEVERITIES:
        raise SchemaError(f"validator emitted unsupported severity {raw.severity!r}")
    body = {"code": raw.code, "severity": raw.severity, "concept": raw.concept,
            "line": raw.line, "message": raw.message}
    expected = canonical_digest(body)
    if raw.fingerprint is not None and raw.fingerprint != expected:
        raise SchemaError("validator finding fingerprint does not match normalized content")
    return ValidationFinding.from_mapping({**body, "fingerprint": expected})


def _normalized(findings: tuple[FrozenValidatorFinding, ...]) -> tuple[ValidationFinding, ...]:
    values = [_normalize_finding(item) for item in findings]
    key = lambda f: (0 if f.concept is None else 1, f.concept or "", f.line, f.severity, f.code, f.fingerprint)
    values.sort(key=key)
    fingerprints: set[str] = set()
    for finding in values:
        if finding.fingerprint in fingerprints:
            raise SchemaError(f"validator emitted duplicate finding {finding.fingerprint}")
        fingerprints.add(finding.fingerprint)
    return tuple(values)


def build_validation(
    before: Mapping[str, bytes],
    after: Mapping[str, bytes],
    validator: FrozenSnapshotValidator,
    *,
    validation_context_digest: str,
    profile: str = DEFAULT_PROFILE,
) -> Validation:
    ids.validate_digest(validation_context_digest, name="validation_context_digest")
    installed = tuple(validator.installed_profiles())
    if profile not in installed:
        raise ValidationProfileError(f"validation profile {profile!r} is not installed")
    before_report = validator.validate(MappingProxyType(dict(before)), profile=profile)
    after_report = validator.validate(MappingProxyType(dict(after)), profile=profile)
    before_findings = _normalized(before_report.findings)
    after_findings = _normalized(after_report.findings)
    before_by = {finding.fingerprint: finding for finding in before_findings}
    after_by = {finding.fingerprint: finding for finding in after_findings}
    added = tuple(f for f in after_findings if f.fingerprint not in before_by)
    removed = tuple(f for f in before_findings if f.fingerprint not in after_by)
    unchanged = tuple(f for f in after_findings if f.fingerprint in before_by)
    body = {
        "schema": 1, "profile": profile, "validation_context_digest": validation_context_digest,
        "before_findings": [f.to_mapping() for f in before_findings],
        "after_findings": [f.to_mapping() for f in after_findings],
        "added": [f.to_mapping() for f in added], "removed": [f.to_mapping() for f in removed],
        "unchanged": [f.to_mapping() for f in unchanged],
        "status": "pass" if after_report.passed else "fail",
    }
    return Validation.from_mapping({**body, "digest": canonical_digest(body)})


__all__ = ["DEFAULT_PROFILE", "build_validation"]
