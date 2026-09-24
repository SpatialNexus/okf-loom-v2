"""Pure Proposal-v1 operation runtime over captured UTF-8 bytes."""
from __future__ import annotations

import re
import posixpath
from types import MappingProxyType
from typing import Any, Mapping

from ..parse import parse_document
from ..roundtrip import serialize_document_round_trip
from .errors import DocumentCodecError, OperationExecutionError, UndeclaredReadError
from .ports import DecodedDocument, DocumentCodec
from .schemas import Operation


def _strict_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_strict_equal(left[k], right[k]) for k in left)
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(_strict_equal(a, b) for a, b in zip(left, right))
    return left == right


def _mutable_json(value: Any) -> Any:
    """Detach schema FrozenDict/FrozenList values for the YAML serializer."""
    if isinstance(value, dict):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mutable_json(item) for item in value]
    return value


class RoundTripDocumentCodec:
    """Production parse/round-trip semantics without a Bundle or live path."""

    def decode(self, data: bytes) -> DecodedDocument:
        try:
            text = data.decode("utf-8", errors="strict")
            frontmatter, body = parse_document(text)
        except Exception as exc:
            # The broad parse wrapper is intentional: YAML implementation
            # details are not allowed to leak across this pure protocol.
            if isinstance(exc, DocumentCodecError):
                raise
            raise DocumentCodecError(f"document cannot be decoded: {exc}") from exc
        return DecodedDocument(MappingProxyType(dict(frontmatter)), body)

    def encode(self, original: bytes, document: DecodedDocument) -> bytes:
        try:
            raw = original.decode("utf-8", errors="strict")
            return serialize_document_round_trip(raw, dict(document.frontmatter), document.body).encode("utf-8")
        except Exception as exc:
            raise DocumentCodecError(f"document cannot be encoded: {exc}") from exc


class DeclaredReads(Mapping[str, bytes]):
    """Guarded operation view which fails closed on undeclared access."""

    def __init__(self, values: Mapping[str, bytes], declared: tuple[str, ...], op_id: str) -> None:
        self._values = values
        self._declared = frozenset(declared)
        self._op_id = op_id

    def __getitem__(self, key: str) -> bytes:
        if key not in self._declared:
            raise UndeclaredReadError(f"operation {self._op_id} attempted undeclared read {key!r}", op_id=self._op_id, concept=key)
        try:
            return self._values[key]
        except KeyError as exc:
            raise OperationExecutionError(f"declared concept {key!r} is absent", op_id=self._op_id, concept=key) from exc

    def __iter__(self):
        return iter(sorted(self._declared))

    def __len__(self) -> int:
        return len(self._declared)


def _sections(body: str) -> list[dict[str, Any]]:
    heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
    fence_re = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
    spans: list[dict[str, Any]] = []
    offset = 0
    fence_char: str | None = None
    fence_len = 0
    for line in body.splitlines(keepends=True):
        plain = line.rstrip("\r\n")
        fence = fence_re.match(plain)
        if fence:
            marker = fence.group(1)
            if fence_char is None:
                fence_char, fence_len = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                fence_char = None
        elif fence_char is None:
            match = heading_re.match(plain)
            if match:
                spans.append({"level": len(match.group(1)), "text": match.group(2).strip(),
                              "start": offset, "content_start": offset + len(line)})
        offset += len(line)
    for index, span in enumerate(spans):
        span["end"] = next((n["start"] for n in spans[index + 1:] if n["level"] <= span["level"]), len(body))
    return spans


def _heading_spec(value: str) -> tuple[int | None, str]:
    match = re.match(r"^(#{1,6})\s+(.+?)\s*$", value.strip())
    return (len(match.group(1)), match.group(2).strip()) if match else (None, value.strip())


def _append_relation(fm: dict[str, Any], destination: str, relation: str | None, detail: str | None) -> bool:
    if relation is None:
        return False
    entry = {"target": destination, "type": relation, "detail": detail or ""}
    current = fm.get("relations")
    if isinstance(current, list):
        if any(isinstance(item, dict) and item.get("target") == destination and item.get("type") == relation for item in current):
            return False
        current.append(entry)
    else:
        fm["relations"] = [entry]
    return True


def _has_concept_link(body: str, source: str, destination: str) -> bool:
    outside = _body_outside_fences(body)
    if re.search(r"\[\[\s*" + re.escape(destination) + r"(?:[#|\]])", outside):
        return True
    for match in re.finditer(r"\]\(\s*([^\s)]+)", outside):
        raw = match.group(1).split("#", 1)[0].split("?", 1)[0]
        if not raw.endswith(".md") or "://" in raw:
            continue
        if raw.startswith("/"):
            concept = raw[1:-3]
        else:
            concept = posixpath.normpath(posixpath.join(posixpath.dirname(source), raw[:-3]))
        if concept == destination:
            return True
    return False


