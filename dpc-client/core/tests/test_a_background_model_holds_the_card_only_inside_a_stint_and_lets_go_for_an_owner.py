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

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.background_model_lease import BackgroundModelLease
from dpc_client_core.managers.card_lease import CardLease

from tests._card_lease_helpers import BOUND, fast, hold_open, holding, music, nothing_left, until, waiting, whisper  # noqa: F401

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
async def test_a_stint_never_waits_for_a_card_another_holds_and_runs_on_the_cpu():
    """A background call (ADR-052 2.1): the card only when it is free right now."""
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    model = FakeModel()
    c = client(model)
    w = whisper()
    release = asyncio.Event()
    holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
    await until(lambda: holding() == ["whisper:turbo"])

    def work():
        with c.stint("index rebuild", 40) as device:
            with c.compute(4) as d:
                return device, d

    assert await asyncio.wait_for(asyncio.to_thread(work), 1.0) == ("cpu", "cpu")
    assert waiting() == [] and model.moves == [] and w.yields == []
    release.set()
    await asyncio.wait_for(holder, BOUND)
    await nothing_left()


@pytest.mark.asyncio
async def test_a_stint_on_a_free_card_holds_one_use_with_its_expected_s_and_releases(monkeypatch):
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    model = FakeModel()
    c = client(model)
    asked = []
    real = card_lease.CardLease.use_blocking

    def recording(self, what, **kw):
        asked.append((what, kw))
        return real(self, what, **kw)

    monkeypatch.setattr(card_lease.CardLease, "use_blocking", recording)
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
    await asyncio.wait_for(asyncio.to_thread(inside.wait, BOUND), BOUND)
    assert asked == [("index rebuild", {"wait_s": 0, "background": True,
                                        "expected_s": pytest.approx(40 * 0.01 + 2 * 1.0)})]
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
@pytest.mark.parametrize("make", [whisper, music], ids=["whisper", "music"])
async def test_a_stint_never_asks_an_idle_warm_holder_to_let_go(make):
    """ADR-052 2.1: the embedder and GLiNER take the card only when it is free; an idle Whisper or
    music grant on the card is not asked to yield for them (the coordinator's probe, 2026-11-27)."""
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    other = make()
    await other.lease.acquire("warm", wait_s=BOUND)  # granted with no use: an idle holder
    model = FakeModel()
    c = client(model)

    def work():
        with c.stint("index update", 12) as device:
            return device

    assert await asyncio.wait_for(asyncio.to_thread(work), BOUND) == "cpu"
    await asyncio.sleep(0.05)
    assert other.yields == [] and other.lease.granted and model.moves == []
    assert waiting() == []
    other.lease.release("test done")
    await nothing_left()


@pytest.mark.asyncio
async def test_a_stint_never_stands_ahead_of_a_later_owner_request():
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    w = whisper()
    release = asyncio.Event()
    holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
    await until(lambda: holding() == ["whisper:turbo"])
    c = client(FakeModel())
    stint = asyncio.create_task(asyncio.to_thread(lambda: c.stint("index rebuild", 40).__enter__()))
    assert await asyncio.wait_for(stint, 1.0) == "cpu"
    assert waiting() == [], "nothing of the stint is left in the line"
    release.set()
    await asyncio.wait_for(holder, BOUND)


@pytest.mark.asyncio
async def test_a_stint_that_opens_while_the_last_one_lets_go_waits_and_then_takes_a_use_of_its_own():
    """The close/open race (review, 2026-11-27): A has left its body and is moving off the card; B
    opens meanwhile. B must not join A's dying use nor move the model onto an ungranted lease."""
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    model = FakeModel()
    c = client(model)
    in_move_off = threading.Event()
    let_off = threading.Event()
    real_to_cpu = c._to_cpu

    def slow_to_cpu():
        if not in_move_off.is_set():
            in_move_off.set()
            assert let_off.wait(BOUND)
        real_to_cpu()

    c._to_cpu = slow_to_cpu
    b_entered = threading.Event()
    seen = {}

    def a():
        with c.stint("pass A", 40):
            pass

    def b():
        assert in_move_off.wait(BOUND)
        with c.stint("pass B", 40):
            b_entered.set()
            with c.compute(4) as d:
                seen["device"] = d
                seen["granted"] = c._lease.granted
                seen["uses"] = [u.what for u in c._lease._uses]

    ta = asyncio.create_task(asyncio.to_thread(a))
    tb = asyncio.create_task(asyncio.to_thread(b))
    await asyncio.wait_for(asyncio.to_thread(in_move_off.wait, BOUND), BOUND)
    await asyncio.sleep(0.1)
    assert not b_entered.is_set(), "B waits until A has let go"
    let_off.set()
    await asyncio.wait_for(ta, BOUND)
    await asyncio.wait_for(tb, BOUND)
    assert seen == {"device": "cuda", "granted": True, "uses": ["pass B"]}
    assert model.moves == ["cuda", "cpu", "cuda", "cpu"]
    await until(lambda: not c._lease.granted and c._lease._uses == [])
    await nothing_left()


@pytest.mark.asyncio
async def test_a_stint_that_opens_while_another_runs_joins_it_and_the_last_to_close_lets_go():
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    model = FakeModel()
    c = client(model)
    a_in, b_in, a_out = threading.Event(), threading.Event(), threading.Event()
    seen = {}

    def a():
        with c.stint("pass A", 40):
            a_in.set()
            assert b_in.wait(BOUND)
        a_out.set()

    def b():
        assert a_in.wait(BOUND)
        with c.stint("pass B", 40):
            b_in.set()
            assert a_out.wait(BOUND)
            with c.compute(4) as d:
                seen["after_a"] = (d, c._lease.granted, [u.what for u in c._lease._uses])

    await asyncio.wait_for(asyncio.gather(asyncio.to_thread(a), asyncio.to_thread(b)), BOUND)
    assert seen["after_a"] == ("cuda", True, ["pass A"]), "B rode A's one use, kept after A closed"
    assert model.moves == ["cuda", "cpu"]
    await until(lambda: not c._lease.granted and c._lease._uses == [])
    await nothing_left()


@pytest.mark.asyncio
async def test_an_error_on_the_way_to_the_lease_leaves_the_batch_on_the_cpu(monkeypatch):
    """A cancel at shutdown surfaces as concurrent.futures.CancelledError: the CPU, never a crash."""
    import concurrent.futures

    base_mod.bind_owner_loop(asyncio.get_running_loop())
    model = FakeModel()
    c = client(model)

    def cancelled(self, what, **kw):
        raise concurrent.futures.CancelledError()

    monkeypatch.setattr(card_lease.CardLease, "use_blocking", cancelled)

    def work():
        with c.stint("index rebuild", 40) as device:
            with c.compute(2) as d:
                return device, d

    assert await asyncio.wait_for(asyncio.to_thread(work), BOUND) == ("cpu", "cpu")
    assert model.moves == []


@pytest.mark.asyncio
async def test_a_model_dropped_mid_stint_computes_on_the_cpu_and_the_use_closes_at_the_end():
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    model = FakeModel()
    c = client(model)
    seen = []

    def work():
        with c.stint("index rebuild", 40):
            with c.compute(1) as d:
                seen.append(d)
            c.model_dropped()
            with c.compute(1) as d:
                seen.append(d)
            seen.append([u.what for u in c._lease._uses])

    await asyncio.wait_for(asyncio.to_thread(work), BOUND)
    assert seen == ["cuda", "cpu", ["index rebuild"]]
    await until(lambda: not c._lease.granted and c._lease._uses == [])
    assert model.moves == ["cuda"], "nothing left to move back"
