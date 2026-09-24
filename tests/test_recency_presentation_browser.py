"""Browser proof for the F7 index recency sort (index-browse.js).

The "Recently changed" client ordering must follow the metadata owner's
effective change instant projected into ``data-index-instant`` (canonical
UTC): real instants first, newest first even when authored offsets make the
raw strings sort the other way; unknown-time cards last in stable card order.
Equal instants and unknown times tie-break by concept identity — the same
order the server's suggestion lists use — even when localized titles are
deliberately ordered opposite to the ids. Skips cleanly when Playwright or a
Chrome channel is unavailable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from browser_support import sync_playwright, launch_chromium, live_server

pytestmark = pytest.mark.browser

_TOOLKIT_ROOT = Path(__file__).resolve().parent.parent


def _build_recency_bundle(root: Path) -> Path:
    """Mixed-offset F7 fixture; ids double as the expected sort order basis."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text("# Recency\n\nStart here.\n", encoding="utf-8")
    concepts = {
        # Newest real instant (2026-09-02T04:00Z) although its authored string
        # sorts below c_utc's — lexical comparison would flip these two.
        "b_offset_late": 'title: B offset late\ngenerated: {by: agent/v2, at: "2026-09-01T23:00:00-05:00"}',
        "c_utc": 'title: C utc\ngenerated: {by: agent/v2, at: "2026-09-02T00:00:00+00:00"}',
        "a_gen_new": 'title: A gen new\ngenerated: {by: agent/v2, at: "2026-09-01T12:00:00+02:00"}',
        # Equal instants in different authored offsets: the client tie-break
        # must be concept identity, so the T1 card precedes T2 even though
        # the titles are ordered deliberately opposite to the ids.
        "t1_tie_alpha": ('title: Zebra\n'
                         'generated: {by: agent/v2, at: "2026-08-31T07:00:00-05:00"}'),
        "t2_tie_zulu": ('title: Alpha\n'
                        'generated: {by: agent/v2, at: "2026-08-31T12:00:00+00:00"}'),
        # Conflicting legacy timestamp must lose to generated.at.
        "d_conflict": ('title: D conflict\ngenerated: {by: agent/v2, at: "2026-08-30T00:00:00Z"}\n'
                       'timestamp: "2026-09-09T00:00:00Z"'),
        # Unknown time: empty by / invalid at must NOT fall back to legacy.
        # Their titles are also ordered opposite to the ids: the unknown-time
        # tie-break is identity, never localized title text.
        "f_empty_by": ('title: Zulu\n'
                       'generated: {by: "", at: "2026-09-10T00:00:00Z"}\n'
                       'timestamp: "2026-09-05T00:00:00Z"'),
        "g_invalid": ('title: Alpha\n'
                      'generated: {by: agent/v2, at: not-a-date}\n'
                      'timestamp: "2026-09-06T00:00:00Z"'),
        "h_undated": "title: Mike",
    }
    for stem, frontmatter in concepts.items():
        body = f"type: Note\n{frontmatter}".rstrip()
        (root / f"{stem}.md").write_text(
            f"---\n{body}\n---\n\nBody of {stem}.\n", encoding="utf-8",
        )
    return root


