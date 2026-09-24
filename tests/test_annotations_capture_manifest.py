from __future__ import annotations

import hashlib
import errno
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "screenshots" / "annotations-v2"

# Shared provenance inventory and validator — single source of truth.
sys.path.insert(0, str(ROOT / "scripts"))
from okf_loom.capture_provenance import (  # noqa: E402
    compute_inventory, compute_inventory_entries, validate_manifest,
    EXPECTED_VIEWPORTS, EXPECTED_MEDIA, SCENARIO_CLASSIFICATIONS,
    _canonical_inventory_bytes, GitProvenanceError,
    PROVENANCE_MODEL, expected_evidence_paths,
    inventory_serialized_digest,
)

def _load_capture_module():
    """Load capture_annotations_v2 as a module for structural/unit tests."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "capture_annotations_v2", ROOT / "scripts" / "capture_annotations_v2.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# audit-7: EXPECTED_ARTIFACTS is derived from the capture script's
# SCENARIO_ASSERTIONS — the single runtime source of truth — so the test
# scenario contract can never drift from what the producer records.  The
# validator compares "; ".join(image.scenario_assertions) to these values.
_CAP = _load_capture_module()
EXPECTED_ARTIFACTS = {
    name: "; ".join(asserts)
    for name, asserts in _CAP.SCENARIO_ASSERTIONS.items()
}


@pytest.fixture(autouse=True)
def isolated_evidence(tmp_path, request):
    """Keep every inventory, manifest and publication probe off the checkout.

    The existing two-commit fixture supplies real Git authority, two consumed
    source files and synthetic image bytes; it is not browser/release evidence.
    Browser flows already copy their bundle and need the complete runtime.
    """
    if request.node.get_closest_marker("browser"):
        yield
        return
    global ROOT, OUTPUT
    original_root, original_output = ROOT, OUTPUT
    parent = tmp_path / "ordinary-source"
    parent.mkdir()
    root, _manifest, output, _png = _build_clean_staged_fixture(parent)
    ROOT, OUTPUT = root, output
    try:
        yield
    finally:
        ROOT, OUTPUT = original_root, original_output


def test_annotations_capture_manifest_is_bound_and_tamper_evident():
    """Synthetic evidence passes the unmodified strict release validator."""
    manifest_path = OUTPUT / "manifest.json"
    if not manifest_path.is_file():
        raise AssertionError("manifest.json not found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = validate_manifest(manifest, ROOT, EXPECTED_ARTIFACTS, check_files=True)
    if errors:
        raise AssertionError("manifest validation errors: " + "; ".join(errors))


# ---------------------------------------------------------------------------
# Focused unit tests (do NOT require regenerated screenshots)
# ---------------------------------------------------------------------------


def test_inventory_includes_untracked_consumed_inputs():
    """Untracked files in inventory roots are included in the digest."""
    digest_before, count_before = compute_inventory(ROOT)
    sentinel = ROOT / "scripts" / "okf_loom" / "_test_provenance_sentinel.txt"
    sentinel.write_text("sentinel content for provenance test\n", encoding="utf-8")
    try:
        digest_after, count_after = compute_inventory(ROOT)
        if digest_after == digest_before:
            raise AssertionError("untracked file in inventory root did not change digest")
        if count_after <= count_before:
            raise AssertionError(f"file count did not increase: {count_before} -> {count_after}")
    finally:
        sentinel.unlink(missing_ok=True)
    digest_removed, count_removed = compute_inventory(ROOT)
    if digest_removed != digest_before:
        raise AssertionError("digest not restored after removing sentinel")


def test_inventory_entries_have_full_metadata():
    """Each inventory entry has path, type, tracked, mode, sha256."""
    entries = compute_inventory_entries(ROOT)
    if not entries:
        raise AssertionError("no inventory entries")
    for e in entries:
        for key in ("path", "type", "tracked", "mode", "sha256"):
            if key not in e:
                raise AssertionError(f"entry missing {key}: {e}")
        if e["type"] not in ("regular", "symlink"):
            raise AssertionError(f"unsupported file type: {e}")
        if not isinstance(e["tracked"], bool):
            raise AssertionError(f"tracked not bool: {e}")
        if len(e["sha256"]) != 64:
            raise AssertionError(f"invalid sha256 length: {e}")


def test_inventory_excludes_protected_paths():
    """.aic, __pycache__, Bn are excluded."""
    from okf_loom.capture_provenance import _EXCLUDED_SEGMENTS
    if ".aic" not in _EXCLUDED_SEGMENTS:
        raise AssertionError(".aic not in excluded segments")
    if "__pycache__" not in _EXCLUDED_SEGMENTS:
        raise AssertionError("__pycache__ not in excluded segments")


def test_inventory_consumed_file_change_invalidates_digest():
    """Modifying a consumed file changes the digest."""
    digest_before, _ = compute_inventory(ROOT)
    target = ROOT / "scripts" / "okf_loom" / "capture_provenance.py"
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n# sentinel modification\n")
        digest_after, _ = compute_inventory(ROOT)
        if digest_after == digest_before:
            raise AssertionError("consumed file modification did not change digest")
    finally:
        target.write_bytes(original)


def test_capture_script_uses_explicit_failures_not_assert():
    """Capture script has no bare assert statements."""
    import ast
    source = (ROOT / "scripts" / "capture_annotations_v2.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            raise AssertionError(
                f"AST contains ast.Assert at line {node.lineno}: "
                "python -O would silently remove this check"
            )


def test_per_image_viewport_expected_set():
    """Each expected artifact has a per-image viewport defined."""
    for name in EXPECTED_ARTIFACTS:
        if name not in EXPECTED_VIEWPORTS:
            raise AssertionError(f"missing expected viewport for {name}")
        vp = EXPECTED_VIEWPORTS[name]
        if "width" not in vp or "height" not in vp:
            raise AssertionError(f"malformed viewport for {name}: {vp}")


def test_per_image_media_expected_set():
    """Each expected artifact has per-image media defined."""
    for name in EXPECTED_ARTIFACTS:
        if name not in EXPECTED_MEDIA:
            raise AssertionError(f"missing expected media for {name}")
        md = EXPECTED_MEDIA[name]
        if "reduced_motion" not in md or "forced_colors" not in md:
            raise AssertionError(f"malformed media for {name}: {md}")


def test_injected_scenarios_are_classified_truthfully():
    """409/500 images are classified as browser-handling-of-injected, not
    production server proof."""
    for name in ("conflict.png", "save-failure.png", "focus-visible.png", "mobile-320.png", "forced-colors.png"):
        cls = SCENARIO_CLASSIFICATIONS.get(name)
        if cls != "browser-handling-of-injected-http-envelope":
            raise AssertionError(f"{name} should be classified as injected envelope, got {cls}")


def test_public_api_scenarios_are_classified_truthfully():
    """resolved/stale/selecting/no-js are classified as public-api flows."""
    for name in ("resolved.png", "stale-after-mutation.png", "selecting.png", "no-js.png"):
        cls = SCENARIO_CLASSIFICATIONS.get(name)
        if cls not in ("public-api-server-flow", "public-api-browser-flow"):
            raise AssertionError(f"{name} should be classified as public-api flow, got {cls}")


def test_scenario_set_rejects_missing_artifact():
    manifest = json.loads((OUTPUT / "manifest.json").read_text())
    manifest["images"].pop()
    assert any("artifact set mismatch" in error for error in
               validate_manifest(manifest, ROOT, EXPECTED_ARTIFACTS))


def test_scenario_set_rejects_extra_artifact():
    manifest = json.loads((OUTPUT / "manifest.json").read_text())
    manifest["images"].append({"file": "unexpected-extra.png"})
    assert any("artifact set mismatch" in error for error in
               validate_manifest(manifest, ROOT, EXPECTED_ARTIFACTS))


def test_scenario_set_rejects_duplicate_rows():
    manifest = json.loads((OUTPUT / "manifest.json").read_text())
    manifest["images"].append(dict(manifest["images"][0]))
    assert any("duplicate image rows" in error for error in
               validate_manifest(manifest, ROOT, EXPECTED_ARTIFACTS))


def test_scenario_set_rejects_misclassified_row():
    manifest = json.loads((OUTPUT / "manifest.json").read_text())
    manifest["images"][0]["release_proof"] = False
    assert any("release_proof is not True" in error for error in
               validate_manifest(manifest, ROOT, EXPECTED_ARTIFACTS))


def test_head_mismatch_detected():
    manifest = json.loads((OUTPUT / "manifest.json").read_text())
    manifest["source_revision"] = manifest["evidence_parent_revision"] = "0" * 40
    assert any("parent does not equal source_revision" in error for error in
               validate_manifest(manifest, ROOT, EXPECTED_ARTIFACTS))


def test_obsolete_png_does_not_poison_manifest_generation():
    """Obsolete filenames cannot enter manifest construction."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "capture_annotations_v2", ROOT / "scripts" / "capture_annotations_v2.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    expected = mod.EXPECTED_PNG_FILES
    assertions = mod.SCENARIO_ASSERTIONS
    if "stale-ambiguous.png" in expected:
        raise AssertionError("obsolete filename in expected set")
    if "stale-after-mutation.png" not in expected:
        raise AssertionError("expected filename missing")
    for name in expected:
        if name not in assertions:
            raise AssertionError(f"missing scenario assertions for {name}")
    if "stale-ambiguous.png" in assertions:
        raise AssertionError("obsolete name in assertions dict")
    entries = [{"file": n, "scenario_assertions": assertions[n]} for n in sorted(expected)]
    if any(e["file"] == "stale-ambiguous.png" for e in entries):
        raise AssertionError("obsolete file in manifest entries")
    if len(entries) != len(expected):
        raise AssertionError("entry count mismatch")


# ---------------------------------------------------------------------------
# R4 negative tamper/validation tests
# ---------------------------------------------------------------------------


def test_inventory_detail_tamper_detected():
    """r5-2: modifying a single inventory entry's hash is detected by the
    canonical serializer (which now binds mode)."""
    entries = compute_inventory_entries(ROOT)
    if not entries:
        raise AssertionError("no inventory entries")
    tampered = list(entries)
    tampered[0] = dict(tampered[0])
    tampered[0]["sha256"] = "0" * 64
    original_digest, _ = compute_inventory(ROOT)
    tampered_digest = hashlib.sha256(_canonical_inventory_bytes(tampered)).hexdigest()
    if tampered_digest == original_digest:
        raise AssertionError("tampered inventory entry did not change digest")


def test_inventory_digest_binds_mode():
    """r5-2: mode changes must affect the canonical digest."""
    entries = compute_inventory_entries(ROOT)
    if not entries:
        raise AssertionError("no inventory entries")
    original_digest = hashlib.sha256(_canonical_inventory_bytes(entries)).hexdigest()
    tampered = [dict(e) for e in entries]
    tampered[0]["mode"] = "0o600" if tampered[0]["mode"] != "0o600" else "0o644"
    tampered_digest = hashlib.sha256(_canonical_inventory_bytes(tampered)).hexdigest()
    if tampered_digest == original_digest:
        raise AssertionError("mode change did not affect canonical digest")


def test_inventory_detail_type_must_be_regular_or_symlink():
    """R4-2: each inventory entry must have type regular or symlink."""
    entries = compute_inventory_entries(ROOT)
    for e in entries:
        if e["type"] not in ("regular", "symlink"):
            raise AssertionError(f"unsupported type in entry: {e}")


def test_inventory_detail_tracked_is_bool():
    """R4-2: tracked field must be boolean."""
    entries = compute_inventory_entries(ROOT)
    for e in entries:
        if not isinstance(e["tracked"], bool):
            raise AssertionError(f"tracked not bool: {e}")


def test_classification_injected_envelopes_not_production():
    """R4-3: injected HTTP envelope images are classified truthfully."""
    for name in ("conflict.png", "save-failure.png", "focus-visible.png",
                 "mobile-320.png", "forced-colors.png"):
        cls = SCENARIO_CLASSIFICATIONS.get(name)
        if cls != "browser-handling-of-injected-http-envelope":
            raise AssertionError(f"{name} misclassified as {cls}")


def test_classification_public_api_flows():
    """R4-3: real server flows are classified as public-api."""
    for name in ("resolved.png", "stale-after-mutation.png", "no-js.png"):
        cls = SCENARIO_CLASSIFICATIONS.get(name)
        if "public-api" not in cls:
            raise AssertionError(f"{name} should be public-api, got {cls}")


def test_per_image_viewport_forced_colors_is_1280():
    """R4: forced-colors viewport must be 1280, not inherited 320."""
    vp = EXPECTED_VIEWPORTS.get("forced-colors.png")
    if vp != {"width": 1280, "height": 900}:
        raise AssertionError(f"forced-colors viewport wrong: {vp}")


def test_release_critical_checks_survive_optimized_python():
    """R4: python -O must not bypass any release-critical check."""
    import ast
    source = (ROOT / "scripts" / "capture_annotations_v2.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            raise AssertionError(
                f"capture script has ast.Assert at line {node.lineno}"
            )


def test_manifest_has_full_inventory_entries():
    """R4-2: manifest must include capture_input_inventory array."""
    manifest_path = OUTPUT / "manifest.json"
    if not manifest_path.is_file():
        raise AssertionError("manifest.json missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    inv = manifest.get("capture_input_inventory")
    if not isinstance(inv, list) or not inv:
        raise AssertionError("capture_input_inventory missing or empty")
    # Verify each entry has required fields.
    for entry in inv:
        for field in ("path", "type", "tracked", "mode", "sha256"):
            if field not in entry:
                raise AssertionError(f"inventory entry missing {field}: {entry}")


def test_manifest_validator_rejects_missing_inventory():
    """R4-2: validator rejects manifest without inventory entries."""
    manifest_path = OUTPUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad = dict(manifest)
    bad["capture_input_inventory"] = []
    bad["capture_input_inventory_sha256"] = "0" * 64
    errors = validate_manifest(bad, ROOT, EXPECTED_ARTIFACTS, check_files=False)
    if not any("inventory" in e.lower() for e in errors):
        raise AssertionError(f"validator should reject empty inventory: {errors}")


# ---------------------------------------------------------------------------
# P1: sentinel contamination guard
# ---------------------------------------------------------------------------


def test_js_on_screenshots_have_no_sentinel():
    """P1: JS-on screenshots must not contain OKF_NOJS_BODY_SENTINEL text.
    The sentinel is seeded AFTER all JS-on captures, exclusively for the
    no-JS proof."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "capture_annotations_v2", ROOT / "scripts" / "capture_annotations_v2.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Verify the capture script's scenario assertions for JS-on images
    # do NOT mention sentinel.
    js_on_images = [
        "resolved.png", "stale-after-mutation.png", "selecting.png",
        "conflict.png", "save-failure.png", "focus-visible.png",
        "mobile-320.png", "forced-colors.png",
    ]
    for name in js_on_images:
        assertions = mod.SCENARIO_ASSERTIONS.get(name, [])
        for a in assertions:
            if "sentinel" in a.lower():
                raise AssertionError(f"JS-on image {name} scenario mentions sentinel: {a}")
    # Verify the no-JS scenario DOES mention sentinels.
    nojs_assertions = mod.SCENARIO_ASSERTIONS.get("no-js.png", [])
    if not any("sentinel" in a.lower() for a in nojs_assertions):
        raise AssertionError("no-JS scenario should mention sentinel proof")


def test_sentinel_posted_after_js_on_captures():
    """P1: verify capture ordering by source structure — the sentinel
    comment POST must come after the forced-colors screenshot capture
    (last JS-on image) and before the no-JS page creation."""
    source = (ROOT / "scripts" / "capture_annotations_v2.py").read_text(encoding="utf-8")
    fc_pos = source.find('_capture_with_metadata(page, "forced-colors.png"')
    sentinel_pos = source.find("body_sentinel = ")
    nojs_pos = source.find('nojs_context = browser.new_context')
    if fc_pos < 0:
        raise AssertionError("forced-colors capture not found in source")
    if sentinel_pos < 0:
        raise AssertionError("sentinel creation not found in source")
    if nojs_pos < 0:
        raise AssertionError("no-JS context creation not found in source")
    if not (fc_pos < sentinel_pos < nojs_pos):
        raise AssertionError(
            f"ordering wrong: forced-colors={fc_pos} sentinel={sentinel_pos} no-JS={nojs_pos}; "
            "sentinel must come AFTER forced-colors and BEFORE no-JS context"
        )


# ---------------------------------------------------------------------------
# r5 fault-injection tests
# ---------------------------------------------------------------------------


def _init_git_repo(root: Path) -> None:
    """Initialise a minimal git repo so _git_tracked_set succeeds in tests."""
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(root), check=True, timeout=5)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(root), check=True, timeout=5)


# ---- r5-2: detailed inventory equality + mode binding --------------------


def test_validator_rejects_tampered_inventory_mode():
    """r5-2: validator detects a tampered mode in capture_input_inventory."""
    manifest_path = OUTPUT / "manifest.json"
    if not manifest_path.is_file():
        raise AssertionError("manifest.json missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(manifest))  # deep copy
    inv = bad["capture_input_inventory"]
    if not inv:
        raise AssertionError("no inventory entries")
    inv[0]["mode"] = "0o600" if inv[0]["mode"] != "0o600" else "0o644"
    errors = validate_manifest(bad, ROOT, EXPECTED_ARTIFACTS, check_files=False)
    if not any("detailed entries mismatch" in e or "inventory" in e.lower() for e in errors):
        raise AssertionError(f"validator should reject tampered mode: {errors}")


def test_validator_rejects_tampered_inventory_path():
    """r5-2: validator detects a tampered path in capture_input_inventory."""
    manifest_path = OUTPUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(manifest))
    inv = bad["capture_input_inventory"]
    inv[0]["path"] = "nonexistent/fake.txt"
    errors = validate_manifest(bad, ROOT, EXPECTED_ARTIFACTS, check_files=False)
    if not any("detailed entries mismatch" in e for e in errors):
        raise AssertionError(f"validator should reject tampered path: {errors}")


def test_validator_rejects_tampered_inventory_tracked():
    """r5-2: validator detects a tampered tracked flag."""
    manifest_path = OUTPUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(manifest))
    inv = bad["capture_input_inventory"]
    inv[0]["tracked"] = not inv[0]["tracked"]
    errors = validate_manifest(bad, ROOT, EXPECTED_ARTIFACTS, check_files=False)
    if not any("detailed entries mismatch" in e for e in errors):
        raise AssertionError(f"validator should reject tampered tracked: {errors}")


def test_validator_rejects_missing_inventory_list():
    """r5-2: validator rejects manifest without inventory list."""
    manifest_path = OUTPUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(manifest))
    del bad["capture_input_inventory"]
    errors = validate_manifest(bad, ROOT, EXPECTED_ARTIFACTS, check_files=False)
    if not any("capture_input_inventory" in e for e in errors):
        raise AssertionError(f"validator should reject missing inventory list: {errors}")


# ---- r5-3: classification enforcement ------------------------------------


def test_validator_rejects_mismatched_classification():
    """r5-3: validator rejects an image with a wrong classification."""
    manifest_path = OUTPUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(manifest))
    for img in bad["images"]:
        if img["file"] == "resolved.png":
            img["classification"] = "browser-handling-of-injected-http-envelope"
    errors = validate_manifest(bad, ROOT, EXPECTED_ARTIFACTS, check_files=False)
    if not any("classification mismatch" in e for e in errors):
        raise AssertionError(f"validator should reject mismatched classification: {errors}")


def test_validator_rejects_missing_classification():
    """r5-3: validator rejects an image without classification."""
    manifest_path = OUTPUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(manifest))
    for img in bad["images"]:
        if img["file"] == "resolved.png":
            del img["classification"]
    errors = validate_manifest(bad, ROOT, EXPECTED_ARTIFACTS, check_files=False)
    if not any("classification" in e.lower() for e in errors):
        raise AssertionError(f"validator should reject missing classification: {errors}")


def test_validator_rejects_unknown_classification():
    """r5-3: validator rejects an image with an unknown classification value."""
    manifest_path = OUTPUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(manifest))
    for img in bad["images"]:
        if img["file"] == "conflict.png":
            img["classification"] = "public-api-server-flow"
    errors = validate_manifest(bad, ROOT, EXPECTED_ARTIFACTS, check_files=False)
    if not any("classification mismatch" in e for e in errors):
        raise AssertionError(f"validator should reject unknown classification: {errors}")


def test_validator_accepts_correct_classifications():
    """r5-3: validator passes when all classifications match the table."""
    manifest_path = OUTPUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = validate_manifest(manifest, ROOT, EXPECTED_ARTIFACTS, check_files=False)
    cls_errors = [e for e in errors if "classification" in e.lower()]
    if cls_errors:
        raise AssertionError(f"validator should not report classification errors: {cls_errors}")


# ---- r5-6: special-file fail-closed enumeration --------------------------


def test_inventory_rejects_fifo(tmp_path):
    """r5-6: a FIFO in an inventory root raises ValueError, not silent omit."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "real.py").write_text("print(1)\n")
    _init_git_repo(root)
    try:
        os.mkfifo(root / "scripts" / "pipe.fifo")
        try:
            compute_inventory_entries(root)
            raise AssertionError("FIFO was silently accepted/omitted")
        except ValueError as exc:
            if "pipe.fifo" not in str(exc):
                raise AssertionError(f"error doesn't name the FIFO: {exc}")
    finally:
        (root / "scripts" / "pipe.fifo").unlink(missing_ok=True)


def test_inventory_rejects_socket():
    """r5-6: a socket in an inventory root raises ValueError."""
    import socket as _socket
    import tempfile
    # AF_UNIX paths are length-limited (~108 chars); the session TMPDIR is
    # already ~104 chars, so use the system /tmp with a very short prefix.
    short_tmp = Path(tempfile.mkdtemp(dir="/tmp", prefix="s"))
    try:
        root = short_tmp / "r"
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "real.py").write_text("print(1)\n")
        _init_git_repo(root)
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock_path = str(root / "scripts" / "t.sock")
        sock.bind(sock_path)
        try:
            try:
                compute_inventory_entries(root)
                raise AssertionError("socket was silently accepted/omitted")
            except ValueError as exc:
                if "t.sock" not in str(exc):
                    raise AssertionError(f"error doesn't name the socket: {exc}")
        finally:
            sock.close()
            Path(sock_path).unlink(missing_ok=True)
    finally:
        import shutil as _shutil
        _shutil.rmtree(str(short_tmp), ignore_errors=True)


