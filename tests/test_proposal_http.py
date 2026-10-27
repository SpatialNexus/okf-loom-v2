from __future__ import annotations

import io
import http.client
import json
import threading
from types import SimpleNamespace
import pytest

from okf_loom.proposal_review.activation import ProductionCapturePort, activate_http
from okf_loom.proposal_review.studio_activity import StudioActivityAdapter
from okf_loom.proposal_review.studio_outbox import StudioOutboxAppender
from okf_loom.proposal_review.http import SECURITY_HEADERS, _error, dispatch
from okf_loom.proposal_review.outbox import OutboxReconciler
from okf_loom.proposal_review.studio_outbox import StudioOutboxAppender
from okf_loom.server import OKFWikiHandler, ThreadingHTTPServer
from okf_loom.studio import Studio
from okf_loom.proposal_review.service_contracts import (TRANSPORT_CODES,
                                                         SuccessEnvelope,
                                                         ErrorBody, ErrorEnvelope,
                                                         PersistedErrorResponse,
                                                         transport_disposition)
from okf_loom.proposal_review.transactions import TransactionTerminalError
from okf_loom.proposal_review.errors import (RepositoryError, SchemaError,
                                             StaleReadModelError)


class _Socket:
    def getsockname(self): return ("127.0.0.1", 8787)
    def getpeername(self): return ("127.0.0.1", 12345)


class _NoRead(io.BytesIO):
    def read(self, *args):
        raise AssertionError("body must not be read")


class _Handler:
    def __init__(self, path, *, headers=None, service=object(), edit=True):
        self.path = path
        self.headers = headers or {"Host": "localhost", "X-OKF-Token": "token",
                                   "X-OKF-Proposal-Token": "proposal-token"}
        self.server = SimpleNamespace(allowed_hosts=("localhost", "127.0.0.1"),
            csrf_token="token", proposal_service=service, proposal_outbox=None,
            proposal_token="proposal-token",
            proposal_allowed_origins=(("http", "localhost", 80),
                                      ("http", "127.0.0.1", 80)),
            studio_edit=edit, tunnel_url=None, proposal_public_exposure=False)
        self.connection = _Socket()
        self.rfile = _NoRead()
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = {}
    def send_response(self, status): self.status = status
    def send_header(self, key, value): self.response_headers[key] = value
    def end_headers(self): pass


def _mutation(path="/__proposal/v1/proposals", *, body=b"{}", service=object(),
              extra=None, edit=True):
    headers = {"Host": "localhost", "X-OKF-Token": "token",
               "X-OKF-Proposal-Token": "proposal-token",
               "Content-Type": "application/json", "Content-Length": str(len(body))}
    headers.update(extra or {})
    handler = _Handler(path, headers=headers, service=service, edit=edit)
    handler.rfile = io.BytesIO(body)
    return handler


def test_err_01_host() -> None:
    handler = _Handler("/__proposal/v1/proposals", headers={"X-OKF-Token": "bad"})
    assert dispatch(handler, "GET") and handler.status == 400
    assert all(handler.response_headers[k] == v for k, v in SECURITY_HEADERS.items())
    assert b'"code":"invalid_host"' in handler.wfile.getvalue()


def test_err_05_no_edit() -> None:
    headers = {"Host": "localhost", "X-OKF-Token": "token",
               "Content-Length": "999999999", "Content-Type": "text/plain"}
    handler = _Handler("/__proposal/v1/proposals", headers=headers, edit=False)
    assert dispatch(handler, "POST") and handler.status == 403
    assert b'"code":"read_only"' in handler.wfile.getvalue()


def test_apply_and_undo_are_registered_and_require_idempotency() -> None:
    for suffix in ("apply", "undo"):
        path = f"/__proposal/v1/proposals/01ARZ3NDEKTSV4RRFFQ69G5FAV/revisions/1/{suffix}"
        handler = _mutation(path)
        assert dispatch(handler, "POST") and handler.status == 422
        assert b'"code":"invalid_request"' in handler.wfile.getvalue()


