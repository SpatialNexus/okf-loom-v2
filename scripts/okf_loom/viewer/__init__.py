"""OKF viewer runtime helpers.

Re-exports the top-level viewer entry points for convenience:

    from okf_loom.viewer import run_server, render_single_file, build_site

Asset/template/markdown helpers live in :mod:`okf_loom.viewer.assets` and
:mod:`okf_loom.viewer.markdown`. The bundled templates and static files
live under ``templates/`` and ``static/`` next to these scripts; see
``OVERRIDES.md`` for the bundle-level override contract.

Implementation note: the leaf modules (:mod:`viewer.assets`,
:mod:`viewer.markdown`) are imported eagerly because they have no upstream
dependency on the render/server modules. The render/server re-exports are
done lazily via ``__getattr__`` to avoid a circular import (``render.py``
imports from ``viewer.assets``; if ``viewer/__init__`` imported
``render`` at module load it would deadlock during the first import).
"""
from __future__ import annotations

# Leaf helpers — safe to import eagerly.
from .assets import (
    auto_palette,
    curated_type_color,
    curated_type_color_map,
    hsl_color,
    list_builtin_static,
    load_config,
    load_palette_override,
    load_static,
    load_template,
    resolve_palette,
    stable_hue,
    type_color_for,
)
from .markdown import markdown_to_html, rewrite_internal_links, url_for_concept
from .plugins import (
    CompositeViewerPlugin,
    ViewerPluginProtocol,
    build_viewer_plugin,
    load_viewer_plugins,
)

__all__ = [
    # Entry points (lazy via __getattr__)
    "run_server",
    "render_single_file",
    "build_site",
    "build_graph_data",
    "OKFWikiHandler",
    "ViewerPlugin",
    "NoOpPlugin",
    # Plugin loader (current spec §15)
    "CompositeViewerPlugin",
    "ViewerPluginProtocol",
    "build_viewer_plugin",
    "load_viewer_plugins",
    # Eager leaf helpers
    "load_template",
    "load_static",
    "load_config",
    "load_palette_override",
    "resolve_palette",
    "auto_palette",
    "stable_hue",
    "hsl_color",
    "curated_type_color",
    "curated_type_color_map",
    "type_color_for",
    "list_builtin_static",
    "markdown_to_html",
    "rewrite_internal_links",
    "url_for_concept",
]


def __getattr__(name: str):
    # Lazy re-exports of render.py / server.py to break the import cycle
    # (those modules import viewer.assets at their top).
    import importlib
    if name in {"render_single_file", "build_site", "build_graph_data"}:
        mod = importlib.import_module("..render", __package__)
        return getattr(mod, name)
    if name in {"run_server", "OKFWikiHandler", "ViewerPlugin", "NoOpPlugin"}:
        mod = importlib.import_module("..server", __package__)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
