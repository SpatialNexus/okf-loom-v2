"""Tests for ``okf_loom.extensions``.

Pinned invariants:
  * Built-in capabilities load at import (default registry non-empty).
  * ``register`` / ``unregister`` add and remove capabilities.
  * ``resolve`` activates declared ids, auto-activates on observed keys
    and sections, and always-activates core-tier capabilities.
  * Unknown declared ids go to ``undeclared``.
  * Custom namespace registration works.
  * SPEC §10: ``okf.cap.wikilinks`` is registered as tier=optional with
    empty frontmatter_keys/body_sections (it is body syntax); its
    auto-activation flows through ``Bundle.capabilities()`` adding the
    id to the declared set when ``bundle.has_wikilinks`` is True.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from okf_loom import CAPABILITY_NAMESPACE
from okf_loom.extensions import (
    Capability,
    CapabilityRegistry,
    ResolvedCapabilities,
    default_registry,
)


# --- built-in capabilities --------------------------------------------------


def test_default_registry_has_builtins() -> None:
    """The default registry is populated at import time."""
    reg = default_registry()
    known = reg.known()
    assert "okf.cap.frontmatter" in known
    assert "okf.cap.links" in known
    assert "okf.cap.index_md" in known
    assert "okf.cap.log_md" in known
    assert "okf.cap.search_lexical" in known
    assert "okf.cap.typed_relations" in known


def test_default_registry_is_singleton() -> None:
    """``default_registry()`` returns the same process-wide object."""
    assert default_registry() is default_registry()


def test_entities_capability_description_documents_spec_shape() -> None:
    """The okf.cap.entities description must document the current spec §4
    canonical object shape {id, label, kind, aliases}, NOT the misleading
    {name, kind, source} that previously appeared here.

    Regression: a misleading description led users to author entities with
    `name:` keys, which entity search silently ignored (it reads `label:`).
    """
    reg = default_registry()
    cap = next(c for c in reg.all() if c.id == "okf.cap.entities")
    desc = cap.description
    assert "label" in desc, f"description must mention `label`: {desc!r}"
    assert "{name" not in desc, (
        f"description must not advertise the non-canonical `name` key: {desc!r}"
    )


def test_builtin_core_capabilities_present() -> None:
    """All built-in capabilities marked tier='core' exist."""
    reg = default_registry()
    core_ids = {c.id for c in reg.all() if c.tier == "core"}
    # The nine core caps from extensions.py.
    assert {
        "okf.cap.frontmatter",
        "okf.cap.links",
        "okf.cap.index_md",
        "okf.cap.log_md",
        "okf.cap.listings",
        "okf.cap.search_lexical",
        "okf.cap.graph",
        "okf.cap.backlinks",
        "okf.cap.discovery_mentions",
    }.issubset(core_ids)


# --- register / unregister --------------------------------------------------


def test_register_adds_capability() -> None:
    reg = CapabilityRegistry()
    cap = Capability(
        id=f"{CAPABILITY_NAMESPACE}.test_cap",
        description="test",
        frontmatter_keys=("test_key",),
    )
    reg.register(cap)
    assert reg.get(f"{CAPABILITY_NAMESPACE}.test_cap") is cap
    assert cap.id in reg.known()


def test_unregister_removes_capability() -> None:
    reg = CapabilityRegistry()
    cap = Capability(id=f"{CAPABILITY_NAMESPACE}.removable")
    reg.register(cap)
    assert cap.id in reg.known()
    reg.unregister(cap.id)
    assert cap.id not in reg.known()
    # Unregistering an unknown id is a no-op.
    reg.unregister("does.not.exist")


def test_register_rejects_unnamespaced_id() -> None:
    """A capability id without a namespace component is rejected."""
    reg = CapabilityRegistry()
    with pytest.raises(ValueError, match="namespaced"):
        reg.register(Capability(id="barename"))


def test_register_allows_custom_namespace() -> None:
    """A custom namespace like ``acme.x`` is allowed."""
    reg = CapabilityRegistry()
    cap = Capability(id="acme.custom_types", description="custom")
    reg.register(cap)
    assert "acme.custom_types" in reg.known()


def test_get_returns_none_for_unknown() -> None:
    reg = CapabilityRegistry()
    assert reg.get("nope.nope") is None


def test_all_returns_iterable_of_capabilities() -> None:
    reg = CapabilityRegistry()
    reg.register(Capability(id="x.y"))
    reg.register(Capability(id="z.w"))
    all_caps = list(reg.all())
    assert len(all_caps) == 2
    assert all(isinstance(c, Capability) for c in all_caps)


# --- resolve: declared ------------------------------------------------------


def test_resolve_activates_declared_known() -> None:
    """Declared ids that exist in the registry are activated."""
    reg = CapabilityRegistry()
    cap = Capability(id="okf.cap.foo", tier="optional")
    reg.register(cap)
    resolved = reg.resolve(declared=["okf.cap.foo"])
    assert "okf.cap.foo" in resolved.active_ids
    assert resolved.undeclared == []


def test_resolve_unknown_declared_goes_to_undeclared() -> None:
    """Declared ids not in the registry are surfaced as ``undeclared``."""
    reg = CapabilityRegistry()
    resolved = reg.resolve(declared=["okf.cap.does_not_exist"])
    assert "okf.cap.does_not_exist" in resolved.undeclared
    assert "okf.cap.does_not_exist" not in resolved.active_ids


# --- resolve: auto-activation by observed keys/sections ---------------------


def test_resolve_auto_activates_on_observed_frontmatter_key() -> None:
    """A capability whose ``frontmatter_keys`` intersect observed_keys activates."""
    reg = CapabilityRegistry()
    cap = Capability(
        id="okf.cap.relations", tier="optional",
        frontmatter_keys=("relations",),
    )
    reg.register(cap)
    resolved = reg.resolve(observed_keys={"relations", "type"})
    assert "okf.cap.relations" in resolved.active_ids


def test_resolve_auto_activates_on_observed_body_section() -> None:
    """A capability whose ``body_sections`` intersect observed_sections activates."""
    reg = CapabilityRegistry()
    cap = Capability(
        id="okf.cap.diagram", tier="optional",
        body_sections=("Diagram",),
    )
    reg.register(cap)
    resolved = reg.resolve(observed_sections={"Diagram", "Overview"})
    assert "okf.cap.diagram" in resolved.active_ids


def test_resolve_does_not_activate_when_no_overlap() -> None:
    reg = CapabilityRegistry()
    cap = Capability(
        id="okf.cap.x", tier="optional", frontmatter_keys=("special_k",)
    )
    reg.register(cap)
    resolved = reg.resolve(observed_keys={"unrelated"})
    assert "okf.cap.x" not in resolved.active_ids


# --- resolve: core tier always active ---------------------------------------


def test_resolve_core_always_active() -> None:
    """Core-tier capabilities are active regardless of declaration/observation."""
    reg = CapabilityRegistry()
    core = Capability(id="okf.cap.always_on", tier="core")
    reg.register(core)
    resolved = reg.resolve(declared=None, observed_keys=set(), observed_sections=set())
    assert "okf.cap.always_on" in resolved.active_ids


def test_default_registry_core_caps_always_resolve() -> None:
    """On the default registry, all core caps resolve active with no input."""
    reg = default_registry()
    resolved = reg.resolve()
    core_ids = {c.id for c in reg.all() if c.tier == "core"}
    assert core_ids.issubset(resolved.active_ids)


# --- resolve: combined ------------------------------------------------------


def test_resolve_combined_declared_auto_and_core() -> None:
    """All three activation rules can fire in a single resolve call."""
    reg = CapabilityRegistry()
    declared_cap = Capability(id="okf.cap.declared_one", tier="optional")
    auto_cap = Capability(
        id="okf.cap.auto_one", tier="optional",
        frontmatter_keys=("auto_k",),
    )
    core_cap = Capability(id="okf.cap.core_one", tier="core")
    for c in (declared_cap, auto_cap, core_cap):
        reg.register(c)
    resolved = reg.resolve(
        declared=["okf.cap.declared_one", "okf.cap.unknown_undeclared"],
        observed_keys={"auto_k"},
    )
    assert "okf.cap.declared_one" in resolved.active_ids
    assert "okf.cap.auto_one" in resolved.active_ids
    assert "okf.cap.core_one" in resolved.active_ids
    assert "okf.cap.unknown_undeclared" in resolved.undeclared


# --- ResolvedCapabilities API -----------------------------------------------


def test_resolved_is_active() -> None:
    reg = CapabilityRegistry()
    cap = Capability(id="okf.cap.x", tier="optional")
    reg.register(cap)
    resolved = reg.resolve(declared=["okf.cap.x"])
    assert isinstance(resolved, ResolvedCapabilities)
    assert resolved.is_active("okf.cap.x") is True
    assert resolved.is_active("okf.cap.unknown") is False


def test_resolved_active_ids_is_a_set() -> None:
    reg = default_registry()
    resolved = reg.resolve()
    assert isinstance(resolved.active_ids, set)


# --- capability dataclass ---------------------------------------------------


def test_capability_default_min_spec_version() -> None:
    cap = Capability(id="okf.cap.x")
    assert cap.min_spec_version == "0.2"
    assert cap.tier == "optional"
    assert cap.frontmatter_keys == ()
    assert cap.body_sections == ()
    assert cap.activator is None


def test_capability_is_frozen() -> None:
    """Capability is a frozen dataclass (hashable, immutable)."""
    cap = Capability(id="okf.cap.x")
    with pytest.raises(Exception):
        cap.id = "okf.cap.y"  # type: ignore[misc]


# --- SPEC §10: okf.cap.wikilinks auto-activation ----------------------------
#
# Wikilinks are body syntax, not a frontmatter key, so the capability
# descriptor has empty frontmatter_keys/body_sections and the auto-activation
# flows through ``Bundle.capabilities()`` (which adds the id to the declared
# set when ``bundle.has_wikilinks`` is True). These tests pin that contract.


def test_wikilinks_capability_descriptor_in_default_registry() -> None:
    """``okf.cap.wikilinks`` is registered at import time with tier=optional
    and intentionally empty frontmatter_keys (it is body syntax, SPEC §10)."""
    reg = default_registry()
    assert "okf.cap.wikilinks" in reg.known()
    cap = reg.get("okf.cap.wikilinks")
    assert cap is not None
    assert cap.tier == "optional"
    assert cap.frontmatter_keys == ()
    assert cap.body_sections == ()


def test_wikilinks_capability_does_not_auto_activate_on_keys_or_sections() -> None:
    """Because wikilinks is body syntax (no frontmatter key, no body section),
    ``resolve()`` must NOT activate it via the observed_keys/sections rules.
    Auto-activation flows through ``Bundle.capabilities()`` instead, which is
    exercised in test_bundle_capabilities_* below."""
    reg = default_registry()
    resolved = reg.resolve(
        observed_keys={"type", "title", "relations"},
        observed_sections={"Schema", "Examples"},
    )
    assert "okf.cap.wikilinks" not in resolved.active_ids


def test_wikilinks_capability_declared_activation_still_works() -> None:
    """A bundle that explicitly declares ``okf.cap.wikilinks`` under
    okf_extensions activates through the normal declared path."""
    reg = default_registry()
    resolved = reg.resolve(declared=["okf.cap.wikilinks"])
    assert "okf.cap.wikilinks" in resolved.active_ids
    assert resolved.undeclared == []


# --- Bundle.capabilities() integration (drives the real auto-activation) -----


def test_bundle_capabilities_activates_wikilinks_on_observed_body(
    tmp_path: Path,
) -> None:
    """SPEC §10 L605-607: ``okf.cap.wikilinks`` auto-activates when any
    ``[[...]]`` is present in a concept body. The activation flows through
    ``Bundle.capabilities()`` adding the id to the declared set."""
    from okf_loom import Bundle

    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nSee [[b]] for more.\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text(
        "---\ntype: T\ntitle: B\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    resolved = b.capabilities()
    assert resolved.is_active("okf.cap.wikilinks") is True
    # The other §10 acceptance detail: has_wikilinks agrees with the
    # capability decision.
    assert b.has_wikilinks is True


def test_bundle_capabilities_does_not_activate_wikilinks_without_observed_body(
    tmp_path: Path,
) -> None:
    """Without any ``[[...]]`` body, the capability stays inactive even when
    other optional capabilities (e.g. typed_relations) activate."""
    from okf_loom import Bundle

    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\nrelations:\n  - target: b\n    type: references\n"
        "---\nSee [b](/b.md).\n",
        encoding="utf-8",
    )
    (tmp_path / "b.md").write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")
    b = Bundle.load(tmp_path)
    resolved = b.capabilities()
    assert resolved.is_active("okf.cap.wikilinks") is False
    # typed_relations should still activate via the relations: frontmatter key.
    assert resolved.is_active("okf.cap.typed_relations") is True


# ---------------------------------------------------------------------------
# P1-21 / P2-21: the four §7 governed-key capabilities
# (aliases/provenance/citations/entities) must auto-activate on key presence
# and round-trip through parse/serialize. The earlier auto-activation tests
# only used synthetic caps; these pin the built-in descriptors.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,cap_id",
    [
        ("aliases", "okf.cap.aliases"),
        ("provenance", "okf.cap.provenance"),
        ("citations", "okf.cap.citations"),
        ("entities", "okf.cap.entities"),
    ],
)
def test_builtin_governed_cap_auto_activates_on_key_presence(
    key: str, cap_id: str, tmp_path
) -> None:
    """P1-21 (SPEC §7 L491): each built-in governed capability auto-activates
    when its observed frontmatter key is present in any concept."""
    from okf_loom import Bundle

    (tmp_path / "index.md").write_text("okf_version: '0.1'\n", encoding="utf-8")
    (tmp_path / "c.md").write_text(
        f"---\ntype: T\ntitle: C\n{key}:\n  - sample\n---\nbody\n",
        encoding="utf-8",
    )
    bundle = Bundle.load(tmp_path)
    resolved = bundle.capabilities()
    assert cap_id in resolved.active_ids, (
        f"{cap_id} should auto-activate on observed key {key!r} "
        f"(active={sorted(resolved.active_ids)})"
    )


def test_default_registry_registers_v02_governed_caps() -> None:
    """P1-21: the four §7 caps are registered on the default registry with
    tier=optional and correct frontmatter_keys."""
    from okf_loom.extensions import default_registry

    reg = default_registry()
    known = reg.known()
    for cap_id, expected_key in [
        ("okf.cap.aliases", "aliases"),
        ("okf.cap.provenance", "provenance"),
        ("okf.cap.citations", "citations"),
        ("okf.cap.entities", "entities"),
    ]:
        assert cap_id in known, f"{cap_id} missing from default registry"
        cap = reg.get(cap_id)
        assert cap is not None and cap.tier == "optional", (
            f"{cap_id} must be tier=optional (got {cap})"
        )
        assert expected_key in cap.frontmatter_keys, (
            f"{cap_id} frontmatter_keys must include {expected_key!r} "
            f"(got {cap.frontmatter_keys})"
        )


def test_v02_governed_keys_round_trip(tmp_path) -> None:
    """P2-21 (SPEC §7 L491-492): the four governed keys round-trip through
    parse_document/serialize_document preserving values + key order."""
    from okf_loom.parse import parse_document, serialize_document

    fm = {
        "type": "Table",
        "aliases": ["purchase orders", "sales orders"],
        "entities": [
            {
                "id": "entity/order",
                "label": "Order",
                "kind": "business_entity",
                "aliases": ["purchase"],
            }
        ],
        "provenance": [
            {
                "source": "https://example.com/orders",
                "note": "imported",
                "timestamp": "2026-06-27T00:00:00Z",
            }
        ],
        "citations": [{"id": "1", "text": "Internal data dictionary"}],
    }
    out = serialize_document(fm, "body\n")
    fm2, body2 = parse_document(out)
    assert fm2["aliases"] == fm["aliases"]
    assert fm2["entities"] == fm["entities"]
    assert fm2["provenance"] == fm["provenance"]
    assert fm2["citations"] == fm["citations"]
    # Key order preserved (round-trip safety, SPEC §3.3).
    assert list(fm2.keys()) == list(fm.keys())
    # parse_document normalizes a trailing newline on the body.
    assert body2.rstrip() == "body"
