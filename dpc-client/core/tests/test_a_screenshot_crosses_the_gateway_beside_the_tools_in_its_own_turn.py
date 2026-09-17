"""A screenshot crosses the gateway beside the tools, in the turn it was sent in.

Claude Code attaches its tools to every request, so a screenshot is images and
tools in one call with the system prompt and the history intact. The gateway
refused that combination with 400 `tools_unsupported`, and because an image
was lifted out of every user turn of the history, every later turn of the same
session was refused too — confirmed live on the peer route: a text-only turn
whose history held one image and the tools came back 400. The first step
(35f54909) taught the tools path to carry an image in its turn; this is the
local route's half of the second, and the peer route's half of the third.

Pinned here, on the local route: an image stays an `image` block at its place
in its user turn, in both HTTP forms, and a request with tools takes
`query_messages` with those turns — system, history, tools and effort as for
any tools call — while a request without tools still takes `query` with the
flat list. The refusal in front of the door asks `entry_point_for`, the
predicate `query_messages` asks, so an alias with no tool path and an alias
that cannot see are both refused with `tools_unsupported` and a sentence
naming which. The effort check follows the same path. On the peer route a
request with tools is sent the turns with their image blocks and no flat list,
but only to a host whose menu row says `serves_images_with_tools`; a row that
says nothing is an older host and is refused before the round trip, fail-closed,
because an older host drops the pictures. Without tools nothing changes: the
turns sent carry no image block and the flat `images` carries the picture. One
test runs the guest's gateway into a real host coordinator, whose own row
decides and whose provider receives the screenshot in its turn.

The stand-in services are the gateway test files' own; one test runs the real
`LLMManager.query_messages` and the real converter, so what is asserted is
what a provider receives. Cross-platform: pure asyncio.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dpc_protocol.protocol import PeerRefused, create_remote_inference_request

from dpc_client_core.gateway import _turns_without_images
from dpc_client_core.llm_manager import flatten_messages
from dpc_client_core.message_handlers.inference_handler import RemoteInferenceRequestHandler
from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.service import CoreService
from dpc_client_core.providers import AIProvider
from dpc_client_core.providers.base import anthropic_to_openai_messages
from tests.test_p2p_coordinator import make_coordinator
from tests.test_the_gateway_carries_the_effort_and_the_image_through_both_doors import (
    PIXEL,
    REMOTE_VISION_MODEL,
    _echoing_service,
    _openai_error,
)
from tests.test_the_gateway_carries_tool_calls_and_streams_in_both_forms import (
    CALL,
    OPENAI_READ_FILE,
    READ_FILE,
    _data_lines,
    _stream_text,
    _tool_service,
)
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    PEER,
    REMOTE_VISION_ALIAS,
    _peer_service,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    LOCAL,
    _Provider,
    _key,
    _providers,
    _request,
    _running,
    _service,
)
from tests.test_the_gateway_speaks_the_anthropic_messages_form_over_the_same_door import (
    _anthropic_error,
    _post_messages,
    _sse_events,
)
from tests.test_the_message_shaped_door_reaches_the_provider_unflattened import _manager

SYSTEM = "you are a coding agent"
OTHER_PIXEL = "R0lGODlhAQABAAAAACw="
SHOT = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PIXEL}}
OTHER_SHOT = {"type": "image", "source": {"type": "base64", "media_type": "image/gif", "data": OTHER_PIXEL}}
WIRE_SHOT = {"base64": PIXEL, "mime_type": "image/png"}
WIRE_OTHER_SHOT = {"base64": OTHER_PIXEL, "mime_type": "image/gif"}


def _messages_body(model, messages, **extra):
    body = {"model": model, "max_tokens": 256, "system": SYSTEM, "messages": messages}
    body.update(extra)
    return body


def _chat_body(model, messages, **extra):
    body = {"model": model, "messages": [{"role": "system", "content": SYSTEM}, *messages]}
    body.update(extra)
    return body


def _data_url(data=PIXEL, mime="image/png"):
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


# The live incident's second form: the screenshot is in the history only, the
# turn being asked is text, and the tools are attached as on every turn.
HISTORY_WITH_A_SCREENSHOT = [
    {"role": "user", "content": [{"type": "text", "text": "what is wrong on this screen?"}, SHOT]},
    {"role": "assistant", "content": [{"type": "text", "text": "the button is cut off"}]},
    {"role": "user", "content": [{"type": "text", "text": "fix it"}]},
]


def _images_in(messages):
    """(turn index, block) for every top-level image block, in order."""
    return [(index, block) for index, turn in enumerate(messages)
            if isinstance(turn.get("content"), list)
            for block in turn["content"] if block.get("type") == "image"]


# --- (1) the Messages form: the image stays in its turn and takes the tools path -----


@pytest.mark.asyncio
async def test_a_screenshot_with_text_and_tools_reaches_query_messages_in_its_turn_with_the_system_intact(tmp_path):
    service = _service(tmp_path, {"serving_local": [LOCAL]})
    body = _messages_body(LOCAL, [
        {"role": "user", "content": [SHOT, {"type": "text", "text": "what is wrong here?"}]},
    ], tools=[READ_FILE])
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(server, body, key=_key(tmp_path))
        assert status == 200, text

        (call,) = service.calls
        assert "prompt" not in call, "images beside tools took the vision door, which holds no tools"
        assert call["messages"] == body["messages"], "the turn did not reach the door as it was sent"
        assert call["system"] == SYSTEM
        assert call["kwargs"]["tools"] == [READ_FILE]
        assert "images" not in call["kwargs"], "the flat list was sent beside turns that already hold it"
        (row,) = ledger.rows()
        assert row["route"] == "local"


@pytest.mark.asyncio
async def test_a_text_turn_whose_history_holds_a_screenshot_is_served_with_the_tools_not_refused(tmp_path):
    """The incident: every turn after the screenshot came back 400."""
    service = _service(tmp_path, {"serving_local": [LOCAL]})
    body = _messages_body(LOCAL, HISTORY_WITH_A_SCREENSHOT, tools=[READ_FILE])
    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_messages(server, body, key=_key(tmp_path))
        assert status == 200, text

        (call,) = service.calls
        assert call["messages"] == HISTORY_WITH_A_SCREENSHOT
        assert _images_in(call["messages"]) == [(0, SHOT)], "the screenshot left the turn it was taken in"
        assert call["kwargs"]["tools"] == [READ_FILE]


@pytest.mark.asyncio
async def test_two_screenshots_in_two_turns_each_keep_their_own_turn(tmp_path):
    service = _service(tmp_path, {"serving_local": [LOCAL]})
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "before"}, SHOT]},
        {"role": "assistant", "content": "I see the old layout"},
        {"role": "user", "content": [OTHER_SHOT, {"type": "text", "text": "after"}]},
    ]
    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_messages(server, _messages_body(LOCAL, messages, tools=[READ_FILE]),
                                            key=_key(tmp_path))
        assert status == 200, text
        (call,) = service.calls
        assert _images_in(call["messages"]) == [(0, SHOT), (2, OTHER_SHOT)]


# --- (2) the OpenAI form: an image_url part becomes an image block at its place --------


@pytest.mark.asyncio
async def test_an_openai_image_url_beside_tools_becomes_an_image_block_in_its_turn_in_the_order_written(tmp_path):
    service = _service(tmp_path, {"serving_local": [LOCAL]})
    body = _chat_body(LOCAL, [
        {"role": "user", "content": [{"type": "text", "text": "compare"}, _data_url(),
                                     {"type": "text", "text": "with the design"}]},
        {"role": "assistant", "content": "they differ"},
        {"role": "user", "content": "fix it"},
    ], tools=[OPENAI_READ_FILE])
    async with _running(tmp_path, service) as (server, _):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path), body=body)
        assert status == 200, text

        (call,) = service.calls
        assert "prompt" not in call
        assert call["system"] == SYSTEM
        assert call["messages"] == [
            {"role": "user", "content": [
                {"type": "text", "text": "compare"}, SHOT, {"type": "text", "text": "with the design"},
            ]},
            {"role": "assistant", "content": [{"type": "text", "text": "they differ"}]},
            {"role": "user", "content": "fix it"},
        ]
        assert call["kwargs"]["tools"] == [READ_FILE]


# --- (3) the real door: the provider sees the image in its turn --------------------------


class _SeeingToolProvider(AIProvider):
    """A provider with a tools path and eyes, running the shared converter as
    the real ones do, and keeping what it would have sent."""

    def __init__(self):
        super().__init__(LOCAL, {"type": "ollama", "model": "qwen3.8-vl"})
        self.sent = []

    def supports_vision(self):
        return True

    async def generate_response(self, prompt, **kwargs):
        raise AssertionError("images beside tools must not be flattened into a prompt")

    async def generate_with_tools(self, messages, tools, system="", on_chunk=None, conversation_id=None):
        self.sent.append({"messages": anthropic_to_openai_messages(system, messages, provider=self),
                          "tools": tools})
        return {"content": "the button is cut off", "tool_calls_raw": [], "thinking": None, "usage": {}}


@pytest.mark.asyncio
async def test_through_the_real_door_the_provider_is_sent_the_screenshot_in_the_turn_it_was_taken_in(tmp_path):
    provider = _SeeingToolProvider()
    service = _service(tmp_path, {"serving_local": [LOCAL]}, providers={LOCAL: provider})
    service.llm_manager = _manager(tmp_path, provider, alias=LOCAL)
    body = _chat_body(LOCAL, [
        {"role": "user", "content": [{"type": "text", "text": "what is wrong?"}, _data_url()]},
        {"role": "assistant", "content": "the button"},
        {"role": "user", "content": "fix it"},
    ], tools=[OPENAI_READ_FILE])
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path), body=body)
        assert status == 200, text
        assert json.loads(text)["choices"][0]["message"]["content"] == "the button is cut off"

    (sent,) = provider.sent
    assert sent["messages"] == [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": "what is wrong?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PIXEL}"}},
        ]},
        {"role": "assistant", "content": "the button"},
        {"role": "user", "content": "fix it"},
    ]
    assert [tool["name"] for tool in sent["tools"]] == ["read_file"]
    (row,) = ledger.rows()
    assert row["route"] == "local"


# --- (4) an alias that cannot take both is refused by the predicate the call asks ------


class _Blind(_Provider):
    def supports_vision(self):
        return False


class _SeesButCallsNoTools:
    """A provider with eyes and no `generate_with_tools`."""

    def __init__(self, type_, model):
        self.config = {"type": type_, "model": model}
        self.model = model

    def supports_vision(self):
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("provider, says", [
    (_Blind("ollama", "qwen3:8b"), "cannot see images (supports_vision)"),
    (_SeesButCallsNoTools("ollama", "llava:13b"), "no native tool-calling path (generate_with_tools)"),
], ids=["tools but no eyes", "eyes but no tools"])
async def test_an_alias_that_cannot_take_images_and_tools_together_is_refused_naming_what_it_lacks(
        tmp_path, provider, says):
    providers = dict(_providers(), **{LOCAL: provider})
    service = _service(tmp_path, {"serving_local": [LOCAL]}, providers=providers)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(
            server, _messages_body(LOCAL, HISTORY_WITH_A_SCREENSHOT, tools=[READ_FILE]), key=_key(tmp_path))
        assert status == 400, text
        message = _anthropic_error(text)["message"]
        assert says in message and LOCAL in message

        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path), body=_chat_body(
            LOCAL, [{"role": "user", "content": [_data_url()]}], tools=[OPENAI_READ_FILE]))
        assert status == 400, text
        error = _openai_error(text)
        assert error["code"] == "tools_unsupported" and says in error["message"]

        assert service.calls == [] and list(ledger.rows()) == []


# --- (5) the effort word is checked against the tools path, not the vision one ---------


class _EffortOnToolsOnly(_Provider):
    async def generate_with_tools(self, messages, tools, system="", on_chunk=None, conversation_id=None,
                                  reasoning_effort=None):
        raise AssertionError("the gateway speaks to the manager, never to the provider")

    async def generate_with_vision(self, prompt, images):  # no effort, no **kwargs
        raise AssertionError("the gateway speaks to the manager, never to the provider")


@pytest.mark.asyncio
async def test_an_effort_beside_images_and_tools_is_judged_by_the_tools_path_it_will_take(tmp_path):
    providers = dict(_providers(), **{LOCAL: _EffortOnToolsOnly("ollama", "qwen3:8b")})
    service = _echoing_service(tmp_path, providers=providers)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(
            server, _messages_body(LOCAL, HISTORY_WITH_A_SCREENSHOT, tools=[READ_FILE],
                                   output_config={"effort": "high"}), key=_key(tmp_path))
        assert status == 200, text
        (call,) = service.calls
        assert call["kwargs"]["reasoning_effort"] == "high"
        assert (list(ledger.rows())[0]["served_effort"]) == "high"

        # The same alias without tools takes the vision path, which takes no word.
        service.calls.clear()
        status, text = await _post_messages(
            server, _messages_body(LOCAL, HISTORY_WITH_A_SCREENSHOT, output_config={"effort": "high"}),
            key=_key(tmp_path))
        assert status == 400, text
        assert "generate_with_vision" in _anthropic_error(text)["message"]
        assert service.calls == []


# --- (6) images without tools keep the vision door and its flat list -------------------


@pytest.mark.asyncio
async def test_two_screenshots_without_tools_still_take_query_with_the_flat_list_in_order(tmp_path):
    service = _service(tmp_path, {"serving_local": [LOCAL]})
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "before"}, SHOT]},
        {"role": "assistant", "content": "the old layout"},
        {"role": "user", "content": [OTHER_SHOT, {"type": "text", "text": "after"}]},
    ]
    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_messages(server, _messages_body(LOCAL, messages), key=_key(tmp_path))
        assert status == 200, text
        (call,) = service.calls
        assert "messages" not in call, "images without tools left the vision door"
        assert call["kwargs"]["images"] == [WIRE_SHOT, WIRE_OTHER_SHOT]
        assert call["prompt"] == flatten_messages(
            [{"role": "user", "content": [{"type": "text", "text": "before"}]},
             {"role": "assistant", "content": "the old layout"},
             {"role": "user", "content": [{"type": "text", "text": "after"}]}], SYSTEM)


# --- (7) a stream with images and tools is the tools path's stream --------------------


@pytest.mark.asyncio
async def test_a_messages_stream_with_a_screenshot_and_tools_is_a_whole_answer_then_the_call(tmp_path):
    """`generate_with_tools` streams nothing on any provider: the door hands no
    chunk back and the shape layer writes the text and the call from the answer."""
    service = _tool_service(tmp_path, text="reading", chunks=(), tool_calls=[CALL])
    async with _running(tmp_path, service) as (server, _):
        body = _messages_body(LOCAL, HISTORY_WITH_A_SCREENSHOT, tools=[READ_FILE], stream=True)
        events = _sse_events(await _stream_text(server, "/v1/messages", body, key=_key(tmp_path)))
        assert [name for name, _ in events] == [
            "message_start", "content_block_start", "content_block_delta", "content_block_stop",
            "content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop",
        ]
        assert events[2][1]["delta"] == {"type": "text_delta", "text": "reading"}
        assert events[4][1]["content_block"]["type"] == "tool_use"
        assert events[7][1]["delta"]["stop_reason"] == "tool_use"
        (call,) = service.calls
        assert call["streamed"] and _images_in(call["messages"]) == [(0, SHOT)]


@pytest.mark.asyncio
async def test_an_openai_stream_with_a_screenshot_and_tools_ends_with_the_call_and_done(tmp_path):
    service = _tool_service(tmp_path, text="", chunks=(), tool_calls=[CALL])
    async with _running(tmp_path, service) as (server, _):
        body = _chat_body(LOCAL, [{"role": "user", "content": [{"type": "text", "text": "fix"}, _data_url()]}],
                          tools=[OPENAI_READ_FILE], stream=True)
        lines = _data_lines(await _stream_text(server, "/v1/chat/completions", body, key=_key(tmp_path)))
        assert lines[-1] == "[DONE]"
        chunks = [json.loads(line) for line in lines[:-1]]
        assert chunks[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "read_file"
        assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


# --- (8) the peer route: in the turns to a row that says so, refused to one that does not


def _peer_serving_tools(tmp_path, serves_images_with_tools=None):
    """`serves_images_with_tools` None leaves the field off the row, as an older host sends it."""
    service = _peer_service(tmp_path)
    for row in service.peer_metadata[PEER]["providers"]:
        row["supports_tools"] = True
        if serves_images_with_tools is not None:
            row["serves_images_with_tools"] = serves_images_with_tools
    return service


A_TOOL_RETURNED_A_SCREENSHOT = [
    {"role": "user", "content": [{"type": "text", "text": "open the page"}]},
    {"role": "assistant", "content": [dict(CALL)]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": CALL["id"],
                                  "content": [{"type": "text", "text": "the page"}, SHOT]}]},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("turns", [
    [{"role": "user", "content": [SHOT, {"type": "text", "text": "what is wrong here?"}]}],
    HISTORY_WITH_A_SCREENSHOT,
    A_TOOL_RETURNED_A_SCREENSHOT,
], ids=["screenshot asked about", "screenshot in the history", "screenshot a tool returned"])
async def test_a_peer_whose_row_serves_images_with_tools_is_sent_the_screenshot_in_its_turn_and_no_flat_list(
        tmp_path, turns):
    service = _peer_serving_tools(tmp_path, serves_images_with_tools=True)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(
            server, _messages_body(REMOTE_VISION_MODEL, turns, tools=[READ_FILE]), key=_key(tmp_path))
        assert status == 200, text

    (call,) = service.peer_calls
    assert call["messages"] == turns, "the turns did not reach the wire with their image blocks"
    assert call["images"] is None, "the picture travelled twice, and the flat field beside tools is refused"
    assert call["tools"] == [READ_FILE] and call["system"] == SYSTEM
    assert call["prompt"] == flatten_messages(_turns_without_images(turns), SYSTEM), \
        "an older host reading the prompt must find the same conversation, pictures aside"
    (row,) = ledger.rows()
    assert row["route"] == "peer"


@pytest.mark.asyncio
async def test_the_openai_form_sends_a_peer_the_image_url_as_an_image_block_in_its_turn(tmp_path):
    service = _peer_serving_tools(tmp_path, serves_images_with_tools=True)
    async with _running(tmp_path, service) as (server, _):
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path), body=_chat_body(
            REMOTE_VISION_MODEL, [
                {"role": "user", "content": [{"type": "text", "text": "what is wrong?"}, _data_url()]},
                {"role": "assistant", "content": "the button"},
                {"role": "user", "content": "fix it"},
            ], tools=[OPENAI_READ_FILE]))
        assert status == 200, text

    (call,) = service.peer_calls
    assert _images_in(call["messages"]) == [(0, SHOT)] and call["images"] is None
    assert call["tools"] == [READ_FILE]


@pytest.mark.asyncio
@pytest.mark.parametrize("serves", [None, False], ids=["an older host says nothing", "the row says no"])
@pytest.mark.parametrize("turns", [HISTORY_WITH_A_SCREENSHOT, A_TOOL_RETURNED_A_SCREENSHOT],
                         ids=["screenshot in the history", "screenshot a tool returned"])
async def test_a_peer_row_that_does_not_say_serves_images_with_tools_is_refused_before_the_round_trip(
        tmp_path, serves, turns):
    """Fail-closed, and in words that differ from the refusal every guest sent
    before this step: a live check from a new guest to an old host reads that
    difference to know which guest answered."""
    service = _peer_serving_tools(tmp_path, serves_images_with_tools=serves)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(
            server, _messages_body(REMOTE_VISION_MODEL, turns, tools=[READ_FILE]), key=_key(tmp_path))
        assert status == 400, text
        message = _anthropic_error(text)["message"]
        assert "serves_images_with_tools" in message and "older host" in message
        assert "beside the prompt" not in message
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path), body=_chat_body(
            REMOTE_VISION_MODEL, [{"role": "user", "content": [_data_url()]}], tools=[OPENAI_READ_FILE]))
        assert status == 400, text
        assert _openai_error(text)["code"] == "tools_unsupported"
        assert service.peer_calls == [] and list(ledger.rows()) == []


# --- (9) end to end: the guest's gateway, the wire, a real host door, the provider ------


def _host_behind_the_wire(tmp_path, provider):
    """A host coordinator serving `provider` as REMOTE_VISION_ALIAS through the
    real `LLMManager.query_messages`, reached over the real request builder and
    the real request handler, its answer or refusal carried back as the guest's
    coordinator carries it."""
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.firewall.compute_serving_alias = REMOTE_VISION_ALIAS
    (tmp_path / "host").mkdir()
    svc.llm_manager = _manager(tmp_path / "host", provider, alias=REMOTE_VISION_ALIAS)
    coord._ledger = NodeLedger(tmp_path / "host" / "ledger")
    handler = RemoteInferenceRequestHandler(SimpleNamespace(_handle_inference_request=coord.handle_inference_request))

    async def request_inference_from_peer(peer_id, prompt, model=None, provider=None, images=None,
                                          reasoning_effort=None, timeout=1200.0, messages=None, system=None,
                                          tools=None, on_chunk=None, request_id=None):
        frame = create_remote_inference_request(
            request_id=request_id or "req-e2e", prompt=prompt, model=model, provider=provider, images=images,
            reasoning_effort=reasoning_effort, messages=messages, system=system, tools=tools,
            stream=on_chunk is not None,
        )
        svc.p2p_manager.send_message_to_peer = AsyncMock()
        await handler.handle("peer-1", json.loads(json.dumps(frame))["payload"])
        (answer,) = [c[0][1]["payload"] for c in svc.p2p_manager.send_message_to_peer.call_args_list
                     if c[0][1]["command"] == "REMOTE_INFERENCE_RESPONSE"]
        if answer["status"] != "success":
            raise PeerRefused(answer["error"], answer.get("code") or "")
        return answer

    return coord, request_inference_from_peer


def _guest_of(tmp_path, host_provider, request_inference_from_peer):
    """The guest reads the menu row the host's own builder writes for its alias."""
    service = _peer_service(tmp_path)
    stub = SimpleNamespace(llm_manager=_manager(tmp_path, host_provider, alias=REMOTE_VISION_ALIAS),
                           _provider_supports_voice=lambda provider: False)
    service.peer_metadata[PEER]["providers"] = [
        CoreService.build_p2p_provider_info(stub, REMOTE_VISION_ALIAS, host_provider)
    ]
    service.p2p_coordinator = SimpleNamespace(request_inference_from_peer=request_inference_from_peer)
    return service


