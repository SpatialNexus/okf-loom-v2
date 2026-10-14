"""Live HTTP wiki server for OKF bundles.

Public API:
    run_server(bundle_root, *, host, port, watch, open_browser) -> None

The request handler ``OKFWikiHandler`` is exposed for harness embedding:
mount it on any ``http.server`` server subclass and attach a ``bundle``
attribute (plus optional ``config`` / ``state``) before serving.

Stdlib-only (``http.server``, ``socketserver``, ``threading``, ``urllib``).
No Flask/FastAPI/Jinja2/Markdown/watchdog.

Routes:
    /                       root index (top-level concepts + subdirs)
    /<concept_id>           concept page (rendered markdown + panels)
    /<dir>/index.md         directory index page
    /index.md               root index page (alias for /)
    /__graph                full-page Cytoscape.js graph view
    /__search?q=...         search results page (HTML or JSON if ?format=json)
    /__raw/<concept_id>     raw markdown body
    /__data/summary?id=...  one concept's title/type/description card JSON
    /__data/graph.json      graph JSON (for external tools / harness embed)
    /__data/content.json    content index JSON
    /__static/<file>        static asset (CSS / JS)
    /<path>.<media ext>     bundle-local media file (png/jpg/webp/gif/svg/
                            mp4/webm/pdf …) — §5-visible + contained only
"""
from __future__ import annotations

import json
import html
import os
import posixpath
import queue
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser
import hashlib
import hmac
import math
import base64
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from .model import Bundle, Concept
from .parse import parse_document
from .exceptions import OKFParseError
from .paths import ConceptId, ConceptIdError, concept_id_from_str, concept_id_to_str, concept_id_to_path
from .io_utils import path_lock
from .studio import Studio, rev_of, CommentMutationError, CommentSummaryUnavailable
from .presence import PresenceUnavailable, PresenceCapacity, valid_focus, valid_label
from .annotations import AnchorError, capture_anchor, concept_content_rev, parse_v2_anchor, resolve_anchor
from .render import (
    build_graph_data,
    _render_concept_page,
    _render_index_page,
    _render_search_page,
    _render_graph_page,
    _render_not_found_page,
    _content_index_json,
    render_feedback_import_page,
)
from .viewer.assets import (
    BUNDLE_MEDIA_EXTENSIONS,
    list_served_static,
    load_config,
    load_static,
    resolve_palette,
    effective_allow_active_code,
    clear_overrides_cache,
)
from .viewer.markdown import markdown_to_html, rewrite_internal_links, url_for_concept

_STDLIB_THREADING_HTTP_SERVER = ThreadingHTTPServer

# ---------------------------------------------------------------------------
# DoS size caps (P2-55). The authoritative YAML/body size cap lives in
# ``parse.py`` (NOT this file — REPORTED: ``safe_load`` alias expansion /
# billion-laughs must be capped at the parser boundary). As defence in
# depth, the server caps the size of bodies it ships over HTTP so a single
# huge concept body cannot saturate a request handler. The cap is generous
# (16 MiB) — it is a backstop, not the primary control.
# ---------------------------------------------------------------------------
MAX_RAW_RESPONSE_BYTES: int = 16 * 1024 * 1024  # 16 MiB cap on /__raw bodies.


def _prefers_html_error(accept: str) -> bool:
    """Opt document errors into HTML only for an explicit HTML preference.

    Absent/wildcard Accept and equally preferred plain/JSON retain the legacy
    text response. This does not negotiate API routes (their format contract
    is owned by their handlers). Ambiguous/invalid weights stay non-HTML.
    """
    weights: dict[str, float] = {}
    for item in accept.split(","):
        media, *params = [part.strip().lower() for part in item.split(";")]
        if media not in {"text/html", "text/plain", "application/json", "text/*", "application/*", "*/*"}:
            continue
        quality = "1"
        seen_q = False
        for param in params:
            key, sep, value = param.partition("=")
            if key.strip() == "q":
                if seen_q or not sep or not re.fullmatch(r"(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)", value.strip()):
                    return False
                quality, seen_q = value.strip(), True
        if media in weights:
            return False
        weights[media] = float(quality)
    if weights.get("text/html", 0) <= 0:
        return False

    def preference(media: str) -> tuple[float, int]:
        for specificity, pattern in ((2, media), (1, media.split("/")[0] + "/*"), (0, "*/*")):
            if pattern in weights:
                return weights[pattern], specificity
        return 0, 0

    return preference("text/html") > max(preference("text/plain"), preference("application/json"))


def _canonical_annotation_source(bundle: Bundle, cid: ConceptId) -> str:
    """Read canonical bytes by trusted root + concept identity, never stale paths."""
    return _canonical_document_source(bundle, "/".join(cid) + ".md")


def _search_section_fragments(
    rows: list[dict[str, Any]], service: Any, revision: str, query: str,
) -> dict[str, dict[str, str]]:
    """Verified section destinations for the HTML search page only.

    R1: the visible ``Open matching section`` action must rest on actual
    textual evidence plus source-owned heading proof. This adapter therefore
    requires ALL of (a) the row's attached evidence passage to contain at
    least one query term (the same tokenize/hit rule the ranking uses, so a
    graph-only result never claims a section), (b) the passage's concept
    identity to equal the row's concept, (c) the revision-bound
    ``verified_section_fragment`` proof to return a rendered heading id, and
    (d) that id to carry the renderer-owned display label the destination
    re-admission will demand. Each claimed destination carries the concept
    identity and that concept's per-document normalized-source revision
    (:func:`annotations.concept_content_rev` of the concept's LF-normalized
    raw source), which the destination page re-proves independently.
    Failures of any kind simply omit the action;
    ordinary Open links and the strict JSON wire rows are never touched.
    """
    from .annotations import concept_content_rev
    from .navigation import _concept_section_prover
    from .search import tokenize

    terms = [term.lower() for term in tokenize(query)]
    if not terms:
        return {}
    sections: dict[str, dict[str, str]] = {}
    provers: dict[str, tuple] = {}
    for row in rows:
        cid = row.get("concept_id") or ""
        evidence = row.get("evidence") or {}
        passage = evidence.get("passage") if isinstance(evidence, dict) else None
        if not isinstance(passage, dict) or not passage.get("passage_id"):
            continue
        if passage.get("concept_id") != cid:
            # Cross-concept passage/row identity must match explicitly; a
            # mismatched evidence passage never earns a section claim.
            continue
        text = ((passage.get("heading") or "") + "\n"
                + (passage.get("excerpt") or "")).lower()
        if not any(text.count(term) for term in terms):
            continue
        fragment = service.verified_section_fragment(
            cid, passage["passage_id"], revision=revision)
        if not fragment:
            continue
        prover_entry = provers.get(cid)
        if prover_entry is None:
            try:
                concept = service.bundle.concepts.get(concept_id_from_str(cid))
            except ConceptIdError:
                concept = None
            prover = _concept_section_prover(concept) if concept else None
            concept_revision = (
                concept_content_rev(concept.raw_text.replace("\r\n", "\n"))
                if concept is not None and prover is not None else None)
            prover_entry = (prover, concept_revision)
            provers[cid] = prover_entry
        prover, concept_revision = prover_entry
        label = prover[1](fragment) if prover and concept_revision else ""
        if not label:
            continue
        sections[cid] = {
            "concept": cid,
            "fragment": fragment,
            "heading": label,
            "source_revision": concept_revision,
        }
    return sections


def _canonical_document_source(bundle: Bundle, document: str) -> str:
    """Strictly decoded canonical source (see :mod:`canonical_source`)."""
    from .canonical_source import read_canonical_document_bytes
    return read_canonical_document_bytes(
        bundle, document, max_bytes=MAX_RAW_RESPONSE_BYTES,
    ).decode("utf-8", errors="strict")

# Defense-in-depth cap on the length of a ``/__search?q=`` query string
# (iter1 P3-5). The lexical/semantic-lite backends tokenize + score the query
# in-process; an unbounded multi-MB query could pin a ThreadingHTTPServer
# worker. 4096 chars is far beyond any realistic knowledge-base query (and
# beyond what the URL-length-aware browsers accept in a querystring anyway).
MAX_SEARCH_QUERY_CHARS: int = 4096
DEFAULT_RETRIEVAL_CONCURRENCY: int = 4
RETRIEVAL_DEADLINE_SECONDS: float = 30.0
_RETRIEVAL_GATE_INIT_LOCK = threading.Lock()


def _retrieval_runtime(owner: Any) -> tuple[threading.BoundedSemaphore, ThreadPoolExecutor]:
    """Return the one retrieval runtime owned by an application server."""
    with _RETRIEVAL_GATE_INIT_LOCK:
        if getattr(owner, "_retrieval_closed", False):
            from .retrieval import RetrievalContractError
            raise RetrievalContractError(
                "retrieval_unavailable", "Retrieval is unavailable", status=503
            )
        gate = owner.__dict__.get("_retrieval_gate")
        executor = owner.__dict__.get("_retrieval_executor")
        if gate is None or executor is None:
            limit = max(1, int(getattr(
                owner, "retrieval_concurrency", DEFAULT_RETRIEVAL_CONCURRENCY
            )))
            gate = threading.BoundedSemaphore(limit)
            executor = ThreadPoolExecutor(
                max_workers=limit, thread_name_prefix="okf-retrieval"
            )
            owner._retrieval_gate = gate
            owner._retrieval_executor = executor
        return gate, executor


def _shutdown_retrieval_runtime(owner: Any) -> None:
    """Fence creation, cancel queued work, and join admitted retrieval work."""
    with _RETRIEVAL_GATE_INIT_LOCK:
        already_closed = getattr(owner, "_retrieval_closed", False)
        owner._retrieval_closed = True
        executor = owner.__dict__.get("_retrieval_executor")
    if not already_closed and executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)

# Studio caps (current spec §13). max_sse_clients bounds the
# worker threads tied up by long-lived SSE streams under ThreadingHTTPServer;
# MAX_EVENT_BODY_BYTES caps a POST /__comment / /__apply / /__undo body so a
# huge payload cannot pin a handler thread (defence in depth — the real size
# guard for rendered markdown lives in parse.py / markdown.py).
DEFAULT_MAX_SSE_CLIENTS: int = 32
MAX_EVENT_BODY_BYTES: int = 1 * 1024 * 1024  # 1 MiB cap on studio POST bodies.
REQUEST_BODY_DEADLINE_SECONDS: float = 5.0

# Public quick-tunnel holders receive a capability scoped to durable comment
# and content mutations.  Server/session administration, proposal review, and
# ephemeral presence control are deliberately outside this set.
_PUBLIC_MUTATION_ROUTES = frozenset({
    "/__comment",
    "/__comment-update",
    "/__comment-reanchor",
    "/__apply",
    "/__undo",
})

# Public GET access is explicit for internal routes. Ordinary concept,
# directory, and media paths remain readable, while every unlisted ``/__*``
# route is treated as operator-only. Keep this in sync with the documented GET
# surfaces rather than growing a denylist that can miss new administration
# endpoints.
_PUBLIC_GET_INTERNAL_ROUTES = frozenset({
    "/__search",
    "/__search-form",
    "/__context",
    "/__events",
    "/__data/doc",
    "/__data/events",
    "/__comments",
    "/__data/page",
    "/__graph",
    "/__graph.html",
    "/__data/graph.json",
    "/__data/content.json",
    "/__data/summary",
})
_PUBLIC_GET_INTERNAL_PREFIXES = ("/__raw/", "/__static/")

_STUDIO_POST_ROUTES = frozenset({
    "/__comment", "/__comment-update", "/__comment-reanchor", "/__presence",
    "/__apply", "/__undo", "/__preview", "/__tunnel",
})

# Portable feedback import (X2) is a LOCAL-OPERATOR-only surface. These routes
# are deliberately absent from every public allowlist above and additionally
# refuse a tunnel-attached or non-loopback session (``local_only``). They take
# the raw note bytes (never the generic JSON parse) under a 32 KiB cap.
_FEEDBACK_IMPORT_PREVIEW = "/__feedback-import/preview"
_FEEDBACK_IMPORT_CONFIRM = "/__feedback-import/confirm"
_FEEDBACK_IMPORT_ROUTES = frozenset({_FEEDBACK_IMPORT_PREVIEW, _FEEDBACK_IMPORT_CONFIRM})
FEEDBACK_IMPORT_BODY_CAP: int = 32 * 1024
_FEEDBACK_PREVIEW_KEY_LOCK = threading.Lock()


def _feedback_preview_key(owner: Any) -> bytes:
    """Per-process preview-token HMAC key owned by the application server."""
    with _FEEDBACK_PREVIEW_KEY_LOCK:
        key = owner.__dict__.get("_portable_feedback_preview_key")
        if key is None:
            from .portable_feedback import new_preview_key
            key = new_preview_key()
            owner._portable_feedback_preview_key = key
        return key


@dataclass(frozen=True)
class _RequestAdmission:
    """Body-independent request decision carried into application dispatch."""

    method: str
    path: str
    content_length: int
    body_cap: int
    proposal: Any = None
    consume_reanchor_rate: bool = False

# CSP for bundle-served media (extensions in
# ``viewer.assets.BUNDLE_MEDIA_EXTENSIONS`` — the shared serve/build
# allowlist). Bundle files are USER CONTENT: a bundle SVG
# can carry <script>, and navigating to it directly would otherwise execute
# in the studio origin — where the per-session token is embedded in every
# HTML page. ``sandbox`` gives direct navigation a unique opaque origin with
# scripting disabled; <img>/<video> embedding is unaffected by the response
# CSP. img/media/style allowances keep the browsers' built-in direct-view
# pages (which wrap the file in a tiny document) rendering.
_BUNDLE_ASSET_CSP: str = (
    "default-src 'none'; "
    "img-src 'self' data:; "
    "media-src 'self'; "
    "style-src 'unsafe-inline'; "
    "sandbox"
)

# ---------------------------------------------------------------------------
# Plugin hook (entry-point loader in current spec §15; see okf_loom.viewer.plugins)
# ---------------------------------------------------------------------------

from typing import Protocol, runtime_checkable


@runtime_checkable
class ViewerPlugin(Protocol):
    """Hook points for viewer plugins.

    The current loader discovers concrete plugins via Python entry points in the group
    ``"okf_loom.viewer_plugins"`` (see
    :mod:`okf_loom.viewer.plugins`). The hook contract below is the
    minimum surface; concrete plugins MUST implement ``on_concept_render``
    and MAY implement ``on_index_render``. The composite returned by
    ``build_viewer_plugin`` honours the §11 active-code gate and skips
    plugins that raise (log to stderr, never crash the viewer).

    Embedders can still attach a custom plugin instance via
    ``server.plugin`` after construction; that bypasses entry-point
    discovery.
    """

    def on_concept_render(self, concept: Concept, html: str) -> str:
        """Called after a concept page body is rendered. Return possibly
        modified HTML (e.g. to inject a widget, badges, or telemetry)."""
        ...

    def on_index_render(self, html: str) -> str:
        """Called after an index page is rendered."""
        ...


class NoOpPlugin:
    """Default plugin: passes values through unchanged."""

    def on_concept_render(self, concept: Concept, html: str) -> str:
        return html

    def on_index_render(self, html: str) -> str:
        return html


# ---------------------------------------------------------------------------
# Bundle watcher
# ---------------------------------------------------------------------------

class _BundleWatcher(threading.Thread):
    """Polls the bundle dir for ``.md`` mtime changes and reloads the bundle.

    Uses ``os.stat`` rather than watchdog, per the hard-constraint list.

    Reload safety (P1-37): the snapshot is advanced ONLY after a successful
    reload. On failure the exception is logged to stderr (with the failing
    path) and the snapshot is left untouched so the next tick retries — a
    transient malformed save (e.g. a half-written file) is not silently
    consumed and the browser never shows stale content with no signal.
    """

    def __init__(self, bundle_root: Path, reload_fn, *,
                 interval: float = 1.0, tick_fn=None) -> None:
        super().__init__(daemon=True, name="okf-watcher")
        self._root = Path(bundle_root)
        self._reload_fn = reload_fn
        self._tick_fn = tick_fn  # fires EVERY tick (tail + sweep), even when .md unchanged
        self._interval = interval
        # NOTE: named ``_stop_event`` (not ``_stop``) to avoid shadowing
        # ``threading.Thread._stop()``, which the base class invokes during
        # ``join()``. The previous ``self._stop = Event()`` shadow broke
        # ``join()`` once the thread had terminated (TypeError: 'Event'
        # object is not callable) — hidden in production because the watcher
        # is a daemon whose join() is never called, but exposed by tests
        # and by any embedding harness that shuts the watcher down cleanly.
        self._stop_event = threading.Event()
        self._snapshot = self._scan()

    def _scan(self) -> dict[Path, float]:
        snap: dict[Path, float] = {}
        if not self._root.is_dir():
            return snap
        # Same exclusion semantics as Bundle.load (spec §5): default
        # excludes + .gitignore + bundle.exclude. Config and .gitignore are
        # re-read every tick (cheap small files; the dir pruning makes the
        # scan far cheaper than the old rglob), so serving a workspace root
        # live-updates when excludes — or new OKF files — appear.
        from .config import OkfConfig, OkfConfigError
        from .ignore import iter_markdown_files
        try:
            bundle_cfg = OkfConfig.load(self._root).bundle
        except OkfConfigError:
            from .config import BundleConfig
            bundle_cfg = BundleConfig()  # Bundle.load warns; keep scanning
        for p in iter_markdown_files(
            self._root,
            exclude=bundle_cfg.exclude,
            include=bundle_cfg.include,
            respect_gitignore=bundle_cfg.respect_gitignore,
        ):
            try:
                snap[p] = p.stat().st_mtime
            except OSError:
                continue
        # P2-6: also track the bundle config + viewer override config/palette,
        # so an operator editing okf-loom.config.yaml (e.g. flipping
        # allow_active_code false->true for lock-down) triggers a reload +
        # cache invalidation without needing to also touch an .md file.
        from .config import CONFIG_FILENAME
        for extra in (
            self._root / CONFIG_FILENAME,
            self._root / ".okf-loom" / "viewer" / "config.json",
            self._root / ".okf-loom" / "viewer" / "palette.json",
        ):
            try:
                if extra.is_file():
                    snap[extra] = extra.stat().st_mtime
            except OSError:
                continue
        return snap

    def run(self) -> None:
        while not self._stop_event.wait(self._interval):
            # ``Event.wait`` may time out immediately before another thread
            # requests shutdown.  Recheck at every callback boundary so a
            # stopped watcher never starts new Studio-owning work.
            if self._stop_event.is_set():
                break
            # ARCH4-001 fix: fire the tick callback EVERY tick, BEFORE the
            # snapshot check. The tick handles session-feed events.jsonl
            # mtime → cross-process SSE tail + staleness sweep — these must
            # fire even when .md files haven't changed (e.g. a CLI claim/
            # resolve/presence write only touches session files).
            if self._tick_fn is not None:
                try:
                    self._tick_fn()
                except Exception as e:
                    print(
                        f"okf: watcher tick failed: {e}",
                        file=sys.stderr,
                    )
            if self._stop_event.is_set():
                break
            snap = self._scan()
            if self._stop_event.is_set():
                break
            if snap == self._snapshot:
                continue
            # Snapshot changed → attempt reload. Only advance self._snapshot
            # AFTER reload succeeds so a transient failure (malformed save,
            # disk hiccup, permission blip) is retried on the next tick
            # instead of being silently swallowed.
            try:
                self._reload_fn()
            except Exception as e:  # noqa: BLE001 — reload errors must not kill the thread
                # Log every swallowed reload exception with the failing path
                # so an operator running `okf serve` sees WHY the browser is
                # showing the previous bundle (P1-37 double-silent fix).
                print(
                    f"okf: bundle reload failed for {self._root} "
                    f"(keeping previous bundle; will retry on next tick): {e}",
                    file=sys.stderr,
                )
                continue  # do NOT advance snapshot → next tick retries.
            self._snapshot = snap

    def stop(self) -> None:
        self.request_stop()

    def request_stop(self) -> None:
        """Fence callbacks without waiting for an admitted callback."""
        self._stop_event.set()


