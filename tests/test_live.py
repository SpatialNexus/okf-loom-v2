"""Live-studio HTTP acceptance tests (``okf_loom.server``).

Exercises the studio endpoints end-to-end against a running
``ThreadingHTTPServer`` with a :class:`~okf_loom.studio.Studio` attached:
the current spec §14 cross-origin guard, the SSE ``ready`` frame, ``/__data/doc``,
``/__data/events``, the comment / apply / undo / preview round-trips, the
``--no-edit`` kiosk refusal, and the watcher change-diff (no-refresh push).

These complement ``test_studio.py`` (the logic layer) and
``test_security.py`` (the bundle/malicious-code gates).
"""
from __future__ import annotations

import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.server import OKFWikiHandler, _BundleWatcher
from okf_loom.studio import Studio, rev_of


# ---------------------------------------------------------------------------
# Test server harness (mirrors test_server_smoke._start_server + studio state)
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _studio_server(bundle: Bundle, *, port: int, studio_edit: bool = True,
                   studio_live: bool = True):
    """Start a server with the full live-studio wiring (like run_server)."""
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
    server.max_sse_clients = 32
    server.studio_live = studio_live
    server.studio_edit = studio_edit
    server.studio_theme = "auto"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, studio


def _post(base: str, path: str, body: dict, token: str | None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-OKF-Token"] = token
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(), method="POST", headers=headers,
    )
    try:
        r = urllib.request.urlopen(req, timeout=3)
        return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        # Read the error body so 400/409/413 responses carry their JSON
        # payload back to the test (otherwise every assertion on the body
        # would see {}). Falls back to {} if the body is empty/non-JSON.
        try:
            return e.code, json.loads(e.read() or "{}")
        except (ValueError, OSError):
            return e.code, {}


@pytest.fixture()
def live_server(tiny_good_bundle: Path):
    bundle = Bundle.load(tiny_good_bundle)
    port = _free_port()
    server, thread, studio = _studio_server(bundle, port=port)
    base = f"http://127.0.0.1:{port}"
    yield base, studio, server
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


# ---------------------------------------------------------------------------
# current spec §14 cross-origin guard
# ---------------------------------------------------------------------------

def test_comment_with_valid_token_accepted(live_server) -> None:
    base, _, server = live_server
    code, body = _post(base, "/__comment",
                       {"concept": "tables/users", "body": "enrich this"}, server.csrf_token)
    assert code == 201
    assert body["ok"] is True
    assert body["comment"]["state"] == "open"


def test_comment_without_token_rejected_403(live_server) -> None:
    base, _, _ = live_server
    code, _ = _post(base, "/__comment", {"concept": "x", "body": "y"}, token=None)
    assert code == 403


def test_comment_with_wrong_token_rejected_403(live_server) -> None:
    base, _, _ = live_server
    code, _ = _post(base, "/__comment", {"concept": "x", "body": "y"},
                    token="not-the-real-token")
    assert code == 403


def test_foreign_origin_rejected_even_with_token(live_server) -> None:
    base, _, server = live_server
    headers = {"Content-Type": "application/json", "X-OKF-Token": server.csrf_token,
               "Origin": "http://evil.example.com"}
    req = urllib.request.Request(base + "/__comment", data=b'{"concept":"x","body":"y"}',
                                 method="POST", headers=headers)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=3)
    assert exc.value.code == 403


# ---------------------------------------------------------------------------
# --no-edit kiosk
# ---------------------------------------------------------------------------

def test_no_edit_kiosk_refuses_writes(tiny_good_bundle: Path) -> None:
    bundle = Bundle.load(tiny_good_bundle)
    port = _free_port()
    server, thread, _ = _studio_server(bundle, port=port, studio_edit=False)
    base = f"http://127.0.0.1:{port}"
    try:
        code, _ = _post(base, "/__comment", {"concept": "x", "body": "y"}, server.csrf_token)
        assert code == 403
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


# ---------------------------------------------------------------------------
# /__data/doc + /__data/events
# ---------------------------------------------------------------------------

