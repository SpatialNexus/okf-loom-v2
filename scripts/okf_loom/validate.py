"""Validator implementing upstream OKF 0.2 §11 conformance plus soft warnings.

The base document checks under upstream OKF 0.2 §11 are:
    1. Every non-reserved .md file has a parseable YAML frontmatter block.
    2. Every frontmatter block has a non-empty `type` field.
    3. Reserved filenames (`index.md`, `log.md`) follow upstream §8/§9 structure.

All other constraints are SOFT guidance; consumers MUST NOT reject a bundle
for them (upstream OKF 0.2 §11). This validator therefore splits findings
into `errors` (conformance failures) and `warnings` (soft issues), and the
CLI exposes a `--strict` flag that promotes warnings to errors for CI use.

Validation profiles (current spec §5)
-----------------------------
Named profiles remap finding *codes* to different severities. The canonical
profile-aware entry point is :func:`validate_bundle_with_profile`; the bare
:func:`validate_bundle` always uses the ``spec`` baseline. The selected
profile is recorded on the :class:`ValidationReport` and surfaced in
``as_dict()`` under the ``"profile"`` key (so library callers see it without
the CLI post-hoc injecting it).

Code → resulting severity by profile (only the remapped codes are shown;
every other code keeps its ``spec`` severity under all profiles):

    ===========================  =========  =========  ========
    Finding code                 spec       producer   loose
    ===========================  =========  =========  ========
    concept.missing_type         ERROR      ERROR      WARNING
    concept.missing_recommended_keys  WARNING  ERROR   WARNING
    ===========================  =========  =========  ========

Orthogonal modifiers (compose with ANY profile):

    --fail-on-broken-links   promotes ``link.broken`` → ERROR.
    --strict                 promotes every remaining WARNING → ERROR
                             (applied AFTER the profile remap, so e.g.
                             ``loose`` + ``--strict`` re-promotes the
                             demoted ``concept.missing_type`` back to ERROR).

Note on ``--checks``: when an explicit ``--checks`` subset is given on the
CLI, :func:`validate_bundle` is called directly and profile remapping is
NOT applied (profiles compose over the full check set). In that path the
JSON ``profile`` reflects the requested name for traceability but no
severity remap runs; see ``cli.cmd_validate`` for the reconciliation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import posixpath
from types import MappingProxyType
from typing import Protocol

from . import (
    RECOMMENDED_FRONTMATTER_KEYS,
    SPEC_VERSION,
)
from .model import Bundle, Concept, IndexFile
from .paths import ConceptId, concept_id_from_str, concept_id_to_str


class Severity(str, Enum):
    ERROR = "error"      # upstream OKF 0.2 §11 conformance failure
    WARNING = "warning"  # soft issue (SPEC-mandated tolerance)
    INFO = "info"        # advisory (e.g. unused capability declarations)


@dataclass(frozen=True)
class ValidatorRule:
    code: str
    base_severity: Severity
    scope: str
    profile_overrides: MappingProxyType

    def effective_severity(self, profile: str, *, fail_on_broken_links: bool = False) -> Severity:
        if profile not in _PROFILE_REMAPS:
            raise ValueError(f"Unknown validation profile {profile!r}; expected one of {sorted(_PROFILE_REMAPS)}.")
        severity = self.profile_overrides.get(profile, self.base_severity)
        if fail_on_broken_links and self.code in _BROKEN_LINK_CODES:
            severity = Severity.ERROR
        return severity


class ValidationFileFacts(Protocol):
    """Read-only existence oracle used by filesystem-sensitive checks."""
    def is_file(self, relative_path: str) -> bool: ...
    def lookup(self, source_relative_path: Path, target: str) -> tuple[bool, bool]: ...


@dataclass(frozen=True)
class LiveValidationFileFacts:
    root: Path

    def is_file(self, relative_path: str) -> bool:
        try:
            return (self.root / relative_path).is_file()
        except OSError:
            return False

    def lookup(self, source_relative_path: Path, target: str) -> tuple[bool, bool]:
        if target.startswith("/"):
            candidate = self.root / target.lstrip("/")
            out_of_bundle = False
        else:
            candidate = self.root / source_relative_path.parent / target
            try:
                candidate.resolve().relative_to(self.root.resolve())
                out_of_bundle = False
            except (ValueError, OSError, RuntimeError):
                out_of_bundle = True
        try:
            return candidate.is_file(), out_of_bundle
        except OSError:
            return False, out_of_bundle


def _bundle_relative_target(source_rel: Path, target: str) -> tuple[str | None, bool]:
    """Resolve a URL-decoded target lexically; never consult the host."""
    value = target
    if value.startswith("/"):
        candidate = value.lstrip("/")
    else:
        candidate = posixpath.join(source_rel.parent.as_posix(), value)
    normalized = posixpath.normpath(candidate)
    escaped = normalized == ".." or normalized.startswith("../") or normalized.startswith("/")
    return (None if escaped else normalized.lstrip("./"), escaped)


def _lookup_fact(facts: ValidationFileFacts, source_rel: Path,
                 target: str) -> tuple[bool, bool]:
    lookup = getattr(facts, "lookup", None)
    if lookup is not None:
        return lookup(source_rel, target)
    relative, escaped = _bundle_relative_target(source_rel, target)
    return bool(relative is not None and facts.is_file(relative)), escaped


@dataclass(frozen=True)
class Finding:
    """One validator finding."""

    code: str
    severity: Severity
    message: str
    path: Path | None = None
    line: int | None = None
    concept_id: ConceptId | None = None
    # Optional structured detail (for machine consumers).
    detail: dict | None = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "path": str(self.path) if self.path else None,
            "line": self.line,
            "concept_id": (
                concept_id_to_str(self.concept_id) if self.concept_id else None
            ),
            "detail": self.detail,
        }


@dataclass
class ValidationReport:
    bundle_root: Path
    findings: list[Finding] = field(default_factory=list)
    spec_version: str = SPEC_VERSION
    bundle_okf_version: str | None = None
    # Current spec §5: the profile that produced this report (spec/producer/loose).
    # ``validate_bundle`` leaves it at the default "spec"; ``apply_profile``
    # / ``validate_bundle_with_profile`` stamp the effective profile. Emitted
    # in ``as_dict()`` so library callers see it without CLI injection.
    profile: str = "spec"

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARNING]

    @property
    def infos(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.INFO]

    @property
    def ok(self) -> bool:
        """True iff there are no ERROR findings (warnings are tolerated)."""
        return not self.errors

    @property
    def ok_strict(self) -> bool:
        """True iff there are no ERROR or WARNING findings."""
        return not self.errors and not self.warnings

    def as_dict(self) -> dict:
        return {
            "bundle_root": str(self.bundle_root),
            "spec_version": self.spec_version,
            "bundle_okf_version": self.bundle_okf_version,
            "profile": self.profile,
            "ok": self.ok,
            "ok_strict": self.ok_strict,
            "counts": {
                "error": len(self.errors),
                "warning": len(self.warnings),
                "info": len(self.infos),
            },
            # iter3 P2-3: sort findings deterministically (§3.2) so
            # ``--format json`` output is byte-stable regardless of
            # Bundle dict-insertion order. Sort by (path, line, code,
            # concept_id) — the same logical order a human scans.
            "findings": [
                f.as_dict()
                for f in sorted(
                    self.findings,
                    key=lambda f: (
                        str(f.path) if f.path else "",
                        f.line or 0,
                        f.code,
                        str(f.concept_id) if f.concept_id else "",
                    ),
                )
            ],
        }


# ---------------------------------------------------------------------------
# Validation profiles (current spec §5)
# ---------------------------------------------------------------------------

# Profile code → severity remap tables.
#
# `spec` (default): upstream OKF 0.2 §11 conformance codes are errors; soft issues are
#   warnings/info.  (This is the baseline behavior of ``validate_bundle``.)
# `producer`: ``spec`` PLUS ``concept.missing_recommended_keys`` → ERROR
#   (agent-produced bundles must be complete).
# `loose`: ``spec`` EXCEPT ``concept.missing_type`` → WARNING
#   (migration aid; syntax errors stay errors).

_PROFILE_REMAPS: dict[str, dict[str, Severity]] = {
    "spec": {},
    "producer": {},
    "loose": {},
}

# Codes promoted to ERROR by ``--fail-on-broken-links``. asset.missing is a
# broken reference to a file just like link.broken, so the flag covers both.
_BROKEN_LINK_CODES: frozenset[str] = frozenset({"link.broken", "asset.missing"})


def apply_profile(
    report: ValidationReport,
    *,
    profile: str = "spec",
    fail_on_broken_links: bool = False,
) -> ValidationReport:
    """Remap finding severities by validation profile (current spec §5).

    Returns a **new** :class:`ValidationReport` with remapped severities.
    The original report is not mutated. ``Finding`` is frozen, so new
    instances are constructed with the updated severity.

    Args:
        report: the base validation report from :func:`validate_bundle`.
        profile: one of ``"spec"``, ``"producer"``, ``"loose"``.
        fail_on_broken_links: if True, promote ``link.broken`` to ERROR.

    The remap table per profile:

    ============ =================================================
    Profile      Behavior
    ============ =================================================
    ``spec``     Current behavior (no remap).
    ``producer`` ``spec`` + ``concept.missing_recommended_keys`` → ERROR
    ``loose``    ``spec`` except ``concept.missing_type`` → WARNING
    ============ =================================================

    ``--fail-on-broken-links`` is orthogonal and composes with any profile.

    Raises:
        ValueError: if ``profile`` is not one of the known profile names
            (``"spec"`` / ``"producer"`` / ``"loose"``). This is fail-closed:
            an unknown profile name must not silently behave as ``spec``
            while the JSON report claims the typo name.
    """
    if profile not in _PROFILE_REMAPS:
        raise ValueError(
            f"Unknown validation profile {profile!r}; "
            f"expected one of {sorted(_PROFILE_REMAPS)}."
        )
    new_findings: list[Finding] = []
    for f in report.findings:
        rule = VALIDATOR_RULE_REGISTRY.get(f.code)
        if rule is None:
            raise ValueError(f"Validator emitted unregistered finding code {f.code!r}")
        new_sev = rule.effective_severity(
            profile, fail_on_broken_links=fail_on_broken_links
        )
        if new_sev is not f.severity:
            new_findings.append(
                Finding(
                    code=f.code,
                    severity=new_sev,
                    message=f.message,
                    path=f.path,
                    line=f.line,
                    concept_id=f.concept_id,
                    detail=f.detail,
                )
            )
        else:
            new_findings.append(f)
    return ValidationReport(
        bundle_root=report.bundle_root,
        findings=new_findings,
        spec_version=report.spec_version,
        bundle_okf_version=report.bundle_okf_version,
        profile=profile,
    )


def validate_bundle_with_profile(
    bundle: Bundle,
    *,
    profile: str = "spec",
    fail_on_broken_links: bool = False,
    strict: bool = False,
) -> ValidationReport:
    """Validate ``bundle`` and apply a profile + optional strict + broken-link fail.

    Convenience wrapper: ``validate_bundle`` → ``apply_profile`` → strict promotion.

    Args:
        bundle: loaded OKF bundle.
        profile: validation profile name (``"spec"`` / ``"producer"`` / ``"loose"``).
        fail_on_broken_links: promote ``link.broken`` to ERROR.
        strict: promote remaining WARNING to ERROR (applied after profile remap).

    Returns:
        Profile-adjusted :class:`ValidationReport`.
    """
    base = validate_bundle(bundle)
    report = apply_profile(
        base, profile=profile, fail_on_broken_links=fail_on_broken_links
    )
    if strict:
        new_findings: list[Finding] = []
        for f in report.findings:
            if f.severity is Severity.WARNING:
                new_findings.append(
                    Finding(
                        code=f.code,
                        severity=Severity.ERROR,
                        message=f.message,
                        path=f.path,
                        line=f.line,
                        concept_id=f.concept_id,
                        detail=f.detail,
                    )
                )
            else:
                new_findings.append(f)
        report = ValidationReport(
            bundle_root=report.bundle_root,
            findings=new_findings,
            spec_version=report.spec_version,
            bundle_okf_version=report.bundle_okf_version,
            profile=report.profile,
        )
    return report


# ---------------------------------------------------------------------------
# Load-warning codes that are upstream OKF 0.2 §11 conformance failures (→ ERROR).
# ---------------------------------------------------------------------------

# §9 #1: every non-reserved .md file MUST have parseable YAML frontmatter.
_CONFORMANCE_PARSE_CODES: frozenset[str] = frozenset({
    "concept.parse_failed",
})

# §9 #2: every frontmatter block MUST contain a non-empty `type` field.
_CONFORMANCE_TYPE_CODES: frozenset[str] = frozenset({
    "concept.missing_type",
})

# §9 #3: reserved filenames MUST follow §6 / §7 structure.
#   - index.md: no frontmatter except at root (§6, §11).
#   - log.md: ISO 8601 YYYY-MM-DD date headings (§7 "MUST use").
_CONFORMANCE_STRUCTURE_CODES: frozenset[str] = frozenset({
    "index.unexpected_frontmatter",
    "log.bad_date_heading",
})

# Note: `log.not_newest_first` is left as WARNING — upstream OKF 0.2 §9 describes
# "newest first" as the conventional ordering, not as a MUST. A
# not-newest-first log is still parseable and useful; treating it as a
# hard conformance failure would be too aggressive.

_CONFORMANCE_ERROR_CODES: frozenset[str] = (
    _CONFORMANCE_PARSE_CODES
    | _CONFORMANCE_TYPE_CODES
    | _CONFORMANCE_STRUCTURE_CODES
)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def validate_bundle(
    bundle: Bundle,
    *,
    checks: "CheckSpec | None" = None,
    file_facts: ValidationFileFacts | None = None,
    _normalize_rules: bool = True,
) -> ValidationReport:
    """Run all validators against a loaded bundle.

    Args:
        bundle: a loaded `Bundle`.
        checks: optional CheckSpec to enable/disable individual checks
            (default: all enabled).

    Returns:
        A ValidationReport with errors (upstream OKF 0.2 §11 conformance) and warnings
        (soft issues). The report's `ok` flag is the SPEC-mandated pass/fail.
    """
    spec = checks or CheckSpec()
    facts = file_facts or LiveValidationFileFacts(bundle.root)
    report = ValidationReport(
        bundle_root=bundle.root,
        bundle_okf_version=bundle.okf_version,
    )

    # Promote load warnings to findings.
    # Upstream OKF 0.2 §11 base conformance failures (the three hard rules:
    # parseable frontmatter, non-empty `type`, reserved-filename structure)
    # become ERROR. Everything else stays WARNING.
    # The codes promoted here are the ones the loader emits when it detects
    # a §9 #1 / #2 / #3 violation; see model.py `_load_concept`/`_load_index`/
    # `_load_log` for the producers.
    for w in bundle.warnings:
        rule = VALIDATOR_RULE_REGISTRY.get(w.code) if _normalize_rules else None
        if _normalize_rules and rule is None:
            raise ValueError(f"Validator emitted unregistered load-warning code {w.code!r}")
        sev = rule.base_severity if rule is not None else Severity.WARNING
        report.findings.append(
            Finding(
                code=w.code,
                severity=sev,
                message=w.message,
                path=w.path,
                line=w.line,
            )
        )

    if spec.spec_version_check:
        _check_spec_version(bundle, report)
    if spec.concept_required_keys:
        _check_concept_required_keys(bundle, report)
    if spec.concept_recommended_keys:
        _check_concept_recommended_keys(bundle, report)
    if spec.reserved_filenames:
        _check_reserved_filenames(bundle, report)
    if spec.link_integrity:
        _check_link_integrity(bundle, report, facts)
        _check_asset_integrity(bundle, report, facts)
        _check_citations_collision(bundle, report)
    if spec.index_structure:
        _check_index_structure(bundle, report)
    if spec.log_structure:
        _check_log_structure(bundle, report)
    if spec.tag_normalization:
        _check_tag_normalization(bundle, report)
    if spec.capability_declarations:
        _check_capability_declarations(bundle, report)
    if spec.timestamp_validity:
        _check_timestamp_validity(bundle, report)
    if spec.metadata_validity:
        _check_metadata_validity(bundle, report)

    if not _normalize_rules:
        return report
    normalized: list[Finding] = []
    for finding in report.findings:
        rule = VALIDATOR_RULE_REGISTRY.get(finding.code)
        if rule is None:
            raise ValueError(f"Validator emitted unregistered finding code {finding.code!r}")
        normalized.append(Finding(
            code=finding.code, severity=rule.base_severity, message=finding.message,
            path=finding.path, line=finding.line, concept_id=finding.concept_id,
            detail=finding.detail,
        ))
    report.findings = normalized
    return report


# ---------------------------------------------------------------------------
# CheckSpec
# ---------------------------------------------------------------------------

# P2-7 (iter-3): map user-facing dotted finding codes (the codes users see in
# ``okf validate`` output and in JSON ``findings[].code``) to the CheckSpec
# category check that produces them. ``CheckSpec.only`` accepts BOTH the
# internal category names (``link_integrity``, ``concept_recommended_keys``,
# …) AND these dotted codes (``link.broken``, ``concept.missing_type``, …) so
# users can filter by the codes they actually see without memorising the
# undocumented internal names. A dotted code enables the responsible category
# check. Codes produced at load time (concept.missing_type, concept.parse_failed,
# …) are promoted into the report unconditionally by validate_bundle's
# load-warning loop (they are §9 conformance failures), so they appear
# regardless of which category is enabled; the mapping here only governs
# ACCEPTANCE in --checks (so the code is not rejected as "unknown").
_FINDING_CODE_TO_CATEGORY: dict[str, str] = {
    # spec_version_check
    "spec.version_undeclared": "spec_version_check",
    "spec.version_malformed": "spec_version_check",
    "spec.version_major_mismatch": "spec_version_check",
    "spec.version_newer_minor": "spec_version_check",
    "spec.version_older_minor": "spec_version_check",
    # concept_required_keys (concept.missing_type + parse-failure codes are
    # §9 #1/#2 conformance failures surfaced from load warnings)
    "concept.missing_type": "concept_required_keys",
    "concept.parse_failed": "concept_required_keys",
    "concept.unreadable": "concept_required_keys",
    "concept.body_too_large": "concept_required_keys",
    "concept.bad_id": "concept_required_keys",
    "concept.path_escapes_bundle": "concept_required_keys",
    "concept.duplicate_id": "concept_required_keys",
    # concept_recommended_keys
    "concept.missing_recommended_keys": "concept_recommended_keys",
    # reserved_filenames (also surfaces non-root index frontmatter)
    "index.non_root_frontmatter": "reserved_filenames",
    # link_integrity (link checks + asset/image checks + citations-collision
    # run under the same gate)
    "link.broken": "link_integrity",
    "link.form_inconsistency": "link_integrity",
    "link.wikilink_present": "link_integrity",
    "asset.missing": "link_integrity",
    "asset.out_of_bundle": "link_integrity",
    "concept.citations_both_forms": "link_integrity",
    # index_structure (index.missing_for_directory is the check-produced code;
    # index.parse_failed / index.unexpected_frontmatter are load warnings)
    "index.missing_for_directory": "index_structure",
    "index.parse_failed": "index_structure",
    "index.unexpected_frontmatter": "index_structure",
    # log_structure
    "log.bad_date_heading": "log_structure",
    "log.not_newest_first": "log_structure",
    # tag_normalization
    "tag.case_inconsistency": "tag_normalization",
    # capability_declarations
    "capability.declared": "capability_declarations",
    # timestamp_validity
    "concept.bad_timestamp": "timestamp_validity",
    **{f"concept.bad_{family}": "metadata_validity" for family in (
        "generated", "verified", "sources", "usage_window", "status", "computation",
    )},
    "concept.missing_runtime": "metadata_validity",
}


def _validator_rule(code: str, severity: Severity, scope: str,
                    **overrides: Severity) -> ValidatorRule:
    return ValidatorRule(code, severity, scope, MappingProxyType(dict(overrides)))


_RULES = (
    _validator_rule("spec.version_undeclared", Severity.INFO, "bundle"),
    _validator_rule("spec.version_malformed", Severity.WARNING, "bundle"),
    _validator_rule("spec.version_major_mismatch", Severity.WARNING, "bundle"),
    _validator_rule("spec.version_newer_minor", Severity.WARNING, "bundle"),
    _validator_rule("spec.version_older_minor", Severity.INFO, "bundle"),
    _validator_rule("config.unparseable", Severity.WARNING, "config"),
    _validator_rule("reserved_file_escapes_bundle", Severity.WARNING, "reserved_file"),
    _validator_rule("concept.missing_type", Severity.ERROR, "concept", loose=Severity.WARNING),
    _validator_rule("concept.parse_failed", Severity.ERROR, "concept"),
    _validator_rule("concept.unreadable", Severity.WARNING, "concept"),
    _validator_rule("concept.body_too_large", Severity.WARNING, "concept"),
    _validator_rule("concept.bad_id", Severity.WARNING, "concept"),
    _validator_rule("concept.path_escapes_bundle", Severity.WARNING, "concept"),
    _validator_rule("concept.duplicate_id", Severity.WARNING, "concept"),
    _validator_rule("concept.missing_recommended_keys", Severity.WARNING, "concept",
                    producer=Severity.ERROR),
    _validator_rule("index.non_root_frontmatter", Severity.WARNING, "reserved_file"),
    _validator_rule("index.parse_failed", Severity.WARNING, "reserved_file"),
    _validator_rule("index.unexpected_frontmatter", Severity.ERROR, "reserved_file"),
    _validator_rule("index.missing_for_directory", Severity.WARNING, "reserved_file"),
    _validator_rule("log.bad_date_heading", Severity.ERROR, "reserved_file"),
    _validator_rule("log.not_newest_first", Severity.WARNING, "reserved_file"),
    _validator_rule("link.broken", Severity.WARNING, "concept"),
    _validator_rule("link.form_inconsistency", Severity.INFO, "concept"),
    _validator_rule("link.wikilink_present", Severity.INFO, "concept"),
    _validator_rule("asset.missing", Severity.WARNING, "concept"),
    _validator_rule("asset.out_of_bundle", Severity.INFO, "concept"),
    _validator_rule("concept.citations_both_forms", Severity.INFO, "concept"),
    _validator_rule("tag.case_inconsistency", Severity.INFO, "concept"),
    _validator_rule("capability.declared", Severity.INFO, "capability"),
    _validator_rule("concept.bad_timestamp", Severity.WARNING, "concept"),
    *(_validator_rule(f"concept.bad_{family}", Severity.WARNING, "concept") for family in (
        "generated", "verified", "sources", "usage_window", "status", "computation",
    )),
    _validator_rule("concept.missing_runtime", Severity.WARNING, "concept"),
)
VALIDATOR_RULE_REGISTRY = MappingProxyType({rule.code: rule for rule in _RULES})
if len(VALIDATOR_RULE_REGISTRY) != len(_RULES):
    raise RuntimeError("duplicate validator rule code")


@dataclass(frozen=True)
class CheckSpec:
    """Toggle individual validator checks. All default to True."""

    spec_version_check: bool = True
    concept_required_keys: bool = True
    concept_recommended_keys: bool = True
    reserved_filenames: bool = True
    link_integrity: bool = True
    index_structure: bool = True
    log_structure: bool = True
    tag_normalization: bool = True
    capability_declarations: bool = True
    timestamp_validity: bool = True
    metadata_validity: bool = True

    @classmethod
    def only(cls, *names: str) -> "CheckSpec":
        """Build a CheckSpec with ONLY the named checks enabled.

        Accepts BOTH internal category names (``link_integrity``,
        ``concept_recommended_keys``, …) AND user-facing dotted finding
        codes (``link.broken``, ``concept.missing_type``, …) as they appear
        in ``okf validate`` output. A dotted code enables the category check
        responsible for producing it (P2-7 / iter-3). Mixed input is allowed.
        """
        defaults = {f.name for f in cls.__dataclass_fields__.values()}
        # Resolve each requested name to a category. Dotted codes map via
        # _FINDING_CODE_TO_CATEGORY; category names pass through unchanged.
        resolved: set[str] = set()
        unknown: list[str] = []
        for n in names:
            if n in defaults:
                resolved.add(n)
            elif n in _FINDING_CODE_TO_CATEGORY:
                resolved.add(_FINDING_CODE_TO_CATEGORY[n])
            else:
                unknown.append(n)
        if unknown:
            raise ValueError(
                f"Unknown check names: {sorted(unknown)}. "
                f"Valid: category names ({sorted(defaults)}) or dotted "
                f"finding codes (e.g. link.broken, concept.missing_type)."
            )
        return cls(**{n: (n in resolved) for n in defaults})


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _parse_version(v: str) -> tuple[int, int] | None:
    """Parse complete ASCII ``major.minor``; ignore surrounding whitespace.

    Malformed input is not a version, including not version ``0.0``.
    """
    import re

    m = re.fullmatch(r"([0-9]+)\.([0-9]+)", v.strip())
    if not m:
        return None
    try:
        return (int(m.group(1)), int(m.group(2)))
    except ValueError:
        # Python may refuse excessively long integer components.
        return None


def _declared_spec_version(bundle: Bundle) -> str | None:
    """Retain present-but-empty declarations lost by the model's convenience field."""
    root_index = bundle.indexes.get(Path("index.md"))
    if root_index is not None and "okf_version" in root_index.frontmatter:
        return str(root_index.frontmatter["okf_version"])
    return bundle.okf_version


