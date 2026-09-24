"""X7 managed static/SPA output ownership: manifest, denials, adoption, races.

Every mutation happens under ``tmp_path``. Deny cases prove zero writes with a
no-follow lstat snapshot of the whole temporary tree plus spies that fail if
staging or rendering starts.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import okf_loom.export_ownership as export_ownership
import okf_loom.render as render_mod
from okf_loom import cli
from okf_loom.export_ownership import (
    MANIFEST_NAME, ExportOwnershipError, canonical_manifest_bytes, parse_manifest,
)
from okf_loom.model import Bundle
from okf_loom.render import build_site

REPO = Path(__file__).resolve().parents[1]
MANIFEST_SCHEMA = "okf-loom/export-ownership"


def tree_bytes(root: Path) -> dict:
    """Full byte view of a regular tree (files and directories only)."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        out[rel] = p.read_bytes() if p.is_file() else None
    return out


def snapshot(root: Path) -> dict:
    """No-follow snapshot: never follows symlinks, never opens special files."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            p = Path(dirpath) / name
            info = p.lstat()
            rel = p.relative_to(root).as_posix()
            if stat.S_ISREG(info.st_mode):
                out[rel] = ("file", info.st_ino, info.st_mtime_ns, p.read_bytes())
            elif stat.S_ISLNK(info.st_mode):
                out[rel] = ("link", info.st_ino, os.readlink(p))
            elif stat.S_ISDIR(info.st_mode):
                out[rel] = ("dir", info.st_ino, info.st_mtime_ns)
            else:
                out[rel] = ("special", info.st_ino, info.st_mtime_ns)
    return out


def no_leftovers(parent: Path) -> None:
    assert not list(parent.glob(".okf-build-*"))
    assert not list(parent.glob(".okf-recovery-*"))


def run(argv: list[str], capsys) -> tuple[int, str]:
    code = cli.main(argv)
    return code, capsys.readouterr().err


def build_cli(bundle: Path, out: Path, *extra: str) -> list[str]:
    return ["build", str(bundle), "--out", str(out), *extra]


def manifest(out: Path) -> dict:
    return json.loads((out / MANIFEST_NAME).read_text("utf-8"))


def _boom(*_a, **_k):
    raise AssertionError("staging/render must not start on a preflight deny")


@pytest.fixture
def no_staging(monkeypatch):
    """Fail the test if anything past preflight starts."""
    monkeypatch.setattr(tempfile, "TemporaryDirectory", _boom)
    monkeypatch.setattr(tempfile, "mkdtemp", _boom)
    monkeypatch.setattr(render_mod, "_emit_site", _boom)


@pytest.fixture
def showcase(tmp_path) -> Path:
    dst = tmp_path / "showcase"
    shutil.copytree(REPO / "samples" / "showcase", dst)
    return dst


# ---------------------------------------------------------------- T0


@pytest.mark.parametrize("target,extra", [
    ("static", {}), ("spa", {}), ("static", {"include_portable_feedback": True}),
])
def test_static_and_spa_full_tree_byte_deterministic(showcase, tmp_path, target, extra):
    """Adoption prerequisite: two builds of one bundle are byte-identical."""
    bundle = Bundle.load(showcase)
    one, two = tmp_path / "one", tmp_path / "two"
    build_site(bundle, one, target=target, **extra)
    build_site(Bundle.load(showcase), two, target=target, **extra)
    assert tree_bytes(one) == tree_bytes(two)


# ---------------------------------------------------------------- legacy


@pytest.mark.parametrize("target", ["static", "spa"])
def test_default_replace_nonempty_writes_no_manifest(tiny_good_bundle, tmp_path, target):
    out = tmp_path / "site"
    out.mkdir()
    (out / "unrelated.txt").write_text("user file")
    stats = build_site(Bundle.load(tiny_good_bundle), out, target=target)
    assert not (out / "unrelated.txt").exists()
    assert not (out / MANIFEST_NAME).exists()
    assert "ownership" not in stats and "output_policy" not in stats


def test_single_file_retains_siblings(tiny_good_bundle, tmp_path):
    out = tmp_path / "site"
    out.mkdir()
    (out / "sibling.txt").write_text("keep")
    build_site(Bundle.load(tiny_good_bundle), out, target="single-file")
    assert (out / "sibling.txt").read_text() == "keep"
    assert set(p.name for p in out.iterdir()) == {"sibling.txt", "index.html"}


@pytest.mark.parametrize("target", ["static", "spa"])
def test_managed_tree_equals_default_tree_plus_manifest(showcase, tmp_path, target):
    legacy, managed = tmp_path / "legacy", tmp_path / "managed"
    build_site(Bundle.load(showcase), legacy, target=target)
    build_site(Bundle.load(showcase), managed, target=target, output_policy="managed")
    managed_tree = tree_bytes(managed)
    managed_tree.pop(MANIFEST_NAME)
    assert managed_tree == tree_bytes(legacy)


# ---------------------------------------------------------------- CLI


def test_adopt_requires_managed_exit2_no_writes(tiny_good_bundle, tmp_path, capsys):
    before = snapshot(tmp_path)
    code, err = run(build_cli(tiny_good_bundle, tmp_path / "site", "--adopt-output"), capsys)
    assert code == 2
    assert "--adopt-output requires --output-policy managed" in err
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("extra", [[], ["--adopt-output"]])
def test_managed_single_file_exit2_no_writes(tiny_good_bundle, tmp_path, capsys, extra):
    out = tmp_path / "site"
    out.mkdir()
    (out / "sibling.txt").write_text("keep")
    before = snapshot(tmp_path)
    code, err = run(build_cli(tiny_good_bundle, out, "--target", "single-file",
                              "--output-policy", "managed", *extra), capsys)
    assert code == 2
    assert "supports only --target static or spa" in err
    assert snapshot(tmp_path) == before


def test_managed_single_file_library_refuses(tiny_good_bundle, tmp_path):
    with pytest.raises(ValueError, match="static and spa"):
        build_site(Bundle.load(tiny_good_bundle), tmp_path / "s", target="single-file",
                   output_policy="managed")
    with pytest.raises(ValueError, match="requires"):
        build_site(Bundle.load(tiny_good_bundle), tmp_path / "s", target="static",
                   adopt_output=True)
    assert not (tmp_path / "s").exists()


def test_bad_policy_choice_exit2(tiny_good_bundle, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(build_cli(tiny_good_bundle, tmp_path / "site", "--output-policy", "adopt"))
    assert exc.value.code == 2
    assert not (tmp_path / "site").exists()


# ---------------------------------------------------------------- creation


def walked_entries(out: Path) -> list[dict]:
    items = []
    for p in out.rglob("*"):
        rel = p.relative_to(out).as_posix()
        if rel == MANIFEST_NAME:
            continue
        if p.is_dir():
            items.append({"path": rel, "type": "dir"})
        else:
            data = p.read_bytes()
            items.append({"path": rel, "type": "file", "size": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
    return sorted(items, key=lambda e: e["path"].encode("utf-8"))


@pytest.mark.parametrize("target", ["static", "spa"])
def test_managed_create_absent(tiny_good_bundle, tmp_path, target, capsys):
    out = tmp_path / "site"
    code, _ = run(build_cli(tiny_good_bundle, out, "--target", target,
                            "--output-policy", "managed"), capsys)
    assert code == 0
    raw = (out / MANIFEST_NAME).read_bytes()
    obj = json.loads(raw)
    assert raw == canonical_manifest_bytes(obj)
    assert set(obj) == {"bundle_revision", "entries", "generator", "loom_version",
                        "schema", "target", "version"}
    assert obj["schema"] == MANIFEST_SCHEMA and obj["generator"] == "okf-loom"
    assert obj["version"] == 1 and obj["target"] == target
    assert obj["entries"] == walked_entries(out)
    assert str(tmp_path).encode() not in raw
    assert parse_manifest(raw)  # round-trips through the strict parser
    no_leftovers(tmp_path)


def test_managed_into_empty_dir_and_missing_parents(tiny_good_bundle, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    stats = build_site(Bundle.load(tiny_good_bundle), empty, target="static",
                       output_policy="managed")
    assert stats["ownership"] == "created" and stats["output_policy"] == "managed"
    deep = tmp_path / "a" / "b" / "site"
    stats = build_site(Bundle.load(tiny_good_bundle), deep, target="spa",
                       output_policy="managed")
    assert stats["ownership"] == "created"
    assert (deep / MANIFEST_NAME).is_file()
    assert stats["files"] == sum(1 for p in deep.rglob("*") if p.is_file())


def test_managed_rebuild_publishes_new_content(tiny_good_bundle, tmp_path):
    out = tmp_path / "site"
    build_site(Bundle.load(tiny_good_bundle), out, target="static", output_policy="managed")
    first = manifest(out)
    concept = next(p for p in sorted(tiny_good_bundle.rglob("*.md"))
                   if p.name not in ("index.md", "log.md"))
    concept.write_text(concept.read_text() + "\nFreshly added sentence X7.\n")
    stats = build_site(Bundle.load(tiny_good_bundle), out, target="static",
                       output_policy="managed")
    assert stats["ownership"] == "replaced_managed"
    assert manifest(out) != first
    assert manifest(out)["entries"] == walked_entries(out)
    assert any(b"Freshly added sentence X7." in p.read_bytes()
               for p in out.rglob("*.html"))
    no_leftovers(tmp_path)


@pytest.mark.parametrize("first,second", [("static", "spa"), ("spa", "static")])
def test_managed_target_switch_allowed(tiny_good_bundle, tmp_path, first, second):
    out = tmp_path / "site"
    build_site(Bundle.load(tiny_good_bundle), out, target=first, output_policy="managed")
    stats = build_site(Bundle.load(tiny_good_bundle), out, target=second,
                       output_policy="managed")
    assert stats["ownership"] == "replaced_managed"
    assert manifest(out)["target"] == second


def test_managed_with_portable_feedback(tiny_good_bundle, tmp_path, capsys):
    out = tmp_path / "site"
    argv = build_cli(tiny_good_bundle, out, "--target", "static", "--output-policy",
                     "managed", "--include-portable-feedback")
    assert run(argv, capsys)[0] == 0
    assert run(argv, capsys)[0] == 0  # managed rebuild over its own output
    assert manifest(out)["entries"] == walked_entries(out)


# ---------------------------------------------------------------- denials


def _mutate(out: Path, case: str) -> None:
    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != MANIFEST_NAME)
    victim = next(p for p in files if p.stat().st_size > 2)
    subdir = next(p for p in sorted(out.rglob("*")) if p.is_dir())
    if case == "unknown_file":
        (out / "notes.txt").write_text("mine")
    elif case == "unknown_dir":
        (out / "extra-dir").mkdir()
    elif case == "same_size_edit":
        data = bytearray(victim.read_bytes())
        data[0] = (data[0] + 1) % 256
        victim.write_bytes(bytes(data))
    elif case == "truncation":
        victim.write_bytes(victim.read_bytes()[:-1])
    elif case == "deletion":
        victim.unlink()
    elif case == "file_to_dir":
        victim.unlink()
        victim.mkdir()
    elif case == "dir_to_file":
        shutil.rmtree(subdir)
        subdir.write_text("x")
    elif case == "file_symlink":
        (out / "link.html").symlink_to(victim)
    elif case == "dir_symlink":
        (out / "linkdir").symlink_to(subdir)
    elif case == "dangling_symlink":
        (out / "dangling").symlink_to(out / "missing")
    elif case == "replace_with_symlink":
        victim.unlink()
        victim.symlink_to(files[-1] if files[-1] != victim else files[0])
    elif case == "fifo":
        os.mkfifo(out / "pipe")
    else:
        raise AssertionError(case)


DENY_CASES = {
    "unknown_file": "entry_unknown", "unknown_dir": "entry_unknown",
    "same_size_edit": "entry_modified", "truncation": "entry_modified",
    "deletion": "entry_modified", "file_to_dir": "entry_modified",
    "dir_to_file": "entry_modified", "file_symlink": "entry_symlink",
    "dir_symlink": "entry_symlink", "dangling_symlink": "entry_symlink",
    "replace_with_symlink": "entry_symlink", "fifo": "entry_special",
}


@pytest.fixture
def managed_site(tiny_good_bundle, tmp_path) -> Path:
    out = tmp_path / "site"
    build_site(Bundle.load(tiny_good_bundle), out, target="static", output_policy="managed")
    return out


def _assert_denied(tiny_good_bundle, tmp_path, out, capsys, code, extra=()):
    before = snapshot(tmp_path)
    rc, err = run(build_cli(tiny_good_bundle, out, "--target", "static",
                            "--output-policy", "managed", *extra), capsys)
    assert rc == 2, err
    assert err.startswith(f"error: export.ownership.{code}: "), err
    assert "no files changed" in err
    assert snapshot(tmp_path) == before
    no_leftovers(tmp_path)
    return err


@pytest.mark.parametrize("adopt", [False, True])
@pytest.mark.parametrize("case", sorted(DENY_CASES))
def test_managed_tree_changes_deny_without_writes(
        tiny_good_bundle, tmp_path, managed_site, capsys, no_staging, case, adopt):
    _mutate(managed_site, case)
    extra = ["--adopt-output"] if adopt else []
    _assert_denied(tiny_good_bundle, tmp_path, managed_site, capsys, DENY_CASES[case], extra)


def test_socket_denies_as_special(tiny_good_bundle, capsys, monkeypatch):
    # AF_UNIX paths are short; pytest's tmp_path may be too deep to bind.
    root = Path(tempfile.mkdtemp(prefix="okf-x7-", dir="/tmp"))
    sock = socket.socket(socket.AF_UNIX)
    try:
        site = root / "site"
        build_site(Bundle.load(tiny_good_bundle), site, target="static",
                   output_policy="managed")
        sock.bind(str(site / "s.sock"))
        before = snapshot(root)
        with monkeypatch.context() as patch:
            patch.setattr(render_mod, "_emit_site", _boom)
            patch.setattr(tempfile, "TemporaryDirectory", _boom)
            rc, err = run(build_cli(tiny_good_bundle, site, "--target", "static",
                                    "--output-policy", "managed"), capsys)
        assert rc == 2
        assert err.startswith("error: export.ownership.entry_special: "), err
        assert snapshot(root) == before
        no_leftovers(root)
    finally:
        sock.close()
        shutil.rmtree(root)


def _manifest_obj(out: Path) -> dict:
    return json.loads((out / MANIFEST_NAME).read_bytes())


def _write_obj(out: Path, obj: dict) -> None:
    (out / MANIFEST_NAME).write_bytes(canonical_manifest_bytes(obj))


def _with_entry(obj: dict, entry) -> dict:
    """Add ``entry`` keeping code-point order, so only its own defect trips."""
    obj["entries"] = sorted([*obj["entries"], entry], key=lambda e: e["path"])
    return obj


def _manifest_defect(out: Path, case: str, monkeypatch=None) -> None:
    path = out / MANIFEST_NAME
    obj = _manifest_obj(out)
    files = [e for e in obj["entries"] if e["type"] == "file"]
    if case == "invalid_json":
        path.write_bytes(b"{not json\n")
    elif case == "noncanonical_whitespace":
        path.write_bytes(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode() + b"\n")
    elif case == "missing_newline":
        path.write_bytes(canonical_manifest_bytes(obj)[:-1])
    elif case == "bom":
        path.write_bytes(b"\xef\xbb\xbf" + canonical_manifest_bytes(obj))
    elif case == "unknown_key":
        _write_obj(out, {**obj, "extra": 1})
    elif case == "missing_key":
        del obj["loom_version"]
        _write_obj(out, obj)
    elif case == "version_2":
        _write_obj(out, {**obj, "version": 2})
    elif case == "version_float":
        _write_obj(out, {**obj, "version": 1.0})
    elif case == "bool_size":
        files[0]["size"] = True
        _write_obj(out, obj)
    elif case == "float_size":
        files[0]["size"] = float(files[0]["size"])
        _write_obj(out, obj)
    elif case == "upper_hash":
        files[0]["sha256"] = files[0]["sha256"].upper()
        _write_obj(out, obj)
    elif case == "duplicate_path":
        obj["entries"].insert(obj["entries"].index(files[0]), dict(files[0]))
        _write_obj(out, obj)
    elif case == "duplicate_key":
        raw = canonical_manifest_bytes(obj)
        path.write_bytes(raw.replace(b'"version":1', b'"version":1,"version":1'))
    elif case == "unsorted":
        obj["entries"].reverse()
        _write_obj(out, obj)
    elif case == "dotdot_path":
        obj["entries"].insert(0, {"path": "../escape", "type": "dir"})
        _write_obj(out, obj)
    elif case == "absolute_path":
        obj["entries"].insert(0, {"path": "/etc", "type": "dir"})
        _write_obj(out, obj)
    elif case == "self_listing":
        obj["entries"].insert(0, {"path": MANIFEST_NAME, "type": "dir"})
        _write_obj(out, obj)
    elif case == "missing_parent_closure":
        nested = next(e for e in obj["entries"] if "/" in e["path"])
        parent = nested["path"].split("/")[0]
        obj["entries"] = [e for e in obj["entries"] if e["path"] != parent]
        _write_obj(out, obj)
    elif case == "manifest_symlink":
        target = out.parent / "elsewhere.json"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    elif case == "manifest_directory":
        path.unlink()
        path.mkdir()
    elif case == "manifest_fifo":
        path.unlink()
        os.mkfifo(path)
    elif case == "manifest_oversize":
        path.write_bytes(b" " * (16 * 1024 * 1024 + 1))
    elif case == "not_an_object":
        path.write_bytes(b"[]\n")
    elif case == "entry_count_over_cap":
        # The manifest itself is unchanged and valid; the cap drops below it
        # (16 MiB byte cap not involved).
        monkeypatch.setattr(export_ownership, "MAX_MANIFEST_ENTRIES",
                            len(obj["entries"]) - 1)
    elif case in _BAD_PATHS:
        _write_obj(out, _with_entry(obj, {"path": _BAD_PATHS[case], "type": "dir"}))
    elif case == "negative_size":
        files[0]["size"] = -1
        _write_obj(out, obj)
    elif case == "size_over_safe_int":
        files[0]["size"] = 2 ** 53
        _write_obj(out, obj)
    elif case == "non_object_entry":
        obj["entries"].append("zzz")
        _write_obj(out, obj)
    elif case == "unknown_entry_type":
        _write_obj(out, _with_entry(obj, {"path": "zzz", "type": "symlink"}))
    elif case == "long_bundle_revision":
        _write_obj(out, {**obj, "bundle_revision": "r" * 129})
    elif case == "long_loom_version":
        _write_obj(out, {**obj, "loom_version": "v" * 65})
    else:
        raise AssertionError(case)


_BAD_PATHS = {
    "nul_path": "zz\x00z", "empty_segment": "zz//z", "trailing_empty_segment": "zz/",
    "dot_segment": "zz/./z", "dot_path": ".", "empty_path": "",
}


MANIFEST_CASES = {
    "invalid_json": "manifest_malformed", "noncanonical_whitespace": "manifest_malformed",
    "missing_newline": "manifest_malformed", "bom": "manifest_malformed",
    "unknown_key": "manifest_malformed", "missing_key": "manifest_malformed",
    "version_2": "manifest_unsupported", "version_float": "manifest_unsupported",
    "bool_size": "manifest_malformed", "float_size": "manifest_malformed",
    "upper_hash": "manifest_malformed", "duplicate_path": "manifest_malformed",
    "duplicate_key": "manifest_malformed", "unsorted": "manifest_malformed",
    "dotdot_path": "manifest_malformed", "absolute_path": "manifest_malformed",
    "self_listing": "manifest_malformed", "missing_parent_closure": "manifest_malformed",
    "manifest_symlink": "entry_symlink", "manifest_directory": "manifest_malformed",
    "manifest_fifo": "entry_special", "manifest_oversize": "manifest_malformed",
    "not_an_object": "manifest_malformed",
    "entry_count_over_cap": "manifest_malformed", "nul_path": "manifest_malformed",
    "empty_segment": "manifest_malformed", "trailing_empty_segment": "manifest_malformed",
    "dot_segment": "manifest_malformed", "dot_path": "manifest_malformed",
    "empty_path": "manifest_malformed", "negative_size": "manifest_malformed",
    "size_over_safe_int": "manifest_malformed", "non_object_entry": "manifest_malformed",
    "unknown_entry_type": "manifest_malformed", "long_bundle_revision": "manifest_malformed",
    "long_loom_version": "manifest_malformed",
}


@pytest.mark.parametrize("adopt", [False, True])
@pytest.mark.parametrize("case", sorted(MANIFEST_CASES))
def test_manifest_defects_deny_without_writes(
        tiny_good_bundle, tmp_path, managed_site, capsys, no_staging, monkeypatch, case, adopt):
    """--adopt-output never overrides a present manifest, however malformed."""
    _manifest_defect(managed_site, case, monkeypatch)
    extra = ["--adopt-output"] if adopt else []
    _assert_denied(tiny_good_bundle, tmp_path, managed_site, capsys,
                   MANIFEST_CASES[case], extra)


def test_unmanaged_nonempty_denies_zero_writes(tiny_good_bundle, tmp_path, capsys, no_staging):
    out = tmp_path / "site"
    out.mkdir()
    (out / "index.html").write_text("hand-written")
    err = _assert_denied(tiny_good_bundle, tmp_path, out, capsys, "unmanaged_nonempty")
    assert "--adopt-output" in err


def test_deny_error_is_file_exists_with_code(tiny_good_bundle, tmp_path, managed_site):
    (managed_site / "extra.txt").write_text("x")
    with pytest.raises(ExportOwnershipError) as exc:
        build_site(Bundle.load(tiny_good_bundle), managed_site, target="spa",
                   output_policy="managed")
    assert isinstance(exc.value, FileExistsError)
    assert exc.value.code == "export.ownership.entry_unknown"
    assert "(1 finding(s); first: 'extra.txt')" in str(exc.value)


# ---------------------------------------------------------------- adoption


@pytest.mark.parametrize("target", ["static", "spa"])
def test_adopt_exact_legacy_build(showcase, tmp_path, target, capsys):
    out = tmp_path / "site"
    build_site(Bundle.load(showcase), out, target=target)
    before = tree_bytes(out)
    stats = build_site(Bundle.load(showcase), out, target=target, output_policy="managed",
                       adopt_output=True)
    assert stats["ownership"] == "adopted"
    after = tree_bytes(out)
    assert set(after) - set(before) == {MANIFEST_NAME}
    after.pop(MANIFEST_NAME)
    assert after == before
    # Now managed: a plain managed rebuild (no adopt) is accepted.
    stats = build_site(Bundle.load(showcase), out, target=target, output_policy="managed")
    assert stats["ownership"] == "replaced_managed"


@pytest.mark.parametrize("case", ["edited_byte", "extra_file", "missing_file"])
def test_adopt_mismatch_denies(tiny_good_bundle, tmp_path, capsys, case):
    out = tmp_path / "site"
    build_site(Bundle.load(tiny_good_bundle), out, target="static")
    victim = sorted(p for p in out.rglob("*.html"))[0]
    if case == "edited_byte":
        data = bytearray(victim.read_bytes())
        data[-2] = (data[-2] + 1) % 256
        victim.write_bytes(bytes(data))
    elif case == "extra_file":
        (out / "mine.txt").write_text("x")
    else:
        victim.unlink()
    err = _assert_denied(tiny_good_bundle, tmp_path, out, capsys, "adopt_mismatch",
                         ["--adopt-output"])
    assert "destination unchanged; private staging removed" in err


@pytest.mark.parametrize("case", ["file_symlink", "fifo", "dangling_symlink"])
def test_adopt_symlink_or_fifo_denies_before_staging(
        tiny_good_bundle, tmp_path, capsys, no_staging, case):
    out = tmp_path / "site"
    (out / "sub").mkdir(parents=True)
    (out / "page.html").write_text("hand-written")
    _mutate(out, case)
    _assert_denied(tiny_good_bundle, tmp_path, out, capsys, DENY_CASES[case],
                   ["--adopt-output"])


def test_adopt_over_empty_or_absent_creates(tiny_good_bundle, tmp_path):
    stats = build_site(Bundle.load(tiny_good_bundle), tmp_path / "site", target="static",
                       output_policy="managed", adopt_output=True)
    assert stats["ownership"] == "created"


# ---------------------------------------------------------------- races


def test_l1_revalidation_denies_race(tiny_good_bundle, tmp_path, managed_site, monkeypatch):
    import fcntl

    real_flock = fcntl.flock

    def flock_then_race(fd, op):
        real_flock(fd, op)
        if op == fcntl.LOCK_EX and not (managed_site / "raced.txt").exists():
            (managed_site / "raced.txt").write_text("written after preflight")

    monkeypatch.setattr(fcntl, "flock", flock_then_race)
    with pytest.raises(ExportOwnershipError) as exc:
        build_site(Bundle.load(tiny_good_bundle), managed_site, target="static",
                   output_policy="managed")
    assert exc.value.code == "export.ownership.entry_unknown"
    assert (managed_site / "raced.txt").read_text() == "written after preflight"
    no_leftovers(tmp_path)


def test_l1_revalidation_rehashes_same_size_edit(tiny_good_bundle, tmp_path, managed_site,
                                                  monkeypatch):
    import fcntl

    victim = sorted(managed_site.rglob("*.html"))[0]
    original = victim.read_bytes()
    real_flock = fcntl.flock

    def flock_then_edit(fd, op):
        real_flock(fd, op)
        if op == fcntl.LOCK_EX:
            data = bytearray(original)
            data[0] ^= 1
            victim.write_bytes(bytes(data))

    monkeypatch.setattr(fcntl, "flock", flock_then_edit)
    with pytest.raises(ExportOwnershipError) as exc:
        build_site(Bundle.load(tiny_good_bundle), managed_site, target="static",
                   output_policy="managed")
    assert exc.value.code == "export.ownership.entry_modified"
    assert victim.read_bytes() != original  # the edit survives; nothing replaced
    no_leftovers(tmp_path)


def _rename_hook(monkeypatch, after_quarantine, fail_rollback=False):
    real_rename = os.rename

    def rename(src, dst, **kwargs):
        if fail_rollback and src == "previous":
            raise OSError("injected rollback failure")
        result = real_rename(src, dst, **kwargs)
        if dst == "previous":
            after_quarantine(kwargs["dst_dir_fd"])
        return result

    monkeypatch.setattr(os, "rename", rename)


def view(root: Path) -> dict:
    """No-follow content view: bytes for files, kinds for everything else."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            p = Path(dirpath) / name
            mode = p.lstat().st_mode
            rel = p.relative_to(root).as_posix()
            if stat.S_ISREG(mode):
                out[rel] = p.read_bytes()
            elif stat.S_ISLNK(mode):
                out[rel] = ("link", os.readlink(p))
            elif stat.S_ISDIR(mode):
                out[rel] = "dir"
            else:
                out[rel] = ("special", stat.S_IFMT(mode))
    return out


