"""A caller's run of calls keeps its alias busy for the window (ADR-044, amendment 2026-11-02,
the card queue: rule 6 and the door half of rule 1).

A peer's and a gateway client's calls arrive one by one and DPC cannot see where their loop
ends, so a holder counts as busy for `[local_engines] caller_idle_s` seconds after the last one.
Real doors (`P2PCoordinator.handle_inference_request`, `Gateway.complete`), real supervisors with
the fakes of `test_one_local_engine_holds_the_card.py`, the swap forced on, and a fake
`_swap_clock`, so that +119 s and +121 s are said and not slept. Only the provider is a double,
and it uses a supervisor as the real ones do.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from dpc_client_core.gateway import Gateway, GatewayError
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings

from tests.test_a_request_holds_the_card_from_its_first_round_to_its_last import (
    F,
    S,
    _alive,
    _Engine,
    _finish,
    _Gap,
    _held,
    _untouched,
)
from tests.test_one_local_engine_holds_the_card import _strata, card  # noqa: F401
from tests.test_p2p_coordinator import make_coordinator
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import _service

PEER = "peer-1"
NODE = "dpc-node-the-owners-own"
METADATA = {"model": "m", "tokens_used": 2, "prompt_tokens": 1, "response_tokens": 1,
            "model_max_tokens": 128000, "vision_used": False, "thinking": None, "thinking_tokens": None}


class _Clock:
    """`_swap_clock` by hand: the test says what time it is."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(base_mod, "_swap_clock", fake)
    return fake


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "dpc-home-settings")


@pytest.fixture(autouse=True)
def swap_on(settings, monkeypatch):
    settings.set_local_engine_swap(True, 300)
    base_mod.bind_local_engine_settings(settings)
    monkeypatch.setattr(base_mod, "_LAST_SWAP", None)
    monkeypatch.setattr(base_mod, "_SWAP_POLL_S", 0.01)
    yield
    base_mod.bind_local_engine_settings(None)
    base_mod.bind_owner_loop(None)


class _Served(_Engine):
    """`_Engine` as the registry holds it: an alias with a provider type."""

    config = {"type": "strata", "model": "m"}


class _NoCard:
    """A provider with no supervisor: a vendor's alias, or an Ollama one."""

    def __init__(self, type_="deepseek", model="deepseek-v4-flash"):
        self.config = {"type": type_, "model": model}
        self.model = model
        self.called = 0

    def supports_vision(self):
        return False

    def supports_thinking(self):
        return False

    def get_last_usage(self):
        return {}

    async def generate_response(self, prompt, **_):
        self.called += 1
        return "the answer"


def _pair(tmp_path):
    f, s = _strata(tmp_path / "f", F), _strata(tmp_path / "s", S)
    _ACTIVE_SUPERVISORS[F], _ACTIVE_SUPERVISORS[S] = f, s
    return f, s, {F: _Served(f), S: _Served(s)}


def _query_over(providers):
    """`LLMManager.query` as far as the doors see it: the named alias answers."""

    async def query(prompt, provider_alias=None, images=None, return_metadata=False, **_):
        text = await providers[provider_alias].generate_response(prompt)
        return {"response": text, "provider": provider_alias, **METADATA}

    return query


def _peer_door(tmp_path, providers):
    coord, svc = make_coordinator()
    # Gate stubbed: these tests are about the served call; the gate has its own.
    svc.firewall.can_request_inference.return_value = True
    svc.firewall.compute_serving_vendor = []
    svc.llm_manager.providers = providers
    svc.llm_manager.query = _query_over(providers)
    coord._ledger = NodeLedger(tmp_path / "peer-ledger")
    return coord, svc


async def _peer_call(coord, svc, alias=F, peer=PEER):
    """One call through the peer door; what it answered."""
    await coord.handle_inference_request(peer, uuid.uuid4().hex, "ping", provider=alias)
    return svc.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]


