"""X2 portable feedback import: local-only preview/confirm HTTP contract."""
from __future__ import annotations

import hashlib
import html
import http.client
import json
import re
import socket
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from scripts.okf_loom.annotations import concept_content_rev
from scripts.okf_loom.model import Bundle
from scripts.okf_loom.portable_feedback import (
    build_marker, fingerprint_request, import_idempotency_key, parse_note)
from scripts.okf_loom.render import (
    _FEEDBACK_IMPORT_BLOCKED, FEEDBACK_IMPORT_RETENTION, build_site, render_feedback_import_page)
from scripts.okf_loom.server import (
    OKFWikiHandler, _start_public_ingress, _stop_public_ingress)
from scripts.okf_loom.studio import Studio
from scripts.okf_loom.viewer.assets import SERVE_ONLY_STATIC, list_builtin_static, list_served_static

PREVIEW = "/__feedback-import/preview"
CONFIRM = "/__feedback-import/confirm"
NOTE_BYTES = b"---\ntype: note\n---\ntarget here\n"
CRLF_BYTES = b"---\r\ntype: note\r\n---\r\n\r\nWindows text \xf0\x9f\x98\x80\r\n"


@pytest.fixture
def bundle_root(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "note.md").write_bytes(NOTE_BYTES)
    (root / "crlf.md").write_bytes(CRLF_BYTES)
    return root


@pytest.fixture
def make_server(bundle_root):
    started = []

    def make(*, edit=True):
        server = ThreadingHTTPServer(("127.0.0.1", 0), OKFWikiHandler)
        server.bundle = Bundle.load(bundle_root)
        server.config = {}
        server.name = "test"
        server.plugin = None
        server.studio = Studio.for_bundle(bundle_root)
        server.studio.ensure_session()
        server.csrf_token = "secret"
        server.allowed_hosts = ("127.0.0.1", "localhost")
        server.studio_edit = edit
        server.studio_live = True
        server._state_lock = threading.RLock()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return server

    yield make
    for server, thread in started:
        server.shutdown(); server.server_close(); thread.join(5)


@pytest.fixture
def server(make_server):
    return make_server()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def note(document="note.md", source=NOTE_BYTES, *, note_id="0123456789abcdef0123456789abcdef",
         body="Please clarify this page.", **extra) -> bytes:
    value = {"format": "okf-loom.portable-feedback", "version": 1, "note_id": note_id,
             "target": {"document": document, "source_sha256": sha(source)},
             "body": body, **extra}
    return json.dumps(value, indent=2).encode()


def post(server, path, body: bytes, *, token="secret", preview=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    request_headers = {"Content-Type": "application/json", "Host": "127.0.0.1"}
    if token is not None:
        request_headers["X-OKF-Token"] = token
    if preview is not None:
        request_headers["X-OKF-Preview-Token"] = preview
    request_headers.update(headers or {})
    conn.request("POST", path, body=body, headers=request_headers)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    try:
        return response.status, json.loads(payload)
    except ValueError:
        return response.status, payload


def import_note(server, raw):
    status, preview = post(server, PREVIEW, raw)
    assert status == 200 and preview["status"] == "ready", preview
    return post(server, CONFIRM, raw, preview=preview["preview_token"])


def session_files(studio):
    """Every durable session file except lock files (which carry no state)."""
    root = studio.session_dir
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and "lock" not in p.name and ".key-locks" not in p.parts
            and ".comment-locks" not in p.parts}


def create_wal_records(studio):
    wal = studio.comment_create_wal_dir
    return [p for p in wal.glob("*.json")
            if p.name not in {"tombstones.json", "tombstone-filter.json"}]


def comment_events(studio, comment_id):
    return [e for e in studio.read_event_snapshot()
            if e.get("type") == "comment" and e.get("id") == comment_id]


