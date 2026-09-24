from __future__ import annotations

import pytest
import json
import os
import stat
import shutil
import threading
import gc
from types import SimpleNamespace

from okf_loom.cli import build_parser, main
from okf_loom.proposal_review.activation import (ActivationError, CAPABILITY_ID,
                                                 activate, activate_http, capability,
                                                 validate_server_activation)
from okf_loom.proposal_review import schemas
from okf_loom.proposal_review.errors import RepositoryError
from okf_loom.server import OKFWikiHandler


def _forbid_directory_chmod(monkeypatch):
    chmod, fchmod = os.chmod, os.fchmod

    def checked_chmod(path, *args, **kwargs):
        info = (os.fstat(path) if isinstance(path, int) else
                os.stat(path, dir_fd=kwargs.get("dir_fd"), follow_symlinks=False))
        assert not stat.S_ISDIR(info.st_mode), "activation must not chmod directories"
        return chmod(path, *args, **kwargs)

    def checked_fchmod(fd, *args, **kwargs):
        assert not stat.S_ISDIR(os.fstat(fd).st_mode), "activation must not fchmod directories"
        return fchmod(fd, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", checked_chmod)
    monkeypatch.setattr(os, "fchmod", checked_fchmod)


def test_capability_is_inactive_until_explicit_embedding_activation() -> None:
    assert capability() == {
        "id": CAPABILITY_ID, "status": "experimental", "active": False,
        "protocol_version": 1, "operation_schema_version": 1,
        "direct_mode_default": True, "inactive_reason": "not_enabled",
    }
    assert capability(True)["active"] is True
    assert capability(True)["inactive_reason"] is None


@pytest.mark.parametrize("values", [
    {"host": "0.0.0.0"}, {"host": "127.0.0.1", "public": True},
    {"host": "127.0.0.1", "tunnel": True},
])
def test_editable_activation_rejects_public_exposure(values) -> None:
    with pytest.raises(ActivationError) as error:
        validate_server_activation(enabled=True, no_edit=False, **values)
    assert error.value.code == "proposal_review_requires_loopback"
    validate_server_activation(enabled=True, no_edit=True, **values)


def test_parser_registers_only_bundle_three_proposal_commands() -> None:
    parser = build_parser()
    for command in ("create", "list", "show", "decide", "withdraw"):
        with pytest.raises(SystemExit) as parsed:
            parser.parse_args(["proposal", command, "--help"])
        assert parsed.value.code == 0
    for absent in ("apply", "undo"):
        with pytest.raises(SystemExit):
            parser.parse_args(["proposal", absent])


def test_http_activation_rotates_private_proposal_token(tmp_path) -> None:
    (tmp_path / "a.md").write_text("---\ntype: Note\n---\n\nA\n")
    first = activate_http(tmp_path)
    token_path = tmp_path / ".okf-loom/session/.proposal-http-token"
    assert len(first.proposal_token) == 64
    assert token_path.read_text() == first.proposal_token
    assert (token_path.stat().st_mode & 0o777) == 0o600
    second = activate_http(tmp_path)
    assert second.proposal_token != first.proposal_token
    first.close(); second.close()


@pytest.mark.parametrize("entrypoint", [activate, activate_http])
@pytest.mark.parametrize("mask", [0o022, 0o077])
def test_public_activation_creates_private_repository_under_setgid_parent(
    tmp_path, monkeypatch, entrypoint, mask,
) -> None:
    monkeypatch.delenv("OKF_LOOM_SESSION_ROOT", raising=False)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    # Prefer a supplementary group to prove group inheritance is not an egid check.
    inherited_gid = next((gid for gid in os.getgroups() if gid != os.getegid()), os.getegid())
    os.chown(bundle, -1, inherited_gid)
    bundle.chmod(0o2755)
    (bundle / "a.md").write_text("---\ntype: Note\n---\n\nA\n")
    credentials = (os.geteuid(), os.getegid(), os.getgroups())
    _forbid_directory_chmod(monkeypatch)
    baseline = len(os.listdir("/proc/self/fd"))
    old_mask = os.umask(mask)
    try:
        activation = entrypoint(bundle)
        try:
            session = bundle / ".okf-loom/session"
            repository = session / "proposal-review-v1"
            assert stat.S_IMODE(bundle.stat().st_mode) == 0o2755
            for ancestor in (session.parent, session):
                assert stat.S_IMODE(ancestor.stat().st_mode) == 0o2000 | (0o777 & ~mask)
            for path in [repository, *repository.rglob("*")]:
                info = path.lstat()
                assert info.st_uid == os.geteuid()
                assert info.st_gid == inherited_gid
                if stat.S_ISDIR(info.st_mode):
                    assert stat.S_IMODE(info.st_mode) == 0o2700
                else:
                    assert stat.S_ISREG(info.st_mode)
                    assert stat.S_IMODE(info.st_mode) == 0o600
                    assert info.st_nlink == 1
            assert list(repository.iterdir())  # real repository bootstrap completed
            if entrypoint is activate_http:
                token = session / ".proposal-http-token"
                assert stat.S_IMODE(token.stat().st_mode) == 0o600
                assert token.stat().st_gid == inherited_gid
                assert token.stat().st_uid == os.geteuid() and token.stat().st_nlink == 1
                assert token.read_text() == activation.proposal_token
        finally:
            activation.close()
        # Reopening admits the now-existing private repository without repairs.
        entrypoint(bundle).close()
        assert len(os.listdir("/proc/self/fd")) == baseline
        assert (os.geteuid(), os.getegid(), os.getgroups()) == credentials
    finally:
        os.umask(old_mask)


@pytest.mark.parametrize("entrypoint", [activate, activate_http])
@pytest.mark.parametrize("mode", [0o700, 0o2700])
def test_public_activation_reopens_existing_private_repository_under_setgid_parent(
    tmp_path, monkeypatch, entrypoint, mode,
) -> None:
    monkeypatch.delenv("OKF_LOOM_SESSION_ROOT", raising=False)
    session = tmp_path / ".okf-loom/session"
    session.mkdir(parents=True)
    session.chmod(0o2755)
    repository = session / "proposal-review-v1"
    repository.mkdir(mode=0o700)
    # Explicit fixture preparation, not a runtime permission repair.
    repository.chmod(mode)
    identity = repository.stat().st_ino
    _forbid_directory_chmod(monkeypatch)
    for _ in range(2):
        result = entrypoint(tmp_path)
        try:
            assert repository.stat().st_ino == identity
            assert stat.S_IMODE(session.stat().st_mode) == 0o2755
            for path in [repository, *repository.rglob("*")]:
                if path.is_dir():
                    assert stat.S_IMODE(path.stat().st_mode) == mode
            if entrypoint is activate_http:
                token = session / ".proposal-http-token"
                assert stat.S_IMODE(token.stat().st_mode) == 0o600
                assert token.read_text() == result.proposal_token
        finally:
            result.close()


@pytest.mark.parametrize("entrypoint", [activate, activate_http])
@pytest.mark.parametrize("mode", [0o2700, 0o2750])
def test_activation_admits_only_safe_state_substituted_before_first_stat(
    tmp_path, monkeypatch, entrypoint, mode,
) -> None:
    monkeypatch.delenv("OKF_LOOM_SESSION_ROOT", raising=False)
    session = tmp_path / ".okf-loom/session"
    session.mkdir(parents=True)
    session.chmod(0o2755)
    repository = session / "proposal-review-v1"
    retained = session / "retained"
    replacement = tmp_path / "preexisting"
    replacement.mkdir()
    replacement.chmod(mode)
    (replacement / "sentinel").write_bytes(b"unchanged")
    (replacement / "sentinel").chmod(0o600)
    replacement_identity = replacement.stat().st_ino
    mkdir = os.mkdir
    swapped = False

    def after_mkdir(path, *args, **kwargs):
        nonlocal swapped
        result = mkdir(path, *args, **kwargs)
        if path == "proposal-review-v1" and not swapped:
            # The real syscall succeeded; no runtime stat/open has happened.
            swapped = True
            repository.rename(retained)
            replacement.rename(repository)
        return result

    monkeypatch.setattr(os, "mkdir", after_mkdir)
    _forbid_directory_chmod(monkeypatch)
    baseline = len(os.listdir("/proc/self/fd"))
    if mode == 0o2700:
        # Reopenable caller-owned state is admitted, not claimed as our creation.
        entrypoint(tmp_path).close()
        entrypoint(tmp_path).close()
        assert (repository / "repository.json").is_file()
    else:
        with pytest.raises(ActivationError) as error:
            entrypoint(tmp_path)
        assert error.value.code == "storage_unsafe"
        assert sorted(path.name for path in repository.iterdir()) == ["sentinel"]
        assert not (session / ".proposal-http-token").exists()
    assert swapped
    assert repository.stat().st_ino == replacement_identity
    assert stat.S_IMODE(repository.stat().st_mode) == mode
    assert (repository / "sentinel").read_bytes() == b"unchanged"
    assert list(retained.iterdir()) == []
    assert stat.S_IMODE(retained.stat().st_mode) == 0o2700
    assert len(os.listdir("/proc/self/fd")) == baseline


@pytest.mark.parametrize("mode", [0o755, 0o777, 0o2750, 0o2701, 0o2755, 0o1700, 0o4700, 0o6700])
@pytest.mark.parametrize("raced_creation", [False, True])
def test_activation_never_repairs_existing_unsafe_repository(
    tmp_path, monkeypatch, mode, raced_creation,
) -> None:
    monkeypatch.delenv("OKF_LOOM_SESSION_ROOT", raising=False)
    session = tmp_path / ".okf-loom/session"
    session.mkdir(parents=True)
    repository = session / "proposal-review-v1"
    mkdir = os.mkdir

    def seed():
        mkdir(repository)
        repository.chmod(mode)
        (repository / "sentinel").write_bytes(b"unchanged")

    if raced_creation:
        def race(path, *args, **kwargs):
            if path == "proposal-review-v1":
                seed()  # another creator wins; mkdir must observe EEXIST
            return mkdir(path, *args, **kwargs)
        monkeypatch.setattr(os, "mkdir", race)
    else:
        seed()
    baseline = len(os.listdir("/proc/self/fd"))
    with pytest.raises(ActivationError) as error:
        activate_http(tmp_path)
    assert error.value.code == "storage_unsafe"
    assert stat.S_IMODE(repository.stat().st_mode) == mode
    assert (repository / "sentinel").read_bytes() == b"unchanged"
    assert sorted(path.name for path in repository.iterdir()) == ["sentinel"]
    assert not (session / ".proposal-http-token").exists()
    # Even while the exception traceback is retained, descriptors are closed.
    assert len(os.listdir("/proc/self/fd")) == baseline


def test_activation_never_repairs_symlinked_repository(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OKF_LOOM_SESSION_ROOT", raising=False)
    session = tmp_path / ".okf-loom/session"
    session.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o2700)
    (outside / "sentinel").write_bytes(b"unchanged")
    (session / "proposal-review-v1").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ActivationError) as error:
        activate_http(tmp_path)
    assert error.value.code == "storage_unsafe"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o2700
    assert (outside / "sentinel").read_bytes() == b"unchanged"
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]
    assert not (session / ".proposal-http-token").exists()


