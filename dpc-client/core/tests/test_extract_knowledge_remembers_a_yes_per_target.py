"""Extract Knowledge: "don't ask again" is a yes bound to one target's facts.

The question's checkbox sends `remember` with the echo. The backend writes an
entry to `privacy_rules.json` (`knowledge_extraction.send_without_asking`) through
the editor's own save. A local alias that matches an entry on all its facts
(the six of the question, the URL among them) is sent to without asking, and
anything that changed asks again; a peer target matches on its alias and its
node id alone, because its kind is the peer menu's word of the moment. Mike's
call, 2026-11-14 (the peer rule, alias + peer, Mike's call in review); the names
are proposed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core import firewall as firewall_module
from dpc_client_core import knowledge_routing
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.knowledge_service import KnowledgeService
from dpc_client_core.service import CoreService

CHAT = "ai_chat_remember"
ME = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
LAN = "http://192.168.1.20:11434"


def _provider(ptype, base_url=None, host=None):
    config = {"type": ptype}
    if base_url:
        config["base_url"] = base_url
    if host:
        config["host"] = host
    return SimpleNamespace(config=config, model="m")


def _llm(**providers):
    return SimpleNamespace(providers=dict(providers), default_provider=None, knowledge_provider=None)


def _monitor(llm, conversation_id=CHAT, ai_query_func=None, stub_generate=True):
    monitor = ConversationMonitor(
        conversation_id=conversation_id,
        participants=[{"node_id": ME, "name": "me", "context": ""}],
        llm_manager=llm,
        settings=SimpleNamespace(get_knowledge_cold_fallback_provider=lambda: "",
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


class _Node:
    """A knowledge service over a real firewall file and the editor's real save path."""

    def __init__(self, tmp_path: Path, llm, rules=None, peer_metadata=None):
        self.rules_path = tmp_path / "privacy_rules.json"
        self.rules_path.write_text(json.dumps(rules or {}), encoding="utf-8")
        self.firewall = ContextFirewall(self.rules_path)
        self.llm = llm
        self.events = []
        self.peer_notices = 0
        self.agent_syncs = 0

        async def _broadcast(event, payload):
            self.events.append((event, payload))

        async def _notify_peers():
            self.peer_notices += 1

        def _sync_agent():
            self.agent_syncs += 1

        agents = SimpleNamespace(_managers={"agent_1": SimpleNamespace(sync_firewall_settings=_sync_agent)})
        core = SimpleNamespace(firewall=self.firewall, llm_manager=SimpleNamespace(providers={"dpc_agent": agents}),
                               local_api=SimpleNamespace(broadcast_event=_broadcast))
        core._merge_unknown_agent_profile_keys = lambda r: CoreService._merge_unknown_agent_profile_keys(core, r)
        core._notify_peers_of_provider_changes = _notify_peers
        core.save_firewall_rules = lambda r: CoreService.save_firewall_rules(core, r)
        self.core = core

        svc = KnowledgeService.__new__(KnowledgeService)
        svc.p2p_manager = SimpleNamespace(node_id=ME, peers={}, own_addresses=None)
        svc.group_manager = SimpleNamespace(get_group=lambda gid: None)
        svc.peer_metadata = peer_metadata or {}
        svc.consensus_manager = SimpleNamespace(sessions={})
        svc.llm_manager = llm
        svc.firewall = self.firewall
        svc._save_firewall_rules = core.save_firewall_rules
        svc.local_api = SimpleNamespace(broadcast_event=_broadcast)
        self.svc = svc

    def entries(self):
        section = json.loads(self.rules_path.read_text(encoding="utf-8")).get("knowledge_extraction", {})
        return section.get("send_without_asking", [])

    async def press(self, *, provenance=None, consent=False, remember=False, monitor=None):
        monitor = monitor or _monitor(self.llm)
        if provenance:
            monitor.set_inference_settings(*provenance)
        self.svc._get_or_create_conversation_monitor = lambda cid: monitor
        result = await KnowledgeService.end_conversation_session(
            self.svc, monitor.conversation_id, extract_consent=consent, remember=remember)
        return result, monitor


def _echo(answer):
    return {k: answer[k] for k in knowledge_routing.CONSENT_TARGET_FIELDS}


VENDOR = (None, "v4", "deepseek_flash")


def _vendor_llm():
    return _llm(deepseek_flash=_provider("deepseek"))