@pytest.mark.asyncio
async def test_a_screenshot_in_the_history_crosses_guest_wire_and_host_into_the_providers_turn(tmp_path):
    provider = _SeeingToolProvider()
    coord, bridge = _host_behind_the_wire(tmp_path, provider)
    service = _guest_of(tmp_path, provider, bridge)
    assert service.peer_metadata[PEER]["providers"][0]["serves_images_with_tools"] is True

    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_messages(
            server, _messages_body(REMOTE_VISION_MODEL, HISTORY_WITH_A_SCREENSHOT, tools=[READ_FILE]),
            key=_key(tmp_path))
        assert status == 200, text
        assert json.loads(text)["content"] == [{"type": "text", "text": "the button is cut off"}]

    (sent,) = provider.sent
    assert sent["messages"] == [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": "what is wrong on this screen?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PIXEL}"}},
        ]},
        {"role": "assistant", "content": "the button is cut off"},
        {"role": "user", "content": "fix it"},
    ]
    assert [tool["name"] for tool in sent["tools"]] == ["read_file"]
    (host_row,) = coord._ledger.rows()
    assert host_row["caller"] == "peer-1"


class _CallsToolsBlind(_SeeingToolProvider):
    def supports_vision(self):
        return False


@pytest.mark.asyncio
async def test_a_host_row_built_for_a_blind_alias_stops_the_guest_before_the_wire(tmp_path):
    provider = _CallsToolsBlind()
    _, bridge = _host_behind_the_wire(tmp_path, provider)
    service = _guest_of(tmp_path, provider, bridge)

    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(
            server, _messages_body(REMOTE_VISION_MODEL, HISTORY_WITH_A_SCREENSHOT, tools=[READ_FILE]),
            key=_key(tmp_path))
        assert status == 400, text
        assert "serves_images_with_tools" in _anthropic_error(text)["message"]
    assert provider.sent == [] and list(ledger.rows()) == []