class _PresenceSweeper(threading.Thread):
    """Single server-owned expiry loop, independent of bundle watching."""
    def __init__(self, studio: Studio, interval: float = 1.0) -> None:
        super().__init__(daemon=True, name="okf-presence-sweeper")
        self._studio, self._interval = studio, interval
        self._stop_event = threading.Event()
    def run(self) -> None:
        while not self._stop_event.wait(self._interval):
            # Fence the timeout/set race before entering Studio.  A sweep
            # already in progress is allowed to complete and is joined by
            # ``stop``; a pending sweep is cancelled.
            if self._stop_event.is_set():
                break
            try:
                self._studio.sweep_stale_presence_and_claims()
            except (PresenceUnavailable, PresenceCapacity):
                pass
            except Exception as e:  # noqa: BLE001 — one bad sweep must not kill liveness
                print(f"okf: presence sweep failed: {e}", file=sys.stderr)
    def stop(self) -> None:
        """Fence new sweeps and await any admitted sweep, idempotently."""
        self.request_stop()
        if self.ident is not None and self is not threading.current_thread():
            self.join()

    def request_stop(self) -> None:
        """Fence new sweeps without waiting for an admitted sweep."""
        self._stop_event.set()


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class OKFWikiHandler(BaseHTTPRequestHandler):
    _presence_rate_lock = threading.Lock()
    _reanchor_rate_init_lock = threading.Lock()
    """HTTP request handler. Reads ``self.server.bundle`` for the live bundle.

    A harness may also set ``self.server.config``, ``self.server.palette``,
    ``self.server.name``, and ``self.server.plugin``. If unset, they are
    lazily computed on first request.

    Reload safety (P2-46): ``run_server`` attaches a ``server._state_lock``
    (a :class:`threading.Lock`). The watcher's reload mutates ``bundle``,
    ``_palette``, and ``plugin`` under that lock, and handlers snapshot all
    three once per request via :meth:`_state_snapshot` (under the same lock)
    so a concurrent reload cannot hand the handler a torn view (e.g. the new
    bundle with the old palette). Harness-embedded servers without the lock
    fall back to direct-attribute reads (single-threaded embedding is safe).
    """

    server_version = "OKFWiki/0.1"
    # HTTP/1.1 enables keep-alive (Connection: keep-alive) and is required for
    # the /__events SSE stream to hold a connection open without a
    # Content-Length. Every non-streaming response sends Content-Length, so
    # keep-alive is safe for them; SSE streams omit it and hold the thread.
    protocol_version = "HTTP/1.1"

    def handle_one_request(self) -> None:
        """Parse one request/body, then cross the application admission fence."""
        admitted = False
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(414)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            if not self._preflight_request():
                return
            if not self._prepare_request_body():
                return
            admit = getattr(self.server, "admit_http_dispatch", None)
            if callable(admit) and not admit(self.request):
                self.close_connection = True
                self._send_text(
                    503, "Server is shutting down.",
                    content_type="text/plain; charset=utf-8", close=True,
                )
                self.wfile.flush()
                return
            admitted = callable(admit)
            method = getattr(self, "do_" + self.command, None)
            if method is None:
                self.send_error(501, f"Unsupported method ({self.command!r})")
                return
            method()
            self.wfile.flush()
        except TimeoutError as exc:
            self.log_error("Request timed out: %r", exc)
            self.close_connection = True
        finally:
            if admitted:
                release = getattr(self.server, "release_http_dispatch", None)
                if callable(release) and release(self.request):
                    self.close_connection = True

    def _prepare_request_body(self) -> bool:
        """Bound transport reads before admitting application execution."""
        self._request_body = None
        if self.command not in {"POST", "PUT", "PATCH", "DELETE"}:
            return True
        if self.headers.get("Transfer-Encoding"):
            return True
        admission = getattr(self, "_request_admission", None)
        raw_length = self.headers.get("Content-Length")
        try:
            length = (admission.content_length if admission is not None
                      else int(raw_length or 0))
            if length < 0:
                raise ValueError
        except (TypeError, ValueError):
            return True
        cap = admission.body_cap if admission is not None else MAX_EVENT_BODY_BYTES
        if length > cap:
            return True
        if length == 0:
            self._request_body = b""
            return True

        timeout_s = float(getattr(
            self.server, "request_body_deadline_s", REQUEST_BODY_DEADLINE_SECONDS,
        ))
        deadline = time.monotonic() + max(0.001, timeout_s)
        previous_timeout = self.connection.gettimeout()
        chunks: list[bytes] = []
        remaining = length
        try:
            while remaining:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    raise TimeoutError("request body deadline elapsed")
                self.connection.settimeout(budget)
                read1 = getattr(self.rfile, "read1", None)
                chunk = (read1(min(remaining, 64 * 1024)) if callable(read1)
                         else self.rfile.read(min(remaining, 64 * 1024)))
                if not chunk:
                    self.close_connection = True
                    try:
                        self._send_contract_error(
                            400, "incomplete_request_body",
                            "Request body ended before Content-Length bytes were received.",
                            close=True,
                        )
                        self.wfile.flush()
                    except OSError:
                        pass
                    return False
                chunks.append(chunk)
                remaining -= len(chunk)
        except (TimeoutError, socket.timeout):
            self.close_connection = True
            try:
                self._send_contract_error(
                    408, "request_body_timeout",
                    "Request body was not received before the deadline.", close=True,
                )
                self.wfile.flush()
            except OSError:
                pass
            return False
        except OSError:
            # Terminal shutdown closes pre-dispatch sockets. No application
            # execution was admitted, so there is no late mutation to settle.
            self.close_connection = True
            return False
        finally:
            try:
                self.connection.settimeout(previous_timeout)
            except OSError:
                pass
        self._request_body = b"".join(chunks)
        return True

    def _preflight_request(self) -> bool:
        """Admit method/path/auth/headers and an effective cap before body I/O."""
        self._request_admission = None
        if self.command not in {"POST", "PUT", "PATCH", "DELETE"}:
            return True

        # Dedicated public ingress is a listener-owned trust boundary. Reject
        # forbidden unsafe requests before inspecting or waiting for a body.
        if self._is_public_tunnel_request() and self.command != "POST":
            self._send_text(
                403, "Public ingress cannot access this mutation route.",
                content_type="text/plain; charset=utf-8", close=True,
            )
            return False
        if self._is_public_tunnel_request() and (
                self._public_tunnel_request_is_read_only()
                or not self._public_mutation_route()):
            self._send_text(
                403, "Public ingress cannot access this mutation route.",
                content_type="text/plain; charset=utf-8", close=True,
            )
            return False

        from .proposal_review.http import admit as admit_proposal
        proposal = admit_proposal(self, self.command)
        if proposal is False:
            return False
        if proposal is not None:
            self._request_admission = _RequestAdmission(
                self.command, urlparse(self.path).path, proposal.content_length,
                proposal.body_cap or MAX_EVENT_BODY_BYTES, proposal,
            )
            return True
        if self.command != "POST":
            # Proposal Review owns its reserved prefix above. Every other
            # local unsafe method is unsupported, so do not buffer a body
            # merely to reach the stdlib's existing 501 response.
            self.close_connection = True
            self.send_error(501, f"Unsupported method ({self.command!r})")
            return False

        from .retrieval import MAX_REQUEST_TARGET_BYTES, strict_decode_path
        early_path = urlparse(self.path).path
        reanchor = early_path == "/__comment-reanchor"
        if reanchor:
            # Preserve the route's non-disclosing precedence. The rate check
            # is observational; quota is consumed only after a complete body.
            if not self._check_write_auth():
                self._send_contract_error(403, "forbidden", "The request is not permitted.", close=True)
                return False
            if self._reanchor_rate_unavailable():
                self._send_json(429, {"ok": False, "error": {
                    "code": "rate_limited", "message": "Too many requests.", "fields": {}
                }}, extra_headers={"Retry-After": "60"}, close=True)
                return False
            if not getattr(self.server, "studio_edit", True):
                self._send_contract_error(403, "read_only", "The studio is read-only.", close=True)
                return False
        if len(self.path.encode("utf-8", errors="replace")) > MAX_REQUEST_TARGET_BYTES:
            if reanchor:
                self._send_contract_error(400, "invalid_request", "The request target is invalid.", close=True)
            else:
                self._send_contract_error(414, "request_target_too_large", "Request target exceeds the size limit", close=True)
            return False
        if self.headers.get("Transfer-Encoding"):
            if reanchor:
                self._send_contract_error(400, "invalid_request", "Transfer-Encoding is not supported.", close=True)
            else:
                self._send_contract_error(400, "unsupported_transfer_encoding", "Chunked request bodies are not supported", close=True)
            return False
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise ValueError
        except (TypeError, ValueError):
            if reanchor:
                self._send_contract_error(400, "invalid_request", "Content-Length is invalid.", close=True)
            else:
                self._send_contract_error(400, "invalid_content_length", "Content-Length must be a non-negative integer", close=True)
            return False
        try:
            path = strict_decode_path(urlparse(self.path).path)
        except Exception as exc:
            # Preserve the retrieval route's existing typed target response.
            if hasattr(exc, "status") and hasattr(exc, "as_error"):
                self._send_json(exc.status, exc.as_error(), close=True)
            else:
                raise
            return False
        if path in _FEEDBACK_IMPORT_ROUTES:
            # Local-operator authority precedes the size diagnostic; both are
            # decided before any body byte is read.
            if self._feedback_import_refused():
                return False
            if length > FEEDBACK_IMPORT_BODY_CAP:
                self._send_contract_error(
                    413, "note_too_large", "The note exceeds 32 KiB.", close=True)
                return False
            self._request_admission = _RequestAdmission(
                self.command, path, length, FEEDBACK_IMPORT_BODY_CAP)
            return True
        cap = 65_536 if reanchor else MAX_EVENT_BODY_BYTES
        if length > cap:
            if reanchor:
                self._send_contract_error(413, "invalid_request", "Request body exceeds 65,536 bytes.", close=True)
            else:
                self._send_text(413, "Request entity too large.",
                                content_type="text/plain; charset=utf-8", close=True)
            return False
        if not reanchor and not self._check_write_auth():
            self._send_text(403, "Forbidden: invalid or missing cross-origin token.",
                            content_type="text/plain; charset=utf-8", close=True)
            return False
        if not reanchor:
            # Ordinary mutation precedence remains auth -> availability ->
            # mode. /__tunnel is the established read-only-mode exception.
            if self.studio is None:
                self._send_text(503, "Studio not enabled on this server.",
                                content_type="text/plain; charset=utf-8", close=True)
                return False
            if (not getattr(self.server, "studio_edit", True)
                    and path != "/__tunnel"):
                self._send_text(
                    403, "Read-only kiosk: commenting/directing is disabled (--no-edit).",
                    content_type="text/plain; charset=utf-8", close=True,
                )
                return False
        if path not in _STUDIO_POST_ROUTES:
            self._send_text(404, "Not found", content_type="text/plain; charset=utf-8", close=True)
            return False
        self._request_admission = _RequestAdmission(
            self.command, path, length, cap, consume_reanchor_rate=reanchor,
        )
        return True

    def _read_request_body(self, length: int) -> bytes:
        """Return the complete transport-owned body, preserving embedder parity."""
        body = getattr(self, "_request_body", None)
        if body is not None:
            if len(body) != length:
                raise ValueError("prepared request body length mismatch")
            return body
        return self.rfile.read(length) if length > 0 else b""

    # --- Convenience accessors --------------------------------------------
    def _application_server(self) -> Any:
        """Return the primary application/session state owner for this listener."""
        if self._is_public_tunnel_request():
            # Production public listeners always attach this explicit owner.
            # The fallback preserves handler-only embedding tests that classify
            # a synthetic listener as public without constructing an ingress.
            return self.server.__dict__.get("app_server", self.server)
        return self.server

    @property
    def studio(self) -> Studio | None:
        return getattr(self._application_server(), "studio", None)

    @property
    def csrf_token(self) -> str | None:
        return getattr(self._application_server(), "csrf_token", None)

    def _is_public_tunnel_request(self) -> bool:
        """Return whether the accepted socket is the dedicated public ingress.

        This is listener identity, not a request-header classification.  The
        public client controls Host, CF-* and X-Forwarded-*; none can turn a
        connection accepted by this listener into a local/admin request.
        """
        return bool(getattr(self.server, "_okf_public_ingress", False))

    def _public_ingress_is_active(self) -> bool:
        if not self._is_public_tunnel_request():
            return False
        app = getattr(self.server, "app_server", None)
        generation = getattr(self.server, "_okf_public_ingress_generation", None)
        return bool(
            app is not None
            and generation
            and getattr(app, "public_ingress_server", None) is self.server
            and secrets.compare_digest(
                generation, getattr(app, "public_ingress_generation", "")
            )
        )

    def _public_mutation_route(self) -> bool:
        try:
            path = urlparse(self.path).path
            return path in _PUBLIC_MUTATION_ROUTES if getattr(self.server, 'public_tunnel_edit', False) else (
                path == '/__comment' and getattr(self.server, 'public_tunnel_comment', False))
        except ValueError:
            return False

    def _public_mutation_authorized_locked(self, path: str) -> tuple[Any, str] | None:
        """Verify public mutation authority while holding the lifecycle lock."""
        if not self._is_public_tunnel_request() or path not in _PUBLIC_MUTATION_ROUTES:
            return None
        ingress = self.server
        app = getattr(ingress, "app_server", None)
        generation = getattr(ingress, "_okf_public_ingress_generation", None)
        if not app or not generation:
            return None
        if (getattr(app, "public_ingress_server", None) is not ingress
                or getattr(app, "public_ingress_generation", None) != generation
                or not getattr(app, "public_ingress_accepting", False)
                or getattr(app, "public_ingress_accepting_generation", None) != generation
                or not self._public_mutation_route()):
            return None
        token = getattr(app, "public_mutation_token", None) if getattr(app, 'public_tunnel_edit', False) else getattr(app, 'public_comment_token', None)
        if not token or not secrets.compare_digest(
                self.headers.get("X-OKF-Token", ""), token):
            return None
        tunnel_url = getattr(app, "tunnel_url", None)
        try:
            expected_host = (urlparse(tunnel_url or "").hostname or "").lower()
            request_host = (
                urlparse("//" + self.headers.get("Host", "")).hostname or ""
            ).lower()
        except ValueError:
            return None
        if not expected_host or request_host != expected_host:
            return None
        origin = self.headers.get("Origin")
        if origin:
            try:
                if (urlparse(origin).hostname or "").lower() != expected_host:
                    return None
            except ValueError:
                return None
        return app, generation

    def _acquire_public_mutation_lease(self, path: str) -> tuple[Any, str] | None:
        """Atomically authorize and admit one generation-scoped mutation."""
        app = getattr(self.server, "app_server", None)
        if app is None:
            return None
        condition = _ensure_public_mutation_lifecycle(app)
        with condition:
            lease = self._public_mutation_authorized_locked(path)
            if lease is None:
                return None
            counts = app.public_mutation_inflight  # type: ignore[attr-defined]
            generation = lease[1]
            counts[generation] = counts.get(generation, 0) + 1
            return lease

    @staticmethod
    def _release_public_mutation_lease(lease: tuple[Any, str]) -> None:
        app, generation = lease
        condition = _ensure_public_mutation_lifecycle(app)
        with condition:
            counts = app.public_mutation_inflight  # type: ignore[attr-defined]
            remaining = max(0, counts.get(generation, 0) - 1)
            if remaining:
                counts[generation] = remaining
            else:
                counts.pop(generation, None)
            condition.notify_all()

    def _public_get_route(self) -> bool:
        """Fail closed for unlisted public internal GET targets."""
        try:
            from .retrieval import strict_decode_path
            path = strict_decode_path(urlparse(self.path).path)
        except (ValueError, UnicodeError):
            return False
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        if not path.startswith("/__"):
            return True
        return (
            path in _PUBLIC_GET_INTERNAL_ROUTES
            or any(path.startswith(prefix) for prefix in _PUBLIC_GET_INTERNAL_PREFIXES)
        )

    def _public_tunnel_request_is_read_only(self) -> bool:
        return self._is_public_tunnel_request() and not bool(
            self._public_ingress_is_active()
            and getattr(self.server, "public_ingress_accepting", False)
            and (getattr(self.server, "public_tunnel_edit", False)
                 or getattr(self.server, 'public_tunnel_comment', False))
        )

    @property
    def bundle(self) -> Bundle:
        return self._application_server().bundle

    @property
    def display_name(self) -> str:
        name = getattr(self._application_server(), "name", None)
        if name:
            return name
        cfg = self._config()
        return cfg.get("name") or self.bundle.name

    @property
    def palette(self) -> dict[str, str]:
        owner = self._application_server()
        if not hasattr(owner, "_palette"):
            owner._palette = resolve_palette(self.bundle)
        return owner._palette

    def _config(self) -> dict[str, Any]:
        return getattr(self._application_server(), "config", None) or load_config(self.bundle)

    def _plugin(self):
        return getattr(self._application_server(), "plugin", None) or NoOpPlugin()

    def _state_lock(self):
        """Return the server's reload-state lock, or None if not attached.

        Harness-embedded servers (tests, custom embeddings) may not attach
        ``_state_lock``; in that case we fall back to unlocked access. The
        lock is only required when a watcher thread can mutate the bundle
        concurrently with request handling, which is the ``run_server``
        contract.
        """
        return getattr(self._application_server(), "_state_lock", None)

    def _state_snapshot(self) -> tuple[Bundle, dict[str, str], Any, dict[str, Any]]:
        """Atomically snapshot (bundle, palette, plugin, config) under the lock.

        Used by handlers that read more than one of these attributes, so a
        concurrent watcher reload cannot produce a torn view (P2-46).
        Palette is lazily resolved inside the lock (so the cached palette
        always matches the snapshot's bundle).
        """
        lock = self._state_lock()
        if lock is None:
            bundle = self.bundle
            palette = (
                getattr(self.server, "_palette", None)
                if hasattr(self.server, "_palette")
                else resolve_palette(bundle)
            )
            return bundle, palette, self._plugin(), self._config()
        with lock:
            owner = self._application_server()
            bundle = owner.bundle
            if not hasattr(owner, "_palette"):
                owner._palette = resolve_palette(bundle)
            palette = owner._palette
            plugin = owner.plugin
            config = getattr(owner, "config", None) or load_config(bundle)
            return bundle, palette, plugin, config

    # --- Routing ----------------------------------------------------------
    def send_error(self, code: int, message: str | None = None,
                   explain: str | None = None) -> None:
        """Reserve the proposal prefix even for unknown HTTP methods."""
        if code == 501 and getattr(self, "path", "").startswith("/__proposal/v1/"):
            if self._is_public_tunnel_request():
                return self._send_text(
                    403, "Public ingress cannot access server administration.",
                    content_type="text/plain; charset=utf-8", close=True,
                )
            from .proposal_review.http import dispatch as dispatch_proposal
            if dispatch_proposal(self, getattr(self, "command", "UNKNOWN")):
                return
        super().send_error(code, message, explain)

    def do_GET(self) -> None:  # noqa: N802 (http.server convention)
        try:
            if self._is_public_tunnel_request() and not self._public_get_route():
                return self._send_text(
                    403, "Public ingress cannot access server administration.",
                    content_type="text/plain; charset=utf-8", close=True,
                )
            self._route()
        except BrokenPipeError:
            pass
        except Exception as e:  # pragma: no cover - defensive
            # Log the full exception server-side (operator can debug); return
            # a generic message to the client to avoid information disclosure
            # (filesystem paths, library internals, partial state).
            import traceback
            traceback.print_exc()
            self._send_text(
                500,
                "Internal Server Error\n\nThe server encountered an unexpected "
                "error. Details have been logged to the server's stderr.",
                content_type="text/plain; charset=utf-8",
            )

    def do_HEAD(self) -> None:  # noqa: N802
        """Handle HEAD requests by routing like GET but discarding the body."""
        self._head_only = True
        try:
            if self._is_public_tunnel_request() and not self._public_get_route():
                return self._send_text(
                    403, "Public ingress cannot access server administration.",
                    content_type="text/plain; charset=utf-8", close=True,
                )
            from .proposal_review.http import dispatch as dispatch_proposal
            if dispatch_proposal(self, "HEAD"):
                return
            self.do_GET()
        finally:
            self._head_only = False

    def _proposal_or_unsupported(self, method: str) -> None:
        if self._is_public_tunnel_request():
            return self._send_text(
                403, "Public ingress cannot access this mutation route.",
                content_type="text/plain; charset=utf-8", close=True,
            )
        from .proposal_review.http import dispatch as dispatch_proposal
        if dispatch_proposal(self, method):
            return
        self.send_error(501, f"Unsupported method ({method!r})")

    def do_PUT(self) -> None:  # noqa: N802
        self._proposal_or_unsupported("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._proposal_or_unsupported("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._proposal_or_unsupported("DELETE")

    # --- Live studio: mutating endpoints (§6, §9, §12, §15) -----------------
    def do_POST(self) -> None:  # noqa: N802
        try:
            # Public quick-tunnel visitors are a separate trust boundary from
            # the loopback operator. Deny before any proposal or studio route
            # dispatch so query/path variants and copied local tokens cannot
            # bypass the default read-only policy.
            if self._is_public_tunnel_request() and (
                self._public_tunnel_request_is_read_only()
                or not self._public_mutation_route()
            ):
                return self._send_text(
                    403, "Public ingress cannot access this mutation route.",
                    content_type="text/plain; charset=utf-8", close=True,
                )
            from .proposal_review.http import dispatch as dispatch_proposal
            admission = getattr(self, "_request_admission", None)
            proposal = admission.proposal if admission is not None else None
            if proposal is not None and dispatch_proposal(
                    self, "POST", admission=proposal):
                return
            self._route_post()
        except BrokenPipeError:
            pass
        except Exception as e:  # pragma: no cover - defensive
            import traceback
            traceback.print_exc()
            if urlparse(self.path).path == "/__comment-reanchor":
                return self._send_contract_error(
                    503, "unavailable", "The annotation service is unavailable."
                )
            self._send_text(
                500, "Internal Server Error", content_type="text/plain; charset=utf-8"
            )

    def _route_post(self) -> None:
        from .retrieval import (MAX_REQUEST_TARGET_BYTES, RetrievalContractError,
                                strict_decode_path)
        admission = getattr(self, "_request_admission", None)
        early_path = admission.path if admission is not None else urlparse(self.path).path
        if early_path == "/__comment-reanchor" and admission is None:
            # Route identity and non-disclosing auth precede every request-body
            # or target diagnostic for this endpoint.
              if not self._check_write_auth():
                  return self._send_contract_error(403, "forbidden", "The request is not permitted.", close=True)
              if self._reanchor_rate_unavailable():
                  return self._send_json(429, {"ok": False, "error": {
                      "code": "rate_limited", "message": "Too many requests.", "fields": {}
                  }}, extra_headers={"Retry-After": "60"}, close=True)
              if not getattr(self.server, "studio_edit", True):
                  return self._send_contract_error(403, "read_only", "The studio is read-only.", close=True)
        if len(self.path.encode("utf-8", errors="replace")) > MAX_REQUEST_TARGET_BYTES:
            if early_path == "/__comment-reanchor":
                return self._send_contract_error(400, "invalid_request", "The request target is invalid.", close=True)
            return self._send_contract_error(
                414, "request_target_too_large", "Request target exceeds the size limit",
                close=True,
            )
        if self.headers.get("Transfer-Encoding"):
            if early_path == "/__comment-reanchor":
                return self._send_contract_error(400, "invalid_request", "Transfer-Encoding is not supported.", close=True)
            return self._send_contract_error(
                400, "unsupported_transfer_encoding",
                "Chunked request bodies are not supported", close=True,
            )
        # Cap body size (defence in depth) BEFORE reading, so a huge body is
        # rejected without buffering it. The body is read here (not later) so
        # that EVERY early-return path below has consumed Content-Length bytes
        # — otherwise an unconsumed POST body desyncs the next keep-alive
        # request on the same connection (the leftover bytes get parsed as the
        # next request's method line). This matters concretely for the studio:
        # a 403 on a stale token (e.g. after a server restart) must still drain
        # the body, or every subsequent GET/module fetch on that connection 501s.
        # F4: a malformed (non-integer) Content-Length must NOT raise a 500
        # here. Default to 0 so the body is treated as empty (→ 400/403 below)
        # and the connection stays in sync (nothing to drain).
        raw_length = self.headers.get("Content-Length")
        try:
            length = (admission.content_length if admission is not None
                      else int(raw_length or 0))
            if length < 0:
                raise ValueError
        except (ValueError, TypeError):
            if early_path == "/__comment-reanchor":
                return self._send_contract_error(400, "invalid_request", "Content-Length is invalid.", close=True)
            return self._send_contract_error(
                400, "invalid_content_length", "Content-Length must be a non-negative integer",
                close=True,
            )
        parsed_early = urlparse(self.path)
        reanchor_route = parsed_early.path == "/__comment-reanchor"
        feedback_route: str | None = None
        if admission is not None:
            if admission.path in _FEEDBACK_IMPORT_ROUTES:
                feedback_route = admission.path
        else:
            # Embedders without transport preflight: same pre-body authority.
            try:
                decoded_early = strict_decode_path(parsed_early.path)
            except Exception:
                decoded_early = None
            if decoded_early in _FEEDBACK_IMPORT_ROUTES:
                if self._feedback_import_refused():
                    return None
                feedback_route = decoded_early
        route_cap = (admission.body_cap if admission is not None
                     else FEEDBACK_IMPORT_BODY_CAP if feedback_route is not None
                     else 65_536 if reanchor_route else MAX_EVENT_BODY_BYTES)
        if length > route_cap:
            if feedback_route is not None:
                return self._send_contract_error(
                    413, "note_too_large", "The note exceeds 32 KiB.", close=True)
            # The oversized body can be arbitrarily large — draining it would
            # pin this worker thread, which is worse than a clean reconnect.
            # Instead CLOSE the connection: set close_connection + send
            # ``Connection: close`` so the client reconnects cleanly, and we
            # never parse leftover body bytes as the next request (the
            # keep-alive desync the body-drain invariant above prevents).
            if reanchor_route:
                return self._send_contract_error(413, "invalid_request", "Request body exceeds 65,536 bytes.", close=True)
            return self._send_text(
                413, "Request entity too large.",
                content_type="text/plain; charset=utf-8", close=True,
            )
        raw_body = self._read_request_body(length)
        parsed = urlparse(self.path)
        try:
            path = strict_decode_path(parsed.path)
        except RetrievalContractError as exc:
            return self._send_json(exc.status, exc.as_error())
        if feedback_route is not None:
            # Raw-body dispatch: the note is never fed to the generic parser.
            return self._handle_feedback_import(feedback_route, raw_body)
        # Current spec §14 invisible cross-origin guard: every mutating endpoint requires
        # a valid per-session token AND (when an Origin is present) a host in
        # allowed_hosts. The studio's own JS carries the token automatically
        # (embedded in the page); a foreign page / DNS-rebinding attack lacks
        # it. A non-browser client (curl, okf CLI, tests) with no Origin and a
        # valid token is allowed.
        if admission is None and not self._check_write_auth():
            if path == "/__comment-reanchor":
                return self._send_contract_error(403, "forbidden", "The request is not permitted.")
            return self._send_text(
                403, "Forbidden: invalid or missing cross-origin token.",
                content_type="text/plain; charset=utf-8",
            )
        studio = self.studio
        if admission is None and studio is None and path != "/__comment-reanchor":
            if path == "/__comment-reanchor":
                return self._send_contract_error(503, "unavailable", "The annotation service is unavailable.")
            return self._send_text(
                503, "Studio not enabled on this server.",
                content_type="text/plain; charset=utf-8",
            )
        # --no-edit kiosk (current spec §9/§14): live reads stay on, but no
        # mutating writes. ``/__tunnel`` is exempt: it is server admin (start
        # or stop the share tunnel), not a bundle mutation — sharing a
        # read-only kiosk is precisely the kiosk use case. It still sits
        # behind the token + Origin/Host guard above.
        if (admission is None and not getattr(self.server, "studio_edit", True)
                and path != "/__tunnel"):
            if path == "/__comment-reanchor":
                return self._send_contract_error(403, "read_only", "The studio is read-only.")
            return self._send_text(
                403, "Read-only kiosk: commenting/directing is disabled (--no-edit).",
                content_type="text/plain; charset=utf-8",
            )
        if path == "/__comment-reanchor":
            media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                return self._send_contract_error(
                    400, "invalid_request", "Content-Type must be application/json."
                )
        try:
            def closed_pairs(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate key")
                    result[key] = value
                return result
            data = json.loads(
                raw_body.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
                    object_pairs_hook=(closed_pairs if path in {
                        "/__comment", "/__comment-update", "/__comment-reanchor",
                        "/__presence", "/__tunnel",
                    } else dict),
            ) if raw_body else {}
            if not isinstance(data, dict):
                raise ValueError("object required")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            if path == "/__comment-reanchor":
                return self._send_contract_error(400, "invalid_request", "The JSON request is invalid.")
            if path in {"/__comment", "/__comment-update", "/__presence"}:
                return self._send_json(400, {"ok": False, "error": "invalid_request"})
            return self._send_text(
                400, "Bad request: invalid JSON body.",
                content_type="text/plain; charset=utf-8",
            )

        if path == "/__comment-reanchor" and self.studio is None:
            return self._send_contract_error(503, "unavailable", "The annotation service is unavailable.")

        lease = None
        if self._is_public_tunnel_request():
            lease = self._acquire_public_mutation_lease(path)
            if lease is None:
                return self._send_text(
                    403, "Forbidden: invalid or missing cross-origin token.",
                    content_type="text/plain; charset=utf-8",
                )
        try:
            if path == "/__tunnel":
                return self._handle_tunnel(data)
            if path == "/__comment":
                return self._handle_comment(data)
            if path == "/__comment-update":
                return self._handle_comment_update(data)
            if path == "/__comment-reanchor":
                if not self._consume_reanchor_rate():
                    return self._send_json(429, {"ok": False, "error": {
                        "code": "rate_limited", "message": "Too many requests.",
                        "fields": {},
                    }}, extra_headers={"Retry-After": "60"}, close=True)
                return self._handle_comment_reanchor(data)
            if path == "/__presence":
                return self._handle_presence(data)
            if path == "/__apply":
                return self._handle_apply(data)
            if path == "/__undo":
                return self._handle_undo(data)
            if path == "/__preview":
                return self._handle_preview(data)
            return self._send_text(
                404, "Not found", content_type="text/plain; charset=utf-8"
            )
        finally:
            if lease is not None:
                self._release_public_mutation_lease(lease)

    def _check_write_auth(self) -> bool:
        """Current spec §14 cross-origin guard: valid token + Origin/Host ∈ allowed_hosts.

        Editable local pages (and explicitly opted-in public-edit pages) embed
        the token; the studio's own JS attaches it as ``X-OKF-Token`` on
        writes. Default public-tunnel pages never receive it and have an
        independent request-host denial before dispatch. ``--no-edit``
        disables the studio entirely (no mutating endpoints). When ``studio``
        is not attached (plain server), all writes are refused here.

        P1-8 (SEC-004): the spec names BOTH Origin and Host. We check
        Origin when present, AND fall back to / additionally verify Host
        against ``allowed_hosts`` so a DNS-rebinding attack that strips
        Origin is still contained. ``allowed_hosts`` defaults to loopback
        (P1-16) and is never empty.
        """
        if self._is_public_tunnel_request():
            app = getattr(self.server, "app_server", None)
            if app is None:
                return False
            condition = _ensure_public_mutation_lifecycle(app)
            with condition:
                try:
                    path = urlparse(self.path).path
                except ValueError:
                    return False
                return self._public_mutation_authorized_locked(path) is not None

        token = self.csrf_token
        if token is None:
            return False
        sent = self.headers.get("X-OKF-Token", "")
        if not secrets.compare_digest(sent, token):
            return False
        # P1-16: never fail open on an unset allowed_hosts. Default to
        # loopback if the server somehow shipped without it set.
        allowed = getattr(self.server, "allowed_hosts", None)
        if not allowed:
            allowed = ("127.0.0.1", "localhost")
        origin = self.headers.get("Origin")
        if origin:
            # Origin is like "http://127.0.0.1:8787"; compare the host part.
            try:
                host = urlparse(origin).hostname or ""
            except ValueError:
                return False
            if host not in allowed:
                return False
        # P1-8: also check Host (defense against DNS-rebinding that strips
        # Origin). Host is like "127.0.0.1:8787"; compare the host part.
        raw_host = self.headers.get("Host", "").strip()
        if not raw_host:
            return False
        try:
            host_only = urlparse("//" + raw_host).hostname or ""
        except ValueError:
            return False
        if not host_only or host_only not in allowed:
            return False
        return True

    def _reanchor_rate_unavailable(self) -> bool:
        """Observe whether re-anchor capacity is currently exhausted."""
        owner, lock = self._reanchor_rate_authority()
        now = time.monotonic()
        with lock:
            attempts = getattr(owner, "_reanchor_rate_attempts", [])
            attempts = [value for value in attempts if now - value < 60.0]
            return len(attempts) >= 120

    def _consume_reanchor_rate(self) -> bool:
        """Atomically consume one completed re-anchor admission."""
        owner, lock = self._reanchor_rate_authority()
        now = time.monotonic()
        with lock:
            attempts = getattr(owner, "_reanchor_rate_attempts", [])
            attempts = [value for value in attempts if now - value < 60.0]
            if len(attempts) >= 120:
                owner._reanchor_rate_attempts = attempts
                return False
            attempts.append(now)
            owner._reanchor_rate_attempts = attempts
            return True

    def _reanchor_rate_authority(self) -> tuple[Any, threading.Lock]:
        """Return the primary session server and its authoritative quota lock."""
        owner = self._application_server()
        lock = getattr(owner, "_reanchor_rate_lock", None)
        if lock is None:
            # Embedders may construct the stdlib server directly. Publish both
            # authority fields exactly once without ever assigning them on a
            # delegated public-ingress object.
            with self._reanchor_rate_init_lock:
                lock = getattr(owner, "_reanchor_rate_lock", None)
                if lock is None:
                    lock = threading.Lock()
                    owner._reanchor_rate_lock = lock
                    if not hasattr(owner, "_reanchor_rate_attempts"):
                        owner._reanchor_rate_attempts = []
        return owner, lock

    # --- Portable feedback import (X2): local operator only ------------------
    def _feedback_import_is_local(self) -> bool:
        """Loopback socket ends on the private listener with no exposure.

        A quick-tunnel client connects from loopback, so a loopback peer alone
        is not proof of a local operator: any attached tunnel (process, URL or
        public ingress listener) or a non-loopback bind refuses the import.
        Mirrors ``proposal_review/http.py`` mutation locality.
        """
        from .proposal_review.activation import is_loopback
        if self._is_public_tunnel_request():
            return False
        try:
            local = self.connection.getsockname()[0]
            remote = self.connection.getpeername()[0]
        except (AttributeError, OSError, TypeError, IndexError):
            return False
        app = self._application_server()
        exposed = bool(
            getattr(app, "tunnel_url", None)
            or getattr(app, "tunnel_proc", None) is not None
            or getattr(app, "public_ingress_server", None) is not None
            or getattr(app, "proposal_public_exposure", False)
        )
        return is_loopback(local) and is_loopback(remote) and not exposed

    def _feedback_import_refused(self) -> bool:
        """Send the body-independent refusal for an import route, if any.

        Order: public ingress -> token + Origin/Host -> Studio attached ->
        ``--no-edit`` -> local-only. Every refusal closes the connection
        because it may be sent before the request body was read.
        """
        if self._is_public_tunnel_request():
            self._send_text(403, "Public ingress cannot access this mutation route.",
                            content_type="text/plain; charset=utf-8", close=True)
            return True
        if not self._check_write_auth():
            self._send_contract_error(403, "forbidden", "The request is not permitted.", close=True)
            return True
        if self.studio is None:
            self._send_contract_error(503, "unavailable", "Studio is not enabled on this server.", close=True)
            return True
        if not getattr(self.server, "studio_edit", True):
            self._send_contract_error(403, "read_only", "The studio is read-only.", close=True)
            return True
        if not self._feedback_import_is_local():
            self._send_contract_error(
                403, "local_only",
                "Feedback import is available only on the local Studio, not through a tunnel.",
                close=True)
            return True
        return False

    def _handle_feedback_import(self, path: str, raw_body: bytes) -> None:
        """Preview or confirm one portable note (raw body, strict parser)."""
        from .portable_feedback import (PortableNoteError, fingerprint_request,
                                        import_idempotency_key, parse_note)
        # Re-check at dispatch: tunnel/no-edit state can change while the
        # body is being received after preflight admission.
        if self._feedback_import_refused():
            return
        media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            return self._send_contract_error(
                415, "unsupported_media_type", "Content-Type must be application/json.")
        try:
            note = parse_note(raw_body)
        except PortableNoteError as exc:
            return self._send_contract_error(
                400, "invalid_note", "The feedback note is invalid.", {"note": exc.reason})
        key = import_idempotency_key(note)
        request = fingerprint_request(note)
        if path == _FEEDBACK_IMPORT_PREVIEW:
            return self._feedback_import_preview(note, key, request)
        return self._feedback_import_confirm(note, key, request)

    def _feedback_import_availability(self) -> str:
        """Presentation-only import availability for the local operator page.

        Follows the POST routes' body-independent refusal order after their
        token check; this GET checks no token or Origin/Host. It never grants
        anything: preview and confirm re-check every condition.
        """
        if self.studio is None:
            return "unavailable"
        if not getattr(self.server, "studio_edit", True):
            return "read_only"
        if not self._feedback_import_is_local():
            return "local_only"
        return "available"

    def _handle_feedback_import_page(self) -> None:
        """``GET /__feedback-import``: the local operator import page.

        Public ingress is already refused by the ``do_GET`` allowlist; the
        explicit check keeps this handler closed if that list ever grows.
        The page names no document, so Studio page feedback stays unmounted.
        """
        if self._is_public_tunnel_request():
            return self._send_text(
                403, "Public ingress cannot access server administration.",
                content_type="text/plain; charset=utf-8", close=True,
            )
        self._page_document = None
        from .portable_feedback import MAX_NOTE_BYTES, PREVIEW_TOKEN_TTL_SECONDS
        self._send_text(200, render_feedback_import_page(
            name=self.display_name, config=self._config(),
            availability=self._feedback_import_availability(),
            max_note_bytes=MAX_NOTE_BYTES,
            preview_ttl_seconds=PREVIEW_TOKEN_TTL_SECONDS,
        ))

    def _feedback_import_target_sha256(self, document: str) -> str:
        """sha256 of the exact on-disk bytes (stable no-follow admission)."""
        from .canonical_source import read_canonical_document_bytes
        return hashlib.sha256(read_canonical_document_bytes(
            self.bundle, document, max_bytes=MAX_RAW_RESPONSE_BYTES)).hexdigest()

    def _feedback_import_preview(self, note: Any, key: str, request: dict[str, Any]) -> None:
        """Side-effect-free classification; issues a preview token only when ready."""
        from .documents import document_concept
        from .portable_feedback import sign_preview_token
        base = {"ok": True, "note": note.echo(), "document": note.document,
                "concept": document_concept(note.document)}
        try:
            state, existing = self.studio.inspect_comment_create_key(
                request=request, idempotency_key=key)
        except CommentMutationError:
            return self._send_contract_error(503, "unavailable", "The comment service is unavailable.")
        if state == "finalized":
            return self._send_json(200, {**base, "status": "already_imported", "comment": existing})
        if state == "conflict":
            return self._send_json(200, {**base, "status": "conflict"})
        # Advisory only (never authority): rows outlive WAL retention.
        try:
            rows = self.studio.list_comments()
        except (OSError, ValueError, CommentMutationError):
            return self._send_contract_error(503, "unavailable", "The comment service is unavailable.")
        for row in rows:
            marker = row.get("portable_import")
            if isinstance(marker, dict) and marker.get("note_id") == note.note_id:
                if marker.get("note_sha256") == note.note_sha256:
                    return self._send_json(200, {**base, "status": "already_imported", "comment": row})
                return self._send_json(200, {**base, "status": "conflict"})
        if state == "retired":
            return self._send_json(200, {**base, "status": "conflict"})
        try:
            current = self._feedback_import_target_sha256(note.document)
        except FileNotFoundError:
            return self._send_json(200, {**base, "status": "target_missing", "source_match": False})
        except (OSError, ValueError):
            return self._send_contract_error(503, "unavailable", "The document is unavailable.")
        if not hmac.compare_digest(current, note.source_sha256):
            return self._send_json(200, {**base, "status": "revision_mismatch", "source_match": False})
        token, exp = sign_preview_token(
            _feedback_preview_key(self._application_server()),
            csrf_token=self.csrf_token or "", note=note)
        return self._send_json(200, {
            **base, "status": "ready", "source_match": True, "preview_token": token,
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp)),
        })

    def _feedback_import_confirm(self, note: Any, key: str, request: dict[str, Any]) -> None:
        from .canonical_source import read_canonical_document_bytes
        from .documents import document_concept
        from .portable_feedback import build_marker, verify_preview_token
        studio = self.studio
        # Replay precedes the preview token and every target lookup, so a
        # lost response replays after a restart or after target deletion.
        try:
            replay = studio.replay_finalized_comment_create(
                request=request, idempotency_key=key, reconcile_prepared=True)
        except CommentMutationError as exc:
            if exc.code == "idempotency_conflict":
                return self._send_contract_error(
                    409, "idempotency_conflict",
                    "A different note with this id was already imported or has expired from the duplicate check.")
            return self._send_contract_error(503, "unavailable", "The comment service is unavailable.")
        if replay is not None:
            return self._send_json(200, {"ok": True, "comment": replay, "replayed": True})
        if not verify_preview_token(
                _feedback_preview_key(self._application_server()),
                self.headers.get("X-OKF-Preview-Token", ""),
                csrf_token=self.csrf_token or "", note=note):
            return self._send_contract_error(
                409, "stale_preview", "The page or session changed; preview again.")
        document = note.document
        try:
            with path_lock(self.bundle.root / document):
                try:
                    current = hashlib.sha256(read_canonical_document_bytes(
                        self.bundle, document, max_bytes=MAX_RAW_RESPONSE_BYTES)).hexdigest()
                except FileNotFoundError:
                    return self._send_contract_error(
                        404, "target_missing", "The page no longer exists.")
                if not hmac.compare_digest(current, note.source_sha256):
                    return self._send_contract_error(
                        409, "revision_mismatch",
                        "The page has changed since this note was written.")
                directive, replayed = studio.post_comment_idempotent(
                    request=request, idempotency_key=key, document=document,
                    concept=document_concept(document), body=note.body,
                    anchor={"kind": "page"}, actor="user", detail={},
                    parent_id=None, portable_import=build_marker(note),
                )
        except CommentMutationError as exc:
            if exc.code == "idempotency_conflict":
                return self._send_contract_error(
                    409, "idempotency_conflict",
                    "A different note with this id was already imported or has expired from the duplicate check.")
            return self._send_contract_error(503, "unavailable", "The comment service is unavailable.")
        except (OSError, ValueError):
            return self._send_contract_error(503, "unavailable", "The comment service is unavailable.")
        if replayed:
            return self._send_json(200, {"ok": True, "comment": directive, "replayed": True})
        return self._send_json(201, {"ok": True, "comment": directive, "replayed": False})

    def _handle_comment(self, data: dict[str, Any]) -> None:
        from .documents import document_path, document_concept, comment_document
        allowed = {"concept", "document", "body", "anchor", "detail", "actor", "parent_id"}
        concept = data.get("concept")
        document = data.get("document")
        if 'document' in data:
            try:
                document = document_path(document)
                if "concept" in data:
                    raise ValueError('two targets')
                concept = document_concept(document)
            except (ValueError, TypeError):
                return self._send_json(400, {"ok": False, "error": "invalid_request"})
        body = data.get("body")
        detail = data.get("detail", {})
        actor = data.get("actor", "user")
        parent_id = data.get("parent_id")
        if (not set(data).issubset(allowed) or not isinstance(concept, str)
                or (not concept and document is None) or concept != concept.strip() or len(concept) > 1024
                  or not isinstance(body, str) or not body.strip() or len(body) > 16_384
                  or not isinstance(detail, dict) or actor != "user"
                or (parent_id is not None and (not isinstance(parent_id, str)
                    or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", parent_id)))):
            return self._send_json(400, {"ok": False, "error": "invalid_request"})
        idem_key = self.headers.get("Idempotency-Key")
        from .portable_feedback import IMPORT_KEY_PREFIX
        if idem_key and idem_key.startswith(IMPORT_KEY_PREFIX):
            # The ``pfn.`` namespace belongs to the local portable-feedback
            # import route. Refusing it here stops key squatting that would
            # otherwise block a later import with an idempotency conflict.
            return self._send_json(400, {"ok": False, "error": "reserved_idempotency_key"})
        # Lock order for a new create is canonical concept writer lock ->
        # create-idempotency key -> comment id(s) -> directives transaction.
        # save_concept takes only the first lock.  Finalized replay is reconciled
        # before any current concept/parent lookup, so a lost-response retry does
        # not acquire the concept lock or depend on mutable canonical bytes.
        if idem_key:
            try:
                replay = self.studio.replay_finalized_comment_create(
                    request=data, idempotency_key=idem_key)
            except CommentMutationError as exc:
                if exc.code in {"invalid_request", "idempotency_conflict"}:
                    return self._send_json(400 if exc.code == "invalid_request" else 409,
                                           {"ok": False, "error": exc.code})
                return self._send_json(503, {"ok": False, "error": "unavailable"})
            if replay is not None:
                return self._send_json(201, {"ok": True, "comment": replay})
        if "anchor" not in data:
            anchor = {}
        elif data.get("anchor") == {"kind": "concept"}:
            anchor = {"kind": "concept"}
        elif document is not None and data.get("anchor") == {"kind": "page"}:
            anchor = {"kind": "page"}
        elif isinstance(data.get("anchor"), dict):
            anchor = data["anchor"]
            try:
                parse_v2_anchor(anchor, public=True)
            except AnchorError as exc:
                return self._send_json(400, {"ok": False, "error": "invalid anchor", "reason": exc.reason})
        else:
            return self._send_json(400, {"ok": False, "error": "invalid anchor", "reason": "malformed_anchor"})
        effective_concept = concept
        if document is not None and not concept and anchor not in ({}, {"kind": "page"}):
            return self._send_json(400, {"ok": False, "error": "invalid anchor", "reason": "source_mapping_failed"})
        if parent_id is not None:
            if not isinstance(parent_id, str):
                return self._send_json(400, {"ok": False, "error": "invalid_request"})
            parent = self.studio.get_comment(parent_id)
            if parent is None or not isinstance(parent.get("concept"), str):
                return self._send_json(400, {"ok": False, "error": "invalid_request"})
            effective_concept = parent["concept"]
            if concept != effective_concept or (document is not None and comment_document(parent) != document):
                return self._send_json(400, {"ok": False, "error": "invalid_request"})
        try:
            target_document = document or '/'.join(concept_id_from_str(effective_concept)) + '.md'
            if not document_concept(target_document) and effective_concept:
                return self._send_json(400, {"ok": False, "error": "invalid_request"})
            canonical_path = self.bundle.root / target_document
        except ConceptIdError:
            return self._send_json(400, {"ok": False, "error": "invalid anchor", "reason": "source_mapping_failed"})
        try:
            with path_lock(canonical_path):
                try:
                    source = _canonical_document_source(self.bundle, target_document)
                except FileNotFoundError:
                    if (tuple(effective_concept.split('/')) in self.bundle.concepts
                            or Path(target_document) in self.bundle.indexes
                            or Path(target_document) in self.bundle.logs):
                        raise
                    return self._send_json(404, {"ok": False, "error": "target_not_found"})
                if anchor not in ({}, {"kind": "concept"}, {"kind": "page"}):
                    position = anchor["position"]
                    if capture_anchor(source, position["start"], position["end"]) != anchor:
                        raise AnchorError("source_mapping_failed")
                if idem_key:
                    directive, _replayed = self.studio.post_comment_idempotent(
                        request=data, idempotency_key=idem_key,
                        concept=effective_concept, body=body, anchor=anchor, actor=actor,
                        detail=detail, parent_id=parent_id,
                        document=document,
                    )
                else:
                    directive = self.studio.post_comment(
                        concept=effective_concept, body=body, anchor=anchor, actor=actor,
                        detail=detail, parent_id=parent_id,
                        document=document,
                    )
        except AnchorError as exc:
            return self._send_json(400, {"ok": False, "error": "invalid anchor", "reason": exc.reason})
        except CommentMutationError as exc:
            if exc.code in {"invalid_request", "idempotency_conflict"}:
                status = 400 if exc.code == "invalid_request" else 409
                return self._send_json(status, {"ok": False, "error": exc.code})
            return self._send_json(503, {"ok": False, "error": "unavailable"})
        except (OSError, ValueError):
            return self._send_json(503, {"ok": False, "error": "unavailable"})
        response_body = {"ok": True, "comment": directive}
        self._send_json(201, response_body)

    def _handle_comment_reanchor(self, data: dict[str, Any]) -> None:
        keys = {"comment_id", "expected_row_token", "expected_concept_content_rev", "anchor"}
        key = self.headers.get("Idempotency-Key", "")
        cid = data.get("comment_id")
        token = data.get("expected_row_token")
        rev = data.get("expected_concept_content_rev")
        if (set(data) != keys or not isinstance(cid, str)
                or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", cid)
                or not isinstance(token, str) or not token or len(token.encode("ascii", "ignore")) != len(token)
                or len(token) > 128 or any(ord(c) < 33 or ord(c) > 126 for c in token)
                or not isinstance(rev, str) or not re.fullmatch(r"[0-9a-f]{64}", rev)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", key)):
            return self._send_contract_error(400, "invalid_request", "The request fields are invalid.")
        try:
            parse_v2_anchor(data.get("anchor"), public=True)
        except AnchorError as exc:
            return self._send_contract_error(400, "invalid_anchor", "The anchor is invalid.", {"anchor": exc.reason})
        try:
            finalized = self.studio.replay_finalized_reanchor(
                comment_id=cid, expected_row_token=token,
                expected_concept_content_rev=rev, anchor=data["anchor"],
                idempotency_key=key)
        except CommentMutationError as exc:
            status = 409 if exc.code == "idempotency_conflict" else 503
            message = ("The idempotency key conflicts with another request."
                       if status == 409 else "The annotation service is unavailable.")
            return self._send_contract_error(status, exc.code, message)
        if finalized is not None:
            response, response_status = finalized
            response["replayed"] = True
            return self._send_json(response_status, response)
        try:
            # Snapshot ownership without nesting locks, then establish the one
            # global order: concept -> endpoint key -> comment -> directives.
            # reanchor_comment's row-token CAS rejects a changed snapshot.
            current = self.studio.get_comment(cid)
            if current is None:
                return self._send_contract_error(404, "comment_not_found", "The comment was not found.")
            if not current.get('concept') or current.get('anchor') == {'kind': 'page'}:
                return self._send_contract_error(400, 'invalid_anchor', 'Page feedback cannot be re-anchored.')
            concept_cid = concept_id_from_str(current.get("concept"))
            canonical_path = concept_id_to_path(self.bundle.root, concept_cid)
            with path_lock(canonical_path):
                source = _canonical_annotation_source(self.bundle, concept_cid)
                response, replayed, response_status = self.studio.reanchor_comment(
                    comment_id=cid, expected_row_token=token,
                    expected_concept_content_rev=rev, anchor=data["anchor"],
                      idempotency_key=key, canonical_source=source,
                      expected_concept=current.get("concept"),
                  )
        except AnchorError as exc:
            return self._send_contract_error(400, "invalid_anchor", "The anchor is invalid.", {"anchor": exc.reason})
        except CommentMutationError as exc:
            mapping = {
                "comment_not_found": (404, "The comment was not found."),
                "comment_conflict": (409, "The comment changed; refetch it and select again."),
                "concept_conflict": (409, "The concept changed; refetch it and select again."),
                "idempotency_conflict": (409, "The idempotency key conflicts with another request."),
                "invalid_anchor": (400, "The anchor is invalid."),
                "unavailable": (503, "The annotation service is unavailable."),
            }
            status, message = mapping.get(exc.code, (503, "The annotation service is unavailable."))
            return self._send_contract_error(status, exc.code, message)
        except OSError:
            return self._send_contract_error(503, "unavailable", "The annotation service is unavailable.")
        response["replayed"] = replayed
        self._send_json(response_status, response)

    def _handle_comment_update(self, data: dict[str, Any]) -> None:
        """Update a comment's state or archive flag — user-facing (§9.1).

        Archive is a SEPARATE track from lifecycle state.

        - ``state`` transitions (open / dismissed, plus compatibility claimed /
          resolved round-trips) do NOT touch the archive flag.
        - ``archived: true/false`` flips only the archive flag; the comment
          keeps its current state (open / resolved / dismissed).
        - Archive is only allowed on ROOT comments, and only when EVERY
          comment in the thread (root + all replies) is ``resolved``.
          Replies to an archived thread auto-unarchive the parent
          (enforced in ``Studio.post_comment``).
        - Dismiss is still rejected for claimed comments (agent is
          mid-flight); archive follows the resolved-thread rule instead.
        """
        allowed = {"id", "expected_row_token", "state", "archived", "body"}
        comment_id = data.get("id")
        expected_row_token = data.get("expected_row_token")
        operations = [name for name in ("state", "archived", "body") if name in data]
        if (not set(data).issubset(allowed) or not isinstance(comment_id, str)
                or not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{10,26}", comment_id)
                or not isinstance(expected_row_token, str)
                or not re.fullmatch(r"[!-~]{1,128}", expected_row_token)
                or len(operations) != 1):
            return self._send_json(400, {"ok": False, "error": "invalid_request"})
        new_state = data.get("state")
        if ("state" in data and (not isinstance(new_state, str)
                                 or new_state not in {"open", "dismissed"})):
            return self._send_json(400, {"ok": False, "error": "invalid_request"})
        has_archived = "archived" in data
        archived_flag = None
        if has_archived:
            if type(data.get("archived")) is not bool:
                return self._send_json(400, {"ok": False, "error": "archived must be boolean"})
            archived_flag = data["archived"]
        # Body edit (the enter-too-soon fix): the comment author can amend
        # the text of a not-yet-resolved comment in place. The edit bumps
        # updated_at, so an agent blocked in ``wait`` re-receives the
        # corrected ask.
        new_body = None
        if "body" in data:
            raw_body = data.get("body")
            if (not isinstance(raw_body, str) or not raw_body.strip()
                    or len(raw_body) > 16_384):
                return self._send_json(400, {"ok": False, "error": "body must be non-empty"})
            new_body = raw_body
        if not new_state and not has_archived and new_body is None:
            return self._send_json(400, {"ok": False, "error": "state, archived, or body required"})

        try:
            updated, error, unresolved = self.studio.update_comment_external(
                comment_id, expected_row_token=expected_row_token,
                state=new_state,
                archived=archived_flag if has_archived else None,
                body=new_body,
            )
        except (OSError, ValueError, CommentMutationError):
            return self._send_json(503, {"ok": False, "error": "unavailable"})
        if error == "not_found":
            return self._send_json(404, {"ok": False, "error": "comment not found"})
        if error in {"invalid_id", "archive_reply"}:
            return self._send_json(400, {"ok": False, "error": error})
        if error:
            public_error = {
                "body_state": "cannot edit a resolved/dismissed comment (reopen it first)",
                  "claimed": "cannot dismiss a claimed comment",
                  "invalid_transition": "invalid lifecycle transition",
                "archive_unresolved": "archive requires every comment in the thread to be resolved",
            }.get(error, error)
            payload = {"ok": False, "error": public_error}
            if unresolved:
                payload["unresolved_ids"] = unresolved
            return self._send_json(409, payload)
        self._send_json(200, {"ok": True, "comment": updated})

    def _handle_presence(self, data: dict[str, Any]) -> None:
        if "action" in data:
            return self._handle_browser_presence(data)
        allowed = {"actor", "state", "focus", "message"}
        actor = data.get("actor", "agent")
        state = data.get("state", "idle")
        focus = data.get("focus")
        message = data.get("message")
        if (not set(data).issubset(allowed)
                or not isinstance(actor, str) or actor not in {"agent", "user"}
                or not isinstance(state, str) or state not in {"idle", "watching", "editing", "thinking"}
                or (focus is not None and not isinstance(focus, str))
                or (message is not None and not isinstance(message, str))):
            return self._send_json(400, {"ok": False, "error": "invalid_request"})
        if focus is not None:
            if not valid_focus(focus) or concept_id_from_str(focus) not in self.bundle.concepts:
                return self._send_json(400, {"ok": False, "error": "invalid_request"})
        # INTENT2-002 fix: don't let a browser boot POST clobber the agent's
        # presence. The studio client used to POST
        # ``{actor:"user", state:"idle"}`` on every boot, silently overwriting
        # "Agent: editing tables/orders". The fix: a ``user`` actor's write
        # only takes effect when the current agent presence is already idle
        # (i.e. there's no active agent state to preserve). The agent's own
        # writes (and explicit ``okf presence`` CLI calls) always go through.
        if actor == "user":
            current = self.studio.get_presence()
            current_state = (current.get("state") or "idle") if current.get("actor") == "agent" else "idle"
            if current_state not in ("idle", "watching"):
                # The agent is actively doing something — keep its presence.
                self._send_json(200, {"ok": True, "presence": current, "preserved": True})
                return
        try:
            presence = self.studio.set_presence(actor=actor, state=state, focus=focus,
                                                message=message)
        except (PresenceUnavailable, PresenceCapacity):
            return self._send_json(503, {"ok": False, "error": "unavailable"})
        self._send_json(200, {"ok": True, "presence": presence})

    def _presence_capability(self, session_id: str) -> str:
        now = time.time(); generation = self._presence_generation()
        payload = {"session_id": session_id, "issued_at": now,
                   "expires_at": now + 60.0, "generation": generation,
                   "nonce": secrets.token_urlsafe(12)}
        encoded = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True,
            separators=(",", ":")).encode()).decode().rstrip("=")
        signature = hmac.new((self.csrf_token or "").encode(), encoded.encode(), hashlib.sha256).hexdigest()
        return encoded + "." + signature

    def _presence_generation(self) -> str:
        owner = self._application_server()
        with self._presence_rate_lock:
            generation = getattr(owner, "_presence_generation", None)
            if generation is None:
                generation = secrets.token_urlsafe(18)
                owner._presence_generation = generation
            return generation

    def _decode_presence_capability(self, capability: str) -> dict[str, Any] | None:
        try:
            encoded, signature = capability.split(".", 1)
            expected = hmac.new((self.csrf_token or "").encode(), encoded.encode(), hashlib.sha256).hexdigest()
            if not secrets.compare_digest(signature, expected): return None
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = json.loads(raw)
            if (not isinstance(payload,dict) or set(payload)!={"session_id","issued_at","expires_at","generation","nonce"}
                    or not isinstance(payload["session_id"],str) or not isinstance(payload["generation"],str)
                    or not isinstance(payload["nonce"],str) or not all(isinstance(payload[k],(int,float)) and not isinstance(payload[k],bool) and math.isfinite(payload[k]) for k in ("issued_at","expires_at"))): return None
            return payload
        except (ValueError, UnicodeError, json.JSONDecodeError): return None

    def _handle_presence_get(self) -> None:
        if self.studio is None:
            return self._send_json(503, {"ok": False, "error": "unavailable"}, extra_headers={"Cache-Control":"no-store"})
        try: snapshot = self.studio.get_presence_snapshot()
        except PresenceUnavailable:
            return self._send_json(503, {"ok": False, "error": "unavailable"}, extra_headers={"Cache-Control":"no-store"})
        # A live page may reconcile frequently (gap/reconnect/visibility/poll
        # takeover). Reuse a still-valid capability rather than consuming an
        # issuance slot and creating an unreachable predecessor session.
        reuse_sid = self.headers.get("X-OKF-Presence-Session")
        reuse_cap = self.headers.get("X-OKF-Presence-Capability")
        if reuse_sid is not None or reuse_cap is not None:
            payload = self._decode_presence_capability(reuse_cap or "")
            now_wall = time.time()
            if (not isinstance(reuse_sid, str) or not 12 <= len(reuse_sid) <= 64
                    or payload is None or payload["session_id"] != reuse_sid
                    or payload["generation"] != self._presence_generation()
                    or payload["expires_at"] <= now_wall):
                return self._send_json(403, {"ok":False,"error":"forbidden"}, extra_headers={"Cache-Control":"no-store"})
            snapshot["browser_session"] = {"session_id": reuse_sid, "capability": reuse_cap}
            return self._send_json(200, snapshot, extra_headers={"Cache-Control":"no-store"})
        source = self.client_address[0] if self.client_address else "unknown"
        now = time.monotonic()
        owner = self._application_server()
        with self._presence_rate_lock:
            issuance = getattr(owner,"_presence_issuance",{})
            recent=[stamp for stamp in issuance.get(source,[]) if now-stamp<60]
            if len(recent)>=60:
                return self._send_json(429,{"ok":False,"error":"rate_limited"},extra_headers={"Cache-Control":"no-store"})
            recent.append(now); issuance[source]=recent
            if len(issuance)>256:
                issuance={k:v for k,v in issuance.items() if v and now-v[-1]<60}
            owner._presence_issuance=issuance
        sid = secrets.token_urlsafe(18); capability=self._presence_capability(sid)
        payload=self._decode_presence_capability(capability)
        try:
            self.studio.presence_store.issue(sid,payload["generation"],payload["issued_at"],payload["expires_at"])
        except PresenceCapacity:
            return self._send_json(429,{"ok":False,"error":"capacity"},extra_headers={"Cache-Control":"no-store"})
        except PresenceUnavailable:
            return self._send_json(503,{"ok":False,"error":"unavailable"},extra_headers={"Cache-Control":"no-store"})
        snapshot["browser_session"] = {"session_id": sid, "capability": capability}
        self._send_json(200, snapshot, extra_headers={"Cache-Control":"no-store"})

    def _handle_browser_presence(self, data: dict[str, Any]) -> None:
        allowed={"action","session_id","capability","client_seq","actor","state","focus","label","viewport"}
        action,sid,cap,seq=data.get("action"),data.get("session_id"),data.get("capability"),data.get("client_seq")
        if (not set(data).issubset(allowed) or action not in {"upsert","leave"}
                or not isinstance(sid,str) or not 12<=len(sid)<=64 or not isinstance(cap,str)
                or not isinstance(seq,int) or isinstance(seq,bool) or not 0<=seq<=2**53-1):
            return self._send_json(400,{"ok":False,"error":"invalid_request"})
        payload=self._decode_presence_capability(cap)
        if (payload is None or payload["session_id"]!=sid or payload["generation"]!=self._presence_generation()
                ):
            return self._send_json(403,{"ok":False,"error":"forbidden"})
        now=time.monotonic()
        owner = self._application_server()
        with self._presence_rate_lock:
            rates=getattr(owner,"_presence_rates",{})
            aggregate=[stamp for stamp in getattr(owner,"_presence_aggregate_rate",[]) if now-stamp<1.0]
            if len(aggregate)>=500:
                return self._send_json(429,{"ok":False,"error":"rate_limited"})
            recent=[stamp for stamp in rates.get(sid,[]) if now-stamp<1.0]
            if len(recent)>=20:
                return self._send_json(429,{"ok":False,"error":"rate_limited"})
            recent.append(now); rates[sid]=recent; aggregate.append(now)
            rates={key:value for key,value in rates.items() if value and now-value[-1]<60}
            owner._presence_rates=rates; owner._presence_aggregate_rate=aggregate
        if action=="leave" and set(data)!={"action","session_id","capability","client_seq"}:
            return self._send_json(400,{"ok":False,"error":"invalid_request"})
        record=None
        if action=="upsert":
            actor,state,focus,label,viewport=data.get("actor"),data.get("state"),data.get("focus"),data.get("label"),data.get("viewport")
            if (actor!="browser" or state not in {"watching","editing","thinking"}
                    or not valid_label(label)
                    or (focus is not None and not isinstance(focus,str))):
                return self._send_json(400,{"ok":False,"error":"invalid_request"})
            if focus is not None:
                if not valid_focus(focus): return self._send_json(400,{"ok":False,"error":"invalid_request"})
                cid=concept_id_from_str(focus)
                if cid not in self.bundle.concepts:
                    return self._send_json(400,{"ok":False,"error":"invalid_request"})
            if not isinstance(viewport,dict) or set(viewport)!={"schema","x","y"} or viewport.get("schema")!="graph-viewport-v1":
                return self._send_json(400,{"ok":False,"error":"invalid_request"})
            coords=(viewport.get("x"),viewport.get("y"))
            if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=1 for v in coords):
                return self._send_json(400,{"ok":False,"error":"invalid_request"})
            record={"kind":"browser","actor":"browser","state":state,"label":label.strip(),"viewport":{"schema":"graph-viewport-v1","x":float(coords[0]),"y":float(coords[1])}}
            if focus is not None: record["focus"]=focus
        try:
            result=self.studio.presence_store.mutate_authorized(
                sid,payload["generation"],payload["issued_at"],payload["expires_at"],seq,record)
            if result is None: return self._send_json(403,{"ok":False,"error":"forbidden"})
            snapshot,applied,delta=result
        except PresenceCapacity: return self._send_json(429,{"ok":False,"error":"capacity"})
        except PresenceUnavailable: return self._send_json(503,{"ok":False,"error":"unavailable"})
        if delta:self.studio.bus.publish(delta)
        self._send_json(200,{"ok":True,"applied":applied,"presence":snapshot})

    def _handle_tunnel(self, data: dict[str, Any]) -> None:
        """Attach/detach a cloudflared quick tunnel to the RUNNING server.

        ``okf tunnel <bundle>`` drives this so going public no longer means
        killing and restarting ``serve`` (losing the session token, open
        SSE clients, and undo history). Actions:

        * ``start`` — create a fresh dedicated public-ingress listener, spawn
          cloudflared against that listener, record it in ``server.json``, and
          return the public URL. Idempotent: a second start returns the existing
          URL with ``already_running: true``.
        * ``stop`` — revoke the public generation/token, terminate the tunnel
          process and listener, and clear ``server.json``'s tunnel_url.
          Idempotent.
        * ``status`` — report the current URL (or null).

        The tunnel process/listener belong to THIS server process (they die
        with it — same lifecycle as ``serve --tunnel``). This route is local
        server administration: exempt from --no-edit, but never from the local
        admin token + Origin/Host guard and never reachable through public
        ingress.
        """
        allowed_keys = {"action", "allow_public_edit", "allow_public_comment"}
        if not set(data).issubset(allowed_keys):
            return self._send_json(
                400, {"ok": False, "error": "unsupported tunnel request field"})
        action = data.get("action", "start")
        if not isinstance(action, str) or action not in ("start", "stop", "status"):
            return self._send_json(
                400, {"ok": False, "error": "action must be start|stop|status"})
        if "allow_public_edit" in data and action != "start":
            return self._send_json(
                400, {"ok": False, "error": "allow_public_edit is valid only for start"})
        allow_public_edit = data.get("allow_public_edit", False)
        allow_public_comment = data.get('allow_public_comment', False)
        if (type(allow_public_comment) is not bool
                or ('allow_public_comment' in data and action != 'start')
                or (allow_public_comment and allow_public_edit)):
            return self._send_json(400, {"ok": False, "error": "invalid public capability"})
        if not isinstance(allow_public_edit, bool):
            return self._send_json(
                400, {"ok": False, "error": "allow_public_edit must be a boolean"})
        server = self.server
        lock = getattr(server, "_tunnel_lock", None)
        if lock is None:
            return self._send_json(
                503, {"ok": False, "error": "tunnel control not available on this server"})
        condition = _ensure_public_mutation_lifecycle(server)
        with condition:
            lifecycle_busy = getattr(
                server, "public_ingress_draining_generation", None
            ) is not None
        if not lifecycle_busy:
            self._reconcile_exited_tunnel(server)
        if action == "status":
            with condition:
                draining = getattr(
                    server, "public_ingress_draining_generation", None
                ) is not None
                current_url = None if draining else getattr(server, "tunnel_url", None)
                public_edit = bool(not draining and current_url and getattr(
                    server, "public_tunnel_edit", False))
                settlement_error = getattr(server, "tunnel_settlement_error", None)
            payload = {
                "ok": True, "url": current_url, "public_edit": public_edit,
                **({'public_comment': True} if not draining and current_url and getattr(server, 'public_tunnel_comment', False) else {}),
            }
            if draining:
                payload["draining"] = True
            if settlement_error:
                payload["error"] = settlement_error
            return self._send_json(200, payload)
        if action == "start":
            if (getattr(server, "proposal_service", None) is not None
                    and getattr(server, "studio_edit", True)):
                return self._send_json(409, {
                    "schema": 1, "error": {
                        "code": "proposal_review_requires_loopback",
                        "message": "Restart with --no-edit before attaching a tunnel.",
                        "request_id": "req_tunnel", "retryable": False,
                        "resync_required": False, "details": [],
                    }})
            if (allow_public_edit or allow_public_comment) and not getattr(server, "studio_edit", True):
                return self._send_json(
                    409, {"ok": False,
                          "error": "read-only studio cannot enable public editing"})
            ingress = None
            start_error = None
            provisional_owner = None
            lifecycle_error = None
            with condition:
                if getattr(server, "public_ingress_draining_generation", None):
                    lifecycle_error = (
                        getattr(server, "tunnel_settlement_error", None)
                        or "tunnel lifecycle is draining"
                    )
                else:
                    current_url = getattr(server, "tunnel_url", None)
                    if current_url:
                        current_edit = bool(getattr(server, "public_tunnel_edit", False))
                        if current_edit != allow_public_edit or bool(getattr(server, 'public_tunnel_comment', False)) != allow_public_comment:
                            return self._send_json(409, {
                                "ok": False,
                                "error": "stop the existing tunnel before changing public edit access",
                            })
                        return self._send_json(
                            200, {"ok": True, "url": current_url,
                                  "public_edit": current_edit,
                                  **({'public_comment': True} if allow_public_comment else {}),
                                  "already_running": True})
                    try:
                        ingress = _start_public_ingress(
                            server, allow_public_edit=allow_public_edit,
                            **({'allow_public_comment': True} if allow_public_comment else {}),
                        )
                        proc, url = start_quick_tunnel(ingress.server_address[1])
                    except RuntimeError as e:
                        start_error = e
                        if isinstance(e, TunnelStartOwnershipError):
                            provisional_owner = e.process
                            server.tunnel_proc = provisional_owner  # type: ignore[attr-defined]
                        server.public_ingress_accepting = False
                        server.public_ingress_accepting_generation = None
                    else:
                        server.tunnel_proc = proc  # type: ignore[attr-defined]
                        server.tunnel_url = url  # type: ignore[attr-defined]
            if lifecycle_error is not None:
                return self._send_json(409, {
                    "ok": False, "error": lifecycle_error, "retryable": True,
                })
            if start_error is not None:
                if provisional_owner is not None:
                    _clear_owned_tunnel(server, self.studio)
                elif ingress is not None:
                    _stop_public_ingress(server)
                payload = {"ok": False, "error": str(start_error)}
                if provisional_owner is not None:
                    payload["retryable"] = True
                return self._send_json(502, payload)
            write_server_state(server, self.studio)
            self._emit_tunnel_activity("tunnel_attach",
                                       f"public tunnel attached: {url}")
            return self._send_json(200, {
                "ok": True, "url": url, "public_edit": allow_public_edit,
                **({'public_comment': True} if allow_public_comment else {}),
            })
        # stop
        with condition:
            current_url = getattr(server, "tunnel_url", None)
        cleared = _clear_owned_tunnel(
            server, self.studio,
            activity_summary=(
                f"public tunnel detached: {current_url}" if current_url else None
            ),
        )
        if not cleared:
            with condition:
                lifecycle_error = (
                    getattr(server, "tunnel_settlement_error", None)
                    or "tunnel lifecycle is draining"
                )
            return self._send_json(503, {
                "ok": False, "error": lifecycle_error, "retryable": True,
            })
        return self._send_json(200, {"ok": True, "url": None,
                                     "stopped": current_url is not None})

    def _reconcile_exited_tunnel(self, server: Any) -> bool:
        """Revoke an advertised tunnel whose owned process is no longer live.

        Process-like test doubles that do not expose ``poll`` remain
        authoritative; an ordinary polling error cannot safely prove liveness,
        so it fails closed like a reported exit.
        """
        condition = _ensure_public_mutation_lifecycle(server)
        with condition:
            proc = getattr(server, "tunnel_proc", None)
            generation = getattr(server, "public_ingress_generation", None)
            advertised_url = getattr(server, "tunnel_url", None)
            settlement_pending = getattr(server, "tunnel_settlement_pending", False)
        if settlement_pending:
            return False
        poll = getattr(proc, "poll", None) if proc is not None else None
        if not callable(poll):
            return False
        try:
            returncode = poll()
        except Exception:
            returncode = "poll_error"
        if returncode is None:
            return False
        return _clear_owned_tunnel(
            server, self.studio,
            expected_proc=proc,
            expected_generation=generation,
            activity_summary=(
                f"public tunnel lost after cloudflared exited: {advertised_url}"
                if advertised_url else None
            ),
        )

    def _emit_tunnel_activity(self, action: str, summary: str) -> None:
        """Best-effort change-list entry so the share/unshare is visible."""
        _emit_owned_tunnel_activity(self.studio, summary, action=action)

    def _handle_apply(self, data: dict[str, Any]) -> None:
        """Run a whitelisted UpdateOp via the studio write funnel (§9.5/§12.3).

        Routes through ``update.apply_plan(..., studio=self.studio)`` so every
        write lands through :meth:`Studio.save_concept` — the single internal
        write path that:

        * validates the concept id (path containment, §9.5);
        * runs the §9.3 collision guard (``expected_rev`` mismatch → 409);
        * atomically writes (AGENTS.md hard rule #8);
        * snapshots prior bytes for one-click Undo (§12.5);
        * records an attributed activity event with `before/snap_rev` in
          detail (§12.2);
        * marks the rev as logged so the watcher's disk event de-dupes it
          (§10.5); and
        * broadcasts `changed` + `graph` (for graph-affecting ops) over SSE.

        The endpoint NEVER accepts a shell/argv string — only a known
        UpdateOp kind + args (§15.5). Returns 409 with a conflict payload
        when a concurrent on-disk edit moved the rev out from under us
        (§9.3/§9.4 — never silent-clobber).
        """
        from .update import UpdateOp, UpdatePlan, apply_plan
        from .paths import concept_id_from_str
        from .studio import validate_apply_args, _assert_safe_path_token

        kind = str(data.get("kind", "")).strip()
        target_str = str(data.get("target", "")).strip()
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        group_id = data.get("group_id")
        expected_rev = data.get("expected_rev")
        if expected_rev is not None:
            expected_rev = str(expected_rev)
        if group_id is not None:
            group_id = str(group_id)
        if not kind or not target_str:
            return self._send_json(400, {"ok": False, "error": "kind and target required"})
        # P2-17 (§12.3 / §15.5): fail-closed per-kind args validation on the
        # public HTTP surface. Returns 400 (with a stable machine-readable
        # reason prefix) for missing / wrongly-typed / unknown args BEFORE
        # the concept lookup or any write attempt. Library handlers
        # (``update._h_*``) stay lenient for in-process / CLI callers.
        valid, reason = validate_apply_args(kind, args)
        if not valid:
            return self._send_json(400, {"ok": False, "error": reason})
        # P2-15 (SEC-005 / §9.5): reject path-traversal-shaped group_id and
        # expected_rev values BEFORE they reach the studio's filesystem ops.
        # ``_assert_safe_path_token`` raises ValueError on a value containing
        # path separators, leading dots, or chars outside ``[A-Za-z0-9._-]``;
        # we surface that as 400 (not 500) so the contract reads cleanly.
        for label, value in (("group_id", group_id), ("expected_rev", expected_rev)):
            if value is None:
                continue
            try:
                _assert_safe_path_token(label, value)
            except ValueError as e:
                return self._send_json(400, {"ok": False, "error": f"unsafe {label}: {e}"})
        live, _, _, _ = self._state_snapshot()
        try:
            cid = concept_id_from_str(target_str)
        except ConceptIdError:
            return self._send_json(400, {"ok": False, "error": f"bad concept id: {target_str}"})
        # P1-1: short-circuit collision check when the caller passed an
        # explicit expected_rev. The caller (an agent that read the doc at
        # reasoning time, possibly seconds or minutes earlier) wants the
        # write to FAIL rather than clobber if the on-disk bytes have moved
        # since. This catches the §9.3/§9.4 case before apply_plan runs.
        if expected_rev is not None:
            from .studio import rev_of
            target_path = live.root / f"{target_str}.md"
            try:
                current_bytes = target_path.read_bytes() if target_path.is_file() else b""
            except OSError:
                current_bytes = b""
            current_rev = rev_of(current_bytes) if current_bytes else None
            if current_rev != expected_rev:
                return self._send_json(409, {
                    "ok": False, "conflict": True,
                    "concept": target_str,
                    "expected_rev": expected_rev,
                    "current_rev": current_rev,
                })
        # F1: operate on a PRIVATE fresh bundle so the live bundle concurrent
        # GET handlers read is never mutated in place (no torn concept reads).
        fresh = Bundle.load(live.root)
        op = UpdateOp(kind=kind, target=cid, args=args)
        plan = UpdatePlan(bundle_root=str(fresh.root), description="studio-apply",
                          ops=[op], plan_kind="studio-apply")
        # When the caller passed expected_rev, prime the studio's collision
        # guard so the FIRST write attempt surfaces the conflict.
        result = apply_plan(
            fresh, plan, apply=True,
            studio=self.studio,
            actor=str(data.get("actor", "agent")),
            origin="http-apply",
            group_id=group_id,
        )
        applied = bool(result.get("applied"))
        # Detect a §9.3 collision surfacing from the funnel.
        conflict = None
        for _op_dict, op_result in result.get("results", []):
            if op_result.get("reason") == "rev_conflict":
                conflict = op_result.get("conflict")
                break
        if conflict is not None:
            # §9.4 conflict UX — return 409 with current vs expected rev so
            # the client can show "Keep mine / Take the agent's / View diff".
            return self._send_json(409, {
                "ok": False, "conflict": True,
                "concept": target_str,
                "expected_rev": conflict.get("expected_rev"),
                "current_rev": conflict.get("current_rev"),
            })
        if applied:
            # F1: swap the private fresh bundle in atomically under the state
            # lock; clear the cached palette so it recomputes from the new
            # bundle. Concurrent handlers that snapshot under the same lock see
            # either the old or the new bundle, never a half-mutation.
            state_lock = self._state_lock()

            def _swap() -> None:
                owner = self._application_server()
                owner.bundle = fresh
                if hasattr(owner, "_palette"):
                    del owner._palette

            if state_lock is not None:
                with state_lock:
                    _swap()
            else:
                _swap()
            # Note: graph + changed events are emitted by the funnel
            # (Studio.save_concept → record_activity → emit_change), so we
            # do not duplicate them here.
        self._send_json(200, {"ok": True, "applied": applied, "result": result})

    def _handle_undo(self, data: dict[str, Any]) -> None:
        """Restore a prior snapshot via :meth:`Studio.restore_snapshot` (§12.5).

        Routes through the studio write funnel so the restore is itself an
        attributed, undoable write (you can redo by undoing the undo).
        """
        from .studio import _assert_safe_path_token
        from .paths import concept_id_from_str, ConceptIdError

        concept_id = str(data.get("concept", "")).strip()
        rev = str(data.get("rev", "")).strip()
        group_id = data.get("group_id")
        if group_id is not None:
            group_id = str(group_id)
        if not concept_id and not group_id:
            return self._send_json(400, {"ok": False, "error": "concept+rev or group_id required"})
        # Validate the concept id lexically, symmetric with /__apply, so a
        # malformed value is a 400 before any filesystem op (defense in
        # depth; the history dir lookup hex-encodes the id anyway).
        if concept_id:
            try:
                concept_id_from_str(concept_id)
            except ConceptIdError as e:
                return self._send_json(400, {"ok": False, "error": f"bad concept id: {e}"})
        # P2-15 (SEC-005 / §9.5): reject path-traversal-shaped rev / group_id
        # values BEFORE they reach the studio's filesystem ops so the
        # contract returns 400 (not 500). rev and group_id are joined into
        # history paths; an unsanitised value could escape the session dir.
        for label, value in (("rev", rev), ("group_id", group_id)):
            if not value:
                continue
            try:
                _assert_safe_path_token(label, value)
            except ValueError as e:
                return self._send_json(400, {"ok": False, "error": f"unsafe {label}: {e}"})
        targets: list[tuple[str, str]] = []
        if group_id:
            members = self.studio.undo_group(str(group_id))
            if not members:
                return self._send_json(404, {"ok": False, "error": "unknown group"})
            targets = [(m["concept"], m["rev"]) for m in members]
        else:
            targets = [(concept_id, rev)]
        # P1-11 (QUA idempotency-retry): an undo of an already-undone
        # activity returns 409 with the current state, not a fresh apply.
        # The snapshot's rev is a content hash; if every target's CURRENT
        # on-disk bytes already hash to the requested rev, the undo has
        # already happened (replay). This catches the "client retry after
        # network glitch" case without re-running the restore (which would
        # be a no-op anyway, but the contract wants an explicit 409 so the
        # caller can tell a replay from a fresh undo).
        from .studio import rev_of as _rev_of_undo
        already_undone: list[dict[str, Any]] = []
        for cid, r in targets:
            try:
                cur_path = self.server.bundle.root / f"{cid}.md"  # type: ignore[attr-defined]
                cur_bytes = cur_path.read_bytes() if cur_path.is_file() else b""
            except OSError:
                cur_bytes = b""
            cur_rev = _rev_of_undo(cur_bytes) if cur_bytes else None
            if cur_rev == r:
                already_undone.append({"concept": cid, "rev": r, "current_rev": cur_rev})
        if already_undone and len(already_undone) == len(targets):
            # Every target is already at its snapshot rev → fully replayed.
            return self._send_json(409, {
                "ok": False, "conflict": True,
                "error": "already_undone",
                "targets": already_undone,
                "current_state": already_undone,
            })
        restored = 0
        missing: list[dict[str, str]] = []
        # Reload bundle after each restore so subsequent restores observe
        # the prior restore's effect.
        for cid, r in targets:
            res = self.studio.restore_snapshot(
                concept_id=cid, rev=r, group_id=group_id,
            )
            if res.ok:
                restored += 1
            elif not res.conflict:
                # INTENT2-003 fix: snapshot was pruned by the history ring
                # cap. Surface as a per-target missing entry so the caller
                # knows the undo is PARTIAL, not silently successful.
                missing.append({"concept": cid, "rev": r, "error": res.error or "missing"})
        # INTENT2-003: if every target was missing (ring cap), return 404 so
        # the browser shows an error toast instead of a success toast while
        # the file is unchanged. If only SOME targets were missing, return 409
        # (partial) — the change-list entry remains for audit either way.
        if missing and not restored:
            return self._send_json(404, {
                "ok": False, "error": "snapshots_pruned",
                "missing": missing,
                "message": "Cannot undo — the snapshot(s) for this change were "
                           "pruned by the history ring cap. The change-list "
                           "entry remains for audit.",
            })
        # Swap in a freshly-loaded bundle so GET handlers see the restored
        # content (the restore wrote through save_concept which mutated the
        # underlying file; reload to refresh caches).
        if restored:
            _, _, _, _ = self._state_snapshot()
            live_root = self.bundle.root
            fresh = Bundle.load(live_root)
            state_lock = self._state_lock()

            def _swap() -> None:
                owner = self._application_server()
                owner.bundle = fresh
                if hasattr(owner, "_palette"):
                    del owner._palette

            if state_lock is not None:
                with state_lock:
                    _swap()
            else:
                _swap()
        if missing:
            return self._send_json(409, {
                "ok": True, "restored": restored, "partial": True,
                "missing": missing,
                "message": f"Partial undo: {restored} restored, "
                           f"{len(missing)} snapshot(s) pruned.",
            })
        self._send_json(200, {"ok": True, "restored": restored})

    def _handle_diff(self, query: dict[str, list[str]]) -> None:
        """Line diff between two revs of one concept for the §9.4 conflict UI.

        Query params: ``concept``, ``from`` (a content-hash rev), ``to`` (a
        content-hash rev). Returns ``{concept, from, to, diff: [...]}`` where
        ``diff`` is a list of ``{kind: "ctx"|"add"|"del", text, num}`` rows
        for the client to render. Reads snapshots from
        ``history/<hex>/<rev>.md``; if either is missing, returns 404.

        SEC2-004 + SEC2-005: the concept id is validated via
        ``concept_id_from_str`` (defense-in-depth; the studio's hex encoding
        already defuses traversal), and the endpoint requires the per-session
        CSRF token because it exposes prior snapshot bytes (a same-origin
        attacker driving the browser can't read it without the token embedded
        in the served page).
        """
        from .studio import rev_of as _rev_of
        import difflib

        # SEC2-005: require the token. Same-origin GET protects against
        # cross-origin reads (CORS blocks the response), but a same-origin
        # attacker (e.g. an XSS in the bundle content) could fetch this
        # without the token. The studio's own JS carries the token on every
        # fetch via ``tokenFetch``.
        if not self._check_write_auth():
            return self._send_text(
                403, "Forbidden: /__diff requires the studio token.",
                content_type="text/plain; charset=utf-8",
            )

        concept = (query.get("concept", [""])[0] or "").strip()
        from_rev = (query.get("from", [""])[0] or "").strip()
        to_rev = (query.get("to", [""])[0] or "").strip()
        if not concept or not from_rev or not to_rev:
            return self._send_json(400, {"ok": False, "error": "concept, from, to required"})
        # SEC2-004: validate concept as a real concept id (defense-in-depth).
        try:
            concept_id_from_str(concept)
        except ConceptIdError:
            return self._send_json(400, {"ok": False, "error": f"bad concept id: {concept}"})
        from_raw = self.studio.undo_snapshot(concept_id=concept, rev=from_rev)
        to_raw = self.studio.undo_snapshot(concept_id=concept, rev=to_rev)
        if from_raw is None or to_raw is None:
            return self._send_json(404, {"ok": False, "error": "snapshot missing"})
        from_lines = from_raw.splitlines(keepends=False)
        to_lines = to_raw.splitlines(keepends=False)
        diff_rows: list[dict[str, Any]] = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, from_lines, to_lines, autojunk=False
        ).get_opcodes():
            if tag == "equal":
                for n, line in enumerate(from_lines[i1:i2]):
                    diff_rows.append({"kind": "ctx", "num": i1 + n + 1, "text": line})
            elif tag in ("replace", "delete"):
                for n, line in enumerate(from_lines[i1:i2]):
                    diff_rows.append({"kind": "del", "num": i1 + n + 1, "text": line})
                if tag == "replace":
                    for n, line in enumerate(to_lines[j1:j2]):
                        diff_rows.append({"kind": "add", "num": j1 + n + 1, "text": line})
            elif tag == "insert":
                for n, line in enumerate(to_lines[j1:j2]):
                    diff_rows.append({"kind": "add", "num": j1 + n + 1, "text": line})
        self._send_json(200, {
            "ok": True, "concept": concept,
            "from": from_rev, "to": to_rev, "diff": diff_rows,
        })

    def _handle_preview(self, data: dict[str, Any]) -> None:
        """Render arbitrary in-flight markdown for preview (§9.5 DoS caps)."""
        md = str(data.get("markdown", ""))
        html = markdown_to_html(md)
        self._send_json(200, {"ok": True, "html": html})

    def _route(self) -> None:
        if self._is_public_tunnel_request() and not self._public_ingress_is_active():
            return self._send_text(
                403, "Public ingress capability is stale.",
                content_type="text/plain; charset=utf-8", close=True,
            )
        if self._is_public_tunnel_request() and (
            self.path.startswith("/__proposal/v1/")
            or urlparse(self.path).path == "/__diff"
        ):
            return self._send_text(
                403, "Public ingress cannot access server administration.",
                content_type="text/plain; charset=utf-8", close=True,
            )
        from .proposal_review.http import dispatch as dispatch_proposal
        if dispatch_proposal(self, "GET"):
            return
        from .retrieval import (MAX_REQUEST_TARGET_BYTES, RetrievalContractError,
                                strict_decode_path)
        if len(self.path.encode("utf-8", errors="replace")) > MAX_REQUEST_TARGET_BYTES:
            return self._send_contract_error(414, "request_target_too_large",
                                             "Request target exceeds the size limit")
        parsed = urlparse(self.path)
        try:
            path = strict_decode_path(parsed.path)
        except RetrievalContractError as exc:
            return self._send_json(exc.status, exc.as_error())
        # Normalize trailing slash (except for root).
        if path != "/" and path.endswith("/"):
            path = path[:-1]
        # Normalize trailing .md → extensionless concept route.
        # SPEC §5 links use /tables/users.md or ./users.md; the server's
        # canonical route is /tables/users (no extension). Stripping .md
        # at the route level makes every valid OKF link reachable.
        # BUT: do NOT strip /index.md or <dir>/index.md — those are
        # reserved-filename routes handled separately below.
        if path.endswith(".md") and not path.startswith("/__") and not path.endswith("/index.md") and path != "/index.md":
            path = path[:-3]

        # Search/context own a strict raw scalar parser. Route them before the
        # legacy permissive parse_qs used by unrelated endpoints.
        if path == "/__search":
            return self._handle_search(parsed.query)
        if path == "/__search-form":
            # Native HTML forms send empty successful controls. This read-only
            # presentation adapter redirects to the strict canonical endpoint;
            # API requests to /__search are never silently normalized.
            from .retrieval import parse_http_query, RetrievalContractError
            try:
                values = parse_http_query(re.sub(r"%2[fF]", "/", parsed.query), allowed={
                    "q", "mode", "type", "tag", "entity", "relation", "source", "target", "limit",
                })
                search_mode = values.get("mode", "lexical")
                for key in ("type", "tag", "entity", "relation", "source", "target"):
                    if not values.get(key) or (key == "entity" and search_mode != "entity") or (
                        key in {"relation", "source", "target"} and search_mode != "relation"
                    ):
                        values.pop(key, None)
                from urllib.parse import urlencode
                self.send_response(303)
                self.send_header("Location", "/__search?" + urlencode(values, safe="/"))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            except RetrievalContractError as exc:
                # Preserve the adapter's closed-key rejection. Re-parsing as
                # strict search alone would accidentally accept format=...,
                # which belongs to the API but not this native form surface.
                return self._handle_search(parsed.query, parse_error=exc)
        if path == "/__context":
            return self._handle_context(parsed.query)
        query = parse_qs(parsed.query, keep_blank_values=True)

        # Internal endpoints first.
        if path == "/":
            return self._handle_root()
        if path == "/__events":
            return self._handle_events(query)
        if path == "/__presence":
            return self._handle_presence_get()
        if path == "/__data/doc":
            return self._handle_data_doc(query)
        if path == "/__data/page":
            return self._handle_data_page(query)
        if path == "/__data/events":
            return self._handle_data_events(query)
        if path == "/__comments":
            return self._handle_comments(query)
        if path == "/__feedback-import":
            return self._handle_feedback_import_page()
        if path == "/__diff":
            return self._handle_diff(query)
        if path in ("/__graph", "/__graph.html"):
            return self._handle_graph()
        if path == "/__data/graph.json":
            return self._handle_graph_json()
        if path == "/__data/content.json":
            return self._handle_content_json()
        if path == "/__data/summary":
            return self._handle_data_summary(query)
        if path.startswith("/__raw/"):
            return self._handle_raw(path[len("/__raw/"):])
        if path.startswith("/__static/"):
            return self._handle_static(path[len("/__static/"):])
        if path in ("/index.md", "/index.html"):
            return self._handle_root()
        if path.endswith("/index.md"):
            return self._handle_dir_index(path[:-len("/index.md")])
        if path == "/log" or path.endswith("/log"):
            return self._handle_reserved_page(path.lstrip('/') + '.md')
        # Bundle-local media: any path with an allowlisted media extension
        # is served from the bundle tree (visibility + containment checks
        # live in the handler). Everything else stays a concept path.
        if posixpath.splitext(path)[1].lower() in BUNDLE_MEDIA_EXTENSIONS:
            return self._handle_bundle_asset(path)
        # Concept page.
        return self._handle_concept(path)

    # --- Route handlers ---------------------------------------------------
    def _page_payload(self, document: str) -> dict[str, Any]:
        from .documents import document_path, document_concept
        from .navigation import normalize_document_links
        document = document_path(document)
        source = _canonical_document_source(self.bundle, document)
        # Reserved pages intentionally expose no projection or guessed offsets.
        body = parse_document(source)[1] if document_concept(document) or document == 'index.md' else source
        return {"schema": 1, "document": document,
                "concept": document_concept(document) or None,
                "document_content_rev": hashlib.sha256(source.encode('utf-8')).hexdigest(),
                "raw": source, "html": normalize_document_links(markdown_to_html(body), document, 'serve'),
                "anchor": {"kind": "page"}}

    def _handle_data_page(self, query: dict[str, list[str]]) -> None:
        if set(query) != {'document'} or len(query['document']) != 1:
            return self._send_json(400, {"error": "invalid_request"})
        try:
            payload = self._page_payload(query['document'][0])
        except (TypeError, ValueError):
            return self._send_json(400, {"error": "invalid_request"})
        except (OSError, OKFParseError):
            return self._send_json(404, {"error": "target_not_found"})
        self._send_json(200, payload)

    def _handle_reserved_page(self, document: str) -> None:
        try:
            payload = self._page_payload(document)
        except (OSError, ValueError, OKFParseError):
            return self._send_text(404, 'Not found', content_type='text/plain')
        self._page_document = document
        # Small reading adapter; the shared Studio bootstrap owns collaboration.
        from .render import render_reserved_document
        self._send_text(200, render_reserved_document(
            document, payload['raw'], mode='serve', config=self._config(),
            bundle=self.bundle, name=self.display_name))

    def _handle_root(self) -> None:
        self._page_document = 'index.md' if self.bundle.root_index() else None
        comment_summary = None
        engagement_events = None
        if self.studio is not None:
            concepts = {concept_id_to_str(c.id) for c in self.bundle.concepts.values()}
            try:
                comment_summary = self.studio.comment_summary(graph_concepts=concepts)
                engagement_events = self.studio.read_events(limit=500, order="desc")
            except (CommentSummaryUnavailable, OSError, ValueError):
                pass
        html = _render_index_page(
            self.bundle, mode="serve", name=self.display_name,
            palette=self.palette, config=self._config(), sub="",
            index_file=self.bundle.root_index(),
            comment_summary=comment_summary,
            engagement_events=engagement_events,
        )
        html = self._plugin().on_index_render(html)
        self._send_text(200, html)

    def _handle_dir_index(self, sub: str) -> None:
        sub = sub.lstrip("/")
        # Validate sub path segments to prevent traversal / weird inputs.
        # bundle.indexes.get() is already safe (returns None for unknown
        # keys), but we validate explicitly as defence in depth: any future
        # change that reads from disk using ``sub`` would silently introduce
        # traversal if we didn't pin the contract here.
        if sub:
            for segment in sub.split("/"):
                if not segment:
                    continue
                # Reject obvious traversal attempts.
                if segment in ("..", ".") or "\x00" in segment:
                    return self._send_text(
                        404, "Not found", content_type="text/plain; charset=utf-8"
                    )
        rel_index_md = Path(sub) / "index.md" if sub else Path("index.md")
        index_file = self.bundle.indexes.get(rel_index_md)
        self._page_document = str(rel_index_md) if index_file is not None else None
        # P2-54 server-side defense: ``index.md`` / ``log.md`` are reserved
        # files NOT covered by the concept-level symlink containment in
        # Bundle.load (model.py — REPORTED). If the loaded index file's
        # resolved path escapes the bundle root (e.g. a symlinked
        # ``subdir/index.md`` pointing at ``/etc/passwd``), refuse to serve.
        if index_file is not None and getattr(index_file, "path", None) is not None:
            if not _path_within(index_file.path, self.bundle.root):
                return self._send_text(
                    404, "Not found", content_type="text/plain; charset=utf-8"
                )
        html = _render_index_page(
            self.bundle, mode="serve", name=self.display_name,
            palette=self.palette, config=self._config(), sub=sub,
            index_file=index_file,
            comment_summary=None,
            engagement_events=None,
        )
        html = self._plugin().on_index_render(html)
        self._send_text(200, html)

    def _send_document_not_found(self, cid_str: str, plain_message: str) -> None:
        # No document authority on this page, including reused HTTP connections.
        self._page_document = None
        headers = {"Vary": "Accept"}
        if _prefers_html_error(",".join(self.headers.get_all("Accept", []))):
            return self._send_text(404, _render_not_found_page(
                name=self.display_name, path=cid_str, config=self._config(),
            ), extra_headers=headers)
        self._send_text(404, plain_message, content_type="text/plain", extra_headers=headers)

    def _handle_concept(self, cid_str: str) -> None:
        cid_str = cid_str.lstrip("/")
        try:
            cid = concept_id_from_str(cid_str)
        except ConceptIdError:
            return self._send_document_not_found(cid_str, f"Not a concept id: {cid_str}")
        bundle = self.bundle
        concept = bundle.concepts.get(cid)
        try:
            canonical_path = concept_id_to_path(bundle.root, cid)
            with path_lock(canonical_path):
                annotation_source = _canonical_annotation_source(bundle, cid)
                parse_document(annotation_source)
            if concept is None:
                current_bundle = Bundle.load(bundle.root)
                concept = current_bundle.concepts.get(cid)
                if concept is None:
                    raise OSError("canonical concept is not parseable")
                bundle = current_bundle
            else:
                concept = concept._private_copy()
                concept._adopt_raw_text(annotation_source)
                bundle = Bundle(bundle.root, name=bundle.name,
                    concepts={**bundle.concepts, cid: concept}, indexes=dict(bundle.indexes),
                    logs=dict(bundle.logs), okf_version=bundle.okf_version,
                    okf_extensions=list(bundle.okf_extensions), warnings=list(bundle.warnings))
        except (OSError, UnicodeDecodeError, OKFParseError):
            concept = None
        if concept is None:
            # Friendly fallback: if the path matches a directory containing
            # concepts, serve the directory index page instead of a 404.
            if any(c.id[:len(cid)] == cid for c in self.bundle.concepts.values()):
                return self._handle_dir_index("/".join(cid))
            return self._send_document_not_found(cid_str, f"Unknown concept: {cid_str}")
        self._page_document = cid_str + '.md'
        graph = bundle.graph()
        plugin = self._plugin()
        comment_summary = None
        if self.studio is not None:
            concepts = {concept_id_to_str(c.id) for c in bundle.concepts.values()}
            try:
                comment_summary = self.studio.comment_summary(graph_concepts=concepts)
            except (CommentSummaryUnavailable, OSError, ValueError):
                pass
        html = _render_concept_page(
            concept, bundle, graph, mode="serve",
            name=self.display_name, palette=self.palette,
            config=self._config(),
            body_plugin=plugin.on_concept_render,
            comment_summary=comment_summary,
        )
        self._send_text(200, html)

    def _handle_graph(self) -> None:
        html = _render_graph_page(
            self.bundle, mode="serve", name=self.display_name,
            config=self._config(),
        )
        self._send_text(200, html)

    def _handle_search(self, raw_query: str, *, parse_error=None) -> None:
        from .retrieval import (RetrievalContractError, SearchRequest,
                                bound_search_payload, parse_http_query, parse_int_param,
                                prescan_search_error_format)
        fmt = "html"
        error_format = prescan_search_error_format(raw_query)
        bundle, _palette, _plugin, config = self._state_snapshot()
        display_name = self.display_name
        params = {}
        try:
            if parse_error is not None:
                raise parse_error
            params = parse_http_query(raw_query, allowed={
                "q", "format", "mode", "type", "tag", "entity", "relation",
                "source", "target", "limit",
            })
            fmt = params.get("format", "html")
            if fmt not in {"html", "json"}:
                raise RetrievalContractError("invalid_request", "Unknown search format",
                                             fields={"format": "unsupported"})
            request = SearchRequest(query=params.get("q", ""), mode=params.get("mode", "lexical"),
                type_filter=params.get("type"), tag=params.get("tag"), entity=params.get("entity"),
                relation=params.get("relation"), source=params.get("source"), target=params.get("target"),
                limit=parse_int_param(params, "limit", 20)).normalized()
            def prepare_search_response():
                from .retrieval import RetrievalService, check_retrieval_cancelled
                with bundle._derived_lock:
                    service = RetrievalService(bundle)
                    rows = self._run_search(
                        request.query, limit=request.limit, bundle=bundle, request=request,
                    )
                    revision_value = service.revision
                    rows, omitted = bound_search_payload(rows)
                    check_retrieval_cancelled()
                    if fmt == "json":
                        code, body = self._prepare_retrieval_json(rows)
                        content_type = "application/json; charset=utf-8"
                    else:
                        sections = _search_section_fragments(
                            rows, service, revision_value, request.query,
                        )
                        html = _render_search_page(
                            bundle, mode="serve", name=display_name, config=config,
                            query=request.query, results=rows, request=request,
                            sections=sections,
                        )
                        code, body = self._prepare_retrieval_text(html, html=True)
                        content_type = "text/html; charset=utf-8"
                    check_retrieval_cancelled()
                    return code, body, content_type, revision_value, omitted

            code, body, content_type, revision, omitted_count = self._run_retrieval_bounded(
                prepare_search_response
            )
            headers = {"ETag": f'"{revision}"', "X-OKF-Bundle-Revision": revision,
                       "Cache-Control": "private, no-cache"}
            if omitted_count:
                headers["X-OKF-Omitted-Count"] = str(omitted_count)
            self._send_bytes(code, body, content_type=content_type, extra_headers=headers)
        except RetrievalContractError as exc:
            if fmt == "json" or error_format == "json":
                self._send_json(exc.status, exc.as_error())
            elif exc.code in {"retrieval_timeout", "retrieval_busy"}:
                self._send_retrieval_error_html(exc, params=params)
            else:
                if not params:
                    # A malformed/unknown field must not erase other valid
                    # form controls. Ambiguous duplicate fields are not chosen.
                    seen = set()
                    for pair in raw_query.split("&")[:100]:
                        try:
                            recovered = parse_http_query(pair, allowed={
                                "q", "mode", "type", "tag", "entity", "relation", "source", "target", "limit",
                            })
                        except RetrievalContractError:
                            continue
                        for key, value in recovered.items():
                            if key in seen:
                                params.pop(key, None)
                            else:
                                params[key] = value
                            seen.add(key)
                self._send_search_error_html(
                    exc, bundle=bundle, config=config, display_name=display_name,
                    params=params,
                )

    def _handle_context(self, raw_query: str) -> None:
        from .retrieval import (ContextRequest, RetrievalContractError, RetrievalService,
                                parse_http_query, parse_int_param)
        try:
            params = parse_http_query(raw_query, allowed={
                "q", "format", "mode", "type", "tag", "entity", "relation", "source",
                "target", "concept", "selection_exact", "selection_prefix",
                "selection_suffix", "selection_heading", "limit", "budget_tokens", "neighbors",
            })
            fmt = params.get("format", "json")
            if fmt not in {"json", "markdown"}:
                raise RetrievalContractError("invalid_request", "Unknown context format",
                                             fields={"format": "unsupported"})
            bundle, _palette, _plugin, _config = self._state_snapshot()
            service = RetrievalService(bundle)
            context_request = ContextRequest(query=params.get("q", ""),
                mode=params.get("mode", "hybrid"), type_filter=params.get("type"),
                tag=params.get("tag"), entity=params.get("entity"), relation=params.get("relation"),
                source=params.get("source"), target=params.get("target"), concept=params.get("concept"),
                selection_exact=params.get("selection_exact"),
                selection_prefix=params.get("selection_prefix"),
                selection_suffix=params.get("selection_suffix"),
                selection_heading=params.get("selection_heading"),
                limit=parse_int_param(params, "limit", 20),
                budget_tokens=parse_int_param(params, "budget_tokens", 6000),
                neighbors=parse_int_param(params, "neighbors", 1))
            def prepare_context_response():
                from .retrieval import check_retrieval_cancelled
                with bundle._derived_lock:
                    pack = service.context(context_request)
                    revision_value = pack["bundle_revision"]
                    check_retrieval_cancelled()
                    if fmt == "markdown":
                        code, body = self._prepare_retrieval_text(
                            service.context_markdown(pack), html=False
                        )
                        content_type = "text/markdown; charset=utf-8"
                    else:
                        code, body = self._prepare_retrieval_json(pack)
                        content_type = "application/json; charset=utf-8"
                    check_retrieval_cancelled()
                    return code, body, content_type, revision_value

            code, body, content_type, revision = self._run_retrieval_bounded(
                prepare_context_response
            )
            headers = {"ETag": f'"{revision}"', "X-OKF-Bundle-Revision": revision,
                       "Cache-Control": "private, no-cache"}
            self._send_bytes(code, body, content_type=content_type, extra_headers=headers)
        except RetrievalContractError as exc:
            self._send_json(exc.status, exc.as_error())

    def _run_search(self, q: str, *, limit: int, bundle: Bundle | None = None,
                    request: Any = None) -> list[dict[str, Any]]:
        """Run the lexical search backend and return rendered result rows.

        Security/determinism (P1-35): there is NO silent substring fallback.
        The lexical backend (``okf_loom.search``) is always-on; if it
        raises, the exception propagates to :meth:`do_GET`, which logs the
        full traceback to stderr and returns a clean 500 to the client. We
        deliberately do NOT fall back to a different ranking algorithm: two
        identical ``(bundle, query)`` inputs MUST rank identically, and a
        hidden fallback that scored by a different algorithm (different
        fields, score=1.0, different snippets) would silently violate that
        contract whenever a backend threw — with no log.
        """
        request_hint = request
        if not q and request_hint is None:
            return []
        # The lexical backend is part of this package; the import always
        # succeeds. If it ever failed (e.g. a broken install), the
        # ImportError propagates to do_GET → 500, which is correct: search
        # is unavailable, not silently degraded.
        from .retrieval import RetrievalService, SearchRequest
        snapshot = bundle or self.bundle
        req = request_hint or SearchRequest(query=q, limit=limit)
        return [r.as_dict() for r in RetrievalService(snapshot).search(req)]

    def _run_retrieval_bounded(self, operation):
        from .retrieval import (RetrievalContractError, set_retrieval_cancel_event)
        owner = self._application_server()
        gate, executor = _retrieval_runtime(owner)
        if not gate.acquire(blocking=False):
            raise RetrievalContractError("retrieval_busy", "Retrieval capacity is busy", status=503)
        cancel_event = threading.Event()

        def run_owned():
            set_retrieval_cancel_event(cancel_event)
            try:
                return operation()
            finally:
                set_retrieval_cancel_event(None)

        try:
            future = executor.submit(run_owned)
        except Exception:
            gate.release()
            raise
        # The future owns admission release, including timeout/abandon paths.
        future.add_done_callback(lambda _future: gate.release())
        try:
            return future.result(timeout=RETRIEVAL_DEADLINE_SECONDS)
        except FutureTimeoutError as exc:
            cancel_event.set()
            raise RetrievalContractError(
                "retrieval_timeout", "Retrieval deadline exceeded", status=503
            ) from exc

    def _prepare_retrieval_json(self, value: Any) -> tuple[int, bytes]:
        """Serialize and cap a retrieval response inside its absolute deadline."""
        from .retrieval import (MAX_RESPONSE_BYTES, RetrievalContractError,
                                canonical_json_bytes, check_retrieval_cancelled)
        check_retrieval_cancelled()
        body = canonical_json_bytes(value)
        check_retrieval_cancelled()
        if len(body) > MAX_RESPONSE_BYTES:
            raise RetrievalContractError(
                "response_too_large", "Response exceeds the size limit", status=413
            )
        return 200, body

    def _prepare_retrieval_text(self, value: str, *, html: bool) -> tuple[int, bytes]:
        """Render bootstrap, encode, and cap retrieval text before deadline expiry."""
        from .retrieval import (MAX_RESPONSE_BYTES, RetrievalContractError,
                                check_retrieval_cancelled)
        check_retrieval_cancelled()
        if html and "</head>" in value:
            value = value.replace("</head>", self._studio_bootstrap() + "\n</head>", 1)
        body = value.encode("utf-8")
        check_retrieval_cancelled()
        if len(body) > MAX_RESPONSE_BYTES:
            raise RetrievalContractError(
                "response_too_large", "Response exceeds the size limit", status=413
            )
        return 200, body

    def _send_retrieval_error_html(self, exc, *, params=None) -> None:
        """Emit deadline/capacity errors without re-entering the busy bundle lock."""
        code = html.escape(exc.code, quote=True)
        message = html.escape(exc.message, quote=False)
        # This recovery form must not access derived bundle data or templates:
        # timed-out work retains its lock and admission slot until it exits.
        # Values are display-only; resubmission crosses the strict search route.
        form = ""
        if params is not None:
            fields = "".join(
                f'<label>{key}<input name="{key}" value="{html.escape(value, quote=True)}"></label>'
                for key, value in params.items()
                if key in {"q", "mode", "type", "tag", "entity", "relation", "source", "target", "limit"}
            )
            form = ('<form action="/__search-form" method="get" role="search">'
                    + fields + '<button type="submit">Search</button></form>')
        body = (
            "<!doctype html><html><head><title>Retrieval unavailable</title></head>"
            f'<body data-okf-error-code="{code}"><h1>Retrieval unavailable</h1>'
            f"<p>{message}</p>{form}</body></html>"
        )
        self._send_text(exc.status, body, content_type="text/html; charset=utf-8")

    def _send_search_error_html(self, exc, *, bundle: Bundle | None = None,
                                config: dict[str, Any] | None = None,
                                display_name: str | None = None, params=None) -> None:
        if bundle is None or config is None:
            bundle, _palette, _plugin, config = self._state_snapshot()
        from types import SimpleNamespace
        params = params or {}
        # Display only. Invalid values must survive correction without ever
        # being treated as a validated retrieval request.
        request = SimpleNamespace(mode=params.get("mode", "lexical"),
            limit=params.get("limit", "20"), type_filter=params.get("type"),
            **{key: params.get(key) for key in ("tag", "entity", "relation", "source", "target")})
        body = _render_search_page(
            bundle, mode="serve", name=display_name or self.display_name, config=config,
            query=params.get("q", ""), results=[], error=exc, request=request,
        )
        self._send_text(exc.status, body, content_type="text/html; charset=utf-8")

    def _handle_raw(self, cid_str: str) -> None:
        cid_str = cid_str.lstrip("/")
        try:
            cid = concept_id_from_str(cid_str)
        except ConceptIdError:
            return self._send_text(404, f"Not a concept id: {cid_str}", content_type="text/plain")
        concept = self.bundle.concepts.get(cid)
        if concept is None:
            return self._send_text(404, f"Unknown concept: {cid_str}", content_type="text/plain")
        # P2-55: DoS backstop. The authoritative body-size cap belongs in
        # parse.py (REPORTED); the server additionally refuses to ship a
        # single raw body larger than MAX_RAW_RESPONSE_BYTES so one huge
        # concept cannot saturate a handler. 413 is the right status for
        # "entity too large"; Content-Type is plain text to avoid rendering.
        body = concept.body
        if len(body.encode("utf-8", errors="replace")) > MAX_RAW_RESPONSE_BYTES:
            return self._send_text(
                413,
                f"Request entity too large: raw body exceeds "
                f"{MAX_RAW_RESPONSE_BYTES} bytes.",
                content_type="text/plain; charset=utf-8",
            )
        self._send_text(200, body, content_type="text/markdown; charset=utf-8")

    def _handle_data_summary(self, query: dict[str, list[str]]) -> None:
        """One concept's identity card: what a link preview shows, nothing more.

        ``/__raw/<id>`` is deliberately the body only, so a preview built from
        it cannot know the concept's title or type (it showed the id and a
        generic type). This read-only projection comes from the same parsed
        snapshot as the page: title, type, description, the palette's type
        colour and icon key, and counts of internal body links and typed
        ``relations`` entries. No body, no rendering.
        """
        if set(query) != {"id"} or len(query["id"]) != 1:
            return self._send_json(400, {"error": "invalid_request"})
        cid_str = query["id"][0].strip().strip("/")
        if cid_str.endswith(".md"):
            cid_str = cid_str[:-3]
        try:
            cid = concept_id_from_str(cid_str)
        except ConceptIdError:
            return self._send_json(404, {"error": "target_not_found"})
        bundle, palette, _plugin, _config = self._state_snapshot()
        concept = bundle.concepts.get(cid)
        if concept is None:
            return self._send_json(404, {"error": "target_not_found"})
        from .viewer.assets import type_color_for, type_icon_key
        type_name = concept.type
        relations = concept.frontmatter.get("relations")
        self._send_json(200, {
            "schema": 1,
            "id": concept_id_to_str(cid),
            "title": concept.title,
            "type": type_name,
            "description": concept.description,
            "type_color": (palette or {}).get(type_name or "", type_color_for(type_name)),
            "icon": type_icon_key(type_name),
            "links": sum(1 for link in concept.links(bundle_root=bundle.root) if link.is_internal),
            "relations": sum(
                1 for entry in relations
                if isinstance(entry, dict) and isinstance(entry.get("target"), str) and entry["target"]
            ) if isinstance(relations, list) else 0,
        })

    def _handle_graph_json(self) -> None:
        bundle, _palette, _plugin, config = self._state_snapshot()
        display_name = getattr(self.server, "name", None) or config.get("name") or bundle.name
        comment_summary = None
        if self.studio is not None:
            concepts = {
                concept_id_to_str(concept.id)
                for concept in bundle.concepts.values()
            }
            try:
                comment_summary = self.studio.comment_summary(graph_concepts=concepts)
            except CommentSummaryUnavailable:
                return self._send_json(
                    503,
                    {"ok": False, "error": "comment_summary_unavailable"},
                    extra_headers={"Cache-Control": "no-store"},
                )
        data = build_graph_data(
            bundle,
            name=display_name,
            comment_summary=comment_summary,
        )
        self._send_json(200, data, extra_headers={"Cache-Control": "no-store"})

    def _handle_content_json(self) -> None:
        data = _content_index_json(self.bundle)
        data["name"] = self.display_name
        self._send_json(200, data)

    # --- Live studio: live push + per-doc + events feed (§7, §9) -----------
    def _handle_events(self, query: dict[str, list[str]] | None = None) -> None:
        """SSE stream of change/presence/activity/comment events (§7.1).

        Registers a bounded queue with the EventBus and streams events until
        the client disconnects, with a 15s heartbeat. Capped at
        ``max_sse_clients`` concurrent streams (503 when full). The handler
        thread blocks for the connection lifetime (ThreadingHTTPServer), so
        the cap bounds resource use.

        P2-14: the max-clients check + the subscribe run under
        ``_state_lock`` so concurrent bursts cannot over-admit (TOCTOU).
        P1-6: the ``ready`` frame carries ``doc_revs`` (cid → rev) so the
        client can detect which docs moved during reconnect.
        """
        # F12: a HEAD request must NOT enter the SSE loop — it has no body to
        # read and no client reading the stream, so it would block this worker
        # thread forever. Return an empty body immediately.
        if getattr(self, "_head_only", False):
            return self._send_text(200, "")
        studio = self.studio
        if studio is None:
            return self._send_text(
                503, "Studio (live updates) not enabled.", content_type="text/plain"
            )
        if not getattr(self.server, "studio_live", True):
            # P1-15: --no-watch-ui disables SSE live push entirely. The
            # endpoint returns 503 so a client knows to fall back to reads.
            return self._send_text(
                503, "Live push disabled (--no-watch-ui).",
                content_type="text/plain",
            )
        if any(key != "since" or len(values) != 1 for key, values in query.items()):
            return self._send_json(400, {"ok": False, "error": "invalid_request"})
        requested_cursor = self.headers.get("Last-Event-ID", "").strip()
        if not requested_cursor and query:
            requested_cursor = (query.get("since", [""])[0] or "").strip()
        initial_frames: list[dict[str, Any]] = []
        replay_high = -1
        max_clients = getattr(self.server, "max_sse_clients", DEFAULT_MAX_SSE_CLIENTS)
        # Subscribe before taking the replay snapshot. Events published while
        # the snapshot is read are then present in both places and are safely
        # removed from the live queue by the replay high-water mark below.
        state_lock = self._state_lock()
        q: queue.Queue | None = None
        try:
            if state_lock is not None:
                with state_lock:
                    if studio.bus.client_count() >= max_clients:
                        return self._send_text(503, "Too many live clients.", content_type="text/plain")
                    q = studio.bus.subscribe()
            else:
                if studio.bus.client_count() >= max_clients:
                    return self._send_text(503, "Too many live clients.", content_type="text/plain")
                q = studio.bus.subscribe()
            doc_revs = self._compute_doc_revs()
            ready_rev = studio.current_rev()
            initial_frames.append({"type": "ready", "rev": ready_rev, "doc_revs": doc_revs})
            if requested_cursor:
                try:
                    cursor = int(requested_cursor)
                    if cursor < 0:
                        raise ValueError
                except ValueError:
                    initial_frames.append({"type": "resync", "reason": "invalid_cursor",
                                           "rev": ready_rev, "doc_revs": doc_revs})
                else:
                      numeric = [row for row in studio.read_event_snapshot()
                                 if isinstance(row.get("event_cursor"), int)]
                      positions = {row["event_cursor"] for row in numeric}
                      reason = None
                      if not numeric and cursor > 0:
                          reason = "history_empty"
                      elif numeric and cursor < numeric[0]["event_cursor"] and cursor not in positions:
                          reason = "cursor_expired"
                      elif numeric and cursor and cursor not in positions and cursor < numeric[-1]["event_cursor"]:
                          reason = "cursor_gap"
                      elif numeric and cursor > numeric[-1]["event_cursor"]:
                          reason = "future_cursor"
                      pending = [row for row in numeric if row["event_cursor"] > cursor]
                      if reason is None and len(pending) > 10_000:
                          reason = "replay_too_large"
                      if reason:
                          initial_frames.append({"type": "resync", "reason": reason,
                                                 "rev": ready_rev, "doc_revs": doc_revs})
                      else:
                          initial_frames.extend(pending)
                          if pending:
                              replay_high = pending[-1]["event_cursor"]
        except (OSError, UnicodeError, ValueError, OKFParseError):
            if q is not None:
                studio.bus.unsubscribe(q)
            return self._send_json(503, {"ok": False, "error": "unavailable"})
        # Send the SSE response headers (no Content-Length → stream stays open).
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # P1-6: initial 'ready' event carries the current rev AND a per-doc
        # rev map (cid → content-hash) so the client can detect which docs
        # moved during a reconnect and refetch just those.
        for frame in initial_frames:
            self._sse_write(frame)
        try:
            while True:
                try:
                    event = q.get(timeout=15.0)
                except queue.Empty:
                    # Heartbeat keeps proxies from timing the idle stream out.
                    # Naming the heartbeat ``type=ping`` lets the client count
                    # it as "stream alive" for its polling-watchdog grace
                    # (§7.3 INTENT-004 fix).
                    self._sse_write({"type": "ping"}, comment="ping")
                    continue
                if event.get("__stop__"):
                    break
                if event.get("type") == "resync":
                    try:
                        payload = {
                            "type": "resync", "rev": studio.current_rev(),
                            "doc_revs": self._compute_doc_revs(),
                        }
                    except (OSError, UnicodeError, ValueError, OKFParseError):
                        self._sse_write({"type": "unavailable", "reason": "canonical_state"})
                        break
                    self._sse_write(payload)
                else:
                    if (isinstance(event.get("event_cursor"), int)
                            and event["event_cursor"] <= replay_high):
                        continue
                    self._sse_write(event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            studio.bus.unsubscribe(q)

    def _compute_doc_revs(self) -> dict[str, str]:
        """Snapshot of every concept's current content-hash rev (P1-6).

        The bundle is loaded under ``_state_lock`` so the snapshot is
        consistent with the latest swap. Used in the SSE ``ready`` and
        ``resync`` frames so the client can detect exactly which docs
        changed during a reconnect.
        """
        bundle, _, _, _ = self._state_snapshot()
        # The loaded bundle is only a cache.  Reload one current canonical
        # inventory so post-start concepts participate in ready membership.
        bundle = Bundle.load(bundle.root)
        out: dict[str, str] = {}
        for cid in bundle.concepts:
            source = _canonical_annotation_source(bundle, cid)
            out[concept_id_to_str(cid)] = concept_content_rev(source)
        return out

    def _sse_write(self, event: dict[str, Any] | None, *, comment: str = "") -> None:
        """Write one SSE frame and flush.

        ``comment`` is the SSE comment line (``: <c>``) — HTTP-level
        keep-alive that proxies/intermediaries see as stream progress.
        ``event`` is a real SSE event frame (``event: <type>\\ndata:
        <json>``) the client's EventSource dispatches.

        Bundle C fix (INTENT-004 / §7.3): when BOTH ``event`` and
        ``comment`` are provided, emit BOTH frames (comment first so a
        buffering proxy sees traffic immediately, then the dispatchable
        event so the client's app-level watchdog can count it). The
        previous implementation dropped the event when ``comment`` was
        set, so the heartbeat's ``type=ping`` event was lost in transit
        and the client's ``markSseReceived`` was never called for
        heartbeats — false-positive starvation → spurious polling.
        """
        try:
            if comment:
                self.wfile.write(f": {comment}\n\n".encode("utf-8"))
            if event is not None:
                etype = str(event.get("type", "message"))
                payload = json.dumps(event, default=str)
                cursor = event.get("event_cursor")
                id_line = f"id: {cursor}\n" if isinstance(cursor, int) and cursor >= 0 else ""
                self.wfile.write(f"{id_line}event: {etype}\ndata: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise

    def _handle_data_doc(self, query: dict[str, list[str]]) -> None:
        """One concept's rendered+raw+meta JSON for in-place re-render (§6)."""
        from .render import build_annotation_projection, render_concept_body_with_projection
        from .viewer.metadata import render_metadata

        cid_str = (query.get("id", [""])[0] or "").strip()
        if not cid_str:
            return self._send_json(400, {"error": "id required"})
        try:
            cid = concept_id_from_str(cid_str)
        except ConceptIdError:
            return self._send_json(404, {"error": f"bad concept id: {cid_str}"})
        bundle, _, _, _ = self._state_snapshot()
        # Identity classification precedes canonical I/O.  A syntactically
        # valid id absent from the loaded bundle is an unknown resource (404),
        # not an unavailable known resource (503).  Conversely, once identity
        # is established here, a later missing/unreadable/replaced canonical
        # file is a post-load availability failure and remains 503.
        concept = bundle.concepts.get(cid)
        annotation_source: str | None = None
        if concept is None:
            # A concept may be created after the server snapshot.  Probe it
            # through the contained/no-follow canonical reader, then reload its
            # model.  Failure in this *unknown identity* branch is still 404;
            # 503 is reserved for identities already known by the snapshot.
            try:
                annotation_source = _canonical_annotation_source(bundle, cid)
                current_bundle = Bundle.load(bundle.root)
                concept = current_bundle.concepts.get(cid)
            except (OSError, UnicodeDecodeError, OKFParseError):
                concept = None
            if concept is None:
                return self._send_json(404, {"error": f"unknown concept: {cid_str}"})
            bundle = current_bundle
        try:
            if annotation_source is None:
                annotation_source = _canonical_annotation_source(bundle, cid)
            # Validate before adopting: browser projection must never silently
            # reinterpret a transient malformed external write as body text.
            parse_document(annotation_source)
            snapshot_concept = concept._private_copy()
            snapshot_concept._adopt_raw_text(annotation_source)
            snapshot_bundle = Bundle(
                bundle.root, name=bundle.name,
                concepts={**bundle.concepts, cid: snapshot_concept},
                indexes=dict(bundle.indexes), logs=dict(bundle.logs),
                okf_version=bundle.okf_version,
                okf_extensions=list(bundle.okf_extensions),
                warnings=list(bundle.warnings),
            )
        except (OSError, UnicodeDecodeError, OKFParseError):
            return self._send_json(503, {"ok": False, "error": {
                "code": "unavailable", "message": "The canonical concept is unavailable.",
                "fields": {},
            }})
        graph = snapshot_bundle.graph()
        # Render the concept body HTML the same way the concept page does, so a
        # live `changed` patch swaps in identical markup (§7.3 no-refresh).
        # Both initial page and /__data/doc share the body_plugin seam so
        # projection is always built from post-plugin HTML (ReviewAll r2).
        body_html, annotation_projection = render_concept_body_with_projection(
            snapshot_concept, snapshot_bundle, mode="serve",
            body_plugin=self._plugin().on_concept_render,
        )
        backlinks = sorted({
            concept_id_to_str(link.source)
            for link in graph.in_edges.get(cid, []) if link.source is not None
        })
        outgoing = sorted({
            concept_id_to_str(link.target)
            for link in graph.out_edges.get(cid, []) if link.target is not None
        })
        self._send_json(200, {
            "id": cid_str,
            "rev": rev_of(annotation_source),
            "concept_content_rev": concept_content_rev(annotation_source),
            "annotation_source": annotation_source,
            "annotation_projection": annotation_projection,
            "title": snapshot_concept.title,
            "type": snapshot_concept.type,
            "html": body_html,
            "raw": snapshot_concept.body,
            "frontmatter": snapshot_concept.frontmatter,
            # Renderer adapter only: the same canonical snapshot and escaping
            # boundary as initial HTML, outside annotation projection text.
            "metadata": snapshot_concept.metadata,
            "metadata_html": render_metadata(snapshot_concept, snapshot_bundle),
            "headings": [{"level": h.level, "text": h.text} for h in snapshot_concept.headings],
            "backlinks": backlinks,
            "outgoing": outgoing,
        })

    def _handle_data_events(self, query: dict[str, list[str]]) -> None:
        """Filtered read of the events feed for the change list (§10.5/§12.2).

        Defaults to ``order=desc`` (newest-first) so a busy session's recent
        activity is surfaced in the change list. Callers can pass
        ``?order=asc&since=<id>`` explicitly for the older replay use case.
        """
        studio = self.studio
        if studio is None:
            return self._send_json(200, {"events": []})
        known = {"since", "limit", "actor", "concept", "order"}
        if any(key not in known or len(values) != 1 for key, values in query.items()):
            return self._send_json(400, {"ok": False, "error": "invalid_request"})
        since = (query.get("since", [""])[0] or "").strip() or None
        if since is not None:
            try:
                cursor = int(since)
                if cursor < 0: raise ValueError
            except ValueError:
                return self._send_json(200, {"events": [], "resync": True,
                                              "reason": "invalid_cursor", "rev": studio.current_rev(),
                                              "doc_revs": self._compute_doc_revs()})
            snapshot = [row for row in studio.read_event_snapshot()
                        if isinstance(row.get("event_cursor"), int)]
            if not snapshot and cursor > 0:
                return self._send_json(200, {"events": [], "resync": True,
                                              "reason": "history_empty", "rev": studio.current_rev(),
                                              "doc_revs": self._compute_doc_revs()})
            if snapshot and cursor > snapshot[-1]["event_cursor"]:
                return self._send_json(200, {"events": [], "resync": True,
                                              "reason": "future_cursor", "rev": studio.current_rev(),
                                              "doc_revs": self._compute_doc_revs()})
            positions = {row["event_cursor"] for row in snapshot}
            if snapshot and cursor != 0 and cursor not in positions:
                reason = "cursor_expired" if cursor < snapshot[0]["event_cursor"] else "cursor_gap"
                return self._send_json(200, {"events": [], "resync": True,
                                              "reason": reason, "rev": studio.current_rev(),
                                              "doc_revs": self._compute_doc_revs()})
            pending = [row for row in snapshot if row["event_cursor"] > cursor]
            if len(pending) > 10_000:
                return self._send_json(200, {"events": [], "resync": True,
                                              "reason": "replay_too_large",
                                              "rev": studio.current_rev(),
                                              "doc_revs": self._compute_doc_revs()})
        try:
            limit = int(query.get("limit", ["200"])[0] or 200)
        except (ValueError, TypeError):
            # Legacy consumer contract: malformed limits fall back to the
            # default page size. Cursor validation above remains strict.
            limit = 200
        if isinstance(limit, bool) or not 1 <= limit <= 1000:
            return self._send_json(400, {"ok": False, "error": "invalid_request"})
        actor = (query.get("actor", [""])[0] or "").strip() or None
        concept = (query.get("concept", [""])[0] or "").strip() or None
        # Default to newest-first; ``?order=asc`` opts into oldest-first
        # (used by okf watch --since replay).
        order = (query.get("order", ["desc"])[0] or "desc").strip().lower()
        if order not in ("asc", "desc"):
            return self._send_json(400, {"ok": False, "error": "invalid_request"})
        # A cursor denotes the first unplayed position.  Replay is necessarily
        # cursor-relative and oldest-first; allowing desc here would return the
        # newest page and silently skip the middle of a retained interval.
        if since is not None:
            order = "asc"
        events = studio.read_events(
            since=since, limit=limit, actor=actor, concept=concept, order=order,
        )
        self._send_json(200, {"events": events, "rev": studio.current_rev()})

    def _handle_comments(self, query: dict[str, list[str]]) -> None:
        """Canonical comment state from directives.jsonl (§9.1, P1-6 QUA3-002).

        Returns the latest record per comment id (last-write-wins), optionally
        filtered by ``state`` or ``concept``. This is the authoritative source
        for the comment panel — the client should NOT reconstruct state from
        the events feed (which had a desc-order + last-iteration inversion bug
        that showed claimed/resolved comments as ``open`` after reload).
        """
        studio = self.studio
        if studio is None:
            return self._send_json(200, {"comments": []})
        state = (query.get("state", [""])[0] or "").strip() or None
        concept = (query.get("concept", [""])[0] or "").strip() or None
        from .documents import document_path
        document = None
        if 'document' in query:
            try:
                if len(query['document']) != 1 or 'concept' in query:
                    raise ValueError('ambiguous target')
                document = document_path(query['document'][0])
            except (TypeError, ValueError):
                return self._send_json(400, {"error": "invalid_request"})
        try:
            comments = studio.list_comments(state=state, concept=concept,
                                             **({'document': document} if document else {}))
        except (OSError, ValueError):
            return self._send_json(503, {"ok": False, "error": {
                "code": "unavailable", "message": "Comment state is unavailable.", "fields": {},
            }})
        # Resolution is a revision-qualified projection, never persisted.
        for row in comments:
            if 'document' in row and row.get('anchor') in ({}, {'kind': 'page'}):
                # A stable page target remains readable after deletion, just like
                # a queued request. It carries no text-resolution assertion.
                continue
            cid = row.get("concept")
            try:
                source = _canonical_annotation_source(self.bundle, concept_id_from_str(cid))
            except (ConceptIdError, TypeError, OSError, UnicodeDecodeError):
                return self._send_json(503, {"ok": False, "error": {
                    "code": "unavailable", "message": "The canonical concept is unavailable.",
                    "fields": {},
                }})
            row["anchor_resolution"] = resolve_anchor(row.get("anchor"), source)
        self._send_json(200, {"comments": comments})

    def _handle_static(self, name: str) -> None:
        # Exact built-in font allowlist before normalization or overrides:
        # neither bundle content nor a user path can select vendor files.
        from .viewer.vendor import LEDGER_FACES, load_ledger_files
        if name in {face[2] for face in LEDGER_FACES}:
            return self._send_bytes(200, load_ledger_files()[name], content_type="font/woff2")
        # Prevent path traversal. Normalize, then reject anything still
        # containing a separator or a traversal token after normalization.
        # ``unquote`` already ran in ``_route`` (we parse the decoded path),
        # so this catches both ``..`` and the post-decode form of ``%2e%2e``.
        # NUL bytes are rejected outright (they break C-string path handling
        # downstream and have no legitimate use in an asset name).
        if "\x00" in name:
            return self._send_text(404, "Not found", content_type="text/plain")
        name = posixpath.normpath("/" + name).lstrip("/")
        if "/" in name or ".." in name or not name:
            return self._send_text(404, "Not found", content_type="text/plain")
        # Bundle override first — gated on the effective active-code gate
        # (security: overrides can carry arbitrary JS/HTML, same trust
        # boundary as plugins/templates). The operator must consent via
        # OKF_LOOM_ALLOW_ACTIVE_CODE / --allow-active-code; the bundle alone
        # cannot enable its own overrides (P1-40).
        from .viewer.assets import _overrides_allowed
        if _overrides_allowed(self.bundle):
            override = self.bundle.root / ".okf-loom" / "viewer" / "static" / name
            if override.is_file():
                # P2-54 defense in depth: refuse to serve an override file
                # whose resolved path escapes the bundle root (e.g. a
                # symlinked static asset pointing at /etc/passwd). Concepts
                # are protected at the model layer; override files are not,
                # so we check here.
                if not _path_within(override, self.bundle.root):
                    return self._send_text(
                        404, "Not found", content_type="text/plain"
                    )
                body = override.read_bytes()
                ctype = _content_type_for(name)
                return self._send_bytes(200, body, content_type=ctype)
        if name in list_served_static():
            body = load_static(name, self.bundle).encode("utf-8")
            ctype = _content_type_for(name)
            return self._send_bytes(200, body, content_type=ctype)
        return self._send_text(404, f"Unknown static asset: {name}", content_type="text/plain")

    def _handle_bundle_asset(self, rel: str) -> None:
        """Serve a bundle-local media file (screenshots, diagrams, video, PDF).

        Only ``BUNDLE_MEDIA_EXTENSIONS`` paths route here, and a file is
        served only when ALL of these hold:

        * the path is clean relative segments — no ``..``/``.``/NUL/backslash
          (rejected lexically; never resolved);
        * the §5 scan would reach it (:func:`okf_loom.ignore.is_path_visible`
          — so ``.okf-loom`` session state, ``.git``, ``node_modules`` and
          gitignored paths stay unreachable, and ``bundle.include`` revives
          work the same as for concepts);
        * its resolved location stays inside the bundle root (``_path_within``
          symlink defence, P2-54);
        * it fits the response size cap (``MAX_RAW_RESPONSE_BYTES`` backstop).

        Responses carry the sandboxed ``_BUNDLE_ASSET_CSP`` (bundle media is
        user content — see the constant) and a weak mtime+size ETag so
        image-heavy pages revalidate with 304s instead of re-downloading
        every screenshot on each visit (Cache-Control stays no-cache).

        A path that names no existing file falls through to concept routing
        (``shot.png.md`` is a legal concept file rendering at ``/shot.png``);
        an existing file that fails visibility/containment is refused with
        404, never fallen through.
        """
        rel = rel.lstrip("/")
        segments = rel.split("/")
        if any(s in ("", ".", "..") or "\x00" in s or "\\" in s for s in segments):
            return self._send_text(
                404, "Not found", content_type="text/plain; charset=utf-8"
            )
        bundle = self.bundle
        target = bundle.root / rel
        try:
            if not target.is_file():
                raise OSError(rel)
            st = target.stat()
        except OSError:
            # No such FILE → fall through to concept routing: a concept id
            # may legally contain a media-looking suffix (``shot.png.md``
            # renders at ``/shot.png``). When both exist, the file wins —
            # an <img src> needs bytes, not an HTML page.
            return self._handle_concept(rel)
        if not _path_within(target, bundle.root):
            return self._send_text(
                404, "Not found", content_type="text/plain; charset=utf-8"
            )
        # Same exclusion semantics as Bundle.load / the watcher (spec §5):
        # defaults + .gitignore + bundle.exclude, include add-backs on top.
        # Config is re-read per request (cheap small file) so a lock-down
        # edit takes effect without a restart, mirroring the watcher.
        from .config import OkfConfig, OkfConfigError
        try:
            bundle_cfg = OkfConfig.load(bundle.root).bundle
        except OkfConfigError:
            from .config import BundleConfig
            bundle_cfg = BundleConfig()
        from .ignore import is_path_visible
        if not is_path_visible(
            bundle.root, rel,
            exclude=bundle_cfg.exclude,
            include=bundle_cfg.include,
            respect_gitignore=bundle_cfg.respect_gitignore,
        ):
            return self._send_text(
                404, "Not found", content_type="text/plain; charset=utf-8"
            )
        if st.st_size > MAX_RAW_RESPONSE_BYTES:
            return self._send_text(
                413,
                f"Request entity too large: bundle file exceeds "
                f"{MAX_RAW_RESPONSE_BYTES} bytes.",
                content_type="text/plain; charset=utf-8",
            )
        ctype = _content_type_for(rel)
        etag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}"'
        if self.headers.get("If-None-Match") == etag:
            return self._send_bytes(
                304, b"", content_type=ctype,
                csp=_BUNDLE_ASSET_CSP, extra_headers={"ETag": etag},
            )
        try:
            body = target.read_bytes()
        except OSError:
            return self._send_text(
                404, f"No such bundle file: {rel}",
                content_type="text/plain; charset=utf-8",
            )
        self._send_bytes(
            200, body, content_type=ctype,
            csp=_BUNDLE_ASSET_CSP, extra_headers={"ETag": etag},
        )

    # --- Response writers -------------------------------------------------
    def _studio_bootstrap(self) -> str:
        """Inline ``<head>`` injection for every served HTML page.

        theme.js (the SOLE owner of viewer appearance state) is ALWAYS
        injected here as a classic (parser-blocking, no defer) script before
        ``</head>`` so it resolves ``data-theme`` BEFORE first paint (no FOUC
        for a stored non-default preference) on every serve path — including a
        plain read-only server with no studio attached. When the studio is
        attached, the CSP-safe bootstrap JSON is injected FIRST so theme.js
        can read ``studio.theme`` (precedence: stored user pref > studio.theme
        > viewer default) during its synchronous init; the studio stylesheet
        and deferred live/studio modules follow theme.js.
        """
        studio = self.studio
        bits: list[str] = []
        if studio is not None:
            public_request = self._is_public_tunnel_request()
            public_read_only = self._public_tunnel_request_is_read_only()
            effective_edit = bool(getattr(self.server, "studio_edit", True))
            effective_comment = effective_edit and not public_read_only
            if public_request:
                effective_edit = bool(effective_comment and getattr(self.server, 'public_tunnel_edit', False))
                token = ((getattr(self.server, "public_mutation_token", None) if effective_edit
                          else getattr(self.server, 'public_comment_token', None)) or '') if effective_comment else ''
            else:
                token = (self.csrf_token or "") if effective_edit else ""
            cfg = {
                "token": token,
                "live": getattr(self.server, "studio_live", True),
                "edit": effective_edit,
                "comment": effective_comment,
                "document": getattr(self, '_page_document', None),
                "presence": not public_request,
                # Presentation-only, non-secret request authority. This is
                # derived from listener identity and effective capability; it
                # never widens permission or reveals either mutation token.
                "authority_mode": (
                    "public-edit" if public_request and effective_edit else
                    "public-comment" if public_request and effective_comment else
                    "public-read" if public_request else
                    "local-edit" if effective_edit else "local-read"
                ),
                # Non-secret capability discovery only. The private proposal
                # mutation token remains server-side and is never embedded.
                "proposal_review": __import__(
                    "okf_loom.proposal_review.activation", fromlist=["capability"]
                ).capability(getattr(self.server, "proposal_service", None) is not None),
                "rev": studio.current_rev(),
                "theme": getattr(self.server, "studio_theme", "bone"),
            }
            payload = json.dumps(cfg, default=str)
            # CSP-safe bootstrap: a non-executable JSON data block. The
            # served CSP is ``script-src 'self' ...`` with NO 'unsafe-inline',
            # so an inline executable <script> is blocked (and the binding
            # studio contract says "no inline <script> bodies"). A
            # ``<script type="application/json">`` data block is NOT executed
            # by the browser, so ``script-src`` does not govern it; the studio
            # modules (live.js/studio.js) read it on boot and expose it as
            # ``window.__OKF_LOOM_STUDIO__`` for parity with the documented API.
            bits.append(
                f'<script type="application/json" id="okf-studio-bootstrap">{payload}</script>'
            )
            bits.append('<link rel="stylesheet" href="/__static/studio.css">')
        # theme.js runs before first paint; reads the bootstrap above (when
        # present) for studio.theme precedence. Self-hosted under /__static;
        # no inline handlers. (On static/SPA builds render.py emits this from
        # the template's __THEME_HEAD_JS__ instead.)
        bits.append('<script src="/__static/theme.js"></script>')
        if studio is not None:
            bits.append('<script type="module" src="/__static/live.js"></script>')
            bits.append('<script type="module" src="/__static/studio.js"></script>')
        return "\n".join(bits)

    def _send_text(self, code: int, body: str, *, content_type: str = "text/html; charset=utf-8",
                   close: bool = False, extra_headers: dict[str, str] | None = None) -> None:
        # Inject theme.js (+ studio bootstrap when attached) into <head> of
        # every served HTML page so data-theme resolves before first paint.
        if content_type.startswith("text/html") and "</head>" in body:
            body = body.replace("</head>", self._studio_bootstrap() + "\n</head>", 1)
        body_bytes = body.encode("utf-8")
        if content_type.startswith("text/html"):
            from .retrieval import MAX_RESPONSE_BYTES
            if len(body_bytes) > MAX_RESPONSE_BYTES:
                code = 413
                body_bytes = (
                    b"<!doctype html><html><head><title>Response too large</title></head>"
                    b"<body><h1>Response too large</h1><p>response_too_large</p></body></html>"
                )
        self._send_bytes(code, body_bytes, content_type=content_type, close=close,
                          extra_headers=extra_headers)

    def _send_bytes(self, code: int, body: bytes, *, content_type: str,
                    close: bool = False, csp: str | None = None,
                    extra_headers: dict[str, str] | None = None) -> None:
        if code == 403 and self._is_public_tunnel_request() and self.studio is not None:
            try:
                path = urlparse(self.path).path
            except ValueError:
                path = ''
            route = path if path in _STUDIO_POST_ROUTES or path in {'/__diff', '/__presence'} else 'other'
            try:
                self.studio.audit_public_denial(route=route, method=self.command if self.command in {'GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD'} else 'other')
            except (OSError, ValueError):
                # Denial stays closed even when private audit storage is unavailable.
                print('okf public denial audit unavailable', file=sys.stderr)
        if close:
            # Signal both the server loop (stop reading after this response)
            # and the client (reconnect cleanly) that this connection closes.
            # Used by the 413 oversized-body path where draining an unbounded
            # body is worse than a clean reconnect (F4).
            self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
        if not extra_headers or "Cache-Control" not in extra_headers:
            self.send_header("Cache-Control", "no-cache")
        # Security response headers (defence in depth). The default CSP
        # restricts script sources to 'self' and the pinned CDN; everything
        # else (inline event handlers, remote img/style, etc.) is blocked or
        # restricted. ``csp`` overrides it per response — used by the bundle
        # media handler to sandbox user content. Adjust via bundle overrides
        # if needed.
        #
        # ``style-src`` and ``connect-src`` also allow
        # https://cdn.jsdelivr.net because highlight.js loads its theme
        # stylesheet and source map from the CDN. ``img-src`` allows
        # ``data:`` (inline SVG icons) plus the CDN for any image asset
        # a bundle might embed from jsdelivr. ``font-src`` allows the CDN
        # because KaTeX's stylesheet references its webfonts relative to
        # the CDN. Keep this list in sync with the per-page
        # <meta> CSPs in viewer/templates/ — browsers enforce the
        # INTERSECTION of header + meta, so a directive missing here or
        # there silently wins.
        self.send_header(
            "Content-Security-Policy",
            csp or (
                "default-src 'self'; "
                "script-src 'self' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "img-src 'self' data: https://cdn.jsdelivr.net; "
                "connect-src 'self' https://cdn.jsdelivr.net; "
                "font-src 'self' data: https://cdn.jsdelivr.net; "
                "object-src 'none'; "
                "base-uri 'self'; "
                "form-action 'self'"
            ),
        )
        if extra_headers:
            for header_name, header_value in extra_headers.items():
                self.send_header(header_name, header_value)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-XSS-Protection", "0")  # disabled in favour of CSP
        self.end_headers()
        try:
            if not getattr(self, "_head_only", False):
                self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _send_json(self, code: int, obj: Any,
                   extra_headers: dict[str, str] | None = None,
                   close: bool = False) -> None:
        from .retrieval import MAX_RESPONSE_BYTES, canonical_json_bytes
        try:
            body = canonical_json_bytes(obj)
        except Exception:
            code = 500
            body = canonical_json_bytes({"ok": False, "error": {
                "code": "invalid_json_value",
                "message": "Response contains a non-canonical JSON value", "fields": {},
            }})
        if len(body) > MAX_RESPONSE_BYTES:
            code = 413
            body = canonical_json_bytes({"ok": False, "error": {
                "code": "response_too_large", "message": "Response exceeds the size limit",
                "fields": {},
            }})
        self._send_bytes(code, body, content_type="application/json; charset=utf-8",
                         extra_headers=extra_headers, close=close)

    def _send_contract_error(self, status: int, code: str, message: str,
                             fields: dict[str, str] | None = None,
                             close: bool = False) -> None:
        self._send_json(status, {"ok": False, "error": {
            "code": code, "message": message, "fields": fields or {},
        }}, close=close)

    # --- Logging (quieter than default) ----------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        # Suppress default noisy stderr access log; keep error log.
        # Guard: send_error passes HTTPStatus enum as args[0], not str.
        if args and isinstance(args[0], str) and args[0].startswith(("GET ", "POST ")):
            return
        super().log_message(format, *args)


# Extension → Content-Type. Every response also carries
# ``X-Content-Type-Options: nosniff``, so a type missing here does not
# degrade to browser sniffing — it ships as ``application/octet-stream``
# (download, never render). That makes this table the effective allowlist
# of what the viewer can DISPLAY, for built-in static, override-dir, and
# bundle media files alike.
_EXT_CONTENT_TYPES: dict[str, str] = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".pdf": "application/pdf",
}


