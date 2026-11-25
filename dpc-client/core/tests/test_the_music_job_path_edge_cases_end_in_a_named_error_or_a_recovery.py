"""The music job path's recoverable edge cases (backlog card THE-MUSIC-JOB-PATH-TURNS-SEVERAL-
RECOVERABLE-EDGE-CASES-INTO-AN-INTERNAL-ERROR): each ends in a named error or a recovery, never
an uncaught exception that the runner reports as 'internal error'.

Covered here: an oversized text is refused at the door and a payload refused mid-run is a named
failure; the yue2 split cache is checked by content hash; comment writes and a merge share one
lock and a track holds a bounded number of comments; a relative path in a hand-edited
library.json is taken from the library's own folder, never from the working directory; a stop is
not refused for a job that only waits for the card; the capabilities report runs off the loop.
The payload adoption (item 1), the broken-adapter skip (item 2) and the one canonical peaks cache
are tested in their own files.
"""

import asyncio
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from dpc_client_core.music import comments as comments_module
from dpc_client_core.music import commands, job_schemas as js
from dpc_client_core.music import jobs as jobs_module
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.jobs import MusicJobError
from dpc_client_core.music.library import MusicLibrary
from dpc_client_core.music.runner import LM_RESULT_PAYLOAD
from tests._acestep_helpers import Rig, free_env, from_text, install_fake_engine, wait_until
from tests._music_job_commands_helpers import Service, without_tools
from tests._yue2_helpers import VOICES, install_fake_yue2, make_yue2

DONE = (js.REVIEW, js.FAILED, js.CANCELLED, js.UNSUPPORTED)


# -- item 3: the door and the runner guard ------------------------------------------------------


def test_a_text_that_cannot_fit_a_payload_is_refused_at_the_door():
    assert js.normalize_params_from_text({"lyrics": "la la", "style": "pop"})["style"] == "pop"
    # the worst case JSON escaping (six bytes per control character) of the largest accepted text fits
    assert 6 * js.MAX_SONG_TEXT_BYTES + 64 * 1024 < jobs_module.MAX_PAYLOAD_BYTES
    with pytest.raises(js.ParamsError) as err:
        js.normalize_params_from_text({"lyrics": "я" * js.MAX_SONG_TEXT_BYTES, "style": "pop"})  # 2 bytes each
    assert err.value.field == "lyrics" and "shorten" in str(err.value)
    with pytest.raises(js.ParamsError):
        js.normalize_params_from_text({"lyrics": "x", "style": "s" * (js.MAX_SONG_TEXT_BYTES + 1)})
    assert js.normalize_params_from_text({"lyrics": "x" * (js.MAX_SONG_TEXT_BYTES - 3), "style": "pop"})


def test_a_lone_surrogate_in_the_text_is_refused_not_a_crash_later():
    with pytest.raises(js.ParamsError) as err:
        js.normalize_params_from_text({"lyrics": "ok \ud800", "style": "pop"})
    assert err.value.field == "lyrics"


def test_the_store_names_an_oversized_payload_and_says_what_to_shorten(tmp_path):
    from tests._music_jobs_helpers import queued_job, store_at
    store = store_at(tmp_path)
    job = queued_job(store)
    with pytest.raises(MusicJobError) as err:
        store.write_payload(job["id"], "lm.request.json", b"x" * (jobs_module.MAX_PAYLOAD_BYTES + 1))
    assert err.value.code == "payload_too_large" and "shorten the lyrics or the style" in err.value.message
    assert str(jobs_module.MAX_PAYLOAD_BYTES) in err.value.message


@pytest.mark.asyncio
async def test_a_payload_the_store_refuses_during_the_run_fails_the_job_with_its_reason(tmp_path, monkeypatch):
    free_env(monkeypatch, record=tmp_path / "sent")
    home = tmp_path / "dpc"
    install_fake_engine(home)
    async with Rig(home) as rig:
        real = rig.store.write_payload

        def refuse_the_result(job_id, name, body, **kw):
            if name == LM_RESULT_PAYLOAD:
                raise MusicJobError("payload_too_large", f"payload {name!r} is too big: shorten the lyrics or the style")
            return real(job_id, name, body, **kw)
        monkeypatch.setattr(rig.store, "write_payload", refuse_the_result)
        job = rig.store.create_job("generate_from_text", from_text(variants=1))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.FAILED
        assert "shorten the lyrics" in job["state_reason"] and "internal error" not in job["state_reason"]


# -- item 4: the split cache is checked by content hash -----------------------------------------


