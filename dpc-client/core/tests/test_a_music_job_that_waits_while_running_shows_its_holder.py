"""ADR-052, Confirmation test 17 (Ark #44 finding 4, Ark #60 §4; slice S4, section 3.2 "Two gates
accept `running`, not one").

A music job's engine child can end under the job's open use (it exits on its own), which lets the
card go (L8); the job's next engine start then asks for the card again while the job is `running`.
So the job store's gate takes a `running` job as well as a `queued` one: `set_waiting_for_card` on a
running job records `card_wait` and the reason "waiting for the card: X holds it", the state stays
`running`, and `clear_waiting_for_card` clears both. The record passes its schema. Every other state
still refuses (`illegal_transition`), and every move out of `queued` or `running` clears the wait. At
a restart a running job that waited is settled as before (failed, or review with takes), with no
wait left. The runner's side (it neither fails the job nor drops it as cancelled) is test 29.
"""

import json

import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music.jobs import RESTART_REASON, MusicJobError
from tests._music_jobs_helpers import queued_job, running_job, store_at

HOLDER = {"alias": "whisper:turbo", "engine": "whisper", "state": "running", "kind": "transcription"}


def _on_disk(home, job_id):
    data = json.loads((home / "music" / "jobs" / job_id / "job.json").read_text(encoding="utf-8"))
    js.validate_job(data["job"])
    return data["job"]


def test_a_running_job_records_and_clears_its_wait_and_stays_running(tmp_path):
    store = store_at(tmp_path)
    job = running_job(store)
    waiting = store.set_waiting_for_card(job["id"], [HOLDER])
    assert waiting["state"] == js.RUNNING
    assert waiting["card_wait"]["holders"] == [{"alias": "whisper:turbo", "engine": "whisper"}]
    assert waiting["state_reason"] == "waiting for the card: whisper:turbo holds it"
    assert [h["state"] for h in waiting["history"]] == ["queued", "running"], "a reason, not a state"
    assert _on_disk(tmp_path, job["id"])["card_wait"] == waiting["card_wait"]

    cleared = store.clear_waiting_for_card(job["id"])
    assert cleared["state"] == js.RUNNING and cleared["card_wait"] is None and cleared["state_reason"] is None
    assert _on_disk(tmp_path, job["id"])["card_wait"] is None


def test_finishing_failing_or_cancelling_a_running_job_ends_its_wait(tmp_path):
    store = store_at(tmp_path)
    for end in (lambda j: store.fail_job(j, "engine gone"), lambda j: store.cancel_job(j)):
        job = running_job(store)
        store.set_waiting_for_card(job["id"], [HOLDER])
        after = end(job["id"])
        assert after["card_wait"] is None
        with pytest.raises(MusicJobError) as err:
            store.set_waiting_for_card(job["id"], [HOLDER])
        assert err.value.code == "illegal_transition"


def test_the_schema_takes_a_wait_on_queued_and_running_only(tmp_path):
    store = store_at(tmp_path)
    job = running_job(store)
    record = store.set_waiting_for_card(job["id"], [HOLDER])
    js.validate_job(record)
    with pytest.raises(js.SchemaError):
        js.validate_job({**record, "state": js.FAILED, "state_reason": "x",
                         "history": record["history"] + [{"state": js.FAILED, "at": "2026-11-24T00:00:00Z",
                                                          "reason": "x"}]})


def test_a_restart_settles_a_running_job_that_waited_and_leaves_no_wait(tmp_path):
    store = store_at(tmp_path)
    job = running_job(store)
    store.set_waiting_for_card(job["id"], [HOLDER])
    after = store_at(tmp_path).get_job(job["id"])
    assert after["state"] == js.FAILED and after["state_reason"] == RESTART_REASON
    assert after["card_wait"] is None


def test_a_queued_job_still_waits_as_before(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    waiting = store.set_waiting_for_card(job["id"], [HOLDER])
    assert waiting["state"] == js.QUEUED and waiting["card_wait"]["holders"][0]["alias"] == "whisper:turbo"
