"""The door hands over the reasoning its usage row counts.

ADR-041 D4, amendment 2026-11-04; board entry THE-DOOR-COUNTS-THE-REASONING-AND-
NEVER-HANDS-IT-OVER-SO-A-CLIENT-WATCHES-AN-EMPTY-THINKING-LINE. On the OpenAI form
a stream carries `reasoning_content` deltas as the model makes them, before the
content they precede, and a whole message carries a `reasoning_content` field: the
names DeepSeek, llama-server and the Strata launcher write (`serve/server.py`
v0.2.9, `chunk({"reasoning_content": ...})` and `msg["reasoning_content"]`).

Reasoning travels by `on_reasoning`, a parameter only the door passes, so the agent
loop, the UI and a peer's chunk sender keep receiving what they received, and that
is pinned on the real provider classes. The stack under the door is real, as in the
file `_Engine` comes from.
"""

import ast
import asyncio
import inspect
import json
import textwrap

import aiohttp
import pytest
from openai import AsyncOpenAI

from dpc_client_core.gateway import _openai_messages
from dpc_client_core.llm_manager import PROVIDER_MAP, LLMManager
from dpc_client_core.providers import AIProvider
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider
from dpc_client_core.providers.neuraldeep_provider import NeuralDeepProvider
from dpc_client_core.providers.strata_provider import StrataProvider
from dpc_client_core.providers.zai_provider import ZaiProvider
from tests.test_a_client_that_leaves_the_door_ends_the_call_and_nothing_is_generated_again import (
    ALIAS,
    CHAT_PATH,
    MESSAGES_PATH,
    TOOLS_ANTHROPIC,
    TOOLS_OPENAI,
    _chat,
    _Client,
    _Engine,
    _forget,
    _local_provider,
    _messages,
    _service,
    _settled,
    _until,
    fast_backoff,  # noqa: F401  (a fixture)
    no_handler_cancellation,  # noqa: F401  (a fixture)
)
from tests.test_strata_supervisor import _install
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    PEER_ANSWER,
    REMOTE_MODEL,
    _peer_service,
    _priced_result,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import _key, _running

REASONING = ("Let me ", "think ", "this over.")
CONTENT = ("the ", "answer")
ONE_PIXEL = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
ENGINE_CALL = {"id": "call_1", "type": "function",
               "function": {"name": "read_file", "arguments": "{\"path\": \"a.txt\"}"}}


async def _post(server, tmp_path, path, body):
    url = f"http://127.0.0.1:{server.port}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers={"x-api-key": _key(tmp_path)}) as response:
            return response.status, await response.text()


def _events(text):
    return [json.loads(line[len("data: "):]) for line in text.split("\n")
            if line.startswith("data: {")]


def _deltas(text):
    return [event["choices"][0]["delta"] for event in _events(text) if event.get("choices")]


def _field(deltas, name):
    return [delta[name] for delta in deltas if delta.get(name)]


async def _door_call(tmp_path, engine_kwargs, body, *, path=CHAT_PATH, provider_kwargs=None, prepare=None):
    """One call through the whole stack. Returns (status, text, engine, rows)."""
    async with _Engine(**engine_kwargs) as engine:
        provider = _local_provider(engine, **(provider_kwargs or {}))
        if prepare is not None:
            prepare(provider)
        try:
            async with _running(tmp_path, _service(tmp_path, provider)) as (server, ledger):
                status, text = await _post(server, tmp_path, path, body)
                return status, text, engine, list(ledger.rows())
        finally:
            _forget(provider)


# --- (1) a stream, as the model makes it ----------------------------------------------


