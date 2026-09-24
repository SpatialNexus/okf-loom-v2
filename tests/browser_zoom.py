"""Explicit headed-X11 driver fixture for the unchanged capture authority gate."""
import json
import os
from pathlib import Path
import shutil
import sys

import pytest


def zoom_runtime(capture, tmp_path, monkeypatch):
    executable = shutil.which("xdotool")
    if not executable or not os.environ.get("DISPLAY"):
        pytest.fail("Real Chrome zoom needs DISPLAY and xdotool; no viewport/CSS substitute is accepted")
    root = tmp_path / "zoom-runtime"
    root.mkdir()
    driver = root / "chrome-zoom-driver"
    environment = {key: os.environ[key] for key in ("DISPLAY", "LD_LIBRARY_PATH") if key in os.environ}
    driver.write_text(f"#!{sys.executable}\n" + """import argparse, os, subprocess
""" + f"os.environ.update({environment!r})\nXDO = {executable!r}\n" + """
p = argparse.ArgumentParser()
p.add_argument('--session-title', required=True)
p.add_argument('--percent', type=int, choices=(100, 200, 400), required=True)
args = p.parse_args()
windows = subprocess.check_output([XDO, 'search', '--sync', '--onlyvisible', '--name', args.session_title], text=True, timeout=4).split()
if len(windows) != 1:
    raise SystemExit('Expected one uniquely title-bound Chrome window')
subprocess.run([XDO, 'windowfocus', '--sync', windows[0]], check=True, timeout=2)
subprocess.run([XDO, 'key', '--clearmodifiers', 'ctrl+0'], check=True, timeout=2)
for _ in range({100: 0, 200: 5, 400: 8}[args.percent]):
    subprocess.run([XDO, 'key', '--clearmodifiers', 'ctrl+plus'], check=True, timeout=2)
""")
    driver.chmod(0o755)
    monkeypatch.setenv("OKF_CAPTURE_BROWSER_ZOOM_DRIVER", str(driver))
    return capture.ImmutableRuntimeWorkspace.for_test(root, root)