@pytest.mark.parametrize("seam", ["before_open", "after_open"])
@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_activation_rejects_repository_substitution_during_admission(
    tmp_path, monkeypatch, seam, replacement,
) -> None:
    monkeypatch.delenv("OKF_LOOM_SESSION_ROOT", raising=False)
    session = tmp_path / ".okf-loom/session"
    session.mkdir(parents=True)
    session.chmod(0o755)
    repository = session / "proposal-review-v1"
    retained = session / "retained"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o2700)
    (outside / "sentinel").write_bytes(b"unchanged")
    opened = os.open
    swapped = False

    def substitute():
        nonlocal swapped
        assert not swapped
        swapped = True
        repository.rename(retained)
        if replacement == "directory":
            repository.mkdir()
            repository.chmod(0o2700)
        else:
            repository.symlink_to(outside, target_is_directory=True)

    def at_open(path, *args, **kwargs):
        target = path == "proposal-review-v1" and not swapped
        if target and seam == "before_open":
            substitute()
        fd = opened(path, *args, **kwargs)
        if target and seam == "after_open":
            substitute()
        return fd

    monkeypatch.setattr(os, "open", at_open)
    baseline = len(os.listdir("/proc/self/fd"))
    with pytest.raises(ActivationError) as error:
        activate_http(tmp_path)
    assert error.value.code == "storage_unsafe"
    assert swapped
    assert stat.S_IMODE(repository.stat().st_mode) == 0o2700
    assert stat.S_IMODE(retained.stat().st_mode) == 0o700
    assert list(retained.iterdir()) == []
    assert (outside / "sentinel").read_bytes() == b"unchanged"
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]
    assert not (session / ".proposal-http-token").exists()
    assert len(os.listdir("/proc/self/fd")) == baseline


