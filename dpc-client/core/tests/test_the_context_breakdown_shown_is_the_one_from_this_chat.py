"""The context breakdown in a chat's runtime block is the one measured in that chat.

It was read from the agent's last request in any conversation, so an agent that
answered a 1:1 chat and then a group was shown the 1:1 prompt's composition as
"your previous request" in the group.
"""

import types

from dpc_client_core.managers.agent_manager import DpcAgentManager


class _Monitor:
    _tokens_after_last_response = 0
    _tokens_after_last_response_at = None
    message_history = []

    def get_token_usage(self):
        return {"token_limit": 204800, "tokens_used": 0}


def _agent(breakdown):
    return types.SimpleNamespace(_last_cap_info={"context_breakdown": breakdown})


def _manager():
    mgr = DpcAgentManager.__new__(DpcAgentManager)
    mgr._agent_monitors = {"group-a": _Monitor(), "chat-b": _Monitor(), "chat-c": _Monitor()}
    mgr.config = {}
    mgr.agent_id = "agent_x"
    mgr.service = None
    mgr._last_used_agent = None
    return mgr


def test_each_chat_gets_its_own_breakdown():
    mgr = _manager()
    group_rows = [{"name": "group prompt", "tokens": 1}]
    chat_rows = [{"name": "1:1 prompt", "tokens": 2}]
    mgr._remember_breakdown("group-a", _agent(group_rows))
    mgr._last_used_agent = _agent(chat_rows)
    mgr._remember_breakdown("chat-b", mgr._last_used_agent)
    assert mgr.get_session_state("group-a")["context_breakdown"] == group_rows
    assert mgr.get_session_state("chat-b")["context_breakdown"] == chat_rows


def test_a_chat_not_yet_served_shows_no_breakdown_from_another():
    mgr = _manager()
    mgr._last_used_agent = _agent([{"name": "1:1 prompt", "tokens": 2}])
    mgr._remember_breakdown("chat-b", mgr._last_used_agent)
    assert mgr.get_session_state("chat-c")["context_breakdown"] is None
