"""ADR-052, Confirmation test 28 (L11 point 0, the worst-case `need`; Ark #60 §5, #61 §2b, #18-3;
slice S3e): the fit counts every resident on the card, not only those in the contender's way, and
`need` is pinned in MiB.

*Who is in the way* decides who is asked to yield; *who is resident* decides the fit. A sum built
from the yield list would leave out the residents that do not exclude the contender and report a fit
where the card would run out. Three residents (a llama-server child with `predicted_vram_mib`, a busy
YuE2, a co-living second Whisper alias) are placed on the card by hand: the runtime never produces
that cell (every lease excludes every holder, and a supervisor excludes every lease), so this is the
arithmetic, as the record says. Figures marked derived are not measured (Forge #12, Zcode #22-5).
"""

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import _fit_now, _LeaseWaiter, l11_arithmetic
from dpc_client_core.managers.llama_server_supervisor import LlamaServerSupervisor
from dpc_client_core.managers.strata_supervisor import StrataSupervisor
from dpc_client_core.providers.whisper_provider import whisper_vram_mib

from tests._card_lease_helpers import music, whisper
from tests.test_strata_supervisor import _FakeProc

TOTAL = 32623
READING = {"free": 30000, "total": TOTAL, "reserve": 2048, "reserve_source": "the desktop reserve"}


def test_whisper_alone_needs_6495_mib_in_mib_not_bytes():
    """A fake revision-less turbo row of 1 622 443 339 B: `whisper_peak` 1547 + 2900 = 4447 at its worst
    band (61-120 s) and `need` 4447 + 2048 = 6495 MiB (derived, not measured)."""
    peak = whisper_vram_mib("openai/whisper-large-v3-turbo", 90.0)
    assert peak == 1547 + 2900 == 4447
    fit = l11_arithmetic(peak, [], reserve_mib=2048, free_mib=30000, total_mib=TOTAL)
    assert fit["need"] == 6495 and fit["outcome"] == "fits"
    assert fit["need"] < 10**6, "a need left in bytes (about 1.6e9) would fail here"


def test_three_residents_are_all_summed_and_the_yield_list_would_have_said_fits():
    residents = [("llama-server 'qwen27'", 20000, False), ("yue2.cpp", 5600, True),
                 ("Whisper (small)", 4447, True)]
    fit = l11_arithmetic(4447, residents, reserve_mib=2048, free_mib=31000, total_mib=TOTAL)
    assert fit["need"] == 4447 + 20000 + 5600 + 4447 + 2048 == 36542
    assert fit["outcome"] == "does_not_fit"
    in_the_way_only = 4447 + 5600 + 2048
    assert in_the_way_only <= TOTAL, "a sum of the yield list alone would have said fits"
    assert fit["growth_left"] == 5600 + 4447, "a supervisor allocates at its start and does not grow"


def _on_the_card(holder, state="claimed"):
    holder._proc = _FakeProc()
    holder.props = {"total_slots": 1}
    base_mod._CARD_CLAIMS[holder] = state


def test_the_module_reads_every_holder_on_the_card(monkeypatch):
    llama = LlamaServerSupervisor("qwen27", {"gguf_path": "D:/models/q.gguf"})
    llama.predicted_vram_mib = 20000
    _on_the_card(llama)
    yue2, other = music(), whisper("small", vram_mib=4447)
    for lease in (yue2.lease, other.lease):
        lease._grant("placed by hand")
    contender = whisper("turbo", vram_mib=4447)
    waiter = _LeaseWaiter(contender.lease, "dictation", None, True, None, vram_mib=4447)

    fit = _fit_now(waiter, READING)

    assert fit["need"] == 36542 and fit["outcome"] == "does_not_fit"
    base_mod._CARD_CLAIMS.clear()


def test_a_strata_child_as_a_resident_makes_it_unknown(tmp_path):
    from tests.test_strata_supervisor import _install

    strata = StrataSupervisor("flash", {"strata_install_dir": str(_install(tmp_path)),
                                        "strata_base_config": "strata-base.json",
                                        "conversation_cache_mib": 10240})
    _on_the_card(strata)
    assert strata.predicted_vram_mib is None
    contender = whisper("turbo", vram_mib=4447)
    waiter = _LeaseWaiter(contender.lease, "dictation", None, True, None, vram_mib=4447)

    fit = _fit_now(waiter, READING)

    assert fit["outcome"] == "unknown" and "strata 'flash' has no declared peak" in fit["why"]
    base_mod._CARD_CLAIMS.clear()


def test_the_reserve_is_the_resident_engines_own_and_never_a_sum(tmp_path):
    """One reserve per engine (Mike 2026-11-23): a resident Strata's reserve is its alias's
    `vram_reserve_mib`; with no engine on the card, the 2048 desktop reserve."""
    from tests.test_strata_supervisor import _install

    strata = StrataSupervisor("flash", {"strata_install_dir": str(_install(tmp_path)),
                                        "strata_base_config": "strata-base.json",
                                        "conversation_cache_mib": 10240, "vram_reserve_mib": 1152})
    assert card_lease._reserve_of([strata]) == (1152, "the alias's vram_reserve_mib, strata 'flash'")
    llama = LlamaServerSupervisor("qwen27", {"gguf_path": "D:/models/q.gguf"})
    assert card_lease._reserve_of([llama])[0] == 2048
    assert card_lease._reserve_of([strata, llama])[0] == 2048, "the larger of the two, not 3200"
    assert card_lease._reserve_of([])[0] == base_mod._DESKTOP_RESERVE_MIB == 2048


@pytest.mark.parametrize("free, total", [(None, TOTAL), (30000, None), (None, None)])
def test_no_reading_is_unknown_never_fits(free, total):
    fit = l11_arithmetic(4447, [("yue2.cpp", 5600, True)], reserve_mib=2048, free_mib=free, total_mib=total)
    assert fit["outcome"] == "unknown"


def test_a_need_above_a_known_total_does_not_fit_even_with_free_unread():
    """Zcode #88-6: the declared check needs only the total; an unread `free` cannot rescue a `need`
    the card is too small for, so the answer is `does_not_fit`, not `unknown`."""
    fit = l11_arithmetic(4447, [("yue2.cpp", 5600, True)], reserve_mib=2048, free_mib=None, total_mib=8192)
    assert fit["need"] == 4447 + 5600 + 2048 == 12095
    assert fit["outcome"] == "does_not_fit" and fit["why"] == "12095 MiB declared against the card's 8192 MiB"
    unread_total = l11_arithmetic(4447, [("yue2.cpp", 5600, True)], reserve_mib=2048, free_mib=None, total_mib=None)
    assert unread_total["outcome"] == "unknown", "without the total nothing is decided"
