"""The default AI Chat and a new AI chat start with instruction_set_name "none".

The UI sends that value on every query of such a chat, so the backend has to
answer it with an empty system instruction, and a *missing* value has to stay
what it always was (the global default set) -- which is why the UI sends "none"
itself rather than leaving the field out.
"""

from dpc_protocol.pcm_core import InstructionBlock, InstructionSet

from dpc_client_core.managers.prompt_manager import PromptManager

MARKER = "ALWAYS ANSWER IN HAIKU"


def _manager() -> PromptManager:
    instructions = InstructionSet(
        default="general",
        sets={
            "general": InstructionBlock(name="General", primary=MARKER),
            "coder": InstructionBlock(name="Coder", primary="WRITE CODE ONLY"),
        },
    )
    return PromptManager(instructions, {})


def _prompt(name):
    return _manager().assemble_prompt(query="hi", include_context=True, instruction_set_name=name)


def test_none_sends_no_instruction_set():
    prompt = _prompt("none")
    assert MARKER not in prompt
    assert "WRITE CODE ONLY" not in prompt
    assert prompt.startswith("\n\nUSER QUERY: hi")


def test_none_is_not_case_sensitive():
    assert _prompt("None") == _prompt("none")


def test_a_missing_name_is_the_global_default_set():
    assert MARKER in _prompt(None)


def test_a_named_set_is_still_sent():
    assert "WRITE CODE ONLY" in _prompt("coder")
