"""Proofs for the friction-report improvements (2026-07 feedback round).

Covers, in feedback-priority order:

1. Block-level partial updates: ``update_section`` / ``replace_text``
   handlers + CLI verbs (no more whole-body reconstruction).
2. Mid-thread replies: ``okf comment-reply`` keeps the parent open,
   never hands the agent its own reply back via ``wait``.
3. ``write-concept`` defaults (resource + generated provenance for new content).
4. Runtime tunnel attach: ``POST /__tunnel`` + ``okf tunnel``.
5. Nits: ``wait`` queue visibility; presence ``message`` updates;
   user-side comment body editing over ``/__comment-update``.
"""
from __future__ import annotations

import json
import re
import secrets
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.server import OKFWikiHandler, _stop_public_ingress
from okf_loom.studio import Studio, wait_for_work
from okf_loom.update import UpdateOp, UpdatePlan, apply_plan, write_concept


# ---------------------------------------------------------------------------
# Helpers (mirror test_update.py / test_live.py idioms)
# ---------------------------------------------------------------------------


def _make_bundle(tmp_path: Path) -> Path:
    (tmp_path / "a.md").write_text(
        "---\n"
        "type: T\n"
        "title: AAA\n"
        "custom_key: preserve_me\n"
        "---\n"
        "# A\n"
        "\n"
        "intro prose\n"
        "\n"
        "## Banking\n"
        "\n"
        "old banking line\n"
        "\n"
        "### Banking detail\n"
        "\n"
        "detail line\n"
        "\n"
        "## Shipping\n"
        "\n"
        "shipping line\n",
        encoding="utf-8",
    )
    return tmp_path


def _plan(ops: list[UpdateOp]) -> UpdatePlan:
    return UpdatePlan(bundle_root=Path("."), description="t", ops=ops,
                      plan_kind="update")


def _op(kind: str, args: dict) -> UpdateOp:
    return UpdateOp(kind, ("a",), args)


def _run_cli(*args: str) -> tuple[int, str, str]:
    from okf_loom.cli import main
    out_buf: list[str] = []
    err_buf: list[str] = []

    class _W:
        def write(self, s):
            out_buf.append(s)

        def flush(self):
            pass

    class _E:
        def write(self, s):
            err_buf.append(s)

        def flush(self):
            pass

    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _W(), _E()
    try:
        rc = main(list(args))
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, "".join(out_buf), "".join(err_buf)


# ---------------------------------------------------------------------------
# 1a. update_section handler
# ---------------------------------------------------------------------------


def test_update_section_replaces_only_that_block(tmp_path: Path) -> None:
    b = Bundle.load(_make_bundle(tmp_path))
    s = apply_plan(b, _plan([_op("update_section", {
        "heading": "## Banking", "body": "new banking content",
    })]))
    assert s["applied"] == 1
    body = b.concept_at("a").body
    assert "new banking content" in body
    assert "old banking line" not in body
    # Subsections belong to their parent section and are replaced with it.
    assert "Banking detail" not in body
    # Everything else survives.
    assert "intro prose" in body and "shipping line" in body
    # Frontmatter untouched (incl. unknown key), file round-trips.
    raw = (tmp_path / "a.md").read_text(encoding="utf-8")
    assert "custom_key: preserve_me" in raw


def test_update_section_missing_heading_fails_closed(tmp_path: Path) -> None:
    b = Bundle.load(_make_bundle(tmp_path))
    s = apply_plan(b, _plan([_op("update_section", {
        "heading": "## Nope", "body": "x",
    })]))
    assert s["applied"] == 0
    assert s["results"][0][1]["reason"] == "section_not_found"


def test_update_section_create_if_missing(tmp_path: Path) -> None:
    b = Bundle.load(_make_bundle(tmp_path))
    s = apply_plan(b, _plan([_op("update_section", {
        "heading": "## Notes", "body": "a note", "create_if_missing": True,
    })]))
    assert s["applied"] == 1
    assert s["results"][0][1]["reason"] == "section_created"
    assert "## Notes" in b.concept_at("a").body


