import http.client
import json
import threading
import socket
import os
import time
from http.server import ThreadingHTTPServer

import pytest

from scripts.okf_loom.annotations import capture_anchor, concept_content_rev
from scripts.okf_loom.model import Bundle
from scripts.okf_loom.server import OKFWikiHandler
from scripts.okf_loom.studio import Studio
from scripts.okf_loom.studio import rev_of
from scripts.okf_loom.parse import parse_document, extract_headings


@pytest.fixture
def annotation_server(tmp_path):
    (tmp_path / "note.md").write_text("---\ntype: note\n---\ntarget here\n", encoding="utf-8")
    bundle = Bundle.load(tmp_path)
    studio = Studio.for_bundle(tmp_path)
    comment = studio.post_comment(concept="note", body="ask")
    server = ThreadingHTTPServer(("127.0.0.1", 0), OKFWikiHandler)
    server.bundle = bundle
    server.config = {}
    server.name = "test"
    server.plugin = None
    server.studio = studio
    server.csrf_token = "secret"
    server.allowed_hosts = ("127.0.0.1", "localhost")
    server.studio_edit = True
    server.studio_live = True
    server._state_lock = threading.RLock()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, studio, comment, bundle.concepts[next(iter(bundle.concepts))].raw_text
    server.shutdown(); server.server_close(); thread.join(5)


def request(server, body, *, token="secret", origin=None, host="127.0.0.1",
            key="http-key-0001", raw=False, content_type="application/json"):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    payload = body if raw else json.dumps(body, separators=(",", ":"))
    headers = {"Content-Type": content_type, "X-OKF-Token": token,
               "Host": host}
    if key is not None:
        headers["Idempotency-Key"] = key
    if origin is not None:
        headers["Origin"] = origin
    conn.request("POST", "/__comment-reanchor", body=payload, headers=headers)
    response = conn.getresponse(); data = json.loads(response.read())
    retry = response.getheader("Retry-After")
    conn.close()
    return response.status, data, retry


def post_json(server, path, body, *, key=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Content-Type": "application/json", "X-OKF-Token": "secret"}
    if key:
        headers["Idempotency-Key"] = key
    conn.request("POST", path, body=json.dumps(body), headers=headers)
    response = conn.getresponse(); payload = json.loads(response.read()); conn.close()
    return response.status, payload


def post_raw_json(server, path, raw):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    conn.request("POST", path, body=raw, headers={
        "Content-Type": "application/json", "X-OKF-Token": "secret"})
    response = conn.getresponse(); payload = response.read(); conn.close()
    return response.status, json.loads(payload)


def get_json(server, path, *, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    response = conn.getresponse(); payload = json.loads(response.read())
    headers = dict(response.getheaders()); conn.close()
    return response.status, payload, headers


@pytest.mark.parametrize("payload", [
    '{"concept":"note","concept":"other","body":"x"}',
    '{"concept":"note","body":"x","unknown":1}',
    '{"concept":[],"body":"x"}',
    '{"concept":"note","body":{},"detail":[]}',
    '{"concept":"note","body":"x","actor":"root"}',
])
def test_comment_create_closed_schema_rejects_duplicates_types_and_unknowns(
        annotation_server, payload):
    server, studio, _, _ = annotation_server
    before = len(studio.list_comments())
    status, response = post_raw_json(server, "/__comment", payload)
    assert status == 400
    assert response["error"] == "invalid_request" or response is not None
    assert len(studio.list_comments()) == before


@pytest.mark.parametrize("payload", [
    '{"id":"01JABED9K0","id":"01JABED9K1","expected_row_token":"r2:x","body":"x"}',
    '{"id":"01JABED9K0","expected_row_token":"r2:x","body":"x","unknown":1}',
    '{"id":1,"expected_row_token":"r2:x","body":"x"}',
    '{"id":"01JABED9K0","expected_row_token":"r2:x","state":"resolved"}',
    '{"id":"01JABED9K0","expected_row_token":"r2:x","state":"open","body":"x"}',
])
def test_comment_update_closed_single_operation_schema(annotation_server, payload):
    server, _, _, _ = annotation_server
    status, response = post_raw_json(server, "/__comment-update", payload)
    assert status == 400
    assert response.get("error") == "invalid_request" or response is not None


def valid_request(comment, source):
    anchor = capture_anchor(source, source.index("target"), source.index("target") + 6)
    return {"comment_id": comment["id"], "expected_row_token": comment["row_token"],
            "expected_concept_content_rev": concept_content_rev(source), "anchor": anchor}


def test_reply_create_and_parent_reanchor_share_global_lock_order(annotation_server):
    server, studio, parent, source = annotation_server
    gate = threading.Barrier(2); outcomes = []

    def reply_create():
        gate.wait()
        outcomes.append(("reply",) + post_json(server, "/__comment", {
            "concept": "note", "body": "reply", "parent_id": parent["id"]
        }, key="reply-lock-order-key"))

    def parent_reanchor():
        gate.wait()
        status, payload, _ = request(server, valid_request(parent, source),
                                     key="reanchor-lock-order-key")
        outcomes.append(("reanchor", status, payload))

    threads = [threading.Thread(target=reply_create), threading.Thread(target=parent_reanchor)]
    for thread in threads: thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive(), "lock-order inversion prevented bounded completion"
    assert len(outcomes) == 2
    assert {item[0] for item in outcomes} == {"reply", "reanchor"}
    assert all(item[1] in {200, 201, 409} for item in outcomes)
    assert studio.get_comment(parent["id"]) is not None


@pytest.mark.parametrize("value", [None, True, 1, [], {}])
def test_comment_actor_and_update_state_wrong_types_fail_closed(annotation_server, value):
    server, studio, comment, _ = annotation_server
    status, payload = post_json(server, "/__comment", {
        "concept": "note", "body": "typed", "actor": value,
      })
    assert status == 400 and payload["error"] == "invalid_request"
    status, payload = post_json(server, "/__comment-update", {
        "id": comment["id"], "expected_row_token": studio.get_comment(comment["id"])["row_token"],
        "state": value,
    })
    assert status == 400 and payload["error"] == "invalid_request"


def test_public_comment_cannot_forge_agent_actor(annotation_server):
    server, studio, _, _ = annotation_server
    before = len(studio.list_comments())
    status, payload = post_json(server, "/__comment", {
        "concept": "note", "body": "forged", "actor": "agent",
    })
    assert status == 400 and payload == {"ok": False, "error": "invalid_request"}
    assert len(studio.list_comments()) == before


@pytest.mark.parametrize("field,value", [
    ("actor", None), ("actor", True), ("actor", 1), ("actor", []), ("actor", {}),
    ("actor", "unknown"), ("state", None), ("state", True), ("state", 1),
    ("state", []), ("state", {}), ("state", "unknown"), ("message", True),
    ("message", 1), ("message", []), ("message", {}), ("unknown", "x"),
])
def test_presence_closed_schema_wrong_types_do_not_mutate(annotation_server, field, value):
    server, studio, _, _ = annotation_server
    before = studio.get_presence()
    body = {"actor": "agent", "state": "idle", field: value}
    status, payload = post_json(server, "/__presence", body)
    assert status == 400 and payload["error"] == "invalid_request"
    assert studio.get_presence() == before


def test_presence_v2_two_sessions_agent_and_no_store_get(annotation_server):
    server, studio, _, _ = annotation_server
    studio.set_presence(state="editing", focus="note")
    status, first, headers = get_json(server, "/__presence")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    status, second, _ = get_json(server, "/__presence")
    for issued, label in ((first, "A"), (second, "B")):
        auth = issued["browser_session"]
        status, response = post_json(server, "/__presence", {
            "action": "upsert", **auth, "client_seq": 1, "actor": "browser",
            "state": "watching", "label": label, "focus": "note",
            "viewport": {"schema": "graph-viewport-v1", "x": .2, "y": .8},
        })
        assert status == 200 and response["applied"] is True
    _, snapshot, _ = get_json(server, "/__presence")
    assert {row["session_id"] for row in snapshot["records"]} == {
        "agent", first["browser_session"]["session_id"], second["browser_session"]["session_id"]}


def test_presence_get_reuses_valid_capability_without_issuance_churn(annotation_server):
    server, _, _, _ = annotation_server
    _, first, _ = get_json(server, "/__presence")
    auth = first["browser_session"]
    before = sum(len(rows) for rows in server._presence_issuance.values())
    status, reused, headers = get_json(server, "/__presence", headers={
        "X-OKF-Presence-Session": auth["session_id"],
        "X-OKF-Presence-Capability": auth["capability"],
    })
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert reused["browser_session"] == auth
    assert sum(len(rows) for rows in server._presence_issuance.values()) == before
    assert get_json(server, "/__presence", headers={"X-OKF-Presence-Session": auth["session_id"]})[0] == 403


def test_presence_v2_capability_sequence_leave_and_forgery(annotation_server):
    server, _, _, _ = annotation_server
    _, issued, _ = get_json(server, "/__presence"); auth = issued["browser_session"]
    body = {"action":"upsert", **auth, "client_seq":2, "actor":"browser",
            "state":"watching", "label":"A",
            "viewport":{"schema":"graph-viewport-v1","x":.2,"y":.8}}
    assert post_json(server, "/__presence", body)[1]["applied"] is True
    assert post_json(server, "/__presence", body)[1]["applied"] is False
    forged = dict(body, capability="0" * 64)
    assert post_json(server, "/__presence", forged)[0] == 403
    leave = {"action":"leave", **auth, "client_seq":3}
    assert post_json(server, "/__presence", leave)[1]["applied"] is True
    assert post_json(server, "/__presence", body)[1]["applied"] is False


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -0.01, 1.01])
def test_presence_v2_coordinates_fail_closed(annotation_server, value):
    server, _, _, _ = annotation_server
    _, issued, _ = get_json(server, "/__presence"); auth = issued["browser_session"]
    status, _ = post_json(server, "/__presence", {"action":"upsert", **auth,
        "client_seq":1,"actor":"browser","state":"watching","label":"A",
        "viewport":{"schema":"graph-viewport-v1","x":value,"y":.5}})
    assert status == 400


