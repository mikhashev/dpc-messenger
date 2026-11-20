"""The slot queue: a conversation keeps the slot until its turn ends (ADR-044, amendment of
2026-11-17, "the slot queue", point 12).

DPC's wait in front of one local alias's slot, the same on a llama-server alias and a Strata one
(Mike's call, 2026-11-20); only llama-server saves the outgoing conversation at a hand-over. An agent's turn holds the slot from its
first call until its `accepted_request` hold leaves the alias; the next conversation waits, and
when the slot passes, the outgoing conversation is saved to the KV snapshots folder before the
incoming call is sent. The turn's own calls pass (a description, the pre-analysis, a tool thread
carrying the live hold). The owner's chat and a peer at its deadline go in between two calls of
the turn, saving first. A named background call is skipped by name rather than wait.

A real supervisor over the fake child of the card tests; `/slots`, the slot save and restore are
faked at the supervisor's own seams, the provider's client by `wire`, and the holds are real
(`card_held_for`), so they stand on the alias as the agent's, the chat's and the gateway's do.
"""

import asyncio
import logging
import re
import time
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio

from dpc_client_core.managers import kv_snapshots as kv
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import carrying_card_hold, serving_a_peer

from tests.test_a_callers_run_of_calls_keeps_its_alias_busy_for_the_window import (
    _gateway, _peer_door, _query_over,
)
from dpc_client_core.providers import StrataProvider
from tests.test_one_local_engine_holds_the_card import _fake_child, _llama, card  # noqa: F401
from tests.test_strata_supervisor import _install
from tests.test_the_last_engine_call_names_owner_peer_or_gateway_from_the_calls_own_context import (
    FakeCompletions, llama, wire,
)

ALIAS = "qwen27"
OTHER = "qwen9"
STRATA = "flash"
SYSTEM = "you are a helper"
TURN_1 = [{"role": "user", "content": "list the files"}]
IMAGE = {"mime_type": "image/png", "base64": "aGVsbG8="}
NODE = "dpc-node-0123456789abcdef0123456789abcdef"
# A peer on a proved connection of `make_coordinator`'s, for calls through the real peer door.
DOOR_PEER = "peer-1"
SUP_LOG = base_mod.logger.name
# The save's allowance before a peer's deadline: the write ceiling plus the switch margin (point 8).
ALLOWANCE = kv.KV_SNAPSHOT_WRITE_CEILING_S + base_mod._KV_SWITCH_DEADLINE_MARGIN_S


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    kv.reset_state()
    monkeypatch.setattr(kv, "disk_short", lambda folder, nbytes: False)
    monkeypatch.setattr(base_mod, "_last_caller_call_at", {})
    monkeypatch.setattr(base_mod, "_SWAP_POLL_S", 0.005)
    yield
    kv.reset_state()
    base_mod.bind_local_engine_settings(None)


@pytest_asyncio.fixture(autouse=True)
async def no_task_left():
    """Every task a test started (a turn's worker, a peer's or a chat's call) is cancelled and awaited
    before the loop closes, so none is torn down half-way in another context."""
    before = asyncio.all_tasks()
    yield
    left = [task for task in asyncio.all_tasks() - before
            if task is not asyncio.current_task() and not task.done()]
    for task in left:
        task.cancel()
    await asyncio.gather(*left, return_exceptions=True)