def _gateway(tmp_path, providers, compute=None, inference_lock=None):
    compute = compute or {"serving_local": list(providers)}
    service = _service(tmp_path, compute, providers=providers)
    service.llm_manager.query = _query_over(providers)
    service.p2p_manager.node_id = NODE
    return Gateway(service, ledger=NodeLedger(tmp_path / "gateway-ledger"), inference_lock=inference_lock)


class _Looks:
    """What the swap's busy test answers about `holder`, read as it answers: a start has been
    seen to wait only when it has looked with the clock as the test set it."""

    def __init__(self, holder):
        self.answers = []
        real = holder._busy_for_the_card

        def looked(state):
            self.answers.append(real(state))
            return self.answers[-1]

        holder._busy_for_the_card = looked

    async def _another(self, seen):
        while len(self.answers) <= seen:
            await asyncio.sleep(0.002)
        return self.answers[-1]

    async def next(self):
        """The next answer given from now on."""
        return await asyncio.wait_for(self._another(len(self.answers)), 10)


def _swapped(frm, to):
    swap = base_mod.last_swap()
    assert swap is not None, "no swap was made"
    assert (swap["from"], swap["to"]) == (frm, to)


class TestAPeersRunOfCalls:
    @pytest.mark.asyncio
    async def test_a_start_on_the_other_alias_waits_at_119_s_after_the_last_call_and_swaps_at_121(
        self, tmp_path, clock,
    ):
        f, s, engines = _pair(tmp_path)
        coord, svc = _peer_door(tmp_path, engines)
        await f.ensure_running()
        for _ in range(3):
            assert (await _peer_call(coord, svc)).get("response") == "described"
            last = clock.now
            clock.now += 30

        clock.now = last + 119
        looks = _Looks(f)
        start = asyncio.create_task(s.ensure_running())
        try:
            assert await looks.next() is True
            assert await looks.next() is True, "still busy at the next look: the clock did not move"
            _untouched(f)
            assert not start.done()

            clock.now = last + 121
            await asyncio.wait_for(start, 30)
        finally:
            await _finish(start)
        assert not _alive(f) and _alive(s)
        _swapped(F, S)

    @pytest.mark.asyncio
    async def test_calls_inside_the_window_renew_it_and_a_waiting_start_does_not_stop_them(
        self, tmp_path, clock,
    ):
        f, s, engines = _pair(tmp_path)
        coord, svc = _peer_door(tmp_path, engines)
        await f.ensure_running()
        await _peer_call(coord, svc)
        first = clock.now

        clock.now = first + 100
        looks = _Looks(f)
        start = asyncio.create_task(s.ensure_running())
        try:
            assert await looks.next() is True
            clock.now = first + 110
            assert (await _peer_call(coord, svc)).get("response") == "described"  # served, not refused

            clock.now = first + 125  # past the first call's window, inside the second's
            assert await looks.next() is True
            _untouched(f)

            clock.now = first + 110 + 121
            await asyncio.wait_for(start, 30)
        finally:
            await _finish(start)
        _swapped(F, S)

    @pytest.mark.asyncio
    async def test_a_gap_past_the_window_cuts_that_callers_loop_and_that_is_the_trade_off(
        self, tmp_path, clock,
    ):
        # Rule 6 on purpose: DPC cannot tell a slow loop from a finished one. A caller whose next
        # call comes later than the window finds the card gone and is refused, not queued.
        f, s, engines = _pair(tmp_path)
        coord, svc = _peer_door(tmp_path, engines)
        await f.ensure_running()
        await _peer_call(coord, svc)
        last = clock.now

        clock.now = last + 121
        await asyncio.wait_for(s.ensure_running(), 30)
        _swapped(F, S)

        clock.now = last + 130  # the loop's next call
        answer = await _peer_call(coord, svc)
        assert answer["code"] == "provider_unavailable" and F in answer["error"]
        assert _alive(s) and not _alive(f)

    @pytest.mark.asyncio
    async def test_a_window_of_zero_is_today_s_behaviour(self, tmp_path, clock, settings):
        settings.set_local_engine_swap(True, 300, 0)
        f, s, engines = _pair(tmp_path)
        coord, svc = _peer_door(tmp_path, engines)
        await f.ensure_running()
        await _peer_call(coord, svc)

        await asyncio.wait_for(s.ensure_running(), 30)  # the clock has not moved

        _swapped(F, S)
        assert base_mod._last_caller_call_at == {}

    @pytest.mark.asyncio
    async def test_the_window_is_read_at_each_look_so_a_change_needs_no_restart(self, tmp_path, clock, settings):
        f, s, engines = _pair(tmp_path)
        coord, svc = _peer_door(tmp_path, engines)
        await f.ensure_running()
        await _peer_call(coord, svc)

        looks = _Looks(f)
        start = asyncio.create_task(s.ensure_running())
        try:
            assert await looks.next() is True
            settings.set_local_engine_swap(True, 300, 10)
            clock.now += 11
            await asyncio.wait_for(start, 30)
        finally:
            await _finish(start)
        _swapped(F, S)

    @pytest.mark.asyncio
    async def test_a_peer_never_swaps_and_its_refused_call_opens_no_window(self, tmp_path, clock):
        # Rule 2 of the idle swap stands: the window is what a peer leaves behind, never what it asks for.
        f, s, engines = _pair(tmp_path)
        coord, svc = _peer_door(tmp_path, engines)
        await f.ensure_running()
        clock.now += 500  # no window stands on F: its holder is idle

        answer = await _peer_call(coord, svc, alias=S)

        assert answer["code"] == "provider_unavailable" and S in answer["error"]
        _untouched(f)
        assert not _alive(s)
        assert base_mod.last_swap() is None
        assert base_mod._last_caller_call_at == {}

    @pytest.mark.asyncio
    async def test_a_failed_call_is_not_a_served_call_and_leaves_no_window(self, tmp_path, clock):
        f, _s, engines = _pair(tmp_path)
        coord, svc = _peer_door(tmp_path, engines)
        await f.ensure_running()

        async def fails(prompt, **_):
            raise RuntimeError("the engine answered nonsense")

        engines[F].generate_response = fails
        answer = await _peer_call(coord, svc)

        assert "nonsense" in answer["error"]
        assert base_mod._last_caller_call_at == {}

    @pytest.mark.asyncio
    async def test_an_alias_with_no_card_leaves_no_record(self, tmp_path, clock):
        vendor = _NoCard()
        coord, svc = _peer_door(tmp_path, {"vendor": vendor})

        assert (await _peer_call(coord, svc, alias="vendor")).get("response") == "the answer"

        assert vendor.called == 1
        assert base_mod._last_caller_call_at == {}

    @pytest.mark.asyncio
    async def test_a_refusal_names_the_caller_in_its_window_and_the_holder_stays_as_it_was(
        self, tmp_path, clock, settings,
    ):
        settings.set_local_engine_swap(True, 60)
        f, s, engines = _pair(tmp_path)
        coord, svc = _peer_door(tmp_path, engines)
        await f.ensure_running()
        await _peer_call(coord, svc)

        looks = _Looks(f)
        start = asyncio.create_task(s.ensure_running())
        try:
            assert await looks.next() is True
            clock.now += 60  # the start's own limit comes before the end of the window
            with pytest.raises(base_mod.CardHeldError) as refused:
                await asyncio.wait_for(start, 10)
        finally:
            await _finish(start)
        assert "busy for 60 s" in str(refused.value)
        assert str(refused.value).endswith(f"serves as before. In use by caller '{PEER}'.")
        _untouched(f)

    @pytest.mark.asyncio
    async def test_a_dead_child_holds_nothing_whatever_called_it_a_moment_ago(self, tmp_path, clock):
        f, s, engines = _pair(tmp_path)
        coord, svc = _peer_door(tmp_path, engines)
        await f.ensure_running()
        await _peer_call(coord, svc)

        f._proc.returncode = 1
        await asyncio.wait_for(s.ensure_running(), 30)

        assert _alive(s)
        assert base_mod.last_swap() is None  # nothing had to be swapped out

    @pytest.mark.asyncio
    async def test_the_window_stands_on_the_alias_so_a_settings_save_replacing_the_supervisor_keeps_it(
        self, tmp_path, clock,
    ):
        f, _s, engines = _pair(tmp_path)
        coord, svc = _peer_door(tmp_path, engines)
        await f.ensure_running()
        await _peer_call(coord, svc)

        replaced = _strata(tmp_path / "f2", F)

        assert replaced._busy_for_the_card("running") is True
        clock.now += 121
        assert replaced._busy_for_the_card("running") is False

    def test_a_window_is_not_a_kind_of_hold(self):
        # A hold is entered and left around a body; a window has no body. It is a test of the clock.
        async def enter():
            async with base_mod.card_held_for(F, "caller_window"):
                pass

        with pytest.raises(ValueError, match="caller_window"):
            asyncio.run(enter())
        assert _held() == {}


