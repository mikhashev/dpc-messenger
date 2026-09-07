"""A vote on a proposal read from messages this node lacks asks for them.

The drift guard was a dead end: it refused the vote, named a count, and offered
no way to get the missing records — and the refusal travelled back inside an OK
envelope, so the dialog closed as though the vote had been cast. Mike's call,
2026-09-06: on a non-empty difference, request the records from the peer and
vote again.

The whole path is here: the request that names the records by `content_hash`,
the export that answers exactly those, the merge that says which of them it
refused, and the held vote resolving into one of three outcomes — cast, refused
because the records never came, refused because they came and did not verify.
"""

import asyncio
from types import SimpleNamespace

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.knowledge_service import KnowledgeService
from dpc_client_core.message_handlers.chat_history_handlers import HistoryRequestRegistry
from dpc_client_core.message_handlers.group_handler import (
    GroupHistoryRequestHandler,
    GroupHistoryResponseHandler,
)

GROUP = "group-b88b65076b85"
ALICE = "dpc-node-" + "a" * 32
BOB = "dpc-node-" + "b" * 32


@pytest.fixture(autouse=True)
def _never_touch_disk(monkeypatch):
    monkeypatch.setattr(
        ConversationMonitor, "persist_history", property(lambda self: False)
    )


def _monitor(texts=("one", "two", "three")):
    m = ConversationMonitor(
        conversation_id=GROUP,
        participants=[{"node_id": ALICE, "name": "Mike", "context": "local"}],
        llm_manager=None,
    )
    for text in texts:
        m.add_message(role="user", content=text, sender_node_id=ALICE, sender_name="Mike")
    return m


def _window(monitor):
    return [m["content_hash"] for m in monitor.message_history if m.get("content_hash")]


def _service(monitor, window, peers=(BOB,)):
    """A KnowledgeService with only the parts the vote path touches."""
    sent, events, votes = [], [], []

    async def _send(node_id, message):
        sent.append((node_id, message))

    async def _broadcast(event, payload):
        events.append((event, payload))

    async def _cast_vote(**kwargs):
        votes.append(kwargs)
        return True

    svc = KnowledgeService.__new__(KnowledgeService)
    svc.conversation_monitors = {GROUP: monitor}
    svc.consensus_manager = SimpleNamespace(
        sessions={
            "p1": SimpleNamespace(
                proposal=SimpleNamespace(
                    proposal_id="p1",
                    conversation_id=GROUP,
                    based_on_content_hashes=window,
                    participants=[ALICE, BOB],
                ),
                status="voting",
            )
        },
        cast_vote=_cast_vote,
    )
    svc.p2p_manager = SimpleNamespace(
        node_id=ALICE, peers={p: object() for p in peers}, send_message_to_peer=_send
    )
    svc.history_requests = HistoryRequestRegistry()
    svc.local_api = SimpleNamespace(broadcast_event=_broadcast)
    svc.llm_manager = SimpleNamespace(providers={})
    svc._pending_votes = {}
    return svc, sent, events, votes


def _drop_last_message(monitor):
    """Leave the monitor one record short of the window it will be asked about."""
    return monitor.message_history.pop()


async def _cancel_pending(svc):
    for pending in svc._pending_votes.values():
        task = pending.get("timeout_task")
        if task is not None:
            task.cancel()
    await asyncio.sleep(0)


# --- the request ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_request_names_exactly_the_records_that_are_missing():
    monitor = _monitor()
    window = _window(monitor)
    gone = _drop_last_message(monitor)
    svc, sent, _events, votes = _service(monitor, window)

    result = await KnowledgeService.vote_knowledge_commit(svc, "p1", "approve")

    assert [m["command"] for _peer, m in sent] == ["GROUP_HISTORY_REQUEST"]
    assert sent[0][1]["payload"]["content_hashes"] == [gone["content_hash"]]
    assert sent[0][0] == BOB
    assert result["status"] == "pending"
    assert not votes, "the vote was cast on a history missing the window"
    await _cancel_pending(svc)


