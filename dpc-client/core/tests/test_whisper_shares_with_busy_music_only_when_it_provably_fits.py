"""ADR-052, Confirmation test 18 (decision 6, L11; Zcode #52, Ark #53; slice S3e): Whisper shares the
card with a busy music job only when it provably fits.

The one shared cell: a lease that `shares_with` a busy holder (Whisper with a music lease) is granted
beside it at once when the declared peaks fit the card's total (`need = peak + every resident's peak
+ reserve`) and the live free-VRAM reading does not veto (`free >= peak + growth_left`, the music
peak whole in v1). Otherwise it stands in the lease line, neither beside nor refused (a person's door);
a peer or a background call that does not fit is refused at once by the rules that follow. No other
pair shares by measurement. The reading is faked (`card_lease._READ_THE_CARD`), so the tests run on
Windows and Linux CI with no card.
"""

import asyncio
import logging

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import LeaseRefusedForPeer, LeaseSkipped, LeaseWaitedTooLong
from dpc_client_core.managers.llama_server_supervisor import LlamaServerSupervisor, serving_a_peer
from dpc_client_core.music.yue2 import Yue2Server

from tests._card_lease_helpers import (  # noqa: F401
    BOUND, embedder, fast, hold_open, holding, music, nothing_left, until, waiting, whisper,
)
from tests.test_strata_supervisor import _FakeProc

pytestmark = pytest.mark.usefixtures("fast")

TOTAL = 32623  # this machine's card
YUE2_PEAK = Yue2Server.VRAM_ESTIMATE_MIB
PEAK = 4447  # Whisper turbo's worst band: 1547 weights + 2900 (derived, not measured)


def card(monkeypatch, free, total=TOTAL):
    """What the card reads (free, total) for L11, counting the reads."""
    reads = []

    def read():
        reads.append((free, total))
        return free, total

    monkeypatch.setattr(card_lease, "_READ_THE_CARD", read)
    return reads


def sharing_whisper(alias="turbo", **kw):
    client = whisper(alias, vram_mib=PEAK, **kw)
    client.lease.shares_with = lambda holder: holder.kind == "music"
    return client


async def busy(client, what="music job J"):
    over = asyncio.Event()
    task = asyncio.create_task(hold_open(client.lease, what, over))
    await until(lambda: client.lease.granted)
    return over, task


async def done(over, task):
    over.set()
    await asyncio.wait_for(task, BOUND)


def rows():
    return {row["alias"]: row for row in base_mod.card_holders()}


@pytest.mark.asyncio
async def test_fits_is_granted_beside_at_once_with_both_rows_and_one_line(monkeypatch, caplog):
    card(monkeypatch, free=30000)
    m = music()
    over, task = await busy(m)
    w = sharing_whisper()
    with caplog.at_level(logging.INFO, logger=card_lease.__name__):
        async with w.lease.use("dictation", wait_s=BOUND):
            assert set(holding()) == {"music:yue2", "whisper:turbo"}
            assert rows()["whisper:turbo"]["beside"] == "music:yue2"
            assert rows()["music:yue2"]["beside"] is None
            assert waiting() == []
    lines = [r.getMessage() for r in caplog.records if "loaded beside" in r.getMessage()]
    # need = 4447 + yue2's peak + 2048 (no engine on the card: the desktop reserve)
    assert lines == [f"card: Whisper (turbo) loaded beside yue2.cpp (fits: {4447 + YUE2_PEAK + 2048} of 32623 MiB declared, "
                     "30000 MiB free; reserve 2048 MiB, the 2048 MiB desktop reserve: no local engine "
                     "on the card)"]
    assert m.yields == [], "the busy music job is never asked"
    await done(over, task)
    assert w.lease.beside == "music:yue2", "the job's grant stays warm with no use"
    m.lease.release("test")
    assert w.lease.beside is None, "the shared holder gone, nothing is beside"
    w.lease.release("test")
    await nothing_left()


@pytest.mark.asyncio
async def test_a_declared_sum_over_total_waits_and_a_live_reading_never_turns_it_into_yes(monkeypatch, caplog):
    # free says room; the declared 4447 + yue2's peak + 2048 > 12000 says no
    card(monkeypatch, free=32000, total=12000)
    assert PEAK + YUE2_PEAK + 2048 > 12000
    m = music()
    over, task = await busy(m)
    w = sharing_whisper()
    with caplog.at_level(logging.INFO, logger=card_lease.__name__):
        with pytest.raises(LeaseWaitedTooLong):
            await asyncio.wait_for(w.lease.acquire("dictation", wait_s=0.1), BOUND)
    assert any("L11 says does_not_fit" in r.getMessage() and f"{PEAK + YUE2_PEAK + 2048} MiB declared against the card's 12000"
               in r.getMessage() for r in caplog.records)
    assert not w.lease.granted
    await done(over, task)
    await nothing_left()


