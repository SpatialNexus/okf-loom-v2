"""Bounded structured domain errors for Proposal / Diff Review v1.

These errors are raised by the pure parse / validate / serialize layer and
carry the structured detail required for later HTTP/CLI mapping *without*
importing those layers. Every error exposes:

* ``code``   - a stable string code drawn from the SPEC §10 error catalogue
                where applicable (``invalid_json`` for canonical-JSON faults,
                ``invalid_request`` for closed-schema faults, plus typed codes
                for digest / state / limit / primitive faults);
* ``message`` - a short human-readable explanation, free of secret material;
* ``path``    - optional dotted path of the offending member within the document;
* ``details`` - optional list of closed structured detail objects.

The hierarchy is intentionally narrow so callers can ``except`` on the
category they handle (canonical JSON vs closed-record validation vs integrity)
while still switching on ``.code`` for wire mapping.
"""
from __future__ import annotations

from typing import Any


class ProposalReviewError(Exception):
    """Base class for every Proposal / Diff Review v1 contract error."""


class CanonicalJsonError(ProposalReviewError):
    """Raised on canonical-JSON parse or serialize failure.

    Maps to HTTP ``400 invalid_json``. Covers: duplicate keys at any depth,
    NaN, infinity, negative zero, nesting depth beyond 32, malformed UTF-8,
    and structural JSON errors.
    """

    code: str = "invalid_json"

    def __init__(self, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.path = path


class ValidationError(ProposalReviewError):
    """Base for closed-record validation failures.

    Maps to HTTP ``422 invalid_request`` by default; subclasses or callers may
    override ``code`` for typed catalogue entries (``storage_corrupt`` etc.).
    """

    code: str = "invalid_request"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        path: str | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.path = path
        self.details = details


class SchemaError(ValidationError):
    """Unknown / missing field, wrong type, wrong discriminator, bad schema int."""


class LimitError(ValidationError):
    """A SPEC §4 length / count / size / depth bound was exceeded."""


class PrimitiveError(ValidationError):
    """An id / path / timestamp / digest / token primitive failed its grammar."""


class IntegrityError(ValidationError):
    """A checksum / digest mismatch or illegal phase / bitmap / state combination.

    Maps to HTTP ``503 storage_corrupt`` for persisted authority faults.
    """

    code: str = "storage_corrupt"


class RepositoryError(ProposalReviewError):
    """Bounded repository failure with a stable, transport-neutral code."""

    code: str = "repository_error"

    def __init__(self, message: str, *, code: str | None = None,
                 details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details


class StorageSafetyError(RepositoryError):
    """A no-follow/ownership/mode/link/identity guarantee could not be met."""

    code = "storage_unsafe"


class RepositoryCorruptError(RepositoryError):
    """Authority is corrupt and the affected repository scope is blocked."""

    code = "storage_corrupt"


class LockUnavailableError(RepositoryError):
    code = "lock_unavailable"


class RepositoryQuotaError(RepositoryError):
    code = "repository_quota_exceeded"


class IdempotencyConflictError(RepositoryError):
    code = "idempotency_conflict"


# ---------------------------------------------------------------------------
# Pure projection / read-model / pagination domain errors (Foundation B).
#
# These are raised by the side-effect-free projectors, read model, and signed
# pagination layer.  They carry the stable SPEC section-10 codes that a later
# HTTP/CLI seam maps to wire status without this layer importing that seam.
# ---------------------------------------------------------------------------


class ProjectionError(ProposalReviewError):
    """Base for pure projector / read-model failures.

    Maps to HTTP ``422 invalid_request`` by default for closed-state faults.
    """

    code = "invalid_request"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class InvalidStateError(ProjectionError):
    """A prohibited lifecycle transition or impossible derived combination.

    Maps to HTTP ``409 invalid_state``.
    """

    code = "invalid_state"


class StaleReadModelError(ProjectionError):
    """A mutation CAS token no longer matches canonical read-model state.

    Maps to HTTP ``409 stale_read_model`` and always requires a refetch.
    """

    code = "stale_read_model"

    def __init__(self, message: str = "Canonical proposal state changed; refetch before retrying.") -> None:
        super().__init__(message)
        self.resync_required = True


class PreviewAuthorityError(ProjectionError):
    """The sole preview artifact is missing/mismatched/oversized.

    Maps to HTTP ``503 preview_authority_unavailable``.  No inline or cached
    copy may substitute (SPEC section 5.3 / 6).
    """

    code = "preview_authority_unavailable"


class ReadModelTooLargeError(ProjectionError):
    """The complete proposal read model exceeds the 40 MiB response cap.

    Maps to HTTP ``413``-class rejection before persistence (SPEC section 4/6).
    """

    code = "read_model_too_large"


class CursorError(ProjectionError):
    """A pagination cursor is malformed, tampered, or signature-invalid.

    Maps to HTTP ``400 invalid_cursor``.
    """

    code = "invalid_cursor"


class CursorExpiredError(ProjectionError):
    """A pagination cursor's frozen authority expired or was pruned.

    Maps to HTTP ``409 cursor_expired`` with ``resync_required``.
    """

    code = "cursor_expired"

    def __init__(self, message: str = "Pagination cursor expired or pruned; start a fresh read.") -> None:
        super().__init__(message)
        self.resync_required = True


# Pure preview-pipeline faults.  These deliberately carry no filesystem or
# transport object and can be mapped by a later service boundary.
class PreviewPipelineError(ProposalReviewError):
    code = "invalid_request"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class DocumentCodecError(PreviewPipelineError):
    code = "invalid_document"


class OperationExecutionError(PreviewPipelineError):
    code = "operation_invalid"

    def __init__(self, message: str, *, op_id: str | None = None, concept: str | None = None) -> None:
        super().__init__(message)
        self.op_id = op_id
        self.concept = concept


class UndeclaredReadError(OperationExecutionError):
    code = "undeclared_read"


class RevisionMismatchError(PreviewPipelineError):
    code = "snapshot_changed"


class ValidationProfileError(PreviewPipelineError):
    code = "validation_profile_unavailable"


class PreviewLimitError(PreviewPipelineError):
    code = "preview_limit_exceeded"


__all__ = [
    "ProposalReviewError",
    "CanonicalJsonError",
    "ValidationError",
    "SchemaError",
    "LimitError",
    "PrimitiveError",
    "IntegrityError",
    "RepositoryError",
    "StorageSafetyError",
    "RepositoryCorruptError",
    "LockUnavailableError",
    "RepositoryQuotaError",
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
]
