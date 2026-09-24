"""Helpers for the governed ``aliases`` frontmatter key."""
from __future__ import annotations

from typing import Any


_FALSE_TOKENS = {"false", "no", "off", "0"}


def alias_label(value: Any) -> str:
    """Return the display/search label for one alias entry, or ``""``.

    OKF bundles historically use ``aliases: [foo, bar]``. Newer producers may
    use mapping entries such as ``{label: Architecture, discoverable: false}``
    to keep broad aliases searchable while excluding them from automatic
    unlinked-mention discovery.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        label = value.get("label")
        if isinstance(label, str):
            return label.strip()
    return ""


def alias_labels(value: Any) -> list[str]:
    """Return all alias labels, including search-only aliases."""
    if not isinstance(value, list):
        return []
    labels: list[str] = []
    for item in value:
        label = alias_label(item)
        if label:
            labels.append(label)
    return labels


def alias_discoverable(value: Any) -> bool:
    """Return whether one alias entry participates in mention discovery."""
    if not isinstance(value, dict):
        return True
    raw = value.get("discoverable", True)
    if raw is False:
        return False
    if isinstance(raw, str) and raw.strip().lower() in _FALSE_TOKENS:
        return False
    return True


def discoverable_alias_labels(value: Any) -> list[str]:
    """Return alias labels eligible for automatic unlinked-mention discovery."""
    if not isinstance(value, list):
        return []
    labels: list[str] = []
    for item in value:
        if not alias_discoverable(item):
            continue
        label = alias_label(item)
        if label:
            labels.append(label)
    return labels
