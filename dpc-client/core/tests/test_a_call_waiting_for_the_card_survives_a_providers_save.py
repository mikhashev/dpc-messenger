"""A call waiting for the card survives a providers save (ADR-044, amendment 2026-11-11).

The case, 2026-11-10 23:34-23:37: Forge's call waited in the card queue for a Strata alias
with no child yet while another alias held the card; a providers save replaced that alias's
supervisor, `_retire` drained the old one, and the waiting call met "is draining; new calls
refused" and ended its turn on round 1.

Real providers, real supervisors and their real `ensure_running`; the spawn, `/health`,
`/props` and the VRAM reader are the fakes of `test_one_local_engine_holds_the_card.py`. No
process runs and no GPU is touched.
"""

import asyncio
import contextlib
import logging
from pathlib import Path

import pytest

from dpc_client_core.dpc_agent.retrieval import factory as retrieval_factory
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import (
    EngineDrainingError,
    card_held_for,
)
from dpc_client_core.providers import LlamaServerProvider, StrataProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from dpc_client_core.settings import Settings

from tests.test_a_request_holds_the_card_from_its_first_round_to_its_last import (
    _alive,
    _another_request,
    _finish,
)
from tests.test_one_local_engine_holds_the_card import _fake_child, _strata, card  # noqa: F401
from tests.test_strata_supervisor import _install

HOLDER = "the holder"
WAITER = "qwen3.8-flash-next-iq2_xs Strata"
BEHIND = "an alias behind"


@pytest.fixture(autouse=True)
def swap_on(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "dpc-home-settings")
    settings.set_local_engine_swap(True, 60, 0)
    base_mod.bind_local_engine_settings(settings)
    monkeypatch.setattr(base_mod, "_LAST_SWAP", None)
    monkeypatch.setattr(base_mod, "_SWAP_POLL_S", 0.005)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(retrieval_factory, "_derive_embedding_metadata", lambda cfg: ("BAAI/bge-m3", 1024))
    yield
    base_mod.bind_local_engine_settings(None)
    base_mod.bind_owner_loop(None)


def _install_of(tmp_path, alias) -> Path:
    """The alias's install folder, made once: a reload names the same one."""
    home = tmp_path / alias.replace(" ", "_")
    return _install(home) if not home.exists() else home / "Strata"


def _config(tmp_path, alias=WAITER, **flags) -> dict:
    return {
        "type": "strata", "alias": alias, "model": "qwen3.8-flash-next-iq2_xs",
        "strata_install_dir": str(_install_of(tmp_path, alias)),
        "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240, "context_window": 200000,
        "engine_watch_interval_s": 3600, "vram_release_timeout_s": 0.05,
        **flags,
    }


def _provider(tmp_path, alias=WAITER, **flags) -> StrataProvider:
    """A provider as the manager builds one at a load or a reload, its child faked."""
    provider = StrataProvider(alias, _config(tmp_path, alias, **flags))
    _fake_child(provider.supervisor)
    return provider


def _reload(tmp_path, alias=WAITER, **flags) -> StrataProvider:
    """The same alias loaded again by a providers save. A supervisor that came through the
    registry unchanged keeps the fakes it had; a new one is faked here."""
    before = _ACTIVE_SUPERVISORS.get(alias)
    provider = StrataProvider(alias, _config(tmp_path, alias, **flags))
    if provider.supervisor is not before:
        _fake_child(provider.supervisor)
    return provider


async def _holder_on_the_card(tmp_path):
    """Another alias's child on the card, busy with a call: nothing can take the card from it."""
    sup = _strata(tmp_path / "holder", HOLDER)
    _ACTIVE_SUPERVISORS[HOLDER] = sup
    await sup.ensure_running()
    return sup, sup.call_slot()


async def _call(provider, stays: asyncio.Event = None, in_slot: asyncio.Event = None):
    """A call as an agent's round makes one: accepted, standing on its alias, then the
    provider's own ensure and its slot. The answer is the supervisor that served it."""
    async with card_held_for(provider.alias, "accepted_request"):
        await provider._ensure_running()
        served_by = provider.supervisor
        async with served_by.call_slot():
            if in_slot is not None:
                in_slot.set()
            if stays is not None:
                await stays.wait()
        return served_by


def _in_line() -> list:
    return [place.alias for place in base_mod._CARD_QUEUE]


async def _until(predicate, timeout=5.0):
    async def looking():
        while not predicate():
            await asyncio.sleep(0.002)

    await asyncio.wait_for(looking(), timeout)