@pytest.mark.asyncio
async def test_reasoning_deltas_arrive_piece_by_piece_before_the_content_they_precede(tmp_path):
    status, text, engine, _ = await _door_call(
        tmp_path, {"reasoning": REASONING, "content": CONTENT}, _chat(stream=True),
    )
    assert status == 200
    deltas = _deltas(text)
    assert _field(deltas, "reasoning_content") == list(REASONING), "the pieces the engine sent, not one lump"
    assert _field(deltas, "content") == list(CONTENT)
    keys = [next(k for k in delta if k != "role") for delta in deltas if delta]
    assert keys == ["reasoning_content"] * 3 + ["content"] * 2, "reasoning after the content it precedes"
    assert deltas[0]["role"] == "assistant", "the first delta names the role, whichever field it carries"
    assert [delta.get("role") for delta in deltas[1:]] == [None] * (len(deltas) - 1), "one role per response"
    assert _events(text)[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_a_reasoning_delta_leaves_while_the_model_is_still_thinking(tmp_path):
    """A door that collected the reasoning and wrote it before the first content
    would pass every order check; the client would still watch an empty line."""
    gate = asyncio.Event()
    async with _Engine(reasoning=REASONING, content=CONTENT, content_gate=gate) as engine:
        provider = _local_provider(engine)
        try:
            async with _running(tmp_path, _service(tmp_path, provider)) as (server, _):
                client = _Client(server.port, _key(tmp_path), CHAT_PATH, _chat(stream=True))
                await client.send()
                seen = await client.read_until(REASONING[-1].encode("utf-8"))
                assert b'"content"' not in seen and not engine.finished
                gate.set()
                rest = await client.read_until(b"[DONE]")
                await client.leave()
                assert b"answer" in rest
        finally:
            gate.set()
            _forget(provider)


@pytest.mark.asyncio
async def test_a_stream_on_a_strata_alias_carries_it_too(tmp_path):
    """The class the owner runs: it inherits the entry points, and this is what says so."""
    def strata(engine):
        return _local_provider(
            engine, alias="flash", provider_class=StrataProvider,
            model="qwen3.8-flash-next-iq3_xxs", strata_install_dir=str(_install(tmp_path)),
            strata_base_config="strata-base.json", conversation_cache_mib=10240,
        )

    async with _Engine(reasoning=REASONING, content=CONTENT) as engine:
        provider = strata(engine)
        try:
            async with _running(tmp_path, _service(tmp_path, provider)) as (server, _):
                _, text = await _post(server, tmp_path, CHAT_PATH, _chat("flash", stream=True))
        finally:
            _forget(provider)
    assert _field(_deltas(text), "reasoning_content") == list(REASONING)


# --- (2) the tools path answers whole: one delta, still in front ----------------------


@pytest.mark.parametrize("content", [CONTENT, ()], ids=["with-prose", "calls-alone"])
@pytest.mark.asyncio
async def test_on_the_tools_path_the_reasoning_is_one_delta_in_front_of_the_prose_and_the_calls(tmp_path, content):
    status, text, engine, _ = await _door_call(
        tmp_path, {"reasoning": REASONING, "content": content, "tool_calls": [ENGINE_CALL]},
        _chat(stream=True, tools=TOOLS_OPENAI),
    )
    assert status == 200
    deltas = _deltas(text)
    assert deltas[0] == {"role": "assistant", "reasoning_content": "".join(REASONING)}
    assert len(_field(deltas, "reasoning_content")) == 1, "the reasoning was written twice"
    filled = [delta for delta in deltas[1:] if delta]
    assert "tool_calls" in filled[-1] and "role" not in filled[-1]
    if content:
        assert filled[0] == {"content": "".join(content)}
    assert _events(text)[-1]["choices"][0]["finish_reason"] == "tool_calls"


# --- (3) a whole message --------------------------------------------------------------


@pytest.mark.parametrize("extra", [{}, {"tools": TOOLS_OPENAI}], ids=["plain", "tools"])
@pytest.mark.asyncio
async def test_a_whole_message_carries_the_field_beside_the_content_and_the_count_is_unchanged(tmp_path, extra):
    status, text, _, rows = await _door_call(
        tmp_path, {"reasoning": REASONING, "content": CONTENT}, _chat(**extra),
    )
    assert status == 200
    body = json.loads(text)
    message = body["choices"][0]["message"]
    assert message["reasoning_content"] == "".join(REASONING)
    assert message["content"] == "".join(CONTENT)
    (row,) = rows
    assert body["usage"]["completion_tokens"] == row["completion_tokens"]


@pytest.mark.asyncio
async def test_a_picture_without_tools_answers_whole_and_its_reasoning_still_comes_first(tmp_path):
    def sees(provider):
        provider.supports_vision = lambda: True

    picture = [{"type": "text", "text": "what is this"},
               {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{ONE_PIXEL}"}}]
    body = {"model": ALIAS, "stream": True, "messages": [{"role": "user", "content": picture}]}
    status, text, _, _ = await _door_call(
        tmp_path, {"reasoning": REASONING, "content": CONTENT}, body, prepare=sees,
    )
    assert status == 200, text
    deltas = [delta for delta in _deltas(text) if delta]
    assert deltas[0] == {"role": "assistant", "reasoning_content": "".join(REASONING)}
    assert deltas[1] == {"content": "".join(CONTENT)}


# --- (4) a provider with none: as it was ----------------------------------------------


@pytest.mark.asyncio
async def test_a_stream_with_no_reasoning_is_the_stream_it_always_was(tmp_path):
    _, text, _, _ = await _door_call(tmp_path, {"content": CONTENT}, _chat(stream=True))
    assert _deltas(text) == [{"role": "assistant", "content": "the "}, {"content": "answer"}, {}]
    assert "reasoning_content" not in text


@pytest.mark.asyncio
async def test_a_whole_message_with_no_reasoning_has_no_such_key(tmp_path):
    _, text, _, _ = await _door_call(tmp_path, {"content": CONTENT}, _chat())
    assert set(json.loads(text)["choices"][0]["message"]) == {"role", "content"}


class _TagsInTheText(AIProvider):
    """A model that writes `<think>` into its answer, on a provider with no stream:
    the manager hands the raw text to `on_chunk`, then parses the tags out."""

    def __init__(self):
        super().__init__(ALIAS, {"type": "ollama", "model": "m"})

    def supports_thinking(self):
        return True

    async def generate_response(self, prompt, **kwargs):
        return "<think>tagged notes</think>the answer"


@pytest.mark.asyncio
async def test_tags_already_written_as_text_on_a_stream_are_not_written_again_as_reasoning(tmp_path):
    async with _running(tmp_path, _service(tmp_path, _TagsInTheText())) as (server, _):
        _, text = await _post(server, tmp_path, CHAT_PATH, _chat(stream=True))
    deltas = _deltas(text)
    assert not _field(deltas, "reasoning_content")
    assert "tagged notes" in "".join(_field(deltas, "content"))


@pytest.mark.asyncio
async def test_tags_in_a_whole_answer_become_the_field(tmp_path):
    async with _running(tmp_path, _service(tmp_path, _TagsInTheText())) as (server, _):
        _, text = await _post(server, tmp_path, CHAT_PATH, _chat())
    message = json.loads(text)["choices"][0]["message"]
    assert message["reasoning_content"] == "tagged notes"
    assert message["content"] == "the answer"


# --- (5) the peer route ---------------------------------------------------------------


async def _peer_call(tmp_path, body, **service):
    async with _running(tmp_path, _peer_service(tmp_path, **service)) as (server, _):
        return await _post(server, tmp_path, CHAT_PATH, body)


@pytest.mark.asyncio
async def test_a_peer_whole_message_carries_what_the_host_sent(tmp_path):
    status, text = await _peer_call(tmp_path, _chat(REMOTE_MODEL), result=_priced_result(thinking="the host's notes"))
    assert status == 200
    assert json.loads(text)["choices"][0]["message"]["reasoning_content"] == "the host's notes"


@pytest.mark.asyncio
async def test_a_peer_that_sends_no_chunk_gets_its_reasoning_in_front_of_the_text(tmp_path):
    _, text = await _peer_call(tmp_path, _chat(REMOTE_MODEL, stream=True),
                               result=_priced_result(thinking="the host's notes"))
    deltas = [delta for delta in _deltas(text) if delta]
    assert deltas[0] == {"role": "assistant", "reasoning_content": "the host's notes"}
    assert deltas[1] == {"content": PEER_ANSWER}


@pytest.mark.asyncio
async def test_a_peer_stream_hands_the_reasoning_over_when_the_answer_frame_brings_it_after_the_chunks(tmp_path):
    """A chunk carries text and the reasoning rides the final frame, so it cannot
    come first without a wire change. Late is what arrives; it is not dropped."""
    _, text = await _peer_call(tmp_path, _chat(REMOTE_MODEL, stream=True),
                               chunks=["hello ", "there"], result=_priced_result(thinking="the host's notes"))
    deltas = _deltas(text)
    assert deltas[0] == {"role": "assistant", "content": "hello "}
    assert deltas[1] == {"content": "there"}
    assert deltas[2] == {"reasoning_content": "the host's notes"}
    assert _events(text)[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize("thinking", [None, "", 7, {"a": 1}], ids=["none", "empty", "number", "object"])
@pytest.mark.asyncio
async def test_a_host_whose_reasoning_is_not_text_sends_none_through_the_door(tmp_path, thinking):
    """The wire can carry anything, and this field is passed on to a third party's parser."""
    _, text = await _peer_call(tmp_path, _chat(REMOTE_MODEL), result=_priced_result(thinking=thinking))
    assert "reasoning_content" not in json.loads(text)["choices"][0]["message"]


# --- (6) raw on purpose ----------------------------------------------------------------


# The shape of the five stored reasoning texts that hold call markup, out of 8356
# read on 2026-11-04 from `~/.dpc`: each is the model talking about a call, none
# makes one. This one is written for the test; none of them is copied here.
TALKING_ABOUT_A_CALL = 'My last turn was "<tool_call><function=read_file>..." — malformed, so it never ran. '


@pytest.mark.asyncio
async def test_call_markup_in_the_reasoning_reaches_the_client_verbatim_and_calls_nothing(tmp_path):
    """Cut at the marker, as the answer's rule cuts, the sentence that diagnoses the
    failure would go, and everything after it. See the amendment for the argument."""
    pieces = (TALKING_ABOUT_A_CALL, "Next I will read it properly.")
    _, text, _, _ = await _door_call(tmp_path, {"reasoning": pieces, "content": CONTENT}, _chat(stream=True))
    deltas = _deltas(text)
    assert _field(deltas, "reasoning_content") == list(pieces)
    assert not _field(deltas, "tool_calls")
    assert _events(text)[-1]["choices"][0]["finish_reason"] == "stop"


# --- (7) a client that sends it back --------------------------------------------------


NOTES = "SENT-BACK-NOTES"


def test_a_reasoning_content_on_an_assistant_turn_is_read_by_nobody():
    system, turns, _ = _openai_messages([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "reasoning_content": NOTES},
        {"role": "user", "content": "more"},
    ])
    assert NOTES not in json.dumps(turns) and NOTES not in system


@pytest.mark.parametrize("extra", [{}, {"tools": TOOLS_OPENAI}, {"stream": True}], ids=["plain", "tools", "stream"])
@pytest.mark.asyncio
async def test_a_client_that_sends_it_back_is_answered_and_the_model_never_sees_it(tmp_path, extra):
    turn = {"role": "assistant", "content": "hello", "reasoning_content": NOTES}
    if "tools" in extra:
        turn["tool_calls"] = [ENGINE_CALL]
    body = {"model": ALIAS, "messages": [
        {"role": "user", "content": "hi"}, turn,
        *([{"role": "tool", "tool_call_id": "call_1", "content": "ok"}] if "tools" in extra else []),
        {"role": "user", "content": "more"},
    ], **extra}
    status, _, engine, _ = await _door_call(
        tmp_path, {"content": CONTENT}, body, provider_kwargs={"preserve_reasoning": True},
    )
    assert status == 200
    (sent,) = engine.requests
    assert NOTES not in json.dumps(sent), "the client's copy reached the model, as text or as a replay"


@pytest.mark.asyncio
async def test_deepseeks_replay_rule_stands_when_a_client_sends_it_back(tmp_path, monkeypatch):
    """The vendor refuses a replayed call turn with no reasoning, so the provider pads
    or looks up its own copy; what a client sent is neither of those."""
    async with _Engine(content=CONTENT) as engine:
        provider = _sdk_provider(DeepSeekProvider, engine, monkeypatch, {"model": "deepseek-v4-flash"})
        _, turns, _ = _openai_messages([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "reasoning_content": NOTES, "tool_calls": [ENGINE_CALL]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ])
        await provider.generate_with_tools(turns, TOOLS_ANTHROPIC)
    (sent,) = engine.requests
    (replayed,) = [m for m in sent["messages"] if m["role"] == "assistant"]
    assert replayed["reasoning_content"] == " "


# --- (8) the Messages form: left as it was ---------------------------------------------


@pytest.mark.parametrize("body", [_messages(), _messages(stream=True), _messages(tools=TOOLS_ANTHROPIC, stream=True)],
                         ids=["whole", "stream", "tools-stream"])
@pytest.mark.asyncio
async def test_the_messages_form_hands_over_no_thinking_block_and_no_reasoning_text(tmp_path, body):
    """A real block carries a signature this door cannot make. Question for Mike."""
    status, text, _, _ = await _door_call(
        tmp_path, {"reasoning": REASONING, "content": CONTENT}, body, path=MESSAGES_PATH,
    )
    assert status == 200
    assert '"thinking_delta"' not in text and '"type": "thinking"' not in text
    assert "think " not in text and "reasoning_content" not in text
    assert "answer" in text


# --- (9) every other consumer sees what it saw -----------------------------------------


def _manager(tmp_path, provider):
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"providers": []}), encoding="utf-8")
    manager = LLMManager(config_path=config)
    manager.providers = {provider.alias: provider}
    manager.default_provider = provider.alias
    return manager