def _swap_identical(path: Path) -> None:
    """Replace ``path`` with a new inode holding identical bytes (os.replace
    is not the patched os.rename, so the quarantine hook does not re-enter)."""
    twin = path.with_name(path.name + ".twin")
    twin.write_bytes(path.read_bytes())
    os.replace(twin, path)


# Late changes injected after the old tree moved into private recovery; the
# L2 metadata recheck (not the entry set or sizes) must catch the ones marked.
L2_CASES = {
    "add": "entry_unknown",
    "append": "entry_modified",
    "remove": "entry_modified",
    "same_size_rewrite": "entry_modified",   # size unchanged; mtime/ctime move
    "inode_swap": "entry_modified",          # bytes and size identical; new inode
    "symlink_swap": "entry_symlink",
    "fifo_swap": "entry_special",
    "manifest_rewrite": "entry_modified",    # identical manifest bytes; new inode
    "root_swap": "entry_modified",           # identical tree under a new root
}


def _l2_inject(case: str, base: Path, victim: str, displaced: Path) -> None:
    target = base / victim
    if case == "add":
        (base / "late.txt").write_text("late")
    elif case == "append":
        with open(target, "ab") as fh:
            fh.write(b"<!-- late -->")
    elif case == "remove":
        target.unlink()
    elif case == "same_size_rewrite":
        with open(target, "r+b") as fh:
            first = fh.read(1)
            fh.seek(0)
            fh.write(bytes([first[0] ^ 1]))
    elif case == "inode_swap":
        _swap_identical(target)
    elif case == "symlink_swap":
        target.unlink()
        os.symlink("elsewhere.html", target)
    elif case == "fifo_swap":
        target.unlink()
        os.mkfifo(target)
    elif case == "manifest_rewrite":
        _swap_identical(base / MANIFEST_NAME)
    elif case == "root_swap":
        twin = base.parent / "twin"
        shutil.copytree(base, twin, symlinks=True)
        os.replace(base, displaced)
        os.replace(twin, base)
    else:
        raise AssertionError(case)


