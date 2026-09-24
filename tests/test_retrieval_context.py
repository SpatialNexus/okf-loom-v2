from __future__ import annotations

import json
import copy
import hashlib
import socket
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from okf_loom import Bundle
from okf_loom.cli import main


def _bundle(tmp_path: Path, *, newline: str = "\n", final_newline: bool = True) -> Path:
    root = tmp_path / "bundle"
    root.mkdir()
    (root / "index.md").write_text("---\nokf_version: '0.1'\n---\n# Bundle\n", encoding="utf-8")
    raw = newline.join([
        "---", "type: Guide", "title: Retrieval", "tags: [search]", "---",
        "# First", "Exact alpha paragraph.", "", "```text", "# not a heading", "```",
        "## Empty", "## Final", "unterminated omega",
    ])
    if final_newline:
        raw += newline
    (root / "guide.md").write_bytes(raw.encode("utf-8"))
    return root


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_passages_use_absolute_physical_lines_and_ignore_fenced_headings(
    tmp_path: Path, newline: str
) -> None:
    from okf_loom.retrieval import RetrievalService

    passages = RetrievalService(Bundle.load(_bundle(tmp_path, newline=newline))).passages
    alpha = next(p for p in passages if "Exact alpha" in p.excerpt)
    assert (alpha.heading, alpha.heading_line, alpha.line_start, alpha.line_end) == (
        "First", 6, 7, 11
    )
    assert "# not a heading" in alpha.excerpt
    empty = next(p for p in passages if p.heading == "Empty")
    assert (empty.heading_line, empty.line_start, empty.line_end, empty.excerpt) == (
        12, 12, 12, "## Empty"
    )


def test_context_json_markdown_share_items_and_obey_budget(tmp_path: Path) -> None:
    from okf_loom.retrieval import ContextRequest, RetrievalService

    service = RetrievalService(Bundle.load(_bundle(tmp_path, final_newline=False)))
    pack = service.context(ContextRequest(query="omega", budget_tokens=6000, limit=20))
    markdown = service.context_markdown(pack)
    assert pack["budget"]["estimated_tokens"] <= 6000
    assert pack["items"]
    assert all(
        f"[{i['path']}#L{i['line_start']}-L{i['line_end']}]" in markdown
        for i in pack["items"]
    )
    assert "unterminated omega" in pack["items"][0]["excerpt"]


def test_context_markdown_labels_multi_hop_relation_path_undirected() -> None:
    from okf_loom.retrieval import _markdown_item

    markdown = _markdown_item({
        "citation": "[target.md#L1-L1]",
        "title": "Target",
        "type": "Guide",
        "backends": ["graph"],
        "reasons": ["query-relevant graph neighbour"],
        "relation_path": ["source", "middle", "target"],
        "freshness": None,
        "provenance": {},
        "excerpt": "Evidence.",
    })

    assert "Relation path (undirected): source ↔ middle ↔ target" in markdown
    assert "source -> middle" not in markdown and "source → middle" not in markdown


def test_default_concept_contexts_are_canonical_target_only_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval

    RetrievalService = retrieval.RetrievalService

    root = _bundle(tmp_path, final_newline=False)
    (root / "empty.md").write_text(
        "---\ntype: Guide\ntitle: Voidtargetxyz\n---\n", encoding="utf-8"
    )
    (root / "metadata.md").write_text(
        "---\ntype: Guide\ntitle: Retrieval\ntags: [search, canonical]\n"
        "provenance:\n  source: authored\n  confidence: high\n---\n"
        "# Metadata\nRetrieval metadata-heavy passage.\n",
        encoding="utf-8",
    )
    (root / "many.md").write_text(
        "---\ntype: Guide\ntitle: Many passages\n---\n"
        + "".join(f"# Section {index:02d}\nowned passage {index}\n" for index in range(25)),
        encoding="utf-8",
    )
    service = RetrievalService(Bundle.load(root))
    serialized: dict[str, dict] = {}
    costed: list[str] = []
    original_markdown = RetrievalService.context_markdown
    original_cost = retrieval.governed_item_token_cost

    def record_markdown(pack):
        serialized[pack["filters"]["concept"]] = copy.deepcopy(pack)
        return original_markdown(pack)

    def record_cost(item):
        costed.append(item["concept_id"])
        return original_cost(item)

    monkeypatch.setattr(RetrievalService, "context_markdown", staticmethod(record_markdown))
    monkeypatch.setattr(retrieval, "governed_item_token_cost", record_cost)

    actual = service.default_concept_contexts()
    guide_pack = serialized["guide"]
    assert guide_pack["schema_version"] == 1
    assert guide_pack["query"] == "Retrieval" and guide_pack["mode"] == "concept-only"
    assert guide_pack["filters"]["concept"] == "guide"
    assert guide_pack["neighbors"] == 0 and guide_pack["budget"]["requested_tokens"] == 6000
    assert guide_pack["budget"]["estimated_tokens"] == sum(
        item["estimated_tokens"] for item in guide_pack["items"]
    )
    assert all(item["concept_id"] == "guide" for item in guide_pack["items"])
    assert costed.count("guide") == len(guide_pack["items"])
    many_pack = serialized["many"]
    assert (many_pack["included"], many_pack["omitted"], many_pack["truncated"]) == (20, 5, True)
    assert [item["line_start"] for item in many_pack["items"]] == sorted(
        item["line_start"] for item in many_pack["items"]
    )
    assert actual == service.default_concept_contexts(reversed(tuple(actual)))
    assert list(actual) == sorted(actual)
    assert "- Query: `Retrieval`" in actual["guide"]
    assert "- Mode: `concept-only`" in actual["guide"]
    assert "[guide.md#" in actual["guide"]
    assert "[metadata.md#" not in actual["guide"]
    assert "[metadata.md#" in actual["metadata"]
    assert "[guide.md#" not in actual["metadata"]
    assert "Backends: concept-target" in actual["guide"]
    assert "Reasons: explicit concept target" in actual["guide"]
    assert "Included: 0; omitted: 0" in actual["empty"]


def test_default_concept_contexts_build_indexes_once_without_per_concept_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval
    from okf_loom.search import LexicalBackend

    root = _bundle(tmp_path)
    for index in range(40):
        (root / f"concept-{index}.md").write_text(
            f"---\ntype: Guide\ntitle: Shared term {index}\n---\n"
            f"# Shared section\nshared term body {index}\n",
            encoding="utf-8",
        )
    service = retrieval.RetrievalService(Bundle.load(root))
    derives = 0
    passage_iterations = 0
    original_derive = retrieval.derive_passages

    class CountingPassages(tuple):
        def __iter__(self):
            nonlocal passage_iterations
            passage_iterations += 1
            return super().__iter__()

    def counted_derive(bundle):
        nonlocal derives
        derives += 1
        return CountingPassages(original_derive(bundle))

    monkeypatch.setattr(retrieval, "derive_passages", counted_derive)
    monkeypatch.setattr(LexicalBackend, "search", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("batch must not run one full lexical search per concept")
    ))
    monkeypatch.setattr(LexicalBackend, "_score_doc", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("batch must not score title-query postings")
    ))
    monkeypatch.setattr(service, "context", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("batch must not run one context call per concept")
    ))

    result = service.default_concept_contexts()

    assert len(result) == 41
    assert derives == 1
    assert passage_iterations == 1


def test_default_concept_contexts_validate_targets_and_refresh_with_new_snapshot(
    tmp_path: Path,
) -> None:
    from okf_loom.retrieval import (
        RetrievalContractError, RetrievalService, set_retrieval_cancel_event,
    )

    root = _bundle(tmp_path)
    first = RetrievalService(Bundle.load(root)).default_concept_contexts(["guide"])["guide"]
    path = root / "guide.md"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "Exact alpha paragraph.", "Fresh alpha paragraph."
    ), encoding="utf-8")
    second = RetrievalService(Bundle.load(root)).default_concept_contexts(["guide"])["guide"]
    assert first != second
    assert "Fresh alpha paragraph." in second

    service = RetrievalService(Bundle.load(root))
    for targets, code, status in [
        (["missing"], "concept_not_found", 404),
        (["../guide"], "invalid_target", 400),
        (["guide", "guide"], "invalid_request", 400),
        ("guide", "invalid_request", 400),
    ]:
        with pytest.raises(RetrievalContractError) as exc:
            service.default_concept_contexts(targets)
        assert (exc.value.code, exc.value.status) == (code, status)

    cancelled = threading.Event()
    cancelled.set()
    set_retrieval_cancel_event(cancelled)
    try:
        with pytest.raises(RetrievalContractError) as exc:
            service.default_concept_contexts(["guide"])
        assert exc.value.code == "retrieval_timeout"
    finally:
        set_retrieval_cancel_event(None)


