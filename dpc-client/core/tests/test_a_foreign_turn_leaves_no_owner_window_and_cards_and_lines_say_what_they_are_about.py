"""Follow-up 13 (Zcode #154, #143): what a foreign turn leaves behind, and what a card or a
line says.

- A browser window kept open under the owner's login lived until Kill, and the read-only
  browser tools were not flagged, so a foreign asker could read it. A turn someone other than
  the owner asked for closes the agent's interactive browser at its end, and snapshot/extract
  refuse a logged-in page last opened on the owner's own turn.
- The owner's card for a peer's unvoted knowledge commit showed topic and summary only; it now
  shows the first lines of the entries, bounded.
- The local-path wake line names the sender, its node, human or agent, and the verdict.
"""
import asyncio
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent import loop as agent_loop
from dpc_client_core.dpc_agent.agent import ASKER_HUMAN, OTHER_NODE_TURN, Asker
from dpc_client_core.dpc_agent.tools import browser
from dpc_client_core.dpc_agent.tools import core as tools_core
from dpc_client_core.dpc_agent.tools.registry import ToolContext

FRIEND = Asker(ASKER_HUMAN, "Miron", "dpc-node-friend", is_owner=False, verified=True)
MIKE = Asker(ASKER_HUMAN, "Mike", "dpc-node-this", is_owner=True, verified=True)


def _ctx(tmp_path, asker):
    root = tmp_path / "agent_ark"
    root.mkdir(exist_ok=True)
    if asker.is_owner:
        return ToolContext(agent_root=root, message_source="ui", turn_class="owner",
                           open_layers=frozenset({"L5", "L6", "EXT"}), asker=asker)
    return ToolContext(agent_root=root, message_source="group", turn_class=OTHER_NODE_TURN,
                       open_layers=frozenset(), asker=asker)


class _Session:
    def __init__(self, etld1s=("example.com",), anonymous=False):
        self._etld1s = set(etld1s)
        self._anonymous = anonymous
        self._page = object()
        self._headed = True
        self.closed = 0


@pytest.fixture
def sessions(monkeypatch):
    calls = []

    async def _run(session, method, *args, **kwargs):
        calls.append(method)
        if method == "close":
            session.closed += 1
            return None
        if method == "a11y_snapshot":
            return "page tree", {}
        if method == "extract":
            return "<html>owner's inbox</html>"
        if method == "detect_scrollable_containers":
            return []
        return None

    monkeypatch.setattr(browser, "_run_in_session", _run)
    saved = dict(browser._active_browser_sessions)
    browser._active_browser_sessions.clear()
    yield calls
    browser._active_browser_sessions.clear()
    browser._active_browser_sessions.update(saved)


# -- the window at the end of a foreign turn ---------------------------------------------------

def test_a_foreign_turn_closes_the_agents_browser_window_at_its_end(tmp_path, sessions):
    session = _Session()
    browser._active_browser_sessions["agent_ark"] = session
    thread = agent_loop._end_foreign_turn(_ctx(tmp_path, FRIEND), "agent_ark")
    assert thread is not None
    thread.join(5)
    assert session.closed == 1 and "agent_ark" not in browser._active_browser_sessions


def test_the_owners_turn_leaves_the_window_open(tmp_path, sessions):
    session = _Session()
    browser._active_browser_sessions["agent_ark"] = session
    assert agent_loop._end_foreign_turn(_ctx(tmp_path, MIKE), "agent_ark") is None
    assert session.closed == 0 and browser._active_browser_sessions["agent_ark"] is session


def test_a_foreign_turn_with_no_window_starts_nothing(tmp_path, sessions):
    assert agent_loop._end_foreign_turn(_ctx(tmp_path, FRIEND), "agent_ark") is None


def test_the_loop_ends_every_turn_through_the_foreign_turn_check():
    import inspect
    source = inspect.getsource(agent_loop.run_llm_loop)
    assert "_end_foreign_turn(" in source


# -- reading a logged-in page opened on the owner's turn -------------------------------------

@pytest.mark.asyncio
async def test_a_foreign_asker_cannot_read_a_logged_in_page_left_by_the_owners_turn(tmp_path, sessions):
    session = _Session()
    session._bound_on_owner_turn = True
    browser._active_browser_sessions["agent_ark"] = session
    ctx = _ctx(tmp_path, FRIEND)
    for result in (await browser.browser_snapshot(ctx), await browser.browser_extract(ctx),
                   await browser.browser_screenshot(ctx)):
        assert result.startswith("⚠️") and "logged in as the owner" in result
    assert "a11y_snapshot" not in sessions and "extract" not in sessions
    assert "screenshot" not in sessions


@pytest.mark.asyncio
async def test_a_logged_in_page_with_no_record_of_its_turn_is_treated_as_the_owners(tmp_path, sessions):
    browser._active_browser_sessions["agent_ark"] = _Session()
    assert (await browser.browser_extract(_ctx(tmp_path, FRIEND))).startswith("⚠️")


