"""A music job moves only along SPEC §5's state machine: every allowed transition works and
lands in the history, every other one is refused, and failed / unsupported need a reason.
"""

import itertools

import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music.jobs import MusicJobError
from tests._music_jobs_helpers import (
    LM_BODY, add_variant, job_in_review, queued_job, running_job, store_at,
)


def _states(job):
    return [h["state"] for h in job["history"]]


def test_the_transition_table_is_spec_section_five_exactly():
    assert js.TRANSITIONS == {
        "queued": {"running", "cancelled", "unsupported"},
        "running": {"review", "cancelled", "failed"},
        "review": {"accepted", "rejected"},
        "accepted": set(), "rejected": set(), "failed": set(), "cancelled": set(), "unsupported": set(),
    }
    for old, new in itertools.product(js.JOB_STATES, js.JOB_STATES):
        assert (js.describe_transition(old, new) is None) == (new in js.TRANSITIONS[old])


def test_queued_running_review_accepted(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    assert job["state"] == "queued" and _states(job) == ["queued"]
    store.write_payload(job["id"], "lm.request.json", LM_BODY)
    job = store.start_job(job["id"])
    assert job["state"] == "running"
    take = add_variant(store, job["id"], 1)
    add_variant(store, job["id"], 2)
    job = store.finish_job(job["id"])
    assert job["state"] == "review" and job["progress"] == 1
    store.keep_take(take["id"])
    job = store.get_job(job["id"])
    assert job["state"] == "accepted"
    assert _states(job) == ["queued", "running", "review", "accepted"]
    assert all(h["at"] for h in job["history"])


def test_review_becomes_rejected_when_every_take_is_discarded(tmp_path):
    store = store_at(tmp_path)
    job, takes = job_in_review(store)
    store.discard_take(takes[0]["id"], reason_class="sel")
    assert store.get_job(job["id"])["state"] == "review"
    store.discard_take(takes[1]["id"], reason_text="vocal sounds metallic")
    job = store.get_job(job["id"])
    assert job["state"] == "rejected"
    assert _states(job) == ["queued", "running", "review", "rejected"]


def test_a_queued_and_a_running_job_can_be_cancelled_and_cancel_is_idempotent(tmp_path):
    store = store_at(tmp_path)
    queued = queued_job(store)
    cancelled = store.cancel_job(queued["id"])
    assert cancelled["state"] == "cancelled" and cancelled["state_reason"]
    assert store.cancel_job(queued["id"]) == cancelled

    running = running_job(store)
    assert store.cancel_job(running["id"], "changed my mind")["state_reason"] == "changed my mind"
    assert _states(store.get_job(running["id"])) == ["queued", "running", "cancelled"]


def test_a_running_job_fails_only_with_a_reason(tmp_path):
    store = store_at(tmp_path)
    job = running_job(store)
    with pytest.raises(MusicJobError) as err:
        store.fail_job(job["id"], "  ")
    assert err.value.code == "reason_required"
    assert store.get_job(job["id"])["state"] == "running"
    failed = store.fail_job(job["id"], "engine answered HTTP 500")
    assert failed["state"] == "failed" and failed["history"][-1]["reason"] == "engine answered HTTP 500"


@pytest.mark.parametrize("final", ["accepted", "rejected", "failed", "cancelled", "unsupported"])
def test_a_final_job_refuses_every_move(tmp_path, final):
    store = store_at(tmp_path)
    if final == "unsupported":
        job = store.create_job("cover", {}, track_id="trk_0123456789abcdef", base_version_id="ver_1")
    elif final in ("accepted", "rejected"):
        job, takes = job_in_review(store, variants=1)
        if final == "accepted":
            store.keep_take(takes[0]["id"])
        else:
            store.discard_take(takes[0]["id"], reason_class="opt")
    elif final == "failed":
        job = store.fail_job(running_job(store)["id"], "boom")
    else:
        job = store.cancel_job(queued_job(store)["id"])
    assert store.get_job(job["id"])["state"] == final
    before = store.get_job(job["id"])
    for move in (lambda: store.start_job(job["id"]), lambda: store.finish_job(job["id"]),
                 lambda: store.fail_job(job["id"], "late")):
        with pytest.raises(MusicJobError) as err:
            move()
        assert err.value.code == "illegal_transition"
    if final != "cancelled":
        with pytest.raises(MusicJobError) as err:
            store.cancel_job(job["id"])
        assert err.value.code == "illegal_transition"
    assert store.get_job(job["id"]) == before


def test_skipping_a_state_is_refused(tmp_path):
    store = store_at(tmp_path)
    queued = queued_job(store)
    with pytest.raises(MusicJobError) as err:
        store.finish_job(queued["id"])
    assert err.value.code == "illegal_transition"
    with pytest.raises(MusicJobError):
        store.fail_job(queued["id"], "queued jobs do not fail, they are cancelled or unsupported")

    job, takes = job_in_review(store, variants=1)
    with pytest.raises(MusicJobError) as err:
        store.start_job(job["id"])
    assert err.value.code == "illegal_transition"
    with pytest.raises(MusicJobError) as err:
        store.cancel_job(job["id"])
    assert err.value.code == "illegal_transition"


def test_a_job_cannot_start_before_its_payload_is_written(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    with pytest.raises(MusicJobError) as err:
        store.start_job(job["id"])
    assert err.value.code == "no_payload"
    assert store.get_job(job["id"])["state"] == "queued"


def test_a_generate_job_cannot_go_to_review_without_a_take(tmp_path):
    store = store_at(tmp_path)
    job = running_job(store)
    with pytest.raises(MusicJobError) as err:
        store.finish_job(job["id"])
    assert err.value.code == "no_takes"
    assert store.get_job(job["id"])["state"] == "running"


def test_takes_arrive_only_while_running_and_once_per_variant_and_file(tmp_path):
    store = store_at(tmp_path)
    queued = queued_job(store)
    with pytest.raises(MusicJobError) as err:
        add_variant(store, queued["id"], 1)
    assert err.value.code == "illegal_transition"

    job = running_job(store)
    first = add_variant(store, job["id"], 1)
    with pytest.raises(MusicJobError) as err:
        add_variant(store, job["id"], 1)
    assert err.value.code == "duplicate_take"
    with pytest.raises(MusicJobError) as err:
        store.add_take(job["id"], variant=2, file_uri=first["file"]["uri"], content_hash=None, title="x",
                       seed=1, synth_batch_size=1, synth_batch_index=0, payload="lm.request.json")
    assert err.value.code == "duplicate_take"
    with pytest.raises(MusicJobError) as err:
        add_variant(store, job["id"], 3)
    assert err.value.field == "variant"
    assert [t["id"] for t in store.list_takes(job["id"])] == [first["id"]]


def test_a_take_carries_seed_batch_size_batch_index_and_its_payload(tmp_path):
    store = store_at(tmp_path)
    job = running_job(store, duration_s=90)
    take = add_variant(store, job["id"], 2)
    assert take["kind"] == "new_track" and take["decision"] == "pending" and take["qa"] is None
    assert (take["seed"], take["synth_batch_size"], take["synth_batch_index"]) == (42, 2, 1)
    assert take["payload"] == "lm.request.json"
    assert take["duration_requested_s"] == 90 and take["file"]["duration_s"] == 119.8
    assert take["track_id"] is None and take["source_track_id"] is None and take["region"] is None
    with pytest.raises(MusicJobError) as err:
        store.add_take(job["id"], variant=1, file_uri="file:///x.flac", content_hash=None, title="x",
                       seed=1, synth_batch_size=2, synth_batch_index=2, payload="lm.request.json")
    assert err.value.code == "invalid_params"
    with pytest.raises(MusicJobError) as err:
        store.add_take(job["id"], variant=1, file_uri="file:///x.flac", content_hash=None, title="x",
                       seed=1, synth_batch_size=1, synth_batch_index=0, payload="synth.request.json")
    assert err.value.field == "payload"


def test_waiting_for_the_card_is_a_reason_on_a_queued_job_not_a_state(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    waiting = store.set_waiting_for_card(job["id"], [
        {"alias": "qwen-27b", "engine": "llama-server", "state": "running", "port": 8090}])
    assert waiting["state"] == "queued"
    assert waiting["state_reason"] == "waiting for the card: qwen-27b holds it"
    assert waiting["card_wait"]["holders"] == [{"alias": "qwen-27b", "engine": "llama-server"}]
    assert _states(waiting) == ["queued"]
    since = waiting["card_wait"]["since"]
    two = store.set_waiting_for_card(job["id"], [{"alias": "a", "engine": "strata"}, {"alias": "b", "engine": None}])
    assert two["state_reason"] == "waiting for the card: a, b hold it"
    assert two["card_wait"]["since"] == since
    cleared = store.clear_waiting_for_card(job["id"])
    assert cleared["state_reason"] is None and cleared["card_wait"] is None

    store.set_waiting_for_card(job["id"], [{"alias": "qwen-27b", "engine": "llama-server"}])
    store.write_payload(job["id"], "lm.request.json", LM_BODY)
    started = store.start_job(job["id"])
    assert started["card_wait"] is None and started["state_reason"] is None
    with pytest.raises(MusicJobError) as err:
        store.set_waiting_for_card(job["id"], [{"alias": "x"}])
    assert err.value.code == "illegal_transition"


def test_an_unknown_job_or_take_is_not_found(tmp_path):
    store = store_at(tmp_path)
    for call in (lambda: store.get_job("job_0000000000000000"), lambda: store.cancel_job("nope"),
                 lambda: store.get_take("tak_0000000000000000"), lambda: store.keep_take(None)):
        with pytest.raises(MusicJobError) as err:
            call()
        assert err.value.code == "not_found"


def test_list_jobs_is_oldest_first_and_filters_by_state_and_type(tmp_path):
    ticks = iter(f"2026-11-21T10:00:{s:02d}.000Z" for s in range(60))
    from dpc_client_core.music.capabilities import MusicCapabilities
    from dpc_client_core.music.jobs import MusicJobStore
    from tests._music_jobs_helpers import from_text_params, installed_probe
    store = MusicJobStore(tmp_path, capabilities=MusicCapabilities(installed_probe), now=lambda: next(ticks))
    a = store.create_job("generate_from_text", from_text_params())
    b = store.create_job("generate_from_text", from_text_params())
    store.cancel_job(b["id"])
    c = store.create_job("cover", {}, track_id="trk_0123456789abcdef", base_version_id="ver_1")
    assert [j["id"] for j in store.list_jobs()] == [a["id"], b["id"], c["id"]]
    assert [j["id"] for j in store.list_jobs(states=["queued"])] == [a["id"]]
    assert [j["id"] for j in store.list_jobs(job_type="cover")] == [c["id"]]
    with pytest.raises(MusicJobError):
        store.list_jobs(states=["waiting"])
