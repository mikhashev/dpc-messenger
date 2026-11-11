"""The agent configuration dialog's providers list says where each model's calls go.

Every row of `get_agent_model_config`'s `providers` carries `route` (`local` or
`peer`) and `provider_kind` (`self_hosted`, `vendor`, `unknown`), the words of the
runtime budget, decided from the provider's type and base_url and never from its
alias. A peer's row takes the host's own word where it states one. Built in tmp_path.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.agent_service import AgentService
from dpc_client_core.dpc_agent.provider_facts import provider_kind, url_host_scope

PEER = "dpc-node-" + "b" * 32


def _provider(**config):
    return SimpleNamespace(config=config, model=config.get("model"))


LOCAL_ROWS = {
    "qwen": _provider(type="llamacpp_server", model="qwen"),
    "ollama on localhost": _provider(type="ollama", model="llama3"),
    "claude": _provider(type="anthropic", model="c"),
    "gateway to my own box": _provider(
        type="openai_compatible", model="m", base_url="http://127.0.0.1:1234/v1"),
    "named like a local one": _provider(
        type="openai_compatible", model="m", base_url="https://api.example.com/v1"),
    "bob's llama": _provider(type="remote_peer", peer_id=PEER, provider="llama70", model="l"),
    "mystery": _provider(type="something_new", model="x"),
    "nas 192": _provider(type="openai_compatible", model="m", base_url="http://192.168.1.20:8000/v1"),
    "nas 10": _provider(type="openai_compatible", model="m", base_url="http://10.0.0.5:8000/v1"),
    "nas 172.16": _provider(type="openai_compatible", model="m", base_url="http://172.16.0.9/v1"),
    "nas 172.31": _provider(type="openai_compatible", model="m", base_url="http://172.31.255.1/v1"),
    "public 172.32": _provider(type="openai_compatible", model="m", base_url="http://172.32.0.1/v1"),
    "nas ula": _provider(type="openai_compatible", model="m", base_url="http://[fd12:3456::1]:8000/v1"),
    "link local": _provider(type="openai_compatible", model="m", base_url="http://169.254.1.1/v1"),
    "nas lan name": _provider(type="openai_compatible", model="m", base_url="http://nas.lan:8000/v1"),
    "single label": _provider(type="openai_compatible", model="m", base_url="http://ollama:8000/v1"),
    "localhost name": _provider(type="openai_compatible", model="m", base_url="http://localhost:1234/v1"),
    "lan ollama": _provider(type="ollama", model="m", host="192.168.1.30:11434"),
    "default ollama": _provider(type="ollama", model="m"),
    "office server": _provider(type="openai_compatible", model="m", base_url="https://ollama.mycompany.com/v1"),
    "openai itself": _provider(type="openai_compatible", model="m", base_url="https://api.openai.com/v1"),
    "openai default": _provider(type="openai_compatible", model="m"),
}

PEER_META = {
    PEER: {"providers": [
        {"alias": "llama70", "model": "l", "type": "ollama"},
        {"alias": "bob vendor", "model": "v", "type": "openai_compatible", "provider_kind": "vendor"},
        {"alias": "bob local", "model": "v", "type": "openai_compatible", "provider_kind": "self_hosted"},
        {"alias": "bob unsaid", "model": "v", "type": "openai_compatible"},
    ]},
}


@pytest.fixture
def dialog_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    home = tmp_path / ".dpc"
    (home / "agents" / "agent_a").mkdir(parents=True)
    (home / "agents" / "agent_a" / "config.json").write_text(
        json.dumps({"agent_id": "agent_a", "name": "Kotler", "provider_alias": "qwen"}),
        encoding="utf-8")
    (home / "agents" / "_registry.json").write_text(json.dumps({"version": 1, "agents": {
        "agent_a": {"agent_id": "agent_a", "name": "Kotler", "profile_name": "default"}}}),
        encoding="utf-8")
    llm = SimpleNamespace(config_path=home / "providers.json", providers=LOCAL_ROWS)
    svc = AgentService(llm, None, None, PEER_META)

    async def providers_getter():
        return {"providers": [
            {"alias": a, "model": p.model, "type": p.config["type"]} for a, p in LOCAL_ROWS.items()
        ], "default_provider": "qwen"}

    result = asyncio.run(svc.get_agent_model_config("agent_a", providers_getter))
    assert result["status"] == "ok"
    return {(r["alias"], bool(r.get("is_remote"))): r for r in result["providers"]}


def _facts(rows, alias, remote=False):
    row = rows[(alias, remote)]
    return row["route"], row["provider_kind"]


def test_a_model_on_this_machine_is_local_and_self_hosted(dialog_rows):
    assert _facts(dialog_rows, "qwen") == ("local", "self_hosted")
    assert _facts(dialog_rows, "ollama on localhost") == ("local", "self_hosted")


def test_a_vendor_api_is_local_route_and_vendor_kind(dialog_rows):
    assert _facts(dialog_rows, "claude") == ("local", "vendor")


def test_an_openai_compatible_alias_is_read_from_its_base_url_not_its_name(dialog_rows):
    assert _facts(dialog_rows, "gateway to my own box") == ("local", "self_hosted")
    assert _facts(dialog_rows, "named like a local one") == ("local", "vendor")


def test_a_local_remote_peer_alias_is_a_peer_route(dialog_rows):
    assert _facts(dialog_rows, "bob's llama") == ("peer", "self_hosted")


def test_a_peers_row_takes_the_hosts_word_and_falls_back_to_its_type(dialog_rows):
    assert _facts(dialog_rows, "bob vendor", True) == ("peer", "vendor")
    assert _facts(dialog_rows, "bob local", True) == ("peer", "self_hosted")
    # No word from the host and a type that cannot say: not guessed.
    assert _facts(dialog_rows, "bob unsaid", True) == ("peer", "unknown")
    assert _facts(dialog_rows, "llama70", True) == ("peer", "self_hosted")


def test_an_unknown_type_is_unknown_not_local_by_default_of_a_word(dialog_rows):
    assert _facts(dialog_rows, "mystery") == ("local", "unknown")


def _scope(rows, alias):
    return rows[(alias, False)].get("host_scope")


def test_a_private_network_address_has_host_scope_lan_and_keeps_its_kind(dialog_rows):
    for alias in ("nas 192", "nas 10", "nas 172.16", "nas 172.31", "nas ula"):
        assert _scope(dialog_rows, alias) == "lan", alias
        assert _facts(dialog_rows, alias) == ("local", "vendor"), alias


def test_an_ollama_host_on_the_lan_has_scope_lan_too(dialog_rows):
    assert _scope(dialog_rows, "lan ollama") == "lan"
    assert _scope(dialog_rows, "default ollama") is None
    assert _facts(dialog_rows, "default ollama") == ("local", "self_hosted")


def test_what_is_not_a_private_network_has_no_scope(dialog_rows):
    # 172.32 is outside 172.16/12; 169.254 is link-local, not private here.
    for alias in ("public 172.32", "link local", "claude"):
        assert _scope(dialog_rows, alias) is None, alias
    assert _facts(dialog_rows, "public 172.32") == ("local", "vendor")
    assert _facts(dialog_rows, "link local") == ("local", "vendor")


def test_loopback_is_this_machine(dialog_rows):
    assert _facts(dialog_rows, "localhost name") == ("local", "self_hosted")
    assert _scope(dialog_rows, "localhost name") == "this_machine"
    assert _scope(dialog_rows, "gateway to my own box") == "this_machine"


def test_a_lan_name_is_scope_unknown_and_the_kind_is_the_kind_function_s(dialog_rows):
    # The scope is carried verbatim; provider_kind is never overwritten by it.
    for alias, url in (("nas lan name", "http://nas.lan:8000/v1"), ("single label", "http://ollama:8000/v1")):
        assert _scope(dialog_rows, alias) == "unknown"
        assert _facts(dialog_rows, alias) == ("local", provider_kind("openai_compatible", url))


def test_a_generic_type_at_an_unnamed_public_host_carries_that_host_and_keeps_its_kind(dialog_rows):
    row = dialog_rows[("office server", False)]
    assert row["base_url_host"] == "ollama.mycompany.com"
    assert (row["route"], row["provider_kind"]) == ("local", "vendor")
    assert "host_scope" not in row
    # Named vendors, OpenAI's own API, the SDK default, LAN and loopback carry none.
    for alias in ("claude", "openai itself", "openai default", "nas 192", "localhost name"):
        assert "base_url_host" not in dialog_rows[(alias, False)], alias


def test_a_failing_row_is_unknown_not_blank(tmp_path, monkeypatch, dialog_rows):
    import dpc_client_core.dpc_agent.provider_facts as pf

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(pf, "provider_row_facts", boom)
    svc = AgentService(SimpleNamespace(config_path=tmp_path / ".dpc" / "providers.json", providers=LOCAL_ROWS),
                       None, None, PEER_META)

    async def getter():
        return {"providers": [{"alias": "qwen", "model": "q", "type": "llamacpp_server"}], "default_provider": "qwen"}

    result = asyncio.run(svc.get_agent_model_config("agent_a", getter))
    assert result["status"] == "ok"
    assert all(r["provider_kind"] == "unknown" for r in result["providers"])


def test_the_budget_kind_function_is_not_touched_by_the_display_rule():
    assert provider_kind("openai_compatible", "http://192.168.1.20:8000/v1") == "vendor"
    assert provider_kind("openai_compatible", "http://nas.lan:8000/v1") == "vendor"
    assert provider_kind("openai_compatible", "localhost:8080") == "vendor"


class _Own:
    def __init__(self, *ips):
        self.ips = set(ips)

    def contains(self, ip):
        return ip in self.ips


class _BrokenOwn:
    def contains(self, ip):
        raise RuntimeError("no service")


def test_url_host_scope_reads_the_host_only():
    assert url_host_scope("http://192.168.1.20:8000") == "lan"
    assert url_host_scope("192.168.1.30:11434") == "lan"
    assert url_host_scope("http://nas.home.arpa/v1") == "unknown"
    assert url_host_scope("http://my-box.local/v1") == "unknown"
    assert url_host_scope("https://api.openai.com/v1") is None
    assert url_host_scope("https://nas.lan.example.com/v1") is None
    assert url_host_scope("") is None
    assert url_host_scope(None) is None


def test_url_host_scope_says_this_machine_for_loopback_and_own_addresses():
    assert url_host_scope("http://127.0.0.1:1234") == "this_machine"
    assert url_host_scope("http://[::1]:1234") == "this_machine"
    assert url_host_scope("http://localhost:1234/v1") == "this_machine"
    assert url_host_scope("localhost:8080") == "this_machine"
    assert url_host_scope("http://192.168.1.20:8000", _Own("192.168.1.20")) == "this_machine"
    assert url_host_scope("http://192.168.1.21:8000", _Own("192.168.1.20")) == "lan"
    assert url_host_scope("http://192.168.1.20:8000", _BrokenOwn()) == "lan"
    assert url_host_scope("http://192.168.1.20:8000", None) == "lan"


def test_a_host_of_digits_and_dots_that_is_no_address_is_unknown():
    assert url_host_scope("http://010.0.0.5/v1") == "unknown"
    assert url_host_scope("http://1.2.3/v1") == "unknown"
