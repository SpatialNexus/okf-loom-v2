"""Adversarial proofs for retained Bundle mutation ownership."""
from __future__ import annotations

import copy
import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.index import regenerate_indexes
from okf_loom.log import append_log_entry
from okf_loom.retrieval import RetrievalService, SearchRequest
from okf_loom.studio import Studio, rev_of
from okf_loom.update import (
    UpdateOp, UpdatePlan, WriteConceptError, apply_plan, write_concept,
)


def _root(tmp_path: Path) -> Path:
    (tmp_path / "a.md").write_text(
        "---\ntype: Guide\ntitle: A\ncustom: keep\n---\n# A\noldword\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: Guide\ntitle: B\n---\n# B\nother\n",
        encoding="utf-8",
    )
    return tmp_path


def _plan(*ops: UpdateOp) -> UpdatePlan:
    return UpdatePlan(None, "state transaction", list(ops), "test")


def _state(bundle: Bundle, cid: tuple[str, ...]) -> tuple[object, ...]:
    concept = bundle.concepts[cid]
    return (
        concept,
        concept.raw_text,
        copy.deepcopy(concept.frontmatter),
        concept.body,
        list(concept.headings),
        bundle._graph,
        bundle._content_index,
        bundle._passage_index,
        bundle._bundle_revision,
    )


def test_raw_text_is_authoritative_when_invalidate_sees_stale_parsed_views(
    tmp_path: Path,
) -> None:
    bundle = Bundle.load(_root(tmp_path))
    concept = bundle.concepts[("a",)]
    raw_edit = concept.raw_text.replace("title: A", "title: Raw").replace(
        "oldword", "rawonly"
    )
    concept.raw_text = raw_edit

    bundle.invalidate()

    assert concept.raw_text == raw_edit
    assert concept.frontmatter["title"] == "Raw"
    assert concept.body.endswith("rawonly")
    assert RetrievalService(bundle).search(SearchRequest("rawonly"))[0].title == "Raw"


def test_atomic_write_failure_leaves_retained_source_and_caches_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.update as update

    bundle = Bundle.load(_root(tmp_path))
    service = RetrievalService(bundle)
    service.search(SearchRequest("oldword"))
    before = _state(bundle, ("a",))
    disk = (tmp_path / "a.md").read_bytes()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected atomic failure")

    monkeypatch.setattr(update, "atomic_write_text", fail)
    with pytest.raises(OSError, match="injected"):
        apply_plan(bundle, _plan(UpdateOp(
            "set_frontmatter", ("a",), {"key": "title", "value": "Rejected"}
        )))

    assert _state(bundle, ("a",)) == before
    assert (tmp_path / "a.md").read_bytes() == disk


def test_dry_run_publishes_preview_invalidates_caches_and_reload_restores(
    tmp_path: Path,
) -> None:
    bundle = Bundle.load(_root(tmp_path))
    disk_before = (tmp_path / "a.md").read_bytes()
    graph_before = bundle.graph()
    revision_before = RetrievalService(bundle).revision
    result = apply_plan(
        bundle,
        _plan(
            UpdateOp("add_tag", ("a",), {"tag": "preview"}),
            UpdateOp("add_tag", ("a",), {"tag": "preview"}),
        ),
        apply=False,
        dry_run=True,
    )
    assert result["results"][0][1]["applied"] is True
    assert result["results"][1][1]["reason"] == "already_tagged"
    assert bundle.concepts[("a",)].frontmatter["tags"] == ["preview"]
    assert (tmp_path / "a.md").read_bytes() == disk_before
    assert bundle.graph() is not graph_before
    assert RetrievalService(bundle).revision != revision_before

    bundle.reload()
    assert "tags" not in bundle.concepts[("a",)].frontmatter
    assert (tmp_path / "a.md").read_bytes() == disk_before


def test_dry_run_then_real_apply_uses_persisted_baseline_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.update as update
    from okf_loom.io_utils import atomic_write_text as real_atomic_write

    bundle = Bundle.load(_root(tmp_path))
    plan = _plan(UpdateOp("add_tag", ("a",), {"tag": "preview"}))
    writes = 0

    def counted_write(path: Path, raw: str) -> None:
        nonlocal writes
        writes += 1
        real_atomic_write(path, raw)

    monkeypatch.setattr(update, "atomic_write_text", counted_write)
    assert apply_plan(bundle, plan, apply=False, dry_run=True)["applied"] == 1
    assert writes == 0
    assert apply_plan(bundle, plan, apply=True)["applied"] == 1
    assert writes == 1
    assert "preview" in (tmp_path / "a.md").read_text(encoding="utf-8")
    assert apply_plan(bundle, plan, apply=True)["applied"] == 0
    assert writes == 1


def test_dry_run_then_external_edit_conflicts_without_stale_publication(
    tmp_path: Path,
) -> None:
    bundle = Bundle.load(_root(tmp_path))
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    plan = _plan(UpdateOp("add_tag", ("a",), {"tag": "preview"}))
    apply_plan(bundle, plan, apply=False, dry_run=True)
    preview = _state(bundle, ("a",))
    external = (tmp_path / "a.md").read_text(encoding="utf-8") + "\nexternal\n"
    (tmp_path / "a.md").write_text(external, encoding="utf-8")

    result = apply_plan(bundle, plan, apply=True, studio=studio)

    assert result["applied"] == 0
    assert result["results"][0][1]["reason"] == "rev_conflict"
    assert _state(bundle, ("a",)) == preview
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == external
    events = studio.read_events(limit=100)
    assert len([event for event in events if event.get("type") == "agent_conflict"]) == 1
    assert not [
        event for event in events
        if event.get("type") in {"activity", "changed", "graph"}
    ]