def test_terminal_transaction_error_is_written_verbatim_to_http_wire() -> None:
    envelope = ErrorEnvelope(ErrorBody(
        "apply_rejected", "Transaction rolled back before commit decision.", "txn_replay",
        details=({"transaction_id": "txn_replay"},))).to_mapping()

    class FailingService:
        def apply(self, *_args, **_kwargs):
            raise TransactionTerminalError(PersistedErrorResponse(409, envelope))

    proposal_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    body = json.dumps({"proposal_id": proposal_id, "proposal_revision": 1}).encode()
    handler = _mutation(
        f"/__proposal/v1/proposals/{proposal_id}/revisions/1/apply",
        body=body, service=FailingService(),
        extra={"Idempotency-Key": "K" * 32})
    assert dispatch(handler, "POST")
    assert handler.status == 409
    assert json.loads(handler.wfile.getvalue()) == envelope


def test_proxied_studio_token_alone_cannot_mutate() -> None:
    headers = {"Host": "localhost", "X-OKF-Token": "token",
               "Content-Type": "application/json", "Content-Length": "2"}
    handler = _Handler("/__proposal/v1/proposals", headers=headers)
    handler.rfile = io.BytesIO(b"{}")
    assert dispatch(handler, "POST") and handler.status == 401
    assert b'"code":"invalid_proposal_token"' in handler.wfile.getvalue()


def test_proposal_token_is_not_embedded_in_ordinary_studio_html(tmp_path) -> None:
    studio = Studio.for_bundle(tmp_path)
    handler = object.__new__(OKFWikiHandler)
    handler.server = SimpleNamespace(
        studio=studio, csrf_token="studio-token", proposal_token="private-proposal-token",
        studio_live=True, studio_edit=True, studio_theme="auto")
    bootstrap = handler._studio_bootstrap()
    assert "studio-token" in bootstrap
    assert "private-proposal-token" not in bootstrap


def test_err_03_origin() -> None:
    accepted = ("http://localhost", "http://localhost:80", "http://127.0.0.1",
                "http://[::1]")
    rejected = ("null", "https://localhost", "http://localhost:81",
                "http://user@localhost", "http://localhost/path",
                "http://localhost?x=1", "http://localhost#x", "http://::1")
    for origin in (*accepted, *rejected):
        headers = {"Host": "localhost", "X-OKF-Token": "token",
                   "X-OKF-Proposal-Token": "proposal-token", "Origin": origin,
                   "Content-Type": "text/plain", "Content-Length": "0"}
        handler = _Handler("/__proposal/v1/proposals", headers=headers)
        handler.server.proposal_allowed_origins += (("http", "::1", 80),)
        dispatch(handler, "POST")
        if origin in accepted:
            assert handler.status == 415
        else:
            assert handler.status == 403


def test_request_target_cap_and_strict_query_precede_service() -> None:
    class Spy:
        called = False
        def list(self, query):
            self.called = True
            raise AssertionError
    for target in (
        "/__proposal/v1/" + "x" * 9000,
        "/__proposal/v1/proposals?state=ready;cursor=x",
        "/__proposal/v1/proposals?state=ready&state=ready",
        "/__proposal/v1/proposals?unknown=x",
        "/__proposal/v1/proposals?state=%GG",
        "/__proposal/v1/proposals?a=1&b=2&c=3&d=4",
    ):
        spy = Spy()
        handler = _Handler(target, service=spy)
        assert dispatch(handler, "GET") and handler.status in {400, 414, 422}
        assert not spy.called


def test_err_04_route() -> None:
    path = "/__proposal/v1/proposals"
    for method in ("HEAD", "OPTIONS", "TRACE", "PUT", "PATCH", "DELETE", "BREW"):
        handler = _Handler(path)
        assert dispatch(handler, method) and handler.status == 405
        assert b'"code":"method_not_allowed"' in handler.wfile.getvalue()
        assert all(handler.response_headers[k] == v for k, v in SECURITY_HEADERS.items())