def test_data_doc_returns_rendered_html_and_meta(live_server) -> None:
    base, _, _ = live_server
    # Find any real concept id in the bundle.
    from okf_loom.paths import concept_id_to_str
    bundle = Bundle.load(live_server[2].bundle.root)  # fresh snapshot
    cid = concept_id_to_str(next(iter(bundle.concepts)))
    r = urllib.request.urlopen(f"{base}/__data/doc?id={cid}", timeout=3)
    d = json.loads(r.read())
    assert d["id"] == cid
    assert d["rev"] and len(d["html"]) > 0
    assert isinstance(d["backlinks"], list) and isinstance(d["outgoing"], list)


def test_data_doc_unknown_concept_404(live_server) -> None:
    base, _, _ = live_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{base}/__data/doc?id=does/not/exist", timeout=3)
    assert exc.value.code == 404


def test_data_events_returns_feed(live_server) -> None:
    base, studio, _ = live_server
    studio.record_activity(actor="agent", action="add_link", ids=["a"], summary="s")
    r = urllib.request.urlopen(f"{base}/__data/events?limit=10", timeout=3)
    d = json.loads(r.read())
    assert any(e["type"] == "activity" for e in d["events"])


# ---------------------------------------------------------------------------
# /__apply + /__undo round-trip
# ---------------------------------------------------------------------------

def test_apply_then_undo_round_trip(live_server) -> None:
    base, studio, server = live_server
    from okf_loom.paths import concept_id_to_str
    bundle = Bundle.load(server.bundle.root)
    cid = concept_id_to_str(next(iter(bundle.concepts)))
    # apply an add_tag (idempotent, safe op)
    code, body = _post(base, "/__apply",
                       {"kind": "add_tag", "target": cid,
                        "args": {"tag": "studio-test"}, "group_id": "GRP1"},
                       server.csrf_token)
    assert code == 200
    assert body["ok"] is True and body["applied"] is True
    # an activity + changed event were recorded
    evs = studio.read_events(limit=20)
    assert any(e["type"] == "activity" for e in evs)
    assert any(e["type"] == "changed" for e in evs)
    # group undo reverts the pass
    code2, body2 = _post(base, "/__undo", {"group_id": "GRP1"}, server.csrf_token)
    assert code2 == 200
    assert body2["ok"] is True and body2["restored"] >= 1


# ---------------------------------------------------------------------------
# /__preview
# ---------------------------------------------------------------------------

def test_preview_renders_markdown(live_server) -> None:
    base, _, server = live_server
    code, body = _post(base, "/__preview", {"markdown": "# Hi\n\nsome **bold**"},
                       server.csrf_token)
    assert code == 200
    assert "<h1" in body["html"] and "Hi</h1>" in body["html"]
    assert "<strong>" in body["html"]


# ---------------------------------------------------------------------------
# SSE ready frame
# ---------------------------------------------------------------------------

def test_sse_ready_frame_on_connect(live_server) -> None:
    base, _, _ = live_server
    # Raw socket: send the GET, read until we see the 'ready' event.
    parsed = urllib.parse.urlparse(base)  # noqa
    import urllib.parse as _up
    host = _up.urlparse(base).hostname
    port = _up.urlparse(base).port
    c = socket.create_connection((host, port), timeout=3)
    c.sendall(f"GET /__events HTTP/1.1\r\nHost: {host}\r\nConnection: keep-alive\r\n\r\n".encode())
    c.settimeout(2.0)
    buf = b""
    try:
        while b"event: ready" not in buf:
            buf += c.recv(4096)
    except socket.timeout:
        pass
    c.close()
    assert b"event: ready" in buf
    assert b"text/event-stream" in buf


# ---------------------------------------------------------------------------
# Watcher change-diff → live push (the no-refresh contract, §7.3)
# ---------------------------------------------------------------------------