def _expected_after(case: str, before: dict, victim: str) -> dict:
    expected = dict(before)
    if case == "add":
        expected["late.txt"] = b"late"
    elif case == "append":
        expected[victim] += b"<!-- late -->"
    elif case == "remove":
        del expected[victim]
    elif case == "same_size_rewrite":
        expected[victim] = bytes([before[victim][0] ^ 1]) + before[victim][1:]
    elif case == "symlink_swap":
        expected[victim] = ("link", "elsewhere.html")
    elif case == "fifo_swap":
        expected[victim] = ("special", stat.S_IFIFO)
    return expected  # inode/manifest/root swaps keep identical bytes


def _run_l2(tiny_good_bundle, out, monkeypatch, capsys, inject, *extra):
    def touch(backup_fd):
        inject(Path(f"/proc/self/fd/{backup_fd}") / "previous")

    _rename_hook(monkeypatch, touch)
    rc, err = run(build_cli(tiny_good_bundle, out, "--target", "static",
                            "--output-policy", "managed", *extra), capsys)
    monkeypatch.undo()
    return rc, err


def _assert_l2_refusal(rc, err, code):
    assert rc == 2, err
    assert err.startswith(f"error: export.ownership.{code}: "), err
    assert "destination unchanged; private staging removed" in err