class Clock:
    """`_swap_clock` by hand: the test says what time it is."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(base_mod, "_swap_clock", fake)
    return fake


class Settings:
    """The settings the supervisor module reads: no idle swap, the owner's wait and the guest's."""

    def __init__(self, swap_wait_s=1800, remote_inference_timeout=1200):
        self.swap_wait_s, self.remote = swap_wait_s, remote_inference_timeout

    def get_swap_when_idle(self):
        return False

    def get_swap_wait_s(self):
        return self.swap_wait_s

    def get_caller_idle_s(self):
        return 0

    def get_remote_inference_timeout(self):
        return float(self.remote)


@pytest.fixture
def peer_time():
    base_mod.bind_local_engine_settings(Settings())


class Engine:
    """One slot: a request puts its prompt there; a save writes the file; a restore fills it.
    `events` says what reached the engine, in order; `hold(label)` keeps the next request of that
    conversation (or caller kind) in the engine until `release(label)`."""

    def __init__(self, sup, folder, alias=ALIAS):
        self.alias = alias
        self.folder = folder
        self.slot_list = [{"id": 0, "n_prompt_tokens": 0}]
        self.events = []
        self.gates = {}
        self.fail = set()
        sup.slots = self.slots
        sup._slot_action = self.action
        self.sup = sup

        async def no_cold_rows(tokens):
            return None, 0

        sup._kv_cold_rate = no_cold_rows

    async def slots(self):
        return [dict(s) for s in self.slot_list]

    async def action(self, slot, action, filename, timeout):
        self.events.append(action)
        if action == "save":
            tokens = self.slot_list[slot]["n_prompt_tokens"]
            (self.folder / filename).write_bytes(b"\0" * 4096)
            self.events.append("save-end")
            return 200, {"n_saved": tokens, "n_written": 4096}
        self.slot_list[0]["n_prompt_tokens"] = 777
        return 200, {"n_restored": 777, "n_read": 4096}

    def hold(self, label):
        self.gates[label] = asyncio.Event()

    def release(self, label):
        self.gates.pop(label).set()

    async def request(self, params):
        record = kv.engine_call_of(self.alias)
        label = record["conversation_id"] or record["caller_kind"] or "background"
        self.events.append(f"request:{label}")
        self.slot_list[0]["n_prompt_tokens"] = 1000 * len(params["messages"])
        gate = self.gates.get(label)
        if gate is not None:
            await gate.wait()
        self.events.append(f"end:{label}")
        if label in self.fail:
            raise ConnectionError("the engine dropped the connection")


class Completions(FakeCompletions):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    async def create(self, **params):
        await self.engine.request(params)
        return await super().create(**params)


async def engine_on(tmp_path, alias=ALIAS, **cfg):
    folder = tmp_path / f"kv-{alias}"
    cfg.setdefault("n_parallel", 1)
    cfg.setdefault("cache_ram_mib", 0)
    sup = _llama(alias, slot_save_path=str(folder), **cfg)
    await sup.ensure_running()
    sup._kv_engine_fresh = False  # the empty-engine restore is not this file's case
    engine = Engine(sup, folder, alias)
    p = llama(alias)
    wire(p, sup=sup, completions=Completions(engine))
    return p, sup, engine, folder


async def strata_on(tmp_path, alias=STRATA):
    """The same engine double over a real Strata supervisor and provider. Strata saves nothing at a
    hand-over, so `engine.events` carries no "save" and no "restore" unless the code asked for one."""
    folder = tmp_path / f"kv-{alias}"
    p = StrataProvider(alias, {
        "type": "strata", "alias": alias, "model": "qwen3.8-flash-next-iq2_xs",
        "strata_install_dir": str(_install(tmp_path)), "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240, "context_window": 200000,
        "engine_watch_interval_s": 3600, "vram_release_timeout_s": 0.05,
    })
    sup = p.supervisor
    _fake_child(sup)
    await sup.ensure_running()
    sup._kv_engine_fresh = False  # the empty-engine restore is not this file's case
    engine = Engine(sup, folder, alias)
    wire(p, sup=sup, completions=Completions(engine))
    return p, sup, engine


class Turn:
    """An agent's turn on the alias: its real hold, and what it does inside it on demand."""

    def __init__(self, p, agent, conversation, alias=ALIAS):
        self.p, self.agent, self.conversation, self.alias = p, agent, conversation, alias
        self.jobs = asyncio.Queue()
        self.hold = None
        kv.note_clearable(conversation)
        self.task = asyncio.ensure_future(self._run())

    async def _run(self):
        async with base_mod.card_held_for(self.alias, "accepted_request", agent_id=self.agent,
                                          conversation_id=self.conversation) as hold:
            self.hold = hold
            while True:
                job = await self.jobs.get()
                if job is None:
                    return
                make, done = job
                if make == "raise":
                    raise RuntimeError("the turn failed")
                try:
                    done.set_result(await make())
                except BaseException as e:  # noqa: BLE001 - handed to the test as it came
                    done.set_exception(e)

    def do(self, make):
        done = asyncio.get_running_loop().create_future()
        self.jobs.put_nowait((make, done))
        return done

    def call(self, messages=TURN_1):
        return self.do(lambda: self.p.generate_with_tools(messages, [], system=SYSTEM))

    def fail(self):
        self.jobs.put_nowait(("raise", None))

    async def end(self):
        self.jobs.put_nowait(None)
        await asyncio.wait_for(self.task, 5)


async def chat(p, conversation, prompt="what is new"):
    async with base_mod.card_held_for(p.alias, "accepted_request", conversation_id=conversation):
        return await p.generate_response(prompt)


async def a_peers_call(p, peer=NODE):
    with serving_a_peer(peer):
        return await p.generate_response("hello")


async def a_tool_threads_call(p, hold):
    """A tool's model call on a thread of its own, carrying the hold it was handed."""
    with carrying_card_hold(hold):
        return await p.generate_with_tools(TURN_1, [], system=SYSTEM)