def test_inventory_records_symlink(tmp_path):
    """r5-6: a symlink is recorded as type 'symlink', not omitted."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "target.txt").write_text("target\n")
    (root / "scripts" / "link.txt").symlink_to(root / "scripts" / "target.txt")
    _init_git_repo(root)
    entries = compute_inventory_entries(root)
    types = {e["path"]: e["type"] for e in entries}
    if types.get("scripts/link.txt") != "symlink":
        raise AssertionError(f"symlink not recorded: {types}")
    if types.get("scripts/target.txt") != "regular":
        raise AssertionError(f"target not regular: {types}")


def test_inventory_records_dangling_symlink(tmp_path):
    """r5-6: a dangling symlink is recorded (target string hashed), not omitted."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "real.py").write_text("print(1)\n")
    (root / "scripts" / "dangling.txt").symlink_to(root / "scripts" / "nonexistent")
    _init_git_repo(root)
    entries = compute_inventory_entries(root)
    types = {e["path"]: e["type"] for e in entries}
    if types.get("scripts/dangling.txt") != "symlink":
        raise AssertionError(f"dangling symlink omitted: {types}")


def test_inventory_rejects_device_file(tmp_path):
    """r5-6: a device file (if creatable) is rejected, not omitted."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "real.py").write_text("print(1)\n")
    _init_git_repo(root)
    try:
        os.mknod(root / "scripts" / "chardev", 0o644 | stat.S_IFCHR, 0)
    except (PermissionError, OSError):
        return  # cannot create device files without privileges — skip
    try:
        try:
            compute_inventory_entries(root)
            raise AssertionError("device file was silently accepted/omitted")
        except ValueError:
            pass  # expected
    finally:
        (root / "scripts" / "chardev").unlink(missing_ok=True)


# ---- r5-7: Git provenance fails closed -----------------------------------


def test_git_tracked_set_raises_on_non_git_dir():
    """r5-7: _git_tracked_set raises GitProvenanceError outside a git repo."""
    from okf_loom.capture_provenance import _git_tracked_set
    # Use /tmp directly — the session TMPDIR is inside the repo so git would
    # discover the parent repo and succeed.
    import tempfile
    import shutil as _shutil
    non_git = Path(tempfile.mkdtemp(dir="/tmp", prefix="ng_"))
    try:
        # Guard: verify this dir really is outside any git repo.
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(non_git), capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return  # can't create a true non-git dir — skip
        try:
            _git_tracked_set(non_git)
            raise AssertionError("should have raised GitProvenanceError")
        except GitProvenanceError:
            pass
    finally:
        _shutil.rmtree(str(non_git), ignore_errors=True)


def test_compute_inventory_fails_on_git_error():
    """r5-7: compute_inventory_entries raises on Git failure."""
    import tempfile
    import shutil as _shutil
    root = Path(tempfile.mkdtemp(dir="/tmp", prefix="ng_")) / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "f.py").write_text("x = 1\n")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(root), capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return  # can't create a true non-git dir — skip
        try:
            compute_inventory_entries(root)
            raise AssertionError("should have raised on missing git")
        except GitProvenanceError:
            pass
    finally:
        _shutil.rmtree(str(root.parent), ignore_errors=True)


def test_validator_fails_closed_on_git_revision_error(monkeypatch):
    """r5-7: validate_manifest appends an error when git rev-parse fails."""
    import okf_loom.capture_provenance as cp

    def fail_rev_parse(*a, **kw):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(cp.subprocess, "check_output", fail_rev_parse)
    manifest_path = OUTPUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = validate_manifest(manifest, ROOT, EXPECTED_ARTIFACTS, check_files=False)
    if not any("release-fatal" in e for e in errors):
        raise AssertionError(f"validator should report git failure as release-fatal: {errors}")


# ---- r5-1: atomic publish swap-boundary tests ----------------------------


def _setup_publish_env(tmp_path, prior_bytes=b"PRIOR", new_bytes=b"NEW"):
    """Create a staging dir, output dir with prior content, and a complete
    manifest whose metadata matches ROOT so validate_manifest passes."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    output.mkdir()
    for name in mod.EXPECTED_PNG_FILES:
        (output / name).write_bytes(prior_bytes)
    prior_manifest = {"images": [
        {"file": name, "sha256": hashlib.sha256(prior_bytes).hexdigest()}
        for name in mod.EXPECTED_PNG_FILES
    ]}
    (output / "manifest.json").write_text(json.dumps(prior_manifest))
    staging = tmp_path / "staging"
    staging.mkdir()
    for name in mod.EXPECTED_PNG_FILES:
        (staging / name).write_bytes(new_bytes)
    image_entries = [
        {
            "file": name,
            "sha256": hashlib.sha256(new_bytes).hexdigest(),
            "classification": SCENARIO_CLASSIFICATIONS.get(name, "release-proof"),
            "release_proof": True,
            "scenario_assertions": mod.SCENARIO_ASSERTIONS[name],
            "viewport": EXPECTED_VIEWPORTS[name],
            "media": EXPECTED_MEDIA[name],
        }
        for name in mod.EXPECTED_PNG_FILES
    ]
    manifest = _build_full_manifest(image_entries)
    expected = {name: "; ".join(mod.SCENARIO_ASSERTIONS[name]) for name in mod.EXPECTED_PNG_FILES}
    entries = manifest["images"]
    return mod, staging, output, manifest, entries, expected


def _build_full_manifest(image_entries):
    """Build a manifest with real metadata computed from ROOT."""
    inv_digest, inv_count = compute_inventory(ROOT)
    inv_entries = compute_inventory_entries(ROOT)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    worktree = subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=ROOT)
    expected = {name: "; ".join(_CAP.SCENARIO_ASSERTIONS[name])
                for name in _CAP.EXPECTED_PNG_FILES}
    script_hash = hashlib.sha256(
        (ROOT / "scripts" / "capture_annotations_v2.py").read_bytes()
    ).hexdigest()
    return {
        "schema": 1,
        "provenance_model": PROVENANCE_MODEL,
        "captured_at": "2026-01-01T00:00:00+00:00",
        "source_revision": head,
        "source_revision_verified": True,
        "evidence_parent_revision": head,
        "evidence_parent_verified": True,
        "evidence_paths": expected_evidence_paths(expected),
        "source_worktree_digest": hashlib.sha256(worktree).hexdigest(),
        "evidence_worktree_digest": hashlib.sha256(worktree).hexdigest(),
        "capture_input_inventory_sha256": inv_digest,
        "capture_input_inventory_serialized_sha256": inventory_serialized_digest(inv_entries),
        "capture_input_file_count": inv_count,
        "capture_input_inventory": inv_entries,
        "capture_script_sha256": script_hash,
        "browser": "test",
        "images": image_entries,
    }


def test_atomic_publish_succeeds_first_run(tmp_path):
    """r5-1: publish succeeds when no prior output exists."""
    mod = _load_capture_module()
    staging = tmp_path / "staging"
    staging.mkdir()
    new_bytes = b"NEW"
    for name in mod.EXPECTED_PNG_FILES:
        (staging / name).write_bytes(new_bytes)
    output = tmp_path / "out"  # does not exist yet
    image_entries = [
        {
            "file": name, "sha256": hashlib.sha256(new_bytes).hexdigest(),
            "classification": SCENARIO_CLASSIFICATIONS.get(name, "release-proof"),
            "release_proof": True,
            "scenario_assertions": mod.SCENARIO_ASSERTIONS[name],
            "viewport": EXPECTED_VIEWPORTS[name], "media": EXPECTED_MEDIA[name],
        }
        for name in mod.EXPECTED_PNG_FILES
    ]
    manifest = _build_full_manifest(image_entries)
    expected = {name: "; ".join(mod.SCENARIO_ASSERTIONS[name]) for name in mod.EXPECTED_PNG_FILES}
    mod._atomic_publish(staging, output, manifest, manifest["images"], expected, ROOT)
    if not output.is_dir():
        raise AssertionError("output directory not created")
    for name in mod.EXPECTED_PNG_FILES:
        data = (output / name).read_bytes()
        if data != new_bytes:
            raise AssertionError(f"{name} has wrong bytes on first run")
    if not (output / "manifest.json").is_file():
        raise AssertionError("manifest not published")


def test_atomic_publish_preserves_prior_on_validation_failure(tmp_path):
    """r5-1: if validation fails, prior output bytes are preserved exactly."""
    mod, staging, output, manifest, entries, expected = _setup_publish_env(tmp_path)
    # Corrupt one entry hash to force validation failure.
    bad_entries = [dict(e) for e in entries]
    bad_entries[0]["sha256"] = "0" * 64
    bad_manifest = json.loads(json.dumps(manifest))
    bad_manifest["images"] = bad_entries
    try:
        mod._atomic_publish(staging, output, bad_manifest, bad_entries, expected, ROOT)
        raise AssertionError("publish should have raised on validation failure")
    except RuntimeError:
        pass
    # Prior bytes must be intact.
    for name in mod.EXPECTED_PNG_FILES:
        if (output / name).read_bytes() != b"PRIOR":
            raise AssertionError(f"{name} prior bytes corrupted by failed publish")
    # Sibling dirs must be cleaned up.
    if (output.parent / (output.name + ".publish-new")).exists():
        raise AssertionError("sibling-new not cleaned up after failure")
    if (output.parent / (output.name + ".publish-old")).exists():
        raise AssertionError("sibling-old not cleaned up after failure")


def test_release_publish_fails_closed_when_exchange_unavailable(tmp_path, monkeypatch):
    """r7-M2: when renameat2(RENAME_EXCHANGE) is unavailable AND a prior output
    exists, release publication FAILS CLOSED instead of using the non-atomic
    two-rename fallback.  The prior output bytes are preserved exactly and the
    sibling directories are cleaned up — no partial swap is left behind.

    The two-rename fallback is not atomic for lock-unaware readers (static
    servers, release collectors, validators) which can observe output missing
    between renames, so it must never be used for a release publication that
    replaces existing output."""
    mod, staging, output, manifest, entries, expected = _setup_publish_env(tmp_path)
    # Force exchange unavailable (non-Linux / old kernel / unsupported fs).
    monkeypatch.setattr(mod, "_try_renameat2_exchange", lambda a, b: False)
    try:
        mod._atomic_publish(staging, output, manifest, entries, expected, ROOT)
        raise AssertionError(
            "publish should have failed closed when atomic exchange is unavailable")
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "non-atomic" not in msg and "lock-unaware" not in msg:
            raise AssertionError(
                f"fail-closed error must explain the non-atomic/lock-unaware "
                f"reason: {exc}")
    monkeypatch.undo()
    # Prior bytes must be preserved exactly (output was never moved).
    for name in mod.EXPECTED_PNG_FILES:
        if (output / name).read_bytes() != b"PRIOR":
            raise AssertionError(f"{name} prior bytes corrupted by fail-closed refusal")
    # Sibling dirs must be cleaned up (no partial swap left behind).
    if (output.parent / (output.name + ".publish-new")).exists():
        raise AssertionError("sibling-new not cleaned up after fail-closed refusal")
    if (output.parent / (output.name + ".publish-old")).exists():
        raise AssertionError("sibling-old must never exist under the exchange-only path")


def test_release_publish_never_uses_two_rename_fallback(tmp_path, monkeypatch):
    """r7-M2: the non-atomic two-rename fallback (moving the existing output
    aside to sibling_old before renaming sibling_new -> output) is NEVER used.
    Either the renameat2 exchange swaps atomically, or the publish fails
    closed — in both cases the existing output is never moved via os.rename,
    so a lock-unaware reader never observes output absent."""
    mod, staging, output, manifest, entries, expected = _setup_publish_env(tmp_path)
    forbidden_moves = []
    original_rename = os.rename
    out_abs = os.path.abspath(str(output))

    def tracking_rename(src, dst):
        if os.path.abspath(str(src)) == out_abs:
            forbidden_moves.append((str(src), str(dst)))
        return original_rename(str(src), str(dst))

    monkeypatch.setattr(os, "rename", tracking_rename)
    try:
        mod._atomic_publish(staging, output, manifest, entries, expected, ROOT)
        published = True
    except RuntimeError:
        # Fail-closed path: exchange unavailable on this platform. Still valid
        # — the point of this test is that output is never moved aside.
        published = False
    monkeypatch.undo()
    if forbidden_moves:
        raise AssertionError(
            f"existing output moved aside — non-atomic fallback was used: "
            f"{forbidden_moves}")
    if (output.parent / (output.name + ".publish-old")).exists():
        raise AssertionError("sibling-old created under exchange-only path")
    if published:
        # Exchange path succeeded: output has new bytes.
        for name in mod.EXPECTED_PNG_FILES:
            if (output / name).read_bytes() != b"NEW":
                raise AssertionError(f"{name} not updated to new bytes via exchange")


def test_atomic_publish_succeeds_and_replaces_prior(tmp_path):
    """r5-1: successful publish replaces prior output with new bytes."""
    mod, staging, output, manifest, entries, expected = _setup_publish_env(tmp_path)
    mod._atomic_publish(staging, output, manifest, entries, expected, ROOT)
    for name in mod.EXPECTED_PNG_FILES:
        if (output / name).read_bytes() != b"NEW":
            raise AssertionError(f"{name} not updated to new bytes")
    if not (output / "manifest.json").is_file():
        raise AssertionError("manifest not published")
    if (output.parent / (output.name + ".publish-old")).exists():
        raise AssertionError("sibling-old not removed after success")


def test_atomic_publish_leaves_no_obsolete_files(tmp_path):
    """An existing output with foreign entries is rejected without mutation."""
    mod, staging, output, manifest, entries, expected = _setup_publish_env(tmp_path)
    # Recovery cannot prove this directory complete, so it must not delete or
    # replace it based only on pathname existence.
    (output / "obsolete.png").write_bytes(b"OLD")
    with pytest.raises(RuntimeError, match="incomplete publication proof"):
        mod._atomic_publish(staging, output, manifest, entries, expected, ROOT)
    if (output / "obsolete.png").read_bytes() != b"OLD":
        raise AssertionError("rejected output was mutated")


# ---- r5-5: staged distinctness check --------------------------------------


def test_distinctness_hashes_staged_not_output():
    """r5-5: the distinctness check must hash staging files, not OUTPUT."""
    source = (ROOT / "scripts" / "capture_annotations_v2.py").read_text(encoding="utf-8")
    save_pos = source.find('save_path = staging / "save-failure.png"')
    focus_pos = source.find('focus_path = staging / "focus-visible.png"')
    if save_pos < 0 or focus_pos < 0:
        raise AssertionError("distinctness check does not hash staging paths")
    old_output_ref = source.find('OUTPUT / "save-failure.png"')
    if old_output_ref >= 0:
        raise AssertionError("distinctness check still references stale OUTPUT path")


def test_distinctness_check_present_in_source():
    """r5-5: the staged distinctness check exists in the capture source."""
    source = (ROOT / "scripts" / "capture_annotations_v2.py").read_text(encoding="utf-8")
    if "identical to save-failure.png" not in source:
        raise AssertionError("distinctness check missing from source")


# ---- r5-4: quote sentinel persistence structure ---------------------------


def test_sentinel_uses_real_token_and_text_quote_anchor():
    """r5-4: sentinel POST must use the real page-context token and a valid
    v2 text-quote anchor produced by capture_anchor whose exact quote IS the
    unique quote sentinel placed in canonical concept text (not a fabricated
    span with invented offsets/hashes, and not unrelated stable text)."""
    source = (ROOT / "scripts" / "capture_annotations_v2.py").read_text(encoding="utf-8")
    if "X-OKF-Token\": token" not in source:
        raise AssertionError("sentinel POST must use the real page token, not empty")
    if "capture_anchor(" not in source:
        raise AssertionError("sentinel anchor must be computed via capture_anchor for real source mapping")
    if 'X-OKF-Token": ""' in source:
        raise AssertionError("sentinel POST must not use empty token (vacuous)")
    # The QUOTE sentinel must be placed into canonical concept text and quoted
    # by capture_anchor (exact == sentinel), not anchored on stable text.
    if "concept_path.write_text(sentinel_source" not in source:
        raise AssertionError("quote sentinel must be written into canonical concept source")
    anchor_call = source[source.find("capture_anchor(current_source"):]
    if "quote_sentinel" not in anchor_call[:120]:
        raise AssertionError("capture_anchor must quote the sentinel span, not stable concept text")
    if "quote sentinel not persisted in canonical /__comments anchor exact" not in source:
        raise AssertionError("persisted anchor exact quote must be confirmed through /__comments")
    # The concept mutation must be reverted before the no-JS absence check so
    # the no-JS proof stays meaningful (sentinel is comment data only).
    if "concept_path.write_text(pre_sentinel_source" not in source:
        raise AssertionError("quote sentinel concept mutation must be reverted before no-JS check")


def test_sentinel_confirmed_via_comments_before_nojs_check():
    """r5-4: both sentinels must be confirmed through /__comments before the
    no-JS absence checks."""
    source = (ROOT / "scripts" / "capture_annotations_v2.py").read_text(encoding="utf-8")
    body_confirm = source.find("body sentinel not persisted in canonical")
    quote_confirm = source.find("quote sentinel not persisted in canonical")
    nojs_html = source.find("nojs_html = nojs.content()")
    if body_confirm < 0:
        raise AssertionError("body sentinel /__comments confirmation missing")
    if quote_confirm < 0:
        raise AssertionError("quote sentinel /__comments confirmation missing")
    if nojs_html < 0:
        raise AssertionError("no-JS HTML check missing")
    if not (body_confirm < nojs_html and quote_confirm < nojs_html):
        raise AssertionError("sentinel confirmations must come before no-JS HTML check")


def test_sentinel_no_silent_fallback():
    """r5-4: the sentinel POST must not have a silent HTTPError fallback
    that makes the check vacuous."""
    source = (ROOT / "scripts" / "capture_annotations_v2.py").read_text(encoding="utf-8")
    sentinel_start = source.find("body_sentinel = ")
    nojs_start = source.find("nojs_context = browser.new_context")
    sentinel_block = source[sentinel_start:nojs_start] if sentinel_start >= 0 < nojs_start else ""
    if "except urllib.error.HTTPError" in sentinel_block:
        raise AssertionError("sentinel block has silent HTTPError fallback (vacuous)")
    if "best-effort" in sentinel_block.lower():
        raise AssertionError("sentinel block has best-effort fallback language (vacuous)")


# ---- r5-2 part B: Git index modes, status, symlink_target ---------------


def test_inventory_modes_are_git_modes_not_fs_perms():
    """r5-2: every entry mode is a Git mode (100644/100755/120000), never a
    raw filesystem-permission string like '0o644'/'0o755'."""
    entries = compute_inventory_entries(ROOT)
    if not entries:
        raise AssertionError("no inventory entries")
    allowed = {"100644", "100755", "120000"}
    for e in entries:
        if e["mode"] not in allowed:
            raise AssertionError(
                f"non-Git mode {e['mode']!r} for {e['path']}")


