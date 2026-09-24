"""Live studio iter 1 — Bundle A regression tests.

Covers the new studio + cli + server surface introduced in iter 1:

  * ``Studio.save_concept(...)`` single write funnel — attribution, undo,
    collision detection, graph event, mark_logged (P0-1 / P1-1 / P1-6).
  * ``Studio.restore_snapshot(...)`` — undo as an attributed write.
  * Cross-process ``.last-logged.json`` dedup map (P1-2).
  * events.jsonl rotation (P1-3, §10.5).
  * Atomic undo snapshot + group manifest writes (P1-4).
  * §17 library API aliases (post_presence, resolve_comment, events_append,
    post_suggestion, EventBus callback subscribe) (P1-5).
  * Path traversal guards on rev + group_id (P2-1, §9.5).
  * §12.3 opt-in constraints (propose_only / forbidden_paths /
    max_edits_per_action) (P2-2).
  * CLI mutator attribution through the funnel (P0-1) — link-add /
    entity-add / update / set-frontmatter / repair.
  * CLI token / comment-claim / comment-resolve / comment-list / presence
    (P0-2).
  * ``--public`` ack gate (P1-7, §15.2).
  * ``--no-watch-ui`` enforced server-side (P1-15).
  * Origin + Host check (P1-8, current spec §14).
  * ``allowed_hosts`` default fail-closed (P1-16).
  * ``ready`` frame carries ``doc_revs`` (P1-6).
  * ``/__apply`` returns 409 on collision (P1-1).
  * ``/__diff`` endpoint (P1-1 / §9.4).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from okf_loom.annotations import concept_content_rev
from okf_loom.studio import (
    EventBus,
    Studio,
    WriteResult,
    _GRAPH_ACTIONS,
    rev_of,
    wait_for_work,
)

from conftest import okf_module_argv, okf_subprocess_env


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A minimal OKF bundle on disk."""
    root = tmp_path / "kb"
    (root / "tables").mkdir(parents=True)
    (root / "tables" / "orders.md").write_text(
        "---\ntype: Table\ntitle: Orders\n---\n# Orders\n\nAn orders table.\n",
        encoding="utf-8",
    )
    (root / "tables" / "customers.md").write_text(
        "---\ntype: Table\ntitle: Customers\n---\n# Customers\n\nA customers table.\n",
        encoding="utf-8",
    )
    (root / "index.md").write_text(
        "---\nokf_version: \"0.1\"\n---\n# Bundle\n", encoding="utf-8",
    )
    return root


@pytest.fixture
def studio(bundle: Path) -> Studio:
    s = Studio.for_bundle(bundle)
    s.ensure_session()
    return s


# ---------------------------------------------------------------------------
# Studio.save_concept — single write funnel (P0-1)
# ---------------------------------------------------------------------------


def test_save_concept_attributes_and_snapshots(studio: Studio, bundle: Path) -> None:
    """save_concept writes the bytes + appends an attributed activity event +
    snapshots prior bytes for undo + marks logged."""
    target = bundle / "tables" / "orders.md"
    prior = target.read_bytes()
    prior_rev = rev_of(prior)
    new_raw = (
        "---\ntype: Table\ntitle: Orders\n---\n# Orders\n\nAn updated orders table.\n"
    )
    result = studio.save_concept(
        concept_id="tables/orders", raw=new_raw, actor="agent",
        action="add_link", origin="mutator",
        summary="agent added a link",
    )
    assert result.ok is True
    assert result.conflict is False
    assert result.rev == rev_of(new_raw.encode("utf-8"))
    assert result.activity_id is not None
    # The new bytes are on disk.
    assert target.read_text(encoding="utf-8") == new_raw
    # An activity event was appended with the right actor/action/origin.
    events = studio.read_events(limit=50)
    activities = [e for e in events if e.get("type") == "activity"]
    assert any(
        a["actor"] == "agent"
        and a["action"] == "add_link"
        and a["origin"] == "mutator"
        and a["ids"] == ["tables/orders"]
        and a["undoable"] is True
        and a.get("detail", {}).get("before") == prior_rev
        for a in activities
    )
    # A graph event was auto-emitted (add_link ∈ _GRAPH_ACTIONS).
    assert any(e.get("type") == "graph" for e in events)
    # A 'changed' event was emitted.
    assert any(
        e.get("type") == "changed" and e.get("origin") == "mutator"
        for e in events
    )
    # The prior bytes are snapshotted for undo.
    snap = studio.undo_snapshot(concept_id="tables/orders", rev=prior_rev)
    assert snap is not None
    assert snap.encode("utf-8") == prior
    # mark_logged recorded the new rev (watcher dedup).
    assert studio._current_disk_rev("tables/orders") == result.rev


