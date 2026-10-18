"""The eval harness takes a `strata` alias (ADR-044), so Strata and the 27B are
compared on one harness, and it refuses by name when the other engine holds the card.

Before this, the GAIA preflight failed every alias that was not `llamacpp_server`
and the loop eval skipped its card check for any other type, so a Strata run
started beside the service's llama-server child without a word
(THE-EVAL-PREFLIGHT-REFUSES-ANY-LOCAL-TYPE-BUT-LLAMACPP-SERVER).
"""

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "eval" / "loop"))
sys.path.insert(0, str(REPO_ROOT / "eval" / "gaia"))
sys.path.insert(0, str(REPO_ROOT / "eval"))

import preflight  # noqa: E402
import run_loop_eval as R  # noqa: E402
from _harness import local_engine, provenance  # noqa: E402
from dpc_client_core.dpc_agent.provider_facts import LOCAL_TYPES  # noqa: E402
from dpc_client_core.firewall import LOCAL_PROVIDER_TYPES  # noqa: E402
from dpc_client_core.managers import llama_server_supervisor as base_mod  # noqa: E402

from tests.test_strata_supervisor import _install  # noqa: E402

LLAMA_ON_CARD = [("llama-server", "llama-server.exe (pid 32848)")]
STRATA_ON_CARD = [("strata", "strata.exe (pid 4711)")]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(base_mod, "DPC_HOME", tmp_path / "dpc-home")


def _strata_entry(tmp_path: Path, **overrides):
    models = tmp_path / "models"
    (models / "pack").mkdir(parents=True)
    native = models / "flash-next-00001-of-00002.gguf"
    native.write_bytes(b"GGUF")
    root = _install(tmp_path, vision=None,
                    args=["--pack", str(models / "pack"), "--native", str(native),
                          "--max-context", "261120"])
    entry = {
        "type": "strata", "alias": "flash", "model": "qwen3.8-flash-next-iq3_xxs",
        "strata_install_dir": str(root), "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240, "context_window": 200000,
        "tested_version": "0.9.1", "tested_version_date": "2026-10-18",
    }
    entry.update(overrides)
    return entry


def _statuses(checks):
    return {name: status for status, name, _ in checks}


# --- which types the harness takes ---------------------------------------------


def test_the_supervised_types_come_from_the_clients_own_sets():
    supervised = local_engine.supervised_types()
    assert supervised == {"llamacpp_server", "strata"}
    assert supervised <= LOCAL_PROVIDER_TYPES and supervised <= LOCAL_TYPES
    assert local_engine.engine_of("strata") == "strata"
    assert local_engine.engine_of("llamacpp_server") == "llama-server"
    assert local_engine.engine_of("ollama") is None


# --- the GAIA preflight --------------------------------------------------------


def test_the_preflight_accepts_a_strata_alias_and_reads_its_engine(tmp_path):
    entry = _strata_entry(tmp_path)
    checks = preflight.engine_checks("flash", entry, ["flash"], tmp_path / "providers.json",
                                     ["xhigh", "low"], tmp_path / "results")
    status = _statuses(checks)
    assert not [c for c in checks if c[0] == "FAIL"], checks
    assert status["alias"] == "OK" and status["strata"] == "OK"
    assert status["native_gguf"] == "OK" and status["pack_dir"] == "OK"
    effort = next(d for s, n, d in checks if n == "effort")
    assert "Strata base config" in effort and "medium" in effort
    assert "llama-server" not in status and "gguf" not in status


def test_a_strata_alias_whose_install_is_gone_is_refused_by_name(tmp_path):
    entry = _strata_entry(tmp_path, strata_install_dir=str(tmp_path / "nowhere"))
    checks = preflight.engine_checks("flash", entry, ["flash"], tmp_path / "p.json",
                                     ["xhigh"], tmp_path / "results")
    detail = next(d for s, n, d in checks if n == "strata" and s == "FAIL")
    assert "nowhere" in detail


def test_a_type_that_starts_no_engine_is_refused_naming_both_engines(tmp_path):
    checks = preflight.engine_checks("o", {"type": "ollama", "alias": "o"}, ["o"],
                                     tmp_path / "p.json", ["xhigh"], tmp_path)
    assert checks[0][0] == "FAIL"
    assert "llamacpp_server" in checks[0][2] and "strata" in checks[0][2]