def test_index_recent_order_follows_effective_instant(tmp_path: Path) -> None:
    bundle = _build_recency_bundle(tmp_path / "bundle")
    with live_server(bundle) as base:
        with sync_playwright() as p:
            browser = launch_chromium(p)
            try:
                page = browser.new_context().new_page()
                page.goto(base + "/", wait_until="domcontentloaded")
                order = page.locator("[data-index-order]")
                order.wait_for(state="attached", timeout=10000)
                # The server must project the canonical UTC instant, empty for
                # unknown time (the client never re-derives dates).
                assert page.locator(
                    '.okf-card[data-concept-id="a_gen_new"]'
                ).get_attribute("data-index-instant") == "2026-09-01T10:00:00+00:00"
                # Equal instants in authored offsets canonicalize identically.
                assert page.locator(
                    '.okf-card[data-concept-id="t1_tie_alpha"]'
                ).get_attribute("data-index-instant") == "2026-08-31T12:00:00+00:00"
                assert page.locator(
                    '.okf-card[data-concept-id="t2_tie_zulu"]'
                ).get_attribute("data-index-instant") == "2026-08-31T12:00:00+00:00"
                assert page.locator(
                    '.okf-card[data-concept-id="h_undated"]'
                ).get_attribute("data-index-instant") == ""
                order.select_option("recent")
                ids = page.eval_on_selector_all(
                    ".okf-card",
                    "els => els.map(e => e.dataset.conceptId)",
                )
                expected = [
                    "b_offset_late", "c_utc", "a_gen_new",
                    # Equal instants tie-break by concept identity — NOT by
                    # title ("Zebra" < "Alpha" would put t2 first).
                    "t1_tie_alpha", "t2_tie_zulu",
                    "d_conflict",
                    # Unknown time stays last, tied on "" and ordered by
                    # identity, not by the deliberately opposite titles.
                    "f_empty_by", "g_invalid", "h_undated",
                ]
                assert ids == expected, f"unexpected client recency order: {ids}"
                # Server parity: the same bundle ordered by the server's own
                # recency helper (the suggestion lists' owner) must agree
                # with the client order, tie for tie.
                from okf_loom import Bundle
                from okf_loom.metadata import effective_instant_key
                from okf_loom.paths import concept_id_to_str
                from okf_loom.render import _recent_concepts_by_instant
                loaded = Bundle.load(bundle)
                server_ids = [concept_id_to_str(c.id)
                              for c in _recent_concepts_by_instant(loaded, limit=99)]
                assert server_ids == expected, (
                    f"server/client recency tie parity broken: {server_ids}"
                )
                assert any(effective_instant_key(c.frontmatter) == "" for c in loaded.concepts.values())
                page.evaluate("() => document.activeElement?.blur?.()")
                page.close()
            finally:
                browser.close()


# ---------------------------------------------------------------------------
# F14 — the LIVE recent rail (post-hydration rebuild + real events) must
# match the SSR rows: reader words, an explicit separator, raw data-event-*
# audit attributes, truthful relative time, and inert hostile values.
# ---------------------------------------------------------------------------

_RAIL_ROW_PROBE_JS = """(cid) => {
            const li = document.querySelector('#okf-recent-rail li[data-concept-id="' + cid + '"]');
            if (!li) return null;
            const link = li.querySelector("a.okf-recent__item");
            const span = link.querySelector("span:not(.sr-only)");
            const sep = link.querySelector(":scope > span.sr-only");
            const time = link.querySelector("time.okf-recent__time");
            return {
                action: li.getAttribute("data-event-action"),
                actor: li.getAttribute("data-event-actor"),
                phrase: span ? span.textContent : null,
                // The separator is spoken (screen-reader-only span) but never
                // painted as a stray grid item between phrase and time.
                separator: sep ? sep.textContent : null,
                bareText: Array.from(link.childNodes)
                    .filter(n => n.nodeType === 3 && n.textContent.trim() !== "")
                    .map(n => n.textContent),
                separatorPainted: sep ? sep.getBoundingClientRect().width > 1 : null,
                linkText: link.textContent,
                timeText: time ? time.textContent : null,
                datetime: time ? time.getAttribute("datetime") : null,
            };
        }"""


def _rail_row_probe(page, concept_id: str) -> dict:
    """Structural probe of one live rail row (post client rebuild)."""
    return page.evaluate(_RAIL_ROW_PROBE_JS, concept_id)


