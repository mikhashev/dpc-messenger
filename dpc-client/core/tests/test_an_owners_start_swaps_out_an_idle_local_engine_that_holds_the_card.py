"""The idle swap of local engines (ADR-044, amendment 2026-11-02).

An owner's start of a local alias that an excluding card holder blocks stops the holder
by the Stop engine command's path and starts the new one, when `swap_when_idle` is on and
the holder is idle or finishes within `swap_wait_s`; a busy holder is not marked and serves
on (the card queue's rule 2; the holds on an alias are in
`test_a_request_holds_the_card_from_its_first_round_to_its_last.py`). A peer's call never
swaps, and with the setting off the refusal is today's. Real supervisors and their real `ensure_running`;
the child, the health and props reads are faked at the supervisors' own seams, as in
`test_one_local_engine_holds_the_card.py`. Every home is under tmp_path.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers import strata_supervisor as strata_mod
from dpc_client_core.managers.llama_server_supervisor import (
    CardHeldError,
    LlamaServerError,
    card_holders,
    serving_a_peer,
)
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings

from tests.test_one_local_engine_holds_the_card import _fake_child, _llama, _strata, card  # noqa: F401


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "dpc-home-settings")


@pytest.fixture(autouse=True)
def swap_on(settings, monkeypatch):
    """The service's binding: the settings object, read at each start, with the swap
    switched on (the default is off since 2026-11-02). Quick polling."""
    settings.set_local_engine_swap(True, 300)
    base_mod.bind_local_engine_settings(settings)
    monkeypatch.setattr(base_mod, "_LAST_SWAP", None)
    monkeypatch.setattr(base_mod, "_SWAP_POLL_S", 0.01)
    yield
    base_mod.bind_local_engine_settings(None)
    base_mod.bind_model_download_service(None)


async def _running_pair(tmp_path, holder_kind):
    """A started holder and a not-started rival, both registered as active aliases."""
    if holder_kind == "llama":
        holder, new = _llama("qwen27"), _strata(tmp_path, "flash")
    elif holder_kind == "strata":
        holder, new = _strata(tmp_path, "flash"), _llama("qwen27")
    else:  # a second Strata alias
        holder, new = _strata(tmp_path / "a", "flash"), _strata(tmp_path / "b", "flash 2")
    await holder.ensure_running()
    _ACTIVE_SUPERVISORS[holder.alias] = holder
    _ACTIVE_SUPERVISORS[new.alias] = new
    return holder, new


def _holder_untouched(holder):
    assert holder._proc is not None and holder._proc.returncode is None
    assert holder._draining is False and holder._swap_for is None
    assert [h["alias"] for h in card_holders()] == [holder.alias]


class TestWhereTheRefusalStaysAsToday:
    @pytest.mark.asyncio
    async def test_with_the_setting_off_the_refusal_is_today_s(self, tmp_path, settings):
        holder, new = await _running_pair(tmp_path, "llama")
        settings.set_local_engine_swap(False, 300)

        with pytest.raises(CardHeldError, match="llama-server alias 'qwen27'"):
            await new.ensure_running()

        _holder_untouched(holder)
        assert new._proc is None

    @pytest.mark.asyncio
    async def test_with_no_settings_bound_nothing_swaps(self, tmp_path):
        holder, new = await _running_pair(tmp_path, "strata")
        base_mod.bind_local_engine_settings(None)

        with pytest.raises(CardHeldError):
            await new.ensure_running()
        _holder_untouched(holder)

    @pytest.mark.asyncio
    async def test_a_peer_call_never_swaps_even_when_the_holder_is_idle(self, tmp_path):
        holder, new = await _running_pair(tmp_path, "strata")

        with serving_a_peer("dpc-node-" + "b" * 32):
            with pytest.raises(CardHeldError, match="strata alias 'flash'"):
                await new.ensure_running()

        _holder_untouched(holder)
        assert new._proc is None

    @pytest.mark.asyncio
    async def test_a_strata_start_begun_for_a_peer_never_swaps_either(self, tmp_path):
        holder, new = await _running_pair(tmp_path, "llama")

        token = strata_mod._STARTED_FOR_PEER.set("dpc-node-" + "c" * 32)
        try:
            with pytest.raises(CardHeldError, match="llama-server alias 'qwen27'"):
                await new.ensure_running()
        finally:
            strata_mod._STARTED_FOR_PEER.reset(token)

        _holder_untouched(holder)

    @pytest.mark.asyncio
    async def test_a_holder_already_stopping_for_some_other_reason_is_not_swapped(self, tmp_path):
        holder, new = await _running_pair(tmp_path, "llama")
        holder._draining = True  # a settings save retiring it, say

        with pytest.raises(CardHeldError, match=r"'qwen27' \(draining\)"):
            await new.ensure_running()
        assert holder._swap_for is None


class TestAnIdleHolderIsSwappedOut:
    @pytest.mark.asyncio
    async def test_a_llama_server_holder_is_stopped_and_strata_starts(self, tmp_path, caplog):
        holder, new = await _running_pair(tmp_path, "llama")
        stops = []
        real_stop = holder._stop_child

        async def stop_child():
            stops.append("stopped")
            await real_stop()

        holder._stop_child = stop_child

        with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
            props = await new.ensure_running()

        assert props == {"total_slots": 1}
        assert stops == ["stopped"]
        assert holder._proc is None
        assert [(h["alias"], h["engine"]) for h in card_holders()] == [("flash", "strata")]
        line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("swapped ")]
        assert line == ["swapped 'qwen27' out for 'flash'"], "llama-server parks nothing, so no second half"

    @pytest.mark.asyncio
    async def test_a_strata_holder_is_stopped_and_the_log_says_its_parked_conversations_go(self, tmp_path, caplog):
        holder, new = await _running_pair(tmp_path, "strata")

        with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
            await new.ensure_running()

        assert holder._proc is None
        assert [h["alias"] for h in card_holders()] == ["qwen27"]
        line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("swapped ")]
        assert line == ["swapped 'flash' out for 'qwen27'; its parked conversations are dropped"]

    @pytest.mark.asyncio
    async def test_a_second_strata_alias_swaps_the_first_out(self, tmp_path):
        holder, new = await _running_pair(tmp_path, "strata2")

        await new.ensure_running()

        assert holder._proc is None
        assert [h["alias"] for h in card_holders()] == ["flash 2"]

    @pytest.mark.asyncio
    async def test_the_swapped_alias_stays_configured_and_serves_again(self, tmp_path):
        holder, new = await _running_pair(tmp_path, "llama")
        await new.ensure_running()

        assert holder._draining is False and holder._swap_for is None
        async with holder.call_slot():
            pass  # not refused: its next owner call may start it again

    @pytest.mark.asyncio
    async def test_the_swap_is_recorded_for_the_status(self, tmp_path):
        holder, new = await _running_pair(tmp_path, "strata")
        assert base_mod.last_swap() is None

        await new.ensure_running()

        got = base_mod.last_swap()
        assert (got["from"], got["to"]) == ("flash", "qwen27")
        assert (got["from_engine"], got["to_engine"]) == ("strata", "llama-server")
        assert got["parked_dropped"] is True and got["at"] > 0

    @pytest.mark.asyncio
    async def test_the_owner_is_told_in_a_toast(self, tmp_path):
        sent = []

        class Api:
            async def broadcast_event(self, name, payload):
                sent.append((name, payload))

        base_mod.bind_model_download_service(SimpleNamespace(local_api=Api()))
        holder, new = await _running_pair(tmp_path, "llama")

        await new.ensure_running()
        await asyncio.gather(*list(base_mod._SWAP_TOASTS))

        assert [n for n, _ in sent] == ["error_toast"]
        assert sent[0][1]["message"] == "swapped 'qwen27' out for 'flash'"

    @pytest.mark.asyncio
    async def test_the_settings_are_read_at_the_start_not_at_the_binding(self, tmp_path, settings):
        holder, new = await _running_pair(tmp_path, "llama")
        settings.set_local_engine_swap(False, 300)
        with pytest.raises(CardHeldError):
            await new.ensure_running()
        _holder_untouched(holder)

        settings.set_local_engine_swap(True, 300)
        assert await new.ensure_running() == {"total_slots": 1}
        assert holder._proc is None

    @pytest.mark.asyncio
    async def test_no_holder_means_no_swap(self, tmp_path):
        new = _strata(tmp_path)
        await new.ensure_running()
        assert base_mod.last_swap() is None


class TestABusyHolder:
    @pytest.mark.asyncio
    async def test_a_busy_holder_is_not_marked_and_keeps_serving_while_a_start_waits(self, tmp_path, settings):
        # The card queue's rule 2 replaced the mark and the refusal this test used to hold.
        settings.set_local_engine_swap(True, 60)
        holder, new = await _running_pair(tmp_path, "llama")
        slot = holder.call_slot()  # a generation in flight
        start = asyncio.ensure_future(new.ensure_running())
        await asyncio.sleep(0.05)

        assert not start.done()
        assert holder._draining is False and holder._swap_for is None
        assert await holder.ensure_running() == {"total_slots": 1}
        second = holder.call_slot()
        assert holder._in_flight == 2, "it serves what it has"
        assert holder._proc.returncode is None, "the running generation is not cut"

        async with second:
            pass
        async with slot:  # the generations end
            pass
        assert await asyncio.wait_for(start, 5) == {"total_slots": 1}
        assert holder._proc is None
        assert [h["alias"] for h in card_holders()] == ["flash"]

    @pytest.mark.asyncio
    async def test_a_timeout_refuses_naming_both_and_the_holder_serves_again(self, tmp_path, settings):
        settings.set_local_engine_swap(True, 1)
        holder, new = await _running_pair(tmp_path, "strata")
        slot = holder.call_slot()

        with pytest.raises(CardHeldError) as refused:
            await asyncio.wait_for(new.ensure_running(), 10)

        message = str(refused.value)
        assert "'flash'" in message and "qwen27" in message
        assert "busy for 1 s" in message
        assert refused.value.holder_alias == "flash" and refused.value.holder_engine == "strata"
        _holder_untouched(holder)
        assert new._proc is None
        async with holder.call_slot():
            pass  # it serves again
        async with slot:
            pass

    @pytest.mark.asyncio
    async def test_a_wait_of_zero_refuses_a_busy_holder_at_once_and_never_marks_it(self, tmp_path, settings, monkeypatch):
        settings.set_local_engine_swap(True, 0)
        holder, new = await _running_pair(tmp_path, "llama")
        marked = []
        monkeypatch.setattr(type(holder), "_begin_swap_drain", lambda self, n: marked.append(n.alias))
        slot = holder.call_slot()

        with pytest.raises(CardHeldError, match=r"'qwen27', busy for 0 s"):
            await asyncio.wait_for(new.ensure_running(), 10)

        assert marked == []
        _holder_untouched(holder)
        async with slot:
            pass

    @pytest.mark.asyncio
    async def test_a_wait_of_zero_still_swaps_an_idle_holder(self, tmp_path, settings):
        settings.set_local_engine_swap(True, 0)
        holder, new = await _running_pair(tmp_path, "llama")

        assert await new.ensure_running() == {"total_slots": 1}
        assert holder._proc is None

    @pytest.mark.asyncio
    async def test_a_start_cancelled_while_it_waits_gives_the_holder_back(self, tmp_path, settings):
        settings.set_local_engine_swap(True, 60)
        holder, new = await _running_pair(tmp_path, "llama")
        slot = holder.call_slot()
        start = asyncio.ensure_future(new.ensure_running())
        await asyncio.sleep(0.05)
        assert not start.done()

        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start

        _holder_untouched(holder)
        async with slot:
            pass


class TestTheRace:
    @pytest.mark.asyncio
    async def test_a_request_arriving_between_the_check_and_the_stop_is_refused_not_run(self, tmp_path):
        holder, new = await _running_pair(tmp_path, "llama")
        in_the_stop = asyncio.Event()
        let_it_stop = asyncio.Event()
        real_stop = holder._stop_child

        async def slow_stop():
            in_the_stop.set()
            await let_it_stop.wait()
            await real_stop()

        holder._stop_child = slow_stop
        start = asyncio.ensure_future(new.ensure_running())
        await asyncio.wait_for(in_the_stop.wait(), 5)

        # The idle check has passed and the engine is not stopped yet.
        assert holder._proc is not None and holder._proc.returncode is None
        reason = "'qwen27' is stopping to free the card for 'flash'"
        with pytest.raises(LlamaServerError) as call:
            holder.call_slot()
        assert str(call.value) == reason
        with pytest.raises(LlamaServerError) as begun:
            await holder.ensure_running()
        assert str(begun.value) == reason
        assert holder._in_flight == 0 and holder._peak_in_flight == 0, "nothing ran on the stopping engine"

        let_it_stop.set()
        assert await asyncio.wait_for(start, 5) == {"total_slots": 1}
        assert holder._proc is None

    @pytest.mark.asyncio
    async def test_the_idle_check_and_the_drain_mark_have_no_await_between_them(self, tmp_path, monkeypatch):
        # Seen from outside: the instant the swap decides, the mark is up.
        holder, new = await _running_pair(tmp_path, "strata")
        seen = []
        real_begin = type(holder)._begin_swap_drain

        def begin(self, other):
            seen.append(self._in_flight)
            real_begin(self, other)
            seen.append(self._draining)

        monkeypatch.setattr(type(holder), "_begin_swap_drain", begin)
        await new.ensure_running()
        assert seen == [0, True]

    @pytest.mark.asyncio
    async def test_two_owner_starts_at_once_one_wins_and_the_other_follows_the_rule_against_it(self, tmp_path):
        a = _llama("qwen27")
        b = _strata(tmp_path, "flash")
        _fake_child(a, health_delay=0.02)
        _fake_child(b, health_delay=0.02)
        for sup in (a, b):
            _ACTIVE_SUPERVISORS[sup.alias] = sup

        results = await asyncio.gather(a.ensure_running(), b.ensure_running(), return_exceptions=True)

        # The later start finds the earlier one claimed and starting, which is busy (the
        # card queue's rule 1): it waits for that start to finish and the rule then applies.
        # Whichever way it fell, exactly one holds the card and the other is stopped.
        assert len(card_holders()) == 1, results
        winner = card_holders()[0]["alias"]
        loser = a if winner == "flash" else b
        assert loser._proc is None or loser._proc.returncode is not None
        assert not any(isinstance(r, BaseException) and not isinstance(r, LlamaServerError) for r in results), results


class TestTheSettings:
    def test_the_defaults_are_off_and_three_hundred_seconds(self, tmp_path):
        # Off since the live check of 2026-11-02 cut an agent's loop; on again after the card queue.
        s = Settings(tmp_path / "h")
        assert s.get_swap_when_idle() is False and s.get_swap_wait_s() == 300

    def test_a_config_without_the_keys_reads_the_same_defaults(self, tmp_path):
        home = tmp_path / "h"
        home.mkdir()
        (home / "config.ini").write_text("[hub]\nurl = http://localhost:8000\n", encoding="utf-8")
        s = Settings(home)
        assert s.get_swap_when_idle() is False and s.get_swap_wait_s() == 300

    def test_a_choice_round_trips_and_survives_a_reload(self, tmp_path):
        home = tmp_path / "h"
        Settings(home).set_local_engine_swap(False, 0)
        again = Settings(home)
        assert again.get_swap_when_idle() is False and again.get_swap_wait_s() == 0

    @pytest.mark.parametrize("enabled, wait_s", [
        ("yes", 5), (1, 5), (None, 5), (True, -1), (True, 1.5), (True, True), (True, "5"), (True, None),
    ])
    def test_a_bad_value_is_refused_and_nothing_is_written(self, tmp_path, enabled, wait_s):
        home = tmp_path / "h"
        s = Settings(home)
        s.set_local_engine_swap(False, 7)
        before = (home / "config.ini").read_bytes()
        with pytest.raises(ValueError):
            s.set_local_engine_swap(enabled, wait_s)
        assert (home / "config.ini").read_bytes() == before

    @pytest.mark.parametrize("raw", ["-3", "soon", "1.5", ""])
    def test_a_hand_edited_wait_that_is_not_a_whole_number_reads_as_the_default(self, tmp_path, raw):
        home = tmp_path / "h"
        home.mkdir()
        (home / "config.ini").write_text(f"[local_engines]\nswap_wait_s = {raw}\n", encoding="utf-8")
        assert Settings(home).get_swap_wait_s() == 300

    def test_a_hand_edited_flag_that_is_not_a_boolean_reads_as_the_default(self, tmp_path):
        home = tmp_path / "h"
        home.mkdir()
        (home / "config.ini").write_text("[local_engines]\nswap_when_idle = maybe\n", encoding="utf-8")
        assert Settings(home).get_swap_when_idle() is False


def _host(settings):
    return SimpleNamespace(settings=settings)


class TestCommandAndStatus:
    def test_the_command_is_allowed_and_is_a_service_method(self):
        assert "set_local_engine_swap" in ALLOWED_COMMANDS
        assert callable(getattr(CoreService, "set_local_engine_swap", None))

    @pytest.mark.asyncio
    async def test_the_command_writes_through_the_settings_and_answers_the_stored_values(self, settings):
        out = await CoreService.set_local_engine_swap(_host(settings), enabled=False, wait_s=45)
        assert out == {"status": "success", "swap_when_idle": False, "swap_wait_s": 45, "caller_idle_s": 120}
        assert (settings.get_swap_when_idle(), settings.get_swap_wait_s()) == (False, 45)
        # and a supervisor reading the bound settings sees it at once
        assert base_mod.swap_policy() == (False, 45)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("params", [
        {"enabled": "true", "wait_s": 5}, {"enabled": True, "wait_s": -1}, {"enabled": True, "wait_s": 2.5},
        {"enabled": True, "wait_s": True}, {"enabled": True}, {"wait_s": 5}, {},
    ])
    async def test_the_command_answers_a_bad_value_as_an_error_and_writes_nothing(self, settings, params):
        before = (settings.dpc_home_dir / "config.ini").read_bytes()
        out = await CoreService.set_local_engine_swap(_host(settings), **params)
        assert out["status"] == "error" and out["message"]
        assert (settings.dpc_home_dir / "config.ini").read_bytes() == before

    @pytest.mark.asyncio
    async def test_the_status_carries_both_settings_and_follows_a_change(self, settings):
        out = await CoreService.local_engine_status(_host(settings))
        assert out["status"] == "success"
        assert out["swap_when_idle"] is True and out["swap_wait_s"] == 300
        await CoreService.set_local_engine_swap(_host(settings), enabled=False, wait_s=0)
        out = await CoreService.local_engine_status(_host(settings))
        assert out["swap_when_idle"] is False and out["swap_wait_s"] == 0

    @pytest.mark.asyncio
    async def test_the_status_carries_the_last_swap_after_one(self, tmp_path, settings):
        out = await CoreService.local_engine_status(_host(settings))
        assert out["last_swap"] is None
        holder, new = await _running_pair(tmp_path, "llama")
        await new.ensure_running()

        out = await CoreService.local_engine_status(_host(settings))
        assert out["last_swap"]["from"] == "qwen27" and out["last_swap"]["to"] == "flash"
        assert out["last_swap"]["parked_dropped"] is False


class TestTheReviewFixes:
    """Zcode's review, 2026-11-02: a retired holder is not reopened, and a holder freed
    by someone else's stop is not reported as this swap's work."""

    @pytest.mark.asyncio
    async def test_a_holder_replaced_while_it_drains_is_not_reopened(self, tmp_path):
        holder, _new = await _running_pair(tmp_path, "llama")
        replacement = _llama("qwen27")
        _ACTIVE_SUPERVISORS[holder.alias] = replacement  # a settings save replaced it

        await base_mod.stop_by_hand(holder)

        assert holder._proc is None
        assert holder._draining is True, "a supervisor its alias no longer runs stays closed"

    @pytest.mark.asyncio
    async def test_the_configured_holder_is_reopened_after_a_stop(self, tmp_path):
        holder, _new = await _running_pair(tmp_path, "llama")

        await base_mod.stop_by_hand(holder)

        assert holder._draining is False

    @pytest.mark.asyncio
    async def test_a_holder_stopped_by_hand_during_the_wait_is_not_called_a_swap(self, tmp_path, caplog):
        holder, new = await _running_pair(tmp_path, "llama")
        holder._in_flight = 1
        with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
            start = asyncio.create_task(new.ensure_running())
            await asyncio.sleep(0.05)
            holder._in_flight = 0
            await holder.drain()  # the owner's Stop engine lands first
            _CARD = [h["alias"] for h in card_holders()]
            assert holder.alias not in _CARD
            await asyncio.wait_for(start, 10)

        assert not [r for r in caplog.records if r.getMessage().startswith("swapped ")]
        assert base_mod._LAST_SWAP is None


class TestTheRefusalNamesTheSwitch:
    """Mike, 2026-11-02: when a second local model is asked for and the swap is off, the
    refusal says the switch exists and where it is."""

    @pytest.mark.asyncio
    async def test_with_the_swap_off_the_refusal_names_the_checkbox(self, tmp_path, settings):
        settings.set_local_engine_swap(False, 300)
        holder, new = await _running_pair(tmp_path, "llama")
        with pytest.raises(CardHeldError) as refused:
            await new.ensure_running()
        assert "Swap local models when the card holder is idle" in str(refused.value)
        assert "Local engines" in str(refused.value)

    def test_with_the_swap_on_the_refusal_does_not_offer_it(self, tmp_path, settings):
        holder = _llama("qwen27")
        base_mod._CARD_CLAIMS[holder] = "running"
        try:
            err = _strata(tmp_path, "flash")._card_held_error()
        finally:
            base_mod._CARD_CLAIMS.pop(holder, None)
        assert err is not None and "Swap local models" not in str(err)
