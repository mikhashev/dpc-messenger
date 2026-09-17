"""Run output lives outside the working tree, and one path serves both users.

Moved there on 2026-09-01 after an audit of all 1 633 files: a disk serial in
45 of them, three peers' node ids, 86 scraped third-party addresses, and in
four the previews of our own chat that a run had read back through
`read_session_archive`.

The second assertion is the one worth having. `run_gaia_eval` writes reports
and copies traces; it also plants the canary and scans for reachable gold —
all against `RESULTS_DIR`. `campaign` decides the filenames from its own
`RESULTS`. Let those two drift and the guards inspect an empty directory and
report it clean, which is this project's oldest failure shape: an instrument
reading zero because nothing is connected to it.
"""
import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[3] / "eval"
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(EVAL / "gaia"))
sys.path.insert(0, str(EVAL / "kv"))

from _harness.results_root import results_root  # noqa: E402
import run_gaia_eval as gaia  # noqa: E402
import campaign  # noqa: E402
import ab_key_quant as kv  # noqa: E402


def test_the_results_root_is_outside_the_repository(monkeypatch):
    # The default is what this asserts, so the override has to be off: a developer with
    # DPC_EVAL_RESULTS exported got a red that said nothing about their code (Fable 5).
    monkeypatch.delenv("DPC_EVAL_RESULTS", raising=False)
    root = results_root("gaia")

    assert REPO not in root.parents, (
        f"{root} is inside the working tree; the traces carry the machine that "
        f"made them and a stray `git add` is all it takes"
    )
    assert root == Path.home() / ".dpc" / "eval-results" / "gaia"


def test_the_paths_the_two_modules_actually_hold_are_outside_the_repository():
    """Asserted on the module attributes, not on the helper.

    Equality between the two (below) survives them drifting back together, and
    the helper being right says nothing about what the modules assigned.
    """
    for name, path in (("run_gaia_eval.RESULTS_DIR", gaia.RESULTS_DIR),
                       ("campaign.RESULTS", campaign.RESULTS),
                       ("ab_key_quant.RESULTS", kv.RESULTS)):
        assert REPO not in path.parents, f"{name} is {path}, inside the working tree"


def test_each_benchmark_gets_its_own_directory():
    """`loop` and `retrieval` take their output path from the caller, so they
    are not asserted here — there is no constant to hold wrong."""
    roots = {b: results_root(b) for b in ("gaia", "kv", "loop", "retrieval")}

    assert len(set(roots.values())) == 4
    assert kv.RESULTS == roots["kv"]


def test_the_runner_and_the_campaign_name_the_same_directory():
    assert gaia.RESULTS_DIR == campaign.RESULTS == results_root("gaia")


def test_an_env_var_redirects_the_whole_root(monkeypatch, tmp_path):
    monkeypatch.setenv("DPC_EVAL_RESULTS", str(tmp_path))

    assert results_root("gaia") == tmp_path / "gaia"
    assert results_root("loop") == tmp_path / "loop"
