"""DeepSeek Flash reads images; Pro does not, until it has been tested.

The vendor's vision guide (api-docs.deepseek.com/guides/vision, read
2026-10-26) names `deepseek-flash` as the model that accepts images, and its
quick start serves the legacy `deepseek-v4-flash` and
`deepseek-v4-flash-vision-exp` from that same model; Pro is not named for
images. Mike's call, 2026-10-26: vision on for Flash, off for Pro.

`generate_with_vision` was written while no model of this vendor could see, and
it showed: no thinking toggle, no effort, no usage. Once Flash answers yes it
is a real path, reached by `describe_image`, the AI chat and an agent's image
description, so it has to say what it cost and run at the rung it was asked
for, as the text paths do. The vendor takes images in user messages only — one
in a system or assistant message is a 400 — so the tools path is held to that
too, including the image a tool returned. No network.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.providers.deepseek_provider import DeepSeekProvider

SHOT_B64 = "aGVsbG8="
IMAGE = {"base64": SHOT_B64, "mime_type": "image/png"}
SHOT = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": SHOT_B64}}
TOOL = {"name": "read_file", "description": "", "input_schema": {"type": "object", "properties": {}}}


def _make(model="deepseek-v4-flash", **config):
    cfg = {"api_key": "test-key", "model": model}
    cfg.update(config)
    return DeepSeekProvider("deepseek_test", cfg)


def _resp(content="DPC 47", tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=content, reasoning_content="cot", tool_calls=tool_calls or []))],
        usage=SimpleNamespace(
            prompt_tokens=237, completion_tokens=95, total_tokens=332,
            prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=237,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=90),
        ),
    )


# --- which names can see ---


@pytest.mark.parametrize("model", [
    "deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "DeepSeek-V4-Flash",
])
def test_every_name_the_vendor_gives_flash_can_see(model):
    assert _make(model).supports_vision() is True


@pytest.mark.parametrize("model", ["deepseek-v4-pro", "deepseek-pro", "deepseek-chat", ""])
def test_pro_and_any_other_name_cannot(model):
    assert _make(model).supports_vision() is False


# --- the vision request ---


@pytest.mark.asyncio
async def test_the_vision_request_is_one_user_message_with_the_text_then_the_image():
    p = _make()
    p.client.chat.completions.create = AsyncMock(return_value=_resp())

    assert await p.generate_with_vision("What text is in this image?", [IMAGE]) == "DPC 47"

    _, sent = p.client.chat.completions.create.call_args
    assert sent["model"] == "deepseek-v4-flash"
    assert sent["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "What text is in this image?"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{SHOT_B64}"}},
    ]}]


@pytest.mark.asyncio
async def test_the_vision_request_carries_the_alias_thinking_and_effort():
    p = _make(thinking={"enabled": True}, reasoning_effort="high", temperature=0.6)
    p.client.chat.completions.create = AsyncMock(return_value=_resp())

    await p.generate_with_vision("read it", [IMAGE])

    _, sent = p.client.chat.completions.create.call_args
    assert sent["extra_body"] == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    # Sampling is inert while thinking is on, so it is withheld as on the text paths.
    assert "temperature" not in sent and "top_p" not in sent


@pytest.mark.asyncio
async def test_a_per_call_off_turns_thinking_off_on_the_vision_path():
    p = _make(thinking={"enabled": True}, reasoning_effort="high", temperature=0.6)
    p.client.chat.completions.create = AsyncMock(return_value=_resp())

    await p.generate_with_vision("read it", [IMAGE], reasoning_effort="off")

    _, sent = p.client.chat.completions.create.call_args
    assert sent["extra_body"] == {"thinking": {"type": "disabled"}}
    assert sent["temperature"] == 0.6
    assert p.get_last_usage()["served_effort"] == "off"


@pytest.mark.asyncio
async def test_the_vision_call_leaves_its_usage_and_the_rung_it_ran_on():
    p = _make(reasoning_effort="high")
    p.client.chat.completions.create = AsyncMock(return_value=_resp())

    await p.generate_with_vision("read it", [IMAGE])

    u = p.get_last_usage()
    assert u is not None, "the vision path recorded nothing"
    assert (u["prompt_tokens"], u["completion_tokens"], u["reasoning_tokens"]) == (237, 95, 90)
    assert u["prompt_cache_miss_tokens"] == 237
    assert u["served_effort"] == "high"
    assert p.get_last_thinking() == "cot"


@pytest.mark.asyncio
async def test_the_vision_line_names_its_own_path(caplog):
    p = _make()
    p.client.chat.completions.create = AsyncMock(return_value=_resp())

    with caplog.at_level("INFO"):
        await p.generate_with_vision("read it", [IMAGE])

    assert any("DeepSeek usage:" in r.getMessage() and "path=vision" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_pro_is_refused_before_the_call_rather_than_sent_the_picture():
    p = _make("deepseek-v4-pro")
    p.client.chat.completions.create = AsyncMock()

    with pytest.raises(ValueError, match="no vision path"):
        await p.generate_with_vision("read it", [IMAGE])
    p.client.chat.completions.create.assert_not_called()


# --- the tools path: images in user messages only ---


def _roles_holding_images(messages):
    return [m["role"] for m in messages
            if isinstance(m.get("content"), list)
            and any(part.get("type") == "image_url" for part in m["content"])]


@pytest.mark.asyncio
async def test_flash_takes_an_image_in_the_turn_on_the_tools_path():
    p = _make()
    p.client.chat.completions.create = AsyncMock(return_value=_resp())

    await p.generate_with_tools(
        messages=[{"role": "user", "content": [{"type": "text", "text": "what is this"}, SHOT]}],
        tools=[TOOL], system="you are an agent",
    )

    _, sent = p.client.chat.completions.create.call_args
    assert _roles_holding_images(sent["messages"]) == ["user"]
    assert sent["messages"][0] == {"role": "system", "content": "you are an agent"}
    assert p.get_last_usage()["prompt_tokens"] == 237


@pytest.mark.asyncio
async def test_an_image_a_tool_returned_rides_in_a_user_message_after_the_tool_message():
    p = _make()
    p.client.chat.completions.create = AsyncMock(return_value=_resp())
    call = {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {}}

    await p.generate_with_tools(
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "open the page"}]},
            {"role": "assistant", "content": [call]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1",
                                          "content": [{"type": "text", "text": "the page"}, SHOT]}]},
        ],
        tools=[TOOL], system="sys",
    )

    _, sent = p.client.chat.completions.create.call_args
    wire = sent["messages"]
    assert _roles_holding_images(wire) == ["user"]
    tool_at = next(i for i, m in enumerate(wire) if m["role"] == "tool")
    assert wire[tool_at] == {"role": "tool", "tool_call_id": "tu_1", "content": "the page"}
    assert wire[tool_at + 1]["role"] == "user"
    assert wire[tool_at + 1]["content"] == [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{SHOT_B64}"}},
    ]
