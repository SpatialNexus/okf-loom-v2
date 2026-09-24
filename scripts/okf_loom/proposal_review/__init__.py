"""Proposal / Diff Review v1 contracts, pure projectors, and preview pipeline.

Foundation A: self-contained pure parse / validate / serialize for the complete
closed persisted + wire schema set (SPEC §§5-9).  Foundation B: side-effect-free
projectors, read model, and signed pagination (SPEC §§7, 10, 12).
It deliberately does **not** implement HTTP routes, CLI, capability activation,
apply publication, or UI. Preview operations reuse the production document
parser/round-trip codec but import no server, Studio, CLI, viewer, direct
mutation handler, or production Bundle adapter.

Public surface (re-exported here):

* :mod:`canonical` - duplicate-key-rejecting JSON load, canonical JSON bytes,
  digest / checksum helpers, depth / finite-number / negative-zero / UTF-8 rules.
* :mod:`ids`       - strict validators for every SPEC §3 primitive and path.
* :mod:`schemas`   - immutable typed closed records and validators for the
  complete Foundation A persisted/wire families.
* :mod:`errors`    - bounded structured domain validation errors.
* :mod:`ports`     - injected filesystem/clock/entropy/lock ports and the
  ``BundleView`` structural protocol for pure projector inputs.
* :mod:`repository`- sole owner of every path below ``proposal-review-v1``.
* :mod:`legacy`    - bounded read-only projector for pre-v1 suggestion JSONL.
* :mod:`projectors`- pure lifecycle/dependency/conflict/retention/audit projectors.
* :mod:`pagination`- pure snapshot-isolated signed-cursor list pagination.
* :mod:`read_model`- pure composite proposal read model.
"""
from __future__ import annotations

from . import canonical, errors, ids, schemas, ports, repository, legacy  # noqa: F401
from . import projectors, pagination, read_model, operations, staging, validation, preview  # noqa: F401
from . import service_contracts, service, outbox  # noqa: F401
from .canonical import (
    MAX_DEPTH,
    canonical_digest,
    compute_checksum,
    dumps_canonical,
    loads_canonical,
    sha256_hex,
)
from .errors import (
    CanonicalJsonError,
    IntegrityError,
    LimitError,
    PrimitiveError,
    ProposalReviewError,
    SchemaError,
    ValidationError,
    RepositoryError,
    RepositoryCorruptError,
    RepositoryQuotaError,
    StorageSafetyError,
    LockUnavailableError,
    IdempotencyConflictError,
    ProjectionError,
    InvalidStateError,
    StaleReadModelError,
    PreviewAuthorityError,
    ReadModelTooLargeError,
    CursorError,
    CursorExpiredError,
    PreviewPipelineError, DocumentCodecError, OperationExecutionError,
    UndeclaredReadError, RevisionMismatchError, ValidationProfileError,
    PreviewLimitError,
)
from .ports import (LinuxDirFdFilesystem, SystemClock, SystemEntropy, DocumentCodec,
                    DecodedDocument, FrozenSnapshotValidator, FrozenValidatorFinding,
                    FrozenValidationReport)
from .repository import ProposalRepositoryV1, ProposalRepositorySnapshot, RepositoryLimits
from .legacy import LegacySuggestionReader

__all__ = [
    "canonical",
    "errors",
    "ids",
    "schemas",
    "ports",
    "repository",
    "legacy",
    "projectors",
    "pagination",
    "read_model",
    "operations", "staging", "validation", "preview",
    "service_contracts", "service", "outbox",
    "MAX_DEPTH",
    "loads_canonical",
    "dumps_canonical",
    "sha256_hex",
    "canonical_digest",
    "compute_checksum",
    "ProposalReviewError",
    "CanonicalJsonError",
    "ValidationError",
    "SchemaError",
    "LimitError",
    "PrimitiveError",
    "IntegrityError",
    "RepositoryError",
    "RepositoryCorruptError",
    "RepositoryQuotaError",
    "StorageSafetyError",
    "LockUnavailableError",
    "IdempotencyConflictError",
    "ProjectionError",
    "InvalidStateError",
    "StaleReadModelError",
    "PreviewAuthorityError",
    "ReadModelTooLargeError",
    "CursorError",
    "CursorExpiredError",
    "PreviewPipelineError", "DocumentCodecError", "OperationExecutionError",
    "UndeclaredReadError", "RevisionMismatchError", "ValidationProfileError",
    "PreviewLimitError",
    "DocumentCodec", "DecodedDocument", "FrozenSnapshotValidator",
    "FrozenValidatorFinding", "FrozenValidationReport",
    "LinuxDirFdFilesystem",
    "SystemClock",
    "SystemEntropy",
    "ProposalRepositoryV1",
    "ProposalRepositorySnapshot",
    "RepositoryLimits",
    "LegacySuggestionReader",
]
