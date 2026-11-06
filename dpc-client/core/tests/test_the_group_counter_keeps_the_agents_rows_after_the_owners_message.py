"""The group counter is the same after the owner's message as after an agent's answer.

Mike, 2026-11-06: after an agent answered, the DPC Project header showed DIALOG,
TOTAL with the agent, AGENT CTX and LOADED; after his own message it fell back to
"75,114 / 250,000 tokens", because `send_group_message` sent the dialogue alone.
Both paths now build the counter in `CoreService._group_token_usage`.
"""
import inspect

from dpc_client_core.service import CoreService


def _service(agents):
    svc = CoreService.__new__(CoreService)
    svc._group_agent_context = {"group-1": agents} if agents else {}
    return svc


def test_with_no_agent_answer_yet_the_counter_is_the_dialogue():
    usage = _service({})._group_token_usage("group-1", 75_114, 250_000)

    assert usage["tokens_used"] == 75_114 and usage["token_limit"] == 250_000
    assert usage["tokens_after_last_response"] == 0
    assert "context_agents" not in usage


def test_after_an_agent_answered_the_counter_carries_the_fullest_agent():
    svc = _service({
        "agent_001": (205_215, 250_000, "2026-11-06T05:40:00+00:00", "Ark"),
        "agent_johnny": (90_000, 250_000, "2026-11-06T05:30:00+00:00", "Johnny"),
    })

    usage = svc._group_token_usage("group-1", 75_669, 250_000)

    assert usage["tokens_used"] == 205_215, "the fullest context, not the dialogue alone"
    assert usage["history_tokens"] == 75_669
    assert usage["tokens_after_last_response"] == 205_215
    assert usage["context_agent"] == "Ark"
    assert {a["name"] for a in usage["context_agents"]} == {"Ark", "Johnny"}


def test_the_owners_message_and_the_model_switch_build_it_in_one_place():
    for method in (CoreService.send_group_message, CoreService.broadcast_group_token_usage):
        source = inspect.getsource(method)
        assert "_group_token_usage(" in source, method.__name__
        assert '"tokens_after_last_response": 0' not in source, method.__name__
