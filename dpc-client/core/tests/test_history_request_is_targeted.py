"""β names the authors it wants, and the answering side sends everything anyway.

Found by @Fable 5 in the external review of 2026-08-06, verified in the code:
`group_handler.py:710` puts the diverging authors into GROUP_HISTORY_REQUEST,
and `GroupHistoryRequestHandler.handle` reads only `group_id` from that payload
before answering with `export_history()` — "No message limit - returns full
history". So β's advertised property, "sync asks only for what is missing", held
in the request and never in the transfer, and every observed transfer was a full
one no matter which branch asked for it.

Detection and transfer are different layers. This covers the second one.

A peer that predates the field sends no `authors`, and must keep receiving
everything — that is the compatibility half, and it is a test here rather than a
comment because it is the half a filter is most likely to break.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.message_handlers.group_handler import GroupHistoryRequestHandler

GROUP = "group-1234567890ab"
ALICE = "dpc-node-" + "a" * 32
BOB = "dpc-node-" + "b" * 32


PARTICIPANTS = [{"node_id": "n1", "name": "User", "context": "local"}]


def _monitor(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ConversationMonitor, "persist_history", property(lambda self: False)
    )
    monitor = ConversationMonitor(
        conversation_id=GROUP, participants=PARTICIPANTS, llm_manager=None
    )
    monitor._get_history_path = lambda: tmp_path / GROUP / "history.json"
    monitor.add_message("user", "from alice 1", sender_node_id=ALICE)
    monitor.add_message("assistant", "from bob 1", sender_node_id=BOB)
    monitor.add_message("user", "from alice 2", sender_node_id=ALICE)
    return monitor


def _handler(monitor, sent):
    service = SimpleNamespace(
        conversation_monitors={GROUP: monitor},
        _get_or_create_conversation_monitor=lambda gid: monitor,
        p2p_manager=SimpleNamespace(send_message_to_peer=_collect(sent)),
        # The door asks the roster before it answers; this file is about which
        # authors travel, so BOB asks as the member he is.
        group_manager=SimpleNamespace(
            get_group=lambda gid: SimpleNamespace(members=[ALICE, BOB]) if gid == GROUP else None
        ),
    )
    return GroupHistoryRequestHandler(service)


def _senders(history):
    return [m.get("sender_node_id") for m in history]


# --- the export itself ------------------------------------------------------


def test_an_export_without_a_filter_is_the_whole_history(tmp_path, monkeypatch):
    """The old behaviour, kept: no filter means no filtering."""
    monitor = _monitor(tmp_path, monkeypatch)
    assert len(monitor.export_history()) == 3


def test_an_export_for_one_author_carries_only_that_author(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    exported = monitor.export_history(authors=[ALICE])
    assert _senders(exported) == [ALICE, ALICE]


def test_an_export_for_several_authors_carries_all_of_them(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    exported = monitor.export_history(authors=[ALICE, BOB])
    assert len(exported) == 3


def test_an_author_we_hold_nothing_for_yields_nothing(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    assert monitor.export_history(authors=["dpc-node-" + "c" * 32]) == []


def test_an_empty_author_list_is_not_read_as_no_filter(tmp_path, monkeypatch):
    """`authors: []` means "nothing differs", and must not fall back to all.

    A falsy-check on the list would turn the cheapest request into the most
    expensive one.
    """
    monitor = _monitor(tmp_path, monkeypatch)
    assert monitor.export_history(authors=[]) == []


def test_a_filtered_message_is_unchanged(tmp_path, monkeypatch):
    """Filtering selects; it must not reshape what it selects.

    The export's field set is load-bearing — signature fields travel only when
    made over the current preimage, and dropping one had the receiver reject an
    untouched message as tampered.
    """
    monitor = _monitor(tmp_path, monkeypatch)
    whole = {m["id"]: m for m in monitor.export_history()}
    for msg in monitor.export_history(authors=[ALICE]):
        assert msg == whole[msg["id"]]


# --- the handler that answers the request -----------------------------------


@pytest.mark.asyncio
async def test_the_handler_honours_the_authors_it_was_given(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    sent = []

    await _handler(monitor, sent).handle(BOB, {"group_id": GROUP, "authors": [ALICE]})

    assert len(sent) == 1
    assert _senders(sent[0]["payload"]["history"]) == [ALICE, ALICE]


@pytest.mark.asyncio
async def test_a_peer_that_sends_no_authors_still_gets_everything(tmp_path, monkeypatch):
    """Compatibility: a node from before the field asks the old way."""
    monitor = _monitor(tmp_path, monkeypatch)
    sent = []

    await _handler(monitor, sent).handle(BOB, {"group_id": GROUP})

    assert len(sent[0]["payload"]["history"]) == 3


@pytest.mark.asyncio
async def test_the_reply_says_which_authors_it_answers(tmp_path, monkeypatch):
    """The receiver merges into a shared pool; it should know what it was sent.

    Without this the response is indistinguishable from a full history that
    happens to be short, and a later "only these authors" reconciliation cannot
    tell a filtered answer from a complete one.
    """
    monitor = _monitor(tmp_path, monkeypatch)
    sent = []

    await _handler(monitor, sent).handle(BOB, {"group_id": GROUP, "authors": [ALICE]})

    assert sent[0]["payload"]["authors"] == [ALICE]


@pytest.mark.asyncio
async def test_an_unfiltered_reply_claims_no_author_scope(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    sent = []

    await _handler(monitor, sent).handle(BOB, {"group_id": GROUP})

    assert "authors" not in sent[0]["payload"]


def _collect(sink):
    async def _send(node_id, message):
        sink.append(message)
    return _send
