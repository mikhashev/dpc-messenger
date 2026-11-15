"""Extract Knowledge asks before the conversation leaves this machine.

The extraction prompt carries the whole conversation. The target is resolved
once, by the chain the extraction uses (`_infer_inference_settings`). When it is
not proven to run here - a vendor, a peer, a kind this node cannot tell, or a
local engine whose URL names another machine - the press answers
`consent_required` naming it and starts nothing. The resend echoes the target it
was asked about in `extract_consent`; it proceeds only while that is still the target,
and the extraction is pinned to it. A retry after a failure goes silently only
to a target that needs no question; any other retry is refused with
`fallback_needs_consent`.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core import knowledge_routing
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.knowledge_service import KnowledgeService
from dpc_client_core.service import CoreService

CHAT = "ai_chat_consent"
ME = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32


def _provider(ptype, base_url=None, host=None):
    config = {"type": ptype}
    if base_url:
        config["base_url"] = base_url
    if host:
        config["host"] = host
    return SimpleNamespace(config=config, model="m")


def _llm(**providers):
    return SimpleNamespace(providers=dict(providers), default_provider=None,
                           knowledge_provider=None)


def _monitor(llm, conversation_id=CHAT, cold="", ai_query_func=None, stub_generate=True):
    monitor = ConversationMonitor(
        conversation_id=conversation_id,
        participants=[{"node_id": ME, "name": "me", "context": ""}],
        llm_manager=llm,
        settings=SimpleNamespace(get_knowledge_cold_fallback_provider=lambda: cold,
                                 get_cultural_perspectives_enabled=lambda: False),
        ai_query_func=ai_query_func,
        p2p_manager=SimpleNamespace(peers={}, own_addresses=None),
    )
    monitor.message_history = [{"id": "m1", "role": "user", "content": "the plan is settled"}]
    monitor.extraction_calls = []
    if stub_generate:
        async def _generate(**kwargs):
            monitor.extraction_calls.append(dict(monitor.extraction_target or {}))
            return None
        monitor.generate_commit_proposal = _generate
    return monitor


def _service(monitor, llm, peer_metadata=None):
    svc = KnowledgeService.__new__(KnowledgeService)
    svc.p2p_manager = SimpleNamespace(node_id=ME, peers={}, own_addresses=None)
    svc.group_manager = SimpleNamespace(get_group=lambda gid: None)
    svc.peer_metadata = peer_metadata or {}
    svc.consensus_manager = SimpleNamespace(sessions={})
    svc.llm_manager = llm
    svc._get_or_create_conversation_monitor = lambda cid: monitor
    svc.events = []

    async def _broadcast(event, payload):
        svc.events.append((event, payload))

    svc.local_api = SimpleNamespace(broadcast_event=_broadcast)
    return svc


async def _press(llm, *, provenance=None, consent=False, peer_metadata=None, monitor=None):
    monitor = monitor or _monitor(llm)
    if provenance:
        monitor.set_inference_settings(*provenance)
    svc = _service(monitor, llm, peer_metadata)
    result = await KnowledgeService.end_conversation_session(
        svc, monitor.conversation_id, extract_consent=consent)
    return result, monitor, svc


def _echo(answer):
    """What the UI sends back: the target it asked about."""
    return {k: answer[k] for k in knowledge_routing.CONSENT_TARGET_FIELDS}


# --- the question ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_vendor_model_gets_a_question_and_nothing_is_touched():
    llm = _llm(deepseek_flash=_provider("deepseek"))
    monitor = _monitor(llm)
    monitor.full_conversation = ["kept"]

    result, monitor, svc = await _press(llm, provenance=(None, "v4", "deepseek_flash"),
                                        monitor=monitor)

    assert result == {
        "status": "consent_required", "conversation_id": CHAT,
        "provider_alias": "deepseek_flash", "provider_type": "deepseek",
        "provider_kind": "vendor", "host_scope": None, "compute_host": None, "url": None,
    }
    assert monitor.extraction_calls == []
    assert monitor.full_conversation == ["kept"], "the question is asked before the buffers are rebuilt"
    assert svc.events == [], "no failure event: the UI asks, it does not report an error"


@pytest.mark.asyncio
async def test_the_resend_naming_the_same_target_proceeds_pinned_to_it():
    llm = _llm(deepseek_flash=_provider("deepseek"))
    asked, _, _ = await _press(llm, provenance=(None, "v4", "deepseek_flash"))

    result, monitor, _ = await _press(llm, provenance=(None, "v4", "deepseek_flash"),
                                      consent=_echo(asked))

    assert result["status"] == "success"
    assert [c["provider_alias"] for c in monitor.extraction_calls] == ["deepseek_flash"]
    assert monitor.extraction_target is None, "the pin lasts one extraction"


@pytest.mark.asyncio
async def test_a_target_that_changed_since_the_question_is_asked_about_again():
    llm = _llm(deepseek_flash=_provider("deepseek"), claude=_provider("anthropic"))
    asked, _, _ = await _press(llm, provenance=(None, "v4", "deepseek_flash"))

    # Another model answered in the conversation between the question and the yes.
    result, monitor, _ = await _press(llm, provenance=(None, "opus", "claude"),
                                      consent=_echo(asked))

    assert result["status"] == "consent_required"
    assert result["provider_alias"] == "claude"
    assert monitor.extraction_calls == []


@pytest.mark.asyncio
async def test_the_same_alias_moved_to_another_host_is_asked_about_again():
    """The alias is the same word; the place the conversation goes is not."""
    provider = _provider("ollama", host="http://192.168.1.20:11434")
    llm = _llm(qwen=provider)
    asked, _, _ = await _press(llm, provenance=(None, "q", "qwen"))
    assert asked["host_scope"] == "lan"
    provider.config["host"] = "http://some-box:11434"  # moved between the question and the yes

    result, monitor, _ = await _press(llm, provenance=(None, "q", "qwen"), consent=_echo(asked))

    assert result["status"] == "consent_required"
    assert result["host_scope"] == "unknown"
    assert monitor.extraction_calls == []


def test_same_target_compares_all_six_fields():
    target = {"provider_alias": "qwen", "provider_type": "ollama", "provider_kind": "self_hosted",
              "host_scope": "this_machine", "compute_host": None, "url": "http://127.0.0.1:11434",
              "model": "q"}
    echo = {k: target[k] for k in knowledge_routing.CONSENT_TARGET_FIELDS}
    assert knowledge_routing.same_target(echo, target)
    for field, other in (("provider_alias", "x"), ("provider_type", "vllm"),
                         ("provider_kind", "vendor"), ("host_scope", "lan"), ("compute_host", PEER),
                         ("url", "http://127.0.0.1:8080")):
        assert not knowledge_routing.same_target(dict(echo, **{field: other}), target), field


@pytest.mark.asyncio
async def test_a_yes_to_a_machine_on_the_network_does_not_cover_a_vendor_of_the_same_alias():
    vendor = _llm(qwen=_provider("openai_compatible", "https://openrouter.ai/api/v1"))
    lan = _llm(qwen=_provider("ollama", host="http://192.168.1.20:11434"))
    asked, _, _ = await _press(lan, provenance=(None, "q", "qwen"))

    result, monitor, _ = await _press(vendor, provenance=(None, "q", "qwen"), consent=_echo(asked))

    assert result["status"] == "consent_required"
    assert result["provider_kind"] == "vendor"
    assert monitor.extraction_calls == []


@pytest.mark.asyncio
async def test_a_resend_with_extract_consent_false_is_asked_again():
    llm = _llm(deepseek_flash=_provider("deepseek"))

    result, monitor, _ = await _press(llm, provenance=(None, "v4", "deepseek_flash"),
                                      consent=False)

    assert result["status"] == "consent_required"
    assert monitor.extraction_calls == []


@pytest.mark.asyncio
async def test_a_local_engine_proceeds_without_a_question():
    llm = _llm(local_qwen=_provider("ollama"))

    result, monitor, _ = await _press(llm)  # the cold fallback: first local provider

    assert result["status"] == "success"
    assert [c["provider_alias"] for c in monitor.extraction_calls] == ["local_qwen"]


@pytest.mark.asyncio
async def test_a_local_engine_on_a_loopback_host_proceeds_without_a_question():
    llm = _llm(local_qwen=_provider("ollama", host="http://localhost:11434"))

    result, monitor, _ = await _press(llm, provenance=(None, "q", "local_qwen"))

    assert result["status"] == "success"
    assert len(monitor.extraction_calls) == 1


@pytest.mark.asyncio
async def test_a_local_engine_on_another_machine_in_the_lan_gets_a_question():
    """The type says self_hosted; the host says the conversation leaves this box."""
    llm = _llm(lan_qwen=_provider("ollama", host="http://192.168.1.20:11434"))

    result, monitor, _ = await _press(llm, provenance=(None, "q", "lan_qwen"))

    assert result["status"] == "consent_required"
    assert result["provider_kind"] == "self_hosted"
    assert result["host_scope"] == "lan"
    assert monitor.extraction_calls == []


@pytest.mark.asyncio
async def test_openai_compatible_on_loopback_proceeds_without_a_question():
    llm = _llm(lmstudio=_provider("openai_compatible", "http://127.0.0.1:1234/v1"))

    result, monitor, _ = await _press(llm, provenance=(None, "q", "lmstudio"))

    assert result["status"] == "success"
    assert len(monitor.extraction_calls) == 1


@pytest.mark.asyncio
async def test_openai_compatible_on_a_public_host_gets_a_question():
    llm = _llm(router=_provider("openai_compatible", "https://openrouter.ai/api/v1"))

    result, monitor, _ = await _press(llm, provenance=(None, "q", "router"))

    assert result["status"] == "consent_required"
    assert result["provider_kind"] == "vendor"
    assert monitor.extraction_calls == []


@pytest.mark.asyncio
async def test_a_peer_extraction_gets_a_question_even_for_a_model_on_the_peers_card():
    llm = _llm(local_qwen=_provider("ollama"))
    menu = {PEER: {"name": "Linux", "providers": [
        {"alias": "qwen", "type": "ollama", "provider_kind": "self_hosted"}]}}

    result, monitor, _ = await _press(llm, provenance=(PEER, "q", "qwen"),
                                      peer_metadata=menu)

    assert result["status"] == "consent_required"
    assert result["compute_host"] == PEER
    assert result["provider_alias"] == "qwen"
    assert result["provider_kind"] == "self_hosted"
    assert monitor.extraction_calls == []


@pytest.mark.asyncio
async def test_a_kind_this_node_cannot_tell_gets_a_question():
    llm = _llm(local_qwen=_provider("ollama"))

    # Provenance names an alias no longer in the registry: its type is unknown.
    result, monitor, _ = await _press(llm, provenance=(None, "x", "gone_alias"))

    assert result["status"] == "consent_required"
    assert result["provider_kind"] == "unknown"
    assert monitor.extraction_calls == []


@pytest.mark.asyncio
async def test_no_provider_anywhere_asks_no_question():
    """NoKnowledgeProvider names no destination, so there is nothing to ask about;
    the extraction itself (stubbed here) refuses as it always has."""
    llm = _llm(deepseek_flash=_provider("deepseek"))

    result, monitor, _ = await _press(llm)

    assert result["status"] != "consent_required"
    assert monitor.extraction_calls == [{}], "reached the extraction, unpinned"


# --- the retries after a failure ---------------------------------------------


async def _peer_consent(llm):
    """The yes to the question about `qwen` on PEER: its answer, echoed."""
    asked, _, _ = await _press(llm, provenance=(PEER, "q", "qwen"))
    assert asked["status"] == "consent_required"
    return _echo(asked)


def _failing_then(answers):
    """An ai_query_func that fails on the first call and records every call."""
    calls = []

    async def _query(prompt, compute_host, model, provider):
        calls.append({"compute_host": compute_host, "provider": provider})
        if len(calls) == 1:
            raise RuntimeError("the first target is down")
        return {"response": answers}

    return _query, calls


@pytest.mark.asyncio
async def test_a_failed_local_extraction_is_not_retried_on_a_peer_unasked():
    llm = _llm(local_qwen=_provider("ollama"))
    query, calls = _failing_then("")
    monitor = _monitor(llm, conversation_id=PEER, ai_query_func=query, stub_generate=False)

    result, _, svc = await _press(llm, monitor=monitor)

    assert result["reason"] == "fallback_needs_consent"
    assert calls == [{"compute_host": None, "provider": "local_qwen"}]
    assert [e for e, _ in svc.events] == ["knowledge_extraction_failed"]


@pytest.mark.asyncio
async def test_the_refusal_names_the_peer_as_the_question_does():
    llm = _llm(local_qwen=_provider("ollama"))
    query, _ = _failing_then("")
    monitor = _monitor(llm, conversation_id=PEER, ai_query_func=query, stub_generate=False)

    result, _, _ = await _press(llm, monitor=monitor, peer_metadata={PEER: {"name": "Linux"}})

    assert result["reason"] == "fallback_needs_consent"
    assert "its default model on peer Linux" in result["message"]
    assert PEER not in result["message"]


def test_describe_target_uses_the_words_of_the_question():
    assert knowledge_routing.describe_target({"provider_alias": None}) == "an unnamed model"
    assert knowledge_routing.describe_target({"compute_host": PEER}) == (
        f"its default model on peer {PEER[:20]} (unknown)")
    assert knowledge_routing.describe_target(
        {"compute_host": PEER, "provider_alias": "qwen", "provider_kind": "self_hosted"},
        "Linux") == "qwen on peer Linux (on that node's own hardware)"


@pytest.mark.parametrize("kind, word", [
    ("self_hosted", "on that node's own hardware"),
    ("vendor", "cloud"),
    ("unknown", "unknown"),
    (None, "unknown"),          # a menu that names no kind
    ("something_new", "unknown"),
])
def test_a_peer_refusal_carries_the_kind_word_of_the_question(kind, word):
    """The question prints `name (kind word)` for a peer (extractionConsent.ts
    `targetWords`); the refusal names the same peer in the same words."""
    target = {"compute_host": PEER, "provider_alias": "qwen", "provider_kind": kind}
    assert knowledge_routing.describe_target(target, "Linux") == f"qwen on peer Linux ({word})"


UI_CONSENT = Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "extractionConsent.ts"


def test_the_kind_words_are_the_uis():
    found = re.search(r"const KIND_WORD: Record<string, string> = \{(.*?)\};",
                      UI_CONSENT.read_text(encoding="utf-8"), re.S)
    assert found, "extractionConsent.ts no longer declares KIND_WORD"
    ui = {key: single or double for key, single, double in
          re.findall(r"""(\w+): (?:'([^']*)'|"([^"]*)")""", found.group(1))}
    assert ui == knowledge_routing.KIND_WORD


