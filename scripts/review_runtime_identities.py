"""Print Proposal Review identity candidates; never install runtime authority."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from okf_loom.proposal_review.canonical import dumps_canonical
from okf_loom.proposal_runtime_identity import (
    OPERATION_RUNTIME_ID, SERIALIZER_ID, SOURCE_MANIFESTS, VALIDATOR_ID,
)


def manifest_digest(root: Path, paths: tuple[str, ...]) -> str:
    """Hash exact source bytes and paths in the reviewed manifest's array order."""
    entries = []
    for relative in paths:
        data = (root / relative).read_bytes()
        entries.append([relative, hashlib.sha256(data).hexdigest()])
    return hashlib.sha256(dumps_canonical(entries)).hexdigest()


def review_candidates(root: Path) -> dict[str, dict[str, str]]:
    """Derive candidates only, keeping the explicitly reviewed name/version."""
    current = {
        "validator": VALIDATOR_ID,
        "serializer": SERIALIZER_ID,
        "operation": OPERATION_RUNTIME_ID,
    }
    return {
        kind: {
            "current": current[kind],
            "candidate": current[kind].rsplit(":", 1)[0] + ":" + manifest_digest(root, paths),
        }
        for kind, paths in SOURCE_MANIFESTS.items()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root", type=Path, default=Path(__file__).resolve().parents[1],
        help="read source bytes here using this helper's manifests (default: this checkout)",
    )
    args = parser.parse_args(argv)
    try:
        candidates = review_candidates(args.source_root)
    except OSError as error:
        parser.error(f"cannot read identity source: {error}")
    print(json.dumps(candidates, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