async def until(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out waiting"
        await asyncio.sleep(0.005)


def waiting(alias=ALIAS):
    return [w["kind"] for w in base_mod.slot_queue().get(alias, {}).get("waiting", [])]


def going_in_between(alias=ALIAS):
    line = base_mod._SLOT_QUEUE.get(alias)
    return line.between.kind if line is not None and line.between is not None else None


def holder(alias=ALIAS):
    row = base_mod.slot_queue().get(alias, {}).get("holder")
    return row["kind"] if row else None


async def still_waits(condition_false, settle=0.1):
    await asyncio.sleep(settle)
    assert not condition_false()


def entries(folder):
    return kv.read_index(folder)["entries"]


def lines(caplog, words):
    return [r.getMessage() for r in caplog.records if words in r.getMessage()]


# --- the card's own test ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b_waits_for_a_s_turn_and_a_s_conversation_is_saved_before_b_s_first_call(tmp_path, caplog):
    p, sup, engine, folder = await engine_on(tmp_path)
    a, b = Turn(p, "a1", "c1"), Turn(p, "a2", "c2")
    engine.hold("c1")
    a_first = a.call()
    await until(lambda: "request:c1" in engine.events)
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        b_first = b.call()
        await until(lambda: waiting() == ["agent"])
        engine.release("c1")
        await a_first
        await asyncio.wait_for(a.call(), 5)  # A's later round goes before B's first call
        assert "request:c2" not in engine.events
        await a.end()
        await asyncio.wait_for(b_first, 5)
    assert engine.events == ["request:c1", "end:c1", "request:c1", "end:c1", "save", "save-end",
                             "request:c2", "end:c2"]
    entry, = entries(folder)
    assert (entry["agent_id"], entry["conversation_id"]) == ("a1", "c1")
    assert (folder / entry["file"]).exists()
    assert not lines(caplog, "still in flight")
    assert lines(caplog, f"slot queue[{ALIAS}]: agent a2 conversation c2 waits, 0 ahead; "
                         f"the slot is held by agent a1 conversation c1 for")
    assert lines(caplog, f"slot queue[{ALIAS}]: the slot passes from agent a1 conversation c1 (held")
    await b.end()


# --- the turn's own calls -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_s_own_description_and_pre_analysis_pass_while_b_waits(tmp_path):
    p, sup, engine, folder = await engine_on(tmp_path)
    a, b = Turn(p, "a1", "c1"), Turn(p, "a2", "c2")
    await asyncio.wait_for(a.call(), 5)
    b_first = b.call()
    await until(lambda: waiting() == ["agent"])

    async def description():
        with base_mod.a_card_borrow():
            return await p.generate_with_vision("what is in it", [IMAGE])

    assert await asyncio.wait_for(a.do(description), 5) == "ok"
    assert await asyncio.wait_for(a.do(lambda: p.generate_with_vision("describe it", [IMAGE])), 5) == "ok"
    assert waiting() == ["agent"] and not b_first.done()
    await a.end()
    await asyncio.wait_for(b_first, 5)
    await b.end()


@pytest.mark.asyncio
async def test_a_tool_thread_carrying_a_s_live_hold_passes_while_b_waits(tmp_path):
    p, sup, engine, folder = await engine_on(tmp_path)
    a, b = Turn(p, "a1", "c1"), Turn(p, "a2", "c2")
    await asyncio.wait_for(a.call(), 5)
    b_first = b.call()
    await until(lambda: waiting() == ["agent"])
    await asyncio.wait_for(a_tool_threads_call(p, a.hold), 5)
    assert not b_first.done()
    await a.end()
    await asyncio.wait_for(b_first, 5)
    await b.end()


@pytest.mark.asyncio
async def test_a_tool_thread_whose_turn_has_ended_is_refused_while_b_holds_the_slot(tmp_path):
    p, sup, engine, folder = await engine_on(tmp_path)
    a, b = Turn(p, "a1", "c1"), Turn(p, "a2", "c2")
    await asyncio.wait_for(a.call(), 5)
    await a.end()
    await asyncio.wait_for(b.call(), 5)  # B holds the slot now
    with pytest.raises(RuntimeError) as failed:
        await asyncio.wait_for(a_tool_threads_call(p, a.hold), 5)
    refusal = base_mod.card_queue_refusal_in(failed.value)
    assert refusal is not None and "the request this description was for has ended" in str(refusal)
    assert engine.events.count("request:c1") == 1
    await b.end()


# --- the card-borrow does not deadlock -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_waiting_for_the_slot_does_not_keep_the_holder_busy_for_the_slot_holders_borrow(tmp_path):
    """Point 5: a description of A that gives up A's alias asks whether the holder is busy with
    every stand counted but A's own; B, waiting in A's slot queue, would wait for A's turn while
    A's description waited for B. B still counts for anyone else, and gets the slot after A."""
    p, sup, engine, folder = await engine_on(tmp_path)
    a, b = Turn(p, "a1", "c1"), Turn(p, "a2", "c2")
    await asyncio.wait_for(a.call(), 5)
    b_first = b.call()
    await until(lambda: waiting() == ["agent"])
    assert sup._busy_for_the_card("running") is True
    assert sup._busy_for_the_card("running", but_for=a.hold) is False
    await a.end()
    await asyncio.wait_for(b_first, 5)
    assert holder() == "agent"
    await b.end()


# --- giving the slot back ----------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["raise", "cancel", "move"])
async def test_a_s_turn_gives_the_slot_to_b_however_it_ends(tmp_path, caplog, how):
    p, sup, engine, folder = await engine_on(tmp_path)
    a, b = Turn(p, "a1", "c1"), Turn(p, "a2", "c2")
    await asyncio.wait_for(a.call(), 5)
    b_first = b.call()
    await until(lambda: waiting() == ["agent"])
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        if how == "raise":
            a.fail()
            with pytest.raises(RuntimeError):
                await asyncio.wait_for(a.task, 5)
        elif how == "cancel":
            a.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await a.task
        else:
            async def moves():
                base_mod.card_hold_follows(OTHER)

            await asyncio.wait_for(a.do(moves), 5)
        await asyncio.wait_for(b_first, 5)
    assert engine.events[-2:] == ["request:c2", "end:c2"]
    assert lines(caplog, f"slot queue[{ALIAS}]: the slot passes from agent a1 conversation c1")
    if how == "move":
        await a.end()
    await b.end()


@pytest.mark.asyncio
async def test_a_call_with_no_hold_keeps_its_grant_across_a_retry_and_gives_it_back_when_it_raises(tmp_path):
    p, sup, engine, folder = await engine_on(tmp_path)
    p.max_retry_seconds = 0.6
    engine.fail.add("background")
    background = asyncio.ensure_future(p.generate_response("summarise the day"))
    await until(lambda: "end:background" in engine.events)
    b = Turn(p, "a2", "c2")
    b_first = b.call()
    await until(lambda: waiting() == ["agent"])  # through the backoff sleep, the slot stays held
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(background, 5)
    assert engine.events.count("request:background") >= 2, "the retry did not pass its own grant"
    await asyncio.wait_for(b_first, 5)
    assert engine.events[-2:] == ["request:c2", "end:c2"]
    await b.end()


@pytest.mark.asyncio
async def test_a_grant_whose_waiter_was_cancelled_passes_to_the_next(tmp_path):
    p, sup, engine, folder = await engine_on(tmp_path)
    a, b, c = Turn(p, "a1", "c1"), Turn(p, "a2", "c2"), Turn(p, "a3", "c3")
    await asyncio.wait_for(a.call(), 5)
    b.call()
    await until(lambda: waiting() == ["agent"])
    c_first = c.call()
    await until(lambda: waiting() == ["agent", "agent"])
    b.task.cancel()
    await until(lambda: waiting() == ["agent"])
    await a.end()
    await asyncio.wait_for(c_first, 5)
    assert "request:c2" not in engine.events
    await c.end()


@pytest.mark.asyncio
async def test_a_persons_stop_takes_a_turn_out_of_the_slot_queue(tmp_path, caplog):
    p, sup, engine, folder = await engine_on(tmp_path)
    a, b = Turn(p, "a1", "c1"), Turn(p, "a2", "c2")
    await asyncio.wait_for(a.call(), 5)
    b_first = b.call()
    await until(lambda: waiting() == ["agent"])
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        base_mod.leave_card_queue("c2")
        with pytest.raises(RuntimeError) as stopped:
            await asyncio.wait_for(b_first, 5)
    refusal = base_mod.engine_refusal_in(stopped.value)
    # The slot queue's own class, so the loop says "waiting for the slot", not "the queue for the card";
    # a TakenOutOfTheCardQueue still, so every existing branch on a Stop keeps working.
    assert isinstance(refusal, base_mod.TakenOutOfTheSlotQueue)
    assert isinstance(refusal, base_mod.TakenOutOfTheCardQueue)
    assert refusal.holder_alias == ALIAS
    assert b.hold.taken_out is True
    assert waiting() == [] and "request:c2" not in engine.events
    assert lines(caplog, f"slot queue[{ALIAS}]: agent a2 conversation c2 leaves the slot queue after")
    assert lines(caplog, "s: Stop")
    await a.end()
    await b.end()


@pytest.mark.asyncio
async def test_a_call_still_in_the_engine_at_a_hand_over_is_waited_for_before_the_save(tmp_path, caplog):
    p, sup, engine, folder = await engine_on(tmp_path)
    a, b = Turn(p, "a1", "c1"), Turn(p, "a2", "c2")
    await asyncio.wait_for(a.call(), 5)
    engine.hold("c1")
    tool = asyncio.ensure_future(a_tool_threads_call(p, a.hold))
    await until(lambda: engine.events.count("request:c1") == 2)
    b_first = b.call()
    await until(lambda: waiting() == ["agent"])
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await a.end()  # the turn ends; its tool thread's call is still in the engine
        await still_waits(lambda: "save" in engine.events)
        engine.release("c1")
        await asyncio.wait_for(tool, 5)
        await asyncio.wait_for(b_first, 5)
    assert engine.events[-5:] == ["end:c1", "save", "save-end", "request:c2", "end:c2"]
    assert lines(caplog, "1 call(s) in flight")
    await b.end()


# --- order --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owners_turns_go_in_arrival_order_and_ahead_of_a_peer_that_came_first(tmp_path, peer_time):
    p, sup, engine, folder = await engine_on(tmp_path)
    a, b, c = Turn(p, "a1", "c1"), Turn(p, "a2", "c2"), Turn(p, "a3", "c3")
    await asyncio.wait_for(a.call(), 5)
    peer = asyncio.ensure_future(a_peers_call(p))
    await until(lambda: waiting() == ["peer"])
    b_first = b.call()
    await until(lambda: waiting() == ["agent", "peer"])
    c_first = c.call()
    await until(lambda: waiting() == ["agent", "agent", "peer"])
    await a.end()
    await asyncio.wait_for(b_first, 5)
    await b.end()
    await asyncio.wait_for(c_first, 5)
    await c.end()
    await asyncio.wait_for(peer, 5)
    sent = [e for e in engine.events if e.startswith("request:")]
    assert sent == ["request:c1", "request:c2", "request:c3", "request:peer"]


@pytest.mark.asyncio
async def test_a_peer_waiting_for_the_slot_does_not_hold_the_peer_doors_semaphore(tmp_path, peer_time):
    p, sup, engine, folder = await engine_on(tmp_path)
    q, other_sup, other_engine, _ = await engine_on(tmp_path, OTHER)
    providers = {ALIAS: p, OTHER: q}
    coord, svc = _peer_door(tmp_path, providers)
    gateway = _gateway(tmp_path, providers, inference_lock=coord._peer_inference_lock)
    a = Turn(p, "a1", "c1")
    await asyncio.wait_for(a.call(), 5)
    door = asyncio.ensure_future(coord.handle_inference_request(DOOR_PEER, uuid.uuid4().hex, "ping", provider=ALIAS))
    await until(lambda: waiting() == ["peer"])
    assert (await asyncio.wait_for(gateway.complete(OTHER, "ping"), 5)).text == "ok"
    assert not door.done()
    await a.end()
    await asyncio.wait_for(door, 5)
    answer = svc.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]
    assert answer.get("response") == "ok"


