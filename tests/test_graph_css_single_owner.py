"""Each viewer selector is declared once per context (source-text check).

The Ledger graph port originally appended a second copy of many graph.css
rules that silently overrode the first with different values: the first
copy read as live while being dead, and one of them still encoded a status
fill. This guard keeps every stylesheet honest: the same selector in the same
at-rule context may not set one property to two different values in two
different rules. A progressive fallback inside one rule (``min-height:
100vh; min-height: 100dvh``) is allowed.

It also pins the Ledger status rule for the graph chrome: status hues colour
text, icons and 2px bars, never a background.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "scripts" / "okf_loom" / "viewer" / "static"
FILES = ("graph.css", "wiki.css", "shell.css", "studio.css", "feedback-import.css")


def _rules(css: str):
    """Yield (context, selector, rule_id, prop, value, line) for every declaration."""
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), css, flags=re.S)
    stack: list[tuple[str, bool, int]] = []
    buf, line, rule_id, quote = "", 1, 0, None
    for ch in text:
        if ch == "\n":
            line += 1
        if quote:
            buf += ch
            quote = None if ch == quote else quote
            continue
        if ch in "\"'":
            quote = ch
            buf += ch
        elif ch == "{":
            prelude = " ".join(buf.split())
            rule_id += 1
            stack.append((prelude, prelude.startswith("@"), rule_id))
            buf = ""
        elif ch in ";}":
            decl = buf.strip()
            if decl and stack and not stack[-1][1] and ":" in decl:
                prop, _, value = decl.partition(":")
                context = tuple(_norm_at(p) for p, is_at, _ in stack if is_at)
                yield context, _norm_selector(stack[-1][0]), stack[-1][2], prop.strip().lower(), " ".join(value.split()), line
            buf = ""
            if ch == "}" and stack:
                stack.pop()
        else:
            buf += ch


def _norm_at(prelude: str) -> str:
    return re.sub(r"\s+", "", prelude.lower())


def _norm_selector(selector: str) -> str:
    selector = re.sub(r"\s*([>+~,])\s*", r"\1", " ".join(selector.split()))
    return selector.replace(",", ", ")


def _conflicts(name: str) -> list[str]:
    seen: dict[tuple, list[tuple[int, str, int]]] = defaultdict(list)
    for context, selector, rule_id, prop, value, line in _rules((STATIC / name).read_text(encoding="utf-8")):
        seen[(context, selector, prop)].append((rule_id, value, line))
    out = []
    for (context, selector, prop), decls in seen.items():
        rules = {rule_id for rule_id, _, _ in decls}
        if len(rules) > 1 and len({value for _, value, _ in decls}) > 1:
            where = " | ".join(f"{line}: {value}" for _, value, line in decls)
            out.append(f"{name} {' '.join(context)} {selector} {{ {prop} }} -> {where}")
    return out


def test_walker_sees_contexts_and_rules() -> None:
    css = """
    .a { color: red; min-height: 1vh; min-height: 2dvh; }
    @media (max-width: 640px) { .a { color: blue; } }
    .a>.b { margin: 0; }
    .a > .b { margin: 1px; }
    """
    decls = list(_rules(css))
    keys = {(c, s, p) for c, s, _, p, _, _ in decls}
    assert ((), ".a", "color") in keys and (("@media(max-width:640px)",), ".a", "color") in keys
    assert ((), ".a>.b", "margin") in keys


@pytest.mark.parametrize("name", FILES)
def test_each_selector_sets_a_property_once_per_context(name: str) -> None:
    conflicts = _conflicts(name)
    assert not conflicts, "conflicting duplicate declarations (merge into one rule):\n" + "\n".join(conflicts[:40])


def test_graph_status_hues_never_fill() -> None:
    """Ledger C6: status hues on text, icons and 2px bars only."""
    offenders = []
    for context, selector, _, prop, value, line in _rules((STATIC / "graph.css").read_text(encoding="utf-8")):
        if any("forced-colors" in c for c in context):
            continue
        if prop in {"background", "background-color"} and re.search(
                r"var\(--(?:okf-(?:error|warn|warning|ok|success|info)(?:-bg|-on)?|status-[a-z]+)\)", value):
            offenders.append(f"graph.css:{line} {selector} {{ {prop}: {value} }}")
    assert not offenders, "status fills:\n" + "\n".join(offenders)