def _check_spec_version(bundle: Bundle, report: ValidationReport) -> None:
    declared = _declared_spec_version(bundle)
    if declared is None:
        # Upstream OKF 0.2 §12: bundles MAY declare a version; absence is legal.
        report.findings.append(
            Finding(
                code="spec.version_undeclared",
                severity=Severity.INFO,
                message=(
                    "Bundle does not declare an okf_version in its root index.md. "
                    f"This toolkit assumes OKF v{SPEC_VERSION}."
                ),
            )
        )
        return
    # Upstream OKF 0.2 §12 asks for best-effort consumption on mismatch. We:
    #   - same major + same minor → fully compatible, no finding.
    #   - newer minor → WARNING; older minor → INFO (review migration needs).
    #   - different major → WARNING (may have breaking changes; still
    #     best-effort per upstream §12, but the consumer should know).
    #   - malformed version → WARNING (cannot reason about compatibility).
    bundle_v = _parse_version(declared)
    toolkit_v = _parse_version(SPEC_VERSION)
    assert toolkit_v is not None
    if bundle_v is None:
        report.findings.append(
            Finding(
                code="spec.version_malformed",
                severity=Severity.WARNING,
                message=(
                    "Bundle declares a malformed okf_version; expected ASCII "
                    "major.minor. Compatibility is unknown; continuing with "
                    "best-effort consumption."
                ),
            )
        )
        return
    if bundle_v == toolkit_v:
        return  # exact match, no finding
    if bundle_v[0] != toolkit_v[0]:
        report.findings.append(
            Finding(
                code="spec.version_major_mismatch",
                severity=Severity.WARNING,
                message=(
                    f"Bundle declares okf_version={declared!r}; this "
                    f"toolkit implements v{SPEC_VERSION}. Major-version "
                    "differences MAY include breaking changes (upstream OKF 0.2 §12). "
                    "Continuing with best-effort consumption."
                ),
            )
        )
    elif bundle_v > toolkit_v:
        report.findings.append(
            Finding(
                code="spec.version_newer_minor",
                severity=Severity.WARNING,
                message=(
                    f"Bundle declares okf_version={declared!r}, newer "
                    f"than okf-loom's v{SPEC_VERSION}. Some features may be "
                    "unrecognised; doing best-effort consumption per upstream OKF 0.2 §12."
                ),
            )
        )
    else:
        # An older minor is informational, not a compatibility certificate.
        # Upstream 0.2 explicitly lists breaking provenance changes in §13.1.
        report.findings.append(
            Finding(
                code="spec.version_older_minor",
                severity=Severity.INFO,
                message=(
                    f"Bundle declares okf_version={declared!r}; this "
                    f"toolkit implements v{SPEC_VERSION}. Continuing best-effort "
                    "consumption; version numbers alone do not establish compatibility. "
                    "Review the OKF 0.1 to 0.2 provenance changes (upstream §13.1) "
                    "before explicit migration."
                ),
            )
        )


