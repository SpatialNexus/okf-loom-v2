"""Exact owner-only setgid admission across session and proposal adapters."""
from __future__ import annotations

import os
import stat

import pytest

from okf_loom.session_authority import resolve_session_authority
from okf_loom.proposal_review.ports import LinuxDirFdFilesystem
from okf_loom.proposal_review.publication import TrustedPublicationAdapter
from okf_loom.proposal_review.errors import StorageSafetyError
from test_proposal_activation import _forbid_directory_chmod


@pytest.mark.parametrize("mode", [0o700, 0o2700])
def test_session_private_components_preserve_mode_and_pinned_authority(tmp_path, monkeypatch, mode):
    tmp_path.chmod(mode)
    baseline = len(os.listdir("/proc/self/fd"))
    _forbid_directory_chmod(monkeypatch)
    authority = resolve_session_authority(tmp_path / "state", approved_root=tmp_path)
    try:
        authority.mkdir("first/second", parents=True)
        child = authority.subdirectory("third/fourth", create=True)
        try:
            fd = authority.open("third/fourth/record", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            try:
                os.write(fd, b"private")
                assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
            finally:
                os.close(fd)
            assert authority.list("third/fourth") == ("record",)
            assert stat.S_IMODE(os.fstat(child.fd).st_mode) == mode
        finally:
            child.close()
        reopened = resolve_session_authority(tmp_path / "state", approved_root=tmp_path, create=False)
        reopened.close()
        (tmp_path / "state").rename(tmp_path / "retained")
        (tmp_path / "state").symlink_to(tmp_path / "retained", target_is_directory=True)
        authority.mkdir("still-pinned")
        assert (tmp_path / "retained/still-pinned").is_dir()
        authority.dispose()
        assert not (tmp_path / "retained/third/fourth/record").exists()
        for path in (tmp_path / "retained").rglob("*"):
            assert stat.S_IMODE(path.stat().st_mode) == mode
    finally:
        authority.close()
    assert len(os.listdir("/proc/self/fd")) == baseline


@pytest.mark.parametrize("mode", [0o700, 0o2700, 0o2750, 0o2701, 0o1700, 0o4700, 0o6700])
def test_private_directory_validators_agree_and_close_rejected_descriptors(tmp_path, monkeypatch, mode):
    tmp_path.chmod(0o700)
    child = tmp_path / "private"
    child.mkdir()
    child.chmod(mode)
    safe = mode in (0o700, 0o2700)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    fs = LinuxDirFdFilesystem.adopt(fd)
    os.close(fd)
    _forbid_directory_chmod(monkeypatch)
    baseline = len(os.listdir("/proc/self/fd"))
    try:
        if safe:
            authority = resolve_session_authority(child, approved_root=tmp_path, create=False)
            authority.close()
            subroot = fs.subroot("private")
            subroot.close()
            fs.write_atomic("private/record", b"private")
            assert fs.walk_files() == ("private/record",)
            assert fs.read_bytes("private/record") == b"private"
            fs.unlink("private/record")
            private_fd = TrustedPublicationAdapter._open_child_dir(fs._fd, "private")
            os.close(private_fd)
            with TrustedPublicationAdapter(tmp_path, child):
                pass
            fs.rmdir("private")
        else:
            with pytest.raises(ValueError):
                resolve_session_authority(child, approved_root=tmp_path, create=False)
            for operation in (lambda: fs.subroot("private"), fs.walk_files,
                              lambda: fs.rmdir("private"),
                              lambda: TrustedPublicationAdapter._open_child_dir(fs._fd, "private"),
                              lambda: TrustedPublicationAdapter(tmp_path, child)):
                with pytest.raises(StorageSafetyError):
                    operation()
                assert len(os.listdir("/proc/self/fd")) == baseline
            private_fd = os.open(child, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with pytest.raises(StorageSafetyError):
                    TrustedPublicationAdapter(tmp_path, child, repository_dir_fd=private_fd)
            finally:
                os.close(private_fd)
            assert stat.S_IMODE(child.stat().st_mode) == mode
        assert len(os.listdir("/proc/self/fd")) == baseline
    finally:
        fs.close()


@pytest.mark.parametrize("mode", [0o640, 0o660, 0o2600])
def test_setgid_directory_does_not_relax_regular_authority_modes(tmp_path, mode):
    tmp_path.chmod(0o2700)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    fs = LinuxDirFdFilesystem.adopt(fd)
    os.close(fd)
    try:
        fs.write_atomic("child/record", b"private")
        record = tmp_path / "child/record"
        record.chmod(mode)
        with pytest.raises(StorageSafetyError):
            fs.read_bytes("child/record")
        record.chmod(0o600)
        os.link(record, tmp_path / "linked")
        with pytest.raises(StorageSafetyError):
            fs.read_bytes("child/record")
    finally:
        fs.close()


def test_setgid_transaction_prepare_and_startup_recovery(tmp_path, monkeypatch):
    from test_proposal_apply import _ready_transaction
    from test_proposal_crash import DiskActivity, DiskEvents
    from test_proposal_service import Clock
    from okf_loom.proposal_review.activation import _compose_transactions

    bundle, session, repository, coordinator, publication, request, _ = _ready_transaction(tmp_path)
    repo_path = session / "proposal-review-v1"
    # Establish inherited mode before preparing any identity-bound transaction.
    for path in [repo_path, *repo_path.rglob("*")]:
        if path.is_dir():
            path.chmod(0o2700)
    activity = DiskActivity(session)
    _forbid_directory_chmod(monkeypatch)

    def stop(boundary, evidence):
        if boundary == "commit_decided":
            raise RuntimeError("pause for recovery")

    coordinator.fault_injector = stop
    recovered = None
    try:
        with pytest.raises(RuntimeError, match="pause for recovery"):
            coordinator.apply(request, "K" * 32)
        coordinator.close()
        transaction = next((repo_path / "transactions").iterdir())
        wal = repository.read_wal(transaction.name)
        assert wal.stage_dir_identity.mode == wal.backup_dir_identity.mode == 0o2700
        recovered, _ = _compose_transactions(bundle, repository, Clock(), activity=activity,
            outbox=DiskEvents(session), repository_path=repo_path)
        assert all(item.classification == "terminal" for item in repository.recovery_report.items)
        assert b"tags:" in (bundle / "tables/orders.md").read_bytes()
        for path in [repo_path, *repo_path.rglob("*")]:
            info = path.stat()
            assert stat.S_IMODE(info.st_mode) == (0o2700 if path.is_dir() else 0o600)
    finally:
        if recovered is not None:
            recovered.close()
        coordinator.close()
        repository.close()
