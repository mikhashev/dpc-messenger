"""Extraction is routed by a chain, and silence walks it.

The prompt carries the conversation itself, so «no field set» must not mean
`default_provider` — that is a paid API, and it is how whole transcripts left
the machine. Order: the role chosen on the providers screen, then the
conversation's provenance, then the cold fallback; and no step may be reached
by guessing from the conversation's name.

Step 1 is a global role on the providers screen, not a field on an agent:
extraction is a human's button producing a user-level artefact, and in an
agent's chat the agent's own model is what step 2 resolves to anyway.
"""

from __future__ import annotations

import logging
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
    return SimpleNamespace(get_knowledge_cold_fallback_provider=lambda: cold,
                           get_cultural_perspectives_enabled=lambda: False)


def _monitor(conversation_id, llm=None, settings=None, p2p_manager=None):
    return ConversationMonitor(
        conversation_id=conversation_id,
        participants=[{"node_id": "n", "name": "n", "context": ""}],
        llm_manager=llm if llm is not None else _llm(local=_provider("ollama")),
        settings=settings if settings is not None else _settings(),
        p2p_manager=p2p_manager,
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


# --- THE-COLD-FALLBACK-HIDES-A-D2-REFUSAL-BEHIND-A-SUCCESSFUL-EXTRACTION ----
#
# The retry onto the cold fallback is the right behaviour — knowledge still
# gets extracted — but when the primary call failed because a peer refused
# (D2, an unproved tier), that refusal must not become invisible just because
# the retry succeeded. And the mirror (local failed, try the peer) must not
# dial a peer it already knows is not on a proved connection, since the host
# will refuse it (D2) and the round trip only buys a misleading ERROR.


class _FakeConnection:
    def __init__(self, connection_type):
        self.connection_type = connection_type


class _FakeP2P:
    """Just enough of P2PManager for peer_proof(): a `.peers` mapping."""

    def __init__(self, peers):
        self.peers = peers


@pytest.mark.asyncio
async def test_a_papered_over_remote_refusal_is_recorded_for_the_caller():
    """The extraction result is unchanged (it still runs on local_qwen), but
    the caller must be able to learn the peer refused."""
    llm = _llm(default="deepseek_flash",
               deepseek_flash=_provider("deepseek"),
               local_qwen=_provider("llamacpp_server"))
    m = _monitor("group-b88b", llm=llm)
    calls = _Calls()
    m.ai_query_func = calls
    m.set_inference_settings(compute_host="dpc-node-abc", model="q", provider="p")

    score = await m._calculate_knowledge_score()

    assert score == 0.5, "extraction still succeeds on the cold fallback"
    assert m.last_compute_refusal == {
        "conversation_id": "group-b88b",
        "node_id": "dpc-node-abc",
        "requested_alias": "p",
        "reason": "peer refused",
        "fallback_alias": "local_qwen",
    }


@pytest.mark.asyncio
async def test_a_remote_success_leaves_no_compute_refusal_recorded():
    llm = _llm(default="deepseek_flash",
               deepseek_flash=_provider("deepseek"),
               local_qwen=_provider("llamacpp_server"))
    m = _monitor("group-b88b", llm=llm)
    calls = _Calls(fail_first=False)
    m.ai_query_func = calls
    m.set_inference_settings(compute_host="dpc-node-abc", model="q", provider="p")

    await m._calculate_knowledge_score()

    assert m.last_compute_refusal is None
    assert len(calls.seen) == 1, "the primary call succeeded, no retry needed"


@pytest.mark.asyncio
async def test_generate_commit_proposal_records_the_same_papered_over_refusal():
    """The duplicate of the fallback block inside _generate_commit_proposal
    must not drift from _calculate_knowledge_score's copy."""
    llm = _llm(default="deepseek_flash",
               deepseek_flash=_provider("deepseek"),
               local_qwen=_provider("llamacpp_server"))
    m = _monitor("group-b88b", llm=llm)
    for text in ("hello there", "how are things", "all fine thanks"):
        m.add_message(role="user", content=text, sender_node_id="n", sender_name="n")
    calls = _Calls()
    calls_holder = {"n": 0}

    async def ai_query_func(prompt, compute_host=None, model=None, provider=None):
        calls_holder["n"] += 1
        if calls_holder["n"] == 1:
            raise RuntimeError("peer refused")
        return {"response": '{"entries": [], "topic": "t"}'}

    m.ai_query_func = ai_query_func
    m.set_inference_settings(compute_host="dpc-node-abc", model="q", provider="p")

    proposal = await m._generate_commit_proposal(proposed_by="test", initiated_by="test")

    assert proposal is not None
    assert proposal.topic == "t"
    assert m.last_compute_refusal == {
        "conversation_id": "group-b88b",
        "node_id": "dpc-node-abc",
        "requested_alias": "p",
        "reason": "peer refused",
        "fallback_alias": "local_qwen",
    }


@pytest.mark.asyncio
async def test_a_local_failure_does_not_retry_a_peer_that_is_not_proved(caplog):
    """Case 2 (local failed, try the peer): the host now refuses an unproved
    tier (D2), so a peer known not to be proved must not even be dialled."""
    llm = _llm(local=_provider("ollama"))
    p2p = _FakeP2P({"dpc-node-xyz": _FakeConnection("hub_webrtc")})
    m = _monitor("dpc-node-xyz", llm=llm, p2p_manager=p2p)
    calls = _Calls()  # fails the first (only) call it sees
    m.ai_query_func = calls
    m.set_inference_settings(compute_host=None, model="q", provider="local")

    with caplog.at_level(logging.INFO, logger="dpc_client_core.conversation_monitor"):
        score = await m._calculate_knowledge_score()

    assert score == 0.0, "no fallback landed, the original error propagated to the catch-all"
    assert len(calls.seen) == 1, "the peer must never have been dialled"
    assert any("not proved" in r.message for r in caplog.records), (
        "the skip must be logged, not merely silent"
    )
    assert m.last_compute_refusal is None


@pytest.mark.asyncio
async def test_a_local_failure_still_retries_a_peer_that_is_proved():
    """The mirror of the test above: a proved connection retries exactly as
    it always did — the proof check narrows the skip, it does not remove the
    retry."""
    llm = _llm(local=_provider("ollama"))
    p2p = _FakeP2P({"dpc-node-xyz": _FakeConnection("direct_tls")})
    m = _monitor("dpc-node-xyz", llm=llm, p2p_manager=p2p)
    calls = _Calls()  # fails the first call, succeeds the retry
    m.ai_query_func = calls
    m.set_inference_settings(compute_host=None, model="q", provider="local")

    score = await m._calculate_knowledge_score()

    assert score == 0.5
    assert len(calls.seen) == 2
    assert calls.seen[1]["compute_host"] == "dpc-node-xyz"


@pytest.mark.asyncio
async def test_with_no_p2p_manager_the_peer_retry_is_unchanged():
    """The monitor cannot fake a check it has no way to make — without a
    p2p_manager, Case 2 must behave exactly as it did before this entry."""
    llm = _llm(local=_provider("ollama"))
    m = _monitor("dpc-node-xyz", llm=llm)  # no p2p_manager
    calls = _Calls()
    m.ai_query_func = calls
    m.set_inference_settings(compute_host=None, model="q", provider="local")

    score = await m._calculate_knowledge_score()

    assert score == 0.5
    assert len(calls.seen) == 2, "unchanged: retried the peer with no proof to check"

