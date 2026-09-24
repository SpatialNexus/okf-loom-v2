"""``okf serve --tunnel`` — quick-tunnel plumbing.

Covers the pure URL parser, the CLI flag wiring, and the fail-closed
behaviour when cloudflared is absent. The actual cloudflared subprocess
is never started (network-free suite).
"""
from __future__ import annotations

import http.client
import json
import re
import secrets
import socket
import subprocess
import threading
import time

import pytest

from http.server import ThreadingHTTPServer

from okf_loom import Bundle
from okf_loom.annotations import capture_anchor, concept_content_rev
from okf_loom.server import (
    OKFWikiHandler,
    TunnelStartOwnershipError,
    _clear_owned_tunnel,
    _settle_tunnel_process,
    _ensure_public_mutation_lifecycle,
    _shutdown_tunnel_owner,
    _shutdown_retrieval_runtime,
    _start_public_ingress,
    _start_tunnel_monitor,
    _stop_public_ingress,
    _stop_tunnel_monitor,
    parse_quick_tunnel_url,
    start_quick_tunnel,
    write_server_state,
)
from okf_loom.studio import Studio


def _app_server(bundle_root):
    bundle = Bundle.load(bundle_root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), OKFWikiHandler)
    server.bundle = bundle
    server.config = {}
    server.name = bundle.name
    server.plugin = None
    server._state_lock = threading.Lock()
    server._reload_error = None
    server.studio = Studio.for_bundle(bundle.root, bundle_name=bundle.name)
    server.studio.ensure_session()
    server.csrf_token = secrets.token_hex(16)
    server.allowed_hosts = ("127.0.0.1", "localhost")
    server.base_allowed_hosts = server.allowed_hosts
    server.max_sse_clients = 32
    server.studio_live = True
    server.studio_edit = True
    server.studio_theme = "auto"
    server.tunnel_url = "https://public-test.trycloudflare.com"
    server.tunnel_proc = None
    server.public_tunnel_edit = False
    server._tunnel_lock = threading.Lock()
    server.proposal_service = None
    return server


def _request(server, method, path, *, token=None, headers=None, body=None):
    ingress = server.public_ingress_server
    conn = http.client.HTTPConnection("127.0.0.1", ingress.server_address[1], timeout=3)
    request_headers = {"Host": "localhost", **(headers or {})}
    if token is not None:
        request_headers["X-OKF-Token"] = token
    encoded = None if body is None else json.dumps(body).encode()
    if encoded is not None:
        request_headers["Content-Type"] = "application/json"
        request_headers["Content-Length"] = str(len(encoded))
    conn.request(method, path, body=encoded, headers=request_headers)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response.status, payload


def _local_request(server, method, path, *, token=None, body=None, key=None):
    conn = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=3
    )
    headers = {"Host": "127.0.0.1"}
    if token is not None:
        headers["X-OKF-Token"] = token
    if key is not None:
        headers["Idempotency-Key"] = key
    encoded = None if body is None else json.dumps(body).encode()
    if encoded is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(encoded))
    conn.request(method, path, body=encoded, headers=headers)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response.status, payload


def test_retrieval_runtime_is_primary_owned_across_local_and_public_listeners(
    tmp_path, monkeypatch
):
    import okf_loom.server as server_module
    from okf_loom.retrieval import RetrievalService

    root = tmp_path / "bundle"
    root.mkdir()
    (root / "guide.md").write_text("---\ntype: guide\n---\n# Guide\nalpha\n")
    server = _app_server(root)
    server.retrieval_concurrency = 1
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    ingress = _start_public_ingress(server, allow_public_edit=False)
    original = RetrievalService.search
    entered = threading.Event()
    release = threading.Event()

    def blocked(self, request):
        entered.set()
        release.wait(3)
        return original(self, request)

    monkeypatch.setattr(RetrievalService, "search", blocked)
    public_result = []
    public_thread = threading.Thread(
        target=lambda: public_result.append(
            _request(server, "GET", "/__search?q=alpha&format=json")
        )
    )
    try:
        public_thread.start()
        assert entered.wait(2)
        gate = server._retrieval_gate
        executor = server._retrieval_executor
        assert "_retrieval_gate" not in ingress.__dict__
        assert "_retrieval_executor" not in ingress.__dict__

        status, payload = _local_request(
            server, "GET", "/__search?q=alpha&format=json"
        )
        assert status == 503
        assert json.loads(payload)["error"]["code"] == "retrieval_busy"
        assert server._retrieval_gate is gate
        assert server._retrieval_executor is executor

        release.set()
        public_thread.join(3)
        assert public_result[0][0] == 200
        _stop_public_ingress(server)
        assert executor._shutdown is False
        assert _local_request(
            server, "GET", "/__search?q=alpha&format=json"
        )[0] == 200
        assert not any(name.startswith("_retrieval_") for name in ingress.__dict__)
    finally:
        release.set()
        public_thread.join(3)
        if getattr(server, "public_ingress_server", None) is not None:
            _stop_public_ingress(server)
        server.shutdown()
        server.server_close()
        local_thread.join(3)
        _shutdown_retrieval_runtime(server)


def test_simultaneous_local_public_retrieval_initialization_creates_one_executor(
    tmp_path, monkeypatch
):
    import okf_loom.server as server_module

    root = tmp_path / "bundle"
    root.mkdir()
    (root / "guide.md").write_text("---\ntype: guide\n---\n# Guide\nalpha\n")
    server = _app_server(root)
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    ingress = _start_public_ingress(server, allow_public_edit=False)
    real_executor = server_module.ThreadPoolExecutor
    created = []

    def counting_executor(*args, **kwargs):
        value = real_executor(*args, **kwargs)
        created.append(value)
        return value

    monkeypatch.setattr(server_module, "ThreadPoolExecutor", counting_executor)
    barrier = threading.Barrier(3)
    results = []

    def request(public):
        barrier.wait()
        call = _request if public else _local_request
        results.append(call(server, "GET", "/__search?q=alpha&format=json")[0])

    workers = [threading.Thread(target=request, args=(public,)) for public in (False, True)]
    try:
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(3)
            assert not worker.is_alive()
        assert results == [200, 200] or sorted(results) == [200, 200]
        assert len(created) == 1
        assert server._retrieval_executor is created[0]
        assert not any(name.startswith("_retrieval_") for name in ingress.__dict__)
    finally:
        _stop_public_ingress(server)
        server.shutdown()
        server.server_close()
        local_thread.join(3)
        _shutdown_retrieval_runtime(server)


def _bootstrap(payload):
    match = re.search(
        br'<script type="application/json" id="okf-studio-bootstrap">(.*?)</script>',
        payload,
    )
    assert match is not None
    return json.loads(match.group(1))


# ---------------------------------------------------------------------------
# parse_quick_tunnel_url — pure parser
# ---------------------------------------------------------------------------

def test_parse_extracts_url_from_cloudflared_banner_line() -> None:
    line = (
        "2026-07-02T10:00:00Z INF |  https://hired-henderson-visual-endorsed"
        ".trycloudflare.com                                        |"
    )
    assert parse_quick_tunnel_url(line) == (
        "https://hired-henderson-visual-endorsed.trycloudflare.com"
    )


def test_parse_returns_none_for_noise_lines() -> None:
    for line in (
        "2026-07-02T10:00:00Z INF Requesting new quick Tunnel on trycloudflare.com...",
        "https://developers.cloudflare.com/cloudflare-one/",  # docs link, not a tunnel
        "",
    ):
        assert parse_quick_tunnel_url(line) is None


def test_parse_handles_none_input() -> None:
    assert parse_quick_tunnel_url(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# start_quick_tunnel — fail-closed without cloudflared
# ---------------------------------------------------------------------------

def test_start_quick_tunnel_missing_binary_raises_actionable_error(monkeypatch) -> None:
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError) as exc:
        start_quick_tunnel(8787)
    assert "cloudflared" in str(exc.value)


