"""The card lease for the in-process background models (ADR-052 section 3.4; backlog
THE-EMBEDDER-GLINER-AND-SKILL-EMBEDDINGS-TAKE-THE-GPU-OUTSIDE-THE-CARD-LEASE-...).

`BackgroundModelLease` puts a model on the card only inside a stint that holds one lease use: bulk work
waits in the line with an honest `expected_s`, moves the model to the card when granted, lets go of its
own accord before its next compute call when an owner's request waits (a busy holder is never asked,
L6), and continues on the CPU. A short call never asks and never waits. Moves and compute calls are
serialised by the device lock. Real `CardLease`s, fake models (a list of moves), no GPU.
"""

import asyncio
import threading

import pytest

from dpc_client_core.managers import background_model_lease as bml
from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.background_model_lease import BackgroundModelLease
from dpc_client_core.managers.card_lease import CardLease

from tests._card_lease_helpers import BOUND, fast, hold_open, holding, nothing_left, until, waiting, whisper  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast")


class FakeModel:
    """Where the weights are, and every move; `gate`, when set, holds a move to the card open."""

    def __init__(self):
        self.where = "cpu"
        self.moves = []
        self.gate = None
        self.moving = threading.Event()

    def to_gpu(self):
        self.moving.set()
        if self.gate is not None:
            assert self.gate.wait(BOUND)
        self.where = "cuda"
        self.moves.append("cuda")

    def to_cpu(self):
        self.where = "cpu"
        self.moves.append("cpu")


def client(model, **kw):
    kw.setdefault("gpu_s_per_item", 0.01)
    kw.setdefault("cpu_s_per_item", 1.0)
    kw.setdefault("move_s", 1.0)
    return BackgroundModelLease(
        "embedder:BAAI/bge-m3", kind="embedding", engine="embedder", display="Embedding model (bge-m3)",
        vram_mib=1400, to_gpu=model.to_gpu, to_cpu=model.to_cpu, **kw)


def lease_row(holder="embedder:BAAI/bge-m3"):
    return next((row for row in base_mod.card_holders() if row["alias"] == holder), None)


def test_without_an_owner_loop_a_stint_stays_on_the_cpu_and_registers_nothing_granted():
    model = FakeModel()
    c = client(model)
    with c.stint("index rebuild", 100) as device:
        with c.compute(4) as inside:
            assert inside == "cpu"
    assert device == "cpu" and model.moves == []
    assert holding() == [] and card_lease._LEASE_LINE == []


@pytest.mark.asyncio
async def test_a_batch_too_small_to_repay_the_moves_never_asks():
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    model = FakeModel()
    c = client(model)  # 1 s per item on the CPU: 5 items is under MIN_GPU_WORTH_S

    def work():
        with c.stint("index rebuild", 5) as device:
            return device

    assert await asyncio.wait_for(asyncio.to_thread(work), BOUND) == "cpu"
    assert model.moves == [] and c._lease is None and holding() == []


@pytest.mark.asyncio
async def test_a_bulk_stint_waits_with_its_expected_s_then_holds_one_use_on_the_card_and_releases():
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    model = FakeModel()
    c = client(model)
    w = whisper()
    release = asyncio.Event()
    holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
    await until(lambda: holding() == ["whisper:turbo"])

    inside = threading.Event()
    done = threading.Event()
    seen = {}

    def work():
        with c.stint("index rebuild", 40) as device:
            seen["device"] = device
            with c.compute(4) as d:
                seen["compute"] = d
            seen["uses"] = [u.what for u in c._lease._uses]
            inside.set()
            assert done.wait(BOUND)

    thread = asyncio.create_task(asyncio.to_thread(work))
    await until(lambda: waiting() == ["embedder:BAAI/bge-m3"])
    (waiter,) = [x for x in card_lease._LEASE_LINE if x.lease.holder == "embedder:BAAI/bge-m3"]
    assert waiter.expected_s == pytest.approx(40 * 0.01 + 2 * 1.0)
    assert waiter.wait_s == pytest.approx(min(bml.BULK_WAIT_CAP_S, 40 * 1.0))
    assert model.moves == []

    release.set()
    await asyncio.wait_for(holder, BOUND)
    await asyncio.wait_for(asyncio.to_thread(inside.wait, BOUND), BOUND)
    assert seen == {"device": "cuda", "compute": "cuda", "uses": ["index rebuild"]}
    row = lease_row()
    assert row["busy"] and row["display"] == "Embedding model (bge-m3)" and row["kind"] == "embedding"
    assert row["eta_s"] == pytest.approx(1.0)  # nothing computing now: one move off

    done.set()
    await asyncio.wait_for(thread, BOUND)
    await until(lambda: not c._lease.granted)
    assert model.moves == ["cuda", "cpu"] and model.where == "cpu"
    assert c._lease._uses == []
    await nothing_left()