class TestAGatewayClientsRunOfCalls:
    @pytest.mark.asyncio
    async def test_a_start_on_the_other_alias_waits_at_119_s_after_the_last_call_and_swaps_at_121(
        self, tmp_path, clock,
    ):
        f, s, engines = _pair(tmp_path)
        gateway = _gateway(tmp_path, engines)
        await f.ensure_running()
        for _ in range(3):
            assert (await gateway.complete(F, "ping")).text == "described"
            last = clock.now
            clock.now += 30

        clock.now = last + 119
        looks = _Looks(f)
        start = asyncio.create_task(s.ensure_running())
        try:
            assert await looks.next() is True
            assert await looks.next() is True
            _untouched(f)
            assert not start.done()

            clock.now = last + 121
            await asyncio.wait_for(start, 30)
        finally:
            await _finish(start)
        assert not _alive(f) and _alive(s)
        _swapped(F, S)

    @pytest.mark.asyncio
    async def test_calls_inside_the_window_renew_it(self, tmp_path, clock):
        f, s, engines = _pair(tmp_path)
        gateway = _gateway(tmp_path, engines)
        await f.ensure_running()
        await gateway.complete(F, "ping")
        first = clock.now

        clock.now = first + 100
        looks = _Looks(f)
        start = asyncio.create_task(s.ensure_running())
        try:
            assert await looks.next() is True
            clock.now = first + 110
            assert (await gateway.complete(F, "ping")).text == "described"

            clock.now = first + 125
            assert await looks.next() is True
            _untouched(f)

            clock.now = first + 110 + 121
            await asyncio.wait_for(start, 30)
        finally:
            await _finish(start)
        _swapped(F, S)

    @pytest.mark.asyncio
    async def test_a_gap_past_the_window_cuts_that_clients_loop_too(self, tmp_path, clock):
        f, s, engines = _pair(tmp_path)
        gateway = _gateway(tmp_path, engines)
        await f.ensure_running()
        await gateway.complete(F, "ping")

        clock.now += 121
        await asyncio.wait_for(s.ensure_running(), 30)

        _swapped(F, S)
        assert not _alive(f)

    @pytest.mark.asyncio
    async def test_a_gateway_call_stands_on_its_alias_until_it_is_answered(self, tmp_path, clock, settings):
        # No window and no call in flight at the moment that counts: only the hold can say busy.
        settings.set_local_engine_swap(True, 300, 0)
        f, s, engines = _pair(tmp_path)
        gateway = _gateway(tmp_path, engines)
        await f.ensure_running()
        gap = _Gap()
        answer = engines[F].generate_response

        async def after_a_gap(prompt, **kwargs):
            await gap()
            return await answer(prompt, **kwargs)

        engines[F].generate_response = after_a_gap
        call = asyncio.create_task(gateway.complete(F, "ping"))
        start = None
        try:
            await asyncio.wait_for(gap.reached.wait(), 10)
            assert _held() == {F: 1}
            assert f._in_flight == 0

            looks = _Looks(f)
            start = asyncio.create_task(s.ensure_running())
            assert await looks.next() is True
            _untouched(f)

            gap.go_on.set()
            assert (await asyncio.wait_for(call, 10)).text == "described"
            assert _held() == {}
            await asyncio.wait_for(start, 30)
        finally:
            await _finish(*[t for t in (call, start) if t is not None])
        _swapped(F, S)

    @pytest.mark.asyncio
    async def test_a_client_waiting_on_the_semaphore_stands_on_its_alias_from_acceptance(
        self, tmp_path, clock,
    ):
        # Turned over by rule 8: the hold moved out to acceptance when the start moved out of the
        # semaphore, so the cycle it used to make is gone. It read "stands on nothing".
        _f, _s, engines = _pair(tmp_path)
        lock = asyncio.Semaphore(1)
        gateway = _gateway(tmp_path, engines, inference_lock=lock)
        await lock.acquire()
        call = asyncio.create_task(gateway.complete(F, "ping"))
        try:
            await asyncio.sleep(0.02)
            assert not call.done() and _held() == {F: 1}
            lock.release()
            assert (await asyncio.wait_for(call, 30)).text == "described"
        finally:
            await _finish(call)
        assert _held() == {}

    @pytest.mark.asyncio
    async def test_two_clients_on_excluding_aliases_do_not_wait_on_each_other(self, tmp_path, clock, settings):
        settings.set_local_engine_swap(True, 300, 0)
        f, s, engines = _pair(tmp_path)
        gateway = _gateway(tmp_path, engines)
        await f.ensure_running()

        on_s = asyncio.create_task(gateway.complete(S, "ping"))
        on_f = asyncio.create_task(gateway.complete(F, "ping"))
        try:
            done = await asyncio.wait_for(asyncio.gather(on_s, on_f), 30)
        finally:
            await _finish(on_s, on_f)
        assert [c.text for c in done] == ["described", "described"]
        assert _alive(f) and not _alive(s)

    @pytest.mark.asyncio
    async def test_a_failed_call_leaves_no_window_and_lets_go_of_the_alias(self, tmp_path, clock):
        f, _s, engines = _pair(tmp_path)
        gateway = _gateway(tmp_path, engines)
        await f.ensure_running()

        async def fails(prompt, **_):
            raise RuntimeError("the engine answered nonsense")

        engines[F].generate_response = fails
        with pytest.raises(GatewayError) as failed:
            await gateway.complete(F, "ping")

        assert failed.value.status == 502
        assert _held() == {}
        assert base_mod._last_caller_call_at == {}

    @pytest.mark.asyncio
    async def test_a_vendor_alias_and_a_local_alias_with_no_supervisor_leave_no_record_and_no_hold(
        self, tmp_path, clock,
    ):
        seen_during_the_call = []
        vendor, ollama = _NoCard(), _NoCard("ollama", "qwen3:8b")
        answer = vendor.generate_response

        async def noting(prompt, **kwargs):
            seen_during_the_call.append(_held())
            return await answer(prompt, **kwargs)

        vendor.generate_response = noting
        ollama.generate_response = noting
        gateway = _gateway(tmp_path, {"vendor": vendor, "ollama": ollama}, compute={
            "serving_local": ["ollama"], "serving_vendor": ["vendor"], "vendor_quotas": {"vendor": 5.0},
        })

        assert (await gateway.complete("vendor", "ping")).text == "the answer"
        assert (await gateway.complete("ollama", "ping")).text == "the answer"

        assert seen_during_the_call == [{}, {}]
        assert base_mod._last_caller_call_at == {}