# --- the write ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_with_the_matching_echo_writes_one_entry_through_the_editor_save(tmp_path):
    node = _Node(tmp_path, _vendor_llm())
    asked, _ = await node.press(provenance=VENDOR)

    result, monitor = await node.press(provenance=VENDOR, consent=_echo(asked), remember=True)

    assert result["status"] == "success"
    assert node.entries() == [{
        "provider_alias": "deepseek_flash", "provider_type": "deepseek", "provider_kind": "vendor",
        "host_scope": None, "compute_host": None, "url": None,
    }]
    assert [c["provider_alias"] for c in monitor.extraction_calls] == ["deepseek_flash"]
    # The editor's path ran: the UI is told, and the loaded rules see the entry.
    assert [e for e, _ in node.events] == ["firewall_rules_updated"]
    assert node.firewall.remembered_extraction_targets() == node.entries()


@pytest.mark.asyncio
async def test_a_second_identical_remember_does_not_duplicate(tmp_path):
    node = _Node(tmp_path, _vendor_llm())
    asked, _ = await node.press(provenance=VENDOR)
    await node.press(provenance=VENDOR, consent=_echo(asked), remember=True)

    await node.press(provenance=VENDOR, consent=_echo(asked), remember=True)  # a stale second yes

    assert len(node.entries()) == 1
    await node.svc._remember_extraction_target(
        knowledge_routing.extraction_target(node.llm.providers, None, "deepseek_flash"))
    assert len(node.entries()) == 1


@pytest.mark.asyncio
async def test_remember_with_a_mismatched_echo_writes_nothing_and_asks_again(tmp_path):
    node = _Node(tmp_path, _llm(deepseek_flash=_provider("deepseek"), claude=_provider("anthropic")))
    asked, _ = await node.press(provenance=VENDOR)

    # Another model answered in the conversation between the question and the yes.
    result, monitor = await node.press(provenance=(None, "opus", "claude"),
                                       consent=_echo(asked), remember=True)

    assert result["status"] == "consent_required"
    assert result["provider_alias"] == "claude"
    assert node.entries() == []
    assert monitor.extraction_calls == []
    assert node.events == []


@pytest.mark.asyncio
async def test_a_yes_without_the_box_writes_nothing(tmp_path):
    node = _Node(tmp_path, _vendor_llm())
    asked, _ = await node.press(provenance=VENDOR)

    result, _ = await node.press(provenance=VENDOR, consent=_echo(asked))  # the box left clear

    assert result["status"] == "success"
    assert node.entries() == []
    assert node.events == []


@pytest.mark.asyncio
async def test_remember_without_an_echo_writes_nothing(tmp_path):
    node = _Node(tmp_path, _vendor_llm())

    result, _ = await node.press(provenance=VENDOR, remember=True)

    assert result["status"] == "consent_required"
    assert node.entries() == []


@pytest.mark.asyncio
async def test_a_blanket_yes_never_writes_even_with_remember(tmp_path):
    node = _Node(tmp_path, _vendor_llm())

    result, monitor = await node.press(provenance=VENDOR, consent=True, remember=True)

    assert result["status"] == "success"
    assert len(monitor.extraction_calls) == 1
    assert node.entries() == []
    assert node.events == []


@pytest.mark.asyncio
async def test_a_target_provable_on_this_machine_is_never_written(tmp_path):
    node = _Node(tmp_path, _llm(local_qwen=_provider("ollama")))
    echo = {"provider_alias": "local_qwen", "provider_type": "ollama", "provider_kind": "self_hosted",
            "host_scope": "this_machine", "compute_host": None}

    result, _ = await node.press(provenance=(None, "q", "local_qwen"), consent=echo, remember=True)

    assert result["status"] == "success"
    assert node.entries() == []


@pytest.mark.asyncio
async def test_a_failed_save_does_not_stop_the_extraction(tmp_path):
    node = _Node(tmp_path, _vendor_llm())
    asked, _ = await node.press(provenance=VENDOR)

    async def _refused(rules):
        return {"status": "error", "message": "no"}
    node.svc._save_firewall_rules = _refused

    result, monitor = await node.press(provenance=VENDOR, consent=_echo(asked), remember=True)

    assert result["status"] == "success"
    assert len(monitor.extraction_calls) == 1
    assert node.entries() == []


