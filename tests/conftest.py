"""Shared pytest fixtures for the okf-loom test suite.

Every fixture that touches the filesystem writes into ``tmp_path`` so the
real upstream fixtures and the in-tree ``tests/fixtures/`` bundles are
never mutated.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

# Resolved absolute path to the okf-loom checkout root (the directory
# containing pyproject.toml and this tests/ tree). Tests use this to locate
# the checked-in fixture bundles and the upstream real-data bundles without
# depending on the process's current working directory.
TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
TOOLKIT_SCRIPTS = TOOLKIT_ROOT / "scripts"
FIXTURES_DIR = TOOLKIT_ROOT / "tests" / "fixtures"

# Real-data bundles shipped in the upstream repo, when a local workspace clone
# is present next to this checkout's source tree.
_UPSTREAM_BUNDLES_DIR = (
    TOOLKIT_ROOT / "workspace" / "upstream" / "okf" / "bundles"
)


# ---------------------------------------------------------------------------
# In-tree synthetic fixtures (tiny_good / tiny_bad / empty)
# ---------------------------------------------------------------------------


@pytest.fixture
def tiny_good_bundle(tmp_path: Path) -> Path:
    """A copy of ``tests/fixtures/tiny_good/`` inside ``tmp_path``.

    Exercises: both absolute and relative link forms, typed relations,
    a root index.md with okf_version, a log.md, tags, a # Schema section
    with foreign-key text, and one concept missing description. Safe to
    mutate.
    """
    dst = tmp_path / "tiny_good"
    shutil.copytree(FIXTURES_DIR / "tiny_good", dst)
    return dst


@pytest.fixture
def tiny_bad_bundle(tmp_path: Path) -> Path:
    """A copy of ``tests/fixtures/tiny_bad/`` inside ``tmp_path``.

    Contains: a missing-type concept, a broken link, malformed frontmatter,
    a non-root ``index.md`` carrying frontmatter, and a log.md that is not
    newest-first. Safe to mutate.
    """
    dst = tmp_path / "tiny_bad"
    shutil.copytree(FIXTURES_DIR / "tiny_bad", dst)
    return dst


@pytest.fixture
def empty_bundle(tmp_path: Path) -> Path:
    """An empty directory (zero ``.md`` files). Safe to mutate."""
    dst = tmp_path / "empty_bundle"
    dst.mkdir()
    return dst


# ---------------------------------------------------------------------------
# Upstream real-data bundle fixtures
# ---------------------------------------------------------------------------


def _copy_upstream(name: str, tmp_path: Path) -> Path:
    src = _UPSTREAM_BUNDLES_DIR / name
    if not src.exists():
        pytest.skip(
            f"Upstream bundle {name!r} not available at {src} "
            "(integration fixtures are optional)."
        )
    dst = tmp_path / name
    shutil.copytree(src, dst)
    return dst


@pytest.fixture
def ga4_bundle(tmp_path: Path) -> Path:
    """A writable copy of the upstream ``ga4`` bundle."""
    return _copy_upstream("ga4", tmp_path)


@pytest.fixture
def stackoverflow_bundle(tmp_path: Path) -> Path:
    """A writable copy of the upstream ``stackoverflow`` bundle."""
    return _copy_upstream("stackoverflow", tmp_path)


@pytest.fixture
def crypto_bitcoin_bundle(tmp_path: Path) -> Path:
    """A writable copy of the upstream ``crypto_bitcoin`` bundle."""
    return _copy_upstream("crypto_bitcoin", tmp_path)


def copy_upstream_bundle(name: str, tmp_path: Path) -> Path:
    """Helper for tests that want to pick an upstream bundle by name.

    Mirrors the spec-required helper signature. Skips the test when the
    upstream tree is unavailable.
    """
    return _copy_upstream(name, tmp_path)


def okf_subprocess_env(**updates: str) -> dict[str, str]:
    """Environment for child processes that import checkout-local OKF runtime.

    ``pyproject.toml`` adds ``scripts`` to ``sys.path`` for the pytest process,
    but child processes launched from temp bundle directories do not inherit
    that Python path automatically.  Keep subprocess tests aligned with the
    checked-in ``scripts/okf-loom`` helper by prepending the checkout runtime path.
    """
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(TOOLKIT_SCRIPTS) + (
        os.pathsep + existing if existing else ""
    )
    env.update(updates)
    return env


def okf_module_argv(*args: str) -> list[str]:
    """Return argv for invoking the checkout-local OKF module in subprocesses."""
    return [sys.executable, "-m", "okf_loom", *args]


def private_session_root(bundle: Path) -> Path:
    """Create this test's session authority, never inherit checkout permissions.

    copytree preserves default ACLs from shared/setgid checkouts. An explicit
    mkdir mode plus chmod on our newly owned fixture directory keeps activation
    tests private without repairing or bypassing the product's privacy check.
    """
    # Proposal publication also correctly rejects a group-writable bundle
    # root. This is a disposable fixture copy, never the authored source.
    bundle.chmod(0o700)
    parent = bundle / ".okf-loom"
    parent.mkdir(exist_ok=True)
    session = parent / "session"
    session.mkdir(mode=0o700)
    session.chmod(0o700)
    return session


def _short_chrome_tmpdir(effective: Path, candidates: tuple[Path, ...]) -> Path | None:
    """Keep Chrome's SingletonSocket within Linux's 107-byte pathname limit.

    Return a newly owned short directory when relocation is needed. Shared or
    setgid temporary ancestors also need a private namespace: child directories
    otherwise inherit setgid even with mkdir(mode=0700). Do not shorten paths
    with symlinks: Chrome resolves them before binding its socket.
    """
    suffix = "/.com.google.Chrome.XXXXXX/SingletonSocket"
    private = not effective.exists() or stat.S_IMODE(effective.stat().st_mode) == 0o700
    if len(os.fsencode(str(effective.resolve()))) + len(suffix) <= 107 and private:
        return None
    errors = []
    for candidate in candidates:
        candidate = candidate.resolve()
        if len(os.fsencode(str(candidate))) + len("/b-XXXXXXXX") + len(suffix) > 107:
            continue
        try:
            path = Path(tempfile.mkdtemp(prefix="b-", dir=candidate))
            path.chmod(0o700)
            return path
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
    raise pytest.UsageError(
        f"TMPDIR {effective} is too long or not private for Chrome's 107-byte Unix socket path; "
        "set TMPDIR to a short writable private directory (see CONTRIBUTING.md). "
        + "; ".join(errors)
    )


@pytest.fixture
def multiprocessing_semaphores():
    """Probe the exact optional host primitive, not a broad platform skip."""
    import multiprocessing
    try:
        lock = multiprocessing.Lock()
    except PermissionError as exc:
        pytest.skip(f"multiprocessing.Lock() denied by host POSIX semaphore policy: {exc}")
    # Exercise the primitive too; other creation/acquisition errors are failures.
    with lock:
        pass


# ---------------------------------------------------------------------------
# pytest markers
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    # Before collection imports capture helpers or any session browser fixture
    # starts its driver, give Chrome one short private temporary namespace.
    effective = Path(tempfile.gettempdir())
    short = _short_chrome_tmpdir(
        effective, ((TOOLKIT_ROOT / ".shared-cache").resolve(),
                    Path(sys.prefix).parent, Path("/tmp")))
    if short is not None:
        previous = {key: os.environ.get(key) for key in ("TMPDIR", "TMP", "TEMP")}
        cached = tempfile.tempdir
        for key in previous:
            os.environ[key] = str(short)
        tempfile.tempdir = str(short)

        def cleanup():
            tempfile.tempdir = cached
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            shutil.rmtree(short)

        config.add_cleanup(cleanup)
    config.addinivalue_line(
        "markers",
        "integration: tests that exercise upstream real-data bundles",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    # Auto-mark any test that uses an upstream-bundle fixture as ``integration``.
    upstream_fixtures = {
        "ga4_bundle",
        "stackoverflow_bundle",
        "crypto_bitcoin_bundle",
    }
    for item in items:
        used = set(getattr(item, "fixturenames", [])) & upstream_fixtures
        if used:
            item.add_marker(pytest.mark.integration)
        # The shared browser Page fixture also appears in mixed contract modules.
        # Mark its users, not source-only/backend tests in those same modules.
        if "page" in getattr(item, "fixturenames", []):
            item.add_marker(pytest.mark.browser)
        if (os.environ.get("OKF_BROWSER_TEST_OPT_OUT") == "1"
                and item.get_closest_marker("browser")):
            if os.environ.get("OKF_REQUIRE_BROWSER") == "1":
                raise pytest.UsageError("OKF_REQUIRE_BROWSER conflicts with OKF_BROWSER_TEST_OPT_OUT")
            item.add_marker(pytest.mark.skip(reason="browser lane explicitly opted out"))


# The item whose run protocol (setup, call, teardown) is in progress. Fixture
# setup of every scope happens inside the protocol of the item that first
# needs it, so module- and session-scoped browser fixtures are attributed to
# that item. browser_support consults it to refuse browser starts from items
# that lane selection (-m browser / -m "not browser") cannot see.
_ACTIVE_ITEM: pytest.Item | None = None


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    global _ACTIVE_ITEM
    _ACTIVE_ITEM = item
    try:
        return (yield)
    finally:
        _ACTIVE_ITEM = None


def active_test_item() -> pytest.Item | None:
    """The pytest item being run, or None outside a test protocol."""
    return _ACTIVE_ITEM


@pytest.fixture(autouse=True)
def _optional_browser_dependency(request):
    if request.node.get_closest_marker("browser"):
        from browser_support import playwright_api
        playwright_api()


@pytest.fixture(autouse=True)
def _reset_operator_consent():
    """P2-22 (iter-3): reset operator-consent module-global state between
    tests. Without this, a test that sets consent and forgets to reset leaks
    ``consent=True`` into every subsequent test in the session, flipping
    security invariants and producing order-dependent flakiness. ~20 test
    sites manually reset ``assets._operator_consent_override``; this autouse
    fixture is the single-source guarantee."""
    yield
    try:
        from okf_loom.viewer import assets
        assets._operator_consent_override = None
        assets.clear_overrides_cache()
    except Exception:
        pass