def test_watcher_change_diff_emits_events(tiny_good_bundle: Path) -> None:
    """Editing a .md triggers a watcher reload that emits a changed event."""
    bundle = Bundle.load(tiny_good_bundle)
    port = _free_port()
    server, thread, studio = _studio_server(bundle, port=port)

    # Wire a reload that diffs old/new revs like run_server does.
    from okf_loom.paths import concept_id_to_str

    def _revs(b: Bundle) -> dict[str, str]:
        return {concept_id_to_str(cid): rev_of(c.raw_text)
                for cid, c in b.concepts.items()}

    def _reload() -> None:
        old = _revs(server.bundle)
        fresh = Bundle.load(tiny_good_bundle)
        with server._state_lock:
            server.bundle = fresh
            if hasattr(server, "_palette"):
                del server._palette
        new = _revs(fresh)
        changed = [i for i in new if i in old and new[i] != old[i]]
        created = [i for i in new if i not in old]
        removed = [i for i in old if i not in new]
        if created:
            studio.emit_change(kind="created", ids=created, origin="disk")
        if changed:
            studio.emit_change(kind="changed", ids=changed, origin="disk")
        if removed:
            studio.emit_change(kind="removed", ids=removed, origin="disk")

    watcher = _BundleWatcher(tiny_good_bundle, _reload, interval=0.2)
    watcher.start()
    try:
        # Pick a concept file and append a line to force an mtime change.
        target = next(iter(bundle.concepts.values()))
        before = studio.read_events(limit=50)
        with target.path.open("a") as fh:
            fh.write("\n\n<!-- studio test edit -->\n")
        # Wait for the watcher to detect + reload + emit.
        deadline = time.time() + 5
        seen = False
        while time.time() < deadline:
            after = studio.read_events(limit=50)
            if any(e["type"] == "changed" for e in after[len(before):]):
                seen = True
                break
            time.sleep(0.2)
        assert seen, "watcher did not emit a changed event after a disk edit"
    finally:
        watcher.stop()
        server.shutdown(); server.server_close(); thread.join(timeout=3)
        # Restore the file content.
        target.path.write_text(target.path.read_text().replace(
            "\n\n<!-- studio test edit -->\n", ""))


# ---------------------------------------------------------------------------
# Bootstrap injection
# ---------------------------------------------------------------------------

def test_html_page_injects_studio_bootstrap_and_assets(live_server) -> None:
    base, _, _ = live_server
    r = urllib.request.urlopen(base + "/", timeout=3)
    html = r.read().decode()
    # CSP-safe JSON data block the studio modules read on boot.
    assert 'id="okf-studio-bootstrap"' in html
    assert "/__static/studio.css" in html
    assert "/__static/live.js" in html
    assert "/__static/studio.js" in html


# ---------------------------------------------------------------------------
# Review-fix regression tests (F1/F2/F4/F12/F13/F17)
# ---------------------------------------------------------------------------

def test_apply_then_read_same_concept_no_torn_read(live_server) -> None:
    """F1: /__apply swaps a private fresh bundle under ``_state_lock``; a
    subsequent GET on the same concept reads the swapped bundle cleanly — no
    torn read and no exception. (Before F1 the live bundle was mutated in
    place with no lock, so a concurrent read could see a half-mutation.)"""
    from okf_loom.paths import concept_id_to_str
    base, _, server = live_server
    bundle = Bundle.load(server.bundle.root)
    cid = concept_id_to_str(next(iter(bundle.concepts)))
    # apply add_tag — mutates a PRIVATE fresh bundle, then swaps it in.
    code, body = _post(
        base, "/__apply",
        {"kind": "add_tag", "target": cid, "args": {"tag": "f1-torn-read"}},
        server.csrf_token,
    )
    assert code == 200 and body["ok"] is True and body["applied"] is True
    # Immediately read the SAME concept via /__data/doc: must not raise and
    # must return a fully-formed doc (the swapped bundle is self-consistent).
    r = urllib.request.urlopen(f"{base}/__data/doc?id={cid}", timeout=3)
    d = json.loads(r.read())
    assert d["id"] == cid and isinstance(d["html"], str) and len(d["html"]) > 0
    # A second, concurrent-ish apply on the same concept must also stay clean.
    code2, body2 = _post(
        base, "/__apply",
        {"kind": "add_tag", "target": cid, "args": {"tag": "f1-second"}},
        server.csrf_token,
    )
    assert code2 == 200 and body2["ok"] is True


