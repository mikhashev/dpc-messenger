"""A dead socket must become an error, not a silence.

Mike, 2026-08-31: «если агент отвечает уже в loop и произошло переключение
интернет соединения то он висит». It is not waiting on a peer. It is waiting on
the vendor, and the wait was inherited rather than chosen. Asked of the
installed library rather than recalled:

    openai 1.109.1
    DEFAULT_TIMEOUT     = Timeout(connect=5.0, read=600, write=600, pool=600)
    DEFAULT_MAX_RETRIES = 2

All four of our network providers built their client with no timeout argument
at all, so all four took that. When the network changes underneath an
*established* connection the five-second connect timeout never applies — the
socket is already open, it is simply dead — and what remains is a ten-minute
read timeout the SDK will then retry twice: up to half an hour on one call, with
nothing in the agent loop above it to call that too long.

The same file, `deepseek_provider.py`, passes `httpx.AsyncClient(timeout=20)`
for the balance endpoint. The secondary request was bounded and the inference
was not.

These tests pin the bound and its shape, not a particular number: the number is
config, the bound is not. `NETWORK_READ_TIMEOUT` may be retuned; a provider that
goes back to inheriting the default fails here.
"""

from __future__ import annotations

import pytest

from dpc_client_core.providers.base import (
    NETWORK_CONNECT_TIMEOUT,
    NETWORK_MAX_RETRIES,
    NETWORK_READ_TIMEOUT,
    network_client_bounds,
)


# The vendor defaults we are refusing to inherit. Named here so the test says
# what it is protecting against, not merely that a number is small.
SDK_DEFAULT_READ = 600.0
SDK_DEFAULT_RETRIES = 2


def _providers(monkeypatch):
    """Every provider that talks over the network, built for inspection."""
    monkeypatch.setenv("TEST_PROVIDER_KEY", "sk-not-a-real-key")

    from dpc_client_core.providers.deepseek_provider import DeepSeekProvider
    from dpc_client_core.providers.zai_provider import ZaiProvider
    from dpc_client_core.providers.openai_provider import OpenAICompatibleProvider
    from dpc_client_core.providers.anthropic_provider import AnthropicProvider

    common = {"api_key_env": "TEST_PROVIDER_KEY", "model": "a-model"}
    return {
        "deepseek": DeepSeekProvider("t", dict(common)),
        "zai": ZaiProvider("t", dict(common)),
        "openai": OpenAICompatibleProvider("t", dict(common, base_url="https://example.invalid/v1")),
        "anthropic": AnthropicProvider("t", dict(common)),
    }


def test_every_network_provider_sets_its_own_read_timeout(monkeypatch):
    """The defect: four out of four inherited 600 seconds."""
    for name, provider in _providers(monkeypatch).items():
        read = provider.client.timeout.read
        assert read is not None, f"{name}: no read timeout"
        assert read < SDK_DEFAULT_READ, f"{name}: still on the SDK default ({read}s)"
        assert read == NETWORK_READ_TIMEOUT, f"{name}: {read}s, not the shared bound"


def test_every_network_provider_sets_its_own_connect_timeout(monkeypatch):
    for name, provider in _providers(monkeypatch).items():
        assert provider.client.timeout.connect == NETWORK_CONNECT_TIMEOUT, name


def test_no_provider_keeps_the_two_automatic_retries(monkeypatch):
    """Retries multiply the wait. Two of them over a ten-minute read timeout is
    where the half hour came from."""
    for name, provider in _providers(monkeypatch).items():
        assert provider.client.max_retries < SDK_DEFAULT_RETRIES, name


def test_the_worst_case_wait_is_bounded_in_minutes_not_half_an_hour(monkeypatch):
    """The number that matters to the person watching the agent: how long a
    dead socket can hold a turn."""
    for name, provider in _providers(monkeypatch).items():
        worst = provider.client.timeout.read * (provider.client.max_retries + 1)
        assert worst <= 600, f"{name}: worst case {worst}s"


