"""Tests for the viewer plugin loader (current spec §15).

Pinned invariants:
  * ``CompositeViewerPlugin`` calls children in registration order.
  * A plugin that raises in ``on_concept_render`` is logged to stderr and
    skipped; the page still renders with the surviving plugins applied.
  * Active-code gate: when ``allow_active_code`` is False (the default), the
    composite is a complete no-op AND ``build_viewer_plugin`` does not even
    call entry-point discovery.
  * Entry-point discovery: plugins registered in the
    ``okf_loom.viewer_plugins`` group are loaded in registration order;
    entry points that fail to load OR do not expose ``on_concept_render``
    are skipped.
  * Server integration: a composite set on ``server.plugin`` transforms the
    concept-page HTML (and a raising plugin is skipped server-side).
"""
from __future__ import annotations

import socket
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.paths import concept_id_to_str
from okf_loom.viewer.plugins import (
    CompositeViewerPlugin,
    build_viewer_plugin,
    load_viewer_plugins,
)


# ---------------------------------------------------------------------------
# Test plugin doubles
# ---------------------------------------------------------------------------


class _StampPlugin:
    """Appends a marker comment so tests can detect the plugin ran."""

    def __init__(self, marker: str = "<!-- stamp -->") -> None:
        self.marker = marker

    def on_concept_render(self, concept, html: str) -> str:
        return html + self.marker

    def on_index_render(self, html: str) -> str:
        return html + self.marker


class _RaisingPlugin:
    """A plugin whose ``on_concept_render`` always raises."""

    def on_concept_render(self, concept, html: str) -> str:
        raise RuntimeError("boom")



@pytest.fixture
def consent_for_discovery(monkeypatch):
    """P2-9: load_viewer_plugins() now internally gates on operator consent
    (defense-in-depth). Tests that exercise discovery directly must opt in."""
    from okf_loom.viewer import assets
    monkeypatch.setenv(assets.OPERATOR_CONSENT_ENV, "1")
    assets._operator_consent_override = None
    assets.clear_overrides_cache()
    yield
    monkeypatch.delenv(assets.OPERATOR_CONSENT_ENV, raising=False)
    assets._operator_consent_override = None
    assets.clear_overrides_cache()



class _FixtureEntryPoint:
    """Minimal stand-in for ``importlib.metadata.EntryPoint`` for tests."""

    def __init__(self, name: str, target_obj, *, load_raises: bool = False) -> None:
        self.name = name
        self._target = target_obj
        self._load_raises = load_raises

    def load(self):
        if self._load_raises:
            raise RuntimeError("import failed")
        return self._target


# ---------------------------------------------------------------------------
# CompositeViewerPlugin direct behaviour
# ---------------------------------------------------------------------------


def test_composite_runs_plugins_in_registration_order() -> None:
    """Plugins are called in the order they were registered."""
    p1 = _StampPlugin("<!-- p1 -->")
    p2 = _StampPlugin("<!-- p2 -->")
    composite = CompositeViewerPlugin([p1, p2], allow_active_code=True)
    out = composite.on_concept_render(object(), "html")
    assert out == "html<!-- p1 --><!-- p2 -->"


def test_composite_skips_plugin_that_raises(capsys) -> None:
    """A raising plugin is logged and skipped; subsequent plugins still run."""
    good = _StampPlugin("<!-- good -->")
    composite = CompositeViewerPlugin(
        [_RaisingPlugin(), good], allow_active_code=True
    )
    out = composite.on_concept_render(object(), "html")
    assert out == "html<!-- good -->"
    err = capsys.readouterr().err
    assert "raised" in err
    assert "boom" in err


def test_composite_skips_raising_plugin_in_index_render(capsys) -> None:
    """``on_index_render`` has the same skip-on-raise behaviour."""
    class _BadIndex:
        def on_index_render(self, html: str) -> str:
            raise RuntimeError("index boom")

    class _GoodIndex:
        def on_index_render(self, html: str) -> str:
            return html + "<!-- idx-good -->"

    composite = CompositeViewerPlugin(
        [_BadIndex(), _GoodIndex()], allow_active_code=True
    )
    out = composite.on_index_render("html")
    assert out == "html<!-- idx-good -->"
    assert "index boom" in capsys.readouterr().err


def test_composite_noop_when_allow_active_code_false() -> None:
    """When the gate is closed, plugins do not run at all."""
    p = _StampPlugin("<!-- should not appear -->")
    composite = CompositeViewerPlugin([p], allow_active_code=False)
    assert composite.on_concept_render(object(), "html") == "html"
    assert composite.on_index_render("html") == "html"