def test_start_quick_tunnel_popen_oserror_is_actionable(monkeypatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/cloudflared")
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn denied")),
    )
    with pytest.raises(RuntimeError, match="could not start.*spawn denied"):
        start_quick_tunnel(8787)


def test_process_settlement_is_bounded_terminate_wait_kill_wait(monkeypatch) -> None:
    calls = []

    class FakePopen:
        def terminate(self):
            calls.append("terminate")

        def wait(self, *, timeout):
            calls.append(("wait", timeout))
            if len([call for call in calls if isinstance(call, tuple)]) == 1:
                raise subprocess.TimeoutExpired("cloudflared", timeout)
            return -9

        def kill(self):
            calls.append("kill")

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    _settle_tunnel_process(FakePopen(), timeout_s=0.01)
    assert calls == [
        "terminate", ("wait", 0.01), "kill", ("wait", 0.01),
    ]


def test_no_url_timeout_reports_unsettled_real_popen_owner(monkeypatch) -> None:
    import shutil

    class FakePopen:
        def __init__(self, *_args, **_kwargs):
            self.stdout = ()

        def terminate(self):
            pass

        def wait(self, *, timeout):
            raise subprocess.TimeoutExpired("cloudflared", timeout)

        def kill(self):
            raise OSError("kill failed")

    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/cloudflared")
    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    with pytest.raises(TunnelStartOwnershipError) as exc:
        start_quick_tunnel(8787, timeout_s=0.01)

    assert isinstance(exc.value.process, FakePopen)


def test_dynamic_no_url_failure_retains_owner_blocks_overlap_and_retries(
    tiny_good_bundle, monkeypatch,
) -> None:
    start_calls = 0

    class FakePopen:
        def __init__(self):
            self.fail = True

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, *, timeout):
            if self.fail:
                raise subprocess.TimeoutExpired("cloudflared", timeout)
            return 0

        def kill(self):
            if self.fail:
                raise OSError("kill failed")

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    proc = FakePopen()

    def no_url(_port):
        nonlocal start_calls
        start_calls += 1
        raise TunnelStartOwnershipError("no tunnel URL", proc)

    monkeypatch.setattr("okf_loom.server.start_quick_tunnel", no_url)
    server = _app_server(tiny_good_bundle)
    server.tunnel_url = None
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    try:
        status, payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "start"},
        )
        assert status == 502
        assert json.loads(payload)["retryable"] is True
        assert server.tunnel_proc is proc
        assert server.tunnel_url is None
        assert server.public_ingress_generation is None
        assert server.public_mutation_token is None
        assert server.public_ingress_draining_generation is not None

        status, payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "status"},
        )
        assert status == 200
        assert json.loads(payload)["draining"] is True
        status, payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "start"},
        )
        assert status == 409
        assert json.loads(payload)["retryable"] is True
        assert start_calls == 1

        proc.fail = False
        status, _ = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "stop"},
        )
        assert status == 200
        assert server.tunnel_proc is None
        assert server.public_ingress_draining_generation is None
    finally:
        proc.fail = False
        _shutdown_tunnel_owner(server, server.studio)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def test_unproven_real_process_settlement_blocks_restart_and_admin_retry_succeeds(
    tiny_good_bundle, monkeypatch,
) -> None:
    calls = []
    start_entered = threading.Event()

    class FakePopen:
        def __init__(self):
            self.fail = True
            self.wait_count = 0

        def poll(self):
            return None

        def terminate(self):
            calls.append("terminate")

        def wait(self, *, timeout):
            calls.append(("wait", timeout))
            if not self.fail:
                return 0
            self.wait_count += 1
            if self.wait_count % 2:
                raise subprocess.TimeoutExpired("cloudflared", timeout)
            raise OSError("reap failed")

        def kill(self):
            calls.append("kill")
            if self.fail:
                raise OSError("kill failed")

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        "okf_loom.server.start_quick_tunnel",
        lambda _port: (
            start_entered.set(),
            (FakePopen(), "https://must-not-overlap.trycloudflare.com"),
        )[1],
    )
    server = _app_server(tiny_good_bundle)
    _start_public_ingress(server, allow_public_edit=False)
    proc = FakePopen()
    server.tunnel_proc = proc
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    try:
        status, payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "stop"},
        )
        assert status == 503
        assert json.loads(payload)["retryable"] is True
        assert server.tunnel_url is None
        assert server.public_ingress_generation is None
        assert server.public_mutation_token is None
        assert server.tunnel_proc is proc
        assert server.public_ingress_draining_generation is not None

        status, payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "status"},
        )
        status_payload = json.loads(payload)
        assert status == 200
        assert status_payload["url"] is None
        assert status_payload["public_edit"] is False
        assert status_payload["draining"] is True
        assert "settlement" in status_payload["error"]

        status, payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "start"},
        )
        assert status == 409
        assert json.loads(payload)["retryable"] is True
        assert not start_entered.is_set()

        proc.fail = False
        status, _ = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "stop"},
        )
        assert status == 200
        assert server.tunnel_proc is None
        assert server.public_ingress_draining_generation is None
        assert calls.count("terminate") == 2
    finally:
        proc.fail = False
        _shutdown_tunnel_owner(server, server.studio)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def test_public_read_only_missing_document_keeps_shell_and_authority(tiny_good_bundle):
    server = _app_server(tiny_good_bundle)
    _start_public_ingress(server, allow_public_edit=False)
    try:
        status, html = _request(server, "GET", "/no-such-page", headers={
            "Accept": "text/html", "Host": "public-test.trycloudflare.com",
        }, token=server.csrf_token)
        assert status == 404
        assert b"Page not found" in html and b"data-okf-application-shell" in html
        assert b'href="/"' in html and b'href="/__search"' in html
        bootstrap = _bootstrap(html)
        assert bootstrap["authority_mode"] == "public-read"
        assert bootstrap["document"] is None
        assert bootstrap["edit"] is False and bootstrap["comment"] is False
        assert bootstrap["presence"] is False and bootstrap["token"] == ""
        assert server.csrf_token.encode() not in html
        assert server.studio.list_comments() == []
        status, body = _request(server, "GET", "/__diff", headers={"Accept": "text/html"})
        assert status == 403 and b"data-okf-application-shell" not in body
    finally:
        _stop_public_ingress(server)
        server.server_close()
        server.studio.close()


def test_read_only_public_ingress_denies_host_spoof_with_copied_admin_token(
    tiny_good_bundle,
) -> None:
    server = _app_server(tiny_good_bundle)
    _start_public_ingress(server, allow_public_edit=False)
    try:
        before = server.studio.list_comments()
        status, _ = _request(
            server,
            "POST",
            "/__comment",
            token=server.csrf_token,
            headers={
                "Origin": "http://localhost",
                "CF-Connecting-IP": "127.0.0.1",
                "X-Forwarded-Host": "localhost",
            },
            body={"concept": "tables/users", "body": "must not persist"},
        )
        assert status == 403
        assert server.studio.list_comments() == before
    finally:
        _stop_public_ingress(server)


def test_public_forbidden_incomplete_tunnel_post_is_immediate(tmp_path) -> None:
    server = _app_server(tmp_path)
    _start_public_ingress(server, allow_public_edit=False)
    ingress = server.public_ingress_server
    client = socket.create_connection(ingress.server_address, timeout=0.3)
    try:
        client.sendall(
            b"POST /__tunnel HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Type: application/json\r\nContent-Length: 100\r\n"
            b"Connection: close\r\n\r\n{}"
        )
        response = client.makefile("rb").read()
        assert b"HTTP/1.1 403" in response
        assert b"Public ingress cannot access this mutation route." in response
        assert b"request_body_timeout" not in response
        assert server.tunnel_url == "https://public-test.trycloudflare.com"
    finally:
        client.close()
        _stop_public_ingress(server)
        server.server_close()
        server.studio.close()