def _content_type_for(name: str) -> str:
    ext = posixpath.splitext(name.lower())[1]
    return _EXT_CONTENT_TYPES.get(ext, "application/octet-stream")


def _path_within(candidate: Path, root: Path) -> bool:
    """True iff ``candidate.resolve()`` is ``root`` or lives beneath it.

    Symlink defense (P2-54): used for static-override files, bundle media,
    and (via the handler) for reserved index/log files so a symlinked
    ``index.md`` / ``log.md`` / override asset pointing outside the bundle
    root is refused at serve time even if :meth:`Bundle.load` followed the
    link at load time. Canonical implementation lives in
    :func:`okf_loom.ignore.path_within` (shared with the static build's
    media copy).
    """
    from .ignore import path_within
    return path_within(candidate, root)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Quick tunnel: one-command public sharing via cloudflared
# ---------------------------------------------------------------------------
# ``okf serve <bundle> --tunnel`` starts a Cloudflare quick tunnel next to
# the loopback server and prints the public URL. cloudflared targets a second,
# generation-bound loopback listener, never the local/admin listener. The
# server itself STAYS on loopback; cloudflared is the only network-facing
# process and request headers never classify ingress authority.

_TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class TunnelStartOwnershipError(RuntimeError):
    """Tunnel startup failed while the spawned process remains server-owned."""

    def __init__(self, message: str, process: Any) -> None:
        super().__init__(message)
        self.process = process