def test_preview_then_confirm_creates_ordinary_open_page_comment_with_server_marker(server):
    raw = note(quote="target here", claimed_author="Ann", claimed_at="2026-09-22T10:15:00.000Z")
    status, preview = post(server, PREVIEW, raw)
    assert status == 200, preview
    assert preview["status"] == "ready" and preview["source_match"] is True
    assert preview["document"] == "note.md" and preview["concept"] == "note"
    assert preview["note"]["claimed_author"] == "Ann" and preview["expires_at"].endswith("Z")
    status, created = post(server, CONFIRM, raw, preview=preview["preview_token"])
    assert status == 201, created
    assert created["replayed"] is False
    row = created["comment"]
    assert (row["actor"], row["state"], row["anchor"], row["detail"]) == ("user", "open", {"kind": "page"}, {})
    assert (row["document"], row["concept"], row["body"]) == ("note.md", "note", "Please clarify this page.")
    assert row["portable_import"] == build_marker(parse_note(raw))
    assert row["portable_import"]["claimed_author"] == "Ann"
    stored = server.studio.get_comment(row["id"])
    assert stored["portable_import"] == row["portable_import"]
    # The closed live-event schema is unchanged: no marker in events.
    events = comment_events(server.studio, row["id"])
    assert len(events) == 1 and "portable_import" not in events[0]
    # A second preview now reports the durable outcome.
    status, again = post(server, PREVIEW, raw)
    assert again["status"] == "already_imported" and again["comment"]["id"] == row["id"]
    assert "preview_token" not in again


def test_crlf_target_uses_exact_disk_bytes_not_normalized_revision(server, bundle_root):
    disk = (bundle_root / "crlf.md").read_bytes()
    normalized = disk.decode().replace("\r\n", "\n")
    assert sha(disk) != sha(normalized.encode())
    assert sha(disk) != concept_content_rev(normalized)
    status, created = import_note(server, note("crlf.md", disk))
    assert status == 201, created
    assert created["comment"]["portable_import"]["source_sha256"] == sha(disk)
    # A note hashed over the normalized text is a revision mismatch.
    status, preview = post(server, PREVIEW, note(
        "crlf.md", normalized.encode(), note_id="1" * 32))
    assert status == 200 and preview["status"] == "revision_mismatch"
    assert "preview_token" not in preview


def test_replay_survives_target_deletion_and_needs_no_preview_token(server, bundle_root):
    raw = note()
    status, created = import_note(server, raw)
    assert status == 201
    (bundle_root / "note.md").unlink()
    status, replay = post(server, CONFIRM, raw)  # no preview token at all
    assert status == 200, replay
    assert replay["replayed"] is True and replay["comment"]["id"] == created["comment"]["id"]
    # Pretty-printing differently is the same note.
    compact = json.dumps(json.loads(raw), separators=(",", ":")).encode()
    assert post(server, CONFIRM, compact)[0] == 200
    assert [c["id"] for c in server.studio.list_comments()] == [created["comment"]["id"]]


def test_same_note_id_with_different_content_conflicts(server):
    assert import_note(server, note())[0] == 201
    other = note(body="A different body")
    status, preview = post(server, PREVIEW, other)
    assert status == 200 and preview["status"] == "conflict" and "preview_token" not in preview
    status, error = post(server, CONFIRM, other, preview="anything")
    assert status == 409 and error["error"]["code"] == "idempotency_conflict"
    assert len(server.studio.list_comments()) == 1


def test_prepared_wal_crash_then_target_deletion_reconciles_to_replay(server, bundle_root):
    raw = note()
    parsed = parse_note(raw)
    with pytest.raises(OSError, match="after append"):
        server.studio.post_comment_idempotent(
            request=fingerprint_request(parsed), idempotency_key=import_idempotency_key(parsed),
            document="note.md", concept="note", body=parsed.body, anchor={"kind": "page"},
            actor="user", detail={}, parent_id=None, portable_import=build_marker(parsed),
            fault="after_append")
    [wal_path] = create_wal_records(server.studio)
    assert json.loads(wal_path.read_text())["state"] == "prepared"
    original = server.studio.list_comments()[0]
    (bundle_root / "note.md").unlink()
    status, replay = post(server, CONFIRM, raw)
    assert status == 200, replay
    assert replay["replayed"] is True and replay["comment"]["id"] == original["id"]
    assert replay["comment"]["portable_import"] == build_marker(parsed)
    assert json.loads(wal_path.read_text())["state"] == "finalized"
    assert len(server.studio.list_comments()) == 1
    assert len(comment_events(server.studio, original["id"])) == 1


