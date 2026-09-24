from copy import deepcopy

import pytest

from test_proposal_apply import _ready_transaction
from okf_loom.proposal_review.errors import SchemaError
from okf_loom.proposal_review.canonical import compute_checksum
from okf_loom.proposal_review.transaction_intent import TransactionFinalizationIntent


def test_state_11_transaction_audit(tmp_path) -> None:
    bundle, session, repository, coordinator, publication, request, activity = _ready_transaction(tmp_path)
    try:
        result = coordinator.apply(request, "K" * 32)
        audits = [row.payload for row in repository.snapshot().aggregate("transactions")
                  if row.kind == "transaction" and row.payload.get("transaction_id") == result.transaction_id]
        assert len(audits) == 1
        replay = coordinator.apply(request, "K" * 32)
        assert replay.transaction_id == result.transaction_id and replay.replayed
        assert audits[0]["request_fingerprint"] != audits[0]["transaction_intent_digest"]
    finally: publication.close()


@pytest.mark.parametrize("change", [
    lambda activity: activity.update(extra="rejected"),
    lambda activity: activity.update(type=1),
])
def test_transaction_intent_rejects_malformed_activity_candidate(change, tmp_path) -> None:
    bundle, session, repository, coordinator, publication, request, _activity = _ready_transaction(tmp_path)
    try:
        result = coordinator.apply(request, "K" * 32)
        value = deepcopy(repository.read_transaction_intent(result.transaction_id).value)
        change(value["candidates"]["commit"]["activities"][0])
        value["checksum"] = compute_checksum(value)
        with pytest.raises(SchemaError):
            TransactionFinalizationIntent.from_mapping(value)
    finally:
        publication.close()