def test_every_shared_code_has_exact_http_flags_and_closed_envelope() -> None:
    for code in TRANSPORT_CODES:
        disposition = transport_disposition(code)
        handler = _Handler("/__proposal/v1/proposals")
        _error(handler, disposition.http_status, code, "bounded")
        body = json.loads(handler.wfile.getvalue())
        assert handler.status == disposition.http_status
        assert body["error"]["code"] == code
        assert body["error"]["retryable"] is disposition.retryable
        assert body["error"]["resync_required"] is disposition.resync_required
        assert body["error"]["details"] == []


def test_err_02_token() -> None:
    handler = _Handler("/__proposal/v1/proposals", headers={"Host": "localhost"})
    dispatch(handler, "GET")
    assert handler.status == 401 and b'"code":"invalid_session_token"' in handler.wfile.getvalue()


def test_err_06_loopback() -> None:
    handler = _mutation()
    handler.connection.getpeername = lambda: ("198.51.100.8", 1234)
    dispatch(handler, "POST")
    assert handler.status == 403 and b"proposal_mutation_loopback_only" in handler.wfile.getvalue()


def test_err_07_body_cap() -> None:
    handler = _mutation(extra={"Content-Length": str(1_048_577)})
    handler.rfile = _NoRead()
    dispatch(handler, "POST")
    assert handler.status == 413 and b'"code":"body_too_large"' in handler.wfile.getvalue()


@pytest.mark.parametrize("suffix", ["apply", "undo"])
def test_transaction_body_cap_and_duplicate_json_precede_service(suffix) -> None:
    class Spy:
        called = False
        def __getattr__(self, _name):
            def call(*_args, **_kwargs):
                self.called = True
                raise AssertionError("service must not run")
            return call
    path = f"/__proposal/v1/proposals/01ARZ3NDEKTSV4RRFFQ69G5FAV/revisions/1/{suffix}"
    spy = Spy()
    oversized = _mutation(path, service=spy, extra={
        "Content-Length": "65537", "Idempotency-Key": "A" * 32})
    oversized.rfile = _NoRead()
    dispatch(oversized, "POST")
    assert oversized.status == 413 and not spy.called
    duplicate = _mutation(path, body=b'{"schema":1,"schema":1}', service=spy,
                          extra={"Idempotency-Key": "A" * 32})
    dispatch(duplicate, "POST")
    assert duplicate.status == 400 and not spy.called


@pytest.mark.parametrize("suffix", ["apply", "undo"])
def test_transaction_routes_keep_inactive_read_only_and_loopback_precedence(suffix) -> None:
    path = f"/__proposal/v1/proposals/01ARZ3NDEKTSV4RRFFQ69G5FAV/revisions/1/{suffix}"
    inactive = _mutation(path, service=None, extra={"Content-Length": "999999"})
    dispatch(inactive, "POST")
    assert inactive.status == 404 and b"capability_unavailable" in inactive.wfile.getvalue()
    readonly = _mutation(path, edit=False, extra={"Content-Length": "999999"})
    dispatch(readonly, "POST")
    assert readonly.status == 403 and b'"code":"read_only"' in readonly.wfile.getvalue()
    remote = _mutation(path, extra={"Content-Length": "999999"})
    remote.connection.getpeername = lambda: ("198.51.100.8", 1234)
    dispatch(remote, "POST")
    assert remote.status == 403 and b"proposal_mutation_loopback_only" in remote.wfile.getvalue()


def test_transaction_progress_is_202_with_retry_after() -> None:
    class Progress:
        def apply(self, *_args, **_kwargs):
            return SuccessEnvelope({"status": "transaction_in_progress",
                                    "attempt": {"phase": "publishing"},
                                    "read_model_token": "sha256:" + "0" * 64})
    proposal_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    path = f"/__proposal/v1/proposals/{proposal_id}/revisions/1/apply"
    body = json.dumps({"schema": 1, "proposal_id": proposal_id,
                       "proposal_revision": 1}).encode()
    handler = _mutation(path, body=body, service=Progress(),
                        extra={"Idempotency-Key": "A" * 32})
    dispatch(handler, "POST")
    assert handler.status == 202 and handler.response_headers["Retry-After"] == "1"