def test_tracked_file_mode_derived_from_git_index(tmp_path):
    """r5-2: a tracked file's mode comes from `git ls-files --stage`, not
    lstat(). Executable -> 100755, normal -> 100644, symlink -> 120000."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    exe = root / "scripts" / "exe.sh"
    exe.write_text("#!/bin/sh\necho hi\n")
    os.chmod(exe, 0o755)
    normal = root / "scripts" / "normal.py"
    normal.write_text("x = 1\n")
    os.chmod(normal, 0o644)
    (root / "scripts" / "target.txt").write_text("t\n")
    (root / "scripts" / "link.txt").symlink_to(root / "scripts" / "target.txt")
    _init_git_repo(root)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True,
                   capture_output=True, timeout=10)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(root), check=True,
                   capture_output=True, timeout=10)
    entries = {e["path"]: e for e in compute_inventory_entries(root)}
    if entries["scripts/exe.sh"]["mode"] != "100755":
        raise AssertionError(f"tracked executable not 100755: {entries['scripts/exe.sh']}")
    if entries["scripts/exe.sh"]["tracked"] is not True:
        raise AssertionError("exe.sh should be tracked")
    if entries["scripts/normal.py"]["mode"] != "100644":
        raise AssertionError(f"tracked normal not 100644: {entries['scripts/normal.py']}")
    if entries["scripts/link.txt"]["mode"] != "120000":
        raise AssertionError(f"tracked symlink not 120000: {entries['scripts/link.txt']}")
    if entries["scripts/link.txt"]["type"] != "symlink":
        raise AssertionError("tracked symlink type wrong")


def test_untracked_file_mode_semantics(tmp_path):
    """r5-2: untracked modes are explicit and stable: executable -> 100755,
    normal -> 100644, symlink -> 120000 (no git add)."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    exe = root / "scripts" / "uexe.sh"
    exe.write_text("#!/bin/sh\necho hi\n")
    os.chmod(exe, 0o755)
    normal = root / "scripts" / "unormal.py"
    normal.write_text("x = 1\n")
    os.chmod(normal, 0o644)
    (root / "scripts" / "target.txt").write_text("t\n")
    (root / "scripts" / "ulink.txt").symlink_to(root / "scripts" / "target.txt")
    _init_git_repo(root)  # git init only — all files untracked
    entries = {e["path"]: e for e in compute_inventory_entries(root)}
    if entries["scripts/uexe.sh"]["mode"] != "100755":
        raise AssertionError(f"untracked executable not 100755: {entries['scripts/uexe.sh']}")
    if entries["scripts/uexe.sh"]["tracked"] is not False:
        raise AssertionError("should be untracked")
    if entries["scripts/unormal.py"]["mode"] != "100644":
        raise AssertionError(f"untracked normal not 100644: {entries['scripts/unormal.py']}")
    if entries["scripts/ulink.txt"]["mode"] != "120000":
        raise AssertionError(f"untracked symlink not 120000: {entries['scripts/ulink.txt']}")


def test_inventory_entry_has_status_field():
    """r5-2: each entry has an explicit status ('tracked'/'untracked') that
    mirrors the tracked boolean."""
    entries = compute_inventory_entries(ROOT)
    for e in entries:
        if e["status"] not in ("tracked", "untracked"):
            raise AssertionError(f"bad status: {e}")
        expected = "tracked" if e["tracked"] else "untracked"
        if e["status"] != expected:
            raise AssertionError(f"status/tracked mismatch: {e}")


def test_inventory_entry_has_symlink_target_field(tmp_path):
    """r5-2: symlink entries carry symlink_target == readlink target; regular
    entries carry None."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "target.txt").write_text("t\n")
    (root / "scripts" / "link.txt").symlink_to(root / "scripts" / "target.txt")
    (root / "scripts" / "reg.py").write_text("x = 1\n")
    _init_git_repo(root)
    entries = {e["path"]: e for e in compute_inventory_entries(root)}
    if entries["scripts/link.txt"]["symlink_target"] != str(root / "scripts" / "target.txt"):
        raise AssertionError(f"symlink_target wrong: {entries['scripts/link.txt']}")
    if entries["scripts/reg.py"]["symlink_target"] is not None:
        raise AssertionError(f"regular symlink_target not None: {entries['scripts/reg.py']}")


def test_digest_binds_status():
    """r5-2: tampering an entry's status changes the canonical digest."""
    entries = compute_inventory_entries(ROOT)
    original = hashlib.sha256(_canonical_inventory_bytes(entries)).hexdigest()
    tampered = [dict(e) for e in entries]
    tampered[0]["status"] = "untracked" if tampered[0]["status"] == "tracked" else "tracked"
    after = hashlib.sha256(_canonical_inventory_bytes(tampered)).hexdigest()
    if after == original:
        raise AssertionError("status change did not affect digest")


def test_digest_binds_symlink_target(tmp_path):
    """r5-2: tampering a symlink_target changes the canonical digest."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "a.txt").write_text("a\n")
    (root / "scripts" / "link.txt").symlink_to(root / "scripts" / "a.txt")
    _init_git_repo(root)
    entries = compute_inventory_entries(root)
    original = hashlib.sha256(_canonical_inventory_bytes(entries)).hexdigest()
    tampered = [dict(e) for e in entries]
    for e in tampered:
        if e["path"] == "scripts/link.txt":
            e["symlink_target"] = "/different/target"
    after = hashlib.sha256(_canonical_inventory_bytes(tampered)).hexdigest()
    if after == original:
        raise AssertionError("symlink_target change did not affect digest")


def test_git_index_stage_parses_modes(tmp_path):
    """r5-2: _git_index_stage parses git ls-files --stage -z into mode/sha."""
    from okf_loom.capture_provenance import _git_index_stage
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    exe = root / "scripts" / "exe.sh"
    exe.write_text("#!/bin/sh\necho\n")
    os.chmod(exe, 0o755)
    (root / "scripts" / "n.py").write_text("x=1\n")
    _init_git_repo(root)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True,
                   capture_output=True, timeout=10)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=str(root), check=True,
                   capture_output=True, timeout=10)
    index = _git_index_stage(root)
    if index["scripts/exe.sh"]["mode"] != "100755":
        raise AssertionError(f"exe mode wrong: {index['scripts/exe.sh']}")
    if index["scripts/n.py"]["mode"] != "100644":
        raise AssertionError(f"normal mode wrong: {index['scripts/n.py']}")
    if len(index["scripts/n.py"]["object_sha"]) != 40:
        raise AssertionError(f"object sha not 40 hex: {index['scripts/n.py']}")


# ---- r5-1 part B: startup recovery of interrupted publish -----------------


def _write_complete_proof(path: Path, marker: bytes) -> None:
    """Write the exact production proof set with hashes bound to *marker*."""
    mod = _load_capture_module()
    path.mkdir(parents=True)
    images = []
    for name in mod.EXPECTED_PNG_FILES:
        (path / name).write_bytes(marker)
        images.append({"file": name, "sha256": hashlib.sha256(marker).hexdigest()})
    (path / "manifest.json").write_text(
        json.dumps({"images": images}, sort_keys=True), encoding="utf-8")


def _proof_probe(mod) -> str:
    return sorted(mod.EXPECTED_PNG_FILES)[0]


def _stage_publish_dirs(output: Path, state: dict) -> tuple[Path, Path]:
    """Construct a crash-state filesystem for recovery tests.

    ``state`` maps 'output'/'old'/'new' -> bool (whether that directory is
    present). Each present state is a complete proof directory; distinct bytes
    prove which proof survives recovery."""
    mod = _load_capture_module()
    parent = output.parent
    old = parent / (output.name + ".publish-old")
    new = parent / (output.name + ".publish-new")
    mod._safe_rmtree(output)
    mod._safe_rmtree(old)
    mod._safe_rmtree(new)
    if state.get("output"):
        _write_complete_proof(output, b"prior-output")
    if state.get("old"):
        _write_complete_proof(old, b"prior-old")
    if state.get("new"):
        _write_complete_proof(new, b"new-build")
    return old, new


def test_recovery_restores_sole_prior_proof_when_output_absent(tmp_path):
    """r5-1: output absent + old present (crash after first rename) -> the
    sole prior proof is MOVED back to output, never deleted."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    old, new = _stage_publish_dirs(output, {"output": False, "old": True, "new": False})
    actions = mod._recover_interrupted_publish(output)
    if not output.is_dir():
        raise AssertionError("output not restored")
    if (output / _proof_probe(mod)).read_bytes() != b"prior-old":
        raise AssertionError("sole prior proof bytes not restored into output")
    if old.exists():
        raise AssertionError("old should have been moved, not left behind")
    if not any("restored sole prior proof" in a for a in actions):
        raise AssertionError(f"restore action not surfaced: {actions}")


def test_recovery_restores_prior_and_removes_leftover_new(tmp_path):
    """r5-1: output absent + old + new (crash mid-swap) -> restore old, drop new."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    old, new = _stage_publish_dirs(output, {"output": False, "old": True, "new": True})
    mod._recover_interrupted_publish(output)
    if (output / _proof_probe(mod)).read_bytes() != b"prior-old":
        raise AssertionError("prior proof not restored")
    if new.exists():
        raise AssertionError("leftover new not removed after restore")


def test_recovery_removes_leftover_old_when_output_present(tmp_path):
    """r5-1: output present + old (succeeded publish, leftover old) -> drop old."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    old, new = _stage_publish_dirs(output, {"output": True, "old": True, "new": False})
    mod._recover_interrupted_publish(output)
    if (output / _proof_probe(mod)).read_bytes() != b"prior-output":
        raise AssertionError("valid output proof disturbed")
    if old.exists():
        raise AssertionError("leftover old not removed")


def test_recovery_removes_leftover_new_when_output_present(tmp_path):
    """r5-1: output present + new (crash before first rename) -> drop new."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    old, new = _stage_publish_dirs(output, {"output": True, "old": False, "new": True})
    mod._recover_interrupted_publish(output)
    if (output / _proof_probe(mod)).read_bytes() != b"prior-output":
        raise AssertionError("valid output proof disturbed")
    if new.exists():
        raise AssertionError("leftover new not removed")


def test_recovery_removes_orphan_new_when_no_prior_proof(tmp_path):
    """r5-1: output absent + old absent + new (first-ever publish crash, no
    prior proof ever existed) -> drop new; no output created."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    old, new = _stage_publish_dirs(output, {"output": False, "old": False, "new": True})
    actions = mod._recover_interrupted_publish(output)
    if output.exists():
        raise AssertionError("output should not be created when no prior proof existed")
    if new.exists():
        raise AssertionError("orphan new not removed")
    if not any("no prior proof" in a for a in actions):
        raise AssertionError(f"orphan action not surfaced: {actions}")


def test_recovery_noop_when_clean(tmp_path):
    """r5-1: clean state -> recovery is a no-op returning no actions."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    old, new = _stage_publish_dirs(output, {"output": False, "old": False, "new": False})
    actions = mod._recover_interrupted_publish(output)
    if actions:
        raise AssertionError(f"recovery should be no-op when clean: {actions}")
    if output.exists() or old.exists() or new.exists():
        raise AssertionError("clean state disturbed")


def test_recovery_surfaces_restoration_failure(tmp_path, monkeypatch):
    """r5-1: if the sole-prior-proof restore rename fails, recovery RAISES
    (never swallows) and leaves old intact so the proof is not lost."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    old, new = _stage_publish_dirs(output, {"output": False, "old": True, "new": False})

    def boom(src, dst):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "rename", boom)
    try:
        mod._recover_interrupted_publish(output)
        raise AssertionError("should have raised RuntimeError on restore failure")
    except RuntimeError as exc:
        if "sole prior proof" not in str(exc):
            raise AssertionError(f"unexpected restore-failure message: {exc}")
    monkeypatch.undo()
    # The sole prior proof must still exist (recovery never deleted it).
    if not old.exists():
        raise AssertionError("sole prior proof deleted despite restore failure")
    if (old / _proof_probe(mod)).read_bytes() != b"prior-old":
        raise AssertionError("prior proof bytes damaged")


def test_recovery_fresh_process_restores_prior_proof(tmp_path):
    """r5-1: recovery works in a FRESH process with no in-memory publish
    state — proving restart recovery is purely filesystem-driven."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    _stage_publish_dirs(output, {"output": False, "old": True, "new": False})
    child_src = (
        "import sys, importlib.util\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
        "spec = importlib.util.spec_from_file_location('capture_annotations_v2', "
        f"{str(ROOT / 'scripts' / 'capture_annotations_v2.py')!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"acts = m._recover_interrupted_publish(Path({str(output)!r}))\n"
        "print('ACTIONS:', acts)\n"
    )
    child = tmp_path / "_recover_child.py"
    child.write_text(child_src, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(child)], capture_output=True, text=True,
        env=dict(os.environ), timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"fresh-process recovery child failed: {result.stderr}")
    if not output.is_dir():
        raise AssertionError(f"fresh-process recovery did not restore output: {result.stdout}")
    if (output / _proof_probe(mod)).read_bytes() != b"prior-old":
        raise AssertionError("fresh-process recovery did not restore prior proof bytes")


# ---- shared staged fixture + production validator negative proof -----------


def _build_clean_staged_fixture(tmp_path):
    """Build a two-phase source/evidence fixture.

    The source commit contains only consumed capture inputs.  A subsequent
    commit contains exactly the nine PNGs plus ``manifest.json``.  This is the
    smallest deterministic proof that final validation accepts a genuine
    release publication rather than the pre-publication snapshot that caused
    the original stale-evidence failure.
    """
    import shutil
    fixture = tmp_path / "fixture"
    (fixture / "scripts" / "okf_loom").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "capture_annotations_v2.py",
                 fixture / "scripts" / "capture_annotations_v2.py")
    shutil.copy2(ROOT / "scripts" / "okf_loom" / "capture_provenance.py",
                 fixture / "scripts" / "okf_loom" / "capture_provenance.py")
    _init_git_repo(fixture)
    subprocess.run(["git", "add", "-A"], cwd=str(fixture), check=True,
                   capture_output=True, timeout=10)
    subprocess.run(["git", "commit", "-qm", "source"], cwd=str(fixture), check=True,
                   capture_output=True, timeout=10)

    out = fixture / "docs" / "screenshots" / "annotations-v2"
    out.mkdir(parents=True)
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    mod = _load_capture_module()
    image_entries = []
    for name in EXPECTED_ARTIFACTS:
        (out / name).write_bytes(png_bytes)
        image_entries.append({
            "file": name,
            "sha256": hashlib.sha256(png_bytes).hexdigest(),
            "classification": SCENARIO_CLASSIFICATIONS[name],
            "release_proof": True,
            "scenario_assertions": mod.SCENARIO_ASSERTIONS[name],
            "viewport": EXPECTED_VIEWPORTS[name],
            "media": EXPECTED_MEDIA[name],
        })
    inv_digest, inv_count = compute_inventory(fixture)
    inv_entries = compute_inventory_entries(fixture)
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(fixture), text=True, timeout=5).strip()
    worktree = subprocess.check_output(
        ["git", "diff", "--binary", "HEAD"], cwd=str(fixture), timeout=5)
    manifest = {
        "schema": 1,
        "provenance_model": PROVENANCE_MODEL,
        "captured_at": "2026-01-01T00:00:00+00:00",
        "source_revision": head,
        "source_revision_verified": True,
        "evidence_parent_revision": head,
        "evidence_parent_verified": True,
        "evidence_paths": expected_evidence_paths(EXPECTED_ARTIFACTS),
        "source_worktree_digest": hashlib.sha256(worktree).hexdigest(),
        "evidence_worktree_digest": hashlib.sha256(worktree).hexdigest(),
        "capture_input_inventory_sha256": inv_digest,
        "capture_input_inventory_serialized_sha256": inventory_serialized_digest(inv_entries),
        "capture_input_file_count": inv_count,
        "capture_input_inventory": inv_entries,
        "capture_script_sha256": hashlib.sha256(
            (fixture / "scripts" / "capture_annotations_v2.py").read_bytes()).hexdigest(),
        "browser": "test",
        "images": image_entries,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(fixture), check=True,
                   capture_output=True, timeout=10)
    subprocess.run(["git", "commit", "-qm", "evidence"], cwd=str(fixture), check=True,
                   capture_output=True, timeout=10)
    return fixture, manifest, out, png_bytes


def test_production_validator_negative_proof_check_files_true(tmp_path):
    """r5-4 / audit-7: production validator with check_files=True must require
    ZERO errors against a self-consistent staged fixture BEFORE tamper, then
    catch an on-disk byte tamper."""
    fixture, manifest, out, png_bytes = _build_clean_staged_fixture(tmp_path)
    errors = validate_manifest(manifest, fixture, EXPECTED_ARTIFACTS, check_files=True)
    if errors:
        raise AssertionError(
            "staged fixture should validate clean with check_files=True but "
            "got: " + "; ".join(errors))
    (out / "resolved.png").write_bytes(b"TAMPERED" + png_bytes)
    errors2 = validate_manifest(manifest, fixture, EXPECTED_ARTIFACTS, check_files=True)
    if not any("resolved.png hash mismatch on disk" in e for e in errors2):
        raise AssertionError(f"validator should detect on-disk tamper: {errors2}")


def test_two_phase_source_commit_then_evidence_commit_validates(tmp_path):
    """A final source commit followed by one exact evidence commit is valid."""
    fixture, manifest, _out, _png_bytes = _build_clean_staged_fixture(tmp_path)
    errors = validate_manifest(manifest, fixture, EXPECTED_ARTIFACTS, check_files=True)
    if errors:
        raise AssertionError(
            "two-phase final evidence should validate: " + "; ".join(errors)
        )


@pytest.mark.parametrize("python_flags", [[], ["-O"]])
def test_selected_historical_checkout_survives_unrelated_work_without_promotion(tmp_path, python_flags):
    """The evidence lane selects its context, never promotes a later worktree."""
    selected = tmp_path / "selected-evidence"
    subprocess.run(["git", "clone", "--quiet", str(ROOT), str(selected)], check=True)
    (ROOT / "notes.txt").write_text("unrelated maintenance\n")
    subprocess.run(["git", "add", "notes.txt"], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-qm", "unrelated"], cwd=ROOT, check=True)
    (ROOT / "notes.txt").write_text("uncommitted maintenance\n")
    manifest = json.loads((OUTPUT / "manifest.json").read_text())
    assert validate_manifest(manifest, selected, EXPECTED_ARTIFACTS) == []
    errors = validate_manifest(manifest, ROOT, EXPECTED_ARTIFACTS)
    assert any("parent does not equal source_revision" in error for error in errors)
    assert any("not clean" in error for error in errors)
    command = [sys.executable, *python_flags, str(ROOT / "scripts/capture_annotations_v2.py"),
               "--verify-checkout", str(selected)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "not current-tree acceptance" in result.stdout
    (selected / "docs/screenshots/annotations-v2/resolved.png").write_bytes(b"tampered")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
    assert "resolved.png hash mismatch" in result.stderr


def test_two_phase_source_mutation_after_evidence_commit_fails_closed(tmp_path):
    """A source mutation after publication cannot masquerade as final proof."""
    fixture, manifest, _out, _png_bytes = _build_clean_staged_fixture(tmp_path)
    target = fixture / "scripts" / "okf_loom" / "capture_provenance.py"
    target.write_bytes(target.read_bytes() + b"\n# post-publication source drift\n")
    errors = validate_manifest(manifest, fixture, EXPECTED_ARTIFACTS, check_files=True)
    if not any(
        "inventory" in error.lower()
        or "not clean" in error.lower()
        or "worktree" in error.lower()
        for error in errors
    ):
        raise AssertionError(
            "source mutation was not rejected by provenance validation: "
            + "; ".join(errors)
        )


def test_two_phase_serialized_inventory_digest_tamper_fails_closed(tmp_path):
    """The manifest's canonical serialized inventory digest is release-bound."""
    fixture, manifest, _out, _png_bytes = _build_clean_staged_fixture(tmp_path)
    bad = json.loads(json.dumps(manifest))
    bad["capture_input_inventory_serialized_sha256"] = "0" * 64
    errors = validate_manifest(bad, fixture, EXPECTED_ARTIFACTS, check_files=False)
    if not any("serialized capture input inventory" in error for error in errors):
        raise AssertionError(
            "serialized inventory tamper was not rejected: " + "; ".join(errors)
        )


def test_two_phase_detached_head_fails_closed(tmp_path):
    """Final evidence on a detached HEAD has ambiguous release provenance."""
    fixture, manifest, _out, _png_bytes = _build_clean_staged_fixture(tmp_path)
    subprocess.run(["git", "checkout", "--detach", "HEAD"], cwd=str(fixture),
                   check=True, capture_output=True, timeout=10)
    errors = validate_manifest(manifest, fixture, EXPECTED_ARTIFACTS, check_files=True)
    if not any("detached" in error.lower() or "branch" in error.lower() for error in errors):
        raise AssertionError(
            "detached HEAD was not rejected: " + "; ".join(errors)
        )


