"""Follow-up 11B (Zcode #27, the hallucination filter of 7346f13e): the filter must not damage a song.

Synthetic segment fixtures only; no model, no audio, no GPU. Nobody has run live Whisper on a song, so
nothing here claims how real singing is transcribed. Each test names the finding it pins; the P1 ones
failed on 8b2e1963 before the fix.
"""

import json
import random
import subprocess
import sys
import types
import zlib

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


REAL_EN = seg(14.0, 17.0, "Hello darkness my old friend")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def refuse(self, request):
        raise AssertionError(f"a test tried to reach the network: {request.url}")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


# -- [P1] the last line of a song: the last word gets a word's length, not the rest of the file ------

SONG_LINES = ("первая строка песни", "вторая строка пошла", "третья строка уже здесь", "и четвёртая строка пошла")


def song_chunks(open_end=True):
    """HF word chunks: four sung lines, a 1.2 s breath between lines; the last word's end is None, as the
    pipeline leaves it (the docstring of `_words_from_chunks`; no live run has shown it)."""
    chunks, t = [], 2.0
    for line in SONG_LINES:
        for word in line.split():
            chunks.append({"text": " " + word, "timestamp": (round(t, 3), round(t + 0.4, 3))})
            t += 0.5
        t += 1.2
    if open_end:
        chunks[-1]["timestamp"] = (chunks[-1]["timestamp"][0], None)
    return chunks


@pytest.fixture
def song_fakes(monkeypatch):
    """A 40 s file: the sung part ends near 17 s, the rest is an instrumental outro."""
    librosa = types.ModuleType("librosa")
    librosa.load = lambda path, sr=16000: ([0.0] * (sr * 40), sr)
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


@pytest.mark.asyncio
async def test_a_song_with_a_long_outro_keeps_its_last_line_whole(song_fakes):
    def pipe(audio, batch_size=None, return_timestamps=None, **kw):
        return {"text": "t", "chunks": song_chunks()}

    answer = await hf_provider(pipe).transcribe("song.wav", with_detail=True)
    assert answer["duration"] == 40.0
    record, info = transcript_lyrics("h", answer)
    assert [line["text"] for line in record["lines"]] == list(SONG_LINES)
    last = record["lines"][-1]
    assert last["t_end"] - last["t_start"] < lf.MAX_LINE_S
    assert info["filter"]["split_by_words"] == 0


@pytest.mark.asyncio
async def test_the_last_line_stays_whole_through_the_library_path_too(song_fakes, tmp_path):
    def pipe(audio, batch_size=None, return_timestamps=None, **kw):
        return {"text": "t", "chunks": song_chunks()}

    answer = await hf_provider(pipe).transcribe("song.wav", with_detail=True)
    svc = make_library(tmp_path, answer)
    stored = await svc.transcribe(TRACK_ID)
    assert [line["text"] for line in stored["lyrics"]["lines"]] == list(SONG_LINES)


def test_an_open_word_gets_the_median_length_of_the_words_that_have_an_end():
    chunks = [{"text": "a", "timestamp": (0.0, 0.3)}, {"text": "b", "timestamp": (1.0, 1.5)},
              {"text": "c", "timestamp": (2.0, 2.4)}, {"text": "d", "timestamp": (3.0, None)}]
    words = LocalWhisperProvider._words_from_chunks(chunks, 100.0)
    assert words[-1] == {"word": "d", "start": 3.0, "end": pytest.approx(3.4)}  # the median of 0.3, 0.5, 0.4


@pytest.mark.parametrize("known, expected", [
    ([], whisper_provider.OPEN_WORD_DEFAULT_S),
    ([0.05, 0.05], whisper_provider.OPEN_WORD_MIN_S),
    ([4.0, 5.0], whisper_provider.OPEN_WORD_MAX_S),
])
def test_an_open_word_length_is_held_to_its_bounds(known, expected):
    chunks = [{"text": f"w{i}", "timestamp": (float(i * 10), i * 10 + length)} for i, length in enumerate(known)]
    chunks.append({"text": "last", "timestamp": (500.0, None)})
    assert LocalWhisperProvider._words_from_chunks(chunks, 1000.0)[-1]["end"] == pytest.approx(500.0 + expected)


def test_an_open_word_never_runs_past_the_end_of_the_audio():
    chunks = [{"text": "a", "timestamp": (0.0, 1.0)}, {"text": "last", "timestamp": (9.8, None)}]
    assert LocalWhisperProvider._words_from_chunks(chunks, 10.0)[-1]["end"] == 10.0


