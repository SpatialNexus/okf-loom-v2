from __future__ import annotations

import os
import http.client
import json
import secrets
import socket
import threading
import time
from pathlib import Path

import pytest

from okf_loom.model import Bundle
from okf_loom.server import (
    OKFWikiHandler,
    _BundleWatcher,
    _OwnedThreadingHTTPServer,
    _PresenceSweeper,
)
from okf_loom.studio import Studio


def _write_bundle(root: Path) -> None:
    (root / "note.md").write_text("---\ntype: note\n---\nBody\n", encoding="utf-8")


def _real_server(root: Path):
    bundle = Bundle.load(root)
    studio = Studio.for_bundle(root, bundle_name=bundle.name)
    studio.ensure_session()
    server = _OwnedThreadingHTTPServer(("127.0.0.1", 0), OKFWikiHandler)
    server.bundle = bundle
    server.config = {}
    server.name = bundle.name
    server.plugin = None
    server._state_lock = threading.Lock()
    server.studio = studio
    server.csrf_token = secrets.token_hex(16)
    server.allowed_hosts = ("127.0.0.1", "localhost")
    server.max_sse_clients = 32
    server.studio_live = True
    server.studio_edit = True
    server.studio_theme = "auto"
    thread = threading.Thread(target=server.serve_forever, name="lifecycle-http")
    thread.start()
    return server, thread, studio


def _finish_real_server(server, thread, studio) -> None:
    server.begin_terminal_http_shutdown()
    server.shutdown()
    studio.bus.close()
    server.server_close()
    thread.join(3)
    studio.close()
    assert not thread.is_alive()


def _presence_body(state: str) -> bytes:
    return (f'{{"actor":"agent","state":"{state}"}}').encode()


def test_idle_keep_alive_is_closed_and_shutdown_does_not_need_client_close(tmp_path: Path) -> None:
    _write_bundle(tmp_path)
    baseline_fds = len(os.listdir("/proc/self/fd"))
    baseline_threads = set(threading.enumerate())
    server, thread, studio = _real_server(tmp_path)
    client = http.client.HTTPConnection(*server.server_address, timeout=2)
    client.request("GET", "/__data/content.json")
    response = client.getresponse()
    assert response.status == 200
    response.read()

    closer = threading.Thread(
        target=_finish_real_server, args=(server, thread, studio), name="lifecycle-close",
    )
    closer.start()
    closer.join(3)

    assert not closer.is_alive()
    with pytest.raises((OSError, http.client.HTTPException)):
        client.request("GET", "/")
        client.getresponse()
    client.close()
    assert len(os.listdir("/proc/self/fd")) == baseline_fds
    assert set(threading.enumerate()) <= baseline_threads


def test_primary_close_cancels_queued_retrieval_joins_worker_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okf_loom.retrieval import RetrievalService

    _write_bundle(tmp_path)
    baseline_threads = set(threading.enumerate())
    server, thread, studio = _real_server(tmp_path)
    server.retrieval_concurrency = 1
    entered = threading.Event()
    release = threading.Event()
    original = RetrievalService.search

    def blocked(self, request):
        entered.set()
        release.wait(3)
        return original(self, request)

    monkeypatch.setattr(RetrievalService, "search", blocked)
    client = http.client.HTTPConnection(*server.server_address, timeout=3)
    request = threading.Thread(
        target=lambda: (
            client.request("GET", "/__search?q=Body&format=json"),
            client.getresponse().read(),
        )
    )
    request.start()
    assert entered.wait(2)
    executor = server._retrieval_executor
    queued = executor.submit(lambda: "must-not-run")
    closer = threading.Thread(target=server.server_close)
    closer.start()
    time.sleep(0.05)
    assert closer.is_alive()
    assert queued.cancelled()
    release.set()
    closer.join(3)
    request.join(3)
    server.server_close()
    server.shutdown()
    thread.join(3)
    studio.bus.close()
    studio.close()
    client.close()
    assert executor._shutdown
    assert not closer.is_alive() and not request.is_alive() and not thread.is_alive()
    assert not any(
        worker.name.startswith("okf-retrieval")
        for worker in set(threading.enumerate()) - baseline_threads
    )


