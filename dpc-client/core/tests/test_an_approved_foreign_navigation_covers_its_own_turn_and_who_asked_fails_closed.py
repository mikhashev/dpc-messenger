"""Follow-up 14, area 1 (Zcode #176 on follow-up 13 A): who asked, and what it covers.

- One approved navigation on a foreign turn marked the browser session as foreign for its
  life, so a later foreign turn read the owner's logged-in page with no card. The mark is now
  per navigation and names its turn; the owner's next opening resets it. The end-of-turn close
  tries once more and logs a failure.
- At the owner's doors, an asker naming a node counted as the owner when this node's id was
  not known. It now fails closed.
- The local wake line read a stored `is_owner`; it now computes the verdict from the node.
- A schedule from a turn with no task id dropped its wake address; it now answers in the
  turn's conversation, or is refused when the turn has neither.
"""
import json
import logging
import types
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent import loop as agent_loop
from dpc_client_core.dpc_agent.agent import (
    ASKER_HUMAN, OTHER_NODE_TURN, Asker, DpcAgent,
)
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
    def __init__(self):
        self._etld1s = {"example.com"}
        self._anonymous = False
        self._page = object()
        self._headed = True
        self.closed = 0
        self.url = "https://example.com/inbox"  # where the page is; every foreign read asks


@pytest.fixture
def sessions(monkeypatch):
    calls = []

    async def _run(session, method, *args, **kwargs):
        calls.append(method)
        if method == "close":
            session.closed += 1
            return None
        if method == "navigate":
            session.url = args[0]
            return "navigated tree"
        if method == "page_url":
            return session.url
        if method == "extract":
            return "<html>owner's inbox</html>"
        return None

    async def _plain(tree, ctx, agent_id):
        return tree

    monkeypatch.setattr(browser, "_run_in_session", _run)
    monkeypatch.setattr(browser, "_maybe_summarize_snapshot", _plain)
    saved = dict(browser._active_browser_sessions)
    browser._active_browser_sessions.clear()
    yield calls
    browser._active_browser_sessions.clear()
    browser._active_browser_sessions.update(saved)


# -- the foreign mark is per navigation ------------------------------------------------------

@pytest.mark.asyncio
async def test_a_page_a_foreign_turn_opened_is_read_on_that_turn(tmp_path, sessions):
    browser._active_browser_sessions["agent_ark"] = _Session()
    turn = _ctx(tmp_path, FRIEND)
    assert (await browser.browser_navigate(turn, "https://example.com/a")).startswith("Navigated")
    assert "owner's inbox" in await browser.browser_extract(turn)


@pytest.mark.asyncio
async def test_a_later_foreign_turn_cannot_read_a_page_an_earlier_one_was_approved_for(
        tmp_path, sessions):
    browser._active_browser_sessions["agent_ark"] = _Session()
    await browser.browser_navigate(_ctx(tmp_path, FRIEND), "https://example.com/a")
    later = _ctx(tmp_path, FRIEND)
    result = await browser.browser_extract(later)
    assert result.startswith("⚠️") and "earlier turn" in result
    assert "extract" not in sessions


@pytest.mark.asyncio
async def test_the_owners_next_navigation_resets_the_foreign_mark(tmp_path, sessions):
    session = _Session()
    browser._active_browser_sessions["agent_ark"] = session
    foreign = _ctx(tmp_path, FRIEND)
    await browser.browser_navigate(foreign, "https://example.com/a")
    await browser.browser_navigate(_ctx(tmp_path, MIKE), "https://example.com/inbox")
    result = await browser.browser_extract(foreign)
    assert result.startswith("⚠️") and "owner's own turn" in result


@pytest.mark.asyncio
async def test_a_navigation_whose_asker_cannot_be_told_counts_as_the_owners(
        tmp_path, sessions, monkeypatch):
    from dpc_client_core.dpc_agent import owner_approval

    def _boom(ctx):
        raise RuntimeError("no asker")

    session = _Session()
    session._bound_on_owner_turn = False
    browser._active_browser_sessions["agent_ark"] = session
    monkeypatch.setattr(owner_approval, "asker_is_owner", _boom)
    browser._note_turn_of_binding(session, _ctx(tmp_path, FRIEND))
    assert session._bound_on_owner_turn is True and session._bound_turn_token is None


# -- the end-of-turn close tries again ----------------------------------------------------------

def test_the_end_of_turn_close_tries_once_more_after_a_failure(tmp_path, sessions, monkeypatch):
    session = _Session()
    browser._active_browser_sessions["agent_ark"] = session
    attempts = []

    async def _flaky(agent_id, **kwargs):
        attempts.append(agent_id)
        if len(attempts) == 1:
            raise RuntimeError("browser thread busy")
        browser._active_browser_sessions.pop(agent_id, None)
        return 1

    monkeypatch.setattr(browser, "close_browser_sessions_of", _flaky)
    thread = agent_loop._end_foreign_turn(_ctx(tmp_path, FRIEND), "agent_ark")
    thread.join(5)
    assert len(attempts) == 2 and "agent_ark" not in browser._active_browser_sessions