def test_prepared_wal_retired_by_concurrent_sweep_still_replays(server, bundle_root, monkeypatch):
    """A sweep under another key may retire the WAL while a retry waits for maintenance."""
    from pathlib import Path

    from scripts.okf_loom import studio as studio_module

    studio = server.studio
    raw = note()
    parsed = parse_note(raw)
    with pytest.raises(OSError, match="after append"):
        studio.post_comment_idempotent(
            request=fingerprint_request(parsed), idempotency_key=import_idempotency_key(parsed),
            document="note.md", concept="note", body=parsed.body, anchor={"kind": "page"},
            actor="user", detail={}, parent_id=None, portable_import=build_marker(parsed),
            fault="after_append")
    [wal_path] = create_wal_records(studio)
    original = studio.list_comments()[0]
    wal = json.loads(wal_path.read_text())
    assert wal["state"] == "prepared"
    expired = time.time() - studio.COMMENT_CREATE_PREPARED_GRACE_SECONDS - 3600
    wal.update(created_at=expired - 1, expires_at=expired)
    wal_path.write_text(json.dumps(wal))
    (bundle_root / "note.md").unlink()

    # Barrier at the maintenance-lock seam: the retry has read the prepared
    # WAL under its key lock and is about to take `.maintenance`.
    maintenance = studio.comment_create_wal_dir / ".maintenance"
    real_path_lock = studio_module.path_lock
    test_thread = threading.get_ident()
    reached, release = threading.Event(), threading.Event()

    def gated_path_lock(path, *args, **kwargs):
        if (Path(path) == maintenance and threading.get_ident() != test_thread
                and not reached.is_set()):
            reached.set()
            release.wait(10)
        return real_path_lock(path, *args, **kwargs)

    monkeypatch.setattr(studio_module, "path_lock", gated_path_lock)
    result = {}
    retry = threading.Thread(target=lambda: result.update(response=post(server, CONFIRM, raw)))
    retry.start()
    try:
        assert reached.wait(10), "retry never reached the maintenance seam"
        # Another key's create sweeps: it reconciles the prepared record and
        # retires the expired WAL into a finalized tombstone.
        other, _ = studio.post_comment_idempotent(
            request={"other": 1}, idempotency_key="ordinary-key-0001", document="crlf.md",
            concept="crlf", body="other", anchor={"kind": "page"}, actor="user", detail={})
        assert not wal_path.exists()
        tombstone = json.loads(studio.comment_create_tombstone_path.read_text())[
            import_idempotency_key(parsed)]
        assert tombstone["state"] == "finalized"
    finally:
        release.set()
        retry.join(10)
    status, replay = result["response"]
    assert status == 200, replay
    assert replay["replayed"] is True and replay["comment"]["id"] == original["id"]
    assert sorted(c["id"] for c in studio.list_comments()) == sorted([original["id"], other["id"]])
    assert len(comment_events(studio, original["id"])) == 1


def test_prepared_wal_without_row_still_reports_missing_target(server, bundle_root):
    raw = note()
    parsed = parse_note(raw)
    with pytest.raises(OSError, match="after prepare"):
        server.studio.post_comment_idempotent(
            request=fingerprint_request(parsed), idempotency_key=import_idempotency_key(parsed),
            document="note.md", concept="note", body=parsed.body, anchor={"kind": "page"},
            actor="user", detail={}, portable_import=build_marker(parsed), fault="after_prepare")
    status, preview = post(server, PREVIEW, raw)
    assert preview["status"] == "ready"
    (bundle_root / "note.md").unlink()
    status, error = post(server, CONFIRM, raw, preview=preview["preview_token"])
    assert status == 404 and error["error"]["code"] == "target_missing"
    assert server.studio.list_comments() == []


