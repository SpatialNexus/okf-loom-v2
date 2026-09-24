#!/usr/bin/env python3
"""Acquire and verify the vendored offline renderer/graph libraries (X1).

Maintainer tool; the okf-loom runtime never imports it. Standard library only.

Modes:

``--check`` (default when no mode is given; no network)
    Re-hash every file in ``scripts/okf_loom/viewer/vendor/`` against
    ``vendor.lock.json``, including the SRI cross-check against the code and the
    pinned derived outputs. Exit 0 when everything matches.

``--fetch`` (network: registry.npmjs.org only, redirects included)
    Download each locked npm tarball, verify its sha512 integrity, stream the
    archive under member-count, member-size and total-expansion limits
    (refusing links, device files, absolute paths, ``..`` and duplicate
    names), keep only the locked files, verify each file's
    size/SHA-256/SHA-384 and, once every package verified, write them
    atomically into ``--dest`` (default: the repo vendor folder). Auxiliary
    files (notices) are repository-authored and never fetched.

``--init-lock`` (network; maintainers bumping a pin)
    Build a fresh lock from the pins below: registry metadata supplies each
    tarball URL and sha512, every extracted file is hashed, every file that
    the CDN mode also loads must equal the SRI value already in the code, the
    highlight.js language parity list is read from ``highlight.js`` itself and
    the derived outputs are hashed, and the auxiliary files already present
    in ``--dest`` (``THIRD-PARTY-NOTICES.txt`` plus any other file of the
    current lock's ``auxiliary`` manifest) are re-hashed into the new lock.
    Regenerate and review the notices BEFORE running it. Writes the files and
    the lock to ``--dest``. Review the diff before committing.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zlib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from okf_loom.viewer import vendor  # noqa: E402

REGISTRY = "https://registry.npmjs.org"
CDN = "https://cdn.jsdelivr.net/npm"

# Exact pins. Paths are inside the npm tarball ("package/..."); destinations
# are below the vendor folder and must never contain "dist" or "build"
# (the release archive and .gitignore drop those names at any depth).
PINS = [
    {
        "name": "mermaid", "version": "11.17.2", "dir": "mermaid-11.17.2",
        "files": [("package/dist/mermaid.min.js", "mermaid.min.js", None),
                  ("package/LICENSE", "LICENSE", None)],
    },
    {
        "name": "katex", "version": "0.16.47", "dir": "katex-0.16.47",
        "files": [("package/dist/katex.min.js", "katex.min.js", "katex@0.16.47/dist/katex.min.js"),
                  ("package/dist/katex.min.css", "katex.min.css", "katex@0.16.47/dist/katex.min.css"),
                  ("package/LICENSE", "LICENSE", None)],
        "globs": [(r"package/dist/fonts/(KaTeX_[A-Za-z0-9-]+\.woff2)", "fonts/")],
    },
    {
        "name": "@highlightjs/cdn-assets", "version": "11.12.0", "dir": "highlightjs-11.12.0",
        "files": [("package/highlight.min.js", "highlight.min.js", None),
                  ("package/styles/github.min.css", "github.min.css",
                   "highlight.js@11.12.0/styles/github.min.css"),
                  ("package/styles/github-dark.min.css", "github-dark.min.css",
                   "highlight.js@11.12.0/styles/github-dark.min.css"),
                  ("package/LICENSE", "LICENSE", None)],
        "hljs_languages": True,
    },
    {
        "name": "cytoscape", "version": "3.34.3", "dir": "cytoscape-3.34.3",
        "files": [("package/dist/cytoscape.min.js", "cytoscape.min.js", "cytoscape@3.34.3/dist/cytoscape.min.js"),
                  ("package/LICENSE", "LICENSE", None)],
    },
    {
        "name": "layout-base", "version": "2.0.1", "dir": "layout-base-2.0.1",
        "files": [("package/layout-base.js", "layout-base.js", "layout-base@2.0.1/layout-base.js"),
                  ("package/LICENSE", "LICENSE", None)],
    },
    {
        "name": "cose-base", "version": "2.2.0", "dir": "cose-base-2.2.0",
        "files": [("package/cose-base.js", "cose-base.js", "cose-base@2.2.0/cose-base.js"),
                  ("package/LICENSE", "LICENSE", None)],
    },
    {
        "name": "cytoscape-fcose", "version": "2.2.0", "dir": "cytoscape-fcose-2.2.0",
        "files": [("package/cytoscape-fcose.js", "cytoscape-fcose.js", "cytoscape-fcose@2.2.0/cytoscape-fcose.js"),
                  ("package/LICENSE", "LICENSE", None)],
    },
    {
        "name": "dagre", "version": "0.8.5", "dir": "dagre-0.8.5",
        "files": [("package/dist/dagre.min.js", "dagre.min.js", "dagre@0.8.5/dist/dagre.min.js"),
                  ("package/LICENSE", "LICENSE", None)],
    },
    {
        "name": "cytoscape-dagre", "version": "2.5.0", "dir": "cytoscape-dagre-2.5.0",
        "files": [("package/cytoscape-dagre.js", "cytoscape-dagre.js", "cytoscape-dagre@2.5.0/cytoscape-dagre.js"),
                  ("package/LICENSE", "LICENSE", None)],
    },
]
# The CDN mode loads jsDelivr's +esm build of highlight.js@11.12.0, which
# registers every language of the package's full build (lib/index.js). The
# prebuilt cdn-assets highlight.min.js registers only lib/common.js; the rest
# are vendored as the project's own separately compiled language files.
HLJS_REFERENCE = {"name": "highlight.js", "version": "11.12.0"}
MAX_TARBALL_BYTES = 64 * 1024 * 1024
# Archive expansion bounds, checked from each member header BEFORE its body
# is read or skipped; the decompressed stream is capped as well, so tar
# metadata (pax/GNU long-name blocks) cannot expand without bound either.
MAX_MEMBERS = 20_000
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024


def _registry_url(url: str) -> bool:
    return url.startswith(REGISTRY + "/")


class _RegistryOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when its target is on the pinned registry."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _registry_url(newurl):
            raise urllib.error.HTTPError(
                req.full_url, code, f"refusing redirect to a non-registry URL: {newurl}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_RegistryOnlyRedirects)


def _fetch(url: str, limit: int = MAX_TARBALL_BYTES) -> bytes:
    if not _registry_url(url):
        raise SystemExit(f"refusing non-registry URL: {url}")
    try:
        with _opener().open(url, timeout=60) as response:  # noqa: S310 - pinned host
            if not _registry_url(response.geturl()):
                raise SystemExit(f"refusing non-registry response URL: {response.geturl()}")
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"download failed ({exc.code} {exc.reason}): {url}") from None
    if len(data) > limit:
        raise SystemExit(f"download exceeds {limit} bytes: {url}")
    return data


def _sri512(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")


class _BoundedGunzip(io.RawIOBase):
    """gzip-decompressing reader that refuses to expand past ``limit`` bytes."""

    def __init__(self, blob: bytes, limit: int):
        self._source = io.BytesIO(blob)
        self._inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self._pending = b""
        self._limit = limit
        self.expanded = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        while not self._pending:
            if self._inflate.eof:
                return 0
            # Input left over by a bounded call is fed again before new input.
            chunk = self._inflate.unconsumed_tail or self._source.read(64 * 1024)
            if not chunk:
                raise SystemExit("truncated gzip stream")
            # Never inflate more than the remaining budget (+1 to detect excess).
            data = self._inflate.decompress(chunk, self._limit - self.expanded + 1)
            self.expanded += len(data)
            if self.expanded > self._limit:
                raise SystemExit(f"archive expands past {self._limit} bytes")
            self._pending = data
        size = min(len(buffer), len(self._pending))
        buffer[:size] = self._pending[:size]
        self._pending = self._pending[size:]
        return size


def _tar_members(blob: bytes, wanted=None, *, max_members: int | None = None,
                 max_member_bytes: int | None = None,
                 max_expanded_bytes: int | None = None) -> dict[str, bytes]:
    """Selected regular files of an npm tarball; refuse anything unsafe.

    ``wanted`` is a predicate on member names (``None`` keeps every regular
    file). The archive is streamed: every header is checked (path safety,
    type, duplicate name, member count, member size, cumulative declared
    size) before its body is read or skipped, only selected bodies are
    materialized, and the decompressed stream itself is capped.
    """
    max_members = MAX_MEMBERS if max_members is None else max_members
    max_member_bytes = MAX_MEMBER_BYTES if max_member_bytes is None else max_member_bytes
    max_expanded_bytes = MAX_EXPANDED_BYTES if max_expanded_bytes is None else max_expanded_bytes
    out: dict[str, bytes] = {}
    seen: set[str] = set()
    declared = 0
    # Tar framing (headers, padding, end blocks) adds to the stream; allow a
    # fixed per-member overhead on top of the declared-content budget.
    stream_cap = max_expanded_bytes + (max_members + 2) * 1024 + 10240
    stream = io.BufferedReader(_BoundedGunzip(blob, stream_cap), buffer_size=64 * 1024)
    try:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for count, member in enumerate(archive, start=1):
                name = member.name
                if count > max_members:
                    raise SystemExit(f"archive has more than {max_members} members")
                parts = PurePosixPath(name).parts
                if not name or name.startswith("/") or ".." in parts or "\\" in name or "\x00" in name:
                    raise SystemExit(f"unsafe tar member path: {name!r}")
                if name in seen:
                    raise SystemExit(f"duplicate tar member: {name!r}")
                seen.add(name)
                if member.isdir():
                    continue
                if not member.isreg():
                    raise SystemExit(f"refusing non-regular tar member: {name!r}")
                if member.size > max_member_bytes:
                    raise SystemExit(f"tar member exceeds {max_member_bytes} bytes: {name!r}")
                declared += member.size
                if declared > max_expanded_bytes:
                    raise SystemExit(f"archive expands past {max_expanded_bytes} bytes")
                if wanted is not None and not wanted(name):
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    raise SystemExit(f"unreadable tar member: {name!r}")
                data = handle.read(member.size + 1)
                if len(data) != member.size:
                    raise SystemExit(f"tar member size mismatch: {name!r}")
                out[name] = data
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        raise SystemExit(f"malformed archive: {exc}") from None
    return out


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".vendor-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _registry_dist(name: str, version: str) -> dict:
    meta = json.loads(_fetch(f"{REGISTRY}/{name}/{version}", 4 * 1024 * 1024))
    if meta.get("version") != version:
        raise SystemExit(f"registry returned {meta.get('version')} for {name}@{version}")
    return {"tarball": meta["dist"]["tarball"], "integrity": meta["dist"]["integrity"],
            "license": meta.get("license")}


def _download(tarball: str, integrity: str, wanted) -> dict[str, bytes]:
    """Verified tarball -> the selected member bodies (``wanted`` predicate)."""
    if not _registry_url(tarball):
        raise SystemExit(f"refusing non-registry tarball URL: {tarball}")
    blob = _fetch(tarball)
    if _sri512(blob) != integrity:
        raise SystemExit(f"tarball integrity mismatch: {tarball}")
    return _tar_members(blob, wanted)


_HLJS_LANGUAGE_FILE = re.compile(r"package/languages/[A-Za-z0-9_-]+\.min\.js")


def _pin_selector(pin: dict):
    """Member-name predicate for the files one pin can lock."""
    explicit = {src for src, _rel, _cdn in pin["files"]}
    globs = [re.compile(pattern) for pattern, _prefix in pin.get("globs", [])]
    languages = bool(pin.get("hljs_languages"))

    def wanted(name: str) -> bool:
        return (name in explicit or any(glob.fullmatch(name) for glob in globs)
                or (languages and bool(_HLJS_LANGUAGE_FILE.fullmatch(name))))
    return wanted


def _auxiliary_entries(dest: Path) -> list[dict]:
    """Hash the repository-authored auxiliary files present in ``dest``."""
    roles = dict(vendor.REQUIRED_AUXILIARY)
    for root in (dest, None):
        try:
            current = vendor.load_lock(root)
        except vendor.OfflineAssetsError:
            continue
        for entry in current.get("auxiliary", []):
            if isinstance(entry, dict) and isinstance(entry.get("dest"), str):
                roles.setdefault(entry["dest"], entry.get("role"))
        break
    entries = []
    for rel, role in sorted(roles.items()):
        try:
            data = vendor.read_vendor_bytes(rel, dest)
        except vendor.OfflineAssetsError as exc:
            raise SystemExit(f"auxiliary file {rel} must exist (regenerate and review it first): {exc}") from None
        entries.append({"dest": rel, "role": role, "size": len(data),
                        "sha256": vendor.sha256_hex(data), "sha384": vendor.sri_sha384(data)})
    return entries


def _entry(dest: str, src: str, data: bytes, cdn: str | None) -> dict:
    entry = {"src": src, "dest": dest, "size": len(data),
             "sha256": vendor.sha256_hex(data), "sha384": vendor.sri_sha384(data)}
    if cdn:
        entry["cdn"] = f"{CDN}/{cdn}"
    return entry


def _hljs_language_sets() -> tuple[dict, list[str], list[str]]:
    dist = _registry_dist(HLJS_REFERENCE["name"], HLJS_REFERENCE["version"])
    members = _download(dist["tarball"], dist["integrity"],
                        {"package/lib/index.js", "package/lib/common.js"}.__contains__)
    pattern = re.compile(r"registerLanguage\('([^']+)'")
    full = pattern.findall(members["package/lib/index.js"].decode("utf-8"))
    common = pattern.findall(members["package/lib/common.js"].decode("utf-8"))
    if not full or not common or not set(common) <= set(full):
        raise SystemExit("highlight.js language lists are not in the expected shape")
    reference = {"name": HLJS_REFERENCE["name"], "version": HLJS_REFERENCE["version"],
                 "tarball": dist["tarball"], "integrity": dist["integrity"],
                 "use": "language parity list (lib/index.js) and the common set (lib/common.js)"}
    return reference, sorted(full), sorted(common)


def init_lock(dest: Path) -> dict:
    pins = vendor.source_sri_pins()
    auxiliary = _auxiliary_entries(dest)  # before any network use
    reference, all_languages, common = _hljs_language_sets()
    packages = []
    verified: dict[str, bytes] = {}
    for pin in PINS:
        dist = _registry_dist(pin["name"], pin["version"])
        members = _download(dist["tarball"], dist["integrity"], _pin_selector(pin))
        files = []
        wanted = list(pin["files"])
        for pattern, prefix in pin.get("globs", []):
            for name in sorted(members):
                match = re.fullmatch(pattern, name)
                if match:
                    wanted.append((name, prefix + match.group(1), None))
        if pin.get("hljs_languages"):
            for language in sorted(set(all_languages) - set(common)):
                wanted.append((f"package/languages/{language}.min.js",
                               f"languages/{language}.min.js", None))
        for src, rel, cdn in wanted:
            if src not in members:
                raise SystemExit(f"{pin['name']}@{pin['version']} has no {src}")
            data = members[src]
            entry = _entry(f"{pin['dir']}/{rel}", src, data, cdn)
            if cdn:
                expected = pins.get(entry["cdn"])
                if expected is None or expected != entry["sha384"]:
                    raise SystemExit(f"{src} does not match the SRI pinned in the code for {entry['cdn']}")
            vendor._safe_rel(entry["dest"])
            files.append(entry)
            verified[entry["dest"]] = data
        packages.append({"name": pin["name"], "version": pin["version"],
                         "license": dist["license"], "tarball": dist["tarball"],
                         "integrity": dist["integrity"], "files": files})
    lock = {"schema": 1, "packages": packages, "reference_packages": [reference],
            "auxiliary": auxiliary,
            "parity": {"highlightjs_languages": all_languages,
                       "highlightjs_common": common}}
    # Derived outputs from the verified package files.
    fonts = {rel[len(vendor.KATEX_FONT_DIR):-len(".woff2")]: data for rel, data in verified.items()
             if rel.startswith(vendor.KATEX_FONT_DIR)}
    languages = [(rel, data) for rel, data in verified.items() if rel.startswith(vendor.HLJS_LANGUAGE_DIR)]
    derived = {
        vendor.KATEX_DERIVED: vendor.derive_katex_css(verified[vendor.KATEX_CSS], fonts),
        vendor.HLJS_DERIVED: vendor.derive_hljs_bundle(verified[vendor.HLJS_CORE], languages),
    }
    lock["derived"] = {rel: {"size": len(data), "sha256": vendor.sha256_hex(data)}
                       for rel, data in sorted(derived.items())}
    # Nothing is written until every package verified.
    for rel, data in verified.items():
        _atomic_write(dest / rel, data)
    _atomic_write(dest / vendor.LOCK_NAME, (json.dumps(lock, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    return lock


def fetch(dest: Path) -> None:
    lock = vendor.load_lock()
    pending: list[tuple[str, bytes]] = []
    for package in lock["packages"]:
        sources = {entry["src"] for entry in package["files"]}
        members = _download(package["tarball"], package["integrity"], sources.__contains__)
        for entry in package["files"]:
            data = members.get(entry["src"])
            if data is None:
                raise SystemExit(f"{package['name']} tarball has no {entry['src']}")
            if len(data) != entry["size"] or vendor.sha256_hex(data) != entry["sha256"] \
                    or vendor.sri_sha384(data) != entry["sha384"]:
                raise SystemExit(f"{entry['src']} does not match the lock")
            pending.append((str(vendor._safe_rel(entry["dest"])), data))
    # Nothing is written until every package verified.
    for rel, data in pending:
        _atomic_write(dest / rel, data)


def check(dest: Path | None) -> int:
    try:
        assets = vendor.load_offline_assets(dest)
    except vendor.OfflineAssetsError as exc:
        print(f"vendor check FAILED: {exc}", file=sys.stderr)
        return 1
    total = sum(len(data) for data in assets.files.values())
    print(f"vendor check OK: {len(vendor.lock_files(vendor.load_lock(dest)))} locked files; "
          f"{len(assets.files)} offline output files, {total} bytes")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify on-disk files (default; no network)")
    mode.add_argument("--fetch", action="store_true", help="download locked tarballs and extract locked files")
    mode.add_argument("--init-lock", action="store_true", help="rebuild the lock from the pins (maintainers)")
    parser.add_argument("--dest", type=Path, default=vendor.VENDOR_ROOT)
    args = parser.parse_args(argv)
    # Absolute but NOT resolved: a symlinked vendor root must reach the
    # verifier as a symlink so it is refused, not silently followed.
    dest = Path(os.path.abspath(args.dest))
    if args.init_lock:
        init_lock(dest)
        return check(dest)
    if args.fetch:
        fetch(dest)
        return check(dest if dest != vendor.VENDOR_ROOT else None)
    return check(dest if dest != vendor.VENDOR_ROOT else None)


if __name__ == "__main__":
    raise SystemExit(main())
