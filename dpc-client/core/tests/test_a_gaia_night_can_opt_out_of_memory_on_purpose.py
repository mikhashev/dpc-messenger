"""Commit 7a19bad3 made the embedding model (BAAI/bge-m3) fail closed: not
cached, not downloaded, and memory_search / Active Recall degrade in silence.
A GAIA night that started that way without knowing it produced a number that
is not comparable with an earlier one, and the GPU time was already spent.

Reviewer's rule, owner-approved: preflight refuses when the model the agent
will use is not cached; --no-memory is the one recorded, honest way to opt
out of it on purpose, and it has to actually remove memory_search from the
agent's tool set rather than merely ask it not to use one.
"""

import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parents[3] / "eval"
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(EVAL / "gaia"))

import preflight  # noqa: E402
import campaign  # noqa: E402
import run_gaia_eval as gaia  # noqa: E402
from _harness import benchmark_tools  # noqa: E402
from dpc_client_core.providers import model_sizes  # noqa: E402


# --- preflight: the embedding model check, in isolation ---------------------
# `memory_check` is exercised directly rather than through `run_checks`, which
# also checks the HF token (a real network call) and the tool registry: this
# check has to stay testable without either.


def test_preflight_fails_when_the_embedding_model_is_not_cached(monkeypatch):
    monkeypatch.setattr(model_sizes, "is_model_cached", lambda name: False)
    monkeypatch.setattr(model_sizes, "model_size_bytes",
                        lambda name, revision=None: (2_293_331_663, "measured"))

    status, name, detail = preflight.memory_check(no_memory=False)

    assert status == "FAIL"
    assert name == "memory"
    assert "BAAI/bge-m3" in detail and "not cached" in detail
    assert "2.29 GB" in detail, "the measured size belongs in the refusal"
    # The fix travels with the refusal, not in a separate document.
    assert "SentenceTransformer" in detail or "DPC app" in detail
    assert "--no-memory" in detail


def test_preflight_passes_when_the_embedding_model_is_cached(monkeypatch):
    monkeypatch.setattr(model_sizes, "is_model_cached", lambda name: True)

    status, name, detail = preflight.memory_check(no_memory=False)

    assert status == "OK"
    assert name == "memory"
    assert "BAAI/bge-m3" in detail


def test_no_memory_turns_the_fail_into_a_skip_note_not_cached(monkeypatch):
    monkeypatch.setattr(model_sizes, "is_model_cached", lambda name: False)

    status, name, detail = preflight.memory_check(no_memory=True)

    assert status == "SKIP"
    assert status != "FAIL", "an explicit opt-out must not still refuse the run"
    assert "--no-memory" in detail
    assert "is not cached" in detail, "the note says the real state, not just the flag"


def test_no_memory_still_skips_when_the_model_happens_to_be_cached(monkeypatch):
    """--no-memory is a choice, not a fallback for an absent model."""
    monkeypatch.setattr(model_sizes, "is_model_cached", lambda name: True)

    status, name, detail = preflight.memory_check(no_memory=True)

    assert status == "SKIP"
    assert "is cached" in detail


def test_a_skip_does_not_stop_the_campaign():
    checks = [("OK", "alias", "x"), ("SKIP", "memory", "--no-memory: ..."), ("OK", "tools", "x")]
    assert preflight.all_ok(checks) is True


def test_a_fail_still_stops_the_campaign():
    checks = [("OK", "alias", "x"), ("FAIL", "memory", "not cached"), ("OK", "tools", "x")]
    assert preflight.all_ok(checks) is False


def test_print_checks_formats_the_skip_status_readably(capsys):
    preflight.print_checks([("SKIP", "memory", "--no-memory: agent memory search disabled")])
    out = capsys.readouterr().out
    assert "SKIP" in out and "memory" in out and "--no-memory" in out


# --- campaign.py: --no-memory reaches the alias, the preflight and the run --


