"""A peer may swap an idle card when the owner allows swaps (ADR-044, amendment 2026-11-06,
Mike's call).

It replaces rule 2 of the idle swap and the first half of point 7 of the card queue ("a peer
never swaps"); a peer still takes no place in the queue and never waits in it. With
`swap_when_idle` on, a peer's request for a local alias that is not on the card swaps the card
under the owner's own conditions: the holder is not busy (no `accepted_request`, outside any
`caller_window`) and the card queue is empty. Otherwise, and with the switch off, the peer is
refused at once, in words that say why and name no agent, chat or caller.

Real supervisors and their real `ensure_running`, a real `StrataProvider` for the peer policy,
the fakes of `test_one_local_engine_holds_the_card.py`. Every home is under tmp_path.
"""

import asyncio
import logging

import pytest

from dpc_client_core import process_registry as procreg
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers import strata_supervisor as strata_mod
from dpc_client_core.managers.llama_server_supervisor import (
    CardHeldError,
    CardQueueRefusal,
    card_held_for,
    card_holders,
    serving_a_peer,
)
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from dpc_client_core.providers.strata_provider import StartingRefusal, StrataProvider

from tests.test_an_owners_start_swaps_out_an_idle_local_engine_that_holds_the_card import (  # noqa: F401
    _holder_untouched,
    _running_pair,
    settings,
    swap_on,
)
from tests.test_a_request_holds_the_card_from_its_first_round_to_its_last import _another_request
from tests.test_one_local_engine_holds_the_card import _fake_child, _llama, _strata, card  # noqa: F401
from tests.test_strata_supervisor import _install

PEER = "dpc-node-" + "b" * 32
PIDS = iter(range(7_960_001, 7_961_000))


@pytest.fixture
def said(monkeypatch):
    """Every stop the supervisors announce to the registry, as (reason, stop_kind)."""
    announced = []
    registry = procreg.get_registry()
    real = registry.note_stop_sent

    def spy(pid, reason, stop=None):
        announced.append((reason, stop))
        return real(pid, reason, stop)

    monkeypatch.setattr(registry, "note_stop_sent", spy)
    return announced


@pytest.fixture(autouse=True)
def no_windows(monkeypatch):
    monkeypatch.setattr(base_mod, "_last_caller_call_at", {})


def _with_a_pid(supervisor):
    supervisor._proc.pid = next(PIDS)
    return supervisor


def _provider(tmp_path, alias="flash") -> StrataProvider:
    """A stopped Strata alias as the peer door sees it, its launcher a fake child."""
    provider = StrataProvider(alias, {
        "type": "strata", "alias": alias, "model": "qwen3.8-flash-next-iq3_xxs",
        "strata_install_dir": str(_install(tmp_path)), "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240, "context_window": 200000,
        "engine_watch_interval_s": 3600, "vram_release_timeout_s": 0.05,
    })
    _fake_child(provider.supervisor)
    _ACTIVE_SUPERVISORS[alias] = provider.supervisor
    return provider


def _in_line() -> list:
    return [place.alias for place in base_mod._CARD_QUEUE]


def _alive(sup) -> bool:
    return sup._proc is not None and sup._proc.returncode is None


def _slow_stop(sup):
    """Hold `sup`'s stop until the test lets it go; `reached` says it got there."""
    reached, let_go = asyncio.Event(), asyncio.Event()
    real = sup._stop_child

    async def stopping():
        reached.set()
        await let_go.wait()
        await real()

    sup._stop_child = stopping
    return reached, let_go


async def _until(predicate, timeout=5.0):
    async def looking():
        while not predicate():
            await asyncio.sleep(0.002)

    await asyncio.wait_for(looking(), timeout)


