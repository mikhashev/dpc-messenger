"""A provider is told how to call a tool in the one way it will be listened to.

Measured 2026-09-03, group-b3fb2a14b815: an agent on a provider with a native
tools API posted its prose, then a ```tool_call fence, then two
[TOOL RESULT: call_00_...] sections — the second carrying the whole vision-call
JSON including an absolute path under the user's home. The structured calls were
already in the record's own field, so every byte of it duplicated a trace the
interface had.

The log says the routing was right: `Using native tool calling path` 204 times,
`falling back to text injection` 0. What was wrong is that the base system prompt
carried «When you want to use a tool, output a code block like ```tool_call» with
no condition on the provider. The model was handed schemas through the API and,
in the same request, a sentence telling it to write the call as text.

The section was also redundant where it did apply: each of the three text paths
injects a fuller version of the same instructions beside the prompt it builds.
So it is gone from the prompt, and these tests pin the two halves of that —
absent where it contradicts, present where it is parsed.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dpc_client_core.dpc_agent.context import _default_system_prompt
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter

FENCE = "```tool_call"
ADAPTER_SRC = (Path(__file__).resolve().parents[1]
               / "dpc_client_core" / "dpc_agent" / "llm_adapter.py").read_text(encoding="utf-8")


class TestTheFormatIsAbsentWhereItContradicts:
    def test_the_default_system_prompt_teaches_no_text_tool_format(self):
        prompt = _default_system_prompt()
        assert FENCE not in prompt, (
            "the system prompt goes to every provider, including those whose API "
            "carries the call in its own field — telling those to write it as text "
            "is how a fence and two invented tool results reached a group chat"
        )
        assert "How to Use Tools" not in prompt

    def test_the_prompt_is_the_same_for_every_provider(self):
        """What the absence buys: one cached prefix rather than two.

        Making the section conditional would have been the other fix, and it
        splits `static_text` — the block every agent shares under a one-hour
        cache — into a native variant and a text variant.
        """
        assert _default_system_prompt() == _default_system_prompt()
        assert "provider" not in _default_system_prompt().lower().split("## your role")[0]


class TestTheFormatIsPresentWhereItIsParsed:
    def test_the_text_path_still_teaches_the_format(self):
        tools = [{"function": {"name": "run_shell", "description": "d", "parameters": {}}}]
        injected = DpcLlmAdapter.__dict__["_format_tools_for_prompt"](None, tools)
        # A complete example, not a mention of one. The word appears four times in
        # that function, so `FENCE in injected` stayed true even with the opening
        # fence of the example deleted — the first version of this test passed
        # under exactly that damage.
        example = "\n".join([
            FENCE,
            '{"name": "tool_name", "arguments": {"arg1": "value1"}}',
            "```",
        ])
        assert example in injected, (
            "removing the section from the prompt is only safe because the text "
            "path injects a working example of its own; a provider that cannot "
            "see the shape has no way to call a tool"
        )
        assert "run_shell" in injected

    def test_every_text_prompt_is_built_beside_a_tool_injection(self):
        """The invariant the removal rests on.

        Each site that turns messages into a text prompt is followed by the tool
        injection for that same prompt. Counted rather than eyeballed: if a
        fourth text path is added without its injection, the numbers part.
        """
        builds = len(re.findall(r"self\._messages_to_prompt\(", ADAPTER_SRC))
        injects = len(re.findall(r"self\._format_tools_for_prompt\(", ADAPTER_SRC))
        assert builds == 3, f"the text paths changed: {builds} — recheck the pairing"
        assert injects == builds, (
            f"{builds} text prompts built, {injects} tool injections — a text path "
            "without its injection is a provider left with no way to call a tool"
        )
