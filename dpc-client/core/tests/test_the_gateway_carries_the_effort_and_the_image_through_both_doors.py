"""The gateway carries reasoning effort and images, or refuses them by name.

The peer wire under this door has carried both since before the door existed
— `create_remote_inference_request(..., images, reasoning_effort)` — and the
door dropped them: no parser read an effort field in either shape, and an
image was refused on the OpenAI form and flattened away to nothing on the
Anthropic one. A probe sending `"reasoning_effort": "banana"` was answered
200 with the model's text (2026-09-14), which is the shape of the defect:
not an error, an answer to a question nobody asked.

What is asserted here is the rule the gateway's docstring states — every
degradation is said on the wire. A word off this node's scale is a 400
listing the words; a known word reaches the provider on the local route and
the host on the peer route; `thinking: {type: disabled}` is `off`, and
`enabled` names no depth and so asks for the alias's own default. A `data:`
image becomes the two fields DPTP §3.4 requires and travels beside the
prompt; an `http` URL, an oversized image, tools beside an image, an alias
or a peer with no vision path, and a peer whose menu lists other effort
words are each refused by name before anything is sent.

The stand-in service, the running listener and the key are the OpenAI-shape
test file's; the provider doubles for the door's own three paths are the
message-shaped door's. Cross-platform: pure asyncio.
"""

import base64
import json
import types

import pytest

from dpc_client_core.gateway import KNOWN_EFFORTS
from dpc_client_core.llm_manager import accepts_reasoning_effort, entry_point_for
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    PEER,
    REMOTE_ALIAS,
    REMOTE_MODEL,
    REMOTE_VISION_ALIAS,
    _peer_service,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    BOTH_LISTS,
    LOCAL,
    MAX_IMAGE_MB,
    _Provider,
    _chat,
    _key,
    _providers,
    _request,
    _running,
    _service,
)
from tests.test_the_gateway_speaks_the_anthropic_messages_form_over_the_same_door import (
    _anthropic_error,
    _messages,
    _post_messages,
)
from tests.test_the_message_shaped_door_reaches_the_provider_unflattened import (
    MESSAGES,
    SYSTEM,
    _Plain,
    _Streaming,
    _WithTools,
    _manager,
)

PIXEL = "iVBORw0KGgoAAAANSUhEUg=="
REMOTE_VISION_MODEL = f"remote:{PEER}:{REMOTE_VISION_ALIAS}"


def _openai_error(text):
    body = json.loads(text)
    return body["error"]


def _image_part(url=f"data:image/png;base64,{PIXEL}"):
    return {"type": "image_url", "image_url": {"url": url}}


def _chat_with_image(model, url=f"data:image/png;base64,{PIXEL}", **extra):
    body = {"model": model, "messages": [
        {"role": "user", "content": [{"type": "text", "text": "what is this"}, _image_part(url)]},
    ]}
    body.update(extra)
    return body


def _messages_with_image(model, source=None, **extra):
    source = source or {"type": "base64", "media_type": "image/png", "data": PIXEL}
    return _messages(model, [
        {"type": "text", "text": "what is this"}, {"type": "image", "source": source},
    ], **extra)


def _echoing_service(tmp_path, compute=BOTH_LISTS, **kwargs):
    """The loopback stand-in whose doors report the effort they were given, as
    the real ones do — `served_effort` is what lands on the row."""
    service = _service(tmp_path, compute, **kwargs)
    inner_messages, inner_query = service.llm_manager.query_messages, service.llm_manager.query

    async def query_messages(messages, **kw):
        return dict(await inner_messages(messages, **kw), served_effort=kw.get("reasoning_effort"))

    async def query(prompt, **kw):
        return dict(await inner_query(prompt, **kw), served_effort=kw.get("reasoning_effort"))

    service.llm_manager.query_messages = query_messages
    service.llm_manager.query = query
    return service


# --- (1) an effort word off this node's scale is refused, in either envelope -------


@pytest.mark.asyncio
async def test_an_unknown_effort_word_is_400_naming_the_words_this_node_knows(tmp_path):
    """The live probe's case: `banana` was answered 200 with the model's text."""
    service = _echoing_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path),
                                      body=_chat(LOCAL, reasoning_effort="banana"))
        assert status == 400, text
        message = _openai_error(text)["message"]
        assert "banana" in message
        for word in KNOWN_EFFORTS:
            assert word in message, f"the refusal does not name {word}"

        assert service.calls == [], "a refused request reached the door"
        assert list(ledger.rows()) == [], "a refused request left a usage row"


