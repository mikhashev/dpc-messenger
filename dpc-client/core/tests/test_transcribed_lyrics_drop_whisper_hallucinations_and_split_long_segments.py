"""Music Transcribe cleans the Whisper result before it becomes lyrics (music-player item 2,
backlog card TRANSCRIBED-LYRICS-CARRY-WHISPER-HALLUCINATIONS-AND-MERGE-LONG-SEGMENTS-INTO-ONE-LINE).

Fixed segment fixtures; the pipeline, mlx_whisper, librosa and torch are fakes. No model, no GPU.
Covers the filter (`music/lyrics_filter.py`), the split, the stored record and raw sidecar, and
that only the music call site asks the provider for the detail (dictation does not).
"""

import json
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from dpc_client_core.music import lyrics_filter as lf
from dpc_client_core.music import schemas
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibraryError
from dpc_client_core.music.lyrics import MusicLyrics, transcript_lyrics
from dpc_client_core.providers import whisper_provider
from dpc_client_core.providers.whisper_provider import LocalWhisperProvider

TRACK_ID = "trk_0123456789abcdef"


def seg(start, end, text, **extra):
    return {"start": start, "end": end, "text": text, **extra}


def clean(segments, **extra):
    return lf.clean_transcript({"text": " ".join(s["text"] for s in segments), "segments": segments, **extra})


def texts(result):
    return [s["text"] for s in result["segments"]]


REAL_RU = seg(10.0, 13.0, "Первая строка песни")
REAL_EN = seg(14.0, 17.0, "Hello darkness my old friend")


# -- hallucination classes are dropped, real lines stay ---------------------------------

@pytest.mark.parametrize("text", [
    "Редактор субтитров А.Семкин Корректор А.Егорова",
    "Продолжение следует...",
    "Субтитры создавал DimaTorzok",
    "Субтитры сделал DimaTorzok",
    "Спасибо за просмотр!",
    "Subtitles by the Amara.org community",
    "Thanks for watching!",
    "Thank you for watching.",
    "Please subscribe to my channel",
])
def test_known_credit_and_boilerplate_phrases_are_dropped(text):
    result, report = clean([seg(0.0, 3.0, text), REAL_RU, REAL_EN])
    assert texts(result) == [REAL_RU["text"], REAL_EN["text"]]
    assert report["dropped"] == 1 and report["dropped_reasons"] == {"phrase": 1}


@pytest.mark.parametrize("text", ["♪", "♪ ♪ ♪", "[Music]", "(музыка)", "[Музыка]", "*applause*", "(Аплодисменты)", "..."])
def test_music_symbols_and_sound_tags_alone_are_dropped(text):
    result, report = clean([seg(0.0, 3.0, text), REAL_EN])
    assert texts(result) == [REAL_EN["text"]]
    assert report["dropped"] == 1


def test_note_symbols_around_sung_text_are_stripped_not_dropped():
    result, report = clean([seg(1.0, 4.0, "♪ Hello darkness ♪")])
    assert texts(result) == ["Hello darkness"] and report["dropped"] == 0


def test_a_segment_the_backend_marks_as_silence_is_dropped_only_on_both_figures():
    silence = seg(0.0, 3.0, "Something invented", no_speech_prob=0.9, avg_logprob=-1.6)
    unsure_singer = seg(4.0, 7.0, "A quiet verse", no_speech_prob=0.9, avg_logprob=-0.4)
    shaky_words = seg(8.0, 11.0, "A mumbled verse", no_speech_prob=0.1, avg_logprob=-1.6)
    result, report = clean([silence, unsure_singer, shaky_words])
    assert texts(result) == ["A quiet verse", "A mumbled verse"]
    assert report["dropped_reasons"] == {"silence": 1}


def test_a_looping_phrase_is_dropped():
    loop = seg(0.0, 20.0, "я я я " * 14)
    result, report = clean([loop, REAL_RU])
    assert texts(result) == [REAL_RU["text"]] and report["dropped_reasons"] == {"loop": 1}


