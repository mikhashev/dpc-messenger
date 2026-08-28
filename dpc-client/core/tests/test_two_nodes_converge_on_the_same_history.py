"""What two nodes have to exchange before their copies of a group agree.

The machinery already existed: each side advertises a per-author digest in
GROUP_HISTORY_STATUS, the other names the authors that differ, and only those
messages are fetched. Two things stopped it working.

**The digest was omitted whenever the group's monitor was not loaded** — and
monitors are created lazily, so on connect that is the usual case. Both sides
then fell back to comparing chain tips, and a chain tip covers `msg_index`,
`prev_hash` and `role`: arrival order and a per-reader rendering. Between two
honest nodes holding identical messages it never matches.

**And the status was computed from whichever folder a fourth resolver picked**,
which while a group was split was not necessarily the folder the monitor wrote
to — so a node could advertise a history it was not keeping.
"""

import json
from pathlib import Path

import pytest

from dpc_client_core import conversation_paths as cp
from dpc_client_core.conversation_monitor import (
    ConversationMonitor,
    authors_that_differ_between,
    chain_hash_for,
    digest_for,
)


GROUP = "group-970e5c7006a0"
ALICE = "dpc-node-" + "a" * 32
BOB = "dpc-node-" + "b" * 32


def _msg(mid, author, ts, content):
    return {
        "id": mid,
        "role": "user",
        "sender_node_id": author,
        "sender_name": author[:12],
        "content": content,
        "timestamp": ts,
        "content_hash": f"hash-of-{mid}",
    }


def _store(base: Path, name: str, messages):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "history.json").write_text(
        json.dumps({"conversation_id": GROUP, "version": 1, "messages": messages}),
        encoding="utf-8",
    )
    return d


# --- the comparison itself --------------------------------------------------

def test_identical_histories_report_no_difference_whatever_the_order():
    a = [_msg("1", ALICE, "2026-08-01T00:00:00+00:00", "one"),
         _msg("2", BOB, "2026-08-02T00:00:00+00:00", "two")]
    b = list(reversed(a))

    assert authors_that_differ_between(digest_for(a), digest_for(b)) == []


def test_a_missing_message_names_its_author_and_only_its_author():
    full = [_msg("1", ALICE, "2026-08-01T00:00:00+00:00", "one"),
            _msg("2", BOB, "2026-08-02T00:00:00+00:00", "two")]
    partial = [full[0]]

    assert authors_that_differ_between(digest_for(full), digest_for(partial)) == [BOB]


def test_an_empty_side_differs_about_every_author_present():
    full = [_msg("1", ALICE, "2026-08-01T00:00:00+00:00", "one"),
            _msg("2", BOB, "2026-08-02T00:00:00+00:00", "two")]

    assert authors_that_differ_between(digest_for(full), digest_for([])) == sorted([ALICE, BOB])


def test_a_digest_is_the_same_whether_it_comes_from_a_list_or_a_monitor():
    """The free function and the method must not drift apart."""
    messages = [_msg("1", ALICE, "2026-08-01T00:00:00+00:00", "one")]
    monitor = ConversationMonitor.__new__(ConversationMonitor)
    monitor.message_history = messages

    assert monitor.history_digest() == digest_for(messages)


# --- advertising without a monitor ------------------------------------------

def test_a_node_can_advertise_a_digest_without_loading_the_conversation(tmp_path, monkeypatch):
    """The case that used to fall back to chain tips: no monitor in memory."""
    home = tmp_path
    _store(home / ".dpc" / "conversations", GROUP,
           [_msg("1", ALICE, "2026-08-01T00:00:00+00:00", "one")])
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    messages = ConversationMonitor.peek_group_messages(GROUP)
    advertised = digest_for(messages)

    assert len(messages) == 1
    assert advertised["digest"] != "sha256:empty"
    assert ALICE in advertised["authors"]


def test_the_advertised_history_is_the_one_the_monitor_writes(tmp_path, monkeypatch):
    """While a group was split, the peek resolver preferred the bare folder and
    the monitor preferred the slugged one, so a node advertised a history it
    was not keeping."""
    conversations = tmp_path / ".dpc" / "conversations"
    _store(conversations, GROUP, [_msg("1", ALICE, "2026-08-01T00:00:00+00:00", "the old half")])
    _store(conversations, f"{GROUP}-work",
           [_msg("2", BOB, "2026-08-23T00:00:00+00:00", "the new half, and a longer one at that")])
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    peeked = ConversationMonitor.peek_group_messages(GROUP)
    store = cp.resolve_store_dir(conversations, GROUP, "work")
    on_disk = json.loads((store / "history.json").read_text(encoding="utf-8"))["messages"]

    assert peeked == on_disk, "peek and resolve must answer with the same folder"


