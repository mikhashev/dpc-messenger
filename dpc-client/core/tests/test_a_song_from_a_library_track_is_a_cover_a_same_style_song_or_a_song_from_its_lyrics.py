"""Song jobs from a library track (`music/similar.py`, ADR-051 row B5; SPEC flows 10, 15, 17, 21): a cover
(`cover`), a same-style song (`generate_similar`) and a song from the track's lyrics (`generate_from_lyrics`).

The params are checked at the door (`job_schemas.normalize_params_song`, `MusicJobStore._song_params`): a web
track's audio waits for the streaming integration (`web_source_pending_integration`), a track still hashing
cannot send it either (`track_not_ready`),
a YuE2 cover needs the lyrics (`missing_lyrics`), and the caption and lyrics share the 1 MiB text bound of a
text job. The runs go to the fake engines as child processes: acestep.cpp's cover is one multipart `/synth`
with the source as `audio`; a same-style song is `/lm` then `/synth`, with the source as `ref_audio` when it
is a reference; yue2.cpp's cover asks `/transcribe` for the score and sends it as `abc`. Every take is a
new-track take with the source's lineage ("cover of", "same style as", "new song from the lyrics of") in the
library, the take record and its provenance; the lineage graph puts the source before the job.
"""

import json
import wave
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import numpy as np
import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import lineage as lin
from dpc_client_core.music import region_edit as re_
from dpc_client_core.music.capabilities import EngineStatus, MusicCapabilities
from dpc_client_core.music.jobs import MusicJobError, MusicJobStore
from dpc_client_core.music.runner import MusicJobRunner
from tests._acestep_helpers import Rig, fake_flac, fake_loudness, fake_qa, files_in, free_env, install_fake_engine
from tests._yue2_helpers import DualRig, install_fake_yue2

RATE = 48000
DONE = (js.REVIEW, js.FAILED, js.CANCELLED, js.UNSUPPORTED)
TRACK = "trk_0123456789abcdef"
VERSION = "ver_0123456789abcdef"
LOCAL = {"kind": "local", "content_hash": "md5:" + "a" * 32, "duration_s": 30.5, "title": "Night Drive"}
LINES = ["[verse]", "night drive, city lights", "[chorus]", "we never stop"]


def write_base(path: Path, seconds=6.0) -> Path:
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


def fake_decoder(src: Path, dest: Path) -> None:
    """The source decode without ffmpeg: the base is already 48 kHz stereo, written as 24-bit PCM."""
    pcm = re_.read_pcm(src)
    re_.write_wav_s24(dest, [pcm.data], rate=pcm.rate, channels=pcm.channels)


def installed():
    return EngineStatus(state="stopped", reason="installed; DPC starts it on demand", supervised=True)


def store_at(home):
    store = MusicJobStore(home, capabilities=MusicCapabilities(installed, ffmpeg_probe=lambda: True))
    store.open()
    return store


def sidecar_of(take) -> dict:
    flac = Path(url2pathname(urlparse(take["file"]["uri"]).path))
    return json.loads(flac.with_suffix(".json").read_text(encoding="utf-8"))


# -- the params --------------------------------------------------------------------------------


def test_a_cover_on_acestep_reuses_the_lyrics_keeps_the_melody_and_is_as_long_as_its_source():
    p = js.normalize_params_song("cover", {"caption": "jazz piano trio", "lyrics": LINES, "engine": "acestep"},
                                 track_id=TRACK, source=LOCAL)
    assert p["engine"] == "acestep" and p["lyrics_mode"] == "reuse" and p["kept"] == ["melody", "lyrics"]
    assert p["melody_source"] == "source_audio" and p["reference"] == "audio"
    assert p["reference_uri"] == f"dpc-library:{TRACK}" and p["cover_strength"] == 1.0
    assert p["duration_s"] == 30.5 and p["source_title"] == "Night Drive" and p["source_kind"] == "local"
    assert js.check_params_song("cover", p) is None                         # a stored record normalizes to itself
    no_lyrics = js.normalize_params_song("cover", {"caption": "punk", "lyrics_mode": "none", "engine": "acestep"},
                                         track_id=TRACK, source=LOCAL)
    assert no_lyrics["kept"] == ["melody"] and no_lyrics["lyrics"] is None  # ACE-Step cover works without lyrics


