"""Extract Knowledge: a local alias that routes to another node is asked about as that node.

A `remote_peer` alias (or a `dpc_agent` provider with a peer) is configured here and
runs there. The consent question used to read it as a local alias of an unknown type
("qwen Remote Peer (remote_peer, unknown)") and a remembered yes was stored under the
local alias with no peer. The target now carries the peer, the peer's own alias and the
peer menu's kind, so the question and the remembered yes are the peer form's; the call
still goes through the local alias, as before.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dpc_client_core import knowledge_routing
from tests.test_extract_knowledge_remembers_a_yes_per_target import (
    MENU, PEER, _echo, _llm, _monitor, _Node, _provider)

FACTS_FIELDS = ("provider_alias", "provider_type", "provider_kind", "host_scope", "compute_host", "url")


def _remote_peer(peer=PEER, provider="qwen", model="m"):
    config = {"type": "remote_peer", "peer_id": peer, "model": model}
    if provider:
        config["provider"] = provider
    return SimpleNamespace(config=config, model=model, peer_id=peer, remote_provider=provider)


def _dpc_agent_with_peer(peer=PEER, remote_provider="qwen"):
    return SimpleNamespace(config={"type": "dpc_agent"}, peer_id=peer, remote_provider=remote_provider)


def test_a_remote_peer_alias_is_a_peer_target_with_the_peers_alias_and_kind():
    llm = _llm(strata_on_win=_remote_peer())
    target = knowledge_routing.extraction_target(llm.providers, None, "strata_on_win", peer_metadata=MENU)

    assert {f: target[f] for f in FACTS_FIELDS} == {
        "provider_alias": "qwen", "provider_type": None, "provider_kind": "self_hosted",
        "host_scope": None, "compute_host": PEER, "url": None}
    assert target["via_alias"] == "strata_on_win"
    assert not knowledge_routing.proven_on_this_machine(target)
    assert "qwen on peer" in knowledge_routing.describe_target(target)


def test_a_remote_peer_alias_reads_unknown_when_the_menu_is_not_known():
    llm = _llm(strata_on_win=_remote_peer())
    target = knowledge_routing.extraction_target(llm.providers, None, "strata_on_win")

    assert target["provider_kind"] == "unknown" and target["compute_host"] == PEER


def test_a_remote_peer_alias_with_no_provider_key_names_the_local_alias_for_the_peer():
    llm = _llm(strata_on_win=_remote_peer(provider=None))
    target = knowledge_routing.extraction_target(llm.providers, None, "strata_on_win", peer_metadata=MENU)

    assert target["provider_alias"] == "strata_on_win" and target["compute_host"] == PEER


def test_a_dpc_agent_provider_with_a_peer_is_a_peer_target():
    llm = _llm(my_agent=_dpc_agent_with_peer())
    target = knowledge_routing.extraction_target(llm.providers, None, "my_agent", peer_metadata=MENU)

    assert (target["provider_alias"], target["compute_host"], target["provider_type"]) == ("qwen", PEER, None)
    assert target["provider_kind"] == "self_hosted" and target["via_alias"] == "my_agent"


def test_a_local_alias_and_an_explicit_peer_keep_their_old_shape():
    llm = _llm(local_qwen=_provider("ollama"))
    local = knowledge_routing.extraction_target(llm.providers, None, "local_qwen")
    assert local["compute_host"] is None and local["via_alias"] is None and local["provider_type"] == "ollama"

    peer = knowledge_routing.extraction_target(llm.providers, PEER, "qwen", peer_metadata=MENU)
    assert peer["provider_alias"] == "qwen" and peer["compute_host"] == PEER and peer["via_alias"] is None


def test_the_pin_still_sends_through_the_local_alias():
    llm = _llm(strata_on_win=_remote_peer())
    monitor = _monitor(llm)
    monitor.extraction_target = knowledge_routing.extraction_target(
        llm.providers, None, "strata_on_win", model=None, peer_metadata=MENU)

    assert monitor._infer_inference_settings() == (None, None, "strata_on_win")


@pytest.mark.asyncio
async def test_the_question_names_the_peer_and_a_remembered_yes_is_a_peer_record(tmp_path):
    node = _Node(tmp_path, _llm(strata_on_win=_remote_peer()), peer_metadata=dict(MENU))

    asked, _ = await node.press(provenance=(None, None, "strata_on_win"))

    assert asked["status"] == "consent_required"
    assert (asked["provider_alias"], asked["compute_host"], asked["provider_kind"],
            asked["provider_type"]) == ("qwen", PEER, "self_hosted", None)
    result, monitor = await node.press(provenance=(None, None, "strata_on_win"),
                                       consent=_echo(asked), remember=True)
    assert result["status"] == "success"
    assert [(e["provider_alias"], e["compute_host"], e["provider_type"]) for e in node.entries()] == [
        ("qwen", PEER, None)]
    # the extraction itself still went out through the local alias
    assert monitor.extraction_calls[0]["via_alias"] == "strata_on_win"

    again, _ = await node.press(provenance=(None, None, "strata_on_win"))
    assert again["status"] == "success", "the peer record matches the peer target"


@pytest.mark.asyncio
async def test_a_record_stored_under_the_old_reading_does_not_cover_the_peer(tmp_path):
    """An entry written before the fix is a local alias of type remote_peer with no peer;
    it is inert: the peer target asks once more and consent is not widened."""
    old = {"provider_alias": "strata_on_win", "provider_type": "remote_peer", "provider_kind": "unknown",
           "host_scope": None, "compute_host": None, "url": None}
    node = _Node(tmp_path, _llm(strata_on_win=_remote_peer()), peer_metadata=dict(MENU),
                 rules={"knowledge_extraction": {"send_without_asking": [old]}})

    asked, _ = await node.press(provenance=(None, None, "strata_on_win"))

    assert asked["status"] == "consent_required" and asked["compute_host"] == PEER
