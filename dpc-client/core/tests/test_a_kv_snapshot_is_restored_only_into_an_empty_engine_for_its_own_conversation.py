"""A KV snapshot is restored only into an empty engine, and only for its own conversation.

ADR-044, amendment of 2026-11-13 (KV snapshots), point 5, as the provider's `_ensure` holds it:
the first call after a start (`_kv_engine_fresh`) restores when it is the owner's own call of the
saved conversation, no call is in flight and the supervisor is not draining; the fresh mark is
cleared before the await, so one call restores; any other first call clears it and leaves the file.
A prefix that moved deletes the file and the call runs cold. The supervisor's
`_restore_kv_snapshot` settles a refused or missing file; on llama-server "no room" in an empty
engine is read as a bad file and deleted here, on Strata it is kept. The restore runs inside a call
slot, before the call's own request.

Real `kv_snapshots` folders under `tmp_path`; a fake supervisor and client, no engine.
"""

import asyncio
import time

import pytest

from dpc_client_core.managers import kv_snapshots as kv
from dpc_client_core.managers.llama_server_supervisor import card_held_for, serving_a_peer
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS

from tests.test_the_last_engine_call_names_owner_peer_or_gateway_from_the_calls_own_context import (
    ALIAS, FakeSupervisor, llama, wire,
)

SYSTEM = "you are a helper"
TURN_1 = [{"role": "user", "content": "list the files"}]
TURN_2 = TURN_1 + [{"role": "assistant", "content": "a.txt b.txt"}, {"role": "user", "content": "open a.txt"}]


class RestoringSupervisor(FakeSupervisor):
    """`_restore_kv_snapshot` in P2's shape: it settles refused and missing files itself."""

    def __init__(self, folder, outcome=kv.RESTORED, engine="llama-server"):
        super().__init__()
        self.folder = folder
        self.outcome = outcome
        self.ENGINE = engine
        self.gate = None

    async def _restore_kv_snapshot(self, entry):
        self.in_flight_during_restore = self._in_flight
        self.restores.append(dict(entry))
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if self.outcome in (kv.REFUSED, kv.MISSING):
            kv.remove_entry(self.folder, entry["file"], reason=self.outcome)
        return {"outcome": self.outcome, "bytes": entry.get("bytes"), "ms": 5, "reason": self.outcome}


@pytest.fixture(autouse=True)
def clean():
    _ACTIVE_SUPERVISORS.clear()
    kv.reset_state()
    yield
    _ACTIVE_SUPERVISORS.clear()
    kv.reset_state()


async def _turn(p, messages, agent_id="agent_7", conversation_id="conv-a"):
    async with card_held_for(ALIAS, "accepted_request", agent_id=agent_id, conversation_id=conversation_id):
        return await p.generate_with_tools(messages, [], system=SYSTEM)


async def _saved(tmp_path, outcome=kv.RESTORED, engine="llama-server"):
    """A provider whose first turn was recorded and saved as P2 saves it, then a fresh engine."""
    folder = tmp_path / "kv"
    folder.mkdir()
    kv.register_folder(ALIAS, folder, cap_bytes=kv.GIB)
    p = llama()
    sup, completions = wire(p, sup=RestoringSupervisor(folder, outcome, engine))
    await _turn(p, TURN_1)
    staging, final = kv.new_snapshot_names(folder)
    (folder / final).write_bytes(b"\0" * 1024)
    kv.release_staging(folder, staging)
    entry = kv.new_entry(file=final, alias=ALIAS, engine="llamacpp_server", identity={},
                         record=kv.engine_call_of(ALIAS), n_saved=10, nbytes=1024, save_ms=3,
                         now=time.time())
    assert kv.add_entry(folder, entry, cap_bytes=kv.GIB)
    sup._kv_engine_fresh = True
    return p, sup, completions, folder, final


def _files(folder):
    return [e["file"] for e in kv.read_index(folder)["entries"]]