@pytest.mark.parametrize("allow_public_edit", [False, True])
def test_public_ingress_denies_presence_get_without_issuing_capability(
    tiny_good_bundle, allow_public_edit,
) -> None:
    server = _app_server(tiny_good_bundle)
    ingress = _start_public_ingress(
        server, allow_public_edit=allow_public_edit
    )
    presence_path = server.studio.presence_path
    before = presence_path.read_bytes() if presence_path.exists() else None
    try:
        for path in (
            "/__presence",
            "/__presence?poll=1",
            "/__presence/",
            "/%5f%5fpresence",
        ):
            status, payload = _request(
                server,
                "GET",
                path,
                token=server.csrf_token,
                headers={
                    "Host": "localhost",
                    "Origin": "http://localhost",
                    "CF-Connecting-IP": "127.0.0.1",
                    "X-Forwarded-For": "127.0.0.1",
                    "X-Forwarded-Host": "localhost",
                },
            )
            assert status == 403, path
            assert b"browser_session" not in payload
        assert not hasattr(ingress, "_presence_issuance")
        after = presence_path.read_bytes() if presence_path.exists() else None
        assert after == before
    finally:
        _stop_public_ingress(server)
        server.server_close()


def test_local_presence_get_stays_functional_with_public_ingress_active(
    tiny_good_bundle,
) -> None:
    server = _app_server(tiny_good_bundle)
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    _start_public_ingress(server, allow_public_edit=False)
    try:
        status, payload = _local_request(server, "GET", "/__presence")
        assert status == 200
        response = json.loads(payload)
        browser_session = response["browser_session"]
        stored = json.loads(server.studio.presence_path.read_text())
        assert browser_session["session_id"] in stored["issued"]
        assert browser_session["capability"]
    finally:
        _stop_public_ingress(server)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


@pytest.mark.parametrize("allow_public_edit", [False, True])
def test_public_get_allowlist_exposes_only_documented_read_surfaces(
    tiny_good_bundle, allow_public_edit,
) -> None:
    server = _app_server(tiny_good_bundle)
    _start_public_ingress(server, allow_public_edit=allow_public_edit)
    try:
        for path in ("/", "/tables/users", "/__graph", "/__data/graph.json"):
            status, _ = _request(server, "GET", path)
            assert status == 200, path

        for path in (
            "/__proposal/v1/proposals",
            "/__diff?concept=tables/users&from=a&to=b",
            "/__tunnel",
            "/__preview",
            "/__admin",
        ):
            status, _ = _request(
                server,
                "GET",
                path,
                token=server.csrf_token,
                headers={
                    "Host": "localhost",
                    "CF-Connecting-IP": "127.0.0.1",
                    "X-Forwarded-Host": "localhost",
                },
            )
            assert status == 403, path
    finally:
        _stop_public_ingress(server)
        server.server_close()


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_read_only_public_ingress_denies_every_unsafe_method(
    tiny_good_bundle, method,
) -> None:
    server = _app_server(tiny_good_bundle)
    _start_public_ingress(server, allow_public_edit=False)
    try:
        status, _ = _request(
            server,
            method,
            "/__comment",
            token=server.csrf_token,
            headers={"CF-Connecting-IP": "127.0.0.1", "X-Forwarded-Host": "localhost"},
            body={"concept": "tables/users", "body": "denied"},
        )
        assert status == 403
        assert server.studio.list_comments() == []
    finally:
        _stop_public_ingress(server)
        server.server_close()


def test_public_edit_bootstrap_uses_scoped_token_and_only_intended_routes(
    tiny_good_bundle,
) -> None:
    server = _app_server(tiny_good_bundle)
    _start_public_ingress(server, allow_public_edit=True)
    try:
        status, html = _request(
            server, "GET", "/", headers={"Host": "public-test.trycloudflare.com"}
        )
        assert status == 200
        bootstrap = _bootstrap(html)
        public_token = bootstrap["token"]
        assert bootstrap["edit"] is True
        assert public_token == server.public_mutation_token
        assert public_token != server.csrf_token
        assert server.csrf_token.encode() not in html

        status, _ = _request(
            server,
            "POST",
            "/__comment",
            token=public_token,
            headers={"Host": "public-test.trycloudflare.com"},
            body={"concept": "tables/users", "body": "public comment"},
        )
        assert status == 201

        for method, path, body in (
            ("POST", "/__tunnel", {"action": "status"}),
            ("POST", "/__preview", {"markdown": "secret"}),
            ("POST", "/__presence", {"state": "watching"}),
            ("GET", "/__diff?concept=tables/users&from=a&to=b", None),
            ("GET", "/__proposal/v1/proposals", None),
            ("POST", "/__proposal/v1/proposals", {}),
        ):
            status, _ = _request(
                server,
                method,
                path,
                token=public_token,
                headers={"Host": "public-test.trycloudflare.com"},
                body=body,
            )
            assert status == 403, (method, path)
    finally:
        _stop_public_ingress(server)
        server.server_close()


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/__comment-update", {}),
        ("/__comment-reanchor", {}),
        ("/__apply", {}),
        ("/__undo", {}),
    ],
)
def test_public_mutation_token_reaches_each_scoped_route(
    tiny_good_bundle, path, body,
) -> None:
    server = _app_server(tiny_good_bundle)
    _start_public_ingress(server, allow_public_edit=True)
    try:
        status, _ = _request(
            server, "POST", path, token=server.public_mutation_token,
            headers={"Host": "public-test.trycloudflare.com"}, body=body,
        )
        # Deliberately malformed route data reaches each route's own validator;
        # the scoped authorization layer must not reject it as forbidden.
        assert status in {400, 404, 409}, path
    finally:
        _stop_public_ingress(server)
        server.server_close()


def test_local_edit_and_admin_remain_authorized_while_public_tunnel_is_active(
    tiny_good_bundle,
) -> None:
    server = _app_server(tiny_good_bundle)
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    _start_public_ingress(server, allow_public_edit=True)
    try:
        status, _ = _local_request(
            server,
            "POST",
            "/__comment",
            token=server.csrf_token,
            body={"concept": "tables/users", "body": "local comment"},
        )
        assert status == 201
        status, payload = _local_request(
            server,
            "POST",
            "/__tunnel",
            token=server.csrf_token,
            body={"action": "status"},
        )
        assert status == 200
        assert json.loads(payload)["public_edit"] is True
        status, _ = _local_request(
            server,
            "POST",
            "/__tunnel",
            token=server.public_mutation_token,
            body={"action": "status"},
        )
        assert status == 403
    finally:
        _stop_public_ingress(server)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def test_reanchor_quota_is_shared_by_local_and_public_ingress(
    tiny_good_bundle,
) -> None:
    server = _app_server(tiny_good_bundle)
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    ingress = _start_public_ingress(server, allow_public_edit=True)
    concept = next(
        value for value in server.bundle.concepts.values()
        if "The `users` table" in value.raw_text
    )
    source = concept.raw_text
    start = source.index("users")
    comment = server.studio.post_comment(
        concept="tables/users", body="quota proof",
        anchor=capture_anchor(source, start, start + len("users")),
    )
    body = {
        "comment_id": comment["id"],
        "expected_row_token": comment["row_token"],
        "expected_concept_content_rev": concept_content_rev(source),
        "anchor": comment["anchor"],
    }
    server._reanchor_rate_attempts = [time.monotonic()] * 119
    barrier = threading.Barrier(3)
    statuses: list[int] = []

    def local() -> None:
        barrier.wait()
        statuses.append(_local_request(
            server, "POST", "/__comment-reanchor",
            token=server.csrf_token, body=body, key="shared-quota-local",
        )[0])

    def public() -> None:
        barrier.wait()
        statuses.append(_request(
            server, "POST", "/__comment-reanchor",
            token=server.public_mutation_token,
            headers={"Host": "public-test.trycloudflare.com",
                     "Idempotency-Key": "shared-quota-public"}, body=body,
        )[0])

    workers = [threading.Thread(target=local), threading.Thread(target=public)]
    try:
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        assert sorted(statuses) == [200, 429]
        assert len(server._reanchor_rate_attempts) == 120
        assert "_reanchor_rate_attempts" not in ingress.__dict__
        assert "_reanchor_rate_lock" not in ingress.__dict__
    finally:
        _stop_public_ingress(server)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def test_incomplete_public_reanchor_does_not_consume_shared_quota(
    tiny_good_bundle,
) -> None:
    server = _app_server(tiny_good_bundle)
    ingress = _start_public_ingress(server, allow_public_edit=True)
    server.request_body_deadline_s = 0.05
    server._reanchor_rate_attempts = [time.monotonic()] * 119
    before = list(server._reanchor_rate_attempts)
    sock = socket.create_connection(ingress.server_address, timeout=2)
    try:
        sock.sendall(
            b"POST /__comment-reanchor HTTP/1.1\r\n"
            b"Host: public-test.trycloudflare.com\r\n"
            + f"X-OKF-Token: {server.public_mutation_token}\r\n".encode()
            + b"Content-Type: application/json\r\nContent-Length: 100\r\n"
              b"Connection: close\r\n\r\n{}"
        )
        response = sock.makefile("rb").read()
        assert b"HTTP/1.1 408" in response
        assert server._reanchor_rate_attempts == before
        assert "_reanchor_rate_attempts" not in ingress.__dict__
    finally:
        sock.close()
        _stop_public_ingress(server)
        server.server_close()