@pytest.mark.asyncio
async def test_the_live_veto_counts_the_music_peak_whole(monkeypatch, caplog):
    card(monkeypatch, free=PEAK + YUE2_PEAK - 1)  # one MiB short of its own peak and the job's whole peak
    m = music()
    over, task = await busy(m)
    w = sharing_whisper()
    with caplog.at_level(logging.INFO, logger=card_lease.__name__):
        with pytest.raises(LeaseWaitedTooLong):
            await asyncio.wait_for(w.lease.acquire("dictation", wait_s=0.1), BOUND)
    assert any(f"{PEAK + YUE2_PEAK - 1} MiB free, {PEAK + YUE2_PEAK} MiB needed now" in r.getMessage() for r in caplog.records)
    await done(over, task)
    await nothing_left()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no reading", "music unsized", "whisper unsized"])
async def test_unknown_waits_in_the_line_neither_beside_nor_refused(monkeypatch, caplog, case):
    if case != "no reading":
        card(monkeypatch, free=30000)
    m = music()
    if case == "music unsized":
        m.lease.vram_mib = None
    over, task = await busy(m)
    w = sharing_whisper()
    if case == "whisper unsized":
        w.lease.vram_mib = None
    with caplog.at_level(logging.INFO, logger=card_lease.__name__):
        acquire = asyncio.create_task(w.lease.acquire("dictation", wait_s=BOUND))
        await until(lambda: waiting() == ["whisper:turbo"])
        assert not w.lease.granted
        assert any("L11 says unknown" in r.getMessage() for r in caplog.records)
        await done(over, task)
        await asyncio.wait_for(acquire, BOUND)
    assert w.lease.granted and w.lease.beside is None
    w.lease.release("test")
    await nothing_left()


@pytest.mark.asyncio
async def test_a_waiter_reads_the_card_once_per_change_of_holders_not_every_poll(monkeypatch):
    reads = card(monkeypatch, free=1000)
    m = music()
    over, task = await busy(m)
    w = sharing_whisper()
    with pytest.raises(LeaseWaitedTooLong):
        await asyncio.wait_for(w.lease.acquire("dictation", wait_s=0.2), BOUND)  # ~40 polls at 5 ms
    assert len(reads) == 1
    await done(over, task)
    await nothing_left()


@pytest.mark.asyncio
async def test_an_earlier_waiter_keeps_the_cell_shut(monkeypatch):
    card(monkeypatch, free=30000)
    m = music()
    over, task = await busy(m)
    e = embedder()
    first = asyncio.create_task(e.lease.acquire("query", wait_s=BOUND))
    await until(lambda: waiting() == ["embedder:BAAI/bge-m3"])
    w = sharing_whisper()
    second = asyncio.create_task(w.lease.acquire("dictation", wait_s=BOUND))
    await until(lambda: waiting() == ["embedder:BAAI/bge-m3", "whisper:turbo"])
    assert not w.lease.granted, "a co-grant must not add to an earlier waiter's wait"
    await done(over, task)
    await asyncio.wait_for(first, BOUND)
    e.lease.release("test")
    await asyncio.wait_for(second, BOUND)
    assert w.lease.beside is None
    w.lease.release("test")
    await nothing_left()


@pytest.mark.asyncio
async def test_an_idle_music_holder_is_asked_to_let_go_not_shared(monkeypatch):
    card(monkeypatch, free=30000)
    m = music()
    await asyncio.wait_for(m.lease.acquire("job", wait_s=BOUND), BOUND)  # granted, no use: idle
    w = sharing_whisper()
    await asyncio.wait_for(w.lease.acquire("dictation", wait_s=BOUND), BOUND)
    assert [why for why, _ in m.yields] == ["yield for whisper:turbo"]
    assert holding() == ["whisper:turbo"] and w.lease.beside is None
    w.lease.release("test")
    await nothing_left()


@pytest.mark.asyncio
async def test_no_other_pair_shares_by_measurement(monkeypatch):
    """Music as the contender beside a busy Whisper, and Whisper beside a busy second Whisper or a
    busy embedder: each waits (only a lease that `shares_with` the holder enters the cell)."""
    card(monkeypatch, free=30000)
    for holder, contender in ((sharing_whisper("busy"), music()),
                              (whisper("other", vram_mib=PEAK), sharing_whisper()),
                              (embedder(vram_mib=2000), sharing_whisper("w2"))):
        over, task = await busy(holder, "work")
        with pytest.raises(LeaseWaitedTooLong):
            await asyncio.wait_for(contender.lease.acquire("x", wait_s=0.05), BOUND)
        assert not contender.lease.granted
        await done(over, task)
        holder.lease.release("test")
        await nothing_left()


