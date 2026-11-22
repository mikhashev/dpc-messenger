"""ADR-052, Confirmation test 6 (slice S1): with no lease registered, the card behaves as before.

`_CARD_CLAIMS` keeps its shape and `_live_claims` stays supervisor-only (section 2.3). With no lease
granted, `_live_holders() == _live_claims()`, `card_holders()` rows are the old rows plus `kind`, and
`card_queue()` rows are the old rows with the old keys. Lease rows come after the supervisor rows,
with the keys of L7, and lease waiters after the card queue's own rows with `until_cancelled`;
`card_holder()` stays supervisor-only, so the panel's Stop engine button never points at a lease
before S2. The supervisor module never imports the lease module: the lease module installs hooks.

Real supervisors through their real `ensure_running`, with the fakes of
`test_one_local_engine_holds_the_card.py`.
"""

import asyncio
import math
import time
from pathlib import Path

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import card_holders, card_queue
from dpc_client_core.providers.llamacpp_server_provider import card_holder

from tests._card_lease_helpers import BOUND, fast, hold_open, music, nothing_left, until, whisper  # noqa: F401
from tests.test_one_local_engine_holds_the_card import _llama, card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast")

LEASE_ROW_KEYS = {"alias", "engine", "state", "port", "kind", "display", "busy", "uses", "vram_mib", "since",
                  "yield_cost", "eta_s", "beside"}
LEASE_QUEUE_KEYS = {"alias", "kind", "what", "queued_at", "give_up_at", "until_cancelled", "wait"}


def _old_card_holders():
    """`card_holders()` as it was before ADR-052."""
    return [{"alias": sup.alias, "engine": sup.ENGINE, "state": state, "port": sup.port}
            for sup, state in base_mod._live_claims()]


def _old_card_queue():
    """`card_queue()` as it was before ADR-052."""
    now, clock = time.time(), base_mod._swap_clock()
    return [
        {
            "alias": place.alias,
            "agent_id": request.hold.agent_id if request.hold is not None else None,
            "conversation_id": request.hold.conversation_id if request.hold is not None else None,
            "queued_at": request.queued_at,
            "give_up_at": None if math.isinf(request.give_up_at) else now + request.give_up_at - clock,
        }
        for place in base_mod._CARD_QUEUE
        for request in place.requests
    ]


class TestNothingChangesWithoutALease:
    @pytest.mark.asyncio
    async def test_the_holders_are_the_supervisors_and_their_rows_gain_only_kind(self):
        llama = _llama()
        await llama.ensure_running()
        llama_2 = _llama("bonsai", gguf_path="D:/models/bonsai.gguf")
        await llama_2.ensure_running()

        assert base_mod._live_holders() == base_mod._live_claims()
        assert [h for h, _state in base_mod._live_holders()] == [llama, llama_2]
        assert card_holders() == [dict(row, kind="inference") for row in _old_card_holders()]
        assert card_holder() == dict(_old_card_holders()[0], kind="inference")

    @pytest.mark.asyncio
    async def test_a_lease_registered_but_not_granted_changes_nothing(self):
        whisper(), music()
        llama = _llama()
        await llama.ensure_running()

        assert base_mod._live_holders() == base_mod._live_claims()
        assert card_holders() == [dict(row, kind="inference") for row in _old_card_holders()]
        assert base_mod._LEASE_WAITERS() == [] and base_mod._LEASE_QUEUE_ROWS() == []

    @pytest.mark.asyncio
    async def test_the_card_queue_rows_keep_their_keys(self):
        place = base_mod._CardPlace("flash")
        place.requests.append(base_mod._QueuedRequest(place))
        base_mod._CARD_QUEUE.append(place)
        whisper()

        rows = card_queue()
        assert rows == _old_card_queue()
        [row] = rows
        assert set(row) == {"alias", "agent_id", "conversation_id", "queued_at", "give_up_at"}
        base_mod._CARD_QUEUE.clear()

    def test_the_default_hooks_see_no_lease(self, monkeypatch):
        monkeypatch.setattr(base_mod, "_LEASE_HOLDERS", lambda: [])
        monkeypatch.setattr(base_mod, "_LEASE_QUEUE_ROWS", lambda: [])
        assert base_mod._live_holders() == base_mod._live_claims() == []
        assert card_holders() == [] and card_queue() == []

    def test_the_supervisor_module_never_imports_the_lease_module(self):
        source = Path(base_mod.__file__).read_text(encoding="utf-8")
        assert "import card_lease" not in source and "from .card_lease" not in source
        assert "from . import card_lease" not in source
        assert base_mod._LEASE_HOLDERS is card_lease._lease_holders, "importing the lease module installs it"