def execute_operation(operation: Operation, reads: Mapping[str, bytes], *, codec: DocumentCodec | None = None) -> bytes:
    """Execute one validated operation and return exact target bytes.

    Identical semantic requests are deterministic no-ops and return the
    original byte object's value, avoiding serializer newline churn.
    """
    codec = codec or RoundTripDocumentCodec()
    guarded = reads if isinstance(reads, DeclaredReads) else DeclaredReads(reads, operation.reads, operation.op_id)
    original = guarded[operation.target]
    document = codec.decode(original)
    fm, body = dict(document.frontmatter), document.body
    changed = False
    args = operation.args

    if operation.kind == "set_frontmatter":
        current = fm.get(args["key"])
        if not _strict_equal(current, args["value"]):
            fm[args["key"]] = _mutable_json(args["value"])
            changed = True
    elif operation.kind == "entity_add":
        entry: dict[str, Any] = {"label": args["label"], "kind": args["kind"]}
        if args["entity_id"] is not None:
            entry["id"] = args["entity_id"]
        current = fm.get("entities")
        exists = isinstance(current, list) and any(
            (isinstance(item, dict) and str(item.get("label", "")).casefold() == args["label"].casefold()
             and str(item.get("kind", "")).casefold() == args["kind"].casefold())
            or (isinstance(item, str) and item.casefold() == args["label"].casefold()) for item in current
        )
        if not exists:
            if isinstance(current, list): current.append(entry)
            else: fm["entities"] = [entry]
            changed = True
    elif operation.kind == "link_add":
        # Access is intentional even for an already-present link: destination
        # existence is part of the fail-closed operation contract.
        guarded[args["destination"]]
        target = f"/{args['destination']}.md"
        linked = _has_concept_link(body, operation.target, args["destination"])
        if not linked:
            body = body.rstrip("\r\n") + f"\n\n* [{args['label']}]({target})\n"
            changed = True
        changed = _append_relation(fm, args["destination"], args["relation"], args["detail"]) or changed
    elif operation.kind == "replace_text":
        count = body.count(args["old"])
        occurrence = args["occurrence"]
        if occurrence == "only":
            if count != 1:
                raise OperationExecutionError(f"replace_text 'only' requires exactly one match, found {count}", op_id=operation.op_id, concept=operation.target)
            body = body.replace(args["old"], args["new"], 1); changed = args["old"] != args["new"]
        else:
            if count < occurrence:
                raise OperationExecutionError(f"replace_text occurrence {occurrence} not found (matches: {count})", op_id=operation.op_id, concept=operation.target)
            pieces = body.split(args["old"])
            body = args["old"].join(pieces[:occurrence]) + args["new"] + args["old"].join(pieces[occurrence:])
            changed = args["old"] != args["new"]
    elif operation.kind == "update_section":
        level, text = _heading_spec(args["heading"])
        matches = [s for s in _sections(body) if s["text"].casefold() == text.casefold() and (level is None or s["level"] == level)]
        if len(matches) != 1:
            raise OperationExecutionError(f"update_section heading must resolve exactly once, found {len(matches)}", op_id=operation.op_id, concept=operation.target)
        section = matches[0]
        fragment = args["body"].strip("\r\n")
        first, sep, rest = fragment.partition("\n")
        first_level, first_text = _heading_spec(first.rstrip("\r"))
        if first_level is not None and first_text.casefold() == section["text"].casefold(): fragment = rest if sep else ""
        tail = body[section["end"]:]
        if args["mode"] == "append":
            prefix = body[:section["end"]]
            if not prefix.endswith("\n"):
                prefix += "\n"
            candidate = prefix.rstrip("\n") + "\n\n" + fragment + "\n" + ("\n" if tail else "") + tail
        else:
            middle = ("\n" + fragment + "\n") if fragment else "\n"
            if tail: middle += "\n"
            candidate = body[:section["content_start"]] + middle + tail
        changed, body = candidate != body, candidate
    else:  # guarded by Operation schema, retained for defense in depth
        raise OperationExecutionError(f"unsupported operation kind {operation.kind!r}", op_id=operation.op_id)

    if not changed:
        return original
    return codec.encode(original, DecodedDocument(MappingProxyType(fm), body))


def _body_outside_fences(body: str) -> str:
    lines: list[str] = []
    in_fence = False
    marker = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
    for line in body.splitlines(keepends=True):
        if marker.match(line):
            in_fence = not in_fence
            lines.append("\n" if line.endswith(("\n", "\r")) else "")
        elif in_fence:
            lines.append("\n" if line.endswith(("\n", "\r")) else "")
        else:
            lines.append(line)
    return "".join(lines)


__all__ = ["RoundTripDocumentCodec", "DeclaredReads", "execute_operation"]
