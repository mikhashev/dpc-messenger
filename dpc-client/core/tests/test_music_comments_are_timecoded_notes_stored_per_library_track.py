"""Timecoded listening notes (`mp.comment/1`, music/comments.py): stored per library entry
id under `<DPC home>/music/comments/`, validated, kept across a rescan and a relink, moved
on a merge, and served by four commands that broadcast `music_comments_updated`."""

import asyncio
import json
import os

import pytest

from dpc_client_core import local_api
from dpc_client_core.music import commands, schemas
from dpc_client_core.music.commands import MUSIC_COMMANDS, MusicCommandsMixin
from dpc_client_core.music.comments import MAX_TEXT_CHARS
from dpc_client_core.music.identity import MediaTools

COMMANDS = ("list_music_comments", "add_music_comment", "update_music_comment", "delete_music_comment")


class _Settings:
    def __init__(self, home):
        self.dpc_home_dir = home

    def get(self, section, key, fallback=None):
        return "0.05" if (section, key) == ("music", "poll_interval_s") else fallback


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, event, payload):
        self.events.append((event, payload))


class _Service(MusicCommandsMixin):
    def __init__(self, home):
        self.settings = _Settings(home)
        self.local_api = _Api()


@pytest.fixture
def no_tools(monkeypatch):
    original = commands.MusicLibrary.__init__

    def init(self, home, **kw):
        kw.setdefault("tools", MediaTools(ffmpeg=None, ffprobe=None))
        kw.setdefault("save_delay_s", 0.01)
        original(self, home, **kw)

    monkeypatch.setattr(commands.MusicLibrary, "__init__", init)


async def _setup(tmp_path, names=("a.mp3",)):
    music = tmp_path / "Music"
    music.mkdir(exist_ok=True)
    for i, n in enumerate(names):
        (music / n).write_bytes(b"audio-%d" % i)
    service = _Service(tmp_path / "dpc")
    await service.add_music_folder(path=str(music))
    await service.rescan_music_folder()
    await service._music_library_instance.wait_hashed()
    entries = (await service.list_music_library())["entries"]
    return service, music, entries


def test_the_commands_are_registered_slow_and_allowed():
    for name in COMMANDS:
        assert name in MUSIC_COMMANDS and name in local_api.ALLOWED_COMMANDS
        fn = getattr(MusicCommandsMixin, name)
        assert asyncio.iscoroutinefunction(fn) and fn.dpc_slow is True


@pytest.mark.asyncio
async def test_a_comment_round_trips_with_the_spec_fields_and_is_written_on_disk(tmp_path, no_tools):
    service, _, [entry] = await _setup(tmp_path)
    try:
        point = await service.add_music_comment(track_id=entry["id"], start_s=12.5, text="  nice bass  ")
        assert point["status"] == "ok"
        c = point["comment"]
        schemas.validate_comment(c)
        assert (c["kind"], c["t_start"], c["t_end"], c["text"], c["action"]) == ("point", 12.5, None, "nice bass", "note")
        assert c["author"] == {"kind": "human", "name": "Me"} and c["track_id"] == entry["id"]
        assert c["created_at"] and c["updated_at"]
        region = (await service.add_music_comment(track_id=entry["id"], start_s=2, end_s=6, text="intro"))["comment"]
        assert region["kind"] == "region"

        listed = await service.list_music_comments(track_id=entry["id"])
        assert [x["t_start"] for x in listed["comments"]] == [2, 12.5]  # by start time

        path = tmp_path / "dpc" / "music" / "comments" / f"{entry['id']}.json"
        assert len(json.loads(path.read_text(encoding="utf-8"))) == 2
        assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]

        edited = await service.update_music_comment(
            track_id=entry["id"], comment_id=region["id"], text="intro, too long", end_s=None)
        assert edited["comment"]["kind"] == "point" and edited["comment"]["text"] == "intro, too long"
        assert edited["comment"]["created_at"] == region["created_at"]
        # A field that is not passed stays.
        again = await service.update_music_comment(track_id=entry["id"], comment_id=region["id"], start_s=3)
        assert again["comment"]["text"] == "intro, too long" and again["comment"]["t_start"] == 3

        gone = await service.delete_music_comment(track_id=entry["id"], comment_id=point["comment"]["id"])
        assert gone["status"] == "ok"
        assert [x["id"] for x in (await service.list_music_comments(track_id=entry["id"]))["comments"]] == [region["id"]]
    finally:
        await service.stop_music_library()