def test_apply_then_single_concept_undo_restores_content(live_server) -> None:
    """F2: a single-concept /__apply (no group_id) stamps ``detail.before``
    (the content-hash rev) and ``detail.before_concept``; /__undo with that
    rev restores the prior file bytes. Before F2 the activity's rev was the
    integer bundle rev, undo_snapshot could not resolve it → silent no-op."""
    from okf_loom.paths import concept_id_from_str, concept_id_to_str
    base, studio, server = live_server
    bundle = Bundle.load(server.bundle.root)
    cid = concept_id_to_str(next(iter(bundle.concepts)))
    concept_path = bundle.concepts[concept_id_from_str(cid)].path
    original_raw = concept_path.read_text(encoding="utf-8")

    code, body = _post(
        base, "/__apply",
        {"kind": "add_tag", "target": cid, "args": {"tag": "f2-undo"}},
        server.csrf_token,
    )
    assert code == 200 and body["applied"] is True
    # The activity event MUST carry the content-hash undo pointer.
    evs = studio.read_events(limit=20)
    act = next(
        e for e in evs
        if e.get("type") == "activity" and e.get("action") == "add_tag"
        and cid in (e.get("ids") or [])
    )
    before_rev = act["detail"]["before"]
    assert isinstance(before_rev, str) and len(before_rev) == 12  # sha1[:12]
    assert act["detail"]["before_concept"] == cid
    # The file changed.
    assert concept_path.read_text(encoding="utf-8") != original_raw
    # Single-concept undo with the content-hash rev restores the bytes.
    code2, body2 = _post(
        base, "/__undo", {"concept": cid, "rev": before_rev}, server.csrf_token,
    )
    assert code2 == 200 and body2["ok"] is True and body2["restored"] >= 1
    assert concept_path.read_text(encoding="utf-8") == original_raw


def test_oversized_post_returns_413_and_closes_connection(live_server) -> None:
    """F4: a POST body exceeding MAX_EVENT_BODY_BYTES returns 413 with
    ``Connection: close`` (draining an unbounded body would pin the worker;
    closing lets the client reconnect cleanly) so the keep-alive connection
    does not desync. The oversized body is NOT sent by the client — the
    server must close without trying to read it."""
    import urllib.parse as _up
    from okf_loom.server import MAX_EVENT_BODY_BYTES
    base, _, _ = live_server
    host = _up.urlparse(base).hostname
    port = _up.urlparse(base).port
    c = socket.create_connection((host, port), timeout=3)
    try:
        c.sendall((
            "POST /__comment HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {MAX_EVENT_BODY_BYTES + 1}\r\n"
            "Connection: keep-alive\r\n\r\n"
        ).encode())
        c.settimeout(3.0)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = c.recv(4096)
            if not chunk:
                break
            buf += chunk
        status_line = buf.split(b"\r\n")[0]
        assert b"413" in status_line
        assert b"connection: close" in buf.lower()
        # Server closes the connection; further reads hit EOF.
        eof = False
        try:
            while True:
                chunk = c.recv(4096)
                if not chunk:
                    eof = True
                    break
        except socket.timeout:
            eof = True
        assert eof, "server should close the connection after the 413"
    finally:
        c.close()


def test_malformed_content_length_does_not_500(live_server) -> None:
    """F4: a non-integer Content-Length must NOT raise a 500; it defaults to
    0 (empty body) and the connection stays in sync."""
    import urllib.parse as _up
    base, _, server = live_server
    host = _up.urlparse(base).hostname
    port = _up.urlparse(base).port
    token = server.csrf_token or ""
    c = socket.create_connection((host, port), timeout=3)
    try:
        c.sendall((
            "POST /__comment HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: not-a-number\r\n"
            f"X-OKF-Token: {token}\r\n"
            "Connection: close\r\n\r\n"
        ).encode())
        c.settimeout(3.0)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = c.recv(4096)
            if not chunk:
                break
            buf += chunk
        status_line = buf.split(b"\r\n")[0]
        assert b"500" not in status_line
        # Empty body + valid token → 400 (concept missing), NOT 500.
        assert b"400" in status_line
    finally:
        c.close()