def test_public_ingress_capabilities_fail_closed_when_wrong_stale_or_restarted(
    tiny_good_bundle,
) -> None:
    server = _app_server(tiny_good_bundle)
    ingress = _start_public_ingress(server, allow_public_edit=True)
    old_token = server.public_mutation_token
    old_generation = server.public_ingress_generation
    try:
        for token in ("wrong", server.csrf_token):
            status, _ = _request(
                server, "POST", "/__comment", token=token,
                headers={"Host": "public-test.trycloudflare.com"},
                body={"concept": "tables/users", "body": "denied"},
            )
            assert status == 403

        server.public_ingress_generation = secrets.token_urlsafe(32)
        status, _ = _request(
            server, "GET", "/", headers={"Host": "public-test.trycloudflare.com"}
        )
        assert status == 403
    finally:
        server.public_ingress_generation = old_generation
        _stop_public_ingress(server)
    assert ingress.socket.fileno() == -1

    _start_public_ingress(server, allow_public_edit=True)
    try:
        assert server.public_ingress_generation != old_generation
        assert server.public_mutation_token != old_token
        status, _ = _request(
            server, "POST", "/__comment", token=old_token,
            headers={"Host": "public-test.trycloudflare.com"},
            body={"concept": "tables/users", "body": "stale denied"},
        )
        assert status == 403
    finally:
        _stop_public_ingress(server)
        server.server_close()


