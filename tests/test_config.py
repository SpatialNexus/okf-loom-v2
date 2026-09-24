"""Tests for ``okf_loom.config`` and the ``okf init`` CLI command.

Pinned invariants (current spec §5):
  * ``OkfConfig.load`` reads ``okf-loom.config.yaml`` at the bundle root.
  * Missing config file ⇒ all defaults (config is purely additive).
  * Unknown keys survive in ``.raw`` (forward-compat; never dropped).
  * Path-bearing fields are bundle-relative: absolute paths and
    ``..``-escaping paths are rejected fail-closed.
  * ``okf init --bundle DIR`` scaffolds index.md + log.md + okf-loom.config.yaml
    and the result is a valid empty bundle (passes ``okf validate``).
  * ``okf init`` refuses to overwrite a non-empty bundle (FileExistsError ⇒
    exit code 2).
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from okf_loom.cli import main
from okf_loom.config import (
    CONFIG_FILENAME,
    DEFAULT_CONFIG_YAML,
    DiscoverConfig,
    OkfConfig,
    OkfConfigError,
    SearchConfig,
    ValidateConfig,
    ViewerConfig,
)


def _capture(argv: list[str]) -> tuple[int, str, str]:
    """Run main(argv) capturing stdout/stderr; return (exit_code, out, err)."""
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = main(argv)
    return rc, out_buf.getvalue(), err_buf.getvalue()


def _write_config(bundle_root: Path, text: str) -> Path:
    cfg = bundle_root / CONFIG_FILENAME
    cfg.write_text(text, encoding="utf-8")
    return cfg


# --- defaults ---------------------------------------------------------------


def test_missing_config_returns_all_defaults(tmp_path: Path) -> None:
    """No okf-loom.config.yaml present ⇒ every field matches the spec defaults."""
    cfg = OkfConfig.load(tmp_path)
    assert isinstance(cfg, OkfConfig)
    assert isinstance(cfg.viewer, ViewerConfig)
    assert isinstance(cfg.search, SearchConfig)
    assert isinstance(cfg.discover, DiscoverConfig)
    assert isinstance(cfg.validate, ValidateConfig)
    # Spec §11.1 default values.
    assert cfg.viewer.allow_active_code is False
    assert cfg.search.default_mode == "lexical"
    assert cfg.discover.suppress_phrases == ()
    assert cfg.discover.suppress_pairs == ()
    assert cfg.validate.default_profile == "spec"
    assert cfg.validate.fail_on_broken_links is False


def test_load_does_not_require_bundle_to_exist(tmp_path: Path) -> None:
    """Loading from a non-existent root is the same as missing config."""
    cfg = OkfConfig.load(tmp_path / "does_not_exist")
    assert cfg.viewer.allow_active_code is False
    assert cfg.search.default_mode == "lexical"


def test_missing_config_raw_is_empty(tmp_path: Path) -> None:
    cfg = OkfConfig.load(tmp_path)
    assert cfg.raw == {}


# --- explicit values from file ---------------------------------------------


def test_load_reads_values_from_file(tmp_path: Path) -> None:
    """Explicit values in okf-loom.config.yaml override the defaults."""
    _write_config(
        tmp_path,
        """\
viewer:
  title: "My Knowledge Bundle"
  allow_active_code: true
search:
  default_mode: semantic
discover:
  suppress_phrases: [Architecture, "Product Spec"]
  suppress_pairs:
    - source: /project-a/product-spec.md
      target: /project-b/product-spec.md
validate:
  default_profile: producer
  fail_on_broken_links: true
