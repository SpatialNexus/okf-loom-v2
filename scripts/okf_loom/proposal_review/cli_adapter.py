"""Non-interactive CLI adapter sharing ProposalService wire envelopes."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..studio import Studio
from .activation import activate
from .canonical import loads_canonical
from .errors import ProposalReviewError
from .ids import validate_idempotency_key
from .outbox import OutboxReconciler
from .service_contracts import (ErrorBody, ErrorEnvelope, bounded_error_details,
                                success_cli_exit, transport_disposition)
from .studio_outbox import StudioOutboxAppender
from .studio_activity import StudioActivityAdapter


def _exit(code: str) -> int:
    return transport_disposition(code).cli_exit


def run(args: Any) -> int:
    service = None
    try:
        command = args.proposal_command
        if command == "create" and (
                bool(args.regenerate_legacy_id) != (args.legacy_source_generation is not None)):
            raise ValueError("legacy regeneration flags must be supplied together")
        studio = Studio.for_configured_bundle(args.bundle)
        studio.ensure_session()
        transaction_outbox = StudioOutboxAppender(studio)
        service = activate(
            args.bundle, transaction_activity=StudioActivityAdapter(studio),
            transaction_outbox=transaction_outbox)
        if command in {"apply", "undo"} and args.idempotency_key is None:
            raise ValueError("idempotency key is required")
        if command in {"create", "decide", "withdraw", "apply", "undo"} and args.idempotency_key is not None:
            validate_idempotency_key(args.idempotency_key)
        if command == "create":
            raw = Path(args.file).read_bytes()
            if len(raw) > 1_048_576:
                raise ValueError("proposal file exceeds the route cap")
            data = loads_canonical(raw)
            data["mode"] = args.mode
            if args.regenerate_legacy_id is not None:
                data["legacy_identity"] = {
                    "legacy_id": args.regenerate_legacy_id,
                    "source_generation": args.legacy_source_generation,
                }
            if args.supersede:
                proposal_id, revision = args.supersede.rsplit(":", 1)
                data.update(proposal_id=proposal_id, supersedes_revision=int(revision))
            result = service.create(data, idempotency_key=args.idempotency_key)
        elif command == "list":
            query = {k: v for k, v in {"state": args.state, "limit": args.limit,
                                      "cursor": args.cursor}.items() if v is not None}
            result = service.list(query)
        elif command == "show":
            result = service.show(args.proposal_id, args.revision)
        elif command == "decide":
            request = {"schema": 1, "decision": args.decision, "reason": args.reason or "",
                       "previous_row_token": args.row_token,
                       "read_model_token": args.read_model_token}
            result = service.decide(args.proposal_id, args.revision, args.op, request,
                                    actor=args.actor, idempotency_key=args.idempotency_key)
        elif command == "withdraw":
            request = {"schema": 1, "read_model_token": args.read_model_token,
                       "reason": args.reason}
            result = service.withdraw(args.proposal_id, args.revision, request,
                                       actor=args.actor, idempotency_key=args.idempotency_key)
        elif command == "apply":
            request = {"schema": 1, "proposal_id": args.proposal_id,
                       "proposal_revision": args.revision, "actor": args.actor,
                       "snapshot_digest": args.snapshot_digest,
                       "validation_context_digest": args.validation_context_digest,
                       "decision_set_token": args.decision_set_token,
                       "read_model_token": args.read_model_token}
            result = service.apply(request, idempotency_key=args.idempotency_key)
        elif command == "undo":
            request = {"schema": 1, "proposal_id": args.proposal_id,
                       "proposal_revision": args.revision, "actor": args.actor,
                       "transaction_id": args.transaction_id,
                       "undo_group_id": args.undo_group_id,
                       "read_model_token": args.read_model_token}
            result = service.undo(request, idempotency_key=args.idempotency_key)
        else:
            raise ValueError("unknown proposal command")
        if command in {"create", "decide", "withdraw", "apply", "undo"}:
            try:
                OutboxReconciler(service.repository, transaction_outbox).reconcile_pending(limit=100)
            except Exception as exc:
                print(f"proposal outbox reconcile deferred ({type(exc).__name__})", file=sys.stderr)
        print(json.dumps(result.to_mapping(), sort_keys=True, separators=(",", ":")))
        return success_cli_exit(result.data)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        code = "invalid_json" if isinstance(exc, json.JSONDecodeError) else "invalid_request"
        envelope = ErrorEnvelope(ErrorBody(code, "The proposal request is invalid.", "req_cli")).to_mapping()
        print(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
        print(f"proposal: {code}", file=sys.stderr)
        return 2
    except ProposalReviewError as exc:
        persisted = getattr(exc, "persisted_error", None)
        if persisted is not None:
            envelope = persisted.to_mapping()
            print(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
            print(f"proposal: {envelope['error']['code']}", file=sys.stderr)
            return transport_disposition(envelope["error"]["code"]).cli_exit
        code = getattr(exc, "code", "internal_error")
        disposition = transport_disposition(code)
        envelope = ErrorEnvelope(ErrorBody(code, getattr(exc, "message", "Proposal request failed."),
                                           "req_cli", disposition.retryable,
                                           disposition.resync_required,
                                           bounded_error_details(exc))).to_mapping()
        print(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
        print(f"proposal: {code}", file=sys.stderr)
        return _exit(code)
    except Exception:
        code = "internal_error"
        disposition = transport_disposition(code)
        envelope = ErrorEnvelope(ErrorBody(
            code, "The proposal command failed.", "req_cli",
            disposition.retryable, disposition.resync_required)).to_mapping()
        print(json.dumps(envelope, sort_keys=True, separators=(",", ":")))
        print(f"proposal: {code}", file=sys.stderr)
        return disposition.cli_exit
    finally:
        if service is not None:
            service.close()


__all__ = ["run"]
