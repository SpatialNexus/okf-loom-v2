"""Finite browser proof for the F9/F10 recovery-and-navigation slice.

F9 — conflict comparisons. A real stale-``expected_rev`` rejection over HTTP
drives the studio's OWN conflict modal (the ``tokenFetch`` product path). With
both revisions retained in the undo-snapshot ring the comparison renders,
labelled as what it actually is (baseline vs current); with a genuinely
unsnapshotted current revision it states a specific truthful unavailability.
Rejections stay side-effect-free (no snapshot or accepted history is created
for a rejected action), the token denial contract is unchanged, and a newer
conflict owns the modal over a superseded conflict's held diff/retry
response.

F10 — exact-identity navigation. Jump resolves the retained record by exact
id through grouping, the virtualized window, filters, and the display cap;
reverse history→comment links reconcile scope/archive/collapse visibility;
missing records get a terminal explanation, and every assertion checks the
actual browser focus identity, not merely that a panel opened.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from browser_support import live_server, launch_chromium, sync_playwright

from conftest import okf_module_argv, okf_subprocess_env

pytestmark = pytest.mark.browser

ALPHA = "notes/alpha"
BETA = "notes/beta"

# A pure EXPRESSION (no arrow) so it can be concatenated into predicates:
# embedding an arrow here made earlier waits compare a function object and
# pass vacuously.
_FOCUSED_ACTIVITY_JS = (
    "((document.activeElement && document.activeElement.dataset) ? "
    "document.activeElement.dataset.activityId || '' : '')")


def _write_bundle(root: Path) -> Path:
    (root / "notes").mkdir(parents=True)
    (root / "index.md").write_text("# Home\n\nIndex.\n", encoding="utf-8")
    (root / "notes" / "alpha.md").write_text(
        "---\ntype: Note\ntitle: Alpha\n---\n\n# Alpha\n\nAlpha body.\n",
        encoding="utf-8")
    (root / "notes" / "beta.md").write_text(
        "---\ntype: Note\ntitle: Beta\n---\n\n# Beta\n\nBeta body.\n",
        encoding="utf-8")
    return root


@pytest.fixture
def studio(tmp_path_factory):
    """One disposable bundle behind one live local-edit server."""
    bundle = _write_bundle(tmp_path_factory.mktemp("f9f10") / "bundle")
    with live_server(bundle) as base:
        yield {"base": base, "bundle": bundle}


@pytest.fixture
def readonly_studio(tmp_path_factory):
    bundle = _write_bundle(tmp_path_factory.mktemp("f9ro") / "bundle")
    with live_server(bundle, "--no-edit") as base:
        yield {"base": base, "bundle": bundle}


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = launch_chromium(p)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        pg = context.new_page()
        pg._diff_requests = []
        pg.on("request", lambda r: pg._diff_requests.append(r.url)
              if "/__diff" in r.url else None)
        pg._page_errors = []
        pg.on("pageerror", lambda e: pg._page_errors.append(str(e)))
        yield pg
        context.close()
        browser.close()


# --- page-side helpers (supported surfaces only) ---------------------------

_REV_JS = """async (target) => {
  const r = await fetch('/__data/doc?id=' + encodeURIComponent(target),
                        {headers: {Accept: 'application/json'}});
  const d = await r.json();
  return d.rev;
}"""

_APPLY_JS = """async ({token, target, tag, expected_rev}) => {
  const body = {kind: 'add_tag', target, args: {tag}};
  if (expected_rev) body.expected_rev = expected_rev;
  const res = await fetch('/__apply', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-OKF-Token': token},
    body: JSON.stringify(body),
  });
  return {status: res.status, body: await res.json().catch(() => ({}))};
}"""

_COMMENT_JS = """async ({token, concept, body}) => {
  const res = await fetch('/__comment', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-OKF-Token': token},
    body: JSON.stringify({concept, body, anchor: {kind: 'concept'}, actor: 'user'}),
  });
  return {status: res.status, body: await res.json().catch(() => ({}))};
}"""


def _goto_alpha(page, base):
    page.goto(f"{base}/{ALPHA}", wait_until="load")
    page.wait_for_function("() => typeof window.okfLoomStudio === 'object'",
                           timeout=10000)
    page.wait_for_function(
        "() => !!(window.okfLoomStudio.state.doc && window.okfLoomStudio.state.doc.id)",
        timeout=10000)
    # Both canonical feeds must have settled before a test mutates or reads
    # session state, so an in-flight initial fetch can never replace seeded
    # events or race an assertion.
    page.wait_for_function(
        "() => window.okfLoomStudio.state.commentsStatus === 'ready'",
        timeout=10000)
    page.wait_for_function(
        "() => window.okfLoomStudio.state.eventsReady === true", timeout=10000)


def _token(page):
    return page.evaluate(
        "window.__OKF_LOOM_STUDIO__ && window.__OKF_LOOM_STUDIO__.token || ''")


def _seed_two_revisions(page, target, tag1, tag2):
    """Two real applies put R0 AND R1 into the undo-snapshot ring.

    W1 snapshots R0 and writes R1; W2 snapshots R1 and writes R2 (disk).
    Returns (r0, r1, r2) content hashes.
    """
    token = _token(page)
    r0 = page.evaluate(_REV_JS, target)
    w1 = page.evaluate(_APPLY_JS, {"token": token, "target": target,
                                   "tag": tag1})
    assert w1["status"] == 200, f"W1 failed: {w1}"
    r1 = page.evaluate(_REV_JS, target)
    assert r0 != r1
    w2 = page.evaluate(_APPLY_JS, {"token": token, "target": target,
                                   "tag": tag2})
    assert w2["status"] == 200, f"W2 failed: {w2}"
    r2 = page.evaluate(_REV_JS, target)
    assert r1 != r2
    return r0, r1, r2


def _wait_disk_rev_changed(page, target, old_rev, timeout=15000):
    """Wait until the watcher has actually picked up an EXTERNAL edit.

    Polls /__data/doc until its rev differs from ``old_rev``; a same-value
    comparison can pass trivially while the bundle cache is stale, so the
    caller compares against the KNOWN pre-edit rev.
    """
    page.wait_for_function(
        "async ([target, oldRev]) => {\n"
        "  const r = await fetch('/__data/doc?id=' + encodeURIComponent(target),\n"
        "                        {headers: {Accept: 'application/json'}});\n"
        "  const d = await r.json();\n"
        "  return d.rev !== oldRev;\n"
        "}", arg=[target, old_rev], timeout=timeout)


def _wait_disk_rev(page, target, expected, timeout=15000):
    page.wait_for_function(
        "async ([target, expected]) => {\n"
        "  const r = await fetch('/__data/doc?id=' + encodeURIComponent(target),\n"
        "                        {headers: {Accept: 'application/json'}});\n"
        "  const d = await r.json();\n"
        "  return d.rev === expected;\n"
        "}", arg=[target, expected], timeout=timeout)


def _wait_events_stable(bundle: Path, timeout=15.0):
    """Wait until events.jsonl stops growing (watcher quiescence)."""
    deadline = time.monotonic() + timeout
    last = _events_count(bundle)
    while time.monotonic() < deadline:
        time.sleep(1.0)
        current = _events_count(bundle)
        if current == last:
            return
        last = current
    raise AssertionError("events.jsonl never settled")


def _history_fingerprint(bundle: Path) -> list[str]:
    history = bundle / ".okf-loom" / "session" / "history"
    if not history.is_dir():
        return []
    return sorted(f"{p.relative_to(history)}:{p.stat().st_size}"
                  for p in history.rglob("*") if p.is_file())


def _events_count(bundle: Path) -> int:
    events = bundle / ".okf-loom" / "session" / "events.jsonl"
    if not events.is_file():
        return 0
    return sum(1 for line in events.read_text(encoding="utf-8").splitlines()
               if line.strip())


def _external_edit(bundle: Path, rel: str, marker: str) -> None:
    """Simulate an external editor writing bytes no studio write captured."""
    path = bundle / rel
    raw = path.read_bytes()
    path.write_bytes(raw + f"\n\nExternal editor note {marker}.\n".encode("utf-8"))


def _real_conflict(page, target, expected_rev, tag):
    """Drive the studio's own tokenFetch into a real HTTP 409 conflict.

    The modal opens through the product path (tokenFetch intercepts the 409).
    Fire-and-record: the evaluate returns while the modal is open (the
    caller's tokenFetch promise resolves only when the modal is settled, so
    awaiting it here would deadlock against the settlement click below); the
    caller's outcome is recorded when the user settles the modal.
    """
    page.evaluate(
        """({target, expected_rev, tag}) => {
          window.__conflictOutcome = null;
          window.okfLoomStudio.ctx().tokenFetch('/__apply', {
            method: 'POST',
            body: {kind: 'add_tag', target, args: {tag}, expected_rev},
          }).then(async (res) => {
            if (!res) { window.__conflictOutcome = {gone: true}; return; }
            const body = await res.clone().json().catch(() => ({}));
            window.__conflictOutcome = {status: res.status, ok: res.ok, body};
          }, (err) => { window.__conflictOutcome = {error: String(err)}; });
        }""",
        {"target": target, "expected_rev": expected_rev, "tag": tag},
    )


# ===========================================================================
# F9 — conflict comparison availability, labelling, and side-effect freedom
# ===========================================================================


def test_f9_unsnapshotted_conflict_states_truthful_unavailable_comparison(
        studio, page):
    """The genuine conflict: an external editor changed bytes that were never
    snapshotted, so the agent's stale expected_rev cannot show a comparison.
    The modal must say exactly that — and the rejected action must leave the
    on-disk bytes, the snapshot ring, and the accepted history untouched."""
    base, bundle = studio["base"], studio["bundle"]
    _goto_alpha(page, base)
    r0, r1, r2 = _seed_two_revisions(page, ALPHA, "f9u-w1", "f9u-w2")
    # External edit: novel bytes C, never snapshotted by any studio write.
    _external_edit(bundle, "notes/alpha.md", "f9u-external")
    _wait_disk_rev_changed(page, ALPHA, r2)  # watcher reloaded the bundle
    _wait_events_stable(bundle)  # the watcher's disk-change event is in
    r_disk = page.evaluate(_REV_JS, ALPHA)
    assert r_disk != r2

    bytes_before = (bundle / "notes" / "alpha.md").read_bytes()
    history_before = _history_fingerprint(bundle)
    events_before = _events_count(bundle)

    # Real conflict through the product path: tokenFetch intercepts the 409.
    _real_conflict(page, ALPHA, r2, "f9u-rejected")
    page.wait_for_selector(".okf-conflict-overlay:not([hidden])", timeout=5000)
    heading = page.locator(".okf-conflict__title").inner_text()
    assert ALPHA in heading
    # "Keep mine": the non-destructive settlement. The caller must see the
    # original 409 (its write did not land).
    page.get_by_role("button", name="Keep mine").click()
    page.wait_for_function("() => window.__conflictOutcome !== null",
                           timeout=5000)
    outcome = page.evaluate("window.__conflictOutcome")
    assert outcome["status"] == 409 and outcome["ok"] is False
    assert outcome["body"]["conflict"] is True
    assert outcome["body"]["expected_rev"] == r2
    assert outcome["body"]["current_rev"] == r_disk

    # Re-trigger the same conflict and ask for the comparison.
    _real_conflict(page, ALPHA, r2, "f9u-rejected-2")
    page.wait_for_selector(".okf-conflict-overlay:not([hidden])", timeout=5000)
    page.locator(".okf-conflict__diff-btn").click()
    page.wait_for_selector(".okf-conflict__diff-empty", timeout=5000)
    message = page.locator(".okf-conflict__diff-empty").inner_text()
    assert "Comparison unavailable" in message
    assert "not retained" in message and "snapshot history" in message
    assert page.locator(".okf-conflict__diff-table").count() == 0, (
        "no diff table may be rendered for an unsnapshotted revision")
    # The caption names the comparison sides without claiming proposed bytes.
    caption = page.locator(".okf-conflict__diff-caption").inner_text()
    assert "Baseline" in caption and "Current" in caption
    page.get_by_role("button", name="Keep mine").click()
    page.wait_for_function("() => window.__conflictOutcome !== null",
                           timeout=5000)

    # Side-effect freedom: rejected writes never manufacture snapshots or
    # accepted history, and never touch the canonical bytes.
    assert (bundle / "notes" / "alpha.md").read_bytes() == bytes_before
    assert _history_fingerprint(bundle) == history_before
    assert _events_count(bundle) == events_before
    # The token denial contract is unchanged: /__diff without the token is 403.
    denial = page.evaluate(
        """async () => {
          const r = await fetch('/__diff?concept=notes%2Falpha&from=a&to=b');
          return {status: r.status, text: await r.text()};
        }""")
    assert denial["status"] == 403
    assert "Forbidden" in denial["text"]
    assert page._page_errors == [], f"unexpected page errors: {page._page_errors}"


def test_f9_snapshotted_conflict_renders_labelled_baseline_current_diff(
        studio, page):
    """When both conflict revisions ARE retained, the comparison renders and
    is labelled what it actually is: the baseline the agent's edit was
    prepared against versus the retained current revision — never 'your edit
    vs the agent's proposed bytes'."""
    base, bundle = studio["base"], studio["bundle"]
    _goto_alpha(page, base)
    r0_bytes = (bundle / "notes" / "alpha.md").read_bytes()
    r0, r1, r2 = _seed_two_revisions(page, ALPHA, "f9s-w1", "f9s-w2")
    # External edit that restores R0's exact bytes (a user-side revert): revs
    # are content hashes, so the retained current revision IS r0 and a real
    # comparison exists against the snapshotted r1.
    (bundle / "notes" / "alpha.md").write_bytes(r0_bytes)
    _wait_disk_rev(page, ALPHA, r0)

    _real_conflict(page, ALPHA, r1, "f9s-rejected")
    page.wait_for_selector(".okf-conflict-overlay:not([hidden])", timeout=5000)
    page.locator(".okf-conflict__diff-btn").click()
    page.wait_for_selector(".okf-conflict__diff-table", timeout=5000)
    rows = page.locator(".okf-conflict__diff-row--add, "
                        ".okf-conflict__diff-row--del").count()
    assert rows > 0, "expected real add/del rows for the retained comparison"
    caption = page.locator(".okf-conflict__diff-caption").inner_text()
    assert "Baseline" in caption and r1 in caption
    assert "Current" in caption and r0 in caption
    assert "proposed bytes are not shown" in caption
    # The accessible name describes the actual comparison, too.
    aria = page.locator(".okf-conflict__diff").get_attribute("aria-label")
    assert "baseline" in aria and "current" in aria
    assert "your edit" not in aria
    page.get_by_role("button", name="Keep mine").click()
    assert page._page_errors == [], f"unexpected page errors: {page._page_errors}"


def test_f9_newer_conflict_owns_modal_over_held_responses(studio, page):
    """Q4 ownership, proven over COMPLETED real responses: a held diff (and a
    held force-retry) response belonging to conflict A must not render into,
    re-enable, close, settle, or steal focus from a newer conflict B —
    including A's delayed 500 ms re-enable callback, which must fire while
    B's OWN comparison is still in flight and B's diff button must stay busy.

    Determinism: every gate wraps the real fetch (nothing is fabricated); a
    release starts the real HTTP exchange and the product is handed the
    response only once it fully arrived. Each gate records delivery, so
    assertions run only after actual response delivery plus the product's
    own 500 ms re-enable window — the stale continuation (session guard drop
    and its scheduled timer) has genuinely executed by then, with no fixed
    sleep guesses. Non-vacuity: B's modal auto-focus SKIPS its busy diff
    button (the product's own focus logic observed the disabled state), and
    B's own re-enable timer later flips that same button back on, proving
    the timer mechanism fires in this environment."""
    base, bundle = studio["base"], studio["bundle"]
    # Hold /__diff comparisons and one forced /__apply retry inside the page.
    page.add_init_script(
        """
        (() => {
          const realFetch = window.fetch.bind(window);
          // Finite test gates around the REAL fetch: a held request starts
          // only when its gate is released, and the product sees the
          // response only once the real exchange has COMPLETED. Delivery
          // (time + status) is recorded per gate so the test can wait for
          // actual response arrival plus handler continuation.
          const mkGate = () => ({ assigned: false, released: false,
                                  delivered: false, deliveredAt: 0, status: 0,
                                  releasePromise: null, _rel: null });
          window.__gates = { diff: [], apply: null };
          window.__held = { diff: 0, apply: 0 };
          const arm = () => {
            const gate = mkGate();
            gate.releasePromise = new Promise((r) => {
              gate._rel = () => { gate.released = true; r(); };
            });
            return gate;
          };
          window.__armDiffGate = () => {
            const gate = arm();
            window.__gates.diff.push(gate);
            return window.__gates.diff.length - 1;
          };
          window.__armApplyGate = () => { window.__gates.apply = arm(); };
          window.__releaseGate = (kind, i) => {
            const gate = (kind === 'diff') ? window.__gates.diff[i]
                                           : window.__gates.apply;
            gate._rel();
          };
          const deliver = (gate, res) => {
            gate.delivered = true;
            gate.deliveredAt = performance.now();
            gate.status = res.status;
            return res;
          };
          window.fetch = function (input, init) {
            const url = (typeof input === 'string') ? input
                        : (input && input.url) || '';
            if (url.indexOf('/__diff') !== -1) {
              const gate = window.__gates.diff.find((g) => !g.assigned);
              if (gate) {
                gate.assigned = true;
                window.__held.diff += 1;
                return gate.releasePromise.then(() => realFetch(input, init))
                  .then((res) => deliver(gate, res));
              }
            }
            if (url.indexOf('/__apply') !== -1 && window.__gates.apply &&
                !window.__gates.apply.assigned) {
              const gate = window.__gates.apply;
              gate.assigned = true;
              window.__held.apply += 1;
              return gate.releasePromise.then(() => realFetch(input, init))
                .then((res) => deliver(gate, res));
            }
            return realFetch(input, init);
          };
        })();
        """)
    _goto_alpha(page, base)

    def stage_conflict(target, rev, tag, record):
        page.evaluate(
            """({target, rev, tag, record}) => {
              window[record] = null;
              window.okfLoomStudio.ctx().tokenFetch('/__apply', {
                method: 'POST',
                body: {kind: 'add_tag', target, args: {tag}, expected_rev: rev},
              }).then((res) => {
                if (!res) { window[record] = {gone: true}; return; }
                const clone = res.clone();
                return clone.json().then((b) => {
                  window[record] = {status: res.status, ok: res.ok, body: b};
                });
              });
            }""",
            {"target": target, "rev": rev, "tag": tag, "record": record},
        )

    def stage_external_conflict(target, tag):
        """Seed one real write, then edit the file externally. Returns the
        now-stale rev the next apply will conflict against. The conflict
        check reads the file fresh from disk, so no watcher wait is needed."""
        token = _token(page)
        r0 = page.evaluate(_REV_JS, target)
        w1 = page.evaluate(_APPLY_JS, {"token": token, "target": target,
                                       "tag": tag})
        assert w1["status"] == 200
        _external_edit(bundle, f"{target}.md", f"own-{tag}")
        return r0

    def stage_retained_revert_conflict(target, tag):
        """A conflict whose comparison genuinely exists: two real writes
        snapshot R0 and R1 (disk reaches R2), then an external edit restores
        R0's exact bytes, so the stale expected R1 and the retained current
        R0 are both in the snapshot ring and /__diff returns a real diff."""
        token = _token(page)
        original = (bundle / f"{target}.md").read_bytes()
        r0 = page.evaluate(_REV_JS, target)
        w1 = page.evaluate(_APPLY_JS, {"token": token, "target": target,
                                       "tag": tag + "-w1"})
        assert w1["status"] == 200
        r1 = page.evaluate(_REV_JS, target)
        assert r0 != r1
        w2 = page.evaluate(_APPLY_JS, {"token": token, "target": target,
                                       "tag": tag + "-w2"})
        assert w2["status"] == 200
        r2 = page.evaluate(_REV_JS, target)
        assert r1 != r2
        (bundle / f"{target}.md").write_bytes(original)
        _wait_disk_rev(page, target, r0)
        return r1, r0

    def gate(kind, i):
        return page.evaluate(
            "([kind, i]) => { const g = (kind === 'diff') ? "
            "window.__gates.diff[i] : window.__gates.apply; return "
            "{assigned: g.assigned, released: g.released, "
            "delivered: g.delivered, status: g.status}; }", [kind, i])

    def wait_gate_delivered(kind, i, timeout=15000):
        page.wait_for_function(
            "([kind, i]) => { const g = (kind === 'diff') ? "
            "window.__gates.diff[i] : window.__gates.apply; "
            "return !!(g && g.delivered); }", arg=[kind, i], timeout=timeout)

    def wait_after_delivery(kind, i, ms, timeout=15000):
        """Observe a window after the real fetch Response becomes available.

        This does not prove body buffering or every callback's execution. The
        stale ownership checks precede body consumption; the diff window also
        covers the 500 ms timer, with B's re-enable as a positive timer control.
        """
        page.wait_for_function(
            "([kind, i, ms]) => { const g = (kind === 'diff') ? "
            "window.__gates.diff[i] : window.__gates.apply; "
            "return !!(g && g.delivered && "
            "(performance.now() - g.deliveredAt) >= ms); }",
            arg=[kind, i, ms], timeout=timeout)

    # --- phase 1: A's diff held; B delivered WITH its own held comparison ---
    a_expected = stage_external_conflict(ALPHA, "own-a")
    a_gate = page.evaluate("window.__armDiffGate()")
    stage_conflict(ALPHA, a_expected, "conflict-a", "__outcomeA")
    page.wait_for_selector(".okf-conflict-overlay:not([hidden])", timeout=5000)
    page.locator(".okf-conflict__diff-btn").click()
    page.wait_for_function("() => window.__held.diff === 1", timeout=5000)

    # B gets its own busy comparison: its gate is armed first, and a
    # MutationObserver clicks B's diff button in the same task that shows
    # B's modal — BEFORE the modal's 20 ms focus timer — so the product's
    # own focus logic must skip the (now-disabled) busy button and choose
    # "Keep mine". That choice is recorded proof the busy state is observed.
    b_gate = page.evaluate("window.__armDiffGate()")
    page.evaluate(
        """
        () => {
          window.__bDiffStarted = false;
          const overlay = document.querySelector('.okf-conflict-overlay');
          const mo = new MutationObserver(() => {
            const t = document.querySelector('.okf-conflict__title');
            if (window.__bDiffStarted || !t) return;
            if (t.textContent.indexOf('notes/beta') === -1) return;
            window.__bDiffStarted = true;
            mo.disconnect();
            document.querySelector('.okf-conflict__diff-btn').click();
          });
          mo.observe(overlay, {childList: true, characterData: true,
                               subtree: true});
        }
        """)
    b_stale, b_current = stage_retained_revert_conflict(BETA, "own-b")
    stage_conflict(BETA, b_stale, "conflict-b", "__outcomeB")
    page.wait_for_function(
        "() => window.__held.diff === 2 && window.__outcomeA && "
        "window.__outcomeA.status === 409 "
        "&& document.querySelector('.okf-conflict__title').textContent.includes('notes/beta')",
        timeout=5000)
    a_outcome = page.evaluate("__outcomeA")
    assert a_outcome["ok"] is False and a_outcome["body"]["concept"] == ALPHA, (
        f"A must settle with its own original 409 at B delivery: {a_outcome}")
    # B's chosen focus: the modal's own selection with its busy diff button
    # skipped — the exact DOM identity held for the whole ownership window.
    page.wait_for_function(
        "() => !!document.activeElement && "
        "!!document.activeElement.closest('[role=alertdialog]')", timeout=5000)
    chosen = page.evaluate(
        "() => { window.__bFocus = document.activeElement; "
        "return window.__bFocus.textContent; }")
    assert chosen == "Keep mine", (
        f"the modal must skip B's busy diff button: chose {chosen!r}")
    assert page.locator(".okf-conflict__diff-btn").is_disabled()
    assert page.locator(".okf-conflict__diff-loading").is_visible()
    assert page.locator(".okf-conflict__diff-table").count() == 0
    assert page.evaluate("window.__outcomeB") is None

    # Release A's diff: the real exchange runs, the response is delivered to
    # the (superseded) session, and its continuation executes — including the
    # 500 ms delayed re-enable timer — while B's comparison is still held.
    page.evaluate(f"window.__releaseGate('diff', {a_gate})")
    wait_gate_delivered("diff", a_gate)
    assert gate("diff", a_gate)["status"] == 404, (
        "A's real comparison must have completed (unsnapshotted external bytes)")
    wait_after_delivery("diff", a_gate, 700)
    # Stale isolation, asserted only after A's handler continuation AND its
    # delayed re-enable callback genuinely ran:
    assert gate("diff", b_gate)["delivered"] is False, (
        "B's comparison must still be in flight across the whole window")
    assert page.locator(".okf-conflict__diff-table").count() == 0, (
        "superseded diff rows must not render into the newer conflict")
    assert page.locator(".okf-conflict__diff-empty").count() == 0, (
        "the superseded 404 message must not render into the newer conflict")
    assert page.locator(".okf-conflict__diff-loading").is_visible()
    assert page.locator(".okf-conflict__diff-btn").is_disabled(), (
        "the stale session's delayed re-enable must not touch B's busy button")
    assert page.evaluate("() => document.activeElement === window.__bFocus"), (
        "exact chosen focus identity must not move")
    assert page.evaluate("window.__outcomeB") is None, (
        "B's caller must remain unsettled")
    assert page.evaluate("__outcomeA") == a_outcome, (
        "A settles exactly once, with its original 409 — never the diff result")

    # Release B's comparison: the REAL diff for B's own revisions renders.
    page.evaluate(f"window.__releaseGate('diff', {b_gate})")
    wait_gate_delivered("diff", b_gate)
    assert gate("diff", b_gate)["status"] == 200
    page.wait_for_selector(".okf-conflict__diff-table", timeout=5000)
    caption = page.locator(".okf-conflict__diff-caption").inner_text()
    assert b_stale in caption and b_current in caption, caption
    assert a_expected not in caption, (
        "the rendered comparison must be B's own, never A's")
    # Positive control for the timer mechanism: B's OWN delayed re-enable
    # (the same 500 ms timer the stale session was guarded against) fires —
    # so the disabled-button assertion above can never pass vacuously.
    wait_after_delivery("diff", b_gate, 700)
    assert page.locator(".okf-conflict__diff-btn").is_disabled() is False
    assert page.evaluate("() => document.activeElement === window.__bFocus")
    # Explicit dismissal: only now does B's caller settle, with B's own 409.
    page.get_by_role("button", name="Keep mine").click()
    page.wait_for_function("() => window.__outcomeB !== null", timeout=5000)
    b_outcome = page.evaluate("__outcomeB")
    assert b_outcome["status"] == 409 and b_outcome["ok"] is False
    assert b_outcome["body"]["concept"] == BETA
    assert page.locator(".okf-conflict-overlay").is_hidden()

    # --- phase 2: A2's force-retry held; B2 delivered; the late retry
    # response (a real, accepted server write) must not close/settle/refocus.
    c_expected = stage_external_conflict(ALPHA, "own-c")
    stage_conflict(ALPHA, c_expected, "conflict-c", "__outcomeC")
    page.wait_for_selector(".okf-conflict-overlay:not([hidden])", timeout=5000)
    # Arm AFTER the modal opens so the gate holds the RETRY (the next
    # /__apply), never conflict C's own rejected request.
    page.evaluate("window.__armApplyGate()")
    # Explicit user force-retry (existing authority). The retry itself is a
    # write the user accepted; only its RESPONSE ownership is under test.
    page.get_by_role("button", name="Take the agent's edit").click()
    page.wait_for_function("() => window.__held.apply === 1", timeout=5000)
    alpha_rev_before_retry = page.evaluate(_REV_JS, ALPHA)
    d_expected = stage_external_conflict(BETA, "own-d")
    stage_conflict(BETA, d_expected, "conflict-d", "__outcomeD")
    page.wait_for_function(
        "() => window.__outcomeC && window.__outcomeC.status === 409 "
        "&& document.querySelector('.okf-conflict__title').textContent.includes('notes/beta')",
        timeout=5000)
    c_outcome = page.evaluate("__outcomeC")
    assert c_outcome["ok"] is False and c_outcome["body"]["concept"] == ALPHA
    page.wait_for_function(
        "() => !!document.activeElement && "
        "!!document.activeElement.closest('[role=alertdialog]')", timeout=5000)
    d_chosen = page.evaluate(
        "() => { window.__dFocus = document.activeElement; "
        "return window.__dFocus.textContent; }")
    assert d_chosen == "View diff", (
        f"B2's fresh modal must focus its own diff button: chose {d_chosen!r}")

    # Release the held retry: the real forced write executes and its late
    # response is delivered to the superseded session only.
    page.evaluate("window.__releaseGate('apply', 0)")
    wait_gate_delivered("apply", 0)
    assert gate("apply", 0)["status"] == 200, (
        "the user's explicit retry is a real accepted write")
    wait_after_delivery("apply", 0, 300)
    # Ownership, not cancellation: the accepted write DID land server-side.
    _wait_disk_rev_changed(page, ALPHA, alpha_rev_before_retry)
    # The superseded retry response must not close/settle/refocus B2.
    assert not page.locator(".okf-conflict-overlay").is_hidden()
    assert BETA in page.locator(".okf-conflict__title").inner_text()
    assert page.evaluate("window.__outcomeD") is None, (
        "B2's caller must remain unsettled by the superseded retry response")
    assert page.evaluate("() => document.activeElement === window.__dFocus"), (
        "the late retry response must not refocus")
    assert page.evaluate("__outcomeC") == c_outcome, (
        "C stays settled exactly once, with its original 409")
    # Explicit dismissal: B2 settles with B2's own response — never C's.
    page.get_by_role("button", name="Keep mine").click()
    page.wait_for_function("() => window.__outcomeD !== null", timeout=5000)
    d_outcome = page.evaluate("__outcomeD")
    assert d_outcome["status"] == 409 and d_outcome["body"]["concept"] == BETA
    assert page.locator(".okf-conflict-overlay").is_hidden()
    assert page.evaluate("__outcomeA")["body"]["concept"] == ALPHA, (
        "A's original settlement is never rewritten by any later response")
    assert page._page_errors == [], f"unexpected page errors: {page._page_errors}"


def test_f9_readonly_and_public_sessions_never_launch_doomed_diff_requests(
        readonly_studio, page):
    """Where the bootstrap authority cannot read /__diff, the modal must not
    offer the action doomed to 403 — it must say why instead. Local read-only
    (no token) and a public-comment bootstrap shape (token but public
    ingress) are both derived from the actual bootstrap, not from edit."""
    base = readonly_studio["base"]
    _goto_alpha(page, base)
    assert page.evaluate("window.__OKF_LOOM_STUDIO__.token") == ""
    # Brace body: evaluate must not await showConflictModal's settlement
    # promise (it resolves only when the modal is settled).
    page.evaluate(
        "() => { window.okfLoomStudio._showConflictModal("
        "{concept: 'notes/alpha', conflict: true, expected_rev: 'x', current_rev: 'y'}); }")
    page.wait_for_selector(".okf-conflict-overlay:not([hidden])", timeout=5000)
    assert page.locator(".okf-conflict__diff-btn").is_hidden()
    note = page.locator(".okf-conflict__diff-na")
    assert note.is_visible()
    assert "read-only view" in note.inner_text()
    # No privileged request was launched, and none can be launched by clicking.
    assert page._diff_requests == []
    page.locator(".okf-conflict__diff-na").click(timeout=1000)
    assert page._diff_requests == []
    page.keyboard.press("Escape")
    page.wait_for_selector(".okf-conflict-overlay[hidden]", state="attached",
                           timeout=5000)

    # Public-comment bootstrap shape: a token exists, but public ingress is
    # route-denied for /__diff — the gate must still refuse the action.
    page2 = page.context.new_page()
    page2.add_init_script(
        "window.__OKF_LOOM_STUDIO__ = {token: 'public-session-token',"
        " edit: false, comment: true, presence: false,"
        " authority_mode: 'public-comment', rev: 'x'};")
    page2.goto(f"{base}/{ALPHA}", wait_until="load")
    page2.wait_for_function(
        "() => typeof window.okfLoomStudio === 'object'", timeout=10000)
    page2.evaluate(
        "() => { window.okfLoomStudio._showConflictModal("
        "{concept: 'notes/alpha', conflict: true, expected_rev: 'x', current_rev: 'y'}); }")
    page2.wait_for_selector(".okf-conflict-overlay:not([hidden])", timeout=5000)
    assert page2.locator(".okf-conflict__diff-btn").is_hidden()
    note2 = page2.locator(".okf-conflict__diff-na")
    assert "public links" in note2.inner_text()
    page2.close()


# ===========================================================================
# F10 — exact-identity reveal through grouping, windowing, filters, caps
# ===========================================================================

# Seeded newest-last so unshift order (top of the timeline) is:
#   solo(0), group(1), deep rows (2..201), cap rows (202..1211).
# Timestamps step 2s apart (> the 1s burst window) so burst coalescing never
# collapses the standalone rows this fixture depends on.
_SEED_EVENTS_JS = """() => {
  const hub = window.okfLoomLive;
  const base = Date.now();
  let seq = 0;
  const mk = (id) => ({
    id, actor: 'agent', action: 'add_tag', ids: ['notes/alpha'],
    summary: 'seed ' + id, undoable: false,
    ts: new Date(base - (1240 - seq++) * 2000).toISOString(),
  });
  for (let i = 0; i < 1010; i++) hub.emit('activity', mk('reveal-cap-' + i));
  for (let i = 0; i < 200; i++) hub.emit('activity', mk('reveal-deep-' + i));
  hub.emit('activity', Object.assign(mk('reveal-grp-a'), {group_id: 'reveal-grp'}));
  hub.emit('activity', Object.assign(mk('reveal-grp-b'), {group_id: 'reveal-grp'}));
  hub.emit('activity', mk('reveal-solo'));
  return window.okfLoomStudio.state.events.length;
}"""


def test_f10_reveal_resolves_exact_identity_through_group_window_filter_cap(
        studio, page):
    """One fixture journey: the retained record must be reachable whether it
    is a standalone row, a member of a grouped pass, outside the mounted
    virtualizer window, hidden behind an active filter, or behind the
    'show more' display cap — and 'jump' must assert exact identity, not
    light up whatever row happens to be mounted."""
    base = studio["base"]
    _goto_alpha(page, base)
    total = page.evaluate(_SEED_EVENTS_JS)
    assert total >= 1213, f"fixture seeding incomplete: {total}"
    page.evaluate("window.okfLoomStudio.openPanel('changes')")
    page.wait_for_selector(".okf-changes__rows .okf-change", timeout=5000)
    # The display cap is up front before any scroll (G10).
    cap_note = page.locator(".okf-changes__cap-note")
    assert cap_note.is_visible()
    assert "1000" in cap_note.inner_text()

    # 1. Standalone row, exact focus identity.
    page.evaluate("window.okfLoomStudio.revealActivity('reveal-solo')")
    page.wait_for_function(
        f"() => {_FOCUSED_ACTIVITY_JS} === 'reveal-solo'", timeout=5000)
    assert page.evaluate(
        "() => document.activeElement.classList.contains('okf-pulse')")

    # 2. Grouped record: the owning group opens; the exact member is focused.
    page.evaluate("window.okfLoomStudio.revealActivity('reveal-grp-a')")
    page.wait_for_function(
        f"() => {_FOCUSED_ACTIVITY_JS} === 'reveal-grp-a'", timeout=5000)
    owner = page.evaluate(
        "() => { const el = document.activeElement; const d = el.closest('details');"
        " return {open: !!(d && d.open), group: !!(d && d.classList.contains("
        "'okf-change--group'))}; }")
    assert owner == {"open": True, "group": True}

    # 3. Off-window record (ordered index ~196, past the 80-row mounted
    # window, still inside the display cap — the note must survive this one).
    page.evaluate("window.okfLoomStudio.revealActivity('reveal-deep-5')")
    page.wait_for_function(
        f"() => {_FOCUSED_ACTIVITY_JS} === 'reveal-deep-5'", timeout=5000)
    assert page.locator(".okf-changes__cap-note").is_visible()

    # 4. Filtered-out record: only the conflicting filter is cleared.
    page.locator('select[aria-label="Filter by actor"]').select_option("user")
    page.wait_for_selector(".okf-empty-state", timeout=5000)
    page.evaluate("window.okfLoomStudio.revealActivity('reveal-deep-10')")
    page.wait_for_function(
        f"() => {_FOCUSED_ACTIVITY_JS} === 'reveal-deep-10'", timeout=5000)
    assert page.locator(
        'select[aria-label="Filter by actor"]').input_value() == ""

    # 5. Load boundary: a record behind the 1000-row cap raises the ceiling.
    page.evaluate("window.okfLoomStudio.revealActivity('reveal-cap-100')")
    page.wait_for_function(
        f"() => {_FOCUSED_ACTIVITY_JS} === 'reveal-cap-100'", timeout=5000)
    assert page.locator(".okf-changes__cap-note").count() == 0, (
        "the cap note must be gone once the reveal raised the ceiling")

    # 6. Absent record: terminal, focused explanation — never a wrong row.
    page.evaluate("window.okfLoomStudio.revealActivity('reveal-ghost')")
    page.wait_for_selector(".okf-panel-reveal-note", timeout=5000)
    note = page.locator(".okf-panel-reveal-note")
    assert "isn't available in the retained history" in note.inner_text()
    page.wait_for_function(
        "() => document.activeElement && "
        "document.activeElement.classList.contains('okf-panel-reveal-note')",
        timeout=5000)
    assert page.evaluate(
        "() => document.querySelectorAll('.okf-change.okf-pulse').length") == 0
    assert page.evaluate(
        "window.okfLoomStudio.state.pendingChangeReveal") is None
    assert page._page_errors == [], f"unexpected page errors: {page._page_errors}"


def test_f10_cross_links_reveal_exact_records_or_report_absence(
        studio, page):
    """The real forward journey (resolved comment → Jump) and the reverse
    history→comment back-link: both must land on the exact record with real
    focus, reconcile archived/collapsed visibility, and state absence
    truthfully for a comment that is not retained."""
    base, bundle = studio["base"], studio["bundle"]
    _goto_alpha(page, base)
    token = _token(page)

    # A real attributed write → its activity id from the session feed.
    apply_res = page.evaluate(_APPLY_JS, {"token": token, "target": ALPHA,
                                          "tag": "f10-crosslink"})
    assert apply_res["status"] == 200
    deadline = time.monotonic() + 10
    activity_id = None
    while time.monotonic() < deadline and activity_id is None:
        events_file = bundle / ".okf-loom" / "session" / "events.jsonl"
        if events_file.is_file():
            for line in reversed(
                    events_file.read_text(encoding="utf-8").splitlines()):
                row = json.loads(line)
                if row.get("action") == "add_tag" and row.get("id"):
                    activity_id = row["id"]
                    break
        if activity_id is None:
            time.sleep(0.2)
    assert activity_id, "add_tag activity id never reached events.jsonl"

    # A real page-level comment, then the real resolve CLI linking activity.
    created = page.evaluate(_COMMENT_JS, {
        "token": token, "concept": ALPHA, "body": "please apply the crosslink"})
    assert created["status"] == 201, f"comment create failed: {created}"
    comment_id = created["body"]["comment"]["id"]
    resolve = subprocess.run(
        okf_module_argv("comment-resolve", str(bundle), comment_id,
                        "--activity", activity_id, "--summary", "did it"),
        cwd=str(bundle.parent), env=okf_subprocess_env(),
        capture_output=True, text=True, timeout=60)
    assert resolve.returncode == 0, resolve.stderr

    # Forward: the resolved comment's Jump link reveals the exact row.
    page.wait_for_function(
        "([cid]) => window.okfLoomStudio.state.comments.some("
        "(c) => c.id === cid && c.state === 'resolved')",
        arg=[comment_id], timeout=10000)
    page.wait_for_function(
        "([aid]) => window.okfLoomStudio.state.events.some("
        "(e) => e.type === 'comment_link' && e.activity_id === aid)",
        arg=[activity_id], timeout=10000)
    page.evaluate("window.okfLoomStudio.openPanel('comments')")
    card = page.locator("#comment-" + comment_id)
    resolved_details = card.locator("details").filter(
        has_text="Resolved by")
    resolved_details.locator("summary").click()
    jump = resolved_details.locator("a", has_text="change #")
    assert jump.count() == 1
    jump.click()
    page.wait_for_function(
        f"() => window.okfLoomStudio.state.openPanel === 'changes' && "
        f"{_FOCUSED_ACTIVITY_JS} === {json.dumps(activity_id)}", timeout=5000)

    # Reverse: the change row's back-link reveals the exact saved comment.
    back_link = page.locator(
        f'.okf-change[data-activity-id="{activity_id}"] '
        ".okf-change__comment-link a")
    assert back_link.count() == 1
    back_link.click()
    page.wait_for_function(
        "() => window.okfLoomStudio.state.openPanel === 'comments' && "
        "document.activeElement && document.activeElement.id === "
        f"'comment-{comment_id}'", timeout=5000)

    # Collapsed-thread reconcile: collapse, then the back-link re-expands.
    chevron = page.locator("#comment-" + comment_id + " .okf-comment__chevron")
    chevron.click()
    page.wait_for_function(
        "([cid]) => document.querySelector('#comment-' + cid + ' "
        ".okf-comment__chevron').getAttribute('aria-expanded') === 'false'",
        arg=[comment_id], timeout=5000)
    page.evaluate("window.okfLoomStudio.openPanel('changes')")
    back_link.click()
    page.wait_for_function(
        "() => window.okfLoomStudio.state.openPanel === 'comments' && "
        "document.activeElement && document.activeElement.id === "
        f"'comment-{comment_id}'", timeout=5000)
    assert chevron.get_attribute("aria-expanded") == "true"

    # Archived reconcile: archive the resolved thread (hidden by default),
    # then the back-link must surface it — not pretend it is visible.
    comments = page.evaluate(
        "async () => (await (await fetch('/__comments', "
        "{headers: {Accept: 'application/json'}})).json())")
    rows = comments.get("comments") if isinstance(comments, dict) else comments
    row_token = next(r["row_token"] for r in rows if r["id"] == comment_id)
    # Archive via the CAS update endpoint (/__comment is create-only).
    archived = page.evaluate(
        """async ({token, cid, rowToken}) => {
          const res = await fetch('/__comment-update', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-OKF-Token': token},
            body: JSON.stringify({id: cid, archived: true,
                                  expected_row_token: rowToken}),
          });
          return {status: res.status, body: await res.json().catch(() => ({}))};
        }""", {"token": token, "cid": comment_id, "rowToken": row_token})
    assert archived["status"] == 200, f"archive failed: {archived}"
    page.wait_for_function(
        "([cid]) => window.okfLoomStudio.state.comments.some("
        "(c) => c.id === cid && c.archived === true)",
        arg=[comment_id], timeout=10000)
    page.evaluate("window.okfLoomStudio.openPanel('changes')")
    back_link.click()
    page.wait_for_function(
        "() => window.okfLoomStudio.state.openPanel === 'comments' && "
        "document.activeElement && document.activeElement.id === "
        f"'comment-{comment_id}'", timeout=5000)
    assert page.locator(
        '[aria-label="Comment view controls"] '
        'button:has-text("Hide archived")').is_visible()

    # Absent target: a back-link whose comment is not retained must say so.
    page.evaluate(
        """() => {
          window.okfLoomLive.emit('activity', {
            id: 'orphan-act', actor: 'agent', action: 'add_tag',
            ids: ['notes/alpha'], summary: 'orphan', undoable: false,
            ts: new Date().toISOString(),
          });
          window.okfLoomLive.emit('comment_link', {
            type: 'comment_link', activity_id: 'orphan-act',
            comment_id: 'ghost-comment', concept: 'notes/alpha',
          });
        }""")
    page.evaluate("window.okfLoomStudio.openPanel('changes')")
    orphan_link = page.locator(
        '.okf-change[data-activity-id="orphan-act"] '
        ".okf-change__comment-link a")
    orphan_link.click()
    page.wait_for_function(
        "() => window.okfLoomStudio.state.openPanel === 'comments' && "
        "document.activeElement && document.activeElement.classList.contains("
        "'okf-panel-reveal-note')", timeout=5000)
    assert "isn't available in the retained comments" in page.locator(
        ".okf-panel-reveal-note").inner_text()
    assert page.evaluate(
        "window.okfLoomStudio.state.pendingCommentReveal") is None
    assert page._page_errors == [], f"unexpected page errors: {page._page_errors}"
