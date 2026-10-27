"""Plain, non-executable reader previews; exact evidence stays source-owned.

Keep this projection aligned with static/readable-text.js, including table cells,
code/escapes, links, entities and emphasis. Never apply it to retrieval records.
"""
from __future__ import annotations

import re

_ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'",
             "nbsp": " ", "mdash": "—", "ndash": "–", "hellip": "…"}


def readable_text(markdown: str | None) -> str:
    escaped: list[str] = []

    def stash(value: str) -> str:
        escaped.append(value)
        return f"\uE000{len(escaped) - 1}\uE001"

    text = str(markdown or "")
    text = re.sub(r"\\([!-\/:-@\[-`{-~])", lambda m: stash(m[1]), text)
    text = re.sub(r"^[ \t]{0,3}(?:`{3,}|~{3,})[^\n]*$", " ", text, flags=re.M)
    text = re.sub(r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$", " ", text, flags=re.M)
    text = re.sub(r"^[ \t]*\|(.*)\|[ \t]*$", lambda m: " · ".join(c.strip() for c in m[1].split("|") if c.strip()), text, flags=re.M)
    text = re.sub(r"^[ \t]{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$", " ", text, flags=re.M)
    text = re.sub(r"^[ \t]{0,3}(?:>[ \t]?)+", "", text, flags=re.M)
    text = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$", r"\1", text, flags=re.M)
    text = re.sub(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?", "", text, flags=re.M)
    text = re.sub(r"(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)", lambda m: stash(m[2]), text)
    text = re.sub(r"<!--[\s\S]*?-->", " ", text)
    text = re.sub(r"<((?:https?|mailto):[^<>\s]+)>", r"\1", text, flags=re.I)
    text = re.sub(r"</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>", " ", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r'''\[([^\]]+)\]\((?:[^()\s]|\([^()\s]*\))*(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)''', r"\1", text)
    text = re.sub(r"!?\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"\[\^[^\]\s]+\]", "", text)
    text = re.sub(r"(\*{1,3}|~~)(?=\S)([\s\S]*?\S)\1", r"\2", text)
    text = re.sub(r"(^|[^0-9A-Za-z_])(_{1,3})(?=\S)([\s\S]*?\S)\2(?![0-9A-Za-z_])", r"\1\3", text)

    def entity(match: re.Match) -> str:
        name = match[1].lower()
        if name.startswith("#"):
            code = int(name[2:], 16) if name.startswith("#x") else int(name[1:])
            return chr(code) if 0 < code < 0x110000 and not 0xD800 <= code <= 0xDFFF else match[0]
        return _ENTITIES.get(name, match[0])

    text = re.sub(r"&(#x[0-9a-f]+|#\d+|[a-z]+);", entity, text, flags=re.I)
    text = re.sub(r"\uE000(\d+)\uE001", lambda m: escaped[int(m[1])] if int(m[1]) < len(escaped) else m[0], text)
    return " ".join(text.split())
