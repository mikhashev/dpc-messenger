"""A voice transcription joins only a voice record of its own conversation, and a parked one lands.

Follow-up 11a to e523b0ef and 8b2e1963, from Zcode #27 (10a), Johnny #25 (3) and Ark #24 (3).
Both claims of those commits failed on the live path, where a group is rendered from its
history.json (`get_conversation_history` with a `group_manager`), while their tests took the
in-memory path of a service double without one:

1. "VOICE_TRANSCRIPTION only from members": the gate ran only when the sender put a
   `group_id` in the payload. Without it the text went into the global registry under any
   file name, and the UI and the in-memory view joined it to any record of that name, in
   any conversation. File names are `voice_<UTC date_time>` to the second, with no node
   part. A transfer id taken first silenced the real transcription of that transfer.
2. "A member's early transcription is parked and waits for its voice record": the disk
   view never looked at the registry, the parked entry was released only by the next
   transcription, and an entry joined by name escaped the per-group bound.

Every check here goes through a real GroupManager whose store holds the group's
history.json, a real ConversationMonitor writing it, and `get_conversation_history`. A record
arriving by transfer comes through `_maybe_transcribe_voice_message`, the call
`_finalize_download` makes right after writing the record.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.message_handlers import voice_transcription_handler as vth
from dpc_client_core.message_handlers.voice_transcription_handler import VoiceTranscriptionHandler
from dpc_client_core.service import CoreService

ME = "dpc-node-" + "a" * 32
MEMBER = "dpc-node-" + "b" * 32
OTHER_MEMBER = "dpc-node-" + "c" * 32
OUTSIDER = "dpc-node-" + "d" * 32
VOICE = "voice_20261124_100000.webm"  # what both send paths mint: to the second, no node part


class _Node:
    """This node: a real group store on disk, its monitor, and the service around them."""

    def __init__(self, home: Path):
        self.groups = GroupManager(home / ".dpc", ME)
        self.group_id = self.groups.create_group("Voice room", "", [MEMBER, OTHER_MEMBER]).group_id
        self.group = ConversationMonitor(
            conversation_id=self.group_id,
            participants=[{"node_id": ME, "name": "Mike", "context": "local"}],
            llm_manager=None, display_name="Voice room",
        )
        core = CoreService.__new__(CoreService)
        core.group_manager = self.groups
        core.llm_manager = None
        core.conversation_monitors = {self.group_id: self.group}
        core._group_agent_context = {}
        core._voice_transcriptions = {}
        core._processed_message_ids = set()
        core.local_api = SimpleNamespace(broadcast_event=AsyncMock())
        core.p2p_manager = SimpleNamespace(
            node_id=ME, peers={MEMBER: object(), OTHER_MEMBER: object(), OUTSIDER: object()},
            send_message_to_peer=AsyncMock())
        # Transcription off here: the receive-path call returns after landing parked text.
        core.settings = SimpleNamespace(get_voice_transcription_enabled=lambda: False)
        self.core = core
        self.handler = VoiceTranscriptionHandler(core)
        core.voice_transcription_handler = self.handler
        self.now = [1000.0]
        self.handler._clock = lambda: self.now[0]

    def one_to_one(self, peer: str, transfer_id: str, filename: str = VOICE):
        monitor = SimpleNamespace(
            message_history=[{"id": f"p-{transfer_id}", "role": "assistant", "content": "",
                              "sender_node_id": peer, "attachments": [
                                  {"type": "voice", "filename": filename, "transfer_id": transfer_id}]}],
            message_buffer=[], full_conversation=[],
            get_message_history=lambda: monitor.message_history, get_token_usage=lambda: {},
            _tokens_after_last_response=0, _tokens_after_last_response_at=None,
            live_history_boundary=None,
        )
        self.core.conversation_monitors[peer] = monitor
        return monitor

    def write_record(self, author: str, message_id: str, filename: str = VOICE,
                     transfer_id: str = "our-transfer-from-the-author"):
        """A voice record lands in the group history and on disk, as the receive path writes it."""
        attachment = {"type": "voice", "filename": filename, "transfer_id": transfer_id}
        self.group.add_message("assistant", f"Received voice message: {filename}", [attachment],
                               sender_node_id=author, message_id=message_id)
        self.group.save_history()

    async def record_arrives(self, author: str, message_id: str, **kw):
        self.write_record(author, message_id, **kw)
        await CoreService._maybe_transcribe_voice_message(
            self.core, transfer_id=kw.get("transfer_id", "our-transfer-from-the-author"),
            node_id=author, file_path=Path("unused.webm"), voice_metadata={},
            is_sender=False, group_id=self.group_id)

    async def shown(self, conversation_id=None):
        page = await CoreService.get_conversation_history(self.core, conversation_id or self.group_id)
        assert page["status"] == "success", page
        return {m["message_id"]: (a.get("transcription") or {}).get("text")
                for m in page["messages"] for a in m.get("attachments") or ()}

    def events(self):
        return [c.args[1] for c in self.core.local_api.broadcast_event.await_args_list
                if c.args[0] == "voice_transcription_received"]


@pytest.fixture
def node(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return _Node(home)


def _said(text, transcriber=MEMBER, group_id="", **over):
    payload = {"transfer_id": "the-members-transfer", "filename": VOICE,
               "transcription_text": text, "provider": "local_whisper",
               "transcriber_node_id": transcriber, "confidence": 0.9, "language": "en"}
    if group_id:
        payload["group_id"] = group_id
    payload.update(over)
    return payload


# --- 1. bound to a conversation and a sender ----------------------------------------


@pytest.mark.asyncio
async def test_a_node_naming_no_group_reaches_no_group_record_and_nothing_is_shown(node):
    """Zcode #27: the gate is skipped by leaving `group_id` out. Executed falsifier."""
    node.write_record(MEMBER, "m-voice")

    await node.handler.handle(OUTSIDER, _said("text nobody said", transcriber=OUTSIDER))

    assert node.core._voice_transcriptions == {}
    assert node.events() == []
    assert await node.shown() == {"m-voice": None}