def test_context_cli_json_contract_and_search_limit_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _bundle(tmp_path)
    assert main(["context", str(root), "alpha", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["items"][0]["path"] == "guide.md"
    with pytest.raises(SystemExit) as exc:
        main(["search", str(root), "alpha", "--limit", "0"])
    assert exc.value.code == 2
    for unsupported in ("md", "dot"):
        with pytest.raises(SystemExit) as exc:
            main(["search", str(root), "alpha", "--format", unsupported])
        assert exc.value.code == 2


@pytest.mark.parametrize("unsupported", [{"set"}, Path("relative"), object(), float("nan")])
def test_cli_json_rejects_unsupported_values_without_partial_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    unsupported: object,
) -> None:
    from okf_loom.search import SearchResult

    root = _bundle(tmp_path)
    monkeypatch.setattr(SearchResult, "as_dict", lambda self: {"unsupported": unsupported})
    assert main(["search", str(root), "alpha", "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "non-canonical JSON value" in captured.err


def test_hybrid_search_is_read_only_and_capability_is_static(tmp_path: Path) -> None:
    from okf_loom.retrieval import RetrievalService, SearchRequest

    bundle = Bundle.load(_bundle(tmp_path))
    caps_before = bundle.capabilities()
    concepts_before = dict(bundle.concepts)
    results = RetrievalService(bundle).search(SearchRequest(query="alpha", mode="hybrid"))
    assert results and results[0].as_dict()["evidence"]
    assert bundle.capabilities() is caps_before
    assert bundle.concepts == concepts_before
    assert caps_before.is_active("okf.cap.search_hybrid")


def test_corpus_manifest_is_public_deterministic_and_content_addressed(tmp_path: Path) -> None:
    from okf_loom.retrieval import build_passage_corpus, load_passage_corpus

    bundle = Bundle.load(_bundle(tmp_path))
    first = build_passage_corpus(bundle, chunk_size=2)
    second = build_passage_corpus(bundle, chunk_size=2)
    assert first == second
    assert first["manifest"]["schema_version"] == 1
    assert first["chunks"]
    assert len(load_passage_corpus(first)) == first["manifest"]["passage_count"]
    rendered = json.dumps(first, sort_keys=True)
    assert str(tmp_path) not in rendered
    assert all(c["id"].startswith(first["manifest"]["bundle_revision"] + ":")
               for c in first["chunks"])


def test_passage_corpus_holds_one_generation_through_projection_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval

    root = _bundle(tmp_path)
    bundle = Bundle.load(root)
    old_revision = retrieval.bundle_revision(bundle)
    entered = threading.Event(); release = threading.Event()
    original = retrieval.Passage.public_dict

    def blocked(self, **kwargs):
        entered.set(); release.wait(timeout=5)
        return original(self, **kwargs)

    monkeypatch.setattr(retrieval.Passage, "public_dict", blocked)
    artifacts: list[dict] = []
    worker = threading.Thread(target=lambda: artifacts.append(retrieval.build_passage_corpus(bundle)))
    worker.start(); assert entered.wait(timeout=2)
    path = root / "guide.md"
    path.write_text(path.read_text(encoding="utf-8").replace("alpha", "freshneedle"),
                    encoding="utf-8")
    reloader = threading.Thread(target=bundle.reload)
    reloader.start(); time.sleep(0.03)
    assert reloader.is_alive(), "reload must wait for complete corpus generation"
    release.set(); worker.join(timeout=5); reloader.join(timeout=5)
    artifact = artifacts[0]
    assert artifact["manifest"]["bundle_revision"] == old_revision
    assert all(chunk["bundle_revision"] == old_revision for chunk in artifact["chunks"])
    assert all(chunk["id"].startswith(old_revision + ":") for chunk in artifact["chunks"])
    assert retrieval.bundle_revision(bundle) != old_revision


def test_corpus_identity_cancellation_publishes_nothing_and_releases_generation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval

    bundle = Bundle.load(_bundle(tmp_path))
    service = retrieval.RetrievalService(bundle)
    passage_cache = service.passages
    revision_cache = service.revision
    entered = threading.Event(); release = threading.Event(); acquired = threading.Event()
    cancel = threading.Event()
    original = retrieval.corpus_chunk_identity

    def blocked_identity(revision, index, passages=None, *, content_hash=None):
        entered.set(); release.wait(timeout=5)
        return original(revision, index, passages, content_hash=content_hash)

    monkeypatch.setattr(retrieval, "corpus_chunk_identity", blocked_identity)
    artifacts: list[dict] = []
    failures: list[BaseException] = []

    def build():
        retrieval.set_retrieval_cancel_event(cancel)
        try:
            artifacts.append(retrieval.build_passage_corpus(bundle, chunk_size=1))
        except BaseException as exc:
            failures.append(exc)
        finally:
            retrieval.set_retrieval_cancel_event(None)

    worker = threading.Thread(target=build)
    worker.start(); assert entered.wait(timeout=2)

    def wait_for_lock():
        with bundle._derived_lock:
            acquired.set()

    waiter = threading.Thread(target=wait_for_lock)
    waiter.start(); time.sleep(0.03)
    assert not acquired.is_set(), "identity generation must still own the generation lock"
    assert artifacts == []
    assert bundle._passage_index[1] is passage_cache
    assert bundle._bundle_revision == revision_cache

    cancel.set(); release.set()
    worker.join(timeout=2); waiter.join(timeout=2)
    assert acquired.is_set(), "cancelled identity path must release the lock promptly"
    assert artifacts == [] and len(failures) == 1
    assert isinstance(failures[0], retrieval.RetrievalContractError)
    assert failures[0].code == "retrieval_timeout"
    assert bundle._passage_index[1] is passage_cache
    assert bundle._bundle_revision == revision_cache


def _serve(bundle: Bundle):
    from okf_loom.server import OKFWikiHandler

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), OKFWikiHandler)
    server.bundle = bundle  # type: ignore[attr-defined]
    server.config = {}  # type: ignore[attr-defined]
    server.name = bundle.name  # type: ignore[attr-defined]
    server.plugin = None  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


def _http_json(url: str) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _http(url: str) -> tuple[int, object, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_native_search_form_redirect_is_read_only_and_does_not_bypass_closed_keys(tmp_path: Path) -> None:
    import http.client

    root = _bundle(tmp_path)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    server, thread, port = _serve(Bundle.load(root))

    def get(target):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            connection.request('GET', target)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    try:
        target = '/__search-form?q=alpha&mode=hybrid&type=&tag=&entity=draft&relation=&source=&target=&limit=20'
        for _ in range(2):  # Replay has no mutation or retrieval side effect.
            status, headers, body = get(target)
            assert status == 303 and body == b''
            assert headers['Location'] == '/__search?q=alpha&mode=hybrid&limit=20'
        assert get(headers['Location'])[0] == 200
        status, headers, _ = get('/__search-form?mode=relation&source=nested%2Fitem')
        assert status == 303 and headers['Location'] == '/__search?mode=relation&source=nested/item'
        for query in ('q=alpha&format=json', 'q=alpha&format=html', 'q=alpha&unknown=x',
                      'q=alpha&q=beta', 'q=%ZZ', 'q=%FF'):
            status, headers, body = get('/__search-form?' + query)
            assert status == 400, (query, status)
            assert 'Location' not in headers
            assert str(root).encode() not in body
        # The presentation adapter must not relax the strict API itself.
        for query in ('q=alpha&type=', 'q=alpha&mode=hybrid&entity=draft'):
            assert get('/__search?' + query)[0] == 400
        assert {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()} == before
        assert not (root / '.okf-loom').exists()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_http_search_context_parity_and_fail_closed_query_contract(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    server, thread, port = _serve(Bundle.load(root))
    base = f"http://127.0.0.1:{port}"
    try:
        status, search = _http_json(base + "/__search?q=alpha&mode=hybrid&format=json&limit=1")
        assert status == 200 and isinstance(search, list)
        assert search[0]["evidence"]["passage"]["line_start"] == 7
        status, context = _http_json(base + "/__context?q=alpha&format=json&limit=1")
        assert status == 200 and context["items"][0]["concept_id"] == search[0]["concept_id"]
        status, headers, markdown = _http(base + "/__context?q=alpha&format=markdown&limit=1")
        assert status == 200 and headers.get_content_type() == "text/markdown"
        assert headers["ETag"] and headers["Cache-Control"] == "private, no-cache"
        assert b"[guide.md#L" in markdown

        for suffix, expected in [
            ("/__search?q=x&q=y&format=json", 400),
            ("/__search?q=x&unknown=y&format=json", 400),
            ("/__search?q=x&limit=0&format=json", 400),
            ("/__search?q=x&mode=lexical&source=guide&format=json", 400),
            ("/__search?mode=relation&target=missing&format=json", 404),
            ("/__search?q=%ZZ&format=json", 400),
            ("/__search?q=%FF&format=json", 400),
            ("/__context?format=json", 400),
            ("/__context?concept=missing&format=json", 404),
        ]:
            code, payload = _http_json(base + suffix)
            assert code == expected
            assert payload["ok"] is False
            assert set(payload["error"]) == {"code", "message", "fields"}
        assert _http_json(base + "/__search?q=" + ("x" * 4096) + "&format=json")[0] == 200
        assert _http_json(base + "/__search?q=" + ("x" * 4097) + "&format=json")[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_all_six_modes_expose_additive_evidence_without_removing_legacy_keys(tmp_path: Path) -> None:
    from okf_loom.retrieval import RetrievalService, SearchRequest

    root = _bundle(tmp_path)
    (root / "linked.md").write_text(
        "---\ntype: Guide\ntitle: Linked\ntags: [search]\n"
        "entities: [Alpha Entity]\nrelations:\n  - target: guide\n    type: cites\n---\n"
        "# Linked\nAlpha Entity links to [guide](/guide.md).\n",
        encoding="utf-8",
    )
    service = RetrievalService(Bundle.load(root))
    legacy = {"concept_id", "title", "score", "snippets", "source_backend",
              "matched_tags", "description"}
    requests = [
        SearchRequest("alpha", mode="lexical"), SearchRequest("alpha", mode="semantic"),
        SearchRequest("alpha", mode="hybrid"), SearchRequest("search", mode="tag"),
        SearchRequest("Alpha Entity", mode="entity"),
        SearchRequest("", mode="relation", relation="cites"),
    ]
    for request in requests:
        payload = service.search(request)[0].as_dict()
        assert legacy <= payload.keys()
        assert payload["evidence"]["schema_version"] == 1
        assert len(payload["reasons"]) <= 8


def test_revision_and_passage_cache_invalidate_after_material_edits(tmp_path: Path) -> None:
    from okf_loom.retrieval import RetrievalService

    bundle = Bundle.load(_bundle(tmp_path))
    service = RetrievalService(bundle)
    old_revision = service.revision
    old_passages = service.passages
    concept = bundle.concepts[("guide",)]
    concept.raw_text = concept.raw_text.replace("alpha", "beta").replace(
        "tags: [search]", "tags: [changed]\nrelations:\n  - target: guide\n    type: self"
    )
    concept.body = concept.body.replace("alpha", "beta")
    concept.frontmatter["tags"] = ["changed"]
    concept.frontmatter["relations"] = [{"target": "guide", "type": "self"}]
    bundle.invalidate()
    fresh = RetrievalService(bundle)
    assert fresh.revision != old_revision
    assert fresh.passages is not old_passages
    assert any("beta" in passage.excerpt for passage in fresh.passages)
    assert fresh.passages[0].tags == ("changed",)


def test_long_physical_line_chunks_deterministically_under_excerpt_cap(tmp_path: Path) -> None:
    from okf_loom.retrieval import RetrievalService

    root = _bundle(tmp_path)
    path = root / "guide.md"
    path.write_text("---\ntype: Guide\n---\n# Long\n" + ("x" * 4501), encoding="utf-8")
    first = RetrievalService(Bundle.load(root)).passages
    second = RetrievalService(Bundle.load(root)).passages
    assert [(p.line_start, p.line_end, p.excerpt) for p in first] == [
        (p.line_start, p.line_end, p.excerpt) for p in second
    ]
    assert [len(p.excerpt) for p in first] == [2000, 2000, 501]


@pytest.mark.parametrize(
    ("needle", "expected_start", "expected_end"),
    [("FIRST", 0, 2000), ("MIDDLE", 2000, 4000), ("FINAL", 4000, 4501)],
)
def test_split_line_passage_identity_evidence_and_citation_agree(
    tmp_path: Path, needle: str, expected_start: int, expected_end: int
) -> None:
    from okf_loom.retrieval import ContextRequest, RetrievalService, SearchRequest

    root = _bundle(tmp_path)
    long_line = (
        "FIRST " + ("a" * 1993) + " "
        + "MIDDLE " + ("b" * 1992) + " "
        + "FINAL " + ("c" * 495)
    )
    (root / "guide.md").write_text(
        "---\ntype: Guide\ntitle: Long\n---\n# Long\n" + long_line,
        encoding="utf-8",
    )
    service = RetrievalService(Bundle.load(root))
    result = service.search(SearchRequest(needle))[0].as_dict()
    passage = result["evidence"]["passage"]
    pack = service.context(ContextRequest(needle, mode="lexical"))
    item = pack["items"][0]
    assert (passage["char_start"], passage["char_end"]) == (expected_start, expected_end)
    assert item["passage_id"] == passage["passage_id"]
    assert item["excerpt"] == passage["excerpt"]
    assert f"@C{expected_start}-C{expected_end}" in service.context_markdown(pack)


def test_context_ranks_multiple_relevant_sections_in_one_concept(tmp_path: Path) -> None:
    from okf_loom.retrieval import ContextRequest, RetrievalService

    root = _bundle(tmp_path)
    (root / "guide.md").write_text(
        "---\ntype: Guide\ntitle: Multi\n---\n# Alpha One\nneedle first\n"
        "# Alpha Two\nneedle second\n# Irrelevant\nother\n",
        encoding="utf-8",
    )
    pack = RetrievalService(Bundle.load(root)).context(
        ContextRequest("needle", mode="lexical", limit=10)
    )
    assert [item["heading"] for item in pack["items"]] == ["Alpha One", "Alpha Two"]
    assert pack["omitted"] == 0
    assert pack["truncated"] is False


def test_context_common_term_candidate_and_metadata_projection_are_bounded(tmp_path: Path) -> None:
    import okf_loom.retrieval as retrieval

    root = _bundle(tmp_path)
    sections = "\n".join(f"# Section {index}\ncommon term {index}" for index in range(1000))
    (root / "guide.md").write_text(
        "---\ntype: Guide\ntitle: Large\nprovenance:\n  giant: "
        + ("P" * 20000) + "\n---\n" + sections + "\n",
        encoding="utf-8",
    )
    pack = retrieval.RetrievalService(Bundle.load(root)).context(
        retrieval.ContextRequest("common", mode="lexical", limit=100,
                                 budget_tokens=50000, neighbors=0)
    )
    assert pack["included"] == retrieval.MAX_CONTEXT_PASSAGES_PER_DOCUMENT
    assert pack["omitted"] == 1000 - retrieval.MAX_CONTEXT_PASSAGES_PER_DOCUMENT
    assert pack["truncated"] is True
    for item in pack["items"]:
        projected = json.dumps(item["provenance"], ensure_ascii=False, sort_keys=True)
        assert len(projected) <= retrieval.PUBLIC_METADATA_MAX_CODEPOINTS
        assert '"truncated": true' in projected


def test_context_budget_counts_binding_evidence_projection(tmp_path: Path) -> None:
    from okf_loom.retrieval import ContextRequest, RetrievalService, governed_item_token_cost

    root = _bundle(tmp_path)
    long_title = "T" * 700
    long_target = "target_" + ("z" * 180)
    (root / f"{long_target}.md").write_text(
        "---\ntype: Guide\ntitle: Target\n---\n# Target\nbody\n", encoding="utf-8"
    )
    (root / "guide.md").write_text(
        "---\ntype: Guide\ntitle: " + long_title + "\n"
        "provenance:\n  note: " + ("P" * 700) + "\n"
        "relations:\n  - target: " + long_target + "\n    type: depends_on\n"
        "---\n# H\nneedle\n",
        encoding="utf-8",
    )
    service = RetrievalService(Bundle.load(root))
    request = dict(query="needle", mode="relation", relation="depends_on")
    roomy = service.context(ContextRequest(**request, budget_tokens=50000))
    item = roomy["items"][0]
    assert long_target in json.dumps(item["evidence"])
    exact = governed_item_token_cost(item)
    display_only = dict(item, title="different" * 1000, provenance={"ignored": "x" * 1000})
    assert governed_item_token_cost(display_only) == exact
    assert governed_item_token_cost(dict(item, excerpt=item["excerpt"] + " evidence")) > exact
    assert item["estimated_tokens"] == exact
    assert service.context(ContextRequest(**request, budget_tokens=exact))["included"] == 1
    assert service.context(ContextRequest(**request, budget_tokens=max(128, exact - 1)))["included"] == 0


def test_service_validation_boundaries_and_encoded_target_rejection(tmp_path: Path) -> None:
    from okf_loom.retrieval import (ContextRequest, RetrievalContractError,
                                    RetrievalService, SearchRequest, parse_http_query)

    bundle = Bundle.load(_bundle(tmp_path))
    service = RetrievalService(bundle)
    assert SearchRequest("x" * 4096, limit=1).normalized().limit == 1
    assert SearchRequest("x", limit=100).normalized().limit == 100
    for request in [SearchRequest("x" * 4097), SearchRequest("x", limit=0),
                    SearchRequest("x", limit=101), SearchRequest("x", type_filter="x" * 257),
                    SearchRequest("x", mode="lexical", entity="alpha")]:
        with pytest.raises(RetrievalContractError):
            request.normalized()
    assert service.context(ContextRequest("alpha", budget_tokens=128, neighbors=0))["neighbors"] == 0
    assert service.context(ContextRequest("alpha", budget_tokens=50000, neighbors=2))["neighbors"] == 2
    for request in [ContextRequest("alpha", budget_tokens=127),
                    ContextRequest("alpha", budget_tokens=50001),
                    ContextRequest("alpha", neighbors=3)]:
        with pytest.raises(RetrievalContractError):
            service.context(request)
    with pytest.raises(RetrievalContractError):
        parse_http_query("concept=foo%2Fbar", allowed={"concept"})
    with pytest.raises(RetrievalContractError):
        parse_http_query("source=foo%5Cbar", allowed={"source"})


def test_canonical_validator_wrong_types_modes_and_seed_forms(tmp_path: Path) -> None:
    from okf_loom.retrieval import ContextRequest, RetrievalContractError, RetrievalService, SearchRequest

    bundle = Bundle.load(_bundle(tmp_path))
    service = RetrievalService(bundle)
    for request in [
        SearchRequest(query=1), SearchRequest("x", mode=None), SearchRequest("x", limit="20"),
        SearchRequest("x", type_filter=object()),
    ]:
        with pytest.raises(RetrievalContractError):
            service.search(request)
    with pytest.raises(RetrievalContractError) as explicit_empty:
        service.context(ContextRequest(concept=""))
    assert explicit_empty.value.code == "invalid_request"
    entity = ContextRequest(mode="entity", entity="Retrieval").normalized(bundle)
    assert entity.query == "Retrieval"
    relation = ContextRequest(mode="relation", relation="depends_on").normalized(bundle)
    assert relation.query == "" and relation.relation == "depends_on"
    with pytest.raises(RetrievalContractError) as missing:
        service.context(ContextRequest(mode="lexical"))
    assert missing.value.code == "missing_seed"


def test_physical_citations_ignore_unicode_and_non_lf_separators(tmp_path: Path) -> None:
    from okf_loom.retrieval import RetrievalService

    root = _bundle(tmp_path)
    (root / "guide.md").write_text(
        "---\ntype: Guide\ntitle: Separators\n---\n# Heading\n"
        "one\u0085two\u000bthree\u000cfour\u2028five\u2029six\n",
        encoding="utf-8",
    )
    passage = RetrievalService(Bundle.load(root)).passages[0]
    assert (passage.heading_line, passage.line_start, passage.line_end) == (5, 6, 6)
    assert "\u0085" in passage.excerpt and "\u2028" in passage.excerpt


def test_canonical_json_and_public_metadata_reject_non_finite_values(tmp_path: Path) -> None:
    import okf_loom.retrieval as retrieval

    assert retrieval.canonical_json_bytes({"unicode": "🧭"}) == b'{"unicode":"\xf0\x9f\xa7\xad"}'
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(retrieval.RetrievalContractError) as exc:
            retrieval.canonical_json_bytes({"value": value})
        assert exc.value.code == "invalid_json_value"
        with pytest.raises(retrieval.RetrievalContractError) as metadata:
            retrieval._public_value(value)
        assert metadata.value.code == "invalid_public_metadata"

    for value in ({"x"}, Path("relative"), object(), {1: "non-string-key"}):
        with pytest.raises(retrieval.RetrievalContractError) as exc:
            retrieval.canonical_json_bytes({"value": value})
        assert exc.value.code == "invalid_json_value"
        with pytest.raises(retrieval.RetrievalContractError) as metadata:
            retrieval._public_value(value)
        assert metadata.value.code == "invalid_public_metadata"


def test_default_concept_pack_accepts_internal_title_beyond_public_query_cap(tmp_path: Path) -> None:
    from okf_loom.retrieval import RetrievalService

    root = _bundle(tmp_path)
    title = "T" * 5000
    (root / "guide.md").write_text(
        f"---\ntype: Guide\ntitle: {title}\n---\n# Body\nowned\n", encoding="utf-8"
    )
    markdown = RetrievalService(Bundle.load(root)).default_concept_contexts(["guide"])["guide"]
    assert f"- Query: `{title}`" in markdown and "[guide.md#" in markdown


def test_queryless_concept_with_long_authored_title_service_cli_and_http(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from okf_loom.retrieval import ContextRequest, RetrievalService

    root = _bundle(tmp_path)
    title = "Long authored identity " + ("T" * 5000)
    (root / "guide.md").write_text(
        f"---\ntype: Guide\ntitle: {title}\n---\n# Body\nowned evidence\n",
        encoding="utf-8",
    )
    pack = RetrievalService(Bundle.load(root)).context(ContextRequest(concept="guide"))
    assert pack["filters"]["concept"] == "guide"
    assert pack["query"] == title
    assert pack["items"][0]["concept_id"] == "guide"
    assert main(["context", str(root), "--concept", "guide", "--format", "json"]) == 0
    cli_pack = json.loads(capsys.readouterr().out)
    assert cli_pack["query"] == title and cli_pack["items"][0]["concept_id"] == "guide"

    server, thread, port = _serve(Bundle.load(root))
    try:
        status, http_pack = _http_json(
            f"http://127.0.0.1:{port}/__context?concept=guide&format=json"
        )
        assert status == 200
        assert http_pack["query"] == title and http_pack["filters"]["concept"] == "guide"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_graph_traversal_uses_fifo_deque_for_scale(monkeypatch: pytest.MonkeyPatch) -> None:
    import collections
    from types import SimpleNamespace
    import okf_loom.retrieval as retrieval

    popleft_calls = 0

    class CountingDeque(collections.deque):
        def popleft(self):
            nonlocal popleft_calls
            popleft_calls += 1
            return super().popleft()

    nodes = [(f"node-{index}",) for index in range(500)]
    out_edges = {
        nodes[index]: [SimpleNamespace(source=nodes[index], target=nodes[index + 1])]
        for index in range(len(nodes) - 1)
    }
    graph = SimpleNamespace(out_edges=out_edges, in_edges={}, concept_ids=set(nodes))
    concepts = {node: SimpleNamespace(type="Guide", tags=[]) for node in nodes}
    bundle = SimpleNamespace(graph=lambda: graph, concepts=concepts)
    monkeypatch.setattr(retrieval, "deque", CountingDeque)
    found = retrieval._bounded_graph_neighbour_paths(
        bundle, [nodes[0]], max_depth=499, type_filter=None, tag=None, max_documents=499,
    )
    assert len(found) == 499 and popleft_calls == 499


def test_retrieval_preserves_actual_hybrid_contributor_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okf_loom.paths import concept_id_from_str
    from okf_loom.retrieval import RetrievalService, SearchRequest
    from okf_loom.search import SearchResult
    import okf_loom.search as search_module

    row = SearchResult(
        concept_id_from_str("guide"), "Guide", 1.0, source_backend="hybrid",
        backends=["semantic-lite"],
    )
    monkeypatch.setattr(search_module, "search_bundle", lambda *args, **kwargs: [row])
    result = RetrievalService(Bundle.load(_bundle(tmp_path))).search(
        SearchRequest("alpha", mode="hybrid")
    )[0]
    assert result.backends == ["semantic-lite"]
    assert result.reasons == ["ranked by semantic-lite"]


def test_http_request_uses_one_bundle_snapshot_across_concurrent_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okf_loom.retrieval import RetrievalService

    root = _bundle(tmp_path)
    old_bundle = Bundle.load(root)
    old_revision = RetrievalService(old_bundle).revision
    path = root / "guide.md"
    path.write_text(path.read_text(encoding="utf-8").replace("alpha", "beta"), encoding="utf-8")
    new_bundle = Bundle.load(root)
    server, thread, port = _serve(old_bundle)
    original = RetrievalService.search

    def swap_then_search(self, request):
        server.bundle = new_bundle  # type: ignore[attr-defined]
        return original(self, request)

    monkeypatch.setattr(RetrievalService, "search", swap_then_search)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/__search?q=alpha&format=json", timeout=5
        ) as response:
            payload = json.loads(response.read())
            assert response.headers["X-OKF-Bundle-Revision"] == old_revision
        assert "alpha" in payload[0]["evidence"]["passage"]["excerpt"]
        assert server.bundle is new_bundle  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_timed_out_keepalive_worker_owns_immutable_request_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import http.client
    import okf_loom.server as server_module

    root = _bundle(tmp_path)
    old_bundle = Bundle.load(root)
    path = root / "guide.md"
    path.write_text(path.read_text(encoding="utf-8").replace("alpha", "beta"), encoding="utf-8")
    new_bundle = Bundle.load(root)
    observations: list[tuple[str, str]] = []
    original = server_module.OKFWikiHandler._run_search

    def delayed(self, q, *, limit, bundle=None, request=None):
        if q == "alpha":
            time.sleep(0.08)
        observations.append((q, request.query))
        return original(self, q, limit=limit, bundle=bundle, request=request)

    monkeypatch.setattr(server_module, "RETRIEVAL_DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(server_module.OKFWikiHandler, "_run_search", delayed)
    server, thread, port = _serve(old_bundle)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/__search?q=alpha&format=json")
        first = connection.getresponse()
        assert first.status == 503
        first.read()
        server.bundle = new_bundle  # type: ignore[attr-defined]
        connection.request("GET", "/__search?q=beta&format=json")
        second = connection.getresponse()
        assert second.status == 200
        second.read()
        time.sleep(0.1)
        assert ("alpha", "alpha") in observations and ("beta", "beta") in observations
    finally:
        connection.close()
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_apply_plan_write_advances_revision_source_snapshot(tmp_path: Path) -> None:
    from okf_loom.retrieval import RetrievalService
    from okf_loom.update import UpdateOp, UpdatePlan, apply_plan

    bundle = Bundle.load(_bundle(tmp_path))
    old_revision = RetrievalService(bundle).revision
    plan = UpdatePlan(bundle_root=str(bundle.root), description="change tag",
        ops=[UpdateOp(kind="set_frontmatter", target=("guide",),
                      args={"key": "tags", "value": ["fresh"]})],
        plan_kind="single-op")
    result = apply_plan(bundle, plan, apply=True)
    assert result["applied"] == 1
    service = RetrievalService(bundle)
    assert service.revision != old_revision
    assert service.passages[0].tags == ("fresh",)
    assert "fresh" in bundle.concepts[("guide",)].raw_text


def test_invalidate_synchronizes_direct_public_concept_mutation(tmp_path: Path) -> None:
    from okf_loom.retrieval import RetrievalService, SearchRequest

    root = _bundle(tmp_path)
    (root / "linked.md").write_text("---\ntype: Guide\ntitle: Linked\n---\n# Linked\nbody\n",
                                      encoding="utf-8")
    bundle = Bundle.load(root)
    old_revision = RetrievalService(bundle).revision
    concept = bundle.concepts[("guide",)]
    concept.body += "\n# Fresh Section\nfreshneedle [linked](/linked.md)\n"
    concept.frontmatter["title"] = "Fresh Title"
    bundle.invalidate()
    service = RetrievalService(bundle)
    result = service.search(SearchRequest("freshneedle"))[0].as_dict()
    assert "freshneedle" in result["evidence"]["passage"]["excerpt"]
    assert service.revision != old_revision
    assert "Fresh Title" in concept.raw_text and "freshneedle" in concept.raw_text
    assert ("linked",) in bundle.graph().neighbours(("guide",))


@pytest.mark.parametrize("use_studio", [False, True])
def test_write_concept_refreshes_supplied_bundle_for_create_and_update(
    tmp_path: Path, use_studio: bool
) -> None:
    from okf_loom.retrieval import RetrievalService, SearchRequest
    from okf_loom.studio import Studio
    from okf_loom.update import write_concept

    bundle = Bundle.load(_bundle(tmp_path))
    studio = Studio.for_bundle(bundle.root) if use_studio else None
    write_concept(bundle, "new", type="Guide", title="Created", body="createword",
                  defaults=False, studio=studio)
    assert RetrievalService(bundle).search(SearchRequest("createword"))[0].concept_id == ("new",)
    write_concept(bundle, "new", type="Guide", title="Updated", body="updateword",
                  force=True, defaults=False, studio=studio)
    result = RetrievalService(bundle).search(SearchRequest("updateword"))[0].as_dict()
    assert result["title"] == "Updated"
    assert "updateword" in result["evidence"]["passage"]["excerpt"]


def test_concurrent_cold_passage_publication_is_single_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval

    bundle = Bundle.load(_bundle(tmp_path))
    original = retrieval.derive_passages
    calls = 0
    calls_lock = threading.Lock()

    def counted(target):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        return original(target)

    monkeypatch.setattr(retrieval, "derive_passages", counted)
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: retrieval.RetrievalService(bundle).passages, range(8)))
    assert calls == 1
    assert all(value is values[0] for value in values)


def test_concurrent_cold_search_backend_index_is_single_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okf_loom.retrieval import RetrievalService, SearchRequest
    from okf_loom.search import LexicalBackend

    bundle = Bundle.load(_bundle(tmp_path))
    original = LexicalBackend.index
    calls = 0
    lock = threading.Lock()

    def counted(self, content_index):
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.02)
        return original(self, content_index)

    monkeypatch.setattr(LexicalBackend, "index", counted)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: RetrievalService(bundle).search(SearchRequest("alpha")), range(8)
        ))
    assert calls == 1
    assert all(result[0].concept_id == ("guide",) for result in results)


