"""Same style on yue2.cpp (Mike 2026-11-26: "For Cover or Same style add the choice of engine. We have two and YuE2
is the default"; option 3, "both"): the describer (`music/describe.py`, Music Flamingo) listens to the source and
fills what the person did not type, and a section of the source's own codes (yue2.cpp `/tokenize`, `source_tokens`)
opens each take and is cut off before it is stored (`music/similar.py` `_same_style_on_yue2`, `cut_head`).

The door (`job_schemas.normalize_params_song`): yue2 by default, acestep the second; the section on whole frames,
10 to 60 s, the first 30 s by default; the section and the new part within yue2.cpp's 360 s; an empty caption leaves
the style to the describer, `keep_from_source` asks it for a Keep box with no value. The capability report says
whether the audio tokenizer is there (no tokenizer, no yue2 same style, with acestep offered) and whether a describer
is. The runs go to the fake yue2.cpp as a child process.
"""

import json
import math
import wave
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import numpy as np
import pytest

from dpc_client_core.music import describe
from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import region_edit as re_
from dpc_client_core.music import similar
from dpc_client_core.music.capabilities import EngineStatus, MusicCapabilities
from dpc_client_core.music.commands import MusicCommandsMixin
from dpc_client_core.music.jobs import MusicJobStore
from dpc_client_core.music.yue2 import MERT_NAME, TOKENIZER_FILES
from tests._acestep_helpers import install_fake_engine
from tests._yue2_helpers import DualRig, install_fake_yue2, make_yue2

RATE = 48000
DONE = (js.REVIEW, js.FAILED, js.CANCELLED, js.UNSUPPORTED)
TRACK = "trk_0123456789abcdef"
VERSION = "ver_0123456789abcdef"
LONG = {"kind": "local", "content_hash": "md5:" + "a" * 32, "duration_s": 237.4, "title": "Two Strings"}
ANSWER = {"genre": "kazakh folk", "tags": ["dombra", "ballad"], "instruments": ["dombra", "voice"], "vocals": "male",
          "bpm": 96, "key": "D minor", "time_signature": "3/4", "mood": "wistful",
          "style": "kazakh folk ballad, dombra, male vocals, wistful"}


def norm(params, source=LONG):
    return js.normalize_params_song("generate_similar", params, track_id=TRACK, source=source)


# -- the door -------------------------------------------------------------------------------------


def test_same_style_runs_on_yue2_by_default_and_acestep_stays_a_choice():
    assert js.SONG_ENGINES["generate_similar"] == ("yue2", "acestep")
    assert js.SONG_DEFAULT_ENGINE["generate_similar"] == "yue2"
    p = norm({"caption": "dombra ballad"})
    assert p["engine"] == "yue2" and p["reference"] == "audio" and p["reference_uri"] == f"dpc-library:{TRACK}"
    assert p["source_section"] == {"start_s": 0.0, "end_s": 30.0} and p["cot"] == "off" and p["keep_from_source"] == []
    assert p["duration_s"] == 238                     # the new part: the source's length, the section not counted
    assert js.check_params_song("generate_similar", p) is None            # a stored record normalizes to itself
    ace = norm({"caption": "dombra ballad", "engine": "acestep"})
    assert ace["engine"] == "acestep" and ace["reference"] == "description" and "source_section" not in ace


def test_an_empty_caption_leaves_the_style_to_the_describer_and_a_keep_box_without_a_value_asks_it():
    p = norm({"caption": "", "keep_from_source": ["vocals", "genre", "bpm"]})
    assert p["caption"] == "" and p["caption_basis"] == "describer"
    assert p["keep_from_source"] == ["genre", "bpm", "vocals"]          # in KEEP_KEYS order
    assert js.check_params_song("generate_similar", p) is None
    assert norm({})["caption_basis"] == "describer"                     # no caption at all is the same request
    with pytest.raises(js.ParamsError) as err:                          # acestep still needs a style
        norm({"caption": "", "engine": "acestep"})
    assert err.value.field == "caption"
    for bad in ({"keep_from_source": ["structure"]}, {"keep_from_source": "genre"},
                {"keep": {"bpm": 90}, "keep_from_source": ["bpm"]}):
        with pytest.raises(js.ParamsError) as err:
            norm({"caption": "x", **bad})
        assert err.value.field == "keep_from_source"


