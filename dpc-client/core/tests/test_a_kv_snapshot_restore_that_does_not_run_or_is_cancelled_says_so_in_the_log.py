"""A KV snapshot restore never ends without an outcome line in the log (ADR-044, amendment of
2026-11-13, point 5; card THE-OUTCOME-OF-A-KV-SNAPSHOT-RESTORE-IS-NOT-ALWAYS-LOGGED).

Before this, five ends were silent: another call already in the engine, a folder that is off or
missing, an owner conversation with no entry in the index, the supervisor's "no folder" skip, and
a cancellation (`asyncio.CancelledError` is a BaseException, so `except Exception` never saw it).
A drain refuses every call's restore too, so it is said once per launch, not once per call, and
only when a restore was possible. Behaviour is unchanged: the call still runs cold, a cancel is
still re-raised, nothing is deleted that was not deleted before.

Fakes only: the provider doubles of the restore tests, and Strata's launcher mock for the
supervisor.
"""

import asyncio

import pytest

from dpc_client_core.managers import kv_snapshots as kv
from dpc_client_core.managers.llama_server_supervisor import card_held_for, serving_a_peer

from tests.test_a_kv_snapshot_is_restored_only_into_an_empty_engine_for_its_own_conversation import (
    SYSTEM, TURN_1, TURN_2, _files, _saved, _turn, clean,  # noqa: F401
)
from tests.test_a_strata_kv_snapshot_is_restored_after_an_unload_by_loading_the_model_first import (
    started_with_a_snapshot,
)
from tests.test_an_unplugged_kv_snapshots_disk_starts_strata_with_saving_off import (  # noqa: F401
    ALIAS,
    card,
    kv_state,
)
from tests.test_the_last_engine_call_names_owner_peer_or_gateway_from_the_calls_own_context import ALIAS as PROVIDER_ALIAS

PROVIDER = "dpc_client_core.providers.llamacpp_server_provider"
SUPERVISOR = "dpc_client_core.managers.llama_server_supervisor"


def _lines(caplog, *words):
    return [r.getMessage() for r in caplog.records if all(w in r.getMessage() for w in words)]


async def _ensure_as_owner(p, conversation_id="conv-a"):
    async with card_held_for(PROVIDER_ALIAS, "accepted_request", agent_id="agent_7", conversation_id=conversation_id):
        await p._ensure_for(p._anthropic_to_openai_messages(SYSTEM, TURN_2, provider=p))


@pytest.mark.asyncio
async def test_a_call_already_in_the_engine_says_why_no_restore_ran(tmp_path, caplog):
    p, sup, _, folder, final = await _saved(tmp_path)
    sup._in_flight = 1
    with caplog.at_level("INFO", logger=PROVIDER):
        await _ensure_as_owner(p)
    assert _lines(caplog, "not restored") == [
        f"{PROVIDER_ALIAS}: KV snapshot not restored: another call is already in the engine "
        "(agent agent_7 conversation conv-a)"
    ]
    assert sup.restores == [] and _files(folder) == [final]


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["missing disk", "no folder"])
async def test_a_folder_that_is_off_or_missing_says_so(tmp_path, caplog, how):
    p, sup, _, folder, final = await _saved(tmp_path)
    if how == "missing disk":
        kv.suspend(PROVIDER_ALIAS, kv.FOLDER_MISSING)
        words = "the KV snapshots folder is off this launch"
    else:
        kv.unregister_folder(PROVIDER_ALIAS)
        words = "this alias has no KV snapshots folder to restore from"
    with caplog.at_level("INFO", logger=PROVIDER):
        await _ensure_as_owner(p)
    assert _lines(caplog, "not restored") == [
        f"{PROVIDER_ALIAS}: KV snapshot not restored: {words} (agent agent_7 conversation conv-a)"
    ]
    assert sup.restores == [] and _files(folder) == [final]