def test_a_group_with_no_folder_advertises_nothing_rather_than_failing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert ConversationMonitor.peek_group_messages(GROUP) == []
    assert ConversationMonitor.peek_group_history_stats(GROUP) == (0, "sha256:empty")
    assert digest_for([]) == {"authors": {}, "digest": "sha256:empty"}


# --- telling peers after a local repair -------------------------------------

class _Peer:
    def __init__(self):
        self.sent = []

    async def send_message_to_peer(self, node_id, message):
        self.sent.append((node_id, message))


class _P2P:
    def __init__(self, node_id, connected):
        self.node_id = node_id
        self.peers = {p: object() for p in connected}
        self.sent = []

    async def send_message_to_peer(self, node_id, message):
        self.sent.append((node_id, message))


class _Group:
    def __init__(self, members):
        self.members = members


class _GroupManager:
    def __init__(self, group):
        self._group = group

    def get_group(self, group_id):
        return self._group


def _service_with(monitor, connected, members):
    from dpc_client_core.knowledge_service import KnowledgeService

    service = KnowledgeService.__new__(KnowledgeService)
    service.conversation_monitors = {GROUP: monitor}
    service.p2p_manager = _P2P(ALICE, connected)
    service.group_manager = _GroupManager(_Group(members))
    return service


def _monitor_with(messages, consolidation):
    monitor = ConversationMonitor.__new__(ConversationMonitor)
    monitor.conversation_id = GROUP
    monitor.message_history = messages
    monitor.last_consolidation = consolidation
    return monitor


@pytest.mark.asyncio
async def test_a_repaired_node_tells_the_members_that_are_connected():
    import asyncio

    messages = [_msg("1", ALICE, "2026-08-01T00:00:00+00:00", "one"),
                _msg("2", BOB, "2026-08-02T00:00:00+00:00", "two")]
    monitor = _monitor_with(messages, {"merged": 1, "messages_added": 58})
    service = _service_with(monitor, connected=[BOB], members=[ALICE, BOB])

    service._announce_history_after_repair(GROUP)
    await asyncio.sleep(0)

    assert len(service.p2p_manager.sent) == 1
    peer_id, message = service.p2p_manager.sent[0]
    assert peer_id == BOB
    assert message["command"] == "GROUP_HISTORY_STATUS"
    assert message["payload"]["message_count"] == 2
    assert message["payload"]["history_digest"] == digest_for(messages)


@pytest.mark.asyncio
async def test_a_node_with_nothing_to_repair_stays_silent():
    import asyncio

    monitor = _monitor_with([], {"merged": 0, "messages_added": 0})
    service = _service_with(monitor, connected=[BOB], members=[ALICE, BOB])

    service._announce_history_after_repair(GROUP)
    await asyncio.sleep(0)

    assert service.p2p_manager.sent == []


@pytest.mark.asyncio
async def test_a_member_that_is_not_connected_is_not_written_to():
    import asyncio

    monitor = _monitor_with([_msg("1", ALICE, "2026-08-01T00:00:00+00:00", "one")],
                            {"merged": 1, "messages_added": 5})
    service = _service_with(monitor, connected=[], members=[ALICE, BOB])

    service._announce_history_after_repair(GROUP)
    await asyncio.sleep(0)

    assert service.p2p_manager.sent == []


# --- the status handler, where the fallback used to happen ------------------

class _Service:
    """Only what GroupHistoryStatusHandler touches."""

    def __init__(self, p2p, monitors, members=None):
        self.p2p_manager = p2p
        self.conversation_monitors = monitors
        # Both doors now ask the roster first; these tests are about the delta,
        # so the sender is a member and the question is what travels.
        self.group_manager = _GroupManager(_Group(list(members or [ALICE, BOB])))

    def _get_or_create_conversation_monitor(self, conversation_id):
        return self.conversation_monitors.get(conversation_id)