class TestSwapOnTheHolderIdleTheQueueEmpty:
    @pytest.mark.asyncio
    async def test_a_peer_call_stops_the_holder_as_a_swap_and_the_alias_starts_in_no_line(self, tmp_path, said):
        holder, new = await _running_pair(tmp_path, "strata")
        _with_a_pid(holder)
        reached, let_go = _slow_stop(holder)

        with serving_a_peer(PEER):
            start = asyncio.ensure_future(new.ensure_running())
            await asyncio.wait_for(reached.wait(), 5)
            assert _in_line() == [], "a peer takes no place, not even for the length of its swap"
            assert holder._swap_for == "qwen27" and holder._draining
            let_go.set()
            props = await asyncio.wait_for(start, 5)

        assert props == {"total_slots": 1}
        assert said == [("stopped", "swap")]
        assert holder._proc is None
        assert [h["alias"] for h in card_holders()] == ["qwen27"]
        assert (base_mod.last_swap()["from"], base_mod.last_swap()["to"]) == ("flash", "qwen27")
        assert _in_line() == []

    @pytest.mark.asyncio
    async def test_the_peer_policy_of_a_stopped_strata_alias_swaps_and_answers_loading_meanwhile(
        self, tmp_path, said, caplog,
    ):
        holder = _llama("qwen27")
        await holder.ensure_running()
        _ACTIVE_SUPERVISORS[holder.alias] = holder
        _with_a_pid(holder)
        provider = _provider(tmp_path)
        sup = provider.supervisor
        reached, let_go = _slow_stop(holder)

        with caplog.at_level(logging.INFO):
            answer = await provider.peer_refusal(PEER)
        assert isinstance(answer, StartingRefusal) and "loading" in answer
        assert answer.refusal_code == "provider_loading", "a guest agent waits on this code"
        assert sup.peer_start_task is not None
        await asyncio.wait_for(reached.wait(), 5)
        # While the swap and the start run, the next peer is answered loading and begins no second start.
        again = await provider.peer_refusal("peer-2")
        assert isinstance(again, StartingRefusal)
        assert any("stops the holder of the card first" in r.getMessage() for r in caplog.records)
        let_go.set()
        await asyncio.wait_for(sup.peer_start_task, 5)

        assert said == [("stopped", "swap")]
        assert holder._proc is None and _alive(sup)
        assert [h["alias"] for h in card_holders()] == ["flash"]
        assert base_mod.last_swap()["to"] == "flash"
        assert await provider.peer_refusal(PEER) is None, "running now: the peer is served"

    @pytest.mark.asyncio
    async def test_the_free_vram_bar_is_not_read_while_the_holder_is_what_takes_it(self, tmp_path, monkeypatch):
        holder = _llama("qwen27")
        await holder.ensure_running()
        _ACTIVE_SUPERVISORS[holder.alias] = holder
        provider = _provider(tmp_path)
        monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: None)  # unreadable: the bar would refuse

        answer = await provider.peer_refusal(PEER)

        assert isinstance(answer, StartingRefusal)
        await asyncio.wait_for(provider.supervisor.peer_start_task, 5)
        assert _alive(provider.supervisor) and holder._proc is None


class TestSwapOffNothingChanges:
    @pytest.mark.asyncio
    async def test_the_policy_refuses_and_the_holder_is_untouched(self, tmp_path, settings):
        holder = _llama("qwen27")
        await holder.ensure_running()
        _ACTIVE_SUPERVISORS[holder.alias] = holder
        provider = _provider(tmp_path)
        settings.set_local_engine_swap(False, 300)

        reason = await provider.peer_refusal(PEER)

        assert not isinstance(reason, StartingRefusal)
        assert "does not start it now: the card is held by llama-server alias 'qwen27'" in reason
        assert "has not turned on swapping" in reason
        assert provider.supervisor.peer_start_task is None
        _holder_untouched(holder)

    @pytest.mark.asyncio
    async def test_a_peer_call_on_a_llama_alias_meets_today_s_refusal(self, tmp_path, settings):
        holder, new = await _running_pair(tmp_path, "strata")
        settings.set_local_engine_swap(False, 300)

        with serving_a_peer(PEER):
            with pytest.raises(CardHeldError, match="strata alias 'flash'"):
                await new.ensure_running()

        _holder_untouched(holder)
        assert new._proc is None and _in_line() == []


class TestTheQueueIsNotEmpty:
    @pytest.mark.asyncio
    async def test_the_peer_is_refused_and_the_holder_and_the_line_stay_as_they_were(self, tmp_path):
        holder = _llama("qwen27")
        await holder.ensure_running()
        _ACTIVE_SUPERVISORS[holder.alias] = holder
        owner_new = _strata(tmp_path / "owner", "flash 2")
        _ACTIVE_SUPERVISORS[owner_new.alias] = owner_new
        provider = _provider(tmp_path / "peer")

        async with card_held_for(holder.alias, "accepted_request", agent_id="agent_owner", conversation_id="owners-chat"):
            owner = _another_request(owner_new.ensure_running())
            await _until(lambda: _in_line() == ["flash 2"])
            before = [(p.alias, len(p.requests)) for p in base_mod._CARD_QUEUE]

            reason = await provider.peer_refusal(PEER)

            assert not isinstance(reason, StartingRefusal)
            assert "a request on this node is waiting for the card" in reason
            assert "agent_owner" not in reason and "owners-chat" not in reason and "flash 2" not in reason
            assert provider.supervisor.peer_start_task is None
            assert [(p.alias, len(p.requests)) for p in base_mod._CARD_QUEUE] == before
            _holder_untouched(holder)
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_a_started_peer_call_is_refused_at_once_with_the_line_as_it_was(self, tmp_path):
        holder = _llama("qwen27")
        await holder.ensure_running()
        _ACTIVE_SUPERVISORS[holder.alias] = holder
        owner_new = _strata(tmp_path / "owner", "flash 2")
        peer_new = _strata(tmp_path / "peer", "flash")
        _ACTIVE_SUPERVISORS[owner_new.alias], _ACTIVE_SUPERVISORS[peer_new.alias] = owner_new, peer_new

        async with card_held_for(holder.alias, "accepted_request"):
            owner = _another_request(owner_new.ensure_running())
            await _until(lambda: _in_line() == ["flash 2"])
            async def peers_start():
                strata_mod._STARTED_FOR_PEER.set(PEER)  # as `start_for_peer` runs it, in a context of its own
                return await peer_new.ensure_running()

            with pytest.raises(CardQueueRefusal, match="waiting for the card"):
                await asyncio.wait_for(_another_request(peers_start()), 5)
            assert _in_line() == ["flash 2"] and peer_new._proc is None
            _holder_untouched(holder)
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)


