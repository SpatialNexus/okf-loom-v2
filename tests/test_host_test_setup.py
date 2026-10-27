"""Restricted-host setup must preserve the assertions it enables."""
from pathlib import Path
import errno
import multiprocessing
import os
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


@pytest.mark.parametrize("tmp_denied", (False, True))
def test_pytest_temp_prefers_tmp_then_external_cache_with_in_repo_venv(tmp_path, monkeypatch, tmp_denied):
    from types import SimpleNamespace

    root = tmp_path / "checkout"
    root.mkdir()
    cache = Path("/external-cache")
    (root / ".shared-cache").symlink_to(cache, target_is_directory=True)
    monkeypatch.setattr(conftest, "TOOLKIT_ROOT", root)
    monkeypatch.setattr(conftest.sys, "prefix", str(root / ".venv"))
    monkeypatch.setattr(conftest.tempfile, "gettempdir", lambda: "/" + "long" * 30)
    owned = tmp_path / "owned"
    calls, cleanups = [], []

    def allocate(*, prefix, dir):
        calls.append(dir)
        if tmp_denied and dir == Path("/tmp"):
            raise PermissionError(errno.EACCES, "restricted host denies /tmp")
        owned.mkdir()
        return str(owned)

    monkeypatch.setattr(conftest.tempfile, "mkdtemp", allocate)
    conftest.pytest_configure(SimpleNamespace(
        add_cleanup=cleanups.append, addinivalue_line=lambda *_: None))
    try:
        assert calls == ([Path("/tmp"), cache] if tmp_denied else [Path("/tmp")])
        assert conftest.tempfile.tempdir == str(owned)
        assert all(os.environ[key] == str(owned) for key in ("TMPDIR", "TMP", "TEMP"))
        assert stat.S_IMODE(owned.stat().st_mode) == 0o700
    finally:
        for cleanup in reversed(cleanups):
            cleanup()
    assert not owned.exists()


def test_short_chrome_tmpdir_rejects_in_tree_effective_and_fallback_paths(monkeypatch, tmp_path):
    # Short synthetic paths prove rejection is about the work tree, not length.
    root = Path("/checkout")
    monkeypatch.setattr(conftest, "TOOLKIT_ROOT", root)
    owned = tmp_path / "external"
    calls = []

    def allocate(*, prefix, dir):
        calls.append(dir)
        owned.mkdir()
        return str(owned)

    monkeypatch.setattr(conftest.tempfile, "mkdtemp", allocate)
    result = conftest._short_chrome_tmpdir(root / "private", (
        root, root / ".venv", root / ".shared-cache", Path("/outside")))
    assert result == owned
    assert calls == [Path("/outside")]


def test_private_external_tmpdir_survives_denied_system_tmp(monkeypatch):
    def denied(**_kwargs):
        pytest.fail("must keep the documented private external TMPDIR")

    monkeypatch.setattr(conftest.tempfile, "mkdtemp", denied)
    assert conftest._short_chrome_tmpdir(Path("/short-private"), (Path("/tmp"),)) is None


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