@pytest.mark.parametrize("existing", [False, True])
def test_activation_never_normalizes_wrong_owner_repository(
    tmp_path, monkeypatch, existing,
) -> None:
    monkeypatch.delenv("OKF_LOOM_SESSION_ROOT", raising=False)
    session = tmp_path / ".okf-loom/session"
    session.mkdir(parents=True)
    session.chmod(0o2755)
    repository = session / "proposal-review-v1"
    if existing:
        repository.mkdir(mode=0o700)
    fstat, path_stat = os.fstat, os.stat

    def wrong_owner(info):
        if stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o2700:
            fields = list(info)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, "fstat", lambda fd: wrong_owner(fstat(fd)))
    monkeypatch.setattr(os, "stat", lambda *a, **kw: wrong_owner(path_stat(*a, **kw)))
    def unexpected_chmod(*args):
        pytest.fail("wrong-owner directory must not be chmodded")
    monkeypatch.setattr(os, "fchmod", unexpected_chmod)
    with pytest.raises(ActivationError) as error:
        activate_http(tmp_path)
    assert error.value.code == "storage_unsafe"
    assert stat.S_IMODE(path_stat(repository).st_mode) == 0o2700
    assert list(repository.iterdir()) == []
    assert not (session / ".proposal-http-token").exists()


