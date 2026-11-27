"""Integration & regression tests: DPC_HOME is resolved one single way.

Verifies that setting ``DPC_HOME`` routes identity/crypto, Settings/config.ini,
cli.dpc_home(), music lineage & library, node_ledger, gateway, agent_manager,
process_registry, dpc_agent/utils, CoreService, and all neighbours into the
same directory (including non-ASCII paths), and never splits across ~/.dpc.
"""

from pathlib import Path

from cryptography.hazmat.primitives import serialization

from dpc_client_core import node_ledger
from dpc_client_core.dpc_agent import memory

# Captured at import before conftest.py's autouse fixtures point them at tmp_path.
_UNPATCHED_LEDGER_DIR = node_ledger.ledger_dir
_UNPATCHED_EMBEDDING_CACHE_ROOT = memory._embedding_cache_root


def test_dpc_home_env_routes_all_subsystems_into_one_directory(monkeypatch, tmp_path):
    from dpc_protocol import crypto
    from dpc_protocol.paths import dpc_home
    from dpc_client_core import (
        ai_chats,
        cli,
        gateway,
        process_registry,
        service as service_mod,
        signing,
        web_auth,
    )
    from dpc_client_core.dpc_agent import knowledge_graph, utils as agent_utils
    from dpc_client_core.dpc_agent.tools import core as tools_core, process as tools_process
    from dpc_client_core.music import library as music_library, lineage as music_lineage
    from dpc_client_core.providers import neuraldeep_prices
    from dpc_client_core.settings import Settings

    custom_home = tmp_path / "узел_тест" / ".dpc"
    monkeypatch.setenv("DPC_HOME", str(custom_home))

    real_home = (Path.home() / ".dpc").resolve()
    assert custom_home.resolve() != real_home

    # 1. Single resolver + crypto identity
    assert dpc_home() == custom_home
    assert crypto.dpc_home_dir() == custom_home
    crypto.generate_identity()
    node_id, key_file, cert_file = crypto.load_identity()
    assert key_file == custom_home / "node.key"
    assert cert_file == custom_home / "node.crt"
    assert (custom_home / "node.id").read_text(encoding="utf-8").strip() == node_id

    # Signing helper reads from DPC_HOME
    signer = signing.node_signer()
    assert signer is not None and signer.node_id == node_id
    with open(key_file, "rb") as f:
        _ = serialization.load_pem_private_key(f.read(), password=None)

    # 2. Settings / config.ini
    settings = Settings(dpc_home())
    assert settings.config_file == custom_home / "config.ini"
    assert settings.config_file.exists()

    # 3. CLI
    assert cli.dpc_home() == custom_home

    # 4. Music lineage & library
    assert music_lineage._default_dpc_home() == custom_home
    lib = music_library.MusicLibrary(dpc_home())
    assert lib.dpc_home == custom_home
    assert lib.root == custom_home / "music"

    # 5. Node ledger
    assert _UNPATCHED_LEDGER_DIR() == custom_home / "ledger"

    # 6. Gateway (unexecuted dir + default key_path)
    assert gateway._unexecuted_dir() == custom_home / "gateway" / "unexecuted"

    # 7. Process registry
    assert process_registry.registry_path() == custom_home / process_registry.REGISTRY_FILE

    # 8. dpc_agent/utils & agent subsystems
    assert agent_utils.get_agents_base_dir() == custom_home / "agents"
    assert agent_utils.get_agent_root("agent_test_01") == custom_home / "agents" / "agent_test_01"
    assert tools_core._dpc_home() == custom_home
    assert knowledge_graph._dpc_home_for(tmp_path / "outside") == custom_home
    assert _UNPATCHED_EMBEDDING_CACHE_ROOT() == custom_home / "cache" / "embeddings"
    assert ai_chats.conversations_dir() == custom_home / "conversations"
    assert web_auth._dpc_home() == custom_home
    assert neuraldeep_prices.cache_path() == custom_home / "cache" / neuraldeep_prices.CACHE_FILE_NAME

    # tools/process reads config.ini from DPC_HOME
    tools_process.forget_config_cache()
    (custom_home / "config.ini").write_text("[system]\nshell_memory_limit_mb = 512\n", encoding="utf-8")
    assert tools_process._config_ceiling_raw() == "512"

    # 9. CoreService wires Settings, GroupManager, LLMManager, Firewall, PCMCore, etc. to DPC_HOME
    svc = service_mod.CoreService()
    assert svc.settings.config_file == custom_home / "config.ini"
    assert svc.firewall.access_file_path == custom_home / "privacy_rules.json"
    assert svc.llm_manager.config_path == custom_home / "providers.json"
    assert svc.pcm_core.file_path == custom_home / "personal.json"
    assert svc.instruction_manager.instruction_set_manager.instructions_file == custom_home / "instructions.json"
    assert svc.group_manager.dpc_home == custom_home
    assert svc.p2p_manager.peer_cache.cache_file == custom_home / "peer_cache.json"
    assert svc.file_server.dpc_home_dir == custom_home
    assert svc.file_transfer_manager.storage_base_path == custom_home / "conversations"

    gw_srv = gateway.GatewayServer(svc)
    assert gw_srv.key_path == custom_home / gateway.GATEWAY_KEY_NAME

    # 10. Local API token & single_instance lock land under DPC_HOME
    svc.local_api._generate_and_persist_auth_token()
    assert (custom_home / ".ws_token").exists()

    from dpc_client_core import single_instance
    single_instance.acquire()
    try:
        assert (custom_home / "run_service.pid").exists()
    finally:
        single_instance.release()
    assert not (custom_home / "run_service.pid").exists()

    # 11. CC bridges resolve under DPC_HOME
    import cc_agent_bridge
    import cc_group_chat_bridge
    assert cc_agent_bridge._get_history_path("agent_001") == custom_home / "conversations" / "agent_001" / "history.json"
    assert cc_group_chat_bridge._card_file_path() == custom_home / "state" / "card_lease.json"
    assert cc_group_chat_bridge._get_node_id() == node_id


