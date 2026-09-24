"""Concept-id <-> path utilities (SPEC §2, §3, §5).

A *concept id* is the path of the concept's file within the bundle, with the
`.md` suffix removed (SPEC §2). We represent it both as a string
(``"tables/users"``) and as a tuple of validated segments
(``("tables", "users")``). The tuple form is canonical in the data model.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .exceptions import OKFError
from . import RESERVED_FILENAMES

# Per SPEC §2 example, segments may include letters, digits, underscore, dot,
# and hyphen. The first character must be alphanumeric or underscore to keep
# things filesystem- and URL-safe across operating systems.
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")

# A concept id is a sequence of segments joined by '/'.
ConceptId = tuple[str, ...]


class ConceptIdError(OKFError, ValueError):
    """Raised when a string cannot be parsed as a concept id."""


def validate_segment(seg: str) -> None:
    """Validate a single concept-id path segment.

    Raises:
        ConceptIdError: if the segment is empty or contains disallowed chars.
    """
    if not seg:
        raise ConceptIdError("Empty concept id segment")
    if not _SEGMENT_RE.fullmatch(seg):
        raise ConceptIdError(f"Invalid concept id segment: {seg!r}")


def is_reserved_filename(name: str) -> bool:
    """Return True if `name` is a SPEC §3.1 reserved filename (case-sensitive)."""
    return name in RESERVED_FILENAMES


def concept_id_from_str(s: str) -> ConceptId:
    """Parse a slash-separated concept id string into a tuple of segments.

    Examples:
        >>> concept_id_from_str("tables/users")
        ('tables', 'users')
        >>> concept_id_from_str("/tables/users/")
        ('tables', 'users')
        >>> concept_id_from_str("/tables/users.md")
        ('tables', 'users')

    A trailing ``.md`` extension is stripped so that absolute link targets
    (SPEC §5.1, e.g. ``/tables/users.md``) used as relation/edge targets
    resolve to the same concept id as the bare form.
    """
    s = s.strip()
    if s.endswith(".md"):
        s = s[: -len(".md")]
    parts = tuple(p for p in s.split("/") if p)
    if not parts:
        raise ConceptIdError(f"Empty concept id: {s!r}")
    for p in parts:
        validate_segment(p)
    return parts


def concept_id_to_str(cid: ConceptId) -> str:
    """Render a concept id tuple as a slash-separated string."""
    if not cid:
        raise ConceptIdError("Empty concept id")
    for p in cid:
        validate_segment(p)
    return "/".join(cid)


def concept_id_from_path(bundle_root: Path, path: Path) -> ConceptId:
    """Compute the concept id for a markdown file path inside a bundle.

    The path must be inside `bundle_root`, must end in `.md`, and must not be
    a reserved filename (callers should filter reserved files first).
    """
    bundle_root = Path(bundle_root)
    path = Path(path)
    rel = path.resolve().relative_to(bundle_root.resolve())
    if rel.suffix != ".md":
        raise ConceptIdError(f"Not a markdown file: {path}")
    parts = tuple(rel.with_suffix("").parts)
    if not parts:
        raise ConceptIdError(f"Cannot derive concept id from {path}")
    for p in parts:
        validate_segment(p)
    return parts


def concept_id_to_path(bundle_root: Path, cid: ConceptId) -> Path:
    """Compute the filesystem path for a concept id inside a bundle."""
    if not cid:
        raise ConceptIdError("Empty concept id")
    for p in cid:
        validate_segment(p)
    *dirs, name = cid
    return Path(bundle_root).joinpath(*dirs, f"{name}.md")


def join_cid(parent: ConceptId, child: str) -> ConceptId:
    """Append a child segment to a concept id."""
    validate_segment(child)
    return parent + (child,)


def parent_cid(cid: ConceptId) -> ConceptId | None:
    """Return the parent concept id, or None if `cid` is at the bundle root."""
    return cid[:-1] if len(cid) > 1 else None


def ancestors(cid: ConceptId) -> Iterable[ConceptId]:
    """Yield ancestor concept ids (directories) from immediate parent upward."""
    for i in range(len(cid) - 1, 0, -1):
        yield cid[:i]


def path_within_bundle(candidate: Path, root: Path) -> bool:
    """True iff ``candidate.resolve()`` is ``root`` or lives beneath it.

    Canonical symlink-escape containment check (SPEC §4.5 / §11.1 security
    boundary; iter-2 P2-13). Resolves ``candidate`` once (following symlinks);
    on any resolution error returns False (fail closed). Used by:

      - :func:`okf_loom.viewer.assets.load_template` /
        :func:`~okf_loom.viewer.assets.load_static` /
        :func:`~okf_loom.viewer.assets.load_palette_override` (P1-4: a
        bundle override symlinked outside the bundle root is refused, falling
        closed to the builtin asset so a host secret is never served/built);
      - :func:`okf_loom.model.Bundle.load` concept path (P1-5: a symlinked
        concept ``.md`` resolving outside the bundle is skipped with a
        ``concept.path_escapes_bundle`` warning rather than crashing).

    Args:
        candidate: path to check (typically ``bundle.root / ".okf-loom" / ...``).
        root: bundle root to contain within.

    Returns:
        True iff ``candidate.resolve()`` equals ``root.resolve()`` or is a
        descendant of it.
    """
    try:
        resolved = Path(candidate).resolve()
        root_resolved = Path(root).resolve()
    except (OSError, RuntimeError):
        return False
    try:
        resolved.relative_to(root_resolved)
        return True
    except (ValueError, RuntimeError):
        return False
