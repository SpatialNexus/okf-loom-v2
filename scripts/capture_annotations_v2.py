#!/usr/bin/env python3
"""Capture durable current-branch Annotations v2 Studio proof.

The helper owns a temporary bundle/server and waits on semantic DOM predicates;
it contains no screenshot timing sleeps. Output is intentionally tracked under
docs/screenshots/annotations-v2.
"""
from __future__ import annotations

import hashlib
import errno
import fcntl
import json
import os
import ctypes
import ctypes.util
from contextlib import contextmanager
from datetime import datetime, timezone
import shutil
import stat
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "screenshots" / "annotations-v2"

# Exact expected screenshot artifact set. The capture flow must produce
# exactly these files. Obsolete filenames (e.g. stale-ambiguous.png) must
# never enter the manifest or survive in the output directory.
EXPECTED_PNG_FILES: frozenset[str] = frozenset({
    "resolved.png",
    "stale-after-mutation.png",
    "selecting.png",
    "conflict.png",
    "save-failure.png",
    "focus-visible.png",
    "mobile-320.png",
    "forced-colors.png",
    "no-js.png",
})

SCENARIO_ASSERTIONS: dict[str, list[str]] = {
    # P1-8: "resolved" here means the ANCHOR resolved ( Attached / verified
    # text-quote match), NOT the thread lifecycle. The thread stays open; the
    # persistent "Attached" anchor-health label and Jump control prove the
    # anchor is attached while the thread remains unresolved.
    "resolved.png": ["anchor Attached (health), thread open", "runtime mark visible", "Jump control visible"],
    "stale-after-mutation.png": ["canonical source mutation produced Text not found"],
    "selecting.png": ["real Re-anchor control entered selection mode", "document context bar visible"],
    "conflict.png": ["browser handling injected HTTP 409 envelope", "Refresh and reselect visible"],
    "save-failure.png": ["browser handling injected HTTP 500 envelope", "Retry save visible", "document context bar visible"],
    "focus-visible.png": ["Retry save keyboard focused with visible outline"],
    "mobile-320.png": [
        "320px document-peek: recovery panel and scrim hidden, authored document visible",
        "okf-reanchor-peek Return to comment visible and bounded, no control overlap",
    ],
    "forced-colors.png": ["forced-colors active on recovery workflow", "single focus outline"],
    "no-js.png": ["exactly one no-JS notice", "authored concept remains visible", "no comment sentinels in HTML"],
}


