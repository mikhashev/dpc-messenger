"""Editing an alias's KV snapshot TTL or cap restarts nothing; a bad value refuses the alias.

ADR-044, amendment of 2026-11-13 (KV snapshots), point 6: `kv_snapshot_ttl_s` and
`kv_snapshot_max_gib` are in neither restart key set (`_FLAG_KEYS`, `_STRATA_FLAG_KEYS`), so a
reload with either changed adopts the running child, on both engines; the folder,
`slot_save_path`, still restarts it. Validation on both engines: the TTL in whole seconds above 0,
the cap a number above 0; on llama-server, which had none, the folder too (absolute, not a root
or a system folder).
"""

from pathlib import Path

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.providers import LlamaServerProvider, StrataProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS, _FLAG_KEYS, _flags_of
from dpc_client_core.providers.strata_provider import _STRATA_FLAG_KEYS

from tests.test_strata_supervisor import _install

GGUF = "D:/models/qwen3.8-27b-Q4_K_M.gguf"
FIELDS = ("kv_snapshot_ttl_s", "kv_snapshot_max_gib")


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(base_mod, "DPC_HOME", tmp_path / "dpc-home")
    _ACTIVE_SUPERVISORS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()


def _llama(**overrides):
    return LlamaServerProvider("local", {"type": "llamacpp_server", "gguf_path": GGUF, **overrides})


def _strata(root, **overrides):
    return StrataProvider("flash", {
        "type": "strata", "alias": "flash", "strata_install_dir": str(root),
        "strata_base_config": "strata-base.json", "conversation_cache_mib": 10240, **overrides,
    })


def test_neither_field_is_a_restart_key_and_the_folder_still_is():
    for field in FIELDS:
        assert field not in _FLAG_KEYS and field not in _STRATA_FLAG_KEYS
    assert "slot_save_path" in _FLAG_KEYS and "slot_save_path" in _STRATA_FLAG_KEYS
    base = {"gguf_path": GGUF}
    assert _flags_of(base) == _flags_of({**base, "kv_snapshot_ttl_s": 600, "kv_snapshot_max_gib": 4})


def test_a_llama_reload_with_a_new_ttl_or_cap_adopts_the_running_child(tmp_path):
    folder = str(tmp_path / "kv")
    first = _llama(slot_save_path=folder)
    first.supervisor.props = {"x": 1}
    second = _llama(slot_save_path=folder, kv_snapshot_ttl_s=3600, kv_snapshot_max_gib=8)
    assert second.supervisor is first.supervisor
    assert second.supervisor.config["kv_snapshot_ttl_s"] == 3600
    third = _llama(slot_save_path=str(tmp_path / "elsewhere"), kv_snapshot_ttl_s=3600, kv_snapshot_max_gib=8)
    assert third.supervisor is not first.supervisor


def test_a_strata_reload_with_a_new_ttl_or_cap_adopts_the_running_launcher(tmp_path):
    root = _install(tmp_path)
    first = _strata(root)
    first.supervisor.props = {"x": 1}
    second = _strata(root, kv_snapshot_ttl_s=7200, kv_snapshot_max_gib=16.5)
    assert second.supervisor is first.supervisor
    assert second.supervisor.config["kv_snapshot_max_gib"] == 16.5


@pytest.mark.parametrize("field,value", [
    ("kv_snapshot_ttl_s", 0), ("kv_snapshot_ttl_s", 1.5), ("kv_snapshot_ttl_s", "86400"),
    ("kv_snapshot_ttl_s", True), ("kv_snapshot_max_gib", -1), ("kv_snapshot_max_gib", 0),
    ("kv_snapshot_max_gib", "32"),
])
def test_a_bad_ttl_or_cap_refuses_the_alias_on_both_engines(tmp_path, field, value):
    with pytest.raises(ValueError, match=field):
        _llama(**{field: value})
    with pytest.raises(ValueError, match=field):
        _strata(_install(tmp_path), **{field: value})


@pytest.mark.parametrize("folder", ["relative/kv", "C:\\", "/"])
def test_a_llama_folder_saved_before_the_check_loads_with_saving_off(folder, caplog):
    """An alias whose folder passed before the check existed still loads; its supervisor gets
    no folder, so the child starts without `--slot-save-path`. The providers save refuses it."""
    from dpc_client_core.managers import kv_snapshots as kv

    with caplog.at_level("WARNING"):
        p = _llama(slot_save_path=folder)
    assert p.supervisor.config.get("slot_save_path") is None
    assert "--slot-save-path" not in p.supervisor.build_command(Path("llama-server"), 8123)
    assert any("KV snapshots are off" in r.getMessage() for r in caplog.records)
    assert any("slot_save_path" in e for e in kv.provider_list_errors(
        [{"alias": "local", "type": "llamacpp_server", "gguf_path": GGUF, "slot_save_path": folder}]))
    # A reload with the same value adopts the running child: the signature compares the same.
    p.supervisor.props = {"x": 1}
    assert _llama(slot_save_path=folder).supervisor is p.supervisor


class _StoppableSupervisor:
    ENGINE = "llama-server"

    def __init__(self, alias, outcome):
        import asyncio

        self.alias = alias
        self._start_lock = asyncio.Lock()
        self.outcome = outcome
        self.reopened = False

    async def drain(self, timeout=None, **kwargs):
        return self.outcome

    def reopen(self):
        self.reopened = True


@pytest.mark.asyncio
async def test_a_stop_by_hand_hands_back_what_its_save_did_and_the_bool_stays():
    from dpc_client_core.providers import llamacpp_server_provider as provider_mod

    saved = {"outcome": "saved", "bytes": 4_000_000_000, "ms": 2100, "reason": None}
    _ACTIVE_SUPERVISORS["local"] = _StoppableSupervisor("local", saved)
    assert await provider_mod.stop_local_engine_with_live_saved("local") == (True, saved)
    assert await provider_mod.stop_local_engine("local") is True
    _ACTIVE_SUPERVISORS["quiet"] = _StoppableSupervisor("quiet", None)
    assert await provider_mod.stop_local_engine_with_live_saved("quiet") == (True, None)
    assert await provider_mod.stop_local_engine_with_live_saved("nobody") == (False, None)
    assert getattr(provider_mod.stop_local_engine_with_live_saved, "runs_on_the_owner_loop", False)


@pytest.mark.asyncio
async def test_the_local_engines_status_carries_a_row_per_alias_with_a_folder_on_both_engines(tmp_path):
    from dpc_client_core.managers import kv_snapshots as kv
    from dpc_client_core.providers import strata_provider

    kv.reset_state()
    try:
        _llama(slot_save_path=str(tmp_path / "llama-kv"))
        LlamaServerProvider("plain", {"type": "llamacpp_server", "gguf_path": GGUF})
        _strata(_install(tmp_path), slot_save_path=str(tmp_path / "strata-kv"))
        kv.register_folder("local", tmp_path, cap_bytes=8 * kv.GIB)
        kv.suspend("flash", kv.FOLDER_MISSING)
        status = await strata_provider.local_engine_status()
        rows = status["kv_snapshots"]
        assert set(rows) == {"local", "flash"}
        assert rows["local"]["cap_bytes"] == 8 * kv.GIB
        assert rows["flash"]["off_reason"] == kv.FOLDER_MISSING
    finally:
        kv.reset_state()


def test_an_empty_or_absent_folder_is_off_and_loads(tmp_path):
    assert _llama().supervisor is not None
    assert _llama(slot_save_path="").supervisor is not None
    assert _llama(slot_save_path=str(tmp_path / "not-yet-made")).supervisor is not None