@pytest.mark.asyncio
async def test_a_one_to_one_peer_transcribes_its_own_conversations_record_and_nothing_else(node):
    """Control: in one-to-one the peer is the one who may transcribe, and only there."""
    node.write_record(MEMBER, "m-voice")
    node.one_to_one(OUTSIDER, "our-transfer-with-the-peer")

    await node.handler.handle(OUTSIDER, _said("what the peer heard", transcriber=OUTSIDER,
                                              transfer_id="our-transfer-with-the-peer"))

    assert await node.shown(OUTSIDER) == {"p-our-transfer-with-the-peer": "what the peer heard"}
    assert await node.shown() == {"m-voice": None}
    [event] = node.events()
    assert event["conversation_id"] == OUTSIDER, "the UI updates that chat only"


@pytest.mark.asyncio
async def test_a_transfer_id_taken_first_under_another_conversation_does_not_silence_a_member(node):
    """Zcode #27: the first entry under a transfer id made the member's a duplicate."""
    node.write_record(MEMBER, "m-voice")
    node.one_to_one(OUTSIDER, "the-members-transfer")

    await node.handler.handle(OUTSIDER, _said("squatted", transcriber=OUTSIDER))
    await node.handler.handle(MEMBER, _said("what was said", group_id=node.group_id))

    assert await node.shown() == {"m-voice": "what was said"}
    assert await node.shown(OUTSIDER) == {"p-the-members-transfer": "squatted"}


@pytest.mark.asyncio
async def test_a_group_member_cannot_put_text_into_a_one_to_one_record_by_its_file_name(node):
    """Johnny #25 P3-1(b): the gate checked the group, the join used the global name."""
    node.one_to_one(OUTSIDER, "our-transfer-with-the-peer")

    await node.handler.handle(MEMBER, _said("injected", group_id=node.group_id))

    assert await node.shown(OUTSIDER) == {"p-our-transfer-with-the-peer": None}


@pytest.mark.asyncio
async def test_two_records_of_one_name_take_only_the_transcription_naming_their_message(node):
    """Johnny #25 P3-1(a): two nodes sending in the same second mint the same file name."""
    node.write_record(MEMBER, "m-from-b", transfer_id="t-b")
    node.write_record(OTHER_MEMBER, "m-from-c", transfer_id="t-c")

    await node.handler.handle(MEMBER, _said("c's words", message_id="m-from-c"))  # wrong: no group
    await node.handler.handle(MEMBER, _said("c's words", group_id=node.group_id,
                                            message_id="m-from-c"))
    await node.handler.handle(OTHER_MEMBER, _said("no message id", group_id=node.group_id,
                                                  transfer_id="t-legacy"))

    assert await node.shown() == {"m-from-b": None, "m-from-c": "c's words"}


# --- 2. a parked transcription lands, and the bound holds ----------------------------


@pytest.mark.asyncio
async def test_a_parked_members_transcription_lands_on_the_disk_view_when_its_record_arrives(node):
    """Zcode #27: on the live path the parked text was never shown. Executed falsifier."""
    await node.handler.handle(MEMBER, _said("ahead of the file", group_id=node.group_id))
    assert node.handler._parked

    await node.record_arrives(OTHER_MEMBER, "m-voice")

    assert await node.shown() == {"m-voice": "ahead of the file"}
    assert not node.handler._parked, "released by the record, not by a next transcription"
    landed = node.events()[-1]
    assert landed["conversation_id"] == node.group_id and landed["message_id"] == "m-voice", (
        "the open chat is told when the text lands")


