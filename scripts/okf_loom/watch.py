"""Headless change feed for the agent — ``okf watch`` (current spec §12).

Standalone (NO HTTP server). Reuses :class:`okf_loom.server._BundleWatcher`
to poll ``.md`` mtimes and reload the bundle; on each change it diffs old/new
concept revs, appends a fully-attributed event to the durable session feed
(``<bundle>/.okf-loom/session/events.jsonl``) via :class:`okf_loom.studio.Studio`,
and prints one event per line to stdout. The harness pipes stdout to the agent.

Design notes (current spec §12):
  * ``--emit jsonl`` (default) prints the §7.2 event schema (``changed`` /
    ``created`` / ``removed`` / ``graph``); ``--emit text`` prints
    human-readable lines (``changed tables/orders``).
  * ``--since <id>`` replays missed events from ``events.jsonl`` starting
    after the given event id, then tails live changes (reconnect-safe).
  * ``--auto-repair`` runs the mechanical ``okf repair`` equivalent (index
    regen + relation mirroring) on change, debounced by ``--debounce-ms``.
  * It ALSO appends to ``events.jsonl`` so a concurrent ``okf serve`` picks
    the same feed up over SSE — the bundle on disk is the bus.

Public API:
    run_watch(bundle_root, *, emit='jsonl', since=None, auto_repair=False,
              debounce_ms=600) -> int
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

from .model import Bundle
from .paths import concept_id_to_str
from .studio import Studio, rev_of


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _concept_revs(bundle: Bundle) -> dict[str, str]:
    """Map ``concept-id-str -> rev_of(raw_text)`` for every concept."""
    return {
        concept_id_to_str(cid): rev_of(c.raw_text)
        for cid, c in bundle.concepts.items()
    }


def _print_event(ev: dict[str, Any], emit: str) -> None:
    """Print one session event to stdout in the chosen wire format.

    ``jsonl``: compact JSON matching the §7.2 schema (the event already
    carries ``id``/``ts``/``seq``/``rev`` stamped by ``Studio.append_event``).
    ``text``: human-readable, e.g. ``changed tables/orders``.
    """
    if emit == "text":
        etype = str(ev.get("type", "message"))
        ids = ev.get("ids") or []
        if etype == "graph":
            print("graph changed", flush=True)
            return
        if ids:
            print(f"{etype} {' '.join(ids)}", flush=True)
            return
        # activity / presence / comment with no ids: surface the action/verb.
        verb = ev.get("action") or etype
        tail = f" {' '.join(ids)}" if ids else ""
        print(f"{verb}{tail}", flush=True)
        return
    # default: jsonl
    print(json.dumps(ev, default=str, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------------------
# Mechanical repair (the ``okf repair`` equivalent, in-process)
# ---------------------------------------------------------------------------

def _run_auto_repair(bundle_root: Path) -> list[str]:
    """Run the mechanical ``okf repair`` equivalent in-process (§10).

    Mirrors ``cmd_repair``'s apply path: mechanical-only actions
    (``mirror_relation`` / ``add_link`` via ``update.apply_plan``, then
    ``index.regenerate_indexes``). Returns the slash-form concept ids whose
    files changed as a result (the rev-diff of before/after). Idempotent:
    a no-op repair returns ``[]`` and converges, so the watch loop does not
    spin.
    """
    import tempfile

    from .index import regenerate_indexes
    from .plan import Plan as PlanObj, build_plan
    from .update import apply_plan, load_plan

    before = Bundle.load(bundle_root)
    before_revs = _concept_revs(before)

    plan = build_plan(before)

    def _is_mechanical(a: Any) -> bool:
        return (
            a.argv is not None
            and a.confidence is None
            and a.agent_instruction is None
            and a.argv_template is None
        )

    mechanical = [a for a in plan.actions if _is_mechanical(a)]
    apply_actions = [
        a for a in mechanical if a.action in ("mirror_relation", "add_link")
    ]
    if apply_actions:
        scoped = PlanObj(bundle_root=plan.bundle_root, actions=apply_actions)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(scoped.as_dict(), f)
            plan_path = f.name
        try:
            loaded = load_plan(plan_path)
            apply_plan(before, loaded, apply=True)
        finally:
            Path(plan_path).unlink(missing_ok=True)

    has_index_work = any(
        a.action in ("create_index", "refresh_index") for a in mechanical
    )
    if has_index_work:
        before.invalidate()
        regen_bundle = Bundle.load(bundle_root)
        regenerate_indexes(regen_bundle)

    # Diff revs to find which concepts the repair actually touched.
    after = Bundle.load(bundle_root)
    after_revs = _concept_revs(after)
    affected = sorted(
        cid for cid in after_revs
        if before_revs.get(cid) != after_revs[cid]
    )
    return affected


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_watch(
    bundle_root: str | Path,
    *,
    emit: str = "jsonl",
    since: str | None = None,
    auto_repair: bool = False,
    debounce_ms: int = 600,
) -> int:
    """Run the headless ``okf watch`` change feed (current spec §12).

    Blocks on the bundle watcher; Ctrl-C exits cleanly (returns 0). Reuses
    :class:`okf_loom.server._BundleWatcher` (polls ``.md`` mtimes, calls
    a reload callback). Each detected change reloads the bundle, diffs old/new
    concept revs, appends ``changed``/``created``/``removed``/``graph``
    events to ``<bundle>/.okf-loom/session/events.jsonl`` via the shared
    :class:`Studio`, and prints one event per line to stdout.

    Args:
        bundle_root: path to an OKF bundle directory.
        emit: ``"jsonl"`` (default) or ``"text"``.
        since: optional event id; when given, missed events are replayed from
            ``events.jsonl`` starting after it, then live changes are tailed.
        auto_repair: when True, run the mechanical repair on each change,
            debounced by ``debounce_ms``.
        debounce_ms: coalesce repair runs by this many milliseconds (default
            600). Only meaningful with ``auto_repair``.

    Returns:
        0 on clean exit (Ctrl-C or watcher stop).
    """
    # Import lazily so importing ``watch`` does not pull the HTTP server into
    # every consumer; the watcher thread itself is HTTP-free.
    from .server import _BundleWatcher

    bundle_root = Path(bundle_root).resolve()
    studio = Studio.for_configured_bundle(bundle_root)
    studio.ensure_session()

    # Live mutable state. The watcher is single-threaded (one reload at a
    # time), so ``revs`` is only mutated from the reload callback. The repair
    # debounce timers are guarded by ``timer_lock`` to serialize re-arm/cancel
    # and shutdown.  ``repair_workers`` owns every Timer until it has been
    # joined during shutdown; this includes canceled timers and a callback
    # already running mechanical repair/activity publication.
    state: dict[str, Any] = {
        "bundle": Bundle.load(bundle_root),
        "revs": {},
        "repair_timer": None,
        "repair_workers": set(),
        "shutting_down": False,
    }
    state["revs"] = _concept_revs(state["bundle"])
    timer_lock = threading.Lock()

    def _on_change() -> None:
        """Watcher reload callback: diff revs, append+print events."""
        old_revs = state["revs"]
        try:
            fresh = Bundle.load(bundle_root)
        except Exception as e:  # noqa: BLE001 — never kill the watcher thread
            # Match _BundleWatcher's own reload-safety contract: log and leave
            # the snapshot untouched so the next tick retries (P1-37).
            print(
                f"okf: watch reload failed for {bundle_root} "
                f"(keeping previous snapshot; will retry): {e}",
                file=sys.stderr,
            )
            return
        new_revs = _concept_revs(fresh)
        created = [i for i in new_revs if i not in old_revs]
        removed = [i for i in old_revs if i not in new_revs]
        changed = [
            i for i in new_revs
            if i in old_revs and new_revs[i] != old_revs[i]
        ]
        state["bundle"] = fresh
        state["revs"] = new_revs

        events: list[dict[str, Any]] = []
        if created:
            events.append(
                studio.emit_change(kind="created", ids=created, origin="disk")
            )
        if changed:
            events.append(
                studio.emit_change(kind="changed", ids=changed, origin="disk")
            )
        if removed:
            events.append(
                studio.emit_change(kind="removed", ids=removed, origin="disk")
            )
        if created or changed or removed:
            events.append(
                studio.emit_change(
                    kind="graph", ids=[], origin="disk", graph=True
                )
            )
        for ev in events:
            _print_event(ev, emit)

        if auto_repair and (created or changed or removed):
            _schedule_repair()

    def _schedule_repair() -> None:
        """Debounce: cancel any pending repair, arm a fresh one."""
        delay = max(0, debounce_ms) / 1000.0
        with timer_lock:
            if state["shutting_down"]:
                return
            state["repair_workers"] = {
                worker for worker in state["repair_workers"]
                if worker.is_alive()
            }
            pending = state.get("repair_timer")
            if pending is not None:
                pending.cancel()
            timer = threading.Timer(delay, _do_repair)
            timer.daemon = True
            state["repair_timer"] = timer
            state["repair_workers"].add(timer)
            try:
                timer.start()
            except BaseException:
                state["repair_workers"].discard(timer)
                if state["repair_timer"] is timer:
                    state["repair_timer"] = None
                raise

    def _do_repair() -> None:
        """Run the mechanical repair; emit an activity event if it touched files."""
        worker = threading.current_thread()
        with timer_lock:
            if state["shutting_down"]:
                return
            if state["repair_timer"] is worker:
                state["repair_timer"] = None
        try:
            affected = _run_auto_repair(bundle_root)
        except Exception as e:  # noqa: BLE001 — repair must not kill the timer pool
            print(f"okf: auto-repair failed: {e}", file=sys.stderr)
            return
        if not affected:
            return
        ev = studio.record_activity(
            actor="cli",
            action="auto_repair",
            ids=affected,
            summary=f"Auto-repair touched {len(affected)} concept(s)",
            origin="auto-repair",
            undoable=False,
        )
        _print_event(ev, emit)
        # The repair's file writes are picked up by the watcher on its next
        # tick and emitted as ordinary ``changed`` disk events — no manual
        # emit needed here. The repair is idempotent, so the loop converges.

    # --- reconnect-safe replay (§10 / §10.5) -----------------------------
    if since is not None:
        # read_events(since=id) returns rows AFTER the row whose id == since.
        for ev in studio.read_events(since=since, limit=1_000_000):
            _print_event(ev, emit)

    # --- live tail --------------------------------------------------------
    watcher = _BundleWatcher(bundle_root, _on_change, interval=1.0)
    watcher.start()
    try:
        while watcher.is_alive():
            watcher.join(0.5)
    except KeyboardInterrupt:
        # Clean exit: stop the watcher + any pending repair, return 0.
        pass
    finally:
        watcher.stop()
        with timer_lock:
            # Fence scheduling before taking the complete worker snapshot.
            # Joining happens without this lock because callbacks acquire it
            # both on entry and exit.
            state["shutting_down"] = True
            pending = state.get("repair_timer")
            if pending is not None:
                pending.cancel()
                state["repair_timer"] = None
            repair_workers = tuple(state["repair_workers"])
        # _on_change also uses Studio, so its owning watcher must terminate;
        # a best-effort timeout would recreate the same use-after-close race.
        watcher.join()
        for worker in repair_workers:
            worker.cancel()
            worker.join()
        studio.close()
    return 0


__all__ = ["run_watch"]