def test_byte_change_between_preview_and_confirm_is_refused(server, bundle_root):
    raw = note()
    status, preview = post(server, PREVIEW, raw)
    (bundle_root / "note.md").write_bytes(NOTE_BYTES + b"edited\n")
    status, error = post(server, CONFIRM, raw, preview=preview["preview_token"])
    assert status == 409 and error["error"]["code"] == "revision_mismatch"
    assert server.studio.list_comments() == []


def test_preview_token_is_bound_to_note_and_process(make_server, server):
    raw = note()
    status, preview = post(server, PREVIEW, raw)
    token = preview["preview_token"]
    for bad in (None, "", token[:-2] + "AA"):
        status, error = post(server, CONFIRM, raw, preview=bad)
        assert status == 409 and error["error"]["code"] == "stale_preview"
    other = note(note_id="2" * 32)
    assert post(server, CONFIRM, other, preview=token)[1]["error"]["code"] == "stale_preview"
    restarted = make_server()  # same bundle/session, new process key
    status, error = post(restarted, CONFIRM, raw, preview=token)
    assert status == 409 and error["error"]["code"] == "stale_preview"
    assert server.studio.list_comments() == []
    assert import_note(restarted, raw)[0] == 201


def test_no_edit_refuses_preview_and_confirm_without_state(make_server):
    server = make_server(edit=False)
    before = session_files(server.studio)
    for path in (PREVIEW, CONFIRM):
        status, error = post(server, path, note(), preview="x")
        assert status == 403 and error["error"]["code"] == "read_only"
    assert session_files(server.studio) == before
    assert create_wal_records(server.studio) == []


@pytest.mark.parametrize("path", [PREVIEW, CONFIRM])
@pytest.mark.parametrize("token, headers", [
    (None, {}), ("wrong", {}), ("secret", {"Origin": "http://evil.example"}),
    ("secret", {"Host": "evil.example"}),
])
def test_token_and_origin_host_are_required(server, path, token, headers):
    status, error = post(server, path, note(), token=token, headers=headers)
    assert status == 403 and error["error"]["code"] == "forbidden"
    assert server.studio.list_comments() == []


def test_media_type_and_invalid_note(server):
    status, error = post(server, PREVIEW, note(), headers={"Content-Type": "text/plain"})
    assert status == 415 and error["error"]["code"] == "unsupported_media_type"
    status, error = post(server, PREVIEW, b'{"format":"x"}')
    assert status == 400 and error["error"]["code"] == "invalid_note"
    assert error["error"]["fields"] == {"note": "keys"}


def _raw_exchange(port, head: bytes, body: bytes = b"") -> bytes:
    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    try:
        client.sendall(head + body)
        return client.makefile("rb").read()
    finally:
        client.close()


@pytest.mark.parametrize("path", [PREVIEW, CONFIRM])
def test_oversize_note_is_refused_before_body_read_and_closes(server, path):
    head = (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nX-OKF-Token: secret\r\n"
            f"Content-Type: application/json\r\nContent-Length: {32 * 1024 + 1}\r\n\r\n").encode()
    response = _raw_exchange(server.server_port, head, b"{")
    assert response.startswith(b"HTTP/1.1 413")
    assert b"note_too_large" in response and b"Connection: close" in response