def test_composite_skips_plugin_without_on_concept_render() -> None:
    """A plugin missing a hook is silently skipped for that hook (duck-typed)."""

    class _ConceptOnly:
        def on_concept_render(self, concept, html: str) -> str:
            return html + "<!-- concept -->"

    class _IndexOnly:
        def on_index_render(self, html: str) -> str:
            return html + "<!-- idx -->"

    composite = CompositeViewerPlugin(
        [_ConceptOnly(), _IndexOnly()], allow_active_code=True
    )
    # concept render: only the concept-only plugin runs.
    assert composite.on_concept_render(object(), "html") == "html<!-- concept -->"
    # index render: only the index-only plugin runs.
    assert composite.on_index_render("html") == "html<!-- idx -->"


# ---------------------------------------------------------------------------
# build_viewer_plugin: active-code gate via OkfConfig
# ---------------------------------------------------------------------------


def test_build_plugin_defaults_to_no_plugins_when_config_absent(
    tmp_path: Path,
) -> None:
    """No okf-loom.config.yaml → gate closed → no plugins loaded."""
    (tmp_path / "a.md").write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")
    composite = build_viewer_plugin(tmp_path)
    assert composite.allow_active_code is False
    assert composite.plugins == []


def test_build_plugin_reads_allow_active_code_from_config(tmp_path: Path) -> None:
    """``viewer.allow_active_code: true`` opens the gate."""
    from okf_loom.config import CONFIG_FILENAME

    (tmp_path / "a.md").write_text("---\ntype: T\n---\nbody\n", encoding="utf-8")
    (tmp_path / CONFIG_FILENAME).write_text(
        "viewer:\n  allow_active_code: true\n", encoding="utf-8"
    )
    composite = build_viewer_plugin(tmp_path)
    assert composite.allow_active_code is True


# ---------------------------------------------------------------------------
# Entry-point discovery (monkeypatched _entry_points_for)
# ---------------------------------------------------------------------------


def test_load_viewer_plugins_discovers_in_registration_order(monkeypatch, consent_for_discovery) -> None:
    """Plugins registered as entry points are loaded in order."""
    p1 = _StampPlugin("<!-- ep1 -->")
    p2 = _StampPlugin("<!-- ep2 -->")
    monkeypatch.setattr(
        "okf_loom.viewer.plugins._entry_points_for",
        lambda group: [
            _FixtureEntryPoint("first", p1),
            _FixtureEntryPoint("second", p2),
        ],
    )
    plugins = load_viewer_plugins()
    assert plugins == [p1, p2]


def test_load_viewer_plugins_accepts_class_entry_point(monkeypatch, consent_for_discovery) -> None:
    """An entry point pointing at a class is instantiated once."""

    class _MyPlugin:
        def __init__(self) -> None:
            self.called = 0

        def on_concept_render(self, concept, html: str) -> str:
            self.called += 1
            return html + "<!-- class -->"

    monkeypatch.setattr(
        "okf_loom.viewer.plugins._entry_points_for",
        lambda group: [_FixtureEntryPoint("my", _MyPlugin)],
    )
    plugins = load_viewer_plugins()
    assert len(plugins) == 1
    assert isinstance(plugins[0], _MyPlugin)


def test_load_viewer_plugins_skips_failing_load(monkeypatch, capsys, consent_for_discovery) -> None:
    """An entry point whose ``.load()`` raises is logged and skipped."""
    bad = _FixtureEntryPoint("bad", None, load_raises=True)
    good = _StampPlugin("<!-- good -->")
    monkeypatch.setattr(
        "okf_loom.viewer.plugins._entry_points_for",
        lambda group: [bad, _FixtureEntryPoint("good", good)],
    )
    plugins = load_viewer_plugins()
    assert plugins == [good]
    err = capsys.readouterr().err
    assert "failed to load" in err
    assert "bad" in err