# --- a peer --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_peer_with_time_finds_the_owners_conversation_in_the_folder_before_its_call(tmp_path, peer_time):
    p, sup, engine, folder = await engine_on(tmp_path)
    a = Turn(p, "a1", "c1")
    await asyncio.wait_for(a.call(), 5)
    peer = asyncio.ensure_future(a_peers_call(p))
    await until(lambda: waiting() == ["peer"])
    await a.end()
    await asyncio.wait_for(peer, 5)
    assert engine.events == ["request:c1", "end:c1", "save", "save-end", "request:peer", "end:peer"]
    entry, = entries(folder)
    assert entry["conversation_id"] == "c1"


@pytest.mark.asyncio
async def test_a_peer_without_time_evicts_the_owners_conversation_unsaved_and_says_so(tmp_path, caplog):
    p, sup, engine, folder = await engine_on(tmp_path)  # no settings bound: the peer has no time
    a = Turn(p, "a1", "c1")
    await asyncio.wait_for(a.call(), 5)
    await a.end()
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await asyncio.wait_for(a_peers_call(p), 5)
    assert "save" not in engine.events and entries(folder) == []
    assert engine.events[-2:] == ["request:peer", "end:peer"]
    evicted, = lines(caplog, "evicted unsaved by peer")
    assert f"KV snapshot of agent a1 conversation c1 evicted unsaved by peer {NODE}" in evicted
    assert "re-reading it costs 2000 tokens" in evicted
    assert not lines(caplog, "KV snapshot save not waited for")


