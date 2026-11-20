"""Each job command answers `{"status": "ok", ...}` or an error with `code`, `message`
and `field`; a bad payload is refused at the door with an `invalid_*` code.
"""

import pytest

from tests._music_job_commands_helpers import Service, review_job, service_with_engine
from tests._music_jobs_helpers import from_text_params


@pytest.mark.asyncio
async def test_capabilities_propose_get_list_and_cancel_round_trip(tmp_path):
    service = service_with_engine(tmp_path / "dpc")
    caps = await service.get_music_capabilities()
    assert caps["status"] == "ok" and caps["capabilities"]["schema"] == "mp.capabilities/1"

    proposed = await service.propose_music_job(type="generate_from_text", params=from_text_params(),
                                               idempotency_key="k1")
    assert proposed["status"] == "ok" and proposed["job"]["state"] == "queued"
    job_id = proposed["job"]["id"]

    assert (await service.get_music_job(job_id=job_id))["job"]["id"] == job_id
    listed = await service.list_music_jobs(states=["queued"], job_type="generate_from_text")
    assert [j["id"] for j in listed["jobs"]] == [job_id]
    assert (await service.list_music_jobs(states=["review"]))["jobs"] == []

    cancelled = await service.cancel_music_job(job_id=job_id, reason="changed my mind")
    assert cancelled["status"] == "ok" and cancelled["job"]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_keep_discard_and_reject_all_answer_take_and_job(tmp_path):
    service = service_with_engine(tmp_path / "dpc")
    store = service._music_jobs_instance
    job, takes = review_job(store, [None, None])

    assert (await service.list_music_takes(job_id=job["id"]))["takes"][0]["id"] == takes[0]["id"]
    kept = await service.keep_music_take(take_id=takes[0]["id"])
    assert kept["status"] == "ok" and kept["take"]["decision"] == "accepted" and kept["job"]["state"] == "accepted"

    discarded = await service.discard_music_take(take_id=takes[1]["id"], reason_text="muddy")
    assert discarded["status"] == "ok" and discarded["take"]["decision"] == "rejected"
    assert discarded["library_hidden"] is False

    job2, _ = review_job(store, [None, None])
    rejected = await service.reject_all_music_takes(job_id=job2["id"], reason_text="none fit")
    assert rejected["status"] == "ok" and rejected["job"]["state"] == "rejected"
    assert {t["decision"] for t in rejected["takes"]} == {"rejected"}


@pytest.mark.asyncio
async def test_a_store_error_is_mapped_to_code_message_and_field(tmp_path):
    service = service_with_engine(tmp_path / "dpc")
    for answer in (
        await service.get_music_job(job_id="job_nope"),
        await service.keep_music_take(take_id="tak_nope"),
        await service.list_music_takes(job_id="job_nope"),
        await service.cancel_music_job(job_id="job_nope"),
        await service.reject_all_music_takes(job_id="job_nope", reason_text="x"),
        await service.propose_music_job(type="no_such_type", params={}),
    ):
        assert answer["status"] == "error"
        assert answer["code"] and answer["message"] and "field" in answer


@pytest.mark.asyncio
async def test_a_payload_of_the_wrong_type_is_refused_at_the_door(tmp_path):
    service = Service(tmp_path / "dpc")
    cases = [
        (service.propose_music_job(type=5, params={}), "invalid_type"),
        (service.propose_music_job(type="generate_from_text", params="x"), "invalid_params"),
        (service.propose_music_job(type="generate_from_text", params={}, track_id=3), "invalid_params"),
        (service.get_music_job(job_id=None), "invalid_job_id"),
        (service.list_music_jobs(states="queued"), "invalid_states"),
        (service.list_music_jobs(states=[1]), "invalid_states"),
        (service.cancel_music_job(job_id=["a"]), "invalid_job_id"),
        (service.list_music_takes(job_id=""), "invalid_job_id"),
        (service.keep_music_take(take_id=1), "invalid_take_id"),
        (service.discard_music_take(take_id="t", reason_text=4), "invalid_reason"),
        (service.reject_all_music_takes(job_id=None), "invalid_job_id"),
    ]
    for coro, code in cases:
        answer = await coro
        assert answer["status"] == "error" and answer["code"] == code, (code, answer)
        assert "Traceback" not in answer["message"]
