"""Browser-free proofs for scripts/record_showcase_media.py.

The recorder drives a live studio in Chromium; these tests pin the pieces it
depends on (terminal rendering, frame timing, the privacy screen, the
condensed ``wait`` output), the clip contract, encoder validation, and the
all-or-nothing publication of a run.
"""
from __future__ import annotations

import importlib
import os
import shlex
import shutil
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
recorder = importlib.import_module("record_showcase_media")


def _typed_argv(lines: list[str]) -> list[str]:
    """Parse displayed terminal lines back into the argv a shell would run."""
    assert lines[0].startswith("$ ")
    text = "\n".join(lines)[2:].replace(" \\\n", " ")
    return shlex.split(text)


# ---------------------------------------------------------------------------
# shell_display
# ---------------------------------------------------------------------------


def test_shell_display_keeps_a_short_command_on_one_line():
    argv = ["scripts/okf-loom", "wait", "docs-bundle", "--for", "comment", "--timeout", "60"]
    assert recorder.shell_display(argv) == [
        "$ scripts/okf-loom wait docs-bundle --for comment --timeout 60"]


def test_shell_display_quotes_wraps_and_round_trips_through_a_shell():
    argv = ["scripts/okf-loom", "replace-text", "--bundle", "docs-bundle",
            "--id", recorder.COMMENT_PAGE, "--actor", recorder.ACTOR,
            "--old", recorder.EDIT_OLD, "--new", recorder.EDIT_NEW,
            "--note", "it's quoted"]
    lines = recorder.shell_display(argv, width=72)

    assert _typed_argv(lines) == argv
    assert lines[0].startswith("$ scripts/okf-loom replace-text")
    assert shlex.quote("it's quoted") in lines[-1]
    for line in lines:
        # An option is never separated from its value by a line break.
        assert not line.removesuffix(" \\").split(" ")[-1].startswith("--"), line
    continued = [line for line in lines if line.endswith(" \\")]
    assert len(continued) >= 2, "a 72-column terminal must wrap this command"
    assert all(len(line) <= 72 for line in continued)
    assert all(line.startswith("    ") for line in lines[1:] if line.startswith(" "))
    # The newline inside --new continues on its own physical line.
    assert any(line.startswith(recorder.EDIT_NEW.split("\n", 1)[1]) for line in lines)


def test_every_displayed_comment_loop_command_fits_the_compact_terminal():
    comment_id = "01K5Z8Q6W2N4R7T9V3X5Z7B9D1"  # a real ULID is 26 characters
    commands = [
        ["wait", "docs-bundle", "--for", "comment", "--timeout", "60"],
        ["comment-claim", "docs-bundle", comment_id, "--actor", recorder.ACTOR],
        ["replace-text", "--bundle", "docs-bundle", "--id", recorder.COMMENT_PAGE,
         "--actor", recorder.ACTOR, "--old", recorder.EDIT_OLD, "--new", recorder.EDIT_NEW],
        ["comment-resolve", "docs-bundle", comment_id, "--actor", recorder.ACTOR,
         "--summary", recorder.RESOLVE_SUMMARY, "--reply", recorder.RESOLVE_REPLY],
    ]
    for args in commands:
        lines = recorder.shell_display(["scripts/okf-loom", *args])
        assert _typed_argv(lines) == ["scripts/okf-loom", *args]
        assert all(len(line) <= recorder._TERMINAL_WIDTH for line in lines), lines
    # The edit leaves the anchored sentence itself untouched.
    assert recorder.EDIT_OLD not in recorder.COMMENT_SENTENCE
    assert recorder.COMMENT_SENTENCE not in recorder.EDIT_NEW


def test_wrap_output_breaks_long_lines_at_spaces_and_never_clips():
    long = "  \u2713 replace text on tutorials/live_studio_basics (replaced 1 occurrence(s))"
    wrapped = recorder.wrap_output(long, width=40)
    assert all(len(line) <= 40 for line in wrapped)
    assert wrapped[0].startswith("  \u2713 replace")
    assert all(line.startswith("    ") for line in wrapped[1:])
    assert " ".join(part.strip() for part in wrapped) == long.strip()
    assert recorder.wrap_output("short", width=40) == ["short"]
    unbroken = "x" * 90
    assert "".join(part.strip() for part in recorder.wrap_output(unbroken, width=40)) == unbroken