@pytest.mark.asyncio
async def test_comments_survive_a_new_process(tmp_path, no_tools):
    service, _, [entry] = await _setup(tmp_path)
    try:
        await service.add_music_comment(track_id=entry["id"], start_s=1, text="keep me")
    finally:
        await service.stop_music_library()
    again = _Service(tmp_path / "dpc")
    try:
        got = await again.list_music_comments(track_id=entry["id"])
        assert [c["text"] for c in got["comments"]] == ["keep me"]
    finally:
        await again.stop_music_library()


@pytest.mark.asyncio
async def test_validation_refuses_bad_times_text_and_actions(tmp_path, no_tools):
    service, _, [entry] = await _setup(tmp_path)
    tid = entry["id"]
    service._music_library_instance._entries[tid]["duration_s"] = 100.0
    try:
        async def add(**kw):
            return await service.add_music_comment(track_id=tid, **kw)

        assert (await add(start_s=-1, text="x"))["code"] == "invalid_comment"
        assert (await add(start_s=100.5, text="x"))["code"] == "invalid_comment"      # past the duration
        assert (await add(start_s=100, text="x"))["status"] == "ok"                    # the very end is fine
        assert (await add(start_s=10, end_s=10, text="x"))["code"] == "invalid_comment"  # end must be > start
        assert (await add(start_s=10, end_s=5, text="x"))["code"] == "invalid_comment"
        assert (await add(start_s=10, end_s=101, text="x"))["code"] == "invalid_comment"
        assert (await add(start_s=10, text=""))["code"] == "invalid_comment"
        assert (await add(start_s=10, text="   "))["code"] == "invalid_comment"
        assert (await add(start_s=10, text="x" * (MAX_TEXT_CHARS + 1)))["code"] == "invalid_comment"
        assert (await add(start_s=10, text="x" * MAX_TEXT_CHARS))["status"] == "ok"
        assert (await add(start_s="10", text="x"))["code"] == "invalid_comment"
        assert (await add(start_s=True, text="x"))["code"] == "invalid_comment"
        assert (await add(start_s=float("nan"), text="x"))["code"] == "invalid_comment"
        assert (await add(start_s=10, text="x", action="note"))["status"] == "ok"
        planned = await add(start_s=10, text="x", action="regenerate_region")
        assert planned["code"] == "action_unsupported" and "planned" in planned["message"]
        assert (await add(start_s=10, text="x", action="nonsense"))["code"] == "invalid_comment"

        bad_edit = await service.update_music_comment(track_id=tid, comment_id="cmt_nope", text="y")
        assert bad_edit["code"] == "unknown_comment"
        assert (await service.delete_music_comment(track_id=tid, comment_id="cmt_nope"))["code"] == "unknown_comment"
        assert (await service.list_music_comments(track_id="trk_0123456789abcdef"))["code"] == "unknown_track"
        assert (await service.list_music_comments(track_id="../x"))["code"] == "unknown_track"  # never reaches a path
    finally:
        await service.stop_music_library()


@pytest.mark.asyncio
async def test_an_unknown_duration_lifts_only_the_upper_bound(tmp_path, no_tools):
    service, _, [entry] = await _setup(tmp_path)
    try:
        assert entry["duration_s"] is None
        assert (await service.add_music_comment(track_id=entry["id"], start_s=9999, text="x"))["status"] == "ok"
        assert (await service.add_music_comment(track_id=entry["id"], start_s=-1, text="x"))["status"] == "error"
    finally:
        await service.stop_music_library()