class _OwnedThreadingHTTPServer(ThreadingHTTPServer):
    """Own accepted sockets and atomically fence terminal request dispatch.

    Requests admitted before the fence complete normally. Idle keep-alive
    connections close at the fence; active connections close after their
    admitted response flushes, before another request can dispatch.
    """

    daemon_threads = False
    block_on_close = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._http_owner_lock = threading.Lock()
        self._http_terminal = False
        self._http_connections: set[socket.socket] = set()
        self._http_active: set[socket.socket] = set()
        super().__init__(*args, **kwargs)

    @staticmethod
    def _shutdown_socket(connection: socket.socket) -> None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    def _register_accepted_connection(
        self, connection: socket.socket, address: Any,
    ) -> tuple[socket.socket, Any]:
        with self._http_owner_lock:
            rejected = self._http_terminal
            if not rejected:
                self._http_connections.add(connection)
        if rejected:
            self._shutdown_socket(connection)
            raise OSError("HTTP server is shutting down")
        return connection, address

    def get_request(self) -> tuple[socket.socket, Any]:
        connection, address = super().get_request()
        return self._register_accepted_connection(connection, address)

    def admit_http_dispatch(self, connection: socket.socket) -> bool:
        with self._http_owner_lock:
            if self._http_terminal or connection not in self._http_connections:
                return False
            self._http_active.add(connection)
            return True

    def release_http_dispatch(self, connection: socket.socket) -> bool:
        with self._http_owner_lock:
            self._http_active.discard(connection)
            terminal = self._http_terminal
        if terminal:
            self._shutdown_socket(connection)
        return terminal

    def begin_terminal_http_shutdown(self) -> bool:
        """Atomically fence admission and close all currently idle sockets."""
        with self._http_owner_lock:
            first = not self._http_terminal
            self._http_terminal = True
            idle = tuple(self._http_connections - self._http_active)
        for connection in idle:
            self._shutdown_socket(connection)
        return first

    def begin_http_drain(self) -> bool:
        """Fence dispatch while retaining idle sockets for a terminal reply."""
        with self._http_owner_lock:
            first = not self._http_terminal
            self._http_terminal = True
        return first

    def shutdown_request(self, request: socket.socket) -> None:
        with self._http_owner_lock:
            self._http_active.discard(request)
            self._http_connections.discard(request)
        super().shutdown_request(request)

    def server_close(self) -> None:
        self.begin_terminal_http_shutdown()
        # Fence/cancel retrieval before ThreadingMixIn joins request handlers:
        # those handlers can themselves be waiting on admitted retrieval work.
        if not getattr(self, "_okf_public_ingress", False):
            _shutdown_retrieval_runtime(self)
        try:
            super().server_close()
        finally:
            # Idempotent fallback if the underlying close raises. Ingress
            # detach never touches the primary server's shared retrieval work.
            if not getattr(self, "_okf_public_ingress", False):
                _shutdown_retrieval_runtime(self)