def test_cancelled_cold_index_is_not_published_and_retry_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okf_loom.retrieval import (RetrievalContractError, RetrievalService, SearchRequest,
                                    set_retrieval_cancel_event)
    from okf_loom.search import LexicalBackend

    root = _bundle(tmp_path)
    for index in range(80):
        (root / f"extra-{index}.md").write_text(
            f"---\ntype: Guide\ntitle: Extra {index}\n---\nneedle body\n", encoding="utf-8"
        )
    bundle = Bundle.load(root)
    cancel = threading.Event()
    original = LexicalBackend._field_texts
    calls = 0

    def cancelling(concept):
        nonlocal calls
        calls += 1
        if calls == 2:
            cancel.set()
        return original(concept)

    monkeypatch.setattr(LexicalBackend, "_field_texts", staticmethod(cancelling))
    set_retrieval_cancel_event(cancel)
    try:
        with pytest.raises(RetrievalContractError) as exc:
            RetrievalService(bundle).search(SearchRequest("needle"))
        assert exc.value.code == "retrieval_timeout"
        assert bundle._lexical_backend is None
    finally:
        set_retrieval_cancel_event(None)
    cancel.clear()
    assert RetrievalService(bundle).search(SearchRequest("needle"))


def test_cancelled_cold_content_index_releases_lock_without_cache_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval
    from okf_loom.model import Concept

    root = _bundle(tmp_path)
    for index in range(80):
        (root / f"extra-{index}.md").write_text(
            f"---\ntype: Guide\ntitle: Extra {index}\n---\nneedle {index}\n", encoding="utf-8"
        )
    bundle = Bundle.load(root)
    cancel = threading.Event()
    original = Concept.search_text
    calls = 0

    def cancelling(self):
        nonlocal calls
        calls += 1
        if calls == 2:
            cancel.set()
        return original(self)

    monkeypatch.setattr(Concept, "search_text", cancelling)
    retrieval.set_retrieval_cancel_event(cancel)
    try:
        with pytest.raises(retrieval.RetrievalContractError) as exc:
            retrieval.RetrievalService(bundle).search(retrieval.SearchRequest("needle"))
        assert exc.value.code == "retrieval_timeout"
        assert bundle._content_index is None and bundle._graph is None
    finally:
        retrieval.set_retrieval_cancel_event(None)
    acquired = threading.Event()

    def acquire():
        with bundle._derived_lock:
            acquired.set()

    waiter = threading.Thread(target=acquire)
    waiter.start(); waiter.join(timeout=1)
    assert acquired.is_set(), "cancelled content-index build must release the derived lock"
    cancel.clear()
    assert retrieval.RetrievalService(bundle).search(retrieval.SearchRequest("needle"))