@pytest.mark.asyncio
async def test_the_request_is_noted_so_the_answer_can_be_claimed():
    """An unnoted request earns an answer the response handler discards."""
    monitor = _monitor()
    window = _window(monitor)
    _drop_last_message(monitor)
    svc, sent, _events, _votes = _service(monitor, window)

    await KnowledgeService.vote_knowledge_commit(svc, "p1", "approve")

    request_id = sent[0][1]["payload"]["request_id"]
    assert svc.history_requests.claim(BOB, GROUP, request_id)
    await _cancel_pending(svc)


@pytest.mark.asyncio
async def test_a_vote_with_nobody_to_ask_is_refused_and_says_so():
    monitor = _monitor()
    window = _window(monitor)
    _drop_last_message(monitor)
    svc, sent, _events, votes = _service(monitor, window, peers=())

    result = await KnowledgeService.vote_knowledge_commit(svc, "p1", "approve")

    assert result["status"] == "error"
    assert result["reason"] == "history_drift"
    assert result["retry"] == "no_peer_to_ask"
    assert not sent and not votes


@pytest.mark.asyncio
async def test_a_refusal_needs_no_evidence_and_is_cast_at_once():
    """Rejecting is «I do not sign this»; it does not require holding the text."""
    monitor = _monitor()
    window = _window(monitor)
    _drop_last_message(monitor)
    svc, sent, _events, votes = _service(monitor, window)

    result = await KnowledgeService.vote_knowledge_commit(svc, "p1", "reject")

    assert [v["vote"] for v in votes] == ["reject"]
    assert result["status"] == "success"
    assert not sent, "a refusal asked the peer for records it does not need"
    assert svc._pending_votes == {}


@pytest.mark.asyncio
async def test_asking_for_changes_is_a_judgement_and_stays_held():
    """`request_changes` says something about the text, so it needs the text."""
    monitor = _monitor()
    window = _window(monitor)
    _drop_last_message(monitor)
    svc, sent, _events, votes = _service(monitor, window)

    result = await KnowledgeService.vote_knowledge_commit(svc, "p1", "request_changes")

    assert result["status"] == "pending"
    assert not votes
    assert [m["command"] for _peer, m in sent] == ["GROUP_HISTORY_REQUEST"]
    await _cancel_pending(svc)


# --- the answer -------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_held_vote_is_cast_once_the_records_arrive():
    monitor = _monitor()
    window = _window(monitor)
    gone = _drop_last_message(monitor)
    svc, _sent, events, votes = _service(monitor, window)

    await KnowledgeService.vote_knowledge_commit(svc, "p1", "approve")
    monitor.message_history.append(gone)  # what the merge would have stored
    await svc.retry_pending_votes(GROUP, rejected=[])

    assert [v["proposal_id"] for v in votes] == ["p1"]
    assert votes[0]["vote"] == "approve"
    assert svc._pending_votes == {}
    resolved = [p for name, p in events if name == "knowledge_vote_resolved"]
    assert resolved and resolved[0]["status"] == "success"


@pytest.mark.asyncio
async def test_an_answer_without_the_records_drops_the_vote():
    monitor = _monitor()
    window = _window(monitor)
    _drop_last_message(monitor)
    svc, _sent, events, votes = _service(monitor, window)

    await KnowledgeService.vote_knowledge_commit(svc, "p1", "approve")
    await svc.retry_pending_votes(GROUP, rejected=[])

    assert not votes
    assert svc._pending_votes == {}
    resolved = [p for name, p in events if name == "knowledge_vote_resolved"]
    assert resolved[0]["reason"] == "records_not_held"


@pytest.mark.asyncio
async def test_a_record_that_arrived_and_failed_verification_is_named_as_such():
    """"It never came" and "it came and is not what it claims" are not one thing."""
    monitor = _monitor()
    window = _window(monitor)
    gone = _drop_last_message(monitor)
    svc, _sent, events, _votes = _service(monitor, window)

    await KnowledgeService.vote_knowledge_commit(svc, "p1", "approve")
    await svc.retry_pending_votes(GROUP, rejected=[{
        "id": gone["id"],
        "content_hash": gone["content_hash"],
        "sender_node_id": ALICE,
        "verdict": "content does not match its hash",
    }])

    resolved = [p for name, p in events if name == "knowledge_vote_resolved"]
    assert resolved[0]["reason"] == "unverifiable_record"
    assert resolved[0]["unverifiable"] == [gone["content_hash"]]