def test_err_08_json() -> None:
    wrong = _mutation(extra={"Content-Type": "text/plain"})
    dispatch(wrong, "POST")
    assert wrong.status == 415
    duplicate = _mutation(body=b'{"schema":1,"schema":1}')
    dispatch(duplicate, "POST")
    assert duplicate.status == 400 and b'"code":"invalid_json"' in duplicate.wfile.getvalue()


class _CreateError:
    def __init__(self, error): self.error = error
    def create(self, *args, **kwargs): raise self.error


def test_err_09_schema() -> None:
    handler = _mutation(service=_CreateError(SchemaError("bad", path="create.mode")),
                        extra={"Idempotency-Key": "A" * 32})
    dispatch(handler, "POST")
    body = json.loads(handler.wfile.getvalue())
    assert handler.status == 422 and body["error"]["details"] == [{"path": "create.mode"}]


@pytest.mark.parametrize("code,status", [
    ("create_rate_limited", 429), ("preview_concurrency_limited", 429),
    ("repository_quota_exceeded", 507), ("checkpoint_quota_exceeded", 507),
    ("tombstone_quota_exceeded", 507), ("quarantine_quota_exceeded", 507),
    ("audit_quota_exceeded", 507), ("bundle_busy", 503),
    ("lock_unavailable", 503), ("storage_corrupt", 503),
])
def test_err_10_resource(code, status) -> None:
    handler = _mutation(service=_CreateError(RepositoryError("bounded", code=code)),
                        extra={"Idempotency-Key": "A" * 32})
    dispatch(handler, "POST")
    body = json.loads(handler.wfile.getvalue())
    assert handler.status == status
    assert body["error"]["retryable"] is (status in {429, 503})


@pytest.mark.parametrize("code", ["idempotency_conflict", "indeterminate_outcome"])
def test_err_11_idempotency(code) -> None:
    handler = _mutation(service=_CreateError(RepositoryError("conflict", code=code)),
                        extra={"Idempotency-Key": "A" * 32})
    dispatch(handler, "POST")
    assert handler.status == 409 and code.encode() in handler.wfile.getvalue()


@pytest.mark.parametrize("code,status", [
    ("proposal_not_found", 404), ("preview_expired", 410), ("invalid_state", 409),
])
def test_err_12_resource_state(code, status) -> None:
    class Missing:
        def show(self, *args): raise RepositoryError("state", code=code)
    handler = _Handler("/__proposal/v1/proposals/01J2Y8N5Z9Q3A7K6M4T1V0WXYZ/revisions/1",
                       service=Missing())
    dispatch(handler, "GET")
    assert handler.status == status and code.encode() in handler.wfile.getvalue()


@pytest.mark.parametrize("error,code,status", [
    (StaleReadModelError(), "stale_read_model", 409),
    (RepositoryError("snapshot", code="snapshot_changed"), "snapshot_changed", 409),
    (RepositoryError("unstable", code="snapshot_unstable"), "snapshot_unstable", 409),
    (RepositoryError("conflict", code="proposal_conflict"), "proposal_conflict", 409),
    (SchemaError("validation", code="validation_failed"), "validation_failed", 422),
])
def test_err_13_conflict(error, code, status) -> None:
    handler = _mutation(service=_CreateError(error),
                        extra={"Idempotency-Key": "A" * 32})
    dispatch(handler, "POST")
    body = json.loads(handler.wfile.getvalue())
    assert handler.status == status and body["error"]["code"] == code


@pytest.mark.parametrize("error,code", [
    (RuntimeError("secret body material"), "internal_error"),
    (RepositoryError("parity", code="contract_parity_failure"), "contract_parity_failure"),
])
def test_err_14_internal(error, code) -> None:
    handler = _mutation(service=_CreateError(error),
                        extra={"Idempotency-Key": "A" * 32})
    dispatch(handler, "POST")
    assert handler.status == 500
    assert code.encode() in handler.wfile.getvalue()
    assert b"secret body material" not in handler.wfile.getvalue()