@pytest.mark.asyncio
async def test_a_host_asked_anyway_refuses_a_blind_alias_by_the_same_word_and_runs_nothing(tmp_path):
    """The row is an optimisation over the host's gate, not the gate: a guest
    that sends regardless meets the same refusal on the host, as a 400."""
    provider = _CallsToolsBlind()
    coord, bridge = _host_behind_the_wire(tmp_path, provider)
    service = _guest_of(tmp_path, provider, bridge)
    # A row that lies about both, so that nothing on the guest stands in the way.
    service.peer_metadata[PEER]["providers"][0].update(serves_images_with_tools=True, supports_vision=True)

    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_messages(
            server, _messages_body(REMOTE_VISION_MODEL, HISTORY_WITH_A_SCREENSHOT, tools=[READ_FILE]),
            key=_key(tmp_path))
        assert status == 400, text
        error = _anthropic_error(text)
        assert "cannot take the 1 image(s)" in error["message"]
    assert provider.sent == [] and list(coord._ledger.rows()) == []


@pytest.mark.asyncio
async def test_the_turns_sent_to_a_peer_carry_no_image_block_and_are_the_turns_hosts_were_sent_before(tmp_path):
    """The picture travels once, on `images`; the turns are what the parsers
    built before images stayed in them, so a host reads them as it did."""
    service = _peer_service(tmp_path)
    async with _running(tmp_path, service) as (server, _):
        status, text = await _post_messages(
            server, _messages_body(REMOTE_VISION_MODEL, HISTORY_WITH_A_SCREENSHOT), key=_key(tmp_path))
        assert status == 200, text
        status, text = await _request(server, "POST", "/v1/chat/completions", key=_key(tmp_path), body=_chat_body(
            REMOTE_VISION_MODEL, [
                {"role": "user", "content": [{"type": "text", "text": "compare"}, _data_url(),
                                             {"type": "text", "text": "with the design"}]},
                {"role": "assistant", "content": "they differ"},
                {"role": "user", "content": [_data_url(OTHER_PIXEL, "image/gif")]},
            ]))
        assert status == 200, text

    anthropic_call, openai_call = service.peer_calls
    assert anthropic_call["images"] == [WIRE_SHOT]
    assert anthropic_call["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "what is wrong on this screen?"}]},
        HISTORY_WITH_A_SCREENSHOT[1],
        HISTORY_WITH_A_SCREENSHOT[2],
    ]
    assert openai_call["images"] == [WIRE_SHOT, WIRE_OTHER_SHOT]
    # Exactly the turns `_openai_messages` built before: every part's text in one block.
    assert openai_call["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "compare\n\nwith the design"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "they differ"}]},
        {"role": "user", "content": [{"type": "text", "text": ""}]},
    ]
    for call in (anthropic_call, openai_call):
        assert _images_in(call["messages"]) == []
        assert call["prompt"] == flatten_messages(call["messages"], SYSTEM), \
            "the prompt and the turns beside it disagree"
