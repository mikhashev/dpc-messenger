"""Follow-up 13, items 7 and 8 (Zcode's review of follow-up 12): Whisper's estimate for a short clip and
its `torch.compile`.

The live GPU estimate for a 4.2 s dictation read 14.0 s (12.8 s of it the fresh-process load) against
1.0 s of transcription and 2.2 s of load; the CPU twin read 26 s cold and 15 s warm against 13 s and
10.7 s for 15.7 s and 13.9 s clips. A clip up to `_SHORT_CLIP_MAX_AUDIO_S` is now `start_s + audio /
ratio` (one pair for a warm call, one for the first call after a load), the load of a model whose
imports are already paid is its own figure, and clips beyond keep the band figures.

`torch.compile` was tried at every load and always failed on the live backend ("cannot import name
'min_cut_rematerialization_partition' from 'functorch.compile'"), after promising a 4.5x speed-up. It is
tried only where the import chain it needs works, and a skip is said once per process, with no promise.

No model is loaded: the provider's figures and two module helpers are read.
"""

import logging
import sys
import types

import pytest

from dpc_client_core.managers.card_lease import seconds_from_ratio
from dpc_client_core.providers import whisper_provider as wp
from dpc_client_core.providers.whisper_provider import _START_FIGURES, LocalWhisperProvider

CUDA, CPU = _START_FIGURES["cuda"], _START_FIGURES["cpu"]


@pytest.fixture(autouse=True)
def clean_process(monkeypatch):
    """No weights loaded in this process, and the imports not paid unless a test says so."""
    monkeypatch.setattr(wp, "_LOADED_WEIGHTS", set())
    monkeypatch.setattr(wp, "imports_are_paid", lambda: False)
    monkeypatch.setattr(wp, "_COMPILE_BLOCKER", None)
    monkeypatch.setattr(wp, "_COMPILE_BLOCKER_KNOWN", False)
    monkeypatch.setattr(wp, "_COMPILE_SKIP_SAID", False)


def _provider(device):
    return LocalWhisperProvider("w", {"type": "local_whisper", "device": device})


def _paid(monkeypatch):
    monkeypatch.setattr(wp, "imports_are_paid", lambda: True)


# -- item 8: the short clip -----------------------------------------------------------------------


def test_a_4_second_dictation_on_a_cold_gpu_model_is_about_what_the_live_run_took(monkeypatch):
    """Live 2026-11-25 09:58: 2.2 s of load and 1.0 s of transcription, 3.2 s in all."""
    _paid(monkeypatch)
    estimate = _provider("cuda").expected_seconds(4.2, device="cuda", loaded=False)
    assert 2.9 <= estimate <= 3.8, estimate
    # The estimate that read 14.0 s: the fresh process's load, when nothing is paid.
    monkeypatch.setattr(wp, "imports_are_paid", lambda: False)
    fresh = _provider("cuda").expected_seconds(4.2, device="cuda", loaded=False)
    assert fresh == pytest.approx(CUDA["load_first_s"] + 0.7 + 4.2 / 15.3)


def test_a_warm_short_clip_has_a_fixed_start_and_a_cold_one_costs_more():
    p = _provider("cuda")
    warm = p._short_clip_seconds(4.2, "cuda", first_call=False)
    cold = p._short_clip_seconds(4.2, "cuda", first_call=True)
    assert warm == pytest.approx(0.3 + 4.2 / 26.8)
    assert cold == pytest.approx(0.7 + 4.2 / 15.3)
    assert cold > warm > seconds_from_ratio(4.2, CUDA["warm"]["<=60"]), "the old estimate had no start"
    # A call that is not the first one after a load reads the warm pair.
    p._calls_since_load = 3
    assert p.expected_seconds(4.2, device="cuda", loaded=True) == pytest.approx(warm)


def test_the_cpu_twin_reads_the_live_clips_within_a_second_and_never_under(monkeypatch):
    """13.9 s took 10.7 s warm; 15.7 s took 13 s cold with a 1.0 s load (live 2026-11-25)."""
    p = _provider("cpu")
    warm = p.expected_seconds(13.9, device="cpu", loaded=True)
    assert 10.7 <= warm <= 11.7, warm
    _paid(monkeypatch)
    cold = p.expected_seconds(15.7, device="cpu", loaded=False)
    assert 13.0 <= cold <= 14.0, cold
    assert warm < 15.3 and cold < 26.0, "the estimates that were read: 15 s warm and 26 s cold"


def test_clips_over_the_short_limit_keep_the_band_figures():
    p = _provider("cuda")
    over = wp._SHORT_CLIP_MAX_AUDIO_S + 0.5
    assert p._short_clip_seconds(over, "cuda", True) is None
    assert p.expected_seconds(30, device="cuda", loaded=True) == pytest.approx(
        seconds_from_ratio(30, CUDA["warm"]["<=60"]) + CUDA["penalty_s"]["<=60"])
    p._calls_since_load = 2
    assert p.expected_seconds(30, device="cuda", loaded=True) == pytest.approx(
        seconds_from_ratio(30, CUDA["warm"]["<=60"]))