class _SuccessService:
    def create(self, *args, **kwargs): return SuccessEnvelope({"operation": "create"})
    def list(self, *args, **kwargs): return SuccessEnvelope({"operation": "list"})
    def show(self, *args, **kwargs): return SuccessEnvelope({"operation": "show"})
    def decide(self, *args, **kwargs): return SuccessEnvelope({"operation": "decide"})
    def withdraw(self, *args, **kwargs): return SuccessEnvelope({"operation": "withdraw"})
    def apply(self, *args, **kwargs): return SuccessEnvelope({"operation": "apply"})
    def undo(self, *args, **kwargs): return SuccessEnvelope({"operation": "undo"})


def test_http_adapter_create_envelope() -> None:
    handler = _mutation(service=_SuccessService(), extra={"Idempotency-Key": "A" * 32})
    dispatch(handler, "POST")
    assert handler.status == 201 and json.loads(handler.wfile.getvalue())["data"]["operation"] == "create"


def test_http_adapter_list_envelope() -> None:
    handler = _Handler("/__proposal/v1/proposals?limit=50", service=_SuccessService())
    dispatch(handler, "GET")
    assert handler.status == 200 and json.loads(handler.wfile.getvalue())["data"]["operation"] == "list"


def test_http_adapter_show_envelope() -> None:
    handler = _Handler("/__proposal/v1/proposals/01J2Y8N5Z9Q3A7K6M4T1V0WXYZ/revisions/1",
                       service=_SuccessService())
    dispatch(handler, "GET")
    assert handler.status == 200 and json.loads(handler.wfile.getvalue())["data"]["operation"] == "show"


def test_http_adapter_decision_envelope() -> None:
    path = ("/__proposal/v1/proposals/01J2Y8N5Z9Q3A7K6M4T1V0WXYZ/revisions/1/"
            "decisions/op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ")
    handler = _mutation(path, service=_SuccessService())
    dispatch(handler, "POST")
    assert handler.status == 200 and json.loads(handler.wfile.getvalue())["data"]["operation"] == "decide"


def test_http_adapter_withdraw_envelope() -> None:
    path = "/__proposal/v1/proposals/01J2Y8N5Z9Q3A7K6M4T1V0WXYZ/revisions/1/withdraw"
    handler = _mutation(path, service=_SuccessService(), extra={"Idempotency-Key": "A" * 32})
    dispatch(handler, "POST")
    assert handler.status == 200 and json.loads(handler.wfile.getvalue())["data"]["operation"] == "withdraw"


class _RealProposalHTTP:
    def __init__(self, root):
        from conftest import private_session_root
        private_session_root(root)
        self.root = root
        (root / "orders.md").write_text(
            "---\ntype: Table\ntitle: Orders\ndescription: Orders table.\n"
            "tags: [commerce]\ntimestamp: 2026-07-15T00:00:00Z\nresource: /orders.md\n"
            "---\n\n# Orders\n")
        (root / "orders.md").chmod(0o600)
        self.studio = Studio.for_bundle(root)
        self.studio.ensure_session()
        appender = StudioOutboxAppender(self.studio)
        self.activation = activate_http(
            root, transaction_activity=StudioActivityAdapter(self.studio),
            transaction_outbox=appender)
        self.service = self.activation.service
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), OKFWikiHandler)
        self.server.allowed_hosts = ("127.0.0.1", "localhost")
        self.server.csrf_token = "test-token"
        self.server.proposal_service = self.service
        self.server.proposal_token = self.activation.proposal_token
        self.server.proposal_allowed_origins = (("http", "127.0.0.1", self.server.server_port),)
        self.server.proposal_outbox = OutboxReconciler(
            self.service.repository, StudioOutboxAppender(self.studio))
        self.server.studio_edit = True
        self.server.tunnel_url = None
        self.server.proposal_public_exposure = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        captured = ProductionCapturePort(root).capture(
            captured_at=self.service.clock.now(), proposal_capability_active=True)
        self.revision = captured.snapshot.concepts[0].revision
        self.op_id = "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
        self.create_body = {"schema": 1, "mode": "proposal", "author": "agent",
            "summary": "Tag orders", "rationale": "Review metadata.",
            "expected_snapshot": captured.snapshot.to_mapping(),
            "operations": [{"schema": 1, "op_id": self.op_id,
                "kind": "set_frontmatter", "target": "orders",
                "args": {"key": "tags", "value": ["orders"]},
                "reads": ["orders"], "writes": ["orders"],
                "expected_revisions": {"orders": self.revision},
                "depends_on": [], "extensions": {}}],
            "provenance": [], "extensions": {}}

    def request(self, method, path, body=None, key=None, proposal_token=True):
        headers = {"Host": "127.0.0.1", "X-OKF-Token": "test-token"}
        if proposal_token:
            headers["X-OKF-Proposal-Token"] = self.activation.proposal_token
        encoded = None
        if body is not None:
            encoded = json.dumps(body, separators=(",", ":")).encode()
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(encoded))})
        if key:
            headers["Idempotency-Key"] = key
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        value = json.loads(response.read())
        result = response.status, value, dict(response.getheaders())
        connection.close()
        return result

    def create(self, key, body=None):
        return self.request("POST", "/__proposal/v1/proposals", body or self.create_body, key)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(5)
        self.activation.close()