@pytest.mark.asyncio
async def test_every_change_broadcasts_music_comments_updated_with_the_track_id(tmp_path, no_tools):
    service, _, [entry] = await _setup(tmp_path)
    try:
        c = (await service.add_music_comment(track_id=entry["id"], start_s=1, text="a"))["comment"]
        await service.update_music_comment(track_id=entry["id"], comment_id=c["id"], text="b")
        await service.delete_music_comment(track_id=entry["id"], comment_id=c["id"])
        await service.add_music_comment(track_id=entry["id"], start_s=-5, text="refused")  # no event
        await service.list_music_comments(track_id=entry["id"])                              # no event
        await asyncio.sleep(0.01)
        assert service.local_api.events == [("music_comments_updated", {"track_id": entry["id"]})] * 3
    finally:
        await service.stop_music_library()


@pytest.mark.asyncio
async def test_comments_survive_a_rescan_and_a_relink(tmp_path, no_tools):
    service, music, [entry] = await _setup(tmp_path)
    tid = entry["id"]
    try:
        await service.add_music_comment(track_id=tid, start_s=4, text="chorus lands")
        # A rescan with nothing changed.
        await service.rescan_music_folder()
        await service._music_library_instance.wait_hashed()
        assert len((await service.list_music_comments(track_id=tid))["comments"]) == 1

        # The file moves: the entry goes missing, then the new path relinks to the same id.
        sub = music / "moved"
        sub.mkdir()
        os.replace(music / "a.mp3", sub / "b.mp3")
        await service.rescan_music_folder()
        await service._music_library_instance.wait_hashed()
        entries = (await service.list_music_library())["entries"]
        assert [e["id"] for e in entries] == [tid]
        assert not entries[0].get("missing") and entries[0]["path"].endswith("b.mp3")
        got = await service.list_music_comments(track_id=tid)
        assert [c["text"] for c in got["comments"]] == ["chorus lands"]
    finally:
        await service.stop_music_library()


@pytest.mark.asyncio
async def test_comments_of_a_merged_entry_move_to_the_one_that_remains_and_a_removed_entry_keeps_its_file(tmp_path, no_tools):
    service, music, [entry] = await _setup(tmp_path)
    tid = entry["id"]
    comments_dir = tmp_path / "dpc" / "music" / "comments"
    try:
        library = service._music_library_instance
        (music / "z.mp3").write_bytes(b"other")
        await service.rescan_music_folder()
        await library.wait_hashed()
        ids = [e["id"] for e in (await service.list_music_library())["entries"]]
        other = next(i for i in ids if i != tid)
        await service.add_music_comment(track_id=other, start_s=1, text="from the twin")
        await service.add_music_comment(track_id=tid, start_s=2, text="on the keeper")
        library._delete_entry(other, replaced_by=tid)
        got = await service.list_music_comments(track_id=tid)
        assert [c["text"] for c in got["comments"]] == ["from the twin", "on the keeper"]
        assert all(c["track_id"] == tid for c in got["comments"])
        assert not (comments_dir / f"{other}.json").exists()

        # A removal with no replacement keeps the comments file (the documented choice).
        library._delete_entry(tid)
        assert (comments_dir / f"{tid}.json").exists()
        assert (await service.list_music_comments(track_id=tid))["code"] == "unknown_track"
    finally:
        await service.stop_music_library()


@pytest.mark.asyncio
async def test_a_corrupt_comments_file_is_moved_aside_not_lost_or_fatal(tmp_path, no_tools):
    service, _, [entry] = await _setup(tmp_path)
    try:
        path = tmp_path / "dpc" / "music" / "comments" / f"{entry['id']}.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        assert (await service.list_music_comments(track_id=entry["id"]))["comments"] == []
        assert [p.name for p in path.parent.iterdir() if ".corrupt-" in p.name]
        assert (await service.add_music_comment(track_id=entry["id"], start_s=1, text="ok"))["status"] == "ok"
    finally:
        await service.stop_music_library()
