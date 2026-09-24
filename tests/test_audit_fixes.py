"""Regression tests for audit findings + security boundaries."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from okf_loom.model import Bundle
from okf_loom.render import build_graph_data, _chip_fg
from okf_loom.cli import main


def _capture(argv: list[str]) -> tuple[int, str, str]:
    import io, contextlib
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


DEMO = "samples/demo_bundle"


@pytest.fixture
def demo_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "demo"
    shutil.copytree(DEMO, dst)
    return dst


# --- Iter-5 regression: graph edge label merging (arch P1) -------------------


def test_graph_edge_label_merge_prefers_relation():
    """When body link + typed relation share (src,tgt), relation label wins."""
    b = Bundle.load(DEMO)
    data = build_graph_data(b)
    # tables/orders has both a body link AND a typed relation to tables/customers
    # The typed relation type is "references" — it should appear as the label
    for edge in data["edges"]:
        d = edge["data"]
        if d["source"] == "tables/orders" and d["target"] == "tables/customers":
            assert d.get("label") == "references", (
                f"Expected 'references' label, got {d.get('label')}"
            )
            break
    else:
        pytest.fail("Edge tables/orders → tables/customers not found")


def test_graph_all_edges_have_labels():
    """All internal edges should carry labels (body link or relation type)."""
    b = Bundle.load(DEMO)
    data = build_graph_data(b)
    for edge in data["edges"]:
        d = edge["data"]
        assert "label" in d, f"Edge {d['source']} → {d['target']} has no label"


# --- Iter-5 regression: chip contrast (critic P1) ----------------------------


def test_chip_fg_dark_on_bright_hues():
    """Yellow/cyan/green hues should get dark foreground text.

    Bundle K (P0-4): the dark fg choice is now pure black (#000000)
    rather than the softened #1a1a2e. Pure black passes WCAG AA for the
    full auto-palette hue range (0..360 at hsl(h, 62%, 48%); the soft
    black failed AA for 18 red-orange hues around L≈0.20).
    """
    assert _chip_fg("hsl(60, 62%, 48%)") == "#000000"   # yellow
    assert _chip_fg("hsl(120, 62%, 48%)") == "#000000"  # green
    assert _chip_fg("hsl(180, 62%, 48%)") == "#000000"  # cyan
    assert _chip_fg("hsl(30, 62%, 48%)") == "#000000"   # orange (threshold fix)
    # Red-orange (Table in demo) was the AA-failing case for soft black.
    assert _chip_fg("hsl(18, 62%, 48%)") == "#000000"   # Table hue


def test_chip_fg_aa_compliance_all_hues():
    """Every auto-palette hue must yield a chip fg that passes WCAG AA (>=4.5:1)."""
    def _lin(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    def _lum(r, g, b):
        return 0.2126 * _lin(r / 255) + 0.7152 * _lin(g / 255) + 0.0722 * _lin(b / 255)
    def _hsl_rgb(h, s, l):
        c = (1 - abs(2 * l - 1)) * s
        x = c * (1 - abs((h / 60) % 2 - 1))
        m = l - c / 2
        if h < 60:    r, g, b = c, x, 0
        elif h < 120: r, g, b = x, c, 0
        elif h < 180: r, g, b = 0, c, x
        elif h < 240: r, g, b = 0, x, c
        elif h < 300: r, g, b = x, 0, c
        else:         r, g, b = c, 0, x
        return (int((r + m) * 255), int((g + m) * 255), int((b + m) * 255))
    def _hex_rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    for hue in range(360):
        bg_rgb = _hsl_rgb(hue, 0.62, 0.48)
        fg_hex = _chip_fg(f"hsl({hue}, 62%, 48%)")
        fg_rgb = _hex_rgb(fg_hex)
        L1, L2 = _lum(*fg_rgb), _lum(*bg_rgb)
        if L1 < L2:
            L1, L2 = L2, L1
        ratio = (L1 + 0.05) / (L2 + 0.05)
        assert ratio >= 4.5, (
            f"hue {hue}: fg {fg_hex} on hsl({hue},62%,48%) = {ratio:.2f}:1 < 4.5:1"
        )


def test_chip_fg_light_on_dark_hues():
    """Blue/red hues should keep light foreground text."""
    assert _chip_fg("hsl(0, 62%, 48%)") == "#ffffff"    # red
    assert _chip_fg("hsl(240, 62%, 48%)") == "#ffffff"  # blue


def test_chip_fg_fallback():
    """Unparseable colors fall back to white.

    P1-7: removed the ``_chip_fg("#ff0000") == "#ffffff"`` line that
    PINS the bug — hex is now parsed (red has luminance ~0.21, so the
    higher-contrast fg is black, not white). The white fallback is
    preserved only for genuinely unparseable strings.
    """
    assert _chip_fg("invalid") == "#ffffff"
    assert _chip_fg("not-a-color") == "#ffffff"
    assert _chip_fg("") == "#ffffff"


def test_chip_fg_parses_hex():
    """P1-7: hex colors are parsed and get luminance-aware fg.

    Previously hex fell through to ``#ffffff``, producing invisible
    white-on-yellow chips for palette overrides. Now ``#rrggbb``,
    ``#rgb`` shorthand, and ``#rrggbbaa`` (alpha stripped) all parse.
    """
    assert _chip_fg("#ffff00") == "#000000"  # yellow → dark fg
    assert _chip_fg("#ffffff") == "#000000"  # white → dark fg
    assert _chip_fg("#000000") == "#ffffff"  # black → light fg
    assert _chip_fg("#ff0000") == "#000000"  # red → dark fg (was the pinned bug)
    # 3-digit shorthand.
    assert _chip_fg("#ff0") == "#000000"     # yellow shorthand
    assert _chip_fg("#000") == "#ffffff"     # black shorthand
    assert _chip_fg("#fff") == "#000000"     # white shorthand
    # 8-digit with alpha (alpha ignored — luminance is RGB-only).
    assert _chip_fg("#ffff00ff") == "#000000"
    assert _chip_fg("#000000ff") == "#ffffff"


def test_chip_fg_parses_rgb():
    """P1-7: ``rgb()``/``rgba()`` are parsed and get luminance-aware fg."""
    assert _chip_fg("rgb(255,255,0)") == "#000000"
    assert _chip_fg("rgb(255, 255, 0)") == "#000000"   # tolerant to spaces
    assert _chip_fg("rgba(255,255,0,1)") == "#000000"  # alpha ignored
    assert _chip_fg("rgb(0,0,0)") == "#ffffff"
    assert _chip_fg("rgb(255,255,255)") == "#000000"


def test_chip_fg_aa_compliance_hex_rgb():
    """P1-7: hex/rgb palette overrides must yield WCAG-AA chip fg (>=4.5:1).

    This is the regression guard for the bug class: palette overrides using
    hex/rgb previously fell through to ``#ffffff``, producing invisible
    white-on-yellow / white-on-cyan chips. The chosen fg must now pass AA
    for every bug-class (high-luminance) override.
    """
    import re as _re
    def _lin(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    def _lum(r, g, b):
        return 0.2126 * _lin(r / 255) + 0.7152 * _lin(g / 255) + 0.0722 * _lin(b / 255)
    def _hex_rgb(h):
        h = h.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    def _parse_bg(bg):
        if bg.startswith("#"):
            # Strip alpha if present before RGB lookup.
            core = bg[:7] if len(bg.lstrip("#")) >= 6 else bg
            return _hex_rgb(core)
        m = _re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", bg)
        return tuple(int(m.group(i)) for i in (1, 2, 3))

    overrides = [
        "#ffff00", "#00ff00", "#00ffff", "#ffffff",
        "#ff0", "#0ff", "#0f0",
        "#ffff0080",  # alpha form
        "rgb(255,255,0)", "rgb(0,255,255)", "rgb(255,255,255)",
        "rgba(255,255,0,0.5)",
    ]
    for bg in overrides:
        fg = _chip_fg(bg)
        bg_rgb = _parse_bg(bg)
        fg_rgb = _hex_rgb(fg)
        L1, L2 = _lum(*bg_rgb), _lum(*fg_rgb)
        if L1 < L2:
            L1, L2 = L2, L1
        ratio = (L1 + 0.05) / (L2 + 0.05)
        assert ratio >= 4.5, (
            f"bg {bg} → fg {fg} = {ratio:.2f}:1 < 4.5:1"
        )


# --- Security: write-concept path traversal (security Finding 1) -------------


@pytest.mark.parametrize("malicious_id", [
    "../etc/passwd",
    "../../etc/passwd",
    "tables/../../etc/passwd",
    "..",
    ".",
    "",
])
def test_write_concept_rejects_traversal(demo_copy: Path, malicious_id: str) -> None:
    """write-concept rejects path-traversal --id values."""
    rc, _, err = _capture([
        "write-concept", "--bundle", str(demo_copy),
        "--id", malicious_id, "--type", "Table",
        "--body", "malicious",
    ])
    assert rc != 0


# --- Security: write-concept reserved filename guard (security Finding 1) ----


@pytest.mark.parametrize("reserved_id", ["index", "log", "tables/index"])
def test_write_concept_rejects_reserved(demo_copy: Path, reserved_id: str) -> None:
    """write-concept refuses to overwrite reserved filenames (index.md, log.md)."""
    rc, _, err = _capture([
        "write-concept", "--bundle", str(demo_copy),
        "--id", reserved_id, "--type", "Table",
        "--body", "should fail", "--force",
    ])
    assert rc != 0
    assert "reserved" in err.lower()


# --- Architecture: _actions_to_ops terminal else (arch P1) ------------------


def test_actions_to_ops_no_silent_drop():
    """Unhandled verbs with argv are reported in skipped, not silently dropped."""
    from okf_loom.update import _actions_to_ops
    actions = [{
        "action": "unknown_verb",
        "why": "test",
        "evidence": "test",
        "concept_id": "tables/orders",
        "argv": ["okf", "unknown", "--source", "tables/orders"],
    }]
    ops, skipped = _actions_to_ops(actions)
    assert len(ops) == 0
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "unhandled_verb"


def test_actions_to_ops_neither_argv_nor_template():
    """Actions with neither argv nor argv_template are reported as skipped (iter-8 fix)."""
    from okf_loom.update import _actions_to_ops
    actions = [{
        "action": "add_link",
        "why": "test",
        "evidence": "test",
        "concept_id": "tables/orders",
    }]
    ops, skipped = _actions_to_ops(actions)
    assert len(ops) == 0
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "no_argv_or_template"


# --- Architecture: cmd_repair default shows all (user Finding 2) -------------


def test_repair_default_shows_all_mechanical(demo_copy: Path) -> None:
    """repair with no flags shows all mechanical fixes (not 0)."""
    rc, out, _ = _capture(["repair", str(demo_copy)])
    assert rc == 0
    # Should show the mirror_relation actions that exist in the demo bundle
    assert "mirror_relation" in out or "mechanical fix" in out.lower()


# --- Iter2: Relation search filters typed-relation only (quality P1) --------


def test_relation_search_filters_typed_only():
    """--relation TYPE matches only typed-relation edges, not markdown link labels."""
    from okf_loom.search import search_bundle, SearchMode
    b = Bundle.load(DEMO)
    # tables/orders has body links to tables/customers with label "references"
    # AND a typed relation references to tables/customers.
    # Searching --relation references should return only typed-relation matches.
    r = search_bundle(b, "", mode=SearchMode.RELATION, relation="references", limit=10)
    # Should find tables/orders (which has typed relation references → tables/customers)
    assert len(r) > 0
    for hit in r:
        assert hit.detail is not None
        assert "edges" in hit.detail
        for edge in hit.detail["edges"]:
            assert edge["via"] == "relations"


def test_relation_search_has_detail_and_snippets():
    """Relation search results carry edge detail and readable snippets."""
    from okf_loom.search import search_bundle, SearchMode
    b = Bundle.load(DEMO)
    r = search_bundle(b, "", mode=SearchMode.RELATION, relation="written_by", limit=5)
    assert len(r) > 0
    hit = r[0]
    assert hit.detail is not None
    assert "edges" in hit.detail
    assert len(hit.snippets) > 0
    assert "written_by" in hit.snippets[0]


# --- Iter2: Unmirrored relations concept-id resolution (quality P1) ---------


def test_unmirrored_relations_no_false_negative():
    """Substring matching fixed: tables/users ≠ tables/users_v2."""
    from okf_loom.plan import _find_unmirrored_relations
    import tempfile, shutil, os
    # Create a minimal bundle with a relation target that shares a prefix
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "bundle"
        shutil.copytree(DEMO, dst)
        # Add a concept tables/users_v2
        (dst / "tables" / "users_v2.md").write_text(
            "---\ntype: Table\ntitle: Users V2\n---\nTest\n", encoding="utf-8"
        )
        # Add a body link to users_v2 in orders
        orders = (dst / "tables" / "orders.md").read_text()
        orders += "\n* [users v2](/tables/users_v2.md)\n"
        (dst / "tables" / "orders.md").write_text(orders)
        # Now orders has a relation to tables/customers but NO body link to customers
        # (the existing body links are to other targets)
        b = Bundle.load(dst)
        results = _find_unmirrored_relations(b)
        # tables/orders should still have unmirrored relations
        order_results = [r for r in results if r[0] == ("tables", "orders")]
        assert len(order_results) > 0


# --- Iter2: Dry-run invalidates caches (quality P1) -------------------------


def test_dry_run_invalidates_caches(demo_copy: Path):
    """apply_plan(dry_run=True) invalidates caches so bundle.graph() is fresh.

    Previously ``assert edges_after >= edges_before`` passed even when the
    cache was STALE: unchanged edge counts are trivially ``>=`` the before
    count, so a regression that SKIPPED invalidation (serving the old cached
    graph) would still pass. Now we prove invalidation three ways:

      1. Negative control — the SPECIFIC edge the mutation adds is ABSENT from
         the pre-mutation graph (otherwise the test cannot tell fresh from
         stale).
      2. Positive proof — that exact edge IS present in the post-dry-run graph.
      3. Strict cardinality — the edge count grew by EXACTLY one (the single
         link added), not merely ``>=``.
    """
    from okf_loom.update import load_plan, apply_plan

    def _edge_pairs(graph):
        return {
            (tuple(edge.source), tuple(edge.target))
            for edge in graph.edges
            if edge.target is not None
        }

    new_edge = (("tables", "customers"), ("datasets", "orders"))

    b = Bundle.load(demo_copy)
    graph_before = b.graph()
    edges_before = len(graph_before.edges)
    before_pairs = _edge_pairs(graph_before)
    # Negative control: the link we are about to add must NOT already exist,
    # otherwise the post-condition cannot distinguish a fresh graph from a
    # stale one.
    assert new_edge not in before_pairs, (
        f"precondition failed: {new_edge} already in the pre-mutation graph; "
        f"pick a fresh (source, target) pair"
    )

    plan_data = {
        "bundle_root": str(demo_copy),
        "ops": [{
            "kind": "add_link",
            "target": "tables/customers",
            "args": {
                "label": "test",
                "target_concept_id": "datasets/orders",
                "section": None,
            },
        }],
    }
    plan_path = demo_copy / "test-plan.json"
    plan_path.write_text(json.dumps(plan_data))
    plan = load_plan(plan_path)
    apply_plan(b, plan, apply=False, dry_run=True)

    graph_after = b.graph()
    edges_after = len(graph_after.edges)
    after_pairs = _edge_pairs(graph_after)

    # (2) The mutation's specific new edge must now be present. If the cache
    # were stale this would still be missing.
    assert new_edge in after_pairs, (
        f"dry-run mutation not reflected in graph(): {new_edge} is absent — "
        f"the derived graph cache was NOT invalidated"
    )
    # (3) Strict +1: exactly the one link we added, no more, no less.
    assert edges_after == edges_before + 1, (
        f"expected exactly +1 edge from the single add_link, "
        f"got {edges_before} -> {edges_after}"
    )


# --- Iter2: Denylist catches credential files (security P1) -----------------


@pytest.mark.parametrize("cred_file", [
    ".netrc", ".npmrc", ".pypirc", ".git-credentials",
    "test.kdbx", "key.ppk", "config.ovpn",
])
def test_denylist_catches_credential_files(cred_file: str):
    """Skill-archive denylist catches common credential file types.

    Previously this test did ``sys.path.insert(0, "scripts")`` before importing
    ``build_skill_archive``: that is cwd-dependent (breaks when pytest is invoked
    from another directory) AND pollutes the global importer for every later
    test in the session. Now the script module is loaded via ``importlib``
    against its absolute repo-relative path, with no ``sys.path`` side effects.
    """
    import importlib.util
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    spec = importlib.util.spec_from_file_location(
        "_build_skill_archive_under_test", scripts_dir / "build_skill_archive.py"
    )
    assert spec is not None and spec.loader is not None, (
        f"could not build importlib spec for {scripts_dir / 'build_skill_archive.py'}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.is_path_denied(cred_file), f"{cred_file} should be denied"


# --- Iter2: _h_set_tag same_value (quality P2) ------------------------------


def test_set_tag_same_value_skips():
    """_h_set_tag returns same_value when value is identical."""
    from okf_loom.update import _h_set_tag
    from okf_loom.model import Concept
    from okf_loom.paths import concept_id_from_str
    from pathlib import Path
    c = Concept(
        id=concept_id_from_str("test/x"),
        frontmatter={"type": "Table", "tags": ["alpha"]},
        body="test",
        path=Path("/tmp/test.md"),
        rel_path=Path("test.md"),
        raw_text="",
        headings=[],
    )
    applied, reason, mutated = _h_set_tag(None, c, {"tag": "alpha"})
    assert not applied
    assert reason == "same_value"


# --- Iter3: P0 fix — okf plan works with discovery suggestions ---------------


def test_plan_works_with_discovery_suggestions(tmp_path: Path):
    """build_plan doesn't crash on bundles with unlinked_mentions (iter-3 P0)."""
    import shutil
    from okf_loom.plan import build_plan
    from okf_loom.model import Bundle

    dst = tmp_path / "bundle"
    shutil.copytree(DEMO, dst)
    # Add a concept that mentions another without linking (triggers unlinked_mentions)
    (dst / "tables" / "test_mentions.md").write_text(
        "---\ntype: Table\ntitle: Test Mentions\n---\n"
        "This concept mentions the orders table without linking it.\n",
        encoding="utf-8",
    )
    b = Bundle.load(dst)
    plan = build_plan(b)
    # Should produce actions without NameError
    assert len(plan.actions) > 0
    # P2-9: argv embeds the REAL (absolute) bundle_root so it is runnable
    # as-is from any CWD (spec §6.1 "executable, shell:false — runnable as-is").
    for action in plan.actions:
        if action.argv:
            assert any(
                str(dst) in arg or str(dst.resolve()) in arg
                for arg in action.argv
            ), f"Action {action.action} argv should reference the bundle root: {action.argv}"


def test_plan_portable_all_actions(tmp_path: Path):
    """P2-9: all plan actions embed the real bundle_root so argv is runnable
    from any CWD. (Replaces the prior 'no absolute paths' contract — the spec
    explicitly favours 'runnable as-is' over portable-by-chdir.)
    """
    import shutil, subprocess, sys, os
    from okf_loom.plan import build_plan
    from okf_loom.model import Bundle

    dst = tmp_path / "bundle"
    shutil.copytree(DEMO, dst)
    (dst / "tables" / "test_mentions.md").write_text(
        "---\ntype: Table\ntitle: Test\n---\nMentions orders.\n",
        encoding="utf-8",
    )
    b = Bundle.load(dst)
    plan = build_plan(b)
    resolved = str(dst.resolve())
    for action in plan.actions:
        if action.argv:
            # Every argv must reference the bundle root (absolute path)
            assert resolved in action.argv, (
                f"Action {action.action} argv missing bundle root: {action.argv}"
            )


# --- Iter4: P0 fix — okf plan create_index branch (NameError on bundle_root) ---


def test_plan_create_index_no_crash(tmp_path: Path):
    """build_plan doesn't crash when missing_indexes fires (create_index branch)."""
    import shutil
    from okf_loom.plan import build_plan
    from okf_loom.model import Bundle

    dst = tmp_path / "bundle"
    shutil.copytree(DEMO, dst)
    # Add a subdirectory with a concept but NO index.md
    (dst / "newdir").mkdir()
    (dst / "newdir" / "thing.md").write_text(
        "---\ntype: Table\ntitle: Thing\n---\nA concept without an index.\n",
        encoding="utf-8",
    )
    b = Bundle.load(dst)
    # This used to crash with NameError: bundle_root
    plan = build_plan(b)
    # Should produce a create_index action
    create_actions = [a for a in plan.actions if a.action == "create_index"]
    assert len(create_actions) > 0
    # P1-15 + P2-9: command uses positional bundle (no --bundle flag) and the
    # real bundle root. Replaces the prior `"--bundle ." in command` contract.
    resolved = str(dst.resolve())
    for a in create_actions:
        assert a.command == f"okf index {resolved}", (
            f"create_index command not positional+absolute: {a.command}"
        )
        assert a.argv == ["okf", "index", resolved], (
            f"create_index argv not positional+absolute: {a.argv}"
        )


# --- Iter4: Config wiring (P1, §11.1) ----------------------------------------


def test_config_wiring_search_default_mode(tmp_path: Path):
    """okf-loom.config.yaml search.default_mode affects CLI search behavior."""
    import shutil, json
    dst = tmp_path / "bundle"
    shutil.copytree(DEMO, dst)
    # Write a config that sets semantic as default
    (dst / "okf-loom.config.yaml").write_text(
        "search:\n  default_mode: semantic\n", encoding="utf-8"
    )
    rc, out, _ = _capture([
        "search", str(dst), "orders", "--limit", "3", "--format", "json",
    ])
    assert rc == 0
    results = json.loads(out)
    assert len(results) > 0
    # Should use semantic-lite backend (not lexical)
    assert results[0]["source_backend"] == "semantic-lite"


def test_config_wiring_validate_default_profile(tmp_path: Path):
    """okf-loom.config.yaml validate.default_profile affects CLI validate behavior."""
    import shutil
    dst = tmp_path / "bundle"
    shutil.copytree(DEMO, dst)
    # Write a config that sets producer profile
    (dst / "okf-loom.config.yaml").write_text(
        "validate:\n  default_profile: producer\n", encoding="utf-8"
    )
    rc, out, _ = _capture(["validate", str(dst), "--format", "json"])
    assert rc == 0
    import json
    d = json.loads(out)
    assert d["profile"] == "producer"


def test_config_cli_flag_overrides_config(tmp_path: Path):
    """CLI flag wins over config value."""
    import shutil, json
    dst = tmp_path / "bundle"
    shutil.copytree(DEMO, dst)
    (dst / "okf-loom.config.yaml").write_text(
        "search:\n  default_mode: semantic\n", encoding="utf-8"
    )
    # Explicit --mode lexical should override config's semantic
    rc, out, _ = _capture([
        "search", str(dst), "orders", "--mode", "lexical", "--limit", "3", "--format", "json",
    ])
    assert rc == 0
    results = json.loads(out)
    assert len(results) > 0
    # Should use lexical backend, not semantic
    assert results[0]["source_backend"] == "lexical"


# --- Iter5: add_link label key mismatch (quality P1) -------------------------


def test_plan_add_link_uses_title_not_id(tmp_path: Path):
    """Planned add_link actions use the target's title, not lowercased id segment."""
    import shutil
    from okf_loom.plan import build_plan
    from okf_loom.model import Bundle

    dst = tmp_path / "bundle"
    shutil.copytree(DEMO, dst)
    # Add a concept that unambiguously mentions "customers" (unique title)
    (dst / "tables" / "sales_tracker.md").write_text(
        "---\ntype: Table\ntitle: Sales Tracker\n---\n"
        "The sales tracker references the customers table.\n",
        encoding="utf-8",
    )
    b = Bundle.load(dst)
    plan = build_plan(b)
    # Find the add_link action
    add_link_actions = [a for a in plan.actions if a.action == "add_link"]
    assert len(add_link_actions) > 0, "Expected at least one add_link action"
    # The label should be "Customers" (the title), not "customers" (id segment)
    action = add_link_actions[0]
    assert "--label" in action.argv
    label_idx = action.argv.index("--label") + 1
    label = action.argv[label_idx]
    # Title is "Customers" with capital C, not lowercase "customers"
    assert label == "Customers", f"Expected 'Customers', got '{label}'"


# --- Iter-10 P1-16: repair --apply actually applies mechanical add_link -----


def test_repair_apply_applies_mechanical_add_link(tmp_path: Path) -> None:
    """P1-16: ``repair --all --apply`` applies mechanical add_link actions
    (from unlinked_mentions) and is idempotent on re-run."""
    import shutil
    from okf_loom.model import Bundle

    dst = tmp_path / "bundle"
    shutil.copytree(DEMO, dst)
    # Build a bundle where `alpha.md` mentions "customers" (a unique-title
    # phrase >= 3 chars so unlinked_mentions fires) without linking it.
    # NOTE: the spec sketch said "see b" + b.md, but the unlinked_mentions
    # rule drops phrases < 3 chars (too noisy); "customers" reliably triggers
    # the rule against the existing demo concept.
    (dst / "alpha.md").write_text(
        "---\ntype: Note\ntitle: Alpha\n---\n"
        "Alpha references the customers table.\n",
        encoding="utf-8",
    )

    # Dry-run: must show an add_link action for alpha → customers
    rc, out, _ = _capture(["repair", str(dst), "--all"])
    assert rc == 0
    assert "add_link" in out, f"dry-run should show add_link; got:\n{out}"

    # Apply
    rc, out, _ = _capture(["repair", str(dst), "--all", "--apply"])
    assert rc == 0

    # After apply: alpha.md body MUST contain a markdown link to customers
    alpha_body = (dst / "alpha.md").read_text(encoding="utf-8")
    assert "customers.md" in alpha_body or "](customers" in alpha_body, (
        f"alpha.md should now link to customers; body was:\n{alpha_body}"
    )

    # Idempotent: re-running --apply reports 0 mutations applied.
    rc2, out2, _ = _capture(["repair", str(dst), "--all", "--apply", "--format", "json"])
    assert rc2 == 0
    import json
    data = json.loads(out2)
    assert data["mutations_applied"] == 0, (
        f"re-apply should be idempotent (mutations_applied:0); got: {data}"
    )


# --- Iter-10 P1-17: repair dry-run filters NON-mechanical template actions --


def test_repair_dry_run_filters_non_mechanical(tmp_path: Path) -> None:
    """P1-17: ``repair --all`` dry-run shows ONLY mechanical actions.

    A messy bundle (unlinked mention + missing description + orphan) must
    NOT leak agent-instruction templates (add_description, connect,
    fix_broken_link, add_relation) into the dry-run preview.
    """
    import shutil

    dst = tmp_path / "bundle"
    shutil.copytree(DEMO, dst)
    # Messy bundle:
    #  - tables/orphan.md: no links at all (orphan_concepts → connect template)
    #  - tables/nodesc.md: missing description (missing_descriptions → add_description template)
    (dst / "tables" / "orphan.md").write_text(
        "---\ntype: Table\ntitle: Orphan\n---\nstandalone\n", encoding="utf-8"
    )
    (dst / "tables" / "nodesc.md").write_text(
        "---\ntype: Table\n---\nno description here\n", encoding="utf-8"
    )

    rc, out, _ = _capture(["repair", str(dst), "--all"])
    assert rc == 0
    # NON-mechanical template verbs MUST NOT appear in dry-run output.
    for forbidden in ("add_description", "connect", "fix_broken_link", "add_relation"):
        assert forbidden not in out, (
            f"dry-run leaked non-mechanical template action '{forbidden}':\n{out}"
        )
    # Mechanical verbs are allowed.
    allowed_mechanical = ("add_link", "create_index", "refresh_index", "mirror_relation")
    # At least one mechanical action should be present (mirror_relation from demo).
    assert any(v in out for v in allowed_mechanical), (
        f"dry-run should still show mechanical actions:\n{out}"
    )


def test_repair_apply_path_uses_same_mechanical_filter(tmp_path: Path) -> None:
    """P1-17 corollary: the apply path uses the SAME mechanical-only filter as
    the dry-run, so a non-mechanical template action can never be applied.

    Previously the body loop was ``for _op, res in ...: pass`` (dead code that
    inspected nothing) and the only real assertion read the *dry-run* JSON.
    That proved the dry-run filter worked, not the apply path. Now we parse the
    ACTUAL ``apply_result["results"]`` and prove:

      * no template-only verb (connect / fix_broken_link / add_description /
        add_relation) ever reports ``applied: True``; and
      * a mechanical ``add_link`` (the body-link op derived from
        mirror_relation) DID apply — the positive proof that the apply path
        runs real mechanical ops rather than silently no-op'ing everything.

    (``add_relation`` is in the forbidden-applied set even though mirror_relation
    *emits* an add_relation op: that op is idempotent — the relation already
    lives in frontmatter — so it reports ``applied: False``; if it ever applied
    we would suspect a template leak.)
    """
    import shutil

    dst = tmp_path / "bundle"
    shutil.copytree(DEMO, dst)
    (dst / "tables" / "orphan.md").write_text(
        "---\ntype: Table\ntitle: Orphan\n---\nstandalone\n", encoding="utf-8"
    )
    # Apply path; capture JSON to inspect what was ACTUALLY applied.
    rc, out, _ = _capture([
        "repair", str(dst), "--all", "--apply", "--format", "json",
    ])
    assert rc == 0
    data = json.loads(out)

    apply_result = data.get("apply_result") or {}
    results = apply_result.get("results", [])
    assert results, (
        "expected at least one mechanical op (demo has unmirrored relations); "
        f"apply_result={apply_result!r}"
    )

    template_verbs = {
        "connect", "fix_broken_link", "add_description", "add_relation",
    }
    applied_kinds = {
        op["kind"] for op, res in results if res.get("applied")
    }
    leaked = applied_kinds & template_verbs
    assert not leaked, (
        f"template-only verbs leaked into the apply path as applied: {leaked}"
    )

    # Positive proof: a mechanical add_link (mirror_relation's body-link op)
    # MUST have applied. Without this, an apply path that silently no-ops every
    # op would still satisfy the "no template verb applied" check above.
    assert "add_link" in applied_kinds, (
        f"expected a mechanical add_link to apply; applied_kinds={applied_kinds}"
    )


# ---------------------------------------------------------------------------
# Bundle integration: cli.py wiring for capabilities
# wikilink activation via CLI, validate profile key from as_dict. These lock
# the cross-bundle wiring that the library tests alone cannot reach (the CLI
# is the primary user surface).
# ---------------------------------------------------------------------------


def test_cli_capabilities_shows_wikilinks_active(demo_copy: Path) -> None:
    """P0-2c: `okf capabilities --bundle` reflects body-syntax auto-activation
    (okf.cap.wikilinks) now that cmd_capabilities uses Bundle.capabilities()."""
    rc, out, _ = _capture([
        "capabilities", "--bundle", str(demo_copy), "--format", "json",
    ])
    assert rc == 0
    data = json.loads(out)
    assert "okf.cap.wikilinks" in data["active"], (
        "wikilinks capability must be active via CLI for a bundle with [[...]] "
        f"(active={data['active']})"
    )


def test_cli_validate_json_profile_key_from_as_dict(demo_copy: Path) -> None:
    """P1-12: `okf validate --format json` carries `profile` from
    ValidationReport.as_dict() (not a CLI post-hoc injection)."""
    rc, out, _ = _capture(["validate", str(demo_copy), "--format", "json"])
    assert rc == 0
    data = json.loads(out)
    assert "profile" in data and data["profile"] == "spec"


def test_cli_validate_checks_branch_reports_spec_profile(demo_copy: Path) -> None:
    """P2-6: under --checks the profile remap does not run, so the JSON
    truthfully reports `profile: spec` rather than claiming the requested
    profile (regardless of whether the checks themselves pass)."""
    rc, out, _ = _capture([
        "validate", str(demo_copy), "--profile", "producer",
        "--checks", "link_integrity", "--format", "json",
    ])
    # rc may be 0 or 1 depending on link state; the contract is the profile
    # field honesty, not the exit code.
    data = json.loads(out)
    assert data["profile"] == "spec", (
        "--checks bypasses profile remap; JSON must honestly report spec, "
        f"not {data['profile']!r}"
    )


# ---------------------------------------------------------------------------
# Small fixes: cmd_info governed-key counts (P1-20),
# citations frontmatter/body collision INFO finding (P1-39 companion).
# ---------------------------------------------------------------------------


def test_cli_info_reports_governed_key_counts(demo_copy: Path) -> None:
    """P1-20 (SPEC §7 L490): `okf info` counts concepts carrying each governed
    optional key (aliases/entities/provenance/citations/relations)."""
    rc, out, _ = _capture(["info", str(demo_copy), "--format", "json"])
    assert rc == 0
    data = json.loads(out)
    assert "governed_key_counts" in data, "info JSON must include governed_key_counts"
    gkc = data["governed_key_counts"]
    # demo_bundle exercises aliases, entities (object form), provenance,
    # citations (added iter-10), relations, and a wikilink.
    assert gkc["aliases"] >= 1, f"aliases count too low: {gkc}"
    assert gkc["citations"] >= 1, f"citations count too low: {gkc}"
    assert gkc["relations"] >= 1, f"relations count too low: {gkc}"
    assert "active_capabilities" in data


def test_validate_emits_citations_collision_info(tmp_path: Path) -> None:
    """P1-39 companion: when a concept has BOTH a frontmatter `citations:` key
    AND a body `# Citations` heading, validate emits an INFO finding."""
    from okf_loom import Bundle
    from okf_loom.validate import validate_bundle
    bundle_dir = tmp_path / "kb"
    bundle_dir.mkdir()
    (bundle_dir / "index.md").write_text("okf_version: '0.1'\n", encoding="utf-8")
    (bundle_dir / "tables/orders.md").parent.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "tables/orders.md").write_text(
        "---\n"
        "type: Table\n"
        "title: Orders\n"
        "citations:\n"
        "  - id: '1'\n"
        "    text: Internal\n"
        "---\n"
        "# Orders\n\n"
        "Body.\n\n"
        "# Citations\n\n"
        "- [1] Internal\n",
        encoding="utf-8",
    )
    bundle = Bundle.load(bundle_dir)
    report = validate_bundle(bundle)
    codes = [f.code for f in report.findings]
    assert "concept.citations_both_forms" in codes, (
        f"expected citations-both-forms INFO; got codes={codes}"
    )


