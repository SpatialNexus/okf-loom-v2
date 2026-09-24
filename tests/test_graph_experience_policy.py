"""Focused contract proof for the remaining graph experience slice."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.okf_loom.model import Bundle
from scripts.okf_loom.render import _graph_thumbnail_html, build_graph_data, graph_export_svg


ROOT = Path(__file__).resolve().parent.parent
GRAPH_JS = ROOT / "scripts/okf_loom/viewer/static/graph.js"


def test_graph_thumbnail_hides_only_the_link_copy_of_the_canonical_svg() -> None:
    payload = build_graph_data(Bundle.load(ROOT / "samples/demo_bundle"))
    canonical = graph_export_svg(payload)
    thumbnail = _graph_thumbnail_html(payload, "/__graph")

    canonical_root = ET.fromstring(canonical)
    assert canonical_root.attrib["role"] == "img"
    assert "aria-labelledby" in canonical_root.attrib
    assert canonical_root.find("title") is not None

    assert 'aria-label=' not in thumbnail
    assert '<span class="sr-only">Open knowledge graph — </span>' in thumbnail
    assert re.search(r'<figcaption><strong>Explore connections</strong><span>\d+ concepts · \d+ relationships?</span>', thumbnail)
    nested_svg = re.search(r"(<svg[^>]+>)", thumbnail)
    assert nested_svg
    assert 'aria-hidden="true"' in nested_svg.group(1)
    assert 'focusable="false"' in nested_svg.group(1)
    assert 'role="img"' not in nested_svg.group(1)
    assert canonical == graph_export_svg(payload), "wrapper adaptation mutated canonical export"


def test_graph_source_uses_threshold_policy_and_native_wheel_behavior() -> None:
    source = GRAPH_JS.read_text(encoding="utf-8")
    assert "wheelSensitivity" not in source
    assert "DENSITY_THRESHOLDS = [0, 25, 50, 100, 250]" in source
    assert "Suggested starting points" in source
    assert 'label: "Explore neighborhood"' in source
    assert 'summary>Graph settings<' in source or '["Graph settings"]' in source