@pytest.mark.parametrize("mode", [0o700, 0o2700])
def test_http_activation_repository_and_token_use_only_external_session_authority(
    tmp_path, monkeypatch, mode,
) -> None:
    bundle = tmp_path / "bundle"; bundle.mkdir(mode=0o700)
    (bundle / "a.md").write_text("---\ntype: Note\n---\n\nA\n")
    local = bundle / ".okf-loom/session"; local.mkdir(parents=True)
    sentinel = local / ".proposal-http-token"; sentinel.write_text("bundle-local-sentinel")
    before = {path.relative_to(local): path.read_bytes() for path in local.rglob("*") if path.is_file()}
    external = tmp_path / "external"; external.mkdir(mode=0o700)
    external.chmod(mode)
    tmp_path.chmod(mode)
    monkeypatch.setenv("OKF_LOOM_SESSION_TRUSTED_ROOT", str(tmp_path))
    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(external))
    _forbid_directory_chmod(monkeypatch)

    activation = activate_http(bundle)
    try:
        external_token = external / ".proposal-http-token"
        assert external_token.read_text() == activation.proposal_token
        assert (external_token.stat().st_mode & 0o777) == 0o600
        assert (external / "proposal-review-v1").is_dir()
        assert all(activation.proposal_token.encode() not in value for value in before.values())
        assert sentinel.read_text() == "bundle-local-sentinel"
        assert before == {path.relative_to(local): path.read_bytes()
                          for path in local.rglob("*") if path.is_file()}
    finally:
        activation.close()


