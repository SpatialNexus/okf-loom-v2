"""Named Proposal Review proof that legacy direct mutation remains unchanged."""

from okf_loom.model import Bundle
from okf_loom.paths import concept_id_to_str
from test_live import _post, live_server
from conftest import private_session_root
from pathlib import Path
import json
import multiprocessing
import shutil
import subprocess
import pytest


def _hold_direct_lock(bundle: str, ready, release) -> None:
    from okf_loom.io_utils import bundle_transaction_lock
    with bundle_transaction_lock(Path(bundle), "exclusive", timeout_seconds=15):
        ready.set(); release.wait(10)


def _direct_fixture(root: Path) -> None:
    (root / "tables").mkdir(parents=True)
    (root / "index.md").write_text(
        "---\ntype: Index\ntitle: Root\n---\n\n# Root\n\n<!-- okf:generated:start -->\n<!-- okf:generated:end -->\n")
    (root / "log.md").write_text("# Log\n")
    (root / "tables/orders.md").write_text(
        "---\ntype: Table\ntitle: Orders\n---\n\n# Orders\n\nReplace me.\n\n## Details\n\nOld details.\n")
    (root / "tables/customers.md").write_text(
        "---\ntype: Table\ntitle: Customers\n---\n\n# Customers\n")


def _visible_bytes(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()
            and ".okf-loom" not in path.parts and not path.name.endswith((".okf-lock", ".lock"))}


def _normalized(result: subprocess.CompletedProcess[str], *paths: Path) -> tuple[int, str, str]:
    stdout, stderr = result.stdout, result.stderr
    for path in paths:
        stdout = stdout.replace(str(path), "<PATH>")
        stderr = stderr.replace(str(path), "<PATH>")
    return result.returncode, stdout, stderr


def test_sec_09_direct_mode(live_server) -> None:
    base, _studio, server = live_server
    bundle = Bundle.load(server.bundle.root)
    concept = next(iter(bundle.concepts))
    concept_id = concept_id_to_str(concept)
    path = Path(server.bundle.root).joinpath(*concept_id.split("/")).with_suffix(".md")
    before = path.read_bytes()
    code, result = _post(base, "/__apply", {
        "kind": "add_tag", "target": concept_id,
        "args": {"tag": "proposal-direct-compat"}, "group_id": "SEC09",
    }, server.csrf_token)
    assert code == 200 and result["ok"] is True and result["applied"] is True
    assert path.read_bytes() != before
    undo_code, undo = _post(base, "/__undo", {"group_id": "SEC09"}, server.csrf_token)
    assert undo_code == 200 and undo["ok"] is True
    assert path.read_bytes() == before