@pytest.mark.asyncio
async def test_a_drain_refuses_a_waiting_peer_with_the_draining_refusal(tmp_path, peer_time):
    p, sup, engine, folder = await engine_on(tmp_path)
    a = Turn(p, "a1", "c1")
    await asyncio.wait_for(a.call(), 5)
    peer = asyncio.ensure_future(a_peers_call(p))
    await until(lambda: waiting() == ["peer"])
    stop = asyncio.ensure_future(base_mod.stop_by_hand(sup))
    with pytest.raises(RuntimeError) as refused:
        await asyncio.wait_for(peer, 5)
    assert isinstance(base_mod.engine_refusal_in(refused.value), base_mod.EngineDrainingError)
    assert "request:peer" not in engine.events
    await asyncio.wait_for(stop, 5)
    await a.end()


@pytest.mark.asyncio
async def test_a_child_that_died_while_an_owners_turn_waited_is_asked_for_again_and_the_turn_keeps_its_place(tmp_path):
    p, sup, engine, folder = await engine_on(tmp_path)
    a, b, c = Turn(p, "a1", "c1"), Turn(p, "a2", "c2"), Turn(p, "a3", "c3")
    await asyncio.wait_for(a.call(), 5)
    b_first = b.call()
    await until(lambda: waiting() == ["agent"])
    c_first = c.call()
    await until(lambda: waiting() == ["agent", "agent"])
    first_child = sup._proc
    first_child.returncode = 1  # the child died
    await a.end()
    await asyncio.wait_for(b_first, 5)
    assert sup._proc is not first_child and sup._proc.returncode is None
    assert [e for e in engine.events if e.startswith("request:")][-1] == "request:c2"
    assert not c_first.done()
    await b.end()
    await asyncio.wait_for(c_first, 5)
    await c.end()


@pytest.mark.asyncio
async def test_a_return_after_a_peer_restores_its_snapshot_on_a_one_slot_alias_with_no_host_cache(
        tmp_path, peer_time):
    p, sup, engine, folder = await engine_on(tmp_path)
    a = Turn(p, "a1", "c1")
    await asyncio.wait_for(a.call(), 5)
    await a.end()
    await asyncio.wait_for(a_peers_call(p), 5)
    again = Turn(p, "a1", "c1")
    await asyncio.wait_for(again.call(), 5)
    await again.end()
    assert engine.events == ["request:c1", "end:c1", "save", "save-end", "request:peer", "end:peer",
                             "restore", "request:c1", "end:c1"]


