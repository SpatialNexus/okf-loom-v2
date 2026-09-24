# UI overhaul strict closeout matrix

> **Historical scope:** this matrix belongs to the UI-overhaul closeout described
> below. “Current” and “final” refer to that work, not the present checkout.
> See the [maintenance plan](../../docs-bundle/explanation/maintenance_plan.md)
> and [current spec](../../docs-bundle/reference/spec.md). Its historical proof
> requirements do not gate ordinary approved maintenance. Executable artifact
> validators and outstanding human/zoom release evidence are not waived.

This matrix supersedes stale checked-in screenshot references for this user-
approved closeout only. Fresh visual evidence is intentionally written under
`/tmp/okf-ui-overhaul-final`; no `docs/screenshots` artifact is evidence for
these slices.

| Plan acceptance criteria | Runtime owner | Exact automated proof | Fresh capture target |
|---|---|---|---|
| Phase 3 — graph visual language: fcose/SRI, collision-safe labels and orphan shelf, typed shaped/icon nodes, typed directed edges and arrowheads | `viewer/static/graph.js`; `render.py`; `viewer/templates/graph_page.html` | `test_graph_phase3_sparse_initial_and_reset_fit_are_label_safe_and_parity`; `test_graph_export_24_node_geometry_and_label_backplates_are_bounded`; typed-edge and canonical-export browser proofs | `graph-default.png`, `graph-nojs-export.png` |
| Phase 4 — graph interaction: hover, selected action bar, focus, successful typed path, keyboard, Lenses filters, zoom/minimap | `viewer/static/graph.js` interaction/path/filter owners; `viewer/static/graph.css` | `test_phase4_slice2_real_shift_pointer_path_is_typed_filtered_and_layered`; `test_successful_path_capture_precondition_resets_and_accepts_only_typed_found_path`; `test_successful_path_capture_precondition_rejects_unavailable_or_untyped_state`; directional edge-flow, focus, keyboard and filter proofs | `graph-hover.png`, `graph-focus.png`, `graph-path.png`, `graph-lenses-advanced.png` |
| Phase 5 — liveness: graph SSE updates, presence, tour, recent changes and comment-count engagement | `viewer/static/graph.js`; `viewer/static/live.js`; `viewer/static/studio.js`; `render.py` | `test_phase4_final_phase5_sse_presence_tour_compatibility_boundary`; live refresh/presence tests; comment-count ownership/browser proofs | `connections-active-light.png`, `connections-active-focus.png`, `connections-active-dark.png`, `connections-active-mobile.png` |
| 4.5 legend merged into one Lenses owner with counted Only/Show all type/edge controls; sparse initial/reset fit is label-safe across live/static/single; mobile canvas/action ownership | `viewer/static/graph.js::buildSignalPanel`, `updateLegend`, `postLayout`, and `overlayAwareFit` | `test_graph_phase3_sparse_initial_and_reset_fit_are_label_safe_and_parity`; `test_graph_phase3_mobile_small_graph_uses_canvas_and_bottom_viewport_row`; `test_graph_phase3_r3_mobile_sheet_isolated_title_complete_and_focus_restored`; filter identity/history/runtime-parity tests | `graph-default.png`, `graph-mobile.png` |
| 3.2/4.6 one canonical hero/static SVG export with stable positions, shapes, icons, edge styles and collision-free count chips | `render.py::graph_export_svg` | `test_graph_export_is_the_exact_shared_semantic_artifact`; `test_graph_export_hostile_values_are_escaped_and_order_independent`; `test_graph_export_24_node_geometry_and_label_backplates_are_bounded`; `test_graph_export_real_multi_edge_chip_clearance_or_semantic_equivalent` | `index-light.png`, `index-mobile.png`, `graph-nojs-export.png` |
| deterministic capture, current tree, no stale screenshots | `scripts/capture_ui_overhaul.py` plus production readiness owner | `tests/test_capture_ui_overhaul.py`; manifest HEAD/tree/worktree and inventory digests | `/tmp/okf-ui-overhaul-final/manifest.json` |

Capture supersession is evidence-location-only. It does not relax browser,
keyboard, forced-colour, reduced-motion, static, single-file, no-JavaScript,
hostile-value, lifecycle, or source/runtime parity requirements.

The manifest's `worktree_digest` and `input_inventory_digest` fields may bind
the capture to an exact **dirty** working tree (including its tracked diff and
inventory); they do not claim that tree is clean. Its `head` field records only
the committed base revision. A dirty-worktree manifest is valid current-tree
capture evidence, but it is not committed-manifest proof and must not be
described as clean. The manifest and captures must therefore be regenerated
after the eventual commit.
No 400% browser/text-zoom pass is claimed without direct actual-zoom proof.

## ASYNC_LIFECYCLE_MATRIX: directional capture lease

| Scenario | Deadline/abort owner | Mutation phase | Public result | Persisted state | Late result | Retry | Proof |
|---|---|---|---|---|---|---|---|
| successful graph screenshot | capture consumer | lease immediately before screenshot | artifact published | complete image only | matching release resumes selected flow | fresh token | `test_capture_success_uses_production_owner_and_atomic_artifact`; browser flow test |
| screenshot failure/timeout | capture consumer absolute budget | lease held only around screenshot | typed capture failure | prior artifact unchanged | `finally` releases matching token | fresh token | `test_screenshot_write_failure_is_distinct_and_cleans_partial_files`; timeout test |
| superseded capture | destination generation + edge lease generation | publication fenced | `superseded` | newer owner only | stale release cannot resume newer lease | newer owner token | browser stale-release assertion; cross-process capture tests |
| pagehide/abort | page lifecycle owner | lease invalidated before departure | no stale success | unchanged | stale release rejected | restored page starts fresh | `test_graph_edge_flow_pagehide_invalidates_capture_lease` |
| retry after failure | new capture invocation | fresh lease | success or typed failure | atomic prior/new artifact | old token cannot release new token | idempotent fresh attempt | `test_capture_retry_gets_fresh_edge_flow_lease_and_releases_each_attempt` |
| initial sparse seed → layout → fit → ready | seed writes geometry only; current layout generation owns settlement; `overlayAwareFit` owns final label-aware viewport | seed: no public readiness; layout: pending; fit: post-settle | `ready` only after `fitAppliedGeneration === appliedLayoutGeneration === layoutGeneration` | none | pagehide invalidates the layout generation; held/late `layoutstop` cannot fit or publish | fresh page/load starts a new generation; explicit reset fits the settled model | `test_graph_held_initial_layout_pagehide_late_stop_and_fresh_retry_ordering` (held seed, production denial, pagehide, late stop, fresh ready); `test_graph_initial_seed_layout_fit_readiness_ordering`; sparse parity test |
| persisted BFCache suspension → restore | persisted pagehide invalidates/stops the current owner and records one suspension epoch | suspended; inventory and interaction retained | production capture returns non-authorized `superseded`/`graph-suspended` until one fresh restore generation settles and fits | none | late pre-suspension `layoutstop` is fenced; duplicate pageshow for the same epoch changes starts/generation exactly once and ready publication is state-deduped once | persisted pageshow starts exactly one fresh layout owner; terminal pagehide is not restorable | `test_graph_held_initial_layout_bfcache_restore_starts_one_fresh_owner` (10×); graph BFCache matrix and selected/focus-bar reconciliation tests |
