"""Tests for bundle scanning exclusions (``okf_loom.ignore`` + ``bundle:`` config).

Pinned invariants (current spec §5):
  * ``Bundle.load`` / ``_BundleWatcher._scan`` / ``import_directory`` all scan
    through ``iter_markdown_files``, never a raw ``rglob("*.md")``.
  * Built-in defaults prune hidden dirs, ``node_modules``, ``__pycache__``,
    ``venv``, ``bower_components`` — and any non-root dir containing a
    ``.git`` entry (nested clone/submodule/worktree).
  * ``.gitignore`` files (root AND nested, scoped) are respected by default;
    ``bundle.respect_gitignore: false`` opts out.
  * ``bundle.exclude`` patterns sit above defaults and ``.gitignore``;
    negations there re-include paths those groups dropped (git-style: not
    from under a pruned directory).
  * ``bundle.include`` beats every exclusion source (incl. the structural
    nested-repo prune) and reaches inside pruned directories. Directory
    form revives a subtree back to normal scanning (with the exclusion
    rules that were overridden at the revived dir masked beneath it); glob
    form force-includes everything it matches; basename form never opens
    pruned dirs. Negations are rejected fail-closed.
  * Hidden FILES still load (only hidden directories are default-pruned).
  * An unparseable config leaves the bundle loadable: LoadWarning
    ``config.unparseable`` + default excludes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from okf_loom.config import CONFIG_FILENAME, BundleConfig, OkfConfig, OkfConfigError
from okf_loom.ignore import compile_rule, is_ignored, iter_markdown_files
from okf_loom.model import Bundle


def _write(root: Path, rel: str, text: str = "---\ntype: Note\n---\nbody\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _rel_set(root: Path, paths: list[Path]) -> set[str]:
    return {p.relative_to(root).as_posix() for p in paths}


# --- pattern matcher ---------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "is_dir", "expected"),
    [
        # basename patterns match at any depth
        ("*.tmp.md", "a/b/x.tmp.md", False, True),
        ("*.tmp.md", "x.tmp.md", False, True),
        ("build", "deep/build", True, True),
        ("build", "deep/build.md", True, False),
        # dir-only (trailing slash) never matches files
        ("build/", "build", True, True),
        ("build/", "build", False, False),
        # a "/" anywhere anchors to the base
        ("/docs", "docs", True, True),
        ("/docs", "x/docs", True, False),
        ("a/b.md", "a/b.md", False, True),
        ("a/b.md", "z/a/b.md", False, False),
        # ** forms
        ("**/build/", "a/b/build", True, True),
        ("a/**/b.md", "a/b.md", False, True),
        ("a/**/b.md", "a/x/y/b.md", False, True),
        ("a/**/b.md", "z/a/b.md", False, False),
        ("logs/**", "logs/x/y.md", False, True),
        ("logs/**", "logs", True, False),
        # ? and character classes stay within a segment
        ("v?.md", "docs/v1.md", False, True),
        ("v?.md", "docs/v/x.md", False, False),
        ("v[12].md", "v2.md", False, True),
        ("v[!12].md", "v3.md", False, True),
        ("v[!12].md", "v1.md", False, False),
        # escapes
        (r"\#literal.md", "d/#literal.md", False, True),
        (r"\!bang.md", "!bang.md", False, True),
    ],
)
def test_compile_rule_matching(pattern: str, path: str, is_dir: bool, expected: bool) -> None:
    rule = compile_rule(pattern)
    assert rule is not None
    assert is_ignored([[rule]], path, is_dir) is expected


def test_blank_and_comment_lines_compile_to_none() -> None:
    assert compile_rule("") is None
    assert compile_rule("   ") is None
    assert compile_rule("# comment") is None
    assert compile_rule("!") is None
    assert compile_rule("/") is None


def test_last_match_wins_within_group_and_later_group_overrides() -> None:
    exclude_all = compile_rule("*.md")
    reinclude = compile_rule("!keep.md")
    assert is_ignored([[exclude_all, reinclude]], "keep.md", False) is False
    assert is_ignored([[exclude_all, reinclude]], "drop.md", False) is True
    # a later GROUP overrides an earlier one outright
    assert is_ignored([[reinclude], [exclude_all]], "keep.md", False) is True


def test_nested_gitignore_rules_are_scoped_to_their_directory(tmp_path: Path) -> None:
    _write(tmp_path, "sub/local-only/c.md")
    _write(tmp_path, "sub/kept.md")
    _write(tmp_path, "other/local-only/d.md")
    (tmp_path / "sub" / ".gitignore").write_text("local-only/\n", encoding="utf-8")

    got = _rel_set(tmp_path, iter_markdown_files(tmp_path))
    assert got == {"sub/kept.md", "other/local-only/d.md"}


# --- default excludes --------------------------------------------------------


def test_default_excludes_prune_vendor_and_hidden_dirs(tmp_path: Path) -> None:
    _write(tmp_path, "index.md")
    _write(tmp_path, "docs/a.md")
    _write(tmp_path, "node_modules/pkg/README.md")
    _write(tmp_path, "__pycache__/junk.md")
    _write(tmp_path, "venv/lib/site.md")
    _write(tmp_path, "bower_components/x/README.md")
    _write(tmp_path, ".okf-loom/session/notes.md")
    _write(tmp_path, ".venv/lib/site.md")

    got = _rel_set(tmp_path, iter_markdown_files(tmp_path))
    assert got == {"index.md", "docs/a.md"}


def test_hidden_files_still_load_only_hidden_dirs_are_pruned(tmp_path: Path) -> None:
    _write(tmp_path, ".dotted-name.md")
    _write(tmp_path, ".hidden-dir/inside.md")

    got = _rel_set(tmp_path, iter_markdown_files(tmp_path))
    assert got == {".dotted-name.md"}


def test_nested_git_repo_is_pruned_dir_and_file_forms(tmp_path: Path) -> None:
    _write(tmp_path, "ours.md")
    _write(tmp_path, "clone/README.md")
    (tmp_path / "clone" / ".git").mkdir()
    _write(tmp_path, "worktree/README.md")
    (tmp_path / "worktree" / ".git").write_text("gitdir: elsewhere", encoding="utf-8")

    got = _rel_set(tmp_path, iter_markdown_files(tmp_path))
    assert got == {"ours.md"}


def test_bundle_root_itself_may_be_a_git_repo(tmp_path: Path) -> None:
    _write(tmp_path, "ours.md")
    (tmp_path / ".git").mkdir()  # the root's own repo must not prune the root
    got = _rel_set(tmp_path, iter_markdown_files(tmp_path))
    assert got == {"ours.md"}


# --- .gitignore respect ------------------------------------------------------


def test_gitignore_respected_by_default_with_negation(tmp_path: Path) -> None:
    _write(tmp_path, "kept.md")
    _write(tmp_path, "generated/out.md")
    _write(tmp_path, "scratch.tmp.md")
    _write(tmp_path, "keep.tmp.md")
    (tmp_path / ".gitignore").write_text(
        "generated/\n*.tmp.md\n!keep.tmp.md\n", encoding="utf-8"
    )

    got = _rel_set(tmp_path, iter_markdown_files(tmp_path))
    assert got == {"kept.md", "keep.tmp.md"}

    got_off = _rel_set(
        tmp_path, iter_markdown_files(tmp_path, respect_gitignore=False)
    )
    assert got_off == {"kept.md", "keep.tmp.md", "generated/out.md", "scratch.tmp.md"}


def test_self_ignoring_session_dir_is_invisible_via_gitignore(tmp_path: Path) -> None:
    # The .okf-loom state dirs are written self-ignoring (a .gitignore with
    # "*" inside). Even at a NON-hidden custom location, gitignore respect
    # keeps their contents out of the scan.
    _write(tmp_path, "real.md")
    _write(tmp_path, "my-session/stray.md")
    (tmp_path / "my-session" / ".gitignore").write_text("*\n", encoding="utf-8")

    got = _rel_set(tmp_path, iter_markdown_files(tmp_path))
    assert got == {"real.md"}


# --- config precedence -------------------------------------------------------


def test_config_exclude_beats_gitignore_negation_and_defaults(tmp_path: Path) -> None:
    _write(tmp_path, "kept.md")
    _write(tmp_path, "drafts/wip.md")
    _write(tmp_path, ".docs/hidden-docs.md")
    (tmp_path / ".gitignore").write_text("!drafts/\n", encoding="utf-8")

    got = _rel_set(
        tmp_path,
        iter_markdown_files(tmp_path, exclude=["drafts/", "!.docs/"]),
    )
    # config "drafts/" overrides the .gitignore negation; "!.docs/"
    # re-includes a directory the hidden-dir default pruned.
    assert got == {"kept.md", ".docs/hidden-docs.md"}


# --- bundle.include: the add-back lever --------------------------------------


def test_include_reaches_into_pruned_dir_without_opening_siblings(tmp_path: Path) -> None:
    _write(tmp_path, "real.md")
    _write(tmp_path, "node_modules/my-pkg/docs/guide.md")
    _write(tmp_path, "node_modules/my-pkg/other.md")
    _write(tmp_path, "node_modules/lib/README.md")

    got = _rel_set(
        tmp_path,
        iter_markdown_files(tmp_path, include=["node_modules/my-pkg/docs/"]),
    )
    assert got == {"real.md", "node_modules/my-pkg/docs/guide.md"}


def test_include_revives_nested_git_repo(tmp_path: Path) -> None:
    _write(tmp_path, "ours.md")
    _write(tmp_path, "clone/README.md")
    (tmp_path / "clone" / ".git").mkdir()
    _write(tmp_path, "clone/node_modules/x.md")  # junk INSIDE stays pruned

    got = _rel_set(tmp_path, iter_markdown_files(tmp_path, include=["clone/"]))
    assert got == {"ours.md", "clone/README.md"}


def test_include_revives_hidden_dir(tmp_path: Path) -> None:
    _write(tmp_path, ".docs/hidden.md")
    got = _rel_set(tmp_path, iter_markdown_files(tmp_path, include=[".docs/"]))
    assert got == {".docs/hidden.md"}


def test_include_beats_exclude_and_gitignore(tmp_path: Path) -> None:
    _write(tmp_path, "keep.md")
    _write(tmp_path, "generated/out.md")
    (tmp_path / ".gitignore").write_text("generated/\nkeep.md\n", encoding="utf-8")

    got = _rel_set(
        tmp_path,
        iter_markdown_files(
            tmp_path, exclude=["keep.md"], include=["keep.md", "generated/"]
        ),
    )
    assert got == {"keep.md", "generated/out.md"}


def test_revival_masks_overridden_ancestor_rule_but_not_unrelated_ones(tmp_path: Path) -> None:
    # An anchored .gitignore rule that killed the whole tree is masked at the
    # revived dir (the override must hold beneath it); a basename rule like
    # *.tmp.md keeps applying inside, as do the built-in junk-dir defaults.
    _write(tmp_path, "node_modules/pkg/docs/guide.md")
    _write(tmp_path, "node_modules/pkg/docs/draft.tmp.md")
    _write(tmp_path, "node_modules/pkg/docs/node_modules/inner.md")
    (tmp_path / ".gitignore").write_text("node_modules/**\n*.tmp.md\n", encoding="utf-8")

    got = _rel_set(
        tmp_path,
        iter_markdown_files(tmp_path, include=["node_modules/pkg/docs/"]),
    )
    assert got == {"node_modules/pkg/docs/guide.md"}


def test_include_glob_form_force_includes_everything_beneath(tmp_path: Path) -> None:
    _write(tmp_path, "node_modules/my-pkg/other.md")
    _write(tmp_path, "node_modules/my-pkg/docs/node_modules/inner.md")
    _write(tmp_path, "node_modules/lib/README.md")

    got = _rel_set(
        tmp_path, iter_markdown_files(tmp_path, include=["node_modules/my-pkg/**"])
    )
    assert got == {
        "node_modules/my-pkg/other.md",
        "node_modules/my-pkg/docs/node_modules/inner.md",
    }


def test_basename_include_applies_where_reached_but_opens_nothing(tmp_path: Path) -> None:
    _write(tmp_path, "visible/keep.md")
    _write(tmp_path, "node_modules/deep/keep.md")
    (tmp_path / ".gitignore").write_text("keep.md\n", encoding="utf-8")

    got = _rel_set(tmp_path, iter_markdown_files(tmp_path, include=["keep.md"]))
    # rescued where the scan already reaches; pruned dirs stay closed
    assert got == {"visible/keep.md"}


def test_include_negation_rejected_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        iter_markdown_files(tmp_path, include=["!nope/"])
    with pytest.raises(OkfConfigError):
        BundleConfig.from_dict({"include": ["!nope/"]})


# --- BundleConfig parsing ----------------------------------------------------


def test_bundle_config_defaults() -> None:
    cfg = BundleConfig.from_dict(None)
    assert cfg.exclude == ()
    assert cfg.include == ()
    assert cfg.respect_gitignore is True


def test_bundle_config_single_string_and_list_forms() -> None:
    assert BundleConfig.from_dict({"exclude": "drafts/**"}).exclude == ("drafts/**",)
    assert BundleConfig.from_dict({"exclude": ["a/", "!b/"]}).exclude == ("a/", "!b/")
    assert BundleConfig.from_dict({"include": "vendor/"}).include == ("vendor/",)
    assert BundleConfig.from_dict({"include": ["a/", "b/**"]}).include == ("a/", "b/**")


def test_bundle_config_rejects_non_string_entries_fail_closed() -> None:
    with pytest.raises(OkfConfigError):
        BundleConfig.from_dict({"exclude": ["ok/", 7]})
    with pytest.raises(OkfConfigError):
        BundleConfig.from_dict({"exclude": {"not": "a list"}})
    with pytest.raises(OkfConfigError):
        BundleConfig.from_dict({"respect_gitignore": "maybe"})


def test_bundle_section_loads_from_yaml_and_unknown_keys_survive(tmp_path: Path) -> None:
    (tmp_path / CONFIG_FILENAME).write_text(
        "bundle:\n"
        "  exclude: [drafts/**]\n"
        "  respect_gitignore: false\n"
        "  acme_future_key: 1\n",
        encoding="utf-8",
    )
    cfg = OkfConfig.load(tmp_path)
    assert cfg.bundle.exclude == ("drafts/**",)
    assert cfg.bundle.respect_gitignore is False
    assert cfg.raw["bundle"] == {"acme_future_key": 1}


# --- Bundle.load integration -------------------------------------------------


def test_bundle_load_skips_vendor_noise_and_gitignored(tmp_path: Path) -> None:
    _write(tmp_path, "docs/real.md")
    _write(tmp_path, "node_modules/lib/README.md", "no frontmatter")
    _write(tmp_path, "clone/README.md", "no frontmatter")
    (tmp_path / "clone" / ".git").mkdir()
    _write(tmp_path, "build-out.md", "no frontmatter")
    (tmp_path / ".gitignore").write_text("build-out.md\n", encoding="utf-8")

    bundle = Bundle.load(tmp_path)
    ids = {"/".join(cid) for cid in bundle.concepts}
    assert ids == {"docs/real"}
    assert not bundle.warnings


def test_bundle_load_honours_config_exclude_and_respect_flag(tmp_path: Path) -> None:
    _write(tmp_path, "docs/real.md")
    _write(tmp_path, "drafts/wip.md")
    _write(tmp_path, "gitignored.md")
    (tmp_path / ".gitignore").write_text("gitignored.md\n", encoding="utf-8")
    (tmp_path / CONFIG_FILENAME).write_text(
        "bundle:\n  exclude: [drafts/]\n  respect_gitignore: false\n",
        encoding="utf-8",
    )

    bundle = Bundle.load(tmp_path)
    ids = {"/".join(cid) for cid in bundle.concepts}
    assert ids == {"docs/real", "gitignored"}


def test_bundle_load_honours_config_include(tmp_path: Path) -> None:
    _write(tmp_path, "docs/real.md")
    _write(tmp_path, "node_modules/my-pkg/docs/guide.md")
    _write(tmp_path, "node_modules/lib/README.md", "no frontmatter")
    (tmp_path / CONFIG_FILENAME).write_text(
        "bundle:\n  include: [node_modules/my-pkg/docs/]\n",
        encoding="utf-8",
    )

    bundle = Bundle.load(tmp_path)
    ids = {"/".join(cid) for cid in bundle.concepts}
    assert ids == {"docs/real", "node_modules/my-pkg/docs/guide"}


def test_bundle_load_survives_unparseable_config_with_warning(tmp_path: Path) -> None:
    _write(tmp_path, "docs/real.md")
    _write(tmp_path, "node_modules/x/README.md", "no frontmatter")
    (tmp_path / CONFIG_FILENAME).write_text("bundle: [not, a, mapping", encoding="utf-8")

    bundle = Bundle.load(tmp_path)
    ids = {"/".join(cid) for cid in bundle.concepts}
    assert ids == {"docs/real"}  # default excludes still applied
    assert any(w.code == "config.unparseable" for w in bundle.warnings)


# --- watcher + import integration ---------------------------------------------


def test_bundle_watcher_scan_honours_excludes(tmp_path: Path) -> None:
    from okf_loom.server import _BundleWatcher

    _write(tmp_path, "real.md")
    _write(tmp_path, "node_modules/lib/README.md")
    (tmp_path / CONFIG_FILENAME).write_text(
        "bundle:\n  exclude: [secret/]\n", encoding="utf-8"
    )
    _write(tmp_path, "secret/hidden.md")

    watcher = _BundleWatcher(tmp_path, lambda: None)
    names = {p.name for p in watcher._snapshot}
    assert "real.md" in names
    assert "README.md" not in names
    assert "hidden.md" not in names
    assert CONFIG_FILENAME in names  # config file itself is still tracked


def test_import_directory_skips_vendor_noise(tmp_path: Path) -> None:
    from okf_loom.bootstrap import import_directory

    src = tmp_path / "src"
    _write(src, "notes/real.md", "just a body\n")
    _write(src, "node_modules/pkg/README.md", "vendor\n")
    _write(src, "ignored.md", "generated\n")
    (src / ".gitignore").write_text("ignored.md\n", encoding="utf-8")

    dest = tmp_path / "dest"
    result = import_directory(src, dest)
    assert result["imported"] == 1
    assert (dest / "notes" / "real.md").exists()
    assert not (dest / "node_modules").exists()
    assert not (dest / "ignored.md").exists()