def free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def wait_server(proc: subprocess.Popen, base: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("Studio server exited before capture")
        try:
            with urllib.request.urlopen(base + "/", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
    raise RuntimeError("Studio server did not become ready")


def seed(page, statuses):
    from playwright.sync_api import expect

    page.evaluate(
        """(statuses) => {
          const exact='customer_id references customers';
          window.okfLoomStudio.state.comments=statuses.map((status,i)=>({
            id:'01JSCREEN'+String(i).padStart(2,'0'),concept:'tables/orders',
            body:'Verify this relationship before publishing.',state:'open',
            row_token:'r2:screen'+i,ts:new Date(Date.now()-i*1000).toISOString(),
            anchor:{schema:2,kind:'text-quote',exact,prefix:'column ',suffix:' in the warehouse',heading:{path:['Schema','Columns']},block:{id:'paragraph:17',hash:'sha256:'+'a'.repeat(64),start:120,end:190},position:{start:127,end:158}},
            anchor_resolution:{status,reason:status==='resolved'?'unique_context':status==='stale'?'exact_not_found':status==='ambiguous'?'multiple_exact_candidates':'conflicting_evidence',candidate_count:status==='ambiguous'?2:0,concept_content_rev:'b'.repeat(64),resolver_version:2}
          }));
          window.okfLoomStudio.openPanel('comments');
        }""",
        statuses,
    )
    expect(page.get_by_role("dialog", name="Comments")).to_be_visible()


def _capture_viewport_media(page) -> dict:
    """Derive actual viewport and media from the live page."""
    return page.evaluate("""() => ({
        viewport: { width: window.innerWidth, height: window.innerHeight },
        media: {
            reduced_motion: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'reduce' : 'none',
            forced_colors: window.matchMedia('(forced-colors: active)').matches ? 'active' : 'none',
        }
    })""")


def _capture_with_metadata(page, name: str, staging: Path):
    """Capture screenshot into staging dir and return per-image metadata."""
    path = staging / name
    page.screenshot(path=path, full_page=False)
    vm = _capture_viewport_media(page)
    from okf_loom.capture_provenance import SCENARIO_CLASSIFICATIONS
    return {
        "file": name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "classification": SCENARIO_CLASSIFICATIONS.get(name, "release-proof"),
        "release_proof": True,
        "scenario_assertions": SCENARIO_ASSERTIONS[name],
        "viewport": vm["viewport"],
        "media": vm["media"],
    }


def unexpected_console_error(message) -> bool:
    if message.type != "error": return False
    text = message.text
    return not ("AbortError" in text or "Superseded" in text or
                ("Failed to load resource" in text and ("409" in text or "500" in text)))


def select_text(page, selector: str, text: str) -> None:
    page.locator(selector).first.evaluate(
        """(root, text) => { const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);let n;
        while((n=walker.nextNode())){const at=(n.nodeValue||'').indexOf(text);if(at>=0){const r=document.createRange();r.setStart(n,at);r.setEnd(n,at+text.length);const s=getSelection();s.removeAllRanges();s.addRange(r);document.dispatchEvent(new Event('selectionchange'));return true;}}throw new Error('capture text not found'); }""",
        text,
    )


# ---------------------------------------------------------------------------
# Resolved-mark scroll: detached-handle resilience (no sleeps)
# ---------------------------------------------------------------------------
# An async comment/doc reconciliation can replace the <mark> for a comment
# between the moment a Playwright locator is resolved and the moment it is
# scrolled/measured, raising "Element is not attached to DOM". The helpers
# below key every lookup by comment id and perform locate+measure+scroll+
# recheck inside ONE page.evaluate transaction (JS is single-threaded, so the
# mark cannot be replaced within the transaction). If the mark is missing or
# detached between the readiness gate and the transaction, the loop reacquires
# a FRESH locator while a bounded operation deadline remains; repeated churn
# or a permanently missing mark exhausts the budget and fails release.

# Bounded share of the capture budget for stabilizing the resolved mark.
_MARK_STABLE_DEADLINE_S = 8.0

_MARK_READINESS_JS = """
(cid) => {
    const s = window.okfLoomStudio && window.okfLoomStudio.state;
    if (!s || s.commentsStatus !== 'ready') return false;
    const doc = s.doc;
    if (!doc || !doc.annotation_projection || doc.annotation_projection.complete !== true) return false;
    const body = document.querySelector('.okf-page__body') || document;
    const sel = 'mark.okf-comment-mark[data-comment-id="' + CSS.escape(String(cid)) + '"]';
    const m = body.querySelector(sel);
    return !!(m && m.isConnected);
}
"""

# Single comment-id-keyed transaction: find the CURRENT mark, measure, scroll
# into view (nudging left if it would sit under the panel drawer), and
# re-measure — all atomically, so the mark is never a stale detached handle.
_MARK_TRANSACTION_JS = """
(args) => {
    const cid = String(args[0]); const panelWidth = args[1]; const vw = args[2];
    const s = window.okfLoomStudio && window.okfLoomStudio.state;
    const doc = s && s.doc;
    const body = document.querySelector('.okf-page__body') || document;
    const sel = 'mark.okf-comment-mark[data-comment-id="' + CSS.escape(cid) + '"]';
    const mark = body.querySelector(sel);
    if (!mark) return {status: 'missing'};
    if (!mark.isConnected) return {status: 'detached'};
    const measure = (el) => {
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return {
            visible: r.width > 0 && r.height > 0,
            top: Math.ceil(r.top), left: Math.ceil(r.left),
            right: Math.ceil(r.right), bottom: Math.ceil(r.bottom),
            hasBg: cs.backgroundColor !== 'rgba(0, 0, 0, 0)' && cs.backgroundColor !== 'transparent',
            hasBorderBottom: cs.borderBottomWidth !== '0px' || cs.borderBottomStyle !== 'none',
        };
    };
    const before = measure(mark);
    mark.scrollIntoView({block: 'center'});
    let after = measure(mark);
    if (after.right > vw - panelWidth) {
        mark.scrollIntoView({block: 'center', inline: 'start'});
        after = measure(mark);
    }
    const rev = doc && doc.concept_content_rev ? doc.concept_content_rev : null;
    const row = s && s.comments
        ? (s.comments.find(function (c) { return c.id === cid; }) || {}).row_token
        : null;
    return {status: 'ok', before: before, after: after, revision: rev, row_token: row};
}
"""


def _validate_mark_geometry(geom: dict, when: str) -> None:
    """Raise if the mark geometry/style is invalid (visible + styled required)."""
    if not geom["visible"]:
        raise RuntimeError(f"Resolved mark not visible{when}: {geom}")
    if not geom["hasBg"] and not geom["hasBorderBottom"]:
        raise RuntimeError(f"Resolved mark lacks visible styling{when}: {geom}")


def _wait_mark_readiness(page, comment_id, timeout_ms: int) -> None:
    """Semantic readiness gate (no sleeps): wait until comments are ready,
    the doc projection is complete, and the resolved mark for *comment_id* is
    present and attached.

    Playwright sync API passes the JS argument via the keyword-only ``arg``
    parameter (not ``argument``); a wrong keyword raises ``TypeError``, which
    the caller must never mistake for an unreachable readiness condition."""
    page.wait_for_function(_MARK_READINESS_JS, arg=comment_id, timeout=timeout_ms)


def _run_mark_transaction(page, comment_id, panel_width: int, vw: int) -> dict:
    """Execute ONE comment-id-keyed evaluate transaction. Returns a dict with
    ``status`` in {ok, missing, detached} (and geometry/revision when ok)."""
    return page.evaluate(_MARK_TRANSACTION_JS, [comment_id, panel_width, vw])


# ---------------------------------------------------------------------------
# Keyboard reachability: known preceding focus owner + one real Tab
# ---------------------------------------------------------------------------
# The wide-viewport comments surface is now a NON-MODAL rail: the re-anchor
# editor is no longer a focus trap, so a control in the rail sits AFTER many
# document-body focus owners. Two earlier strategies were proven UNSOUND by a
# live trace (diag_kb): (1) the old fixed ``range(40)`` Tab loop could not
# deterministically reach the Retry control from an arbitrary start
# (``isActive:false``); (2) a bounded full-ring Tab traversal failed at the
# forced-colors step because focus was carried ONTO the control from the prior
# mobile step, so a forward Tab had to complete a FULL document cycle to return
# — and the real native tab order is longer than any DOM-derived ring count
# (``aria-hidden`` / ``BODY`` tab stops are not enumerable by selector), so the
# ``count + 1`` bound was too small.
#
# The robust, deterministic fix is the task's option A: establish a KNOWN
# PRECEDING focus owner and Tab ONCE. The owner is the control's nearest
# preceding tabbable element in real DOM/tab order; the re-anchor error alert
# (``tabindex="-1"``) sits between that owner and the Retry control, so exactly
# one forward Tab lands on the control — in rail mode (native Tab, trap
# disarmed) AND in the narrow overlay (focus trap, where ``focusableIn`` also
# skips ``tabindex="-1"``). Because the control never starts focused, there is
# no full-cycle dependency, and no programmatic ``focus()`` is ever called on
# the control itself: the landing move is a genuine Tab keystroke, which is
# what activates ``:focus-visible`` in Chromium (programmatic focus suppresses
# the ring). The helper fails closed if the control is not tabbable, has no
# focusable preceding owner, or the single Tab does not reach it.

_KEYBOARD_OWNER_JS = """
(e) => {
    const FOCUSABLE = 'a[href], area[href], button, input, select, textarea, iframe,'
        + ' audio[controls], video[controls],'
        + ' details > summary:first-of-type, [tabindex]';
    const pass = (el, allowNegativeTabindex) => {
        if (!el || el.disabled) return false;
        if (el.tagName === 'INPUT' && el.type === 'hidden') return false;
        if (!el.matches(FOCUSABLE)) return false;
        const ti = el.getAttribute('tabindex');
        if (ti !== null && parseInt(ti, 10) < 0) return allowNegativeTabindex;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 && r.height <= 0) return false;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        // NOTE: aria-hidden is intentionally NOT excluded — Chrome keeps such
        // elements in the tab order, so excluding them would undercount and pick
        // a wrong predecessor (the root cause of the earlier bound failure).
        if (el.closest('[hidden]') || el.closest('[inert]')) return false;
        return true;
    };
    const tabbable = (el) => pass(el, false);
    const all = Array.prototype.slice.call(document.querySelectorAll(FOCUSABLE));
    const idx = all.indexOf(e);
    // Nearest preceding tabbable owner: one forward Tab from it reaches the
    // control (any tabindex=-1 nodes between, e.g. the re-anchor error alert,
    // are skipped by both native Tab and the narrow-overlay focus trap).
    let owner = null;
    for (let i = idx - 1; i >= 0; i--) {
        if (tabbable(all[i])) { owner = all[i]; break; }
    }
    let ownerKind = owner ? 'tabbable' : null;
    // Fallback: if the control is the first tabbable, use the nearest preceding
    // tabindex=-1 focusable element as the known owner (focusable, not a tab
    // stop, but Tab from it lands on the next tab stop = the control).
    if (!owner) {
        for (let i = idx - 1; i >= 0; i--) {
            if (pass(all[i], true) && all[i].getAttribute('tabindex') === '-1') {
                owner = all[i]; ownerKind = 'tabindex-1'; break;
            }
        }
    }
    let ownerFocused = false;
    if (owner) {
        try { owner.focus({preventScroll: true}); } catch (x) {}
        ownerFocused = document.activeElement === owner;
    }
    return {
        retryTabbable: idx >= 0 && tabbable(e),
        ownerFound: !!owner,
        ownerKind: ownerKind,
        ownerFocused: ownerFocused,
        ownerTag: owner ? owner.tagName : null,
        ownerText: owner ? (owner.textContent || '').trim().slice(0, 24) : null,
    };
}
"""


def _keyboard_reach_and_focus(page, control, *, label: str,
                              _setup_fn=None, _step_fn=None) -> None:
    """Prove *control* is keyboard-reachable and land real keyboard focus on it.

    Establishes a KNOWN PRECEDING focus owner (the control's nearest preceding
    tabbable element, located in real DOM/tab order) and presses Tab ONCE. One
    forward Tab is guaranteed to reach the control because the owner is its
    immediate tabbable predecessor and any ``tabindex="-1"`` node between them
    (e.g. the re-anchor error alert) is skipped by both native Tab and the
    narrow-overlay focus trap. This is deterministic regardless of whether the
    wide comments rail is in non-modal rail mode (native Tab) or narrow overlay
    mode (focus trap), and crucially does NOT depend on the control's prior
    focus state — so it cannot regress the way a fixed-Tab-budget or full-ring
    traversal does when focus is carried onto the control between captures.

    The landing move is a genuine Tab keystroke, which is what activates
    ``:focus-visible`` in Chromium — programmatic ``focus()`` on the control
    suppresses the ring and is never used on it (``focus()`` is used only on the
    PRECEDING owner to establish the known start, which the task explicitly
    permits).

    This is the KEYBOARD-REACHABILITY test only. Callers separately assert the
    CAPTURE FOCUS STYLING (``:focus-visible`` computed outline) they need, so the
    two concerns are never conflated. Fails closed (explicit ``RuntimeError``,
    safe under ``python -O``) if the control is not tabbable, has no focusable
    preceding owner, or the single Tab does not reach it.

    ``_setup_fn`` / ``_step_fn`` inject fakes for deterministic unit testing
    (no browser required)."""
    if _setup_fn is None:
        _setup_fn = lambda: control.evaluate(_KEYBOARD_OWNER_JS)
    if _step_fn is None:
        def _step_fn():
            page.keyboard.press("Tab")
            return control.evaluate("e => document.activeElement === e")
    info = _setup_fn()
    if not isinstance(info, dict) or not info.get("retryTabbable"):
        raise RuntimeError(
            f"{label}: control is not tabbable; cannot be keyboard-reached "
            f"({info})")
    if not info.get("ownerFound"):
        raise RuntimeError(
            f"{label}: no focusable element precedes the control in tab order; "
            f"cannot establish a known preceding focus owner ({info})")
    if not info.get("ownerFocused"):
        raise RuntimeError(
            f"{label}: preceding focus owner did not receive programmatic "
            f"focus ({info})")
    # One real Tab from the immediate tabbable predecessor lands keyboard focus
    # on the control (activating :focus-visible). Not reached => the tab order
    # between owner and control changed => fail closed.
    if not _step_fn():
        raise RuntimeError(
            f"{label}: control not keyboard-reached with one Tab from its "
            f"preceding focus owner ({info})")


def _scroll_resolved_mark_into_view(page, comment_id, *,
                                    deadline_s: float = _MARK_STABLE_DEADLINE_S,
                                    panel_width: int = 440, vw: int = 1280,
                                    readiness_timeout_ms: int = 2000,
                                    _readiness_fn=None, _transaction_fn=None):
    """Scroll the resolved comment mark for *comment_id* into the visible
    document viewport and verify its geometry/style, resilient to async
    reconciliation replacing the mark.

    Each attempt is gated by semantic readiness (``_wait_mark_readiness``),
    then locate+measure+scroll+recheck run in ONE comment-id-keyed
    ``page.evaluate`` transaction so the mark can never be a stale detached
    handle. If the mark is missing/detached between readiness and the
    transaction (reconciliation in flight), the loop reacquires a FRESH
    locator while the bounded deadline remains. Repeated churn or a
    permanently missing mark exhausts the budget and fails release
    explicitly. No sleeps. Returns the validated post-scroll geometry dict.

    ``_readiness_fn`` / ``_transaction_fn`` inject fakes for deterministic
    unit testing (no browser required)."""
    readiness = _readiness_fn or (lambda: _wait_mark_readiness(page, comment_id, readiness_timeout_ms))
    transaction = _transaction_fn or (lambda: _run_mark_transaction(page, comment_id, panel_width, vw))
    deadline = time.monotonic() + deadline_s
    last = None
    while time.monotonic() < deadline:
        try:
            readiness()
        except Exception as exc:  # readiness not reached within sub-budget
            last = "readiness:" + type(exc).__name__
            continue
        result = transaction()
        status = result.get("status") if isinstance(result, dict) else None
        if status == "ok":
            # Geometry/style checks preserved (initial + post-scroll recheck).
            _validate_mark_geometry(result["before"], "")
            if result["before"]["top"] < 0 or result["before"]["left"] < 0:
                raise RuntimeError(f"Resolved mark off-screen: {result['before']}")
            _validate_mark_geometry(result["after"], " after scroll")
            return result["after"]
        last = status or "non-dict"
        # missing/detached — reconciliation may be in flight; reacquire a
        # fresh locator (loop) while the deadline remains. No sleep.
    raise RuntimeError(
        f"resolved mark for comment {comment_id} never stabilized within "
        f"{deadline_s}s budget (last={last})")


# ---------------------------------------------------------------------------
# Atomic publication (r5-1): sibling-directory build + fsync + directory swap
#
# Obsolete .png artifacts are NOT cleaned out of OUTPUT before capture: that
# would mutate the published directory before capture and validation succeed
# (r5-1 defect).  Instead the whole OUTPUT directory is replaced atomically
# by the sibling swap below, so any stale extras vanish only on a successful
# commit.  An interrupted publish is recovered by _recover_interrupted_publish
# before any sibling is deleted, so the sole prior proof is never destroyed.
# ---------------------------------------------------------------------------

def _remove_tree_durable(path: Path) -> None:
    """Remove one real directory and durably persist its directory entry.

    Missing paths are idempotent; symlinks, files, and special entries are
    rejected rather than followed or silently left behind.  Every real removal
    is fail-visible and followed by a parent-directory fsync.
    """
    try:
        st = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"refusing to remove non-directory publication state: {path}")
    shutil.rmtree(str(path))
    _fsync_dir(path.parent)


# Kept as an internal compatibility name for existing test/maintenance callers;
# unlike the former helper this is fail-visible and durable.
_safe_rmtree = _remove_tree_durable


def _remove_tree_at(dir_fd: int) -> None:
    """Descriptor-relatively remove every entry under the directory open as
    *dir_fd* (r12 finding 7).

    Unlike pathname-based ``shutil.rmtree``, no entry is removed by a fresh
    pathname lookup that could be redirected by a replacement: each child is
    re-stat'd, classified, and removed relative to the pinned descriptor (or a
    child descriptor pinned from it).  Symlinks and special files are rejected
    rather than followed.  The directory itself is NOT removed here -- the
    caller unlinks its (then empty) name in the parent once this has emptied
    it.  Every removal is therefore bound to the inode open as *dir_fd*,
    independent of any name substitution.
    """
    with os.scandir(dir_fd) as scan:
        entries = list(scan)
    for entry in entries:
        name = entry.name
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                               | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
            try:
                _remove_tree_at(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=dir_fd)
        elif stat.S_ISREG(st.st_mode):
            os.unlink(name, dir_fd=dir_fd)
        else:
            raise RuntimeError(
                f"refusing descriptor-relative removal of non-regular/"
                f"non-directory entry: {name}")


def _validate_proof_directory_at(dir_fd: int, label) -> None:
    """Fail closed unless the directory open as *dir_fd* is one complete,
    self-consistent proof directory.

    Descriptor-relative: every leaf is opened relative to *dir_fd* with
    ``O_NOFOLLOW`` and read under a before/after ``fstat`` identity check, so
    the validated bytes are bound to the descriptor's pinned inode.  The
    caller owns *dir_fd* (it is NOT closed here).  *label* is used only for
    diagnostic messages.

    r11: a descriptor-relative validator lets recovery revalidate the
    complete content of a pinned candidate immediately before publication,
    binding the result to the retained inode instead of a fresh pathname
    lookup that could resolve to a replacement.
    """
    expected = set(EXPECTED_PNG_FILES) | {"manifest.json"}
    with os.scandir(dir_fd) as scan:
        actual = {entry.name for entry in scan}
    if actual != expected:
        raise RuntimeError(
            f"incomplete publication proof {label}: missing={expected-actual} extra={actual-expected}")

    def secure_bytes(name: str) -> bytes:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise RuntimeError(f"publication proof contains non-regular entry: {label / name}")
            chunks = []
            while chunk := os.read(fd, 1024 * 1024):
                chunks.append(chunk)
            after = os.fstat(fd)
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                raise RuntimeError(f"publication artifact changed while reading: {label / name}")
            return b"".join(chunks)
        finally:
            os.close(fd)

    try:
        manifest = json.loads(secure_bytes("manifest.json").decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"invalid publication manifest in {label}: {exc}") from exc
    records = {}
    for value in manifest.values() if isinstance(manifest, dict) else ():
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, dict) and "file" in entry and "sha256" in entry:
                    records[entry["file"]] = entry["sha256"]
    if set(records) != set(EXPECTED_PNG_FILES):
        raise RuntimeError(f"publication manifest has incomplete artifact membership: {label}")
    for name, expected_hash in records.items():
        digest = hashlib.sha256(secure_bytes(name)).hexdigest()
        if digest != expected_hash:
            raise RuntimeError(f"publication artifact hash mismatch: {label / name}")


def _validate_proof_directory(path: Path) -> None:
    """Fail closed unless *path* is one complete, self-consistent proof dir."""
    try:
        dir_fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RuntimeError(f"publication proof is not a real directory: {path}") from exc
    try:
        _validate_proof_directory_at(dir_fd, path)
    finally:
        os.close(dir_fd)