def test_the_section_is_on_whole_frames_within_the_source_and_ten_to_sixty_seconds_long():
    p = norm({"caption": "x", "source_section": {"start_s": 70.013, "end_s": 100.03}})
    assert p["source_section"] == {"start_s": 70.0, "end_s": 100.04}  # 25 frames a second, as the engine rounds
    for sec in ({"start_s": 0, "end_s": 9.9}, {"start_s": 0, "end_s": 61}, {"start_s": 230, "end_s": 250},
                {"start_s": -1, "end_s": 20}, {"start_s": 0}, [0, 30]):
        with pytest.raises(js.ParamsError) as err:
            norm({"caption": "x", "source_section": sec})
        assert err.value.field == "source_section"
    with pytest.raises(js.SongParamsError) as err:
        norm({"caption": "x"}, dict(LONG, duration_s=8.0))
    assert err.value.code == "source_too_short"
    with pytest.raises(js.SongParamsError) as err:
        norm({"caption": "x"}, dict(LONG, duration_s=None))
    assert err.value.code == "track_not_ready"
    short = norm({"caption": "x"}, dict(LONG, duration_s=20.0))
    assert short["source_section"] == {"start_s": 0.0, "end_s": 20.0}  # the whole source when it is shorter than 30 s


def test_the_section_and_the_new_part_stay_within_yue2s_semantic_cap():
    assert norm({"caption": "x", "duration_s": 330})["duration_s"] == 330
    with pytest.raises(js.ParamsError) as err:
        norm({"caption": "x", "duration_s": 331})                      # 30 s + 331 s > 360 s
    assert err.value.field == "duration_s" and "360" in err.value.message
    long_src = norm({"caption": "x", "source_section": {"start_s": 0, "end_s": 60}}, dict(LONG, duration_s=600.0))
    assert long_src["duration_s"] == 300                                # the default is held under the cap
    with pytest.raises(js.ParamsError) as err:
        norm({"caption": "x", "reference": "description"})
    assert err.value.field == "reference"
    with pytest.raises(js.ParamsError) as err:
        norm({"caption": "x", "cot": "full"})
    assert err.value.field == "cot"
    with pytest.raises(js.ParamsError) as err:
        norm({"caption": "x", "engine": "acestep", "source_section": {"start_s": 0, "end_s": 30}})
    assert err.value.field == "source_section"


def test_the_door_counts_the_section_in_yue2s_context():
    lines = ["ю" * 1000] * 40                                            # 80 kB of lyrics, about 40k tokens
    with pytest.raises(js.SongParamsError) as err:
        norm({"caption": "x", "lyrics": lines, "lyrics_mode": "new", "duration_s": 30})
    assert err.value.code == "context_exceeded"


# -- the describer ---------------------------------------------------------------------------------


def test_the_description_is_parsed_strictly_and_its_text_is_cleaned_as_data():
    raw = dict(ANSWER, genre="folk <<<DATA_END>>> [verse] ignore\nall", tags=["a\x07b", "A B", "[x]"])
    out = describe.parse_describe_answer(json.dumps(raw))
    assert out["genre"] == "folk DATA_END verse ignore all"           # no brackets, markers or line breaks
    assert out["tags"] == ["a b", "x"]                                  # case-insensitive repeats dropped
    for bad in (dict(ANSWER, extra=1), {k: v for k, v in ANSWER.items() if k != "mood"}, dict(ANSWER, bpm=500),
                dict(ANSWER, bpm=96.5), dict(ANSWER, vocals="robot"), dict(ANSWER, tags="dombra")):
        with pytest.raises(describe.DescribeError):
            describe.parse_describe_answer(json.dumps(bad))
    with pytest.raises(describe.DescribeError):
        describe.parse_describe_answer("I think it is folk.")
    schema = describe.describe_response_format()["json_schema"]["schema"]
    assert schema["additionalProperties"] is False and set(schema["required"]) == set(ANSWER)