def test_a_local_refusal_prints_the_place_words_of_the_question():
    """The question's brackets for a local target read `type, place` in screen words
    (extractionConsent.ts `targetWords`); the refusal prints the same, not the enum words."""
    assert knowledge_routing.describe_target({
        "provider_alias": "lan_qwen", "url": "http://192.168.1.5:11434", "provider_type": "ollama",
        "provider_kind": "self_hosted", "host_scope": "lan",
    }) == "lan_qwen at 192.168.1.5:11434 (ollama, a machine on your network)"


@pytest.mark.parametrize("target, place", [
    ({"provider_kind": "self_hosted", "host_scope": "lan"}, "a machine on your network"),
    ({"provider_kind": "self_hosted", "host_scope": "unknown"}, "a host this node cannot place"),
    ({"provider_kind": "self_hosted", "host_scope": "this_machine"},
     "a server at an address of this machine"),
    ({"provider_kind": "self_hosted", "host_scope": None}, "another machine"),
    ({"provider_kind": "vendor", "host_scope": None}, "cloud"),
    ({"provider_kind": "unknown", "host_scope": None}, "unknown"),
])
def test_the_local_place_words_are_the_uis(target, place):
    """Each phrase is also a literal in `targetWords`, so a reworded question fails here."""
    ui = UI_CONSENT.read_text(encoding="utf-8")
    if place not in ("cloud", "unknown"):
        assert f"'{place}'" in ui, f"extractionConsent.ts no longer says {place!r}"
    assert knowledge_routing.describe_target(
        {"provider_alias": "q", "provider_type": "ollama", **target}) == f"q (ollama, {place})"
    assert knowledge_routing.describe_target({"provider_alias": "q", **target}) == f"q ({place})"