class _Trace:
    """Both callbacks on one list, so their order is what is read."""

    def __init__(self):
        self.seen = []

    async def chunk(self, text, conversation_id=None):
        self.seen.append(("content", text))

    async def reasoning(self, text, conversation_id=None):
        self.seen.append(("reasoning", text))

    def of(self, kind):
        return [text for what, text in self.seen if what == kind]


def _sdk_provider(cls, engine, monkeypatch, config):
    """A vendor class, its client aimed at the double: the real SDK reads the real frames."""
    monkeypatch.setenv("TEST_PROVIDER_KEY", "not-a-real-key")
    provider = cls("vendor", {"api_key_env": "TEST_PROVIDER_KEY", **config})
    provider.client = AsyncOpenAI(api_key="k", base_url=f"http://127.0.0.1:{engine.port}/v1")
    return provider


VENDOR_CLASSES = [
    (DeepSeekProvider, {"model": "deepseek-v4-flash"}),
    (ZaiProvider, {"model": "glm-4.7"}),
    (NeuralDeepProvider, {"model": "qwen3.8-27b"}),
]


@pytest.fixture
def _neuraldeep_is_quiet(monkeypatch):
    async def _no_balance(self):
        return {"billing_mode": "wallet"}

    monkeypatch.setattr(NeuralDeepProvider, "get_balance", _no_balance)


