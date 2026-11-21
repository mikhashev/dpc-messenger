"""Runner hardening before the push: a job that names an engine this DPC has not set up is
refused with a reason (not left in `queued`), an intact payload left by a crash is reused and
a differing or oversized one refuses the job cleanly, file stems keep Unicode letters, and the
acestep take records the advisory length check (codes x 0.2 s, decision 29)."""

import json

import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import jobs as jobs_module
from dpc_client_core.music.jobs import MusicJobError
from dpc_client_core.music.runner import LM_PAYLOAD, build_lm_body, length_check_of, slug_of
from tests._acestep_helpers import Rig, free_env, from_text, install_fake_engine
from tests._music_jobs_helpers import LM_BODY, queued_job, store_at

DONE = (js.REVIEW, js.FAILED, js.CANCELLED, js.UNSUPPORTED)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "dpc"
    install_fake_engine(h)
    return h


def payload_file(home, job_id, name):
    return home / "music" / "jobs" / job_id / "payload" / name


# -- slug_of ------------------------------------------------------------------------------


def test_the_slug_keeps_unicode_letters_and_digits():
    assert slug_of("русский рок") == "русский-рок"
    assert slug_of("классика") == "классика"
    assert slug_of("русский рок") != slug_of("классика")
    assert slug_of("Indie Rock, warm guitars!") == "indie-rock-warm-guitars"
    assert slug_of("日本語 ポップ 80s") == "日本語-ポップ-80s"


def test_the_slug_drops_what_a_file_name_forbids_and_trailing_dots_and_spaces():
    forbidden = '<>:"/\\|?*\x00\x1f. '
    out = slug_of("a<b>c:d\"e/f\\g|h?i*j\x00k\x1fl.  ")
    assert all(ch not in out for ch in forbidden)
    assert out == "a-b-c-d-e-f-g-h-i-j-k-l"
    assert slug_of("") == "take" and slug_of("...///") == "take" and slug_of(None) == "take"


def test_a_reserved_windows_name_is_prefixed():
    assert slug_of("CON") == "take-con"
    assert slug_of("nul") == "take-nul"
    assert slug_of("COM1") == "take-com1"
    assert slug_of("console") == "console"


def test_a_very_long_caption_is_capped():
    out = slug_of("очень длинное описание стиля " * 20)
    assert 0 < len(out) <= 40 and not out.endswith("-") and not out.startswith("-")
    assert len(slug_of("x" * 500)) == 40


# -- payload adoption in the store -----------------------------------------------------------


def test_an_unrecorded_payload_file_with_the_same_bytes_is_adopted(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    path = payload_file(tmp_path, job["id"], "lm.request.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(LM_BODY)  # a crash after the atomic write, before job.json
    record = store.write_payload(job["id"], "lm.request.json", LM_BODY, endpoint="/lm")
    assert store.get_job(job["id"])["payloads"] == [record]
    assert store.verify_payload(job["id"], "lm.request.json") is True


def test_an_unrecorded_payload_file_that_differs_is_refused_and_left_alone(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    path = payload_file(tmp_path, job["id"], "lm.request.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{}")
    with pytest.raises(MusicJobError) as err:
        store.write_payload(job["id"], "lm.request.json", LM_BODY)
    assert err.value.code == "payload_exists" and "differs" in err.value.message
    assert path.read_bytes() == b"{}" and store.get_job(job["id"])["payloads"] == []


# -- the runner --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_job_naming_an_engine_that_is_not_set_up_is_refused_with_a_reason(home, monkeypatch, tmp_path):
    free_env(monkeypatch, record=tmp_path / "sent")
    async with Rig(home) as rig:
        monkeypatch.setattr(rig.runner.engines, "find", lambda key: None)
        job = rig.store.create_job("generate_from_text", from_text(variants=1))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.UNSUPPORTED
        assert "not set up" in job["state_reason"] and "acestep" in job["state_reason"]
        assert not rig.runner._skipped


@pytest.mark.asyncio
async def test_wake_forgets_what_was_skipped(home):
    async with Rig(home, start_runner=False) as rig:
        rig.runner._skipped["job_x"] = "why"
        rig.runner.wake()
        assert rig.runner._skipped == {}


@pytest.mark.asyncio
async def test_an_intact_payload_left_by_a_crash_is_reused_on_restart(home, monkeypatch, tmp_path):
    free_env(monkeypatch, record=tmp_path / "sent")
    async with Rig(home) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=1, seed=42))
        path = payload_file(home, job["id"], LM_PAYLOAD)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(build_lm_body(rig.store.get_job(job["id"])["params"], 42))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        assert any(p["name"] == LM_PAYLOAD for p in job["payloads"])


@pytest.mark.asyncio
async def test_a_payload_that_differs_from_the_one_on_disk_refuses_the_job_with_the_reason(home, monkeypatch, tmp_path):
    free_env(monkeypatch, record=tmp_path / "sent")
    async with Rig(home) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=1, seed=42))
        path = payload_file(home, job["id"], LM_PAYLOAD)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"seed": 1}')
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.UNSUPPORTED
        assert "earlier start" in job["state_reason"] and "internal error" not in job["state_reason"]


@pytest.mark.asyncio
async def test_an_oversized_payload_refuses_the_job_with_the_limit_as_the_reason(home, monkeypatch, tmp_path):
    free_env(monkeypatch, record=tmp_path / "sent")
    monkeypatch.setattr(jobs_module, "MAX_PAYLOAD_BYTES", 64)
    async with Rig(home) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=1))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.UNSUPPORTED
        assert "at most 64 bytes" in job["state_reason"] and "internal error" not in job["state_reason"]


# -- the advisory length check -----------------------------------------------------------------


def test_the_length_check_compares_the_received_length_with_the_codes_window():
    assert length_check_of(30.0, 30.2) == {"length_check": "ok", "length_delta_s": 0.2}
    bad = length_check_of(30.0, 33.0)
    assert bad["length_check"] == "mismatch" and bad["length_delta_s"] == 3.0
    assert length_check_of(30.0, 29.0)["length_check"] == "mismatch"


@pytest.mark.asyncio
async def test_an_acestep_take_and_its_sidecar_carry_the_window_and_the_check(home, monkeypatch, tmp_path):
    free_env(monkeypatch, record=tmp_path / "sent")
    async with Rig(home) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=1))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        [take] = rig.store.list_takes(job["id"])
        assert take["engine_window_s"] == 30.0  # 150 codes x 0.2 s
        assert take["length_check"] in ("ok", "mismatch")
        assert take["length_delta_s"] == pytest.approx(take["file"]["duration_s"] - 30.0, abs=0.01)
        side = json.loads(next((home / "music" / "generated" / job["id"]).glob("*.json")).read_text(encoding="utf-8"))
        assert side["engine_window_s"] == 30.0 and side["length_check"] == take["length_check"]
        assert side["length_delta_s"] == take["length_delta_s"]