def test_cancelled_cold_graph_does_not_publish_and_retry_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval
    from okf_loom.model import Concept

    root = _bundle(tmp_path)
    for index in range(80):
        (root / f"extra-{index}.md").write_text(
            f"---\ntype: Guide\ntitle: Extra {index}\n---\n[guide](/guide.md)\n", encoding="utf-8"
        )
    bundle = Bundle.load(root)
    cancel = threading.Event()
    original = Concept.links
    calls = 0

    def cancelling(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            cancel.set()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Concept, "links", cancelling)
    retrieval.set_retrieval_cancel_event(cancel)
    try:
        with pytest.raises(retrieval.RetrievalContractError) as exc:
            bundle.graph(cancel_check=retrieval.check_retrieval_cancelled)
        assert exc.value.code == "retrieval_timeout"
        assert bundle._graph is None
    finally:
        retrieval.set_retrieval_cancel_event(None)
    cancel.clear()
    assert bundle.graph(cancel_check=retrieval.check_retrieval_cancelled).nodes


def test_cancellation_during_revision_hash_does_not_publish_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval

    root = _bundle(tmp_path)
    (root / "guide.md").write_text(
        "---\ntype: Guide\ntitle: Large\n---\n" + ("body " * 50000), encoding="utf-8"
    )
    bundle = Bundle.load(root)
    cancel = threading.Event()
    real_sha256 = retrieval.hashlib.sha256

    class CancellingDigest:
        def __init__(self, initial=b""):
            self.inner = real_sha256(initial)
            self.updates = 0

        def update(self, value):
            self.inner.update(value)
            self.updates += 1
            if self.updates == 2:
                cancel.set()

        def hexdigest(self):
            return self.inner.hexdigest()

    monkeypatch.setattr(retrieval.hashlib, "sha256", CancellingDigest)
    retrieval.set_retrieval_cancel_event(cancel)
    try:
        with pytest.raises(retrieval.RetrievalContractError) as exc:
            retrieval.bundle_revision(bundle)
        assert exc.value.code == "retrieval_timeout"
        assert getattr(bundle, "_bundle_revision", None) is None
    finally:
        retrieval.set_retrieval_cancel_event(None)


def test_cancellation_during_context_capping_stops_next_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval

    service = retrieval.RetrievalService(Bundle.load(_bundle(tmp_path)))
    cancel = threading.Event()
    original = retrieval.canonical_json_bytes
    calls = 0

    def cancel_after_first(value):
        nonlocal calls
        if isinstance(value, dict) and "items" in value and "budget" in value:
            calls += 1
        if calls == 1:
            cancel.set()
            return b"x" * (retrieval.MAX_RESPONSE_BYTES + 1)
        return original(value)

    monkeypatch.setattr(retrieval, "canonical_json_bytes", cancel_after_first)
    retrieval.set_retrieval_cancel_event(cancel)
    try:
        with pytest.raises(retrieval.RetrievalContractError) as exc:
            service.context(retrieval.ContextRequest("alpha"))
        assert exc.value.code == "retrieval_timeout" and calls == 1
    finally:
        retrieval.set_retrieval_cancel_event(None)


def test_cancelled_passage_build_never_publishes_derived_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval

    bundle = Bundle.load(_bundle(tmp_path))
    cancel = threading.Event()
    original = retrieval.derive_passages

    def cancel_before_publish(target):
        built = original(target)
        cancel.set()
        return built

    monkeypatch.setattr(retrieval, "derive_passages", cancel_before_publish)
    retrieval.set_retrieval_cancel_event(cancel)
    try:
        with pytest.raises(retrieval.RetrievalContractError):
            _ = retrieval.RetrievalService(bundle).passages
        assert getattr(bundle, "_passage_index", None) is None
    finally:
        retrieval.set_retrieval_cancel_event(None)


@pytest.mark.parametrize("mode", ["lexical", "semantic"])
@pytest.mark.parametrize("mutation", ["reload", "invalidate"])
def test_search_backend_publication_cannot_cross_bundle_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, mutation: str
) -> None:
    from okf_loom.retrieval import RetrievalService, SearchRequest
    from okf_loom.search import LexicalBackend, SemanticLiteBackend

    root = _bundle(tmp_path)
    path = root / "guide.md"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "title: Retrieval", "title: oldneedle"
    ).replace("Exact alpha", "oldneedle alpha"), encoding="utf-8")
    bundle = Bundle.load(root)
    backend_type = LexicalBackend if mode == "lexical" else SemanticLiteBackend
    original_index = backend_type.index
    entered = threading.Event(); release = threading.Event()

    def blocked_index(self, content_index):
        entered.set(); release.wait(timeout=5)
        return original_index(self, content_index)

    monkeypatch.setattr(backend_type, "index", blocked_index)
    search_thread = threading.Thread(target=lambda: RetrievalService(bundle).search(
        SearchRequest("oldneedle", mode=mode)
    ))
    search_thread.start(); assert entered.wait(timeout=2)
    if mutation == "reload":
        path.write_text(path.read_text(encoding="utf-8").replace("oldneedle", "newneedle"),
                        encoding="utf-8")
        mutate = bundle.reload
    else:
        concept = bundle.concepts[("guide",)]
        concept.frontmatter["title"] = "newneedle"
        concept.body = concept.body.replace("oldneedle", "newneedle")
        mutate = bundle.invalidate
    mutation_thread = threading.Thread(target=mutate)
    mutation_thread.start(); time.sleep(0.03)
    assert mutation_thread.is_alive(), "generation mutation must wait for in-flight publication"
    release.set(); search_thread.join(timeout=5); mutation_thread.join(timeout=5)
    results = RetrievalService(bundle).search(SearchRequest("newneedle", mode=mode))
    assert results and results[0].concept_id == ("guide",)