@pytest.mark.asyncio
async def test_b_waits_for_a_s_turn_on_a_strata_alias_as_on_llama_server(tmp_path, caplog):
    p, sup, engine = await strata_on(tmp_path)
    a, b = Turn(p, "a1", "c1", STRATA), Turn(p, "a2", "c2", STRATA)
    engine.hold("c1")
    a_first = a.call()
    await until(lambda: "request:c1" in engine.events)
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        b_first = b.call()
        await until(lambda: waiting(STRATA) == ["agent"])
        assert holder(STRATA) == "agent"
        engine.release("c1")
        await a_first
        await asyncio.wait_for(a.call(), 5)  # A's later round goes before B's first call
        assert "request:c2" not in engine.events
        await a.end()
        await asyncio.wait_for(b_first, 5)
    # Nothing is saved or restored at the hand-over: Strata's own cache keeps A's conversation.
    assert engine.events == ["request:c1", "end:c1", "request:c1", "end:c1", "request:c2", "end:c2"]
    assert lines(caplog, f"slot queue[{STRATA}]: agent a2 conversation c2 waits, 0 ahead; "
                         f"the slot is held by agent a1 conversation c1 for")
    assert lines(caplog, f"slot queue[{STRATA}]: the slot passes from agent a1 conversation c1 (held")
    assert not lines(caplog, "evicted unsaved")
    await b.end()
    assert STRATA not in base_mod.slot_queue()


@pytest.mark.asyncio
async def test_a_persons_stop_takes_a_turn_out_of_the_slot_queue_of_a_strata_alias(tmp_path):
    p, sup, engine = await strata_on(tmp_path)
    a, b = Turn(p, "a1", "c1", STRATA), Turn(p, "a2", "c2", STRATA)
    await asyncio.wait_for(a.call(), 5)
    b_first = b.call()
    await until(lambda: waiting(STRATA) == ["agent"])
    base_mod.leave_card_queue("c2")
    with pytest.raises(RuntimeError) as stopped:
        await asyncio.wait_for(b_first, 5)
    assert isinstance(base_mod.engine_refusal_in(stopped.value), base_mod.TakenOutOfTheSlotQueue)
    assert waiting(STRATA) == [] and "request:c2" not in engine.events
    await a.end()
    await b.end()


@pytest.mark.asyncio
async def test_a_named_background_call_is_skipped_while_a_turn_holds_the_slot_of_a_strata_alias(tmp_path):
    p, sup, engine = await strata_on(tmp_path)
    a = Turn(p, "a1", "c1", STRATA)
    await asyncio.wait_for(a.call(), 5)
    with pytest.raises(RuntimeError) as skipped:
        with base_mod.a_background_call("skill_reflection"):
            await asyncio.wait_for(p.generate_response("reflect"), 1)
    assert isinstance(base_mod.engine_refusal_in(skipped.value), base_mod.BackgroundCallSkipped)
    assert "request:background" not in engine.events
    await a.end()


@pytest.mark.asyncio
async def test_the_owners_chat_goes_in_between_two_calls_of_a_turn_on_a_strata_alias(tmp_path):
    p, sup, engine = await strata_on(tmp_path)
    a, b = Turn(p, "a1", "c1", STRATA), Turn(p, "a2", "c2", STRATA)
    await asyncio.wait_for(a.call(), 5)
    engine.hold("c1")
    a_second = a.call()
    await until(lambda: engine.events.count("request:c1") == 2)
    b_first = b.call()
    await until(lambda: waiting(STRATA) == ["agent"])
    first = asyncio.ensure_future(chat(p, "chat1"))
    await until(lambda: going_in_between(STRATA) == "owner")
    await still_waits(lambda: "request:chat1" in engine.events)  # A's call in the engine first
    engine.release("c1")
    await asyncio.wait_for(a_second, 5)
    await asyncio.wait_for(first, 5)
    await asyncio.wait_for(a.call(), 5)
    assert engine.events[-6:] == ["request:c1", "end:c1", "request:chat1", "end:chat1", "request:c1", "end:c1"]
    assert "save" not in engine.events and "restore" not in engine.events
    assert holder(STRATA) == "agent" and waiting(STRATA) == ["agent"] and not b_first.done()
    await a.end()
    await asyncio.wait_for(b_first, 5)
    await b.end()


@pytest.mark.asyncio
async def test_a_peer_waits_for_a_turn_on_a_strata_alias_and_nothing_is_evicted_unsaved(tmp_path, peer_time, caplog):
    p, sup, engine = await strata_on(tmp_path)
    a = Turn(p, "a1", "c1", STRATA)
    await asyncio.wait_for(a.call(), 5)
    peer = asyncio.ensure_future(a_peers_call(p))
    await until(lambda: waiting(STRATA) == ["peer"])
    assert "request:peer" not in engine.events
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await a.end()
        await asyncio.wait_for(peer, 5)
    assert engine.events == ["request:c1", "end:c1", "request:peer", "end:peer"]
    assert not lines(caplog, "evicted unsaved")


