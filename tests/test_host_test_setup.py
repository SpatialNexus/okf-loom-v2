"""Restricted-host setup must preserve the assertions it enables."""
from pathlib import Path
import errno
import multiprocessing
import stat

import pytest

import conftest


def test_short_chrome_tmpdir_leaves_short_effective_parent_alone(monkeypatch):
    monkeypatch.setattr(conftest.tempfile, "mkdtemp", lambda **_kw: pytest.fail("relocated short TMPDIR"))
    assert conftest._short_chrome_tmpdir(Path("/short"), ()) is None


@pytest.mark.parametrize("effective", [Path("/" + "long" * 30), Path("/tmp")])
def test_short_chrome_tmpdir_uses_writable_private_fallback(tmp_path, monkeypatch, effective):
    calls = []
    owned = tmp_path / "owned"

    def mkdtemp(*, prefix, dir):
        calls.append((prefix, dir))
        if dir == Path("/denied"):
            raise PermissionError(errno.EACCES, "denied")
        owned.mkdir(mode=0o775)
        return str(owned)

    monkeypatch.setattr(conftest.tempfile, "mkdtemp", mkdtemp)
    assert conftest._short_chrome_tmpdir(effective, (Path("/denied"), Path("/cache"))) == owned
    assert calls == [("b-", Path("/denied")), ("b-", Path("/cache"))]
    assert stat.S_IMODE(owned.stat().st_mode) == 0o700


def test_short_chrome_tmpdir_fails_early_with_actionable_diagnostic(monkeypatch):
    def denied(**_kwargs):
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(conftest.tempfile, "mkdtemp", denied)
    with pytest.raises(pytest.UsageError, match="107-byte Unix socket.*set TMPDIR"):
        conftest._short_chrome_tmpdir(Path("/" + "long" * 30), (Path("/denied"),))


def test_short_chrome_tmpdir_measures_resolved_bytes(tmp_path, monkeypatch):
    link = tmp_path / "alias"
    link.symlink_to(Path("/" + "é" * 60), target_is_directory=True)
    monkeypatch.setattr(conftest.tempfile, "mkdtemp", lambda **_kwargs: pytest.fail("tried long parent"))
    with pytest.raises(pytest.UsageError, match="too long"):
        conftest._short_chrome_tmpdir(link, (link,))


def test_private_session_fixture_repairs_only_its_new_owned_directory(tmp_path, monkeypatch):
    parent = tmp_path / ".okf-loom"
    parent.mkdir(mode=0o2775)
    parent.chmod(0o2775)
    original_mkdir = Path.mkdir

    def inherited_acl(path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path.name == "session":
            path.chmod(0o2775)
        return result

    monkeypatch.setattr(Path, "mkdir", inherited_acl)
    session = conftest.private_session_root(tmp_path)
    assert stat.S_IMODE(session.stat().st_mode) == 0o700
    assert stat.S_IMODE(parent.stat().st_mode) == 0o2775
    with pytest.raises(FileExistsError):
        conftest.private_session_root(tmp_path)


def test_semaphore_probe_skips_only_permission_denial(monkeypatch):
    def denied():
        raise PermissionError(errno.EACCES, "host denies SemLock")

    monkeypatch.setattr(multiprocessing, "Lock", denied)
    with pytest.raises(pytest.skip.Exception, match=r"multiprocessing.Lock\(\) denied"):
        conftest.multiprocessing_semaphores.__wrapped__()

    def broken():
        raise RuntimeError("broken multiprocessing")

    monkeypatch.setattr(multiprocessing, "Lock", broken)
    with pytest.raises(RuntimeError, match="broken multiprocessing"):
        conftest.multiprocessing_semaphores.__wrapped__()