def _check_concept_required_keys(bundle: Bundle, report: ValidationReport) -> None:
    # `type` is the only SPEC-required key. Missing type was already promoted
    # from load warnings; nothing else to do here.
    pass


def _check_concept_recommended_keys(
    bundle: Bundle, report: ValidationReport
) -> None:
    for c in bundle.concepts.values():
        missing = [
            k for k in RECOMMENDED_FRONTMATTER_KEYS if not c.frontmatter.get(k)
            and not (k == "timestamp" and "generated" in c.frontmatter)
        ]
        if missing:
            report.findings.append(
                Finding(
                    code="concept.missing_recommended_keys",
                    severity=Severity.WARNING,
                    message=(
                        f"Concept is missing recommended frontmatter keys: "
                        f"{', '.join(missing)}"
                    ),
                    path=c.path,
                    concept_id=c.id,
                    detail={"missing": missing},
                )
            )


def _check_reserved_filenames(bundle: Bundle, report: ValidationReport) -> None:
    # Reserved filenames used for non-reserved purposes already caught at
    # load. Here we check the inverse: a concept path that collides with a
    # reserved name at any level.
    for rel, idx in bundle.indexes.items():
        # Non-root index files with frontmatter is already a load warning;
        # surface it again under the dedicated code for clarity.
        if not idx.is_root and idx.frontmatter:
            report.findings.append(
                Finding(
                    code="index.non_root_frontmatter",
                    severity=Severity.WARNING,
                    message=(
                        "Non-root index.md carries frontmatter; upstream OKF 0.2 §8 reserves "
                        "frontmatter for the bundle-root index.md only."
                    ),
                    path=idx.path,
                )
            )