@pytest.mark.asyncio
async def test_an_owner_conversation_without_a_snapshot_says_so_and_a_peer_does_not(tmp_path, caplog):
    p, sup, _, folder, final = await _saved(tmp_path)
    with caplog.at_level("INFO", logger=PROVIDER):
        await _ensure_as_owner(p, conversation_id="conv-never-saved")
    assert _lines(caplog, "not restored") == [
        f"{PROVIDER_ALIAS}: KV snapshot not restored: no snapshot of this conversation in the index "
        "(agent agent_7 conversation conv-never-saved)"
    ]
    # A peer fills the next fresh engine: the "filled by" line speaks, the miss line does not.
    caplog.clear()
    sup._kv_engine_fresh = True
    with caplog.at_level("INFO", logger=PROVIDER):
        with serving_a_peer("dpc-node-x"):
            await p.generate_with_tools(TURN_2, [], system=SYSTEM)
    assert _lines(caplog, "KV snapshot not restored") == []
    assert len(_lines(caplog, "the empty engine was filled by a peer call")) == 1
    assert sup.restores == [] and _files(folder) == [final]


@pytest.mark.asyncio
async def test_a_drain_is_said_once_per_launch_and_only_for_a_restore_that_was_possible(tmp_path, caplog):
    p, sup, _, folder, final = await _saved(tmp_path)
    sup._draining = True
    with caplog.at_level("INFO", logger=PROVIDER):
        with serving_a_peer("dpc-node-x"):  # no restore was possible for a peer
            await p._ensure_for(p._anthropic_to_openai_messages(SYSTEM, TURN_2, provider=p))
        assert _lines(caplog, "draining") == []
        await _ensure_as_owner(p)
        await _ensure_as_owner(p)
        await _ensure_as_owner(p)
    assert _lines(caplog, "not restored") == [
        f"{PROVIDER_ALIAS}: KV snapshot not restored: the engine is draining (agent agent_7 conversation conv-a)"
    ]
    # Nothing else changed: the mark stays for the launch's own end, the file stays.
    assert sup.restores == [] and sup._kv_engine_fresh is True and _files(folder) == [final]


@pytest.mark.asyncio
async def test_a_drain_with_a_filled_engine_says_nothing(tmp_path, caplog):
    p, sup, _, folder, final = await _saved(tmp_path)
    sup._draining = True
    sup._kv_engine_fresh = False
    with caplog.at_level("INFO", logger=PROVIDER):
        await _ensure_as_owner(p)
    assert _lines(caplog, "not restored") == []


@pytest.mark.asyncio
async def test_a_cancelled_restore_is_logged_and_reraised(tmp_path, caplog):
    p, sup, completions, folder, final = await _saved(tmp_path, outcome=asyncio.CancelledError())
    sent = len(completions.bodies)
    with caplog.at_level("INFO", logger=PROVIDER):
        with pytest.raises(asyncio.CancelledError):
            await _turn(p, TURN_2)
    assert _lines(caplog, "restore cancelled") == [
        f"{PROVIDER_ALIAS}: KV snapshot restore cancelled (agent agent_7 conversation conv-a); "
        "the call is cancelled with it"
    ]
    assert len(completions.bodies) == sent  # the call did not run on
    assert sup._in_flight == 0 and _files(folder) == [final]


@pytest.mark.asyncio
async def test_a_cancel_in_the_engine_request_is_logged_by_the_supervisor_and_reraised(tmp_path, monkeypatch, caplog):
    sup, launcher, folder, entry = await started_with_a_snapshot(tmp_path, monkeypatch, state="idle")

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    sup._slot_action = cancelled
    with caplog.at_level("INFO", logger=SUPERVISOR):
        with pytest.raises(asyncio.CancelledError) as raised:
            await sup._restore_kv_snapshot(entry)
    assert _lines(caplog, "cancelled") == [
        f"strata[{ALIAS}]: KV snapshot restore of conversation c1 (agent a1) cancelled; the file is kept"
    ]
    # The mark tells the provider this one is already said.
    assert raised.value.kv_restore_logged is True
    assert [e["file"] for e in kv.read_index(folder)["entries"]] == [entry["file"]]


@pytest.mark.asyncio
async def test_the_supervisors_no_folder_skip_says_so(tmp_path, monkeypatch, caplog):
    sup, launcher, folder, entry = await started_with_a_snapshot(tmp_path, monkeypatch, state="idle")
    with caplog.at_level("INFO", logger=SUPERVISOR):
        outcome = await sup._restore_kv_snapshot(dict(entry, file=None))
    assert outcome["outcome"] == kv.SKIPPED and outcome["reason"] == "no KV snapshots folder"
    assert _lines(caplog, "not restored") == [
        f"strata[{ALIAS}]: KV snapshot of conversation c1 (agent a1) not restored: no KV snapshots folder"
    ]
    assert launcher.requests == []