@pytest.mark.parametrize("cls, config", VENDOR_CLASSES, ids=lambda v: getattr(v, "__name__", None))
@pytest.mark.parametrize("tools", [None, TOOLS_ANTHROPIC], ids=["stream", "tools"])
@pytest.mark.asyncio
async def test_a_vendor_class_hands_it_to_the_door_and_to_nobody_else(
    tmp_path, monkeypatch, _neuraldeep_is_quiet, cls, config, tools,
):
    async with _Engine(reasoning=REASONING, content=CONTENT) as engine:
        provider = _sdk_provider(cls, engine, monkeypatch, config)
        manager = _manager(tmp_path, provider)
        turns = [{"role": "user", "content": "hi"}]

        door = _Trace()
        result = await manager.query_messages(
            turns, tools=tools, on_chunk=door.chunk, on_reasoning=door.reasoning, return_metadata=True,
        )
        assert result["thinking"] == "".join(REASONING)
        assert door.seen[0][0] == "reasoning", "the reasoning came after what it precedes"
        assert "".join(door.of("reasoning")) == "".join(REASONING)
        assert "".join(door.of("content")) == "".join(CONTENT)
        if tools is None:
            assert door.of("reasoning") == list(REASONING)

        agent = _Trace()
        result = await manager.query_messages(turns, tools=tools, on_chunk=agent.chunk, return_metadata=True)
        assert [what for what, _ in agent.seen] == ["content"] * len(agent.seen)
        assert "".join(agent.of("content")) == "".join(CONTENT)
        assert result["thinking"] == "".join(REASONING), "the record the agent reads did not move"