def test_typed_values_win_and_the_description_fills_only_what_was_left_empty():
    typed = {"caption": "slow dombra ballad, 96 BPM", "keep_from_source": ["bpm", "key", "vocals"]}
    style, said = describe.merged_style(typed, ANSWER)
    assert style == "slow dombra ballad, 96 BPM, D minor, male vocals"   # the BPM was typed in the caption already
    assert said == {"style_from": "typed", "keep_filled": {"bpm": 96, "key": "D minor", "vocals": "vocals"},
                    "keep_without_value": []}
    style, said = describe.merged_style({"caption": "", "keep_from_source": ["genre", "timesig", "mood"]}, ANSWER)
    # the description's own line already holds its genre and mood: they are filled, not said twice
    assert style == "kazakh folk ballad, dombra, male vocals, wistful, 3/4 time" and said["style_from"] == "describer"
    assert said["keep_filled"] == {"genre": "kazakh folk", "timesig": "3/4", "mood": "wistful"}
    style, said = describe.merged_style({"caption": "", "keep_from_source": ["bpm"]}, None)
    assert style == "" and said["keep_without_value"] == ["bpm"]
    assert not describe.needs_describer({"caption": "x", "keep_from_source": []})
    assert describe.needs_describer({"caption": "x", "keep_from_source": ["mood"]})


def test_merged_style_keeps_typed_params_keep_and_typed_beats_describer():
    # Describe-then-edit UI path: caption comes from Flamingo (no BPM/key/timesig), keep carries all checked values,
    # keep_from_source is empty and answer is None (describer is skipped).
    style, said = describe.merged_style(
        {"caption": ANSWER["style"],
         "keep": {"genre": "kazakh folk", "bpm": 128, "key": "D minor", "timesig": "3/4", "mood": "wistful",
                  "structure": "verse-chorus", "vocals": "vocals"},
         "keep_from_source": []},
        None,
    )
    assert style == "kazakh folk ballad, dombra, male vocals, wistful, 128 BPM, D minor, 3/4 time, verse-chorus"
    assert said == {"style_from": "typed", "keep_filled": {}, "keep_without_value": []}

    # Corrected Keep values replace conflicting tags in a filled caption instead of duplicating or losing them.
    style, _ = describe.merged_style(
        {"caption": "kazakh folk ballad, 96 BPM, D minor, 3/4 time, male vocals",
         "keep": {"bpm": 128, "key": "E minor", "timesig": "4/4", "vocals": "instrumental"}},
        None,
    )
    assert style == "kazakh folk ballad, 128 BPM, E minor, 4/4 time, instrumental"

    # Typed values already matching the caption are not duplicated, and partial numbers ("6 BPM", "4 time") or prose
    # keys ("A major rock anthem") are not misread or corrupted.
    style, _ = describe.merged_style(
        {"caption": "A major rock anthem, 96 BPM, 3/4 time",
         "keep": {"bpm": 6, "key": "E minor", "timesig": "4"}},
        None,
    )
    assert style == "A major rock anthem, 6 BPM, 4 time, E minor"

    style, _ = describe.merged_style(
        {"caption": "indie rock, 96 bpm, d minor, 3/4 time, with vocals",
         "keep": {"bpm": 96, "key": "D minor", "timesig": "3/4", "vocals": "vocals"}},
        None,
    )
    assert style == "indie rock, 96 bpm, d minor, 3/4 time, with vocals"

    # Typed params.keep wins over Flamingo's keep_from_source when both name a field, and over Flamingo's style line.
    style, said = describe.merged_style(
        {"caption": "folk", "keep": {"bpm": 128}, "keep_from_source": ["bpm", "key"]},
        ANSWER,
    )
    assert style == "folk, 128 BPM, D minor"
    assert said == {"style_from": "typed", "keep_filled": {"key": "D minor"}, "keep_without_value": []}

    style, said = describe.merged_style(
        {"caption": "", "keep": {"bpm": 128, "genre": "synthwave"}, "keep_from_source": ["key"]},
        dict(ANSWER, style="kazakh folk ballad, 96 BPM, male vocals"),
    )
    assert style == "kazakh folk ballad, 128 BPM, male vocals, synthwave, D minor"
    assert said == {"style_from": "describer", "keep_filled": {"key": "D minor"}, "keep_without_value": []}

    # Lyrics theme stays attached to the caption before kept values; vocals='vocals' adds 'with vocals' when absent.
    style, _ = describe.merged_style(
        {"caption": "synthwave", "lyrics_theme": "night roads", "keep": {"bpm": 128, "vocals": "vocals"}},
        None,
    )
    assert style == "synthwave, lyrics about night roads, 128 BPM, with vocals"