def test_studio_conflict_and_validation_failure_publish_nothing(
    tmp_path: Path,
) -> None:
    bundle = Bundle.load(_root(tmp_path))
    service = RetrievalService(bundle)
    service.search(SearchRequest("oldword"))
    before = _state(bundle, ("a",))
    external = (tmp_path / "a.md").read_text(encoding="utf-8").replace(
        "oldword", "external"
    )
    (tmp_path / "a.md").write_text(external, encoding="utf-8")

    result = apply_plan(
        bundle,
        _plan(UpdateOp("set_frontmatter", ("a",), {"key": "title", "value": "No"})),
        studio=Studio.for_bundle(tmp_path),
    )
    assert result["applied"] == 0
    assert result["results"][0][1]["reason"] == "rev_conflict"
    assert _state(bundle, ("a",)) == before
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == external

    # A malformed later op rolls back the whole private candidate plan.
    fresh = Bundle.load(tmp_path)
    disk_before = (tmp_path / "a.md").read_bytes()
    result = apply_plan(fresh, _plan(
        UpdateOp("set_frontmatter", ("a",), {"key": "title", "value": "No"}),
        UpdateOp("set_frontmatter", ("b",), {"value": "missing key"}),
    ))
    assert result["applied"] == 0
    assert (tmp_path / "a.md").read_bytes() == disk_before
    assert fresh.concepts[("a",)].frontmatter["title"] == "A"


