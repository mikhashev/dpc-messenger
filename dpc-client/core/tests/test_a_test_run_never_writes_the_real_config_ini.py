"""A test never opens this machine's own ~/.dpc/config.ini (ADR-052 S3 follow-up 3, item 1).

On 2026-11-23 the developer's real config.ini got `[local_transcription] idle_unload_s = 300` from a
test run of the S3 branch: `Settings._seed_missing_keys` writes a key a config written before it
lacks, and several tests build a Settings on the real home without asking for it.
test_conversation_monitoring's `CoreService()` reaches `Settings(DPC_HOME_DIR)`, and the `kg`
fixtures of test_knowledge_graph, test_graph_snapshot and test_legacy_forms reach
`KnowledgeGraph._dpc_home_for` of a tmp root, which is the real home. conftest's
`_settings_never_open_the_real_home` builds every such Settings on the test's tmp_path instead.

Read-only here: the real file's bytes are compared before and after, never written.
"""

from pathlib import Path

from dpc_client_core.settings import Settings

REAL_CONFIG = Path.home() / ".dpc" / "config.ini"


def _real_config_state():
    try:
        return REAL_CONFIG.stat().st_mtime_ns, REAL_CONFIG.read_bytes()
    except FileNotFoundError:
        return None


def test_a_settings_asked_for_the_real_home_is_built_on_the_tests_own_folder(tmp_path):
    before = _real_config_state()
    settings = Settings(Path.home() / ".dpc")
    assert tmp_path in settings.config_file.parents
    assert settings.config_file.exists(), "the fresh default config went to the stand-in"
    settings.set("local_transcription", "queue_wait_s", "601")
    assert _real_config_state() == before


def test_the_tilde_spelling_is_the_real_home_too(tmp_path):
    assert tmp_path in Settings(Path("~") / ".dpc").config_file.parents


def test_the_paths_that_build_one_by_themselves_reach_the_stand_in(tmp_path, monkeypatch):
    from dpc_client_core import service
    from dpc_client_core.dpc_agent.knowledge_graph import _dpc_home_for

    monkeypatch.delenv("DPC_HOME", raising=False)
    before = _real_config_state()
    assert tmp_path in Settings(service.DPC_HOME_DIR).config_file.parents
    assert tmp_path in Settings(_dpc_home_for(tmp_path / "graph-root")).config_file.parents
    assert _real_config_state() == before


def test_a_home_the_test_names_is_left_as_named(tmp_path):
    home = tmp_path / "mine"
    assert Settings(home).config_file == home / "config.ini"