@pytest.mark.asyncio
async def test_a_supervisor_on_the_card_shuts_the_cell_before_any_arithmetic(monkeypatch, caplog):
    reads = card(monkeypatch, free=30000)
    m = music()
    over, task = await busy(m)
    # An engine that appeared beside the job (the runtime keeps them apart; the guard is what counts).
    llama = LlamaServerSupervisor("qwen27", {"gguf_path": "D:/models/q.gguf"})
    llama._proc = _FakeProc()
    llama.props = {"total_slots": 1}
    llama.predicted_vram_mib = 1000
    base_mod._CARD_CLAIMS[llama] = "claimed"
    w = sharing_whisper()
    with caplog.at_level(logging.INFO, logger=card_lease.__name__):
        with pytest.raises(LeaseWaitedTooLong):
            await asyncio.wait_for(w.lease.acquire("dictation", wait_s=0.05), BOUND)
    assert reads == [] and not [r for r in caplog.records if "beside" in r.getMessage()]
    base_mod._CARD_CLAIMS.clear()
    await done(over, task)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_larger_band_on_a_grant_made_beside_is_fitted_again(monkeypatch):
    """A grant fitted for a 10 s dictation's peak is not a pass for a 90 s one's: the new use is fitted
    again (admitted when it fits, waits for the job while it does not)."""
    reading = {"free": 30000}
    monkeypatch.setattr(card_lease, "_READ_THE_CARD", lambda: (reading["free"], TOTAL))
    m = music()
    over, task = await busy(m)
    w = sharing_whisper()
    await asyncio.wait_for(w.lease.acquire("dictation", wait_s=BOUND, vram_mib=3147), BOUND)
    assert w.lease.beside == "music:yue2" and w.lease._beside_peak == 3147
    async with w.lease.use("a second short one", wait_s=BOUND, vram_mib=3147):
        pass
    async with w.lease.use("a 90 s one", wait_s=BOUND, vram_mib=PEAK):
        assert w.lease._beside_peak == PEAK
    reading["free"] = PEAK + YUE2_PEAK - 1  # one MiB short of 4447 + yue2's peak now
    w.lease._beside_peak = 3147
    use = asyncio.create_task(hold_open(w.lease, "another 90 s one", asyncio.Event(), vram_mib=PEAK))
    await until(lambda: waiting() == ["whisper:turbo"])
    await done(over, task)
    await until(lambda: len(w.lease._uses) == 1)
    use.cancel()
    with pytest.raises(asyncio.CancelledError):
        await use
    w.lease.release("test")
    await nothing_left()


@pytest.mark.asyncio
async def test_the_music_lease_is_untouched_by_its_co_holder(monkeypatch):
    card(monkeypatch, free=30000)
    m = music()
    over, task = await busy(m)
    w = sharing_whisper()
    await asyncio.wait_for(w.lease.acquire("dictation", wait_s=BOUND), BOUND)
    assert m.lease._busy_for_the_card(m.lease.state) and m.lease.state == "running"
    await done(over, task)
    assert not m.lease._busy_for_the_card(m.lease.state), "its busy test reads only its own uses"
    assert m.yields == []
    w.lease.release("test")
    m.lease.release("test")
    await nothing_left()


@pytest.mark.asyncio
async def test_a_peer_is_granted_beside_when_it_fits_and_refused_at_once_when_not(monkeypatch):
    reading = {"free": 30000}
    monkeypatch.setattr(card_lease, "_READ_THE_CARD", lambda: (reading["free"], TOTAL))
    m = music()
    over, task = await busy(m)
    w = sharing_whisper()
    with serving_a_peer("peer-1"):
        await asyncio.wait_for(w.lease.acquire("peer peer-1", wait_s=None, expected_s=5.0), BOUND)
    assert w.lease.beside == "music:yue2"
    w.lease.release("test")
    reading["free"] = 1000
    with serving_a_peer("peer-1"):
        with pytest.raises(LeaseRefusedForPeer) as refused:
            await asyncio.wait_for(w.lease.acquire("peer peer-1", wait_s=None, expected_s=5.0), BOUND)
    assert "yue2" not in str(refused.value) and waiting() == []
    await done(over, task)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_background_call_is_granted_beside_when_it_fits_and_skipped_when_not(monkeypatch):
    reading = {"free": 30000}
    monkeypatch.setattr(card_lease, "_READ_THE_CARD", lambda: (reading["free"], TOTAL))
    m = music()
    over, task = await busy(m)
    w = sharing_whisper()
    await asyncio.wait_for(w.lease.acquire("preload", wait_s=0, background=True), BOUND)
    assert w.lease.beside == "music:yue2"
    w.lease.release("test")
    reading["free"] = 1000
    with pytest.raises(LeaseSkipped):
        await asyncio.wait_for(w.lease.acquire("preload", wait_s=0, background=True), BOUND)
    await done(over, task)
    await nothing_left()
