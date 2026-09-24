"""Unit tests for the OKF viewer assets module.

Currently focused on the type-palette algorithm (iter3 CRI3-004):
``auto_palette`` switched from a per-name crc32 hash to a golden-angle
hue spread over the sorted type list so adjacent types get maximally
distinct hues. The iter-2 visual review specifically called out
Dataset/Playbook and Reference/Table pairs as visually indistinguishable
under the old hash — these tests pin the new behaviour.
"""
from __future__ import annotations

import re

from okf_loom.viewer import assets


# ---------------------------------------------------------------------------
# iter3 CRI3-004 — auto_palette uses golden-angle hue spread
# ---------------------------------------------------------------------------


# Hue extraction from an hsl(...) string. Tolerates the whitespace/comma
# variants hsl_color may emit across Python format strings.
_HSL_HUE_RE = re.compile(r"hsla?\(\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)


def _hue(color: str) -> float:
    m = _HSL_HUE_RE.match(color.strip())
    assert m, f"not an hsl color: {color!r}"
    return float(m.group(1)) % 360


def _hue_distance(a: float, b: float) -> float:
    """Shortest arc distance between two hues on the 360° wheel."""
    d = abs(a - b) % 360
    return min(d, 360 - d)


def test_auto_palette_returns_one_color_per_type() -> None:
    """Every non-empty type gets exactly one colour; empties are dropped."""
    pal = assets.auto_palette(["Dataset", "Table", "Playbook", "Reference", ""])
    assert set(pal.keys()) == {"Dataset", "Table", "Playbook", "Reference"}
    # Every value is a valid hsl() string (the renderer's CSS-colour
    # allowlist accepts hsl(); a malformed entry would survive the
    # auto-palette path but break the bundle-override sanitiser).
    for v in pal.values():
        assert v.startswith("hsl("), f"non-hsl color in auto palette: {v!r}"


def test_auto_palette_is_deterministic_across_call_order() -> None:
    """The palette is keyed off the SORTED type list, so a different input
    order must produce the same mapping (stable across runs + machines;
    a hash-of-the-input-order would not have this property).
    """
    types_a = ["Dataset", "Table", "Playbook", "Reference"]
    types_b = list(reversed(types_a))
    types_c = ["Playbook", "Reference", "Dataset", "Table"]
    pal_a = assets.auto_palette(types_a)
    pal_b = assets.auto_palette(types_b)
    pal_c = assets.auto_palette(types_c)
    assert pal_a == pal_b == pal_c, (
        "auto_palette is not order-independent — the sorted-list seeding "
        "is broken"
    )


def test_auto_palette_dedupes_repeated_types() -> None:
    """A repeated type in the input gets a single palette entry (the set
    comprehension in auto_palette strips dups)."""
    pal = assets.auto_palette(["Dataset", "Dataset", "Table", "Table"])
    assert pal == assets.auto_palette(["Dataset", "Table"])


def test_auto_palette_no_two_types_share_a_hue() -> None:
    """iter3 CRI3-004: the whole point of the golden-angle spread is that
    adjacent types in the sorted list get maximally distinct hues. With
    up to 12 types in a typical catalog, every hue must be unique."""
    types = ["Dataset", "Table", "Playbook", "Reference", "Metric",
             "Service", "API", "PlaybookRun", "Decision", "Note",
             "Glossary", "Owner"]
    pal = assets.auto_palette(types)
    hues = sorted({_hue(c) for c in pal.values()})
    assert len(hues) == len(pal), (
        f"two types share a hue under golden-angle spread: {pal!r}"
    )


def test_auto_palette_fixes_iter2_collision_pairs() -> None:
    """iter3 CRI3-004: the iter-2 visual review specifically called out
    Dataset/Playbook and Reference/Table pairs as near-indistinguishable
    under the old crc32 hash (their hues landed within ~20° of each other
    at 62% saturation). Under the golden-angle spread, every adjacent
    pair in a small type catalog must be ≥ 30° apart on the hue wheel so
    a user can tell the two apart on the graph canvas at a glance.
    """
    types = sorted({"Dataset", "Table", "Playbook", "Reference"})
    pal = assets.auto_palette(types)
    hues = {t: _hue(pal[t]) for t in types}
    # Every PAIR must be at least 30° apart — far above the JND for hue
    # at 62% saturation, and enough that two adjacent legend swatches
    # read as different colours.
    pairs = [
        ("Dataset", "Playbook"),
        ("Reference", "Table"),
        ("Dataset", "Reference"),
        ("Table", "Playbook"),
    ]
    for a, b in pairs:
        d = _hue_distance(hues[a], hues[b])
        assert d >= 30.0, (
            f"{a} (h={hues[a]:.1f}°) and {b} (h={hues[b]:.1f}°) are only "
            f"{d:.1f}° apart — under the 30° minimum for visual "
            f"distinctness at 62% saturation. Palette: {pal!r}"
        )


def test_auto_palette_handles_empty_and_single_type() -> None:
    """Edge cases: empty list → empty palette; one type → one entry at
    the seed hue (0°)."""
    assert assets.auto_palette([]) == {}
    assert assets.auto_palette([""]) == {}
    pal = assets.auto_palette(["Dataset"])
    assert set(pal.keys()) == {"Dataset"}
    # The first type in the sorted list is at index 0 → hue 0.
    assert _hue(pal["Dataset"]) == 0.0


def test_auto_palette_first_three_hues_follow_golden_angle() -> None:
    """The first three types in the sorted list must land at hue 0°,
    ~137.5°, and ~275° — the canonical golden-angle progression. This
    pins the algorithm against accidental re-ordering: if the seeding
    changes (e.g. someone re-introduces the hash), this test fails
    loudly with the expected-vs-actual hues.
    """
    pal = assets.auto_palette(["A", "B", "C"])
    h_a = _hue(pal["A"])
    h_b = _hue(pal["B"])
    h_c = _hue(pal["C"])
    assert h_a == 0.0, f"first hue should be 0°, got {h_a}"
    assert abs(h_b - 137.508) < 1.0, (
        f"second hue should be ~137.5° (golden angle), got {h_b}"
    )
    assert abs(h_c - 275.016) < 1.0, (
        f"third hue should be ~275° (2× golden angle mod 360), got {h_c}"
    )


def test_stable_hue_still_exists_for_render_fallback() -> None:
    """render.py:404 falls back to ``hsl_color(stable_hue(type))`` for a
    type that is not in the resolved palette (e.g. a concept whose type
    was added live by a CLI mutator that has not re-resolved yet). The
    golden-angle change must NOT remove that fallback path — stable_hue
    still exists and still produces a stable 0..359 hue for any string.
    """
    h1 = assets.stable_hue("Reference")
    h2 = assets.stable_hue("Reference")
    assert h1 == h2, "stable_hue must be deterministic across calls"
    assert 0 <= h1 < 360, f"stable_hue out of range: {h1}"
    # And the two utilities compose (render.py:404 pattern).
    color = assets.hsl_color(assets.stable_hue("Dataset"))
    assert color.startswith("hsl(")


# ---------------------------------------------------------------------------
# UI overhaul Phase 1 — curated semantic type-colour map + hash fallback
# (type = colour + icon, everywhere)
# ---------------------------------------------------------------------------


def test_curated_type_color_known_types_get_stable_semantic_colour() -> None:
    """Every common KB type category resolves to a curated colour that is
    STABLE across calls and independent of bundle composition (unlike
    golden-angle auto_palette, whose hue depends on sorted position).
    "Dataset" is always the same hue whether or not "Service" exists."""
    for t in ("Dataset", "dataset", "DATASET", "datasets"):
        c = assets.curated_type_color(t)
        assert c is not None, f"curated colour missing for {t!r}"
        assert c.startswith("hsl("), f"non-hsl curated colour for {t!r}: {c!r}"
    # Same normalised category → same colour regardless of casing/plural.
    assert assets.curated_type_color("Dataset") == assets.curated_type_color("datasets")
    assert assets.curated_type_color("Service") == assets.curated_type_color("API")
    assert assets.curated_type_color("Playbook") == assets.curated_type_color("runbook")


def test_curated_type_color_unknown_returns_none() -> None:
    """Types outside the curated map return None so the caller can apply the
    hash fallback. This is the seam that lets unknown types degrade
    deterministically rather than colliding with a curated hue."""
    assert assets.curated_type_color("WibblyFrob") is None
    assert assets.curated_type_color("") is None
    assert assets.curated_type_color(None) is None


def test_type_color_for_uses_curated_then_hash_fallback() -> None:
    """The uniform entry point returns the curated colour for known types
    and a deterministic hash-based colour for unknown types (never None,
    never random). The hash fallback is stable across calls/machines."""
    # Known → curated.
    assert assets.type_color_for("Dataset") == assets.curated_type_color("Dataset")
    # Unknown → deterministic hash fallback (same value every call).
    c1 = assets.type_color_for("WibblyFrob")
    c2 = assets.type_color_for("WibblyFrob")
    assert c1 == c2, "hash fallback must be deterministic"
    assert c1.startswith("hsl("), f"non-hsl fallback colour: {c1!r}"
    # Two distinct unknowns get distinct colours (crc32 spread).
    assert assets.type_color_for("WibblyFrob") != assets.type_color_for("FlappyWidget")


def test_curated_colours_are_well_separated() -> None:
    """Curated hues must be visually distinct: every pair ≥ 18° apart on the
    hue wheel so two type swatches in the same legend read as different
    colours. Below 18° at ~55% saturation the JND is marginal; the icon
    disambiguates below that but the colour should still carry identity.
    Also verifies exactly 8 curated categories (plan §2.1)."""
    colours = assets.curated_type_color_map()
    assert len(colours) == 8, (
        f"curated map has {len(colours)} entries, expected exactly 8 (plan §2.1)"
    )
    hues = {k: _hue(v) for k, v in colours.items()}
    keys = sorted(hues)
    worst = 360.0
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            d = _hue_distance(hues[a], hues[b])
            worst = min(worst, d)
    assert worst >= 18.0, (
        f"curated hues have a pair only {worst:.1f}° apart (< 18° min): "
        f"{ {k: round(hues[k], 1) for k in keys} }"
    )


def test_resolve_palette_uses_curated_colours_with_hash_fallback() -> None:
    """resolve_palette layers curated colours for known types over a
    deterministic hash fallback for unknowns. A known type gets its
    curated colour even when unknown types are present; an unknown type
    gets a stable hash-based colour (not golden-angle positional)."""
    from okf_loom.model import Bundle
    from okf_loom.paths import ConceptId
    import tempfile, pathlib

    # Minimal bundle with one known + one unknown type.
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "okf-loom.config.yaml").write_text("viewer: {}\n", encoding="utf-8")
    (root / "index.md").write_text("# T\n", encoding="utf-8")
    (root / "a.md").write_text(
        "---\ntype: Dataset\ntitle: A\ndescription: d\n---\nbody\n", encoding="utf-8")
    (root / "b.md").write_text(
        "---\ntype: WibblyFrob\ntitle: B\ndescription: d\n---\nbody\n", encoding="utf-8")
    bundle = Bundle.load(root)

    pal = assets.resolve_palette(bundle)
    # Known type → curated colour.
    assert pal["Dataset"] == assets.curated_type_color("Dataset")
    # Unknown type → hash fallback (stable, not positional golden-angle).
    assert pal["WibblyFrob"] == assets.hsl_color(assets.stable_hue("WibblyFrob"))
    # The unknown's colour is deterministic regardless of bundle siblings.
    pal2 = assets.resolve_palette(bundle)
    assert pal2["WibblyFrob"] == pal["WibblyFrob"]
