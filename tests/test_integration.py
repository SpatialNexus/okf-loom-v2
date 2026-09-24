"""End-to-end integration tests over upstream real-data bundles.

The full happy-path flow exercised is:

    load -> validate -> discover -> build plan from suggestion -> apply
    -> re-discover (suggestion gone) -> re-validate (still ok)

Plus: render single-file output is valid HTML containing the expected
concept count.

All tests in this module are marked ``@pytest.mark.integration`` via
``conftest.py`` (any test using ``ga4_bundle`` / ``stackoverflow_bundle``
fixtures gets the marker automatically).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.discover import discover_suggestions
from okf_loom.search import clear_search_cache, search_bundle
from okf_loom.update import UpdateOp, UpdatePlan, apply_plan
from okf_loom.validate import validate_bundle


# ---------------------------------------------------------------------------
# Load -> validate happy path on real bundles
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ga4_loads_and_validates(ga4_bundle: Path) -> None:
    """ga4 loads cleanly and validates as SPEC-conformant."""
    b = Bundle.load(ga4_bundle)
    assert len(b.concepts) > 0
    r = validate_bundle(b)
    assert r.ok is True


@pytest.mark.integration
def test_stackoverflow_loads_and_validates(stackoverflow_bundle: Path) -> None:
    """stackoverflow loads cleanly and validates as SPEC-conformant."""
    b = Bundle.load(stackoverflow_bundle)
    assert len(b.concepts) > 0
    r = validate_bundle(b)
    assert r.ok is True


@pytest.mark.integration
def test_crypto_bitcoin_loads_and_validates(crypto_bitcoin_bundle: Path) -> None:
    """crypto_bitcoin loads cleanly and validates as SPEC-conformant."""
    b = Bundle.load(crypto_bitcoin_bundle)
    assert len(b.concepts) > 0
    r = validate_bundle(b)
    assert r.ok is True


# ---------------------------------------------------------------------------
# Discover -> apply -> re-discover loop on stackoverflow (has unlinked_mentions)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_stackoverflow_unlinked_mentions_loop(stackoverflow_bundle: Path) -> None:
    """Full discover -> apply -> re-discover loop on a real unlinked mention.

    Pins: applying the suggested add_link removes the suggestion on the
    next discovery pass, and the bundle stays SPEC-conformant.

    Note: ``unlinked_mentions`` does not depend on the memoized graph, so
    it works against the in-memory mutated bundle. We still reload from
    disk after apply_plan to exercise the full disk round-trip and to be
    consistent with the orphan_concepts loop.
    """
    b = Bundle.load(stackoverflow_bundle)
    assert validate_bundle(b).ok is True

    rep = discover_suggestions(b, rules=["unlinked_mentions"])
    assert rep.suggestions, "expected at least one unlinked mention"

    first = rep.suggestions[0]
    assert first.concept_id is not None
    assert first.target_concept_id is not None

    plan = UpdatePlan(
        bundle_root=str(stackoverflow_bundle),
        description="link the first unlinked mention",
        plan_kind="update",
        ops=[
            UpdateOp(
                kind="add_link",
                target=first.concept_id,
                args={
                    "label": first.detail.get("label") or "link",
                    "target_concept_id": "/".join(first.target_concept_id),
                },
            )
        ],
    )
    summary = apply_plan(b, plan)
    assert summary["applied"] == 1

    # Reload from disk so the memoized graph is rebuilt with the new edge.
    b = Bundle.load(stackoverflow_bundle)

    # Re-discover: that specific (source -> target) suggestion is gone.
    rep2 = discover_suggestions(b, rules=["unlinked_mentions"])
    still = [
        s for s in rep2.suggestions
        if s.concept_id == first.concept_id
        and s.target_concept_id == first.target_concept_id
    ]
    assert still == []

    # Re-validate: still conformant.
    assert validate_bundle(b).ok is True


# ---------------------------------------------------------------------------
# Orphan_concepts -> add_link -> re-discover loop on ga4 (no unlinked_mentions)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ga4_orphan_to_connected_loop(ga4_bundle: Path) -> None:
    """Exercise orphan repair in a real bundle with a controlled missing edge.

    The flow: discover orphan_concepts -> pick the first orphan -> add a link
    FROM it TO another concept in the bundle -> re-discover -> that orphan
    is no longer flagged -> bundle still validates.

    Note: ``orphan_concepts`` reads from the memoized graph, so after
    apply_plan we MUST reload from disk for the new edge to be visible.
    """
    # Upstream can repair its own orphans. Seed this scenario only in the
    # disposable copy, rather than requiring a permanent upstream defect.
    orphan_path = ga4_bundle / "integration_orphan.md"
    assert not orphan_path.exists()
    orphan_path.write_text(
        "---\ntype: Note\ntitle: Integration orphan\n"
        "description: Synthetic disconnected concept for the repair rehearsal.\n"
        "tags: [integration]\n---\nNo connections yet.\n", encoding="utf-8",
    )
    b = Bundle.load(ga4_bundle)
    assert validate_bundle(b).ok is True

    rep = discover_suggestions(b, rules=["orphan_concepts"])
    orphan = next(s for s in rep.suggestions if s.concept_id == ("integration_orphan",))
    assert orphan.concept_id is not None

    # Pick any *other* concept to link to.
    other = next(
        cid for cid in b.concepts if cid != orphan.concept_id
    )
    plan = UpdatePlan(
        bundle_root=str(ga4_bundle),
        description="connect the orphan",
        plan_kind="update",
        ops=[
            UpdateOp(
                kind="add_link",
                target=orphan.concept_id,
                args={
                    "label": b.concepts[other].title,
                    "target_concept_id": "/".join(other),
                },
            )
        ],
    )
    summary = apply_plan(b, plan)
    assert summary["applied"] == 1

    # Reload from disk so the memoized graph is rebuilt with the new edge.
    b = Bundle.load(ga4_bundle)

    # Re-discover: that specific orphan is no longer flagged.
    rep2 = discover_suggestions(b, rules=["orphan_concepts"])
    still_orphan = [
        s for s in rep2.suggestions if s.concept_id == orphan.concept_id
    ]
    assert still_orphan == []

    # Re-validate: still conformant.
    assert validate_bundle(b).ok is True


# ---------------------------------------------------------------------------
# Apply idempotency on a real bundle
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_apply_plan_idempotent_on_stackoverflow(stackoverflow_bundle: Path) -> None:
    """Applying the same single-op plan twice yields applied:0 on the 2nd run."""
    b = Bundle.load(stackoverflow_bundle)
    rep = discover_suggestions(b, rules=["unlinked_mentions"])
    if not rep.suggestions:
        pytest.skip("no unlinked_mentions available")
    first = rep.suggestions[0]
    plan = UpdatePlan(
        bundle_root=str(stackoverflow_bundle),
        description="idempotency check",
        plan_kind="update",
        ops=[
            UpdateOp(
                kind="add_link",
                target=first.concept_id,
                args={
                    "label": first.detail.get("label") or "link",
                    "target_concept_id": "/".join(first.target_concept_id),
                },
            )
        ],
    )
    s1 = apply_plan(b, plan)
    s2 = apply_plan(b, plan)
    assert s1["applied"] == 1
    assert s2["applied"] == 0
    assert s2["results"][0][1]["reason"] == "already_linked"


# ---------------------------------------------------------------------------
# Search determinism on real bundles
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_search_determinism_on_ga4(ga4_bundle: Path) -> None:
    """Two identical searches on ga4 return the same concept-id ordering."""
    b = Bundle.load(ga4_bundle)
    clear_search_cache()
    r1 = search_bundle(b, "events ecommerce user", limit=10)
    r2 = search_bundle(b, "events ecommerce user", limit=10)
    assert [r.concept_id for r in r1] == [r.concept_id for r in r2]
    assert [r.score for r in r1] == [r.score for r in r2]


# ---------------------------------------------------------------------------
# Render integration: single-file output is valid HTML with the right count
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_render_single_file_on_ga4_contains_concept_count(
    ga4_bundle: Path, tmp_path: Path
) -> None:
    """Rendered single-file HTML embeds the ga4 concept count."""
    from okf_loom.render import render_single_file

    b = Bundle.load(ga4_bundle)
    n = len(b.concepts)
    out = tmp_path / "ga4.html"
    stats = render_single_file(b, out)
    assert stats["concepts"] == n

    content = out.read_text(encoding="utf-8")
    assert "<html" in content.lower() or "<!doctype html>" in content.lower()
    # The node count is embedded as JSON: '"nodes":[{...}' repeated N times.
    assert content.count('"data": {') >= n


@pytest.mark.integration
def test_render_single_file_on_stackoverflow_embeds_graph(
    stackoverflow_bundle: Path, tmp_path: Path
) -> None:
    """Rendered single-file HTML contains a JSON graph blob for stackoverflow."""
    from okf_loom.render import render_single_file

    b = Bundle.load(stackoverflow_bundle)
    out = tmp_path / "so.html"
    render_single_file(b, out)
    content = out.read_text(encoding="utf-8")
    assert '"nodes"' in content
    assert '"edges"' in content


# ---------------------------------------------------------------------------
# Full pipeline: load -> validate -> discover -> plan -> apply -> re-validate
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    "bundle_fixture",
    ["ga4_bundle", "stackoverflow_bundle"],
)
def test_full_pipeline_load_to_revalidate(
    bundle_fixture: str, request: pytest.FixtureRequest
) -> None:
    """The end-to-end pipeline: load -> validate -> discover -> plan ->
    apply -> re-validate. Both upstream bundles stay conformant throughout.
    """
    bundle_path: Path = request.getfixturevalue(bundle_fixture)
    b = Bundle.load(bundle_path)

    # 1. Validate the source bundle.
    r1 = validate_bundle(b)
    assert r1.ok is True, f"{bundle_fixture} should validate ok"

    # 2. Discover suggestions (any rule).
    rep = discover_suggestions(b)
    # Apply only if there's an actionable add_link suggestion; otherwise
    # just verify re-validation still passes (no-op pipeline).
    actionable = next(
        (s for s in rep.suggestions
         if s.rule in ("unlinked_mentions", "orphan_concepts")
         and s.concept_id is not None),
        None,
    )
    if actionable is None:
        # Re-validate without applying anything.
        assert validate_bundle(b).ok is True
        return

    # 3. Pick a target concept to link to.
    target_cid = actionable.target_concept_id
    if target_cid is None:
        # orphan_concepts: pick any other concept in the bundle.
        target_cid = next(
            cid for cid in b.concepts if cid != actionable.concept_id
        )

    plan = UpdatePlan(
        bundle_root=str(bundle_path),
        description="integration test plan",
        plan_kind="update",
        ops=[
            UpdateOp(
                kind="add_link",
                target=actionable.concept_id,
                args={
                    "label": b.concepts[target_cid].title,
                    "target_concept_id": "/".join(target_cid),
                },
            )
        ],
    )

    # 4. Apply the plan (writes to disk).
    summary = apply_plan(b, plan)
    assert summary["applied"] == 1, (
        f"apply_plan should succeed for {bundle_fixture}; "
        f"results={summary['results']}"
    )

    # Reload from disk so the memoized graph sees the new edge.
    b = Bundle.load(bundle_path)

    # 5. Re-validate the mutated bundle.
    r2 = validate_bundle(b)
    assert r2.ok is True, (
        f"{bundle_fixture} should still validate ok after the plan; "
        f"errors={[f.code for f in r2.errors]}"
    )


# ---------------------------------------------------------------------------
# Atomic write sanity on a real bundle: no .tmp files left behind
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_apply_plan_leaves_no_tmp_files_on_stackoverflow(
    stackoverflow_bundle: Path,
) -> None:
    """After applying a plan to a real bundle, no .tmp files remain."""
    b = Bundle.load(stackoverflow_bundle)
    rep = discover_suggestions(b, rules=["unlinked_mentions"])
    if not rep.suggestions:
        pytest.skip("no unlinked_mentions")
    first = rep.suggestions[0]
    plan = UpdatePlan(
        bundle_root=str(stackoverflow_bundle),
        description="tmp file check",
        plan_kind="update",
        ops=[
            UpdateOp(
                kind="add_link",
                target=first.concept_id,
                args={
                    "label": "link",
                    "target_concept_id": "/".join(first.target_concept_id),
                },
            )
        ],
    )
    apply_plan(b, plan)
    tmps = list(stackoverflow_bundle.rglob("*.tmp"))
    assert tmps == []