@pytest.mark.asyncio
async def test_an_owner_request_arriving_mid_stint_makes_it_let_go_and_the_batch_goes_on_on_the_cpu():
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    model = FakeModel()
    c = client(model)
    on_card = threading.Event()
    go_on = threading.Event()
    devices = []

    def work():
        with c.stint("index rebuild", 40):
            with c.compute(4) as d:
                devices.append(d)
            on_card.set()
            assert go_on.wait(BOUND)
            for _ in range(3):
                with c.compute(4) as d:
                    devices.append(d)

    thread = asyncio.create_task(asyncio.to_thread(work))
    await asyncio.wait_for(asyncio.to_thread(on_card.wait, BOUND), BOUND)
    assert holding() == ["embedder:BAAI/bge-m3"] and model.where == "cuda"

    # A dictation needs the card: the busy stint is not asked (L6), it waits in the line.
    w = whisper()
    got = asyncio.Event()

    async def dictation():
        async with w.lease.use("dictation", wait_s=BOUND):
            got.set()

    dictating = asyncio.create_task(dictation())
    await until(lambda: waiting() == ["whisper:turbo"])
    assert w.yields == [] and model.where == "cuda"

    go_on.set()
    await asyncio.wait_for(dictating, BOUND)
    await asyncio.wait_for(thread, BOUND)
    assert got.is_set()
    assert devices == ["cuda", "cpu", "cpu", "cpu"]
    assert model.moves == ["cuda", "cpu"], "moved off once, never back"
    await until(lambda: not c._lease.granted)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_short_call_never_waits_and_computes_on_the_cpu_while_another_holds_the_card():
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    model = FakeModel()
    c = client(model)
    w = whisper()
    release = asyncio.Event()
    holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
    await until(lambda: holding() == ["whisper:turbo"])

    def short_call():
        with c.compute(1) as device:
            return device

    assert await asyncio.wait_for(asyncio.to_thread(short_call), 1.0) == "cpu"
    assert waiting() == [] and model.moves == [] and c._lease is None
    release.set()
    await asyncio.wait_for(holder, BOUND)


@pytest.mark.asyncio
async def test_a_short_call_rides_a_running_stint_on_the_card():
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    model = FakeModel()
    c = client(model)
    on_card = threading.Event()
    done = threading.Event()

    def work():
        with c.stint("index rebuild", 40):
            on_card.set()
            assert done.wait(BOUND)

    thread = asyncio.create_task(asyncio.to_thread(work))
    await asyncio.wait_for(asyncio.to_thread(on_card.wait, BOUND), BOUND)

    def short_call():
        with c.compute(1) as device:
            return device

    assert await asyncio.wait_for(asyncio.to_thread(short_call), BOUND) == "cuda"
    assert [u.what for u in c._lease._uses] == ["index rebuild"], "it opens no use of its own"
    done.set()
    await asyncio.wait_for(thread, BOUND)
    assert await asyncio.wait_for(asyncio.to_thread(short_call), BOUND) == "cpu"


@pytest.mark.asyncio
async def test_a_compute_call_during_a_move_waits_for_the_move_to_end():
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    model = FakeModel()
    model.gate = threading.Event()
    c = client(model)
    done = threading.Event()

    def work():
        with c.stint("index rebuild", 40):
            assert done.wait(BOUND)

    thread = asyncio.create_task(asyncio.to_thread(work))
    await asyncio.wait_for(asyncio.to_thread(model.moving.wait, BOUND), BOUND)

    entered = threading.Event()
    seen = []

    def short_call():
        with c.compute(1) as device:
            entered.set()
            seen.append((device, model.where))

    rider = asyncio.create_task(asyncio.to_thread(short_call))
    await asyncio.sleep(0.1)
    assert not entered.is_set(), "no compute call runs mid-move"
    model.gate.set()
    await asyncio.wait_for(rider, BOUND)
    assert seen == [("cuda", "cuda")]
    done.set()
    await asyncio.wait_for(thread, BOUND)
    assert model.moves == ["cuda", "cpu"]


@pytest.mark.asyncio
async def test_a_stint_whose_grant_meets_an_owner_already_waiting_lets_go_before_moving(monkeypatch):
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    model = FakeModel()
    c = client(model)
    monkeypatch.setattr(card_lease, "an_owner_waits_for_the_card", lambda lease=None: True)

    def work():
        with c.stint("index rebuild", 40) as device:
            return device

    assert await asyncio.wait_for(asyncio.to_thread(work), BOUND) == "cpu"
    assert model.moves == []
    await until(lambda: not c._lease.granted)


@pytest.mark.asyncio
async def test_a_failed_move_to_the_card_runs_the_batch_on_the_cpu_and_releases():
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    model = FakeModel()

    def boom():
        raise RuntimeError("CUDA out of memory")

    c = client(model)
    c._to_gpu = boom

    def work():
        with c.stint("index rebuild", 40) as device:
            with c.compute(2) as d:
                return device, d

    assert await asyncio.wait_for(asyncio.to_thread(work), BOUND) == ("cpu", "cpu")
    await until(lambda: not c._lease.granted)
    assert model.moves == ["cpu"]


