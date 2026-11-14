"""An unplugged KV snapshots disk starts Strata with saving off (ADR-044, amendment 2026-11-13,
point 6 and "Decided": a missing disk).

A folder whose parent does not exist turns saving off for that launch, never the engine: the
folder is not created, and the start config carries the key empty, because the pinned launcher
makes a missing folder itself, parents included (serve/server.py `slot_save_dir`), and reads a
falsy key as off (`if a.slot_save_path or cfg.get("slot_save_path")`). A parent that exists
but refuses the folder (a file in the way) keeps today's refusal, in words.

Real supervisors, the launch run through `ensure_running`; the child, `/health`, `/props`,
`/metrics` and the smoke are faked at the supervisor's seams. The other P3 test files import the
helpers here.
"""

import json
import logging
from pathlib import Path

import pytest

from dpc_client_core.managers import kv_snapshots
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import LlamaServerError
from dpc_client_core.managers.strata_supervisor import StrataSupervisor

from tests.test_one_local_engine_holds_the_card import _fake_child, card  # noqa: F401
from tests.test_strata_supervisor import _install

ALIAS = "flash"


@pytest.fixture(autouse=True)
def kv_state(monkeypatch):
    """Records and outcomes are module state; a tmp disk is never short here."""
    kv_snapshots.reset_state()
    monkeypatch.setattr(kv_snapshots, "disk_short", lambda folder, nbytes: False)
    monkeypatch.setattr(base_mod, "_last_caller_call_at", {})
    yield
    kv_snapshots.reset_state()


def saving_strata(tmp_path, folder, install=None, **cfg) -> StrataSupervisor:
    """A Strata alias whose KV snapshots folder is `folder`, on the fakes of the card tests."""
    sup = StrataSupervisor(ALIAS, {
        "strata_install_dir": str(install or _install(tmp_path)),
        "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240,
        "engine_watch_interval_s": 3600,
        "vram_release_timeout_s": 0.05,
        "slot_save_path": str(folder),
        **cfg,
    })
    _fake_child(sup)
    return sup


def written_config(sup) -> dict:
    return json.loads(Path(sup.start_config_used).read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_a_missing_parent_starts_the_engine_with_the_key_empty_and_creates_nothing(tmp_path, caplog):
    folder = tmp_path / "usb-drive" / "kv"
    sup = saving_strata(tmp_path, folder)

    with caplog.at_level(logging.WARNING):
        await sup.ensure_running()

    assert sup._proc is not None and sup._proc.returncode is None
    assert written_config(sup)["slot_save_path"] == ""
    assert not (tmp_path / "usb-drive").exists()
    assert sup._kv_folder_off_this_launch() is True
    assert kv_snapshots.off_reason(ALIAS) == kv_snapshots.FOLDER_MISSING
    assert kv_snapshots.status_of(ALIAS)["off_reason"] == kv_snapshots.FOLDER_MISSING
    assert any("off this launch" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)
    assert sup._kv_prepared is False


@pytest.mark.asyncio
async def test_the_base_configs_own_folder_does_not_survive_an_unplugged_alias_folder(tmp_path):
    base_folder = tmp_path / "from-base"
    install = _install(tmp_path, slot_save_path=str(base_folder))
    sup = saving_strata(tmp_path, tmp_path / "usb-drive" / "kv", install=install)

    await sup.ensure_running()

    assert written_config(sup)["slot_save_path"] == ""
    assert not base_folder.exists()


@pytest.mark.asyncio
async def test_the_disk_plugged_back_in_saves_again_from_the_next_launch(tmp_path):
    folder = tmp_path / "usb-drive" / "kv"
    sup = saving_strata(tmp_path, folder)
    await sup.ensure_running()
    assert written_config(sup)["slot_save_path"] == ""

    await sup.stop()
    (tmp_path / "usb-drive").mkdir()
    await sup.ensure_running()

    assert written_config(sup)["slot_save_path"] == str(folder)
    assert folder.is_dir()
    assert sup._kv_folder_off_this_launch() is False and kv_snapshots.off_reason(ALIAS) is None
    assert kv_snapshots.folder_of(ALIAS) == folder


@pytest.mark.asyncio
async def test_a_stop_with_saving_off_this_launch_tries_no_save(tmp_path):
    sup = saving_strata(tmp_path, tmp_path / "usb-drive" / "kv")
    await sup.ensure_running()
    calls = []

    async def action(slot, action, filename, timeout):
        calls.append(action)
        return 200, {}

    sup._slot_action = action
    kv_snapshots.note_clearable("c1")  # a ConversationMonitor exists for it
    kv_snapshots.note_engine_call(ALIAS, caller_kind="agent", agent_id="a1", conversation_id="c1",
                                  prefix=("ab" * 32, 3), at=1.0)

    assert await sup.drain(timeout=None) is None
    assert calls == [] and sup._proc is None


@pytest.mark.asyncio
async def test_a_file_in_the_way_on_an_existing_parent_refuses_the_start_in_words(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    sup = saving_strata(tmp_path, blocker / "kv")
    spawned = []

    async def spawn(cmd, env):
        spawned.append(cmd)
        raise AssertionError("spawned")

    sup._spawn = spawn

    with pytest.raises(LlamaServerError, match="cannot create the KV snapshots folder slot_save_path"):
        await sup.ensure_running()

    assert spawned == [] and sup._proc is None
    assert blocker.read_text(encoding="utf-8") == "x"
    assert sup._kv_prepared is False


@pytest.mark.asyncio
async def test_a_start_that_fails_before_the_child_leaves_the_next_launch_to_prepare_afresh(tmp_path):
    folder = tmp_path / "kv"
    sup = saving_strata(tmp_path, folder, conversation_cache_mib=None)

    with pytest.raises(LlamaServerError, match="conversation_cache_mib"):
        await sup.ensure_running()

    assert sup._kv_prepared is False and sup._proc is None
    sup.config["conversation_cache_mib"] = 10240
    await sup.ensure_running()
    assert written_config(sup)["slot_save_path"] == str(folder)


def test_writing_the_config_outside_a_launch_keeps_the_same_split(tmp_path):
    sup = saving_strata(tmp_path, tmp_path / "usb-drive" / "kv")
    path = sup.write_start_config(5000)
    assert json.loads(path.read_text(encoding="utf-8"))["slot_save_path"] == ""
    assert not (tmp_path / "usb-drive").exists()

    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(LlamaServerError, match="cannot create the KV snapshots folder"):
        saving_strata(tmp_path, blocker / "kv", install=tmp_path / "Strata").write_start_config(5000)