# --- the read ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_remembered_target_is_sent_to_without_a_question(tmp_path):
    node = _Node(tmp_path, _vendor_llm())
    asked, _ = await node.press(provenance=VENDOR)
    await node.press(provenance=VENDOR, consent=_echo(asked), remember=True)

    result, monitor = await node.press(provenance=VENDOR)  # a fresh press, no echo

    assert result["status"] == "success"
    assert [c["provider_alias"] for c in monitor.extraction_calls] == ["deepseek_flash"]


@pytest.mark.asyncio
async def test_a_remembered_peer_is_sent_to_without_a_question(tmp_path):
    menu = {PEER: {"name": "Linux", "providers": [
        {"alias": "qwen", "type": "ollama", "provider_kind": "self_hosted"}]}}
    node = _Node(tmp_path, _llm(local_qwen=_provider("ollama")), peer_metadata=menu)
    asked, _ = await node.press(provenance=(PEER, "q", "qwen"))
    assert asked["compute_host"] == PEER
    await node.press(provenance=(PEER, "q", "qwen"), consent=_echo(asked), remember=True)
    assert node.entries()[0]["compute_host"] == PEER and node.entries()[0]["url"] is None

    result, monitor = await node.press(provenance=(PEER, "q", "qwen"))

    assert result["status"] == "success"
    assert monitor.extraction_calls[0]["compute_host"] == PEER


@pytest.mark.asyncio
async def test_a_remembered_lan_model_is_sent_to_without_a_question(tmp_path):
    node = _Node(tmp_path, _llm(lan_qwen=_provider("ollama", host=LAN)))
    asked, _ = await node.press(provenance=(None, "q", "lan_qwen"))
    assert asked["host_scope"] == "lan"
    await node.press(provenance=(None, "q", "lan_qwen"), consent=_echo(asked), remember=True)

    result, _ = await node.press(provenance=(None, "q", "lan_qwen"))

    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_the_same_alias_at_another_url_in_the_same_scope_asks_again(tmp_path):
    provider = _provider("ollama", host=LAN)
    node = _Node(tmp_path, _llm(lan_qwen=provider))
    asked, _ = await node.press(provenance=(None, "q", "lan_qwen"))
    await node.press(provenance=(None, "q", "lan_qwen"), consent=_echo(asked), remember=True)
    provider.config["host"] = "http://192.168.1.99:11434"  # still lan, another machine

    result, monitor = await node.press(provenance=(None, "q", "lan_qwen"))

    assert result["status"] == "consent_required"
    assert result["host_scope"] == "lan"
    assert monitor.extraction_calls == []


@pytest.mark.asyncio
async def test_the_same_alias_in_another_host_scope_asks_again(tmp_path):
    provider = _provider("ollama", host=LAN)
    node = _Node(tmp_path, _llm(lan_qwen=provider))
    asked, _ = await node.press(provenance=(None, "q", "lan_qwen"))
    await node.press(provenance=(None, "q", "lan_qwen"), consent=_echo(asked), remember=True)
    provider.config["host"] = "http://some-box:11434"  # scope lan -> unknown

    result, _ = await node.press(provenance=(None, "q", "lan_qwen"))

    assert result["status"] == "consent_required"
    assert result["host_scope"] == "unknown"


@pytest.mark.asyncio
async def test_the_same_alias_of_another_kind_asks_again(tmp_path):
    llm = _llm(qwen=_provider("ollama", host=LAN))
    node = _Node(tmp_path, llm)
    asked, _ = await node.press(provenance=(None, "q", "qwen"))
    await node.press(provenance=(None, "q", "qwen"), consent=_echo(asked), remember=True)
    llm.providers["qwen"] = _provider("openai_compatible", "https://openrouter.ai/api/v1")

    result, _ = await node.press(provenance=(None, "q", "qwen"))

    assert result["status"] == "consent_required"
    assert result["provider_kind"] == "vendor"


@pytest.mark.asyncio
async def test_the_same_alias_of_another_type_asks_again(tmp_path):
    llm = _llm(box=_provider("ollama", host=LAN))
    node = _Node(tmp_path, llm)
    asked, _ = await node.press(provenance=(None, "q", "box"))
    await node.press(provenance=(None, "q", "box"), consent=_echo(asked), remember=True)
    llm.providers["box"] = _provider("llamacpp_server", "http://192.168.1.20:8080")

    result, _ = await node.press(provenance=(None, "q", "box"))

    assert result["status"] == "consent_required"
    assert result["provider_type"] == "llamacpp_server"


