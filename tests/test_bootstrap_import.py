"""Import's public API preserves authored content and reports partial failures."""
from pathlib import Path

import pytest

from okf_loom.bootstrap import import_directory
from okf_loom.cli import main
from okf_loom.parse import parse_document


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("via_cli", [False, True])
def test_import_preserves_typed_authored_bytes_and_retry(tmp_path: Path, newline: bytes, via_cli: bool):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    raw = newline.join([
        b"---", b"# authored comment", b'type: "Memo" # keep explicit type',
        b"custom: {quoted: '001', tags: [a, b]}", b"---", b"", b"",
        b"# API guide", b"", b"Body with trailing spaces  ", b"no final newline",
    ])
    (src / "api_note.md").write_bytes(raw)
    if via_cli:
        assert main(["import", str(src), str(dest), "--default-type", "Decision"]) == 0
    else:
        result = import_directory(src, dest, default_type="Decision")
        assert result["imported"] == 1
        assert result["guessed_types"] == {}
    assert (dest / "api_note.md").read_bytes() == raw
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in dest.glob("*.md")}
    (src / "api_note.md").write_text("# Changed source\n")
    if via_cli:
        assert main(["import", str(src), str(dest)]) == 0
    else:
        retry = import_directory(src, dest)
        assert (retry["imported"], retry["skipped"]) == (0, 1)
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in dest.glob("*.md")}


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_import_adds_metadata_preserving_header_style_and_body(tmp_path: Path, newline: str):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    header = newline.join(["---", "# Keep me", "title: 'Authored title'",
                           'custom: {value: "001"} # unknown key', "---", ""])
    body = "\r\n\n# Notes\r\n\nBody  \r\nno final newline"
    (src / "notes.md").write_bytes((header + body).encode())
    (src / "plain.md").write_bytes(body.encode())
    (src / "api_users.md").write_text("# Notes\n")
    (src / "other.md").write_text("# A tutorial\n")
    result = import_directory(src, dest, default_type="Decision")
    assert result["guessed_types"] == {
        "notes.md": "Decision", "plain.md": "Decision",
        "api_users.md": "API Endpoint", "other.md": "Tutorial",
    }
    raw = (dest / "notes.md").read_bytes().decode()
    assert raw == header.replace("title:", "type: Decision" + newline + "title:") + body
    fm, _ = parse_document(raw)
    assert fm == {"title": "Authored title", "custom": {"value": "001"}, "type": "Decision"}
    assert (dest / "plain.md").read_bytes().endswith(body.encode())


@pytest.mark.parametrize("via_cli", [False, True])
def test_import_reports_bad_peer_without_leaking_content(tmp_path: Path, capsys, via_cli: bool):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    (src / "bad.md").write_text("---\nprivate: [SOURCE_SECRET\n---\n")
    (src / "bytes.md").write_bytes(b"\xffENCODING_SECRET")
    (src / "good.md").write_text("# Notes\n")
    (src / "index.md").write_text("Reserved\n")
    (src / "okf-loom.config.yaml").write_text("invalid: [CONFIG_SECRET\n")
    if via_cli:
        assert main(["import", str(src), str(dest), "--default-type", "Decision"]) == 0
        captured = capsys.readouterr()
        report = captured.out + captured.err
        assert "bad.md" in captured.err and "bytes.md" in captured.err
        assert "parse" in captured.err and "read" in captured.err
    else:
        result = import_directory(src, dest, default_type="Decision")
        assert (result["imported"], result["skipped"]) == (1, 1)
        assert [(e["path"], e["stage"]) for e in result["errors"]] == [
            ("bad.md", "parse"), ("bytes.md", "read")]
        assert result["guessed_types"] == {"good.md": "Decision"}
        report = str(result)
    assert "SOURCE_SECRET" not in report
    assert "ENCODING_SECRET" not in report
    assert "CONFIG_SECRET" not in report
    assert parse_document((dest / "good.md").read_text())[0]["type"] == "Decision"
    assert not (dest / "bad.md").exists()
    assert not (dest / "bytes.md").exists()


def test_import_write_failure_is_atomic_and_diagnosed(tmp_path: Path, monkeypatch):
    import os

    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    (src / "notes.md").write_text("# Original\n")
    import_directory(src, dest)
    original = (dest / "notes.md").read_bytes()
    (src / "notes.md").write_text("# Replacement\n")
    replace = os.replace

    def fail_target(source, target):
        if Path(target) == dest / "notes.md":
            raise OSError("WRITE_SECRET")
        return replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_target)
        result = import_directory(src, dest, overwrite=True)
    assert (result["imported"], result["skipped"]) == (0, 0)
    assert result["errors"][0]["stage"] == "write"
    assert "WRITE_SECRET" not in str(result)
    assert result["guessed_types"] == {}
    assert (dest / "notes.md").read_bytes() == original
    assert not list(dest.glob(".okf-*.tmp"))
    assert import_directory(src, dest, overwrite=True)["imported"] == 1
    assert (dest / "notes.md").read_bytes().endswith(b"# Replacement\n")


def test_import_refuses_destination_symlink_escape(tmp_path: Path):
    src, dest, outside = tmp_path / "src", tmp_path / "dest", tmp_path / "outside"
    (src / "nested").mkdir(parents=True)
    (src / "nested" / "notes.md").write_text("# Notes\n")
    outside.mkdir()
    import_directory(src, dest)
    (dest / "nested" / "notes.md").unlink()
    (dest / "nested").rmdir()
    (dest / "nested").symlink_to(outside, target_is_directory=True)
    result = import_directory(src, dest, overwrite=True)
    assert result["imported"] == 0
    assert result["errors"][0]["stage"] == "write"
    assert not list(outside.iterdir())
