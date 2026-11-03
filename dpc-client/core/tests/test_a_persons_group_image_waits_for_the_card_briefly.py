"""A person's group image waits for the card briefly (ADR-044, the card queue; Mike's call,
2026-11-03).

`_describe_image_for_agents` runs before the group message is built and its text goes into the
message, so `swap_wait_s` was how long a person's message could hang on a busy holder. It waits
30 s and the message goes with its warning and no description. That bounds the wait and no work:
a description running on the card is not cut, and a start past the claim is waited for, which is
the part of the hang that is with the owner. With the swap off nothing changes.

Real supervisors and their real `ensure_running`, with the fakes of
`test_one_local_engine_holds_the_card.py`; `_swap_clock` is faked where a test says a time.
"""

import asyncio
import time
from pathlib import Path

import pytest

from dpc_client_core import service as service_mod
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import card_queue, waiting_for_the_card_at_most
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings

from tests.test_a_callers_run_of_calls_keeps_its_alias_busy_for_the_window import _Clock
from tests.test_a_group_image_reports_a_refused_description_and_still_routes_the_mention import (
    _make_self,
    _png_data_url,
)
from tests.test_a_request_holds_the_card_from_its_first_round_to_its_last import (
    F,
    S,
    _alive,
    _another_request,
    _finish,
    _untouched,
)
from tests.test_one_local_engine_holds_the_card import _fake_child, _strata, card  # noqa: F401
from tests.test_requests_wanting_the_card_wait_in_one_line_and_nothing_is_forced import _in_line, _request, _until
from tests.test_the_card_queue_is_reported_and_a_background_call_neither_swaps_nor_queues import (
    _QueryThatNeedsTheEngine,
)

SETTING = "Settings: Wait for a busy holder"
WHY = "a person's group message waits on this description"
SWAP_WAIT_S = 300


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "dpc-home-settings")


@pytest.fixture(autouse=True)
def swap_on(settings, tmp_path, monkeypatch):
    settings.set_local_engine_swap(True, SWAP_WAIT_S, 0)
    base_mod.bind_local_engine_settings(settings)
    monkeypatch.setattr(base_mod, "_LAST_SWAP", None)
    monkeypatch.setattr(base_mod, "_SWAP_POLL_S", 0.005)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    yield
    base_mod.bind_local_engine_settings(None)
    base_mod.bind_owner_loop(None)


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(base_mod, "_swap_clock", fake)
    return fake


def _pair(tmp_path):
    f, s = _strata(tmp_path / "f", F), _strata(tmp_path / "s", S)
    _ACTIVE_SUPERVISORS[F], _ACTIVE_SUPERVISORS[S] = f, s
    return f, s


def _sent_with(llm, tmp_path):
    """A person's image going out through the real `send_group_image` and the real description."""
    service, monitor = _make_self(tmp_path)
    service.llm_manager = llm
    service._describe_image_for_agents = lambda *args: CoreService._describe_image_for_agents(service, *args)
    task = _another_request(CoreService.send_group_image(
        service, "group-test", _png_data_url(), "shot.png", "caption", describe_for_agents=True,
    ))
    return task, monitor


async def _busy(holder):
    """`holder` up with a call in flight."""
    await holder.ensure_running()
    slot = holder.call_slot()
    await slot.__aenter__()
    return slot


def _went_without_a_description(result, monitor) -> str:
    assert result["status"] == "success" and result["vl_description_status"] == "failed"
    [warning] = result["warnings"]
    assert warning.startswith("Image sent, but describe for agents (VL) produced no description:")
    assert monitor.add_message.call_count == 1
    assert "[VL]" not in str(monitor.add_message.call_args)
    assert "vision failed" not in warning, "no model was asked: the provider's wrap is not true of it"
    return warning


async def _still_waiting(task):
    await asyncio.sleep(0.03)  # several polls of the line
    assert not task.done()


