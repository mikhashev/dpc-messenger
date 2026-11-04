"""A client that leaves a start does not take the engine from whoever else wants it.

ADR-041 D4, amendment 2026-11-04. A client's connection closing cancels its call, and
a cold start under that call is stopped with it. That is right for a start nobody
else wants. It is not right for a load somebody else stands on: an agent's turn
waiting for the same alias, or a second client, would lose the minutes already spent,
and a client with a short timeout repeating against a slow load could do that again.

Real supervisors and their real `ensure_running`, with the fake child of
`test_one_local_engine_holds_the_card.py`: `_health_ok` is the slow part of a load.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import (
    KV_LADDER,
    LlamaServerSupervisor,
    _CARD_CLAIMS,
    _CARD_QUEUE,
    LlamaServerError,
    card_held_for,
    card_holders,
)
from dpc_client_core.providers import LlamaServerProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from tests.test_a_client_that_leaves_the_door_ends_the_call_and_nothing_is_generated_again import (
    CHAT_PATH,
    ALIAS,
    _chat,
    _Client,
    _Engine,
    _service,
)
from tests.test_one_local_engine_holds_the_card import GGUF, _fake_child, _llama, _strata, card  # noqa: F401
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import _key, _running

LOAD_S = 0.5


def _slow(sup):
    """`sup` with a child that takes a while to load; the list of children it spawned."""
    return _fake_child(sup, health_delay=LOAD_S)


def _both(tmp_path):
    return {"llama-server": _llama(), "strata": _strata(tmp_path / "s")}


@pytest.fixture(params=["llama-server", "strata"])
def engine(request, tmp_path):
    sup = _both(tmp_path)[request.param]
    _ACTIVE_SUPERVISORS[sup.alias] = sup
    return sup


async def _until(predicate, timeout=5.0):
    async def looking():
        while not predicate():
            await asyncio.sleep(0.002)

    await asyncio.wait_for(looking(), timeout)


async def _let_go(*tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _gone(task):
    task.cancel()
    await asyncio.wait({task})
    assert task.cancelled()


@pytest.mark.asyncio
async def test_alone_a_client_that_leaves_stops_the_load_and_lets_go_of_the_card(engine):
    spawned = _slow(engine)
    first = asyncio.ensure_future(engine.ensure_running())
    await _until(lambda: spawned)
    await _gone(first)
    assert engine._proc is None, "a child nobody wants was left loading"
    assert not engine._start_lock.locked()
    assert engine not in _CARD_CLAIMS
    # The next call starts it again, and that one is seen through.
    assert await engine.ensure_running()
    assert len(spawned) == 2


@pytest.mark.asyncio
async def test_a_second_caller_at_the_same_start_gets_the_load_the_first_began(engine):
    spawned = _slow(engine)
    first = asyncio.ensure_future(engine.ensure_running())
    await _until(lambda: spawned)
    second = asyncio.ensure_future(engine.ensure_running())
    await asyncio.sleep(0.02)
    await _gone(first)
    assert await asyncio.wait_for(second, 5)
    assert len(spawned) == 1, "the load was thrown away and begun again"
    assert spawned[0].returncode is None
    assert [holder["alias"] for holder in card_holders()] == [engine.alias]
    assert not engine._start_lock.locked()


@pytest.mark.asyncio
async def test_an_agents_turn_standing_on_the_alias_keeps_the_load_though_it_has_not_asked_yet(engine):
    """Between two rounds, or behind a semaphore: it has not reached the start yet."""
    spawned = _slow(engine)
    first = asyncio.ensure_future(engine.ensure_running())
    await _until(lambda: spawned)
    next_round = asyncio.Event()

    async def turn():
        async with card_held_for(engine.alias, "accepted_request", agent_id="agent", conversation_id="c"):
            await next_round.wait()
            return await engine.ensure_running()

    agent = asyncio.ensure_future(turn())
    await asyncio.sleep(0.02)
    await _gone(first)
    next_round.set()
    assert await asyncio.wait_for(agent, 5)
    assert len(spawned) == 1
    assert spawned[0].returncode is None


@pytest.mark.asyncio
async def test_a_client_repeating_on_a_short_timeout_never_kills_the_load_for_the_turn_behind_it(engine):
    spawned = _slow(engine)
    client = asyncio.ensure_future(engine.ensure_running())
    await _until(lambda: spawned)
    agent = asyncio.ensure_future(engine.ensure_running())
    await asyncio.sleep(0.01)
    for _ in range(3):
        await _gone(client)
        client = asyncio.ensure_future(engine.ensure_running())
        await asyncio.sleep(0.03)
    assert await asyncio.wait_for(agent, 5)
    assert len(spawned) == 1
    await _let_go(client)


@pytest.mark.asyncio
async def test_a_kept_load_that_fails_lets_the_one_waiting_try_for_itself(tmp_path):
    sup = _llama("failing", cache_type_k="q8_0", cache_type_v="q8_0")
    spawned = _slow(sup)
    real_health = sup._health_ok
    fail_once = [True]

    async def health():
        await asyncio.sleep(LOAD_S)
        if fail_once:
            fail_once.clear()
            raise LlamaServerError("the child could not load")
        return await real_health()

    sup._health_ok = health
    first = asyncio.ensure_future(sup.ensure_running())
    await _until(lambda: spawned)
    second = asyncio.ensure_future(sup.ensure_running())
    await asyncio.sleep(0.02)
    await _gone(first)
    assert await asyncio.wait_for(second, 5)
    assert len(spawned) == 2


@pytest.mark.asyncio
async def test_a_stop_ends_a_kept_load(engine):
    spawned = _slow(engine)
    first = asyncio.ensure_future(engine.ensure_running())
    await _until(lambda: spawned)
    second = asyncio.ensure_future(engine.ensure_running())
    await asyncio.sleep(0.02)
    await _gone(first)
    await engine.stop()
    await _let_go(second)
    await asyncio.sleep(0.05)
    assert engine._start_lock.locked() is False, "the kept load still holds the start"
    assert engine._detached_start is None


@pytest.mark.asyncio
async def test_a_queue_place_behind_a_load_that_was_alone_gets_its_own_engine(tmp_path, monkeypatch):
    from dpc_client_core.settings import Settings

    settings = Settings(tmp_path / "home-settings")
    settings.set_local_engine_swap(True, 60, 0)
    base_mod.bind_local_engine_settings(settings)
    monkeypatch.setattr(base_mod, "_LAST_SWAP", None)
    monkeypatch.setattr(base_mod, "_SWAP_POLL_S", 0.005)
    try:
        ahead, behind = _strata(tmp_path / "a", "ahead"), _strata(tmp_path / "b", "behind")
        _ACTIVE_SUPERVISORS[ahead.alias], _ACTIVE_SUPERVISORS[behind.alias] = ahead, behind
        spawned = _slow(ahead)
        first = asyncio.ensure_future(ahead.ensure_running())
        await _until(lambda: spawned)
        place = asyncio.ensure_future(behind.ensure_running())
        await asyncio.sleep(0.02)
        await _gone(first)
        assert await asyncio.wait_for(place, 8)
        assert [holder["alias"] for holder in card_holders()] == ["behind"]
        assert not _CARD_QUEUE
    finally:
        base_mod.bind_local_engine_settings(None)
        base_mod.bind_owner_loop(None)


@pytest.mark.asyncio
async def test_two_clients_at_the_door_the_first_leaves_the_cold_start_and_the_second_is_answered(tmp_path):
    """Through the listener, the semaphore and the card hold: the second client waits on
    the semaphore the first holds, so it is the hold that says somebody still wants it."""
    async with _Engine(content=("the ", "answer")) as engine_double:
        provider = LlamaServerProvider(ALIAS, {"type": "llamacpp_server", "gguf_path": "D:/m/x.gguf",
                                               "cache_type_k": "q8_0", "cache_type_v": "q8_0",
                                               "context_window": 4096})
        sup = provider.supervisor
        spawned = _slow(sup)
        sup.port = engine_double.port
        try:
            service = _service(tmp_path, provider)
            async with _running(tmp_path, service) as (server, ledger):
                key = _key(tmp_path)
                first = _Client(server.port, key, CHAT_PATH, _chat(stream=True))
                await first.send()
                await _until(lambda: spawned)
                second = _Client(server.port, key, CHAT_PATH, _chat(stream=True))
                await second.send()
                await asyncio.sleep(0.05)
                await first.leave()
                seen = await second.read_until(b"[DONE]")
                await second.leave()
                assert b"answer" in seen
                assert len(spawned) == 1, "the second client's load was thrown away"
                (row,) = list(ledger.rows())
                assert row["counts_source"] == "engine"
        finally:
            sup._proc = None
            _ACTIVE_SUPERVISORS.pop(ALIAS, None)


@pytest.mark.asyncio
async def test_a_caller_standing_on_its_own_hold_is_still_alone(engine):
    """A door's call holds the alias itself: that stand is not somebody else wanting it."""
    spawned = _slow(engine)

    async def call():
        async with card_held_for(engine.alias, "accepted_request", caller="client"):
            return await engine.ensure_running()

    first = asyncio.ensure_future(call())
    await _until(lambda: spawned)
    await _gone(first)
    assert engine._proc is None, "its own hold kept a load nobody wants"


