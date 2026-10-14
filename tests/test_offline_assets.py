"""X1: opt-in offline assets (``--assets offline``) — no-browser contracts.

Default output is unchanged; the vendored bytes are verified against the lock,
the code's SRI pins and the pinned derived outputs before anything is written;
offline output is closed (no remote resource reference) and carries licences.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

from conftest import TOOLKIT_ROOT, okf_module_argv, okf_subprocess_env
from okf_loom import render
from okf_loom.model import Bundle
from okf_loom.render import build_site, render_single_file
from okf_loom.viewer import vendor

RICH_BODY = r"""
# Rich rendering

Links to [the other note](other.md).

```mermaid
flowchart LR
  A[Start] --> B{Check}
  B --> C[Done]
```

```mermaid
gantt
  title Plan
  dateFormat YYYY-MM-DD
  section Build
  Vendor :a1, 2026-01-01, 3d
  Prove  :after a1, 2d
```

```math
\mathbb{R} \ni x \mapsto \mathfrak{g}(x) + \mathscr{L}\left( \sum_{i=1}^{n} \frac{a_i}{b_i} \right)
```

```python
def greet(name):
    return f"hello {name}"
```

```elixir
defmodule Greeter do
  def hello(name), do: "hello #{name}"
end
```
"""


def write_offline_fixture(root: Path, *, rich: bool = True) -> Path:
    """A two-concept bundle; ``rich`` adds Mermaid, math and code blocks."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.md").write_text('---\nokf: "0.2"\n---\nOffline asset proof bundle.\n', encoding="utf-8")
    body = RICH_BODY if rich else "\n# Plain\n\nLinks to [the other note](other.md).\n"
    (root / "rich.md").write_text(
        "---\ntype: Note\ntitle: Rich rendering\n"
        "description: Mermaid, math and code for the offline proof.\n---\n" + body,
        encoding="utf-8")
    (root / "other.md").write_text(
        "---\ntype: Note\ntitle: Other note\ndescription: The link target.\n---\n"
        "\n# Other\n\nBack to [rich](rich.md).\n", encoding="utf-8")
    return root


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _html_pages(root: Path) -> list[Path]:
    return sorted(root.rglob("*.html"))


def _csp(html: str) -> list[str]:
    return re.findall(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)">', html)


# ---------------------------------------------------------------------------
# Default (cdn) output is unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["static", "spa", "single-file"])
def test_default_and_explicit_cdn_output_are_identical_and_vendor_free(tmp_path, target):
    bundle = Bundle.load(write_offline_fixture(tmp_path / "bundle"))
    build_site(bundle, tmp_path / "default", target=target)
    build_site(bundle, tmp_path / "cdn", target=target, assets="cdn")
    default, cdn = _tree(tmp_path / "default"), _tree(tmp_path / "cdn")
    assert default == cdn
    assert not any(name.startswith("__static/vendor") for name in default)
    for page in _html_pages(tmp_path / "default"):
        html = page.read_text(encoding="utf-8")
        assert 'id="okf-asset-bootstrap"' not in html
        assert 'id="okf-vendor-' not in html
        expected = vendor.CDN_SINGLE_FILE_CSP if target == "single-file" else vendor.CDN_PAGE_CSP
        assert _csp(html) == [expected], page
        font_src = _csp(html)[0].split("font-src ", 1)[1].split(";", 1)[0]
        assert font_src == ("'self' data: https://cdn.jsdelivr.net" if target == "single-file"
                            else "'self' https://cdn.jsdelivr.net"), page
    if target != "single-file":
        graph = (tmp_path / "default" / "__graph.html").read_text(encoding="utf-8")
        assert render._CDN_SCRIPTS.replace("<script ", "<script defer ") in graph


class _ScriptTags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.scripts = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts.append(dict(attrs))