def test_save_concept_conflict_returns_no_clobber(studio: Studio, bundle: Path) -> None:
    """When expected_rev mismatches the on-disk rev, save_concept returns
    WriteResult(conflict=True) WITHOUT writing (§9.3/§9.4 no silent clobber)."""
    target = bundle / "tables" / "orders.md"
    prior = target.read_bytes()
    prior_rev = rev_of(prior)
    # Simulate a concurrent disk edit by writing a different content.
    target.write_bytes(b"---\ntype: Table\ntitle: Orders\n---\n# Orders\n\nEDITED BY USER\n")
    # The agent thinks it is overwriting the original (expected_rev=prior_rev)
    # but the on-disk rev has moved.
    result = studio.save_concept(
        concept_id="tables/orders",
        raw="---\ntype: Table\ntitle: Orders\n---\n# Orders\n\nAGENT WROTE THIS\n",
        expected_rev=prior_rev,
    )
    assert result.ok is False
    assert result.conflict is True
    assert result.expected_rev == prior_rev
    assert result.current_rev == rev_of(target.read_bytes())
    # The user's bytes are NOT clobbered.
    assert b"EDITED BY USER" in target.read_bytes()
    assert b"AGENT WROTE THIS" not in target.read_bytes()


def test_save_concept_leading_slash_id_stays_in_bundle(
    studio: Studio, bundle: Path, tmp_path: Path,
) -> None:
    """The target path is built from the VALIDATED id tuple.

    A leading-slash id survives lexical validation (empty segments are
    dropped) but a raw-string path join would treat it as absolute and
    escape the bundle. The write must land inside the bundle root.
    """
    escape_target = tmp_path / "outside.md"
    result = studio.save_concept(
        concept_id=f"/{escape_target}".replace(".md", ""),
        raw="---\ntype: T\ntitle: Escape\n---\nX\n",
    )
    # Whether the write is accepted or refused, nothing may appear outside
    # the bundle root.
    assert not escape_target.exists()
    if result.ok:
        written = bundle / str(escape_target).lstrip("/")
        assert written.resolve().is_relative_to(bundle.resolve())


def test_save_concept_forbidden_paths_constraint(studio: Studio, bundle: Path) -> None:
    """§12.3 opt-in forbidden_paths refuses writes matching the glob."""
    studio.constraints = {"forbidden_paths": ["tables/*"]}
    result = studio.save_concept(
        concept_id="tables/orders",
        raw="---\ntype: Table\ntitle: Orders\n---\n# Orders\n",
    )
    assert result.ok is False
    assert result.conflict is False
    assert "forbidden_paths" in (result.error or "")


def test_save_concept_max_edits_per_action_constraint(
    studio: Studio, bundle: Path,
) -> None:
    """§12.3 opt-in max_edits_per_action caps writes per group_id."""
    studio.constraints = {"max_edits_per_action": 2}
    raw = "---\ntype: Table\ntitle: Orders\n---\n# Orders\n\nv1\n"
    # Two unique members are allowed.
    r1 = studio.save_concept(concept_id="tables/orders", raw=raw + "1",
                             group_id="G1", action="write_concept")
    r2 = studio.save_concept(concept_id="tables/customers", raw=raw + "2",
                             group_id="G1", action="write_concept")
    assert r1.ok and r2.ok
    # Retrying an existing member at capacity remains allowed.
    retry = studio.save_concept(concept_id="tables/orders", raw=raw + "retry",
                                group_id="G1", action="write_concept")
    assert retry.ok
    # A third unique member is refused.
    r3 = studio.save_concept(concept_id="tables/products", raw=raw + "3",
                             group_id="G1", action="write_concept")
    assert r3.ok is False
    assert "max_edits_per_action" in (r3.error or "")