def test_a_four_minute_source_is_heard_whole_and_a_long_one_in_three_windows():
    assert describe.plan_windows(237.4, 16384) == [(0.0, 237.4)]
    w = describe.plan_windows(900.0, 16384)
    assert w == [(0.0, 120.0), (390.0, 510.0), (780.0, 900.0)]
    assert describe.merge_answers([ANSWER, dict(ANSWER, bpm=100, tags=["epic"], vocals="unknown"),
                                   dict(ANSWER, bpm=120, key="F major")])["bpm"] == 100


class _Describer:
    def __init__(self, answer=ANSWER, hears=True):
        self.answer, self.hears, self.calls = answer, hears, []
        self.config = {"type": "llamacpp_server", "gguf_path": "D:/m/music-flamingo-hf.Q8_0.gguf"}
        self.model = None

    def supports_audio(self):
        return self.hears

    def server_window(self):
        return 16384

    def get_last_usage(self):
        return {"prompt_tokens": 7100, "completion_tokens": 90, "finish_reason": "stop"}

    async def generate_with_audio(self, prompt, audio, **kwargs):
        self.calls.append((prompt, Path(audio).name, kwargs))
        return json.dumps(self.answer) if isinstance(self.answer, dict) else self.answer


def _copy_decoder(seen):
    def decode(src, dest, start, length):
        seen.append((start, length))
        Path(dest).write_bytes(Path(src).read_bytes())
    return decode


@pytest.mark.asyncio
async def test_the_describer_is_asked_under_the_schema_with_no_text_of_the_person_and_its_answer_is_kept(tmp_path):
    src = tmp_path / "song.wav"
    src.write_bytes(b"RIFF")
    model, seen = _Describer(), []
    doc = await describe.describe_audio(model, "music_flamingo", src, source_s=237.4, content_hash="md5:" + "b" * 32,
                                        work_dir=tmp_path / "w", decode=_copy_decoder(seen))
    prompt, _name, kw = model.calls[0]
    assert kw["response_format"]["json_schema"]["name"] == describe.DESCRIBE_SCHEMA_NAME
    assert kw["temperature"] == 0.0 and kw["max_tokens"] == describe.DESCRIBE_MAX_TOKENS
    assert "whole song" in prompt and seen == [(None, None)]
    assert doc["status"] == "done" and doc["whole"] is True and doc["answer"]["bpm"] == 96
    assert doc["model"] == "music-flamingo-hf.Q8_0" and "does not rank" in doc["caveat"]
    assert doc["usage"] == [{"prompt_tokens": 7100, "completion_tokens": 90, "finish_reason": "stop"}]
    assert not list((tmp_path / "w").iterdir())                       # the decoded window is removed
    describe.write_cached(tmp_path, doc)
    assert describe.read_cached(tmp_path, "md5:" + "b" * 32)["answer"] == doc["answer"]
    assert describe.read_cached(tmp_path, "md5:" + "c" * 32) is None
    deaf = _Describer(hears=False)
    with pytest.raises(describe.DescribeError) as err:
        await describe.describe_audio(deaf, "x", src, source_s=10, content_hash=None, work_dir=tmp_path / "w",
                                      decode=_copy_decoder([]))
    assert err.value.code == "no_audio"
    rambling = _Describer(answer="it sounds like folk")
    with pytest.raises(describe.DescribeError) as err:
        await describe.describe_audio(rambling, "x", src, source_s=10, content_hash=None, work_dir=tmp_path / "w",
                                      decode=_copy_decoder([]))
    assert err.value.code == "failed"


# -- the cut ----------------------------------------------------------------------------------------


def _two_tones(path: Path, first_s: float, second_s: float, rate=RATE):
    n1, n2 = int(round(first_s * rate)), int(round(second_s * rate))
    t = np.arange(n1 + n2) / rate
    x = np.where(np.arange(n1 + n2) < n1, 0.3 * np.sin(2 * np.pi * 300 * t), 0.3 * np.sin(2 * np.pi * 900 * t))
    re_.write_wav_s24(path, [np.stack([x, x], axis=1).astype(np.float32)], rate=rate, channels=2)
    return path