def test_a_split_file_changed_in_place_with_the_same_size_is_split_again(tmp_path):
    home = tmp_path / "dpc"
    models = install_fake_yue2(home)
    entries = make_yue2(home).prepare_adapters()
    name = VOICES[0]
    music = models / "loras" / ".split" / f"{name}-music.safetensors"
    good = music.read_bytes()
    flipped = bytearray(good)
    flipped[-1] ^= 0xFF   # the same size, other content
    music.write_bytes(bytes(flipped))
    os.utime(music, ns=(music.stat().st_atime_ns, music.stat().st_mtime_ns + 5 * 10 ** 9))
    assert music.stat().st_size == len(good)
    again = make_yue2(home).prepare_adapters()
    assert music.read_bytes() == good, "a same-size file with other content is not trusted"
    assert again[name]["halves"]["music"]["sha256"] == entries[name]["halves"]["music"]["sha256"]


def test_an_intact_split_is_reused_without_writing_it_again(tmp_path):
    home = tmp_path / "dpc"
    models = install_fake_yue2(home)
    make_yue2(home).prepare_adapters()
    music = models / "loras" / ".split" / f"{VOICES[0]}-music.safetensors"
    before = music.stat().st_mtime_ns
    make_yue2(home).prepare_adapters()
    assert music.stat().st_mtime_ns == before


# -- items 5 and the cap: comments --------------------------------------------------------------


class _Settings:
    def __init__(self, home):
        self.dpc_home_dir = home

    def get(self, section, key, fallback=None):
        return "0.05" if (section, key) == ("music", "poll_interval_s") else fallback


class _Api:
    async def broadcast_event(self, event, payload):
        pass


class _CommentsService(commands.MusicCommandsMixin):
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


async def _two_tracks(tmp_path):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"audio-a")
    (music / "b.mp3").write_bytes(b"audio-b")
    service = _CommentsService(tmp_path / "dpc")
    await service.add_music_folder(path=str(music))
    await service.rescan_music_folder()
    await service._music_library_instance.wait_hashed()
    ids = [e["id"] for e in (await service.list_music_library())["entries"]]
    return service, ids


@pytest.mark.asyncio
async def test_a_track_holds_a_bounded_number_of_comments(tmp_path, no_tools, monkeypatch):
    assert comments_module.MAX_COMMENTS_PER_TRACK == 500
    monkeypatch.setattr(comments_module, "MAX_COMMENTS_PER_TRACK", 3)
    service, [tid, _] = await _two_tracks(tmp_path)
    try:
        for i in range(3):
            assert (await service.add_music_comment(track_id=tid, start_s=i, text=f"n{i}"))["status"] == "ok"
        refused = await service.add_music_comment(track_id=tid, start_s=1, text="one too many")
        assert refused["status"] == "error" and refused["code"] == "too_many_comments"
        assert "3" in refused["message"]
        assert len((await service.list_music_comments(track_id=tid))["comments"]) == 3
        # a delete makes room again; an update of an existing one is not an add
        first = (await service.list_music_comments(track_id=tid))["comments"][0]
        assert (await service.update_music_comment(track_id=tid, comment_id=first["id"], text="edited"))["status"] == "ok"
        assert (await service.delete_music_comment(track_id=tid, comment_id=first["id"]))["status"] == "ok"
        assert (await service.add_music_comment(track_id=tid, start_s=1, text="room again"))["status"] == "ok"
    finally:
        await service.stop_music_library()


@pytest.mark.asyncio
async def test_a_merge_waits_for_a_comment_write_in_progress_and_takes_it_along(tmp_path, no_tools):
    service, [keeper, twin] = await _two_tracks(tmp_path)
    try:
        await service.add_music_comment(track_id=twin, start_s=1, text="already there")
        library = service._music_library_instance
        comments = service._music_comments_instance
        held, release, done = threading.Event(), threading.Event(), threading.Event()

        def writer():
            with comments._file_lock:     # what an add holds while it reads and writes the file
                held.set()
                release.wait(5)
                items = comments._read(twin)
                items.append({**items[0], "id": "cmt_" + "0" * 16, "text": "written during the merge"})
                comments._write(twin, items)

        def merger():
            library._delete_entry(twin, replaced_by=keeper)
            done.set()

        t1 = threading.Thread(target=writer)
        t1.start()
        assert held.wait(5)
        t2 = threading.Thread(target=merger)
        t2.start()
        await asyncio.sleep(0.3)
        assert not done.is_set(), "merge must wait for the write that holds the lock"
        release.set()
        t1.join(5)
        t2.join(5)
        assert done.is_set()
        texts = [c["text"] for c in (await service.list_music_comments(track_id=keeper))["comments"]]
        assert sorted(texts) == ["already there", "written during the merge"]
        assert not (tmp_path / "dpc" / "music" / "comments" / f"{twin}.json").exists()
    finally:
        await service.stop_music_library()


