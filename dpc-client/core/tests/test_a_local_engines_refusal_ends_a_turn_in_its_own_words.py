"""A local engine's refusal ends an agent turn in its own words (ADR-044, the card queue, a
refusal in an agent turn).

With the swap off an agent's turn read "LLM error: llamacpp_server streaming provider 'X' failed:
CardHeldError: strata[X] cannot load: the card is held by...": no model failed, and the engine was
Strata. The label and the wrap are gone for the `CardHeldError` of the swap off and for
`EngineDrainingError`, as they were for a refusal of the queue; the turn stays `failed`.

A real `StrataProvider` for the wrap's words, a real supervisor, a real adapter and a real loop.
"""

from pathlib import Path

import pytest

from dpc_client_core.dpc_agent.retrieval import factory as retrieval_factory
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import (
    CardHeldError,
    CardQueueRefusal,
    EngineDrainingError,
    LlamaServerError,
    card_queue_refusal_in,
    engine_refusal_in,
)
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from dpc_client_core.settings import Settings

from tests.test_a_request_holds_the_card_from_its_first_round_to_its_last import (
    F,
    _agent,
    _Manager,
    _untouched,
)
from tests.test_one_local_engine_holds_the_card import _strata, card  # noqa: F401
from tests.test_strata_is_offered_to_peers_on_the_owners_terms import _strata as _strata_provider

ALIAS = "flash"  # `_strata_provider`'s
WRAP_WORDS = ("LLM error", "llamacpp_server", "failed:", "CardHeldError", "EngineDrainingError")


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "dpc-home-settings")


@pytest.fixture(autouse=True)
def swap_off(settings, tmp_path, monkeypatch):
    settings.set_local_engine_swap(False, 60, 0)
    base_mod.bind_local_engine_settings(settings)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(retrieval_factory, "_derive_embedding_metadata", lambda cfg: ("BAAI/bge-m3", 1024))
    yield
    base_mod.bind_local_engine_settings(None)
    base_mod.bind_owner_loop(None)


class _StreamingOnly:
    """The real provider with no native tool path: the adapter takes the text path, whose wrap
    is the one the owner read. Nothing else differs."""

    def __init__(self, provider):
        self.provider = provider
        self.model = provider.model
        self.generate_response_stream = provider.generate_response_stream
        self.generate_response = provider.generate_response
        self.supports_vision = provider.supports_vision
        self.supports_thinking = provider.supports_thinking
        self.get_last_usage = provider.get_last_usage


async def _a_card_held_by_another_engine(tmp_path):
    """`F` up on the card, and the real provider of an alias it excludes, on the swap off."""
    holder = _strata(tmp_path / "holder", F)
    _ACTIVE_SUPERVISORS[F] = holder
    await holder.ensure_running()
    return holder, _strata_provider(tmp_path / "provider")


def _ends_in_its_own_words(agent, answer: str, opening: str):
    assert answer.startswith(f"⚠️ {opening}"), answer
    for word in WRAP_WORDS:
        assert word not in answer, answer
    assert len(answer.splitlines()) == 1, "a failed record is replayed by its first line"
    assert agent._last_trace["turn_end"] == "failed"
    assert not agent._last_trace.get("stopped_by_user")


class TestTheSwapOff:
    @pytest.mark.asyncio
    async def test_the_native_path_reads_the_refusal_and_does_not_try_the_text_path(self, tmp_path):
        holder, provider = await _a_card_held_by_another_engine(tmp_path)
        asked = []
        real = provider.generate_response_stream

        async def text_path(*args, **kwargs):
            asked.append("text path")
            return await real(*args, **kwargs)

        provider.generate_response_stream = text_path
        agent = _agent(tmp_path, _Manager({ALIAS: provider}), provider_alias=ALIAS)

        answer = await agent.process("a task", "the-chat", on_stream_chunk=lambda *_: None)

        _ends_in_its_own_words(agent, answer, f"strata[{ALIAS}] cannot load: the card is held by strata alias '{F}'")
        assert "Swap local models" in answer, "the switch that would have swapped stays named"
        assert asked == [], "the text path would only meet the same refusal"
        _untouched(holder)

    @pytest.mark.asyncio
    async def test_the_streaming_wrap_the_owner_read_leaves_only_the_refusal(self, tmp_path):
        holder, provider = await _a_card_held_by_another_engine(tmp_path)
        streaming = _StreamingOnly(provider)
        agent = _agent(tmp_path, _Manager({ALIAS: streaming}), provider_alias=ALIAS)
        seen = []
        real = provider.generate_response_stream

        async def noting(*args, **kwargs):
            try:
                return await real(*args, **kwargs)
            except Exception as e:
                seen.append(str(e))
                raise

        streaming.generate_response_stream = noting

        answer = await agent.process("a task", "the-chat", on_stream_chunk=lambda *_: None)

        [wrapped] = seen
        assert wrapped.startswith(f"llamacpp_server streaming provider '{ALIAS}' failed: CardHeldError: strata[")
        _ends_in_its_own_words(agent, answer, f"strata[{ALIAS}] cannot load: the card is held by strata alias '{F}'")
        _untouched(holder)


class TestADrainingEngine:
    @pytest.mark.asyncio
    async def test_a_draining_alias_ends_the_turn_in_its_own_words(self, tmp_path):
        provider = _strata_provider(tmp_path / "provider")
        provider.supervisor._draining = True
        agent = _agent(tmp_path, _Manager({ALIAS: provider}), provider_alias=ALIAS)

        answer = await agent.process("a task", "the-chat", on_stream_chunk=lambda *_: None)

        _ends_in_its_own_words(agent, answer, f"strata[{ALIAS}] is draining; new calls refused")


class TestAFailureOfAModelStaysAnLlmError:
    @pytest.mark.asyncio
    async def test_a_start_that_failed_is_not_a_refusal(self, tmp_path):
        """A `LlamaServerError` that is neither is the engine's failure, and the label is true of it."""
        provider = _strata_provider(tmp_path / "provider")

        async def fails():
            raise LlamaServerError("strata[flash] did not become healthy within 600s")

        provider.supervisor.ensure_running = fails
        agent = _agent(tmp_path, _Manager({ALIAS: provider}), provider_alias=ALIAS)

        answer = await agent.process("a task", "the-chat", on_stream_chunk=lambda *_: None)

        assert answer.startswith("⚠️ LLM error") and "did not become healthy" in answer
        assert agent._last_trace["turn_end"] == "failed"


class TestTheHelper:
    @pytest.mark.parametrize("refusal", [
        CardHeldError("held", "a", "strata"),
        CardQueueRefusal("line", "a", "strata"),
        EngineDrainingError("draining"),
    ])
    def test_it_finds_each_refusal_through_the_wraps(self, refusal):
        wrapped = RuntimeError("router")
        wrapped.__cause__ = RuntimeError("provider")
        wrapped.__cause__.__cause__ = refusal

        assert engine_refusal_in(wrapped) is refusal

    def test_the_queues_own_helper_still_finds_only_the_queues(self):
        assert card_queue_refusal_in(CardHeldError("held", "a", "strata")) is None
        assert card_queue_refusal_in(EngineDrainingError("draining")) is None

    def test_it_finds_nothing_in_another_failure(self):
        assert engine_refusal_in(RuntimeError("boom")) is None
        assert engine_refusal_in(LlamaServerError("did not become healthy")) is None
        assert engine_refusal_in(None) is None