class TestTheHolderIsBusy:
    @pytest.mark.asyncio
    async def test_a_request_standing_on_it_refuses_the_peer_in_a_peer_s_words(self, tmp_path):
        holder = _llama("qwen27")
        await holder.ensure_running()
        _ACTIVE_SUPERVISORS[holder.alias] = holder
        provider = _provider(tmp_path)

        async with card_held_for(holder.alias, "accepted_request", agent_id="agent_owner", conversation_id="owners-chat"):
            reason = await provider.peer_refusal(PEER)

        assert not isinstance(reason, StartingRefusal)
        assert "the card is held by llama-server alias 'qwen27'; it is busy" in reason
        assert "agent_owner" not in reason and "owners-chat" not in reason
        assert provider.supervisor.peer_start_task is None and _in_line() == []
        _holder_untouched(holder)

    @pytest.mark.asyncio
    async def test_a_caller_in_its_window_refuses_the_peer(self, tmp_path):
        holder = _llama("qwen27")
        await holder.ensure_running()
        _ACTIVE_SUPERVISORS[holder.alias] = holder
        provider = _provider(tmp_path)
        base_mod.note_caller_call(holder.alias, "a gateway client")

        reason = await provider.peer_refusal(PEER)

        assert "it is busy" in reason and "a gateway client" not in reason
        assert provider.supervisor.peer_start_task is None
        _holder_untouched(holder)

    @pytest.mark.asyncio
    async def test_a_peer_call_is_refused_at_once_by_a_busy_holder_and_waits_for_nothing(self, tmp_path):
        holder, new = await _running_pair(tmp_path, "strata")
        slot = holder.call_slot()
        await slot.__aenter__()  # a generation in flight
        try:
            with serving_a_peer(PEER):
                with pytest.raises(CardQueueRefusal, match="cannot load for a peer.*it is busy"):
                    await asyncio.wait_for(new.ensure_running(), 5)
            assert _in_line() == []
            _holder_untouched(holder)
        finally:
            await slot.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_a_holder_already_stopping_is_not_swapped_again(self, tmp_path):
        holder, new = await _running_pair(tmp_path, "strata")
        holder._draining = True  # a settings save retiring it, say

        with serving_a_peer(PEER):
            with pytest.raises(CardQueueRefusal, match="it is already stopping"):
                await asyncio.wait_for(new.ensure_running(), 5)

        assert holder._swap_for is None and new._proc is None


class TestAnOwnerRequestArrivesDuringThePeersSwap:
    @pytest.mark.asyncio
    async def test_it_takes_the_first_place_and_the_card_and_the_peer_s_start_is_the_one_refused(
        self, tmp_path, caplog,
    ):
        holder = _llama("qwen27")
        await holder.ensure_running()
        _ACTIVE_SUPERVISORS[holder.alias] = holder
        provider = _provider(tmp_path / "peer")
        owner_new = _strata(tmp_path / "owner", "flash 2")
        _ACTIVE_SUPERVISORS[owner_new.alias] = owner_new
        reached, let_go = _slow_stop(holder)

        with caplog.at_level(logging.INFO):
            assert isinstance(await provider.peer_refusal(PEER), StartingRefusal)
            await asyncio.wait_for(reached.wait(), 5)
            assert _in_line() == [], "the peer is in no line: the owner is first in an empty one"
            owner = _another_request(owner_new.ensure_running())
            await _until(lambda: _in_line() == ["flash 2"])
            let_go.set()
            await asyncio.wait_for(owner, 10)
            await asyncio.wait_for(provider.supervisor.peer_start_task, 10)

        assert [h["alias"] for h in card_holders()] == ["flash 2"], "the owner's request was served first"
        assert not _alive(provider.supervisor) and not _alive(holder)
        assert any("begun for peer" in r.getMessage() and "failed" in r.getMessage() for r in caplog.records)
        assert _in_line() == []