""",
    )
    cfg = OkfConfig.load(tmp_path)
    assert cfg.viewer.title == "My Knowledge Bundle"
    assert cfg.viewer.allow_active_code is True
    assert cfg.search.default_mode == "semantic"
    assert cfg.discover.suppress_phrases == ("architecture", "product spec")
    assert len(cfg.discover.suppress_pairs) == 1
    assert cfg.discover.suppress_pairs[0].source == ("project-a", "product-spec")
    assert cfg.discover.suppress_pairs[0].target == ("project-b", "product-spec")
    assert cfg.validate.default_profile == "producer"
    assert cfg.validate.fail_on_broken_links is True


def test_partial_config_keeps_other_defaults(tmp_path: Path) -> None:
    """Setting one key does not unset siblings."""
    _write_config(tmp_path, "viewer:\n  title: Only Title\n")
    cfg = OkfConfig.load(tmp_path)
    assert cfg.viewer.title == "Only Title"
    # Unset keys keep their defaults.
    assert cfg.viewer.allow_active_code is False
    assert cfg.search.default_mode == "lexical"


def test_empty_config_file_returns_defaults(tmp_path: Path) -> None:
    """An empty (or null-doc) config file is treated as all defaults."""
    _write_config(tmp_path, "")
    cfg = OkfConfig.load(tmp_path)
    assert cfg.viewer.allow_active_code is False


def test_comment_only_config_returns_defaults(tmp_path: Path) -> None:
    """A config file with only comments parses to an empty mapping."""
    _write_config(tmp_path, "# just a comment\n# another\n")
    cfg = OkfConfig.load(tmp_path)
    assert cfg.search.default_mode == "lexical"


# --- unknown keys preserved in .raw (forward-compat) ----------------------


def test_unknown_top_level_keys_preserved_in_raw(tmp_path: Path) -> None:
    """Unknown top-level keys MUST survive in .raw (SPEC forward-compat)."""
    _write_config(
        tmp_path,
        """\
future_key:
  nested: value
top_level_scalar: 42
viewer:
  title: "Hi"
""",
    )
    cfg = OkfConfig.load(tmp_path)
    assert cfg.raw["future_key"] == {"nested": "value"}
    assert cfg.raw["top_level_scalar"] == 42
    # Known key is not duplicated into raw.
    assert "viewer" not in cfg.raw


def test_unknown_section_keys_preserved_in_raw(tmp_path: Path) -> None:
    """Unknown keys inside a known section are also preserved in .raw."""
    _write_config(
        tmp_path,
        """\
viewer:
  title: "Hi"
  custom_palette: neon
  unknown_number: 7
search:
  default_mode: hybrid
  future_provider: magic
