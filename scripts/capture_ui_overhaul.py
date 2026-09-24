#!/usr/bin/env python3
"""Deterministically capture the final-v2 UI-overhaul release proof set.

ASYNC_LIFECYCLE_MATRIX
scenario | owner | mutation | result | persisted state | late result | retry | proof
readiness timeout/abort | capture_target monotonic budget | private stage only | failure | prior destination unchanged | stage discarded | fresh stage | capture-consumer tests
capture deadline while screenshot is running | capture consumer absolute budget | private temp only | typed timeout | prior artifact unchanged | late screenshot cannot publish | fresh invocation/lease | capture-consumer timeout test
superseded capture invocation | destination generation claim + edge-flow lease | publication boundary | typed superseded | newer artifact only | stale release cannot publish or release newer lease | fresh token | cross-process/lease tests
late readiness/layout result | page lifecycle/readiness owner | no publication before settled generation | ignored/superseded | unchanged | late result fenced by generation | new page/load generation | readiness and graph lifecycle tests
server exit | process owner | none | failure | prior destination unchanged | process reaped | fresh process | finally terminate/kill/wait
input drift | inventory owner | private stage complete | failure | prior destination unchanged | unpublished | retry inventories again | pre/post digest check
publish | main invocation | serialized recoverable two-phase replacement | manifest+PNGs | old or new complete after return; transient absence is possible between renames | no worker survives publish | next invocation restores a crash-left backup before publishing | fault/SIGKILL/concurrent-reader tests

RENDERING_CAPTURE_MATRIX
target | visibility/readability | readiness | invalid-state diagnostic | provenance proof
document/search/studio | visible non-zero DOM and readable text | production capture-readiness owner | hidden/unavailable/not-ready | artifact row + scenario assertion
graph | visible canvas/SVG and readable canvas | settled current layout generation + overlay-aware fit | missing/unreadable/unavailable/not-ready | artifact row + graph scenario assertion
no-JS | visible authored fallback or canonical export | DOM visibility/text, never fake JS readiness | no-js-not-ready/no-js-fallback-missing | artifact row + javascript=false
final bundle | all expected files and no stale extras | clean HEAD/tree and unchanged inventory | precondition/drift/incomplete matrix | atomic manifest publication

The final runner refuses dirty worktrees and known historical/output paths. A
manifest-only invocation is intentionally non-evidence; the full invocation
executes every ``SCENARIO_SPECS`` row and publishes only a complete stage.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import ExitStack
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.parse
from pathlib import Path
from types import MappingProxyType
from typing import Any, NamedTuple


def _seal_git_authority() -> tuple[str, MappingProxyType[str, str]]:
    """Resolve Git once and remove ambient config/object redirection."""

    executable = shutil.which("git")
    if not executable:
        raise RuntimeError("git executable is unavailable")
    git = str(Path(executable).resolve(strict=True))
    return git, MappingProxyType({
        "PATH": os.path.dirname(git), "LANG": "C", "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "",
        "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
    })


_SEALED_GIT, _SEALED_GIT_ENV = _seal_git_authority()


def _seal_repository_authority() -> tuple[Any, Any]:
    """Capture physical checkout authority where module globals cannot redirect it."""

    class Authority(NamedTuple):
        root: Path
        device: int
        inode: int

    root = Path(__file__).resolve().parent.parent
    info = root.stat(follow_symlinks=False)
    authority = Authority(root=root, device=info.st_dev, inode=info.st_ino)

    def repository_root(_authority: Authority = authority) -> Path:
        try:
            current = _authority.root.stat(follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError("sealed capture checkout is unavailable") from exc
        if (current.st_dev, current.st_ino) != (_authority.device, _authority.inode) or not stat.S_ISDIR(current.st_mode):
            raise RuntimeError("sealed capture checkout identity changed")
        return _authority.root

    def checkout_identity(_authority: Authority = authority) -> tuple[int, int]:
        repository_root(_authority)
        return _authority.device, _authority.inode

    return repository_root, checkout_identity


_sealed_repository_root, _sealed_checkout_identity = _seal_repository_authority()
SELECTED_CAPTURE_PROTOCOL = "okf-ui-selected-v1"
_SELECTED_DRIVER = "scripts/capture_ui_overhaul.py"
# Compatibility/display aliases only. Security-sensitive code exclusively calls
# the closure-backed accessor above, so importers may rebind these harmlessly.
ROOT = _sealed_repository_root()
DEFAULT_BUNDLE = ROOT / "samples" / "demo_bundle"
DEFAULT_DENSE_BUNDLE = ROOT / "docs-bundle"
DEFAULT_EMPTY_BUNDLE = ROOT / "samples" / "empty_bundle"
_PUBLISH_THREAD_LOCKS: dict[str, threading.Lock] = {}
_PUBLISH_THREAD_LOCKS_GUARD = threading.Lock()
_CAPTURE_CLAIMS: dict[str, int] = {}
_CAPTURE_CLAIMS_GUARD = threading.Lock()
_NEXT_CAPTURE_CLAIM = 0
# A process whose death cannot be confirmed retains its private authority in a
# durable registry.  Roots are allocated with mkdtemp (no process-exit
# finalizer), so a surviving child can continue safely after this interpreter
# exits.  A later operator/cleanup job may inspect this append-only registry.
_RETAINED_LIVE_STATE: list[Any] = []
_LIVE_STATE_REGISTRY = (Path(tempfile.gettempdir())
                        / f"okf-loom-retained-{os.geteuid()}" / "registry.jsonl")


def _record_state_retention(
    authority: Any, errors: list[str], *, disposition: str, reason: str,
    retain_descriptor: bool,
) -> dict[str, Any]:
    """Durably record truthful redacted state disposition before releasing FDs."""
    import fcntl
    fallback = {
        "schema": 3, "device": authority.device, "inode": authority.inode,
        "uid": authority.uid, "originalPath": "<redacted>", "reachablePath": None,
    }
    record = dict(fallback)
    metadata_failed = False
    try:
        record.update(authority.retention_record())
    except BaseException as exc:
        errors.append(f"state-metadata:{type(exc).__name__}")
        metadata_failed = True
        disposition = "cleanup-failed-retained"
        retain_descriptor = True
    record.update({
        "pid": os.getpid(), "reason": reason, "stateDisposition": disposition,
        "contentState": (
            "sanitized-empty-tree" if disposition == "sanitized-retained-tree"
            else "potentially-secret-bearing"
        ),
    })

    parent_fd = -1
    fd = -1
    created = False
    try:
        parent = _LIVE_STATE_REGISTRY.parent
        if not parent.exists():
            parent.mkdir(mode=0o700)
        parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        parent_info = os.fstat(parent_fd)
        if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != os.geteuid()
                or stat.S_IMODE(parent_info.st_mode) != 0o700):
            raise OSError("unsafe retained-state registry parent")
        flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(_LIVE_STATE_REGISTRY.name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            fd = os.open(_LIVE_STATE_REGISTRY.name, flags | os.O_CREAT | os.O_EXCL,
                         0o600, dir_fd=parent_fd)
            created = True
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise OSError("unsafe retained-state registry")
        fcntl.flock(fd, fcntl.LOCK_EX)
        pre_offset = os.lseek(fd, 0, os.SEEK_END)
        if pre_offset and os.pread(fd, 1, pre_offset - 1) != b"\n":
            record["stateDisposition"] = "registry-corrupt"
            raise OSError("retained-state registry has an incomplete JSONL tail")
        payload = (json.dumps(record, sort_keys=True) + "\n").encode()
        written = 0
        try:
            while written < len(payload):
                count = os.write(fd, payload[written:])
                if count <= 0:
                    raise OSError("retained-state registry write made no progress")
                written += count
            os.fsync(fd)
            if created:
                os.fsync(parent_fd)
        except BaseException:
            rollback_failed = False
            try:
                os.ftruncate(fd, pre_offset)
                os.fsync(fd)
                if created:
                    current = os.stat(_LIVE_STATE_REGISTRY.name, dir_fd=parent_fd,
                                      follow_symlinks=False)
                    if ((current.st_dev, current.st_ino, current.st_mode, current.st_uid)
                            != (info.st_dev, info.st_ino, info.st_mode, info.st_uid)):
                        raise OSError("created retained-state registry identity changed")
                    os.unlink(_LIVE_STATE_REGISTRY.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
            except BaseException:
                rollback_failed = True
            if rollback_failed:
                record["stateDisposition"] = "registry-corrupt"
            raise
        # The fallback row itself is durable, but the requested metadata
        # operation failed, so callers must not advertise a complete durable
        # retention claim for this cleanup attempt.
        record["durable"] = not metadata_failed
    except BaseException as exc:
        errors.append(f"state-registry:{type(exc).__name__}")
        retain_descriptor = True
        if record.get("stateDisposition") != "registry-corrupt":
            record["stateDisposition"] = "cleanup-failed-retained"
        record["contentState"] = "potentially-secret-bearing"
        record["durable"] = False
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)
    if retain_descriptor:
        # Active or uncertain content keeps descriptor authority for this
        # parent's lifetime. A registry failure also fails closed this way.
        _RETAINED_LIVE_STATE.append(authority)
    return record


def _retain_live_state(authority: Any, errors: list[str]) -> dict[str, Any]:
    """Compatibility wrapper for active, potentially secret-bearing state."""
    return _record_state_retention(
        authority, errors, disposition="active-retained",
        reason="process-death-unconfirmed", retain_descriptor=True)


class CapturePreconditionError(RuntimeError):
    """Raised before any build or publication when release inputs are unsafe."""


class CaptureFailure(RuntimeError):
    """A typed, fail-closed capture diagnostic suitable for a release manifest."""

    def __init__(self, diagnostic: dict[str, Any]) -> None:
        self.diagnostic = diagnostic
        super().__init__(json.dumps(diagnostic, sort_keys=True))


def _claim_capture(path: Path) -> tuple[str, int]:
    """Claim one private screenshot destination for local supersession fencing."""

    global _NEXT_CAPTURE_CLAIM
    key = str(path.resolve())
    with _CAPTURE_CLAIMS_GUARD:
        _NEXT_CAPTURE_CLAIM += 1
        claim = _NEXT_CAPTURE_CLAIM
        _CAPTURE_CLAIMS[key] = claim
    return key, claim


def _capture_claim_is_current(key: str, claim: int) -> bool:
    with _CAPTURE_CLAIMS_GUARD:
        return _CAPTURE_CLAIMS.get(key) == claim


def _release_capture_claim(key: str, claim: int) -> None:
    with _CAPTURE_CLAIMS_GUARD:
        if _CAPTURE_CLAIMS.get(key) == claim:
            _CAPTURE_CLAIMS.pop(key, None)


def _validate_release_destination(destination: Path, *, _root: Any = _sealed_repository_root) -> Path:
    """Return a safe absolute destination outside the checkout's evidence tree."""

    root = _root()
    candidate = Path(destination).expanduser().absolute()
    protected = root / "docs" / "screenshots"
    resolved = candidate.resolve(strict=False)
    if candidate != resolved:
        raise CapturePreconditionError("release output path must not traverse a symlink")
    if resolved == root or root in resolved.parents or resolved == protected or protected in resolved.parents:
        raise CapturePreconditionError(
            "protected release output: choose an explicit artifact directory outside the checkout"
        )
    if candidate.name in {"", ".", ".."}:
        raise CapturePreconditionError("release output must name a directory")
    if candidate.exists() and candidate.is_symlink():
        raise CapturePreconditionError("release output must not be a symlink")
    if candidate.exists() and not candidate.is_dir():
        raise CapturePreconditionError("release output must be a directory")
    parent = candidate.parent
    if parent.exists() and parent.resolve() != parent:
        raise CapturePreconditionError("release output parent must not be a symlink")
    return candidate


class ScenarioSpec(NamedTuple):
    """One deterministic release-evidence scenario.

    The manifest is deliberately data-first: a reviewer can inspect the
    complete release matrix without launching a browser, while the runner
    below still executes every row by its ``action``.  ``resolved_theme`` is
    the expected concrete paint state; public family and mode are seeded
    independently before navigation (and Auto is derived).
    """

    id: str
    artifact: str
    source: str
    route: str
    target: str
    assertion: str
    coverage: tuple[str, ...]
    resolved_theme: str | None = "technical-light"
    theme_preference: str | None = "light"
    contrast: str | None = "normal"
    border: str | None = "on"
    viewport: tuple[int, int] = (1440, 1000)
    javascript: bool = True
    color_scheme: str = "light"
    forced_colors: bool = False
    reduced_motion: bool = False
    zoom_percent: int | None = None
    action: str = "none"
    expected_http_status: tuple[int, ...] = ()
    expected_event_fragments: tuple[str, ...] = ()
    locator: str | None = None
    full_page: bool = True
    # Public theme state is represented as independent family/mode values.
    # ``theme_preference`` remains a compatibility input for older matrix rows;
    # manifest output normalizes it to the public mode only.
    theme_family: str | None = None
    theme_mode: str | None = None
    storage: str = "seeded"
    bundle: str = "demo"
    density: str = "comfortable"
    zoom_mechanism: str | None = None
    server_mode: str = "editable"


_SUPPORTING_ARTIFACTS = (
    "keyboard-focus-trace.json",
    "graph-at-journey.json",
    "zoom-evidence.json",
    "manual-screen-reader-session.md",
)


_REQUIRED_COVERAGE = frozenset({
    "appearance-soft-contrast-border-off", "studio-rail", "studio-overlays",
    "studio-focus", "studio-split", "studio-mobile",
    "graph-map", "graph-lenses", "graph-typed-path", "graph-focus", "graph-mobile",
    "search-result", "search-no-result", "search-error", "search-manual-copy",
    "live", "static", "spa", "single-file", "no-js",
    "boot-success", "boot-blocked", "boot-failure",
    "forced-colors", "reduced-motion", "zoom-400",
    "technical-auto", "technical-auto-default", "technical-auto-storage-denied",
    "technical-auto-os-light", "technical-auto-os-dark",
    *(f"F{number:02d}" for number in range(1, 25)),
    "viewport-1440", "viewport-1280", "viewport-390", "demo-bundle", "dense-docs-bundle",
    "index-empty", "index-filter-mismatch", "concept-outline", "concept-rendered",
    "concept-source", "concept-split", "concept-focus-view", "search-expanded",
    "graph-keyboard-journey", "coffee-showcase", "compact-density",
    "studio-idle", "studio-error", "studio-retry", "studio-read-only-authority",
    "studio-proposal-enabled", "studio-unavailable", "zoom-200",
    "index-graph-thumbnail-desktop", "index-graph-thumbnail-mobile",
})
_VALID_SOURCES = frozenset({"live", "static", "spa", "single-file"})
_VALID_THEMES = frozenset({
    "technical-light", "technical-dark", "swiss-light", "swiss-dark",
    "purple-light", "purple-dark", "coffee-light", "coffee-dark",
})
_VALID_THEME_FAMILIES = frozenset({"technical", "swiss", "purple", "coffee"})
_VALID_THEME_MODES = frozenset({"light", "dark", "auto"})
_VALID_STORAGE_MODES = frozenset({"seeded", "none", "denied"})
_VALID_BUNDLES = frozenset({"demo", "dense-docs", "empty-okf"})
_VALID_DENSITIES = frozenset({"comfortable", "compact"})
_VALID_SERVER_MODES = frozenset({"editable", "read-only", "proposal-enabled"})
_IMPLEMENTED_ACTIONS = frozenset({
    "none", "theme", "parity", "appearance-modifiers", "appearance-overlay",
    "studio-rail", "studio-mobile", "studio-split", "concept-rendered", "concept-source",
    "concept-focus-view", "concept-outline", "index-empty", "index-filter-mismatch",
    "graph-preview-name", "graph-map", "graph-lenses", "graph-focus", "graph-path",
    "graph-mobile", "graph-keyboard-journey", "search-result", "search-no-result",
    "search-error", "search-manual-copy", "search-expanded", "studio-authority",
    "studio-error-retry", "boot-success", "boot-blocked", "boot-failure", "zoom-200",
    "zoom-400", "no-js", "no-js-document", "no-js-graph",
})
_ACCEPTANCE_CRITERIA = frozenset(f"F{number:02d}" for number in range(1, 25))


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_fixture_revisions(revisions: object) -> dict[str, str]:
    """Require exactly one typed revision for every fixture used by scenarios."""

    required = {spec.bundle for spec in SCENARIO_SPECS}
    if not isinstance(revisions, dict) or set(revisions) != required or any(
        not _nonempty_string(revisions.get(name)) for name in required
    ):
        raise CapturePreconditionError(
            "fixture revision map must contain exactly every scenario fixture with nonempty string values"
        )
    return revisions