@pytest.mark.parametrize("url, host", [
    ("http://192.168.1.5:80", "192.168.1.5"),               # the scheme's default port, no path
    ("https://192.168.1.5:443", "192.168.1.5"),
    ("http://192.168.1.20:80/", "192.168.1.20"),            # the scheme's default port
    ("https://box.lan:443/v1", "box.lan"),
    ("http://user:secret@box.lan:11434", "box.lan:11434"),  # no user info
    ("http://[fe80::1]:11434/", "[fe80::1]:11434"),         # IPv6 in brackets
    ("http://[::1]/", "[::1]"),
    ("https://box.lan:8443", "box.lan:8443"),               # a port that is not the default
    ("http://Box.LAN:8080", "box.lan:8080"),                # the browser lowercases the host
    ("localhost:11434", "localhost:11434"),                 # no URL: shown as stored
])
def test_the_printed_host_is_the_browsers(url, host):
    """As the UI's `new URL(url).host` prints it; identity still compares `url` whole."""
    assert knowledge_routing._url_host(url) == host
    assert knowledge_routing.describe_target({"provider_alias": "q", "url": url}) == f"q at {host}"


@pytest.mark.asyncio
async def test_a_consented_peer_that_fails_does_not_fall_to_a_configured_vendor():
    llm = _llm(deepseek_flash=_provider("deepseek"))
    query, calls = _failing_then("")
    monitor = _monitor(llm, cold="deepseek_flash", ai_query_func=query, stub_generate=False)
    consent = await _peer_consent(llm)

    result, _, svc = await _press(llm, provenance=(PEER, "q", "qwen"), consent=consent,
                                  monitor=monitor)

    assert result["reason"] == "fallback_needs_consent"
    assert "deepseek_flash" in result["message"]
    assert calls == [{"compute_host": PEER, "provider": "qwen"}]
    assert [e for e, _ in svc.events] == ["knowledge_extraction_failed"]


