"""Public version diagnostics are honest and never rewrite bundle bytes."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from okf_loom.model import Bundle
from okf_loom.validate import CheckSpec, Severity, validate_bundle


CLI = Path(__file__).resolve().parents[1] / "scripts" / "okf-loom"


def make_bundle(tmp_path: Path, declaration: str | None = '"0.1"') -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    header = "" if declaration is None else f"---\nokf_version: {declaration}\n---\n"
    (bundle / "index.md").write_bytes(
        (header + "# Authored index\n\n* [A](notes/a.md)\n").encode()
    )
    (bundle / "log.md").write_bytes(b"# Log\r\n\r\n## 2026-09-08\r\nHandwritten.\r\n")
    (bundle / "notes").mkdir()
    (bundle / "notes/index.md").write_bytes(b"# Notes\n\n* [A](a.md)\n")
    # Only type is required; unknown metadata and body bytes must survive.
    (bundle / "notes/a.md").write_bytes(
        b'---\r\ntype: Note\r\ncustom: "untouched" # authored\r\n---\r\nBody.\r\n'
    )
    return bundle


def snapshot(bundle: Path) -> dict[Path, bytes | None]:
    return {p.relative_to(bundle): p.read_bytes() if p.is_file() else None
            for p in bundle.rglob("*")}


def run_cli(bundle: Path, *args: str) -> subprocess.CompletedProcess[str]:
    before = snapshot(bundle)
    result = subprocess.run([str(CLI), *args], cwd=bundle.parent,
                            capture_output=True, text=True, timeout=30)
    assert snapshot(bundle) == before
    assert "Traceback" not in result.stderr
    return result


@pytest.mark.parametrize(("declaration", "code", "severity"), [
    ('"0.1"', "spec.version_older_minor", Severity.INFO),  # Explicit legacy input.
    ('" 0.2 "', None, None),
    ('"00.02"', None, None),
    ('0.2', None, None),  # Preserve existing scalar coercion; recommend quotes.
    ('"0.0"', "spec.version_older_minor", Severity.INFO),
    ('"0.3"', "spec.version_newer_minor", Severity.WARNING),
    ('"1.0"', "spec.version_major_mismatch", Severity.WARNING),
    ('"10.123"', "spec.version_major_mismatch", Severity.WARNING),
    (None, "spec.version_undeclared", Severity.INFO),
])
def test_public_validation_versions(tmp_path: Path, declaration, code, severity) -> None:
    bundle = make_bundle(tmp_path, declaration)
    before = snapshot(bundle)
    loaded = Bundle.load(bundle)
    report = validate_bundle(loaded)
    findings = [f for f in report.findings if f.code.startswith("spec.version_")]
    assert [(f.code, f.severity) for f in findings] == ([] if code is None else [(code, severity)])
    assert report.ok  # Version guidance never prevents best-effort consumption.
    assert ("notes", "a") in loaded.concepts
    assert snapshot(bundle) == before
    result = run_cli(bundle, "validate", str(bundle), "--format", "json")
    assert result.returncode == 0, result.stderr
    codes = [f["code"] for f in json.loads(result.stdout)["findings"]
             if f["code"].startswith("spec.version_")]
    assert codes == ([] if code is None else [code])


MALFORMED = ["0.1suffix", "0.1.0", "0.1-rc1", "0.1+build", "0", "v0.1",
             "", "  ", "not-a-version", "0.1\ntrailing", "0. 1", "-1.0",
             "０.１", "0.١"]


@pytest.mark.parametrize("declaration", [*(json.dumps(v) for v in MALFORMED),
                                         "", "null", "false", "0", "[]", "{}"])
def test_malformed_declarations_warn_not_absent_or_compatible(tmp_path: Path, declaration: str) -> None:
    bundle = make_bundle(tmp_path, declaration)
    before = snapshot(bundle)
    loaded = Bundle.load(bundle)
    report = validate_bundle(loaded)
    findings = [f for f in report.findings if f.code.startswith("spec.version_")]
    assert [(f.code, f.severity) for f in findings] == [("spec.version_malformed", Severity.WARNING)]
    assert "Compatibility is unknown" in findings[0].message
    assert report.ok and ("notes", "a") in loaded.concepts
    selected = validate_bundle(loaded, checks=CheckSpec.only("spec.version_malformed"))
    assert any(f.code == "spec.version_malformed" for f in selected.findings)
    assert snapshot(bundle) == before
    permissive = run_cli(bundle, "validate", str(bundle))
    assert permissive.returncode == 0 and "spec.version_malformed" in permissive.stdout
    result = run_cli(bundle, "validate", str(bundle), "--strict", "--format", "json")
    assert result.returncode == 2, result.stderr
    assert any(f["code"] == "spec.version_malformed" for f in json.loads(result.stdout)["findings"])
    result = run_cli(bundle, "upgrade", str(bundle), "--apply")
    assert result.returncode == 1
    assert "spec.version_malformed" in result.stderr
    assert "Supported no-op" not in result.stdout


@pytest.mark.parametrize("declaration", ['"0.2"', '" 00.02 "', None])
@pytest.mark.parametrize("options", [[], ["--check"], ["--apply"], ["--to-spec", "0.2"],
                                      ["--apply", "--to-spec", "00.02"]])
def test_supported_info_check_and_noop(tmp_path: Path, declaration, options: list[str]) -> None:
    bundle = make_bundle(tmp_path, declaration)
    # Exercise both public bundle argument forms.
    bundle_args = ["--bundle", str(bundle)] if options else [str(bundle)]
    result = run_cli(bundle, "upgrade", *bundle_args, *options)
    assert result.returncode == 0, result.stderr
    assert "noop" in result.stdout and "no files changed" in result.stdout
    if declaration is None:
        assert "undeclared; assuming SPEC v0.2" in result.stdout


@pytest.mark.parametrize("source", ["0.0", "0.3", "1.0"])
def test_mismatch_info_is_best_effort_but_conversion_is_unsupported(tmp_path: Path, source: str) -> None:
    bundle = make_bundle(tmp_path, json.dumps(source))
    info = run_cli(bundle, "upgrade", str(bundle))
    assert info.returncode == 0 and "best-effort" in info.stdout
    for options in (["--check"], ["--apply"], ["--to-spec", "0.2"],
                    ["--apply", "--to-spec", source]):
        result = run_cli(bundle, "upgrade", str(bundle), *options)
        assert result.returncode == 1
        assert "unsupported" in result.stderr and "no files changed" in result.stderr
        assert "Supported no-op" not in result.stdout


@pytest.mark.parametrize("options", [[], ["--check"], ["--apply"]])
def test_unsupported_target_is_never_success(tmp_path: Path, options: list[str]) -> None:
    bundle = make_bundle(tmp_path)
    result = run_cli(bundle, "upgrade", str(bundle), *options, "--to-spec", "0.3")
    assert result.returncode == 1
    assert "unsupported target SPEC v0.3" in result.stderr


@pytest.mark.parametrize("target", MALFORMED)
def test_malformed_target_argument(tmp_path: Path, target: str) -> None:
    bundle = make_bundle(tmp_path)
    result = run_cli(bundle, "upgrade", str(bundle), "--apply", f"--to-spec={target}")
    assert result.returncode == 2
    assert "expected an ASCII major.minor version" in result.stderr


def test_info_without_bundle_and_usage_errors(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    info = run_cli(bundle, "upgrade")
    assert info.returncode == 0 and "implements SPEC v0.2" in info.stdout
    for options in (["--apply"], ["--check"], ["--to-spec", "0.1"],
                    [str(bundle), "--check", "--apply"],
                    [str(bundle), "--bundle", str(bundle)],
                    [str(bundle), "--to-spec"]):
        result = run_cli(bundle, "upgrade", *options)
        assert result.returncode == 2 and "error:" in result.stderr


@pytest.mark.parametrize("options", [[], ["--check"], ["--apply"]])
def test_malformed_source_is_not_informational_success(tmp_path: Path, options: list[str]) -> None:
    bundle = make_bundle(tmp_path, '"0.1suffix"')
    result = run_cli(bundle, "upgrade", str(bundle), *options)
    assert result.returncode == 1 and "spec.version_malformed" in result.stderr


def test_unreadable_declaration_is_not_assumed_current(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    (bundle / "index.md").write_text("---\nokf_version: [\n---\n# Index\n")
    result = run_cli(bundle, "upgrade", str(bundle), "--apply")
    assert result.returncode == 1 and "index.parse_failed" in result.stderr
    assert "Supported no-op" not in result.stdout


def test_missing_bundle_is_not_supported_noop(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    missing = tmp_path / "missing"
    result = run_cli(bundle, "upgrade", str(missing), "--check")
    assert result.returncode == 2  # Existing CLI FileNotFoundError policy.
    assert not missing.exists()
    assert "Supported no-op" not in result.stdout


def test_older_minor_is_not_a_backward_compatibility_certificate(tmp_path):
    bundle = make_bundle(tmp_path, '"0.1"')
    result = run_cli(bundle, 'validate', str(bundle), '--format', 'json')
    finding = next(row for row in json.loads(result.stdout)['findings']
                   if row['code'] == 'spec.version_older_minor')
    assert finding['severity'] == 'info'
    assert 'version numbers alone do not establish compatibility' in finding['message']
    assert 'upstream §13.1' in finding['message']
    assert 'Backward-compatible' not in finding['message']
    assert 'optional features only' not in finding['message']


def test_public_help_and_capabilities_use_current_upstream_sections(tmp_path):
    import okf_loom
    bundle = make_bundle(tmp_path)
    help_text = ' '.join(run_cli(bundle, '--help').stdout.split())
    assert 'upstream OKF 0.2 §11' in help_text  # Conformance
    assert 'upstream OKF 0.2 §8' in help_text   # Index files
    assert 'upstream OKF 0.2 §9' in help_text   # Log files
    links = ' '.join(run_cli(bundle, 'link-add', '--help').stdout.split())
    assert 'upstream OKF 0.2 §6.1' in links
    assert 'open-knowledge-format/blob/main/SPEC.md' in okf_loom.__doc__
    assert 'knowledge-catalog' not in okf_loom.__doc__
    capabilities = run_cli(bundle, 'capabilities', '--format', 'json')
    descriptions = '\n'.join(row['description'] for row in json.loads(capabilities.stdout)['available'])
    assert 'upstream OKF 0.2 §6.1' in descriptions
    assert 'upstream OKF 0.2 §8' in descriptions
    assert 'upstream OKF 0.2 §9' in descriptions
    assert 'Loom extension; current spec §4' in descriptions
    for obsolete in ('SPEC §5.', 'SPEC §6', 'SPEC §7', 'SPEC §10'):
        assert obsolete not in descriptions
