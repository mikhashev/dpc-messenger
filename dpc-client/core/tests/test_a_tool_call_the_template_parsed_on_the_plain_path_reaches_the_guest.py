"""A tool call the engine's template parsed on the plain path reaches the caller.

A guest agent on a peer's model puts its tools in the prompt and sends none on
the wire, so the host serves it on the plain path. llama-server with a chat
template parses the model's own call into `tool_calls` even then
(`finish=tool_calls`, `tools=0`), and the plain path returned `content` alone:
the guest got empty text, logged «LLM returned empty content» and asked the
same prompt again. Host log 2026-10-20 16:51:43 – 16:53:36, strata flash-next
(A-PEER-SERVED-TOOL-CALL-IS-DROPPED-ON-THE-PLAIN-PATH).

The call now comes back as the ```tool_call block the guest's text path
teaches and parses, so a guest of any version reads it.
"""

import json
import logging
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.llm_manager import LLMManager
from dpc_client_core.providers import StrataProvider
from dpc_client_core.providers.openai_provider import OpenAICompatibleProvider

from tests.test_llamacpp_server_provider import (
    _FakeSupervisor, _chat_resp, _fake_client, _provider,
)
from tests.test_strata_provider import _clean_registry, _config  # noqa: F401 (fixture)
from tests.test_strata_supervisor import _install


def _call(name, arguments, index=0):
    return SimpleNamespace(
        index=index, id=f"call_{index}", type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _guest_parse(text):
    """The guest's own parser, the one `_chat_via_remote_peer` runs."""
    adapter = DpcLlmAdapter.__new__(DpcLlmAdapter)
    return [
        (tc["function"]["name"], json.loads(tc["function"]["arguments"]))
        for tc in adapter._parse_tool_calls(text)
    ]


def _wire(p, resp):
    p.supervisor = _FakeSupervisor()
    client, _ = _fake_client(resp)

    async def _ensure():
        return client
    p._ensure = _ensure


@pytest.mark.asyncio
async def test_a_call_with_no_text_comes_back_as_a_block_the_guest_parses():
    p = _provider()
    _wire(p, _chat_resp(content="", tool_calls=[_call("read_file", '{"path": "a.md"}')],
                        finish_reason="tool_calls"))

    text = await p.generate_response("prompt with the tools in it")

    assert _guest_parse(text) == [("read_file", {"path": "a.md"})]


@pytest.mark.asyncio
async def test_text_beside_the_calls_is_kept_and_every_call_follows_it():
    p = _provider()
    _wire(p, _chat_resp(content="Reading both.", tool_calls=[
        _call("read_file", '{"path": "a.md"}', 0),
        _call("list_dir", '{"path": "."}', 1),
    ], finish_reason="tool_calls"))

    text = await p.generate_response("prompt")

    assert text.startswith("Reading both.\n")
    assert _guest_parse(text) == [("read_file", {"path": "a.md"}), ("list_dir", {"path": "."})]


@pytest.mark.asyncio
async def test_the_usage_line_counts_the_calls_on_the_plain_path(caplog):
    p = _provider()
    _wire(p, _chat_resp(content="", tool_calls=[_call("read_file", "{}")],
                        finish_reason="tool_calls"))

    with caplog.at_level(logging.INFO, logger="dpc_client_core.providers.llamacpp_server_provider"):
        await p.generate_response("prompt")

    assert any("tool_calls=1" in r.getMessage() and "path=plain" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_arguments_that_are_not_a_json_object_become_empty_as_on_the_tools_path():
    p = _provider()
    _wire(p, _chat_resp(content="", tool_calls=[_call("get_time", "not json")],
                        finish_reason="tool_calls"))

    assert _guest_parse(await p.generate_response("prompt")) == [("get_time", {})]


@pytest.mark.asyncio
async def test_the_stream_puts_the_calls_into_the_text_and_into_the_chunks():
    """Deltas split the arguments across chunks; the chunks still add up to
    the answer, so a streamed reader sees what the return value says."""
    p = _provider()
    p.supervisor = _FakeSupervisor()

    def _delta(content=None, tool_calls=None, finish_reason=None):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(reasoning_content=None, content=content,
                                      tool_calls=tool_calls),
                finish_reason=finish_reason)],
            usage=None)

    chunks = [
        _delta(content="Looking."),
        _delta(tool_calls=[_call("read_file", '{"pa', 0)]),
        _delta(tool_calls=[SimpleNamespace(index=0, id=None, function=SimpleNamespace(
            name=None, arguments='th": "a.md"}'))]),
        _delta(finish_reason="tool_calls"),
        SimpleNamespace(choices=[], usage=SimpleNamespace(
            prompt_tokens=9, completion_tokens=5, total_tokens=14)),
    ]

    class _Stream:
        def __aiter__(self):
            async def gen():
                for c in chunks:
                    yield c
            return gen()

    class _Completions:
        async def create(self, **params):
            return _Stream()

    async def _ensure():
        return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    p._ensure = _ensure
    seen = []

    async def on_chunk(text, _conv):
        seen.append(text)

    text = await p.generate_response_stream("prompt", on_chunk=on_chunk)

    assert _guest_parse(text) == [("read_file", {"path": "a.md"})]
    assert "".join(seen) == text