# ---------------------------------------------------------------------------
# concat_script
# ---------------------------------------------------------------------------


def test_concat_script_gives_each_frame_its_real_duration_and_repeats_the_last():
    script = recorder.concat_script([(1.0, "a.jpg"), (1.5, "b.jpg"), (1.25, "c.jpg")], tail=0.1)
    assert script.splitlines() == [
        "ffconcat version 1.0",
        "file 'a.jpg'", "duration 0.250000",
        "file 'c.jpg'", "duration 0.250000",
        "file 'b.jpg'", "duration 0.100000",
        "file 'b.jpg'",
    ]


def test_concat_script_never_emits_a_zero_duration():
    script = recorder.concat_script([(2.0, "a.jpg"), (2.0, "b.jpg")], tail=0.1)
    assert "duration 0.001000" in script.splitlines()


def test_concat_script_refuses_an_empty_recording():
    with pytest.raises(ValueError, match="no frames"):
        recorder.concat_script([], tail=0.1)


# ---------------------------------------------------------------------------
# privacy screen
# ---------------------------------------------------------------------------


def test_private_text_screen_catches_paths_host_and_tunnels(monkeypatch):
    monkeypatch.setenv("HOME", "/home/reviewer")
    monkeypatch.setattr(recorder.socket, "gethostname", lambda: "build-box")
    patterns = recorder.private_text_patterns(("/tmp/okf-showcase-x1",))

    found = recorder.find_private_text(
        f"cwd {recorder.REPO_ROOT}/docs-bundle on build-box via https://a-b.trycloudflare.com "
        "scratch /tmp/okf-showcase-x1/docs-bundle home /home/reviewer/notes", patterns)

    for leak in (str(recorder.REPO_ROOT), "build-box", "trycloudflare.com",
                 "/tmp/okf-showcase-x1", "/home/reviewer"):
        assert leak in found


def test_private_text_screen_passes_the_public_terminal_text(monkeypatch):
    monkeypatch.setenv("HOME", "/home/reviewer")
    monkeypatch.setattr(recorder.socket, "gethostname", lambda: "build-box")
    patterns = recorder.private_text_patterns()
    assert recorder.find_private_text(
        "$ scripts/okf-loom wait docs-bundle --for comment --timeout 60\n"
        "claimed 01ABC (by docs-agent) on build-boxes and my-build-box", patterns) == []


# ---------------------------------------------------------------------------
# condensed_comment
# ---------------------------------------------------------------------------


def test_condensed_comment_keeps_the_reader_facing_keys_and_marks_the_rest():
    raw = ("waiting for a comment...\n"
           '{"body": "Tell readers they can comment here too.", "id": "01ABC", '
           '"state": "open", "anchor": {"quote": "This page exists to be looked at."}}\n')
    assert recorder.condensed_comment(raw, width=200) == [
        '{"body": "Tell readers they can comment here too.", "id": "01ABC", "state": "open", \u2026}']
    assert recorder.condensed_comment(raw, width=40) == [
        '{"body": "Tell readers they can comment here too.",',
        ' "id": "01ABC", "state": "open", \u2026}',
    ]


def test_condensed_comment_without_extra_keys_has_no_ellipsis_and_keeps_unicode():
    assert recorder.condensed_comment('{"id": "x", "body": "caf\u00e9", "state": "open"}') == [
        '{"id": "x", "body": "caf\u00e9", "state": "open"}']


# ---------------------------------------------------------------------------
# clip contract and encoder validation
# ---------------------------------------------------------------------------


def test_clip_contract_matches_the_readme_media_budget():
    windows = {name: (spec.min_seconds, spec.max_seconds, spec.gif_budget)
               for name, spec in recorder.CLIPS.items()}
    assert windows == {
        "comment-loop": (25, 35, 3 * recorder.MB),
        "appearance": (15, 25, 2 * recorder.MB),
        "graph": (15, 25, 2 * recorder.MB),
        "search": (10, 15, 2 * recorder.MB),
    }
    assert recorder.MP4_SIZE == (1280, 720)
    assert recorder.MP4_BUDGET == int(2.5 * recorder.MB)
    assert recorder.GIF_WIDTH == 960 and recorder.GIF_FPS == 10


needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not on PATH")


def _testsrc_mp4(path: Path, *, faststart: bool) -> None:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=1",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if faststart:
        args += ["-movflags", "+faststart"]
    subprocess.run([*args, str(path)], check=True, capture_output=True, timeout=60)


@needs_ffmpeg
def test_validate_accepts_a_conforming_mp4_and_names_every_problem(tmp_path):
    good = tmp_path / "good.mp4"
    _testsrc_mp4(good, faststart=True)
    report = recorder.validate(good, recorder.ClipSpec("t", recorder.direct_search, 0.5, 2, recorder.MB))
    assert (report["width"], report["height"]) == (1280, 720)
    assert report["seconds"] == pytest.approx(1.0, abs=0.1)

    late = tmp_path / "late.mp4"
    _testsrc_mp4(late, faststart=False)
    with pytest.raises(RuntimeError) as caught:
        recorder.validate(late, recorder.ClipSpec("t", recorder.direct_search, 10, 15, recorder.MB))
    assert "duration 1.00s outside 10-15s" in str(caught.value)
    assert "+faststart" in str(caught.value)


# ---------------------------------------------------------------------------
# all-or-nothing publication
# ---------------------------------------------------------------------------


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _offline_run(monkeypatch, fail_on: str | None = None) -> _FakeBrowser:
    """Replace the browser and the per-clip recording; keep main's publication."""
    class _NoPlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_exc):
            return False

    monkeypatch.setitem(sys.modules, "playwright.sync_api", types.SimpleNamespace(
        Error=type("PlaywrightError", (Exception,), {}), sync_playwright=_NoPlaywright))
    monkeypatch.setattr(recorder.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    browser = _FakeBrowser()
    monkeypatch.setattr(recorder, "launch_chromium", lambda _p, _chrome=None: browser)

    def record(_browser, spec, stage, _work, _diagnostics):
        if spec.name == fail_on:
            raise RuntimeError(f"{spec.name}: graph lens 'Themes' did not become the checked lens")
        reports = []
        for suffix in ("mp4", "gif"):
            path = stage / f"{spec.name}.{suffix}"
            path.write_bytes(f"new {path.name}".encode())
            os.chmod(path, 0o600)
            reports.append({"file": path.name, "seconds": 12.0, "bytes": 12,
                            "width": 1280, "height": 720, "encoding": "test"})
        return reports

    monkeypatch.setattr(recorder, "record_clip", record)
    return browser


def test_a_failing_clip_publishes_nothing_and_keeps_the_previous_media(tmp_path, monkeypatch, capsys):
    out = tmp_path / "media"
    out.mkdir()
    (out / "search.mp4").write_bytes(b"previous")
    browser = _offline_run(monkeypatch, fail_on="graph")

    assert recorder.main(["--only", "search", "--only", "graph", "--out", str(out)]) == 1

    assert "graph lens 'Themes'" in capsys.readouterr().err
    assert browser.closed
    assert (out / "search.mp4").read_bytes() == b"previous"
    assert sorted(p.name for p in out.iterdir()) == ["search.mp4"]


def test_a_clean_run_publishes_every_file_world_readable(tmp_path, monkeypatch, capsys):
    out = tmp_path / "media"
    _offline_run(monkeypatch)

    assert recorder.main(["--only", "search", "--out", str(out)]) == 0

    assert "# wrote 2 files" in capsys.readouterr().out
    assert sorted(p.name for p in out.iterdir()) == ["search.gif", "search.mp4"]
    assert (out / "search.mp4").read_bytes() == b"new search.mp4"
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o644 for p in out.iterdir())


def test_help_lists_the_clips_and_options():
    result = subprocess.run([sys.executable, str(SCRIPTS / "record_showcase_media.py"), "--help"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    for text in ("--only", "--out", "--chrome", "--keep-frames",
                 "comment-loop", "appearance", "graph", "search"):
        assert text in result.stdout
