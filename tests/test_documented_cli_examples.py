"""Execute corrected public CLI recipes against disposable bundles only."""
from pathlib import Path
import shlex
import subprocess

from okf_loom.model import Bundle


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "okf-loom"


def run_cli(*args):
    result = subprocess.run(
        [str(CLI), *(str(arg) for arg in args)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def make_bundle(tmp_path):
    bundle = tmp_path / "bundle"
    run_cli("bootstrap", bundle)
    run_cli("write-concept", "--bundle", bundle, "--id", "reference/spec",
            "--type", "Reference", "--title", "Specification")
    return bundle


def snapshot(bundle):
    return {path.relative_to(bundle): path.read_bytes()
            for path in bundle.rglob("*") if path.is_file()}


def test_spec_authoring_recipe_in_sequence(tmp_path):
    bundle = make_bundle(tmp_path)
    fragment = tmp_path / "frag.md"
    spec = (ROOT / "docs-bundle/reference/spec.md").read_text()
    recipes = [block.split("```", 1)[0] for block in spec.split("```bash\n")[1:]
               if block.startswith("scripts/okf-loom write-concept")]
    assert len(recipes) == 1
    # Run the published block, including its fragment preparation and preceding
    # mutations. Only bundle/output paths change; no real docs are mutated.
    for line in recipes[0].strip().splitlines():
        argv = shlex.split(line)
        if argv[0] == "printf":
            assert argv[-2:] == [">", "/tmp/frag.md"]
            result = subprocess.run(argv[:-2], capture_output=True, check=True)
            fragment.write_bytes(result.stdout)
        else:
            assert argv[0] == "scripts/okf-loom"
            run_cli(*(bundle if arg == "docs-bundle" else
                      fragment if arg == "/tmp/frag.md" else arg
                      for arg in argv[1:]))
    concept = Bundle.load(bundle).concepts[("reference", "new_topic")]
    assert concept.frontmatter["tags"] == ["reference"]
    assert {"label": "okf-loom", "kind": "Product"} in concept.frontmatter["entities"]
    assert "## Usage" in concept.body
    assert "exact new text" in concept.body
    assert "exact old text" not in concept.body
    assert "current spec" in concept.body


def test_repair_indexes_defaults_to_preview_without_writes(tmp_path):
    bundle = make_bundle(tmp_path)
    before = snapshot(bundle)
    result = run_cli("repair", bundle, "--indexes")
    assert "mechanical fix(es)" in result.stdout
    assert "create_index" in result.stdout
    assert snapshot(bundle) == before


def test_single_file_build_uses_output_directory_and_render_uses_filename(tmp_path):
    bundle = make_bundle(tmp_path)
    before = snapshot(bundle)
    out = tmp_path / "dist"
    run_cli("build", bundle, "--target", "single-file", "--out", out)
    assert (out / "index.html").is_file()
    assert "<html" in (out / "index.html").read_text().lower()
    named = tmp_path / "viz.html"
    run_cli("render", bundle, "--out", named)
    assert named.is_file()
    assert "<html" in named.read_text().lower()
    assert snapshot(bundle) == before
