"""Region-edit jobs through the runner (`music/runner.py` "Region edits"; ADR-051 row B4): a repaint goes to
acestep.cpp as one multipart `/synth` (request + the control), every take is spliced back into the
control and stored as a sibling `edit` take (a new file, the base untouched) with a provenance record
naming its parent and region and a QA record carrying the seam numbers; a removal runs with no engine;
what cannot run is refused with the reason. The engine is `_fake_acestep_server.py` as a child process,
ffmpeg is replaced by pure-Python stand-ins (the control decoder, the FLAC header writer).
"""

import asyncio
import hashlib
import json
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import qa as qa_module
from dpc_client_core.music import region_edit as re_
from dpc_client_core.music.runner import MusicJobRunner
from tests._acestep_helpers import Rig, fake_flac, fake_loudness, files_in, free_env, install_fake_engine

RATE = 48000
DONE = (js.REVIEW, js.FAILED, js.CANCELLED, js.UNSUPPORTED)


def write_base(path: Path, seconds=6.0) -> Path:
    t = np.arange(int(seconds * RATE)) / RATE
    x = 0.3 * np.sin(2 * np.pi * 220 * t)
    pcm = np.clip(np.round(np.stack([x, x], axis=1) * 32767), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return path


def fake_decoder(src: Path, dest: Path) -> None:
    """The control without ffmpeg: the base is already 48 kHz stereo, written as 24-bit PCM."""
    pcm = re_.read_pcm(src)
    re_.write_wav_s24(dest, [pcm.data], rate=pcm.rate, channels=pcm.channels)


class Keep:
    """A converter that keeps a copy of each WAV it converts (the take's samples), then writes the FLAC header."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.wavs = []

    def __call__(self, wav, flac, tags):
        self.folder.mkdir(parents=True, exist_ok=True)
        copy = self.folder / f"{len(self.wavs)}-{wav.name}"
        shutil.copyfile(wav, copy)
        self.wavs.append(copy)
        fake_flac(wav, flac, tags)


def edit_qa(calls):
    def meter(flac, *, requested_s=None, length_mode="exact", duration_s=None, loudness=None, tool_version=None,
              edit=None):
        calls.append({"requested_s": requested_s, "length_mode": length_mode, "duration_s": duration_s, "edit": edit})
        gates = {name: qa_module._gate(js.GATE_PASS, "fake QA") for name in js.QA_GATES}
        gates["length"] = qa_module.length_gate(requested_s, duration_s, length_mode)
        seams = (edit or {}).get("seams") or {}
        loud = loudness or {}
        return qa_module.build_record(gates=gates, measurements={"duration_s": duration_s}, integrated_lufs=loud.get("lufs_i"),
                                      true_peak_dbtp=loud.get("true_peak_dbtp"), clipped_samples=0,
                                      measured_by="fake QA", seam_fields=seams.get("qa_fields"),
                                      seams_advisory=seams.get("entry"))
    return meter


class EditRig(Rig):
    def __init__(self, home: Path, **kw):
        super().__init__(home, start_runner=False)
        self.qa_calls = []
        self.keep = Keep(home / "kept-wavs")
        self.saved_lyrics = {}
        self.base_lyrics = kw.pop("base_lyrics", None)
        hooks = kw.pop("lyrics_hooks", True)
        self.runner = MusicJobRunner(
            self.store, self.engine, self._library, dpc_home=home, converter=self.keep,
            on_job_change=self.events.append, card_poll_s=0.05, engine_poll_s=0.02, loudness_meter=fake_loudness,
            qa_meter=edit_qa(self.qa_calls), control_decoder=fake_decoder,
            lyrics_get=self._lyrics_get if hooks else None, lyrics_save=self._lyrics_save if hooks else None,
            draw_seed=lambda: 4242)

    async def _lyrics_get(self, track_id):
        return self.base_lyrics

    async def _lyrics_save(self, track_id, lrc):
        self.saved_lyrics[track_id] = lrc

    async def __aenter__(self):
        await super().__aenter__()
        self.runner.start()
        return self

    async def base(self, seconds=6.0):
        path = write_base(self.home / "music" / "Night Drive.wav", seconds)
        entry = await self.library.register_generated(str(path), job_id="job_" + "1" * 16,
                                                      lineage={"relation": "song_from_own_text", "variant": 1})
        return entry, path

    def propose(self, entry, action, region, params=None, seconds=6.0, **kw):
        source = {"kind": "local", "content_hash": entry["content_hash"], "duration_s": seconds}
        job = self.store.create_job("edit", params or {}, track_id=entry["id"], base_version_id="ver_" + "0" * 16,
                                    comment_id="cmt_" + "a" * 16, action=action, region=region, prompt="the vocal cracks",
                                    source=source, **kw)
        self.runner.wake()   # as the command door does after a proposal
        return job


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sidecar_of(take) -> dict:
    from urllib.parse import urlparse
    from urllib.request import url2pathname
    flac = Path(url2pathname(urlparse(take["file"]["uri"]).path))
    return json.loads(flac.with_suffix(".json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_a_regenerate_makes_two_sibling_takes_spliced_into_the_control_and_never_touches_the_base(tmp_path, monkeypatch):
    record = tmp_path / "posts"
    free_env(monkeypatch, record=record)
    install_fake_engine(tmp_path)
    async with EditRig(tmp_path) as rig:
        entry, base = await rig.base()
        before = sha(base)
        job = rig.propose(entry, "regenerate_region", {"t_start": 2.0, "t_end": 4.0},
                          {"caption": "synthwave, warm pads", "lyrics": "[verse]\nnight drive"})
        assert job["state"] == js.QUEUED and job["params"]["takes"] == 2
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")
        assert sha(base) == before                                      # v1 never touched

        # the payloads: the edit request and acestep.cpp's repaint request, written before the run
        names = [p["name"] for p in done["payloads"]]
        assert names == ["edit.request.json", "synth.request.json"]
        sent = json.loads(rig.store.read_payload(job["id"], "synth.request.json"))
        assert sent["task_type"] == "repaint" and sent["synth_batch_size"] == 2 and sent["output_format"] == "wav32"
        assert sent["seed"] == 4242 and sent["caption"] == "synthwave, warm pads"
        assert (int(sent["repainting_start"] * 25), int(sent["repainting_end"] * 25)) == (50, 100)
        posted = json.loads(sorted(record.glob("*-synth.json"))[-1].read_bytes())
        assert posted == sent                                           # what the engine got is the payload
        edit_req = json.loads(rig.store.read_payload(job["id"], "edit.request.json"))
        assert edit_req["control"]["frames"] == 6 * RATE and edit_req["control"]["rate"] == RATE
        assert edit_req["caption_from"] == "params" and edit_req["base"]["library_track_id"] == entry["id"]

        takes = rig.store.list_takes(job["id"])
        assert [t["variant"] for t in takes] == [1, 2]
        for i, take in enumerate(takes):
            assert take["kind"] == "edit" and take["track_id"] == entry["id"] and take["source_track_id"] == entry["id"]
            assert take["region"] == {"t_start": 2.0, "t_end": 4.0} and take["base_version_id"] == "ver_" + "0" * 16
            assert take["file"]["duration_s"] == pytest.approx(6.0) and take["duration_requested_s"] == pytest.approx(6.0)
            assert (take["seed"], take["synth_batch_size"], take["synth_batch_index"]) == (4242 + i, 2, i)
            assert take["engine"] == "acestep.cpp" and take["payload"] == "synth.request.json"
            assert "regenerate 0:02.0-0:04.0" in take["title"]
            # the seam numbers are in mp.qa/1 and the advisory entry; they never gate
            assert take["qa"]["seam_start_jump_db"] is not None and take["qa"]["seam_click_score"] is not None
            seams = take["qa_report"]["advisory"]["seams"]
            assert seams["status"] == "measured" and seams["engine_window"]["t_start"] == 2.0 and len(seams["seams"]) == 2
            assert take["qa_report"]["gates"]["length"]["status"] == "pass"
            # provenance: the parent, the region and the zones
            side = sidecar_of(take)
            assert side["relation"] == "region_edit" and side["edit_action"] == "regenerate_region"
            assert side["parents"][0]["library_track_id"] == entry["id"]
            assert side["parents"][0]["content_hash"] == entry["content_hash"]
            assert side["parents"][0]["job_id"] == "job_" + "1" * 16
            assert side["edit"]["zones"]["splice"] == {"t_start": 2.0, "t_end": 4.0}
            assert side["edit"]["control"]["sha256"] == edit_req["control"]["sha256"]
            assert side["take_id"] == take["id"] and side["licence"]["components"][-1]["role"] == "parent"
            lib = rig.library.get_entry(take["library_track_id"])
            assert lib["source"]["lineage"]["relation"] == "region_edit"
            assert lib["source"]["lineage"]["base_track_id"] == entry["id"]
        assert all(c["length_mode"] == "same" and c["edit"]["zone"] == (2.0, 4.0) for c in rig.qa_calls)

        # the samples: the control outside the splice, the engine's tone inside, both takes differ
        a, b = (re_.read_pcm(w) for w in rig.keep.wavs)
        control = re_.read_pcm(base)
        assert a.frames == b.frames == control.frames
        assert np.max(np.abs(a.data[:RATE] - control.data[:RATE])) < 1e-4       # 16 -> 24 bit, same samples
        assert np.max(np.abs(a.data[5 * RATE:] - control.data[5 * RATE:])) < 1e-4
        assert np.max(np.abs(a.data[int(2.5 * RATE):int(3.5 * RATE)] - b.data[int(2.5 * RATE):int(3.5 * RATE)])) > 0.05
        # the working files are gone; the takes and their sidecars stay
        left = files_in(rig.runner.generated_root / job["id"])
        assert not [n for n in left if n.endswith(".part")]
        assert len([n for n in left if n.endswith(".flac")]) == 2

        # Keep one: the job is accepted; the base is still the same file
        kept = rig.store.keep_take(takes[0]["id"])
        assert kept["decision"] == js.ACCEPTED and rig.store.get_job(job["id"])["state"] == js.ACCEPTED
        assert sha(base) == before


@pytest.mark.asyncio
async def test_fix_seam_repaints_a_window_around_the_seam_on_the_engines_grid(tmp_path, monkeypatch):
    free_env(monkeypatch)
    install_fake_engine(tmp_path)
    async with EditRig(tmp_path) as rig:
        entry, _base = await rig.base()
        job = rig.propose(entry, "fix_seam", {"t_start": 3.0, "t_end": 3.2}, {"caption": "synthwave"})
        assert job["params"]["allowed_zone"] == {"t_start": 2.1, "t_end": 4.1}
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")
        sent = json.loads(rig.store.read_payload(job["id"], "synth.request.json"))
        assert (int(sent["repainting_start"] * 25), int(sent["repainting_end"] * 25)) == (52, 103)
        assert len(rig.store.list_takes(job["id"])) == 2


@pytest.mark.asyncio
async def test_a_removal_runs_with_no_engine_and_its_one_take_is_silent_inside_the_region(tmp_path, monkeypatch):
    record = tmp_path / "posts"
    free_env(monkeypatch, record=record)
    install_fake_engine(tmp_path)
    async with EditRig(tmp_path) as rig:
        entry, base = await rig.base()
        job = rig.propose(entry, "remove_region", {"t_start": 2.0, "t_end": 3.0})
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")
        assert not rig.engine.alive() and not record.exists()          # no engine, no card, nothing posted
        assert [p["name"] for p in done["payloads"]] == ["edit.request.json"]
        (take,) = rig.store.list_takes(job["id"])
        assert take["kind"] == "edit" and take["engine"] == "dpc" and take["payload"] == "edit.request.json"
        assert "seed" not in take and "synth_batch_size" not in take
        assert take["qa"]["seam_start_jump_db"] is None
        assert take["qa_report"]["advisory"]["seams"]["kind"] == "removed_region"
        assert rig.qa_calls[-1]["edit"]["expects_silence"] is True
        samples = re_.read_pcm(rig.keep.wavs[0])
        assert samples.frames == 6 * RATE
        assert not samples.data[int(2.1 * RATE):int(2.9 * RATE)].any()
        side = sidecar_of(take)
        assert side["engine"] == "dpc" and side["task_type"] == "remove_region" and side["seed"] == "not_stored"


@pytest.mark.asyncio
async def test_a_cut_with_shift_shortens_the_take_and_gives_it_the_bases_lyrics_re_timed(tmp_path, monkeypatch):
    free_env(monkeypatch)
    install_fake_engine(tmp_path)
    lyrics = {"schema": "mp.lyrics/1", "track_hash": "x", "source": "manual", "language": None,
              "lines": [{"t_start": 1.0, "t_end": 2.0, "text": "first"}, {"t_start": 4.6, "t_end": 5.0, "text": "gone"},
                        {"t_start": 5.6, "t_end": 5.9, "text": "last"}], "updated_at": "2026-11-25T00:00:00Z"}
    async with EditRig(tmp_path, base_lyrics=lyrics) as rig:
        entry, _base = await rig.base()
        job = rig.propose(entry, "remove_region", {"t_start": 4.5, "t_end": 5.5}, {"cut_with_shift": True})
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")
        (take,) = rig.store.list_takes(job["id"])
        assert take["file"]["duration_s"] == pytest.approx(5.0) and take["duration_requested_s"] == pytest.approx(5.0)
        lrc = rig.saved_lyrics[take["library_track_id"]]
        assert "first" in lrc and "last" in lrc and "gone" not in lrc and "[00:04.60]" in lrc
        assert sidecar_of(take)["lyrics_retimed"]["saved"] is True


@pytest.mark.asyncio
async def test_a_cut_with_shift_on_a_runner_without_lyrics_hooks_records_why_the_lyrics_were_not_retimed(tmp_path, monkeypatch):
    """Review #180 1.5: the re-timing was skipped silently when the lyrics hooks were not wired."""
    free_env(monkeypatch)
    install_fake_engine(tmp_path)
    async with EditRig(tmp_path, lyrics_hooks=False) as rig:
        entry, _base = await rig.base()
        job = rig.propose(entry, "remove_region", {"t_start": 4.5, "t_end": 5.5}, {"cut_with_shift": True})
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")
        (take,) = rig.store.list_takes(job["id"])
        retimed = sidecar_of(take)["lyrics_retimed"]
        assert retimed["saved"] is False and retimed["reason"].startswith("lyrics_hooks_not_wired")


@pytest.mark.asyncio
async def test_an_agent_cannot_ask_for_a_cut_with_shift(tmp_path):
    from tests._music_jobs_helpers import store_at
    store = store_at(tmp_path)
    from dpc_client_core.music.jobs import MusicJobError
    with pytest.raises(MusicJobError) as exc:
        store.create_job("edit", {"cut_with_shift": True}, track_id="trk_" + "0" * 16, base_version_id="ver_" + "0" * 16,
                         comment_id="cmt_" + "0" * 16, action="remove_region", region={"t_start": 55, "t_end": 59},
                         source={"kind": "local", "content_hash": "md5:x", "duration_s": 60},
                         requested_by={"kind": "agent", "agent_id": "agent_1"})
    assert exc.value.field == "cut_with_shift"


@pytest.mark.asyncio
async def test_a_repaint_without_any_style_caption_is_refused_with_what_to_send(tmp_path, monkeypatch):
    free_env(monkeypatch)
    install_fake_engine(tmp_path)
    async with EditRig(tmp_path) as rig:
        entry, _base = await rig.base()
        job = rig.propose(entry, "regenerate_region", {"t_start": 2.0, "t_end": 4.0})
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.UNSUPPORTED and "params.caption" in done["state_reason"]
        assert not rig.engine.alive()


@pytest.mark.asyncio
async def test_a_track_whose_file_changed_since_the_proposal_is_refused_as_a_hash_mismatch(tmp_path, monkeypatch):
    free_env(monkeypatch)
    install_fake_engine(tmp_path)
    async with EditRig(tmp_path) as rig:
        entry, _base = await rig.base()
        stale = dict(entry, content_hash="sha256:" + "f" * 64)
        job = rig.propose(stale, "remove_region", {"t_start": 2.0, "t_end": 3.0})
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.UNSUPPORTED and done["state_reason"].startswith("hash_mismatch")


@pytest.mark.asyncio
async def test_a_base_whose_bytes_changed_behind_the_librarys_back_is_refused_before_it_is_decoded(tmp_path, monkeypatch):
    """Review #180 1.1: the runner compared the library's stored hash with the proposal's copy and never hashed
    the file itself; a rewrite the library had not polled yet was decoded and edited."""
    free_env(monkeypatch)
    install_fake_engine(tmp_path)
    async with EditRig(tmp_path) as rig:
        entry, base = await rig.base()
        decoded = []
        real = rig.runner._decode_control
        rig.runner._decode_control = lambda src, dest: (decoded.append(src), real(src, dest))
        t = np.arange(6 * RATE) / RATE
        x = np.round(0.2 * np.sin(2 * np.pi * 330 * t) * 32767).astype("<i2")
        with wave.open(str(base), "wb") as w:            # other audio, the library not told
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(np.stack([x, x], axis=1).tobytes())
        assert rig.library.get_entry(entry["id"])["content_hash"] == entry["content_hash"]
        job = rig.propose(entry, "remove_region", {"t_start": 2.0, "t_end": 3.0})
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.UNSUPPORTED and done["state_reason"].startswith("hash_mismatch")
        assert "hashed again" in done["state_reason"] and decoded == []


@pytest.mark.asyncio
async def test_a_repaint_answer_missing_a_take_fails_the_job_and_names_the_count(tmp_path, monkeypatch):
    free_env(monkeypatch, modes="short_batch")
    install_fake_engine(tmp_path)
    async with EditRig(tmp_path) as rig:
        entry, _base = await rig.base()
        job = rig.propose(entry, "regenerate_region", {"t_start": 2.0, "t_end": 4.0}, {"caption": "synthwave"})
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.FAILED and "1 audio part(s)" in done["state_reason"]