def test_proposal_activation_ignores_mutated_legacy_authority_anchor(
    tmp_path, monkeypatch,
) -> None:
    from okf_loom import studio as studio_module

    bundle = tmp_path / "bundle"; bundle.mkdir(mode=0o700)
    (bundle / "a.md").write_text("---\ntype: Note\n---\n\nA\n")
    external = tmp_path / "external"; external.mkdir(mode=0o700)
    outside = tmp_path / "outside"; outside.mkdir(mode=0o700)
    tmp_path.chmod(0o700)
    monkeypatch.setenv("OKF_LOOM_SESSION_TRUSTED_ROOT", str(tmp_path))
    monkeypatch.setenv("OKF_LOOM_SESSION_ROOT", str(external))
    resolve = studio_module._configured_session_root
    (external / ".okf-authority-anchor").mkdir(mode=0o700)

    def resolve_then_replace_anchor(*args, **kwargs):
        authority = resolve(*args, **kwargs)
        anchor = external / ".okf-authority-anchor"
        anchor.rmdir()
        anchor.symlink_to(outside, target_is_directory=True)
        return authority

    monkeypatch.setattr(studio_module, "_configured_session_root", resolve_then_replace_anchor)
    baseline = len(os.listdir("/proc/self/fd"))
    activation = activate_http(bundle)
    try:
        assert (external / ".proposal-http-token").read_text() == activation.proposal_token
        assert (external / "proposal-review-v1").is_dir()
        assert list(outside.iterdir()) == []
    finally:
        activation.close()
    assert len(os.listdir("/proc/self/fd")) == baseline


@pytest.mark.parametrize("rows,identity,code", [
    ([{"id": "same", "summary": "a", "author": "agent", "created_at": "2026-07-15T00:00:00.000Z"},
      {"id": "same", "summary": "b", "author": "agent", "created_at": "2026-07-15T00:00:01.000Z"}],
     {"legacy_id": "same", "source_generation": 0}, "legacy_identity_conflict"),
    ([{"id": "bad", "unknown": True}],
     {"legacy_id": "bad", "source_generation": 0}, "storage_corrupt"),
    ([{"id": "kept", "summary": "a", "author": "agent", "created_at": "2026-07-15T00:00:00.000Z"}],
     {"legacy_id": "missing", "source_generation": 0}, "legacy_identity_conflict"),
    ([{"id": "kept", "summary": "a", "author": "agent", "created_at": "2026-07-15T00:00:00.000Z"}],
     {"legacy_id": "kept", "source_generation": 1}, "legacy_identity_conflict"),
])
def test_legacy_duplicate_malformed_stale_fail_without_mutation(tmp_path, rows, identity, code) -> None:
    (tmp_path / "a.md").write_text("---\ntype: Note\n---\n\nA\n")
    service = activate(tmp_path)
    path = tmp_path / ".okf-loom/session/proposals.jsonl"
    path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(RepositoryError) as error:
        service.legacy_reader.verify(schemas.LegacyIdentity.from_mapping(identity))
    assert error.value.code == code
    assert path.read_bytes() == before
    service.close()


def test_cmd_serve_inactive_active_loopback_and_public_no_edit(tmp_path, monkeypatch) -> None:
    (tmp_path / "a.md").write_text("---\ntype: Note\n---\n\nA\n")
    calls = []
    monkeypatch.setattr("okf_loom.server.run_server", lambda *args, **kwargs: calls.append(kwargs))
    assert main(["serve", str(tmp_path), "--no-open", "--no-watch"]) == 0
    assert calls[-1]["enable_proposal_review"] is False
    assert calls[-1]["tunnel_public_edit"] is False
    assert main(["serve", str(tmp_path), "--allow-tunnel-edit",
                 "--no-open", "--no-watch"]) == 2
    assert calls[-1]["tunnel_public_edit"] is False
    assert main(["serve", str(tmp_path), "--tunnel", "--allow-tunnel-edit",
                 "--no-open", "--no-watch"]) == 0
    assert calls[-1]["tunnel"] is True and calls[-1]["tunnel_public_edit"] is True
    assert main(["serve", str(tmp_path), "--enable-proposal-review",
                 "--no-open", "--no-watch"]) == 0
    assert calls[-1]["enable_proposal_review"] is True and calls[-1]["host"] == "127.0.0.1"
    assert main(["serve", str(tmp_path), "--enable-proposal-review", "--public",
                 "--public-ack", "--no-edit", "--no-open", "--no-watch"]) == 0
    assert calls[-1]["host"] == "0.0.0.0" and calls[-1]["studio_edit"] is False


