#!/usr/bin/env python3
"""Generate the bundled-dependency licence notices of the offline libraries (X1).

Maintainer tool; the okf-loom runtime never imports it. Standard library only.

The vendored files under ``scripts/okf_loom/viewer/vendor/`` contain
third-party code beyond the nine top-level packages whose LICENSE files ship
next to them: ``mermaid.min.js`` is a single-file esbuild bundle of Mermaid's
dependency tree, and ``cytoscape.min.js``, ``dagre.min.js`` and
``layout-base.js`` inline further code. This tool builds the exact inventory
of that code and writes, from exact upstream provenance only:

* ``vendor/BUNDLED-DEPENDENCY-NOTICES.txt`` -- the full licence text of every
  inventory entry (shipped with every offline output; locked in
  ``vendor.lock.json`` as an ``auxiliary`` notices file), and
* ``scripts/bundled_notices_inventory.json`` -- the machine-readable
  inventory with the evidence for every entry (name@version, where it is
  included, how it was established, tarball, integrity, licence-file hashes).

How the inventory is established (nothing is inferred from "latest" registry
metadata):

1. Mermaid: the ``dist/mermaid.min.js.map`` source map from the SAME verified
   npm tarball as the vendored ``mermaid.min.js`` (whose SHA-256 must equal
   the lock) lists every bundled module with its pnpm store path, which
   carries the exact ``name@version``. The prebuilt ``@mermaid-js/parser``
   chunks it bundles carry esbuild module-path comments with the same form.
   Every package is cross-checked against Mermaid's own ``pnpm-lock.yaml`` at
   the ``mermaid@11.17.2`` release commit: it must be in the lock with the
   same integrity the registry reports, and reachable from the ``mermaid``
   production dependencies (or the bundled parser workspace).
2. Prebuilt bundles inside that tree (and the vendored ``cytoscape.min.js``
   and ``dagre.min.js``) inline further packages. Their original sources in
   the exact tarball are scanned for bare imports, transitively, and each
   inlined package's version comes from that project's lockfile at the
   commit the npm release was published from.
3. Code snippets that upstream adapted into its own files (identified by the
   banners/comments they retained) and the KaTeX fonts (OFL-1.1, from the
   fonts' own ``name`` tables) are listed separately.

Every licence text is copied verbatim from the exact version's npm tarball
(integrity-verified) or from a pinned upstream commit; every GitHub file below
is pinned by commit AND SHA-256. Network: registry.npmjs.org and
raw.githubusercontent.com only; redirects are refused.

Modes: ``--check`` (default) regenerates in memory and compares with the
checked-in files byte for byte; ``--write`` rewrites them. After ``--write``,
run ``fetch_vendor_assets.py --init-lock`` (which re-hashes the auxiliary
notices files) or update the notices entries' hashes in the lock, then
review the diff.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import struct
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import fetch_vendor_assets as fva  # noqa: E402
from okf_loom.viewer import vendor  # noqa: E402

NOTICES_REL = vendor.BUNDLED_NOTICES_NAME
INVENTORY_PATH = SCRIPTS / "bundled_notices_inventory.json"
GITHUB_RAW = "https://raw.githubusercontent.com/"

MERMAID = {"name": "mermaid", "version": "11.17.2", "shipped": "mermaid-11.17.2/mermaid.min.js",
           "min": "package/dist/mermaid.min.js", "map": "package/dist/mermaid.min.js.map",
           "tag": "mermaid@11.17.2", "commit": "dcb694ddb58dc5ad3502e7e903cac05fd812eac3"}

#: Every upstream (non-npm) input: key -> (repo, commit, path, sha256).
GITHUB = {
    "mermaid-pnpm-lock": ("mermaid-js/mermaid", MERMAID["commit"], "pnpm-lock.yaml",
                          "fdc5fdda2c7067c9e10cc8743e0837554c5a924a763a7e2fbaa1cbaa9a775ffa"),
    "mermaid-root-package": ("mermaid-js/mermaid", MERMAID["commit"], "package.json",
                             "7749d044bdbf36544669c544bb69428a12d18bd963d843c3776b8ae2bbc0ee37"),
    "mermaid-parser-package": ("mermaid-js/mermaid", MERMAID["commit"], "packages/parser/package.json",
                               "131781c3d500d3c43453193dc3deeea281f8645d00831d85905180a6796e14ce"),
    "mermaid-parser-license": ("mermaid-js/mermaid", MERMAID["commit"], "packages/parser/LICENSE",
                               "d374061c40bfb6f99c5033290caefda97a9b3e7e477e5ee02f487143ef2177e8"),
    "mermaid-roughjs-patch": ("mermaid-js/mermaid", MERMAID["commit"], "patches/roughjs.patch",
                              "3543d47108cb41b68ec6a671c0e1f9d0cfe2ce524fea5b0992511ae84c3c6b64"),
    "cytoscape-3.34.3-lock": ("cytoscape/cytoscape.js", "716a1cb6c6015d57b674abe626deaeb5e817ee30",
                              "package-lock.json",
                              "602d7361177bf254f8f85225c4c036c6f04410d0236a98f36d90a2f7ecb2c8fb"),
    "cytoscape-3.34.0-lock": ("cytoscape/cytoscape.js", "22716bfb75834b56fa6679648b0abb06f4ae691c",
                              "package-lock.json",
                              "77313c9711350bdfbcd2a669f5a4d2dc1cc3fd8c7d6fe3e0641bd134761927e5"),
    "dagre-0.8.5-lock": ("dagrejs/dagre", "f56edb1abbb8530e532158f7cbd403228f5b0018", "package-lock.json",
                         "d859ed4f10013435b9d443225bafd01bd9971d1a95623d90452d4ecc4c941ae4"),
    "roughjs-4.6.6-lock": ("pshihn/rough", "56a2762171b1294d643501e8d14f120db6b27bd7", "package-lock.json",
                           "da065ed0bdfb3bf196c7cb05bf20dcb10100b6aa0f1f1d6037ea8aeb108b3ad5"),
    "venn-2.0.0-package": ("upsetjs/venn.js", "350c835aab4a92a7570963c28f725cf9f6e5f258", "package.json",
                           "7687fdd6ab0d96ae3bc9d6b254ac8e89a51bb1cc40f5e52da5d3fb5e534211ae"),
    "venn-2.0.0-yarn-lock": ("upsetjs/venn.js", "350c835aab4a92a7570963c28f725cf9f6e5f258", "yarn.lock",
                             "448c08192ee0db11a00e07fb87f30a7b4a4d32722a38ee15002ce6dea84be1d0"),
    "venn-2.0.0-fmin-patch": ("upsetjs/venn.js", "350c835aab4a92a7570963c28f725cf9f6e5f258",
                              ".yarn/patches/fmin-npm-0.0.4-e439f499bd.patch",
                              "bfef8fd004e5d4e0703164521f3c943ab5a4ff422aaa6abf54f19626c282e399"),
    "spdx-MIT": ("spdx/license-list-data", "31ba1a50e5397e00a304dbadc76531740e89ee48", "text/MIT.txt",
                 "b05785f9f18e6716bab63424b11454513b9943a222595b70411009202fc592b5"),
    "spdx-OFL-1.1": ("spdx/license-list-data", "31ba1a50e5397e00a304dbadc76531740e89ee48",
                     "text/OFL-1.1.txt",
                     "8eea8287e5876b539670cadb82e99f9a7afddec6f6730811be1daf25d2e9bcfd"),
}

#: Packages whose code in the Mermaid source map comes from files outside
#: ``src/``/``lib/``: each was reviewed. "modules" = one original module per
#: file (every import is itself a source-map entry); "bundle" = a prebuilt
#: bundle whose original sources are scanned from ``entry`` for inlined
#: packages, resolved through ``lock`` (a key of GITHUB; None = none may be
#: inlined).
MERMAID_PREBUILT = {
    "@braintree/sanitize-url": {"kind": "modules", "why": "tsc output; imports are source-map entries"},
    "uuid": {"kind": "modules", "why": "tsc output; imports are source-map entries"},
    "lodash-es": {"kind": "modules", "why": "one ES module per file at the package root"},
    "es-toolkit": {"kind": "modules", "why": "one ES module per file under dist/"},
    "js-yaml": {"kind": "modules", "why": "one CommonJS module per file (index.js + lib/)"},
    "khroma": {"kind": "modules", "why": "tsc output, one ES module per file under dist/"},
    "dayjs": {"kind": "bundle", "entry": "package/esm/index.js", "lock": None},
    "fastdom": {"kind": "bundle", "entry": "package/fastdom.js", "lock": None},
    "katex": {"kind": "bundle", "entry": "package/katex.ts", "lock": None},
    "layout-base": {"kind": "bundle", "entry": "package/index.js", "lock": None},
    "cose-base": {"kind": "bundle", "entry": "package/index.js", "lock": None,
                  "externals": ["layout-base"]},
    "cytoscape-cose-bilkent": {"kind": "bundle", "entry": "package/src/index.js", "lock": None,
                               "externals": ["cose-base"]},
    "cytoscape-fcose": {"kind": "bundle", "entry": "package/src/index.js", "lock": None,
                        "externals": ["cose-base"]},
    "cytoscape": {"kind": "bundle", "entry": "package/src/index.mjs", "lock": "cytoscape-{version}-lock"},
    "roughjs": {"kind": "bundle", "entry": "package/bin/rough.js", "lock": "roughjs-{version}-lock"},
    "@upsetjs/venn.js": {"kind": "bundle", "entry": "package/src/index.js", "lock": "venn-{version}-yarn-lock",
                         "externals": ["d3-selection", "d3-transition"]},
}

#: Top-level vendored files that inline other packages (beyond their own
#: LICENSE, which ships next to them).
VENDORED_BUNDLES = [
    {"shipped": "cytoscape-3.34.3/cytoscape.min.js", "name": "cytoscape", "version": "3.34.3",
     "entry": "package/src/index.mjs", "lock": "cytoscape-3.34.3-lock"},
    {"shipped": "dagre-0.8.5/dagre.min.js", "name": "dagre", "version": "0.8.5",
     "entry": "package/index.js", "lock": "dagre-0.8.5-lock",
     "fingerprints": {"graphlib": 'module.exports="{version}"'}},
    {"shipped": "layout-base-2.0.1/layout-base.js", "name": "layout-base", "version": "2.0.1",
     "entry": "package/index.js", "lock": None},
    {"shipped": "cose-base-2.2.0/cose-base.js", "name": "cose-base", "version": "2.2.0",
     "entry": "package/index.js", "lock": None, "externals": ["layout-base"]},
    {"shipped": "cytoscape-fcose-2.2.0/cytoscape-fcose.js", "name": "cytoscape-fcose", "version": "2.2.0",
     "entry": "package/src/index.js", "lock": None, "externals": ["cose-base"]},
    {"shipped": "cytoscape-dagre-2.5.0/cytoscape-dagre.js", "name": "cytoscape-dagre", "version": "2.5.0",
     "entry": "package/src/index.js", "lock": None, "externals": ["dagre"]},
    {"shipped": "katex-0.16.47/katex.min.js", "name": "katex", "version": "0.16.47",
     "entry": "package/katex.ts", "lock": None},
]

#: Code upstream adapted into its own files. ``marker`` must occur in
#: ``source`` (a file of the named exact tarball); the retained banner is
#: quoted verbatim from there. ``terms``: a GITHUB key, or ("tarball-comment",
#: start, end) to quote the licence text the upstream file itself carries.
SNIPPETS = [
    {"id": "snippet:promises-aplus-thenable", "title": "Embeddable Minimum Strictly-Compliant Promises/A+ 1.1.1 Thenable",
     "package": "cytoscape@3.34.3", "source": "package/src/promise.mjs",
     "marker": "Embeddable Minimum Strictly-Compliant Promises/A+ 1.1.1 Thenable", "license": "MIT",
     "terms": "spdx-MIT"},
    {"id": "snippet:jquery-event-object", "title": "Event object based on jQuery events",
     "package": "cytoscape@3.34.3", "source": "package/src/event.mjs",
     "marker": "Event object based on jQuery events, MIT license", "license": "MIT", "terms": "spdx-MIT"},
    {"id": "snippet:bezier-curve-generator", "title": "Bezier curve function generator (Gaetan Renaudeau)",
     "package": "cytoscape@3.34.3", "source": "package/src/core/animation/cubic-bezier.mjs",
     "marker": "Bezier curve function generator. Copyright Gaetan Renaudeau", "license": "MIT",
     "terms": "spdx-MIT"},
    {"id": "snippet:runge-kutta-spring", "title": "Runge-Kutta spring physics function generator (Framer.js, Koen Bok)",
     "package": "cytoscape@3.34.3", "source": "package/src/core/animation/spring.mjs",
     "marker": "Runge-Kutta spring physics function generator", "license": "MIT", "terms": "spdx-MIT"},
    {"id": "snippet:jamajs-svd", "title": "Singular value decomposition adopted from JamaJS",
     "package": "layout-base@2.0.1", "source": "package/src/util/SVD.js",
     "marker": "adopted from https://github.com/dragonfly-ai/JamaJS", "license": "Apache-2.0",
     "terms": ("tarball-comment", "Apache License", "limitations under the License.")},
]

#: Snippets whose licence terms cannot be established from exact provenance.
#: They are listed (attribution verbatim) and reported as gaps, never guessed.
UNRESOLVED_SNIPPETS = [
    {"id": "snippet:stackoverflow-19303725", "title": "Seeded pseudo-random nextDouble",
     "package": "layout-base@2.0.1", "source": "package/src/util/RandomSeed.js",
     "marker": "adapted from: https://stackoverflow.com/a/19303725",
     "gap": ("The two-line seeded random function was adapted from Stack Overflow answer 19303725; "
             "upstream records only the URL. The contribution's licence terms and author are not recorded "
             "in any exact upstream artifact, so no licence text is reproduced here.")},
]

#: Exact tarballs without a licence file whose full licence text is a section
#: of a file in the same tarball: id -> (member, heading). Quoted verbatim
#: from the heading to the next heading of the same level (or the end).
LICENCE_SECTIONS = {
    "fastdom@1.0.12": ("package/README.md", "## License"),
}

#: Copyright notices that the exact tarball states only in a retained source
#: banner (the licence file carries the licence terms alone): id ->
#: (member, marker). The banner line is quoted verbatim.
ATTRIBUTION_BANNERS = {
    "dompurify@3.4.12": ("package/dist/purify.es.mjs", "@license DOMPurify 3.4.12"),
}

LICENCE_FILE = re.compile(
    r"^[^/]+/(?:licen[cs]e|copying|notice|unlicense)"
    r"(?:[-_](?:mit|mpl|apache(?:-?2(?:\.0)?)?|bsd|isc|ofl))?(?:\.(?:md|txt|markdown|rst))?$", re.I)
_IMPORT = re.compile(
    r"""(?:\bimport\s*(?:[\w$*{}\s,]+?\s*from\s*)?|\bexport\s*(?:type\s*)?[\w$*{}\s,]*?\s*from\s*"""
    r"""|(?<![.\w$])require\s*\(\s*|\bimport\s*\(\s*)["']([^"'\n]+)["']""")
_COMMENTS = re.compile(r"/\*.*?\*/|(?<![:\"'\\])//[^\n]*", re.S)
NODE_BUILTINS = {"fs", "path", "os", "url", "util", "crypto", "stream", "events", "module", "process",
                 "child_process", "worker_threads", "vm", "assert", "buffer", "http", "https", "zlib"}


# ---------------------------------------------------------------------------
# Network (pinned)
# ---------------------------------------------------------------------------

class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"refusing redirect to {newurl}", headers, fp)


_GITHUB_CACHE: dict[str, bytes] = {}


def github(key: str) -> bytes:
    if key in _GITHUB_CACHE:
        return _GITHUB_CACHE[key]
    repo, commit, path, sha256 = GITHUB[key]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SystemExit(f"{key}: GitHub inputs must be pinned to a full commit")
    url = f"{GITHUB_RAW}{repo}/{commit}/{path}"
    try:
        with urllib.request.build_opener(_NoRedirects).open(url, timeout=60) as response:  # noqa: S310
            data = response.read(32 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"download failed ({exc.code}): {url}") from None
    if hashlib.sha256(data).hexdigest() != sha256:
        raise SystemExit(f"{key}: {url} does not match its pinned SHA-256")
    _GITHUB_CACHE[key] = data
    return data


def github_url(key: str) -> str:
    repo, commit, path, _sha = GITHUB[key]
    return f"https://github.com/{repo}/blob/{commit}/{path}"


_META: dict[str, dict] = {}
_TARBALLS: dict[str, dict[str, bytes]] = {}


def registry(name: str, version: str) -> dict:
    key = f"{name}@{version}"
    if key not in _META:
        meta = json.loads(fva._fetch(f"{fva.REGISTRY}/{name}/{version}", 4 * 1024 * 1024))
        if meta.get("name") != name or meta.get("version") != version:
            raise SystemExit(f"registry returned {meta.get('name')}@{meta.get('version')} for {key}")
        _META[key] = meta
    return _META[key]


def tarball(name: str, version: str, integrity: str | None = None) -> dict[str, bytes]:
    """Every regular file of name@version's npm tarball (sha512-verified)."""
    key = f"{name}@{version}"
    if key not in _TARBALLS:
        dist = registry(name, version)["dist"]
        if integrity is not None and dist["integrity"] != integrity:
            raise SystemExit(f"{key}: registry integrity differs from the upstream lockfile")
        _TARBALLS[key] = fva._download(dist["tarball"], dist["integrity"], None)
    return _TARBALLS[key]


# ---------------------------------------------------------------------------
# Lockfiles
# ---------------------------------------------------------------------------

def _unquote(text: str) -> str:
    text = text.strip()
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"" else text


def parse_pnpm_lock(text: str) -> dict:
    """The parts of a pnpm v9 lockfile this tool needs (block-style YAML)."""
    if not text.startswith("lockfileVersion: '9.0'"):
        raise SystemExit("pnpm-lock.yaml is not lockfile version 9.0")
    importers: dict[str, dict[str, dict[str, str]]] = {}
    packages: dict[str, str] = {}
    snapshots: dict[str, dict[str, str]] = {}
    section = importer = group = entry = dep = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        if indent == 0:
            section = body.rstrip(":")
            continue
        if section == "importers":
            if indent == 2:
                importer = _unquote(body.rstrip(":"))
                importers[importer] = {}
            elif indent == 4:
                group = body.rstrip(":")
                importers[importer][group] = {}
            elif indent == 6:
                dep = _unquote(body.rstrip(":"))
            elif indent == 8 and body.startswith("version:"):
                importers[importer][group][dep] = _unquote(body.split(":", 1)[1])
        elif section == "packages":
            if indent == 2:
                entry = _unquote(body.rstrip(":"))
            elif indent == 4 and body.startswith("resolution:"):
                match = re.search(r"integrity:\s*([^,}\s]+)", body)
                if match:
                    packages[entry] = match.group(1)
        elif section == "snapshots":
            if indent == 2:
                entry = _unquote(body[:-1] if body.endswith(":") else body.split(": ", 1)[0])
                snapshots[entry] = {}
            elif indent == 4:
                group = body.rstrip(":")
            elif indent == 6 and group in ("dependencies", "optionalDependencies"):
                name, _, version = body.partition(":")
                snapshots[entry][_unquote(name)] = _unquote(version)
    return {"importers": importers, "packages": packages, "snapshots": snapshots}


def pnpm_closure(lock: dict, importer: str, groups: tuple[str, ...]) -> set[tuple[str, str]]:
    """(name, version) reachable from an importer's dependency groups."""
    seen: set[str] = set()
    out: set[tuple[str, str]] = set()
    stack: list[tuple[str, str]] = []
    for group in groups:
        stack.extend(lock["importers"].get(importer, {}).get(group, {}).items())
    while stack:
        name, version = stack.pop()
        if version.startswith("link:"):
            continue
        key = f"{name}@{version}"
        if key in seen:
            continue
        seen.add(key)
        out.add((name, version.split("(", 1)[0]))
        stack.extend(lock["snapshots"].get(key, {}).items())
    return out


def npm_lock_entry(lock: dict, name: str) -> tuple[str, str | None]:
    """(version, integrity) of a top-level package in an npm package-lock."""
    if "packages" in lock:
        entry = lock["packages"].get(f"node_modules/{name}")
    else:
        entry = lock.get("dependencies", {}).get(name)
    if not entry or "version" not in entry:
        raise SystemExit(f"{name} is not in the upstream lockfile")
    return entry["version"], entry.get("integrity")


def yarn_berry_version(lock_text: str, package_json: dict, name: str) -> str:
    spec = {**package_json.get("dependencies", {}), **package_json.get("devDependencies", {})}.get(name)
    match = re.search(r"(?:^|@)npm(?:%3A|:)(\d+\.\d+\.\d+)(?:#|$)", spec or "")
    if not match:
        raise SystemExit(f"{name}: no exact npm version in package.json ({spec!r})")
    version = match.group(1)
    if f'\n"{name}@npm:{version}":\n  version: {version}\n' not in lock_text:
        raise SystemExit(f"{name}@{version} is not resolved in yarn.lock")
    return version


def lock_resolver(key: str | None):
    """name -> (version, integrity or None, provenance text) for one lockfile."""
    if key is None:
        def refuse(name: str):
            raise SystemExit(f"unexpected inlined package {name!r} (no lockfile declared)")
        return refuse
    data = github(key)
    if key.endswith("yarn-lock"):
        package_json = json.loads(github(key.replace("yarn-lock", "package")))
        text = data.decode("utf-8")
        return lambda name: (yarn_berry_version(text, package_json, name), None,
                             f"{github_url(key)} (+ package.json at the same commit)")
    lock = json.loads(data)

    def resolve(name: str):
        version, integrity = npm_lock_entry(lock, name)
        return version, integrity, github_url(key)
    return resolve


# ---------------------------------------------------------------------------
# Source scanning (what a bundle inlined)
# ---------------------------------------------------------------------------

def _package_name(spec: str) -> str:
    parts = spec.split("/")
    return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


def _resolve_file(files: dict[str, bytes], path: str) -> str | None:
    for candidate in (path, path + ".js", path + ".mjs", path + ".ts", path + ".cjs",
                      path + "/index.js", path + "/index.mjs", path + "/index.ts"):
        if candidate in files:
            return candidate
    return None


def _entry_of(files: dict[str, bytes], subpath: str) -> str:
    if subpath:
        found = _resolve_file(files, f"package/{subpath}")
    else:
        meta = json.loads(files["package/package.json"])
        found = None
        for field in ("module", "jsnext:main", "main"):
            if isinstance(meta.get(field), str):
                found = _resolve_file(files, posixpath.normpath(f"package/{meta[field]}"))
                if found:
                    break
        found = found or _resolve_file(files, "package/index")
    if not found:
        raise SystemExit(f"cannot resolve package entry {subpath!r}")
    return found


def scan_imports(files: dict[str, bytes], entry: str, texts: list[str] | None = None) -> dict[str, set[str]]:
    """Bare specifiers reachable from ``entry`` through relative imports.

    The raw text of every visited file is appended to ``texts`` (when given).
    """
    if entry not in files:
        raise SystemExit(f"scan entry {entry} is not in the tarball")
    bare: dict[str, set[str]] = {}
    stack, seen = [entry], set()
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        raw = files[path].decode("utf-8", "replace")
        if texts is not None:
            texts.append(raw)
        text = _COMMENTS.sub(" ", raw)
        for spec in _IMPORT.findall(text):
            if spec.startswith("."):
                target = _resolve_file(files, posixpath.normpath(posixpath.join(posixpath.dirname(path), spec)))
                if target is None:
                    if spec.endswith((".css", ".less", ".json")):
                        continue
                    raise SystemExit(f"unresolved relative import {spec!r} in {path}")
                stack.append(target)
            else:
                bare.setdefault(_package_name(spec), set()).add(spec)
    return bare


def inlined_packages(container: str, files: dict[str, bytes], entry: str, lock_key: str | None,
                     externals: list[str], texts: list[str] | None = None) -> list[dict]:
    """Packages inlined into a bundle: scan transitively, versions from its lock."""
    resolve = lock_resolver(lock_key)
    found: dict[str, dict] = {}
    queue = [(container, files, entry)]
    first = True
    while queue:
        via, tree, start = queue.pop()
        bare = scan_imports(tree, start, texts)
        for name, specs in sorted(bare.items()):
            if name in NODE_BUILTINS:
                raise SystemExit(f"{via} imports the Node built-in {name!r}; review before bundling notices")
            if first and name in externals:
                continue
            if name in found:
                continue
            version, integrity, provenance = resolve(name)
            nested = tarball(name, version, integrity)
            found[name] = {"name": name, "version": version, "integrity": integrity,
                           "lock": provenance, "via": via}
            for spec in sorted(specs):
                queue.append((f"{name}@{version}", nested, _entry_of(nested, spec[len(name) + 1:])))
        first = False
    return sorted(found.values(), key=lambda item: item["name"])


# ---------------------------------------------------------------------------
# Mermaid inventory
# ---------------------------------------------------------------------------

def _pnpm_dir(directory: str, expect: str) -> tuple[str, str, str]:
    base, _, suffix = directory.partition("_")
    name, _, version = base.rpartition("@")
    name = name.replace("+", "/")
    if name != expect or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[\w.]+)?", version):
        raise SystemExit(f"unexpected pnpm store path {directory!r} for {expect!r}")
    return name, version, suffix


_STORE = re.compile(r"node_modules/\.pnpm/([^/]+)/node_modules/((?:@[^/]+/)?[^/]+)/(.+)$")


def mermaid_inventory() -> tuple[dict, dict]:
    lock = vendor.load_lock()
    package = next(p for p in lock["packages"] if p["name"] == MERMAID["name"])
    shipped = next(f for f in package["files"] if f["dest"] == MERMAID["shipped"])
    members = fva._download(package["tarball"], package["integrity"], {MERMAID["min"], MERMAID["map"]}.__contains__)
    if vendor.sha256_hex(members[MERMAID["min"]]) != shipped["sha256"]:
        raise SystemExit("mermaid.min.js in the tarball is not the vendored file")
    source_map = json.loads(members[MERMAID["map"]])
    sources, contents = source_map["sources"], source_map.get("sourcesContent") or []
    if len(sources) != len(contents):
        raise SystemExit("mermaid.min.js.map has no complete sourcesContent")

    direct: dict[tuple[str, str], dict] = {}
    parser: dict[tuple[str, str], dict] = {}
    texts: dict[str, list[str]] = {}
    own = parser_files = 0
    for path, content in zip(sources, contents):
        match = _STORE.search(path)
        if match:
            name, version, suffix = _pnpm_dir(match.group(1), match.group(2))
            item = direct.setdefault((name, version), {"files": set(), "suffix": suffix})
            item["files"].add(match.group(3))
            texts.setdefault(f"{name}@{version}", []).append(content or "")
        elif path.startswith("../src/"):
            own += 1
        elif path.startswith("../../parser/dist/"):
            parser_files += 1
            for comment in re.findall(r"^// (\S+)$", content or "", re.M):
                inner = _STORE.search(comment)
                if inner:
                    name, version, suffix = _pnpm_dir(inner.group(1), inner.group(2))
                    parser.setdefault((name, version), {"files": set(), "suffix": suffix})["files"].add(inner.group(3))
        else:
            raise SystemExit(f"mermaid.min.js.map source of unknown origin: {path}")

    pnpm = parse_pnpm_lock(github("mermaid-pnpm-lock").decode("utf-8"))
    closure = pnpm_closure(pnpm, "packages/mermaid", ("dependencies", "optionalDependencies"))
    # esbuild bundles whatever the sources import, so a bundled package may
    # also come from mermaid's devDependencies or the (hoisted) workspace root.
    reachable = [
        ("packages/mermaid dependencies", closure),
        ("packages/mermaid devDependencies", pnpm_closure(pnpm, "packages/mermaid", ("devDependencies",))),
        ("workspace root dependencies", pnpm_closure(pnpm, ".", ("dependencies", "devDependencies"))),
    ]
    parser_reachable = [("packages/parser dependencies and devDependencies",
                         pnpm_closure(pnpm, "packages/parser", ("dependencies", "devDependencies")))]
    if pnpm["importers"]["packages/mermaid"]["dependencies"].get("@mermaid-js/parser") != "link:../parser":
        raise SystemExit("mermaid's @mermaid-js/parser is not the workspace package in pnpm-lock.yaml")
    parser_pkg = json.loads(github("mermaid-parser-package"))
    patch_sha = hashlib.sha256(github("mermaid-roughjs-patch")).hexdigest()

    entries: list[dict] = []
    for origin, found, reach in (("mermaid.min.js.map", direct, reachable),
                                 ("@mermaid-js/parser dist chunks", parser, parser_reachable)):
        for (name, version), item in sorted(found.items()):
            integrity = pnpm["packages"].get(f"{name}@{version}")
            if integrity is None:
                raise SystemExit(f"{name}@{version} is not in mermaid's pnpm-lock.yaml")
            via = next((label for label, members in reach if (name, version) in members), None)
            if via is None:
                raise SystemExit(f"{name}@{version} is not reachable from {origin}'s lockfile importers")
            if "patch_hash=" in item["suffix"] and item["suffix"].split("patch_hash=", 1)[1] != patch_sha:
                raise SystemExit(f"{name}@{version} carries a patch hash that is not patches/roughjs.patch")
            entries.append({"name": name, "version": version, "integrity": integrity, "origin": origin,
                            "reachable_from": via,
                            "files": sorted(item["files"]), "patched": "patch_hash=" in item["suffix"]})
    evidence = {
        "sourcemap": {"tarball": package["tarball"], "integrity": package["integrity"],
                      "member": MERMAID["map"], "sha256": vendor.sha256_hex(members[MERMAID["map"]]),
                      "sources": len(sources), "mermaid_own_sources": own,
                      "parser_chunk_sources": parser_files},
        "pnpm_lock": github_url("mermaid-pnpm-lock"),
        "release": {"tag": MERMAID["tag"], "commit": MERMAID["commit"]},
        "parser_workspace": {"name": parser_pkg["name"], "version": parser_pkg["version"],
                             "package_json": github_url("mermaid-parser-package")},
        "production_closure_not_in_bundle": sorted(
            f"{n}@{v}" for n, v in closure - set(direct) if not n.startswith("@types/")),
        "production_closure_type_packages": sorted(f"{n}@{v}" for n, v in closure if n.startswith("@types/")),
    }
    return {"entries": entries, "parser": parser_pkg, "texts": texts}, evidence


# ---------------------------------------------------------------------------
# Licence texts
# ---------------------------------------------------------------------------

def as_quoted(text: str) -> str:
    """The form a licence text takes in the notices file: LF line endings, no
    trailing spaces, no trailing newline. Its SHA-256 is ``quoted_sha256``."""
    return "\n".join(line.rstrip(" \t") for line in text.replace("\r\n", "\n").split("\n")).rstrip("\n")


def licence_files(files: dict[str, bytes], label: str) -> list[dict]:
    out = []
    for path in sorted(files):
        if LICENCE_FILE.match(path):
            text = files[path].decode("utf-8")
            if not text.strip():
                raise SystemExit(f"{label}: empty licence file {path}")
            out.append({"path": path, "sha256": vendor.sha256_hex(files[path]), "text": text})
    if not out and label in LICENCE_SECTIONS:
        member, heading = LICENCE_SECTIONS[label]
        text = files[member].decode("utf-8")
        match = re.search(rf"^{re.escape(heading)}[ \t]*\n(.*?)(?=^{heading.split(' ')[0]} |\Z)", text, re.M | re.S)
        if not match or not match.group(1).strip():
            raise SystemExit(f"{label}: no {heading!r} section in {member}")
        section = (heading + "\n" + match.group(1)).rstrip() + "\n"
        out.append({"path": f"{member} ({heading} section)", "sha256": vendor.sha256_hex(section.encode("utf-8")),
                    "text": section})
    if not out:
        raise SystemExit(f"{label}: the exact tarball has no licence file")
    return out


def _font_names(ttf: bytes) -> dict[int, str]:
    (count,) = struct.unpack(">H", ttf[4:6])
    for index in range(count):
        tag, _sum, offset, _length = struct.unpack(">4sIII", ttf[12 + 16 * index:28 + 16 * index])
        if tag != b"name":
            continue
        _fmt, records, storage = struct.unpack(">HHH", ttf[offset:offset + 6])
        names: dict[int, str] = {}
        for record in range(records):
            pid, _eid, lid, nid, length, start = struct.unpack(
                ">HHHHHH", ttf[offset + 6 + 12 * record:offset + 18 + 12 * record])
            if pid == 3 and lid == 0x409:
                raw = ttf[offset + storage + start:offset + storage + start + length]
                names[nid] = raw.decode("utf-16-be")
        return names
    raise SystemExit("font has no name table")


def katex_fonts_entry() -> dict:
    lock = vendor.load_lock()
    package = next(p for p in lock["packages"] if p["name"] == "katex")
    files = tarball("katex", package["version"], package["integrity"])
    shipped = sorted(f["dest"] for f in package["files"] if f["dest"].startswith(vendor.KATEX_FONT_DIR))
    notices: dict[str, list[str]] = {}
    for dest in shipped:
        face = dest.rsplit("/", 1)[1][:-len(".woff2")]
        names = _font_names(files[f"package/dist/fonts/{face}.ttf"])
        if "Open Font License" not in names.get(13, "") or not names.get(0):
            raise SystemExit(f"{face}: the font's name table does not state the SIL OFL")
        notices.setdefault(names[13].strip(), []).append(face)
    return {"id": "katex-fonts@0.16.47", "name": "KaTeX fonts", "version": package["version"],
            "license": "OFL-1.1", "included_in": [f"{vendor.KATEX_FONT_DIR}*.woff2 (inlined into "
                                                    f"{vendor.KATEX_DERIVED})"],
            "evidence": [f"name-table records 0/13 of package/dist/fonts/<face>.ttf in {package['tarball']} "
                         "(the same faces as the vendored .woff2 files)"],
            "tarball": package["tarball"], "integrity": package["integrity"],
            "notices": [{"faces": faces, "text": text} for text, faces in sorted(notices.items())],
            "terms": "spdx-OFL-1.1"}


def _quote_between(text: str, start: str, end: str) -> str:
    at = text.index(start)
    stop = text.index(end, at) + len(end)
    line_start = text.rfind("\n", 0, at) + 1
    return text[line_start:stop]


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------

def build() -> tuple[str, str]:
    lock = vendor.load_lock()
    shipped_bytes = {rel: vendor.read_vendor_bytes(rel) for rel in
                     [MERMAID["shipped"]] + [b["shipped"] for b in VENDORED_BUNDLES]}
    mermaid, evidence = mermaid_inventory()
    entries: dict[str, dict] = {}

    def add(name: str, version: str, integrity: str | None, included: str, how: str, lock_url: str,
            *, patched: str | None = None) -> None:
        key = f"{name}@{version}"
        meta = registry(name, version)
        files = tarball(name, version, integrity)
        entry = entries.get(key)
        if entry is None:
            entry = entries[key] = {
                "id": key, "name": name, "version": version, "license": meta.get("license"),
                "tarball": meta["dist"]["tarball"], "integrity": meta["dist"]["integrity"],
                "included_in": [], "evidence": [], "licence_files": licence_files(files, key)}
            if key in ATTRIBUTION_BANNERS:
                member, marker = ATTRIBUTION_BANNERS[key]
                lines = [line for line in files[member].decode("utf-8").splitlines() if marker in line]
                if len(lines) != 1:
                    raise SystemExit(f"{key}: attribution banner not found once in {member}")
                entry["attribution"] = {"source": member, "text": lines[0].strip()}
        if included not in entry["included_in"]:
            entry["included_in"].append(included)
        line = f"{how}; version and integrity: {lock_url}"
        if line not in entry["evidence"]:
            entry["evidence"].append(line)
        if patched and patched not in entry["evidence"]:
            entry["evidence"].append(patched)

    mermaid_file = MERMAID["shipped"]
    reviewed: dict[str, str] = {}
    unreviewed: list[str] = []
    for item in mermaid["entries"]:
        name, version = item["name"], item["version"]
        outside = sorted({f.split("/", 1)[0] for f in item["files"]} - {"src", "lib"})
        how = (f"{len(item['files'])} module(s) in {item['origin']} under "
               f"node_modules/.pnpm/{name.replace('/', '+')}@{version}; lockfile: {item['reachable_from']}")
        patched = None
        if item["patched"]:
            patched = (f"patched by mermaid: {github_url('mermaid-roughjs-patch')} (SHA-256 = the pnpm "
                       "patch_hash; it changes only the bin/rough.d.ts type declaration)")
        add(name, version, item["integrity"], mermaid_file, how, evidence["pnpm_lock"], patched=patched)
        if outside and item["origin"] == "mermaid.min.js.map":
            review = MERMAID_PREBUILT.get(name)
            if review is None:
                unreviewed.append(f"{name}@{version} ({', '.join(item['files'][:3])}, ...)")
                continue
            reviewed[f"{name}@{version}"] = review["kind"]
            if review["kind"] == "bundle":
                files = tarball(name, version, item["integrity"])
                lock_key = review["lock"].format(version=version) if review["lock"] else None
                for inner in inlined_packages(f"{name}@{version}", files, review["entry"], lock_key,
                                              review.get("externals", []),
                                              mermaid["texts"].setdefault(f"{name}@{version}", [])):
                    add(inner["name"], inner["version"], inner["integrity"], mermaid_file,
                        f"inlined into {inner['via']} (bundled by {name}@{version}; found by scanning "
                        f"its sources from {review['entry']})", inner["lock"])
    if unreviewed:
        raise SystemExit("prebuilt files not reviewed in MERMAID_PREBUILT: " + "; ".join(unreviewed))
    parser = mermaid["parser"]
    parser_id = f"{parser['name']}@{parser['version']}"
    license_text = github("mermaid-parser-license").decode("utf-8")
    entries[parser_id] = {
        "id": parser_id, "name": parser["name"], "version": parser["version"], "license": parser["license"],
        "tarball": None, "integrity": None, "included_in": [mermaid_file],
        "evidence": [f"workspace package bundled from ../../parser/dist ({evidence['sourcemap']['parser_chunk_sources']} "
                     f"chunk files) at {MERMAID['tag']}; {evidence['parser_workspace']['package_json']}"],
        "licence_files": [{"path": github_url("mermaid-parser-license"),
                           "sha256": vendor.sha256_hex(license_text.encode("utf-8")), "text": license_text}]}

    bundle_texts: dict[str, list[str]] = {}
    for bundle in VENDORED_BUNDLES:
        package = next(p for p in lock["packages"] if p["name"] == bundle["name"])
        if package["version"] != bundle["version"]:
            raise SystemExit(f"{bundle['name']}: lock version {package['version']} is not {bundle['version']}")
        files = tarball(bundle["name"], bundle["version"], package["integrity"])
        shipped = bundle["shipped"]
        src = "package/" + shipped.split("/", 1)[1]
        for candidate in (src, src.replace("package/", "package/dist/", 1)):
            if candidate in files:
                if files[candidate] != shipped_bytes[shipped]:
                    raise SystemExit(f"{shipped} is not the tarball's {candidate}")
                break
        else:
            raise SystemExit(f"{shipped}: tarball file not found")
        for inner in inlined_packages(f"{bundle['name']}@{bundle['version']}", files, bundle["entry"],
                                      bundle["lock"], bundle.get("externals", []),
                                      bundle_texts.setdefault(shipped, [])):
            fingerprint = bundle.get("fingerprints", {}).get(inner["name"])
            if fingerprint and fingerprint.format(version=inner["version"]).encode() not in shipped_bytes[shipped]:
                raise SystemExit(f"{shipped} lacks the {inner['name']}@{inner['version']} version fingerprint")
            add(inner["name"], inner["version"], inner["integrity"], shipped,
                f"inlined into {inner['via']} (found by scanning {bundle['name']}@{bundle['version']} "
                f"sources from {bundle['entry']})", inner["lock"])

    snippets = []
    for spec in SNIPPETS + UNRESOLVED_SNIPPETS:
        name, _, version = spec["package"].rpartition("@")
        files = tarball(name, version)
        text = files[spec["source"]].decode("utf-8")
        if spec["marker"] not in text:
            raise SystemExit(f"{spec['id']}: marker not found in {spec['package']} {spec['source']}")
        at = text.index(spec["marker"])
        start = max(text.rfind("/*", 0, at), text.rfind("//", 0, at))
        end = text.index("*/", at) + 2 if text.rfind("/*", 0, at) == start else text.index("\n", at)
        banner = text[start:end].rstrip("\r")
        retained = sorted(rel for rel, data in shipped_bytes.items() if spec["marker"].encode() in data)
        # Where the snippet's code is: every scanned bundle source and every
        # source-map package whose original text carries the marker.
        where = sorted(rel for rel, texts in bundle_texts.items() if any(spec["marker"] in t for t in texts))
        where += sorted(f"{mermaid_file} (via {key})" for key, texts in mermaid["texts"].items()
                        if any(spec["marker"] in t for t in texts))
        if not where:
            raise SystemExit(f"{spec['id']}: not found in any shipped file's sources")
        item = {"id": spec["id"], "title": spec["title"], "found_in": f"{spec['package']} {spec['source']}",
                "included_in": where, "banner_retained_in": retained, "banner": banner}
        if "gap" in spec:
            item.update(license=None, gap=spec["gap"])
        else:
            terms = spec["terms"]
            if isinstance(terms, tuple):
                item["terms_text"] = _quote_between(text, terms[1], terms[2])
                item["terms_source"] = f"{spec['package']} {spec['source']} (verbatim)"
            else:
                item["terms_text"] = github(terms).decode("utf-8")
                item["terms_source"] = github_url(terms)
            item["license"] = spec["license"]
        snippets.append(item)

    fonts = katex_fonts_entry()
    fonts["terms_text"] = github(fonts.pop("terms")).decode("utf-8")
    fonts["terms_source"] = github_url("spdx-OFL-1.1")

    ordered = [entries[key] for key in sorted(entries)]
    for entry in ordered:
        entry["included_in"].sort()
    inventory = {
        "schema": 1,
        "generator": "scripts/generate_bundled_notices.py",
        "notices_file": f"scripts/okf_loom/viewer/vendor/{NOTICES_REL}",
        "mermaid": evidence,
        "mermaid_prebuilt_reviewed": dict(sorted(reviewed.items())),
        "github_inputs": {key: {"url": github_url(key), "sha256": value[3]} for key, value in sorted(GITHUB.items())},
        "packages": [{k: v for k, v in entry.items() if k != "licence_files"}
                     | {"licence_files": [{"path": f["path"], "sha256": f["sha256"],
                                          "quoted_sha256": vendor.sha256_hex(as_quoted(f["text"]).encode("utf-8"))}
                                         for f in entry["licence_files"]]}
                     for entry in ordered],
        "snippets": [{k: v for k, v in item.items() if k != "terms_text"} for item in snippets],
        "fonts": {k: v for k, v in fonts.items() if k != "terms_text"},
        "gaps": [{"id": item["id"], "reason": item["gap"]} for item in snippets if item.get("gap")],
    }
    return render_notices(ordered, snippets, fonts, evidence), json.dumps(inventory, indent=1, sort_keys=True) + "\n"


RULE = "=" * 78


def render_notices(entries: list[dict], snippets: list[dict], fonts: dict, evidence: dict) -> str:
    out = [
        "okf-loom offline assets: bundled-dependency licence notices",
        "=" * 59,
        "",
        "GENERATED by scripts/generate_bundled_notices.py -- do not edit by hand.",
        "",
        "The vendored libraries listed in THIRD-PARTY-NOTICES.txt contain further",
        "third-party code. This file reproduces, verbatim, the licence text of every",
        "such component, taken from the exact version's npm tarball (integrity-",
        "verified) or from a pinned upstream commit. The machine-readable inventory",
        "and its evidence are in scripts/bundled_notices_inventory.json. Line endings",
        "are normalised to LF and trailing spaces dropped; each quoted file's sha256",
        "is that of the unmodified upstream file.",
        "",
        "How the inventory was established:",
        f"  * mermaid.min.js: the source map shipped in the same mermaid@{MERMAID['version']} npm",
        "    tarball (dist/mermaid.min.js.map) names every bundled module with its exact",
        "    name@version; the bundled @mermaid-js/parser chunks name theirs in esbuild",
        f"    module comments. Every package was checked against Mermaid's pnpm-lock.yaml",
        f"    at {MERMAID['tag']} (commit {MERMAID['commit']}).",
        "  * Prebuilt bundles (cytoscape, roughjs, @upsetjs/venn.js, dagre, ...) were",
        "    scanned from their original sources in the exact tarball; the versions of",
        "    the packages they inline come from that project's lockfile at the commit",
        "    the npm release was published from.",
        "  * Adapted snippets are identified by the banners upstream retained; the KaTeX",
        "    fonts' notices come from the fonts' own name tables.",
        "",
        "Licence election: DOMPurify is offered under MPL-2.0 OR Apache-2.0; okf-loom",
        "redistributes it under the Apache License, Version 2.0 (its LICENSE file below",
        "contains the full Apache-2.0 text; the package ships no NOTICE file).",
        "",
        f"Inventory: {len(entries)} packages, {len(snippets)} adapted snippets, 1 font family.",
        "",
    ]
    width = max(len(entry["id"]) for entry in entries) + 2
    for entry in entries:
        out.append(f"  {entry['id'].ljust(width)}{entry['license'] or '(not declared; see its licence file)'}")
    out.append("")
    for entry in entries:
        out += ["", RULE, f"PACKAGE {entry['id']}", RULE,
                f"Declared licence: {entry['license'] or '(none in package.json; the licence file below applies)'}"]
        if entry["name"] == "dompurify":
            out.append("Redistributed under: Apache-2.0 (election; see above)")
        out += [f"Included in: {', '.join(entry['included_in'])}"]
        out += [f"Evidence: {line}" for line in entry["evidence"]]
        if entry.get("attribution"):
            out += [f"Copyright notice (verbatim from {entry['attribution']['source']}): "
                    f"{entry['attribution']['text']}"]
        if entry["tarball"]:
            out += [f"Tarball: {entry['tarball']}", f"Integrity: {entry['integrity']}"]
        for licence in entry["licence_files"]:
            out += ["", f"--- {licence['path']} (sha256 {licence['sha256']}) ---", "",
                    as_quoted(licence["text"])]
        out.append(f"END PACKAGE {entry['id']}")
    for item in snippets:
        out += ["", RULE, f"SNIPPET {item['id']}", RULE, f"{item['title']}",
                f"Found in: {item['found_in']}", f"Included in: {', '.join(item['included_in'])}",
                f"Banner retained verbatim in: {', '.join(item['banner_retained_in']) or '(stripped by minifiers)'}",
                "", "--- upstream attribution (verbatim) ---", "", item["banner"]]
        if item.get("gap"):
            out += ["", f"Licence: NOT ESTABLISHED -- {item['gap']}"]
        else:
            out += ["", f"--- licence terms: {item['license']} ({item['terms_source']}) ---", ""]
            if item["terms_source"].startswith("https://"):
                out += ["(SPDX licence template: its copyright placeholder refers to the upstream",
                        "attribution quoted above.)", ""]
            out += [item["terms_text"].rstrip("\n")]
        out.append(f"END SNIPPET {item['id']}")
    out += ["", RULE, f"FONTS {fonts['id']}", RULE,
            f"Licence: {fonts['license']}", f"Included in: {', '.join(fonts['included_in'])}",
            *[f"Evidence: {line}" for line in fonts["evidence"]],
            f"Tarball: {fonts['tarball']}", f"Integrity: {fonts['integrity']}"]
    for notice in fonts["notices"]:
        out += ["", f"--- notice of {', '.join(notice['faces'])} (font name table) ---", "", notice["text"]]
    out += ["", f"--- licence terms: SIL Open Font License 1.1 ({fonts['terms_source']}) ---", "",
            fonts["terms_text"].rstrip("\n"), f"END FONTS {fonts['id']}", ""]
    text = "\n".join(line.rstrip(" \t") for line in "\n".join(out).replace("\r\n", "\n").split("\n"))
    if "\r" in text:
        raise SystemExit("notices text contains a bare carriage return")
    lowered = text.lower()
    if "</script" in lowered or "<!--" in lowered:
        raise SystemExit("notices text cannot be embedded in a single-file export")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="regenerate in memory and compare (default)")
    mode.add_argument("--write", action="store_true", help="rewrite the notices and inventory files")
    args = parser.parse_args(argv)
    notices, inventory = build()
    notices_path = vendor.VENDOR_ROOT / NOTICES_REL
    if args.write:
        fva._atomic_write(notices_path, notices.encode("utf-8"))
        fva._atomic_write(INVENTORY_PATH, inventory.encode("utf-8"))
        print(f"wrote {notices_path} ({len(notices.encode('utf-8'))} bytes) and {INVENTORY_PATH}")
        return 0
    stale = [str(path) for path, text in ((notices_path, notices), (INVENTORY_PATH, inventory))
             if not path.is_file() or path.read_bytes() != text.encode("utf-8")]
    if stale:
        print("bundled notices are stale: " + ", ".join(stale), file=sys.stderr)
        return 1
    print("bundled notices reproduce byte for byte")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
