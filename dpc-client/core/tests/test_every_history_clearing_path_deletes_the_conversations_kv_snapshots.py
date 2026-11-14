"""Every path that clears a conversation's history deletes its KV snapshots.

ADR-044, KV snapshots amendment, point 7. Four paths clear a conversation:
`reset_conversation`; `clear_history` after a ratified new session; turning
`persist_history` off (which saves the setting and then calls `clear_history`);
and `clear_before`, the marker applied from another node. All reach
`_advance_live_history_boundary`, which forgets the conversation first, whether
or not the boundary moves. A `clear_before` whose archive fails drops no
history and so deletes nothing.

Real monitors over a temporary home; real `kv_snapshots` folders registered
under two aliases, so "every folder" is checked, not one.
"""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core import conversation_monitor as cm
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.knowledge_service import KnowledgeService
from dpc_client_core.managers import kv_snapshots as kv

CLEARED = "group-aaaa1111"
OTHER = "group-bbbb2222"
OLD, NEW = "2026-01-01T10:00:00+00:00", "2026-03-01T10:00:00+00:00"
MARKER = "2026-02-01T00:00:00+00:00"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    kv.reset_state()
    folders = {}
    for alias in ("flash", "bonsai"):
        folder = tmp_path / f"snaps-{alias}"
        folder.mkdir()
        kv.register_folder(alias, folder)
        folders[alias] = folder
        _snapshot(folder, alias, CLEARED)
        _snapshot(folder, alias, OTHER)
    yield SimpleNamespace(folders=folders)
    kv.reset_state()


def _snapshot(folder: Path, alias: str, conversation_id: str) -> None:
    staging, final = kv.new_snapshot_names(folder)
    kv.release_staging(folder, staging)
    (folder / final).write_bytes(b"x" * 16)
    record = {"agent_id": None, "conversation_id": conversation_id, "caller_kind": "owner",
              "prefix_sha256": "0" * 64, "prefix_messages": 1}
    entry = kv.new_entry(file=final, alias=alias, engine="strata", identity={}, record=record,
                         n_saved=5, nbytes=16, save_ms=1, now=time.time())
    assert kv.add_entry(folder, entry, cap_bytes=10 ** 9)


def _entries(folder: Path, conversation_id: str) -> list:
    return [e for e in kv.read_index(folder)["entries"] if e.get("conversation_id") == conversation_id]


def _monitor(conversation_id: str = CLEARED) -> ConversationMonitor:
    mon = ConversationMonitor(conversation_id=conversation_id, participants=[], llm_manager=None)
    for i, stamp in enumerate((OLD, NEW)):
        mon.add_message(role="user", content=f"m{i}", timestamp=stamp, sender_node_id="dpc-node-" + "a" * 32,
                        sender_name="A", message_id=f"id-{i}")
    mon.save_history()
    return mon


def _assert_cleared(world) -> None:
    for alias, folder in world.folders.items():
        assert _entries(folder, CLEARED) == [], f"{alias}: an entry of the cleared conversation is left"
        assert len(_entries(folder, OTHER)) == 1, f"{alias}: another conversation's entry must stay"
        assert [p.name for p in folder.glob("dpc-kv-*.bin")] == [e["file"] for e in _entries(folder, OTHER)]
    assert kv.is_cleared(CLEARED)
    assert not kv.is_cleared(OTHER)


async def _noop(*args, **kwargs):
    return None


def test_a_new_chat_deletes_the_snapshots_and_marks_the_conversation(world):
    _monitor().reset_conversation()
    _assert_cleared(world)


def test_a_ratified_new_session_deletes_the_snapshots_and_marks_the_conversation(world):
    _monitor().clear_history()
    _assert_cleared(world)


def test_turning_persistence_off_deletes_the_snapshots_though_the_boundary_does_not_move(world):
    mon = _monitor()
    service = SimpleNamespace(
        _get_or_create_conversation_monitor=lambda cid: mon,
        local_api=SimpleNamespace(broadcast_event=_noop),
    )
    result = asyncio.run(KnowledgeService.set_conversation_persist_history(service, CLEARED, False))
    assert result["status"] == "success"
    assert mon.persist_history is False
    assert mon.live_history_boundary is None, "with persistence off the boundary stays put"
    _assert_cleared(world)


def test_a_marker_that_drops_messages_deletes_the_snapshots(world):
    mon = _monitor()
    assert mon.clear_before(MARKER) == 1
    assert [m["id"] for m in mon.message_history] == ["id-1"]
    _assert_cleared(world)


