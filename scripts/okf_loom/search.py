"""Layered search for OKF bundles (see docs/research.md §A).

Three backends behind one interface, **all zero-dependency**:

    :class:`LexicalBackend`      — pure-Python BM25 with field weighting.
                                   Satisfies lexical + tag search offline.
    :class:`SemanticLiteBackend` — dependency-free fuzzy-semantic ranker
                                   (token-cosine + char-trigram cosine over a
                                   weighted concept profile). The default for
                                   ``--mode semantic``. The agent (an LLM in
                                   opencode / Claude Code / Codex) is itself
                                   the best true-semantic engine, so a local
                                   embedding backend would be redundant; this
                                   lightweight layer helps the agent find
                                   candidates cheaply without one.
    :class:`HybridBackend`       — Reciprocal Rank Fusion (k=60) of
                                   Lexical + SemanticLite.

The public entry point is :func:`search_bundle`, which the CLI ``okf search``
command calls directly. Backends consume the canonical
:class:`~okf_loom.model.ContentIndex` (the Quartz "one model" pattern) so
search, graph, and listings all read the same object.

BM25 formula (per field, then weighted-summed across fields)::

    For query terms ``t`` in field ``f`` of document ``d``:

        idf(t, f)  = ln( (N - df(t, f) + 0.5) / (df(t, f) + 0.5) + 1 )

        bm25(t, d, f) = idf(t, f) * ( tf(t, d, f) * (k1 + 1) )
                                     / ( tf(t, d, f) + k1 * (1 - b + b * dl(d, f) / avgdl(f)) )

        bm25(q, d, f) = Σ_t  bm25(t, d, f)

        score(q, d)   = Σ_f  weight(f) * bm25(q, d, f)

where:

    ``N``          total number of concepts in the corpus
    ``df(t, f)``   number of concepts whose field ``f`` contains term ``t``
    ``tf(t, d, f)``term frequency of ``t`` in field ``f`` of ``d``
    ``dl(d, f)``   token length of field ``f`` of ``d`` (0 if absent)
    ``avgdl(f)``   mean of ``dl(·, f)`` over all ``N`` concepts
    ``k1``         term-frequency saturation (default 1.5)
    ``b``          length-normalization (default 0.75)

The ``+1`` inside the ``idf`` log is the standard variant that keeps ``idf``
strictly positive (so a term appearing in every document still contributes a
small positive amount rather than going negative).

Default field weights (highest signal first):

    title (5.0) > headings (3.0) > description (2.0) > body (1.0) > tags (1.0)

Scoring is deterministic: documents are scored in sorted concept-id order and
the final ranking tie-breaks by concept id, so the same ``(bundle, query)``
always yields the same ordering.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from .aliases import alias_labels
from .model import Bundle, Concept, ContentIndex
from .parse import strip_markdown_for_search
from .paths import ConceptId, concept_id_from_str, concept_id_to_str


# ---------------------------------------------------------------------------
# Modes & result type
# ---------------------------------------------------------------------------


class SearchMode(str, Enum):
    """Selector for which backend/services :func:`search_bundle` should use."""

    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"
    TAG = "tag"
    ENTITY = "entity"
    RELATION = "relation"


@dataclass
class SearchResult:
    """A single ranked search hit.

    Attributes:
        concept_id: canonical concept id tuple (e.g. ``("tables", "users")``).
        title: display title of the concept.
        score: backend-specific relevance score (higher = better). Tag-mode
            results carry a constant score of ``1.0``.
        snippets: 0..N short excerpts (~120 chars) centred on the first
            matches; ellipsized when truncated.
        source_backend: which backend produced this hit
            (``"lexical"`` | ``"semantic-lite"`` | ``"hybrid"`` | ``"tag"``).
        matched_tags: subset of the concept's own tags that matched the query
            (populated in tag mode; empty otherwise).
    """

    concept_id: ConceptId
    title: str
    score: float
    snippets: list[str] = field(default_factory=list)
    source_backend: str = "lexical"
    matched_tags: list[str] = field(default_factory=list)
    detail: dict | None = None
    description: str = ""  # P2-52: concept description for richer result context
    backends: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    evidence: dict | None = None
    # Phase 2 search polish (plan §3.4): concept type identity (name + the
    # auto-generated type colour + the resolved icon key) so the live-
    # suggestions combobox can render a type ICON + tinted chip per result —
    # not text/colour only. The icon key is resolved via the SAME
    # :func:`type_icon_key` the renderer uses everywhere else, so the
    # suggestion chip carries exact type identity (icon + colour + name).
    # Back-filled from the content index alongside description; empty when
    # the concept is unknown.
    type: str = ""
    type_color: str = ""
    icon: str = ""
    metadata: dict | None = None

    def as_dict(self) -> dict:
        """JSON-serializable view (concept_id rendered via ``concept_id_to_str``)."""
        d = {
            "concept_id": concept_id_to_str(self.concept_id),
            "title": self.title,
            "score": self.score,
            "snippets": list(self.snippets),
            "source_backend": self.source_backend,
            "matched_tags": list(self.matched_tags),
            "description": self.description,
            "type": self.type,
            "type_color": self.type_color,
            "icon": self.icon,
        }
        if self.detail is not None:
            d["detail"] = self.detail
        if self.backends:
            d["backends"] = list(self.backends)
        if self.reasons:
            d["reasons"] = list(self.reasons)
        if self.evidence is not None:
            d["evidence"] = self.evidence
        if self.metadata is not None:
            from .retrieval import public_metadata
            d["metadata"] = public_metadata(self.metadata)
        return d


def _type_identity(type_name: str | None) -> tuple[str, str]:
    """Load presentation-only type metadata only when a result needs it."""
    from .viewer.assets import type_color_for, type_icon_key

    return type_color_for(type_name), type_icon_key(type_name)


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

# A small, dependency-free English stopword set. Deliberately modest so that
# domain terms (table, user, data, event, ...) are never stripped. Shipped
# in-tree; no nltk or similar dependency.
_STOPWORDS: frozenset[str] = frozenset(
    """
    a an and are as at be been being but by for from had has have having
    he her here hers him his how i if in into is it its itself me more most
    my no nor not of off on once only or other our out over own same she so
    some such than that the their them then there these they this those
    through to too under until up very was we were what when where which
    while who whom why will with would you your yours about above after
    again against all because before below between during each few further
    do does did doing down can could should shall may might must also via
    per within without
    """.split()
)

# Unicode word runs. ``\\w`` matches CJK ideographs too, so a run of CJK
# characters becomes a single token; the CJK fallback below splits those.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Lowercase, split on word boundaries, drop stopwords, split CJK runs.

    CJK fallback: a token that contains *no* ASCII letter/digit is treated as
    a CJK/ideographic run and is split into one-character tokens (so that
    "比特币" indexes as ``["比", "特", "币"]``). Mixed tokens like ``hello世界``
    keep ASCII letters and are left intact per the literal rule.
    """
    if not text:
        return []
    out: list[str] = []
    for raw in _WORD_RE.findall(text.lower()):
        if raw in _STOPWORDS:
            continue
        # Does this token contain any ASCII letter/digit?
        if any(c.isascii() and c.isalnum() for c in raw):
            out.append(raw)
        else:
            # Pure non-ASCII run (e.g. CJK): index per character.
            out.extend(c for c in raw if c not in _STOPWORDS)
    return out