def test_the_backends_compression_ratio_drops_a_long_degenerate_text():
    long_text = "Это длинная строка которая повторяется. " * 6
    result, report = clean([seg(0.0, 20.0, long_text, compression_ratio=3.1), REAL_EN])
    assert texts(result) == [REAL_EN["text"]] and report["dropped_reasons"] == {"loop": 1}


def test_a_segment_repeated_over_and_over_keeps_its_first_copy():
    echoes = [seg(10.0 + 3 * i, 12.0 + 3 * i, "Я тебя люблю") for i in range(6)]
    result, report = clean(echoes + [REAL_EN])
    assert texts(result) == ["Я тебя люблю", REAL_EN["text"]]
    assert report["dropped_reasons"] == {"repeat": 5}


def test_a_chorus_sung_a_few_times_in_a_row_is_kept():
    chorus = [seg(10.0 + 3 * i, 12.0 + 3 * i, "Hey hey hey") for i in range(3)]
    result, report = clean(chorus)
    assert texts(result) == ["Hey hey hey"] * 3 and report["dropped"] == 0


@pytest.mark.parametrize("line", [
    "Thank you", "Спасибо тебе за всё", "La la la la la la la la", "На ход ноги", "Subtitles are just words",
    "Thanks for staying with me tonight",
])
def test_real_lines_that_look_a_little_like_boilerplate_stay(line):
    result, report = clean([seg(1.0, 3.0, line)])
    assert texts(result) == [line] and report["dropped"] == 0


def test_when_every_segment_would_be_dropped_the_raw_result_is_kept_and_marked():
    raw = [seg(0.0, 3.0, "Продолжение следует..."), seg(3.0, 6.0, "♪")]
    result, report = clean(raw)
    assert report["kept_raw"] is True and report["dropped"] == 0
    assert texts(result) == ["Продолжение следует..."]


def test_the_stored_lyrics_say_the_filter_ran_with_its_version():
    record, info = transcript_lyrics("h", {"text": "x", "provider": "p", "model": "m", "segments": [
        seg(0.0, 3.0, "Thanks for watching!"), REAL_EN]})
    assert [line["text"] for line in record["lines"]] == [REAL_EN["text"]]
    assert record["transcription"]["filter"] == {
        "version": lf.FILTER_VERSION, "dropped": 1, "kept_raw": False, "split_by_words": 0,
        "split_proportional": 0, "words": False}
    assert info["filter"]["dropped_reasons"] == {"phrase": 1}
    schemas.validate_lyrics(record)


def test_text_without_segments_loses_credit_lines_but_never_all_of_them():
    record, info = transcript_lyrics("h", {"text": "Subtitles by X\nreal line\nThanks for watching", "provider": "p"})
    assert [line["text"] for line in record["lines"]] == ["real line"] and info["filter"]["dropped"] == 2
    record, info = transcript_lyrics("h", {"text": "Thanks for watching", "provider": "p"})
    assert [line["text"] for line in record["lines"]] == ["Thanks for watching"]
    assert info["filter"]["kept_raw"] is True


# -- a long segment gets one line per sung line ------------------------------------------

def words_of(text, start, step=0.5, pauses=None):
    """Words at `step` seconds each; `pauses` maps a word index to the silence after it."""
    out, t = [], start
    for i, word in enumerate(text.split()):
        out.append({"word": word, "start": round(t, 3), "end": round(t + step, 3)})
        t += step + (pauses or {}).get(i, 0.05)
    return out


LONG_TEXT = "Первая строка песни, вторая строка пошла, третья строка уже здесь и четвёртая тоже"


def test_a_long_segment_is_split_at_pauses_and_punctuation_with_the_words_times():
    words = words_of(LONG_TEXT, 12.0, pauses={2: 0.9, 5: 0.9, 9: 0.9})
    long_segment = seg(12.0, words[-1]["end"], LONG_TEXT, words=words)
    result, report = clean([long_segment])
    assert len(result["segments"]) >= 3
    assert report["split_by_words"] == 1 and report["split_proportional"] == 0 and report["words"] is True
    assert " ".join(texts(result)) == LONG_TEXT
    assert texts(result)[0].endswith("песни,") and texts(result)[1].endswith("пошла,")
    by_start = {w["start"]: w for w in words}
    assert result["segments"][1]["start"] in by_start       # a piece starts on a word's own time
    for piece in result["segments"]:
        assert piece["end"] - piece["start"] <= lf.MAX_LINE_S and len(piece["text"]) <= lf.MAX_LINE_CHARS
    starts = [p["start"] for p in result["segments"]]
    assert starts == sorted(set(starts))