def test_a_slow_model_can_be_given_more_time_in_its_own_config(monkeypatch):
    """The bound is a default, not a ceiling. A reasoning model answering
    without streaming sends no bytes until it is done, and that is the one case
    where a longer wait is the right answer — so it stays configurable."""
    monkeypatch.setenv("TEST_PROVIDER_KEY", "sk-not-a-real-key")
    from dpc_client_core.providers.deepseek_provider import DeepSeekProvider

    provider = DeepSeekProvider("t", {"api_key_env": "TEST_PROVIDER_KEY",
                                      "model": "a-model",
                                      "timeout_seconds": 900})

    assert provider.client.timeout.read == 900


def test_the_shared_helper_reads_the_config_and_falls_back_to_the_bound():
    assert network_client_bounds({})["timeout"].read == NETWORK_READ_TIMEOUT
    assert network_client_bounds({})["max_retries"] == NETWORK_MAX_RETRIES
    assert network_client_bounds({"timeout_seconds": 42})["timeout"].read == 42
    assert network_client_bounds({"connect_timeout_seconds": 3})["timeout"].connect == 3
    assert network_client_bounds({"max_retries": 0})["max_retries"] == 0


def test_a_nonsense_timeout_in_the_config_does_not_become_no_timeout():
    """`timeout_seconds: 0` reads as «no wait at all» to httpx, and a negative
    number is not a duration. Neither may quietly restore the unbounded case."""
    for bad in (0, -1, "", None):
        bounds = network_client_bounds({"timeout_seconds": bad})
        assert bounds["timeout"].read == NETWORK_READ_TIMEOUT, bad


def test_the_local_providers_are_left_alone(monkeypatch):
    """Ollama and the llama.cpp child are on loopback: their waits are about a
    model loading, not about a network, and they already set their own."""
    import inspect
    from dpc_client_core.providers import ollama_provider, llamacpp_server_provider

    for module in (ollama_provider, llamacpp_server_provider):
        source = inspect.getsource(module)
        assert "timeout" in source, module.__name__
        assert "network_client_bounds" not in source, module.__name__


# --- The second layer, found in review the same day -------------------------
#
# Ark and Johnny both read the first fix and said the falsifier under-counts.
# They are right twice over. `max_retries=1` means two attempts, so the bound
# is 600 s and not the «about five minutes» first written. And DeepSeek — with
# Z.AI and the llama.cpp child, which subclass it — wraps every call in its own
# `_retry_with_backoff`, whose `_is_retryable` names `APITimeoutError`. So the
# SDK's retries and ours stack.
#
# Measured from the loop rather than estimated: its budget `max_retry_seconds`
# counts only what it *sleeps* (`elapsed += delay`), never what the call costs.
# With delays 3, 6, 12 … capped at 192 the sleeps reach 600 on the ninth pass,
# so a call that always times out is attempted **ten** times. At 600 s each
# that is one hundred minutes, and before the timeouts landed it was five hours.

def _timeout_error():
    import openai
    return openai.APITimeoutError(request=None)