def test_a_llama_server_alias_is_still_read_by_its_gguf(tmp_path, monkeypatch):
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF")
    monkeypatch.setattr(provenance, "llama_server_binary", lambda entry: {
        "binary": "llama-server", "binary_is_the_pin": True, "pinned_tag": "b1",
        "version": ["version: 1"]})
    checks = preflight.engine_checks(
        "q", {"type": "llamacpp_server", "alias": "q", "gguf_path": str(gguf)}, ["q"],
        tmp_path / "p.json", ["xhigh"], tmp_path / "results")
    status = _statuses(checks)
    assert status["alias"] == "OK" and status["llama-server"] == "OK" and status["gguf"] == "OK"


@pytest.mark.parametrize("ptype, holders", [
    ("strata", LLAMA_ON_CARD),
    ("llamacpp_server", STRATA_ON_CARD),
    ("strata", STRATA_ON_CARD),
])
def test_the_other_engine_on_the_card_fails_the_preflight_by_name(ptype, holders):
    checks = preflight.card_checks({"type": ptype, "alias": "a"}, 26000, 30000,
                                   lambda: [], lambda: holders)
    held = [d for s, n, d in checks if n == "card holder" and s == "FAIL"]
    assert held and holders[0][1] in held[0]
    assert "cannot hold it together" in held[0]


def test_two_llama_server_children_are_a_vram_question_not_a_card_holder_one():
    checks = preflight.card_checks({"type": "llamacpp_server", "alias": "a"}, 26000, 9000,
                                   lambda: ["llama-server.exe (pid 1)"], lambda: LLAMA_ON_CARD)
    assert _statuses(checks) == {"vram": "WARN"}


# --- the loop eval -------------------------------------------------------------


def test_the_loop_eval_refuses_a_strata_run_beside_the_llama_server_child(monkeypatch):
    monkeypatch.setattr(local_engine, "engines_on_the_card", lambda: LLAMA_ON_CARD)
    monkeypatch.setattr(R, "_free_vram_mib", lambda: 30000)
    with pytest.raises(SystemExit) as refused:
        R._check_vram_or_refuse({"type": "strata", "alias": "flash"})
    assert "llama-server.exe (pid 32848)" in str(refused.value)


def test_the_loop_eval_checks_the_card_for_a_strata_run(monkeypatch):
    monkeypatch.setattr(local_engine, "engines_on_the_card", lambda: [])
    monkeypatch.setattr(R, "_free_vram_mib", lambda: 30000)
    assert R._check_vram_or_refuse({"type": "strata", "alias": "flash"}) == 30000
    monkeypatch.setattr(R, "_free_vram_mib", lambda: 9000)
    with pytest.raises(SystemExit):
        R._check_vram_or_refuse({"type": "strata", "alias": "flash"})


def test_the_loop_dry_run_reads_the_strata_start_and_refuses_a_missing_file(tmp_path, capsys):
    entry = _strata_entry(tmp_path)
    R._strata_dry_run(entry)
    out = capsys.readouterr().out
    assert "server_script" in out and "native_gguf" in out and "0.9.1" in out
    (tmp_path / "models" / "flash-next-00001-of-00002.gguf").unlink()
    with pytest.raises(SystemExit, match="native_gguf"):
        R._strata_dry_run(entry)


# --- the card, asked by name ---------------------------------------------------


def test_engines_are_named_from_windows_and_posix_paths():
    out = types.SimpleNamespace(stdout=(
        "14248, C:\\Windows\\explorer.exe\n"
        "32848, C:\\Users\\x\\.dpc\\bin\\llama.cpp\\b1\\llama-server.exe\n"
        "4711, D:\\Strata\\engine\\strata.exe\n"
        "4712, /opt/strata/engine/strata-vision\n"
        "45312, C:\\Python312\\python.exe\n"))
    found = local_engine.engines_on_the_card(run=lambda *a, **k: out)
    assert found == [("llama-server", "llama-server.exe (pid 32848)"),
                     ("strata", "strata.exe (pid 4711)"),
                     ("strata", "strata-vision (pid 4712)")]


def test_no_nvidia_smi_is_no_answer_rather_than_an_empty_card():
    def missing(*a, **k):
        raise FileNotFoundError("nvidia-smi")
    assert local_engine.engines_on_the_card(run=missing) is None
    assert local_engine.card_conflicts("strata", None) == []


# --- provenance ----------------------------------------------------------------


def test_a_strata_run_records_its_start_files_and_its_template_default(tmp_path):
    entry = _strata_entry(tmp_path)
    record = provenance.model_files(entry, tmp_path)
    assert record["model_files"]["native_gguf"]["size"] == 4
    assert record["model_files"]["pack_dir"]["kind"] == "folder"
    assert record["strata"]["tested_version"] == "0.9.1"
    effort = provenance.effort_record(entry)
    assert effort["value"] == "xhigh" and "Strata base config" in effort["source"]
