"""A remote peer's vision claim must be the peer's own, and must be backed.

`RemotePeerProvider.supports_vision()` returned an unconditional True with the
comment "Assume remote peer can handle vision", while the class defined no
`generate_with_vision` at all — so the inherited one at `providers/base.py`
raised `NotImplementedError`. `llm_manager.query` picks the *first* provider
that answers yes when an image query names no provider, and then calls exactly
the method that raises; the agent's `llm_adapter` swallows that into
"[Image analysis failed: ...]" and hands it to the model under the header
"[The user has shared an image. Here is the visual analysis]" — a declared
capability that never ran, presented as a finished analysis.

Both halves are tested here: the yes has to be the peer's own answer (its
PROVIDERS_RESPONSE row, cached in `CoreService.peer_metadata`), and any yes has
to be backed by a call that actually reaches the peer with the images.
"""

import asyncio

import pytest

from dpc_client_core.providers.remote_peer_provider import RemotePeerProvider

PEER = "dpc-node-" + "b" * 32
OTHER_PEER = "dpc-node-" + "c" * 32


class _StubService:
    """Stands in for CoreService: the peer cache, and the one call out."""

    def __init__(self, peer_metadata=None):
        self.peer_metadata = peer_metadata if peer_metadata is not None else {}
        self.calls = []

    async def _request_inference_from_peer(self, **kwargs):
        self.calls.append(kwargs)
        return "answer from the peer"


def _row(alias, supports_vision):
    return {
        "alias": alias,
        "model": "qwen3-vl:8b",
        "type": "ollama",
        "supports_vision": supports_vision,
        "supports_voice": False,
        "context_window": 131072,
    }


def _provider(service=None, remote_alias="peer_ollama_vl"):
    p = RemotePeerProvider(
        "remote_peer_vl",
        {
            "type": "remote_peer",
            "peer_id": PEER,
            "model": "qwen3-vl:8b",
            "provider": remote_alias,
        },
    )
    if service is not None:
        p.set_service(service)
    return p


def _service_advertising(supports_vision, alias="peer_ollama_vl", peer=PEER):
    return _StubService({peer: {"providers": [_row(alias, supports_vision)]}})


IMAGES = [{"path": "/tmp/screenshot.png", "mime_type": "image/png"}]


# --- the declared capability is backed by an implementation -------------------

def test_a_vision_query_reaches_the_peer_carrying_its_images():
    """The wire already carries images (`create_remote_inference_request` has an
    `images` parameter) and `generate_response` already forwards them. Only the
    vision entry point was missing, so the whole transport was unreachable from
    the one method `llm_manager` calls for an image query."""
    service = _service_advertising(True)
    provider = _provider(service)

    response = asyncio.run(provider.generate_with_vision("what is in this?", IMAGES))

    assert response == "answer from the peer"
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["images"] == IMAGES
    assert call["prompt"] == "what is in this?"
    assert call["peer_id"] == PEER
    assert call["provider"] == "peer_ollama_vl"


def test_the_vision_entry_point_is_this_classs_own_and_not_the_raising_base():
    """The base implementation raises `NotImplementedError`; inheriting it while
    answering yes is the defect itself."""
    assert (
        RemotePeerProvider.generate_with_vision
        is not RemotePeerProvider.__mro__[1].generate_with_vision
    )


# --- the yes belongs to the peer, not to us -----------------------------------

def test_vision_is_yes_when_the_peers_own_row_says_yes():
    assert _provider(_service_advertising(True)).supports_vision() is True


def test_vision_is_no_when_the_peers_own_row_says_no():
    assert _provider(_service_advertising(False)).supports_vision() is False


def test_a_peer_we_have_no_row_for_is_not_credited_with_vision():
    """Fail closed: a truthful no makes `llm_manager` refuse with the message
    naming the provider, instead of routing an image query into a call that
    raises and gets swallowed."""
    assert _provider(_StubService({})).supports_vision() is False
    assert _provider(_StubService({PEER: {}})).supports_vision() is False
    assert _provider(_service_advertising(True, peer=OTHER_PEER)).supports_vision() is False


def test_an_alias_absent_from_the_peers_list_is_not_credited_with_vision():
    assert _provider(_service_advertising(True, alias="peer_ollama_text")).supports_vision() is False


def test_no_service_injected_means_no_vision():
    """Before `set_service`, the provider knows nothing about the peer at all."""
    assert _provider(service=None).supports_vision() is False


def test_a_missing_remote_alias_cannot_be_matched_to_a_row():
    """`provider` is optional in the config; with no alias there is no row to
    read, and a guess is what this fix removes."""
    p = RemotePeerProvider(
        "remote_peer_default",
        {"type": "remote_peer", "peer_id": PEER, "model": "qwen3-vl:8b"},
    )
    p.set_service(_service_advertising(True))
    assert p.supports_vision() is False


# --- the regression guard on the failure mode itself --------------------------

@pytest.mark.parametrize("advertised", [True, False])
def test_a_yes_is_never_answered_by_a_method_that_raises(advertised):
    """The failure mode was exactly this pair: `supports_vision()` True and
    `generate_with_vision` raising `NotImplementedError`. Whatever the peer
    advertises, that combination must not exist."""
    service = _service_advertising(advertised)
    provider = _provider(service)

    if not provider.supports_vision():
        pytest.skip("a no needs no implementation behind it")

    try:
        asyncio.run(provider.generate_with_vision("describe", IMAGES))
    except NotImplementedError as exc:  # pragma: no cover - the defect
        pytest.fail(f"declared vision but raised: {exc}")