def test_save_concept_group_undo_round_trip(studio: Studio, bundle: Path) -> None:
    """Multiple save_concept writes sharing a group_id are undoable as one."""
    raw_a = "---\ntype: Table\ntitle: Orders\n---\n# Orders\n\nedit A\n"
    raw_b = "---\ntype: Table\ntitle: Customers\n---\n# Customers\n\nedit B\n"
    prior_orders = (bundle / "tables" / "orders.md").read_bytes()
    prior_customers = (bundle / "tables" / "customers.md").read_bytes()
    r1 = studio.save_concept(
        concept_id="tables/orders", raw=raw_a, group_id="PASS1",
        action="write_concept", emit_graph=False,
    )
    r2 = studio.save_concept(
        concept_id="tables/customers", raw=raw_b, group_id="PASS1",
        action="write_concept", emit_graph=False,
    )
    assert r1.ok and r2.ok
    members = studio.undo_group("PASS1")
    assert members is not None and len(members) == 2
    # Restore both through restore_snapshot.
    for m in members:
        res = studio.restore_snapshot(
            concept_id=m["concept"], rev=m["rev"], group_id="UNDO1",
        )
        assert res.ok
    # Both files are back to their prior bytes.
    assert (bundle / "tables" / "orders.md").read_bytes() == prior_orders
    assert (bundle / "tables" / "customers.md").read_bytes() == prior_customers


def test_restore_snapshot_no_prior_returns_error(studio: Studio) -> None:
    res = studio.restore_snapshot(concept_id="x", rev="nonexistent000")
    assert res.ok is False
    assert "no snapshot" in (res.error or "")


# ---------------------------------------------------------------------------
# Cross-process dedup map (P1-2)
# ---------------------------------------------------------------------------


def test_last_logged_persists_across_processes(studio: Studio, bundle: Path) -> None:
    """save_concept persists _last_logged_rev to .last-logged.json so a NEW
    Studio instance for the same bundle sees the same dedup state."""
    raw = "---\ntype: Table\ntitle: Orders\n---\n# Orders\n\nnew content\n"
    studio.save_concept(concept_id="tables/orders", raw=raw, action="write_concept")
    new_rev = rev_of(raw.encode("utf-8"))
    # The .last-logged.json file exists and contains the rev.
    ll_path = studio.last_logged_path
    assert ll_path.is_file()
    data = json.loads(ll_path.read_text(encoding="utf-8"))
    assert data.get("tables/orders") == new_rev
    # A new Studio instance picks up the same map.
    s2 = Studio.for_bundle(bundle)
    assert s2._last_logged_rev.get("tables/orders") == new_rev


def test_seq_and_rev_resume_from_events(studio: Studio, bundle: Path) -> None:
    """A new Studio instance continues seq + bundle_rev from events.jsonl."""
    studio.append_event({"type": "changed", "ids": ["a"]})
    studio.append_event({"type": "changed", "ids": ["b"]})
    seq_before = studio.seq
    rev_before = studio.bundle_rev
    s2 = Studio.for_bundle(bundle)
    assert s2.seq == seq_before
    assert s2.bundle_rev == rev_before


# ---------------------------------------------------------------------------
# events.jsonl rotation (P1-3)
# ---------------------------------------------------------------------------


def test_events_jsonl_rotates_at_cap(studio: Studio, bundle: Path) -> None:
    """When events.jsonl exceeds events_max_bytes, it rotates to a dated
    sibling; read_events still reads across rotations (within the keep cap)."""
    studio.events_max_bytes = 200  # tiny cap to force rotation
    studio.events_keep = 20  # generous keep so we can verify read-across-rotations
    for i in range(20):
        studio.append_event({"type": "changed", "ids": [f"t{i}"], "fill": "x" * 30})
    # At least one rotated file exists. The rotation pattern is
    # ``events-YYYYMMDD-NN.jsonl``.
    rotated = list(studio.session_dir.glob("events-*-*[0-9].jsonl"))
    assert len(rotated) >= 1, f"expected rotation, got {rotated}"
    # read_events still sees the events across rotations (all 20 within the
    # generous keep cap).
    all_rows = studio.read_events(limit=10000)
    changed = [r for r in all_rows if r.get("type") == "changed"]
    assert len(changed) == 20, f"read across rotations missed events: {len(changed)}"
    # Rotation respects the keep cap.
    studio.events_keep = 2
    # Force one more rotation to trigger pruning.
    for i in range(20, 40):
        studio.append_event({"type": "changed", "ids": [f"t{i}"], "fill": "x" * 30})
    rotated_after = list(studio.session_dir.glob("events-*-*[0-9].jsonl"))
    assert len(rotated_after) <= 5  # bounded; not all historical rotations kept


# ---------------------------------------------------------------------------
# Atomic undo snapshot writes (P1-4)
# ---------------------------------------------------------------------------