@pytest.mark.asyncio
async def test_a_caller_that_leaves_before_the_spawn_keeps_the_card_for_the_load_that_goes_on(engine):
    """Nothing is running yet, and the card is claimed for the load that is on its way."""
    spawned = _slow(engine)
    real_spawn = engine._spawn
    let_it_spawn = asyncio.Event()
    spawning = asyncio.Event()

    async def slow_spawn(cmd, env):
        spawning.set()
        await let_it_spawn.wait()
        return await real_spawn(cmd, env)

    engine._spawn = slow_spawn
    first = asyncio.ensure_future(engine.ensure_running())
    await asyncio.wait_for(spawning.wait(), 5)
    second = asyncio.ensure_future(engine.ensure_running())
    await asyncio.sleep(0.02)
    await _gone(first)
    assert engine in _CARD_CLAIMS, "the card was let go under a load that goes on"
    let_it_spawn.set()
    assert await asyncio.wait_for(second, 5)
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_a_request_still_in_line_behind_it_on_the_same_alias_keeps_the_load(tmp_path, monkeypatch):
    """Through the line and not at the lock yet: it looks again only at the next poll."""
    from dpc_client_core.settings import Settings

    settings = Settings(tmp_path / "home-settings")
    settings.set_local_engine_swap(True, 60, 0)
    base_mod.bind_local_engine_settings(settings)
    monkeypatch.setattr(base_mod, "_LAST_SWAP", None)
    monkeypatch.setattr(base_mod, "_SWAP_POLL_S", 0.3)
    try:
        sup = _strata(tmp_path / "a", "alias")
        _ACTIVE_SUPERVISORS[sup.alias] = sup
        spawned = _slow(sup)
        first = asyncio.ensure_future(sup.ensure_running())
        second = asyncio.ensure_future(sup.ensure_running())
        await _until(lambda: spawned)
        assert sup._start_waiters == 0 and _CARD_QUEUE, "the case under test: in line, and not at the lock"
        await _gone(first)
        assert await asyncio.wait_for(second, 8)
        assert len(spawned) == 1
    finally:
        base_mod.bind_local_engine_settings(None)
        base_mod.bind_owner_loop(None)


