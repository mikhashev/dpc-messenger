"""ADR-052 slice S3a (Ark #10-4, accepted 2026-11-23): a supervisor's stop figure in the wait estimate.

L10 said "Supervisors: None", so a lease behind any supervisor had an unknown wait and a peer was
refused even where the supervisor was already going. A supervisor that is going (stopping, draining,
or an idle one this very waiter swaps out) now counts the supervisor's own `_expected_stop_s()`: the
last stop it measured, else its grace default. What does not change (L13, "As built", Mike's option
(a)): a supervisor that is not going has no ETA. A busy one is waited for with no figure (L10), and
an idle one that nobody stops for this waiter is too: a peer never swaps a supervisor out and a
lease that does not `waits_for_engines` waits until it goes by itself, so a peer is still refused
at once behind a running supervisor, idle or busy (`test_a_peer_lease_waits_only_when_...`).

Fake supervisors, fake lease clients; no GPU.
"""

import asyncio
import time

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.card_lease import PEER_TRIP_S, LeaseRefusedForPeer
from dpc_client_core.managers.llama_server_supervisor import serving_a_peer

from tests._card_lease_helpers import (  # noqa: F401
    BOUND,
    fast,
    music,
    swap,
    until,
    waiting,
    whisper,
)

pytestmark = pytest.mark.usefixtures("fast")

STOP_S = 6.0


class Supervisor:
    """A supervisor as `_live_claims` and `_estimate` read it."""

    ENGINE, port = "strata", None

    def __init__(self, *, alias="flash", running=True, busy=False, draining=False, stop_s=STOP_S):
        self.alias = alias
        self._proc = type("Proc", (), {"returncode": None})() if running else None
        self.props = {"total_slots": 1} if running else None
        self._draining = draining
        self.busy = busy
        if stop_s is not None:
            self._expected_stop_s = lambda: stop_s

    def _busy_for_the_card(self, state, but_for=None, before=None):
        return self.busy


def _claim(sup, mark="claimed"):
    base_mod._CARD_CLAIMS[sup] = mark
    return sup


def _estimate_for(lease, *, peer=False):
    def make():
        return card_lease._LeaseWaiter(lease, "x", 60, False, 1.0)

    if peer:
        with serving_a_peer("node-guest"):
            waiter = make()
    else:
        waiter = make()
    return card_lease._estimate(waiter)


class TestAGoingSupervisorHasAFigure:
    def test_a_stopping_supervisor(self):
        _claim(Supervisor(), "stopping")
        estimate = _estimate_for(whisper().lease)
        assert estimate == {"wait_s": STOP_S, "known": True, "behind": ["strata 'flash'"], "where": "yielding"}

    def test_a_draining_supervisor(self):
        _claim(Supervisor(draining=True))
        estimate = _estimate_for(whisper().lease)
        assert estimate["known"] is True and estimate["wait_s"] == STOP_S and estimate["where"] == "yielding"

    def test_an_idle_one_a_music_lease_swaps_out_with_the_swap_on(self):
        swap(True)
        _claim(Supervisor())
        estimate = _estimate_for(music(waits_for_engines=True).lease)
        assert estimate["known"] is True and estimate["wait_s"] == STOP_S and estimate["where"] == "yielding"

    def test_the_figure_is_the_supervisors_own(self):
        _claim(Supervisor(stop_s=23.5), "stopping")
        assert _estimate_for(whisper().lease)["wait_s"] == 23.5

    def test_two_going_supervisors_go_together_the_longest_counts(self):
        _claim(Supervisor(alias="a", stop_s=4.0), "stopping")
        _claim(Supervisor(alias="b", stop_s=9.0), "stopping")
        assert _estimate_for(whisper().lease)["wait_s"] == 9.0

    def test_the_real_supervisors_figure_is_the_last_stop_else_the_grace(self):
        sup = base_mod.LlamaServerSupervisor.__new__(base_mod.LlamaServerSupervisor)
        sup._last_stop_s = None
        assert sup._expected_stop_s() == base_mod._STOP_CHILD_GRACE_S
        sup._last_stop_s = 3.25
        assert sup._expected_stop_s() == 3.25


class TestASupervisorThatIsNotGoingHasNone:
    def test_a_busy_one_even_for_a_music_lease_with_the_swap_on(self):
        swap(True)
        _claim(Supervisor(busy=True))
        estimate = _estimate_for(music(waits_for_engines=True).lease)
        assert estimate["known"] is False and estimate["wait_s"] is None
        assert estimate["where"] == "busy_holder"

    def test_an_idle_one_a_lease_that_does_not_swap_waits_for(self):
        _claim(Supervisor())
        estimate = _estimate_for(whisper().lease)
        assert estimate["known"] is False and estimate["where"] == "busy_holder"

    def test_an_idle_one_a_music_lease_meets_with_the_swap_off(self):
        swap(False)
        _claim(Supervisor())
        assert _estimate_for(music(waits_for_engines=True).lease)["known"] is False

    @pytest.mark.parametrize("busy", [False, True], ids=["idle", "busy"])
    def test_a_running_one_for_a_peer(self, busy):
        swap(True)
        _claim(Supervisor(busy=busy))
        assert _estimate_for(whisper("small").lease, peer=True)["known"] is False

    def test_a_starting_one(self):
        _claim(Supervisor(running=False))
        assert base_mod._live_claims()[0][1] == "starting"
        assert _estimate_for(whisper().lease)["known"] is False

    def test_a_going_one_that_keeps_no_figure(self):
        _claim(Supervisor(stop_s=None), "stopping")
        assert _estimate_for(whisper().lease)["known"] is False


class TestAPeerBehindAStoppingSupervisor:
    @pytest.mark.asyncio
    async def test_waits_when_the_stop_and_its_own_work_fit_and_is_granted_when_it_goes(self):
        sup = _claim(Supervisor(), "stopping")
        p = whisper("small")

        async def ask():
            with serving_a_peer("node-guest"):
                await p.lease.acquire("peer node-guest", wait_s=None, expected_s=1.0)

        task = asyncio.create_task(ask())
        await until(lambda: waiting() == ["whisper:small"])
        base_mod._CARD_CLAIMS.pop(sup)
        await asyncio.wait_for(task, BOUND)
        assert p.lease._granted

    @pytest.mark.asyncio
    async def test_is_refused_at_once_when_the_stop_and_its_own_work_do_not_fit(self):
        _claim(Supervisor(stop_s=PEER_TRIP_S - 10.0), "stopping")
        p = whisper("small")
        started = time.monotonic()
        with pytest.raises(LeaseRefusedForPeer) as refused:
            with serving_a_peer("node-guest"):
                await asyncio.wait_for(p.lease.acquire("peer node-guest", wait_s=None, expected_s=19.0), BOUND)
        assert time.monotonic() - started < 0.5
        assert refused.value.wait["known"] is True and refused.value.wait["wait_s"] == PEER_TRIP_S - 10.0
        assert "flash" not in str(refused.value) and "strata" not in str(refused.value)
        assert waiting() == []