def test_the_opening_section_is_cut_off_sample_exactly_with_a_short_fade_in(tmp_path):
    src = _two_tones(tmp_path / "take.wav", 10.0, 1.5)
    whole = re_.read_pcm(src)
    out = tmp_path / "cut.wav"
    info = similar.cut_head(src, 0, src.stat().st_size, out, 10.0, 10.0)
    assert info == {"frames_cut": 480000, "frames_kept": 72000, "rate": RATE, "fade_frames": 480}
    cut = re_.read_pcm(out)
    assert cut.frames == 72000 and cut.channels == 2
    assert np.array_equal(cut.data[480:], whole.data[480000 + 480:])   # untouched after the fade, bit for bit
    assert abs(cut.data[0, 0]) == 0.0 and abs(cut.data[479, 0]) <= abs(whole.data[480479, 0])
    with pytest.raises(ValueError, match="less than a second"):
        similar.cut_head(_two_tones(tmp_path / "short.wav", 10.0, 0.5), 0, (tmp_path / "short.wav").stat().st_size,
                         tmp_path / "x.wav", 10.0)
    assert not (tmp_path / "x.wav").exists()


# -- the capability report and the server's flags ---------------------------------------------------


def _install_tokenizer(models: Path, mert=True):
    rel = Path(TOKENIZER_FILES[1])
    (models / rel).parent.mkdir(parents=True, exist_ok=True)
    (models / rel).write_bytes(b"gguf tokenizer")
    if mert:
        (models / rel.parent / MERT_NAME.format(quant="Q8_0")).write_bytes(b"gguf mert")


def test_yue2_same_style_needs_the_audio_tokenizer_and_the_report_says_whether_a_describer_is_set_up(tmp_path):
    models = install_fake_yue2(tmp_path, voices=())
    yue2 = make_yue2(tmp_path)
    assert yue2.tokenizer_problem() and "--tokenizer" not in yue2.build_command(0)
    caps = MusicCapabilities(engines={"yue2": yue2, "acestep": lambda: EngineStatus(state="stopped", reason="ok")},
                             ffmpeg_probe=lambda: True)
    same = caps.report()["features"]["same_style"]
    assert same["default_engine"] == "yue2" and same["same_style_engines"]["yue2"]["status"] == "unsupported"
    assert "audio tokenizer is missing" in same["same_style_engines"]["yue2"]["reason"]
    assert "acestep.cpp runs without it" in same["same_style_engines"]["yue2"]["reason"]
    assert same["fallback_engine"] == "acestep" and same["status"] == "supported"
    assert caps.unsupported_reason("generate_similar", {"engine": "yue2"}).startswith("yue2.cpp same style:")
    _install_tokenizer(models, mert=False)
    assert yue2.tokenizer_problem()                                     # MERT must be beside it
    _install_tokenizer(models)
    assert yue2.tokenizer_problem() is None and "--tokenizer" in yue2.build_command(0)
    report = caps.report()
    st = report["features"]["same_style"]["same_style_engines"]["yue2"]
    assert st["status"] == "supported" and "no describer" in st["reason"] and st["verified"] is False
    assert report["features"]["describe"]["status"] == "unsupported"
    heard = MusicCapabilities(engines={"yue2": yue2}, ffmpeg_probe=lambda: True,
                              describer_probe=lambda: {"status": "supported", "alias": "music_flamingo", "reason": "ok"})
    assert "describer music_flamingo" in heard.song_engine_status("generate_similar", "yue2", needs_audio=False)["reason"]
    assert heard.report()["features"]["describe"]["alias"] == "music_flamingo"
    deaf = MusicCapabilities(engines={"yue2": yue2}, ffmpeg_probe=lambda: False)
    assert "ffmpeg" in deaf.unsupported_reason("generate_similar", {"engine": "yue2"})


def test_the_agents_card_says_the_opening_codes_and_the_describer_go_in():
    from dpc_client_core.dpc_agent.tools import music as tool
    p = norm({"caption": "", "keep_from_source": ["bpm"]})
    about = tool._song_about("generate_similar", p)
    assert "audio codes from 0 to 30 s as the opening (cut off each take)" in about
    assert "a description of it by the audio describer" in about and "yue2.cpp" in about
    typed = norm({"caption": "folk"})
    assert "describer" not in tool._song_about("generate_similar", typed)


# -- the runs -----------------------------------------------------------------------------------------