def _check_link_integrity(bundle: Bundle, report: ValidationReport,
                          file_facts: ValidationFileFacts) -> None:
    """Upstream OKF 0.2 §11: broken links are tolerated; flag as warnings.

    Two categories of "broken" links are silently accepted
    rather than flagged, because they are valid references that the
    bundle-scoped resolver cannot classify:

    1. **Directory-index references** (``/foo/index.md`` or
       ``./index.md``): ``index.md`` is a SPEC §3.1 reserved filename
       that exists on disk as a directory listing but is not registered
       as a concept (only non-reserved ``.md`` files are). Links to a
       directory's ``index.md`` are valid in the viewer; the validator
       now skips the broken-link warning when the file exists on disk.

    2. **Out-of-bundle references** (``../../docs/foo.md``): paths that
       escape the bundle root are now classified as
       ``external_out_of_bundle`` by the parser when the target file
       exists in the workspace, so they never enter ``unresolved``. The
       remaining broken-link findings here are genuinely missing files.
    """
    graph = bundle.graph()
    seen: set[tuple[ConceptId, str, int]] = set()
    for link in graph.unresolved:
        key = (link.source, link.target_raw, link.line)
        if key in seen:
            continue
        seen.add(key)
        # Graph owns document/fragment diagnostics; valid reserved navigation
        # is already separated from unresolved links without host-file guesses.
        # Build a clearer message for out-of-bundle broken links.
        raw = link.target_raw
        is_out_of_bundle = raw.startswith("../") or "/.." in raw or raw == ".."
        if is_out_of_bundle:
            message = (
                f"Out-of-bundle link target does not exist in workspace: "
                f"{raw!r}"
            )
        else:
            message = (
                f"Internal link target or heading does not exist in bundle: {raw!r}"
            )
        report.findings.append(
            Finding(
                code="link.broken",
                severity=Severity.WARNING,
                message=message,
                path=bundle.concepts[link.source].path if link.source in bundle.concepts else None,
                line=link.line,
                concept_id=link.source,
                detail={
                    "target_raw": raw,
                    "form": link.form,
                    "label": link.label,
                    "out_of_bundle": is_out_of_bundle,
                },
            )
        )

    # Detect absolute-vs-relative inconsistency: a concept referenced by both
    # forms is legal but worth flagging for curation consistency.
    by_target: dict[ConceptId, set[str]] = {}
    for link in graph.edges:
        if link.target and link.target in graph.concept_ids:
            by_target.setdefault(link.target, set()).add(link.form)
    for target, forms in by_target.items():
        if len(forms) > 1:
            report.findings.append(
                Finding(
                    code="link.form_inconsistency",
                    severity=Severity.INFO,
                    message=(
                        f"Concept {concept_id_to_str(target)!r} is referenced via "
                        f"multiple link forms: {sorted(forms)}. Upstream OKF 0.2 §6.1 "
                        "recommends the absolute form for stability."
                    ),
                    concept_id=target,
                    detail={"forms": sorted(forms)},
                )
            )

    # Loom extension (current spec §4): wikilinks are a migration convenience, not a
    # problem -- so this is an INFO nudge to convert them to standard
    # markdown links for plain-OKF-consumer compatibility, never a warning.
    # One finding per wikilink occurrence (deduped by source/target/line).
    wl_seen: set[tuple[ConceptId, str, int]] = set()
    for link in graph.edges + graph.unresolved:
        if link.form != "wikilink":
            continue
        key = (link.source, link.target_raw, link.line)
        if key in wl_seen:
            continue
        wl_seen.add(key)
        report.findings.append(
            Finding(
                code="link.wikilink_present",
                severity=Severity.INFO,
                message=(
                    f"Wikilink to {link.target_raw!r}; consider converting to a "
                    "standard markdown link for plain-OKF-consumer compatibility."
                ),
                path=(
                    bundle.concepts[link.source].path
                    if link.source in bundle.concepts
                    else None
                ),
                line=link.line,
                concept_id=link.source,
                detail={
                    "target_raw": link.target_raw,
                    "label": link.label,
                },
            )
        )