class _PublicIngressHTTPServer(_OwnedThreadingHTTPServer):
    """Dedicated loopback listener whose socket identity is public authority.

    cloudflared targets this listener, never the local/admin listener. Missing
    application attributes are read from the primary server so both listeners
    share one Studio and bundle state while retaining distinct socket identity.
    """

    def __init__(self, app_server: Any, generation: str) -> None:
        self.app_server = app_server
        self._okf_public_ingress = True
        self._okf_public_ingress_generation = generation
        super().__init__(("127.0.0.1", 0), OKFWikiHandler)

    def __getattr__(self, name: str) -> Any:
        app = self.__dict__.get("app_server")
        if app is None:
            raise AttributeError(name)
        return getattr(app, name)


def _start_public_ingress(server: Any, *, allow_public_edit: bool,
                          allow_public_comment: bool = False) -> Any:
    """Create one generation-bound public listener and mutation capability."""
    if type(allow_public_comment) is not bool or type(allow_public_edit) is not bool or (allow_public_comment and allow_public_edit):
        raise ValueError('invalid public capability')
    if (allow_public_comment or allow_public_edit) and not getattr(server, 'studio_edit', True):
        raise ValueError('read-only studio cannot enable public mutation')
    _ensure_public_mutation_lifecycle(server)
    if getattr(server, "tunnel_terminal_closing", False):
        raise RuntimeError("public ingress lifecycle is terminal-closing")
    if getattr(server, "public_ingress_draining_generation", None) is not None:
        raise RuntimeError("public ingress is still draining")
    if getattr(server, "public_ingress_server", None) is not None:
        raise RuntimeError("public ingress is already active")
    generation = secrets.token_urlsafe(32)
    ingress = _PublicIngressHTTPServer(server, generation)
    thread = threading.Thread(
        target=ingress.serve_forever,
        daemon=True,
        name="okf-public-ingress",
    )
    server.public_ingress_generation = generation
    server.public_mutation_token = (
        secrets.token_urlsafe(32) if allow_public_edit else None
    )
    server.public_tunnel_edit = bool(allow_public_edit)
    server.public_tunnel_comment = allow_public_comment
    server.public_comment_token = secrets.token_urlsafe(32) if allow_public_comment else None
    server.public_ingress_server = ingress
    server.public_ingress_thread = thread
    server.public_ingress_accepting = True
    server.public_ingress_accepting_generation = generation
    server.public_mutation_inflight.setdefault(generation, 0)
    try:
        thread.start()
    except BaseException:
        server.public_ingress_accepting = False
        server.public_ingress_accepting_generation = None
        server.public_ingress_generation = None
        server.public_mutation_token = None
        server.public_comment_token = None
        server.public_tunnel_comment = False
        server.public_tunnel_edit = False
        server.public_ingress_server = None
        server.public_ingress_thread = None
        ingress.server_close()
        raise
    return ingress