class TestTheGatewayAndAPeersWindow:
    @pytest.mark.asyncio
    async def test_a_gateway_start_outside_the_semaphore_lets_a_peers_next_call_renew_its_window(
        self, tmp_path, clock,
    ):
        # Turned over by rule 8. It read "inside the semaphore lets a peer's window lapse": the
        # gateway waited for the card sitting on the semaphore, and the peer's next call waited
        # behind it until its window ran out. Now that call is served and renews the window.
        f, s, engines = _pair(tmp_path)
        coord, svc = _peer_door(tmp_path, engines)
        gateway = _gateway(tmp_path, engines, inference_lock=coord._peer_inference_lock)
        await f.ensure_running()
        await _peer_call(coord, svc)
        last = clock.now

        looks = _Looks(f)
        clock.now = last + 100
        on_s = asyncio.create_task(gateway.complete(S, "ping"))
        next_call = None
        try:
            assert await looks.next() is True
            clock.now = last + 110
            next_call = asyncio.create_task(_peer_call(coord, svc))
            assert (await asyncio.wait_for(next_call, 5)).get("response") == "described"

            clock.now = last + 121  # the first call's window has ended, the renewed one has not
            assert await looks.next() is True
            _untouched(f)
            assert not on_s.done()

            clock.now = last + 110 + 121
            assert (await asyncio.wait_for(on_s, 30)).text == "described"
        finally:
            await _finish(*[t for t in (on_s, next_call) if t is not None])
        _swapped(F, S)


