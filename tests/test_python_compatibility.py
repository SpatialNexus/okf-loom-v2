"""Keep checkout scripts parseable by the supported Python 3.11 baseline."""
import ast

from conftest import TOOLKIT_ROOT


def test_scripts_parse_with_python311_grammar() -> None:
    for path in sorted((TOOLKIT_ROOT / "scripts").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path),
                         feature_version=(3, 11))
        compile(tree, str(path), "exec")
