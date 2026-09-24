"""Export side of portable page-level feedback notes (roadmap X2).

``--include-portable-feedback`` is an explicit opt-in for static and
single-file exports. Each concept page binds to the SHA-256 of its canonical
document's exact on-disk bytes (never newline-normalized text), and a page
whose bytes no longer match what the build loaded is skipped, warned about and
counted rather than failing the build. Default output is untouched.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from scripts.okf_loom import portable_feedback
from scripts.okf_loom.cli import main
from scripts.okf_loom.model import Bundle
from scripts.okf_loom.portable_feedback_export import (
    ASSET_NAME,
    collect_targets,
    exact_source_target,
    page_bootstrap_script,
)
from scripts.okf_loom.render import build_site, render_single_file
from scripts.okf_loom.viewer.assets import OPT_IN_STATIC, list_builtin_static, load_static

BOOTSTRAP = re.compile(
    r'<script type="application/json" id="okf-portable-feedback">(.*?)</script>', re.S)
PAGE_SCRIPT = re.compile(r'\n<script src="[^"]*/portable-feedback\.js" defer></script>')
ASSET_PATH = (Path(__file__).resolve().parent.parent / "scripts" / "okf_loom" / "viewer"
              / "static" / ASSET_NAME)
ORDERS = "---\ntype: Table\ntitle: Orders\n---\n\n# Orders\n\nOne row per order.\n"
CRLF = b"---\r\ntype: Note\r\ntitle: Windows note\r\n---\r\n\r\n# Windows note\r\n\r\nWritten with CRLF.\r\n"


def _bundle(root: Path) -> Path:
    (root / "tables").mkdir(parents=True)
    (root / "notes").mkdir()
    (root / "index.md").write_text("# Home\n\nOverview.\n", encoding="utf-8")
    (root / "tables" / "orders.md").write_text(ORDERS, encoding="utf-8")
    (root / "notes" / "windows.md").write_bytes(CRLF)
    (root / "top.md").write_text("---\ntype: Note\ntitle: Top\n---\n\n# Top\n\nText.\n", encoding="utf-8")
    return root


def _bootstrap(html: str) -> dict | None:
    found = BOOTSTRAP.findall(html)
    assert len(found) <= 1
    return json.loads(found[0]) if found else None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_static_pages_bind_to_the_exact_disk_bytes_and_load_the_composer(tmp_path):
    root = _bundle(tmp_path / "bundle")
    out = tmp_path / "site"
    stats = build_site(Bundle.load(root), out, target="static", include_portable_feedback=True)

    orders = (out / "tables" / "orders.html").read_text(encoding="utf-8")
    assert _bootstrap(orders) == {
        "version": 1, "document": "tables/orders.md",
        "source_sha256": _sha((root / "tables" / "orders.md").read_bytes()),
    }
    assert '<script src="../__static/portable-feedback.js" defer></script>' in orders
    top = (out / "top.html").read_text(encoding="utf-8")
    assert _bootstrap(top)["document"] == "top.md"
    assert '<script src="__static/portable-feedback.js" defer></script>' in top
    assert (out / "__static" / "portable-feedback.js").is_file()
    # Reserved documents and synthesized pages are never note targets.
    for page in ("index.html", "tables/index.html", "__graph.html", "__search.html"):
        html = (out / page).read_text(encoding="utf-8")
        assert _bootstrap(html) is None and "portable-feedback.js" not in html
    assert stats["portable_feedback_pages"] == 3
    assert stats["portable_feedback_skipped"] == 0


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _strip_opt_in(html: str) -> str:
    return PAGE_SCRIPT.sub("", BOOTSTRAP.sub("", html))


def test_static_default_is_unchanged_and_the_opt_in_only_adds_its_snippets(tmp_path):
    root = _bundle(tmp_path / "bundle")
    default = tmp_path / "default"
    explicit_off = tmp_path / "off"
    opted_in = tmp_path / "on"
    default_stats = build_site(Bundle.load(root), default, target="static")
    build_site(Bundle.load(root), explicit_off, target="static", include_portable_feedback=False)
    build_site(Bundle.load(root), opted_in, target="static", include_portable_feedback=True)

    base = _tree(default)
    assert _tree(explicit_off) == base
    assert "portable_feedback_pages" not in default_stats
    assert f"__static/{ASSET_NAME}" not in base
    assert not any(b"okf-portable-feedback" in data or ASSET_NAME.encode() in data
                   for data in base.values())

    added = _tree(opted_in)
    assert set(added) == set(base) | {f"__static/{ASSET_NAME}"}
    assert added[f"__static/{ASSET_NAME}"] == ASSET_PATH.read_bytes()
    for name, data in base.items():
        if name.endswith(".html"):
            assert _strip_opt_in(added[name].decode("utf-8")) == data.decode("utf-8"), name
        else:
            assert added[name] == data, name


def test_single_file_embeds_a_concept_map_and_inlines_the_composer_only_on_opt_in(tmp_path):
    root = _bundle(tmp_path / "bundle")
    default = render_single_file(Bundle.load(root), tmp_path / "default.html")
    render_single_file(Bundle.load(root), tmp_path / "off.html", include_portable_feedback=False)
    stats = render_single_file(Bundle.load(root), tmp_path / "on.html", include_portable_feedback=True)

    base = (tmp_path / "default.html").read_text(encoding="utf-8")
    assert (tmp_path / "off.html").read_text(encoding="utf-8") == base
    assert "portable_feedback_pages" not in default
    assert "okf-portable-feedback" not in base and "__okfPortableFeedbackMounted" not in base

    html = (tmp_path / "on.html").read_text(encoding="utf-8")
    boot = _bootstrap(html)
    assert boot == {"version": 1, "documents": {
        "notes/windows": {"document": "notes/windows.md", "source_sha256": _sha(CRLF)},
        "tables/orders": {"document": "tables/orders.md",
                          "source_sha256": _sha(ORDERS.encode("utf-8"))},
        "top": {"document": "top.md",
                "source_sha256": _sha((root / "top.md").read_bytes())},
    }}
    assert html.index('id="okf-portable-feedback"') < html.index("</head>")
    asset = load_static(ASSET_NAME)
    assert asset in html
    assert (stats["portable_feedback_pages"], stats["portable_feedback_skipped"]) == (3, 0)
    assert BOOTSTRAP.sub("", html).replace("\n" + asset, "", 1) == base


def test_crlf_source_binds_to_raw_disk_bytes_not_normalized_text(tmp_path):
    root = _bundle(tmp_path / "bundle")
    bundle = Bundle.load(root)
    concept = next(c for c in bundle.concepts.values() if c.title == "Windows note")
    assert "\r" not in concept.raw_text
    target, reason = exact_source_target(bundle, concept)
    assert reason is None
    assert target == {"document": "notes/windows.md", "source_sha256": _sha(CRLF)}
    assert target["source_sha256"] != _sha(concept.raw_text.encode("utf-8"))

    out = tmp_path / "site"
    build_site(bundle, out, target="static", include_portable_feedback=True)
    page = (out / "notes" / "windows.html").read_text(encoding="utf-8")
    assert _bootstrap(page)["source_sha256"] == _sha((root / "notes" / "windows.md").read_bytes())


def test_the_exported_target_is_admitted_by_the_import_parser(tmp_path):
    root = _bundle(tmp_path / "bundle")
    targets = collect_targets(Bundle.load(root)).targets
    for target in targets.values():
        note = json.dumps({
            "format": portable_feedback.NOTE_FORMAT, "version": portable_feedback.NOTE_VERSION,
            "note_id": "0" * 32, "target": target, "body": "Looks right.",
        }, indent=2).encode("utf-8") + b"\n"
        parsed = portable_feedback.parse_note(note)
        assert (parsed.document, parsed.source_sha256) == (
            target["document"], target["source_sha256"])


def test_pages_whose_source_moved_after_load_are_skipped_warned_and_counted(tmp_path, capsys):
    root = _bundle(tmp_path / "bundle")
    (root / "notes" / "latin.md").write_text(
        "---\ntype: Note\ntitle: Latin\n---\n\nText.\n", encoding="utf-8")
    (root / "notes" / "keep.md").write_text(
        "---\ntype: Note\ntitle: Keep\n---\n\nStable.\n", encoding="utf-8")
    bundle = Bundle.load(root)

    (root / "tables" / "orders.md").write_text(ORDERS + "\nEdited later.\n", encoding="utf-8")
    (root / "top.md").unlink()
    outside = tmp_path / "outside.md"
    outside.write_bytes(CRLF)
    (root / "notes" / "windows.md").unlink()
    (root / "notes" / "windows.md").symlink_to(outside)
    (root / "notes" / "latin.md").write_bytes(b"---\ntype: Note\ntitle: Latin\n---\n\nT\xe9xt.\n")

    out = tmp_path / "site"
    stats = build_site(bundle, out, target="static", include_portable_feedback=True)
    assert (stats["portable_feedback_pages"], stats["portable_feedback_skipped"]) == (1, 4)
    err = capsys.readouterr().err
    for document, message in (
            ("tables/orders.md", "its source changed after the bundle was loaded"),
            ("top.md", "its source file is missing or not visible"),
            ("notes/windows.md", "its source could not be read safely"),
            ("notes/latin.md", "its source is not valid UTF-8")):
        assert f"warning: portable feedback skipped for {document}: {message}" in err
    for page in ("tables/orders.html", "top.html", "notes/windows.html", "notes/latin.html"):
        html = (out / page).read_text(encoding="utf-8")
        assert _bootstrap(html) is None and ASSET_NAME not in html, page
    keep = (out / "notes" / "keep.html").read_text(encoding="utf-8")
    assert _bootstrap(keep)["source_sha256"] == _sha((root / "notes" / "keep.md").read_bytes())
    assert (out / "__static" / ASSET_NAME).is_file()


def test_cli_refuses_spa_before_writing_and_reports_counts(tmp_path, capsys):
    root = _bundle(tmp_path / "bundle")
    spa = tmp_path / "spa"
    assert main(["build", str(root), "--target", "spa", "--out", str(spa),
                 "--include-portable-feedback"]) == 2
    assert not spa.exists()
    assert ("error: --include-portable-feedback is supported only for --target static "
            "and --target single-file") in capsys.readouterr().err
    with pytest.raises(ValueError, match="static and single-file"):
        build_site(Bundle.load(root), spa, target="spa", include_portable_feedback=True)
    assert not spa.exists()

    viz = tmp_path / "viz.html"
    assert main(["render", str(root), "--out", str(viz), "--include-portable-feedback"]) == 0
    assert "Portable feedback: 3 pages ready, 0 skipped" in capsys.readouterr().out
    assert len(_bootstrap(viz.read_text(encoding="utf-8"))["documents"]) == 3

    plain = tmp_path / "plain.html"
    assert main(["render", str(root), "--out", str(plain)]) == 0
    assert "Portable feedback" not in capsys.readouterr().out

    single = tmp_path / "single"
    assert main(["build", str(root), "--target", "single-file", "--out", str(single),
                 "--include-portable-feedback"]) == 0
    out = capsys.readouterr().out
    assert "portable_feedback_pages: 3" in out and "portable_feedback_skipped: 0" in out
    assert len(_bootstrap((single / "index.html").read_text(encoding="utf-8"))["documents"]) == 3

    static = tmp_path / "static"
    assert main(["build", str(root), "--target", "static", "--out", str(static),
                 "--include-portable-feedback"]) == 0
    assert "portable_feedback_pages: 3" in capsys.readouterr().out
    assert (static / "__static" / ASSET_NAME).is_file()


def test_bootstrap_json_cannot_close_its_script_element():
    script = page_bootstrap_script({"document": "a/</script><b>&.md", "source_sha256": "0" * 64})
    inner = script[len('<script type="application/json" id="okf-portable-feedback">'):-len("</script>")]
    assert "<" not in inner and ">" not in inner and "&" not in inner
    assert json.loads(inner)["document"] == "a/</script><b>&.md"


def test_the_composer_asset_is_opt_in_offline_and_mirrors_the_parser_limits():
    assert ASSET_NAME in OPT_IN_STATIC
    assert ASSET_PATH.is_file() and ASSET_NAME not in list_builtin_static()
    source = ASSET_PATH.read_text(encoding="utf-8")
    for forbidden in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource",
                      "localStorage", "sessionStorage", "indexedDB", "innerHTML",
                      "outerHTML", "insertAdjacentHTML", "document.cookie", "document.write",
                      "eval(", "new Function", "history.pushState", "history.replaceState"):
        assert forbidden not in source, forbidden

    def constant(name: str) -> int:
        return int(re.search(rf"var {name} = (\d+);", source).group(1))

    assert constant("MAX_NOTE_BYTES") == portable_feedback.MAX_NOTE_BYTES
    assert constant("MAX_BODY_CODE_POINTS") == portable_feedback.MAX_BODY_CODE_POINTS
    assert constant("MAX_QUOTE_CODE_POINTS") == portable_feedback.MAX_QUOTE_CODE_POINTS
    assert (constant("MAX_CLAIMED_AUTHOR_CODE_POINTS")
            == portable_feedback.MAX_CLAIMED_AUTHOR_CODE_POINTS)
    assert constant("NOTE_VERSION") == portable_feedback.NOTE_VERSION
    assert f'var FORMAT = "{portable_feedback.NOTE_FORMAT}";' in source
