"""A call served to a peer parks only where the conversation cache has room
(ADR-044 point 9, amendment 2026-11-13: variant C2, voted by CC, Zcode and
Johnny; Ark pending).

`StrataSupervisor.guest_checkpoint` decides, right before the call, from the
launcher's `/metrics` `conversation_cache`, free physical RAM and the peer's
own earlier calls:

- slots: parked, less this peer's own entry, plus our live conversation, plus
  the peer's snapshot, plus one slot kept free (Zcode's condition), within `slots`;
- MiB: `bytes` plus the snapshot estimate within `budget_mib`;
- physical RAM: free less the estimate at least the engine's floor;
- one guest entry for all peers;
- the estimate: 750 MiB + 13.8 MiB per 1000 tokens of the peer's last call plus
  the growth between its last two calls, or x1.15 with one call known
  (Johnny's condition (c));
- `/metrics` silent, no estimate, or the alias field `guest_conversation_cache`
  off: off.

One INFO line per decision prints the inputs and the answer (Johnny's (b)).
"""

import asyncio
import logging
import re
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.managers import strata_supervisor as strata_mod
from dpc_client_core.managers.strata_supervisor import (
    GUEST_SLOTS_KEPT_FREE,
    StrataSupervisor,
    guest_cache_room,
    guest_snapshot_estimate,
    strata_config_errors,
)

LOGGER = "dpc_client_core.managers.strata_supervisor"
PEER, OTHER = "dpc-node-guest-a", "dpc-node-guest-b"
GIB = 1 << 30


def _cache(parked=0, slots=3, held=0, budget=10240, evictions=0, enabled=True):
    return {"enabled": enabled, "parked": parked, "slots": slots, "bytes": held,
            "budget_mib": budget, "evictions": evictions}


def _sup(tmp_path, cache=None, floor=1000, **config):
    sup = StrataSupervisor("flash", {
        "strata_install_dir": str(tmp_path), "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240, **config,
    })
    sup._engine_log_path = tmp_path / "strata-flash-engine.log"
    body = {"conversation_cache": cache if cache is not None else _cache(),
            "engine": {"conversation_cache_min_free_mib": floor}}
    sup._get = AsyncMock(return_value=body)
    return sup


@pytest.fixture(autouse=True)
def _ram(monkeypatch):
    monkeypatch.setattr(strata_mod, "read_free_commit_or_physical",
                        lambda: {"avail_phys_mib": 40000, "total_phys_mib": 63168})


def _decisions(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == LOGGER and "a call served to peer" in r.getMessage()]


# --- the estimate (Johnny's (c)) -------------------------------------------------------


class TestTheEstimate:
    def test_no_call_known_is_no_estimate(self):
        assert guest_snapshot_estimate([]) is None

    def test_one_call_known_grows_by_fifteen_percent(self):
        est = guest_snapshot_estimate([100000])
        assert est["tokens"] == 115000
        assert est["mib"] == 2337  # 750 + 13.8 * 115, no float noise rounded up
        assert "x1.15" in est["basis"]

    def test_two_calls_known_add_the_growth_between_them(self):
        est = guest_snapshot_estimate([123732, 125000])
        assert est["tokens"] == 125000 + 1268
        assert "1268 grown" in est["basis"]

    def test_a_shrunk_conversation_adds_no_growth(self):
        assert guest_snapshot_estimate([140682, 130059])["tokens"] == 130059

    def test_the_fit_is_within_its_residual_of_a_measured_snapshot(self):
        """143622 tokens parked as 2737 MiB on this alias; the fit's residuals run
        from -528 to +33 MiB (ideas/peer-cache-variant-c-2026-11-12.md, question 1)."""
        est = guest_snapshot_estimate([143622, 143622])
        assert est["tokens"] == 143622 and 2737 - 33 <= est["mib"] <= 2737 + 528


# --- the room (C2, Zcode's free slot) --------------------------------------------------


