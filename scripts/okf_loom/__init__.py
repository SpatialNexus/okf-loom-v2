"""okf-loom — generic Open Knowledge Format tooling.

A vendor-neutral, harness-agnostic toolkit for working with OKF bundles.
Implements upstream OKF SPEC v0.2 as both a library and a CLI.

Public surface:
    - Versioning: `SPEC_VERSION`, `LOOM_VERSION`
    - Core model: `Bundle`, `Concept`, `Link`, `Graph`, `ContentIndex`
    - Parsing: `parse_document`, `serialize_document`, `extract_links`
    - Validation: `validate_bundle`, `ValidationReport`
    - Discovery/update: see `okf_loom.discover`, `okf_loom.update`
    - Viewer: see `okf_loom.server`, `okf_loom.render`
    - Search: see `okf_loom.search`

The OKF specification is published at:
    https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md
"""
from __future__ import annotations

# --- Versioning --------------------------------------------------------------
# The upstream OKF wire-format specification version okf-loom implements.
SPEC_VERSION: str = "0.2"
# The checkout runtime version (the repo is distributed as a skill, not as an
# installable package).
LOOM_VERSION: str = "2.0.0"

# Reserved filenames per SPEC §3.1.
RESERVED_FILENAMES: frozenset[str] = frozenset({"index.md", "log.md"})

# Frontmatter keys defined by the SPEC.
REQUIRED_FRONTMATTER_KEYS: tuple[str, ...] = ("type",)
RECOMMENDED_FRONTMATTER_KEYS: tuple[str, ...] = (
    "title",
    "description",
    "resource",
    "tags",
)
# Upstream OKF 0.2 §12: bundle-root index.md may declare okf_version.
OKF_VERSION_KEY: str = "okf_version"
# Toolkit-namespaced capability markers (forward-compat, see docs/architecture.md).
OKF_EXTENSIONS_KEY: str = "okf_extensions"
GENERATED_MARKER_KEY: str = "generated"  # used in index.md frontmatter

# Names of capabilities recognised by okf-loom (see extensions.py for detail).
CAPABILITY_NAMESPACE: str = "okf.cap"


from .paths import (  # noqa: E402
    ConceptId,
    concept_id_from_path,
    concept_id_from_str,
    concept_id_to_str,
    is_reserved_filename,
    validate_segment,
    ConceptIdError,
)
from .exceptions import (  # noqa: E402
    OKFError,
    OKFParseError,
    OKFValidationError,
    OKFIOError,
)
from .parse import (  # noqa: E402
    parse_document,
    serialize_document,
    extract_links,
    strip_markdown_for_search,
    FRONTMATTER_DELIM,
)
from .model import (  # noqa: E402
    Concept,
    IndexFile,
    LogEntry,
    LogFile,
    Link,
    LinkForm,
    Heading,
    Bundle,
    Graph,
    ContentIndex,
    LoadWarning,
)
from .retrieval import (  # noqa: E402
    ContextRequest,
    Passage,
    RetrievalContractError,
    RetrievalService,
    SearchRequest,
    build_passage_corpus,
    bundle_revision,
    corpus_chunk_identity,
    governed_item_token_cost,
    load_passage_corpus,
)

__all__ = [
    # versioning
    "SPEC_VERSION",
    "LOOM_VERSION",
    "RESERVED_FILENAMES",
    "REQUIRED_FRONTMATTER_KEYS",
    "RECOMMENDED_FRONTMATTER_KEYS",
    "OKF_VERSION_KEY",
    "OKF_EXTENSIONS_KEY",
    "GENERATED_MARKER_KEY",
    "CAPABILITY_NAMESPACE",
    # errors
    "OKFError",
    "OKFParseError",
    "OKFValidationError",
    "OKFIOError",
    # paths
    "ConceptId",
    "ConceptIdError",
    "concept_id_from_path",
    "concept_id_from_str",
    "concept_id_to_str",
    "is_reserved_filename",
    "validate_segment",
    # parse
    "parse_document",
    "serialize_document",
    "extract_links",
    "strip_markdown_for_search",
    "FRONTMATTER_DELIM",
    # model
    "Concept",
    "IndexFile",
    "LogEntry",
    "LogFile",
    "Link",
    "LinkForm",
    "Heading",
    "Bundle",
    "Graph",
    "ContentIndex",
    # retrieval/context
    "SearchRequest",
    "ContextRequest",
    "Passage",
    "RetrievalService",
    "RetrievalContractError",
    "build_passage_corpus",
    "bundle_revision",
    "corpus_chunk_identity",
    "governed_item_token_cost",
    "load_passage_corpus",
    "LoadWarning",
]