@pytest.mark.asyncio
async def test_a_yes_for_a_peer_does_not_cover_another_peer_of_the_same_alias(tmp_path):
    other = "dpc-node-" + "c" * 32
    menu = {PEER: {"name": "Linux", "providers": [{"alias": "qwen", "provider_kind": "self_hosted"}]},
            other: {"name": "Mac", "providers": [{"alias": "qwen", "provider_kind": "self_hosted"}]}}
    node = _Node(tmp_path, _llm(local_qwen=_provider("ollama")), peer_metadata=menu)
    asked, _ = await node.press(provenance=(PEER, "q", "qwen"))
    await node.press(provenance=(PEER, "q", "qwen"), consent=_echo(asked), remember=True)

    result, _ = await node.press(provenance=(other, "q", "qwen"))

    assert result["status"] == "consent_required"
    assert result["compute_host"] == other


@pytest.mark.asyncio
async def test_the_question_carries_the_url(tmp_path):
    node = _Node(tmp_path, _llm(lan_qwen=_provider("ollama", host=LAN)))

    asked, _ = await node.press(provenance=(None, "q", "lan_qwen"))

    assert set(asked) == {"status", "conversation_id", *knowledge_routing.CONSENT_TARGET_FIELDS}
    assert asked["url"] == LAN
    assert node.firewall.remembered_extraction_targets() == []


@pytest.mark.asyncio
async def test_a_yes_to_a_host_that_moved_in_the_same_scope_asks_again_and_stores_nothing(tmp_path):
    provider = _provider("ollama", host="http://192.168.1.5:11434")
    node = _Node(tmp_path, _llm(lan_qwen=provider))
    asked, _ = await node.press(provenance=(None, "q", "lan_qwen"))
    provider.config["host"] = "http://192.168.1.99:11434"  # still lan, another machine

    result, monitor = await node.press(provenance=(None, "q", "lan_qwen"),
                                       consent=_echo(asked), remember=True)

    assert result["status"] == "consent_required"
    assert result["url"] == "http://192.168.1.99:11434"
    assert monitor.extraction_calls == []
    assert node.entries() == []
    assert node.events == []


# --- a peer: alias + peer ----------------------------------------------------

MENU = {PEER: {"name": "Linux", "providers": [
    {"alias": "qwen", "type": "ollama", "provider_kind": "self_hosted"}]}}


async def _remember_peer(node):
    """A real press and yes for `qwen` on PEER while its menu is known (self_hosted)."""
    asked, _ = await node.press(provenance=(PEER, "q", "qwen"))
    assert asked["status"] == "consent_required" and asked["provider_kind"] == "self_hosted"
    result, _ = await node.press(provenance=(PEER, "q", "qwen"), consent=_echo(asked), remember=True)
    assert result["status"] == "success"
    assert [(e["provider_alias"], e["compute_host"], e["provider_kind"]) for e in node.entries()] == [
        ("qwen", PEER, "self_hosted")]


@pytest.mark.asyncio
async def test_a_remembered_peer_holds_after_its_menu_is_gone_and_is_not_stored_twice(tmp_path):
    node = _Node(tmp_path, _llm(local_qwen=_provider("ollama")), peer_metadata=dict(MENU))
    await _remember_peer(node)
    node.svc.peer_metadata = {}  # a restart: no HELLO yet, the kind reads unknown

    result, monitor = await node.press(provenance=(PEER, "q", "qwen"))

    assert result["status"] == "success"
    assert monitor.extraction_calls[0]["provider_kind"] == "unknown"
    await node.svc._remember_extraction_target(monitor.extraction_calls[0])
    assert len(node.entries()) == 1


@pytest.mark.asyncio
async def test_a_peer_restart_between_the_question_and_the_yes_does_not_ask_again(tmp_path):
    """The echo is compared on the remembered yes's ruler: a peer is alias + peer,
    so its kind going from self_hosted to unknown in between changes nothing."""
    node = _Node(tmp_path, _llm(local_qwen=_provider("ollama")), peer_metadata=dict(MENU))
    asked, _ = await node.press(provenance=(PEER, "q", "qwen"))
    assert asked["provider_kind"] == "self_hosted"
    node.svc.peer_metadata = {}  # the peer restarted while the owner read the question

    result, monitor = await node.press(provenance=(PEER, "q", "qwen"), consent=_echo(asked),
                                       remember=True)

    assert result["status"] == "success"
    assert [c["provider_kind"] for c in monitor.extraction_calls] == ["unknown"]
    assert [(e["provider_alias"], e["compute_host"]) for e in node.entries()] == [("qwen", PEER)]