def _stop_public_ingress(server: Any) -> None:
    """Stop admission/listener, drain leases, then clear capability state."""
    condition = _ensure_public_mutation_lifecycle(server)
    ingress = getattr(server, "public_ingress_server", None)
    thread = getattr(server, "public_ingress_thread", None)
    generation = getattr(server, "public_ingress_generation", None)
    with condition:
        server.public_ingress_accepting = False
        server.public_ingress_accepting_generation = None
    _close_public_ingress_listener(ingress, thread)
    with condition:
        while generation and server.public_mutation_inflight.get(generation, 0):
            condition.wait()
        if getattr(server, "public_ingress_generation", None) == generation:
            server.public_ingress_generation = None
            server.public_mutation_token = None
            server.public_comment_token = None
            server.public_tunnel_comment = False
            server.public_tunnel_edit = False
            server.public_ingress_server = None
            server.public_ingress_thread = None
            if generation:
                server.public_mutation_inflight.pop(generation, None)
        condition.notify_all()


def _close_public_ingress_listener(ingress: Any, thread: Any) -> None:
    """Close public admission sockets without mutating generation authority."""
    if ingress is not None:
        # Tunnel detach is a generation drain rather than process shutdown.
        # Retain accepted keep-alive sockets long enough for one useful
        # terminal 503; terminal run_server shutdown closes them immediately.
        fence = getattr(ingress, "begin_http_drain", None)
        if callable(fence):
            fence()
        ingress.shutdown()
        ingress.server_close()
    if thread is not None and thread is not threading.current_thread():
        thread.join()


