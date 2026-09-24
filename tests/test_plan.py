"""Tests for ``okf_loom.plan`` (current spec §7)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from okf_loom.model import Bundle
from okf_loom.plan import PlannedAction, Plan, build_plan

from conftest import okf_module_argv, okf_subprocess_env


@pytest.fixture
def demo_bundle() -> Bundle:
    return Bundle.load("samples/demo_bundle")


def test_planned_action_is_frozen() -> None:
    """PlannedAction is a frozen dataclass."""
    a = PlannedAction(action="test", why="x", evidence="y")
    with pytest.raises(AttributeError):
        a.action = "other"  # type: ignore[misc]


def test_planned_action_as_dict_has_all_fields() -> None:
    """as_dict includes all envelope fields."""
    a = PlannedAction(
        action="add_link",
        why="test",
        evidence="file:1",
        concept_id="tables/users",
        argv=["okf", "link-add"],
        shell=False,
    )
    d = a.as_dict()
    assert d["action"] == "add_link"
    assert d["argv"] == ["okf", "link-add"]
    assert d["shell"] is False
    assert d["concept_id"] == "tables/users"


def test_plan_as_dict_shape() -> None:
    """Plan.as_dict has bundle_root, total, by_action, actions."""
    p = Plan(
        bundle_root="/tmp/x",
        actions=[
            PlannedAction(action="add_link", why="a", evidence="b"),
            PlannedAction(action="add_link", why="c", evidence="d"),
            PlannedAction(action="create_index", why="e", evidence="f"),
        ],
    )
    d = p.as_dict()
    assert d["bundle_root"] == "/tmp/x"
    assert d["total"] == 3
    assert d["by_action"] == {"add_link": 2, "create_index": 1}
    assert len(d["actions"]) == 3


def test_build_plan_returns_plan(demo_bundle: Bundle) -> None:
    """build_plan returns a Plan with actions."""
    plan = build_plan(demo_bundle)
    assert isinstance(plan, Plan)
    assert plan.bundle_root is not None
    assert isinstance(plan.actions, list)


def test_build_plan_mirror_relation(demo_bundle: Bundle) -> None:
    """Demo bundle has typed relations without body links → mirror_relation actions."""
    plan = build_plan(demo_bundle)
    mirror_actions = [a for a in plan.actions if a.action == "mirror_relation"]
    assert len(mirror_actions) > 0
    for a in mirror_actions:
        assert a.argv is not None
        assert "link-add" in a.argv
        assert "--relation" in a.argv


def test_build_plan_deterministic(demo_bundle: Bundle) -> None:
    """Same bundle → same plan."""
    p1 = build_plan(demo_bundle)
    p2 = build_plan(demo_bundle)
    assert [a.as_dict() for a in p1.actions] == [a.as_dict() for a in p2.actions]


def test_plan_to_json_and_back(demo_bundle: Bundle, tmp_path: Path) -> None:
    """Plan JSON round-trips and can be consumed by update.load_plan."""
    from okf_loom.update import load_plan

    plan = build_plan(demo_bundle)
    out = tmp_path / "plan.json"
    out.write_text(json.dumps(plan.as_dict(), indent=2), encoding="utf-8")

    loaded = load_plan(out)
    assert loaded.plan_kind == "action-envelope"
    # mirror_relation actions produce add_link + add_relation ops
    assert len(loaded.ops) > 0


# NOTE: a weaker ``test_plan_update_idempotent`` previously lived here AND was
# redefined further below. Python keeps only the LAST definition, so the first
# one (a shallow ``count_after < count_before`` check) was dead code that
# shadowed nothing and gave false confidence. It has been removed; the
# canonical, strengthened version (P2-12: asserts applied == 0 on re-apply) is
# the sole definition further below. Do NOT re-add a duplicate here.


def test_mechanical_vs_template_actions(demo_bundle: Bundle) -> None:
    """Mechanical actions have argv; template actions have argv_template."""
    plan = build_plan(demo_bundle)
    for action in plan.actions:
        if action.argv_template:
            assert action.agent_instruction is not None
            assert action.confidence is not None
        elif action.argv:
            assert action.agent_instruction is None or action.action == "mirror_relation"


# --- Iter-10 P1-14: missing_relations_hint -> add_relation (template) -------


def test_missing_relations_hint_becomes_add_relation_template(tmp_path: Path) -> None:
    """P1-14: a table with an FK-like schema column and no outgoing link
    must produce an ``add_relation`` action with agent_instruction + TWO
    unfilled slots (<TARGET_ID> + <RELATION_TYPE>). Previously the branch was
    gated on ``cid_str and target_str`` but discover always emits
    target_concept_id=None, silently dropping every such suggestion."""
    import shutil
    from okf_loom.discover import discover_suggestions
    from okf_loom.model import Bundle

    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    (dst / "tables" / "fk_test.md").write_text(
        "---\ntype: Table\ntitle: FK Test\n---\n"
        "# Schema\n\n"
        "| Column | Type | Description |\n|---|---|---|\n"
        "| id | STRING | primary key |\n"
        "| user_id | STRING | FK to users |\n",
        encoding="utf-8",
    )
    b = Bundle.load(dst)

    # First confirm discover emits missing_relations_hint for this concept.
    report = discover_suggestions(b, rules=["missing_relations_hint"])
    hints = [s for s in report.suggestions if s.rule == "missing_relations_hint"]
    assert any(("tables", "fk_test") == s.concept_id for s in hints), (
        "discover should emit missing_relations_hint for tables/fk_test"
    )

    plan = build_plan(b)
    add_rel = [a for a in plan.actions if a.action == "add_relation"]
    assert len(add_rel) >= 1, (
        f"Expected at least one add_relation action; got {[a.action for a in plan.actions]}"
    )
    a = add_rel[0]
    # Template-only: no concrete argv; has argv_template + agent_instruction + confidence.
    assert a.argv is None
    assert a.argv_template is not None
    assert "<TARGET_ID>" in a.argv_template, a.argv_template
    assert "<RELATION_TYPE>" in a.argv_template, a.argv_template
    assert a.agent_instruction is not None
    assert a.confidence is not None


# --- Iter-10 P1-15: create_index/refresh_index argv is POSITIONAL bundle ---


def test_create_index_argv_is_positional_bundle(tmp_path: Path) -> None:
    """P1-15: ``okf index`` takes a POSITIONAL bundle. Plan's create_index
    argv must be ``["okf", "index", <bundle>]`` (no ``--bundle`` flag)."""
    import shutil
    from okf_loom.plan import build_plan
    from okf_loom.model import Bundle

    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    (dst / "newdir").mkdir()
    (dst / "newdir" / "thing.md").write_text(
        "---\ntype: Table\ntitle: Thing\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(dst)
    plan = build_plan(b)
    create_actions = [a for a in plan.actions if a.action == "create_index"]
    assert len(create_actions) >= 1
    for a in create_actions:
        assert a.argv[:3] == ["okf", "index", str(dst.resolve())], a.argv
        assert "--bundle" not in a.argv, f"--bundle leaked into index argv: {a.argv}"


def test_refresh_index_argv_is_positional_bundle(tmp_path: Path) -> None:
    """P1-15: refresh_index argv is positional too."""
    import shutil
    from okf_loom.plan import build_plan
    from okf_loom.model import Bundle

    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    # Force a stale index by adding a concept not reflected in tables/index.md.
    (dst / "tables" / "extra.md").write_text(
        "---\ntype: Table\ntitle: Extra\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(dst)
    plan = build_plan(b)
    refresh_actions = [a for a in plan.actions if a.action == "refresh_index"]
    # The refresh_index may or may not appear depending on whether discovery
    # already flagged the directory via create_index; if it does appear, it
    # must use the positional form.
    for a in refresh_actions:
        assert a.argv[:3] == ["okf", "index", str(dst.resolve())], a.argv
        assert "--bundle" not in a.argv, a.argv


def test_index_argv_runs_via_subprocess(tmp_path: Path) -> None:
    """P1-15: the literal index argv executes via subprocess and exits 0.
    (Run from a DIFFERENT cwd to prove the absolute bundle path is portable.)"""
    import shutil, subprocess
    from okf_loom.plan import build_plan
    from okf_loom.model import Bundle

    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    (dst / "newdir").mkdir()
    (dst / "newdir" / "thing.md").write_text(
        "---\ntype: Table\ntitle: Thing\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(dst)
    plan = build_plan(b)
    create_actions = [a for a in plan.actions if a.action == "create_index"]
    assert create_actions, "expected a create_index action"
    argv = create_actions[0].argv
    # Substitute argv[0] "okf" with the module invocation for the test process;
    # argv shape after [0] is the contract.
    run_argv = okf_module_argv(*argv[1:])
    # Run from tmp_path (NOT the bundle dir) to prove the absolute path works.
    result = subprocess.run(
        run_argv, cwd=str(tmp_path), env=okf_subprocess_env(), capture_output=True,
    )
    assert result.returncode == 0, (
        f"subprocess failed rc={result.returncode}\n"
        f"stderr: {result.stderr.decode(errors='replace')}"
    )


# --- Iter-10 P1-19: no silent suggestion drops (skipped discipline) ---------


def test_no_suggestion_silently_dropped(tmp_path: Path) -> None:
    """P1-19: every discover suggestion is either an action or in Plan.skipped."""
    import shutil
    from okf_loom.discover import discover_suggestions
    from okf_loom.plan import build_plan
    from okf_loom.model import Bundle

    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    (dst / "tables" / "fk_test.md").write_text(
        "---\ntype: Table\ntitle: FK Test\n---\n"
        "# Schema\n\n| user_id | STRING | FK to users |\n",
        encoding="utf-8",
    )
    (dst / "tables" / "orphan.md").write_text(
        "---\ntype: Table\ntitle: Orphan\n---\nstandalone\n", encoding="utf-8"
    )
    b = Bundle.load(dst)

    report = discover_suggestions(b)
    plan = build_plan(b)

    # Total accounting: every suggestion maps 1:1 to either an action or a
    # skipped entry. (mirror_relation/refresh_index actions don't come from
    # suggestions, so they're additive on the action side.)
    assert len(report.suggestions) == len(plan.actions) - (
        len([a for a in plan.actions if a.action not in (
            "add_link", "add_description", "create_index",
            "fix_broken_link", "add_relation", "connect",
        )])
    ) + len(plan.skipped)

    # Stronger, rule-level assertion: every suggestion's rule appears in
    # either actions (via the rule->verb map) or skipped.
    from okf_loom.plan import _RULE_TO_ACTION
    action_verbs = {a.action for a in plan.actions}
    skipped_rules = {rule for (rule, _reason) in plan.skipped}
    for s in report.suggestions:
        verb = _RULE_TO_ACTION.get(s.rule)
        # Either the suggestion's verb is in actions, or its rule is in skipped.
        assert (verb in action_verbs) or (s.rule in skipped_rules), (
            f"Suggestion {s.rule!r} was silently dropped "
            f"(verb={verb}, action_verbs={action_verbs}, skipped={skipped_rules})"
        )


def test_plan_as_dict_has_skipped_field() -> None:
    """P1-19: Plan.as_dict exposes a `skipped` summary."""
    p = Plan(
        bundle_root="/x",
        actions=[PlannedAction(action="add_link", why="a", evidence="b")],
        skipped=[("missing_relations_hint", "missing_concept_id")],
    )
    d = p.as_dict()
    assert d["skipped"]["total"] == 1
    assert d["skipped"]["items"] == [
        {"rule": "missing_relations_hint", "reason": "missing_concept_id"}
    ]


# --- Iter-10 P2-10: dedup reads PlannedAction.context, not the why string ---


def test_create_index_dedup_via_context(tmp_path: Path) -> None:
    """P2-10: when discovery's missing_indexes AND index staleness both fire
    for the same directory, only ONE action is emitted. Dedup must read
    PlannedAction.context['directory'], not parse the why string."""
    import shutil
    from okf_loom.plan import build_plan
    from okf_loom.model import Bundle

    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    # newdir/ has a concept but no index.md — triggers BOTH missing_indexes
    # (discovery) AND would_write (plan_index_regeneration).
    (dst / "newdir").mkdir()
    (dst / "newdir" / "thing.md").write_text(
        "---\ntype: Table\ntitle: Thing\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(dst)
    plan = build_plan(b)
    # Only ONE create_index action for "newdir" (no duplicate refresh_index).
    newdir_actions = [
        a for a in plan.actions
        if a.action in ("create_index", "refresh_index")
        and a.context.get("directory") == "newdir"
    ]
    assert len(newdir_actions) == 1, (
        f"Expected exactly one index action for newdir; got {newdir_actions}"
    )
    # And the create_index branch carried the structured directory field.
    assert newdir_actions[0].context.get("directory") == "newdir"


def test_planned_action_as_dict_includes_context() -> None:
    """P2-10: PlannedAction.as_dict exposes the context field."""
    a = PlannedAction(
        action="create_index", why="x", evidence="y",
        context={"directory": "foo"},
    )
    d = a.as_dict()
    assert d["context"] == {"directory": "foo"}


# --- Iter-10 P2-12: idempotency acceptance strengthened to applied:0 --------


def test_plan_update_idempotent(demo_bundle: Bundle, tmp_path: Path) -> None:
    """P2-12: strengthen the idempotency assertion to acceptance level —
    applying the re-built plan reports 0 ops applied (excluding the standard
    needs_agent_input / run_okf_index_directly skips)."""
    import shutil
    from okf_loom.update import load_plan, apply_plan

    # Copy demo bundle to a temp location so we can mutate
    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    bundle = Bundle.load(dst)

    # Generate plan before applying
    plan1 = build_plan(bundle)
    count_before = len(plan1.actions)

    # Apply the plan
    out = tmp_path / "plan.json"
    out.write_text(json.dumps(plan1.as_dict(), indent=2), encoding="utf-8")
    loaded1 = load_plan(out)
    apply_plan(bundle, loaded1, apply=True)

    # Reload bundle and re-plan
    bundle2 = Bundle.load(dst)
    plan2 = build_plan(bundle2)
    count_after = len(plan2.actions)

    # After mirroring, the mirror_relation actions should be gone (body links now exist)
    assert count_after < count_before

    # P2-12 STRENGTHENED: applying the second plan MUST report 0 applied ops
    # for all mutation ops (idempotency acceptance — not just "decreased").
    out2 = tmp_path / "plan2.json"
    out2.write_text(json.dumps(plan2.as_dict(), indent=2), encoding="utf-8")
    bundle3 = Bundle.load(dst)
    loaded2 = load_plan(out2)
    result2 = apply_plan(bundle3, loaded2, apply=True)
    applied2 = sum(1 for _op, r in result2["results"] if r.get("applied"))
    assert applied2 == 0, (
        f"Second apply must be idempotent (applied:0); got {applied2} applied. "
        f"Reasons: {[r.get('reason') for _o, r in result2['results']]}"
    )


# --- Iter-10 P2-14: Plan.as_dict by_action is explicitly sorted -------------


def test_plan_by_action_sorted_in_as_dict() -> None:
    """P2-14: by_action keys appear in sorted order in the as_dict output."""
    # Insert actions in reverse-sorted verb order.
    p = Plan(
        bundle_root="/x",
        actions=[
            PlannedAction(action="create_index", why="a", evidence="b"),
            PlannedAction(action="add_link", why="c", evidence="d"),
            PlannedAction(action="add_link", why="e", evidence="f"),
            PlannedAction(action="mirror_relation", why="g", evidence="h"),
        ],
    )
    d = p.as_dict()
    keys = list(d["by_action"].keys())
    assert keys == sorted(keys), f"by_action keys not sorted: {keys}"
    assert keys == ["add_link", "create_index", "mirror_relation"]
    assert d["by_action"] == {"add_link": 2, "create_index": 1, "mirror_relation": 1}


# --- Iter-10 P2-15: relation-mirror target tolerates trailing .md -----------


def test_relation_mirror_tolerates_md_suffix(tmp_path: Path) -> None:
    """P2-15: relations whose target ends in ``.md`` are compared against body
    links by stripping the suffix, mirroring how body links resolve. No
    spurious mirror_relation should fire when a body link already exists."""
    import shutil
    from okf_loom.plan import build_plan, _find_unmirrored_relations
    from okf_loom.model import Bundle

    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    # customers.md already exists in demo and orders.md already has a body
    # link to /tables/customers.md. Add a typed relation with the .md suffix
    # that points to the SAME target the body link resolves to.
    orders_path = dst / "tables" / "orders.md"
    orders_text = orders_path.read_text(encoding="utf-8")
    # Insert a relations entry with target ending in .md pointing to customers
    orders_path.write_text(
        orders_text.replace(
            "relations:",
            "relations:\n  - target: tables/customers.md\n    type: extra_rel\n    detail: ''",
        ),
        encoding="utf-8",
    )
    b = Bundle.load(dst)
    # The .md-suffixed relation to customers should NOT be flagged unmirrored,
    # because orders.md already has a body link to customers.
    unmirrored = _find_unmirrored_relations(b)
    customers_unmirrored = [
        r for r in unmirrored
        if r[0] == ("tables", "orders") and "customers" in r[2]
    ]
    assert customers_unmirrored == [], (
        f".md-suffixed relation to customers should be considered already "
        f"mirrored; got {customers_unmirrored}"
    )


# --- Iter-10 P3-3: PlannedAction.shell=False when argv set (invariant) -----


def test_planned_action_post_init_rejects_argv_with_shell() -> None:
    """P3-3: PlannedAction raises if argv is set AND shell=True."""
    with pytest.raises(ValueError, match="shell"):
        PlannedAction(
            action="x", why="y", evidence="z",
            argv=["okf"], shell=True,
        )


def test_planned_action_post_init_allows_argv_no_shell() -> None:
    """P3-3: argv + shell=False (default) is fine."""
    a = PlannedAction(
        action="x", why="y", evidence="z",
        argv=["okf", "info"], shell=False,
    )
    assert a.argv == ["okf", "info"]
    assert a.shell is False


def test_planned_action_post_init_allows_template_with_shell_false() -> None:
    """P3-3: template actions (no argv) are unaffected."""
    a = PlannedAction(
        action="x", why="y", evidence="z",
        argv_template=["okf", "<X>"],
    )
    assert a.shell is False  # default


# --- Iter-2 P2-3: build_plan(portable=True) emits bundle-relative argv ------


def test_build_plan_portable_emits_relative_bundle_arg(tmp_path: Path) -> None:
    """P2-3: ``build_plan(bundle, portable=True)`` sets
    ``Plan.bundle_root == "."`` and emits ``"."`` as the bundle argument
    in every action's argv. No host-absolute path leaks into the plan.

    Operators must run the emitted commands from the bundle directory
    (the chdir precondition); the default (portable=False) keeps the
    resolved absolute path so argv is runnable as-is from any CWD
    (current spec §7).
    """
    import shutil
    from okf_loom.model import Bundle

    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    b = Bundle.load(dst)

    plan_portable = build_plan(b, portable=True)
    assert plan_portable.bundle_root == ".", (
        f"portable Plan.bundle_root must be '.'; got {plan_portable.bundle_root!r}"
    )
    assert len(plan_portable.actions) > 0
    host_abs = str(dst.resolve())
    for a in plan_portable.actions:
        if a.argv:
            # No host path leaks into the argv.
            assert not any(host_abs in str(v) for v in a.argv), (
                f"host-absolute path leaked into portable argv: {a.argv}"
            )
            # The bundle argument is "." for --bundle verbs, or appears as a
            # positional "." for create_index/refresh_index.
            if "--bundle" in a.argv:
                idx = a.argv.index("--bundle")
                assert a.argv[idx + 1] == "."
            else:
                assert "." in a.argv[2:], (
                    f"positional-bundle argv must include '.': {a.argv}"
                )


def test_build_plan_default_keeps_absolute_bundle_arg(tmp_path: Path) -> None:
    """P2-3 contrast: ``build_plan(bundle)`` (default) keeps the existing
    P2-9 behaviour — argv uses the resolved absolute bundle path so it is
    runnable as-is from any CWD."""
    import shutil
    from okf_loom.model import Bundle

    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    b = Bundle.load(dst)
    plan = build_plan(b)  # portable=False default
    assert plan.bundle_root == str(b.root)
    for a in plan.actions:
        if a.argv and "--bundle" in a.argv:
            idx = a.argv.index("--bundle")
            assert a.argv[idx + 1] == str(dst.resolve())


def test_plan_as_dict_docstring_documents_portable_precondition() -> None:
    """P2-3: ``Plan.as_dict`` docstring documents the chdir precondition
    so a downstream consumer reading the docstring (or `help`) learns
    that portable plans must be run from the bundle directory."""
    from okf_loom.plan import Plan
    doc = Plan.as_dict.__doc__ or ""
    assert "portable" in doc.lower(), (
        "Plan.as_dict docstring must mention the portable flag"
    )
    assert "bundle directory" in doc.lower(), (
        "Plan.as_dict docstring must document the chdir precondition"
    )


def test_iter3_p1_5_mirror_relation_normalizes_md_slash_target(tmp_path):
    """P1-5: a relation with target '/tables/customers.md' (leading slash +
    .md) must emit a NORMALIZED target in argv so the mirror action
    converges. iter-1 P2-15 stripped .md only for comparison, not argv
    emission → infinite non-converging loop."""
    from okf_loom import Bundle
    from okf_loom.plan import build_plan
    (tmp_path / "index.md").write_text("okf_version: '0.1'\n", encoding="utf-8")
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\nrelations:\n  - target: /b.md\n    type: references\n"
        "---\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text("---\ntype: T\ntitle: B\n---\nbody\n", encoding="utf-8")
    b = Bundle.load(tmp_path)
    plan = build_plan(b)
    mirrors = [a for a in plan.actions if a.action == "mirror_relation"]
    assert mirrors, "expected at least one mirror_relation action"
    for m in mirrors:
        # argv target must be normalized (no leading /, no .md suffix).
        target_idx = m.argv.index("--target") + 1
        target = m.argv[target_idx]
        assert not target.startswith("/"), f"leading slash not stripped: {target}"
        assert not target.endswith(".md"), f".md suffix not stripped: {target}"


# --- Iter-1 P2-10 (arch Candidate 2): op_payload structured application -------
#
# PlannedAction.op_payload is the structured application path that lets
# update._actions_to_ops build UpdateOps directly from a dict instead of
# re-parsing argv. The contract: the op_payload path and the argv fallback
# path MUST produce identical UpdateOps for every verb the planner emits.
# This eliminates the emitter/parser drift class of bugs (P1-5 iter-3 was
# of this class).


def test_op_payload_populated_on_mechanical_actions(demo_bundle: Bundle) -> None:
    """Mechanical actions (add_link, mirror_relation, create_index,
    refresh_index) carry op_payload; template actions (add_description,
    fix_broken_link, add_relation, connect) do not."""
    plan = build_plan(demo_bundle)
    mechanical_verbs = {"add_link", "mirror_relation", "create_index", "refresh_index"}
    template_verbs = {"add_description", "fix_broken_link", "add_relation", "connect"}
    for a in plan.actions:
        d = a.as_dict()
        if a.action in mechanical_verbs:
            assert d.get("op_payload") is not None, (
                f"mechanical action {a.action!r} must carry op_payload"
            )
            assert "handler" in d["op_payload"], (
                f"op_payload for {a.action!r} missing 'handler' key"
            )
        elif a.action in template_verbs:
            # Template actions need agent input; op_payload is None and the
            # key is OMITTED from as_dict (preserves the exact §6.1 shape).
            assert "op_payload" not in d, (
                f"template action {a.action!r} must NOT carry op_payload"
            )
            assert a.op_payload is None


def test_op_payload_as_dict_round_trips(tmp_path: Path) -> None:
    """as_dict() → JSON → load preserves op_payload (round-trip safety)."""
    from okf_loom.update import load_plan

    a = PlannedAction(
        action="add_link",
        why="x",
        evidence="y",
        concept_id="tables/users",
        argv=["okf", "link-add", "--source", "tables/users", "--target", "tables/orders"],
        op_payload={
            "handler": "add_link",
            "source": "tables/users",
            "target": "tables/orders",
            "label": "orders",
            "section": None,
        },
    )
    p = Plan(bundle_root="/x", actions=[a])
    out = tmp_path / "plan.json"
    out.write_text(json.dumps(p.as_dict(), indent=2), encoding="utf-8")

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["actions"][0]["op_payload"] == {
        "handler": "add_link",
        "source": "tables/users",
        "target": "tables/orders",
        "label": "orders",
        "section": None,
    }
    # load_plan accepts the action-envelope plan (exercises _actions_to_ops).
    loaded = load_plan(out)
    assert loaded.plan_kind == "action-envelope"
    assert len(loaded.ops) == 1
    assert loaded.ops[0].kind == "add_link"


def test_op_payload_omitted_from_as_dict_when_none() -> None:
    """When op_payload is None, as_dict() must OMIT the key entirely so
    template actions preserve the exact §6.1 envelope shape (no null key
    leaks into the JSON)."""
    a = PlannedAction(
        action="add_description", why="x", evidence="y",
        argv_template=["okf", "set-frontmatter", "--value", "<DESC>"],
    )
    d = a.as_dict()
    assert "op_payload" not in d, (
        f"op_payload key must be omitted when None; got keys {list(d)}"
    )


def test_op_payload_path_matches_argv_path(demo_bundle: Bundle) -> None:
    """CONTRACT-RUNTIME-PARITY: for every mechanical action the planner
    emits, the structured op_payload path and the argv fallback path MUST
    produce identical (ops, skipped). This is the core correctness
    property of the op_payload design — if it ever breaks, the two paths
    can drift and reintroduce the P1-5 convergence bug class."""
    from okf_loom.update import _actions_to_ops

    plan = build_plan(demo_bundle)
    # Only compare actions that have BOTH argv AND op_payload (mechanical).
    # Template actions have neither a payload nor a concrete argv, so they
    # cannot cross-check the two paths.
    mechanical = [a for a in plan.actions if a.argv and a.op_payload is not None]
    assert mechanical, (
        "expected at least one mechanical action with both argv and op_payload"
    )

    for action in mechanical:
        d_with = action.as_dict()
        d_without = {k: v for k, v in d_with.items() if k != "op_payload"}

        ops_with, skipped_with = _actions_to_ops([d_with])
        ops_without, skipped_without = _actions_to_ops([d_without])

        # Compare ops via as_dict (JSON-serialisable structural equality).
        ops_with_d = [op.as_dict() for op in ops_with]
        ops_without_d = [op.as_dict() for op in ops_without]
        assert ops_with_d == ops_without_d, (
            f"op_payload path and argv path produced different ops for "
            f"action {action.action!r}:\n"
            f"  payload path: {ops_with_d}\n"
            f"  argv path:    {ops_without_d}"
        )
        assert skipped_with == skipped_without, (
            f"op_payload path and argv path produced different skipped for "
            f"action {action.action!r}:\n"
            f"  payload path: {skipped_with}\n"
            f"  argv path:    {skipped_without}"
        )


def test_op_payload_none_falls_back_to_argv(demo_bundle: Bundle) -> None:
    """BACKWARD COMPAT: a plan whose actions have NO op_payload (older
    plan files written before op_payload existed, or hand-authored plans)
    still loads and applies via the argv fallback path."""
    from okf_loom.update import _actions_to_ops

    plan = build_plan(demo_bundle)
    # Strip op_payload from every action to simulate an argv-only plan file.
    compat_actions = [
        {k: v for k, v in a.as_dict().items() if k != "op_payload"}
        for a in plan.actions
    ]
    # Sanity: confirm we actually stripped it.
    assert all("op_payload" not in a for a in compat_actions)

    ops, skipped = _actions_to_ops(compat_actions)
    # The demo bundle has mirror_relation actions → at least one add_link op.
    assert len(ops) > 0, "argv fallback path should still produce ops"
    # No skip reason should blame op_payload (the argv fallback never sees it).
    for s in skipped:
        assert "op_payload" not in s.get("reason", ""), (
            f"argv fallback path leaked op_payload awareness into skip reason: {s}"
        )


def test_plan_update_idempotent_via_op_payload(tmp_path: Path) -> None:
    """IDEMPOTENCY via op_payload: applying the re-built plan (which now
    populates op_payload on every mechanical action) reports 0 ops applied
    on the second run. Strengthens test_plan_update_idempotent by asserting
    op_payload was actually consumed (not silently bypassed)."""
    import shutil
    from okf_loom.update import load_plan, apply_plan, _actions_to_ops

    dst = tmp_path / "bundle"
    shutil.copytree("samples/demo_bundle", dst)
    bundle = Bundle.load(dst)

    plan1 = build_plan(bundle)
    out = tmp_path / "plan.json"
    out.write_text(json.dumps(plan1.as_dict(), indent=2), encoding="utf-8")

    # Sanity: the plan actually carries op_payload on mechanical actions,
    # and _actions_to_ops consumes it (the structured path is live, not
    # bypassed by an early argv branch).
    mechanical_with_payload = [
        a for a in plan1.actions
        if a.op_payload is not None and a.argv
    ]
    if mechanical_with_payload:
        sample = mechanical_with_payload[0].as_dict()
        ops_from_payload, _ = _actions_to_ops([sample])
        assert len(ops_from_payload) >= 1, (
            "op_payload path should produce ops for a mechanical action; "
            "the structured path may be bypassed"
        )

    loaded1 = load_plan(out)
    apply_plan(bundle, loaded1, apply=True)

    # Re-plan + re-apply → must be idempotent (0 applied).
    bundle2 = Bundle.load(dst)
    plan2 = build_plan(bundle2)
    out2 = tmp_path / "plan2.json"
    out2.write_text(json.dumps(plan2.as_dict(), indent=2), encoding="utf-8")
    bundle3 = Bundle.load(dst)
    loaded2 = load_plan(out2)
    result2 = apply_plan(bundle3, loaded2, apply=True)
    applied2 = sum(1 for _op, r in result2["results"] if r.get("applied"))
    assert applied2 == 0, (
        f"Second apply must be idempotent via op_payload (applied:0); "
        f"got {applied2} applied. Reasons: "
        f"{[r.get('reason') for _o, r in result2['results']]}"
    )


def test_op_payload_fail_closed_missing_source() -> None:
    """FAIL-CLOSED: an op_payload with handler=add_link but missing source
    is skipped with a machine-readable reason, never silently dropped."""
    from okf_loom.update import _actions_to_ops

    actions = [{
        "action": "add_link",
        "why": "x",
        "evidence": "y",
        "concept_id": "tables/users",
        "argv": ["okf", "link-add", "--target", "tables/orders"],
        "op_payload": {"handler": "add_link", "target": "tables/orders"},
    }]
    ops, skipped = _actions_to_ops(actions)
    assert ops == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "op_payload_missing_source_or_target"


def test_op_payload_fail_closed_invalid_source() -> None:
    """FAIL-CLOSED: an op_payload whose source is not a valid concept id
    is skipped with reason invalid_source (mirrors the argv path)."""
    from okf_loom.update import _actions_to_ops

    actions = [{
        "action": "add_link",
        "why": "x",
        "evidence": "y",
        "concept_id": "bad",
        "argv": ["okf", "link-add"],
        "op_payload": {
            "handler": "add_link",
            "source": "not/a/valid///concept id",
            "target": "tables/orders",
        },
    }]
    ops, skipped = _actions_to_ops(actions)
    assert ops == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "invalid_source"


def test_op_payload_fail_closed_unknown_handler() -> None:
    """FAIL-CLOSED: an op_payload with an unknown handler is skipped with
    a machine-readable reason (forward-compatible; never silently dropped)."""
    from okf_loom.update import _actions_to_ops

    actions = [{
        "action": "future_verb",
        "why": "x",
        "evidence": "y",
        "concept_id": "tables/users",
        "argv": ["okf", "future"],
        "op_payload": {"handler": "future_handler"},
    }]
    ops, skipped = _actions_to_ops(actions)
    assert ops == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "unhandled_op_payload_handler:future_handler"


def test_op_payload_regenerate_indexes_skipped_same_as_argv() -> None:
    """create_index/refresh_index via op_payload are skipped with the SAME
    reason as the argv path (run_okf_index_directly) — the two paths are
    observably identical to consumers of the skipped list."""
    from okf_loom.update import _actions_to_ops

    # op_payload path
    payload_action = {
        "action": "create_index",
        "why": "x",
        "evidence": "y",
        "argv": ["okf", "index", "/b"],
        "op_payload": {"handler": "regenerate_indexes", "directory": "."},
    }
    _ops_p, skipped_p = _actions_to_ops([payload_action])
    # argv-only path
    argv_action = {k: v for k, v in payload_action.items() if k != "op_payload"}
    _ops_a, skipped_a = _actions_to_ops([argv_action])
    assert skipped_p == skipped_a == [
        {"action": "create_index", "reason": "run_okf_index_directly"}
    ]


# --- §11 scoped enrichment behavioural tests (P1-12 / ARCH-004) -------------
#
# build_plan(scope=..., neighbors=...) must produce a Plan whose every action
# targets the scoped concept set (+ neighbours). The scope filter is applied
# to discovery, to index staleness, AND to relation mirroring (see
# build_plan docstring), so a scoped plan never leaks out-of-scope work.


def _plan_scope_bundle(tmp_path: Path) -> Path:
    """Bundle with two source concepts each carrying an unlinked mention.

    Gives build_plan two add_link actions (a→b and x→y) so we can prove
    scope narrows the plan to a's action only.
    """
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: Ay\n---\nWe mention Bee without linking.\n",
        encoding="utf-8",
    )
    (tmp_path / "x.md").write_text(
        "---\ntype: T\ntitle: Ex\n---\nWe mention Whye without linking.\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: T\ntitle: Bee\n---\nBee body.\n", encoding="utf-8",
    )
    (tmp_path / "y.md").write_text(
        "---\ntype: T\ntitle: Whye\n---\nWhye body.\n", encoding="utf-8",
    )
    return tmp_path


def _plan_neighbor_bundle(tmp_path: Path) -> Path:
    """Bundle where ``a`` links to ``b`` (graph neighbour), and both ``a``
    and ``b`` mention ``c`` without linking. Used to prove ``--neighbors``
    pulls the neighbour's add_link action into the scoped plan.
    """
    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: Ay\n---\n"
        "We link to [Bee](/b.md) and also mention Cee without linking.\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: T\ntitle: Bee\n---\n"
        "Bee body. We also mention Cee without linking.\n",
        encoding="utf-8",
    )
    (tmp_path / "c.md").write_text(
        "---\ntype: T\ntitle: Cee\n---\nCee body.\n", encoding="utf-8",
    )
    return tmp_path


def test_build_plan_scope_restricts_every_action(tmp_path: Path) -> None:
    """§11: ``build_plan(scope=["a"])`` returns a Plan whose every action's
    concept_id is in scope. The x→y action that appears in the unscoped plan
    must NOT appear in the scoped plan."""
    from okf_loom.model import Bundle as _B

    b = _B.load(_plan_scope_bundle(tmp_path))
    # Unscoped plan: both add_link actions appear.
    full = build_plan(b)
    full_sources = {a.concept_id for a in full.actions if a.action == "add_link"}
    assert "a" in full_sources and "x" in full_sources, (
        f"unscoped plan missing expected add_link actions: {full_sources}"
    )
    # Scoped plan: ONLY a's add_link remains.
    scoped = build_plan(b, scope=["a"])
    scoped_sources = {a.concept_id for a in scoped.actions if a.action == "add_link"}
    assert scoped_sources == {"a"}, (
        f"scope=['a'] leaked out-of-scope add_link actions: {scoped_sources}"
    )
    # Every action in the scoped plan must target the scoped set.
    for action in scoped.actions:
        if action.concept_id is not None:
            assert action.concept_id == "a", (
                f"action {action.action!r} targets out-of-scope concept "
                f"{action.concept_id!r}"
            )


def test_build_plan_scope_with_neighbors_pulls_neighbour_actions(
    tmp_path: Path,
) -> None:
    """§11 ``--neighbors``: ``build_plan(scope=["a"], neighbors=True)`` also
    returns the add_link action on b (a's graph neighbour), which the bare
    scope plan drops."""
    from okf_loom.model import Bundle as _B

    b = _B.load(_plan_neighbor_bundle(tmp_path))
    # Bare scope: only a's add_link (a→c).
    bare = build_plan(b, scope=["a"])
    bare_sources = {a.concept_id for a in bare.actions if a.action == "add_link"}
    assert bare_sources == {"a"}, bare_sources
    # With neighbours: b's add_link (b→c) is now in the plan too.
    nb = build_plan(b, scope=["a"], neighbors=True)
    nb_sources = {a.concept_id for a in nb.actions if a.action == "add_link"}
    assert nb_sources == {"a", "b"}, (
        f"--neighbors did not pull b's add_link into the plan: {nb_sources}"
    )


def test_build_plan_scope_filters_relation_mirroring(tmp_path: Path) -> None:
    """§11: relation mirroring is restricted to scoped concepts. A
    mirror_relation action for an out-of-scope source concept must NOT
    appear in the scoped plan."""
    from okf_loom.model import Bundle as _B

    (tmp_path / "index.md").write_text("# Bundle\n", encoding="utf-8")
    # orders has a typed relation to customers with no body link → mirror.
    (tmp_path / "orders.md").write_text(
        "---\ntype: Table\ntitle: Orders\n"
        "relations:\n  - target: customers\n    type: references\n"
        "---\nOrders body.\n",
        encoding="utf-8",
    )
    (tmp_path / "customers.md").write_text(
        "---\ntype: Table\ntitle: Customers\n---\nCustomers body.\n",
        encoding="utf-8",
    )
    # invoices has a typed relation to vendors with no body link → mirror.
    (tmp_path / "invoices.md").write_text(
        "---\ntype: Table\ntitle: Invoices\n"
        "relations:\n  - target: vendors\n    type: references\n"
        "---\nInvoices body.\n",
        encoding="utf-8",
    )
    (tmp_path / "vendors.md").write_text(
        "---\ntype: Table\ntitle: Vendors\n---\nVendors body.\n",
        encoding="utf-8",
    )
    b = _B.load(tmp_path)
    full = build_plan(b)
    full_mirrors = {a.concept_id for a in full.actions if a.action == "mirror_relation"}
    assert "orders" in full_mirrors and "invoices" in full_mirrors, full_mirrors
    # Scoped to orders: only orders' mirror_relation remains.
    scoped = build_plan(b, scope=["orders"])
    scoped_mirrors = {
        a.concept_id for a in scoped.actions if a.action == "mirror_relation"
    }
    assert scoped_mirrors == {"orders"}, (
        f"scope=['orders'] leaked out-of-scope mirror_relation: {scoped_mirrors}"
    )


def test_build_plan_scope_filter_load_bearing_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    """§11 mutation test (P1-12): if the scope plumbing in build_plan is
    bypassed, a scoped plan leaks out-of-scope actions. We simulate the
    mutation by stripping ``scope``/``neighbors`` from the kwargs (the
    exact regression the test guards against) and confirm the unscoped
    path returns actions for both source concepts.
    """
    from okf_loom.model import Bundle as _B

    b = _B.load(_plan_scope_bundle(tmp_path))
    scoped = build_plan(b, scope=["a"])
    scoped_sources = {a.concept_id for a in scoped.actions if a.action == "add_link"}
    assert scoped_sources == {"a"}, scoped_sources

    # Mutation: simulate someone deleting the scope block in build_plan.
    real_fn = build_plan

    def _mutation_no_scope(bundle, **kwargs):
        kwargs.pop("scope", None)
        kwargs.pop("neighbors", None)
        return real_fn(bundle, **kwargs)

    mutated = _mutation_no_scope(b, scope=["a"])
    mutated_sources = {
        a.concept_id for a in mutated.actions if a.action == "add_link"
    }
    # The mutation leaked x back in — that is the regression this test pins.
    assert "x" in mutated_sources, (
        "expected the no-scope mutation to leak x back in; "
        "if it does not, the scope filter contract changed"
    )


# --- Planner skips reserved-filename + out-of-bundle broken links -----------


def test_plan_skips_broken_link_to_directory_index(tmp_path: Path) -> None:
    """A broken-link finding for an ``index.md`` target is
    skipped by the planner with reason ``reserved_filename_index`` —
    directory-index references are valid in the viewer and the
    ``okf link-add --target <TARGET_ID>`` template is useless for them.
    """
    from okf_loom.model import Bundle
    from okf_loom.plan import build_plan

    # A concept with a link to /tutorials/index.md where the index.md
    # does NOT exist on disk (so the validator still emits broken-link).
    (tmp_path / "concept.md").write_text(
        "---\ntype: T\ntitle: C\n---\n"
        "Go to [Tutorials](/tutorials/index.md).\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    plan = build_plan(b)
    # Plan.skipped is list[tuple[rule, reason]].
    skipped_reasons = [reason for (_rule, reason) in plan.skipped]
    assert "reserved_filename_index" in skipped_reasons, (
        f"expected reserved_filename_index skip; got skipped={plan.skipped}"
    )
    # And no fix_broken_link action was emitted for an index.md target.
    fix_broken_actions = [a for a in plan.actions if a.action == "fix_broken_link"]
    for a in fix_broken_actions:
        target_raw = (a.context or {}).get("target_raw", "")
        assert not target_raw.endswith("index.md"), (
            f"planner emitted fix_broken_link for index.md target: {a}"
        )


def test_plan_skips_broken_out_of_bundle_link(tmp_path: Path) -> None:
    """A broken-link finding for an out-of-bundle path
    (``../docs/ghost.md`` that doesn't exist) is skipped by the
    planner with reason ``out_of_bundle_link`` — the agent's only
    meaningful action is to move the target into the bundle or convert
    to an external URL, not to ``okf link-add`` a placeholder.
    """
    from okf_loom.model import Bundle
    from okf_loom.plan import build_plan

    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    (bundle_root / "concept.md").write_text(
        "---\ntype: T\ntitle: C\n---\n"
        # One ../ escapes bundle; the target doesn't exist.
        "Missing: [ghost](../docs/ghost.md).\n",
        encoding="utf-8",
    )
    b = Bundle.load(bundle_root)
    plan = build_plan(b)
    skipped_reasons = [reason for (_rule, reason) in plan.skipped]
    assert "out_of_bundle_link" in skipped_reasons, (
        f"expected out_of_bundle_link skip; got skipped={plan.skipped}"
    )
    # And no fix_broken_link action was emitted for the out-of-bundle link.
    fix_broken_actions = [a for a in plan.actions if a.action == "fix_broken_link"]
    for a in fix_broken_actions:
        target_raw = (a.context or {}).get("target_raw", "")
        assert ".." not in target_raw, (
            f"planner emitted fix_broken_link for out-of-bundle target: {a}"
        )
