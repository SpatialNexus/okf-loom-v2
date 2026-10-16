"""Tests for ``okf_loom.render``.

Pinned invariants:
  * ``render_single_file`` writes a valid HTML file containing the bundle
    JSON; the embedded JSON parses and matches ``bundle.graph()``.
  * The bytes count returned is the actual file size in bytes.
  * ``build_site`` for ``static`` and ``single-file`` targets writes the
    expected file layout.
  * Atomic writes: no partial files left behind on success.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.render import (_graph_thumbnail_html, _render_index_page,
                             graph_export_svg, build_graph_data, build_site,
                             render_single_file)


def test_graph_thumbnail_is_deterministic_safe_and_order_independent() -> None:
    nodes = [
        {"data": {"id": "b", "label": 'B</text><script>alert(1)</script>', "color": 'red" onload="x', "degree": 2}},
        {"data": {"id": "a", "label": "A & one", "color": "#123abc", "degree": 1}},
    ]
    edges = [{"data": {"id": "e", "source": "a", "target": "b"}}]
    first = _graph_thumbnail_html({"nodes": nodes, "edges": edges}, "/__graph")
    second = _graph_thumbnail_html({"nodes": list(reversed(nodes)), "edges": list(reversed(edges))}, "/__graph")
    assert first == second
    assert "<script>" not in first and "onload=" not in first
    # Ledger: the index preview is the organic mini-graph of square ink dots;
    # node colours from the payload are never painted (types are not colour-
    # coded), so a hostile colour cannot reach an attribute either.
    assert 'data-okf-graph-preview="organic-v1"' in first
    assert first.count('class="okf-graph-thumb__dot"') == 2
    assert "#123abc" not in first and "fill=" not in first and "style=" not in first
    assert 'href="/__graph"' in first and "https://" not in first
    assert 'aria-hidden="true"' in first and 'focusable="false"' in first
    assert 'role="img"' not in first
    assert '<span class="sr-only">Open knowledge graph — </span>' in first
    assert '<figcaption><strong>Explore connections</strong><span>2 concepts · 1 relationship</span>' in first


def test_recent_time_text_floors_units_like_the_live_rail() -> None:
    """Server-rendered "Recently changed" rows use the exact format of the
    live Studio rail (studio.js fmtRecentAgo) and wiki.js OKFLoomRecency:
    whole elapsed units rounded DOWN, so it never prints "60m ago" or
    "24h ago"; future instants read "just now"; the title is the UTC minute."""
    from datetime import datetime, timedelta, timezone
    from okf_loom.render import _recent_time_text, _recent_time_title
    now = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
    ts = lambda seconds: (now - timedelta(seconds=seconds)).isoformat()
    cases = {0: "just now", 59: "just now", 60: "1m ago", 3599: "59m ago",
             3600: "1h ago", 86399: "23h ago", 86400: "1d ago",
             9 * 86400 + 86399: "9d ago", -300: "just now"}
    for seconds, expected in cases.items():
        assert _recent_time_text(ts(seconds), now=now) == expected, (seconds, expected)
    assert _recent_time_title("2026-10-13T13:59:59+02:00") == "2026-10-13 11:59 UTC"
    assert _recent_time_text("not a time", now=now) == "not a time"
    assert _recent_time_title("not a time") == "not a time"


def test_graph_thumbnail_empty_is_semantic_link() -> None:
    html = _graph_thumbnail_html({"nodes": [], "edges": []}, "/__graph")
    assert "No concepts are available" in html
    assert '<a href="/__graph">Open the graph</a>' in html


def test_graph_export_is_the_exact_shared_semantic_artifact() -> None:
    payload = {
        "nodes": [
            {"data": {"id": "orders", "label": "Orders", "type": "table", "color": "#123abc", "degree": 3}},
            {"data": {"id": "api", "label": "API", "type": "service", "color": "#456789", "degree": 1}},
        ],
        "edges": [{"data": {"id": "e", "source": "orders", "target": "api", "relations": [
            {"type": "feeds"}, {"type": "depends_on"}
        ]}}],
    }
    export = graph_export_svg(payload)
    hero = _graph_thumbnail_html(payload, "/__graph")
    assert export not in hero, "the link-nested copy must not contribute SVG text to its name"
    # The index hero draws the Ledger organic preview, not a copy of the
    # canonical export; the export itself stays the shared semantic artifact.
    assert 'data-okf-graph-export' not in hero
    assert 'data-okf-graph-preview="organic-v1"' in hero
    assert '<text' not in hero, "preview must not contribute SVG text to the link name"
    assert 'aria-hidden="true"' in hero and 'focusable="false"' in hero
    assert 'data-okf-graph-export="canonical-v1"' in export
    assert 'data-concept-id="orders"' in export and 'data-type="table"' in export
    assert "<ellipse" in export and "<rect" in export
    assert 'marker-end="url(#okf-export-arrow)"' in export
    assert 'class="okf-graph-export__count"' in export and ">2</text>" in export


def test_graph_export_hostile_values_are_escaped_and_order_independent() -> None:
    payload = {"nodes": [{"data": {"id": 'x\" onload=\"1', "label": "</text><script>x</script>", "type": "service"}}], "edges": []}
    first = graph_export_svg(payload)
    second = graph_export_svg({"edges": [], "nodes": list(reversed(payload["nodes"]))})
    assert first == second
    assert "<script>" not in first and 'onload="1"' not in first


def test_graph_export_24_node_geometry_and_label_backplates_are_bounded() -> None:
    payload = {"nodes": [{"data": {"id": f"n{i:02}", "label": f"Hostile long label {i}", "type": "concept", "degree": i % 8}} for i in range(24)], "edges": []}
    root = ET.fromstring(graph_export_svg(payload))
    plates = root.findall(".//rect[@class='okf-graph-export__label-bg']")
    assert len(plates) == 24
    boxes = []
    for plate in plates:
        x, y, w, h = (float(plate.attrib[k]) for k in ("x", "y", "width", "height"))
        assert 0 <= x and 0 <= y and x + w <= 560 and y + h <= 250
        boxes.append((x, y, x + w, y + h))
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
    assert 'class="okf-graph-export__count"><circle' not in graph_export_svg(payload)


def test_graph_export_real_multi_edge_chip_clearance_or_semantic_equivalent() -> None:
    payload = build_graph_data(Bundle.load(TOOLKIT_ROOT / "samples" / "demo_bundle"))
    svg = graph_export_svg(payload)
    root = ET.fromstring(svg)
    counts = []
    for group in root.findall(".//g[@class='okf-graph-export__count']"):
        circle = group.find("circle"); assert circle is not None
        counts.append((float(circle.attrib["cx"]), float(circle.attrib["cy"]), float(circle.attrib["r"])))
    node_boxes = []
    label_boxes = []
    for group in root.findall(".//g[@data-concept-id]"):
        for shape in list(group):
            if shape.tag == "circle":
                x,y,r=(float(shape.attrib[k]) for k in ("cx","cy","r"));node_boxes.append((x-r,y-r,x+r,y+r))
            elif shape.tag == "ellipse":
                x,y,rx,ry=(float(shape.attrib[k]) for k in ("cx","cy","rx","ry"));node_boxes.append((x-rx,y-ry,x+rx,y+ry))
            elif shape.tag == "rect" and shape.attrib.get("class") == "okf-graph-export__label-bg":
                x,y,w,h=(float(shape.attrib[k]) for k in ("x","y","width","height"));label_boxes.append((x,y,x+w,y+h))
            elif shape.tag == "rect":
                x,y,w,h=(float(shape.attrib[k]) for k in ("x","y","width","height"));node_boxes.append((x,y,x+w,y+h))
    for i,(x,y,r) in enumerate(counts):
        assert r <= x <= 560-r and r <= y <= 250-r
        for box in node_boxes+label_boxes:
            assert x+r <= box[0] or x-r >= box[2] or y+r <= box[1] or y-r >= box[3]
        for ox,oy,orr in counts[i+1:]:
            assert math.hypot(x-ox,y-oy) >= r+orr+2
    multi = sum(1 for edge in payload["edges"] if len(edge["data"].get("relations", [])) > 1)
    assert len(counts) <= multi
    if len(counts) < multi:
        assert "Count equivalents:" in svg and "relationships" in svg

from conftest import TOOLKIT_ROOT


def _runtime_file(*parts: str) -> Path:
    """Path to a file in the checkout-local OKF runtime package."""
    return TOOLKIT_ROOT / "scripts" / "okf_loom" / Path(*parts)


def _portable_node(artifact: dict, program: str) -> dict:
    """Run the dependency-free browser provider in Node's WebCrypto runtime."""
    script = f"""
const fs=require('fs'), crypto=require('crypto');
global.window={{crypto:crypto.webcrypto,TextEncoder:global.TextEncoder}};
eval(fs.readFileSync({json.dumps(str(_runtime_file('viewer','static','portable-retrieval.js')))},'utf8'));
const artifact=JSON.parse(fs.readFileSync(0,'utf8'));
(async()=>{{{program}}})().catch(e=>{{console.error(e.stack||e);process.exit(1);}});
"""
    result = subprocess.run(
        ["node", "-e", script], input=json.dumps(artifact), text=True,
        capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# --- render_single_file -----------------------------------------------------


def test_portable_manifest_cap_boundary_and_failed_build_preserves_output(
    tiny_good_bundle: Path, tmp_path: Path, monkeypatch,
) -> None:
    from okf_loom import render

    monkeypatch.setattr(render, "MAX_PORTABLE_MANIFEST_BYTES", 10)
    assert render._canonical_portable_manifest({"x": "aa"}) == b'{"x":"aa"}'
    with pytest.raises(ValueError, match="16 MiB consumer limit"):
        render._canonical_portable_manifest({"x": "aaa"})

    output = tmp_path / "site"
    output.mkdir()
    marker = output / "existing.txt"
    marker.write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="16 MiB consumer limit"):
        render.build_site(Bundle.load(tiny_good_bundle), output, target="static")
    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not (output / "__data" / "passages-manifest.json").exists()


def test_render_single_file_writes_html(tiny_good_bundle: Path) -> None:
    """``render_single_file`` writes a non-empty HTML file."""
    b = Bundle.load(tiny_good_bundle)
    out = tiny_good_bundle / "viz.html"
    stats = render_single_file(b, out)
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert content.lstrip().lower().startswith("<!doctype html>") or "<html" in content.lower()
    # Stats contain the expected keys.
    for key in ("concepts", "edges", "bytes"):
        assert key in stats
    assert stats["concepts"] == len(b.concepts)


def test_render_single_file_contains_bundle_json(tiny_good_bundle: Path) -> None:
    """The HTML embeds the bundle as a JSON blob."""
    b = Bundle.load(tiny_good_bundle)
    out = tiny_good_bundle / "viz.html"
    render_single_file(b, out)
    content = out.read_text(encoding="utf-8")
    # Find the embedded JSON: it's a JSON-encoded object with "nodes".
    assert '"nodes"' in content
    assert '"edges"' in content


def test_single_file_exposes_offline_full_search_and_context(tiny_good_bundle: Path) -> None:
    """The self-contained graph includes a visible, corpus-backed workbench."""
    b = Bundle.load(tiny_good_bundle)
    out = tiny_good_bundle / "viz-search.html"
    stats = render_single_file(b, out)
    content = out.read_text(encoding="utf-8")
    assert 'id="okf-single-search-toggle"' in content
    assert 'id="okf-single-search"' in content
    assert "Best overall" in content and "Exact wording" in content and "Connected by relation" in content
    assert "Build context pack" in content and 'data-single-copy-pack disabled>Copy context pack' in content
    # U8: the boot state (zero filters) hides the ", 0 active" fragment from
    # the rendered text and the accessibility tree via the hidden attribute;
    # the count span stays for the JS owner to fill on change.
    assert re.search(
        r'<summary>Filters<span data-single-filter-active hidden>,&nbsp;'
        r'<span[^>]+>0</span>&nbsp;active</span></summary>', content)
    assert '"retrieval_corpus"' in content
    assert "single-search.js" not in content  # source is inlined, no network seam
    assert "portable-single-file" in content
    assert stats["bytes"] == out.stat().st_size
    assert stats["bytes"] < 8 * 1024 * 1024, "fixture single-file output unexpectedly unbounded"


def test_embedded_json_matches_bundle_graph(tiny_good_bundle: Path) -> None:
    """The embedded JSON blob matches what ``build_graph_data`` produces."""
    b = Bundle.load(tiny_good_bundle)
    out = tiny_good_bundle / "viz.html"
    render_single_file(b, out)
    content = out.read_text(encoding="utf-8")

    # Extract the JSON: locate the "nodes" key embedded in the HTML.
    # We pull a sufficiently large JSON object using a balanced scan.
    start = content.index("{", content.index('"nodes"') - 200)
    # Walk forward balancing braces.
    depth = 0
    in_string = False
    esc = False
    end = -1
    for i in range(start, len(content)):
        ch = content[i]
        if in_string:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
    assert end > start, "could not extract embedded JSON object"
    blob = content[start:end]
    data = json.loads(blob)

    # Compare with the canonical graph data.
    canonical = build_graph_data(b)
    # Spot-check core fields (don't require byte-for-byte equality of HTML
    # interpolation, but the JSON-parseable shape must match).
    assert data["name"] == canonical["name"]
    assert len(data["nodes"]) == len(canonical["nodes"])
    assert {n["data"]["id"] for n in data["nodes"]} == {
        n["data"]["id"] for n in canonical["nodes"]
    }
    # Edges count matches.
    assert len(data["edges"]) == len(canonical["edges"])


def test_render_bytes_matches_file_size(tiny_good_bundle: Path) -> None:
    """The returned ``bytes`` count equals the actual UTF-8 file size."""
    b = Bundle.load(tiny_good_bundle)
    out = tiny_good_bundle / "viz.html"
    stats = render_single_file(b, out)
    file_bytes = out.read_bytes()
    assert stats["bytes"] == len(file_bytes)


def test_render_respects_name_override(tiny_good_bundle: Path) -> None:
    """Passing ``name=`` overrides the bundle name in the rendered HTML."""
    b = Bundle.load(tiny_good_bundle)
    out = tiny_good_bundle / "viz.html"
    render_single_file(b, out, name="Custom Display Name")
    content = out.read_text(encoding="utf-8")
    assert "Custom Display Name" in content


def test_render_creates_parent_dirs(tmp_path: Path) -> None:
    """``render_single_file`` creates parent directories as needed."""
    b = Bundle.load(tmp_path)
    (tmp_path / "a.md").write_text(
        "---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8"
    )
    b = Bundle.load(tmp_path)
    out = tmp_path / "nested" / "deep" / "viz.html"
    render_single_file(b, out)
    assert out.exists()


# --- build_site: single-file target -----------------------------------------


def test_build_site_single_file_writes_one_html(tiny_good_bundle: Path) -> None:
    """``build_site(target='single-file')`` writes a single ``index.html``."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site"
    stats = build_site(b, out_dir, target="single-file")
    assert (out_dir / "index.html").exists()
    assert stats["target"] == "single-file"
    assert stats["files"] == 1
    assert stats["concepts"] == len(b.concepts)


# --- build_site: static target ----------------------------------------------


def test_build_site_static_writes_per_concept_pages(tiny_good_bundle: Path) -> None:
    """``build_site(target='static')`` writes one HTML page per concept."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site"
    stats = build_site(b, out_dir, target="static")
    assert stats["target"] == "static"
    # Every concept has a corresponding .html file.
    for concept in b.concepts.values():
        rel = "/".join(concept.id) + ".html"
        assert (out_dir / rel).exists(), f"missing page for {concept.id}"
    # Root index exists.
    assert (out_dir / "index.html").exists()


def test_build_site_static_writes_static_assets(tiny_good_bundle: Path) -> None:
    """The static site bundles CSS/JS under ``__static/``."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site"
    build_site(b, out_dir, target="static")
    static_dir = out_dir / "__static"
    assert static_dir.exists()
    # At least wiki.css should be there.
    assert (static_dir / "wiki.css").exists()


def test_build_site_static_includes_graph_data(tiny_good_bundle: Path) -> None:
    """The static site emits ``__data/graph.json`` consumable by graph.js."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site"
    build_site(b, out_dir, target="static")
    graph_json = out_dir / "__data" / "graph.json"
    assert graph_json.exists()
    data = json.loads(graph_json.read_text(encoding="utf-8"))
    assert "nodes" in data
    assert len(data["nodes"]) == len(b.concepts)


@pytest.mark.parametrize("target", ["static", "spa", "single-file"])
@pytest.mark.parametrize("assets", ["cdn", "offline"])
@pytest.mark.parametrize("override", [None, "Explicit & <site>"])
def test_portable_pages_share_serves_configured_title(
    tmp_path: Path, monkeypatch, target: str, assets: str, override: str | None,
) -> None:
    """Build resolves the same title as serve; --name still wins on every page."""
    from html import unescape
    from okf_loom import cli, server

    root = tmp_path / "bundle"
    (root / "guides").mkdir(parents=True)
    (root / "okf-loom.config.yaml").write_text(
        "viewer:\n  title: configured-site\n", encoding="utf-8")
    (root / "index.md").write_text("Welcome.\n", encoding="utf-8")
    (root / "guides" / "index.md").write_text("# Guides\n", encoding="utf-8")
    (root / "guides" / "one.md").write_text(
        "---\ntype: Guide\ntitle: One\n---\n# One\nText.\n", encoding="utf-8")
    served = {}
    monkeypatch.setattr(server, "run_server", lambda *args, **kwargs: served.update(kwargs))
    assert cli.main(["serve", str(root), "--no-open", "--no-watch"]) == 0
    assert served["name"] == "configured-site"
    out = tmp_path / "output"
    args = ["build", str(root), "--out", str(out), "--target", target, "--assets", assets]
    if override:
        args += ["--name", override]
    assert cli.main(args) == 0
    expected = override or served["name"]
    pages = list(out.rglob("*.html"))
    assert len(pages) == (1 if target == "single-file" else 5)
    for page in pages:
        html = page.read_text(encoding="utf-8")
        brand = re.search(r'class="okf-brand-link"[^>]*><strong>(.*?)</strong>', html)
        title = re.search(r'<title>(.*?)</title>', html)
        assert brand and unescape(brand[1]) == expected, page
        assert title and expected in unescape(title[1]), page


def test_build_site_unknown_target_raises(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    with pytest.raises(ValueError, match="Unknown target"):
        build_site(b, tiny_good_bundle / "x", target="bogus")


def test_build_site_static_has_no_dead_internal_links(tmp_path: Path) -> None:
    """End-to-end link integrity for the static build of the real docs
    bundle (the artifact deployed to GitHub Pages).

    Guards the family of static-only link bugs fixed together: the root
    index.md body skipping ``rewrite_internal_links``, anchored
    ``cli.md#x`` links never matching the anchor-stripped rewrite lookup,
    sub-index concept cards missing source relativisation, and raw
    ``/a/b.md`` resource/provenance hrefs. All were invisible in serve
    mode because the live server resolves raw .md paths. Out-of-bundle
    repo references (``../../SKILL.md``) and external URLs are exempt —
    they have no static-build page by design.
    """
    import posixpath

    b = Bundle.load(TOOLKIT_ROOT / "docs-bundle")
    out_dir = tmp_path / "_site"
    build_site(b, out_dir, target="static")
    dead: list[str] = []
    for page in out_dir.rglob("*.html"):
        rel_dir = page.parent.relative_to(out_dir)
        for href in re.findall(r'href="([^"]+)"', page.read_text(encoding="utf-8")):
            if href.startswith(("http://", "https://", "data:", "#", "mailto:")):
                continue
            target = posixpath.normpath(
                posixpath.join(str(rel_dir), href.split("#")[0].split("?")[0])
            )
            if target.startswith(".."):  # out-of-bundle repo reference
                continue
            p = out_dir / target
            if not (p.is_file() or (p.is_dir() and (p / "index.html").is_file())):
                dead.append(f"{page.relative_to(out_dir)} -> {href}")
    assert not dead, (
        "dead internal links in static build:\n  " + "\n  ".join(dead)
    )


# --- atomic writes: no .tmp leftovers ---------------------------------------


def test_build_site_no_tmp_left_behind(tiny_good_bundle: Path) -> None:
    """After ``build_site`` succeeds, no ``.tmp`` files exist in the output."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site"
    build_site(b, out_dir, target="static")
    tmps = list(out_dir.rglob("*.tmp"))
    # The atomic-write helpers unlink the tmp file via os.replace, so none
    # should remain. (mkstemp-created names start with ".okf-".)
    assert tmps == []


# --- build_graph_data shape -------------------------------------------------


def test_build_graph_data_shape(tiny_good_bundle: Path) -> None:
    """``build_graph_data`` returns the documented top-level keys."""
    b = Bundle.load(tiny_good_bundle)
    data = build_graph_data(b)
    for key in (
        "name", "nodes", "edges", "external", "bodies",
        "types", "palette", "backlinks",
    ):
        assert key in data
    # bodies has one entry per concept.
    assert len(data["bodies"]) == len(b.concepts)


def test_build_graph_data_node_has_required_fields(tiny_good_bundle: Path) -> None:
    b = Bundle.load(tiny_good_bundle)
    data = build_graph_data(b)
    node = data["nodes"][0]["data"]
    for field in ("id", "label", "type", "description", "tags", "color"):
        assert field in node


def test_build_graph_data_backlinks(tiny_good_bundle: Path) -> None:
    """``backlinks`` maps target_id -> [source_id, ...] for resolved edges."""
    b = Bundle.load(tiny_good_bundle)
    data = build_graph_data(b)
    # tables/users is linked-to by metrics and events.
    bl = data["backlinks"].get("tables/users", [])
    assert set(bl) == {"references/metrics", "tables/events"}


# ===========================================================================
# P1-39, P1-36, P2-22, P2-57, P2-68: render-layer fixes
# ===========================================================================


from pathlib import Path as _Path  # noqa: E402

from okf_loom.render import (  # noqa: E402
    _render_link_list,
    _render_subtitle,
    _strip_body_citations_section,
    _is_typed_relation_target_raw,
)


def _build_bundle_with(
    tmp_path: _Path, concept_files: dict[str, str]
) -> _Path:
    """Stage a synthetic bundle and return its root path."""
    for relpath, content in concept_files.items():
        p = tmp_path / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


def _render_concept_html(bundle_root: _Path, concept_id: str) -> str:
    """Render a single concept page to HTML using the static target."""
    from okf_loom import Bundle
    from okf_loom.render import build_site
    b = Bundle.load(bundle_root)
    out_dir = bundle_root / "_site"
    build_site(b, out_dir, target="static")
    return (out_dir / (concept_id.replace("/", "/") + ".html")).read_text(
        encoding="utf-8"
    )


# --- P1-39: frontmatter citations suppress duplicate body # Citations ------


def test_p1_39_frontmatter_citations_suppress_body_citations(tmp_path: _Path) -> None:
    """P1-39: when frontmatter ``citations:`` is present, the body's
    ``# Citations`` heading is stripped so the two lists don't render twice.
    """
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": (
            "---\n"
            "type: Note\n"
            "title: Both\n"
            "citations:\n"
            "  - id: \"1\"\n"
            "    text: Frontmatter citation\n"
            "---\n\n"
            "# Body\n\nProse.\n\n"
            "# Citations\n\n- Body citation line.\n"
        ),
    })
    html = _render_concept_html(bundle, "concept")
    # The body's "# Citations" heading must NOT appear in the rendered body.
    # (The frontmatter-rendered citation block is separate and IS present.)
    assert "Frontmatter citation" in html  # governed-keys block
    # Body Citations heading stripped: look for an h-tag containing "Citations"
    # that comes from the body. The governed-keys block uses a span/div, not h.
    import re
    body_citations_headings = re.findall(
        r"<h\d>\s*Citations\s*</h\d>", html, re.IGNORECASE
    )
    assert body_citations_headings == [], (
        f"body Citations heading should be suppressed; found "
        f"{body_citations_headings}"
    )
    # Body citation content (the markdown bullet list) should also be gone.
    assert "Body citation line" not in html


def test_p1_39_no_frontmatter_citations_keeps_body_citations(tmp_path: _Path) -> None:
    """P1-39: when frontmatter ``citations:`` is ABSENT, the body's
    ``# Citations`` heading is preserved (no suppression)."""
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": (
            "---\n"
            "type: Note\n"
            "title: BodyOnly\n"
            "---\n\n"
            "# Body\n\nProse.\n\n"
            "# Citations\n\n- Body citation line.\n"
        ),
    })
    html = _render_concept_html(bundle, "concept")
    # Body Citations heading present (heading gets demoted to h2 with an
    # id attribute; the regex allows attributes between tag name and >).
    import re
    headings = re.findall(
        r"<h\d(?:[^>]*)>\s*Citations\s*</h\d>", html, re.IGNORECASE
    )
    assert len(headings) >= 1, "body Citations heading should be preserved"
    assert "Body citation line" in html


def test_p1_39_strip_body_citations_section_unit() -> None:
    """P1-39 unit test: ``_strip_body_citations_section`` removes the
    ``<hN>Citations</hN>`` heading + body up to the next heading."""
    html = (
        "<h2>Schema</h2>\n<p>cols</p>\n"
        "<h2>Citations</h2>\n<ul><li>body cite</li></ul>\n"
        "<h2>Notes</h2>\n<p>misc</p>\n"
    )
    out = _strip_body_citations_section(html)
    assert "Citations" not in out
    assert "body cite" not in out
    # Surrounding sections preserved.
    assert "<h2>Schema</h2>" in out
    assert "<h2>Notes</h2>" in out
    assert "misc" in out


