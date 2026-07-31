"""Tests for memory provider config (ADR-010, MEM-3.11)."""

from dpc_client_core.dpc_agent.memory_config import MemoryConfig, get_memory_config


def test_defaults():
    mc = MemoryConfig()
    assert mc.enabled is False
    assert mc.embedding_model == "BAAI/bge-m3"
    assert mc.active_recall is True
    assert mc.batch_size == 16
    assert mc.memory_provider is None


def test_from_dict():
    mc = MemoryConfig.from_dict({"enabled": True, "batch_size": 8, "future_field": "ignored"})
    assert mc.enabled is True
    assert mc.batch_size == 8


def test_batch_size_survives_roundtrip():
    """It is a tuning knob; losing it on save is how it went missing before."""
    mc = MemoryConfig.from_dict(MemoryConfig(batch_size=24).to_dict())
    assert mc.batch_size == 24


def test_max_tokens_is_configurable_and_survives_roundtrip():
    """The other half of peak memory, and the half batch_size cannot reach."""
    assert MemoryConfig().max_tokens == 4096
    mc = MemoryConfig.from_dict(MemoryConfig(max_tokens=2048).to_dict())
    assert mc.max_tokens == 2048


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