def test_mig_07_cli(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"; concept = bundle / "tables/orders.md"
    concept.parent.mkdir(parents=True)
    concept.write_bytes(b"---\ntype: Table\n---\n\nOrders\n")
    command = [str(Path(__file__).parents[1] / "scripts/okf-loom"),
        "set-frontmatter", "--bundle", str(bundle), "--id", "tables/orders",
        "--key", "status", "--value", "reviewed", "--format", "json"]
    result = subprocess.run(command, check=False, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert b"status: reviewed" in concept.read_bytes()
    assert not (bundle / ".okf-loom/session/proposal-review-v1").exists()


@pytest.mark.usefixtures("multiprocessing_semaphores")
def test_mig_11_cli_direct_compatibility(tmp_path: Path) -> None:
    """Every direct writer family is byte/CLI/lock compatible after activation."""
    executable = str(Path(__file__).parents[1] / "scripts/okf-loom")
    cases = {
        "write-concept": lambda bundle, aux: [executable, "write-concept", "--bundle", str(bundle),
            "--id", "notes/new", "--type", "Note", "--title", "New", "--body", "Body",
            "--resource", "/notes/new.md", "--timestamp", "2026-07-15T00:00:00Z"],
        "set-frontmatter": lambda bundle, aux: [executable, "set-frontmatter", "--bundle", str(bundle),
            "--id", "tables/orders", "--key", "status", "--value", "reviewed"],
        "update-section": lambda bundle, aux: [executable, "update-section", "--bundle", str(bundle),
            "--id", "tables/orders", "--heading", "## Details", "--body", "New details."],
        "replace-text": lambda bundle, aux: [executable, "replace-text", "--bundle", str(bundle),
            "--id", "tables/orders", "--old", "Replace me.", "--new", "Replaced."],
        "link-add": lambda bundle, aux: [executable, "link-add", "--bundle", str(bundle),
            "--source", "tables/orders", "--target", "tables/customers", "--label", "Customers"],
        "entity-add": lambda bundle, aux: [executable, "entity-add", "--bundle", str(bundle),
            "--id", "tables/orders", "--label", "Order", "--kind", "business_entity"],
        "update": lambda bundle, aux: [executable, "update", str(bundle), "--plan", str(aux)],
        "index": lambda bundle, aux: [executable, "index", str(bundle)],
        "log": lambda bundle, aux: [executable, "log", str(bundle), "--append", "compat entry",
            "--date", "2026-07-15"],
        "repair": lambda bundle, aux: [executable, "repair", str(bundle), "--all", "--apply"],
        "upgrade": lambda bundle, aux: [executable, "upgrade", "--bundle", str(bundle), "--apply"],
    }
    from okf_loom.proposal_review.activation import activate
    for family, command in cases.items():
        case = tmp_path / family; baseline = case / "baseline"; activated = case / "activated"
        _direct_fixture(baseline); shutil.copytree(baseline, activated)
        private_session_root(activated)
        activation = activate(activated); activation.close()
        auxiliaries = []
        for side in (baseline, activated):
            plan = case / f"{side.name}-plan.json"
            plan.write_text(json.dumps({"plan_kind": "update", "description": "compat",
                "ops": [{"kind": "add_tag", "target": "tables/orders",
                         "args": {"tag": "compat"}}]}))
            auxiliaries.append(plan)
        left = subprocess.run(command(baseline, auxiliaries[0]), text=True, capture_output=True)
        right = subprocess.run(command(activated, auxiliaries[1]), text=True, capture_output=True)
        assert _normalized(left, baseline, auxiliaries[0]) == _normalized(
            right, activated, auxiliaries[1]), family
        assert left.returncode == 0, (family, left.stderr)
        assert _visible_bytes(baseline) == _visible_bytes(activated), family
        assert not (baseline / ".okf-loom/session/proposal-review-v1").exists()
        assert (activated / ".okf-loom/session/proposal-review-v1").exists()

    # The common direct-writer lock remains blocking before and after backend activation.
    lock_case = tmp_path / "locking"; baseline = lock_case / "baseline"; activated = lock_case / "activated"
    _direct_fixture(baseline); shutil.copytree(baseline, activated)
    private_session_root(activated)
    activation = activate(activated); activation.close()
    context = multiprocessing.get_context("spawn")
    lock_results = []
    for side in (baseline, activated):
        ready, release = context.Event(), context.Event()
        holder = context.Process(target=_hold_direct_lock, args=(str(side), ready, release))
        holder.start(); assert ready.wait(5)
        command = [executable, "set-frontmatter", "--bundle", str(side), "--id", "tables/orders",
                   "--key", "locked", "--value", "yes"]
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                subprocess.run(command, text=True, capture_output=True, timeout=.25)
        finally:
            release.set(); holder.join(10)
        assert holder.exitcode == 0
        completed = subprocess.run(command, text=True, capture_output=True)
        assert completed.returncode == 0
        lock_results.append(_normalized(completed, side))
    assert lock_results[0] == lock_results[1]
    assert _visible_bytes(baseline) == _visible_bytes(activated)

    for unsupported in ("apply", "undo"):
        result = subprocess.run([executable, "proposal", unsupported, str(baseline)],
                                text=True, capture_output=True)
        assert result.returncode == 2 and result.stdout == "" and result.stderr