@pytest.mark.parametrize("mode", ["serve", "static", "spa"])
@pytest.mark.parametrize("assets", ["cdn", "offline"])
def test_graph_vendor_chain_defers_in_order_without_changing_integrity(tmp_path, mode, assets):
    bundle = Bundle.load(write_offline_fixture(tmp_path / "bundle"))
    offline = vendor.load_offline_assets() if assets == "offline" else None
    html = render._render_graph_page(bundle, mode=mode, name="Proof", config={},
                                     offline_assets=offline)
    scripts = _ScriptTags(html).scripts
    graph_index = next(i for i, tag in enumerate(scripts) if tag.get("src", "").endswith("/graph.js"))
    chain = [tag for tag in scripts[:graph_index] if "cdn.jsdelivr.net" in tag.get("src", "")
             or "/vendor/" in tag.get("src", "")]
    original = _ScriptTags(render._CDN_SCRIPTS).scripts
    locked = {entry["cdn"]: entry for entry in vendor.lock_files(vendor.load_lock()) if entry.get("cdn")}
    assert len(chain) == 6
    prefix = "/__static" if mode in ("serve", "spa") else "__static"
    for tag, pin in zip(chain, original):
        assert "defer" in tag and "async" not in tag
        if assets == "cdn":
            assert {key: value for key, value in tag.items() if key != "defer"} == pin
            assert tag["integrity"] == locked[tag["src"]]["sha384"]
            assert tag["crossorigin"] == "anonymous"
        else:
            assert tag["src"] == f'{prefix}/vendor/{locked[pin["src"]]["dest"]}'
            assert "integrity" not in tag and "crossorigin" not in tag
    assert "defer" in scripts[graph_index]
    if mode != "serve":
        theme = next(tag for tag in scripts if tag.get("src", "").endswith("/theme.js"))
        assert "defer" not in theme and "async" not in theme


def test_render_single_file_default_equals_cdn(tmp_path):
    bundle = Bundle.load(write_offline_fixture(tmp_path / "bundle"))
    render_single_file(bundle, tmp_path / "a.html")
    render_single_file(bundle, tmp_path / "b.html", assets="cdn")
    assert (tmp_path / "a.html").read_bytes() == (tmp_path / "b.html").read_bytes()


def test_unknown_assets_mode_is_rejected(tmp_path):
    bundle = Bundle.load(write_offline_fixture(tmp_path / "bundle"))
    with pytest.raises(ValueError, match="Unknown assets mode"):
        build_site(bundle, tmp_path / "out", target="static", assets="local")
    assert not (tmp_path / "out").exists()


# ---------------------------------------------------------------------------
# Drift guards: lock <-> code pins <-> renderers.js <-> repository layout
# ---------------------------------------------------------------------------

def test_lock_matches_code_pins_versions_and_sri():
    lock = vendor.load_lock()
    pins = vendor.source_sri_pins()
    entries = vendor.lock_files(lock)
    by_cdn = {entry["cdn"]: entry for entry in entries if entry.get("cdn")}
    # Every SRI pinned in the code has a vendored copy with the same sha384 ...
    assert set(pins) == set(by_cdn)
    for url, integrity in pins.items():
        assert by_cdn[url]["sha384"] == integrity
    # ... and every package version equals the version in the code's CDN URLs.
    code_versions = {}
    renderers = (vendor.VENDOR_ROOT.parent / "static" / "renderers.js").read_text(encoding="utf-8")
    for url in [*pins, *re.findall(r'"(https://cdn\.jsdelivr\.net/npm/[^"]+)"', renderers)]:
        name, version = re.match(r"https://cdn\.jsdelivr\.net/npm/((?:@[^/]+/)?[^@/]+)@([^/]+)/", url).groups()
        code_versions[name] = version
    lock_versions = {package["name"]: package["version"] for package in lock["packages"] if package["license"] != "OFL-1.1"}
    lock_versions["highlight.js"] = lock_versions.pop("@highlightjs/cdn-assets")
    assert code_versions == lock_versions
    assert [ref["version"] for ref in lock["reference_packages"]] == ["11.12.0"]


def test_renderers_offline_table_matches_vendor_module():
    renderers = (vendor.VENDOR_ROOT.parent / "static" / "renderers.js").read_text(encoding="utf-8")
    table = dict(
        (key, (path, carrier)) for key, path, carrier in re.findall(
            r'(\w+): \{ file: "([^"]+)", carrier: "([^"]+)" \}', renderers))
    assert table == vendor.RENDERER_ASSETS


def test_vendor_paths_survive_the_release_archive():
    sys.path.insert(0, str(TOOLKIT_ROOT / "scripts"))
    try:
        import build_skill_archive as archive
    finally:
        sys.path.remove(str(TOOLKIT_ROOT / "scripts"))
    rel_root = vendor.VENDOR_ROOT.relative_to(TOOLKIT_ROOT).as_posix()
    lock = vendor.load_lock()
    names = [vendor.LOCK_NAME, *(entry["dest"] for entry in vendor.auxiliary_files(lock)),
             *(entry["dest"] for entry in vendor.lock_files(lock))]
    assert vendor.NOTICES_NAME in names
    for name in names:
        parts = set(name.lower().split("/"))
        assert not parts & {"dist", "build"}, name
        assert not archive.is_path_denied(f"{rel_root}/{name}"), name


