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
