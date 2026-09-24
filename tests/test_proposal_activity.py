import pytest

from okf_loom.proposal_review.errors import SchemaError


class FakeStudio:
    def __init__(self): self.rows = []
    def read_events(self, **_): return list(self.rows)
    def events_append(self, row, publish=False):
        value = {**row, "event_cursor": len(self.rows) + 1, "seq": len(self.rows) + 1,
                 "ts": "2026-07-15T00:00:00.000Z"}
        self.rows.append(value); return value


def test_caller_assigned_activity_append_is_idempotent() -> None:
    from okf_loom.proposal_review.studio_activity import StudioActivityAdapter
    studio = FakeStudio(); adapter = StudioActivityAdapter(studio)
    aid = "act_" + "0" * 26
    payload = {"type": "activity", "actor": "local", "origin": "proposal",
               "action": "apply", "ids": ["orders"], "summary": "apply",
               "undoable": True, "detail": {"transaction_id": "txn_" + "0" * 26,
               "proposal_id": "0" * 26, "proposal_revision": 1,
               "undo_group_id": "undo_" + "0" * 26}}
    adapter.append_activity(aid, payload)
    adapter.append_activity(aid, payload)
    assert len(studio.rows) == 1
    assert adapter.verify_activity_ids("txn_" + "0" * 26, (aid,))


@pytest.mark.parametrize("change", [
    lambda payload: payload.update(extra="rejected"),
    lambda payload: payload.update(type=1),
])
def test_activity_append_rejects_non_closed_or_wrong_type_without_append(change) -> None:
    from okf_loom.proposal_review.studio_activity import StudioActivityAdapter
    studio = FakeStudio(); adapter = StudioActivityAdapter(studio)
    payload = {"type": "activity", "actor": "local", "origin": "proposal",
               "action": "apply", "ids": ["orders"], "summary": "apply",
               "undoable": True, "detail": {"transaction_id": "txn_" + "0" * 26,
               "proposal_id": "0" * 26, "proposal_revision": 1,
               "undo_group_id": "undo_" + "0" * 26}}
    change(payload)
    with pytest.raises(SchemaError):
        adapter.append_activity("act_" + "0" * 26, payload)
    assert studio.rows == []
