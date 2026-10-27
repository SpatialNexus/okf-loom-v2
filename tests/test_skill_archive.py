"""Regression proofs for the commit-authoritative skill archive boundary."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_skill_archive.py"
if str(SCRIPT.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT.parent))
import build_skill_archive as archive_builder  # noqa: E402


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=True,
    )
    return proc.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "skill"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Archive Test")
    (root / "README.md").write_text("tracked\n", encoding="utf-8")
    (root / "scripts").mkdir()
    executable = root / "scripts" / "run"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    (root / ".gitignore").write_text("*.local\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "fixture")
    return root


def _rewrite(out: Path, mutate) -> None:
    with zipfile.ZipFile(out, "r") as source:
        records = [(info, source.read(info)) for info in source.infolist()]
    mutate(records)
    with zipfile.ZipFile(out, "w") as target:
        for info, data in records:
            target.writestr(info, data)


def _reencode(out: Path) -> None:
    """Use valid DEFLATE stored blocks, not ZIP_STORED or altered metadata."""
    with zipfile.ZipFile(out) as source:
        records = [(info, source.read(info)) for info in source.infolist()]
    with zipfile.ZipFile(out, "w") as target:
        for info, data in records:
            target.writestr(info, data, compresslevel=0)


def test_integrity_accepts_only_compression_representation_difference(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    names = archive_builder.build_skill_archive(root, out)
    assert archive_builder.verify_zip(out, source=root, verification="integrity") == names
    canonical = out.read_bytes()
    _reencode(out)
    assert out.read_bytes() != canonical
    assert archive_builder.verify_zip(out, source=root, verification="integrity") == names
    with pytest.raises(archive_builder.SecretScrubError, match="complete archive bytes differ"):
        archive_builder.verify_zip(out, source=root)


def test_verify_cli_is_explicit_read_only_and_defaults_to_reproducible(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out)
    _reencode(out)
    prior = out.read_bytes()
    base = [sys.executable, str(SCRIPT), "--source", str(root), "--verify", str(out)]
    strict = subprocess.run(base, capture_output=True, text=True)
    assert strict.returncode == 2
    assert "complete archive bytes differ" in strict.stderr
    integrity = subprocess.run(base + ["--verification", "integrity"], capture_output=True, text=True)
    assert integrity.returncode == 0, integrity.stderr
    assert "verified (integrity)" in integrity.stdout
    assert "wrote" not in integrity.stdout
    unknown = subprocess.run(base + ["--verification", "unknown"], capture_output=True, text=True)
    assert unknown.returncode == 2
    assert "invalid choice" in unknown.stderr
    build = subprocess.run(
        [sys.executable, str(SCRIPT), "--source", str(root), "--out", str(out),
         "--verification", "integrity"], capture_output=True, text=True,
    )
    assert build.returncode == 2
    assert "requires --verify" in build.stderr
    assert out.read_bytes() == prior
    assert _archive_temporaries(out) == []
    conflict = subprocess.run(base + ["--out", str(out)], capture_output=True, text=True)
    assert conflict.returncode == 2
    assert out.read_bytes() == prior
    _rewrite(out, lambda records: records.pop())
    tampered = out.read_bytes()
    rejected = subprocess.run(base + ["--verification", "integrity"], capture_output=True, text=True)
    assert rejected.returncode == 2
    assert "missing entries" in rejected.stderr
    assert out.read_bytes() == tampered


@pytest.mark.parametrize("mode", ["", " ", "unknown", "INTEGRITY", None, False, [], {}])
def test_verify_rejects_unknown_mode_before_accessing_source(tmp_path: Path, mode) -> None:
    with pytest.raises(archive_builder.SecretScrubError, match="unsupported archive verification mode"):
        archive_builder.verify_zip(tmp_path / "absent.zip", source=tmp_path, verification=mode)


def test_integrity_cannot_disable_secret_scan(tmp_path: Path) -> None:
    with pytest.raises(archive_builder.SecretScrubError, match="requires content scanning"):
        archive_builder.verify_zip(tmp_path / "absent.zip", verification="integrity", scan_contents=False)


def _replace_first_deflate(out: Path, transform) -> None:
    """Rewrite one compressed region and keep all size/offset claims consistent."""
    with zipfile.ZipFile(out) as archive:
        first = archive.infolist()[0]
        central = archive.start_dir
    raw = bytearray(out.read_bytes())
    start = 30 + struct.unpack_from("<H", raw, 26)[0]
    end = start + first.compress_size
    replacement = transform(bytes(raw[start:end]))
    delta = len(replacement) - first.compress_size
    raw[start:end] = replacement
    struct.pack_into("<L", raw, 18, len(replacement))
    cursor = central + delta
    while raw[cursor:cursor + 4] == b"PK\x01\x02":
        offset = struct.unpack_from("<L", raw, cursor + 42)[0]
        if offset == 0:
            struct.pack_into("<L", raw, cursor + 20, len(replacement))
        else:
            struct.pack_into("<L", raw, cursor + 42, offset + delta)
        cursor += 46 + sum(struct.unpack_from("<3H", raw, cursor + 28))
    struct.pack_into("<L", raw, cursor + 16, central + delta)
    out.write_bytes(raw)


@pytest.mark.parametrize("verification", ["reproducible", "integrity"])
@pytest.mark.parametrize("tamper", [
    "trailing_bytes", "second_stream", "truncated_stream", "invalid_stream", "excess_output", "empty_stream",
])
def test_verify_rejects_unconsumed_or_malformed_deflate(
    tmp_path: Path, verification: str, tamper: str,
) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out)
    transforms = {
        "trailing_bytes": lambda data: data + b"unaccounted",
        "second_stream": lambda data: data + zlib.compress(b"hidden", wbits=-15),
        "truncated_stream": lambda data: data[:-1],
        "invalid_stream": lambda data: b"\x07" + data[1:],
        "excess_output": lambda data: zlib.compress(b"tracked\n" + b"hidden" * 100, wbits=-15),
        "empty_stream": lambda data: b"",
    }
    _replace_first_deflate(out, transforms[tamper])
    if tamper in {"trailing_bytes", "second_stream"}:
        # Standard ZIP parsing accepts this; integrity must not.
        with zipfile.ZipFile(out) as archive:
            assert archive.read("skill/README.md") == b"tracked\n"
    with pytest.raises(archive_builder.SecretScrubError, match="release parity"):
        archive_builder.verify_zip(out, source=root, verification=verification)


@pytest.mark.parametrize("verification", ["reproducible", "integrity"])
@pytest.mark.parametrize("tamper", [
    "gap", "intermember_gap", "leading_gap", "hidden_local", "duplicate_local",
    "overlap", "duplicate_offset", "central_gap",
])
def test_verify_requires_complete_physical_coverage(
    tmp_path: Path, verification: str, tamper: str,
) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out)
    with zipfile.ZipFile(out) as archive:
        infos = archive.infolist()
        central = archive.start_dir
    raw = bytearray(out.read_bytes())
    second_central = central + 46 + len(infos[0].filename)
    if tamper in {"intermember_gap", "leading_gap"}:
        at = infos[1].header_offset if tamper == "intermember_gap" else 0
        raw[at:at] = b"gap"
        for index, info in enumerate(infos):
            if info.header_offset >= at:
                header = central if index == 0 else second_central
                struct.pack_into("<L", raw, header + 3 + 42, info.header_offset + 3)
        struct.pack_into("<L", raw, len(raw) - 6, central + 3)
    elif tamper in {"gap", "hidden_local", "duplicate_local"}:
        if tamper == "gap":
            inserted = b"unaccounted"
        else:
            inserted = bytes(raw[:infos[1].header_offset])
            if tamper == "hidden_local":
                inserted = inserted.replace(b"README.md", b"HIDDEN.md")
        raw[central:central] = inserted
        struct.pack_into("<L", raw, len(raw) - 6, central + len(inserted))
    elif tamper == "overlap":
        size = infos[0].compress_size + 1
        struct.pack_into("<L", raw, 18, size)
        struct.pack_into("<L", raw, central + 20, size)
    elif tamper == "duplicate_offset":
        struct.pack_into("<L", raw, second_central + 42, 0)
    else:
        raw[-22:-22] = b"gap"
        size = struct.unpack_from("<L", raw, len(raw) - 10)[0]
        struct.pack_into("<L", raw, len(raw) - 10, size + 3)
    out.write_bytes(raw)
    with pytest.raises(archive_builder.SecretScrubError, match="release parity"):
        archive_builder.verify_zip(out, source=root, verification=verification)


@pytest.mark.parametrize("verification", ["reproducible", "integrity"])
@pytest.mark.parametrize(("location", "offset", "fmt", "value"), [
    ("local", 4, "H", 21),  # extraction version
    ("local", 8, "H", zipfile.ZIP_STORED),
    ("local", 10, "H", 1),  # time
    ("local", 12, "H", 34),  # date
    ("local", 14, "L", 0),  # CRC
    ("local", 18, "L", 1),  # compressed size
    ("local", 22, "L", 1),  # uncompressed size
    ("local", 26, "H", 1),  # name length
    ("local", 30, "B", 0),  # name bytes
    ("central", 4, "H", 0x0315),  # creator version
    ("central", 6, "H", 21),  # extraction version
    ("central", 10, "H", zipfile.ZIP_STORED),
    ("central", 12, "H", 1),
    ("central", 14, "H", 34),
    ("central", 16, "L", 0),
    ("central", 20, "L", 0xffffffff),  # ZIP64 sentinel
    ("central", 24, "L", 1),
    ("central", 34, "H", 1),  # disk start
    ("central", 36, "H", 1),  # internal attributes
    ("central", 38, "L", (0o100644 << 16) | 1),  # DOS attributes
    ("central", 46, "B", 0),
])
def test_verify_rejects_raw_metadata_tamper(
    tmp_path: Path, verification: str, location: str, offset: int, fmt: str, value: int,
) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out)
    raw = bytearray(out.read_bytes())
    start = 0 if location == "local" else raw.index(b"PK\x01\x02")
    struct.pack_into("<" + fmt, raw, start + offset, value)
    out.write_bytes(raw)
    with pytest.raises(archive_builder.SecretScrubError, match="release parity"):
        archive_builder.verify_zip(out, source=root, verification=verification)


def test_integrity_does_not_count_signatures_inside_valid_payloads(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "unicode-λ.txt").write_bytes(b"PK\x03\x04PK\x01\x02PK\x05\x06" + b"\0" * 22)
    (root / "empty.txt").write_bytes(b"")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "payload signatures")
    out = tmp_path / "skill.zip"
    names = archive_builder.build_skill_archive(root, out)
    canonical_count = out.read_bytes().count(b"PK\x05\x06")
    _reencode(out)
    assert out.read_bytes().count(b"PK\x05\x06") > canonical_count
    assert archive_builder.verify_zip(out, source=root, verification="integrity") == names


@pytest.mark.parametrize("verification", ["reproducible", "integrity"])
def test_verification_is_bound_to_selected_commit_not_checkout(tmp_path: Path, verification: str) -> None:
    root = _repo(tmp_path)
    commit = _git(root, "rev-parse", "HEAD")
    out = tmp_path / "skill.zip"
    names = archive_builder.build_skill_archive(root, out)
    (root / "README.md").write_bytes(b"new tracked data\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "later revision")
    # Neither current HEAD nor dirty checkout bytes override selected Git blobs.
    (root / "README.md").write_bytes(b"dirty\n")
    assert archive_builder.verify_zip(out, source=root, revision=commit, verification=verification) == names
    with pytest.raises(archive_builder.SecretScrubError, match="release parity"):
        archive_builder.verify_zip(out, source=root, verification=verification)


@pytest.mark.parametrize("verification", ["reproducible", "integrity"])
def test_empty_inventory_still_requires_complete_zip_coverage(tmp_path: Path, verification: str) -> None:
    root = _repo(tmp_path)
    _git(root, "rm", "-qr", ".")
    _git(root, "commit", "-qm", "empty tree")
    out = tmp_path / "skill.zip"
    assert archive_builder.build_skill_archive(root, out) == []
    assert archive_builder.verify_zip(out, source=root, verification=verification) == []
    out.write_bytes(b"hidden" + out.read_bytes())
    with pytest.raises(archive_builder.SecretScrubError, match="release parity"):
        archive_builder.verify_zip(out, source=root, verification=verification)


def test_build_rejects_integrity_only_archive_before_publication(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out)
    prior = out.read_bytes()
    original = archive_builder._write_canonical_zip

    def different_build_encoding(path, *args):
        original(path, *args)
        if path.parent == out.parent:
            _reencode(path)
            archive_builder.verify_zip(path, source=root, verification="integrity")

    monkeypatch.setattr(archive_builder, "_write_canonical_zip", different_build_encoding)
    with pytest.raises(archive_builder.SecretScrubError, match="complete archive bytes differ"):
        archive_builder.build_skill_archive(root, out)
    assert out.read_bytes() == prior
    assert _archive_temporaries(out) == []


def _archive_temporaries(out: Path) -> list[Path]:
    return list(out.parent.glob(f".{out.name}.*"))


def _archive_recoveries(out: Path) -> list[Path]:
    return sorted(out.parent.glob(f".{out.name}.recovery-*"))


def test_workspace_contamination_and_dirty_gitignore_cannot_enter_archive(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    captured = _git(root, "rev-parse", "HEAD")
    # Dirty tracked bytes, ignored/untracked files, a stale nested checkout, and
    # local tool state all exist only in the mutable operator workspace.
    (root / "README.md").write_text("DIRTY\n", encoding="utf-8")
    (root / ".gitignore").write_text("!uv.lock\n", encoding="utf-8")
    (root / "uv.lock").write_text("local lock\n", encoding="utf-8")
    (root / ".playwright-mcp").mkdir()
    (root / ".playwright-mcp" / "trace.json").write_text("local", encoding="utf-8")
    stale = root / "docs" / "design" / "incoming-v1-theme"
    stale.mkdir(parents=True)
    _git(stale, "init", "-q")
    (stale / "stale.txt").write_text("stale", encoding="utf-8")

    one = tmp_path / "one.zip"
    two = tmp_path / "two.zip"
    names = archive_builder.build_skill_archive(root, one, revision=captured)
    archive_builder.build_skill_archive(root, two, revision=captured)

    assert one.read_bytes() == two.read_bytes()
    assert names == ["skill/README.md", "skill/scripts/run"]
    with zipfile.ZipFile(one) as built:
        assert built.read("skill/README.md") == b"tracked\n"
    assert archive_builder.verify_zip(one, source=root, revision=captured) == names


def test_release_inventory_rejects_symlinks_gitlinks_and_special_modes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    os.symlink("README.md", root / "link")
    _git(root, "add", "link")
    _git(root, "commit", "-qm", "symlink")
    with pytest.raises(archive_builder.SecretScrubError, match="unsupported tracked entry"):
        archive_builder.release_inventory(root)


def test_modes_are_commit_derived_and_preserved(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out)
    with zipfile.ZipFile(out) as built:
        info = built.getinfo("skill/scripts/run")
    assert stat.S_ISREG(info.external_attr >> 16)
    assert (info.external_attr >> 16) & 0o777 == 0o755


@pytest.mark.parametrize(
    "tamper",
    ["extra", "missing", "bytes", "mode", "name", "traversal", "duplicate", "excluded", "order"],
)
@pytest.mark.parametrize("verification", ["reproducible", "integrity"])
def test_verify_rejects_every_archive_parity_tamper(tmp_path: Path, tamper: str, verification: str) -> None:
    root = _repo(tmp_path)
    commit = _git(root, "rev-parse", "HEAD")
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out, revision=commit)

    def mutate(records):
        if tamper == "extra":
            info = zipfile.ZipInfo("skill/extra.txt", (1980, 1, 1, 0, 0, 0))
            info.create_system = 3; info.external_attr = 0o100644 << 16; info.compress_type = zipfile.ZIP_DEFLATED
            records.append((info, b"extra"))
        elif tamper == "missing":
            records.pop()
        elif tamper == "bytes":
            info, _ = records[0]; records[0] = (info, b"changed")
        elif tamper == "mode":
            records[0][0].external_attr = 0o100755 << 16
        elif tamper == "name":
            records[0][0].filename = "skill/renamed.md"
        elif tamper == "traversal":
            info = zipfile.ZipInfo("skill/../escape", (1980, 1, 1, 0, 0, 0))
            info.create_system = 3; info.external_attr = 0o100644 << 16; info.compress_type = zipfile.ZIP_DEFLATED
            records.append((info, b"escape"))
        elif tamper == "excluded":
            records[0][0].filename = "skill/.env"
        elif tamper == "order":
            records.reverse()
        else:
            records.append(records[0])

    _rewrite(out, mutate)
    with pytest.raises(archive_builder.SecretScrubError, match="release parity"):
        archive_builder.verify_zip(out, source=root, revision=commit, verification=verification)


@pytest.mark.parametrize(
    ("tamper", "diagnostic"),
    [
        ("archive_comment", "archive comment"),
        ("entry_comment", "entry comment"),
        ("local_extra", "local-header extra field"),
        ("central_extra", "central-directory extra field"),
        ("unsupported_flags", "unsupported general-purpose flags"),
        ("encrypted", "encrypted entry"),
        ("descriptor", "data descriptor"),
        ("prepended", "prepended bytes"),
        ("trailing", "trailing bytes"),
        ("duplicate_eocd", "EOCD|central.directory"),
    ],
)
@pytest.mark.parametrize("verification", ["reproducible", "integrity"])
def test_verify_rejects_normalized_zip_header_tamper(
    tmp_path: Path, tamper: str, diagnostic: str, verification: str,
) -> None:
    root = _repo(tmp_path)
    commit = _git(root, "rev-parse", "HEAD")
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out, revision=commit)

    if tamper == "archive_comment":
        pem = (
            b"-----BEGIN PRIVATE KEY-----\n"
            b"b2tmLXRlc3QtYXJjaGl2ZS1jb21tZW50\n"
            b"-----END PRIVATE KEY-----\n"
        )
        with zipfile.ZipFile(out, "a") as archive:
            archive.comment = pem
    elif tamper == "entry_comment":
        def mutate(records):
            records[0][0].comment = b"not canonical"

        _rewrite(out, mutate)
    elif tamper in {"local_extra", "central_extra"}:
        raw = bytearray(out.read_bytes())
        extra = b"\xfe\xca\x02\x00xx"
        if tamper == "local_extra":
            central = raw.index(b"PK\x01\x02")
            local = raw.rfind(b"PK\x03\x04", 0, central)
            name_len = struct.unpack_from("<H", raw, local + 26)[0]
            old_len = struct.unpack_from("<H", raw, local + 28)[0]
            struct.pack_into("<H", raw, local + 28, old_len + len(extra))
            raw[local + 30 + name_len:local + 30 + name_len] = extra
            eocd = raw.rfind(b"PK\x05\x06")
            old_offset = struct.unpack_from("<L", raw, eocd + 16)[0]
            struct.pack_into("<L", raw, eocd + 16, old_offset + len(extra))
        else:
            central = raw.index(b"PK\x01\x02")
            name_len = struct.unpack_from("<H", raw, central + 28)[0]
            old_len = struct.unpack_from("<H", raw, central + 30)[0]
            struct.pack_into("<H", raw, central + 30, old_len + len(extra))
            raw[central + 46 + name_len:central + 46 + name_len] = extra
            eocd = raw.rfind(b"PK\x05\x06")
            old_size = struct.unpack_from("<L", raw, eocd + 12)[0]
            struct.pack_into("<L", raw, eocd + 12, old_size + len(extra))
        out.write_bytes(raw)
    elif tamper in {"unsupported_flags", "encrypted", "descriptor"}:
        raw = bytearray(out.read_bytes())
        flag = {"unsupported_flags": 0x20, "encrypted": 0x1, "descriptor": 0x8}[tamper]
        local = raw.index(b"PK\x03\x04")
        central = raw.index(b"PK\x01\x02")
        struct.pack_into("<H", raw, local + 6, struct.unpack_from("<H", raw, local + 6)[0] | flag)
        struct.pack_into("<H", raw, central + 8, struct.unpack_from("<H", raw, central + 8)[0] | flag)
        out.write_bytes(raw)
    elif tamper == "prepended":
        out.write_bytes(b"junk" + out.read_bytes())
    elif tamper == "trailing":
        with out.open("ab") as stream:
            stream.write(b"junk")
    else:
        raw = out.read_bytes()
        eocd = raw.rfind(b"PK\x05\x06")
        out.write_bytes(raw + raw[eocd:])

    with pytest.raises(archive_builder.SecretScrubError, match=diagnostic):
        archive_builder.verify_zip(out, source=root, revision=commit, verification=verification)


@pytest.mark.parametrize("verification", ["reproducible", "integrity"])
def test_raw_archive_comment_secret_is_reported(tmp_path: Path, verification: str) -> None:
    root = _repo(tmp_path)
    commit = _git(root, "rev-parse", "HEAD")
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out, revision=commit)
    with zipfile.ZipFile(out, "a") as archive:
        archive.comment = (
            b"-----BEGIN RSA PRIVATE KEY-----\n"
            b"b2tmLXRlc3QtcmF3LXNlY3JldA==\n"
            b"-----END RSA PRIVATE KEY-----\n"
        )

    with pytest.raises(archive_builder.SecretScrubError, match="raw archive bytes"):
        archive_builder.verify_zip(out, source=root, revision=commit, verification=verification)


@pytest.mark.parametrize("path", ["notes.txt", "tokenize.py", "private_api.md", "uv.lock"])
@pytest.mark.parametrize("verification", ["reproducible", "integrity"])
def test_content_scan_rejects_private_key_material(tmp_path: Path, capsys, path: str, verification: str) -> None:
    root = _repo(tmp_path)
    payload = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "b2tmLXRlc3Qta2V5LW1hdGVyaWFsAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    (root / path).write_text(payload, encoding="utf-8")
    _git(root, "add", path)
    _git(root, "commit", "-qm", "key fixture")
    out = tmp_path / "skill.zip"
    assert archive_builder.main(["--source", str(root), "--out", str(out)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "content scan failed" in captured.err
    assert not any(line in captured.err for line in payload.splitlines())
    assert not out.exists()
    assert _archive_temporaries(out) == []
    # A self-consistent, commit-matching archive is still subject to scanning.
    _, inventory = archive_builder.release_inventory(root)
    archive_builder._write_canonical_zip(out, inventory, root.name, archive_builder._FIXED_TIMESTAMP)
    with pytest.raises(archive_builder.SecretScrubError, match="content scan failed") as raised:
        archive_builder.verify_zip(out, source=root, verification=verification)
    assert not any(line in str(raised.value) for line in payload.splitlines())


def test_failed_secret_rebuild_preserves_prior_archive(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out)
    prior = out.read_bytes()
    payload = (
        "-----BEGIN PRIVATE KEY-----\n"
        "b2tmLXRlc3QtcHJpb3ItYXJjaGl2ZQ==\n"
        "-----END PRIVATE KEY-----\n"
    )
    (root / "notes.txt").write_text(payload, encoding="utf-8")
    _git(root, "add", "notes.txt")
    _git(root, "commit", "-qm", "key fixture")

    with pytest.raises(archive_builder.SecretScrubError, match="content scan failed"):
        archive_builder.build_skill_archive(root, out)

    assert out.read_bytes() == prior
    assert _archive_temporaries(out) == []


@pytest.mark.parametrize("prior_exists", [False, True])
def test_failed_structural_verification_does_not_publish(
    tmp_path: Path, monkeypatch, prior_exists: bool,
) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    prior = None
    if prior_exists:
        archive_builder.build_skill_archive(root, out)
        prior = out.read_bytes()

    def write_invalid(path, *_args):
        path.write_bytes(b"not a zip")

    monkeypatch.setattr(archive_builder, "_write_canonical_zip", write_invalid)
    with pytest.raises(archive_builder.SecretScrubError, match="release parity"):
        archive_builder.build_skill_archive(root, out)

    if prior is None:
        assert not out.exists()
    else:
        assert out.read_bytes() == prior
    assert _archive_temporaries(out) == []


@pytest.mark.parametrize("failure", ["fsync_file", "replace"])
def test_publication_failure_preserves_prior_archive(
    tmp_path: Path, monkeypatch, failure: str,
) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out)
    prior = out.read_bytes()
    original_replace = archive_builder.os.replace

    if failure == "fsync_file":
        monkeypatch.setattr(
            archive_builder, "_fsync_file",
            lambda _path: (_ for _ in ()).throw(OSError("injected file fsync failure")),
        )
    else:
        def fail_replace(source, destination):
            if Path(destination) == out:
                raise OSError("injected replace failure")
            return original_replace(source, destination)

        monkeypatch.setattr(archive_builder.os, "replace", fail_replace)

    with pytest.raises(archive_builder.SecretScrubError, match="publication failed"):
        archive_builder.build_skill_archive(root, out)

    assert out.read_bytes() == prior
    assert _archive_temporaries(out) == []


@pytest.mark.parametrize("failure", ["file", "parent"])
def test_post_replace_fsync_failure_rolls_back_published_archive(
    tmp_path: Path, monkeypatch, failure: str,
) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out)
    prior = out.read_bytes()
    (root / "new.txt").write_text("new\n", encoding="utf-8")
    _git(root, "add", "new.txt")
    _git(root, "commit", "-qm", "new archive")
    calls = 0

    def fail_selected_fsync(_path):
        nonlocal calls
        calls += 1
        fail_at = 2
        if calls == fail_at:
            raise OSError(f"injected {failure} fsync failure")

    monkeypatch.setattr(
        archive_builder, "_fsync_file" if failure == "file" else "_fsync_dir",
        fail_selected_fsync,
    )
    with pytest.raises(archive_builder.SecretScrubError, match="publication failed"):
        archive_builder.build_skill_archive(root, out)

    assert out.read_bytes() == prior
    assert calls == (2 if failure == "file" else 3)
    assert _archive_temporaries(out) == []


def test_failed_post_replace_rollback_retains_exact_prior_recovery(
    tmp_path: Path, monkeypatch,
) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out)
    prior = out.read_bytes()
    prior_mode = stat.S_IMODE(out.stat().st_mode)
    unrelated = tmp_path / f".{out.name}.operator-note"
    unrelated.write_text("keep\n", encoding="utf-8")
    (root / "new.txt").write_text("new\n", encoding="utf-8")
    _git(root, "add", "new.txt")
    _git(root, "commit", "-qm", "new archive")
    original_replace = archive_builder.os.replace
    replace_calls = 0
    fsync_calls = 0

    def fail_published_fsync(_path):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("injected published fsync failure")

    def fail_rollback_replace(source, destination):
        nonlocal replace_calls
        if Path(destination) == out:
            replace_calls += 1
            if replace_calls == 2:
                raise OSError("injected rollback replace failure")
        return original_replace(source, destination)

    monkeypatch.setattr(archive_builder, "_fsync_file", fail_published_fsync)
    monkeypatch.setattr(archive_builder.os, "replace", fail_rollback_replace)

    with pytest.raises(archive_builder.SecretScrubError) as raised:
        archive_builder.build_skill_archive(root, out)

    recoveries = _archive_recoveries(out)
    assert len(recoveries) == 1
    assert recoveries[0].read_bytes() == prior
    assert stat.S_IMODE(recoveries[0].stat().st_mode) == prior_mode
    assert os.fspath(recoveries[0]) in str(raised.value)
    assert unrelated.read_text(encoding="utf-8") == "keep\n"


def test_rollback_restoration_fsync_failure_retains_recovery(
    tmp_path: Path, monkeypatch,
) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out)
    prior = out.read_bytes()
    (root / "new.txt").write_text("new\n", encoding="utf-8")
    _git(root, "add", "new.txt")
    _git(root, "commit", "-qm", "new archive")
    file_calls = 0
    dir_calls = 0

    def fail_published_fsync(_path):
        nonlocal file_calls
        file_calls += 1
        if file_calls == 2:
            raise OSError("injected published fsync failure")

    def fail_rollback_fsync(_path):
        nonlocal dir_calls
        dir_calls += 1
        if dir_calls >= 2:
            raise OSError("injected rollback fsync failure")

    monkeypatch.setattr(archive_builder, "_fsync_file", fail_published_fsync)
    monkeypatch.setattr(archive_builder, "_fsync_dir", fail_rollback_fsync)

    with pytest.raises(archive_builder.SecretScrubError) as raised:
        archive_builder.build_skill_archive(root, out)

    recoveries = _archive_recoveries(out)
    assert out.read_bytes() == prior
    assert len(recoveries) == 1
    assert recoveries[0].read_bytes() == prior
    assert os.fspath(recoveries[0]) in str(raised.value)
    assert "recovery parent fsync failed" in str(raised.value)


def test_repeated_compound_failures_use_unique_recovery_slots(
    tmp_path: Path, monkeypatch,
) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out)
    expected_recoveries = []
    original_replace = archive_builder.os.replace
    file_calls = 0
    replace_calls = 0

    def fail_each_published_fsync(_path):
        nonlocal file_calls
        file_calls += 1
        if file_calls % 2 == 0:
            raise OSError("injected published fsync failure")

    def fail_each_rollback_replace(source, destination):
        nonlocal replace_calls
        if Path(destination) == out:
            replace_calls += 1
            if replace_calls % 2 == 0:
                raise OSError("injected rollback replace failure")
        return original_replace(source, destination)

    monkeypatch.setattr(archive_builder, "_fsync_file", fail_each_published_fsync)
    monkeypatch.setattr(archive_builder.os, "replace", fail_each_rollback_replace)

    for generation in range(2):
        expected_recoveries.append(out.read_bytes())
        (root / f"new-{generation}.txt").write_text("new\n", encoding="utf-8")
        _git(root, "add", f"new-{generation}.txt")
        _git(root, "commit", "-qm", f"archive generation {generation}")
        with pytest.raises(archive_builder.SecretScrubError) as raised:
            archive_builder.build_skill_archive(root, out)
        assert os.fspath(_archive_recoveries(out)[generation]) in str(raised.value)

    recoveries = _archive_recoveries(out)
    assert [path.name for path in recoveries] == [
        f".{out.name}.recovery-01", f".{out.name}.recovery-02",
    ]
    assert [path.read_bytes() for path in recoveries] == expected_recoveries


def test_full_recovery_slots_block_without_overwriting_or_publishing(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out)
    prior = out.read_bytes()
    sentinels = []
    for slot in range(1, archive_builder._MAX_RECOVERY_BACKUPS + 1):
        recovery = tmp_path / f".{out.name}.recovery-{slot:02d}"
        recovery.write_bytes(f"operator recovery {slot}\n".encode())
        sentinels.append(recovery.read_bytes())
    (root / "new.txt").write_text("new\n", encoding="utf-8")
    _git(root, "add", "new.txt")
    _git(root, "commit", "-qm", "new archive")

    with pytest.raises(archive_builder.SecretScrubError, match="recovery slots.*occupied"):
        archive_builder.build_skill_archive(root, out)

    assert out.read_bytes() == prior
    assert [path.read_bytes() for path in _archive_recoveries(out)] == sentinels
    assert not list(tmp_path.glob(f".{out.name}.build-*"))


def test_post_replace_fsync_failure_restores_absent_output(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    calls = 0

    def fail_first_parent_fsync(_path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected parent fsync failure")

    monkeypatch.setattr(archive_builder, "_fsync_dir", fail_first_parent_fsync)
    with pytest.raises(archive_builder.SecretScrubError, match="publication failed"):
        archive_builder.build_skill_archive(root, out)

    assert not out.exists()
    assert calls == 2
    assert _archive_temporaries(out) == []


def test_symlink_destination_keeps_current_resolved_target_contract(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    target = tmp_path / "target.zip"
    declared = tmp_path / "declared.zip"
    declared.symlink_to(target.name)

    names = archive_builder.build_skill_archive(root, declared)

    assert declared.is_symlink()
    assert archive_builder.verify_zip(target, source=root) == names


def test_successful_replacement_is_atomic_and_concurrent_builders_do_not_collide(
    tmp_path: Path, monkeypatch,
) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    out.write_bytes(b"prior archive bytes")
    original_replace = archive_builder.os.replace
    observations = []

    def observe_replace(source, destination):
        if Path(destination) == out:
            observations.append((out.read_bytes(), archive_builder.verify_zip(
                source, source=root,
            )))
        return original_replace(source, destination)

    monkeypatch.setattr(archive_builder.os, "replace", observe_replace)
    with ThreadPoolExecutor(max_workers=2) as workers:
        manifests = list(workers.map(
            lambda _: archive_builder.build_skill_archive(root, out), range(2),
        ))

    assert manifests[0] == manifests[1]
    assert len(observations) == 2
    assert observations[0][0] == b"prior archive bytes"
    assert archive_builder.verify_zip(out, source=root) == manifests[0]
    assert _archive_temporaries(out) == []


def test_safe_names_and_dependency_locks_survive_and_launcher_imports(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    # Only the launcher is copied from the checkout. Everything packaged is a
    # deliberately authored temporary fixture, not the real workspace inventory.
    files = {
        "scripts/okf-loom": (ROOT / "scripts" / "okf-loom").read_text(),
        "scripts/okf_loom/__init__.py": "",
        "scripts/okf_loom/__main__.py": (
            "from .tokenize import split\n"
            "from .private_api import label\n"
            "from .private.secret_helpers import SAFE\n"
            "assert SAFE\n"
            "assert split('archive works') == ['archive', 'works']\n"
            "print(label)\n"
        ),
        "scripts/okf_loom/tokenize.py": "def split(text): return text.split()\n",
        "scripts/okf_loom/private_api.py": "label = 'archive-import-ok'\n",
        "scripts/okf_loom/private/secret_helpers.py": "SAFE = True\n",
        "docs/private_api.md": "# Private API documentation\n",
        "docs/tokenization.md": "# Tokenization\n",
        "uv.lock": 'version = 1\nrequires-python = ">=3.11"\n[[package]]\nname = "demo"\nversion = "1.0.0"\nsource = { virtual = "." }\n',
        "package-lock.json": '{"name":"demo","version":"1.0.0","lockfileVersion":3,"packages":{"":{"name":"demo","version":"1.0.0"}}}\n',
        "npm-shrinkwrap.json": '{"name":"demo","version":"1.0.0","lockfileVersion":3,"packages":{"":{"name":"demo","version":"1.0.0"}}}\n',
        "yarn.lock": '# yarn lockfile v1\n\nis-number@7.0.0:\n  version "7.0.0"\n  resolved "https://registry.yarnpkg.com/is-number/-/is-number-7.0.0.tgz"\n',
        "pnpm-lock.yaml": "lockfileVersion: '9.0'\nimporters:\n  .:\n    dependencies:\n      is-number:\n        specifier: 7.0.0\n        version: 7.0.0\npackages:\n  is-number@7.0.0: {}\nsnapshots:\n  is-number@7.0.0: {}\n",
        "poetry.lock": '[[package]]\nname = "isort"\nversion = "5.13.2"\ndescription = "Import sorting"\noptional = false\npython-versions = ">=3.8"\nfiles = []\n[metadata]\nlock-version = "2.0"\npython-versions = "^3.11"\ncontent-hash = "fixture"\n',
        "Pipfile.lock": '{"_meta":{"hash":{"sha256":"fixture"},"pipfile-spec":6,"requires":{"python_version":"3.11"},"sources":[{"name":"pypi","url":"https://pypi.org/simple","verify_ssl":true}]},"default":{"isort":{"version":"==5.13.2"}},"develop":{}}\n',
    }
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    (root / "scripts" / "okf-loom").chmod(0o755)
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "safe distribution fixture")
    out = tmp_path / "skill.zip"
    names = archive_builder.build_skill_archive(root, out)
    # An explicit inventory and an actual import are independent of the filter
    # shared by builder/verifier: self-consistent omission must not pass.
    assert set(names) == {f"skill/{path}" for path in files} | {
        "skill/README.md", "skill/scripts/run",
    }
    assert archive_builder.verify_zip(out, source=root) == names
    extracted = tmp_path / "extracted"
    launcher = extracted / "skill/scripts/okf-loom"
    with zipfile.ZipFile(out) as built:
        for path, text in files.items():
            assert built.read(f"skill/{path}") == text.encode()
        built.extractall(extracted)
        # zipfile extraction does not restore Unix permissions; apply the mode
        # carried by the archive, as a Unix-aware extractor would.
        launcher.chmod((built.getinfo("skill/scripts/okf-loom").external_attr >> 16) & 0o777)
    env = {**os.environ, "PYTHON": sys.executable, "PYTHONPATH": "", "PYTHONNOUSERSITE": "1"}
    proc = subprocess.run(
        [str(launcher)], cwd=tmp_path,
        env=env, text=True, capture_output=True, check=True,
    )
    assert proc.stdout == "archive-import-ok\n"


def test_vendored_licence_notices_ship_in_the_skill_archive(tmp_path: Path) -> None:
    """The offline libraries' licences and notices are release artifacts, byte for byte."""
    vendor_rel = "scripts/okf_loom/viewer/vendor"
    lock = json.loads((ROOT / vendor_rel / "vendor.lock.json").read_text(encoding="utf-8"))
    licences = [entry["dest"] for package in lock["packages"] for entry in package["files"]
                if entry["dest"].rsplit("/", 1)[-1] == "LICENSE"]
    notices = [entry["dest"] for entry in lock["auxiliary"] if entry["role"] == "notices"]
    assert len(licences) == len(lock["packages"]) == 11
    assert {"THIRD-PARTY-NOTICES.txt", "BUNDLED-DEPENDENCY-NOTICES.txt"} <= set(notices)
    paths = [f"{vendor_rel}/{dest}" for dest in (*licences, *notices)]
    root = _repo(tmp_path)
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / path).read_bytes())
    _git(root, "add", "--", *paths)
    _git(root, "commit", "-qm", "licence notices fixture")
    out = tmp_path / "skill.zip"
    names = archive_builder.build_skill_archive(root, out)
    assert set(names) == {f"skill/{path}" for path in paths} | {"skill/README.md", "skill/scripts/run"}
    with zipfile.ZipFile(out) as built:
        for path in paths:
            assert built.read(f"skill/{path}") == (ROOT / path).read_bytes(), path