@pytest.mark.parametrize("mutation", ["reload", "invalidate"])
def test_passage_publication_cannot_cross_bundle_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    import okf_loom.retrieval as retrieval

    root = _bundle(tmp_path)
    path = root / "guide.md"
    bundle = Bundle.load(root)
    original = retrieval.derive_passages
    entered = threading.Event(); release = threading.Event()

    def blocked(target):
        entered.set(); release.wait(timeout=5)
        return original(target)

    monkeypatch.setattr(retrieval, "derive_passages", blocked)
    worker = threading.Thread(target=lambda: retrieval.RetrievalService(bundle).passages)
    worker.start(); assert entered.wait(timeout=2)
    if mutation == "reload":
        path.write_text(path.read_text(encoding="utf-8").replace("alpha", "freshpassage"),
                        encoding="utf-8")
        mutate = bundle.reload
    else:
        bundle.concepts[("guide",)].body = bundle.concepts[("guide",)].body.replace(
            "alpha", "freshpassage"
        )
        mutate = bundle.invalidate
    mutation_thread = threading.Thread(target=mutate)
    mutation_thread.start(); time.sleep(0.03); assert mutation_thread.is_alive()
    release.set(); worker.join(timeout=5); mutation_thread.join(timeout=5)
    assert any("freshpassage" in passage.excerpt
               for passage in retrieval.RetrievalService(bundle).passages)


@pytest.mark.parametrize("mutation", ["reload", "invalidate"])
def test_revision_publication_cannot_cross_bundle_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    import okf_loom.retrieval as retrieval

    root = _bundle(tmp_path)
    path = root / "guide.md"
    bundle = Bundle.load(root)
    old_revision = retrieval.bundle_revision(Bundle.load(root))
    original_sha256 = retrieval.hashlib.sha256
    entered = threading.Event(); release = threading.Event()

    def blocked_sha256(*args, **kwargs):
        entered.set(); release.wait(timeout=5)
        return original_sha256(*args, **kwargs)

    monkeypatch.setattr(retrieval.hashlib, "sha256", blocked_sha256)
    worker = threading.Thread(target=lambda: retrieval.bundle_revision(bundle))
    worker.start(); assert entered.wait(timeout=2)
    if mutation == "reload":
        path.write_text(path.read_text(encoding="utf-8").replace("alpha", "freshrevision"),
                        encoding="utf-8")
        mutate = bundle.reload
    else:
        bundle.concepts[("guide",)].body = bundle.concepts[("guide",)].body.replace(
            "alpha", "freshrevision"
        )
        mutate = bundle.invalidate
    mutation_thread = threading.Thread(target=mutate)
    mutation_thread.start(); time.sleep(0.03); assert mutation_thread.is_alive()
    release.set(); worker.join(timeout=5); mutation_thread.join(timeout=5)
    monkeypatch.setattr(retrieval.hashlib, "sha256", original_sha256)
    current = retrieval.bundle_revision(bundle)
    if mutation == "reload":
        assert current == retrieval.bundle_revision(Bundle.load(root))
    else:
        assert current != old_revision
        assert "freshrevision" in bundle.concepts[("guide",)].raw_text


def test_corpus_loader_rejects_partial_mixed_duplicate_and_hash_mismatch(tmp_path: Path) -> None:
    from okf_loom.retrieval import (RetrievalContractError, build_passage_corpus,
                                    load_passage_corpus)

    artifact = build_passage_corpus(Bundle.load(_bundle(tmp_path)), chunk_size=1)
    assert load_passage_corpus(artifact)
    variants = []
    partial = copy.deepcopy(artifact)
    partial["chunks"].pop()
    variants.append(partial)
    mixed = copy.deepcopy(artifact)
    mixed["chunks"][0]["bundle_revision"] = "other"
    variants.append(mixed)
    duplicate = copy.deepcopy(artifact)
    if len(duplicate["chunks"]) > 1:
        duplicate["chunks"][1]["id"] = duplicate["chunks"][0]["id"]
    else:
        duplicate["manifest"]["chunks"].append(copy.deepcopy(duplicate["manifest"]["chunks"][0]))
        duplicate["chunks"].append(copy.deepcopy(duplicate["chunks"][0]))
        duplicate["manifest"]["chunk_count"] += 1
    variants.append(duplicate)
    corrupt = copy.deepcopy(artifact)
    corrupt["chunks"][0]["passages"][0]["excerpt"] += "tampered"
    variants.append(corrupt)
    unsupported = copy.deepcopy(artifact)
    unsupported["manifest"]["schema_version"] = 999
    variants.append(unsupported)
    duplicate_file = copy.deepcopy(artifact)
    if len(duplicate_file["manifest"]["chunks"]) > 1:
        duplicate_file["manifest"]["chunks"][1]["file"] = duplicate_file["manifest"]["chunks"][0]["file"]
        variants.append(duplicate_file)
    for value in variants:
        with pytest.raises(RetrievalContractError):
            load_passage_corpus(value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda artifact: artifact.__setitem__("extra", "value"),
        lambda artifact: artifact.pop("chunks"),
        lambda artifact: artifact.__setitem__("\ud800", "value"),
        lambda artifact: artifact.__setitem__("extra", "\ud800"),
    ],
)
def test_corpus_loader_requires_exact_top_level_schema_with_unicode_safe_error(
    tmp_path: Path, mutate
) -> None:
    import okf_loom.retrieval as retrieval

    artifact = retrieval.build_passage_corpus(Bundle.load(_bundle(tmp_path)), chunk_size=100)
    mutate(artifact)
    with pytest.raises(retrieval.RetrievalContractError) as exc:
        retrieval.load_passage_corpus(artifact)
    assert exc.value.code == "invalid_corpus"


def test_corpus_loader_rejects_reversed_distinct_same_prefix_passages(
    tmp_path: Path,
) -> None:
    import okf_loom.retrieval as retrieval

    artifact = retrieval.build_passage_corpus(Bundle.load(_bundle(tmp_path)), chunk_size=100)
    chunk = artifact["chunks"][0]
    base = copy.deepcopy(chunk["passages"][0])
    line = base["line_start"]

    def span(end: int) -> dict:
        passage = copy.deepcopy(base)
        passage["line_end"] = line
        passage["char_start"] = 0
        passage["char_end"] = end
        passage["excerpt"] = "x" * end
        suffix = f"@C0-C{end}"
        passage["passage_id"] = f"{passage['concept_id']}:L{line}-L{line}{suffix}"
        passage["citation"] = f"[{passage['path']}#L{line}-L{line}{suffix}]"
        return passage

    # Both rows shared the old ordering prefix. Canonical order is now the
    # shorter char_end first; deliberately reverse it and recompute identity.
    chunk["passages"] = [span(2), span(1)]
    chunk["count"] = 2
    raw = retrieval.canonical_json_bytes(chunk["passages"])
    digest = hashlib.sha256(raw).hexdigest()
    chunk["hash"] = digest
    chunk["id"] = retrieval.corpus_chunk_identity(
        chunk["bundle_revision"], chunk["index"], content_hash=digest
    )
    descriptor = artifact["manifest"]["chunks"][0]
    descriptor.update(
        count=2, hash=digest, id=chunk["id"],
        file=f"passages-{chunk['index']:06d}-{digest[:16]}.json",
    )
    artifact["manifest"]["passage_count"] = 2
    artifact["manifest"]["chunk_count"] = 1
    artifact["manifest"]["chunks"] = [descriptor]
    artifact["chunks"] = [chunk]

    with pytest.raises(retrieval.RetrievalContractError) as exc:
        retrieval.load_passage_corpus(artifact)
    assert exc.value.code == "invalid_corpus"


def test_corpus_loader_rejects_declared_bounds_before_passage_serialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval

    artifact = retrieval.build_passage_corpus(Bundle.load(_bundle(tmp_path)), chunk_size=1)
    oversized = copy.deepcopy(artifact)
    oversized["manifest"]["passage_count"] = retrieval.MAX_BUNDLE_PASSAGES + 1
    serialized = 0

    def forbidden(_value):
        nonlocal serialized
        serialized += 1
        raise AssertionError("oversized declarations must fail before serialization")

    monkeypatch.setattr(retrieval, "canonical_json_bytes", forbidden)
    with pytest.raises(retrieval.RetrievalContractError) as exc:
        retrieval.load_passage_corpus(oversized)
    assert exc.value.code == "corpus_too_large" and exc.value.status == 413
    assert serialized == 0


def test_corpus_loader_cancels_validation_without_partial_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval

    root = _bundle(tmp_path)
    for index in range(80):
        (root / f"extra-{index}.md").write_text(
            f"---\ntype: Guide\ntitle: Extra {index}\n---\nbody {index}\n", encoding="utf-8"
        )
    artifact = retrieval.build_passage_corpus(Bundle.load(root), chunk_size=100)
    cancel = threading.Event()
    original = retrieval._validate_corpus_passage
    validated = 0

    def cancelling(passage, seen, **kwargs):
        nonlocal validated
        validated += 1
        if validated == 2:
            cancel.set()
        return original(passage, seen, **kwargs)

    monkeypatch.setattr(retrieval, "_validate_corpus_passage", cancelling)
    retrieval.set_retrieval_cancel_event(cancel)
    try:
        with pytest.raises(retrieval.RetrievalContractError) as exc:
            retrieval.load_passage_corpus(artifact)
        assert exc.value.code == "retrieval_timeout"
    finally:
        retrieval.set_retrieval_cancel_event(None)


def test_corpus_loader_rejects_cumulative_chunk_bytes_before_monolithic_serialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval

    root = _bundle(tmp_path)
    for index in range(40):
        (root / f"extra-{index}.md").write_text(
            f"---\ntype: Guide\ntitle: Extra {index}\n---\n" + ("evidence " * 180) + "\n",
            encoding="utf-8",
        )
    artifact = retrieval.build_passage_corpus(Bundle.load(root), chunk_size=1000)
    first_size = len(retrieval.canonical_json_bytes(artifact["chunks"][0]["passages"][0]))
    monkeypatch.setattr(retrieval, "MAX_RESPONSE_BYTES", first_size * 3)
    original = retrieval.canonical_json_bytes
    serialized_lists = 0

    def counted(value):
        nonlocal serialized_lists
        if isinstance(value, list):
            serialized_lists += 1
        return original(value)

    monkeypatch.setattr(retrieval, "canonical_json_bytes", counted)
    with pytest.raises(retrieval.RetrievalContractError) as exc:
        retrieval.load_passage_corpus(artifact)
    assert exc.value.code == "corpus_too_large" and exc.value.status == 413
    assert serialized_lists == 0, "loader must not serialize the monolithic passage list"


def test_corpus_loader_cancels_during_cumulative_encoding_without_partial_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval

    root = _bundle(tmp_path)
    for index in range(4):
        (root / f"extra-{index}.md").write_text(
            f"---\ntype: Guide\ntitle: Extra {index}\n---\nbody {index}\n", encoding="utf-8"
        )
    artifact = retrieval.build_passage_corpus(Bundle.load(root), chunk_size=100)
    cancel = threading.Event()
    original = retrieval.canonical_json_bytes
    encoded = 0

    def cancelling(value):
        nonlocal encoded
        result = original(value)
        if isinstance(value, dict) and "passage_id" in value:
            encoded += 1
            if encoded == 2:
                cancel.set()
        return result

    monkeypatch.setattr(retrieval, "canonical_json_bytes", cancelling)
    retrieval.set_retrieval_cancel_event(cancel)
    try:
        with pytest.raises(retrieval.RetrievalContractError) as exc:
            retrieval.load_passage_corpus(artifact)
        assert exc.value.code == "retrieval_timeout" and encoded == 2
    finally:
        retrieval.set_retrieval_cancel_event(None)


def test_corpus_loader_bounds_huge_nested_projection_before_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval

    artifact = retrieval.build_passage_corpus(Bundle.load(_bundle(tmp_path)), chunk_size=100)
    artifact["chunks"][0]["passages"][0]["provenance"] = {
        "nested": {"value": "x" * (retrieval.PUBLIC_METADATA_STRING_CODEPOINTS + 1)}
    }
    encoded_passages = 0
    original = retrieval.canonical_json_bytes

    def counted(value):
        nonlocal encoded_passages
        if isinstance(value, dict) and "passage_id" in value:
            encoded_passages += 1
        return original(value)

    monkeypatch.setattr(retrieval, "canonical_json_bytes", counted)
    with pytest.raises(retrieval.RetrievalContractError) as exc:
        retrieval.load_passage_corpus(artifact)
    assert exc.value.code == "invalid_corpus_passage"
    assert encoded_passages == 0