def test_tunnel_attach_targets_dedicated_listener_and_rotates_on_restart(
    tiny_good_bundle, monkeypatch,
) -> None:
    class Proc:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

    targets = []
    processes = []

    def fake_start(port):
        targets.append(port)
        process = Proc()
        processes.append(process)
        return process, "https://public-test.trycloudflare.com"

    monkeypatch.setattr("okf_loom.server.start_quick_tunnel", fake_start)
    server = _app_server(tiny_good_bundle)
    server.tunnel_url = None
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    try:
        status, start_payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "start", "allow_public_edit": True},
        )
        assert status == 200
        first_token = server.public_mutation_token
        first_generation = server.public_ingress_generation
        assert server.csrf_token.encode() not in start_payload
        assert first_token.encode() not in start_payload
        state_bytes = server.studio.server_state_path.read_bytes()
        assert server.csrf_token.encode() not in state_bytes
        assert first_token.encode() not in state_bytes
        events_bytes = json.dumps(server.studio.read_events()).encode()
        assert server.csrf_token.encode() not in events_bytes
        assert first_token.encode() not in events_bytes
        assert targets == [server.public_ingress_server.server_address[1]]
        assert targets[0] != server.server_address[1]

        status, _ = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "stop"},
        )
        assert status == 200
        assert processes[0].terminated is True
        assert server.public_mutation_token is None
        assert server.public_ingress_generation is None

        status, _ = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "start", "allow_public_edit": True},
        )
        assert status == 200
        assert len(targets) == 2
        assert server.public_mutation_token != first_token
        assert server.public_ingress_generation != first_generation
    finally:
        if server.tunnel_proc is not None:
            server.tunnel_proc.terminate()
        _stop_public_ingress(server)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def test_unexpected_cloudflared_death_revokes_stale_state_and_restarts_fresh(
    tiny_good_bundle, monkeypatch,
) -> None:
    class ExitedProc:
        returncode = 1

        def poll(self):
            return self.returncode

    class RunningProc:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    targets = []
    restarted_proc = RunningProc()

    def fake_start(port):
        targets.append(port)
        return restarted_proc, "https://restarted-test.trycloudflare.com"

    monkeypatch.setattr("okf_loom.server.start_quick_tunnel", fake_start)

    server = _app_server(tiny_good_bundle)
    old_ingress = _start_public_ingress(server, allow_public_edit=True)
    old_token = server.public_mutation_token
    old_generation = server.public_ingress_generation
    server.tunnel_proc = ExitedProc()
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    try:
        status, payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "status"},
        )
        assert status == 200
        assert json.loads(payload) == {
            "ok": True, "url": None, "public_edit": False,
        }
        assert server.tunnel_proc is None
        assert server.tunnel_url is None
        assert server.public_mutation_token is None
        assert server.public_ingress_generation is None
        assert server.public_ingress_server is None
        assert old_ingress.socket.fileno() == -1
        assert server.allowed_hosts == server.base_allowed_hosts
        state = json.loads(server.studio.server_state_path.read_text())
        assert state["tunnel_url"] is None
        assert state["tunnel_public_edit"] is False

        detach_events = [
            event for event in server.studio.read_events()
            if event.get("action") == "tunnel_detach"
        ]
        assert len(detach_events) == 1

        status, payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "status"},
        )
        assert status == 200
        assert json.loads(payload)["url"] is None
        assert len([
            event for event in server.studio.read_events()
            if event.get("action") == "tunnel_detach"
        ]) == 1

        status, payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "start", "allow_public_edit": True},
        )
        restart_payload = json.loads(payload)
        assert status == 200
        assert restart_payload["url"] == "https://restarted-test.trycloudflare.com"
        assert "already_running" not in restart_payload
        assert server.public_ingress_server is not old_ingress
        assert targets == [server.public_ingress_server.server_address[1]]
        assert server.public_ingress_generation != old_generation
        assert server.public_mutation_token != old_token

        status, _ = _request(
            server,
            "POST", "/__comment", token=old_token,
            headers={"Host": "restarted-test.trycloudflare.com"},
            body={"concept": "tables/users", "body": "stale denied"},
        )
        assert status == 403

        status, _ = _local_request(server, "GET", "/")
        assert status == 200
    finally:
        if server.tunnel_proc is not None:
            server.tunnel_proc.terminate()
        _stop_public_ingress(server)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def test_tunnel_activity_is_recorded_as_cli_never_as_agent(
    tiny_good_bundle, monkeypatch,
) -> None:
    """Attach, detach and loss are driven by ``okf-loom tunnel`` or by the
    server itself, which cannot tell a person from an agent. Every tunnel row
    in the change list is therefore recorded with actor ``cli``."""

    class Proc:
        def __init__(self):
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

    processes = []

    def fake_start(port):
        process = Proc()
        processes.append(process)
        return process, f"https://share-{len(processes)}.trycloudflare.com"

    monkeypatch.setattr("okf_loom.server.start_quick_tunnel", fake_start)
    server = _app_server(tiny_good_bundle)
    server.tunnel_url = None
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    try:
        for action in ("start", "stop", "start"):
            status, _ = _local_request(
                server, "POST", "/__tunnel", token=server.csrf_token,
                body={"action": action},
            )
            assert status == 200, action
        # cloudflared exits on its own; the next status check revokes it.
        processes[-1].returncode = 1
        status, payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "status"},
        )
        assert status == 200
        assert json.loads(payload)["url"] is None

        tunnel_events = [
            event for event in server.studio.read_events()
            if event.get("origin") == "tunnel"
        ]
        assert [event["action"] for event in tunnel_events] == [
            "tunnel_attach", "tunnel_detach", "tunnel_attach", "tunnel_detach",
        ]
        assert tunnel_events[1]["summary"] == (
            "public tunnel detached: https://share-1.trycloudflare.com"
        )
        assert tunnel_events[3]["summary"] == (
            "public tunnel lost after cloudflared exited: "
            "https://share-2.trycloudflare.com"
        )
        assert [event["actor"] for event in tunnel_events] == ["cli"] * 4
    finally:
        if server.tunnel_proc is not None:
            server.tunnel_proc.terminate()
        _stop_public_ingress(server)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def test_tunnel_status_fails_closed_when_process_poll_raises_oserror(
    tiny_good_bundle,
) -> None:
    class PollErrorProc:
        def poll(self):
            raise OSError("process handle unavailable")

    server = _app_server(tiny_good_bundle)
    ingress = _start_public_ingress(server, allow_public_edit=True)
    server.tunnel_proc = PollErrorProc()
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    try:
        status, payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "status"},
        )
        assert status == 200
        assert json.loads(payload)["url"] is None
        assert server.public_mutation_token is None
        assert server.public_ingress_generation is None
        assert ingress.socket.fileno() == -1
    finally:
        _stop_public_ingress(server)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def test_tunnel_status_does_not_guess_process_without_poll_is_dead(
    tiny_good_bundle,
) -> None:
    server = _app_server(tiny_good_bundle)
    _start_public_ingress(server, allow_public_edit=True)
    server.tunnel_proc = object()
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    try:
        status, payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "status"},
        )
        assert status == 200
        assert json.loads(payload) == {
            "ok": True,
            "url": "https://public-test.trycloudflare.com",
            "public_edit": True,
        }
        assert server.tunnel_proc is not None
        assert server.public_mutation_token is not None
    finally:
        _stop_public_ingress(server)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def _wait_until(predicate, *, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("condition was not reached before timeout")


def test_tunnel_monitor_autonomously_revokes_exited_process_without_admin_probe(
    tiny_good_bundle,
) -> None:
    class Proc:
        def __init__(self):
            self.exited = False
            self.terminated = 0

        def poll(self):
            return 1 if self.exited else None

        def terminate(self):
            self.terminated += 1

    server = _app_server(tiny_good_bundle)
    ingress = _start_public_ingress(server, allow_public_edit=True)
    old_token = server.public_mutation_token
    proc = Proc()
    server.tunnel_proc = proc
    _start_tunnel_monitor(server, server.studio, interval_s=0.01)
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    try:
        proc.exited = True
        _wait_until(lambda: server.public_ingress_generation is None)
        _wait_until(lambda: proc.terminated == 1)
        assert server.tunnel_proc is None
        assert server.tunnel_url is None
        assert server.public_mutation_token is None
        assert ingress.socket.fileno() == -1
        assert proc.terminated == 1
        state = json.loads(server.studio.server_state_path.read_text())
        assert state["tunnel_url"] is None
        _wait_until(lambda: len([
            event for event in server.studio.read_events()
            if event.get("action") == "tunnel_detach"
        ]) == 1)
        assert len([
            event for event in server.studio.read_events()
            if event.get("action") == "tunnel_detach"
        ]) == 1
        status, _ = _local_request(server, "GET", "/")
        assert status == 200
        assert old_token is not None
    finally:
        _stop_tunnel_monitor(server)
        _stop_public_ingress(server)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def test_tunnel_monitor_poll_exception_revokes_and_attempts_settlement(
    tiny_good_bundle,
) -> None:
    class Proc:
        def __init__(self):
            self.terminated = 0

        def poll(self):
            raise ValueError("broken process adapter")

        def terminate(self):
            self.terminated += 1

    server = _app_server(tiny_good_bundle)
    _start_public_ingress(server, allow_public_edit=True)
    proc = Proc()
    server.tunnel_proc = proc
    _start_tunnel_monitor(server, server.studio, interval_s=0.01)
    try:
        _wait_until(lambda: proc.terminated == 1)
        assert proc.terminated == 1
        assert server.public_ingress_generation is None
        assert server.public_mutation_token is None
        assert server.tunnel_url is None
    finally:
        _stop_tunnel_monitor(server)
        _stop_public_ingress(server)
        server.server_close()


def test_stop_drains_admitted_public_mutation_before_finalizing_or_restarting(
    tiny_good_bundle, monkeypatch,
) -> None:
    admitted = threading.Event()
    release_mutation = threading.Event()
    mutation_done = threading.Event()
    stop_done = threading.Event()
    restart_done = threading.Event()
    order = []

    def blocked_comment(handler, _data):
        if not handler._is_public_tunnel_request():
            return handler._send_json(201, {"ok": True, "local": True})
        admitted.set()
        assert release_mutation.wait(3)
        order.append("mutation")
        mutation_done.set()
        return handler._send_json(201, {"ok": True})

    class Proc:
        def poll(self):
            return None

        def terminate(self):
            pass

    def fake_start(_port):
        order.append("restart")
        return Proc(), "https://drain-restart.trycloudflare.com"

    monkeypatch.setattr(OKFWikiHandler, "_handle_comment", blocked_comment)
    monkeypatch.setattr("okf_loom.server.start_quick_tunnel", fake_start)
    server = _app_server(tiny_good_bundle)
    ingress = _start_public_ingress(server, allow_public_edit=True)
    original_generation = server.public_ingress_generation
    token = server.public_mutation_token
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()

    mutation_result = []
    mutation_thread = threading.Thread(
        target=lambda: mutation_result.append(_request(
            server, "POST", "/__comment", token=token,
            headers={"Host": "public-test.trycloudflare.com"},
            body={"concept": "tables/users", "body": "admitted"},
        )),
    )
    # Keep one already-accepted public connection available to prove that
    # admission, not merely listener closure, rejects requests during drain.
    pending = http.client.HTTPConnection(
        "127.0.0.1", ingress.server_address[1], timeout=3,
    )
    pending.request("GET", "/", headers={"Host": "public-test.trycloudflare.com"})
    response = pending.getresponse()
    assert response.status == 200
    response.read()

    stop_result = []
    restart_result = []
    try:
        mutation_thread.start()
        assert admitted.wait(3)

        def stop_tunnel():
            stop_result.append(_local_request(
                server, "POST", "/__tunnel", token=server.csrf_token,
                body={"action": "stop"},
            ))
            order.append("stop")
            stop_done.set()

        stop_thread = threading.Thread(target=stop_tunnel)
        stop_thread.start()
        _wait_until(
            lambda: getattr(server, "public_ingress_accepting", True) is False
        )
        assert server.public_ingress_generation == original_generation
        assert server.tunnel_url is None
        assert not stop_done.wait(0.1)

        status, payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "status"},
        )
        status_payload = json.loads(payload)
        assert status == 200
        assert status_payload["url"] is None
        assert status_payload["public_edit"] is False
        assert status_payload["draining"] is True

        encoded = json.dumps({
            "concept": "tables/users", "body": "must be denied",
        }).encode()
        try:
            pending.request(
                "POST", "/__comment", body=encoded,
                headers={
                    "Host": "public-test.trycloudflare.com",
                    "X-OKF-Token": token,
                    "Content-Type": "application/json",
                    "Content-Length": str(len(encoded)),
                },
            )
            denied = pending.getresponse()
            assert denied.status in {403, 503}
            denied.read()
        except (OSError, http.client.HTTPException):
            # The HTTP admission fence may close an already-accepted idle
            # socket before this raced request is writable. Both outcomes are
            # terminal and, critically, neither reaches mutation dispatch.
            pass

        status, _ = _local_request(
            server, "POST", "/__comment", token=server.csrf_token,
            body={"concept": "tables/users", "body": "local during drain"},
        )
        assert status == 201
        status, _ = _local_request(server, "GET", "/")
        assert status == 200

        def restart_tunnel():
            restart_result.append(_local_request(
                server, "POST", "/__tunnel", token=server.csrf_token,
                body={"action": "start", "allow_public_edit": True},
            ))
            restart_done.set()

        restart_thread = threading.Thread(target=restart_tunnel)
        restart_thread.start()
        assert restart_done.wait(3)
        restart_thread.join(timeout=3)
        assert restart_result[0][0] == 409
        assert json.loads(restart_result[0][1])["retryable"] is True
        assert "restart" not in order

        release_mutation.set()
        assert mutation_done.wait(3)
        assert stop_done.wait(3)
        mutation_thread.join(timeout=3)
        stop_thread.join(timeout=3)

        assert mutation_result[0][0] == 201
        assert stop_result[0][0] == 200
        assert order.index("mutation") < order.index("stop")
        status, _ = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "start", "allow_public_edit": True},
        )
        assert status == 200
        assert order.index("mutation") < order.index("restart")
        assert server.public_ingress_generation != original_generation
    finally:
        release_mutation.set()
        pending.close()
        _shutdown_tunnel_owner(server, server.studio)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def test_duplicate_stop_during_public_mutation_drain_is_retryable_and_single_owner(
    tiny_good_bundle, monkeypatch,
) -> None:
    admitted = threading.Event()
    release_mutation = threading.Event()
    mutation_done = threading.Event()
    first_stop_done = threading.Event()
    second_stop_done = threading.Event()

    def blocked_comment(handler, _data):
        admitted.set()
        assert release_mutation.wait(3)
        mutation_done.set()
        return handler._send_json(201, {"ok": True})

    class Proc:
        def __init__(self):
            self.settlements = 0

        def poll(self):
            return None

        def terminate(self):
            self.settlements += 1

    monkeypatch.setattr(OKFWikiHandler, "_handle_comment", blocked_comment)
    server = _app_server(tiny_good_bundle)
    ingress = _start_public_ingress(server, allow_public_edit=True)
    generation = server.public_ingress_generation
    token = server.public_mutation_token
    proc = Proc()
    server.tunnel_proc = proc
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    mutation_result = []
    first_stop_result = []
    second_stop_result = []

    mutation_thread = threading.Thread(target=lambda: mutation_result.append(_request(
        server, "POST", "/__comment", token=token,
        headers={"Host": "public-test.trycloudflare.com"},
        body={"concept": "tables/users", "body": "admitted"},
    )))

    def stop_tunnel(result, done):
        result.append(_local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "stop"},
        ))
        done.set()

    first_stop_thread = threading.Thread(
        target=stop_tunnel, args=(first_stop_result, first_stop_done),
    )
    second_stop_thread = threading.Thread(
        target=stop_tunnel, args=(second_stop_result, second_stop_done),
    )
    try:
        mutation_thread.start()
        assert admitted.wait(3)
        first_stop_thread.start()
        _wait_until(
            lambda: getattr(server, "public_ingress_accepting", True) is False
        )
        assert server.public_ingress_draining_generation == generation

        second_stop_thread.start()
        assert second_stop_done.wait(3)
        second_stop_thread.join(timeout=3)
        assert second_stop_result[0][0] == 503
        assert json.loads(second_stop_result[0][1])["retryable"] is True
        assert not first_stop_done.is_set()
        assert server.public_ingress_generation == generation
        assert server.public_mutation_token == token
        assert server.public_ingress_server is ingress
        assert server.tunnel_proc is proc
        assert proc.settlements == 0
        assert not [
            event for event in server.studio.read_events()
            if event.get("action") == "tunnel_detach"
        ]

        release_mutation.set()
        assert mutation_done.wait(3)
        assert first_stop_done.wait(3)
        mutation_thread.join(timeout=3)
        first_stop_thread.join(timeout=3)

        assert mutation_result[0][0] == 201
        assert len(first_stop_result) == 1
        assert first_stop_result[0][0] == 200
        assert proc.settlements == 1
        assert server.public_ingress_generation is None
        assert server.public_mutation_token is None
        assert server.public_ingress_server is None
        assert server.tunnel_proc is None
        assert len([
            event for event in server.studio.read_events()
            if event.get("action") == "tunnel_detach"
        ]) == 1
    finally:
        release_mutation.set()
        _shutdown_tunnel_owner(server, server.studio)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def test_autonomous_death_drains_admitted_public_mutation_before_finalizing(
    tiny_good_bundle, monkeypatch,
) -> None:
    admitted = threading.Event()
    release_mutation = threading.Event()
    mutation_done = threading.Event()
    exited = threading.Event()
    settled = threading.Event()
    order = []

    def blocked_comment(handler, _data):
        admitted.set()
        assert release_mutation.wait(3)
        order.append("mutation")
        mutation_done.set()
        return handler._send_json(201, {"ok": True})

    class Proc:
        def poll(self):
            return 1 if exited.is_set() else None

        def terminate(self):
            order.append("death-finalized")
            settled.set()

    monkeypatch.setattr(OKFWikiHandler, "_handle_comment", blocked_comment)
    server = _app_server(tiny_good_bundle)
    _start_public_ingress(server, allow_public_edit=True)
    original_generation = server.public_ingress_generation
    token = server.public_mutation_token
    server.tunnel_proc = Proc()
    _start_tunnel_monitor(server, server.studio, interval_s=0.01)
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    mutation_result = []
    mutation_thread = threading.Thread(
        target=lambda: mutation_result.append(_request(
            server, "POST", "/__comment", token=token,
            headers={"Host": "public-test.trycloudflare.com"},
            body={"concept": "tables/users", "body": "admitted"},
        )),
    )
    try:
        mutation_thread.start()
        assert admitted.wait(3)
        exited.set()
        _wait_until(
            lambda: getattr(server, "public_ingress_accepting", True) is False
        )
        assert server.public_ingress_generation == original_generation
        assert not settled.wait(0.1)
        status, _ = _local_request(server, "GET", "/")
        assert status == 200

        release_mutation.set()
        assert mutation_done.wait(3)
        assert settled.wait(3)
        mutation_thread.join(timeout=3)
        assert mutation_result[0][0] == 201
        assert order == ["mutation", "death-finalized"]
        assert server.public_ingress_generation is None
        assert server.public_mutation_token is None
        assert server.tunnel_url is None
    finally:
        release_mutation.set()
        _stop_tunnel_monitor(server)
        _stop_public_ingress(server)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def test_public_mutation_lease_releases_when_handler_raises(
    tiny_good_bundle, monkeypatch,
) -> None:
    def fail_comment(_handler, _data):
        raise RuntimeError("mutation failed")

    monkeypatch.setattr(OKFWikiHandler, "_handle_comment", fail_comment)
    server = _app_server(tiny_good_bundle)
    _start_public_ingress(server, allow_public_edit=True)
    generation = server.public_ingress_generation
    token = server.public_mutation_token
    try:
        status, _ = _request(
            server, "POST", "/__comment", token=token,
            headers={"Host": "public-test.trycloudflare.com"},
            body={"concept": "tables/users", "body": "fails"},
        )
        assert status == 500
        assert server.public_mutation_inflight.get(generation, 0) == 0
        _clear_owned_tunnel(server, server.studio)
        assert server.public_ingress_generation is None
    finally:
        _stop_public_ingress(server)
        server.server_close()