# ---------------------------------------------------------------------------
# Snippet extraction
# ---------------------------------------------------------------------------


def _extract_snippets(
    text: str,
    query_terms: list[str],
    *,
    max_snippets: int = 2,
    width: int = 120,
    min_gap: int = 60,
) -> list[str]:
    """Return up to ``max_snippets`` excerpts of ``text`` centred on matches.

    Matches are found case-insensitively as substrings of any query term
    (robust for both ASCII words and CJK characters). Two snippet centres are
    kept at least ``min_gap`` characters apart so we don't show the same
    window twice. Each excerpt is ~``width`` chars, whitespace-collapsed, and
    ellipsized when it doesn't reach a string boundary.
    """
    if not text or not query_terms:
        return []
    terms = [re.escape(t) for t in query_terms if t]
    if not terms:
        return []
    pat = re.compile("|".join(terms))
    lower = text.lower()

    centres: list[int] = []
    last = -(10**9)
    for m in pat.finditer(lower):
        start = m.start()
        if start - last >= min_gap:
            centres.append(start)
            last = start
        if len(centres) >= max_snippets:
            break
    if not centres:
        return []

    half = width // 2
    snippets: list[str] = []
    n = len(text)
    for pos in centres:
        start = max(0, pos - half)
        end = min(n, start + width)
        # Re-extend the start if we hit the right edge short.
        if end - start < width:
            start = max(0, end - width)
        chunk = re.sub(r"\s+", " ", text[start:end]).strip()
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < n else ""
        snippets.append(f"{prefix}{chunk}{suffix}")
    return snippets


# ---------------------------------------------------------------------------
# Backend protocol + LexicalBackend
# ---------------------------------------------------------------------------


@runtime_checkable
class SearchBackend(Protocol):
    """Pluggable search backend (research.md §A plugin interface).

    A backend indexes the canonical :class:`ContentIndex` once and answers
    ranked queries. ``name`` identifies the backend on :class:`SearchResult`.
    """

    name: str

    def index(self, content_index: ContentIndex) -> None: ...

    def search(self, query: str, *, limit: int) -> list[SearchResult]: ...


DEFAULT_FIELD_WEIGHTS: dict[str, float] = {
    "title": 5.0,
    "headings": 3.0,
    "aliases": 3.0,
    "description": 2.0,
    "body": 1.0,
    "tags": 1.0,
}


