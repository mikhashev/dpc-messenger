"""Tests for the six narrow P3 gaps in music library, yue2, runner, and jobs
(backlog card MUSIC-LIBRARY-RUNNER-AND-JOBS-HAVE-SIX-NARROW-GAPS-FROM-THE-2026-11-26-AUDIT).

1. Invalid folder identity (`library._apply_identity`): never leave `origin="folder"`
   without a valid `folder_id` so subsequent `flush()` calls succeed.
2. `.safetensors` YuE2 adapters falling back to `acestep` (`yue2.py` + `runner.py`).
3. `remove_region` with `cut_with_shift=True` over an entire <= 30s track refused cleanly
   with zero 0 s FLAC or orphan library entries.
4. Interrupted `_edit_prepare` on repaint edits writes missing `SYNTH_PAYLOAD` on resume.
5. `MusicJobStore._derived_key` and explicit idempotency conflict checks include `action`,
   `region`, `base_version_id`, `comment_id`, and `conversation_id`.
6. Pre-ceiling YuE2 `job.json` records load on `MusicJobStore.open()` without moving to
   `.rejected-*`, allowing the runner to emit `context_exceeded` while malformed jobs
   are still quarantined.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import region_edit as re_
from dpc_client_core.music import schemas
from dpc_client_core.music.capabilities import MusicCapabilities
from dpc_client_core.music.engine_base import MusicEngineError
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.jobs import MusicJobError, MusicJobStore
from dpc_client_core.music.library import MusicLibrary
from dpc_client_core.music.runner import EDIT_PAYLOAD, SYNTH_PAYLOAD, MusicJobRunner
from tests._acestep_helpers import free_env, install_fake_engine
from tests._music_job_commands_helpers import library_tracks, service_with_engine, without_tools
from tests._music_jobs_helpers import from_text_params, installed_probe, job_in_review
from tests._yue2_helpers import DualRig, install_both, safetensors_bytes, yue2_env, yue2_params
from tests.test_a_region_edit_runs_through_the_runner_into_sibling_takes_with_a_seam_report import (
    DONE,
    EditRig,
    write_base,
)


# -- 1: invalid folder identity in library._apply_identity ----------------------------------------


@pytest.mark.asyncio
async def test_1_apply_identity_never_leaves_folder_origin_without_valid_folder_id_and_flush_succeeds(tmp_path):
    home = tmp_path / "dpc"
    outside = tmp_path / "outside"
    outside.mkdir()
    lib = MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
    await lib.open()
    try:
        gen_path = write_base(outside / "gen_take.wav", seconds=2.0)
        gen = await lib.register_generated(
            str(gen_path),
            job_id="job_" + "1" * 16,
            lineage={"relation": "song_from_own_text", "variant": 1},
        )
        assert gen["origin"] == "generated" and gen["folder_id"] is None

        # Replace audio bytes on disk for an entry outside any watched folder (`previous != key`).
        write_base(gen_path, seconds=3.0)
        lib._verify[gen["id"]] = (gen["hash_kind"], gen["content_hash"])
        lib._enqueue(gen["id"])
        await lib.wait_hashed()

        # Before flush: every present/missing entry must satisfy the schema (no origin="folder" + folder_id=None).
        for entry in lib.list_entries():
            schemas.validate_library_entry(entry)
            if entry.get("origin") == "folder":
                assert entry.get("folder_id") is not None

        # Must persist cleanly without raising SchemaError.
        await lib.flush()

        # Original generated entry keeps its origin; new entry has a valid local origin without folder_id=None bug.
        orig_after = lib.get_entry(gen["id"])
        assert orig_after["origin"] == "generated" and orig_after["missing"] is True
        active = [e for e in lib.list_entries(include_missing=False)]
        assert len(active) == 1
        assert active[0]["origin"] == "import" and active[0]["folder_id"] is None

        # Also handle an existing corrupt/stale entry with origin="folder", folder_id=None on re-hash (`previous == key`).
        stale_id = active[0]["id"]
        lib._entries[stale_id]["origin"] = "folder"
        lib._entries[stale_id]["folder_id"] = None
        cur = lib._entries[stale_id]
        lib._verify[stale_id] = (cur["hash_kind"], cur["content_hash"])
        lib._enqueue(stale_id)
        await lib.wait_hashed()
        schemas.validate_library_entry(lib.get_entry(stale_id))
        await lib.flush()
    finally:
        await lib.stop()

    reloaded = MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
    await reloaded.open()
    try:
        for entry in reloaded.list_entries():
            schemas.validate_library_entry(entry)
    finally:
        await reloaded.stop()


# -- 2: .safetensors YuE2 adapter fallback to acestep --------------------------------------------


def _install_single_file_instrumental_safetensors(home: Path, filename: str) -> Path:
    folder = home / "models" / "music" / "yue2" / "loras"
    folder.mkdir(parents=True, exist_ok=True)
    keys = [
        f"layers.{i}.{site}.lora_{ab}"
        for i in range(2)
        for site in ("self_attn.q_proj", "mlp.down_proj")
        for ab in "AB"
    ]
    path = folder / filename
    path.write_bytes(safetensors_bytes(keys, {"rank": "64", "intended_cot": "full"}))
    return path


@pytest.mark.asyncio
async def test_2_safetensors_yue2_instrumental_adapter_falls_back_cleanly_to_acestep(tmp_path, monkeypatch):
    adapter_name = "my-instrumental.safetensors"
    yue2_env(monkeypatch, "skip_instrumental")
    home = tmp_path / "dpc"
    install_both(home)
    _install_single_file_instrumental_safetensors(home, adapter_name)

    async with DualRig(home, yue2_settings={"yue2_instrumental_adapter": adapter_name}) as rig:
        info = rig.yue2.form_info()
        purpose = info["purpose_adapters"]["instrumental"]
        assert purpose["offered"] is True and purpose["adapter"] == adapter_name

        job = rig.propose(yue2_params(lyrics=None, style="ambient synthwave", variants=1))
        assert job["params"]["engine"] == "yue2"
        assert job["engine_choice"]["adapters"] == [adapter_name]
        assert job["engine_choice"]["fallback"] == "acestep"

        # Fallback matcher should also accept split filenames even if caller provides only the split error text.
        split_only_err = MusicEngineError("invalid_adapter", "did not load my-instrumental-music.safetensors")
        assert MusicJobRunner._fall_back_why(job, split_only_err, "did not load") is not None

        done = await rig.wait_for(job["id"], ("review", "failed", "unsupported"))
        assert done["state"] == js.REVIEW, done.get("state_reason")
        assert done["params"]["engine"] == "acestep"
        choice = done["engine_choice"]
        assert choice["engine"] == "acestep"
        assert choice["fell_back"]["from"] == "yue2"
        assert choice["fell_back"]["adapters"] == [adapter_name]
        assert [t["engine"] for t in rig.store.list_takes(job["id"])] == ["acestep.cpp"]


# -- 3: remove_region with cut_with_shift=True over whole <= 30s track ----------------------------


@pytest.mark.asyncio
async def test_3_remove_region_cut_with_shift_whole_track_is_refused_without_crashing_or_zero_s_flac(
    tmp_path, monkeypatch
):
    free_env(monkeypatch)
    install_fake_engine(tmp_path)

    # Proposal/normalization rejection for exact end and 4-decimal near-end rounding
    for region in ({"t_start": 0.0, "t_end": 6.0}, {"t_start": 0.0, "t_end": 5.9999}):
        with pytest.raises(re_.EditError) as err:
            re_.normalize_params_edit("remove_region", {"cut_with_shift": True}, region, duration_s=6.0)
        assert "whole_track_removal" in err.value.message

    # Direct DSP remove() guard
    pcm = re_.Pcm(data=re_.read_pcm(write_base(tmp_path / "direct.wav", seconds=6.0)).data, rate=re_.RATE)
    with pytest.raises(re_.EditError) as direct_err:
        re_.remove(pcm, (0.0, 6.0), re_.CROSSFADE_MS_DEFAULT, cut_with_shift=True)
    assert "whole_track_removal" in direct_err.value.message

    # Store-level creation rejection AND runner runtime guard (for a pre-existing queued job on disk)
    async with EditRig(tmp_path) as rig:
        entry, _base = await rig.base(seconds=6.0)
        with pytest.raises(MusicJobError) as prop_err:
            rig.propose(entry, "remove_region", {"t_start": 0.0, "t_end": 6.0}, {"cut_with_shift": True})
        assert prop_err.value.code == "invalid_params"
        assert "whole_track_removal" in prop_err.value.message

        # Plant a pre-fix queued cut_with_shift whole-track job directly via an existing valid job.json
        valid_job = rig.store.create_job(
            "edit",
            {"cut_with_shift": True},
            track_id=entry["id"],
            base_version_id="ver_" + "0" * 16,
            comment_id="cmt_" + "a" * 16,
            action="remove_region",
            region={"t_start": 4.0, "t_end": 6.0},
            prompt="cut tail",
            source={"kind": "local", "content_hash": entry["content_hash"], "duration_s": 6.0},
        )
        job_file = rig.store.job_path(valid_job["id"])
        on_disk = json.loads(job_file.read_text(encoding="utf-8"))
        on_disk["job"]["region"] = {"t_start": 0.0, "t_end": 6.0}
        job_file.write_text(json.dumps(on_disk), encoding="utf-8")

        reopened = MusicJobStore(tmp_path, capabilities=rig.store.capabilities)
        reopened.open()
        rig.store = reopened
        rig.runner.store = reopened
        rig.runner.wake()

        done = await rig.wait_for(valid_job["id"], DONE)
        assert done["state"] in (js.UNSUPPORTED, js.FAILED)
        assert "whole_track_removal" in (done.get("state_reason") or "")
        assert "internal error" not in (done.get("state_reason") or "").lower()
        assert rig.store.list_takes(valid_job["id"]) == []
        gen_dir = rig.runner.generated_root / valid_job["id"]
        flacs = list(gen_dir.glob("*.flac")) if gen_dir.exists() else []
        assert flacs == []
        assert [e["id"] for e in rig.library.list_entries()] == [entry["id"]]


# -- 4: interrupted _edit_prepare misses SYNTH_PAYLOAD on resume ----------------------------------


@pytest.mark.asyncio
async def test_4_interrupted_edit_prepare_writes_missing_synth_payload_on_resume(tmp_path, monkeypatch):
    free_env(monkeypatch, record=tmp_path / "posts")
    install_fake_engine(tmp_path)

    async with EditRig(tmp_path) as rig:
        entry, _base = await rig.base(seconds=6.0)
        # Pause runner loop while constructing the interrupted preparation state cleanly.
        await rig.runner.stop()
        job = rig.store.create_job(
            "edit",
            {"caption": "synthwave, warm pads", "lyrics": "[verse]\nnight drive"},
            track_id=entry["id"],
            base_version_id="ver_" + "0" * 16,
            comment_id="cmt_" + "b" * 16,
            action="regenerate_region",
            region={"t_start": 2.0, "t_end": 4.0},
            prompt="fix the synth",
            source={"kind": "local", "content_hash": entry["content_hash"], "duration_s": 6.0},
        )
        out_dir = rig.runner.generated_root / job["id"]
        control = out_dir / "control.wav.part"

        real_write = rig.store.write_payload

        def crash_before_synth(job_id, name, body, **kw):
            if name == SYNTH_PAYLOAD:
                raise OSError("simulated crash before writing synth.request.json")
            return real_write(job_id, name, body, **kw)

        monkeypatch.setattr(rig.store, "write_payload", crash_before_synth)
        with pytest.raises(OSError, match="simulated crash"):
            await rig.runner._edit_prepare(rig.store.get_job(job["id"]), out_dir, control)
        monkeypatch.undo()

        mid = rig.store.get_job(job["id"])
        assert [p["name"] for p in mid["payloads"]] == [EDIT_PAYLOAD]
        edit_req = json.loads(rig.store.read_payload(job["id"], EDIT_PAYLOAD))

        # Resume the interrupted job through the runner.
        rig.runner.start()
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")
        names = [p["name"] for p in done["payloads"]]
        assert EDIT_PAYLOAD in names and SYNTH_PAYLOAD in names
        synth_req = json.loads(rig.store.read_payload(job["id"], SYNTH_PAYLOAD))
        assert synth_req["seed"] == edit_req["seed"]
        assert len(rig.store.list_takes(job["id"])) == 2


# -- 5: _derived_key and idempotency matching include edit dimensions -----------------------------


@pytest.mark.asyncio
async def test_5_derived_key_and_idempotency_separate_distinct_edits_and_dedupe_retries(tmp_path, monkeypatch):
    def store_many(sub: str) -> MusicJobStore:
        s = MusicJobStore(tmp_path / sub, capabilities=MusicCapabilities(installed_probe, ffmpeg_probe=lambda: True))
        s.open()
        return s

    s = store_many("store")
    tid = "trk_" + "1" * 16
    src = {"kind": "local", "content_hash": "md5:" + "a" * 32, "duration_s": 60.0}
    agent = {"kind": "agent", "agent_id": "agent_1", "conversation_id": "chat_1", "turn_id": "turn_1"}

    # Two actions with identical normalized params (explicit allowed_zone >= 2 s so fix_seam matches regenerate_region).
    j_region1 = s.create_job(
        "edit",
        {},
        track_id=tid,
        base_version_id="ver_" + "0" * 16,
        comment_id="cmt_" + "0" * 16,
        action="remove_region",
        region={"t_start": 1.0, "t_end": 2.0},
        prompt="remove a",
        source=src,
        requested_by=agent,
        turn_jobs_max=10,
        agent_queue_max=10,
    )
    j_region2 = s.create_job(
        "edit",
        {},
        track_id=tid,
        base_version_id="ver_" + "0" * 16,
        comment_id="cmt_" + "0" * 16,
        action="remove_region",
        region={"t_start": 3.0, "t_end": 4.0},
        prompt="remove b",
        source=src,
        requested_by=agent,
        turn_jobs_max=10,
        agent_queue_max=10,
    )
    j_regen = s.create_job(
        "edit",
        {"caption": "rock", "allowed_zone": {"t_start": 10.0, "t_end": 12.0}},
        track_id=tid,
        base_version_id="ver_" + "0" * 16,
        comment_id="cmt_" + "0" * 16,
        action="regenerate_region",
        region={"t_start": 10.0, "t_end": 12.0},
        prompt="regen",
        source=src,
        requested_by=agent,
        turn_jobs_max=10,
        agent_queue_max=10,
    )
    j_seam = s.create_job(
        "edit",
        {"caption": "rock", "allowed_zone": {"t_start": 10.0, "t_end": 12.0}},
        track_id=tid,
        base_version_id="ver_" + "0" * 16,
        comment_id="cmt_" + "0" * 16,
        action="fix_seam",
        region={"t_start": 10.0, "t_end": 12.0},
        prompt="fix seam",
        source=src,
        requested_by=agent,
        turn_jobs_max=10,
        agent_queue_max=10,
    )
    j_ver = s.create_job(
        "edit",
        {},
        track_id=tid,
        base_version_id="ver_" + "1" * 16,
        comment_id="cmt_" + "0" * 16,
        action="remove_region",
        region={"t_start": 1.0, "t_end": 2.0},
        prompt="remove ver 1",
        source=src,
        requested_by=agent,
        turn_jobs_max=10,
        agent_queue_max=10,
    )
    j_cmt = s.create_job(
        "edit",
        {},
        track_id=tid,
        base_version_id="ver_" + "0" * 16,
        comment_id="cmt_" + "1" * 16,
        action="remove_region",
        region={"t_start": 1.0, "t_end": 2.0},
        prompt="remove cmt 1",
        source=src,
        requested_by=agent,
        turn_jobs_max=10,
        agent_queue_max=10,
    )
    j_other_chat = s.create_job(
        "edit",
        {},
        track_id=tid,
        base_version_id="ver_" + "0" * 16,
        comment_id="cmt_" + "0" * 16,
        action="remove_region",
        region={"t_start": 1.0, "t_end": 2.0},
        prompt="other chat",
        source=src,
        requested_by={**agent, "conversation_id": "chat_2"},
        turn_jobs_max=10,
        agent_queue_max=10,
    )

    ids = [j["id"] for j in (j_region1, j_region2, j_regen, j_seam, j_ver, j_cmt, j_other_chat)]
    assert len(set(ids)) == len(ids), f"distinct edit proposals merged into same job: {ids}"

    # Genuine retry (even with int vs float formatting in region) still deduplicates even under turn_jobs_max=1.
    retry = s.create_job(
        "edit",
        {},
        track_id=tid,
        base_version_id="ver_" + "0" * 16,
        comment_id="cmt_" + "0" * 16,
        action="remove_region",
        region={"t_start": 1, "t_end": 2},
        prompt="remove a",
        source=src,
        requested_by=agent,
        turn_jobs_max=1,
        agent_queue_max=10,
    )
    assert retry["id"] == j_region1["id"]

    # Explicit idempotency_key conflict check across all four edit axes.
    explicit = s.create_job(
        "edit",
        {"caption": "rock", "allowed_zone": {"t_start": 10.0, "t_end": 12.0}},
        track_id=tid,
        base_version_id="ver_" + "0" * 16,
        comment_id="cmt_" + "9" * 16,
        action="regenerate_region",
        region={"t_start": 10.0, "t_end": 12.0},
        prompt="explicit key",
        idempotency_key="edit-key-1",
        source=src,
    )
    for delta in (
        {"action": "fix_seam"},
        {"region": {"t_start": 10.0, "t_end": 11.5}},
        {"base_version_id": "ver_" + "2" * 16},
        {"comment_id": "cmt_" + "8" * 16},
    ):
        kw = dict(
            track_id=tid,
            base_version_id="ver_" + "0" * 16,
            comment_id="cmt_" + "9" * 16,
            action="regenerate_region",
            region={"t_start": 10.0, "t_end": 12.0},
            prompt="explicit key",
            idempotency_key="edit-key-1",
            source=src,
        )
        kw.update(delta)
        with pytest.raises(MusicJobError) as err:
            s.create_job("edit", {"caption": "rock", "allowed_zone": {"t_start": 10.0, "t_end": 12.0}}, **kw)
        assert err.value.code == "idempotency_conflict", delta

    same_explicit = s.create_job(
        "edit",
        {"caption": "rock", "allowed_zone": {"t_start": 10.0, "t_end": 12.0}},
        track_id=tid,
        base_version_id="ver_" + "0" * 16,
        comment_id="cmt_" + "9" * 16,
        action="regenerate_region",
        region={"t_start": 10.0, "t_end": 12.0},
        prompt="explicit key",
        idempotency_key="edit-key-1",
        source=src,
    )
    assert same_explicit["id"] == explicit["id"]

    # Command door retry without comment_id also deduplicates cleanly while rejecting distinct regions in 1-job turn.
    without_tools(monkeypatch)
    service = service_with_engine(tmp_path / "cmd_dpc")
    service._music_jobs_instance = store_many("cmd_dpc")
    try:
        tracks = await library_tracks(service, tmp_path / "cmd_songs", names=("song.mp3",))
        lib = await service._music()
        lib._entries[tracks["song.mp3"]]["duration_s"] = 60.0
        a1 = await service.propose_music_job(
            type="edit", params={}, track_id=tracks["song.mp3"], action="remove_region",
            region={"t_start": 5, "t_end": 8}, prompt="first region", requested_by=agent,
        )
        assert a1["status"] == "ok", a1
        a1_retry = await service.propose_music_job(
            type="edit", params={}, track_id=tracks["song.mp3"], action="remove_region",
            region={"t_start": 5.0, "t_end": 8.0}, prompt="first region", requested_by=agent,
        )
        assert a1_retry["status"] == "ok" and a1_retry["job"]["id"] == a1["job"]["id"]
        a2_diff = await service.propose_music_job(
            type="edit", params={}, track_id=tracks["song.mp3"], action="remove_region",
            region={"t_start": 10, "t_end": 12}, prompt="other region", requested_by=agent,
        )
        assert a2_diff["status"] == "error" and a2_diff["code"] == "turn_job_limit"
    finally:
        await service.stop_music_library()


# -- 6: pre-ceiling job.json accepted on open() so runner emits context_exceeded -------------------


@pytest.mark.asyncio
async def test_6_open_loads_pre_ceiling_yue2_jobs_so_runner_can_emit_context_exceeded_without_rejecting(
    tmp_path, monkeypatch
):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)

    # Plant: (a) a pre-ceiling queued YuE2 generate_from_text job, (b) a historical pre-ceiling review job,
    # and (c) a genuinely malformed job that must still be quarantined to .rejected-*.
    store = MusicJobStore(home, capabilities=MusicCapabilities(installed_probe))
    store.open()
    queued_base = store.create_job("generate_from_text", yue2_params())
    hist_job, _ = job_in_review(store)
    bad_base = store.create_job("generate_from_text", yue2_params(style="bad job"))

    q_path = store.job_path(queued_base["id"])
    q_doc = json.loads(q_path.read_text(encoding="utf-8"))
    q_doc["job"]["params"]["lyrics"] = "a" * 60_000
    q_doc["job"]["prompt"] = "pop"
    q_doc["job"]["params"]["style"] = "pop"
    q_path.write_text(json.dumps(q_doc), encoding="utf-8")

    # Historical review job created on yue2 before ceiling existed.
    h_path = store.job_path(hist_job["id"])
    h_doc = json.loads(h_path.read_text(encoding="utf-8"))
    h_doc["job"]["params"] = {
        "lyrics": "b" * 60_000,
        "style": "historic",
        "variants": 2,
        "duration_s": 180,
        "seed": None,
        "lyrics_prep": {},
        "engine": "yue2",
        "backbone": "stock",
        "cot": "full",
        "adapters": [],
    }
    h_path.write_text(json.dumps(h_doc), encoding="utf-8")

    # Genuinely malformed job.
    b_path = store.job_path(bad_base["id"])
    b_doc = json.loads(b_path.read_text(encoding="utf-8"))
    b_doc["job"]["state"] = "waiting"
    b_path.write_text(json.dumps(b_doc), encoding="utf-8")

    # Re-open through DualRig (calls MusicJobStore.open())
    async with DualRig(home, start_runner=False) as rig:
        assert not list(q_path.parent.glob("job.json.rejected-*"))
        assert not list(h_path.parent.glob("job.json.rejected-*"))
        assert len(list(b_path.parent.glob("job.json.rejected-*"))) == 1

        loaded_q = rig.store.get_job(queued_base["id"])
        assert loaded_q["state"] == js.QUEUED
        assert rig.store.get_job(hist_job["id"])["state"] == js.REVIEW
        with pytest.raises(MusicJobError) as missing_bad:
            rig.store.get_job(bad_base["id"])
        assert missing_bad.value.code == "not_found"

        # New proposals above the context ceiling are still rejected at the door.
        with pytest.raises(MusicJobError) as door_err:
            rig.store.create_job("generate_from_text", from_text_params(lyrics="a" * 60_000))
        assert door_err.value.code == "context_exceeded"

        # Runner handles the loaded queued pre-ceiling job cleanly and emits context_exceeded.
        await rig.runner.run_job(queued_base["id"])
        refused = rig.store.get_job(queued_base["id"])
        assert refused["state"] == js.UNSUPPORTED
        assert (refused.get("state_reason") or "").startswith("context_exceeded:")
