"""Tests for authoring mutator CLI verbs (current spec §7) + entity/relation search."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from okf_loom.cli import main
from okf_loom.model import Bundle
from okf_loom.search import search_bundle, SearchMode
from okf_loom.update import load_plan, apply_plan, UpdateOp
from okf_loom.paths import concept_id_from_str


DEMO = "samples/demo_bundle"


def _capture(argv: list[str]) -> tuple[int, str, str]:
    """Run CLI, capture stdout/stderr."""
    import io, contextlib
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def demo_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "demo"
    shutil.copytree(DEMO, dst)
    return dst


# --- write-concept (§8.1) ---------------------------------------------------


def test_write_concept_creates_new(demo_copy: Path) -> None:
    rc, out, _ = _capture([
        "write-concept", "--bundle", str(demo_copy),
        "--id", "tables/new", "--type", "Table",
        "--title", "New Table", "--description", "A test",
        "--body", "Hello world",
    ])
    assert rc == 0
    assert (demo_copy / "tables" / "new.md").exists()


def test_write_concept_creates_parent_dirs(demo_copy: Path) -> None:
    rc, _, _ = _capture([
        "write-concept", "--bundle", str(demo_copy),
        "--id", "deep/nested/concept", "--type", "Note",
        "--body", "Deep",
    ])
    assert rc == 0
    assert (demo_copy / "deep" / "nested" / "concept.md").exists()


def test_write_concept_update_no_clobber(demo_copy: Path) -> None:
    """Without --force, refuses to overwrite non-empty body."""
    rc, _, err = _capture([
        "write-concept", "--bundle", str(demo_copy),
        "--id", "tables/orders", "--type", "Table",
        "--body", "New body",
    ])
    assert rc == 1
    assert "force" in err.lower()


def test_write_concept_update_with_force(demo_copy: Path) -> None:
    rc, _, _ = _capture([
        "write-concept", "--bundle", str(demo_copy),
        "--id", "tables/orders", "--type", "Table",
        "--body", "Replaced body", "--force",
    ])
    assert rc == 0


def test_write_concept_tags_accumulate(demo_copy: Path) -> None:
    rc, _, _ = _capture([
        "write-concept", "--bundle", str(demo_copy),
        "--id", "tables/tagged", "--type", "Table",
        "--tag", "alpha", "--tag", "beta",
        "--body", "Tagged",
    ])
    assert rc == 0
    b = Bundle.load(demo_copy)
    assert ("tables", "tagged") in b.concepts
    assert "alpha" in b.concepts[("tables", "tagged")].tags
    assert "beta" in b.concepts[("tables", "tagged")].tags


# --- set-frontmatter (§8.2) -------------------------------------------------


def test_set_frontmatter_cli(demo_copy: Path) -> None:
    rc, _, _ = _capture([
        "set-frontmatter", "--bundle", str(demo_copy),
        "--id", "tables/orders", "--key", "description",
        "--value", "CLI-set description",
    ])
    assert rc == 0


def test_set_frontmatter_json_value(demo_copy: Path) -> None:
    rc, _, _ = _capture([
        "set-frontmatter", "--bundle", str(demo_copy),
        "--id", "tables/orders", "--key", "custom_list",
        "--value", '["a", "b"]', "--json-value",
    ])
    assert rc == 0
    b = Bundle.load(demo_copy)
    assert b.concepts[("tables", "orders")].frontmatter.get("custom_list") == ["a", "b"]


# --- link-add (§8.3) --------------------------------------------------------


def test_link_add_creates_link(demo_copy: Path) -> None:
    rc, _, _ = _capture([
        "link-add", "--bundle", str(demo_copy),
        "--source", "tables/customers", "--target", "datasets/orders",
        "--label", "purchases from",
    ])
    assert rc == 0
    b = Bundle.load(demo_copy)
    links = b.concepts[("tables", "customers")].links(bundle_root=b.root)
    targets = [str(l.target) for l in links]
    assert any("orders" in t for t in targets)


def test_link_add_with_relation(demo_copy: Path) -> None:
    rc, _, _ = _capture([
        "link-add", "--bundle", str(demo_copy),
        "--source", "tables/customers", "--target", "datasets/orders",
        "--relation", "purchases_from",
    ])
    assert rc == 0


def test_link_add_idempotent(demo_copy: Path) -> None:
    """Running link-add twice: the second call is a true no-op.

    Previously ``assert "already_linked" in out or rc == 0`` passed for any
    rc==0 (which a no-op second call still returns), masking a non-idempotent
    re-add that appended a *duplicate* link to the body. Now we prove
    idempotency by hashing the file bytes and the resolved link set after each
    call and asserting they are byte-identical — a duplicate append would
    change both, failing the test.
    """
    args = [
        "link-add", "--bundle", str(demo_copy),
        "--source", "tables/customers", "--target", "datasets/orders",
        "--label", "test",
    ]
    src_path = demo_copy / "tables" / "customers.md"

    rc1, out1, err1 = _capture(args)
    assert rc1 == 0, f"first link-add failed: {err1}"

    content_after_first = src_path.read_bytes()
    b1 = Bundle.load(demo_copy)
    links_after_first = sorted(
        str(link.target)
        for link in b1.concepts[("tables", "customers")].links(bundle_root=b1.root)
        if link.target is not None
    )

    # Second call MUST be a no-op.
    rc2, out2, err2 = _capture(args)
    assert rc2 == 0, f"second link-add failed: {err2}"

    # Byte-identical file content proves no duplicate link was appended.
    content_after_second = src_path.read_bytes()
    assert content_after_second == content_after_first, (
        "non-idempotent link-add mutated the file on the second call "
        "(a duplicate link was appended)"
    )
    # The resolved link set is unchanged after the second call.
    b2 = Bundle.load(demo_copy)
    links_after_second = sorted(
        str(link.target)
        for link in b2.concepts[("tables", "customers")].links(bundle_root=b2.root)
        if link.target is not None
    )
    assert links_after_second == links_after_first, (
        "link set changed after the (supposedly idempotent) second call"
    )
    # And the second call explicitly reported the skip.
    assert "already_linked" in out2, (
        f"second link-add did not report already_linked: {out2!r}"
    )


# --- link-add --allow-forward-reference (P2-6 / iter-3) ---------------------
# SPEC §3 / AGENTS.md hard rule #3: a link whose target does not exist is
# "not-yet-written knowledge", not malformed. link-add defaults to fail-closed
# (exit 1) but opts in to a forward reference via --allow-forward-reference.


def test_link_add_forward_reference_default_fail_closed(demo_copy: Path) -> None:
    """Without --allow-forward-reference, a missing target exits 1
    (target_concept_not_found). The fail-closed default is the safe path."""
    rc, out, err = _capture([
        "link-add", "--bundle", str(demo_copy),
        "--source", "tables/customers", "--target", "tables/notyet",
    ])
    assert rc == 1, f"expected exit 1 for missing target, got {rc}: {out}"
    assert "target_concept_not_found" in out


def test_link_add_forward_reference_with_flag_succeeds(demo_copy: Path) -> None:
    """--allow-forward-reference writes a body link to a not-yet-written
    target (exit 0). The link target path is the SPEC §5.1 absolute form."""
    rc, out, err = _capture([
        "link-add", "--bundle", str(demo_copy),
        "--source", "tables/customers", "--target", "tables/notyet",
        "--allow-forward-reference",
    ])
    assert rc == 0, f"forward-ref link-add failed: {out}{err}"
    # The body link must point at the not-yet-written target via its absolute
    # bundle-relative path (/tables/notyet.md, SPEC §5.1).
    src = demo_copy / "tables" / "customers.md"
    body = src.read_text(encoding="utf-8")
    assert "/tables/notyet.md" in body, (
        f"forward-ref link not written to source body:\n{body}"
    )


def test_link_add_forward_reference_idempotent(demo_copy: Path) -> None:
    """A second --allow-forward-reference call to the same missing target is
    a no-op (already_linked, exit 0) — no duplicate body link is appended."""
    args = [
        "link-add", "--bundle", str(demo_copy),
        "--source", "tables/customers", "--target", "tables/notyet",
        "--allow-forward-reference",
    ]
    src_path = demo_copy / "tables" / "customers.md"
    rc1, out1, err1 = _capture(args)
    assert rc1 == 0, f"first forward-ref link-add failed: {err1}"
    after_first = src_path.read_bytes()

    rc2, out2, err2 = _capture(args)
    assert rc2 == 0, f"second forward-ref link-add failed: {err2}"
    after_second = src_path.read_bytes()
    assert after_second == after_first, (
        "second forward-ref link-add appended a duplicate link (not idempotent)"
    )
    assert "already_linked" in out2, (
        f"second forward-ref link-add did not report already_linked: {out2!r}"
    )


# --- entity-add (§8.4) ------------------------------------------------------


def test_entity_add_creates_entity(demo_copy: Path) -> None:
    rc, _, _ = _capture([
        "entity-add", "--bundle", str(demo_copy),
        "--id", "tables/orders", "--label", "Purchase Order",
        "--kind", "business_entity", "--alias", "PO",
    ])
    assert rc == 0
    b = Bundle.load(demo_copy)
    entities = b.concepts[("tables", "orders")].frontmatter.get("entities", [])
    # Should have the new entity (may coexist with existing ones)
    labels = [e.get("label") if isinstance(e, dict) else e for e in entities]
    assert "Purchase Order" in labels


def test_entity_add_idempotent(demo_copy: Path) -> None:
    """Adding the same entity twice: the second call is a true no-op.

    Previously ``assert "entity_exists" in out or rc == 0`` passed for any
    rc==0, masking a non-idempotent re-add that appended a *duplicate* entity.
    Now we hash the file bytes and capture the entities list after each call
    and assert they are byte-identical — a duplicate append would change both.
    """
    args = [
        "entity-add", "--bundle", str(demo_copy),
        "--id", "tables/orders", "--label", "UniqueEntity",
    ]
    tgt_path = demo_copy / "tables" / "orders.md"

    rc1, out1, err1 = _capture(args)
    assert rc1 == 0, f"first entity-add failed: {err1}"

    content_after_first = tgt_path.read_bytes()
    b1 = Bundle.load(demo_copy)
    entities_after_first = list(
        b1.concepts[("tables", "orders")].frontmatter.get("entities", [])
    )

    # Second call MUST be a no-op.
    rc2, out2, err2 = _capture(args)
    assert rc2 == 0, f"second entity-add failed: {err2}"

    # Byte-identical file content proves no duplicate entity was appended.
    content_after_second = tgt_path.read_bytes()
    assert content_after_second == content_after_first, (
        "non-idempotent entity-add mutated the file on the second call "
        "(a duplicate entity was appended)"
    )
    # The entities list is unchanged after the second call.
    b2 = Bundle.load(demo_copy)
    entities_after_second = list(
        b2.concepts[("tables", "orders")].frontmatter.get("entities", [])
    )
    assert entities_after_second == entities_after_first, (
        "entities list changed after the (supposedly idempotent) second call"
    )
    # And the second call explicitly reported the skip.
    assert "entity_exists" in out2, (
        f"second entity-add did not report entity_exists: {out2!r}"
    )


# --- add_entity handler (unit test) -----------------------------------------


def test_add_entity_handler_directly(tmp_path: Path) -> None:
    """Test the add_entity handler directly via apply_plan."""
    import json
    # Create a minimal bundle
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "index.md").write_text("---\nokf_version: '0.1'\n---\n# Test\n")
    (bundle_dir / "concept.md").write_text("---\ntype: Table\n---\nBody\n")

    bundle = Bundle.load(bundle_dir)
    plan_data = {
        "bundle_root": str(bundle_dir),
        "ops": [{
            "kind": "add_entity",
            "target": "concept",
            "args": {"label": "TestEntity", "kind": "test"},
        }],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan_data))

    loaded = load_plan(plan_path)
    result = apply_plan(bundle, loaded, apply=True)
    assert result["applied"] == 1


# --- Entity search mode (§4.3) ----------------------------------------------


def test_entity_search_finds_by_title(demo_copy: Path) -> None:
    b = Bundle.load(demo_copy)
    results = search_bundle(b, "orders", mode=SearchMode.ENTITY, limit=10)
    assert len(results) > 0
    assert all(r.source_backend == "entity" for r in results)


def test_entity_search_finds_by_alias(demo_copy: Path) -> None:
    """Entity search matches aliases."""
    b = Bundle.load(demo_copy)
    # Add an alias to a concept
    b.concepts[("tables", "orders")].frontmatter["aliases"] = ["purchase records"]
    b.invalidate()
    results = search_bundle(b, "purchase records", mode=SearchMode.ENTITY, limit=10)
    assert len(results) > 0
    assert any("orders" in str(r.concept_id) for r in results)


def test_entity_search_empty_query(demo_copy: Path) -> None:
    b = Bundle.load(demo_copy)
    results = search_bundle(b, "", mode=SearchMode.ENTITY, limit=10)
    assert len(results) == 0


# --- Relation search mode (§4.3) --------------------------------------------


def test_relation_search_by_type(demo_copy: Path) -> None:
    b = Bundle.load(demo_copy)
    results = search_bundle(b, "", mode=SearchMode.RELATION, relation="written_by", limit=10)
    assert len(results) > 0
    # tables/orders has written_by relations
    ids = [str(r.concept_id) for r in results]
    assert any("orders" in i for i in ids)


def test_relation_search_by_source(demo_copy: Path) -> None:
    b = Bundle.load(demo_copy)
    results = search_bundle(
        b, "", mode=SearchMode.RELATION,
        source="tables/orders", limit=10,
    )
    assert len(results) > 0


def test_relation_search_no_match(demo_copy: Path) -> None:
    b = Bundle.load(demo_copy)
    results = search_bundle(
        b, "", mode=SearchMode.RELATION,
        relation="nonexistent_relation", limit=10,
    )
    assert len(results) == 0


# --- repair (§9.1) ----------------------------------------------------------


def test_repair_dry_run(demo_copy: Path) -> None:
    rc, out, _ = _capture(["repair", str(demo_copy), "--all"])
    assert rc == 0
    assert "mechanical fix" in out.lower()


def test_repair_apply(demo_copy: Path) -> None:
    rc, out, _ = _capture(["repair", str(demo_copy), "--all", "--apply"])
    assert rc == 0


# --- P0-1: write-concept doubled-path bug (relative --bundle) ---------------


def test_write_concept_relative_bundle_no_doubled_path(tmp_path: Path, monkeypatch) -> None:
    """P0-1 regression: a RELATIVE --bundle must not double the path.

    Before the fix, ``concept_path = bundle_root / concept_id_to_path(...)``
    re-anchored an already-anchored path, producing
    ``<bundle>/<bundle>/<id>.md`` whenever bundle_root was relative.
    """
    # Stage a real bundle under tmp_path, then chdir there and pass a RELATIVE
    # bundle name to the CLI — this is the failing real-world invocation.
    bundle_dir = tmp_path / "mybundle"
    shutil.copytree(DEMO, bundle_dir)
    monkeypatch.chdir(tmp_path)

    rc, out, err = _capture([
        "write-concept", "--bundle", "mybundle",
        "--id", "tables/relative_test", "--type", "Table",
        "--title", "Relative Test", "--body", "hello",
    ])
    assert rc == 0, f"unexpected rc={rc} err={err}"
    # The file MUST land at mybundle/tables/relative_test.md, NOT
    # mybundle/mybundle/tables/relative_test.md.
    assert (bundle_dir / "tables" / "relative_test.md").exists()
    assert not (bundle_dir / "mybundle" / "tables" / "relative_test.md").exists()

    # JSON "created"/"updated" path field must be correct too (the bug also
    # corrupted the reported path).
    rc2, out2, _ = _capture([
        "write-concept", "--bundle", "mybundle",
        "--id", "tables/relative_test", "--type", "Table",
        "--body", "updated", "--force",
        "--format", "json",
    ])
    assert rc2 == 0
    import json
    data = json.loads(out2)
    reported_path = Path(data["updated"])
    # The reported path must resolve to the SAME file we just verified exists.
    assert reported_path.name == "relative_test.md"
    assert reported_path.parent.name == "tables"


def test_write_concept_relative_bundle_subdir(tmp_path: Path, monkeypatch) -> None:
    """P0-1: relative bundle + nested concept id lands at the correct path."""
    bundle_dir = tmp_path / "relbundle"
    shutil.copytree(DEMO, bundle_dir)
    monkeypatch.chdir(tmp_path)
    rc, _, err = _capture([
        "write-concept", "--bundle", "relbundle",
        "--id", "deep/nested/concept", "--type", "Note",
        "--body", "deep",
    ])
    assert rc == 0, f"unexpected rc={rc} err={err}"
    assert (bundle_dir / "deep" / "nested" / "concept.md").exists()
    assert not (bundle_dir / "relbundle" / "deep" / "nested" / "concept.md").exists()


# --- P1-22: write-concept fail-closed on missing bundle ---------------------


def test_write_concept_missing_bundle_exit_2(tmp_path: Path) -> None:
    """P1-22: missing bundle directory -> exit 2 with clear message."""
    missing = tmp_path / "does_not_exist"
    rc, _, err = _capture([
        "write-concept", "--bundle", str(missing),
        "--id", "tables/x", "--type", "Table",
        "--body", "x",
    ])
    assert rc == 2
    assert "Bundle directory not found" in err
    assert str(missing) in err


def test_write_concept_bundle_is_file_exit_2(tmp_path: Path) -> None:
    """P1-22: bundle path is a file (not a directory) -> exit 2."""
    file_path = tmp_path / "not_a_dir"
    file_path.write_text("i am a file")
    rc, _, err = _capture([
        "write-concept", "--bundle", str(file_path),
        "--id", "tables/x", "--type", "Table",
        "--body", "x",
    ])
    assert rc == 2
    assert "Bundle directory not found" in err