class LexicalBackend:
    """Always-available BM25 backend with field weighting. Pure Python, zero deps.

    Args:
        k1: BM25 term-frequency saturation (default 1.5).
        b: BM25 length normalization (default 0.75).
        field_weights: optional override/extension of :data:`DEFAULT_FIELD_WEIGHTS`.
            Unknown field names are tolerated (treated as empty per concept).
    """

    name = "lexical"

    def __init__(
        self,
        *,
        k1: float = 1.5,
        b: float = 0.75,
        field_weights: dict[str, float] | None = None,
    ) -> None:
        self.k1 = k1
        self.b = b
        if field_weights is None:
            self._field_weights = dict(DEFAULT_FIELD_WEIGHTS)
        else:
            merged = dict(DEFAULT_FIELD_WEIGHTS)
            merged.update(field_weights)
            self._field_weights = merged
        self._fields: tuple[str, ...] = tuple(self._field_weights.keys())

        self._indexed: bool = False
        self._doc_ids: list[ConceptId] = []
        self._N: int = 0
        self._tf: dict[str, dict[ConceptId, Counter]] = {}
        self._df: dict[str, dict[str, int]] = {}
        self._dl: dict[str, dict[ConceptId, int]] = {}
        self._avgdl: dict[str, float] = {}
        self._concepts: dict[ConceptId, Concept] = {}
        self._body_text: dict[ConceptId, str] = {}
        self._desc_text: dict[ConceptId, str] = {}

    # -- indexing -----------------------------------------------------------

    @staticmethod
    def _field_texts(concept: Concept) -> dict[str, str]:
        return {
            "title": concept.title or "",
            "headings": " ".join(h.text for h in concept.headings),
            "aliases": " ".join(alias_labels(concept.frontmatter.get("aliases"))),
            "description": concept.description or "",
            "body": strip_markdown_for_search(concept.body or ""),
            "tags": " ".join(concept.tags),
        }

    def index(self, content_index: ContentIndex) -> None:
        """Build the per-field BM25 statistics from the canonical content index."""
        by_id = content_index.by_id
        doc_ids = sorted(by_id.keys())
        self._doc_ids = doc_ids
        self._N = len(doc_ids)
        self._concepts = dict(by_id)

        tf: dict[str, dict[ConceptId, Counter]] = {f: {} for f in self._fields}
        df: dict[str, dict[str, int]] = {f: {} for f in self._fields}
        dl: dict[str, dict[ConceptId, int]] = {f: {} for f in self._fields}
        body_text: dict[ConceptId, str] = {}
        desc_text: dict[ConceptId, str] = {}

        for index, cid in enumerate(doc_ids):
            if index % 64 == 0:
                _check_cancelled()
            concept = by_id[cid]
            field_texts = self._field_texts(concept)
            body_text[cid] = field_texts.get("body", "")
            desc_text[cid] = field_texts.get("description", "")
            for f in self._fields:
                toks = tokenize(field_texts.get(f, ""))
                if not toks:
                    continue
                counter = Counter(toks)
                tf[f][cid] = counter
                dl[f][cid] = len(toks)
                for term in counter:  # distinct terms only
                    df[f][term] = df[f].get(term, 0) + 1

        avgdl: dict[str, float] = {}
        for f in self._fields:
            total = sum(dl[f].values())
            avgdl[f] = (total / self._N) if self._N else 0.0

        self._tf = tf
        self._df = df
        self._dl = dl
        self._avgdl = avgdl
        self._body_text = body_text
        self._desc_text = desc_text
        self._indexed = True

    # -- querying -----------------------------------------------------------

    def _score_doc(self, cid: ConceptId, q_terms: list[str]) -> float:
        """Weighted-sum BM25 score for one document against the distinct query terms."""
        score = 0.0
        N = self._N
        k1 = self.k1
        b = self.b
        for f in self._fields:
            weight = self._field_weights[f]
            tf_doc = self._tf[f].get(cid)
            if not tf_doc:
                continue
            avgdl = self._avgdl[f]
            dl = self._dl[f][cid]
            norm_dl = (dl / avgdl) if avgdl > 0 else 0.0
            denom_base = k1 * (1.0 - b + b * norm_dl)
            df_field = self._df[f]
            bm25_f = 0.0
            for t in q_terms:
                tf_t = tf_doc.get(t, 0)
                if tf_t == 0:
                    continue
                df_t = df_field.get(t, 0)
                if df_t == 0:
                    continue
                idf = math.log((N - df_t + 0.5) / (df_t + 0.5) + 1.0)
                bm25_f += idf * (tf_t * (k1 + 1.0)) / (tf_t + denom_base)
            score += weight * bm25_f
        return score

    def search(self, query: str, *, limit: int) -> list[SearchResult]:
        if not self._indexed:
            return []
        # Distinct query terms (order preserved for stable iteration).
        seen: set[str] = set()
        q_terms: list[str] = []
        for t in tokenize(query):
            if t not in seen:
                seen.add(t)
                q_terms.append(t)
        if not q_terms:
            return []

        scored: list[tuple[float, ConceptId]] = []
        for index, cid in enumerate(self._doc_ids):
            if index % 64 == 0:
                _check_cancelled()
            s = self._score_doc(cid, q_terms)
            if s > 0.0:
                scored.append((s, cid))
        # Highest score first; concept id as a deterministic tie-break.
        scored.sort(key=lambda x: (-x[0], x[1]))

        cap = limit if limit and limit > 0 else len(scored)
        results: list[SearchResult] = []
        for score, cid in scored[:cap]:
            concept = self._concepts[cid]
            snips = _extract_snippets(self._body_text[cid], q_terms)
            if not snips:
                # Fall back to the description when the body had no match.
                snips = _extract_snippets(self._desc_text[cid], q_terms)
            results.append(
                SearchResult(
                    concept_id=cid,
                    title=concept.title,
                    score=score,
                    snippets=snips,
                    source_backend="lexical",
                    matched_tags=[],
                )
            )
        return results


# ---------------------------------------------------------------------------
# SemanticLiteBackend — dependency-free fuzzy-semantic ranker (current spec §6)
# ---------------------------------------------------------------------------


def _char_trigrams(text: str) -> Counter:
    """Character trigram multiset over a lowercased, whitespace-collapsed string.

    Each token is padded with a leading/trailing space. E.g. ``orders`` →
    `` or``, ``ord``, ``rde``, ``der``, ``ers``, ``rs ``.
    """
    if not text:
        return Counter()
    collapsed = re.sub(r"\s+", " ", text.lower()).strip()
    if not collapsed:
        return Counter()
    counts: Counter = Counter()
    for token in collapsed.split():
        padded = f" {token} "
        for i in range(len(padded) - 2):
            counts[padded[i : i + 3]] += 1
    return counts