class TestWithTheSwapOff:
    @pytest.mark.asyncio
    async def test_a_recent_caller_changes_nothing_and_the_refusal_is_today_s_word_for_word(
        self, tmp_path, clock, settings,
    ):
        settings.set_local_engine_swap(False, 300)
        f, s, engines = _pair(tmp_path)
        coord, svc = _peer_door(tmp_path, engines)
        await f.ensure_running()

        with pytest.raises(base_mod.CardHeldError) as nobody_called:
            await s.ensure_running()

        await _peer_call(coord, svc)
        with pytest.raises(base_mod.CardHeldError) as a_caller_is_recent:
            await asyncio.wait_for(s.ensure_running(), 5)

        assert str(a_caller_is_recent.value) == str(nobody_called.value)
        assert "swapping is off" in str(nobody_called.value)
        _untouched(f)
        assert base_mod.last_swap() is None

    @pytest.mark.asyncio
    async def test_a_gateway_call_on_a_held_card_gets_today_s_answer(self, tmp_path, clock, settings):
        settings.set_local_engine_swap(False, 300)
        f, _s, engines = _pair(tmp_path)
        gateway = _gateway(tmp_path, engines)
        await f.ensure_running()

        with pytest.raises(GatewayError) as refused:
            await asyncio.wait_for(gateway.complete(S, "ping"), 5)

        assert refused.value.status == 502 and "swapping is off" in str(refused.value)
        _untouched(f)
        assert _held() == {}
        assert base_mod._last_caller_call_at == {}