@pytest.mark.asyncio
async def test_a_peer_at_its_deadline_goes_in_between_two_calls_of_a_turn_on_a_strata_alias(
        tmp_path, clock, peer_time, caplog):
    p, sup, engine = await strata_on(tmp_path)
    a, b = Turn(p, "a1", "c1", STRATA), Turn(p, "a2", "c2", STRATA)
    await asyncio.wait_for(a.call(), 5)
    engine.hold("c1")
    a_second = a.call()
    await until(lambda: engine.events.count("request:c1") == 2)
    b_first = b.call()
    await until(lambda: waiting(STRATA) == ["agent"])
    arrival = clock.now
    peer = asyncio.ensure_future(a_peers_call(p))
    await until(lambda: waiting(STRATA) == ["agent", "peer"])
    # The save's allowance is taken off the deadline here too, though Strata saves nothing.
    clock.now = arrival + 1200 - ALLOWANCE
    await until(lambda: waiting(STRATA) == ["agent"])
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        engine.release("c1")
        await asyncio.wait_for(a_second, 5)
        await asyncio.wait_for(peer, 5)
        await asyncio.wait_for(a.call(), 5)
    assert engine.events[-6:] == ["request:c1", "end:c1", "request:peer", "end:peer", "request:c1", "end:c1"]
    assert "save" not in engine.events and "restore" not in engine.events
    assert not lines(caplog, "evicted unsaved")
    assert holder(STRATA) == "agent" and waiting(STRATA) == ["agent"] and not b_first.done()
    await a.end()
    await asyncio.wait_for(b_first, 5)
    await b.end()


@pytest.mark.asyncio
async def test_a_peer_through_the_door_and_a_gateway_call_wait_behind_a_turn_on_a_strata_alias(tmp_path, peer_time):
    p, sup, engine = await strata_on(tmp_path)
    providers = {STRATA: p}
    coord, svc = _peer_door(tmp_path, providers)
    gateway = _gateway(tmp_path, providers, inference_lock=coord._peer_inference_lock)
    a = Turn(p, "a1", "c1", STRATA)
    await asyncio.wait_for(a.call(), 5)
    door = asyncio.ensure_future(coord.handle_inference_request(DOOR_PEER, uuid.uuid4().hex, "ping", provider=STRATA))
    await until(lambda: waiting(STRATA) == ["peer"])
    assert not coord._peer_inference_lock.locked()  # a peer in the slot queue holds no semaphore
    via_gateway = asyncio.ensure_future(gateway.complete(STRATA, "ping"))
    await until(lambda: sorted(waiting(STRATA)) == ["gateway", "peer"])
    assert not door.done() and not via_gateway.done()
    await a.end()
    await asyncio.wait_for(door, 5)
    assert (await asyncio.wait_for(via_gateway, 5)).text == "ok"
    # The owner's own work goes before a peer (point 6): the gateway call first.
    sent = [e for e in engine.events if e.startswith("request:")]
    assert sent == ["request:c1", "request:gateway", "request:peer"]


# --- a peer's deadline, and going in between -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_peers_deadline_is_the_smaller_wait_counted_from_its_arrival_at_the_door(tmp_path, clock, caplog):
    """swap_wait_s 1800 and remote_inference_timeout 1200: the peer stops waiting for A's turn at
    1,138 s after its arrival at the door (1200 less the save's allowance), and the time it then
    spends on the door's semaphore is inside the deadline the save is bounded by."""
    base_mod.bind_local_engine_settings(Settings(1800, 1200))
    p, sup, engine, folder = await engine_on(tmp_path)
    coord, svc = _peer_door(tmp_path, {ALIAS: p})
    a = Turn(p, "a1", "c1")
    await asyncio.wait_for(a.call(), 5)
    arrival = clock.now
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        door = asyncio.ensure_future(coord.handle_inference_request(DOOR_PEER, uuid.uuid4().hex, "ping", provider=ALIAS))
        await until(lambda: waiting() == ["peer"])
        await coord._peer_inference_lock.acquire()  # someone sits on the semaphore
        clock.now = arrival + 1200 - ALLOWANCE - 0.5
        await still_waits(lambda: waiting() != ["peer"])
        clock.now = arrival + 1200 - ALLOWANCE
        await until(lambda: waiting() == [])
        clock.now = arrival + 1170  # 32 s on the semaphore
        coord._peer_inference_lock.release()
        await asyncio.wait_for(door, 5)
    assert lines(caplog, f"slot queue[{ALIAS}]: peer {DOOR_PEER}, its deadline less the save's allowance reached, "
                         "goes in between two calls of agent a1 conversation c1 after a wait of 1138 s")
    decision, = lines(caplog, "KV snapshot decision at a switch")
    left = float(re.search(r"([\d.]+) s of the bound left", decision).group(1))
    assert 27.0 < left <= 28.0  # 1200 - 1170 - the switch margin of 2 s
    await a.end()