class _ADescriptionThatTakesAWhile:
    """`LLMManager.query` on a local alias: the start, then the model's work inside the slot."""

    def __init__(self, sup, seconds=None):
        self.sup = sup
        self.seconds = seconds
        self.on_the_card = asyncio.Event()
        self.finish = asyncio.Event()

    async def query(self, prompt=None, **_):
        await self.sup.ensure_running()
        async with self.sup.call_slot():
            self.on_the_card.set()
            if self.seconds is not None:
                await asyncio.sleep(self.seconds)
            else:
                await self.finish.wait()
        return {"response": "a picture"}


class TestTheLimit:
    @pytest.mark.asyncio
    async def test_the_message_goes_at_the_limit_with_the_holder_untouched(self, tmp_path, clock):
        f, s = _pair(tmp_path)
        slot = await _busy(f)
        task, monitor = _sent_with(_QueryThatNeedsTheEngine(s), tmp_path)
        try:
            await _until(lambda: _in_line() == [S])
            [row] = card_queue()
            assert row["give_up_at"] - time.time() == pytest.approx(30, abs=2)
            clock.now += 29
            await _still_waiting(task)
            clock.now += 2
            result = await asyncio.wait_for(task, 5)
        finally:
            await _finish(task)
        warning = _went_without_a_description(result, monitor)
        assert "cannot load" in warning and f"'{F}'" in warning and "busy for 31 s" in warning
        assert f"at most 30 s ({WHY})" in warning
        assert SETTING not in warning, "the setting did not decide this, and changing it would not help"
        assert _in_line() == []
        _untouched(f)
        async with slot:
            pass

    @pytest.mark.asyncio
    async def test_a_holder_freed_in_time_gets_the_description_into_the_message(self, tmp_path, clock):
        f, s = _pair(tmp_path)
        slot = await _busy(f)
        task, monitor = _sent_with(_QueryThatNeedsTheEngine(s, "a picture"), tmp_path)
        try:
            await _until(lambda: _in_line() == [S])
            clock.now += 29
            await _still_waiting(task)
            await slot.__aexit__(None, None, None)
            result = await asyncio.wait_for(task, 10)
        finally:
            await _finish(task)
        assert result["status"] == "success" and result["vl_description_status"] == "produced"
        assert "[VL] image description:\\na picture" in str(monitor.add_message.call_args)
        assert _alive(s) and not _alive(f)

    @pytest.mark.asyncio
    async def test_a_setting_shorter_than_the_limit_still_decides_and_is_named(self, tmp_path, clock, settings):
        settings.set_local_engine_swap(True, 10, 0)
        f, s = _pair(tmp_path)
        slot = await _busy(f)
        task, monitor = _sent_with(_QueryThatNeedsTheEngine(s), tmp_path)
        try:
            await _until(lambda: _in_line() == [S])
            clock.now += 11
            result = await asyncio.wait_for(task, 5)
        finally:
            await _finish(task)
        warning = _went_without_a_description(result, monitor)
        assert f"at most 10 s ({SETTING})" in warning and WHY not in warning
        _untouched(f)
        async with slot:
            pass

    @pytest.mark.asyncio
    async def test_behind_a_place_it_leaves_at_the_limit_and_the_place_stays(self, tmp_path, clock):
        f, s = _pair(tmp_path)
        third = _strata(tmp_path / "g", "third")
        _ACTIVE_SUPERVISORS["third"] = third
        slot = await _busy(f)
        ahead = _request(third)
        await _until(lambda: _in_line() == ["third"])
        task, monitor = _sent_with(_QueryThatNeedsTheEngine(s), tmp_path)
        try:
            await _until(lambda: _in_line() == ["third", S])
            clock.now += 31
            result = await asyncio.wait_for(task, 5)
        finally:
            await _finish(task)
        warning = _went_without_a_description(result, monitor)
        assert "behind 'third'" in warning and f"at most 30 s ({WHY})" in warning
        assert _in_line() == ["third"] and not ahead.done(), "the other keeps its own deadline"
        await _finish(ahead)
        async with slot:
            pass

    @pytest.mark.asyncio
    async def test_a_stop_it_began_runs_on_and_the_message_goes_at_the_limit(self, tmp_path, clock, monkeypatch):
        f, s = _pair(tmp_path)
        await f.ensure_running()
        go_on = asyncio.Event()
        real_stop = base_mod.stop_by_hand

        async def a_slow_stop(supervisor, *args, **kwargs):
            await go_on.wait()
            await real_stop(supervisor, *args, **kwargs)

        monkeypatch.setattr(base_mod, "stop_by_hand", a_slow_stop)
        task, monitor = _sent_with(_QueryThatNeedsTheEngine(s), tmp_path)
        try:
            await _until(lambda: f._swap_for == S)
            clock.now += 31
            result = await asyncio.wait_for(task, 5)
            warning = _went_without_a_description(result, monitor)
            assert "already stopping" in warning and f"the wait of 30 s ({WHY}) ran out" in warning
            assert f._swap_for == S, "the stop goes on"
            go_on.set()
            await _until(lambda: not _alive(f))
        finally:
            go_on.set()
            await _finish(task)
            await asyncio.gather(*list(base_mod._SWAP_STOPS), return_exceptions=True)
        assert f._swap_for is None and not f._draining

    def test_each_refusal_names_the_bound_that_decided(self, tmp_path):
        f, s = _pair(tmp_path)
        place = base_mod._CardPlace(S)
        with waiting_for_the_card_at_most(30, WHY):
            limited = base_mod._QueuedRequest(place)
        plain = base_mod._QueuedRequest(place)
        for request, named, other in ((limited, f"at most 30 s ({WHY})", SETTING),
                                      (plain, f"at most {SWAP_WAIT_S} s ({SETTING})", WHY)):
            for refusal in (s._ran_out_refusal(request), s._behind_a_place_refusal(request),
                            s._swap_busy_refusal(f, "running", False, request),
                            s._swap_busy_refusal(f, "stopping", True, request)):
                assert named in str(refusal) and other not in str(refusal), str(refusal)