class TestTheRoom:
    def _room(self, cache, est=2000, free=40000, floor=1000, own=False):
        return guest_cache_room(cache, estimate_mib=est, free_physical_mib=free,
                                floor_mib=floor, own_entry=own)[0]

    def test_one_slot_is_always_kept_free(self):
        assert GUEST_SLOTS_KEPT_FREE == 1
        # 3 slots: nothing parked -> 0 + 2 + 1 = 3, room; one parked -> 4 > 3, none.
        assert self._room(_cache(parked=0, slots=3)) == []
        assert self._room(_cache(parked=1, slots=3)) == ["slots: 4 needed of 3"]

    def test_with_two_slots_there_is_never_room(self):
        """Today's alias: the rule is inert until the owner raises the slots."""
        for parked in (0, 1, 2):
            assert any(r.startswith("slots:") for r in self._room(_cache(parked=parked, slots=2)))

    def test_the_peers_own_entry_is_not_counted(self):
        assert self._room(_cache(parked=1, slots=3), own=True) == []

    def test_budget_short_is_no_room(self):
        reasons = self._room(_cache(held=9 * GIB, budget=10240), est=2000)
        assert reasons == ["MiB: 11216 needed of budget 10240"]

    def test_the_ram_floor_short_is_no_room(self):
        reasons = self._room(_cache(), est=2000, free=2900, floor=1000)
        assert reasons == ["physical RAM: 900 MiB left, under the floor 1000"]

    def test_unknown_values_are_reasons_not_guesses(self):
        assert self._room({"parked": 0, "slots": None, "bytes": 0, "budget_mib": 10240})
        assert self._room(_cache(), free=None) == ["free physical RAM is unreadable"]
        assert self._room(_cache(enabled=False))


# --- the decision, both sides (Johnny's (d)) ------------------------------------------


class TestTheDecision:
    @pytest.mark.asyncio
    async def test_a_free_slot_and_room_turn_it_on(self, tmp_path):
        sup = _sup(tmp_path, _cache(parked=0, slots=3))
        sup.note_guest_call(PEER, 125000, False)
        assert await sup.guest_checkpoint(PEER) is True

    @pytest.mark.asyncio
    async def test_slots_full_turn_it_off(self, tmp_path):
        sup = _sup(tmp_path, _cache(parked=2, slots=2, held=5 * GIB))
        sup.note_guest_call(PEER, 125000, False)
        assert await sup.guest_checkpoint(PEER) is False

    @pytest.mark.asyncio
    async def test_no_room_by_budget_or_ram_turns_it_off(self, tmp_path, monkeypatch):
        sup = _sup(tmp_path, _cache(parked=0, slots=4, held=8 * GIB))
        sup.note_guest_call(PEER, 125000, False)
        assert await sup.guest_checkpoint(PEER) is False
        sup = _sup(tmp_path, _cache(parked=0, slots=4))
        sup.note_guest_call(PEER, 125000, False)
        monkeypatch.setattr(strata_mod, "read_free_commit_or_physical", lambda: {"avail_phys_mib": 3000})
        assert await sup.guest_checkpoint(PEER) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", [ConnectionError("refused"), asyncio.TimeoutError()])
    async def test_metrics_not_answering_turns_it_off(self, tmp_path, failure, caplog):
        sup = _sup(tmp_path)
        sup.note_guest_call(PEER, 125000, False)
        sup._get = AsyncMock(side_effect=failure)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert await sup.guest_checkpoint(PEER) is False
        (line,) = _decisions(caplog)
        assert "did not answer" in line
        assert re.search(r"; /metrics read took \d+ ms; ", line)

    @pytest.mark.asyncio
    async def test_metrics_slower_than_the_limit_turns_it_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(strata_mod, "GUEST_METRICS_TIMEOUT_S", 0.01)
        sup = _sup(tmp_path)
        sup.note_guest_call(PEER, 125000, False)

        async def slow(path):
            await asyncio.sleep(1)

        sup._get = slow
        assert await sup.guest_checkpoint(PEER) is False

    @pytest.mark.asyncio
    async def test_metrics_without_the_cache_turns_it_off(self, tmp_path):
        sup = _sup(tmp_path)
        sup.note_guest_call(PEER, 125000, False)
        sup._get = AsyncMock(return_value={"live": {"state": "idle"}})
        assert await sup.guest_checkpoint(PEER) is False

    @pytest.mark.asyncio
    async def test_the_field_off_turns_it_off_without_reading_metrics(self, tmp_path, caplog):
        sup = _sup(tmp_path, guest_conversation_cache=False)
        sup.note_guest_call(PEER, 125000, False)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert await sup.guest_checkpoint(PEER) is False
        sup._get.assert_not_called()
        assert "guest_conversation_cache is off" in _decisions(caplog)[0]

    @pytest.mark.asyncio
    async def test_the_field_is_on_when_absent(self, tmp_path):
        assert _sup(tmp_path).config["guest_conversation_cache"] is True

    @pytest.mark.asyncio
    async def test_a_first_call_has_no_estimate_and_is_off(self, tmp_path, caplog):
        sup = _sup(tmp_path)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert await sup.guest_checkpoint(PEER) is False
        assert "no earlier call of this peer" in _decisions(caplog)[0]


