"""The current message reaches the model once: as the USER QUERY line.

`execute_ai_query` records the user's message in the conversation monitor and
builds the prompt from the history plus that message. Passing the history taken
after the record put the message in the prompt twice -- as the last history entry
and again as USER QUERY -- and spent the context window on it twice (an 11.7K-token
message became 23.5K). The prompt is driven for real from `execute_ai_query`; only
the monitor and the model call are doubles, reusing the harness of the peer-routing
test.
"""

import pytest

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.managers.prompt_manager import PromptManager
from tests.test_a_peer_routed_query_carries_only_the_context_its_node_rules_allow import (
    _FakeService,
    _Instructions,
)

MARKER = "UNIQUE-CURRENT-MESSAGE-7f3a"


@pytest.fixture
def service(tmp_path):
    rules = tmp_path / "privacy_rules.json"
    rules.write_text("{}")
    return _FakeService(ContextFirewall(rules), None, None)


async def _prompt_for(service, text, earlier=()):
    for role, content in earlier:
        service.monitor.add_message(role, content)
    await service.execute_ai_query(
        command_id="c1",
        prompt=text,
        context_ids=None,
        compute_host=None,
        provider=None,
        include_context=False,
        conversation_id="local_ai",
    )
    assert len(service.sent) == 1
    return service.sent[0][2]


@pytest.mark.asyncio
async def test_the_first_message_of_a_chat_appears_once(service):
    prompt = await _prompt_for(service, MARKER)

    assert prompt.count(MARKER) == 1
    assert prompt.endswith(f"USER QUERY: {MARKER}")
    assert "CONVERSATION HISTORY" not in prompt


@pytest.mark.asyncio
async def test_a_message_after_a_history_appears_once_and_the_history_is_kept(service):
    prompt = await _prompt_for(
        service, MARKER, earlier=[("user", "earlier question"), ("assistant", "earlier answer")]
    )

    assert prompt.count(MARKER) == 1
    assert prompt.endswith(f"USER QUERY: {MARKER}")
    assert "USER: earlier question" in prompt
    assert "ASSISTANT: earlier answer" in prompt


@pytest.mark.asyncio
async def test_the_same_text_sent_twice_keeps_the_earlier_copy_as_history(service):
    prompt = await _prompt_for(
        service, MARKER, earlier=[("user", MARKER), ("assistant", "answer")]
    )

    # one copy is the real earlier message, one is the current USER QUERY
    assert prompt.count(MARKER) == 2
    assert f"USER: {MARKER}" in prompt
    assert prompt.endswith(f"USER QUERY: {MARKER}")


@pytest.mark.asyncio
async def test_the_message_is_still_recorded_in_the_conversation(service):
    await _prompt_for(service, MARKER, earlier=[("user", "earlier question")])

    assert [m["content"] for m in service.monitor.history] == ["earlier question", MARKER]


def test_assemble_prompt_writes_what_it_is_given():
    """assemble_prompt itself drops nothing: the caller decides what is history."""
    history = [{"role": "user", "content": "a"}, {"role": "user", "content": "a"}]
    prompt = PromptManager(_Instructions(), {}).assemble_prompt(
        query="a", message_history=history, include_context=False
    )

    assert prompt.count("USER: a") == 2
    assert prompt.endswith("USER QUERY: a")