class TestOtherRequestsKeepTheSetting:
    @pytest.mark.asyncio
    async def test_another_request_past_the_limit_waits_on_to_the_setting(self, tmp_path, clock):
        f, s = _pair(tmp_path)
        slot = await _busy(f)
        other = _request(s, agent_id="agent_1", conversation_id="chat_1")
        try:
            await _until(lambda: _in_line() == [S])
            clock.now += 31
            await _still_waiting(other)
            clock.now += SWAP_WAIT_S
            with pytest.raises(base_mod.CardQueueRefusal, match=f"at most {SWAP_WAIT_S} s"):
                await asyncio.wait_for(other, 5)
        finally:
            await _finish(other)
        async with slot:
            pass

    @pytest.mark.asyncio
    async def test_the_limit_ends_with_the_description(self, tmp_path):
        f, s = _pair(tmp_path)
        place = base_mod._CardPlace(S)
        service = _make_self(tmp_path)[0]
        service.llm_manager = _QueryThatNeedsTheEngine(s, "a picture")

        await CoreService._describe_image_for_agents(service, "b64", "image/png")

        assert base_mod._QueuedRequest(place).wait_s == SWAP_WAIT_S

    def test_the_limit_is_the_owners_number(self):
        assert service_mod.GROUP_IMAGE_CARD_WAIT_S == 30