@pytest.mark.asyncio
async def test_a_comment_added_for_an_entry_that_was_folded_away_is_refused_not_orphaned(tmp_path, no_tools):
    service, [keeper, twin] = await _two_tracks(tmp_path)
    try:
        comments = service._music_comments_instance
        library = service._music_library_instance
        library._delete_entry(twin, replaced_by=keeper)
        # the add passed its first check before the fold: its locked write checks again
        with pytest.raises(Exception) as err:
            with comments._file_lock:
                comments._duration(twin)
        assert getattr(err.value, "code", None) == "unknown_track"
        assert (await service.add_music_comment(track_id=twin, start_s=1, text="late"))["code"] == "unknown_track"
        assert not (tmp_path / "dpc" / "music" / "comments" / f"{twin}.json").exists()
    finally:
        await service.stop_music_library()


@pytest.mark.asyncio
async def test_a_merge_never_loses_a_comment_to_the_cap(tmp_path, no_tools, monkeypatch):
    service, [keeper, twin] = await _two_tracks(tmp_path)
    try:
        for i in range(2):
            await service.add_music_comment(track_id=keeper, start_s=i, text=f"k{i}")
            await service.add_music_comment(track_id=twin, start_s=i, text=f"t{i}")
        monkeypatch.setattr(comments_module, "MAX_COMMENTS_PER_TRACK", 3)
        service._music_library_instance._delete_entry(twin, replaced_by=keeper)
        # Follow-up 16 (Zcode #188, finding 9): every comment is attached to the survivor, past the cap;
        # the folded entry's file does not stay behind holding comments nobody can reach.
        got = (await service.list_music_comments(track_id=keeper))["comments"]
        assert sorted(c["text"] for c in got) == ["k0", "k1", "t0", "t1"]
        assert not service._music_comments_instance.comments_path(twin).exists()
    finally:
        await service.stop_music_library()


# -- item 6: a relative path in a hand-edited library.json --------------------------------------


async def _library_with_one_track(tmp_path):
    home = tmp_path / "dpc"
    music = tmp_path / "Music"
    music.mkdir()
    (music / "one.mp3").write_bytes(b"ID3 audio one")
    lib = MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
    await lib.open()
    folder = await lib.add_folder(str(music), scan=False)
    await lib.rescan_folder(folder["id"])
    await lib.wait_hashed()
    track_id = lib.list_entries()[0]["id"]
    await lib.stop()
    return home, track_id


def _edit_path(home, track_id, new_path, **fields):
    target = home / "music" / "library.json"
    records = json.loads(target.read_text(encoding="utf-8"))
    for r in records:
        if r["id"] == track_id:
            r["path"] = new_path
            r.update(fields)
    target.write_text(json.dumps(records), encoding="utf-8")


async def _reopen(home):
    lib = MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
    await lib.open()
    return lib


@pytest.mark.asyncio
async def test_a_relative_path_is_taken_from_the_library_folder_not_the_working_directory(tmp_path, monkeypatch):
    home, tid = await _library_with_one_track(tmp_path)
    inside = home / "music" / "files"
    inside.mkdir()
    (inside / "kept.mp3").write_bytes(b"audio")
    cwd = tmp_path / "elsewhere"
    (cwd / "files").mkdir(parents=True)
    (cwd / "files" / "kept.mp3").write_bytes(b"a different file from the working directory")
    monkeypatch.chdir(cwd)
    _edit_path(home, tid, "files/kept.mp3")
    lib = await _reopen(home)
    try:
        entry = lib.get_entry(tid)
        assert entry["path"] == str((inside / "kept.mp3").resolve()) or entry["path"] == str(inside / "kept.mp3")
        assert not entry.get("missing")
        assert lib.get_track_path(tid) is not None and lib.get_track_path(tid).is_absolute()
    finally:
        await lib.stop()
    saved = json.loads((home / "music" / "library.json").read_text(encoding="utf-8"))
    assert os.path.isabs(next(r for r in saved if r["id"] == tid)["path"]), "the anchored path is saved"


@pytest.mark.asyncio
async def test_a_relative_path_into_generated_or_out_of_the_library_folder_is_refused(tmp_path, monkeypatch):
    home, tid = await _library_with_one_track(tmp_path)
    gen = home / "music" / "generated" / "job_x"
    gen.mkdir(parents=True)
    (gen / "take.flac").write_bytes(b"flac")
    monkeypatch.chdir(tmp_path)
    for bad in ("generated/job_x/take.flac", "../../outside.mp3", "..\\..\\outside.mp3" if os.name == "nt" else "../x/../../y.mp3"):
        _edit_path(home, tid, bad, missing=False, origin="folder")
        lib = await _reopen(home)
        try:
            entry = lib.get_entry(tid)
            assert entry["missing"] is True, bad
            assert os.path.isabs(entry["path"]), bad
            assert lib.get_track_path(tid) is None, bad
            assert entry["deletable"] is False, bad
            with pytest.raises(Exception) as err:
                await lib.delete_generated_file(tid)
            assert getattr(err.value, "code", None) in ("file_missing", "not_owned", "invalid_path"), bad
            assert (gen / "take.flac").exists()
        finally:
            await lib.stop()


