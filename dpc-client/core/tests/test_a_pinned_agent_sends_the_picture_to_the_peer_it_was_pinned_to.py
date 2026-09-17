"""A picture must go where the agent was pinned, and must arrive as bytes.

The wire already says both. `specs/dptp_v1.md` §3.14 makes `supports_vision:
true` in PROVIDERS_RESPONSE the condition for sending a vision query; §3.4 makes
`base64` required in an `images` entry, `path` being only the original filename
and nothing the receiver is promised it can open.

`llm_adapter.chat()` did neither: the image branch ran before the routing
branch, so a pinned agent's picture was handled locally, where the alias
resolves in the local registry and falls through to `default_provider`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter


PEER = "dpc-node-" + "c" * 32
PIXEL = "iVBORw0KGgoAAAANSUhEUg=="


def _picture_turn(text: str = "what is in this picture?"):
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{PIXEL}"}},
            ],
        }
    ]


class _LocalVisionProvider:
    """A local provider that can see — the one a bypassed pin falls through to."""

    alias = "cloud_default"
    model = "some-cloud-vlm"

    def __init__(self):
        self.vision_calls = []
        self.text_calls = []

    def supports_vision(self):
        return True

    async def generate_with_vision(self, prompt, images, **kwargs):
        self.vision_calls.append({"prompt": prompt, "images": images})
        return "a local look at the picture"

    async def generate_response(self, prompt, **kwargs):
        self.text_calls.append(prompt)
        return "a local answer"

    def get_last_usage(self):
        return None


class _LocalBlindProvider(_LocalVisionProvider):
    alias = "local_text"
    model = "some-text-model"

    def supports_vision(self):
        return False


def _build(
    *,
    compute_host: str = "",
    peer_rows=None,
    local_provider=None,
    analysis=None,
):
    """An adapter, plus the recorders the assertions read.

    `analysis` is what LLMManager.query does for the local pre-analysis path:
    a dict to return, or an exception to raise.
    """
    sent = {}
    queries = []

    async def _request_inference_from_peer(**kwargs):
        sent.update(kwargs)
        return {"response": "the peer looked", "tokens_used": 3,
                "prompt_tokens": 1, "response_tokens": 2}

    async def _query(**kwargs):
        queries.append(kwargs)
        if isinstance(analysis, Exception):
            raise analysis
        return analysis or {"response": "a local description"}

    service = SimpleNamespace(
        peer_metadata={PEER: {"providers": list(peer_rows or [])}},
        _request_inference_from_peer=_request_inference_from_peer,
    )
    dpc_agent = SimpleNamespace(
        peer_id=None,
        remote_model="qwen-vl",
        remote_provider="peer_vlm",
        timeout=30,
        _service=service,
    )
    provider = local_provider if local_provider is not None else _LocalVisionProvider()

    adapter = DpcLlmAdapter.__new__(DpcLlmAdapter)
    adapter._llm_manager = SimpleNamespace(
        providers={"dpc_agent": dpc_agent, provider.alias: provider},
        default_provider=provider.alias,
        agent_provider=None,
        query=_query,
    )
    adapter._provider_alias = "peer_vlm"
    adapter._compute_host = compute_host
    adapter._token_counter = None
    adapter._default_model = None
    return adapter, sent, queries, provider


# --- the pin decides where the picture goes ---------------------------------


@pytest.mark.asyncio
async def test_a_pinned_agent_whose_peer_sees_sends_the_picture_to_that_peer():
    adapter, sent, queries, local = _build(
        compute_host=PEER,
        peer_rows=[{"alias": "peer_vlm", "model": "qwen-vl", "supports_vision": True}],
    )

    msg, _ = await adapter.chat(_picture_turn())

    assert sent["peer_id"] == PEER
    assert [img["base64"] for img in sent["images"]] == [PIXEL]
    assert msg["content"] == "the peer looked"


@pytest.mark.asyncio
async def test_the_pinned_peer_sees_it_instead_of_any_local_model():
    """The bypass did not merely pick another model: it picked default_provider."""
    adapter, sent, queries, local = _build(
        compute_host=PEER,
        peer_rows=[{"alias": "peer_vlm", "supports_vision": True}],
    )

    await adapter.chat(_picture_turn())

    assert local.vision_calls == []
    assert queries == []


@pytest.mark.asyncio
async def test_a_pinned_peer_that_cannot_see_keeps_the_local_pre_analysis():
    """Requirement 2: the fix must not make the blind-peer case worse."""
    adapter, sent, queries, local = _build(
        compute_host=PEER,
        peer_rows=[{"alias": "peer_vlm", "supports_vision": False}],
        local_provider=_LocalBlindProvider(),
    )

    await adapter.chat(_picture_turn())

    assert len(queries) == 1, "the local vision model was not asked to describe it"
    assert queries[0]["images"][0]["base64"] == PIXEL
    assert sent["images"] == []
    assert "a local description" in sent["prompt"]


@pytest.mark.asyncio
async def test_a_peer_that_advertised_nothing_is_not_sent_the_picture():
    """§3.14: the peer must advertise supports_vision before a vision query."""
    adapter, sent, queries, local = _build(
        compute_host=PEER,
        peer_rows=[],
        local_provider=_LocalBlindProvider(),
    )

    await adapter.chat(_picture_turn())

    assert sent["images"] == []
    assert len(queries) == 1


@pytest.mark.asyncio
async def test_an_unpinned_agent_still_looks_with_its_own_provider():
    adapter, sent, queries, local = _build(compute_host="")

    msg, _ = await adapter.chat(_picture_turn())

    assert sent == {}
    assert [img["base64"] for img in local.vision_calls[0]["images"]] == [PIXEL]
    assert msg["content"] == "a local look at the picture"


# --- what crosses the wire is bytes, never a path of this machine -----------


def test_an_image_prepared_for_a_peer_carries_base64_and_no_path():
    prepared = DpcLlmAdapter._images_for_peer(
        [{"path": "C:\\Users\\mikha\\shot.png", "mime_type": "image/png", "base64": PIXEL}]
    )

    assert prepared == [{"base64": PIXEL, "mime_type": "image/png"}]


def test_a_path_only_image_is_not_prepared_at_all():
    """§3.4 makes base64 required, so such an entry is malformed and is not sent."""
    assert DpcLlmAdapter._images_for_peer([{"path": "/home/mike/shot.png"}]) == []


def test_an_image_that_does_not_say_what_it_is_is_not_prepared_either():
    """§3.4 makes mime_type required, and a receiver defaults an absent one to
    PNG — so a JPEG would arrive announced as something it is not."""
    assert DpcLlmAdapter._images_for_peer([{"base64": PIXEL}]) == []


def test_one_unsendable_image_stops_the_whole_set():
    """A partial set would reach the peer's model as the whole set.

    The first entry is complete on purpose: the drop has to be caused by the
    second one, not by the entry the assertion is not about.
    """
    prepared = DpcLlmAdapter._images_for_peer(
        [
            {"base64": PIXEL, "mime_type": "image/png"},
            {"path": "/home/mike/shot.png"},
        ]
    )

    assert prepared == []


@pytest.mark.asyncio
async def test_the_wire_call_drops_a_path_even_when_a_caller_hands_one_over():
    adapter, sent, _, _ = _build(compute_host=PEER)
    ctx = SimpleNamespace(
        peer_id=PEER,
        remote_model="qwen-vl",
        remote_provider="peer_vlm",
        timeout=30,
        _service=adapter._llm_manager.providers["dpc_agent"]._service,
    )

    await adapter._chat_via_remote_peer(
        ctx,
        [{"role": "user", "content": "look"}],
        images=[{"path": "C:\\Users\\mikha\\shot.png", "mime_type": "image/png"}],
    )

    assert sent["images"] == []


@pytest.mark.asyncio
async def test_the_pinned_peer_is_never_sent_a_path():
    adapter, sent, queries, local = _build(
        compute_host=PEER,
        peer_rows=[{"alias": "peer_vlm", "supports_vision": True}],
    )

    await adapter.chat(_picture_turn())

    assert sent["images"]
    for img in sent["images"]:
        assert img["base64"]
        assert "path" not in img