def test_presence_v2_duplicate_key_and_bidi_label_rejected(annotation_server):
    server, _, _, _ = annotation_server
    _, issued, _ = get_json(server, "/__presence"); auth = issued["browser_session"]
    raw = ('{"action":"leave","action":"leave","session_id":"%s",'
           '"capability":"%s","client_seq":1}') % (auth["session_id"], auth["capability"])
    assert post_raw_json(server, "/__presence", raw)[0] == 400
    status, _ = post_json(server, "/__presence", {"action":"upsert", **auth,
        "client_seq":1,"actor":"browser","state":"watching","label":"A\u202eB",
        "viewport":{"schema":"graph-viewport-v1","x":.5,"y":.5}})
    assert status == 400


def test_presence_v2_unknown_focus_and_rate_cap(annotation_server, monkeypatch):
    import types
    from scripts.okf_loom import server as server_module
    # Rate-window semantics must not depend on 20 durable writes fitting in a
    # real second on a loaded filesystem. Freeze only the handler's clock.
    clock = types.SimpleNamespace(**{name: getattr(time, name) for name in dir(time)})
    clock.monotonic = lambda: 1000.0
    monkeypatch.setattr(server_module, "time", clock)
    server, _, _, _ = annotation_server
    _, issued, _ = get_json(server, "/__presence"); auth = issued["browser_session"]
    base = {"action":"upsert", **auth, "actor":"browser", "state":"watching",
            "label":"A", "viewport":{"schema":"graph-viewport-v1","x":.5,"y":.5}}
    assert post_json(server, "/__presence", {**base, "client_seq":1, "focus":"missing"})[0] == 400
    statuses = [post_json(server, "/__presence", {**base, "client_seq":seq})[0]
                for seq in range(2, 23)]
    assert 429 in statuses


def test_presence_v2_cross_session_prior_server_and_registry_expiry(annotation_server):
    server, studio, _, _ = annotation_server
    _, first, _ = get_json(server, "/__presence"); _, second, _ = get_json(server, "/__presence")
    leave={"action":"leave", **first["browser_session"], "client_seq":1}
    assert post_json(server,"/__presence",{**leave,"session_id":second["browser_session"]["session_id"]})[0] == 403
    restarted=ThreadingHTTPServer(("127.0.0.1",0),OKFWikiHandler)
    for name in ("bundle","config","name","plugin","studio","csrf_token","allowed_hosts","studio_edit","studio_live"):
        setattr(restarted,name,getattr(server,name))
    restarted._state_lock=threading.RLock(); restarted._presence_generation="actual-restart-generation"
    thread=threading.Thread(target=restarted.serve_forever,daemon=True); thread.start()
    try: assert post_json(restarted,"/__presence",leave)[0] == 403
    finally: restarted.shutdown(); restarted.server_close(); thread.join(3)
    data=json.loads(studio.presence_path.read_text()); sid=first["browser_session"]["session_id"]
    data["issued"][sid]["expires_at"]=time.time()-1
    studio.presence_path.write_text(json.dumps(data))
    assert post_json(server,"/__presence",leave)[0] == 403


def test_presence_corrupt_authority_http_is_stable_503(annotation_server):
    server, studio, _, _ = annotation_server
    studio.presence_path.write_text("not json")
    assert get_json(server,"/__presence")[0] == 503
    assert post_json(server,"/__presence",{"actor":"agent","state":"idle"}) == (503,{"ok":False,"error":"unavailable"})


def test_valid_browser_capability_then_corruption_is_503_without_bus_or_cache_change(annotation_server):
    server,studio,_,_=annotation_server
    _,issued,_=get_json(server,"/__presence"); auth=issued["browser_session"]
    q=studio.bus.subscribe(); cache=studio.get_presence(); studio.presence_path.write_text("corrupt")
    status,payload=post_json(server,"/__presence",{"action":"leave",**auth,"client_seq":1})
    assert (status,payload)==(503,{"ok":False,"error":"unavailable"})
    assert q.empty() and studio.get_presence()==cache and studio.presence_path.read_text()=="corrupt"


def test_comment_update_route_cas_success_and_conflicts(annotation_server):
    server, studio, comment, _ = annotation_server
    token = comment["row_token"]
    status, payload = post_json(server, "/__comment-update", {
        "id": comment["id"], "expected_row_token": token, "body": "edited",
    })
    assert status == 200 and payload["comment"]["body"] == "edited"
    fresh = payload["comment"]; assert fresh["row_token"] != token
    status, stale = post_json(server, "/__comment-update", {
        "id": comment["id"], "expected_row_token": token, "state": "dismissed",
    })
    assert status == 409 and studio.get_comment(comment["id"])["body"] == "edited"
    status, missing = post_json(server, "/__comment-update", {
        "id": "01JABED9K0", "expected_row_token": "r2:missing", "state": "open",
    })
    assert status == 404
    status, claimed = post_json(server, "/__comment-update", {
        "id": comment["id"], "expected_row_token": fresh["row_token"], "state": "open",
    })
    assert status == 409 and claimed["error"] == "invalid lifecycle transition"
    resolved = studio.post_comment(concept="note", body="resolved", state="resolved")
    status, archived = post_json(server, "/__comment-update", {
        "id": resolved["id"], "expected_row_token": resolved["row_token"], "archived": True,
    })
    assert status == 200 and archived["comment"]["archived"] is True
    assert archived["comment"]["row_token"] != resolved["row_token"]


def test_comment_create_missing_target_is_stable_4xx_but_known_io_is_503(annotation_server):
    server, studio, _, _ = annotation_server
    status, missing = post_json(server, "/__comment", {
        "concept": "missing", "body": "ask",
    })
    assert status == 404 and missing == {"ok": False, "error": "target_not_found"}
    (studio.bundle_root / "note.md").unlink()
    status, unavailable = post_json(server, "/__comment", {
        "concept": "note", "body": "ask",
    })
    assert status == 503 and unavailable == {"ok": False, "error": "unavailable"}