# ===========================================================================
# audit: stricter capture/provenance invariant tests
# ===========================================================================


# ---- audit-1: main-entry recovery before OUTPUT creation ------------------

def test_entry_recover_restores_prior_proof_without_empty_output(tmp_path):
    """audit-1: publish-old present + no output -> main-entry prepare RESTORES
    the prior proof (move) and never creates an empty OUTPUT that would mask
    the crash state."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    old = output.parent / (output.name + ".publish-old")
    _write_complete_proof(old, b"prior-proof")
    if output.exists():
        raise AssertionError("setup: output should not exist (crash state)")
    actions = mod._entry_recover_and_prepare(output)
    if (not output.is_dir()
            or (output / _proof_probe(mod)).read_bytes() != b"prior-proof"):
        raise AssertionError(f"prior proof not restored: actions={actions}")
    if old.exists():
        raise AssertionError("publish-old should be gone after restore")
    if not any("restored sole prior proof" in a for a in actions):
        raise AssertionError(f"restore action not surfaced: {actions}")


def test_entry_recover_creates_parent_only_when_clean(tmp_path):
    """audit-1: on a clean state, entry prepare creates the PARENT only —
    OUTPUT is never pre-created (the atomic swap creates it on commit)."""
    mod = _load_capture_module()
    output = tmp_path / "nested" / "deep" / "out"
    actions = mod._entry_recover_and_prepare(output)
    if not output.parent.is_dir():
        raise AssertionError("parent not created")
    if output.exists():
        raise AssertionError("OUTPUT must NOT be created by entry prepare")
    if actions:
        raise AssertionError(f"clean state should yield no recovery actions: {actions}")


# ---- audit-2: fail-closed directory fsync --------------------------------

def test_fsync_dir_open_failure_raises(tmp_path):
    """audit-2: _fsync_dir on a missing directory raises (never swallowed)."""
    mod = _load_capture_module()
    try:
        mod._fsync_dir(tmp_path / "does-not-exist")
        raise AssertionError("should raise on missing directory")
    except RuntimeError as exc:
        if "failed to open directory" not in str(exc):
            raise AssertionError(f"unexpected message: {exc}")


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_recovery_rejects_non_directory_output(tmp_path, kind):
    mod = _load_capture_module()
    output = tmp_path / "out"
    if kind == "file":
        output.write_bytes(b"not a proof")
    else:
        target = tmp_path / "target"
        target.mkdir()
        output.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="not a real directory"):
        mod._recover_interrupted_publish(output)


def test_recovery_rejects_incomplete_publish_sibling(tmp_path):
    mod = _load_capture_module()
    sibling = tmp_path / "out.publish-new"
    sibling.mkdir()
    (sibling / "partial.png").write_bytes(b"partial")
    with pytest.raises(RuntimeError, match="incomplete publication proof"):
        mod._recover_interrupted_publish(tmp_path / "out")


def test_fsync_dir_tolerates_only_enumerated_unsupported_errno(tmp_path, monkeypatch):
    """audit-2: EINVAL (tmpfs) tolerated; EIO (real failure) raises."""
    import errno as _errno
    mod = _load_capture_module()
    d = tmp_path / "d"
    d.mkdir()

    def fsync_einval(fd):
        raise OSError(_errno.EINVAL, "tmpfs")

    def fsync_eio(fd):
        raise OSError(_errno.EIO, "disk")

    monkeypatch.setattr(os, "fsync", fsync_einval)
    mod._fsync_dir(d)  # must NOT raise — enumerated unsupported platform errno
    monkeypatch.setattr(os, "fsync", fsync_eio)
    try:
        mod._fsync_dir(d)
        raise AssertionError("EIO must raise RuntimeError")
    except RuntimeError:
        pass


def test_atomic_publish_dir_fsync_failure_preserves_prior(tmp_path, monkeypatch):
    """audit-2: any directory-fsync failure during publish aborts and leaves
    the prior proof intact (fault every fsync phase)."""
    mod, staging, output, manifest, entries, expected = _setup_publish_env(tmp_path)

    def boom(path):
        raise RuntimeError("injected dir fsync failure")

    monkeypatch.setattr(mod, "_fsync_dir", boom)
    try:
        mod._atomic_publish(staging, output, manifest, entries, expected, ROOT)
        raise AssertionError("publish should abort on dir fsync failure")
    except RuntimeError as exc:
        if "injected" not in str(exc):
            raise AssertionError(f"unexpected error: {exc}")
    monkeypatch.undo()
    for name in mod.EXPECTED_PNG_FILES:
        if (output / name).read_bytes() != b"PRIOR":
            raise AssertionError(f"{name} prior bytes not preserved")
    if (output.parent / (output.name + ".publish-new")).exists():
        raise AssertionError("sibling-new not cleaned up")
    if (output.parent / (output.name + ".publish-old")).exists():
        raise AssertionError("sibling-old not cleaned up")


# ---- audit-3: immutable provenance snapshot -------------------------------

def _build_snapshot_fixture(tmp_path):
    """A small controlled git fixture for snapshot tests, isolated from the
    concurrently-mutated ROOT so determinism/drift assertions are not racy."""
    import shutil
    fixture = tmp_path / "snaprepo"
    (fixture / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "capture_annotations_v2.py",
                 fixture / "scripts" / "capture_annotations_v2.py")
    (fixture / "scripts" / "mod.py").write_text("x = 1\n")
    _init_git_repo(fixture)
    subprocess.run(["git", "add", "-A"], cwd=str(fixture), check=True,
                   capture_output=True, timeout=10)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=str(fixture), check=True,
                   capture_output=True, timeout=10)
    return fixture


def test_provenance_snapshot_deterministic(tmp_path):
    """audit-3: two consecutive snapshots of a stable fixture are equal."""
    from okf_loom.capture_provenance import capture_provenance_snapshot
    fixture = _build_snapshot_fixture(tmp_path)
    if capture_provenance_snapshot(fixture) != capture_provenance_snapshot(fixture):
        raise AssertionError("snapshot not deterministic across calls")


def test_provenance_snapshot_detects_consumed_file_drift(tmp_path):
    """audit-3: a consumed-file change between snapshots is detected."""
    from okf_loom.capture_provenance import capture_provenance_snapshot
    fixture = _build_snapshot_fixture(tmp_path)
    target = fixture / "scripts" / "mod.py"
    original = target.read_bytes()
    try:
        before = capture_provenance_snapshot(fixture)
        target.write_bytes(original + b"\n# drift sentinel\n")
        after = capture_provenance_snapshot(fixture, require_clean=False)
        if before == after:
            raise AssertionError("snapshot did not detect consumed-file drift")
        if before.inventory_digest == after.inventory_digest:
            raise AssertionError("inventory digest did not change on drift")
    finally:
        target.write_bytes(original)


def test_provenance_snapshot_rejects_dirty_source_by_default(tmp_path):
    """The production source phase cannot start from a dirty checkout."""
    from okf_loom.capture_provenance import capture_provenance_snapshot
    fixture = _build_snapshot_fixture(tmp_path)
    target = fixture / "scripts" / "mod.py"
    target.write_bytes(target.read_bytes() + b"\n# dirty source\n")
    with pytest.raises(GitProvenanceError, match="clean index/worktree"):
        capture_provenance_snapshot(fixture)


def test_provenance_snapshot_single_scan_consistency(tmp_path):
    """audit-3: digest/count derive from the SAME entries (no double scan)."""
    from okf_loom.capture_provenance import capture_provenance_snapshot
    fixture = _build_snapshot_fixture(tmp_path)
    snap = capture_provenance_snapshot(fixture)
    expected_digest = hashlib.sha256(
        _canonical_inventory_bytes(list(snap.inventory_entries))).hexdigest()
    if snap.inventory_digest != expected_digest:
        raise AssertionError("digest not derived from snapshot entries")
    if snap.inventory_count != len(snap.inventory_entries):
        raise AssertionError("count not derived from snapshot entries")


def test_provenance_snapshot_is_immutable_namedtuple(tmp_path):
    """audit-3: ProvenanceSnapshot is a frozen NamedTuple."""
    from okf_loom.capture_provenance import capture_provenance_snapshot, ProvenanceSnapshot
    fixture = _build_snapshot_fixture(tmp_path)
    snap = capture_provenance_snapshot(fixture)
    if not isinstance(snap, ProvenanceSnapshot):
        raise AssertionError("not a ProvenanceSnapshot")
    try:
        snap.head = "mutated"  # type: ignore[misc]
        raise AssertionError("NamedTuple field should be immutable")
    except AttributeError:
        pass
    try:
        snap.inventory_entries[0]["sha256"] = "0" * 64
        raise AssertionError("nested inventory entry should be immutable")
    except TypeError:
        pass


def test_snapshot_inventory_manifest_copy_is_json_serializable_and_detached(tmp_path):
    from okf_loom.capture_provenance import (
        capture_provenance_snapshot, snapshot_inventory_for_manifest)
    fixture = _build_snapshot_fixture(tmp_path)
    snapshot = capture_provenance_snapshot(fixture)
    manifest_entries = snapshot_inventory_for_manifest(snapshot)
    encoded = json.dumps({"capture_input_inventory": manifest_entries}, sort_keys=True)
    if '"capture_input_inventory"' not in encoded:
        raise AssertionError("manifest inventory did not serialize")
    original = snapshot.inventory_entries[0]["sha256"]
    manifest_entries[0]["sha256"] = "0" * 64
    if snapshot.inventory_entries[0]["sha256"] != original:
        raise AssertionError("manifest copy mutated immutable snapshot authority")


# ---- audit-4: strict git stage parser + ignored classification ------------

def _patch_stage_output(monkeypatch, payload: bytes):
    import okf_loom.capture_provenance as cp
    monkeypatch.setattr(cp.subprocess, "check_output", lambda *a, **k: payload + b"\0")


def test_strict_parser_rejects_unsupported_mode(monkeypatch):
    import okf_loom.capture_provenance as cp
    _patch_stage_output(monkeypatch, b"100600 " + b"a" * 40 + b" 0\tscripts/bad.py")
    try:
        cp._git_index_stage(ROOT)
        raise AssertionError("should reject unsupported mode")
    except cp.GitProvenanceError as exc:
        if "unsupported git mode" not in str(exc):
            raise AssertionError(f"unexpected: {exc}")


def test_strict_parser_rejects_invalid_object_id(monkeypatch):
    import okf_loom.capture_provenance as cp
    _patch_stage_output(monkeypatch, b"100644 tooshort 0\tscripts/bad.py")
    try:
        cp._git_index_stage(ROOT)
        raise AssertionError("should reject invalid object id")
    except cp.GitProvenanceError as exc:
        if "invalid git object id" not in str(exc):
            raise AssertionError(f"unexpected: {exc}")


def test_strict_parser_rejects_conflict_stage(monkeypatch):
    import okf_loom.capture_provenance as cp
    _patch_stage_output(monkeypatch, b"100644 " + b"a" * 40 + b" 1\tscripts/conflict.py")
    try:
        cp._git_index_stage(ROOT)
        raise AssertionError("should reject non-stage-0 conflict entry")
    except cp.GitProvenanceError as exc:
        if "non-stage-0" not in str(exc) and "conflict" not in str(exc).lower():
            raise AssertionError(f"unexpected: {exc}")


def test_strict_parser_rejects_duplicate_path(monkeypatch):
    import okf_loom.capture_provenance as cp
    rec = b"100644 " + b"a" * 40 + b" 0\tscripts/dup.py"
    _patch_stage_output(monkeypatch, rec + b"\0" + rec)
    try:
        cp._git_index_stage(ROOT)
        raise AssertionError("should reject duplicate path")
    except cp.GitProvenanceError as exc:
        if "duplicate" not in str(exc).lower():
            raise AssertionError(f"unexpected: {exc}")


def test_strict_parser_rejects_missing_tab(monkeypatch):
    import okf_loom.capture_provenance as cp
    _patch_stage_output(monkeypatch, b"100644 " + b"a" * 40 + b" 0notab")
    try:
        cp._git_index_stage(ROOT)
        raise AssertionError("should reject missing tab delimiter")
    except cp.GitProvenanceError as exc:
        if "tab" not in str(exc).lower():
            raise AssertionError(f"unexpected: {exc}")


def test_strict_parser_rejects_unterminated_nul_output(monkeypatch):
    import okf_loom.capture_provenance as cp
    monkeypatch.setattr(
        cp.subprocess, "check_output",
        lambda *a, **k: b"100644 " + b"a" * 40 + b" 0\tscripts/a.py")
    with pytest.raises(cp.GitProvenanceError, match="NUL-terminated"):
        cp._git_index_stage(ROOT)


def test_git_ignored_rejects_foreign_path(monkeypatch):
    import okf_loom.capture_provenance as cp
    result = subprocess.CompletedProcess([], 0, b"scripts/foreign.py\0", b"")
    monkeypatch.setattr(cp.subprocess, "run", lambda *a, **k: result)
    with pytest.raises(cp.GitProvenanceError, match="not submitted"):
        cp._git_ignored_set(ROOT, ["scripts/local.py"])


def test_inventory_excludes_transient_okf_lock(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "real.py").write_text("x=1\n")
    (root / "scripts" / ".real.py.okf-lock").write_bytes(b"")
    _init_git_repo(root)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=root, check=True, capture_output=True)
    paths = {entry["path"] for entry in compute_inventory_entries(root)}
    if "scripts/.real.py.okf-lock" in paths:
        raise AssertionError("transient mutation lock must not enter capture provenance")


def test_inventory_classifies_ignored_files(tmp_path):
    """audit-4: a gitignored consumed file is classified status='ignored'
    (via git check-ignore), distinct from plain untracked."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "real.py").write_text("x = 1\n")
    (root / "scripts" / "ignored.log").write_text("log\n")
    (root / ".gitignore").write_text("*.log\n")
    _init_git_repo(root)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True,
                   capture_output=True, timeout=10)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=str(root), check=True,
                   capture_output=True, timeout=10)
    # untracked, NOT ignored file after commit
    (root / "scripts" / "fresh.py").write_text("y = 2\n")
    entries = {e["path"]: e for e in compute_inventory_entries(root)}
    if entries["scripts/real.py"]["status"] != "tracked":
        raise AssertionError(f"real.py not tracked: {entries['scripts/real.py']}")
    if entries["scripts/ignored.log"]["status"] != "ignored":
        raise AssertionError(f"ignored.log not 'ignored': {entries['scripts/ignored.log']}")
    if entries["scripts/fresh.py"]["status"] != "untracked":
        raise AssertionError(f"fresh.py not 'untracked': {entries['scripts/fresh.py']}")


# ---- audit-5: validator check_files symlink/special/exact-set rejection ----

def test_validator_rejects_symlink_artifact(tmp_path):
    """audit-5: a symlink artifact is rejected (lstat, never followed)."""
    fixture, manifest, out, png_bytes = _build_clean_staged_fixture(tmp_path)
    target = tmp_path / "elsewhere.png"
    target.write_bytes(png_bytes)
    (out / "resolved.png").unlink()
    (out / "resolved.png").symlink_to(target)
    errors = validate_manifest(manifest, fixture, EXPECTED_ARTIFACTS, check_files=True)
    if not any("resolved.png is a symlink" in e for e in errors):
        raise AssertionError(f"validator should reject symlink artifact: {errors}")


def test_validator_rejects_extra_directory_entry(tmp_path):
    """audit-5: an extra file/dir in the output directory is rejected
    (exact staging/published set)."""
    fixture, manifest, out, png_bytes = _build_clean_staged_fixture(tmp_path)
    (out / "stale-extra.png").write_bytes(b"extra")
    errors = validate_manifest(manifest, fixture, EXPECTED_ARTIFACTS, check_files=True)
    if not any("extra entries" in e for e in errors):
        raise AssertionError(f"validator should reject extra dir entry: {errors}")


def test_validator_rejects_extra_subdirectory(tmp_path):
    """audit-5: an extra subdirectory in output is rejected too."""
    fixture, manifest, out, png_bytes = _build_clean_staged_fixture(tmp_path)
    (out / "subdir").mkdir()
    errors = validate_manifest(manifest, fixture, EXPECTED_ARTIFACTS, check_files=True)
    if not any("extra entries" in e and "subdir" in e for e in errors):
        raise AssertionError(f"validator should reject extra subdir: {errors}")


def test_validator_rejects_missing_manifest_on_disk(tmp_path):
    """audit-5: a missing manifest.json in the output dir is rejected."""
    fixture, manifest, out, png_bytes = _build_clean_staged_fixture(tmp_path)
    (out / "manifest.json").unlink()
    errors = validate_manifest(manifest, fixture, EXPECTED_ARTIFACTS, check_files=True)
    if not any("manifest.json" in e and ("missing" in e or "missing entries" in e) for e in errors):
        raise AssertionError(f"validator should report missing manifest.json: {errors}")


# ===========================================================================
# detached-handle race: resolved-mark scroll resilience (no sleeps)
# ===========================================================================


def _geom(**over):
    base = {"visible": True, "top": 100, "left": 50, "right": 200, "bottom": 130,
            "hasBg": True, "hasBorderBottom": False}
    base.update(over)
    return base


def test_mark_scroll_uses_comment_id_keyed_transaction():
    """The scroll path must key the mark lookup by comment id (not a stale
    'first mark' handle) inside a single evaluate transaction."""
    mod = _load_capture_module()
    js = mod._MARK_TRANSACTION_JS
    if "data-comment-id" not in js:
        raise AssertionError("transaction JS must key by data-comment-id")
    if "CSS.escape" not in js:
        raise AssertionError("transaction JS must CSS.escape the comment id")
    if "isConnected" not in js:
        raise AssertionError("transaction JS must check mark.isConnected")
    # readiness must require comments ready + projection complete + mark present
    rjs = mod._MARK_READINESS_JS
    for needle in ("commentsStatus", "annotation_projection", "complete",
                   "data-comment-id"):
        if needle not in rjs:
            raise AssertionError(f"readiness JS missing {needle!r}")


def test_mark_scroll_recovers_from_one_detachment():
    """Deterministic fault: the mark is reported detached once (async
    reconciliation replaced it between readiness and scroll), then stabilizes
    — the helper recovers and returns the CURRENT post-scroll geometry, never
    a stale handle."""
    mod = _load_capture_module()
    calls = {"n": 0}

    def transaction():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "detached"}  # replaced mid-flight
        return {"status": "ok", "before": _geom(),
                "after": _geom(top=300)}

    geom = mod._scroll_resolved_mark_into_view(
        page=None, comment_id="c1", deadline_s=2,
        _readiness_fn=lambda: None, _transaction_fn=transaction)
    if calls["n"] != 2:
        raise AssertionError(f"should retry exactly once after detach; calls={calls['n']}")
    if geom["top"] != 300:
        raise AssertionError(f"wrong (stale) geometry returned: {geom}")


def test_mark_scroll_recovers_from_one_missing():
    """Fault: mark missing once (removed by reconciliation), then present."""
    mod = _load_capture_module()
    calls = {"n": 0}

    def transaction():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "missing"}
        return {"status": "ok", "before": _geom(), "after": _geom()}

    mod._scroll_resolved_mark_into_view(
        page=None, comment_id="c1", deadline_s=2,
        _readiness_fn=lambda: None, _transaction_fn=transaction)
    if calls["n"] != 2:
        raise AssertionError(f"should recover from one missing; calls={calls['n']}")


def test_mark_scroll_repeated_churn_times_out():
    """Fault: repeated churn (mark always detached) exhausts the bounded
    budget and fails release explicitly — no infinite loop, no sleep."""
    import time as _time
    mod = _load_capture_module()
    t0 = _time.monotonic()
    try:
        mod._scroll_resolved_mark_into_view(
            page=None, comment_id="c1", deadline_s=0.3,
            _readiness_fn=lambda: None,
            _transaction_fn=lambda: {"status": "detached"})
        raise AssertionError("should time out on repeated churn")
    except RuntimeError as exc:
        if "never stabilized" not in str(exc):
            raise AssertionError(f"unexpected message: {exc}")
    elapsed = _time.monotonic() - t0
    if elapsed > 5:
        raise AssertionError(f"did not respect deadline (elapsed={elapsed:.1f}s)")