def test_dynamic_tunnel_attach_rejects_editable_active_session() -> None:
    handler = object.__new__(OKFWikiHandler)
    handler.server = SimpleNamespace(proposal_service=object(), studio_edit=True,
                                     _tunnel_lock=threading.Lock(), tunnel_url=None)
    captured = {}
    handler._send_json = lambda status, body, **kwargs: captured.update(status=status, body=body)
    handler._handle_tunnel({"action": "start"})
    assert captured["status"] == 409
    assert captured["body"]["error"]["code"] == "proposal_review_requires_loopback"


def test_activation_rejects_unsafe_bundle_root(tmp_path) -> None:
    real = tmp_path / "real"; real.mkdir()
    (real / "a.md").write_text("---\ntype: Note\n---\n\nA\n")
    link = tmp_path / "linked"; link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ActivationError) as error:
        activate_http(link)
    assert error.value.code == "storage_unsafe"


def test_run_server_real_activation_inactive_and_active_loopback(tmp_path, monkeypatch) -> None:
    import okf_loom.server as server_module
    (tmp_path / "a.md").write_text("---\ntype: Note\n---\n\nA\n")
    real_type = server_module.ThreadingHTTPServer
    instances = []
    class OneShot(real_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); instances.append(self)
        def serve_forever(self, *args, **kwargs): return None
        def shutdown(self): return None
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", OneShot)
    server_module.run_server(tmp_path, host="127.0.0.1", port=0,
                             watch=False, open_browser=False)
    assert instances[-1].proposal_service is None and instances[-1].proposal_token is None
    server_module.run_server(tmp_path, host="127.0.0.1", port=0,
                             watch=False, open_browser=False,
                             enable_proposal_review=True)
    assert instances[-1].proposal_service is not None
    assert len(instances[-1].proposal_token) == 64


def test_run_server_real_public_no_edit_activation(tmp_path, monkeypatch) -> None:
    import okf_loom.server as server_module
    (tmp_path / "a.md").write_text("---\ntype: Note\n---\n\nA\n")
    real_type = server_module.ThreadingHTTPServer
    instances = []
    class OneShot(real_type):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); instances.append(self)
        def serve_forever(self, *args, **kwargs): return None
        def shutdown(self): return None
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", OneShot)
    server_module.run_server(tmp_path, host="0.0.0.0", port=0,
                             watch=False, open_browser=False, studio_edit=False,
                             enable_proposal_review=True)
    assert instances[-1].proposal_service is not None
    assert instances[-1].studio_edit is False and instances[-1].proposal_public_exposure


def test_startup_transaction_recovery_precedes_service_availability(tmp_path) -> None:
    from test_proposal_crash import _killed, DiskActivity, DiskEvents
    from test_proposal_service import Clock
    from okf_loom.proposal_review.activation import _compose_transactions
    bundle, session, repository, coordinator, publication, request = _killed(tmp_path, "commit_decided")
    coordinator.close(); activity = DiskActivity(session)
    recovered = None
    try:
        recovered, publication = _compose_transactions(bundle, repository, Clock(), activity=activity,
            outbox=DiskEvents(session), repository_path=session / "proposal-review-v1")
        assert all(item.classification == "terminal" for item in repository.recovery_report.items)
        assert b"tags:" in (bundle / "tables/orders.md").read_bytes()
    finally:
        if recovered is not None: recovered.close()
        repository.close()


