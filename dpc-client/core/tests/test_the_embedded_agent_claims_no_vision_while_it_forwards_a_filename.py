"""The embedded agent does not claim a capability its own code substitutes away.

`DpcAgentProvider.supports_vision()` returned an unconditional True on the
strength of the agent's VLM tools. What `generate_with_vision` actually does
with an image is replace it with a line of text — `[Image: <path>]`, or
`[Image: base64 data]` when the pixels are all there is — and hand that to
`_generate_response_impl`. No image reaches a model on this path, and nothing
instructs the agent to go and fetch one.

The claim is not cosmetic. `llm_manager.query` picks the **first** provider that
answers yes when an image query names none (`llm_manager.py:617-621`), so the
agent alias volunteers for image work — a group image description, a
`read_document` page — and the model answers about a sentence naming a file
rather than about the picture. A truthful no makes the same call raise with a
message naming the provider; a yes we cannot honour produces an answer that
reads as though somebody looked.

Same shape, and the same fix, as `RemotePeerProvider` in `eae3fe66`: the claim
comes from what is actually available, and unknown reads as no.
"""

import asyncio

import pytest

from dpc_client_core.providers.dpc_agent_provider import DpcAgentProvider

PIXELS = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
AN_IMAGE = [{"base64": PIXELS, "mime_type": "image/png"}]
AN_IMAGE_ON_DISK = [{"path": "/home/mike/shot.png", "mime_type": "image/png"}]


def _provider(config=None, prompts=None):
    """The provider with its one call out recorded rather than made."""
    p = DpcAgentProvider("dpc_agent", config or {})

    async def _impl(prompt, conversation_id=None, agent_llm_provider=None, **kwargs):
        if prompts is not None:
            prompts.append(prompt)
        return "the agent's answer"

    p._generate_response_impl = _impl
    return p


def test_the_agent_does_not_claim_vision():
    assert _provider().supports_vision() is False


def test_no_configuration_turns_the_claim_back_on():
    """There is no config under which this path forwards pixels, so there is
    none under which the answer may differ."""
    for config in (
        {"tools": ["vlm_query", "analyze_screenshot"]},
        {"peer_id": "dpc-node-" + "b" * 32, "remote_provider": "peer_ollama_vl"},
        {"remote_model": "qwen3-vl:8b"},
    ):
        assert _provider(config).supports_vision() is False


def test_the_pixels_are_replaced_by_a_sentence_about_them():
    """The mechanism behind the no, pinned so the two cannot drift apart."""
    prompts = []
    asyncio.run(_provider(prompts=prompts).generate_with_vision(
        "what is on this screenshot?", AN_IMAGE, conversation_id="agent_001",
    ))

    (prompt,) = prompts
    assert PIXELS not in prompt
    assert "[Image: base64 data]" in prompt


def test_a_path_becomes_a_sentence_too_and_is_not_opened():
    prompts = []
    asyncio.run(_provider(prompts=prompts).generate_with_vision(
        "what is on this screenshot?", AN_IMAGE_ON_DISK, conversation_id="agent_001",
    ))

    (prompt,) = prompts
    assert "[Image: /home/mike/shot.png]" in prompt


def test_a_yes_would_have_to_carry_the_pixels():
    """The regression guard on the pair itself: whoever answers yes here must
    also put the image in front of a model. Flipping the claim back without
    forwarding the image fails this."""
    prompts = []
    provider = _provider(prompts=prompts)

    if not provider.supports_vision():
        pytest.skip("a no needs no pixels behind it")

    asyncio.run(provider.generate_with_vision(
        "what is on this screenshot?", AN_IMAGE, conversation_id="agent_001",
    ))

    assert PIXELS in "".join(prompts), (
        "supports_vision() says yes while the image is dropped before the model"
    )