def test_restart_waits_for_complete_old_process_settlement(
    tiny_good_bundle, monkeypatch,
) -> None:
    settlement_entered = threading.Event()
    release_settlement = threading.Event()
    new_start_entered = threading.Event()
    stop_done = threading.Event()
    start_done = threading.Event()

    class OldProc:
        def poll(self):
            return None

        def terminate(self):
            settlement_entered.set()
            assert release_settlement.wait(3)

    class NewProc:
        def poll(self):
            return None

        def terminate(self):
            pass

    def fake_start(_port):
        new_start_entered.set()
        return NewProc(), "https://settled-restart.trycloudflare.com"

    monkeypatch.setattr("okf_loom.server.start_quick_tunnel", fake_start)
    server = _app_server(tiny_good_bundle)
    _start_public_ingress(server, allow_public_edit=False)
    server.tunnel_proc = OldProc()
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    stop_result = []
    start_result = []

    def stop_tunnel():
        stop_result.append(_local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "stop"},
        ))
        stop_done.set()

    def start_tunnel():
        start_result.append(_local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "start"},
        ))
        start_done.set()

    stop_thread = threading.Thread(target=stop_tunnel)
    start_thread = threading.Thread(target=start_tunnel)
    try:
        stop_thread.start()
        assert settlement_entered.wait(3)
        start_thread.start()
        assert not new_start_entered.wait(0.1)
        assert not stop_done.is_set()
        assert start_done.wait(3)
        start_thread.join(timeout=3)
        assert start_result[0][0] == 409

        release_settlement.set()
        assert stop_done.wait(3)
        stop_thread.join(timeout=3)
        assert stop_result[0][0] == 200
        status, _ = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "start"},
        )
        assert status == 200
        assert new_start_entered.is_set()
    finally:
        release_settlement.set()
        _shutdown_tunnel_owner(server, server.studio)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def test_active_state_publication_cannot_overwrite_newer_inactive_cleanup(
    tiny_good_bundle, monkeypatch,
) -> None:
    import okf_loom.io_utils as io_utils
    import okf_loom.server as server_module

    active_writer_entered = threading.Event()
    release_active_writer = threading.Event()
    cleanup_done = threading.Event()
    call_count = 0
    call_lock = threading.Lock()

    def barrier_write(relative, text):
        nonlocal call_count
        with call_lock:
            call_count += 1
            ordinal = call_count
        if ordinal == 1:
            active_writer_entered.set()
            assert release_active_writer.wait(3)
        return real_session_write(relative, text)

    class ExitedProc:
        def poll(self):
            return 1

        def terminate(self):
            pass

    server = _app_server(tiny_good_bundle)
    real_session_write = server.studio.write_session_text
    ingress = _start_public_ingress(server, allow_public_edit=True)
    proc = ExitedProc()
    server.tunnel_proc = proc
    monkeypatch.setattr(server.studio, "write_session_text", barrier_write)
    monkeypatch.setattr(
        server_module, "_close_public_ingress_listener",
        lambda _ingress, _thread: None,
    )

    writer = threading.Thread(
        target=write_server_state, args=(server, server.studio),
    )
    cleanup = threading.Thread(
        target=lambda: (
            _clear_owned_tunnel(
                server, server.studio,
                expected_proc=proc,
                expected_generation=server.public_ingress_generation,
            ),
            cleanup_done.set(),
        ),
    )
    try:
        writer.start()
        assert active_writer_entered.wait(3)
        cleanup.start()
        assert not cleanup_done.wait(0.1)
        release_active_writer.set()
        writer.join(timeout=3)
        cleanup.join(timeout=3)
        assert cleanup_done.is_set()
        state = json.loads(server.studio.server_state_path.read_text())
        assert server.tunnel_url is None
        assert state["tunnel_url"] is None
        assert state["tunnel_public_edit"] is False
    finally:
        release_active_writer.set()
        _stop_public_ingress(server)
        ingress.shutdown()
        ingress.server_close()
        server.server_close()