def _check_asset_integrity(bundle: Bundle, report: ValidationReport,
                           file_facts: ValidationFileFacts) -> None:
    """Image targets must exist for the viewer to display them.

    Images are deliberately not graph edges (spec: only ``[label](x.md)``
    links are), so a typo'd screenshot path was previously invisible to
    every check while rendering as a broken image in the studio and in
    static builds. Two findings:

    * ``asset.missing`` (WARNING, promoted by ``--fail-on-broken-links``):
      the target file does not exist — neither inside the bundle nor, for
      relative paths that escape it, anywhere in the workspace.
    * ``asset.out_of_bundle`` (INFO): the target file exists but lives
      outside the bundle root. Editors and GitHub render it, but the live
      studio and static builds only serve files under the bundle root, so
      it shows as broken there.

    External URLs (any scheme, including the CSP-blocked remote images and
    the sanitizer-blocked ``data:`` URIs) are skipped: they are not file
    references. Targets are percent-decoded before the existence check
    (``foo%20bar.png`` names ``foo bar.png``).
    """
    from urllib.parse import unquote
    from .parse import extract_image_targets

    for cid, concept in bundle.concepts.items():
        seen: set[tuple[str, int]] = set()
        for target_raw, line in extract_image_targets(concept.body):
            key = (target_raw, line)
            if key in seen:
                continue
            seen.add(key)
            target = unquote(target_raw.split("#", 1)[0])
            if not target or target.startswith("//"):
                continue
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", target):
                continue  # external URL / data: — not a file reference
            exists, out_of_bundle = _lookup_fact(file_facts, concept.rel_path, target)
            if exists and not out_of_bundle:
                continue
            if exists and out_of_bundle:
                report.findings.append(
                    Finding(
                        code="asset.out_of_bundle",
                        severity=Severity.INFO,
                        message=(
                            f"Image target {target_raw!r} exists but lives outside "
                            "the bundle root; the live studio and static builds "
                            "only serve bundle-local files, so it will render "
                            "broken there."
                        ),
                        path=concept.path,
                        line=line,
                        concept_id=cid,
                        detail={"target_raw": target_raw},
                    )
                )
                continue
            report.findings.append(
                Finding(
                    code="asset.missing",
                    severity=Severity.WARNING,
                    message=f"Image target does not exist: {target_raw!r}",
                    path=concept.path,
                    line=line,
                    concept_id=cid,
                    detail={
                        "target_raw": target_raw,
                        "out_of_bundle": out_of_bundle,
                    },
                )
            )