def test_iter4_p1_2_typed_scalar_preserves_comments(tmp_path):
    """P1-2: a typed-scalar (int/bool/float) frontmatter change must preserve
    comments. iter-3's _format_scalar did str(value) → YAML string → gate
    rejected → safe_dump → ALL comments destroyed."""
    from okf_loom import Bundle
    from okf_loom.update import UpdateOp, UpdatePlan, apply_plan
    (tmp_path / "index.md").write_text("okf_version: '0.1'\n", encoding="utf-8")
    (tmp_path / "c.md").write_text(
        "---\ntype: T\n# keeper\ncount: 1\ntags: [a, b]\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    plan = UpdatePlan(
        bundle_root=str(tmp_path), description="t",
        ops=[UpdateOp(kind="set_frontmatter", target=("c",), args={"key": "count", "value": 2})],
        plan_kind="single-op",
    )
    apply_plan(b, plan, apply=True)
    content = (tmp_path / "c.md").read_text(encoding="utf-8")
    assert "# keeper" in content, f"comment destroyed:\n{content}"
    # The value must remain int (not stringified to '2')
    b2 = Bundle.load(tmp_path)
    assert b2.concepts[("c",)].frontmatter["count"] == 2, "count not int"


def test_iter4_p1_8_render_warns_on_active_code(demo_copy):
    """P1-8/P2-15: okf render --allow-active-code on a bundle with
    allow_active_code:true must print the WARNING (parity with serve/build).
    This test was MISSING — mutation-proven zero coverage."""
    import json
    (demo_copy / "okf-loom.config.yaml").write_text(
        "viewer:\n  allow_active_code: true\n", encoding="utf-8"
    )
    rc, out, err = _capture([
        "render", str(demo_copy), "--out", str(demo_copy / "viz.html"),
        "--allow-active-code",
    ])
    assert rc == 0
    assert "WARNING: active code" in err, f"warning missing: {err!r}"