def test_direct_start_reconciles_dead_process_and_rotates_authority(
    tiny_good_bundle, monkeypatch,
) -> None:
    class ExitedProc:
        def __init__(self):
            self.terminated = 0

        def poll(self):
            return 1

        def terminate(self):
            self.terminated += 1

    class NewProc:
        def poll(self):
            return None

        def terminate(self):
            pass

    targets = []

    def fake_start(port):
        targets.append(port)
        return NewProc(), "https://direct-restart.trycloudflare.com"

    monkeypatch.setattr("okf_loom.server.start_quick_tunnel", fake_start)
    server = _app_server(tiny_good_bundle)
    old_ingress = _start_public_ingress(server, allow_public_edit=True)
    old_generation = server.public_ingress_generation
    old_token = server.public_mutation_token
    old_proc = ExitedProc()
    server.tunnel_proc = old_proc
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    try:
        status, payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "start", "allow_public_edit": True},
        )
        assert status == 200
        assert "already_running" not in json.loads(payload)
        assert old_proc.terminated == 1
        assert server.public_ingress_server is not old_ingress
        assert targets == [server.public_ingress_server.server_address[1]]
        assert server.public_ingress_generation != old_generation
        assert server.public_mutation_token != old_token
    finally:
        _shutdown_tunnel_owner(server, server.studio)
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def test_monitor_stale_exit_observation_cannot_revoke_restarted_generation(
    tiny_good_bundle, monkeypatch,
) -> None:
    poll_started = threading.Event()
    release_poll = threading.Event()

    class OldProc:
        def poll(self):
            poll_started.set()
            assert release_poll.wait(3)
            return 1

        def terminate(self):
            pass

    class NewProc:
        def poll(self):
            return None

        def terminate(self):
            pass

    monkeypatch.setattr(
        "okf_loom.server.start_quick_tunnel",
        lambda _port: (NewProc(), "https://fenced-restart.trycloudflare.com"),
    )
    server = _app_server(tiny_good_bundle)
    _start_public_ingress(server, allow_public_edit=True)
    server.tunnel_proc = OldProc()
    _start_tunnel_monitor(server, server.studio, interval_s=0.01)
    try:
        assert poll_started.wait(3)
        _clear_owned_tunnel(server, server.studio)
        ingress = _start_public_ingress(server, allow_public_edit=True)
        server.tunnel_proc = NewProc()
        server.tunnel_url = "https://fenced-restart.trycloudflare.com"
        new_proc = server.tunnel_proc
        new_generation = server.public_ingress_generation
        assert ingress is server.public_ingress_server
        release_poll.set()
        time.sleep(0.05)
        assert server.tunnel_proc is new_proc
        assert server.public_ingress_generation == new_generation
        assert server.tunnel_url == "https://fenced-restart.trycloudflare.com"
    finally:
        release_poll.set()
        _shutdown_tunnel_owner(server, server.studio)
        server.server_close()


def test_shutdown_cancels_monitor_and_settles_owned_process_once(
    tiny_good_bundle,
) -> None:
    class Proc:
        def __init__(self):
            self.terminated = 0

        def poll(self):
            return None

        def terminate(self):
            self.terminated += 1

    server = _app_server(tiny_good_bundle)
    _start_public_ingress(server, allow_public_edit=False)
    proc = Proc()
    server.tunnel_proc = proc
    monitor = _start_tunnel_monitor(server, server.studio, interval_s=0.01)

    _shutdown_tunnel_owner(server, server.studio)
    _shutdown_tunnel_owner(server, server.studio)

    assert not monitor.is_alive()
    assert proc.terminated == 1
    assert server.tunnel_proc is None
    assert server.public_ingress_generation is None
    server.server_close()