def _validate_and_pin_leaves(dir_fd: int, label) -> list:
    """Validate the complete content of the directory open as *dir_fd* AND
    retain an open descriptor for every leaf (r12 finding 8).

    Returns a list of ``(name, fd, dev, ino, expected_hash)`` records -- one
    per leaf including ``manifest.json`` -- with each descriptor pinned to the
    inode that was validated.  The caller closes the descriptors (typically
    via ``_recheck_pinned_leaves``) after the publication rename so leaf
    identity and content can be re-verified across the rename.  On any failure
    every descriptor opened so far is closed before re-raising.
    """
    expected = set(EXPECTED_PNG_FILES) | {"manifest.json"}
    with os.scandir(dir_fd) as scan:
        actual = {entry.name for entry in scan}
    if actual != expected:
        raise RuntimeError(
            f"incomplete publication proof {label}: missing={expected-actual} extra={actual-expected}")

    leaves: list = []

    def _read_open(name: str):
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            os.close(fd)
            raise RuntimeError(f"publication proof contains non-regular entry: {label / name}")
        chunks = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            os.close(fd)
            raise RuntimeError(f"publication artifact changed while reading: {label / name}")
        return fd, before.st_dev, before.st_ino, b"".join(chunks)

    try:
        mfd, mdev, mino, mbytes = _read_open("manifest.json")
        leaves.append(("manifest.json", mfd, mdev, mino,
                       hashlib.sha256(mbytes).hexdigest()))
        try:
            manifest = json.loads(mbytes.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"invalid publication manifest in {label}: {exc}") from exc
        records = {}
        for value in manifest.values() if isinstance(manifest, dict) else ():
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict) and "file" in entry and "sha256" in entry:
                        records[entry["file"]] = entry["sha256"]
        if set(records) != set(EXPECTED_PNG_FILES):
            raise RuntimeError(f"publication manifest has incomplete artifact membership: {label}")
        for name, expected_hash in records.items():
            fd, dev, ino, data = _read_open(name)
            if hashlib.sha256(data).hexdigest() != expected_hash:
                raise RuntimeError(f"publication artifact hash mismatch: {label / name}")
            leaves.append((name, fd, dev, ino, expected_hash))
        return leaves
    except BaseException:
        for _, fd, _, _, _ in leaves:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _recheck_pinned_leaves(leaves: list, dir_fd: int, label) -> None:
    """Re-verify every retained leaf descriptor across a publication rename
    (r12 finding 8).

    For each retained leaf the descriptor inode must be unchanged, the leaf
    NAME (resolved relative to *dir_fd*, the renamed directory) must still
    point at that same inode, and the re-read content must hash to the value
    captured at validation.  This binds the validated candidate bytes through
    the rename: neither an in-place content mutation nor an unlink+replace of
    a leaf can be published undetected.  All descriptors are closed here.
    """
    try:
        for name, fd, dev, ino, expected_hash in leaves:
            cur = os.fstat(fd)
            if (cur.st_dev, cur.st_ino) != (dev, ino):
                raise RuntimeError(
                    f"retained leaf descriptor identity changed across rename: {label / name}")
            try:
                named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"retained leaf disappeared across rename: {label / name}") from exc
            if (named.st_dev, named.st_ino) != (dev, ino):
                raise RuntimeError(
                    f"leaf replaced across rename: name {name} resolves to a "
                    f"different inode than the validated leaf")
            os.lseek(fd, 0, os.SEEK_SET)
            before = os.fstat(fd)
            chunks = []
            while chunk := os.read(fd, 1024 * 1024):
                chunks.append(chunk)
            after = os.fstat(fd)
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                raise RuntimeError(
                    f"retained leaf changed while re-reading across rename: {label / name}")
            if hashlib.sha256(b"".join(chunks)).hexdigest() != expected_hash:
                raise RuntimeError(
                    f"publication artifact hash mismatch across rename: {label / name}")
    finally:
        for _, fd, _, _, _ in leaves:
            try:
                os.close(fd)
            except OSError:
                pass


def _close_leaf_state(leaves: list) -> None:
    """Close every retained leaf descriptor in a leaf state (r12 finding 8).

    Used on restore error paths that raise before ``_recheck_pinned_leaves``
    runs, so descriptors opened by ``_validate_and_pin_leaves`` never leak.
    Idempotent against already-closed descriptors.
    """
    for _, fd, _, _, _ in leaves:
        try:
            os.close(fd)
        except OSError:
            pass


def _fsync_file(path: Path) -> None:
    """fsync a single regular file for on-disk durability."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# Directory fsync is supported on real filesystems (ext4/xfs) but returns
# EINVAL on tmpfs and ENOSYS/EPERM on some platform/fs combinations.  Only
# those explicitly enumerated "unsupported platform" errnos are tolerated;
# every other OSError (real I/O failure, ENOSPC, EIO, ...) fails closed so a
# durability failure can never be silently swallowed.
_UNSUPPORTED_DIR_FSYNC_ERRNO: frozenset[int] = frozenset({
    errno.EINVAL, errno.ENOSYS, errno.EPERM,
})


def _fsync_dir(path: Path) -> None:
    """fsync a directory so its entries are persisted to disk.

    Fail closed: an open or fsync ``OSError`` raises ``RuntimeError`` EXCEPT
    for the explicitly enumerated unsupported-platform errnos (tmpfs EINVAL
    etc.) which are tolerated so publication still proceeds on such
    filesystems.  Restoration/rename parent fsyncs use this same mandatory,
    fail-closed path.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError as exc:
        raise RuntimeError(f"fsync: failed to open directory {path}: {exc}") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_DIR_FSYNC_ERRNO:
            raise RuntimeError(
                f"fsync: directory sync failed for {path}: {exc}") from exc
        # explicitly enumerated unsupported-platform case — tolerated
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# r6-1 / r7-M1 / r7-M2: Cross-process publication lock + atomic exchange
#
# The r6 finding: the two-rename swap (output → .publish-old, .publish-new →
# output) leaves a window where output does not exist.  Concurrent readers can
# observe missing proof, and concurrent capture processes using fixed sibling
# names can interpret or remove each other's active state.
#
# Fix: serialize the entire recovery/publication protocol with a cross-process
# ``fcntl.flock`` on a dedicated lock file (r6), and eliminate the
# missing-OUTPUT window on Linux by using ``renameat2(RENAME_EXCHANGE)`` to
# atomically swap sibling_new and output in a single syscall (r6).
#
# r7-M1: startup recovery (``_entry_recover_and_prepare``) now runs UNDER THE
# SAME lock, so a production restart cannot touch another publisher's active
# sibling state mid-swap.
#
# r7-M2: the two-rename fallback is NOT atomic for lock-unaware readers — a
# static server, release collector, validator, or any reader that does not
# take the flock can observe OUTPUT absent between the two renames.  Release
# publication therefore REQUIRES ``renameat2(RENAME_EXCHANGE)`` to replace an
# existing OUTPUT.  If exchange is unavailable (non-Linux, kernel < 3.15, or a
# filesystem such as tmpfs that rejects RENAME_EXCHANGE) the publish FAILS
# CLOSED rather than silently publishing a non-atomic replacement that no
# ordinary reader can safely observe.  The first-run case (no prior OUTPUT) is
# still a single rename with no gap, so it remains correct on every platform.
#
# r7-M3: after a successful exchange, sibling_new holds the OLD proof.  If the
# parent-directory fsync that persists the exchange boundary fails, the OLD
# rollback content is PRESERVED (not deleted) until the exchange is durably
# resolved; the publish fails closed with the rollback copy left in place.
# ---------------------------------------------------------------------------

# Lock file lives next to the output directory so all capture processes
# targeting the same output agree on a single lock.
_PUBLICATION_LOCK_TIMEOUT_S = 300  # max wait for another publisher to finish


@contextmanager
def _publication_lock(output: Path, *, timeout_s: float = _PUBLICATION_LOCK_TIMEOUT_S):
    """Acquire an exclusive cross-process ``flock`` for publication.

    The lock file is ``<output>.publish-lock`` in the output's parent
    directory.  It is created (``O_CREAT``) if missing and locked with
    ``LOCK_EX`` (blocking).  The lock is released when the context manager
    exits.  The lock file itself is NOT removed — it persists for the next
    publisher, which is correct (a stale lock file with no lock held is
    harmless).

    This serializes concurrent capture processes so they cannot interpret or
    remove each other's ``.publish-old`` / ``.publish-new`` sibling state.
    """
    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = parent / (output.name + ".publish-lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        # Blocking lock with a deadline.  fcntl.flock with LOCK_EX blocks
        # until the lock is acquired.  We poll with LOCK_EX | LOCK_NB and a
        # timeout so the caller is not stuck forever if another process
        # deadlocks.
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError) as exc:
                if exc.errno != errno.EAGAIN:
                    raise
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"publication lock timed out after {timeout_s}s "
                        f"(another capture process may be stuck); "
                        f"lock file: {lock_path}"
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# --- Linux renameat2(RENAME_EXCHANGE) ---------------------------------------

_RENAME_EXCHANGE = 2  # renameat2 flag: atomically swap two paths
_AT_FDCWD = -100  # AT_FDCWD for relative paths in renameat2

# Lazily loaded libc handle for renameat2.
_libc = None
_renameat2_fn = None
_renameat2_checked = False


def _load_renameat2():
    """Lazily load ``renameat2`` from libc.  Returns the callable or None."""
    global _libc, _renameat2_fn, _renameat2_checked
    if _renameat2_checked:
        return _renameat2_fn
    _renameat2_checked = True
    try:
        lib_path = ctypes.util.find_library("c")
        if lib_path is None:
            return None
        _libc = ctypes.CDLL(lib_path, use_errno=True)
        # renameat2 may not exist on older libc; hasattr check via getattr.
        fn = getattr(_libc, "renameat2", None)
        if fn is None:
            return None
        fn.argtypes = [
            ctypes.c_int, ctypes.c_char_p,
            ctypes.c_int, ctypes.c_char_p,
            ctypes.c_uint,
        ]
        fn.restype = ctypes.c_int
        _renameat2_fn = fn
        return fn
    except (OSError, AttributeError):
        return None


def _try_renameat2_exchange(path_a: Path, path_b: Path) -> bool:
    """Atomically exchange two directory paths via ``renameat2(RENAME_EXCHANGE)``.

    Returns ``True`` if the exchange succeeded.  Returns ``False`` if
    ``renameat2`` is not available on this platform (non-Linux, older kernel,
    or older libc) so the caller can fall back to the two-rename approach.

    Both paths must exist and be on the same filesystem.  The exchange is a
    single atomic syscall — there is NO window where either path is missing,
    so concurrent readers always observe a valid directory at each path.
    """
    fn = _load_renameat2()
    if fn is None:
        return False
    a_bytes = str(path_a).encode("utf-8")
    b_bytes = str(path_b).encode("utf-8")
    result = fn(_AT_FDCWD, a_bytes, _AT_FDCWD, b_bytes, _RENAME_EXCHANGE)
    if result == 0:
        return True
    err = ctypes.get_errno()
    # ENOSYS = kernel does not support renameat2; EINVAL = filesystem doesn't
    # support it (e.g. tmpfs on some kernels).  Both are "not available" →
    # fall back.  Any other errno is a real error.
    if err in (errno.ENOSYS, errno.EINVAL):
        return False
    raise OSError(err, os.strerror(err), str(path_a))


def _recovery_pre_mutation_hook(parent_fd: int, name: str, role: str) -> None:
    """Test/audit seam invoked immediately before a recovery filesystem
    mutation, AFTER the pinned identity has been verified and the content
    revalidated.  Production is a no-op.  Tests monkeypatch it to inject a
    directory replacement or an in-place content mutation at the exact
    check-then-mutate boundary so the fail-closed identity/revalidation
    barriers can be exercised deterministically.

    r11: this seam exists ONLY to make the check-then-mutate race (the r11
    Major finding) deterministically testable; it performs no work in
    production and never inspects *parent_fd*/*name*.
    """
    return None