def test_a_marker_that_drops_nothing_deletes_the_snapshots_too(world):
    mon = _monitor()
    assert mon.clear_before("2025-12-01T00:00:00+00:00") == 0
    assert len(mon.message_history) == 2
    _assert_cleared(world)


def test_a_marker_whose_archive_fails_deletes_nothing(world, monkeypatch):
    mon = _monitor()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mon, "_archive_messages", boom)
    assert mon.clear_before(MARKER) == 0
    assert len(mon.message_history) == 2
    for folder in world.folders.values():
        assert len(_entries(folder, CLEARED)) == 1
        assert len(_entries(folder, OTHER)) == 1
    assert not kv.is_cleared(CLEARED)


def test_a_clear_never_fails_on_the_snapshots(world, monkeypatch):
    def boom(conversation_id):
        raise RuntimeError("index unreadable")

    monkeypatch.setattr(cm.kv_snapshots, "forget_conversation", boom)
    mon = _monitor()
    mon.reset_conversation()
    assert mon.message_history == []


def test_a_reset_finds_the_snapshots_of_an_alias_that_never_launched(tmp_path, monkeypatch):
    # No supervisor registered the folder: the service's provider load does.
    from dpc_client_core.service import CoreService

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    kv.reset_state()
    try:
        folder = tmp_path / "snaps-cold"
        folder.mkdir()
        _snapshot(folder, "cold", CLEARED)
        _snapshot(folder, "cold", OTHER)
        providers = {
            "cold": SimpleNamespace(config={"type": "strata", "slot_save_path": str(folder),
                                            "kv_snapshot_max_gib": 4}),
            "off": SimpleNamespace(config={"type": "llamacpp_server"}),
            "api": SimpleNamespace(config={"type": "ollama", "slot_save_path": str(tmp_path / "no")}),
        }
        service = SimpleNamespace(llm_manager=SimpleNamespace(providers=providers))
        assert kv.folder_of("cold") is None
        CoreService._register_kv_snapshot_folders(service)
        assert kv.folder_of("cold") == folder
        assert kv.folder_of("off") is None and kv.folder_of("api") is None

        _monitor().reset_conversation()
        assert _entries(folder, CLEARED) == []
        assert len(_entries(folder, OTHER)) == 1
    finally:
        kv.reset_state()


def test_the_service_and_the_launch_register_one_folder_and_a_missing_parent_is_off(tmp_path, monkeypatch):
    # Two writers of the registration: the service at the providers load, the supervisor at
    # launch. For a folder whose parent is missing they must agree, and the launch must say off.
    from dpc_client_core.managers.llama_server_supervisor import LlamaServerSupervisor
    from dpc_client_core.service import CoreService

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    kv.reset_state()
    try:
        folder = tmp_path / "unplugged-drive" / "snaps"          # the parent does not exist
        cfg = {"type": "llamacpp_server", "gguf_path": str(tmp_path / "m.gguf"),
               "slot_save_path": str(folder), "kv_snapshot_max_gib": 4}
        service = SimpleNamespace(llm_manager=SimpleNamespace(providers={"gone": SimpleNamespace(config=cfg)}))
        CoreService._register_kv_snapshot_folders(service)
        assert kv.folder_of("gone") == folder
        before = kv.status_of("gone")

        supervisor = LlamaServerSupervisor("gone", dict(cfg))
        supervisor._prepare_kv_snapshots()

        after = kv.status_of("gone")
        assert after["off_reason"] == "folder missing"
        assert after["folder"] == before["folder"] == str(folder)
        assert after["cap_bytes"] == before["cap_bytes"] == 4 * kv.GIB
        assert not folder.parent.exists(), "an unplugged disk's folder is never made"
        assert supervisor._kv_folder_off_this_launch()

        CoreService._register_kv_snapshot_folders(service)       # a later providers save
        assert kv.status_of("gone")["folder"] == str(folder)
    finally:
        kv.reset_state()


def test_only_a_conversation_with_a_monitor_is_clearable(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    kv.reset_state()
    try:
        assert not kv.is_clearable("group-with-a-monitor")
        assert not kv.is_clearable("task-nightly")          # a task turn: no monitor, so nothing to clear it
        ConversationMonitor(conversation_id="group-with-a-monitor", participants=[], llm_manager=None)
        assert kv.is_clearable("group-with-a-monitor")
        assert not kv.is_clearable("task-nightly")
    finally:
        kv.reset_state()