@pytest.mark.asyncio
async def test_an_unknown_effort_word_on_the_messages_form_is_400_in_the_anthropic_envelope(tmp_path):
    service = _echoing_service(tmp_path)
    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_messages(
            server, _messages(LOCAL, output_config={"effort": "banana"}), key=_key(tmp_path),
        )
        assert status == 400, text
        message = _anthropic_error(text)["message"]
        assert "output_config.effort" in message and "banana" in message
        assert all(word in message for word in KNOWN_EFFORTS)
        assert service.calls == []


# --- (2) a known word reaches the provider, on either form -------------------------


@pytest.mark.asyncio
async def test_a_known_word_reaches_the_local_door_and_lands_on_the_row(tmp_path):
    service = _echoing_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path),
                                      body=_chat(LOCAL, reasoning_effort="high"))
        assert status == 200, text
        assert service.calls[0]["kwargs"]["reasoning_effort"] == "high"

        (row,) = list(ledger.rows())
        assert (row["route"], row["served_effort"]) == ("local", "high")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body_kwargs, expected",
    [
        ({"output_config": {"effort": "high"}}, "high"),
        ({"output_config": {"effort": "xhigh"}}, "high"),
        ({"thinking": {"type": "disabled"}}, "off"),
        ({"thinking": {"type": "enabled", "budget_tokens": 2048}}, None),
        ({"thinking": {"type": "adaptive"}}, None),
        ({}, None),
    ],
    ids=["a word", "xhigh is read as high", "thinking disabled is off",
         "a budget names no depth", "adaptive names no depth", "nothing asked"],
)
async def test_the_messages_form_reads_its_own_two_fields(tmp_path, body_kwargs, expected):
    """`budget_tokens` is a quantity this node's scale cannot express, so an
    enabled `thinking` asks for the alias's own default rather than a word."""
    service = _echoing_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(server, _messages(LOCAL, **body_kwargs), key=_key(tmp_path))
        assert status == 200, text
        assert service.calls[0]["kwargs"]["reasoning_effort"] == expected

        (row,) = list(ledger.rows())
        assert row["served_effort"] == expected


@pytest.mark.asyncio
async def test_a_disabled_thinking_beside_an_effort_word_is_a_contradiction_and_is_refused(tmp_path):
    service = _echoing_service(tmp_path)
    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_messages(
            server,
            _messages(LOCAL, thinking={"type": "disabled"}, output_config={"effort": "high"}),
            key=_key(tmp_path),
        )
        assert status == 400, text
        assert "contradict" in _anthropic_error(text)["message"]
        assert service.calls == []


# --- (3) the peer route: the word travels, and the peer's own words bound it -------


@pytest.mark.asyncio
async def test_a_known_word_travels_to_the_host_on_the_peer_route(tmp_path):
    service = _peer_service(tmp_path)
    async with _running(tmp_path, service) as (server, _):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path),
                                      body=_chat(REMOTE_MODEL, reasoning_effort="low"))
        assert status == 200, text
        (call,) = service.peer_calls
        assert call["reasoning_effort"] == "low", "the host was asked at this node's default instead"


@pytest.mark.asyncio
async def test_a_word_the_peers_menu_does_not_list_is_refused_before_the_send(tmp_path):
    """`reasoning_words` is the peer's own word about what its model knows."""
    service = _peer_service(tmp_path)
    service.peer_metadata[PEER]["providers"][0]["reasoning_words"] = ["low", "medium", "high"]
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path),
                                      body=_chat(REMOTE_MODEL, reasoning_effort="max"))
        assert status == 400, text
        message = _openai_error(text)["message"]
        assert "max" in message and "low, medium, high" in message

        assert service.peer_calls == [], "the request was sent anyway"
        assert list(ledger.rows()) == []


