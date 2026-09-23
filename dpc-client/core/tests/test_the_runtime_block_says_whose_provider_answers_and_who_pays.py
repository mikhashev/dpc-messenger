"""The budget in an agent's runtime block names whose engine answers and who pays for it.

It used to print one word, `subscription`, as the default for "nothing configured":
Johnny, on a llama-server model on this machine, read in DPC Research on 2026-09-23
that he was on a subscription with 272M tokens used — neither of which described him.
The three facts come from the agent's provider alias and its type in providers.json;
`billing` appears only when the agent's config sets it.
"""

import json
import types

from dpc_client_core.dpc_agent.context import _build_runtime_section
from dpc_client_core.dpc_agent.provider_facts import provider_facts_for


def _llm(rows, default="deepseek_flash"):
    return types.SimpleNamespace(
        providers={r["alias"]: types.SimpleNamespace(alias=r["alias"], model=r.get("model"), config=r)
                   for r in rows},
        agent_provider=None,
        default_provider=default,
    )


ROWS = [
    {"alias": "qwen3.8 27b", "type": "llamacpp_server", "model": "qwen"},
    {"alias": "deepseek_flash", "type": "deepseek", "model": "deepseek-v4-flash"},
    {"alias": "lmstudio", "type": "openai_compatible", "model": "m", "base_url": "http://127.0.0.1:1234/v1"},
    {"alias": "alice_llama", "type": "remote_peer", "peer_id": "dpc-node-alice", "provider": "llama70"},
]


def _budget(tmp_path, **kwargs):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "state.json").write_text(json.dumps({"tokens_used_total": 272499775}), encoding="utf-8")
    text = _build_runtime_section(tmp_path, {"id": "c", "type": "chat"}, **kwargs)
    return json.loads(text.split("\n\n", 1)[1])["budget"]


def test_a_local_model_agent_is_told_nobody_pays_and_never_subscription(tmp_path):
    facts = provider_facts_for(_llm(ROWS), "qwen3.8 27b")
    budget = _budget(tmp_path, provider_facts=facts)
    assert budget["provider_alias"] == "qwen3.8 27b"
    assert budget["route"] == "local"
    assert budget["provider_kind"] == "local"
    assert budget["tokens_paid_by"] == "nobody"
    assert "billing" not in budget
    assert "subscription" not in json.dumps(budget)


def test_the_lifetime_counter_is_named_as_this_agents(tmp_path):
    budget = _budget(tmp_path, provider_facts=provider_facts_for(_llm(ROWS), "qwen3.8 27b"))
    assert budget["agent_lifetime_tokens"] == 272499775
    assert "tokens_used_total" not in budget


def test_a_vendor_api_on_this_node_is_paid_by_this_node(tmp_path):
    budget = _budget(tmp_path, provider_facts=provider_facts_for(_llm(ROWS), "deepseek_flash"))
    assert (budget["route"], budget["provider_kind"], budget["tokens_paid_by"]) == (
        "local", "vendor", "this_node")


def test_an_openai_compatible_server_on_loopback_is_a_local_model(tmp_path):
    facts = provider_facts_for(_llm(ROWS), "lmstudio")
    assert (facts["provider_kind"], facts["tokens_paid_by"]) == ("local", "nobody")


def test_an_agent_pinned_to_a_peer_is_served_and_paid_by_that_peer():
    peers = {"dpc-node-bob": {"providers": [{"alias": "qwen3.8 27b", "type": "llamacpp_server"}]}}
    facts = provider_facts_for(_llm(ROWS), "qwen3.8 27b", compute_host="dpc-node-bob",
                               peer_metadata=peers)
    assert facts["route"] == "peer"
    assert facts["served_by"] == "dpc-node-bob"
    assert facts["provider_kind"] == "local"
    assert facts["tokens_paid_by"] == "peer"


def test_a_remote_peer_provider_row_is_a_peer_route():
    facts = provider_facts_for(_llm(ROWS), "alice_llama")
    assert facts["route"] == "peer"
    assert facts["served_by"] == "dpc-node-alice"
    assert facts["provider_kind"] == "unknown"
    assert facts["tokens_paid_by"] == "peer"


def test_an_unknown_provider_is_printed_unknown(tmp_path):
    facts = provider_facts_for(_llm([], default=None), "nowhere")
    budget = _budget(tmp_path, provider_facts=facts)
    assert budget["provider_kind"] == "unknown"
    assert budget["tokens_paid_by"] == "unknown"


def test_no_provider_facts_at_all_prints_unknown_not_subscription(tmp_path):
    budget = _budget(tmp_path)
    assert budget["tokens_paid_by"] == "unknown"
    assert "subscription" not in json.dumps(budget)


def test_an_explicit_pay_per_use_agent_keeps_its_dollar_fields(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "state.json").write_text(json.dumps({"spent_usd": 12.5, "budget_usd": 50}), encoding="utf-8")
    text = _build_runtime_section(
        tmp_path, {"id": "c", "type": "chat"}, billing_model="pay_per_use",
        provider_facts=provider_facts_for(_llm(ROWS), "deepseek_flash"))
    budget = json.loads(text.split("\n\n", 1)[1])["budget"]
    assert budget["billing"] == "pay_per_use"
    assert budget["spent_usd"] == 12.5
    assert budget["total_usd"] == 50.0
    assert budget["remaining_usd"] == 37.5
    assert budget["tokens_paid_by"] == "this_node"


def test_an_explicit_subscription_is_still_printed(tmp_path):
    budget = _budget(tmp_path, billing_model="subscription",
                     provider_facts=provider_facts_for(_llm(ROWS), "deepseek_flash"))
    assert budget["billing"] == "subscription"
    assert budget["agent_lifetime_tokens"] == 272499775


def test_the_manager_marks_billing_explicit_only_when_the_config_sets_it():
    from dpc_client_core.managers.agent_manager import DpcAgentManager
    mgr = DpcAgentManager.__new__(DpcAgentManager)
    mgr.config = {"budget_usd": 50}
    assert mgr._billing_kwargs() == {"billing_model": "subscription", "billing_model_explicit": False}
    mgr.config = {"billing_model": "pay_per_use"}
    assert mgr._billing_kwargs() == {"billing_model": "pay_per_use", "billing_model_explicit": True}


def test_the_agent_hands_the_block_no_billing_word_unless_configured():
    from dpc_client_core.dpc_agent.agent import AgentConfig, DpcAgent
    agent = DpcAgent.__new__(DpcAgent)
    agent.config = AgentConfig()
    assert agent._runtime_billing_model() is None
    agent.config = AgentConfig(billing_model="pay_per_use", billing_model_explicit=True)
    assert agent._runtime_billing_model() == "pay_per_use"


def test_the_agent_reads_its_facts_from_the_adapter_it_calls_through():
    from dpc_client_core.dpc_agent.agent import DpcAgent
    from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
    agent = DpcAgent.__new__(DpcAgent)
    agent._provider_alias = "qwen3.8 27b"
    agent._service = types.SimpleNamespace(peer_metadata={})
    agent.llm = DpcLlmAdapter(_llm(ROWS), provider_alias="qwen3.8 27b", compute_host="dpc-node-bob")
    facts = agent._provider_facts()
    assert (facts["route"], facts["served_by"], facts["tokens_paid_by"]) == ("peer", "dpc-node-bob", "peer")