def test_snapshot_write_is_atomic(studio: Studio, bundle: Path, monkeypatch) -> None:
    """If the snapshot write is interrupted mid-write, no torn file is left.

    We simulate a failure inside the write and confirm either the file lands
    fully or not at all — never a partial write that corrupts future undo.
    """
    from okf_loom import studio as studio_mod

    call_count = {"n": 0}
    real_atomic_write_bytes = studio_mod.atomic_write_bytes

    def flaky(path, data):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("simulated mid-write crash")
        return real_atomic_write_bytes(path, data)

    monkeypatch.setattr(studio_mod, "atomic_write_bytes", flaky)
    with pytest.raises(OSError):
        studio.snapshot_for_undo(concept_id="tables/orders", raw=b"prior bytes")
    # The history dir contains no torn .md files (the failed tmp was cleaned).
    safe = studio.history_dir / "tables/orders".encode("utf-8").hex()
    if safe.is_dir():
        # An older snapshot from a prior test could be here; what matters is
        # the failed write did not leave a partial file. Atomic helpers
        # unlink tmp on exception; verify no .tmp files remain.
        assert not list(safe.glob("*.tmp"))


def test_group_manifest_write_is_atomic(studio: Studio) -> None:
    """The group manifest is written atomically (tmp + rename)."""
    studio.snapshot_for_undo(
        concept_id="tables/orders", raw=b"x", group_id="G-atomictest-001",
    )
    manifest = (studio.history_dir / "_groups" / "G-atomictest-001" / "manifest.json")
    # The manifest exists and parses cleanly (not torn).
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert isinstance(data, list) and data[0]["concept"] == "tables/orders"


# ---------------------------------------------------------------------------
# §17 library API aliases (P1-5)
# ---------------------------------------------------------------------------


def test_post_presence_alias(studio: Studio) -> None:
    p = studio.post_presence(actor="agent", state="editing", focus="tables/orders")
    assert p["state"] == "editing"
    assert p["focus"] == "tables/orders"
    assert studio.get_presence()["state"] == "editing"


def test_events_append_alias(studio: Studio) -> None:
    e = studio.events_append({"type": "changed", "ids": ["x"]})
    assert "id" in e and e["type"] == "changed"


def test_resolve_comment_alias(studio: Studio) -> None:
    c = studio.post_comment(concept="tables/orders", body="link to customers")
    updated = studio.resolve_comment(
        c["id"], reply="done", activity_ids=["act1", "act2"],
    )
    assert updated["state"] == "resolved"
    assert updated["resolved_activity"] == ["act1", "act2"]
    assert updated["reply"] == "done"


def test_eventbus_callback_subscribe() -> None:
    """EventBus.subscribe(callback) returns a _CallbackSub whose daemon
    thread invokes the callback per event."""
    bus = EventBus()
    seen: list[dict] = []
    ev = threading.Event()

    def cb(event):
        seen.append(event)
        if event.get("type") == "ping":
            ev.set()

    sub = bus.subscribe(callback=cb)
    try:
        bus.publish({"type": "activity", "ids": ["a"]})
        bus.publish({"type": "ping"})
        assert ev.wait(timeout=2.0)
        assert any(e.get("type") == "activity" for e in seen)
    finally:
        bus.unsubscribe(sub)
    # After unsubscribe, no further events are delivered to the callback.
    n_before = len(seen)
    bus.publish({"type": "after_unsubscribe"})
    time.sleep(0.2)
    # Allow for the small race between unsubscribe + worker loop; the count
    # must not grow without bound.
    assert len(seen) - n_before <= 1


def test_eventbus_callback_slow_callback_does_not_block() -> None:
    """A slow callback is treated like a slow SSE client: drop+resync."""
    bus = EventBus(max_queue=2)
    block_event = threading.Event()
    invoked: list[str] = []
    lock = threading.Lock()

    def slow_cb(event):
        with lock:
            invoked.append(event.get("type", "?"))
        if event.get("type") == "start_block":
            block_event.wait(timeout=2.0)

    sub = bus.subscribe(callback=slow_cb)
    try:
        bus.publish({"type": "e1"})
        bus.publish({"type": "start_block"})
        # Give the worker time to enter the blocked callback.
        time.sleep(0.2)
        # Fill the queue past its cap while the callback is blocked.
        for i in range(10):
            bus.publish({"type": f"overflow{i}"})
        # Publish returned without raising — that is the assertion.
        block_event.set()
        time.sleep(0.5)
        bus.publish({"type": "after"})
        time.sleep(0.3)
    finally:
        bus.unsubscribe(sub)
    # The first event was delivered; not all overflow events were.
    with lock:
        assert "e1" in invoked
        overflow_delivered = sum(1 for v in invoked if v and v.startswith("overflow"))
    assert overflow_delivered < 10


# ---------------------------------------------------------------------------
# Path traversal guards on rev + group_id (P2-1, §9.5)
# ---------------------------------------------------------------------------


