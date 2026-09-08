"""Three defects of one shape: the remote path drops what the peer sent.

An agent whose inference runs on somebody else's node lost its reasoning three
times over. The peer computes it and the wire carries it; the caller read only
the answer text, so the round had no thinking; and a round with no thinking
falls back to `content`, which on a tool round is the tool_call block itself —
so the UI labelled a tool invocation as the agent's reasoning. Separately, the
request never carried the effort the caller wanted, so a peer that asked for
`off` was served at the host's default and paid minutes of decode for it.

Mike, 2026-09-05 («почему у Linus в thinking блоках tool calls?») and
2026-08-28 («похоже кстати не работает режим reasoning off»).
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent import loop as agent_loop


# --- the round's reasoning is not the call it made --------------------------


FENCED = (
    "```tool_call\n"
    '{"name": "browse_page", "arguments": {"url": "https://yandex.ru/pogoda/tomsk/"}}\n'
    "```"
)


def test_a_message_that_is_only_a_call_leaves_no_reasoning():
    """Mike's screenshot: THINKING showed the JSON of the call."""
    assert agent_loop._prose_outside_tool_calls(FENCED) == ""


def test_a_bare_json_call_counts_too():
    """Providers that emit no fence produced the same block."""
    bare = '{"name": "read_file", "arguments": {"path": "a.md"}}'

    assert agent_loop._prose_outside_tool_calls(bare) == ""


def test_a_preamble_survives_because_it_is_reasoning():
    text = "Смотрю погоду в Томске.\n\n" + FENCED

    assert agent_loop._prose_outside_tool_calls(text) == "Смотрю погоду в Томске."


def test_two_calls_and_a_note_between_them_keep_the_note():
    text = FENCED + "\n\nтеперь второй\n\n" + FENCED

    assert agent_loop._prose_outside_tool_calls(text) == "теперь второй"


def test_text_with_no_call_is_returned_whole():
    assert agent_loop._prose_outside_tool_calls("just prose") == "just prose"


def test_empty_content_is_not_an_error():
    assert agent_loop._prose_outside_tool_calls("") == ""
    assert agent_loop._prose_outside_tool_calls(None) == ""


# --- the host serves the peer's effort, capped by its own --------------------


def _coordinator(configured_effort=None, alias="qwen"):
    from dpc_client_core.p2p_coordinator import P2PCoordinator

    coord = P2PCoordinator.__new__(P2PCoordinator)
    provider = SimpleNamespace(config={"reasoning_effort": configured_effort} if configured_effort else {})
    coord.service = SimpleNamespace(
        llm_manager=SimpleNamespace(providers={alias: provider})
    )
    return coord


@pytest.mark.parametrize("wanted", ["off", "low", "medium", "high", "max"])
def test_an_uncapped_host_serves_exactly_what_the_peer_asked(wanted):
    coord = _coordinator(configured_effort=None)

    assert coord._effort_for_peer("peer", wanted, "qwen") == wanted


def test_the_host_caps_downwards():
    """A request cannot make this node spend more than it chose to."""
    coord = _coordinator(configured_effort="low")

    assert coord._effort_for_peer("peer", "max", "qwen") == "low"


def test_the_host_does_not_raise_a_smaller_request():
    """The whole point of the entry: `off` must stay `off`."""
    coord = _coordinator(configured_effort="high")

    assert coord._effort_for_peer("peer", "off", "qwen") == "off"


def test_an_unknown_word_is_not_guessed_at():
    coord = _coordinator(configured_effort="high")

    assert coord._effort_for_peer("peer", "enthusiastic", "qwen") is None


def test_no_request_means_the_host_default():
    coord = _coordinator(configured_effort="high")

    assert coord._effort_for_peer("peer", None, "qwen") is None


def test_xhigh_is_folded_the_way_the_scale_folds_it():
    coord = _coordinator(configured_effort=None)

    assert coord._effort_for_peer("peer", "xhigh", "qwen") == "high"


def test_an_alias_with_no_provider_does_not_crash_the_request():
    coord = _coordinator(configured_effort="low", alias="other")

    assert coord._effort_for_peer("peer", "max", "qwen") == "max"


# --- the wire carries the field ---------------------------------------------


