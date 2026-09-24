"""Action planner: discovery → executable, reviewable actions (current spec §7).

This module is the "tell the agent exactly what to run next" layer. ``discover``
produces ``Suggestion`` objects; ``plan`` wraps them (plus index/link/relation
checks) into **executable, reviewable actions** with a stable envelope.

A :class:`PlannedAction` can be:

* **Fully mechanical** — has a runnable ``argv`` (e.g.
  ``["okf","link-add","--bundle",B,"--source",S,"--target",T]``),
  ``shell=False``, ``confidence=None``, ``agent_instruction=None``.
* **Needs agent decision** — has ``argv_template``/``command_template``
  with ``<PLACEHOLDER>`` slots, ``agent_instruction`` describing what the agent
  must decide, and a ``confidence`` score.

``okf update`` consumes action envelopes: ``update.load_plan`` recognises
``{"actions": [...]}`` and converts concrete ``PlannedAction`` objects into
:class:`~okf_loom.update.UpdateOp`\\s. Template actions (with unfilled
``<...>`` slots) are skipped with reason ``needs_agent_input``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .discover import discover_suggestions
from .index import plan_index_regeneration
from .model import Bundle
from .paths import (
    ConceptId,
    ConceptIdError,
    concept_id_from_str,
    concept_id_to_str,
)


@dataclass(frozen=True)
class PlannedAction:
    """One executable, reviewable action (current spec §7).

    Attributes:
        action: stable verb id (``add_link``, ``add_description``,
            ``create_index``, ``fix_broken_link``, ``mirror_relation``, …).
        why: human/agent rationale.
        evidence: concrete pointer (file:line, matched text, …).
        concept_id: primary target (slash string) or None.
        confidence: 0..1 for heuristic actions; None for mechanical.
        agent_instruction: set when human/agent judgement is required.
        argv: executable command (shell=False); None if template.
        argv_template: argv with ``<PLACEHOLDER>`` slots the agent fills.
        command: display-only pretty string; never a shell contract.
        command_template: display string with ``<...>`` slots.
        shell: always False when argv is set.
    """

    action: str
    why: str
    evidence: str
    concept_id: str | None = None
    confidence: float | None = None
    agent_instruction: str | None = None
    argv: list[str] | None = None
    argv_template: list[str] | None = None
    command: str | None = None
    command_template: str | None = None
    shell: bool = False
    # P2-10: structured context for dedup/matching. ``why`` is display-only;
    # never parse it for machine logic. Use ``context`` for structured fields.
    context: dict = field(default_factory=dict)
    # P2-10 (iter-1 arch Candidate 2): structured application payload.
    # Populated ONLY for mechanical actions that map to one or more
    # :class:`~okf_loom.update.UpdateOp`\\s. Carries the SAME information
    # as ``argv`` but as a dict keyed by the consuming handler's expected
    # args, so ``update._actions_to_ops`` can build the UpdateOp(s) directly
    # WITHOUT re-parsing ``argv`` via ``_parse_argv``. This eliminates the
    # emitter/parser drift class of bugs (see the P1-5 iter-3 convergence
    # bug previously documented at the mirror_relation call site).
    # ``None`` for template/judgement actions (they need agent input;
    # ``argv_template`` is the contract) and for any argv-only action without
    # a clean mechanical mapping. ``argv`` remains the agent-facing
    # machine contract (current spec §7); both coexist.
    op_payload: dict | None = None

    def __post_init__(self) -> None:
        # P3-3: enforce the "shell=False when argv is set" invariant at
        # construction time. A PlannedAction with a runnable argv must never
        # enable shell interpolation (no shell=True with argv).
        if self.argv is not None and self.shell:
            raise ValueError(
                "PlannedAction.shell must be False when argv is set "
                "(no shell interpolation allowed for runnable argv)"
            )

    def as_dict(self) -> dict:
        d = {
            "action": self.action,
            "why": self.why,
            "evidence": self.evidence,
            "concept_id": self.concept_id,
            "confidence": self.confidence,
            "agent_instruction": self.agent_instruction,
            "argv": list(self.argv) if self.argv else None,
            "argv_template": list(self.argv_template) if self.argv_template else None,
            "command": self.command,
            "command_template": self.command_template,
            "shell": self.shell,
            "context": dict(self.context),
        }
        # P2-10 (iter-1): include op_payload ONLY when populated so template
        # actions (op_payload is None) preserve the exact §6.1 envelope shape
        # and old consumers never see a null op_payload key. Round-trip safe:
        # load via ``.get("op_payload")`` reconstructs None when absent.
        if self.op_payload is not None:
            d["op_payload"] = dict(self.op_payload)
        return d


@dataclass
class Plan:
    """A collection of :class:`PlannedAction`\\s for a bundle (current spec §7)."""

    bundle_root: str | None
    actions: list[PlannedAction] = field(default_factory=list)
    # P1-19: discovery suggestions that did not become actions, paired with a
    # short machine-readable reason. Mirrors update._actions_to_ops'
    # skipped-reason discipline: nothing is silently dropped.
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        """Return a JSON-serialisable dict for ``okf plan --out`` / ``--format json``.

        Shape::

            {"bundle_root": str|None, "total": int,
             "by_action": {verb: count},   # explicitly sorted (P2-14)
             "actions": [PlannedAction.as_dict(), ...],
             "skipped": {"total": int,
                         "items": [{"rule": str, "reason": str}, ...]}}

        Portability note (P2-3): when :func:`build_plan` was called with
        ``portable=True``, ``bundle_root == "."`` and every action's
        ``argv`` uses ``"."`` as the bundle argument. Operators MUST run
        the emitted commands from the bundle directory (the chdir
        precondition); otherwise the relative path will not resolve.
        The default (non-portable) mode emits absolute paths so argv is
        runnable as-is from any CWD (current spec §7: "executable,
        shell:false — runnable as-is").
        """
        by_action: dict[str, int] = {}
        for a in self.actions:
            by_action[a.action] = by_action.get(a.action, 0) + 1
        # P2-14: explicit sort for deterministic output (§3.2).
        by_action = {k: by_action[k] for k in sorted(by_action)}
        return {
            "bundle_root": self.bundle_root,
            "total": len(self.actions),
            "by_action": by_action,
            "actions": [a.as_dict() for a in self.actions],
            # P1-19: surface dropped suggestions with reasons (no silent drops).
            "skipped": {
                "total": len(self.skipped),
                "items": [
                    {"rule": rule, "reason": reason}
                    for (rule, reason) in self.skipped
                ],
            },
        }


# ---------------------------------------------------------------------------
# Suggestion → PlannedAction mapping
# ---------------------------------------------------------------------------

# Maps discovery rule names to action verbs.
_RULE_TO_ACTION: dict[str, str] = {
    "unlinked_mentions": "add_link",
    "missing_descriptions": "add_description",
    "missing_indexes": "create_index",
    "broken_links": "fix_broken_link",
    "missing_relations_hint": "add_relation",
    "orphan_concepts": "connect",
}


def _suggestion_to_action(
    bundle_arg: str,
    suggestion: Any,
) -> PlannedAction | tuple[Any, str]:
    """Convert one discovery :class:`Suggestion` to a :class:`PlannedAction`.

    Returns either:
        * a :class:`PlannedAction` (the suggestion became an actionable step), or
        * a ``(suggestion, reason)`` tuple when the suggestion cannot be turned
          into an action. Callers MUST accumulate these into ``Plan.skipped``
          so no suggestion is silently dropped (P1-19 discipline, mirroring
          ``update._actions_to_ops``).
    """
    rule = suggestion.rule
    action_verb = _RULE_TO_ACTION.get(rule)
    if action_verb is None:
        return suggestion, f"unhandled_rule:{rule}"

    cid_str = (
        concept_id_to_str(suggestion.concept_id) if suggestion.concept_id else None
    )
    target_str = (
        concept_id_to_str(suggestion.target_concept_id)
        if suggestion.target_concept_id
        else None
    )
    detail = suggestion.detail or {}

    if action_verb == "add_link":
        if not (target_str and cid_str):
            return suggestion, "missing_target_or_source"
        # Mechanical: add a link from source to target.
        label = detail.get("label") or target_str.split("/")[-1]
        argv = [
            "okf", "link-add",
            "--bundle", bundle_arg,
            "--source", cid_str,
            "--target", target_str,
            "--label", label,
        ]
        # P2-10 (iter-1): structured payload consumed directly by
        # ``update._actions_to_ops`` (no argv re-parse). Precompute the label
        # default here so the consumer needs no fallback logic — this is the
        # exact value the argv path's ``label or target.split('/')[-1]``
        # resolves to, guaranteeing parity between the two paths.
        op_payload = {
            "handler": "add_link",
            "source": cid_str,
            "target": target_str,
            "label": label,
            "section": None,
        }
        return PlannedAction(
            action="add_link",
            why=f"Unlinked mention of '{target_str}' in {cid_str}",
            evidence=suggestion.message,
            concept_id=cid_str,
            argv=argv,
            command=f"okf link-add --source {cid_str} --target {target_str}",
            op_payload=op_payload,
        )

    if action_verb == "add_description":
        if not cid_str:
            return suggestion, "missing_concept_id"
        # Needs agent input: what should the description say?
        argv_template = [
            "okf", "set-frontmatter",
            "--bundle", bundle_arg,
            "--id", cid_str,
            "--key", "description",
            "--value", "<DESCRIPTION>",
        ]
        return PlannedAction(
            action="add_description",
            why=f"Concept {cid_str} is missing a description",
            evidence=suggestion.message,
            concept_id=cid_str,
            confidence=0.8,
            agent_instruction="Write a concise (1-2 sentence) description for this concept.",
            argv_template=argv_template,
            command_template=f"okf set-frontmatter --id {cid_str} --key description --value \"<DESCRIPTION>\"",
        )

    if action_verb == "create_index":
        # Mechanical: regenerate indexes.
        directory = detail.get("directory", ".")
        # P1-15: ``okf index`` takes a POSITIONAL bundle, not --bundle.
        argv = [
            "okf", "index",
            bundle_arg,
        ]
        # P2-10 (iter-1): structured payload. Index regeneration is applied via
        # ``okf index`` directly (not an UpdateOp), so ``_actions_to_ops``
        # skips these with reason ``run_okf_index_directly`` — but the
        # structured directory is carried so a future in-process consumer
        # could apply it without re-parsing argv or the ``why`` string.
        op_payload = {
            "handler": "regenerate_indexes",
            "directory": directory,
        }
        return PlannedAction(
            action="create_index",
            why=f"Missing or stale index for directory: {directory}",
            evidence=suggestion.message,
            concept_id=None,
            argv=argv,
            command=f"okf index {bundle_arg}",
            # P2-10: structured directory for dedup; do not parse ``why``.
            context={"directory": directory},
            op_payload=op_payload,
        )

    if action_verb == "fix_broken_link":
        if not cid_str:
            return suggestion, "missing_concept_id"
        target_raw = detail.get("target_raw", "<UNKNOWN>")
        # Skip two categories that the validator may still surface
        # as broken (defense-in-depth — the validator now classifies most
        # of these correctly, but the planner should not emit useless
        # template actions if any leak through):
        #
        # 1. **Reserved-filename directory indexes** (``/foo/index.md``):
        #    these are valid viewer references; the link-add template
        #    would emit ``--target <TARGET_ID>`` with no clear guidance.
        stripped_target = (target_raw or "").split("#", 1)[0]
        if stripped_target.endswith("index.md"):
            return suggestion, "reserved_filename_index"
        # 2. **Out-of-bundle links** (``../../docs/foo.md``): if the
        #    detail flags this as out_of_bundle, the link escaped the
        #    bundle root. The agent's only meaningful action is to move
        #    the target into the bundle or convert to an external URL —
        #    not to ``okf link-add`` a placeholder. Skip with a reason
        #    the human/agent can read.
        if detail.get("out_of_bundle") or stripped_target.startswith("../") or "/.." in stripped_target:
            return suggestion, "out_of_bundle_link"
        # Needs agent input: what should the broken link point to?
        argv_template = [
            "okf", "link-add",
            "--bundle", bundle_arg,
            "--source", cid_str,
            "--target", "<TARGET_ID>",
            "--label", "<LABEL>",
        ]
        return PlannedAction(
            action="fix_broken_link",
            why=f"Broken link to '{target_raw}' in {cid_str}",
            evidence=suggestion.message,
            concept_id=cid_str,
            confidence=0.5,
            agent_instruction=f"Determine the correct target for the broken link '{target_raw}' or create the missing concept.",
            argv_template=argv_template,
            command_template=f"okf link-add --source {cid_str} --target <TARGET_ID>",
        )

    if action_verb == "add_relation":
        # P1-14: missing_relations_hint always emits target_concept_id=None,
        # so the previous `target_str and cid_str` gate silently dropped every
        # suggestion of this rule. Emit a template action with TWO unfilled
        # slots (<TARGET_ID> + <RELATION_TYPE>) and an agent_instruction;
        # mirror the connect/fix_broken_link template pattern.
        if not cid_str:
            return suggestion, "missing_concept_id"
        matched = detail.get("matched_text") or "a foreign-key-like column"
        argv_template = [
            "okf", "link-add",
            "--bundle", bundle_arg,
            "--source", cid_str,
            "--target", "<TARGET_ID>",
            "--relation", "<RELATION_TYPE>",
        ]
        return PlannedAction(
            action="add_relation",
            why=(
                f"Suggested relation from {cid_str} "
                f"(schema hint: {matched})"
            ),
            evidence=suggestion.message,
            concept_id=cid_str,
            confidence=0.5,
            agent_instruction=(
                "Identify the target concept and the relation type for this "
                "foreign-key-like schema hint (e.g. depends_on, written_by, "
                "references, belongs_to)."
            ),
            argv_template=argv_template,
            command_template=(
                f"okf link-add --source {cid_str} --target <TARGET_ID> "
                f"--relation <RELATION_TYPE>"
            ),
        )

    if action_verb == "connect":
        if not cid_str:
            return suggestion, "missing_concept_id"
        # Needs agent input: how to connect this orphan?
        argv_template = [
            "okf", "link-add",
            "--bundle", bundle_arg,
            "--source", "<SOURCE_ID>",
            "--target", cid_str,
            "--label", "<LABEL>",
        ]
        return PlannedAction(
            action="connect",
            why=f"Orphan concept {cid_str} has no incoming or outgoing links",
            evidence=suggestion.message,
            concept_id=cid_str,
            confidence=0.4,
            agent_instruction=f"Find a concept that should link to {cid_str} or that {cid_str} should link to.",
            argv_template=argv_template,
            command_template=f"okf link-add --source <SOURCE_ID> --target {cid_str}",
        )

    # Defensive: action_verb is in _RULE_TO_ACTION but no branch handled it.
    return suggestion, f"unhandled_verb:{action_verb}"


# ---------------------------------------------------------------------------
# Relation mirroring (current spec §7)
# ---------------------------------------------------------------------------


def _find_unmirrored_relations(bundle: Bundle) -> list[tuple[ConceptId, str, str]]:
    """Find ``relations:`` entries whose target has no markdown body link.

    Returns list of ``(concept_id, relation_type, target_str)`` tuples.
    """
    from .paths import concept_id_from_str, ConceptIdError

    results: list[tuple[ConceptId, str, str]] = []
    caps = bundle.capabilities()
    if not caps.is_active("okf.cap.typed_relations"):
        return results

    for cid, concept in sorted(bundle.concepts.items()):
        rels = concept.frontmatter.get("relations")
        if not rels or not isinstance(rels, list):
            continue
        # Collect all resolved body link target concept-ids for this concept
        body_target_cids: set = set()
        for link in concept.links(bundle_root=bundle.root):
            if link.target is not None:
                body_target_cids.add(link.target)
        for rel in rels:
            if not isinstance(rel, dict):
                continue
            target_str = rel.get("target", "")
            if not target_str:
                continue
            # Resolve the relation target to a concept-id for exact comparison.
            # P2-15: tolerate a trailing ``.md`` (mirror how body links resolve
            # ``/tables/customers.md`` -> concept-id ``tables/customers``).
            normalized_target = target_str
            if normalized_target.endswith(".md"):
                normalized_target = normalized_target[:-3]
            try:
                target_cid = concept_id_from_str(normalized_target)
            except (ConceptIdError, ValueError):
                continue
            # Check if any body link resolves to the same concept-id
            if target_cid not in body_target_cids:
                rel_type = rel.get("type", "related")
                results.append((cid, rel_type, target_str))
    return results


# ---------------------------------------------------------------------------
# Build plan (current spec §7)
# ---------------------------------------------------------------------------


def build_plan(
    bundle: Bundle,
    *,
    rules: list[str] | None = None,
    portable: bool = False,
    scope: list[str] | set[str] | None = None,
    neighbors: bool = False,
) -> Plan:
    """Build a :class:`Plan` of executable actions for ``bundle``.

    Collects (in order):
        1. Discovery suggestions → PlannedActions.
        2. Index staleness → create_index/refresh_index actions.
        3. Relation mirroring → mirror_relation actions.

    Args:
        bundle: loaded OKF bundle.
        rules: optional subset of discovery rules to run.
        portable: when True, emit bundle-relative argv ("." as the bundle
            argument) and set ``Plan.bundle_root == "."`` so a committed
            plan is portable across machines. Operators MUST run the
            emitted commands from the bundle directory (the chdir
            precondition is surfaced in the ``okf plan`` text output and
            the :meth:`Plan.as_dict` docstring). Default False keeps the
            resolved absolute path so argv is runnable as-is from any
            CWD (current spec §7).
        scope: optional set/list of concept-id strings (current spec §7 scoped
            enrichment). When given, discovery, index staleness, and relation
            mirroring are ALL restricted to the scoped concepts (and their
            graph neighbours when ``neighbors=True``). ``scope=None`` keeps
            the whole-bundle behaviour unchanged.
        neighbors: when True AND ``scope`` is given, expand the scope set to
            include direct graph neighbours (1 hop, in+out edges) before
            discovery. Ignored when ``scope`` is None.

    Returns:
        A :class:`Plan` ready for ``okf update`` or agent review.
    """
    # P2-3: --portable emits bundle-relative argv so committed plans are
    # portable across machines (no host-absolute path leaks). Default
    # (P2-9) keeps the resolved absolute path so argv is runnable as-is
    # from any CWD.
    if portable:
        bundle_arg = "."
        bundle_root = "."
    else:
        bundle_arg = str(Path(bundle.root).resolve())
        bundle_root = str(bundle.root)

    # Current spec §7 scoped enrichment: resolve the scope set to ConceptId
    # tuples and (optionally) expand it by one graph hop. ``scope_ids`` stays
    # None when scope is None so the whole-bundle behaviour is unchanged.
    scope_ids: set[ConceptId] | None = None
    if scope is not None:
        scope_ids = set()
        for s in scope:
            s = s.strip() if isinstance(s, str) else s
            if not s:
                continue
            try:
                scope_ids.add(concept_id_from_str(s))
            except (ConceptIdError, ValueError):
                continue  # ignore an unparseable scope id; never crash.
        if neighbors and scope_ids:
            graph = bundle.graph()
            expanded = set(scope_ids)
            for cid in list(scope_ids):
                expanded |= graph.neighbours(cid, max_depth=1)
            scope_ids = expanded
    # discover_suggestions takes id strings (slash form).
    discover_scope = (
        {concept_id_to_str(c) for c in scope_ids}
        if scope_ids is not None
        else None
    )

    actions: list[PlannedAction] = []
    skipped: list[tuple[str, str]] = []

    # 1. Discovery suggestions
    report = discover_suggestions(bundle, rules=rules, scope=discover_scope)
    for suggestion in report.suggestions:
        result = _suggestion_to_action(bundle_arg, suggestion)
        if isinstance(result, PlannedAction):
            actions.append(result)
        else:
            # P1-19: (suggestion, reason) tuple — accumulate, never drop.
            _, reason = result
            skipped.append((suggestion.rule, reason))

    # Scoped plans also restrict index staleness + relation mirroring so the
    # emitted plan is genuinely scoped (not "discovery scoped but everything
    # else global"). Compute the set of directories that hold a scoped
    # concept; index actions are kept only for those directories.
    scoped_dirs: set[str] | None = None
    if scope_ids is not None:
        scoped_dirs = set()
        for cid in scope_ids:
            concept = bundle.concepts.get(cid)
            if concept is not None:
                scoped_dirs.add(str(concept.rel_path.parent))

    # 2. Index staleness (skip directories already covered by discovery's
    # create_index). P2-10: read the directory from PlannedAction.context
    # (structured), not by parsing the human-readable ``why`` string.
    covered_dirs: set[str] = set()
    for a in actions:
        if a.action == "create_index":
            d = a.context.get("directory")
            if d:
                covered_dirs.add(d)
    index_plan = plan_index_regeneration(bundle)
    for rel_path in index_plan.get("would_write", []):
        # Skip if discovery already flagged this directory's index as missing
        dir_of = str(Path(rel_path).parent) if Path(rel_path).parent != Path(".") else "."
        if dir_of in covered_dirs or rel_path in covered_dirs:
            continue
        # §11: when scoped, skip index work outside the scoped directories.
        if scoped_dirs is not None and dir_of not in scoped_dirs:
            continue
        # P1-15: ``okf index`` takes a POSITIONAL bundle, not --bundle.
        argv = ["okf", "index", bundle_arg]
        actions.append(
            PlannedAction(
                action="refresh_index",
                why=f"Index is stale or missing: {rel_path}",
                evidence=f"plan_index_regeneration: would_write {rel_path}",
                concept_id=None,
                argv=argv,
                command=f"okf index {bundle_arg}",
                context={"directory": dir_of, "rel_path": rel_path},
                # P2-10 (iter-1): structured payload (see create_index above).
                op_payload={
                    "handler": "regenerate_indexes",
                    "directory": dir_of,
                    "rel_path": rel_path,
                },
            )
        )

    # 3. Relation mirroring
    for cid, rel_type, target_str in _find_unmirrored_relations(bundle):
        # §11: when scoped, skip relations whose source concept is out of scope.
        if scope_ids is not None and cid not in scope_ids:
            continue
        cid_str = concept_id_to_str(cid)
        # P1-5 (iter-3): emit a NORMALIZED target in argv so the action
        # converges. _find_unmirrored_relations strips a trailing .md only for
        # the comparison (so a relation to tables/customers.md is detected as
        # mirrored when a body link to tables/customers exists), but the raw
        # target_str (with .md and/or a leading slash) was emitted in argv,
        # making _parse_argv/concept_id_from_str produce a phantom segment
        # (e.g. ('tables','customers.md')) → target_concept_not_found every
        # run → infinite non-converging loop. Normalize here.
        norm_target = target_str
        if norm_target.startswith("/"):
            norm_target = norm_target[1:]
        if norm_target.endswith(".md"):
            norm_target = norm_target[:-3]
        argv = [
            "okf", "link-add",
            "--bundle", bundle_arg,
            "--source", cid_str,
            "--target", norm_target,
            "--relation", rel_type,
        ]
        # P2-10 (iter-1): structured payload consumed directly by
        # ``update._actions_to_ops``. mirror_relation maps to TWO UpdateOps
        # (add_link + add_relation); the payload's ``handler == "add_link"``
        # drives the link op and the presence of ``relation`` drives the
        # relation op. The label is precomputed to the exact value the argv
        # path resolves to (``target.split('/')[-1]``; argv has no --label) so
        # the two paths produce identical ops — the drift class of bug that
        # caused P1-5 is now structurally impossible for new plans.
        op_payload = {
            "handler": "add_link",
            "source": cid_str,
            "target": norm_target,
            "label": norm_target.split("/")[-1],
            "relation": rel_type,
            "section": None,
        }
        actions.append(
            PlannedAction(
                action="mirror_relation",
                why=f"Typed relation '{rel_type}' to '{target_str}' has no body link in {cid_str}",
                evidence=f"relations: [{rel_type} → {target_str}] without markdown link",
                concept_id=cid_str,
                argv=argv,
                command=f"okf link-add --source {cid_str} --target {norm_target} --relation {rel_type}",
                op_payload=op_payload,
            )
        )

    return Plan(bundle_root=bundle_root, actions=actions, skipped=skipped)


__all__ = [
    "PlannedAction",
    "Plan",
    "build_plan",
]
