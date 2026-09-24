"""Session/cache directories are self-ignoring.

`ensure_session()` and the derived-index emit both drop a `.gitignore`
containing `*` inside the directory they create, so ephemeral state —
including the per-session `.token` secret — can never be committed from
ANY repository the bundle lives in, without parent-gitignore cooperation.
"""
from __future__ import annotations

from pathlib import Path

from okf_loom.io_utils import ensure_self_ignored
from okf_loom.studio import Studio


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    root.mkdir()
    (root / "index.md").write_text("---\nokf_version: \"0.1\"\n---\n# B\n", encoding="utf-8")
    (root / "a.md").write_text("---\ntype: T\ntitle: A\n---\nbody\n", encoding="utf-8")
    return root


def test_ensure_session_writes_self_ignore(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    studio = Studio.for_bundle(root)
    studio.ensure_session()
    gi = studio.session_dir / ".gitignore"
    assert gi.is_file(), "session dir did not get a self-ignoring .gitignore"
    assert "*" in gi.read_text(encoding="utf-8").splitlines(), \
        "self-ignore must contain a bare * line"


def test_self_ignore_never_overwrites_user_edits(tmp_path: Path) -> None:
    d = tmp_path / "state"
    d.mkdir()
    (d / ".gitignore").write_text("# user-managed\n", encoding="utf-8")
    ensure_self_ignored(d)
    assert (d / ".gitignore").read_text(encoding="utf-8") == "# user-managed\n"


def test_self_ignore_actually_hides_from_git(tmp_path: Path) -> None:
    """End-to-end with a real git repo: the session dir (and its .token)
    stay invisible to `git status` with NO parent gitignore rules."""
    import subprocess

    repo = tmp_path / "repo"
    root = repo / "kb"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    root.mkdir()
    (root / "index.md").write_text("---\nokf_version: \"0.1\"\n---\n# B\n", encoding="utf-8")
    studio = Studio.for_bundle(root)
    studio.ensure_session()
    (studio.session_dir / ".token").write_text("secret", encoding="utf-8")
    out = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert ".token" not in out and "session" not in out, (
        f"session state leaked into git status:\n{out}"
    )