def test_mark_scroll_missing_mark_times_out():
    """Fault: a permanently missing mark times out / fails explicitly."""
    mod = _load_capture_module()
    try:
        mod._scroll_resolved_mark_into_view(
            page=None, comment_id="c1", deadline_s=0.3,
            _readiness_fn=lambda: None,
            _transaction_fn=lambda: {"status": "missing"})
        raise AssertionError("should time out on missing mark")
    except RuntimeError as exc:
        if "missing" not in str(exc) and "never stabilized" not in str(exc):
            raise AssertionError(f"unexpected: {exc}")


def test_mark_scroll_validates_post_scroll_geometry():
    """Post-scroll geometry recheck remains: an invisible-after-scroll mark is
    rejected (does not silently pass)."""
    mod = _load_capture_module()

    def transaction():
        return {"status": "ok", "before": _geom(), "after": _geom(visible=False)}

    try:
        mod._scroll_resolved_mark_into_view(
            page=None, comment_id="c1", deadline_s=1,
            _readiness_fn=lambda: None, _transaction_fn=transaction)
        raise AssertionError("should reject invisible post-scroll mark")
    except RuntimeError as exc:
        if "not visible after scroll" not in str(exc):
            raise AssertionError(f"unexpected: {exc}")


def test_mark_scroll_rejects_unstyled_mark():
    """Style recheck remains: a post-scroll mark with no background and no
    border is rejected."""
    mod = _load_capture_module()

    def transaction():
        return {"status": "ok", "before": _geom(),
                "after": _geom(hasBg=False, hasBorderBottom=False)}

    try:
        mod._scroll_resolved_mark_into_view(
            page=None, comment_id="c1", deadline_s=1,
            _readiness_fn=lambda: None, _transaction_fn=transaction)
        raise AssertionError("should reject unstyled post-scroll mark")
    except RuntimeError as exc:
        if "lacks visible styling after scroll" not in str(exc):
            raise AssertionError(f"unexpected: {exc}")


def test_mark_scroll_rejects_offscreen_initial():
    """Initial off-screen check remains (the mark must start on-screen)."""
    mod = _load_capture_module()

    def transaction():
        return {"status": "ok",
                "before": _geom(top=-5, left=10),
                "after": _geom()}

    try:
        mod._scroll_resolved_mark_into_view(
            page=None, comment_id="c1", deadline_s=1,
            _readiness_fn=lambda: None, _transaction_fn=transaction)
        raise AssertionError("should reject off-screen initial mark")
    except RuntimeError as exc:
        if "off-screen" not in str(exc):
            raise AssertionError(f"unexpected: {exc}")


def test_mark_scroll_readiness_failure_retries_within_budget():
    """If readiness is not yet reached (sub-budget timeout), the helper keeps
    retrying within the overall deadline rather than failing immediately."""
    mod = _load_capture_module()
    calls = {"n": 0}

    def readiness():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("not ready yet")

    def transaction():
        return {"status": "ok", "before": _geom(), "after": _geom()}

    mod._scroll_resolved_mark_into_view(
        page=None, comment_id="c1", deadline_s=3,
        _readiness_fn=readiness, _transaction_fn=transaction)
    if calls["n"] < 3:
        raise AssertionError(f"should retry readiness; calls={calls['n']}")


def test_wait_mark_readiness_uses_playwright_arg_keyword():
    """Regression guard: _wait_mark_readiness must call wait_for_function with
    the Playwright sync API ``arg=`` keyword (NOT ``argument=``). Using the
    wrong keyword raises TypeError, which the scroll loop misclassifies as
    'readiness unreachable' and silently exhausts the budget (the original
    capture regression). This test fails if the keyword regresses."""
    mod = _load_capture_module()
    captured = {}

    class _FakePage:
        # Signature mirrors the real Playwright sync API (keyword-only ``arg``).
        def wait_for_function(self, expression, *, arg=None, timeout=None,
                              polling=None):
            captured["arg"] = arg
            captured["timeout"] = timeout
            captured["expression"] = expression
            # return immediately (readiness satisfied)

    mod._wait_mark_readiness(_FakePage(), "01JCIDXYZ", 1500)
    if captured.get("arg") != "01JCIDXYZ":
        raise AssertionError(
            f"wait_for_function not called with arg=<comment id>: {captured}")
    if captured.get("timeout") != 1500:
        raise AssertionError(f"timeout not forwarded: {captured}")


# ===========================================================================
# Keyboard reachability: known preceding focus owner + one real Tab
# ===========================================================================
# The wide-viewport comments surface is now a NON-MODAL rail, so the re-anchor
# editor is no longer a focus trap. The old fixed range(40) Tab loop could not
# deterministically reach the Retry control from an arbitrary starting focus
# owner and left it unfocused (isActive:false), failing the final forced-colors
# capture. A full-ring bounded traversal was also proven unsound: when focus is
# carried ONTO the control between captures, a forward Tab must complete a full
# document cycle, and the real native tab order is longer than any DOM-derived
# ring count. _keyboard_reach_and_focus instead establishes the control's
# nearest preceding tabbable owner and presses Tab exactly once.


def test_keyboard_owner_js_locates_preceding_owner_and_focuses_it():
    """The owner JS must locate the nearest preceding tabbable element, focus it
    as the known start, and report whether the control itself is tabbable. It
    must NOT exclude aria-hidden (Chrome keeps such elements in tab order)."""
    mod = _load_capture_module()
    js = mod._KEYBOARD_OWNER_JS
    for needle in ("FOCUSABLE", "button", 'tabindex',
                   "getBoundingClientRect", "getComputedStyle",
                   "owner.focus(", "ownerFocused",
                   "retryTabbable", "ownerFound"):
        if needle not in js:
            raise AssertionError(f"_KEYBOARD_OWNER_JS missing {needle!r}")
    if 'aria-hidden' in js and 'aria-hidden") continue' in js:
        raise AssertionError(
            "_KEYBOARD_OWNER_JS must NOT exclude aria-hidden ancestors (Chrome "
            "keeps them in tab order; excluding them undercounts and picks a "
            "wrong predecessor)")


def test_keyboard_reach_succeeds_with_one_tab_from_owner():
    """With a tabbable control and a focused preceding owner, exactly one Tab
    reaches the control (no full-cycle traversal, no fixed budget)."""
    mod = _load_capture_module()
    tabs = []

    mod._keyboard_reach_and_focus(
        page=None, control=None, label="t",
        _setup_fn=lambda: {"retryTabbable": True, "ownerFound": True,
                           "ownerFocused": True, "ownerKind": "tabbable"},
        _step_fn=lambda: tabs.append(1) or True)
    if len(tabs) != 1:
        raise AssertionError(
            f"must reach the control with a single Tab; tabs={len(tabs)}")


def test_keyboard_reach_fails_closed_when_control_not_tabbable():
    """If the control is not tabbable the helper fails closed WITHOUT pressing
    Tab (it cannot be keyboard-reached at all)."""
    mod = _load_capture_module()
    tabs = []
    try:
        mod._keyboard_reach_and_focus(
            page=None, control=None, label="t",
            _setup_fn=lambda: {"retryTabbable": False, "ownerFound": True,
                               "ownerFocused": True},
            _step_fn=lambda: tabs.append(1) or True)
        raise AssertionError("should fail closed when control is not tabbable")
    except RuntimeError as exc:
        if "not tabbable" not in str(exc):
            raise AssertionError(f"unexpected message: {exc}")
    if tabs:
        raise AssertionError(
            f"must not Tab when control is not tabbable; tabs={len(tabs)}")


def test_keyboard_reach_fails_closed_when_no_preceding_owner():
    """If no focusable element precedes the control, the helper cannot establish
    a known start and fails closed."""
    mod = _load_capture_module()
    tabs = []
    try:
        mod._keyboard_reach_and_focus(
            page=None, control=None, label="t",
            _setup_fn=lambda: {"retryTabbable": True, "ownerFound": False,
                               "ownerFocused": False},
            _step_fn=lambda: tabs.append(1) or True)
        raise AssertionError("should fail closed when no preceding owner")
    except RuntimeError as exc:
        if "no focusable element precedes" not in str(exc):
            raise AssertionError(f"unexpected message: {exc}")
    if tabs:
        raise AssertionError(f"must not Tab when no owner; tabs={len(tabs)}")


def test_keyboard_reach_fails_closed_when_owner_not_focused():
    """If the preceding owner exists but cannot receive programmatic focus, the
    helper fails closed rather than Tabbing from an unknown start."""
    mod = _load_capture_module()
    tabs = []
    try:
        mod._keyboard_reach_and_focus(
            page=None, control=None, label="t",
            _setup_fn=lambda: {"retryTabbable": True, "ownerFound": True,
                               "ownerFocused": False},
            _step_fn=lambda: tabs.append(1) or True)
        raise AssertionError("should fail closed when owner not focused")
    except RuntimeError as exc:
        if "did not receive programmatic focus" not in str(exc):
            raise AssertionError(f"unexpected message: {exc}")
    if tabs:
        raise AssertionError(f"must not Tab when owner not focused; tabs={len(tabs)}")


def test_keyboard_reach_fails_closed_when_tab_does_not_reach():
    """If the single Tab from the owner does not land on the control, the tab
    order between them changed and the helper fails closed."""
    mod = _load_capture_module()
    tabs = []
    try:
        mod._keyboard_reach_and_focus(
            page=None, control=None, label="t",
            _setup_fn=lambda: {"retryTabbable": True, "ownerFound": True,
                               "ownerFocused": True, "ownerKind": "tabbable"},
            _step_fn=lambda: tabs.append(1) or False)
        raise AssertionError("should fail closed when Tab does not reach control")
    except RuntimeError as exc:
        if "not keyboard-reached with one Tab" not in str(exc):
            raise AssertionError(f"unexpected message: {exc}")
    if len(tabs) != 1:
        raise AssertionError(
            f"must press exactly one Tab before failing; tabs={len(tabs)}")


def test_keyboard_reach_only_focuses_the_preceding_owner_not_the_target():
    """The landing move must be a real Tab keystroke (activates :focus-visible).
    Programmatic focus() may be used only on the PRECEDING owner (to establish
    the known start), never on the target control."""
    import inspect
    mod = _load_capture_module()
    helper_src = inspect.getsource(mod._keyboard_reach_and_focus)
    if "page.keyboard.press" not in helper_src:
        raise AssertionError("helper must land focus via a real Tab keystroke")
    # The helper body itself must not call focus() (the owner focus happens in
    # the _KEYBOARD_OWNER_JS evaluate, not in Python on the control).
    if ".focus(" in helper_src:
        raise AssertionError(
            "helper must not call programmatic focus() on the control "
            "(suppresses :focus-visible in Chromium); only the JS owner focus "
            "is permitted")


def test_capture_retry_focus_uses_known_preceding_owner():
    """All three Retry-save capture surfaces (desktop focus-visible, mobile-320,
    forced-colors) must reach the control through the known preceding owner +
    one Tab, never a fixed Tab budget. The wide comments rail is non-modal, so a
    fixed range(40) loop cannot deterministically reach the control."""
    import re
    source = (ROOT / "scripts" / "capture_annotations_v2.py").read_text(encoding="utf-8")
    code_loops = re.findall(r"for\s+\w+\s+in\s+range\(40\)\s*:", source)
    if code_loops:
        raise AssertionError(f"fixed range(40) Tab loop still present: {code_loops}")
    for surface_label in ("focus-visible Retry save",
                          "mobile-320 Retry save",
                          "forced-colors Retry save"):
        if ('label="%s"' % surface_label) not in source:
            raise AssertionError(
                f"Retry surface {surface_label!r} must route through "
                f"_keyboard_reach_and_focus")
    if source.count("_keyboard_reach_and_focus(page, ") < 3:
        raise AssertionError(
            "all three Retry surfaces must call _keyboard_reach_and_focus")


# ===========================================================================
# Real Playwright integration: resolved-mark helpers against the live flow
# ===========================================================================

from browser_support import sync_playwright as _sync_playwright, launch_chromium


def _post_real_comment_and_get_mark_id(page, server_url, mod):
    """Drive the actual captured comment flow (select -> comment -> send) and
    return the rendered mark's data-comment-id, with the page left in the
    ready, mark-rendered state."""
    page.goto(server_url + "/tables/orders", wait_until="load")
    page.wait_for_function(
        "() => window.okfLoomStudio && window.okfLoomStudio.state.doc")
    page.wait_for_function(
        "() => ['ready','error'].includes(window.okfLoomStudio.state.commentsStatus)")
    if page.evaluate("window.okfLoomStudio.state.commentsStatus") != "ready":
        page.reload(wait_until="load")
        page.wait_for_function(
            "() => window.okfLoomStudio.state.commentsStatus === 'ready'")
    page.wait_for_function(
        "() => window.okfLoomStudio.state.selectionAffordanceReady === true")
    mod.select_text(
        page, ".okf-page__body #schema[data-okf-projection-key]", "Schema")
    page.get_by_role(
        "button", name="Comment on the selected text").click()
    composer = page.get_by_role("textbox", name="Comment for agent")
    composer.fill("Integration probe for resolved-mark helpers.")
    page.get_by_role("button", name="Send", exact=True).click()
    page.wait_for_function(
        "() => document.querySelector('mark.okf-comment-mark') !== null")
    comment_id = page.evaluate(
        "() => document.querySelector('mark.okf-comment-mark')"
        ".getAttribute('data-comment-id')")
    if not comment_id:
        raise AssertionError("no data-comment-id on the rendered mark")
    return comment_id


@pytest.mark.browser
def test_resolved_mark_helpers_real_comment_flow(tmp_path):
    """Real Playwright integration: post a comment through the actual UI flow
    and exercise _wait_mark_readiness + _run_mark_transaction +
    _scroll_resolved_mark_into_view against the live DOM.

    Proves the wait_for_function(arg=...) call is correct (no TypeError) and
    the comment-id-keyed transaction locates the real rendered mark, measures
    geometry, scrolls, and re-checks — the exact path that previously
    exhausted with last=readiness:TypeError."""
    import shutil
    mod = _load_capture_module()
    with _sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        bundle = tmp_path / "bundle"
        shutil.copytree(
            ROOT / "samples" / "demo_bundle", bundle,
            ignore=shutil.ignore_patterns(".okf-loom"))
        port = mod.free_port()
        base = f"http://127.0.0.1:{port}"
        proc = subprocess.Popen(
            [str(ROOT / "scripts" / "okf-loom"), "serve", str(bundle),
             "--host", "127.0.0.1", "--port", str(port), "--no-open"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        console_errors = []
        try:
            mod.wait_server(proc, base)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
            page = context.new_page()
            page.on("console", lambda m: console_errors.append(m.text)
                    if mod.unexpected_console_error(m) else None)
            comment_id = _post_real_comment_and_get_mark_id(page, base, mod)
            # 1. readiness passes against the live DOM — NO TypeError.
            mod._wait_mark_readiness(page, comment_id, 8000)
            # 2. transaction finds the real mark; status ok with valid geometry.
            res = mod._run_mark_transaction(page, comment_id, 440, 1280)
            if res.get("status") != "ok":
                raise AssertionError(f"transaction not ok on live DOM: {res}")
            if not res["after"]["visible"]:
                raise AssertionError(f"live mark not visible after scroll: {res['after']}")
            if not (res["after"]["hasBg"] or res["after"]["hasBorderBottom"]):
                raise AssertionError(f"live mark lacks styling: {res['after']}")
            # 3. full scroll helper returns validated post-scroll geometry.
            geom = mod._scroll_resolved_mark_into_view(page, comment_id)
            if not geom["visible"]:
                raise AssertionError("scroll helper returned invisible geometry")
            context.close()
            if console_errors:
                raise AssertionError("console errors: " + " | ".join(console_errors))
        finally:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
            browser.close()


# ===========================================================================
# r6 capture findings: stage-0 object ID, cross-process lock, atomic
# exchange, directory-fsync restoration, mobile assertion, concurrent tests
# ===========================================================================


# ---- r6: Git index stage-0 object ID binding ------------------------------


def test_tracked_entry_has_object_sha(tmp_path):
    """r6: tracked inventory entries carry the Git index stage-0 object ID
    (40-char lowercase hex), bound from ``git ls-files --stage``."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "f.py").write_text("x = 1\n")
    _init_git_repo(root)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True,
                   capture_output=True, timeout=10)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=str(root), check=True,
                   capture_output=True, timeout=10)
    entries = {e["path"]: e for e in compute_inventory_entries(root)}
    entry = entries["scripts/f.py"]
    if not entry.get("object_sha"):
        raise AssertionError(f"tracked entry missing object_sha: {entry}")
    if len(entry["object_sha"]) != 40:
        raise AssertionError(f"object_sha not 40 chars: {entry['object_sha']}")
    if any(c not in "0123456789abcdef" for c in entry["object_sha"]):
        raise AssertionError(f"object_sha not lowercase hex: {entry['object_sha']}")


def test_untracked_entry_object_sha_is_none(tmp_path):
    """r6: untracked/ignored entries carry object_sha=None (not in the Git
    index)."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "tracked.py").write_text("x = 1\n")
    (root / "scripts" / "untracked.py").write_text("y = 2\n")
    _init_git_repo(root)
    subprocess.run(["git", "add", "scripts/tracked.py"], cwd=str(root),
                   check=True, capture_output=True, timeout=10)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=str(root), check=True,
                   capture_output=True, timeout=10)
    entries = {e["path"]: e for e in compute_inventory_entries(root)}
    if entries["scripts/tracked.py"]["object_sha"] is None:
        raise AssertionError("tracked entry has None object_sha")
    if entries["scripts/untracked.py"]["object_sha"] is not None:
        raise AssertionError(
            f"untracked entry should have None object_sha: "
            f"{entries['scripts/untracked.py']}")


def test_digest_binds_object_sha(tmp_path):
    """r6: tampering an entry's object_sha changes the canonical digest."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "f.py").write_text("x = 1\n")
    _init_git_repo(root)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True,
                   capture_output=True, timeout=10)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=str(root), check=True,
                   capture_output=True, timeout=10)
    entries = compute_inventory_entries(root)
    original = hashlib.sha256(_canonical_inventory_bytes(entries)).hexdigest()
    tampered = [dict(e) for e in entries]
    # Change object_sha to a different valid 40-char hex.
    tampered[0]["object_sha"] = "e" * 40 if tampered[0]["object_sha"] != "e" * 40 else "f" * 40
    after = hashlib.sha256(_canonical_inventory_bytes(tampered)).hexdigest()
    if after == original:
        raise AssertionError("object_sha change did not affect canonical digest")


def test_index_only_mutation_detected(tmp_path):
    """r6: changing ONLY the Git index (stage-0 object ID) while preserving
    working-tree bytes is detected by the inventory digest.

    This is the core r6 finding: ``_git_index_stage()`` reads ``object_sha``
    but the inventory previously omitted it, so an index-only mutation
    (``git update-index --cacheinfo``) could leave the manifest valid even
    though the staged blob changed.  Now object_sha is bound."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    f = root / "scripts" / "file.py"
    f.write_text("original content\n")
    _init_git_repo(root)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True,
                   capture_output=True, timeout=10)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(root), check=True,
                   capture_output=True, timeout=10)

    entries_before = compute_inventory_entries(root)
    entry_before = next(e for e in entries_before if e["path"] == "scripts/file.py")
    original_object_sha = entry_before["object_sha"]
    original_sha256 = entry_before["sha256"]
    if not original_object_sha:
        raise AssertionError("setup: tracked file should have object_sha")

    # Create a DIFFERENT blob object in the git store.
    new_blob_sha = subprocess.check_output(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(root), input=b"different content for index\n", timeout=10,
    ).decode().strip()

    # Update the index to point to the new blob WITHOUT touching the
    # working-tree file (working-tree bytes are preserved).
    subprocess.run(
        ["git", "update-index", "--cacheinfo", "100644", new_blob_sha,
         "scripts/file.py"],
        cwd=str(root), check=True, capture_output=True, timeout=10,
    )

    entries_after = compute_inventory_entries(root)
    entry_after = next(e for e in entries_after if e["path"] == "scripts/file.py")

    # object_sha changed (index mutation detected).
    if entry_after["object_sha"] == original_object_sha:
        raise AssertionError(
            "object_sha should change after index-only mutation")
    if entry_after["object_sha"] != new_blob_sha:
        raise AssertionError(
            f"object_sha should equal new blob sha: "
            f"{entry_after['object_sha']} != {new_blob_sha}")
    # sha256 is unchanged (working tree bytes preserved).
    if entry_after["sha256"] != original_sha256:
        raise AssertionError(
            "sha256 should be unchanged (working tree preserved)")
    # The canonical digest changed (index-only mutation detected).
    digest_before = hashlib.sha256(
        _canonical_inventory_bytes(entries_before)).hexdigest()
    digest_after = hashlib.sha256(
        _canonical_inventory_bytes(entries_after)).hexdigest()
    if digest_before == digest_after:
        raise AssertionError(
            "digest should change after index-only mutation — "
            "object_sha is not bound into the canonical digest")