def _oracle_contract_examples(label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Production-field examples used to prove each registry predicate directly."""

    examples: dict[str, tuple[dict[str, Any], str]] = {
        "F01": ({"order": ["Index", "Search", "Graph", "Studio"], "current": "Index", "currentHref": "/"}, "currentHref"),
        "F02": ({"emptyText": "No matching concepts. Clear filter."}, "emptyText"),
        "F03": ({"sections": 3}, "sections"),
        "F04": ({"journey": {"restored": True, "outline_opened": True, "escape_closed": True, "section_activated": "#schema", "no_horizontal_overflow": True}}, "journey"),
        "F05": ({"name": "Relationship graph, 24 concepts", "svgHidden": "true", "svgFocusable": "false"}, "svgHidden"),
        "F06": ({"journey": {"activeRole": "listbox", "activeDescendant": "node-a", "selectedId": "a", "listTarget": "a", "pathKind": "found", "pathStepCount": 2}}, "journey"),
        "F07": ({"layout": {"ready": True, "layoutSettled": True, "transitionPending": False, "layoutGeneration": 2, "appliedLayoutGeneration": 2}, "nodeCount": 24, "labelCount": 24}, "labelCount"),
        "F08": ({"tourObscures": False, "tourLaunch": True}, "tourLaunch"),
        "F09": ({"expanded": True, "queryRetained": True}, "queryRetained"),
        "F10": ({"required": ["a", "b"], "matched": ["a", "b"]}, "matched"),
        "F11": ({"evidence": {"trigger": "search-copy-passage", "fallbackVisible": True, "exactMarkdown": "x", "citation": "c"}}, "evidence"),
        "F12": ({"focusAmbiguous": False, "taskLabel": True}, "taskLabel"),
        "F13": ({"studio": "Studio", "idleCounters": 0}, "idleCounters"),
        "F14": ({"retry": {"errorObserved": True, "retryActivated": True, "recovered": True, "commentsReady": True, "eventsReady": True, "conceptContentRev": "r", "alertAbsent": True}, "canonicalIdle": False}, "retry"),
        "F15": ({"proposalCopy": True, "proposalState": "available"}, "proposalCopy"),
        "F16": ({"findControl": True, "settingsControl": True}, "settingsControl"),
        "F17": ({"touchAction": "none", "libraryRuntime": "3.0", "journey": True}, "journey"),
        "F18": ({"landmark": "On this page navigation"}, "landmark"),
        "F19": ({"startHere": "Open the live studio"}, "startHere"),
        "F20": ({"forcedColors": True, "marks": 4, "nonColor": 4}, "nonColor"),
        "F21": ({"headings": ["Title", "Section"], "h1Count": 1}, "h1Count"),
        "F22": ({"unnamed": 0, "minimumTarget": 24, "undersized": 0}, "minimumTarget"),
        "F23": ({"density": "compact", "scale": "0.9", "consumedHeight": 48}, "density"),
        "F24": ({"showcase": "coffee-light", "stored": {"family": "coffee", "mode": "light"}, "runtimeFamily": "coffee", "runtimeMode": "light"}, "runtimeMode"),
    }
    passing, key = examples[label]
    rejecting = json.loads(json.dumps(passing))
    value = rejecting[key]
    rejecting[key] = 1 if value == 0 else (False if value is not False else True)
    return passing, rejecting


def _all_true(value: object, keys: tuple[str, ...]) -> bool:
    return isinstance(value, dict) and all(value.get(key) is True for key in keys)


ACCEPTANCE_ORACLE_REGISTRY = {
    "F01": lambda o: o.get("order", [])[:4] == ["Index", "Search", "Graph", "Studio"] and o.get("current") == "Index" and o.get("currentHref") == "/",
    "F02": lambda o: bool(__import__("re").search(r"no concepts|no matching|clear filter", str(o.get("emptyText", "")), __import__("re").I)),
    "F03": lambda o: isinstance(o.get("sections"), int) and o["sections"] > 1,
    "F04": lambda o: _all_true(o.get("journey"), ("restored", "outline_opened", "escape_closed", "no_horizontal_overflow")) and _nonempty_string((o.get("journey") or {}).get("section_activated")),
    "F05": lambda o: _nonempty_string(o.get("name")) and len(o["name"]) < 160 and o.get("svgHidden") == "true" and o.get("svgFocusable") == "false",
    "F06": lambda o: isinstance(o.get("journey"), dict) and o["journey"].get("activeRole") == "listbox" and _nonempty_string(o["journey"].get("activeDescendant")) and o["journey"].get("selectedId") == o["journey"].get("listTarget") and o["journey"].get("pathKind") == "found" and o["journey"].get("pathStepCount", 0) >= 2,
    "F07": lambda o: _all_true(o.get("layout"), ("ready", "layoutSettled")) and (o.get("layout") or {}).get("transitionPending") is False and (o.get("layout") or {}).get("layoutGeneration") == (o.get("layout") or {}).get("appliedLayoutGeneration") and o.get("nodeCount", 0) >= 24 and o.get("labelCount") == o.get("nodeCount"),
    "F08": lambda o: o.get("tourObscures") is False and o.get("tourLaunch") is True,
    "F09": lambda o: o.get("expanded") is True and o.get("queryRetained") is True,
    "F10": lambda o: bool(o.get("required")) and o.get("matched") == o.get("required"),
    "F11": lambda o: isinstance(o.get("evidence"), dict) and o["evidence"].get("trigger") == "search-copy-passage" and o["evidence"].get("fallbackVisible") is True and _nonempty_string(o["evidence"].get("exactMarkdown")) and _nonempty_string(o["evidence"].get("citation")),
    "F12": lambda o: o.get("focusAmbiguous") is False and o.get("taskLabel") is True,
    "F13": lambda o: _nonempty_string(o.get("studio")) and o.get("idleCounters") == 0,
    "F14": lambda o: (_all_true(o.get("retry"), ("errorObserved", "retryActivated", "recovered", "commentsReady", "eventsReady", "alertAbsent")) and _nonempty_string((o.get("retry") or {}).get("conceptContentRev"))) or o.get("canonicalIdle") is True,
    "F15": lambda o: o.get("proposalCopy") is True and _nonempty_string(o.get("proposalState")),
    "F16": lambda o: o.get("findControl") is True and o.get("settingsControl") is True,
    "F17": lambda o: o.get("touchAction") not in {None, "", "auto"} and _nonempty_string(o.get("libraryRuntime")) and o.get("journey") is True,
    "F18": lambda o: "navigation" in str(o.get("landmark", "")).lower(),
    "F19": lambda o: bool(__import__("re").search(r"use from a clone|open the live studio|find a command|understand the format", str(o.get("startHere", "")), __import__("re").I)),
    "F20": lambda o: o.get("forcedColors") is True and o.get("marks", 0) > 0 and o.get("nonColor") == o.get("marks"),
    "F21": lambda o: o.get("h1Count") == 1 and bool(o.get("headings")) and all(_nonempty_string(v) for v in o["headings"]),
    "F22": lambda o: o.get("unnamed") == 0 and o.get("undersized") == 0 and o.get("minimumTarget", 0) >= 24,
    "F23": lambda o: o.get("density") == "compact" and float(o.get("scale") or 1) < 1 and o.get("consumedHeight", 0) > 0,
    "F24": lambda o: o.get("showcase") == "coffee-light" and o.get("stored") == {"family": "coffee", "mode": "light"} and o.get("runtimeFamily") == "coffee" and o.get("runtimeMode") == "light",
}


def _public_theme(spec: ScenarioSpec) -> tuple[str | None, str | None]:
    """Resolve a scenario's public family/mode pair without inventing Auto.

    The old matrix used strings such as ``technical-auto``.  Runtime and
    manifest contracts use independent public values instead: ``family`` is
    ``technical`` and ``mode`` is ``auto``; the concrete paint result remains
    in ``resolved_theme``.
    """

    if spec.theme_family is not None or spec.theme_mode is not None:
        if spec.theme_family is None or spec.theme_mode is None:
            raise ValueError(f"scenario {spec.id} has an incomplete public theme pair")
        return spec.theme_family, spec.theme_mode
    preference = spec.theme_preference
    if preference is None:
        return (None, None) if not spec.javascript else ("technical", "auto")
    if preference == "technical-auto":
        return "technical", "auto"
    if preference in _VALID_THEME_MODES:
        return "technical", preference
    if "-" in preference:
        family, mode = preference.rsplit("-", 1)
        return family, mode
    raise ValueError(f"scenario {spec.id} has an invalid public theme preference")


def _scenario_theme_preference(spec: ScenarioSpec) -> tuple[str | None, str | None]:
    """Return the public theme pair expected by the browser runtime."""

    family, mode = _public_theme(spec)
    if family is None or mode is None:
        return None, None
    return family, mode


def _has_explicit_theme_preference(spec: ScenarioSpec) -> bool:
    return (
        spec.theme_preference is not None
        or spec.theme_family is not None
        or spec.theme_mode is not None
    )


def _theme_scenario(theme: str) -> ScenarioSpec:
    family, mode = theme.rsplit("-", 1)
    return ScenarioSpec(
        id=f"theme-{theme}",
        artifact=f"theme-{theme}.png",
        source="live",
        route="/",
        target="document",
        assertion=f"document root resolves data-theme={theme} with {family}/{mode} preference",
        coverage=("live",),
        resolved_theme=theme,
        theme_preference=mode,
        action="theme",
        color_scheme="dark" if mode == "dark" else "light",
        theme_family=family,
        theme_mode=mode,
    )


def _technical_auto_scenario(source: str, os_mode: str, storage: str) -> ScenarioSpec:
    storage_label = "default" if storage == "none" else "storage-denied"
    route = "/" if source == "live" else "/index.html"
    return ScenarioSpec(
        id=f"theme-technical-auto-{storage_label}-os-{os_mode}-{source}",
        artifact=f"theme-technical-auto-{storage_label}-os-{os_mode}-{source}.png",
        source=source,
        route=route,
        target="document",
        assertion=(
            f"public family=technical and mode=auto resolve to technical-{os_mode} "
            f"on {source} with {storage_label} storage"
        ),
        coverage=(
            "technical-auto", f"technical-auto-{storage_label}",
            f"technical-auto-os-{os_mode}", source,
        ),
        resolved_theme=f"technical-{os_mode}",
        theme_preference="auto",
        contrast="normal",
        border="on",
        color_scheme=os_mode,
        action="theme",
        theme_family="technical",
        theme_mode="auto",
        storage=storage,
    )


SCENARIO_SPECS: tuple[ScenarioSpec, ...] = (
      _theme_scenario("technical-light"),
      _theme_scenario("technical-dark"),
      _theme_scenario("swiss-light"),
      _theme_scenario("swiss-dark"),
      _theme_scenario("purple-light"),
      _theme_scenario("purple-dark"),
      _theme_scenario("coffee-light"),
      _theme_scenario("coffee-dark"),
    ScenarioSpec(
        "theme-technical-auto", "theme-technical-auto.png", "live", "/", "document",
        "public family=technical and mode=auto resolve to technical-light with no saved storage",
        ("live", "technical-auto", "technical-auto-default", "technical-auto-os-light"),
        "technical-light", "auto", "normal", "on", action="theme",
        theme_family="technical", theme_mode="auto", storage="none",
    ),
    ScenarioSpec(
        "appearance-soft-contrast-border-off", "appearance-soft-contrast-border-off.png",
        "live", "/tables/orders", "document",
        "Appearance reports Soft Contrast and Border Off as semantic root modifiers",
        ("appearance-soft-contrast-border-off", "live"), "technical-light", "light",
        "soft", "off", action="appearance-modifiers",
    ),
    ScenarioSpec(
        "studio-rail", "studio-rail.png", "live", "/tables/orders", "document",
        "desktop Studio Comments panel is a visible complementary rail without aria-modal",
        ("studio-rail", "live"), action="studio-rail",
    ),
    ScenarioSpec(
        "studio-appearance-overlay", "studio-appearance-overlay.png", "live", "/tables/orders", "document",
        "Appearance popover is visible, labelled, and owns its topmost overlay state",
        ("studio-overlays", "live"), action="appearance-overlay",
    ),
    ScenarioSpec(
        "studio-focus", "studio-focus.png", "live", "/__graph", "graph",
        "graph Focus action leaves a visible focus-root state and settled graph readiness",
        ("studio-focus", "graph-focus", "live"), action="graph-focus", full_page=False,
    ),
    ScenarioSpec(
        "studio-split", "studio-split.png", "live", "/tables/orders", "document",
        "Studio Split is pressed and the view wrapper owns the split state",
        ("studio-split", "live"), action="studio-split",
    ),
    ScenarioSpec(
        "studio-mobile", "studio-mobile.png", "live", "/tables/orders", "document",
        "mobile Studio panel is a visible aria-modal dialog with a focusable close owner",
        ("studio-mobile", "studio-overlays", "live"), viewport=(390, 844), action="studio-mobile",
    ),
    ScenarioSpec(
        "graph-map", "graph-map.png", "live", "/__graph", "graph",
        "Map view has a visible readable canvas and a settled generation",
        ("graph-map", "live"), action="graph-map", full_page=False,
    ),
    ScenarioSpec(
        "graph-lenses", "graph-lenses.png", "live", "/__graph", "graph",
        "Lenses and Advanced graph controls are visible after semantic layout readiness",
        ("graph-lenses", "live"), action="graph-lenses", full_page=False,
    ),
    ScenarioSpec(
        "graph-typed-path", "graph-typed-path.png", "live", "/__graph", "graph",
        "a visible found path has typed directional relations and a readable detail panel",
        ("graph-typed-path", "live"), action="graph-path", full_page=False,
    ),
    ScenarioSpec(
        "graph-focus", "graph-focus.png", "live", "/__graph", "graph",
        "Focus mode is visible, settled, and owns the selected concept action state",
        ("graph-focus", "live"), action="graph-focus", full_page=False,
    ),
    ScenarioSpec(
        "graph-mobile", "graph-mobile.png", "live", "/__graph", "graph",
        "mobile Graph pane is visible within the 390px viewport after a settled layout",
        ("graph-mobile", "live"), viewport=(390, 844), action="graph-mobile", full_page=False,
    ),
    ScenarioSpec(
        "search-result", "search-result.png", "live", "/__search?q=orders", "search",
        "search result page exposes at least one settled result and its context action",
        ("search-result", "live"), action="search-result",
    ),
    ScenarioSpec(
        "search-no-result", "search-no-result.png", "live",
        "/__search?q=orders&type=okf-no-such-type", "search",
        "search explicitly reports a settled no-result state without treating it as hidden",
        ("search-no-result", "live"), action="search-no-result",
    ),
    ScenarioSpec(
        "search-error", "search-error.png", "live", "/__search?unknown=release-capture", "search",
        "search error response renders a user-visible alert for an unsupported parameter",
        ("search-error", "live"), action="search-error", expected_http_status=(400,),
    ),
    ScenarioSpec(
        "search-manual-copy", "search-manual-copy.png", "live", "/__search?q=orders", "search",
        "clipboard rejection exposes the exact Markdown manual-copy fallback",
        ("search-manual-copy", "live"), action="search-manual-copy",
    ),
    ScenarioSpec(
        "static-parity", "static-parity.png", "static", "/index.html", "document",
        "static multi-file output renders the authored index with the same capture revision",
        ("static",), action="parity",
    ),
    ScenarioSpec(
        "spa-parity", "spa-parity.png", "spa", "/index.html", "document",
        "SPA output renders the authored index and retains the portable capture contract",
        ("spa",), action="parity",
    ),
    ScenarioSpec(
        "single-file-parity", "single-file-parity.png", "single-file", "/index.html", "document",
        "single-file output renders embedded authored evidence without a server dependency",
        ("single-file",), action="parity",
    ),
    ScenarioSpec(
        "no-js-live", "no-js-live.png", "live", "/tables/orders", "document",
        "live no-JS authored content and explicit fallback remain readable",
        ("no-js", "live"), theme_preference=None, contrast=None, border=None,
        javascript=False, action="no-js",
    ),
    ScenarioSpec(
        "no-js-static", "no-js-static.png", "static", "/index.html", "document",
        "static no-JS fallback is visible and does not claim interactive search",
        ("no-js", "static"), theme_preference=None, contrast=None, border=None,
        javascript=False, action="no-js",
    ),
    ScenarioSpec(
        "no-js-static-graph", "no-js-static-graph.png", "static", "/__graph.html", "graph-nojs",
        "static no-JS canonical SVG export is visible and readable",
        ("no-js", "static"), theme_preference=None, contrast=None, border=None,
        javascript=False, action="no-js-graph", full_page=False,
        locator='svg[data-okf-graph-export="canonical-v1"]',
    ),
    ScenarioSpec(
        "no-js-single-file", "no-js-single-file.png", "single-file", "/index.html", "document",
        "single-file no-JS authored evidence remains readable without interactive claims",
        ("no-js", "single-file"), theme_preference=None, contrast=None, border=None,
        javascript=False, action="no-js",
    ),
    ScenarioSpec(
        "boot-success", "boot-success.png", "live", "/tables/orders", "document",
        "Studio boot reaches its explicit success class and hides the fallback banner",
        ("boot-success", "live"), action="boot-success",
    ),
    ScenarioSpec(
        "boot-blocked", "boot-blocked.png", "live", "/tables/orders", "document",
        "blocked Studio script leaves the typed fallback visible without false success",
        ("boot-blocked", "live"), theme_preference=None, contrast=None, border=None,
        action="boot-blocked", expected_event_fragments=("studio.js",),
    ),
    ScenarioSpec(
        "boot-failure", "boot-failure.png", "live", "/tables/orders", "document",
        "a Studio script initialization exception leaves the typed failure fallback visible",
        ("boot-failure", "live"), theme_preference=None, contrast=None, border=None,
        action="boot-failure", expected_event_fragments=(),
    ),
    ScenarioSpec(
        "forced-colors", "forced-colors.png", "live", "/__graph", "graph",
        "forced-colors media state is active while graph target remains visible and readable",
        ("forced-colors", "graph-map", "live"), forced_colors=True, action="graph-map", full_page=False,
    ),
    ScenarioSpec(
        "reduced-motion", "reduced-motion.png", "live", "/__graph", "graph",
        "reduced-motion media state is active and graph readiness reaches a settled terminal state",
        ("reduced-motion", "graph-map", "live"), reduced_motion=True, action="graph-map", full_page=False,
    ),
    ScenarioSpec(
        "zoom-400", "zoom-400.png", "live", "/tables/orders", "document",
        "real Chrome page zoom is 400 percent with one-column content and no document horizontal overflow",
        ("zoom-400", "live"), viewport=(1440, 1000), zoom_percent=400, action="zoom-400",
        zoom_mechanism="chrome-page-zoom-keyboard",
    ),
) + (
    ScenarioSpec("f01-shell-1440", "f01-shell-1440.png", "live", "/", "document",
                 "stable shell destination order and current Index destination on the checked-in zero-concept OKF fixture", ("F01", "viewport-1440", "index-empty", "studio-idle", "F13", "F14", "F19"), action="index-empty", bundle="empty-okf"),
    ScenarioSpec("f02-index-filter-mismatch", "f02-index-filter-mismatch-1280.png", "live", "/", "document",
                 "index filter mismatch exposes a recoverable empty state", ("F02", "index-filter-mismatch", "viewport-1280", "demo-bundle"), viewport=(1280, 800), action="index-filter-mismatch"),
    ScenarioSpec("f03-concept-outline", "f03-concept-outline-1440.png", "live", "/reference/spec", "document",
                 "dense long concept has a reachable filtered outline", ("F03", "F18", "concept-outline", "viewport-1440", "dense-docs-bundle"), action="concept-outline", bundle="dense-docs"),
    ScenarioSpec("f03-concept-rendered", "f03-concept-rendered-1280.png", "live", "/tables/orders", "document",
                 "concept rendered mode is selected in the page-local view bar", ("concept-rendered", "viewport-1280", "demo-bundle"), viewport=(1280, 800), action="concept-rendered"),
    ScenarioSpec("f03-concept-source", "f03-concept-source-1280.png", "live", "/tables/orders", "document",
                 "concept source mode is selected and readable", ("concept-source", "viewport-1280", "demo-bundle"), viewport=(1280, 800), action="concept-source"),
    ScenarioSpec("f03-concept-split", "f03-concept-split-1440.png", "live", "/tables/orders", "document",
                 "concept split mode is selected and both projections are visible", ("concept-split", "viewport-1440", "demo-bundle"), action="studio-split"),
    ScenarioSpec("f03-concept-focus", "f03-concept-focus-1440.png", "live", "/tables/orders", "document",
                 "Focus view hides competing chrome while preserving the article", ("concept-focus-view", "F12", "viewport-1440", "demo-bundle"), action="concept-focus-view"),
    ScenarioSpec("f05-index-graph-name", "f05-index-graph-name-1440.png", "live", "/", "document",
                 "graph preview has one concise purpose-and-count accessible name", ("F05", "viewport-1440", "dense-docs-bundle", "index-graph-thumbnail-desktop"), action="graph-preview-name", bundle="dense-docs"),
    ScenarioSpec("f05-index-graph-name-mobile", "f05-index-graph-name-390.png", "live", "/", "document",
                 "mobile graph preview keeps its visible label in the purpose-and-count accessible name", ("F05", "viewport-390", "dense-docs-bundle", "index-graph-thumbnail-mobile"), viewport=(390, 844), action="graph-preview-name", bundle="dense-docs"),
    ScenarioSpec("f06-graph-keyboard", "f06-graph-keyboard-journey-1440.png", "live", "/__graph", "graph",
                 "keyboard list selection, typed path and detail are synchronized", ("F06", "F07", "F08", "F16", "F17", "graph-keyboard-journey", "viewport-1440", "dense-docs-bundle"), action="graph-keyboard-journey", full_page=False, bundle="dense-docs"),
    ScenarioSpec("f09-search-expanded", "f09-search-expanded-1440.png", "live", "/__search?q=tunnel", "search",
                 "one Search evidence disclosure is expanded with query state retained", ("F09", "F10", "F11", "F21", "search-expanded", "viewport-1440", "dense-docs-bundle"), action="search-expanded", bundle="dense-docs"),
    ScenarioSpec("f20-forced-colors-dense", "f20-forced-colors-dense-1440.png", "live", "/__graph", "graph",
                 "dense graph remains distinguishable in forced colors", ("F20", "F22", "forced-colors", "viewport-1440", "dense-docs-bundle"), forced_colors=True, action="graph-map", full_page=False, bundle="dense-docs"),
    ScenarioSpec("f23-compact-density", "f23-compact-density-1280.png", "live", "/__search?q=orders", "search",
                 "compact density is runtime-owned and Search remains readable", ("F23", "compact-density", "viewport-1280", "demo-bundle"), viewport=(1280, 800), density="compact", action="search-result"),
    ScenarioSpec("f24-coffee-showcase", "f24-coffee-showcase-1440.png", "live", "/", "document",
                 "Coffee Light is captured as the editorial showcase", ("F24", "coffee-showcase", "viewport-1440", "demo-bundle"), resolved_theme="coffee-light", theme_preference="light", theme_family="coffee", theme_mode="light", action="theme"),
    ScenarioSpec("studio-error-retry", "studio-error-retry-1440.png", "live", "/tables/orders", "document",
                 "Studio data error exposes a bounded Retry action", ("studio-error", "studio-retry", "F14", "viewport-1440", "demo-bundle"), action="studio-error-retry"),
    ScenarioSpec("studio-read-only-authority", "studio-read-only-authority-1440.png", "live", "/tables/orders", "document",
                 "Studio states read-only authority without exposing mutation controls or tokens", ("studio-read-only-authority", "viewport-1440", "demo-bundle"), action="studio-authority", server_mode="read-only"),
    ScenarioSpec("studio-proposal-enabled", "studio-proposal-enabled-1440.png", "live", "/tables/orders", "document",
                 "Studio exposes proposal review only when runtime capability is enabled", ("studio-proposal-enabled", "F15", "viewport-1440", "demo-bundle"), action="studio-authority", server_mode="proposal-enabled"),
    ScenarioSpec("studio-unavailable", "studio-unavailable-390.png", "live", "/tables/orders", "document",
                 "mobile Studio unavailable state is explicit and authored content remains usable", ("studio-unavailable", "viewport-390", "demo-bundle"), viewport=(390, 844), action="boot-failure", expected_event_fragments=()),
    ScenarioSpec("zoom-200", "zoom-200.png", "live", "/tables/orders", "document",
                 "real Chrome page zoom is 200 percent with task-equivalent outline access", ("zoom-200", "F04", "demo-bundle"), viewport=(1440, 900), zoom_percent=200, action="zoom-200", zoom_mechanism="chrome-page-zoom-keyboard"),
) + tuple(
    _technical_auto_scenario(source, os_mode, storage)
    for source in ("live", "static", "spa", "single-file")
    for os_mode in ("light", "dark")
    for storage in ("none", "denied")
)


def _scenario_row(spec: ScenarioSpec) -> dict[str, Any]:
    row = dict(spec._asdict())
    width, height = spec.viewport
    row["viewport"] = {"width": width, "height": height}
    family, mode = _public_theme(spec)
    # Public serialization keeps Auto split into independent values.  A
    # concrete ``technical-light``/``technical-dark`` value is paint output,
    # not a public preference token.
    row["theme_family"] = family
    row["theme_mode"] = mode
    row["theme_preference"] = mode
    for key in ("coverage", "expected_http_status", "expected_event_fragments"):
        row[key] = list(row[key])
    return row


def _validate_scenario_specs(specs: tuple[ScenarioSpec, ...] = SCENARIO_SPECS) -> None:
    ids = [spec.id for spec in specs]
    artifacts = [spec.artifact for spec in specs]
    if len(ids) != len(set(ids)):
        raise ValueError("release capture scenario IDs must be unique")
    if len(artifacts) != len(set(artifacts)):
        raise ValueError("release capture artifact names must be unique")
    if any(spec.source not in _VALID_SOURCES for spec in specs):
        raise ValueError("release capture scenario has an unsupported source")
    if any(spec.resolved_theme is not None and spec.resolved_theme not in _VALID_THEMES for spec in specs):
        raise ValueError("release capture scenario has an unsupported resolved theme")
    for spec in specs:
        family, mode = _public_theme(spec)
        if family is not None and family not in _VALID_THEME_FAMILIES:
            raise ValueError(f"scenario {spec.id} has an unsupported theme family")
        if mode is not None and mode not in _VALID_THEME_MODES:
            raise ValueError(f"scenario {spec.id} has an unsupported theme mode")
        if spec.storage not in _VALID_STORAGE_MODES:
            raise ValueError(f"scenario {spec.id} has an unsupported storage mode")
        if spec.bundle not in _VALID_BUNDLES:
            raise ValueError(f"scenario {spec.id} has an unsupported bundle fixture")
        if spec.density not in _VALID_DENSITIES:
            raise ValueError(f"scenario {spec.id} has an unsupported density")
        if spec.server_mode not in _VALID_SERVER_MODES:
            raise ValueError(f"scenario {spec.id} has an unsupported server mode")
        if bool(spec.zoom_percent) != bool(spec.zoom_mechanism):
            raise ValueError(f"scenario {spec.id} has incomplete zoom provenance")
        if spec.zoom_percent and spec.zoom_mechanism != "chrome-page-zoom-keyboard":
            raise ValueError(f"scenario {spec.id} uses a viewport/compositor zoom proxy")
        if spec.javascript and (family is None or mode is None):
            raise ValueError(f"JavaScript scenario {spec.id} has no public theme state")
    coverage = {item for spec in specs for item in spec.coverage}
    unsupported_actions = {spec.action for spec in specs} - _IMPLEMENTED_ACTIONS
    if unsupported_actions:
        raise ValueError("release scenarios have no runtime dispatcher: " + ", ".join(sorted(unsupported_actions)))
    missing = sorted(_REQUIRED_COVERAGE - coverage)
    if missing:
        raise ValueError("release capture scenario matrix is incomplete: " + ", ".join(missing))


def release_scenario_manifest() -> dict[str, Any]:
    """Return the deterministic, executable final-release scenario matrix."""

    _validate_scenario_specs()
    return {
        "schema": 2,
        "name": "okf-loom-v2-ui-overhaul-final",
        "required_coverage": sorted(_REQUIRED_COVERAGE),
        "supporting_artifacts": list(_SUPPORTING_ARTIFACTS),
        "screen_reader_proof": {
            "artifact": "manual-screen-reader-session.md",
            "status": "PENDING HUMAN",
            "automated": False,
            "counts_as_executed_proof": False,
        },
        "evidence_status": "INTERIM AUTOMATED — PENDING EXTERNAL ZOOM AND HUMAN SCREEN READER",
        "final_release_evidence": False,
        "scenarios": [_scenario_row(spec) for spec in SCENARIO_SPECS],
    }


def _git_bytes(
    args: list[str] | tuple[str, ...], *, input_bytes: bytes | None = None,
    _root: Any = _sealed_repository_root, _git: str = _SEALED_GIT,
    _environment: MappingProxyType[str, str] = _SEALED_GIT_ENV,
) -> bytes:
    root = _root()
    try:
        result = subprocess.run(
            [_git, "-C", str(root), "--no-replace-objects", *args], cwd=root,
            env=dict(_environment), input=input_bytes, check=False, capture_output=True,
        )
    except OSError as exc:
        raise CapturePreconditionError("sealed Git command could not execute") from exc
    if result.returncode:
        detail = (result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes)
                  else str(result.stderr))[-1000:].strip()
        raise CapturePreconditionError(
            f"sealed Git command failed ({args[0] if args else 'unknown'}): {detail}"
        )
    return result.stdout if isinstance(result.stdout, bytes) else result.stdout.encode()


def _git_output(args: list[str] | tuple[str, ...], *, _root: Any = _sealed_repository_root) -> str:
    try:
        return _git_bytes(args, _root=_root).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CapturePreconditionError("sealed Git command returned non-UTF-8 metadata") from exc


def _strict_git_oid(raw: bytes, *, object_format: str, label: str) -> str:
    """Decode exactly one canonically newline-framed full lowercase object ID."""

    width = {"sha1": 40, "sha256": 64}.get(object_format)
    if width is None:
        raise CapturePreconditionError("sealed Git object format is unsupported")
    if len(raw) != width + 1 or not raw.endswith(b"\n"):
        raise CapturePreconditionError(f"{label} does not use canonical Git OID framing")
    try:
        value = raw[:-1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise CapturePreconditionError(f"{label} is not an ASCII Git object ID") from exc
    if any(char not in "0123456789abcdef" for char in value):
        raise CapturePreconditionError(f"{label} is not a strict full Git object ID")
    return value


def _strict_git_object_format(raw: bytes) -> str:
    """Accept one canonically framed, explicitly supported object format."""

    if raw == b"sha1\n": return "sha1"
    if raw == b"sha256\n": return "sha256"
    raise CapturePreconditionError("sealed Git object format is non-canonical or unsupported")


def _git_provenance(*, require_clean: bool) -> dict[str, Any]:
    """Bind one commit/tree pair, then separately observe symbolic HEAD."""

    object_format = _strict_git_object_format(_git_bytes(("rev-parse", "--show-object-format")))
    # HEAD is resolved exactly once for the transaction pair. Every object
    # query below is addressed by that immutable OID, never symbolic HEAD.
    head = _strict_git_oid(
        _git_bytes(("rev-parse", "--verify", "HEAD")),
        object_format=object_format, label="captured HEAD",
    )
    if _git_bytes(("cat-file", "-t", head)) != b"commit\n":
        raise CapturePreconditionError("captured HEAD object is not a commit")
    tree = _strict_git_oid(
        _git_bytes(("rev-parse", "--verify", f"{head}^{{tree}}")),
        object_format=object_format, label="captured commit tree",
    )
    status = _git_output(("status", "--porcelain=v1", "--untracked-files=all"))
    diff = _git_output(("diff", "--binary", head))
    observed_head = _strict_git_oid(
        _git_bytes(("rev-parse", "HEAD")),
        object_format=object_format, label="observed symbolic HEAD",
    )
    provenance = {
        "head": head,
        "tree": tree,
        "object_format": object_format,
        "observed_head": observed_head,
        "clean": not bool(status),
        "worktree_digest": _digest({"status": status, "diff": diff}),
        "worktree_status": status.splitlines(),
    }
    if require_clean and not provenance["clean"]:
        raise CapturePreconditionError(
            "release capture requires a clean worktree; do not publish dirty screenshots"
        )
    return provenance


def _assert_bound_revision(expected_head: str, expected_tree: str, observed: dict[str, Any]) -> None:
    if ((observed.get("head"), observed.get("tree")) != (expected_head, expected_tree)
            or observed.get("observed_head", observed.get("head")) != expected_head):
        raise CapturePreconditionError(
            "git provenance moved and no longer matches the transaction-bound revision"
        )


def _assert_provenance_stable(before: dict[str, Any], after: dict[str, Any]) -> None:
    keys = ("head", "tree", "observed_head", "clean", "worktree_digest")
    if any(before.get(key) != after.get(key) for key in keys):
        raise CapturePreconditionError(
            "git provenance drifted while capture was running; discard private evidence and retry"
        )


def _coordination_authority(parent_fd: int, authority_name: str) -> int:
    created = False
    try: os.mkdir(authority_name, 0o700, dir_fd=parent_fd); created = True
    except FileExistsError: pass
    authority_fd = os.open(authority_name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    if created: os.fchmod(authority_fd, 0o700)
    info = os.fstat(authority_fd)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(authority_fd); raise RuntimeError("unsafe capture coordination authority")
    pinned = os.stat(authority_name, dir_fd=parent_fd, follow_symlinks=False)
    if (pinned.st_dev, pinned.st_ino) != (info.st_dev, info.st_ino):
        os.close(authority_fd); raise RuntimeError("capture authority identity changed")
    return authority_fd


def _secure_regular_fd(dir_fd: int, name: str) -> int:
    fd = os.open(name, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dir_fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(fd); raise RuntimeError("unsafe capture coordination file")
    return fd


def _exists_at(dir_fd: int, name: str) -> bool:
    try: os.stat(name, dir_fd=dir_fd, follow_symlinks=False); return True
    except FileNotFoundError: return False


def _write_journal(dir_fd: int, payload: bytes) -> None:
    name = f"journal.tmp-{os.getpid()}-{threading.get_ident()}"
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dir_fd)
    try: os.write(fd, payload); os.fsync(fd)
    finally: os.close(fd)
    os.replace(name, "journal.json", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _rmtree_at(parent_fd: int, name: str) -> None:
    try: fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    except FileNotFoundError: return
    try:
        for child in os.listdir(fd):
            info = os.stat(child, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode): _rmtree_at(fd, child)
            else: os.unlink(child, dir_fd=fd)
    finally: os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


class PublicationProvenance(NamedTuple):
    """Immutable repository and consumed-input authority for one publication."""

    head: str
    tree: str
    clean: bool
    worktree_digest: str
    inventory_digest: str | None = None
    bundles: tuple[AdmittedBundle, ...] = ()
    runtime: "ImmutableRuntimeWorkspace | None" = None
    source_kind: str = "clean-head"


def _publication_provenance(
    provenance: dict[str, Any], *, inventory_digest: str | None = None,
    bundles: tuple[AdmittedBundle, ...] = (), runtime: "ImmutableRuntimeWorkspace | None" = None,
) -> PublicationProvenance:
    return PublicationProvenance(
        head=str(provenance.get("commit", provenance.get("head"))), tree=str(provenance["tree"]),
        clean=provenance.get("clean") is True,
        worktree_digest=str(provenance.get("worktree_digest", "")),
        inventory_digest=inventory_digest, bundles=bundles, runtime=runtime,
        source_kind=provenance.get("kind", "clean-head"),
    )


def _verify_publication_provenance(expected: PublicationProvenance) -> None:
    """Verify transaction-owned immutable snapshots, not moving symbolic refs."""

    if expected.source_kind == "selected-commit":
        if (expected.runtime is None or not expected.inventory_digest
                or not expected.runtime.strict_inventory or expected.clean
                or (expected.head, expected.tree) != (expected.runtime.head, expected.runtime.tree)):
            raise CapturePreconditionError("publication lacks selected-source admission")
    elif expected.source_kind != "clean-head":
        raise CapturePreconditionError("publication has unknown source admission")
    elif not expected.clean:
        raise CapturePreconditionError("publication lacks clean-worktree admission")
    if expected.runtime is not None:
        expected.runtime.verify()
    if expected.inventory_digest is not None:
        if expected.runtime is not None:
            current = _runtime_inventory(expected.runtime)
        elif expected.bundles:
            current = _inventory(expected.bundles[0], additional_bundles=expected.bundles[1:])
        else:
            raise CapturePreconditionError("publication inventory authority is missing")
        if _digest(current) != expected.inventory_digest:
            raise CapturePreconditionError("publication input inventory drifted")


def _publish_generation(
    stage: Path, destination: Path, *, expected_provenance: PublicationProvenance,
) -> None:
    """Serialized recoverable two-phase replacement; readers may observe absence."""
    import fcntl
    destination = destination.parent.resolve() / destination.name
    stage = stage.parent.resolve() / stage.name
    if stage.parent != destination.parent: raise RuntimeError("capture stage must be a destination sibling")
    resources = ExitStack()
    try:
      parent_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
      resources.callback(os.close, parent_fd)
      parent_info = os.fstat(parent_fd)
      authority_name = f".{destination.name}.capture-authority"
      with _PUBLISH_THREAD_LOCKS_GUARD:
          thread_lock = _PUBLISH_THREAD_LOCKS.setdefault(f"{parent_info.st_dev}:{parent_info.st_ino}:{destination.name}", threading.Lock())
      fcntl.flock(parent_fd, fcntl.LOCK_EX)
      authority_fd = _coordination_authority(parent_fd, authority_name)
      resources.callback(os.close, authority_fd)
      lock_fd = _secure_regular_fd(authority_fd, "owner.lock")
      resources.callback(os.close, lock_fd)
      with thread_lock, os.fdopen(lock_fd, "r+b", closefd=False) as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        backup_name = "backup"
        # Recover a process interrupted after old->backup but before rollback.
        destination_exists = _exists_at(parent_fd, destination.name)
        backup_exists = _exists_at(authority_fd, backup_name)
        if not destination_exists and backup_exists:
            os.replace(backup_name, destination.name, src_dir_fd=authority_fd, dst_dir_fd=parent_fd)
        elif backup_exists: _rmtree_at(authority_fd, backup_name)
        if _exists_at(authority_fd, "journal.json"):
            ji = os.stat("journal.json", dir_fd=authority_fd, follow_symlinks=False)
            if not stat.S_ISREG(ji.st_mode) or ji.st_nlink != 1 or ji.st_uid != os.getuid(): raise RuntimeError("unsafe capture journal")
        _write_journal(authority_fd, (json.dumps({"destination": str(destination), "stage": str(stage)}) + "\n").encode())
        moved_old = False
        try:
            # This is deliberately a fixed internal verifier, not a caller hook.
            # It checks only transaction-owned immutable snapshots. Symbolic
            # branch movement after complete materialization is irrelevant.
            _verify_publication_provenance(expected_provenance)
            if _exists_at(parent_fd, destination.name):
                os.replace(destination.name, backup_name, src_dir_fd=parent_fd, dst_dir_fd=authority_fd); moved_old = True
            try:
                os.replace(stage.name, destination.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except BaseException:
                if moved_old and _exists_at(authority_fd, backup_name) and not _exists_at(parent_fd, destination.name):
                    os.replace(backup_name, destination.name, src_dir_fd=authority_fd, dst_dir_fd=parent_fd)
                raise
        except BaseException:
            # If new is complete, retain it; otherwise restore complete old.
            if not _exists_at(parent_fd, destination.name) and _exists_at(authority_fd, backup_name):
                os.replace(backup_name, destination.name, src_dir_fd=authority_fd, dst_dir_fd=parent_fd)
            raise
        finally:
            if _exists_at(parent_fd, destination.name) and _exists_at(authority_fd, backup_name): _rmtree_at(authority_fd, backup_name)
            try: os.unlink("journal.json", dir_fd=authority_fd)
            except FileNotFoundError: pass
            if _exists_at(parent_fd, stage.name): _rmtree_at(parent_fd, stage.name)
    finally:
        resources.close()


class _PortReservation:
    """Runner-owned ephemeral listener choice, held until immediately before spawn."""

    def __init__(self, requested: int | None = None) -> None:
        self.socket = socket.socket()
        try:
            self.socket.bind(("127.0.0.1", requested or 0))
        except OSError as exc:
            self.socket.close()
            raise CapturePreconditionError("capture server port already has a listener") from exc
        self.port = int(self.socket.getsockname()[1])

    def release_for_spawn(self) -> None:
        self.socket.close()


def _port() -> int:
    """Compatibility helper for focused browser tests; production holds a reservation."""
    reservation = _PortReservation()
    port = reservation.port
    reservation.release_for_spawn()
    return port


def _tree_inventory(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            if path.is_symlink():
                raise CapturePreconditionError("capture build contains an unsupported symlink")
            continue
        data = path.read_bytes()
        rows.append({"path": path.relative_to(root).as_posix(), "bytes": len(data),
                     "sha256": hashlib.sha256(data).hexdigest()})
    if not rows:
        raise CapturePreconditionError("capture build inventory is empty")
    return rows


def _fetch_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5) as response:
        if response.status != 200:
            raise CaptureFailure({"code": "served-byte-status", "phase": "server-provenance",
                                  "url": url, "status": response.status})
        return response.read()


def _static_served_proof(base: str, root: Path, probe: str) -> list[dict[str, object]]:
    inventory = _tree_inventory(root)
    candidates = [str(row["path"]) for row in inventory if str(row["path"]) == probe.lstrip("/")
                  or str(row["path"]).endswith((".css", ".js"))]
    if probe.lstrip("/") not in candidates:
        raise CapturePreconditionError("served entrypoint is absent from the sealed build")
    proof = []
    by_path = {str(row["path"]): row for row in inventory}
    for relative in candidates:
        data = _fetch_bytes(base + "/" + urllib.parse.quote(relative, safe="/"))
        digest = hashlib.sha256(data).hexdigest()
        if digest != by_path[relative]["sha256"] or len(data) != by_path[relative]["bytes"]:
            raise CapturePreconditionError("served static bytes do not match the sealed build")
        proof.append({"path": relative, "role": "entrypoint" if relative == probe.lstrip("/") else "critical-asset",
                      "bytes": len(data), "sha256": digest})
    return proof


def _live_served_proof(base: str, runtime: "ImmutableRuntimeWorkspace") -> list[dict[str, object]]:
    entrypoint = _fetch_bytes(base + "/")
    rows = [{"path": "/", "role": "entrypoint", "bytes": len(entrypoint),
             "sha256": hashlib.sha256(entrypoint).hexdigest()}]
    urls = sorted(set(match.decode("utf-8") for match in re.findall(
        rb'(?:src|href)="([^"?#]+\.(?:css|js)(?:\?[^"#]*)?)"', entrypoint)))
    runtime_hashes = {(Path(str(row["path"])).name, str(row["sha256"])) for row in runtime.inventory}
    for relative in urls:
        url = urllib.parse.urljoin(base + "/", relative)
        data = _fetch_bytes(url)
        digest = hashlib.sha256(data).hexdigest()
        name = Path(urllib.parse.urlparse(url).path).name
        if (name, digest) not in runtime_hashes:
            raise CapturePreconditionError("served live asset does not match the immutable runtime")
        rows.append({"path": urllib.parse.urlparse(url).path, "role": "critical-asset",
                     "bytes": len(data), "sha256": digest})
    if len(rows) == 1:
        raise CapturePreconditionError("live entrypoint exposed no verifiable critical assets")
    return rows


def _verify_server_provenance(record: dict[str, Any], *, runtime: "ImmutableRuntimeWorkspace") -> None:
    if (record.get("started_by_runner") is not True or record.get("cwd") != str(runtime.root)
            or record.get("head") != runtime.head or record.get("tree") != runtime.tree
            or not isinstance(record.get("pid"), int) or record["pid"] <= 0
            or record.get("readiness") != "http-200-owned-process"
            or not record.get("served_bytes")):
        raise CapturePreconditionError("capture server provenance is incomplete or has the wrong source")
    for row in record["served_bytes"]:
        if not isinstance(row, dict) or len(str(row.get("sha256", ""))) != 64 or not row.get("bytes"):
            raise CapturePreconditionError("capture server served-byte provenance is invalid")
    build = record.get("build")
    if isinstance(build, dict):
        inventory = build.get("inventory")
        if (not isinstance(inventory, list) or build.get("inventory_digest") != _digest(inventory)
                or build.get("cwd") != str(runtime.root) or build.get("head") != runtime.head
                or build.get("tree") != runtime.tree):
            raise CapturePreconditionError("capture server build provenance is invalid")
        expected = {(str(row["path"]), str(row["sha256"]), int(row["bytes"])) for row in inventory}
        if any((str(row["path"]), str(row["sha256"]), int(row["bytes"])) not in expected
               for row in record["served_bytes"]):
            raise CapturePreconditionError("served static assets do not match the sealed build inventory")
    else:
        runtime_assets = {(Path(str(row["path"])).name, str(row["sha256"]), int(row["bytes"]))
                          for row in runtime.inventory}
        for row in record["served_bytes"]:
            if row.get("role") == "critical-asset" and (
                Path(str(row["path"])).name, str(row["sha256"]), int(row["bytes"])
            ) not in runtime_assets:
                raise CapturePreconditionError("served live assets do not match the immutable runtime inventory")


def _validate_server_bindings(
    artifacts: list[dict[str, Any]], records: list[dict[str, Any]], *,
    runtime: "ImmutableRuntimeWorkspace",
) -> None:
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        _verify_server_provenance(record, runtime=runtime)
        identity = str(record.get("id", ""))
        if not identity or identity in by_id:
            raise CapturePreconditionError("capture server provenance IDs are missing or duplicated")
        by_id[identity] = record
    for artifact in artifacts:
        record = by_id.get(str(artifact.get("server_provenance_id", "")))
        if (record is None or artifact.get("server_provenance_digest") != _digest(record)
                or artifact.get("served_entrypoint_sha256") != next(
                    (row["sha256"] for row in record["served_bytes"] if row["role"] == "entrypoint"), None)
                or len(str(artifact.get("sha256", ""))) != 64):
            raise CapturePreconditionError("capture artifact is not bound to its server and screenshot bytes")


def _env(*, runtime: "ImmutableRuntimeWorkspace") -> dict[str, str]:
    """Return the narrow subprocess environment owned by an immutable workspace."""

    return runtime.subprocess_env()


def _wait(base: str, process: subprocess.Popen[bytes], timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise CaptureFailure({"code": "server-start-exit", "phase": "server-start",
                                  "returncode": process.returncode, "base": base})
        try:
            with urllib.request.urlopen(base + "/", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(.1)
    raise CaptureFailure({"code": "server-start-timeout", "phase": "server-start",
                          "timeoutSeconds": timeout, "base": base})


def _wait_url(url: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status in {200, 400}:
                    return
        except OSError:
            pass
        time.sleep(0.01)
    raise CaptureFailure({"code": "static-server-start-timeout", "phase": "server-start",
                          "timeoutSeconds": timeout, "url": url})


class _StaticServer:
    """Small deterministic stdlib server for a private built target tree."""

    def __init__(self, root: Path, *, runtime: "ImmutableRuntimeWorkspace", target: str,
                 build: dict[str, Any], probe: str = "/index.html", port: int | None = None) -> None:
        self.root = root
        self.runtime = runtime
        self.target = target
        self.build = build
        self.probe = probe
        self.requested_port = port
        self.process: subprocess.Popen[bytes] | None = None
        self.provenance: dict[str, Any] | None = None

    def __enter__(self) -> str:
        reservation = _PortReservation(self.requested_port)
        port = reservation.port
        base = f"http://127.0.0.1:{port}"
        command = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
                   "--directory", str(self.root)]
        reservation.release_for_spawn()
        try:
            self.process = subprocess.Popen(command, cwd=self.runtime.root, env=_env(runtime=self.runtime),
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _wait(base, self.process)
            served = _static_served_proof(base, self.root, self.probe)
            self.provenance = {
                "id": f"{self.target}-{port}", "kind": "sealed-build-http", "target": self.target,
                "started_by_runner": True, "command": command, "cwd": str(self.runtime.root),
                "pid": self.process.pid, "port": port, "head": self.runtime.head, "tree": self.runtime.tree,
                "readiness": "http-200-owned-process", "build": self.build, "served_bytes": served,
            }
            _verify_server_provenance(self.provenance, runtime=self.runtime)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return base

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        process = self.process
        errors: list[str] = []
        confirmed = process is None
        if process is not None:
            try: confirmed = process.poll() is not None
            except BaseException as exc: errors.append(f"poll:{type(exc).__name__}")
            if not confirmed:
                try: process.terminate()
                except ProcessLookupError: pass
                except BaseException as exc: errors.append(f"terminate:{type(exc).__name__}")
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: pass
                except BaseException as exc: errors.append(f"wait:{type(exc).__name__}")
                try: confirmed = process.poll() is not None
                except BaseException as exc: errors.append(f"poll:{type(exc).__name__}")
            if not confirmed:
                try: process.kill()
                except ProcessLookupError: pass
                except BaseException as exc: errors.append(f"kill:{type(exc).__name__}")
                try: process.wait(timeout=5)
                except BaseException as exc: errors.append(f"final-wait:{type(exc).__name__}")
                try: confirmed = process.poll() is not None
                except BaseException as exc: errors.append(f"poll:{type(exc).__name__}")
        self.process = None if confirmed else process
        if errors or not confirmed:
            raise CaptureFailure({"code": "server-cleanup-failure", "phase": "server-cleanup",
                                  "processExitConfirmed": confirmed, "errors": errors})


class _LiveServer:
    """Own one bounded checkout-local Studio process for a fixture."""

    def __init__(
        self, bundle: Path | "AdmittedBundle" | "RuntimeBundle", *,
        mode: str = "editable", runtime: "ImmutableRuntimeWorkspace", port: int | None = None,
    ) -> None:
        self.bundle = _bundle_path(bundle)
        self.mode = mode
        self.runtime = runtime
        self.requested_port = port
        self.process: subprocess.Popen[bytes] | None = None
        self._state_authority: Any | None = None
        self._state_owner = None  # compatibility: finalizer-backed ownership is forbidden
        self.state_root: Path | None = None
        self.cleanup_result: Any | None = None
        self.retention_record: dict[str, Any] | None = None
        self.provenance: dict[str, Any] | None = None

    def __enter__(self) -> str:
        root = self.runtime.root
        reservation = _PortReservation(self.requested_port)
        port = reservation.port
        base = f"http://127.0.0.1:{port}"
        command = [str(root / "scripts/okf-loom"), "serve", str(self.bundle),
                   "--host", "127.0.0.1", "--port", str(port), "--no-watch", "--no-open"]
        if self.mode == "read-only":
            command.append("--no-edit")
        elif self.mode == "proposal-enabled":
            command.append("--enable-proposal-review")
        self.runtime.audit("serve", executable=command[0], cwd=root, inputs=(self.bundle,))
        state_parent = next(
            (candidate for candidate in (Path("/tmp"), Path("/var/tmp"))
             if candidate.is_dir() and not candidate.resolve().is_relative_to(root.resolve(strict=True))),
            None,
        )
        if state_parent is None:
            raise CapturePreconditionError("no mutable state authority is available outside immutable runtime")
        try:
            from okf_loom.session_authority import temporary_session_authority
            self._state_authority = temporary_session_authority(
                prefix=f"okf-live-{self.mode}-", parent=state_parent)
            self.state_root = self._state_authority.display_path
            env = _env(runtime=self.runtime)
            env["OKF_LOOM_SESSION_ROOT"] = str(self.state_root)
            env["OKF_LOOM_IMMUTABLE_ROOT"] = str(root.resolve(strict=True))
            try:
                reservation.release_for_spawn()
                self.process = subprocess.Popen(
                    command, cwd=root, env=env, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                raise CaptureFailure({"code": "server-spawn-failure", "phase": "server-spawn",
                                      "error": type(exc).__name__}) from exc
            _wait(base, self.process)
            # Lifecycle unit tests use deliberately tiny process doubles. Real
            # subprocesses always expose a positive PID and must complete the
            # served-byte/source verifier before any browser can navigate.
            if isinstance(getattr(self.process, "pid", None), int):
                served = _live_served_proof(base, self.runtime)
                self.provenance = {
                    "id": f"live-{self.mode}-{port}", "kind": "sealed-live-studio", "mode": self.mode,
                    "started_by_runner": True, "command": command, "cwd": str(root),
                    "pid": self.process.pid, "port": port, "head": self.runtime.head, "tree": self.runtime.tree,
                    "readiness": "http-200-owned-process", "served_bytes": served,
                }
                _verify_server_provenance(self.provenance, runtime=self.runtime)
        except BaseException:
            reservation.release_for_spawn()
            self.__exit__(None, None, None)
            raise
        return base

    def assert_running(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            raise CaptureFailure({
                "code": "server-exit", "phase": "server",
                "returncode": None if process is None else process.returncode,
            })

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        process = self.process
        authority = self._state_authority
        state_root = self.state_root
        errors: list[str] = []
        confirmed_dead = process is None

        def poll_dead() -> bool:
            try:
                return process is None or process.poll() is not None or process.returncode is not None
            except BaseException as exc:
                errors.append(f"poll:{type(exc).__name__}")
                return False

        if process is not None:
            if poll_dead():
                confirmed_dead = True
            else:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                except BaseException as exc:
                    errors.append(f"terminate:{type(exc).__name__}")
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                except BaseException as exc:
                    errors.append(f"wait:{type(exc).__name__}")
                confirmed_dead = poll_dead()
            if not confirmed_dead:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                except BaseException as exc:
                    errors.append(f"kill:{type(exc).__name__}")
                try:
                    process.wait(timeout=5)
                except BaseException as exc:
                    errors.append(f"final-wait:{type(exc).__name__}")
                confirmed_dead = poll_dead()

        disposition = "active-retained"
        descriptor_retained = False
        if confirmed_dead and authority is not None:
            try:
                cleanup = authority.dispose()
                if cleanup.disposition != "sanitized-retained-tree":
                    raise OSError("session authority cleanup result was not sanitized")
                self.cleanup_result = cleanup
                disposition = cleanup.disposition
                registry_error_count = len(errors)
                self.retention_record = _record_state_retention(
                    authority, errors, disposition=disposition,
                    reason="confirmed-death-sanitized", retain_descriptor=False)
                disposition = self.retention_record["stateDisposition"]
                descriptor_retained = len(errors) > registry_error_count
            except BaseException as exc:
                errors.append(f"state-cleanup:{type(exc).__name__}")
                disposition = "cleanup-failed-retained"
                self.retention_record = _record_state_retention(
                    authority, errors, disposition=disposition,
                    reason="confirmed-death-cleanup-failed", retain_descriptor=True)
                disposition = self.retention_record["stateDisposition"]
                descriptor_retained = True
        elif authority is not None:
            self.retention_record = _retain_live_state(authority, errors)
            disposition = self.retention_record["stateDisposition"]
            descriptor_retained = True

        if authority is not None and not descriptor_retained and authority.fd >= 0:
            authority.close()
        self.process = None
        self._state_authority = authority if descriptor_retained else None
        self.state_root = None
        if errors or not confirmed_dead:
            raise CaptureFailure({
                "code": "server-cleanup-failure",
                "phase": "server-cleanup",
                "processExitConfirmed": confirmed_dead,
                "stateDisposition": disposition,
                "stateIdentity": None if self.retention_record is None else {
                    "device": self.retention_record["device"],
                    "inode": self.retention_record["inode"],
                },
                "errors": errors,
            })


def _build_target(
    bundle: Path | "AdmittedBundle" | "RuntimeBundle", target: str, out_dir: Path,
    *, runtime: "ImmutableRuntimeWorkspace",
) -> dict[str, Any]:
    root = runtime.root
    bundle_path = _bundle_path(bundle)
    out_dir.mkdir(parents=True, exist_ok=True)
    executable = root / "scripts/okf-loom"
    runtime.audit("build", executable=executable, cwd=root, inputs=(bundle_path,))
    command = [str(executable), "build", str(bundle_path), "--target", target, "--out", str(out_dir)]
    result = subprocess.run(
        command,
        cwd=root, env=_env(runtime=runtime), text=True, capture_output=True,
    )
    if result.returncode:
        raise CaptureFailure({
            "code": "build-failure", "phase": "build", "target": target,
            "message": f"target {target!r} failed with exit {result.returncode}",
            "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:],
        })
    inventory = _tree_inventory(out_dir)
    return {
        "target": target, "command": command, "cwd": str(root), "head": runtime.head,
        "tree": runtime.tree, "inventory": inventory, "inventory_digest": _digest(inventory),
    }


def _tracked_paths(*, _root: Any = _sealed_repository_root) -> list[tuple[Path, str, bool]]:
    """Return every checkout-tracked path using NUL-framed Git metadata.

    A hand-maintained list of renderer modules is too easy to leave stale
    when a new imported consumer is added.  The release gate therefore uses a
    conservative tracked-code inventory: every stage-0 tracked path is
    fingerprinted, including support code not currently obvious from the
    capture script.  The selected bundle is added separately when it lives
    outside this checkout.
    """

    root = _root()
    raw = _git_output(("ls-files", "-z", "--stage"))
    paths: list[tuple[Path, str, bool]] = []
    for record in raw.split("\0"):
        if not record:
            continue
        header, separator, name = record.partition("\t")
        if not separator:
            raise CapturePreconditionError("Git tracked-path inventory is malformed")
        fields = header.split()
        if len(fields) != 3 or fields[2] != "0":
            raise CapturePreconditionError("Git tracked-path inventory contains a non-stage-0 entry")
        try:
            mode = fields[0]
            path = (root / name).absolute()
        except (OSError, ValueError) as exc:
            raise CapturePreconditionError("Git tracked-path inventory contains an unsafe path") from exc
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise CapturePreconditionError("Git tracked-path inventory contains an escaping path")
        paths.append((path, mode, True))
    return paths


def _inventory_path(path: Path, *, label: str, tracked: bool, git_mode: str | None) -> dict[str, object]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise CapturePreconditionError(f"capture input disappeared: {label}") from exc
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        data = target.encode("utf-8", "surrogateescape")
        kind = "symlink"
        symlink_target: str | None = target
    elif stat.S_ISREG(info.st_mode):
        data = path.read_bytes()
        kind = "regular"
        symlink_target = None
    else:
        raise CapturePreconditionError(f"unsupported special capture input: {label}")
    return {
        "path": label,
        "type": kind,
        "tracked": tracked,
        "git_mode": git_mode,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "symlink_target": symlink_target,
    }


def _runtime_inventory(runtime: ImmutableRuntimeWorkspace) -> list[dict[str, object]]:
    runtime.verify()
    return [
        {
            "path": str(row["path"]), "type": "regular", "tracked": True,
            "git_mode": str(row["git_mode"]), "oid": row.get("oid"),
            "bytes": int(row["bytes"]), "sha256": str(row["sha256"]),
            "symlink_target": None,
        }
        for row in runtime.inventory
    ]


def _inventory(
    bundle: Path | "AdmittedBundle", *,
    additional_bundles: tuple[Path | "AdmittedBundle", ...] = (),
    _root: Any = _sealed_repository_root,
) -> list[dict[str, object]]:
    """Fingerprint all tracked checkout inputs plus the complete bundle.

    Invariant: every stage-0 tracked path is represented, and every regular
    or symlink input reachable below the selected bundle is represented except
    session state and exact ``*.okf-lock`` runtime coordination artifacts.  A
    source edit, runtime consumer addition, bundle edit, symlink substitution,
    or special-file substitution therefore changes the digest or fails
    closed before publication.
    """

    root = _root()
    entries: dict[str, tuple[Path, str | None, bool]] = {}
    admitted_labels: set[str] = set()
    for path, mode, tracked in _tracked_paths():
        label = str(path.relative_to(root))
        entries[label] = (path, mode, tracked)

    bundles = (bundle, *additional_bundles)
    for item in bundles:
      bundle_root = _bundle_path(item)
      original_root = item.original_path if isinstance(item, AdmittedBundle) else bundle_root
      admitted_modes = ({str(row["path"]): str(row["git_mode"])
                         for row in item.inventory}
                        if isinstance(item, AdmittedBundle) else {})
      bundle_paths = sorted(bundle_root.rglob("*"))
      for path in bundle_paths:
          try:
              path_info = path.lstat()
          except OSError as exc:
              raise CapturePreconditionError(f"capture input disappeared: {path}") from exc
          if stat.S_ISDIR(path_info.st_mode):
              continue
          if ".okf-loom/session" in path.as_posix():
              continue
          if path.name.endswith(".okf-lock"):
              continue
          relative = path.relative_to(bundle_root)
          label_path = original_root / relative
          label = str(label_path.relative_to(root)) if label_path.is_relative_to(root) else str(label_path)
          if isinstance(item, AdmittedBundle):
              # An admitted bundle always overrides the checkout path: inventory
              # must hash the same immutable HEAD blob consumed by every runtime.
              entries[label] = (path, admitted_modes.get(relative.as_posix()), True)
              admitted_labels.add(label)
          elif label not in entries:
              entries[label] = (path, None, False)

    rows = [
        _inventory_path(path, label=label, tracked=tracked, git_mode=mode)
        for label, (path, mode, tracked) in sorted(entries.items())
    ]
    for row in rows:
        if row["path"] in admitted_labels:
            row["type"] = "regular"
    return rows


def _derive_expected_revision(
    bundle: Path | "AdmittedBundle" | "RuntimeBundle", *,
    runtime: "ImmutableRuntimeWorkspace | None" = None,
    _root: Any = _sealed_repository_root,
) -> str:
    """Derive the revision emitted by live and portable runtime pages."""

    try:
        root = runtime.root if runtime is not None else _root()
        if runtime is not None:
            runtime.audit("derive-bundle-revision", inputs=(_bundle_path(bundle),))
            result = subprocess.run(
                [str(root / "scripts/okf-loom"), "context", str(_bundle_path(bundle)),
                 "bundle revision", "--mode", "lexical", "--format", "json", "--limit", "1"],
                cwd=root, env=runtime.subprocess_env(), text=True, capture_output=True,
            )
            if result.returncode:
                raise RuntimeError(result.stderr[-1000:] or result.stdout[-1000:])
            revision = json.loads(result.stdout).get("bundle_revision")
            if not _nonempty_string(revision):
                raise RuntimeError("immutable runtime emitted no bundle revision")
            return str(revision)
        if str(root / "scripts") not in sys.path:
            sys.path.insert(0, str(root / "scripts"))
        from okf_loom.model import Bundle
        from okf_loom.retrieval import bundle_revision

        revision = bundle_revision(Bundle.load(_bundle_path(bundle)))
    except Exception as exc:
        raise CapturePreconditionError(
            f"could not derive the expected bundle/source revision: {exc}"
        ) from exc
    if not isinstance(revision, str) or not revision:
        raise CapturePreconditionError("bundle/source revision derivation returned no revision")
    return revision


def _assert_input_inventory_stable(
    before: list[dict[str, object]], after: list[dict[str, object]],
) -> None:
    if before != after:
        raise CapturePreconditionError(
            "capture input inventory drifted while browser evidence was running"
        )


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _png(path: Path, target: str) -> dict[str, object]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        raise RuntimeError(f"invalid PNG: {path.name}")
    width, height = struct.unpack(">II", data[16:24])
    return {"file": path.name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "width": width, "height": height, "target": target, "readiness": "capture_target"}


def _artifact_record(
    path: Path,
    spec: ScenarioSpec,
    *,
    provenance: dict[str, Any],
    expected_revision: str | None = None,
    input_inventory: list[dict[str, object]],
    input_inventory_digest: str,
    runtime: dict[str, Any],
    theme_state: dict[str, Any],
    browser: dict[str, Any],
    readiness: str = "capture_target",
) -> dict[str, Any]:
    """Attach immutable release provenance to one already-validated PNG."""

    if not _nonempty_string(expected_revision):
        raise CapturePreconditionError(f"artifact {spec.id} has no expected source revision")
    if not _nonempty_string(provenance.get("commit", provenance.get("head"))) or not _nonempty_string(provenance.get("tree")):
        raise CapturePreconditionError(f"artifact {spec.id} has empty source identity")
    row = _png(path, spec.target)
    width, height = spec.viewport
    runtime_metadata = dict(runtime.get("metadata") or {})
    runtime_metadata["generation"] = runtime.get("generation")
    observed_revision = runtime_metadata.get("revision")
    if observed_revision != expected_revision or runtime_metadata.get("pageRevision") != expected_revision:
        raise CaptureFailure({
            "code": (
                "revision-missing"
                if not observed_revision or not runtime_metadata.get("pageRevision")
                else "revision-mismatch"
            ),
            "phase": "artifact-provenance", "scenario": spec.id,
            "expectedRevision": expected_revision, "observedRevision": observed_revision,
            "message": "artifact provenance cannot bless an unverified source revision",
        })
    family, mode = _public_theme(spec)
    row.update({
        "scenario": spec.id,
        "action": spec.action,
        "source": spec.source,
        "route": spec.route,
        "capture_locator": spec.locator or spec.target,
        "coverage": list(spec.coverage),
        "browser_assertion": spec.assertion,
        "viewport": {"width": width, "height": height},
        "resolved_theme": theme_state.get("resolved_theme") or "unresolved",
        "theme_resolution": theme_state.get("resolution") or "unavailable",
        "modifiers": {
            "contrast": theme_state.get("contrast") or "unresolved",
            "border": theme_state.get("border") or "unresolved",
        },
        "theme_family": family or "unresolved",
        "theme_mode": mode or "unresolved",
        "theme_preference": mode or "unresolved",
        "storage": spec.storage,
        "theme_state": json.loads(json.dumps(theme_state, sort_keys=True)),
        "capture_kind": runtime_metadata.get("captureKind") or "production-ready",
        "failure_provenance": (
            {
                "code": runtime_metadata.get("failureCode"),
                "honestFallback": runtime_metadata.get("honestFallback") is True,
                "readiness": readiness,
            }
            if runtime_metadata.get("failureCode")
            else None
        ),
        "runtime": runtime_metadata,
        "browser": browser,
        **_source_record(provenance),
        "expected_revision": expected_revision,
        "bundle_fixture": spec.bundle,
        "observed_revision": observed_revision,
        "source_revision": expected_revision,
        "bundle_revision": expected_revision,
        "input_inventory": json.loads(json.dumps(input_inventory, sort_keys=True)),
        "input_inventory_digest": input_inventory_digest,
        "readiness": readiness,
    })
    return row


def _page_revision_values(page: Any, response: Any) -> list[str]:
    """Collect all independently exposed runtime revision values.

    A capture is not allowed to trust a value merely because it was copied
    into a manifest.  The response header and server-painted DOM attribute
    are both runtime evidence when present; disagreement is itself a
    fail-closed mismatch.
    """

    values: list[str] = []
    headers = getattr(response, "headers", {}) or {}
    for key in ("x-okf-bundle-revision", "X-OKF-Bundle-Revision"):
        value = headers.get(key)
        if isinstance(value, str) and value:
            values.append(value)
    try:
        body = page.locator("body")
        for attribute in ("data-okf-capture-revision", "data-okf-revision"):
            value = body.get_attribute(attribute)
            if isinstance(value, str) and value:
                values.append(value)
    except Exception:
        # A missing DOM accessor is handled as missing evidence below.  It
        # must not turn a response-only value into an unverified claim.
        pass
    return values


def _assert_page_revision(
    page: Any,
    response: Any,
    expected_revision: str,
    *,
    phase: str,
    target: str,
    url: str,
    failure_code: str | None = None,
) -> str:
    if not isinstance(expected_revision, str) or not expected_revision.strip():
        raise CapturePreconditionError("capture requires an expected source revision")
    values = _page_revision_values(page, response)
    extra = {"expectedRevision": expected_revision}
    if failure_code:
        extra["failureCode"] = failure_code
    if not values:
        raise CaptureFailure({
            "code": "revision-missing", "phase": phase, "target": target,
            "url": url, **extra,
            "message": "capture target did not expose a source revision",
        })
    if len(set(values)) != 1 or values[0] != expected_revision:
        raise CaptureFailure({
            "code": "revision-mismatch", "phase": phase, "target": target,
            "url": url, "observedRevision": values if len(set(values)) != 1 else values[0],
            **extra,
            "message": "capture target source revision mismatched the expected revision",
        })
    return values[0]


def _assert_runtime_page_revision(page: Any, expected_revision: str, *, scenario: str) -> str:
    """Independently observe the current page after a JS capture.

    ``capture_target`` already passes the expected revision to the production
    readiness owner.  This second observation prevents a test double or a
    future consumer adapter from satisfying the release gate with metadata it
    merely recorded rather than a revision actually present in the page.
    """

    try:
        observed = page.evaluate(
            """() => {
              const body = document.body, root = document.documentElement;
              return {
                body: body && (body.dataset.okfCaptureRevision || body.dataset.okfRevision || null),
                root: root && (root.dataset.okfCaptureRevision || root.dataset.okfRevision || null),
              };
            }"""
        )
    except Exception as exc:
        raise CaptureFailure({
            "code": "revision-missing", "phase": "revision", "scenario": scenario,
            "expectedRevision": expected_revision,
            "message": f"could not observe the runtime page revision: {exc}",
        }) from exc
    values = [
        value for value in (observed.values() if isinstance(observed, dict) else [])
        if isinstance(value, str) and value
    ]
    if not values:
        raise CaptureFailure({
            "code": "revision-missing", "phase": "revision", "scenario": scenario,
            "expectedRevision": expected_revision,
            "message": "runtime page did not expose a source revision",
        })
    if len(set(values)) != 1 or values[0] != expected_revision:
        raise CaptureFailure({
            "code": "revision-mismatch", "phase": "revision", "scenario": scenario,
            "expectedRevision": expected_revision,
            "observedRevision": values if len(set(values)) != 1 else values[0],
            "message": "runtime page source revision mismatched the expected revision",
        })
    return values[0]


def _capture_nojs_target(
    page: Any,
    url: str,
    target: str,
    path: Path,
    *,
    expected_revision: str | None = None,
    locator: str | None = None,
    full_page: bool = True,
) -> dict[str, Any]:
    """Capture a no-JS fallback using DOM visibility, never fake JS readiness."""

    started = time.monotonic()
    if threading.current_thread() is not threading.main_thread():
        raise CaptureFailure({
            "code": "thread-ownership", "phase": "ownership", "target": target,
            "url": url, "javascript": False,
        })
    if not isinstance(expected_revision, str) or not expected_revision.strip():
        raise CaptureFailure({
            "code": "revision-missing", "phase": "revision", "target": target,
            "url": url, "javascript": False,
            "message": "no-JS capture requires an expected source revision",
        })
    selector = locator or {
        "document": "body", "search": ".okf-search", "graph-nojs": "body",
    }.get(target, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    claim_key, claim = _claim_capture(path)
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=f".capture-tmp{path.suffix or '.png'}", dir=path.parent,
        )
    except BaseException:
        _release_capture_claim(claim_key, claim)
        raise
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            node = page.locator(selector)
            node.wait_for(state="visible", timeout=30_000)
            if target == "document" or target == "search":
                text = node.inner_text().strip()
                if not text:
                    raise RuntimeError("no-JS target has no readable authored text")
            observed_revision = _assert_page_revision(
                page, response, expected_revision, phase="revision", target=target, url=url,
            )
            if selector == "body":
                page.screenshot(path=str(temporary), type="png", full_page=full_page, timeout=30_000)
            else:
                node.screenshot(path=str(temporary), type="png", timeout=30_000)
        except CaptureFailure:
            raise
        except Exception as exc:
            elapsed = max(0, int((time.monotonic() - started) * 1000))
            raise CaptureFailure({
                "code": "no-js-not-ready", "phase": "no-js-dom", "target": target,
                "url": url, "elapsedMs": elapsed, "javascript": False,
                "message": str(exc), "metadata": {"readinessPhase": "no-js-dom"},
            }) from exc
        if not _capture_claim_is_current(claim_key, claim):
            raise CaptureFailure({
                "code": "superseded", "phase": "publication", "target": target,
                "url": url, "expectedRevision": expected_revision,
                "message": "a newer no-JS capture owns publication",
            })
        os.replace(temporary, path)
        return {
            "generation": None, "screenshotAllowed": True,
            "metadata": {
                "revision": observed_revision,
                "pageRevision": observed_revision,
                "expectedRevision": expected_revision,
                "readinessPhase": "no-js-dom", "javascript": False,
                "url": url, "cssViewport": None,
            },
        }
    finally:
        temporary.unlink(missing_ok=True)
        _release_capture_claim(claim_key, claim)


_FALLBACK_CAPTURE_BUDGET_MS = 30_000
_FALLBACK_FAILURE_CODES = frozenset({"blocked-script", "boot-failure"})
_FALLBACK_SCENARIO_CODES = {"boot-blocked": "blocked-script", "boot-failure": "boot-failure"}


def _capture_fallback_target(
    page: Any,
    url: str,
    target: str,
    path: Path,
    *,
    expected_revision: str,
    failure_code: str,
    full_page: bool = True,
    locator: str = ".okf-studio-fallback-banner",
) -> dict[str, Any]:
    """Capture an explicitly injected Studio failure through the fallback DOM.

    This is deliberately not a second readiness implementation.  Failure
    scenarios are allowed to prove only the honest, visible fallback notice;
    success scenarios continue to use ``capture_target`` and its production
    readiness owner.  The same bounded, temporary-file publication discipline
    applies so a failed fallback capture cannot replace an older artifact.
    """

    started = time.monotonic()
    if threading.current_thread() is not threading.main_thread():
        raise CaptureFailure({
            "code": "thread-ownership", "phase": "ownership", "target": target,
            "url": url, "javascript": True, "failureCode": failure_code,
        })
    if not isinstance(expected_revision, str) or not expected_revision.strip():
        raise CapturePreconditionError("fallback capture requires an expected source revision")
    if failure_code not in _FALLBACK_FAILURE_CODES:
        raise CapturePreconditionError(f"unsupported fallback capture failure code: {failure_code}")

    path.parent.mkdir(parents=True, exist_ok=True)
    claim_key, claim = _claim_capture(path)
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=f".capture-tmp{path.suffix or '.png'}", dir=path.parent,
        )
    except BaseException:
        _release_capture_claim(claim_key, claim)
        raise
    os.close(fd)
    temporary = Path(temporary_name)

    def remaining_ms() -> int:
        elapsed = int((time.monotonic() - started) * 1000)
        return max(0, _FALLBACK_CAPTURE_BUDGET_MS - elapsed)

    def failure(code: str, phase: str, *, message: str, **extra: Any) -> CaptureFailure:
        elapsed = max(0, int((time.monotonic() - started) * 1000))
        return CaptureFailure({
            "code": code, "phase": phase, "target": target, "url": url,
            "elapsedMs": elapsed, "remainingMs": remaining_ms(),
            "javascript": True, "failureCode": failure_code,
            "expectedRevision": expected_revision, "message": message, **extra,
        })

    try:
        budget = remaining_ms()
        if budget <= 0:
            raise failure("timeout", "fallback-navigation", message="fallback capture deadline expired")
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=budget)
            budget = remaining_ms()
            if budget <= 0:
                raise failure("timeout", "fallback-dom", message="fallback capture deadline expired")
            banner = page.locator(locator).first
            banner.wait_for(state="visible", timeout=budget)
            text = banner.inner_text().strip()
            if not text or not any(
                marker in text.lower() for marker in ("unavailable", "failed", "javascript is off", "readable")
            ):
                raise failure(
                    "fallback-missing", "fallback-dom",
                    message="failure scenario did not expose an honest fallback notice",
                    fallbackText=text,
                )
            state = page.evaluate(
                """() => {
                  const root = document.documentElement;
                  const body = document.body;
                  const banners = Array.from(document.querySelectorAll('.okf-studio-fallback-banner'));
                  const visible = node => node.offsetParent !== null &&
                    getComputedStyle(node).display !== 'none';
                  const status = (body && body.dataset.okfStudioStatus) ||
                    root.dataset.okfStudioStatus || null;
                  const code = (body && body.dataset.okfStudioCode) ||
                    root.dataset.okfStudioCode || null;
                  return {
                    success: root.classList.contains('okf-studio-booted') || status === 'ready',
                    status, code, fallbackVisible: banners.some(visible),
                  };
                }"""
            )
            if not isinstance(state, dict) or state.get("success") is True or state.get("fallbackVisible") is not True:
                raise failure(
                    "fallback-not-ready", "fallback-dom",
                    message="failure scenario did not remain in a visible non-success fallback state",
                    studioState=state,
                )
            try:
                observed_revision = _assert_page_revision(
                    page, response, expected_revision, phase="revision", target=target,
                    url=url, failure_code=failure_code,
                )
            except CaptureFailure as exc:
                raise failure(
                    exc.diagnostic["code"], "revision", message=exc.diagnostic["message"],
                    observedRevision=exc.diagnostic.get("observedRevision"),
                ) from exc
            budget = remaining_ms()
            if budget <= 0:
                raise failure("timeout", "fallback-screenshot", message="fallback capture deadline expired")
            page.screenshot(path=str(temporary), type="png", full_page=full_page, timeout=budget)
        except CaptureFailure:
            raise
        except Exception as exc:
            raise failure("fallback-not-ready", "fallback-dom", message=str(exc)) from exc
        if not _capture_claim_is_current(claim_key, claim):
            raise CaptureFailure({
                "code": "superseded", "phase": "publication", "target": target,
                "url": url, "expectedRevision": expected_revision,
                "failureCode": failure_code,
                "message": "a newer fallback capture owns publication",
            })
        os.replace(temporary, path)
        return {
            "generation": None,
            "screenshotAllowed": True,
            "metadata": {
                "revision": expected_revision,
                "pageRevision": observed_revision,
                "expectedRevision": expected_revision,
                "readinessPhase": "fallback-dom",
                "captureKind": "intentional-fallback",
                "failureCode": failure_code,
                "honestFallback": True,
                "url": url,
                "cssViewport": None,
            },
        }
    finally:
        temporary.unlink(missing_ok=True)
        _release_capture_claim(claim_key, claim)


def _prepare_successful_graph_path(page) -> dict[str, object]:
    """Reset graph-owned state and stage a real, typed, visible path.

    The reset control is the public owner for focus, filters, isolation, and
    selection cleanup.  Endpoint selection then uses the same public
    interaction dispatcher as pointer and keyboard input.  The final oracle is
    intentionally checked before capture readiness/screenshot publication: an
    unavailable/filtered/disconnected path must never become visual proof.
    """
    page.locator("#okf-reset").click(timeout=5_000)
    page.wait_for_function(
        "() => { const G=window.__okfLoomGraph,s=G&&G.getState(); "
        "return !!(G&&s&&s.lens!=='focus'&&!G.focusRoot()&&"
        "Object.values(s.hiddenTypes||{}).every(v=>!v)&&"
        "Object.values(s.hiddenRelations||{}).every(v=>!v)&&"
        "window.okfGraphCaptureState().layoutSettled); }"
    )
    endpoints = page.evaluate("""() => {
      const G=window.__okfLoomGraph;
      const edge=G.cy.edges().filter(e => !e.hasClass('dim') &&
        !e.source().hasClass('dim') && !e.target().hasClass('dim'))
        .sort((a,b)=>a.id().localeCompare(b.id())).first();
      if(!edge.length) throw new Error('successful path capture requires a visible connected edge');
      const from=edge.source().id(),to=edge.target().id();
      G.interaction.dispatch({type:'SELECT',id:from,source:'canvas-keyboard'});
      G.interaction.dispatch({type:'PATH',from,id:to,source:'canvas-keyboard'});
      return {from,to};
    }""")
    page.wait_for_function("""([from,to]) => {
      const G=window.__okfLoomGraph,s=G.interaction.snapshot(),p=s.pathResult;
      const panel=document.querySelector('.okf-detail-path');
      return !!(p&&p.kind==='found'&&p.steps&&p.steps.length>=2&&
        p.steps[0].id===from&&p.steps[p.steps.length-1].id===to&&
        p.relations&&p.relations.length===p.steps.length-1&&
        p.relations.every(r=>r.label&&['forward','reverse'].includes(r.orientation))&&
        panel&&panel.offsetParent!==null&&
        panel.querySelectorAll('.okf-detail-path__relation').length===p.relations.length);
    }""", arg=[endpoints["from"], endpoints["to"]])
    oracle = page.evaluate("""() => {
      const p=window.__okfLoomGraph.interaction.snapshot().pathResult;
      const panel=document.querySelector('.okf-detail-path');
      return {kind:p&&p.kind,message:p&&p.message,relations:(p&&p.relations||[]).map(r=>({label:r.label,orientation:r.orientation})),
        panelText:panel&&panel.textContent,visible:!!(panel&&panel.offsetParent!==null)};
    }""")
    text = f"{oracle.get('message') or ''} {oracle.get('panelText') or ''}".lower()
    if (oracle.get("kind") != "found" or not oracle.get("visible") or
            not oracle.get("relations") or "unavailable" in text):
        raise RuntimeError(f"graph path capture oracle rejected non-success state: {oracle}")
    return {**endpoints, **oracle}


def _prepare_graph_keyboard_journey(page: Any) -> dict[str, Any]:
    """Exercise the public graph composite and record synchronized AT state."""

    index = page.locator(".okf-node-index")
    if index.count() and not index.get_attribute("open"):
        index.locator("summary").click(timeout=5_000)
    concepts = page.get_by_role("listbox", name="Graph concepts", exact=False).first
    concepts.wait_for(state="visible", timeout=5_000)
    page.evaluate("""() => {
      window.__okfObservedGraphKeys=[];
      if(!window.__okfObservedPreventDefault){ const nativePrevent=Event.prototype.preventDefault;
        Event.prototype.preventDefault=function(){ if(this.type==='keydown') this.__okfPreventDefaultCalled=true; return nativePrevent.call(this); };
        window.__okfObservedPreventDefault=true;
      }
      document.addEventListener('keydown',event=>{ const a=document.activeElement,row={key:event.key,shiftKey:event.shiftKey,
        trusted:event.isTrusted,defaultPrevented:false,
        targetRole:event.target&&event.target.getAttribute('role'),
        targetName:event.target&&(event.target.getAttribute('aria-label')||event.target.textContent||'').trim(),
        pre:{role:a&&a.getAttribute('role'),name:a&&(a.getAttribute('aria-label')||a.textContent||'').trim(),
          activeDescendant:a&&a.getAttribute('aria-activedescendant'),expanded:a&&a.getAttribute('aria-expanded'),
          selected:a&&a.getAttribute('aria-selected')},
        editable:!!(event.target&&event.target.closest('input,textarea,select,[contenteditable=true]'))};
        window.__okfObservedGraphKeys.push(row); queueMicrotask(()=>{row.defaultPrevented=event.defaultPrevented||event.__okfPreventDefaultCalled===true;});
      },true);
    }""")
    concepts.focus()
    events = []
    synchronized: dict[str, Any] | None = None
    for sequence, key in enumerate(("ArrowDown", "Space", "ArrowDown", "Shift+Space", "Escape"), 1):
        pre = page.evaluate("""() => { const a=document.activeElement; return {role:a&&a.getAttribute('role'),name:a&&(a.getAttribute('aria-label')||a.textContent||'').trim(),activeDescendant:a&&a.getAttribute('aria-activedescendant'),expanded:a&&a.getAttribute('aria-expanded'),selected:a&&a.getAttribute('aria-selected')}; }""")
        observed_before = page.evaluate("() => window.__okfObservedGraphKeys.length")
        page.keyboard.press(key)
        page.wait_for_function("count => window.__okfObservedGraphKeys.length===count+1", arg=observed_before, timeout=5_000)
        post = page.evaluate("""() => { const a=document.activeElement; return {role:a&&a.getAttribute('role'),name:a&&(a.getAttribute('aria-label')||a.textContent||'').trim(),activeDescendant:a&&a.getAttribute('aria-activedescendant'),expanded:a&&a.getAttribute('aria-expanded'),selected:a&&a.getAttribute('aria-selected')}; }""")
        observed = page.evaluate("index => window.__okfObservedGraphKeys[index]", observed_before)
        events.append({"sequence": sequence, "key": key, "pre": pre, "post": post,
                       "ownership": "component" if observed and observed.get("targetRole") == "listbox" else "native",
                       "active_descendant": post.get("activeDescendant"),
                       "aria_state": {"expanded": post.get("expanded"), "selected": post.get("selected")},
                       "native_editable": bool(observed and observed.get("editable")),
                       "default_prevented": bool(observed and observed.get("defaultPrevented")),
                       "restoration": key == "Escape" and post.get("role") == "listbox",
                       "observed": observed})
        if key == "Shift+Space":
            page.wait_for_function(
                "() => { const G=window.__okfLoomGraph,s=G&&G.interaction.snapshot(); return !!(s&&s.pathResult&&s.pathResult.kind==='found'); }",
                timeout=5_000,
            )
            synchronized = page.evaluate("""() => {
      const G=window.__okfLoomGraph,s=G.interaction.snapshot(),active=document.activeElement;
      const row=document.querySelector('.okf-node-index__item[aria-selected=true]');
      const detail=document.querySelector('.okf-detail');
      const p=s.pathResult;
      return {source:'playwright-keyboard',activeRole:active&&active.getAttribute('role'),activeDescendant:active&&active.getAttribute('aria-activedescendant'),
        selectedId:p&&p.steps&&p.steps[0]&&p.steps[0].id,pathKind:p&&p.kind,pathStart:p&&p.steps&&p.steps[0]&&p.steps[0].id,
        pathEnd:p&&p.steps&&p.steps[p.steps.length-1]&&p.steps[p.steps.length-1].id,
        pathStepCount:p&&p.steps&&p.steps.length,listTarget:row&&row.dataset.target,
        detailText:detail&&detail.innerText,layout:window.okfGraphCaptureState()};
    }""")
    evidence = synchronized or {}
    evidence["activeRole"] = events[-1]["post"].get("role")
    evidence["activeDescendant"] = events[-1]["post"].get("activeDescendant")
    evidence["events"] = events
    if (evidence.get("activeRole") != "listbox" or not evidence.get("activeDescendant") or
            not evidence.get("selectedId") or evidence.get("listTarget") != evidence.get("selectedId") or
            evidence.get("pathKind") != "found" or evidence.get("pathStepCount", 0) < 2 or
            evidence.get("pathStart") == evidence.get("pathEnd") or not evidence.get("detailText") or
            any(event["ownership"] != "component" or event["native_editable"] or
                event["observed"].get("trusted") is not True or
                event["default_prevented"] is not True for event in events) or
            events[-1]["key"] != "Escape" or events[-1]["restoration"] is not True or len(events) != 5):
        raise CaptureFailure({"code": "graph-at-desynchronized", "phase": "graph-journey", "evidence": evidence})
    return evidence


def _theme_seed_script(spec: ScenarioSpec) -> str | None:
    if spec.storage != "seeded" or not _has_explicit_theme_preference(spec):
        return None
    family, mode = _scenario_theme_preference(spec)
    if family is None or mode is None:
        return None
    values = {
        "family": family, "mode": mode,
        "contrast": spec.contrast or "normal", "border": spec.border or "on",
    }
    encoded = json.dumps(values, sort_keys=True)
    return (
        "(() => { const p=" + encoded + "; "
        "localStorage.setItem('okf-theme-family',p.family);"
        "localStorage.setItem('okf-theme-mode',p.mode);"
        "localStorage.setItem('okf-theme-contrast',p.contrast);"
        "localStorage.setItem('okf-theme-border',p.border);"
        "localStorage.removeItem('okf-theme'); })();"
    )


_THEME_STORAGE_KEYS = (
    "okf-theme-family", "okf-theme-mode", "okf-theme-contrast", "okf-theme-border", "okf-theme",
)


def _clear_theme_storage_script() -> str:
    keys = json.dumps(_THEME_STORAGE_KEYS)
    return (
        "(() => { const keys=" + keys + "; "
        "try { keys.forEach(k => localStorage.removeItem(k)); } catch (_) {} })();"
    )


def _storage_denied_script() -> str:
    return """(() => {
      const deny = () => { throw new DOMException('capture storage denied', 'SecurityError'); };
      for (const method of ['getItem', 'setItem', 'removeItem', 'clear', 'key']) {
        try { Object.defineProperty(Storage.prototype, method, {value: deny, configurable: true}); }
        catch (_) { try { Storage.prototype[method] = deny; } catch (_) {} }
      }
    })();"""


def _theme_state(page: Any, *, javascript: bool) -> dict[str, Any]:
    if javascript:
        return page.evaluate(
            """() => {
              const root=document.documentElement, t=window.OKFLoomTheme;
              let storageAccess='available', storageKeys=[];
              try {
                storageKeys=Array.from({length:localStorage.length},(_,i)=>localStorage.key(i)).filter(Boolean).sort();
                localStorage.getItem('__okf_capture_storage_probe__');
              } catch (_) { storageAccess='denied'; storageKeys=null; }
              return {
                  resolved_theme: root.getAttribute('data-theme'),
                  runtime_resolved_theme: t && typeof t.getResolvedTheme === 'function' ? t.getResolvedTheme() : null,
                  resolved_mode: t && typeof t.getResolvedMode === 'function' ? t.getResolvedMode() : null,
                  contrast: root.getAttribute('data-okf-contrast'),
                border: root.getAttribute('data-okf-border'),
                family: t && typeof t.getFamily === 'function' ? t.getFamily() : null,
                  mode: t && typeof t.getMode === 'function' ? t.getMode() : null,
                  ready: root.classList.contains('okf-theme-ready'),
                  resolution: t ? 'resolved' : 'unavailable',
                  storageAccess, storageKeys,
                };
            }"""
        )
    root = page.locator("html")
    return {
        "resolved_theme": root.get_attribute("data-theme"),
        "runtime_resolved_theme": None,
        "resolved_mode": None,
        "contrast": root.get_attribute("data-okf-contrast"),
        "border": root.get_attribute("data-okf-border"),
        "family": None, "mode": None, "ready": False,
        "storageAccess": None, "storageKeys": None,
        "resolution": "server-painted-hint",
    }


def _assert_theme_state(page: Any, spec: ScenarioSpec) -> dict[str, Any]:
    state = _theme_state(page, javascript=spec.javascript)
    expected = spec.resolved_theme
    if expected and state.get("resolved_theme") != expected:
        raise CaptureFailure({
            "code": "theme-mismatch", "phase": "theme-assertion", "scenario": spec.id,
            "expected": expected, "actual": state.get("resolved_theme"),
        })
    if spec.javascript:
        if state.get("ready") is not True or state.get("resolution") != "resolved":
            raise CaptureFailure({
                "code": "theme-not-ready", "phase": "theme-assertion", "scenario": spec.id,
                "expected": {"ready": True, "resolution": "resolved"},
                "actual": {"ready": state.get("ready"), "resolution": state.get("resolution")},
            })
        if expected and (
            state.get("runtime_resolved_theme") != expected
            or state.get("resolved_mode") != expected.rsplit("-", 1)[1]
        ):
            raise CaptureFailure({
                "code": "theme-resolution-mismatch", "phase": "theme-assertion", "scenario": spec.id,
                "expected": {"theme": expected, "mode": expected.rsplit("-", 1)[1]},
                "actual": {
                    "theme": state.get("runtime_resolved_theme"),
                    "mode": state.get("resolved_mode"),
                },
            })
        expected_family, expected_mode = _scenario_theme_preference(spec)
        if state.get("family") != expected_family or state.get("mode") != expected_mode:
            raise CaptureFailure({
                "code": "theme-preference-mismatch", "phase": "theme-assertion",
                "scenario": spec.id,
                "expected": {"family": expected_family, "mode": expected_mode},
                "actual": {"family": state.get("family"), "mode": state.get("mode")},
            })
        if (
            (spec.contrast is not None and state.get("contrast") != spec.contrast)
            or (spec.border is not None and state.get("border") != spec.border)
        ):
            raise CaptureFailure({
                "code": "theme-modifier-mismatch", "phase": "theme-assertion",
                "scenario": spec.id,
                "expected": {"contrast": spec.contrast, "border": spec.border},
                "actual": {"contrast": state.get("contrast"), "border": state.get("border")},
            })
        if spec.storage == "denied" and state.get("storageAccess") != "denied":
            raise CaptureFailure({
                "code": "storage-not-denied", "phase": "theme-assertion", "scenario": spec.id,
                "message": "storage-denied scenario did not observe denied browser storage",
                "actual": state.get("storageAccess"),
            })
        if spec.storage == "none" and state.get("storageAccess") == "available":
            keys = state.get("storageKeys") or []
            if any(key in _THEME_STORAGE_KEYS for key in keys):
                raise CaptureFailure({
                    "code": "saved-theme-storage-present", "phase": "theme-assertion",
                    "scenario": spec.id, "message": "default Auto scenario had saved theme storage",
                    "actual": keys,
                })
    return state


def _wait_for_graph(page: Any) -> None:
    page.wait_for_function(
        """() => {
          const s=window.okfGraphCaptureState&&window.okfGraphCaptureState();
          return !!(s&&s.initialized&&s.layoutSettled&&!s.transitionPending&&
            s.layoutGeneration===s.appliedLayoutGeneration);
        }""",
        timeout=30_000,
    )


def _prepare_studio_panel(page: Any, *, mobile: bool) -> None:
    page.get_by_role("button", name="Comments", exact=True).click(timeout=5_000)
    page.wait_for_function(
        """(mobile) => {
          const p=document.querySelector('.okf-panel');
          if(!p||p.hidden) return false;
          if(mobile) return p.getAttribute('role')==='dialog'&&p.getAttribute('aria-modal')==='true';
          return p.getAttribute('role')==='complementary'&&!p.hasAttribute('aria-modal');
        }""",
        arg=mobile,
        timeout=5_000,
    )


def _prepare_studio_retry_stale_fence(page: Any) -> dict[str, Any]:
    """Project Retry while a real stale load is held, then prove its fence."""

    page.get_by_role("button", name="Comments", exact=False).first.click(timeout=5_000)
    page.evaluate("""async () => { const S=window.okfLoomStudio,native=window.fetch.bind(window);
      const canonicalComments=await native('/__comments').then(async response=>({
        status:response.status,body:await response.text(),contentType:response.headers.get('content-type')||'application/json'}));
      let release; const gate=new Promise(resolve=>{release=resolve}); let held=false;
      window.__okfStudioRetryLifecycle={held:false,released:false,fetchContinuationCompleted:false,loadCompleted:false};
      window.fetch=(url,options)=>{
        if(!held&&String(url).startsWith('/__comments')){
          held=true; window.__okfStudioRetryLifecycle.held=true;
          return gate.then(()=>new Response(canonicalComments.body,{status:canonicalComments.status,
            headers:{'Content-Type':canonicalComments.contentType}})).then(response=>{
            window.__okfStudioRetryLifecycle.fetchContinuationCompleted=true; return response;
          });
        }
        return native(url,options);
      };
      window.__okfStudioRetryRelease=()=>{window.__okfStudioRetryLifecycle.released=true;release();};
      window.__okfStudioRetryLoad=S._loadComments().finally(()=>{
        window.__okfStudioRetryLifecycle.loadCompleted=true;
      });
    }""")
    page.wait_for_function("() => window.__okfStudioRetryLifecycle?.held===true", timeout=5_000)
    # Production has no timeout/error state while this request is still held.
    # Project the independent visible error honestly; Retry itself remains the
    # production control and starts a newer loadComments generation.
    page.evaluate("""() => { const S=window.okfLoomStudio;
      S.state.commentsReady=false; S.state.commentsStatus='error'; S._renderComments(); }""")
    retry = page.get_by_role("button", name="Retry loading comments", exact=True)
    retry.wait_for(state="visible", timeout=5_000)
    retry.focus()
    retry.click(timeout=5_000)
    page.wait_for_function("""() => { const S=window.okfLoomStudio;
      return !!(S&&S.state.commentsReady&&S.state.eventsReady&&S.state.doc&&
        S.state.doc.concept_content_rev&&!document.querySelector('[role=alert] .okf-studiobtn')); }""", timeout=5_000)
    before = page.evaluate("""() => { const S=window.okfLoomStudio,a=document.activeElement;
      return {commentsStatus:S.state.commentsStatus,commentsReady:S.state.commentsReady,
        eventsReady:S.state.eventsReady,conceptContentRev:S.state.doc.concept_content_rev,
        commentIds:S.state.comments.map(c=>c.id),eventIds:S.state.events.map(e=>e.event_id||e.id),
        focusedRole:a&&a.getAttribute('role'),focusedName:a&&(a.getAttribute('aria-label')||a.textContent||'').trim(),
        panelText:document.querySelector('.okf-panel')&&document.querySelector('.okf-panel').innerText,
        alertAbsent:!document.querySelector('[role=alert] .okf-studiobtn')}; }""")
    page.evaluate("value => { window.__okfStudioRetryCanonicalEvidence=value; }", before)
    page.evaluate("() => window.__okfStudioRetryRelease()")
    page.wait_for_function("""() => { const L=window.__okfStudioRetryLifecycle;
      return !!(L&&L.released&&L.fetchContinuationCompleted&&L.loadCompleted); }""", timeout=5_000)
    after = page.evaluate("""() => { const S=window.okfLoomStudio,a=document.activeElement;
      return {state:{commentsStatus:S.state.commentsStatus,commentsReady:S.state.commentsReady,
        eventsReady:S.state.eventsReady,conceptContentRev:S.state.doc.concept_content_rev,
        commentIds:S.state.comments.map(c=>c.id),eventIds:S.state.events.map(e=>e.event_id||e.id),
        focusedRole:a&&a.getAttribute('role'),focusedName:a&&(a.getAttribute('aria-label')||a.textContent||'').trim(),
        panelText:document.querySelector('.okf-panel')&&document.querySelector('.okf-panel').innerText,
        alertAbsent:!document.querySelector('[role=alert] .okf-studiobtn')},
        persisted:window.__okfStudioRetryCanonicalEvidence,
        lifecycle:window.__okfStudioRetryLifecycle}; }""")
    if before != after.get("state") or before != after.get("persisted"):
        raise CaptureFailure({"code":"studio-retry-stale-overwrite","phase":"studio",
                              "before":before,"after":after})
    evidence = {"errorObserved": True, "errorProjection":"manual-independent-while-request-held",
                "retryActivated": True, "recovered": True, "staleFirstReleased": True,
                "staleFetchContinuationCompleted": after["lifecycle"]["fetchContinuationCompleted"],
                "staleLoadCompleted": after["lifecycle"]["loadCompleted"],
                "staleFenced": True, **after["state"]}
    page.evaluate("e => { window.__okfStudioRetryEvidence=e; }", evidence)
    return evidence


def _prepare_scenario(
    page: Any, spec: ScenarioSpec, *, runtime: "ImmutableRuntimeWorkspace | None" = None,
) -> None:
    family, mode = _scenario_theme_preference(spec)
    if (
        spec.javascript and spec.storage == "seeded"
        and _has_explicit_theme_preference(spec) and family and mode
    ):
        page.evaluate(
            """([family,mode,contrast,border]) => {
              const t=window.OKFLoomTheme;
              if(!t) throw new Error('theme owner unavailable');
              t.setFamily(family); t.setMode(mode); t.setContrast(contrast); t.setBorder(border);
            }""",
            [
                family, mode,
                spec.contrast, spec.border,
            ],
        )
        page.wait_for_function(
            """([theme,contrast,border]) => document.documentElement.getAttribute('data-theme')===theme &&
              document.documentElement.getAttribute('data-okf-contrast')===contrast &&
              document.documentElement.getAttribute('data-okf-border')===border""",
            arg=[spec.resolved_theme, spec.contrast, spec.border],
            timeout=5_000,
        )

    action = spec.action
    if action in {"none", "theme", "parity"}:
        if action == "parity":
            page.wait_for_function("() => document.body && document.body.innerText.trim().length > 0", timeout=5_000)
    elif action == "appearance-modifiers":
        page.locator("#okf-theme").click(timeout=5_000)
        page.locator('input[data-okf-set="contrast"][data-okf-val="soft"]').check(force=True)
        page.locator('input[data-okf-set="border"][data-okf-val="off"]').check(force=True)
        page.wait_for_function(
            "() => document.documentElement.dataset.okfContrast==='soft' && document.documentElement.dataset.okfBorder==='off'",
            timeout=5_000,
        )
    elif action == "appearance-overlay":
        page.locator("#okf-theme").click(timeout=5_000)
        page.wait_for_function(
            "() => { const m=document.querySelector('#okf-appearance-menu'); return !!(m&&!m.hidden&&document.querySelector('#okf-theme[aria-expanded=true]')); }",
            timeout=5_000,
        )
    elif action == "studio-rail":
        page.wait_for_function("() => document.documentElement.classList.contains('okf-studio-booted')", timeout=5_000)
        _prepare_studio_panel(page, mobile=False)
    elif action == "studio-mobile":
        _prepare_studio_panel(page, mobile=True)
    elif action == "studio-split":
        page.locator('.okf-viewswitch__btn[data-mode="split"]').click(timeout=5_000)
        page.wait_for_function(
            "() => { const v=document.querySelector('[data-okf-view=split]'); const b=document.querySelector('.okf-viewswitch__btn[data-mode=split]'); return !!(v&&b&&b.getAttribute('aria-pressed')==='true'); }",
            timeout=5_000,
        )
    elif action in {"concept-rendered", "concept-source"}:
        mode = action.rsplit("-", 1)[-1]
        page.locator(f'.okf-viewswitch__btn[data-mode="{mode}"]').click(timeout=5_000)
        page.wait_for_function(
            "mode => { const b=document.querySelector('.okf-viewswitch__btn[data-mode='+mode+']'); return !!(b&&b.getAttribute('aria-pressed')==='true'); }",
            arg=mode, timeout=5_000,
        )
    elif action == "concept-focus-view":
        page.get_by_role("button", name="Focus view", exact=True).click(timeout=5_000)
        page.wait_for_function("() => document.documentElement.classList.contains('okf-focus') || document.body.classList.contains('okf-focus')", timeout=5_000)
    elif action == "concept-outline":
        page.get_by_role("button", name="On this page", exact=False).first.click(timeout=5_000)
        page.wait_for_function("() => !!document.querySelector('.okf-outline:not([hidden]),.okf-document-outline:not([hidden])')", timeout=5_000)
    elif action in {"index-empty", "index-filter-mismatch"}:
        if action == "index-filter-mismatch":
            page.locator('[data-okf-index-filter], .okf-index-filter input').first.fill("no-such-capture-type")
        page.wait_for_function("() => document.body && document.body.innerText.trim().length > 0", timeout=5_000)
    elif action == "graph-preview-name":
        page.wait_for_function("() => { const a=document.querySelector('.okf-graph-thumb__link'); return !!(a&&(a.getAttribute('aria-label')||a.textContent).trim()); }", timeout=5_000)
    elif action in {"graph-map", "graph-lenses", "graph-focus", "graph-path", "graph-mobile", "graph-keyboard-journey"}:
        _wait_for_graph(page)
        if action == "graph-lenses":
            page.evaluate("document.querySelector('details.okf-signal__advanced').open=true")
            page.wait_for_function("() => document.querySelector('details.okf-signal__advanced[open]')", timeout=5_000)
        elif action == "graph-focus":
            page.evaluate("() => { window.__okfLoomGraph.cy.nodes().first().trigger('tap'); return undefined; }")
            page.get_by_role("toolbar", name="Selected concept actions").get_by_role("button", name="Explore neighborhood").click(timeout=5_000)
            page.wait_for_function(
                "() => { const s=window.__okfLoomGraph.interaction.snapshot(); return s.lens==='focus' || !!window.__okfLoomGraph.focusRoot(); }",
                timeout=5_000,
            )
        elif action == "graph-path":
            _prepare_successful_graph_path(page)
        elif action == "graph-mobile":
            page.get_by_role("navigation", name="Graph panes").get_by_role("button", name="Graph").click(timeout=5_000)
            page.wait_for_function("() => { const n=document.querySelector('#okf-graph'); return !!(n&&n.offsetParent!==null); }", timeout=5_000)
        elif action == "graph-keyboard-journey":
            journey = _prepare_graph_keyboard_journey(page)
            page.evaluate("evidence => { window.__okfGraphATJourney = evidence; }", journey)
    elif action == "search-result":
        page.wait_for_function("() => document.querySelectorAll('.okf-search-result').length > 0", timeout=5_000)
    elif action == "search-no-result":
        page.wait_for_function(
            "() => !!document.querySelector('.okf-search-empty') || /no results|no evidence/i.test(document.body.innerText)",
            timeout=5_000,
        )
    elif action == "search-error":
        page.wait_for_function(
            "() => { const a=document.querySelector('[role=alert]'); return !!(a&&/search|parameter|unsupported/i.test(a.textContent)); }",
            timeout=5_000,
        )
    elif action == "search-manual-copy":
        page.wait_for_function("() => document.querySelectorAll('.okf-search-result').length > 0", timeout=5_000)
        page.evaluate(
            """() => Object.defineProperty(navigator,'clipboard',{
              configurable:true, value:{writeText:() => Promise.reject(new Error('capture clipboard denial'))}
            })"""
        )
        page.locator("button:visible").filter(has_text="Copy").first.click(timeout=5_000)
        page.wait_for_function(
            "() => { const n=document.querySelector('#okf-copy-fallback'); return !!(n&&!n.hidden&&/manual copy|exact/i.test(n.textContent)); }",
            timeout=5_000,
        )
    elif action == "search-expanded":
        page.wait_for_function("() => document.querySelectorAll('.okf-search-result').length > 0", timeout=5_000)
        disclosure = page.locator('.okf-search-result details, .okf-search-result [aria-expanded="false"]').first
        disclosure.click(timeout=5_000)
        page.wait_for_function("() => !!document.querySelector('.okf-search-result details[open],.okf-search-result [aria-expanded=true]')", timeout=5_000)
        page.evaluate("""() => Object.defineProperty(navigator,'clipboard',{configurable:true,
          value:{writeText:() => Promise.reject(new Error('capture Search clipboard denial'))}})""")
        copy = page.locator('.okf-search-result [data-copy-passage]').first
        copy.click(timeout=5_000)
        page.wait_for_function("() => { const n=document.querySelector('#okf-copy-fallback'); return !!(n&&!n.hidden&&document.querySelector('#okf-copy-fallback-text').value.trim()); }", timeout=5_000)
        page.evaluate("""() => { const n=document.querySelector('#okf-copy-fallback'),a=document.querySelector('#okf-copy-fallback-text');
          window.__okfSearchCopyEvidence={trigger:'search-copy-passage',fallbackVisible:!!(n&&!n.hidden),
            exactMarkdown:a&&a.value,source:n&&n.dataset.sourceTitle,citation:n&&n.dataset.sourceCitation}; }""")
    elif action in {"studio-authority", "studio-error-retry"}:
        page.get_by_role("button", name="Studio", exact=False).first.click(timeout=5_000)
        if action == "studio-error-retry":
            _prepare_studio_retry_stale_fence(page)
        else:
            expected = "local-read" if spec.server_mode == "read-only" else "local-edit"
            evidence = page.evaluate("""expected => { const text=document.body.innerText; const boot=JSON.parse(document.getElementById('okf-studio-bootstrap').textContent); const mutation=Array.from(document.querySelectorAll('textarea,[contenteditable=true],button[data-action],button[type=submit]')).filter(n=>/send|post|resolve|re-?anchor|apply|accept|reject|add section|split document|add links|enrich content/i.test((n.dataset.action||n.textContent||n.getAttribute('aria-label')||''))).length; const proposal=document.querySelector('[data-proposal-state]'); return {authority:boot.authority_mode,edit:boot.edit===true,tokenExposed:!!boot.token,mutationControls:mutation,proposalState:proposal&&proposal.dataset.proposalState,proposalText:proposal&&proposal.textContent,text}; }""", expected)
            if evidence.get("authority") != expected:
                raise CaptureFailure({"code":"studio-authority-mismatch","phase":"studio","evidence":evidence})
            if spec.server_mode == "read-only" and (evidence.get("mutationControls") != 0 or evidence.get("tokenExposed") or evidence.get("edit")):
                raise CaptureFailure({"code":"studio-read-only-controls","phase":"studio","evidence":evidence})
            if spec.server_mode == "proposal-enabled" and (not evidence.get("proposalText") or not evidence.get("edit") or not evidence.get("tokenExposed")):
                raise CaptureFailure({"code":"studio-proposal-gating","phase":"studio","evidence":evidence})
            page.evaluate("e => { window.__okfStudioAuthorityEvidence=e; }", evidence)
    elif action == "boot-success":
        page.wait_for_function(
            "() => document.documentElement.classList.contains('okf-studio-booted') && Array.from(document.querySelectorAll('.okf-studio-fallback-banner')).every(n => n.offsetParent===null || getComputedStyle(n).display==='none')",
            timeout=5_000,
        )
    elif action in {"boot-blocked", "boot-failure"}:
        page.wait_for_function(
            "() => !document.documentElement.classList.contains('okf-studio-booted') && Array.from(document.querySelectorAll('.okf-studio-fallback-banner')).some(n => n.offsetParent!==null)",
            timeout=5_000,
        )
    elif action in {"zoom-200", "zoom-400"}:
        page.evaluate(
            "evidence => { window.__okfZoomEvidence = evidence; }",
            _apply_browser_zoom(page, spec.zoom_percent or 100, runtime=runtime),
        )
    else:
        raise CapturePreconditionError(f"unimplemented release scenario action: {action}")

    if spec.forced_colors or spec.reduced_motion:
        page.wait_for_function(
            "([forced,reduced]) => (!forced || matchMedia('(forced-colors: active)').matches) && (!reduced || matchMedia('(prefers-reduced-motion: reduce)').matches)",
            arg=[spec.forced_colors, spec.reduced_motion], timeout=5_000,
        )
    criteria = sorted(_ACCEPTANCE_CRITERIA.intersection(spec.coverage))
    if criteria:
        oracles = page.evaluate(r"""criteria => {
          const q=s=>document.querySelector(s), qa=s=>Array.from(document.querySelectorAll(s));
          const visible=n=>!!(n&&n.getClientRects().length), text=n=>(n&&n.textContent||'').replace(/\s+/g,' ').trim();
          const links=qa('header a,header button,.okf-shell a,.okf-shell button').filter(visible);
          const graph=window.__okfLoomGraph, gs=graph&&graph.interaction&&graph.interaction.snapshot();
          const outline=q('[data-okf-document-outline]'), result=q('.okf-search-result');
          const root=getComputedStyle(document.documentElement), controls=qa('button,a,input,summary').filter(visible);
          const names=controls.map(n=>text(n)||n.getAttribute('aria-label')||n.getAttribute('title')||'');
          const rect=n=>{const r=n.getBoundingClientRect();return {width:r.width,height:r.height}};
          const all={
            F01:(()=>{const order=links.map(text).filter(v=>/^(Index|Search|Graph|Studio)/i.test(v));const current=q('[aria-current=page]');return {pass:order.slice(0,4).every((v,i)=>['Index','Search','Graph','Studio'][i]===v.split(/\s/)[0])&&/^Index\b/i.test(text(current))&&current.getAttribute('href')==='/',order,current:text(current),currentHref:current&&current.getAttribute('href')};})(),
            F02:{pass:!!q('.okf-index-empty,.okf-empty-state,[data-okf-index-empty]')||/no concepts|no matching|clear filter/i.test(document.body.innerText),emptyText:text(q('.okf-index-empty,.okf-empty-state,[data-okf-index-empty]'))},
            F03:{pass:!!outline&&qa('[data-okf-document-outline] a[href^="#"]').length>1,sections:qa('[data-okf-document-outline] a[href^="#"]').length},
            F04:{pass:!!(window.__okfZoomEvidence&&window.__okfZoomEvidence.journey&&window.__okfZoomEvidence.journey.restored),journey:window.__okfZoomEvidence&&window.__okfZoomEvidence.journey},
            F05:(()=>{const a=q('.okf-graph-thumb__link'),svg=a&&a.querySelector('svg');const name=a&&(a.getAttribute('aria-label')||[text(a.querySelector('.sr-only')),text(a.querySelector('figcaption'))].filter(Boolean).join(' '));return {pass:!!(a&&name&&name.length<160&&svg&&svg.getAttribute('aria-hidden')==='true'&&svg.getAttribute('focusable')==='false'),name,svgHidden:svg&&svg.getAttribute('aria-hidden'),svgFocusable:svg&&svg.getAttribute('focusable')};})(),
            F06:{pass:!!(window.__okfGraphATJourney&&window.__okfGraphATJourney.activeRole==='listbox'&&window.__okfGraphATJourney.activeDescendant),journey:window.__okfGraphATJourney},
            F07:(()=>{const state=window.okfGraphCaptureState&&window.okfGraphCaptureState(),nodes=graph&&graph.cy&&graph.cy.nodes();const labels=nodes&&nodes.map(n=>n.data('label'));return {pass:!!(state&&state.ready&&nodes&&nodes.length>=24&&labels.every(Boolean)),layout:state,nodeCount:nodes&&nodes.length,labelCount:labels&&labels.length};})(),
            F08:{pass:!visible(q('[role=dialog][aria-label*="tour" i],.okf-graph-tour:not([hidden])'))&&qa('button,a').some(n=>/tour/i.test(text(n))),tourObscures:visible(q('[role=dialog][aria-label*="tour" i],.okf-graph-tour:not([hidden])')),tourLaunch:qa('button,a').some(n=>/tour/i.test(text(n)))},
            F09:{pass:!!(result&&result.querySelector('details[open],[aria-expanded=true]')),expanded:!!(result&&result.querySelector('details[open],[aria-expanded=true]')),queryRetained:new URL(location.href).searchParams.get('q')==='tunnel',scrollY},
            F10:(()=>{const required=['best overall','exact wording','similar meaning','tagged with','mentions entity','connected by relation'],body=document.body.innerText.toLowerCase();return {pass:required.every(v=>body.includes(v)),required,matched:required.filter(v=>body.includes(v))};})(),
            F11:(()=>{const e=window.__okfSearchCopyEvidence;return {pass:!!(e&&e.trigger==='search-copy-passage'&&e.fallbackVisible&&e.exactMarkdown&&e.citation),copyLabels:names.filter(v=>/copy|context pack/i.test(v)),evidence:e||null};})(),
            F12:(()=>{const labels=controls.map(text).filter(v=>/focus|neighborhood/i.test(v));return {pass:!qa('button,a').some(n=>text(n)==='Focus')&&labels.length>0,focusAmbiguous:qa('button,a').some(n=>text(n)==='Focus'),taskLabel:labels.length>0,labels};})(),
            F13:(()=>{const idle=qa('header button,.okf-shell button').filter(visible).filter(n=>/^0 (comments|changes)$/i.test(text(n)));return {pass:links.some(n=>/studio/i.test(text(n)))&&idle.length===0,studio:text(links.find(n=>/studio/i.test(text(n)))),idleCounters:idle.length};})(),
            F14:(()=>{const r=window.__okfStudioRetryEvidence;const state=text(q('.okf-empty-state,[role=alert],.okf-studio-home__intro'));const canonicalIdle=/select text to comment|start watching|agent-loop|no pending work|could not be loaded/i.test(document.body.innerText);return {pass:!!(r&&r.errorObserved&&r.retryActivated&&r.recovered&&r.commentsReady&&r.eventsReady&&r.conceptContentRev&&r.alertAbsent)||canonicalIdle,state,retry:r||null,canonicalIdle};})(),
            F15:(()=>{const proposalCopy=/ask the agent to improve this concept/i.test(document.body.innerText)&&/does not edit immediately|nothing edits markdown immediately/i.test(document.body.innerText);return {pass:proposalCopy,proposalCopy,proposalState:q('[data-proposal-state]')&&q('[data-proposal-state]').dataset.proposalState};})(),
            F16:(()=>{const findControl=!!q('[aria-label*="Find in graph" i],input[placeholder*="graph" i]'),settingsControl=!!q('[aria-label*="Graph settings" i],details.okf-signal__advanced');return {pass:findControl&&settingsControl,findControl,settingsControl,controls:controls.map(text).filter(v=>/graph|zoom|fit|lens|reset/i.test(v)).slice(0,20)};})(),
            F17:(()=>{const c=q('#okf-graph'),style=c&&getComputedStyle(c);return {pass:!!(graph&&graph.cy&&c&&style.touchAction!=='auto'&&window.__okfGraphATJourney),touchAction:style&&style.touchAction,libraryRuntime:graph&&graph.cy&&graph.cy.version&&graph.cy.version(),journey:!!window.__okfGraphATJourney};})(),
            F18:{pass:!!outline&&/navigation/i.test(outline.getAttribute('aria-label')||outline.querySelector('nav')&&outline.querySelector('nav').getAttribute('aria-label')||''),landmark:outline&&outline.getAttribute('aria-label')},
            F19:{pass:/use from a clone|open the live studio|find a command|understand the format/i.test(document.body.innerText),startHere:text(q('.okf-start-here,[aria-label*="start" i]'))},
            F20:(()=>{const marks=qa('[data-type],[data-relation],.okf-node-index__item').filter(visible),nonColor=marks.filter(n=>text(n)||n.getAttribute('aria-label')||n.getAttribute('data-type')||n.getAttribute('data-relation'));return {pass:matchMedia('(forced-colors: active)').matches&&marks.length>0&&nonColor.length===marks.length,forcedColors:matchMedia('(forced-colors: active)').matches,marks:marks.length,nonColor:nonColor.length};})(),
            F21:{pass:qa('h1').length===1&&qa('h1,h2').every(n=>text(n).length>0),h1Count:qa('h1').length,headings:qa('h1,h2').map(text).slice(0,12)},
            F22:(()=>{const unnamed=controls.filter(n=>!names[controls.indexOf(n)]),targets=controls.map(rect),undersized=targets.filter(r=>r.width<24||r.height<24);return {pass:unnamed.length===0&&undersized.length===0,unnamed:unnamed.length,minimumTarget:targets.reduce((m,r)=>Math.min(m,r.width,r.height),Infinity),undersized:undersized.length};})(),
            F23:(()=>{const sample=q('.okf-search-result'),r=sample&&sample.getBoundingClientRect();return {pass:document.documentElement.dataset.okfDensity==='compact'&&parseFloat(root.getPropertyValue('--okf-density-scale')||'1')<1&&r&&r.height>0,density:document.documentElement.dataset.okfDensity,scale:root.getPropertyValue('--okf-density-scale'),consumedHeight:r&&r.height};})(),
            F24:(()=>{let stored=null;try{stored={family:localStorage.getItem('okf-theme-family'),mode:localStorage.getItem('okf-theme-mode')}}catch(_){ }const T=window.OKFLoomTheme;return {pass:document.documentElement.dataset.theme==='coffee-light'&&stored&&stored.family==='coffee'&&stored.mode==='light'&&T&&T.getFamily()==='coffee'&&T.getMode()==='light',showcase:document.documentElement.dataset.theme,stored,runtimeFamily:T&&T.getFamily(),runtimeMode:T&&T.getMode(),technicalDefaultContract:'technical/auto covered by technical-auto scenarios'};})()
          };
          return Object.fromEntries(criteria.map(k=>[k,all[k]]));
        }""", criteria)
        failed = [criterion for criterion, observation in oracles.items()
                  if not isinstance(observation, dict)
                  or not ACCEPTANCE_ORACLE_REGISTRY[criterion](observation)]
        if failed:
            raise CaptureFailure({"code":"acceptance-oracle-failed","phase":"scenario-acceptance",
                                  "scenario":spec.id,"criteria":failed,"oracles":oracles})
        page.evaluate("value => { window.__okfAcceptanceOracles=value; }", oracles)


def _apply_browser_zoom_impl(
    page: Any, percent: int, *, runtime: "ImmutableRuntimeWorkspace | None",
) -> dict[str, Any]:
    """Apply Chrome's user-facing page zoom through an external chrome driver.

    Playwright keyboard input targets page content and cannot own browser chrome.
    CDP page-scale and narrow viewports are forbidden proxies. The explicit
    environment-gated driver must operate the headed browser's chrome; runtime
    viewport and DPR then independently prove that the requested zoom occurred.
    """

    if os.environ.get("OKF_CAPTURE_REAL_BROWSER_ZOOM") != "1":
        raise CaptureFailure({"code": "zoom-unsupported", "phase": "browser-owned-zoom",
                              "zoomPercent": percent, "message": "headed Chrome integration is not enabled"})
    if percent not in {200, 400}:
        raise CapturePreconditionError(f"unsupported canonical page zoom: {percent}")
    driver_value = os.environ.get("OKF_CAPTURE_BROWSER_ZOOM_DRIVER")
    if not driver_value:
        raise CaptureFailure({"code": "zoom-unsupported", "phase": "browser-owned-zoom",
                              "zoomPercent": percent,
                              "message": "headed Chrome is enabled but no browser-chrome zoom driver is configured"})
    if runtime is None:
        # Compatibility/test calls may diagnose the feature gate but cannot
        # execute a dynamically selected checkout-local driver.
        raise CaptureFailure({"code": "zoom-unsupported", "phase": "browser-owned-zoom",
                              "zoomPercent": percent,
                              "message": "browser-chrome zoom requires immutable runtime authority"})
    driver = Path(driver_value)
    if not driver.is_absolute():
        driver = runtime.root / driver
    try:
        runtime.audit("browser-zoom", executable=driver, cwd=runtime.root)
    except CapturePreconditionError as exc:
        raise CaptureFailure({"code": "zoom-unsupported", "phase": "browser-owned-zoom",
                              "zoomPercent": percent,
                              "message": "browser-chrome zoom driver is outside immutable runtime"}) from exc
    binding = "okf-capture-zoom-" + secrets.token_hex(12)
    page.evaluate("binding => { document.title=binding; document.documentElement.dataset.okfZoomBinding=binding; }", binding)
    nominal = page.viewport_size
    nominal_width = int((nominal or {}).get("width") or 0)
    if nominal_width <= 0:
        raise CaptureFailure({"code": "zoom-unsupported", "phase": "browser-owned-zoom",
                              "zoomPercent": percent, "message": "headed page has no nominal viewport binding"})
    def invoke(target: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(driver), "--session-title", binding, "--percent", str(target)], cwd=runtime.root,
            env=runtime.subprocess_env(), text=True, capture_output=True, timeout=10, check=False,
        )
    try:
        baseline_result = invoke(100)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptureFailure({"code": "zoom-unsupported", "phase": "browser-owned-zoom",
                              "zoomPercent": percent, "message": f"browser-chrome zoom driver unavailable: {exc}"}) from exc
    if baseline_result.returncode:
        raise CaptureFailure({"code": "zoom-unsupported", "phase": "browser-owned-zoom",
                              "zoomPercent": percent, "driverReturncode": baseline_result.returncode,
                              "message": "browser-chrome zoom baseline reset failed", "stderr": baseline_result.stderr[-1000:]})
    try:
        page.wait_for_function("expected => innerWidth===expected", arg=nominal_width, timeout=5_000)
    except Exception as exc:
        raise CaptureFailure({"code": "zoom-baseline-mismatch", "phase": "browser-owned-zoom",
                              "zoomPercent": percent, "expectedWidth": nominal_width}) from exc
    before = page.evaluate("binding => ({dpr:Number(devicePixelRatio),width:innerWidth,bound:document.title===binding&&document.documentElement.dataset.okfZoomBinding===binding})", binding)
    try:
        result = invoke(percent)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptureFailure({"code": "zoom-unsupported", "phase": "browser-owned-zoom",
                              "zoomPercent": percent, "message": f"browser-chrome zoom driver unavailable: {exc}"}) from exc
    if result.returncode:
        raise CaptureFailure({"code": "zoom-unsupported", "phase": "browser-owned-zoom",
                              "zoomPercent": percent, "driverReturncode": result.returncode,
                              "message": "browser-chrome zoom driver failed", "stderr": result.stderr[-1000:]})
    expected_width = round(before["width"] * 100 / percent)
    try:
        # Viewport identity is window.innerWidth: it is the exact zoom-owned
        # measure (1440 → 720/360) and never absorbs the classic scrollbar.
        # documentElement.clientWidth stays separate page geometry because a
        # vertical scrollbar subtracts ~8 CSS px at 200% (712), which is not
        # a zoom failure.
        page.wait_for_function("expected => Math.abs(window.innerWidth-expected) <= 4",
                               arg=expected_width, timeout=5_000)
    except Exception as exc:
        raise CaptureFailure({"code": "zoom-unsupported", "phase": "browser-owned-zoom",
                              "zoomPercent": percent, "message": "Chrome ignored or could not expose page zoom"}) from exc
    evidence = page.evaluate(
        """() => ({
          cssViewportWidth: document.documentElement.clientWidth,
          cssViewportHeight: document.documentElement.clientHeight,
          innerWidth: window.innerWidth,
          innerHeight: window.innerHeight,
          visualViewportWidth: window.visualViewport ? Number(window.visualViewport.width || 0) : null,
          visualViewportScale: window.visualViewport ? Number(window.visualViewport.scale || 1) : null,
          devicePixelRatio: Number(window.devicePixelRatio || 1),
          cssZoom: document.body && document.body.style.zoom || null,
          mechanism: 'chrome-page-zoom-keyboard',
        })"""
    )
    evidence.update({"nominalViewportWidth": before["width"], "zoomPercent": percent,
                     "devicePixelRatioBefore": before["dpr"], "expectedCssViewportWidth": expected_width,
                     "baselinePercent": 100, "baselineVerified": before["width"] == nominal_width,
                     "sessionBinding": binding, "sessionBindingVerified": before["bound"] is True})
    _assert_zoom_evidence(evidence, expected_percent=percent)
    evidence["journey"] = _prepare_zoom_keyboard_journey(page)
    return evidence


def _apply_browser_zoom(
    page: Any, percent: int, *, runtime: "ImmutableRuntimeWorkspace | None" = None,
) -> dict[str, Any]:
    return _apply_browser_zoom_impl(page, percent, runtime=runtime)


def _prepare_zoom_keyboard_journey(page: Any) -> dict[str, Any]:
    """At real page zoom, open outline, activate a section, Escape, and restore focus."""

    outline = page.locator("details[data-okf-document-outline]").first
    summary = outline.locator("summary")
    summary.focus()
    before = page.evaluate("() => ({role:document.activeElement&&document.activeElement.tagName.toLowerCase(),name:(document.activeElement&&document.activeElement.textContent||'').trim()})")
    page.keyboard.press("Enter")
    page.wait_for_function("() => { const d=document.querySelector('details[data-okf-document-outline]'); return !!(d&&d.open); }", timeout=5_000)
    link = outline.locator("a[href^='#']").first
    target = link.get_attribute("href")
    link.focus(); page.keyboard.press("Enter")
    page.wait_for_function("target => location.hash===target", arg=target, timeout=5_000)
    summary.focus(); page.keyboard.press("Escape")
    page.wait_for_function("() => { const d=document.querySelector('details[data-okf-document-outline]'); return !!(d&&!d.open&&document.activeElement===d.querySelector('summary')); }", timeout=5_000)
    after = page.evaluate("""() => { const a=document.activeElement,r=document.documentElement,b=document.body; return {role:a&&a.tagName.toLowerCase(),name:(a&&a.textContent||'').trim(),restored:!!(a&&a.closest('details[data-okf-document-outline]')),hash:location.hash,noHorizontalOverflow:Math.max(r.scrollWidth,b.scrollWidth)<=r.clientWidth+1}; }""")
    if not target or after.get("restored") is not True or after.get("noHorizontalOverflow") is not True:
        raise CaptureFailure({"code":"zoom-keyboard-journey","phase":"browser-owned-zoom","evidence":{"before":before,"after":after,"target":target}})
    return {"outline_opened": True, "section_activated": target, "escape_closed": True,
            "pre": before, "post": after, "restored": True,
            "no_horizontal_overflow": after["noHorizontalOverflow"]}


def _apply_browser_zoom_400(
    page: Any, *, runtime: "ImmutableRuntimeWorkspace | None" = None,
) -> dict[str, Any]:
    """Compatibility wrapper for focused consumers; now uses real page zoom."""

    return _apply_browser_zoom(page, 400, runtime=runtime)


def _assert_zoom_evidence(evidence: dict[str, Any], *, expected_percent: int) -> None:
    if evidence.get("mechanism") != "chrome-page-zoom-keyboard":
        raise CaptureFailure({
            "code": "zoom-mechanism-mismatch", "phase": "browser-assertion",
            "expected": "chrome-page-zoom-keyboard", "actual": evidence.get("mechanism"),
            "message": "zoom evidence used a viewport/compositor proxy instead of real Chrome page zoom",
        })
    if evidence.get("zoomPercent") != expected_percent:
        raise CaptureFailure({"code": "zoom-mechanism-mismatch", "phase": "browser-assertion",
                              "message": "observed Chrome zoom level does not match the scenario", "evidence": evidence})
    if (evidence.get("baselinePercent") != 100 or evidence.get("baselineVerified") is not True or
            evidence.get("sessionBindingVerified") is not True or not evidence.get("sessionBinding")):
        raise CaptureFailure({"code": "zoom-session-unbound", "phase": "browser-assertion",
                              "message": "zoom was not reset to 100% and bound to the active headed page", "evidence": evidence})
    # Zoom identity is innerWidth (exact, scrollbar-free viewport measure);
    # cssViewportWidth is retained as separate page geometry only.
    if abs(int(evidence.get("innerWidth") or 0) - int(evidence.get("expectedCssViewportWidth") or -100)) > 4:
        raise CaptureFailure({"code": "zoom-mechanism-mismatch", "phase": "browser-assertion",
                              "message": "real page zoom did not produce the expected CSS viewport", "evidence": evidence})


def _browser_metadata(page: Any, spec: ScenarioSpec) -> dict[str, Any]:
    if not spec.javascript:
        return {
            "engine": "chromium", "javascript": False,
            "forced_colors": False, "reduced_motion": False,
            "zoom_percent": None,
        }
    evidence = page.evaluate(
        """() => ({
          visual_viewport_scale: window.visualViewport ? Number(window.visualViewport.scale || 1) : null,
          inner_width: window.innerWidth, inner_height: window.innerHeight,
          css_viewport_width: document.documentElement ? document.documentElement.clientWidth : null,
          css_viewport_height: document.documentElement ? document.documentElement.clientHeight : null,
          forced_colors: matchMedia('(forced-colors: active)').matches,
          reduced_motion: matchMedia('(prefers-reduced-motion: reduce)').matches,
          css_zoom: document.body.style.zoom || null,
        })"""
    )
    if spec.zoom_percent:
        reflow = page.evaluate(
            """() => {
              const root = document.documentElement;
                const body = document.body;
                const shell = document.querySelector('.okf-page');
                const shellStyle = shell && getComputedStyle(shell);
                const horizontalScrollports = Array.from(document.querySelectorAll('*')).filter(el => {
                  const style = getComputedStyle(el);
                  return el.scrollWidth > el.clientWidth + 1 && /(auto|scroll)/.test(style.overflowX);
                }).map(el => ({
                  tag: el.tagName.toLowerCase(),
                  role: el.getAttribute('role'),
                  label: el.getAttribute('aria-label'),
                  labelledby: el.getAttribute('aria-labelledby'),
                }));
                const scrollports = Array.from(document.querySelectorAll('*')).filter(el => {
                const style = getComputedStyle(el);
                return el.scrollWidth > el.clientWidth + 1 &&
                  el.scrollHeight > el.clientHeight + 1 &&
                  /(auto|scroll)/.test(style.overflowX) &&
                  /(auto|scroll)/.test(style.overflowY);
              }).map(el => ({
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role'),
                label: el.getAttribute('aria-label'),
                labelledby: el.getAttribute('aria-labelledby'),
              }));
              return {
                cssViewportWidth: root.clientWidth,
                documentScrollWidth: Math.max(root.scrollWidth, body ? body.scrollWidth : 0),
                  documentClientWidth: root.clientWidth,
                  pageGrid: shellStyle ? shellStyle.gridTemplateColumns : null,
                  pageGridTracks: shellStyle ? shellStyle.gridTemplateColumns.split(/\\s+/).filter(Boolean).length : 0,
                  horizontalScrollports,
                  scrollports,
              };
            }"""
        )
        evidence["reflow"] = reflow
        zoom_evidence = page.evaluate("() => window.__okfZoomEvidence || null")
        evidence["zoom_evidence"] = zoom_evidence
        expected_width = round(spec.viewport[0] * 100 / spec.zoom_percent)
        # Same identity contract as _assert_zoom_evidence: innerWidth owns the
        # zoom comparison; clientWidth (css_viewport_width) stays geometry.
        if abs(int(evidence.get("inner_width") or 0) - expected_width) > 4:
            raise CaptureFailure({
                "code": "zoom-mechanism-mismatch", "phase": "browser-assertion", "scenario": spec.id,
                "message": "real page zoom did not expose its expected CSS viewport",
                "evidence": evidence,
            })
        if reflow["documentScrollWidth"] > reflow["documentClientWidth"] + 1:
            raise CaptureFailure({
                "code": "zoom-horizontal-overflow", "phase": "browser-assertion", "scenario": spec.id,
                "message": "page zoom reflow has document horizontal overflow outside a local scrollport",
                "evidence": evidence,
            })
        if reflow["pageGridTracks"] != 1:
            raise CaptureFailure({
                "code": "zoom-no-single-column", "phase": "browser-assertion", "scenario": spec.id,
                "message": "page zoom reflow did not collapse the concept page to one column",
                "evidence": evidence,
            })
        if any(not item.get("role") == "region" or not (item.get("label") or item.get("labelledby"))
               for item in reflow["scrollports"]):
            raise CaptureFailure({
                "code": "zoom-unlabelled-scrollport", "phase": "browser-assertion", "scenario": spec.id,
                "message": "page zoom reflow exposed an unlabelled local two-dimensional scrollport",
                "evidence": evidence,
            })
        if not zoom_evidence:
            raise CaptureFailure({"code": "zoom-mechanism-mismatch", "phase": "browser-assertion",
                                  "scenario": spec.id, "message": "page zoom proof is missing mechanism evidence"})
        _assert_zoom_evidence(zoom_evidence, expected_percent=spec.zoom_percent)
        evidence["zoom_percent"] = spec.zoom_percent
    else:
        evidence["zoom_percent"] = 100
    evidence.update({"engine": "chromium", "javascript": True})
    evidence["acceptance_oracles"] = page.evaluate("() => window.__okfAcceptanceOracles || {}")
    if spec.action == "graph-keyboard-journey":
        evidence["graph_at_journey"] = page.evaluate("() => window.__okfGraphATJourney || null")
    return evidence


class AdmittedBundle(NamedTuple):
    """One private bundle materialized exclusively from immutable HEAD blobs."""

    original_path: Path
    path: Path
    original_identity: tuple[int, int]
    inventory: tuple[dict[str, object], ...]
    owner: tempfile.TemporaryDirectory[str]
    head: str
    tree: str

    def __enter__(self) -> "AdmittedBundle":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.owner.cleanup()


def _bundle_path(bundle: Path | AdmittedBundle | RuntimeBundle) -> Path:
    return bundle.path if isinstance(bundle, (AdmittedBundle, RuntimeBundle)) else Path(bundle)


def _validate_release_bundle(bundle: Path, *, _root: Any = _sealed_repository_root) -> Path:
    admitted = _root()
    supplied = Path(bundle).expanduser()
    if ".." in supplied.parts:
        raise CapturePreconditionError("capture bundle must resolve inside sealed checkout without traversal")
    lexical = (admitted / supplied).absolute() if not supplied.is_absolute() else supplied.absolute()
    try:
        relative = lexical.relative_to(admitted)
    except ValueError as exc:
        raise CapturePreconditionError("capture bundle must resolve inside sealed checkout") from exc
    candidate = admitted
    for part in relative.parts:
        candidate = candidate / part
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise CapturePreconditionError(f"capture bundle is unavailable: {candidate}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise CapturePreconditionError("capture bundle must resolve inside sealed checkout without symlinks")
    if candidate != admitted and admitted not in candidate.parents:
        raise CapturePreconditionError("capture bundle must resolve inside sealed checkout")
    historical_roots = (
        admitted / "docs" / "design" / "incoming-v1-theme",
        admitted / "docs" / "screenshots",
    )
    if not candidate.is_dir():
        raise CapturePreconditionError(f"capture bundle is not a directory: {candidate}")
    if any(candidate == historical or historical in candidate.parents for historical in historical_roots):
        raise CapturePreconditionError("historical bundle inputs are not valid release evidence")
    return candidate


class _GitTreeEntry(NamedTuple):
    mode: str
    kind: str
    oid: str
    repo_path: str
    relative: Path


class RuntimeBundle(NamedTuple):
    """A bundle path whose label is checkout-relative but bytes are immutable."""

    original_path: Path
    path: Path


class ImmutableRuntimeWorkspace:
    """Complete private commit-tree workspace and explicit runtime authority.

    Git is used only while this object is created. Every later dynamic code,
    template, asset, bundle, executable and subprocess cwd admission is checked
    against ``root`` and recorded in ``accesses``.
    """

    def __init__(
        self, *, owner: tempfile.TemporaryDirectory[str] | None, root: Path,
        source_root: Path, head: str, tree: str,
        inventory: tuple[dict[str, object], ...],
        strict_inventory: bool = False,
    ) -> None:
        self.owner = owner
        self.root = root
        self.source_root = source_root
        self.head = head
        self.tree = tree
        self.inventory = inventory
        self.strict_inventory = strict_inventory
        self.accesses: list[dict[str, object]] = []

    @classmethod
    def for_test(cls, root: Path, source_root: Path) -> "ImmutableRuntimeWorkspace":
        files = [
            path for path in sorted(root.rglob("*"))
            if path.is_file() and not ({".git", ".aic", "__pycache__"} & set(path.relative_to(root).parts))
        ]
        rows = tuple(
            {"path": path.relative_to(root).as_posix(), "git_mode": "100755" if path.stat().st_mode & stat.S_IXUSR else "100644",
             "oid": None, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in files
        )
        return cls(owner=None, root=root, source_root=source_root, head="test", tree="test", inventory=rows)

    def __enter__(self) -> "ImmutableRuntimeWorkspace":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        if self.owner is not None:
            self.owner.cleanup()

    def _inside(self, path: Path) -> Path:
        candidate = Path(path).absolute()
        if candidate != self.root and self.root not in candidate.parents:
            raise CapturePreconditionError(f"runtime access escaped immutable workspace: {candidate}")
        return candidate

    def audit(
        self, operation: str, *, executable: Path | str | None = None,
        cwd: Path | None = None, inputs: tuple[Path, ...] = (),
    ) -> None:
        checked_cwd = self._inside(cwd or self.root)
        checked_executable = self._inside(Path(executable)) if executable is not None else None
        checked_inputs = tuple(self._inside(path) for path in inputs)
        self.accesses.append({
            "operation": operation, "root": str(self.root), "cwd": str(checked_cwd),
            "executable": str(checked_executable) if checked_executable else None,
            "inputs": [str(path) for path in checked_inputs],
        })

    def subprocess_env(self) -> dict[str, str]:
        scripts = str(self.root / "scripts")
        return {
            "PATH": os.path.dirname(sys.executable) + os.pathsep + "/usr/bin:/bin",
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": str(self.root),
            "PYTHONPATH": scripts, "PYTHONDONTWRITEBYTECODE": "1",
        }

    def admit_bundle(self, original: Path) -> RuntimeBundle:
        relative = Path(original).absolute().relative_to(self.source_root)
        path = self._inside(self.root / relative)
        if not path.is_dir():
            raise CapturePreconditionError(f"immutable runtime bundle is unavailable: {relative}")
        self.audit("bundle", inputs=(path,))
        return RuntimeBundle(Path(original).absolute(), path)

    def verify(self) -> None:
        expected = {str(row["path"]): row for row in self.inventory}
        if self.strict_inventory:
            # lstat every entry before considering exclusions: broken links,
            # linked directories and special files cannot disappear from proof.
            actual_paths = []
            expected_dirs = {parent.as_posix() for name in expected
                             for parent in Path(name).parents if parent != Path(".")}
            for directory, dirs, files in os.walk(self.root, followlinks=False):
                for name in dirs + files:
                    path = Path(directory) / name
                    info = path.lstat()
                    relative = path.relative_to(self.root).as_posix()
                    if stat.S_ISDIR(info.st_mode) and relative in expected_dirs:
                        continue
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise CapturePreconditionError("selected runtime contains unsupported entry")
                    if relative not in expected and path.name.endswith(".okf-lock"):
                        continue  # Existing regular-file coordination exception.
                    actual_paths.append(path)
        else:
            actual_paths = [
                path for path in sorted(self.root.rglob("*"))
                if (path.is_file() and not path.name.endswith(".okf-lock")
                    and not ({".git", ".aic", "__pycache__"} & set(path.relative_to(self.root).parts)))
            ]
        actual = {path.relative_to(self.root).as_posix() for path in actual_paths}
        if actual != set(expected):
            raise CapturePreconditionError(
                "immutable runtime workspace inventory changed; "
                f"added={sorted(actual - set(expected))[:8]!r}; "
                f"missing={sorted(set(expected) - actual)[:8]!r}"
            )
        for path in actual_paths:
            relative = path.relative_to(self.root).as_posix()
            info = path.lstat()
            row = expected[relative]
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise CapturePreconditionError("immutable runtime workspace contains unsupported entry")
            data = path.read_bytes()
            if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise CapturePreconditionError(f"immutable runtime workspace changed: {relative}")
            executable = bool(info.st_mode & stat.S_IXUSR)
            if executable != (row["git_mode"] == "100755"):
                raise CapturePreconditionError(f"immutable runtime workspace mode changed: {relative}")


def _parse_bundle_root_tree(raw: bytes, bundle_path: str, *, object_format: str) -> str:
    """Require exactly one canonical tree row for the selected bundle root."""

    records = raw.split(b"\0")
    if len(records) != 2 or records[-1] or not records[0]:
        raise CapturePreconditionError("release bundle root tree metadata is malformed")
    header, separator, path_raw = records[0].partition(b"\t")
    fields = header.split(b" ")
    if not separator or len(fields) != 3 or fields[:2] != [b"040000", b"tree"]:
        raise CapturePreconditionError("release bundle directory is not a tracked HEAD tree")
    if path_raw != bundle_path.encode("utf-8"):
        raise CapturePreconditionError("release bundle root tree metadata is malformed")
    return _strict_git_oid(fields[2] + b"\n", object_format=object_format,
                           label="release bundle root tree object ID")


def _parse_ls_tree(raw: bytes, bundle_path: str, *, object_format: str) -> tuple[_GitTreeEntry, ...]:
    """Parse recursive NUL-framed ls-tree output and reject every ambiguity."""

    records = raw.split(b"\0")
    if not raw or not raw.endswith(b"\0") or records[-1] or any(not record for record in records[:-1]):
        raise CapturePreconditionError("Git bundle tree has invalid NUL framing")
    prefix = bundle_path.rstrip("/") + "/" if bundle_path else ""
    entries: list[_GitTreeEntry] = []
    seen: set[str] = set()
    leaves: set[str] = set()
    for record in records[:-1]:
        header, separator, path_raw = record.partition(b"\t")
        if not separator or b"\n" in header or b"\0" in path_raw:
            raise CapturePreconditionError("Git bundle tree contains a malformed ls-tree row")
        fields = header.split(b" ")
        if len(fields) != 3 or any(not field for field in fields):
            raise CapturePreconditionError("Git bundle tree contains a malformed ls-tree row")
        try:
            mode, kind = (field.decode("ascii") for field in fields[:2])
            oid = _strict_git_oid(fields[2] + b"\n", object_format=object_format,
                                  label="Git bundle tree object ID")
            repo_path = path_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CapturePreconditionError("Git bundle tree contains a non-canonical path or object ID") from exc
        if mode not in {"100644", "100755"} or kind != "blob":
            raise CapturePreconditionError(
                f"Git bundle tree rejects non-regular entry mode {mode}: {repo_path}"
            )
        if prefix and not repo_path.startswith(prefix):
            raise CapturePreconditionError("Git bundle tree escaped the selected bundle")
        relative_text = repo_path[len(prefix):]
        parts = relative_text.split("/")
        if (not relative_text or repo_path.startswith("/") or any(
                part in {"", ".", ".."} or "\0" in part for part in parts)):
            raise CapturePreconditionError("Git bundle tree contains an unsafe path")
        if repo_path in seen:
            raise CapturePreconditionError("Git bundle tree contains a duplicate path")
        parents = {"/".join(parts[:index]) for index in range(1, len(parts))}
        if repo_path in leaves or any(prefix + parent in leaves for parent in parents):
            raise CapturePreconditionError("Git bundle tree contains a path conflict")
        if any(existing.startswith(repo_path + "/") for existing in seen):
            raise CapturePreconditionError("Git bundle tree contains a path conflict")
        seen.add(repo_path)
        leaves.add(repo_path)
        entries.append(_GitTreeEntry(mode, kind, oid, repo_path, Path(*parts)))
    if not entries:
        raise CapturePreconditionError("Git bundle tree contains no tracked files")
    return tuple(sorted(entries, key=lambda entry: entry.repo_path))


def _materialize_runtime_workspace(
    provenance: dict[str, Any], *, _root: Any = _sealed_repository_root,
    _git: Any = _git_bytes,
) -> ImmutableRuntimeWorkspace:
    """Materialize and verify the complete captured commit tree once."""

    root = _root()
    selected = provenance.get("kind") == "selected-commit"
    head, tree = str(provenance.get("commit", provenance.get("head"))), str(provenance["tree"])
    object_format = _validate_git_object_authority(root=root, git=_git, allow_shallow=selected)
    if provenance.get("object_format") != object_format:
        raise CapturePreconditionError("Git object format changed during runtime materialization")
    if selected:
        entries = _selected_tree_entries(tree, object_format=object_format, git=_git)
    else:
        entries = _parse_ls_tree(
            _git(("ls-tree", "-rz", "--full-tree", head)), "", object_format=object_format,
        )
    owner = tempfile.TemporaryDirectory(prefix="okf-capture-runtime-")
    workspace_root = Path(owner.name) / "workspace"
    workspace_root.mkdir(mode=0o700)
    try:
        root_fd = os.open(
            workspace_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        rows: list[dict[str, object]] = []
        try:
            for entry in entries:
                data = _git(("cat-file", "blob", entry.oid))
                _verify_blob_oid(entry.oid, data)
                _write_snapshot_blob(root_fd, entry.relative, data, entry.mode)
                rows.append({
                    "path": entry.relative.as_posix(), "git_mode": entry.mode,
                    "oid": entry.oid, "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                })
        finally:
            os.close(root_fd)
        workspace = ImmutableRuntimeWorkspace(
            owner=owner, root=workspace_root, source_root=root, head=head, tree=tree,
            inventory=tuple(rows), strict_inventory=selected,
        )
        workspace.verify()
        if not selected:
            observed_head = _strict_git_oid(
                _git(("rev-parse", "HEAD")), object_format=object_format,
                label="post-materialization symbolic HEAD",
            )
            if observed_head != head:
                raise CapturePreconditionError("Git HEAD/tree moved during runtime materialization")
        return workspace
    except BaseException:
        owner.cleanup()
        raise


def _verify_blob_oid(oid: str, data: bytes) -> None:
    algorithm = "sha1" if len(oid) == 40 else "sha256"
    actual = hashlib.new(algorithm, b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    if actual != oid:
        raise CapturePreconditionError(f"Git blob response does not match object ID {oid}")


def _write_snapshot_blob(root_fd: int, relative: Path, data: bytes, mode: str) -> None:
    current_fd = os.dup(root_fd)
    try:
        for part in relative.parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=current_fd)
            except FileExistsError:
                pass
            child_fd = os.open(
                part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = child_fd
        fd = os.open(
            relative.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o700 if mode == "100755" else 0o600,
            dir_fd=current_fd,
        )
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise CapturePreconditionError("private Git snapshot write made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.close(current_fd)


def _canonical_git_path(raw: bytes, *, root: Path, label: str) -> Path:
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise CapturePreconditionError(f"sealed Git {label} is non-UTF-8") from exc
    if not text or "\0" in text or "\n" in text:
        raise CapturePreconditionError(f"sealed Git {label} is non-canonical")
    return ((root / text) if not Path(text).is_absolute() else Path(text)).resolve(strict=True)


def _validate_git_object_authority(*, root: Path, git: Any, allow_shallow: bool = False) -> str:
    """Admit only the repository's canonical common object database."""

    if os.environ.get("GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        raise CapturePreconditionError("sealed Git rejects GIT_ALTERNATE_OBJECT_DIRECTORIES")
    common = _canonical_git_path(git(("rev-parse", "--git-common-dir")), root=root,
                                 label="common directory")
    objects = _canonical_git_path(git(("rev-parse", "--git-path", "objects")), root=root,
                                  label="object directory")
    try:
        objects.relative_to(common)
    except ValueError as exc:
        raise CapturePreconditionError("sealed Git object directory escapes common authority") from exc
    if objects != (common / "objects").resolve(strict=True):
        raise CapturePreconditionError("sealed Git object directory is non-canonical")
    alternates = objects / "info" / "alternates"
    if os.path.lexists(alternates):
        raise CapturePreconditionError("sealed Git object authority rejects repository alternates")
    for anomalous in ((common / "info" / "grafts",) if allow_shallow else
                      (common / "info" / "grafts", common / "shallow")):
        if os.path.lexists(anomalous):
            raise CapturePreconditionError(f"sealed Git authority rejects {anomalous.name} metadata")
    replace_refs = common / "refs" / "replace"
    if replace_refs.exists() and any(replace_refs.iterdir()):
        raise CapturePreconditionError("sealed Git authority rejects replace refs")
    if allow_shallow and git(("for-each-ref", "--format=%(refname)", "refs/replace/")):
        raise CapturePreconditionError("sealed Git authority rejects replace refs")
    # Older Git versions may not implement GIT_NO_LAZY_FETCH. Refuse promisor
    # repositories as well, rather than allowing cat-file to hydrate objects.
    config = git(("config", "--local", "--includes", "--list")) if allow_shallow else b""
    if any(b"promisor=" in line.lower() or b"partialclone" in line.lower()
           for line in config.splitlines()):
        raise CapturePreconditionError("selected capture rejects automatic object hydration metadata")
    object_format = _strict_git_object_format(git(("rev-parse", "--show-object-format")))
    shallow = common / "shallow"
    if allow_shallow and os.path.lexists(shallow):
        info = shallow.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CapturePreconditionError("selected Git authority rejects unsafe shallow metadata")
        for line in shallow.read_bytes().splitlines(keepends=True):
            _strict_git_oid(line, object_format=object_format, label="shallow boundary")
    return object_format


def _admit_release_bundle(
    bundle: Path, *, expected_head: str | None = None, expected_tree: str | None = None,
    _validate: Any = _validate_release_bundle, _root: Any = _sealed_repository_root,
    _git: Any = _git_bytes, _provenance: Any = _git_provenance,
) -> AdmittedBundle:
    """Materialize selected bundle bytes only from one transaction-bound HEAD."""

    original = _validate(bundle)
    root = _root()
    provenance = _provenance(require_clean=True)
    head = expected_head or provenance["head"]
    tree = expected_tree or provenance["tree"]
    if (provenance["head"], provenance["tree"]) != (head, tree):
        raise CapturePreconditionError("Git HEAD/tree moved before bundle admission")
    object_format = _validate_git_object_authority(root=root, git=_git)
    if provenance.get("object_format", object_format) != object_format:
        raise CapturePreconditionError("Git object format changed during bundle admission")
    relative_root = original.relative_to(root).as_posix()
    ignored = _git((
        "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--", relative_root,
    ))
    if ignored:
        raise CapturePreconditionError("release bundle contains ignored worktree-only content")
    root_row = _git(("ls-tree", "-z", "--full-tree", head, "--", relative_root))
    _parse_bundle_root_tree(root_row, relative_root, object_format=object_format)
    raw_tree = _git(("ls-tree", "-rz", "--full-tree", head, "--", relative_root))
    entries = _parse_ls_tree(raw_tree, relative_root, object_format=object_format)
    owner = tempfile.TemporaryDirectory(prefix="okf-capture-bundle-")
    snapshot = Path(owner.name) / "bundle"
    snapshot.mkdir(mode=0o700)
    try:
        snapshot_fd = os.open(snapshot, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        rows: list[dict[str, object]] = []
        try:
            for entry in entries:
                data = _git(("cat-file", "blob", entry.oid))
                _verify_blob_oid(entry.oid, data)
                _write_snapshot_blob(snapshot_fd, entry.relative, data, entry.mode)
                rows.append({"path": entry.relative.as_posix(), "type": "regular", "tracked": True,
                             "git_mode": entry.mode, "oid": entry.oid, "bytes": len(data),
                             "sha256": hashlib.sha256(data).hexdigest(), "symlink_target": None})
        finally:
            os.close(snapshot_fd)
        final_rows = []
        for row in rows:
            path = snapshot / str(row["path"])
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise CapturePreconditionError("private Git snapshot inventory contains a non-regular entry")
            data = path.read_bytes()
            final_rows.append({**row, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        if final_rows != rows:
            raise CapturePreconditionError("private Git snapshot inventory does not match admitted blobs")
        after_head = _strict_git_oid(
            _git(("rev-parse", "HEAD")), object_format=object_format,
            label="post-materialization symbolic HEAD",
        )
        if after_head != head:
            raise CapturePreconditionError("Git HEAD/tree moved during bundle materialization")
        return AdmittedBundle(original, snapshot, (0, 0), tuple(rows), owner, head, tree)
    except BaseException:
        owner.cleanup()
        raise


def _capture_spec(
    page: Any,
    base: str,
    spec: ScenarioSpec,
    out: Path,
    *,
    provenance: dict[str, Any],
    expected_revision: str,
    input_inventory: list[dict[str, object]],
    input_inventory_digest: str,
    capture_fn: Any,
    immutable_runtime: "ImmutableRuntimeWorkspace | None" = None,
    server_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = base.rstrip("/") + spec.route
    path = out / spec.artifact
    if not isinstance(expected_revision, str) or not expected_revision.strip():
        raise CapturePreconditionError(f"scenario {spec.id} has no expected source revision")
    if spec.javascript:
        if spec.action in {"boot-blocked", "boot-failure"}:
            runtime = _capture_fallback_target(
                page, url, spec.target, path,
                expected_revision=expected_revision,
                failure_code=_FALLBACK_SCENARIO_CODES[spec.action],
                full_page=spec.full_page,
                locator=spec.locator or ".okf-live-studio-fallback",
            )
            readiness = "fallback-dom"
        else:
            try:
                runtime = capture_fn(
                    page, url, spec.target, path,
                    expected_revision=expected_revision,
                    full_page=spec.full_page, locator=spec.locator,
                    prepare=lambda current: _prepare_scenario(
                        current, spec, runtime=immutable_runtime,
                    ),
                )
            except Exception as exc:
                diagnostic = getattr(exc, "diagnostic", None)
                if isinstance(diagnostic, dict):
                    raise CaptureFailure(diagnostic) from exc
                raise
            observed_revision = (runtime.get("metadata") or {}).get("revision")
            if observed_revision != expected_revision:
                raise CaptureFailure({
                    "code": "revision-missing" if not observed_revision else "revision-mismatch",
                    "phase": "revision", "scenario": spec.id,
                    "expectedRevision": expected_revision, "observedRevision": observed_revision,
                    "message": "production capture did not return the expected source revision",
                })
            page_revision = _assert_runtime_page_revision(page, expected_revision, scenario=spec.id)
            runtime.setdefault("metadata", {})["pageRevision"] = page_revision
            readiness = "capture_target"
    else:
        runtime = _capture_nojs_target(
            page, url, spec.target, path, expected_revision=expected_revision,
            locator=spec.locator, full_page=spec.full_page,
        )
        readiness = "no-js-dom"
        _assert_nojs_state(page, spec)
    theme_state = _assert_theme_state(page, spec)
    browser = _browser_metadata(page, spec)
    artifact = _artifact_record(
        path, spec, provenance=provenance,
        expected_revision=expected_revision,
        input_inventory=input_inventory,
        input_inventory_digest=input_inventory_digest,
        runtime=runtime, theme_state=theme_state, browser=browser,
        readiness=readiness,
    )
    if server_provenance is not None:
        artifact["server_provenance_id"] = server_provenance["id"]
        artifact["server_provenance_digest"] = _digest(server_provenance)
        artifact["served_entrypoint_sha256"] = next(
            row["sha256"] for row in server_provenance["served_bytes"] if row["role"] == "entrypoint"
        )
    return artifact


def _assert_nojs_state(page: Any, spec: ScenarioSpec) -> None:
    text = page.locator("body").inner_text().lower()
    if spec.action == "no-js-graph":
        page.locator('svg[data-okf-graph-export="canonical-v1"]').wait_for(state="visible", timeout=5_000)
        return
    if not any(token in text for token in ("javascript", "interactive", "authored content")):
        raise CaptureFailure({
            "code": "no-js-fallback-missing", "phase": "no-js-dom", "scenario": spec.id,
            "message": "no-JS capture did not expose a truthful fallback or authored-content notice",
        })


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _write_manual_screen_reader_template(path: Path) -> None:
    text = """# Manual screen-reader session

**Status: NOT EXECUTED — PENDING HUMAN**

This generated worksheet does not count as executed screen-reader proof. Automation must
never change that status or infer a pass from the template's presence.

## Human session record

- Tester: PENDING
- Date/time: PENDING
- Screen reader/browser/OS and versions: PENDING
- Bundle revision and evidence manifest digest: PENDING
- Index orientation and graph-preview name: PENDING
- Concept heading/outline/Rendered/Source/Split/Focus journey: PENDING
- Search results/disclosure/error/manual-copy announcements: PENDING
- Graph list selection/path/detail/open-concept journey: PENDING
- Studio authority/error/retry/mobile journey: PENDING
- Findings and severity: PENDING
- Outcome: PENDING HUMAN
"""
    path.write_text(text, encoding="utf-8")


def _read_support_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CapturePreconditionError(f"support artifact is not valid JSON: {path.name}") from exc
    if not isinstance(value, dict) or value.get("schema") != 2 or value.get("automated") is not True:
        raise CapturePreconditionError(f"support artifact has an unsupported schema: {path.name}")
    return value


def _support_inventory(out: Path) -> list[dict[str, object]]:
    return [
        {"path": name, "bytes": (out / name).stat().st_size,
         "sha256": hashlib.sha256((out / name).read_bytes()).hexdigest()}
        for name in _SUPPORTING_ARTIFACTS
    ]


def _validate_supporting_artifacts(
    out: Path, *, expected_revisions: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    missing = [name for name in _SUPPORTING_ARTIFACTS if not (out / name).is_file()]
    if missing:
        raise CapturePreconditionError("capture supporting evidence is incomplete: " + ", ".join(missing))
    keyboard = _read_support_json(out / "keyboard-focus-trace.json")
    graph = _read_support_json(out / "graph-at-journey.json")
    zoom = _read_support_json(out / "zoom-evidence.json")
    if expected_revisions is not None:
        _validate_fixture_revisions(expected_revisions)
        if keyboard.get("source_revisions") != expected_revisions or zoom.get("source_revisions") != expected_revisions:
            raise CapturePreconditionError("support evidence is not bound to the capture revisions")
        if graph.get("source_revision") != expected_revisions.get("dense-docs"):
            raise CapturePreconditionError("graph journey is not bound to the dense fixture revision")
    keyboard_rows = keyboard.get("scenarios")
    graph_spec = next(spec for spec in SCENARIO_SPECS if spec.id == "f06-graph-keyboard")
    zoom_artifacts = {
        spec.zoom_percent: spec.artifact for spec in SCENARIO_SPECS if spec.zoom_percent in {200, 400}
    }
    if not isinstance(keyboard_rows, list) or not keyboard_rows:
        raise CapturePreconditionError("keyboard trace requires substantive scenario rows")
    if len(keyboard_rows) != 1:
        raise CapturePreconditionError("keyboard trace must bind exactly one canonical graph scenario")
    for row in keyboard_rows:
        if (not isinstance(row, dict) or row.get("scenario") != "f06-graph-keyboard" or
                row.get("artifact") != graph_spec.artifact):
            raise CapturePreconditionError("keyboard trace row is not bound to its canonical scenario/artifact")
        events = row.get("events")
        if not isinstance(events, list) or not events:
            raise CapturePreconditionError("keyboard trace requires observed ordered events")
        for index, event in enumerate(events, 1):
            required = {"sequence", "key", "pre", "post", "ownership", "active_descendant",
                        "aria_state", "native_editable", "default_prevented", "restoration"}
            if not isinstance(event, dict) or not required <= set(event) or event.get("sequence") != index:
                raise CapturePreconditionError("keyboard trace event schema is incomplete or unordered")
            if (event.get("ownership") not in {"component", "native"} or
                    type(event.get("native_editable")) is not bool or
                    type(event.get("default_prevented")) is not bool or
                    type(event.get("restoration")) is not bool or
                    not isinstance(event.get("aria_state"), dict) or
                    not isinstance(event.get("observed"), dict)):
                raise CapturePreconditionError("keyboard trace event lacks observed ownership/native/ARIA state")
            if event["observed"].get("trusted") is not True:
                raise CapturePreconditionError("keyboard trace event was not generated by trusted browser input")
            for state in (event.get("pre"), event.get("post")):
                if not isinstance(state, dict) or not isinstance(state.get("role"), str) or not isinstance(state.get("name"), str):
                    raise CapturePreconditionError("keyboard trace lacks focused role/name state")
        if not any(event.get("restoration") is True for event in events):
            raise CapturePreconditionError("keyboard trace lacks focus restoration proof")
    if (graph.get("scenario") != "f06-graph-keyboard" or
            graph.get("artifact") != graph_spec.artifact):
        raise CapturePreconditionError("graph journey is not bound to its canonical scenario/artifact")
    journey = graph.get("journey")
    if (not isinstance(journey, dict) or not journey.get("selectedId") or
            journey.get("selectedId") != journey.get("listTarget") or journey.get("pathKind") != "found" or
            journey.get("pathStart") == journey.get("pathEnd") or
            not isinstance(journey.get("pathStepCount"), int) or journey.get("pathStepCount", 0) < 2 or
            not journey.get("events")):
        raise CapturePreconditionError("graph journey is null or substantively incomplete")
    zoom_rows = zoom.get("scenarios")
    if not isinstance(zoom_rows, list) or {row.get("zoom_percent") for row in zoom_rows if isinstance(row, dict)} != {200, 400}:
        raise CapturePreconditionError("zoom evidence requires non-null 200 and 400 percent rows")
    if len(zoom_rows) != 2:
        raise CapturePreconditionError("zoom evidence must bind exactly one row per canonical zoom scenario")
    for row in zoom_rows:
        if (not isinstance(row.get("scenario"), str) or row["scenario"] != f"zoom-{row.get('zoom_percent')}" or
                row.get("artifact") != zoom_artifacts.get(row.get("zoom_percent"))):
            raise CapturePreconditionError("zoom evidence row is not bound to its canonical scenario/artifact")
        evidence = row.get("evidence")
        if (not isinstance(evidence, dict) or evidence.get("mechanism") != "chrome-page-zoom-keyboard" or
                evidence.get("zoomPercent") != row.get("zoom_percent") or
                not isinstance(evidence.get("innerWidth"), (int, float)) or
                not isinstance(evidence.get("expectedCssViewportWidth"), (int, float)) or
                abs(evidence["innerWidth"] - evidence["expectedCssViewportWidth"]) > 4 or
                not isinstance(evidence.get("cssViewportWidth"), (int, float)) or
                not isinstance(evidence.get("devicePixelRatio"), (int, float)) or
                evidence.get("devicePixelRatio") == evidence.get("devicePixelRatioBefore") or
                not isinstance(row.get("journey"), dict) or row["journey"].get("restored") is not True or
                row["journey"].get("outline_opened") is not True or row["journey"].get("escape_closed") is not True or
                not row["journey"].get("section_activated") or row["journey"].get("no_horizontal_overflow") is not True):
            raise CapturePreconditionError("zoom evidence lacks browser-owned mechanism or completed keyboard journey")
    template = (out / "manual-screen-reader-session.md").read_text(encoding="utf-8")
    if "NOT EXECUTED" not in template or "PENDING HUMAN" not in template:
        raise CapturePreconditionError("manual screen-reader template must remain pending human proof")
    inventory = _support_inventory(out)
    if any(not isinstance(row["bytes"], int) or row["bytes"] <= 0 or len(str(row["sha256"])) != 64 for row in inventory):
        raise CapturePreconditionError("support evidence inventory is empty or unhashable")
    return inventory


def _coverage_from_artifacts(artifacts: list[dict[str, Any]]) -> dict[str, list[str]]:
    coverage: dict[str, list[str]] = {key: [] for key in sorted(_REQUIRED_COVERAGE)}
    for artifact in artifacts:
        for key in artifact.get("coverage", []):
            if key in coverage:
                coverage[key].append(artifact["scenario"])
    return {key: sorted(values) for key, values in coverage.items()}


def _validate_capture_set(
    out: Path,
    artifacts: list[dict[str, Any]],
    *,
    expected_revision: str | dict[str, str] | None = None,
    input_inventory: list[dict[str, object]] | None = None,
    input_inventory_digest: str | None = None,
    selected_provenance: dict[str, Any] | None = None,
) -> None:
    if not isinstance(expected_revision, (str, dict)) or not expected_revision:
        raise CapturePreconditionError("final capture validation requires an expected source revision")
    if isinstance(expected_revision, dict):
        _validate_fixture_revisions(expected_revision)
    if not input_inventory or not input_inventory_digest:
        raise CapturePreconditionError("final capture validation requires complete input inventory provenance")
    if _digest(input_inventory) != input_inventory_digest:
        raise CapturePreconditionError("final input inventory digest does not match its serialized inventory")
    expected = {spec.artifact for spec in SCENARIO_SPECS}
    if len(artifacts) != len(SCENARIO_SPECS):
        raise CapturePreconditionError("capture matrix must bind exactly one row per scenario")
    scenario_ids = [row.get("scenario") for row in artifacts]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise CapturePreconditionError("capture matrix contains duplicate scenario bindings")
    actual = {row.get("file") for row in artifacts}
    stage_pngs = {path.name for path in out.glob("*.png")}
    if actual != expected or stage_pngs != expected:
        raise CapturePreconditionError(
            "capture matrix is incomplete or contains stale artifacts before publication"
        )
    coverage = _coverage_from_artifacts(artifacts)
    missing = [key for key, scenarios in coverage.items() if not scenarios]
    if missing:
        raise CapturePreconditionError("capture matrix has uncovered scenarios: " + ", ".join(missing))
    for artifact in artifacts:
        runtime = artifact.get("runtime")
        if not isinstance(runtime, dict):
            raise CapturePreconditionError(
                f"artifact {artifact.get('file', '<unknown>')} lacks typed runtime provenance"
            )
        if selected_provenance is not None:
            _validate_selected_source(selected_provenance, input_inventory)
            if (artifact.get("source_provenance") != selected_provenance
                    or any(key in artifact for key in ("clean", "worktree", "worktree_status", "worktree_digest"))):
                raise CapturePreconditionError("artifact lacks selected-source provenance")
        elif artifact.get("clean") is not True:
            raise CapturePreconditionError(
                f"artifact {artifact.get('file', '<unknown>')} lacks final clean-tree provenance"
            )
        if selected_provenance is None and (not _nonempty_string(artifact.get("head")) or not _nonempty_string(artifact.get("tree"))):
            raise CapturePreconditionError(
                f"artifact {artifact.get('file', '<unknown>')} has invalid source identity"
            )
        if artifact.get("input_inventory_digest") != input_inventory_digest:
            raise CapturePreconditionError(
                f"artifact {artifact.get('file', '<unknown>')} lacks input inventory provenance"
            )
        if artifact.get("input_inventory") != input_inventory:
            raise CapturePreconditionError(
                f"artifact {artifact.get('file', '<unknown>')} has incomplete input inventory provenance"
            )
        if not artifact.get("browser_assertion") or not artifact.get("scenario"):
            raise CapturePreconditionError(
                f"artifact {artifact.get('file', '<unknown>')} lacks executable scenario proof"
            )
        spec = next((item for item in SCENARIO_SPECS if item.id == artifact.get("scenario")), None)
        if spec is None:
            raise CapturePreconditionError(f"artifact {artifact.get('file', '<unknown>')} names an unknown scenario")
        width, height = spec.viewport
        exact_contract = {
            "file": spec.artifact,
            "bundle_fixture": spec.bundle,
            "action": spec.action,
            "source": spec.source,
            "route": spec.route,
            "target": spec.target,
            "capture_locator": spec.locator or spec.target,
            "browser_assertion": spec.assertion,
            "coverage": list(spec.coverage),
            "viewport": {"width": width, "height": height},
        }
        mismatched = [key for key, value in exact_contract.items() if artifact.get(key) != value]
        if mismatched:
            raise CapturePreconditionError(
                f"artifact {artifact.get('file', '<unknown>')} is not exactly bound to scenario {spec.id}: "
                + ", ".join(mismatched)
            )
        scenario_revision = expected_revision.get(spec.bundle) if isinstance(expected_revision, dict) else expected_revision
        if (
            artifact.get("expected_revision") != scenario_revision
            or artifact.get("observed_revision") != scenario_revision
            or artifact.get("source_revision") != scenario_revision
            or artifact.get("bundle_revision") != scenario_revision
            or runtime.get("revision") != scenario_revision
            or runtime.get("pageRevision") != scenario_revision
        ):
            raise CapturePreconditionError(
                f"artifact {artifact.get('file', '<unknown>')} lacks its expected source revision for the fixture"
            )
        if spec.action in {"boot-blocked", "boot-failure"}:
            metadata = runtime
            if (
                artifact.get("readiness") != "fallback-dom"
                or artifact.get("capture_kind") != "intentional-fallback"
                or metadata.get("captureKind") != "intentional-fallback"
            ):
                raise CapturePreconditionError(
                    f"failure artifact {artifact.get('file', '<unknown>')} was not captured through the fallback path"
                )
            if (
                metadata.get("failureCode") != _FALLBACK_SCENARIO_CODES[spec.action]
                or metadata.get("honestFallback") is not True
                or (artifact.get("failure_provenance") or {}).get("code") != _FALLBACK_SCENARIO_CODES[spec.action]
            ):
                raise CapturePreconditionError(
                    f"failure artifact {artifact.get('file', '<unknown>')} lacks typed fallback provenance"
                )
        elif artifact.get("readiness") != "capture_target" and spec.javascript:
            raise CapturePreconditionError(
                f"success artifact {artifact.get('file', '<unknown>')} lacks production readiness provenance"
            )
        family, mode = _public_theme(spec)
        if artifact.get("theme_family") != (family or "unresolved") or artifact.get("theme_mode") != (mode or "unresolved"):
            raise CapturePreconditionError(
                f"artifact {artifact.get('file', '<unknown>')} has a non-canonical public theme state"
            )
        theme_state = artifact.get("theme_state")
        if not isinstance(theme_state, dict):
                raise CapturePreconditionError(
                    f"artifact {artifact.get('file', '<unknown>')} lacks runtime theme-state proof"
                )
        observer = (artifact.get("browser") or {}).get("observer_provenance")
        if (
            not isinstance(observer, dict)
            or observer.get("installed") is not True
            or observer.get("scenario") != spec.id
            or tuple(observer.get("events") or ())
            != ("console", "requestfailed", "response>=400")
        ):
            raise CapturePreconditionError(
                f"artifact {artifact.get('file', '<unknown>')} was not claimed by installed browser observers"
            )
        if spec.javascript:
            if (
                theme_state.get("ready") is not True
                or theme_state.get("resolution") != "resolved"
                or theme_state.get("family") != family
                or theme_state.get("mode") != mode
                or theme_state.get("runtime_resolved_theme") != spec.resolved_theme
                or theme_state.get("resolved_mode") != (spec.resolved_theme or "").rsplit("-", 1)[-1]
            ):
                raise CapturePreconditionError(
                    f"artifact {artifact.get('file', '<unknown>')} lacks resolved runtime theme proof"
                )
            if spec.storage == "denied" and theme_state.get("storageAccess") != "denied":
                raise CapturePreconditionError(
                    f"artifact {artifact.get('file', '<unknown>')} lacks storage-denied proof"
                )
            if spec.storage == "none" and theme_state.get("storageAccess") == "available" and any(
                key in _THEME_STORAGE_KEYS for key in (theme_state.get("storageKeys") or [])
            ):
                raise CapturePreconditionError(
                    f"artifact {artifact.get('file', '<unknown>')} includes saved Auto storage"
                )
    revisions = expected_revision if isinstance(expected_revision, dict) else None
    _validate_supporting_artifacts(out, expected_revisions=revisions)


def _source_record(provenance: dict[str, Any]) -> dict[str, Any]:
    if provenance.get("kind") == "selected-commit":
        return {"source_provenance": provenance}
    return {
        "head": provenance["head"], "tree": provenance["tree"],
        "clean": provenance["clean"], "worktree_digest": provenance["worktree_digest"],
        "worktree_status": list(provenance.get("worktree_status", [])),
        "worktree": {"clean": provenance["clean"], "digest": provenance["worktree_digest"],
                     "status": list(provenance.get("worktree_status", []))},
    }


def _validate_selected_source(provenance: dict[str, Any], inventory: list[dict[str, Any]]) -> None:
    if (not isinstance(provenance, dict) or set(provenance) != {
            "kind", "protocol", "commit", "tree", "object_format", "driver", "inventory_digest"}
            or provenance["kind"] != "selected-commit"
            or provenance["protocol"] != SELECTED_CAPTURE_PROTOCOL
            or provenance["inventory_digest"] != _digest(inventory)):
        raise CapturePreconditionError("invalid selected-source provenance")
    for key in ("commit", "tree"):
        if not isinstance(provenance[key], str):
            raise CapturePreconditionError("invalid selected-source object identity")
        _strict_git_oid((provenance[key] + "\n").encode(),
                        object_format=provenance["object_format"], label="selected source")
    driver = next((row for row in inventory if row.get("path") == _SELECTED_DRIVER), None)
    if driver is None or provenance["driver"] != {
            "path": _SELECTED_DRIVER, "oid": driver["oid"], "sha256": driver["sha256"]}:
        raise CapturePreconditionError("invalid selected-source driver identity")


def _verified_object(oid: str, kind: str, *, git: Any) -> bytes:
    data = git(("cat-file", kind, oid))
    algorithm = "sha1" if len(oid) == 40 else "sha256"
    actual = hashlib.new(algorithm, kind.encode() + b" " + str(len(data)).encode() + b"\0" + data).hexdigest()
    if actual != oid:
        raise CapturePreconditionError("selected Git object does not match its full object ID")
    return data


def _selected_provenance(revision: str, *, root: Path, git: Any) -> dict[str, Any]:
    object_format = _validate_git_object_authority(root=root, git=git, allow_shallow=True)
    try:
        commit = _strict_git_oid(revision.encode("ascii") + b"\n", object_format=object_format,
                                 label="selected revision")
    except UnicodeError as exc:
        raise CapturePreconditionError("selected revision must be a full commit OID") from exc
    if git(("cat-file", "-t", commit)) != b"commit\n":
        raise CapturePreconditionError("selected revision is not a commit")
    data = _verified_object(commit, "commit", git=git)
    first = data.split(b"\n", 1)[0]
    if not first.startswith(b"tree "):
        raise CapturePreconditionError("selected commit has no canonical tree")
    tree = _strict_git_oid(first[5:] + b"\n", object_format=object_format, label="selected tree")
    return {"kind": "selected-commit", "protocol": SELECTED_CAPTURE_PROTOCOL,
            "commit": commit, "tree": tree, "object_format": object_format}


def _selected_tree_entries(tree: str, *, object_format: str, git: Any) -> tuple[_GitTreeEntry, ...]:
    """Enumerate only hash-verified tree bytes, never a second symbolic lookup.

    Shallow parents are not inputs. A cache binds reused subtrees to the same
    verified bytes even if the local object database changes during admission.
    """
    pending = [(tree, b"")]
    verified: dict[str, bytes] = {}
    rows = []
    width = {"sha1": 20, "sha256": 32}[object_format]
    while pending:
        oid, prefix = pending.pop()
        if oid not in verified:
            verified[oid] = _verified_object(oid, "tree", git=git)
        data = verified[oid]
        if not data:
            raise CapturePreconditionError("selected tree contains an unsupported empty directory")
        names: set[bytes] = set()
        while data:
            header, separator, rest = data.partition(b"\0")
            mode, space, name = header.partition(b" ")
            if (not separator or not space or not name or b"/" in name or name in names
                    or name in {b".", b"..", b".git"} or len(rest) < width
                    or mode not in {b"40000", b"100644", b"100755"}):
                raise CapturePreconditionError("selected tree contains unsafe metadata")
            names.add(name)
            child, data = rest[:width].hex(), rest[width:]
            if mode == b"40000":
                pending.append((child, prefix + name + b"/"))
            else:
                rows.append(mode + b" blob " + child.encode() + b"\t" + prefix + name + b"\0")
    return _parse_ls_tree(b"".join(rows), "", object_format=object_format)


def _selected_bundle(bundle: Path, runtime: ImmutableRuntimeWorkspace) -> Path:
    supplied = bundle.expanduser()
    original = supplied if supplied.is_absolute() else runtime.source_root / supplied
    try:
        relative = original.relative_to(runtime.source_root)
    except ValueError as exc:
        raise CapturePreconditionError("selected bundle must be inside source repository") from exc
    if ".." in relative.parts:
        raise CapturePreconditionError("selected bundle must not traverse directories")
    # Apply the existing historical-path restrictions to the selected snapshot,
    # never to the caller's possibly absent/dirty/symlinked bundle directory.
    _validate_release_bundle(runtime.root / relative, _root=lambda: runtime.root)
    runtime.admit_bundle(original)
    return relative


def _selected_driver_identity(runtime: ImmutableRuntimeWorkspace) -> dict[str, Any]:
    row = next((row for row in runtime.inventory if row["path"] == _SELECTED_DRIVER), None)
    if row is None:
        raise CapturePreconditionError("unsupported selected capture driver: missing driver")
    try:
        module = ast.parse((runtime.root / _SELECTED_DRIVER).read_bytes())
        declarations = [node.value for node in module.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "SELECTED_CAPTURE_PROTOCOL"
                                for target in node.targets)]
        entrypoints = [node for node in module.body if isinstance(node, ast.FunctionDef)
                       and node.name == "selected_capture_v1"]
        compatible = (len(declarations) == 1 and isinstance(declarations[0], ast.Constant)
                      and declarations[0].value == SELECTED_CAPTURE_PROTOCOL
                      and len(entrypoints) == 1)
        if compatible:
            args = entrypoints[0].args
            compatible = (len(args.posonlyargs + args.args) == 1 and not args.defaults
                          and not args.kwonlyargs and args.vararg is None and args.kwarg is None)
    except (SyntaxError, ValueError, UnicodeError) as exc:
        raise CapturePreconditionError("unsupported selected capture driver: invalid source") from exc
    if not compatible:
        raise CapturePreconditionError("unsupported selected capture driver protocol")
    return {"path": _SELECTED_DRIVER, "oid": row["oid"], "sha256": row["sha256"]}


def _dispatch_selected(args: argparse.Namespace, *, root: Path) -> int:
    """Trusted local code execution, NOT a sandbox. Only the parent publishes."""
    destination = _validate_release_destination(args.out, _root=lambda: root)
    def git(command: tuple[str, ...]) -> bytes:
        try:
            return _git_bytes(command, _root=lambda: root)
        except CapturePreconditionError as exc:
            raise CapturePreconditionError("selected Git object or metadata unavailable") from exc
    provenance = _selected_provenance(args.revision, root=root, git=git)
    with _materialize_runtime_workspace(provenance, _root=lambda: root, _git=git) as runtime:
        provenance["driver"] = _selected_driver_identity(runtime)
        bundle = _selected_bundle(args.bundle or Path("samples/demo_bundle"), runtime)
        inventory = _runtime_inventory(runtime)
        digest = _digest(inventory)
        provenance["inventory_digest"] = digest
        _validate_selected_source(provenance, inventory)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{destination.name}.capture-stage-",
                                         dir=destination.parent) as stage:
            request = {"protocol": SELECTED_CAPTURE_PROTOCOL, "runtime_root": str(runtime.root),
                       "source_root": str(root), "stage": stage, "bundle": bundle.as_posix(),
                       "chrome": args.chrome, "provenance": provenance,
                       "inventory": inventory, "inventory_digest": digest}
            # -I/-B avoid ambient Python paths/startup and bytecode writes. The
            # sole repository import path is the complete selected snapshot.
            bootstrap = '''import importlib.util,json,sys
from pathlib import Path
r=json.load(sys.stdin)
p=Path(r["runtime_root"])/"scripts/capture_ui_overhaul.py"
sys.path.insert(0,str(p.parent))
s=importlib.util.spec_from_file_location("_okf_selected_driver",p)
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
if m.SELECTED_CAPTURE_PROTOCOL != r["protocol"]: raise SystemExit(2)
raise SystemExit(m.selected_capture_v1(r))
'''
            env = runtime.subprocess_env()
            # Host browser/display dependencies remain operator-owned, not Git
            # inputs. Never serialize their environment or credentials.
            for key in ("HOME", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY",
                        "PLAYWRIGHT_BROWSERS_PATH", "OKF_CHROME", "AIC_PLAYWRIGHT_CHROME_PATH"):
                if key in os.environ:
                    env[key] = os.environ[key]
            if os.environ.get("OKF_CAPTURE_REAL_BROWSER_ZOOM") == "1":
                env["OKF_CAPTURE_REAL_BROWSER_ZOOM"] = "1"
            result = subprocess.run([sys.executable, "-I", "-B", "-c", bootstrap],
                                    input=json.dumps(request), text=True, cwd=runtime.root,
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode:
                raise CapturePreconditionError("selected capture driver failed; no evidence published")
            runtime.verify()
            manifest = json.loads((Path(stage) / "manifest.json").read_text())
            if (not isinstance(manifest, dict) or manifest.get("schema") != 4
                    or any(key in manifest for key in ("clean", "worktree", "worktree_status", "worktree_digest"))
                    or manifest.get("source_provenance") != provenance
                    or manifest.get("input_inventory") != inventory
                    or manifest.get("input_inventory_digest") != digest
                    or manifest.get("input_inventory_digest_after") != digest):
                raise CapturePreconditionError("selected capture driver returned incompatible provenance")
            _publish_generation(Path(stage), destination, expected_provenance=_publication_provenance(
                provenance, inventory_digest=digest, runtime=runtime))
    print(destination)
    return 0


def selected_capture_v1(request: dict[str, Any]) -> int:
    """Versioned Python driver protocol; a private stage, never a destination.

    The launcher supplies already verified objects. This selected driver owns
    scenario/page-revision validation; the launcher rechecks inventory before
    publication. There is no fallback to a different revision's capture code.
    """
    if (not isinstance(request, dict) or set(request) != {
            "protocol", "runtime_root", "source_root", "stage", "bundle", "chrome",
            "provenance", "inventory", "inventory_digest"}
            or request["protocol"] != SELECTED_CAPTURE_PROTOCOL):
        raise CapturePreconditionError("unsupported selected driver request")
    root = _sealed_repository_root()
    if request["runtime_root"] != str(root):
        raise CapturePreconditionError("selected driver is not executing in selected workspace")
    provenance = request["provenance"]
    _validate_selected_source(provenance, request["inventory"])
    runtime = ImmutableRuntimeWorkspace(owner=None, root=root, source_root=Path(request["source_root"]),
                                        head=provenance["commit"], tree=provenance["tree"],
                                        inventory=tuple(request["inventory"]), strict_inventory=True)
    runtime.verify()
    if (_digest(_runtime_inventory(runtime)) != request["inventory_digest"]
            or _selected_driver_identity(runtime) != provenance["driver"]):
        raise CapturePreconditionError("selected driver input identity mismatch")
    args = argparse.Namespace(bundle=runtime.source_root / request["bundle"], chrome=request["chrome"])
    return _run_capture(args, root=runtime.source_root,
                        admitted=(runtime, provenance, Path(request["stage"])))


def main(argv: list[str] | None = None, *, _root: Any = _sealed_repository_root) -> int:
    root = _root()
    parser = argparse.ArgumentParser(
        description="Capture the complete final-v2 UI evidence matrix using production readiness."
    )
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--revision", help="full local commit OID supplying both compatible driver and product (trusted code)")
    parser.add_argument("--workspace", "--source-root", dest="workspace", type=Path,
                        help=argparse.SUPPRESS)
    parser.add_argument(
        "--out", type=Path, default=Path("/tmp/okf-ui-overhaul-final"),
        help="explicit artifact directory (default: /tmp/okf-ui-overhaul-final)",
    )
    parser.add_argument("--chrome", help="Chrome/Chromium executable")
    parser.add_argument(
        "--manifest-only", action="store_true",
        help="print the executable scenario matrix without launching or publishing",
    )
    args = parser.parse_args(argv)
    requested_root = args.workspace or os.environ.get("OKF_CAPTURE_SOURCE_ROOT")
    if requested_root is not None and Path(requested_root).expanduser().resolve() != root:
        print(json.dumps({"code": "workspace-mismatch",
                          "message": "capture runner operates only on its checked-in ROOT"},
                         sort_keys=True), file=sys.stderr)
        return 2
    if args.manifest_only:
        if args.revision is not None:
            parser.error("--revision cannot be combined with --manifest-only")
        print(json.dumps(release_scenario_manifest(), indent=2, sort_keys=True))
        return 0

    if args.revision is not None:
        try:
            return _dispatch_selected(args, root=root)
        except CapturePreconditionError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except Exception:
            # Never emit Git stderr, dirty paths, driver output or ambient data.
            print("selected capture failed; inspect destination and its capture-authority recovery before retry",
                  file=sys.stderr)
            return 2
    return _run_capture(args, root=root)


def _run_capture(args: argparse.Namespace, *, root: Path,
                 admitted: tuple[ImmutableRuntimeWorkspace, dict[str, Any], Path] | None = None) -> int:

    stage_owner: tempfile.TemporaryDirectory[str] | None = None
    immutable_runtime: ImmutableRuntimeWorkspace | None = None
    try:
        if admitted is None:
            destination = _validate_release_destination(args.out)
            provenance = _git_provenance(require_clean=True)
            selected_bundle = _validate_release_bundle(args.bundle or root / "samples/demo_bundle")
            immutable_runtime = _materialize_runtime_workspace(provenance)
        else:
            immutable_runtime, provenance, out = admitted
            selected_bundle = args.bundle
        expected_head, expected_tree = immutable_runtime.head, immutable_runtime.tree
        # Architectural cut-over: after this line no runtime/build/serve
        # consumer may consult the physical checkout or Git authority.
        bundle = immutable_runtime.admit_bundle(selected_bundle)
        dense_bundle = immutable_runtime.admit_bundle(root / "docs-bundle")
        empty_bundle = immutable_runtime.admit_bundle(root / "samples/empty_bundle")
        if admitted is None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            stage_owner = tempfile.TemporaryDirectory(
                prefix=f".{destination.name}.capture-stage-", dir=destination.parent,
            )
            out = Path(stage_owner.name)
        before_inventory = _runtime_inventory(immutable_runtime)
        before_digest = _digest(before_inventory)
        revisions = {"demo": _derive_expected_revision(bundle, runtime=immutable_runtime),
                     "dense-docs": _derive_expected_revision(dense_bundle, runtime=immutable_runtime),
                     "empty-okf": _derive_expected_revision(empty_bundle, runtime=immutable_runtime)}
        _validate_fixture_revisions(revisions)
        expected_revision = revisions["demo"]

        try:
            from playwright.sync_api import sync_playwright
            import importlib.util
            helper_path = immutable_runtime.root / "scripts/capture_viewer_proof.py"
            immutable_runtime.audit("load-capture-helper", inputs=(helper_path,))
            helper_spec = importlib.util.spec_from_file_location(
                "_okf_immutable_capture_viewer_proof", helper_path,
            )
            if helper_spec is None or helper_spec.loader is None:
                raise ImportError("immutable capture helper has no loader")
            helper_module = importlib.util.module_from_spec(helper_spec)
            helper_spec.loader.exec_module(helper_module)
            capture_target = helper_module.capture_target
            launch_chromium = helper_module.launch_chromium
        except ImportError as exc:
            raise CapturePreconditionError(
                f"capture dependency unavailable: {exc}; install the repository's Playwright test dependencies"
            ) from exc

        artifacts: list[dict[str, Any]] = []
        server_provenance: list[dict[str, Any]] = []
        unexpected_events: list[dict[str, Any]] = []
        expected_events: list[dict[str, Any]] = []
        event_state: dict[str, ScenarioSpec | None] = {"current": None}

        def record_event(kind: str, text: str, *, status: int | None = None, url: str | None = None) -> None:
            spec = event_state["current"]
            if kind == "requestfailed" and url and (
                url.endswith(("/__events", "/__presence", "/favicon.ico"))
                or "/__events?" in url or "/__presence?" in url
            ):
                return
            event = {
                "kind": kind, "message": text, "status": status,
                "url": url, "scenario": spec.id if spec else None,
            }
            expected = bool(spec and (
                (status is not None and status in spec.expected_http_status)
                or any(fragment in text for fragment in spec.expected_event_fragments)
            ))
            (expected_events if expected else unexpected_events).append(event)

        def install_observers(page: Any, spec: ScenarioSpec) -> dict[str, Any]:
            """Install every error observer before a candidate can navigate or route."""

            provenance: dict[str, Any] = {
                "installed": True,
                "scenario": spec.id,
                "events": ("console", "requestfailed", "response>=400"),
                "observed": 0,
            }

            def observed(kind: str, text: str, *, status: int | None = None,
                         url: str | None = None) -> None:
                provenance["observed"] = int(provenance["observed"]) + 1
                previous = event_state["current"]
                event_state["current"] = spec
                try:
                    record_event(kind, text, status=status, url=url)
                finally:
                    event_state["current"] = previous

            page.on(
                "console",
                lambda message: (
                    observed("console", message.text, url=page.url)
                    if message.type == "error" and "Failed to load resource" not in message.text else None
                ),
            )
            page.on(
                "response",
                lambda response: observed(
                    "response", f"http {response.status}: {response.url}",
                    status=response.status, url=response.url,
                ) if response.status >= 400 and not response.url.endswith("/favicon.ico") else None,
            )
            page.on(
                "requestfailed",
                lambda request: observed("requestfailed", "network: " + request.url, url=request.url),
            )
            return provenance

        def run_source(browser: Any, source: str, base: str, *, server_record: dict[str, Any],
                       selected_specs: tuple[ScenarioSpec, ...] | None = None) -> None:
            rows = selected_specs if selected_specs is not None else SCENARIO_SPECS
            for spec in (item for item in rows if item.source == source):
                context_kwargs: dict[str, Any] = {
                    "viewport": {"width": spec.viewport[0], "height": spec.viewport[1]},
                    "color_scheme": spec.color_scheme,
                    "reduced_motion": "reduce" if spec.reduced_motion else "no-preference",
                    "forced_colors": "active" if spec.forced_colors else "none",
                    "java_script_enabled": spec.javascript,
                }
                context = browser.new_context(**context_kwargs)
                seed = _theme_seed_script(spec)
                if spec.javascript and spec.storage == "none":
                    context.add_init_script(_clear_theme_storage_script())
                elif spec.javascript and spec.storage == "denied":
                    context.add_init_script(_storage_denied_script())
                elif seed and spec.javascript:
                    context.add_init_script(seed)
                if spec.javascript and spec.density == "compact":
                    context.add_init_script("localStorage.setItem('okf-theme-density','compact')")
                if spec.target == "graph" and spec.javascript:
                    context.add_init_script("localStorage.setItem('okfGraphTourDone','1')")
                page = context.new_page()
                observer_provenance = install_observers(page, spec)
                if observer_provenance.get("installed") is not True:
                    raise CapturePreconditionError(
                        f"scenario {spec.id} has no installed browser error observers"
                    )
                if spec.action == "boot-blocked":
                    page.route("**/studio.js", lambda route: route.abort("blockedbyclient"))
                elif spec.action == "boot-failure":
                    page.route(
                        "**/studio.js",
                        lambda route: route.fulfill(
                            status=200, content_type="application/javascript",
                            body="throw new Error('capture injected studio failure');",
                        ),
                    )
                event_state["current"] = spec
                try:
                    artifact = _capture_spec(
                            page, base, spec, out, provenance=provenance,
                            expected_revision=revisions[spec.bundle],
                            input_inventory=before_inventory,
                            input_inventory_digest=before_digest, capture_fn=capture_target,
                            immutable_runtime=immutable_runtime,
                            server_provenance=server_record,
                        )
                    artifact["browser"]["observer_provenance"] = dict(observer_provenance)
                    artifacts.append(artifact)
                finally:
                    event_state["current"] = None
                    context.close()

        with tempfile.TemporaryDirectory(prefix="okf-ui-build-") as built:
            built_root = Path(built)
            target_dirs = {
                target: built_root / target for target in ("static", "spa", "single-file")
            }
            build_provenance: dict[str, dict[str, Any]] = {}
            for target, target_dir in target_dirs.items():
                build_provenance[target] = _build_target(
                    bundle, target, target_dir, runtime=immutable_runtime)

            with sync_playwright() as playwright:
                requires_browser_chrome = any(spec.zoom_percent in {200, 400} for spec in SCENARIO_SPECS)
                if requires_browser_chrome and os.environ.get("OKF_CAPTURE_REAL_BROWSER_ZOOM") != "1":
                    raise CaptureFailure({"code":"zoom-unsupported","phase":"browser-launch",
                                          "message":"release matrix requires explicit headed Chrome admission"})
                try:
                    browser = launch_chromium(playwright, args.chrome, headed=requires_browser_chrome)
                except Exception as exc:
                    if requires_browser_chrome:
                        raise CaptureFailure({"code":"zoom-unsupported","phase":"browser-launch",
                                              "message":f"headed Chrome launch unavailable: {exc}"}) from exc
                    raise
                try:
                    live_groups = sorted({(spec.bundle, spec.server_mode) for spec in SCENARIO_SPECS if spec.source == "live"})
                    for bundle_name, server_mode in live_groups:
                        selected_bundle = {"demo": bundle, "dense-docs": dense_bundle, "empty-okf": empty_bundle}[bundle_name]
                        server = _LiveServer(selected_bundle, mode=server_mode, runtime=immutable_runtime)
                        with server as live_base:
                            server.assert_running()
                            if server.provenance is None:
                                raise CapturePreconditionError("live server has no sealed provenance")
                            selected_specs = tuple(
                                spec for spec in SCENARIO_SPECS
                                if spec.bundle == bundle_name and spec.server_mode == server_mode
                            )
                            run_source(browser, "live", live_base, server_record=server.provenance,
                                       selected_specs=selected_specs)
                            server.assert_running()
                            # The context owner must have observed the process alive before cleanup.
                            # __exit__ then owns graceful termination and kill fallback.
                        if server.process is not None:
                            raise CapturePreconditionError("live server cleanup did not release its process")
                        server_provenance.append(server.provenance)
                    for target in ("static", "spa", "single-file"):
                        static_server = _StaticServer(
                            target_dirs[target], runtime=immutable_runtime, target=target,
                            build=build_provenance[target],
                        )
                        with static_server as base:
                            if static_server.provenance is None:
                                raise CapturePreconditionError("static server has no sealed provenance")
                            run_source(browser, target, base, server_record=static_server.provenance)
                        if static_server.process is not None:
                            raise CapturePreconditionError("static server cleanup did not release its process")
                        server_provenance.append(static_server.provenance)
                finally:
                    browser.close()


        if unexpected_events:
            raise CaptureFailure({
                "code": "browser-errors", "phase": "browser-assertion",
                "message": "capture completed with unexpected console/network events",
                "events": unexpected_events,
            })
        for spec in SCENARIO_SPECS:
            observed = [event for event in expected_events if event.get("scenario") == spec.id]
            if any(status not in [event.get("status") for event in observed] for status in spec.expected_http_status):
                raise CaptureFailure({
                    "code": "expected-browser-event-missing", "phase": "browser-assertion",
                    "scenario": spec.id, "expected_status": list(spec.expected_http_status),
                })
            if spec.expected_event_fragments and not any(
                any(fragment in str(event.get("message")) for fragment in spec.expected_event_fragments)
                for event in observed
            ):
                raise CaptureFailure({
                    "code": "expected-browser-event-missing", "phase": "browser-assertion",
                    "scenario": spec.id, "expected_fragments": list(spec.expected_event_fragments),
                })

        after_inventory = _runtime_inventory(immutable_runtime)
        after_digest = _digest(after_inventory)
        _assert_input_inventory_stable(before_inventory, after_inventory)
        # All consumers below read only the admitted immutable snapshots. A
        # later symbolic branch move cannot change or relabel their bytes.
        keyboard_rows = [row for row in artifacts if row["scenario"] == "f06-graph-keyboard"]
        _write_json_atomic(out / "keyboard-focus-trace.json", {
            "schema": 2, "automated": True, "source_revisions": revisions,
            "scenarios": [{"scenario": row["scenario"], "artifact": row["file"],
                           "events": row["browser"].get("graph_at_journey", {}).get("events")} for row in keyboard_rows],
        })
        graph_row = next(row for row in artifacts if row["scenario"] == "f06-graph-keyboard")
        _write_json_atomic(out / "graph-at-journey.json", {
            "schema": 2, "automated": True, "source_revision": graph_row["source_revision"],
            "artifact": graph_row["file"], "scenario": graph_row["scenario"],
            "journey": graph_row["browser"].get("graph_at_journey"),
        })
        zoom_rows = [row for row in artifacts if row.get("browser", {}).get("zoom_percent") in {200, 400}]
        _write_json_atomic(out / "zoom-evidence.json", {
            "schema": 2, "automated": True, "source_revisions": revisions,
            "required_mechanism": "chrome-page-zoom-keyboard",
            "scenarios": [{"scenario": row["scenario"], "artifact": row["file"],
                           "zoom_percent": row["browser"].get("zoom_percent"),
                           "evidence": row["browser"].get("zoom_evidence"),
                           "journey": (row["browser"].get("zoom_evidence") or {}).get("journey")} for row in zoom_rows],
        })
        _write_manual_screen_reader_template(out / "manual-screen-reader-session.md")
        support_inventory = _validate_supporting_artifacts(out, expected_revisions=revisions)
        _validate_server_bindings(artifacts, server_provenance, runtime=immutable_runtime)
        _validate_capture_set(
            out, artifacts, expected_revision=revisions,
            input_inventory=before_inventory, input_inventory_digest=before_digest,
            selected_provenance=provenance if admitted is not None else None,
        )
        artifacts = sorted(artifacts, key=lambda row: str(row["file"]))
        manifest = {
            "schema": 4 if admitted is not None else 3,
            "name": "okf-loom-v2-ui-overhaul-final",
            # Automated browser evidence is useful but is not final release
            # evidence until the required human screen-reader session is
            # executed and accepted by a compatible consumer.
            "final_release_evidence": False,
            "evidence_status": "INTERIM AUTOMATED — PENDING HUMAN",
            "automated_evidence": {
                "status": "ACCEPTED",
                "counts_as_interim_evidence": True,
                "capture_count": len(artifacts),
                "coverage": _coverage_from_artifacts(artifacts),
            },
            "head": expected_head, "head_revision": expected_head,
            "tree": provenance["tree"], "tree_revision": provenance["tree"],
            "source_workspace": str(root),
            "runtime_workspace": {
                "kind": "private-complete-commit-tree",
                "head": immutable_runtime.head, "tree": immutable_runtime.tree,
                "inventory_digest": before_digest,
                "file_count": len(immutable_runtime.inventory),
                "access_registry": list(immutable_runtime.accesses),
                "cleaned_after_publication": True,
            },
            **_source_record(provenance),
            "expected_revision": expected_head,
            "expected_tree_revision": expected_tree,
            "source_revisions": revisions,
            "source_revision": expected_head,
            "source_tree_revision": expected_tree,
            "bundle_revision": expected_head,
            "bundle_tree_revision": expected_tree,
             "bundles": {"demo": str(bundle.original_path), "dense-docs": str(dense_bundle.original_path),
                          "empty-okf": {"kind": "checked-in-zero-concept-okf-v1",
                                        "path": str(empty_bundle.original_path), "revision": revisions["empty-okf"],
                                        "inventory_digest": _digest([row for row in before_inventory if row.get("path", "").startswith("samples/empty_bundle/")])}},
            "bundle": str(bundle.original_path), "input_inventory": before_inventory,
            "input_inventory_digest": before_digest,
            "input_inventory_digest_after": after_digest,
            "scenario_manifest": release_scenario_manifest(),
            "coverage": _coverage_from_artifacts(artifacts),
            "artifacts": artifacts, "captures": [row["file"] for row in artifacts],
            "artifact_digest": _digest({"captures": artifacts, "support": support_inventory}),
            "console_or_network_errors": unexpected_events,
            "expected_browser_events": expected_events,
            "production_readiness": "capture_target; fallback-dom; no-js-dom",
            "screen_reader_proof": {
                "artifact": "manual-screen-reader-session.md", "status": "PENDING HUMAN",
                "automated": False, "counts_as_executed_proof": False,
            },
            "supporting_artifacts": support_inventory,
            "supporting_artifact_digest": _digest(support_inventory),
            "static_build_completed": True,
            "built_targets": ["static", "spa", "single-file"],
            "server_provenance": server_provenance,
            "server_cleanup": {"all_processes_stopped": True, "owned_server_count": len(server_provenance)},
        }
        if admitted is not None:
            # Schema 4 names Git identities only in source_provenance and the
            # runtime workspace. source_revisions retains rendered content IDs.
            for key in ("head", "head_revision", "tree", "tree_revision", "expected_revision",
                        "expected_tree_revision", "source_revision", "source_tree_revision",
                        "bundle_revision", "bundle_tree_revision"):
                del manifest[key]
        _write_json_atomic(out / "manifest.json", manifest)
        if admitted is not None:
            return 0  # Launcher alone verifies and publishes this private stage.
        _publish_generation(
            out, destination,
            expected_provenance=_publication_provenance(
                provenance, inventory_digest=before_digest,
                runtime=immutable_runtime,
            ),
        )
        print(destination)
        return 0
    except CapturePreconditionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except CaptureFailure as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"code": "capture-failure", "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    finally:
        if stage_owner is not None:
            stage_owner.cleanup()
        if immutable_runtime is not None:
            immutable_runtime.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
