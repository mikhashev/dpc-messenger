"""What a drain does not save, and says so (ADR-044, amendment 2026-11-13, points 2, 3, 6, 7):
a call still in flight after a timed-out drain, the backend's own stop (slice 1b), a last call
that was a peer's or a gateway's (however long ago it ended), a caller inside its window, a
conversation cleared since its last call, a call that named none. A folder whose parent is
missing turns saving off for the launch and the child starts without `--slot-save-path`; a
folder that cannot be made on an existing parent refuses the start in words.
"""

import logging
import time

import pytest

from dpc_client_core import process_registry as procreg
from dpc_client_core.managers import kv_snapshots
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import LlamaServerError, card_holders, stop_by_hand

from tests.test_a_swap_saves_the_owners_live_conversation_before_the_stop import (  # noqa: F401
    ALIAS,
    FakeEngine,
    card,
    index_entries,
    kv_state,
    note_call,
    saving_llama,
    saving_pair,
    settings,
    swap_on,
)

PEER = "dpc-node-" + "b" * 32


def _skipped(outcome, fake, words):
    assert outcome["outcome"] == base_mod._KV_SKIPPED and words in outcome["reason"]
    assert fake.calls == []


@pytest.mark.asyncio
async def test_a_drain_that_timed_out_with_a_call_in_flight_saves_nothing(tmp_path):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    note_call()
    slot = holder.call_slot()
    await slot.__aenter__()

    outcome = await holder.drain(timeout=0.05)
    await slot.__aexit__(None, None, None)

    _skipped(outcome, fake, "1 call(s) still in flight")
    assert holder._proc is None and index_entries(folder) == []


@pytest.mark.asyncio
async def test_the_backends_own_stop_saves_nothing_in_slice_one(tmp_path, caplog):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    note_call()
    procreg.get_registry().begin_shutdown()

    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        outcome = await holder.drain(timeout=120.0)  # a provider's close()

    _skipped(outcome, fake, "the backend is stopping")
    assert fake.slot_reads == 0 and holder._proc is None
    assert any("not saved: the backend is stopping" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("ended_ago", [1.0, 10_000.0])
async def test_a_slot_whose_last_call_was_a_peers_writes_nothing(tmp_path, ended_ago):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    note_call(kind="peer", agent=None, conversation=None, peer=PEER, at=time.time() - ended_ago)

    outcome = await stop_by_hand(holder)

    _skipped(outcome, fake, f"the last call was a peer's (node {PEER})")
    assert holder._proc is None and index_entries(folder) == []


@pytest.mark.asyncio
async def test_a_slot_whose_last_call_was_a_gateway_clients_writes_nothing(tmp_path):
    holder, _new, fake, _folder = await saving_pair(tmp_path)
    note_call(kind="gateway", agent=None, conversation=None)

    _skipped(await stop_by_hand(holder), fake, "the last call was a gateway client's")


@pytest.mark.asyncio
async def test_an_owner_call_after_the_peers_is_saved(tmp_path):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    note_call(kind="peer", agent=None, conversation=None, peer=PEER, at=time.time() - 5)
    note_call()

    outcome = await stop_by_hand(holder)

    assert outcome["outcome"] == kv_snapshots.SAVED and len(index_entries(folder)) == 1


@pytest.mark.asyncio
async def test_a_caller_inside_its_window_keeps_the_slot_unsaved(tmp_path, settings):
    holder, _new, fake, _folder = await saving_pair(tmp_path)
    note_call()
    base_mod.note_caller_call(ALIAS, PEER)

    _skipped(await stop_by_hand(holder), fake, f"caller(s) {PEER} still inside their window")


@pytest.mark.asyncio
async def test_a_conversation_cleared_since_its_last_call_is_not_saved_again(tmp_path):
    holder, _new, fake, _folder = await saving_pair(tmp_path)
    note_call()
    kv_snapshots.forget_conversation("c1")

    _skipped(await stop_by_hand(holder), fake, "the conversation was cleared")


@pytest.mark.asyncio
async def test_an_owner_chats_single_prompt_call_is_not_saved(tmp_path):
    """The owner's chat sends one rebuilt prompt: its one-message prefix never matches the next
    turn, so the file would be written only to be deleted at the lookup."""
    holder, _new, fake, _folder = await saving_pair(tmp_path)
    note_call(kind=kv_snapshots.OWNER_CHAT_KIND, agent=None, conversation="chat-1", prefix=("cd" * 32, 1))

    _skipped(await stop_by_hand(holder), fake, "single-prompt call: no stable prefix")


@pytest.mark.asyncio
async def test_an_agents_one_message_turn_is_still_saved(tmp_path):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    note_call(prefix=("cd" * 32, 1))

    assert (await stop_by_hand(holder))["outcome"] == kv_snapshots.SAVED


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [False, True])
async def test_a_last_call_that_named_no_conversation_is_not_saved(tmp_path, recorded):
    holder, _new, fake, _folder = await saving_pair(tmp_path)
    if recorded:
        note_call(kind=None, agent=None, conversation=None)  # a background call

    words = "the last call named no conversation" if recorded else "no call is recorded"
    _skipped(await stop_by_hand(holder), fake, words)