def raw_request(server, request_bytes):
    sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
    sock.sendall(request_bytes)
    chunks = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    sock.close()
    raw = b"".join(chunks)
    status = int(raw.split(b" ", 2)[1])
    body = raw.split(b"\r\n\r\n", 1)[1]
    return status, json.loads(body)


def test_http_success_replay_comments_projection_and_data_revision(annotation_server):
    server, _, comment, source = annotation_server
    body = valid_request(comment, source)
    status, data, _ = request(server, body)
    assert status == 200 and data["ok"] and data["replayed"] is False
    status, replay, _ = request(server, body)
    assert status == 200 and replay["replayed"] is True
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/__comments?concept=note")
    comments = json.loads(conn.getresponse().read())["comments"]
    assert comments[0]["anchor_resolution"]["status"] == "resolved"
    conn.request("GET", "/__data/doc?id=note")
    doc = json.loads(conn.getresponse().read())
    assert doc["concept_content_rev"] == concept_content_rev(doc["annotation_source"])
    assert doc["annotation_projection"]["concept_content_rev"] == doc["concept_content_rev"]
    assert comments[0]["anchor_resolution"]["concept_content_rev"] == doc["concept_content_rev"]
    assert doc["annotation_source"] != doc["raw"]
    conn.close()


def test_data_doc_atomic_external_mutation_is_one_coherent_snapshot(annotation_server):
    server, studio, _, _ = annotation_server
    path = studio.bundle_root / "note.md"
    old_source = "---\ntype: note\ntitle: Old\n---\n# Old heading\n\nOld body target\n"
    new_source = "---\ntype: note\ntitle: New\n---\n# New heading\n\nNew body target\n"
    path.write_text(old_source, encoding="utf-8")
    server.bundle = Bundle.load(studio.bundle_root)
    entered = threading.Event(); release = threading.Event()

    class BlockingPlugin:
        def on_concept_render(self, concept, html):
            entered.set()
            assert release.wait(5)
            return html + f'<span data-snapshot-title="{concept.title}"></span>'

    server.plugin = BlockingPlugin()
    result = {}
    def fetch():
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        conn.request("GET", "/__data/doc?id=note")
        response = conn.getresponse(); result["status"] = response.status
        result["body"] = json.loads(response.read()); conn.close()
    thread = threading.Thread(target=fetch); thread.start(); assert entered.wait(5)
    replacement = path.with_name("note.new")
    replacement.write_text(new_source, encoding="utf-8")
    os.replace(replacement, path)
    release.set(); thread.join(10); assert not thread.is_alive()

    def assert_snapshot(payload, source):
        frontmatter, body = parse_document(source)
        assert payload["annotation_source"] == source
        assert payload["raw"] == body
        assert payload["frontmatter"] == frontmatter
        assert payload["rev"] == rev_of(source)
        assert payload["concept_content_rev"] == concept_content_rev(source)
        assert payload["annotation_projection"]["concept_content_rev"] == payload["concept_content_rev"]
        assert payload["headings"] == [
            {"level": level, "text": text} for level, _slug, text in extract_headings(body)
        ]
        assert ("Old body" in payload["html"]) == ("Old body" in source)
        assert ("New body" in payload["html"]) == ("New body" in source)
        assert f'data-snapshot-title="{frontmatter["title"]}"' in payload["html"]

    assert result["status"] == 200
    assert_snapshot(result["body"], old_source)

    # The next request is built wholly from the successor bytes even though the
    # watcher-owned Bundle still contains the old concept snapshot.
    server.plugin = type("Plugin", (), {"on_concept_render": lambda self, concept, html:
        html + f'<span data-snapshot-title="{concept.title}"></span>'})()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/__data/doc?id=note")
    response = conn.getresponse(); successor = json.loads(response.read()); conn.close()
    assert response.status == 200
    assert_snapshot(successor, new_source)


def test_plugin_replacement_makes_projection_incomplete(annotation_server):
    """Plugin that REPLACES renderer-owned visible text makes projection
    incomplete because the canonical block text no longer maps to the DOM."""
    server, _, _, _ = annotation_server
    server.plugin = type("Plugin", (), {"on_concept_render": lambda self, concept, html:
        html.replace("target here", "Plugin replacement")})()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/__data/doc?id=note")
    response = conn.getresponse(); doc = json.loads(response.read()); conn.close()
    assert response.status == 200
    assert "Plugin" in doc["html"]
    assert doc["annotation_projection"]["concept_content_rev"] == doc["concept_content_rev"]
    assert doc["annotation_projection"]["complete"] is False


def test_plugin_additive_paragraph_keeps_projection_complete(annotation_server):
    """Plugin that ADDS a semantic element (paragraph) after renderer-owned
    content must NOT break projection completeness. The canonical blocks
    that the renderer produced still map exactly; the plugin-added element
    is simply an extra DOM node with no canonical block to pair with."""
    server, _, _, _ = annotation_server
    server.plugin = type("Plugin", (), {"on_concept_render": lambda self, concept, html:
        html + "<p>Plugin injection</p>"})()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/__data/doc?id=note")
    response = conn.getresponse(); doc = json.loads(response.read()); conn.close()
    assert response.status == 200
    assert "Plugin injection" in doc["html"]
    assert doc["annotation_projection"]["concept_content_rev"] == doc["concept_content_rev"]
    assert doc["annotation_projection"]["complete"] is True


def test_plugin_additive_heading_keeps_projection_complete(annotation_server):
    """Plugin that ADDS a heading must NOT break projection completeness."""
    server, _, _, _ = annotation_server
    server.plugin = type("Plugin", (), {"on_concept_render": lambda self, concept, html:
        html + "<h2>Plugin heading</h2>"})()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/__data/doc?id=note")
    response = conn.getresponse(); doc = json.loads(response.read()); conn.close()
    assert response.status == 200
    assert doc["annotation_projection"]["complete"] is True


def test_plugin_additive_list_item_keeps_projection_complete(annotation_server):
    """Plugin that ADDS a list item must NOT break projection completeness."""
    server, _, _, _ = annotation_server
    server.plugin = type("Plugin", (), {"on_concept_render": lambda self, concept, html:
        html + "<ul><li>Plugin item</li></ul>"})()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/__data/doc?id=note")
    response = conn.getresponse(); doc = json.loads(response.read()); conn.close()
    assert response.status == 200
    assert doc["annotation_projection"]["complete"] is True


def test_plugin_removal_of_owned_node_makes_incomplete(annotation_server):
    """Plugin that REMOVES a renderer-owned paragraph makes projection
    incomplete because the canonical block has no DOM node to pair with."""
    server, _, _, _ = annotation_server
    server.plugin = type("Plugin", (), {"on_concept_render": lambda self, concept, html:
        __import__("re").sub(r'<p\s[^>]*>target here</p>', "", html)})()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/__data/doc?id=note")
    response = conn.getresponse(); doc = json.loads(response.read()); conn.close()
    assert response.status == 200
    assert doc["annotation_projection"]["complete"] is False


def test_plugin_cannot_forge_projection_ownership(annotation_server):
    """A plugin cannot forge ownership by injecting a data-okf-projection-key
    attribute on its added element. The forged key is stripped, and the
    fail-closed contract forces complete=False for any pre-finalization
    projection-key occurrence (ReviewAll r7)."""
    server, _, _, _ = annotation_server
    server.plugin = type("Plugin", (), {"on_concept_render": lambda self, concept, html:
        html + '<p data-okf-projection-key="999">Forged key</p>'})()
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/__data/doc?id=note")
    response = conn.getresponse(); doc = json.loads(response.read()); conn.close()
    assert response.status == 200
    # Fail-closed: the pre-finalization projection-key forces incomplete.
    assert doc["annotation_projection"]["complete"] is False, (
        "forged data-okf-projection-key must force complete=False"
    )
    # The forged key "999" must not appear in the actual projection blocks
    for block in doc["annotation_projection"]["blocks"]:
        if block.get("dom_key") is not None:
            assert block["dom_key"] != 999, "forged projection key survived in blocks"
    # The forged key "999" must be stripped from the rendered HTML.
    html = doc["html"]
    assert 'data-okf-projection-key="999"' not in html