def test_same_target_compares_a_peer_on_alias_and_peer_alone():
    target = {"provider_alias": "qwen", "provider_type": None, "provider_kind": "unknown",
              "host_scope": None, "compute_host": PEER, "url": None, "model": "q"}
    echo = dict({k: target[k] for k in knowledge_routing.CONSENT_TARGET_FIELDS},
                provider_kind="self_hosted", provider_type="ollama")
    assert knowledge_routing.same_target(echo, target)
    assert not knowledge_routing.same_target(dict(echo, provider_alias="llama"), target)
    assert not knowledge_routing.same_target(dict(echo, compute_host="dpc-node-" + "c" * 32), target)
    assert not knowledge_routing.same_target(dict(echo, compute_host=None), target)


@pytest.mark.asyncio
async def test_a_yes_for_one_alias_on_a_peer_does_not_cover_another_alias_there(tmp_path):
    node = _Node(tmp_path, _llm(local_qwen=_provider("ollama")), peer_metadata=dict(MENU))
    await _remember_peer(node)

    result, _ = await node.press(provenance=(PEER, "q", "llama"))

    assert result["status"] == "consent_required"
    assert result["provider_alias"] == "llama"


# --- a retry to a remembered target ------------------------------------------


def _failing_then(answers):
    calls = []

    async def _query(prompt, compute_host, model, provider):
        calls.append({"compute_host": compute_host, "provider": provider})
        if len(calls) == 1:
            raise RuntimeError("the first target is down")
        return {"response": answers}

    return _query, calls


def _scoring_monitor(llm, conversation_id, ai_query_func, remembered):
    monitor = _monitor(llm, conversation_id=conversation_id, ai_query_func=ai_query_func,
                       stub_generate=False)
    monitor.message_buffer = [
        SimpleNamespace(sender_name="me", text="the plan is settled", timestamp="2026-11-14T10:00:00Z",
                        sender_node_id=ME, sender_id=ME, content="the plan is settled")]
    monitor.fallbacks_need_consent = True
    monitor.remembered_targets = remembered
    return monitor


@pytest.mark.asyncio
async def test_a_retry_to_a_peer_remembered_under_an_alias_is_refused_naming_its_model_and_name(tmp_path):
    """The Case-2 retry sends no alias, only `last_model`; a yes bound to alias +
    peer does not cover it (the safe direction), and the refusal says what the
    retry would have sent and to whom, not a default it never asks for."""
    llm = _llm(local_qwen=_provider("ollama"))
    node = _Node(tmp_path, llm, peer_metadata=dict(MENU))
    await _remember_peer(node)
    query, calls = _failing_then("")
    monitor = _monitor(llm, conversation_id=PEER, ai_query_func=query, stub_generate=False)

    result, _ = await node.press(provenance=(None, "q-retry", "local_qwen"), monitor=monitor)

    assert result["reason"] == "fallback_needs_consent"
    assert calls == [{"compute_host": None, "provider": "local_qwen"}]
    assert "q-retry on peer Linux" in result["message"]
    assert "its default model" not in result["message"]
    assert len(node.entries()) == 1


@pytest.mark.asyncio
async def test_the_scoring_retry_to_a_peer_remembered_under_an_alias_raises():
    llm = _llm(local_qwen=_provider("ollama"))
    entry = knowledge_routing.remembered_entry(knowledge_routing.extraction_target(
        llm.providers, PEER, "qwen", peer_metadata=MENU))
    query, calls = _failing_then('{"score": 0.9}')
    monitor = _scoring_monitor(llm, PEER, query, [entry])
    monitor.set_inference_settings(None, "q-retry", "local_qwen")

    with pytest.raises(knowledge_routing.FallbackNeedsConsent) as refused:
        await monitor._calculate_knowledge_score()
    assert refused.value.target["model"] == "q-retry"
    assert "q-retry on peer" in str(refused.value)
    assert calls == [{"compute_host": None, "provider": "local_qwen"}]