@pytest.mark.parametrize("case", sorted(L2_CASES))
def test_l2_revalidation_rolls_back(tiny_good_bundle, tmp_path, managed_site, monkeypatch,
                                    capsys, case):
    victim = sorted(p.relative_to(managed_site).as_posix()
                    for p in managed_site.rglob("*.html"))[0]
    before = view(managed_site)
    watched = managed_site / (MANIFEST_NAME if case == "manifest_rewrite" else victim)
    before_ino = watched.stat().st_ino
    before_root = managed_site.stat().st_ino
    displaced = tmp_path / "displaced-root"
    rc, err = _run_l2(tiny_good_bundle, managed_site, monkeypatch, capsys,
                      lambda base: _l2_inject(case, base, victim, displaced))
    _assert_l2_refusal(rc, err, L2_CASES[case])
    # The quarantined tree, late change included, is back at the destination.
    assert view(managed_site) == _expected_after(case, before, victim)
    if case in ("inode_swap", "manifest_rewrite"):
        assert watched.stat().st_ino != before_ino
    if case == "root_swap":
        assert managed_site.stat().st_ino != before_root
        assert view(displaced) == before  # the original root, moved by the racer
    no_leftovers(tmp_path)


@pytest.mark.parametrize("state", ["adopt", "create_empty"])
def test_l2_revalidation_rolls_back_adopt_and_empty_create(tiny_good_bundle, tmp_path,
                                                           monkeypatch, capsys, state):
    out = tmp_path / "site"
    extra = []
    if state == "adopt":
        build_site(Bundle.load(tiny_good_bundle), out, target="static")
        extra = ["--adopt-output"]
    else:
        out.mkdir()
    before = view(out)
    rc, err = _run_l2(tiny_good_bundle, out, monkeypatch, capsys,
                      lambda base: (base / "late.txt").write_text("late"), *extra)
    _assert_l2_refusal(rc, err, "entry_unknown")
    assert view(out) == {**before, "late.txt": b"late"}
    assert not (out / MANIFEST_NAME).exists()
    no_leftovers(tmp_path)


