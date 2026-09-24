"""Round-trip-preserving YAML frontmatter serialization.

Extracted verbatim from ``update.py`` (P1-24 subsystem) so the
style-preservation code has its own shallow leaf module instead of
being buried under ``update.py``'s "Apply a reviewed update plan" banner.
The navigator problem (``parse.py`` owns the lossy ``serialize_document``;
the style-preserving variant lived in ``update.py``) is resolved by giving
the style-preserving path its own importable home.

Public surface (re-exported from ``update.py`` for backward compat):

* :func:`serialize_document_round_trip` -- entry point used by every
  mutation in ``update.py``.
* :func:`patch_frontmatter_block` -- text-surgery primitive (also used
  directly by tests).

Everything else here is an internal helper (``_``-prefixed) and should not
be imported from outside this package.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import _yaml_compat as yaml

from .parse import (
    FRONTMATTER_DELIM,
    parse_document,
    serialize_document,
)


# ---------------------------------------------------------------------------
# Round-trip-preserving YAML serialization (P1-24)
# ---------------------------------------------------------------------------
#
# Problem: ``parse.serialize_document`` calls ``yaml.safe_dump`` with
# ``default_flow_style=False``. Every mutation (set-frontmatter / link-add /
# entity-add / update / repair / write-concept) round-trips through that
# serializer, so a 1-key add rewrites the ENTIRE frontmatter — turning flow
# ``[a, b]`` into block style, changing quote styles, and rewriting indents.
# A 1-key add produced a 54-line diff. This violates AGENTS.md
# "Diffable in git" and SPEC §3.3 ("Never hand-format YAML").
#
# Fix: capture the ORIGINAL frontmatter text from the source file and patch
# ONLY the lines that changed (text-surgery). When the change is too complex
# for surgery, fall back to ``safe_dump`` so we never produce wrong YAML.
#
# parse.py is owned by another bundle; we therefore do NOT change it. All
# style capture happens here, by re-parsing ``concept.raw_text``.

# Canonical position for newly-inserted top-level keys (SPEC §8.1: "include
# provided recommended keys in canonical order: type, title, description,
# resource, tags, timestamp, then any others").
_CANONICAL_KEY_ORDER: tuple[str, ...] = (
    "type", "title", "description", "resource", "tags", "timestamp",
)


def _split_document(text: str) -> tuple[str | None, str]:
    """Return ``(frontmatter_block, body)`` from a raw document text.

    ``frontmatter_block`` is the text BETWEEN the ``---`` delimiters (without
    the delimiters themselves), or ``None`` when the document has no
    frontmatter. The body is everything after the closing delimiter, with
    the single leading blank line stripped (mirroring ``parse_document``).
    """
    if text is None:
        return None, ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        return None, text
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONTMATTER_DELIM:
            end_idx = i
            break
    if end_idx is None:
        return None, text  # treat unterminated as no-frontmatter
    block = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:])
    if body.startswith("\n"):
        body = body[1:]
    return block, body


# A top-level YAML key line: ``key:``, ``key: value``, or ``key: "quoted"``.
# We deliberately restrict the key charset to identifier-ish characters so
# we don't false-match values like ``[a, b]`` (which start with ``[``).
_TOPLEVEL_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.\-]*):(?:\s|$)")


@dataclass
class _FmEntry:
    """One logical entry in a split frontmatter block.

    ``key`` is the empty string for leading comments / blank lines / lines
    that are not a top-level mapping key (those are folded into the previous
    entry's ``lines`` by the splitter).
    """
    key: str
    lines: list[str]


def _split_frontmatter_entries(block: str) -> list[_FmEntry]:
    """Split a frontmatter block into ordered entries.

    Each top-level ``key:`` starts a new entry. Subsequent indented lines
    (block-style collections), continued flow lines, blank lines, and
    comment lines are appended to the most recent entry's ``lines``. A
    leading run of comments / blanks before the first key is folded into a
    synthetic entry with ``key == ""`` so it round-trips byte-for-byte.
    """
    entries: list[_FmEntry] = []
    current: _FmEntry | None = None
    for line in block.split("\n"):
        m = _TOPLEVEL_KEY_RE.match(line)
        if m:
            if current is not None:
                entries.append(current)
            current = _FmEntry(key=m.group(1), lines=[line])
        else:
            if current is None:
                # Leading line(s) before the first top-level key. Keep them
                # in a synthetic "" entry so they survive round-trip.
                if not entries or entries[-1].key != "":
                    entries.append(_FmEntry(key="", lines=[]))
                entries[-1].lines.append(line)
            else:
                current.lines.append(line)
    if current is not None:
        entries.append(current)
    return entries


def _detect_scalar_style(line: str) -> str:
    """Detect the quote style of a scalar value on a ``key: value`` line.

    Returns one of ``"single"``, ``"double"``, or ``"plain"``.
    """
    # Find the value portion (everything after the first ``: ``).
    m = re.match(r"^[A-Za-z0-9_.\-]+:\s*(.*)$", line)
    if not m:
        return "plain"
    val = m.group(1).rstrip()
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        return "double"
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return "single"
    return "plain"


def _detect_collection_style(lines: list[str]) -> str:
    """Detect ``flow`` vs ``block`` style for a collection value.

    Returns ``"flow"`` for ``key: [a, b]`` / ``key: {a: 1}`` and ``"block"``
    for the indented ``- item`` / ``subkey: value`` form. Empty collections
    are detected from the inline ``[]`` / ``{}`` markers.
    """
    if not lines:
        return "block"
    first = lines[0]
    m = re.match(r"^[A-Za-z0-9_.\-]+:\s*(.*)$", first)
    val = (m.group(1) if m else first).rstrip()
    if val.startswith("[") or val.startswith("{"):
        return "flow"
    if val == "" or val.endswith("|") or val.endswith(">"):
        # Block scalar or block collection.
        return "block"
    return "block"


def _format_scalar(value: Any, style: str) -> str:
    """Format a scalar value as a YAML scalar token with the given style.

    Uses a single-key dict wrapper so ``safe_dump`` emits a mapping line
    (``_: <value>``) instead of a bare top-level scalar (which would carry
    a trailing ``...`` document-end marker). The ``_:`` prefix is stripped
    from the FIRST line only.

    P1-6(b): returns the FULL ``safe_dump`` output. For a multi-line
    string under plain or single-quote style, PyYAML emits a multi-line
    scalar (e.g. ``'Step 1.\\n\\n  Step 2.'`` spread across several
    physical lines). The previous implementation took only
    ``dumped.splitlines()[0]``, which truncated the scalar mid-quote and
    produced an unparseable block — the iter-2 validation gate caught
    the corruption and fell back to ``safe_dump``, destroying every
    comment in the file. Returning the full output lets the surrounding
    patch reassemble a parseable multi-line block so the gate PASSES and
    comments survive.
    """
    # P1-2/P2-1 (iter-4): dump the REAL typed value (int/bool/float/None),
    # NOT str(value). str(True)='True' becomes YAML string 'True' → gate
    # rejects (type mismatch) → safe_dump fallback → ALL comments destroyed.
    # Passing the native value lets safe_dump emit canonical YAML tokens
    # (true/false/1/1.5/null) so the gate PASSES and comments survive.
    s = value  # keep native type; yaml.safe_dump handles it correctly
    style_arg = '"' if style == "double" else ("'" if style == "single" else "")
    dumped = yaml.safe_dump(
        {"_": s}, default_style=style_arg, allow_unicode=True,
    )
    lines = dumped.splitlines()
    if not lines:
        return ""
    # Drop the ``_:`` prefix from the first line only; subsequent lines
    # (continuation of a multi-line scalar) are kept verbatim.
    first = lines[0]
    if ":" in first:
        lines[0] = first.split(":", 1)[1].lstrip()
    return "\n".join(lines)


def _detect_block_indent(orig_lines: list[str]) -> int | None:
    """Detect the sequence-indent used by a block-style collection.

    Returns the leading-space count on the first ``- `` line under the key,
    or ``None`` if no block items are present.
    """
    for line in orig_lines[1:] if len(orig_lines) > 1 else []:
        stripped = line.lstrip(" ")
        if stripped.startswith("- "):
            return len(line) - len(stripped)
        if stripped and not stripped.startswith("#"):
            # A non-comment, non-dash line at this indent — likely a dict
            # value (block mapping). Use its indent as the canonical.
            return len(line) - len(stripped)
    return None


class _IndentedDumper(yaml.SafeDumper):
    """SafeDumper marker subclass (kept for future tuning; PyYAML's
    ``best_sequence_indent`` attribute does not actually shift sequences
    under their parent key in current PyYAML releases — we post-process
    the output instead in :func:`_serialize_collection`).
    """


def _make_indented_dumper(seq_indent: int) -> type:
    """Return a SafeDumper subclass (no-op marker for now)."""
    return type(
        "_IndentedDumperN",
        (_IndentedDumper,),
        {"best_sequence_indent": max(2, seq_indent)},
    )


def _indent_block_seq(lines: list[str], indent: int) -> list[str]:
    """Post-process safe_dump output to indent block-sequence items.

    PyYAML aligns ``- item`` lines with their parent mapping key column by
    default. Most hand-written YAML indents sequence items UNDER the key
    by 2 spaces (or by ``indent`` spaces when explicitly detected). This
    helper shifts ALL lines belonging to a sequence item — including
    nested mapping keys inside dict-valued items — by ``indent`` columns.
    """
    out: list[str] = []
    # ``seq_col`` records the column at which top-level dash-led items
    # appear in the safe_dump output. While we walk lines inside a
    # sequence item, every line strictly deeper than ``seq_col`` belongs
    # to the current item and must be shifted too.
    in_seq = False
    seq_col = -1
    for line in lines:
        stripped = line.lstrip(" ")
        cur_indent = len(line) - len(stripped)
        if stripped.startswith("- "):
            if not in_seq:
                in_seq = True
                seq_col = cur_indent
            if cur_indent == seq_col:
                # Top-level sequence item.
                out.append(" " * indent + line)
            else:
                # A nested sequence item (deeper than seq_col). Preserve.
                out.append(line)
        else:
            if in_seq and cur_indent > seq_col:
                # This line is part of the current sequence item's body
                # (nested mapping keys, nested sequences, or wrapped
                # scalars). Indent it too so the item stays coherent.
                out.append(" " * indent + line)
            else:
                # Outside any sequence item — mapping key, scalar, etc.
                in_seq = False
                out.append(line)
    return out


def _serialize_collection(value: Any, *, flow: bool, seq_indent: int = 2) -> list[str]:
    """Serialize a list/dict as either flow or block style.

    Returns the BODY lines (no key prefix) ready to be appended after a
    ``key:`` line. For flow style, returns a single line containing the
    flow representation. For block style, returns multi-line indented YAML
    where sequences are indented ``seq_indent`` spaces UNDER their parent
    key (matching the common hand-written convention; PyYAML aligns
    sequences with the parent key column by default, so we post-process).
    """
    if flow:
        dumped = yaml.safe_dump(
            value, default_flow_style=True, allow_unicode=True, sort_keys=False,
        ).rstrip()
        return [dumped]
    # Wrap in a single-key dict so mappings get indented correctly under
    # the parent key. The wrapper key ``_`` is dropped before return.
    dumped = yaml.safe_dump(
        {"_": value}, default_flow_style=False, allow_unicode=True, sort_keys=False,
    ).rstrip()
    raw_lines = dumped.split("\n")
    if not raw_lines or raw_lines[0].rstrip() == "_:":
        body_lines = raw_lines[1:]
    else:
        body_lines = raw_lines
    return _indent_block_seq(body_lines, seq_indent)


def _format_key_value(key: str, value: Any) -> list[str]:
    """Format a NEW key/value pair using safe defaults (block collections,
    plain scalars when safe, quoted otherwise).

    Used when inserting a key that didn't previously exist (no original
    style to preserve).
    """
    if isinstance(value, (list, dict)):
        if isinstance(value, list) and all(
            not isinstance(v, (list, dict)) for v in value
        ) and value:
            # Simple list of scalars — emit as flow to keep diffs small
            # (matches the most common producer convention).
            dumped = yaml.safe_dump(
                value, default_flow_style=True, allow_unicode=True,
            ).rstrip()
            return [f"{key}: {dumped}"]
        # Dicts and nested lists → block style
        dumped = yaml.safe_dump(
            value, default_flow_style=False, allow_unicode=True, sort_keys=False,
        ).rstrip()
        lines = dumped.split("\n")
        # First line is ``key:``; subsequent lines are the indented block.
        # Ensure the key prefix is correct.
        if lines and lines[0].startswith(f"{key}:"):
            return lines
        # Re-format with key prefix.
        return [f"{key}:"] + lines
    # Scalar
    scalar = _format_scalar(value, "plain")
    # P1-6(b): a newly-inserted multi-line description (or any new key
    # whose value has newlines) must be split into multiple list entries
    # so the surrounding ``"\\n".join`` reassembles a parseable block.
    out = scalar.split("\n")
    out[0] = f"{key}: {out[0]}"
    return out


def _patch_value_lines(
    key: str,
    orig_lines: list[str],
    new_value: Any,
) -> list[str] | None:
    """Re-serialize a single key's value, preserving the original style.

    Returns the new lines for this entry, or ``None`` if the change is too
    complex for text-surgery (caller should fall back to safe_dump).
    """
    if not orig_lines:
        return None
    # ``orig_lines`` may include trailing comment / blank lines that the
    # entry splitter folded into this entry (they aren't part of the
    # value). Preserve them through the patch so comments survive the
    # round-trip; the gate's re-parse catches any case where preserving
    # them would corrupt a multi-line original block scalar.
    trailing = orig_lines[1:]
    # Case 1: scalar value.
    if not isinstance(new_value, (list, dict)):
        style = _detect_scalar_style(orig_lines[0])
        scalar = _format_scalar(new_value, style)
        # P1-6(b): ``_format_scalar`` may return a multi-line block scalar
        # (e.g. plain-style description with newlines, which PyYAML emits
        # as a multi-line single-quoted scalar). Split so each physical
        # line is a distinct entry; the caller's ``"\\n".join`` reassembles
        # them into a parseable block whose value round-trips.
        out = scalar.split("\n")
        out[0] = f"{key}: {out[0]}"
        # P1-2 (iter-5): preserve inline trailing comment from the ORIGINAL
        # value line (e.g. ``count: 1  # KEEP ME``). _format_scalar replaces
        # the value but drops the comment; extract it from orig_lines[0]
        # and re-append to the formatted first line. Heuristic: find `` #``
        # or ``\t#`` outside of quotes on the original line.
        orig_first = orig_lines[0]
        # Simple heuristic: look for '  #' or '\t#' (YAML comment marker
        # preceded by whitespace) NOT inside a quoted string. For robustness
        # we only extract if the line has a comment-like suffix AND the
        # original value was single-line (multi-line block scalars are
        # handled by the gate fallback).
        if len(orig_lines) == 1 or (len(orig_lines) > 1 and not orig_lines[1].strip().startswith("#")):
            # Only extract inline comment from the FIRST line itself.
            _comment = ""
            _in_squote = _in_dquote = False
            for _i, _c in enumerate(orig_first):
                if _c == "'" and not _in_dquote: _in_squote = not _in_squote
                elif _c == '"' and not _in_squote: _in_dquote = not _in_dquote
                elif _c == '#' and not _in_squote and not _in_dquote and _i > 0 and orig_first[_i-1] in ' \t':
                    _comment = orig_first[_i-1:]  # include the leading space
                    break
            if _comment:
                out[0] = out[0] + _comment
        out.extend(trailing)
        return out
    # Case 2: collection value. Preserve flow vs block.
    style = _detect_collection_style(orig_lines)
    if style == "block":
        # Detect the original per-item indent (relative to the parent key).
        seq_indent = _detect_block_indent(orig_lines) or 2
        body_lines = _serialize_collection(
            new_value, flow=False, seq_indent=seq_indent,
        )
        # Body lines are the indented value body (no key prefix). Prepend
        # ``key:`` to form the full entry.
        return [f"{key}:"] + body_lines
    # Flow style — single inline value.
    body_lines = _serialize_collection(new_value, flow=True)
    if not body_lines:
        return [f"{key}:"]
    return [f"{key}: {body_lines[0]}"]


def _canonical_insert_index(
    entries: list[_FmEntry], new_key: str
) -> int:
    """Compute the index in ``entries`` where a new key should be inserted.

    Spec §8.1 canonical order: type, title, description, resource, tags,
    timestamp, then any others. New keys not in the canonical list go to
    the END (after the last existing top-level key, before any trailing
    synthetic "" entry).
    """
    existing_keys = [e.key for e in entries if e.key]
    if new_key in _CANONICAL_KEY_ORDER:
        nki = _CANONICAL_KEY_ORDER.index(new_key)
        for i, ek in enumerate(existing_keys):
            if ek in _CANONICAL_KEY_ORDER:
                if _CANONICAL_KEY_ORDER.index(ek) > nki:
                    # Insert BEFORE the first existing canonical key that
                    # comes after the new one. Map the existing-keys index
                    # back to the entries index (entries may have leading
                    # synthetic "" entries).
                    return _entries_index_for_key(entries, ek)
        # New canonical key is after all existing canonical keys → append
        # at the end of the real keys.
        return _end_of_real_keys(entries)
    # Non-canonical key → append at end.
    return _end_of_real_keys(entries)


def _entries_index_for_key(entries: list[_FmEntry], key: str) -> int:
    for i, e in enumerate(entries):
        if e.key == key:
            return i
    return 0


def _end_of_real_keys(entries: list[_FmEntry]) -> int:
    """Index AFTER the last real-key entry (skipping trailing synthetic)."""
    last_real = -1
    for i, e in enumerate(entries):
        if e.key:
            last_real = i
    return last_real + 1


def _values_equal(a: Any, b: Any) -> bool:
    """Strict structural equality that distinguishes bool from int/float.

    Python's ``==`` treats ``True == 1`` and ``1 == 1.0`` as equal, which
    silently drops valid frontmatter mutations at every comparison site
    in the round-trip pipeline (``changed_keys`` detection, the validation
    gate, and the ``set_frontmatter`` idempotency guard): setting
    ``flag: true`` against an existing ``flag: 1`` reported ``same_value``
    and never wrote.

    This helper recurses into dict/list containers (order matters for
    lists; keys must match for dicts) and requires ``type(a) is type(b)``
    at scalar leaves so ``bool`` / ``int`` / ``float`` stay distinct.
    (``bool`` is a subclass of ``int`` in Python, so the explicit
    type-identity check is what separates them.)
    """
    # Container recursion (recurse element/key-wise; require same shape).
    if isinstance(a, dict) and isinstance(b, dict):
        if len(a) != len(b):
            return False
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_values_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_values_equal(x, y) for x, y in zip(a, b))
    # Scalar leaf: require exact type identity (bool vs int, int vs float,
    # str vs int, None vs anything). Returns False on any type mismatch
    # even when ``a == b`` under Python's coercion rules.
    if type(a) is not type(b):
        return False
    return a == b


def patch_frontmatter_block(
    original_block: str,
    original_fm: dict[str, Any],
    new_fm: dict[str, Any],
) -> str | None:
    """Patch ``original_block`` to reflect ``new_fm`` via minimal text surgery.

    Returns the patched block text, or ``None`` if the diff is too complex
    for text-surgery (caller should fall back to ``safe_dump``).

    Supported (simple) diffs:
      * Added top-level keys (inserted at canonical position).
      * Removed top-level keys (lines dropped).
      * Changed top-level scalar values (preserves quote style).
      * Changed top-level collection values (preserves flow vs block).

    Falls back (returns ``None``) when:
      * Key ORDER changed in a way we can't represent by simple
        insertion/removal (we don't reorder existing keys).
      * The original block has a top-level key whose value spans lines in a
        way we can't safely re-emit.
    """
    entries = _split_frontmatter_entries(original_block)

    added_keys = [k for k in new_fm if k not in original_fm]
    removed_keys = [k for k in original_fm if k not in new_fm]
    # P1-6(a): use strict type-aware equality so bool<->int/float changes
    # are detected (Python ``True == 1`` would otherwise silently drop
    # the change).
    changed_keys = [
        k for k in new_fm
        if k in original_fm and not _values_equal(original_fm[k], new_fm[k])
    ]

    # Detect ordering changes among keys present in BOTH dicts. If the
    # relative order of common keys differs, surgery can't represent it
    # without reordering — fall back.
    common_orig = [k for k in original_fm if k in new_fm]
    common_new = [k for k in new_fm if k in original_fm]
    if common_orig != common_new:
        return None

    # 1) Remove deleted keys' entries.
    if removed_keys:
        entries = [e for e in entries if e.key not in removed_keys]

    # 2) Patch changed keys' value lines in place.
    entry_by_key = {e.key: e for e in entries if e.key}
    for k in changed_keys:
        e = entry_by_key.get(k)
        if e is None:
            return None  # shouldn't happen (k in both)
        patched = _patch_value_lines(k, list(e.lines), new_fm[k])
        if patched is None:
            return None
        e.lines = patched

    # 3) Insert added keys at canonical positions.
    # Sort additions so canonical-order siblings land in the right order
    # relative to each other.
    added_keys_sorted = sorted(
        added_keys,
        key=lambda k: (
            _CANONICAL_KEY_ORDER.index(k)
            if k in _CANONICAL_KEY_ORDER
            else len(_CANONICAL_KEY_ORDER)
        ),
    )
    for k in added_keys_sorted:
        new_lines = _format_key_value(k, new_fm[k])
        new_entry = _FmEntry(key=k, lines=new_lines)
        idx = _canonical_insert_index(entries, k)
        entries.insert(idx, new_entry)
        # Re-resolve the entry_by_key view (cheap; entries is small).
        entry_by_key = {e.key: e for e in entries if e.key}

    # Reassemble.
    out_lines: list[str] = []
    for e in entries:
        out_lines.extend(e.lines)
    return "\n".join(out_lines)


def serialize_document_round_trip(
    original_raw_text: str | None,
    new_frontmatter: dict[str, Any],
    new_body: str,
) -> str:
    """Serialize a document, preserving original frontmatter formatting.

    P1-24: when ``original_raw_text`` carries a frontmatter block, patches
    it via :func:`patch_frontmatter_block` so flow style, quote style,
    comments, and key order survive. Falls back to :func:`safe_dump` when
    the diff is too complex for text-surgery (or when there was no original
    frontmatter, e.g. a newly-created concept).
    """
    body = new_body if new_body.endswith("\n") else new_body + "\n"

    if not new_frontmatter:
        return body

    if not original_raw_text:
        return serialize_document(new_frontmatter, body)

    original_block, _ = _split_document(original_raw_text)
    if original_block is None:
        # Original had no frontmatter (e.g. plain markdown) → safe_dump.
        return serialize_document(new_frontmatter, body)

    try:
        original_fm, _ = parse_document(original_raw_text)
    except Exception:
        return serialize_document(new_frontmatter, body)

    patched = patch_frontmatter_block(original_block, original_fm, new_frontmatter)
    if patched is None:
        return serialize_document(new_frontmatter, body)

    # P0-1/P0-2 (iter-2): VALIDATION GATE. The text-surgery in
    # patch_frontmatter_block is a complexity bomb that corrupts data on
    # multi-line scalar values (unterminated quoted scalar) and on nested
    # list-of-dicts shapes (e.g. entities with aliases). Re-parse the patched
    # block; if it fails OR yields a different dict than desired, fall back to
    # safe_dump. This converts silent data corruption into "loses style
    # preservation" — a strictly better failure mode. ~6 LOC; strictly
    # behavior-preserving for every case text-surgery handles correctly.
    try:
        reparsed_fm, _ = parse_document(
            f"{FRONTMATTER_DELIM}\n{patched}\n{FRONTMATTER_DELIM}\n\nbody\n"
        )
    except Exception:
        reparsed_fm = None
    # P1-6(a): strict type-aware equality so a re-parsed ``flag: True`` is
    # NOT considered equal to a desired ``flag: 1`` (Python ``True == 1``
    # would otherwise mask a real semantic change).
    if reparsed_fm is None or not _values_equal(reparsed_fm, new_frontmatter):
        # Text-surgery corrupted semantics (multi-line scalar, nested list,
        # block scalar, anchor/alias, etc.). Fall back to a correct (if not
        # style-preserved) safe_dump so we NEVER write an unparseable file.
        return serialize_document(new_frontmatter, body)

    return f"{FRONTMATTER_DELIM}\n{patched}\n{FRONTMATTER_DELIM}\n\n{body}"