@pytest.mark.asyncio
async def test_off_is_not_checked_against_the_models_rungs(tmp_path):
    """`off` is the foot of the scale, not a rung: every provider has its own
    way of saying no, and a template's word list does not name it."""
    service = _peer_service(tmp_path)
    service.peer_metadata[PEER]["providers"][0]["reasoning_words"] = ["low", "medium", "high"]
    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_messages(
            server, _messages(REMOTE_MODEL, thinking={"type": "disabled"}), key=_key(tmp_path),
        )
        assert status == 200, text
        assert service.peer_calls[0]["reasoning_effort"] == "off"


# --- (4) the door itself: the word reaches the path, or the path says it cannot ----


@pytest.mark.asyncio
async def test_the_message_door_hands_the_word_to_the_provider_and_reports_it(tmp_path):
    provider = _Plain()
    manager = _manager(tmp_path, provider)

    result = await manager.query_messages(MESSAGES, system=SYSTEM, return_metadata=True,
                                          reasoning_effort="high")

    assert result["served_effort"] == "high"
    assert provider.efforts == ["high"], "the word did not reach generate_response"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider, tools", [(_Streaming(), None), (_WithTools(), [{"name": "t"}])],
                         ids=["a stream that takes no effort", "a tools path that takes no effort"])
async def test_a_path_that_cannot_take_the_word_raises_rather_than_dropping_it(tmp_path, provider, tools):
    """`ZaiProvider.generate_with_tools` is the live case: no parameter and no
    `**kwargs`, so the word cannot be sent and must not be silently lost."""
    manager = _manager(tmp_path, provider)

    async def on_chunk(*args):
        return None

    with pytest.raises(ValueError) as refused:
        await manager.query_messages(MESSAGES, system=SYSTEM, tools=tools, on_chunk=on_chunk,
                                     return_metadata=True, reasoning_effort="high")
    assert "high" in str(refused.value)


def test_the_door_and_the_gateway_ask_the_same_question_of_a_path():
    """The gateway refuses in front of the door, so both must read one answer."""
    assert entry_point_for(_Plain(), tools=False, streaming=False)[0] == "generate_response"
    assert entry_point_for(_Streaming(), tools=False, streaming=True)[0] == "generate_response_stream"
    assert entry_point_for(_WithTools(), tools=True, streaming=False)[0] == "generate_with_tools"
    assert accepts_reasoning_effort(_Plain().generate_response) is True
    assert accepts_reasoning_effort(_Streaming().generate_response_stream) is False
    assert accepts_reasoning_effort(None) is False


@pytest.mark.asyncio
async def test_an_alias_whose_path_takes_no_effort_is_refused_by_name_at_the_gateway(tmp_path):
    class _NoEffort(_Provider):
        async def generate_response(self, prompt):  # no effort, no **kwargs
            raise AssertionError("the gateway refuses before the call")

    providers = dict(_providers(), **{LOCAL: _NoEffort("ollama", "qwen3:8b")})
    service = _echoing_service(tmp_path, providers=providers)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path),
                                      body=_chat(LOCAL, reasoning_effort="high"))
        assert status == 400, text
        message = _openai_error(text)["message"]
        assert "generate_response" in message and "high" in message
        assert service.calls == [] and list(ledger.rows()) == []


class _TemplateLadder(_Provider):
    """An alias whose rungs are its model's own, as llamacpp_server's are when
    the gguf's template named them: three words, and `high`/`max` folded onto
    the top one."""

    WORDS = ["low", "medium", "xhigh"]

    def __init__(self, type_="llamacpp_server", model="qwen3.8-27b", default="xhigh", configured=None):
        super().__init__(type_, model)
        self._template_efforts = tuple(self.WORDS)
        self._template_efforts_source = "model"
        self._template_default = default
        if configured is not None:
            self.config["reasoning_effort"] = configured

    def _template_effort(self, requested):
        word = (requested or "").strip().lower()
        if word in ("high", "max"):
            return "xhigh"
        return word if word in self._template_efforts else None


def _ladder_service(tmp_path, **kwargs):
    providers = dict(_providers(), **{LOCAL: _TemplateLadder(**kwargs)})
    return _echoing_service(tmp_path, providers=providers)


# --- (5) the words are the alias's own, and the row names the rung it ran on -------