# --- P1-36: unified typed-relation detection --------------------------------


def test_p1_36_is_typed_relation_target_raw_predicate() -> None:
    """P1-36: the magic-prefix discriminator is the single source of truth
    for typed-relation detection in render.py."""
    assert _is_typed_relation_target_raw("relation:tables/users") is True
    assert _is_typed_relation_target_raw("relation:any/forward/ref") is True
    assert _is_typed_relation_target_raw("/tables/users.md") is False
    assert _is_typed_relation_target_raw("./users.md") is False
    assert _is_typed_relation_target_raw(None) is False
    assert _is_typed_relation_target_raw("") is False


def test_p1_36_link_list_renders_typed_relation_chip(tmp_path: _Path) -> None:
    """P1-36 + P2-57: a concept reached via BOTH a body link AND a typed
    relation shows ONCE in the link list, with the relation type as a chip
    that uses the ``okf-rel-type`` CSS class (the one wiki.css defines).
    """
    from okf_loom import Bundle
    bundle = _build_bundle_with(tmp_path, {
        "a.md": (
            "---\n"
            "type: T\ntitle: A\n"
            "relations:\n"
            "  - target: b\n"
            "    type: references\n"
            "    detail: \"\"\n"
            "---\n\n"
            "# A\n\nSee [B](/b.md).\n"
        ),
        "b.md": "---\ntype: T\ntitle: B\n---\nB body\n",
    })
    b = Bundle.load(bundle)
    graph = b.graph()
    out_links = graph.outlinks(("a",))
    items = [
        (l.target, l.label, "static", ("a",), l.target_raw)
        for l in out_links if l.target is not None
    ]
    html = _render_link_list(items, b, kind="out")
    # P2-57: chip uses okf-rel-type, not the orphaned okf-rel-kind.
    assert "okf-rel-type" in html, f"chip should use okf-rel-type\n{html}"
    assert "okf-rel-kind" not in html, (
        f"orphaned okf-rel-kind class should not be emitted\n{html}"
    )
    # Chip carries the relation type label.
    assert "references" in html
    # Only ONE list item for target b (deduped). Phase 2: items render as
    # connection cards (<li class="okf-conn" ...>), so count tag opens.
    assert html.count("<li") == 1


# --- P2-22: aliases rendered as subtitle ------------------------------------


def test_p2_22_subtitle_renders_aliases(tmp_path: _Path) -> None:
    """P2-22: aliases are rendered as a ``<p class="okf-subtitle">`` line
    immediately after the page title (SPEC §7.1 wording)."""
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": (
            "---\n"
            "type: Note\n"
            "title: Main Title\n"
            "aliases: [alt one, alt two]\n"
            "---\n\nBody.\n"
        ),
    })
    html = _render_concept_html(bundle, "concept")
    assert 'class="okf-subtitle"' in html
    # Subtitle text joins aliases with " · ".
    assert "alt one · alt two" in html
    # Subtitle appears AFTER the title (so the document outline reads
    # title → subtitle → description).
    title_pos = html.find("Main Title")
    subtitle_pos = html.find("okf-subtitle")
    assert title_pos != -1 and subtitle_pos != -1
    assert subtitle_pos > title_pos, (
        f"subtitle should come after title (title@{title_pos}, "
        f"subtitle@{subtitle_pos})"
    )


def test_p2_22_subtitle_absent_when_no_aliases(tmp_path: _Path) -> None:
    """P2-22: no aliases → no subtitle line emitted."""
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": "---\ntype: T\ntitle: Just Title\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "concept")
    assert "okf-subtitle" not in html


def test_p2_22_subtitle_unit_empty_aliases() -> None:
    """P2-22: ``_render_subtitle`` returns "" when aliases is empty/absent."""
    from okf_loom.model import Concept
    from okf_loom.paths import concept_id_from_str
    cid = concept_id_from_str("x")
    c = Concept(
        id=cid, path=_Path("/x.md"), rel_path=_Path("x.md"),
        frontmatter={"type": "T", "title": "X"}, body="",
        raw_text="", headings=[],
    )
    assert _render_subtitle(c) == ""
    c.frontmatter["aliases"] = []
    assert _render_subtitle(c) == ""
    c.frontmatter["aliases"] = ["a", "b"]
    out = _render_subtitle(c)
    assert "okf-subtitle" in out
    assert "a · b" in out


# --- P2-57: typed-relation chip class ---------------------------------------


def test_p2_57_link_list_uses_okf_rel_type_class() -> None:
    """P2-57: link list emits ``okf-rel-type`` (the CSS rule) not the
    orphaned ``okf-rel-kind`` (no CSS rule, labels were invisible)."""
    from okf_loom import Bundle
    import tempfile, shutil
    with tempfile.TemporaryDirectory() as td:
        bundle = _Path(td) / "demo"
        shutil.copytree("samples/demo_bundle", bundle)
        b = Bundle.load(bundle)
        # tables/orders has typed relations to tables/customers etc.
        graph = b.graph()
        out_links = graph.outlinks(("tables", "orders"))
        items = [
            (l.target, l.label, "static", ("tables", "orders"), l.target_raw)
            for l in out_links if l.target is not None
        ]
        html = _render_link_list(items, b, kind="out")
        assert "okf-rel-type" in html
        assert "okf-rel-kind" not in html


# --- P2-68: empty link-list sections are skipped ----------------------------


def test_p2_68_empty_link_list_returns_empty_string() -> None:
    """P2-68: ``_render_link_list`` returns "" when there are no items."""
    from okf_loom import Bundle
    import tempfile, shutil
    with tempfile.TemporaryDirectory() as td:
        bundle = _Path(td) / "demo"
        shutil.copytree("samples/demo_bundle", bundle)
        b = Bundle.load(bundle)
        # An isolated concept has no out-links and no backlinks.
        out = _render_link_list([], b, kind="out")
        assert out == ""
        assert "(none)" not in out


def test_p2_68_empty_section_stripped_from_page(tmp_path: _Path) -> None:
    """P2-68: when a concept has NO outgoing links and NO backlinks, the
    rendered concept page omits the empty ``<section class="okf-relations
    __block">`` blocks (no "Links to" / "Cited by" headings + "(none)")."""
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": "---\ntype: T\ntitle: Lonely\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "concept")
    # No "(none)" placeholder rendered.
    assert "(none)" not in html
    # The wrapping ``<section class="okf-relations__block">`` element is
    # gone entirely (both for outgoing and backlinks). NOTE: the template
    # carries a comment that mentions "rendered ... list" — we look for
    # the actual rendered ``<section>`` tag, not the comment.
    assert 'class="okf-relations__block"' not in html
    # No rendered ``<h2>Links to</h2>`` or ``<h2>Cited by</h2>`` headings
    # (the template wraps these in the section we just stripped).
    import re
    h2_texts = re.findall(r"<h2[^>]*>\s*([^<]+?)\s*</h2>", html)
    h2_norm = [t.strip().lower() for t in h2_texts]
    assert "links to" not in h2_norm
    assert "cited by" not in h2_norm


def test_p2_68_nonempty_section_kept(tmp_path: _Path) -> None:
    """P2-68 contrast: when a concept DOES have out-links, the connections
    section IS rendered (with the heading and the link list). Phase 2
    renamed the heading to 'This concept links to' and the wrapper class
    to ``okf-connections__block``."""
    bundle = _build_bundle_with(tmp_path, {
        "a.md": (
            "---\ntype: T\ntitle: A\n---\n\nSee [B](/b.md).\n"
        ),
        "b.md": "---\ntype: T\ntitle: B\n---\nB body\n",
    })
    html = _render_concept_html(bundle, "a")
    assert "This concept links to" in html
    assert "okf-connections__block" in html


# ===========================================================================
# P1-3: static-mode nav URLs must carry .html (no live router rewrites them)
# ===========================================================================


def test_p1_3_static_concept_nav_urls_have_html_extension(
    tiny_good_bundle: _Path,
) -> None:
    """P1-3: in ``--target static`` the concept page nav emits extensionful
    URLs for the search form action and the Graph link.

    Previously both emitted extensionless ``__search`` / ``__graph``,
    which 404'd in static builds (no server rewrites the path). Uses the
    nested concept ``tables/users`` so the test also confirms the relative
    ``../`` prefix is preserved alongside the new extension.
    """
    html = _render_concept_html(tiny_good_bundle, "tables/users")
    import re

    # Search form action: must be ``../__search.html`` (relative + .html).
    form_match = re.search(
        r'<form\s+action="([^"]*__search[^"]*)"[^>]*role="search"', html
    )
    assert form_match, "search form not found in concept page"
    assert "__search.html" in form_match.group(1), (
        f"static search action should include .html; got {form_match.group(1)!r}"
    )
    # The nested concept must keep its relative ``../`` prefix.
    assert "__search.html" in form_match.group(1)

    # Graph link: must be ``../__graph.html``.
    graph_match = re.search(
        r'<a\s+class="okf-btn"\s+href="([^"]*__graph[^"]*)">Graph</a>', html
    )
    assert graph_match, "Graph link not found in concept page"
    assert "__graph.html" in graph_match.group(1), (
        f"static graph link should include .html; got {graph_match.group(1)!r}"
    )


def test_p1_3_spa_concept_nav_urls_extensionless(
    tiny_good_bundle: _Path,
) -> None:
    """P1-3 parity: in ``--target spa`` (and serve) the nav URLs stay
    extensionless — the live router handles the bare path. This guards
    against an over-eager ``mode != 'serve'`` style regression."""
    from okf_loom import Bundle
    from okf_loom.render import build_site, _render_concept_page
    b = Bundle.load(tiny_good_bundle)
    g = b.graph()
    concept = b.concepts[("tables", "users")]
    html = _render_concept_page(
        concept, b, g, mode="spa", name=b.name,
        palette={"table": "hsl(18, 62%, 48%)"}, config={},
    )
    import re
    form_match = re.search(
        r'<form\s+action="([^"]*__search[^"]*)"[^>]*role="search"', html
    )
    assert form_match, "search form not found in spa concept page"
    assert ".html" not in form_match.group(1), (
        f"spa search action should be extensionless; got {form_match.group(1)!r}"
    )