def test_plugin_initial_page_and_data_doc_projection_parity(annotation_server):
    """Group 2: initial concept-page and /__data/doc produce identical
    post-plugin body HTML and projection under the same configured plugin.
    Exercises both public HTTP paths, not just helper functions."""
    server, studio, _, _ = annotation_server
    import http.client, re as _re
    injection = '<span data-plugin-marker="parity">Injected</span>'
    server.plugin = type("Plugin", (), {"on_concept_render": lambda self, concept, html: html + injection})()

    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    # Initial page: GET /note
    conn.request("GET", "/note")
    resp = conn.getresponse(); page_html = resp.read().decode("utf-8"); assert resp.status == 200
    # /__data/doc: GET /__data/doc?id=note
    conn.request("GET", "/__data/doc?id=note")
    resp2 = conn.getresponse(); doc_json = json.loads(resp2.read()); assert resp2.status == 200
    conn.close()

    # Plugin injection present in both
    assert "Injected" in page_html, "plugin injection missing from initial page"
    assert "Injected" in doc_json["html"], "plugin injection missing from /__data/doc"

    # Extract embedded projection from initial page
    proj_match = _re.search(r'<script type="application/json" id="okf-annotation-projection">(.*?)</script>', page_html, _re.S)
    assert proj_match, "no embedded projection in initial page"
    page_proj = json.loads(proj_match.group(1))

    # Compare projection records
    doc_proj = doc_json["annotation_projection"]
    assert page_proj["concept_content_rev"] == doc_proj["concept_content_rev"], "revision mismatch"
    assert page_proj["complete"] == doc_proj["complete"], "completeness mismatch"
    assert page_proj["blocks"] == doc_proj["blocks"], "projection blocks differ"

    # Body HTML from initial page must match /__data/doc html
    body_match = _re.search(r'<div class="okf-prose okf-page__body">\s*(.*?)\s*</div>', page_html, _re.S)
    assert body_match, "no body div in initial page"
    page_body = body_match.group(1).strip()
    doc_body = doc_json["html"].strip()
    assert page_body == doc_body, f"body HTML differs:\npage: {page_body[:200]}\ndoc: {doc_body[:200]}"


def test_comment_resolution_and_doc_projection_revision_parity(annotation_server):
    server, _, comment, source = annotation_server
    assert request(server, valid_request(comment, source), key="parity-reanchor-key")[0] == 200
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/__data/doc?id=note")
    doc = json.loads(conn.getresponse().read())
    conn.request("GET", "/__comments?concept=note")
    comments = json.loads(conn.getresponse().read())["comments"]; conn.close()
    resolution = next(row["anchor_resolution"] for row in comments if row["id"] == comment["id"])
    assert resolution["concept_content_rev"] == doc["concept_content_rev"]
    assert doc["annotation_projection"]["concept_content_rev"] == doc["concept_content_rev"]


@pytest.mark.parametrize("mutate,key,code", [
    (lambda b: {**b, "extra": 1}, "http-key-0002", "invalid_request"),
    (lambda b: {**b, "comment_id": 3}, "http-key-0003", "invalid_request"),
    (lambda b: {**b, "expected_row_token": ""}, "http-key-0004", "invalid_request"),
    (lambda b: {**b, "expected_concept_content_rev": "BAD"}, "http-key-0005", "invalid_request"),
    (lambda b: {**b, "anchor": {**b["anchor"], "extra": 1}}, "http-key-0006", "invalid_anchor"),
])
def test_http_malformed_closed_matrix(annotation_server, mutate, key, code):
    server, _, comment, source = annotation_server
    status, data, _ = request(server, mutate(valid_request(comment, source)), key=key)
    assert status == 400 and data["error"]["code"] == code


def test_http_duplicate_json_key_bad_key_stale_and_cross_context(annotation_server):
    server, studio, comment, source = annotation_server
    body = valid_request(comment, source)
    raw = json.dumps(body)[:-1] + ',"comment_id":"' + comment["id"] + '"}'
    assert request(server, raw, raw=True, key="http-key-0007")[1]["error"]["code"] == "invalid_request"
    assert request(server, "{", raw=True, key="http-key-0007b")[1]["error"]["code"] == "invalid_request"
    assert request(server, body, key=None)[1]["error"]["code"] == "invalid_request"
    assert request(server, body, key="bad")[1]["error"]["code"] == "invalid_request"
    assert request(server, body, key="http-key-0007c", content_type="text/plain")[1]["error"]["code"] == "invalid_request"
    stale = {**body, "expected_row_token": "r2:stale"}
    assert request(server, stale, key="http-key-0008")[1]["error"]["code"] == "comment_conflict"
    stale_doc = {**body, "expected_concept_content_rev": "0" * 64}
    assert request(server, stale_doc, key="http-key-0009")[1]["error"]["code"] == "concept_conflict"
    missing = {**body, "comment_id": "01JABED9K1"}
    assert request(server, missing, key="http-key-0010")[1]["error"]["code"] == "comment_not_found"


def test_http_idempotency_conflict_and_unavailable(annotation_server, monkeypatch):
    server, studio, comment, source = annotation_server
    body = valid_request(comment, source)
    assert request(server, body, key="http-key-conflict")[0] == 200
    different = {**body, "expected_row_token": "r2:different"}
    status, data, _ = request(server, different, key="http-key-conflict")
    assert status == 409 and data["error"]["code"] == "idempotency_conflict"
    current = studio.get_comment(comment["id"])
    fresh = {**body, "expected_row_token": current["row_token"]}
    monkeypatch.setattr(studio, "reanchor_comment", lambda **_: (_ for _ in ()).throw(OSError("disk")))
    status, data, _ = request(server, fresh, key="http-key-unavailable")
    assert status == 503 and data["error"]["code"] == "unavailable"


def test_unknown_anchor_extensions_round_trip_and_projection_not_persisted(annotation_server):
    server, studio, _, source = annotation_server
    comment = studio.post_comment(
        concept="note", body="extended",
        anchor={"future": {"keep": True}, "block": {"future_nested": 7}},
    )
    body = valid_request(comment, source)
    status, data, _ = request(server, body, key="extension-key-1")
    assert status == 200
    saved = studio.get_comment(comment["id"])
    assert saved["anchor"]["future"] == {"keep": True}
    assert saved["anchor"]["block"]["future_nested"] == 7
    studio.update_comment(comment["id"], body="edited", state="claimed")
    after = studio.get_comment(comment["id"])
    assert after["anchor"]["future"] == {"keep": True}
    assert after["anchor"]["block"]["future_nested"] == 7
    assert "anchor_resolution" not in server.studio.directives_path.read_text()


def test_projection_recomputes_content_changes_and_restore(annotation_server):
    server, studio, comment, source = annotation_server
    body = valid_request(comment, source)
    assert request(server, body, key="projection-key-1")[0] == 200

    def projections():
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", "/__comments?concept=note")
        rows = json.loads(conn.getresponse().read())["comments"]
        conn.close()
        return {row["id"]: row["anchor_resolution"] for row in rows}

    assert projections()[comment["id"]]["status"] == "resolved"
    path = studio.bundle_root / "note.md"; original = path.read_text()
    path.write_text(original.replace("target", "gone"), encoding="utf-8")
    server.bundle = Bundle.load(studio.bundle_root)
    assert projections()[comment["id"]]["status"] == "stale"
    path.write_text(original + "target here\n", encoding="utf-8")
    ambiguous = studio.post_comment(concept="note", body="legacy",
                                    anchor={"kind": "text", "ref": "target"})
    invalid = studio.post_comment(concept="note", body="invalid", anchor={"kind": "text", "ref": ""})
    server.bundle = Bundle.load(studio.bundle_root)
    current = projections()
    assert current[ambiguous["id"]]["status"] == "ambiguous"
    assert current[invalid["id"]]["status"] == "invalid"
    # Undo restoration changes canonical bytes back while anchor intent stays.
    path.write_text(original, encoding="utf-8")
    server.bundle = Bundle.load(studio.bundle_root)
    assert projections()[comment["id"]]["status"] == "resolved"