class TestLeaseRowsComeAfterTheSupervisors:
    @pytest.mark.asyncio
    async def test_a_granted_lease_is_listed_after_the_supervisor_and_card_holder_skips_it(self):
        w = whisper(eta=12)
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)
        assert card_holder() is None, "a lease alone is no local engine to stop by hand before S2"
        [row] = card_holders()
        assert set(row) == LEASE_ROW_KEYS
        assert row["alias"] == "whisper:turbo" and row["kind"] == "transcription" and row["port"] is None
        assert row["state"] == "running" and row["busy"] is True and row["eta_s"] == 12
        assert [u["what"] for u in row["uses"]] == ["dictation"] and row["beside"] is None
        assert row["yield_cost"] == "cheap" and row["vram_mib"] is None

        # A supervisor claims beside it: in S1 supervisors do not see leases yet (S2), which is how
        # both rows can be shown here.
        llama = _llama()
        await llama.ensure_running()
        assert [(r["alias"], r["kind"]) for r in card_holders()] == [("qwen27", "inference"),
                                                                     ("whisper:turbo", "transcription")]
        assert card_holder()["alias"] == "qwen27"
        assert [h for h, _state in base_mod._live_claims()] == [llama], "_live_claims stays supervisor-only"
        assert llama in base_mod._CARD_CLAIMS and len(base_mod._CARD_CLAIMS) == 1
        release.set()
        await asyncio.wait_for(dictation, BOUND)

    @pytest.mark.asyncio
    async def test_a_lease_waiter_is_listed_after_the_card_queue_with_until_cancelled(self):
        place = base_mod._CardPlace("flash")
        place.requests.append(base_mod._QueuedRequest(place))
        base_mod._CARD_QUEUE.append(place)
        m = music()
        job = asyncio.create_task(m.lease.acquire("music job J", wait_s=None))
        await until(lambda: len(card_queue()) == 2)

        old, lease_row = card_queue()
        assert set(old) == {"alias", "agent_id", "conversation_id", "queued_at", "give_up_at"}
        assert set(lease_row) == LEASE_QUEUE_KEYS
        assert lease_row["alias"] == "music:yue2" and lease_row["what"] == "music job J"
        assert lease_row["give_up_at"] is None and lease_row["until_cancelled"] is True
        assert abs(lease_row["queued_at"] - time.time()) < 30
        assert lease_row["wait"]["where"] == "card_queue"

        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        base_mod._CARD_QUEUE.clear()
        bounded = asyncio.create_task(m.lease.acquire("music job K", wait_s=60))
        await asyncio.wait_for(bounded, BOUND)
        await nothing_left()

    @pytest.mark.asyncio
    async def test_a_bounded_lease_waiter_reports_when_it_gives_up(self):
        w, m = whisper(), music()
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)
        job = asyncio.create_task(m.lease.acquire("music job J", wait_s=60))
        await until(lambda: len(card_queue()) == 1)
        [row] = card_queue()
        assert row["until_cancelled"] is False
        assert row["give_up_at"] - row["queued_at"] == pytest.approx(60, abs=5)
        release.set()
        await asyncio.wait_for(asyncio.gather(dictation, job), BOUND)
        await nothing_left()