class TestOneGuestEntry:
    @pytest.mark.asyncio
    async def test_a_second_peer_is_off_while_the_first_holds_the_entry(self, tmp_path):
        sup = _sup(tmp_path, _cache(parked=0, slots=4))
        sup.note_guest_call(PEER, 125000, True)
        sup.note_guest_call(OTHER, 1000, False)
        assert await sup.guest_checkpoint(OTHER) is False
        assert await sup.guest_checkpoint(PEER) is True

    @pytest.mark.asyncio
    async def test_a_call_that_went_out_off_gives_the_entry_back(self, tmp_path):
        sup = _sup(tmp_path, _cache(parked=0, slots=4))
        sup.note_guest_call(PEER, 125000, True)
        sup.note_guest_call(PEER, 126000, False)
        sup.note_guest_call(OTHER, 1000, False)
        assert await sup.guest_checkpoint(OTHER) is True

    @pytest.mark.asyncio
    async def test_an_eviction_since_drops_the_entry(self, tmp_path):
        sup = _sup(tmp_path, _cache(parked=0, slots=4, evictions=7))
        sup._cache_evictions_polled = 7
        sup.note_guest_call(PEER, 125000, True)
        sup.note_guest_call(OTHER, 1000, False)
        sup._get = AsyncMock(return_value={"conversation_cache": _cache(parked=0, slots=4, evictions=8)})
        assert await sup.guest_checkpoint(OTHER) is True

    @pytest.mark.asyncio
    async def test_a_new_launch_drops_the_entry_and_keeps_the_sizes(self, tmp_path):
        sup = _sup(tmp_path, _cache(parked=0, slots=4))
        sup.note_guest_call(PEER, 125000, True)
        sup.prime_restore_scan()
        assert sup._guest_entry_holder() is None
        assert sup._guest_sizes[PEER] == [125000]

    def test_only_the_last_two_sizes_and_a_bounded_number_of_peers_are_kept(self, tmp_path):
        sup = _sup(tmp_path)
        for size in (1, 2, 3):
            sup.note_guest_call(PEER, size, False)
        assert sup._guest_sizes[PEER] == [2, 3]
        for n in range(40):
            sup.note_guest_call(f"dpc-node-{n}", 10, False)
        assert len(sup._guest_sizes) == strata_mod._GUEST_SIZES_KEPT
        assert PEER not in sup._guest_sizes

    def test_a_call_with_no_reported_size_adds_none(self, tmp_path):
        sup = _sup(tmp_path)
        sup.note_guest_call(PEER, None, True)
        assert PEER not in sup._guest_sizes and sup._guest_entry_holder() == PEER


