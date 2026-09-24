"""Read-only OKF 0.2 metadata semantics, shared by model and validation.

Authored frontmatter remains authoritative and is never rewritten here. Trust
is advisory, not authorization or evidence that any computation has run. No
resource named by these fields is opened, fetched, imported, or executed.
"""
from __future__ import annotations

import copy
import math
import re
from datetime import date, datetime, timezone
from typing import Any


_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ][0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]+)?)?(?:Z|[+-][0-9]{2}(?::?[0-9]{2})?)"
)


def parse_instant(value: Any) -> datetime | None:
    """Parse a real, explicitly offset datetime; never invent a timezone.

    Check offset components before ``fromisoformat`` (which otherwise accepts
    and normalizes such malformed offsets as +01:99).
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None and value.utcoffset() is not None else None
    if not isinstance(value, str) or not _DATETIME.fullmatch(value):
        return None
    offset = re.search(r"[+-]([0-9]{2})(?::?([0-9]{2}))?$", value)
    if offset and (int(offset[1]) > 23 or int(offset[2] or 0) > 59):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def instant_text(value: Any) -> str | None:
    instant = parse_instant(value)
    return instant.isoformat() if instant is not None else None


def instant_key(value: Any) -> str:
    """Canonical UTC ordering text for an explicit-offset instant.

    Returns ``""`` when the value is absent or not a real offset datetime, so
    callers can sort unknown-time entries last without inventing a date. The
    returned texts compare chronologically as plain strings (fixed UTC offset,
    zero-padded fields); presentation keeps the authored offset via
    :func:`instant_text`.
    """
    instant = parse_instant(value)
    if instant is None:
        return ""
    return instant.astimezone(timezone.utc).isoformat()


def effective_instant_key(frontmatter: dict[str, Any]) -> str:
    """Canonical UTC ordering key for a concept's effective change instant.

    Ownership mirrors :func:`normalize_metadata` exactly: ``generated.at``
    counts only when ``generated`` is present with a nonempty ``by``; the
    legacy ``timestamp`` is consulted only when ``generated`` is absent; a
    present but incomplete or malformed mapping never yields an instant.
    Returns ``""`` for unknown time — callers must order those last and never
    fall back to the legacy key in those cases.
    """
    return instant_key(normalize_metadata(frontmatter).get("generated_at"))


def json_metadata(value: Any, *, _depth: int = 0, _ancestors: frozenset[int] = frozenset()) -> Any:
    """Detached JSON view, retaining unknown fields without serializing YAML objects.

    Depth bounding also handles YAML alias cycles. Raw values remain available
    on Concept.frontmatter; this projection is not a round-trip representation.
    """
    if _depth >= 32:
        return {"truncated": True, "reason": "max_depth"}
    if isinstance(value, (dict, list, tuple)):
        if id(value) in _ancestors:
            return {"truncated": True, "reason": "cyclic_metadata"}
        _ancestors = _ancestors | {id(value)}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_metadata(v, _depth=_depth + 1, _ancestors=_ancestors)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_metadata(v, _depth=_depth + 1, _ancestors=_ancestors) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return ".nan" if math.isnan(value) else "-.inf" if value < 0 else ".inf"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _event(value: Any) -> Any:
    result = json_metadata(value)
    if isinstance(result, dict) and "at" in result:
        result["at"] = instant_text(value.get("at")) or result["at"]
    return result


def _window(value: Any) -> Any:
    result = json_metadata(value)
    if isinstance(result, dict):
        for key in ("from", "to"):
            if key in result:
                result[key] = instant_text(value.get(key)) or result[key]
    return result


def normalize_metadata(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """JSON-compatible projection with defaults only where OKF specifies them.

    ``generated_at`` is the effective content-change instant; ``timestamp`` is
    considered ONLY when the generated key is absent. Invalid present values
    remain visible in their family but never become valid derived instants.
    Sources retain authored ids; no positional ids or credibility scores are
    invented. An entry's usage_window overrides the shared window by presence.
    """
    fm = frontmatter
    result = {key: json_metadata(fm[key]) for key in (
        "generated", "verified", "sources", "usage_window", "status", "stale_after",
        "runtime", "parameters", "computation", "executor", "attester", "receipt",
    ) if key in fm}
    generated = fm.get("generated")
    if "generated" in fm:
        result["generated"] = _event(generated)
        at = (generated.get("at") if isinstance(generated, dict)
              and _nonempty(generated.get("by")) else None)
        origin = "generated.at"
    else:
        at = fm.get("timestamp")
        origin = "timestamp"
    result["generated_at"] = instant_text(at)
    result["generated_at_source"] = origin if result["generated_at"] else None
    verified = fm.get("verified")
    events = [verified] if isinstance(verified, dict) else verified
    if isinstance(events, list):
        result["verified"] = [_event(event) for event in events]
    # Only well-formed verification events support a derived trust signal.
    actors = [event["by"] for event in (events if isinstance(events, list) else [])
              if isinstance(event, dict) and _nonempty(event.get("by"))
              and parse_instant(event.get("at")) is not None]
    result["trust_tier"] = ("human-reviewed" if any(
        actor.startswith("human:") and actor[6:].strip() for actor in actors
    ) else "machine-confirmed" if actors else "unverified")
    result.setdefault("status", "stable")
    if "stale_after" in fm:
        result["stale_after"] = instant_text(fm["stale_after"]) or result["stale_after"]
    if "usage_window" in fm:
        result["usage_window"] = _window(fm["usage_window"])
    if isinstance(fm.get("sources"), list):
        sources = []
        for source in fm["sources"]:
            row = json_metadata(source)
            if isinstance(source, dict):
                if "last_modified" in source:
                    row["last_modified"] = instant_text(source["last_modified"]) or row["last_modified"]
                if "usage_window" in source:
                    row["usage_window"] = _window(source["usage_window"])
                elif "usage_window" in fm:
                    row["usage_window"] = copy.deepcopy(result["usage_window"])
            sources.append(row)
        result["sources"] = sources
    return result


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_stale(frontmatter: dict[str, Any], now: datetime) -> bool | None:
    """Compare on/after expiry. None means absent/invalid expiry, not fresh.

    ``now`` is explicit so cached search/context artifacts stay deterministic.
    A naive caller clock is an API error rather than an assumed local offset.
    """
    if not isinstance(now, datetime) or parse_instant(now) is None:
        raise ValueError("now must be a timezone-aware datetime")
    expiry = parse_instant(frontmatter.get("stale_after"))
    return now >= expiry if expiry is not None else None


def metadata_issues(fm: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return (code, field, guidance) without rejecting optional families.

    No existence checks on computation/executor/attester resources, no receipt
    verification, and no attestation verdict are performed by this consumer.
    Unknown keys and runtime/type names deliberately receive no finding.
    """
    issues: list[tuple[str, str, str]] = []

    def issue(family: str, field: str, message: str) -> None:
        issues.append((f"concept.bad_{family}", field, message))

    def timestamp(value: Any, field: str) -> None:
        if parse_instant(value) is None:
            issue("timestamp", field, "Expected a real ISO 8601 datetime with an explicit UTC offset")

    def window(value: Any, field: str) -> None:
        if not isinstance(value, dict):
            issue("usage_window", field, "Expected a {from, to} mapping")
            return
        for key in ("from", "to"):
            timestamp(value.get(key), f"{field}.{key}")
        start, end = (parse_instant(value.get(key)) for key in ("from", "to"))
        if start is not None and end is not None and start > end:
            issue("usage_window", field, "from must not be later than to")

    if "generated" in fm:
        value = fm["generated"]
        if not isinstance(value, dict):
            issue("generated", "generated", "Expected a mapping with by")
        else:
            if not _nonempty(value.get("by")):
                issue("generated", "generated.by", "Expected a non-empty actor string")
            if "at" in value:
                timestamp(value["at"], "generated.at")
    elif "timestamp" in fm:
        timestamp(fm["timestamp"], "timestamp")
    if "verified" in fm:
        value = fm["verified"]
        events = [value] if isinstance(value, dict) else value
        if not isinstance(events, list):
            issue("verified", "verified", "Expected a verification mapping or list")
        else:
            for index, event in enumerate(events):
                field = f"verified[{index}]"
                if not isinstance(event, dict):
                    issue("verified", field, "Expected a {by, at} mapping")
                    continue
                if not _nonempty(event.get("by")):
                    issue("verified", f"{field}.by", "Expected a non-empty actor string")
                timestamp(event.get("at"), f"{field}.at")
    if "status" in fm and (not isinstance(fm["status"], str)
                            or fm["status"] not in ("draft", "stable", "deprecated")):
        issue("status", "status", "Expected draft, stable, or deprecated")
    if "stale_after" in fm:
        timestamp(fm["stale_after"], "stale_after")
    if "usage_window" in fm:
        window(fm["usage_window"], "usage_window")
    if "sources" in fm:
        sources = fm["sources"]
        if not isinstance(sources, list):
            issue("sources", "sources", "Expected a list of source mappings")
        else:
            seen_ids: set[str] = set()
            for index, source in enumerate(sources):
                field = f"sources[{index}]"
                if not isinstance(source, dict):
                    issue("sources", field, "Expected a source mapping")
                    continue
                if not _nonempty(source.get("resource")):
                    issue("sources", f"{field}.resource", "Expected a non-empty resource or scope descriptor")
                for key in ("id", "title", "author"):
                    if key in source and not _nonempty(source[key]):
                        issue("sources", f"{field}.{key}", "Expected a non-empty string")
                source_id = source.get("id")
                if _nonempty(source_id):
                    if source_id in seen_ids:
                        issue("sources", f"{field}.id", "Duplicate source id makes claim attribution ambiguous")
                    seen_ids.add(source_id)
                if "usage_count" in source:
                    count = source["usage_count"]
                    if type(count) is not int or count < 0:
                        issue("sources", f"{field}.usage_count", "Expected a non-negative integer count")
                if "last_modified" in source:
                    timestamp(source["last_modified"], f"{field}.last_modified")
                if "usage_window" in source:
                    window(source["usage_window"], f"{field}.usage_window")
    if fm.get("type") == "Attested Computation" and "runtime" not in fm:
        issues.append(("concept.missing_runtime", "runtime", "Attested Computation should declare its runtime"))
    for key in ("runtime", "computation"):
        if key in fm and not _nonempty(fm[key]):
            issue("computation", key, "Expected a non-empty string")
    if "parameters" in fm:
        parameters = fm["parameters"]
        if not isinstance(parameters, list):
            issue("computation", "parameters", "Expected a list of {name, type, required} mappings")
        else:
            for index, parameter in enumerate(parameters):
                if (not isinstance(parameter, dict)
                    or not _nonempty(parameter.get("name"))
                    or not _nonempty(parameter.get("type"))
                    or type(parameter.get("required")) is not bool):
                    issue("computation", f"parameters[{index}]", "Expected named, typed parameters with boolean required")
    for key in ("executor", "attester"):
        if key not in fm:
            continue
        value = fm[key]
        if not isinstance(value, dict) or not _nonempty(value.get("resource")):
            issue("computation", key, "Expected a mapping with a non-empty resource")
        if key == "executor" and isinstance(value, dict) and "receipt" in value:
            receipt = value["receipt"]
            if not isinstance(receipt, list) or any(not _nonempty(item) for item in receipt):
                issue("computation", "executor.receipt", "Expected a list of receipt field names")
    return issues