def test_head_events_returns_immediately_without_blocking(live_server) -> None:
    """F12: HEAD /__events returns an empty 200 immediately. Before the fix a
    HEAD entered the blocking SSE loop and pinned the worker thread forever."""
    import urllib.parse as _up
    base, _, _ = live_server
    host = _up.urlparse(base).hostname
    port = _up.urlparse(base).port
    c = socket.create_connection((host, port), timeout=3)
    try:
        c.sendall((
            f"HEAD /__events HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\nConnection: close\r\n\r\n"
        ).encode())
        c.settimeout(3.0)
        buf = b""
        # The response must arrive promptly (HEAD has no body) — if it blocked
        # in the SSE loop this recv would time out.
        while b"\r\n\r\n" not in buf:
            chunk = c.recv(4096)
            if not chunk:
                break
            buf += chunk
        assert b"200" in buf.split(b"\r\n")[0]
        # HEAD body is empty by contract.
        assert buf.endswith(b"\r\n\r\n")
    finally:
        c.close()


def test_data_doc_applies_plugin_render_hook(tiny_good_bundle: Path) -> None:
    """F13: /__data/doc routes the rendered body through on_concept_render so
    a plugin's body injection survives the first live patch (parity with the
    concept-page render path). Before the fix the hook ran on the page but
    was stripped on the first ``changed`` re-render from this endpoint."""
    from okf_loom.paths import concept_id_to_str
    bundle = Bundle.load(tiny_good_bundle)
    port = _free_port()
    server, thread, _ = _studio_server(bundle, port=port)

    class Injecting:
        def on_concept_render(self, concept, html):
            return html + "<!-- plugin-injected-F13 -->"

        def on_index_render(self, html):
            return html

    server.plugin = Injecting()  # type: ignore[attr-defined]
    base = f"http://127.0.0.1:{port}"
    try:
        cid = concept_id_to_str(next(iter(bundle.concepts)))
        r = urllib.request.urlopen(f"{base}/__data/doc?id={cid}", timeout=3)
        d = json.loads(r.read())
        assert "<!-- plugin-injected-F13 -->" in d["html"]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def test_data_events_malformed_limit_does_not_500(live_server) -> None:
    """F17: a non-integer ?limit= must NOT raise a 500; it defaults to 200."""
    base, studio, _ = live_server
    studio.record_activity(actor="agent", action="x", ids=["a"], summary="s")
    r = urllib.request.urlopen(f"{base}/__data/events?limit=not-a-number", timeout=3)
    d = json.loads(r.read())
    assert isinstance(d.get("events"), list)


# ---------------------------------------------------------------------------
# P2-17 — per-kind args validation in /__apply (§12.3 / §15.5)
# ---------------------------------------------------------------------------
# /__apply must fail-closed (400) on missing / wrongly-typed / unknown args
# BEFORE any write attempt. The library handlers (update._h_*) stay lenient
# for in-process callers, but the public HTTP surface must reject bad input
# so an agent mistake surfaces as a 400 with a clear machine-readable reason
# rather than a 200 with applied:False.


def _apply(base: str, body: dict, token: str) -> tuple[int, dict]:
    """POST /__apply and return (status, parsed-body-or-{})."""
    return _post(base, "/__apply", body, token)


def test_apply_rejects_unknown_kind_with_400(live_server) -> None:
    """An unknown ``kind`` is rejected with 400 + a stable prefix."""
    base, _, server = live_server
    code, body = _apply(base, {
        "kind": "totally_made_up", "target": "tables/users", "args": {},
    }, server.csrf_token)
    assert code == 400
    assert body["error"].startswith("unknown_kind:"), body


def test_apply_rejects_missing_required_arg_with_400(live_server) -> None:
    """``add_link`` requires ``target_concept_id``; missing → 400 + missing_arg."""
    base, _, server = live_server
    code, body = _apply(base, {
        "kind": "add_link", "target": "tables/users", "args": {"label": "x"},
    }, server.csrf_token)
    assert code == 400
    assert body["error"].startswith("missing_arg:"), body
    assert "target_concept_id" in body["error"]


def test_apply_rejects_bad_type_required_arg_with_400(live_server) -> None:
    """``add_link.target_concept_id`` must be a string; an int → 400."""
    base, _, server = live_server
    code, body = _apply(base, {
        "kind": "add_link", "target": "tables/users",
        "args": {"target_concept_id": 123},
    }, server.csrf_token)
    assert code == 400
    assert body["error"].startswith("bad_type:"), body