def test_a_yue2_cover_without_lyrics_is_missing_lyrics_and_a_song_from_lyrics_needs_them():
    with pytest.raises(js.SongParamsError) as err:
        js.normalize_params_song("cover", {"caption": "x", "engine": "yue2", "lyrics_mode": "none"}, track_id=TRACK,
                                 source=LOCAL)
    assert err.value.code == "missing_lyrics"
    p = js.normalize_params_song("cover", {"caption": "x", "engine": "yue2", "lyrics": LINES, "cot": "melody"},
                                 track_id=TRACK, source=LOCAL)
    assert p["melody_source"] == "sheetsage2:abc" and p["duration_s"] == 30.5 and "cover_strength" not in p
    with pytest.raises(js.ParamsError, match="cot"):
        js.normalize_params_song("cover", {"caption": "x", "engine": "yue2", "lyrics": LINES, "cot": "off"},
                                 track_id=TRACK, source=LOCAL)
    with pytest.raises(js.ParamsError, match="lyrics_mode"):
        js.normalize_params_song("generate_from_lyrics", {"caption": "x", "lyrics_mode": "none"}, track_id=TRACK,
                                 source=LOCAL)
    lyr = js.normalize_params_song("generate_from_lyrics", {"caption": "x", "lyrics": "a\nb"}, track_id=TRACK,
                                   source=LOCAL)
    assert lyr["engine"] == "yue2" and lyr["lyrics"] == ["a", "b"]
    with pytest.raises(js.ParamsError, match="reference"):
        js.normalize_params_song("generate_from_lyrics", {"caption": "x", "lyrics": "a", "reference": "audio"},
                                 track_id=TRACK, source=LOCAL)


def test_same_style_keeps_what_is_kept_and_refuses_unknown_keys_and_engines():
    p = js.normalize_params_song("generate_similar", {"caption": "synthwave", "keep": {"bpm": 96, "key": "E minor"},
                                                      "lyrics_mode": "new", "lyrics_theme": "night roads",
                                                      "engine": "acestep"},
                                 track_id=TRACK, source=LOCAL)
    assert p["keep"] == {"bpm": 96, "key": "E minor"} and p["lyrics"] is None and p["lyrics_theme"] == "night roads"
    assert p["duration_s"] == 31 and p["reference"] == "description" and p["reference_uri"] is None
    for bad, field in (({"caption": "x", "colour": "red"}, "colour"), ({"caption": "x", "engine": "ace"}, "engine"),
                       ({"caption": "x", "keep": {"tempo": 90}}, "keep"),
                       ({"caption": " ", "engine": "acestep"}, "caption"),
                       ({"caption": "x", "cover_strength": 0.5}, "cover_strength")):
        with pytest.raises(js.ParamsError) as err:
            js.normalize_params_song("generate_similar", bad, track_id=TRACK, source=LOCAL)
        assert err.value.field == field


