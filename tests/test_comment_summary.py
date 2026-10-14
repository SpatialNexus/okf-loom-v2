"""Portable V2 comment-summary contract proofs."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.cli import main
from okf_loom.render import build_graph_data, build_site, render_single_file
from okf_loom.server import OKFWikiHandler
from okf_loom.io_utils import path_lock
from okf_loom.directive_store import DirectiveStore
from okf_loom.studio import (
    CommentMutationError,
    CommentSummaryUnavailable,
    Studio,
    canonical_comment_summary_revision,
    read_comment_summary,
    read_event_snapshot,
)


def _row(comment_id: str, concept: str, **updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": comment_id,
        "concept": concept,
        "state": "open",
        "archived": False,
        "body": f"private body {comment_id}",
        "anchor": {"quote": f"private anchor {comment_id}"},
        "row_token": f"private-token-{comment_id}",
        "parent_id": None,
        "actor": "private-actor",
        "session": {"secret": "private-session"},
        "created_at": "private-timestamp",
        "seq": 91,
        "rev": "private-revision",
        "event_cursor": "private-cursor",
        "unknown_private_extension": "private-extension",
    }
    row.update(updates)
    return row


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / "..directives-transaction.okf-lock").touch(mode=0o600)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _seed_authority_lock(session: Path) -> None:
    session.mkdir(parents=True, exist_ok=True)
    (session / "..directives-transaction.okf-lock").touch(mode=0o600)


def _bundle(root: Path) -> Bundle:
    (root / "tables").mkdir(parents=True, exist_ok=True)
    (root / "tables" / "orders.md").write_text(
        "---\ntype: Table\ntitle: Orders\n---\nOrders.\n", encoding="utf-8"
    )
    (root / "tables" / "customers.md").write_text(
        "---\ntype: Table\ntitle: Customers\n---\nCustomers.\n", encoding="utf-8"
    )
    return Bundle.load(root)


def test_summary_reads_checkpoint_rotations_with_lww_archive_replies_and_legacy(tmp_path: Path) -> None:
    session = tmp_path / ".okf-loom" / "session"
    _seed_authority_lock(session)
    (session / "directives-checkpoint.json").write_text(json.dumps({
        "version": 1,
        "through_rotation": 0,
        "rows": [
            _row("A", "tables/orders"),
            _row("ARCHIVED", "tables/orders", state="archived"),
        ],
    }), encoding="utf-8")
    _write_jsonl(session / "directives-20260717-000000000001.jsonl", [
        _row("A", "tables/orders", state="claimed"),
        _row("PARENT", "tables/orders", archived=True),
    ])
    _write_jsonl(session / "directives.jsonl", [
        _row("A", "tables/orders", state="resolved"),
        _row("REPLY", "tables/orders", parent_id="PARENT"),
        _row("CUSTOMER", "tables/customers", state="claimed"),
        _row("GHOST", "not/a/graph-node"),
    ])

    summary = read_comment_summary(
        session,
        graph_concepts={"tables/orders", "tables/customers"},
    )

    assert summary["schema"] == 1
    assert summary["semantics"] == "active-comment-row"
    assert summary["counts"] == {"tables/customers": 1, "tables/orders": 1}
    assert re.fullmatch(r"[0-9a-f]{64}", summary["summary_revision"])
    serialized = json.dumps(summary, sort_keys=True)
    for secret in (
        "private body", "private anchor", "private-token", "private-parent",
        "private-actor", "private-session", "private-timestamp", "private-revision",
        "private-cursor", "private-extension", "REPLY", "CUSTOMER",
    ):
        assert secret not in serialized


def test_summary_checkpoint_watermark_ignores_retained_pre_checkpoint_rotation(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    studio = Studio.for_bundle(bundle.root)
    studio.DIRECTIVES_MAX_BYTES = 1
    studio.DIRECTIVES_KEEP = 3
    created = studio.post_comment(concept="tables/orders", body="private")
    # The resolve append executes the real rotate -> append -> checkpoint ->
    # prune ordering. Generation 1 remains retained after its newer resolved
    # row has been incorporated into the checkpoint.
    studio.resolve_comment(created["id"])
    assert list(studio.session_dir.glob("directives-*-*.jsonl"))

    summary = read_comment_summary(studio.session_dir, graph_concepts={"tables/orders"})
    assert summary["counts"] == {}


def test_legacy_checkpoint_with_retained_rotations_is_upgraded_by_writer_flow(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    studio = Studio.for_bundle(bundle.root)
    studio.DIRECTIVES_MAX_BYTES = 1
    studio.DIRECTIVES_KEEP = 3
    created = studio.post_comment(concept="tables/orders", body="private")
    studio.update_comment(created["id"], state="claimed", claimed_by="agent")
    checkpoint = json.loads(studio.directives_checkpoint_path.read_text(encoding="utf-8"))
    checkpoint.pop("through_rotation")
    studio.directives_checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    assert list(studio.session_dir.glob("directives-*-*.jsonl"))

    # The read-only summary cannot invent chronology.  The normal locked
    # writer path proves the historical active-file prefix, upgrades atomically,
    # and then appends the lifecycle row.
    with pytest.raises(CommentSummaryUnavailable):
        read_comment_summary(studio.session_dir, graph_concepts={"tables/orders"})
    studio.resolve_comment(created["id"], summary="done")

    upgraded = json.loads(studio.directives_checkpoint_path.read_text(encoding="utf-8"))
    assert type(upgraded["through_rotation"]) is int
    assert studio.get_comment(created["id"])["state"] == "resolved"
    assert read_comment_summary(
        studio.session_dir, graph_concepts={"tables/orders"})["counts"] == {}


def test_legacy_checkpoint_at_post_rotation_boundary_preserves_newer_active_state(
        tmp_path: Path) -> None:
    """A checkpoint may represent exactly the boundary before active JSONL."""
    bundle = _bundle(tmp_path / "bundle")
    studio = Studio.for_bundle(bundle.root)
    _seed_authority_lock(studio.session_dir)
    rotation_row = _row("BOUNDARY", "tables/orders", state="claimed")
    active_row = _row("BOUNDARY", "tables/orders", state="resolved")
    (studio.session_dir / "directives-20260717-000000000001.jsonl").write_text(
        json.dumps(rotation_row) + "\n", encoding="utf-8")
    studio.directives_checkpoint_path.write_text(json.dumps({
        "version": 1,
        "rows": [rotation_row],
    }), encoding="utf-8")
    studio.directives_path.write_text(
        json.dumps(active_row) + "\n", encoding="utf-8")

    comments = studio.list_comments()

    assert len(comments) == 1
    assert comments[0]["id"] == "BOUNDARY"
    assert comments[0]["state"] == "resolved"
    checkpoint = json.loads(studio.directives_checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["through_rotation"] == 1


def test_legacy_checkpoint_with_ambiguous_chronology_still_fails_closed(
        tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    studio = Studio.for_bundle(bundle.root)
    _seed_authority_lock(studio.session_dir)
    rotation_row = _row("AMBIGUOUS", "tables/orders", state="open")
    checkpoint_row = _row("AMBIGUOUS", "tables/orders", state="claimed")
    active_row = _row("AMBIGUOUS", "tables/orders", state="resolved")
    (studio.session_dir / "directives-20260717-000000000001.jsonl").write_text(
        json.dumps(rotation_row) + "\n", encoding="utf-8")
    before = json.dumps({"version": 1, "rows": [checkpoint_row]})
    studio.directives_checkpoint_path.write_text(before, encoding="utf-8")
    studio.directives_path.write_text(json.dumps(active_row) + "\n", encoding="utf-8")

    with pytest.raises(OSError, match="chronology is unprovable"):
        studio.list_comments()

    assert studio.directives_checkpoint_path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("corruption", ["checkpoint", "active-torn", "rotated-torn"])
def test_summary_corruption_is_unavailable_not_zero(tmp_path: Path, corruption: str) -> None:
    session = tmp_path / ".okf-loom" / "session"
    _seed_authority_lock(session)
    if corruption == "checkpoint":
        (session / "directives-checkpoint.json").write_text("{bad", encoding="utf-8")
    elif corruption == "active-torn":
        (session / "directives.jsonl").write_text('{"id":"A"', encoding="utf-8")
    else:
        (session / "directives-20260717-000000000001.jsonl").write_text(
            '{"id":"A"', encoding="utf-8"
        )

    with pytest.raises(CommentSummaryUnavailable):
        read_comment_summary(session, graph_concepts={"tables/orders"})


def test_summary_missing_authority_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(CommentSummaryUnavailable):
        read_comment_summary(tmp_path / "missing", graph_concepts={"tables/orders"})


@pytest.mark.parametrize("authority_name", [
    "directives.jsonl",
    "directives-20260717-000000000001.jsonl",
])
def test_summary_rejects_symlinked_jsonl_authority(tmp_path: Path, authority_name: str) -> None:
    session = tmp_path / "session"
    _seed_authority_lock(session)
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps(_row("A", "tables/orders")) + "\n", encoding="utf-8")
    (session / authority_name).symlink_to(outside)
    with pytest.raises(CommentSummaryUnavailable):
        read_comment_summary(session, graph_concepts={"tables/orders"})


@pytest.mark.parametrize("checkpoint_value", [
    [],
    {"version": 1, "rows": "not-a-list"},
    {"version": 1, "rows": ["not-an-object"]},
    {"version": 1, "rows": [_row("A", "tables/orders")], "through_rotation": True},
    {"version": 1, "rows": [_row("A", "tables/orders")], "through_rotation": "1"},
])
def test_summary_rejects_invalid_checkpoint_shapes(
        tmp_path: Path, checkpoint_value: object) -> None:
    session = tmp_path / "session"
    _seed_authority_lock(session)
    (session / "directives-checkpoint.json").write_text(
        json.dumps(checkpoint_value), encoding="utf-8")
    with pytest.raises(CommentSummaryUnavailable):
        read_comment_summary(session, graph_concepts={"tables/orders"})


@pytest.mark.parametrize("bad_row", [
    [],
    _row("A", "tables/orders", state="unknown"),
    _row("A", "tables/orders", archived=1),
    {**_row("A", "tables/orders"), "concept": []},
])
def test_summary_rejects_non_object_and_invalid_complete_rows(
        tmp_path: Path, bad_row: object) -> None:
    session = tmp_path / "session"
    _seed_authority_lock(session)
    (session / "directives.jsonl").write_text(
        json.dumps(bad_row) + "\n", encoding="utf-8")
    with pytest.raises(CommentSummaryUnavailable):
        read_comment_summary(session, graph_concepts={"tables/orders"})


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_summary_rejects_non_regular_checkpoint(tmp_path: Path, kind: str) -> None:
    session = tmp_path / "session"
    _seed_authority_lock(session)
    checkpoint = session / "directives-checkpoint.json"
    if kind == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"version": 1, "rows": []}), encoding="utf-8")
        checkpoint.symlink_to(outside)
    else:
        checkpoint.mkdir()
    with pytest.raises(CommentSummaryUnavailable):
        read_comment_summary(session, graph_concepts={"tables/orders"})


@pytest.mark.parametrize("kind", ["symlink", "directory"])
@pytest.mark.parametrize("consumer", ["list", "get", "mutate", "transaction"])
def test_studio_checkpoint_consumers_reject_non_regular_authority(
        tmp_path: Path, kind: str, consumer: str) -> None:
    bundle = _bundle(tmp_path / "bundle")
    studio = Studio.for_bundle(bundle.root)
    studio.session_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = studio.directives_checkpoint_path
    sentinel = tmp_path / "external-checkpoint.json"
    sentinel_bytes = json.dumps({
        "version": 1,
        "through_rotation": 0,
        "rows": [_row("EXTERNAL", "tables/orders")],
    }).encode()
    sentinel.write_bytes(sentinel_bytes)
    if kind == "symlink":
        checkpoint.symlink_to(sentinel)
    else:
        checkpoint.mkdir()

    with pytest.raises((ValueError, CommentMutationError, OSError)):
        if consumer == "list":
            studio.list_comments()
        elif consumer == "get":
            studio.get_comment("EXTERNAL")
        elif consumer == "mutate":
            studio.post_comment(concept="tables/orders", body="must fail closed")
        else:
                with DirectiveStore(studio.session_dir).writer() as transaction:
                    studio._directive_transaction_rows_locked(
                        transaction, "comment_create_transaction_id", "01KZZZZZZZ")
    assert sentinel.read_bytes() == sentinel_bytes


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_comment_mutation_rejects_non_regular_directive_lock_leaf(
        tmp_path: Path, kind: str) -> None:
    bundle = _bundle(tmp_path / "bundle")
    studio = Studio.for_bundle(bundle.root)
    studio.session_dir.mkdir(parents=True, exist_ok=True)
    lock = studio.session_dir / "..directives-transaction.okf-lock"
    sentinel = tmp_path / "external-lock-target"
    sentinel_bytes = b"LOCK-SENTINEL-MUST-NOT-CHANGE"
    sentinel.write_bytes(sentinel_bytes)
    if kind == "symlink":
        lock.symlink_to(sentinel)
    else:
        lock.mkdir()

    with pytest.raises(OSError):
        studio.post_comment(concept="tables/orders", body="must not append")
    assert sentinel.read_bytes() == sentinel_bytes
    assert not studio.directives_path.exists()


def test_summary_read_does_not_create_lock_or_paths(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    _write_jsonl(session / "seed.jsonl", [])
    lock = session / "..directives-transaction.okf-lock"
    lock.unlink()
    before = {path.name for path in session.iterdir()}
    with pytest.raises(CommentSummaryUnavailable):
        read_comment_summary(session, graph_concepts={"tables/orders"})
    assert {path.name for path in session.iterdir()} == before


def test_summary_rejects_symlinked_transaction_path(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _seed_authority_lock(session)
    (session / "directives.jsonl").write_text("", encoding="utf-8")
    outside_transaction = tmp_path / "outside-transaction"
    outside_transaction.write_text("", encoding="utf-8")
    (session / ".directives-transaction").symlink_to(outside_transaction)

    with pytest.raises(CommentSummaryUnavailable):
        read_comment_summary(session, graph_concepts={"tables/orders"})


def test_summary_reader_mutually_excludes_session_writer_lock(tmp_path: Path) -> None:
    import fcntl

    session = tmp_path / "session"
    _seed_authority_lock(session)
    (session / "directives.jsonl").write_text("", encoding="utf-8")
    lock = session / "..directives-transaction.okf-lock"
    fd = os.open(lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    started = threading.Event()
    finished = threading.Event()
    result: list[dict[str, object]] = []

    def read_locked_summary() -> None:
        started.set()
        result.append(read_comment_summary(session, graph_concepts={"tables/orders"}))
        finished.set()

    reader = threading.Thread(target=read_locked_summary)
    reader.start()
    try:
        assert started.wait(timeout=5)
        time.sleep(0.05)
        assert not finished.is_set()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    reader.join(timeout=5)
    assert not reader.is_alive()
    assert result[0]["counts"] == {}


def test_summary_reader_and_writer_share_the_same_lock_inode(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    transaction = session / ".directives-transaction"
    lock = session / "..directives-transaction.okf-lock"
    started = threading.Event()
    finished = threading.Event()
    result: list[dict[str, object]] = []

    with path_lock(transaction):
        writer_identity = (lock.stat().st_dev, lock.stat().st_ino)

        def read_locked_summary() -> None:
            started.set()
            result.append(read_comment_summary(session, graph_concepts={"tables/orders"}))
            finished.set()

        reader = threading.Thread(target=read_locked_summary)
        reader.start()
        assert started.wait(timeout=5)
        time.sleep(0.05)
        assert not finished.is_set()
        assert (lock.stat().st_dev, lock.stat().st_ino) == writer_identity

    reader.join(timeout=5)
    assert not reader.is_alive()
    assert result[0]["counts"] == {}
    assert (lock.stat().st_dev, lock.stat().st_ino) == writer_identity


def test_graph_data_comment_counts_are_exact_and_absent_by_default(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    counts = {"tables/orders": 2}
    summary = {
        "schema": 1,
        "semantics": "active-comment-row",
        "counts": counts,
        "summary_revision": canonical_comment_summary_revision(counts),
    }

    default_nodes = {node["data"]["id"]: node["data"] for node in build_graph_data(bundle)["nodes"]}
    opted_nodes = {
        node["data"]["id"]: node["data"]
        for node in build_graph_data(bundle, comment_summary=summary)["nodes"]
    }
    assert all("active_comment_count" not in node for node in default_nodes.values())
    assert opted_nodes["tables/orders"]["active_comment_count"] == 2
    assert opted_nodes["tables/customers"]["active_comment_count"] == 0
    assert "comment_summary" not in build_graph_data(bundle, comment_summary=summary)

    malformed = dict(summary)
    malformed["counts"] = {"not/a/graph-node": 1}
    with pytest.raises(ValueError, match="CommentSummaryV1"):
        build_graph_data(bundle, comment_summary=malformed)

    mismatched = dict(summary)
    mismatched["summary_revision"] = "a" * 64
    with pytest.raises(ValueError, match="revision"):
        build_graph_data(bundle, comment_summary=mismatched)


def test_final_graph_outputs_are_private_and_count_identical_across_surfaces(
        tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    session = bundle.root / ".okf-loom" / "session"
    private_row = _row(
        "COMMENT-ID-SENTINEL-7F3A",
        "tables/orders",
        body="BODY-SENTINEL-7F3A",
        anchor={"quote": "ANCHOR-SENTINEL-7F3A"},
        row_token="ROW-TOKEN-SENTINEL-7F3A",
        parent_id="PARENT-ID-SENTINEL-7F3A",
        actor="ACTOR-SENTINEL-7F3A",
        session={"secret": "SESSION-SENTINEL-7F3A"},
        created_at="CREATED-AT-SENTINEL-7F3A",
        updated_at="UPDATED-AT-SENTINEL-7F3A",
        claimed_at="CLAIMED-AT-SENTINEL-7F3A",
        resolved_at="RESOLVED-AT-SENTINEL-7F3A",
        lifecycle_detail="LIFECYCLE-DETAIL-SENTINEL-7F3A",
        state_detail="STATE-DETAIL-SENTINEL-7F3A",
        seq=739_391,
        rev="REV-SENTINEL-7F3A",
        event_cursor="EVENT-CURSOR-SENTINEL-7F3A",
        unknown_private_extension="UNKNOWN-EXTENSION-SENTINEL-7F3A",
        another_unknown_extension={"nested": "NESTED-EXTENSION-SENTINEL-7F3A"},
    )
    _write_jsonl(session / "directives.jsonl", [private_row])

    private_keys = set(private_row) - {"id", "concept", "state", "archived"}
    private_keys.update({"quote", "secret", "nested"})
    private_sentinels = (
        "COMMENT-ID-SENTINEL-7F3A",
        "BODY-SENTINEL-7F3A",
        "ANCHOR-SENTINEL-7F3A",
        "ROW-TOKEN-SENTINEL-7F3A",
        "PARENT-ID-SENTINEL-7F3A",
        "ACTOR-SENTINEL-7F3A",
        "SESSION-SENTINEL-7F3A",
        "CREATED-AT-SENTINEL-7F3A",
        "UPDATED-AT-SENTINEL-7F3A",
        "CLAIMED-AT-SENTINEL-7F3A",
        "RESOLVED-AT-SENTINEL-7F3A",
        "LIFECYCLE-DETAIL-SENTINEL-7F3A",
        "STATE-DETAIL-SENTINEL-7F3A",
        "739391",
        "REV-SENTINEL-7F3A",
        "EVENT-CURSOR-SENTINEL-7F3A",
        "UNKNOWN-EXTENSION-SENTINEL-7F3A",
        "NESTED-EXTENSION-SENTINEL-7F3A",
    )

    def graph_values(payload: dict[str, object]) -> dict[str, int]:
        nodes = payload["nodes"]
        assert isinstance(nodes, list)
        return {
            node["data"]["id"]: node["data"]["active_comment_count"]
            for node in nodes
        }

    def all_keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value).union(*(all_keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(all_keys(item) for item in value))
        return set()

    def assert_sanitized(payload: dict[str, object], artifact: str) -> None:
        serialized_graph = {
            "nodes": payload["nodes"],
            "edges": payload["edges"],
        }
        assert private_keys.isdisjoint(all_keys(serialized_graph))
        assert all(sentinel not in artifact for sentinel in private_sentinels)
        assert graph_values(payload) == {
            "tables/customers": 0,
            "tables/orders": 1,
        }

    default_out = tmp_path / "default"
    build_site(bundle, default_out, target="static")
    default_graph = json.loads((default_out / "__data" / "graph.json").read_text(encoding="utf-8"))
    assert all("active_comment_count" not in node["data"] for node in default_graph["nodes"])

    default_single_path = tmp_path / "default-single.html"
    render_single_file(bundle, default_single_path)
    default_single_text = default_single_path.read_text(encoding="utf-8")
    default_single_match = re.search(r"window\.BUNDLE = (.*);\n", default_single_text)
    assert default_single_match is not None
    default_single_graph = json.loads(default_single_match.group(1))
    assert all(
        "active_comment_count" not in node["data"]
        for node in default_single_graph["nodes"]
    )

    static_out = tmp_path / "static"
    build_site(bundle, static_out, target="static", include_comment_summary=True)
    static_text = (static_out / "__data" / "graph.json").read_text(encoding="utf-8")
    static_graph = json.loads(static_text)

    single_path = tmp_path / "single.html"
    render_single_file(bundle, single_path, include_comment_summary=True)
    single_text = single_path.read_text(encoding="utf-8")
    match = re.search(r"window\.BUNDLE = (.*);\n", single_text)
    assert match is not None
    single_graph = json.loads(match.group(1))

    studio = Studio.for_bundle(bundle.root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), OKFWikiHandler)
    server.bundle = bundle  # type: ignore[attr-defined]
    server.config = {}  # type: ignore[attr-defined]
    server.name = bundle.name  # type: ignore[attr-defined]
    server.plugin = None  # type: ignore[attr-defined]
    server.studio = studio  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/__data/graph.json"
        with urllib.request.urlopen(url, timeout=5) as response:
            live_text = response.read().decode("utf-8")
            live_graph = json.loads(live_text)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert_sanitized(live_graph, live_text)
    assert_sanitized(static_graph, static_text)
    assert_sanitized(single_graph, single_text)
    assert graph_values(live_graph) == graph_values(static_graph) == graph_values(single_graph)


def test_live_graph_uses_existing_studio_and_fails_closed_on_corruption(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    studio = Studio.for_bundle(bundle.root)
    studio.post_comment(concept="tables/orders", body="HTTP private body")
    server = ThreadingHTTPServer(("127.0.0.1", 0), OKFWikiHandler)
    server.bundle = bundle  # type: ignore[attr-defined]
    server.config = {}  # type: ignore[attr-defined]
    server.name = bundle.name  # type: ignore[attr-defined]
    server.plugin = None  # type: ignore[attr-defined]
    server.studio = studio  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/__data/graph.json"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.loads(response.read())
            assert response.headers["Cache-Control"] == "no-cache"
            assert re.fullmatch(r'"[0-9a-f]{64}"', response.headers["ETag"])
        nodes = {node["data"]["id"]: node["data"] for node in payload["nodes"]}
        assert nodes["tables/orders"]["active_comment_count"] == 1
        assert "HTTP private body" not in json.dumps(payload)

        (studio.directives_checkpoint_path).write_text("{bad", encoding="utf-8")
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(url, timeout=5)
        assert raised.value.code == 503
        assert raised.value.headers["Cache-Control"] == "no-store"
        assert json.loads(raised.value.read())["error"] == "comment_summary_unavailable"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_fresh_studio_graph_has_empty_authoritative_comment_summary(tmp_path: Path) -> None:
    """A newly started studio must not report its graph as unavailable.

    The read-only summary path intentionally does not create authority.  Server
    startup therefore owns creating the empty directive authority before the
    browser's first ``/__data/graph.json`` request.
    """
    bundle = _bundle(tmp_path / "bundle")
    studio = Studio.for_bundle(bundle.root)
    studio.ensure_session()

    summary = studio.comment_summary(
        graph_concepts={"tables/orders", "tables/customers"},
    )

    assert summary["counts"] == {}
    assert (studio.session_dir / "..directives-transaction.okf-lock").is_file()


def test_renderer_summary_and_events_read_only_external_session_authority(
    monkeypatch, tmp_path: Path,
) -> None:
    from okf_loom.render import (_read_bundle_comment_summary,
                                 _read_bundle_engagement_events)
    bundle = _bundle(tmp_path / "bundle")
    local = Studio.for_bundle(bundle.root)
    local.post_comment(concept="tables/customers", body="LOCAL_AUTHORITY_MARKER")
    local.append_event({"type": "changed", "concept": "tables/customers",
                        "ts": "2026-08-12T00:00:00Z"})

    external_root = tmp_path / "external"; external_root.mkdir(mode=0o700)
    tmp_path.chmod(0o700)
    monkeypatch.setenv("OKF_LOOM_SESSION_TRUSTED_ROOT", str(tmp_path))
    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(external_root))
    external = Studio.for_configured_bundle(bundle.root)
    external.post_comment(concept="tables/orders", body="EXTERNAL_AUTHORITY_MARKER")
    external.append_event({"type": "changed", "concept": "tables/orders",
                           "ts": "2026-08-12T00:00:01Z"})
    local_before = {path.relative_to(local.session_dir): path.read_bytes()
                    for path in local.session_dir.rglob("*") if path.is_file()}

    summary = _read_bundle_comment_summary(bundle)
    events = _read_bundle_engagement_events(bundle)

    assert summary["counts"] == {"tables/orders": 1}
    assert events is not None and events[0]["concept"] == "tables/orders"
    rendered_authority = json.dumps({"summary": summary, "events": events})
    assert "EXTERNAL_AUTHORITY_MARKER" in rendered_authority
    assert "LOCAL_AUTHORITY_MARKER" not in rendered_authority
    assert local_before == {path.relative_to(local.session_dir): path.read_bytes()
                            for path in local.session_dir.rglob("*") if path.is_file()}


def test_live_graph_allowlist_and_serialization_use_one_bundle_snapshot(tmp_path: Path) -> None:
    original = _bundle(tmp_path / "original")
    replacement_root = tmp_path / "replacement"
    (replacement_root / "services").mkdir(parents=True)
    (replacement_root / "services" / "billing.md").write_text(
        "---\ntype: Service\ntitle: Billing\n---\nBilling.\n", encoding="utf-8"
    )
    replacement = Bundle.load(replacement_root)
    studio = Studio.for_bundle(original.root)
    studio.post_comment(concept="tables/orders", body="private")
    server = ThreadingHTTPServer(("127.0.0.1", 0), OKFWikiHandler)
    server.bundle = original  # type: ignore[attr-defined]
    server.config = {}  # type: ignore[attr-defined]
    server.name = original.name  # type: ignore[attr-defined]
    server.plugin = None  # type: ignore[attr-defined]
    server.studio = studio  # type: ignore[attr-defined]
    real_summary = studio.comment_summary

    def interleaving_summary(*, graph_concepts: set[str] | frozenset[str]):
        assert graph_concepts == {"tables/orders", "tables/customers"}
        server.bundle = replacement  # deterministic reload after snapshot
        return real_summary(graph_concepts=graph_concepts)

    studio.comment_summary = interleaving_summary  # type: ignore[method-assign]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/__data/graph.json"
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.loads(response.read())
        ids = {node["data"]["id"] for node in payload["nodes"]}
        assert ids == {"tables/orders", "tables/customers"}
        orders = next(node["data"] for node in payload["nodes"] if node["data"]["id"] == "tables/orders")
        assert orders["active_comment_count"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cli_static_summary_flag_is_explicit_and_spa_rejects_it(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    _write_jsonl(
        bundle.root / ".okf-loom" / "session" / "directives.jsonl",
        [_row("CLI-ID", "tables/orders")],
    )
    output = tmp_path / "site"
    assert main([
        "build", str(bundle.root), "--target", "static", "--out", str(output),
        "--include-comment-summary",
    ]) == 0
    payload = json.loads((output / "__data" / "graph.json").read_text(encoding="utf-8"))
    orders = next(node["data"] for node in payload["nodes"] if node["data"]["id"] == "tables/orders")
    assert orders["active_comment_count"] == 1

    assert main([
        "build", str(bundle.root), "--target", "spa", "--out", str(tmp_path / "spa"),
        "--include-comment-summary",
    ]) == 1


def test_static_index_engagement_snapshot_keeps_comments_opt_in_and_recent_default(
        tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    session = bundle.root / ".okf-loom" / "session"
    _write_jsonl(session / "directives.jsonl", [
        _row("OPEN", "tables/orders"),
        _row("REPLY", "tables/orders", parent_id="OPEN"),
        _row("RESOLVED", "tables/orders", state="resolved"),
        _row("ARCHIVED", "tables/orders", archived=True),
    ])
    _write_jsonl(session / "events.jsonl", [
        {"id": "e1", "event_id": "event-e1", "type": "activity", "concept": "tables/orders",
         "actor": "agent", "action": "updated <script>alert(1)</script>", "ts": "2026-07-17T10:00:00Z"},
        {"id": "e2", "type": "activity", "concept_id": "missing/concept",
         "actor": "agent", "action": "updated", "ts": "2026-07-17T11:00:00Z"},
        {"id": "e3", "type": "removed", "ids": ["tables/customers"],
         "origin": "disk", "ts": "2026-07-17T12:00:00Z"},
    ])
    (session / "..sidecar-transaction.okf-lock").touch(mode=0o600)

    default_out = tmp_path / "default"
    build_site(bundle, default_out, target="static")
    default_html = (default_out / "index.html").read_text(encoding="utf-8")
    assert "okf-card__comments" not in default_html
    assert "okf-recent-rail" in default_html
    assert "agent updated &lt;script&gt;alert(1)&lt;/script&gt;" in default_html

    out = tmp_path / "site"
    build_site(bundle, out, target="static", include_comment_summary=True)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert html.count('class="okf-card__comments okf-comment-count"') >= 1
    assert "2 active comments on Orders" in html
    assert "0 active comments on Customers" not in html
    assert '<ol class="okf-recent__list">' in html
    assert "agent updated &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert 'href="tables/orders.html"' in html
    assert "missing/concept" not in html
    assert "Customers — disk removed" not in html
    assert "&quot;tables/orders&quot;:&quot;tables/orders.html&quot;" in html
    sub_html = (out / "tables" / "index.html").read_text(encoding="utf-8")
    assert "&quot;tables/orders&quot;:&quot;orders.html&quot;" in sub_html

    single = tmp_path / "single.html"
    render_single_file(bundle, single, include_comment_summary=True)
    single_text = single.read_text(encoding="utf-8")
    assert '"recent_changes": [{"id": "tables/orders"' in single_text


def test_portable_engagement_snapshot_is_strictly_read_only_with_pending_receipt(
        tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    session = bundle.root / ".okf-loom" / "session"
    _write_jsonl(session / "directives.jsonl", [_row("OPEN", "tables/orders")])
    _write_jsonl(session / "events.jsonl", [{
        "id": "legacy", "seq": 7, "event_cursor": 9, "type": "changed",
        "ids": ["tables/orders"], "ts": "2026-07-17T10:00:00Z",
    }])
    (session / "..sidecar-transaction.okf-lock").touch(mode=0o600)
    (session / "pending-receipt.json").write_bytes(b'{"pending":true}\n')

    def snapshot() -> dict[str, bytes]:
        return {str(path.relative_to(session)): path.read_bytes()
                for path in session.rglob("*") if path.is_file()}

    before = snapshot()
    build_site(bundle, tmp_path / "site", target="static", include_comment_summary=True)
    render_single_file(bundle, tmp_path / "single.html", include_comment_summary=True)
    assert snapshot() == before


def test_read_event_snapshot_authority_edge_cases(tmp_path: Path) -> None:
    session = tmp_path / "session"; session.mkdir()
    assert read_event_snapshot(session) is None
    (session / "..sidecar-transaction.okf-lock").touch(mode=0o600)
    (session / "events-20260717-000000000001.jsonl").write_text(
        '{"event_id":"rotated","type":"changed","ids":["x"],"ts":"2026-07-17T00:00:00Z"}\n',
        encoding="utf-8",
    )
    assert read_event_snapshot(session)[0]["event_id"] == "rotated"
    (session / "events.jsonl").write_bytes(b"")
    assert read_event_snapshot(session)[0]["event_id"] == "rotated"
    (session / "events.jsonl").write_bytes(b'{"event_id":"torn"')
    with pytest.raises(CommentSummaryUnavailable):
        read_event_snapshot(session)


def test_read_event_snapshot_existing_authority_requires_valid_lock(tmp_path: Path) -> None:
    session = tmp_path / "session"; session.mkdir()
    (session / "events.jsonl").write_bytes(b"")
    with pytest.raises(CommentSummaryUnavailable):
        read_event_snapshot(session)
    lock = session / "..sidecar-transaction.okf-lock"
    lock.mkdir()
    with pytest.raises(CommentSummaryUnavailable):
        read_event_snapshot(session)
    lock.rmdir()
    outside = tmp_path / "outside.lock"; outside.touch()
    lock.symlink_to(outside)
    with pytest.raises(CommentSummaryUnavailable):
        read_event_snapshot(session)
    (session / "events.jsonl").unlink()
    outside = tmp_path / "outside.jsonl"; outside.write_text("{}\n", encoding="utf-8")
    (session / "events.jsonl").symlink_to(outside)
    with pytest.raises(CommentSummaryUnavailable):
        read_event_snapshot(session)


@pytest.mark.parametrize("authority", ["missing-lock", "torn", "symlink"])
def test_portable_recent_unavailable_authority_omits_without_mutation(
        tmp_path: Path, authority: str) -> None:
    bundle = _bundle(tmp_path / "bundle")
    session = bundle.root / ".okf-loom" / "session"
    session.mkdir(parents=True, exist_ok=True)
    events = session / "events.jsonl"
    if authority == "torn":
        events.write_bytes(b'{"event_id":"torn"')
        (session / "..sidecar-transaction.okf-lock").touch()
    elif authority == "symlink":
        outside = tmp_path / "outside-events"; outside.write_text("{}\n", encoding="utf-8")
        events.symlink_to(outside)
        (session / "..sidecar-transaction.okf-lock").touch()
    else:
        events.write_text("{}\n", encoding="utf-8")
    before = {str(p.relative_to(session)): (p.is_symlink(), p.readlink() if p.is_symlink() else p.read_bytes())
              for p in session.rglob("*") if p.is_file() or p.is_symlink()}
    out = tmp_path / "site"
    build_site(bundle, out, target="static")
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "okf-recent-rail" not in html
    after = {str(p.relative_to(session)): (p.is_symlink(), p.readlink() if p.is_symlink() else p.read_bytes())
             for p in session.rglob("*") if p.is_file() or p.is_symlink()}
    assert after == before
