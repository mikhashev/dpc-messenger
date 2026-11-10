"""A tool-only assistant turn (content='' with tool_calls) is rendered by the
text fallback, ahead of its tool result, through both `messages_to_prompt` and
`flatten_messages` (the gateway and peer route)."""

from dpc_client_core.dpc_agent.llm_adapter import messages_to_prompt
from dpc_client_core.llm_manager import flatten_messages

CALL = {"id": "call_1", "type": "function",
        "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}


def _turns(content=""):
    return [
        {"role": "user", "content": "read it"},
        {"role": "assistant", "content": content, "tool_calls": [CALL]},
        {"role": "tool", "tool_call_id": "call_1", "content": "file body"},
    ]


def test_a_tool_only_turn_is_rendered_before_its_result():
    prompt = messages_to_prompt(_turns(""))
    assert '"name": "read_file"' in prompt
    assert '{"path": "a.txt"}' in prompt
    assert prompt.index("```tool_call") < prompt.index("[TOOL RESULT: call_1]")
    assert "[ASSISTANT]\n```tool_call" in prompt


def test_a_whitespace_only_tool_turn_is_rendered_too():
    assert '"name": "read_file"' in messages_to_prompt(_turns("  \n"))


def test_a_turn_with_text_and_calls_renders_as_before():
    prompt = messages_to_prompt(_turns("looking"))
    assert "[ASSISTANT]\nlooking\n```tool_call" in prompt


def test_empty_turns_without_calls_are_still_skipped():
    prompt = messages_to_prompt([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "   "},
    ])
    assert prompt == "[USER]\nhi"


def test_flatten_messages_keeps_the_tool_only_turn_on_the_anthropic_shape():
    turns = [
        {"role": "user", "content": "read it"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "a.txt"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "file body"}]},
    ]
    prompt = flatten_messages(turns, "sys")
    assert '"name": "read_file"' in prompt
    assert prompt.index("```tool_call") < prompt.index("[TOOL RESULT:")