def _cosine_sparse(a: Counter, b: Counter) -> float:
    """Cosine similarity over two sparse TF dicts (Counters)."""
    if not a or not b:
        return 0.0
    # Iterate over the smaller dict for efficiency.
    if len(a) > len(b):
        a, b = b, a
    dot = sum(count * b.get(key, 0) for key, count in a.items())
    if dot == 0:
        return 0.0
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class SemanticLiteBackend:
    """Dependency-free fuzzy-semantic ranker (current spec §6).

    **Not embeddings.** Combines token-level TF cosine (shares the lexical
    tokenizer) with character-trigram TF cosine (robust to morphology/typos).
    Score = ``α * cosine(tokens) + (1-α) * cosine(trigrams)``, default ``α=0.6``.

    The per-concept *profile string* weights title (×3) and description (×2)
    plus type, headings, tags, and — if ``okf.cap.entities`` /
    ``okf.cap.aliases`` are active — entity labels and aliases.
    """

    name = "semantic-lite"

    def __init__(self, *, token_weight: float = 0.6) -> None:
        self.token_weight = token_weight
        self._indexed = False
        self._doc_ids: list[ConceptId] = []
        self._concepts: dict[ConceptId, Concept] = {}
        self._token_tf: dict[ConceptId, Counter] = {}
        self._trigram_tf: dict[ConceptId, Counter] = {}
        self._body_text: dict[ConceptId, str] = {}
        self._desc_text: dict[ConceptId, str] = {}

    # -- profile construction ------------------------------------------------

    @staticmethod
    def _profile_string(concept: Concept) -> str:
        """Build the weighted profile string for fuzzy-semantic matching."""
        parts: list[str] = []
        # Title ×3
        title = concept.title or ""
        for _ in range(3):
            parts.append(title)
        # Description ×2
        desc = concept.description or ""
        for _ in range(2):
            parts.append(desc)
        # Type
        if concept.type:
            parts.append(concept.type)
        # All heading texts
        for h in concept.headings:
            parts.append(h.text)
        # Tags
        parts.extend(concept.tags)
        # Entity labels + aliases (if entities capability active)
        entities = concept.frontmatter.get("entities")
        if entities and isinstance(entities, list):
            for ent in entities:
                if isinstance(ent, str):
                    parts.append(ent)
                elif isinstance(ent, dict):
                    label = ent.get("label")
                    if label:
                        parts.append(label)
                    ent_aliases = ent.get("aliases")
                    if isinstance(ent_aliases, list):
                        parts.extend(a for a in ent_aliases if isinstance(a, str))
        # Top-level aliases (if aliases capability active)
        parts.extend(alias_labels(concept.frontmatter.get("aliases")))
        return " ".join(parts)

    # -- indexing ------------------------------------------------------------

    def index(self, content_index: ContentIndex) -> None:
        by_id = content_index.by_id
        doc_ids = sorted(by_id.keys())
        self._doc_ids = doc_ids
        self._concepts = dict(by_id)
        self._token_tf = {}
        self._trigram_tf = {}
        self._body_text = {}
        self._desc_text = {}
        for index, cid in enumerate(doc_ids):
            if index % 64 == 0:
                _check_cancelled()
            concept = by_id[cid]
            profile = self._profile_string(concept)
            self._token_tf[cid] = Counter(tokenize(profile))
            self._trigram_tf[cid] = _char_trigrams(profile)
            self._body_text[cid] = strip_markdown_for_search(concept.body or "")
            self._desc_text[cid] = concept.description or ""
        self._indexed = True

    # -- querying ------------------------------------------------------------

    def search(self, query: str, *, limit: int) -> list[SearchResult]:
        if not self._indexed:
            return []
        q_token_tf = Counter(tokenize(query))
        q_trigram_tf = _char_trigrams(query)
        alpha = self.token_weight

        scored: list[tuple[float, ConceptId]] = []
        for index, cid in enumerate(self._doc_ids):
            if index % 64 == 0:
                _check_cancelled()
            tok_sim = _cosine_sparse(q_token_tf, self._token_tf[cid])
            tri_sim = _cosine_sparse(q_trigram_tf, self._trigram_tf[cid])
            score = alpha * tok_sim + (1.0 - alpha) * tri_sim
            if score > 0.0:
                scored.append((score, cid))

        scored.sort(key=lambda x: (-x[0], x[1]))
        cap = limit if limit and limit > 0 else len(scored)
        results: list[SearchResult] = []
        q_terms = list(q_token_tf.keys())
        for score, cid in scored[:cap]:
            concept = self._concepts[cid]
            snips = _extract_snippets(self._body_text[cid], q_terms)
            if not snips:
                snips = _extract_snippets(self._desc_text[cid], q_terms)
            results.append(
                SearchResult(
                    concept_id=cid,
                    title=concept.title,
                    score=score,
                    snippets=snips,
                    source_backend="semantic-lite",
                )
            )
        return results


# ---------------------------------------------------------------------------
# HybridBackend — RRF fusion (current spec §6)
# ---------------------------------------------------------------------------