def test_dynamic_popen_oserror_closes_provisional_ingress_and_stays_local(
    tiny_good_bundle, monkeypatch,
) -> None:
    def fail_start(_port):
        raise RuntimeError("cloudflared could not start: spawn denied")

    monkeypatch.setattr("okf_loom.server.start_quick_tunnel", fail_start)
    server = _app_server(tiny_good_bundle)
    server.tunnel_url = None
    local_thread = threading.Thread(target=server.serve_forever, daemon=True)
    local_thread.start()
    try:
        status, payload = _local_request(
            server, "POST", "/__tunnel", token=server.csrf_token,
            body={"action": "start"},
        )
        assert status == 502
        assert b"could not start" in payload
        assert server.public_ingress_server is None
        assert server.public_ingress_generation is None
        assert server.public_mutation_token is None
        status, _ = _local_request(server, "GET", "/")
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()
        local_thread.join(timeout=3)


def test_startup_popen_failure_closes_ingress_and_continues_local_only(
    tiny_good_bundle, monkeypatch, capsys,
) -> None:
    import okf_loom.server as server_module

    servers = []

    class StubServer:
        def __init__(self, address, _handler):
            self.server_address = address
            servers.append(self)

        def serve_forever(self):
            return None

        def shutdown(self):
            return None

        def server_close(self):
            return None

    monkeypatch.setattr(server_module, "ThreadingHTTPServer", StubServer)
    monkeypatch.setattr(
        server_module, "start_quick_tunnel",
        lambda _port: (_ for _ in ()).throw(
            RuntimeError("cloudflared could not start: spawn denied")
        ),
    )

    server_module.run_server(
        tiny_good_bundle,
        host="127.0.0.1",
        port=0,
        watch=False,
        open_browser=False,
        tunnel=True,
    )

    assert servers
    assert servers[0].public_ingress_server is None
    assert servers[0].public_ingress_generation is None
    assert servers[0].public_mutation_token is None
    assert "continuing local-only" in capsys.readouterr().err


def test_startup_no_url_failure_retains_owner_until_shutdown_retry(
    tiny_good_bundle, monkeypatch,
) -> None:
    import okf_loom.server as server_module

    observed = []
    servers = []

    class FakePopen:
        def __init__(self):
            self.fail = True
            self.terminated = 0

        def poll(self):
            return None

        def terminate(self):
            self.terminated += 1

        def wait(self, *, timeout):
            if self.fail:
                raise subprocess.TimeoutExpired("cloudflared", timeout)
            return 0

        def kill(self):
            if self.fail:
                raise OSError("kill failed")

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    proc = FakePopen()

    class StubServer:
        def __init__(self, address, _handler):
            self.server_address = address
            servers.append(self)

        def serve_forever(self):
            observed.append((
                self.tunnel_proc,
                self.tunnel_url,
                self.public_ingress_generation,
                self.public_mutation_token,
                self.public_ingress_draining_generation,
            ))
            proc.fail = False

        def shutdown(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(server_module, "ThreadingHTTPServer", StubServer)
    monkeypatch.setattr(
        server_module, "start_quick_tunnel",
        lambda _port: (_ for _ in ()).throw(
            TunnelStartOwnershipError("no tunnel URL", proc)
        ),
    )

    server_module.run_server(
        tiny_good_bundle, host="127.0.0.1", port=0, watch=False,
        open_browser=False, tunnel=True,
    )

    assert observed[0][:4] == (proc, None, None, None)
    assert observed[0][4] is not None
    assert proc.terminated >= 2
    assert servers[0].tunnel_proc is None


def test_run_server_terminal_shutdown_waits_for_admitted_lease_and_settles_process(
    tiny_good_bundle, monkeypatch,
) -> None:
    import okf_loom.server as server_module

    servers = []

    class Proc:
        def __init__(self):
            self.terminated = 0

        def poll(self):
            return None

        def terminate(self):
            self.terminated += 1

    proc = Proc()
    lease_release = threading.Event()

    class StubServer:
        def __init__(self, address, _handler):
            self.server_address = address
            servers.append(self)

        def serve_forever(self):
            generation = self.public_ingress_generation
            self.public_mutation_inflight[generation] = 1
            def finish_admitted_mutation():
                assert lease_release.wait(3)
                with self._public_mutation_condition:
                    self.public_mutation_inflight[generation] = 0
                    self._public_mutation_condition.notify_all()
            threading.Thread(target=finish_admitted_mutation).start()
            threading.Timer(0.05, lease_release.set).start()

        def shutdown(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(server_module, "ThreadingHTTPServer", StubServer)
    monkeypatch.setattr(
        server_module, "start_quick_tunnel",
        lambda _port: (proc, "https://terminal.trycloudflare.com"),
    )

    started = time.monotonic()
    server_module.run_server(
        tiny_good_bundle, host="127.0.0.1", port=0, watch=False,
        open_browser=False, tunnel=True,
    )
    elapsed = time.monotonic() - started

    server = servers[0]
    assert elapsed >= 0.04
    assert proc.terminated == 1
    assert server.tunnel_proc is None
    assert server.tunnel_url is None
    assert server.public_ingress_generation is None
    assert server.public_mutation_token is None
    assert server.tunnel_terminal_closing is True
    assert server.public_ingress_draining_generation is None
    with pytest.raises(RuntimeError, match="draining|terminal"):
        _start_public_ingress(server, allow_public_edit=False)


def test_terminal_cleanup_takes_over_retained_drain_and_is_idempotent(
    tiny_good_bundle,
) -> None:
    server = _app_server(tiny_good_bundle)
    condition = _ensure_public_mutation_lifecycle(server)
    with condition:
        server.tunnel_terminal_closing = True
        server.public_ingress_draining_generation = "retained-generation"
        server.public_ingress_generation = None
        server.tunnel_proc = None
        server.tunnel_settlement_in_progress = False

    _shutdown_tunnel_owner(server, server.studio, terminal=True, drain_timeout_s=0.01)
    _shutdown_tunnel_owner(server, server.studio, terminal=True, drain_timeout_s=0.01)

    assert server.public_ingress_draining_generation is None
    assert server.tunnel_proc is None
    assert server.tunnel_terminal_closing is True
    server.server_close()
    server.studio.close()


def test_startup_immediate_exit_never_publishes_url_without_process_owner(
    tiny_good_bundle, monkeypatch,
) -> None:
    import okf_loom.server as server_module

    monitor_started = threading.Event()
    settled = threading.Event()
    observed = []
    real_start_monitor = server_module._start_tunnel_monitor

    class ExitedProc:
        def poll(self):
            return 1

        def terminate(self):
            settled.set()

    class StubServer:
        def __init__(self, address, _handler):
            self.server_address = address

        def serve_forever(self):
            assert settled.wait(3)
            observed.append((self.tunnel_proc, self.tunnel_url))

        def shutdown(self):
            return None

        def server_close(self):
            return None

    def immediate_exit_start(_port):
        assert not monitor_started.is_set()
        return ExitedProc(), "https://startup-exit.trycloudflare.com"

    def start_monitor_after_publication(server, studio):
        pair = (server.tunnel_proc, server.tunnel_url)
        assert (pair[0] is None) == (pair[1] is None)
        assert pair[0] is not None
        monitor_started.set()
        return real_start_monitor(server, studio, interval_s=0.01)

    monkeypatch.setattr(server_module, "ThreadingHTTPServer", StubServer)
    monkeypatch.setattr(server_module, "start_quick_tunnel", immediate_exit_start)
    monkeypatch.setattr(server_module, "_start_tunnel_monitor", start_monitor_after_publication)

    server_module.run_server(
        tiny_good_bundle,
        host="127.0.0.1",
        port=0,
        watch=False,
        open_browser=False,
        tunnel=True,
    )

    assert monitor_started.is_set()
    assert observed == [(None, None)]


# ---------------------------------------------------------------------------
# CLI wiring — the flag exists and defaults off
# ---------------------------------------------------------------------------

def test_serve_parser_accepts_tunnel_flag() -> None:
    from okf_loom.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["serve", "some-bundle", "--tunnel", "--no-open"])
    assert args.tunnel is True
    args2 = parser.parse_args(["serve", "some-bundle"])
    assert args2.tunnel is False
