"""Public rendering contracts for the shared application shell."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from okf_loom import Bundle
from okf_loom.render import build_site, render_single_file


class _ShellProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.items: list[tuple[str, dict[str, str | None]]] = []
        self.item_tags: dict[str, str] = {}
        self.current_links: list[str] = []
        self._item: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        role = values.get("data-okf-shell-item")
        if role:
            self.items.append((role, values))
            self.item_tags[role] = tag
            self._item = role
        if tag in {"a", "button", "form"} and values.get("aria-current") == "page" and self._item:
            self.current_links.append(self._item)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"span", "form"}:
            self._item = None


def _probe(path: Path) -> _ShellProbe:
    probe = _ShellProbe()
    probe.feed(path.read_text(encoding="utf-8"))
    return probe


def test_shared_shell_has_stable_order_and_one_active_destination(
    tiny_good_bundle: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "site"
    build_site(Bundle.load(tiny_good_bundle), output, target="static")

    pages = {
        "index": output / "index.html",
        "concept": output / "tables" / "users.html",
        "search": output / "__search.html",
        "graph": output / "__graph.html",
    }
    expected_active = {
        "index": "index",
        "concept": "index",
        "search": "search",
        "graph": "graph",
    }

    for route, path in pages.items():
        probe = _probe(path)
        assert [name for name, _ in probe.items] == [
            "bundle", "search", "index", "graph", "studio", "appearance",
        ], route
        assert probe.current_links == [expected_active[route]], route


def test_portable_shell_reserves_but_does_not_advertise_studio(
    tiny_good_bundle: Path, tmp_path: Path,
) -> None:
    for target in ("static", "spa"):
        output = tmp_path / target
        build_site(Bundle.load(tiny_good_bundle), output, target=target)
        html = (output / "index.html").read_text(encoding="utf-8")
        probe = _probe(output / "index.html")
        studio = dict(probe.items)["studio"]
        assert "hidden" in studio
        assert studio.get("data-okf-studio-capability") == "unavailable"
        assert ">Studio<" not in html
        for page in output.rglob("*.html"):
            assert "data-okf-viewbar-placeholder" not in page.read_text(encoding="utf-8")


def test_inactive_live_shell_does_not_emit_launcher():
    from okf_loom.render import _application_shell_html
    html = _application_shell_html(
        name="Proof", bundle_href="/", search_href="/__search", index_href="/",
        graph_href="/__graph", current="index", mode="serve", studio_available=False,
    )
    assert 'data-okf-studio-capability="unavailable"' in html
    assert 'okf-studio-launcher' not in html


def test_live_shell_emits_only_inert_launcher_placeholder():
    from okf_loom.render import _application_shell_html

    html = _application_shell_html(
        name="Proof", bundle_href="/", search_href="/__search", index_href="/",
        graph_href="/__graph", current="index", mode="serve",
    )
    probe = _ShellProbe()
    probe.feed(html)
    studio = dict(probe.items)["studio"]
    assert studio["data-okf-studio-capability"] == "available" and "hidden" in studio
    assert 'class="okf-studiobtn okf-studio-launcher" disabled tabindex="-1" aria-hidden="true"' in html
    assert 'class="okf-studio-launcher__label">Studio</span>' in html


def test_single_file_uses_target_honest_shared_shell(
    tiny_good_bundle: Path, tmp_path: Path,
) -> None:
    output = tmp_path / "bundle.html"
    render_single_file(Bundle.load(tiny_good_bundle), output)
    html = output.read_text(encoding="utf-8")
    probe = _probe(output)

    assert "data-okf-application-shell" in html
    assert [name for name, _ in probe.items[:6]] == [
        "bundle", "search", "index", "graph", "studio", "appearance",
    ]
    assert probe.current_links == ["index"]
    assert 'href="#okf-single-index" class="okf-skip-link"' in html
    assert 'href="#okf-single-index" class="okf-brand-link"' in html
    assert 'href="#okf-single-graph-fallback">Graph</a>' in html
    assert 'id="okf-single-graph-fallback"' in html
    studio = dict(probe.items)["studio"]
    assert studio.get("data-okf-studio-capability") == "unavailable"
    assert ".okf-shell__nav" in html


def test_graph_shell_keeps_search_destination_without_javascript(
    tiny_good_bundle: Path, tmp_path: Path,
) -> None:
    for target in ("static", "spa"):
        output = tmp_path / target
        build_site(Bundle.load(tiny_good_bundle), output, target=target)
        html = (output / "__graph.html").read_text(encoding="utf-8")
        probe = _probe(output / "__graph.html")
        # The real GET form is the search destination: it is the one
        # data-okf-shell-item="search" control, it submits to the honest
        # search route, and it offers a clearly labelled submit button even
        # with JavaScript disabled.
        assert probe.item_tags["search"] == "form"
        search_form = dict(probe.items)["search"]
        assert search_form.get("role") == "search"
        assert search_form.get("method") == "get"
        assert "__search" in (search_form.get("action") or "")
        assert '<button type="submit" class="okf-shell__search-submit">Search</button>' in html
        # The former standalone Search shortcut link is gone.
        assert ">Search</a>" not in html
        assert 'class="okf-shell__search-destination"' not in html
        assert "not([data-okf-js=\"1\"]) .okf-viewer--graph .okf-shell" not in html


def test_shared_shell_has_one_search_form_and_no_duplicate_shortcut(
    tiny_good_bundle: Path, tmp_path: Path,
) -> None:
    for target in ("static", "spa"):
        output = tmp_path / target
        build_site(Bundle.load(tiny_good_bundle), output, target=target)
        for route in ("index.html", "tables/users.html", "__search.html", "__graph.html"):
            html = (output / route).read_text(encoding="utf-8")
            assert html.count('data-okf-shell-item="search"') == 1, route
            assert ">Search</a>" not in html, route
            assert ">Go</button>" not in html, route
            assert '<button type="submit" class="okf-shell__search-submit">Search</button>' in html, route
            assert '<input type="search" name="q"' in html, route


def test_shell_brand_suffix_deduplicates_branded_titles_and_escapes_names(
    tiny_good_bundle: Path, tmp_path: Path,
) -> None:
    """F13: the muted attribution follows the bundle name only when the name
    does not already carry the brand. An already-branded title ("okf-loom
    Documentation") renders once; a generically named bundle keeps the
    attribution; a hostile branded name stays HTML-escaped in both shells.
    """
    from okf_loom.viewer.shell import (
        ApplicationShell,
        render_application_shell,
        render_single_file_shell,
    )

    branded = "okf-loom Documentation"
    # Live/shared shell: branded title carries the brand exactly once...
    shell = render_application_shell(
        ApplicationShell(
            bundle_name=branded, bundle_href="/", search_href="/__search",
            index_href="/", graph_href="/__graph", current="index",
            studio_available=True,
        ),
        "",
    )
    assert shell.count("okf-loom Documentation") == 1
    assert '<span class="okf-muted">okf-loom</span>' not in shell, (
        "an already-branded title must not receive a duplicate attribution"
    )
    # ...while a generic title keeps it.
    generic = render_application_shell(
        ApplicationShell(
            bundle_name="Acme Field Notes", bundle_href="/",
            search_href="/__search", index_href="/", graph_href="/__graph",
            current=None, studio_available=False,
        ),
        "",
    )
    assert '<span class="okf-muted">okf-loom</span>' in generic
    # The single-file shell follows the same rule (its brand link targets
    # the in-document index).
    single_branded = render_single_file_shell(branded, "")
    assert single_branded.count("okf-loom Documentation") == 1
    assert '<span class="okf-muted">okf-loom</span>' not in single_branded
    single_generic = render_single_file_shell("Acme Field Notes", "")
    assert '<span class="okf-muted">okf-loom</span>' in single_generic
    # Cased brand tokens still count as branded (no second attribution)...
    cased = render_application_shell(
        ApplicationShell(
            bundle_name="OKF-Loom Handbook", bundle_href="/",
            search_href="/__search", index_href="/", graph_href="/__graph",
            current=None, studio_available=False,
        ),
        "",
    )
    assert '<span class="okf-muted">okf-loom</span>' not in cased
    # ...and a hostile branded name is still attribute/body-escaped; the
    # suppression must not open an injection path.
    hostile = 'okf-loom"><script>alert(1)</script>'
    hostile_shell = render_application_shell(
        ApplicationShell(
            bundle_name=hostile, bundle_href="/", search_href="/__search",
            index_href="/", graph_href="/__graph", current=None,
            studio_available=False,
        ),
        "",
    )
    assert "<script>" not in hostile_shell
    assert "&lt;script&gt;" in hostile_shell


def test_shell_search_current_destination_and_query_escaping(
    tiny_good_bundle: Path,
) -> None:
    from okf_loom.render import _application_shell_html

    hostile_query = '"><script>alert(1)</script>'
    shell = _application_shell_html(
        name="tiny_good", bundle_href="/", search_href="/__search", index_href="/",
        graph_href="/__graph", current="search", mode="serve",
        appearance_choice="bone", search_query=hostile_query,
    )
    probe = _ShellProbe()
    probe.feed(shell)
    # The search route's current destination is the form itself.
    assert probe.current_links == ["search"]
    search_form = dict(probe.items)["search"]
    assert search_form.get("aria-current") == "page"
    # The echoed query is attribute-escaped and never breaks out of value="".
    assert 'value="&quot;&gt;&lt;script&gt;' in shell
    assert "<script>" not in shell
    # A non-search route does not mark the form as the current destination.
    plain = _application_shell_html(
        name="tiny_good", bundle_href="/", search_href="/__search", index_href="/",
        graph_href="/__graph", current=None, mode="serve",
        appearance_choice="bone",
    )
    plain_probe = _ShellProbe()
    plain_probe.feed(plain)
    assert plain_probe.current_links == []
    assert 'aria-current' not in dict(plain_probe.items)["search"]


def test_shell_stylesheet_override_has_documented_precedence_in_every_output(
    tiny_good_bundle: Path, tmp_path: Path, monkeypatch,
) -> None:
    from okf_loom.config import CONFIG_FILENAME
    from okf_loom.viewer import assets

    (tiny_good_bundle / CONFIG_FILENAME).write_text(
        "viewer:\n  allow_active_code: true\n", encoding="utf-8",
    )
    override = tiny_good_bundle / ".okf-loom" / "viewer" / "static"
    override.mkdir(parents=True)
    marker = "/* SHELL OVERRIDE PRECEDENCE */"
    (override / "shell.css").write_text(marker, encoding="utf-8")
    monkeypatch.setenv(assets.OPERATOR_CONSENT_ENV, "1")
    assets._operator_consent_override = None
    assets.clear_overrides_cache()
    try:
        output = tmp_path / "site"
        build_site(Bundle.load(tiny_good_bundle), output, target="static")
        for page in (
            output / "index.html", output / "__search.html",
            output / "__graph.html", output / "tables" / "users.html",
        ):
            assert "shell.css" in page.read_text(encoding="utf-8")
        shell_assets = list(output.rglob("shell.css"))
        assert shell_assets and any(marker in path.read_text(encoding="utf-8") for path in shell_assets)

        single = tmp_path / "single.html"
        render_single_file(Bundle.load(tiny_good_bundle), single)
        assert marker in single.read_text(encoding="utf-8")
    finally:
        assets.clear_overrides_cache()
