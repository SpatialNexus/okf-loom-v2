"""Executable developer-lane contracts; no browser installation required."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from conftest import TOOLKIT_ROOT


def test_core_collection_never_imports_playwright(tmp_path):
    # A poison package detects even a caught import attempt, on provisioned hosts too.
    package = tmp_path / "playwright"
    package.mkdir()
    marker = tmp_path / "import-attempt"
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "raise RuntimeError('core collection imported optional Playwright')\n"
    )
    env = dict(os.environ, PYTHONPATH=str(tmp_path), PYTHONDONTWRITEBYTECODE="1",
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    env.pop("OKF_BROWSER_TEST_OPT_OUT", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "-m", "not browser and not integration"],
        cwd=TOOLKIT_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists(), result.stdout + result.stderr
    for name in (
        "test_static_concept_has_one_ssr_outline_owner_and_end_paths",
        "test_bootstrap_reports_honest_proposal_capability_without_private_token",
        "test_renderer_generation_fence_and_theme_mode_policy",
        "test_release_runtime_identities_match_reviewed_source_manifest",
        "test_keyboard_selection_contract_assessment",
        "test_presence_frontend_contract_matrices_are_complete",
    ):
        assert name in result.stdout


@pytest.mark.parametrize("code, timeout, message", [
    ("import sys; print('startup diagnostic', flush=True); sys.exit(7)", 5, "rc=7"),
    ("import time; print('startup diagnostic', flush=True); time.sleep(60)", .3, "deadline"),
])
def test_server_startup_failures_fail_with_logs_and_reap(tmp_path, monkeypatch, code, timeout, message):
    from browser_support import managed_server
    import browser_support

    started = []
    popen = subprocess.Popen

    def record(*args, **kwargs):
        process = popen(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(browser_support.subprocess, "Popen", record)

    with pytest.raises(pytest.fail.Exception, match=message) as failure:
        with managed_server([sys.executable, "-c", code], "http://127.0.0.1:1",
                            tmp_path / "server.log", timeout=timeout):
            pytest.fail("unready process yielded")
    assert "startup diagnostic" in str(failure.value)
    assert len(started) == 1 and started[0].poll() is not None


@pytest.mark.parametrize("required", [False, True])
def test_missing_browser_is_optional_only_outside_provisioned_lane(tmp_path, monkeypatch, required):
    from types import SimpleNamespace
    from browser_support import launch_chromium

    monkeypatch.delenv("OKF_CHROME", raising=False)
    monkeypatch.delenv("AIC_PLAYWRIGHT_CHROME_PATH", raising=False)
    monkeypatch.setenv("OKF_REQUIRE_BROWSER", "1" if required else "0")
    monkeypatch.setattr("browser_support.shutil.which", lambda name: None)
    owner = SimpleNamespace(chromium=SimpleNamespace(executable_path=str(tmp_path / "missing")))
    expected = pytest.fail.Exception if required else pytest.skip.Exception
    with pytest.raises(expected, match="Chromium is not installed"):
        launch_chromium(owner)


def test_installed_browser_launch_failure_is_not_a_skip(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from browser_support import launch_chromium

    executable = tmp_path / "chrome"
    executable.touch()
    monkeypatch.setenv("OKF_CHROME", str(executable))

    def broken(**kwargs):
        raise RuntimeError("browser crashed")

    owner = SimpleNamespace(chromium=SimpleNamespace(launch=broken))
    with pytest.raises(RuntimeError, match="browser crashed"):
        launch_chromium(owner)


@pytest.mark.parametrize("required", [False, True])
def test_missing_playwright_is_optional_only_outside_provisioned_lane(monkeypatch, required):
    from browser_support import playwright_api

    monkeypatch.setenv("OKF_REQUIRE_BROWSER", "1" if required else "0")
    monkeypatch.setattr("browser_support.importlib.util.find_spec", lambda name: None)
    expected = pytest.fail.Exception if required else pytest.skip.Exception
    with pytest.raises(expected, match="Playwright is not installed"):
        playwright_api()


def test_broken_playwright_installation_is_not_a_skip(monkeypatch):
    from browser_support import playwright_api

    monkeypatch.setattr("browser_support.importlib.util.find_spec", lambda name: object())

    def broken(name):
        raise ImportError("broken dependency inside installed Playwright")

    monkeypatch.setattr("browser_support.importlib.import_module", broken)
    with pytest.raises(ImportError, match="broken dependency"):
        playwright_api()


def test_viewer_server_uses_disposable_bundle(tmp_path_factory):
    from test_viewer_browser import server_url
    import urllib.request

    before = {p: p.read_bytes() for p in (TOOLKIT_ROOT / "samples/demo_bundle").rglob("*")
              if p.is_file()}
    fixture = server_url.__wrapped__(tmp_path_factory)
    base = next(fixture)
    try:
        with urllib.request.urlopen(base + "/", timeout=5) as response:
            assert response.status == 200
    finally:
        fixture.close()
    after = {p: p.read_bytes() for p in (TOOLKIT_ROOT / "samples/demo_bundle").rglob("*")
             if p.is_file()}
    assert after == before
