"""Shared, target-honest application-shell renderer.

The URL/rendering layer owns the current destination.  Live Studio owns the
contents and state of the reserved Studio slot; portable targets receive the
same structural seam without advertising a capability they do not have.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Literal


ShellDestination = Literal["search", "index", "graph"]


def _brand_suffix_html(bundle_name: str) -> str:
    """Attribution after the bundle name, suppressed when it would duplicate.

    F13: a configured display name that already carries the ``okf-loom``
    brand (any casing) must not be followed by a second, muted
    ``okf-loom``; generically named bundles keep the attribution. The name
    itself stays escaped by the caller's own ``escape()`` interpolation;
    this returns either the static span or nothing.
    """
    if "okf-loom" in bundle_name.casefold():
        return ""
    return '<span class="okf-muted">okf-loom</span>'


@dataclass(frozen=True)
class ApplicationShell:
    bundle_name: str
    bundle_href: str
    search_href: str
    index_href: str
    graph_href: str
    current: ShellDestination | None
    studio_available: bool
    search_query: str = ""


def render_single_file_shell(bundle_name: str, appearance_html: str) -> str:
    """Render honest in-document destinations for the self-contained target."""
    return (
        '<header class="okf-topbar okf-shell" data-okf-application-shell>'
        '<div class="okf-topbar__brand okf-shell__brand">'
        f'<a href="#okf-single-index" class="okf-brand-link" data-okf-shell-item="bundle"><strong>{escape(bundle_name)}</strong></a>'
        + _brand_suffix_html(bundle_name) + '</div>'
        '<nav class="okf-topbar__controls okf-global-nav okf-shell__nav" aria-label="Primary navigation">'
        '<span data-okf-shell-item="search"><button id="okf-single-search-toggle" class="okf-btn" type="button" '
        'aria-expanded="false" aria-controls="okf-single-search">Search</button></span>'
        '<span data-okf-shell-item="index"><a class="okf-btn" href="#okf-single-index" aria-current="page">Index</a></span>'
        '<span data-okf-shell-item="graph"><a class="okf-btn" href="#okf-single-graph-fallback">Graph</a></span>'
        '<span class="okf-shell__studio-slot" data-okf-shell-item="studio" '
        'data-okf-studio-capability="unavailable" hidden></span>'
        '<span class="okf-shell__appearance" data-okf-shell-item="appearance">'
        + appearance_html + '</span></nav></header>'
    )


def render_application_shell(model: ApplicationShell, appearance_html: str) -> str:
    """Render the one canonical shell in stable keyboard and visual order."""

    def destination(name: ShellDestination, label: str, href: str) -> str:
        current = ' aria-current="page"' if model.current == name else ""
        return (
            f'<span data-okf-shell-item="{name}">'
            f'<a class="okf-btn" href="{escape(href, quote=True)}"{current}>{label}</a></span>'
        )

    capability = "available" if model.studio_available else "unavailable"
    # The live server supplies its actual Studio attachment state. Inert
    # first-paint geometry is revealed only with JavaScript; Studio replaces
    # it at boot. Inactive sessions, portable and no-JS targets reserve none.
    studio_placeholder = (
        '<button type="button" class="okf-studiobtn okf-studio-launcher" '
        'disabled tabindex="-1" aria-hidden="true">'
        '<span class="okf-studio-launcher__label">Studio</span>'
        '<span class="okf-studio-launcher__summary"></span></button>'
        if model.studio_available else ""
    )
    search_value = (
        f' value="{escape(model.search_query, quote=True)}"' if model.search_query else ""
    )
    # One header search control: the real GET form IS the search destination.
    # There is no separate Search shortcut link and no generic "Go" label;
    # submitting (button or Enter) navigates to the target-honest search
    # route, and an empty query opens the search page itself. When the search
    # page is the current destination the form carries aria-current="page"
    # so the active-route context stays machine-readable without resurrecting
    # a redundant link.
    search_current = ' aria-current="page"' if model.current == "search" else ""
    search = (
        f'<form action="{escape(model.search_href, quote=True)}" method="get" role="search" '
        f'class="okf-search-form okf-shell__search-form" data-okf-shell-item="search"{search_current}>'
        '<input type="search" name="q" placeholder="Search…" autocomplete="off" '
        f'aria-label="Search this bundle"{search_value}>'
        '<button type="submit" class="okf-shell__search-submit">Search</button></form>'
    )
    return (
        '<header class="okf-topbar okf-shell" data-okf-application-shell>'
        '<div class="okf-topbar__brand okf-shell__brand">'
        f'<a href="{escape(model.bundle_href, quote=True)}" class="okf-brand-link" '
        f'data-okf-shell-item="bundle"><strong>{escape(model.bundle_name)}</strong></a>'
        + _brand_suffix_html(model.bundle_name) + '</div>'
        '<nav class="okf-topbar__controls okf-global-nav okf-shell__nav" '
        'aria-label="Primary navigation">'
        + search
        + destination("index", "Index", model.index_href)
        + destination("graph", "Graph", model.graph_href)
        + f'<span class="okf-shell__studio-slot" data-okf-shell-item="studio" '
          f'data-okf-studio-capability="{capability}" hidden>{studio_placeholder}</span>'
        + '<span class="okf-shell__appearance" data-okf-shell-item="appearance">'
        + appearance_html
        + '</span></nav></header>'
    )