@pytest.mark.asyncio
async def test_a_word_this_alias_reaches_no_rung_from_is_refused_naming_its_own_words(tmp_path):
    """The scale is per alias: `off` and the shared four pass the shape layer,
    and this model knows three words of its own."""
    service = _ladder_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(
            server, _messages(LOCAL, output_config={"effort": "medium"}), key=_key(tmp_path),
        )
        assert status == 200, f"a word the model's own template lists was refused: {text}"

        service.calls.clear()
        served = len(list(ledger.rows()))
        service.llm_manager.providers[LOCAL]._template_efforts = ("thorough",)  # nothing folds onto it
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path),
                                      body=_chat(LOCAL, reasoning_effort="low"))
        assert status == 400, text
        assert "thorough" in _openai_error(text)["message"]
        assert service.calls == [] and len(list(ledger.rows())) == served


@pytest.mark.asyncio
async def test_the_aliass_own_top_rung_reaches_the_provider_unfolded_and_names_the_row(tmp_path):
    """The live regression: `xhigh` is this model's top rung and was folded to
    `high` on the shared scale before anyone asked the alias, so the alias then
    refused its own word (2026-09-14)."""
    service = _ladder_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path),
                                      body=_chat(LOCAL, reasoning_effort="xhigh"))
        assert status == 200, text
        assert service.calls[0]["kwargs"]["reasoning_effort"] == "xhigh", "the word was folded on the way"

        (row,) = list(ledger.rows())
        assert row["served_effort"] == "xhigh"


@pytest.mark.asyncio
@pytest.mark.parametrize("asked", ["high", "max", "banana"],
                         ids=["a shared-scale word this ladder lacks", "the shared scale's top",
                              "a word off every scale"])
async def test_a_word_this_ladder_lacks_is_refused_in_this_ladders_words(tmp_path, asked):
    """One door, one dictionary: whether the word happens to sit on the shared
    scale decides nothing, because the alias was resolved before it was judged."""
    service = _ladder_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path),
                                      body=_chat(LOCAL, reasoning_effort=asked))
        assert status == 400, text
        message = _openai_error(text)["message"]
        assert asked in message and "low, medium, xhigh" in message
        assert "xhigh is read as high" not in message, "the shared scale answered for an alias with words"

        assert service.calls == [] and list(ledger.rows()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({}, "xhigh"),
        ({"configured": "low"}, "low"),
        ({"configured": "max"}, "xhigh"),
        ({"default": None}, None),
    ],
    ids=["the template's default", "the alias's configured word",
         "a configured word folded onto a rung", "a template that named no default"],
)
async def test_a_row_for_a_call_that_asked_nothing_names_the_aliass_own_word(tmp_path, kwargs, expected):
    """A caller who asks for nothing still gets some depth, and the row says
    which when the alias makes it knowable."""
    service = _ladder_service(tmp_path, **kwargs)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(LOCAL))
        assert status == 200, text
        assert service.calls[0]["kwargs"]["reasoning_effort"] is None, "a word was sent that nobody asked for"

        (row,) = list(ledger.rows())
        assert row["served_effort"] == expected


def test_one_source_answers_what_words_an_alias_knows(tmp_path):
    """The menu row, the UI row and the door's refusal read one function."""
    from dpc_client_core.providers.base import declared_reasoning_words, reasoning_word_for

    ladder = _TemplateLadder()
    assert declared_reasoning_words(ladder) == (_TemplateLadder.WORDS, "xhigh")
    assert declared_reasoning_words(_Provider("ollama", "qwen3:8b")) == (None, None)
    assert reasoning_word_for(ladder, "max") == "xhigh"
    assert reasoning_word_for(_Provider("ollama", "qwen3:8b"), "xhigh") == "high"


# --- (6) an image crosses as the two fields the wire requires ----------------------


@pytest.mark.asyncio
async def test_a_data_url_image_reaches_the_local_door_as_the_wires_two_fields(tmp_path):
    service = _echoing_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat_with_image(LOCAL))
        assert status == 200, text

        (call,) = service.calls
        assert call["kwargs"]["images"] == [{"base64": PIXEL, "mime_type": "image/png"}]
        assert "prompt" in call, "an image took the message door, which carries none"
        assert "what is this" in call["prompt"], "the text did not travel beside the image"
        assert list(ledger.rows())[0]["route"] == "local"


@pytest.mark.asyncio
async def test_an_anthropic_image_block_reaches_the_same_door_the_same_way(tmp_path):
    service = _echoing_service(tmp_path)
    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_messages(server, _messages_with_image(LOCAL), key=_key(tmp_path))
        assert status == 200, text

        (call,) = service.calls
        assert call["kwargs"]["images"] == [{"base64": PIXEL, "mime_type": "image/png"}]
        assert "what is this" in call["prompt"]