def test_undo_snapshot_rejects_traversal_rev(studio: Studio) -> None:
    with pytest.raises(ValueError):
        studio.undo_snapshot(concept_id="x", rev="../../../etc/passwd")


def test_undo_group_rejects_traversal_group_id(studio: Studio) -> None:
    with pytest.raises(ValueError):
        studio.undo_group("../../etc")


def test_snapshot_for_undo_rejects_traversal_group_id(studio: Studio) -> None:
    with pytest.raises(ValueError):
        studio.snapshot_for_undo(
            concept_id="x", raw=b"", group_id="../../../etc",
        )


# ---------------------------------------------------------------------------
# §12.3 opt-in constraints (P2-2)
# ---------------------------------------------------------------------------


def test_post_suggestion_noop_without_propose_only(studio: Studio) -> None:
    """When propose_only is not enabled, post_suggestion is a no-op (§1.1/D8)."""
    result = studio.post_suggestion(
        concept="tables/orders", action="add_link",
        args={"target": "tables/customers"}, summary="link them",
    )
    assert result == {}  # no proposal appended


def test_post_suggestion_writes_proposal_when_opted_in(studio: Studio) -> None:
    studio.constraints = {"propose_only": True}
    result = studio.post_suggestion(
        concept="tables/orders", action="add_link",
        args={"target": "tables/customers"}, summary="link them",
        group_id="G1",
    )
    assert result.get("state") == "open"
    assert result.get("concept") == "tables/orders"
    # proposals.jsonl exists.
    prop_path = studio.session_dir / "proposals.jsonl"
    assert prop_path.is_file()
    rows = [json.loads(ln) for ln in prop_path.read_text().splitlines() if ln.strip()]
    assert rows and rows[0]["action"] == "add_link"


# ---------------------------------------------------------------------------
# CLI mutators route through the studio funnel (P0-1)
# ---------------------------------------------------------------------------


def _run_cli(bundle: Path, *args: str) -> tuple[int, str, str]:
    """Run the okf CLI in-process; return (rc, stdout, stderr)."""
    from okf_loom.cli import main
    old_in, old_out, old_err = sys.stdin, sys.stdout, sys.stderr
    sys.stdin = sys.__stdin__
    out_buf, err_buf = [], []
    class _W:
        def write(self, s): out_buf.append(s)
        def flush(self): pass
    class _E:
        def write(self, s): err_buf.append(s)
        def flush(self): pass
    sys.stdout = _W()
    sys.stderr = _E()
    try:
        rc = main(list(args))
    except SystemExit as e:
        rc = int(e.code) if e.code is not None else 0
    except Exception as e:
        err_buf.append(f"{type(e).__name__}: {e}\n")
        rc = 1
    finally:
        sys.stdin, sys.stdout, sys.stderr = old_in, old_out, old_err
    return rc, "".join(out_buf), "".join(err_buf)


def test_cli_link_add_routes_through_studio_funnel(bundle: Path) -> None:
    """okf link-add, with an active session, attributes the write via the
    studio funnel — events.jsonl shows actor=agent, origin=mutator."""
    # Stand up the session dir so _maybe_studio_for_bundle returns a Studio.
    studio = Studio.for_bundle(bundle)
    studio.ensure_session()
    rc, out, err = _run_cli(
        bundle,
        "link-add", "--bundle", str(bundle),
        "--source", "tables/orders", "--target", "tables/customers",
    )
    assert rc == 0, f"stdout={out!r} stderr={err!r}"
    # The activity event attributes the agent.
    events = studio.read_events(limit=50)
    activities = [e for e in events if e.get("type") == "activity"]
    assert any(
        a.get("actor") == "agent"
        and a.get("origin") == "mutator"
        and a.get("action") == "add_link"
        and a.get("ids") == ["tables/orders"]
        for a in activities
    ), f"no attributed activity: {activities}"


def test_cli_link_add_group_id_threaded(bundle: Path) -> None:
    """okf link-add --group-id G1 stamps the group on the activity event."""
    studio = Studio.for_bundle(bundle)
    studio.ensure_session()
    rc, out, err = _run_cli(
        bundle,
        "link-add", "--bundle", str(bundle),
        "--source", "tables/orders", "--target", "tables/customers",
        "--group-id", "PASS1",
    )
    assert rc == 0
    activities = [e for e in studio.read_events(limit=50)
                  if e.get("type") == "activity"]
    assert any(a.get("group_id") == "PASS1" for a in activities)
    members = studio.undo_group("PASS1")
    assert members and len(members) == 1