def test_the_caption_theme_and_lyrics_share_the_one_mebibyte_text_bound_of_a_text_job(tmp_path):
    line = "ю" * 1000                                            # 2000 bytes a line
    lines = [line] * (js.MAX_SONG_TEXT_BYTES // 2000 + 1)
    with pytest.raises(js.ParamsError) as err:
        js.normalize_params_song("generate_from_lyrics", {"caption": "x", "lyrics": lines, "engine": "acestep"},
                                 track_id=TRACK, source=LOCAL)
    assert err.value.field == "lyrics" and str(js.MAX_SONG_TEXT_BYTES) in err.value.message
    with pytest.raises(js.ParamsError, match="not valid text"):
        js.normalize_params_song("generate_similar", {"caption": "x\ud800"}, track_id=TRACK, source=LOCAL)
    with pytest.raises(MusicJobError) as door:
        store_at(tmp_path).create_job("cover", {"caption": "x" * (js.MAX_SONG_TEXT_BYTES + 1), "engine": "acestep"},
                                      track_id=TRACK, base_version_id=VERSION, source=LOCAL)
    assert door.value.code == "invalid_params"


# -- the door ----------------------------------------------------------------------------------


def test_the_door_refuses_a_web_tracks_audio_a_track_still_hashing_and_a_missing_source(tmp_path):
    store = store_at(tmp_path)
    web = {"kind": "web", "content_hash": None, "duration_s": 200.0, "title": "A stream"}
    for job_type, params in (("cover", {"caption": "punk", "lyrics_mode": "none", "engine": "acestep"}),
                             ("generate_similar", {"caption": "punk", "reference": "audio", "engine": "acestep"}),
                             ("generate_similar", {"caption": "punk"})):
        with pytest.raises(MusicJobError) as err:
            store.create_job(job_type, params, track_id=TRACK, base_version_id=VERSION, source=web)
        assert err.value.code == "web_source_pending_integration" and err.value.field == "track_id"
        assert "Yandex Music / SoundCloud integration" in err.value.message
    # a same-style song from a web track's description needs no audio: allowed (SPEC: web tracks fully allowed)
    ok = store.create_job("generate_similar", {"caption": "punk", "engine": "acestep"}, track_id=TRACK,
                          base_version_id=VERSION, source=web)
    assert ok["state"] == js.QUEUED and ok["prompt"] == "punk" and ok["params"]["source_kind"] == "web"
    js.validate_job(ok)
    hashing = dict(LOCAL, content_hash=None)
    with pytest.raises(MusicJobError) as err:
        store.create_job("cover", {"caption": "punk", "lyrics_mode": "none", "engine": "acestep"}, track_id=TRACK,
                         base_version_id=VERSION, source=hashing)
    assert err.value.code == "track_not_ready"
    with pytest.raises(MusicJobError) as err:
        store.create_job("cover", {"caption": "punk"}, track_id=TRACK, base_version_id=VERSION, source=None)
    assert err.value.code == "invalid_source"
    with pytest.raises(MusicJobError) as err:
        store.create_job("cover", {"caption": "x", "engine": "yue2", "lyrics_mode": "none"}, track_id=TRACK,
                         base_version_id=VERSION, source=LOCAL)
    assert err.value.code == "missing_lyrics"


def test_each_song_type_and_engine_says_whether_it_runs_with_the_reason():
    caps = MusicCapabilities(installed, ffmpeg_probe=lambda: True)
    report = caps.report()
    for t in js.SONG_TYPES:
        assert report["job_types"][t]["status"] == "supported", t
    cover = report["features"]["cover"]["cover_engines"]
    assert cover["acestep"]["status"] == "supported" and "task cover" in cover["acestep"]["reason"]
    assert cover["yue2"]["status"] == "unsupported" and "transcriber" in cover["yue2"]["reason"]
    assert "acestep.cpp runs without it" in cover["yue2"]["reason"]          # the fallback is offered
    assert report["features"]["cover"]["default_engine"] == "yue2"
    assert report["features"]["cover"]["fallback_engine"] == "acestep"
    assert report["features"]["web_source_audio"]["status"] == "planned"
    ref = report["features"]["audio_reference"]
    assert ref["status"] == "supported" and "timbre" in ref["reason"] and ref["verified"] is False
    assert report["limits"]["song"]["relations"]["cover"] == "cover of"
    # a cover on yue2 is checked against yue2 (its transcriber); a reference without ffmpeg cannot decode
    assert caps.unsupported_reason("cover", {"engine": "yue2"}).startswith("yue2.cpp cover:")
    no_ffmpeg = MusicCapabilities(installed, ffmpeg_probe=lambda: False)
    assert "ffmpeg" in no_ffmpeg.unsupported_reason("generate_similar", {"engine": "acestep", "reference": "audio"})
    assert no_ffmpeg.unsupported_reason("generate_similar", {"engine": "acestep", "reference": "description"}) is None
    off = MusicCapabilities(lambda: EngineStatus(state="not_installed", reason="ace-server missing"))
    assert off.report()["job_types"]["cover"]["status"] == "unsupported"


# -- the runs ----------------------------------------------------------------------------------


class SongRig(Rig):
    def __init__(self, home: Path):
        super().__init__(home, start_runner=False)
        self.store.capabilities._ffmpeg_probe = lambda: True
        self.runner = MusicJobRunner(
            self.store, self.engine, self._library, dpc_home=home, converter=fake_flac,
            on_job_change=self.events.append, card_poll_s=0.05, engine_poll_s=0.02, loudness_meter=fake_loudness,
            qa_meter=fake_qa, control_decoder=fake_decoder, draw_seed=lambda: 777)

    async def __aenter__(self):
        await super().__aenter__()
        self.runner.start()
        return self

    async def source(self, seconds=12.0, name="Night Drive.wav"):
        path = write_base(self.home / "music" / name, seconds)
        entry = await self.library.register_generated(str(path), job_id="job_" + "1" * 16,
                                                      lineage={"relation": "song_from_own_text", "variant": 1})
        return entry, path

    def propose(self, job_type, entry, params, kind="local", seconds=12.0):
        source = {"kind": kind, "content_hash": entry["content_hash"], "duration_s": seconds, "title": "Night Drive"}
        job = self.store.create_job(job_type, params, track_id=entry["id"], base_version_id=VERSION, source=source)
        self.runner.wake()
        return job


@pytest.mark.asyncio
async def test_an_acestep_cover_sends_the_source_as_audio_and_makes_new_tracks_titled_cover_of(tmp_path, monkeypatch):
    record = tmp_path / "posts"
    free_env(monkeypatch, record=record)
    install_fake_engine(tmp_path)
    async with SongRig(tmp_path) as rig:
        entry, base = await rig.source()
        before = base.read_bytes()
        job = rig.propose("cover", entry, {"caption": "bossa nova, nylon guitar", "lyrics": LINES, "engine": "acestep",
                                           "keep": {"bpm": 92}, "cover_strength": 0.6})
        assert job["state"] == js.QUEUED and job["params"]["duration_s"] == 12.0
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")
        assert [p["name"] for p in done["payloads"]] == ["song.request.json", "synth.request.json"]
        sent = json.loads(rig.store.read_payload(job["id"], "synth.request.json"))
        assert sent["task_type"] == "cover" and sent["audio_cover_strength"] == 0.6 and sent["bpm"] == 92
        assert sent["caption"] == "bossa nova, nylon guitar" and sent["lyrics"] == "\n".join(LINES)
        assert sent["synth_batch_size"] == 2 and sent["seed"] == 777 and sent["output_format"] == "wav24"
        assert not list(record.glob("*-lm.json"))                                   # a cover has no LM pass
        parts = json.loads(sorted(record.glob("*-synth-parts.json"))[-1].read_bytes())
        assert parts["parts"] == ["request", "audio"] and parts["sizes"]["audio"] > 12 * RATE * 6
        song = json.loads(rig.store.read_payload(job["id"], "song.request.json"))
        assert song["relation"] == "cover of" and song["source"]["library_track_id"] == entry["id"]
        assert song["source_audio"]["frames"] == 12 * RATE and song["source_audio"]["sha256"].startswith("sha256:")

        takes = rig.store.list_takes(job["id"])
        assert [t["variant"] for t in takes] == [1, 2]
        for i, take in enumerate(takes):
            assert take["kind"] == "new_track" and take["track_id"] is None and take["source_track_id"] == entry["id"]
            assert take["title"] == f"Cover of Night Drive (v{i + 1})" and take["engine"] == "acestep.cpp"
            assert (take["seed"], take["synth_batch_size"], take["synth_batch_index"]) == (777, 2, i)
            assert take["qc"]["flow"] == "cover" and take["qa_report"]["verdict"] == "passed"
            lib = rig.library.get_entry(take["library_track_id"])
            lineage = lib["source"]["lineage"]
            assert lineage["relation"] == "cover of" and lineage["source_track_id"] == entry["id"]
            assert lineage["source_name"] == "Night Drive" and lineage["engine"] == "acestep.cpp"
            side = sidecar_of(take)
            assert side["relation"] == "cover of" and side["task_type"] == "cover"
            assert side["parents"][0]["library_track_id"] == entry["id"] and side["parents"][0]["role"] == "source"
            assert side["song"]["kept"] == ["melody", "lyrics"] and side["song"]["cover_strength"] == 0.6
            assert "song.request.json" in side["payloads"]
            assert side["licence"]["components"][-1] == {"role": "parent", "name": "the source track",
                                                         "licence": side["licence"]["components"][-1]["licence"]}
        assert base.read_bytes() == before                                          # the source never changes
        assert not [n for n in files_in(rig.runner.generated_root / job["id"]) if n.endswith(".part")]


@pytest.mark.asyncio
async def test_same_style_with_an_audio_reference_sends_the_kept_metadata_to_the_lm_and_the_source_as_ref_audio(
        tmp_path, monkeypatch):
    record = tmp_path / "posts"
    free_env(monkeypatch, record=record)
    install_fake_engine(tmp_path)
    async with SongRig(tmp_path) as rig:
        entry, _ = await rig.source()
        job = rig.propose("generate_similar", entry, {"caption": "synthwave, warm pads", "reference": "audio",
                                                      "engine": "acestep",
                                                      "keep": {"bpm": 96, "key": "E minor", "timesig": "4",
                                                               "genre": "synthwave"},
                                                      "variants": 1, "duration_s": 20})
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")
        lm = json.loads(rig.store.read_payload(job["id"], "lm.request.json"))
        assert (lm["bpm"], lm["keyscale"], lm["timesignature"]) == (96, "E minor", "4")
        assert lm["lyrics"] == "[Instrumental]" and lm["caption"] == "synthwave, warm pads" and lm["duration"] == 20
        parts = json.loads(sorted(record.glob("*-synth-parts.json"))[-1].read_bytes())
        assert parts["parts"] == ["request", "ref_audio"]
        take = rig.store.list_takes(job["id"])[0]
        assert take["title"] == "Same style as Night Drive (v1)" and take["qc"]["flow"] == "same_style"
        side = sidecar_of(take)
        assert side["relation"] == "same style as" and side["task_type"] == "text2music"
        assert "timbre" in side["song"]["reference_role"] and side["audio_codes"]   # the LM's codes are the genome
        assert rig.library.get_entry(take["library_track_id"])["source"]["lineage"]["relation"] == "same style as"


@pytest.mark.asyncio
async def test_same_style_from_a_description_decodes_nothing_and_new_lyrics_go_to_the_lm_empty(tmp_path, monkeypatch):
    record = tmp_path / "posts"
    free_env(monkeypatch, record=record)
    install_fake_engine(tmp_path)
    async with SongRig(tmp_path) as rig:
        entry, _ = await rig.source()
        rig.runner._decode_control = lambda *a: (_ for _ in ()).throw(AssertionError("no decode for a description"))
        job = rig.propose("generate_similar", entry, {"caption": "lo-fi", "lyrics_mode": "new", "engine": "acestep",
                                                      "lyrics_theme": "rainy mornings", "variants": 1})
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")
        lm = json.loads(rig.store.read_payload(job["id"], "lm.request.json"))
        assert lm["lyrics"] == "" and lm["caption"] == "lo-fi, lyrics about rainy mornings"
        assert not list(record.glob("*-synth-parts.json"))                         # a plain JSON /synth
        side = sidecar_of(rig.store.list_takes(job["id"])[0])
        # no audio and no lyrics of the source went in: its licence does not join the take's
        assert all(c.get("role") != "parent" for c in side["licence"]["components"])


@pytest.mark.asyncio
async def test_a_song_from_the_lyrics_on_acestep_sings_the_lines_and_a_changed_source_is_refused(tmp_path, monkeypatch):
    free_env(monkeypatch)
    install_fake_engine(tmp_path)
    async with SongRig(tmp_path) as rig:
        entry, path = await rig.source()
        job = rig.propose("generate_from_lyrics", entry, {"caption": "folk", "lyrics": LINES, "engine": "acestep",
                                                          "variants": 1})
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")
        assert json.loads(rig.store.read_payload(job["id"], "lm.request.json"))["lyrics"] == "\n".join(LINES)
        take = rig.store.list_takes(job["id"])[0]
        assert take["title"] == "From the lyrics of Night Drive (v1)"
        assert sidecar_of(take)["relation"] == "new song from the lyrics of"

        # the source's hash at the proposal is not the library's now: refused before any engine work
        source = {"kind": "local", "content_hash": "md5:" + "f" * 32, "duration_s": 12.0, "title": "Night Drive"}
        stale = rig.store.create_job("cover", {"caption": "punk", "lyrics_mode": "none", "engine": "acestep"},
                                     track_id=entry["id"], base_version_id=VERSION, source=source)
        rig.runner.wake()
        refused = await rig.wait_for(stale["id"], DONE)
        assert refused["state"] == js.UNSUPPORTED and refused["state_reason"].startswith("hash_mismatch")


@pytest.mark.asyncio
@pytest.mark.parametrize("job_type, params", [
    ("cover", {"caption": "punk", "lyrics_mode": "none", "engine": "acestep"}),
    ("generate_similar", {"caption": "synthwave", "reference": "audio", "lyrics_mode": "none", "variants": 1,
                          "engine": "acestep"}),
])
async def test_a_source_whose_bytes_changed_behind_the_librarys_back_is_refused_before_it_is_decoded_or_sent(
        tmp_path, monkeypatch, job_type, params):
    """Follow-up 15 (review #180 1.1 carried to song jobs): the source was decoded and sent as `audio` /
    `ref_audio` on the library's stored hash alone; a rewrite the library had not polled yet went through."""
    record = tmp_path / "posts"
    free_env(monkeypatch, record=record)
    install_fake_engine(tmp_path)
    async with SongRig(tmp_path) as rig:
        entry, path = await rig.source()
        decoded = []
        real = rig.runner._decode_control
        rig.runner._decode_control = lambda src, dest: (decoded.append(src), real(src, dest))
        t = np.arange(12 * RATE) / RATE
        x = np.round(0.2 * np.sin(2 * np.pi * 330 * t) * 32767).astype("<i2")
        with wave.open(str(path), "wb") as w:                 # other audio, the library not told
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(np.stack([x, x], axis=1).tobytes())
        assert rig.library.get_entry(entry["id"])["content_hash"] == entry["content_hash"]
        job = rig.propose(job_type, entry, params)
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.UNSUPPORTED and done["state_reason"].startswith("hash_mismatch"), done
        assert "hashed again" in done["state_reason"] and decoded == []
        assert not list(record.glob("*-synth-parts.json")) and not rig.engine.alive()


@pytest.mark.asyncio
async def test_a_yue2_cover_transcribes_the_source_to_a_score_and_sings_it_with_the_lyrics(tmp_path, monkeypatch):
    record = tmp_path / "posts"
    monkeypatch.setenv("FAKE_YUE2_MODE", "")
    monkeypatch.setenv("FAKE_YUE2_RECORD", str(record))
    free_env(monkeypatch)
    models = install_fake_yue2(tmp_path, voices=())
    install_fake_engine(tmp_path)
    rig = DualRig(tmp_path)
    assert rig.store.capabilities.song_type_status("cover", {"engine": "yue2"})["status"] in ("supported",
                                                                                            "unsupported")
    for rel in ("serveurperso/SheetSage2-Q8_0.gguf", "serveurperso/MERT-v2-FullSong-Q8_0.gguf"):
        (models / rel).write_bytes(b"gguf")
    rig.store.capabilities._ffmpeg_probe = lambda: True
    rig.runner._decode_control = fake_decoder
    async with rig:
        assert rig.store.capabilities.song_type_status("cover", {"engine": "yue2"})["status"] == "supported"
        path = write_base(tmp_path / "music" / "Night Drive.wav")
        entry = await rig.library.register_generated(str(path), job_id="job_" + "1" * 16, lineage={"variant": 1})
        source = {"kind": "local", "content_hash": entry["content_hash"], "duration_s": 6.0, "title": "Night Drive"}
        job = rig.store.create_job("cover", {"caption": "russian rock, male vocal", "engine": "yue2", "lyrics": LINES,
                                             "cot": "melody", "variants": 1, "seed": 5},
                                   track_id=entry["id"], base_version_id=VERSION, source=source)
        assert job["state"] == js.QUEUED and job["params"]["duration_s"] == 6.0  # the source's length
        rig.runner.wake()
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")
        names = [p["name"] for p in done["payloads"]]
        assert names[:3] == ["song.request.json", "transcribe.result.json", "synth.v1.request.json"]
        asked = json.loads(sorted(record.glob("*-transcribe.json"))[-1].read_bytes())
        assert asked["melody_only"] is True and asked["audio_bytes"] > 6 * RATE * 6
        body = json.loads(rig.store.read_payload(job["id"], "synth.v1.request.json"))
        assert body["abc"].startswith("X:1\nT:fake transcription") and body["cot"] == "melody"
        assert body["lyrics"] == "\n".join(LINES) and body["style"] == "russian rock, male vocal" and body["seed"] == 5
        # as long as the source: the cap one second over it, the floor at it (25 frames a second; follow-up 21)
        assert body["duration"] == 7.0 and body["semantic_sampling"] == {"min_tokens": 150}
        take = rig.store.list_takes(job["id"])[0]
        assert take["title"] == "Cover of Night Drive (v1)" and take["engine"] == "yue2.cpp"
        side = sidecar_of(take)
        assert side["relation"] == "cover of" and side["task_type"] == "cover"
        assert side["genome_basis"] == "semantic_tokens+score_abc" and side["score_abc"] == body["abc"]
        assert "transcribe.result.json" in side["payloads"] and side["song"]["melody_source"] == "sheetsage2:abc"
        assert "--transcriber" in rig.yue2.build_command(0)


# -- the lineage graph ---------------------------------------------------------------------------


def _job(jid, job_type, track_id=None, params=None, created="2026-11-25T10:00:00Z"):
    return {"id": jid, "type": job_type, "track_id": track_id, "action": None, "state": "review",
            "created_at": created, "params": params or {}, "take_ids": []}


def _take(tid, jid, variant, lib, h):
    return {"id": tid, "job_id": jid, "variant": variant, "kind": "new_track", "library_track_id": lib,
            "file": {"content_hash": h, "duration_s": 6.0}, "decision": "pending", "title": tid}


def test_the_lineage_puts_the_source_track_before_a_cover_and_a_take_source_joins_its_own_branch():
    cover = _job("job_c", "cover", TRACK, {"caption": "bossa nova", "engine": "acestep", "lyrics": LINES,
                                           "lyrics_mode": "reuse", "kept": ["melody", "lyrics"]})
    doc = lin.build_lineage([cover], [_take("tak_c1", "job_c", 1, "lib_c1", "md5:c1")], track_id=TRACK)
    by_id = {n["id"]: n for n in doc["nodes"]}
    assert by_id[f"source:{TRACK}"]["kind"] == "source" and by_id["intent:job_c"]["relation"] == "cover of"
    derive = [e for e in doc["edges"] if e["kind"] == "derive"]
    assert derive == [{"id": f"source:{TRACK}>intent:job_c", "from": f"source:{TRACK}", "to": "intent:job_c",
                       "kind": "derive", "op": "cover of", "job_id": "job_c", "group": "job_c", "region": None,
                       "rejected": False, "reason": None}]
    assert [n["generation"] for n in doc["nodes"]] == [0, 1, 2] and doc["root"]["node_id"] == f"source:{TRACK}"
    assert by_id["intent:job_c"]["params"]["lyrics"] == {"instrumental": False, "lines": 2}

    # the source is itself a take of a text job: the derive edge leaves that take, one graph for both
    text = _job("job_t", "generate_from_text", None, {"style": "synthwave"}, created="2026-11-25T09:00:00Z")
    similar = _job("job_s", "generate_similar", "lib_t1", {"caption": "same, slower", "engine": "acestep"})
    takes = [_take("tak_t1", "job_t", 1, "lib_t1", "md5:t1"), _take("tak_s1", "job_s", 1, "lib_s1", "md5:s1")]
    doc = lin.build_lineage([text, similar], takes, job_id="job_s")
    edges = {(e["from"], e["to"], e["kind"], e["op"]) for e in doc["edges"]}
    assert ("tak_t1", "intent:job_s", "derive", "same style as") in edges
    assert ("intent:job_s", "tak_s1", "generate", "generate_similar") in edges
    assert doc["stats"]["jobs"] == 2 and doc["stats"]["max_generation"] == 3


# -- the agent's card ----------------------------------------------------------------------------


def test_the_agents_approval_card_names_the_source_the_mode_and_what_of_the_source_goes_in():
    from dpc_client_core.dpc_agent.tools import music as tool
    p = js.normalize_params_song("cover", {"caption": "bossa nova", "lyrics": LINES}, track_id=TRACK, source=LOCAL)
    about = tool._song_about("cover", p)
    assert 'a cover (same melody, new arrangement) of "Night Drive"' in about and TRACK in about
    assert "its audio (melody and lyrics)" in about and "yue2.cpp" in about and "bossa nova" in about
    parts = tool._song_parts("cover", p)
    assert parts["source_title"] == "Night Drive" and parts["mode"] == "cover" and parts["relation"] == "cover of"
    assert parts["has_lyrics"] is True and parts["style"] == "bossa nova"
    ref = js.normalize_params_song("generate_similar", {"caption": "x", "reference": "audio", "engine": "acestep"},
                                   track_id=TRACK, source=LOCAL)
    assert "timbre reference" in tool._song_about("generate_similar", ref)
    args, rest, problem = tool._song_split({"caption": "x"})
    assert problem and "track_id" in problem
    args, rest, problem = tool._song_split({"caption": "x", "track_id": TRACK})
    assert args == {"track_id": TRACK} and rest == {"caption": "x"} and problem is None
    assert "cover" in tool.get_tools()[1].schema["parameters"]["properties"]["type"]["enum"]