def _check_citations_collision(bundle: Bundle, report: ValidationReport) -> None:
    """Loom current spec §4: ``citations:`` is a governed frontmatter key; a body
    ``# Citations`` heading is the compatibility OKF convention. When a concept
    carries BOTH, the viewer would render duplicate lists (the renderer
    suppresses the body heading when frontmatter is present). Surface an
    INFO so the author can pick one representation."""
    for cid, concept in bundle.concepts.items():
        fm_citations = concept.frontmatter.get("citations")
        if not (isinstance(fm_citations, list) and fm_citations):
            continue
        has_body_section = any(
            h.text.strip().lower() == "citations" for h in concept.headings
        )
        if has_body_section:
            report.findings.append(
                Finding(
                    code="concept.citations_both_forms",
                    severity=Severity.INFO,
                    message=(
                        "Concept has both a frontmatter `citations:` key and a "
                        "body `# Citations` heading. The viewer renders the "
                        "frontmatter form and suppresses the body heading; "
                        "consider removing the body section for clarity."
                    ),
                    path=concept.path,
                    concept_id=cid,
                )
            )


def _check_index_structure(bundle: Bundle, report: ValidationReport) -> None:
    # Upstream OKF 0.2 §8: index.md lists contents for progressive disclosure.
    # We surface two soft issues:
    #   (a) directories with concepts but no index.md at all (loss of PD)
    #   (b) stale generated index.md that's missing entries
    dir_has_index: set[Path] = {idx.rel_path.parent for idx in bundle.indexes.values()}
    concept_dirs: set[Path] = set()
    for c in bundle.concepts.values():
        concept_dirs.add(c.rel_path.parent)
    for d in sorted(concept_dirs):
        if d not in dir_has_index:
            report.findings.append(
                Finding(
                    code="index.missing_for_directory",
                    severity=Severity.WARNING,
                    message=(
                        f"Directory {d}/ contains concepts but has no index.md "
                    "for progressive disclosure (upstream OKF 0.2 §8)."
                    ),
                    path=bundle.root / d,
                )
            )