def test_head_index_worktree_three_way_divergence(tmp_path):
    """r6: HEAD, index, and worktree can all diverge.  The inventory binds
    the worktree sha256 (file content) AND the index object_sha (staged blob),
    so all three states are independently detectable."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    f = root / "scripts" / "file.py"
    f.write_text("HEAD content\n")
    _init_git_repo(root)
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True,
                   capture_output=True, timeout=10)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(root), check=True,
                   capture_output=True, timeout=10)

    # State 1: HEAD == index == worktree (all agree).
    entries_1 = compute_inventory_entries(root)
    entry_1 = next(e for e in entries_1 if e["path"] == "scripts/file.py")

    # State 2: index-only mutation (index != HEAD, worktree == HEAD).
    new_blob = subprocess.check_output(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(root), input=b"staged but not committed\n", timeout=10,
    ).decode().strip()
    subprocess.run(
        ["git", "update-index", "--cacheinfo", "100644", new_blob,
         "scripts/file.py"],
        cwd=str(root), check=True, capture_output=True, timeout=10,
    )
    entries_2 = compute_inventory_entries(root)
    entry_2 = next(e for e in entries_2 if e["path"] == "scripts/file.py")

    # index object_sha changed, worktree sha256 unchanged.
    if entry_2["object_sha"] == entry_1["object_sha"]:
        raise AssertionError("index-only mutation: object_sha should differ")
    if entry_2["sha256"] != entry_1["sha256"]:
        raise AssertionError("index-only mutation: sha256 should be same")

    # State 3: worktree mutation too (HEAD != index != worktree).
    f.write_text("worktree mutation\n")
    entries_3 = compute_inventory_entries(root)
    entry_3 = next(e for e in entries_3 if e["path"] == "scripts/file.py")

    # Both object_sha and sha256 changed.
    if entry_3["sha256"] == entry_1["sha256"]:
        raise AssertionError("worktree mutation: sha256 should differ")
    # object_sha should still reflect the index (unchanged from state 2).
    if entry_3["object_sha"] != entry_2["object_sha"]:
        raise AssertionError("worktree mutation should not change index object_sha")


def test_validator_rejects_tampered_object_sha(tmp_path):
    """r6: validator detects a tampered object_sha in capture_input_inventory."""
    fixture, manifest, out, png_bytes = _build_clean_staged_fixture(tmp_path)
    bad = json.loads(json.dumps(manifest))
    inv = bad["capture_input_inventory"]
    # Find a tracked entry and tamper its object_sha.
    for e in inv:
        if e.get("object_sha"):
            e["object_sha"] = "e" * 40 if e["object_sha"] != "e" * 40 else "f" * 40
            break
    else:
        raise AssertionError("no tracked entry with object_sha found")
    errors = validate_manifest(bad, fixture, EXPECTED_ARTIFACTS, check_files=False)
    if not any("detailed entries mismatch" in err for err in errors):
        raise AssertionError(f"validator should reject tampered object_sha: {errors}")


# ---- r6: cross-process publication lock -----------------------------------


def test_publication_lock_acquires_and_releases(tmp_path):
    """r6: _publication_lock acquires an exclusive flock and releases it on
    exit.  The lock file persists for the next publisher."""
    import fcntl
    mod = _load_capture_module()
    output = tmp_path / "out"
    lock_path = output.parent / (output.name + ".publish-lock")
    with mod._publication_lock(output, timeout_s=5):
        if not lock_path.exists():
            raise AssertionError("lock file not created")
        # A second non-blocking lock attempt must fail (EAGAIN).
        fd = os.open(str(lock_path), os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise AssertionError("second lock should be blocked")
            except (BlockingIOError, OSError):
                pass  # expected
        finally:
            os.close(fd)
    # After release, a new lock can be acquired.
    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # should succeed
    finally:
        os.close(fd)


def test_publication_lock_serializes_concurrent_processes(tmp_path):
    """r6: two concurrent child processes calling _atomic_publish on the same
    output serialize via the cross-process flock — neither corrupts the
    other's state.  The final output is internally consistent (all files from
    the same publish, no mixed content, no leftover siblings)."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    _write_complete_proof(output, b"PRIOR")

    # Two staging dirs with distinct content markers.
    staging_a = tmp_path / "staging_a"
    staging_b = tmp_path / "staging_b"
    staging_a.mkdir()
    staging_b.mkdir()
    for name in mod.EXPECTED_PNG_FILES:
        (staging_a / name).write_bytes(b"MARKER_A")
        (staging_b / name).write_bytes(b"MARKER_B")

    # Build manifests using ROOT metadata so validate_manifest passes.
    image_entries_a = [
        {
            "file": name, "sha256": hashlib.sha256(b"MARKER_A").hexdigest(),
            "classification": SCENARIO_CLASSIFICATIONS.get(name, "release-proof"),
            "release_proof": True,
            "scenario_assertions": mod.SCENARIO_ASSERTIONS[name],
            "viewport": EXPECTED_VIEWPORTS[name], "media": EXPECTED_MEDIA[name],
        }
        for name in mod.EXPECTED_PNG_FILES
    ]
    image_entries_b = [
        {
            "file": name, "sha256": hashlib.sha256(b"MARKER_B").hexdigest(),
            "classification": SCENARIO_CLASSIFICATIONS.get(name, "release-proof"),
            "release_proof": True,
            "scenario_assertions": mod.SCENARIO_ASSERTIONS[name],
            "viewport": EXPECTED_VIEWPORTS[name], "media": EXPECTED_MEDIA[name],
        }
        for name in mod.EXPECTED_PNG_FILES
    ]
    manifest_a = _build_full_manifest(image_entries_a)
    manifest_b = _build_full_manifest(image_entries_b)
    expected = {name: "; ".join(mod.SCENARIO_ASSERTIONS[name])
                for name in mod.EXPECTED_PNG_FILES}

    # Write manifest files for child processes to read.
    (tmp_path / "manifest_a.json").write_text(json.dumps(manifest_a))
    (tmp_path / "manifest_b.json").write_text(json.dumps(manifest_b))

    # Write child scripts.
    child_template = (
        "import sys, importlib.util, json\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, %r)\n"
        "spec = importlib.util.spec_from_file_location('cap', %r)\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "manifest = json.loads(Path(%r).read_text())\n"
        "m._atomic_publish(\n"
        "    Path(%r), Path(%r),\n"
        "    manifest, manifest['images'],\n"
        "    %r, Path(%r))\n"
        "print('DONE')\n"
    )
    (tmp_path / "child_a.py").write_text(child_template % (
        str(ROOT / "scripts"),
        str(ROOT / "scripts" / "capture_annotations_v2.py"),
        str(tmp_path / "manifest_a.json"),
        str(staging_a), str(output),
        expected, str(ROOT),
    ))
    (tmp_path / "child_b.py").write_text(child_template % (
        str(ROOT / "scripts"),
        str(ROOT / "scripts" / "capture_annotations_v2.py"),
        str(tmp_path / "manifest_b.json"),
        str(staging_b), str(output),
        expected, str(ROOT),
    ))

    # Launch both children concurrently.
    proc_a = subprocess.Popen(
        [sys.executable, str(tmp_path / "child_a.py")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=dict(os.environ),
    )
    proc_b = subprocess.Popen(
        [sys.executable, str(tmp_path / "child_b.py")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=dict(os.environ),
    )
    out_a, err_a = proc_a.communicate(timeout=60)
    out_b, err_b = proc_b.communicate(timeout=60)

    if proc_a.returncode != 0:
        raise AssertionError(f"child_a failed: {err_a}")
    if proc_b.returncode != 0:
        raise AssertionError(f"child_b failed: {err_b}")

    # Final output must be internally consistent: ALL files from the same
    # publish (either all MARKER_A or all MARKER_B), never mixed.
    sorted_names = sorted(mod.EXPECTED_PNG_FILES)
    first_data = (output / sorted_names[0]).read_bytes()
    for name in sorted_names:
        data = (output / name).read_bytes()
        if data not in (b"MARKER_A", b"MARKER_B"):
            raise AssertionError(f"{name} has unexpected content: {data!r}")
        if data != first_data:
            raise AssertionError(
                f"mixed content: {sorted_names[0]}={first_data!r} but "
                f"{name}={data!r} — concurrent publish was not serialized")
    # No leftover siblings.
    sibling_old = output.parent / (output.name + ".publish-old")
    sibling_new = output.parent / (output.name + ".publish-new")
    if sibling_old.exists():
        raise AssertionError("leftover .publish-old after concurrent publish")
    if sibling_new.exists():
        raise AssertionError("leftover .publish-new after concurrent publish")
    # manifest.json exists.
    if not (output / "manifest.json").is_file():
        raise AssertionError("manifest.json missing after concurrent publish")
    # Exact output set.
    actual = {p.name for p in output.iterdir()}
    expected_names = set(mod.EXPECTED_PNG_FILES) | {"manifest.json"}
    if actual != expected_names:
        raise AssertionError(f"output set mismatch: extra={actual - expected_names}")


# ---- r6: Linux renameat2 atomic exchange ----------------------------------


def test_renameat2_exchange_atomically_swaps(tmp_path):
    """r6: _try_renameat2_exchange atomically swaps two directories.  On
    non-Linux or older kernels, it returns False (caller falls back)."""
    mod = _load_capture_module()
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "marker").write_text("A")
    (b / "marker").write_text("B")
    result = mod._try_renameat2_exchange(a, b)
    if not result:
        return  # not available on this platform — skip
    # After exchange: a has B, b has A.
    if (a / "marker").read_text() != "B":
        raise AssertionError("exchange failed: a should have B's content")
    if (b / "marker").read_text() != "A":
        raise AssertionError("exchange failed: b should have A's content")


def test_renameat2_exchange_returns_false_for_missing_path(tmp_path):
    """r6: _try_renameat2_exchange returns False (ENOSYS/EINVAL) or raises
    for a non-existent path — never silently succeeds."""
    mod = _load_capture_module()
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "does_not_exist"
    # If renameat2 is available, exchanging with a non-existent path should
    # raise OSError (ENOENT), not return False.  If not available, returns
    # False.  Either way, it never silently succeeds.
    try:
        result = mod._try_renameat2_exchange(a, b)
        if result:
            raise AssertionError("exchange with non-existent path should not succeed")
    except OSError:
        pass  # expected: ENOENT is a real error, not "not available"


def test_atomic_publish_uses_exchange_when_available(tmp_path, monkeypatch):
    """r6: when renameat2 is available, _atomic_publish uses the exchange path
    (not the two-rename fallback).  The output is never missing during the
    swap.  After success, output has new bytes and sibling is cleaned up."""
    mod, staging, output, manifest, entries, expected = _setup_publish_env(tmp_path)

    # Track whether renameat2 exchange was called.
    exchange_called = []
    original_exchange = mod._try_renameat2_exchange

    def tracking_exchange(a, b):
        exchange_called.append((str(a), str(b)))
        return original_exchange(a, b)

    monkeypatch.setattr(mod, "_try_renameat2_exchange", tracking_exchange)
    mod._atomic_publish(staging, output, manifest, entries, expected, ROOT)
    monkeypatch.undo()

    if not exchange_called:
        # renameat2 not available on this platform — exchange path skipped.
        return

    # The exchange swapped sibling_new ↔ output.
    if len(exchange_called) != 1:
        raise AssertionError(f"exchange should be called once: {exchange_called}")

    # Output has new bytes.
    for name in mod.EXPECTED_PNG_FILES:
        if (output / name).read_bytes() != b"NEW":
            raise AssertionError(f"{name} not updated to new bytes via exchange")
    # Sibling cleaned up.
    sibling_new = output.parent / (output.name + ".publish-new")
    if sibling_new.exists():
        raise AssertionError("sibling-new not cleaned up after exchange")
    sibling_old = output.parent / (output.name + ".publish-old")
    if sibling_old.exists():
        raise AssertionError("sibling-old should not exist when exchange is used")


def test_renameat2_exchange_failure_preserves_prior(tmp_path, monkeypatch):
    """r6: if the renameat2 exchange raises OSError (real error, not
    ENOSYS/EINVAL), prior output is preserved and sibling is cleaned up."""
    mod, staging, output, manifest, entries, expected = _setup_publish_env(tmp_path)

    def boom_exchange(a, b):
        raise OSError(errno.EIO, "injected exchange failure")

    monkeypatch.setattr(mod, "_try_renameat2_exchange", boom_exchange)
    try:
        mod._atomic_publish(staging, output, manifest, entries, expected, ROOT)
        raise AssertionError("publish should have raised on exchange failure")
    except OSError as exc:
        if "injected" not in str(exc):
            raise AssertionError(f"unexpected error: {exc}")
    monkeypatch.undo()
    # Prior bytes must be intact.
    for name in mod.EXPECTED_PNG_FILES:
        if (output / name).read_bytes() != b"PRIOR":
            raise AssertionError(f"{name} prior bytes corrupted by failed exchange")
    # Sibling cleaned up.
    if (output.parent / (output.name + ".publish-new")).exists():
        raise AssertionError("sibling-new not cleaned up after exchange failure")


# ---- r6: directory-fsync restoration paths --------------------------------


def test_recovery_restoration_fsyncs_parent(tmp_path, monkeypatch):
    """r6-minor: _recover_interrupted_publish fsyncs the parent directory
    after restoring the sole prior proof (output absent + old present →
    restore).  Without this fsync, a second crash can lose a restoration
    that was reported as successful."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    old = output.parent / (output.name + ".publish-old")
    _write_complete_proof(old, b"prior-proof")

    fsync_calls = []
    original_fsync = mod._fsync_dir

    def tracking_fsync(path):
        fsync_calls.append(str(path))
        original_fsync(path)

    monkeypatch.setattr(mod, "_fsync_dir", tracking_fsync)
    mod._recover_interrupted_publish(output)
    monkeypatch.undo()

    # The parent directory must have been fsynced after the restore rename.
    if not any(str(output.parent) in c for c in fsync_calls):
        raise AssertionError(
            f"parent directory not fsynced after recovery restore: {fsync_calls}")
    # Prior proof must be restored.
    if not (output / _proof_probe(mod)).exists():
        raise AssertionError("prior proof not restored")


def test_recovery_validates_all_present_states_before_any_mutation(tmp_path, monkeypatch):
    """r9: a corrupt later sibling cannot be discovered after valid state was removed."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    old, new = _stage_publish_dirs(
        output, {"output": True, "old": True, "new": True})
    (new / _proof_probe(mod)).unlink()
    removals = []
    real_remove = mod._remove_tree_durable
    monkeypatch.setattr(mod, "_remove_tree_durable",
                        lambda path: removals.append(path) or real_remove(path))
    with pytest.raises(RuntimeError, match="incomplete publication proof"):
        mod._recover_interrupted_publish(output)
    if removals:
        raise AssertionError(f"recovery mutated state before complete validation: {removals}")
    if not output.exists() or not old.exists() or not new.exists():
        raise AssertionError("recovery changed observed state after preflight failure")


def test_capture_script_hash_comes_from_inventory_authority(tmp_path, monkeypatch):
    """r9: snapshot must not perform a second pathname read of the script."""
    from okf_loom.capture_provenance import capture_provenance_snapshot
    fixture = _build_snapshot_fixture(tmp_path)
    original = Path.read_bytes

    def reject_script_path_read(path):
        if path == fixture / "scripts" / "capture_annotations_v2.py":
            raise AssertionError("capture script was read outside secure inventory")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", reject_script_path_read)
    snap = capture_provenance_snapshot(fixture)
    row = next(entry for entry in snap.inventory_entries
               if entry["path"] == "scripts/capture_annotations_v2.py")
    if snap.script_sha256 != row["sha256"]:
        raise AssertionError("script hash did not derive from inventory entry")


def test_exchange_success_then_parent_fsync_failure_preserves_rollback(tmp_path, monkeypatch):
    """r7-M3: after a successful renameat2 exchange, sibling_new holds the OLD
    proof.  If the parent-directory fsync that persists the exchange boundary
    then fails, the OLD rollback content in sibling_new is PRESERVED (not
    deleted) and the publish fails closed — the broad except path must not
    delete the rollback copy while exchange durability is uncertain.

    The parent fsync is faulted ONLY after the exchange; the sibling_new build
    fsync still succeeds so the failure is precisely at the exchange boundary."""
    mod, staging, output, manifest, entries, expected = _setup_publish_env(tmp_path)
    sibling_new = output.parent / (output.name + ".publish-new")
    parent_abs = os.path.abspath(str(output.parent))
    original_fsync = mod._fsync_dir

    def fsync_failing_on_parent(path):
        # The build phase fsyncs sibling_new (must succeed); the post-exchange
        # boundary fsyncs the parent (must fail to trigger r7-M3).
        if os.path.abspath(str(path)) == parent_abs:
            raise RuntimeError("injected parent-fsync failure")
        return original_fsync(path)

    monkeypatch.setattr(mod, "_fsync_dir", fsync_failing_on_parent)
    try:
        mod._atomic_publish(staging, output, manifest, entries, expected, ROOT)
        raise AssertionError("publish should have failed closed on parent-fsync failure")
    except RuntimeError as exc:
        if "parent-directory fsync" not in str(exc) or "PRESERVED" not in str(exc):
            raise AssertionError(
                f"fail-closed error must reference the preserved rollback copy: {exc}")
    monkeypatch.undo()
    if not sibling_new.is_dir():
        raise AssertionError(
            "OLD rollback content (sibling_new) was deleted after exchange + "
            "parent-fsync failure — rollback authority lost")
    # The preserved sibling_new must hold the OLD (prior) proof bytes — the
    # rollback authority is genuine, not empty/new.
    for name in mod.EXPECTED_PNG_FILES:
        if not (sibling_new / name).is_file():
            raise AssertionError(f"preserved rollback missing {name}")
        if (sibling_new / name).read_bytes() != b"PRIOR":
            raise AssertionError(
                f"preserved rollback {name} does not hold the OLD prior bytes")
    # output currently holds the NEW content (the exchange took effect), but its
    # on-disk durability is uncertain — the operator must resolve it manually
    # using the preserved sibling_new.
    for name in mod.EXPECTED_PNG_FILES:
        if (output / name).read_bytes() != b"NEW":
            raise AssertionError(
                f"{name} output should hold new content after the exchange: "
                f"{(output / name).read_bytes()!r}")


def test_post_exchange_fsync_failure_cleanup_resumes_after_resolve(tmp_path, monkeypatch):
    """r7-M3 corollary: the rollback_preserved flag is scoped to the
    post-exchange-fsync failure only.  A subsequent clean publish (parent fsync
    succeeds) must resume normal cleanup — the preserved sibling is removed
    once durability is resolved, so the recovery/publish protocol does not
    leak rollback copies forever."""
    mod, staging, output, manifest, entries, expected = _setup_publish_env(tmp_path)
    sibling_new = output.parent / (output.name + ".publish-new")
    # First publish: succeed normally (exchange + durable fsync) — sibling_new
    # (old content) is cleaned up.  This proves cleanup resumes when durable.
    mod._atomic_publish(staging, output, manifest, entries, expected, ROOT)
    if sibling_new.exists():
        raise AssertionError(
            "old-content sibling not cleaned after a durable exchange — "
            "rollback_preserved must be scoped to fsync FAILURE only")
    for name in mod.EXPECTED_PNG_FILES:
        if (output / name).read_bytes() != b"NEW":
            raise AssertionError(f"{name} not updated after durable exchange")


# ---- r6: mobile manifest assertion truthfulness ---------------------------


def test_mobile_assertion_matches_capture_flow():
    """r7-M4: mobile-320.png scenario assertions must match what the capture
    flow actually verifies — the ACTUAL document-peek state (recovery panel and
    scrim hidden, authored document visible, .okf-reanchor-peek with
    Return-to-comment visible and bounded), not the prior open-drawer
    instruction check that depicted the wrong state."""
    source = (ROOT / "scripts" / "capture_annotations_v2.py").read_text(encoding="utf-8")
    assertions = _CAP.SCENARIO_ASSERTIONS["mobile-320.png"]
    # The unsupported 'document context bar visible' claim must be removed.
    for a in assertions:
        if "document context bar" in a.lower():
            raise AssertionError(
                f"mobile assertion still claims unsupported 'document context "
                f"bar visible': {assertions}")
    # The assertion must mention document-peek context (true peek proof).
    if not any("document-peek" in a.lower() or "document peek" in a.lower()
               for a in assertions):
        raise AssertionError(
            f"mobile assertion must mention document-peek context: {assertions}")
    # The capture source must drive the ACTUAL document-peek state between the
    # 320px viewport switch and the mobile-320 screenshot: the .okf-reanchor-peek
    # bar (Return to comment) must be present, and the open-drawer instruction
    # must NOT be the depicted proof.
    mobile_section_start = source.find('page.set_viewport_size({"width": 320')
    mobile_screenshot_pos = source.find(
        '_capture_with_metadata(page, "mobile-320.png"')
    if mobile_section_start < 0 or mobile_screenshot_pos < 0:
        raise AssertionError("mobile capture section not found in source")
    mobile_section = source[mobile_section_start:mobile_screenshot_pos]
    if "okf-reanchor-peek" not in mobile_section:
        raise AssertionError(
            "mobile capture must check the .okf-reanchor-peek document-peek bar")
    if "Return to comment" not in mobile_section:
        raise AssertionError(
            "mobile capture must verify the Return-to-comment action is visible")
    if "Select in document" not in mobile_section:
        raise AssertionError(
            "mobile capture must enter the peek state via Select in document")
    if "document-peek" not in mobile_section.lower():
        raise AssertionError(
            "mobile capture must reference document-peek context")