class TestAStaleEntry:
    def test_a_note_drops_every_ended_window_on_every_alias(self, clock):
        for n in range(1000):
            base_mod.note_caller_call(f"alias-{n % 7}", f"peer-{n}")
        clock.now += 121

        base_mod.note_caller_call(F, PEER)

        assert base_mod._last_caller_call_at == {F: {PEER: clock.now}}

    def test_with_the_swap_off_a_run_of_notes_does_not_grow_either(self, clock, settings):
        settings.set_local_engine_swap(False, 300)
        for n in range(1000):
            base_mod.note_caller_call(F, f"peer-{n}")
            clock.now += 121
        assert len(base_mod._last_caller_call_at[F]) == 1

    def test_the_busy_test_drops_them_too_and_keeps_a_window_still_open(self, clock, tmp_path):
        f = _strata(tmp_path / "f", F)
        base_mod.note_caller_call(S, "old")
        clock.now += 100
        base_mod.note_caller_call(F, "recent")
        clock.now += 21

        assert f._busy_for_the_card("running") is True
        assert base_mod._last_caller_call_at == {F: {"recent": clock.now - 21}}

        clock.now += 100
        assert f._busy_for_the_card("running") is False
        assert base_mod._last_caller_call_at == {}

    def test_a_window_of_zero_keeps_nothing(self, clock, settings):
        settings.set_local_engine_swap(True, 300, 0)
        base_mod.note_caller_call(F, PEER)
        assert base_mod._last_caller_call_at == {}

    def test_with_no_settings_bound_nothing_is_kept(self, clock):
        base_mod.bind_local_engine_settings(None)
        base_mod.note_caller_call(F, PEER)
        assert base_mod._last_caller_call_at == {}

    @pytest.mark.asyncio
    async def test_a_note_from_another_loop_lands_on_the_owner_loop(self, clock):
        owner = asyncio.get_running_loop()
        base_mod.bind_owner_loop(owner)
        wrote_on = []
        real = base_mod._drop_ended_windows

        def spying():
            wrote_on.append(asyncio.get_running_loop())
            real()

        base_mod._drop_ended_windows = spying
        try:
            async def elsewhere():
                base_mod.note_caller_call(F, PEER)

            await asyncio.to_thread(lambda: asyncio.run(elsewhere()))
            await asyncio.sleep(0)
        finally:
            base_mod._drop_ended_windows = real
        assert wrote_on == [owner]
        assert base_mod._last_caller_call_at == {F: {PEER: clock.now}}