def _failing_rungs(sup, fail_first, *log_lines):
    """`sup` with a child whose first `fail_first` loads fail as an out-of-memory child does."""
    spawned = _slow(sup)
    real_health = sup._health_ok
    left = [fail_first]

    async def health():
        await asyncio.sleep(LOAD_S)
        if left[0] > 0:
            left[0] -= 1
            raise LlamaServerError("the child could not load", list(log_lines))
        return await real_health()

    sup._health_ok = health
    return spawned


def _rungs_tried(sup):
    tried = []
    real_launch = sup._launch

    async def launch(binary, cache_type=None):
        tried.append(cache_type)
        return await real_launch(binary, cache_type=cache_type)

    sup._launch = launch
    return tried


@pytest.mark.asyncio
async def test_a_kept_load_steps_down_its_ladder_as_its_caller_would_have(tmp_path):
    """A failed rung stops its own child, in a stop of its own task: that stop is the load's
    own and must not end it, or the waiter starts again from the first rung."""
    sup = LlamaServerSupervisor("ladder", {"gguf_path": GGUF})
    spawned = _failing_rungs(sup, 1, "failed to allocate: out of memory")
    tried = _rungs_tried(sup)
    first = asyncio.ensure_future(sup.ensure_running())
    await _until(lambda: spawned)
    second = asyncio.ensure_future(sup.ensure_running())
    await asyncio.sleep(0.02)
    await _gone(first)
    assert await asyncio.wait_for(second, 5)
    assert tried == list(KV_LADDER[:2]), "not the ladder: the next rung was never reached by the kept load"
    assert len(spawned) == 2


@pytest.mark.asyncio
async def test_a_kept_load_that_fails_says_why_and_hands_over_the_lock(engine, caplog):
    """The load's own error and not a cancellation, in the log since no caller is left to get
    it; then the one waiting tries for itself."""
    spawned = _failing_rungs(engine, 1, "the real reason")
    first = asyncio.ensure_future(engine.ensure_running())
    await _until(lambda: spawned)
    second = asyncio.ensure_future(engine.ensure_running())
    await asyncio.sleep(0.02)
    await _gone(first)
    kept = engine._detached_start
    assert kept is not None
    with caplog.at_level("WARNING"):
        await asyncio.wait({kept})
        assert not kept.cancelled(), "the load's own stop cancelled it, and its error is gone"
        assert isinstance(kept.exception(), LlamaServerError)
        assert await asyncio.wait_for(second, 5)
    assert "the child could not load" in caplog.text
    assert len(spawned) == 2