@pytest.mark.asyncio
async def test_the_owners_own_conversation_is_restored_once_before_its_request(tmp_path):
    p, sup, completions, folder, final = await _saved(tmp_path)
    order = []
    completions.on_create = lambda params: order.append("request")
    real = sup._restore_kv_snapshot

    async def restore(entry):
        order.append("restore")
        return await real(entry)

    sup._restore_kv_snapshot = restore
    await _turn(p, TURN_2)
    assert order == ["restore", "request"]
    assert [r["file"] for r in sup.restores] == [final]
    assert sup.in_flight_during_restore == 1
    assert sup._in_flight == 0
    assert sup._kv_engine_fresh is False
    assert _files(folder) == [final]  # kept after a restore
    await _turn(p, TURN_2 + [{"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}])
    assert len(sup.restores) == 1


@pytest.mark.asyncio
async def test_an_engine_that_is_not_empty_restores_nothing(tmp_path):
    p, sup, _, folder, final = await _saved(tmp_path)
    sup._kv_engine_fresh = False
    await _turn(p, TURN_2)
    assert sup.restores == []
    assert _files(folder) == [final]


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["other conversation", "other agent", "peer", "background"])
async def test_a_first_call_that_is_not_the_saved_conversation_restores_and_deletes_nothing(tmp_path, first):
    p, sup, _, folder, final = await _saved(tmp_path)
    if first == "other conversation":
        await _turn(p, TURN_2, conversation_id="conv-b")
    elif first == "other agent":
        await _turn(p, TURN_2, agent_id="agent_8")
    elif first == "peer":
        with serving_a_peer("dpc-node-x"):
            await p.generate_with_tools(TURN_2, [], system=SYSTEM)
    else:
        await p.generate_with_tools(TURN_2, [], system=SYSTEM)
    assert sup.restores == []
    assert _files(folder) == [final]
    # The engine is no longer empty: the saved conversation coming next is not restored.
    assert sup._kv_engine_fresh is False
    await _turn(p, TURN_2)
    assert sup.restores == []


@pytest.mark.asyncio
@pytest.mark.parametrize("first,words", [
    ("peer", "a peer call"),
    ("gateway", "a gateway call"),
    ("background", "a background call with no conversation"),
    ("owner chat without a conversation", "an owner call with no conversation"),
])
async def test_a_non_owner_call_that_fills_the_empty_engine_says_so_once(tmp_path, caplog, first, words):
    from dpc_client_core.managers.llama_server_supervisor import carrying_card_hold
    from types import SimpleNamespace

    p, sup, _, folder, final = await _saved(tmp_path)
    with caplog.at_level("INFO", logger="dpc_client_core.providers.llamacpp_server_provider"):
        if first == "peer":
            with serving_a_peer("dpc-node-x"):
                await p.generate_response("hello")
        elif first == "gateway":
            hold = SimpleNamespace(alias=ALIAS, live=True, agent_id=None, conversation_id=None,
                                   borrow=None, caller="dpc-node-owner")
            with carrying_card_hold(hold):
                await p.generate_response("hello")
        elif first == "background":
            await p.generate_response("hello")
        else:
            async with card_held_for(ALIAS, "accepted_request"):
                await p.generate_response("hello")
        await p.generate_response("a second call says nothing")
    lines = [r.getMessage() for r in caplog.records if "the empty engine was filled" in r.getMessage()]
    assert lines == [f"{ALIAS}: the empty engine was filled by {words}; no restore until the engine is empty again"]
    assert _files(folder) == [final]


@pytest.mark.asyncio
async def test_the_owners_own_first_call_logs_no_filled_line(tmp_path, caplog):
    p, sup, _, folder, final = await _saved(tmp_path)
    with caplog.at_level("INFO", logger="dpc_client_core.providers.llamacpp_server_provider"):
        await _turn(p, TURN_2)
    assert len(sup.restores) == 1
    assert not any("the empty engine was filled" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_call_arriving_while_the_supervisor_drains_neither_records_nor_restores(tmp_path):
    p, sup, _, folder, final = await _saved(tmp_path)
    before = kv.engine_call_of(ALIAS)
    sup._draining = True
    async with card_held_for(ALIAS, "accepted_request", agent_id="agent_7", conversation_id="conv-a"):
        await p._ensure_for(p._anthropic_to_openai_messages(SYSTEM, TURN_2, provider=p))
    assert sup.restores == []
    assert kv.engine_call_of(ALIAS)["seq"] == before["seq"]
    assert _files(folder) == [final]


@pytest.mark.asyncio
async def test_a_call_in_flight_means_the_engine_is_not_empty(tmp_path):
    p, sup, _, folder, final = await _saved(tmp_path)
    sup._in_flight = 1
    async with card_held_for(ALIAS, "accepted_request", agent_id="agent_7", conversation_id="conv-a"):
        await p._ensure_for(p._anthropic_to_openai_messages(SYSTEM, TURN_2, provider=p))
    assert sup.restores == []
    assert sup._kv_engine_fresh is False
    assert _files(folder) == [final]


@pytest.mark.asyncio
@pytest.mark.parametrize("meanwhile", ["a call entered", "a drain began"])
async def test_what_happens_during_the_lookup_is_asked_again_before_the_restore(tmp_path, monkeypatch, meanwhile):
    p, sup, completions, folder, final = await _saved(tmp_path)
    real_lookup = kv.lookup

    def lookup(*args, **kwargs):
        # The lookup is awaited (a thread): another call may enter its slot, or a drain begin.
        if meanwhile == "a call entered":
            sup._in_flight += 1
        else:
            sup._draining = True
        return real_lookup(*args, **kwargs)

    monkeypatch.setattr(kv, "lookup", lookup)
    async with card_held_for(ALIAS, "accepted_request", agent_id="agent_7", conversation_id="conv-a"):
        await p._ensure_for(p._anthropic_to_openai_messages(SYSTEM, TURN_2, provider=p))
    assert sup.restores == []
    assert _files(folder) == [final]


@pytest.mark.asyncio
async def test_a_prefix_that_moved_deletes_the_file_and_the_call_runs_cold(tmp_path):
    p, sup, completions, folder, final = await _saved(tmp_path)
    sent = len(completions.bodies)
    await _turn(p, [{"role": "user", "content": "a compacted history"}] + TURN_2[1:])
    assert sup.restores == []
    assert _files(folder) == []
    assert not (folder / final).exists()
    assert len(completions.bodies) == sent + 1
    assert kv.status_of(ALIAS)["last_restore"]["outcome"] == "prefix_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,engine,kept", [
    (kv.REFUSED, "llama-server", False),
    (kv.MISSING, "llama-server", False),
    (kv.NO_ROOM, "llama-server", False),
    (kv.NO_ROOM, "strata", True),
    (kv.TRANSPORT, "llama-server", True),
    (kv.NOT_LOADED, "strata", True),
])
async def test_what_happens_to_the_file_follows_the_answer(tmp_path, outcome, engine, kept):
    p, sup, completions, folder, final = await _saved(tmp_path, outcome=outcome, engine=engine)
    sent = len(completions.bodies)
    assert await _turn(p, TURN_2)
    assert len(sup.restores) == 1
    assert (_files(folder) == [final]) is kept
    assert (folder / final).exists() is kept
    assert len(completions.bodies) == sent + 1


@pytest.mark.asyncio
async def test_two_calls_of_the_saved_conversation_at_once_restore_once(tmp_path):
    p, sup, _, folder, final = await _saved(tmp_path)
    sup.gate = asyncio.Event()
    first = asyncio.ensure_future(_turn(p, TURN_2))
    while not sup.restores:
        await asyncio.sleep(0)
    second = asyncio.ensure_future(_turn(p, TURN_2))
    for _ in range(20):
        await asyncio.sleep(0)
    sup.gate.set()
    await asyncio.gather(first, second)
    assert len(sup.restores) == 1


@pytest.mark.asyncio
async def test_a_restore_that_raises_never_fails_the_call(tmp_path):
    p, sup, completions, folder, final = await _saved(tmp_path, outcome=RuntimeError("socket closed"))
    sent = len(completions.bodies)
    assert await _turn(p, TURN_2)
    assert len(completions.bodies) == sent + 1
    assert sup._in_flight == 0
    assert _files(folder) == [final]


@pytest.mark.asyncio
async def test_a_supervisor_without_the_restore_hook_runs_the_call(tmp_path):
    p, sup, completions, folder, final = await _saved(tmp_path)
    sup._restore_kv_snapshot = None
    sent = len(completions.bodies)
    assert await _turn(p, TURN_2)
    assert len(completions.bodies) == sent + 1
    assert _files(folder) == [final]


@pytest.mark.asyncio
async def test_no_registered_folder_or_a_missing_disk_restores_nothing(tmp_path):
    p, sup, _, folder, final = await _saved(tmp_path)
    kv.suspend(ALIAS, kv.FOLDER_MISSING)
    await _turn(p, TURN_2)
    assert sup.restores == []
    sup._kv_engine_fresh = True
    kv.launch_began(ALIAS)
    kv.unregister_folder(ALIAS)
    await _turn(p, TURN_2)
    assert sup.restores == []
    assert _files(folder) == [final]


@pytest.mark.asyncio
async def test_an_expired_snapshot_is_swept_at_the_lookup_and_never_restored(tmp_path):
    p, sup, _, folder, final = await _saved(tmp_path)
    index = kv.read_index(folder)
    index["entries"][0]["expires_at"] = 1.0
    kv.write_index(folder, index)
    await _turn(p, TURN_2)
    assert sup.restores == []
    assert not (folder / final).exists()