@pytest.mark.asyncio
async def test_a_page_the_owner_approved_for_this_foreign_turn_can_be_read(tmp_path, sessions):
    session = _Session()
    session._bound_on_owner_turn = False
    browser._active_browser_sessions["agent_ark"] = session
    assert "owner's inbox" in await browser.browser_extract(_ctx(tmp_path, FRIEND))


@pytest.mark.asyncio
async def test_a_page_without_the_owners_login_can_be_read_by_anyone(tmp_path, sessions):
    browser._active_browser_sessions["agent_ark"] = _Session(etld1s=())
    assert "owner's inbox" in await browser.browser_extract(_ctx(tmp_path, FRIEND))


@pytest.mark.asyncio
async def test_the_owner_reads_his_own_logged_in_page(tmp_path, sessions, monkeypatch):
    async def _plain(tree, ctx, agent_id):
        return tree

    monkeypatch.setattr(browser, "_maybe_summarize_snapshot", _plain)
    session = _Session()
    session._bound_on_owner_turn = True
    browser._active_browser_sessions["agent_ark"] = session
    assert "page tree" in await browser.browser_snapshot(_ctx(tmp_path, MIKE))


# -- the knowledge card shows the entries ------------------------------------------------------

ME = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
GROUP = "group-0a52389f2bb6"


class _LocalApi:
    has_clients = True

    async def broadcast_event(self, name, payload):
        pass


@pytest.mark.asyncio
async def test_the_owners_card_for_a_peer_commit_shows_the_first_lines_of_its_entries(monkeypatch):
    from dpc_client_core.message_handlers.knowledge_handler import ApplyKnowledgeCommitHandler
    from dpc_protocol.knowledge_commit import CommitProvenance, KnowledgeCommit
    from dpc_protocol.pcm_core import KnowledgeEntry, KnowledgeSource

    monkeypatch.setattr(KnowledgeCommit, "verify_provenance",
                        lambda self, peers_dir=None: CommitProvenance("verified", "test"))
    monkeypatch.setattr(tools_core, "_SCHEDULE_APPROVAL_TTL_SECONDS", 0.3)
    cards = []

    async def announce(**kwargs):
        cards.append(kwargs)
        tools_core.resolve_schedule_approval(kwargs["request_id"], False)

    async def closed(**kwargs):
        pass

    service = SimpleNamespace(
        consensus_manager=SimpleNamespace(_apply_commit=AsyncMock(return_value=True),
                                          peer_proposal_roster=lambda c, s: [ME, PEER]),
        knowledge_service=SimpleNamespace(judged_proposal=lambda pid: False),
        pcm_core=SimpleNamespace(load_context=lambda: SimpleNamespace(commit_history=[])),
        pending_certificate_requests=set(),
        p2p_manager=SimpleNamespace(node_id=ME, peers={PEER: object()},
                                    send_message_to_peer=AsyncMock(), get_display_name=lambda: "Mike"),
        local_api=_LocalApi(),
        announce_schedule_approval_request=announce,
        announce_schedule_approval_closed=closed,
        _get_agent_telegram_bridge=lambda agent_id: None,
        _peer_name=lambda node: "Miron",
    )
    entries = [KnowledgeEntry(content=f"fact {i}: the card is 12 GB\nsecond line not shown",
                              tags=["gpu"], source=KnowledgeSource(type="ai_summary"))
               for i in range(8)]
    entries.append(KnowledgeEntry(content="x" * 2000, source=KnowledgeSource(type="ai_summary")))
    commit = KnowledgeCommit(topic="hardware", summary="12 GB", conversation_id=GROUP,
                             entries=entries, participants=[ME, PEER], approved_by=[ME, PEER],
                             proposed_by=PEER, timestamp="2026-11-25T10:00:00+00:00")
    commit.compute_hash()
    await ApplyKnowledgeCommitHandler(service).handle(PEER, commit.to_dict())
    details = cards[0]["details"]
    assert "hardware" in details and "fact 0: the card is 12 GB" in details
    assert "second line not shown" not in details
    assert "more" in details, "entries past the bound are counted, not listed"
    assert len(details) <= 600


# -- the local wake line names the sender ------------------------------------------------------

def test_the_local_wake_line_names_a_human_sender_on_this_node():
    from dpc_client_core.service import local_wake_sender
    words = local_wake_sender("Mike", False, None, "dpc-node-this")
    assert "Mike" in words and "human" in words and "dpc-node-this"[:16] in words
    assert "written on this node" in words


def test_the_local_wake_line_names_an_agent_and_the_relayed_asker_with_its_verdict():
    from dpc_client_core.service import local_wake_sender
    relayed = Asker(ASKER_HUMAN, "Miron", "dpc-node-friend", is_owner=False, verified=True,
                    via=("Ark",))
    words = local_wake_sender("Ark", True, relayed, "dpc-node-this")
    assert "Ark" in words and "agent" in words
    assert "Miron" in words and "verified" in words


def test_the_local_wake_line_carries_the_sender_words():
    import inspect
    from dpc_client_core.service import CoreService
    source = inspect.getsource(CoreService._handle_group_agent_mentions)
    assert "local_wake_sender(" in source