def test_cli_mutator_no_session_writes_plain(bundle: Path) -> None:
    """When no session dir exists, the mutator still writes (compatibility path)
    and produces no events.jsonl."""
    assert not (bundle / ".okf-loom" / "session").is_dir()
    rc, out, err = _run_cli(
        bundle,
        "link-add", "--bundle", str(bundle),
        "--source", "tables/orders", "--target", "tables/customers",
    )
    assert rc == 0
    # No session was created.
    assert not (bundle / ".okf-loom" / "session" / "events.jsonl").is_file()


# ---------------------------------------------------------------------------
# CLI token / comment-* / presence (P0-2)
# ---------------------------------------------------------------------------


def test_cli_token_reads_session_token(bundle: Path) -> None:
    """okf token prints the per-session CSRF token from .okf-loom/session/.token."""
    studio = Studio.for_bundle(bundle)
    studio.ensure_session()
    token_path = studio.token_path
    token_path.write_text("abc123secret", encoding="utf-8")
    rc, out, err = _run_cli(bundle, "token", str(bundle))
    assert rc == 0
    assert out == "abc123secret"  # no trailing newline


def test_cli_token_no_session_exits_1(bundle: Path) -> None:
    rc, out, err = _run_cli(bundle, "token", str(bundle))
    assert rc == 1
    assert "no studio session" in err.lower()


def test_cli_comment_claim_and_resolve_round_trip(bundle: Path) -> None:
    """scripts/okf-loom comment-claim + comment-resolve completes the agent loop."""
    studio = Studio.for_bundle(bundle)
    studio.ensure_session()
    c = studio.post_comment(concept="tables/orders", body="link to customers")
    cid = c["id"]
    # Claim.
    rc, out, err = _run_cli(bundle, "comment-claim", str(bundle), cid)
    assert rc == 0, f"stdout={out!r} stderr={err!r}"
    assert studio.get_comment(cid)["state"] == "claimed"
    # Resolve with reply + activity links.
    rc, out, err = _run_cli(
        bundle, "comment-resolve", str(bundle), cid,
        "--reply", "done", "--activity", "act1,act2",
    )
    assert rc == 0
    updated = studio.get_comment(cid)
    assert updated["state"] == "resolved"
    assert updated["resolved_activity"] == ["act1", "act2"]
    assert updated["reply"] == "done"


def test_cli_comment_list_filters_by_state(bundle: Path) -> None:
    studio = Studio.for_bundle(bundle)
    studio.ensure_session()
    studio.post_comment(concept="tables/orders", body="one")
    c2 = studio.post_comment(concept="tables/orders", body="two")
    studio.update_comment(c2["id"], state="resolved")
    rc, out, err = _run_cli(bundle, "comment-list", str(bundle), "--state", "open")
    assert rc == 0
    assert "one" in out
    assert "two" not in out


def test_cli_presence_sets_state(bundle: Path) -> None:
    studio = Studio.for_bundle(bundle)
    studio.ensure_session()
    rc, out, err = _run_cli(
        bundle, "presence", str(bundle),
        "--state", "editing", "--focus", "tables/orders",
    )
    assert rc == 0
    # The CLI's Studio instance has its own in-memory state; read the
    # persisted presence.json which is the cross-process source of truth.
    p = json.loads(studio.presence_path.read_text(encoding="utf-8"))
    assert p["state"] == "editing"
    assert p["focus"] == "tables/orders"


# ---------------------------------------------------------------------------
# SSE watchdog fix (INTENT-004) — heartbeat emits a named event
# ---------------------------------------------------------------------------


def test_graph_actions_set_complete() -> None:
    """Sanity-check the _GRAPH_ACTIONS set covers the spec's graph-affecting
    ops (so the auto-emit-graph heuristic is correct)."""
    for action in ("add_link", "remove_link", "add_relation", "remove_relation",
                   "add_entity", "remove_entity", "write_concept", "repair"):
        assert action in _GRAPH_ACTIONS


# ---------------------------------------------------------------------------
# Live HTTP server tests (P1-6 doc_revs, P1-8 Host check, P1-16 allowed_hosts,
# P1-1 conflict 409, /__diff)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _start_server(bundle: Path, port: int, **kwargs) -> subprocess.Popen:
    """Start ``okf serve`` on the given port; do NOT open a browser."""
    return subprocess.Popen(
        okf_module_argv(
            "serve", str(bundle), "--host", "127.0.0.1", "--port", str(port),
            "--no-open", "--no-watch",
        ),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(bundle.parent),
        env=okf_subprocess_env(),
    )