@pytest.mark.asyncio
async def test_the_status_reply_carries_a_digest_even_with_no_monitor_loaded(tmp_path, monkeypatch):
    """The load-bearing one for automatic convergence: with no monitor and no
    digest, both sides used to compare chain tips — which never match between
    two honest nodes — and every connection either cried divergence or shipped
    the whole file."""
    from dpc_client_core.message_handlers.group_handler import GroupHistoryStatusHandler

    ours = [_msg("1", ALICE, "2026-08-01T00:00:00+00:00", "one")]
    _store(tmp_path / ".dpc" / "conversations", GROUP, ours)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    p2p = _P2P(ALICE, connected=[BOB])
    handler = GroupHistoryStatusHandler(_Service(p2p, {}))

    await handler.handle(BOB, {
        "group_id": GROUP,
        "history_hash": "sha256:whatever",
        "message_count": 1,
        "history_digest": digest_for(ours),
        "is_reply": False,
    })

    replies = [m for _, m in p2p.sent if m["command"] == "GROUP_HISTORY_STATUS"]
    assert replies, "the initiating status is always answered"
    assert replies[0]["payload"]["history_digest"] == digest_for(ours)
    requests = [m for _, m in p2p.sent if m["command"] == "GROUP_HISTORY_REQUEST"]
    assert requests == [], "identical histories must not trigger a transfer"


@pytest.mark.asyncio
async def test_a_divergent_peer_is_asked_only_for_the_authors_that_differ(tmp_path, monkeypatch):
    from dpc_client_core.message_handlers.group_handler import GroupHistoryStatusHandler

    ours = [_msg("1", ALICE, "2026-08-01T00:00:00+00:00", "one")]
    theirs = ours + [_msg("2", BOB, "2026-08-02T00:00:00+00:00", "two")]
    _store(tmp_path / ".dpc" / "conversations", GROUP, ours)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    p2p = _P2P(ALICE, connected=[BOB])
    handler = GroupHistoryStatusHandler(_Service(p2p, {}))

    await handler.handle(BOB, {
        "group_id": GROUP,
        "history_hash": "sha256:whatever",
        "message_count": 2,
        "history_digest": digest_for(theirs),
        "is_reply": True,
    })

    requests = [m for _, m in p2p.sent if m["command"] == "GROUP_HISTORY_REQUEST"]
    assert len(requests) == 1
    assert requests[0]["payload"]["authors"] == [BOB]


# --- the whole exchange, both real handlers, two real monitors --------------

class _LocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, data):
        self.events.append((name, data))


class _Node:
    """One side of the pair: a real monitor, real handlers, a recorded wire."""

    def __init__(self, node_id, home, messages):
        _store(home / ".dpc" / "conversations", GROUP, messages)
        self.node_id = node_id
        self.home = home
        self.p2p_manager = _P2P(node_id, connected=[])
        self.conversation_monitors = {}
        self.outbox = self.p2p_manager.sent
        self.local_api = _LocalApi()
        # Both nodes are in the group; this file is about convergence, and the
        # roster gate that now guards these doors has its own tests.
        self.group_manager = _GroupManager(_Group([ALICE, BOB]))

    def _get_or_create_conversation_monitor(self, conversation_id):
        if conversation_id not in self.conversation_monitors:
            monitor = ConversationMonitor(
                conversation_id=conversation_id,
                participants=[],
                llm_manager=None,
            )
            monitor.load_history()
            self.conversation_monitors[conversation_id] = monitor
        return self.conversation_monitors[conversation_id]

    def messages(self):
        return self._get_or_create_conversation_monitor(GROUP).message_history


