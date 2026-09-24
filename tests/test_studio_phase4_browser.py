"""Focused Phase 4 Studio workflow contracts and browser proof."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import okf_module_argv, okf_subprocess_env
from okf_loom.server import OKFWikiHandler
from okf_loom.studio import Studio

from browser_support import expect, sync_playwright, launch_chromium, copy_bundle, live_server

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "samples" / "demo_bundle"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait(base: str, proc: subprocess.Popen) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail("Studio exited before becoming ready")
        try:
            with urllib.request.urlopen(base + "/", timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    pytest.fail("Studio did not become ready")


@pytest.fixture(scope="module")
def studio_url(tmp_path_factory) -> str:
    bundle = copy_bundle(DEMO, tmp_path_factory.mktemp("phase4") / "bundle")
    with live_server(bundle) as base:
        yield base


@pytest.fixture(scope="module")
def readonly_url(tmp_path_factory) -> str:
    bundle = copy_bundle(DEMO, tmp_path_factory.mktemp("phase4-readonly") / "bundle")
    with live_server(bundle, "--no-edit") as base:
        yield base


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        try:
            yield context.new_page()
        finally:
            context.close()
            browser.close()


def _open(page, url: str) -> None:
    page.goto(url + "/tables/orders", wait_until="load")
    page.wait_for_function("() => window.okfLoomStudio && window.okfLoomStudio.state.commentsReady")
    page.get_by_role("button", name="Studio", exact=False).click()
    expect(page.get_by_role("heading", name="Studio", exact=True)).to_be_visible()


def test_bootstrap_reports_honest_proposal_capability_without_private_token(tmp_path: Path) -> None:
    handler = object.__new__(OKFWikiHandler)
    handler.server = SimpleNamespace(
        studio=Studio.for_bundle(tmp_path), csrf_token="studio-token", studio_live=True,
        studio_edit=True, studio_theme="auto", proposal_service=None,
        proposal_token="never-in-html",
    )
    inactive = handler._studio_bootstrap()
    assert '"proposal_review"' in inactive
    assert '"active": false' in inactive
    assert "never-in-html" not in inactive

    handler.server.proposal_service = object()
    active = handler._studio_bootstrap()
    assert '"active": true' in active
    assert '"status": "experimental"' in active
    assert "never-in-html" not in active


@pytest.mark.browser
def test_idle_home_has_action_oriented_empty_states_and_explicit_quick_actions(studio_url: str, page) -> None:
    _open(page, studio_url)
    launcher = page.get_by_role("button", name="Studio", exact=False)
    expect(launcher).to_have_attribute("data-attention", "idle")
    # R3/D4: the launcher names the session's permission; "Live" is kept for
    # the connection chip, which means connected.
    expect(launcher).to_contain_text("Editable")
    expect(launcher).not_to_contain_text("Live")
    page.evaluate("""() => {
        window.okfLoomStudio.state.comments = [];
        window.okfLoomStudio.state.commentsReady = true;
        window.okfLoomStudio.state.events = [];
        window.okfLoomStudio.state.eventsReady = true;
        window.okfLoomStudio.state.commentsStatus = 'ready';
    }""")

    page.locator("#okf-panel").get_by_role("button", name="Comments", exact=False).click()
    expect(page.get_by_text("No comments yet", exact=False)).to_be_visible()
    expect(page.get_by_role("button", name="Select text to comment")).to_be_visible()
    page.get_by_role("button", name="Back to Studio").click()

    page.get_by_role("button", name="Changes", exact=False).click()
    expect(page.get_by_text("No changes yet", exact=False)).to_be_visible()
    expect(page.get_by_role('button', name='About authoring')).to_be_visible()
    page.get_by_role("button", name="Back to Studio").click()

    expect(page.get_by_role("heading", name="Ask the agent to improve this concept")).to_be_visible()
    intents = page.locator("section[aria-labelledby='okf-studio-intents-title']")
    expect(intents).to_contain_text("Each action starts a comment with a suggested request.")
    quick = page.locator(".okf-studio-quick-action")
    expect(quick).to_have_count(4)
    for index in range(4):
        expect(quick.nth(index)).to_contain_text("Starts a comment")
        expect(quick.nth(index)).to_contain_text("does not edit immediately")
    # Intents start comments; their outcomes never borrow X3 proposal wording.
    outcomes = page.locator(".okf-studio-quick-action__outcome").all_inner_texts()
    assert len(outcomes) == 4
    assert not [text for text in outcomes if re.search(r"propos|review", text, re.IGNORECASE)], outcomes
    expect(page.get_by_role("button", name="Suggest an enrichment", exact=True)).to_have_accessible_description(
        "Starts a comment · Asks the agent to describe the intended addition first. This does not edit immediately.")

    expect(page.locator("[data-proposal-state='unavailable']")).to_contain_text("Experimental")
    expect(page.locator("[data-proposal-state='unavailable']")).to_contain_text("private capability")


PANEL_PURPOSES = [
    ("Comments", "Requests and discussion saved on this bundle for readers and its agent. Exact annotations "
                 "are pinned to text; page comments are about the whole page."),
    ("Changes", "What actually happened: saved changes to the bundle, newest first. Comments and presence "
                "marks are not listed here."),
    ("Agent activity", "Evidence of recent agent work: comments an agent claimed, reported status changes, and "
                       "agent writes in the last 5 minutes. A status mark is not proof an agent is running."),
]


@pytest.mark.browser
@pytest.mark.parametrize("width", [1280, 390])
def test_collaboration_panels_state_their_purpose_and_studio_home_does_not(studio_url: str, page, width: int) -> None:
    """R3: each collaboration panel says, under its title, what question it
    answers; the panel is described by that sentence only while it shows."""
    page.set_viewport_size({"width": width, "height": 900})
    _open(page, studio_url)
    shell = page.locator("#okf-panel")
    purpose = page.locator("#okf-panel-purpose")
    expect(shell).to_have_accessible_description("")
    expect(purpose).to_be_hidden()
    for name, sentence in PANEL_PURPOSES:
        shell.get_by_role("button", name=name, exact=False).first.click()
        expect(page.locator("#okf-panel-title")).to_have_text(name)
        expect(purpose).to_have_text(sentence)
        expect(purpose).to_be_visible()
        expect(shell).to_have_accessible_description(sentence)
        # The line sits between the title and the panel content, inside the panel.
        geometry = page.evaluate("""() => {
          const r = (s) => document.querySelector(s).getBoundingClientRect();
          const p = r('#okf-panel-purpose'), t = r('#okf-panel-title'), b = r('#okf-panel-body'), s = r('#okf-panel');
          return {below: p.top >= t.bottom - 1, above: p.bottom <= b.top + 1,
                  inside: p.left >= s.left - 1 && p.right <= s.right + 1,
                  overflow: document.documentElement.scrollWidth > innerWidth + 1};
        }""")
        assert geometry == {"below": True, "above": True, "inside": True, "overflow": False}, geometry
        page.get_by_role("button", name="Back to Studio").click()
        expect(page.locator("#okf-panel-title")).to_have_text("Studio")
        expect(purpose).to_be_hidden()
        expect(shell).to_have_accessible_description("")


@pytest.mark.browser
def test_loading_error_retry_and_nonempty_states_are_visible(studio_url: str, page) -> None:
    _open(page, studio_url)
    page.evaluate("""() => {
        const api = window.okfLoomStudio;
        api.openPanel('comments');
        api.state.commentsReady = false;
        api.state.commentsStatus = 'loading';
        api._renderComments();
    }""")
    expect(page.get_by_role("status", name="Loading comments")).to_be_visible()

    page.evaluate("""() => {
        const api = window.okfLoomStudio;
        api.state.commentsStatus = 'error';
        api._renderComments();
    }""")
    expect(page.get_by_role("alert")).to_contain_text("Comments could not be loaded")
    expect(page.get_by_role("button", name="Retry loading comments")).to_be_visible()

    page.evaluate("""() => {
        const api = window.okfLoomStudio;
        api.state.commentsReady = true;
        api.state.commentsStatus = 'ready';
        api.state.comments = [{id:'PHASE4COMMENT', concept:api.state.conceptId,
          anchor:{kind:'concept', ref:api.state.conceptId}, actor:'user', body:'Clarify this contract',
          state:'open', ts:new Date().toISOString()}];
        api._renderComments();
    }""")
    expect(page.get_by_text("Clarify this contract", exact=True)).to_be_visible()


@pytest.mark.browser
def test_activity_empty_state_explains_agent_setup_and_restores_studio_focus(studio_url: str, page) -> None:
    _open(page, studio_url)
    page.evaluate("""() => {
        window.okfLoomStudio.state.comments = [];
        window.okfLoomStudio.state.events = [];
        window.okfLoomStudio.state.presenceHistory = [];
    }""")
    page.get_by_role("button", name="Agent activity").click()
    expect(page.get_by_text("No agent activity yet", exact=False)).to_be_visible()
    # Studio never starts an agent, so the empty state must not offer to: it
    # reports the evidence it has and shows the external loop an agent runs.
    expect(page.get_by_role("button", name="Start Watching")).to_have_count(0)
    expect(page.locator(".okf-agent-availability")).to_contain_text("No agent has acted in this session")
    setup = page.locator("details.okf-agent-setup")
    expect(setup).to_have_attribute("open", "")
    expect(setup).to_contain_text("Studio does not start or host an agent")
    expect(setup.locator("code")).to_contain_text("scripts/okf-loom wait <bundle> --for comment")
    page.get_by_role("button", name="Back to Studio").click()
    expect(page.get_by_role("button", name="Agent activity")).to_be_focused()


@pytest.mark.browser
def test_reconnecting_is_promoted_without_fabricating_work(studio_url: str, page) -> None:
    _open(page, studio_url)
    # A late SSE ready frame would reset the launcher to Live; inject only
    # after the live connection has settled.
    page.wait_for_function("() => window.okfLoomLive && window.okfLoomLive.state === 'online'")
    page.evaluate("""() => window.okfLoomStudio._applyConnectionState('reconnecting')""")
    launcher = page.get_by_role("button", name="Studio", exact=False)
    # Reconnecting is shown on the launcher but is not a failure (only
    # offline is); it never fabricates pending work.
    expect(launcher).to_have_attribute("data-attention", "idle")
    expect(launcher).to_contain_text("Reconnecting")
    expect(launcher).not_to_contain_text("0 Comments")


@pytest.mark.browser
def test_local_read_authority_disables_session_and_mutation_actions(readonly_url: str, page) -> None:
    _open(page, readonly_url)
    expect(page.get_by_text("This local Studio is read-only", exact=False)).to_be_visible()
    expect(page.get_by_role('button', name='Watching', exact=False)).to_have_count(0)
    expect(page.get_by_text('Authoring unavailable here', exact=True)).to_be_visible()
    for action in page.locator(".okf-studio-quick-action button").all():
        expect(action).to_be_disabled()
    page.evaluate("""() => {
        window.okfLoomStudio.state.comments = [];
        window.okfLoomStudio.state.commentsReady = true;
        window.okfLoomStudio.state.commentsStatus = 'ready';
    }""")
    page.locator("#okf-panel").get_by_role("button", name="Comments", exact=False).click()
    expect(page.get_by_text("Commenting is disabled", exact=False)).to_be_visible()
    expect(page.get_by_role("link", name="Learn about Studio authority")).to_be_visible()