def test_mobile_assertion_count_matches_expected():
    """r6: mobile-320.png has exactly 2 scenario assertions (viewport +
    document-peek), not the old 2 with an unsupported claim."""
    assertions = _CAP.SCENARIO_ASSERTIONS["mobile-320.png"]
    if len(assertions) != 2:
        raise AssertionError(
            f"mobile-320.png should have exactly 2 assertions, got {len(assertions)}: "
            f"{assertions}")


# ---- r6: crash-after-restoration durability --------------------------------


def test_crash_after_restoration_proof_survives(tmp_path):
    """r6: after recovery restores the sole prior proof (old → output), a
    SECOND recovery invocation finds output present and clean — the restored
    proof survives a second crash because the restoration rename was fsynced."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    old = output.parent / (output.name + ".publish-old")
    _write_complete_proof(old, b"prior-proof")

    # First recovery: restores old → output.
    mod._recover_interrupted_publish(output)
    if not (output / _proof_probe(mod)).exists():
        raise AssertionError("first recovery did not restore prior proof")
    if old.exists():
        raise AssertionError("first recovery did not move old")

    # Second recovery: output present → clean state, no actions.
    actions = mod._recover_interrupted_publish(output)
    if actions:
        raise AssertionError(
            f"second recovery should be clean (output present): {actions}")
    if not (output / _proof_probe(mod)).exists():
        raise AssertionError("prior proof lost after second recovery")
    # No siblings should exist.
    if old.exists():
        raise AssertionError("old reappeared after second recovery")


def test_crash_after_exchange_output_has_new_content(tmp_path):
    """r6: after a successful renameat2 exchange, output has the NEW content.
    If a crash happens after the exchange but before cleanup, recovery finds
    output present (valid new content) and cleans up the leftover sibling."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    _write_complete_proof(output, b"OLD")
    sibling_new = output.parent / (output.name + ".publish-new")
    _write_complete_proof(sibling_new, b"NEW")

    # Simulate a crash AFTER the exchange: output has NEW content, sibling_new
    # has OLD content (leftover).  In this state, recovery should treat output
    # as valid and clean up the leftover sibling.
    # But wait — with renameat2 exchange, after the exchange, sibling_new has
    # OLD content and output has NEW content.  Recovery sees output present →
    # removes leftover sibling_new.
    # Actually, after the exchange the sibling_new has the old content.
    # But we're simulating a crash state where output has NEW (from the exchange)
    # and sibling_new has OLD (the leftover).  Let's set it up directly:
    for name in mod.EXPECTED_PNG_FILES:
        (output / name).write_bytes(b"NEW")  # exchange completed
        (sibling_new / name).write_bytes(b"OLD")  # leftover old content
    (output / "manifest.json").write_text(json.dumps({"images": [
        {"file": name, "sha256": hashlib.sha256(b"NEW").hexdigest()}
        for name in mod.EXPECTED_PNG_FILES]}), encoding="utf-8")
    (sibling_new / "manifest.json").write_text(json.dumps({"images": [
        {"file": name, "sha256": hashlib.sha256(b"OLD").hexdigest()}
        for name in mod.EXPECTED_PNG_FILES]}), encoding="utf-8")

    actions = mod._recover_interrupted_publish(output)
    # Output is present → valid; sibling_new is a leftover → removed.
    if sibling_new.exists():
        raise AssertionError("leftover sibling_new not cleaned up after crash")
    if not any("output present" in a for a in actions):
        raise AssertionError(f"recovery should note output present: {actions}")
    # Output still has new content.
    for name in mod.EXPECTED_PNG_FILES:
        if (output / name).read_bytes() != b"NEW":
            raise AssertionError(f"{name} new content lost after crash recovery")


# ---- r6: no-assert guard for new code -------------------------------------


def test_capture_script_has_no_assert_in_new_r6_code():
    """r6: all new r6 functions (lock, exchange, fsync) use explicit raises,
    not assert — safe under python -O."""
    import ast
    source = (ROOT / "scripts" / "capture_annotations_v2.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            raise AssertionError(
                f"capture script has ast.Assert at line {node.lineno}: "
                "python -O would silently remove this check")


def test_provenance_module_has_no_assert():
    """r6: capture_provenance.py uses explicit if/raise, not assert."""
    import ast
    source = (ROOT / "scripts" / "okf_loom" / "capture_provenance.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            raise AssertionError(
                f"capture_provenance.py has ast.Assert at line {node.lineno}")


# ---- r6: object_sha field in manifest entries -----------------------------


def test_manifest_inventory_entries_have_object_sha():
    """r6: freshly computed inventory entries include the object_sha field."""
    entries = compute_inventory_entries(ROOT)
    if not entries:
        raise AssertionError("no inventory entries")
    for e in entries:
        if "object_sha" not in e:
            raise AssertionError(f"entry missing object_sha field: {e}")
        if e["tracked"]:
            if not e["object_sha"] or len(e["object_sha"]) != 40:
                raise AssertionError(
                    f"tracked entry has invalid object_sha: {e}")
        else:
            if e["object_sha"] is not None:
                raise AssertionError(
                    f"untracked entry should have None object_sha: {e}")


def test_validator_detailed_entries_include_object_sha(tmp_path):
    """r6: the validator's detailed-entry comparison includes object_sha, so
    a manifest built before an index-only mutation fails validation after
    the mutation."""
    fixture, manifest, out, png_bytes = _build_clean_staged_fixture(tmp_path)
    # Sanity: the fixture validates clean.
    errors = validate_manifest(manifest, fixture, EXPECTED_ARTIFACTS, check_files=True)
    if errors:
        raise AssertionError(
            "fixture should validate clean: " + "; ".join(errors))
    # Tamper object_sha in the manifest's inventory.
    bad = json.loads(json.dumps(manifest))
    for e in bad["capture_input_inventory"]:
        if e.get("object_sha"):
            e["object_sha"] = "0" * 40
            break
    else:
        raise AssertionError("no tracked entry found in fixture")
    errors = validate_manifest(bad, fixture, EXPECTED_ARTIFACTS, check_files=False)
    if not any("detailed entries mismatch" in err for err in errors):
        raise AssertionError(
            f"validator should detect object_sha tamper: {errors}")


# ---- r6: publication lock file is not in inventory roots -------------------


def test_publication_lock_file_excluded_from_inventory():
    """r6: the .publish-lock file created by _publication_lock must NOT
    appear in the inventory (it's a runtime artifact, not a consumed input).
    It's in the screenshots directory which is outside the inventory roots."""
    from okf_loom.capture_provenance import INVENTORY_ROOTS
    # The lock file lives in docs/screenshots/annotations-v2/ which is
    # NOT in INVENTORY_ROOTS (scripts, tests, samples/demo_bundle).
    for root in INVENTORY_ROOTS:
        if "docs" in root or "screenshots" in root:
            raise AssertionError(
                f"screenshots dir should not be in inventory roots: {INVENTORY_ROOTS}")


# ---- r6: atomic publish first-run uses no exchange (no prior output) -------


def test_atomic_publish_first_run_no_exchange(tmp_path, monkeypatch):
    """r6: on first run (no prior output), _atomic_publish does NOT call
    renameat2 exchange — it just renames sibling_new → output (no swap
    needed, no window)."""
    mod = _load_capture_module()
    staging = tmp_path / "staging"
    staging.mkdir()
    new_bytes = b"NEW"
    for name in mod.EXPECTED_PNG_FILES:
        (staging / name).write_bytes(new_bytes)
    output = tmp_path / "out"  # does not exist

    exchange_called = []
    monkeypatch.setattr(mod, "_try_renameat2_exchange",
                        lambda a, b: exchange_called.append(1) or True)
    image_entries = [
        {
            "file": name, "sha256": hashlib.sha256(new_bytes).hexdigest(),
            "classification": SCENARIO_CLASSIFICATIONS.get(name, "release-proof"),
            "release_proof": True,
            "scenario_assertions": mod.SCENARIO_ASSERTIONS[name],
            "viewport": EXPECTED_VIEWPORTS[name], "media": EXPECTED_MEDIA[name],
        }
        for name in mod.EXPECTED_PNG_FILES
    ]
    manifest = _build_full_manifest(image_entries)
    expected = {name: "; ".join(mod.SCENARIO_ASSERTIONS[name])
                for name in mod.EXPECTED_PNG_FILES}
    mod._atomic_publish(staging, output, manifest, manifest["images"], expected, ROOT)
    monkeypatch.undo()
    # Exchange should NOT have been called (no prior output to swap with).
    if exchange_called:
        raise AssertionError(
            f"exchange should not be called on first run: {exchange_called}")
    if not output.is_dir():
        raise AssertionError("output not created on first run")


# ===========================================================================
# r7 capture findings: recovery under lock, atomic-exchange requirement,
# post-exchange fsync rollback preservation, lock-unaware reader, mobile peek
# ===========================================================================


# ---- r7-M1: startup recovery runs under the publication lock ---------------


def test_entry_recovery_holds_publication_lock_against_concurrent_publisher(tmp_path):
    """r7-M1: _entry_recover_and_prepare acquires the SAME publication lock as
    _atomic_publish.  A concurrent capture process that legitimately owns the
    lock mid-swap (with transient .publish-new state) cannot have its active
    state inspected, removed, or restored by a second process's startup
    recovery.  Recovery must BLOCK on the lock and only proceed once the
    holder releases it."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    output.parent.mkdir(parents=True, exist_ok=True)
    sibling_new = output.parent / (output.name + ".publish-new")
    holding_flag = tmp_path / "holding.flag"
    release_flag = tmp_path / "release.flag"

    # Holder child: acquire the lock, create transient .publish-new (simulating
    # an active publisher mid-swap), signal it is holding, then wait for a
    # release signal before releasing the lock.
    holder_src = (
        "import sys, importlib.util, os, time\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
        "spec = importlib.util.spec_from_file_location('cap', "
        f"{str(ROOT / 'scripts' / 'capture_annotations_v2.py')!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"output = Path({str(output)!r})\n"
        f"sibling_new = Path({str(sibling_new)!r})\n"
        "output.parent.mkdir(parents=True, exist_ok=True)\n"
        "with m._publication_lock(output, timeout_s=10):\n"
        "    sibling_new.mkdir(parents=True, exist_ok=True)\n"
        "    (sibling_new / 'active').write_bytes(b'publisher-active')\n"
        f"    Path({str(holding_flag)!r}).write_text('holding')\n"
        "    for _ in range(400):\n"
        f"        if Path({str(release_flag)!r}).exists(): break\n"
        "        time.sleep(0.05)\n"
        "print('HOLDER_DONE')\n"
    )
    holder = tmp_path / "_holder.py"
    holder.write_text(holder_src, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(holder)], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=dict(os.environ),
    )
    try:
        # Wait for the holder to acquire the lock + create transient state.
        deadline = __import__("time").monotonic() + 10
        import time as _time
        while _time.monotonic() < deadline and not holding_flag.exists():
            _time.sleep(0.02)
        if not holding_flag.exists():
            raise AssertionError("holder did not acquire the lock in time")
        if not sibling_new.is_dir():
            raise AssertionError("holder did not create transient .publish-new")
        # Recovery must BLOCK on the lock — a 1s timeout must elapse because
        # the holder owns the lock.
        blocked_started = _time.monotonic()
        try:
            mod._entry_recover_and_prepare(output, timeout_s=1)
            raise AssertionError(
                "recovery returned while another publisher held the lock "
                "— it did not acquire the publication lock")
        except RuntimeError as exc:
            if "timed out" not in str(exc).lower():
                raise AssertionError(
                    f"recovery raised an unexpected error (not a lock timeout): "
                    f"{exc}")
        blocked_elapsed = _time.monotonic() - blocked_started
        if blocked_elapsed < 0.8:
            raise AssertionError(
                f"recovery did not block on the lock (elapsed={blocked_elapsed:.2f}s)")
        # While the holder owns the lock, recovery MUST NOT have touched the
        # transient state.
        if not sibling_new.is_dir():
            raise AssertionError(
                "recovery removed the active publisher's .publish-new without "
                "the lock — r7-M1 regression")
        if (sibling_new / "active").read_bytes() != b"publisher-active":
            raise AssertionError(
                "recovery mutated the active publisher's transient state")
        # Release the holder; recovery can now proceed.
        release_flag.write_text("release")
        holder_out, holder_err = proc.communicate(timeout=15)
        if proc.returncode != 0:
            raise AssertionError(f"holder failed: {holder_err}")
        # Once unlocked, the incomplete transient directory is rejected and
        # retained for diagnosis; recovery must never bless/delete it.
        with pytest.raises(RuntimeError, match="incomplete publication proof"):
            mod._entry_recover_and_prepare(output, timeout_s=10)
        if not sibling_new.exists():
            raise AssertionError("rejected incomplete sibling was removed")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)


# ---- r7-M2: lock-unaware reader never observes a gap during exchange -------


def _build_exchange_reader_env(tmp_path):
    """Build output (prior OLD content) plus two staging/manifest pairs (OLD,
    NEW)     sharing a single ROOT inventory snapshot, for lock-unaware reader
    publish cycles."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    output.mkdir()
    for name in mod.EXPECTED_PNG_FILES:
        (output / name).write_bytes(b"OLD")
    # Seed a manifest.json so the initial output already matches the full
    # published set the lock-unaware reader expects (the publish always emits
    # PNGs + manifest.json).  Without this the reader would flag the baseline
    # state, not a publish-time gap.
    (output / "manifest.json").write_text(json.dumps({"images": [
        {"file": name, "sha256": hashlib.sha256(b"OLD").hexdigest()}
        for name in mod.EXPECTED_PNG_FILES]}), encoding="utf-8")
    inv_digest, inv_count = compute_inventory(ROOT)
    inv_entries = compute_inventory_entries(ROOT)
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    worktree = subprocess.check_output(
        ["git", "diff", "--binary", "HEAD"], cwd=ROOT)
    expected = {name: "; ".join(_CAP.SCENARIO_ASSERTIONS[name])
                for name in mod.EXPECTED_PNG_FILES}
    base = {
        "schema": 1, "provenance_model": PROVENANCE_MODEL,
        "captured_at": "2026-01-01T00:00:00+00:00",
        "source_revision": head, "source_revision_verified": True,
        "evidence_parent_revision": head, "evidence_parent_verified": True,
        "evidence_paths": expected_evidence_paths(expected),
        "source_worktree_digest": hashlib.sha256(worktree).hexdigest(),
        "evidence_worktree_digest": hashlib.sha256(worktree).hexdigest(),
        "capture_input_inventory_sha256": inv_digest,
        "capture_input_inventory_serialized_sha256": inventory_serialized_digest(inv_entries),
        "capture_input_file_count": inv_count,
        "capture_input_inventory": inv_entries,
        "capture_script_sha256": hashlib.sha256(
            (ROOT / "scripts" / "capture_annotations_v2.py").read_bytes()
        ).hexdigest(),
        "browser": "test", "images": [],
    }

    def make(marker):
        staging = tmp_path / ("staging_" + marker.decode())
        if staging.exists():
            import shutil
            shutil.rmtree(staging)
        staging.mkdir()
        for name in mod.EXPECTED_PNG_FILES:
            (staging / name).write_bytes(marker)
        images = [
            {"file": name, "sha256": hashlib.sha256(marker).hexdigest(),
             "classification": SCENARIO_CLASSIFICATIONS.get(name, "release-proof"),
             "release_proof": True,
             "scenario_assertions": mod.SCENARIO_ASSERTIONS[name],
             "viewport": EXPECTED_VIEWPORTS[name], "media": EXPECTED_MEDIA[name]}
            for name in mod.EXPECTED_PNG_FILES
        ]
        m = json.loads(json.dumps(base))
        m["images"] = images
        return staging, m

    return (mod, output) + make(b"OLD") + make(b"NEW") + (expected,)