def test_fetch_script_check_mode_passes_offline():
    result = subprocess.run([sys.executable, str(TOOLKIT_ROOT / "scripts" / "fetch_vendor_assets.py"), "--check"],
                            capture_output=True, text=True, env=okf_subprocess_env(), timeout=120)
    assert result.returncode == 0, result.stderr
    assert "vendor check OK" in result.stdout


def test_hljs_parity_list_is_the_full_build_and_vendored_languages_cover_it():
    lock = vendor.load_lock()
    full = lock["parity"]["highlightjs_languages"]
    common = lock["parity"]["highlightjs_common"]
    assert len(full) == 193 and "elixir" in full and "python" in common
    vendored = {entry["dest"].rsplit("/", 1)[1][:-len(".min.js")]
                for entry in vendor.lock_files(lock) if entry["dest"].startswith(vendor.HLJS_LANGUAGE_DIR)}
    assert vendored == set(full) - set(common)


# ---------------------------------------------------------------------------
# Integrity negatives: nothing is written, previous output survives
# ---------------------------------------------------------------------------

@pytest.fixture
def vendor_copy(tmp_path, monkeypatch):
    copy = tmp_path / "vendor-copy"
    shutil.copytree(vendor.VENDOR_ROOT, copy)
    monkeypatch.setattr(vendor, "VENDOR_ROOT", copy)
    return copy


def _flip_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))


@pytest.mark.parametrize("damage", ["flip", "missing", "symlink"])
@pytest.mark.parametrize("target", ["static", "single-file"])
def test_integrity_failure_fails_the_build_and_keeps_previous_output(tmp_path, vendor_copy, damage, target):
    bundle = Bundle.load(write_offline_fixture(tmp_path / "bundle"))
    out = tmp_path / "out"
    build_site(bundle, out, target=target)
    before = _tree(out)
    victim = vendor_copy / "cytoscape-3.34.3" / "cytoscape.min.js"
    if damage == "flip":
        _flip_byte(victim)
    elif damage == "missing":
        victim.unlink()
    else:
        real = tmp_path / "elsewhere.js"
        shutil.move(victim, real)
        victim.symlink_to(real)
    with pytest.raises(vendor.VendorIntegrityError):
        build_site(bundle, out, target=target, assets="offline")
    assert _tree(out) == before


def test_lock_and_bytes_changed_together_still_fail_the_code_sri(vendor_copy):
    victim = vendor_copy / "layout-base-2.0.1" / "layout-base.js"
    _flip_byte(victim)
    lock_path = vendor_copy / vendor.LOCK_NAME
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    for entry in vendor.lock_files(lock):
        if entry["dest"] == "layout-base-2.0.1/layout-base.js":
            data = victim.read_bytes()
            entry.update(sha256=vendor.sha256_hex(data), sha384=vendor.sri_sha384(data))
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(vendor.VendorIntegrityError, match="does not match the code's SRI"):
        vendor.load_offline_assets()


def test_derived_output_hash_is_pinned(vendor_copy):
    lock_path = vendor_copy / vendor.LOCK_NAME
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["derived"][vendor.KATEX_DERIVED]["sha256"] = "0" * 64
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(vendor.VendorIntegrityError, match="derived offline asset"):
        vendor.load_offline_assets()


def test_lock_paths_cannot_escape_the_vendor_folder(vendor_copy):
    lock_path = vendor_copy / vendor.LOCK_NAME
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["packages"][0]["files"][0]["dest"] = "../vendor-copy/mermaid-11.17.2/mermaid.min.js"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(vendor.VendorIntegrityError, match="escapes"):
        vendor.load_offline_assets()


# ---------------------------------------------------------------------------
# Active-code exclusion (exit 2, nothing written)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verb", ["build", "render"])
def test_offline_with_allow_active_code_is_a_usage_error(tmp_path, verb):
    bundle = write_offline_fixture(tmp_path / "bundle")
    out = tmp_path / ("out" if verb == "build" else "out.html")
    argv = okf_module_argv(verb, str(bundle), "--out", str(out), "--assets", "offline", "--allow-active-code")
    result = subprocess.run(argv, capture_output=True, text=True, env=okf_subprocess_env(), timeout=120)
    assert result.returncode == 2
    assert "--assets offline cannot be combined with --allow-active-code" in result.stderr
    assert not out.exists()