@pytest.mark.asyncio
async def test_the_segment_fallback_does_not_stretch_the_last_line_over_the_outro_either(song_fakes):
    def pipe(audio, batch_size=None, return_timestamps=None, **kw):
        if return_timestamps == "word":
            raise ValueError("model has no alignment_heads")
        return {"text": "t", "chunks": [{"text": " first line of the song", "timestamp": (2.0, 6.0)},
                                        {"text": " and the very last line", "timestamp": (7.0, None)}]}

    answer = await hf_provider(pipe).transcribe("song.wav", with_detail=True)
    assert answer["words_unavailable"] is True
    assert answer["segments"][-1]["end"] == pytest.approx(7.0 + 5 * whisper_provider.OPEN_SEGMENT_PER_WORD_S)
    record, _ = transcript_lyrics("h", answer)
    assert [line["text"] for line in record["lines"]] == ["first line of the song", "and the very last line"]


@pytest.mark.asyncio
async def test_dictation_still_gives_an_open_last_segment_the_end_of_the_audio(song_fakes):
    def pipe(audio, batch_size=None, return_timestamps=None, **kw):
        return {"text": "t", "chunks": [{"text": " the tail", "timestamp": (12.0, None)}]}

    answer = await hf_provider(pipe).transcribe("voice.wav")
    assert answer["segments"] == [{"start": 12.0, "end": 40.0, "text": "the tail"}] and "words" not in answer


# -- [P1] a repeat is a hallucination only where the times say so ------------------------------------

def test_a_chorus_sung_four_times_in_a_row_keeps_every_line_and_its_stamp():
    chorus = [seg(20.0 + 4 * i, 23.0 + 4 * i, "Hey hey hey hey") for i in range(4)]
    result, report = clean(chorus + [REAL_EN])
    assert texts(result) == ["Hey hey hey hey"] * 4 + [REAL_EN["text"]]
    assert [s["start"] for s in result["segments"]][:4] == [20.0, 24.0, 28.0, 32.0]
    assert report["dropped"] == 0


def test_a_long_run_of_honest_repeats_is_kept_whole():
    chorus = [seg(20.0 + 3 * i, 22.5 + 3 * i, "Я тебя люблю") for i in range(8)]
    result, report = clean(chorus)
    assert len(result["segments"]) == 8 and report["dropped"] == 0


def test_repeats_stamped_with_one_time_are_dropped_after_the_first():
    echoes = [seg(30.0, 32.0, "Спасибо тебе за всё") for _ in range(5)]
    result, report = clean([REAL_EN] + echoes)
    assert texts(result) == [REAL_EN["text"], "Спасибо тебе за всё"]
    assert report["dropped_reasons"] == {"repeat": 4}
    assert [d["reason"] for d in report["dropped_lines"]] == ["repeat"] * 4


def test_repeats_squeezed_into_less_time_than_their_text_needs_are_dropped():
    text = "Я тебя люблю"
    assert len(text) / lf.MAX_SUNG_CHARS_PER_S == pytest.approx(0.4)
    echoes = [seg(30.0 + 0.5 * i, 30.0 + 0.5 * i + 0.3, text) for i in range(6)]
    result, report = clean(echoes)
    assert texts(result) == [text] and report["dropped_reasons"] == {"repeat": 5}


def test_repeats_that_begin_after_the_end_of_the_audio_are_dropped():
    echoes = [seg(94.0 + 3 * i, 96.0 + 3 * i, "La la la la") for i in range(6)]
    result, report = clean(echoes, duration=100.0)
    assert [s["start"] for s in result["segments"]] == [94.0, 97.0]
    assert report["dropped_reasons"] == {"repeat": 4}
    result, report = clean(echoes)
    assert len(result["segments"]) == 6 and report["dropped"] == 0  # the same run with no audio length: no evidence


def test_a_run_is_judged_member_by_member():
    honest = [seg(20.0 + 4 * i, 23.0 + 4 * i, "Hey hey hey hey") for i in range(4)]
    echoes = [seg(32.0, 33.0, "Hey hey hey hey"), seg(32.0, 33.0, "Hey hey hey hey")]
    result, report = clean(honest + echoes)
    assert len(result["segments"]) == 4 and report["dropped_reasons"] == {"repeat": 2}


def test_segments_without_times_give_no_time_evidence_and_stay():
    run = [{"text": "Hey hey hey hey"} for _ in range(5)]
    reasons = {}
    assert lf._collapse_runs(run, reasons) == run and reasons == {}


# -- [P1] zlib alone never drops a segment ----------------------------------------------------------

VERSE = "Над рекой туман плывёт, ночь зовёт меня домой"


def test_a_verse_sung_three_times_in_one_segment_is_kept():
    text = " ".join([VERSE] * 3)
    raw = text.encode()
    assert len(text) >= lf.COMPRESSION_MIN_CHARS and len(raw) / len(zlib.compress(raw)) > lf.COMPRESSION_RATIO_MAX
    result, report = clean([seg(10.0, 40.0, text), REAL_EN])  # a neighbour, or "everything dropped" would keep it
    assert texts(result)[-1] == REAL_EN["text"] and " ".join(texts(result)[:-1]) == text and report["dropped"] == 0


