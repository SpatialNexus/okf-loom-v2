# Contributing

Thank you for your interest in okf-loom.
This repository is an Apache-2.0 loadable skill checkout for okf-loom 2.0.0 / OKF 0.2.

## License expectations

By contributing, you agree that your contribution may be distributed under the Apache License, Version 2.0.
Do not submit code, docs, generated artifacts, or assets that cannot be redistributed under Apache-2.0 unless the maintainers explicitly approve a separate license notice.

## Development model

- Use the checked-in `scripts/okf-loom` helper from a source checkout.
- Do not add PyPI or package-publishing workflow as the primary path.
- Keep upstream OKF 0.2 as the current wire-format contract, preserving documented legacy reading behavior.
- Treat product version 2.0.0, OKF format version 0.2, and API/storage schemas separately.
- Use `ui-analysis` as the supported local checkout; no fork publication or private-reporting destination is declared.
- Validate docs bundles after documentation changes.

## Public-readiness checklist

Before submitting a change, run the focused checks that match your work.
For documentation-only changes, at minimum run:

```bash
scripts/okf-loom validate docs-bundle --strict
```

For runtime changes, add or update tests and run the relevant pytest selection.
See [Developer tests](README.md#developer-tests) for the pinned Linux Python
3.11/3.12 core setup and the separately provisioned browser lane. Pull
requests that touch runtime, viewer, template, sample or test paths also run
the automatic five-journey Chromium smoke workflow; the full browser suite
stays manual.

## Security-sensitive changes

Be careful with live studio routes, CSRF token handling, Origin/Host allow-lists, `--public`, `--tunnel`, and active viewer/plugin code.
Document any exposure-boundary changes in `SECURITY.md`, the README, and the relevant docs-bundle reference page.