def _check_log_structure(bundle: Bundle, report: ValidationReport) -> None:
    # Upstream OKF 0.2 §9: log.md uses ISO 8601 YYYY-MM-DD headings, newest first.
    for rel, log in bundle.logs.items():
        prev: str | None = None
        prev_was_iso = False
        for entry in log.entries:
            is_iso = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.date))
            if not is_iso:
                report.findings.append(
                    Finding(
                        code="log.bad_date_heading",
                        severity=Severity.WARNING,
                        message=(
                            f"Log heading is not ISO 8601 YYYY-MM-DD: "
                            f"{entry.raw_heading!r}"
                        ),
                        path=log.path,
                    )
                )
            # P1-6 (iter-4): only check newest-first ordering between ISO
            # dates. Non-ISO heading text vs ISO date is a meaningless string
            # compare that fires spuriously (e.g. "Notes" > "2024-01-01").
            if is_iso and prev_was_iso and entry.date > prev:
                report.findings.append(
                    Finding(
                        code="log.not_newest_first",
                        severity=Severity.WARNING,
                        message=(
                            f"Log entries are not newest-first: {entry.date} "
                            f"appears after {prev}."
                        ),
                        path=log.path,
                    )
                )
            if is_iso:
                prev = entry.date
                prev_was_iso = True
            # non-ISO entries don't update prev (meaningless for ordering)