def test_the_backends_ratio_alone_does_not_drop_a_long_segment():
    text = "Ночью над рекой плывёт туман, а я стою один и жду рассвета, и ветер носит мои мысли далеко, туда где нет ни слёз ни боли"
    result, report = clean([seg(10.0, 18.0, text, compression_ratio=3.4), REAL_EN])
    assert report["dropped"] == 0 and " ".join(texts(result)[:-1]) == text


def test_the_ratio_with_a_phrase_looped_beyond_a_sung_repeat_drops_the_segment():
    phrase = "Это длинная строка которая повторяется. "
    result, report = clean([seg(0.0, 20.0, phrase * 6, compression_ratio=3.1), REAL_EN])
    assert texts(result) == [REAL_EN["text"]] and report["dropped_reasons"] == {"loop": 1}


def test_a_phrase_looped_fewer_times_than_the_confirmation_needs_is_kept_even_with_a_high_ratio():
    phrase = "Это длинная строка которая повторяется. "
    assert lf.CONFIRM_NGRAM_RUN > 3
    result, report = clean([seg(0.0, 30.0, phrase * 3, compression_ratio=3.1), REAL_EN])
    assert report["dropped"] == 0 and len(result["segments"]) >= 2


# -- [P2] a stock sign-off is dropped only when it is all the segment says --------------------------

@pytest.mark.parametrize("line", [
    "До новых встреч, моя любовь",
    "Продолжение следует в моих снах",
    "To be continued in my dreams",
    "и до свидания друзья мои",
])
def test_a_stock_phrase_inside_a_real_line_does_not_drop_the_line(line):
    result, report = clean([seg(5.0, 9.0, line), REAL_EN])
    assert texts(result) == [line, REAL_EN["text"]] and report["dropped"] == 0


@pytest.mark.parametrize("text", [
    "До новых встреч!", "Спасибо за просмотр. До новых встреч!", "Thanks for watching, like and subscribe",
    "Please subscribe to my channel", "Продолжение следует...",
])
def test_a_segment_that_is_only_stock_phrases_is_dropped(text):
    result, report = clean([seg(0.0, 3.0, text), REAL_EN])
    assert texts(result) == [REAL_EN["text"]] and report["dropped_reasons"] == {"phrase": 1}


def test_a_credit_with_names_is_still_dropped_wherever_the_phrase_stands():
    result, report = clean([seg(0.0, 3.0, "Субтитры сделал DimaTorzok"), seg(3.0, 6.0, "Editor Ivan Petrov, subtitles by Anna"),
                            REAL_EN])
    assert texts(result) == [REAL_EN["text"]] and report["dropped_reasons"] == {"phrase": 2}


# -- [P2] the proportional split holds the same limits as the word split -----------------------------

def test_the_proportional_split_holds_the_line_limits_on_a_realistic_spread_of_segments():
    rng = random.Random(5)
    for _ in range(300):
        words = ["".join(rng.choice("абвгдежз") for _ in range(rng.randint(2, 9))) for _ in range(rng.randint(14, 40))]
        text, duration = " ".join(words), rng.choice([8.0, 12.0, 20.0, 30.0])
        pieces = lf._split_proportionally(100.0, 100.0 + duration, text)
        assert " ".join(p["text"] for p in pieces) == text
        for p in pieces:
            assert p["end"] - p["start"] <= lf.MAX_LINE_S + 0.0005 and len(p["text"]) <= lf.MAX_LINE_CHARS, (duration, text)


def test_one_pinned_segment_the_old_cut_left_over_the_time_limit():
    text = "вдгаза ееааззв едз вагаг жед зедб вгвгаждаз зж ааеб гзб агазжаадж даагвжггб ббвбгаз зжжвадз бвдзб жеввбз бжгда"
    result, report = clean([seg(0.0, 30.0, text)])
    assert report["split_proportional"] == 1
    for piece in result["segments"]:
        assert piece["end"] - piece["start"] <= lf.MAX_LINE_S and len(piece["text"]) <= lf.MAX_LINE_CHARS


def test_a_few_words_held_for_a_long_time_cannot_be_cut_under_the_time_limit_and_are_not_lost():
    result, report = clean([seg(0.0, 30.0, "ooh ooh ooh")])
    assert " ".join(texts(result)) == "ooh ooh ooh"
    assert sum(p["end"] - p["start"] for p in result["segments"]) == pytest.approx(30.0, abs=0.01)


# -- [P2] the flag "no word timestamps were available" survives -------------------------------------