@pytest.mark.parametrize("mode", ["edit", "comment", "read"])
@pytest.mark.parametrize("path", [PREVIEW, CONFIRM])
def test_public_ingress_is_refused_before_body_read(server, mode, path):
    ingress = _start_public_ingress(server, allow_public_edit=mode == "edit",
                                    allow_public_comment=mode == "comment")
    try:
        token = (getattr(server, "public_mutation_token", None) if mode == "edit"
                 else getattr(server, "public_comment_token", None)) or "secret"
        head = (f"POST {path} HTTP/1.1\r\nHost: localhost\r\nX-OKF-Token: {token}\r\n"
                "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n").encode()
        response = _raw_exchange(ingress.server_address[1], head, b"{")  # body never completes
        assert response.startswith(b"HTTP/1.1 403")
        assert b"request_body_timeout" not in response
        # A local import is also refused while the public ingress is attached.
        status, error = post(server, PREVIEW, note())
        assert status == 403 and error["error"]["code"] == "local_only"
    finally:
        _stop_public_ingress(server)
    assert server.studio.list_comments() == []


def test_attached_tunnel_url_blocks_local_import(server):
    server.tunnel_url = "https://public-test.trycloudflare.com"
    before = session_files(server.studio)
    for path in (PREVIEW, CONFIRM):
        status, error = post(server, path, note(), preview="x")
        assert status == 403 and error["error"]["code"] == "local_only"
    assert session_files(server.studio) == before
    server.tunnel_url = None
    assert post(server, PREVIEW, note())[1]["status"] == "ready"


def test_preview_writes_no_wal_receipt_or_event(server):
    before = session_files(server.studio)
    events = len(server.studio.read_event_snapshot())
    for raw in (note(), note(body="other", note_id="3" * 32), note("missing.md", note_id="4" * 32)):
        status, preview = post(server, PREVIEW, raw)
        assert status == 200
    assert preview["status"] == "target_missing"
    assert session_files(server.studio) == before
    assert len(server.studio.read_event_snapshot()) == events
    assert create_wal_records(server.studio) == []


def _comment(server, body, *, key=None):
    headers = {"Idempotency-Key": key} if key else {}
    return post(server, "/__comment", json.dumps(body).encode(), headers=headers)


def test_ordinary_comment_routes_cannot_set_or_strip_the_marker(server):
    marker = {"schema": 1, "note_id": "5" * 32}
    status, error = _comment(server, {"document": "note.md", "body": "x",
                                      "anchor": {"kind": "page"}, "portable_import": marker})
    assert status == 400
    status, created = _comment(server, {"document": "note.md", "body": "detail claim",
                                        "anchor": {"kind": "page"},
                                        "detail": {"portable_import": marker}})
    assert status == 201
    assert "portable_import" not in created["comment"]
    assert "portable_import" not in server.studio.get_comment(created["comment"]["id"])
    status, imported = import_note(server, note())
    assert status == 201, imported
    row = imported["comment"]
    status, error = post(server, "/__comment-update", json.dumps({
        "id": row["id"], "expected_row_token": row["row_token"],
        "portable_import": None}).encode())
    assert status == 400
    resolved = server.studio.update_comment(row["id"], state="resolved", claimed_by="agent")
    assert resolved["portable_import"] == row["portable_import"]
    status, archived = post(server, "/__comment-update", json.dumps({
        "id": row["id"], "expected_row_token": resolved["row_token"], "archived": True}).encode())
    assert status == 200, archived
    latest = server.studio.get_comment(row["id"])
    assert latest["archived"] is True and latest["portable_import"] == row["portable_import"]


def test_import_key_namespace_is_reserved_on_ordinary_comments(server):
    status, error = _comment(server, {"document": "note.md", "body": "squat",
                                      "anchor": {"kind": "page"}},
                             key="pfn.0123456789abcdef0123456789abcdef")
    assert status == 400 and error["error"] == "reserved_idempotency_key"
    assert server.studio.list_comments() == []
    assert create_wal_records(server.studio) == []
    assert import_note(server, note())[0] == 201


# ---- GET /__feedback-import: the local operator page ----------------------
PAGE = "/__feedback-import"