def test_the_gpus_first_call_meets_the_band_figure_where_the_short_model_hands_over():
    p = _provider("cuda")
    at_limit = p._short_clip_seconds(wp._SHORT_CLIP_MAX_AUDIO_S, "cuda", True)
    band = seconds_from_ratio(wp._SHORT_CLIP_MAX_AUDIO_S, CUDA["warm"]["<=60"]) + CUDA["penalty_s"]["<=60"]
    assert abs(at_limit - band) < 0.1, (at_limit, band)


def test_a_device_with_no_figures_and_an_unknown_length_stay_unknown():
    p = _provider("mps")
    assert p.expected_seconds(4.2, device="mps", loaded=True) is None
    assert p._short_clip_seconds(None, "cuda", True) is None
    assert p._short_clip_seconds(0, "cuda", True) is None


def test_a_measured_short_call_replaces_the_pair_and_the_next_call_reads_it():
    p = _provider("cuda")
    p._note_run("cuda", 4.2, 0.8, first=True)
    assert p._short_clip_seconds(4.2, "cuda", True) == pytest.approx(0.8)
    assert p._short_clip_seconds(2.1, "cuda", True) == pytest.approx(0.4), "its effective ratio, 5.25x"
    assert p._short_clip_seconds(4.2, "cuda", False) == pytest.approx(0.3 + 4.2 / 26.8), "warm is a pair of its own"
    # Another provider of the same model is handed the figure.
    new = _provider("cuda")
    p._carry_figures_to(new, fresh_load=False)
    assert new._short_clip_seconds(4.2, "cuda", True) == pytest.approx(0.8)


# -- the load of a model whose imports are paid -----------------------------------------------------


def test_the_imports_are_paid_only_when_torch_and_transformers_pipelines_are_in_the_process(monkeypatch):
    monkeypatch.undo()  # the real `imports_are_paid`, not the fixture's
    fake = types.ModuleType("x")
    monkeypatch.setitem(sys.modules, "torch", fake)
    monkeypatch.delitem(sys.modules, "transformers.pipelines", raising=False)
    assert wp.imports_are_paid() is False
    monkeypatch.setitem(sys.modules, "transformers.pipelines", fake)
    assert wp.imports_are_paid() is True
    monkeypatch.delitem(sys.modules, "torch")
    assert wp.imports_are_paid() is False


def test_a_first_load_with_paid_imports_reads_and_records_its_own_figure(monkeypatch):
    p = _provider("cuda")
    assert p.speed_figures("cuda")["load_s"] == CUDA["load_first_s"]
    _paid(monkeypatch)
    assert p.speed_figures("cuda")["load_s"] == 2.2
    assert _provider("cpu").speed_figures("cpu")["load_s"] == 1.1
    # Recorded under its own name, never as the fresh process's first load.
    p._note_load("cuda", 2.4, imports_paid=True)
    assert p._measured["cuda"]["load_imported_s"] == 2.4 and "load_first_s" not in p._measured["cuda"]
    # The same model loaded again on the device is a reload whatever the imports.
    assert p.speed_figures("cuda")["load_s"] == CUDA["load_reload_s"]


def test_a_load_without_paid_imports_is_still_recorded_as_the_first_load():
    p = _provider("cuda")
    p._note_load("cuda", 12.0)
    assert p._measured["cuda"]["load_first_s"] == 12.0


# -- item 7: torch.compile -------------------------------------------------------------------------


def test_a_broken_import_chain_blocks_torch_compile_and_is_probed_once(monkeypatch):
    import importlib
    calls = []

    def broken(name, *a, **k):
        calls.append(name)
        raise ImportError("cannot import name 'min_cut_rematerialization_partition' from 'functorch.compile'")

    monkeypatch.setattr(importlib, "import_module", broken)
    reason = wp.torch_compile_blocker()
    assert "min_cut_rematerialization_partition" in reason and reason.startswith("ImportError")
    assert wp.torch_compile_blocker() == reason
    assert calls == ["torch._dynamo.backends.debugging"], "probed once per process"


def test_a_working_import_chain_does_not_block(monkeypatch):
    import importlib
    monkeypatch.setattr(importlib, "import_module", lambda name, *a, **k: types.ModuleType(name))
    assert wp.torch_compile_blocker() is None


def test_the_skip_is_said_once_with_no_speed_up_promised(caplog):
    with caplog.at_level(logging.INFO, logger=wp.logger.name):
        wp._say_compile_skipped_once("ImportError: no functorch")
        wp._say_compile_skipped_once("ImportError: no functorch")
        wp._say_compile_skipped_once("another reason")
    lines = [r.getMessage() for r in caplog.records if "torch.compile" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "ImportError: no functorch" in lines[0] and "unaffected" in lines[0]
    assert "4.5" not in lines[0] and "speedup" not in lines[0].lower().replace("speed-up", "")


def test_the_load_no_longer_promises_a_speed_up():
    import inspect
    source = inspect.getsource(wp)
    assert "4.5x speedup" not in source
    assert "just slower" not in source