def _ensure_public_mutation_lifecycle(server: Any) -> threading.Condition:
    """Install the generation lease state on embedding/test servers lazily."""
    lock = getattr(server, "_tunnel_lock", None)
    if lock is None:
        lock = threading.RLock()
        server._tunnel_lock = lock
    condition = getattr(server, "_public_mutation_condition", None)
    if condition is None:
        condition = threading.Condition(lock)
        server._public_mutation_condition = condition
    if not hasattr(server, "public_mutation_inflight"):
        server.public_mutation_inflight = {}
    if not hasattr(server, "public_ingress_accepting"):
        server.public_ingress_accepting = False
    if not hasattr(server, "public_ingress_accepting_generation"):
        server.public_ingress_accepting_generation = None
    if not hasattr(server, "public_ingress_draining_generation"):
        server.public_ingress_draining_generation = None
    if not hasattr(server, "tunnel_settlement_pending"):
        server.tunnel_settlement_pending = False
    if not hasattr(server, "tunnel_settlement_in_progress"):
        server.tunnel_settlement_in_progress = False
    if not hasattr(server, "tunnel_settlement_error"):
        server.tunnel_settlement_error = None
    if not hasattr(server, "tunnel_pending_activity_summary"):
        server.tunnel_pending_activity_summary = None
    if not hasattr(server, "tunnel_terminal_closing"):
        server.tunnel_terminal_closing = False
    if not hasattr(server, "_server_state_publication_lock"):
        # Publication order, rather than filesystem completion order, fences
        # snapshots.  Keep this separate from the lifecycle condition so no
        # filesystem or Studio lock is ever acquired while lifecycle-owned.
        server._server_state_publication_lock = threading.Lock()
    return condition


_TUNNEL_OWNER_UNSET = object()


def _settle_tunnel_process(proc: Any, *, timeout_s: float = 1.0) -> bool:
    """Boundedly settle an owner, returning whether real-Popen exit was proven."""
    if proc is None:
        return True
    import subprocess

    terminate = getattr(proc, "terminate", None)
    if callable(terminate):
        try:
            terminate()
        except Exception:
            pass
    if not isinstance(proc, subprocess.Popen):
        return True

    wait = getattr(proc, "wait", None)
    if not callable(wait):
        return False
    try:
        wait(timeout=timeout_s)
        return True
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        # A failed wait cannot establish that the owned process is gone.
        pass

    kill = getattr(proc, "kill", None)
    if callable(kill):
        try:
            kill()
        except Exception:
            pass
    try:
        wait(timeout=timeout_s)
    except Exception:
        # Settlement is bounded even when the OS process adapter is unhealthy.
        return False
    return True


# Sharing is driven through the token-guarded ``/__tunnel`` admin route (the
# ``okf-loom tunnel`` command) or by the server itself when cloudflared exits.
# Neither path can tell a person from an agent, so tunnel events are recorded
# as the local command line, never as an agent.
_TUNNEL_ACTIVITY_ACTOR = "cli"


def _emit_owned_tunnel_activity(
    studio: "Studio | None", summary: str, *, action: str = "tunnel_detach",
) -> None:
    """Best-effort change-list entry for a tunnel attach, detach or loss."""
    if studio is None:
        return
    try:
        studio.append_event({
            "type": "activity", "actor": _TUNNEL_ACTIVITY_ACTOR,
            "origin": "tunnel", "action": action, "ids": [],
            "summary": summary, "undoable": False,
        })
    except Exception:
        pass


def _clear_owned_tunnel(
    server: Any,
    studio: "Studio | None",
    *,
    expected_proc: Any = _TUNNEL_OWNER_UNSET,
    expected_generation: Any = _TUNNEL_OWNER_UNSET,
    activity_summary: str | None = None,
    wait_for_existing: bool = False,
    terminal: bool = False,
    drain_timeout_s: float | None = None,
) -> bool:
    """Drain, revoke, and settle one generation without waiting under its lock.

    Lock order is lifecycle condition (backed by ``_tunnel_lock``) before any
    state access. Listener shutdown and process settlement happen unlocked;
    ``Condition.wait`` releases the lifecycle lock so admitted handlers can
    release their leases. Only the first caller finalizes/emits for a generation.
    """
    condition = _ensure_public_mutation_lifecycle(server)
    retry_settlement = False
    drain_deadline = (
        time.monotonic() + max(0.0, drain_timeout_s)
        if drain_timeout_s is not None else None
    )

    def _terminal_wait_budget() -> float | None:
        nonlocal drain_deadline
        if not getattr(server, "tunnel_terminal_closing", False):
            return None
        if drain_deadline is None:
            timeout = float(getattr(server, "tunnel_terminal_drain_timeout_s", 2.0))
            drain_deadline = time.monotonic() + max(0.0, timeout)
        return max(0.0, drain_deadline - time.monotonic())

    while True:
        with condition:
            if terminal:
                # Terminal shutdown is a permanent lifecycle fence. Python
                # cannot cancel an admitted daemon request thread; process exit
                # remains its final abort owner after this bounded drain.
                server.tunnel_terminal_closing = True
            proc = getattr(server, "tunnel_proc", None)
            generation = getattr(server, "public_ingress_generation", None)
            fence = getattr(server, "public_ingress_draining_generation", None)
            if fence is not None:
                if (getattr(server, "tunnel_settlement_pending", False)
                        and not getattr(server, "tunnel_settlement_in_progress", False)):
                    server.tunnel_settlement_in_progress = True
                    retry_settlement = True
                    activity_summary = (
                        getattr(server, "tunnel_pending_activity_summary", None)
                        or activity_summary
                    )
                    ingress = None
                    thread = None
                    break
                if (terminal
                        and not getattr(server, "tunnel_settlement_in_progress", False)
                        and getattr(server, "tunnel_proc", None) is None
                        and getattr(server, "public_ingress_generation", None) is None):
                    # A prior owner completed settlement while terminal close
                    # retained its fence. Terminal cleanup is now the sole live
                    # owner, so finalize the marker atomically.
                    server.public_ingress_draining_generation = None
                    server.tunnel_pending_activity_summary = None
                    condition.notify_all()
                    return True
                if not wait_for_existing:
                    return False
                budget = _terminal_wait_budget()
                if budget == 0:
                    # Terminal shutdown takes over a retained fence whose
                    # previous owner can no longer make progress.  Generation
                    # and process identity checks below prevent stale cleanup
                    # from clearing a replacement owner.
                    server.tunnel_settlement_in_progress = True
                    retry_settlement = True
                    proc = getattr(server, "tunnel_proc", None)
                    generation = getattr(server, "public_ingress_generation", None)
                    ingress = getattr(server, "public_ingress_server", None)
                    thread = getattr(server, "public_ingress_thread", None)
                    break
                condition.wait(timeout=budget)
                if getattr(server, "public_ingress_draining_generation", None) is None:
                    return True
                continue
            if expected_proc is not _TUNNEL_OWNER_UNSET and proc is not expected_proc:
                return False
            if (expected_generation is not _TUNNEL_OWNER_UNSET
                    and generation != expected_generation):
                return False
            ingress = getattr(server, "public_ingress_server", None)
            thread = getattr(server, "public_ingress_thread", None)
            fence = generation or "unscoped-tunnel-settlement"
            server.public_ingress_accepting = False
            server.public_ingress_accepting_generation = None
            server.public_ingress_draining_generation = fence
            server.tunnel_settlement_in_progress = True
            server.tunnel_settlement_pending = False
            server.tunnel_settlement_error = None
            server.tunnel_pending_activity_summary = activity_summary
            # URL authority is revoked immediately; generation/token remain
            # until admitted mutations have settled.
            server.tunnel_url = None  # type: ignore[attr-defined]
            break

    if not retry_settlement:
        write_server_state(server, studio)
        _close_public_ingress_listener(ingress, thread)
        with condition:
            while generation and server.public_mutation_inflight.get(generation, 0):
                budget = _terminal_wait_budget()
                if budget == 0:
                    break
                condition.wait(timeout=budget)
            if (getattr(server, "tunnel_proc", None) is not proc
                    or getattr(server, "public_ingress_generation", None) != generation):
                server.tunnel_settlement_in_progress = False
                if not getattr(server, "tunnel_terminal_closing", False):
                    server.public_ingress_draining_generation = None
                condition.notify_all()
                return False
            server.public_ingress_generation = None
            server.public_mutation_token = None
            server.public_tunnel_edit = False
            server.public_comment_token = None
            server.public_tunnel_comment = False
            server.public_ingress_server = None
            server.public_ingress_thread = None
            if generation:
                server.public_mutation_inflight.pop(generation, None)
            base = getattr(server, "base_allowed_hosts", None) or (
                "127.0.0.1", "localhost"
            )
            server.allowed_hosts = tuple(base)  # type: ignore[attr-defined]

    settled = _settle_tunnel_process(proc)
    if not settled:
        with condition:
            server.tunnel_proc = proc  # type: ignore[attr-defined]
            server.tunnel_settlement_in_progress = False
            server.tunnel_settlement_pending = True
            server.tunnel_settlement_error = "tunnel process settlement incomplete"
            condition.notify_all()
        write_server_state(server, studio)
        return False

    with condition:
        server.tunnel_proc = None  # type: ignore[attr-defined]
        server.tunnel_settlement_in_progress = False
        server.tunnel_settlement_pending = False
        server.tunnel_settlement_error = None
    write_server_state(server, studio)
    if activity_summary:
        _emit_owned_tunnel_activity(studio, activity_summary)
    with condition:
        server.tunnel_pending_activity_summary = None
        # The terminal flag permanently fences replacement owners; the drain
        # fence represents work ownership and must always be finalized once no
        # writer remains.  Retaining it creates an ownerless terminal wait.
        server.public_ingress_draining_generation = None
        condition.notify_all()
    return True


def _tunnel_monitor_loop(
    server: Any,
    studio: "Studio | None",
    stop_event: threading.Event,
    interval_s: float,
) -> None:
    """Observe the current process while fencing every result by owner identity."""
    while not stop_event.wait(interval_s):
        condition = _ensure_public_mutation_lifecycle(server)
        with condition:
            proc = getattr(server, "tunnel_proc", None)
            generation = getattr(server, "public_ingress_generation", None)
            advertised_url = getattr(server, "tunnel_url", None)
        poll = getattr(proc, "poll", None) if proc is not None else None
        if not callable(poll):
            continue
        try:
            returncode = poll()
        except Exception:
            returncode = "poll_error"
        if returncode is None:
            continue
        _clear_owned_tunnel(
            server, studio,
            expected_proc=proc,
            expected_generation=generation,
            activity_summary=(
                f"public tunnel lost after cloudflared exited: {advertised_url}"
                if advertised_url else None
            ),
        )


def _start_tunnel_monitor(
    server: Any,
    studio: "Studio | None",
    *,
    interval_s: float = 0.25,
) -> threading.Thread:
    """Start the server-owned daemon that autonomously reconciles cloudflared."""
    current = getattr(server, "_tunnel_monitor_thread", None)
    if current is not None and current.is_alive():
        return current
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_tunnel_monitor_loop,
        args=(server, studio, stop_event, interval_s),
        daemon=True,
        name="okf-tunnel-monitor",
    )
    server._tunnel_monitor_stop = stop_event  # type: ignore[attr-defined]
    server._tunnel_monitor_thread = thread  # type: ignore[attr-defined]
    thread.start()
    return thread


def _stop_tunnel_monitor(server: Any) -> None:
    """Fence and join the Studio-owning monitor, without joining itself."""
    stop_event = getattr(server, "_tunnel_monitor_stop", None)
    thread = getattr(server, "_tunnel_monitor_thread", None)
    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join()
    if getattr(server, "_tunnel_monitor_thread", None) is thread:
        server._tunnel_monitor_thread = None  # type: ignore[attr-defined]
        server._tunnel_monitor_stop = None  # type: ignore[attr-defined]