@pytest.mark.asyncio
async def test_a_kept_load_a_new_pin_fails_rolls_back_as_its_caller_would_have(tmp_path, monkeypatch):
    sup = LlamaServerSupervisor("rollback", {"gguf_path": GGUF, "cache_type_k": "q8_0", "cache_type_v": "q8_0"})
    spawned = _slow(sup)
    real_health = sup._health_ok
    left = [1]

    async def health():
        await asyncio.sleep(LOAD_S)
        if left[0]:
            left[0] = 0
            raise base_mod._build_fault(LlamaServerError("this build cannot start"))
        return await real_health()

    sup._health_ok = health
    monkeypatch.setattr(base_mod, "_is_pin", lambda root: True)
    sup._plan_pin_start = lambda current: {"start": Path("new"), "fallback": Path("old"), "retained": Path("old")}
    sup._roll_back = AsyncMock()
    sup._note_pass = AsyncMock()
    first = asyncio.ensure_future(sup.ensure_running())
    await _until(lambda: spawned)
    second = asyncio.ensure_future(sup.ensure_running())
    await asyncio.sleep(0.02)
    await _gone(first)
    assert await asyncio.wait_for(second, 5)
    sup._roll_back.assert_awaited_once()


async def _stopped_and_let_go(sup, spawned):
    await _until(lambda: sup._detached_start is None)
    assert sup._proc is None, "a load nobody wants is still up"
    assert sup not in _CARD_CLAIMS, "it still holds the card"
    assert not sup._start_lock.locked()
    assert len(spawned) == 1 and spawned[0].returncode is not None


@pytest.mark.asyncio
async def test_a_kept_load_stops_when_the_last_who_wanted_it_leaves(engine):
    spawned = _slow(engine)
    first = asyncio.ensure_future(engine.ensure_running())
    await _until(lambda: spawned)
    second = asyncio.ensure_future(engine.ensure_running())
    await asyncio.sleep(0.02)
    await _gone(first)
    assert engine._detached_start is not None
    await _gone(second)
    await _stopped_and_let_go(engine, spawned)


@pytest.mark.asyncio
async def test_two_clients_leaving_in_one_tick_stop_the_load_they_both_left(engine):
    """The second still counts as wanting it when the first is judged."""
    spawned = _slow(engine)
    first = asyncio.ensure_future(engine.ensure_running())
    await _until(lambda: spawned)
    second = asyncio.ensure_future(engine.ensure_running())
    await asyncio.sleep(0.02)
    first.cancel()
    second.cancel()
    await asyncio.wait({first, second})
    await _stopped_and_let_go(engine, spawned)


@pytest.mark.asyncio
async def test_a_kept_load_stops_when_the_turn_standing_on_it_ends(engine):
    spawned = _slow(engine)
    first = asyncio.ensure_future(engine.ensure_running())
    await _until(lambda: spawned)
    ends = asyncio.Event()

    async def turn():
        async with card_held_for(engine.alias, "accepted_request", agent_id="agent", conversation_id="c"):
            await ends.wait()

    agent = asyncio.ensure_future(turn())
    await asyncio.sleep(0.02)
    await _gone(first)
    ends.set()
    await agent
    await _stopped_and_let_go(engine, spawned)


@pytest.mark.asyncio
async def test_a_request_that_arrives_after_the_others_left_keeps_a_load_that_is_still_kept(engine):
    spawned = _slow(engine)
    first = asyncio.ensure_future(engine.ensure_running())
    await _until(lambda: spawned)
    second = asyncio.ensure_future(engine.ensure_running())
    await asyncio.sleep(0.02)
    await _gone(first)
    third = asyncio.ensure_future(engine.ensure_running())
    await asyncio.sleep(0)
    await _gone(second)
    assert await asyncio.wait_for(third, 5)
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_a_kept_load_ended_before_its_spawn_lets_go_of_the_card_it_claimed(engine):
    """No child yet, so no stop of one lets go of it: the load's own end has to."""
    _slow(engine)
    spawning = asyncio.Event()

    async def never_spawns(cmd, env):
        spawning.set()
        await asyncio.Event().wait()

    engine._spawn = never_spawns
    first = asyncio.ensure_future(engine.ensure_running())
    await asyncio.wait_for(spawning.wait(), 5)
    second = asyncio.ensure_future(engine.ensure_running())
    await asyncio.sleep(0.02)
    await _gone(first)
    assert engine in _CARD_CLAIMS
    await _gone(second)
    await _until(lambda: engine._detached_start is None)
    assert engine not in _CARD_CLAIMS, "the card stays claimed for a load that is over"
    assert not engine._start_lock.locked()