def test_update_section_ambiguous_fails_and_level_pin_disambiguates(
    tmp_path: Path,
) -> None:
    root = _make_bundle(tmp_path)
    p = root / "a.md"
    p.write_text(
        p.read_text(encoding="utf-8") + "\n### Shipping\n\nnested shipping\n",
        encoding="utf-8",
    )
    b = Bundle.load(root)
    s = apply_plan(b, _plan([_op("update_section", {
        "heading": "Shipping", "body": "x",
    })]))
    assert s["applied"] == 0
    assert s["results"][0][1]["reason"].startswith("section_ambiguous:")
    # Pinning the level picks exactly one.
    s2 = apply_plan(b, _plan([_op("update_section", {
        "heading": "### Shipping", "body": "replaced nested",
    })]))
    assert s2["applied"] == 1
    assert "replaced nested" in b.concept_at("a").body


def test_update_section_append_mode(tmp_path: Path) -> None:
    b = Bundle.load(_make_bundle(tmp_path))
    s = apply_plan(b, _plan([_op("update_section", {
        "heading": "## Shipping", "body": "appended line", "mode": "append",
    })]))
    assert s["applied"] == 1
    body = b.concept_at("a").body
    assert "shipping line" in body  # append keeps existing content
    assert "appended line" in body
    assert body.index("shipping line") < body.index("appended line")


def test_update_section_idempotent_on_second_run(tmp_path: Path) -> None:
    b = Bundle.load(_make_bundle(tmp_path))
    op_args = {"heading": "## Banking", "body": "stable content"}
    s1 = apply_plan(b, _plan([_op("update_section", dict(op_args))]))
    s2 = apply_plan(b, _plan([_op("update_section", dict(op_args))]))
    assert s1["applied"] == 1
    assert s2["applied"] == 0
    assert s2["results"][0][1]["reason"] == "section_unchanged"


def test_update_section_strips_duplicated_heading_in_fragment(
    tmp_path: Path,
) -> None:
    b = Bundle.load(_make_bundle(tmp_path))
    apply_plan(b, _plan([_op("update_section", {
        "heading": "## Banking", "body": "## Banking\n\nfrag body",
    })]))
    body = b.concept_at("a").body
    assert body.count("## Banking") == 1
    assert "frag body" in body


def test_update_section_ignores_headings_in_fenced_code(tmp_path: Path) -> None:
    root = tmp_path
    (root / "a.md").write_text(
        "---\ntype: T\n---\n# A\n\n```\n## Banking\n```\n\n## Banking\n\nreal\n",
        encoding="utf-8",
    )
    b = Bundle.load(root)
    # Only ONE real Banking heading → no ambiguity.
    s = apply_plan(b, _plan([_op("update_section", {
        "heading": "## Banking", "body": "patched",
    })]))
    assert s["applied"] == 1
    body = b.concept_at("a").body
    assert "patched" in body and "real" not in body
    assert "```\n## Banking\n```" in body  # fenced content untouched


# ---------------------------------------------------------------------------
# 1b. replace_text handler
# ---------------------------------------------------------------------------


def test_replace_text_single_occurrence(tmp_path: Path) -> None:
    b = Bundle.load(_make_bundle(tmp_path))
    s = apply_plan(b, _plan([_op("replace_text", {
        "old": "intro prose", "new": "intro prose v2",
    })]))
    assert s["applied"] == 1
    assert "intro prose v2" in b.concept_at("a").body


def test_replace_text_not_found_fails_closed(tmp_path: Path) -> None:
    b = Bundle.load(_make_bundle(tmp_path))
    s = apply_plan(b, _plan([_op("replace_text", {
        "old": "zz-not-here", "new": "x",
    })]))
    assert s["applied"] == 0
    assert s["results"][0][1]["reason"] == "text_not_found"


def test_replace_text_ambiguous_fails_unless_all(tmp_path: Path) -> None:
    b = Bundle.load(_make_bundle(tmp_path))
    s = apply_plan(b, _plan([_op("replace_text", {"old": "line", "new": "row"})]))
    assert s["applied"] == 0
    assert s["results"][0][1]["reason"].startswith("text_ambiguous:")
    s2 = apply_plan(b, _plan([_op("replace_text", {
        "old": "line", "new": "row", "all": True,
    })]))
    assert s2["applied"] == 1
    assert "line" not in b.concept_at("a").body


def test_replace_text_same_value_noop(tmp_path: Path) -> None:
    b = Bundle.load(_make_bundle(tmp_path))
    s = apply_plan(b, _plan([_op("replace_text", {
        "old": "intro prose", "new": "intro prose",
    })]))
    assert s["applied"] == 0
    assert s["results"][0][1]["reason"] == "same_value"