def test_frontmatter_only_change_changes_annotation_revision(annotation_server):
    server, studio, _, _ = annotation_server
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/__data/doc?id=note")
    before = json.loads(conn.getresponse().read())
    path = studio.bundle_root / "note.md"
    path.write_text(path.read_text().replace("type: note", "type: note\ntitle: Changed"), encoding="utf-8")
    server.bundle = Bundle.load(studio.bundle_root)
    conn.request("GET", "/__data/doc?id=note")
    after = json.loads(conn.getresponse().read()); conn.close()
    assert before["raw"] == after["raw"]
    assert before["concept_content_rev"] != after["concept_content_rev"]
    assert before["annotation_source"] != after["annotation_source"]


def test_last_event_id_replays_oldest_first_over_http(annotation_server):
    server, studio, _, _ = annotation_server
    events = [studio.append_event({"type": "comment", "id": f"E{i}"}) for i in range(3)]
    sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
    request_bytes = (
        "GET /__events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"Last-Event-ID: {events[0]['event_cursor']}\r\nConnection: close\r\n\r\n"
    ).encode()
    sock.sendall(request_bytes)
    sock.settimeout(5)
    data = b""
    marker = f"id: {events[2]['event_cursor']}\n".encode()
    while marker not in data:
        data += sock.recv(65536)
    sock.close()
    first = data.index(f"id: {events[1]['event_cursor']}\n".encode())
    second = data.index(marker)
    assert first < second


def test_sse_subscribe_first_handoff_delivers_racing_event_once(annotation_server, monkeypatch):
    server, studio, _, _ = annotation_server
    baseline = studio.append_event({"type": "proof", "id": "baseline"})
    snapshot_started = threading.Event()
    release_snapshot = threading.Event()
    original = studio.read_event_snapshot

    def blocked_snapshot():
        snapshot_started.set()
        assert release_snapshot.wait(5)
        return original()

    monkeypatch.setattr(studio, "read_event_snapshot", blocked_snapshot)
    sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
    sock.sendall(("GET /__events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                  f"Last-Event-ID: {baseline['event_cursor']}\r\n"
                  "Connection: close\r\n\r\n").encode())
    assert snapshot_started.wait(5)
    racing = studio.append_event({"type": "proof", "id": "racing"})
    release_snapshot.set()
    sock.settimeout(5); data = b""
    marker = f"id: {racing['event_cursor']}\n".encode()
    while marker not in data:
        data += sock.recv(65536)
    sock.close()
    assert data.count(marker) == 1


@pytest.mark.parametrize("target", ["/__events?since=0&since=1", "/__events?unknown=1"])
def test_sse_query_validation_matches_polling(annotation_server, target):
    server, _, _, _ = annotation_server
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    conn.request("GET", target)
    response = conn.getresponse(); payload = json.loads(response.read())
    conn.close()
    assert response.status == 400
    assert payload == {"ok": False, "error": "invalid_request"}


def test_http_guard_precedence_body_cap_no_edit_and_rate_limit(annotation_server):
    server, _, comment, source = annotation_server
    body = valid_request(comment, source)
    # Authentication hides body and mode details.
    status, data, _ = request(server, "{", raw=True, token="wrong", key="bad")
    assert status == 403 and data["error"]["code"] == "forbidden"
    assert request(server, body, origin="http://evil.example", key="http-key-0011")[1]["error"]["code"] == "forbidden"
    assert request(server, body, host="evil.example", key="http-key-0012")[1]["error"]["code"] == "forbidden"
    server.studio_edit = False
    before_rate = list(getattr(server, "_reanchor_rate_attempts", []))
    status, data, _ = request(server, "x" * 70000, raw=True, key="http-key-0013")
    assert status == 403 and data["error"]["code"] == "read_only"
    assert getattr(server, "_reanchor_rate_attempts", []) == before_rate
    server.studio_edit = True
    status, data, _ = request(server, "x" * 70000, raw=True, key="http-key-0014")
    assert status == 413 and data["error"]["code"] == "invalid_request"
    server._reanchor_rate_attempts = [0.0] * 120
    # Monotonic zeroes are expired; use the current clock deterministically.
    import time
    server._reanchor_rate_attempts = [time.monotonic()] * 120
    status, data, retry = request(server, body, key="http-key-0015")
    assert status == 429 and data["error"]["code"] == "rate_limited" and retry == "60"


@pytest.mark.parametrize("ending", ["timeout", "eof"])
def test_incomplete_reanchor_body_does_not_consume_rate_quota(
        annotation_server, ending):
    server, _, _, _ = annotation_server
    server.request_body_deadline_s = 0.05
    before = list(getattr(server, "_reanchor_rate_attempts", []))
    sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
    sock.sendall(
        b"POST /__comment-reanchor HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"X-OKF-Token: secret\r\nContent-Type: application/json\r\n"
        b"Content-Length: 100\r\nConnection: close\r\n\r\n{}"
    )
    if ending == "eof":
        sock.shutdown(socket.SHUT_WR)
    response = sock.makefile("rb").read()
    sock.close()
    assert (b"HTTP/1.1 408" if ending == "timeout" else b"HTTP/1.1 400") in response
    assert getattr(server, "_reanchor_rate_attempts", []) == before