class TestAnUnchangedAliasKeepsItsSupervisor:
    @pytest.mark.asyncio
    async def test_a_supervisor_with_no_child_and_unchanged_flags_is_adopted_and_its_waiter_goes_on(
        self, tmp_path, caplog,
    ):
        holder, slot = await _holder_on_the_card(tmp_path)
        first = _provider(tmp_path)
        call = _another_request(_call(first))
        await _until(lambda: _in_line() == [WAITER])
        assert first.supervisor.props is None and not _alive(first.supervisor)

        with caplog.at_level(logging.INFO):
            second = _reload(tmp_path)   # a save that moved no flag
        assert second.supervisor is first.supervisor, "nothing to drain: the same supervisor stays"
        assert not first.supervisor._draining
        assert any("adopting the supervisor that has no child yet" in r.getMessage()
                   and "1 request(s) waiting" in r.getMessage() for r in caplog.records)

        await asyncio.sleep(0.05)
        assert not call.done() and _in_line() == [WAITER], "it still waits for the card"
        async with slot:
            pass   # the holder lets go
        served_by = await asyncio.wait_for(call, 10)
        assert served_by is first.supervisor and _alive(served_by)
        await _finish(call)

    @pytest.mark.asyncio
    async def test_a_non_flag_change_reaches_the_adopted_supervisor_with_no_child(self, tmp_path):
        first = _provider(tmp_path)
        second = _reload(tmp_path, start_timeout_s=777.0)
        assert second.supervisor is first.supervisor
        assert second.supervisor.config["start_timeout_s"] == 777.0

    @pytest.mark.asyncio
    async def test_a_running_child_with_unchanged_flags_is_still_adopted(self, tmp_path):
        first = _provider(tmp_path)
        await first.supervisor.ensure_running()
        child = first.supervisor._proc
        second = _reload(tmp_path)
        assert second.supervisor is first.supervisor
        assert first.supervisor._proc is child and _alive(first.supervisor)
        async with second.supervisor.call_slot():
            pass

    def test_a_strata_launcher_stopped_after_an_engine_death_is_not_adopted(self, tmp_path):
        # Its latched failure is the owner's to reset (ADR-044 point 3). A save built a fresh
        # supervisor for it before this change, and nothing can be waiting on it: a latched
        # failure is refused at once.
        first = _provider(tmp_path)
        first.supervisor.failure = {"reason": "engine died"}
        second = _reload(tmp_path)
        assert second.supervisor is not first.supervisor
        assert second.supervisor.failure is None


