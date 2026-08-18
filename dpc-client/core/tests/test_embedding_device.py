import logging

import pytest

from dpc_client_core.dpc_agent import memory
from dpc_client_core.dpc_agent.agent import AgentConfig
from dpc_client_core.dpc_agent.memory_config import MemoryConfig, get_memory_config


@pytest.fixture(autouse=True)
def _clean_singletons():
    memory._singleton_providers.clear()
    yield
    memory._singleton_providers.clear()


def test_the_device_is_absent_by_default_so_nothing_changes_for_an_install_that_never_sets_it():
    assert MemoryConfig().embedding_device is None
    assert get_memory_config({}).embedding_device is None
    assert AgentConfig().embedding_device is None


def test_a_device_written_in_the_agent_config_survives_the_round_trip():
    cfg = get_memory_config({"memory": {"embedding_device": "cpu"}})
    assert cfg.embedding_device == "cpu"
    assert MemoryConfig.from_dict(cfg.to_dict()).embedding_device == "cpu"


def test_the_first_caller_decides_the_device():
    provider = memory.get_embedding_provider(model_name="test/device-a", device="cpu")
    assert provider.device == "cpu"


def test_a_later_caller_asking_for_another_device_is_told_it_was_not_obeyed(caplog):
    memory.get_embedding_provider(model_name="test/device-b", device="cpu")
    with caplog.at_level(logging.WARNING, logger=memory.log.name):
        again = memory.get_embedding_provider(model_name="test/device-b", device="cuda")
    assert again.device == "cpu"
    assert any("first caller decides" in r.getMessage() for r in caplog.records)


def test_a_later_caller_that_states_nothing_is_silent(caplog):
    memory.get_embedding_provider(model_name="test/device-c", device="cpu")
    with caplog.at_level(logging.WARNING, logger=memory.log.name):
        again = memory.get_embedding_provider(model_name="test/device-c")
    assert again.device == "cpu"
    assert caplog.records == []


def test_a_later_caller_naming_the_same_device_is_silent(caplog):
    memory.get_embedding_provider(model_name="test/device-d", device="cpu")
    with caplog.at_level(logging.WARNING, logger=memory.log.name):
        memory.get_embedding_provider(model_name="test/device-d", device="cpu")
    assert caplog.records == []


def test_the_model_name_has_a_default_in_the_dataclass_so_removing_it_from_config_is_safe():
    # The nine agent profiles carried embedding_model until 2026-08-18 and it was
    # deleted from all of them; what keeps them on bge-m3 is this default alone.
    assert MemoryConfig().embedding_model == "BAAI/bge-m3"
    assert get_memory_config({}).embedding_model == "BAAI/bge-m3"
    assert get_memory_config({"memory": {"enabled": True}}).embedding_model == "BAAI/bge-m3"


def test_the_agent_config_carries_the_model_name_as_well_as_the_device():
    assert AgentConfig().embedding_model is None
    assert AgentConfig(embedding_model="some/model").embedding_model == "some/model"


def test_a_stated_model_name_builds_its_own_provider():
    a = memory.get_embedding_provider(model_name="test/model-a", device="cpu")
    b = memory.get_embedding_provider(model_name="test/model-b", device="cpu")
    assert a is not b
    assert a.model_name == "test/model-a"
    assert b.model_name == "test/model-b"


def test_cuda_keeps_fp16_and_a_bf16_cpu_gets_bfloat16(monkeypatch):
    import torch
    p = memory.EmbeddingProvider(model_name="test/dtype", device="cuda")
    assert p._torch_dtype() is torch.float16

    p = memory.EmbeddingProvider(model_name="test/dtype", device="cpu")
    monkeypatch.setattr(p, "_cpu_supports_bfloat16", lambda: True)
    assert p._torch_dtype() is torch.bfloat16


def test_a_cpu_without_native_bfloat16_stays_in_fp32_rather_than_fp16(monkeypatch):
    # fp16 on a CPU that emulates it measured 6.3x slower than fp32 on this box,
    # so "no native bf16" must fall back up to fp32, never sideways to fp16.
    p = memory.EmbeddingProvider(model_name="test/dtype", device="cpu")
    monkeypatch.setattr(p, "_cpu_supports_bfloat16", lambda: False)
    assert p._torch_dtype() is None


def test_a_probe_that_raises_is_read_as_unsupported(monkeypatch):
    import torch
    p = memory.EmbeddingProvider(model_name="test/dtype", device="cpu")
    monkeypatch.setattr(torch.cpu, "_is_avx512_bf16_supported",
                        lambda: (_ for _ in ()).throw(RuntimeError("gone")), raising=False)
    monkeypatch.setattr(torch.ops.mkldnn, "_is_mkldnn_bf16_supported",
                        lambda: (_ for _ in ()).throw(RuntimeError("gone")), raising=False)
    assert p._cpu_supports_bfloat16() is False
    assert p._torch_dtype() is None