@pytest.mark.asyncio
async def test_with_no_folder_the_drain_is_todays(tmp_path):
    from tests.test_one_local_engine_holds_the_card import _llama

    holder = _llama(ALIAS)
    await holder.ensure_running()
    fake = FakeEngine(holder, tmp_path)
    note_call()

    assert await stop_by_hand(holder) is None
    assert fake.slot_reads == 0 and fake.calls == [] and holder._proc is None


def _capture_commands(sup):
    commands = []
    real = sup._spawn

    async def spawn(cmd, env):
        commands.append(cmd)
        return await real(cmd, env)

    sup._spawn = spawn
    return commands


@pytest.mark.asyncio
async def test_a_missing_disk_starts_the_child_with_saving_off(tmp_path, caplog):
    holder, folder = saving_llama(tmp_path, folder=tmp_path / "unplugged" / "kv")
    commands = _capture_commands(holder)

    with caplog.at_level(logging.WARNING, logger=base_mod.logger.name):
        await holder.ensure_running()

    assert holder._proc is not None and "--slot-save-path" not in commands[0]
    assert not folder.parent.exists()
    assert kv_snapshots.off_reason(ALIAS) == kv_snapshots.FOLDER_MISSING
    assert any("KV snapshots off this launch: folder missing" in r.getMessage() for r in caplog.records)
    fake = FakeEngine(holder, folder)
    note_call()
    assert await stop_by_hand(holder) is None  # off: null, the notice unchanged
    assert fake.calls == []


@pytest.mark.asyncio
async def test_the_disk_back_in_place_turns_saving_on_at_the_next_launch(tmp_path):
    holder, folder = saving_llama(tmp_path, folder=tmp_path / "usb" / "kv")
    commands = _capture_commands(holder)
    await holder.ensure_running()
    await stop_by_hand(holder)

    folder.parent.mkdir()
    await holder.ensure_running()

    assert "--slot-save-path" not in commands[0]
    assert commands[1][commands[1].index("--slot-save-path") + 1] == str(folder)
    assert folder.is_dir() and kv_snapshots.off_reason(ALIAS) is None


@pytest.mark.asyncio
async def test_a_folder_that_cannot_be_made_on_an_existing_parent_refuses_the_start_in_words(tmp_path):
    in_the_way = tmp_path / "kv"
    in_the_way.write_text("a file, not a folder")
    holder, _folder = saving_llama(tmp_path, folder=in_the_way)

    with pytest.raises(LlamaServerError, match="cannot create the KV snapshots folder slot_save_path"):
        await holder.ensure_running()

    assert holder._proc is None and card_holders() == []


@pytest.mark.asyncio
async def test_a_call_with_no_prefix_recorded_is_not_saved(tmp_path):
    holder, _new, fake, _folder = await saving_pair(tmp_path)
    note_call(prefix=None)

    _skipped(await stop_by_hand(holder), fake, "no prefix recorded")


@pytest.mark.asyncio
async def test_a_history_cleared_while_the_engine_writes_leaves_no_file(tmp_path):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    note_call()
    real = fake.action

    async def clears_meanwhile(slot, action, filename, timeout):
        answer = await real(slot, action, filename, timeout)
        kv_snapshots.forget_conversation("c1")  # a reset lands while the engine writes
        return answer

    holder._slot_action = clears_meanwhile

    outcome = await stop_by_hand(holder)

    assert outcome["outcome"] == base_mod._KV_SKIPPED and "cleared during the save" in outcome["reason"]
    assert index_entries(folder) == [] and list(folder.glob("dpc-kv-*")) == []
    assert holder._proc is None


@pytest.mark.asyncio
async def test_a_conversation_no_monitor_can_clear_is_not_saved(tmp_path, monkeypatch):
    """A task turn's conversation has no monitor, so no reset could ever delete its snapshot."""
    holder, _new, fake, folder = await saving_pair(tmp_path)
    note_call(conversation="task-7")
    monkeypatch.setattr(kv_snapshots, "is_clearable", lambda conversation_id: conversation_id != "task-7",
                        raising=False)

    _skipped(await stop_by_hand(holder), fake, "no conversation monitor can clear it (a task turn)")
    assert index_entries(folder) == []


@pytest.mark.asyncio
async def test_an_owner_chat_is_not_saved_in_slice_1_whatever_its_prefix(tmp_path):
    """Slice 1 saves agents' conversations; the owner's own chat waits for a later slice, even
    when its prefix has more than one message."""
    holder, _new, fake, _folder = await saving_pair(tmp_path)
    note_call(kind=kv_snapshots.OWNER_CHAT_KIND, agent=None, conversation="chat-1", prefix=("cd" * 32, 3))

    _skipped(await stop_by_hand(holder), fake, "the owner's own chat is not saved in slice 1")
