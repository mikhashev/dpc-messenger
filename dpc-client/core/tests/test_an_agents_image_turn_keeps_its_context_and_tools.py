"""An agent's turn that carries an image is still the agent's turn.

The adapter used to answer it with `generate_with_vision(prompt=<last user
text>, images=...)`: no system prompt, no history, no tool schemas and no
effort, with the token counts estimated from that one string. An agent on
llama-server with an mmproj answered a screenshot with a raw tool-call block,
because the tools never reached the call (A-VISION-TURN-PUBLISHES-THE-MARKUP-
IT-COULD-NOT-PARSE-AND-THE-LOG-SAYS-SO). Mike's call, 2026-10-26: the image goes
into the normal turn, and DeepSeek Flash sees with it — an agent on a node with
no GPU reaches the picture only through its own provider.

Two branches, both keeping the context:

- the provider serves image blocks beside tools (`entry_point_for(...,
  images=True)` names `generate_with_tools`): the picture rides in its user turn
  of the native tools call, with the system prompt, the history, the tools and
  the effort, and the round's usage is the provider's;
- it does not (no tools path, or a `tool_choice="none"` round it cannot take):
  the picture is described — by the agent's own provider when that one can see —
  and the description joins the same full text turn.

Fakes only, no network.
"""

import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider


def _png() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (24, 8), (250, 250, 250)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


PNG = _png()
SYSTEM = "You are Ark, the agent of this node."
EARLIER_Q = "What did we decide about the release?"
EARLIER_A = "We ship on Friday."
ASK = "What text is in this image? Answer with the text only."
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
REPORTED = {"prompt_tokens": 1234, "completion_tokens": 5, "total_tokens": 1239,
            "output_includes_thinking": "includes"}


def _agent_turn():
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": EARLIER_Q},
        {"role": "assistant", "content": EARLIER_A},
        {"role": "user", "content": [
            {"type": "text", "text": ASK},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}},
        ]},
    ]


class _SeeingWithoutTools:
    """Sees, but has no native tools path (Anthropic, OpenAI, Gemini here)."""

    alias = "seer"
    model = "seer-vl"
    config = {"type": "ollama", "model": "seer-vl"}

    def __init__(self, *, fail_tools=False):
        self.fail_tools = fail_tools
        self.tool_calls = []
        self.vision_calls = []
        self.text_calls = []

    def supports_vision(self):
        return True

    def image_pixel_limit(self):
        return None

    async def generate_with_vision(self, prompt, images, **kwargs):
        self.vision_calls.append(prompt)
        return "a context-less look"

    async def generate_response(self, prompt, **kwargs):
        self.text_calls.append(prompt)
        return "an answer from the text turn"

    def get_last_usage(self):
        return None


class _Seeing(_SeeingWithoutTools):
    """A provider that sees and serves image blocks beside tools."""

    async def generate_with_tools(self, messages, tools, system="", on_chunk=None,
                                  conversation_id=None, reasoning_effort=None, tool_choice="auto"):
        self.tool_calls.append({"messages": messages, "tools": tools, "system": system,
                                "reasoning_effort": reasoning_effort, "tool_choice": tool_choice})
        if self.fail_tools:
            raise RuntimeError("the vendor refused the request")
        return {"content": "DPC 47", "tool_calls_raw": [], "thinking": None, "usage": dict(REPORTED)}


class _SeeingWithoutToolChoice(_Seeing):
    async def generate_with_tools(self, messages, tools, system="", on_chunk=None,
                                  conversation_id=None, reasoning_effort=None):
        return await super().generate_with_tools(messages, tools, system, on_chunk,
                                                 conversation_id, reasoning_effort)


class _Blind(_Seeing):
    alias = "blind"

    def supports_vision(self):
        return False


def _adapter(provider, tmp_path, description="the image reads DPC 47"):
    queries = []

    async def _query(**kwargs):
        queries.append(kwargs)
        return {"response": description}

    manager = SimpleNamespace(
        providers={provider.alias: provider}, default_provider=provider.alias,
        agent_provider=None, query=_query, token_count_manager=None,
    )
    ledger = NodeLedger(tmp_path / "ledger")
    adapter = DpcLlmAdapter(manager, provider_alias=provider.alias, caller="agent_ark", ledger=ledger)
    return adapter, queries, ledger


def _texts(turn):
    content = turn["content"]
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")


# --- the provider serves images beside tools -----------------------------------

@pytest.mark.asyncio
async def test_the_image_turn_reaches_the_tools_call_with_everything_the_agent_has(tmp_path):
    provider = _Seeing()
    adapter, queries, _ = _adapter(provider, tmp_path)

    msg, _ = await adapter.chat(_agent_turn(), tools=TOOLS, reasoning_effort="high")

    assert msg["content"] == "DPC 47"
    (call,) = provider.tool_calls
    assert call["system"] == SYSTEM
    turns = call["messages"]
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert _texts(turns[0]) == EARLIER_Q and _texts(turns[1]) == EARLIER_A
    last = turns[-1]["content"]
    assert ASK in _texts(turns[-1])
    assert [b for b in last if b["type"] == "image"] == [{"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": PNG}}]
    assert not [b for b in last if b["type"] == "image_url"], "a part the converter drops in silence"
    assert [t["name"] for t in call["tools"]] == ["read_file"]
    assert call["reasoning_effort"] == "high"
    assert provider.vision_calls == [] and provider.text_calls == [] and queries == []


@pytest.mark.asyncio
async def test_the_rounds_counts_are_the_providers_own(tmp_path):
    provider = _Seeing()
    adapter, _, ledger = _adapter(provider, tmp_path)

    _, usage = await adapter.chat(_agent_turn(), tools=TOOLS)

    assert (usage["prompt_tokens"], usage["completion_tokens"]) == (1234, 5)
    (row,) = ledger.rows()
    assert row["counts_source"] == "engine"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (1234, 5)