def test_l2_failed_rollback_retains_recovery(tiny_good_bundle, tmp_path, managed_site,
                                             monkeypatch):
    from okf_loom.io_utils import TreePublicationRecoveryError

    def touch(backup_fd):
        (Path(f"/proc/self/fd/{backup_fd}") / "previous" / "late.txt").write_text("late")

    _rename_hook(monkeypatch, touch, fail_rollback=True)
    with pytest.raises(TreePublicationRecoveryError) as exc:
        build_site(Bundle.load(tiny_good_bundle), managed_site, target="static",
                   output_policy="managed")
    assert isinstance(exc.value.publish_error, ExportOwnershipError)
    recovered = exc.value.recovery_path
    assert (recovered / "late.txt").read_text() == "late"
    assert (recovered / MANIFEST_NAME).is_file()
    assert not managed_site.exists()
    assert not list(tmp_path.glob(".okf-build-*"))


def test_managed_rollback_retains_recovery_after_stage_cleanup(
        tiny_good_bundle, tmp_path, managed_site, monkeypatch):
    """Parity with the legacy failed-publish recovery contract."""
    from okf_loom.io_utils import TreePublicationRecoveryError

    real_rename = os.rename

    def fail_publish_and_restore(src, dst, **kwargs):
        if str(src).startswith(".okf-build-") or str(src) == "previous":
            raise OSError("injected failure")
        return real_rename(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", fail_publish_and_restore)
    with pytest.raises(TreePublicationRecoveryError) as error:
        build_site(Bundle.load(tiny_good_bundle), managed_site, target="static",
                   output_policy="managed")
    assert (error.value.recovery_path / MANIFEST_NAME).is_file()
    assert not list(tmp_path.glob(".okf-build-*"))


def test_crash_between_renames_next_managed_run_creates_and_keeps_recovery(
        tiny_good_bundle, tmp_path, managed_site):
    from conftest import okf_subprocess_env

    program = """
import os, sys
real = os.rename
def rename(src, dst, **kw):
    real(src, dst, **kw)
    if dst == "previous":
        os._exit(17)
os.rename = rename
from okf_loom.model import Bundle
from okf_loom.render import build_site
build_site(Bundle.load(sys.argv[1]), sys.argv[2], target="static", output_policy="managed")
"""
    proc = subprocess.run([sys.executable, "-c", program, str(tiny_good_bundle),
                           str(managed_site)], env=okf_subprocess_env(),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 17, proc.stderr
    assert not managed_site.exists()
    recovery = list(tmp_path.glob(".okf-recovery-*"))
    assert len(recovery) == 1 and (recovery[0] / "previous" / MANIFEST_NAME).is_file()
    stats = build_site(Bundle.load(tiny_good_bundle), managed_site, target="static",
                       output_policy="managed")
    assert stats["ownership"] == "created"
    assert list(tmp_path.glob(".okf-recovery-*")) == recovery  # never adopted or cleaned
    assert (recovery[0] / "previous" / MANIFEST_NAME).is_file()


# ---------------------------------------------------------------- limits


def test_wide_tree_managed_create_and_rebuild_under_low_fd_limit(tiny_good_bundle, tmp_path):
    """The walker holds descriptors per depth, not per sibling directory."""
    from conftest import okf_subprocess_env

    for i in range(100):
        folder = tiny_good_bundle / f"wide{i:03d}"
        folder.mkdir()
        (folder / "page.md").write_text(f"---\ntype: Note\ntitle: Wide {i}\n---\n\n# Wide {i}\n")
    program = """
import resource, sys
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
from okf_loom.model import Bundle
from okf_loom.render import build_site
for expected in ("created", "replaced_managed"):
    stats = build_site(Bundle.load(sys.argv[1]), sys.argv[2], target="static",
                       output_policy="managed")
    assert stats["ownership"] == expected, stats
"""
    out = tmp_path / "site"
    proc = subprocess.run([sys.executable, "-c", program, str(tiny_good_bundle), str(out)],
                          env=okf_subprocess_env(), capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert len([p for p in out.iterdir() if p.is_dir() and p.name.startswith("wide")]) == 100
    assert manifest(out)["entries"] == walked_entries(out)
    no_leftovers(tmp_path)


def test_descriptor_exhaustion_is_an_operational_error_not_a_finding(
        tiny_good_bundle, tmp_path, managed_site, capsys, no_staging, monkeypatch):
    import errno

    real_open = os.open

    def open_emfile(path, flags, *args, **kwargs):
        if flags == export_ownership._FILE_FLAGS:
            raise OSError(errno.EMFILE, "Too many open files")
        return real_open(path, flags, *args, **kwargs)

    before = snapshot(tmp_path)
    monkeypatch.setattr(os, "open", open_emfile)
    rc, err = run(build_cli(tiny_good_bundle, managed_site, "--target", "static",
                            "--output-policy", "managed"), capsys)
    monkeypatch.undo()
    assert rc == 1, err
    assert "Too many open files" in err and "export.ownership" not in err
    assert snapshot(tmp_path) == before
    no_leftovers(tmp_path)


def test_manifest_at_exact_entry_cap_rebuilds(tiny_good_bundle, tmp_path, monkeypatch):
    probe = tmp_path / "probe"
    build_site(Bundle.load(tiny_good_bundle), probe, target="static", output_policy="managed")
    count = len(manifest(probe)["entries"])
    monkeypatch.setattr(export_ownership, "MAX_MANIFEST_ENTRIES", count)
    out = tmp_path / "site"
    stats = build_site(Bundle.load(tiny_good_bundle), out, target="static",
                       output_policy="managed")
    assert stats["ownership"] == "created"
    assert len(manifest(out)["entries"]) == count
    stats = build_site(Bundle.load(tiny_good_bundle), out, target="static",
                       output_policy="managed")
    assert stats["ownership"] == "replaced_managed"
    no_leftovers(tmp_path)
    # One entry over the cap is refused while sealing, before publication.
    monkeypatch.setattr(export_ownership, "MAX_MANIFEST_ENTRIES", count - 1)
    with pytest.raises(RuntimeError, match="over the entry limit"):
        build_site(Bundle.load(tiny_good_bundle), tmp_path / "over", target="static",
                   output_policy="managed")
    assert not (tmp_path / "over").exists()
    no_leftovers(tmp_path)


# ---------------------------------------------------------------- X1 composition


def test_managed_offline_assets_create_then_rebuild(tiny_good_bundle, tmp_path, capsys):
    out = tmp_path / "site"
    argv = build_cli(tiny_good_bundle, out, "--target", "static", "--output-policy",
                     "managed", "--assets", "offline")
    assert cli.main(argv) == 0, capsys.readouterr().err
    assert "ownership: created" in capsys.readouterr().out
    entries = manifest(out)["entries"]
    assert any(e["path"].startswith("__static/vendor/") for e in entries)
    assert entries == walked_entries(out)
    assert cli.main(argv) == 0, capsys.readouterr().err
    assert "ownership: replaced_managed" in capsys.readouterr().out
    assert manifest(out)["entries"] == walked_entries(out)
    no_leftovers(tmp_path)