def test_no_memory_flag_reaches_run_checks(monkeypatch, tmp_path, capsys):
    seen = {}

    def _fake_run_checks(alias, efforts, results_dir, gpu_needed, no_memory=False):
        seen["no_memory"] = no_memory
        return [("OK", "alias", "stub")]

    monkeypatch.setattr(campaign, "RESULTS", tmp_path)
    monkeypatch.setitem(sys.modules, "preflight", preflight)
    monkeypatch.setattr(preflight, "run_checks", _fake_run_checks)
    monkeypatch.setattr(sys, "argv", ["campaign.py", "--dry-run", "--no-memory"])

    code = campaign.main()

    assert code == 0
    assert seen["no_memory"] is True


def test_the_runner_command_passes_no_memory_through():
    cmd = campaign.runner_command(
        campaign.QUEUE[0], Path("r.json"),
        {"alias": "some alias", "no_memory": True},
    )
    assert "--no-memory" in cmd


def test_the_runner_command_omits_no_memory_by_default():
    cmd = campaign.runner_command(
        campaign.QUEUE[0], Path("r.json"), {"alias": "some alias"},
    )
    assert "--no-memory" not in cmd


# --- run_gaia_eval.py: the tool set the agent is actually built with --------


def test_memory_search_is_a_benchmark_tool_by_default():
    """The thing --no-memory has to remove has to be on the list to start with."""
    assert "memory_search" in benchmark_tools.BENCHMARK_TOOLS


def test_memory_search_is_excluded_from_the_tool_set_under_no_memory(tmp_path):
    allowed = benchmark_tools.BENCHMARK_TOOLS - {"memory_search"}
    firewall = gaia.benchmark_firewall(tmp_path, allowed=allowed)

    enabled = {n for n, on in firewall.get_agent_tools_map(gaia.BENCH_PROFILE).items() if on}

    assert "memory_search" not in enabled
    assert "search_web" in enabled, "only memory_search should have moved"


def test_memory_search_is_present_by_default(tmp_path):
    firewall = gaia.benchmark_firewall(tmp_path)
    enabled = {n for n, on in firewall.get_agent_tools_map(gaia.BENCH_PROFILE).items() if on}
    assert "memory_search" in enabled


def test_the_runner_recognizes_no_memory_on_the_command_line(monkeypatch):
    """`main()` builds its own argparse; this exercises that parser directly,
    without letting `main_async` (dataset, provider, agent) actually run.
    """
    captured = {}

    async def _fake_main_async(args):
        captured["no_memory"] = args.no_memory
        return 0

    monkeypatch.setattr(gaia, "main_async", _fake_main_async)
    monkeypatch.setattr(sys, "argv", ["run_gaia_eval.py", "--no-memory"])

    assert gaia.main() == 0
    assert captured["no_memory"] is True


def test_no_memory_defaults_off(monkeypatch):
    captured = {}

    async def _fake_main_async(args):
        captured["no_memory"] = args.no_memory
        return 0

    monkeypatch.setattr(gaia, "main_async", _fake_main_async)
    monkeypatch.setattr(sys, "argv", ["run_gaia_eval.py"])

    gaia.main()

    assert captured["no_memory"] is False


# --- provenance: every report says what memory it ran with -----------------


def test_provenance_records_memory_on_by_default():
    fields = gaia.memory_provenance_fields(no_memory=False, embedding_model="BAAI/bge-m3",
                                           cached=True)
    assert fields["memory_search"] == "on"
    assert fields["embedding_model"] == "BAAI/bge-m3"
    assert fields["embedding_model_cached"] is True


def test_provenance_records_memory_off_with_the_flag_named():
    fields = gaia.memory_provenance_fields(no_memory=True, embedding_model="BAAI/bge-m3",
                                           cached=False)
    assert fields["memory_search"] == "off (--no-memory)"
    assert fields["embedding_model_cached"] is False


def test_provenance_is_honest_about_active_recall_having_no_toggle():
    """The harness cannot turn Active Recall off directly (context.py calls it
    unconditionally); the record says that rather than implying a switch.
    """
    fields = gaia.memory_provenance_fields(no_memory=True, embedding_model="BAAI/bge-m3",
                                           cached=False)
    assert "no per-run toggle" in fields["active_recall"]
    assert "not cached" in fields["active_recall"]