def _check_tag_normalization(bundle: Bundle, report: ValidationReport) -> None:
    # Surface tags that differ only by case/whitespace (curator hint).
    norm: dict[str, str] = {}
    for c in bundle.concepts.values():
        for t in c.tags:
            key = t.strip().lower()
            if not key:
                continue
            if key in norm and norm[key] != t:
                report.findings.append(
                    Finding(
                        code="tag.case_inconsistency",
                        severity=Severity.INFO,
                        message=(
                            f"Tags differ only by case/spacing: {norm[key]!r} "
                            f"vs {t!r}"
                        ),
                        path=c.path,
                        concept_id=c.id,
                    )
                )
            else:
                norm.setdefault(key, t)


def _check_capability_declarations(
    bundle: Bundle, report: ValidationReport
) -> None:
    # We just record what the bundle declares; resolution happens in extensions.
    for ext in bundle.okf_extensions:
        report.findings.append(
            Finding(
                code="capability.declared",
                severity=Severity.INFO,
                message=f"Bundle declares capability: {ext}",
            )
        )


def _check_timestamp_validity(bundle: Bundle, report: ValidationReport) -> None:
    _append_metadata_issues(bundle, report, timestamps=True)


def _check_metadata_validity(bundle: Bundle, report: ValidationReport) -> None:
    _append_metadata_issues(bundle, report, timestamps=False)


def _append_metadata_issues(bundle: Bundle, report: ValidationReport, *, timestamps: bool) -> None:
    from .metadata import metadata_issues
    for c in bundle.concepts.values():
        for code, field, message in metadata_issues(c.frontmatter):
            if (code == "concept.bad_timestamp") != timestamps:
                continue
            report.findings.append(Finding(
                code=code, severity=Severity.WARNING, message=f"{field}: {message}",
                path=c.path, concept_id=c.id, detail={"field": field},
            ))