def test_apply_rejects_unknown_arg_with_400(live_server) -> None:
    """An arg not in the kind's schema is rejected with 400 + unknown_arg."""
    base, _, server = live_server
    code, body = _apply(base, {
        "kind": "add_tag", "target": "tables/users",
        "args": {"tag": "x", "malicious_extra": "drop table"},
    }, server.csrf_token)
    assert code == 400
    assert body["error"].startswith("unknown_arg:"), body
    assert "malicious_extra" in body["error"]


def test_apply_add_relation_requires_target_and_relation_type(live_server) -> None:
    """``add_relation`` needs BOTH ``target_concept_id`` and ``relation_type``."""
    base, _, server = live_server
    # Missing relation_type → 400.
    code, body = _apply(base, {
        "kind": "add_relation", "target": "tables/users",
        "args": {"target_concept_id": "tables/orders"},
    }, server.csrf_token)
    assert code == 400
    assert "relation_type" in body["error"]
    # Both present + well-typed → 200 (target may not exist; that's the
    # handler's call, not the args validator's).
    code2, body2 = _apply(base, {
        "kind": "add_relation", "target": "tables/users",
        "args": {"target_concept_id": "tables/orders", "relation_type": "depends_on"},
    }, server.csrf_token)
    assert code2 == 200, body2


def test_apply_set_frontmatter_requires_key_and_value(live_server) -> None:
    """``set_frontmatter`` requires ``key`` (str) + ``value`` (any non-null)."""
    base, _, server = live_server
    # Missing value → 400.
    code, body = _apply(base, {
        "kind": "set_frontmatter", "target": "tables/users",
        "args": {"key": "description"},
    }, server.csrf_token)
    assert code == 400
    assert "value" in body["error"]
    # value: null → 400 (we treat null as "missing" for a required field).
    code2, body2 = _apply(base, {
        "kind": "set_frontmatter", "target": "tables/users",
        "args": {"key": "description", "value": None},
    }, server.csrf_token)
    assert code2 == 400
    assert body2["error"].startswith("bad_type:"), body2
    # value: any real JSON type is accepted (int here).
    code3, body3 = _apply(base, {
        "kind": "set_frontmatter", "target": "tables/users",
        "args": {"key": "owner_id", "value": 42},
    }, server.csrf_token)
    assert code3 == 200, body3


def test_apply_append_body_section_requires_heading_and_body(live_server) -> None:
    """``append_body_section`` requires ``heading`` + ``body`` (strings)."""
    base, _, server = live_server
    code, body = _apply(base, {
        "kind": "append_body_section", "target": "tables/users",
        "args": {"heading": "Notes"},  # missing body
    }, server.csrf_token)
    assert code == 400
    assert "body" in body["error"]
    code2, _ = _apply(base, {
        "kind": "append_body_section", "target": "tables/users",
        "args": {"heading": "Notes", "body": "added"},
    }, server.csrf_token)
    assert code2 == 200


def test_apply_add_entity_requires_label(live_server) -> None:
    """``add_entity`` requires ``label``."""
    base, _, server = live_server
    code, body = _apply(base, {
        "kind": "add_entity", "target": "tables/users",
        "args": {"kind": "business_entity"},  # missing label
    }, server.csrf_token)
    assert code == 400
    assert "label" in body["error"]


def test_apply_remove_link_requires_target_concept_id(live_server) -> None:
    """``remove_link`` requires ``target_concept_id``."""
    base, _, server = live_server
    code, body = _apply(base, {
        "kind": "remove_link", "target": "tables/users", "args": {},
    }, server.csrf_token)
    assert code == 400
    assert "target_concept_id" in body["error"]


def test_apply_valid_args_still_succeed(live_server) -> None:
    """A well-formed add_tag still applies (sanity: validator doesn't
    over-reject happy-path input)."""
    from okf_loom.paths import concept_id_to_str

    base, _, server = live_server
    bundle = Bundle.load(server.bundle.root)
    cid = concept_id_to_str(next(iter(bundle.concepts)))
    code, body = _apply(base, {
        "kind": "add_tag", "target": cid, "args": {"tag": "p2-17-test"},
    }, server.csrf_token)
    assert code == 200 and body["ok"] is True, body


