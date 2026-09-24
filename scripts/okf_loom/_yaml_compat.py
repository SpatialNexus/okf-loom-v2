"""YAML compatibility layer for checkout-local OKF runtime.

The preferred parser/dumper is PyYAML when it is available in the active
Python environment. A small built-in fallback covers the conservative YAML
subset used by OKF bundles and ``okf-loom.config.yaml`` so the checked-out skill can
run without requiring a PyYAML installation step.

The fallback is intentionally fail-closed. It accepts mappings, sequences,
flow lists/maps, scalars, quoted strings, and simple block scalars. It rejects
anchors, aliases, tags, directives, multi-document streams, and other advanced
YAML features instead of guessing.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable


_FORCE_FALLBACK = os.environ.get("OKF_LOOM_FORCE_BUILTIN_YAML") == "1"

if not _FORCE_FALLBACK:  # pragma: no cover - exercised by normal runtime
    try:
        import yaml as _pyyaml  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover - exercised by subprocess proof
        _pyyaml = None
else:  # pragma: no cover - exercised by subprocess proof
    _pyyaml = None


if _pyyaml is not None:
    YAMLError = _pyyaml.YAMLError
    SafeLoader = _pyyaml.SafeLoader
    SafeDumper = _pyyaml.SafeDumper

    def safe_load(stream: Any) -> Any:
        return _pyyaml.safe_load(stream)

    def safe_dump(data: Any, *args: Any, **kwargs: Any) -> str:
        return _pyyaml.safe_dump(data, *args, **kwargs)

    def load(stream: Any, Loader: type | None = None) -> Any:  # noqa: N803 - PyYAML API
        return _pyyaml.load(stream, Loader or SafeLoader)

else:
    class YAMLError(Exception):
        """Fallback YAML parse/dump error."""

    class SafeLoader:
        """Compatibility marker for PyYAML's SafeLoader."""

    class SafeDumper:
        """Compatibility marker for PyYAML's SafeDumper."""

    @dataclass(frozen=True)
    class _Line:
        indent: int
        text: str

    _UNSUPPORTED_RE = re.compile(
        r"(^|[\s\[\{,])([&*][A-Za-z0-9_.-]+|![A-Za-z0-9_./:-]*|!!|"
        r"%[A-Za-z][A-Za-z0-9_.-]*|---\s*$|\.\.\.\s*$)",
        re.MULTILINE,
    )

    def safe_load(stream: Any) -> Any:
        text = _read_text(stream)
        if not text.strip():
            return None
        if _UNSUPPORTED_RE.search(text) or re.search(r"^\s*<<\s*:", text, re.MULTILINE):
            raise YAMLError(
                "built-in YAML fallback rejects anchors, aliases, tags, "
                "directives, merge keys, and multi-document streams"
            )
        lines = _prepare_lines(text)
        if not lines:
            return None
        if len(lines) == 1 and lines[0].text.startswith(("{", "[")):
            return _parse_scalar(lines[0].text)
        value, idx = _parse_block(lines, 0, lines[0].indent)
        if idx != len(lines):
            raise YAMLError(f"unexpected YAML content: {lines[idx].text!r}")
        return value

    def load(stream: Any, Loader: type | None = None) -> Any:  # noqa: N803 - PyYAML API
        return safe_load(stream)

    def safe_dump(
        data: Any,
        *args: Any,
        sort_keys: bool = True,
        allow_unicode: bool = True,
        default_flow_style: bool | None = None,
        default_style: str | None = None,
        **kwargs: Any,
    ) -> str:
        flow = bool(default_flow_style)
        if flow:
            return _dump_flow(data, default_style=default_style) + "\n"
        lines = _dump_block(data, 0, sort_keys=sort_keys, default_style=default_style)
        return "\n".join(lines) + "\n"

    def _read_text(stream: Any) -> str:
        if hasattr(stream, "read"):
            return stream.read()
        return "" if stream is None else str(stream)

    def _prepare_lines(text: str) -> list[_Line]:
        out: list[_Line] = []
        for raw in text.splitlines():
            if raw.strip() == "" or raw.lstrip().startswith("#"):
                continue
            if raw.startswith("\t"):
                raise YAMLError("tabs are not supported in indentation")
            indent = len(raw) - len(raw.lstrip(" "))
            out.append(_Line(indent=indent, text=_strip_inline_comment(raw[indent:]).rstrip()))
        return out

    def _strip_inline_comment(text: str) -> str:
        in_single = False
        in_double = False
        escape = False
        for i, ch in enumerate(text):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_double:
                escape = True
                continue
            quote_start = i == 0 or text[i - 1].isspace() or text[i - 1] in "[{,:"
            if ch == "'" and in_single and i + 1 < len(text) and text[i + 1] == "'":
                escape = True
                continue
            if ch == "'" and not in_double and (in_single or quote_start):
                in_single = not in_single
                continue
            if ch == '"' and not in_single and (in_double or quote_start):
                in_double = not in_double
                continue
            if ch == "#" and not in_single and not in_double and (i == 0 or text[i - 1].isspace()):
                return text[:i].rstrip()
        return text

    def _parse_block(lines: list[_Line], idx: int, indent: int) -> tuple[Any, int]:
        if idx >= len(lines) or lines[idx].indent < indent:
            return {}, idx
        if lines[idx].text.startswith("- "):
            return _parse_list(lines, idx, indent)
        return _parse_map(lines, idx, indent)

    def _parse_map(lines: list[_Line], idx: int, indent: int) -> tuple[dict[str, Any], int]:
        result: dict[str, Any] = {}
        while idx < len(lines):
            line = lines[idx]
            if line.indent < indent:
                break
            if line.indent > indent:
                raise YAMLError(f"unexpected indentation before {line.text!r}")
            if line.text.startswith("- "):
                break
            key, value_text = _split_key_value(line.text)
            idx += 1
            if value_text in {"|", ">"}:
                value, idx = _parse_block_scalar(lines, idx, line.indent, folded=(value_text == ">"))
            elif value_text == "":
                # YAML permits a mapping's sequence value at the key's indent.
                if idx < len(lines) and lines[idx].indent == line.indent and lines[idx].text.startswith("- "):
                    value, idx = _parse_list(lines, idx, line.indent)
                elif idx < len(lines) and lines[idx].indent > line.indent:
                    value, idx = _parse_block(lines, idx, lines[idx].indent)
                else:
                    value = None
            else:
                value = _parse_scalar(value_text)
            if key in result:
                raise YAMLError(f"duplicate YAML key {key!r}")
            result[key] = value
        return result, idx

    def _parse_list(lines: list[_Line], idx: int, indent: int) -> tuple[list[Any], int]:
        result: list[Any] = []
        while idx < len(lines):
            line = lines[idx]
            if line.indent < indent or line.indent != indent or not line.text.startswith("- "):
                break
            rest = line.text[2:].strip()
            idx += 1
            if rest == "":
                if idx < len(lines) and lines[idx].indent > indent:
                    value, idx = _parse_block(lines, idx, lines[idx].indent)
                else:
                    value = None
            elif _looks_like_key_value(rest):
                # Treat the compact first key just like subsequent mapping keys,
                # including nested/indentless sequences and duplicate checks.
                first = idx - 1
                lines[first] = _Line(indent + 2, rest)
                try:
                    value, idx = _parse_map(lines, first, indent + 2)
                finally:
                    lines[first] = line
            else:
                value = _parse_scalar(rest)
            result.append(value)
        return result, idx

    def _parse_block_scalar(lines: list[_Line], idx: int, parent_indent: int, *, folded: bool) -> tuple[str, int]:
        body: list[str] = []
        scalar_indent: int | None = None
        while idx < len(lines) and lines[idx].indent > parent_indent:
            if scalar_indent is None:
                scalar_indent = lines[idx].indent
            cut = min(lines[idx].indent, scalar_indent)
            body.append(" " * (lines[idx].indent - cut) + lines[idx].text)
            idx += 1
        if folded:
            return " ".join(part.strip() for part in body).rstrip() + "\n", idx
        return "\n".join(body) + ("\n" if body else ""), idx

    def _key_separator(text: str, *, flow: bool = False) -> int | None:
        # A colon within a plain key (or a URL/actor scalar) is not a
        # separator. Quoted keys may omit separation space only in flow maps.
        if text.startswith(("'", '"')):
            match = re.match(r'''(?:'(?:[^']|'')*'|"(?:[^"\\]|\\.)*")''', text)
            if not match:
                raise YAMLError(f"invalid quoted YAML key {text!r}")
            pos = match.end()
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos < len(text) and text[pos] == ":" and (
                flow or pos + 1 == len(text) or text[pos + 1].isspace()
            ):
                return pos
            return None
        for pos, ch in enumerate(text):
            if ch == ":" and (pos + 1 == len(text) or text[pos + 1].isspace()
                              or (flow and text[pos + 1] in "[]{},")):
                return pos
        return None

    def _split_key_value(text: str, *, flow: bool = False) -> tuple[str, str]:
        pos = _key_separator(text, flow=flow)
        if pos is None:
            raise YAMLError(f"expected mapping key/value, got {text!r}")
        key, value = text[:pos].strip(), text[pos + 1:]
        if key.startswith(("'", '"')):
            key = _parse_scalar(key)
        elif not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:'\" /-]*", key) or key.lower() in {
            "true", "false", "null", "yes", "no", "on", "off",
        }:
            raise YAMLError(f"unsupported YAML key {key!r}")
        return key, value.strip()

    def _looks_like_key_value(text: str) -> bool:
        return not text.startswith(("{", "[")) and _key_separator(text) is not None

    def _parse_scalar(text: str) -> Any:
        text = text.strip()
        if text == "":
            return ""
        if _starts_unsupported_token(text):
            raise YAMLError(
                "built-in YAML fallback rejects anchors, aliases, tags, "
                "directives, merge keys, and multi-document streams"
            )
        if _has_unbalanced_flow_edge(text):
            raise YAMLError(f"unbalanced YAML flow scalar {text!r}")
        if text.startswith("[") and text.endswith("]"):
            return [_parse_scalar(part) for part in _split_flow(text[1:-1])]
        if text.startswith("{") and text.endswith("}"):
            result: dict[str, Any] = {}
            for part in _split_flow(text[1:-1]):
                key, value = _split_key_value(part, flow=True)
                if key in result:
                    raise YAMLError(f"duplicate YAML key {key!r}")
                result[key] = _parse_scalar(value) if value else None
            return result
        if _has_unbalanced_quotes(text):
            raise YAMLError(f"invalid quoted scalar {text!r}")
        if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
            try:
                if text.startswith("'"):
                    if not re.fullmatch(r"'(?:[^']|'')*'", text):
                        raise ValueError("invalid single-quoted YAML string")
                    return text[1:-1].replace("''", "'")
                return _decode_double_quoted(text)
            except (SyntaxError, ValueError) as e:
                raise YAMLError(f"invalid quoted scalar {text!r}") from e
        low = text.lower()
        if low in {"null", "~"}:
            return None
        if low == "true":
            return True
        if low == "false":
            return False
        if re.fullmatch(r"[-+]?\d+", text):
            try:
                return int(text)
            except ValueError:
                pass
        if re.fullmatch(r"[-+]?(\d+\.\d*|\d*\.\d+)([eE][-+]?\d+)?", text):
            try:
                return float(text)
            except ValueError:
                pass
        return text

    _YAML_ESCAPES = {
        "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t",
        "n": "\n", "v": "\v", "f": "\f", "r": "\r", "e": "\x1b",
        " ": " ", '"': '"', "/": "/", "\\": "\\",
        "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029",
    }

    def _decode_double_quoted(text: str) -> str:
        """Decode single-line YAML strings, not Python or JSON escapes.

        PyYAML emits YAML-only escapes even for ordinary Unicode metadata.
        Consume each escape once so escaped backslashes stay literal; reject
        unknown/truncated escapes and non-Unicode scalar values without repair.
        Multiline quoted scalars remain outside the fallback's supported subset.
        """
        body = text[1:-1]
        result: list[str] = []
        idx = 0
        while idx < len(body):
            ch = body[idx]
            idx += 1
            if ch == "\\":
                if idx == len(body):
                    raise ValueError("truncated YAML escape")
                escape = body[idx]
                idx += 1
                if escape in _YAML_ESCAPES:
                    result.append(_YAML_ESCAPES[escape])
                    continue
                width = {"x": 2, "u": 4, "U": 8}.get(escape)
                if width is None:
                    raise ValueError("unknown YAML escape")
                digits = body[idx:idx + width]
                if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                    raise ValueError("invalid YAML hexadecimal escape")
                point = int(digits, 16)
                if point > 0x10FFFF or 0xD800 <= point <= 0xDFFF:
                    raise ValueError("invalid Unicode scalar in YAML escape")
                result.append(chr(point))
                idx += width
            else:
                if ch == '"' or (ord(ch) < 0x20 and ch != "\t"):
                    raise ValueError("invalid character in quoted YAML string")
                result.append(ch)
        return "".join(result)

    def _starts_unsupported_token(text: str) -> bool:
        return bool(re.match(r"([&*][A-Za-z0-9_.-]+|![A-Za-z0-9_./:-]*|!!|%[A-Za-z][A-Za-z0-9_.-]*)", text))

    def _has_unbalanced_flow_edge(text: str) -> bool:
        pairs = {"[": "]", "{": "}"}
        closers = {"]": "[",
            "}": "{",
        }
        if text[0] in pairs and not text.endswith(pairs[text[0]]):
            return True
        if text[-1] in closers and not text.startswith(closers[text[-1]]):
            return True
        return False

    def _has_unbalanced_quotes(text: str) -> bool:
        if text[0] in {'"', "'"}:
            return not (len(text) >= 2 and text.endswith(text[0]))
        if text[-1] in {'"', "'"}:
            return True
        return False

    def _split_flow(text: str) -> list[str]:
        parts: list[str] = []
        cur: list[str] = []
        depth = 0
        in_single = False
        in_double = False
        escape = False
        for i, ch in enumerate(text):
            if escape:
                cur.append(ch)
                escape = False
                continue
            if ch == "\\" and in_double:
                cur.append(ch)
                escape = True
                continue
            quote_start = i == 0 or text[i - 1].isspace() or text[i - 1] in "[{,:"
            if ch == "'" and in_single and i + 1 < len(text) and text[i + 1] == "'":
                cur.append(ch)
                escape = True
                continue
            if ch == "'" and not in_double and (in_single or quote_start):
                in_single = not in_single
            elif ch == '"' and not in_single and (in_double or quote_start):
                in_double = not in_double
            elif not in_single and not in_double:
                if ch in "[{":
                    depth += 1
                elif ch in "]}":
                    depth -= 1
                    if depth < 0:
                        raise YAMLError(f"unbalanced YAML flow value {text!r}")
                elif ch == "," and depth == 0:
                    part = "".join(cur).strip()
                    if part:
                        parts.append(part)
                    cur = []
                    continue
            cur.append(ch)
        part = "".join(cur).strip()
        if part:
            parts.append(part)
        if in_single or in_double or depth != 0:
            raise YAMLError(f"unbalanced YAML flow value {text!r}")
        return parts

    def _dump_flow(data: Any, *, default_style: str | None = None) -> str:
        if isinstance(data, dict):
            return "{" + ", ".join(f"{_dump_key(k)}: {_dump_flow(v, default_style=default_style)}" for k, v in data.items()) + "}"
        if isinstance(data, (list, tuple)):
            return "[" + ", ".join(_dump_flow(v, default_style=default_style) for v in data) + "]"
        return _dump_scalar(data, default_style=default_style)

    def _dump_block(data: Any, indent: int, *, sort_keys: bool, default_style: str | None = None) -> list[str]:
        pad = " " * indent
        if isinstance(data, dict):
            if not data:
                return [pad + "{}"]
            items: Iterable[tuple[Any, Any]] = data.items()
            if sort_keys:
                items = sorted(data.items(), key=lambda kv: str(kv[0]))
            lines: list[str] = []
            for key, value in items:
                key_s = _dump_key(key)
                if isinstance(value, dict):
                    if not value:
                        lines.append(f"{pad}{key_s}: {{}}")
                    else:
                        lines.append(f"{pad}{key_s}:")
                        lines.extend(_dump_block(value, indent + 2, sort_keys=sort_keys, default_style=default_style))
                elif isinstance(value, list):
                    if not value:
                        lines.append(f"{pad}{key_s}: []")
                    else:
                        lines.append(f"{pad}{key_s}:")
                        lines.extend(_dump_block(value, indent + 2, sort_keys=sort_keys, default_style=default_style))
                else:
                    lines.append(f"{pad}{key_s}: {_dump_scalar(value, default_style=default_style)}")
            return lines
        if isinstance(data, list):
            lines = []
            for item in data:
                if isinstance(item, (dict, list)):
                    # Keep complex list items in flow form so the fallback
                    # parser can round-trip its own output without relying on
                    # the YAML shorthand bare dash line (``-``), which the
                    # conservative parser intentionally does not accept.
                    lines.append(f"{pad}- {_dump_flow(item, default_style=default_style)}")
                else:
                    lines.append(f"{pad}- {_dump_scalar(item, default_style=default_style)}")
            return lines
        return [pad + _dump_scalar(data, default_style=default_style)]

    def _dump_scalar(value: Any, *, default_style: str | None = None) -> str:
        if value is None:
            return "null"
        if value is True:
            return "true"
        if value is False:
            return "false"
        if isinstance(value, (int, float)):
            return str(value)
        s = str(value)
        if default_style in {'"', "'"}:
            return "'" + s.replace("'", "''") + "'" if default_style == "'" and "\n" not in s else _double_quote(s)
        if "\n" in s:
            return _double_quote(s)
        if s == "" or re.search(r"[:#\[\]{}&,*!|>'\"%@`]|^[-?]|\s$|^\s", s):
            return _double_quote(s)
        low = s.lower()
        if low in {"true", "false", "null", "~"} or re.fullmatch(r"[-+]?\d+(\.\d*)?", s):
            return _double_quote(s)
        return s

    def _double_quote(s: str) -> str:
        return json.dumps(s, ensure_ascii=False)

    def _dump_key(key: Any) -> str:
        if not isinstance(key, str):
            raise YAMLError("built-in YAML fallback requires string mapping keys")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", key) and key.lower() not in {
            "true", "false", "null", "yes", "no", "on", "off",
        }:
            return key
        return _double_quote(key)