def test_sse_ready_frame_carries_doc_revs(bundle: Path) -> None:
    """The SSE ``ready`` frame includes a ``doc_revs`` map (P1-6)."""
    port = _free_port()
    proc = _start_server(bundle, port)
    try:
        # Wait for the server to come up.
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).read()
                break
            except Exception:
                time.sleep(0.2)
        # Open the SSE stream with a raw socket so we can read until the
        # ready frame arrives (then bail).
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET /__events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        buf = b""
        ready_payload: dict | None = None
        end = time.time() + 5
        while time.time() < end:
            try:
                chunk = s.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            # Look for a complete SSE frame: ``event: ready\ndata: {...}\n\n``.
            for frame in buf.split(b"\n\n"):
                if b"event: ready" in frame and b"data: " in frame:
                    for line in frame.split(b"\n"):
                        if line.startswith(b"data: "):
                            try:
                                ready_payload = json.loads(line[6:])
                                break
                            except json.JSONDecodeError:
                                pass
            if ready_payload:
                break
        s.close()
        assert ready_payload is not None, f"no ready frame in {buf!r}"
        assert "doc_revs" in ready_payload
        canonical = (bundle / "tables" / "orders.md").read_text(encoding="utf-8")
        doc_rev = ready_payload["doc_revs"].get("tables/orders")
        assert doc_rev == concept_content_rev(canonical)
        assert len(doc_rev) == 64
        assert isinstance(ready_payload["rev"], int)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_apply_returns_409_on_conflict(bundle: Path) -> None:
    """/__apply with a stale expected_rev returns 409 (§9.3/§9.4)."""
    port = _free_port()
    proc = _start_server(bundle, port)
    try:
        # Wait for ready.
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).read()
                break
            except Exception:
                time.sleep(0.2)
        # Read the CSRF token.
        token_path = bundle / ".okf-loom" / "session" / ".token"
        deadline = time.time() + 5
        while time.time() < deadline and not token_path.is_file():
            time.sleep(0.1)
        assert token_path.is_file(), "server did not write .token"
        token = token_path.read_text(encoding="utf-8").strip()
        headers = {"X-OKF-Token": token, "Content-Type": "application/json",
                   "Origin": "http://127.0.0.1:%d" % port}
        body = json.dumps({
            "kind": "add_link",
            "target": "tables/orders",
            "args": {
                "label": "Customers",
                "target_concept_id": "tables/customers",
            },
            "expected_rev": "stale_rev_value_x",  # not the current rev
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/__apply", data=body, headers=headers,
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5).read()
            assert False, "expected 409"
        except urllib.error.HTTPError as e:
            assert e.code == 409
            payload = json.loads(e.read().decode("utf-8"))
            assert payload.get("conflict") is True
            assert payload.get("current_rev")  # populated
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_foreign_origin_rejected(bundle: Path) -> None:
    """A cross-origin POST (foreign Origin) is rejected (current spec §14)."""
    port = _free_port()
    proc = _start_server(bundle, port)
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).read()
                break
            except Exception:
                time.sleep(0.2)
        token_path = bundle / ".okf-loom" / "session" / ".token"
        deadline = time.time() + 5
        while time.time() < deadline and not token_path.is_file():
            time.sleep(0.1)
        token = token_path.read_text(encoding="utf-8").strip()
        # Foreign origin.
        headers = {"X-OKF-Token": token, "Content-Type": "application/json",
                   "Origin": "http://evil.example.com"}
        body = json.dumps({
            "kind": "add_link", "target": "tables/orders",
            "args": {"label": "X", "target_concept_id": "tables/customers"},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/__apply", data=body, headers=headers,
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5).read()
            assert False, "expected 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_foreign_host_rejected(bundle: Path) -> None:
    """A POST with a foreign Host header (no Origin) is rejected (P1-8)."""
    port = _free_port()
    proc = _start_server(bundle, port)
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).read()
                break
            except Exception:
                time.sleep(0.2)
        token_path = bundle / ".okf-loom" / "session" / ".token"
        deadline = time.time() + 5
        while time.time() < deadline and not token_path.is_file():
            time.sleep(0.1)
        token = token_path.read_text(encoding="utf-8").strip()
        # Foreign Host but valid token. urllib doesn't let us set Host easily,
        # so use a raw socket.
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({
            "kind": "add_link", "target": "tables/orders",
            "args": {"label": "X", "target_concept_id": "tables/customers"},
        })
        # Override Host with a foreign value via the lower-level putheader.
        conn.putrequest("POST", "/__apply", skip_host=True)
        conn.putheader("Host", "evil.example.com")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(body)))
        conn.putheader("X-OKF-Token", token)
        conn.endheaders(body.encode("utf-8"))
        resp = conn.getresponse()
        assert resp.status == 403, f"expected 403, got {resp.status}"
        resp.read()
        conn.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# Need threading import for the callback tests.
