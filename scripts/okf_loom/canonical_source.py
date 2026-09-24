"""Canonical, stable, no-follow byte admission for one bundle document.

This is the single reader behind every server-side "canonical source" use
(annotation recapture, ``/__data/page``, comment target proof) and the
portable-feedback exact-byte revision. It returns the exact on-disk bytes;
callers that need text decode them strictly themselves.

Admission rules (unchanged from the original ``server._canonical_document_source``):

* the canonical document path must be visible under the bundle's
  include/exclude/gitignore policy (else ``FileNotFoundError``);
* the bundle root is the loader-pinned trusted root, and its ``(st_dev,
  st_ino)`` identity must still match;
* every path component is opened relative to its parent descriptor with
  ``O_NOFOLLOW`` (a symlink anywhere fails closed);
* the final object must be a regular file no larger than ``max_bytes``;
* the file's identity/size/mtime must be identical before and after the
  read, so a concurrent writer cannot hand out torn bytes.

Every refusal other than a genuinely missing/invisible document is an
``OSError`` that callers map to "unavailable".
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

# Default cap. ``server.MAX_RAW_RESPONSE_BYTES`` passes its (patchable) value
# explicitly so existing callers keep observing that module-level knob.
MAX_CANONICAL_SOURCE_BYTES: int = 16 * 1024 * 1024


def read_canonical_document_bytes(bundle: Any, document: str, *,
                                  max_bytes: int = MAX_CANONICAL_SOURCE_BYTES) -> bytes:
    """Return the exact, stably-read bytes of one canonical bundle document."""
    from .config import OkfConfig
    from .documents import document_path
    from .ignore import is_path_visible

    relative = Path(document_path(document))
    cfg = OkfConfig.load(bundle.root).bundle
    if not is_path_visible(bundle.root, str(relative), exclude=cfg.exclude,
                           include=cfg.include,
                           respect_gitignore=cfg.respect_gitignore):
        raise FileNotFoundError('document is not visible')
    root = getattr(bundle, "_trusted_root", None)
    expected_root = getattr(bundle, "_trusted_root_identity", None)
    if not isinstance(root, Path) or expected_root is None:
        raise OSError("trusted bundle root unavailable")
    candidate = root / relative
    try:
        candidate.resolve(strict=True).relative_to(root)
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise OSError("canonical concept escaped bundle containment") from exc
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    root_fd = os.open(root, os.O_RDONLY | directory | nofollow)
    opened: list[int] = [root_fd]
    try:
        root_stat = os.fstat(root_fd)
        if (root_stat.st_dev, root_stat.st_ino) != expected_root:
            raise OSError("bundle root identity changed")
        current_fd = root_fd
        parts = list(relative.parts)
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | directory | nofollow, dir_fd=current_fd)
            opened.append(fd)
            current_fd = fd
        file_fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=current_fd)
        opened.append(file_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("canonical concept is not a regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise OSError("canonical concept exceeds size limit")
            chunks.append(chunk)
        after = os.fstat(file_fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise OSError("canonical concept changed during read")
        return b"".join(chunks)
    finally:
        for fd in reversed(opened):
            os.close(fd)