# ---------------------------------------------------------------------------
# 1c. CLI verbs + exit codes (SPEC §3.7 classes)
# ---------------------------------------------------------------------------


def test_cli_update_section_round_trip(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    rc, out, err = _run_cli(
        "update-section", "--bundle", str(root), "--id", "a",
        "--heading", "## Banking", "--body", "cli content",
    )
    assert rc == 0, f"stdout={out!r} stderr={err!r}"
    assert "cli content" in (root / "a.md").read_text(encoding="utf-8")


def test_cli_update_section_missing_heading_exits_1(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    rc, _, _ = _run_cli(
        "update-section", "--bundle", str(root), "--id", "a",
        "--heading", "## Nope", "--body", "x",
    )
    assert rc == 1


def test_cli_update_section_requires_exactly_one_body_source(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    rc, _, err = _run_cli(
        "update-section", "--bundle", str(root), "--id", "a",
        "--heading", "## Banking",
    )
    assert rc == 2
    assert "--body" in err


def test_cli_replace_text_ambiguous_exits_1(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    rc, _, _ = _run_cli(
        "replace-text", "--bundle", str(root), "--id", "a",
        "--old", "line", "--new", "row",
    )
    assert rc == 1


def test_cli_replace_text_from_files(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    (tmp_path / "old.txt").write_text("shipping line", encoding="utf-8")
    (tmp_path / "new.txt").write_text("shipping row", encoding="utf-8")
    rc, _, err = _run_cli(
        "replace-text", "--bundle", str(root), "--id", "a",
        "--old-file", str(tmp_path / "old.txt"),
        "--new-file", str(tmp_path / "new.txt"),
    )
    assert rc == 0, err
    assert "shipping row" in (root / "a.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 3. write-concept strict-friendly defaults
# ---------------------------------------------------------------------------


_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_write_concept_create_defaults_resource_and_generated(tmp_path: Path) -> None:
    res = write_concept(tmp_path, "topics/fresh", type="Reference",
                        title="Fresh", description="d", tags=["t"])
    assert res["status"] == "created"
    assert res["defaults_applied"] == ["resource", "generated"]
    from okf_loom.parse import parse_document
    fm, _ = parse_document((tmp_path / "topics/fresh.md").read_text(encoding="utf-8"))
    assert fm["resource"] == "/topics/fresh.md"
    assert _ISO_Z.match(str(fm["generated"]["at"])), fm["generated"]
    assert fm["generated"]["by"] == "okf-loom/2.0.0"
    assert "timestamp" not in fm


def test_write_concept_explicit_values_beat_defaults(tmp_path: Path) -> None:
    res = write_concept(tmp_path, "t/x", type="T",
                        resource="https://example.com/x",
                        timestamp="2020-01-01T00:00:00Z")
    assert "defaults_applied" not in res
    from okf_loom.parse import parse_document
    fm, _ = parse_document((tmp_path / "t/x.md").read_text(encoding="utf-8"))
    assert fm["resource"] == "https://example.com/x"
    assert str(fm["timestamp"]).startswith("2020-01-01")
    assert "generated" not in fm  # Explicit legacy evidence is not tool provenance.


def test_write_concept_no_defaults_opt_out(tmp_path: Path) -> None:
    res = write_concept(tmp_path, "t/bare", type="T", defaults=False)
    assert "defaults_applied" not in res
    from okf_loom.parse import parse_document
    fm, _ = parse_document((tmp_path / "t/bare.md").read_text(encoding="utf-8"))
    assert "resource" not in fm and "timestamp" not in fm


def test_write_concept_update_never_injects_defaults(tmp_path: Path) -> None:
    (tmp_path / "u.md").write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")
    res = write_concept(tmp_path, "u", type="T", title="New Title")
    assert res["status"] == "updated"
    from okf_loom.parse import parse_document
    fm, _ = parse_document((tmp_path / "u.md").read_text(encoding="utf-8"))
    assert "resource" not in fm and "timestamp" not in fm


def test_write_concept_created_passes_strict_recommended_keys(tmp_path: Path) -> None:
    write_concept(tmp_path, "topics/full", type="Reference",
                  title="Full", description="Fully dressed", tags=["ok"])
    from okf_loom.validate import validate_bundle
    report = validate_bundle(Bundle.load(tmp_path))
    flagged = [f for f in report.findings
               if f.code == "concept.missing_recommended_keys"
               and "topics/full" in str(f.path)]
    assert flagged == []


def test_cli_write_concept_no_defaults_flag(tmp_path: Path) -> None:
    rc, out, err = _run_cli(
        "write-concept", "--bundle", str(tmp_path), "--id", "c/bare",
        "--type", "T", "--no-defaults", "--format", "json",
    )
    assert rc == 0, err
    payload = json.loads(out)
    assert "defaults_applied" not in payload
    raw = (tmp_path / "c/bare.md").read_text(encoding="utf-8")
    assert "resource" not in raw and "timestamp" not in raw


# ---------------------------------------------------------------------------
# 2. comment-reply (mid-thread channel)
# ---------------------------------------------------------------------------


@pytest.fixture
def studio_bundle(tmp_path: Path) -> Path:
    root = _make_bundle(tmp_path)
    studio = Studio.for_bundle(root)
    studio.ensure_session()
    return root


def test_comment_reply_keeps_parent_open(studio_bundle: Path) -> None:
    studio = Studio.for_bundle(studio_bundle)
    parent = studio.post_comment(concept="a", body="please clarify X")
    rc, out, err = _run_cli(
        "comment-reply", str(studio_bundle), parent["id"],
        "--body", "did you mean X1 or X2?", "--format", "json",
    )
    assert rc == 0, err
    reply = json.loads(out)
    assert reply["parent_id"] == parent["id"]
    assert reply["actor"] == "agent"
    # The reply is a statement, not an ask: posted resolved.
    assert reply["state"] == "resolved"
    # The parent stays open — reply-without-resolve is the whole point.
    assert studio.get_comment(parent["id"])["state"] == "open"


def test_comment_reply_never_returned_by_wait(studio_bundle: Path) -> None:
    studio = Studio.for_bundle(studio_bundle)
    parent = studio.post_comment(concept="a", body="the ask")
    _run_cli("comment-reply", str(studio_bundle), parent["id"], "--body", "a reply")
    item = wait_for_work(studio_bundle, kinds=("comment",), timeout=0.1)
    assert item is not None
    assert item["id"] == parent["id"]  # the ask, never the agent's own reply


def test_comment_reply_to_reply_hoists_to_root(studio_bundle: Path) -> None:
    studio = Studio.for_bundle(studio_bundle)
    parent = studio.post_comment(concept="a", body="root ask")
    first = studio.post_comment(concept="a", body="user reply",
                                parent_id=parent["id"])
    rc, out, _ = _run_cli(
        "comment-reply", str(studio_bundle), first["id"],
        "--body", "agent answer", "--format", "json",
    )
    assert rc == 0
    assert json.loads(out)["parent_id"] == parent["id"]


def test_comment_reply_missing_comment_exits_1(studio_bundle: Path) -> None:
    rc, _, err = _run_cli(
        "comment-reply", str(studio_bundle), "NO_SUCH", "--body", "x",
    )
    assert rc == 1
    assert "no such comment" in err


def test_comment_reply_rejects_named_actor_before_any_mutation(
    studio_bundle: Path,
) -> None:
    """A created receipt's actor is the closed user|agent role.

    Named identities (used for claim/resolve attribution) must fail with an
    actionable usage error at the CLI boundary — before any directive row is
    created or the parent changes — not with the durable validator's opaque
    "unavailable" after partial work.
    """
    studio = Studio.for_bundle(studio_bundle)
    parent = studio.post_comment(concept="a", body="the ask")
    before = studio.list_comments()
    with pytest.raises(SystemExit) as parsed:
        _run_cli(
            "comment-reply", str(studio_bundle), parent["id"],
            "--body", "clarify?", "--actor", "aic/Polaris",
        )
    assert parsed.value.code == 2
    # No row may be created, and the parent ask must be untouched.
    assert studio.list_comments() == before
    assert studio.get_comment(parent["id"])["state"] == "open"


def test_comment_reply_supports_both_documented_create_roles(
    studio_bundle: Path,
) -> None:
    studio = Studio.for_bundle(studio_bundle)
    parent = studio.post_comment(concept="a", body="the ask")
    for actor in ("user", "agent"):
        rc, out, err = _run_cli(
            "comment-reply", str(studio_bundle), parent["id"],
            "--body", f"reply as {actor}", "--actor", actor,
            "--format", "json",
        )
        assert rc == 0, err
        reply = json.loads(out)
        assert reply["actor"] == actor
        assert reply["state"] == "resolved"
        # A reply never resolves the parent it answers.
        assert studio.get_comment(parent["id"])["state"] == "open"


def test_post_comment_sse_event_carries_parent_and_actor(studio_bundle: Path) -> None:
    studio = Studio.for_bundle(studio_bundle)
    parent = studio.post_comment(concept="a", body="root")
    studio.post_comment(concept="a", body="reply", actor="agent",
                        parent_id=parent["id"], state="resolved")
    events = [e for e in studio.read_events(limit=20) if e.get("type") == "comment"]
    reply_events = [e for e in events if e.get("parent_id") == parent["id"]]
    assert reply_events, "reply comment event must carry parent_id"
    assert reply_events[-1]["actor"] == "agent"


# ---------------------------------------------------------------------------
# 5a. wait queue visibility
# ---------------------------------------------------------------------------


def test_wait_reports_queue_of_other_open_comments(studio_bundle: Path) -> None:
    studio = Studio.for_bundle(studio_bundle)
    first = studio.post_comment(concept="a", body="first ask")
    second = studio.post_comment(concept="a", body="second ask")
    item = wait_for_work(studio_bundle, kinds=("comment",), timeout=0.1)
    assert item["id"] == first["id"]  # FIFO backlog drain
    assert item["queue"]["pending"] == 1
    assert item["queue"]["ids"] == [second["id"]]


def test_wait_queue_empty_when_single_comment(studio_bundle: Path) -> None:
    studio = Studio.for_bundle(studio_bundle)
    studio.post_comment(concept="a", body="only ask")
    item = wait_for_work(studio_bundle, kinds=("comment",), timeout=0.1)
    assert item["queue"] == {"pending": 0, "ids": []}


# ---------------------------------------------------------------------------
# 5b. presence message
# ---------------------------------------------------------------------------


def test_set_presence_message_round_trip(studio_bundle: Path) -> None:
    studio = Studio.for_bundle(studio_bundle)
    p = studio.set_presence(state="editing", focus="a",
                            message="linking 3 of 7 tables…")
    assert p["message"] == "linking 3 of 7 tables…"
    on_disk = json.loads(studio.presence_path.read_text(encoding="utf-8"))
    assert on_disk["message"] == "linking 3 of 7 tables…"
    # The presence event carries it too (live tabs).
    ev = [e for e in studio.read_events(limit=10) if e.get("type") == "presence"]
    assert ev[-1]["message"] == "linking 3 of 7 tables…"


def test_set_presence_message_capped_at_200(studio_bundle: Path) -> None:
    studio = Studio.for_bundle(studio_bundle)
    p = studio.set_presence(state="editing", message="x" * 500)
    assert len(p["message"]) <= 201  # 200 + ellipsis


def test_cli_presence_message(studio_bundle: Path) -> None:
    rc, out, err = _run_cli(
        "presence", str(studio_bundle), "--state", "editing",
        "--message", "pass 2 of 3", "--format", "json",
    )
    assert rc == 0, err
    assert json.loads(out)["message"] == "pass 2 of 3"


def test_comment_claim_presence_carries_ask(studio_bundle: Path) -> None:
    studio = Studio.for_bundle(studio_bundle)
    c = studio.post_comment(concept="a", body="rename the Banking section")
    rc, _, err = _run_cli(
        "comment-claim", str(studio_bundle), c["id"],
        "--summary", "renaming Banking",
    )
    assert rc == 0, err
    # The CLI ran in its own Studio instance; the cross-process channel is
    # presence.json (same as the serve process's ARCH5-001 mtime check).
    on_disk = json.loads(studio.presence_path.read_text(encoding="utf-8"))
    assert on_disk.get("message") == "renaming Banking"
    assert on_disk.get("state") == "editing"


def test_comment_claim_with_named_actor_still_flips_presence(studio_bundle: Path) -> None:
    # A truthful --actor names the claimer on the comment; presence shows the
    # agent role (it used to fail silently, so the chip never showed editing).
    studio = Studio.for_bundle(studio_bundle)
    c = studio.post_comment(concept="a", body="link the tables")
    rc, _, err = _run_cli(
        "comment-claim", str(studio_bundle), c["id"],
        "--actor", "aic/Polaris", "--summary", "linking tables",
    )
    assert rc == 0, err
    on_disk = json.loads(studio.presence_path.read_text(encoding="utf-8"))
    assert (on_disk.get("actor"), on_disk.get("state"), on_disk.get("focus")) == ("agent", "editing", "a")
    assert on_disk.get("message") == "linking tables"
    assert studio.get_comment(c["id"])["claimed_by"] == "aic/Polaris"


def test_cli_presence_named_actor_shows_as_agent(studio_bundle: Path) -> None:
    rc, out, err = _run_cli(
        "presence", str(studio_bundle), "--state", "editing",
        "--actor", "claude", "--format", "json",
    )
    assert rc == 0, err
    assert json.loads(out)["actor"] == "agent"


# ---------------------------------------------------------------------------
# HTTP surface: /__comment-update body edit, /__presence message, /__tunnel
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _studio_server(bundle: Bundle, *, port: int, studio_edit: bool = True):
    server = ThreadingHTTPServer(("127.0.0.1", port), OKFWikiHandler)
    server.bundle = bundle
    server.config = {}
    server.name = bundle.name
    server.plugin = None
    server._state_lock = threading.Lock()
    server._reload_error = None
    studio = Studio.for_bundle(bundle.root, bundle_name=bundle.name)
    studio.ensure_session()
    server.studio = studio
    server.csrf_token = secrets.token_hex(16)
    server.allowed_hosts = ("127.0.0.1", "localhost")
    server.base_allowed_hosts = ("127.0.0.1", "localhost")
    server.tunnel_proc = None
    server.tunnel_url = None
    server.public_tunnel_edit = False
    server._tunnel_lock = threading.Lock()
    server.max_sse_clients = 32
    server.studio_live = True
    server.studio_edit = studio_edit
    server.studio_theme = "auto"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, studio


def _post(base: str, path: str, body: dict, token: str | None, *,
          host: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-OKF-Token"] = token
    if host is not None:
        headers["Host"] = host
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(), method="POST",
        headers=headers,
    )
    try:
        r = urllib.request.urlopen(req, timeout=3)
        return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or "{}")
        except (ValueError, OSError):
            return e.code, {}


@pytest.fixture
def live(tmp_path: Path):
    bundle = Bundle.load(_make_bundle(tmp_path))
    port = _free_port()
    server, thread, studio = _studio_server(bundle, port=port)
    yield f"http://127.0.0.1:{port}", server, studio
    _stop_public_ingress(server)
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def test_http_comment_body_edit(live) -> None:
    base, server, studio = live
    c = studio.post_comment(concept="a", body="typo'd ask")
    code, resp = _post(base, "/__comment-update",
                       {"id": c["id"], "expected_row_token": c["row_token"],
                        "body": "corrected ask"},
                       server.csrf_token)
    assert code == 200
    assert resp["comment"]["body"] == "corrected ask"
    # request_summary re-derives from the corrected body.
    assert resp["comment"]["request_summary"] == "corrected ask"
    assert studio.get_comment(c["id"])["body"] == "corrected ask"


def test_http_comment_body_edit_rejected_on_resolved(live) -> None:
    base, server, studio = live
    c = studio.post_comment(concept="a", body="done ask")
    studio.resolve_comment(c["id"])
    latest = studio.get_comment(c["id"])
    code, resp = _post(base, "/__comment-update",
                       {"id": c["id"], "expected_row_token": latest["row_token"],
                        "body": "rewrite history"},
                       server.csrf_token)
    assert code == 409
    assert "resolved" in resp["error"]


def test_http_comment_body_edit_rejects_empty(live) -> None:
    base, server, studio = live
    c = studio.post_comment(concept="a", body="ask")
    code, _ = _post(base, "/__comment-update",
                    {"id": c["id"], "expected_row_token": c["row_token"],
                     "body": "   "}, server.csrf_token)
    assert code == 400


def test_http_presence_message(live) -> None:
    base, server, _ = live
    code, resp = _post(base, "/__presence",
                       {"state": "editing", "message": "step 1 of 4"},
                       server.csrf_token)
    assert code == 200
    assert resp["presence"]["message"] == "step 1 of 4"


def test_http_tunnel_status_start_stop(live, monkeypatch) -> None:
    base, server, studio = live

    class _FakeProc:
        def __init__(self) -> None:
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True

    fake = _FakeProc()
    monkeypatch.setattr(
        "okf_loom.server.start_quick_tunnel",
        lambda port, **kw: (fake, "https://fake-quick.trycloudflare.com"),
    )
    token = server.csrf_token

    code, resp = _post(base, "/__tunnel", {"action": "status"}, token)
    assert (code, resp["url"]) == (200, None)

    code, resp = _post(base, "/__tunnel", {"action": "start"}, token)
    assert code == 200
    assert resp["url"] == "https://fake-quick.trycloudflare.com"
    # cloudflared targets a distinct loopback listener; the local/admin
    # listener's Host allowlist is never widened to a public hostname.
    assert server.public_ingress_server.server_address[1] != server.server_address[1]
    assert server.allowed_hosts == ("127.0.0.1", "localhost")
    # server.json records the tunnel for `okf tunnel --status` and friends.
    state = json.loads(studio.server_state_path.read_text(encoding="utf-8"))
    assert state["tunnel_url"] == "https://fake-quick.trycloudflare.com"
    assert state["tunnel_public_edit"] is False

    # Idempotent second start.
    code, resp = _post(base, "/__tunnel", {"action": "start"}, token)
    assert resp.get("already_running") is True

    code, resp = _post(base, "/__tunnel", {"action": "stop"}, token)
    assert code == 200 and resp["stopped"] is True
    assert fake.terminated is True
    assert server.allowed_hosts == ("127.0.0.1", "localhost")
    state = json.loads(studio.server_state_path.read_text(encoding="utf-8"))
    assert state["tunnel_url"] is None


def test_public_tunnel_defaults_read_only_without_exposing_token(live, monkeypatch) -> None:
    base, server, studio = live

    class _FakeProc:
        def terminate(self) -> None:
            pass

    public_host = "readonly-quick.trycloudflare.com"
    monkeypatch.setattr(
        "okf_loom.server.start_quick_tunnel",
        lambda port, **kw: (_FakeProc(), f"https://{public_host}"),
    )
    code, _ = _post(base, "/__tunnel", {"action": "start"}, server.csrf_token)
    assert code == 200

    public_base = (
        f"http://127.0.0.1:{server.public_ingress_server.server_address[1]}"
    )
    req = urllib.request.Request(public_base + "/", headers={"Host": public_host})
    html = urllib.request.urlopen(req, timeout=3).read().decode()
    assert server.csrf_token not in html
    assert '"edit": false' in html

    before = studio.list_comments()
    code, _ = _post(
        public_base, "/__comment?variant=1", {"concept": "a", "body": "blocked"},
        server.csrf_token, host=public_host,
    )
    assert code == 403
    assert studio.list_comments() == before

    req = urllib.request.Request(
        public_base + "/__proposal/variant?x=1", data=b"{}", method="PUT",
        headers={"Host": public_host.upper() + ".", "Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=3)
    assert exc.value.code == 403

    # The same session remains editable through its loopback URL.
    code, _ = _post(base, "/__comment", {"concept": "a", "body": "local"},
                    server.csrf_token)
    assert code == 201


def test_public_tunnel_edit_requires_explicit_attach_opt_in(live, monkeypatch) -> None:
    base, server, _ = live

    class _FakeProc:
        def terminate(self) -> None:
            pass

    public_host = "editable-quick.trycloudflare.com"
    monkeypatch.setattr(
        "okf_loom.server.start_quick_tunnel",
        lambda port, **kw: (_FakeProc(), f"https://{public_host}"),
    )
    code, resp = _post(
        base, "/__tunnel",
        {"action": "start", "allow_public_edit": True}, server.csrf_token,
    )
    assert code == 200 and resp["public_edit"] is True

    public_base = (
        f"http://127.0.0.1:{server.public_ingress_server.server_address[1]}"
    )
    req = urllib.request.Request(public_base + "/", headers={"Host": public_host})
    html = urllib.request.urlopen(req, timeout=3).read().decode()
    assert server.csrf_token not in html
    assert server.public_mutation_token in html
    assert '"edit": true' in html

    code, _ = _post(
        public_base, "/__comment", {"concept": "a", "body": "public opt-in"},
        server.public_mutation_token, host=public_host,
    )
    assert code == 201


@pytest.mark.parametrize("payload", [
    {"action": ""},
    {"action": " start "},
    {"action": "start/../stop"},
    {"action": "start", "allow_public_edit": "true"},
    {"action": "status", "allow_public_edit": True},
    {"action": "start", "unexpected": True},
])
def test_tunnel_public_edit_input_fails_closed(live, payload) -> None:
    base, server, _ = live
    code, _ = _post(base, "/__tunnel", payload, server.csrf_token)
    assert code == 400


@pytest.mark.parametrize("raw", [
    b"[]",
    b'{"action":"start","action":"stop"}',
    b'{"action":"start","allow_public_edit":true,"allow_public_edit":false}',
])
def test_tunnel_request_shape_fails_closed(live, raw: bytes) -> None:
    base, server, _ = live
    req = urllib.request.Request(
        base + "/__tunnel", data=raw, method="POST",
        headers={"Content-Type": "application/json",
                 "X-OKF-Token": server.csrf_token},
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=3)
    assert exc.value.code == 400


def test_running_tunnel_access_cannot_be_escalated_in_place(live, monkeypatch) -> None:
    base, server, _ = live

    class _FakeProc:
        def terminate(self) -> None:
            pass

    monkeypatch.setattr(
        "okf_loom.server.start_quick_tunnel",
        lambda port, **kw: (_FakeProc(), "https://fixed-quick.trycloudflare.com"),
    )
    code, _ = _post(base, "/__tunnel", {"action": "start"}, server.csrf_token)
    assert code == 200
    code, _ = _post(
        base, "/__tunnel", {"action": "start", "allow_public_edit": True},
        server.csrf_token,
    )
    assert code == 409
    assert server.public_tunnel_edit is False


def test_http_tunnel_requires_token(live) -> None:
    base, _, _ = live
    code, _ = _post(base, "/__tunnel", {"action": "status"}, token=None)
    assert code == 403


def test_http_tunnel_allowed_in_no_edit_kiosk(tmp_path: Path) -> None:
    bundle = Bundle.load(_make_bundle(tmp_path))
    port = _free_port()
    server, thread, studio = _studio_server(bundle, port=port, studio_edit=False)
    base = f"http://127.0.0.1:{port}"
    try:
        # Bundle mutation stays blocked…
        code, _ = _post(base, "/__comment", {"concept": "a", "body": "x"},
                        server.csrf_token)
        assert code == 403
        # …but tunnel admin works: sharing a read-only kiosk is the use case.
        code, resp = _post(base, "/__tunnel", {"action": "status"},
                           server.csrf_token)
        assert (code, resp["url"]) == (200, None)
        code, _ = _post(
            base, "/__tunnel",
            {"action": "start", "allow_public_edit": True}, server.csrf_token,
        )
        assert code == 409
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_cli_tunnel_without_session_exits_2(tmp_path: Path) -> None:
    root = _make_bundle(tmp_path)
    rc, _, err = _run_cli("tunnel", str(root))
    assert rc == 2
    assert "no live studio server" in err


def test_cli_tunnel_drives_running_server(live, monkeypatch, tmp_path: Path) -> None:
    base, server, studio = live

    class _FakeProc:
        def terminate(self) -> None:
            pass

    monkeypatch.setattr(
        "okf_loom.server.start_quick_tunnel",
        lambda port, **kw: (_FakeProc(), "https://cli-fake.trycloudflare.com"),
    )
    # Simulate what run_server does at startup: server.json + .token.
    from okf_loom.server import write_server_state
    write_server_state(server, studio)
    studio.token_path.write_text(server.csrf_token, encoding="utf-8")

    rc, out, err = _run_cli("tunnel", str(studio.bundle_root), "--format", "json")
    assert rc == 0, err
    assert json.loads(out)["url"] == "https://cli-fake.trycloudflare.com"

    rc, out, _ = _run_cli("tunnel", str(studio.bundle_root), "--status",
                          "--format", "json")
    assert json.loads(out)["url"] == "https://cli-fake.trycloudflare.com"

    rc, out, _ = _run_cli("tunnel", str(studio.bundle_root), "--stop",
                          "--format", "json")
    assert rc == 0
    assert json.loads(out)["stopped"] is True

    rc, out, err = _run_cli(
        "tunnel", str(studio.bundle_root), "--allow-tunnel-edit",
    )
    assert rc == 0, err
    assert "Anyone with this URL can comment and mutate" in out
    assert "gives anyone with the public URL" in err
    assert server.public_tunnel_edit is True

    rc, _, err = _run_cli(
        "tunnel", str(studio.bundle_root), "--status", "--allow-tunnel-edit",
    )
    assert rc == 2
    assert "valid only when starting" in err