def test_a_close_that_fails_twice_is_logged(tmp_path, sessions, monkeypatch, caplog):
    browser._active_browser_sessions["agent_ark"] = _Session()
    attempts = []

    async def _stuck(agent_id, **kwargs):
        attempts.append(agent_id)
        return 0

    monkeypatch.setattr(browser, "close_browser_sessions_of", _stuck)
    with caplog.at_level(logging.WARNING):
        thread = agent_loop._end_foreign_turn(_ctx(tmp_path, FRIEND), "agent_ark")
        thread.join(5)
    assert len(attempts) == 2
    assert any("could not be closed" in r.getMessage() for r in caplog.records)


# -- the owner's door with no node id --------------------------------------------------------

def _agent(this_node="dpc-node-this", own_group=None):
    agent = DpcAgent.__new__(DpcAgent)
    gm = types.SimpleNamespace(node_id=this_node)
    agent._service = types.SimpleNamespace(group_manager=gm, p2p_manager=gm)
    agent._firewall = types.SimpleNamespace(
        node_groups={"mine": list(own_group or [])},
        get_agent_profile_settings=lambda profile: {"memory": {"own_node_group": "mine"}},
    )
    agent._firewall_profile = "agent_ark"
    return agent


def test_without_a_node_id_a_named_foreign_node_at_the_owners_door_is_not_the_owner():
    agent = _agent(this_node=None)
    asker = Asker(ASKER_HUMAN, "Miron", "dpc-node-friend", is_owner=True, verified=True)
    assert agent.resolve_asker(asker, "ui").is_owner is False


def test_without_a_node_id_the_owners_door_with_no_named_node_is_still_the_owner():
    agent = _agent(this_node=None)
    assert agent.resolve_asker(None, "ui").is_owner is True


def test_without_a_node_id_an_own_node_at_the_owners_door_is_the_owner():
    agent = _agent(this_node=None, own_group=["dpc-node-laptop"])
    asker = Asker(ASKER_HUMAN, "Mike", "dpc-node-laptop", is_owner=False, verified=True)
    assert agent.resolve_asker(asker, "ui").is_owner is True


# -- the wake line computes its verdict -----------------------------------------------------

def test_the_wake_line_does_not_trust_a_stored_owner_flag():
    from dpc_client_core.service import local_wake_sender
    forged = Asker(ASKER_HUMAN, "Miron", "dpc-node-friend", is_owner=True, verified=False,
                   via=("Ark",))
    words = local_wake_sender("Ark", True, forged, "dpc-node-this")
    assert "signature not verified" in words and "this node" not in words.split("asked by")[1]


def test_the_wake_line_says_this_node_only_for_a_verified_asker_on_this_node():
    from dpc_client_core.service import local_wake_sender
    mine = Asker(ASKER_HUMAN, "Mike", "dpc-node-this", is_owner=False, verified=True,
                 via=("Ark",))
    assert "signature this node" in local_wake_sender("Ark", True, mine, "dpc-node-this")
    other = Asker(ASKER_HUMAN, "Miron", "dpc-node-friend", is_owner=True, verified=True,
                  via=("Ark",))
    assert "signature verified" in local_wake_sender("Ark", True, other, "dpc-node-this")


# -- a schedule's wake address ---------------------------------------------------------------

def _schedule_ctx(current_task_id=None, conversation_id=None):
    scheduled = []

    def _schedule(task_type, data, priority, delay_seconds):
        task = SimpleNamespace(id=f"task-{len(scheduled)}", task_type=task_type, data=data,
                               status="pending", scheduled_at=None)
        scheduled.append(task)
        return task

    agent = SimpleNamespace(queue=SimpleNamespace(_queue=[], is_running=lambda: True),
                            schedule_task=_schedule, _task_handlers={},
                            _firewall_profile=None, display_name="Ark")
    return SimpleNamespace(_agent=agent, current_task_id=current_task_id,
                           conversation_id=conversation_id, check_back_depth=0,
                           reply_telegram_chat_id=None, message_source="ui", asker=MIKE,
                           turn_class="owner", firewall=None, _scheduled=scheduled)


def test_a_turn_without_a_task_id_schedules_into_its_conversation():
    ctx = _schedule_ctx(conversation_id="local_ai_chat_ark")
    result = tools_core.schedule_task(ctx, "chat", json.dumps({"text": "hi"}), 60)
    assert result.startswith("✓"), result
    assert ctx._scheduled[0].data["_reply_conversation_id"] == "local_ai_chat_ark"


def test_a_turn_with_no_chat_behind_it_schedules_nothing():
    ctx = _schedule_ctx()
    result = tools_core.schedule_task(ctx, "chat", json.dumps({"text": "hi"}), 60)
    assert result == tools_core.NO_REPLY_ADDRESS_REFUSAL
    assert ctx._scheduled == []