@pytest.mark.asyncio
async def test_a_later_tool_round_still_carries_the_picture_and_the_results(tmp_path):
    """The loop sends the same user turn again each round, now followed by the
    call and its result; the round stays a tools turn with the picture in it."""
    provider = _Seeing()
    adapter, _, _ = _adapter(provider, tmp_path)
    messages = _agent_turn() + [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "file body"},
    ]

    await adapter.chat(messages, tools=TOOLS)

    turns = provider.tool_calls[0]["messages"]
    assert any(b["type"] == "image" for b in turns[2]["content"])
    assert turns[-1]["content"][0]["type"] == "tool_result"


@pytest.mark.asyncio
async def test_a_failed_tools_call_describes_the_picture_rather_than_dropping_it(tmp_path):
    provider = _Seeing(fail_tools=True)
    adapter, queries, _ = _adapter(provider, tmp_path)

    await adapter.chat(_agent_turn(), tools=TOOLS)

    assert [q["provider_alias"] for q in queries] == ["seer"]
    (prompt,) = provider.text_calls
    assert "the image reads DPC 47" in prompt and SYSTEM in prompt and EARLIER_A in prompt


# --- the provider cannot take images beside tools on this call -----------------

@pytest.mark.asyncio
async def test_a_provider_without_a_tools_path_describes_with_itself_and_keeps_the_context(tmp_path):
    provider = _SeeingWithoutTools()
    adapter, queries, _ = _adapter(provider, tmp_path)

    msg, _ = await adapter.chat(_agent_turn(), tools=TOOLS)

    assert [q["provider_alias"] for q in queries] == ["seer"], "the picture went to another provider"
    assert [img["base64"] for img in queries[0]["images"]] == [PNG]
    assert provider.vision_calls == [], "the adapter called the context-less path itself"
    (prompt,) = provider.text_calls
    for part in (SYSTEM, EARLIER_Q, EARLIER_A, ASK, "the image reads DPC 47", "read_file"):
        assert part in prompt
    assert msg["content"] == "an answer from the text turn"


@pytest.mark.asyncio
async def test_a_no_tool_round_on_a_provider_that_cannot_take_the_word_is_described(tmp_path):
    provider = _SeeingWithoutToolChoice()
    adapter, queries, _ = _adapter(provider, tmp_path)

    await adapter.chat(_agent_turn(), tools=TOOLS, tool_choice="none")

    assert provider.tool_calls == []
    assert [q["provider_alias"] for q in queries] == ["seer"]
    assert "the image reads DPC 47" in provider.text_calls[0]


@pytest.mark.asyncio
async def test_a_blind_provider_keeps_the_auto_selected_description_and_sends_no_image_part(tmp_path):
    provider = _Blind()
    adapter, queries, _ = _adapter(provider, tmp_path)

    await adapter.chat(_agent_turn(), tools=TOOLS)

    assert [q["provider_alias"] for q in queries] == [None]
    turns = provider.tool_calls[0]["messages"]
    assert "the image reads DPC 47" in _texts(turns[-1])
    assert all(isinstance(t["content"], str) or not [
        b for b in t["content"] if b.get("type") in ("image", "image_url")] for t in turns)


# --- DeepSeek Flash, as one concrete provider ----------------------------------

def _deepseek(model):
    provider = DeepSeekProvider("deepseek_flash", {
        "type": "deepseek", "model": model, "api_key": "test-key", "thinking_enabled": True})
    provider.client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content="DPC 47", reasoning_content="cot", tool_calls=[]))],
        usage=SimpleNamespace(
            prompt_tokens=880, completion_tokens=40, total_tokens=920,
            prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=880,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=35)),
    ))
    return provider


@pytest.mark.asyncio
async def test_deepseek_flash_gets_the_whole_turn_with_the_image_in_the_user_message(tmp_path):
    provider = _deepseek("deepseek-v4-flash")
    adapter, queries, _ = _adapter(provider, tmp_path)

    msg, usage = await adapter.chat(_agent_turn(), tools=TOOLS, reasoning_effort="high")

    assert msg["content"] == "DPC 47" and queries == []
    (_, sent), = provider.client.chat.completions.create.call_args_list
    wire = sent["messages"]
    assert wire[0] == {"role": "system", "content": SYSTEM}
    assert wire[1] == {"role": "user", "content": EARLIER_Q}
    assert wire[2]["role"] == "assistant" and wire[2]["content"] == EARLIER_A
    assert wire[-1]["role"] == "user"
    parts = wire[-1]["content"]
    assert {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}} in parts
    assert any(p["type"] == "text" and ASK in p["text"] for p in parts)
    assert all(isinstance(m["content"], str) or m["content"] is None
               for m in wire if m["role"] != "user"), "DeepSeek takes images in user messages only"
    assert [t["function"]["name"] for t in sent["tools"]] == ["read_file"]
    assert sent["extra_body"]["thinking"] == {"type": "enabled"}
    assert sent["extra_body"]["reasoning_effort"] == "high"
    assert usage["prompt_tokens"] == 880


@pytest.mark.asyncio
async def test_deepseek_pro_does_not_see_and_the_picture_is_described_for_it(tmp_path):
    provider = _deepseek("deepseek-v4-pro")
    adapter, queries, _ = _adapter(provider, tmp_path)

    await adapter.chat(_agent_turn(), tools=TOOLS)

    assert [q["provider_alias"] for q in queries] == [None]
    (_, sent), = provider.client.chat.completions.create.call_args_list
    assert all(isinstance(m["content"], str) or m["content"] is None for m in sent["messages"])
    assert "the image reads DPC 47" in sent["messages"][-1]["content"]