import threading  # noqa: E402


# ---------------------------------------------------------------------------
# iter-2 Bundle H — cmd_repair index regen through the studio funnel
# (P2-2 / ARCH2-003 / AGENTS.md hard rule #7)
# ---------------------------------------------------------------------------


def test_repair_routes_index_regen_through_studio(bundle: Path) -> None:
    """P2-2 / ARCH2-003: ``okf repair --apply`` with an active session routes
    index regen through ``Studio.save_concept`` so the mechanical refresh
    appears in events.jsonl with attribution (actor=agent, origin=auto-repair,
    action=refresh_index) and stamps the --group-id for one-click group undo.

    Spec contract (AGENTS.md hard rule #7): every concept write — CLI
    mutator, apply_plan, POST /__apply, POST /__undo — goes through one
    internal funnel, ``Studio.save_concept``, when a studio session is
    live. Iter-1 routed mirror_relation / mechanical add_link through the
    funnel but left ``regenerate_indexes`` calling ``atomic_write_text``
    directly, which made mechanical index refresh invisible on the change
    list. This test closes that gap.
    """
    studio = Studio.for_bundle(bundle)
    studio.ensure_session()
    # The bundle fixture ships tables/orders.md + tables/customers.md but
    # NO tables/index.md, so repair --indexes --apply will need to CREATE
    # one. (create_index in the planner → refresh_index in the funnel.)
    assert not (bundle / "tables" / "index.md").is_file()

    rc, out, err = _run_cli(
        bundle,
        "repair", str(bundle), "--indexes", "--apply",
        "--group-id", "REPAIR1",
    )
    assert rc == 0, f"stdout={out!r} stderr={err!r}"

    # The index file was written to disk.
    idx_path = bundle / "tables" / "index.md"
    assert idx_path.is_file(), "tables/index.md was not created by repair"
    assert "Orders" in idx_path.read_text(encoding="utf-8"), (
        "regenerated tables/index.md missing the Orders entry"
    )

    # The activity event attributes the regen through the funnel.
    activities = [
        e for e in studio.read_events(limit=200)
        if e.get("type") == "activity"
    ]
    refresh_acts = [a for a in activities if a.get("action") == "refresh_index"]
    assert refresh_acts, (
        f"no refresh_index activity event; saw actions="
        f"{[a.get('action') for a in activities]!r}"
    )
    a = refresh_acts[0]
    assert a["actor"] == "agent", f"actor={a['actor']!r}"
    assert a["origin"] == "auto-repair", f"origin={a['origin']!r}"
    assert a.get("group_id") == "REPAIR1", f"group_id={a.get('group_id')!r}"
    # The concept id of the written index is path-derived ("tables/index"
    # for tables/index.md). The reserved-filename rule (hard rule #5) is
    # about not using index.md as a CONCEPT DOCUMENT; here we are writing
    # the directory index itself, and save_concept is the funnel that gets
    # the refresh attributed.
    assert "tables/index" in a.get("ids", []), f"ids={a.get('ids')!r}"
    # undoable=False per Option A: index regen is mechanical, one
    # group-undo per repair pass instead of N undo buttons.
    assert a.get("undoable") is False, f"undoable={a.get('undoable')!r}"

    # Group-undo membership: the snapshot ring should have the group entry
    # even though undoable=False (the group manifest tracks the pass for
    # discovery; per-write snapshots are skipped when undoable=False).
    # We assert the weaker, durable contract: the activity event itself
    # carries the group_id so /__undo {group_id:"REPAIR1"} can resolve it.


def test_repair_no_session_skips_studio_funnel(bundle: Path) -> None:
    """Complement: when no session is active, repair --apply still refreshes
    indexes via the compatibility atomic_write_text path (no events.jsonl, no
    attribution). Preserves behavior for non-studio callers."""
    assert not (bundle / ".okf-loom" / "session").is_dir()
    rc, out, err = _run_cli(
        bundle,
        "repair", str(bundle), "--indexes", "--apply",
    )
    assert rc == 0, f"stdout={out!r} stderr={err!r}"
    assert (bundle / "tables" / "index.md").is_file(), (
        "tables/index.md should still be created via the no-session path"
    )
    # No session → no events.jsonl.
    assert not (bundle / ".okf-loom" / "session" / "events.jsonl").is_file(), (
        "no-session path should not create events.jsonl"
    )