@pytest.mark.asyncio
async def test_a_consented_peer_that_fails_falls_silently_to_this_machine():
    llm = _llm(local_qwen=_provider("ollama"))
    query, calls = _failing_then("")
    monitor = _monitor(llm, ai_query_func=query, stub_generate=False)
    consent = await _peer_consent(llm)

    await _press(llm, provenance=(PEER, "q", "qwen"), consent=consent, monitor=monitor)

    assert calls == [{"compute_host": PEER, "provider": "qwen"},
                     {"compute_host": None, "provider": "local_qwen"}]


@pytest.mark.asyncio
async def test_extract_consent_true_keeps_the_old_retries():
    """The Telegram command's blanket yes: the peer retry runs as it did before."""
    llm = _llm(local_qwen=_provider("ollama"))
    query, calls = _failing_then("")
    monitor = _monitor(llm, conversation_id=PEER, ai_query_func=query, stub_generate=False)

    await _press(llm, consent=True, monitor=monitor)

    assert calls[1]["compute_host"] == PEER


# The scoring path (force=False) carries a second copy of the fallback ladder.


def _scoring_monitor(llm, conversation_id, ai_query_func):
    monitor = _monitor(llm, conversation_id=conversation_id, ai_query_func=ai_query_func,
                       stub_generate=False)
    monitor.message_buffer = [
        SimpleNamespace(sender_name="me", text="the plan is settled", timestamp="2026-11-14T10:00:00Z",
                        sender_node_id=ME, sender_id=ME, content="the plan is settled")]
    monitor.fallbacks_need_consent = True
    return monitor