def _recover_interrupted_publish(output: Path) -> list[str]:
    """Recover an interrupted publish *before* deleting any sibling state.

    Inspects the sibling ``.publish-old`` / ``.publish-new`` directories left
    by a previously crashed publish and restores a single consistent state
    WITHOUT ever deleting the sole prior proof.  Returns a list of human-
    readable recovery actions (possibly empty).  Restoration failures raise
    ``RuntimeError`` (never swallowed) so data loss is surfaced loudly.

    States handled (``output`` = published dir, ``old`` = ``.publish-old``,
    ``new`` = ``.publish-new``):

    * ``output`` present — it is the valid current proof.  Any siblings are
      redundant leftovers from a publish that succeeded the swap but did not
      finish cleanup → remove them.
    * ``output`` absent, ``old`` present — the first swap rename
      (``output → old``) completed but the second (``new → output``) did not.
      ``old`` holds the SOLE prior proof → RESTORE ``old → output`` (a move,
      never a delete).  A leftover ``new`` is removed afterwards.
    * ``output`` absent, ``old`` absent, ``new`` present — a first-ever
      publish crashed before the first rename (no prior proof ever existed)
      → remove ``new``.
    * all absent — clean; nothing to do.

    This makes restart recovery safe at every swap phase: the sole prior
    proof is moved back into place rather than discarded.

    r11: candidate authority is retained through mutation.  Each present
    candidate is pinned by a descriptor opened at enumeration and held until
    after every mutation, so identity cannot drift between the check and the
    mutate.  Destructive cleanup never removes a candidate by a fresh
    pathname lookup: it quarantines the leftover by a rename under the pinned
    parent and destroys the quarantine ONLY after verifying it holds exactly
    the pinned inode (a replacement or collision fails closed and leaves the
    quarantine in place for manual inspection).  The sole-prior-proof restore
    revalidates the complete content through the pinned descriptor
    immediately before the rename and verifies the restored name holds the
    pinned inode, so neither a name replacement nor an in-place PNG/manifest
    mutation can be published.

    r12: the quarantine/destroy split is hardened end-to-end.  (finding 6)
    Before destroying any quarantined sibling, recovery quarantines it
    WITHOUT destruction, then immediately descriptor-revalidates the
    authoritative output, and destroys the sibling only once the authority is
    proven current and complete -- so in-place output corruption after the
    initial validation can never cause the only valid sibling to be deleted.
    (finding 7) Quarantine destruction no longer falls back to a pathname
    ``shutil.rmtree``: the directory contents are removed descriptor-relatively
    via the candidate's UNREPLACEABLE pinned descriptor, and only the
    then-empty directory entry is removed by name, so a quarantine-name
    replacement cannot redirect destruction.  (finding 8) The restore path
    validates the complete content while retaining an open descriptor for
    every leaf and rechecks each leaf's inode identity and content hash across
    the rename, so a leaf cannot mutate in place (or be replaced) between
    validation and publication undetected.
    """
    parent = output.parent
    sibling_new = parent / (output.name + ".publish-new")
    sibling_old = parent / (output.name + ".publish-old")
    actions: list[str] = []

    paths = (output, sibling_old, sibling_new)
    parent_fd = os.open(str(parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0))

    def identity(st):
        return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))

    # r11: Pin each present candidate by a descriptor opened relative to the
    # pinned parent at enumeration time, and hold every pin until after all
    # mutations.  The descriptor binds to the inode observed at enumeration
    # and retains authority over it: a name that is later replaced no longer
    # matches the pin, which the barriers below detect and reject.
    present: list[Path] = []
    pins: dict[Path, int] = {}
    identities: dict[Path, tuple] = {}

    def name_identity(candidate: Path) -> tuple:
        return identity(os.stat(candidate.name, dir_fd=parent_fd, follow_symlinks=False))

    def assert_name_matches_pin(candidate: Path, when: str) -> None:
        try:
            current = name_identity(candidate)
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"recovery candidate disappeared {when}: {candidate.name}") from exc
        if current != identities[candidate]:
            raise RuntimeError(
                f"recovery candidate identity changed {when}: "
                f"replacement/mutation/collision detected for {candidate.name}")

    def revalidate_content(candidate: Path) -> None:
        # r11/r12: immediate complete-content revalidation through the PINNED
        # descriptor so the validated bytes are bound to the retained inode
        # and cannot have drifted after the path-based validation closed its
        # own descriptor.
        _validate_proof_directory_at(pins[candidate], candidate)

    def quarantine_candidate(candidate: Path, label: str) -> Path:
        # r12 finding 6/7: rename the candidate to a unique quarantine name
        # under the pinned parent and verify the quarantine holds exactly the
        # pinned inode, WITHOUT destroying it.  The candidate is merely moved
        # aside so a later authoritative-output revalidation can fail closed
        # without losing the sibling.  Destruction is a separate step that
        # uses the unreplaceable pinned descriptor.
        quarantine = parent / (
            output.name + f".publish-{label}.quarantine-{os.getpid()}")
        try:
            os.stat(quarantine.name, dir_fd=parent_fd, follow_symlinks=False)
            raise RuntimeError(
                f"recovery quarantine collision detected: {quarantine.name} "
                f"already exists under the pinned parent")
        except FileNotFoundError:
            pass
        _recovery_pre_mutation_hook(parent_fd, candidate.name, "quarantine-" + label)
        os.rename(candidate.name, quarantine.name,
                  src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        try:
            q_identity = identity(
                os.stat(quarantine.name, dir_fd=parent_fd, follow_symlinks=False))
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"recovery quarantine disappeared after rename: "
                f"{quarantine.name}") from exc
        if q_identity != identities[candidate]:
            raise RuntimeError(
                f"recovery quarantine identity mismatch for "
                f"{candidate.name}: the quarantined directory is not the "
                f"pinned candidate (replacement/mutation/collision "
                f"detected). The quarantine is left at {quarantine.name} "
                f"for manual inspection.")
        return quarantine

    def destroy_quarantined_via_pin(candidate: Path, quarantine: Path, label: str) -> None:
        # r12 finding 7: destroy the quarantined candidate via its
        # UNREPLACEABLE pinned descriptor -- _remove_tree_at empties the
        # directory relative to the pin, which refers to the verified inode
        # regardless of any later name substitution.  Only the (then-empty)
        # directory entry is removed by name; if the name was replaced after
        # the post-rename identity check, rmdir fails closed and the empty
        # pinned inode is reclaimed when the pin closes.  Destruction can
        # never be redirected to a replacement directory.
        _recovery_pre_mutation_hook(parent_fd, quarantine.name, "destroy-" + label)
        _remove_tree_at(pins[candidate])
        try:
            os.rmdir(quarantine.name, dir_fd=parent_fd)
        except OSError as exc:
            raise RuntimeError(
                f"recovery failed to remove quarantined directory entry "
                f"{quarantine.name} after descriptor-relative emptying "
                f"(name may have been replaced): {exc}") from exc
        _fsync_dir(parent)

    try:
        for candidate in paths:
            try:
                fd = os.open(candidate.name,
                             os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                             | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                # The name exists under the pinned parent but is not a real
                # directory (e.g. a regular file or a symlink).  Fail closed
                # rather than silently skip — a non-directory candidate is a
                # corrupt recovery state, never a valid proof.
                raise RuntimeError(
                    f"publication proof is not a real directory: {candidate}") from exc
            try:
                st = os.fstat(fd)
                if not stat.S_ISDIR(st.st_mode):
                    raise RuntimeError(
                        f"recovery candidate is not a directory: {candidate.name}")
            except BaseException:
                os.close(fd)
                raise
            present.append(candidate)
            pins[candidate] = fd
            identities[candidate] = identity(st)

        # Validate the complete observed recovery state before the first
        # rename or removal.  A corrupt later sibling must never be discovered
        # after an earlier sibling has already been destroyed.
        for candidate in present:
            _validate_proof_directory(candidate)

        if output in present:
            # output is the valid current proof; siblings are redundant
            # leftovers from a publish that finished the swap but not cleanup.
            # r12 finding 6: quarantine each sibling WITHOUT destruction, then
            # immediately descriptor-revalidate the authoritative output, and
            # only destroy the sibling once the authority is proven current
            # and complete.  An in-place corruption of output after the
            # initial validation can never cause the only valid sibling to be
            # destroyed.
            for sibling, label in ((sibling_old, "old"), (sibling_new, "new")):
                if sibling not in present:
                    continue
                assert_name_matches_pin(sibling, "before quarantine " + label)
                quarantine = quarantine_candidate(sibling, label)
                assert_name_matches_pin(output, "before output revalidation " + label)
                _recovery_pre_mutation_hook(parent_fd, output.name, "revalidate-output-" + label)
                revalidate_content(output)
                destroy_quarantined_via_pin(sibling, quarantine, label)
                actions.append("removed leftover .publish-" + label + " (output present)")
            return actions

        # output absent.
        if sibling_old in present:
            # SOLE prior proof — restore it by MOVE (never delete).
            assert_name_matches_pin(sibling_old, "before restore revalidation")
            _recovery_pre_mutation_hook(parent_fd, sibling_old.name, "restore")
            # r12 finding 8: validate the complete content AND retain an open
            # descriptor for every leaf so leaf identity+hash can be rechecked
            # across the rename.  The directory inode is pinned, but a leaf
            # file could otherwise mutate in place between validation and
            # publication.
            leaf_state = _validate_and_pin_leaves(pins[sibling_old], sibling_old)
            try:
                _recovery_pre_mutation_hook(parent_fd, sibling_old.name, "restore-rename")
                assert_name_matches_pin(sibling_old, "before restore rename")
                try:
                    os.rename(sibling_old.name, output.name,
                              src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                except (OSError, TypeError) as exc:
                    raise RuntimeError(
                        f"FAILED to restore sole prior proof .publish-old -> "
                        f"{output.name}: {exc}"
                    ) from exc
                try:
                    restored = name_identity(output)
                except FileNotFoundError as exc:
                    raise RuntimeError(
                        f"restored output disappeared after rename: "
                        f"{output.name}") from exc
                if restored != identities[sibling_old]:
                    raise RuntimeError(
                        f"recovery restore identity mismatch: output does not "
                        f"hold the pinned prior proof (replacement/mutation/"
                        f"collision detected): {output.name}")
            except BaseException:
                # Any failure before the recheck must not leak the retained
                # leaf descriptors; _recheck_pinned_leaves closes them itself.
                _close_leaf_state(leaf_state)
                raise
            # r12 finding 8: recheck every retained leaf across the rename --
            # the validated bytes are now bound through publication.
            _recheck_pinned_leaves(leaf_state, pins[sibling_old], output)
            # r6-minor: fsync the parent directory so the restoration rename
            # is durable.  Without this, a second crash can lose a restoration
            # that was reported as successful.
            _fsync_dir(parent)
            actions.append("restored sole prior proof .publish-old -> output")
            if sibling_new in present:
                # r12 finding 6 (restore variant): output was absent at
                # enumeration, so the authoritative restored proof is the
                # sibling_old inode (pinned as pins[sibling_old]) now living
                # at the output name.  Before destroying the leftover new,
                # confirm output's name still holds that inode and
                # descriptor-revalidate it through the retained pin.
                try:
                    out_now = identity(
                        os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False))
                except FileNotFoundError as exc:
                    raise RuntimeError(
                        f"restored output disappeared before new cleanup: "
                        f"{output.name}") from exc
                if out_now != identities[sibling_old]:
                    raise RuntimeError(
                        f"restored output identity changed before new "
                        f"cleanup (replacement/mutation/collision detected): "
                        f"{output.name}")
                assert_name_matches_pin(sibling_new, "before quarantine new after restore")
                quarantine = quarantine_candidate(sibling_new, "new")
                _recovery_pre_mutation_hook(parent_fd, output.name, "revalidate-output-new")
                _validate_proof_directory_at(pins[sibling_old], output)
                destroy_quarantined_via_pin(sibling_new, quarantine, "new")
                actions.append("removed leftover .publish-new after restore")
            return actions

        # output absent and old absent.
        if sibling_new in present:
            assert_name_matches_pin(sibling_new, "before quarantine new (no prior)")
            quarantine = quarantine_candidate(sibling_new, "new")
            destroy_quarantined_via_pin(sibling_new, quarantine, "new")
            actions.append("removed leftover .publish-new (no prior proof existed)")
        return actions
    finally:
        for fd in pins.values():
            try:
                os.close(fd)
            except OSError:
                pass
        os.close(parent_fd)


def _atomic_publish(staging: Path, output: Path, manifest: dict,
                    entries: list, expected_artifacts: dict, root: Path) -> None:
    """Publish *staging* contents to *output* via an atomic sibling swap.

    The complete publication set (all PNGs + manifest) is copied into a fresh
    sibling directory next to *output*, fsynced, validated as a self-contained
    unit, then swapped into place atomically.  The existing *output* is never
    mutated before the swap.  At every failure point — copy, fsync,
    validation, or either rename — the prior *output* bytes are preserved and
    the sibling directories are cleaned up.

    r6-1: Publication is serialized by a cross-process ``fcntl.flock`` so
    concurrent capture processes cannot interpret or remove each other's
    ``.publish-old`` / ``.publish-new`` sibling state.  On Linux the swap
    uses ``renameat2(RENAME_EXCHANGE)`` — a single atomic syscall that
    exchanges ``sibling_new`` and ``output`` with NO window where ``output``
    is missing.

    r7-M2: the two-rename fallback is NOT atomic for lock-unaware readers
    (static servers, release collectors, validators — any reader that does
    not take the flock can observe ``output`` absent between the renames).
    Release publication therefore REQUIRES ``renameat2(RENAME_EXCHANGE)`` to
    replace an existing OUTPUT.  If exchange is unavailable, the publish
    FAILS CLOSED rather than silently publishing a non-atomic replacement.
    The first-run case (no prior OUTPUT) is a single rename with no gap.

    r7-M3: after a successful exchange, ``sibling_new`` holds the OLD proof.
    If the parent-directory fsync that persists the exchange boundary then
    fails, the OLD rollback content in ``sibling_new`` is PRESERVED (not
    deleted) until the exchange is durably resolved, and the publish fails
    closed with the rollback copy left in place.

    Publication protocol:
      1. Acquire cross-process publication lock.
      2. Recover any interrupted publish via ``_recover_interrupted_publish``
         (moves the sole prior proof back if needed; never deletes it).
      3. Build ``sibling_new`` with all files + manifest.
      4. fsync every file and the directory.
      5. Validate ``sibling_new`` as a complete unit: exact file set
         (expected PNGs + manifest.json only), manifest schema, and per-file
         SHA-256.
      6. Publish:
         - If output exists: REQUIRE ``renameat2(RENAME_EXCHANGE)`` to swap
           ``sibling_new`` ↔ ``output`` atomically (no missing-output window);
           fail closed if exchange is unavailable.  After exchange, fsync the
           parent to persist the exchange boundary.  If that fsync fails,
           PRESERVE ``sibling_new`` (it holds the OLD rollback content) and
           fail closed.  Otherwise clean up ``sibling_new``.
         - If output does not exist (first run): rename
           ``sibling_new → output`` (no swap needed, no window) and fsync
           parent.
      7. On success remove the old-content sibling.
    """
    parent = output.parent
    sibling_new = parent / (output.name + ".publish-new")
    sibling_old = parent / (output.name + ".publish-old")

    with _publication_lock(output):
        # 1. Recover an interrupted publish BEFORE deleting anything.
        _recover_interrupted_publish(output)

        # r7-M3: when set, sibling_new holds the irreplaceable OLD rollback
        # content after a successful exchange whose parent-fsync failed.  It
        # must NOT be cleaned up — preserve it for manual rollback until the
        # exchange boundary is durably resolved.  Initialized here (before the
        # try) so a pre-exchange failure still has a well-defined value.
        rollback_preserved = False
        try:
            # 2. Build complete sibling directory (same filesystem as output
            #    so os.rename / renameat2 is atomic).
            sibling_new.mkdir(parents=True)
            for png in sorted(staging.glob("*.png")):
                shutil.copy2(str(png), str(sibling_new / png.name))
            staging_manifest = sibling_new / "manifest.json"
            staging_manifest.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            # 3. Pin the publication directory once.  All authority checks
            # below are descriptor-relative and leaf opens are no-follow.
            publication_fd = os.open(
                str(sibling_new), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0))
            try:
                for row in sorted(os.scandir(publication_fd), key=lambda item: item.name):
                    fd = os.open(row.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                                 dir_fd=publication_fd)
                    try:
                        if not stat.S_ISREG(os.fstat(fd).st_mode):
                            raise RuntimeError(f"non-regular publication leaf: {row.name}")
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                os.fsync(publication_fd)

            # 4. Validate sibling_new as a complete self-contained unit.
            #    (a) Exact file set — expected PNGs + manifest.json, nothing else.
                actual_set = {row.name for row in os.scandir(publication_fd)}
                expected_set = set(EXPECTED_PNG_FILES) | {"manifest.json"}
                if actual_set != expected_set:
                    missing = expected_set - actual_set
                    extra = actual_set - expected_set
                    raise RuntimeError(
                        f"sibling file set mismatch: missing={missing} extra={extra}")
            #    (b) Manifest schema/inventory/classification (no on-disk hash
            #        check here — sibling is self-contained; per-file hashes below).
                from okf_loom.capture_provenance import _hash_regular_at, validate_manifest
                errors = validate_manifest(
                    manifest, root, expected_artifacts,
                    check_files=False, phase="capture",
                )
            #    (c) Per-file SHA-256 against the sibling bytes.
                for entry in entries:
                    try:
                        digest, _ = _hash_regular_at(publication_fd, entry["file"])
                    except (OSError, ValueError) as exc:
                        errors.append(f"{entry['file']} secure read failed: {exc}")
                    else:
                        if digest != entry["sha256"]:
                            errors.append(f"{entry['file']} hash mismatch in sibling")
                try:
                    _, manifest_bytes = _hash_regular_at(publication_fd, "manifest.json")
                    if json.loads(manifest_bytes) != manifest:
                        errors.append("manifest bytes differ from publication authority")
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    errors.append(f"manifest secure read failed: {exc}")
                if errors:
                    raise RuntimeError("Manifest validation failed: " + "; ".join(errors))
            finally:
                os.close(publication_fd)
            # Preserve the existing fail-visible durability abstraction (and
            # its platform-specific unsupported-errno policy) after the pinned
            # descriptor checks have completed.
            _fsync_dir(sibling_new)

            # 5. Atomic publication — no missing-OUTPUT window.
            output_existed = output.exists()
            if not output_existed:
                # First run: no prior output to swap — a single rename has no
                # missing-output window and is correct on every platform.
                os.rename(str(sibling_new), str(output))
                _fsync_dir(parent)
            else:
                # r7-M2: release publication MUST be atomic for ordinary
                # (lock-unaware) readers.  renameat2(RENAME_EXCHANGE) swaps
                # sibling_new <-> output in one syscall — no window where
                # output is missing.  The two-rename fallback is NOT atomic
                # (a static reader can observe output absent between renames),
                # so it is no longer used for release publication: if exchange
                # is unavailable we fail closed instead.
                exchanged = _try_renameat2_exchange(sibling_new, output)
                if not exchanged:
                    raise RuntimeError(
                        "atomic directory exchange unavailable "
                        "(renameat2(RENAME_EXCHANGE) is not supported on this "
                        "platform/filesystem); cannot atomically replace the "
                        "existing output. Release publication refuses to use "
                        "the non-atomic two-rename fallback because lock-unaware "
                        "readers (static servers, release collectors, "
                        "validators) can observe output missing between renames."
                    )
                # Exchange succeeded: sibling_new now holds the OLD content.
                try:
                    _fsync_dir(parent)  # persist the exchange boundary
                except Exception as exc:
                    # r7-M3: exchange done but the durability boundary is not
                    # synced.  sibling_new holds the OLD rollback content —
                    # PRESERVE it (do not delete) until the exchange is
                    # durably resolved.  Fail closed; output currently holds
                    # the new content but its on-disk durability is uncertain.
                    rollback_preserved = True
                    raise RuntimeError(
                        f"exchange succeeded but the parent-directory fsync "
                        f"failed ({exc}); exchange durability is uncertain. OLD content "
                        f"is PRESERVED in {sibling_new} for manual rollback. "
                        f"Resolve the durability failure and remove {sibling_new} "
                        f"only once the exchange boundary is confirmed synced."
                    ) from None
                # Exchange boundary is durably synced — old content is safe to
                # remove.
                _remove_tree_durable(sibling_new)
        except BaseException:
            # On ANY failure, clean up sibling_new — UNLESS it holds the
            # irreplaceable OLD rollback content (r7-M3: a successful exchange
            # whose parent-fsync failed leaves the rollback copy there).
            if not rollback_preserved:
                _remove_tree_durable(sibling_new)
            # Restore prior output if a legacy two-rename crash (from an older
            # publisher version) left it moved aside to sibling_old.  The
            # exchange path never creates sibling_old, so this is defensive.
            # Restoration failure is surfaced (never swallow data-loss risk).
            if sibling_old.exists() and not output.exists():
                try:
                    os.rename(str(sibling_old), str(output))
                except OSError as exc:
                    raise RuntimeError(
                        f"FAILED to restore prior proof after publish failure "
                        f"(prior bytes are in {sibling_old}): {exc}"
                    ) from exc
                _fsync_dir(parent)  # r6-minor: persist restoration rename
            raise