class HybridBackend:
    """Hybrid fusion via Reciprocal Rank Fusion (current spec §6).

    Fuses :class:`LexicalBackend` and :class:`SemanticLiteBackend` (both
    zero-dependency) so hybrid works dependency-free.

    RRF: for each concept, ``rrf = Σ_backend 1/(k + rank_backend)``, ``k=60``.
    Concepts missing from a backend contribute 0 from that backend. Sort by
    ``(-rrf, concept_id)``.
    """

    name = "hybrid"

    def __init__(
        self,
        lexical: SearchBackend,
        semantic: SearchBackend,
        *,
        k: int = 60,
    ) -> None:
        self._lexical = lexical
        self._semantic = semantic
        self.k = k

    def search(self, query: str, *, limit: int) -> list[SearchResult]:
        n = max(limit * 5, 50)
        lex_results = self._lexical.search(query, limit=n)
        sem_results = self._semantic.search(query, limit=n)

        # Build rank maps (1-indexed).
        lex_rank: dict[ConceptId, int] = {}
        for i, r in enumerate(lex_results):
            lex_rank[r.concept_id] = i + 1
        sem_rank: dict[ConceptId, int] = {}
        for i, r in enumerate(sem_results):
            sem_rank[r.concept_id] = i + 1

        all_ids = set(lex_rank.keys()) | set(sem_rank.keys())
        k = self.k
        scored: list[tuple[float, ConceptId]] = []
        # P3-5: build the cid → title map ONCE in O(N). The previous loop
        # re-scanned (*lex_results, *sem_results) for every cid and relied
        # on a misleading ``break`` that only exited the inner-most loop
        # (O(N²) with the same final result). A single comprehension is
        # behaviour-preserving and asymptotically linear. NOTE: dict
        # comprehension keeps the LATER value on duplicate keys, so for a
        # concept ranked by BOTH backends the SEMANTIC title wins — this
        # matches the original code's accidental behaviour (the misleading
        # ``break`` did not exit the outer loop, so semantic overwrote
        # lexical). snippet_map below uses explicit ``not in`` so it keeps
        # LEXICAL precedence; the two intentionally differ, unchanged.
        title_map: dict[ConceptId, str] = {
            r.concept_id: r.title for r in (*lex_results, *sem_results)
        }
        # P1-11: forward snippets from whichever fused backend ranked the
        # concept. Check lexical first (precedence), then semantic, so the
        # fused result carries non-empty snippets when at least one backend
        # produced them.
        snippet_map: dict[ConceptId, list[str]] = {}
        for cid in all_ids:
            rrf = 0.0
            if cid in lex_rank:
                rrf += 1.0 / (k + lex_rank[cid])
            if cid in sem_rank:
                rrf += 1.0 / (k + sem_rank[cid])
            scored.append((rrf, cid))

        # Forward snippets: lexical precedence, then semantic fallback.
        for results in (lex_results, sem_results):
            for r in results:
                if r.concept_id not in snippet_map:
                    snippet_map[r.concept_id] = list(r.snippets)

        scored.sort(key=lambda x: (-x[0], x[1]))
        cap = limit if limit and limit > 0 else len(scored)
        return [
            SearchResult(
                concept_id=cid,
                title=title_map.get(cid, ""),
                score=rrf,
                snippets=snippet_map.get(cid, []),
                source_backend="hybrid",
                backends=(["lexical"] if cid in lex_rank else [])
                         + (["semantic-lite"] if cid in sem_rank else []),
            )
            for rrf, cid in scored[:cap]
        ]


# ---------------------------------------------------------------------------
# Per-bundle backend cache
# ---------------------------------------------------------------------------

# Per-bundle lexical backend cache, stored as a Bundle attribute
# (``bundle._lexical_backend``). This replaces the former module-global
# ``_LEXICAL_BACKEND_CACHE`` keyed by ``id(bundle)`` which could leak
# memory across reloads in a long-running server. The per-bundle attribute
# is dropped when the Bundle is GC'd; ``Bundle.invalidate()`` resets it.
#
# ``clear_search_cache()`` is retained for backward compatibility as a no-op
# (the old module-global no longer exists); callers that used it should
# switch to ``bundle.invalidate()``.


def _get_lexical_backend(bundle: Bundle) -> "LexicalBackend":
    """Return (creating if needed) the cached ``LexicalBackend`` for ``bundle``.

    The backend is stored on the bundle itself (``bundle._lexical_backend``)
    so it is automatically released when the bundle is GC'd, and is
    invalidated by ``bundle.invalidate()``.
    """
    with bundle._derived_lock:
        if bundle._lexical_backend is None:
            bundle._lexical_backend = LexicalBackend()
        return bundle._lexical_backend


def _get_semantic_lite_backend(bundle: Bundle) -> "SemanticLiteBackend":
    """Return (creating if needed) the cached ``SemanticLiteBackend`` for ``bundle``."""
    with bundle._derived_lock:
        if bundle._semantic_lite_backend is None:
            bundle._semantic_lite_backend = SemanticLiteBackend()
        return bundle._semantic_lite_backend


def _ensure_indexed(bundle: Bundle, backend: SearchBackend,
                    content_index: ContentIndex) -> None:
    """Single-flight publication for mutable backend index construction."""
    with bundle._derived_lock:
        if not getattr(backend, "_indexed", False):
            try:
                backend.index(content_index)
                _check_cancelled()
            except Exception:
                if getattr(bundle, "_lexical_backend", None) is backend:
                    bundle._lexical_backend = None
                if getattr(bundle, "_semantic_lite_backend", None) is backend:
                    bundle._semantic_lite_backend = None
                raise


def clear_search_cache() -> None:
    """Deprecated no-op.

    The lexical backend cache is now per-bundle (``bundle._lexical_backend``).
    Call ``bundle.invalidate()`` to reset it. This function is retained for
    backward compatibility with existing callers and tests.
    """
    pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def mark_hybrid_active(bundle: Bundle) -> None:
    """Deprecated compatibility no-op; hybrid support is statically derived."""
    return None