def test_load_viewer_plugins_skips_non_plugin(monkeypatch, capsys, consent_for_discovery) -> None:
    """An entry point whose target lacks ``on_concept_render`` is skipped."""

    class _NotAPlugin:
        pass

    monkeypatch.setattr(
        "okf_loom.viewer.plugins._entry_points_for",
        lambda group: [_FixtureEntryPoint("bogus", _NotAPlugin())],
    )
    plugins = load_viewer_plugins()
    assert plugins == []
    assert "on_concept_render" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Server integration: concept-page render path applies plugins
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve_with_plugin(bundle: Bundle, plugin) -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    from okf_loom.server import OKFWikiHandler

    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), OKFWikiHandler)
    server.bundle = bundle  # type: ignore[attr-defined]
    server.config = {}  # type: ignore[attr-defined]
    server.name = bundle.name  # type: ignore[attr-defined]
    server.plugin = plugin  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


def test_server_concept_page_applies_plugin(tiny_good_bundle: Path) -> None:
    """A composite set on ``server.plugin`` transforms concept-page HTML."""
    bundle = Bundle.load(tiny_good_bundle)
    cid_str = concept_id_to_str(next(iter(bundle.concepts)))
    plugin = CompositeViewerPlugin(
        [_StampPlugin("<!-- plugin-ran -->")], allow_active_code=True
    )
    server, thread, port = _serve_with_plugin(bundle, plugin)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/{cid_str}", timeout=5
        ) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            assert "<!-- plugin-ran -->" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_concept_page_skips_raising_plugin(tiny_good_bundle: Path) -> None:
    """A raising plugin is skipped server-side; surviving plugins still apply."""
    bundle = Bundle.load(tiny_good_bundle)
    cid_str = concept_id_to_str(next(iter(bundle.concepts)))
    plugin = CompositeViewerPlugin(
        [_RaisingPlugin(), _StampPlugin("<!-- after-raise -->")],
        allow_active_code=True,
    )
    server, thread, port = _serve_with_plugin(bundle, plugin)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/{cid_str}", timeout=5
        ) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            # The good plugin ran AFTER the raising one was skipped.
            assert "<!-- after-raise -->" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_server_concept_page_noop_when_gate_closed(tiny_good_bundle: Path) -> None:
    """``allow_active_code=False`` → no plugin transformation on the page."""
    bundle = Bundle.load(tiny_good_bundle)
    cid_str = concept_id_to_str(next(iter(bundle.concepts)))
    plugin = CompositeViewerPlugin(
        [_StampPlugin("<!-- should not appear -->")], allow_active_code=False
    )
    server, thread, port = _serve_with_plugin(bundle, plugin)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/{cid_str}", timeout=5
        ) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            assert "<!-- should not appear -->" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Static build integration (render.build_site)
# ---------------------------------------------------------------------------


def test_build_site_applies_plugin_to_concept_pages(tiny_good_bundle: Path) -> None:
    """``build_site(target='static', plugin=...)`` runs the plugin on each page."""
    from okf_loom.render import build_site

    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site"
    plugin = CompositeViewerPlugin(
        [_StampPlugin("<!-- static-plugin -->")], allow_active_code=True
    )
    build_site(b, out_dir, target="static", plugin=plugin)
    cid = next(iter(b.concepts))
    rel = "/".join(cid) + ".html"
    content = (out_dir / rel).read_text(encoding="utf-8")
    assert "<!-- static-plugin -->" in content


def test_build_site_skips_raising_plugin(tiny_good_bundle: Path) -> None:
    """A raising plugin is skipped during static build; pages still written."""
    from okf_loom.render import build_site

    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site"
    plugin = CompositeViewerPlugin(
        [_RaisingPlugin(), _StampPlugin("<!-- after-raise -->")],
        allow_active_code=True,
    )
    build_site(b, out_dir, target="static", plugin=plugin)
    cid = next(iter(b.concepts))
    rel = "/".join(cid) + ".html"
    content = (out_dir / rel).read_text(encoding="utf-8")
    assert "<!-- after-raise -->" in content


def test_iter3_p1_2_composite_non_string_return_guarded(consent_for_discovery):
    """P1-2: a plugin returning a non-string (None/int/dict) must not crash
    the composite; the previous html is kept. Spec §12: viewer must never
    crash because of a plugin."""
    from okf_loom.viewer.plugins import CompositeViewerPlugin

    class ReturnsNone:
        def on_concept_render(self, concept, html):
            return None

    class ReturnsInt:
        def on_concept_render(self, concept, html):
            return 42

    for bad in (ReturnsNone(), ReturnsInt()):
        comp = CompositeViewerPlugin([bad], allow_active_code=True)
        out = comp.on_concept_render(None, "<p>orig</p>")
        assert out == "<p>orig</p>", f"non-string return corrupted html: {out!r}"