def _shutdown_tunnel_owner(
    server: Any,
    studio: "Studio | None",
    *,
    terminal: bool = False,
    drain_timeout_s: float | None = None,
) -> None:
    """Fence observation, settle/take over drain ownership, then join monitor."""
    stop_event = getattr(server, "_tunnel_monitor_stop", None)
    if stop_event is not None:
        stop_event.set()
    _clear_owned_tunnel(
        server, studio, wait_for_existing=True, terminal=terminal,
        drain_timeout_s=drain_timeout_s,
    )
    _stop_tunnel_monitor(server)


def parse_quick_tunnel_url(line: str) -> str | None:
    """Extract a trycloudflare quick-tunnel URL from a cloudflared log line.

    Returns the full ``https://…`` URL, or ``None`` when the line carries
    no tunnel URL. Pure + deterministic (unit-tested separately from the
    subprocess plumbing).
    """
    m = _TUNNEL_URL_RE.search(line or "")
    return m.group(0) if m else None


def start_quick_tunnel(port: int, *, timeout_s: float = 45.0):
    """Start ``cloudflared tunnel --url http://127.0.0.1:<port>``.

    Returns ``(process, public_url)``. Raises ``RuntimeError`` with an
    actionable message when the cloudflared binary is missing or the
    public URL does not appear within ``timeout_s``. If bounded process
    settlement cannot be proven, :class:`TunnelStartOwnershipError` carries
    the still-owned process so the server can retain and retry settlement.
    """
    import shutil
    import subprocess

    exe = shutil.which("cloudflared")
    if not exe:
        raise RuntimeError(
            "cloudflared not found on PATH. Install it "
            "(https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) "
            "or share the server another way."
        )
    try:
        proc = subprocess.Popen(
            [exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as e:
        raise RuntimeError(f"cloudflared could not start: {e}") from e
    found: list[str] = []
    got_url = threading.Event()

    def _scan() -> None:
        try:
            for line in proc.stdout or ():
                url = parse_quick_tunnel_url(line)
                if url and not found:
                    found.append(url)
                    got_url.set()
                    # Keep draining so cloudflared never blocks on a full pipe.
        except (OSError, ValueError):
            pass
        got_url.set()  # EOF: unblock the waiter (found stays empty on failure)

    threading.Thread(target=_scan, daemon=True).start()
    if not got_url.wait(timeout_s) or not found:
        message = (
            f"cloudflared started but no tunnel URL appeared within {timeout_s:.0f}s."
        )
        if not _settle_tunnel_process(proc):
            raise TunnelStartOwnershipError(message, proc)
        raise RuntimeError(message)
    return proc, found[0]


def write_server_state(server: Any, studio: "Studio | None") -> None:
    """Persist the running server's address to ``<session>/server.json``.

    Best-effort: session-aware CLI verbs (``okf tunnel``) read this to find
    the live server's port; everything else keeps working without it.
    """
    if studio is None:
        return
    addr = getattr(server, "server_address", None)
    if not addr:
        return  # embedding stubs without a bound socket have no port to record

    condition = _ensure_public_mutation_lifecycle(server)
    publication_lock = server._server_state_publication_lock

    def _snapshot() -> dict[str, Any]:
        host, port = addr[:2]
        tunnel_url = getattr(server, "tunnel_url", None)
        return {
            "host": host,
            "port": port,
            "pid": os.getpid(),
            "started": getattr(server, "started_iso", None),
            "url": f"http://{host}:{port}/",
            "tunnel_url": tunnel_url,
            "tunnel_public_edit": bool(
                tunnel_url and getattr(server, "public_tunnel_edit", False)
            ),
            **({'tunnel_public_comment': True} if tunnel_url and getattr(server, 'public_tunnel_comment', False) else {}),
        }

    def _publish(state: dict[str, Any]) -> None:
        try:
            studio.ensure_session()
            studio.write_session_text(
                "server.json", json.dumps(state, indent=2) + "\n")
        except OSError:
            pass

    # Serialize snapshot order with atomic publication order.  The lifecycle
    # lock is held only for the snapshot, never for Studio/filesystem work: an
    # older active writer may finish before cleanup, but can never land after
    # cleanup's newer inactive snapshot.
    with publication_lock:
        with condition:
            state = _snapshot()
        _publish(state)


def run_server(
    bundle_root: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    watch: bool = True,
    open_browser: bool = True,
    name: str | None = None,
    allow_active_code: bool | None = None,
    studio_edit: bool = True,
    studio_live: bool = True,
    tunnel: bool = False,
    tunnel_public_edit: bool = False,
    tunnel_public_comment: bool = False,
    enable_proposal_review: bool = False,
) -> None:
    """Serve an OKF bundle as a live wiki. Blocks until Ctrl-C.

    Loads the bundle on startup, optionally watches ``.md`` files for changes
    (reloads in a background thread), optionally opens the default browser,
    and prints a startup banner. KeyboardInterrupt at the top of this
    function triggers a clean shutdown.

    current studio (docs-bundle/reference/spec.md): by default the full live collaborative
    studio is on — SSE push, commenting/directing, agent enrichment.
    ``studio_edit=False`` (--no-edit) makes a read-only kiosk (no comments);
    ``studio_live=False`` disables SSE push. Both read the ``studio:``
    section of ``okf-loom.config.yaml``; explicit args override config.

    Active-code gate (P1-40): ``allow_active_code`` is the operator-level
    override. ``None`` (default) defers to :func:`operator_consent` (env var
    ``OKF_LOOM_ALLOW_ACTIVE_CODE``). An explicit ``True`` / ``False`` from the
    CLI ``--allow-active-code`` / ``--no-allow-active-code`` flag (cli.py —
    REPORTED) wins. The effective gate is
    ``bundle_cfg.allow_active_code AND operator_consent``; the bundle alone
    cannot enable viewer overrides / plugins.

    Reload safety (P2-46): the server is reload-safe — a watcher reload
    mutates ``bundle`` / ``_palette`` / ``plugin`` under a lock, and every
    handler that needs more than one of those snapshots them once under the
    same lock (``_state_snapshot``).
    """
    from .proposal_review.activation import is_loopback, validate_server_activation
    if tunnel_public_edit and not tunnel:
        raise ValueError("tunnel_public_edit requires tunnel=True")
    if tunnel_public_comment and (not tunnel or tunnel_public_edit):
        raise ValueError('comment sharing requires tunnel and excludes public edit')
    validate_server_activation(enabled=enable_proposal_review, host=host,
                               public=not is_loopback(host), tunnel=tunnel,
                               no_edit=not studio_edit)
    bundle_root = Path(bundle_root).resolve()

    # Operator consent: an explicit CLI argument wins over the env var.
    if allow_active_code is not None:
        from .viewer.assets import set_operator_consent
        set_operator_consent(bool(allow_active_code))
    # effective_allow drives the warning + the plugin build.
    effective = effective_allow_active_code(bundle_root)

    bundle = Bundle.load(bundle_root)
    config = load_config(bundle)

    # --- Live studio setup -------------------------------------------------
    from .config import OkfConfig
    okf_cfg = OkfConfig.load(bundle_root)
    sc = okf_cfg.studio
    studio_edit = studio_edit and sc.edit
    studio_live = studio_live and sc.live
    if (tunnel_public_edit or tunnel_public_comment) and not studio_edit:
        raise RuntimeError(
            "public tunnel editing cannot be enabled when Studio editing is disabled"
        )
    validate_server_activation(enabled=enable_proposal_review, host=host,
                               public=not is_loopback(host), tunnel=tunnel,
                               no_edit=not studio_edit)
    studio: Studio | None = None
    if studio_live or studio_edit:
        studio = Studio.for_configured_bundle(
            bundle_root,
            bundle_name=name or config.get("name") or bundle.name,
            max_queue=64,
        )
        studio.ensure_session()
        # Seed the bundle rev so the first 'ready' event is coherent.
        studio.set_presence(actor="agent", state="idle")

    # Current spec §15 + §14: load viewer plugins at server start, gated on
    # the EFFECTIVE active-code gate (bundle cfg AND operator consent). We
    # pass the precomputed ``allow_active_code=effective`` so the plugin
    # builder does NOT re-read the bundle cfg alone (which would ignore
    # operator consent). When the gate is closed, no entry-point discovery
    # happens and the composite is a complete no-op.
    from .viewer.plugins import build_viewer_plugin
    plugin = build_viewer_plugin(bundle_root, allow_active_code=effective)

    # P1-40: prominent stderr WARNING whenever the effective gate is open.
    # Mirrors the --host warning style. Active code means viewer overrides
    # AND plugins execute attacker-controllable JS/Python; the operator
    # must understand the trust implication.
    if effective:
        print(
            f"WARNING: active code (viewer overrides + plugins) is ENABLED "
            f"for bundle {bundle_root}.\n"
            f"WARNING: Only run this for bundles whose override/plugin "
            f"sources you trust.\n"
            f"WARNING: A malicious override can execute arbitrary "
            f"JavaScript in every visitor's browser.\n"
            f"WARNING: (gate opened by OKF_LOOM_ALLOW_ACTIVE_CODE env var or "
            f"--allow-active-code)",
            file=sys.stderr,
        )

    # Warn if binding to a non-loopback address (the server has no auth,
    # no rate-limiting, and serves raw bundle bodies — exposure is a footgun).
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding to {host} — the viewer has NO authentication and\n"
            f"WARNING: will be reachable from the network. Anyone who can reach\n"
            f"WARNING: this host can read the full bundle content. Use\n"
            f"WARNING: 127.0.0.1 (the default) for local-only access.",
            file=sys.stderr,
        )

    # Preserve the long-standing test/embedding seam that replaces the module
    # ``ThreadingHTTPServer`` name with a one-shot stub. Real stdlib operation
    # always uses the connection-owning subclass.
    server_type = (
        _OwnedThreadingHTTPServer
        if ThreadingHTTPServer is _STDLIB_THREADING_HTTP_SERVER
        else ThreadingHTTPServer
    )
    server = server_type((host, port), OKFWikiHandler)
    server.bundle = bundle  # type: ignore[attr-defined]
    server.config = config  # type: ignore[attr-defined]
    server.name = name or config.get("name") or bundle.name  # type: ignore[attr-defined]
    server.plugin = plugin  # type: ignore[attr-defined]
    # P2-46: lock that serializes watcher reloads with handler snapshots.
    # Handlers detect its presence via ``_state_lock`` and snapshot bundle /
    # palette / plugin once per request under it; the reload path holds it
    # while mutating all three.
    server._state_lock = threading.Lock()  # type: ignore[attr-defined]
    # P1-37: last reload error (if any), surfaced for a transient banner.
    # Best-effort display state; set under the same lock.
    server._reload_error = None  # type: ignore[attr-defined]
    # Palette is lazily computed on first request via the handler accessor.
    # Live studio state on the server object (§6 ``server.state``).
    server.studio = studio  # type: ignore[attr-defined]
    # P1-16: allowed_hosts MUST default to loopback and never be empty —
    # the cross-origin guard fails closed against an unset/empty list.
    allowed_hosts_final = sc.allowed_hosts or ("127.0.0.1", "localhost")
    csrf_token = secrets.token_hex(16) if studio else None
    server.csrf_token = csrf_token  # type: ignore[attr-defined]
    server._presence_generation = secrets.token_urlsafe(18)  # type: ignore[attr-defined]
    # The primary application server owns the complete re-anchor quota state.
    # Dedicated ingress listeners only resolve this owner; they never cache or
    # assign mutable quota state through their read-through delegation seam.
    server._reanchor_rate_lock = threading.Lock()  # type: ignore[attr-defined]
    server._reanchor_rate_attempts = []  # type: ignore[attr-defined]
    server.allowed_hosts = allowed_hosts_final  # type: ignore[attr-defined]
    server.max_sse_clients = sc.max_sse_clients  # type: ignore[attr-defined]
    server.studio_live = studio_live  # type: ignore[attr-defined]
    server.studio_edit = studio_edit  # type: ignore[attr-defined]
    server.studio_theme = sc.theme  # type: ignore[attr-defined]
    server.proposal_public_exposure = not is_loopback(host)  # type: ignore[attr-defined]
    server.proposal_service = None  # type: ignore[attr-defined]
    server.proposal_outbox = None  # type: ignore[attr-defined]
    server.proposal_token = None  # type: ignore[attr-defined]
    proposal_activation = None
    served_port = int(getattr(server, "server_address", (host, port))[1])
    server.proposal_allowed_origins = tuple(  # type: ignore[attr-defined]
        ("http", str(allowed).lower(), served_port) for allowed in allowed_hosts_final
    )
    if enable_proposal_review:
        from .proposal_review.activation import activate_http
        from .proposal_review.outbox import OutboxReconciler
        from .proposal_review.studio_outbox import StudioOutboxAppender
        from .proposal_review.studio_activity import StudioActivityAdapter
        if studio is None:
            raise RuntimeError("Proposal Review requires Studio session state")
        transaction_appender = StudioOutboxAppender(studio)
        try:
            proposal_activation = activate_http(
                bundle_root, transaction_activity=StudioActivityAdapter(studio),
                transaction_outbox=transaction_appender)
            proposal_service = proposal_activation.service
            proposal_outbox = OutboxReconciler(
                proposal_service.repository, transaction_appender)
            proposal_outbox.reconcile_pending(limit=100)
            server.proposal_service = proposal_service  # type: ignore[attr-defined]
            server.proposal_outbox = proposal_outbox  # type: ignore[attr-defined]
            server.proposal_token = proposal_activation.proposal_token  # type: ignore[attr-defined]
        except BaseException:
            if proposal_activation is not None:
                proposal_activation.close()
            raise
    # P0-2: persist the CSRF token to <session>/.token (0o600) so an
    # external agent can read it via ``okf token <bundle>`` (or the library)
    # and then drive /__apply /__presence /__comment /__undo over HTTP.
    # The file is owned by the user running ``okf serve``; the per-session
    # token rotates on each serve start.
    if studio is not None and csrf_token is not None:
        try:
            studio.ensure_session()
            studio.write_session_text(".token", csrf_token)
        except OSError:
            pass

    display_name = server.name  # type: ignore[attr-defined]

    def _concept_revs(b: Bundle) -> dict[str, str]:
        return {
            concept_id_to_str(cid): rev_of(c.raw_text)
            for cid, c in b.concepts.items()
        }

    watcher: _BundleWatcher | None = None
    presence_sweeper = _PresenceSweeper(studio) if studio is not None else None
    # P1-15: --no-watch-ui / studio_live=False disables SSE live push, but
    # the watcher STILL runs so a headless ``okf watch`` consumer (or the
    # ``/__data/events`` polling endpoint) keeps working. When both
    # ``studio_live`` and ``studio_edit`` are False (plain kiosk), we skip
    # the watcher too — there is no consumer for the events.
    if watch and (studio_live or studio_edit):
        state_lock = server._state_lock  # type: ignore[attr-defined]

        def _reload() -> None:
            # Reload the bundle and rebuild the plugin composite atomically
            # under the state lock so a concurrent request cannot see a
            # torn view (new bundle + old palette + old plugin). On failure
            # we re-raise so the watcher logs the error AND does not advance
            # its mtime snapshot (next tick retries — P1-37).
            try:
                old_bundle = server.bundle  # type: ignore[attr-defined]
                old_revs = _concept_revs(old_bundle) if studio else None
                fresh = Bundle.load(bundle_root)
                fresh_plugin = build_viewer_plugin(
                    bundle_root,
                    allow_active_code=effective_allow_active_code(bundle_root),
                )
                with state_lock:
                    server.bundle = fresh  # type: ignore[attr-defined]
                    # Reset cached palette so it recomputes from the new bundle.
                    if hasattr(server, "_palette"):
                        del server._palette  # type: ignore[attr-defined]
                    server.plugin = fresh_plugin  # type: ignore[attr-defined]
                    server._reload_error = None  # type: ignore[attr-defined]
                # Invalidate the per-root override cache so an okf-loom.config.yaml
                # edit (e.g. toggling allow_active_code) made alongside a
                # concept edit is picked up on the next request.
                clear_overrides_cache(bundle_root)
                # Watcher change-diff (§7): publish created/changed/removed
                # + graph events so open tabs patch in place with no refresh.
                if studio is not None and old_revs is not None:
                    new_revs = _concept_revs(fresh)
                    created = [i for i in new_revs if i not in old_revs]
                    removed = [i for i in old_revs if i not in new_revs]
                    changed = [
                        i for i in new_revs
                        if i in old_revs and new_revs[i] != old_revs[i]
                    ]
                    if created:
                        studio.emit_change(kind="created", ids=created, origin="disk")
                    if changed:
                        studio.emit_change(kind="changed", ids=changed, origin="disk")
                    if removed:
                        studio.emit_change(kind="removed", ids=removed, origin="disk")
                    if created or removed or changed:
                        studio.emit_change(kind="graph", ids=[], origin="disk", graph=True)
                    # Cross-process SSE tail (INTENT2-001 / Bundle H finding):
                    # when a CLI mutator in a SEPARATE process wrote via
                    # save_concept, its activity/change/graph events landed in
                    # events.jsonl but were never broadcast to THIS process's
                    # SSE subscribers (the CLI's bus has no subscribers; this
                    # process's bus never saw the append). The dedup map
                    # (.last-logged.json) correctly suppresses a duplicate
                    # disk-origin ``changed`` for the same .md write, but
                    # that leaves the browser with NO signal at all. Tail
                    # events.jsonl for entries newer than the last one this
                    # process published + broadcast them. The bounded
                    # ``_recently_published_events`` set prevents double-
                    # broadcast of this process's own writes.
                    if created or removed or changed:
                        studio.tail_and_broadcast_cross_process_events()
                    # ARCH4-001 fix: the events.jsonl mtime check + staleness
                    # sweep are now handled by the watcher's _tick() callback
                    # (fires every tick, not just when .md changed). They were
                    # previously inside _reload() which is gated by the .md
                    # snapshot diff — so they never fired for session-only
                    # writes (claim/resolve/presence). Removed from here to
                    # avoid duplication; _tick() handles both the mtime→tail
                    # path AND the staleness sweep unconditionally.
            except Exception as e:
                # Record the failure for a transient banner; re-raise so the
                # watcher logs to stderr and does NOT advance its snapshot
                # (the watcher's own except handles logging + retry).
                with state_lock:
                    server._reload_error = str(e)  # type: ignore[attr-defined]
                raise
        # ARCH4-001 fix: the tick_fn fires every watcher tick (1s) regardless
        # of .md activity, handling cross-process session-feed events
        # (events.jsonl mtime → tail broadcast) + the staleness sweep
        # (§13.8). These must fire even when the bundle is idle.
        def _tick() -> None:
            if studio is None:
                return
            # Cross-process SSE tail: check events.jsonl mtime.
            try:
                ev_path = studio.events_path
                if ev_path.is_file():
                    ev_mtime = ev_path.stat().st_mtime
                    last_ev_mtime = getattr(server, "_last_events_mtime", 0.0)
                    if ev_mtime != last_ev_mtime:
                        server._last_events_mtime = ev_mtime
                        studio.tail_and_broadcast_cross_process_events()
            except OSError:
                pass
            # Presence expiry has a separate lifecycle owner so watch=False
            # behaves identically. This tick only owns retained-feed tailing.

        watcher = _BundleWatcher(bundle_root, _reload, interval=1.0, tick_fn=_tick)
        watcher.start()

    # --tunnel: public sharing without touching the bind or the
    # bundle config. The tunnel hostname joins allowed_hosts at runtime, but
    # public requests remain read-only unless tunnel_public_edit was explicitly
    # opted into; the HTTP server itself stays loopback-only.
    # Tunnel state lives on the server object (not run_server locals) so the
    # ``POST /__tunnel`` admin route can attach/detach a tunnel at runtime
    # (``okf tunnel <bundle>``) without restarting serve. base_allowed_hosts
    # is the pre-tunnel allowlist a detach restores.
    server.base_allowed_hosts = tuple(allowed_hosts_final)  # type: ignore[attr-defined]
    server.tunnel_proc = None  # type: ignore[attr-defined]
    server.tunnel_url = None  # type: ignore[attr-defined]
    server.public_tunnel_edit = False  # type: ignore[attr-defined]
    server.public_mutation_token = None  # type: ignore[attr-defined]
    server.public_tunnel_comment = False
    server.public_comment_token = None
    server.public_ingress_generation = None  # type: ignore[attr-defined]
    server.public_ingress_server = None  # type: ignore[attr-defined]
    server.public_ingress_thread = None  # type: ignore[attr-defined]
    server._tunnel_lock = threading.RLock()  # type: ignore[attr-defined]
    server._public_mutation_condition = threading.Condition(  # type: ignore[attr-defined]
        server._tunnel_lock
    )
    server.public_mutation_inflight = {}  # type: ignore[attr-defined]
    server.public_ingress_accepting = False  # type: ignore[attr-defined]
    server.public_ingress_accepting_generation = None  # type: ignore[attr-defined]
    server.public_ingress_draining_generation = None  # type: ignore[attr-defined]
    server.tunnel_settlement_pending = False  # type: ignore[attr-defined]
    server.tunnel_settlement_in_progress = False  # type: ignore[attr-defined]
    server.tunnel_settlement_error = None  # type: ignore[attr-defined]
    server.tunnel_pending_activity_summary = None  # type: ignore[attr-defined]
    server.tunnel_terminal_closing = False  # type: ignore[attr-defined]
    server.tunnel_terminal_drain_timeout_s = 2.0  # type: ignore[attr-defined]
    server._server_state_publication_lock = threading.Lock()  # type: ignore[attr-defined]
    server._tunnel_monitor_stop = None  # type: ignore[attr-defined]
    server._tunnel_monitor_thread = None  # type: ignore[attr-defined]
    from datetime import datetime, timezone
    server.started_iso = (  # type: ignore[attr-defined]
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    if tunnel:
        ingress = None
        try:
            ingress = _start_public_ingress(
                server,
                allow_public_edit=bool(tunnel_public_edit and studio_edit),
                **({'allow_public_comment': True} if tunnel_public_comment else {}),
            )
            proc_url = start_quick_tunnel(ingress.server_address[1])
            with server._public_mutation_condition:  # type: ignore[attr-defined]
                server.tunnel_proc, server.tunnel_url = proc_url  # type: ignore[attr-defined]
        except RuntimeError as e:
            if isinstance(e, TunnelStartOwnershipError):
                with server._public_mutation_condition:  # type: ignore[attr-defined]
                    server.tunnel_proc = e.process  # type: ignore[attr-defined]
                _clear_owned_tunnel(server, studio)
            elif ingress is not None:
                _stop_public_ingress(server)
            print(f"WARNING: --tunnel failed: {e}", file=sys.stderr)
            print("WARNING: continuing local-only.", file=sys.stderr)
        if server.tunnel_url:  # type: ignore[attr-defined]
            if server.public_tunnel_edit:  # type: ignore[attr-defined]
                print(
                    "WARNING: --allow-tunnel-edit is ENABLED. Anyone with the public\n"
                    "WARNING: tunnel URL can comment and mutate studio content. Share\n"
                    "WARNING: the URL only with people you trust.",
                    file=sys.stderr,
                )
            elif server.public_tunnel_comment:
                print('WARNING: anyone with the public URL may create comments and replies; no content or admin powers.', file=sys.stderr)
            else:
                print(
                    f"WARNING: --tunnel makes this bundle READABLE BY ANYONE with the\n"
                    f"WARNING: link below (quick tunnels are unauthenticated). Public\n"
                    f"WARNING: tunnel access is read-only by default.",
                    file=sys.stderr,
                )
    # Start autonomous observation only after startup attach has published the
    # complete (process, URL, generation) owner tuple or finalized local-only.
    _start_tunnel_monitor(server, studio)
    tunnel_url = server.tunnel_url  # type: ignore[attr-defined]
    # server.json: lets session-aware CLI verbs (``okf tunnel``) find the
    # running server's port. Written after the tunnel attempt so a
    # --tunnel start records its URL too.
    write_server_state(server, studio)

    print(
        f"Serving OKF bundle '{display_name}' at http://{host}:{port}/  "
        f"(Ctrl-C to stop)",
        flush=True,
    )
    if tunnel_url:
        print(f"Public tunnel: {tunnel_url}/  (dies with this process)", flush=True)

    if open_browser:
        url = f"http://{host}:{port}/"
        threading.Thread(
            target=lambda: (time.sleep(0.4), webbrowser.open(url)),
            daemon=True,
        ).start()

    try:
        if presence_sweeper is not None:
            presence_sweeper.start()
        server.serve_forever()
    except KeyboardInterrupt:
        # Clean shutdown: don't re-raise (the brief says to handle Ctrl-C at
        # the top of run_server; embedding harnesses can catch this exit).
        pass
    finally:
        # Fence every source of new Studio work first.  Do not join while an
        # outer server/state lock is held: admitted callbacks and handlers may
        # need those locks to finish.  Completion policy is explicit: pending
        # callbacks are cancelled; callbacks already admitted run to their
        # ordinary success/failure handling and are joined before close.
        http_fence = getattr(server, "begin_terminal_http_shutdown", None)
        if callable(http_fence):
            http_fence()
        ingress = getattr(server, "public_ingress_server", None)
        ingress_fence = getattr(ingress, "begin_terminal_http_shutdown", None)
        if callable(ingress_fence):
            ingress_fence()
        condition = getattr(server, "_public_mutation_condition", None)
        if condition is not None:
            with condition:
                server.tunnel_terminal_closing = True  # type: ignore[attr-defined]
                condition.notify_all()

        if watcher is not None:
            request_stop = getattr(watcher, "request_stop", None)
            (request_stop if callable(request_stop) else watcher.stop)()
        if presence_sweeper is not None:
            request_stop = getattr(presence_sweeper, "request_stop", None)
            (request_stop if callable(request_stop) else presence_sweeper.stop)()

        # Stop primary HTTP admission first, then finish the dedicated
        # background publishers while Studio and its bus remain open.
        server.shutdown()
        if watcher is not None:
            watcher.join()
        if (presence_sweeper is not None
                and getattr(presence_sweeper, "ident", None) is not None
                and presence_sweeper is not threading.current_thread()):
            presence_sweeper.join()

        # End SSE/callback subscriptions so long-lived request workers have a
        # deterministic cancellation outcome, then close the listener and
        # join every admitted non-daemon request. Other admitted handlers may
        # complete normally while Studio authority is still open.
        if studio is not None:
            studio.bus.close()
        server.server_close()
        # The tunnel may have been attached at startup or dynamically. Stop
        # its autonomous monitor before settlement of the final owner.
        _shutdown_tunnel_owner(server, studio, terminal=True)
        if studio is not None:
            try:
                studio.unlink_session_file("server.json", missing_ok=True)
            except OSError:
                pass
        # Idempotent with the owned server's close path. Timed-out retrievals
        # can ignore cooperative cancellation, so authority closes only after
        # every admitted worker has settled.
        _shutdown_retrieval_runtime(server)
        if proposal_activation is not None:
            proposal_activation.close()
        if studio is not None:
            studio.close()