def search_bundle(
    bundle: Bundle,
    query: str,
    *,
    mode: SearchMode = SearchMode.LEXICAL,
    type_filter: str | None = None,
    tag: str | None = None,
    limit: int = 20,
    relation: str | None = None,
    source: str | None = None,
    target: str | None = None,
) -> list[SearchResult]:
    """Generation-safe public search over one Bundle lock ownership."""
    from .retrieval import (RetrievalContractError, SearchRequest,
                            validate_retrieval_work)
    mode_value = mode.value if isinstance(mode, SearchMode) else mode
    request = SearchRequest(
        query=query, mode=mode_value, type_filter=type_filter, tag=tag,
        relation=relation, source=source, target=target, limit=limit,
    ).normalized()
    with bundle._derived_lock:
        validate_retrieval_work(bundle)
        for field, value in (("source", request.source), ("target", request.target)):
            if value and concept_id_from_str(value) not in bundle.concepts:
                raise RetrievalContractError(
                    "concept_not_found", "Concept target was not found",
                    fields={field: "not_found"}, status=404,
                )
        return _search_bundle_locked(
            bundle, request.query, mode=SearchMode(request.mode),
            type_filter=request.type_filter, tag=request.tag, limit=request.limit,
            relation=request.relation, source=request.source, target=request.target,
        )


def _search_bundle_locked(
    bundle: Bundle,
    query: str,
    *,
    mode: SearchMode = SearchMode.LEXICAL,
    type_filter: str | None = None,
    tag: str | None = None,
    limit: int = 20,
    relation: str | None = None,
    source: str | None = None,
    target: str | None = None,
) -> list[SearchResult]:
    """Search ``bundle`` and return ranked :class:`SearchResult` objects.

    Args:
        bundle: a loaded OKF :class:`~okf_loom.model.Bundle`.
        query: search string. In tag mode this may be a bare tag (``"badges"``)
            or ``#badges`` form. In relation mode, query is optional when
            edge filters (relation/source/target) are provided.
        mode: one of :class:`SearchMode`.
        type_filter: optional post-filter — keep only concepts whose ``type``
            frontmatter equals this string (applied in every mode).
        tag: optional post-filter — keep only concepts carrying this tag
            (case-insensitive; applied in every mode).
        limit: maximum number of results to return.
        relation: relation type filter (relation mode only).
        source: source concept-id filter (relation mode only).
        target: target concept-id filter (relation mode only).

    Returns:
        Ranked list of :class:`SearchResult` (highest score first).

    All three backends (:class:`LexicalBackend`, :class:`SemanticLiteBackend`,
    :class:`HybridBackend`) are zero-dependency, so there are no provider
    switches. ``mode`` selects which runs: ``lexical`` → LexicalBackend,
    ``semantic`` → SemanticLiteBackend, ``hybrid`` → HybridBackend (RRF fusion
    of the two). The agent (an LLM in opencode / Claude Code / Codex) is
    itself the best true-semantic engine, so a local embedding backend would
    be redundant; SemanticLite is the cheap fuzzy-semantic layer that helps
    the agent find candidates.
    """
    content_index = bundle.content_index(cancel_check=_check_cancelled)

    if mode == SearchMode.TAG:
        return _populate_description(
            _search_tag(
                content_index,
                query,
                type_filter=type_filter,
                tag=tag,
                limit=limit,
            ),
            content_index,
        )

    if mode == SearchMode.ENTITY:
        return _populate_description(
            _search_entity(
                bundle,
                content_index,
                query,
                type_filter=type_filter,
                tag=tag,
                limit=limit,
            ),
            content_index,
        )

    if mode == SearchMode.RELATION:
        return _populate_description(
            _search_relation(
                bundle,
                content_index,
                relation=relation,
                source=source,
                target=target,
                type_filter=type_filter,
                tag=tag,
                limit=limit,
            ),
            content_index,
        )

    if mode == SearchMode.SEMANTIC:
        backend = _get_semantic_lite_backend(bundle)
        _ensure_indexed(bundle, backend, content_index)
        corpus_size = max(len(content_index.by_id), 1)
        raw = backend.search(query, limit=corpus_size)
        return _apply_filters(raw, content_index, type_filter, tag, limit)

    if mode == SearchMode.HYBRID:
        # §4.2: hybrid fuses LexicalBackend + SemanticLiteBackend (zero-dep).
        lex = _get_lexical_backend(bundle)
        _ensure_indexed(bundle, lex, content_index)
        sem = _get_semantic_lite_backend(bundle)
        _ensure_indexed(bundle, sem, content_index)
        hybrid = HybridBackend(lex, sem)
        corpus_size = max(len(content_index.by_id), 1)
        raw = hybrid.search(query, limit=corpus_size)
        return _apply_filters(raw, content_index, type_filter, tag, limit)

    # --- lexical (default) ---
    backend = _get_lexical_backend(bundle)
    _ensure_indexed(bundle, backend, content_index)

    # Fetch every positive-scoring hit (backend caps at corpus size), then
    # apply the cross-cutting type/tag post-filters, then trim to ``limit``.
    corpus_size = max(len(content_index.by_id), 1)
    raw = backend.search(query, limit=corpus_size)
    return _apply_filters(raw, content_index, type_filter, tag, limit)