@pytest.mark.parametrize("tools", [None, TOOLS_ANTHROPIC], ids=["stream", "tools"])
@pytest.mark.asyncio
async def test_the_local_class_gives_the_agent_loop_what_it_gave(tmp_path, tools):
    async with _Engine(reasoning=REASONING, content=CONTENT) as engine:
        provider = _local_provider(engine)
        try:
            manager = _manager(tmp_path, provider)
            agent = _Trace()
            result = await manager.query_messages(
                [{"role": "user", "content": "hi"}], tools=tools, on_chunk=agent.chunk, return_metadata=True,
            )
            assert [what for what, _ in agent.seen] == ["content"] * len(agent.seen)
            assert "".join(agent.of("content")) == "".join(CONTENT)
            assert result["thinking"] == "".join(REASONING)
        finally:
            _forget(provider)


@pytest.mark.asyncio
async def test_ollama_on_the_tools_path_hands_it_over_in_front_of_the_content(tmp_path, monkeypatch):
    from tests.test_ollama_says_what_the_call_cost import _answered, _make

    from dpc_client_core.providers import ollama_provider as op

    monkeypatch.setattr(op.ollama, "Client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no daemon")))
    provider = _make()
    provider.client.chat = _answered("the answer", "my notes", "stop")
    door = _Trace()
    await provider.generate_with_tools(messages=[{"role": "user", "content": "x"}], tools=[],
                                       on_chunk=door.chunk, on_reasoning=door.reasoning)
    assert door.seen == [("reasoning", "my notes"), ("content", "the answer")]