def test_concrete_sensitive_state_and_credentials_are_not_published(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    forbidden = [
        ".aic/state.json", ".playwright-mcp/session.json",
        "bundle/.okf-loom/session/.token", "bundle/.okf-loom/proposals/state.json",
        "nested/.aws/credentials", "nested/.ssh/config", "nested/.kube/config",
        "nested/.gnupg/state", "nested/.docker/config.json", ".env", ".env.production",
        ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".token",
        "id_ed25519", "server.pem", "server.key", "client.p12", "secrets.yaml",
        "service_account-prod.json", "docs/incoming-v1-theme/old.md",
        "docs/ui-overhaul-final/old.md",
    ]
    for path in forbidden:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("synthetic-sensitive-state\n", encoding="utf-8")
    _git(root, "add", "-f", "--", *forbidden)
    _git(root, "commit", "-qm", "sensitive fixture")
    out = tmp_path / "skill.zip"
    assert archive_builder.build_skill_archive(root, out) == [
        "skill/README.md", "skill/scripts/run",
    ]
    assert archive_builder.is_path_denied("nested/.git/config")


def test_readme_media_stays_in_the_repository_not_the_skill_archive(tmp_path: Path) -> None:
    """Root docs/media/ (README screenshots, GIFs and clips) is repository-only.

    Other docs, a nested bundle's own docs/media/, and "media" names elsewhere
    still ship, and the archive still verifies against the same commit.
    """
    root = _repo(tmp_path)
    media = ["docs/media/graph.mp4", "docs/media/graph.gif", "docs/media/stills/graph-map.png"]
    kept = ["docs/guide.md", "docs/media.md", "samples/demo/docs/media/figure.png", "scripts/media/helper.py"]
    for path in (*media, *kept):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.encode("utf-8") + b"\n")
    _git(root, "add", "--", *media, *kept)
    _git(root, "commit", "-qm", "media fixture")
    captured = _git(root, "rev-parse", "HEAD")
    out = tmp_path / "skill.zip"
    names = archive_builder.build_skill_archive(root, out, revision=captured)
    assert sorted(names) == sorted(["skill/README.md", "skill/scripts/run", *(f"skill/{path}" for path in kept)])
    with zipfile.ZipFile(out) as built:
        assert not [name for name in built.namelist() if name.startswith("skill/docs/media/")]
        assert built.read("skill/samples/demo/docs/media/figure.png") == b"samples/demo/docs/media/figure.png\n"
    assert archive_builder.verify_zip(out, source=root, revision=captured) == names


