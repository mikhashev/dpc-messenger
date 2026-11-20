"""Music jobs and takes are saved atomically under `<DPC home>/music/jobs/<job_id>/` and
load back as they were. At startup a job left `running` stops claiming to run: `failed`
with "interrupted by restart" when it has no take, `review` when variants were finished
(SPEC §4: a job fails only when no take of it is usable). A file that does not parse or
fails its schema is set aside, never deleted.
"""

import json

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music.jobs import RESTART_REASON
from tests._music_jobs_helpers import (
    add_variant, installed_probe, job_in_review, queued_job, running_job, store_at,
)


def test_jobs_takes_and_decisions_load_back_as_they_were(tmp_path):
    store = store_at(tmp_path)
    queued = queued_job(store, variants=3, duration_s=45.5, seed=7)
    cancelled = store.cancel_job(queued_job(store)["id"])
    job, takes = job_in_review(store)
    store.discard_take(takes[0]["id"], reason_text="ending cut off", reason_class="struct")
    store.keep_take(takes[1]["id"])
    unsupported = store.create_job("voice_clone", {"x": 1}, track_id="trk_0123456789abcdef", base_version_id="ver_1")

    path = tmp_path / "music" / "jobs" / job["id"] / "job.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"job", "takes"}
    js.validate_job(data["job"])
    for t in data["takes"]:
        js.validate_take(t)

    again = store_at(tmp_path)
    for j in (queued, cancelled, unsupported):
        assert again.get_job(j["id"]) == store.get_job(j["id"])
    assert again.get_job(job["id"]) == store.get_job(job["id"])
    assert again.list_takes(job["id"]) == store.list_takes(job["id"])
    rejected = again.get_take(takes[0]["id"])
    assert (rejected["decision"], rejected["reason_text"], rejected["reason_class"]) == ("rejected", "ending cut off", "struct")
    assert again.get_job(job["id"])["state"] == "accepted"


def test_a_save_leaves_no_temp_file(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    folder = tmp_path / "music" / "jobs" / job["id"]
    assert sorted(p.name for p in folder.iterdir()) == ["job.json"]


def test_a_running_job_without_takes_fails_at_restart(tmp_path):
    store = store_at(tmp_path)
    job = running_job(store)

    again = store_at(tmp_path)
    after = again.get_job(job["id"])
    assert after["state"] == "failed" and after["state_reason"] == RESTART_REASON
    assert [h["state"] for h in after["history"]] == ["queued", "running", "failed"]
    # Written to disk, not only in memory: a third start sees the same.
    assert store_at(tmp_path).get_job(job["id"])["state"] == "failed"


def test_a_running_job_with_finished_variants_goes_to_review_at_restart(tmp_path):
    store = store_at(tmp_path)
    job = running_job(store, variants=2)
    take = add_variant(store, job["id"], 1)

    again = store_at(tmp_path)
    after = again.get_job(job["id"])
    assert after["state"] == "review"
    assert after["state_reason"] == f"{RESTART_REASON} after 1 of 2 variant(s)"
    again.keep_take(take["id"])
    assert again.get_job(job["id"])["state"] == "accepted"


def test_a_queued_job_stays_queued_and_forgets_a_stale_card_holder(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    store.set_waiting_for_card(job["id"], [{"alias": "strata", "engine": "strata"}])

    after = store_at(tmp_path).get_job(job["id"])
    assert after["state"] == "queued"
    assert after["card_wait"] is None and after["state_reason"] is None
    assert [h["state"] for h in after["history"]] == ["queued"]


def test_a_job_file_that_does_not_parse_is_moved_aside(tmp_path):
    store = store_at(tmp_path)
    good = queued_job(store)
    bad = queued_job(store)
    bad_path = tmp_path / "music" / "jobs" / bad["id"] / "job.json"
    bad_path.write_text("{ half a file", encoding="utf-8")

    again = store_at(tmp_path)
    assert [j["id"] for j in again.list_jobs()] == [good["id"]]
    names = [p.name for p in bad_path.parent.iterdir()]
    assert not bad_path.exists()
    assert len([n for n in names if n.startswith("job.json.corrupt-")]) == 1


def test_a_job_file_that_fails_its_schema_is_kept_aside(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    path = tmp_path / "music" / "jobs" / job["id"] / "job.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["job"]["state"] = "waiting"
    path.write_text(json.dumps(data), encoding="utf-8")

    again = store_at(tmp_path)
    assert again.list_jobs() == []
    aside = [p for p in path.parent.iterdir() if p.name.startswith("job.json.rejected-")]
    assert len(aside) == 1 and json.loads(aside[0].read_text(encoding="utf-8")) == data


def test_a_failed_write_leaves_memory_and_disk_as_they_were(tmp_path, monkeypatch):
    from dpc_client_core.music import jobs as jobs_module
    store = store_at(tmp_path)
    job = queued_job(store)
    on_disk = (tmp_path / "music" / "jobs" / job["id"] / "job.json").read_text(encoding="utf-8")

    def refuse(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(jobs_module, "atomic_write_text", refuse)
    try:
        store.cancel_job(job["id"])
    except OSError:
        pass
    monkeypatch.undo()
    assert store.get_job(job["id"])["state"] == "queued"
    assert (tmp_path / "music" / "jobs" / job["id"] / "job.json").read_text(encoding="utf-8") == on_disk


def test_an_idempotency_key_finds_its_job_after_a_restart(tmp_path):
    store = store_at(tmp_path)
    first = store.create_job("generate_from_text", {"lyrics": None, "style": "ambient"}, idempotency_key="k-1")
    again = store_at(tmp_path, installed_probe)
    assert again.create_job("generate_from_text", {"lyrics": None, "style": "ambient"}, idempotency_key="k-1") == first