def test_corpus_loader_corpus_wide_bytes_reject_and_exact_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval

    artifact = retrieval.build_passage_corpus(Bundle.load(_bundle(tmp_path)), chunk_size=1)
    assert len(artifact["chunks"]) > 1
    exact = sum(len(retrieval.canonical_json_bytes(chunk["passages"]))
                for chunk in artifact["chunks"])
    monkeypatch.setattr(retrieval, "MAX_CORPUS_CANONICAL_BYTES", exact - 1)
    with pytest.raises(retrieval.RetrievalContractError) as exc:
        retrieval.load_passage_corpus(artifact)
    assert exc.value.code == "corpus_too_large" and exc.value.status == 413
    monkeypatch.setattr(retrieval, "MAX_CORPUS_CANONICAL_BYTES", exact)
    assert len(retrieval.load_passage_corpus(artifact)) == artifact["manifest"]["passage_count"]


@pytest.mark.parametrize("bad_index", ["1", 1.0, True, None])
def test_corpus_loader_malformed_mixed_chunk_indices_stay_in_contract(
    tmp_path: Path, bad_index: object
) -> None:
    import okf_loom.retrieval as retrieval

    artifact = retrieval.build_passage_corpus(Bundle.load(_bundle(tmp_path)), chunk_size=1)
    artifact["chunks"][-1]["index"] = bad_index
    with pytest.raises(retrieval.RetrievalContractError) as exc:
        retrieval.load_passage_corpus(artifact)
    assert exc.value.code == "invalid_corpus"


@pytest.mark.parametrize("key", ["k" * 1025, "🧭" * 1024])
def test_corpus_loader_rejects_projection_keys_by_char_and_byte_bound(
    tmp_path: Path, key: str
) -> None:
    import okf_loom.retrieval as retrieval

    artifact = retrieval.build_passage_corpus(Bundle.load(_bundle(tmp_path)), chunk_size=100)
    artifact["chunks"][0]["passages"][0]["provenance"] = {key: "value"}
    with pytest.raises(retrieval.RetrievalContractError) as exc:
        retrieval.load_passage_corpus(artifact)
    assert exc.value.code == "invalid_corpus_passage"


@pytest.mark.parametrize("location", ["projection_key", "projection_value", "passage_title"])
def test_corpus_loader_rejects_lone_surrogates_with_stable_error(
    tmp_path: Path, location: str
) -> None:
    import okf_loom.retrieval as retrieval

    artifact = retrieval.build_passage_corpus(Bundle.load(_bundle(tmp_path)), chunk_size=100)
    passage = artifact["chunks"][0]["passages"][0]
    surrogate = "\ud800"
    if location == "projection_key":
        passage["provenance"] = {surrogate: "value"}
    elif location == "projection_value":
        passage["provenance"] = {"key": surrogate}
    else:
        passage["title"] = surrogate
    with pytest.raises(retrieval.RetrievalContractError) as exc:
        retrieval.load_passage_corpus(artifact)
    assert exc.value.code == "invalid_corpus_passage"


@pytest.mark.parametrize("value", [{"key": "\ud800"}, {"\ud800": "value"}, "\ud800"])
def test_canonical_serializer_wraps_lone_surrogates(value: object) -> None:
    import okf_loom.retrieval as retrieval

    with pytest.raises(retrieval.RetrievalContractError) as exc:
        retrieval.canonical_json_bytes(value)
    assert exc.value.code == "invalid_json_value"


def test_corpus_loader_cancels_inside_recursive_projection_without_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval

    artifact = retrieval.build_passage_corpus(Bundle.load(_bundle(tmp_path)), chunk_size=100)
    nested: object = "leaf"
    for _ in range(4):
        nested = {f"k{i}": copy.deepcopy(nested) for i in range(8)}
    artifact["chunks"][0]["passages"][0]["provenance"] = nested
    cancel = threading.Event()
    original_checkpoint = retrieval._projection_checkpoint
    recursive_depths: list[int] = []
    passage_encodes = 0
    original_json = retrieval.canonical_json_bytes

    def controlled_checkpoint(work_state, depth):
        if depth > 0:
            recursive_depths.append(depth)
        if depth >= 2:
            cancel.set()
            retrieval.check_retrieval_cancelled()
        return original_checkpoint(work_state, depth)

    def counted_json(value):
        nonlocal passage_encodes
        if isinstance(value, dict) and "passage_id" in value:
            passage_encodes += 1
        return original_json(value)

    monkeypatch.setattr(retrieval, "_projection_checkpoint", controlled_checkpoint)
    monkeypatch.setattr(retrieval, "canonical_json_bytes", counted_json)
    retrieval.set_retrieval_cancel_event(cancel)
    try:
        with pytest.raises(retrieval.RetrievalContractError) as exc:
            retrieval.load_passage_corpus(artifact)
        assert exc.value.code == "retrieval_timeout"
        assert recursive_depths and max(recursive_depths) >= 2
        assert passage_encodes == 0
    finally:
        retrieval.set_retrieval_cancel_event(None)


def test_corpus_loader_shared_projection_work_budget_exhaustion_has_no_partial_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval

    artifact = retrieval.build_passage_corpus(Bundle.load(_bundle(tmp_path)), chunk_size=100)
    assert artifact["manifest"]["passage_count"] > 1
    # Each otherwise-trivial passage consumes two projection nodes
    # (provenance + citations). Three nodes fit one passage but not the corpus.
    monkeypatch.setattr(retrieval, "MAX_CORPUS_PROJECTION_NODES", 3)
    with pytest.raises(retrieval.RetrievalContractError) as exc:
        retrieval.load_passage_corpus(artifact)
    assert exc.value.code == "corpus_too_large" and exc.value.status == 413


def test_corpus_loader_strict_passage_schema_identity_and_canonical_order(tmp_path: Path) -> None:
    from okf_loom.retrieval import (RetrievalContractError, build_passage_corpus,
                                    corpus_chunk_identity, load_passage_corpus)

    artifact = build_passage_corpus(Bundle.load(_bundle(tmp_path)), chunk_size=1)

    def rehash(value, chunk_index=0):
        chunk = value["chunks"][chunk_index]
        raw = json.dumps(chunk["passages"], ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        chunk["hash"] = digest
        chunk["id"] = corpus_chunk_identity(chunk["bundle_revision"], chunk["index"], chunk["passages"])
        descriptor = value["manifest"]["chunks"][chunk["index"]]
        descriptor.update(hash=digest, id=chunk["id"],
                          file=f"passages-{chunk['index']:06d}-{digest[:16]}.json",
                          count=len(chunk["passages"]))

    unsupported = copy.deepcopy(artifact)
    unsupported["manifest"]["algorithm_version"] = "other"
    with pytest.raises(RetrievalContractError):
        load_passage_corpus(unsupported)

    mutations = [
        lambda p: p.__setitem__("passage_id", "bogus"),
        lambda p: p.__setitem__("citation", "[bogus]"),
        lambda p: p.__setitem__("line_start", 0),
        lambda p: p.__setitem__("line_end", 0),
        lambda p: p.__setitem__("char_start", 5),
        lambda p: p.__setitem__("title", 42),
        lambda p: p.__setitem__("path", "/absolute.md"),
    ]
    for mutate in mutations:
        value = copy.deepcopy(artifact)
        mutate(value["chunks"][0]["passages"][0])
        rehash(value)
        with pytest.raises(RetrievalContractError):
            load_passage_corpus(value)

    duplicate = copy.deepcopy(artifact)
    duplicate["chunks"][0]["passages"].append(
        copy.deepcopy(duplicate["chunks"][0]["passages"][0])
    )
    duplicate["chunks"][0]["count"] += 1
    duplicate["manifest"]["passage_count"] += 1
    rehash(duplicate)
    with pytest.raises(RetrievalContractError):
        load_passage_corpus(duplicate)

    reversed_chunks = copy.deepcopy(artifact)
    reversed_chunks["chunks"].reverse()
    assert load_passage_corpus(reversed_chunks) == load_passage_corpus(artifact)


@pytest.mark.parametrize(
    ("location", "value"),
    [
        ("manifest.schema_version", True), ("manifest.schema_version", 1.0),
        ("manifest.passage_count", True), ("manifest.passage_count", 1.0),
        ("manifest.chunk_count", True), ("manifest.chunk_count", 1.0),
        ("descriptor.index", False), ("descriptor.index", 0.0),
        ("descriptor.count", True), ("descriptor.count", 1.0),
        ("chunk.schema_version", True), ("chunk.schema_version", 1.0),
        ("chunk.index", False), ("chunk.index", 0.0),
        ("chunk.count", True), ("chunk.count", 1.0),
    ],
)
def test_corpus_loader_rejects_boolean_and_float_integer_metadata(
    tmp_path: Path, location: str, value: object
) -> None:
    from okf_loom.retrieval import (RetrievalContractError, build_passage_corpus,
                                    load_passage_corpus)

    root = tmp_path / "integer-corpus"
    root.mkdir()
    (root / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (root / "only.md").write_text("---\ntype: Guide\n---\nbody\n", encoding="utf-8")
    artifact = build_passage_corpus(Bundle.load(root), chunk_size=10)
    assert artifact["manifest"]["passage_count"] == 1
    target, field = location.split(".")
    if target == "manifest":
        artifact["manifest"][field] = value
    elif target == "descriptor":
        artifact["manifest"]["chunks"][0][field] = value
    else:
        artifact["chunks"][0][field] = value
    with pytest.raises(RetrievalContractError):
        load_passage_corpus(artifact)


def test_corpus_rebuild_changes_revision_and_excludes_runtime_secrets(tmp_path: Path) -> None:
    from okf_loom.retrieval import build_passage_corpus

    root = _bundle(tmp_path)
    path = root / "guide.md"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "tags: [search]", "tags: [search]\nprovenance:\n  token: authored-public"
    ), encoding="utf-8")
    before = build_passage_corpus(Bundle.load(root))
    path.write_text(path.read_text(encoding="utf-8").replace("alpha", "changed"), encoding="utf-8")
    after = build_passage_corpus(Bundle.load(root))
    rendered = json.dumps(after, sort_keys=True)
    assert before["manifest"]["bundle_revision"] != after["manifest"]["bundle_revision"]
    assert "authored-public" in rendered
    assert str(root.resolve()) not in rendered
    for forbidden in ("session_dir", "csrf", "comments", "private_config", "runtime-secret"):
        assert forbidden not in rendered


def test_public_exports_and_aggregate_work_caps(tmp_path: Path, monkeypatch) -> None:
    import okf_loom
    import okf_loom.retrieval as retrieval

    assert okf_loom.build_passage_corpus and okf_loom.load_passage_corpus
    assert retrieval.MAX_BUNDLE_CONCEPTS >= 100_000
    assert retrieval.MAX_BUNDLE_PASSAGES >= 250_000
    bundle = Bundle.load(_bundle(tmp_path))
    monkeypatch.setattr(retrieval, "MAX_BUNDLE_CONCEPTS", 0)
    with pytest.raises(retrieval.RetrievalContractError) as exc:
        retrieval.RetrievalService(bundle).passages
    assert exc.value.status == 413 and exc.value.code == "bundle_too_large"


def test_aggregate_caps_run_before_public_search_backend_indexing(tmp_path: Path, monkeypatch) -> None:
    import okf_loom.retrieval as retrieval
    from okf_loom.search import LexicalBackend, SearchMode, search_bundle

    bundle = Bundle.load(_bundle(tmp_path))
    indexed = False

    def forbidden_index(self, content_index):
        nonlocal indexed
        indexed = True
        raise AssertionError("backend indexing must not start")

    monkeypatch.setattr(retrieval, "MAX_BUNDLE_CONCEPTS", 0)
    monkeypatch.setattr(LexicalBackend, "index", forbidden_index)
    with pytest.raises(retrieval.RetrievalContractError) as exc:
        search_bundle(bundle, "alpha", mode=SearchMode.LEXICAL)
    assert exc.value.code == "bundle_too_large" and indexed is False


def test_context_envelope_impossible_under_response_cap_returns_413(tmp_path: Path, monkeypatch) -> None:
    import okf_loom.retrieval as retrieval

    monkeypatch.setattr(retrieval, "MAX_RESPONSE_BYTES", 10)
    with pytest.raises(retrieval.RetrievalContractError) as exc:
        retrieval.RetrievalService(Bundle.load(_bundle(tmp_path))).context(
            retrieval.ContextRequest("alpha")
        )
    assert exc.value.status == 413 and exc.value.code == "response_too_large"