class _StoredThinking(AIProvider):
    """The shape of Anthropic's and Ollama's prompt door: no stream, the reasoning
    stored on the instance by `generate_response` and read back by the manager."""

    def __init__(self, thinking="stored notes", supports=True):
        super().__init__(ALIAS, {"type": "anthropic", "model": "m"})
        self.thinking, self.supports = thinking, supports

    def supports_thinking(self):
        return self.supports

    def get_last_thinking(self):
        return self.thinking

    async def generate_response(self, prompt, **kwargs):
        assert "on_reasoning" not in kwargs, "this entry point names no such parameter"
        return "the answer"


@pytest.mark.asyncio
async def test_a_provider_with_no_stream_hands_its_stored_reasoning_over_before_the_whole_answer(tmp_path):
    manager = _manager(tmp_path, _StoredThinking())
    door = _Trace()
    await manager.query_messages([{"role": "user", "content": "hi"}], on_chunk=door.chunk,
                                 on_reasoning=door.reasoning, return_metadata=True)
    assert door.seen == [("reasoning", "stored notes"), ("content", "the answer")]


@pytest.mark.parametrize("provider", [_StoredThinking(thinking=None), _StoredThinking(supports=False)],
                         ids=["nothing-stored", "does-not-think"])
@pytest.mark.asyncio
async def test_a_provider_with_none_to_give_sends_none(tmp_path, provider):
    manager = _manager(tmp_path, provider)
    door = _Trace()
    await manager.query_messages([{"role": "user", "content": "hi"}], on_chunk=door.chunk,
                                 on_reasoning=door.reasoning, return_metadata=True)
    assert door.seen == [("content", "the answer")]


class _StreamWithKwargs(AIProvider):
    """A stream whose `**kwargs` would swallow the callback and forward it to a vendor."""

    def __init__(self):
        super().__init__(ALIAS, {"type": "gemini", "model": "m"})
        self.kwargs = None

    async def generate_response_stream(self, prompt, on_chunk=None, conversation_id=None, **kwargs):
        self.kwargs = kwargs
        await on_chunk("the answer", conversation_id)
        return "the answer"


@pytest.mark.asyncio
async def test_an_entry_point_that_does_not_name_the_parameter_is_never_handed_it(tmp_path):
    provider = _StreamWithKwargs()
    door = _Trace()
    await _manager(tmp_path, provider).query_messages(
        [{"role": "user", "content": "hi"}], on_chunk=door.chunk, on_reasoning=door.reasoning, return_metadata=True,
    )
    assert provider.kwargs == {}
    assert door.seen == [("content", "the answer")]