@pytest.mark.asyncio
async def test_a_turn_that_is_only_an_image_is_served_rather_than_called_empty(tmp_path):
    """«No message carries text» is about a request carrying nothing at all; a
    picture with no question is a question."""
    service = _echoing_service(tmp_path)
    body = {"model": LOCAL, "messages": [{"role": "user", "content": [_image_part()]}]}
    async with _running(tmp_path, service) as (server, _):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=body)
        assert status == 200, text
        (call,) = service.calls
        assert call["kwargs"]["images"] == [{"base64": PIXEL, "mime_type": "image/png"}]
        assert call["prompt"] == ""


@pytest.mark.asyncio
async def test_an_image_travels_to_the_peer_beside_the_prompt(tmp_path):
    service = _peer_service(tmp_path)
    async with _running(tmp_path, service) as (server, _):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat_with_image(REMOTE_VISION_MODEL))
        assert status == 200, text
        (call,) = service.peer_calls
        assert call["images"] == [{"base64": PIXEL, "mime_type": "image/png"}]
        assert "what is this" in call["prompt"]


# --- (7) what cannot cross is refused by name, before anything is sent -------------


@pytest.mark.asyncio
async def test_a_web_url_is_refused_in_either_form_rather_than_fetched(tmp_path):
    service = _echoing_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path),
                                      body=_chat_with_image(LOCAL, url="https://example.com/cat.png"))
        assert status == 400, text
        assert "fetches nothing from the web" in _openai_error(text)["message"]

        status, text = await _post_messages(
            server,
            _messages_with_image(LOCAL, source={"type": "url", "url": "https://example.com/cat.png"}),
            key=_key(tmp_path),
        )
        assert status == 400, text
        assert "fetches nothing from the web" in _anthropic_error(text)["message"]

        assert service.calls == [] and list(ledger.rows()) == []


@pytest.mark.asyncio
async def test_an_image_past_the_wires_cap_is_413_in_either_form(tmp_path):
    """The cap is the one the settings name, not one written here: this node
    says one megabyte and the same image that crosses at the shipped five
    does not cross at one."""
    service = _echoing_service(tmp_path)
    assert service.settings.get_vision_max_image_size_mb() == MAX_IMAGE_MB
    service.settings.get_vision_max_image_size_mb = lambda: 1
    oversize = base64.b64encode(b"\0" * (1024 * 1024 + 1)).decode("ascii")
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path),
                                      body=_chat_with_image(LOCAL, url=f"data:image/png;base64,{oversize}"))
        assert status == 413, text
        assert "max_image_size_mb" in _openai_error(text)["message"]

        status, text = await _post_messages(
            server,
            _messages_with_image(LOCAL, source={"type": "base64", "media_type": "image/png", "data": oversize}),
            key=_key(tmp_path),
        )
        assert status == 413, text
        assert _anthropic_error(text)["type"] == "request_too_large"

        assert service.calls == [] and list(ledger.rows()) == []


@pytest.mark.asyncio
async def test_a_peer_whose_menu_row_says_no_vision_is_refused_before_the_send(tmp_path):
    service = _peer_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat_with_image(REMOTE_MODEL))
        assert status == 400, text
        message = _openai_error(text)["message"]
        assert "supports_vision" in message and REMOTE_ALIAS in message

        assert service.peer_calls == [], "the image was sent to a peer that said it has no vision"
        assert list(ledger.rows()) == []


@pytest.mark.asyncio
async def test_an_alias_with_no_vision_path_is_refused_by_name(tmp_path):
    class _Blind(_Provider):
        def supports_vision(self):
            return False

    providers = dict(_providers(), **{LOCAL: _Blind("ollama", "qwen3:8b")})
    service = _echoing_service(tmp_path, providers=providers)
    async with _running(tmp_path, service) as (server, _):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat_with_image(LOCAL))
        assert status == 400, text
        assert "no vision path" in _openai_error(text)["message"]
        assert service.calls == []


