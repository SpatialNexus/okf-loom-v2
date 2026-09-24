"""Capture proof screenshots for the frontend remediation.

Stands up ``okf serve samples/demo_bundle`` (studio on), drives Chrome to the
key surfaces, and writes PNGs under docs/screenshots/iter1-frontend/.

Run:  python3 tests/capture_iter1_frontend_proof.py
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

TK = Path(__file__).resolve().parent.parent
DEMO = TK / "samples" / "demo_bundle"
OUT = TK / "docs" / "screenshots" / "iter1-frontend"
OUT.mkdir(parents=True, exist_ok=True)


def okf_env():
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(TK / "scripts") + (os.pathsep + existing if existing else "")
    return env


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_up(base):
    dl = time.monotonic() + 15
    while time.monotonic() < dl:
        try:
            urllib.request.urlopen(base + "/", timeout=1)
            return
        except Exception:
            time.sleep(0.15)
    raise RuntimeError("server not ready")


def wait_studio(pg):
    # CSP ('self' only) blocks wait_for_function's string eval, so poll via
    # evaluate (function form, which Playwright routes around CSP) instead.
    for _ in range(80):
        if pg.evaluate("() => typeof window.okfLoomStudio === 'object'"):
            return
        pg.wait_for_timeout(100)
    raise RuntimeError("studio did not boot")


def poll(pg, fn, timeout_ms=8000):
    """Poll a Playwright function-form evaluate until it returns truthy."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if pg.evaluate(fn):
            return True
        pg.wait_for_timeout(100)
    return False


def main():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "okf_loom", "serve", str(DEMO),
         "--host", "127.0.0.1", "--port", str(port), "--no-open"],
        cwd=str(TK), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=okf_env(),
    )
    try:
        wait_up(base)
        with sync_playwright() as p:
            b = p.chromium.launch(channel="chrome")
            ctx = b.new_context(viewport={"width": 1280, "height": 800},
                                device_scale_factor=1)
            pg = ctx.new_page()

            # 1. Comment mark + margin marker on a concept page.
            pg.goto(base + "/tables/orders", wait_until="load")
            wait_studio(pg)
            # Seed a confirmed comment so both the <mark> and the margin
            # marker render (the marker reads from state.comments).
            pg.evaluate("""() => {
                const p = document.querySelector('.okf-page__body p, .okf-page__body li, .okf-page__body h2');
                let t = p; while (t && t.nodeType !== 3) t = t.firstChild;
                const snippet = t.nodeValue.slice(0, 40);
                window.okfLoomStudio.state.comments.unshift({
                    id: 'shot-mark-1', concept: 'tables/orders',
                    anchor: { kind: 'text', ref: snippet, block_id: '', concept: 'tables/orders' },
                    body: 'Link this section to Customers.', state: 'open',
                    resolved_activity: [], ts: new Date().toISOString(),
                });
                // Drive mark + marker re-application.
                const body = document.querySelector('.okf-page__body');
                window.okfLoomStudio.applyDoc({html: body.innerHTML, title:'', description:'', raw:'', rev:8888}, {pulse:false});
            }""")
            pg.wait_for_selector(".okf-comment-mark", timeout=4000)
            pg.wait_for_selector(".okf-comment-marker", timeout=4000)
            pg.screenshot(path=str(OUT / "iter1-comment-mark-desktop.png"), full_page=False)
            print("captured: comment-mark-desktop")

            # 2. Presence focus highlight on a subdir index.
            pg.goto(base + "/tables/", wait_until="load")
            wait_studio(pg)
            pg.wait_for_selector(".okf-concept-list li a", timeout=5000)
            pg.evaluate("""() => {
                const a = document.querySelector('.okf-concept-list li a');
                const cid = a.getAttribute('href').replace(/^\\//,'').replace(/\\.(html|md)$/,'');
                const token = window.__OKF_LOOM_STUDIO__ && window.__OKF_LOOM_STUDIO__.token || '';
                return fetch('/__presence', { method:'POST', headers:{'Content-Type':'application/json','X-OKF-Token':token},
                    body: JSON.stringify({state:'editing', focus:cid, actor:'agent'}) });
            }""")
            assert poll(pg, "() => document.querySelectorAll('.okf-presence-focus').length >= 1", 8000)
            pg.screenshot(path=str(OUT / "iter1-presence-focus-desktop.png"), full_page=False)
            print("captured: presence-focus-desktop")

            # 3. Dark theme variant of the presence highlight.
            pg.evaluate("""() => { document.documentElement.setAttribute('data-theme','dark'); }""")
            pg.wait_for_timeout(400)
            pg.screenshot(path=str(OUT / "iter1-presence-focus-dark.png"), full_page=False)
            print("captured: presence-focus-dark")
            pg.evaluate("""() => { document.documentElement.setAttribute('data-theme','light'); }""")

            # 4. Command palette open (focus trap surface).
            pg.goto(base + "/tables/orders", wait_until="load")
            wait_studio(pg)
            pg.keyboard.press("Control+k")
            pg.wait_for_selector(".okf-palette-overlay:not([hidden])", timeout=3000)
            pg.screenshot(path=str(OUT / "iter1-palette-desktop.png"), full_page=False)
            print("captured: palette-desktop")
            pg.keyboard.press("Escape")

            # 5. Activity toast with inline Undo button.
            pg.evaluate("""() => {
                const hub = window.okfLoomLive;
                for (let i=0;i<3;i++) hub.emit('activity', {
                    id:'shot-'+i, actor:'agent', action:'add_link', ids:['tables/orders'],
                    summary:'Linked Orders to Customers', undoable:true, group_id:'shot-grp',
                    ts:new Date().toISOString(),
                });
            }""")
            pg.wait_for_selector(".okf-toast", timeout=3000)
            pg.wait_for_timeout(600)
            pg.screenshot(path=str(OUT / "iter1-toast-undo-desktop.png"), full_page=False)
            print("captured: toast-undo-desktop")

            # 6. Scoped block pulse (trigger a 1-block patch).
            pg.goto(base + "/tables/orders", wait_until="load")
            wait_studio(pg)
            pg.evaluate("""() => {
                const body = document.querySelector('.okf-page__body');
                const kids = Array.from(body.children).filter(n=>n.nodeType===1);
                const first = kids[0];
                const changed = '<p style="background:var(--okf-accent-bg)">Agent just rewrote this block live.</p>';
                const rest = kids.slice(1).map(k=>k.outerHTML).join('\\n');
                window.okfLoomStudio.applyDoc({html: changed+'\\n'+rest, title:'', description:'', raw:'', rev:9001}, {pulse:true});
            }""")
            pg.wait_for_timeout(200)
            pg.screenshot(path=str(OUT / "iter1-block-pulse-desktop.png"), full_page=False)
            print("captured: block-pulse-desktop")

            # 7. Mobile viewport: concept page (responsive check).
            ctx2 = b.new_context(viewport={"width": 390, "height": 844},
                                 device_scale_factor=2, is_mobile=True)
            pg2 = ctx2.new_page()
            pg2.goto(base + "/tables/orders", wait_until="load")
            pg2.wait_for_function("typeof window.okfLoomStudio === 'object'", timeout=8000)
            pg2.screenshot(path=str(OUT / "iter1-concept-mobile.png"), full_page=False)
            print("captured: concept-mobile")
            ctx2.close()
            ctx.close()
            b.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("all screenshots captured to", OUT)


if __name__ == "__main__":
    main()
