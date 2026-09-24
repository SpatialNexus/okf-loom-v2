"""Dedicated stdlib-HTTP adapter for the five Proposal Review v1 routes."""
from __future__ import annotations

import re
import secrets
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit

from .canonical import dumps_canonical, loads_canonical
from .errors import ProposalReviewError
from .service_contracts import (ErrorBody, ErrorEnvelope, StrictListQuery,
                                bounded_error_details, transport_disposition)

PREFIX = "/__proposal/v1/"
REQUEST_TARGET_MAX_BYTES = 8192
QUERY_FIELD_MAX = 3
SECURITY_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache",
                    "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}
_SHOW = re.compile(r"^/__proposal/v1/proposals/([^/]+)/revisions/([^/]+)$")
_DECIDE = re.compile(r"^/__proposal/v1/proposals/([^/]+)/revisions/([^/]+)/decisions/([^/]+)$")
_WITHDRAW = re.compile(r"^/__proposal/v1/proposals/([^/]+)/revisions/([^/]+)/withdraw$")
_APPLY = re.compile(r"^/__proposal/v1/proposals/([^/]+)/revisions/([^/]+)/apply$")
_UNDO = re.compile(r"^/__proposal/v1/proposals/([^/]+)/revisions/([^/]+)/undo$")
_KNOWN_POST = "/__proposal/v1/proposals"


@dataclass(frozen=True)
class Admission:
    """Body-independent proposal route decision owned by the HTTP boundary."""

    operation: str
    captures: Any
    raw_query: str
    body_cap: int
    content_length: int

def _request_id(handler: Any) -> str:
    return "req_" + secrets.token_hex(13)


def _send(handler: Any, status: int, value: dict[str, Any]) -> None:
    body = dumps_canonical(value)
    handler.close_connection = True
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    for key, val in SECURITY_HEADERS.items():
        handler.send_header(key, val)
    if getattr(handler, "_proposal_retry_after", False):
        handler.send_header("Retry-After", "1")
        handler._proposal_retry_after = False
    handler.send_header("Connection", "close")
    handler.end_headers()
    if not getattr(handler, "_head_only", False):
        handler.wfile.write(body)


def _error(handler: Any, status: int, code: str, message: str,
           *, retryable: bool | None = None, resync: bool | None = None,
           details: tuple[Any, ...] = ()) -> None:
    disposition = transport_disposition(code)
    body = ErrorEnvelope(ErrorBody(code, message, _request_id(handler),
        disposition.retryable if retryable is None else retryable,
        disposition.resync_required if resync is None else resync,
        details)).to_mapping()
    _send(handler, status, body)


def _raw_target(handler: Any) -> tuple[str, str]:
    raw = handler.path
    try:
        encoded = raw.encode("iso-8859-1")
    except UnicodeEncodeError:
        encoded = raw.encode("utf-8")
    if len(encoded) > REQUEST_TARGET_MAX_BYTES:
        raise _AdapterError(414, "request_target_too_large", "Proposal request target is too large.")
    if not raw.startswith("/") or "#" in raw or raw.count("?") > 1:
        raise _AdapterError(400, "invalid_request_target", "Proposal request target is invalid.")
    for match in re.finditer("%", raw):
        if not re.fullmatch(r"[0-9A-Fa-f]{2}", raw[match.start() + 1:match.start() + 3]):
            raise _AdapterError(400, "invalid_request_target", "Proposal request target has malformed escaping.")
    path_raw, separator, query = raw.partition("?")
    try:
        path = unquote_to_bytes(path_raw).decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise _AdapterError(400, "invalid_request_target", "Proposal path is not UTF-8.") from exc
    if "\\" in path or "\x00" in path or "%2f" in path_raw.lower() or "%5c" in path_raw.lower():
        raise _AdapterError(400, "invalid_request_target", "Proposal path contains an encoded separator.")
    if not path.startswith(PREFIX):
        return path, query if separator else ""
    return path, query if separator else ""