""",
    )
    cfg = OkfConfig.load(tmp_path)
    # Known section's known keys are populated.
    assert cfg.viewer.title == "Hi"
    assert cfg.search.default_mode == "hybrid"
    # Unknown section-keys preserved under the section name in raw.
    assert cfg.raw["viewer"]["custom_palette"] == "neon"
    assert cfg.raw["viewer"]["unknown_number"] == 7
    assert cfg.raw["search"]["future_provider"] == "magic"


def test_raw_round_trips_through_dict(tmp_path: Path) -> None:
    """.raw is JSON-serialisable plain data (no Path/datetime leakage)."""
    _write_config(
        tmp_path,
        "extra:\n  list: [1, 2, 3]\n  flag: true\n",
    )
    cfg = OkfConfig.load(tmp_path)
    # Must not raise.
    json.dumps(cfg.raw)


# --- fail-closed path validation -------------------------------------------


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",                # POSIX absolute
        "/absolute/inside/bundle",    # absolute even if nominally inside
        "../escape.md",               # one level up
        "../../etc/hosts",            # multiple levels up
        "sub/../../escape.md",        # escapes after a valid prefix
        "okf/../../../escape",        # deep escape
    ],
)
def test_absolute_or_escaping_paths_rejected(
    tmp_path: Path, bad_path: str
) -> None:
    """Path-bearing viewer fields reject absolute / escaping values."""
    _write_config(
        tmp_path,
        f"viewer:\n  override_dir: {bad_path}\n",
    )
    with pytest.raises(OkfConfigError):
        OkfConfig.load(tmp_path)


def test_extension_css_absolute_rejected(tmp_path: Path) -> None:
    _write_config(tmp_path, "viewer:\n  extension_css: /etc/shadow\n")
    with pytest.raises(OkfConfigError):
        OkfConfig.load(tmp_path)


def test_extension_js_escaping_rejected(tmp_path: Path) -> None:
    _write_config(tmp_path, "viewer:\n  extension_js: ../evil.js\n")
    with pytest.raises(OkfConfigError):
        OkfConfig.load(tmp_path)


# --- F5: studio.session_dir must be bundle-relative (no .. escape) --------


@pytest.mark.parametrize(
    "bad_path",
    [
        "../../outside",            # the exact finding from the review
        "../escape",                # one level up
        "sub/../../outside",        # escapes after a valid prefix
        ".okf-loom/../../../tmp/evil",   # deep escape with a valid prefix
    ],
)
def test_studio_session_dir_escaping_rejected(tmp_path: Path, bad_path: str) -> None:
    """F5: studio.session_dir with a ``..``-escaping value is rejected
    fail-closed. Before the fix only absolute paths were rejected, so
    ``../../tmp/evil`` passed and session state (events.jsonl, undo
    snapshots) would be written OUTSIDE the bundle root."""
    _write_config(tmp_path, f"studio:\n  session_dir: {bad_path}\n")
    with pytest.raises(OkfConfigError):
        OkfConfig.load(tmp_path)


@pytest.mark.parametrize("bad_path", ["/etc/passwd", "/absolute/session"])
def test_studio_session_dir_absolute_rejected(tmp_path: Path, bad_path: str) -> None:
    """F5: absolute session_dir values are still rejected (the existing
    absolute-only check in StudioConfig.from_dict is preserved as a backstop;
    OkfConfig.load's resolve-and-contain gate is the authoritative check)."""
    _write_config(tmp_path, f"studio:\n  session_dir: {bad_path}\n")
    with pytest.raises(OkfConfigError):
        OkfConfig.load(tmp_path)


def test_studio_session_dir_bundle_relative_accepted(tmp_path: Path) -> None:
    """F5 positive case: a legitimate nested bundle-relative session_dir is
    accepted and normalized by session_path."""
    _write_config(tmp_path, "studio:\n  session_dir: .okf-loom/custom-session\n")
    cfg = OkfConfig.load(tmp_path)
    assert cfg.studio.session_dir == ".okf-loom/custom-session"
    assert cfg.studio.session_path == ".okf-loom/custom-session"


def test_legitimate_nested_paths_accepted(tmp_path: Path) -> None:
    """Bundle-relative nested paths are accepted (positive case)."""
    _write_config(
        tmp_path,
        "viewer:\n"
        "  override_dir: .okf-loom/viewer\n"
        "  extension_css: .okf-loom/viewer/extension.css\n"
        "  extension_js: assets/sub/ext.js\n",
    )
    cfg = OkfConfig.load(tmp_path)
    assert cfg.viewer.override_dir == ".okf-loom/viewer"
    assert cfg.viewer.extension_css == ".okf-loom/viewer/extension.css"
    assert cfg.viewer.extension_js == "assets/sub/ext.js"


# --- malformed input fail-closed ------------------------------------------


def test_non_mapping_top_level_rejected(tmp_path: Path) -> None:
    """A YAML list or scalar at the top level is not a valid config."""
    _write_config(tmp_path, "- a\n- b\n")
    with pytest.raises(OkfConfigError):
        OkfConfig.load(tmp_path)


def test_malformed_yaml_rejected(tmp_path: Path) -> None:
    """Unparseable YAML raises OkfConfigError (not a raw YAMLError)."""
    _write_config(
        tmp_path,
        "viewer:\n  title: [unterminated\n  bad: : :\n",
    )
    with pytest.raises(OkfConfigError):
        OkfConfig.load(tmp_path)


# --- okf init CLI ----------------------------------------------------------


def test_init_creates_valid_bundle(tmp_path: Path) -> None:
    """``okf init --bundle DIR`` scaffolds a bundle that validates clean."""
    bundle = tmp_path / "kb"
    rc, out, err = _capture([
        "init", "--bundle", str(bundle), "--name", "My Bundle",
    ])
    assert rc == 0, f"stdout={out!r} stderr={err!r}"
    # Reserved scaffolding files exist.
    assert (bundle / "index.md").is_file()
    assert (bundle / "log.md").is_file()
    assert (bundle / CONFIG_FILENAME).is_file()
    # Root index declares okf_version.
    idx = (bundle / "index.md").read_text(encoding="utf-8")
    assert "okf_version" in idx
    assert "0.2" in idx
    # Config round-trips through OkfConfig.load (spec acceptance).
    cfg = OkfConfig.load(bundle)
    assert cfg.viewer.allow_active_code is False
    # The bundle itself validates cleanly (spec acceptance).
    rc2, out2, err2 = _capture(["validate", str(bundle)])
    assert rc2 == 0, f"validate failed: stdout={out2!r} stderr={err2!r}"


def test_init_writes_commented_defaults_config(tmp_path: Path) -> None:
    """The written config file is the commented-defaults template."""
    bundle = tmp_path / "kb"
    _capture(["init", "--bundle", str(bundle)])
    written = (bundle / CONFIG_FILENAME).read_text(encoding="utf-8")
    # The template body is used verbatim (commented defaults).
    assert written == DEFAULT_CONFIG_YAML


def test_init_prints_next_steps(tmp_path: Path) -> None:
    bundle = tmp_path / "kb"
    rc, out, _ = _capture(["init", "--bundle", str(bundle)])
    assert rc == 0
    assert "Next steps" in out
    assert "validate" in out


def test_init_refuses_non_empty_bundle(tmp_path: Path) -> None:
    """Init into a non-empty directory fails with exit code 2."""
    bundle = tmp_path / "kb"
    bundle.mkdir()
    (bundle / "stray.md").write_text("# nope\n", encoding="utf-8")
    rc, _, err = _capture(["init", "--bundle", str(bundle)])
    assert rc == 2
    assert "error" in err.lower() or "not empty" in err.lower()
    # Original content is untouched.
    assert (bundle / "stray.md").read_text(encoding="utf-8") == "# nope\n"
    # Init did not write any of its scaffolding.
    assert not (bundle / "index.md").exists()
    assert not (bundle / CONFIG_FILENAME).exists()


def test_init_into_empty_existing_dir_succeeds(tmp_path: Path) -> None:
    """An empty existing directory is fine (consistent with bootstrap)."""
    bundle = tmp_path / "kb"
    bundle.mkdir()
    rc, out, err = _capture(["init", "--bundle", str(bundle)])
    assert rc == 0, f"stdout={out!r} stderr={err!r}"
    assert (bundle / "index.md").is_file()


def test_init_name_appears_in_index(tmp_path: Path) -> None:
    bundle = tmp_path / "kb"
    _capture(["init", "--bundle", str(bundle), "--name", "Custom Name"])
    idx = (bundle / "index.md").read_text(encoding="utf-8")
    assert "Custom Name" in idx


def test_init_format_json_emits_valid_json(tmp_path: Path) -> None:
    """``--format json`` produces a parseable JSON document."""
    bundle = tmp_path / "kb"
    rc, out, _ = _capture([
        "init", "--bundle", str(bundle), "--format", "json",
    ])
    assert rc == 0
    data = json.loads(out)
    assert "created" in data
    assert CONFIG_FILENAME in data["created"]


def test_init_refuses_nested_bundle(tmp_path: Path) -> None:
    """Init refuses to create a bundle inside another bundle (SPEC §3).

    ``bootstrap_bundle``'s nested-bundle guard raises ``ValueError`` before
    any scaffolding is written. We accept either a non-zero exit code or the
    raised exception — both prove the refusal. (``cmd_init`` intentionally
    mirrors ``cmd_bootstrap`` here: the nested-bundle guard is upstream of
    the spec's FileExistsError guard and is not translated by the CLI.)
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "index.md").write_text(
        "---\nokf_version: '0.1'\n---\n# Parent\n", encoding="utf-8"
    )
    nested = parent / "child"
    try:
        rc, _, _ = _capture(["init", "--bundle", str(nested)])
        refused = rc != 0
    except ValueError:
        # Propagated from _refuse_if_nested; matches cmd_bootstrap behaviour.
        refused = True
    assert refused, "init must refuse to create a nested bundle"
    # Nothing was scaffolded.
    assert not nested.exists() or not any(nested.iterdir())


# --- iter-10 P1-26: strict bool coercion (§11.1 SECURITY) ------------------
#
# ``bool("false")`` is True in Python, so a quoted "false" previously
# ENABLED ``allow_active_code`` — silently turning on JS / full-HTML
# override execution. ``_coerce_bool`` fixes this fail-OPEN hole. The
# battery covers BOTH security-sensitive bools (``allow_active_code`` and
# ``fail_on_broken_links``) since they share the helper.

# (field_path, dataclass attr, yaml key) for each bool field under test.
_BOOL_FIELDS = [
    ("viewer", "allow_active_code", "allow_active_code"),
    ("validate", "fail_on_broken_links", "fail_on_broken_links"),
]

# Values that MUST coerce to a known bool (never raise).
_BOOL_ACCEPTED = [
    # (yaml literal, expected python bool)
    ("true", True),       # real YAML bool
    ("false", False),     # real YAML bool
    ('"true"', True),     # quoted truthy
    ('"yes"', True),
    ('"on"', True),
    ('"1"', True),
    ('"false"', False),   # quoted falsy — MUST NOT enable the flag
    ('"no"', False),
    ('"off"', False),
    ('"0"', False),
    ('""', False),        # empty string → fail-closed False
]

# Values that MUST be rejected fail-closed.
_BOOL_REJECTED = ['"maybe"', "bogus", '"2"', '"yep"', '"enable"']


@pytest.mark.parametrize("section,key", [(s, k) for s, _, k in _BOOL_FIELDS])
@pytest.mark.parametrize("yaml_val,expected", _BOOL_ACCEPTED)
def test_coerce_bool_accepts_known_tokens(
    tmp_path: Path, section: str, key: str, yaml_val: str, expected: bool
) -> None:
    """Known bool/str tokens coerce to the documented bool (fail-closed)."""
    _write_config(tmp_path, f"{section}:\n  {key}: {yaml_val}\n")
    cfg = OkfConfig.load(tmp_path)
    actual = getattr(getattr(cfg, section), key)
    assert actual is expected, (
        f"{section}.{key}={yaml_val!r} -> {actual!r}, expected {expected!r}"
    )


@pytest.mark.parametrize("section,key", [(s, k) for s, _, k in _BOOL_FIELDS])
@pytest.mark.parametrize("yaml_val", _BOOL_REJECTED)
def test_coerce_bool_rejects_ambiguous(
    tmp_path: Path, section: str, key: str, yaml_val: str
) -> None:
    """Ambiguous values raise OkfConfigError instead of guessing (§11.1)."""
    _write_config(tmp_path, f"{section}:\n  {key}: {yaml_val}\n")
    with pytest.raises(OkfConfigError, match="boolean"):
        OkfConfig.load(tmp_path)


@pytest.mark.parametrize("section,key", [(s, k) for s, _, k in _BOOL_FIELDS])
def test_coerce_bool_missing_key_uses_default(
    tmp_path: Path, section: str, key: str
) -> None:
    """An absent bool key falls back to its (False) default — fail-closed."""
    _write_config(tmp_path, f"{section}:\n  title: x\n")  # no bool key
    cfg = OkfConfig.load(tmp_path)
    assert getattr(getattr(cfg, section), key) is False


def test_quoted_false_does_not_enable_active_code(tmp_path: Path) -> None:
    """SECURITY regression guard: ``allow_active_code: "false"`` ⇒ False.

    This is the exact fail-OPEN hole P1-26 closes: ``bool("false")`` is
    True, so the old bare ``bool(...)`` coercion silently enabled active
    code on a quoted disable.
    """
    _write_config(tmp_path, 'viewer:\n  allow_active_code: "false"\n')
    cfg = OkfConfig.load(tmp_path)
    assert cfg.viewer.allow_active_code is False


# --- iter-10 P1-27: search enum validation (§11.1) -------------------------


# Authoritative valid values per field (mirrors search.SearchMode). Keep in
# sync with config._ALLOWED_* if the spec changes. Note: the provider backends
# (dense / vector-index / sqlite-fts) and their config knobs were removed;
# semantic is always SemanticLite, lexical is always BM25, so only
# ``default_mode`` survives under ``search:``.
_VALID_SEARCH_ENUMS = {
    "default_mode": ["lexical", "semantic", "hybrid", "tag", "entity", "relation"],
}


@pytest.mark.parametrize("field", list(_VALID_SEARCH_ENUMS))
def test_search_enum_accepts_all_valid(tmp_path: Path, field: str) -> None:
    """Every documented enum value is accepted by OkfConfig.load."""
    for value in _VALID_SEARCH_ENUMS[field]:
        _write_config(tmp_path, f"search:\n  {field}: {value}\n")
        cfg = OkfConfig.load(tmp_path)
        assert getattr(cfg.search, field) == value, (
            f"{field}={value!r} was rejected"
        )


@pytest.mark.parametrize("field", list(_VALID_SEARCH_ENUMS))
@pytest.mark.parametrize("bad", ["sematic", "Magic", "bogus", ""])
def test_search_enum_rejects_unknown(
    tmp_path: Path, field: str, bad: str
) -> None:
    """A typo'd enum value raises OkfConfigError (fail-closed, §11.1)."""
    # Empty string needs explicit quoting so YAML treats it as "" not null.
    yaml_val = f'"{bad}"' if bad == "" else bad
    _write_config(tmp_path, f"search:\n  {field}: {yaml_val}\n")
    with pytest.raises(OkfConfigError, match="unknown value"):
        OkfConfig.load(tmp_path)


def test_search_enum_tag_entity_relation_accepted(tmp_path: Path) -> None:
    """P1-27 NOTE: tag/entity/relation are valid SearchModes (not a typo)."""
    for mode in ("tag", "entity", "relation"):
        _write_config(tmp_path, f"search:\n  default_mode: {mode}\n")
        cfg = OkfConfig.load(tmp_path)
        assert cfg.search.default_mode == mode


def test_discover_suppress_pairs_rejects_bad_entries(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "discover:\n"
        "  suppress_pairs:\n"
        "    - source: /valid.md\n"
        "      target: '../bad.md'\n",
    )
    with pytest.raises(OkfConfigError, match="suppress_pairs"):
        OkfConfig.load(tmp_path)


# --- iter-10 P1-13a: validate.default_profile validation (§5) --------------


@pytest.mark.parametrize("profile", ["spec", "producer", "loose"])
def test_default_profile_valid_accepted(tmp_path: Path, profile: str) -> None:
    """Each documented profile name is accepted."""
    _write_config(tmp_path, f"validate:\n  default_profile: {profile}\n")
    cfg = OkfConfig.load(tmp_path)
    assert cfg.validate.default_profile == profile


@pytest.mark.parametrize("bad", ["bogus", "Prodcer", "SPEC", "strict", ""])
def test_default_profile_invalid_rejected(
    tmp_path: Path, bad: str
) -> None:
    """An unknown default_profile raises OkfConfigError (fail-closed, §5).

    Previously a typo silently behaved as ``spec`` while the JSON report
    claimed the typo name — a fail-OPEN config/report mismatch.
    """
    yaml_val = f'"{bad}"' if bad == "" else bad
    _write_config(tmp_path, f"validate:\n  default_profile: {yaml_val}\n")
    with pytest.raises(OkfConfigError, match="unknown value"):
        OkfConfig.load(tmp_path)


# --- iter-10 P2-8: fail_on_broken_links parse wiring -----------------------
#
# The ``OkfConfig.load`` parse of ``validate.fail_on_broken_links`` is
# already covered by ``test_load_reads_values_from_file`` (true → True) and
# by the P1-26 ``_coerce_bool`` battery above (true/false/quoted/rejected).
# The CLI wiring that consumes this flag lives in ``cli.cmd_validate``
# (cli.py:81-82: ``cfg.validate.fail_on_broken_links`` promotes
# ``--fail-on-broken-links`` when the CLI flag is absent). That wiring is
# owned by tests/test_cli.py, not this file.
