# Contributing

Thank you for your interest in okf-loom. This repository is an Apache-2.0
loadable skill checkout for okf-loom 2.0.0, which implements OKF 0.2. Report
bugs and propose changes through issues and pull requests on
[SpatialNexus/okf-loom-v2](https://github.com/SpatialNexus/okf-loom-v2),
against `main`. Report security issues privately instead; see
[SECURITY.md](SECURITY.md).

## License expectations

By contributing, you agree that your contribution may be distributed under the
Apache License, Version 2.0. Do not submit code, docs, generated artifacts, or
assets that cannot be redistributed under Apache-2.0 unless the maintainers
explicitly approve a separate license notice.

## Development model

- Use the checked-in `scripts/okf-loom` helper from a source checkout.
- Do not add PyPI or package publishing as the primary path. The repository
  layout is the distribution unit.
- Keep upstream OKF 0.2 as the current wire-format contract, preserving
  documented legacy reading behavior.
- Treat the product version (2.0.0), the OKF format version (0.2), and API and
  storage schemas separately.
- Record user-visible changes in [CHANGELOG.md](CHANGELOG.md).
- Edit `docs-bundle/` concepts with the `scripts/okf-loom` mutators
  (`write-concept`, `update-section`, `replace-text`, …) and validate the
  bundle after documentation changes.

## Before you submit

Run the focused checks that match your work. For documentation-only changes,
at minimum run:

```bash
scripts/okf-loom validate docs-bundle --strict
```

For runtime changes, add or update tests and run the relevant pytest
selection (see [Developer tests](#developer-tests)). If you touch the viewer,
templates, samples or browser-facing behavior, also run the affected browser
tests. The GitHub Actions workflows run only when started by hand (see
[CI workflows](#ci-workflows)), so pull requests are not checked
automatically.

## Developer tests

Using okf-loom needs no install. For development on Linux, use Python **3.11
or 3.12**, Git, and **Node 22.22.1**. Node runs the portable retrieval parity
and JavaScript syntax and logic checks; there is no npm installation.

### Core tests

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
export PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
python -m pytest -q -p no:cacheprovider -m "not browser and not integration"
```

This is the core run. It collects without importing Playwright and covers local
HTTP, persistence, capture validators, retrieval and proposal runtime
identity. Only real browser tests and the optional external upstream-bundle
integration tests are deselected. Fixtures that serve shipped bundles use
disposable copies, and no stored evidence is regenerated. On restricted hosts,
set `TMPDIR` to a writable private directory (`mkdir -m 700`, then `chmod 700`
if inherited ACLs require it); tests and capture helpers honour it rather than
requiring `/tmp`. Proposal fixtures create their own 0700 session authorities,
so a shared/setgid checkout does not require stripping its ACLs. Tests needing
POSIX multiprocessing semaphores probe `multiprocessing.Lock()` and skip only
on `PermissionError`; the capture publisher race uses semaphore-free pipes.
`requirements-dev.txt` pins the
complete Linux dependency set; there is no `uv sync` or editable-install step.

### Built-in YAML fallback

okf-loom uses PyYAML when it is installed and a conservative built-in parser
otherwise. Exercise the fallback explicitly:

```bash
OKF_LOOM_FORCE_BUILTIN_YAML=1 python -m pytest -q -p no:cacheprovider \
  tests/test_yaml_fallback.py tests/test_bootstrap_import.py
scripts/okf-loom validate docs-bundle --strict
```

### Browser tests

Provision the optional browser tools separately:

```bash
python -m pip install -r requirements-browser.txt
python -m playwright install --with-deps chromium
OKF_REQUIRE_BROWSER=1 python -m pytest -q -p no:cacheprovider -m browser \
  tests/test_document_outline_browser.py tests/test_a11y_reflow_browser.py
```

Select other `browser`-marked modules explicitly for the surface you changed;
some need CDN access, or headed Chrome for real browser zoom.

- With `OKF_REQUIRE_BROWSER=1`, a missing Playwright or browser fails the run.
  Without it, they may skip. Broken imports, browser launch errors and product
  startup failures are always errors.
- `OKF_CHROME` (or `AIC_PLAYWRIGHT_CHROME_PATH`) selects a specific browser
  executable, and a bad path fails. Otherwise the fixtures prefer
  Playwright-managed Chromium, then an installed Chrome or Chromium.
- The legacy `OKF_BROWSER_TEST_OPT_OUT=1` opt-out still works, but conflicts
  with `OKF_REQUIRE_BROWSER=1`.
- Chrome's Unix socket path is limited to 107 bytes. Before collection, conftest
  relocates an overlong, shared or setgid effective `TMPDIR` to a short private
  directory under writable `/tmp` first, then the resolved `.shared-cache` or
  the virtualenv's parent. Effective temp directories and fallback candidates
  inside this checkout's work tree are rejected (including an in-repo `.venv`
  parent); an external cache symlink remains a valid fallback. A short private
  external `TMPDIR` is kept as-is, including on hosts that deny `/tmp`.
  If none fits, it fails early with a request for a short writable external `TMPDIR`.
  The directory is removed after the test session; do not use a symlink to a
  long path as a workaround (Chrome resolves it).
- The shared server fixture prints startup logs on failure and cleans up with
  a bounded terminate-then-kill.
- Use marker selection for core runs. A test that starts Playwright or
  Chromium through `tests/browser_support.py` without the `browser` marker
  fails with an error that names it; `tests/test_browser_marker_guard.py`
  proves this without starting a browser.

### Browser and accessibility coverage limits

Automated browser checks run on Chromium only, driven by Playwright 1.58.0.

- `tests/test_a11y_reflow_browser.py` checks reflow at 200% and 400% with
  CSS-viewport equivalents (720×450 at DPR 2, and 320×256 at DPR 4). These
  reproduce the layout viewport that WCAG 1.4.10 is defined in, but they are
  not browser-owned zoom.
- Real page zoom is checked only by opt-in proofs in headed Chrome:
  `OKF_TEST_HEADED_ZOOM=1` runs the `HEADED_ZOOM_TESTS` in
  `tests/test_ui_reflow_nojs_remediation.py` with
  `OKF_CAPTURE_REAL_BROWSER_ZOOM=1`, pressing Ctrl+plus through `xdotool`, so
  they need X11 `DISPLAY` and `xdotool` (see `tests/browser_zoom.py`). With no
  display and no `xvfb-run`, only these seven proofs skip, with an explicit
  environment reason (even in the required-browser lane). If Xvfb is installed,
  prefer `OKF_TEST_HEADED_ZOOM=1 xvfb-run -a python -m pytest -q
  tests/test_ui_reflow_nojs_remediation.py`. Wayland alone cannot drive the
  X11 keyboard driver. They are not part of CI.
- Text-only zoom and custom browser font sizes are not tested.
- Accessibility semantics are checked against Playwright's ARIA snapshot and
  Chromium's accessibility tree. No screen reader has been run, so spoken
  wording, live-region timing and virtual-cursor behavior are unverified.
- Firefox, Safari/WebKit and mobile browsers are not tested, and no automated
  rule engine such as axe-core is used.

Treat these checks as regression guards, not a claim of accessibility
conformance.

### Release evidence

Historical release verification is an explicit, selected-source operation; see
[ordinary regression versus release evidence](docs-bundle/reference/spec.md#ordinary-regression-versus-selected-release-evidence).
Passing core or browser tests does not promote stored evidence or certify
release readiness.

`python scripts/benchmark_gold_standard.py --help` describes the separate
release benchmark harness. It defaults to 100 and 1,000 concepts; sizes above
1,000 need `--large-scales`, static builds need `--static-build`, and large
static builds need both. These are maintainer tools, not `scripts/okf-loom`
commands.

## CI workflows

All three workflows in `.github/workflows/` run only on demand, from
**Actions → <workflow> → Run workflow**:

- **Developer tests** (`tests.yml`): the core run on Python 3.11 and 3.12, the
  built-in YAML lane, strict docs validation and a clean-tree check. Tick
  **browser** to add the document outline and accessibility reflow browser
  tests.
- **Browser smoke** (`browser-smoke.yml`): five short Chromium journeys with
  `OKF_REQUIRE_BROWSER=1`, one existing test each, as separately named steps:
  shell and capability truth; search to a document and Back restoring the
  query; comment, reply, agent mutation and resolution; portable feedback
  import; and a document patch with the outline lifecycle. A failing step
  names its journey and the pytest node id that reproduces it locally.
- **Deploy docs to Pages** (`pages.yml`): builds `docs-bundle/` as a static
  site and deploys it to GitHub Pages.

Neither test workflow is a full browser, accessibility or release gate; the
full browser suite stays manual.

## Regenerating the README media

The README's screenshots and clips live in [`docs/media/`](docs/media/) and
are reproducible from a live Studio. Use the browser setup above.

**Screenshots and the social card:**

```bash
PYTHONPATH=scripts python scripts/capture_readme_media.py
```

It serves a snapshot of the docs bundle's git-tracked files, so local comment
history and activity never appear in the media (`--in-place` serves the bundle
as-is instead). It seeds no appearance settings, so the stills show the
shipped defaults, and it fails on any browser console, page or network error.
It also captures the two phone stills (`phone-top.png`, `phone-scrolled.png`)
on a 390×844 touch viewport, swiping the page so the chrome scrolls away, and
composes `docs/media/social-preview.png`, the 1280×640 repository card. Options: `--bundle`, `--out-dir`, `--chrome`.

**Clips (MP4 and GIF):**

```bash
python scripts/record_showcase_media.py
python scripts/record_showcase_media.py --only search --only graph
```

It records `comment-loop`, `appearance`, `graph` and `search`, each as an
H.264 1280×720 MP4 and a looping 960 px GIF. Every clip runs against its own
scratch copy of the docs bundle's git-tracked files, so recording never
changes the checkout. The pointer, captions, key hints and terminal panel are
a recording-only overlay, never part of the product. It needs `ffmpeg` and
`ffprobe` (with libx264 and the GIF encoder) on `PATH`; Pillow is not needed.
Options: `--out`, `--chrome`, `--keep-frames DIR`.

Both scripts find a browser the same way: `--chrome`, then `OKF_CHROME`, then
`AIC_PLAYWRIGHT_CHROME_PATH`, known system Chrome paths, the Chrome channel,
and finally Playwright-managed Chromium.

Only `docs/media/` is committed. The proof helpers
(`capture_viewer_proof.py`, `capture_annotations_v2.py`,
`capture_signal_controls.py`) write to the git-ignored `captures/` directory,
so proof screenshots never enter the published tree:

```bash
PYTHONPATH=scripts python scripts/capture_viewer_proof.py
```

The capture helpers wait on the viewer's own readiness signal rather than a
fixed sleep. One 30-second budget covers navigation, readiness, painting and
publishing the screenshot, and outputs are replaced atomically: a failed or
late capture keeps the previous file.

## Security-sensitive changes

Be careful with live Studio routes, CSRF token handling, Origin/Host
allow-lists, `--public`, `--tunnel` and its `--allow-tunnel-comment` and
`--allow-tunnel-edit` opt-ins, portable feedback import, and active viewer or
plugin code. Document any exposure-boundary change in
[SECURITY.md](SECURITY.md), the README, and the relevant `docs-bundle/`
reference page.
