"""The browser-marker guard: only browser-marked items may start a browser.

Lane selection (``-m browser`` / ``-m "not browser"``) trusts the marker, so
browser_support refuses a Playwright or Chromium start from an unmarked item.
Each case runs the real conftest and browser_support in a child pytest and
reads the outcome from its JUnit report; no browser is ever started.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ElementTree

from browser_support import unavailable
from conftest import TOOLKIT_ROOT

REFUSAL = "without the browser marker"

POISON_PLAYWRIGHT = "raise RuntimeError('the guard let Playwright be imported')\n"

UNMARKED_PROBE = '''
import pytest
import browser_support


@pytest.fixture(scope="module")
def module_driver():
    return browser_support.sync_playwright()


def test_unmarked_driver_start():
    browser_support.sync_playwright()


def test_unmarked_module_fixture(module_driver):
    pass
'''

MARKER_PROBE = '''
import pytest
import browser_support
from playwright.sync_api import Playwright


class Chromium:
    executable_path = __file__

    def launch(self, **options):
        return "launched"


class RealDriver(Playwright):
    """The real driver type with a synthetic launcher, so nothing starts."""
    chromium = Chromium()


class SyntheticOwner:
    chromium = Chromium()


@pytest.fixture(autouse=True)
def _optional_browser_dependency():
    """The dependency check is the lane policy's job, not this probe's."""


def real_driver():
    return RealDriver.__new__(RealDriver)


def test_unmarked_launch():
    browser_support.launch_chromium(real_driver())


@pytest.mark.browser
def test_marked_launch():
    assert browser_support.launch_chromium(real_driver()) == "launched"


@pytest.mark.browser
def test_marked_driver_start():
    browser_support.sync_playwright()


def test_unmarked_synthetic_owner():
    assert browser_support.launch_chromium(SyntheticOwner()) == "launched"
'''

SCRIPT = '''
import browser_support
from playwright.sync_api import Playwright


class Chromium:
    executable_path = __file__

    def launch(self, **options):
        return "launched"


class RealDriver(Playwright):
    chromium = Chromium()


browser_support.sync_playwright()
print(browser_support.launch_chromium(RealDriver.__new__(RealDriver)))
'''


def _child_env(**updates):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    for name in ("OKF_BROWSER_TEST_OPT_OUT", "OKF_REQUIRE_BROWSER", "OKF_CHROME",
                 "AIC_PLAYWRIGHT_CHROME_PATH", "OKF_CAPTURE_REAL_BROWSER_ZOOM"):
        env.pop(name, None)
    env.update(updates)
    return env


def _run_probe(tmp_path, source, **env):
    """Run one probe module beside copies of the real conftest and browser_support."""
    probe = tmp_path / "probe"
    probe.mkdir()
    for name in ("conftest.py", "browser_support.py"):
        shutil.copy(TOOLKIT_ROOT / "tests" / name, probe / name)
    (probe / "pytest.ini").write_text("[pytest]\nmarkers =\n    browser: probe\n")
    (probe / "test_probe.py").write_text(source)
    report = tmp_path / "report.xml"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"--junitxml={report}", "test_probe.py"],
        cwd=probe, env=_child_env(**env), capture_output=True, text=True, timeout=120,
    )
    assert report.exists(), result.stdout + result.stderr
    outcomes = {}
    for case in ElementTree.parse(report).iter("testcase"):
        problems = [child for child in case if child.tag in ("failure", "error", "skipped")]
        outcomes[case.get("name")] = (
            (problems[0].tag, problems[0].get("message", "")) if problems else ("passed", "")
        )
    return outcomes, result.stdout + result.stderr


def test_unmarked_items_cannot_start_playwright_and_are_named(tmp_path):
    # A poison package proves the refusal precedes any Playwright import or start.
    poison = tmp_path / "poison" / "playwright"
    poison.mkdir(parents=True)
    (poison / "__init__.py").write_text(POISON_PLAYWRIGHT)
    outcomes, output = _run_probe(tmp_path, UNMARKED_PROBE, PYTHONPATH=str(poison.parent))

    kind, message = outcomes["test_unmarked_driver_start"]
    assert kind == "failure", output
    assert "test_probe.py::test_unmarked_driver_start starts a browser through " \
        "browser_support.sync_playwright " + REFUSAL in message, output
    # A module-scoped browser fixture is refused during the setup of the item
    # that first needs it, and that item is the one named.
    kind, message = outcomes["test_unmarked_module_fixture"]
    assert kind == "error", output
    assert "test_probe.py::test_unmarked_module_fixture starts a browser through " \
        "browser_support.sync_playwright " + REFUSAL in message, output
    assert "the guard let Playwright be imported" not in output


def test_marker_admits_real_driver_while_synthetic_owners_and_scripts_are_unguarded(tmp_path):
    if importlib.util.find_spec("playwright") is None:
        unavailable("Playwright is not installed; the real driver type is needed")
    outcomes, output = _run_probe(tmp_path, MARKER_PROBE)

    kind, message = outcomes["test_unmarked_launch"]
    assert kind == "failure", output
    assert "test_probe.py::test_unmarked_launch starts a browser through " \
        "browser_support.launch_chromium " + REFUSAL in message, output
    assert outcomes["test_marked_launch"] == ("passed", ""), output
    assert outcomes["test_marked_driver_start"] == ("passed", ""), output
    # Helper unit tests hand launch_chromium synthetic owners that cannot
    # start Chromium; they stay in the core lane unmarked.
    assert outcomes["test_unmarked_synthetic_owner"] == ("passed", ""), output

    # Capture scripts import browser_support outside any pytest item.
    script = tmp_path / "capture.py"
    script.write_text(SCRIPT)
    result = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True,
        env=_child_env(PYTHONPATH=str(TOOLKIT_ROOT / "tests") + os.pathsep
                       + str(TOOLKIT_ROOT / "scripts")),
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "launched", result.stdout + result.stderr