def _apply_filters(
    raw: list[SearchResult],
    content_index: ContentIndex,
    type_filter: str | None,
    tag: str | None,
    limit: int,
) -> list[SearchResult]:
    """Apply type/tag post-filters and trim to ``limit``.

    Also back-fills ``title`` (P1-6) and ``description`` (P2-52) from the
    content index so every backend's results expose the same user-visible
    shape.
    """
    tag_lc = tag.lower() if tag else None
    filtered: list[SearchResult] = []
    for index, r in enumerate(raw):
        if index % 64 == 0:
            _check_cancelled()
        concept = content_index.by_id.get(r.concept_id)
        if concept is None:
            continue
        if type_filter is not None and concept.type != type_filter:
            continue
        if tag_lc is not None and tag_lc not in {t.lower() for t in concept.tags}:
            continue
        # P1-6: back-fill the concept title in case a backend emitted a
        # raw/placeholder title. Don't overwrite a real title with an empty
        # concept.title fallback (defensive — concept.title always returns
        # at least the last id segment).
        if concept.title:
            r.title = concept.title
        # P2-52: populate description from the content index so callers/server
        # get richer result context without a fabricated empty value.
        if not r.description and concept.description:
            r.description = concept.description
        # Phase 2 search polish: back-fill type identity (name + colour +
        # resolved icon key) so the live-suggestions combobox can render a
        # type ICON + tinted chip per result. Set unconditionally so a
        # backend that pre-populated a stale type still matches the current
        # concept. The icon key resolves via the same type_icon_key used by
        # the renderer so identity is exact across cards/header/chip.
        r.type = concept.type or ""
        r.metadata = concept.metadata
        r.type_color, r.icon = _type_identity(concept.type)
        filtered.append(r)
    return filtered[:limit]


def _populate_description(
    results: list[SearchResult],
    content_index: ContentIndex,
) -> list[SearchResult]:
    """Back-fill ``SearchResult.description`` from the content index (P2-19).

    TAG/ENTITY/RELATION helpers apply type/tag filters inline (so they do
    not route through :func:`_apply_filters`). Without this back-fill,
    their results carry an empty ``description`` field, which is an
    inconsistency in the :class:`SearchResult` shape across modes. This
    helper mirrors the description back-fill that ``_apply_filters``
    performs for the lexical/semantic/hybrid paths.
    """
    for r in results:
        concept = content_index.by_id.get(r.concept_id)
        if concept is not None:
            if not r.description and concept.description:
                r.description = concept.description
            # Phase 2 search polish: type identity (name + colour + icon)
            # for the tinted type chip with the type ICON.
            r.type = concept.type or ""
            r.metadata = concept.metadata
            r.type_color, r.icon = _type_identity(concept.type)
    return results


def _search_tag(
    content_index: ContentIndex,
    query: str,
    *,
    type_filter: str | None,
    tag: str | None,
    limit: int,
) -> list[SearchResult]:
    """Tag-mode search: match ``Concept.tags`` case-insensitively, sort by title."""
    needle = query.lstrip("#").strip().lower()
    tag_filter_lc = tag.lower() if tag else None

    results: list[SearchResult] = []
    for index, cid in enumerate(sorted(content_index.by_id.keys())):
        if index % 64 == 0:
            _check_cancelled()
        concept = content_index.by_id[cid]
        if type_filter is not None and concept.type != type_filter:
            continue
        own_tags = concept.tags
        if tag_filter_lc is not None and tag_filter_lc not in {
            t.lower() for t in own_tags
        }:
            continue
        if not needle:
            # An empty tag query still lists concepts passing the filters.
            matched: list[str] = []
        else:
            matched = [t for t in own_tags if t.lower() == needle]
            if not matched:
                continue
        results.append(
            SearchResult(
                concept_id=cid,
                title=concept.title,
                score=1.0,
                snippets=[],
                source_backend="tag",
                matched_tags=matched,
            )
        )
    # Tag mode is ordered by title (stable on concept id as secondary key).
    results.sort(key=lambda r: (r.title, r.concept_id))
    return results[:limit]


# ---------------------------------------------------------------------------
# Entity search mode (current spec §6)
# ---------------------------------------------------------------------------


def _entity_match_fields(concept: Concept) -> dict[str, str]:
    """Return the set of searchable fields for entity-mode matching."""
    fields: dict[str, str] = {
        "id": concept_id_to_str(concept.id),
        "title": concept.title or "",
        "description": concept.description or "",
    }
    # Entity labels + aliases (if entities capability active)
    entities = concept.frontmatter.get("entities")
    if entities and isinstance(entities, list):
        labels: list[str] = []
        for ent in entities:
            if isinstance(ent, str):
                labels.append(ent)
            elif isinstance(ent, dict):
                label = ent.get("label")
                if label:
                    labels.append(label)
                ent_aliases = ent.get("aliases")
                if isinstance(ent_aliases, list):
                    labels.extend(a for a in ent_aliases if isinstance(a, str))
        if labels:
            fields["entities"] = " ".join(labels)
    # Top-level aliases
    aliases = alias_labels(concept.frontmatter.get("aliases"))
    if aliases:
        fields["aliases"] = " ".join(aliases)
    return fields


# Field weights for entity-mode score (title/alias > description > id)
_ENTITY_FIELD_WEIGHTS: dict[str, float] = {
    "title": 3.0,
    "entities": 3.0,
    "aliases": 3.0,
    "description": 2.0,
    "id": 1.0,
}