class TestTheLine:
    @pytest.mark.asyncio
    async def test_it_prints_the_inputs_the_estimate_and_the_answer(self, tmp_path, caplog):
        sup = _sup(tmp_path, _cache(parked=1, slots=3, held=2 * GIB), floor=1000)
        sup.note_guest_call(PEER, 123000, False)
        sup.note_guest_call(PEER, 125000, False)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert await sup.guest_checkpoint(PEER) is False
        (line,) = _decisions(caplog)
        assert f"peer {PEER} goes out with checkpointing off" in line
        assert re.search(r"\(guest_conversation_cache\); /metrics read took \d+ ms; slots:", line)
        assert "slots: parked 1 - 0 own + 2" in line and "+ 1 kept free = 4 of 3" in line
        assert "MiB: 2048 held + 2503 estimate of budget 10240" in line
        assert "physical RAM: 40000 MiB free - 2503 estimate against the floor 1000" in line
        assert "snapshot estimate: 2503 MiB for 127000 tokens (last call 125000 tokens + 2000 grown" in line
        assert "off because slots: 4 needed of 3" in line

    @pytest.mark.asyncio
    async def test_on_says_no_reason_and_names_no_agent_or_conversation(self, tmp_path, caplog):
        sup = _sup(tmp_path, _cache(parked=0, slots=3))
        sup.note_guest_call(PEER, 125000, False)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert await sup.guest_checkpoint(PEER) is True
        (line,) = _decisions(caplog)
        assert "checkpointing on" in line and "off because" not in line
        # The only node it names is the peer; no conversation or agent id is printed.
        assert line.count("dpc-node-") == 1
        assert "conv=" not in line and "agent_id" not in line and "conversation_id" not in line

    @pytest.mark.asyncio
    async def test_the_floor_falls_back_to_the_alias_value(self, tmp_path, caplog):
        sup = _sup(tmp_path, conversation_cache_min_free_mib=1234)
        sup._get = AsyncMock(return_value={"conversation_cache": _cache()})
        sup.note_guest_call(PEER, 1000, False)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            await sup.guest_checkpoint(PEER)
        assert "against the floor 1234" in _decisions(caplog)[0]


class TestTheField:
    def test_a_value_that_is_not_true_or_false_is_refused_on_save(self, tmp_path):
        base = {"strata_base_config": str(tmp_path / "b.json"), "conversation_cache_mib": 10240}
        assert not any("guest_conversation_cache" in e for e in strata_config_errors(
            {**base, "guest_conversation_cache": False}))
        errors = strata_config_errors({**base, "guest_conversation_cache": "yes"})
        assert any("guest_conversation_cache: expected true or false" in e for e in errors)

    def test_it_does_not_restart_the_launcher(self):
        from dpc_client_core.providers.strata_provider import _STRATA_FLAG_KEYS

        assert "guest_conversation_cache" not in _STRATA_FLAG_KEYS


class TestTheEntryIsReservedWhenTheDecisionIsOn:
    @pytest.mark.asyncio
    async def test_a_call_decided_on_and_cancelled_with_no_usage_still_holds_the_entry(self, tmp_path):
        sup = _sup(tmp_path, _cache(parked=0, slots=4))
        sup.note_guest_call(PEER, 125000, False)
        sup.note_guest_call(OTHER, 1000, False)
        # The call goes out on, and is cancelled before its usage chunk: no note_guest_call.
        assert await sup.guest_checkpoint(PEER) is True
        assert sup._guest_entry_holder() == PEER
        assert await sup.guest_checkpoint(OTHER) is False
        # The first peer's own next call still decides normally.
        assert await sup.guest_checkpoint(PEER) is True

    @pytest.mark.asyncio
    async def test_a_decision_that_is_off_reserves_nothing(self, tmp_path):
        sup = _sup(tmp_path, _cache(parked=2, slots=2, held=5 * GIB))
        sup.note_guest_call(PEER, 125000, False)
        assert await sup.guest_checkpoint(PEER) is False
        assert sup._guest_entry_holder() is None

    @pytest.mark.asyncio
    async def test_a_reservation_the_call_never_used_goes_with_an_eviction(self, tmp_path):
        sup = _sup(tmp_path, _cache(parked=0, slots=4, evictions=7))
        sup._cache_evictions_polled = 7
        sup.note_guest_call(PEER, 125000, False)
        sup.note_guest_call(OTHER, 1000, False)
        assert await sup.guest_checkpoint(PEER) is True
        sup._get = AsyncMock(return_value={"conversation_cache": _cache(parked=0, slots=4, evictions=8)})
        assert await sup.guest_checkpoint(OTHER) is True

    @pytest.mark.asyncio
    async def test_a_reservation_the_call_never_used_goes_with_a_new_launch(self, tmp_path):
        sup = _sup(tmp_path, _cache(parked=0, slots=4))
        sup.note_guest_call(PEER, 125000, False)
        assert await sup.guest_checkpoint(PEER) is True
        sup.prime_restore_scan()
        assert sup._guest_entry_holder() is None