@pytest.mark.asyncio
async def test_scoring_does_not_retry_a_failed_local_call_on_a_peer_unasked():
    llm = _llm(local_qwen=_provider("ollama"))
    query, calls = _failing_then('{"score": 0.9}')
    monitor = _scoring_monitor(llm, PEER, query)
    monitor.set_inference_settings(None, "q", "local_qwen")

    with pytest.raises(knowledge_routing.FallbackNeedsConsent):
        await monitor._calculate_knowledge_score()

    assert calls == [{"compute_host": None, "provider": "local_qwen"}]


@pytest.mark.asyncio
async def test_scoring_does_not_retry_a_failed_peer_on_a_configured_vendor_unasked():
    llm = _llm(deepseek_flash=_provider("deepseek"))
    query, calls = _failing_then('{"score": 0.9}')
    monitor = _scoring_monitor(llm, "group-1", query)
    monitor.settings = SimpleNamespace(get_knowledge_cold_fallback_provider=lambda: "deepseek_flash",
                                       get_cultural_perspectives_enabled=lambda: False)
    monitor.set_inference_settings(PEER, "q", "qwen")

    with pytest.raises(knowledge_routing.FallbackNeedsConsent):
        await monitor._calculate_knowledge_score()

    assert calls == [{"compute_host": PEER, "provider": "qwen"}]


