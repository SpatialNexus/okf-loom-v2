"""Explicit, fail-closed composition for Proposal Review transports."""
from __future__ import annotations

import os
import secrets
import stat
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..config import OkfConfig
from ..proposal_snapshot import (DEFAULT_RUNTIME_IDENTITIES, ProductionDocumentCodec,
                                 ProductionFrozenSnapshotValidator, capture_bundle)
from .canonical import canonical_digest
from .ports import LinuxDirFdFilesystem, SystemClock, SystemEntropy
from .errors import RepositoryError
from .repository import ProposalRepositoryV1
from .publication import TrustedPublicationAdapter
from .transactions import ProposalTransactionCoordinator
from .legacy import LegacySuggestionReader
from .schemas import Snapshot
from . import service as _domain
from ..session_authority import SessionAuthority


CAPABILITY_ID = _domain.PROPOSAL_CAPABILITY


class ActivationError(RepositoryError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}", code=code)
        self.message = f"{code}: {message}"


def capability(active: bool = False, *, reason: str | None = None) -> dict[str, Any]:
    return {"id": CAPABILITY_ID, "status": "experimental", "active": bool(active),
            "protocol_version": 1, "operation_schema_version": 1,
            "direct_mode_default": True,
            "inactive_reason": None if active else (reason or "not_enabled")}


def is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def validate_server_activation(*, enabled: bool, host: str, public: bool = False,
                               tunnel: bool = False, no_edit: bool = False) -> None:
    if enabled and not no_edit and (public or tunnel or not is_loopback(host)):
        raise ActivationError("proposal_review_requires_loopback",
                              "Editable Proposal Review requires loopback without public or tunnel exposure.")


@dataclass(frozen=True)
class ProductionCapturePort:
    bundle_root: Path

    def capture(self, *, captured_at: str,
                proposal_capability_active: bool) -> _domain.CapturedServiceBundle:
        captured = capture_bundle(self.bundle_root, captured_at=captured_at,
                                  identities=DEFAULT_RUNTIME_IDENTITIES)
        caps = set(captured.capabilities)
        if proposal_capability_active:
            caps.add(CAPABILITY_ID)
        else:
            caps.discard(CAPABILITY_ID)
        ordered = tuple(sorted(caps))
        body = {"schema": 1,
                "concepts": [entry.to_mapping() for entry in captured.snapshot.concepts],
                "capabilities": list(ordered), "config_digest": captured.config_digest,
                "validation_context_digest": captured.validation_context_digest,
                "validator_id": captured.validator_id, "serializer_id": captured.serializer_id,
                "operation_runtime_id": captured.operation_runtime_id}
        digest = canonical_digest(body)
        snapshot = Snapshot.from_mapping({**body, "captured_at": captured.snapshot.captured_at,
                                          "snapshot_digest": digest})
        current = replace(captured, snapshot=snapshot, snapshot_digest=digest,
                          capabilities=ordered)
        return _domain.CapturedServiceBundle(snapshot, captured.concept_bytes, current,
                                             ProductionDocumentCodec(),
                                             ProductionFrozenSnapshotValidator(captured.facts))


@dataclass(frozen=True)
class ProductionLegacyReader:
    """Bounded read-only resolver over retained Studio suggestion generations."""
    session_root: LinuxDirFdFilesystem

    def verify(self, identity):
        generation = identity.source_generation
        if generation == 0:
            path = "proposals.jsonl"
        else:
            path = ""
            for name in self.session_root.list_dir("."):
                match = re.fullmatch(r"proposals-(\d{8})-(\d{2})\.jsonl", name)
                if match and int(match.group(1)) * 100 + int(match.group(2)) == generation:
                    if path:
                        raise RepositoryError("legacy source generation is ambiguous",
                                              code="legacy_identity_conflict")
                    path = name
            if not path:
                raise RepositoryError("legacy source generation is stale or missing",
                                      code="legacy_identity_conflict")
        try:
            return LegacySuggestionReader(self.session_root).verify(identity, path=path)
        except FileNotFoundError as exc:
            raise RepositoryError("legacy source generation is stale or missing",
                                  code="legacy_identity_conflict") from exc


def _open_session_root(bundle_root: Path) -> tuple[LinuxDirFdFilesystem, SessionAuthority | None]:
    cfg = OkfConfig.load(bundle_root)
    from ..studio import _configured_session_root
    resolved = _configured_session_root(bundle_root, cfg.studio.session_path)
    authority = resolved if isinstance(resolved, SessionAuthority) else None
    if authority is None:
        from ..session_authority import resolve_bundle_session_authority
        authority = resolve_bundle_session_authority(
            bundle_root, cfg.studio.session_path, create=True)
    candidate = authority.path if authority is not None else resolved
    resolved_root = bundle_root.resolve(strict=True)
    if os.environ.get("OKF_LOOM_SESSION_ROOT") is None:
        try:
            authority.display_path.relative_to(resolved_root)
        except ValueError as exc:
            authority.close()
            raise ActivationError("storage_unsafe", "Configured session root escapes the bundle.") from exc
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        # Admit owner-only 0700 or 02700 without chmod. mkdir success cannot
        # establish inode provenance across the first stat/open boundary.
        authority.mkdir("proposal-review-v1", exist_ok=True)
        fd = os.open(candidate, flags)
        st = os.fstat(fd)
        if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
                or stat.S_IMODE(st.st_mode) & 0o022):
            raise ActivationError("storage_unsafe", "Configured session root is not private.")
        return LinuxDirFdFilesystem.adopt(fd), authority
    except OSError as exc:
        if authority is not None:
            authority.close()
        raise ActivationError("storage_unsafe", "Configured session root is unsafe.") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _trusted_bundle_root(bundle: str | Path) -> Path:
    supplied = Path(bundle)
    try:
        if supplied.is_symlink():
            raise ActivationError("storage_unsafe", "Bundle root symlinks are not trusted.")
        return supplied.resolve(strict=True)
    except OSError as exc:
        raise ActivationError("storage_unsafe", "Bundle root is unavailable.") from exc