def test_offline_is_refused_when_the_effective_gate_is_open_via_env(tmp_path):
    bundle = write_offline_fixture(tmp_path / "bundle")
    (bundle / "okf-loom.config.yaml").write_text("viewer:\n  allow_active_code: true\n", encoding="utf-8")
    out = tmp_path / "out"
    env = okf_subprocess_env(OKF_LOOM_ALLOW_ACTIVE_CODE="1")
    result = subprocess.run(okf_module_argv("build", str(bundle), "--target", "static", "--out", str(out),
                                            "--assets", "offline"),
                            capture_output=True, text=True, env=env, timeout=120)
    assert result.returncode == 2, result.stderr
    assert "active code" in result.stderr
    assert not out.exists()


def test_cli_offline_build_and_render_succeed(tmp_path):
    bundle = write_offline_fixture(tmp_path / "bundle")
    built = subprocess.run(okf_module_argv("build", str(bundle), "--target", "static", "--out",
                                           str(tmp_path / "site"), "--assets", "offline"),
                           capture_output=True, text=True, env=okf_subprocess_env(), timeout=180)
    assert built.returncode == 0, built.stderr
    assert (tmp_path / "site" / "__static" / "vendor" / vendor.NOTICES_NAME).is_file()
    assert (tmp_path / "site" / "__static" / "vendor" / vendor.BUNDLED_NOTICES_NAME).is_file()
    rendered = subprocess.run(okf_module_argv("render", str(bundle), "--out", str(tmp_path / "one.html"),
                                              "--assets", "offline"),
                              capture_output=True, text=True, env=okf_subprocess_env(), timeout=180)
    assert rendered.returncode == 0, rendered.stderr
    assert 'id="okf-asset-bootstrap"' in (tmp_path / "one.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Payload closure and licences (no browser)
# ---------------------------------------------------------------------------

_RESOURCE_REF = re.compile(r'<(?:script|img|source|video|audio|iframe|embed)\b[^>]*\ssrc="([^"]*)"'
                           r'|<link\b[^>]*\shref="([^"]*)"')


@pytest.mark.parametrize("target", ["static", "spa"])
def test_offline_site_is_closed_and_licensed(tmp_path, target):
    bundle = Bundle.load(write_offline_fixture(tmp_path / "bundle"))
    out = tmp_path / "out"
    build_site(bundle, out, target=target, assets="offline")
    vendor_out = out / "__static" / "vendor"
    for name in (vendor.NOTICES_NAME, vendor.BUNDLED_NOTICES_NAME):
        assert (vendor_out / name).read_bytes() == (vendor.VENDOR_ROOT / name).read_bytes()
    licences = sorted(p.relative_to(vendor_out).as_posix() for p in vendor_out.rglob("LICENSE"))
    assert len(licences) == 11
    for page in _html_pages(out):
        html = page.read_text(encoding="utf-8")
        assert _csp(html) == [vendor.OFFLINE_PAGE_CSP], page
        assert "cdn.jsdelivr.net" not in html.split("<body", 1)[0], page
        assert html.count('id="okf-asset-bootstrap"') == 1
        for match in _RESOURCE_REF.finditer(html):
            ref = match.group(1) or match.group(2)
            if ref.startswith("data:"):
                continue
            assert not re.match(r"^[a-z][a-z0-9+.-]*:|^//", ref, re.I), (page, ref)
            base = out if ref.startswith("/") else page.parent
            assert (base / ref.lstrip("/")).is_file(), (page, ref)
    graph = (out / "__graph.html").read_text(encoding="utf-8")
    prefix = "/__static" if target == "spa" else "__static"
    chain = re.findall(r'<script defer src="([^"]+/vendor/[^"]+)"></script>', graph)
    assert chain == [f"{prefix}/vendor/{rel}" for rel in (
        "cytoscape-3.34.3/cytoscape.min.js", "layout-base-2.0.1/layout-base.js",
        "cose-base-2.2.0/cose-base.js", "cytoscape-fcose-2.2.0/cytoscape-fcose.js",
        "dagre-0.8.5/dagre.min.js", "cytoscape-dagre-2.5.0/cytoscape-dagre.js")]
    for css in vendor_out.rglob("*.css"):
        text = css.read_text(encoding="utf-8")
        assert all(ref.startswith("data:") for ref in re.findall(r"url\(([^)]*)\)", text)), css
    assert "integrity=" not in graph


def test_offline_single_file_is_one_closed_file_with_needed_carriers(tmp_path):
    rich = Bundle.load(write_offline_fixture(tmp_path / "rich"))
    plain = Bundle.load(write_offline_fixture(tmp_path / "plain", rich=False))
    build_site(rich, tmp_path / "rich-out", target="single-file", assets="offline")
    build_site(plain, tmp_path / "plain-out", target="single-file", assets="offline")
    assert [p.name for p in (tmp_path / "rich-out").iterdir()] == ["index.html"]
    rich_html = (tmp_path / "rich-out" / "index.html").read_text(encoding="utf-8")
    plain_html = (tmp_path / "plain-out" / "index.html").read_text(encoding="utf-8")
    for html in (rich_html, plain_html):
        assert _csp(html) == [vendor.OFFLINE_SINGLE_FILE_CSP]
        assert not re.search(r'<(?:script|link)\b[^>]*\s(?:src|href)="(?!data:)', html)
        assert html.count('id="okf-asset-bootstrap"') == 1
        for index in range(6):
            assert f'id="okf-vendor-graph-{index}"' in html
        notices = re.search(r'<script type="text/plain" id="okf-third-party-notices">(.*?)</script>', html, re.S)
        assert notices and "mermaid@11.17.2" in notices.group(1) and "BSD-3-Clause" in notices.group(1)
        bundled = (vendor.VENDOR_ROOT / vendor.BUNDLED_NOTICES_NAME).read_text(encoding="utf-8")
        assert f"===== {vendor.BUNDLED_NOTICES_NAME} =====\n{bundled.rstrip()}\n" in notices.group(1)
    renderer_carriers = [carrier for _path, carrier in vendor.RENDERER_ASSETS.values()]
    assert all(f'id="{carrier}"' in rich_html for carrier in renderer_carriers)
    assert not any(f'id="{carrier}"' in plain_html for carrier in renderer_carriers)


def test_single_file_offline_takes_precedence_over_config_cdn_false(tmp_path):
    root = write_offline_fixture(tmp_path / "bundle")
    (root / ".okf-loom" / "viewer").mkdir(parents=True)
    (root / ".okf-loom" / "viewer" / "config.json").write_text('{"cdn": false}', encoding="utf-8")
    bundle = Bundle.load(root)
    build_site(bundle, tmp_path / "out", target="single-file", assets="offline")
    html = (tmp_path / "out" / "index.html").read_text(encoding="utf-8")
    assert 'id="okf-vendor-graph-0"' in html and "config.cdn=false" not in html


# ---------------------------------------------------------------------------
# Bundled-dependency notices: full licence texts of the code INSIDE the
# vendored files (generated by scripts/generate_bundled_notices.py; its
# network-backed --check is the upstream proof, these are the offline ones)
# ---------------------------------------------------------------------------

INVENTORY = TOOLKIT_ROOT / "scripts" / "bundled_notices_inventory.json"
_BLOCK = re.compile(r"^(PACKAGE|SNIPPET|FONTS) (\S+)\n=+\n(.*?)^END \1 \2$", re.M | re.S)
_QUOTE = re.compile(r"^--- ([^\n]+) \(sha256 ([0-9a-f]{64})\) ---\n\n(.*?)(?=\n\n--- |\n\Z)", re.M | re.S)


def _bundled_notices() -> tuple[str, dict[tuple[str, str], str], dict]:
    text = (vendor.VENDOR_ROOT / vendor.BUNDLED_NOTICES_NAME).read_text(encoding="utf-8")
    blocks = {(kind, ident): body for kind, ident, body in _BLOCK.findall(text)}
    return text, blocks, json.loads(INVENTORY.read_text(encoding="utf-8"))


def test_bundled_notices_are_locked_and_embeddable():
    lock = vendor.load_lock()
    assert vendor.REQUIRED_AUXILIARY[vendor.BUNDLED_NOTICES_NAME] == "notices"
    entry = next(e for e in vendor.auxiliary_files(lock) if e["dest"] == vendor.BUNDLED_NOTICES_NAME)
    data = (vendor.VENDOR_ROOT / vendor.BUNDLED_NOTICES_NAME).read_bytes()
    assert (entry["sha256"], entry["size"]) == (vendor.sha256_hex(data), len(data))
    text = data.decode("utf-8")
    assert "\r" not in text and not re.search(r"[ \t]$", text, re.M)
    assert "</script" not in text.lower() and "<!--" not in text


def test_ledger_notice_generator_reproduces_the_locked_ofl_appendices():
    import generate_bundled_notices as generator

    entries = generator.ledger_fonts_entries(vendor.load_lock())
    notices, _blocks, inventory = _bundled_notices()
    assert inventory["ledger_fonts"] == [{k: v for k, v in entry.items() if k != "text"}
                                        for entry in entries]
    assert [entry["name"] for entry in entries] == ["Public Sans", "Azeret Mono"]
    # Minimal renderer inputs isolate the appendix renderer without network use.
    package = {"id": "fixture@1", "name": "fixture", "license": "MIT", "included_in": [],
               "evidence": [], "tarball": None, "licence_files": []}
    fonts = {"id": "fixture-fonts@1", "license": "OFL-1.1", "included_in": [], "evidence": [],
             "tarball": "fixture", "integrity": "fixture", "notices": [],
             "terms_source": "fixture", "terms_text": "fixture"}
    rendered = generator.render_notices([package], [], fonts, {}, entries)
    marker = "===== Ledger font: Public Sans (OFL-1.1) ====="
    appendix = rendered[rendered.index(marker):]
    assert appendix == notices[notices.index(marker):]
    primary = vendor.read_vendor_bytes(vendor.NOTICES_NAME).decode("utf-8")
    assert appendix == primary[primary.index(marker):]
    for entry in entries:
        assert entry["version"] == "ledger-snapshot" and entry["provenance"]
        assert "tarball" not in entry and "integrity" not in entry
        licence = entry["licence_files"][0]
        assert vendor.sha256_hex(entry["text"].encode("utf-8")) == licence["sha256"]
        assert vendor.sha256_hex(generator.as_quoted(entry["text"]).encode("utf-8")) == \
            licence["quoted_sha256"]


@pytest.mark.parametrize("damage", ["flip", "missing", "symlink"])
def test_notice_regeneration_refuses_damaged_local_licences_before_network(vendor_copy, monkeypatch,
                                                                        tmp_path, damage):
    import generate_bundled_notices as generator

    victim = vendor_copy / vendor.LEDGER_LICENSES[0]
    if damage == "flip":
        _flip_byte(victim)
    elif damage == "missing":
        victim.unlink()
    else:
        real = tmp_path / "licence.txt"
        victim.rename(real)
        victim.symlink_to(real)

    def no_network():
        pytest.fail("local licence damage must fail before upstream regeneration")

    monkeypatch.setattr(generator, "mermaid_inventory", no_network)
    with pytest.raises(SystemExit, match="local Ledger snapshot verification failed"):
        generator.build()


def test_bundled_notices_quote_every_inventoried_licence_in_full():
    text, blocks, inventory = _bundled_notices()
    packages = [ident for kind, ident in blocks if kind == "PACKAGE"]
    assert packages == [entry["id"] for entry in inventory["packages"]]
    assert [ident for kind, ident in blocks if kind == "SNIPPET"] == [s["id"] for s in inventory["snippets"]]
    assert f"Inventory: {len(packages)} packages, {len(inventory['snippets'])} adapted snippets, 1 font family." \
        in text
    assert len(packages) >= 80
    for entry in inventory["packages"]:
        body = blocks[("PACKAGE", entry["id"])]
        quotes = _QUOTE.findall(body)
        assert [(path, sha) for path, sha, _ in quotes] == \
            [(f["path"], f["sha256"]) for f in entry["licence_files"]], entry["id"]
        for (_, _, quoted), recorded in zip(quotes, entry["licence_files"]):
            assert len(quoted.strip()) > 100, entry["id"]
            assert vendor.sha256_hex(quoted.encode("utf-8")) == recorded["quoted_sha256"], entry["id"]
        assert f"Included in: {', '.join(entry['included_in'])}\n" in body
        assert entry["evidence"] and all(f"Evidence: {line}\n" in body for line in entry["evidence"])
    dompurify = blocks[("PACKAGE", "dompurify@3.4.12")]
    assert "Redistributed under: Apache-2.0" in dompurify
    assert "Apache License\n                           Version 2.0, January 2004" in dompurify
    assert "END OF TERMS AND CONDITIONS" in dompurify and "(c) Cure53 and other contributors" in dompurify
    for snippet in inventory["snippets"]:
        body = blocks[("SNIPPET", snippet["id"])]
        banner = "\n".join(line.rstrip(" \t") for line in snippet["banner"].replace("\r\n", "\n").split("\n"))
        assert f"--- upstream attribution (verbatim) ---\n\n{banner}\n" in body
        if snippet.get("gap"):
            assert "Licence: NOT ESTABLISHED" in body
            assert {"id": snippet["id"], "reason": snippet["gap"]} in inventory["gaps"]
        else:
            terms = body.split(f"--- licence terms: {snippet['license']} (", 1)[1]
            assert len(terms.strip()) > 300, snippet["id"]
    fonts = blocks[("FONTS", inventory["fonts"]["id"])]
    assert "Licence: OFL-1.1" in fonts and "SIL OPEN FONT LICENSE\n\nVersion 1.1 - 26 February 2007" in fonts
    assert "5) The Font Software, modified or unmodified, in part or in whole," in fonts
    assert "This Font Software is licensed under the SIL Open Font License, Version 1.1." in fonts
    notices = (vendor.VENDOR_ROOT / vendor.NOTICES_NAME).read_text(encoding="utf-8")
    assert "SIL Open Font" in notices and vendor.BUNDLED_NOTICES_NAME in notices


def test_bundled_notices_agree_with_versions_in_the_shipped_bytes():
    _, _, inventory = _bundled_notices()
    included = {entry["id"]: entry["included_in"] for entry in inventory["packages"]}
    lock = vendor.load_lock()
    shipped = {entry["dest"] for entry in vendor.lock_files(lock)}
    for places in included.values():
        assert places and all(place.split(" (", 1)[0] in shipped for place in places), places
    mermaid = vendor.read_vendor_bytes("mermaid-11.17.2/mermaid.min.js")
    cytoscape = vendor.read_vendor_bytes("cytoscape-3.34.3/cytoscape.min.js")
    dagre = vendor.read_vendor_bytes("dagre-0.8.5/dagre.min.js")
    # Version strings the minified bytes themselves carry.
    assert b"@license DOMPurify 3.4.12 " in mermaid
    assert "mermaid-11.17.2/mermaid.min.js" in included["dompurify@3.4.12"]
    assert mermaid.count(b'="3.34.0"') == 1 and "mermaid-11.17.2/mermaid.min.js" in included["cytoscape@3.34.0"]
    assert b'module.exports="2.1.8"' in dagre and "dagre-0.8.5/dagre.min.js" in included["graphlib@2.1.8"]
    assert b'.version="3.34.3"' in cytoscape
    for inner in ("heap@0.2.7", "lodash@4.18.1", "gl-matrix@3.4.3"):
        assert "cytoscape-3.34.3/cytoscape.min.js" in included[inner], inner
    assert "dagre-0.8.5/dagre.min.js" in included["lodash@4.17.15"]
    assert inventory["mermaid"]["release"] == {"tag": "mermaid@11.17.2",
                                              "commit": "dcb694ddb58dc5ad3502e7e903cac05fd812eac3"}


def test_cdn_render_golden_provenance_matches_the_pinned_versions():
    """The browser parity golden was rendered from exactly the pinned CDN
    versions; a version bump without regenerating it fails here."""
    golden = json.loads((TOOLKIT_ROOT / "tests" / "fixtures" / "offline_parity" / "cdn_render_golden.json")
                        .read_text(encoding="utf-8"))
    provenance = golden["provenance"]
    versions = {p["name"]: p["version"] for p in vendor.load_lock()["packages"]}
    assert provenance["versions"] == {"highlight.js": versions["@highlightjs/cdn-assets"],
                                      "katex": versions["katex"], "mermaid": versions["mermaid"]}
    renderers = (TOOLKIT_ROOT / "scripts" / "okf_loom" / "viewer" / "static" / "renderers.js").read_text(encoding="utf-8")
    pins = set(re.findall(r'"(https://cdn\.jsdelivr\.net/npm/[^"]+)"', renderers))
    # GitHub sheets remain locked for vendor parity but neither is loaded:
    # Ledger paints syntax locally from palette tokens in every scheme.
    retired_sheets = {url for url in pins if re.search(r"/styles/github(?:-dark)?\.min\.css$", url)}
    active_pins = pins - retired_sheets
    requests = set(provenance["cdn_requests"])
    assert active_pins and active_pins <= requests
    assert not retired_sheets & requests
    assert all(url.startswith("https://cdn.jsdelivr.net/npm/") for url in provenance["cdn_requests"])
    assert provenance["generated"] and provenance["browser"].startswith("Chromium ")
    assert len(golden["mermaid"]) == 2 and len(golden["hljs"]) == 2 and len(golden["katex"]) == 1


@pytest.mark.parametrize("damage", ["flip", "missing", "unlisted"])
def test_bundled_notices_tampering_fails_closed(vendor_copy, damage):
    victim = vendor_copy / vendor.BUNDLED_NOTICES_NAME
    if damage == "flip":
        _flip_byte(victim)
    elif damage == "missing":
        victim.unlink()
    else:
        lock_path = vendor_copy / vendor.LOCK_NAME
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["auxiliary"] = [e for e in lock["auxiliary"] if e["dest"] != vendor.BUNDLED_NOTICES_NAME]
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(vendor.VendorIntegrityError):
        vendor.load_offline_assets()


@pytest.mark.parametrize("target", ["static", "spa", "single-file"])
@pytest.mark.parametrize("assets", ["cdn", "offline"])
def test_ledger_fonts_and_full_ofl_ship_on_every_target(tmp_path, target, assets):
    bundle = Bundle.load(write_offline_fixture(tmp_path / "bundle"))
    out = tmp_path / "site"
    build_site(bundle, out, target=target, assets=assets)
    text = ((out / "index.html") if target == "single-file" else
            (out / "__static" / "ledger-fonts.css")).read_text()
    if target == "single-file":
        assert text.count('src:url(data:font/woff2;base64,') >= 4
        assert not list(out.rglob("*.woff2"))
        assert 'as="font"' not in text
    else:
        assert "data:font" not in text
        assert len(text.encode()) < 10_000
        for _family, _style, path in vendor.LEDGER_FACES:
            assert f'src:url({path}) format("woff2")' in text
            assert (out / "__static" / path).read_bytes() == vendor.read_vendor_bytes(path)
        for path in vendor.LEDGER_LICENSES:
            assert (out / "__static" / path).read_bytes() == vendor.read_vendor_bytes(path)
        for page in out.rglob("*.html"):
            html = page.read_text()
            if target == "spa":
                assert html.count('as="font" type="font/woff2" crossorigin') == 2, page
                assert html.index('rel="preload"') < html.index('rel="stylesheet"')
            else:
                assert 'as="font"' not in html, page
    for family, style, _path in vendor.LEDGER_FACES:
        assert f'font-family:"{family}";font-style:{style};' in text
    assert text.count('font-display:swap;') >= 4
    for path in vendor.LEDGER_LICENSES:
        assert (vendor.VENDOR_ROOT / path).read_text() in text
    lock = vendor.load_lock()
    css = vendor.load_ledger_css(inline=True).encode()
    pin = lock["derived"][vendor.LEDGER_DERIVED]
    assert pin["sha256"] == vendor.sha256_hex(css)
    assert pin["external"]["sha256"] == vendor.sha256_hex(vendor.load_ledger_css().encode())


@pytest.mark.parametrize("path", [face[2] for face in vendor.LEDGER_FACES] + list(vendor.LEDGER_LICENSES))
def test_ledger_corruption_fails_closed_even_in_cdn_mode(vendor_copy, path):
    target = vendor_copy / path
    target.write_bytes(target.read_bytes() + b"corrupt")
    with pytest.raises(vendor.VendorIntegrityError, match="mismatch"):
        vendor.load_ledger_css(vendor_copy)


@pytest.mark.parametrize("variant", ["inline", "external"])
def test_ledger_derived_hash_and_symlinks_are_verified(vendor_copy, variant):
    lock_path = vendor_copy / vendor.LOCK_NAME
    lock = json.loads(lock_path.read_text())
    pin = lock["derived"][vendor.LEDGER_DERIVED]
    if variant == "external":
        pin = pin["external"]
    pin["sha256"] = "0" * 64
    lock_path.write_text(json.dumps(lock))
    with pytest.raises(vendor.VendorIntegrityError, match="derived Ledger"):
        vendor.load_ledger_css(vendor_copy)
    path = vendor_copy / vendor.LEDGER_FACES[0][2]
    path.unlink()
    path.symlink_to(vendor.VENDOR_ROOT / vendor.LEDGER_FACES[0][2])
    with pytest.raises(vendor.VendorIntegrityError, match="symlink"):
        vendor.load_ledger_css(vendor_copy)