def _query(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    fields = raw.split("&")
    if len(fields) > QUERY_FIELD_MAX or any(not field or ";" in field for field in fields):
        raise _AdapterError(422, "invalid_request", "Proposal query has too many or invalid fields.")
    result: dict[str, Any] = {}
    for field in fields:
        if field.count("=") != 1:
            raise _AdapterError(422, "invalid_request", "Proposal query field is malformed.")
        key_raw, value_raw = field.split("=", 1)
        try:
            key = unquote_to_bytes(key_raw).decode("utf-8", "strict")
            value = unquote_to_bytes(value_raw).decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise _AdapterError(422, "invalid_request", "Proposal query is not UTF-8.") from exc
        if key not in {"state", "cursor", "limit"} or key in result or len(key) > 16 or len(value) > 4096:
            raise _AdapterError(422, "invalid_request", "Proposal query is not closed.")
        result[key] = int(value) if key == "limit" and value.isascii() and value.isdigit() else value
    return result


def _canonical_origin(raw: str) -> tuple[str, str, int] | None:
    if not raw or raw == "null" or any(ch.isspace() for ch in raw):
        return None
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            return None
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not host:
        return None
    if host.count(":") >= 2 and not parsed.netloc.startswith("["):
        return None
    return parsed.scheme.lower(), host.lower(), port or (443 if parsed.scheme == "https" else 80)


def _host(handler: Any) -> bool:
    raw = (handler.headers.get("Host") or "").strip()
    if not raw:
        return False
    try:
        hostname = urlsplit("//" + raw).hostname or ""
    except ValueError:
        return False
    allowed = getattr(handler.server, "allowed_hosts", ())
    return bool(allowed and hostname in allowed)


def _route(method: str, path: str) -> tuple[str | None, Any]:
    if path == _KNOWN_POST:
        return ("create" if method == "POST" else "list" if method == "GET" else "method", None)
    match = _SHOW.fullmatch(path)
    if match:
        return ("show" if method == "GET" else "method", match.groups())
    match = _DECIDE.fullmatch(path)
    if match:
        return ("decide" if method == "POST" else "method", match.groups())
    match = _WITHDRAW.fullmatch(path)
    if match:
        return ("withdraw" if method == "POST" else "method", match.groups())
    match = _APPLY.fullmatch(path)
    if match:
        return ("apply" if method == "POST" else "method", match.groups())
    match = _UNDO.fullmatch(path)
    if match:
        return ("undo" if method == "POST" else "method", match.groups())
    return (None, None)


def admit(handler: Any, method: str) -> Admission | bool | None:
    """Reject a proposal request before its body is read, or return admission.

    ``None`` means that the target is outside the reserved proposal prefix;
    ``False`` means a response was sent.  The returned object carries the one
    route/auth/header decision later consumed by :func:`dispatch`.
    """
    if not handler.path.startswith(PREFIX):
        return None
    if not _host(handler):
        _error(handler, 400, "invalid_host", "The Host header is not allowed."); return False
    expected = getattr(handler.server, "csrf_token", None)
    supplied = handler.headers.get("X-OKF-Token", "")
    if not expected or not secrets.compare_digest(supplied, expected):
        _error(handler, 401, "invalid_session_token", "The session capability is invalid."); return False
    try:
        path, raw_query = _raw_target(handler)
    except _AdapterError as exc:
        _error(handler, exc.status, exc.code, exc.message)
        return False
    operation, captures = _route(method, path)
    mutating = operation in {"create", "decide", "withdraw", "apply", "undo"}
    if mutating and handler.headers.get("Origin") is not None:
        origin = _canonical_origin(handler.headers.get("Origin", ""))
        if origin not in getattr(handler.server, "proposal_allowed_origins", ()):
            _error(handler, 403, "origin_forbidden", "The request Origin is not allowed."); return False
    if operation is None:
        _error(handler, 404, "not_found", "Route not found."); return False
    if operation == "method":
        _error(handler, 405, "method_not_allowed", "Method not allowed."); return False
    service = getattr(handler.server, "proposal_service", None)
    if service is None:
        _error(handler, 404, "capability_unavailable", "Proposal Review is not active."); return False
    if mutating and not getattr(handler.server, "studio_edit", True):
        _error(handler, 403, "read_only", "The studio is read-only."); return False
    if mutating:
        expected_proposal = getattr(handler.server, "proposal_token", None)
        supplied_proposal = handler.headers.get("X-OKF-Proposal-Token", "")
        if not expected_proposal or not secrets.compare_digest(supplied_proposal, expected_proposal):
            _error(handler, 401, "invalid_proposal_token",
                   "The local proposal mutation capability is invalid."); return False
        try:
            local = handler.connection.getsockname()[0]
            remote = handler.connection.getpeername()[0]
        except (AttributeError, OSError, TypeError):
            local = remote = ""
        from .activation import is_loopback
        exposed = bool(getattr(handler.server, "tunnel_url", None)
                       or getattr(handler.server, "proposal_public_exposure", False))
        if not is_loopback(local) or not is_loopback(remote) or exposed:
            _error(handler, 403, "proposal_mutation_loopback_only",
                    "Proposal mutation is restricted to loopback."); return False
    if not mutating:
        return Admission(operation, captures, raw_query, 0, 0)
    cap = 1_048_576 if operation == "create" else 65_536
    raw_length = handler.headers.get("Content-Length")
    try:
        length = int(raw_length or "0")
        if length < 0:
            raise ValueError
    except ValueError:
        _error(handler, 422, "invalid_request", "Content-Length is invalid.")
        return False
    if length > cap:
        _error(handler, 413, "body_too_large", "Request body exceeds the route limit.")
        return False
    media = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        _error(handler, 415, "unsupported_media_type", "Content-Type must be application/json.")
        return False
    return Admission(operation, captures, raw_query, cap, length)


def dispatch(handler: Any, method: str, *, admission: Admission | None = None) -> bool:
    if admission is None:
        decision = admit(handler, method)
        if decision is None:
            return False
        if decision is False:
            return True
        admission = decision
    operation = admission.operation
    captures = admission.captures
    raw_query = admission.raw_query
    mutating = operation in {"create", "decide", "withdraw", "apply", "undo"}
    service = getattr(handler.server, "proposal_service", None)
    try:
        from .ids import (validate_idempotency_key, validate_op_id,
                          validate_proposal_id, validate_revision)
        if operation != "list" and raw_query:
            raise _AdapterError(422, "invalid_request", "This route does not accept a query string.")
        if operation == "list":
            result = service.list(StrictListQuery.from_mapping(_query(raw_query))); status = 200
        elif operation == "show":
            proposal_id, revision = captures
            result = service.show(validate_proposal_id(proposal_id),
                                  validate_revision(int(revision))); status = 200
        else:
            length = admission.content_length
            read_body = getattr(handler, "_read_request_body", None)
            raw_body = (read_body(length) if callable(read_body)
                        else handler.rfile.read(length))
            data = loads_canonical(raw_body)
            key = handler.headers.get("Idempotency-Key")
            if operation in {"create", "withdraw", "apply", "undo"} and not key:
                raise _AdapterError(422, "invalid_request", "Idempotency-Key is required.")
            if key is not None:
                key = validate_idempotency_key(key)
            if operation == "create":
                result = service.create(data, idempotency_key=key); status = 201
            elif operation == "decide":
                proposal_id, revision, op_id = captures
                result = service.decide(validate_proposal_id(proposal_id),
                                        validate_revision(int(revision)),
                                        validate_op_id(op_id), data,
                                        idempotency_key=key); status = 200
            elif operation == "withdraw":
                proposal_id, revision = captures
                result = service.withdraw(validate_proposal_id(proposal_id),
                                           validate_revision(int(revision)), data,
                                           idempotency_key=key); status = 200
            else:
                proposal_id, revision = captures
                route_id = validate_proposal_id(proposal_id)
                route_revision = validate_revision(int(revision))
                if (not isinstance(data, dict) or data.get("proposal_id") != route_id
                        or data.get("proposal_revision") != route_revision):
                    raise _AdapterError(422, "invalid_request",
                                        "Request proposal authority does not match the route.")
                result = (service.apply(data, idempotency_key=key)
                          if operation == "apply"
                          else service.undo(data, idempotency_key=key))
                status = (202 if result.data.get("status") == "transaction_in_progress"
                          else 200)
        reconciler = getattr(handler.server, "proposal_outbox", None)
        if mutating and reconciler is not None:
            try:
                reconciler.reconcile_pending(limit=100)
            except Exception as exc:
                print(f"proposal outbox reconcile deferred ({type(exc).__name__})", file=sys.stderr)
        if status == 202:
            handler._proposal_retry_after = True
        _send(handler, status, result.to_mapping())
    except _AdapterError as exc:
        _error(handler, exc.status, exc.code, exc.message)
    except (ValueError, TypeError) as exc:
        _error(handler, 422, "invalid_request", "The request is invalid.")
    except ProposalReviewError as exc:
        persisted = getattr(exc, "persisted_error", None)
        if persisted is not None:
            _send(handler, persisted.status, persisted.to_mapping())
            return True
        code = getattr(exc, "code", "internal_error")
        disposition = transport_disposition(code)
        _error(handler, disposition.http_status, code,
               str(getattr(exc, "message", "The proposal request failed."))[:240],
               details=bounded_error_details(exc))
    except Exception:
        _error(handler, 500, "internal_error", "The proposal service failed.")
    return True


class _AdapterError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


__all__ = ["Admission", "PREFIX", "SECURITY_HEADERS", "admit", "dispatch"]