def _selection_bundle(tmp_path: Path) -> Path:
    root = tmp_path / "selection-bundle"
    root.mkdir()
    (root / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (root / "guide.md").write_text(
        "---\ntype: Guide\ntitle: Selection Guide\n---\n"
        "# One\nbefore unique selected quote after\n"
        "# Two\nother selected quote tail\n",
        encoding="utf-8",
    )
    return root


def test_exact_selection_disambiguates_with_context_and_has_exact_identity(tmp_path: Path) -> None:
    from okf_loom.retrieval import ContextRequest, RetrievalService

    service = RetrievalService(Bundle.load(_selection_bundle(tmp_path)))
    pack = service.context(ContextRequest(
        query="selected quote", concept="guide", mode="lexical",
        selection_exact="selected quote", selection_prefix="unique ",
        selection_suffix=" after",
    ))
    selection = pack["selection"]
    assert selection == {
        "status": "attached_exact", "exact": "selected quote",
        "prefix": "unique ", "suffix": " after", "heading": None,
        "passage_id": "guide:L6-L6", "citation": "[guide.md#L6-L6@C14-C28]",
        "line_start": 6, "line_end": 6, "char_start": 14, "char_end": 28,
    }
    assert pack["items"][0]["passage_id"] == selection["passage_id"]
    markdown = pack["representations"]["markdown"]
    assert markdown == service.context_markdown(pack)
    assert markdown.index("## Selected text") < markdown.index("## [guide.md#L6-L6]")
    assert "selected quote" in markdown


def test_exact_selection_heading_preference_and_zero_silent_wrong_attachment(tmp_path: Path) -> None:
    from okf_loom.retrieval import ContextRequest, RetrievalContractError, RetrievalService

    service = RetrievalService(Bundle.load(_selection_bundle(tmp_path)))
    attached = service.context(ContextRequest(
        query="selected quote", concept="guide", selection_exact="selected quote",
        selection_heading="Two",
    ))
    assert attached["selection"]["line_start"] == 8
    with pytest.raises(RetrievalContractError) as ambiguous:
        service.context(ContextRequest(
            query="selected quote", concept="guide", selection_exact="selected quote",
        ))
    assert ambiguous.value.code == "selection_ambiguous" and ambiguous.value.status == 409
    with pytest.raises(RetrievalContractError) as absent:
        service.context(ContextRequest(
            query="missing", concept="guide", selection_exact="not present",
        ))
    assert absent.value.code == "selection_not_found" and absent.value.status == 400


def test_selection_crossing_passage_chunk_boundary_fails_closed(tmp_path: Path) -> None:
    from okf_loom.retrieval import ContextRequest, RetrievalContractError, RetrievalService

    root = tmp_path / "split-selection"
    root.mkdir()
    (root / "index.md").write_text("# Bundle\n", encoding="utf-8")
    (root / "guide.md").write_text(
        "---\ntype: Guide\n---\n# Long\n" + ("a" * 1999) + "XYZ" + ("b" * 10),
        encoding="utf-8",
    )
    with pytest.raises(RetrievalContractError) as exc:
        RetrievalService(Bundle.load(root)).context(ContextRequest(
            query="XYZ", concept="guide", selection_exact="XYZ",
        ))
    assert exc.value.code == "selection_not_found"


def test_exact_selection_budget_is_authoritative_and_never_silently_dropped(tmp_path: Path) -> None:
    from okf_loom.retrieval import ContextRequest, RetrievalContractError, RetrievalService

    service = RetrievalService(Bundle.load(_selection_bundle(tmp_path)))
    request = dict(query="selected quote", concept="guide", mode="lexical", limit=1,
                   selection_exact="selected quote", selection_heading="One")
    roomy = service.context(ContextRequest(**request, budget_tokens=50000))
    exact_budget = roomy["budget"]["estimated_tokens"]
    assert service.context(ContextRequest(**request, budget_tokens=exact_budget))["selection"]["status"] == "attached_exact"
    with pytest.raises(RetrievalContractError) as too_small:
        service.context(ContextRequest(**request, budget_tokens=max(128, exact_budget - 1)))
    assert too_small.value.code == "budget_too_small"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"selection_exact": "text"},
        {"concept": "guide", "selection_prefix": "prefix"},
        {"concept": "guide", "selection_exact": ""},
        {"concept": "guide", "selection_exact": "x" * 4097},
        {"concept": "guide", "selection_exact": "text", "selection_prefix": "x" * 513},
        {"concept": "guide", "selection_exact": "text", "selection_suffix": "x" * 513},
        {"concept": "guide", "selection_exact": "text", "selection_heading": "x" * 257},
        {"concept": "guide", "selection_exact": "text\u0085control"},
    ],
)
def test_selection_input_combinations_lengths_and_controls_fail_closed(
    tmp_path: Path, kwargs: dict[str, str]
) -> None:
    from okf_loom.retrieval import ContextRequest, RetrievalContractError, RetrievalService

    service = RetrievalService(Bundle.load(_selection_bundle(tmp_path)))
    with pytest.raises(RetrievalContractError) as exc:
        service.context(ContextRequest(query="text", **kwargs))
    assert exc.value.code == "invalid_selection"


def test_query_equal_to_quote_without_selection_remains_ordinary_context(tmp_path: Path) -> None:
    from okf_loom.retrieval import ContextRequest, RetrievalService

    pack = RetrievalService(Bundle.load(_selection_bundle(tmp_path))).context(
        ContextRequest(query="selected quote", concept="guide", mode="lexical")
    )
    assert "selection" not in pack
    assert pack["representations"]["markdown"]


def test_http_selection_json_contains_canonical_markdown_and_matches_markdown_endpoint(
    tmp_path: Path,
) -> None:
    server, thread, port = _serve(Bundle.load(_selection_bundle(tmp_path)))
    params = {
        "q": "selected quote", "concept": "guide",
        "selection_exact": "selected quote", "selection_prefix": "unique ",
        "selection_suffix": " after",
    }
    encoded = urllib.parse.urlencode(params)
    base = f"http://127.0.0.1:{port}/__context?{encoded}"
    try:
        status, headers, body = _http(base + "&format=json")
        pack = json.loads(body)
        assert status == 200 and headers["X-OKF-Bundle-Revision"] == pack["bundle_revision"]
        canonical = pack["representations"]["markdown"]
        status, markdown_headers, markdown = _http(base + "&format=markdown")
        assert status == 200 and markdown_headers.get_content_type() == "text/markdown"
        assert markdown.decode() == canonical
        assert pack["selection"]["exact"] == "selected quote"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_http_selection_errors_keep_stable_envelopes_and_statuses(tmp_path: Path) -> None:
    server, thread, port = _serve(Bundle.load(_selection_bundle(tmp_path)))
    base = f"http://127.0.0.1:{port}/__context?format=json&q=x&concept=guide"
    try:
        status, payload = _http_json(base + "&selection_exact=selected+quote")
        assert status == 409 and payload["error"]["code"] == "selection_ambiguous"
        status, payload = _http_json(base + "&selection_exact=absent")
        assert status == 400 and payload["error"]["code"] == "selection_not_found"
        status, payload = _http_json(
            f"http://127.0.0.1:{port}/__context?format=json&q=x&selection_exact=text"
        )
        assert status == 400 and payload["error"]["code"] == "invalid_selection"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_selection_context_uses_one_snapshot_across_bundle_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okf_loom.retrieval import RetrievalService

    root = _selection_bundle(tmp_path)
    old_bundle = Bundle.load(root)
    old_revision = RetrievalService(old_bundle).revision
    path = root / "guide.md"
    path.write_text(path.read_text(encoding="utf-8").replace("selected quote", "changed quote"),
                    encoding="utf-8")
    new_bundle = Bundle.load(root)
    server, thread, port = _serve(old_bundle)
    original = RetrievalService.context

    def swap_then_context(self, request):
        server.bundle = new_bundle  # type: ignore[attr-defined]
        return original(self, request)

    monkeypatch.setattr(RetrievalService, "context", swap_then_context)
    query = urllib.parse.urlencode({
        "format": "json", "q": "selected quote", "concept": "guide",
        "selection_exact": "selected quote", "selection_heading": "One",
    })
    try:
        status, headers, body = _http(f"http://127.0.0.1:{port}/__context?{query}")
        pack = json.loads(body)
        assert status == 200 and headers["X-OKF-Bundle-Revision"] == old_revision
        assert pack["representations"]["markdown"] == RetrievalService.context_markdown(pack)
        assert "selected quote" in pack["representations"]["markdown"]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_huge_numeric_http_inputs_return_stable_400_json(tmp_path: Path) -> None:
    server, thread, port = _serve(Bundle.load(_bundle(tmp_path)))
    base = f"http://127.0.0.1:{port}"
    huge = "9" * 5000
    try:
        urls = [
            f"/__search?q=x&format=json&limit={huge}",
            f"/__context?q=x&format=json&limit={huge}",
            f"/__context?q=x&format=json&budget_tokens={huge}",
            f"/__context?q=x&format=json&neighbors={huge}",
        ]
        for suffix in urls:
            status, payload = _http_json(base + suffix)
            assert status == 400
            assert payload["error"]["code"] == "invalid_request"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_search_payload_boundaries_never_hide_oversize_as_empty(monkeypatch) -> None:
    import okf_loom.retrieval as retrieval

    rows = [{"concept_id": "a", "title": "x" * 30}]
    exact = len(retrieval.canonical_json_bytes(rows))
    monkeypatch.setattr(retrieval, "MAX_RESPONSE_BYTES", exact)
    assert retrieval.bound_search_payload(rows) == (rows, 0)
    monkeypatch.setattr(retrieval, "MAX_RESPONSE_BYTES", exact - 1)
    with pytest.raises(retrieval.RetrievalContractError) as exc:
        retrieval.bound_search_payload(rows)
    assert exc.value.status == 413
    two = rows + [{"concept_id": "b", "title": "y" * 30}]
    monkeypatch.setattr(retrieval, "MAX_RESPONSE_BYTES", exact)
    assert retrieval.bound_search_payload(two) == (rows, 1)