def _write_source(path: Path, seconds=12.0) -> Path:
    t = np.arange(int(seconds * RATE)) / RATE
    x = 0.3 * np.sin(2 * np.pi * 220 * t)
    pcm = np.clip(np.round(np.stack([x, x], axis=1) * 32767), -32768, 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return path


def _fake_decoder(src: Path, dest: Path) -> None:
    pcm = re_.read_pcm(src)
    re_.write_wav_s24(dest, [pcm.data], rate=pcm.rate, channels=pcm.channels)


def _sidecar(take) -> dict:
    flac = Path(url2pathname(urlparse(take["file"]["uri"]).path))
    return json.loads(flac.with_suffix(".json").read_text(encoding="utf-8"))


async def _rig(tmp_path, monkeypatch, describer=None):
    record = tmp_path / "posts"
    monkeypatch.setenv("FAKE_YUE2_MODE", "")
    monkeypatch.setenv("FAKE_YUE2_RECORD", str(record))
    monkeypatch.setenv("FAKE_ACESTEP_MODE", "")
    models = install_fake_yue2(tmp_path, voices=())
    install_fake_engine(tmp_path)
    _install_tokenizer(models)
    rig = DualRig(tmp_path)
    rig.store.capabilities._ffmpeg_probe = lambda: True
    rig.runner._decode_control = _fake_decoder
    rig.runner._describer = describer
    return rig, record


def _propose(rig, entry, params, seconds=12.0):
    source = {"kind": "local", "content_hash": entry["content_hash"], "duration_s": seconds, "title": "Two Strings"}
    job = rig.store.create_job("generate_similar", params, track_id=entry["id"], base_version_id=VERSION, source=source)
    rig.runner.wake()
    return job


@pytest.mark.asyncio
async def test_a_yue2_same_style_song_opens_on_the_sources_codes_in_the_described_style_and_the_section_is_cut_off(
        tmp_path, monkeypatch):
    asked = []

    async def describer(path, content_hash, source_s, work_dir):
        asked.append((Path(path).name, content_hash, round(source_s, 3)))
        return {"schema": describe.DESCRIBE_SCHEMA, "status": "done", "source_content_hash": content_hash,
                "alias": "music_flamingo", "model": "music-flamingo-hf.Q8_0", "whole": True,
                "windows": [{"start_s": 0.0, "end_s": source_s}], "answer": dict(ANSWER), "caveat": describe.CAVEAT}

    rig, record = await _rig(tmp_path, monkeypatch, describer)
    async with rig:
        path = _write_source(tmp_path / "music" / "Two Strings.wav")
        entry = await rig.library.register_generated(str(path), job_id="job_" + "1" * 16, lineage={"variant": 1})
        job = _propose(rig, entry, {"caption": "", "keep_from_source": ["bpm", "key"], "variants": 1, "seed": 5,
                                    "duration_s": 30, "source_section": {"start_s": 2, "end_s": 12}})
        assert job["state"] == js.QUEUED and job["params"]["engine"] == "yue2"
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")
        assert asked == [("Two Strings.wav", entry["content_hash"], 12.0)]        # the original file, its hash
        names = [p["name"] for p in done["payloads"]]
        assert names[:4] == ["song.request.json", "describe.result.json", "tokenize.result.json",
                             "synth.v1.request.json"]
        body = json.loads(rig.store.read_payload(job["id"], "synth.v1.request.json"))
        codes = json.loads(rig.store.read_payload(job["id"], "tokenize.result.json"))["codes"]
        assert body["source_tokens"] == codes and len(codes.split(",")) == 300    # 12 s at 25 codes a second
        assert (body["source_start"], body["source_end"], body["cot"]) == (2.0, 12.0, "off")
        assert body["duration"] == 40.0 and body["semantic_sampling"] == {"min_tokens": 250}
        assert body["style"] == "kazakh folk ballad, dombra, male vocals, wistful, 96 BPM, D minor"
        assert body["lyrics"] == "" and "semantic_tokens" not in body and body["seed"] == 5
        take = rig.store.list_takes(job["id"])[0]
        # the fake renders the 10 s section then 1.5 s of new music: the cut leaves exactly the new part
        assert take["file"]["duration_s"] == 1.5 and take["title"] == "Same style as Two Strings (v1)"
        assert take["qa_report"]["measurements"]["duration_requested_s"] == 30.0
        assert take["qa_report"]["measurements"]["length_mode"] == "cap"
        side = _sidecar(take)
        song = side["song"]
        assert side["relation"] == "same style as" and side["parents"][0]["library_track_id"] == entry["id"]
        assert song["prompt_cut"]["cut"] is True and song["prompt_cut"]["section_s"] == 10.0
        assert song["source_section"] == {"start_s": 2.0, "end_s": 12.0}
        assert song["describe"]["status"] == "done" and song["describe"]["model"] == "music-flamingo-hf.Q8_0"
        assert song["describe"]["answer_sha256"].startswith("sha256:") and song["style_from"] == "describer"
        assert song["keep_filled"] == {"bpm": 96, "key": "D minor"} and song["codes"]["count"] == 300
        assert "not verified" in song["reference_role"]
        assert {"describe.result.json", "tokenize.result.json"} <= set(side["payloads"])
        assert side["duration_requested_s"] == 30 and side["duration_received_s"] == 1.5
        assert any("opening section" in n for n in side["record_notes"])
        lineage = rig.library.get_entry(take["library_track_id"])["source"]["lineage"]
        assert lineage["relation"] == "same style as" and lineage["engine"] == "yue2.cpp"
        assert lineage["caption"] == body["style"]
        assert "--tokenizer" in rig.yue2.build_command(0)

        # a second job on the same audio reads the kept codes: no second /tokenize
        tokenized = len(list(record.glob("*-tokenize.json")))
        again = _propose(rig, entry, {"caption": "dombra ballad", "variants": 1, "duration_s": 30,
                                      "source_section": {"start_s": 0, "end_s": 10}})
        second = await rig.wait_for(again["id"], DONE)
        assert second["state"] == js.REVIEW, second.get("state_reason")
        assert len(list(record.glob("*-tokenize.json"))) == tokenized
        side2 = _sidecar(rig.store.list_takes(again["id"])[0])
        assert side2["song"]["codes"]["kept_across_jobs"] is True
        # a typed style and no Keep box waiting: the describer is not asked
        assert len(asked) == 1 and side2["song"]["describe"]["status"] == "skipped"
        assert json.loads(rig.store.read_payload(again["id"], "synth.v1.request.json"))["style"] == "dombra ballad"


@pytest.mark.asyncio
async def test_a_style_left_to_a_describer_that_cannot_answer_fails_with_the_reason(tmp_path, monkeypatch):
    rig, _record = await _rig(tmp_path, monkeypatch, describer=None)
    async with rig:
        path = _write_source(tmp_path / "music" / "Two Strings.wav")
        entry = await rig.library.register_generated(str(path), job_id="job_" + "1" * 16, lineage={"variant": 1})
        job = _propose(rig, entry, {"caption": "", "variants": 1, "duration_s": 30})
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] in (js.FAILED, js.UNSUPPORTED)
        assert done["state_reason"].startswith("no_style:") and "runs no describer" in done["state_reason"]
        kept = json.loads(rig.store.read_payload(job["id"], "describe.result.json"))
        assert kept["status"] == "skipped"