@pytest.mark.asyncio
async def test_a_retry_to_a_peer_that_is_not_remembered_still_raises():
    llm = _llm(local_qwen=_provider("ollama"))
    other = knowledge_routing.extraction_target(llm.providers, "dpc-node-" + "c" * 32, None)
    query, calls = _failing_then('{"score": 0.9}')
    monitor = _scoring_monitor(llm, PEER, query, [knowledge_routing.remembered_entry(other)])
    monitor.set_inference_settings(None, "q", "local_qwen")

    with pytest.raises(knowledge_routing.FallbackNeedsConsent):
        await monitor._calculate_knowledge_score()
    assert calls == [{"compute_host": None, "provider": "local_qwen"}]


@pytest.mark.asyncio
async def test_a_retry_to_a_remembered_vendor_alias_passes_the_guard():
    llm = _llm(deepseek_flash=_provider("deepseek"))
    vendor = knowledge_routing.extraction_target(llm.providers, None, "deepseek_flash")
    query, calls = _failing_then('{"score": 0.9}')
    monitor = _scoring_monitor(llm, "group-1", query, [knowledge_routing.remembered_entry(vendor)])
    monitor.settings = SimpleNamespace(get_knowledge_cold_fallback_provider=lambda: "deepseek_flash",
                                       get_cultural_perspectives_enabled=lambda: False)
    monitor.set_inference_settings(PEER, "q", "qwen")

    assert await monitor._calculate_knowledge_score() == 0.9
    assert calls[1] == {"compute_host": None, "provider": "deepseek_flash"}


@pytest.mark.asyncio
async def test_the_service_hands_the_remembered_entries_to_the_monitor_for_one_press(tmp_path):
    llm = _llm(local_qwen=_provider("ollama"), deepseek_flash=_provider("deepseek"))
    node = _Node(tmp_path, llm)
    vendor = knowledge_routing.extraction_target(llm.providers, None, "deepseek_flash")
    node.rules_path.write_text(json.dumps({"knowledge_extraction": {
        "send_without_asking": [knowledge_routing.remembered_entry(vendor)]}}), encoding="utf-8")
    node.firewall.reload()
    monitor = _monitor(llm)
    seen = []

    async def _generate(**kwargs):
        seen.append(list(monitor.remembered_targets))
    monitor.generate_commit_proposal = _generate

    await node.press(provenance=(None, "q", "local_qwen"), monitor=monitor)

    assert seen == [[knowledge_routing.remembered_entry(vendor)]]
    assert monitor.remembered_targets == [], "the list lasts one press"


# --- the firewall ------------------------------------------------------------

_GOOD = {"provider_alias": "q", "provider_type": "ollama", "provider_kind": "self_hosted",
         "host_scope": "lan", "compute_host": None, "url": LAN}


def test_the_entry_fields_of_the_rules_are_the_target_fields_of_the_question():
    assert firewall_module.KNOWLEDGE_EXTRACTION_ENTRY_FIELDS == knowledge_routing.CONSENT_TARGET_FIELDS
    assert "url" in knowledge_routing.CONSENT_TARGET_FIELDS


UI_CONSENT = Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "extractionConsent.ts"


def test_the_ui_echoes_the_target_fields_of_the_backend_in_order():
    found = re.search(r"export const CONSENT_TARGET_FIELDS = \[(.*?)\] as const",
                      UI_CONSENT.read_text(encoding="utf-8"), re.S)
    assert found, "extractionConsent.ts no longer declares CONSENT_TARGET_FIELDS"
    assert tuple(re.findall(r"'([a-z_]+)'", found.group(1))) == knowledge_routing.CONSENT_TARGET_FIELDS


def test_the_validator_accepts_a_good_section_and_the_empty_default():
    assert ContextFirewall.validate_config(
        {"knowledge_extraction": {"send_without_asking": [_GOOD]}}) == (True, [])
    assert ContextFirewall.validate_config(
        {"knowledge_extraction": {"send_without_asking": []}}) == (True, [])
    peer = {"provider_alias": None, "provider_type": None, "provider_kind": "self_hosted",
            "host_scope": None, "compute_host": PEER, "url": None}
    assert ContextFirewall.validate_config(
        {"knowledge_extraction": {"send_without_asking": [peer]}})[0] is True


