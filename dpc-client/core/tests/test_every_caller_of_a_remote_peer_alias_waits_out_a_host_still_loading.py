"""Every caller of a remote-peer alias waits out a host still loading its model
(ONLY-A-PINNED-AGENT-WAITS-OUT-A-HOST-STILL-LOADING-ITS-MODEL, Mike 2026-11-19).

Live, 2026-11-19 18:49: a Linux node ticked Describe for agents on a group
image; its vision provider was a remote-peer alias for Strata on this Windows
node, which was stopped. The request started the launcher and the host answered
at once with `provider_loading`; Strata was ready 38 s later, and nobody asked
again: the guest's description failed on the first refusal. The wait for
`provider_loading` existed only on the agent's pinned route
(`DpcLlmAdapter._chat_via_remote_peer`); `RemotePeerProvider`, which a group
image's description, a chat and an agent on a remote-peer alias all call, gave
up at once.

The wait now lives in `RemotePeerProvider`, on the same schedule as the
pinned route's (Mike's call 2026-10-20), and nested waits ask once, so no
caller waits twice. The schedule is scaled down here; the stub service stands
in for `CoreService._request_inference_from_peer` and answers in the shape the
guest's handler settles a request with (a dict, the text beside its counts).
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from dpc_protocol.protocol import (
    REFUSAL_IDENTITY_UNPROVED,
    REFUSAL_INSUFFICIENT_QUOTA,
    REFUSAL_NOT_ALLOWED,
    REFUSAL_PROVIDER_LOADING,
    REFUSAL_PROVIDER_UNAVAILABLE,
    REFUSAL_TOOLS_UNSUPPORTED,
    PeerRefused,
)
from dpc_client_core.providers import remote_peer_provider as wait_mod
from dpc_client_core.providers.remote_peer_provider import RemotePeerProvider

PEER = "dpc-node-" + "8" * 32
HOST_ALIAS = "qwen3.8-flash-next-iq3_xxs Strata"
LOADING_TEXT = f"'{HOST_ALIAS}' is loading (the Strata launcher is starting); try again shortly"
STEP_S = 0.01
CAP_S = 0.05


@pytest.fixture
def fast_wait(monkeypatch):
    """The real schedule scaled down. `raising=False`: before the fix the
    provider had no schedule, and these tests must fail on behaviour there,
    not on a missing name."""
    monkeypatch.setattr(wait_mod, "PEER_LOADING_RETRY_EVERY_S", STEP_S, raising=False)
    monkeypatch.setattr(wait_mod, "PEER_LOADING_WAIT_CAP_S", CAP_S, raising=False)
    monkeypatch.setattr(wait_mod, "PEER_LOADING_SHORTEST_STEP_S", 0.001, raising=False)


def _loading(retry_after_sec=None) -> PeerRefused:
    return PeerRefused(LOADING_TEXT, code=REFUSAL_PROVIDER_LOADING, retry_after_sec=retry_after_sec)


def _served(text="the answer") -> dict:
    return {"request_id": "req-1", "response": text, "tokens_used": 3,
            "prompt_tokens": 2, "response_tokens": 1}


class _Host:
    """`_request_inference_from_peer` answering each ask with the next item;
    the last one repeats. An exception is raised."""

    def __init__(self, *answers, vision=True):
        self.answers = list(answers)
        self.asks = []
        self.peer_metadata = {PEER: {"providers": [
            {"alias": HOST_ALIAS, "model": "qwen3.8", "type": "strata", "supports_vision": vision},
        ]}}
        self.p2p_manager = SimpleNamespace(
            peers={},
            peer_cache=SimpleNamespace(get_peer=lambda nid: SimpleNamespace(display_name="Mike Windows 11")),
        )

    async def _request_inference_from_peer(self, **kwargs):
        self.asks.append(kwargs)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _alias(host) -> RemotePeerProvider:
    p = RemotePeerProvider(f"{HOST_ALIAS} Remote Peer",
                           {"type": "remote_peer", "peer_id": PEER, "model": "qwen3.8",
                            "provider": HOST_ALIAS})
    p.set_service(host)
    return p


# --- the provider waits out a load, and nothing else --------------------------------


@pytest.mark.asyncio
async def test_two_loading_refusals_then_an_answer_is_one_answer(fast_wait):
    host = _Host(_loading(), _loading(), _served())

    text = await _alias(host).generate_response("hello")

    assert text == "the answer", "the text, as every provider answers - not the handler's dict"
    assert len(host.asks) == 3


@pytest.mark.asyncio
async def test_a_load_longer_than_the_ceiling_raises_the_refusal_after_about_the_ceiling(fast_wait):
    host = _Host(_loading())

    started = time.monotonic()
    with pytest.raises(PeerRefused) as raised:
        await _alias(host).generate_response("hello")
    elapsed = time.monotonic() - started

    assert raised.value.code == REFUSAL_PROVIDER_LOADING
    assert isinstance(raised.value, wait_mod.PeerStillLoading)
    assert raised.value.host_name == "Mike Windows 11"
    assert CAP_S - 2 * STEP_S <= elapsed < 1.0, elapsed
    assert 3 <= len(host.asks) <= 7, "asked every step until the ceiling, then stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [
    REFUSAL_NOT_ALLOWED, REFUSAL_IDENTITY_UNPROVED, REFUSAL_INSUFFICIENT_QUOTA,
    REFUSAL_TOOLS_UNSUPPORTED, REFUSAL_PROVIDER_UNAVAILABLE, "", "a_word_from_a_newer_host",
])
async def test_any_other_refusal_is_raised_at_once_with_its_code(fast_wait, code):
    host = _Host(PeerRefused(LOADING_TEXT, code=code), _served())

    with pytest.raises(PeerRefused) as raised:
        await _alias(host).generate_response("hello")

    assert len(host.asks) == 1
    assert raised.value.code == code and not isinstance(raised.value, wait_mod.PeerStillLoading)


@pytest.mark.asyncio
async def test_the_hosts_retry_after_sec_sets_the_step(fast_wait, monkeypatch):
    """Our own step alone would overrun the ceiling at once; the host's shorter
    word is what makes the second ask."""
    monkeypatch.setattr(wait_mod, "PEER_LOADING_RETRY_EVERY_S", 10.0, raising=False)
    monkeypatch.setattr(wait_mod, "PEER_LOADING_WAIT_CAP_S", 1.0, raising=False)
    host = _Host(_loading(retry_after_sec=0.02), _served())

    assert await _alias(host).generate_response("hello") == "the answer"
    assert len(host.asks) == 2


@pytest.mark.asyncio
async def test_a_cancelled_call_stops_waiting_at_once(monkeypatch):
    monkeypatch.setattr(wait_mod, "PEER_LOADING_RETRY_EVERY_S", 5.0, raising=False)
    monkeypatch.setattr(wait_mod, "PEER_LOADING_WAIT_CAP_S", 60.0, raising=False)
    host = _Host(_loading())

    task = asyncio.ensure_future(_alias(host).generate_response("hello"))
    await asyncio.sleep(0.02)
    assert not task.done(), "the provider is waiting for the load"
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert time.monotonic() - started < 0.5
    await asyncio.sleep(0.05)
    assert len(host.asks) == 1


# --- a group image's description, the live case -------------------------------------


def _png_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _llm_with_vision_alias(tmp_path, alias):
    import json

    from dpc_client_core.llm_manager import LLMManager

    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"providers": []}), encoding="utf-8")
    manager = LLMManager(config_path=path)
    manager.providers = {alias.alias: alias}
    manager.vision_provider = alias.alias
    manager.default_provider = alias.alias
    return manager


@pytest.mark.asyncio
async def test_a_group_images_description_on_a_loading_remote_vision_alias_arrives(fast_wait, tmp_path):
    from dpc_client_core.service import CoreService

    host = _Host(_loading(), _served("a red square"))
    service = SimpleNamespace(llm_manager=_llm_with_vision_alias(tmp_path, _alias(host)))

    description = await CoreService._describe_image_for_agents(service, _png_b64(), "image/png", "look")

    assert description == "a red square"
    assert len(host.asks) == 2 and host.asks[-1]["images"], "the picture went with every ask"


# --- the agent waits once on either route ------------------------------------------


class _AgentHost(_Host):
    """What the agent's adapter reads off CoreService besides the call itself."""

    firewall = None
    device_context = {}