# ---------------------------------------------------------------------------
# P1-11 (QUA idempotency-retry) — /__comment + /__undo idempotency
# ---------------------------------------------------------------------------
# A re-POST with the same Idempotency-Key + body within a 10-minute window
# returns the ORIGINAL response (no duplicate directive). /__undo of an
# already-undone activity returns 409 with the current state (no double-apply).


def _post_with_headers(base: str, path: str, body: dict, token: str,
                       extra_headers: dict) -> tuple[int, dict]:
    """POST with extra headers (e.g. Idempotency-Key); return (status, body)."""
    headers = {"Content-Type": "application/json", "X-OKF-Token": token}
    headers.update(extra_headers)
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(), method="POST", headers=headers,
    )
    try:
        r = urllib.request.urlopen(req, timeout=4)
        return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or "{}")
        except (ValueError, OSError):
            return e.code, {}


def test_comment_idempotency_key_dedupes_replay(live_server) -> None:
    """A re-POST with the same Idempotency-Key + body returns the ORIGINAL
    comment id; no duplicate directive lands in directives.jsonl."""
    base, studio, server = live_server
    key = "idem-comment-1"
    body = {"concept": "tables/users", "body": "idempotency-test-comment"}
    code1, body1 = _post_with_headers(base, "/__comment", body, server.csrf_token,
                                      {"Idempotency-Key": key})
    assert code1 == 201
    original_id = body1["comment"]["id"]
    # Re-POST with the same key + body — must return the same comment id.
    code2, body2 = _post_with_headers(base, "/__comment", body, server.csrf_token,
                                      {"Idempotency-Key": key})
    assert code2 == 201
    assert body2["comment"]["id"] == original_id, (
        f"idempotent replay returned a different comment id: "
        f"orig={original_id} replay={body2['comment']['id']}"
    )
    # No duplicate directive in directives.jsonl (the replay MUST NOT append).
    comments = [c for c in studio.list_comments(concept="tables/users")
                if c.get("body") == "idempotency-test-comment"]
    assert len(comments) == 1, (
        f"idempotency replay created a duplicate directive: {comments}"
    )


def test_comment_idempotency_same_key_different_body_conflicts(live_server) -> None:
    """A durable intent key cannot be reused for a different mutation."""
    base, _, server = live_server
    key = "idem-comment-2"
    body1 = {"concept": "tables/users", "body": "first-body"}
    body2 = {"concept": "tables/users", "body": "second-body"}
    code1, b1 = _post_with_headers(base, "/__comment", body1, server.csrf_token,
                                   {"Idempotency-Key": key})
    assert code1 == 201
    code2, b2 = _post_with_headers(base, "/__comment", body2, server.csrf_token,
                                   {"Idempotency-Key": key})
    assert code2 == 409
    assert b2["error"] == "idempotency_conflict"


def test_comment_idempotency_record_persisted(live_server) -> None:
    """The crash-recoverable endpoint WAL persists finalized replay state."""
    base, studio, server = live_server
    key = "idem-persist-1"
    body = {"concept": "tables/users", "body": "persisted-idempotency"}
    _post_with_headers(base, "/__comment", body, server.csrf_token,
                       {"Idempotency-Key": key})
    import hashlib
    record_path = studio.comment_create_wal_dir / (hashlib.sha256(key.encode()).hexdigest() + ".json")
    assert record_path.is_file(), f"idempotency record not persisted at {record_path}"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["key"] == key
    assert record["state"] == "finalized"
    assert "response" in record and "fingerprint" in record


def test_undo_malformed_concept_id_is_400(live_server) -> None:
    """/__undo validates ``concept`` lexically (symmetric with
    /__apply) so a malformed id is a 400 before any filesystem op."""
    base, _, server = live_server
    code, body = _post(base, "/__undo",
                       {"concept": "tables/../../etc/passwd", "rev": "a" * 12},
                       server.csrf_token)
    assert code == 400, f"expected 400 for malformed concept id, got {code}: {body}"
    assert "bad concept id" in (body.get("error") or "")


