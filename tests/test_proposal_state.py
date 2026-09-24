from pathlib import Path

from okf_loom.proposal_review import canonical


def test_state_01_concept_authority(tmp_path: Path) -> None:
    path = tmp_path / "concept.md"; path.write_bytes(b"one")
    first = canonical.sha256_hex(path.read_bytes())
    path.write_bytes(b"two")
    second = canonical.sha256_hex(path.read_bytes())
    assert first != second
