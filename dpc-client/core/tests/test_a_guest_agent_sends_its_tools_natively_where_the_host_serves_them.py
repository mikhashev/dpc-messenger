"""A guest agent sends its tools natively where the host's menu row says it serves them.

Board entry A-GUEST-AGENT-SENDS-ITS-TOOLS-AS-PROMPT-TEXT-THOUGH-THE-HOST-SERVES-
THEM-NATIVELY (Mike's call, 2026-10-20). The agent's peer route described its
tools in the prompt and parsed calls out of the answer text, because
`CoreService._request_inference_from_peer` dropped the DPTP v1.7 half
(`messages`, `system`, `tools`, `on_chunk`, `request_id`) that the coordinator
below it already carried.

Pinned here: the service door is no narrower than the coordinator; a row that
says `supports_tools: true` gets the turns and the tools on the wire and its
`tool_calls` read back in the local native path's shape; a row that says
nothing keeps the text branch; images beside tools go native only where the row
says `serves_images_with_tools: true`, and then in their turns; the native
prompt is the turns and system flattened, without tool text; a withheld round
(`tool_choice="none"`) goes as text without tools, because the wire carries no
`tool_choice`; a `tools_unsupported` refusal sends the same call again as text;
and a native round streams. Cross-platform: pure asyncio.
"""

import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_protocol.protocol import (
    REFUSAL_NOT_ALLOWED,
    REFUSAL_TOOLS_UNSUPPORTED,
    PeerRefused,
)
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.llm_manager import flatten_messages
from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.p2p_coordinator import P2PCoordinator
from dpc_client_core.service import CoreService

PEER = "dpc-node-" + "c" * 32
ALIAS = "qwen27"
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
TURNS = [{"role": "system", "content": "You are Linus."},
         {"role": "user", "content": "read a.md"}]
CALL = {"type": "tool_use", "id": "toolu_01", "name": "read_file", "input": {"path": "a.md"}}
TEXT_CALL = '```tool_call\n{"name": "read_file", "arguments": {"path": "a.md"}}\n```'
PNG = "data:image/png;base64,iVBORw0KGgo="
WITH_IMAGE = [{"role": "user", "content": [
    {"type": "text", "text": "what is this"},
    {"type": "image_url", "image_url": {"url": PNG}}]}]


def _answer(**fields):
    return {"request_id": "req-wire", "response": "", "model": "qwen-on-the-peer",
            "prompt_tokens": 40, "response_tokens": 3, "tokens_used": 43, **fields}


def _guest(tmp_path, answers, **row):
    """An agent pinned to PEER, whose cached menu row for ALIAS says `row`."""
    asks = []
    answers = list(answers)

    async def _request(**kwargs):
        asks.append(kwargs)
        answer = answers.pop(0)
        if callable(answer):
            answer = await answer(kwargs)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    ledger = NodeLedger(tmp_path / "ledger")
    manager = SimpleNamespace(token_count_manager=None, providers={},
                              agent_provider=None, default_provider=None)
    adapter = DpcLlmAdapter(manager, provider_alias=ALIAS, caller="agent_test",
                            ledger=ledger, compute_host=PEER)
    service = SimpleNamespace(
        _request_inference_from_peer=_request,
        peer_metadata={PEER: {"providers": [dict(alias=ALIAS, type="llamacpp_server", **row)]}},
        p2p_manager=SimpleNamespace(peers={}, peer_cache=None),
    )
    manager.providers["dpc_agent"] = SimpleNamespace(
        peer_id=None, remote_model=None, timeout=5, _service=service)
    return adapter, asks, ledger


def _row(adapter):
    """The cached menu row the guest reads, for a test that changes it."""
    return adapter._llm_manager.providers["dpc_agent"]._service.peer_metadata[PEER]["providers"][0]


# --- (1) the service door carries what the coordinator carries ------------------------


@pytest.mark.asyncio
async def test_the_service_door_is_no_narrower_than_the_coordinator_it_calls():
    """Generated from the coordinator's signature, so a parameter added there
    and not here fails this test rather than vanishing on the agent's route."""
    below = inspect.signature(P2PCoordinator.request_inference_from_peer)
    names = [name for name in below.parameters if name != "self"]
    sentinels = {name: object() for name in names}

    service = CoreService.__new__(CoreService)
    service.settings = SimpleNamespace(get_remote_inference_timeout=lambda: 1.0)
    service.p2p_coordinator = SimpleNamespace(request_inference_from_peer=AsyncMock(return_value={}))
    await service._request_inference_from_peer(**sentinels)

    call = service.p2p_coordinator.request_inference_from_peer.call_args
    received = below.bind(None, *call.args, **call.kwargs).arguments
    for name in names:
        assert received[name] is sentinels[name], f"the service door dropped {name!r}"