class TestTheLimitIsOnWaitingNotOnWork:
    """Real time, with the limit made small: a plain `wait_for` around the query cuts these."""

    @pytest.fixture(autouse=True)
    def a_small_limit(self, monkeypatch):
        monkeypatch.setattr(service_mod, "GROUP_IMAGE_CARD_WAIT_S", 0.05)

    @pytest.mark.asyncio
    async def test_a_description_running_on_the_card_is_not_cut(self, tmp_path):
        _f, s = _pair(tmp_path)
        await s.ensure_running()
        task, monitor = _sent_with(_ADescriptionThatTakesAWhile(s, seconds=0.3), tmp_path)
        result = await asyncio.wait_for(task, 10)
        assert result["vl_description_status"] == "produced"
        assert "a picture" in str(monitor.add_message.call_args)

    @pytest.mark.asyncio
    async def test_a_description_that_took_the_card_after_a_wait_is_not_cut_either(self, tmp_path, monkeypatch):
        """Past its deadline only after the swap, by the queue's clock (held still until then so
        the swap passes) and by the real one: the deadline has gone with the place."""
        clock = _Clock()
        monkeypatch.setattr(base_mod, "_swap_clock", clock)
        f, s = _pair(tmp_path)
        await f.ensure_running()
        llm = _ADescriptionThatTakesAWhile(s)
        task, monitor = _sent_with(llm, tmp_path)
        try:
            await asyncio.wait_for(llm.on_the_card.wait(), 10)
            clock.now += 1000
            await asyncio.sleep(0.2)
            assert not task.done()
            llm.finish.set()
            result = await asyncio.wait_for(task, 10)
        finally:
            await _finish(task)
        assert result["vl_description_status"] == "produced"
        assert _alive(s) and not _alive(f)

    @pytest.mark.asyncio
    async def test_a_start_past_the_claim_is_not_cut_and_the_message_waits_for_it(self, tmp_path):
        """The hang that is left: what the owner is asked about. A start is work (rule 5), and
        nothing here lets the message go on while it runs."""
        _f, s = _pair(tmp_path)
        _fake_child(s, health_delay=0.3)
        task, monitor = _sent_with(_QueryThatNeedsTheEngine(s, "a picture"), tmp_path)
        result = await asyncio.wait_for(task, 10)
        assert result["vl_description_status"] == "produced"
        assert _alive(s)


class TestTheSwapOff:
    @pytest.mark.asyncio
    async def test_the_refusal_is_todays_and_nothing_stands_in_line(self, tmp_path, settings):
        settings.set_local_engine_swap(False, SWAP_WAIT_S, 0)
        f, s = _pair(tmp_path)
        await f.ensure_running()
        task, monitor = _sent_with(_QueryThatNeedsTheEngine(s), tmp_path)
        result = await asyncio.wait_for(task, 5)
        warning = _went_without_a_description(result, monitor)
        assert "Swap local models" in warning and WHY not in warning
        assert _in_line() == []
        _untouched(f)

    @pytest.mark.asyncio
    async def test_a_supervisor_draining_reads_its_own_words_too(self, tmp_path):
        _f, s = _pair(tmp_path)
        s._draining = True
        task, monitor = _sent_with(_QueryThatNeedsTheEngine(s), tmp_path)
        result = await asyncio.wait_for(task, 5)
        warning = _went_without_a_description(result, monitor)
        assert str(s._draining_error()) in warning

    @pytest.mark.asyncio
    async def test_a_failure_that_is_no_refusal_keeps_the_providers_words(self, tmp_path):
        class _AModelThatFailed:
            async def query(self, prompt=None, **_):
                raise RuntimeError("llamacpp_server vision failed for 'x': the model is gone")

        task, monitor = _sent_with(_AModelThatFailed(), tmp_path)
        result = await asyncio.wait_for(task, 5)
        [warning] = result["warnings"]
        assert "vision failed for 'x': the model is gone" in warning

    @pytest.mark.asyncio
    async def test_a_description_runs_past_the_limit_as_it_did(self, tmp_path, settings, monkeypatch):
        settings.set_local_engine_swap(False, SWAP_WAIT_S, 0)
        monkeypatch.setattr(service_mod, "GROUP_IMAGE_CARD_WAIT_S", 0.05)
        _f, s = _pair(tmp_path)
        _fake_child(s, health_delay=0.2)
        task, _monitor = _sent_with(_ADescriptionThatTakesAWhile(s, seconds=0.2), tmp_path)
        result = await asyncio.wait_for(task, 10)
        assert result["vl_description_status"] == "produced"