def _compose_transactions(root: Path, repository: ProposalRepositoryV1, clock: Any,
                          *, activity: Any | None = None, outbox: Any | None = None,
                          repository_path: Path | None = None,
                          session_authority: SessionAuthority | None = None,
                          ) -> tuple[ProposalTransactionCoordinator, TrustedPublicationAdapter]:
    from ..studio import _configured_session_root
    session = (session_authority or
               _configured_session_root(root, OkfConfig.load(root).studio.session_path))
    authority = session if isinstance(session, SessionAuthority) else None
    session_path = authority.path if authority is not None else session
    repository_fd = -1
    if authority is not None and repository_path is None:
        repository_fd = os.open("proposal-review-v1", os.O_RDONLY | os.O_DIRECTORY
                                | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=authority.fd)
    try:
        publication = TrustedPublicationAdapter(
            root, repository_path or (session_path / "proposal-review-v1"),
            repository_dir_fd=repository_fd if repository_fd >= 0 else None)
    finally:
        if repository_fd >= 0: os.close(repository_fd)
    if authority is not None:
        publication._session_authority = authority
    coordinator = ProposalTransactionCoordinator(repository, ProductionCapturePort(root), publication,
                                                 clock=clock, activity=activity, outbox=outbox)
    try: coordinator.recover_all()
    except Exception:
        coordinator.close()
        raise
    return coordinator, publication


def activate(bundle: str | Path, *, active: bool = True,
             transaction_activity: Any | None = None,
             transaction_outbox: Any | None = None) -> _domain.ProposalService:
    root = _trusted_bundle_root(bundle)
    fs, authority = _open_session_root(root)
    repository = None
    coordinator = None
    publication = None
    try:
        clock = SystemClock()
        repository = ProposalRepositoryV1.bootstrap(
            fs, clock=clock, entropy=SystemEntropy(),
            activity_link_verifier=transaction_activity)
        coordinator, publication = _compose_transactions(root, repository, clock,
            activity=transaction_activity, outbox=transaction_outbox,
            session_authority=authority)
        return _domain.ProposalService(repository, ProductionCapturePort(root), clock,
                                       DEFAULT_RUNTIME_IDENTITIES,
                                       proposal_capability_active=active,
                                       legacy_reader=ProductionLegacyReader(fs),
                                       transaction_coordinator=coordinator,
                                       transaction_publication=publication)
    except BaseException:
        if coordinator is not None:
            coordinator.close()
        elif publication is not None:
            publication.close()
        if repository is not None:
            repository.close()
        fs.close()
        if authority is not None: authority.close()
        raise


@dataclass(frozen=True)
class HttpActivation:
    service: _domain.ProposalService
    proposal_token: str
    coordinator: ProposalTransactionCoordinator | None = None

    def close(self) -> None:
        self.service.close()

    def __enter__(self) -> "HttpActivation":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def activate_http(bundle: str | Path, *, transaction_activity: Any | None = None,
                  transaction_outbox: Any | None = None) -> HttpActivation:
    """Activate HTTP service and rotate its private mutation capability."""
    root = _trusted_bundle_root(bundle)
    fs, authority = _open_session_root(root)
    repository = None
    coordinator = None
    publication = None
    try:
        clock = SystemClock()
        repository = ProposalRepositoryV1.bootstrap(
            fs, clock=clock, entropy=SystemEntropy(),
            activity_link_verifier=transaction_activity)
        proposal_token = SystemEntropy().token_bytes(32).hex()
        fs.write_atomic(".proposal-http-token", proposal_token.encode("ascii"))
        # Read through the trusted adapter to prove the persisted authority is
        # exactly what this process will compare.
        persisted = fs.read_bytes(".proposal-http-token", max_bytes=128).decode("ascii")
        if not secrets.compare_digest(proposal_token, persisted):
            raise ActivationError("storage_unsafe", "Proposal mutation token persistence failed.")
        coordinator, publication = _compose_transactions(root, repository, clock,
            activity=transaction_activity, outbox=transaction_outbox,
            session_authority=authority)
        service = _domain.ProposalService(repository, ProductionCapturePort(root), clock,
                                          DEFAULT_RUNTIME_IDENTITIES,
                                          proposal_capability_active=True,
                                          legacy_reader=ProductionLegacyReader(fs),
                                          transaction_coordinator=coordinator,
                                          transaction_publication=publication)
        return HttpActivation(service, proposal_token, coordinator)
    except BaseException:
        if coordinator is not None:
            coordinator.close()
        elif publication is not None:
            publication.close()
        if repository is not None:
            repository.close()
        fs.close()
        if authority is not None: authority.close()
        raise


__all__ = ["ActivationError", "CAPABILITY_ID", "HttpActivation", "ProductionCapturePort",
           "ProductionLegacyReader",
           "activate", "activate_http",
           "capability", "is_loopback", "validate_server_activation"]