def _entry_recover_and_prepare(output: Path, *, timeout_s: float = _PUBLICATION_LOCK_TIMEOUT_S) -> list[str]:
    """Main-entry preparation: recover an interrupted publish BEFORE OUTPUT
    is touched, then ensure only OUTPUT's PARENT exists.

    r7-M1: recovery runs UNDER THE SAME cross-process publication lock as
    ``_atomic_publish``.  A second capture process can legitimately own the
    ``.publish-old`` / ``.publish-new`` transient paths mid-swap while it
    holds the lock; recovering without the lock could inspect, remove, or
    restore another publisher's active state.  Holding the lock during
    recovery makes the recovery/publication protocol a single non-nested
    critical section: no two processes ever touch the sibling state
    concurrently.  The publish-time recovery (also under the lock) is
    idempotent, so recovering twice under the lock is safe.

    OUTPUT itself is NEVER created here.  If a previous publish crashed with
    ``output`` moved to ``.publish-old`` (output absent), creating ``output``
    now would mask the crash state and cause recovery to treat ``.publish-old``
    as a redundant leftover and DELETE the sole prior proof.  Recovering first
    restores the prior proof (by move) so it survives.  OUTPUT is brought into
    being solely by the atomic sibling swap in ``_atomic_publish`` on a
    successful commit.
    """
    # The lock file lives in output.parent, so the parent must exist first.
    output.parent.mkdir(parents=True, exist_ok=True)
    with _publication_lock(output, timeout_s=timeout_s):
        actions = _recover_interrupted_publish(output)
    return actions


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-checkout", type=Path,
        help="Verify recorded annotation evidence in a selected clean evidence checkout; "
             "does not capture or certify the invoking working tree.",
    )
    args = parser.parse_args()
    if args.verify_checkout is not None:
        from okf_loom.capture_provenance import validate_manifest

        checkout = args.verify_checkout.resolve()
        try:
            manifest = json.loads((checkout / "docs/screenshots/annotations-v2/manifest.json").read_text())
            expected = {name: "; ".join(values) for name, values in SCENARIO_ASSERTIONS.items()}
            errors = validate_manifest(manifest, checkout, expected, check_files=True, phase="final")
        except (OSError, ValueError) as exc:
            errors = [str(exc)]
        if errors:
            parser.exit(1, "Evidence verification failed: " + "; ".join(errors) + "\n")
        print(f"Verified selected annotation evidence checkout: {checkout} (not current-tree acceptance)")
        return 0

    # Browser dependencies belong to capture, not provenance/publication checks.
    from playwright.sync_api import expect, sync_playwright

    # audit-1 / r7-M1: recover an interrupted publish BEFORE creating OUTPUT,
    # and create only the PARENT.  Recovery runs UNDER THE SAME publication
    # lock as the publish itself, so a concurrent capture process cannot be
    # mid-swap on the sibling state while this recovery inspects it.  OUTPUT is
    # created solely by the atomic swap in _atomic_publish on a successful
    # commit — never pre-created (which could mask a crash state and destroy
    # the sole prior proof).
    _entry_recover_and_prepare(OUTPUT)
    # audit-3: take an immutable provenance snapshot BEFORE the server/capture
    # so source-byte drift during capture is detectable.  The post-capture
    # snapshot must be byte-identical; the manifest is built from it.
    from okf_loom.capture_provenance import (
        PROVENANCE_MODEL,
        capture_provenance_snapshot,
        expected_evidence_paths,
        inventory_serialized_digest,
        snapshot_inventory_for_manifest,
    )
    pre_snapshot = capture_provenance_snapshot(ROOT)
    with tempfile.TemporaryDirectory(prefix="okf-annotations-capture-") as temp:
        bundle = Path(temp) / "bundle"
        shutil.copytree(ROOT / "samples" / "demo_bundle", bundle, ignore=shutil.ignore_patterns(".okf-loom"))
        port = free_port(); base = f"http://127.0.0.1:{port}"
        proc = subprocess.Popen(
            [str(ROOT / "scripts" / "okf-loom"), "serve", str(bundle), "--host", "127.0.0.1", "--port", str(port), "--no-open"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            wait_server(proc, base)
            # R4-4: staging directory for atomic publish.
            staging = Path(temp) / "staging"
            staging.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="chrome")
                context = browser.new_context(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
                page = context.new_page(); console_errors = []
                page.on("console", lambda message: console_errors.append(message.text) if unexpected_console_error(message) else None)
                page.goto(base + "/tables/orders", wait_until="load")
                page.wait_for_function("() => window.okfLoomStudio && window.okfLoomStudio.state.doc")
                page.wait_for_function("() => ['ready','error'].includes(window.okfLoomStudio.state.commentsStatus)")
                if page.evaluate("window.okfLoomStudio.state.commentsStatus") != "ready":
                    # Diagnostic: capture why status is not ready before reload.
                    diag = page.evaluate("""() => {
                        const s = window.okfLoomStudio.state;
                        const doc = s.doc;
                        let projValid = null;
                        if (doc && doc.annotation_projection) {
                            try { projValid = window.okfLoomStudio._validProjection(
                                doc.annotation_projection, doc.concept_content_rev,
                                doc.annotation_source, doc.html); } catch(e) { projValid = 'throw: ' + e.message; }
                        }
                        return {
                            status: s.commentsStatus,
                            hasDoc: !!doc,
                            docRev: doc ? doc.concept_content_rev?.substring(0,8) : null,
                            projComplete: doc?.annotation_projection?.complete,
                            projRev: doc?.annotation_projection?.concept_content_rev?.substring(0,8),
                            projValid: projValid,
                            mappedBlocks: doc?.annotation_projection?.blocks?.filter(b => b.dom_key !== null).length,
                            totalBlocks: doc?.annotation_projection?.blocks?.length,
                        };
                    }""")
                    if diag.get("status") == "error":
                        raise RuntimeError(f"commentsStatus=error on initial load; diag={diag}; console={console_errors}")
                    page.reload(wait_until="load")
                    page.wait_for_function("() => window.okfLoomStudio && window.okfLoomStudio.state.commentsStatus === 'ready'")
                page.wait_for_function("() => window.okfLoomStudio.state.selectionAffordanceReady === true")
                select_text(page, ".okf-page__body #schema[data-okf-projection-key]", "Schema")
                expect(page.get_by_role("button", name="Comment on the selected text")).to_be_visible()
                page.get_by_role("button", name="Comment on the selected text").click()
                composer = page.get_by_role("textbox", name="Comment for agent")
                expect(composer).to_be_visible()
                # P1 fix: JS-on screenshots must NOT contain sentinel text.
                # Use a clean comment body for all JS-on captures. A separate
                # sentinel-bearing comment is posted AFTER all JS-on
                # screenshots are done, exclusively for the no-JS proof.
                composer.fill("Verify this relationship before publishing.")
                expect(page.get_by_role("button", name="Send", exact=True)).to_be_enabled()
                with page.expect_response(lambda response: "/__comment" in response.url and response.request.method == "POST") as posted:
                    page.get_by_role("button", name="Send", exact=True).click()
                if not (posted.value.ok and posted.value.json().get("ok") is True):
                    raise RuntimeError(f"Comment POST failed: {posted.value.status} {posted.value.json()}")
                posted_comment = posted.value.json()["comment"]
                comment_id = posted_comment["id"]
                with urllib.request.urlopen(base + "/__comments", timeout=2) as response:
                    canonical_comments = json.loads(response.read().decode("utf-8"))["comments"]
                if not any(row.get("id") == comment_id for row in canonical_comments):
                    raise RuntimeError("Posted comment ID not found in canonical /__comments")
                expect(page.locator("mark.okf-comment-mark")).to_be_visible()
                expect(page.get_by_role("button", name="Jump")).to_be_visible()
                # Scroll the resolved mark for the posted comment into the
                # visible document viewport and verify geometry/style.
                # Resilient to async comment/doc reconciliation replacing the
                # mark between readiness and scroll: comment-id-keyed single
                # evaluate transaction + retryable fresh-locator reacquire
                # (no sleeps, no stale detached handle).
                _scroll_resolved_mark_into_view(page, comment_id)
                image_meta = []
                image_meta.append(_capture_with_metadata(page, "resolved.png", staging))

                concept_path = bundle / "tables" / "orders.md"
                original = concept_path.read_text(encoding="utf-8")
                with urllib.request.urlopen(base + "/__data/doc?id=tables/orders", timeout=2) as response:
                    original_rev = json.loads(response.read().decode("utf-8"))["concept_content_rev"]
                concept_path.write_text(original.replace("# Schema", "# Changed schema", 1), encoding="utf-8")
                stale_row = None
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    with urllib.request.urlopen(base + "/__data/doc?id=tables/orders", timeout=2) as response:
                        changed_doc = json.loads(response.read().decode("utf-8"))
                    with urllib.request.urlopen(base + "/__comments", timeout=2) as response:
                        rows = json.loads(response.read().decode("utf-8"))["comments"]
                    row = next((item for item in rows if item.get("id") == comment_id), None)
                    resolution = (row or {}).get("anchor_resolution") or {}
                    if changed_doc.get("concept_content_rev") != original_rev and resolution.get("status") == "stale" and resolution.get("reason") == "exact_not_found":
                        stale_row = row
                        break
                    time.sleep(0.1)
                if stale_row is None:
                    raise RuntimeError("Server did not derive stale/exact_not_found after canonical mutation")
                page.close()
                ready = False
                for attempt in range(3):
                    page = context.new_page()
                    page.on("console", lambda message: console_errors.append(message.text) if unexpected_console_error(message) else None)
                    page.goto(base + "/tables/orders", wait_until="load")
                    page.wait_for_function("() => window.okfLoomStudio && ['ready','error'].includes(window.okfLoomStudio.state.commentsStatus)")
                    if page.evaluate("window.okfLoomStudio.state.commentsStatus") == "ready": ready = True; break
                    if attempt < 2: page.close()
                if not ready:
                    raise RuntimeError("Fresh page did not reach comments ready state")
                page.get_by_role("button", name="Comments").click()
                expect(page.locator("#comment-" + comment_id)).to_be_visible()
                expect(page.get_by_text("Text not found", exact=False)).to_be_visible(timeout=15_000)
                expect(page.get_by_role("button", name="Re-anchor")).to_be_visible()
                image_meta.append(_capture_with_metadata(page, "stale-after-mutation.png", staging))

                page.get_by_role("button", name="Re-anchor").click()
                expect(page.get_by_text("Select the exact text this comment refers to in the document.")).to_be_visible()
                expect(page.locator(".okf-reanchor__instruction")).to_be_focused()
                image_meta.append(_capture_with_metadata(page, "selecting.png", staging))

                select_text(page, ".okf-page__body p[data-okf-projection-key]", "The")
                page.wait_for_function("() => document.body.dataset.okfSelectionReady === 'true'")
                page.get_by_role("button", name="Use selected text").click()
                expect(page.get_by_role("button", name="Save anchor")).to_be_enabled()
                page.route("**/__comment-reanchor", lambda route: route.fulfill(status=409, json={"ok": False, "error": {"code": "concept_conflict", "message": "The document changed before this anchor was saved."}}))
                page.get_by_role("button", name="Save anchor").click()
                expect(page.get_by_role("button", name="Refresh and reselect")).to_be_visible(timeout=10_000)
                image_meta.append(_capture_with_metadata(page, "conflict.png", staging))
                page.unroute("**/__comment-reanchor")
                page.locator(".okf-reanchor").get_by_role("button", name="Cancel", exact=True).click()
                page.get_by_role("button", name="Re-anchor").click()
                expect(page.locator(".okf-reanchor__instruction")).to_be_focused()
                select_text(page, ".okf-page__body p[data-okf-projection-key]", "The")
                page.wait_for_function("() => document.body.dataset.okfSelectionReady === 'true'")
                page.get_by_role("button", name="Use selected text").click()
                expect(page.get_by_role("button", name="Save anchor")).to_be_enabled()
                page.route("**/__comment-reanchor", lambda route: route.fulfill(status=500, json={"ok": False, "error": {"code": "temporary", "message": "The anchor could not be saved."}}))
                page.get_by_role("button", name="Save anchor").click()
                expect(page.get_by_role("button", name="Retry save")).to_be_visible()
                image_meta.append(_capture_with_metadata(page, "save-failure.png", staging))

                retry = page.get_by_role("button", name="Retry save")
                # Keyboard-reachability test: bounded traversal over the REAL
                # focusable ring. The wide comments rail is now non-modal (no
                # focus trap), so a fixed Tab budget cannot deterministically
                # reach the control; the helper fails closed if it is not
                # keyboard-reachable. The landing Tab is a genuine keystroke,
                # which activates :focus-visible; programmatic focus() on the
                # control is never used (it suppresses the Chromium ring). The
                # :focus-visible computed-style check below is the SEPARATE
                # capture-focus-styling assertion.
                _keyboard_reach_and_focus(page, retry, label="focus-visible Retry save")
                expect(retry).to_be_focused()
                readiness = retry.evaluate("""e => { const s=getComputedStyle(e); return {
                  label:e.textContent.trim(), focusVisible:e.matches(':focus-visible'),
                  outlineStyle:s.outlineStyle, outlineWidth:s.outlineWidth,
                  outlineColor:s.outlineColor, boxShadow:s.boxShadow
                }; }""")
                if readiness["label"] != "Retry save":
                    raise RuntimeError(f"Focus target label mismatch: {readiness}")
                if readiness["focusVisible"] is not True:
                    raise RuntimeError(f"Retry save not :focus-visible: {readiness}")
                if readiness["outlineStyle"] in ("none", "hidden"):
                    raise RuntimeError(f"No visible focus outline: {readiness}")
                if float(readiness["outlineWidth"].removesuffix("px")) < 2:
                    raise RuntimeError(f"Focus outline too thin: {readiness}")
                image_meta.append(_capture_with_metadata(page, "focus-visible.png", staging))
                # r5-5: distinctness check must hash the STAGED files (just
                # written to staging), not the stale OUTPUT copies.  Hashing
                # OUTPUT reads prior-bytes on first run (no file yet) and can
                # accept identical new screenshots based on unrelated old bytes.
                save_path = staging / "save-failure.png"
                focus_path = staging / "focus-visible.png"
                if not save_path.is_file():
                    raise RuntimeError("save-failure.png missing from staging")
                if not focus_path.is_file():
                    raise RuntimeError("focus-visible.png missing from staging")
                save_hash = hashlib.sha256(save_path.read_bytes()).hexdigest()
                focus_hash = hashlib.sha256(focus_path.read_bytes()).hexdigest()
                if focus_hash == save_hash:
                    raise RuntimeError(f"focus-visible.png identical to save-failure.png: {save_hash}")

                page.set_viewport_size({"width": 320, "height": 900})
                # Group 10: deterministic mobile readiness — keyboard-focus the
                # exact Retry save control, assert active element, focus-visible,
                # accessible role/name/state, full viewport/container geometry.
                mobile_retry = page.get_by_role("button", name="Retry save")
                expect(mobile_retry).to_be_visible()
                _keyboard_reach_and_focus(page, mobile_retry, label="mobile-320 Retry save")
                mobile_geom = mobile_retry.evaluate("""e => {
                    const r = e.getBoundingClientRect();
                    const s = getComputedStyle(e);
                    return {
                        isFocused: document.activeElement === e,
                        focusVisible: e.matches(':focus-visible'),
                        visible: r.width > 0 && r.height > 0,
                        right: Math.ceil(r.right), bottom: Math.ceil(r.bottom),
                        left: Math.ceil(r.left), top: Math.ceil(r.top),
                        tabbable: e.matches('button:not([disabled])'),
                        role: e.getAttribute('role') || 'button',
                        name: e.textContent.trim(),
                        outlineStyle: s.outlineStyle,
                        outlineWidth: parseFloat(s.outlineWidth),
                    };
                }""")
                if not mobile_geom["isFocused"]:
                    raise RuntimeError(f"Mobile retry not keyboard-focused: {mobile_geom}")
                if not mobile_geom["focusVisible"]:
                    raise RuntimeError(f"Mobile retry not :focus-visible: {mobile_geom}")
                if not mobile_geom["visible"]:
                    raise RuntimeError(f"Mobile retry not visible: {mobile_geom}")
                if mobile_geom["right"] > 320 or mobile_geom["bottom"] > 900:
                    raise RuntimeError(f"Mobile retry clipped: {mobile_geom}")
                if mobile_geom["left"] < 0 or mobile_geom["top"] < 0:
                    raise RuntimeError(f"Mobile retry negative position: {mobile_geom}")
                if mobile_geom["name"] != "Retry save":
                    raise RuntimeError(f"Mobile retry wrong accessible name: {mobile_geom}")
                if mobile_geom["outlineStyle"] in ("none", "hidden"):
                    raise RuntimeError(f"Mobile retry no outline: {mobile_geom}")
                # Error summary must have role=alert and be unclipped.
                error_summary = page.locator(".okf-reanchor__error[role='alert']")
                error_geom = error_summary.evaluate("""e => {
                    const r = e.getBoundingClientRect();
                    return { visible: r.width > 0 && r.height > 0, right: Math.ceil(r.right) };
                }""")
                if not error_geom["visible"] or error_geom["right"] > 320:
                    raise RuntimeError(f"Mobile error summary clipped/hidden: {error_geom}")
                # r7-M4: capture the ACTUAL 320px document-peek state, not the
                # open-drawer instruction. The peek is a distinct state: the
                # recovery drawer (panel) and scrim are hidden so the authored
                # document is visible for text selection, and a floating
                # .okf-reanchor-peek bar provides "Return to comment". Enter
                # selecting mode (so the "Select in document" bridge appears),
                # then peek the drawer open.
                page.locator(".okf-reanchor").get_by_role(
                    "button", name="Edit selection", exact=True).click()
                expect(page.get_by_role("button", name="Select in document")).to_be_visible()
                page.get_by_role("button", name="Select in document").click()
                # Wait for the document-peek state: panel + scrim hidden, the
                # floating peek bar shown.
                page.wait_for_function("""() => {
                    const panel = document.querySelector('.okf-panel');
                    const overlay = document.querySelector('.okf-panel-overlay');
                    const peek = document.querySelector('.okf-reanchor-peek');
                    return panel && panel.hidden && overlay && overlay.hidden
                        && peek && !peek.hidden;
                }""")
                peek_geom = page.evaluate("""() => {
                    const panel = document.querySelector('.okf-panel');
                    const overlay = document.querySelector('.okf-panel-overlay');
                    const peek = document.querySelector('.okf-reanchor-peek');
                    const body = document.querySelector('.okf-page__body');
                    const ret = document.querySelector('.okf-reanchor-peek button');
                    const pr = peek.getBoundingClientRect();
                    const br = body.getBoundingClientRect();
                    const editor = window.okfLoomStudio.state.annotationEditor;
                    return {
                        panelHidden: !!panel && panel.hidden,
                        overlayHidden: !!overlay && overlay.hidden,
                        peekVisible: !!peek && !peek.hidden && pr.width > 0 && pr.height > 0,
                        peekRight: Math.ceil(pr.right), peekBottom: Math.ceil(pr.bottom),
                        peekLeft: Math.ceil(pr.left), peekTop: Math.ceil(pr.top),
                        returnVisible: !!ret && ret.offsetWidth > 0 && ret.offsetHeight > 0
                            && ret.getBoundingClientRect().width > 0,
                        returnText: ret ? ret.textContent.trim() : null,
                        bodyVisible: body.offsetWidth > 0 && br.width > 0 && br.height > 0,
                        bodyRight: Math.round(br.right),
                        editorAlive: !!editor,
                        editorMode: editor && editor.mode,
                        docScrollWidth: document.documentElement.scrollWidth,
                        viewportWidth: document.documentElement.clientWidth,
                    };
                }""")
                if not (peek_geom["panelHidden"] and peek_geom["overlayHidden"]):
                    raise RuntimeError(f"document-peek: panel/scrim not hidden: {peek_geom}")
                if not peek_geom["peekVisible"]:
                    raise RuntimeError(f"document-peek: .okf-reanchor-peek not visible: {peek_geom}")
                if peek_geom["returnText"] != "Return to comment" or not peek_geom["returnVisible"]:
                    raise RuntimeError(
                        f"document-peek: Return to comment not visible: {peek_geom}")
                if not peek_geom["bodyVisible"] or peek_geom["bodyRight"] < 300:
                    raise RuntimeError(
                        f"document-peek: authored document not visible: {peek_geom}")
                if (peek_geom["peekRight"] > 320 or peek_geom["peekBottom"] > 900
                        or peek_geom["peekLeft"] < 0 or peek_geom["peekTop"] < 0):
                    raise RuntimeError(
                        f"document-peek: peek bar outside viewport bounds: {peek_geom}")
                if peek_geom["docScrollWidth"] > peek_geom["viewportWidth"]:
                    raise RuntimeError(
                        f"document-peek: horizontal overflow at 320px: {peek_geom}")
                if not (peek_geom["editorAlive"] and peek_geom["editorMode"] == "selecting"):
                    raise RuntimeError(
                        f"document-peek: editor/selection state lost: {peek_geom}")
                image_meta.append(_capture_with_metadata(page, "mobile-320.png", staging))

                # r7-M4: "Return to comment" must restore the drawer and preserve
                # editor/selection ownership (editor still alive, selecting mode,
                # recovery actions within the 320px bounds).
                page.get_by_role("button", name="Return to comment").click()
                page.wait_for_function("""() => {
                    const panel = document.querySelector('.okf-panel');
                    const peek = document.querySelector('.okf-reanchor-peek');
                    const editor = window.okfLoomStudio.state.annotationEditor;
                    return panel && !panel.hidden && peek && peek.hidden && editor;
                }""")
                restored_geom = page.evaluate("""() => {
                    const panel = document.querySelector('.okf-panel');
                    const peek = document.querySelector('.okf-reanchor-peek');
                    const editor = window.okfLoomStudio.state.annotationEditor;
                    const actions = document.querySelector(
                        '.okf-reanchor .okf-reanchor__actions');
                    return {
                        panelRestored: !!panel && !panel.hidden,
                        peekHidden: !!peek && peek.hidden,
                        editorAlive: !!editor,
                        editorMode: editor && editor.mode,
                        actionsRight: actions
                            ? Math.ceil(actions.getBoundingClientRect().right) : -1,
                    };
                }""")
                if not restored_geom["panelRestored"]:
                    raise RuntimeError(f"drawer not restored after Return: {restored_geom}")
                if not restored_geom["peekHidden"]:
                    raise RuntimeError(f"peek bar not hidden after Return: {restored_geom}")
                if not (restored_geom["editorAlive"]
                        and restored_geom["editorMode"] == "selecting"):
                    raise RuntimeError(
                        f"editor/selection not preserved after Return: {restored_geom}")
                if restored_geom["actionsRight"] > 320:
                    raise RuntimeError(
                        f"recovery actions clipped at 320 after Return: {restored_geom}")

                # Restore the save_failed scenario for the forced-colors capture
                # (which focuses "Retry save"). The /__comment-reanchor 500 route
                # set above is still active, so re-saving reproduces save_failed.
                select_text(page, ".okf-page__body p[data-okf-projection-key]", "The")
                page.wait_for_function("() => document.body.dataset.okfSelectionReady === 'true'")
                page.get_by_role("button", name="Use selected text").click()
                expect(page.get_by_role("button", name="Save anchor")).to_be_enabled()
                page.get_by_role("button", name="Save anchor").click()
                expect(page.get_by_role("button", name="Retry save")).to_be_visible(timeout=10_000)

                # Restore desktop viewport before forced-colors capture.
                page.set_viewport_size({"width": 1280, "height": 900})
                page.emulate_media(forced_colors="active", reduced_motion="reduce")
                # Group 10: forced-colors deterministic readiness — keyboard-
                # focus the EXACT Retry save control, assert active element,
                # focus-visible, outline, role/name, error geometry, media.
                forced_retry = page.get_by_role("button", name="Retry save")
                expect(forced_retry).to_be_visible()
                _keyboard_reach_and_focus(page, forced_retry, label="forced-colors Retry save")
                forced_check = forced_retry.evaluate("""e => {
                    const s = getComputedStyle(e);
                    const r = e.getBoundingClientRect();
                    const fcActive = window.matchMedia('(forced-colors: active)').matches;
                    const rmActive = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
                    const errorEl = document.querySelector('.okf-reanchor__error[role="alert"]');
                    const er = errorEl ? errorEl.getBoundingClientRect() : null;
                    return {
                        isActive: document.activeElement === e,
                        focusVisible: e.matches(':focus-visible'),
                        outlineStyle: s.outlineStyle,
                        outlineWidth: parseFloat(s.outlineWidth),
                        forcedColorsActive: fcActive,
                        reducedMotionActive: rmActive,
                        retryText: e.textContent.trim(),
                        retryEnabled: !e.disabled,
                        retryRole: e.tagName.toLowerCase(),
                        retryRight: Math.ceil(r.right), retryBottom: Math.ceil(r.bottom),
                        retryLeft: Math.ceil(r.left), retryTop: Math.ceil(r.top),
                        errorVisible: !!errorEl && errorEl.offsetWidth > 0 && errorEl.offsetHeight > 0,
                        errorRole: errorEl ? (errorEl.getAttribute('role') || '') : '',
                        errorRight: er ? Math.ceil(er.right) : -1,
                        errorBottom: er ? Math.ceil(er.bottom) : -1,
                    };
                }""")
                vw = 1280; vh = 900
                if not forced_check["isActive"]:
                    raise RuntimeError(f"Forced-colors retry not active element: {forced_check}")
                if not forced_check["focusVisible"]:
                    raise RuntimeError(f"Forced-colors retry not :focus-visible: {forced_check}")
                if forced_check["outlineStyle"] in ("none", "hidden"):
                    raise RuntimeError(f"Forced-colors retry outline none: {forced_check}")
                if forced_check["outlineWidth"] <= 0:
                    raise RuntimeError(f"Forced-colors retry outline width <=0: {forced_check}")
                if not forced_check["forcedColorsActive"]:
                    raise RuntimeError("forced-colors media not active")
                if not forced_check["reducedMotionActive"]:
                    raise RuntimeError("reduced-motion media not active")
                if forced_check["retryText"] != "Retry save":
                    raise RuntimeError(f"Forced-colors selected wrong control: {forced_check}")
                if not forced_check["retryEnabled"]:
                    raise RuntimeError(f"Forced-colors retry disabled: {forced_check}")
                if forced_check["retryRole"] != "button":
                    raise RuntimeError(f"Forced-colors retry wrong role: {forced_check}")
                if forced_check["retryRight"] > vw or forced_check["retryBottom"] > vh:
                    raise RuntimeError(f"Forced-colors retry clipped: {forced_check}")
                if forced_check["retryLeft"] < 0 or forced_check["retryTop"] < 0:
                    raise RuntimeError(f"Forced-colors retry negative: {forced_check}")
                if not forced_check["errorVisible"]:
                    raise RuntimeError(f"Forced-colors error not visible: {forced_check}")
                if forced_check["errorRole"] != "alert":
                    raise RuntimeError(f"Forced-colors error role wrong: {forced_check}")
                if forced_check["errorRight"] > vw or forced_check["errorBottom"] > vh:
                    raise RuntimeError(f"Forced-colors error clipped: {forced_check}")
                image_meta.append(_capture_with_metadata(page, "forced-colors.png", staging))

                # r5-4: Persist sentinel strings in a REAL comment AFTER all
                # JS-on screenshots are captured (so sentinel text never
                # contaminates a screenshot).  The POST uses the real public
                # /__comment API from the page context so the CSRF token,
                # Origin, and Host are all valid.  The BODY sentinel lives in
                # the comment body; the QUOTE sentinel is placed into the
                # canonical selectable concept text and quoted by a real
                # production capture_anchor v2 anchor (exact == sentinel), so
                # the anchor is semantically valid and maps to real source,
                # not a fabricated span with invented offsets/hashes.
                # Both sentinels are confirmed through /__comments BEFORE the
                # no-JS absence checks so those checks are meaningful: the
                # sentinels are provably in server state and must not leak
                # into no-JS HTML.
                body_sentinel = "OKF_NOJS_BODY_SENTINEL_" + str(int(time.time()))
                quote_sentinel = "OKF_NOJS_QUOTE_SENTINEL_" + str(int(time.time()))
                sentinel_body = f"Sentinel probe for no-JS isolation. BODY={body_sentinel} QUOTE={quote_sentinel}"
                # Put the unique QUOTE sentinel into canonical selectable
                # concept text so the production capture_anchor builds a real
                # text-quote anchor whose exact quote IS the sentinel.  The
                # concept is the temporary bundle copy (never product source);
                # the sentinel is reverted before the no-JS check so the
                # absence proof stays meaningful (the anchor data persists
                # independently in /__comments).
                from okf_loom.annotations import capture_anchor
                with urllib.request.urlopen(base + "/__data/doc?id=tables/orders", timeout=2) as response:
                    pre_sentinel_rev = json.loads(response.read().decode("utf-8"))["concept_content_rev"]
                pre_sentinel_source = concept_path.read_text(encoding="utf-8")
                anchor_host = "Globally unique order identifier."
                if anchor_host not in pre_sentinel_source:
                    raise RuntimeError("anchor host text missing from concept source for sentinel")
                sentinel_phrase = f"{anchor_host} {quote_sentinel}"
                sentinel_source = pre_sentinel_source.replace(anchor_host, sentinel_phrase, 1)
                if sentinel_source == pre_sentinel_source:
                    raise RuntimeError("quote sentinel was not inserted into concept source")
                concept_path.write_text(sentinel_source, encoding="utf-8")
                # Wait for the studio watcher to reload the mutated concept.
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    with urllib.request.urlopen(base + "/__data/doc?id=tables/orders", timeout=2) as response:
                        mutated_rev = json.loads(response.read().decode("utf-8"))["concept_content_rev"]
                    if mutated_rev != pre_sentinel_rev:
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("studio did not reload sentinel concept mutation")
                # Build a semantically valid v2 anchor quoting the sentinel
                # via the production canonical capture_anchor implementation.
                current_source = concept_path.read_text(encoding="utf-8")
                anchor_idx = current_source.find(quote_sentinel)
                if anchor_idx < 0:
                    raise RuntimeError("quote sentinel not found in concept source after mutation")
                real_anchor = capture_anchor(current_source, anchor_idx, anchor_idx + len(quote_sentinel))
                sentinel_result = page.evaluate(
                    """async ({concept, body, anchor}) => {
                        const token = ((window.__OKF_LOOM_STUDIO__ || {}).token) || "";
                        const resp = await fetch("/__comment", {
                            method: "POST",
                            headers: {"Content-Type": "application/json", "X-OKF-Token": token},
                            body: JSON.stringify({concept, body, anchor, actor: "user"}),
                        });
                        return {status: resp.status, ok: resp.ok, json: await resp.json()};
                    }""",
                    {"concept": "tables/orders", "body": sentinel_body, "anchor": real_anchor},
                )
                if not (sentinel_result.get("ok") and sentinel_result.get("json", {}).get("ok") is True):
                    raise RuntimeError(f"Sentinel comment POST failed: {sentinel_result}")
                sentinel_comment = sentinel_result["json"]["comment"]
                # Confirm BOTH sentinels are persisted in canonical /__comments,
                # and that the anchor's exact quote IS the QUOTE sentinel: a
                # semantically valid persisted quote sentinel.
                with urllib.request.urlopen(base + "/__comments", timeout=2) as resp:
                    sentinel_comments = json.loads(resp.read().decode("utf-8"))["comments"]
                sentinel_row = next((r for r in sentinel_comments if r.get("id") == sentinel_comment.get("id")), None)
                if sentinel_row is None:
                    raise RuntimeError("body sentinel not persisted in canonical /__comments")
                if body_sentinel not in (sentinel_row.get("body") or ""):
                    raise RuntimeError("body sentinel not persisted in canonical /__comments body")
                persisted_exact = (sentinel_row.get("anchor") or {}).get("exact")
                if persisted_exact != quote_sentinel:
                    raise RuntimeError("quote sentinel not persisted in canonical /__comments anchor exact")
                # Revert the concept so the QUOTE sentinel (now persisted only
                # as comment anchor data) cannot appear in no-JS SSR HTML.
                concept_path.write_text(pre_sentinel_source, encoding="utf-8")
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    with urllib.request.urlopen(base + "/__data/doc?id=tables/orders", timeout=2) as response:
                        reverted_rev = json.loads(response.read().decode("utf-8"))["concept_content_rev"]
                    if reverted_rev == pre_sentinel_rev:
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("studio did not reload sentinel concept revert")

                nojs_context = browser.new_context(viewport={"width": 1280, "height": 900}, java_script_enabled=False)
                nojs = nojs_context.new_page(); nojs.goto(base + "/tables/orders", wait_until="load")
                expect(nojs.locator(".okf-nojs-live-banner:visible")).to_have_count(1)
                expect(nojs.locator(".okf-studio-fallback-banner:visible")).to_have_count(1)
                # R3-4: no-JS must not expose any annotation/comment UI.
                if nojs.get_by_role("button", name="Re-anchor").count() > 0:
                    raise RuntimeError("Re-anchor visible without JS")
                if nojs.get_by_role("button", name="Save anchor").count() > 0:
                    raise RuntimeError("Save anchor visible without JS")
                if nojs.get_by_role("button", name="Jump").count() > 0:
                    raise RuntimeError("Jump visible without JS")
                if nojs.locator("mark.okf-comment-mark").count() > 0:
                    raise RuntimeError("Comment mark visible without JS")
                if nojs.locator(".okf-anchor-row").count() > 0:
                    raise RuntimeError("Anchor health row visible without JS")
                # R4-1: no-JS sentinel proof — the sentinels were persisted via
                # the real public comment workflow (POST /__comment). Verify they
                # do NOT appear anywhere in the raw response HTML or DOM text.
                nojs_html = nojs.content()
                nojs_text = nojs.locator("body").inner_text()
                if body_sentinel in nojs_html or body_sentinel in nojs_text:
                    raise RuntimeError(f"body sentinel leaked into no-JS response: {body_sentinel}")
                if quote_sentinel in nojs_html or quote_sentinel in nojs_text:
                    raise RuntimeError(f"quote sentinel leaked into no-JS response: {quote_sentinel}")
                # Authored concept must remain visible.
                if "Orders" not in (nojs.locator("h1").text_content() or ""):
                    raise RuntimeError("Authored concept title not visible without JS")
                nojs.screenshot(path=staging / "no-js.png", full_page=False)
                from okf_loom.capture_provenance import SCENARIO_CLASSIFICATIONS
                image_meta.append({
                    "file": "no-js.png",
                    "sha256": hashlib.sha256((staging / "no-js.png").read_bytes()).hexdigest(),
                    "classification": SCENARIO_CLASSIFICATIONS.get("no-js.png", "release-proof"),
                    "release_proof": True,
                    "scenario_assertions": SCENARIO_ASSERTIONS["no-js.png"],
                    "viewport": {"width": 1280, "height": 900},
                    "media": {"reduced_motion": "none", "forced_colors": "none"},
                })
                nojs_context.close()
                if console_errors:
                    raise RuntimeError("Browser console errors during capture: " + " | ".join(console_errors))
                # audit-6: NO stale-OUTPUT-extras precheck here.  The whole
                # OUTPUT directory is replaced atomically by the sibling swap,
                # so any stale extras vanish ONLY on a successful commit (the
                # validator's exact-set check enforces the published set).
                # R4-4: validate complete set in staging before atomic publish.
                staging_files = {p.name for p in staging.glob("*.png")}
                if staging_files != EXPECTED_PNG_FILES:
                    missing_s = EXPECTED_PNG_FILES - staging_files
                    extra_s = staging_files - EXPECTED_PNG_FILES
                    raise RuntimeError(f"staging set mismatch: missing={missing_s} extra={extra_s}")
                # Verify all expected files are present in entries.
                entries = image_meta
                entry_files = {e["file"] for e in entries}
                missing = EXPECTED_PNG_FILES - entry_files
                if missing:
                    raise RuntimeError(f"Expected screenshots not captured: {missing}")
                # audit-3: post-capture snapshot must be byte-identical to the
                # pre-server snapshot — source bytes must not drift during
                # capture (single-scan immutable snapshot, no double scan).
                post_snapshot = capture_provenance_snapshot(ROOT)
                if pre_snapshot != post_snapshot:
                    raise RuntimeError(
                        "provenance drift: source bytes changed during capture "
                        f"(pre head={pre_snapshot.head[:8]} "
                        f"inv={pre_snapshot.inventory_digest[:8]}; "
                        f"post head={post_snapshot.head[:8]} "
                        f"inv={post_snapshot.inventory_digest[:8]})")
                # Build expected_artifacts for the validator.
                _expected = {name: "; ".join(asserts) for name, asserts in SCENARIO_ASSERTIONS.items()}
                manifest_inventory = snapshot_inventory_for_manifest(post_snapshot)
                manifest = {
                    "schema": 1,
                    "provenance_model": PROVENANCE_MODEL,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "source_revision": post_snapshot.head,
                    "source_revision_verified": True,
                    "evidence_parent_revision": post_snapshot.head,
                    "evidence_parent_verified": True,
                    "evidence_paths": expected_evidence_paths(_expected),
                    "source_worktree_digest": post_snapshot.worktree_digest,
                    "evidence_worktree_digest": post_snapshot.worktree_digest,
                    "capture_input_inventory_sha256": post_snapshot.inventory_digest,
                    "capture_input_inventory_serialized_sha256": inventory_serialized_digest(manifest_inventory),
                    "capture_input_file_count": post_snapshot.inventory_count,
                    "capture_input_inventory": manifest_inventory,
                    "capture_script_sha256": post_snapshot.script_sha256,
                    "browser": browser.version,
                    "images": entries,
                }
                # r5-1: Atomic publish via sibling-directory swap. The complete
                # publication set is built, fsynced, and validated in a fresh
                # sibling directory, then swapped into OUTPUT with two atomic
                # os.rename calls. Prior OUTPUT bytes are preserved at every
                # failure point (copy, fsync, validation, or either rename).
                _atomic_publish(staging, OUTPUT, manifest, entries, _expected, ROOT)
                context.close(); browser.close()
        finally:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