def test_http_01_create(tmp_path) -> None:
    live = _RealProposalHTTP(tmp_path)
    try:
        missing, _, _ = live.create(None)
        assert missing == 422
        status, created, _ = live.create("A" * 32)
        assert status == 201 and created["schema"] == 1
        proposal_id = created["data"]["proposal"]["proposal_id"]
        supersede = dict(live.create_body)
        supersede.update(summary="Successor", proposal_id=proposal_id, supersedes_revision=1)
        status, successor, _ = live.create("B" * 32, supersede)
        assert status == 201 and successor["data"]["proposal"]["proposal_revision"] == 2

        legacy_path = tmp_path / ".okf-loom/session/proposals.jsonl"
        legacy_path.write_text(json.dumps({"id": "legacy-7", "summary": "Old",
            "author": "agent", "created_at": "2026-07-15T00:00:00.000Z"}) + "\n")
        legacy_path.chmod(0o600)
        before = legacy_path.read_bytes()
        legacy = dict(live.create_body)
        legacy["legacy_identity"] = {"legacy_id": "legacy-7", "source_generation": 0}
        status, regenerated, _ = live.create("C" * 32, legacy)
        assert status == 201
        assert any("legacy-7" in row["value"] for row in regenerated["data"]["proposal"]["provenance"])
        assert legacy_path.read_bytes() == before
    finally:
        live.close()


def test_http_02_list(tmp_path) -> None:
    live = _RealProposalHTTP(tmp_path)
    try:
        assert live.create("A" * 32)[0] == 201
        second = dict(live.create_body); second["summary"] = "Second"
        assert live.create("B" * 32, second)[0] == 201
        status, first, _ = live.request("GET", "/__proposal/v1/proposals?limit=1")
        assert status == 200 and first["data"]["next_cursor"]
        assert first["data"]["page_snapshot_token"].startswith("sha256:")
        cursor = first["data"]["next_cursor"]
        status, continuation, _ = live.request(
            "GET", "/__proposal/v1/proposals?limit=1&cursor=" + cursor)
        assert status == 200 and len(continuation["data"]["items"]) == 1
        assert continuation["data"]["items"][0]["proposal_id"] != first["data"]["items"][0]["proposal_id"]
    finally:
        live.close()


def test_http_03_show(tmp_path) -> None:
    live = _RealProposalHTTP(tmp_path)
    try:
        _, created, _ = live.create("A" * 32)
        proposal_id = created["data"]["proposal"]["proposal_id"]
        status, shown, headers = live.request(
            "GET", f"/__proposal/v1/proposals/{proposal_id}/revisions/1")
        assert status == 200 and shown["data"]["proposal"]["proposal_id"] == proposal_id
        assert set(shown) == {"schema", "data"}
        assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
    finally:
        live.close()