def test_keep_alive_mutation_after_terminal_fence_is_rejected_without_state_change(
    tmp_path: Path,
) -> None:
    _write_bundle(tmp_path)
    server, thread, studio = _real_server(tmp_path)
    client = http.client.HTTPConnection(*server.server_address, timeout=2)
    headers = {"X-OKF-Token": server.csrf_token, "Content-Type": "application/json"}
    client.request("POST", "/__presence", body=_presence_body("editing"), headers=headers)
    first = client.getresponse()
    assert first.status == 200
    first.read()
    before = studio.get_presence()

    server.begin_terminal_http_shutdown()
    try:
        client.request("POST", "/__presence", body=_presence_body("thinking"), headers=headers)
        rejected = client.getresponse()
        rejected.read()
        assert rejected.status == 503
        assert rejected.getheader("Connection") == "close"
    except (OSError, http.client.HTTPException):
        pass

    after = studio.get_presence()
    _finish_real_server(server, thread, studio)
    client.close()
    assert before["state"] == "editing"
    assert after["state"] == "editing"


def test_accept_registration_is_linearized_with_terminal_fence() -> None:
    for _ in range(20):
        server = _OwnedThreadingHTTPServer(("127.0.0.1", 0), OKFWikiHandler)
        accepted, peer = socket.socketpair()
        barrier = threading.Barrier(2)
        outcome: list[str] = []

        def register() -> None:
            barrier.wait()
            try:
                server._register_accepted_connection(accepted, ("local", 0))
                outcome.append("registered")
            except OSError:
                outcome.append("rejected")

        worker = threading.Thread(target=register)
        worker.start()
        barrier.wait()
        server.begin_terminal_http_shutdown()
        worker.join(2)
        assert outcome in (["registered"], ["rejected"])
        assert accepted.fileno() == -1
        peer.close()
        server.server_close()