@pytest.mark.parametrize("path", [
    "deploy/access-token.json", "private-backup.txt", "credentials.csv",
    "settings/api_key.toml", "secrets", "passwords.db",
    "exports/credentials/customer.json", "nested/.secrets/data.bin",
])
@pytest.mark.parametrize("verification", ["reproducible", "integrity"])
def test_unhandled_sensitive_artifacts_fail_redacted_before_publication(
    tmp_path: Path, capsys, path: str, verification: str,
) -> None:
    root = _repo(tmp_path)
    out = tmp_path / "skill.zip"
    archive_builder.build_skill_archive(root, out)
    prior = out.read_bytes()
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic-credential-do-not-echo", encoding="utf-8")
    _git(root, "add", "--", path)
    _git(root, "commit", "-qm", "suspicious fixture")
    assert archive_builder.main(["--source", str(root), "--out", str(out)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "suspicious sensitive artifact" in captured.err
    assert "Git tree entry" in captured.err
    assert "inspect" in captured.err
    assert path not in captured.err
    assert "synthetic-credential-do-not-echo" not in captured.err
    with pytest.raises(archive_builder.SecretScrubError, match="suspicious sensitive artifact"):
        archive_builder.verify_zip(out, source=root, verification=verification)
    assert out.read_bytes() == prior
    assert _archive_temporaries(out) == []