def test_startup_indeterminate_recovery_blocks_activation_with_typed_state(tmp_path) -> None:
    from test_proposal_crash import _killed, DiskActivity, DiskEvents
    from test_proposal_service import Clock
    from okf_loom.proposal_review.activation import _compose_transactions
    bundle, session, repository, coordinator, publication, request = _killed(tmp_path, "commit_decided")
    coordinator.close()
    try:
        transaction_id = next(item.transaction_id for item in repository.recovery_report.items)
        stage = next((session / f"proposal-review-v1/transactions/{transaction_id}/stage").iterdir())
        stage.unlink(); stage.write_bytes(b"substitution"); stage.chmod(0o600)
        with pytest.raises(RepositoryError) as blocked:
            _compose_transactions(bundle, repository, Clock(), activity=DiskActivity(session),
                outbox=DiskEvents(session), repository_path=session / "proposal-review-v1")
        assert blocked.value.code == "transaction_recovering"
        assert repository.read_wal(transaction_id).phase == "indeterminate"
    finally:
        repository.close()


def _proposal_threads() -> set[int | None]:
    return {thread.ident for thread in threading.enumerate()
            if thread.name.startswith(("proposal-", "okf-proposal-"))}


def test_direct_repeat_start_stop_closes_resources_and_releases_bundle(tmp_path) -> None:
    bundle = tmp_path / "bundle"; bundle.mkdir()
    (bundle / "a.md").write_text("---\ntype: Note\n---\n\nA\n")
    gc.collect()
    baseline_fds = len(os.listdir("/proc/self/fd"))
    baseline_threads = _proposal_threads()
    for _ in range(5):
        activation = activate_http(bundle)
        activation.close()
    gc.collect()
    assert len(os.listdir("/proc/self/fd")) <= baseline_fds
    assert _proposal_threads() == baseline_threads
    moved = tmp_path / "moved"
    bundle.rename(moved)
    shutil.rmtree(moved)
    assert not moved.exists()


@pytest.mark.parametrize("mode", [0o755, 0o2755])
def test_activation_failure_closes_resources_and_releases_bundle(tmp_path, monkeypatch, mode) -> None:
    bundle = tmp_path / "bundle"; bundle.mkdir()
    bundle.chmod(mode)
    (bundle / "a.md").write_text("---\ntype: Note\n---\n\nA\n")
    gc.collect()
    baseline_fds = len(os.listdir("/proc/self/fd"))
    baseline_threads = _proposal_threads()
    def fail_recovery(self):
        raise RuntimeError("activation recovery failure")
    monkeypatch.setattr(
        "okf_loom.proposal_review.transactions.ProposalTransactionCoordinator.recover_all",
        fail_recovery)
    for _ in range(5):
        with pytest.raises(RuntimeError, match="activation recovery failure"):
            activate_http(bundle)
    gc.collect()
    assert len(os.listdir("/proc/self/fd")) <= baseline_fds
    assert _proposal_threads() == baseline_threads
    moved = tmp_path / "moved"
    bundle.rename(moved)
    shutil.rmtree(moved)
    assert not moved.exists()


def test_server_repeat_start_stop_closes_activation_chain(tmp_path, monkeypatch) -> None:
    import okf_loom.server as server_module
    bundle = tmp_path / "bundle"; bundle.mkdir()
    (bundle / "a.md").write_text("---\ntype: Note\n---\n\nA\n")
    real_type = server_module.ThreadingHTTPServer
    class OneShot(real_type):
        def serve_forever(self, *args, **kwargs): return None
        def shutdown(self): return None
    monkeypatch.setattr(server_module, "ThreadingHTTPServer", OneShot)
    gc.collect()
    baseline_fds = len(os.listdir("/proc/self/fd"))
    baseline_threads = _proposal_threads()
    for _ in range(3):
        server_module.run_server(bundle, host="127.0.0.1", port=0,
                                 watch=False, open_browser=False,
                                 enable_proposal_review=True)
    gc.collect()
    assert len(os.listdir("/proc/self/fd")) <= baseline_fds
    assert _proposal_threads() == baseline_threads
    moved = tmp_path / "moved"
    bundle.rename(moved); shutil.rmtree(moved)
    assert not moved.exists()