def test_iter4_p1_8_viewer_title_null_stays_none(tmp_path):
    """P1-8/P2-11: okf-loom.config.yaml with `title: null` must produce
    cfg.viewer.title is None (not the literal string 'None'). Mutation-proven
    zero coverage."""
    import shutil
    shutil.copytree("samples/demo_bundle", tmp_path / "demo", dirs_exist_ok=True)
    (tmp_path / "demo" / "okf-loom.config.yaml").write_text(
        "viewer:\n  title: null\n", encoding="utf-8"
    )
    from okf_loom.config import OkfConfig
    cfg = OkfConfig.load(tmp_path / "demo")
    assert cfg.viewer.title is None, f"title is {cfg.viewer.title!r}, expected None"


def test_iter4_p1_8_non_iso_log_heading_flagged(tmp_path):
    """P1-8/P2-12: a log.md with `## Notes` must produce a log.bad_date_heading
    finding. Mutation-proven zero coverage."""
    from okf_loom import Bundle
    from okf_loom.validate import validate_bundle
    (tmp_path / "index.md").write_text("okf_version: '0.1'\n", encoding="utf-8")
    (tmp_path / "log.md").write_text(
        "## 2024-01-01\n- real\n\n## Notes\n- not a date\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    report = validate_bundle(b)
    codes = [f.code for f in report.findings]
    assert "log.bad_date_heading" in codes, f"bad_date_heading not live: {codes}"
    # P1-6: non-ISO heading must NOT produce spurious not_newest_first
    assert "log.not_newest_first" not in codes, f"spurious ordering: {codes}"


def test_iter5_p1_2_inline_comment_preserved(tmp_path):
    """P1-2: editing a scalar value must preserve the inline comment on the
    same line (e.g. ``count: 1  # KEEP ME`` → ``count: 2  # KEEP ME``)."""
    from okf_loom import Bundle
    from okf_loom.update import UpdateOp, UpdatePlan, apply_plan
    (tmp_path / "index.md").write_text("okf_version: '0.1'\n", encoding="utf-8")
    (tmp_path / "c.md").write_text(
        "---\ntype: T\ncount: 1  # KEEP ME\ntitle: X\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    plan = UpdatePlan(
        bundle_root=str(tmp_path), description="t",
        ops=[UpdateOp(kind="set_frontmatter", target=("c",), args={"key": "count", "value": 2})],
        plan_kind="single-op",
    )
    apply_plan(b, plan, apply=True)
    content = (tmp_path / "c.md").read_text(encoding="utf-8")
    assert "# KEEP ME" in content, f"inline comment destroyed:\n{content}"
    assert "count: 2" in content


def test_iter5_p1_4_fenced_heading_not_in_log_entries(tmp_path):
    """P1-4/P1-7: a ``## heading`` inside a fenced code block in log.md must
    NOT be parsed as a log entry. Mutation-proven zero coverage."""
    from okf_loom.model import _parse_log
    raw = (
        "## 2024-01-01\n- real\n\n"
        "```\n## FAKE_IN_FENCE\n- fake\n```\n\n"
        "## 2024-01-02\n- real2\n"
    )
    entries = _parse_log(raw)
    dates = [e.date for e in entries]
    assert "FAKE_IN_FENCE" not in dates, f"fenced heading leaked: {dates}"
    assert len(entries) == 2, f"expected 2 entries, got {len(entries)}: {dates}"


def test_iter5_p2_6_spa_concept_nav_root_absolute():
    """P2-6/P1-3: nested-concept nav in spa/serve must be root-absolute
    (start with '/'). Mutation-proven: reverting _root_prefix_for to ''
    keeps suite green without this test."""
    from okf_loom import Bundle
    from okf_loom.render import _render_concept_page, _root_prefix_for
    # Verify _root_prefix_for returns '/' for serve/spa
    assert _root_prefix_for("spa", ("tables", "orders")) == "/"
    assert _root_prefix_for("serve", ("tables", "orders")) == "/"


# ===========================================================================
# iter-6: regression tests for iter-5 fixes that had ZERO coverage
# (mutation-proven by both cross-cutting auditors).
# ===========================================================================


def test_iter6_body_size_cap_rejects_oversize(tmp_path):
    """P1-1: Bundle.load must skip concepts with bodies > 1 MiB
    (concept.body_too_large warning). Mutation-proven zero coverage."""
    from okf_loom import Bundle
    (tmp_path / "index.md").write_text("okf_version: '0.1'\n", encoding="utf-8")
    (tmp_path / "big.md").write_text(
        f"---\ntype: T\n---\n{'x' * 2_000_000}\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    assert ("big",) not in b.concepts, "oversize concept should be skipped"
    codes = [w.code for w in b.warnings]
    assert "concept.body_too_large" in codes, f"warning missing: {codes}"


def test_iter6_inline_comment_on_value_line_preserved(tmp_path):
    """P1-2: inline comment on the SAME line as the edited scalar value must
    survive. Mutation-proven: the existing test only covers standalone-line
    comments, not inline."""
    from okf_loom import Bundle
    from okf_loom.update import UpdateOp, UpdatePlan, apply_plan
    (tmp_path / "index.md").write_text("okf_version: '0.1'\n", encoding="utf-8")
    (tmp_path / "c.md").write_text(
        "---\ntype: T\ncount: 1  # inline keeper\n---\nbody\n",
        encoding="utf-8",
    )
    b = Bundle.load(tmp_path)
    plan = UpdatePlan(
        bundle_root=str(tmp_path), description="t",
        ops=[UpdateOp(kind="set_frontmatter", target=("c",),
                      args={"key": "count", "value": 42})],
        plan_kind="single-op",
    )
    apply_plan(b, plan, apply=True)
    content = (tmp_path / "c.md").read_text(encoding="utf-8")
    assert "# inline keeper" in content, f"inline comment destroyed:\n{content}"
    assert "count: 42" in content


def test_iter6_subdir_index_nav_root_absolute_spa():
    """P1-3: subdir index page nav must be root-absolute in spa mode.
    Mutation-proven: _render_index_page has its own root_prefix_for_nav,
    independent of _root_prefix_for (which the existing test covers)."""
    import shutil, tempfile
    from okf_loom import Bundle
    from okf_loom.render import build_site
    d = tempfile.mkdtemp()
    shutil.copytree("samples/demo_bundle", d, dirs_exist_ok=True)
    b = Bundle.load(d)
    out = tempfile.mkdtemp()
    build_site(b, out, target="spa")
    # Check a subdir index page has root-absolute nav.
    tables_index = Path(out) / "tables" / "index.html"
    if tables_index.is_file():
        html = tables_index.read_text(encoding="utf-8")
        # iter-7: EXACT assertion — the bare substring "/__search" matches
        # both "/__search" (correct) AND "../__search" (buggy), so the old
        # assertion was a tautology. Assert the EXACT root-absolute form.
        assert 'action="/__search"' in html, (
            f"subdir index search action not root-absolute:\n{html[:500]}"
        )
        # Negative: must NOT have the buggy relative form.
        assert 'action="../__search"' not in html, "subdir-relative nav (buggy)"


def test_iter6_build_warning_not_duplicated(tmp_path):
    """P2-2 (iter-6): build_site must print the active-code WARNING exactly
    ONCE (not twice). iter-5's refactor left the original block in place,
    causing duplicate warnings on spa/static builds."""
    import shutil
    from okf_loom import Bundle
    from okf_loom.render import build_site
    shutil.copytree("samples/demo_bundle", tmp_path / "demo", dirs_exist_ok=True)
    (tmp_path / "demo" / "okf-loom.config.yaml").write_text(
        "viewer:\n  allow_active_code: true\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path / "demo")
    import io, contextlib
    err_buf = io.StringIO()
    with contextlib.redirect_stderr(err_buf):
        build_site(b, tmp_path / "out", target="spa", allow_active_code=True)
    warnings = err_buf.getvalue().count("WARNING: active code")
    assert warnings == 1, f"expected 1 WARNING, got {warnings}: {err_buf.getvalue()!r}"


# ===========================================================================
# Iter-3 user-outcome fixes: P2-5 (snippet cap), P2-6 (forward reference),
# P2-7 (--checks dotted codes).
# ===========================================================================


def test_p2_5_relation_text_mode_shows_all_edge_snippets(demo_copy: Path) -> None:
    """P2-5: relation text mode must show ALL edge snippets, not cap at 2.

    For relation/entity modes the snippets ARE the result data (matched
    edges / matched fields). The old ``snippets[:2]`` CLI cap silently
    dropped everything after the second edge.
    """
    rc, out, err = _capture([
        "search", str(demo_copy), "--mode", "relation",
        "--source", "services/checkout",
    ])
    assert rc == 0, f"relation search failed: {err}"
    # services/checkout has 3 relation/link edges; the 3rd has type
    # 'writes_to'. The old cap dropped it; the fix must show it.
    assert "writes_to" in out, (
        f"3rd relation edge was truncated in text mode (cap bug):\n{out}"
    )


def test_p2_5_lexical_text_mode_shows_more_footer(
    monkeypatch, tiny_good_bundle: Path
) -> None:
    """P2-5: when a non-relation/entity result carries >2 snippets, text mode
    prints a ``+N more`` footer so truncation is visible (instead of silently
    dropping the rest)."""
    import okf_loom.search as search_mod
    from okf_loom.paths import concept_id_from_str
    from okf_loom.search import SearchResult

    fake = SearchResult(
        concept_id=concept_id_from_str("tables/users"),
        title="Users",
        score=1.0,
        snippets=["snip one", "snip two", "snip three", "snip four"],
        source_backend="lexical",
    )
    monkeypatch.setattr(search_mod, "search_bundle", lambda *a, **k: [fake])
    rc, out, err = _capture(["search", str(tiny_good_bundle), "users"])
    assert rc == 0, f"search failed: {err}"
    assert "+2 more" in out, (
        f"expected '+2 more' footer for 4-snippet result:\n{out}"
    )


def test_p2_7_validate_checks_accepts_dotted_finding_code(
    tiny_bad_bundle: Path,
) -> None:
    """P2-7: --checks accepts user-facing dotted finding codes (link.broken,
    concept.missing_type, ...) that users see in validate output, mapping each
    to the category check that produces it. Internal category names still
    work."""
    rc, out, err = _capture([
        "validate", str(tiny_bad_bundle), "--checks", "link.broken",
        "--format", "json",
    ])
    # Must be ACCEPTED (the old behavior rejected with "Unknown check names").
    assert "Unknown check names" not in err, (
        f"dotted code link.broken must be accepted, not rejected:\n{err}"
    )
    data = json.loads(out)
    codes = {f["code"] for f in data["findings"]}
    assert "link.broken" in codes, (
        f"link.broken finding must appear under --checks link.broken:\n{out}"
    )


def test_p2_7_validate_checks_rejects_truly_unknown_name(
    tiny_bad_bundle: Path,
) -> None:
    """P2-7 fail-closed: a name that is neither a category nor a known dotted
    finding code is still rejected with an actionable error (we did not widen
    acceptance to 'anything goes')."""
    rc, out, err = _capture([
        "validate", str(tiny_bad_bundle), "--checks", "totally_made_up",
    ])
    assert rc != 0, "truly unknown check name must not exit 0"
    assert "Unknown check names" in err, (
        f"unknown name must be reported clearly:\n{err}"
    )