def test_http_search_tail_omission_header_and_single_oversize_413(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval
    from okf_loom.paths import concept_id_from_str
    from okf_loom.search import SearchResult

    server, thread, port = _serve(Bundle.load(_bundle(tmp_path)))
    row_size = len(json.dumps([SearchResult(concept_id_from_str("guide"), "x" * 60, 1.0).as_dict()]).encode())
    monkeypatch.setattr(retrieval, "MAX_RESPONSE_BYTES", row_size + 8)
    monkeypatch.setattr(retrieval.RetrievalService, "search", lambda self, request: [
        SearchResult(concept_id_from_str("guide"), "x" * 60, 2.0),
        SearchResult(concept_id_from_str("guide"), "y" * 60, 1.0),
    ])
    try:
        status, headers, body = _http(
            f"http://127.0.0.1:{port}/__search?q=x&format=json"
        )
        assert status == 200 and headers["X-OKF-Omitted-Count"] == "1"
        assert len(json.loads(body)) == 1
        monkeypatch.setattr(retrieval, "MAX_RESPONSE_BYTES", 10)
        status, _, body = _http(
            f"http://127.0.0.1:{port}/__search?q=x&format=json"
        )
        assert status == 413 and json.loads(body)["error"]["code"] == "response_too_large"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_http_json_cap_uses_exact_single_unicode_wire_serialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval
    from okf_loom.paths import concept_id_from_str
    from okf_loom.search import SearchResult

    result = SearchResult(concept_id_from_str("guide"), "🧭" * 1000, 1.0)
    expected = retrieval.canonical_json_bytes([result.as_dict()])
    monkeypatch.setattr(retrieval, "MAX_RESPONSE_BYTES", len(expected))
    monkeypatch.setattr(retrieval.RetrievalService, "search", lambda self, request: [result])
    server, thread, port = _serve(Bundle.load(_bundle(tmp_path)))
    try:
        status, headers, body = _http(
            f"http://127.0.0.1:{port}/__search?q=x&format=json"
        )
        assert status == 200
        assert body == expected
        assert int(headers["Content-Length"]) == len(expected)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_http_final_html_wire_cap_returns_typed_413(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.retrieval as retrieval
    import okf_loom.server as server_module

    monkeypatch.setattr(retrieval, "MAX_RESPONSE_BYTES", 512)
    monkeypatch.setattr(server_module, "_render_search_page", lambda *args, **kwargs: "<html>" + "🧭" * 1000 + "</html>")
    server, thread, port = _serve(Bundle.load(_bundle(tmp_path)))
    try:
        status, headers, body = _http(f"http://127.0.0.1:{port}/__search?q=alpha")
        assert status == 413
        assert len(body) == int(headers["Content-Length"]) <= retrieval.MAX_RESPONSE_BYTES
        assert b"response_too_large" in body
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_malformed_post_drains_body_and_request_target_cap_has_stable_envelope(tmp_path: Path) -> None:
    import http.client
    import okf_loom.retrieval as retrieval

    server, thread, port = _serve(Bundle.load(_bundle(tmp_path)))
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("POST", "/%ZZ", body=b'{"x":1}', headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["error"]["code"] == "malformed_encoding"
        connection.request("GET", "/__search?q=alpha&format=json")
        response = connection.getresponse()
        assert response.status == 200
        response.read()

        oversized = "/__search?q=" + ("x" * retrieval.MAX_REQUEST_TARGET_BYTES)
        connection.request("GET", oversized)
        response = connection.getresponse()
        assert response.status == 414
        assert json.loads(response.read())["error"]["code"] == "request_target_too_large"
    finally:
        connection.close()
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_strict_path_and_unicode_control_battery(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    (root / "café.png").write_bytes(b"public-media")
    server, thread, port = _serve(Bundle.load(root))
    base = f"http://127.0.0.1:{port}"
    try:
        for path in ["/%ZZ", "/%2e%2e/guide", "/guide%2Fchild", "/%5f%5fsearch",
                     "/__%73earch", "/guide%00"]:
            status, _, _ = _http(base + path)
            assert status == 400, path
        assert _http(base + "/caf%C3%A9.png")[0] == 200
        for value in ["%C2%85", "%C2%9F", "%E2%80%AE"]:
            assert _http_json(base + f"/__search?q=x&type={value}&format=json")[0] == 400
            assert _http_json(base + f"/__context?q=x&type={value}&format=json")[0] == 400
        good = urllib.parse.quote("مرحبا e\u0301 🧭")
        assert _http_json(base + f"/__search?q={good}&format=json")[0] == 200
        assert _http_json(base + "/__search?q=%E2%80%AE&format=json")[0] == 400
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_html_search_errors_are_typed_escaped_and_json_errors_stay_json(tmp_path: Path) -> None:
    server, thread, port = _serve(Bundle.load(_bundle(tmp_path)))
    base = f"http://127.0.0.1:{port}"
    try:
        status, headers, body = _http(base + "/__search?q=x&limit=0")
        assert status == 400 and headers.get_content_type() == "text/html"
        text = body.decode()
        assert 'data-okf-error-code="invalid_request"' in text
        assert str(tmp_path) not in text
        status, headers, body = _http(base + "/__search?mode=relation&target=missing")
        assert status == 404 and headers.get_content_type() == "text/html"
        # Rejected authored input stays editable, not a claimed search result.
        assert b'name="target" value="missing"' in body
        assert b'data-okf-error-code="concept_not_found"' in body
        evil = urllib.parse.quote("<script>alert(1)</script>")
        status, _, body = _http(base + f"/__search?q=x&{evil}=y")
        assert status == 400 and b"<script>" not in body and b"&lt;script&gt;" in body
        status, headers, body = _http(base + "/__search?q=x&limit=0&format=json")
        assert status == 400 and headers.get_content_type() == "application/json"
        assert set(json.loads(body)["error"]) == {"code", "message", "fields"}
        for query in [
            "%66ormat=json&unknown=x",
            "format=%6ason&bad=%ZZ",
            "format=json&format=json",
        ]:
            status, headers, body = _http(base + "/__search?" + query)
            assert status == 400 and headers.get_content_type() == "application/json"
            assert json.loads(body)["ok"] is False
        status, headers, body = _http(
            base + "/__search?format=json&format=html&unknown=x"
        )
        assert status == 400 and headers.get_content_type() == "text/html"
        assert b'duplicate_parameter' in body or b'unknown_parameter' in body
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_context_request_uses_one_bundle_snapshot_across_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okf_loom.retrieval import RetrievalService

    root = _bundle(tmp_path)
    old_bundle = Bundle.load(root)
    old_revision = RetrievalService(old_bundle).revision
    path = root / "guide.md"
    path.write_text(path.read_text(encoding="utf-8").replace("alpha", "beta"), encoding="utf-8")
    new_bundle = Bundle.load(root)
    server, thread, port = _serve(old_bundle)
    original = RetrievalService.context

    def swap_then_context(self, request):
        server.bundle = new_bundle  # type: ignore[attr-defined]
        return original(self, request)

    monkeypatch.setattr(RetrievalService, "context", swap_then_context)
    try:
        status, headers, body = _http(
            f"http://127.0.0.1:{port}/__context?q=alpha&format=json"
        )
        payload = json.loads(body)
        assert status == 200 and headers["X-OKF-Bundle-Revision"] == old_revision
        assert "alpha" in payload["items"][0]["excerpt"]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_server_retrieval_saturation_and_deadline_return_stable_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okf_loom.server as server_module
    from okf_loom.retrieval import RetrievalService

    server, thread, port = _serve(Bundle.load(_bundle(tmp_path)))
    server.retrieval_concurrency = 1  # type: ignore[attr-defined]
    entered = threading.Event()
    release = threading.Event()
    original = RetrievalService.search

    def blocked(self, request):
        entered.set()
        release.wait(timeout=5)
        return original(self, request)

    monkeypatch.setattr(RetrievalService, "search", blocked)
    first_result = []

    def first_request():
        first_result.append(_http_json(f"http://127.0.0.1:{port}/__search?q=alpha&format=json"))

    worker = threading.Thread(target=first_request)
    worker.start()
    assert entered.wait(timeout=2)
    status, payload = _http_json(f"http://127.0.0.1:{port}/__search?q=alpha&format=json")
    assert status == 503 and payload["error"]["code"] == "retrieval_busy"
    status, payload = _http_json(f"http://127.0.0.1:{port}/__context?q=alpha&format=json")
    assert status == 503 and payload["error"]["code"] == "retrieval_busy"
    release.set(); worker.join(timeout=5)
    assert first_result[0][0] == 200

    timeout_entered = threading.Event()
    timeout_release = threading.Event()

    def abandoned(self, request):
        timeout_entered.set()
        timeout_release.wait(timeout=5)  # deliberately ignores cooperative cancellation
        return original(self, request)

    monkeypatch.setattr(RetrievalService, "search", abandoned)
    monkeypatch.setattr(server_module, "RETRIEVAL_DEADLINE_SECONDS", 0.05)
    started = time.monotonic()
    status, headers, body = _http(f"http://127.0.0.1:{port}/__search?q=alpha")
    elapsed = time.monotonic() - started
    assert timeout_entered.is_set()
    assert status == 503 and headers.get_content_type() == "text/html"
    assert b'retrieval_timeout' in body
    assert b'name="q" value="alpha"' in body
    assert b'action="/__search-form"' in body
    assert 0.03 <= elapsed < 0.30, elapsed
    # Timed-out work still owns the only slot until it truly exits.
    status, payload = _http_json(f"http://127.0.0.1:{port}/__search?q=alpha&format=json")
    assert status == 503 and payload["error"]["code"] == "retrieval_busy"
    monkeypatch.setattr(RetrievalService, "search", original)
    timeout_release.set()
    deadline = time.monotonic() + 2
    while True:
        status, payload = _http_json(f"http://127.0.0.1:{port}/__search?q=alpha&format=json")
        if status == 200:
            break
        assert status == 503 and payload["error"]["code"] == "retrieval_busy"
        assert time.monotonic() < deadline
        time.sleep(0.01)
    executor = getattr(server, "_retrieval_executor", None)
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
    server.shutdown(); server.server_close(); thread.join(timeout=5)


@pytest.mark.parametrize("route", ["search", "context"])
def test_http_payload_revision_and_serialization_share_in_place_reload_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    import okf_loom.server as server_module
    from okf_loom.retrieval import RetrievalService

    root = _bundle(tmp_path)
    bundle = Bundle.load(root)
    old_revision = RetrievalService(bundle).revision
    server, thread, port = _serve(bundle)
    entered = threading.Event(); release = threading.Event()
    original = server_module.OKFWikiHandler._prepare_retrieval_json

    def blocked(self, value):
        entered.set(); release.wait(timeout=5)
        return original(self, value)

    monkeypatch.setattr(server_module.OKFWikiHandler, "_prepare_retrieval_json", blocked)
    result: list[tuple[int, object, bytes]] = []
    path = "/__search?q=alpha&format=json" if route == "search" else "/__context?q=alpha&format=json"
    request_thread = threading.Thread(
        target=lambda: result.append(_http(f"http://127.0.0.1:{port}{path}"))
    )
    request_thread.start(); assert entered.wait(timeout=2)
    concept_path = root / "guide.md"
    concept_path.write_text(
        concept_path.read_text(encoding="utf-8").replace("alpha", "freshneedle"),
        encoding="utf-8",
    )
    reload_thread = threading.Thread(target=bundle.reload)
    reload_thread.start(); time.sleep(0.03)
    assert reload_thread.is_alive(), "in-place reload must wait for response serialization"
    release.set(); request_thread.join(timeout=5); reload_thread.join(timeout=5)
    status, headers, body = result[0]
    payload = json.loads(body)
    assert status == 200 and headers["X-OKF-Bundle-Revision"] == old_revision
    assert headers["ETag"] == f'"{old_revision}"'
    if route == "context":
        assert payload["bundle_revision"] == old_revision
    assert "alpha" in body.decode("utf-8")
    assert RetrievalService(bundle).revision != old_revision
    server.shutdown(); server.server_close(); thread.join(timeout=5)


def test_http_invalid_json_value_fails_closed_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okf_loom.search import SearchResult

    server, thread, port = _serve(Bundle.load(_bundle(tmp_path)))
    monkeypatch.setattr(SearchResult, "as_dict", lambda self: {"unsupported": {"set"}})
    try:
        first = _http_json(f"http://127.0.0.1:{port}/__search?q=alpha&format=json")
        second = _http_json(f"http://127.0.0.1:{port}/__search?q=alpha&format=json")
        assert first == second
        assert first[0] == 500 and first[1]["error"]["code"] == "invalid_json_value"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


@pytest.mark.parametrize("phase", ["json", "html"])
def test_absolute_deadline_covers_response_construction_and_worker_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    import okf_loom.server as server_module

    server, thread, port = _serve(Bundle.load(_bundle(tmp_path)))
    server.retrieval_concurrency = 1  # type: ignore[attr-defined]
    monkeypatch.setattr(server_module, "RETRIEVAL_DEADLINE_SECONDS", 0.02)
    entered = threading.Event()
    if phase == "json":
        original = server_module.OKFWikiHandler._prepare_retrieval_json

        def delayed(self, value):
            if not entered.is_set():
                entered.set(); time.sleep(0.06)
            return original(self, value)

        monkeypatch.setattr(server_module.OKFWikiHandler, "_prepare_retrieval_json", delayed)
        url = f"http://127.0.0.1:{port}/__search?q=alpha&format=json"
    else:
        original_render = server_module._render_search_page

        def delayed_render(*args, **kwargs):
            if not entered.is_set():
                entered.set(); time.sleep(0.06)
            return original_render(*args, **kwargs)

        monkeypatch.setattr(server_module, "_render_search_page", delayed_render)
        url = f"http://127.0.0.1:{port}/__search?q=alpha"
    try:
        status, headers, body = _http(url)
        assert entered.is_set() and status == 503 and b"retrieval_timeout" in body
        deadline = time.monotonic() + 2
        while True:
            status, _, _ = _http(url)
            if status != 503:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert status == 200
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