class TestTheSetting:
    def test_the_default_is_a_hundred_and_twenty(self, tmp_path):
        assert Settings(tmp_path / "h").get_caller_idle_s() == 120

    def test_a_config_without_the_key_reads_the_same_default(self, tmp_path):
        home = tmp_path / "h"
        home.mkdir()
        (home / "config.ini").write_text("[hub]\nurl = http://localhost:8000\n", encoding="utf-8")
        assert Settings(home).get_caller_idle_s() == 120

    @pytest.mark.parametrize("raw", ["-1", "1.5", "soon", ""])
    def test_a_hand_edited_value_that_is_not_a_whole_number_reads_as_the_default(self, tmp_path, raw):
        home = tmp_path / "h"
        home.mkdir()
        (home / "config.ini").write_text(f"[local_engines]\ncaller_idle_s = {raw}\n", encoding="utf-8")
        assert Settings(home).get_caller_idle_s() == 120

    def test_a_choice_round_trips_and_survives_a_reload(self, tmp_path):
        home = tmp_path / "h"
        Settings(home).set_local_engine_swap(False, 7, 0)
        again = Settings(home)
        assert (again.get_swap_when_idle(), again.get_swap_wait_s(), again.get_caller_idle_s()) == (False, 7, 0)

    def test_without_the_third_argument_the_stored_value_stays(self, tmp_path):
        s = Settings(tmp_path / "h")
        s.set_local_engine_swap(True, 300, 45)
        s.set_local_engine_swap(False, 7)
        assert s.get_caller_idle_s() == 45

    @pytest.mark.parametrize("bad", [-1, 1.5, True, "5"])
    def test_a_bad_value_is_refused_and_nothing_is_written(self, tmp_path, bad):
        home = tmp_path / "h"
        s = Settings(home)
        s.set_local_engine_swap(False, 7, 9)
        before = (home / "config.ini").read_bytes()
        with pytest.raises(ValueError, match="caller_idle_s"):
            s.set_local_engine_swap(True, 300, bad)
        assert (home / "config.ini").read_bytes() == before

    @pytest.mark.asyncio
    async def test_the_command_takes_it_and_the_status_reports_it(self, settings):
        host = SimpleNamespace(settings=settings)
        out = await CoreService.set_local_engine_swap(host, enabled=True, wait_s=300, caller_idle_s=45)
        assert out == {"status": "success", "swap_when_idle": True, "swap_wait_s": 300, "caller_idle_s": 45}
        assert (await CoreService.local_engine_status(host))["caller_idle_s"] == 45

    @pytest.mark.asyncio
    async def test_the_editor_s_two_arguments_leave_it_alone(self, settings):
        host = SimpleNamespace(settings=settings)
        await CoreService.set_local_engine_swap(host, enabled=True, wait_s=300, caller_idle_s=45)
        out = await CoreService.set_local_engine_swap(host, enabled=False, wait_s=60)
        assert out["caller_idle_s"] == 45 and settings.get_caller_idle_s() == 45

    @pytest.mark.asyncio
    async def test_the_command_answers_a_bad_value_as_an_error_and_writes_nothing(self, settings):
        before = (settings.dpc_home_dir / "config.ini").read_bytes()
        out = await CoreService.set_local_engine_swap(
            SimpleNamespace(settings=settings), enabled=True, wait_s=300, caller_idle_s=-1,
        )
        assert out["status"] == "error" and "caller_idle_s" in out["message"]
        assert (settings.dpc_home_dir / "config.ini").read_bytes() == before