def test_the_request_carries_the_effort_only_when_one_was_chosen():
    from dpc_protocol.protocol import create_remote_inference_request

    with_effort = create_remote_inference_request("r1", "hi", reasoning_effort="off")
    without = create_remote_inference_request("r1", "hi")

    assert with_effort["payload"]["reasoning_effort"] == "off"
    assert "reasoning_effort" not in without["payload"]


# --- the caller reads what the wire delivered -------------------------------


def _adapter():
    from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter

    a = DpcLlmAdapter.__new__(DpcLlmAdapter)
    a._llm_manager = SimpleNamespace(providers={})
    a._provider_alias = "peer_alias"
    a._token_counter = None
    a._default_model = "m"
    return a


def _peer_ctx(result, captured):
    async def _request(**kwargs):
        captured.update(kwargs)
        return result

    return SimpleNamespace(
        peer_id="dpc-node-" + "b" * 32,
        remote_model="qwen",
        remote_provider="qwen",
        timeout=30,
        _service=SimpleNamespace(_request_inference_from_peer=_request),
    )


@pytest.mark.asyncio
async def test_the_peers_thinking_becomes_the_rounds_reasoning():
    """It travelled the whole wire and was dropped at the last step."""
    adapter = _adapter()
    captured = {}
    ctx = _peer_ctx(
        {
            "response": "the answer",
            "tokens_used": 30,
            "prompt_tokens": 10,
            "response_tokens": 20,
            "thinking": "я подумал вот так",
            "thinking_tokens": 7,
        },
        captured,
    )

    msg, usage = await adapter._chat_via_remote_peer(
        ctx, [{"role": "user", "content": "hi"}], reasoning_effort="off",
    )

    assert msg["thinking"] == "я подумал вот так"
    assert usage["reasoning_tokens"] == 7
    assert captured["reasoning_effort"] == "off"


@pytest.mark.asyncio
async def test_a_peer_that_sent_no_thinking_adds_no_empty_field():
    """Absent must stay absent: the loop reads a missing field as «no round
    reported one», and an empty string would read as reasoning that happened."""
    adapter = _adapter()
    ctx = _peer_ctx(
        {"response": "answer", "tokens_used": 3, "prompt_tokens": 1, "response_tokens": 2},
        {},
    )

    msg, usage = await adapter._chat_via_remote_peer(ctx, [{"role": "user", "content": "hi"}])

    assert "thinking" not in msg
    assert "reasoning_tokens" not in usage


# --- the assembly, not just the helper --------------------------------------


CALL = [{"function": {"name": "browse_page"}}]


def test_a_remote_tool_round_shows_no_reasoning_at_all():
    """Mike's screenshot end to end: peer text is content, content is the call."""
    assert agent_loop._round_reasoning(None, None, FENCED, CALL) == ""


def test_a_remote_tool_round_keeps_the_peers_thinking_when_it_arrives():
    got = agent_loop._round_reasoning(None, "мысли пира", FENCED, CALL)

    assert got == "мысли пира"


def test_a_round_without_calls_keeps_its_content_whole():
    """A message that merely mentions a tool_call block is not a call."""
    text = "вот пример вызова:\n" + FENCED

    assert agent_loop._round_reasoning(None, None, text, None) == text


def test_thinking_and_preamble_are_joined_and_deduped():
    got = agent_loop._round_reasoning("одно", "одно", "два\n\n" + FENCED, CALL)

    assert got == "одно\n\nдва"


def test_a_cap_this_node_cannot_read_is_not_an_open_door():
    """llamacpp_server's ladder is the model's jinja template, not our scale.

    `xhigh` normalises to `high` and clamps fine; a word that does not, such as
    a template-only rung, used to mean «no cap» and served the peer whatever it
    asked for. A stated ceiling nobody could read must fall back to this node's
    own default, never to the request.
    """
    coord = _coordinator(configured_effort="ultra-deep")

    assert coord._effort_for_peer("peer", "max", "qwen") is None


def test_xhigh_configured_still_caps_because_the_scale_folds_it():
    coord = _coordinator(configured_effort="xhigh")

    assert coord._effort_for_peer("peer", "max", "qwen") == "high"
    assert coord._effort_for_peer("peer", "low", "qwen") == "low"