def test_admitted_request_completes_before_connection_is_cancelled(tmp_path: Path) -> None:
    _write_bundle(tmp_path)
    server, thread, studio = _real_server(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    real_set_presence = studio.set_presence

    def blocked_set_presence(**kwargs):
        entered.set()
        assert release.wait(3)
        return real_set_presence(**kwargs)

    studio.set_presence = blocked_set_presence  # type: ignore[method-assign]
    result: list[int] = []

    def mutate() -> None:
        client = http.client.HTTPConnection(*server.server_address, timeout=3)
        client.request(
            "POST", "/__presence", body=_presence_body("thinking"),
            headers={"X-OKF-Token": server.csrf_token, "Content-Type": "application/json"},
        )
        response = client.getresponse()
        result.append(response.status)
        response.read()
        client.close()

    worker = threading.Thread(target=mutate)
    worker.start()
    assert entered.wait(2)
    server.begin_terminal_http_shutdown()
    release.set()
    worker.join(3)
    final_presence = studio.get_presence()
    _finish_real_server(server, thread, studio)
    assert result == [200]
    assert final_presence["state"] == "thinking"


def test_partial_content_length_is_cancelled_by_shutdown_without_mutation(
    tmp_path: Path,
) -> None:
    _write_bundle(tmp_path)
    server, thread, studio = _real_server(tmp_path)
    before = studio.get_presence()
    client = socket.create_connection(server.server_address, timeout=2)
    body = _presence_body("thinking")
    client.sendall(
        b"POST /__presence HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        + f"X-OKF-Token: {server.csrf_token}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body[:3]
    )
    time.sleep(0.05)

    closer = threading.Thread(
        target=_finish_real_server, args=(server, thread, studio), name="partial-close",
    )
    closer.start()
    closer.join(3)

    assert not closer.is_alive()
    assert before == studio.get_presence()
    client.close()


def test_partial_reanchor_shutdown_does_not_consume_rate_quota(tmp_path: Path) -> None:
    _write_bundle(tmp_path)
    server, thread, studio = _real_server(tmp_path)
    before = list(getattr(server, "_reanchor_rate_attempts", []))
    client = socket.create_connection(server.server_address, timeout=2)
    client.sendall(
        b"POST /__comment-reanchor HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        + f"X-OKF-Token: {server.csrf_token}\r\n".encode()
        + b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{}"
    )
    time.sleep(0.05)
    closer = threading.Thread(
        target=_finish_real_server, args=(server, thread, studio),
        name="partial-reanchor-close",
    )
    closer.start()
    closer.join(3)
    assert not closer.is_alive()
    assert getattr(server, "_reanchor_rate_attempts", []) == before
    client.close()


def test_slow_complete_body_within_deadline_is_admitted(tmp_path: Path) -> None:
    _write_bundle(tmp_path)
    server, thread, studio = _real_server(tmp_path)
    server.request_body_deadline_s = 1.0
    client = socket.create_connection(server.server_address, timeout=2)
    body = _presence_body("thinking")
    headers = (
        "POST /__presence HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"X-OKF-Token: {server.csrf_token}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode()
    client.sendall(headers + body[:4])
    time.sleep(0.05)
    client.sendall(body[4:])
    response = client.makefile("rb").read()

    assert b"HTTP/1.1 200" in response
    assert studio.get_presence()["state"] == "thinking"
    client.close()
    _finish_real_server(server, thread, studio)


def test_request_body_deadline_returns_typed_timeout_without_mutation(tmp_path: Path) -> None:
    _write_bundle(tmp_path)
    server, thread, studio = _real_server(tmp_path)
    server.request_body_deadline_s = 0.05
    before = studio.get_presence()
    client = socket.create_connection(server.server_address, timeout=2)
    client.sendall(
        b"POST /__presence HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        + f"X-OKF-Token: {server.csrf_token}\r\n".encode()
        + b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{}"
    )
    response = client.makefile("rb").read()
    payload = json.loads(response.split(b"\r\n\r\n", 1)[1])

    assert b"HTTP/1.1 408" in response
    assert payload["error"]["code"] == "request_body_timeout"
    assert studio.get_presence() == before
    client.close()
    _finish_real_server(server, thread, studio)


def test_unauthorized_incomplete_mutation_is_rejected_before_body_read(tmp_path: Path) -> None:
    _write_bundle(tmp_path)
    server, thread, studio = _real_server(tmp_path)
    server.request_body_deadline_s = 1.0
    before = studio.get_presence()
    client = socket.create_connection(server.server_address, timeout=0.3)
    client.sendall(
        b"POST /__presence HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"X-OKF-Token: wrong\r\nContent-Type: application/json\r\n"
        b"Content-Length: 100\r\nConnection: close\r\n\r\n{}"
    )
    response = client.makefile("rb").read()

    assert b"HTTP/1.1 403" in response
    assert b"request_body_timeout" not in response
    assert studio.get_presence() == before
    client.close()
    _finish_real_server(server, thread, studio)


@pytest.mark.parametrize(
    ("studio_present", "studio_edit", "status", "body"),
    [
        (True, False, 403, b"Read-only kiosk"),
        (False, True, 503, b"Studio not enabled"),
    ],
)
def test_ordinary_unavailable_or_read_only_mutation_is_rejected_before_body_read(
    tmp_path: Path, studio_present: bool, studio_edit: bool, status: int, body: bytes,
) -> None:
    _write_bundle(tmp_path)
    server, thread, studio = _real_server(tmp_path)
    server.request_body_deadline_s = 1.0
    server.studio_edit = studio_edit
    if not studio_present:
        server.studio = None
    before = studio.get_presence()
    client = socket.create_connection(server.server_address, timeout=0.3)
    client.sendall(
        b"POST /__presence HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        + f"X-OKF-Token: {server.csrf_token}\r\n".encode()
        + b"Content-Type: application/json\r\nContent-Length: 100\r\n"
          b"Connection: close\r\n\r\n{}"
    )
    response = client.makefile("rb").read()
    assert f"HTTP/1.1 {status}".encode() in response
    assert body in response
    assert b"request_body_timeout" not in response
    assert studio.get_presence() == before
    client.close()
    server.studio = studio
    _finish_real_server(server, thread, studio)


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_unknown_local_unsafe_method_returns_501_before_body_read(
    tmp_path: Path, method: str,
) -> None:
    _write_bundle(tmp_path)
    server, thread, studio = _real_server(tmp_path)
    client = socket.create_connection(server.server_address, timeout=0.3)
    client.sendall(
        f"{method} /ordinary HTTP/1.1\r\nHost: 127.0.0.1\r\n".encode()
        + b"Content-Length: 100\r\nConnection: close\r\n\r\n{}"
    )
    response = client.makefile("rb").read()
    assert b"HTTP/1.1 501" in response
    assert f"Unsupported method ('{method}')".encode() in response
    client.close()
    _finish_real_server(server, thread, studio)


def test_route_specific_oversize_is_rejected_before_body_read(tmp_path: Path) -> None:
    _write_bundle(tmp_path)
    server, thread, studio = _real_server(tmp_path)
    client = socket.create_connection(server.server_address, timeout=0.3)
    client.sendall(
        b"POST /__comment-reanchor HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        + f"X-OKF-Token: {server.csrf_token}\r\n".encode()
        + b"Content-Type: application/json\r\nContent-Length: 65537\r\n"
          b"Connection: close\r\n\r\n{}"
    )
    response = client.makefile("rb").read()
    payload = json.loads(response.split(b"\r\n\r\n", 1)[1])

    assert b"HTTP/1.1 413" in response
    assert payload["error"]["code"] == "invalid_request"
    client.close()
    _finish_real_server(server, thread, studio)


def test_half_closed_incomplete_body_returns_exact_400_and_no_mutation(tmp_path: Path) -> None:
    _write_bundle(tmp_path)
    server, thread, studio = _real_server(tmp_path)
    before = studio.get_presence()
    client = socket.create_connection(server.server_address, timeout=1)
    client.sendall(
        b"POST /__presence HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        + f"X-OKF-Token: {server.csrf_token}\r\n".encode()
        + b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{}"
    )
    client.shutdown(socket.SHUT_WR)
    response = client.makefile("rb").read()
    payload = json.loads(response.split(b"\r\n\r\n", 1)[1])

    assert b"HTTP/1.1 400" in response
    assert b"Connection: close" in response
    assert payload["error"]["code"] == "incomplete_request_body"
    assert studio.get_presence() == before
    client.close()
    _finish_real_server(server, thread, studio)


def test_sse_and_repeated_terminal_shutdown_release_all_workers(tmp_path: Path) -> None:
    _write_bundle(tmp_path)
    baseline_threads = set(threading.enumerate())
    server, thread, studio = _real_server(tmp_path)
    client = http.client.HTTPConnection(*server.server_address, timeout=2)
    client.request("GET", "/__events")
    response = client.getresponse()
    assert response.status == 200
    assert response.readline().startswith(b"event: ready")

    assert server.begin_terminal_http_shutdown() is True
    assert server.begin_terminal_http_shutdown() is False
    server.shutdown()
    server.shutdown()
    studio.bus.close()
    server.server_close()
    server.server_close()
    thread.join(3)
    client.close()
    studio.close()

    assert not thread.is_alive()
    assert set(threading.enumerate()) <= baseline_threads


class _StubServer:
    instances: list["_StubServer"] = []
    serve_hook = staticmethod(lambda _server: None)

    def __init__(self, address, _handler):
        self.server_address = address
        self.__class__.instances.append(self)

    def serve_forever(self):
        self.__class__.serve_hook(self)

    def shutdown(self):
        return None

    def server_close(self):
        return None


class _CallbackOwner:
    """One admitted watcher callback under run_server lifecycle ownership."""

    instances: list["_CallbackOwner"] = []
    callback_kind = "reload"
    mutate = staticmethod(lambda: None)

    def __init__(self, _root, reload_fn, *, interval, tick_fn=None):
        self._callback = reload_fn if self.callback_kind == "reload" else tick_fn
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"blocked-{self.callback_kind}")
        self.__class__.instances.append(self)

    def _run(self):
        self.__class__.mutate()
        if not self._stop_event.is_set():
            self._callback()

    def start(self):
        self._thread.start()

    def request_stop(self):
        self._stop_event.set()

    def stop(self):
        self.request_stop()

    def join(self):
        self._thread.join()

    @property
    def ident(self):
        return self._thread.ident

    def is_alive(self):
        return self._thread.is_alive()


@pytest.mark.parametrize("kind", ["reload", "tick", "presence"])
@pytest.mark.parametrize("close_preexisting", [False, True])
def test_run_server_waits_for_admitted_studio_callback_before_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, kind: str,
    close_preexisting: bool, request: pytest.FixtureRequest,
) -> None:
    """Admitted callback completes; run_server returns only after its owner exits."""
    import okf_loom.server as server_module

    _write_bundle(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    closed = threading.Event()
    errors: list[BaseException] = []
    publications: list[str] = []
    # An unrelated owner may finish during this invocation (e.g. a delayed
    # finalizer from another test). Its cleanup must not affect this oracle.
    preexisting = (tmp_path / "unrelated.txt").open("w")
    request.addfinalizer(preexisting.close)
    baseline_threads = set(threading.enumerate())

    real_close = Studio.close
    real_fd_close = os.close
    descriptor_closes: list[tuple[int, int, int]] = []
    closure_checks = []

    def descriptor_identity(fd):
        info = os.fstat(fd)
        return fd, info.st_dev, info.st_ino

    def close_fd(fd):
        identity = descriptor_identity(fd)
        real_fd_close(fd)
        descriptor_closes.append(identity)

    monkeypatch.setattr(os, "close", close_fd)

    def close(self):
        if self.bundle_root.resolve() != tmp_path.resolve() or closed.is_set():
            return real_close(self)
        # Retain the owners so __del__ cannot conceal a missing explicit close.
        # This stub-server invocation owns directory authorities, not sockets.
        authorities = [self._bundle_authority, self.session_authority,
                       *self._session_descendants.values()]
        authorities = [authority for authority in authorities if authority is not None]
        expected = [descriptor_identity(fd) for authority in authorities
                    for fd in (authority.fd, authority.parent_fd)]
        start = len(descriptor_closes)
        closed.set()
        real_close(self)
        closure_checks.append((authorities, expected, descriptor_closes[start:]))

    monkeypatch.setattr(Studio, "close", close)
    _StubServer.instances = []
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", _StubServer)

    if kind in {"reload", "tick"}:
        owner_type = type("CallbackOwner", (_CallbackOwner,), {})
        owner_type.instances = []
        owner_type.callback_kind = kind
        owner_type.mutate = staticmethod(
            lambda: (tmp_path / "note.md").write_text(
                "---\ntype: note\n---\nChanged\n", encoding="utf-8"
            ) if kind == "reload" else None
        )
        monkeypatch.setattr(server_module, "_BundleWatcher", owner_type)
        if kind == "reload":
            original_emit = Studio.emit_change

            def blocked_emit(self, *, kind, **kwargs):
                if kind == "changed":
                    entered.set()
                    release.wait()
                    assert not closed.is_set()
                    publications.append(kind)
                    completed.set()
                return original_emit(self, kind=kind, **kwargs)

            monkeypatch.setattr(Studio, "emit_change", blocked_emit)
        else:
            original_tail = Studio.tail_and_broadcast_cross_process_events

            def blocked_tail(self):
                entered.set()
                release.wait()
                assert not closed.is_set()
                publications.append("tick")
                completed.set()
                return original_tail(self)

            monkeypatch.setattr(Studio, "tail_and_broadcast_cross_process_events", blocked_tail)
    else:
        monkeypatch.setattr(server_module, "_BundleWatcher", _CallbackOwner)
        original_sweep = Studio.sweep_stale_presence_and_claims

        def blocked_sweep(self):
            entered.set()
            release.wait()
            assert not closed.is_set()
            publications.append("presence")
            completed.set()
            return original_sweep(self)

        monkeypatch.setattr(Studio, "sweep_stale_presence_and_claims", blocked_sweep)
        real_sweeper = server_module._PresenceSweeper
        monkeypatch.setattr(
            server_module, "_PresenceSweeper",
            lambda studio: real_sweeper(studio, interval=0.001),
        )

    def serve_hook(_server):
        assert entered.wait(3), f"{kind} callback did not start"
        if close_preexisting:
            preexisting.close()

    _StubServer.serve_hook = staticmethod(serve_hook)

    def run():
        try:
            server_module.run_server(
                tmp_path, host="127.0.0.1", port=0,
                watch=kind in {"reload", "tick"}, open_browser=False,
            )
        except BaseException as exc:
            errors.append(exc)

    runner = threading.Thread(target=run, name=f"run-server-{kind}")
    runner.start()
    assert entered.wait(3)
    runner.join(0.05)
    assert runner.is_alive(), "run_server returned while callback still owned Studio"
    assert not closed.is_set()
    release.set()
    runner.join(5)

    assert not runner.is_alive()
    assert errors == []
    assert completed.is_set()
    assert publications == ["changed" if kind == "reload" else kind]
    assert closed.is_set()
    assert len(closure_checks) == 1
    authorities, expected, actual = closure_checks[0]
    assert expected, "must observe live invocation-owned descriptors before close"
    assert set(expected) <= set(actual), "Studio.close must close each owned kernel descriptor"
    assert all(authority.fd == authority.parent_fd == -1 for authority in authorities)
    if close_preexisting:
        assert preexisting.closed
    else:
        os.fstat(preexisting.fileno())  # Closing Studio must not close another owner.
    assert set(threading.enumerate()) <= baseline_threads
    assert "bundle session authority is closed" not in capsys.readouterr().err
    preexisting.close()


def test_bundle_watcher_stop_cancels_pending_callbacks_and_is_idempotent(tmp_path: Path) -> None:
    _write_bundle(tmp_path)
    calls: list[str] = []
    watcher = _BundleWatcher(
        tmp_path, lambda: calls.append("reload"), interval=60,
        tick_fn=lambda: calls.append("tick"),
    )
    watcher.start()
    watcher.stop()
    watcher.stop()
    watcher.join()
    assert calls == []
    assert not watcher.is_alive()


def test_watcher_callback_exception_is_reported_and_thread_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    _write_bundle(tmp_path)
    called = threading.Event()

    def failed_tick():
        called.set()
        raise RuntimeError("tick exploded")

    watcher = _BundleWatcher(tmp_path, lambda: None, interval=0.001, tick_fn=failed_tick)
    watcher.start()
    assert called.wait(2)
    watcher.stop()
    watcher.join()
    assert not watcher.is_alive()
    assert "watcher tick failed: tick exploded" in capsys.readouterr().err


def test_presence_sweeper_stop_cancels_pending_and_repeated_stop(tmp_path: Path) -> None:
    studio = Studio.for_bundle(tmp_path)
    called = threading.Event()
    studio.sweep_stale_presence_and_claims = lambda: called.set()  # type: ignore[method-assign]
    sweeper = _PresenceSweeper(studio, interval=60)
    sweeper.start()
    sweeper.stop()
    sweeper.stop()
    assert not called.is_set()
    assert not sweeper.is_alive()
    studio.close()


def test_studio_close_joins_blocked_callback_subscriber_without_late_publish(
    tmp_path: Path,
) -> None:
    studio = Studio.for_bundle(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def callback(_event):
        entered.set()
        assert release.wait(5)
        # Re-entrant late publication is fenced once close has begun.
        studio.bus.publish({"type": "late"})
        completed.set()

    sub = studio.bus.subscribe(callback)
    studio.bus.publish({"type": "admitted"})
    assert entered.wait(2)
    closer = threading.Thread(target=studio.close, name="studio-close")
    closer.start()
    closer.join(0.05)
    assert closer.is_alive()
    release.set()
    closer.join(5)
    assert completed.is_set()
    assert not closer.is_alive()
    assert not sub._thread.is_alive()
    studio.close()


ASYNC_LIFECYCLE_MATRIX = """
Scenario | Abort owner | Outcome | Late-result behavior | Proof
idle accepted keep-alive | HTTP connection owner | socket shutdown at terminal fence | next dispatch cannot enter Studio | real-server idle keep-alive test
request parsed after terminal fence | HTTP admission owner | 503 + Connection: close when writable, otherwise socket close | no mutation | second-presence test
request admitted before terminal fence | HTTP admission owner | ordinary response completes, then connection closes | no second dispatch | blocked active-request test
partial request body at terminal fence | HTTP transport owner | socket shutdown before application admission | no route mutation | raw partial-body shutdown test
pre-body authorization/path/cap refusal | HTTP boundary owner | existing typed/plain error + connection close | body is not read; no mutation | unauthorized and route-cap tests
pre-body availability/mode/unsupported-method refusal | HTTP boundary owner | existing 503/403/501 + connection close | body is not read; no mutation or worker hold | ordinary mutation and unsafe-method tests
slow complete request body | HTTP transport deadline | complete body admitted once | ordinary route result | slow-body test
request body deadline | HTTP transport owner | typed 408 + connection close | no route mutation | body-timeout test
request body EOF/half-close | HTTP transport owner | typed 400 incomplete_request_body + connection close | no route mutation | half-close test
re-anchor timeout/EOF/shutdown before complete body | HTTP transport owner | typed timeout/EOF or socket cancellation | rate quota unchanged | re-anchor incomplete-body and shutdown tests
accept concurrent with fence | HTTP connection owner lock | registered-and-closed or rejected-and-closed | no unowned socket | accept/fence race test
SSE admitted before fence | Studio bus + HTTP connection owner | bus close cancels stream and worker joins | no late event work | real-server SSE test
pending watcher/presence callback | run_server worker stop event | cancelled before Studio entry | no publication | pending-cancellation tests
admitted reload/tick/presence callback | run_server finally | ordinary callback completion or existing logged failure | joined before Studio.close and terminal return | deterministic blocked callback tests
callback exception | worker callback boundary | logged and worker remains stoppable | no unhandled late exception | callback-exception test
repeated stop | worker owner | idempotent terminal stop | no new callback | repeated-stop tests
EventBus callback during Studio close | Studio/EventBus | admitted callback completes | re-entrant publication fenced; subscriber joined | blocked callback close test
repeated HTTP shutdown | HTTP server owner | idempotent fence/shutdown/server_close | no surviving socket or worker | repeated-terminal test
"""


API_PROTOCOL_MATRIX = """
Operation | Mutation | Admission rule | Terminal result | Proof
HTTP request dispatch (local/admin/public) | route-dependent | atomically admitted before route code | 503 + close when possible, otherwise connection close | keep-alive mutation test
POST pre-body admission | none | listener/path/auth/header/effective cap decided before transport read | existing refusal or validated admission object | unauthorized/public/cap tests
ordinary mutation mode/availability and unsupported unsafe methods | none | side-effect-free pre-body decision | immediate existing 403/503/501; body unread | lifecycle raw-request tests
re-anchor rate admission | rate quota | preflight only observes; complete parsed request consumes atomically immediately before dispatch | exact 429 when exhausted | timeout/EOF/shutdown/complete/concurrency tests
admitted in-flight mutation | yes | admission remains owned through response flush | completes normally; connection then closes | blocked active-request test
SSE stream | no | admitted once before subscription loop | cancelled by bus close during shutdown | SSE cleanup test
"""