@pytest.mark.parametrize("section", [
    [],                                                          # not a dict
    {"send_without_asking": "x"},                                # not a list
    {"send_without_asking": ["x"]},                              # entry not an object
    {"send_without_asking": [dict(_GOOD, extra="x")]},           # unknown field
    {"send_without_asking": [dict(_GOOD, host_scope=3)]},        # not a string
    {"send_without_asking": [{"provider_type": "ollama"}]},      # names no alias and no peer
    {"send_without_asking": [dict(_GOOD, provider_alias=None)]}, # likewise
    {"send_without_asking": [dict(_GOOD, compute_host="bob")]},  # not a node id
    {"other": []},                                               # unknown key
])
def test_the_validator_rejects_a_malformed_section(section):
    ok, errors = ContextFirewall.validate_config({"knowledge_extraction": section})
    assert ok is False and errors


def test_a_fresh_rules_file_carries_the_empty_section(tmp_path):
    path = tmp_path / "privacy_rules.json"
    firewall = ContextFirewall(path)
    assert json.loads(path.read_text(encoding="utf-8"))["knowledge_extraction"] == {"send_without_asking": []}
    assert firewall.remembered_extraction_targets() == []


def test_a_malformed_section_on_disk_remembers_nothing(tmp_path):
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps({"knowledge_extraction": {"send_without_asking": "x"}}), encoding="utf-8")
    assert ContextFirewall(path).remembered_extraction_targets() == []


@pytest.mark.asyncio
async def test_an_editor_save_without_the_section_keeps_it(tmp_path):
    node = _Node(tmp_path, _vendor_llm(), rules={"knowledge_extraction": {"send_without_asking": [_GOOD]}})
    snapshot = node.firewall.get_rules_as_dict()
    del snapshot["knowledge_extraction"]  # a client that does not carry it

    saved = await node.core.save_firewall_rules(snapshot)

    assert saved["status"] == "success"
    assert node.entries() == [_GOOD]
    assert node.firewall.remembered_extraction_targets() == [_GOOD]


@pytest.mark.asyncio
async def test_an_editor_save_that_removed_an_entry_removes_it(tmp_path):
    second = dict(_GOOD, provider_alias="r")
    node = _Node(tmp_path, _vendor_llm(), rules={"knowledge_extraction": {"send_without_asking": [_GOOD, second]}})
    snapshot = node.firewall.get_rules_as_dict()
    snapshot["knowledge_extraction"]["send_without_asking"].pop(0)  # the Remove button

    saved = await node.core.save_firewall_rules(snapshot)

    assert saved["status"] == "success"
    assert node.entries() == [second]


@pytest.mark.asyncio
async def test_a_remembered_yes_tells_neither_the_peers_nor_the_agents(tmp_path):
    """Only the remembered section changed: no provider change goes to the
    network and the agents re-read nothing, but the rules hot-reload and the UI
    hears `firewall_rules_updated`."""
    node = _Node(tmp_path, _vendor_llm())
    asked, _ = await node.press(provenance=VENDOR)

    result, _ = await node.press(provenance=VENDOR, consent=_echo(asked), remember=True)

    assert result["status"] == "success" and len(node.entries()) == 1
    assert (node.peer_notices, node.agent_syncs) == (0, 0)
    assert [e for e, _ in node.events] == ["firewall_rules_updated"]
    assert node.firewall.remembered_extraction_targets() == node.entries()


@pytest.mark.asyncio
async def test_an_editor_remove_of_a_remembered_entry_tells_neither_the_peers_nor_the_agents(tmp_path):
    node = _Node(tmp_path, _vendor_llm(), rules={"knowledge_extraction": {"send_without_asking": [_GOOD]}})
    snapshot = node.firewall.get_rules_as_dict()
    snapshot["knowledge_extraction"]["send_without_asking"] = []

    saved = await node.core.save_firewall_rules(snapshot)

    assert saved["status"] == "success" and node.entries() == []
    assert (node.peer_notices, node.agent_syncs) == (0, 0)
    assert [e for e, _ in node.events] == ["firewall_rules_updated"]


@pytest.mark.asyncio
async def test_a_writer_landing_after_a_section_only_save_does_not_widen_it(tmp_path):
    """The comparison is with what this save wrote, not a re-read of the disk: another
    writer's change between the write and the read is its own business."""
    node = _Node(tmp_path, _vendor_llm(), rules={"knowledge_extraction": {"send_without_asking": [_GOOD]}})
    snapshot = node.firewall.get_rules_as_dict()
    snapshot["knowledge_extraction"]["send_without_asking"] = []
    write = node.firewall.save_rules_from_dict

    def write_then_another_writer(rules_dict):
        result = write(rules_dict)
        landed = node.firewall.get_rules_as_dict()
        landed["node_groups"] = {"friends": [PEER]}
        node.firewall.access_file_path.write_text(json.dumps(landed), encoding="utf-8")
        return result

    node.firewall.save_rules_from_dict = write_then_another_writer

    saved = await node.core.save_firewall_rules(snapshot)

    assert saved["status"] == "success"
    assert (node.peer_notices, node.agent_syncs) == (0, 0)


