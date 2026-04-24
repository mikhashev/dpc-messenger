"""Tests for memory provider config (ADR-010, MEM-3.11)."""

from dpc_client_core.dpc_agent.memory_config import MemoryConfig, get_memory_config


def test_defaults():
    mc = MemoryConfig()
    assert mc.enabled is False
    assert mc.embedding_model == "intfloat/multilingual-e5-small"
    assert mc.active_recall is True
    assert mc.batch_size == 32
    assert mc.memory_provider is None


def test_from_dict():
    mc = MemoryConfig.from_dict({"enabled": True, "batch_size": 16, "future_field": "ignored"})
    assert mc.enabled is True
    assert mc.batch_size == 16


def test_to_dict_roundtrip():
    mc = MemoryConfig(enabled=True, memory_provider="ollama")
    d = mc.to_dict()
    mc2 = MemoryConfig.from_dict(d)
    assert mc2.enabled is True
    assert mc2.memory_provider == "ollama"


def test_get_memory_config_from_agent():
    config = {"memory": {"enabled": True, "embedding_model": "custom/model"}}
    mc = get_memory_config(config)
    assert mc.enabled is True
    assert mc.embedding_model == "custom/model"


def test_get_memory_config_missing():
    mc = get_memory_config({})
    assert mc.enabled is False
