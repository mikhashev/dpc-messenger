"""The person's take decisions (SPEC §12 decision 35, Mike 2026-11-21). Keep marks a take
accepted. Discard needs a reason: free text, a failure class from EVOLUTION §6, or both,
never neither. "None of these" rejects every take with one reason. A take is decided once,
and a rejected take keeps its record.
"""

import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music.jobs import MusicJobError
from tests._music_jobs_helpers import add_variant, job_in_review, running_job, store_at


def test_the_failure_classes_are_evolutions_six():
    assert js.FAILURE_CLASSES == ("prep", "sel", "struct", "opt", "transport", "listen")


def test_discard_without_a_reason_is_refused(tmp_path):
    store = store_at(tmp_path)
    job, takes = job_in_review(store)
    for kwargs in ({}, {"reason_text": None, "reason_class": None}, {"reason_text": "   "}):
        with pytest.raises(MusicJobError) as err:
            store.discard_take(takes[0]["id"], **kwargs)
        assert err.value.code == "reason_required"
        assert "prep, sel, struct, opt, transport, listen" in err.value.message
    assert store.get_take(takes[0]["id"])["decision"] == "pending"


def test_discard_with_text_only_is_accepted(tmp_path):
    store = store_at(tmp_path)
    _, takes = job_in_review(store)
    take = store.discard_take(takes[0]["id"], reason_text="the vocal drowns in reverb")
    assert (take["decision"], take["reason_text"], take["reason_class"]) == ("rejected", "the vocal drowns in reverb", None)
    assert take["decided_at"]


def test_discard_with_a_class_only_is_accepted(tmp_path):
    store = store_at(tmp_path)
    _, takes = job_in_review(store)
    take = store.discard_take(takes[0]["id"], reason_class="listen")
    assert (take["decision"], take["reason_text"], take["reason_class"]) == ("rejected", None, "listen")


def test_discard_with_text_and_class_is_accepted(tmp_path):
    store = store_at(tmp_path)
    _, takes = job_in_review(store)
    take = store.discard_take(takes[0]["id"], reason_text="guidance 1.0 on SFT", reason_class="sel")
    assert (take["reason_text"], take["reason_class"]) == ("guidance 1.0 on SFT", "sel")


def test_an_unknown_class_or_an_overlong_text_is_refused(tmp_path):
    store = store_at(tmp_path)
    _, takes = job_in_review(store)
    with pytest.raises(MusicJobError) as err:
        store.discard_take(takes[0]["id"], reason_class="bad_vibes")
    assert err.value.code == "invalid_params" and err.value.field == "reason_class"
    with pytest.raises(MusicJobError) as err:
        store.discard_take(takes[0]["id"], reason_text="x" * (js.MAX_REASON_TEXT + 1))
    assert err.value.field == "reason_text"


def test_a_rejected_take_keeps_its_record_and_cannot_be_decided_again(tmp_path):
    store = store_at(tmp_path)
    job, takes = job_in_review(store)
    store.discard_take(takes[0]["id"], reason_class="struct")
    assert [t["id"] for t in store.list_takes(job["id"])] == [t["id"] for t in takes]
    for decide in (lambda: store.keep_take(takes[0]["id"]),
                   lambda: store.discard_take(takes[0]["id"], reason_class="opt")):
        with pytest.raises(MusicJobError) as err:
            decide()
        assert err.value.code == "already_decided"


def test_keep_accepts_the_job_and_the_other_take_can_still_be_discarded(tmp_path):
    store = store_at(tmp_path)
    job, takes = job_in_review(store)
    kept = store.keep_take(takes[1]["id"])
    assert kept["decision"] == "accepted" and kept["reason_text"] is None and kept["decided_at"]
    after = store.get_job(job["id"])
    assert after["state"] == "accepted" and takes[1]["id"] in after["state_reason"]
    store.discard_take(takes[0]["id"], reason_text="flat chorus")
    assert store.get_job(job["id"])["state"] == "accepted"


def test_none_of_these_rejects_every_take_with_one_reason(tmp_path):
    store = store_at(tmp_path)
    job, takes = job_in_review(store, variants=3)
    store.discard_take(takes[0]["id"], reason_class="opt")
    with pytest.raises(MusicJobError) as err:
        store.reject_all_takes(job["id"])
    assert err.value.code == "reason_required"
    after = store.reject_all_takes(job["id"], reason_text="none sound like the brief", reason_class="prep")
    assert after["state"] == "rejected"
    decided = store.list_takes(job["id"])
    assert [t["decision"] for t in decided] == ["rejected"] * 3
    assert decided[0]["reason_class"] == "opt"
    assert [(t["reason_text"], t["reason_class"]) for t in decided[1:]] == [("none sound like the brief", "prep")] * 2


def test_none_of_these_is_refused_once_a_take_is_kept(tmp_path):
    store = store_at(tmp_path)
    job, takes = job_in_review(store)
    store.keep_take(takes[0]["id"])
    with pytest.raises(MusicJobError) as err:
        store.reject_all_takes(job["id"], reason_class="sel")
    assert err.value.code == "illegal_transition"
    assert store.get_take(takes[1]["id"])["decision"] == "pending"


def test_takes_are_not_decided_before_review(tmp_path):
    store = store_at(tmp_path)
    job = running_job(store)
    take = add_variant(store, job["id"], 1)
    with pytest.raises(MusicJobError) as err:
        store.keep_take(take["id"])
    assert err.value.code == "illegal_transition"
    with pytest.raises(MusicJobError) as err:
        store.discard_take(take["id"], reason_class="sel")
    assert err.value.code == "illegal_transition"


def test_the_take_schema_refuses_a_rejection_without_a_reason_and_a_reason_without_a_rejection(tmp_path):
    store = store_at(tmp_path)
    _, takes = job_in_review(store)
    bad = dict(takes[0], decision="rejected")
    with pytest.raises(js.SchemaError):
        js.validate_take(bad)
    with pytest.raises(js.SchemaError):
        js.validate_take(dict(takes[0], reason_text="pending with a reason"))
    js.validate_take(dict(takes[0], decision="rejected", reason_class="transport"))