class TestAChangedAliasHandsItsWaitingCallsOn:
    @pytest.mark.asyncio
    async def test_a_waiting_call_moves_to_the_successor_and_is_not_refused(self, tmp_path, caplog):
        holder, slot = await _holder_on_the_card(tmp_path)
        first = _provider(tmp_path)
        call = _another_request(_call(first))
        await _until(lambda: _in_line() == [WAITER])
        place, old = base_mod._CARD_QUEUE[0], first.supervisor

        with caplog.at_level(logging.INFO):
            second = _reload(tmp_path, conversation_cache_mib=20480)   # a flag moved
        assert second.supervisor is not first.supervisor
        assert _ACTIVE_SUPERVISORS[WAITER] is second.supervisor
        assert any("replaced by a settings save while no child of it is up" in r.getMessage()
                   and "1 request(s) waiting" in r.getMessage() for r in caplog.records)

        await _until(lambda: first.supervisor._draining)   # `_retire` drains the old one
        await asyncio.sleep(0.05)
        assert not call.done(), "the waiting call is not refused"
        assert _in_line() == [WAITER] and base_mod._CARD_QUEUE[0] is place, "it keeps its place"

        async with slot:
            pass   # the holder lets go
        served_by = await asyncio.wait_for(call, 10)
        assert served_by is second.supervisor and _alive(second.supervisor)
        assert first.supervisor is second.supervisor, "the provider the call came through follows it"
        assert not _alive(old) and old._proc is None, "the old supervisor never started a child"
        assert _in_line() == []
        await _finish(call)

    @pytest.mark.asyncio
    async def test_the_place_in_the_line_is_kept_and_nobody_behind_goes_ahead(self, tmp_path):
        holder, slot = await _holder_on_the_card(tmp_path)
        behind_sup = _strata(tmp_path / "behind", BEHIND)
        _ACTIVE_SUPERVISORS[BEHIND] = behind_sup
        first = _provider(tmp_path)
        in_slot, stays = asyncio.Event(), asyncio.Event()
        call = _another_request(_call(first, stays, in_slot))
        await _until(lambda: _in_line() == [WAITER])

        async def behind():
            async with card_held_for(BEHIND, "accepted_request"):
                await behind_sup.ensure_running()
                return True

        other = _another_request(behind())
        await _until(lambda: _in_line() == [WAITER, BEHIND])

        _reload(tmp_path, conversation_cache_mib=20480)
        await asyncio.sleep(0.05)
        assert _in_line() == [WAITER, BEHIND], "a save does not send the waiting call to the back"

        async with slot:
            pass
        await asyncio.wait_for(in_slot.wait(), 10)
        assert not other.done() and _in_line() == [BEHIND], "the one behind waits for its turn"
        stays.set()
        await asyncio.wait_for(call, 10)
        await _finish(call, other)

    @pytest.mark.asyncio
    async def test_a_call_that_comes_through_the_old_provider_after_the_save_goes_to_the_successor(
        self, tmp_path,
    ):
        holder, slot = await _holder_on_the_card(tmp_path)
        first = _provider(tmp_path)
        second = _reload(tmp_path, conversation_cache_mib=20480)
        await _until(lambda: first.supervisor._draining)
        call = _another_request(_call(first))   # an agent still holding the provider it had
        await _until(lambda: _in_line() == [WAITER])
        async with slot:
            pass
        served_by = await asyncio.wait_for(call, 10)
        assert served_by is second.supervisor and first.supervisor is second.supervisor
        await _finish(call)

    @pytest.mark.asyncio
    async def test_two_saves_in_a_row_hand_the_call_down_the_chain(self, tmp_path):
        holder, slot = await _holder_on_the_card(tmp_path)
        first = _provider(tmp_path)
        call = _another_request(_call(first))
        await _until(lambda: _in_line() == [WAITER])
        _reload(tmp_path, conversation_cache_mib=20480)
        third = _reload(tmp_path, conversation_cache_mib=30720)
        await asyncio.sleep(0.05)
        assert not call.done()
        async with slot:
            pass
        served_by = await asyncio.wait_for(call, 10)
        assert served_by is third.supervisor and _alive(third.supervisor)
        await _finish(call)

    @pytest.mark.asyncio
    async def test_an_alias_that_changed_engine_still_refuses_its_waiting_call(self, tmp_path, caplog):
        # The waiting call belongs to a provider of the old engine's dialect; it cannot be
        # pointed at another engine's supervisor.
        holder, slot = await _holder_on_the_card(tmp_path)
        first = _provider(tmp_path)
        call = _another_request(_call(first))
        await _until(lambda: _in_line() == [WAITER])
        with caplog.at_level(logging.WARNING):
            LlamaServerProvider(WAITER, {
                "type": "llamacpp_server", "alias": WAITER, "gguf_path": "D:/models/x.gguf",
            })
        with pytest.raises(EngineDrainingError):
            await asyncio.wait_for(call, 10)
        assert _in_line() == []
        assert any("changed engine" in r.getMessage() for r in caplog.records)
        async with slot:
            pass
        await _finish(call)


class TestACallInFlightKeepsTheDrain:
    @pytest.mark.asyncio
    async def test_a_call_in_flight_finishes_on_the_old_child_and_the_old_child_stops_after_it(
        self, tmp_path,
    ):
        first = _provider(tmp_path)
        old = first.supervisor
        in_slot, stays = asyncio.Event(), asyncio.Event()
        running = _another_request(_call(first, stays, in_slot))
        await asyncio.wait_for(in_slot.wait(), 5)
        child = old._proc

        second = _reload(tmp_path, conversation_cache_mib=20480)
        assert second.supervisor is not old
        await _until(lambda: old._draining)
        await asyncio.sleep(0.05)
        assert _alive(old) and old._proc is child, "the child is not stopped under a running call"
        with pytest.raises(EngineDrainingError):
            old.call_slot()   # new work is refused on the old one, as before

        stays.set()
        assert await asyncio.wait_for(running, 10) is old, "the call that had started stays on it"
        await _until(lambda: not _alive(old))
        assert first.supervisor is old, "an answer already running does not move its provider"

        async with _another_call_on(second):
            pass
        assert _alive(second.supervisor)
        await _finish(running)


@contextlib.asynccontextmanager
async def _another_call_on(provider):
    await provider._ensure_running()
    async with provider.supervisor.call_slot():
        yield
