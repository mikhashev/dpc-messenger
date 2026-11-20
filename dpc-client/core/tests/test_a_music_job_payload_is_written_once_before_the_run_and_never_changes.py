"""A job's payload is the exact bytes the engine will receive, written before they are
sent (SPEC §3 "show and store the payload"; PROVENANCE payload rule). The bytes on disk
equal the bytes given, a name is written once, and the file is still unchanged after the
job has run and the store has restarted.
"""

import hashlib

import pytest

from dpc_client_core.music.jobs import MusicJobError
from tests._music_jobs_helpers import LM_BODY, add_variant, queued_job, store_at

SYNTH_BODY = b'{"audio_codes":"1,2,3","seed":42,"synth_batch_size":2,"output_format":"wav24"}\r\n'


def test_the_payload_file_holds_exactly_the_bytes_given(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    record = store.write_payload(job["id"], "lm.request.json", LM_BODY, endpoint="/lm")
    path = tmp_path / "music" / "jobs" / job["id"] / "payload" / "lm.request.json"
    assert path.read_bytes() == LM_BODY
    assert record == {"name": "lm.request.json", "sha256": "sha256:" + hashlib.sha256(LM_BODY).hexdigest(),
                      "size_bytes": len(LM_BODY), "written_at": record["written_at"], "endpoint": "/lm"}
    assert store.get_job(job["id"])["payloads"] == [record]
    assert store.read_payload(job["id"], "lm.request.json") == LM_BODY


def test_crlf_and_utf8_bytes_are_not_translated(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    store.write_payload(job["id"], "synth.request.json", SYNTH_BODY)
    assert (tmp_path / "music" / "jobs" / job["id"] / "payload" / "synth.request.json").read_bytes() == SYNTH_BODY


def test_a_payload_name_is_written_once(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    store.write_payload(job["id"], "lm.request.json", LM_BODY)
    with pytest.raises(MusicJobError) as err:
        store.write_payload(job["id"], "lm.request.json", b"{}")
    assert err.value.code == "payload_exists"
    assert store.read_payload(job["id"], "lm.request.json") == LM_BODY
    assert len(store.get_job(job["id"])["payloads"]) == 1


def test_the_payload_is_unchanged_after_the_run_and_a_restart(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    store.write_payload(job["id"], "lm.request.json", LM_BODY, endpoint="/lm")
    store.start_job(job["id"])
    store.write_payload(job["id"], "synth.request.json", SYNTH_BODY, endpoint="/synth")
    for v in (1, 2):
        add_variant(store, job["id"], v)
    store.finish_job(job["id"])

    again = store_at(tmp_path)
    assert again.verify_payload(job["id"], "lm.request.json")
    assert again.verify_payload(job["id"], "synth.request.json")
    assert again.read_payload(job["id"], "lm.request.json") == LM_BODY
    assert [p["name"] for p in again.get_job(job["id"])["payloads"]] == ["lm.request.json", "synth.request.json"]


def test_a_payload_changed_on_disk_fails_verification(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    store.write_payload(job["id"], "lm.request.json", LM_BODY)
    path = tmp_path / "music" / "jobs" / job["id"] / "payload" / "lm.request.json"
    path.write_bytes(LM_BODY.replace(b"42", b"43"))
    assert store.verify_payload(job["id"], "lm.request.json") is False
    path.unlink()
    assert store.verify_payload(job["id"], "lm.request.json") is False


def test_no_payload_is_written_once_the_run_is_over(tmp_path):
    store = store_at(tmp_path)
    job = store.cancel_job(queued_job(store)["id"])
    with pytest.raises(MusicJobError) as err:
        store.write_payload(job["id"], "lm.request.json", LM_BODY)
    assert err.value.code == "illegal_transition"
    assert not (tmp_path / "music" / "jobs" / job["id"] / "payload").exists()


@pytest.mark.parametrize("name", ["", "../escape.json", "a/b.json", "a\\b.json", "Upper.json", ".hidden", "x" * 65])
def test_a_payload_name_cannot_leave_the_job_folder(tmp_path, name):
    store = store_at(tmp_path)
    job = queued_job(store)
    with pytest.raises(MusicJobError) as err:
        store.write_payload(job["id"], name, b"{}")
    assert err.value.field == "name"


def test_a_payload_must_be_bytes(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    with pytest.raises(MusicJobError) as err:
        store.write_payload(job["id"], "lm.request.json", '{"text": "not bytes"}')
    assert err.value.field == "body"


def test_a_payload_that_could_not_be_recorded_leaves_no_file(tmp_path, monkeypatch):
    from dpc_client_core.music import jobs as jobs_module
    store = store_at(tmp_path)
    job = queued_job(store)

    def refuse(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(jobs_module, "atomic_write_text", refuse)
    with pytest.raises(OSError):
        store.write_payload(job["id"], "lm.request.json", LM_BODY)
    monkeypatch.undo()
    assert not (tmp_path / "music" / "jobs" / job["id"] / "payload" / "lm.request.json").exists()
    assert store.get_job(job["id"])["payloads"] == []
    store.write_payload(job["id"], "lm.request.json", LM_BODY)
    assert store.verify_payload(job["id"], "lm.request.json")
