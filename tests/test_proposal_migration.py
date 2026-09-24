from __future__ import annotations

import json

import pytest

from okf_loom.proposal_review import schemas
from okf_loom.proposal_review.errors import RepositoryError
from okf_loom.proposal_review.legacy import LegacySuggestionReader
from test_proposal_service_recovery import _adapter


def _write(root, rows):
    root.mkdir(mode=0o700)
    path = root / "proposals.jsonl"
    path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
    path.chmod(0o600)
    return path


def test_mig_01_projection(tmp_path) -> None:
    root = tmp_path / "legacy"
    _write(root, [{"id": "legacy-1", "summary": "old", "author": "agent",
                   "created_at": "2026-07-15T00:00:00.000Z"}])
    identity = schemas.LegacyIdentity.from_mapping(
        {"legacy_id": "legacy-1", "source_generation": 7})
    projection = LegacySuggestionReader(_adapter(root)).verify(identity)
    assert projection.legacy_identity == identity
    assert projection.regeneration_required and not projection.applicable


def test_mig_02_malformed(tmp_path) -> None:
    root = tmp_path / "malformed"
    path = _write(root, [{"id": "legacy-1", "unexpected": "body"}])
    before = path.read_bytes()
    identity = schemas.LegacyIdentity.from_mapping(
        {"legacy_id": "legacy-1", "source_generation": 0})
    with pytest.raises(RepositoryError) as error:
        LegacySuggestionReader(_adapter(root)).verify(identity)
    assert error.value.code == "storage_corrupt" and path.read_bytes() == before


def test_mig_03_generation(tmp_path) -> None:
    root = tmp_path / "duplicate"
    path = _write(root, [
        {"id": "same", "summary": "one", "author": "agent",
         "created_at": "2026-07-15T00:00:00.000Z"},
        {"id": "same", "summary": "two", "author": "agent",
         "created_at": "2026-07-15T00:00:01.000Z"},
    ])
    before = path.read_bytes()
    identity = schemas.LegacyIdentity.from_mapping(
        {"legacy_id": "same", "source_generation": 0})
    with pytest.raises(RepositoryError) as error:
        LegacySuggestionReader(_adapter(root)).verify(identity)
    assert error.value.code == "legacy_identity_conflict"
    assert path.read_bytes() == before
