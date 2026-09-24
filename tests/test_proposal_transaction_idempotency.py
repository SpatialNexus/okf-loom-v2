from okf_loom.proposal_review.transactions import APPLY_SCOPE, UNDO_SCOPE


def test_apply_and_undo_idempotency_scopes_are_distinct() -> None:
    assert APPLY_SCOPE != UNDO_SCOPE
    assert APPLY_SCOPE.endswith("/apply") and UNDO_SCOPE.endswith("/undo")
