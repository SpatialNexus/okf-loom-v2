"""Exception hierarchy for okf-loom."""
from __future__ import annotations


class OKFError(Exception):
    """Base class for all okf-loom errors."""


class OKFParseError(OKFError):
    """Raised when a markdown file cannot be parsed as an OKF document."""


class OKFValidationError(OKFError):
    """Raised when a bundle or document fails SPEC conformance.

    Use `strict=False` on validators to demote these to warnings instead.
    """


class OKFIOError(OKFError):
    """Raised on filesystem or packaging I/O failures."""


class OKFCapabilityError(OKFError):
    """Raised when a requested capability is not registered."""