def test_complete_reanchor_consumes_once_and_concurrent_completion_enforces_cap(
        annotation_server):
    server, _, comment, source = annotation_server
    body = valid_request(comment, source)
    before = len(getattr(server, "_reanchor_rate_attempts", []))
    status, _, _ = request(server, body, key="complete-consumes-once")
    assert status == 200
    assert len(server._reanchor_rate_attempts) == before + 1

    server._reanchor_rate_attempts = [time.monotonic()] * 119
    barrier = threading.Barrier(3)
    statuses = []

    def send(key):
        barrier.wait()
        statuses.append(request(server, body, key=key)[0])

    workers = [threading.Thread(target=send, args=(f"concurrent-cap-{i}",))
               for i in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(5)
    assert statuses.count(429) == 1
    assert len(server._reanchor_rate_attempts) == 120


def test_every_post_requires_nonempty_allowlisted_host(annotation_server):
    server, _, _, _ = annotation_server
    raw = (b"POST /__comment-reanchor HTTP/1.0\r\nX-OKF-Token: secret\r\n"
           b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
    status, _ = raw_request(server, raw)
    assert status == 403
    raw = raw.replace(b"HTTP/1.0\r\n", b"HTTP/1.0\r\nHost: evil.example\r\n")
    status, _ = raw_request(server, raw)
    assert status == 403


@pytest.mark.parametrize("value", ["false", 0, 1, [], {}])
def test_comment_update_archive_requires_exact_boolean(annotation_server, value):
    server, studio, _, _ = annotation_server
    row = studio.post_comment(concept="note", body="resolved", state="resolved")
    status, _ = post_json(server, "/__comment-update", {
        "id": row["id"], "expected_row_token": row["row_token"], "archived": value,
    })
    assert status == 400
    assert studio.get_comment(row["id"])["archived"] is False


def test_post_start_canonical_concept_is_read_rendered_and_resolved(annotation_server):
    server, studio, _, _ = annotation_server
    source = "---\ntype: note\n---\nnew target\n"
    (studio.bundle_root / "added.md").write_text(source, encoding="utf-8")
    row = studio.post_comment(concept="added", body="new",
        anchor=capture_anchor(source, source.index("target"), source.index("target") + 6))
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/added"); page = conn.getresponse(); html = page.read().decode()
    assert page.status == 200 and "new target" in html
    conn.request("GET", "/__data/doc?id=added"); doc = conn.getresponse(); payload = json.loads(doc.read())
    assert doc.status == 200 and payload["annotation_source"] == source
    conn.request("GET", "/__comments?concept=added")
    comments = json.loads(conn.getresponse().read())["comments"]; conn.close()
    assert next(item for item in comments if item["id"] == row["id"])["anchor_resolution"]["status"] == "resolved"


def test_initial_page_and_live_doc_use_current_bytes_during_watcher_lag(annotation_server):
    server, studio, _, _ = annotation_server
    changed = "---\ntype: note\ntitle: Current\n---\n# Current\n\ncurrent bytes\n"
    (studio.bundle_root / "note.md").write_text(changed, encoding="utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/note"); page = conn.getresponse(); html = page.read().decode()
    conn.request("GET", "/__data/doc?id=note"); doc = json.loads(conn.getresponse().read()); conn.close()
    assert page.status == 200 and "current bytes" in html
    assert doc["annotation_source"] == changed and "current bytes" in doc["html"]
    assert doc["concept_content_rev"] in html


@pytest.mark.parametrize("bad_header", [
    b"Transfer-Encoding: chunked\r\n",
    b"Content-Length: -1\r\n",
    b"Content-Length: nope\r\n",
])
def test_raw_guard_auth_masks_transport_details(annotation_server, bad_header):
    server, _, _, _ = annotation_server
    base = b"POST /__comment-reanchor HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
    status, body = raw_request(server, base + bad_header + b"\r\n")
    assert status == 403 and body["error"]["code"] == "forbidden"
    authenticated = base + b"X-OKF-Token: secret\r\n" + bad_header + b"\r\n"
    status, body = raw_request(server, authenticated)
    assert status == 400 and body["error"]["code"] == "invalid_request"


def test_raw_oversized_target_auth_mask_and_mode_precedence(annotation_server):
    server, _, _, _ = annotation_server
    target = b"/__comment-reanchor?x=" + b"x" * 20000
    common = b" HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nContent-Length: 0\r\n"
    status, body = raw_request(server, b"POST " + target + common + b"\r\n")
    assert status == 403 and body["error"]["code"] == "forbidden"
    status, body = raw_request(server, b"POST " + target + common + b"X-OKF-Token: secret\r\n\r\n")
    assert status == 400 and body["error"]["code"] == "invalid_request"
    server.studio = None; server.studio_edit = False
    status, body, _ = request(server, {}, key="precedence-key-1")
    assert status == 403 and body["error"]["code"] == "read_only"
    server.studio_edit = True
    import time
    server._reanchor_rate_attempts = [time.monotonic()] * 120
    status, body, retry = request(server, {}, key="precedence-key-2")
    assert status == 429 and body["error"]["code"] == "rate_limited" and retry == "60"


def test_unavailable_is_after_authenticated_transport_and_request_validation(annotation_server):
    server, _, _, _ = annotation_server
    server.studio = None
    base = b"POST /__comment-reanchor HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nX-OKF-Token: secret\r\n"
    for header in (b"Transfer-Encoding: chunked\r\n", b"Content-Length: -1\r\n",
                   b"Content-Length: nope\r\n"):
        status, body = raw_request(server, base + header + b"\r\n")
        assert status == 400 and body["error"]["code"] == "invalid_request"
    status, body, _ = request(server, "x" * 70000, raw=True, key="none-cap-key")
    assert status == 413 and body["error"]["code"] == "invalid_request"
    status, body, _ = request(server, "{", raw=True, key="none-json-key")
    assert status == 400 and body["error"]["code"] == "invalid_request"
    status, body, _ = request(server, {}, key="none-type-key", content_type="text/plain")
    assert status == 400 and body["error"]["code"] == "invalid_request"
    status, body, _ = request(server, {}, key="none-valid-key")
    assert status == 503 and body["error"]["code"] == "unavailable"


def test_disk_crlf_annotation_source_revision_offsets_and_reanchor(annotation_server):
    server, studio, comment, _ = annotation_server
    path = studio.bundle_root / "note.md"
    exact_bytes = b"---\r\ntype: note\r\n---\r\nTarget CRLF here\r\n"
    path.write_bytes(exact_bytes)
    server.bundle = Bundle.load(studio.bundle_root)
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/__data/doc?id=note")
    doc = json.loads(conn.getresponse().read()); conn.close()
    assert doc["annotation_source"].encode("utf-8") == exact_bytes
    assert doc["concept_content_rev"] == concept_content_rev(exact_bytes.decode("utf-8"))
    lf = exact_bytes.replace(b"\r\n", b"\n").decode("utf-8")
    assert doc["concept_content_rev"] != concept_content_rev(lf)
    source = doc["annotation_source"]
    start = source.index("CRLF")
    anchor = capture_anchor(source, start, start + 4)
    assert anchor["position"] == {"start": start, "end": start + 4}
    block_text = source[anchor["block"]["start"]:anchor["block"]["end"]]
    import hashlib
    assert anchor["block"]["hash"] == "sha256:" + hashlib.sha256(
        block_text.replace("\r\n", "\n").encode("utf-8")
    ).hexdigest()
    body = {"comment_id": comment["id"], "expected_row_token": comment["row_token"],
            "expected_concept_content_rev": doc["concept_content_rev"], "anchor": anchor}
    status, response, _ = request(server, body, key="crlf-reanchor-key")
    assert status == 200 and response["concept_content_rev"] == doc["concept_content_rev"]


def test_post_load_symlink_swap_fails_closed_on_all_annotation_paths(annotation_server, tmp_path):
    server, studio, comment, source = annotation_server
    body = valid_request(comment, source)
    concept_path = studio.bundle_root / "note.md"
    external = tmp_path.parent / f"{tmp_path.name}-external-secret.md"
    external.write_text("---\ntype: note\n---\nEXTERNAL_SECRET target\n", encoding="utf-8")
    concept_path.unlink()
    concept_path.symlink_to(external)

    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/__data/doc?id=note")
    response = conn.getresponse(); data_raw = response.read()
    assert response.status == 503
    assert b"EXTERNAL_SECRET" not in data_raw
    assert json.loads(data_raw)["error"]["code"] == "unavailable"

    conn.request("GET", "/__comments?concept=note")
    response = conn.getresponse(); comments_raw = response.read()
    assert response.status == 503
    assert b"EXTERNAL_SECRET" not in comments_raw
    assert json.loads(comments_raw)["error"]["code"] == "unavailable"
    conn.close()

    status, payload, _ = request(server, body, key="symlink-swap-key")
    assert status == 503 and payload["error"]["code"] == "unavailable"
    assert "EXTERNAL_SECRET" not in json.dumps(payload)
    assert studio.get_comment(comment["id"])["row_token"] == comment["row_token"]


def test_public_comment_create_anchor_validation_matrix(annotation_server):
    server, _, _, source = annotation_server
    base = {"concept": "note", "body": "ask"}
    assert post_json(server, "/__comment", base)[0] == 201
    assert post_json(server, "/__comment", {**base, "anchor": {"kind": "concept"}})[0] == 201
    valid = capture_anchor(source, source.index("target"), source.index("target") + 6)
    assert post_json(server, "/__comment", {**base, "anchor": valid})[0] == 201
    invalid = [
        None, "text", {"kind": "text", "ref": "target"},
        {**valid, "schema": 3}, {**valid, "extra": True},
        {**valid, "position": {"start": valid["position"]["start"] + 1,
                                "end": valid["position"]["end"] + 1}},
        {**valid, "block": {**valid["block"], "hash": "sha256:" + "0" * 64}},
        {**valid, "block": {**valid["block"], "id": "paragraph:999"}},
        {**valid, "heading": {"path": ["forged"]}},
        {**valid, "prefix": "forged"}, {**valid, "suffix": "forged"},
    ]
    for index, anchor in enumerate(invalid):
        status, payload = post_json(server, "/__comment", {**base, "anchor": anchor},
                                    key=f"create-invalid-{index}")
        assert status == 400 and payload["ok"] is False


@pytest.mark.parametrize("anchor,key", [
    ({"kind": "concept"}, "missing-concept-anchor"),
    ("omitted", "missing-omitted-anchor"),
])
def test_comment_create_rejects_nonexistent_canonical_concept_for_every_mode(
        annotation_server, anchor, key):
    server, _, _, _ = annotation_server
    body = {"concept": "missing", "body": "ask"}
    if anchor != "omitted":
        body["anchor"] = anchor
    status, payload = post_json(server, "/__comment", body, key=key)
    assert status == 404 and payload["error"] == "target_not_found"
    assert payload["ok"] is False


def test_comment_create_rejects_nonexistent_canonical_concept_for_v2_anchor(
        annotation_server):
    server, _, _, source = annotation_server
    anchor = capture_anchor(source, source.index("target"), source.index("target") + 6)
    status, payload = post_json(server, "/__comment", {
        "concept": "missing", "body": "ask", "anchor": anchor,
    }, key="missing-v2-anchor")
    assert status == 404 and payload == {"ok": False, "error": "target_not_found"}


def test_reply_uses_parent_concept_and_rejects_cross_concept_redirect(annotation_server):
    server, studio, parent, source = annotation_server
    (studio.bundle_root / "other.md").write_text(
        "---\ntype: note\n---\nother target\n", encoding="utf-8")
    anchor = capture_anchor(source, source.index("target"), source.index("target") + 6)
    status, payload = post_json(server, "/__comment", {
        "concept": "other", "body": "reply", "parent_id": parent["id"], "anchor": anchor,
    }, key="reply-cross-context")
    assert status == 400 and payload["error"] == "invalid_request"
    assert len([row for row in studio.list_comments() if row.get("parent_id") == parent["id"]]) == 0
    status, payload = post_json(server, "/__comment", {
        "concept": "note", "body": "reply", "parent_id": parent["id"],
    }, key="reply-effective-concept")
    assert status == 201 and payload["comment"]["concept"] == "note"
    event = next(event for event in reversed(studio.read_event_snapshot())
                 if event.get("id") == payload["comment"]["id"])
    assert event["concept"] == payload["comment"]["concept"]


def test_reply_rejects_unavailable_parent_effective_concept(annotation_server):
    server, studio, parent, _ = annotation_server
    (studio.bundle_root / "note.md").unlink()
    status, payload = post_json(server, "/__comment", {
        "concept": "note", "body": "reply", "parent_id": parent["id"],
    }, key="reply-missing-concept")
    assert status == 503 and payload["error"] == "unavailable"
    assert len([row for row in studio.list_comments() if row.get("parent_id") == parent["id"]]) == 0


def test_finalized_create_replays_after_canonical_delete_and_changed_body_conflicts(
        annotation_server):
    server, studio, _, source = annotation_server
    anchor = capture_anchor(source, source.index("target"), source.index("target") + 6)
    request_body = {"concept": "note", "body": "durable", "anchor": anchor}
    status, original = post_json(server, "/__comment", request_body, key="create-delete-replay")
    assert status == 201
    (studio.bundle_root / "note.md").unlink()
    status, replay = post_json(server, "/__comment", request_body, key="create-delete-replay")
    assert status == 201 and replay == original
    status, conflict = post_json(server, "/__comment", {**request_body, "body": "different"},
                                 key="create-delete-replay")
    assert status == 409 and conflict["error"] == "idempotency_conflict"


def test_finalized_reanchor_replays_after_canonical_delete(annotation_server):
    server, studio, comment, source = annotation_server
    body = valid_request(comment, source)
    status, original, _ = request(server, body, key="reanchor-delete-replay")
    assert status == 200
    (studio.bundle_root / "note.md").unlink()
    status, replay, _ = request(server, body, key="reanchor-delete-replay")
    assert status == 200 and replay["replayed"] is True
    assert {k: v for k, v in replay.items() if k != "replayed"} == {
        k: v for k, v in original.items() if k != "replayed"
    }


def test_comment_create_validation_and_append_hold_canonical_writer_lock(
        annotation_server, monkeypatch):
    server, studio, _, source = annotation_server
    anchor = capture_anchor(source, source.index("target"), source.index("target") + 6)
    entered = threading.Event(); release = threading.Event(); writer_entered = threading.Event()
    original = studio.post_comment_idempotent
    def blocked(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)
    monkeypatch.setattr(studio, "post_comment_idempotent", blocked)
    result = {}
    creator = threading.Thread(target=lambda: result.update(
        create=post_json(server, "/__comment", {
            "concept": "note", "body": "locked", "anchor": anchor,
        }, key="create-concept-lock")))
    creator.start(); assert entered.wait(5)
    def writer():
        writer_entered.set()
        studio.save_concept(concept_id="note", raw=source.replace("target", "changed"),
                            actor="test", publish=False)
    mutation = threading.Thread(target=writer)
    mutation.start(); assert writer_entered.wait(5)
    release.set(); creator.join(5); mutation.join(5)
    assert not creator.is_alive() and not mutation.is_alive()
    assert result["create"][0] == 201
    stored = studio.get_comment(result["create"][1]["comment"]["id"])
    assert stored["anchor"] == anchor


def test_public_comment_update_requires_and_enforces_row_token(annotation_server):
    server, _, comment, _ = annotation_server
    status, _ = post_json(server, "/__comment-update", {"id": comment["id"], "body": "edit"})
    assert status == 400
    status, payload = post_json(server, "/__comment-update", {
        "id": comment["id"], "expected_row_token": "r2:stale", "body": "edit"
    })
    assert status == 409 and payload["error"] == "stale"
    status, payload = post_json(server, "/__comment-update", {
        "id": comment["id"], "expected_row_token": comment["row_token"], "body": "edit"
    })
    assert status == 200 and payload["comment"]["body"] == "edit"


def test_reanchor_revision_cas_holds_canonical_writer_lock(annotation_server, monkeypatch):
    server, studio, comment, source = annotation_server
    body = valid_request(comment, source)
    entered = threading.Event(); release = threading.Event()
    original = studio.reanchor_comment
    def blocked(**kwargs):
        entered.set(); assert release.wait(5)
        return original(**kwargs)
    monkeypatch.setattr(studio, "reanchor_comment", blocked)
    response = {}
    request_thread = threading.Thread(target=lambda: response.update(
        result=request(server, body, key="commit-lock-key")))
    request_thread.start(); assert entered.wait(5)
    writer_started = threading.Event(); writer_done = threading.Event()
    changed = source.replace("target", "changed")
    def write():
        writer_started.set()
        studio.save_concept(concept_id="note", raw=changed, actor="test", publish=False)
        writer_done.set()
    writer = threading.Thread(target=write); writer.start(); assert writer_started.wait(5)
    assert not writer_done.wait(0.2)
    release.set(); request_thread.join(5); writer.join(5)
    assert response["result"][0] == 200
    assert writer_done.is_set()


@pytest.mark.parametrize("last_id,reason", [("not-a-number", "invalid_cursor"), ("999999", "future_cursor")])
def test_sse_malformed_and_future_cursor_resync(annotation_server, last_id, reason):
    server, studio, _, _ = annotation_server
    studio.append_event({"type": "proof"})
    sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
    sock.sendall(("GET /__events HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                  f"Last-Event-ID: {last_id}\r\nConnection: close\r\n\r\n").encode())
    sock.settimeout(5); data = b""
    marker = f'"reason": "{reason}"'.encode()
    while marker not in data:
        data += sock.recv(65536)
    sock.close(); assert marker in data


def test_sse_cold_session_positive_cursor_requires_canonical_resync(annotation_server):
    server, studio, _, _ = annotation_server
    studio.events_path.write_text("", encoding="utf-8")
    studio.event_sequence_path.unlink(missing_ok=True)
    studio.seq = 0
    sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
    sock.sendall(b"GET /__events HTTP/1.1\r\nHost: 127.0.0.1\r\nLast-Event-ID: 7\r\nConnection: close\r\n\r\n")
    sock.settimeout(5); data = b""
    marker = b'"type": "resync"'
    while marker not in data:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    sock.close()
    assert marker in data
    assert b'"reason": "history_empty"' in data


def test_sse_ready_uses_canonical_content_hash_without_changing_numeric_identities(
        annotation_server):
    server, studio, _, source = annotation_server
    event = studio.append_event({"type": "proof"})
    sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
    sock.sendall(b"GET /__events HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
    sock.settimeout(5); data = b""
    while b"event: ready" not in data or b"\n\n" not in data.split(b"event: ready", 1)[1]:
        data += sock.recv(65536)
    sock.close()
    frame = next(part for part in data.split(b"\n\n") if b"event: ready" in part)
    payload = json.loads(next(line[6:] for line in frame.split(b"\n")
                              if line.startswith(b"data: ")))
    assert payload["doc_revs"]["note"] == concept_content_rev(source)
    assert len(payload["doc_revs"]["note"]) == 64
    assert isinstance(payload["rev"], int)
    assert isinstance(event["rev"], int)
    assert isinstance(event["event_cursor"], int)


def test_sse_ready_inventory_includes_post_start_concept(annotation_server):
    server, studio, _, _ = annotation_server
    late = "---\ntype: note\n---\nlate\n"
    (studio.bundle_root / "late.md").write_text(late, encoding="utf-8")
    sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
    sock.sendall(b"GET /__events HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
    data = b""
    while b"event: ready" not in data or b"\n\n" not in data.split(b"event: ready", 1)[1]:
        data += sock.recv(65536)
    sock.close()
    frame = next(part for part in data.split(b"\n\n") if b"event: ready" in part)
    payload = json.loads(next(line[6:] for line in frame.split(b"\n") if line.startswith(b"data: ")))
    assert payload["doc_revs"]["late"] == concept_content_rev(late)


def test_polling_validation_and_resync_revision_parity(annotation_server):
    server, _, _, _ = annotation_server
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/__data/events?limit=bad")
    response = conn.getresponse(); payload = json.loads(response.read()); conn.close()
    assert response.status == 200 and isinstance(payload["events"], list)
    for query in ("limit=0", "limit=1001", "order=sideways", "limit=2&limit=3"):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", f"/__data/events?{query}")
        response = conn.getresponse(); payload = json.loads(response.read()); conn.close()
        assert response.status == 400 and payload["error"] == "invalid_request"
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/__data/events?since=bad")
    response = conn.getresponse(); payload = json.loads(response.read()); conn.close()
    assert response.status == 200
    assert isinstance(payload["rev"], int) and payload["doc_revs"]["note"]


def test_sse_more_than_10000_interval_resyncs_without_truncation(annotation_server):
    server, studio, _, _ = annotation_server
    rows = [{"type": "proof", "id": str(index), "event_id": f"E{index}",
             "seq": index + 1, "event_cursor": index + 1} for index in range(10002)]
    with open(studio.events_path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
    sock.sendall(b"GET /__events HTTP/1.1\r\nHost: 127.0.0.1\r\nLast-Event-ID: 1\r\nConnection: close\r\n\r\n")
    sock.settimeout(5); data = b""
    marker = b'"reason": "replay_too_large"'
    while marker not in data:
        data += sock.recv(65536)
    sock.close(); assert marker in data


def test_polling_more_than_10000_interval_has_sse_resync_parity(annotation_server):
    server, studio, _, _ = annotation_server
    rows = [{"type": "proof", "id": str(index), "event_id": f"E{index}",
             "seq": index + 1, "event_cursor": index + 1} for index in range(10002)]
    studio.events_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    conn.request("GET", "/__data/events?since=1")
    response = conn.getresponse(); payload = json.loads(response.read()); conn.close()
    assert response.status == 200
    assert payload["events"] == [] and payload["resync"] is True
    assert payload["reason"] == "replay_too_large"


def test_polling_cursor_pages_oldest_first_without_skipping_middle(annotation_server):
    server, studio, _, _ = annotation_server
    events = [studio.append_event({"type": "poll-page", "index": index}) for index in range(6)]
    cursor = events[0]["event_cursor"]
    observed = []
    for _ in range(2):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", f"/__data/events?since={cursor}&limit=2")
        response = conn.getresponse(); payload = json.loads(response.read()); conn.close()
        assert response.status == 200
        observed.extend(row["event_cursor"] for row in payload["events"])
        cursor = payload["events"][-1]["event_cursor"]
    assert observed == [row["event_cursor"] for row in events[1:5]]


def test_polling_cursor_resync_matrix(annotation_server):
    server, studio, _, _ = annotation_server
    events = [studio.append_event({"type": "poll-proof"}) for _ in range(3)]
    def poll(value):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        conn.request("GET", f"/__data/events?since={value}")
        response = conn.getresponse(); payload = json.loads(response.read()); conn.close()
        assert response.status == 200
        return payload
    assert poll("bad")["reason"] == "invalid_cursor"
    assert poll(str(events[-1]["event_cursor"] + 100))["reason"] == "future_cursor"
    gap = events[0]["event_cursor"] + 50
    assert poll(str(gap))["reason"] in {"cursor_gap", "future_cursor"}
    # Retain only the newest row to make the oldest cursor provably expired.
    studio.events_path.write_text(json.dumps(events[-1]) + "\n")
    assert poll(str(events[0]["event_cursor"]))["reason"] == "cursor_expired"
    studio.events_path.write_text("")
    assert poll(str(events[-1]["event_cursor"]))["reason"] == "history_empty"


def test_polling_cold_session_positive_cursor_requires_canonical_resync(annotation_server):
    server, studio, _, _ = annotation_server
    studio.events_path.write_text("", encoding="utf-8")
    studio.event_sequence_path.unlink(missing_ok=True)
    studio.seq = 0
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    conn.request("GET", "/__data/events?since=7")
    response = conn.getresponse(); payload = json.loads(response.read()); conn.close()
    assert response.status == 200
    assert payload["events"] == []
    assert payload["resync"] is True
    assert payload["reason"] == "history_empty"


def test_37_blank_blockquotes_canonical_get_emits_unkeyed_records_and_accepts_hr(annotation_server):
    """Finding 4 (HTTP/producer proof): a document with 37 blank blockquotes
    must produce a canonical GET (/__data/doc) whose projection is complete and
    where every blank block-quote block is UNKEYED (dom_key=null, segments=[]),
    while the thematic break remains a keyed zero-segment record. This is the
    real failure shape that previously surfaced 'Canonical document response
    was invalid.' before the producer fix."""
    server, studio, _, _ = annotation_server
    body = "".join("> \n\n" for _ in range(37)) + "target line\n\n---\n\nafter\n"
    (studio.bundle_root / "blanks.md").write_text("---\ntype: note\n---\n" + body, encoding="utf-8")
    server.bundle = Bundle.load(studio.bundle_root)
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    conn.request("GET", "/__data/doc?id=blanks")
    response = conn.getresponse(); doc = json.loads(response.read()); conn.close()
    assert response.status == 200
    proj = doc["annotation_projection"]
    assert proj["complete"] is True, "37-blank-blockquote projection must be complete after the producer fix"
    bq = [b for b in proj["blocks"] if str(b.get("block_id", "")).startswith("block-quote")]
    assert len(bq) == 37, f"expected 37 block-quote blocks, got {len(bq)}"
    # Every blank block-quote must be unkeyed with no segments (post-fix shape).
    for b in bq:
        assert b["dom_key"] is None, f"blank block-quote must be unkeyed: {b}"
        assert b["segments"] == [], f"blank block-quote must have no segments: {b}"
    # The thematic break remains a keyed zero-segment record (the only valid
    # keyed-empty kind under the strict browser validator).
    hr = [b for b in proj["blocks"] if str(b.get("block_id", "")).startswith("hr")]
    assert len(hr) == 1 and hr[0]["dom_key"] is not None and hr[0]["segments"] == [], hr
    # A neighboring keyed paragraph carries the selectable text.
    para = [b for b in proj["blocks"] if str(b.get("block_id", "")).startswith("paragraph")]
    keyed_para = [p for p in para if p["dom_key"] is not None and p["segments"]]
    assert keyed_para, f"expected a keyed paragraph with segments for selection: {para}"
    assert "target line" in doc["annotation_source"]
