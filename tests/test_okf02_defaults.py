"""New content uses 0.2 without backdating or attributing imported prose."""
from pathlib import Path

import pytest

from okf_loom.bootstrap import bootstrap_bundle, import_directory
from okf_loom.cli import main
from okf_loom.index import regenerate_indexes
from okf_loom.metadata import parse_instant
from okf_loom.model import Bundle
from okf_loom.parse import parse_document


def test_public_bootstrap_and_new_authoring_defaults(tmp_path):
    root = tmp_path / "bundle"
    assert main(["bootstrap", str(root)]) == 0
    assert parse_document((root / "index.md").read_text())[0] == {"okf_version": "0.2"}
    assert main(["write-concept", "--bundle", str(root), "--id", "new",
                 "--type", "Note", "--body", "Newly produced."]) == 0
    fm, _ = parse_document((root / "new.md").read_text())
    assert fm["generated"]["by"] == "okf-loom/2.0.0"
    assert parse_instant(fm["generated"]["at"]) is not None
    assert "timestamp" not in fm and "verified" not in fm


def test_import_never_fabricates_actor_or_generation_time(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    typed = b'---\r\ntype: Note\r\ntimestamp: "2020-01-01T00:00:00Z"\r\n---\r\nOriginal  '
    (src / "typed.md").write_bytes(typed)
    (src / "plain.md").write_bytes(b"# Preserved\r\nOld prose  ")
    import_directory(src, dest)
    assert parse_document((dest / "index.md").read_text())[0] == {"okf_version": "0.2"}
    assert (dest / "typed.md").read_bytes() == typed
    fm, _ = parse_document((dest / "plain.md").read_text())
    assert "generated" not in fm and "timestamp" not in fm
    assert (dest / "plain.md").read_bytes().endswith(b"# Preserved\r\nOld prose  ")


@pytest.mark.parametrize("generated", ['{by: "okf-loom/2.0.0"}', '"true"', "1"])
def test_provenance_mapping_or_truthy_value_never_owns_index(tmp_path, generated):
    raw = f'---\nokf_version: "0.2"\ngenerated: {generated}\n---\n# Precious index\n'
    (tmp_path / "index.md").write_text(raw)
    (tmp_path / "a.md").write_text("---\ntype: Note\n---\nBody\n")
    regenerate_indexes(Bundle.load(tmp_path))
    assert (tmp_path / "index.md").read_text() == raw
