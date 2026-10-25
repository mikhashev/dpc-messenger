"""An offer that waits for a person has a deadline, on both sides.

Mike's call, 2026-10-25 (review of 93ebbecf by Ark and Zcode): a receiver that
puts "allow files from X?" to its user and hears nothing held the sender's upload
slot for good, and three such offers stopped every file, screenshot and voice
message the sender had. Now:

- the receiver gives every offer that waits for a person (the question, and the
  accept button of an ordinary file) OFFER_ANSWER_DEADLINE_S, 120 s; with no
  answer it refuses on its own with FILE_CANCEL `no_answer`, drops the download
  and tells its UI;
- the sender frees an upload that had neither FILE_ACCEPT nor FILE_CANCEL after
  UNANSWERED_UPLOAD_RELEASE_S, 180 s, so a receiver that never sends `no_answer`
  cannot hold a slot either; and it frees the upload on a received `no_answer`
  like on any FILE_CANCEL.

The clocks are held by a test double for `_answer_sleep`: no test waits for
minutes, and each clock is fired by hand. The firewall, manager, handlers and
coordinator are real; only the wire and the UI socket are collected.
"""

import asyncio

import pytest
import pytest_asyncio

import tests.test_a_peer_on_a_direct_connection_may_send_files_and_a_stranger_is_asked as base
from dpc_client_core.managers import file_transfer_manager as ftm
from dpc_client_core.message_handlers.file_cancel_handler import FileCancelHandler

PEER = base.PEER
NO_ENTRIES = base.NO_ENTRIES
ALLOW_DIRECT = base.ALLOW_DIRECT
DIRECT = base.DIRECT
_Node = base._Node
_offer = base._offer


class _Clocks:
    """Stands in for `asyncio.sleep` in the answer clocks: records the span, waits for a hand."""

    def __init__(self):
        self.started = []  # (seconds, gate)

    async def sleep(self, seconds):
        gate = asyncio.Event()
        self.started.append((seconds, gate))
        await gate.wait()

    @property
    def spans(self):
        return [seconds for seconds, _gate in self.started]

    async def fire_all(self):
        for _seconds, gate in self.started:
            gate.set()
        await settle()


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


async def clocks_running(clocks, count=1):
    """Let the clocks reach their sleep and say how many wait: a test that answers
    before its clock started would pass without ever testing the clock."""
    await settle()
    assert len(clocks.started) == count
    assert not any(gate.is_set() for _seconds, gate in clocks.started)


@pytest.fixture
def clocks(monkeypatch):
    held = _Clocks()
    monkeypatch.setattr(ftm, "_answer_sleep", held.sleep)
    return held


@pytest_asyncio.fixture(autouse=True)
async def _close_every_node():
    yield
    while base._NODES:
        await base._NODES.pop().close()


def _cancels(node):
    return [m["payload"] for m in node.sent if m["command"] == "FILE_CANCEL"]


def _cancelled_events(node):
    return [p for name, p in node.events if name == "file_transfer_cancelled"]


# --- the numbers -----------------------------------------------------------------


def test_the_deadline_is_120_seconds_and_the_sender_waits_longer_than_the_receiver():
    assert ftm.OFFER_ANSWER_DEADLINE_S == 120.0
    assert ftm.UNANSWERED_UPLOAD_RELEASE_S == 180.0
    assert ftm.UNANSWERED_UPLOAD_RELEASE_S > ftm.OFFER_ANSWER_DEADLINE_S
    assert ftm.NO_ANSWER_REASON == "no_answer"


# --- the receiver ------------------------------------------------------------------


@pytest.mark.parametrize("kind,group_id", [
    ("file", None), ("image", None), ("voice", None), ("file", "group-x"),
])
@pytest.mark.asyncio
async def test_an_asked_offer_of_any_kind_is_refused_with_no_answer_when_nobody_answers(clocks, tmp_path, kind, group_id):
    node = _Node(tmp_path, NO_ENTRIES)
    await node.handler.handle(PEER, _offer(kind=kind, group_id=group_id))
    await settle()
    assert clocks.spans == [ftm.OFFER_ANSWER_DEADLINE_S]
    assert node.sent == []

    await clocks.fire_all()

    [cancel] = _cancels(node)
    assert cancel == {"transfer_id": "t1", "reason": "no_answer"}
    assert "t1" not in node.manager.active_transfers
    [event] = _cancelled_events(node)
    assert (event["transfer_id"], event["reason"], event["direction"]) == ("t1", "no_answer", "download")
    assert event.get("group_id") == group_id