def test_live_recent_rail_reader_words_survive_hydration_and_real_events(
    tmp_path: Path,
) -> None:
    """F14 live half: the rail a real visitor sees on the serve landing page
    is rebuilt by the client after hydration and again on every delivered
    event. Both rebuilds must keep the SSR repair — reader words (never the
    raw 'disk changed' machine verb), an explicit separator before the time,
    and the raw actor/action audit attributes — with hostile actor values
    rendered as inert text."""
    import re
    import shutil

    from okf_loom.studio import Studio

    root = tmp_path / "bundle"
    shutil.copytree(_TOOLKIT_ROOT / "samples" / "demo_bundle", root)
    # Real pre-serve history through the studio funnel: a mapped verb and an
    # unknown op id (raw audit fallback — nothing invented for unknowns).
    studio = Studio.for_bundle(root)
    studio.record_activity(actor="agent", action="updated",
                           ids=["tables/orders"], summary="updated Orders",
                           origin="mutator")
    studio.record_activity(actor="cli", action="update-section",
                           ids=["tables/customers"], summary="edited customers",
                           origin="mutator")

    with live_server(root) as base:
        with sync_playwright() as p:
            browser = launch_chromium(p)
            try:
                page = browser.new_context().new_page()
                page.add_init_script(
                    """
                    window.__okfUnhandled = [];
                    window.addEventListener('unhandledrejection', (e) => {
                        window.__okfUnhandled.push(String(e.reason));
                    });
                    """
                )
                page.goto(base + "/", wait_until="domcontentloaded")
                page.wait_for_function(
                    "() => window.okfLoomStudio && window.okfLoomStudio.state.eventsReady",
                    timeout=10000,
                )
                orders = page.locator(
                    '#okf-recent-rail li[data-concept-id="tables/orders"]'
                )
                orders.wait_for(state="attached", timeout=10000)

                def assert_reader_row(probe, *, action, actor, phrase):
                    assert probe is not None, "rail row missing"
                    assert probe["action"] == action, probe
                    assert probe["actor"] == actor, probe
                    assert probe["phrase"].endswith(phrase), probe
                    # Explicit separator between the phrase and the time,
                    # present for assistive tech but not painted as a dot.
                    assert probe["separator"] == " · ", probe
                    assert probe["bareText"] == [], probe
                    assert probe["separatorPainted"] is False, probe
                    assert " · " in probe["linkText"], probe
                    assert probe["timeText"], probe
                    assert probe["datetime"], probe

                # AFTER HYDRATION: the client rebuilt the SSR rail — proven
                # by the relative time text (SSR rows print "YYYY-MM-DD
                # HH:MM UTC", the live rows print "just now"/"N… ago").
                orders_probe = _rail_row_probe(page, "tables/orders")
                assert re.search(r"ago|just now", orders_probe["timeText"]), (
                    f"row is not the client-rebuilt live row: {orders_probe}"
                )
                assert "UTC" not in orders_probe["timeText"], orders_probe
                # "updated" mapped to the reader word "edited".
                assert "edited" in orders_probe["phrase"], orders_probe
                assert "updated" not in orders_probe["phrase"], orders_probe
                assert_reader_row(orders_probe, action="updated", actor="agent",
                                  phrase="agent edited")
                # Unknown action keeps the raw audit record ("cli
                # update-section"), never an invented verb.
                assert_reader_row(_rail_row_probe(page, "tables/customers"),
                                  action="update-section", actor="cli",
                                  phrase="cli update-section")

                # AFTER A REAL DELIVERED EVENT: touch a concept on disk so
                # the watcher emits a real disk-origin changed event; the
                # rebuilt row must read "edited on disk" — the exact B8
                # wording bug — with the audit attributes retained.
                target = root / "datasets" / "orders.md"
                original = target.read_text(encoding="utf-8")
                target.write_text(
                    original + "\nDisk-touched for the F14 live-rail proof.\n",
                    encoding="utf-8",
                )
                disk_row = page.locator(
                    '#okf-recent-rail li[data-concept-id="datasets/orders"]'
                )
                disk_row.wait_for(state="attached", timeout=20000)
                disk_probe = _rail_row_probe(page, "datasets/orders")
                assert_reader_row(disk_probe, action="changed", actor="disk",
                                  phrase="edited on disk")
                assert re.search(r"ago|just now", disk_probe["timeText"]), disk_probe
                rail_text = page.locator("#okf-recent-rail").inner_text()
                assert "disk changed" not in rail_text

                # Hostile audit values stay inert: the established public
                # seam (state + _buildIndexEngagement) renders the raw value
                # as escaped TEXT and a data attribute — never markup.
                # The seam is a no-op while the disk event's own graph/comments
                # refresh is in flight (buildIndexEngagement returns early, and
                # the authoritative settle then drops a synthetic event), so
                # settle first, then inject, rebuild and probe in ONE task.
                hostile = page.evaluate(
                    """async () => {
                        const S = window.okfLoomStudio;
                        const deadline = Date.now() + 10000;
                        while (!(S.state.graphReady && S.state.commentsReady)) {
                            if (Date.now() > deadline) throw new Error("studio never settled");
                            await new Promise((resolve) => setTimeout(resolve, 20));
                        }
                        S.state.events.push({
                            event_cursor: 999901, type: "changed",
                            ids: ["services/checkout"],
                            actor: '<img src=x onerror=alert(1)>',
                            action: "changed",
                            ts: new Date().toISOString(),
                        });
                        S._buildIndexEngagement();
                        return (""" + _RAIL_ROW_PROBE_JS + """)("services/checkout");
                    }"""
                )
                assert_reader_row(hostile, action="changed",
                                  actor="<img src=x onerror=alert(1)>",
                                  phrase="<img src=x onerror=alert(1)> edited")
                assert page.locator("#okf-recent-rail img").count() == 0
                assert page.evaluate(
                    "() => window.__okfUnhandled || []"
                ) == []
                page.evaluate("() => document.activeElement?.blur?.()")
                page.close()
            finally:
                browser.close()