@pytest.mark.asyncio
async def test_tools_beside_an_image_are_refused_rather_than_one_of_them_dropped(tmp_path):
    service = _echoing_service(tmp_path)
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    async with _running(tmp_path, service) as (server, _):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path),
                                      body=_chat_with_image(LOCAL, tools=tools))
        assert status == 400, text
        message = _openai_error(text)["message"]
        assert "image" in message and "tool" in message
        assert service.calls == []


@pytest.mark.asyncio
async def test_a_part_that_is_neither_text_nor_an_image_is_still_refused_by_name(tmp_path):
    service = _echoing_service(tmp_path)
    body = {"model": LOCAL, "messages": [
        {"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "AA=="}}]},
    ]}
    async with _running(tmp_path, service) as (server, _):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path), body=body)
        assert status == 400, text
        assert "input_audio" in _openai_error(text)["message"]
        assert service.calls == []


# --- (7) one dictionary per alias, on either route --------------------------------


def _peer_row_words(service, words):
    """The menu row the gateway reads for REMOTE_ALIAS, given its own words or
    none. The row is this peer's copy, so the edit reaches no other test."""
    row = service.peer_metadata[PEER]["providers"][0]
    if words is None:
        row.pop("reasoning_words", None)
    else:
        row["reasoning_words"] = list(words)
    return row


ROW_WORDS = ["xhigh", "medium", "low"]


@pytest.mark.asyncio
async def test_the_peers_own_top_word_crosses_unfolded(tmp_path):
    """The Linux guest's case: `xhigh` is on the row and was folded to `high`
    before the row was read, so the host was asked for a word it never named."""
    service = _peer_service(tmp_path)
    _peer_row_words(service, ROW_WORDS)
    async with _running(tmp_path, service) as (server, _):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path),
                                      body=_chat(REMOTE_MODEL, reasoning_effort="xhigh"))
        assert status == 200, text
        assert service.peer_calls[0]["reasoning_effort"] == "xhigh"


@pytest.mark.asyncio
@pytest.mark.parametrize("asked", ["high", "banana"],
                         ids=["a shared-scale word the row lacks", "a word off every scale"])
async def test_a_word_the_peers_row_lacks_is_refused_in_the_rows_words(tmp_path, asked):
    """`banana` was refused listing the shared scale and `xhigh` listing the
    row's words, on the same alias and the same call path (2026-09-14)."""
    service = _peer_service(tmp_path)
    _peer_row_words(service, ROW_WORDS)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path),
                                      body=_chat(REMOTE_MODEL, reasoning_effort=asked))
        assert status == 400, text
        message = _openai_error(text)["message"]
        assert asked in message and "xhigh, medium, low" in message
        assert "xhigh is read as high" not in message, "the shared scale answered for a row with words"

        assert service.peer_calls == [] and list(ledger.rows()) == []


@pytest.mark.asyncio
async def test_a_row_that_names_no_words_is_the_shared_scale_and_folds_xhigh(tmp_path):
    """Where the host said nothing about its model's rungs there is no other
    ladder to stand on, and `xhigh` reads as `high` as it always has."""
    service = _peer_service(tmp_path)
    _peer_row_words(service, None)
    async with _running(tmp_path, service) as (server, _):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path),
                                      body=_chat(REMOTE_MODEL, reasoning_effort="xhigh"))
        assert status == 200, text
        assert service.peer_calls[0]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_a_word_off_every_scale_on_a_wordless_row_lists_the_shared_scale(tmp_path):
    service = _peer_service(tmp_path)
    _peer_row_words(service, None)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path),
                                      body=_chat(REMOTE_MODEL, reasoning_effort="banana"))
        assert status == 400, text
        message = _openai_error(text)["message"]
        assert "banana" in message
        assert all(word in message for word in KNOWN_EFFORTS)

        assert service.peer_calls == [] and list(ledger.rows()) == []


@pytest.mark.asyncio
async def test_the_messages_form_names_its_own_field_in_an_aliass_refusal(tmp_path):
    """The field name survives the move of the check from the shape layer to
    the door: a client that wrote `output_config.effort` is answered about it."""
    service = _ladder_service(tmp_path)
    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_messages(
            server, _messages(LOCAL, output_config={"effort": "banana"}), key=_key(tmp_path),
        )
        assert status == 400, text
        message = _anthropic_error(text)["message"]
        assert "output_config.effort" in message and "low, medium, xhigh" in message