def get(port, path, *, host="127.0.0.1"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Host": host})
    response = conn.getresponse()
    payload = response.read().decode("utf-8", "replace")
    content_type = response.getheader("Content-Type", "")
    conn.close()
    return response.status, content_type, payload


def page_config(page):
    match = re.search(r'<script type="application/json" id="okf-feedback-import-config">(.*?)</script>', page)
    assert match, page[:400]
    return json.loads(match.group(1))


def test_local_import_page_offers_the_chooser_and_carries_the_local_token(server):
    before = session_files(server.studio)
    status, content_type, page = get(server.server_port, PAGE)
    assert status == 200 and content_type.startswith("text/html")
    assert page_config(page) == {"version": 1, "availability": "available",
                                 "max_note_bytes": 32768, "preview_ttl_seconds": 600}
    assert 'data-availability="available"' in page and 'id="okf-fbi-file"' in page
    assert '<script src="/__static/feedback-import.js" defer></script>' in page
    assert 'id="okf-studio-bootstrap"' in page  # the POST routes need the token
    text = html.unescape(page)
    assert FEEDBACK_IMPORT_RETENTION in text
    assert "Retries return the original result for up to 24 hours (7 days as a compact record)." in text
    # R3: an imported note is a page comment; any quote it has is never pinned.
    assert ("It is a page comment: any quote is shown with it but is not highlighted or pinned to "
            "exact text.") in text
    assert session_files(server.studio) == before  # rendering the page writes nothing


@pytest.mark.parametrize("availability", ["read_only", "local_only", "unavailable"])
def test_import_page_explains_why_import_is_unavailable(make_server, availability):
    server = make_server(edit=availability != "read_only")
    if availability == "local_only":
        server.tunnel_url = "https://public-test.trycloudflare.com"
    if availability == "unavailable":
        server.studio = None
    status, _, page = get(server.server_port, PAGE)
    assert status == 200
    assert page_config(page)["availability"] == availability
    assert f'data-availability="{availability}"' in page
    assert 'id="okf-fbi-file"' not in page and "okf-fbi-result" not in page
    text = html.unescape(re.sub(r"<[^>]+>", "", page))
    assert _FEEDBACK_IMPORT_BLOCKED[availability] in text
    if availability == "local_only":
        assert "Import is available only on the local Studio, not through a tunnel." in text
    if availability == "read_only":
        # The flag is one unbreakable token, never split across lines.
        assert '<code class="okf-fbi__flag">--no-edit</code>' in page


@pytest.mark.parametrize("mode", ["edit", "comment", "read"])
def test_public_ingress_cannot_fetch_the_import_page(server, mode):
    ingress = _start_public_ingress(server, allow_public_edit=mode == "edit",
                                    allow_public_comment=mode == "comment")
    try:
        status, _, body = get(ingress.server_address[1], PAGE, host="localhost")
        assert status == 403 and "okf-feedback-import-config" not in body
        # While the ingress is attached the local page itself says why not.
        status, _, page = get(server.server_port, PAGE)
        assert status == 200 and page_config(page)["availability"] == "local_only"
    finally:
        _stop_public_ingress(server)


def test_import_page_assets_are_served_live_but_never_built(server, bundle_root, tmp_path):
    for name, kind in (("feedback-import.js", "javascript"), ("feedback-import.css", "text/css")):
        status, content_type, body = get(server.server_port, f"/__static/{name}")
        assert status == 200 and kind in content_type and body.strip()
        assert name in SERVE_ONLY_STATIC and name in list_served_static()
        assert name not in list_builtin_static()
    # Even an export that opts in to the offline note composer never ships
    # the import page, its assets, or a link to them.
    for target in ("static", "single-file"):
        out = tmp_path / f"site-{target}"
        build_site(Bundle.load(bundle_root), out, target=target, include_portable_feedback=True)
        assert not list(out.rglob("feedback-import*"))
        # studio.js ships its (local-edit-gated) path constant; no page links it.
        assert not any(b"feedback-import" in p.read_bytes() for p in out.rglob("*.html"))


def test_renderer_refuses_an_unknown_availability():
    with pytest.raises(ValueError):
        render_feedback_import_page(name="x", config={}, availability="public",
                                    max_note_bytes=1, preview_ttl_seconds=1)