@pytest.mark.asyncio
async def test_scoring_reaches_the_peer_retry_of_a_group_by_its_last_compute_host():
    """Case 2 reads `last_compute_host` as the extraction copy does, so a group
    whose last answer came from a peer is retried there - and, guarded, is not."""
    llm = _llm(local_qwen=_provider("ollama"))
    query, calls = _failing_then('{"score": 0.9}')
    monitor = _scoring_monitor(llm, "group-1", query)
    monitor.set_inference_settings(PEER, "q", "qwen")
    monitor.extraction_target = {"provider_alias": "local_qwen", "compute_host": None, "model": None}

    with pytest.raises(knowledge_routing.FallbackNeedsConsent):
        await monitor._calculate_knowledge_score()
    assert calls == [{"compute_host": None, "provider": "local_qwen"}]

    query, calls = _failing_then('{"score": 0.9}')
    monitor = _scoring_monitor(llm, "group-1", query)
    monitor.fallbacks_need_consent = False
    monitor.set_inference_settings(PEER, "q", "qwen")
    monitor.extraction_target = {"provider_alias": "local_qwen", "compute_host": None, "model": None}

    assert await monitor._calculate_knowledge_score() == 0.9
    assert calls[1] == {"compute_host": PEER, "provider": None}


# --- the self-reflection confirmation inside the extraction ------------------


@pytest.mark.asyncio
async def test_the_self_reflection_confirmation_goes_to_the_pinned_local_target():
    llm = SimpleNamespace(query=AsyncMock(return_value="yes"))
    monitor = ConversationMonitor.__new__(ConversationMonitor)
    monitor.conversation_id = CHAT
    monitor.llm_manager = llm
    monitor.extraction_target = {"provider_alias": "local_qwen", "compute_host": None}

    assert await monitor._is_self_reflection_conversation("text") is True
    assert llm.query.await_args.kwargs["provider_alias"] == "local_qwen"


@pytest.mark.asyncio
async def test_the_self_reflection_confirmation_is_not_sent_for_a_peer_target():
    llm = SimpleNamespace(query=AsyncMock(return_value="yes"))
    monitor = ConversationMonitor.__new__(ConversationMonitor)
    monitor.conversation_id = CHAT
    monitor.llm_manager = llm
    monitor.extraction_target = {"provider_alias": "qwen", "compute_host": PEER}

    assert await monitor._is_self_reflection_conversation("text") is False
    llm.query.assert_not_awaited()


# --- the command door --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_service_passes_extract_consent_from_the_command_payload():
    seen = {}

    async def _end(conversation_id, initiated_by, extract_consent=False, remember=False):
        seen.update(conversation_id=conversation_id, extract_consent=extract_consent)
        return {"status": "success"}

    core = CoreService.__new__(CoreService)
    core.knowledge_service = SimpleNamespace(end_conversation_session=_end)
    echo = {"provider_alias": "x", "compute_host": None}

    await CoreService.end_conversation_session(core, conversation_id=CHAT, extract_consent=echo)

    assert seen == {"conversation_id": CHAT, "extract_consent": echo}


@pytest.mark.asyncio
async def test_a_second_press_does_not_unpin_the_running_extraction():
    llm = _llm(local_qwen=_provider("ollama"))
    monitor = _monitor(llm)
    running = {"provider_alias": "deepseek_flash", "compute_host": None}
    monitor._extracting = True
    monitor.extraction_target = running
    monitor.fallbacks_need_consent = True

    # The monitor turns the second press away with this sentinel string.
    async def _turned_away(**kwargs):
        return "EXTRACTION_IN_PROGRESS"
    monitor.generate_commit_proposal = _turned_away

    result, _, svc = await _press(llm, monitor=monitor)

    assert monitor.extraction_target is running
    assert monitor.fallbacks_need_consent is True
    assert result["status"] == "error"
    assert result["reason"] == "extraction_in_progress"
    assert [e for e, _ in svc.events] == ["knowledge_extraction_failed"]
    assert svc.events[0][1]["reason"] == "extraction_in_progress"