def test_words_that_arrive_without_segments_become_lines_at_pauses():
    words = words_of("One two three four. Five six seven eight", 5.0, pauses={7: 0.0})
    result, report = lf.clean_transcript({"text": "x", "segments": [], "words": words})
    assert texts(result) == ["One two three four.", "Five six seven eight"]
    assert result["segments"][1]["start"] == words[4]["start"]
    assert "words" not in result and report["words"] is True


def test_a_long_segment_without_word_times_is_split_in_proportion_to_its_characters():
    long_segment = seg(30.0, 60.0, LONG_TEXT)
    result, report = clean([long_segment])
    pieces = result["segments"]
    assert report["split_proportional"] == 1 and report["split_by_words"] == 0 and len(pieces) >= 4
    assert " ".join(texts(result)) == LONG_TEXT
    assert pieces[0]["start"] == 30.0 and pieces[-1]["end"] == 60.0
    assert all(a["end"] == pytest.approx(b["start"], abs=1e-2) for a, b in zip(pieces, pieces[1:]))
    for p in pieces:
        assert p["end"] - p["start"] <= lf.MAX_LINE_S + 1.0


def test_words_that_do_not_match_the_text_fall_back_to_the_proportional_split():
    words = words_of("completely different", 30.0)
    result, report = clean([seg(30.0, 60.0, LONG_TEXT, words=words)])
    assert report["split_proportional"] == 1 and report["split_by_words"] == 0


def test_a_short_segment_is_left_alone_and_lines_never_share_a_stamp():
    result, report = clean([seg(5.0, 8.0, "short one"), seg(5.0, 5.0, "same stamp"), seg(5.0, 9.0, "again")])
    starts = [s["start"] for s in result["segments"]]
    assert starts == sorted(set(starts)) and len(starts) == 3
    assert report["split_by_words"] == report["split_proportional"] == 0


def test_a_cleaned_result_becomes_lines_through_the_same_path_as_before():
    record, info = transcript_lyrics("h", {"text": "x", "provider": "p", "segments": [seg(30.0, 60.0, LONG_TEXT)]})
    assert info["synced"] is True and info["segments"] == len(record["lines"]) >= 4
    assert all(line["t_start"] is not None and line["t_end"] > line["t_start"] for line in record["lines"])


# -- the stored record and the raw sidecar ------------------------------------------------

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def refuse(self, request):
        raise AssertionError(f"a test tried to reach the network: {request.url}")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


class FakeLibrary:
    def __init__(self, path):
        self.path = path
        self.entry = {"id": TRACK_ID, "title": "Song", "artist": "Band", "album": None, "duration_s": 215.0,
                      "source_hash": "sha256:aa", "source": {"kind": "local", "name": path.name, "uri": "file:///x"}}

    def get_entry(self, track_id):
        return dict(self.entry)

    def get_track_path(self, track_id):
        return self.path


def no_probe(cmd, *, timeout):
    out = {"format": {"duration": "215.0", "tags": {}}, "streams": [{"codec_type": "audio"}]}
    return subprocess.CompletedProcess(cmd, 0, json.dumps(out).encode(), b"")


def make(tmp_path, result):
    audio = tmp_path / "Band - Song.mp3"
    audio.write_bytes(b"ID3" + b"\x00" * 64)

    async def transcriber(path):
        return result

    return MusicLyrics(tmp_path / "dpc", FakeLibrary(audio), tools=MediaTools(ffmpeg=None, ffprobe="ffprobe", runner=no_probe),
                       client=object(), lrclib_enabled=lambda: False, transcriber=transcriber)


