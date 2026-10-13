"""Execute the sole appearance owner in Node, including denial and external inputs."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from okf_loom.config import _LEGACY_THEME_MIGRATIONS

THEME = Path(__file__).resolve().parents[1] / "scripts/okf_loom/viewer/static/theme.js"
KEY = "okf-loom.appearance"
HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const opts = JSON.parse(process.argv[1]), source = fs.readFileSync(process.argv[2], 'utf8');
const store = Object.assign({}, opts.storage), attrs = Object.assign({}, opts.attrs);
const events = [], listeners = {}, classes = [];
let osListener;
const mq = {matches: !!opts.dark, addEventListener: (_, fn) => {osListener = fn;}};
const localStorage = {
 getItem: k => {if(opts.denyRead) throw Error('denied'); return k in store ? store[k] : null;},
 setItem: (k,v) => {if(opts.denyWrite) throw Error('denied'); store[k] = v;},
 removeItem: k => {if(opts.denyWrite) throw Error('denied'); delete store[k];}
};
const sb = {
 localStorage, matchMedia: () => mq,
 document: {readyState:'loading', addEventListener: () => {},
  documentElement: {getAttribute:k => attrs[k] || null, setAttribute:(k,v) => {attrs[k]=v;},
   classList:{add:k => classes.push(k)}},
  getElementById: id => {
   const value = id === 'okf-theme-config' ? opts.config : id === 'okf-studio-bootstrap' ? opts.bootstrap : null;
   return value == null ? null : {textContent: typeof value === 'string' ? value : JSON.stringify(value)};
  }},
 addEventListener: (k,fn) => {(listeners[k] ||= []).push(fn);},
 removeEventListener: (k,fn) => {listeners[k] = (listeners[k] || []).filter(f => f !== fn);},
 dispatchEvent: e => {events.push(e.detail); (listeners[e.type] || []).forEach(fn => fn(e));},
 CustomEvent: function(type, init) {this.type=type;this.detail=init.detail;}
};
sb.window=sb;
vm.runInNewContext(source, sb);
const api=sb.OKFLoomTheme;
function snap() {return {choice:api.getChoice(), theme:api.getResolvedTheme(), scheme:api.getScheme(),
 attrs:{...attrs}, storage:{...store}, events:events.slice(), ready:classes.includes('okf-theme-ready')};}
const result=[snap()];
for(const a of opts.actions || []) {
 if(a.os != null) {mq.matches=a.os;osListener({matches:a.os});}
 else if(a.storage) {(listeners.storage || []).forEach(fn => fn(a.storage));}
 else if(a.boot) vm.runInNewContext(source,sb);
 else api[a.fn](...(a.args || []));
 result.push(snap());
}
console.log(JSON.stringify(result));
"""


def run(**opts):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for theme logic")
    proc = subprocess.run([node, "-e", HARNESS, json.dumps(opts), str(THEME)],
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("dark", [False, True])
def test_bone_default_ignores_os_and_paint_hint(dark):
    state = run(dark=dark, attrs={"data-theme": "plum"})[0]
    assert state["theme"] == state["choice"] == "bone"
    assert state["storage"] == {} and state["ready"]
    assert not any(k.startswith("data-okf-") and k != "data-okf-js" for k in state["attrs"])


@pytest.mark.parametrize("choice", ["bone", "slate", "clay", "plum", "system"])
def test_precedence(choice):
    configured = run(config={"theme": choice}, dark=True)[0]
    assert configured["choice"] == choice and configured["storage"] == {}
    assert configured["theme"] == ("slate" if choice == "system" else choice)
    assert run(config={"theme": choice}, bootstrap={"theme": "clay"})[0]["choice"] == "clay"
    assert run(config={"theme": choice}, bootstrap={"theme": "clay"},
               storage={KEY: "plum"})[0]["choice"] == "plum"


@pytest.mark.parametrize("legacy,expected", _LEGACY_THEME_MIGRATIONS.items())
@pytest.mark.parametrize("key", ["okf-theme", KEY])
def test_every_legacy_name(legacy, expected, key):
    state = run(storage={key: legacy})[0]
    assert state["choice"] == expected
    if key != KEY:
        assert state["storage"][KEY] == expected


@pytest.mark.parametrize("family", ["technical", "swiss", "coffee", "purple"])
@pytest.mark.parametrize("mode", ["light", "dark", "auto"])
def test_old_axes(family, mode):
    state = run(storage={"okf-theme-family": family, "okf-theme-mode": mode})[0]
    assert state["choice"] == ("system" if mode == "auto" else _LEGACY_THEME_MIGRATIONS[f"{family}-{mode}"])


@pytest.mark.parametrize("value", ["", "bogus", "__proto__", "toString", "null", "[]"])
def test_corrupt_new_key_blocks_stale_migration(value):
    state = run(storage={KEY: value, "okf-theme": "coffee-dark"}, config={"theme": "plum"})[0]
    assert state["choice"] == "plum" and state["storage"][KEY] == value


def test_corrupt_config_and_legacy_do_not_invent_preferences():
    assert run(config="{oops", bootstrap={"theme": []})[0]["choice"] == "bone"
    assert run(storage={"okf-theme-family": "bogus"})[0]["choice"] == "bone"


@pytest.mark.parametrize("deny_read", [False, True])
def test_denial_keeps_in_memory_choice(deny_read):
    states = run(denyRead=deny_read, denyWrite=True, config={"theme": "system"},
                 actions=[{"fn": "setTheme", "args": ["clay"]}, {"os": True}, {"fn": "reset"}])
    assert [s["theme"] for s in states] == ["bone", "clay", "clay", "slate"]
    assert all(s["storage"] == {} for s in states)


def test_reset_does_not_resurrect_legacy_on_reload():
    state = run(storage={"okf-theme": "coffee-dark", "okf-theme-family": "coffee", "okf-theme-mode": "dark"},
                config={"theme": "plum"}, actions=[{"fn": "reset"}])[-1]
    assert state["choice"] == "plum" and state["storage"] == {}
    assert run(storage=state["storage"], config={"theme": "plum"})[0]["choice"] == "plum"


def test_events_palette_not_scheme_dedupe_and_system_changes():
    states = run(attrs={"data-theme": "bone"}, actions=[
        {"fn": "setTheme", "args": ["system"]}, {"os": True}, {"os": True},
        {"fn": "setTheme", "args": ["clay"]}, {"fn": "setTheme", "args": ["clay"]},
        {"fn": "setTheme", "args": ["invalid"]}, {"os": False}, {"boot": True}])
    events = states[-1]["events"]
    assert len(events) == 2
    assert [(e["theme"], e["previous"], e["scheme"]) for e in events] == [
        ("slate", "bone", "dark"), ("clay", "slate", "dark")]
    assert [e["generation"] for e in events] == [1, 2]
    assert all(e["previousTheme"] == e["previous"] for e in events)
    assert states[-1]["storage"][KEY] == "clay"


def test_cross_tab_changes_are_inputs_not_writes():
    states = run(config={"theme": "plum"}, actions=[
        {"storage": {"key": KEY, "newValue": "system"}}, {"os": True},
        {"storage": {"key": "okf-theme", "newValue": "light"}},
        {"storage": {"key": KEY, "newValue": "bad"}},
        {"storage": {"key": KEY, "newValue": "clay"}},
        {"storage": {"key": None, "newValue": None}}])
    assert [s["theme"] for s in states] == ["plum", "bone", "slate", "slate", "plum", "clay", "plum"]
    assert all(s["storage"] == {} for s in states)
