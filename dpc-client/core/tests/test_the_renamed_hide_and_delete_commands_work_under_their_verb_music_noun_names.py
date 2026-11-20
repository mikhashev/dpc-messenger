"""The four library commands once named `music_*` are `verb_music_noun`, like the rest,
and the old names are gone from the mixin and the allowlist. The job commands are
allowed and are not agent tools (ADR-051 Q1).
"""

import pathlib

import pytest

import dpc_client_core
from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.music.commands import MUSIC_COMMANDS, MusicCommandsMixin
from tests._music_job_commands_helpers import Service, library_tracks, without_tools

NEW = ("hide_music_track", "unhide_music_track", "remove_missing_music_tracks", "delete_generated_music_file")
OLD = ("music_hide_track", "music_unhide_track", "music_remove_missing", "music_delete_generated_file")


def test_the_new_names_are_commands_and_the_old_names_are_gone():
    for name in NEW:
        assert name in MUSIC_COMMANDS and name in ALLOWED_COMMANDS and hasattr(MusicCommandsMixin, name)
    for name in OLD:
        assert name not in ALLOWED_COMMANDS and not hasattr(MusicCommandsMixin, name)


def test_every_job_command_is_allowed_and_none_is_an_agent_tool():
    jobs = ("get_music_capabilities", "propose_music_job", "get_music_job", "list_music_jobs", "cancel_music_job",
            "list_music_takes", "keep_music_take", "discard_music_take", "reject_all_music_takes")
    for name in jobs:
        assert name in MUSIC_COMMANDS and name in ALLOWED_COMMANDS
    root = pathlib.Path(dpc_client_core.__file__).parent
    for path in root.rglob("*.py"):
        if path.name in ("commands.py", "local_api.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in ("keep_music_take", "discard_music_take", "reject_all_music_takes"):
            assert name not in text, (path, name)


@pytest.mark.asyncio
async def test_hide_unhide_remove_missing_and_delete_generated_answer(tmp_path, monkeypatch):
    without_tools(monkeypatch)
    service = Service(tmp_path / "dpc")
    ids = await library_tracks(service, tmp_path / "Music")
    a = ids["a.mp3"]
    assert (await service.hide_music_track(track_id=a))["entry"]["hidden"] is True
    assert not (await service.unhide_music_track(track_id=a))["entry"].get("hidden")
    refused = await service.remove_missing_music_tracks(track_id=a)
    assert refused["status"] == "error" and refused["code"] == "not_missing"
    assert (await service.remove_missing_music_tracks())["removed"] == []
    refused = await service.delete_generated_music_file(track_id=a)
    assert refused["status"] == "error"
    assert (tmp_path / "Music" / "a.mp3").exists()
    await service.stop_music_library()