@pytest.mark.asyncio
async def test_transcribe_stores_the_cleaned_lines_and_keeps_the_raw_result_beside_them(tmp_path):
    raw = {"text": "Продолжение следует... Hello", "language": "en", "duration": 215.0, "provider": "local_whisper",
           "model": "m", "segments": [seg(0.0, 3.0, "Продолжение следует..."), seg(5.0, 8.0, "Hello")]}
    svc = make(tmp_path, json.loads(json.dumps(raw)))
    answer = await svc.transcribe(TRACK_ID)
    assert [line["text"] for line in answer["lyrics"]["lines"]] == ["Hello"]
    assert answer["lyrics"]["transcription"]["filter"]["dropped"] == 1
    sidecar = tmp_path / "dpc" / "music" / "lyrics" / f"{TRACK_ID}.transcribe-raw.json"
    kept = json.loads(sidecar.read_text(encoding="utf-8"))
    assert kept["result"] == raw and kept["filter_version"] == lf.FILTER_VERSION and kept["track_hash"] == "sha256:aa"


@pytest.mark.asyncio
async def test_a_raw_sidecar_that_cannot_be_written_does_not_fail_the_run(tmp_path, monkeypatch):
    from dpc_client_core.music import lyrics as lyrics_module

    real = lyrics_module.atomic_write_json

    def refuse_raw(path, data):
        if str(path).endswith(".transcribe-raw.json"):
            raise OSError("disk full")
        return real(path, data)

    monkeypatch.setattr(lyrics_module, "atomic_write_json", refuse_raw)
    svc = make(tmp_path, {"text": "Hello", "provider": "p", "segments": [seg(5.0, 8.0, "Hello")]})
    answer = await svc.transcribe(TRACK_ID)
    assert answer["applied"] is True


def test_an_older_record_without_the_filter_still_validates_and_a_malformed_one_does_not():
    base = {"schema": schemas.LYRICS, "track_hash": "h", "source": "transcribe", "language": None,
            "lines": [{"t_start": 1.0, "text": "a"}]}
    schemas.validate_lyrics({**base, "transcription": {"provider": "p", "model": None}})
    good = {"version": 1, "dropped": 0, "kept_raw": False, "split_by_words": 0, "split_proportional": 0, "words": False}
    schemas.validate_lyrics({**base, "transcription": {"provider": "p", "model": None, "filter": good}})
    for bad in ({**good, "dropped": "1"}, {**good, "kept_raw": 0}, {k: v for k, v in good.items() if k != "words"},
                {**good, "extra": 1}):
        with pytest.raises(schemas.SchemaError):
            schemas.validate_lyrics({**base, "transcription": {"provider": "p", "model": None, "filter": bad}})


# -- the provider: detail only when the music path asks ----------------------------------

@pytest.fixture
def fakes(monkeypatch):
    librosa = types.ModuleType("librosa")
    librosa.load = lambda path, sr=16000: ([0.0] * (sr * 10), sr)
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None)
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "librosa", librosa)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(whisper_provider, "_idle_unload_source", None)


def hf_provider(pipe):
    p = LocalWhisperProvider("w", {"type": "local_whisper", "device": "cpu", "language": "en"})
    p.pipeline, p.model_loaded = pipe, True
    return p


WORD_CHUNKS = [{"text": " Hello", "timestamp": (1.0, 1.4)}, {"text": " world", "timestamp": (1.5, None)}]


@pytest.mark.asyncio
async def test_dictation_keeps_segment_timestamps_and_gets_no_words(fakes):
    seen = []

    def pipe(audio, batch_size=None, return_timestamps=None, **kw):
        seen.append(return_timestamps)
        return {"text": "t", "chunks": [{"text": " Hello", "timestamp": (0.0, 2.0)}]}

    answer = await hf_provider(pipe).transcribe("a.wav")
    assert seen == [True] and "words" not in answer and answer["segments"][0]["text"] == "Hello"


@pytest.mark.asyncio
async def test_with_detail_asks_the_pipeline_for_word_timestamps(fakes):
    seen = []

    def pipe(audio, batch_size=None, return_timestamps=None, **kw):
        seen.append(return_timestamps)
        return {"text": "Hello world", "chunks": WORD_CHUNKS}

    answer = await hf_provider(pipe).transcribe("a.wav", with_detail=True)
    assert seen == ["word"] and answer["segments"] == []
    assert answer["words"] == [{"word": "Hello", "start": 1.0, "end": 1.4}, {"word": "world", "start": 1.5, "end": 10.0}]
    assert "words_unavailable" not in answer


