"""Capability registry — the forward-compatibility mechanism (see docs/architecture.md).

OKF is intentionally permissive (upstream OKF 0.2 §11: producers MAY add arbitrary
frontmatter keys, consumers MUST preserve them round-trip). The capability
registry is how okf-loom makes arbitrary keys *meaningful* without
requiring a spec change.

Each capability is a named, versioned declaration of an OPTIONAL behavior
okf-loom recognizes. Examples:
    - `okf.cap.typed_relations`  — interpret a `relations:` frontmatter key
      as a list of typed relation objects (link + type + provenance).
    - `okf.cap.embeddings`      — emit a per-concept embedding sidecar for
      semantic search.
    - `okf.cap.diataxis_types`  — recognise the Diátaxis type taxonomy.

A bundle opts in either by listing capabilities under `okf_extensions:` in
its root `index.md` frontmatter, or simply by using the keys a capability
governs (auto-activation). Plugins may also register capabilities at
import time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import CAPABILITY_NAMESPACE, SPEC_VERSION


@dataclass(frozen=True)
class Capability:
    """A named, optional behavior okf-loom recognises.

    Attributes:
        id: dotted id starting with `okf.cap.` (built-ins) or a custom
            namespace (e.g. `acme.custom_types`) for user-supplied plugins.
        min_spec_version: minimum OKF spec version required (default "0.1").
        tier: one of `"core"`, `"recommended"`, `"optional"`. Core caps are
            always available; recommended ones ship but may need an extra;
            optional ones require a plugin or extra to be installed.
        description: human-readable summary.
        frontmatter_keys: frontmatter keys this capability governs.
        body_sections: conventional `# Heading` sections this capability
            recognises.
        activator: optional callable invoked when the capability activates.
    """

    id: str
    min_spec_version: str = SPEC_VERSION
    tier: str = "optional"
    description: str = ""
    frontmatter_keys: tuple[str, ...] = ()
    body_sections: tuple[str, ...] = ()
    activator: Callable[[], None] | None = None


class CapabilityRegistry:
    """Process-wide registry of capabilities.

    Built-in capabilities are registered at import time. Plugins extend the
    registry via `register()`. Bundles declare their opt-ins via
    `okf_extensions:` in their root `index.md` frontmatter (parsed by
    `Bundle.load`); `resolve()` maps a bundle's declarations to a
    `ResolvedCapabilities` view.
    """

    def __init__(self) -> None:
        self._caps: dict[str, Capability] = {}

    def register(self, cap: Capability) -> None:
        if not cap.id.startswith(CAPABILITY_NAMESPACE + ".") and "." not in cap.id:
            raise ValueError(
                f"Capability id must be namespaced (e.g. '{CAPABILITY_NAMESPACE}.x' or 'acme.x'): {cap.id!r}"
            )
        self._caps[cap.id] = cap

    def unregister(self, cap_id: str) -> None:
        self._caps.pop(cap_id, None)

    def get(self, cap_id: str) -> Capability | None:
        return self._caps.get(cap_id)

    def all(self) -> Iterable[Capability]:
        return list(self._caps.values())

    def known(self) -> set[str]:
        return set(self._caps.keys())

    def resolve(
        self,
        *,
        declared: Iterable[str] | None = None,
        observed_keys: Iterable[str] | None = None,
        observed_sections: Iterable[str] | None = None,
    ) -> "ResolvedCapabilities":
        """Resolve which capabilities are active for a bundle.

        Activation rules:
            1. Every id in `declared` that is known activates (unknown
               declarations are surfaced as `undeclared` for diagnostics).
            2. A capability auto-activates if any of its `frontmatter_keys`
               appear in `observed_keys`, or any of its `body_sections`
               appear in `observed_sections`.
            3. `core` tier capabilities are always active.
        """
        declared = list(declared or [])
        observed_keys = set(observed_keys or [])
        observed_sections = set(observed_sections or [])

        active: dict[str, Capability] = {}
        undeclared: list[str] = []

        # 1. declared
        for d in declared:
            cap = self._caps.get(d)
            if cap is None:
                undeclared.append(d)
            else:
                active[d] = cap

        # 2. auto-activation by observed keys/sections
        for cap in self._caps.values():
            if cap.id in active:
                continue
            if cap.frontmatter_keys and any(
                k in observed_keys for k in cap.frontmatter_keys
            ):
                active[cap.id] = cap
                continue
            if cap.body_sections and any(
                s in observed_sections for s in cap.body_sections
            ):
                active[cap.id] = cap

        # 3. core
        for cap in self._caps.values():
            if cap.tier == "core":
                active.setdefault(cap.id, cap)

        return ResolvedCapabilities(
            active=dict(active),
            undeclared=undeclared,
        )


@dataclass(frozen=True)
class ResolvedCapabilities:
    active: dict[str, Capability]
    undeclared: list[str]

    @property
    def active_ids(self) -> set[str]:
        return set(self.active.keys())

    def is_active(self, cap_id: str) -> bool:
        return cap_id in self.active


# ---------------------------------------------------------------------------
# Built-in capabilities (always registered at import time)
# ---------------------------------------------------------------------------


_BUILTIN_CAPABILITIES: tuple[Capability, ...] = (
    # --- core (always active) ---
    Capability(
        id="okf.cap.frontmatter",
        tier="core",
        description="Parse YAML frontmatter, alias-collapse known key variants.",
        frontmatter_keys=("type", "title", "description", "resource", "tags", "timestamp"),
    ),
    Capability(
        id="okf.cap.links",
        tier="core",
        description="Resolve absolute and relative cross-links (upstream OKF 0.2 §6.1).",
    ),
    Capability(
        id="okf.cap.index_md",
        tier="core",
        description="Synthesize and respect upstream OKF 0.2 §8 index.md directory listings.",
    ),
    Capability(
        id="okf.cap.log_md",
        tier="core",
        description="Append to upstream OKF 0.2 §9 log.md update history.",
    ),
    Capability(
        id="okf.cap.listings",
        tier="core",
        description="Auto-generate folder/tag listing pages in the viewer.",
    ),
    Capability(
        id="okf.cap.search_lexical",
        tier="core",
        description="Zero-dependency lexical search (BM25/TF-IDF).",
    ),
    Capability(
        id="okf.cap.graph",
        tier="core",
        description="Force-directed graph view of concept cross-links.",
    ),
    Capability(
        id="okf.cap.backlinks",
        tier="core",
        description="Reverse-link (cited-by) panels in the viewer.",
    ),
    Capability(
        id="okf.cap.discovery_mentions",
        tier="core",
        description="Detect unlinked mentions of concept titles as discovery suggestions.",
    ),

    # --- recommended (ship with okf-loom; opt-in by declaration or use) ---
    Capability(
        id="okf.cap.search_hybrid",
        tier="recommended",
        description="Hybrid lexical + SemanticLite search with reciprocal rank fusion.",
        frontmatter_keys=(),
    ),
    Capability(
        id="okf.cap.popovers",
        tier="recommended",
        description="Hover-preview popovers for cross-concept links in the viewer.",
    ),
    Capability(
        id="okf.cap.log_timeline",
        tier="recommended",
        description="Render upstream OKF 0.2 §9 log entries as a navigable timeline view.",
    ),
    Capability(
        id="okf.cap.tag_pages",
        tier="recommended",
        description="Synthesize per-tag listing pages and tag cloud.",
    ),

    # --- optional (require an extra or plugin) ---
    Capability(
        id="okf.cap.typed_relations",
        tier="optional",
        description=(
            "Interpret a `relations:` frontmatter key as a list of typed "
            "relation objects ({target, type, ...}). Recognised-if-present, "
            "never required."
        ),
        frontmatter_keys=("relations",),
    ),
    Capability(
        id="okf.cap.entities",
        tier="optional",
        description=(
            "Interpret an `entities:` frontmatter key as named-entity tags. "
            "Entries may be bare strings or objects of shape "
            "{id, label, kind, aliases} (current spec §4). Entity labels and "
            "aliases feed entity search and lexical indexing."
        ),
        frontmatter_keys=("entities",),
    ),
    Capability(
        id="okf.cap.aliases",
        tier="optional",
        description=(
            "Interpret an `aliases:` frontmatter key as alternate names for "
            "the concept. Consumed by entity search and the viewer."
        ),
        frontmatter_keys=("aliases",),
    ),
    Capability(
        id="okf.cap.provenance",
        tier="optional",
        description=(
            "Interpret a `provenance:` frontmatter key as source/origin "
            "metadata for the concept."
        ),
        frontmatter_keys=("provenance",),
    ),
    Capability(
        id="okf.cap.citations",
        tier="optional",
        description=(
            "Interpret a `citations:` frontmatter key as reference citations "
            "for the concept."
        ),
        frontmatter_keys=("citations",),
    ),
    Capability(
        id="okf.cap.embeddings",
        tier="optional",
        description="Emit per-concept dense-vector embeddings for semantic search.",
    ),
    Capability(
        id="okf.cap.discovery_ner",
        tier="optional",
        description="Use offline GLiNER NER to suggest entities in concept bodies.",
    ),
    Capability(
        id="okf.cap.discovery_llm",
        tier="optional",
        description="Use an LLM to suggest missing links, relations, and summaries.",
    ),
    Capability(
        id="okf.cap.diataxis_types",
        tier="optional",
        description="Recognise the Diátaxis taxonomy (tutorial/how-to/reference/explanation) for `type` values.",
    ),
    Capability(
        id="okf.cap.wikilinks",
        tier="optional",
        description=(
            "Recognise Obsidian-style ``[[target]]`` and ``[[target|Label]]`` "
            "wikilinks in concept bodies (Loom extension; current spec §4). Resolved to concept ids "
            "and added to the graph as ``form='wikilink'`` edges. This is body "
            "syntax (no frontmatter key); mutators always emit standard "
            "markdown links."
        ),
        frontmatter_keys=(),
    ),
    Capability(
        id="okf.cap.jsonld_export",
        tier="optional",
        description="Export the bundle graph as JSON-LD (schema.org/Dataset where applicable).",
    ),
    Capability(
        id="okf.cap.multi_bundle",
        tier="optional",
        description="Treat multiple bundles as one federated graph.",
    ),
)


# Module-level singleton registry. Tests and downstream code may construct
# their own CapabilityRegistry() but the default is what CLI/library uses.
DEFAULT_REGISTRY = CapabilityRegistry()
for _cap in _BUILTIN_CAPABILITIES:
    DEFAULT_REGISTRY.register(_cap)


def default_registry() -> CapabilityRegistry:
    """Return the process-wide default registry."""
    return DEFAULT_REGISTRY