def _search_entity(
    bundle: Bundle,
    content_index: ContentIndex,
    query: str,
    *,
    type_filter: str | None,
    tag: str | None,
    limit: int,
) -> list[SearchResult]:
    """Entity-mode search: case-insensitive substring match against concept fields."""
    needle = query.lower().strip() if query else ""
    tag_lc = tag.lower() if tag else None

    scored: list[tuple[float, ConceptId, list[str]]] = []
    for index, cid in enumerate(sorted(content_index.by_id.keys())):
        if index % 64 == 0:
            _check_cancelled()
        concept = content_index.by_id[cid]
        if type_filter is not None and concept.type != type_filter:
            continue
        if tag_lc is not None and tag_lc not in {t.lower() for t in concept.tags}:
            continue
        fields = _entity_match_fields(concept)
        score = 0.0
        matched_snippets: list[str] = []
        for fname, fval in fields.items():
            if needle and needle in fval.lower():
                weight = _ENTITY_FIELD_WEIGHTS.get(fname, 1.0)
                score += weight
                # Record a short snippet of the matched field
                idx = fval.lower().index(needle)
                start = max(0, idx - 30)
                end = min(len(fval), idx + len(needle) + 30)
                snippet = fval[start:end]
                if start > 0:
                    snippet = "…" + snippet
                if end < len(fval):
                    snippet = snippet + "…"
                matched_snippets.append(f"[{fname}] {snippet}")
        if score > 0.0:
            scored.append((score, cid, matched_snippets))

    scored.sort(key=lambda x: (-x[0], x[1]))
    results: list[SearchResult] = []
    for score, cid, snippets in scored[:limit]:
        concept = content_index.by_id[cid]
        # Build detail with matched entity info
        fields = _entity_match_fields(concept)
        matched_fields = {
            fname: fval for fname, fval in fields.items()
            if needle and needle in fval.lower()
        } if needle else {}
        results.append(SearchResult(
            concept_id=cid,
            title=concept.title,
            score=score,
            # P2-5 (iter-3): entity snippets ARE the result data (matched
            # fields). Do not cap here; the CLI text mode shows all of them.
            snippets=snippets,
            source_backend="entity",
            detail={"matched_fields": matched_fields} if matched_fields else None,
        ))
    return results


# ---------------------------------------------------------------------------
# Relation search mode (current spec §6)
# ---------------------------------------------------------------------------


def _search_relation(
    bundle: Bundle,
    content_index: ContentIndex,
    *,
    relation: str | None,
    source: str | None,
    target: str | None,
    type_filter: str | None,
    tag: str | None,
    limit: int,
) -> list[SearchResult]:
    """Relation-mode search: query typed-relation, source, and markdown edges.

    ``--relation TYPE`` filters typed relations and sources (derived_from),
    never markdown links whose label happens to match.
    """
    from .paths import concept_id_from_str, ConceptIdError

    graph = bundle.graph()
    tag_lc = tag.lower() if tag else None
    rel_lc = relation.lower() if relation else None

    # Parse source/target filters with safe error handling
    source_cid = None
    target_cid = None
    if source:
        try:
            source_cid = concept_id_from_str(source)
        except (ConceptIdError, ValueError):
            return []
    if target:
        try:
            target_cid = concept_id_from_str(target)
        except (ConceptIdError, ValueError):
            return []

    # Collect matching edges grouped by source concept.
    by_source: dict[ConceptId, list[dict]] = {}
    for index, edge in enumerate(graph.edges):
        if index % 64 == 0:
            _check_cancelled()
        src = edge.source
        tgt = edge.target
        if source_cid is not None and src != source_cid:
            continue
        if target_cid is not None and tgt != target_cid:
            continue
        # Determine edge type and via (typed-relation vs markdown)
        edge_label = edge.label or ""
        via = edge.origin
        if via == "markdown" and edge.target_raw and edge.target_raw.startswith("relation:"):
            via = "relations"  # compatibility for callers constructing legacy Link objects
        edge_type = edge_label if via in ("relations", "sources") else ""

        # P2-1 (spec §4.3 "unioned" ambiguity, resolved toward "filter the
        # union"): when --relation TYPE is given, filter the UNION by edge
        # type. Markdown edges have an empty ``type`` so they drop out
        # naturally; typed-relation edges whose type does not match also
        # drop out. When NO --relation is given, ALL edges (frontmatter
        # relations ∪ markdown links) are kept.
        if rel_lc is not None and edge_type.lower() != rel_lc:
            continue

        # Apply type/tag filters on the source concept
        concept = content_index.by_id.get(src)
        if concept is None:
            continue
        if type_filter is not None and concept.type != type_filter:
            continue
        if tag_lc is not None and tag_lc not in {t.lower() for t in concept.tags}:
            continue
        by_source.setdefault(src, []).append({
            "type": edge_type,
            "target": concept_id_to_str(tgt) if tgt is not None else "",
            "via": via,
            **({"source_id": edge.source_id} if via == "sources" else {}),
        })

    scored: list[tuple[float, ConceptId]] = [
        (float(len(edges)), src) for src, edges in by_source.items()
    ]
    scored.sort(key=lambda x: (-x[0], x[1]))

    results: list[SearchResult] = []
    for score, src in scored[:limit]:
        concept = content_index.by_id[src]
        edges = by_source[src]
        # Build readable snippets from matched edges.
        edge_snippets = [
            f"{e['type']} → {e['target']}" if e["type"] else f"→ {e['target']}"
            for e in edges
        ]
        results.append(
            SearchResult(
                concept_id=src,
                title=concept.title,
                score=score,
                # P2-5 (iter-3): relation snippets ARE the result data (the
                # matched edges). The full edge set also lives in
                # detail["edges"]; do not cap the readable rendering here —
                # capping silently hid most of a concept's edges in text
                # mode. The CLI text mode now shows all of them.
                snippets=edge_snippets,
                source_backend="relation",
                detail={"edges": edges, "omitted_count": 0},
            )
        )
    return results


__all__ = [
    "SearchMode",
    "SearchResult",
    "SearchBackend",
    "LexicalBackend",
    "SemanticLiteBackend",
    "HybridBackend",
    "DEFAULT_FIELD_WEIGHTS",
    "search_bundle",
    "mark_hybrid_active",
    "clear_search_cache",
    "tokenize",
]
def _check_cancelled() -> None:
    # Lazy import avoids the retrieval/search module cycle.
    from .retrieval import check_retrieval_cancelled
    check_retrieval_cancelled()