def test_http_04_decision(tmp_path) -> None:
    live = _RealProposalHTTP(tmp_path)
    try:
        _, created, _ = live.create("A" * 32)
        proposal_id = created["data"]["proposal"]["proposal_id"]
        base = f"/__proposal/v1/proposals/{proposal_id}/revisions/1"
        _, shown, _ = live.request("GET", base)
        decision = {"schema": 1, "decision": "accepted", "reason": "",
                    "previous_row_token": None,
                    "read_model_token": shown["data"]["read_model_token"]}
        status, keyless, _ = live.request("POST", base + f"/decisions/{live.op_id}", decision)
        assert status == 200
        row_token = keyless["data"]["decision"]["row_token"]
        keyed = {"schema": 1, "decision": "rejected", "reason": "changed",
                 "previous_row_token": row_token,
                 "read_model_token": keyless["data"]["read_model_token"]}
        status, changed, _ = live.request(
            "POST", base + f"/decisions/{live.op_id}", keyed, "B" * 32)
        assert status == 200 and changed["data"]["decision"]["decision"] == "rejected"
        stale, _, _ = live.request("POST", base + f"/decisions/{live.op_id}", keyed, "C" * 32)
        assert stale == 409
    finally:
        live.close()


def test_http_05_withdraw(tmp_path) -> None:
    live = _RealProposalHTTP(tmp_path)
    try:
        _, created, _ = live.create("A" * 32)
        proposal_id = created["data"]["proposal"]["proposal_id"]
        base = f"/__proposal/v1/proposals/{proposal_id}/revisions/1"
        _, shown, _ = live.request("GET", base)
        body = {"schema": 1, "read_model_token": shown["data"]["read_model_token"],
                "reason": "stop"}
        status, withdrawn, _ = live.request("POST", base + "/withdraw", body, "B" * 32)
        assert status == 200 and withdrawn["data"]["state"] == "withdrawn"
        replay_status, replayed, _ = live.request("POST", base + "/withdraw", body, "B" * 32)
        assert replay_status == 200 and replayed == withdrawn
        _, terminal, _ = live.request("GET", base)
        assert terminal["data"]["state"] == "withdrawn"
        invalid, _, _ = live.request("POST", base + "/withdraw", body, "C" * 32)
        assert invalid == 409
    finally:
        live.close()


def test_http_06_07_apply_undo_success_and_replay(tmp_path) -> None:
    live = _RealProposalHTTP(tmp_path)
    try:
        _, created, _ = live.create("A" * 32)
        proposal = created["data"]["proposal"]
        proposal_id = proposal["proposal_id"]
        base = f"/__proposal/v1/proposals/{proposal_id}/revisions/1"
        _, shown, _ = live.request("GET", base)
        decision = {"schema": 1, "decision": "accepted", "reason": "",
                    "previous_row_token": None,
                    "read_model_token": shown["data"]["read_model_token"]}
        assert live.request("POST", base + f"/decisions/{live.op_id}",
                            decision, "B" * 32)[0] == 200
        _, ready, _ = live.request("GET", base)
        apply = {"schema": 1, "proposal_id": proposal_id, "proposal_revision": 1,
                 "actor": "local-user",
                 "snapshot_digest": proposal["expected_snapshot"]["snapshot_digest"],
                 "validation_context_digest": proposal["expected_snapshot"]["validation_context_digest"],
                 "decision_set_token": ready["data"]["decision_set_token"],
                 "read_model_token": ready["data"]["read_model_token"]}
        status, applied, _ = live.request("POST", base + "/apply", apply, "K" * 32)
        assert status == 200, applied
        assert applied["data"]["status"] == "applied"
        replay_status, replayed, _ = live.request("POST", base + "/apply", apply, "K" * 32)
        assert replay_status == 200 and replayed["data"]["replayed"] is True
        undo = {"schema": 1, "proposal_id": proposal_id, "proposal_revision": 1,
                "actor": "local-user", "transaction_id": applied["data"]["transaction_id"],
                "undo_group_id": applied["data"]["undo_group_id"],
                "read_model_token": ready["data"]["read_model_token"]}
        status, undone, _ = live.request("POST", base + "/undo", undo, "U" * 32)
        assert status == 200 and undone["data"]["status"] == "undone"
        replay_status, replayed, _ = live.request("POST", base + "/undo", undo, "U" * 32)
        assert replay_status == 200 and replayed["data"]["replayed"] is True
    finally:
        live.close()