def test_the_words_unavailable_flag_is_kept_on_the_record_and_in_the_answer():
    result = {"text": "x", "provider": "p", "words_unavailable": True, "segments": [seg(5.0, 8.0, "Hello")]}
    record, info = transcript_lyrics("h", result)
    assert record["transcription"]["filter"]["words_unavailable"] is True
    assert info["filter"]["words_unavailable"] is True
    schemas.validate_lyrics(record)


def test_without_the_flag_the_record_has_no_such_key():
    record, info = transcript_lyrics("h", {"text": "x", "provider": "p", "segments": [seg(5.0, 8.0, "Hello")]})
    assert "words_unavailable" not in record["transcription"]["filter"] and "words_unavailable" not in info["filter"]


def test_the_stored_flag_is_validated():
    base = {"schema": schemas.LYRICS, "track_hash": "h", "source": "transcribe", "language": None,
            "lines": [{"t_start": 1.0, "text": "a"}]}
    good = {"version": 2, "dropped": 0, "kept_raw": False, "split_by_words": 0, "split_proportional": 0, "words": False}
    schemas.validate_lyrics({**base, "transcription": {"provider": "p", "model": None,
                                                        "filter": {**good, "words_unavailable": True}}})
    for bad in ({**good, "words_unavailable": False}, {**good, "words_unavailable": "yes"}, {**good, "other": True}):
        with pytest.raises(schemas.SchemaError):
            schemas.validate_lyrics({**base, "transcription": {"provider": "p", "model": None, "filter": bad}})


# -- [P2] the report lists what was filtered, for the UI's note --------------------------------------

def test_the_report_lists_the_dropped_lines_with_their_reason_and_caps_the_list():
    credits = [seg(float(i), i + 0.5, f"Subtitles by person {i}") for i in range(lf.MAX_REPORTED_DROPS + 5)]
    result, report = clean(credits + [seg(100.0, 103.0, "Real words here")])
    assert report["dropped"] == lf.MAX_REPORTED_DROPS + 5
    assert len(report["dropped_lines"]) == lf.MAX_REPORTED_DROPS
    assert report["dropped_lines"][0] == {"start": 0.0, "text": "Subtitles by person 0", "reason": "phrase"}
    json.dumps(report)


def test_a_dropped_line_is_listed_with_a_bounded_text():
    long_credit = "Subtitles by " + "x" * 500
    _, report = clean([seg(0.0, 3.0, long_credit), REAL_EN])
    assert len(report["dropped_lines"][0]["text"]) == lf.MAX_REPORTED_TEXT


def test_when_everything_would_go_nothing_is_listed_as_dropped():
    _, report = clean([seg(0.0, 3.0, "Продолжение следует...")])
    assert report["kept_raw"] is True and report["dropped"] == 0 and report["dropped_lines"] == []


# -- helpers for the library path --------------------------------------------------------------------

class FakeLibrary:
    def __init__(self, path):
        self.path = path
        self.entry = {"id": TRACK_ID, "title": "Song", "artist": "Band", "album": None, "duration_s": 40.0,
                      "source_hash": "sha256:aa", "source": {"kind": "local", "name": path.name, "uri": "file:///x"}}

    def get_entry(self, track_id):
        return dict(self.entry)

    def get_track_path(self, track_id):
        return self.path


def no_probe(cmd, *, timeout):
    out = {"format": {"duration": "40.0", "tags": {}}, "streams": [{"codec_type": "audio"}]}
    return subprocess.CompletedProcess(cmd, 0, json.dumps(out).encode(), b"")


def make_library(tmp_path, result):
    audio = tmp_path / "Band - Song.mp3"
    audio.write_bytes(b"ID3" + b"\x00" * 64)

    async def transcriber(path):
        return result

    return MusicLyrics(tmp_path / "dpc", FakeLibrary(audio), tools=MediaTools(ffmpeg=None, ffprobe="ffprobe", runner=no_probe),
                       client=object(), lrclib_enabled=lambda: False, transcriber=transcriber)


@pytest.mark.asyncio
async def test_a_fallback_run_stores_the_flag_on_the_lyrics_the_library_writes(tmp_path):
    svc = make_library(tmp_path, {"text": "Hello", "provider": "local_whisper", "words_unavailable": True,
                                  "segments": [seg(5.0, 8.0, "Hello")]})
    answer = await svc.transcribe(TRACK_ID)
    assert answer["lyrics"]["transcription"]["filter"]["words_unavailable"] is True
    assert answer["transcription"]["filter"]["words_unavailable"] is True


def test_a_text_that_is_no_speech_still_raises():
    with pytest.raises(MusicLibraryError):
        transcript_lyrics("h", {"text": "", "segments": []})