@pytest.mark.asyncio
async def test_a_generated_take_may_keep_a_relative_path_inside_generated(tmp_path):
    home, tid = await _library_with_one_track(tmp_path)
    gen = home / "music" / "generated" / "job_y"
    gen.mkdir(parents=True)
    (gen / "take.flac").write_bytes(b"flac")
    source = {"kind": "generated", "name": "take.flac", "uri": (gen / "take.flac").as_uri(), "size_bytes": 4,
              "mime": "audio/flac", "lineage": {"job_id": "job_y"}}
    _edit_path(home, tid, "generated/job_y/take.flac", origin="generated", missing=False, source=source,
               folder_id=None)
    lib = await _reopen(home)
    try:
        entry = lib.get_entry(tid)
        assert not entry.get("missing") and entry["path"].endswith("take.flac") and os.path.isabs(entry["path"])
    finally:
        await lib.stop()


# -- small items: the stop and the report -------------------------------------------------------


def _service(home, monkeypatch):
    without_tools(monkeypatch)
    service = Service(home)
    service.settings.get = lambda section, key, fallback=None: "0" if key == "acestep_port" else fallback
    return service


@pytest.mark.asyncio
async def test_stop_is_not_refused_for_a_job_that_only_waits_for_the_card(tmp_path, monkeypatch):
    home = tmp_path / "dpc"
    install_fake_engine(home)
    service = _service(home, monkeypatch)
    try:
        store = await service._music_jobs()
        job = store.create_job("generate_from_text", from_text(variants=1))
        job_id = job["id"]
        service._music_runner_instance = SimpleNamespace(current_job_id=job_id, current_engine=None, running=True)
        # queued: it waits in the card line and holds nothing
        stopped = await service.stop_music_engine(engine="acestep")
        assert stopped["status"] == "ok", stopped
        # running with the card: refused
        store.write_payload(job_id, "lm.request.json", b"{}")
        store.start_job(job_id)
        service._music_runner_instance.current_engine = "acestep"
        refused = await service.stop_music_engine(engine="acestep")
        assert refused["status"] == "error" and refused["code"] == "engine_busy" and job_id in refused["message"]
        # running but asking for the card again (its engine child ended): holds nothing
        store.set_waiting_for_card(job_id, [{"alias": "llm", "engine": "llama"}])
        assert (await service.stop_music_engine(engine="acestep"))["status"] == "ok"
        # force still stops a job that holds the card
        store.set_waiting_for_card(job_id, [])
        assert (await service.stop_music_engine(engine="acestep"))["code"] == "engine_busy"
        assert (await service.stop_music_engine(engine="acestep", force=True))["status"] == "ok"
    finally:
        service._music_runner_instance = None
        await service.stop_music_library()


@pytest.mark.asyncio
async def test_the_capabilities_report_is_built_off_the_event_loop(tmp_path, monkeypatch):
    home = tmp_path / "dpc"
    install_fake_engine(home)
    service = _service(home, monkeypatch)
    loop_thread = threading.get_ident()
    seen = []
    try:
        store = await service._music_jobs()
        real = store.capabilities.report

        def spy():
            seen.append(threading.get_ident())
            return real()
        monkeypatch.setattr(store.capabilities, "report", spy)
        answer = await service.get_music_capabilities()
        assert answer["status"] == "ok" and answer["capabilities"]["schema"]
        assert seen and seen[0] != loop_thread
    finally:
        await service.stop_music_library()


@pytest.mark.parametrize("name, said, not_said", [
    ("lm.request.json", "shorten the lyrics or the style", "answer"),
    ("synth.v2.request.json", "shorten the lyrics or the style", "answer"),
    ("edit.request.json", "note of the edit or its region", "lyrics"),
    ("lm.result.json", "the engine's answer", "lyrics"),
    ("synth.v1.result.json", "the engine's answer", "lyrics"),
    ("transcribe.result.json", "the engine's answer", "lyrics"),
])
def test_the_oversized_payload_message_is_worded_by_the_payload_kind(tmp_path, name, said, not_said):
    """Follow-up 16 (Zcode #188, finding 10): a binary result is not something to shorten the lyrics for."""
    from tests._music_jobs_helpers import queued_job, store_at
    store = store_at(tmp_path)
    job = queued_job(store)
    with pytest.raises(MusicJobError) as err:
        store.write_payload(job["id"], name, b"x" * (jobs_module.MAX_PAYLOAD_BYTES + 1))
    assert err.value.code == "payload_too_large" and said in err.value.message
    assert not_said not in err.value.message
