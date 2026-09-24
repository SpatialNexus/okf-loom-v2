r"""Gitignore-style exclusion for bundle markdown scanning (current spec §5).

Why this exists: ``Bundle.load`` used to be a raw ``rglob("*.md")`` over the
bundle root. Serving or validating a real workspace root swept
``node_modules``, nested cloned repos, virtualenvs and ``.okf-loom`` session
state into the bundle — thousands of vendor ``.md`` files surfacing as
missing-``type`` errors, and forcing docs to physically move into a clean
subdirectory just to get a usable bundle root.

This module is the single scanning path. It walks the tree top-down and
PRUNES excluded directories (never descending into them — a ``node_modules``
holding 30k files costs nothing), applying three exclusion rule groups where
a match in a later group overrides the earlier ones:

1. Built-in defaults (:data:`DEFAULT_EXCLUDES`): hidden directories
   (``.git``, ``.okf-loom``, ``.venv``, …), ``node_modules``,
   ``__pycache__``, ``venv``, ``bower_components``. Plus one structural
   rule that is NOT pattern-based: any non-root directory containing a
   ``.git`` entry (a nested cloned repo, submodule, or worktree) is pruned.
2. ``.gitignore`` rules — the bundle root's file AND nested ones, each
   scoped to its own directory — when ``respect_gitignore`` is on (the
   default). This also makes the self-ignoring ``.okf-loom`` state dirs
   (each holds a ``.gitignore`` containing ``*``) invisible wherever a
   custom ``studio.session_dir`` puts them.
3. ``bundle.exclude`` patterns from ``okf-loom.config.yaml`` (a negation
   here, e.g. ``"!.docs/"``, can re-include something the defaults or a
   ``.gitignore`` excluded — with git's own limitation that a path under a
   PRUNED directory cannot come back this way).

Above all of that sit ``bundle.include`` patterns — the explicit add-back
lever. Include beats every exclusion source, including the structural
nested-repo rule, and CAN reach inside pruned directories. Two forms:

* **Directory form** (``"vendor-repo/"`` or a wildcard-free path like
  ``"node_modules/my-pkg/docs"``): the directory is *revived* — the scanner
  descends its ancestors just far enough to reach it, then scans the
  subtree as normal bundle content. Exclusion rules that were overridden AT
  the revived directory (an ancestor-level ``node_modules/**`` in a
  ``.gitignore``, a ``bundle.exclude`` that killed it) stay overridden for
  its contents, while unrelated rules — the hidden-dir default, basename
  rules like ``*.tmp.md``, excludes targeting deeper paths — keep applying
  inside. This is the recommended form.
* **Glob form** (``"vendor-repo/**"``): force-includes exactly what it
  matches, everything beneath — defaults included. The scanner traverses
  whatever the glob covers, so a broad ``**`` on a huge tree is a
  deliberate, paid-for choice.

Include patterns without a ``/`` (basename form, e.g. ``"keep.md"``) apply
wherever the scan already reaches but do NOT open up pruned directories —
pulling something out of a pruned tree requires naming its path. This keeps
an innocuous one-word include from turning every watcher tick into a full
``.git``/``node_modules`` walk.

Pattern dialect: the practical gitignore subset — ``*`` / ``?`` / character
classes (none of which cross ``/``), ``**`` path-segment wildcards, trailing
``/`` = directory-only, a ``/`` anywhere else = anchored to the rule's base
directory, leading ``!`` = negation (exclude groups only), ``\`` escapes,
``#`` comments. Within an exclusion group the LAST matching rule wins (git
semantics).

Zero dependencies (stdlib only) per the §3 hard invariant, and a leaf
module: nothing here imports other ``okf_loom`` modules.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable, Sequence

# Directory-only by design: hidden FILES (e.g. ``.notes.md``) still load, so
# switching on the new scanner never drops a tracked concept that merely has
# a dotted name. Hidden/vendor DIRECTORIES are where the vendor-sweep pain
# lives (``.git``, ``.okf-loom``, ``.venv``, ``node_modules``…).
DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".*/",
    "node_modules/",
    "__pycache__/",
    "venv/",
    "bower_components/",
)

GITIGNORE_FILENAME: str = ".gitignore"

_WILDCARD_CHARS: frozenset[str] = frozenset("*?[")


@dataclass(frozen=True)
class IgnoreRule:
    """One compiled gitignore-style exclusion rule.

    ``regex`` full-matches candidate paths given RELATIVE to ``base`` in
    posix form (no leading/trailing slash). ``base`` scopes nested
    ``.gitignore`` rules to their own directory ("" = bundle root).
    ``anchored`` records whether the source pattern contained a ``/`` —
    revival masking (see :func:`iter_markdown_files`) only ever masks
    anchored rules, so basename rules keep protecting revived subtrees.
    """

    regex: re.Pattern[str]
    negated: bool
    dir_only: bool
    base: str = ""
    anchored: bool = False


@dataclass(frozen=True)
class IncludeRule:
    """One compiled ``bundle.include`` rule (always positive).

    ``revive`` marks the directory form (trailing ``/`` or a wildcard-free
    final segment): a matching directory re-enters NORMAL scanning.
    ``prefix_regexes`` (anchored rules only) match the ancestor paths that
    lead to the target, letting the scanner reach through pruned parents.
    """

    regex: re.Pattern[str]
    dir_only: bool
    revive: bool
    anchored: bool
    prefix_regexes: tuple[re.Pattern[str], ...] = ()


@dataclass(frozen=True)
class VisibilityRulePolicy:
    """Filesystem-free compiled form of the current bundle visibility policy.

    Walkers supply captured nested-gitignore rules and entry facts; all match
    precedence, include revival, and masking semantics remain centralized here.
    """

    default_rules: tuple[IgnoreRule, ...]
    config_rules: tuple[IgnoreRule, ...]
    include_rules: tuple[IncludeRule, ...]

    @classmethod
    def compile(cls, *, exclude: Sequence[str] = (), include: Sequence[str] = ()) -> "VisibilityRulePolicy":
        return cls(compile_rules(DEFAULT_EXCLUDES), compile_rules(exclude),
                   compile_include_rules(include))

    def groups(self, git_rules: Sequence[IgnoreRule]) -> tuple[Sequence[IgnoreRule], ...]:
        return (self.default_rules, git_rules, self.config_rules)

    def file_included(self, rel: str) -> bool:
        return _include_matches_file(self.include_rules, rel)

    def dir_included(self, rel: str) -> bool:
        return _include_matches_dir(self.include_rules, rel)

    def reaches(self, rel: str) -> bool:
        return _include_reaches(self.include_rules, rel)

    def revives(self, rel: str) -> bool:
        return _include_revives(self.include_rules, rel)

    def ignored(self, git_rules: Sequence[IgnoreRule], rel: str, is_dir: bool,
                *, masked: frozenset[IgnoreRule] = frozenset()) -> bool:
        return is_ignored(self.groups(git_rules), rel, is_dir, masked=masked)

    def revival_mask(self, git_rules: Sequence[IgnoreRule], rel: str,
                     masked: frozenset[IgnoreRule]) -> frozenset[IgnoreRule]:
        return _revival_mask(self.groups(git_rules), rel, masked)


def _pattern_body_to_regex(pat: str) -> str:
    """Translate a gitignore glob (sans ``!``/anchors/trailing ``/``) to regex."""
    i, n = 0, len(pat)
    res = ""
    while i < n:
        c = pat[i]
        if c == "*":
            if pat[i:i + 2] == "**":
                at_boundary = i == 0 or pat[i - 1] == "/"
                if at_boundary and pat[i:i + 3] == "**/":
                    res += "(?:[^/]+/)*"  # zero or more whole segments
                    i += 3
                    continue
                if at_boundary and i + 2 == n:
                    res += ".*"  # trailing "/**": everything underneath
                    i += 2
                    continue
                # Not slash-delimited (e.g. "a**b"): git treats it like "*";
                # be lenient and let it cross segments.
                res += ".*"
                i += 2
                continue
            res += "[^/]*"
            i += 1
        elif c == "?":
            res += "[^/]"
            i += 1
        elif c == "[":
            # Character class: find the closing "]" (a "]" first-in-class is
            # literal, as is one right after the "!" negator).
            j = i + 1
            if j < n and pat[j] in "!^":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                res += re.escape("[")  # unterminated: literal bracket
                i += 1
            else:
                cls = pat[i + 1:j]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                # A trailing lone backslash would escape our closing bracket.
                if cls.endswith("\\") and not cls.endswith("\\\\"):
                    cls += "\\"
                res += "[" + cls + "]"
                i = j + 1
        elif c == "\\" and i + 1 < n:
            res += re.escape(pat[i + 1])
            i += 2
        else:
            res += re.escape(c)
            i += 1
    return res


def _split_pattern(line: str) -> tuple[str, bool, bool] | None:
    """Common gitignore-line surgery: (cleaned pattern, dir_only, anchored).

    Returns ``None`` for lines that carry no pattern after cleaning.
    Handles unescaped-trailing-space stripping, trailing ``/`` (dir-only)
    and the "any interior ``/`` anchors" rule. Does NOT handle ``!``
    (exclusion-only concern) or comments (caller's concern).
    """
    while line.endswith(" ") and not line.endswith("\\ "):
        line = line[:-1]
    if not line:
        return None
    dir_only = line.endswith("/") and not line.endswith("\\/")
    if dir_only:
        line = line.rstrip("/")
    anchored = "/" in line
    line = line.lstrip("/")
    if not line:
        return None
    return line, dir_only, anchored


def compile_rule(line: str, *, base: str = "") -> IgnoreRule | None:
    """Compile one gitignore-style line; ``None`` for blanks and comments."""
    if not line or line.lstrip() == "":
        return None
    if line.startswith("#"):
        return None  # "\#name" survives: the body translator unescapes it
    negated = line.startswith("!")
    if negated:
        line = line[1:]
    split = _split_pattern(line)
    if split is None:
        return None
    line, dir_only, anchored = split
    body = _pattern_body_to_regex(line)
    if not anchored:
        body = "(?:.*/)?" + body
    return IgnoreRule(
        regex=re.compile("^" + body + "$"),
        negated=negated,
        dir_only=dir_only,
        base=base,
        anchored=anchored,
    )


def compile_rules(
    lines: Iterable[str], *, base: str = ""
) -> tuple[IgnoreRule, ...]:
    """Compile many lines, dropping blanks/comments (order preserved)."""
    out = []
    for line in lines:
        rule = compile_rule(str(line), base=base)
        if rule is not None:
            out.append(rule)
    return tuple(out)


def compile_include_rule(line: str) -> IncludeRule | None:
    """Compile one ``bundle.include`` pattern; ``None`` for blanks/comments.

    Raises:
        ValueError: on a ``!``-prefixed pattern — include is already the
            positive direction; removals belong in ``bundle.exclude``.
    """
    if not line or line.lstrip() == "":
        return None
    if line.startswith("#"):
        return None
    if line.startswith("!"):
        raise ValueError(
            f"include patterns are positive; negation is not allowed: {line!r}"
        )
    split = _split_pattern(line)
    if split is None:
        return None
    line, dir_only, anchored = split
    segments = line.split("/")
    # Directory form ⇒ revival: an explicit trailing "/" or a final segment
    # spelled out without wildcards ("node_modules/my-pkg/docs").
    last = segments[-1]
    revive = dir_only or not any(c in _WILDCARD_CHARS for c in last)
    body = _pattern_body_to_regex(line)
    if not anchored:
        body = "(?:.*/)?" + body
    prefixes: list[re.Pattern[str]] = []
    if anchored:
        for i in range(1, len(segments)):
            prefix_body = _pattern_body_to_regex("/".join(segments[:i]))
            prefixes.append(re.compile("^" + prefix_body + "$"))
    return IncludeRule(
        regex=re.compile("^" + body + "$"),
        dir_only=dir_only,
        revive=revive,
        anchored=anchored,
        prefix_regexes=tuple(prefixes),
    )


def compile_include_rules(lines: Iterable[str]) -> tuple[IncludeRule, ...]:
    """Compile many include patterns, dropping blanks/comments."""
    out = []
    for line in lines:
        rule = compile_include_rule(str(line))
        if rule is not None:
            out.append(rule)
    return tuple(out)


def _rule_matches(rule: IgnoreRule, rel_posix: str, is_dir: bool) -> bool:
    if rule.dir_only and not is_dir:
        return False
    if rule.base:
        prefix = rule.base + "/"
        if not rel_posix.startswith(prefix):
            return False
        rel_posix = rel_posix[len(prefix):]
    return rule.regex.match(rel_posix) is not None


def is_ignored(
    groups: Sequence[Sequence[IgnoreRule]],
    rel_posix: str,
    is_dir: bool,
    *,
    masked: frozenset[IgnoreRule] = frozenset(),
) -> bool:
    """Decide a path against ordered exclusion rule groups.

    Later groups override earlier ones outright; within a group the last
    matching rule wins (git semantics). No match anywhere ⇒ not ignored.
    ``masked`` rules are skipped — revival masking for re-included subtrees.
    """
    for group in reversed(groups):
        for rule in reversed(group):
            if rule in masked:
                continue
            if _rule_matches(rule, rel_posix, is_dir):
                return not rule.negated
    return False


def _read_gitignore_rules(dirpath: Path, base: str) -> tuple[IgnoreRule, ...]:
    gi = dirpath / GITIGNORE_FILENAME
    try:
        if not gi.is_file():
            return ()
        text = gi.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    return compile_rules(text.splitlines(), base=base)


# --- Include-rule predicates (shared by the walker and the per-path oracle) --

def _include_matches_file(rules: Sequence[IncludeRule], rel: str) -> bool:
    return any(not r.dir_only and r.regex.match(rel) for r in rules)


def _include_matches_dir(rules: Sequence[IncludeRule], rel: str) -> bool:
    return any(r.regex.match(rel) for r in rules)


def _include_revives(rules: Sequence[IncludeRule], rel: str) -> bool:
    return any(r.revive and r.regex.match(rel) for r in rules)


def _include_reaches(rules: Sequence[IncludeRule], rel: str) -> bool:
    return any(
        p.match(rel)
        for r in rules if r.anchored
        for p in r.prefix_regexes
    )


def _revival_mask(
    groups: Sequence[Sequence[IgnoreRule]],
    rel: str,
    masked: frozenset[IgnoreRule],
) -> frozenset[IgnoreRule]:
    """Anchored exclusion rules matching the revived dir itself."""
    hit = {
        rule
        for group in groups
        for rule in group
        if rule.anchored and rule not in masked
        and _rule_matches(rule, rel, True)
    }
    return masked | hit if hit else masked


def iter_markdown_files(
    root: str | Path,
    *,
    exclude: Sequence[str] = (),
    include: Sequence[str] = (),
    respect_gitignore: bool = True,
) -> list[Path]:
    """All ``*.md`` files under ``root``, minus excluded/pruned subtrees.

    The deterministic replacement for ``sorted(root.rglob("*.md"))`` — the
    ``*.md`` specialization of :func:`iter_bundle_files`, which owns the
    traversal semantics.
    """
    return iter_bundle_files(
        root,
        name_predicate=lambda name: fnmatch(name, "*.md"),
        exclude=exclude,
        include=include,
        respect_gitignore=respect_gitignore,
    )


def iter_bundle_files(
    root: str | Path,
    *,
    name_predicate,
    exclude: Sequence[str] = (),
    include: Sequence[str] = (),
    respect_gitignore: bool = True,
) -> list[Path]:
    """All files matching ``name_predicate`` under ``root``, minus
    excluded/pruned subtrees.

    Entries are visited name-sorted per directory, symlinked directories are
    never followed (matching ``rglob``'s ``**`` behaviour; symlinked FILES
    are still yielded so the loader's containment guards keep applying),
    and unreadable directories are skipped silently (permissive load, spec
    §9 — the loader separately warns about unreadable files it can see).

    ``include`` beats every exclusion source (module docstring has the full
    story). Traversal states:

    * normal — exclusion groups decide; include full-matches force files in.
    * suppressed — inside a pruned directory being crossed only because an
      include reaches through it (or a glob include covers it): nothing is
      collected unless an include rule matches it.
    * revived — a directory-form include matched: back to normal scanning,
      with the anchored exclusion rules that matched the revived directory
      itself masked for its whole subtree (they were overridden there;
      letting them keep killing descendants would undo the override).

    Args:
        root: bundle root directory.
        name_predicate: ``(filename) -> bool`` filter applied to file
            basenames (e.g. ``lambda n: fnmatch(n, "*.md")``). Include
            rules do NOT bypass it — the predicate scopes what kind of
            file the caller wants at all.
        exclude: ``bundle.exclude`` gitignore-style patterns (negations may
            re-include defaulted-out paths, git-style).
        include: ``bundle.include`` add-back patterns (positive only).
        respect_gitignore: honour ``.gitignore`` files (root and nested).
    """
    root = Path(root)
    default_rules = compile_rules(DEFAULT_EXCLUDES)
    config_rules = compile_rules(exclude)
    include_rules = compile_include_rules(include)
    found: list[Path] = []

    def _inc_file(rel: str) -> bool:
        return _include_matches_file(include_rules, rel)

    def _inc_dir(rel: str) -> bool:
        return _include_matches_dir(include_rules, rel)

    def _inc_revive(rel: str) -> bool:
        return _include_revives(include_rules, rel)

    def _inc_reach(rel: str) -> bool:
        return _include_reaches(include_rules, rel)

    _mask_for = _revival_mask

    def _walk(
        dirpath: Path,
        rel: str,
        git_rules: tuple[IgnoreRule, ...],
        *,
        suppressed: bool,
        masked: frozenset[IgnoreRule],
    ) -> None:
        if respect_gitignore:
            git_rules = git_rules + _read_gitignore_rules(dirpath, rel)
        groups = (default_rules, git_rules, config_rules)
        try:
            with os.scandir(dirpath) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            child_rel = f"{rel}/{entry.name}" if rel else entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                child = Path(entry.path)
                if _inc_revive(child_rel):
                    _walk(
                        child, child_rel, git_rules,
                        suppressed=False,
                        masked=_mask_for(groups, child_rel, masked),
                    )
                    continue
                if suppressed:
                    if _inc_dir(child_rel) or _inc_reach(child_rel):
                        _walk(child, child_rel, git_rules,
                              suppressed=True, masked=masked)
                    continue
                ignored = is_ignored(groups, child_rel, True, masked=masked)
                if not ignored:
                    # Structural prune: a non-root dir with a .git entry is
                    # a nested repo/submodule/worktree — not bundle content
                    # unless an include revives or crosses it (above/below).
                    try:
                        ignored = (child / ".git").exists()
                    except OSError:
                        continue
                if not ignored:
                    _walk(child, child_rel, git_rules,
                          suppressed=False, masked=masked)
                elif _inc_dir(child_rel) or _inc_reach(child_rel):
                    _walk(child, child_rel, git_rules,
                          suppressed=True, masked=masked)
            else:
                if not name_predicate(entry.name):
                    continue
                if _inc_file(child_rel):
                    found.append(Path(entry.path))
                    continue
                if suppressed:
                    continue
                if not is_ignored(groups, child_rel, False, masked=masked):
                    found.append(Path(entry.path))

    _walk(root, "", (), suppressed=False, masked=frozenset())
    return found


def path_within(candidate: Path, root: Path) -> bool:
    """True iff ``candidate.resolve()`` is ``root`` or lives beneath it.

    The containment guard that pairs with :func:`is_path_visible` /
    :func:`iter_bundle_files` (which yield symlinked FILES): a symlinked
    asset or reserved file pointing outside the bundle root is refused at
    serve/copy time even though the scan can see it (P2-54). Resolves the
    symlink once; non-existent paths resolve to their lexical location and
    are checked the same way.
    """
    try:
        resolved = candidate.resolve()
        root_resolved = root.resolve()
    except (OSError, RuntimeError):
        return False
    try:
        resolved.relative_to(root_resolved)
        return True
    except ValueError:
        return False


def is_path_visible(
    root: str | Path,
    rel_posix: str,
    *,
    exclude: Sequence[str] = (),
    include: Sequence[str] = (),
    respect_gitignore: bool = True,
) -> bool:
    """Would the §5 scan reach this one path? An O(path-depth) oracle.

    Mirrors :func:`iter_bundle_files` decision-for-decision without walking
    the whole tree: ancestor directories are checked top-down with the same
    pruning/revival logic (built-in defaults, ``.gitignore``, ``bundle.exclude``,
    include revival + masking, the structural nested-repo rule, symlinked
    directories never followed), then ``rel_posix`` itself is checked as a
    file. The live server uses this to decide whether a bundle-local asset
    may be served; anything the concept scan would prune (session state,
    ``.git``, ``node_modules``, gitignored paths) stays unreachable.

    Purely lexical plus rule files on disk: the caller owns existence and
    containment checks (a symlinked FILE pointing outside the root passes
    here, exactly as the scanner yields it — pair with a resolve-based
    containment guard).
    """
    root = Path(root)
    rel_posix = rel_posix.strip("/")
    segments = rel_posix.split("/") if rel_posix else []
    if not segments or any(
        s in ("", ".", "..") or "\x00" in s or "\\" in s for s in segments
    ):
        return False
    default_rules = compile_rules(DEFAULT_EXCLUDES)
    config_rules = compile_rules(exclude)
    include_rules = compile_include_rules(include)
    git_rules: tuple[IgnoreRule, ...] = ()
    dirpath = root
    rel = ""
    masked: frozenset[IgnoreRule] = frozenset()
    suppressed = False
    for seg in segments[:-1]:
        if respect_gitignore:
            git_rules = git_rules + _read_gitignore_rules(dirpath, rel)
        groups = (default_rules, git_rules, config_rules)
        child_rel = f"{rel}/{seg}" if rel else seg
        child = dirpath / seg
        # The walker never descends symlinked directories
        # (``is_dir(follow_symlinks=False)``), so neither does the oracle.
        try:
            if child.is_symlink():
                return False
        except OSError:
            return False
        if _include_revives(include_rules, child_rel):
            masked = _revival_mask(groups, child_rel, masked)
            suppressed = False
        elif suppressed:
            if not (
                _include_matches_dir(include_rules, child_rel)
                or _include_reaches(include_rules, child_rel)
            ):
                return False
        else:
            ignored = is_ignored(groups, child_rel, True, masked=masked)
            if not ignored:
                try:
                    ignored = (child / ".git").exists()
                except OSError:
                    return False
            if ignored:
                if (
                    _include_matches_dir(include_rules, child_rel)
                    or _include_reaches(include_rules, child_rel)
                ):
                    suppressed = True
                else:
                    return False
        dirpath = child
        rel = child_rel
    if respect_gitignore:
        git_rules = git_rules + _read_gitignore_rules(dirpath, rel)
    groups = (default_rules, git_rules, config_rules)
    if _include_matches_file(include_rules, rel_posix):
        return True
    if suppressed:
        return False
    return not is_ignored(groups, rel_posix, False, masked=masked)


__all__ = [
    "DEFAULT_EXCLUDES",
    "GITIGNORE_FILENAME",
    "IgnoreRule",
    "IncludeRule",
    "VisibilityRulePolicy",
    "compile_include_rule",
    "compile_include_rules",
    "compile_rule",
    "compile_rules",
    "is_ignored",
    "is_path_visible",
    "iter_bundle_files",
    "iter_markdown_files",
    "path_within",
]