def test_apply_plan_rolls_back_prior_file_when_later_atomic_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.update as update
    from okf_loom.io_utils import atomic_write_text as real_atomic_write

    bundle = Bundle.load(_root(tmp_path))
    RetrievalService(bundle).search(SearchRequest("oldword"))
    before_a = _state(bundle, ("a",))
    before_b = _state(bundle, ("b",))
    disk_a = (tmp_path / "a.md").read_bytes()
    disk_b = (tmp_path / "b.md").read_bytes()
    calls = 0

    def fail_second(path: Path, raw: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second write failed")
        real_atomic_write(path, raw)

    monkeypatch.setattr(update, "atomic_write_text", fail_second)
    with pytest.raises(OSError, match="second"):
        apply_plan(bundle, _plan(
            UpdateOp("add_tag", ("a",), {"tag": "one"}),
            UpdateOp("add_tag", ("b",), {"tag": "two"}),
        ))

    assert (tmp_path / "a.md").read_bytes() == disk_a
    assert (tmp_path / "b.md").read_bytes() == disk_b
    assert _state(bundle, ("a",)) == before_a
    assert _state(bundle, ("b",)) == before_b


def test_later_studio_write_failure_rolls_back_with_zero_success_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = Bundle.load(_root(tmp_path))
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    before_a = _state(bundle, ("a",))
    before_b = _state(bundle, ("b",))
    disk_a = (tmp_path / "a.md").read_bytes()
    disk_b = (tmp_path / "b.md").read_bytes()
    original_save = studio.save_concept
    calls = 0

    def fail_second(**kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("later studio write failed")
        return original_save(**kwargs)

    monkeypatch.setattr(studio, "save_concept", fail_second)
    with pytest.raises(OSError, match="later studio"):
        apply_plan(bundle, _plan(
            UpdateOp("add_tag", ("a",), {"tag": "one"}),
            UpdateOp("add_tag", ("b",), {"tag": "two"}),
        ), studio=studio, group_id="ROLLBACK-1")

    assert (tmp_path / "a.md").read_bytes() == disk_a
    assert (tmp_path / "b.md").read_bytes() == disk_b
    assert _state(bundle, ("a",)) == before_a
    assert _state(bundle, ("b",)) == before_b
    assert not [
        event for event in studio.read_events(limit=100)
        if event.get("type") in {"activity", "changed", "graph"}
    ]
    assert not list(studio.history_dir.rglob("*.md"))
    assert studio.undo_group("ROLLBACK-1") is None


def test_same_target_ops_coalesce_write_but_keep_ordered_activities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = Bundle.load(_root(tmp_path))
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    original_save = studio.save_concept
    physical_writes = 0

    def counted_save(**kwargs: object):
        nonlocal physical_writes
        physical_writes += 1
        return original_save(**kwargs)

    monkeypatch.setattr(studio, "save_concept", counted_save)
    result = apply_plan(bundle, _plan(
        UpdateOp("add_link", ("a",), {
            "target_concept_id": "b", "label": "B",
        }),
        UpdateOp("add_relation", ("a",), {
            "target_concept_id": "b", "relation_type": "depends_on",
        }),
    ), studio=studio, group_id="SAME-TARGET")

    assert result["applied"] == 2
    assert physical_writes == 1
    persisted = (tmp_path / "a.md").read_text(encoding="utf-8")
    assert "](/b.md)" in persisted
    assert "depends_on" in persisted
    activities = [
        event for event in studio.read_events(limit=100)
        if event.get("type") == "activity"
    ]
    assert [event["action"] for event in activities] == [
        "add_link", "add_relation",
    ]
    assert [event.get("group_id") for event in activities] == [
        "SAME-TARGET", "SAME-TARGET",
    ]
    assert [event.get("undoable") for event in activities] == [False, True]
    assert "before" not in activities[0].get("detail", {})
    assert activities[1]["detail"]["before_concept"] == "a"


def test_deferred_history_batch_failure_unwinds_snapshots_and_group_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.studio as studio_module

    studio = Studio.for_bundle(_root(tmp_path))
    studio.ensure_session()
    real_write = studio_module.atomic_write_bytes
    calls = 0

    def fail_second(path: Path, raw: bytes, *, mode: int | None = None) -> None:
        nonlocal calls
        assert mode == 0o600
        calls += 1
        if calls == 2:
            raise OSError("history staging failed")
        real_write(path, raw, mode=mode)

    monkeypatch.setattr(studio_module, "atomic_write_bytes", fail_second)
    with pytest.raises(OSError, match="history staging"):
        studio.snapshot_many_for_undo(
            [("a", b"before a"), ("b", b"before b")], group_id="HISTORY-TX"
        )

    assert not list(studio.history_dir.rglob("*.md"))
    assert studio.undo_group("HISTORY-TX") is None


def test_write_concept_rejects_malformed_existing_document_byte_exact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "broken.md"
    malformed = b"---\ntype: [unterminated\n---\n# Hand authored\n"
    path.write_bytes(malformed)
    bundle = Bundle.load(tmp_path)

    with pytest.raises(WriteConceptError) as raised:
        write_concept(bundle, "broken", type="Guide", title="No rewrite")

    assert raised.value.code == "invalid_existing_document"
    assert str(raised.value) == "refusing to rewrite unparseable existing concept broken"
    assert path.read_bytes() == malformed


def test_nonstudio_write_concept_update_and_create_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.update as update

    bundle = Bundle.load(_root(tmp_path))
    real_parse = update.parse_document

    def edit_after_read(raw: str):
        parsed = real_parse(raw)
        (tmp_path / "a.md").write_text(raw + "\nexternal\n", encoding="utf-8")
        return parsed

    monkeypatch.setattr(update, "parse_document", edit_after_read)
    with pytest.raises(WriteConceptError) as updated:
        write_concept(bundle, "a", type="Guide", title="No")
    assert updated.value.code == "rev_conflict"
    assert (tmp_path / "a.md").read_text(encoding="utf-8").endswith("external\n")

    monkeypatch.setattr(update, "parse_document", real_parse)
    real_serialize = update.serialize_document_round_trip

    def create_after_plan(*args: object, **kwargs: object) -> str:
        raw = real_serialize(*args, **kwargs)
        (tmp_path / "new.md").write_text("external create\n", encoding="utf-8")
        return raw

    monkeypatch.setattr(update, "serialize_document_round_trip", create_after_plan)
    with pytest.raises(WriteConceptError) as created:
        write_concept(bundle, "new", type="Guide")
    assert created.value.code == "rev_conflict"
    assert (tmp_path / "new.md").read_text(encoding="utf-8") == "external create\n"


def _append_in_process(root: str, entry: str) -> None:
    append_log_entry(
        Bundle.load(root), kind="Update", entry=entry, date_str="2026-07-11"
    )


def _race_plan_worker(root: str, barrier: object, output: object, studio_mode: bool) -> None:
    bundle = Bundle.load(root)
    plan = _plan(UpdateOp("add_tag", ("a",), {"tag": "race"}))
    barrier.wait()
    result = apply_plan(
        bundle, plan, studio=Studio.for_bundle(root) if studio_mode else None
    )
    output.put(result["applied"])


def _race_index_worker(root: str, barrier: object, output: object) -> None:
    bundle = Bundle.load(root)
    barrier.wait()
    try:
        regenerate_indexes(bundle)
    except Exception:
        output.put("conflict")
    else:
        output.put("success")


def _group_member_worker(root: str, barrier: object, concept: str) -> None:
    studio = Studio.for_bundle(root)
    studio.ensure_session()
    barrier.wait()
    studio.snapshot_for_undo(concept_id=concept, raw=f"before {concept}", group_id="MP-GROUP")


def _sidecar_second_writer(root: str, started: object, output: object) -> None:
    started.set()
    studio = Studio.for_bundle(root)
    studio.ensure_session()
    event = studio.append_event({"type": "second-writer", "value": "keep"})
    studio.mark_logged("b", b"second logged")
    rev = studio.snapshot_for_undo(
        concept_id="b", raw=b"second snapshot", group_id="SHARED-GROUP"
    )
    output.put((event["event_id"], rev))


def _near_cap_worker(root: str, barrier: object, output: object, target: str) -> None:
    bundle = Bundle.load(root)
    studio = Studio.for_bundle(root)
    studio.ensure_session()
    studio.constraints = {"max_edits_per_action": 1}
    barrier.wait()
    try:
        result = apply_plan(
            bundle,
            _plan(UpdateOp("add_tag", (target,), {"tag": "capacity"})),
            studio=studio,
            group_id="CAP-GROUP",
        )
        output.put((target, result["applied"]))
    except Exception:
        output.put((target, 0))


def _append_cross_process_event(root: str) -> None:
    studio = Studio.for_bundle(root)
    studio.ensure_session()
    studio.append_event({"type": "cross-process-exact", "value": 1})


def _append_cross_process_burst(root: str, count: int, max_bytes: int, keep: int) -> None:
    studio = Studio.for_bundle(root)
    studio.events_max_bytes = max_bytes
    studio.events_keep = keep
    studio.ensure_session()
    try:
        for index in range(count):
            studio.append_event(
                {"type": "cross-process-burst", "index": index}, publish=False
            )
    finally:
        studio.close()


def _blocked_burst_child(root: str, started: object, hold: object) -> None:
    """Same session setup as the burst worker, then park on a test event."""
    studio = Studio.for_bundle(root)
    studio.ensure_session()
    started.set()
    try:
        assert hold.wait(120), "test harness released the blocked child"
    finally:
        studio.close()


def _stop_owned_process(process: multiprocessing.process.BaseProcess,
                        *, join_timeout: float = 5.0) -> dict:
    """Unconditionally stop and reap an owned child, then close its handle.

    Ordering is terminate → join → kill → join → reap → close: a cooperative
    child dies on the first TERM; only a child that resists TERM escalates to
    KILL. ``join`` reaps the pid so no zombie survives the failure path, and
    ``close`` releases the process handle either way. Returns the recorded
    cleanup steps and final exitcode because ``close`` invalidates the object.
    """
    steps: list[str] = []
    if process.is_alive():
        process.terminate()
        steps.append("terminate")
        process.join(join_timeout)
        steps.append("join")
    if process.is_alive():
        process.kill()
        steps.append("kill")
        process.join(join_timeout)
        steps.append("join-kill")
    assert not process.is_alive(), "owned child survived terminate and kill"
    steps.append("reap")
    outcome = {"steps": steps, "exitcode": process.exitcode}
    process.close()
    steps.append("close")
    return outcome


def _run_cross_process_burst(
    tmp_path: Path, *, count: int = 0, fail_after_start: BaseException | None = None,
    child_target: object = _append_cross_process_burst, child_args: tuple = (),
) -> tuple[BaseException | None, dict, list[list[dict]], list[Path]]:
    """Run the burst workload with unconditional owned-child cleanup.

    The shared contract for the drain proof and its deterministic failure
    path: whatever happens after the child starts (clean exit, drain
    failure, join timeout, injected failure), the finally block stops,
    reaps and closes the owned child and closes the parent Studio. The
    original failure, if any, is returned (not raised) so the test can
    assert on the recorded cleanup alongside it.
    """
    _root(tmp_path)
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    ctx = multiprocessing.get_context("spawn")
    if child_target is _append_cross_process_burst:
        target_args: tuple = (str(tmp_path), count, 16 * 1024, 1000)
    else:
        target_args = (str(tmp_path), *child_args)
    process = ctx.Process(target=child_target, args=target_args)
    process.start()
    failure: BaseException | None = None
    batches: list[list[dict]] = []
    archives: list[Path] = []
    cleanup: dict = {"steps": [], "exitcode": None}
    try:
        if fail_after_start is not None:
            raise fail_after_start
        process.join(30)
        if process.exitcode != 0:
            raise AssertionError(f"burst child failed: exitcode={process.exitcode!r}")
        for _ in range(10):
            batch = studio.tail_and_broadcast_cross_process_events()
            batches.append(batch)
            if not batch:
                break
        # Empty repeated tail: the cursor is exhausted and stays exhausted.
        batches.append(studio.tail_and_broadcast_cross_process_events())
        batches.append(studio.tail_and_broadcast_cross_process_events())
        archives = sorted(studio.events_path.parent.glob(
            f"{studio.events_path.stem}-*-*{studio.events_path.suffix}"))
    except BaseException as exc:  # returned after unconditional cleanup
        failure = exc
    finally:
        cleanup = _stop_owned_process(process)
        studio.close()
    return failure, cleanup, batches, archives


def test_log_cross_process_appends_do_not_lose_entries(tmp_path: Path) -> None:
    _root(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    processes = [
        ctx.Process(target=_append_in_process, args=(str(tmp_path), f"p{i}"))
        for i in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)
        assert process.exitcode == 0
    raw = (tmp_path / "log.md").read_text(encoding="utf-8")
    for i in range(8):
        assert raw.count(f"* **Update**: p{i}\n") == 1


@pytest.mark.parametrize("studio_mode", [False, True])
@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_cross_process_plan_cas_has_one_success_one_conflict(
    tmp_path: Path, studio_mode: bool
) -> None:
    _root(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    output = ctx.Queue()
    processes = [
        ctx.Process(target=_race_plan_worker, args=(str(tmp_path), barrier, output, studio_mode))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)
        assert process.exitcode == 0
    assert sorted(output.get(timeout=1) for _ in processes) == [0, 1]


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_cross_process_index_create_cas_has_one_success_one_conflict(tmp_path: Path) -> None:
    _root(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    output = ctx.Queue()
    processes = [
        ctx.Process(target=_race_index_worker, args=(str(tmp_path), barrier, output))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)
        assert process.exitcode == 0
    assert sorted(output.get(timeout=1) for _ in processes) == ["conflict", "success"]


def test_rollback_conflict_preserves_newer_external_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.update as update
    from okf_loom.io_utils import atomic_write_text as real_write

    bundle = Bundle.load(_root(tmp_path))
    calls = 0

    def fail_second(path: Path, raw: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            (tmp_path / "a.md").write_text("newer external bytes\n", encoding="utf-8")
            raise OSError("second failed")
        real_write(path, raw)

    monkeypatch.setattr(update, "atomic_write_text", fail_second)
    with pytest.raises(RuntimeError, match="rollback conflict"):
        apply_plan(bundle, _plan(
            UpdateOp("add_tag", ("a",), {"tag": "one"}),
            UpdateOp("add_tag", ("b",), {"tag": "two"}),
        ))
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "newer external bytes\n"


@pytest.mark.parametrize("failure", ["snapshot", "mark", "activity", "graph", "change"])
def test_post_persistence_finalization_failure_rolls_back_complete_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    bundle = Bundle.load(_root(tmp_path))
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    before = (tmp_path / "a.md").read_bytes()

    def fail(*_args: object, **_kwargs: object):
        raise OSError(f"{failure} finalization")

    if failure == "snapshot":
        monkeypatch.setattr(studio, "snapshot_many_for_undo", fail)
    elif failure == "mark":
        monkeypatch.setattr(studio, "mark_logged", fail)
    elif failure == "activity":
        monkeypatch.setattr(studio, "record_activity", fail)
    else:
        original_append = studio.append_event

        def conditional(event: dict, **kwargs: object):
            target_type = "changed" if failure == "change" else failure
            if event.get("type") == target_type:
                raise OSError(f"{failure} finalization")
            return original_append(event, **kwargs)

        monkeypatch.setattr(studio, "append_event", conditional)

    with pytest.raises(OSError, match="finalization"):
        apply_plan(bundle, _plan(UpdateOp(
            "add_link", ("a",), {"target_concept_id": "b", "label": "B"}
        )), studio=studio, group_id="FINALIZE")

    assert (tmp_path / "a.md").read_bytes() == before
    assert not studio.read_events(limit=100)
    assert not list(studio.history_dir.rglob("*.md"))
    assert studio.undo_group("FINALIZE") is None
    assert not studio.last_logged_path.exists()


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_cross_process_group_manifest_merge_has_no_lost_members(tmp_path: Path) -> None:
    _root(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    concepts = [f"concept-{i}" for i in range(8)]
    barrier = ctx.Barrier(len(concepts))
    processes = [
        ctx.Process(target=_group_member_worker, args=(str(tmp_path), barrier, concept))
        for concept in concepts
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)
        assert process.exitcode == 0
    members = Studio.for_bundle(tmp_path).undo_group("MP-GROUP") or []
    assert {member["concept"] for member in members} == set(concepts)
    for member in members:
        assert Studio.for_bundle(tmp_path).undo_snapshot(
            concept_id=member["concept"], rev=member["rev"]
        ) == f"before {member['concept']}"


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_failed_finalization_sidecar_lock_preserves_second_writer_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = Bundle.load(_root(tmp_path))
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    entered = threading.Event()
    release = threading.Event()
    original_emit = studio.emit_change

    def pause_then_fail(**kwargs: object):
        entered.set()
        assert release.wait(30)
        raise OSError("late finalization failure")

    monkeypatch.setattr(studio, "emit_change", pause_then_fail)
    errors: list[BaseException] = []

    def mutate() -> None:
        try:
            apply_plan(bundle, _plan(UpdateOp(
                "add_link", ("a",), {"target_concept_id": "b", "label": "B"}
            )), studio=studio, group_id="FAILED-GROUP")
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=mutate)
    thread.start()
    assert entered.wait(3)
    ctx = multiprocessing.get_context("spawn")
    started = ctx.Event()
    output = ctx.Queue()
    process = ctx.Process(
        target=_sidecar_second_writer, args=(str(tmp_path), started, output)
    )
    process.start()
    assert started.wait(10)
    time.sleep(0.1)
    assert process.is_alive(), "second writer bypassed transaction-wide sidecar lock"
    release.set()
    thread.join(5)
    process.join(5)
    assert errors and isinstance(errors[0], OSError)
    assert process.exitcode == 0
    event_id, snapshot_rev = output.get(timeout=1)
    fresh = Studio.for_bundle(tmp_path)
    assert any(event.get("event_id") == event_id for event in fresh.read_events(limit=100))
    assert fresh._last_logged_rev["b"] == rev_of(b"second logged")
    assert fresh.undo_snapshot(concept_id="b", rev=snapshot_rev) == "second snapshot"
    assert {row["concept"] for row in (fresh.undo_group("SHARED-GROUP") or [])} == {"b"}
    assert fresh.undo_group("FAILED-GROUP") is None
    monkeypatch.setattr(studio, "emit_change", original_emit)


def test_staged_events_are_exact_with_rotation_and_large_prior_feed(tmp_path: Path) -> None:
    bundle = Bundle.load(_root(tmp_path))
    studio = Studio.for_bundle(tmp_path)
    studio.events_max_bytes = 512
    studio.events_keep = 20
    studio.ensure_session()
    for index in range(1105):
        studio.append_event({"type": "prior", "index": index}, publish=False)
    observed: list[dict] = []
    handle = studio.bus.subscribe(lambda event: observed.append(event))
    try:
        apply_plan(bundle, _plan(UpdateOp(
            "add_link", ("a",), {"target_concept_id": "b", "label": "B"}
        )), studio=studio)
        time.sleep(0.1)
    finally:
        studio.bus.unsubscribe(handle)
    produced = [event for event in observed if event.get("type") != "prior"]
    assert [event["type"] for event in produced] == ["activity", "graph", "changed"]
    keys = [f"eid:{event['event_id']}" for event in produced]
    assert all(key in studio._recently_published_events for key in keys)
    tailed = studio.tail_and_broadcast_cross_process_events()
    assert not any(event.get("event_id") in {row["event_id"] for row in produced} for event in tailed)


def test_cross_process_event_is_tailed_exactly_once(tmp_path: Path) -> None:
    _root(tmp_path)
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    observed: list[dict] = []
    handle = studio.bus.subscribe(lambda event: observed.append(event))
    process = multiprocessing.get_context("spawn").Process(
        target=_append_cross_process_event, args=(str(tmp_path),)
    )
    process.start()
    process.join(5)
    assert process.exitcode == 0
    try:
        first = studio.tail_and_broadcast_cross_process_events()
        second = studio.tail_and_broadcast_cross_process_events()
        time.sleep(0.05)
    finally:
        studio.bus.unsubscribe(handle)
    exact = [event for event in observed if event.get("type") == "cross-process-exact"]
    assert len(exact) == 1
    assert len([event for event in first if event.get("type") == "cross-process-exact"]) == 1
    assert not [event for event in second if event.get("type") == "cross-process-exact"]


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_deferred_group_capacity_is_reserved_across_processes(tmp_path: Path) -> None:
    _root(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    output = ctx.Queue()
    processes = [
        ctx.Process(target=_near_cap_worker, args=(str(tmp_path), barrier, output, target))
        for target in ("a", "b")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(5)
        assert process.exitcode == 0
    outcomes = [output.get(timeout=1) for _ in processes]
    assert sorted(applied for _target, applied in outcomes) == [0, 1]
    members = Studio.for_bundle(tmp_path).undo_group("CAP-GROUP") or []
    assert len(members) == 1
    persisted = {
        target: "capacity" in (tmp_path / f"{target}.md").read_text(encoding="utf-8")
        for target in ("a", "b")
    }
    assert sum(persisted.values()) == 1


@pytest.mark.parametrize("raw", [b"{broken", b"{}", b"[1]", b'[{"concept":1,"rev":"x"}]'])
def test_invalid_group_manifest_fails_closed_and_preserves_bytes(
    tmp_path: Path, raw: bytes
) -> None:
    _root(tmp_path)
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    manifest = studio.history_dir / "_groups" / "BAD" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_bytes(raw)
    with pytest.raises(ValueError, match="invalid group manifest"):
        studio.snapshot_for_undo(concept_id="a", raw=b"before", group_id="BAD")
    assert manifest.read_bytes() == raw
    assert not list((studio.history_dir / "61").glob("*.md"))


def test_unreadable_group_manifest_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root(tmp_path)
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    manifest = studio.history_dir / "_groups" / "UNREADABLE" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    raw = b'[{"concept":"existing","rev":"abc"}]'
    manifest.write_bytes(raw)
    original_read_text = Path.read_text

    def unreadable(path: Path, *args: object, **kwargs: object) -> str:
        if path.name == "manifest.json":
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(ValueError, match="invalid group manifest"):
        studio.snapshot_for_undo(
            concept_id="a", raw=b"before", group_id="UNREADABLE"
        )
    assert manifest.read_bytes() == raw
    assert not list((studio.history_dir / "61").glob("*.md"))


@pytest.mark.parametrize("cap,expected", [(1, 0), (2, 2)])
def test_deferred_multi_target_capacity_preflight_is_atomic(
    tmp_path: Path, cap: int, expected: int
) -> None:
    bundle = Bundle.load(_root(tmp_path))
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    studio.constraints = {"max_edits_per_action": cap}
    before = {(tmp_path / name).read_bytes() for name in ("a.md", "b.md")}
    result = apply_plan(bundle, _plan(
        UpdateOp("add_tag", ("a",), {"tag": "multi"}),
        UpdateOp("add_tag", ("b",), {"tag": "multi"}),
    ), studio=studio, group_id="MULTI-CAP")
    assert result["applied"] == expected
    if expected == 0:
        assert {(tmp_path / name).read_bytes() for name in ("a.md", "b.md")} == before
        assert studio.undo_group("MULTI-CAP") is None
    else:
        assert len(studio.undo_group("MULTI-CAP") or []) == 2


def _index_bundle(tmp_path: Path) -> Bundle:
    _root(tmp_path)
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "c.md").write_text(
        "---\ntype: Guide\ntitle: C\n---\n# C\n", encoding="utf-8"
    )
    return Bundle.load(tmp_path)


def test_grouped_index_preflight_rejects_malformed_manifest_before_content(
    tmp_path: Path,
) -> None:
    bundle = _index_bundle(tmp_path)
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    manifest = studio.history_dir / "_groups" / "INDEX-BAD" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    raw = b"{broken"
    manifest.write_bytes(raw)
    with pytest.raises(ValueError, match="invalid group manifest"):
        regenerate_indexes(bundle, studio=studio, group_id="INDEX-BAD")
    assert manifest.read_bytes() == raw
    assert not (tmp_path / "index.md").exists()
    assert not (tmp_path / "nested" / "index.md").exists()


@pytest.mark.parametrize("cap,success", [(1, False), (2, True)])
def test_grouped_index_nonundoable_writes_account_against_capacity(
    tmp_path: Path, cap: int, success: bool
) -> None:
    bundle = _index_bundle(tmp_path)
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    studio.constraints = {"max_edits_per_action": cap}
    if success:
        regenerate_indexes(bundle, studio=studio, group_id="INDEX-CAP")
        members = studio.undo_group("INDEX-CAP") or []
        assert {member["concept"] for member in members} == {"index", "nested/index"}
    else:
        with pytest.raises(Exception, match="max_edits_per_action"):
            regenerate_indexes(bundle, studio=studio, group_id="INDEX-CAP")
        assert not (tmp_path / "index.md").exists()
        assert not (tmp_path / "nested" / "index.md").exists()
        assert studio.undo_group("INDEX-CAP") is None


def test_staged_event_keys_registered_before_direct_publish_and_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = Bundle.load(_root(tmp_path))
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    entered = threading.Event()
    release = threading.Event()
    original_publish = studio.bus.publish
    calls = 0

    def paused_publish(event: dict) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(3)
        original_publish(event)

    monkeypatch.setattr(studio.bus, "publish", paused_publish)
    thread = threading.Thread(target=lambda: apply_plan(
        bundle,
        _plan(UpdateOp("add_link", ("a",), {
            "target_concept_id": "b", "label": "B",
        })),
        studio=studio,
    ))
    thread.start()
    assert entered.wait(3)
    assert studio.tail_and_broadcast_cross_process_events() == []
    release.set()
    thread.join(5)
    assert not thread.is_alive()


def test_durable_event_cursor_drains_cross_process_burst_across_rotation(
    tmp_path: Path,
) -> None:
    # 501 events with a 16 KiB active-file cap: large enough to force real
    # multiple rotations on disk, past the historical last-500 tail
    # truncation AND past the current 256-row single-tail batch (so the
    # drain genuinely spans several batches). Every archive is retained, so
    # retention never prunes the cursor's generation and no resync replaces
    # the exact ordered drain.
    failure, cleanup, batches, archives = _run_cross_process_burst(tmp_path, count=501)
    # Owned child exited cleanly and the unconditional guard reaped and
    # closed it (no terminate was needed).
    assert failure is None, failure
    assert cleanup["steps"] == ["reap", "close"], cleanup
    assert cleanup["exitcode"] == 0, cleanup
    # Real multiple rotations happened on disk, not one rollover.
    assert len(archives) >= 2, archives
    drained = [
        event for batch in batches for event in batch
        if event.get("type") == "cross-process-burst"
    ]
    # No retention resync: every rotated generation stayed readable, so the
    # drain is one exact ordered pass rather than a pruned-cursor restart.
    assert not [
        event for batch in batches for event in batch
        if event.get("type") == "resync"
    ]
    assert [event["index"] for event in drained] == list(range(501))
    # Multiple batches: the first tail is capped at the 256-row batch, the
    # remainder drains in a follow-up call (501 > 256 > old last-500 bug).
    non_empty = [batch for batch in batches if batch]
    assert [len(batch) for batch in non_empty] == [256, 245]
    # Empty repeated tail: both post-drain calls stay empty.
    assert batches[-2:] == [[], []]


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_owned_child_failure_path_stops_reaps_and_closes_deterministically(
    tmp_path: Path,
) -> None:
    # Whatever fails after the child starts, the owned child is terminated,
    # joined (reaped) and closed and the parent Studio closes — here proven
    # with the injected failure landing while the child is still alive.
    ctx = multiprocessing.get_context("spawn")
    failure, cleanup, batches, archives = _run_cross_process_burst(
        tmp_path,
        fail_after_start=RuntimeError("injected drain failure"),
        child_target=_blocked_burst_child,
        child_args=(ctx.Event(), ctx.Event()),
    )
    assert isinstance(failure, RuntimeError) and "injected" in str(failure), failure
    # The child was alive at cleanup, so TERM was issued, joined (reaped)
    # and the process handle closed before the failure was returned.
    assert cleanup["steps"] == ["terminate", "join", "reap", "close"], cleanup
    # Reaped, not a zombie: the SIGTERM death was acknowledged.
    assert cleanup["exitcode"] is not None, cleanup
    assert batches == [] and archives == []


def test_durable_event_cursor_reports_resync_when_retention_pruned_cursor(
    tmp_path: Path,
) -> None:
    _root(tmp_path)
    studio = Studio.for_bundle(tmp_path)
    studio.events_max_bytes = 1
    studio.events_keep = 1
    first = studio.append_event({"type": "retention-proof", "index": 0})
    studio._event_tail_cursor_key = f"eid:{first['event_id']}"
    for index in range(1, 6):
        studio.append_event({"type": "retention-proof", "index": index})
    assert studio.tail_and_broadcast_cross_process_events() == [
        {"type": "resync", "reason": "retention_gap"}
    ]


def test_direct_append_is_tail_ineligible_before_direct_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.studio as studio_module

    _root(tmp_path)
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    entered = threading.Event()
    release = threading.Event()
    original_append = studio_module._append_jsonl

    def paused_append(path: Path, obj: dict, **kwargs: object) -> None:
        original_append(path, obj, **kwargs)
        if obj.get("type") == "direct-race":
            entered.set()
            assert release.wait(3)

    monkeypatch.setattr(studio_module, "_append_jsonl", paused_append)
    observed: list[dict] = []
    handle = studio.bus.subscribe(lambda event: observed.append(event))
    appended: list[dict] = []
    writer = threading.Thread(target=lambda: appended.append(
        studio.append_event({"type": "direct-race"}, publish=True)
    ))
    writer.start()
    assert entered.wait(3)
    tailed: list[list[dict]] = []
    tailer = threading.Thread(
        target=lambda: tailed.append(studio.tail_and_broadcast_cross_process_events())
    )
    tailer.start()
    time.sleep(0.05)
    assert tailer.is_alive(), "tail classification bypassed sidecar publication lock"
    release.set()
    writer.join(3)
    tailer.join(3)
    time.sleep(0.05)
    studio.bus.unsubscribe(handle)
    assert len(appended) == 1
    event_id = appended[0]["event_id"]
    assert [event["event_id"] for event in observed].count(event_id) == 1
    assert not any(event.get("event_id") == event_id for event in tailed[0])


def test_staged_append_is_tail_ineligible_before_key_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.studio as studio_module

    bundle = Bundle.load(_root(tmp_path))
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    entered = threading.Event()
    release = threading.Event()
    original_append = studio_module._append_jsonl

    def paused_append(path: Path, obj: dict, **kwargs: object) -> None:
        original_append(path, obj, **kwargs)
        if obj.get("type") == "activity":
            entered.set()
            assert release.wait(3)

    monkeypatch.setattr(studio_module, "_append_jsonl", paused_append)
    observed: list[dict] = []
    handle = studio.bus.subscribe(lambda event: observed.append(event))
    writer = threading.Thread(target=lambda: apply_plan(
        bundle,
        _plan(UpdateOp("add_link", ("a",), {
            "target_concept_id": "b", "label": "B",
        })),
        studio=studio,
    ))
    writer.start()
    assert entered.wait(3)
    tailed: list[list[dict]] = []
    tailer = threading.Thread(
        target=lambda: tailed.append(studio.tail_and_broadcast_cross_process_events())
    )
    tailer.start()
    time.sleep(0.05)
    assert tailer.is_alive(), "tail observed staged row before canonical registration"
    release.set()
    writer.join(3)
    tailer.join(3)
    time.sleep(0.05)
    studio.bus.unsubscribe(handle)
    produced = [event for event in observed if event.get("type") in {
        "activity", "graph", "changed",
    }]
    assert [event["type"] for event in produced] == ["activity", "graph", "changed"]
    produced_ids = {event["event_id"] for event in produced}
    assert not any(event.get("event_id") in produced_ids for event in tailed[0])


@pytest.mark.parametrize(
    "existing,requested,cap,expected",
    [
        (("a", "b"), ("a",), 2, 1),
        (("a",), ("a", "b"), 2, 2),
        (("a", "b"), ("a", "c"), 2, 0),
    ],
)
def test_group_capacity_uses_unique_member_union(
    tmp_path: Path,
    existing: tuple[str, ...],
    requested: tuple[str, ...],
    cap: int,
    expected: int,
) -> None:
    bundle = Bundle.load(_root(tmp_path))
    if "c" in requested:
        (tmp_path / "c.md").write_text(
            "---\ntype: Guide\ntitle: C\n---\n# C\n", encoding="utf-8"
        )
        bundle = Bundle.load(tmp_path)
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    for concept in existing:
        studio.snapshot_for_undo(
            concept_id=concept, raw=f"existing {concept}", group_id="UNION"
        )
    studio.constraints = {"max_edits_per_action": cap}
    before = {
        concept: (tmp_path / f"{concept}.md").read_bytes()
        for concept in requested
    }
    result = apply_plan(
        bundle,
        _plan(*(UpdateOp("add_tag", (concept,), {"tag": "union"})
                for concept in requested)),
        studio=studio,
        group_id="UNION",
    )
    assert result["applied"] == expected
    if expected == 0:
        assert {
            concept: (tmp_path / f"{concept}.md").read_bytes()
            for concept in requested
        } == before


def test_write_concept_refreshes_only_affected_concept(
    tmp_path: Path,
) -> None:
    bundle = Bundle.load(_root(tmp_path))
    pending = bundle.concepts[("b",)]
    pending.body += "\npending in memory\n"
    pending.frontmatter["custom_pending"] = ["keep"]
    exact_pending = _state(bundle, ("b",))

    write_concept(bundle, "a", type="Guide", title="Updated")

    assert _state(bundle, ("b",)) == exact_pending
    assert bundle.concepts[("b",)] is pending
    assert bundle.concepts[("a",)].frontmatter["title"] == "Updated"


def test_retrieval_observes_old_or_new_only_while_commit_holds_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.update as update
    from okf_loom.io_utils import atomic_write_text as real_atomic_write

    bundle = Bundle.load(_root(tmp_path))
    assert RetrievalService(bundle).search(SearchRequest("oldword"))
    entered = threading.Event()
    release = threading.Event()
    observed: list[str] = []

    def paused_write(path: Path, raw: str) -> None:
        entered.set()
        assert release.wait(3)
        real_atomic_write(path, raw)

    monkeypatch.setattr(update, "atomic_write_text", paused_write)
    writer = threading.Thread(target=lambda: apply_plan(bundle, _plan(UpdateOp(
        "replace_text", ("a",), {"old": "oldword", "new": "newword"}
    ))))
    writer.start()
    assert entered.wait(2)

    def retrieve() -> None:
        results = RetrievalService(bundle).search(SearchRequest("newword"))
        observed.append(results[0].evidence["passage"]["excerpt"] if results else "old")

    reader = threading.Thread(target=retrieve)
    reader.start()
    time.sleep(0.05)
    assert reader.is_alive(), "reader bypassed mutation ownership lock"
    release.set()
    writer.join(3)
    reader.join(3)
    assert not writer.is_alive() and not reader.is_alive()
    assert observed and "newword" in observed[0]


def test_reload_retries_when_mutation_publishes_after_disk_scan_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = Bundle.load(_root(tmp_path))
    original_load = Bundle.load.__func__
    first_scan_complete = threading.Event()
    resume_reload = threading.Event()
    load_calls = 0

    def paused_load(cls: type[Bundle], root: str | Path, *, name: str | None = None):
        nonlocal load_calls
        fresh = original_load(cls, root, name=name)
        load_calls += 1
        if load_calls == 1:
            first_scan_complete.set()
            assert resume_reload.wait(5)
        return fresh

    monkeypatch.setattr(Bundle, "load", classmethod(paused_load))
    reload_thread = threading.Thread(target=bundle.reload)
    reload_thread.start()
    assert first_scan_complete.wait(3)

    result = apply_plan(
        bundle,
        _plan(UpdateOp("add_tag", ("a",), {"tag": "committed-during-reload"})),
    )
    assert result["applied"] == 1
    assert "committed-during-reload" in bundle.concepts[("a",)].frontmatter["tags"]

    resume_reload.set()
    reload_thread.join(5)
    assert not reload_thread.is_alive()
    assert load_calls >= 2, "reload published its stale first filesystem scan"
    assert "committed-during-reload" in bundle.concepts[("a",)].frontmatter["tags"]
    assert "committed-during-reload" in (
        tmp_path / "a.md"
    ).read_text(encoding="utf-8")


def test_reload_retries_after_scanning_failed_transaction_temporary_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.update as update
    from okf_loom.io_utils import atomic_write_text as real_atomic_write

    bundle = Bundle.load(_root(tmp_path))
    retained_a = _state(bundle, ("a",))
    retained_b = _state(bundle, ("b",))
    disk_a = (tmp_path / "a.md").read_bytes()
    disk_b = (tmp_path / "b.md").read_bytes()
    original_load = Bundle.load.__func__
    reload_scan_started = threading.Event()
    first_candidate_visible = threading.Event()
    transient_scan_complete = threading.Event()
    resume_reload = threading.Event()
    force_failure = threading.Event()
    load_calls = 0

    def controlled_load(cls: type[Bundle], root: str | Path, *, name: str | None = None):
        nonlocal load_calls
        load_calls += 1
        if load_calls == 1:
            reload_scan_started.set()
            assert first_candidate_visible.wait(5)
            fresh = original_load(cls, root, name=name)
            assert "temporary-transaction" in fresh.concepts[("a",)].frontmatter["tags"]
            transient_scan_complete.set()
            assert resume_reload.wait(5)
            return fresh
        return original_load(cls, root, name=name)

    monkeypatch.setattr(Bundle, "load", classmethod(controlled_load))
    writes = 0

    def fail_second(path: Path, raw: str) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            real_atomic_write(path, raw)
            first_candidate_visible.set()
            return
        assert force_failure.wait(5)
        raise OSError("forced later transaction failure")

    monkeypatch.setattr(update, "atomic_write_text", fail_second)
    reload_thread = threading.Thread(target=bundle.reload)
    reload_thread.start()
    assert reload_scan_started.wait(3)

    mutation_errors: list[BaseException] = []

    def mutate() -> None:
        try:
            apply_plan(bundle, _plan(
                UpdateOp("add_tag", ("a",), {"tag": "temporary-transaction"}),
                UpdateOp("add_tag", ("b",), {"tag": "later-failure"}),
            ))
        except BaseException as exc:
            mutation_errors.append(exc)

    mutation_thread = threading.Thread(target=mutate)
    mutation_thread.start()
    assert transient_scan_complete.wait(5)
    force_failure.set()
    mutation_thread.join(5)
    assert mutation_errors and isinstance(mutation_errors[0], OSError)
    assert (tmp_path / "a.md").read_bytes() == disk_a
    assert (tmp_path / "b.md").read_bytes() == disk_b

    resume_reload.set()
    reload_thread.join(5)
    assert not reload_thread.is_alive()
    assert load_calls >= 2, "reload published bytes from a rolled-back transaction"
    assert _state(bundle, ("a",)) == retained_a
    assert _state(bundle, ("b",)) == retained_b
    assert bundle.concepts[("a",)].raw_text.encode("utf-8") == disk_a
    assert bundle.concepts[("b",)].raw_text.encode("utf-8") == disk_b


def test_index_and_log_publish_snapshots_and_revision_immediately(tmp_path: Path) -> None:
    bundle = Bundle.load(_root(tmp_path))
    revision0 = RetrievalService(bundle).revision

    regenerate_indexes(bundle)
    revision1 = RetrievalService(bundle).revision
    assert revision1 != revision0
    assert bundle.indexes[Path("index.md")].raw_text == (
        tmp_path / "index.md"
    ).read_text(encoding="utf-8")

    append_log_entry(bundle, kind="Update", entry="retained", date_str="2026-07-10")
    revision2 = RetrievalService(bundle).revision
    assert revision2 != revision1
    assert bundle.logs[Path("log.md")].raw_text == (
        tmp_path / "log.md"
    ).read_text(encoding="utf-8")


def test_index_and_log_atomic_failures_leave_retained_revision_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.index as index_module
    import okf_loom.log as log_module

    bundle = Bundle.load(_root(tmp_path))
    revision = RetrievalService(bundle).revision
    passages = RetrievalService(bundle).passages

    monkeypatch.setattr(
        index_module,
        "atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("index failure")),
    )
    with pytest.raises(OSError, match="index failure"):
        regenerate_indexes(bundle)
    assert Path("index.md") not in bundle.indexes
    assert not (tmp_path / "index.md").exists()
    assert RetrievalService(bundle).revision == revision
    assert RetrievalService(bundle).passages is passages

    monkeypatch.setattr(
        log_module,
        "atomic_write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("log failure")),
    )
    with pytest.raises(OSError, match="log failure"):
        append_log_entry(bundle, kind="Update", entry="no", date_str="2026-07-10")
    assert Path("log.md") not in bundle.logs
    assert not (tmp_path / "log.md").exists()
    assert RetrievalService(bundle).revision == revision
    assert RetrievalService(bundle).passages is passages


def test_multi_index_studio_failure_has_no_public_or_history_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root(tmp_path)
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "c.md").write_text(
        "---\ntype: Guide\ntitle: C\n---\n# C\n", encoding="utf-8"
    )
    bundle = Bundle.load(tmp_path)
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    original_save = studio.save_concept
    calls = 0

    def fail_second(**kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second index failed")
        return original_save(**kwargs)

    monkeypatch.setattr(studio, "save_concept", fail_second)
    with pytest.raises(OSError, match="second index"):
        regenerate_indexes(bundle, studio=studio, group_id="INDEX-TX")

    assert not (tmp_path / "index.md").exists()
    assert not (tmp_path / "nested" / "index.md").exists()
    assert not [
        event for event in studio.read_events(limit=100)
        if event.get("type") in {"activity", "changed", "graph"}
    ]
    assert not list(studio.history_dir.rglob("*.md"))
    assert studio.undo_group("INDEX-TX") is None


def test_index_regeneration_conflicts_with_external_edit(tmp_path: Path) -> None:
    bundle = Bundle.load(_root(tmp_path))
    regenerate_indexes(bundle)
    bundle = Bundle.load(tmp_path)
    path = tmp_path / "index.md"
    bundle.concepts[("a",)].frontmatter["title"] = "Changed in plan"
    external = path.read_text(encoding="utf-8") + "\nhand edit\n"
    path.write_text(external, encoding="utf-8")

    with pytest.raises(Exception, match="index write conflict"):
        regenerate_indexes(bundle)

    assert path.read_text(encoding="utf-8") == external
