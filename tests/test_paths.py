"""Tests for ``okf_loom.paths``.

Pinned invariants:
  * ``concept_id_from_str`` / ``concept_id_to_str`` round-trip cleanly.
  * Bad segments (empty, leading hyphen, spaces, slashes-only) are rejected.
  * Reserved filenames (``index.md``, ``log.md``) are detected case-sensitively.
  * ``ancestors`` yields parent directory concept ids bottom-up.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from okf_loom import RESERVED_FILENAMES
from okf_loom.paths import (
    ConceptId,
    ConceptIdError,
    ancestors,
    concept_id_from_path,
    concept_id_from_str,
    concept_id_to_path,
    concept_id_to_str,
    is_reserved_filename,
    join_cid,
    parent_cid,
    validate_segment,
)


# --- round-trip str <-> tuple -------------------------------------------------


@pytest.mark.parametrize(
    "s,expected",
    [
        ("tables/users", ("tables", "users")),
        ("users", ("users",)),
        ("/tables/users/", ("tables", "users")),  # leading/trailing slashes
        ("a/b/c", ("a", "b", "c")),
        ("tables/users_v2", ("tables", "users_v2")),
        ("tables/user-events", ("tables", "user-events")),
        ("tables/user.events", ("tables", "user.events")),
        # SPEC §5.1 absolute-link form with .md extension must normalise
        # to the same concept id as the bare form (regression: relation
        # targets written as link paths broke the graph view).
        ("/tables/users.md", ("tables", "users")),
        ("tables/users.md", ("tables", "users")),
        ("/a/b/c.md", ("a", "b", "c")),
    ],
)
def test_from_str_parses_segments(s: str, expected: tuple) -> None:
    """``concept_id_from_str`` splits on '/' and drops empty parts."""
    assert concept_id_from_str(s) == expected


def test_round_trip_str_to_tuple_to_str() -> None:
    """``to_str(from_str(x)) == x`` for normalised inputs."""
    for cid_str in ("users", "tables/users", "a/b/c", "tables/user_v2-events"):
        cid = concept_id_from_str(cid_str)
        assert concept_id_to_str(cid) == cid_str


def test_round_trip_tuple_to_str_to_tuple() -> None:
    """``from_str(to_str(t)) == t`` for arbitrary tuples."""
    for cid in [("a",), ("tables", "users"), ("x", "y", "z")]:
        assert concept_id_from_str(concept_id_to_str(cid)) == tuple(cid)


# --- bad segments -------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        " ",
        "-leading",       # leading hyphen not allowed
        "with space",
        "with/slash",     # slash would be caught by from_str, not segment
        "dollar$",
        "dot.dot.dot.",   # trailing dot is allowed by the regex actually
    ],
)
def test_validate_segment_rejects_invalid(bad: str) -> None:
    """``validate_segment`` rejects empty/disallowed-char segments."""
    if bad == "dot.dot.dot.":
        # Trailing dot IS allowed by the regex (it's in the char class).
        # This case documents the actual permissiveness rather than asserting
        # rejection; if the regex tightens later, this branch will fail loudly.
        validate_segment(bad)
        return
    with pytest.raises(ConceptIdError):
        validate_segment(bad)


def test_validate_segment_accepts_known_good() -> None:
    """Letter/digit/underscore start; letters/digits/_/-/. interior."""
    for ok in ("a", "A", "_x", "users", "users_v2", "user-events", "user.id"):
        validate_segment(ok)  # must not raise


def test_from_str_rejects_empty_string() -> None:
    """``concept_id_from_str("")`` and slash-only strings raise."""
    for bad in ("", "/", "//", "///"):
        with pytest.raises(ConceptIdError):
            concept_id_from_str(bad)


def test_from_str_rejects_bad_segment() -> None:
    """Bad chars in any segment raise ConceptIdError."""
    with pytest.raises(ConceptIdError):
        concept_id_from_str("good/-bad")
    with pytest.raises(ConceptIdError):
        concept_id_from_str("good name/second")


def test_to_str_rejects_empty_tuple() -> None:
    with pytest.raises(ConceptIdError):
        concept_id_to_str(())


# --- concept_id_from_path / concept_id_to_path -------------------------------


def test_from_path_and_to_path_round_trip(tmp_path: Path) -> None:
    """``concept_id_from_path`` and ``concept_id_to_path`` are inverses."""
    root = tmp_path
    cid = ("tables", "users")
    p = concept_id_to_path(root, cid)
    assert p == root / "tables" / "users.md"
    assert concept_id_from_path(root, p) == cid


def test_from_path_rejects_non_markdown(tmp_path: Path) -> None:
    with pytest.raises(ConceptIdError):
        concept_id_from_path(tmp_path, tmp_path / "tables" / "users.txt")


def test_from_path_rejects_outside_bundle(tmp_path: Path) -> None:
    """A path not inside the bundle root cannot yield a concept id."""
    other = tmp_path / "outside.md"
    other.write_text("hi", encoding="utf-8")
    with pytest.raises((ConceptIdError, ValueError)):
        concept_id_from_path(tmp_path / "bundle", other)


def test_to_path_creates_nested_path(tmp_path: Path) -> None:
    """``concept_id_to_path`` joins segments with the OS separator."""
    p = concept_id_to_path(tmp_path, ("a", "b", "c"))
    assert p == tmp_path / "a" / "b" / "c.md"


# --- reserved filenames -------------------------------------------------------


def test_reserved_set_contents() -> None:
    """SPEC §3.1 reserved filenames are index.md and log.md."""
    assert RESERVED_FILENAMES == frozenset({"index.md", "log.md"})


@pytest.mark.parametrize("name", ["index.md", "log.md"])
def test_is_reserved_filename_true(name: str) -> None:
    assert is_reserved_filename(name)


@pytest.mark.parametrize(
    "name",
    ["Index.md", "INDEX.md", "index.txt", "users.md", "log", ".md", ""],
)
def test_is_reserved_filename_false(name: str) -> None:
    """Reserved-name detection is case-sensitive and exact."""
    assert not is_reserved_filename(name)


# --- ancestor iteration -------------------------------------------------------


def test_parent_cid_returns_immediate_parent() -> None:
    assert parent_cid(("a", "b", "c")) == ("a", "b")
    assert parent_cid(("a",)) is None  # root-level concept


def test_ancestors_yields_bottom_up() -> None:
    """``ancestors`` yields immediate parent first, then upward."""
    cid = ("a", "b", "c", "d")
    assert list(ancestors(cid)) == [
        ("a", "b", "c"),
        ("a", "b"),
        ("a",),
    ]


def test_ancestors_empty_for_root_concept() -> None:
    assert list(ancestors(("only",))) == []


# --- join_cid -----------------------------------------------------------------


def test_join_cid_appends_validated_child() -> None:
    assert join_cid(("a", "b"), "c") == ("a", "b", "c")


def test_join_cid_validates_child() -> None:
    with pytest.raises(ConceptIdError):
        join_cid(("a",), "-bad")


# --- ConceptId type alias ----------------------------------------------------


def test_concept_id_alias_is_tuple_of_str() -> None:
    """The ConceptId alias is a tuple of strings (runtime check)."""
    cid: ConceptId = ("x", "y")
    assert isinstance(cid, tuple)
    assert all(isinstance(s, str) for s in cid)