@pytest.mark.asyncio
async def test_the_side_that_is_behind_ends_up_with_the_messages_it_lacked(tmp_path, monkeypatch):
    """STATUS → author diff → REQUEST → RESPONSE → merge, no reconnect involved."""
    from dpc_client_core.message_handlers.group_handler import (
        GroupHistoryStatusHandler,
        GroupHistoryRequestHandler,
        GroupHistoryResponseHandler,
    )

    shared = _msg("1", ALICE, "2026-08-01T00:00:00+00:00", "both have this")
    only_a = _msg("2", BOB, "2026-08-02T00:00:00+00:00", "only A has this")

    a_home = tmp_path / "a"
    b_home = tmp_path / "b"

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: a_home))
    a = _Node(ALICE, a_home, [shared, only_a])
    a_status = {
        "group_id": GROUP,
        "history_hash": "sha256:a",
        "message_count": 2,
        "history_digest": digest_for([shared, only_a]),
        "is_reply": False,
    }

    # B receives A's status. Everything B does happens with B's home in place.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: b_home))
    b = _Node(BOB, b_home, [shared])
    # Deliberately no monitor on B yet: on connect that is the usual state, and
    # it is the state in which the digest used to be dropped.
    assert b.conversation_monitors == {}

    await GroupHistoryStatusHandler(b).handle(ALICE, a_status)
    requests = [m for _, m in b.outbox if m["command"] == "GROUP_HISTORY_REQUEST"]
    assert len(requests) == 1, "B noticed it is behind"
    assert requests[0]["payload"]["authors"] == [BOB], "and asked only about the author that differs"

    # A answers the request.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: a_home))
    await GroupHistoryRequestHandler(a).handle(BOB, requests[0]["payload"])
    responses = [m for _, m in a.outbox if m["command"] == "GROUP_HISTORY_RESPONSE"]
    assert len(responses) == 1
    assert [m["id"] for m in responses[0]["payload"]["history"]] == ["2"], "delta, not the whole file"

    # B merges it.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: b_home))
    await GroupHistoryResponseHandler(b).handle(ALICE, responses[0]["payload"])

    assert sorted(m["id"] for m in b.messages()) == ["1", "2"]
    on_disk = json.loads(
        (cp.resolve_store_dir(b_home / ".dpc" / "conversations", GROUP) / "history.json")
        .read_text(encoding="utf-8")
    )["messages"]
    assert sorted(m["id"] for m in on_disk) == ["1", "2"], "and it survived the restart"


# --- order, not just membership ---------------------------------------------

def test_a_history_handed_over_after_a_gap_ends_up_in_time_order():
    """A merge appends, so a recovered older block used to land after the recent
    one and the file became two chronological runs stuck together. Measured on
    the live pair: same messages, same count, order broken at index 31 and 61."""
    from dpc_client_core.conversation_monitor import ConversationMonitor

    monitor = ConversationMonitor.__new__(ConversationMonitor)
    monitor.conversation_id = GROUP
    monitor.message_history = [
        _msg("new1", ALICE, "2026-08-23T10:00:00+00:00", "recent one"),
        _msg("new2", ALICE, "2026-08-24T10:00:00+00:00", "recent two"),
        _msg("old1", BOB, "2026-08-04T10:00:00+00:00", "ancient one"),
        _msg("old2", BOB, "2026-08-11T10:00:00+00:00", "ancient two"),
    ]

    moved = monitor.restore_chronological_order()

    assert moved is True
    assert [m["id"] for m in monitor.message_history] == ["old1", "old2", "new1", "new2"]
    prev = "genesis"
    for i, m in enumerate(monitor.message_history):
        assert m["msg_index"] == i + 1
        assert m["chain_hash"] == chain_hash_for(m, prev)
        prev = m["chain_hash"]


def test_a_history_already_in_order_is_left_alone():
    from dpc_client_core.conversation_monitor import ConversationMonitor

    monitor = ConversationMonitor.__new__(ConversationMonitor)
    monitor.conversation_id = GROUP
    original = [
        _msg("a", ALICE, "2026-08-01T10:00:00+00:00", "one"),
        _msg("b", BOB, "2026-08-02T10:00:00+00:00", "two"),
    ]
    monitor.message_history = list(original)

    assert monitor.restore_chronological_order() is False
    assert monitor.message_history == original, "no rechain, no rewrite, no churn"


@pytest.mark.asyncio
async def test_the_merge_path_itself_restores_order(tmp_path, monkeypatch):
    """Not the helper in isolation — `merge_history` has to call it. Without
    this the helper can be correct and never reached, which is how a guard
    passes vacuously."""
    from dpc_client_core.message_handlers.group_handler import GroupHistoryResponseHandler

    recent = _msg("recent", ALICE, "2026-08-24T10:00:00+00:00", "what B already had")
    ancient = _msg("ancient", BOB, "2026-08-04T10:00:00+00:00", "handed over after a gap")

    home = tmp_path / "b"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    b = _Node(BOB, home, [recent])
    b._get_or_create_conversation_monitor(GROUP)

    await GroupHistoryResponseHandler(b).handle(
        ALICE, {"group_id": GROUP, "history": [ancient]}
    )

    stored = json.loads(
        (cp.resolve_store_dir(home / ".dpc" / "conversations", GROUP) / "history.json")
        .read_text(encoding="utf-8")
    )["messages"]
    assert [m["id"] for m in stored] == ["ancient", "recent"], (
        "the older message arrived second and must not stay at the end"
    )