@pytest.mark.asyncio
async def test_a_record_that_comes_by_history_sync_lands_its_parked_transcription_on_read(node):
    await node.handler.handle(MEMBER, _said("ahead of the sync", group_id=node.group_id))

    node.write_record(OTHER_MEMBER, "m-synced")  # merged, no transfer: no receive-path call

    assert await node.shown() == {"m-synced": "ahead of the sync"}
    assert not node.handler._parked


@pytest.mark.asyncio
async def test_a_history_answer_lands_the_parked_transcription_and_tells_the_open_chat(node):
    """Follow-up 11e: the sync's merge settled nothing; the text waited for the next read and the
    live UI heard nothing. Goes through GroupHistoryResponseHandler with the real manager."""
    from dpc_client_core.message_handlers.group_handler import GroupHistoryResponseHandler

    await node.handler.handle(MEMBER, _said("ahead of the answer", group_id=node.group_id))
    assert node.handler._parked
    node.core._get_or_create_conversation_monitor = lambda gid: node.group
    node.core.history_requests = SimpleNamespace(claim=lambda *a: True, claim_any=lambda *a: True)
    record = {"id": "m-answer", "role": "assistant", "content": "Received voice message",
              "sender_node_id": OTHER_MEMBER, "timestamp": "2026-11-24T10:00:00+00:00",
              "attachments": [{"type": "voice", "filename": VOICE, "transfer_id": "t-x"}]}

    await GroupHistoryResponseHandler(node.core).handle(
        MEMBER, {"group_id": node.group_id, "history": [record], "request_id": "r-1"})

    assert not node.handler._parked, "settled by the answer, not by a later read"
    landed = node.events()
    assert landed and landed[-1]["message_id"] == "m-answer", "the open chat is told, as for a live attach"
    assert landed[-1]["conversation_id"] == node.group_id


@pytest.mark.asyncio
async def test_a_landed_transcription_leaves_the_bound_and_is_not_evicted_by_later_ones(node):
    """Ark #24 (3): 32 parked, the oldest's record comes, a 33rd arrives."""
    await node.handler.handle(MEMBER, _said("the oldest", group_id=node.group_id,
                                            transfer_id="t-0", filename="v-0.webm"))
    for n in range(1, vth.PARKED_PER_GROUP):
        await node.handler.handle(MEMBER, _said(f"n{n}", group_id=node.group_id,
                                                transfer_id=f"t-{n}", filename=f"v-{n}.webm"))

    await node.record_arrives(OTHER_MEMBER, "m-0", filename="v-0.webm", transfer_id="r-0")
    await node.handler.handle(MEMBER, _said("one more", group_id=node.group_id,
                                            transfer_id="t-new", filename="v-new.webm"))

    assert (await node.shown())["m-0"] == "the oldest"


@pytest.mark.asyncio
async def test_the_age_limit_holds_with_no_further_transcription(node):
    """Johnny #25 P3-2: park one, send nothing more, read past the age limit."""
    await node.handler.handle(MEMBER, _said("too late", group_id=node.group_id))
    node.now[0] += vth.PARKED_MAX_AGE_SECONDS + 1

    await node.shown()

    assert node.core._voice_transcriptions == {} and not node.handler._parked
    await node.record_arrives(OTHER_MEMBER, "m-late")
    assert await node.shown() == {"m-late": None}


@pytest.mark.asyncio
async def test_transcriptions_joined_by_name_do_not_grow_the_registry(node):
    """Zcode #27: 96 entries joined by name, 0 parked. One record takes one transcription."""
    node.write_record(OTHER_MEMBER, "m-voice")

    for n in range(96):
        await node.handler.handle(MEMBER, _said(f"take {n}", group_id=node.group_id,
                                                transfer_id=f"t-{n}"))

    assert len(node.core._voice_transcriptions) == 1
    assert await node.shown() == {"m-voice": "take 0"}


# --- 3. a transcriber id that is not a string ---------------------------------------


@pytest.mark.asyncio
async def test_a_transcriber_id_that_is_not_a_string_is_dropped_without_an_error(node):
    node.write_record(OTHER_MEMBER, "m-voice")

    await node.handler.handle(MEMBER, _said("x", group_id=node.group_id, transcriber_node_id=12345))
    await node.handler.handle(MEMBER, _said("x", group_id=node.group_id, transcriber_node_id=["a"]))

    assert node.core._voice_transcriptions == {}
    assert await node.shown() == {"m-voice": None}