@pytest.mark.asyncio
async def test_an_ordinary_file_waiting_for_the_accept_click_has_the_deadline_too(clocks, tmp_path):
    node = _Node(tmp_path, ALLOW_DIRECT, connection_type=DIRECT)
    await node.handler.handle(PEER, _offer())  # allowed by rule, waits for the click
    await settle()
    assert clocks.spans == [ftm.OFFER_ANSWER_DEADLINE_S]

    await clocks.fire_all()

    assert [c["reason"] for c in _cancels(node)] == ["no_answer"]
    assert node.manager.active_transfers == {}


@pytest.mark.parametrize("kind,group_id", [("image", None), ("voice", None), ("file", "group-x")])
@pytest.mark.asyncio
async def test_an_offer_accepted_on_arrival_has_no_deadline(clocks, tmp_path, kind, group_id):
    rules = {"file_transfer": {"nodes": {PEER: {"file_transfer.allow": "allow"}}, "groups": {}}}
    node = _Node(tmp_path, rules)

    await node.handler.handle(PEER, _offer(kind=kind, group_id=group_id))

    assert node.commands() == ["FILE_ACCEPT"]
    assert clocks.started == []


@pytest.mark.asyncio
async def test_an_offer_answered_in_time_is_not_refused_later(clocks, tmp_path):
    node = _Node(tmp_path, NO_ENTRIES)
    await node.handler.handle(PEER, _offer("t1"))
    await clocks_running(clocks)

    await node.coordinator.accept_file_transfer("t1")
    await clocks.fire_all()

    assert node.commands() == ["FILE_ACCEPT"]
    assert "t1" in node.manager.active_transfers
    assert _cancelled_events(node) == []


@pytest.mark.asyncio
async def test_an_offer_declined_in_time_sends_one_cancel_and_the_clock_stays_quiet(clocks, tmp_path):
    node = _Node(tmp_path, NO_ENTRIES)
    await node.handler.handle(PEER, _offer("t1"))
    await clocks_running(clocks)
    clock = node.manager.active_transfers["t1"].answer_timer

    await node.coordinator.cancel_file_transfer("t1", "user_rejected")
    await settle()
    assert clock.done()  # the answer stopped the clock
    await clocks.fire_all()

    assert [c["reason"] for c in _cancels(node)] == ["user_rejected"]


@pytest.mark.asyncio
async def test_an_accept_that_is_slow_to_save_cannot_be_overtaken_by_the_deadline(clocks, tmp_path):
    """The click is the answer: the deadline stops before "always allow" is saved."""
    node = _Node(tmp_path, NO_ENTRIES)
    await node.handler.handle(PEER, _offer("t1"))
    await clocks_running(clocks)
    saved = node.coordinator._allow_files_from

    async def _slow_save(node_id):
        await clocks.fire_all()  # the 120 s run out while the rule is being written
        return await saved(node_id)

    node.coordinator._allow_files_from = _slow_save

    result = await node.coordinator.accept_file_transfer("t1", remember=True)

    assert result["remembered"] is True
    assert node.commands() == ["FILE_ACCEPT"]
    assert "t1" in node.manager.active_transfers


@pytest.mark.asyncio
async def test_always_allow_also_stops_the_deadline_of_the_senders_other_waiting_offers(clocks, tmp_path):
    node = _Node(tmp_path, NO_ENTRIES)
    await node.handler.handle(PEER, _offer("t1", kind="image"))
    await node.handler.handle(PEER, _offer("t2", kind="image"))
    await clocks_running(clocks, 2)

    await node.coordinator.accept_file_transfer("t2", remember=True)
    await clocks.fire_all()

    assert sorted(m["payload"]["transfer_id"] for m in node.sent if m["command"] == "FILE_ACCEPT") == ["t1", "t2"]
    assert _cancels(node) == []


@pytest.mark.asyncio
async def test_a_second_call_does_not_start_a_second_clock(clocks, tmp_path):
    node = _Node(tmp_path, NO_ENTRIES)
    await node.handler.handle(PEER, _offer("t1"))

    node.manager.begin_waiting_for_answer("t1")
    await settle()

    assert len(clocks.started) == 1


@pytest.mark.asyncio
async def test_a_sender_that_gave_up_drops_the_waiting_offer_of_a_receiver(clocks, tmp_path):
    """The sender's own `no_answer` clears an offer an older receiver still shows."""
    node = _Node(tmp_path, NO_ENTRIES)
    await node.handler.handle(PEER, _offer("t1"))
    await clocks_running(clocks)

    await FileCancelHandler(node.service).handle(PEER, {"transfer_id": "t1", "reason": "no_answer"})

    assert "t1" not in node.manager.active_transfers
    [event] = _cancelled_events(node)
    assert (event["reason"], event["direction"]) == ("no_answer", "download")
    await clocks.fire_all()
    assert _cancels(node) == []  # the receiver does not answer a cancel with a cancel


# --- the sender --------------------------------------------------------------------