@pytest.mark.asyncio
async def test_a_model_that_refuses_word_timestamps_falls_back_to_segments_and_says_so(fakes):
    seen = []

    def pipe(audio, batch_size=None, return_timestamps=None, **kw):
        seen.append(return_timestamps)
        if return_timestamps == "word":
            raise ValueError("model has no alignment_heads")
        return {"text": "t", "chunks": [{"text": " Hello", "timestamp": (0.0, 2.0)}]}

    answer = await hf_provider(pipe).transcribe("a.wav", with_detail=True)
    assert seen == ["word", True] and answer["words_unavailable"] is True
    assert answer["segments"] == [{"start": 0.0, "end": 2.0, "text": "Hello"}] and "words" not in answer


@pytest.mark.asyncio
async def test_mlx_with_detail_uses_the_backends_hallucination_options_and_keeps_its_figures(fakes, monkeypatch):
    calls = []
    mlx = types.ModuleType("mlx_whisper")

    def transcribe(audio_path, path_or_hf_repo=None, language=None, task=None, **options):
        calls.append(options)
        return {"text": "t", "language": "en", "segments": [
            {"start": 0.0, "end": 2.0, "text": " Hi there", "no_speech_prob": 0.1, "avg_logprob": -0.3,
             "compression_ratio": 1.2, "words": [{"word": " Hi", "start": 0.0, "end": 0.5, "probability": 0.9},
                                                  {"word": " there", "start": 0.6, "end": 1.0}]}]}

    mlx.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", mlx)
    p = LocalWhisperProvider("w", {"type": "local_whisper", "device": "mlx", "language": "en"})
    p.pipeline, p.model_loaded = "mlx", True
    plain = await p.transcribe("a.wav")
    detailed = await p.transcribe("a.wav", with_detail=True)
    assert calls[0] == {}
    assert calls[1] == {"word_timestamps": True, "condition_on_previous_text": False,
                        "hallucination_silence_threshold": whisper_provider.MUSIC_HALLUCINATION_SILENCE_S}
    assert plain["segments"] == [{"start": 0.0, "end": 2.0, "text": "Hi there"}]
    first = detailed["segments"][0]
    assert first["no_speech_prob"] == 0.1 and first["avg_logprob"] == -0.3 and first["compression_ratio"] == 1.2
    assert first["words"] == [{"word": "Hi", "start": 0.0, "end": 0.5}, {"word": "there", "start": 0.6, "end": 1.0}]


class SpyWhisper:
    def __init__(self):
        self.alias, self.config = "w", {"type": "local_whisper"}
        self.model_name, self.language = "m", "auto"
        self.kwargs = []

    def is_model_loaded(self):
        return True

    async def transcribe(self, path, release_if_loaded_here=False, **kwargs):
        self.kwargs.append(kwargs)
        return {"text": "hello", "language": "en", "duration": 3.0, "provider": "local_whisper", "segments": []}


@pytest.mark.asyncio
async def test_only_the_music_door_asks_for_detail_and_dictation_does_not(tmp_path):
    from dpc_client_core.music.commands import MusicCommandsMixin
    from dpc_client_core.service import CoreService

    audio = tmp_path / "x.wav"
    audio.write_bytes(b"RIFF....WAVEfmt fake")
    music_spy, dictation_spy = SpyWhisper(), SpyWhisper()
    commands = MusicCommandsMixin.__new__(MusicCommandsMixin)
    commands.settings = SimpleNamespace(get=lambda section, key, fallback=None: fallback,
                                        get_local_transcription_max_file_size_mb=lambda: 25)
    await commands._music_transcribe_with_whisper(music_spy, Path(audio))
    settings = MagicMock()
    settings.get.return_value = "10"
    settings.get_local_transcription_queue_wait_s.return_value = 600
    svc = SimpleNamespace(settings=settings, llm_manager=SimpleNamespace(providers={"w": dictation_spy}))
    await CoreService.transcribe_audio(svc, file_path=str(audio), provider_alias="w")
    assert music_spy.kwargs[0].get("with_detail") is True
    assert "with_detail" not in dictation_spy.kwargs[0]
