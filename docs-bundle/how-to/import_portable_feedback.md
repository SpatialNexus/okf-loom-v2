---
type: How-to
title: Import a portable feedback note
description: Preview a reader's offline portable feedback note in the local Studio
  and import it as an ordinary open page comment, with its disclosures, blocked states,
  and retry retention.
resource: /how-to/import_portable_feedback.md
tags:
- feedback
- comments
- studio
- import
generated:
  by: okf-loom/2.0.0
  at: '2026-09-22T21:04:01Z'
---

# Import a portable feedback note

This guide takes a feedback note file that a reader wrote on an exported copy
of your bundle and turns it into an ordinary comment in your local Studio.

Readers write notes on a static or single-file export built with
`--include-portable-feedback` (see [Build a static site](/how-to/build_static_site.md)),
then send you the downloaded `.okf-feedback.json` file however they like.

# Before you start

Import works only in the local Studio on your own machine.

* Start it with `scripts/okf-loom serve <bundle> --no-open` and open the local
  address it prints. Do not add `--no-edit`: a read-only Studio can't import.
* Stop any tunnel first. While a tunnel or public ingress is attached, the page
  says import is available only on the local Studio, and the import routes
  refuse with `local_only`.

# Steps

1. Open the Studio and choose **Import feedback note** under **Feedback notes**.
   The command palette has the same command, or go straight to
   `/__feedback-import`.
2. Choose the note file. Choosing only checks the note; nothing is imported yet.
3. Read the preview: the page, the note text, any quote (headed **Quote in the
   note (unverified)**), and any name and time.
   The name and time are marked **unverified**.
4. Read the two notices. Origin is unauthenticated: anyone could have written or
   edited the file, and identical copies of a page can't be told apart.
   Importing creates an open comment recorded as you, visible to every reader
   and agent of the bundle, and it may trigger agent work. It is a page comment:
   any quote is shown with it but is not highlighted or pinned to exact text.
5. Select **Import as comment**. Then use **Show the comment** to open it in the
   Comments panel, or open its page.

The imported comment carries an **Imported portable note** chip with the
unverified claims beside it, and any quote is labelled **Quote in the note
(unverified) — not highlighted on the page**. Otherwise it is an ordinary page
comment: agents claim,
resolve, and archive it like any other.

# When a note can't be imported

| Message | What it means | What to do |
|---|---|---|
| The page has changed since this note was written | The page's bytes differ from the version the note was written about. | Ask for a note written on a current export, or add the feedback yourself. |
| The page no longer exists | The target page was removed or renamed. | Add the feedback where it now belongs. |
| Already imported | This exact note is already a comment. | Select **Show the comment**. |
| A different note with this ID was already imported | The ID was used for different text, or it expired from the duplicate check. | Ask the reader to write a new note. |
| The page or session changed; preview again | The preview is out of date. It lasts 10 minutes, and a page update, a live-update gap, or a restore from browser history also ends it. | Select **Preview again**. |
| Import not confirmed | The Studio didn't confirm the import, so it may or may not have happened. | Select **Check again**. It reports **Already imported** if the import went through. |

# Retries and retention

Retries return the original result for up to 24 hours (7 days as a compact
record). After that, the same note is refused rather than imported twice.

# See also

* [HTTP server routes](/reference/http_routes.md) — the page and the preview and confirm routes.
* [Spec §12.2](/reference/spec.md#122-portable-page-level-feedback-import-local-operator-only) — note format, check order, and the provenance marker.
* [Build a static site](/how-to/build_static_site.md) — exporting with the offline note composer.
* [Archive and resolve comment threads](/how-to/archive_threads.md) — what happens to the comment afterwards.