# -- the command ------------------------------------------------------------------------------------------


class _Settings:
    def __init__(self, home, values):
        self.dpc_home_dir = home
        self.values = values

    def get(self, section, key, fallback=None):
        return self.values.get((section, key), fallback)


class _Library:
    def __init__(self, path, entry):
        self.path, self.entry = path, entry

    def get_entry(self, tid):
        if tid != self.entry["id"]:
            from dpc_client_core.music.library import MusicLibraryError
            raise MusicLibraryError("not_found", "no such track")
        return self.entry

    def get_track_path(self, tid):
        return self.path


class _Service(MusicCommandsMixin):
    def __init__(self, home, values, providers, library):
        self.settings = _Settings(home, values)
        self.llm_manager = type("M", (), {"providers": providers})()
        self._lib = library

    async def _music(self):
        return self._lib


@pytest.mark.asyncio
async def test_describe_source_answers_the_suggestion_and_keeps_it_by_the_audio_hash(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(describe, "ffmpeg_window_decoder", lambda *a, **k: _copy_decoder(seen))
    src = _write_source(tmp_path / "Two Strings.wav", 2.0)
    entry = {"id": TRACK, "content_hash": "md5:" + "d" * 32, "duration_s": 237.4, "title": "Two Strings"}
    model = _Describer()
    service = _Service(tmp_path / "dpc", {}, {"music_flamingo": model}, _Library(src, entry))
    assert service._music_describe_alias()[0] == "music_flamingo"                 # the default when it is loaded
    out = await service.describe_music_track(track_id=TRACK)
    assert out["status"] == "ok", out
    assert out["suggestion"] == {"caption": ANSWER["style"],
                                 "keep": {"genre": "kazakh folk", "bpm": 96, "key": "D minor", "timesig": "3/4",
                                          "mood": "wistful", "vocals": "vocals"}}
    assert out["description"]["cached"] is False and len(model.calls) == 1
    again = await service.describe_music_track(track_id=TRACK)
    assert again["description"]["cached"] is True and len(model.calls) == 1      # the kept one, no second call
    fresh = await service.describe_music_track(track_id=TRACK, refresh=True)
    assert fresh["description"]["cached"] is False and len(model.calls) == 2
    status = service._music_describer_status()
    assert status["status"] == "supported" and status["alias"] == "music_flamingo"

    off = _Service(tmp_path / "dpc2", {("music", "describe_alias"): "none"}, {"music_flamingo": model},
                   _Library(src, entry))
    answer = await off.describe_music_track(track_id=TRACK)
    assert answer["status"] == "error" and answer["code"] == "no_describer" and "turned off" in answer["message"]
    none = _Service(tmp_path / "dpc3", {}, {}, _Library(src, entry))
    assert none._music_describer_status()["status"] == "unsupported"
    named = _Service(tmp_path / "dpc4", {("music", "describe_alias"): "other"}, {"music_flamingo": model},
                     _Library(src, entry))
    assert "not a loaded provider" in named._music_describe_alias()[1]
    record = await none._music_describe_for_job(src, entry["content_hash"], 2.0, tmp_path / "w")
    assert record["status"] == "skipped" and "music_flamingo" in record["reason"]
    missing = await service.describe_music_track(track_id="trk_ffffffffffffffff")
    assert missing["code"] == "invalid_source"


@pytest.mark.asyncio
async def test_describe_then_edit_keep_reaches_yue2_style_and_style_sent_through_propose_music_job(
        tmp_path, monkeypatch):
    monkeypatch.setattr(describe, "ffmpeg_window_decoder", lambda *a, **k: _copy_decoder([]))
    rig, _record = await _rig(tmp_path, monkeypatch, describer=None)
    async with rig:
        path = _write_source(tmp_path / "music" / "Two Strings.wav")
        entry = await rig.library.register_generated(str(path), job_id="job_" + "1" * 16, lineage={"variant": 1})
        rig.library._entries[entry["id"]]["duration_s"] = 12.0
        service = _Service(tmp_path, {}, {"music_flamingo": _Describer()}, rig.library)
        service._music_jobs_instance = rig.store
        service._music_runner_instance = rig.runner
        service._music_engines_instance = rig.engines

        described = await service.describe_music_track(track_id=entry["id"])
        assert described["status"] == "ok", described
        sugg = described["suggestion"]
        assert sugg["caption"] == ANSWER["style"]

        # Simulate MusicSimilar applyDescription + editing BPM to 128 and checking Key ('D minor') before Create.
        params = {
            "caption": sugg["caption"],
            "keep": {
                "genre": sugg["keep"]["genre"],
                "bpm": 128,
                "key": sugg["keep"]["key"],
                "mood": sugg["keep"]["mood"],
                "vocals": sugg["keep"]["vocals"],
            },
            "variants": 1,
            "seed": 7,
            "duration_s": 30,
            "source_section": {"start_s": 2, "end_s": 12},
        }
        proposed = await service.propose_music_job(type="generate_similar", track_id=entry["id"], params=params)
        assert proposed["status"] == "ok", proposed
        done = await rig.wait_for(proposed["job"]["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")

        expected_style = "kazakh folk ballad, dombra, male vocals, wistful, 128 BPM, D minor"
        body = json.loads(rig.store.read_payload(done["id"], "synth.v1.request.json"))
        assert body["style"] == expected_style
        take = rig.store.list_takes(done["id"])[0]
        side = _sidecar(take)
        assert side["song"]["style_sent"] == expected_style
        assert side["caption_sent"] == expected_style
        assert rig.library.get_entry(take["library_track_id"])["source"]["lineage"]["caption"] == expected_style