@pytest.mark.asyncio
async def test_an_editor_save_between_the_remembers_read_and_write_is_the_one_lost(tmp_path):
    """The accepted window, in the direction it loses: the service writes the rules it
    read before the editor's Save, so the yes survives and the editor's change does not
    (GLOSSARY `send_without_asking`)."""
    node = _Node(tmp_path, _vendor_llm())
    asked, _ = await node.press(provenance=VENDOR)
    save = node.core.save_firewall_rules

    async def editor_save_lands_first(rules_dict):
        edited = node.firewall.get_rules_as_dict()
        edited["node_groups"] = {"friends": [PEER]}
        assert (await save(edited))["status"] == "success"
        return await save(rules_dict)

    node.svc._save_firewall_rules = editor_save_lands_first

    result, _ = await node.press(provenance=VENDOR, consent=_echo(asked), remember=True)

    assert result["status"] == "success"
    assert len(node.entries()) == 1
    assert "friends" not in json.loads(node.rules_path.read_text(encoding="utf-8")).get("node_groups", {})


@pytest.mark.parametrize("touch_remembered", [False, True])
@pytest.mark.asyncio
async def test_an_editor_save_that_changed_another_section_still_tells_them(tmp_path, touch_remembered):
    node = _Node(tmp_path, _vendor_llm(), rules={"knowledge_extraction": {"send_without_asking": [_GOOD]}})
    snapshot = node.firewall.get_rules_as_dict()
    snapshot["node_groups"] = {"friends": [PEER]}
    if touch_remembered:
        snapshot["knowledge_extraction"]["send_without_asking"] = []

    saved = await node.core.save_firewall_rules(snapshot)

    assert saved["status"] == "success"
    assert (node.peer_notices, node.agent_syncs) == (1, 1)


@pytest.mark.asyncio
async def test_an_editor_save_that_changed_nothing_still_tells_them(tmp_path):
    node = _Node(tmp_path, _vendor_llm(), rules={"knowledge_extraction": {"send_without_asking": [_GOOD]}})

    saved = await node.core.save_firewall_rules(node.firewall.get_rules_as_dict())

    assert saved["status"] == "success"
    assert (node.peer_notices, node.agent_syncs) == (1, 1)


@pytest.mark.asyncio
async def test_an_editor_save_with_a_malformed_entry_is_refused_and_keeps_the_file(tmp_path):
    node = _Node(tmp_path, _vendor_llm(), rules={"knowledge_extraction": {"send_without_asking": [_GOOD]}})
    snapshot = node.firewall.get_rules_as_dict()
    snapshot["knowledge_extraction"]["send_without_asking"].append({"nonsense": 1})

    saved = await node.core.save_firewall_rules(snapshot)

    assert saved["status"] == "error"
    assert node.entries() == [_GOOD]


def test_blocking_a_peer_removes_the_remembered_entries_naming_it(tmp_path):
    peer = dict(_GOOD, provider_alias="qwen", provider_type=None, host_scope=None, url=None,
                compute_host=PEER)
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps({"knowledge_extraction": {"send_without_asking": [_GOOD, peer]}}),
                    encoding="utf-8")
    firewall = ContextFirewall(path)

    ok, _, removed = firewall.block_node(PEER)

    assert ok is True
    assert firewall.remembered_extraction_targets() == [_GOOD]
    assert removed == ["knowledge_extraction.send_without_asking[1]"]


# --- the command door --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_service_passes_remember_from_the_command_payload():
    seen = {}

    async def _end(conversation_id, initiated_by, extract_consent=False, remember=False):
        seen.update(extract_consent=extract_consent, remember=remember)
        return {"status": "success"}

    core = CoreService.__new__(CoreService)
    core.knowledge_service = SimpleNamespace(end_conversation_session=_end)

    await CoreService.end_conversation_session(core, conversation_id=CHAT, extract_consent={"a": 1},
                                               remember=True)

    assert seen == {"extract_consent": {"a": 1}, "remember": True}