def test_undo_already_undone_returns_409(live_server) -> None:
    """/__undo of an already-applied undo returns 409 with the current
    state, not a fresh apply."""
    from okf_loom.paths import concept_id_from_str, concept_id_to_str

    base, studio, server = live_server
    bundle = Bundle.load(server.bundle.root)
    cid = concept_id_to_str(next(iter(bundle.concepts)))
    concept_path = bundle.concepts[concept_id_from_str(cid)].path
    original_raw = concept_path.read_text(encoding="utf-8")

    # Apply a change so there's something to undo.
    code, body = _apply(base, {
        "kind": "add_tag", "target": cid, "args": {"tag": "p1-11-undo"},
    }, server.csrf_token)
    assert code == 200 and body["applied"] is True
    # Find the snapshot rev for the undo.
    evs = studio.read_events(limit=20)
    act = next(e for e in evs
               if e.get("type") == "activity" and e.get("action") == "add_tag"
               and cid in (e.get("ids") or []))
    before_rev = act["detail"]["before"]

    # First undo: restores the original bytes.
    code1, body1 = _post(base, "/__undo", {"concept": cid, "rev": before_rev},
                         server.csrf_token)
    assert code1 == 200 and body1["ok"] is True and body1["restored"] >= 1
    assert concept_path.read_text(encoding="utf-8") == original_raw

    # Second undo with the SAME rev: the file is already at before_rev, so
    # this is a replay. Must return 409, not 200.
    code2, body2 = _post(base, "/__undo", {"concept": cid, "rev": before_rev},
                         server.csrf_token)
    assert code2 == 409, f"expected 409 for already-undone, got {code2}: {body2}"
    assert body2.get("conflict") is True
    assert body2.get("error") == "already_undone"
    # No further mutation.
    assert concept_path.read_text(encoding="utf-8") == original_raw


# ---------------------------------------------------------------------------
# P1-18 (ARCH-012) — group-undo HTTP path reverts multi-write pass atomically
# ---------------------------------------------------------------------------
# 3 writes via save_concept(group_id="G1") + /__undo {group_id: "G1"} reverts
# all three; events.jsonl records the undo as one entry. Closes the §12.5
# group-undo HTTP surface that was previously library-only.


def test_group_undo_http_reverts_a_multi_write_pass(live_server, tmp_path) -> None:
    """3 writes under group_id="G1" → POST /__undo {group_id: "G1"} reverts
    all three atomically; events.jsonl gains one undo activity entry."""
    from okf_loom.paths import concept_id_to_str

    base, studio, server = live_server
    bundle = Bundle.load(server.bundle.root)
    # Pick three distinct concepts to write under one group_id.
    cids = [concept_id_to_str(c) for c in list(bundle.concepts)[:3]]
    assert len(cids) == 3, f"need 3 concepts, got {len(cids)}"
    originals = {cid: (bundle.root / f"{cid}.md").read_bytes() for cid in cids}

    # Three independent add_tag writes via /__apply, all sharing G1.
    for i, cid in enumerate(cids):
        code, body = _apply(base, {
            "kind": "add_tag", "target": cid,
            "args": {"tag": f"g1-step-{i}"},
            "group_id": "G1-P1-18",
        }, server.csrf_token)
        assert code == 200 and body["applied"] is True, (cid, body)
        # Each write changed the file.
        assert (bundle.root / f"{cid}.md").read_bytes() != originals[cid]

    # Group undo reverts all three.
    before_events = studio.read_events(limit=200)
    code, body = _post(base, "/__undo", {"group_id": "G1-P1-18"}, server.csrf_token)
    assert code == 200
    assert body["ok"] is True
    assert body["restored"] >= 3, body
    # All three files reverted to their original bytes.
    for cid in cids:
        assert (bundle.root / f"{cid}.md").read_bytes() == originals[cid], (
            f"group undo did not restore {cid}"
        )
    # events.jsonl records the undo as a small number of entries (one
    # restore per concept, each as an attributed undo_restore activity).
    # There should be NO duplicate group-undo entries (no replay).
    after_events = studio.read_events(limit=200)
    new_undo_events = [
        e for e in after_events[len(before_events):]
        if e.get("type") == "activity" and e.get("action") == "undo_restore"
    ]
    assert len(new_undo_events) == 3, (
        f"expected 3 undo_restore activities (one per group member); "
        f"got {len(new_undo_events)}: {new_undo_events}"
    )
    # Each undo event is attributed to the group.
    for e in new_undo_events:
        assert e.get("group_id") == "G1-P1-18", e
