"""`get_music_lyrics_transcript_raw`: read only, by track id; returns the kept
`<track_id>.transcribe-raw.json` and the filter report over it with the dropped lines, so
the lyrics panel can show them after the run's own answer is gone."""

import asyncio

import pytest

from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.music.commands import MUSIC_COMMANDS, MusicCommandsMixin
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibraryError
from dpc_client_core.music.lyrics import MusicLyrics

TRACK_ID = "trk_0123456789abcdef"
RESULT = {
    "text": "la la la [Music]",
    "language": "en", "provider": "whisper", "model": "base", "duration": 30.0,
    "segments": [
        {"start": 1.0, "end": 3.0, "text": "la la la", "words": [{"word": "la", "start": 1.0, "end": 1.5}]},
        {"start": 4.0, "end": 6.0, "text": "[Music]"},
    ],
}


class _Library:
    def __init__(self, source_hash="sha256:aa"):
        self.source_hash = source_hash

    def get_entry(self, track_id):
        if track_id != TRACK_ID:
            raise MusicLibraryError("unknown_track", "no")
        return {"id": TRACK_ID, "source_hash": self.source_hash}


def _svc(tmp_path, **kw):
    return MusicLyrics(tmp_path / "dpc", _Library(**kw), lrclib_enabled=lambda: False,
                       tools=MediaTools(ffmpeg=None, ffprobe=None))


@pytest.mark.asyncio
async def test_without_a_kept_transcript_it_says_not_present(tmp_path):
    assert await _svc(tmp_path).raw_transcript(TRACK_ID) == {"present": False}


@pytest.mark.asyncio
async def test_the_dropped_lines_come_back_from_the_kept_file_and_nothing_is_written(tmp_path):
    svc = _svc(tmp_path)
    await svc._keep_raw_transcript(TRACK_ID, "sha256:aa", RESULT, {"version": 1})
    path = svc.raw_transcript_path(TRACK_ID)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    files = sorted(p.name for p in path.parent.iterdir())

    answer = await svc.raw_transcript(TRACK_ID)
    assert answer["present"] is True and answer["stale"] is False
    assert answer["track_hash"] == "sha256:aa" and answer["filter_version_saved"] == 1
    assert [d["text"] for d in answer["dropped_lines"]] == ["[Music]"]
    assert answer["dropped_lines"][0]["reason"] == "sound_tag" and answer["dropped_lines"][0]["start"] == 4.0
    assert answer["filter"]["dropped"] == 1
    assert answer["raw"]["text"] == "la la la [Music]" and answer["raw"]["words"] == 1
    assert [s["text"] for s in answer["raw"]["segments"]] == ["la la la", "[Music]"]
    assert "words" not in answer["raw"]["segments"][0]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in path.parent.iterdir()) == files


@pytest.mark.asyncio
async def test_a_changed_file_marks_the_transcript_stale(tmp_path):
    svc = _svc(tmp_path, source_hash="sha256:bb")
    await svc._keep_raw_transcript(TRACK_ID, "sha256:aa", RESULT, {"version": 1})
    assert (await svc.raw_transcript(TRACK_ID))["stale"] is True


@pytest.mark.asyncio
async def test_a_damaged_file_is_not_present_and_a_bad_id_is_refused(tmp_path):
    svc = _svc(tmp_path)
    await svc._keep_raw_transcript(TRACK_ID, "sha256:aa", RESULT, {"version": 1})
    svc.raw_transcript_path(TRACK_ID).write_text("{not json", encoding="utf-8")
    assert await svc.raw_transcript(TRACK_ID) == {"present": False}
    with pytest.raises(MusicLibraryError):
        await svc.raw_transcript("../etc")


def test_the_command_is_a_registered_read_only_coroutine():
    name = "get_music_lyrics_transcript_raw"
    assert name in MUSIC_COMMANDS and name in ALLOWED_COMMANDS
    assert asyncio.iscoroutinefunction(getattr(MusicCommandsMixin, name))
