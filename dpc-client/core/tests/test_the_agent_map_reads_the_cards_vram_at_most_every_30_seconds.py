"""ADR-046 point 8: `list_processes` answers with the card's `vram_free_mib` and
`vram_total_mib`, and the Agent map's 5-second poll does not run nvidia-smi each time.

What is asserted: both values present beside `free_commit`; both None without an
NVIDIA signal; one probe per 30 s window (each probe is a registered
`device_probe` entry), the total kept once read; a refused probe answers None and
is not cached.

Nothing here runs nvidia-smi or touches `~/.dpc`: the two probe helpers are
replaced and the cache is reset around every test.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dpc_client_core.managers import commit_memory as cm
from dpc_client_core.managers import llama_server_supervisor as lss


@pytest.fixture(autouse=True)
def fresh_cache():
    lss._vram_reading.update(at=None, free=None, total=None)
    yield
    lss._vram_reading.update(at=None, free=None, total=None)


class _Card:
    """The two helpers, counting their calls."""

    def __init__(self, free=1234, total=32607):
        self.free, self.total = free, total
        self.free_calls = self.total_calls = 0

    def install(self, monkeypatch):
        def free(while_stopping=False):
            self.free_calls += 1
            return self.free

        def total():
            self.total_calls += 1
            return self.total

        monkeypatch.setattr(lss, "_free_vram_mib", free)
        monkeypatch.setattr(lss, "_total_vram_mib", total)
        return self


@pytest.mark.asyncio
async def test_the_answer_carries_the_cards_free_and_total_vram_beside_free_commit(monkeypatch):
    from dpc_client_core.service import CoreService

    _Card().install(monkeypatch)
    monkeypatch.setattr(cm, "read_free_commit_or_physical", lambda: None)
    out = await CoreService.list_processes(SimpleNamespace())
    assert out["status"] == "success"
    assert (out["vram_free_mib"], out["vram_total_mib"]) == (1234, 32607)
    assert "free_commit" in out


@pytest.mark.asyncio
async def test_the_answer_has_null_vram_without_an_nvidia_signal(monkeypatch):
    from dpc_client_core.service import CoreService

    _Card(free=None, total=None).install(monkeypatch)
    monkeypatch.setattr(cm, "read_free_commit_or_physical", lambda: None)
    out = await CoreService.list_processes(SimpleNamespace())
    assert out["status"] == "success"
    assert out["vram_free_mib"] is None and out["vram_total_mib"] is None


def test_a_second_read_inside_the_window_runs_no_probe_and_one_after_it_does(monkeypatch):
    card = _Card().install(monkeypatch)
    assert lss.read_vram_mib(now=100.0) == (1234, 32607)
    card.free = 900
    assert lss.read_vram_mib(now=105.0) == (1234, 32607), "the 5-second poll reuses the reading"
    assert lss.read_vram_mib(now=129.9) == (1234, 32607)
    assert (card.free_calls, card.total_calls) == (1, 1)
    assert lss.read_vram_mib(now=130.0) == (900, 32607), "30 s later the free figure is read again"
    assert (card.free_calls, card.total_calls) == (2, 1), "the total is the card's and is read once"


def test_a_total_that_was_unreadable_is_asked_again_with_the_next_window(monkeypatch):
    card = _Card(free=None, total=None).install(monkeypatch)
    assert lss.read_vram_mib(now=0.0) == (None, None)
    assert lss.read_vram_mib(now=10.0) == (None, None)
    assert (card.free_calls, card.total_calls) == (1, 1), "a missing signal is cached for the window too"
    card.free, card.total = 500, 8192
    assert lss.read_vram_mib(now=31.0) == (500, 8192)


def test_a_refused_probe_answers_none_and_is_not_cached(monkeypatch):
    card = _Card().install(monkeypatch)

    def refused(while_stopping=False):
        raise RuntimeError("the registry is closed")

    monkeypatch.setattr(lss, "_free_vram_mib", refused)
    assert lss.read_vram_mib(now=0.0) == (None, None)
    monkeypatch.setattr(lss, "_free_vram_mib", lambda while_stopping=False: 777)
    assert lss.read_vram_mib(now=1.0) == (777, 32607), "the failure left no reading behind"
    assert card.total_calls == 1