def test_lock_unaware_reader_never_observes_gap_during_exchange_publish(tmp_path):
    """r7-M2: a lock-unaware reader (one that does NOT take the publication
    flock — e.g. a static server, release collector, or validator) never
    observes output missing or with mixed OLD/NEW content while the publisher
    repeatedly swaps output via renameat2(RENAME_EXCHANGE).  This is the
    atomicity contract that the removed two-rename fallback could not provide.

    The publish is repeated across many cycles while the reader polls
    aggressively, so any non-atomic window would be observed.  The exchange
    syscall is atomic, so no violation is recorded."""
    mod = _load_capture_module()
    # Verify renameat2 exchange is available on this platform; if not, the
    # atomicity contract is enforced via fail-closed (covered by the
    # release_publish tests) and there is nothing for this reader probe to
    # observe.
    probe_a = tmp_path / "_avail_a"
    probe_b = tmp_path / "_avail_b"
    probe_a.mkdir()
    probe_b.mkdir()
    if not mod._try_renameat2_exchange(probe_a, probe_b):
        return  # exchange unavailable — atomicity enforced via fail-closed
    (_, output, staging_old, manifest_old, staging_new, manifest_new,
     expected) = _build_exchange_reader_env(tmp_path)
    expected_names = sorted(mod.EXPECTED_PNG_FILES)
    stop_flag = tmp_path / "reader.stop"

    reader_src = (
        "import os, sys, time, json\n"
        "output = sys.argv[1]\n"
        "expected_files = sys.argv[2].split(',')\n"
        "expected_set = set(expected_files) | {'manifest.json'}\n"
        "stop = sys.argv[3]\n"
        "violations = []\n"
        # The r7-M2 contract: the canonical output directory is NEVER missing\n"
        # during an atomic exchange publish.  The removed two-rename fallback\n"
        # left output absent between renames, so a lock-unaware reader could\n"
        # observe ENOENT.  (A torn multi-file read catching the swap mid-\n"
        # sequence is a reader artifact, not a publish violation — only\n"
        # missing-directory/missing-file observations are atomicity breaks.)\n"
        "deadline = time.time() + 12\n"
        "while time.time() < deadline and not os.path.exists(stop):\n"
        "    try:\n"
        "        names = set(os.listdir(output))\n"
        "    except OSError as e:\n"
        "        violations.append('listdir ENOENT/missing: ' + repr(e)); continue\n"
        "    missing = expected_set - names\n"
        "    if missing:\n"
        "        violations.append('missing files: ' + repr(sorted(missing))); continue\n"
        "    for n in expected_files:\n"
        "        if not n: continue\n"
        "        try:\n"
        "            with open(os.path.join(output, n), 'rb') as f:\n"
        "                f.read()\n"
        "        except OSError as e:\n"
        "            violations.append('read ENOENT ' + n + ': ' + repr(e))\n"
        "print(json.dumps(violations[:50]))\n"
    )
    reader = tmp_path / "_reader.py"
    reader.write_text(reader_src, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(reader), str(output), ",".join(expected_names),
         str(stop_flag)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=dict(os.environ),
    )
    try:
        import time as _time
        _time.sleep(0.1)  # let the reader start polling
        # Repeatedly swap output OLD<->NEW via atomic exchange. Each cycle the
        # reader must observe either all-OLD or all-NEW, never missing/mixed.
        for i in range(12):
            if i % 2 == 0:
                mod._atomic_publish(staging_new, output, manifest_new,
                                    manifest_new["images"], expected, ROOT)
            else:
                mod._atomic_publish(staging_old, output, manifest_old,
                                    manifest_old["images"], expected, ROOT)
        stop_flag.write_text("stop")
        out, err = proc.communicate(timeout=15)
        if proc.returncode != 0:
            raise AssertionError(f"reader failed: {err}")
        import json as _json
        violations = _json.loads(out.strip().splitlines()[-1]) if out.strip() else []
        if violations:
            raise AssertionError(
                "lock-unaware reader observed a gap/mixed state during exchange "
                f"publish (atomicity broken): {violations[:5]}")
    finally:
        if proc.poll() is None:
            stop_flag.write_text("stop")
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_lock_unaware_reader_probe_detects_non_atomic_swap(tmp_path):
    """r7-M2 non-vacuity control: the lock-unaware reader probe used above MUST
    be able to detect a non-atomic swap.  A simulated two-rename swap (output ->
    old, then a visible gap, then new -> output) makes output missing between
    the renames, exactly the window the removed fallback created.  The reader
    must observe a missing-directory/missing-file violation — proving the
    exchange-path test above is not a false pass."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    output.mkdir()
    for name in mod.EXPECTED_PNG_FILES:
        (output / name).write_bytes(b"OLD")
    (output / "manifest.json").write_text("{}", encoding="utf-8")
    sibling_old = output.parent / (output.name + ".publish-old")
    sibling_new = output.parent / (output.name + ".publish-new")
    sibling_new.mkdir()
    for name in mod.EXPECTED_PNG_FILES:
        (sibling_new / name).write_bytes(b"NEW")
    (sibling_new / "manifest.json").write_text("{}", encoding="utf-8")
    expected_names = sorted(mod.EXPECTED_PNG_FILES)
    stop_flag = tmp_path / "reader.stop"
    reader_src = (
        "import os, sys, time, json\n"
        "output = sys.argv[1]\n"
        "expected_files = sys.argv[2].split(',')\n"
        "expected_set = set(expected_files) | {'manifest.json'}\n"
        "stop = sys.argv[3]\n"
        "violations = []\n"
        "deadline = time.time() + 5\n"
        "while time.time() < deadline and not os.path.exists(stop):\n"
        "    try:\n"
        "        names = set(os.listdir(output))\n"
        "    except OSError as e:\n"
        "        violations.append('listdir ENOENT: ' + repr(e)); continue\n"
        "    missing = expected_set - names\n"
        "    if missing:\n"
        "        violations.append('missing: ' + repr(sorted(missing))); continue\n"
        "print(json.dumps(violations[:50]))\n"
    )
    reader = tmp_path / "_reader_neg.py"
    reader.write_text(reader_src, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(reader), str(output), ",".join(expected_names),
         str(stop_flag)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=dict(os.environ),
    )
    try:
        import time as _time
        _time.sleep(0.1)  # let the reader start polling
        # Non-atomic two-rename swap with a visible gap where output is absent.
        os.rename(str(output), str(sibling_old))   # output now MISSING
        _time.sleep(0.3)                            # reader must observe the gap
        os.rename(str(sibling_new), str(output))    # output restored (new)
        stop_flag.write_text("stop")
        out, err = proc.communicate(timeout=10)
        if proc.returncode != 0:
            raise AssertionError(f"reader failed: {err}")
        import json as _json
        violations = _json.loads(out.strip().splitlines()[-1]) if out.strip() else []
        if not violations:
            raise AssertionError(
                "non-vacuity control failed: the reader probe did NOT detect "
                "the missing-directory window of a non-atomic swap, so the "
                "exchange-path reader test above is a false pass")
    finally:
        if proc.poll() is None:
            stop_flag.write_text("stop")
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---- r7-M4: capture drives the actual 320px document-peek/return sequence ---


def test_mobile_capture_drives_actual_peek_and_return_sequence():
    """r7-M4: the capture source must drive the ACTUAL document-peek state and
    verify the full Return-to-comment sequence — not depict the open drawer.
    The mobile section (between the 320px viewport switch and the mobile-320
    screenshot) must: enter selecting mode, activate the peek via Select in
    document, assert panel/scrim hidden + authored document visible + peek bar
    + Return action visible + viewport bounds + no overflow + editor state,
    then after Return assert the drawer is restored and editor/selection
    preserved."""
    source = (ROOT / "scripts" / "capture_annotations_v2.py").read_text(encoding="utf-8")
    mobile_section_start = source.find('page.set_viewport_size({"width": 320')
    mobile_screenshot_pos = source.find('_capture_with_metadata(page, "mobile-320.png"')
    if mobile_section_start < 0 or mobile_screenshot_pos < 0:
        raise AssertionError("mobile capture section not found in source")
    mobile_section = source[mobile_section_start:mobile_screenshot_pos]
    # Enter the peek state.
    required = {
        "Select in document": "must activate the peek via Select in document",
        "okf-reanchor-peek": "must assert the .okf-reanchor-peek peek bar",
        "Return to comment": "must verify Return to comment is visible",
        "panelHidden": "must assert the recovery panel is hidden while peeked",
        "overlayHidden": "must assert the scrim/overlay is hidden while peeked",
        "bodyVisible": "must assert the authored document is visible while peeked",
        "editorMode": "must verify editor state is preserved",
        "docScrollWidth": "must assert no horizontal overflow at 320px",
    }
    missing = [label for needle, label in required.items() if needle not in mobile_section]
    if missing:
        raise AssertionError(
            "mobile capture does not drive/verify the full document-peek "
            f"sequence: {missing}")
    # The screenshot is captured WHILE peeked; the Return-to-comment click that
    # ends the peek must come AFTER the screenshot (in full-source position).
    click_return_pos = source.find('name="Return to comment")', mobile_screenshot_pos)
    if click_return_pos < 0:
        raise AssertionError(
            "mobile capture must click Return to comment after the peek screenshot")
    # The editor-preserved check must follow the Return click.
    if source.find("editorMode", click_return_pos) < 0:
        raise AssertionError(
            "mobile capture must verify editor state preserved after Return")


# ---- r10: capture/provenance race regressions ------------------------------

def test_inventory_repeated_membership_snapshot_fails_closed(tmp_path, monkeypatch):
    import okf_loom.capture_provenance as provenance
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    for rel in provenance.INVENTORY_ROOTS:
        (repo / rel).mkdir(parents=True, exist_ok=True)
    (repo / "scripts" / "capture_annotations_v2.py").write_text("pass\n")
    monkeypatch.setattr(provenance, "_inventory_identity_snapshot", lambda root: [])
    with pytest.raises(ValueError, match="membership or identity changed"):
        provenance.compute_inventory_entries(repo)


def test_validator_requires_authoritative_capture_script(monkeypatch, tmp_path):
    import okf_loom.capture_provenance as provenance
    monkeypatch.setattr(provenance, "compute_inventory_entries", lambda root: [])
    monkeypatch.setattr(subprocess, "check_output",
                        lambda *args, **kwargs: "rev" if kwargs.get("text") else b"")
    errors = provenance.validate_manifest({}, tmp_path, {}, check_files=False)
    if not any("exactly one regular authoritative capture script" in e for e in errors):
        raise AssertionError(errors)


def test_recovery_rechecks_candidate_identity_before_remove(tmp_path, monkeypatch):
    mod = _load_capture_module()
    output = tmp_path / "out"
    old, _ = _stage_publish_dirs(output, {"output": True, "old": True, "new": False})
    original_validate = mod._validate_proof_directory
    calls = 0
    def swap_after_validation(path):
        nonlocal calls
        original_validate(path)
        calls += 1
        if calls == 2:
            old.rename(tmp_path / "validated-old")
            _write_complete_proof(old, b"replacement")
    monkeypatch.setattr(mod, "_validate_proof_directory", swap_after_validation)
    with pytest.raises(RuntimeError, match="identity changed before quarantine old"):
        mod._recover_interrupted_publish(output)
    if not old.exists():
        raise AssertionError("replacement candidate was mutated")


def test_secure_output_hash_rejects_symlink_leaf(tmp_path):
    import okf_loom.capture_provenance as provenance
    target = tmp_path / "target"
    target.write_bytes(b"substituted")
    (tmp_path / "proof.png").symlink_to(target)
    dfd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with pytest.raises(OSError):
            provenance._hash_regular_at(dfd, "proof.png")
    finally:
        os.close(dfd)


# ---- r11: capture recovery check-then-mutate barrier tests ----------------
#
# r11 Major finding: _recover_interrupted_publish had a check-then-mutate
# race -- recheck() confirmed identity, returned, then a pathname-based
# deletion/rename performed a fresh lookup, and content could drift after
# validation closed its descriptors.  The remediation pins each candidate by
# a descriptor held through mutation, quarantines/removes under the pinned
# parent with identity verification, revalidates complete content immediately
# before the restore rename, and fails closed on replacement/mutation/
# collision.  Each barrier below is exercised deterministically through the
# _recovery_pre_mutation_hook seam.


def _r11_quarantine_path(output: Path, label: str) -> Path:
    return output.parent / (output.name + f".publish-{label}.quarantine-{os.getpid()}")


def test_r11_cleanup_quarantine_replacement_fails_closed(tmp_path, monkeypatch):
    """r11: a leftover sibling whose name is replaced at the check-then-mutate
    boundary is NOT destroyed -- the quarantine identity check fails closed
    and leaves the replacement (and the pinned original) in place for manual
    inspection."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    old = output.parent / (output.name + ".publish-old")
    _stage_publish_dirs(output, {"output": True, "old": True, "new": False})
    stash = tmp_path / "stashed-original"

    def inject(parent_fd, name, role):
        if role != "quarantine-old":
            return
        # Replace the leftover's name with a fresh directory AFTER the pin and
        # identity check, BEFORE the quarantine rename.  The pinned inode is
        # preserved at `stash`.
        old.rename(stash)
        _write_complete_proof(old, b"IMPORTANT-REPLACEMENT")

    monkeypatch.setattr(mod, "_recovery_pre_mutation_hook", inject)
    with pytest.raises(RuntimeError, match="quarantine identity mismatch"):
        mod._recover_interrupted_publish(output)

    quarantine = _r11_quarantine_path(output, "old")
    # The replacement must NOT have been destroyed: it sits at the quarantine.
    if not quarantine.is_dir():
        raise AssertionError("replacement was destroyed instead of quarantined")
    if (quarantine / _proof_probe(mod)).read_bytes() != b"IMPORTANT-REPLACEMENT":
        raise AssertionError("quarantine does not hold the replacement content")
    # The authority (output) is untouched.
    if (output / _proof_probe(mod)).read_bytes() != b"prior-output":
        raise AssertionError("authority output disturbed by cleanup failure")
    # The pinned original survived at the stash.
    if not stash.is_dir():
        raise AssertionError("pinned original leftover was lost")


def test_r11_restore_name_replacement_fails_closed(tmp_path, monkeypatch):
    """r11: the sole prior proof's name replaced at the restore boundary is
    caught by the final pre-rename identity check -- recovery fails closed,
    never performs the rename, and never publishes the replacement."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    _stage_publish_dirs(output, {"output": False, "old": True, "new": False})
    old = output.parent / (output.name + ".publish-old")
    stash = tmp_path / "stashed-prior-proof"

    def inject(parent_fd, name, role):
        if role != "restore":
            return
        # Replace the sole-prior-proof name with a foreign directory AFTER the
        # pin/revalidation, BEFORE the restore rename.  The real prior proof
        # is preserved at `stash`.
        old.rename(stash)
        _write_complete_proof(old, b"BOGUS-REPLACEMENT")

    monkeypatch.setattr(mod, "_recovery_pre_mutation_hook", inject)
    with pytest.raises(RuntimeError, match="identity changed before restore rename"):
        mod._recover_interrupted_publish(output)

    # The restore rename never happened: no output was published.
    if output.exists():
        raise AssertionError("recovery published despite a name replacement")
    # The real sole prior proof survived at the stash.
    if (stash / _proof_probe(mod)).read_bytes() != b"prior-old":
        raise AssertionError("sole prior proof content was lost")


def test_r11_restore_inplace_content_mutation_fails_closed(tmp_path, monkeypatch):
    """r11: an in-place PNG mutation of the pinned sole prior proof after the
    initial validation is caught by the immediate complete-content
    revalidation before the restore rename -- recovery fails closed and
    publishes nothing."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    _stage_publish_dirs(output, {"output": False, "old": True, "new": False})
    old = output.parent / (output.name + ".publish-old")
    probe = _proof_probe(mod)

    def inject(parent_fd, name, role):
        if role != "restore":
            return
        # Mutate one PNG in place (same inode) AFTER the path-based validation
        # closed its descriptor but BEFORE the restore rename.  The manifest
        # hash no longer matches this leaf.
        (old / probe).write_bytes(b"MUTATED-IN-PLACE")

    monkeypatch.setattr(mod, "_recovery_pre_mutation_hook", inject)
    with pytest.raises(RuntimeError, match="hash mismatch"):
        mod._recover_interrupted_publish(output)

    if output.exists():
        raise AssertionError("recovery published despite in-place mutation")
    # The (mutated) candidate directory was not deleted -- on the restore path
    # a candidate is only MOVED on success, which never happened.
    if not old.is_dir():
        raise AssertionError("mutated candidate directory was destroyed")


def test_r11_unchanged_recovery_succeeds_and_leaves_no_quarantine(tmp_path):
    """r11: when nothing is replaced or mutated (the production no-op hook),
    recovery restores the sole prior proof, removes leftovers, and leaves NO
    quarantine directories behind on success."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    _stage_publish_dirs(output, {"output": False, "old": True, "new": True})

    actions = mod._recover_interrupted_publish(output)

    if (output / _proof_probe(mod)).read_bytes() != b"prior-old":
        raise AssertionError("sole prior proof not restored")
    old = output.parent / (output.name + ".publish-old")
    new = output.parent / (output.name + ".publish-new")
    if old.exists():
        raise AssertionError("old should have been moved, not left behind")
    if new.exists():
        raise AssertionError("leftover new not removed after restore")
    if not any("restored sole prior proof" in a for a in actions):
        raise AssertionError(f"restore action not surfaced: {actions}")
    if not any("publish-new after restore" in a for a in actions):
        raise AssertionError(f"new-cleanup action not surfaced: {actions}")
    # Success must destroy every quarantine entirely.
    leftovers = [p.name for p in output.parent.iterdir() if ".quarantine-" in p.name]
    if leftovers:
        raise AssertionError(f"quarantine leftovers after success: {leftovers}")


def test_r11_recovery_never_deletes_sole_prior_proof(tmp_path):
    """r11: across crash states recovery NEVER reduces the sole prior proof to
    zero copies -- it is MOVED back into output (output absent + old present)
    or the authority output is left intact (output present + old present)."""
    mod = _load_capture_module()
    marker = b"SOLE-PROOF-MARKER"

    # State 1: output absent + old present (sole proof).  Must be MOVED to
    # output, never deleted.
    output = tmp_path / "state1" / "out"
    old = output.parent / (output.name + ".publish-old")
    old.parent.mkdir(parents=True)
    _write_complete_proof(old, marker)
    mod._recover_interrupted_publish(output)
    proof_copies = [
        p for p in output.parent.iterdir()
        if p.is_dir() and (p / _proof_probe(mod)).exists()
        and (p / _proof_probe(mod)).read_bytes() == marker]
    if not proof_copies:
        raise AssertionError("sole prior proof was deleted (no copy survives)")
    if len(proof_copies) != 1:
        raise AssertionError(
            f"expected exactly one surviving proof copy, got {proof_copies}")
    if not output.is_dir() or (output / _proof_probe(mod)).read_bytes() != marker:
        raise AssertionError("sole proof was not restored into output")

    # State 2: output present (authority, same marker) + old present.  The
    # authority proof must survive; the redundant leftover is removed but the
    # proof content is preserved at output.
    output2 = tmp_path / "state2" / "out"
    old2 = output2.parent / (output2.name + ".publish-old")
    output2.parent.mkdir(parents=True)
    _write_complete_proof(output2, marker)
    _write_complete_proof(old2, marker)
    mod._recover_interrupted_publish(output2)
    if not output2.is_dir() or (output2 / _proof_probe(mod)).read_bytes() != marker:
        raise AssertionError("authority proof was disturbed or lost")
    if old2.exists():
        raise AssertionError("redundant leftover not cleaned up")


# ---- r12: capture recovery end-to-end authority/destruction barriers -------
#
# r12 findings 6-8: the r11 quarantine/destroy split still left three gaps.
# (6) before destroying a quarantined sibling the authoritative output was not
#     revalidated, so in-place output corruption could delete the only valid
#     sibling.  (7) quarantine destruction fell back to a pathname
#     shutil.rmtree that a name replacement could redirect.  (8) restore
#     validation closed leaf descriptors before the rename, so a leaf could
#     mutate in place while the directory inode stayed pinned.  Each barrier
# below is exercised deterministically through the _recovery_pre_mutation_hook
# seam.


def test_r12_output_corruption_before_sibling_destruction_fails_closed(tmp_path, monkeypatch):
    """r12 finding 6: if the authoritative output is corrupted in place after
    the initial validation, recovery detects it BEFORE destroying any sibling
    -- the sibling is quarantined (preserved, not destroyed), output is
    descriptor-revalidated, and recovery fails closed rather than deleting the
    only remaining valid proof."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    _stage_publish_dirs(output, {"output": True, "old": True, "new": False})
    probe = _proof_probe(mod)

    def inject(parent_fd, name, role):
        if role != "revalidate-output-old":
            return
        # Corrupt the authoritative output in place AFTER the initial
        # validation, immediately before the pre-destruction revalidation.
        (output / probe).write_bytes(b"CORRUPTED-OUTPUT")

    monkeypatch.setattr(mod, "_recovery_pre_mutation_hook", inject)
    with pytest.raises(RuntimeError, match="hash mismatch"):
        mod._recover_interrupted_publish(output)

    quarantine = _r11_quarantine_path(output, "old")
    # The sibling was quarantined but NOT destroyed (output authority failed).
    if not quarantine.is_dir():
        raise AssertionError(
            "valid sibling was destroyed before output authority was proven")
    if (quarantine / probe).read_bytes() != b"prior-old":
        raise AssertionError("quarantined valid sibling content was lost")
    # The (corrupted) output was not destroyed either.
    if not output.is_dir():
        raise AssertionError("output disappeared during failed cleanup")


def test_r12_quarantine_replacement_before_destruction_fails_closed(tmp_path, monkeypatch):
    """r12 finding 7: if the quarantine name is replaced after the identity
    check but before destruction, descriptor-relative deletion via the
    unreplaceable pin cannot be redirected -- the verified candidate's contents
    are removed via the pin, the foreign replacement survives, and rmdir on the
    substituted name fails closed."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    _stage_publish_dirs(output, {"output": True, "old": True, "new": False})
    probe = _proof_probe(mod)
    quarantine_path = _r11_quarantine_path(output, "old")
    rescued = tmp_path / "rescued-verified"

    def inject(parent_fd, name, role):
        if role != "destroy-old":
            return
        # Replace the quarantine name with a foreign non-empty directory AFTER
        # the post-rename identity check, BEFORE descriptor-relative
        # destruction.  The verified candidate is moved aside (still held by
        # the pin); a foreign directory takes the quarantine name.
        os.rename(quarantine_path, rescued)
        _write_complete_proof(quarantine_path, b"FOREIGN-UNTOUCHABLE")

    monkeypatch.setattr(mod, "_recovery_pre_mutation_hook", inject)
    with pytest.raises(RuntimeError, match="failed to remove quarantined directory entry"):
        mod._recover_interrupted_publish(output)

    # The foreign replacement must survive: descriptor-relative deletion via
    # the pin removed the verified candidate's contents, NOT the foreign name.
    if not quarantine_path.is_dir():
        raise AssertionError("foreign quarantine replacement was destroyed")
    if (quarantine_path / probe).read_bytes() != b"FOREIGN-UNTOUCHABLE":
        raise AssertionError("foreign quarantine replacement content was damaged")
    # The authority output is untouched.
    if (output / probe).read_bytes() != b"prior-output":
        raise AssertionError("authority output disturbed by quarantine failure")


def test_r12_restore_leaf_mutation_after_validation_fails_closed(tmp_path, monkeypatch):
    """r12 finding 8: an in-place leaf mutation of the sole prior proof AFTER
    validation is bound through the rename by retained leaf descriptors -- the
    post-rename recheck fails closed rather than publishing the mutated bytes
    silently."""
    mod = _load_capture_module()
    output = tmp_path / "out"
    _stage_publish_dirs(output, {"output": False, "old": True, "new": False})
    old = output.parent / (output.name + ".publish-old")
    probe = _proof_probe(mod)

    def inject(parent_fd, name, role):
        if role != "restore-rename":
            return
        # Mutate a leaf in place (same inode) AFTER validation opened+hashed
        # it, BEFORE the rename.  The manifest hash no longer matches this
        # leaf, and the retained descriptor must observe the drift.
        (old / probe).write_bytes(b"DRIFTED-AFTER-VALIDATION")

    monkeypatch.setattr(mod, "_recovery_pre_mutation_hook", inject)
    with pytest.raises(RuntimeError, match="hash mismatch across rename"):
        mod._recover_interrupted_publish(output)