def _adapter(route, host):
    from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter

    if route == "pinned":
        dpc_agent = SimpleNamespace(peer_id=None, remote_model="qwen3.8", remote_provider=HOST_ALIAS,
                                    timeout=5, _service=host)
        manager = SimpleNamespace(providers={"dpc_agent": dpc_agent}, token_count_manager=None,
                                  default_provider=None)
        return DpcLlmAdapter(manager, provider_alias=HOST_ALIAS, compute_host=PEER)
    alias = _alias(host)
    manager = SimpleNamespace(providers={alias.alias: alias}, token_count_manager=None,
                              default_provider=None)
    return DpcLlmAdapter(manager, provider_alias=alias.alias)


ROUTES = ["pinned", "remote_peer_alias"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_the_agent_asks_three_times_for_two_loads_and_shows_each_wait(fast_wait, route):
    host = _AgentHost(_loading(), _loading(), _served("ok"))
    lines = []
    token = wait_mod.CALL_PROGRESS.set(lines.append)
    try:
        msg, _usage = await _adapter(route, host)._chat([{"role": "user", "content": "hi"}])
    finally:
        wait_mod.CALL_PROGRESS.reset(token)

    assert msg["content"] == "ok"
    assert len(host.asks) == 3, "one wait, not one inside another"
    assert len(lines) == 2 and all("Mike Windows 11 is loading, waiting" in l for l in lines)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_a_load_that_never_ends_ends_the_agents_call_in_still_loading(fast_wait, route):
    """The loop's notice (`peer_refusal_notice`) reads `PeerStillLoading`; on
    the alias route the provider used to wrap every refusal in a bare
    RuntimeError, so the agent read an LLM error instead."""
    host = _AgentHost(_loading())

    with pytest.raises(wait_mod.PeerStillLoading):
        await _adapter(route, host)._chat([{"role": "user", "content": "hi"}])

    assert len(host.asks) <= 7, "one schedule's asks, not a schedule's asks squared"


@pytest.mark.asyncio
async def test_a_wait_inside_another_waits_ask_asks_once(fast_wait):
    """A caller that wraps the provider in the same wait must not multiply it:
    the outer wait owns the schedule."""
    host = _Host(_loading())
    alias = _alias(host)

    with pytest.raises(wait_mod.PeerStillLoading):
        await wait_mod.ask_peer_waiting_out_a_load(lambda: alias.generate_response("hi"), "outer")

    assert len(host.asks) <= 7