def _reads_reasoning_inside(method) -> bool:
    """Whether the method's own body keeps a reasoning text: what makes a class one
    that has something to give on this path."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    return any(
        (isinstance(node, ast.Constant) and node.value in ("reasoning_content", "thinking"))
        or (isinstance(node, ast.Attribute) and node.attr == "_last_thinking")
        for node in ast.walk(tree)
    )


def test_every_class_that_keeps_reasoning_on_a_door_path_takes_the_callback_by_keyword():
    """A new provider cannot join the door's paths and stay silent unseen. By keyword
    only, so no positional caller of these signatures can be moved by it."""
    checked = []
    for cls in {c for c in PROVIDER_MAP.values() if isinstance(c, type)}:
        for name in ("generate_with_tools", "generate_response_stream"):
            method = getattr(cls, name, None)
            if method is None or not _reads_reasoning_inside(method):
                continue
            parameter = inspect.signature(method).parameters.get("on_reasoning")
            assert parameter is not None, f"{cls.__name__}.{name} keeps reasoning and has no way to hand it over"
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY and parameter.default is None
            checked.append(f"{cls.__name__}.{name}")
    assert {"LlamaServerProvider.generate_response_stream", "LlamaServerProvider.generate_with_tools",
            "DeepSeekProvider.generate_response_stream", "DeepSeekProvider.generate_with_tools",
            "ZaiProvider.generate_response_stream", "ZaiProvider.generate_with_tools",
            "NeuralDeepProvider.generate_response_stream", "NeuralDeepProvider.generate_with_tools",
            "OllamaProvider.generate_with_tools"} <= set(checked)


# --- (10) with defect B: the reasoning is what now notices the client leaving ----------


@pytest.mark.asyncio
async def test_a_client_gone_during_the_reasoning_is_found_by_the_reasoning_write_and_nothing_runs_again(
    tmp_path, fast_backoff, no_handler_cancellation,  # noqa: F811
):
    many = tuple(f"thought{i} " for i in range(60))
    lock = asyncio.Semaphore(1)
    async with _Engine(reasoning=many, content=CONTENT, delay=0.02) as engine:
        provider = _local_provider(engine)
        try:
            async with _running(tmp_path, _service(tmp_path, provider), inference_lock=lock) as (server, _):
                client = _Client(server.port, _key(tmp_path), CHAT_PATH, _chat(stream=True))
                await client.send()
                await client.read_until(b"thought1 ")
                await client.leave()
                await _until(lambda: not lock.locked())
                assert await _settled(engine) == 1, "the answer was generated again for nobody"
                assert engine.left == 1, "the engine went on reasoning for nobody"
        finally:
            _forget(provider)


# --- (11) an answer that is the reasoning is said once --------------------------------


def _ollama_at_the_door(monkeypatch, content, thinking):
    from tests.test_ollama_says_what_the_call_cost import _answered, _Resp

    from dpc_client_core.providers import ollama_provider as op

    monkeypatch.setattr(op.ollama, "Client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no daemon")))
    provider = op.OllamaProvider(ALIAS, {"type": "ollama", "model": "m", "host": "http://127.0.0.1:11434"})
    provider.supports_thinking = lambda: True
    provider.client.chat = _answered(content, thinking, "stop")
    assert _Resp
    return provider


NO_ANSWER_BODIES = [_chat(), _chat(stream=True), _chat(tools=TOOLS_OPENAI), _chat(stream=True, tools=TOOLS_OPENAI)]


@pytest.mark.parametrize("body", NO_ANSWER_BODIES, ids=["plain", "plain-stream", "tools", "tools-stream"])
@pytest.mark.asyncio
async def test_ollama_with_no_answer_promotes_its_reasoning_once_as_the_answer(tmp_path, monkeypatch, body):
    """`generate_response` and `generate_with_tools` return the reasoning as the content when
    the model answered nothing. It is the answer then, and it is not also reasoning."""
    provider = _ollama_at_the_door(monkeypatch, "", "only my notes")
    async with _running(tmp_path, _service(tmp_path, provider)) as (server, _):
        status, text = await _post(server, tmp_path, CHAT_PATH, body)
    assert status == 200, text
    assert "reasoning_content" not in text
    assert text.count("only my notes") == 1


@pytest.mark.parametrize("body", NO_ANSWER_BODIES, ids=["plain", "plain-stream", "tools", "tools-stream"])
@pytest.mark.asyncio
async def test_ollama_with_an_answer_hands_its_reasoning_over_apart_from_it(tmp_path, monkeypatch, body):
    provider = _ollama_at_the_door(monkeypatch, "the answer", "my notes")
    async with _running(tmp_path, _service(tmp_path, provider)) as (server, _):
        status, text = await _post(server, tmp_path, CHAT_PATH, body)
    assert status == 200, text
    assert text.count("my notes") == 1 and "reasoning_content" in text
    assert text.count("the answer") == 1


@pytest.mark.asyncio
async def test_a_host_that_promoted_its_reasoning_is_not_read_twice_at_the_guests_door(tmp_path):
    same = _priced_result(response="only my notes", thinking="only my notes")
    _, text = await _peer_call(tmp_path, _chat(REMOTE_MODEL), result=same)
    message = json.loads(text)["choices"][0]["message"]
    assert message["content"] == "only my notes" and "reasoning_content" not in message
    _, text = await _peer_call(tmp_path, _chat(REMOTE_MODEL, stream=True), chunks=["only my notes"], result=same)
    assert "reasoning_content" not in text