# --- the two wire seams -----------------------------------------------------


def test_an_export_by_content_hash_carries_those_records_and_no_others():
    monitor = _monitor()
    wanted = monitor.message_history[1]["content_hash"]

    exported = monitor.export_history(content_hashes=[wanted])

    assert [m["content_hash"] for m in exported] == [wanted]


def test_an_empty_hash_list_is_not_read_as_no_filter():
    assert _monitor().export_history(content_hashes=[]) == []


@pytest.mark.asyncio
async def test_the_answering_side_honours_the_hashes_it_was_asked_for():
    monitor = _monitor()
    wanted = monitor.message_history[1]["content_hash"]
    sent = []

    async def _send(node_id, message):
        sent.append(message)

    service = SimpleNamespace(
        conversation_monitors={GROUP: monitor},
        _get_or_create_conversation_monitor=lambda gid: monitor,
        p2p_manager=SimpleNamespace(send_message_to_peer=_send),
        group_manager=SimpleNamespace(
            get_group=lambda gid: SimpleNamespace(members=[ALICE, BOB])
        ),
    )

    await GroupHistoryRequestHandler(service).handle(
        BOB, {"group_id": GROUP, "content_hashes": [wanted], "request_id": "r1"}
    )

    payload = sent[0]["payload"]
    assert [m["content_hash"] for m in payload["history"]] == [wanted]
    assert payload["content_hashes"] == [wanted]


@pytest.mark.asyncio
async def test_the_records_a_merge_refused_reach_the_waiting_vote():
    """The response handler is the only place that knows both halves."""
    monitor = _monitor()
    tampered = dict(monitor.export_history()[0])
    tampered["id"] = "forged-1"
    tampered["content"] = "not what the hash says"
    retried = []

    async def _retry(group_id, rejected=None, request_id=None):
        retried.append((group_id, rejected, request_id))

    registry = HistoryRequestRegistry()
    registry.note(BOB, GROUP, "r1")
    service = SimpleNamespace(
        conversation_monitors={GROUP: monitor},
        _get_or_create_conversation_monitor=lambda gid: monitor,
        group_manager=SimpleNamespace(
            get_group=lambda gid: SimpleNamespace(members=[ALICE, BOB])
        ),
        history_requests=registry,
        knowledge_service=SimpleNamespace(retry_pending_votes=_retry),
        local_api=SimpleNamespace(broadcast_event=_noop_event),
    )

    await GroupHistoryResponseHandler(service).handle(
        BOB, {"group_id": GROUP, "history": [tampered], "request_id": "r1"}
    )

    assert len(retried) == 1
    group_id, rejected, request_id = retried[0]
    assert group_id == GROUP and request_id == "r1"
    assert [r["id"] for r in rejected] == ["forged-1"]


@pytest.mark.asyncio
async def test_an_empty_answer_still_reaches_the_waiting_vote():
    """"The peer had nothing" is an outcome a held vote needs to hear."""
    retried = []

    async def _retry(group_id, rejected=None, request_id=None):
        retried.append(group_id)

    registry = HistoryRequestRegistry()
    registry.note(BOB, GROUP, "r1")
    service = SimpleNamespace(
        conversation_monitors={},
        group_manager=SimpleNamespace(
            get_group=lambda gid: SimpleNamespace(members=[ALICE, BOB])
        ),
        history_requests=registry,
        knowledge_service=SimpleNamespace(retry_pending_votes=_retry),
    )

    await GroupHistoryResponseHandler(service).handle(
        BOB, {"group_id": GROUP, "history": [], "request_id": "r1"}
    )

    assert retried == [GROUP]


async def _noop_event(event, payload):
    return None