class _Clock:
    """A fake monotonic clock; `asyncio.sleep` and the call both advance it."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def deepseek_with_a_fake_clock(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "sk-not-a-real-key")
    from dpc_client_core.providers import deepseek_provider as mod

    clock = _Clock()
    monkeypatch.setattr(mod.time, "monotonic", clock, raising=False)

    async def _sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(mod.asyncio, "sleep", _sleep)

    provider = mod.DeepSeekProvider("t", {"api_key_env": "TEST_PROVIDER_KEY", "model": "a-model"})
    return provider, clock


@pytest.mark.asyncio
async def test_the_retry_budget_counts_the_calls_and_not_only_the_sleeps(deepseek_with_a_fake_clock):
    """`max_retry_seconds` is 600 by default. A call that costs 600 s of that
    budget has to spend it."""
    provider, clock = deepseek_with_a_fake_clock
    attempts = []

    async def _always_times_out():
        attempts.append(clock.now)
        clock.now += 600.0          # what one call costs once the SDK gives up
        raise _timeout_error()

    with pytest.raises(RuntimeError):
        await provider._retry_with_backoff(_always_times_out, _timeout_error())

    assert len(attempts) <= 2, f"{len(attempts)} attempts, {clock.now}s of wall time"
    assert clock.now <= 2 * provider.max_retry_seconds


@pytest.mark.asyncio
async def test_a_retryable_error_is_still_retried(deepseek_with_a_fake_clock):
    """The guard on the fix: the loop must not become a single attempt."""
    provider, clock = deepseek_with_a_fake_clock
    calls = []

    async def _fails_once_then_works():
        calls.append(clock.now)
        if len(calls) == 1:
            raise _timeout_error()
        return "answer"

    assert await provider._retry_with_backoff(_fails_once_then_works, _timeout_error()) == "answer"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_permanent_error_is_not_retried_at_all(deepseek_with_a_fake_clock):
    provider, _clock = deepseek_with_a_fake_clock

    async def _bad_request():
        raise ValueError("model does not exist")

    with pytest.raises(ValueError):
        await provider._retry_with_backoff(_bad_request, _timeout_error())


def test_a_provider_that_retries_itself_does_not_also_let_the_sdk_retry(monkeypatch):
    """Two layers of retry is one too many, and ours is the better one — it
    backs off, the SDK's does not."""
    providers = _providers(monkeypatch)
    for name in ("deepseek", "zai"):
        assert providers[name].client.max_retries == 0, name
    for name in ("openai", "anthropic"):
        assert providers[name].client.max_retries == NETWORK_MAX_RETRIES, name


def test_the_llama_cpp_child_inherits_the_repaired_loop():
    """`LlamaServerProvider` subclasses `DeepSeekProvider`, so the loop is the
    same object — a fix that missed it would be silent."""
    from dpc_client_core.providers.deepseek_provider import DeepSeekProvider
    from dpc_client_core.providers.llamacpp_server_provider import LlamaServerProvider

    assert issubclass(LlamaServerProvider, DeepSeekProvider)
    assert LlamaServerProvider._retry_with_backoff is DeepSeekProvider._retry_with_backoff


def test_one_deepseek_call_is_bounded_by_the_read_timeout_alone(monkeypatch):
    """With the SDK's retries off, one attempt costs one read timeout."""
    provider = _providers(monkeypatch)["deepseek"]
    worst_one_call = provider.client.timeout.read * (provider.client.max_retries + 1)
    assert worst_one_call == NETWORK_READ_TIMEOUT


# The deadline existed and was checked at the top of each pass, and then the
# pass overran it twice over: the sleep ran its full ladder step and the call
# after it was bounded only by the client's own read timeout. Measured on the
# ladder 3, 6, 12 … 192: the loop entered its last pass at 573 s and left at
# 765 s against a budget of 600.


@pytest.mark.asyncio
async def test_the_loop_leaves_at_its_deadline_and_not_a_ladder_step_past_it(
    deepseek_with_a_fake_clock,
):
    """Only the sleeps move the clock here, so what is measured is the ladder."""
    provider, clock = deepseek_with_a_fake_clock

    async def _instant_failure():
        raise _timeout_error()

    with pytest.raises(RuntimeError):
        await provider._retry_with_backoff(_instant_failure, _timeout_error())

    assert clock.now <= provider.max_retry_seconds, (
        f"budget {provider.max_retry_seconds}s, left at {clock.now}s"
    )


@pytest.mark.asyncio
async def test_the_zai_loop_is_bounded_the_same_way(monkeypatch):
    """The two providers carry the same loop, so they need the same proof."""
    monkeypatch.setenv("TEST_PROVIDER_KEY", "sk-not-a-real-key")
    from dpc_client_core.providers import zai_provider as mod

    clock = _Clock()
    monkeypatch.setattr(mod.time, "monotonic", clock, raising=False)

    async def _sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(mod.asyncio, "sleep", _sleep)
    provider = mod.ZaiProvider("t", {"api_key_env": "TEST_PROVIDER_KEY", "model": "glm-4.7"})

    async def _instant_failure():
        raise _timeout_error()

    with pytest.raises(RuntimeError):
        await provider._retry_with_backoff(_instant_failure, _timeout_error())

    assert clock.now <= provider.max_retry_seconds, (
        f"budget {provider.max_retry_seconds}s, left at {clock.now}s"
    )