def _sender(tmp_path, slots=1):
    node = _Node(tmp_path, NO_ENTRIES)
    node.manager.max_concurrent_transfers = slots
    return node


def _file(tmp_path, name="a.bin"):
    path = tmp_path / name
    path.write_bytes(b"x" * 100)
    return path


@pytest.mark.asyncio
async def test_an_upload_nobody_answers_is_released_after_180_seconds(clocks, tmp_path):
    node = _sender(tmp_path)
    transfer_id = await node.manager.send_file(PEER, _file(tmp_path))
    await settle()
    assert clocks.spans == [ftm.UNANSWERED_UPLOAD_RELEASE_S]
    assert [u["status"] for u in node.manager.open_uploads()] == ["pending"]

    await clocks.fire_all()

    assert node.manager.open_uploads() == []
    assert transfer_id not in node.manager.active_transfers
    assert _cancels(node) == [{"transfer_id": transfer_id, "reason": "no_answer"}]
    [event] = _cancelled_events(node)
    assert (event["transfer_id"], event["reason"], event["direction"]) == (transfer_id, "no_answer", "upload")
    assert event["filename"] == "a.bin"


@pytest.mark.asyncio
async def test_the_released_slot_takes_the_next_file(clocks, tmp_path):
    node = _sender(tmp_path, slots=1)
    await node.manager.send_file(PEER, _file(tmp_path, "a.bin"))
    await settle()
    with pytest.raises(ftm.MaxConcurrentTransfersExceeded):
        await node.manager.send_file(PEER, _file(tmp_path, "b.bin"))

    await clocks.fire_all()
    second = await node.manager.send_file(PEER, _file(tmp_path, "b.bin"))

    assert second in node.manager.active_transfers


@pytest.mark.asyncio
async def test_a_received_no_answer_frees_the_upload_and_tells_the_ui_it_was_unanswered(clocks, tmp_path):
    node = _sender(tmp_path)
    transfer_id = await node.manager.send_file(PEER, _file(tmp_path))
    await clocks_running(clocks)
    clock = node.manager.active_transfers[transfer_id].answer_timer

    await FileCancelHandler(node.service).handle(PEER, {"transfer_id": transfer_id, "reason": "no_answer"})
    await settle()

    assert clock.done()  # leaving the table stopped the clock, it does not sleep on
    assert node.manager.open_uploads() == []
    [event] = _cancelled_events(node)
    assert (event["reason"], event["direction"], event["filename"]) == ("no_answer", "upload", "a.bin")
    await clocks.fire_all()
    assert _cancels(node) == []  # the clock died with the upload; nothing is sent back


@pytest.mark.asyncio
async def test_an_accepted_upload_is_not_released(clocks, tmp_path):
    node = _sender(tmp_path)
    transfer_id = await node.manager.send_file(PEER, _file(tmp_path))
    await clocks_running(clocks)

    await node.manager.handle_file_accept(PEER, transfer_id)
    await clocks.fire_all()

    assert transfer_id in node.manager.active_transfers
    assert _cancels(node) == []
    assert _cancelled_events(node) == []


@pytest.mark.asyncio
async def test_an_upload_cancelled_by_its_user_stops_its_clock(clocks, tmp_path):
    node = _sender(tmp_path)
    transfer_id = await node.manager.send_file(PEER, _file(tmp_path))
    await clocks_running(clocks)

    await node.manager.cancel_transfer(transfer_id, "user_cancelled")
    await clocks.fire_all()

    assert [c["reason"] for c in _cancels(node)] == ["user_cancelled"]
    assert _cancelled_events(node) == []


# --- who the dialog names for a group file ---------------------------------------


AUTHOR = "dpc-node-" + "c" * 32


@pytest.mark.asyncio
async def test_a_group_offer_put_to_the_user_names_the_author_beside_the_hop(clocks, tmp_path):
    """The dialog says "from X via Y": `node_id` is the hop, `sender_node_id` the unsigned author."""
    node = _Node(tmp_path, NO_ENTRIES)
    payload = _offer(group_id="group-x")
    payload.update({"message_id": "m-1", "sender_node_id": AUTHOR})

    await node.handler.handle(PEER, payload)

    [event] = node.offered()
    assert event["node_id"] == PEER
    assert event["sender_node_id"] == AUTHOR
    assert event["group_id"] == "group-x"


@pytest.mark.asyncio
async def test_an_offer_with_no_author_claim_carries_no_sender_node_id(clocks, tmp_path):
    node = _Node(tmp_path, NO_ENTRIES)

    await node.handler.handle(PEER, _offer(group_id="group-x"))
    await node.handler.handle(PEER, _offer("t2"))

    assert all("sender_node_id" not in e for e in node.offered())