class TestTheYieldOfAnIdleGrant:

    def test_nothing_on_the_card_is_nothing_to_give(self):
        c = client(FakeModel())
        assert c._yield_in_a_thread("yield for x") == "nothing_to_give"

    def test_a_model_on_the_card_with_no_use_moves_off_and_lets_go(self):
        model = FakeModel()
        c = client(model)
        c._on_gpu, model.where = True, "cuda"
        assert c._yield_in_a_thread("yield for x") == "let_go"
        assert model.where == "cpu" and c.device == "cpu"

    def test_a_move_off_that_fails_keeps_the_card(self):
        model = FakeModel()
        c = client(model)
        c._on_gpu = True

        def boom():
            raise RuntimeError("driver")

        c._to_cpu = boom
        assert c._yield_in_a_thread("yield for x") == "could_not_let_go"
        assert c.device == "cuda"


class TestWhoCountsAsAnOwnerWaiting:

    @pytest.mark.asyncio
    async def test_an_owner_lease_waiter_counts_and_the_lease_itself_does_not(self):
        base_mod.bind_owner_loop(asyncio.get_running_loop())
        w = whisper()
        other = CardLease("music:yue2", kind="music", engine="yue2.cpp", display="yue2.cpp",
                          vram_mib=None, yield_cost="engine", on_yield=lambda why: None)
        release = asyncio.Event()
        holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: holding() == ["whisper:turbo"])
        assert not card_lease.an_owner_waits_for_the_card(w.lease)
        queued = asyncio.create_task(hold_open(other, "song", asyncio.Event(), wait_s=BOUND))
        await until(lambda: waiting() == ["music:yue2"])
        assert card_lease.an_owner_waits_for_the_card(w.lease)
        assert not card_lease.an_owner_waits_for_the_card(other), "its own waiter is not a contender"
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release.set()
        await asyncio.wait_for(holder, BOUND)

    def test_a_peers_waiter_does_not_count(self, monkeypatch):
        from types import SimpleNamespace

        peer = SimpleNamespace(granted=False, out=None, for_a_peer=True, lease=object())
        monkeypatch.setattr(card_lease, "_LEASE_LINE", [peer])
        assert not card_lease.an_owner_waits_for_the_card()
        peer.for_a_peer = False
        assert card_lease.an_owner_waits_for_the_card()

    def test_a_place_in_the_card_queue_counts(self, monkeypatch):
        monkeypatch.setattr(base_mod, "_CARD_QUEUE", [object()])
        assert card_lease.an_owner_waits_for_the_card()


def test_use_blocking_gives_up_on_an_owner_loop_that_does_not_answer(monkeypatch):
    """Zcode #54, known debt 4: a thread client's wait for the owner loop is bounded by its `wait_s`
    plus `THREAD_ANSWER_MARGIN_S`; past it the request is cancelled and the refusal raised."""
    import concurrent.futures

    class Deaf:
        """An owner loop that says it runs and never answers."""

        def is_closed(self):
            return False

        def is_running(self):
            return True

        def call_soon_threadsafe(self, *a, **kw):
            pass

    never = concurrent.futures.Future()
    lease = CardLease("ner:gliner", kind="ner", engine="gliner", display="GLiNER", vram_mib=None,
                      yield_cost="cheap", on_yield=lambda why: None)
    base_mod.bind_owner_loop(Deaf())
    monkeypatch.setattr(card_lease, "THREAD_ANSWER_MARGIN_S", 0.05)
    monkeypatch.setattr(card_lease.asyncio, "run_coroutine_threadsafe",
                        lambda coro, owner: (coro.close(), never)[1])
    with pytest.raises(card_lease.LeaseWaitedTooLong, match="no answer"):
        with lease.use_blocking("entity extraction", wait_s=0.05):
            pass
    assert never.cancelled()


@pytest.mark.asyncio
async def test_a_stint_does_not_wait_behind_a_local_engine_on_the_card(monkeypatch):
    """A llama-server or Strata does not go by itself soon and this lease never swaps one out, so the
    stint asks without a wait (refused at once, the CPU) rather than stand in the line, where it would
    keep a later Whisper or music request from swapping the engine out (L5a)."""
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    monkeypatch.setattr(bml, "_a_local_engine_holds_the_card", lambda: True)
    model = FakeModel()
    c = client(model)
    w = whisper()
    release = asyncio.Event()
    holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
    await until(lambda: holding() == ["whisper:turbo"])

    def work():
        with c.stint("index rebuild", 40) as device:
            return device

    assert await asyncio.wait_for(asyncio.to_thread(work), 1.0) == "cpu"
    assert waiting() == [] and model.moves == []
    release.set()
    await asyncio.wait_for(holder, BOUND)
    await nothing_left()


def test_a_local_engine_on_the_card_is_read_from_the_claims_and_a_stopping_one_does_not_count(monkeypatch):
    engine = object()
    monkeypatch.setattr(base_mod, "_CARD_CLAIMS", {engine: "claimed"})
    assert bml._a_local_engine_holds_the_card()
    monkeypatch.setattr(base_mod, "_CARD_CLAIMS", {engine: "stopping"})
    assert not bml._a_local_engine_holds_the_card()
    monkeypatch.setattr(base_mod, "_CARD_CLAIMS", {})
    assert not bml._a_local_engine_holds_the_card()
