"""Module entry used by the checked-in ``scripts/okf-loom`` helper."""
from .cli import main
import sys

raise SystemExit(main(sys.argv[1:]))