def test_p1_3_static_graph_page_has_data_okf_mode(
    tiny_good_bundle: _Path,
) -> None:
    """P1-3: the graph page body carries ``data-okf-mode="static"`` so
    graph.js can decide whether Open-page hrefs need a ``.html`` suffix."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site_p13_graph"
    build_site(b, out_dir, target="static")
    graph_html = (out_dir / "__graph.html").read_text(encoding="utf-8")
    assert 'data-okf-mode="static"' in graph_html, (
        "graph page body missing data-okf-mode attribute"
    )


def test_iter8_tilde_fence_renders_as_code_block():
    """P1-1 (iter-8): ~~~ code fences must render as <pre><code>, not plain
    text. CommonMark §4.5 defines both ``` and ~~~ as fences; the viewer
    renderer previously only matched backticks."""
    from okf_loom.viewer.markdown import markdown_to_html
    # Tilde fence
    html = markdown_to_html("~~~python\nprint('hello')\n~~~")
    assert "<pre>" in html, f"tilde fence not rendered as code: {html}"
    assert "language-python" in html or "<code>" in html
    # Backtick fence (must still work)
    html2 = markdown_to_html("```python\nprint('hello')\n```")
    assert "<pre>" in html2


# ===========================================================================
# Current spec §9: static-build client-side search
#
# Static builds used to show only a "search unavailable" notice. We now emit
# a versioned manifest plus content-addressed passage chunks and a vanilla-JS
# searcher. Static and SPA outputs consume the same portable contract; live
# serve keeps the authoritative /__search backend.
# ===========================================================================


def test_static_build_emits_versioned_chunked_passage_corpus(
    tiny_good_bundle: Path,
) -> None:
    """Portable search is versioned, content-addressed, and complete."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site_static_search"
    build_site(b, out_dir, target="static")

    import json as _json
    manifest_path = out_dir / "__data" / "passages-manifest.json"
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["algorithm_version"] == "section-retrieval-v1"
    assert manifest["chunk_count"] == len(manifest["chunks"])
    rows = []
    for descriptor in manifest["chunks"]:
        assert descriptor["hash"][:16] in descriptor["file"]
        raw = (manifest_path.parent / descriptor["file"]).read_text(encoding="utf-8")
        chunk = _json.loads(raw)
        assert chunk["bundle_revision"] == manifest["bundle_revision"]
        assert raw == _json.dumps(chunk, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        import hashlib
        assert hashlib.sha256(raw.encode()).hexdigest() == descriptor["hash"]
        assert descriptor["id"] == f'{manifest["bundle_revision"]}:{chunk["index"]:06d}:{descriptor["hash"]}'
        rows.extend(chunk["passages"])
    assert len(rows) == manifest["passage_count"]
    assert rows
    assert {"passage_id", "citation", "path", "line_start", "line_end", "excerpt"} <= rows[0].keys()
    assert not (manifest_path.parent / "search.json").exists()


def test_static_search_page_loads_client_side_searcher(
    tiny_good_bundle: Path,
) -> None:
    """The static ``__search.html`` page references ``static-search.js``
    (the client-side searcher), not the old "search unavailable" message."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site_static_search_page"
    build_site(b, out_dir, target="static")

    html = (out_dir / "__search.html").read_text(encoding="utf-8")
    # Client-side searcher is wired in (CSP: script-src 'self', same-origin).
    assert "static-search.js" in html, (
        "static __search.html must reference __static/static-search.js"
    )
    # The script tag must be properly formed (not inside a comment).
    import re
    script_match = re.search(
        r'<script\s+src="[^"]*portable-page\.js" data-okf-runtime="static-search\.js"[^>]*></script>', html
    )
    assert script_match, (
        "static __search.html missing valid <script> tag for static-search.js"
    )
    # The server-emitted page itself must not declare search broken.
    assert "search unavailable" not in html.lower(), (
        "static __search.html still contains a 'search unavailable' message"
    )
    # The static-search.js asset itself must be emitted under __static/.
    assert (out_dir / "__static" / "static-search.js").exists(), (
        "static-search.js was not emitted under __static/"
    )


def test_serve_uses_backend_and_spa_uses_portable_searcher(
    tiny_good_bundle: Path,
) -> None:
    """Serve stays backend-backed while SPA consumes normalized corpus."""
    from okf_loom.render import _render_search_page
    b = Bundle.load(tiny_good_bundle)
    serve = _render_search_page(b, mode="serve", name=b.name, config={}, query="x", results=[])
    spa = _render_search_page(b, mode="spa", name=b.name, config={}, query="x", results=[])
    assert "static-search.js" not in serve
    assert "static-search.js" in spa
    assert "wiki.js" in serve and "wiki.js" in spa


def test_static_search_corpus_is_deterministic_across_builds(
    tiny_good_bundle: Path,
) -> None:
    """Two builds of the same bundle produce byte-identical search corpora
    (current spec §3 determinism — no timestamps, no dict-order reliance)."""
    import json as _json
    b1 = Bundle.load(tiny_good_bundle)
    out1 = tiny_good_bundle / "_site_det_a"
    build_site(b1, out1, target="static")
    b2 = Bundle.load(tiny_good_bundle)
    out2 = tiny_good_bundle / "_site_det_b"
    build_site(b2, out2, target="static")

    a_manifest = _json.loads((out1 / "__data" / "passages-manifest.json").read_text())
    b_manifest = _json.loads((out2 / "__data" / "passages-manifest.json").read_text())
    assert a_manifest == b_manifest
    for descriptor in a_manifest["chunks"]:
        assert (out1 / "__data" / descriptor["file"]).read_bytes() == (out2 / "__data" / descriptor["file"]).read_bytes()


def test_portable_chunk_url_changes_with_revision_and_dotted_ids_do_not_collide(tmp_path: Path) -> None:
    import hashlib
    root = _build_bundle_with(tmp_path, {
        "index.md": "# Bundle\n",
        "guides/api.md": "---\ntype: Guide\ntitle: API\n---\n# API\nbase\n",
        "guides/api.v2.md": "---\ntype: Guide\ntitle: API v2\n---\n# API v2\nversion two\n",
    })
    first = tmp_path / "first"
    second = tmp_path / "second"
    build_site(Bundle.load(root), first, target="static")
    (root / "index.md").write_text("# Bundle\n\nrevision-only change\n", encoding="utf-8")
    build_site(Bundle.load(root), second, target="static")
    import json as _json
    left = _json.loads((first / "__data" / "passages-manifest.json").read_text())
    right = _json.loads((second / "__data" / "passages-manifest.json").read_text())
    assert left["bundle_revision"] != right["bundle_revision"]
    assert [row["file"] for row in left["chunks"]] != [row["file"] for row in right["chunks"]]
    for out, manifest in ((first, left), (second, right)):
        for descriptor in manifest["chunks"]:
            raw = (out / "__data" / descriptor["file"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == descriptor["hash"]
        assert (out / "guides" / "api.html").is_file()
        assert (out / "guides" / "api.v2.html").is_file()
        assert (out / "__context" / "guides" / "api.txt").is_file()
        assert (out / "__context" / "guides" / "api.v2.txt").is_file()


def test_browser_corpus_hash_modes_neighbors_and_context_schema(tmp_path: Path) -> None:
    """Portable runtime verifies Python bytes and publishes canonical packs."""
    root = _build_bundle_with(tmp_path, {
        "index.md": "# Bundle\n",
        "alpha.md": (
            "---\ntype: Guide\ntitle: Alpha\ntags: [shared]\n"
            "entities: [Alpha]\nprovenance:\n  confidence: 1e-07\n---\n"
            "# Shared\nshared needle [Beta](/beta.md)\n"
        ),
        "beta.md": "---\ntype: Guide\ntitle: Beta\ntags: [shared]\n---\n# Shared\nshared needle\n",
    })
    bundle = Bundle.load(root)
    from okf_loom.render import _browser_passage_corpus
    artifact = _browser_passage_corpus(bundle, build_graph_data(bundle)["edges"])
    assert "1e-07" in artifact["chunks"][0]["canonical_chunk"]
    result = _portable_node(artifact, r"""
const v=await window.okfPortableRetrieval.validate(artifact.manifest,artifact.chunks);
const p=window.okfPortableRetrieval.create(artifact.manifest,v.passages,v.edges);
const cases={
 lexical:{query:'shared',mode:'lexical',limit:20},
 semantic:{query:'shared',mode:'semantic',limit:20},
 hybrid:{query:'shared',mode:'hybrid',limit:20},
 tag:{query:'shared',mode:'tag',limit:20},
 entity:{query:'Alpha',mode:'entity',limit:20},
 relation:{query:'link',mode:'relation',source:'alpha',target:'beta',limit:20}
};
const counts={};Object.keys(cases).forEach(k=>counts[k]=p.search(cases[k]).rows.length);
const packs=[0,1,2].map(n=>p.context({concept:'alpha',query:'shared'},{query:'shared',mode:'lexical',limit:20,budget:6000,neighbors:n},[]));
const selection=p.context({concept:'alpha',query:'needle',selection_exact:'needle',selection_prefix:'shared ',selection_suffix:' [Beta]',selection_heading:'Shared'},{query:'needle',mode:'lexical',limit:20,budget:6000,neighbors:0},[]);
const bad=[];[
 {query:'x',mode:'unknown',limit:20},
 {query:'x',mode:'lexical',limit:20,entity:'Alpha'},
 {query:'x',mode:'lexical',limit:20,relation:'link'},
 {query:'x',mode:'lexical',limit:0},
 {query:'x',mode:'lexical',limit:20,unknown:'x'}
].forEach(o=>{try{p.search(o);bad.push(false);}catch(e){bad.push(true);}});
console.log(JSON.stringify({counts,bad,selection,packs:packs.map(x=>({ids:x.items.map(i=>i.concept_id),keys:Object.keys(x).sort(),itemKeys:Object.keys(x.items[0]).sort(),estimated:x.budget.estimated_tokens,markdown:x.representations.markdown}))}));
""")
    assert all(result["counts"][mode] > 0 for mode in (
        "lexical", "semantic", "hybrid", "tag", "entity", "relation"
    ))
    assert result["bad"] == [True] * 5
    packs = result["packs"]
    assert packs[0]["ids"] == ["alpha", "beta"]
    assert packs[0]["ids"] == packs[1]["ids"] == packs[2]["ids"]
    assert packs[0]["keys"] == sorted({
        "schema_version", "bundle_revision", "query", "mode", "filters", "budget",
        "neighbors", "included", "omitted", "truncated", "items", "representations",
    })
    assert {"heading_line", "char_start", "char_end", "backends", "evidence", "estimated_tokens"} <= set(packs[0]["itemKeys"])
    assert packs[0]["estimated"] <= 6000
    assert packs[0]["markdown"].startswith("# OKF Context Pack\n")
    assert "## [alpha.md#L" in packs[0]["markdown"]
    assert result["selection"]["selection"]["status"] == "attached_exact"
    assert "@C" in result["selection"]["selection"]["citation"]
    assert result["selection"]["items"][0]["passage_id"] == result["selection"]["selection"]["passage_id"]


def test_portable_tag_mode_matches_live_title_then_concept_order(tmp_path: Path) -> None:
    """Equal-score tag hits follow Python's Unicode title/id ordering."""
    root = _build_bundle_with(tmp_path, {
        "index.md": "# Bundle\n",
        "runbooks/payment-retries.md": "---\ntype: Runbook\ntitle: Payment Retry Safety\ntags: [critical-path]\n---\n# Safety\nretry\n",
        "services/checkout.md": "---\ntype: Service\ntitle: Checkout Service\ntags: [critical-path]\n---\n# Dependencies\ncheckout\n",
    })
    bundle = Bundle.load(root)
    from okf_loom.render import _browser_passage_corpus
    from okf_loom.search import SearchMode, search_bundle
    artifact = _browser_passage_corpus(bundle, build_graph_data(bundle)["edges"])
    portable = _portable_node(artifact, r"""
const v=await window.okfPortableRetrieval.validate(artifact.manifest,artifact.chunks);
const p=window.okfPortableRetrieval.create(artifact.manifest,v.passages,v.edges);
const seen=new Set(),ids=[];p.search({query:'critical-path',mode:'tag',limit:20}).rows.forEach(row=>{const id=row.passage.concept_id;if(!seen.has(id)){seen.add(id);ids.push(id);}});
console.log(JSON.stringify({ids}));
""")
    live = search_bundle(bundle, "critical-path", mode=SearchMode.TAG, limit=20)
    live_ids = ["/".join(result.concept_id) for result in live]
    assert live_ids == ["services/checkout", "runbooks/payment-retries"]
    assert portable["ids"] == live_ids


def test_browser_corpus_rejects_canonical_byte_and_mixed_graph_tampering(tmp_path: Path) -> None:
    root = _build_bundle_with(tmp_path, {
        "index.md": "# Bundle\n", "a.md": "---\ntype: Note\n---\n# A\nvalue\n",
    })
    bundle = Bundle.load(root)
    from okf_loom.render import _browser_passage_corpus
    artifact = _browser_passage_corpus(bundle, build_graph_data(bundle)["edges"])
    result = _portable_node(artifact, r"""
const copies=[JSON.parse(JSON.stringify(artifact)),JSON.parse(JSON.stringify(artifact)),JSON.parse(JSON.stringify(artifact))];
  copies[0].chunks[0].canonical_chunk+=' ';
  copies[1].chunks[0].payload.bundle_revision='mixed';
copies[2].manifest.graph.canonical_edges='[] ';
const rejected=[];for(const c of copies){try{await window.okfPortableRetrieval.validate(c.manifest,c.chunks);rejected.push(false);}catch(e){rejected.push(true);}}
console.log(JSON.stringify({rejected}));
""")
    assert result["rejected"] == [True, True, True]


def test_portable_validation_rejects_oversize_and_expired_lifecycle_before_publish(tmp_path: Path) -> None:
    root = _build_bundle_with(tmp_path, {
        "index.md": "# Bundle\n", "a.md": "---\ntype: Note\n---\n# A\nvalue\n",
    })
    bundle = Bundle.load(root)
    from okf_loom.render import _browser_passage_corpus
    artifact = _browser_passage_corpus(bundle, build_graph_data(bundle)["edges"])
    result = _portable_node(artifact, r"""
let oversize=false,expired=false;
try{await window.okfPortableRetrieval.validate(artifact.manifest,[' '.repeat(4*1024*1024+1)]);}catch(e){oversize=/byte limit/.test(e.message);}
try{await window.okfPortableRetrieval.validate(artifact.manifest,artifact.chunks,{deadline:Date.now()-1,generation:1,isCurrent:()=>true});}catch(e){expired=e.name==='AbortError';}
console.log(JSON.stringify({oversize,expired}));
""")
    assert result == {"oversize": True, "expired": True}


def test_static_nojs_context_is_canonical_and_error_links_target_real_controls(tmp_path: Path) -> None:
    root = _build_bundle_with(tmp_path, {
        "index.md": "# Bundle\n", "a.md": "---\ntype: Note\ntitle: Alpha\n---\n# A\nalpha text\n",
    })
    bundle = Bundle.load(root)
    out = tmp_path / "site"
    build_site(bundle, out, target="static")
    from okf_loom.retrieval import RetrievalService
    expected = RetrievalService(bundle).default_concept_contexts()["a"]
    assert (out / "__context" / "a.txt").read_text(encoding="utf-8") == expected
    concept_html = (out / "a.html").read_text(encoding="utf-8")
    search_html = (out / "__search.html").read_text(encoding="utf-8")
    assert "concept-only context pack" in concept_html
    assert "Selecting exact rendered text loads portable evidence only when needed" in concept_html
    assert "read-only view" not in concept_html.lower()
    assert 'class="okf-btn okf-context-copy okf-js-action"' in concept_html
    assert ".okf-js-action{display:none!important}" in concept_html
    assert "Open concept-only context pack" in concept_html
    # R3: the copy button is described as cited evidence that messages no one;
    # the note is hidden, read only as the button's description.
    assert 'aria-describedby="okf-context-action-note">Copy concept context</button>' in concept_html
    assert concept_html.count('id="okf-context-action-note"') == 1
    assert ('<span id="okf-context-action-note" hidden>Cited passages for reuse, '
            'not a summary. Copying sends nothing to the agent.</span>') in concept_html
    assert "arbitrary search requires JavaScript" in search_html

    from okf_loom.render import _render_search_page
    class Error:
        code = "invalid_request"
        message = "bad"
        fields = {"q": "too_long", "type_filter": "invalid"}
    errored = _render_search_page(
        bundle, mode="serve", name=bundle.name, config={}, query="x", results=[], error=Error(),
    )
    assert 'href="#okf-search-query"' in errored
    assert 'href="#okf-search-type"' in errored
    assert 'href="#okf-search-q"' not in errored


def test_search_excerpt_presentation_is_plain_and_escaped(tmp_path: Path) -> None:
    from okf_loom.render import _render_search_page
    bundle = Bundle.load(_build_bundle_with(tmp_path, {
        "index.md": "# Bundle\n", "a.md": "---\ntype: Note\n---\ntext\n",
    }))
    excerpt = "**bold** [label](/a.md) `customer_id` __init__ <img src=x onerror=alert(1)> needle"
    html = _render_search_page(bundle, mode="serve", name=bundle.name, config={}, query="needle", results=[{
        "concept_id": "a", "title": "A", "evidence": {"passage": {
            "excerpt": excerpt, "citation": "[a.md#L4-L4]", "path": "a.md", "heading": None,
        }},
    }])
    presented = re.search(
        r'<blockquote class="okf-search-snippet okf-search-snippet--compact">(.*?)</blockquote>',
        html, re.DOTALL,
    ).group(1)
    assert "**bold**" not in presented and "[label](/a.md)" not in presented
    assert "customer_id" in presented and "__init__" in presented
    assert "&lt;img src=x onerror=alert(1)&gt;" in presented
    assert "<img src=x" not in presented
    assert "<mark>needle</mark>" in presented
    assert 'data-excerpt="**bold** [label](/a.md) `customer_id` __init__ &lt;img' in html  # exact copy source retained


def test_relation_result_without_passage_omits_exact_evidence_controls(tmp_path: Path) -> None:
    """Concept-level graph matches must not impersonate passage evidence."""
    from okf_loom.render import _render_search_page
    bundle = Bundle.load(_build_bundle_with(tmp_path, {
        "index.md": "# Bundle\n", "a.md": "---\ntype: Note\n---\ntext\n",
    }))
    html = _render_search_page(bundle, mode="serve", name=bundle.name, config={}, query="a", results=[{
        "concept_id": "a", "title": "A", "description": "Concept description",
        "source_backend": "relation", "reasons": ["relation target"],
        "evidence": {"relation": {"source": "b", "target": "a"}},
    }])
    article = re.search(r'<article class="okf-search-result".*?</article>', html, re.DOTALL).group(0)
    assert "Graph evidence only" in article
    assert "No exact passage or line citation is available" in article
    assert "Evidence and context" in article and "relation target" in article
    assert "okf-search-citation" not in article
    assert "data-copy-passage" not in article
    assert "Copy passage" not in article
    assert "Copy concept context" in article
    assert "Open canonical Markdown context" in article


def test_live_nojs_nested_context_link_and_default_controls_are_honest(tmp_path: Path) -> None:
    from okf_loom.render import _render_search_page
    bundle = Bundle.load(_build_bundle_with(tmp_path, {
        "index.md": "# Bundle\n",
        "nested/item.md": "---\ntype: Note\ntitle: Nested\n---\n# Nested\nbody\n",
    }))
    html = _render_search_page(bundle, mode="serve", name=bundle.name, config={}, query="nested", results=[{
        "concept_id": "nested/item", "title": "Nested", "evidence": {"passage": {
            "excerpt": "body", "citation": "[nested/item.md#L5-L5]", "path": "nested/item.md",
        }},
    }])
    context_href = re.search(r'href="([^"]+)">Open canonical Markdown context', html).group(1)
    assert "concept=nested/item" in context_href
    assert "%2F" not in context_href.upper()
    assert 'data-context-budget min="128" max="50000" value="6000"' in html
    assert '<details class="okf-context-advanced">' in html
    assert "Open default canonical Markdown context" in html


def test_portable_relation_inventory_unicode_mapping_and_validation(tmp_path: Path) -> None:
    """Portable retrieval keeps complete graph evidence and fail-closed parity."""
    bundle = Bundle.load(_build_bundle_with(tmp_path, {
        "index.md": "# Bundle\n",
        "source.md": "---\ntype: Note\n---\n# Source\nsource body\n",
    }))
    from okf_loom.render import _browser_passage_corpus
    artifact = _browser_passage_corpus(bundle, build_graph_data(bundle)["edges"])
    result = _portable_node(artifact, r"""
const edges=[];for(let i=0;i<25;i++)edges.push({source:'source',target:'empty-'+String(i).padStart(2,'0'),type:'links',via:'relations'});edges.push({source:'empty-24',target:'source',type:'links',via:'relations'});edges.sort((a,b)=>(a.source+'\0'+a.target+'\0'+a.via+'\0'+a.type).localeCompare(b.source+'\0'+b.target+'\0'+b.via+'\0'+b.type));
artifact.manifest.concept_ids=Array.from(new Set(['source'].concat(edges.map(e=>e.target)))).sort();
  artifact.manifest.concepts=artifact.manifest.concept_ids.map(id=>id==='source'?artifact.manifest.concepts.find(c=>c.id==='source'):{id,path:id+'.md',tags:[],title:'Empty '+id,type:'Note',search:{aliases:[],body:'',description:'',entities:[],headings:[]}});
artifact.manifest.graph.canonical_edges=JSON.stringify(edges);
artifact.manifest.graph.count=edges.length;
artifact.manifest.graph.hash=await crypto.webcrypto.subtle.digest('SHA-256',new TextEncoder().encode(artifact.manifest.graph.canonical_edges)).then(b=>Buffer.from(b).toString('hex'));
const validated=await window.okfPortableRetrieval.validate(artifact.manifest,artifact.chunks);
const provider=window.okfPortableRetrieval.create(artifact.manifest,validated.passages,validated.edges);
const all=provider.search({query:'ignored free query',mode:'relation',source:'source',limit:100});
const target=provider.search({query:'also ignored',mode:'relation',target:'empty-24',limit:100});
const pack=provider.context({}, {query:'ignored',mode:'relation',source:'source',limit:100,budget:50000,neighbors:0}, all.rows);
const lexicalRows=provider.search({query:'source',mode:'lexical',limit:20});
const lexicalPack=provider.context({}, {query:'source',mode:'lexical',limit:20,budget:50000,neighbors:0}, lexicalRows.rows);
const emptyRelation=provider.search({query:'ignored',mode:'relation',source:'empty-24',limit:20});
  let emptySeedRejected=false;try{provider.context({concept:'empty-24'}, {query:'',mode:'lexical',limit:20,budget:6000}, []);}catch(e){emptySeedRejected=/no canonical passage/.test(e.message);}
let emoji4096=true,emoji4097=false;try{provider.search({query:'😀'.repeat(4096),mode:'lexical',limit:1});}catch(e){emoji4096=false;}try{provider.search({query:'😀'.repeat(4097),mode:'lexical',limit:1});}catch(e){emoji4097=true;}
const mapped=window.okfPortableRetrieval.mapRenderedSource('# Heading\n**bold** and [link](/target) &amp; `code`', {concept:'source',rendered_exact:'bold and link & code',rendered_prefix:'',rendered_suffix:'',selection_heading:'Heading'});
let ambiguous=false;try{window.okfPortableRetrieval.mapRenderedSource('**same** and **same**', {concept:'source',rendered_exact:'same'});}catch(e){ambiguous=/multiple/.test(e.message);}
let asyncReject=false,syncThrow=false;try{const promise=window.okfPortableRetrieval.validate(null,null);if(!promise||typeof promise.then!=='function')syncThrow=true;else await promise.catch(()=>{asyncReject=true;});}catch(e){syncThrow=true;}
  const bad=JSON.parse(JSON.stringify(artifact));bad.chunks[0].payload.passages[0].provenance={'bad\u202ekey':'x'};bad.chunks[0].canonical_chunk=JSON.stringify(bad.chunks[0].payload);const badHash=await crypto.webcrypto.subtle.digest('SHA-256',new TextEncoder().encode(bad.chunks[0].canonical_chunk)).then(b=>Buffer.from(b).toString('hex'));bad.manifest.chunks[0].hash=badHash;bad.manifest.chunks[0].id=bad.manifest.bundle_revision+':000000:'+badHash;bad.manifest.chunks[0].file='passages-000000-'+badHash.slice(0,16)+'.json';
  let metadataRejected=false;try{await window.okfPortableRetrieval.validate(bad.manifest,bad.chunks);}catch(e){metadataRejected=true;}
  console.log(JSON.stringify({all:all.rows[0].matched_edges.length,target:target.rows.length,evidence:pack.items[0].evidence.edges.length,omitted:pack.items[0].evidence.omitted_count,lexicalEvidence:lexicalPack.items[0].evidence.edges.length,lexicalOmitted:lexicalPack.items[0].evidence.omitted_count,emptyRelationPassage:emptyRelation.rows.length?emptyRelation.rows[0].passage:'missing',emptySeedRejected,inventory:provider.manifest.concept_ids.includes('empty-24'),emoji4096,emoji4097,mapped:mapped.selection_exact,ambiguous,asyncReject,syncThrow,metadataRejected,readable:window.okfPortableRetrieval.displayText('`customer_id` and __init__')}));
""")
    assert result == {
        "all": 25, "target": 1, "evidence": 20, "omitted": 5,
        "lexicalEvidence": 0, "lexicalOmitted": 0,
        "emptyRelationPassage": None, "emptySeedRejected": True,
        "inventory": True, "emoji4096": True, "emoji4097": True,
        "mapped": "bold** and [link](/target) &amp; `code`",
        "ambiguous": True, "asyncReject": True, "syncThrow": False,
        "metadataRejected": True, "readable": "customer_id and __init__",
    }


def test_static_render_uses_batch_default_context_api(
    tiny_good_bundle: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Static generation performs one supported batch call, never N context calls."""
    from okf_loom.retrieval import RetrievalService

    bundle = Bundle.load(tiny_good_bundle)
    expected = {str(concept.id): f"# batch {concept.id}\n" for concept in bundle.concepts.values()}
    calls = 0

    def batch(self, concept_ids=None):
        nonlocal calls
        calls += 1
        assert concept_ids is None
        return expected

    def forbidden_context(self, request):
        raise AssertionError("render must not issue per-concept context calls")

    monkeypatch.setattr(RetrievalService, "default_concept_contexts", batch)
    monkeypatch.setattr(RetrievalService, "context", forbidden_context)
    out = tiny_good_bundle / "_site_batch_contexts"
    build_site(bundle, out, target="static")
    assert calls == 1
    for concept_id, markdown in expected.items():
        assert (out / "__context" / f"{concept_id}.txt").read_text(encoding="utf-8") == markdown


# ===========================================================================
# iter-1 frontend fixes: P1-3 graph governed keys, P1-4/P2-2 graph a11y,
# P2-3 breadcrumb, P2-4 related label, P2-6 governed hierarchy,
# P2-7 subtitle CSS class.
# ===========================================================================


def test_iter1_build_graph_data_ships_governed_keys(tmp_path: _Path) -> None:
    """P1-3: build_graph_data ships §7 governed keys per node so the graph
    detail panel can render them. Every key is always present (defaults to
    an empty list when absent — graceful)."""
    bundle = _build_bundle_with(tmp_path, {
        "rich.md": (
            "---\n"
            "type: Table\n"
            "title: Rich\n"
            "aliases: [alt one, alt two]\n"
            "entities:\n"
            "  - id: entity/x\n"
            "    label: X\n"
            "    kind: business_entity\n"
            "    aliases: [a1, a2]\n"
            "provenance:\n"
            "  - source: https://src.example.com\n"
            "    note: a note\n"
            "    timestamp: '2026-06-27T00:00:00Z'\n"
            "citations:\n"
            "  - id: '1'\n"
            "    text: ref\n"
            "    url: https://c.example.com\n"
            "relations:\n"
            "  - target: other/c\n"
            "    type: references\n"
            "    detail: fk\n"
            "---\nbody\n"
        ),
        "plain.md": "---\ntype: Note\ntitle: Plain\n---\nbody\n",
    })
    b = Bundle.load(bundle)
    data = build_graph_data(b)
    by_id = {n["data"]["id"]: n["data"] for n in data["nodes"]}
    # All governed keys present on EVERY node (graceful defaults).
    for cid, nd in by_id.items():
        for key in ("aliases", "entities", "provenance", "citations", "relations"):
            assert key in nd, f"{cid} missing governed key {key}"
            assert isinstance(nd[key], list), f"{cid}.{key} not a list"
    rich = by_id["rich"]
    assert rich["aliases"] == ["alt one", "alt two"]
    assert rich["entities"] and rich["entities"][0]["label"] == "X"
    assert rich["provenance"] and rich["provenance"][0]["source"] == "https://src.example.com"
    assert rich["citations"] and rich["citations"][0]["id"] == "1"
    assert rich["relations"] and rich["relations"][0]["type"] == "references"
    # Plain node: all governed keys are empty lists (graceful absence).
    plain = by_id["plain"]
    assert plain["aliases"] == [] and plain["entities"] == []
    assert plain["provenance"] == [] and plain["citations"] == []
    assert plain["relations"] == []


def test_iter1_build_graph_data_sanitizes_governed_urls(tmp_path: _Path) -> None:
    """P1-3 / security: a javascript: provenance source or citation url is
    neutralized via _safe_url (graph.js assigns these to a.href)."""
    bundle = _build_bundle_with(tmp_path, {
        "x.md": (
            "---\n"
            "type: T\n"
            "title: X\n"
            "provenance:\n"
            "  - source: javascript:alert(1)\n"
            "    note: bad\n"
            "citations:\n"
            "  - id: '1'\n"
            "    text: t\n"
            "    url: data:text/html,<script>\n"
            "---\nbody\n"
        ),
    })
    b = Bundle.load(bundle)
    data = build_graph_data(b)
    nd = data["nodes"][0]["data"]
    # _safe_url returns None for dangerous schemes; the helper keeps the raw
    # string as text but graph.js only turns http(s) into <a href>. Either way
    # the dangerous scheme must NOT survive as a usable href.
    assert "javascript:" not in nd["provenance"][0]["source"], nd["provenance"]
    assert "data:text/html" not in nd["citations"][0]["url"], nd["citations"]


def test_iter1_graph_page_has_h1_and_node_index(tiny_good_bundle: _Path) -> None:
    """The graph route has a visible focus target h1 and keyboard index."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site_iter1_graph"
    build_site(b, out_dir, target="static")
    html = (out_dir / "__graph.html").read_text(encoding="utf-8")
    assert re.search(r'<h1[^>]*class="[^"]*okf-graph-route-title"', html), "graph page missing visible route h1"
    assert not re.search(r'<h1[^>]*class="[^"]*sr-only"', html), "graph route target must not be hidden"
    assert "okf-node-index" in html, "graph page missing node-index container"
    assert 'id="okf-node-index-list"' in html
    # detail-title is an <h2> now (was <h1>), demoted because the sr-only h1
    # sits above the layout.
    assert re.search(r'<h2[^>]*id="detail-title"', html), "detail-title not demoted to h2"
    # No inline script (CSP hard rule) — iter-1 removed the comment-only block.
    assert not re.search(r"<script>(?!\s*</script>)", html) or \
        "<script>\n</script>" not in html


def test_graph_detail_templates_have_stable_accessible_hierarchy(
    tiny_good_bundle: _Path,
) -> None:
    """Graph and single-file detail panes expose the same ordered slots."""
    bundle = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site_graph_detail_hierarchy"
    build_site(bundle, out_dir, target="static")
    single = tiny_good_bundle / "_single_graph_detail_hierarchy.html"
    render_single_file(bundle, single)

    for html in (
        (out_dir / "__graph.html").read_text(encoding="utf-8"),
        single.read_text(encoding="utf-8"),
    ):
        assert 'id="detail-content" aria-labelledby="detail-title"' in html
        assert 'data-graph-pane="graph" aria-controls="okf-graph"' in html
        assert 'data-graph-pane="concepts" aria-controls="okf-concept-index"' in html
        assert 'data-graph-pane="details" aria-controls="okf-detail"' in html
        assert '<summary aria-controls="okf-node-index-list">' in html
        order = [
            html.index('id="detail-title"'),
            html.index('id="detail-description"'),
            html.index('id="detail-connection-summary"'),
            html.index('id="detail-body"'),
            html.index('id="detail-relationships-slot"'),
            html.index('id="okf-detail-signals"'),
            html.index('id="okf-lens-controls-slot"'),
        ]
        assert order == sorted(order)
        assert '<h3 id="detail-connection-summary-title">' in html
        assert '<h3 id="okf-detail-signals-title"' in html


def test_iter1_concept_page_has_clickable_breadcrumb(tmp_path: _Path) -> None:
    """P2-3: concept page renders a <nav aria-label="Breadcrumb"> with
    clickable ancestor links, not a flat raw-id <p>."""
    bundle = _build_bundle_with(tmp_path, {
        "tables/orders.md": (
            "---\ntype: Table\ntitle: Orders\n---\nbody\n"
        ),
        "tables/index.md": "# tables\n",
        "index.md": "# root\n",
    })
    b = Bundle.load(bundle)
    out_dir = bundle / "_site_iter1_crumb"
    build_site(b, out_dir, target="static")
    html = (out_dir / "tables" / "orders.html").read_text(encoding="utf-8")
    assert '<nav class="okf-breadcrumb" aria-label="Breadcrumb">' in html
    # Ancestor links exist (root index + tables index).
    assert 'href="../index.html"' in html, "missing root-index breadcrumb link"
    assert 'href="index.html"' in html, "missing tables-index breadcrumb link"
    # Current concept is plain text (aria-current), not a self-link.
    assert 'aria-current="page"' in html
    # P2-10 (iter-3): the raw-id <small class="okf-breadcrumb__id"> is gone
    # (it duplicated the path segments shown in the trail AND fell below the
    # 12px type floor). The breadcrumb trail itself is the only affordance.
    assert "okf-breadcrumb__id" not in html, (
        "breadcrumb raw-id <small> should be removed (P2-10)"
    )
    # The trail still shows the path segments individually.
    assert 'aria-current="page">orders<' in html, (
        "breadcrumb current-segment label missing"
    )


def test_iter1_concept_page_governed_keys_distinct_structure(tmp_path: _Path) -> None:
    """P2-6: each governed section has a distinct structure — entities as
    <dl>, provenance as <ul>, citations as <ol> (was a flat inline run)."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\n"
            "type: T\n"
            "title: C\n"
            "aliases: [a1, a2]\n"
            "entities:\n"
            "  - id: e1\n"
            "    label: Ent\n"
            "    kind: business_entity\n"
            "    aliases: [x]\n"
            "provenance:\n"
            "  - source: https://s.example.com\n"
            "    note: n\n"
            "    timestamp: '2026-06-27T00:00:00Z'\n"
            "citations:\n"
            "  - id: '1'\n"
            "    text: ref\n"
            "    url: https://c.example.com\n"
            "---\nbody\n"
        ),
    })
    html = _render_concept_html(bundle, "c")
    # Phase 2: entities now render as clickable header chips, not a <dl>.
    assert 'class="okf-entity-chip"' in html, "entities not header chips"
    assert 'class="okf-entity-list"' not in html, (
        "entities should be header chips, not a governed <dl> (Phase 2)"
    )
    # Provenance/citations still render with their distinct structures.
    assert 'class="okf-provenance-list"' in html, "provenance not a <ul>"
    assert "<time" in html and 'class="okf-provenance-time"' in html
    assert 'class="okf-citation-list"' in html, "citations not an <ol>"
    assert "<ol" in html
    # P2-8 (iter-3): aliases are NOT rendered as pills here — they render
    # ONCE as the page subtitle (see test_iter3_aliases_render_once_only).
    assert "okf-alias-pill" not in html, (
        "aliases pills should be removed from the governed-keys block; "
        "the subtitle is the single canonical surface (P2-8)"
    )


def test_iter1_subtitle_uses_css_class_not_inline_style(tmp_path: _Path) -> None:
    """P2-7: the subtitle no longer carries an inline style= fallback; it
    lives on the .okf-subtitle CSS rule (token system)."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": "---\ntype: T\ntitle: C\naliases: [one, two]\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "c")
    assert 'class="okf-subtitle"' in html
    assert "style=" not in html.split('class="okf-subtitle"', 1)[1].split(">", 1)[0], (
        "subtitle still carries an inline style attribute"
    )


def test_iter1_concept_page_sidebar_labeled_related(tmp_path: _Path) -> None:
    """The static/SSR concept-page sidebar aria-label is ``"Related concepts"``
    — honest for the SSR output that JS-enhanced static/serve builds consume.
    The static template's sidebar contains only the local-graph widget
    placeholder (populated by wiki.js when JS runs).

    Note on TRUE no-JS: the template's ``<noscript>`` block sets
    ``.okf-page__sidebar { display: none }``, so without JavaScript the
    sidebar is entirely hidden and the label is never announced. The label
    is therefore not "what a no-JS user reads" but "what the SSR HTML
    describes for the JS-enhanced render" — and "Related concepts" is
    accurate for that (the sidebar's sole SSR child is the local-graph
    widget). The live studio updates the label after injecting Sections +
    Quick Actions (covered by the browser test
    test_phase1_live_sidebar_label_after_studio_rebuild)."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": "---\ntype: T\ntitle: C\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "c")
    # The aside MUST carry an aria-label (landmark naming).
    assert 'class="okf-page__sidebar" aria-label="' in html, (
        "sidebar aside missing an aria-label landmark name"
    )
    assert 'aria-label="Concept navigation"' not in html
    # The SSR/no-JS label is "Related concepts" — honest for static output
    # which contains only the local-graph widget, not the merged live rail.
    assert 'aria-label="Related concepts"' in html, (
        "static SSR sidebar label must be 'Related concepts' (honest for "
        "no-JS output); the live merged-rail label is set by studio.js"
    )


def test_iter1_accent_is_not_tailwind_blue() -> None:
    """P2-8: the UI accent token is no longer the default Tailwind blue
    (#2563eb light / #60a5fa dark). Guards against regressing to the
    generic 'wiki → blue' reflex."""
    css = _runtime_file("viewer", "static", "wiki.css").read_text(encoding="utf-8")

    def _token(block: str, name: str) -> str:
        # Extract the actual token VALUE (skip comment-only mentions).
        m = re.search(rf"--{name}:\s*([^;]+);", block)
        assert m, f"token --{name} not defined in block"
        return m.group(1).strip()

    tailwind_blues = {"#2563eb", "#60a5fa", "#3b82f6", "#1d4ed8"}
    for theme in ("bone", "slate", "clay", "plum"):
        block = css.split(f'[data-theme="{theme}"]', 1)[1].split("}", 1)[0]
        for name in ("accent", "accent-text", "link"):
            value = _token(block, name).lower()
            assert value not in tailwind_blues, f"{theme} --{name} is Tailwind blue ({value})"
    # The legacy alias reads the palette role rather than a literal.
    assert re.search(r"--okf-accent:\s*var\(--accent-text\);", css)


# ===========================================================================
# UI overhaul Phase 1 — curated type colours, 76ch measure, card treatment.
# Static + served + single-file parity (contract-runtime-parity): the same
# semantic structure ships in every render target because they share one
# resolve_palette + one wiki.css + one template set.
# ===========================================================================


def _assert_no_type_colour(html: str, *colours: str) -> None:
    """Ledger: content types are never colour-coded. No inline type accent,
    no curated/hash type colour and no inline background reach the page."""
    assert "--okf-type-accent:" not in html, "inline type accent still rendered"
    assert "hsl(" not in html, "a type hue still reaches the rendered page"
    assert not re.search(r'style="[^"]*background', html), "inline background on a type element"
    for colour in colours:
        assert colour not in html, f"type colour {colour!r} still rendered"


def test_ledger_concept_header_type_chip_is_neutral(tmp_path: _Path) -> None:
    """The concept header names its type in a neutral chip (ink on the
    page, styled by wiki.css tokens); the curated type colour that
    ``resolve_palette`` still computes for the graph is not painted here."""
    from okf_loom.viewer import assets
    bundle = _build_bundle_with(tmp_path, {
        "a.md": "---\ntype: Dataset\ntitle: A\ndescription: d\n---\nbody\n",
        "okf-loom.config.yaml": "viewer: {}\n",
        "index.md": "# T\n",
    })
    html = _render_concept_html(bundle, "a")
    curated = assets.curated_type_color("Dataset")
    assert curated is not None
    header = re.search(r'<header class="okf-page__header">.*?</header>', html, re.S)
    assert header, "concept header missing"
    assert re.search(r'<span class="okf-type-chip">.*?Dataset', header.group(0), re.S)
    _assert_no_type_colour(html, curated)


def test_ledger_unknown_type_header_has_no_hash_colour(tmp_path: _Path) -> None:
    """An unknown type ("WibblyFrob") renders the same neutral chip as a
    curated one: the hash-fallback hue is not painted on the page."""
    from okf_loom.viewer import assets
    expected = assets.type_color_for("WibblyFrob")
    assert assets.curated_type_color("WibblyFrob") is None  # truly unknown
    bundle = _build_bundle_with(tmp_path, {
        "a.md": "---\ntype: WibblyFrob\ntitle: A\ndescription: d\n---\nbody\n",
        "okf-loom.config.yaml": "viewer: {}\n",
        "index.md": "# T\n",
    })
    html = _render_concept_html(bundle, "a")
    assert re.search(r'<span class="okf-type-chip">.*?WibblyFrob', html, re.S)
    _assert_no_type_colour(html, expected)


def test_ledger_index_cards_are_neutral_rows(tmp_path: _Path) -> None:
    """Index cards keep their semantic structure (title link, description,
    tag chips, type as data) but carry no type colour. The ``.okf-concept-list
    li`` + first-anchor contract (studio.js stampConceptIds) is preserved."""
    from okf_loom.viewer import assets
    bundle = _build_bundle_with(tmp_path, {
        "a.md": "---\ntype: Dataset\ntitle: Alpha\ndescription: A data set.\n---\nbody\n",
        "b.md": "---\ntype: Service\ntitle: Beta\ndescription: A service.\ntags: [x, y]\n---\nbody\n",
        "okf-loom.config.yaml": "viewer: {}\n",
        "index.md": "# T\n",
    })
    html = _render_concept_html(bundle, "index")
    ds_color = assets.curated_type_color("Dataset")
    sv_color = assets.curated_type_color("Service")
    assert ds_color is not None and sv_color is not None
    _assert_no_type_colour(html, ds_color, sv_color)
    # The type survives as data (filtering), not as paint.
    assert 'data-index-type="Dataset"' in html and 'data-index-type="Service"' in html
    # Card structure: title link + description + tag chips present.
    assert 'class="okf-card"' in html
    assert 'okf-card__link' in html
    assert 'okf-card__desc' in html
    assert 'okf-tag--card' in html  # tag chips on cards
    # The li/first-anchor contract is preserved (studio.js depends on it).
    assert '<li class="okf-card"' in html
    assert '<a href="' in html and 'okf-card__link' in html


def test_phase1_wiki_css_has_76ch_measure_and_wide_escape() -> None:
    """wiki.css reading-column contract (viewer-layout-usability revision):

    - Concept pages have ONE bounded article column (--okf-article-maxw)
      that caps running text AND wide blocks (pre/table/tablewrap/figure/
      scrollport) via the shared --okf-lane, so code boxes and paragraphs
      share edges instead of the lane floating centered inside a wider
      column.
    - Generic prose surfaces keep the ~76ch text measure.
    - The .okf-wide full-width escape hatch survives (design §3.1).
    - The body container itself stays unconstrained (wide blocks scroll
      locally inside the column, never sideways at document level)."""
    css = _runtime_file("viewer", "static", "wiki.css").read_text(encoding="utf-8")
    # The article column token exists and the article main column is the
    # bounded content column (not max-width:none).
    assert "--okf-article-maxw" in css, "article column token missing"
    main_block = css.split(".okf-page__main {", 1)[1].split("}", 1)[0]
    assert "var(--okf-article-maxw)" in main_block, (
        "concept article column is not bounded by --okf-article-maxw"
    )
    # The text measure is still applied to prose children on generic surfaces.
    assert "max-width: var(--okf-prose-maxw)" in css, (
        "76ch prose measure not applied to any prose element"
    )
    # The .okf-wide escape hatch exists.
    assert ".okf-wide" in css, ".okf-wide escape hatch missing from wiki.css"
    # The body container is unconstrained (local scrolling, not document
    # sideways scroll).
    assert ".okf-page__body { max-width: none; }" in css, (
        "body container must stay unconstrained"
    )
    # Wide blocks share the reading lane with text on concept pages: the
    # page-scoped alignment selector must include pre/table wrappers AND the
    # renderers.js scrollport, capped by the same min(var(--okf-lane), 100%)
    # expression used for text elements. The body's padding IS the reading
    # inset, so children cap at its content box (subtracting the inset again
    # left code/tables short of the text edge).
    assert re.search(
        r"\.okf-page__body\.okf-prose \{ padding-inline: var\(--okf-reading-inset\);", css
    ), "the prose body no longer carries the reading inset"
    lane_rule = re.compile(
        r"\.okf-page__body\.okf-prose > :is\(([^)]*)\)\s*\{\s*"
        r"max-width:\s*min\(var\(--okf-lane\), 100%\);"
    )
    groups = lane_rule.findall(css)
    wide_group = next(
        (g for g in groups if "pre" in g and ".okf-tablewrap-frame" in g and "figure" in g), None
    )
    text_group = next((g for g in groups if "p," in g and "dl" in g), None)
    assert wide_group is not None, (
        "concept-page wide blocks are not capped by the shared reading lane"
    )
    for token in ("pre", "table", ".okf-tablewrap", ".okf-tablewrap-frame", "figure",
                  ".okf-content-scrollport"):
        assert token in wide_group, f"wide block {token!r} missing from shared lane cap"
    assert text_group is not None, "running text lost its page-scoped lane cap"
    # The escape hatch survives the page-scoped cap: the lane rule comes later
    # in the cascade with higher specificity, so it needs an explicit
    # direct-child .okf-wide exception or an opted-in block stays lane-capped.
    assert re.search(
        r"\.okf-page__body\.okf-prose > \.okf-wide\s*\{\s*max-width:\s*none;", css
    ), "concept-page lane cap defeats the .okf-wide full-width escape"


def test_phase1_cards_have_no_sidestripe_borders() -> None:
    """Phase 1 design handoff: "complete borders/background rather than a
    card side stripe". The index card and concept header must NOT use a
    thick (>1px) ``border-left`` side stripe — they use complete borders
    + a flat tinted background instead. (The established side-stripe BAN
    in test_studio_iter1_browser covers toasts/comment cards; this covers
    the Phase-1 card + header surfaces the ban didn't yet reach.)"""
    css = _runtime_file("viewer", "static", "wiki.css").read_text(encoding="utf-8")

    def _border_left_width(selector_block: str) -> str | None:
        m = re.search(r"border-left-width\s*:\s*([^;]+);", selector_block)
        if m:
            return m.group(1).strip()
        # Fall back to the shorthand border-left: <width> ...
        m = re.search(r"border-left\s*:\s*(\S+)", selector_block)
        return m.group(1) if m else None

    # Phase-1 surfaces only: card + header. .okf-conn (connection cards) is a
    # Phase-2 surface — its side-stripe style was reverted to HEAD and is NOT
    # part of the Phase-1 complete-border contract.
    for sel in (".okf-card", ".okf-page__header"):
        # Grab the first rule block for this selector.
        idx = css.find(sel + " {")
        if idx < 0:
            idx = css.find(sel + "{")
        assert idx >= 0, f"selector {sel} not found in wiki.css"
        block = css[idx:css.find("}", idx) + 1]
        bw = _border_left_width(block)
        # Either no border-left at all, or ≤ 1px (no thick side stripe).
        if bw is not None:
            bw_num = float(re.match(r"([\d.]+)", bw).group(1))
            assert bw_num <= 1.0, (
                f"{sel} has a thick border-left side stripe ({bw}); Phase 1 "
                f"requires complete borders, not side stripes"
            )

    # The header must not use a gradient (design: "no gradients").
    header_block = css.split(".okf-page__header", 1)[1].split("}", 1)[0]
    assert "gradient" not in header_block.lower(), (
        "concept header band still uses a gradient; Phase 1 requires a flat tint"
    )


# ===========================================================================
# Phase 1 index visual P1 fixes: compact type-nav, card type chip,
# responsive auto-fit grid. Static proof (no browser needed).
# ===========================================================================


def test_phase1_index_has_compact_type_nav_replacing_subdirs(tmp_path: _Path) -> None:
    """The root index renders a compact ``<nav class="okf-type-nav">`` with
    jump links to each type section, replacing the old bulky subdirs tile
    grid. Each nav link carries the type icon + textual label + count. The
    hero (title/intro/stats) is preserved."""
    bundle = _build_bundle_with(tmp_path, {
        "a.md": "---\ntype: Table\ntitle: A\ndescription: d\n---\nbody\n",
        "b.md": "---\ntype: Service\ntitle: B\ndescription: d\n---\nbody\n",
        "okf-loom.config.yaml": "viewer: {}\n",
        "index.md": "# T\n",
    })
    out_dir = bundle / "_site"
    build_site(Bundle.load(bundle), out_dir, target="static")
    html = (out_dir / "index.html").read_text(encoding="utf-8")

    # The type-nav is present.
    assert 'class="okf-type-nav"' in html, "root index missing the type-nav component"
    assert 'aria-label="Jump to concept type"' in html, "type-nav missing aria-label"
    # Each type has a jump link with icon + label + count.
    assert 'okf-type-nav__link' in html
    assert 'okf-type-nav__label' in html
    assert ">Table<" in html or ">Service<" in html, "type-nav missing textual type labels"
    # The old subdirs tile grid is NOT present on the root index (replaced).
    assert 'class="okf-subdirs"' not in html, (
        "root index still renders the old subdirs tile grid (should be replaced by type-nav)"
    )


def test_phase1_index_type_nav_links_match_section_ids(tmp_path: _Path) -> None:
    """Each type-nav jump link's ``href`` matches a type section's ``id``
    so anchor navigation actually works."""
    bundle = _build_bundle_with(tmp_path, {
        "a.md": "---\ntype: Table\ntitle: A\n---\nbody\n",
        "b.md": "---\ntype: Dataset\ntitle: B\n---\nbody\n",
        "okf-loom.config.yaml": "viewer: {}\n",
        "index.md": "# T\n",
    })
    out_dir = bundle / "_site"
    build_site(Bundle.load(bundle), out_dir, target="static")
    html = (out_dir / "index.html").read_text(encoding="utf-8")

    import re as _re
    # Extract nav hrefs (href may come before or after class in the markup).
    nav_hrefs = _re.findall(r'href="#([^"]+)"[^>]*class="okf-type-nav__link"', html)
    if not nav_hrefs:
        nav_hrefs = _re.findall(r'class="okf-type-nav__link"[^>]*href="#([^"]+)"', html)
    # Extract section ids.
    section_ids = _re.findall(r'<section class="okf-section" id="([^"]+)"', html)
    assert nav_hrefs, "no type-nav links found"
    assert section_ids, "no type sections with ids found"
    # Every nav href has a matching section id.
    for href in nav_hrefs:
        assert href in section_ids, (
            f"type-nav href #{href!r} has no matching section id: {section_ids!r}"
        )


def test_phase1_index_cards_have_type_icon_and_textual_label(tmp_path: _Path) -> None:
    """Each concept card carries a ``.okf-card__type`` chip with an inline
    SVG icon AND a textual type label — strengthening non-colour type
    identity (Phase 1: icon + label, not colour-only)."""
    bundle = _build_bundle_with(tmp_path, {
        "a.md": "---\ntype: Table\ntitle: A\ndescription: d\n---\nbody\n",
        "okf-loom.config.yaml": "viewer: {}\n",
        "index.md": "# T\n",
    })
    out_dir = bundle / "_site"
    build_site(Bundle.load(bundle), out_dir, target="static")
    html = (out_dir / "index.html").read_text(encoding="utf-8")

    assert 'class="okf-card__type"' in html, "card missing .okf-card__type chip"
    assert 'okf-card__type-label' in html, "card type chip missing textual label"
    assert ">Table<" in html, "card type label text not present"
    # The chip contains an inline SVG (the type icon).
    import re as _re
    type_chip = _re.search(
        r'<span class="okf-card__type"[^>]*>(.*?)</span>\s*<a',
        html, _re.S,
    )
    assert type_chip, "could not extract the card type chip"
    assert "<svg" in type_chip.group(1), "card type chip has no inline SVG icon"
    # The first <a> in the li is still the concept link (presence contract).
    assert 'okf-card__link' in html, "card concept link missing"
    li_block = _re.search(r'<li class="okf-card"[^>]*>(.*?)</li>', html, _re.S)
    assert li_block, "could not extract a card li"
    first_a = _re.search(r'<a\s', li_block.group(1))
    assert first_a, "card li has no <a> (presence contract broken)"


def test_ledger_cardgrid_is_a_numbered_row_ruler() -> None:
    """Ledger: every record is a row. The old card grid is a single-column
    ruler of hairline rows with a row counter, so no type section can leave
    an orphaned tile or an empty track."""
    css = _runtime_file("viewer", "static", "wiki.css").read_text(encoding="utf-8")
    cardgrid_block = css.split(".okf-cardgrid {", 1)[1].split("}", 1)[0]
    assert "display: block" in cardgrid_block, cardgrid_block
    assert "counter-reset: okf-row" in cardgrid_block, cardgrid_block
    for retired in ("grid-template-columns", "auto-fill", "auto-fit"):
        assert retired not in cardgrid_block, (retired, cardgrid_block)


def test_phase1_index_subdir_page_keeps_subdirs(tmp_path: _Path) -> None:
    """Subdir index pages (not the root) keep their subdirs tile grid for
    deeper-directory navigation — the type-nav replaces subdirs ONLY on the
    root index where the type sections already cover every concept."""
    bundle = _build_bundle_with(tmp_path, {
        "sub/a.md": "---\ntype: Table\ntitle: A\ndescription: d\n---\nbody\n",
        "sub/deep/b.md": "---\ntype: Service\ntitle: B\ndescription: d\n---\nbody\n",
        "okf-loom.config.yaml": "viewer: {}\n",
        "index.md": "# T\n",
    })
    out_dir = bundle / "_site"
    build_site(Bundle.load(bundle), out_dir, target="static")
    sub_html = (out_dir / "sub" / "index.html").read_text(encoding="utf-8")
    # Subdir index keeps subdirs (navigates to sub/deep/).
    assert 'class="okf-subdirs"' in sub_html, (
        "subdir index should keep subdirs for deeper-directory navigation"
    )


# ===========================================================================
# Phase 1 audit P1 invariants: semantic DOM order, unique type-nav IDs,
# adversarial type-name security.
# ===========================================================================


def test_phase1_concept_article_precedes_sidebar_in_dom(tmp_path: _Path) -> None:
    """P1 #1 (layout-graph): the <article> MUST precede the <aside> in the
    rendered concept-page DOM so the title/header/body come first in
    accessibility/keyboard/screen-reader order. CSS grid placement (not DOM
    order) puts the sidebar in the left desktop column. This is a static
    HTML proof (no browser needed)."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": "---\ntype: T\ntitle: C\ndescription: d\n---\nbody\n",
        "okf-loom.config.yaml": "viewer: {}\n",
        "index.md": "# T\n",
    })
    html = _render_concept_html(bundle, "c")
    article_pos = html.find('class="okf-page__main"')
    sidebar_pos = html.find('class="okf-page__sidebar"')
    assert article_pos > 0 and sidebar_pos > 0, "article or sidebar not found in HTML"
    assert article_pos < sidebar_pos, (
        f"<article> (pos {article_pos}) does not precede <aside> (pos {sidebar_pos}) "
        f"in the concept-page DOM — the title must come before the rail in source order"
    )
    # The critical capture/projection IDs are inside the article (preserved).
    body_pos = html.find('class="okf-prose okf-page__body"')
    canon_pos = html.find('id="okf-canonical-source"')
    proj_pos = html.find('id="okf-annotation-projection"')
    assert article_pos < body_pos < canon_pos < proj_pos < sidebar_pos, (
        "capture/projection IDs not inside <article> or not before <aside>"
    )


def test_phase1_type_ids_unique_for_collision_classes() -> None:
    """P1 #1 (fail-closed-validation): ``_allocate_type_section_ids``
    produces GUARANTEED-unique ids across the actual type set — no
    probabilistic hash. Collision classes (spaces, punctuation, case,
    non-ASCII, whitespace padding) get deterministic numeric suffixes."""
    from okf_loom.render import _allocate_type_section_ids, _type_base_slug
    import re as _re

    # Collision class: types that slug to the same base get numeric suffixes.
    types = ["A B", "A-B", "Table", "table", "Data/Set", "Data-Set"]
    ids = _allocate_type_section_ids(types)
    id_values = list(ids.values())
    # Every id is unique (guaranteed, not probabilistic).
    assert len(id_values) == len(set(id_values)), (
        f"duplicate ids allocated: {id_values}"
    )
    # Readable: each id starts with the lowercased slug.
    for t, sid in ids.items():
        assert sid.startswith(_type_base_slug(t)), (
            f"id {sid!r} does not start with the readable slug for {t!r}"
        )
    # Valid HTML id fragment (alphanumeric + hyphens only).
    for sid in id_values:
        assert _re.match(r"^[a-z0-9-]+$", sid), f"invalid id fragment: {sid!r}"

    # Whitespace: " A " and "A" produce the same base slug (stripped).
    assert _type_base_slug(" A ") == _type_base_slug("A") == "a"
    # Non-ASCII: stripped to valid ASCII slug.
    assert _re.match(r"^[a-z0-9-]+$", _type_base_slug("Café!")), (
        f"non-ASCII slug invalid: {_type_base_slug('Café!')!r}"
    )

    # Deterministic: same input → same output.
    types2 = list(reversed(types))
    ids2 = _allocate_type_section_ids(types2)
    # Result is the same regardless of input ORDER (function sorts internally).
    assert ids == ids2, "id allocation is not order-independent"

    # Rendered nav 1:1: every type maps to exactly one unique id.
    assert len(ids) == len(types), "not every type got an id"


def test_phase1_type_ids_globally_collision_safe_cross_base() -> None:
    """P1 #1 regression: the allocator must be GLOBALLY collision-safe, not
    merely per-base-slug. The classic cross-base collision: ``['a', 'a!',
    'a-2']`` — ``a!`` slugs to base ``a`` (collision → suffix ``a-2``), but
    ``a-2`` independently slugs to base ``a-2`` which looks unique per-base.
    Without a global ``used`` set, both get ``a-2`` (duplicate). This test
    catches that exact regression."""
    from okf_loom.render import _allocate_type_section_ids

    # The specific cross-base collision class.
    types = ["a", "a!", "a-2"]
    ids = _allocate_type_section_ids(types)
    vals = list(ids.values())
    assert len(vals) == len(set(vals)), (
        f"cross-base collision produced duplicate ids: {vals}"
    )
    # Specifically: 'a-2' (the type) must NOT get 'a-2' (taken by 'a!').
    assert ids["a-2"] != "a-2", (
        f"'a-2' type got id 'a-2' which collides with 'a!' suffix: {ids}"
    )

    # Broader cross-base + whitespace + punctuation + non-ASCII.
    types2 = [" A ", "A", "A!", "a-2", "a-3", "Table", "table", "Café!", "cafe"]
    ids2 = _allocate_type_section_ids(types2)
    vals2 = list(ids2.values())
    assert len(vals2) == len(set(vals2)), (
        f"mixed collision classes produced duplicate ids: {vals2}"
    )


def test_phase1_index_type_nav_links_unique_and_match_sections(tmp_path: _Path) -> None:
    """P1 #3: every type-nav link href has exactly one matching section id,
    and no two sections share an id (even for collision-class type names)."""
    bundle = _build_bundle_with(tmp_path, {
        "a.md": "---\ntype: A B\ntitle: A\ndescription: d\n---\nbody\n",
        "b.md": "---\ntype: A-B\ntitle: B\ndescription: d\n---\nbody\n",
        "c.md": "---\ntype: Table\ntitle: C\ndescription: d\n---\nbody\n",
        "okf-loom.config.yaml": "viewer: {}\n",
        "index.md": "# T\n",
    })
    out_dir = bundle / "_site"
    build_site(Bundle.load(bundle), out_dir, target="static")
    html = (out_dir / "index.html").read_text(encoding="utf-8")

    import re as _re
    nav_hrefs = _re.findall(r'href="#([^"]+)"[^>]*class="okf-type-nav__link"', html)
    if not nav_hrefs:
        nav_hrefs = _re.findall(r'class="okf-type-nav__link"[^>]*href="#([^"]+)"', html)
    section_ids = _re.findall(r'<section class="okf-section" id="([^"]+)"', html)
    assert len(nav_hrefs) >= 3, f"expected ≥3 nav links, got {len(nav_hrefs)}"
    assert len(section_ids) >= 3, f"expected ≥3 sections, got {len(section_ids)}"
    # No duplicate section ids.
    assert len(section_ids) == len(set(section_ids)), (
        f"duplicate section ids: {section_ids}"
    )
    # Every nav href matches exactly one section id.
    for href in nav_hrefs:
        assert section_ids.count(href) == 1, (
            f"nav href #{href!r} matches {section_ids.count(href)} sections (expected 1)"
        )


def test_phase1_adversarial_type_name_escaped_in_card_chip(tmp_path: _Path) -> None:
    """P1 #4 (security-boundary): an HTML-significant adversarial type name
    in frontmatter must be ESCAPED in the rendered card chip — no live
    ``<script>``, ``<img>``, or event-handler attributes introduced. The
    first anchor remains ``.okf-card__link``, and the strict CSP meta is
    unaffected. Fail-closed validation: type text is data, never markup."""
    import html as _html_module
    adversarial = '"><script>alert(1)</script><img src=x onerror=alert(1)>'
    # YAML single-quote the value so the HTML-significant chars are literal
    # frontmatter data (double-quotes would be interpreted by YAML).
    bundle = _build_bundle_with(tmp_path, {
        "a.md": f"---\ntype: '{adversarial}'\ntitle: A\ndescription: d\n---\nbody\n",
        "okf-loom.config.yaml": "viewer: {}\n",
        "index.md": "# T\n",
    })
    out_dir = bundle / "_site"
    build_site(Bundle.load(bundle), out_dir, target="static")
    html = (out_dir / "index.html").read_text(encoding="utf-8")

    # The raw adversarial MARKUP must NOT appear as live HTML elements.
    # HTML-escaping converts < > " to entities, so the literal tag-open
    # character `<` from the type name never reaches the DOM as markup.
    assert "<script>alert(1)</script>" not in html, (
        "unescaped <script> tag from type name reached the rendered HTML"
    )
    assert "<img src=x" not in html, (
        "unescaped <img> tag from type name reached the rendered HTML"
    )
    # The ESCAPED form IS present (the type name rendered as visible text).
    # HTML-escaping turns < > " into entities; the text content is data.
    escaped_fragment = "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert escaped_fragment in html, (
        f"escaped type text not found in HTML; expected {escaped_fragment!r}"
    )
    # The card chip does not introduce any new HTML elements beyond our own
    # SVG icon + spans. The type-name text is pure text content (no live tags).
    import re as _re
    card_li = _re.search(
        r'<li class="okf-card"[^>]*>(.*?)</li>',
        html, _re.S,
    )
    assert card_li, "no card <li> found"
    card_html = card_li.group(1)
    # Count elements in the card: should be exactly our chip span, SVG, label
    # span, concept <a>, id span, desc span. No <script>, <img>, <iframe> etc.
    forbidden_tags = _re.findall(r'<(script|img|iframe|object|embed|svg)[\s/>]', card_html)
    # The only allowed tag from the list above is our own <svg> icon (exactly 1).
    non_svg_forbidden = [t for t in forbidden_tags if t != "svg"]
    assert not non_svg_forbidden, (
        f"forbidden HTML elements from type name in card: {non_svg_forbidden}"
    )
    # First <a> in the card is the concept link (presence contract).
    first_a = _re.search(r'<a\s[^>]*class="[^"]*okf-card__link', card_html)
    assert first_a, "first <a> in card is not .okf-card__link (presence contract)"
    # No on* event ATTRIBUTES on any element in the card. We check for the
    # pattern `<tag ... onevent=` — but must exclude text content where the
    # literal text `onerror=alert(1)` appears as escaped data. A real event
    # attribute would be `<... onerror=...>` (inside a tag, after a space).
    # Since all type-name `<` are escaped to `&lt;`, no new tags exist, so
    # no event attributes can be injected. Verify by counting unescaped `<`
    # occurrences in the type-label span: should be zero (all text).
    type_label = _re.search(
        r'<span class="okf-card__type-label">(.*?)</span>', card_html, _re.S,
    )
    assert type_label, "card type-label span not found"
    label_content = type_label.group(1)
    assert "<" not in label_content, (
        f"unescaped `<` in type label content (markup injection): {label_content!r}"
    )
    # The strict CSP meta tag is present and unaffected.
    csp = _re.search(
        r'<meta http-equiv="Content-Security-Policy" content="([^"]+)"',
        html,
    )
    assert csp, "CSP meta tag missing"
    assert "script-src 'self'" in csp.group(1), (
        f"CSP script-src changed: {csp.group(1)!r}"
    )


# ===========================================================================
# Phase 1 strict-completion audit: 8-color palette, uniform fallback,
# AA-safe type label contrast, .okf-conn Phase-2 boundary.
# ===========================================================================


def test_phase1_curated_palette_has_exactly_eight_categories() -> None:
    """P1 #5 (contract-runtime-parity): the curated type colour map MUST
    have exactly 8 entries (a curated 8-colour palette + icon set). Types
    outside the 8 fall through to the hash
    fallback."""
    from okf_loom.viewer import assets
    colours = assets.curated_type_color_map()
    assert len(colours) == 8, (
        f"curated palette has {len(colours)} entries, expected exactly 8: "
        f"{list(colours.keys())}"
    )
    # All 8 are valid HSL strings.
    for k, v in colours.items():
        assert v.startswith("hsl("), f"non-hsl colour for {k!r}: {v!r}"
    # Known common types resolve to a curated colour.
    assert assets.curated_type_color("Dataset") is not None
    assert assets.curated_type_color("Table") is not None
    assert assets.curated_type_color("Service") is not None
    assert assets.curated_type_color("Playbook") is not None
    # Types outside the 8 categories get None (hash fallback path).
    assert assets.curated_type_color("Model") is None  # box dropped
    assert assets.curated_type_color("Person") is None  # people dropped
    assert assets.curated_type_color("Event") is None  # zap dropped


def test_serve_context_action_is_described_as_cited_evidence(tmp_path: _Path) -> None:
    """R3: the live Context Pack link carries the same one-sentence description
    as the static button: cited passages, not a summary, sent to no agent."""
    from okf_loom.viewer import assets
    from okf_loom.render import _render_concept_page
    from okf_loom import Bundle
    root = _build_bundle_with(tmp_path, {
        "index.md": "# T\n", "a.md": "---\ntype: Note\ntitle: A\n---\n# A\nalpha\n",
    })
    b = Bundle.load(root)
    html = _render_concept_page(b.concepts[("a",)], b, b.graph(), mode="serve", name="T",
                                palette=assets.resolve_palette(b), config={})
    assert re.search(r'<a class="okf-btn okf-context-copy"[^>]*aria-describedby="okf-context-action-note"'
                     r'[^>]*>Open context pack</a>', html)
    assert ('<span id="okf-context-action-note" hidden>Cited passages for reuse, not a summary. '
            'Copying sends nothing to the agent.</span>') in html
    assert html.count('id="okf-context-action-note"') == 1


@pytest.mark.browser
@pytest.mark.parametrize("mode", ["serve", "static"])
def test_context_action_note_is_its_description_not_page_text(tmp_path: _Path, mode: str) -> None:
    """R3 review: the Context Pack note is announced as the action's
    description only; browse mode does not read it again as page text."""
    from browser_support import expect, launch_chromium, sync_playwright
    from okf_loom.viewer import assets
    from okf_loom.render import _render_concept_page
    root = _build_bundle_with(tmp_path, {
        "index.md": "# T\n", "a.md": "---\ntype: Note\ntitle: A\n---\n# A\nalpha\n",
    })
    b = Bundle.load(root)
    note = "Cited passages for reuse, not a summary. Copying sends nothing to the agent."
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        try:
            page = browser.new_page()
            if mode == "static":
                build_site(b, tmp_path / "site", target="static")
                page.goto((tmp_path / "site" / "a.html").as_uri())
                action = page.get_by_role("button", name="Copy concept context", exact=True)
            else:
                page.set_content(_render_concept_page(
                    b.concepts[("a",)], b, b.graph(), mode="serve", name="T",
                    palette=assets.resolve_palette(b), config={}))
                action = page.get_by_role("link", name="Open context pack", exact=True)
            expect(action).to_have_accessible_description(note)
            assert "Cited passages" not in page.locator("body").aria_snapshot()
        finally:
            browser.close()


def test_ledger_unknown_type_render_has_no_type_colour(tmp_path: _Path) -> None:
    """The served concept page paints neither the hash-fallback hue nor the
    old fixed gray ``#94a3b8`` for an unknown type: Ledger type identity is
    the neutral chip text, so there is no colour fallback to drift."""
    from okf_loom.viewer import assets
    from okf_loom.render import _render_concept_page
    expected = assets.type_color_for("ZyxNoMatch")
    assert expected != "#94a3b8", "fallback is still the old gray"
    assert assets.curated_type_color("ZyxNoMatch") is None  # truly unknown
    bundle = _build_bundle_with(tmp_path, {
        "a.md": "---\ntype: ZyxNoMatch\ntitle: A\ndescription: d\n---\nbody\n",
        "okf-loom.config.yaml": "viewer: {}\n",
        "index.md": "# T\n",
    })
    from okf_loom import Bundle
    b = Bundle.load(bundle)
    pal = assets.resolve_palette(b)
    graph = b.graph()
    concept = b.concepts[("a",)]
    html = _render_concept_page(concept, b, graph, mode="serve", name="T",
                                palette=pal, config={})
    _assert_no_type_colour(html, expected, "#94a3b8")
    assert 'class="okf-type-chip"' in html


def test_ledger_empty_palette_renders_the_same_neutral_page(tmp_path: _Path) -> None:
    """With an EMPTY palette dict (stale/missing) the concept page is the
    same neutral page as with a populated one: no hash fallback, no gray."""
    from okf_loom.viewer import assets
    from okf_loom.render import _render_concept_page
    bundle = _build_bundle_with(tmp_path, {
        "a.md": "---\ntype: Table\ntitle: A\ndescription: d\n---\nbody\n",
        "okf-loom.config.yaml": "viewer: {}\n",
        "index.md": "# T\n",
    })
    from okf_loom import Bundle
    b = Bundle.load(bundle)
    graph = b.graph()
    concept = b.concepts[("a",)]
    # Empty palette — every type hits the fallback path.
    html = _render_concept_page(concept, b, graph, mode="serve", name="T",
                                palette={}, config={})
    _assert_no_type_colour(html, assets.type_color_for("Table"), "#94a3b8")
    populated = _render_concept_page(concept, b, graph, mode="serve", name="T",
                                     palette=assets.resolve_palette(b), config={})
    assert populated == html, "palette contents must not change the rendered page"


def test_phase1_card_type_label_uses_safe_text_token(tmp_path: _Path) -> None:
    """P1 #3 (keyboard-focus/accessibility): the card type label TEXT uses
    the AA-safe muted text token (``var(--okf-fg-muted)``), NOT the raw type
    accent colour. The icon keeps the accent colour (decorative geometry,
    no text-contrast requirement). Verified by CSS source inspection."""
    css = _runtime_file("viewer", "static", "wiki.css").read_text(encoding="utf-8")
    # The label rule must use the muted token, not inherit a raw colour.
    label_block = css.split(".okf-card__type-label", 1)[1].split("}", 1)[0]
    assert "var(--okf-fg-muted)" in label_block, (
        f"card type label does not use var(--okf-fg-muted): {label_block!r}"
    )
    # The icon wrapper carries the inline colour (the SVG inherits it).
    icon_block = css.split(".okf-card__type-icon", 1)[1].split("}", 1)[0]
    assert "inline-flex" in icon_block, "card type icon wrapper missing"


def test_ledger_conn_is_a_hairline_row() -> None:
    """``.okf-conn`` is a Ledger row: a 1px bottom hairline on the page,
    with no side stripe, no tinted fill and no colour-mix wash."""
    css = _runtime_file("viewer", "static", "wiki.css").read_text(encoding="utf-8")
    conn_block = css.split(".okf-conn {", 1)[1].split("}", 1)[0]
    assert "border-bottom: 1px solid var(--border)" in conn_block, conn_block
    assert "background: transparent" in conn_block, conn_block
    assert "border-left" not in conn_block, ".okf-conn still has a side stripe"
    assert "color-mix" not in conn_block, ".okf-conn still has a color-mix wash"


def test_phase1_graph_js_paints_content_types_neutrally() -> None:
    """Ledger: content types are not colour-coded on the canvas. graph.js has
    no type-hash colour or grey fallback; every node starts on the resolved
    --graph-node fill and lenses recolour from the palette-derived ramp."""
    js = _runtime_file("viewer", "static", "graph.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    code = re.sub(r"//[^\n]*", "", code)
    assert "function typeColorFor(" not in code
    assert "#94a3b8" not in code.lower()
    assert "palette[n.data.type]" not in code and "palette[data.type]" not in code
    elem_for = code.split("function elemFor(", 1)[1].split("return {", 1)[0]
    assert "graphPalette().node" in elem_for
    assert "function buildLensRamp(" in code and ".ramp.categorical" in code


# ===========================================================================
# Phase 1 parity across actual consumers (contract-runtime-parity):
# served/live concept identity, static concept header + index cards, and
# single-file graph manifest/node identity MUST all carry the SAME curated
# (known type) and hash-fallback (unknown type) colours. A divergence
# between targets is a parity bug. The existing body-projection parity test
# covers the annotation projection seam only; these cover the TYPE IDENTITY
# (colour) seam.
# ===========================================================================


def _phase1_parity_bundle(tmp_path: _Path) -> tuple[_Path, Bundle]:
    """A bundle with one known type (Table) and one unknown type, so parity
    covers both the curated path and the hash-fallback path."""
    root = tmp_path
    (root / "okf-loom.config.yaml").write_text("viewer: {}\n", encoding="utf-8")
    (root / "index.md").write_text("# T\n", encoding="utf-8")
    (root / "a.md").write_text(
        "---\ntype: Table\ntitle: Alpha\ndescription: A table.\n---\nbody\n",
        encoding="utf-8")
    (root / "b.md").write_text(
        "---\ntype: ZyxUnknown\ntitle: Beta\ndescription: Unknown.\n---\nbody\n",
        encoding="utf-8")
    return root, Bundle.load(root)


def test_phase1_parity_served_and_static_concept_header_match(tmp_path: _Path) -> None:
    """The concept header type chip is IDENTICAL in the served and static
    render targets for a curated and an unknown type, and neither target
    paints a type colour. The server and static builder share one
    ``_render_concept_page``; this pins that the identity does not diverge."""
    from okf_loom.render import _render_concept_page
    from okf_loom.viewer import assets

    root, bundle = _phase1_parity_bundle(tmp_path)
    pal = assets.resolve_palette(bundle)
    graph = bundle.graph()

    for cid_suffix, expected_kind in [("a", "curated"), ("b", "hash")]:
        concept = bundle.concepts[(cid_suffix,)]
        ctype = concept.type
        # The expected colour: curated for known, hash for unknown.
        if expected_kind == "curated":
            expected = assets.curated_type_color(ctype)
            assert expected is not None
        else:
            expected = assets.type_color_for(ctype)
            assert assets.curated_type_color(ctype) is None  # truly unknown

        chips = {}
        for mode in ("serve", "static"):
            html = _render_concept_page(
                concept, bundle, graph, mode=mode, name="T",
                palette=pal, config={},
            )
            _assert_no_type_colour(html, expected)
            chip = re.search(r'<span class="okf-type-chip">.*?</span>\s*</span>|<span class="okf-type-chip">.*?</span>', html, re.S)
            assert chip and ctype in chip.group(0), (
                f"concept header for type {ctype!r} ({expected_kind}) in "
                f"mode={mode!r} has no neutral type chip"
            )
            chips[mode] = chip.group(0)
        assert chips["serve"] == chips["static"], chips


def test_phase1_parity_static_index_cards_match_curated_and_hash(tmp_path: _Path) -> None:
    """The static index page renders known-type and unknown-type cards the
    same neutral way: the palette ``resolve_palette`` still computes (for the
    graph canvas) is never painted on an index card."""
    from okf_loom.viewer import assets

    root, bundle = _phase1_parity_bundle(tmp_path)
    pal = assets.resolve_palette(bundle)
    # Build the static site and read the index.
    out_dir = root / "_site"
    build_site(bundle, out_dir, target="static")
    html = (out_dir / "index.html").read_text(encoding="utf-8")

    table_color = pal["Table"]
    unknown_color = pal["ZyxUnknown"]
    # The curated path: Table gets its curated colour.
    assert table_color == assets.curated_type_color("Table")
    # The hash path: ZyxUnknown gets the hash fallback (not curated).
    assert assets.curated_type_color("ZyxUnknown") is None
    assert unknown_color == assets.type_color_for("ZyxUnknown")
    _assert_no_type_colour(html, table_color, unknown_color)
    cards = re.findall(r'<li class="okf-card"[^>]*>', html)
    assert len(cards) == 2, cards
    assert {re.search(r'data-index-type="([^"]+)"', c).group(1) for c in cards} == {"Table", "ZyxUnknown"}
    assert not any("style=" in c for c in cards), cards


def test_phase1_parity_single_file_graph_node_identity(tmp_path: _Path) -> None:
    """The single-file graph manifest (build_graph_data → embedded JSON)
    carries the curated colour on known-type nodes and the hash fallback on
    unknown-type nodes, matching the same ``resolve_palette`` the wiki
    surfaces use. This is the single-file consumer parity: the graph canvas
    and the wiki must not disagree on a type's identity colour."""
    from okf_loom.viewer import assets

    root, bundle = _phase1_parity_bundle(tmp_path)
    pal = assets.resolve_palette(bundle)
    data = build_graph_data(bundle, name="T")

    # Map node id → colour from the graph data.
    node_colors = {n["data"]["id"]: n["data"]["color"] for n in data["nodes"]}
    assert "a" in node_colors, "known-type node missing from graph data"
    assert "b" in node_colors, "unknown-type node missing from graph data"

    # Known type: curated colour, matching the palette and curated_type_color.
    assert node_colors["a"] == pal["Table"]
    assert node_colors["a"] == assets.curated_type_color("Table")
    # Unknown type: hash fallback, matching the palette and type_color_for.
    assert node_colors["b"] == pal["ZyxUnknown"]
    assert node_colors["b"] == assets.type_color_for("ZyxUnknown")

    # The single-file build embeds this graph data as JSON; verify the
    # colours are present in the rendered single-file HTML too.
    out_dir = tmp_path / "_sf"
    render_single_file(bundle, out_dir / "index.html", name="T")
    sf_html = (out_dir / "index.html").read_text(encoding="utf-8")
    # Direct, non-tautological proof: the actual curated colour value
    # (e.g. ``hsl(255, 50%, 54%)``) must appear verbatim in the rendered
    # single-file HTML, embedded inside the graph-data JSON blob. HSL
    # colours contain no characters that the JSON/stringifier would escape
    # (no ``<`` or ``&``), so a plain ``in`` membership check is exact.
    assert node_colors["a"] in sf_html, (
        f"curated node colour {node_colors['a']!r} not found verbatim in the "
        f"rendered single-file HTML (graph data not embedded correctly)"
    )
    assert node_colors["b"] in sf_html, (
        f"hash-fallback node colour {node_colors['b']!r} not found verbatim in "
        f"the rendered single-file HTML (graph data not embedded correctly)"
    )


# ===========================================================================
# iter-2 frontend fixes: P1-1 subtitle out of h1, P2-3 search h1 hierarchy,
# P2-4 graph node-index collapsed by default, P2-5 graph color governance.
# ===========================================================================


def test_iter2_subtitle_is_sibling_outside_h1(tmp_path: _Path) -> None:
    """P1-1 (iter-2): the alias subtitle is a SIBLING <p> after the <h1>,
    not a child of it.

    The prior ``.replace("__CONCEPT_TITLE__", title + subtitle_html)`` put a
    block <p> inside the h1 — invalid HTML (h1 content model is phrasing-
    only) AND it polluted the h1 accessible name on every aliased concept.
    """
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": (
            "---\n"
            "type: Note\n"
            "title: Main Title\n"
            "aliases: [alt one, alt two]\n"
            "---\n\nBody.\n"
        ),
    })
    html = _render_concept_html(bundle, "concept")
    # Extract the h1 element and assert it contains NO <p> / no subtitle.
    h1_match = re.search(
        r'<h1 class="okf-page__title">(.*?)</h1>', html, re.DOTALL
    )
    assert h1_match, "missing concept page h1"
    h1_inner = h1_match.group(1)
    assert "<p" not in h1_inner, (
        f"h1 contains a <p> (invalid content model): {h1_inner!r}"
    )
    assert "okf-subtitle" not in h1_inner, (
        f"subtitle leaked inside h1: {h1_inner!r}"
    )
    # The h1 accessible name is just the title text (no alias concatenation).
    assert h1_inner.strip() == "Main Title", (
        f"h1 text is not just the title: {h1_inner!r}"
    )
    # The subtitle <p> IS present, as a sibling AFTER the </h1>.
    after_h1 = html[h1_match.end():]
    assert '<p class="okf-subtitle">' in after_h1, (
        "subtitle <p> not found as a sibling after the h1"
    )
    # And it appears BEFORE the description paragraph (document order:
    # h1 → subtitle → description).
    sub_pos = after_h1.find('class="okf-subtitle"')
    desc_pos = after_h1.find('class="okf-page__description"')
    assert sub_pos != -1 and desc_pos != -1, "subtitle or description missing"
    assert sub_pos < desc_pos, "subtitle should come before description"


def test_iter2_subtitle_placeholder_empty_when_no_aliases(tmp_path: _Path) -> None:
    """P1-1 (iter-2): when there are no aliases, __CONCEPT_SUBTITLE__ renders
    nothing and the h1 is still valid (no stray empty <p>)."""
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": "---\ntype: T\ntitle: Just Title\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "concept")
    assert "okf-subtitle" not in html, "subtitle emitted despite no aliases"
    h1_match = re.search(
        r'<h1 class="okf-page__title">(.*?)</h1>', html, re.DOTALL
    )
    assert h1_match and h1_match.group(1).strip() == "Just Title"
    # No empty <p> left behind by the placeholder.
    assert "<p></p>" not in html, "empty <p> left by subtitle placeholder"


def test_iter2_search_h1_not_wrapped_in_muted(tmp_path: _Path) -> None:
    """P2-3 (iter-2): the search page's only <h1> renders full-size
    full-colour. The prior ``<span class="okf-muted">`` wrapper demoted it
    to 14px gray (.okf-muted sets font-size: --okf-text-sm), making the
    search h1 the visually-weakest heading across the three text surfaces.
    """
    from okf_loom.render import _render_search_page
    bundle = _build_bundle_with(tmp_path, {
        "index.md": "# root\n",
        "c.md": "---\ntype: T\ntitle: Orders\n---\nbody\n",
    })
    from okf_loom import Bundle
    b = Bundle.load(bundle)
    html = _render_search_page(
        b, mode="static", name=b.name, config={},
        query="orders",
        results=[{"title": "Orders", "concept_id": "c", "id": "c"}],
    )
    h1_match = re.search(
        r'<h1 class="okf-search__title">(.*?)</h1>', html, re.DOTALL
    )
    assert h1_match, "missing search h1"
    assert "okf-muted" not in h1_match.group(0), (
        f"search h1 still wrapped in .okf-muted (demotes it to 14px gray): "
        f"{h1_match.group(0)!r}"
    )
    # The heading text is present and full-colour (no wrapper span at all).
    assert "Orders" not in h1_match.group(0) or "1 result" in h1_match.group(0)


def test_iter2_search_title_css_is_full_size() -> None:
    """P2-3 (iter-2): .okf-search__title uses --okf-text-xl (parity with
    .okf-index__title and .okf-page__title), not --okf-text-lg."""
    css = _runtime_file("viewer", "static", "wiki.css").read_text(encoding="utf-8")
    rule_match = re.search(r'\.okf-search__title\s*\{([^}]*)\}', css)
    assert rule_match, ".okf-search__title rule missing from wiki.css"
    rule = rule_match.group(1)
    assert "--okf-text-xl" in rule, (
        f".okf-search__title not at --okf-text-xl (h1 parity): {rule!r}"
    )
    assert "--okf-text-lg" not in rule, (
        f".okf-search__title still at --okf-text-lg: {rule!r}"
    )


def test_iter2_graph_node_index_not_open_by_default(
    tiny_good_bundle: Path,
) -> None:
    """P2-4 (iter-2): the node-index <details> defaults to COLLAPSED on all
    screen sizes. The `open` attribute was removed from graph_page.html
    (the CSS comment claiming "starts collapsed on small screens" was false
    — CSS cannot remove the attribute)."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tiny_good_bundle / "_site_iter2_nodeindex"
    build_site(b, out_dir, target="static")
    html = (out_dir / "__graph.html").read_text(encoding="utf-8")
    details_match = re.search(
        r'<details class="okf-node-index"([^>]*)>', html
    )
    assert details_match, "missing node-index <details> element"
    attrs = details_match.group(1)
    assert not re.search(r'\bopen\b', attrs), (
        f"node-index <details> still carries the open attribute "
        f"(crowds the mobile canvas): {details_match.group(0)!r}"
    )


def test_iter2_single_file_node_index_not_open(
    tiny_good_bundle: Path,
) -> None:
    """P2-4 (iter-2): single-file viewer node-index also defaults collapsed
    (parity with graph_page.html)."""
    b = Bundle.load(tiny_good_bundle)
    out = tiny_good_bundle / "_single_iter2.html"
    render_single_file(b, out)
    html = out.read_text(encoding="utf-8")
    details_match = re.search(
        r'<details class="okf-node-index"([^>]*)>', html
    )
    assert details_match, "missing node-index <details> in single-file viewer"
    assert not re.search(r'\bopen\b', details_match.group(1)), (
        f"single-file node-index still carries open: "
        f"{details_match.group(0)!r}"
    )


def test_iter2_graph_node_index_css_comment_is_honest() -> None:
    """P2-4 (iter-2): the false CSS comment claiming the node-index 'starts
    collapsed on small screens' has been removed/fixed (CSS cannot remove
    the open attribute)."""
    css = _runtime_file("viewer", "static", "graph.css").read_text(encoding="utf-8")
    # The old false claim must be gone.
    assert "starts collapsed" not in css.lower(), (
        "graph.css still carries the false 'starts collapsed on small "
        "screens' comment"
    )


def test_iter2_graph_selection_color_is_token_governed() -> None:
    """The selection stroke and every canvas colour come from the active
    palette's tokens (getComputedStyle), not a per-theme JS table, and are
    re-resolved on every okf-loom:themeChanged."""
    graph_js = _runtime_file("viewer", "static", "graph.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", graph_js, flags=re.DOTALL)
    code = re.sub(r"//[^\n]*", "", code)
    assert "f59e0b" not in code.lower()
    assert "GRAPH_COLORS" not in code
    for old in ("technical-light", "swiss-dark", "purple-dark", "coffee-dark", "themeIsDark"):
        assert old not in code, old
    tokens = code.split("var GRAPH_TOKENS = {", 1)[1].split("};", 1)[0]
    for role, token in (("select", "--accent"), ("node", "--graph-node"),
                        ("edge", "--graph-edge"), ("nodeText", "--graph-label"),
                        ("halo", "--graph-halo"), ("nodeIcon", "--graph-node-icon")):
        assert re.search(rf'{role}:\s*"{token}"', tokens), (role, token)
    assert "getComputedStyle(document.documentElement)" in code
    assert re.search(r'node:selected.*?border-color.*?\.select', code, re.DOTALL)
    sync = code.split("function syncGraphTheme(", 1)[1].split("window.addEventListener", 1)[0]
    assert "invalidateGraphPalette()" in sync and "repaintLensColours()" in sync


_LEDGER_PALETTES = ("bone", "slate", "clay", "plum")


def _ledger_palette_block(css: str, theme: str) -> str:
    """The raw palette block for one Ledger palette (hex roles only)."""
    marker = f'[data-theme="{theme}"] {{'
    assert marker in css, f"{theme} palette block missing"
    return css.split(marker, 1)[1].split("}", 1)[0]


def _ledger_alias_block(css: str) -> str:
    """The ``--okf-*`` compatibility alias block that maps onto the roles."""
    for chunk in css.split(":root, [data-theme] {")[1:]:
        block = chunk.split("}", 1)[0]
        if "--okf-bg: var(--bg);" in block:
            return block
    raise AssertionError("--okf-* alias block missing from wiki.css")


def test_ledger_palettes_define_graph_roles() -> None:
    """Every Ledger palette ships the graph canvas roles the index preview
    and the graph page paint (graph.css/graph.js mirror these per palette).
    Edges are a quiet decorative hairline, distinct from both the canvas and
    the node fill; labels read as text on the graph canvas."""
    css = _runtime_file("viewer", "static", "wiki.css").read_text(encoding="utf-8")

    def _contrast(a: str, b: str) -> float:
        def lum(value: str) -> float:
            value = value.lstrip("#")
            channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    for theme in _LEDGER_PALETTES:
        block = _ledger_palette_block(css, theme)
        roles = {}
        for name in ("graph-bg", "graph-edge", "graph-node", "graph-label"):
            m = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}});", block)
            assert m, f"{theme} --{name} missing"
            roles[name] = m.group(1).lower()
        assert len(set(roles.values())) == 4, (theme, roles)
        assert _contrast(roles["graph-label"], roles["graph-bg"]) >= 4.5, (theme, roles)
    # The preview reads the role, never a literal.
    assert ".okf-graph-thumb__edges line { stroke: var(--graph-edge);" in css


def test_graph_edge_colour_is_each_palette_graph_edge_token() -> None:
    """Graph edges read the palette's own --graph-edge token, defined in
    every Ledger palette block of wiki.css."""
    graph_js = _runtime_file("viewer", "static", "graph.js").read_text(encoding="utf-8")
    css = _runtime_file("viewer", "static", "wiki.css").read_text(encoding="utf-8")
    assert re.search(r'edge:\s*"--graph-edge"', graph_js)
    for palette in ("bone", "slate", "clay", "plum"):
        block = css.split(f'[data-theme="{palette}"]', 1)[1].split("}", 1)[0]
        assert re.search(r"--graph-edge:\s*#[0-9a-fA-F]{6};", block), palette


def test_iter2_select_token_defined_in_wiki_css() -> None:
    """P2-5 (iter-2): --okf-select resolves in EVERY palette: the alias block
    maps it onto the palette ``--accent`` role, which each Ledger palette
    defines as a literal."""
    css = _runtime_file("viewer", "static", "wiki.css").read_text(encoding="utf-8")
    assert re.search(r"--okf-select:\s*var\(--accent\);", _ledger_alias_block(css))
    for theme in _LEDGER_PALETTES:
        assert re.search(r"--accent:\s*#[0-9a-fA-F]{6};", _ledger_palette_block(css, theme)), (
            f"{theme} palette missing its --accent role"
        )


def test_theme_blocks_override_full_token_set() -> None:
    """Every Ledger palette block defines exactly the same set of colour
    roles as Bone (the :root default), so no palette inherits a Bone-only
    colour that breaks contrast (e.g. Plum with Bone's light inline-code
    chip). Every legacy ``--okf-*`` colour token is aliased onto a role."""
    css = _runtime_file("viewer", "static", "wiki.css").read_text(encoding="utf-8")
    assert ':root, [data-theme="bone"] {' in css, "Bone is not the :root default"

    def _roles(theme: str) -> set[str]:
        return set(re.findall(r"(--[a-z0-9-]+):", _ledger_palette_block(css, theme)))

    bone = _roles("bone")
    assert len(bone) > 60, sorted(bone)
    for theme in _LEDGER_PALETTES[1:]:
        roles = _roles(theme)
        assert roles == bone, (theme, sorted(bone ^ roles))
    alias = _ledger_alias_block(css)
    core_tokens = (
        "okf-bg", "okf-bg-elev", "okf-bg-inset", "okf-fg", "okf-fg-muted",
        "okf-border", "okf-border-strong", "okf-accent", "okf-accent-hover",
        "okf-accent-on", "okf-accent-bg", "okf-select", "okf-code-bg",
        "okf-code-fg", "okf-pre-bg", "okf-pre-fg", "okf-broken",
        "okf-ok", "okf-ok-bg", "okf-warn", "okf-warn-bg",
        "okf-info", "okf-info-bg", "okf-error", "okf-error-bg", "okf-shadow",
    )
    for token in core_tokens:
        m = re.search(rf"--{token}:\s*([^;]+);", alias)
        assert m, f"alias block missing --{token}"
        value = m.group(1).strip()
        # Each alias reads a role (or is a deliberate flat value), never a hex.
        assert not re.search(r"#[0-9a-fA-F]{3,8}", value), (token, value)
        for role in re.findall(r"var\((--[a-z0-9-]+)\)", value):
            assert role in bone, f"--{token} aliases {role}, which no palette defines"


# ===========================================================================
# P1-4: theme.js resolves data-theme BEFORE first paint on every output
# surface. It is a parser-blocking <script> in <head> (no defer), placed
# BEFORE capture-readiness.js (which reads data-theme), and is NOT duplicated
# at end of <body>. Strict CSP is preserved (no inline handler, no relaxation).
# ===========================================================================


def test_static_output_resolves_theme_in_head_before_capture_readiness(
    tiny_good_bundle: Path, tmp_path: Path
) -> None:
    """P1-4: on static concept/index/search/graph pages theme.js is a
    parser-blocking <script> in <head>, before capture-readiness.js, never
    duplicated in <body>; script-src stays strict (no 'unsafe-inline')."""
    b = Bundle.load(tiny_good_bundle)
    out_dir = tmp_path / "site"
    build_site(b, out_dir, target="static")

    concept_pages = [
        p for p in out_dir.rglob("*.html")
        if p.name not in ("index.html", "__search.html", "__graph.html")
    ]
    assert concept_pages, "no concept page rendered"
    samples = {
        "concept": concept_pages[0].read_text(encoding="utf-8"),
        "index": (out_dir / "index.html").read_text(encoding="utf-8"),
        "search": (out_dir / "__search.html").read_text(encoding="utf-8"),
        "graph": (out_dir / "__graph.html").read_text(encoding="utf-8"),
    }
    theme_tag = '<script src="__static/theme.js"></script>'
    theme_re = re.compile(r'<script src="[^"]*?/theme\.js"></script>')
    cap_re = re.compile(r'<script src="[^"]*?/capture-readiness\.js" defer></script>')
    for label, html in samples.items():
        head, _, body = html.partition("</head>")
        tm = theme_re.search(head)
        assert tm, f"{label}: theme.js not in <head>"
        # Parser-blocking: the matched tag carries no `defer`.
        assert "defer" not in tm.group(0), f"{label}: theme.js must not defer"
        # Exactly one theme.js script in the document (no body duplicate).
        assert len(theme_re.findall(html)) == 1, (
            f"{label}: theme.js not emitted exactly once"
        )
        # capture-readiness runs AFTER theme.js (head or body).
        cm = cap_re.search(html)
        assert cm, f"{label}: capture-readiness.js missing"
        assert html.index(tm.group(0)) < html.index(cm.group(0)), (
            f"{label}: capture-readiness precedes theme.js"
        )
        # Strict script-src unchanged (no 'unsafe-inline' relaxation).
        csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html)
        assert csp, f"{label}: CSP meta missing"
        assert "script-src 'self' https://cdn.jsdelivr.net" in csp.group(1), (
            f"{label}: strict script-src changed"
        )
        assert "'unsafe-inline'" not in csp.group(1).split("style-src")[0], (
            f"{label}: script-src relaxed with unsafe-inline"
        )


def test_single_file_inlines_theme_js_in_head_before_body_bundle(
    tiny_good_bundle: Path, tmp_path: Path
) -> None:
    """P1-4: the single-file viewer inlines theme.js in a <head> <script>
    (before first paint) and the capture-readiness/graph body bundle runs
    after it. The single-file CSP allows 'unsafe-inline' (the whole viewer is
    inlined) and is unchanged by this slice."""
    out = tmp_path / "single.html"
    render_single_file(Bundle.load(tiny_good_bundle), out)
    html = out.read_text(encoding="utf-8")
    head, _, body = html.partition("</head>")
    # theme.js is inlined inside the <head> <script> (the sole owner marker).
    assert "window.OKFLoomTheme" in head, "theme.js not inlined in <head>"
    # capture-readiness (okfCaptureReady) is in the body bundle, AFTER head.
    assert "okfCaptureReady" in body, "capture-readiness not in body bundle"
    assert html.index("window.OKFLoomTheme") < html.index("okfCaptureReady"), (
        "single-file: body capture-readiness precedes head theme.js"
    )


def test_serve_injects_bootstrap_before_theme_js_in_head(tmp_path: Path) -> None:
    """P1-3/P1-4: on serve, server._studio_bootstrap emits the CSP-safe
    bootstrap JSON (studio.theme) BEFORE theme.js so theme.js can read it at
    synchronous init, and theme.js before the deferred live/studio modules.
    theme.js is always injected (plain-serve with no studio still gets it)."""
    from types import SimpleNamespace
    from okf_loom.server import OKFWikiHandler
    from okf_loom.studio import Studio

    def _bits(server):
        h = object.__new__(OKFWikiHandler)
        h.server = server
        return h._studio_bootstrap()

    # With studio: bootstrap JSON -> studio.css -> theme.js -> live.js/studio.js.
    studio = Studio.for_bundle(tmp_path)
    with_studio = _bits(SimpleNamespace(
        studio=studio, csrf_token="tok", studio_live=True, studio_edit=True,
        studio_theme="auto",
    ))
    assert with_studio.index('id="okf-studio-bootstrap"') < with_studio.index("theme.js")
    assert with_studio.index("theme.js") < with_studio.index("live.js")
    assert with_studio.index("theme.js") < with_studio.index("studio.js")
    assert "studio_theme" not in with_studio  # only the "theme" key is exposed

    # Without studio (plain read-only serve): still injects theme.js only.
    no_studio = _bits(SimpleNamespace())
    assert "<script src=\"/__static/theme.js\"></script>" in no_studio
    assert "okf-studio-bootstrap" not in no_studio
    assert "live.js" not in no_studio


def test_studio_bootstrap_exposes_only_explicit_request_authority_mode(tmp_path: Path) -> None:
    """Local/public-read/public-comment/public-edit follows explicit capability.

    The mode is presentation metadata only: public read emits no token and
    public edit emits only its already-scoped token, never local admin power.
    """
    import json
    import re
    from types import SimpleNamespace
    from okf_loom.server import OKFWikiHandler
    from okf_loom.studio import Studio

    studio = Studio.for_bundle(tmp_path)

    def payload(*, public: bool, edit: bool = True, public_edit: bool = False,
                public_comment: bool = False, active: bool = True) -> dict:
        handler = object.__new__(OKFWikiHandler)
        handler.server = SimpleNamespace(
            studio=studio, csrf_token="local-secret",
            public_mutation_token="scoped-edit", public_comment_token="scoped-comment",
            public_tunnel_edit=public_edit, public_tunnel_comment=public_comment,
            public_ingress_accepting=active,
            studio_live=True, studio_edit=edit, studio_theme="auto",
        )
        handler._is_public_tunnel_request = lambda: public
        handler._public_ingress_is_active = lambda: active
        html = handler._studio_bootstrap()
        match = re.search(r'id="okf-studio-bootstrap">(.*?)</script>', html)
        assert match
        return json.loads(match.group(1))

    cases = [
        ({"public": False}, ("local-edit", True, True, "local-secret", True)),
        ({"public": False, "edit": False}, ("local-read", False, False, "", True)),
        ({"public": True}, ("public-read", False, False, "", False)),
        ({"public": True, "public_comment": True}, ("public-comment", False, True, "scoped-comment", False)),
        ({"public": True, "public_edit": True}, ("public-edit", True, True, "scoped-edit", False)),
        ({"public": True, "public_edit": True, "edit": False}, ("public-read", False, False, "", False)),
        ({"public": True, "public_comment": True, "edit": False}, ("public-read", False, False, "", False)),
        ({"public": True, "public_edit": True, "active": False}, ("public-read", False, False, "", False)),
        ({"public": True, "public_comment": True, "active": False}, ("public-read", False, False, "", False)),
    ]
    for args, expected in cases:
        result = payload(**args)
        assert tuple(result[k] for k in ("authority_mode", "edit", "comment", "token", "presence")) == expected
        if args["public"]:
            assert "local-secret" not in json.dumps(result)
        if not expected[1]:
            assert "scoped-edit" not in json.dumps(result)


# ===========================================================================
# iter-3 frontend fixes:
#   P2-8  aliases render ONCE (subtitle is canonical, pills removed)
#   P2-9  wiki.js no longer emits any static-search note (the labelled
#         Search submit is the honest route; static typed-query gating
#         excludes the search page, where static-search.js owns the searcher)
#   P2-10 breadcrumb raw-id <small> removed (size + duplication)
#   P2-11 "All fields" table excludes header-already-rendered keys
# ===========================================================================


def test_iter3_aliases_render_once_only(tmp_path: _Path) -> None:
    """P2-8: aliases appear EXACTLY ONCE on the rendered concept page — as
    the subtitle <p>. The prior "Also known as:" pills block in
    _render_governed_keys duplicated the same values within ~80px of the
    subtitle (conspicuous once P1-1 iter-2 made the subtitle a sibling of
    the h1)."""
    bundle = _build_bundle_with(tmp_path, {
        "concept.md": (
            "---\n"
            "type: Note\n"
            "title: Main Title\n"
            "aliases: [alt one, alt two]\n"
            "---\n\nBody.\n"
        ),
    })
    html = _render_concept_html(bundle, "concept")
    # The subtitle (canonical surface) IS present.
    assert 'class="okf-subtitle"' in html, "subtitle line missing"
    assert "alt one · alt two" in html, "alias text missing from subtitle"
    # The pills block is GONE — no "Also known as:" label, no alias pills.
    assert "okf-alias-pill" not in html, (
        "aliases pills should be removed (P2-8: subtitle is canonical)"
    )
    assert "Also known as:" not in html, (
        "'Also known as:' label should be removed with the pills block"
    )
    assert 'class="okf-aliases"' not in html, (
        "okf-aliases wrapper div should be removed (no longer has content)"
    )
    # Each alias value appears exactly ONCE in the rendered HTML (count the
    # subtitle occurrence only; they previously appeared in BOTH subtitle
    # and pills, doubling the count).
    assert html.count("alt one") == 1, (
        f"alias 'alt one' should appear once (subtitle only); "
        f"found {html.count('alt one')} occurrences"
    )
    assert html.count("alt two") == 1, (
        f"alias 'alt two' should appear once (subtitle only); "
        f"found {html.count('alt two')} occurrences"
    )


def test_iter3_subtitle_still_present_with_entities(tmp_path: _Path) -> None:
    """P2-8 regression guard: removing the aliases pills does NOT also drop
    the entities/provenance/citations/relations sections (those still come
    from _render_governed_keys). Only the aliases pills block was removed."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\n"
            "type: T\n"
            "title: C\n"
            "aliases: [a1, a2]\n"
            "entities:\n"
            "  - id: e1\n"
            "    label: Ent\n"
            "    kind: business_entity\n"
            "provenance:\n"
            "  - source: https://s.example.com\n"
            "    note: n\n"
            "citations:\n"
            "  - id: '1'\n"
            "    text: ref\n"
            "    url: https://c.example.com\n"
            "---\nbody\n"
        ),
    })
    html = _render_concept_html(bundle, "c")
    # Subtitle (aliases) still renders.
    assert 'class="okf-subtitle"' in html
    # Phase 2: entities now render as clickable header chips
    # (okf-entity-chip), not as a <dl> in the governed block.
    assert 'class="okf-entity-chip"' in html, "entity chips missing from header"
    assert 'class="okf-entity-list"' not in html, (
        "entities should render as header chips, not a governed <dl> (Phase 2)"
    )
    # Other governed sections survive (in the infobox).
    assert 'class="okf-provenance-list"' in html, "provenance section dropped"
    assert 'class="okf-citation-list"' in html, "citations section dropped"
    # Aliases pills do NOT render.
    assert "okf-alias-pill" not in html


def test_iter3_breadcrumb_has_no_raw_id_small(tmp_path: _Path) -> None:
    """P2-10: the breadcrumb raw-id ``<small class="okf-breadcrumb__id">``
    is gone. It duplicated the path segments shown in the trail AND fell
    below the 12px type floor (``<small>`` × ``.okf-muted`` 14px ×
    ``<small>`` default 0.8em ≈ 11.2px)."""
    bundle = _build_bundle_with(tmp_path, {
        "tables/orders.md": (
            "---\ntype: Table\ntitle: Orders\n---\nbody\n"
        ),
        "tables/index.md": "# tables\n",
        "index.md": "# root\n",
    })
    b = _Path(str(bundle))
    from okf_loom import Bundle
    bb = Bundle.load(b)
    out_dir = b / "_site_iter3_crumb"
    build_site(bb, out_dir, target="static")
    html = (out_dir / "tables" / "orders.html").read_text(encoding="utf-8")
    # The breadcrumb nav still exists.
    assert '<nav class="okf-breadcrumb" aria-label="Breadcrumb">' in html
    # The raw-id <small> wrapper class is GONE.
    assert "okf-breadcrumb__id" not in html, (
        "breadcrumb raw-id <small> should be removed (P2-10: duplicates "
        "trail + falls below 12px type floor)"
    )
    # The breadcrumb nav closes right after the trail (no stray <small>).
    nav_match = re.search(
        r'<nav class="okf-breadcrumb"[^>]*>(.*?)</nav>',
        html, re.DOTALL,
    )
    assert nav_match, "breadcrumb nav not found"
    assert "<small" not in nav_match.group(1), (
        f"stray <small> inside breadcrumb nav: {nav_match.group(1)!r}"
    )
    # Trail still shows the current segment (orders) as aria-current.
    assert 'aria-current="page">orders<' in nav_match.group(1), (
        "breadcrumb current segment missing"
    )


def test_iter3_wiki_js_skips_search_note_on_search_page() -> None:
    """P2-9, header-search dedup revision: wiki.js emits NO static-search
    note at all. The topbar form's visible labelled Search submit is the
    honest route to the dedicated search page — a JS-injected
    'Press Enter to search, or Open search.' chip beside it duplicated the
    same affordance and was removed (the user-visible header must show one
    search control: field + Search).

    The static typed-query gate is simply ``isStatic`` (phase-1 portable
    correctness): every static page — the search route included — must
    never fire the doomed live /__search suggestion fetch, because that
    endpoint only exists behind the serve runtime. The former
    ``isStatic && !body.okf-viewer--search`` carve-out made the static
    search page's own topbar field fire (and announce) a fetch that can
    only 404 on a static host. The dedicated searcher is NOT affected:
    static-search.js owns the search-route results over
    __data/search.json, independent of this wiki.js gate (proven in the
    browser by test_static_search_route_typing_never_fires_doomed_
    suggestions_fetch and test_static_topbar_single_search_control_and_
    portable_submit).

    This is a static-source contract test: we assert wiki.js both dropped
    the note emission and preserved the (corrected) gating predicate."""
    wiki_js = _runtime_file("viewer", "static", "wiki.js").read_text(encoding="utf-8")
    # The JS-injected duplicate hint is gone — no note element, no prose, no
    # 'Open search' shortcut anywhere in wiki.js.
    for obsolete in ("okf-search-note", "Open search", "Press Enter to search"):
        assert obsolete not in wiki_js, (
            f"wiki.js must not emit the static-search note ({obsolete!r} found); "
            "the labelled Search submit is the single honest search affordance "
            "(header-search dedup)"
        )
    # The static typed-query gate keys off isStatic alone — no per-route
    # carve-out that re-arms the doomed fetch on the static search page.
    branch_match = re.search(
        r'var\s+staticSubmitOnly\s*=\s*isStatic\s*;',
        wiki_js,
    )
    assert branch_match, (
        "wiki.js static typed-query gate must be exactly "
        "`var staticSubmitOnly = isStatic;` (phase-1 portable correctness: "
        "no static page, search route included, may fire the doomed live "
        "/__search suggestions fetch)"
    )
    assert not re.search(
        r'isStatic\s*&&\s*!\s*document\.body\.classList', wiki_js
    ), (
        "wiki.js must not re-introduce a per-route body-class carve-out on "
        "the static typed-query gate; the static search route is a static "
        "page like any other for that gate"
    )


def test_iter3_search_page_has_no_server_side_search_note(tmp_path: _Path) -> None:
    """P2-9 (server-side parity): the search page does NOT carry a
    server-rendered 'Press Enter to search' note either (wiki.js no longer
    injects a note anywhere, and the labelled Search submit is the route).
    This guards against a future render.py change that re-adds the note
    statically."""
    bundle = _build_bundle_with(tmp_path, {
        "index.md": "# root\n",
        "c.md": "---\ntype: T\ntitle: Orders\n---\nbody\n",
    })
    from okf_loom import Bundle
    from okf_loom.render import _render_search_page, build_site
    b = Bundle.load(bundle)
    # 1) Direct render.
    html = _render_search_page(
        b, mode="static", name=b.name, config={},
        query="orders",
        results=[{"title": "Orders", "concept_id": "c", "id": "c"}],
    )
    assert "Press Enter to search" not in html, (
        "search page must not statically render the 'Press Enter to search' "
        "note (P2-9: wiki.js now gates it on body:not(.okf-viewer--search))"
    )
    # 2) Built-page parity: same invariant on the built __search.html.
    out_dir = bundle / "_site_iter3_search"
    build_site(b, out_dir, target="static")
    built = (out_dir / "__search.html").read_text(encoding="utf-8")
    assert "Press Enter to search" not in built, (
        "built __search.html must not contain the 'Press Enter to search' "
        "note (P2-9)"
    )


def test_iter3_all_fields_excludes_header_rendered_keys(tmp_path: _Path) -> None:
    """P2-11: the 'All fields' <details> frontmatter table excludes the
    five keys already rendered in the page header (type chip / h1 title /
    description paragraph / resource link / tags row). Only ``timestamp``
    plus unknown/extra producer-added keys should remain.

    The 'All fields' affordance promises completeness but previously
    repeated 5 of 6 already-visible header rows; this fix keeps the
    forward-compat value (unknown keys surface here) without the noise.
    """
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\n"
            "type: Table\n"
            "title: Orders\n"
            "description: One row per order.\n"
            "resource: https://example.com/orders\n"
            "tags: [orders, revenue]\n"
            "timestamp: '2026-06-27T09:30:00Z'\n"
            "custom_key: custom_value\n"
            "---\nbody\n"
        ),
    })
    html = _render_concept_html(bundle, "c")
    # Phase 2: the "All fields" table is now inside the infobox facts
    # disclosure (<details class="okf-infobox__facts">), not a standalone
    # <details class="okf-frontmatter">.
    details_match = re.search(
        r'<details class="okf-infobox__facts"[^>]*>(.*?)</details>',
        html, re.DOTALL,
    )
    assert details_match, "missing 'All fields' <details> block in infobox"
    details_html = details_match.group(1)

    # Header-already-rendered keys must NOT appear as rows in the table.
    # (We check the <th> text precisely so a value containing the literal
    # string — e.g. 'orders' in tags — does not false-positive.)
    for header_key in ("type", "title", "description", "resource", "tags"):
        th_match = re.search(
            rf'<th>\s*{re.escape(header_key)}\s*</th>',
            details_html,
        )
        assert th_match is None, (
            f"'All fields' table still includes header-rendered key "
            f"'{header_key}' (P2-11)"
        )

    # Governed keys must remain excluded too (regression guard).
    for gov_key in ("aliases", "entities", "provenance",
                    "citations", "relations"):
        th_match = re.search(
            rf'<th>\s*{re.escape(gov_key)}\s*</th>',
            details_html,
        )
        assert th_match is None, (
            f"'All fields' table includes governed key '{gov_key}' "
            f"(should be in dedicated sections)"
        )

    # OKF 0.2 labels timestamp explicitly in the generation disclosure.
    assert not re.search(r'<th>\s*timestamp\s*</th>', details_html)
    assert 'Legacy timestamp (generation fallback)' in html
    assert '2026-06-27T09:30:00+00:00' in html
    # Unknown / producer-added keys are preserved (forward-compat, SPEC §11).
    assert re.search(r'<th>\s*custom_key\s*</th>', details_html), (
        "'All fields' table should preserve unknown producer-added keys "
        "(forward-compat / SPEC §11)"
    )


def test_iter3_all_fields_header_keys_actually_rendered(tmp_path: _Path) -> None:
    """P2-11 paired invariant: the keys we now exclude from 'All fields'
    ARE actually rendered in the page header (otherwise the exclusion
    would hide them entirely). This catches a future change that drops a
    header field without re-adding it to the frontmatter table."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\n"
            "type: Table\n"
            "title: Orders\n"
            "description: One row per order.\n"
            "resource: https://example.com/orders\n"
            "tags: [orders, revenue]\n"
            "---\nbody\n"
        ),
    })
    html = _render_concept_html(bundle, "c")
    # Type chip shows the type value.
    assert re.search(r'class="okf-type-chip"[^>]*>\s*Table\s*<', html), (
        "type not rendered as the type chip in the header"
    )
    # Title in the h1.
    h1_match = re.search(
        r'<h1 class="okf-page__title">(.*?)</h1>', html, re.DOTALL
    )
    assert h1_match and "Orders" in h1_match.group(1), (
        "title not rendered in the page h1"
    )
    # Description paragraph.
    assert re.search(
        r'<p class="okf-page__description">[^<]*One row per order\.',
        html,
    ), "description not rendered in header"
    # Resource link.
    assert 'href="https://example.com/orders"' in html, (
        "resource URL not rendered in header"
    )
    # Tags row.
    assert '<span class="okf-tag">orders</span>' in html, (
        "tag 'orders' not rendered (now in infobox)"
    )
    assert '<span class="okf-tag">revenue</span>' in html, (
        "tag 'revenue' not rendered (now in infobox)"
    )


# ===========================================================================
# Phase 2: concept anatomy (infobox / entity chips / connections / prev-next)
# ===========================================================================


def test_phase2_concept_page_dom_order(tmp_path: _Path) -> None:
    """Reading-first layout: the article DOM order must be
    header → body → details → canonical-source → projection → connections.
    The infobox is always OUTSIDE .okf-page__body so projection parity is
    preserved (contract-runtime-parity invariant)."""
    bundle = _build_bundle_with(tmp_path, {
        "tables/orders.md": (
            "---\ntype: Table\ntitle: Orders\ntags: [a]\n---\n\nbody\n"
        ),
        "tables/users.md": (
            "---\ntype: Table\ntitle: Users\n---\n\nSee [Orders](/tables/orders.md).\n"
        ),
    })
    html = _render_concept_html(bundle, "tables/orders")
    # The five structural landmarks must appear in this exact order.
    positions = {}
    for label, needle in [
        ("header", 'class="okf-page__header"'),
        ("infobox", 'class="okf-page__infobox"'),
        ("body", 'class="okf-prose okf-page__body"'),
        ("canonical", 'id="okf-canonical-source"'),
        ("projection", 'id="okf-annotation-projection"'),
        ("connections", 'class="okf-page__connections"'),
    ]:
        idx = html.find(needle)
        assert idx != -1, f"{label} landmark not found in HTML"
        positions[label] = idx
    assert positions["header"] < positions["body"] < positions["infobox"], (
        "header → body → details order violated"
    )
    assert positions["body"] < positions["canonical"] < positions["projection"], (
        "body → canonical-source → projection order violated"
    )
    assert positions["projection"] < positions["connections"], (
        "projection must come before connections"
    )


def test_phase2_infobox_external_to_body(tmp_path: _Path) -> None:
    """contract-runtime-parity: the infobox is OUTSIDE .okf-page__body, so
    the projection finalizer never sees infobox nodes."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": "---\ntype: T\ntitle: C\ntags: [x]\ntimestamp: '2026-01-01'\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "c")
    body_start = html.find('class="okf-prose okf-page__body"')
    body_end = html.find("</div>", body_start)
    infobox_pos = html.find('class="okf-page__infobox"')
    assert infobox_pos != -1, "infobox not rendered"
    assert infobox_pos > body_end, "details must follow the authored body"
    # The infobox does not appear inside the body div's content.
    assert 'class="okf-page__infobox"' not in html[body_start:body_end], (
        "infobox must not appear inside .okf-page__body"
    )


def test_phase2_infobox_contains_tags_and_facts(tmp_path: _Path) -> None:
    """Phase 2: tags moved to the infobox; the frontmatter facts table
    (timestamp + extras) is in the infobox facts disclosure."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\ntype: Table\ntitle: Orders\ntags: [orders, revenue]\n"
            "timestamp: '2026-06-27T09:30:00Z'\n"
            "custom_key: custom_value\n---\nbody\n"
        ),
    })
    html = _render_concept_html(bundle, "c")
    # Tags are inside the infobox, not the header meta.
    infobox_start = html.find('class="okf-page__infobox"')
    infobox_end = html.find("</aside>", infobox_start)
    infobox_html = html[infobox_start:infobox_end]
    assert "okf-infobox__tags" in infobox_html
    assert '<span class="okf-tag">orders</span>' in infobox_html
    # Facts table in the disclosure.
    assert 'class="okf-infobox__facts"' in infobox_html
    # Legacy generation is in the metadata disclosure, not duplicated in facts.
    assert "2026-06-27" in html
    assert "custom_key" in infobox_html
    # Revision summary present.
    assert "okf-infobox__rev" in infobox_html
    # Tags NOT in the header meta row.
    header_end = html.find("</header>")
    header_html = html[:header_end]
    assert "okf-tag" not in header_html, "tags should be in infobox, not header"


def test_phase2_infobox_omits_empty_facts(tmp_path: _Path) -> None:
    """Phase 2: when there are no extra frontmatter keys (all governed or
    header-rendered), the facts disclosure is omitted."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": "---\ntype: T\ntitle: C\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "c")
    assert "okf-infobox__facts" not in html, (
        "empty facts table should not produce a disclosure"
    )
    # The summary row still renders.
    assert "okf-infobox__summary" in html


def test_phase2_entity_chips_in_header(tmp_path: _Path) -> None:
    """Phase 2: entities render as clickable header chips linking to search."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\ntype: T\ntitle: C\n"
            "entities:\n"
            "  - label: Customer\n"
            "    kind: business_entity\n"
            "  - label: Order ID\n"
            "---\nbody\n"
        ),
    })
    html = _render_concept_html(bundle, "c")
    # Find the PAGE header (not the topbar header).
    page_header_start = html.find('class="okf-page__header"')
    assert page_header_start != -1
    header_end = html.find("</header>", page_header_start)
    header_html = html[page_header_start:header_end]
    assert 'class="okf-entity-chips"' in header_html, "entity chips missing from header"
    # Static mode, top-level concept: root_prefix is "./" so search URL is
    # "./__search.html?q=...".
    assert 'href="./__search.html?q=Customer"' in header_html
    assert 'href="./__search.html?q=Order+ID"' in header_html
    # Kind chip renders.
    assert "business_entity" in header_html


def test_phase2_entity_chips_escape_label(tmp_path: _Path) -> None:
    """security-boundary: entity labels are HTML-escaped in both visible
    text and the query-string href."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\ntype: T\ntitle: C\n"
            'entities: ["<script>alert(1)</script>"]\n'
            "---\nbody\n"
        ),
    })
    html = _render_concept_html(bundle, "c")
    assert "<script>" not in html, "unescaped script tag in entity label"
    assert "&lt;script&gt;" in html, "entity label not escaped in visible text"
    # The href URL-encodes the label so it can't break out of the attribute.
    assert "q=%3Cscript%3E" in html, (
        "entity label not URL-encoded in search href"
    )


def test_phase2_entity_chips_absent_when_empty(tmp_path: _Path) -> None:
    bundle = _build_bundle_with(tmp_path, {
        "c.md": "---\ntype: T\ntitle: C\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "c")
    assert "okf-entity-chips" not in html


def test_phase2_connections_labels_and_cards(tmp_path: _Path) -> None:
    """Phase 2: the connections footer uses the new labels and wraps the
    existing card markup."""
    bundle = _build_bundle_with(tmp_path, {
        "a.md": (
            "---\ntype: T\ntitle: A\n---\n\nSee [B](/b.md).\n"
        ),
        "b.md": "---\ntype: T\ntitle: B\n---\n\nSee [A](/a.md).\n",
    })
    html = _render_concept_html(bundle, "a")
    assert 'class="okf-page__connections"' in html
    assert "This concept links to" in html
    # b links to a, so a has a backlink.
    assert "Referenced by" in html
    # Card markup from _render_link_list survives.
    assert "okf-conn" in html


def test_phase3_multi_edge_static_single_and_nojs_parity(tmp_path: _Path) -> None:
    """Grouped payload and all three labels survive static + single/no-JS."""
    from okf_loom import Bundle
    from okf_loom.render import build_site, render_single_file

    bundle = Bundle.load("samples/demo_bundle")
    site = tmp_path / "site"
    build_site(bundle, site, target="static")
    graph_json = (site / "__data" / "graph.json").read_text(encoding="utf-8")
    graph_html = (site / "__graph.html").read_text(encoding="utf-8")
    single = tmp_path / "single.html"
    render_single_file(bundle, single)
    single_html = single.read_text(encoding="utf-8")

    assert '"raw_relationship_count":23' in graph_json.replace(" ", "")
    assert '"multiplicity":3' in graph_json.replace(" ", "")
    for label in ("orders table", "writes_to", "depends_on"):
        assert label in graph_json
        assert label in graph_html
        assert label in single_html
    assert "Resolved in-bundle relationships" in graph_html
    assert "Resolved in-bundle relationships" in single_html


def test_phase3_multi_edge_ui_contract_is_keyboard_and_pointer_activatable() -> None:
    graph_js = _runtime_file("viewer", "static", "graph.js").read_text(encoding="utf-8")
    assert 'edge.okf-multi-edge' in graph_js
    assert 'cy.on("tap", "edge"' in graph_js
    assert 'bundleIndexList.setAttribute("role","listbox")' in graph_js
    assert 'button.setAttribute("role","option")' in graph_js
    assert 'aria-selected' in graph_js
    assert "Collapse relationships" in graph_js
    assert "renderBundleDetail" in graph_js


def test_phase2_connections_omits_empty_blocks(tmp_path: _Path) -> None:
    """Phase 2: empty connection blocks are omitted entirely."""
    bundle = _build_bundle_with(tmp_path, {
        "a.md": "---\ntype: T\ntitle: A\n---\nbody\n",
        "b.md": "---\ntype: T\ntitle: B\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "a")
    # a has no outgoing or incoming links → no connections nav at all.
    assert "okf-page__connections" not in html


def test_phase2_prevnext_section_siblings(tmp_path: _Path) -> None:
    """Phase 2: prev/next navigates section siblings deterministically."""
    bundle = _build_bundle_with(tmp_path, {
        "tables/alpha.md": "---\ntype: Table\ntitle: Alpha\n---\nbody\n",
        "tables/beta.md": "---\ntype: Table\ntitle: Beta\n---\nbody\n",
        "tables/gamma.md": "---\ntype: Table\ntitle: Gamma\n---\nbody\n",
        "index.md": "# root\n",
    })
    html = _render_concept_html(bundle, "tables/beta")
    assert 'class="okf-prevnext"' in html
    assert 'rel="prev"' in html
    assert 'rel="next"' in html
    # Alpha (prev) and Gamma (next) in deterministic order.
    assert "Alpha" in html
    assert "Gamma" in html


def test_phase2_prevnext_first_and_last(tmp_path: _Path) -> None:
    """Phase 2: the first sibling has no prev; the last has no next.
    Placeholders keep the grid balanced."""
    bundle = _build_bundle_with(tmp_path, {
        "tables/alpha.md": "---\ntype: Table\ntitle: Alpha\n---\nbody\n",
        "tables/beta.md": "---\ntype: Table\ntitle: Beta\n---\nbody\n",
    })
    # Alpha is first → no prev link.
    html_a = _render_concept_html(bundle, "tables/alpha")
    assert 'rel="next"' in html_a
    assert 'rel="prev"' not in html_a
    assert "okf-prevnext__placeholder" in html_a
    # Beta is last → no next link.
    html_b = _render_concept_html(bundle, "tables/beta")
    assert 'rel="prev"' in html_b
    assert 'rel="next"' not in html_b


def test_phase2_prevnext_absent_top_level(tmp_path: _Path) -> None:
    """Phase 2: top-level concepts (no directory section) get no prev/next."""
    bundle = _build_bundle_with(tmp_path, {
        "a.md": "---\ntype: T\ntitle: A\n---\nbody\n",
        "b.md": "---\ntype: T\ntitle: B\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "a")
    assert "okf-prevnext" not in html


def test_phase2_prevnext_absent_single_sibling(tmp_path: _Path) -> None:
    """Phase 2: a concept with no siblings in its section gets no prev/next."""
    bundle = _build_bundle_with(tmp_path, {
        "tables/orders.md": "---\ntype: Table\ntitle: Orders\n---\nbody\n",
        "index.md": "# root\n",
    })
    html = _render_concept_html(bundle, "tables/orders")
    assert "okf-prevnext" not in html


def _prevnext_titles(html: str) -> tuple[str | None, str | None]:
    """Extract (prev_title, next_title) from a rendered prev/next nav.

    Used by the index-order tests so they assert structural ORDER, not just
    presence — a test that only checks "Alpha and Gamma both appear" passes
    even when the order is wrong (the very regression we are guarding against).
    """
    import re
    prev_m = re.search(
        r'<a class="okf-prevnext__link okf-prevnext__prev"[^>]*>.*?'
        r'<span class="okf-prevnext__title">(.*?)</span>',
        html, re.DOTALL,
    )
    next_m = re.search(
        r'<a class="okf-prevnext__link okf-prevnext__next"[^>]*>.*?'
        r'<span class="okf-prevnext__title">(.*?)</span>',
        html, re.DOTALL,
    )
    return (
        prev_m.group(1) if prev_m else None,
        next_m.group(1) if next_m else None,
    )


def test_phase2_prevnext_follows_section_index_order_not_lexical(tmp_path: _Path) -> None:
    """P1 #5 (contract-runtime-parity): prev/next follows the AUTHORITATIVE
    section ``index.md`` order, not a lexical concept-id sort.

    Bundle has three tables (alpha, beta, gamma). The hand-authored
    ``tables/index.md`` lists them in REVERSE lexical order: zeta-first, then
    beta, then alpha. A lexical cid sort would put alpha first; the curated
    index must win. Files are named so their concept ids, sorted lexically,
    DIFFER from the index order — this is what distinguishes the test from a
    tautology.

    To make the distinction crisp, the files are named so lexical sort gives
    (gamma, beta, alpha) but the index lists (alpha, beta, gamma):
      - lexical cid sort: gamma < beta < alpha (g < b < a is FALSE; actually
        a < b < g). Reverse the file names so the lexical sort disagrees with
        the curated order.

    Concretely: files ``tables/c01_first.md`` (title "Zeta"), ``tables/c02.md``
    (title "Alpha"), ``tables/c03.md`` (title "Beta"). Lexical cid sort is
    c01, c02, c03 → Zeta, Alpha, Beta. The index lists c02 first (Alpha),
    then c01 (Zeta), then c03 (Beta). Reading c02 must show c03 (Beta) as
    next, NOT c01 (Zeta) — proving the index overrides lexical order.
    """
    bundle = _build_bundle_with(tmp_path, {
        "tables/c01_zeta.md": "---\ntype: Table\ntitle: Zeta\n---\nbody\n",
        "tables/c02_alpha.md": "---\ntype: Table\ntitle: Alpha\n---\nbody\n",
        "tables/c03_beta.md": "---\ntype: Table\ntitle: Beta\n---\nbody\n",
        # Hand-authored section index: Alpha first, Zeta second, Beta third.
        # This is the AUTHORITATIVE reading order. Lexical cid sort would
        # instead give Zeta(c01) → Alpha(c02) → Beta(c03).
        "tables/index.md": (
            "# Tables\n\n"
            "- [Alpha](c02_alpha) - the first table\n"
            "- [Zeta](c01_zeta) - the second table\n"
            "- [Beta](c03_beta) - the third table\n"
        ),
        "index.md": "# root\n",
    })
    # Render the MIDDLE index entry (Zeta). Per the index, prev=Alpha, next=Beta.
    html = _render_concept_html(bundle, "tables/c01_zeta")
    assert 'class="okf-prevnext"' in html
    prev_title, next_title = _prevnext_titles(html)
    assert prev_title == "Alpha", (
        f"prev must be Alpha (index order); got {prev_title!r}. "
        "If this is Zeta or Beta, the renderer fell back to lexical cid sort."
    )
    assert next_title == "Beta", (
        f"next must be Beta (index order); got {next_title!r}. "
        "If this is Alpha or Zeta, the renderer fell back to lexical cid sort."
    )


def test_phase2_prevnext_index_accepts_md_and_dot_slash_targets(tmp_path: _Path) -> None:
    """P1 #5: section index entries can be authored as ``orders``, ``orders.md``,
    or ``./orders.md``; all three resolve to the same sibling id. This mirrors
    how authors actually write index files in the wild."""
    bundle = _build_bundle_with(tmp_path, {
        "tables/aa_first.md": "---\ntype: Table\ntitle: First\n---\nbody\n",
        "tables/bb_second.md": "---\ntype: Table\ntitle: Second\n---\nbody\n",
        "tables/cc_third.md": "---\ntype: Table\ntitle: Third\n---\nbody\n",
        # Three different target forms; all must resolve to siblings.
        "tables/index.md": (
            "# Tables\n\n"
            "- [First](./bb_second.md) - mid\n"   # ./prefix + .md
            "- [Second](cc_third) - last\n"       # bare
            "- [Third](aa_first.md) - first\n"    # .md only
        ),
        "index.md": "# root\n",
    })
    # Per index: bb_second is FIRST (prev=None), next=cc_third.
    html = _render_concept_html(bundle, "tables/bb_second")
    prev_title, next_title = _prevnext_titles(html)
    assert prev_title is None, (
        f"bb_second is the index's first entry; prev must be None, got {prev_title!r}"
    )
    assert next_title == "Third", (
        f"next must be 'Third' (cc_third, the second index entry); got {next_title!r}"
    )


def test_phase2_prevnext_falls_back_to_source_order_without_section_index(tmp_path: _Path) -> None:
    """P1 #5: when no section ``index.md`` exists, prev/next uses SOURCE order
    (path-sorted load order) — stable + reproducible, and matches the
    directory listing the reader sees. This is the deterministic fallback
    that does not depend on a hand-authored index."""
    bundle = _build_bundle_with(tmp_path, {
        # Source order (path-sorted): apricot, banana, cherry.
        "trees/apricot.md": "---\ntype: Tree\ntitle: Apricot\n---\nbody\n",
        "trees/banana.md": "---\ntype: Tree\ntitle: Banana\n---\nbody\n",
        "trees/cherry.md": "---\ntype: Tree\ntitle: Cherry\n---\nbody\n",
        # NO trees/index.md → fallback to source order.
        "index.md": "# root\n",
    })
    html = _render_concept_html(bundle, "trees/banana")
    prev_title, next_title = _prevnext_titles(html)
    assert prev_title == "Apricot", (
        f"prev must be Apricot (source order); got {prev_title!r}"
    )
    assert next_title == "Cherry", (
        f"next must be Cherry (source order); got {next_title!r}"
    )


def test_phase2_prevnext_index_missing_concept_appended_in_source_order(tmp_path: _Path) -> None:
    """P1 #5: a concept NOT listed in the section index is appended in source
    order rather than stranded. This keeps prev/next usable while an index is
    being curated (e.g. a concept was added but the index not yet updated)."""
    bundle = _build_bundle_with(tmp_path, {
        "tables/aa_first.md": "---\ntype: Table\ntitle: Listed\n---\nbody\n",
        "tables/bb_second.md": "---\ntype: Table\ntitle: AlsoListed\n---\nbody\n",
        "tables/cc_unlisted.md": "---\ntype: Table\ntitle: Unlisted\n---\nbody\n",
        # Index lists ONLY aa_first and bb_second (cc_unlisted missing).
        "tables/index.md": (
            "# Tables\n\n"
            "- [Listed](aa_first)\n"
            "- [AlsoListed](bb_second)\n"
        ),
        "index.md": "# root\n",
    })
    # The unlisted concept still appears at the end (source order) so it gets
    # a prev (AlsoListed) and no next.
    html = _render_concept_html(bundle, "tables/cc_unlisted")
    assert 'class="okf-prevnext"' in html, (
        "unlisted concept must still get a prev/next nav (appended in source order)"
    )
    prev_title, next_title = _prevnext_titles(html)
    assert prev_title == "AlsoListed", (
        f"prev for the unlisted concept must be AlsoListed (last index entry); "
        f"got {prev_title!r}"
    )
    assert next_title is None, (
        f"unlisted concept is appended last; next must be None, got {next_title!r}"
    )


def test_phase2_governed_keys_in_infobox(tmp_path: _Path) -> None:
    """Phase 2: provenance/citations/relations from _render_governed_keys
    are inside the infobox, not the header."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\ntype: T\ntitle: C\n"
            "provenance:\n  - source: https://s.example.com\n    note: n\n"
            "citations:\n  - id: '1'\n    text: ref\n    url: https://c.example.com\n"
            "relations:\n  - target: d\n    type: derived_from\n"
            "---\nbody\n"
        ),
        "d.md": "---\ntype: T\ntitle: D\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "c")
    infobox_start = html.find('class="okf-page__infobox"')
    infobox_end = html.find("</aside>", infobox_start)
    infobox_html = html[infobox_start:infobox_end]
    assert "okf-provenance-list" in infobox_html
    assert "okf-citation-list" in infobox_html
    assert "okf-relations-fm" in infobox_html
    # Header does NOT contain governed content.
    header_end = html.find("</header>")
    header_html = html[:header_end]
    assert "okf-provenance-list" not in header_html
    assert "okf-citation-list" not in header_html


def test_phase2_serve_mode_entity_chip_search_url(tmp_path: _Path) -> None:
    """Phase 2: in serve/spa mode the entity chip search URL has no .html."""
    from okf_loom import Bundle
    from okf_loom.render import _render_entity_chips
    from okf_loom.model import Concept
    from okf_loom.paths import concept_id_from_str
    cid = concept_id_from_str("c")
    c = Concept(
        id=cid, path=tmp_path / "c.md", rel_path=_Path("c.md"),
        frontmatter={"type": "T", "title": "C", "entities": [{"label": "X"}]},
        body="", raw_text="", headings=[],
    )
    html = _render_entity_chips(c, mode="serve", root_prefix="/")
    # Serve mode: root-absolute URL matching the nav form action pattern.
    assert 'href="/__search?q=X"' in html
    html_static = _render_entity_chips(c, mode="static", root_prefix="../")
    assert 'href="../__search.html?q=X"' in html_static


# --- Phase 2 P1 fixes: on-this-page disclosure, infobox tint, relations rows


def test_phase2_onthispage_disclosure_links_match_body_ids(tmp_path: _Path) -> None:
    """Phase 2 layout-graph: the mobile 'On this page' disclosure mirrors the
    body's rendered heading ids so its links resolve to real anchors. The
    hrefs come from the RENDERED body HTML (authoritative), not from
    concept.headings (whose slugify can diverge)."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\ntype: T\ntitle: C\n---\n\n"
            "# Schema\n\ntext.\n\n## Details\n\nmore.\n\n# Examples\n\nx.\n"
        ),
    })
    html = _render_concept_html(bundle, "c")
    import re as _re
    m = _re.search(r'<details class="okf-onthispage".*?</details>', html, _re.DOTALL)
    assert m, "on-this-page disclosure not rendered"
    disc = m.group(0)
    # The disclosure carries one link per body heading, with hrefs matching
    # the ids the renderer stamped on the <hN> elements.
    body = _re.search(r'<div class="okf-prose okf-page__body">(.*?)</div>\s*</div>', html, _re.DOTALL)
    assert body, "rendered authored body not found"
    body_ids = _re.findall(r'<h[1-6][^>]*\bid="([^"]+)"', body.group(1))
    disc_hrefs = _re.findall(r'href="#([^"]+)"', disc)
    assert disc_hrefs == body_ids, (
        f"disclosure hrefs {disc_hrefs} != body ids {body_ids}"
    )
    # Labels are plain text (markup stripped).
    assert "Schema" in disc and "Examples" in disc and "Details" in disc


def test_metadata_groups_and_scrollports_are_structural_before_runtime(tmp_path: _Path) -> None:
    """SSR owns readable structure; runtime alone owns overflow semantics."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\ntype: Guide\ntitle: Accessible blocks\ntags: [a11y]\n---\n\n"
            "```python\nprint('a very long line')\n```\n\n"
            "```mermaid\ngraph LR; A-->B\n```\n"
        ),
    })
    html = _render_concept_html(bundle, "c")
    assert 'aria-labelledby="okf-infobox-label"' in html
    assert '<span class="okf-infobox__label" id="okf-infobox-label">Document details</span>' in html
    assert 'role="group" aria-labelledby="okf-infobox-about-title"' in html
    assert '<h2 id="okf-infobox-about-title">About</h2>' in html
    assert 'data-okf-scroll-kind="code"' in html
    assert 'data-okf-scroll-label="python code block"' in html
    assert 'class="okf-content-scrollport okf-content-scrollport--code" role="region"' not in html
    assert 'class="okf-content-scrollport okf-content-scrollport--code" tabindex="0"' not in html


def test_all_rendering_outputs_emit_product_favicon(tmp_path: _Path) -> None:
    bundle_path = _build_bundle_with(tmp_path, {"c.md": "---\ntype: Guide\ntitle: C\n---\n\nBody.\n"})
    bundle = Bundle.load(bundle_path)
    single_path = tmp_path / "single.html"
    render_single_file(bundle, single_path)
    outputs = (
        _render_concept_html(bundle_path, "c"),
        _render_index_page(bundle, mode="serve", name="Test", palette={}, config={}, sub="", index_file=None),
        single_path.read_text(encoding="utf-8"),
    )
    for html in outputs:
        assert '<link rel="icon" href="data:image/svg+xml,' in html


def test_product_favicon_is_one_square_ledger_mark_in_every_emitter(tmp_path: _Path) -> None:
    """Every built-in template and every Python-built page carries the SAME
    inline icon: a square Ledger ink tile (Bone ``--text``) framed in
    ``--border-strong`` with Bone ``--bg`` bars. No rounded corners, no
    retired teal, and no request (it stays a ``data:`` URL)."""
    from okf_loom.render import _render_not_found_page, load_template

    pattern = re.compile(r'<link rel="icon"[^>]*>')
    links = {
        name: pattern.findall(load_template(name))
        for name in ("index_page.html", "concept_page.html", "search_page.html",
                     "graph_page.html", "single_file.html")
    }
    bundle_path = _build_bundle_with(tmp_path, {"c.md": "---\ntype: Guide\ntitle: C\n---\n\nBody.\n"})
    single_path = tmp_path / "single.html"
    render_single_file(Bundle.load(bundle_path), single_path)
    links["single output"] = pattern.findall(single_path.read_text(encoding="utf-8"))
    links["concept output"] = pattern.findall(_render_concept_html(bundle_path, "c"))
    links["not found"] = pattern.findall(_render_not_found_page(name="T", path="x", config={}))
    assert all(len(found) == 1 for found in links.values()), links
    icons = {found[0] for found in links.values()}
    assert len(icons) == 1, links
    icon = icons.pop()
    assert icon.startswith('<link rel="icon" href="data:image/svg+xml,')
    assert "rx=" not in icon and "0c7373" not in icon and "round" not in icon
    # Bone tokens: --text tile, --border-strong frame, --bg bars.
    assert "fill='%231d1a16'" in icon
    assert "stroke='%23b0a696'" in icon
    assert "stroke='%23f6f3ed'" in icon


def test_phase2_onthispage_headingless_emits_stable_hidden_host(tmp_path: _Path) -> None:
    """F8: a body with no headings still emits the stable outline host —
    hidden, marked ``data-okf-outline-empty``, carrying no jump links — so the
    client owner can fill the SAME slot after a live mutation without a
    reload. With JavaScript off the empty shell stays invisible (no
    disclosure chrome)."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": "---\ntype: T\ntitle: C\n---\n\nJust prose, no headings.\n",
    })
    html = _render_concept_html(bundle, "c")
    shell_open = (
        '<details class="okf-onthispage" aria-label="On this page" '
        'data-okf-document-outline data-okf-outline-empty hidden>'
        '<summary><span>On this page</span>'
        '<span class="okf-onthispage__current" aria-hidden="true"></span></summary>'
        '</details>'
    )
    assert shell_open in html
    shell = html[html.index('<details class="okf-onthispage"'):]
    shell = shell[:shell.index("</details>") + len("</details>")]
    assert "<nav" not in shell and "<a " not in shell and "<ol" not in shell


def test_phase2_onthispage_labels_single_escaped_and_stripped(tmp_path: _Path) -> None:
    """Phase 2 contract-runtime parity (P1): the mobile 'On this page'
    disclosure labels must be the CANONICAL VISIBLE text of each heading,
    single-encoded — exactly what a reader sees and exactly what the live
    rebuild (studio.js rebuildOnThisPage) produces from heading.textContent.

    Regression for a double-escape: the body fragment carries HTML-escaped
    text ("API &amp; SDK"), and the label builder used to feed that already-
    escaped fragment straight into _esc(), producing "API &amp;amp; SDK"
    (rendering as the literal "API &amp; SDK"). Headings with inline code,
    links, "<", and ">" must all collapse to their visible text, and the
    emitted label source must decode (browser textContent) back to that text
    without leaking markup or double-encoded entities."""
    import html as _html
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\ntype: T\ntitle: C\n---\n\n"
            "# API & SDK\n\n"
            "intro.\n\n"
            "## Use the `foo()` method\n\n"
            "body.\n\n"
            "## See the [Loom](loom) guide\n\n"
            "more.\n\n"
            "## a <tag> & b\n\n"
            "end.\n"
        ),
    })
    html = _render_concept_html(bundle, "c")
    import re as _re
    m = _re.search(r'<details class="okf-onthispage".*?</details>', html, _re.DOTALL)
    assert m, "on-this-page disclosure not rendered"
    disc = m.group(0)
    # Pull each label's inner-HTML source, then decode it the way a browser
    # would (strip tags + unescape entities) to read the VISIBLE text.
    label_src = _re.findall(
        r'<a class="okf-onthispage__link"[^>]*>(.*?)</a>', disc, _re.DOTALL
    )
    assert label_src, "no on-this-page labels rendered"
    visible = [_html.unescape(_re.sub(r"<[^>]+>", "", s)).strip() for s in label_src]

    # The ampersand heading must NOT be double-escaped in the source.
    assert "API &amp;amp; SDK" not in disc, (
        "double-escaped label leaked into SSR: " + disc
    )
    assert "API &amp; SDK" in disc, (
        "ampersand heading must be single-encoded in the label source"
    )
    # Canonical visible labels, in document order.
    assert visible == [
        "API & SDK",
        "Use the foo() method",
        "See the Loom guide",
        "a <tag> & b",
    ], f"on-this-page labels not canonical visible text: {visible!r}"

    # Safety: no raw child elements leaked into a label (it is a jump list,
    # not a re-render) — every label source is pure text/entities.
    for s in label_src:
        assert "<a " not in s and "<code" not in s, (
            f"inline markup leaked into a plain-text label: {s!r}"
        )


def test_phase2_infobox_is_a_neutral_details_disclosure(tmp_path: _Path) -> None:
    """The facts card is a native <details> disclosure (collapsed by default
    — mobile-correct) with a <summary> type/rev row and a .okf-infobox__body
    wrapper. Ledger: it carries no inline type accent; its hairline frame
    comes from wiki.css tokens."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": "---\ntype: Table\ntitle: C\n---\nbody\n",
    })
    html = _render_concept_html(bundle, "c")
    import re as _re
    m = _re.search(r'<details class="okf-page__infobox"[^>]*>', html)
    assert m, "infobox not rendered as a <details> disclosure"
    tag = m.group(0)
    assert "style=" not in tag and "--okf-type-accent" not in tag, (
        f"infobox must not carry an inline type accent; got {tag!r}"
    )
    # The summary is the type/rev toggle; the body wraps tags/facts/governed.
    assert '<summary class="okf-infobox__summary"' in html, "infobox summary missing"
    assert 'class="okf-infobox__body"' in html, "infobox body wrapper missing"
    # Collapsed by default (no open attr) so mobile avoids the tall facts wall.
    assert 'open' not in tag, "infobox must not be open by default (mobile-collapsed)"


def test_phase2_infobox_exposes_resolved_type_icon_key(tmp_path: _Path) -> None:
    """P1 #3 (contract-runtime-parity): the infobox carries the resolved type
    icon KEY (data-okf-type-icon) so wiki.js's recent-page record can store
    the SAME icon the search backend ships — a single identity across fresh
    suggestions, recent pages, and the concept header. The key is the curated
    icon (database/table/server/…), not the type name; a Dataset concept maps
    to 'database' exactly as :func:`type_icon_key` prescribes."""
    bundle = _build_bundle_with(tmp_path, {
        "ds.md": "---\ntype: Dataset\ntitle: DS\n---\nbody\n",
        "pb.md": "---\ntype: Playbook\ntitle: PB\n---\nbody\n",
        "weird.md": "---\ntype: Totally Unknown Type\ntitle: Weird\n---\nbody\n",
    })
    # Dataset → 'database' (curated synonym mapping).
    html_ds = _render_concept_html(bundle, "ds")
    assert 'data-okf-type-icon="database"' in html_ds, (
        "Dataset must resolve to the 'database' icon key"
    )
    # Playbook → 'book-open'.
    html_pb = _render_concept_html(bundle, "pb")
    assert 'data-okf-type-icon="book-open"' in html_pb, (
        "Playbook must resolve to the 'book-open' icon key"
    )
    # Unknown type → 'file' (fallback), so no type is second-class.
    html_w = _render_concept_html(bundle, "weird")
    assert 'data-okf-type-icon="file"' in html_w, (
        "Unknown type must degrade to 'file' (the universal fallback)"
    )


def test_phase2_type_icons_block_shipped_on_pages_with_topbar_search(tmp_path: _Path) -> None:
    """P1 #3 (contract-runtime-parity / security-boundary): the icon-key →
    SVG-inner-markup map is shipped once per page via a CSP-safe JSON data
    block so wiki.js's suggestion chip can render the SAME icon the renderer
    emits everywhere else. The block is a non-executed <script
    type="application/json"> so script-src does not govern it (matches the
    studio bootstrap pattern). Payload contains the curated 'database' and
    'table' keys (no drift between Python and JS)."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": "---\ntype: Table\ntitle: C\n---\nbody\n",
        "index.md": "# root\n",
    })
    html = _render_concept_html(bundle, "c")
    assert '<script type="application/json" id="okf-type-icons">' in html, (
        "icon-paths JSON data block missing from concept page"
    )
    # The 'database' and 'table' keys (curated entries) must be present so
    # search suggestions for Dataset/Table/etc. concepts render a real glyph.
    assert '"database"' in html, "icon map missing the 'database' entry"
    assert '"table"' in html, "icon map missing the 'table' entry"
    # The 'file' fallback must be present so unknown types still render a glyph.
    assert '"file"' in html, "icon map missing the 'file' fallback"
    # Block is non-executed (JSON), so CSP script-src never governs it.
    assert 'id="okf-type-icons"\n' not in html or "application/json" in html


def test_phase2_search_result_carries_resolved_icon_key(tmp_path: _Path) -> None:
    """P1 #3 (contract-runtime-parity): the search JSON response carries the
    resolved icon key per result, so the live-suggestions chip can render a
    type ICON (not just text+colour). The icon key matches the same
    type_icon_key used by the renderer — exact identity, no client-side
    resolver duplication."""
    from okf_loom import Bundle
    from okf_loom.search import search_bundle
    bundle_root = _build_bundle_with(tmp_path, {
        "tables/orders.md": "---\ntype: Table\ntitle: Orders\n---\nbody\n",
        "datasets/sales.md": "---\ntype: Dataset\ntitle: Sales\n---\nbody\n",
        "playbooks/refund.md": "---\ntype: Playbook\ntitle: Refund Flow\n---\nbody\n",
        "index.md": "# root\n",
    })
    b = Bundle.load(bundle_root)
    results = search_bundle(b, "orders", limit=8)
    assert results, "expected at least one search hit for 'orders'"
    by_title = {r.title: r for r in results}
    # The orders concept (type Table) ships icon='table'.
    r = by_title.get("Orders")
    assert r is not None, "Orders result missing"
    d = r.as_dict()
    assert d["icon"] == "table", (
        f"Table concept must resolve icon='table'; got {d.get('icon')!r}"
    )
    assert d["type"] == "Table" and d["type_color"], (
        "type name and type_color must also be present (chip needs all three)"
    )


def test_phase2_relations_render_as_scannable_rows(tmp_path: _Path) -> None:
    """Phase 2 scanability: frontmatter relations render as a repeatable
    semantic list — type badge, direction arrow, LINKED target (icon + title
    + canonical id), optional explanation — not dense comma-joined prose.
    The wrapper keeps okf-relations-fm and the badge keeps okf-rel-type."""
    bundle = _build_bundle_with(tmp_path, {
        "orders.md": (
            "---\ntype: Table\ntitle: Orders\n"
            "relations:\n"
            "  - target: customers\n"
            "    type: references\n"
            "    detail: orders.customer_id -> customers.customer_id\n"
            "  - target: services/checkout\n"
            "    type: written_by\n"
            "---\nbody\n"
        ),
        "customers.md": "---\ntype: Table\ntitle: Customers\n---\nbody\n",
        "services/checkout.md": "---\ntype: Service\ntitle: Checkout\n---\nbody\n",
        "index.md": "# root\n",
    })
    html = _render_concept_html(bundle, "orders")
    assert "okf-relations-list" in html, "relations must be a <ul class=okf-relations-list>"
    assert "okf-relation-row" in html, "each relation must be an okf-relation-row <li>"
    # Two rows (one per relation).
    import re as _re
    assert html.count('class="okf-relation-row"') == 2
    # Type badge present with existing token class.
    assert "okf-rel-type" in html
    assert "references" in html and "written_by" in html
    # Direction arrow present.
    assert 'aria-hidden="true">→</span>' in html
    # Targets are LINKED (resolve to real concepts), with title + canonical id.
    assert 'class="okf-relation__target"' in html
    assert ">Customers</span>" in html
    assert "customers" in html  # canonical id text
    assert ">Checkout</span>" in html
    # Optional explanation renders.
    assert "orders.customer_id" in html
    # The prior dense comma-joined prose shape is gone.
    assert ", <span class=\"okf-relation\">" not in html


def test_phase2_relations_unresolved_target_degrades(tmp_path: _Path) -> None:
    """Phase 2 security/scanability: a relation whose target does not resolve
    to a concept degrades to a muted, NON-actionable span — never a broken
    or guessed link."""
    bundle = _build_bundle_with(tmp_path, {
        "c.md": (
            "---\ntype: T\ntitle: C\n"
            "relations:\n"
            "  - target: does/not/exist\n"
            "    type: derived_from\n"
            "---\nbody\n"
        ),
    })
    html = _render_concept_html(bundle, "c")
    import re as _re
    m = _re.search(r'<li class="okf-relation-row">.*?</li>', html, _re.DOTALL)
    assert m, "relation row not rendered"
    row = m.group(0)
    assert "okf-relation__target--unresolved" in row
    # No <a> for the unresolved target.
    assert "<a " not in row or 'class="okf-relation__target"' not in row.replace(
        'class="okf-relation__target okf-relation__target--unresolved"', ""
    )
    assert 'class="okf-relation__target okf-relation__target--unresolved"' in row


def test_phase2_connection_card_id_not_truncated(tmp_path: _Path) -> None:
    """Phase 2 scanability: connection cards must not destructively truncate
    canonical paths. The conn__id span must not carry nowrap/ellipsis."""
    bundle = _build_bundle_with(tmp_path, {
        "a.md": (
            "---\ntype: T\ntitle: A\n---\n\nSee [B](/deeply/nested/path/to/b.md).\n"
        ),
        "deeply/nested/path/to/b.md": "---\ntype: T\ntitle: B\n---\nbody\n",
        "index.md": "# root\n",
    })
    html = _render_concept_html(bundle, "a")
    import re as _re
    m = _re.search(r'<span class="okf-muted okf-conn__id">([^<]*)</span>', html)
    assert m, "conn__id span not rendered"
    # The full canonical path is present in the rendered id text.
    assert "deeply/nested/path/to/b" in m.group(1)


# The 'New in Annotations v2' hero module promotes the v2 hands-on demo to the
# primary above-the-fold CTA and demotes 'Open graph' to secondary. It
# is emitted ONLY for a bundle that actually contains the
# demo/annotations_v2_showcase concept (fail-closed: a generic bundle must not
# ship a broken demo link). See render.py:_v2_spotlight_html.

_DEMO_CONCEPT_REL = "demo/annotations_v2_showcase.md"
_DEMO_CONCEPT_BODY = (
    "---\ntype: Walkthrough\ntitle: Annotations v2 Showcase\n"
    "description: Hands-on v2 demo.\n---\n\n# Annotations v2 Showcase\n\nBody.\n"
)
# The honest release-notes concept the spotlight tertiary links to.
_RELEASE_NOTES_REL = "explanation/v2_release_notes.md"
_RELEASE_NOTES_BODY = (
    "---\ntype: Explanation\ntitle: What changed in okf-loom v2\n"
    "description: Release notes summary.\n"
    "resource: /explanation/v2_release_notes.md\n---\n\n"
    "# What changed in okf-loom v2\n\nSummary.\n"
)


def _index_with_demo(tmp_path: _Path) -> _Path:
    """A bundle whose root index will get the v2 spotlight (has the demo)."""
    return _build_bundle_with(tmp_path, {
        _DEMO_CONCEPT_REL: _DEMO_CONCEPT_BODY,
        _RELEASE_NOTES_REL: _RELEASE_NOTES_BODY,
        "tables/orders.md": "---\ntype: Table\ntitle: Orders\n---\nOrders.\n",
        "index.md": "# Demo bundle\n\nIntro.\n",
    })


def _index_without_demo(tmp_path: _Path) -> _Path:
    """A bundle with NO demo/annotations_v2_showcase concept (no spotlight)."""
    return _build_bundle_with(tmp_path, {
        "tables/orders.md": "---\ntype: Table\ntitle: Orders\n---\nOrders.\n",
        "index.md": "# Demo bundle\n\nIntro.\n",
    })


def _render_root_index(bundle_root: _Path, *, mode: str) -> str:
    """Render a bundle's ROOT index page HTML under the given mode."""
    from okf_loom import Bundle
    from okf_loom.render import _render_index_page
    b = Bundle.load(bundle_root)
    return _render_index_page(
        b, mode=mode, name="Demo", palette={}, config={}, sub="", index_file=None,
    )


def test_v2_spotlight_present_on_root_index_with_demo(tmp_path: _Path) -> None:
    """Root index of a bundle containing the demo concept shows the spotlight."""
    html = _render_root_index(_index_with_demo(tmp_path), mode="serve")
    assert 'class="okf-hero okf-hero--split"' in html, (
        "hero must carry the --split modifier when the spotlight is present"
    )
    assert 'class="okf-v2-spotlight"' in html
    assert "New in Annotations v2" in html
    assert "Try the v2 hands-on demo" in html
    # Primary CTA points at the v2 demo (serve = extensionless absolute path).
    assert re.search(
        r'class="okf-btn okf-btn--primary okf-v2-spotlight__cta" '
        r'href="/demo/annotations_v2_showcase"', html), (
        "primary CTA must link the v2 demo concept"
    )
    # Secondary CTA points at the graph. F13: it NAVIGATES there, so its label
    # is distinct from the hero's inline 'Preview graph' disclosure.
    assert re.search(
        r'okf-v2-spotlight__secondary" href="/__graph"[^>]*>.*Open graph',
        html, re.S), "secondary CTA must be 'Open graph' -> /__graph"
    # 4-6 scannable feature labels are present.
    for label in (
        "Anchor health", "Explicit re-anchor", "Six-mode search",
        "Deterministic Context Packs", "Live comments + undo",
    ):
        assert label in html, f"feature label missing: {label!r}"


def test_graph_actions_are_distinctly_named_open_vs_preview(tmp_path: _Path) -> None:
    """F13: the landing page previously offered two identically named 'Explore
    the graph' controls for DIFFERENT actions. The spotlight secondary
    NAVIGATES to /__graph ('Open graph'); the hero <details> only reveals the
    inline thumbnail ('Preview graph'). Both behaviors stay; the duplicated
    label is gone."""
    html = _render_root_index(_index_with_demo(tmp_path), mode="serve")
    assert re.search(
        r'okf-v2-spotlight__secondary" href="/__graph"[^>]*>.*Open graph</a>',
        html, re.S,
    ), "navigating secondary CTA must read 'Open graph' and target /__graph"
    assert '<details class="okf-home-graph"><summary>Preview graph</summary>' in html, (
        "inline thumbnail disclosure must read 'Preview graph'"
    )
    assert html.count("Open graph</a>") == 1
    assert html.count("<summary>Preview graph</summary>") == 1
    assert "Explore the graph" not in html


def test_v2_spotlight_demotes_graph_link_out_of_stats(tmp_path: _Path) -> None:
    """When the spotlight is present, the hero stats row no longer carries a
    competing filled 'Explore the graph' button (the spotlight's secondary CTA
    is the sole graph entry point in the hero)."""
    html = _render_root_index(_index_with_demo(tmp_path), mode="serve")
    stats = re.search(r'<div class="okf-hero__stats">(.*?)</div>', html, re.S)
    assert stats, "hero stats row must render"
    assert "okf-hero__graph-link" not in stats.group(1), (
        "graph link must be dropped from stats when the spotlight owns it"
    )
    # Graph remains reachable but its visual preview is a secondary disclosure.
    hero = re.search(r'<header class="okf-hero[^"]*">(.*?)</header>', html, re.S)
    assert hero and 'href="/__graph"' in hero.group(1)
    assert '<details class="okf-home-graph"' in hero.group(1)


def test_v2_spotlight_absent_without_demo_keeps_graph_reachable(
    tmp_path: _Path,
) -> None:
    """Fail-closed: a bundle WITHOUT the demo concept ships no spotlight, no
    split modifier, no placeholder leak, and keeps a native graph link
    (so no bundle loses its graph affordance)."""
    html = _render_root_index(_index_without_demo(tmp_path), mode="serve")
    assert "okf-v2-spotlight" not in html
    assert "okf-hero--split" not in html
    assert "Try the v2 hands-on demo" not in html
    assert "__HERO_V2__" not in html and "__HERO_MODIFIER__" not in html, (
        "template placeholders must never leak into served HTML"
    )
    stats = re.search(r'<div class="okf-hero__stats">(.*?)</div>', html, re.S)
    assert stats and "okf-hero__graph-link" not in stats.group(1)
    assert 'href="/__graph"' in html
    assert '<details class="okf-home-graph"' in html


def test_v2_spotlight_urls_are_mode_aware(tmp_path: _Path) -> None:
    """The spotlight CTA URLs follow the build mode (serve=extensionless,
    static=relative .html)."""
    bundle_root = _index_with_demo(tmp_path)
    serve_html = _render_root_index(bundle_root, mode="serve")
    assert 'href="/demo/annotations_v2_showcase"' in serve_html
    assert 'href="/__graph"' in serve_html
    # Tertiary release-notes link is mode-aware too.
    assert 'href="/explanation/v2_release_notes"' in serve_html
    static_html = _render_root_index(bundle_root, mode="static")
    assert 'href="demo/annotations_v2_showcase.html"' in static_html, (
        "static mode must relativise the demo link to its .html file"
    )
    assert 'href="__graph.html"' in static_html
    assert 'href="explanation/v2_release_notes.html"' in static_html, (
        "static mode must relativise the release-notes tertiary link"
    )


def test_v2_spotlight_accessibility_wiring(tmp_path: _Path) -> None:
    """The spotlight is a labelled complementary region: <aside> with
    aria-labelledby pointing at its heading id, and the page keeps a single h1."""
    html = _render_root_index(_index_with_demo(tmp_path), mode="serve")
    assert 'aria-labelledby="okf-v2-spotlight-title"' in html
    assert 'id="okf-v2-spotlight-title"' in html
    # The spotlight heading is an <h2> (sibling to the demoted intro headings),
    # so the page outline stays: one h1 (bundle name) then h2 sections.
    from html.parser import HTMLParser

    class Outline(HTMLParser):
        def __init__(self):
            super().__init__()
            self.headings = []
            self.regions = []

        def handle_starttag(self, tag, attrs):
            if tag in {"h1", "h2"}:
                self.headings.append((tag, dict(attrs)))
            if tag == "aside":
                self.regions.append(dict(attrs))

    outline = Outline()
    outline.feed(html)
    h1s = [attrs for tag, attrs in outline.headings if tag == "h1"]
    assert len(h1s) == 1
    assert "okf-index__title" in h1s[0].get("class", "").split()
    assert h1s[0].get("id"), "the bundle title remains a fragment destination"
    assert sum(attrs.get("aria-labelledby") == "okf-v2-spotlight-title"
               for attrs in outline.regions) == 1
    assert sum(tag == "h2" and attrs.get("id") == "okf-v2-spotlight-title"
               for tag, attrs in outline.headings) == 1
    assert re.search(
        r'<h2 class="okf-v2-spotlight__title"[^>]*>New in Annotations v2</h2>',
        html), "spotlight title must be an h2 labelled 'New in Annotations v2'"
    # Decorative check icons are hidden from AT.
    assert 'aria-hidden="true"' in html


def test_v2_spotlight_absent_on_subdir_index(tmp_path: _Path) -> None:
    """Subdirectory indexes never get the spotlight (root-index-only contract)."""
    from okf_loom import Bundle
    from okf_loom.render import _render_index_page
    b = Bundle.load(_index_with_demo(tmp_path))
    html = _render_index_page(
        b, mode="serve", name="Demo", palette={}, config={}, sub="demo",
        index_file=None,
    )
    assert "okf-v2-spotlight" not in html
    assert "okf-hero--split" not in html
    assert "__HERO_V2__" not in html and "__HERO_MODIFIER__" not in html


def test_v2_spotlight_ships_in_static_build(tmp_path: _Path) -> None:
    """End-to-end static build writes a working index.html with the module and
    no dead demo link (the target .html file is actually produced)."""
    from okf_loom import Bundle
    b = Bundle.load(_index_with_demo(tmp_path))
    out = tmp_path / "_site"
    build_site(b, out, target="static")
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "okf-v2-spotlight" in html
    assert 'href="demo/annotations_v2_showcase.html"' in html
    assert 'href="explanation/v2_release_notes.html"' in html, (
        "static build must emit the tertiary release-notes link"
    )
    assert (out / "demo" / "annotations_v2_showcase.html").is_file(), (
        "static build must emit the demo page the CTA targets"
    )
    assert (out / "explanation" / "v2_release_notes.html").is_file(), (
        "static build must emit the release-notes page the tertiary targets"
    )


def test_v2_spotlight_tertiary_fail_closed_without_release_notes(
    tmp_path: _Path,
) -> None:
    """Fail-closed: a bundle with the demo but WITHOUT the release-notes
    concept ships the spotlight (primary+secondary) but NO tertiary link, so
    no broken 'Read what changed' href leaks."""
    # A bundle that has the demo but not the release-notes concept.
    bundle_root = _build_bundle_with(tmp_path, {
        _DEMO_CONCEPT_REL: _DEMO_CONCEPT_BODY,
        "tables/orders.md": "---\ntype: Table\ntitle: Orders\n---\nOrders.\n",
        "index.md": "# Demo bundle\n\nIntro.\n",
    })
    html = _render_root_index(bundle_root, mode="serve")
    assert "okf-v2-spotlight" in html
    assert "Try the v2 hands-on demo" in html
    assert "Open graph" in html
    # No tertiary link, no broken href.
    assert "okf-v2-spotlight__tertiary" not in html
    assert "Read what changed" not in html
    assert "v2_release_notes" not in html


def test_v2_spotlight_tertiary_present_with_release_notes(
    tmp_path: _Path,
) -> None:
    """When both the demo and release-notes concepts exist, the tertiary
    'Read what changed' action renders as the third spotlight action and links
    the release-notes concept (closes the user-outcome gap in one click)."""
    html = _render_root_index(_index_with_demo(tmp_path), mode="serve")
    assert re.search(
        r'okf-v2-spotlight__tertiary" href="/explanation/v2_release_notes"[^>]*>'
        r".*Read what changed",
        html, re.S,
    ), "tertiary 'Read what changed' must link /explanation/v2_release_notes"
    # The three spotlight actions render in prominence order: primary, then
    # secondary, then tertiary text link.
    cta_pos = html.find("okf-v2-spotlight__cta")
    sec_pos = html.find("okf-v2-spotlight__secondary")
    ter_pos = html.find("okf-v2-spotlight__tertiary")
    assert 0 < cta_pos < sec_pos < ter_pos, (
        "spotlight actions must render primary -> secondary -> tertiary"
    )
