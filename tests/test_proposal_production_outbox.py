from __future__ import annotations

import json
import os
import threading

import pytest

from okf_loom.proposal_review.errors import RepositoryCorruptError
from okf_loom.studio import Studio


def test_production_studio_outbox_append_is_restart_idempotent(tmp_path) -> None:
    first = Studio.for_bundle(tmp_path)
    cursor = first.append_outbox_event("out_restart", {"schema": 1, "event": "created"})
    assert first.append_outbox_event(
        "out_restart", {"schema": 1, "event": "created"}) == cursor
    restarted = Studio.for_bundle(tmp_path)
    assert restarted.append_outbox_event(
        "out_restart", {"schema": 1, "event": "created"}) == cursor
    rows = [row for row in restarted.read_events()
            if row.get("outbox_id") == "out_restart"]
    assert len(rows) == 1


def test_retained_outbox_identity_payload_conflict_fails_closed(tmp_path) -> None:
    studio = Studio.for_bundle(tmp_path)
    studio.append_outbox_event("out_conflict", {"schema": 1, "event": "created"})
    with pytest.raises(RepositoryCorruptError):
        studio.append_outbox_event("out_conflict", {"schema": 1, "event": "withdrawn"})


def test_concurrent_outbox_append_has_one_exact_retained_event(tmp_path) -> None:
    first = Studio.for_bundle(tmp_path)
    second = Studio.for_bundle(tmp_path)
    payload = {"schema": 1, "event": "created"}
    cursors = []
    threads = [threading.Thread(target=lambda owner=owner:
        cursors.append(owner.append_outbox_event("out_concurrent", payload)))
        for owner in (first, second)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len(cursors) == 2 and cursors[0] == cursors[1]
    rows = [row for row in first.read_events() if row.get("outbox_id") == "out_concurrent"]
    assert len(rows) == 1 and rows[0]["data"] == payload


def test_legacy_event_file_mode_is_normalized_before_proposal_append(tmp_path) -> None:
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    studio.events_path.write_text(json.dumps({"type": "old", "seq": 1}) + "\n")
    os.chmod(studio.events_path, 0o644)
    studio.append_outbox_event("out_mode", {"schema": 1, "event": "created"})
    assert (studio.events_path.stat().st_mode & 0o777) == 0o600