# --- (2) the row decides the branch ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_row_that_says_supports_tools_gets_the_tools_natively_and_its_calls_come_back(tmp_path):
    adapter, asks, ledger = _guest(
        tmp_path, [_answer(tool_calls=[CALL], finish_reason="tool_calls")], supports_tools=True)

    msg, usage = await adapter.chat(TURNS, tools=TOOLS)

    (ask,) = asks
    assert ask["tools"] == [{"name": "read_file", "description": "Read a file",
                             "input_schema": TOOLS[0]["function"]["parameters"]}]
    assert ask["system"] == "You are Linus."
    assert [turn["role"] for turn in ask["messages"]] == ["user"]
    # DPTP §3.4: `prompt` is rendered from the turns and system and nothing
    # else, as the gateway renders it; the tools travel in `tools`.
    assert ask["prompt"] == flatten_messages(ask["messages"], ask["system"])
    assert "read a.md" in ask["prompt"]
    assert "read_file" not in ask["prompt"], "no tool description in the native prompt"
    assert not ask.get("images"), "beside tools nothing rides on the flat field"
    assert msg["tool_calls"] == [{"id": "toolu_01", "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps({"path": "a.md"})}}]
    assert usage["finish_reason"] == "tool_calls"
    (row,) = ledger.rows()
    assert (row["route"], row["request_id"]) == ("peer", "req-wire")
    assert row["tool_calls"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("says", [{}, {"supports_tools": False}], ids=["row says nothing", "row says no"])
async def test_a_row_that_does_not_say_supports_tools_keeps_the_text_branch(tmp_path, says):
    adapter, asks, _ = _guest(tmp_path, [_answer(response=TEXT_CALL), _answer()], **says)

    msg, _ = await adapter.chat(TURNS, tools=TOOLS)
    # The same call once the row says it: the row is what decided.
    _row(adapter)["supports_tools"] = True
    await adapter.chat(TURNS, tools=TOOLS)

    text, native = asks
    assert "tools" not in text and "messages" not in text
    assert "read_file" in text["prompt"], "the tools are described in the prompt, as before"
    assert msg["tool_calls"][0]["function"]["name"] == "read_file"
    assert native["tools"]


@pytest.mark.asyncio
@pytest.mark.parametrize("says", [None, False], ids=["row says nothing", "row says no"])
async def test_images_beside_tools_stay_on_the_text_branch_unless_the_row_says_serves_images_with_tools(
        tmp_path, says):
    row = {"supports_tools": True, "supports_vision": True}
    if says is not None:
        row["serves_images_with_tools"] = says
    adapter, asks, _ = _guest(tmp_path, [_answer(response="a cat"), _answer()], **row)

    await adapter.chat(WITH_IMAGE, tools=TOOLS)
    await adapter.chat(TURNS, tools=TOOLS)

    with_image, without = asks
    assert "tools" not in with_image
    assert with_image["images"] == [{"base64": "iVBORw0KGgo=", "mime_type": "image/png"}]
    assert without["tools"], "without the picture the same row is served natively"


@pytest.mark.asyncio
async def test_a_row_that_serves_images_with_tools_gets_the_picture_in_its_turn(tmp_path):
    adapter, asks, _ = _guest(tmp_path, [_answer(response="a cat")], supports_tools=True,
                              supports_vision=True, serves_images_with_tools=True)

    await adapter.chat(WITH_IMAGE, tools=TOOLS)

    (ask,) = asks
    assert ask["tools"] and not ask.get("images")
    (turn,) = ask["messages"]
    images = [block for block in turn["content"] if block.get("type") == "image"]
    assert images == [{"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}]
    assert not [block for block in turn["content"] if block.get("type") == "image_url"]


# --- (3) the local route's tool gate, and the host's refusal --------------------------


@pytest.mark.asyncio
async def test_a_withheld_round_goes_as_text_without_tools(tmp_path):
    """The wire carries no `tool_choice` (DPTP §3.4), so "none" cannot keep
    the tools on the peer route: the conservative branch, as on the local
    route for a provider that does not take the word."""
    adapter, asks, _ = _guest(tmp_path, [_answer(tool_calls=[CALL]), _answer(response="done")],
                              supports_tools=True)

    await adapter.chat(TURNS, tools=TOOLS)
    msg, _ = await adapter.chat(TURNS, tools=TOOLS, tool_choice="none")

    native, withheld = asks
    assert native["tools"]
    assert "tools" not in withheld and "messages" not in withheld
    assert "read_file" not in withheld["prompt"]
    assert "tool_calls" not in msg


@pytest.mark.asyncio
async def test_a_tools_unsupported_refusal_sends_the_same_call_again_as_text(tmp_path):
    refusal = PeerRefused("no native tool-calling path", code=REFUSAL_TOOLS_UNSUPPORTED)
    adapter, asks, _ = _guest(tmp_path, [refusal, _answer(response=TEXT_CALL)], supports_tools=True)

    msg, _ = await adapter.chat(TURNS, tools=TOOLS)

    native, text = asks
    assert native["tools"]
    assert "tools" not in text and "read_file" in text["prompt"]
    assert msg["tool_calls"][0]["function"]["name"] == "read_file"


@pytest.mark.asyncio
async def test_another_refusal_on_the_native_branch_is_not_turned_into_text(tmp_path):
    refusal = PeerRefused("compute sharing disabled", code=REFUSAL_NOT_ALLOWED)
    adapter, asks, _ = _guest(tmp_path, [refusal], supports_tools=True)

    with pytest.raises(PeerRefused):
        await adapter.chat(TURNS, tools=TOOLS)
    (ask,) = asks
    assert ask["tools"], "refused on the native branch, and not asked again"


# --- (4) a native round streams --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_native_round_streams_its_deltas_to_the_agents_callback(tmp_path):
    async def _host(kwargs):
        for piece in ("reading ", "a.md"):
            await kwargs["on_chunk"](piece)
        return _answer(response="reading a.md")

    adapter, asks, _ = _guest(tmp_path, [_host], supports_tools=True)
    seen = []

    async def on_stream_chunk(chunk, conversation_id):
        seen.append((chunk, conversation_id))

    msg, _ = await adapter.chat(TURNS, tools=TOOLS, on_stream_chunk=on_stream_chunk,
                                conversation_id="conv-1")

    assert seen == [("reading ", "conv-1"), ("a.md", "conv-1")]
    assert msg["content"] == "reading a.md", "the text is the response's, not the deltas'"