@pytest.mark.asyncio
async def test_a_peer_at_its_deadline_goes_in_between_two_calls_of_a_s_turn_saving_first(tmp_path, clock, peer_time):
    p, sup, engine, folder = await engine_on(tmp_path)
    a, b = Turn(p, "a1", "c1"), Turn(p, "a2", "c2")
    await asyncio.wait_for(a.call(), 5)
    engine.hold("c1")
    a_second = a.call()
    await until(lambda: engine.events.count("request:c1") == 2)
    b_first = b.call()
    await until(lambda: waiting() == ["agent"])
    arrival = clock.now
    peer = asyncio.ensure_future(a_peers_call(p))
    await until(lambda: waiting() == ["agent", "peer"])
    clock.now = arrival + 1200 - ALLOWANCE
    await until(lambda: waiting() == ["agent"])
    await still_waits(lambda: "save" in engine.events)  # the call A has in the engine first
    engine.release("c1")
    await asyncio.wait_for(a_second, 5)
    await asyncio.wait_for(peer, 5)
    await asyncio.wait_for(a.call(), 5)
    assert engine.events[-9:] == ["request:c1", "end:c1", "save", "save-end", "request:peer", "end:peer",
                                  "restore", "request:c1", "end:c1"]
    entry, = entries(folder)
    assert entry["conversation_id"] == "c1"
    assert holder() == "agent" and waiting() == ["agent"] and not b_first.done()
    await a.end()
    await asyncio.wait_for(b_first, 5)
    await b.end()


@pytest.mark.asyncio
async def test_a_call_in_the_engine_past_the_allowance_lets_the_peer_evict_a_s_conversation_unsaved(
        tmp_path, clock, peer_time, caplog):
    p, sup, engine, folder = await engine_on(tmp_path)
    a = Turn(p, "a1", "c1")
    await asyncio.wait_for(a.call(), 5)
    engine.hold("c1")
    a_second = a.call()
    await until(lambda: engine.events.count("request:c1") == 2)
    arrival = clock.now
    peer = asyncio.ensure_future(a_peers_call(p))
    await until(lambda: waiting() == ["peer"])
    clock.now = arrival + 1200 - ALLOWANCE
    await until(lambda: waiting() == [])
    clock.now = arrival + 1201  # A's call ran past the allowance
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        engine.release("c1")
        await asyncio.wait_for(a_second, 5)
        await asyncio.wait_for(peer, 5)
    assert "save" not in engine.events and entries(folder) == []
    assert engine.events[-2:] == ["request:peer", "end:peer"]
    evicted, = lines(caplog, "evicted unsaved by peer")
    assert f"evicted unsaved by peer {NODE}" in evicted and "2000 tokens" in evicted
    await a.end()


# --- calls with no hold --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_named_background_call_is_skipped_while_a_turn_holds_the_slot_and_runs_when_it_is_free(tmp_path):
    p, sup, engine, folder = await engine_on(tmp_path)
    a = Turn(p, "a1", "c1")
    await asyncio.wait_for(a.call(), 5)
    with pytest.raises(RuntimeError) as skipped:
        with base_mod.a_background_call("skill_reflection"):
            await asyncio.wait_for(p.generate_response("reflect"), 1)
    assert isinstance(base_mod.engine_refusal_in(skipped.value), base_mod.BackgroundCallSkipped)
    row = base_mod.last_background_skip()["skill_reflection"]
    assert row["reason"] == f"the slot of '{ALIAS}' is held by agent a1 conversation c1"
    assert "request:background" not in engine.events
    await a.end()
    with base_mod.a_background_call("skill_reflection"):
        assert await asyncio.wait_for(p.generate_response("reflect"), 5) == "ok"


@pytest.mark.asyncio
async def test_an_unmarked_call_with_no_hold_waits_behind_a_s_turn(tmp_path):
    p, sup, engine, folder = await engine_on(tmp_path)
    a = Turn(p, "a1", "c1")
    await asyncio.wait_for(a.call(), 5)
    extraction = asyncio.ensure_future(p.generate_response("score this conversation"))
    await until(lambda: waiting() == [None])
    assert "request:background" not in engine.events
    await a.end()
    await asyncio.wait_for(extraction, 5)
    assert engine.events[-2:] == ["request:background", "end:background"]


# --- the owner's chat ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_owners_chat_goes_in_after_a_s_call_in_the_engine_and_gives_the_slot_back_to_a(tmp_path):
    p, sup, engine, folder = await engine_on(tmp_path)
    a, b = Turn(p, "a1", "c1"), Turn(p, "a2", "c2")
    await asyncio.wait_for(a.call(), 5)
    engine.hold("c1")
    a_second = a.call()
    await until(lambda: engine.events.count("request:c1") == 2)
    b_first = b.call()
    await until(lambda: waiting() == ["agent"])
    first = asyncio.ensure_future(chat(p, "chat1"))
    # The first chat takes its place in between at once, and waits there for A's call in the engine.
    await until(lambda: going_in_between() == "owner")
    second = asyncio.ensure_future(chat(p, "chat2"))
    await until(lambda: waiting() == ["agent", "owner"])
    await still_waits(lambda: "request:chat1" in engine.events)
    engine.release("c1")
    await asyncio.wait_for(a_second, 5)
    await asyncio.wait_for(first, 5)
    await asyncio.wait_for(second, 5)
    await asyncio.wait_for(a.call(), 5)
    assert engine.events[-11:] == ["request:c1", "end:c1", "save", "save-end", "request:chat1", "end:chat1",
                                   "request:chat2", "end:chat2", "restore", "request:c1", "end:c1"]
    assert holder() == "agent" and waiting() == ["agent"] and not b_first.done()
    await a.end()
    await asyncio.wait_for(b_first, 5)
    await b.end()