def test_transport_async_committed_mutation_outbox_failure_retry(tmp_path) -> None:
    from conftest import private_session_root
    private_session_root(tmp_path)
    (tmp_path / "orders.md").write_text(
        "---\ntype: Table\n---\n\nOrders\n", encoding="utf-8")
    activation = activate_http(tmp_path)
    service = activation.service
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    server = ThreadingHTTPServer(("127.0.0.1", 0), OKFWikiHandler)
    server.allowed_hosts = ("127.0.0.1", "localhost")
    server.csrf_token = "test-token"
    server.proposal_service = service
    server.proposal_token = activation.proposal_token
    server.proposal_allowed_origins = (("http", "127.0.0.1", server.server_port),)
    real_outbox = OutboxReconciler(service.repository, StudioOutboxAppender(studio))
    class FailingOutbox:
        def reconcile_pending(self, *, limit):
            raise RuntimeError("simulated append failure")
    server.proposal_outbox = FailingOutbox()
    server.studio_edit = True
    server.tunnel_url = None
    server.proposal_public_exposure = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    base_headers = {"Host": "127.0.0.1", "X-OKF-Token": "test-token",
                    "X-OKF-Proposal-Token": activation.proposal_token}

    def request(method, path, body=None, key=None):
        headers = dict(base_headers)
        encoded = None
        if body is not None:
            encoded = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(encoded))
        if key:
            headers["Idempotency-Key"] = key
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        value = json.loads(response.read())
        connection.close()  # proposal responses intentionally close to mask unread bodies
        return response.status, value

    try:
        captured = ProductionCapturePort(tmp_path).capture(
            captured_at=service.clock.now(), proposal_capability_active=True)
        revision = captured.snapshot.concepts[0].revision
        op_id = "op_01J2Y8N5Z9Q3A7K6M4T1V0WXYZ"
        create = {"schema": 1, "mode": "proposal", "author": "agent",
            "summary": "Tag orders", "rationale": "Review metadata.",
            "expected_snapshot": captured.snapshot.to_mapping(),
            "operations": [{"schema": 1, "op_id": op_id,
                "kind": "set_frontmatter", "target": "orders",
                "args": {"key": "tags", "value": ["orders"]},
                "reads": ["orders"], "writes": ["orders"],
                "expected_revisions": {"orders": revision},
                "depends_on": [], "extensions": {}}],
            "provenance": [], "extensions": {}}
        status, created = request("POST", "/__proposal/v1/proposals", create, "A" * 32)
        assert status == 201
        assert service.repository.pending_outbox()
        server.proposal_outbox = real_outbox
        replay_status, replayed = request(
            "POST", "/__proposal/v1/proposals", create, "A" * 32)
        assert replay_status == 201 and replayed == created
        assert not service.repository.pending_outbox()
        model = created["data"]
        proposal_id = model["proposal"]["proposal_id"]
        status, listed = request("GET", "/__proposal/v1/proposals?limit=50")
        assert status == 200 and listed["data"]["items"][0]["proposal_id"] == proposal_id
        path = f"/__proposal/v1/proposals/{proposal_id}/revisions/1"
        status, shown = request("GET", path)
        assert status == 200
        decision = {"schema": 1, "decision": "accepted", "reason": "",
                    "previous_row_token": None,
                    "read_model_token": shown["data"]["read_model_token"]}
        status, decided = request("POST", path + f"/decisions/{op_id}", decision, "B" * 32)
        assert status == 200
        status, ready = request("GET", path)
        assert status == 200 and ready["data"]["state"] == "ready"
        withdrawal = {"schema": 1, "read_model_token": ready["data"]["read_model_token"],
                      "reason": "No longer needed"}
        status, withdrawn = request("POST", path + "/withdraw", withdrawal, "C" * 32)
        assert status == 200 and withdrawn["data"]["state"] == "withdrawn"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(5)
