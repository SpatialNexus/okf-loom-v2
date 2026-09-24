"""X1 security-review hardening (no browser, no network).

Vendor-root authority (no-follow root and descendants), the locked auxiliary
(notices) manifest, bounded archive processing and registry-only redirects in
the maintainer fetcher, the explicit-plugin refusal in offline builds, and
the reserved-id markup a default CDN build must not treat as configuration.
"""
from __future__ import annotations

import gzip
import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import tarfile
import threading
from pathlib import Path

import pytest

from conftest import TOOLKIT_ROOT
from okf_loom import cli
from okf_loom.model import Bundle
from okf_loom.render import build_site
from okf_loom.viewer import vendor
from test_offline_assets import _tree, write_offline_fixture

MERMAID = "mermaid-11.17.2/mermaid.min.js"


def _load_fetcher():
    spec = importlib.util.spec_from_file_location(
        "fetch_vendor_assets_under_test", TOOLKIT_ROOT / "scripts" / "fetch_vendor_assets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fva = _load_fetcher()


def _relock(tree: Path, rel: str, *, auxiliary: bool = False, role: str = "notices") -> None:
    """Rewrite the lock entry of ``rel`` so it matches the bytes now on disk."""
    lock_path = tree / vendor.LOCK_NAME
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    data = (tree / rel).read_bytes()
    fields = {"size": len(data), "sha256": vendor.sha256_hex(data), "sha384": vendor.sri_sha384(data)}
    entries = lock["auxiliary"] if auxiliary else vendor.lock_files(lock)
    for entry in entries:
        if entry["dest"] == rel:
            entry.update(fields)
            break
    else:
        assert auxiliary, rel
        lock["auxiliary"].append({"dest": rel, "role": role, **fields})
    lock_path.write_text(json.dumps(lock, indent=1, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture
def substituted_tree(tmp_path) -> Path:
    """A self-consistent vendor tree whose Mermaid (no code SRI) was replaced."""
    tree = tmp_path / "substituted-vendor"
    shutil.copytree(vendor.VENDOR_ROOT, tree)
    mermaid = tree / MERMAID
    mermaid.write_bytes(mermaid.read_bytes() + b"\n;window.__okfSubstitutedMermaid=1;\n")
    _relock(tree, MERMAID)
    return tree


def _cli(capsys, verb: str, bundle: Path, out: Path) -> tuple[int, str]:
    argv = [verb, str(bundle), "--out", str(out), "--assets", "offline"]
    if verb == "build":
        argv[2:2] = ["--target", "static"]
    code = cli.main(argv)
    return code, capsys.readouterr().err


def _previous_output(tmp_path, verb: str, bundle: Path) -> Path:
    """Existing (default CDN) output that a failed offline run must keep."""
    if verb == "build":
        out = tmp_path / "site"
        build_site(Bundle.load(bundle), out, target="static")
    else:
        out = tmp_path / "one.html"
        assert cli.main(["render", str(bundle), "--out", str(out)]) == 0
    return out


def _snapshot(out: Path):
    return _tree(out) if out.is_dir() else out.read_bytes()


# ---------------------------------------------------------------------------
# Finding 1: the vendor root and every descendant are no-follow
# ---------------------------------------------------------------------------

def test_substituted_tree_is_self_consistent_as_a_real_directory(substituted_tree):
    """Positive control: only the symlink refusal stops the tests below."""
    assets = vendor.load_offline_assets(substituted_tree)
    assert b"__okfSubstitutedMermaid" in assets.files[MERMAID]


@pytest.mark.parametrize("verb", ["build", "render"])
def test_symlinked_vendor_root_is_refused(tmp_path, monkeypatch, capsys, substituted_tree, verb):
    link = tmp_path / "vendor-link"
    link.symlink_to(substituted_tree, target_is_directory=True)
    monkeypatch.setattr(vendor, "VENDOR_ROOT", link)
    bundle = write_offline_fixture(tmp_path / "bundle")
    out = _previous_output(tmp_path, verb, bundle)
    capsys.readouterr()
    before = _snapshot(out)
    code, err = _cli(capsys, verb, bundle, out)
    assert code == 1, err
    assert "vendor folder is a symlink" in err
    assert _snapshot(out) == before
    with pytest.raises(vendor.VendorIntegrityError, match="vendor folder is a symlink"):
        vendor.load_lock()


@pytest.mark.parametrize("verb", ["build", "render"])
def test_symlinked_intermediate_directory_is_refused(tmp_path, monkeypatch, capsys, substituted_tree, verb):
    root = tmp_path / "vendor-copy"
    shutil.copytree(vendor.VENDOR_ROOT, root)
    shutil.rmtree(root / "mermaid-11.17.2")
    (root / "mermaid-11.17.2").symlink_to(substituted_tree / "mermaid-11.17.2", target_is_directory=True)
    _relock(root, MERMAID)  # the lock now matches the substituted Mermaid
    monkeypatch.setattr(vendor, "VENDOR_ROOT", root)
    bundle = write_offline_fixture(tmp_path / "bundle")
    out = _previous_output(tmp_path, verb, bundle)
    capsys.readouterr()
    before = _snapshot(out)
    code, err = _cli(capsys, verb, bundle, out)
    assert code == 1, err
    assert "vendored path is a symlink: mermaid-11.17.2/" in err
    assert _snapshot(out) == before


@pytest.mark.parametrize("kind", ["file", "missing"])
def test_non_directory_vendor_root_is_refused(tmp_path, kind):
    root = tmp_path / "not-a-dir"
    if kind == "file":
        root.write_text("{}", encoding="utf-8")
    with pytest.raises(vendor.VendorIntegrityError, match="vendor folder (is not a directory|missing)"):
        vendor.load_offline_assets(root)


def test_check_mode_refuses_a_symlinked_dest(tmp_path, capsys, substituted_tree):
    link = tmp_path / "vendor-link"
    link.symlink_to(substituted_tree, target_is_directory=True)
    assert fva.main(["--check", "--dest", str(link)]) == 1
    assert "vendor folder is a symlink" in capsys.readouterr().err
    assert fva.main(["--check", "--dest", str(substituted_tree)]) == 0


@pytest.mark.skipif(not vendor._DIR_FD_READS, reason="descriptor-relative reads need dir_fd support")
def test_reads_stay_pinned_to_the_opened_root_across_a_swap(tmp_path, substituted_tree):
    root = tmp_path / "vendor-root"
    shutil.copytree(vendor.VENDOR_ROOT, root)
    original = (root / MERMAID).read_bytes()
    with vendor.VendorTree(root) as tree:
        root.rename(tmp_path / "moved-away")
        root.symlink_to(substituted_tree, target_is_directory=True)
        assert tree.read(MERMAID) == original
        assets = vendor._load_offline_assets(tree)
    assert assets.files[MERMAID] == original


# ---------------------------------------------------------------------------
# Finding 5: notices are locked auxiliary files
# ---------------------------------------------------------------------------

def test_lock_carries_the_required_auxiliary_manifest():
    lock = vendor.load_lock()
    entries = {entry["dest"]: entry for entry in vendor.auxiliary_files(lock)}
    assert entries[vendor.NOTICES_NAME]["role"] == "notices"
    data = (vendor.VENDOR_ROOT / vendor.NOTICES_NAME).read_bytes()
    assert entries[vendor.NOTICES_NAME]["sha256"] == vendor.sha256_hex(data)


@pytest.fixture
def offline_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "vendor-copy"
    shutil.copytree(vendor.VENDOR_ROOT, root)
    monkeypatch.setattr(vendor, "VENDOR_ROOT", root)
    return root


def _damage_notices(root: Path, damage: str) -> None:
    notices = root / vendor.NOTICES_NAME
    if damage == "flip":
        data = bytearray(notices.read_bytes())
        data[len(data) // 2] ^= 0x01
        notices.write_bytes(bytes(data))
    elif damage == "delete-byte":
        data = notices.read_bytes()
        notices.write_bytes(data[:10] + data[11:])
    elif damage == "unlocked":
        lock_path = root / vendor.LOCK_NAME
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["auxiliary"] = []
        lock_path.write_text(json.dumps(lock), encoding="utf-8")


@pytest.mark.parametrize("damage", ["flip", "delete-byte", "unlocked"])
@pytest.mark.parametrize("verb", ["check", "build", "render"])
def test_damaged_notices_fail_check_build_and_render(tmp_path, capsys, offline_root, damage, verb):
    bundle = write_offline_fixture(tmp_path / "bundle")
    if verb == "check":
        _damage_notices(offline_root, damage)
        assert fva.main(["--check", "--dest", str(offline_root)]) == 1
        err = capsys.readouterr().err
        assert "vendor check FAILED" in err
    else:
        # Previous output is a good OFFLINE build from the intact tree.
        out = tmp_path / ("site" if verb == "build" else "one.html")
        code, err = _cli(capsys, verb, bundle, out)
        assert code == 0, err
        before = _snapshot(out)
        _damage_notices(offline_root, damage)
        code, err = _cli(capsys, verb, bundle, out)
        assert code == 1, err
        assert "offline asset integrity check failed" in err
        assert _snapshot(out) == before
    expected = "does not lock the auxiliary file" if damage == "unlocked" else \
        "THIRD-PARTY-NOTICES.txt"
    assert expected in err


def test_further_locked_notice_files_are_verified_and_published(tmp_path, offline_root):
    extra = "katex-0.16.47/BUNDLED-NOTICES.txt"
    (offline_root / extra).write_text("Bundled dependency notices (test).\n", encoding="utf-8")
    _relock(offline_root, extra, auxiliary=True)
    bundle = Bundle.load(write_offline_fixture(tmp_path / "bundle"))
    build_site(bundle, tmp_path / "site", target="static", assets="offline")
    assert (tmp_path / "site" / "__static" / "vendor" / extra).read_bytes() == (offline_root / extra).read_bytes()
    build_site(bundle, tmp_path / "one", target="single-file", assets="offline")
    html = (tmp_path / "one" / "index.html").read_text(encoding="utf-8")
    block = re.search(r'id="okf-third-party-notices">(.*?)</script>', html, re.S).group(1)
    assert f"===== {extra} =====\nBundled dependency notices (test)." in block
    # ... and it is integrity-checked like the primary notices file.
    (offline_root / extra).write_text("Bundled dependency notices (tampered).\n", encoding="utf-8")
    with pytest.raises(vendor.VendorIntegrityError, match="BUNDLED-NOTICES"):
        vendor.load_offline_assets()


@pytest.mark.parametrize("entry", [
    {"dest": "../escape.txt", "role": "notices"},
    {"dest": vendor.NOTICES_NAME, "role": "script"},
    {"dest": MERMAID, "role": "notices"},
    "not-an-object",
])
def test_malformed_auxiliary_entries_fail_closed(offline_root, entry):
    lock_path = offline_root / vendor.LOCK_NAME
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if isinstance(entry, dict) and entry["dest"] == vendor.NOTICES_NAME:
        lock["auxiliary"][0]["role"] = entry["role"]
    else:
        lock["auxiliary"].append(entry)
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(vendor.VendorIntegrityError):
        vendor.load_offline_assets()


# ---------------------------------------------------------------------------
# Finding 4: bounded archive processing, registry-only redirects
# ---------------------------------------------------------------------------

def _tgz(members, *, declared: dict[str, int] | None = None) -> bytes:
    """gzip'd tar; ``declared`` writes a header claiming a size with no body."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    body = raw.getvalue()
    if declared:
        headers = b""
        for name, size in declared.items():
            info = tarfile.TarInfo(name)
            info.size = size
            headers += info.tobuf(format=tarfile.USTAR_FORMAT)
        body = body.rstrip(b"\0") + b"\0" * (-len(body.rstrip(b"\0")) % 512) + headers
    return gzip.compress(body, mtime=0)


def _package(name: str, blob: bytes, files: dict[str, bytes]) -> dict:
    return {"name": name, "version": "1.0.0",
            "tarball": f"{fva.REGISTRY}/{name}/-/{name}-1.0.0.tgz", "integrity": fva._sri512(blob),
            "files": [{"src": src, "dest": f"{name}-1.0.0/{src.rsplit('/', 1)[1]}", "size": len(data),
                       "sha256": vendor.sha256_hex(data), "sha384": vendor.sri_sha384(data)}
                      for src, data in files.items()]}


@pytest.fixture
def fetch_env(tmp_path, monkeypatch):
    """A synthetic lock + in-memory 'registry'; any real network use fails."""
    lock_root = tmp_path / "lock-root"
    lock_root.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    blobs: dict[str, bytes] = {}
    monkeypatch.setattr(vendor, "VENDOR_ROOT", lock_root)
    monkeypatch.setattr(fva, "_fetch", lambda url, limit=fva.MAX_TARBALL_BYTES: blobs[url])

    def install(*packages: tuple[str, bytes, dict[str, bytes]]):
        lock = {"schema": 1, "packages": [], "auxiliary": []}
        for name, blob, files in packages:
            package = _package(name, blob, files)
            blobs[package["tarball"]] = blob
            lock["packages"].append(package)
        (lock_root / vendor.LOCK_NAME).write_text(json.dumps(lock), encoding="utf-8")
    return install, dest


GOOD = b"console.log('selected');\n"


def test_fetch_positive_control_writes_only_selected_files(fetch_env):
    install, dest = fetch_env
    blob = _tgz([("package/a.js", GOOD), ("package/unselected.js", b"x" * 100)])
    install(("good", blob, {"package/a.js": GOOD}))
    fva.fetch(dest)
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == ["good-1.0.0/a.js"]
    assert (dest / "good-1.0.0" / "a.js").read_bytes() == GOOD


@pytest.mark.parametrize("case, message", [
    ("oversized-selected", "exceeds 1024 bytes: 'package/a.js'"),
    ("oversized-unselected", "exceeds 1024 bytes: 'package/big.map'"),
    ("too-many", "more than 20 members"),
    ("expansion", "expands past 4096 bytes"),
    ("duplicate", "duplicate tar member: 'package/a.js'"),
    ("unsafe", "unsafe tar member path"),
])
def test_archive_bounds_fail_before_any_write(fetch_env, monkeypatch, case, message):
    monkeypatch.setattr(fva, "MAX_MEMBER_BYTES", 1024)
    monkeypatch.setattr(fva, "MAX_MEMBERS", 20)
    monkeypatch.setattr(fva, "MAX_EXPANDED_BYTES", 4096)
    install, dest = fetch_env
    selected = {"package/a.js": GOOD}
    members = [("package/a.js", GOOD)]
    if case == "oversized-selected":
        selected = {"package/a.js": b"y" * 2048}
        members = [("package/a.js", b"y" * 2048)]
    elif case == "oversized-unselected":
        members.append(("package/big.map", b"z" * 2048))
    elif case == "too-many":
        members += [(f"package/f{i}.js", b"") for i in range(30)]
    elif case == "expansion":
        members += [(f"package/f{i}.js", b"w" * 1000) for i in range(6)]
    elif case == "duplicate":
        members.append(("package/a.js", GOOD))
    elif case == "unsafe":
        members.append(("package/../../evil.js", b"evil"))
    # A valid first package proves nothing is written until all verified.
    first = _tgz([("package/a.js", GOOD)])
    install(("first", first, {"package/a.js": GOOD}), ("bad", _tgz(members), selected))
    with pytest.raises(SystemExit, match=re.escape(message)):
        fva.fetch(dest)
    assert list(dest.iterdir()) == []


def test_declared_huge_member_is_refused_from_its_header_with_default_limits(fetch_env):
    install, dest = fetch_env
    # The header claims 1 GiB but no body follows: refusal must come from the
    # header, before any body is read, skipped or allocated.
    blob = _tgz([("package/a.js", GOOD)], declared={"package/huge.bin": 1 << 30})
    assert len(blob) < 4096
    install(("huge", blob, {"package/a.js": GOOD}))
    with pytest.raises(SystemExit, match="exceeds 67108864 bytes: 'package/huge.bin'"):
        fva.fetch(dest)
    assert list(dest.iterdir()) == []


def test_decompressed_stream_is_capped():
    bomb = gzip.compress(b"\0" * (1 << 20), mtime=0)
    with pytest.raises(SystemExit, match="archive expands past"):
        fva._tar_members(bomb, None, max_members=1, max_member_bytes=10, max_expanded_bytes=10)


class _RedirectServer(http.server.ThreadingHTTPServer):
    hits: list[str]


def _redirect_handler(foreign_port: int):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.server.hits.append(self.path)
            port = self.server.server_address[1]
            if self.path == "/pkg/same-host":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port}/pkg/ok")
            elif self.path == "/pkg/foreign":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{foreign_port}/pkg/ok")
            elif self.path == "/pkg/ok":
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
                return
            else:
                self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass
    return Handler


@pytest.fixture
def registry_servers(monkeypatch):
    foreign = _RedirectServer(("127.0.0.1", 0), _redirect_handler(0))
    foreign.hits = []
    registry = _RedirectServer(("127.0.0.1", 0), _redirect_handler(foreign.server_address[1]))
    registry.hits = []
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (registry, foreign)]
    for thread in threads:
        thread.start()
    for key in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(fva, "REGISTRY", f"http://127.0.0.1:{registry.server_address[1]}")
    try:
        yield registry, foreign
    finally:
        for server in (registry, foreign):
            server.shutdown()
            server.server_close()


def test_redirects_stay_on_the_registry_host(registry_servers):
    registry, foreign = registry_servers
    assert fva._fetch(f"{fva.REGISTRY}/pkg/same-host") == b"ok"
    with pytest.raises(SystemExit, match="refusing redirect to a non-registry URL: http://localhost:"):
        fva._fetch(f"{fva.REGISTRY}/pkg/foreign")
    assert foreign.hits == []
    assert registry.hits == ["/pkg/same-host", "/pkg/ok", "/pkg/foreign"]


def test_non_registry_urls_are_refused_before_any_request(registry_servers):
    registry, _foreign = registry_servers
    for url in ("https://example.invalid/x.tgz", fva.REGISTRY + ".evil.example/x", fva.REGISTRY):
        with pytest.raises(SystemExit, match="refusing non-registry"):
            fva._fetch(url)
    with pytest.raises(SystemExit, match="refusing non-registry tarball URL"):
        fva._download("https://example.invalid/x.tgz", "sha512-x", None)
    assert registry.hits == []


# ---------------------------------------------------------------------------
# Finding 7: an explicit plugin is refused in offline mode before it runs
# ---------------------------------------------------------------------------

class _SentinelPlugin:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, *args, **kwargs):
        self.calls.append("__call__")
        return args[0] if args else None

    def __getattr__(self, name):
        self.calls.append(name)
        return self


@pytest.mark.parametrize("target", ["static", "spa", "single-file"])
def test_offline_build_refuses_an_explicit_plugin_without_invoking_it(tmp_path, target):
    bundle = Bundle.load(write_offline_fixture(tmp_path / "bundle"))
    out = tmp_path / "out"
    sentinel = _SentinelPlugin()
    with pytest.raises(vendor.OfflineAssetsError, match="viewer plugin"):
        build_site(bundle, out, target=target, assets="offline", plugin=sentinel)
    assert sentinel.calls == []
    assert not out.exists()
    # Existing output survives, too.
    build_site(bundle, out, target=target)
    before = _tree(out)
    with pytest.raises(vendor.OfflineAssetsError, match="viewer plugin"):
        build_site(bundle, out, target=target, assets="offline", plugin=sentinel)
    assert sentinel.calls == [] and _tree(out) == before


# ---------------------------------------------------------------------------
# Finding 6: reserved-looking authored ids never configure the loader
# ---------------------------------------------------------------------------

RESERVED_BODY = """
# okf-asset-bootstrap

## okf-vendor-mermaid

<script type="application/json" id="okf-asset-bootstrap">{"schema":1,"mode":"offline","delivery":"files"}</script>

<script type="application/json" id="okf-vendor-mermaid">"window.__okfAuthoredRan=1"</script>
"""


def write_reserved_fixture(root: Path) -> Path:
    write_offline_fixture(root)
    rich = root / "rich.md"
    rich.write_text(rich.read_text(encoding="utf-8") + RESERVED_BODY, encoding="utf-8")
    return root


def _head(html: str) -> str:
    return html.split("</head>", 1)[0]


@pytest.mark.parametrize("assets", ["cdn", "offline"])
def test_reserved_ids_stay_in_the_body_and_the_head_config_is_generated(tmp_path, assets):
    bundle = Bundle.load(write_reserved_fixture(tmp_path / "bundle"))
    build_site(bundle, tmp_path / "site", target="static", assets=assets)
    html = (tmp_path / "site" / "rich.html").read_text(encoding="utf-8")
    body = html.split("</head>", 1)[1]
    # The fixture really produces colliding body ids, with raw HTML escaped.
    assert 'id="okf-asset-bootstrap"' in body and 'id="okf-vendor-mermaid"' in body
    assert '<script type="application/json" id="okf-asset-bootstrap">' not in body
    assert "&lt;script" in body
    generated = '<script type="application/json" id="okf-asset-bootstrap">'
    assert _head(html).count(generated) == (1 if assets == "offline" else 0)