def test_agent_manager_uses_dpc_home_for_reset_and_brief(monkeypatch, tmp_path):
    from dpc_client_core.managers.agent_manager import DpcAgentManager

    custom_home = tmp_path / "агент_дом" / ".dpc"
    monkeypatch.setenv("DPC_HOME", str(custom_home))

    conv_dir = custom_home / "conversations" / "telegram-99999"
    conv_dir.mkdir(parents=True)
    (conv_dir / "history.json").write_text('{"messages": []}', encoding="utf-8")

    # Instantiate minimal DpcAgentManager-like holder via __new__ to test reset_conversation path lookup
    mgr = DpcAgentManager.__new__(DpcAgentManager)
    mgr._agent_monitors = {}
    created_for = []

    class _FakeMonitor:
        def reset_conversation(self, preserve=True, max_sessions=0):
            return 0

    def _fake_get_or_create(cid):
        created_for.append(cid)
        return _FakeMonitor()

    mgr._get_or_create_agent_monitor = _fake_get_or_create
    mgr._get_history_settings = lambda: (True, 0)

    assert mgr.reset_conversation("telegram-99999") is True
    assert created_for == ["telegram-99999"]


def test_explicit_dpc_home_dir_monkeypatch_still_works(monkeypatch, tmp_path):
    from dpc_protocol import commit_integrity, crypto

    patched = tmp_path / "patched_home"
    monkeypatch.setenv("DPC_HOME", str(tmp_path / "env_home"))
    monkeypatch.setattr(crypto, "DPC_HOME_DIR", patched)
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", patched)

    assert crypto.dpc_home_dir() == patched
    crypto.generate_identity()
    node_id, key_file, cert_file = crypto.load_identity()
    assert key_file == patched / "node.key"
    assert cert_file == patched / "node.crt"
    assert node_id.startswith("dpc-node-")