@pytest.mark.asyncio
async def test_a_strata_alias_serves_the_call_to_a_guest_through_the_hosts_query(tmp_path):
    """The live shape: the host's peer path is `query` on the serving alias,
    and the guest's peer route reads the `response` it sends back."""
    p = StrataProvider("strata flash-next", _config(_install(tmp_path)))
    _wire(p, _chat_resp(content="", tool_calls=[_call("read_file", '{"path": "a.md"}')],
                        finish_reason="tool_calls"))
    manager = LLMManager.__new__(LLMManager)
    manager.providers = {"strata flash-next": p}
    manager.default_provider = "strata flash-next"
    manager.vision_provider = None
    manager.provider_configs = {}
    manager.token_count_manager = None
    manager.count_tokens = lambda text, model: len(text or "") // 4
    manager.get_context_window = lambda model: 200000

    async def _request(**kwargs):
        served = await manager.query(kwargs["prompt"], provider_alias="strata flash-next",
                                     return_metadata=True)
        return {"response": served["response"], "prompt_tokens": 9, "response_tokens": 5,
                "tokens_used": 14}

    adapter = DpcLlmAdapter.__new__(DpcLlmAdapter)
    adapter._llm_manager = SimpleNamespace(providers={})
    adapter._provider_alias = "strata flash-next"
    adapter._token_counter = None
    adapter._default_model = "m"
    service = SimpleNamespace(
        _request_inference_from_peer=_request,
        p2p_manager=SimpleNamespace(peers={}, peer_cache=None),
    )
    ctx = SimpleNamespace(peer_id="dpc-node-" + "b" * 32, remote_model="qwen",
                          remote_provider="strata flash-next", timeout=30, _service=service)
    tools = [{"type": "function", "function": {
        "name": "read_file", "description": "", "parameters": {"type": "object"}}}]

    msg, _usage = await adapter._chat_via_remote_peer(
        ctx, [{"role": "user", "content": "read a.md"}], tools)

    assert [(tc["function"]["name"], json.loads(tc["function"]["arguments"]))
            for tc in msg.get("tool_calls", [])] == [("read_file", {"path": "a.md"})]


@pytest.mark.asyncio
async def test_an_openai_compatible_alias_in_front_of_a_template_keeps_the_call_too():
    p = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    p.alias, p.model, p.temperature = "lan_llama", "qwen", 0.7
    resp = _chat_resp(content=None, tool_calls=[_call("read_file", '{"path": "a.md"}')],
                      finish_reason="tool_calls")

    async def create(**params):
        return resp
    p.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    assert _guest_parse(await p.generate_response("prompt")) == [("read_file", {"path": "a.md"})]
