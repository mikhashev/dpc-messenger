"""Extraction is routed by a chain, and silence walks it.

The prompt carries the conversation itself, so «no field set» must not mean
`default_provider` — that is a paid API, and it is how whole transcripts left
the machine. Order: the agent's own choice, then the conversation's provenance,
then the cold fallback; and no step may be reached by guessing from the
conversation's name.

Step 1 is a global role on the providers screen, not a field on an agent:
extraction is a human's button producing a user-level artefact, and in an
agent's chat the agent's own model is what step 2 resolves to anyway.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dpc_client_core import knowledge_routing as KR
from dpc_client_core.conversation_monitor import ConversationMonitor


def _provider(kind):
    return SimpleNamespace(config={"type": kind}, model="m")


def _llm(default="deepseek_flash", knowledge=None, **providers):
    return SimpleNamespace(providers=dict(providers), default_provider=default,
                           knowledge_provider=knowledge)


def _settings(cold=""):
    return SimpleNamespace(get_knowledge_cold_fallback_provider=lambda: cold)


def _monitor(conversation_id, llm=None, settings=None):
    return ConversationMonitor(
        conversation_id=conversation_id,
        participants=[{"node_id": "n", "name": "n", "context": ""}],
        llm_manager=llm if llm is not None else _llm(local=_provider("ollama")),
        settings=settings if settings is not None else _settings(),
    )


# --- step 1: the role chosen on the providers screen ------------------------


def test_the_chosen_extraction_provider_wins_over_provenance():
    llm = _llm(knowledge="llama.cpp", **{"llama.cpp": _provider("llamacpp_server")})
    m = _monitor("agent_forge_7244b181", llm=llm)
    m.set_inference_settings(compute_host=None, model="x", provider="deepseek_flash")

    assert m._infer_inference_settings() == (None, None, "llama.cpp")


def test_the_same_role_applies_to_a_group_because_it_is_not_an_agents_field():
    llm = _llm(knowledge="llama.cpp", **{"llama.cpp": _provider("llamacpp_server")})

    assert _monitor("group-b88b", llm=llm)._infer_inference_settings() == (
        None, None, "llama.cpp")


def test_a_role_pointing_at_a_deleted_alias_walks_on_instead_of_failing():
    providers = {"local_qwen": _provider("ollama")}
    assert KR.chosen_provider("gone", providers) is None
    assert KR.chosen_provider("", providers) is None
    assert KR.chosen_provider("   ", providers) is None
    assert KR.chosen_provider(None, providers) is None
    assert KR.chosen_provider("local_qwen", providers) == "local_qwen"


def test_a_role_pointing_at_a_deleted_alias_falls_to_provenance_not_the_default():
    llm = _llm(knowledge="gone", local_qwen=_provider("ollama"))
    m = _monitor("group-b88b", llm=llm)
    m.set_inference_settings(compute_host=None, model="qwen3.8", provider="llama.cpp")

    assert m._infer_inference_settings() == (None, "qwen3.8", "llama.cpp")


# --- step 2: the conversation's provenance ----------------------------------


def test_a_locally_answered_conversation_is_extracted_by_the_model_that_answered():
    """The case the old PRIORITY 2b could not see: no host, but a provider."""
    m = _monitor("group-b88b")
    m.set_inference_settings(compute_host=None, model="qwen3.8", provider="llama.cpp")

    assert m._infer_inference_settings() == (None, "qwen3.8", "llama.cpp")


def test_a_remotely_answered_conversation_keeps_its_host():
    m = _monitor("group-b88b")
    m.set_inference_settings(compute_host="dpc-node-abc", model="q", provider="p")

    assert m._infer_inference_settings() == ("dpc-node-abc", "q", "p")


def test_a_configured_text_default_no_longer_short_circuits_the_chain():
    """PRIORITY 1 returned (None, None, None) here, i.e. default_provider."""
    m = _monitor("group-b88b", llm=_llm(default="deepseek_flash", local=_provider("ollama")))
    m.set_inference_settings(compute_host=None, model="qwen3.8", provider="llama.cpp")

    host, model, provider = m._infer_inference_settings()
    assert provider == "llama.cpp"
    assert provider != m.llm_manager.default_provider


# --- step 3: the cold fallback ----------------------------------------------


def test_a_conversation_nobody_answered_in_goes_to_the_first_local_provider():
    llm = _llm(default="deepseek_flash",
               deepseek_flash=_provider("deepseek"),
               anthropic=_provider("anthropic"),
               local_qwen=_provider("llamacpp_server"))
    host, model, provider = _monitor("group-b88b", llm=llm)._infer_inference_settings()

    assert (host, model, provider) == (None, None, "local_qwen")


def test_a_peer_chat_is_no_longer_routed_to_the_peer_by_its_name():
    """PRIORITY 2 turned a `dpc-node-…` id into a compute host with no evidence."""
    llm = _llm(local=_provider("ollama"))
    host, _, provider = _monitor("dpc-node-abc123", llm=llm)._infer_inference_settings()

    assert host is None, "the peer is a rescue in `except`, not the primary route"
    assert provider == "local"


def test_a_named_cold_fallback_is_taken_as_written():
    llm = _llm(local=_provider("ollama"), deepseek_flash=_provider("deepseek"))
    m = _monitor("group-b88b", llm=llm, settings=_settings(cold="deepseek_flash"))

    assert m._infer_inference_settings() == (None, None, "deepseek_flash")


def test_a_cold_fallback_naming_nothing_is_an_error_not_a_silent_default():
    llm = _llm(local=_provider("ollama"))
    m = _monitor("group-b88b", llm=llm, settings=_settings(cold="typo_alias"))

    with pytest.raises(KR.NoKnowledgeProvider, match="typo_alias"):
        m._infer_inference_settings()


def test_with_no_local_provider_extraction_refuses_rather_than_using_the_default():
    llm = _llm(default="deepseek_flash",
               deepseek_flash=_provider("deepseek"),
               claude=_provider("anthropic"))
    m = _monitor("group-b88b", llm=llm)

    with pytest.raises(KR.NoKnowledgeProvider):
        m._infer_inference_settings()


# --- what counts as local ---------------------------------------------------


def test_only_providers_that_answer_from_this_machine_count_as_local():
    assert KR.first_local_provider({"a": _provider("ollama")}) == "a"
    assert KR.first_local_provider({"a": _provider("llamacpp_server")}) == "a"
    for paid in ("deepseek", "anthropic", "zai", "gemini", "github_models", "remote_peer"):
        assert KR.first_local_provider({"a": _provider(paid)}) is None, paid


def test_an_openai_compatible_alias_is_not_assumed_local():
    """Its base_url may name LM Studio next door or a service anywhere."""
    assert KR.first_local_provider({"lmstudio": _provider("openai_compatible")}) is None


# --- the rescue path carries the same transcript ----------------------------


class _Calls:
    def __init__(self, fail_first=True):
        self.seen = []
        self.fail_first = fail_first

    async def __call__(self, prompt, compute_host=None, model=None, provider=None):
        self.seen.append({"compute_host": compute_host, "provider": provider})
        if self.fail_first and len(self.seen) == 1:
            raise RuntimeError("peer refused")
        return {"response": '{"score": 0.5, "reasoning": "x"}'}


@pytest.mark.asyncio
async def test_a_retry_after_a_remote_failure_lands_on_the_cold_fallback():
    """It used to retry with provider=None — the global text default, and the
    retry carries the same conversation the failed call did."""
    llm = _llm(default="deepseek_flash",
               deepseek_flash=_provider("deepseek"),
               local_qwen=_provider("llamacpp_server"))
    m = _monitor("group-b88b", llm=llm)
    calls = _Calls()
    m.ai_query_func = calls
    m.set_inference_settings(compute_host="dpc-node-abc", model="q", provider="p")

    await m._calculate_knowledge_score()

    assert len(calls.seen) == 2, "the primary should fail and be retried once"
    assert calls.seen[0]["compute_host"] == "dpc-node-abc"
    assert calls.seen[1]["compute_host"] is None
    assert calls.seen[1]["provider"] == "local_qwen"
    assert calls.seen[1]["provider"] != llm.default_provider


@pytest.mark.asyncio
async def test_with_nothing_local_a_remote_failure_is_not_retried_on_the_paid_default():
    llm = _llm(default="deepseek_flash", deepseek_flash=_provider("deepseek"))
    m = _monitor("group-b88b", llm=llm)
    calls = _Calls()
    m.ai_query_func = calls
    m.set_inference_settings(compute_host="dpc-node-abc", model="q", provider="p")

    await m._calculate_knowledge_score()

    assert [c["compute_host"] for c in calls.seen] == ["dpc-node-abc"], (
        "no local retry exists, and the text default is not one"
    )
