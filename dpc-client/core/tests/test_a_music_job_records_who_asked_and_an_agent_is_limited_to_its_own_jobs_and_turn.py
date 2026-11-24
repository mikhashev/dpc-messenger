"""Music tools part 1: a job records who proposed it (`requested_by`), an agent is limited per
requester and per turn, cancels only its own jobs, a stopped turn's queued jobs can be cancelled
at once, and a take resolves by id without the job's request. Every test runs on a real
`MusicJobStore` in a temporary DPC home (and the real command mixin for the door)."""

import json

import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music.jobs import MusicJobError, MusicJobStore
from tests._music_job_commands_helpers import Service
from tests._music_jobs_helpers import (
    LM_BODY, from_text_params, installed_probe, queued_job, running_job, store_at,
)
from dpc_client_core.music.capabilities import MusicCapabilities


def agent(agent_id="agent-a", turn_id=None, conversation_id=None):
    rb = {"kind": "agent", "agent_id": agent_id}
    if turn_id is not None:
        rb["turn_id"] = turn_id
    if conversation_id is not None:
        rb["conversation_id"] = conversation_id
    return rb


def propose(store, who, style="indie rock", **kw):
    return store.create_job("generate_from_text", from_text_params(style=style), requested_by=who, **kw)


def reopen(home):
    return store_at(home)


# -- (a) the field survives a reopen --------------------------------------------------


def test_requested_by_survives_closing_and_reopening_the_store(tmp_path):
    store = store_at(tmp_path)
    who = agent("agent-a", turn_id="turn-1", conversation_id="conv-9")
    job = propose(store, who)
    ui_job = queued_job(store, style="from the ui")
    assert job["requested_by"] == who
    assert ui_job["requested_by"] == {"kind": "person"}

    again = reopen(tmp_path)
    assert again.get_job(job["id"])["requested_by"] == who
    assert again.get_job(ui_job["id"])["requested_by"] == {"kind": "person"}
    on_disk = json.loads(again.job_path(job["id"]).read_text(encoding="utf-8"))
    assert on_disk["job"]["requested_by"] == who


def test_a_malformed_requested_by_is_refused_and_writes_nothing(tmp_path):
    store = store_at(tmp_path)
    for bad in ({"kind": "robot"}, {"kind": "agent"}, {"kind": "agent", "agent_id": "a", "x": 1},
                {"kind": "person", "agent_id": "a"}, {"kind": "agent", "agent_id": ""}, "agent"):
        with pytest.raises(MusicJobError) as err:
            propose(store, bad)
        assert err.value.code == "invalid_params" and err.value.field == "requested_by"
    assert store.list_jobs() == []


# -- (b) a store written before the field opens -----------------------------------------


def test_a_store_written_before_the_field_opens_and_reads_as_the_persons(tmp_path):
    store = store_at(tmp_path)
    job = queued_job(store)
    path = store.job_path(job["id"])
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["job"]["requested_by"]  # the record as it was written before the field existed
    path.write_text(json.dumps(data), encoding="utf-8")

    old = reopen(tmp_path)
    loaded = old.get_job(job["id"])
    assert "requested_by" not in loaded
    assert js.requested_by_of(loaded) == {"kind": "person"}
    # still a live job: an agent cannot cancel it, the person can
    with pytest.raises(MusicJobError) as err:
        old.cancel_job(job["id"], requested_by=agent("agent-a"))
    assert err.value.code == "not_your_job"
    assert old.cancel_job(job["id"])["state"] == "cancelled"


# -- (c) the per-requester queue cap ------------------------------------------------------


def test_a_third_job_of_the_same_agent_is_refused_and_another_agent_is_not(tmp_path):
    store = store_at(tmp_path)
    propose(store, agent("agent-a", turn_id="t1"), style="one")
    propose(store, agent("agent-a", turn_id="t2"), style="two")
    with pytest.raises(MusicJobError) as err:
        propose(store, agent("agent-a", turn_id="t3"), style="three")
    assert err.value.code == "requester_queue_full"
    assert len(store.list_jobs()) == 2

    assert propose(store, agent("agent-b", turn_id="t1"), style="other agent")["state"] == "queued"
    # the person is never limited
    for i in range(4):
        assert queued_job(store, style=f"person {i}")["state"] == "queued"


def test_a_finished_or_cancelled_job_frees_the_requesters_queue(tmp_path):
    store = store_at(tmp_path)
    first = propose(store, agent("agent-a", turn_id="t1"), style="one")
    propose(store, agent("agent-a", turn_id="t2"), style="two")
    store.cancel_job(first["id"])
    assert propose(store, agent("agent-a", turn_id="t3"), style="three")["state"] == "queued"


def test_the_queue_cap_can_be_set_per_call_and_per_store(tmp_path):
    store = MusicJobStore(tmp_path, capabilities=MusicCapabilities(installed_probe), agent_queue_max=1)
    store.open()
    propose(store, agent("agent-a", turn_id="t1"), style="one")
    with pytest.raises(MusicJobError) as err:
        propose(store, agent("agent-a", turn_id="t2"), style="two")
    assert err.value.code == "requester_queue_full"
    assert propose(store, agent("agent-a", turn_id="t2"), style="two", agent_queue_max=3)["state"] == "queued"


# -- (d) the per-turn cap and the default key ---------------------------------------------


def test_a_second_call_in_the_same_turn_without_a_key_returns_the_same_job(tmp_path):
    store = store_at(tmp_path)
    who = agent("agent-a", turn_id="turn-7")
    first = propose(store, who, style="the same song")
    second = propose(store, who, style="the same song")
    assert second["id"] == first["id"]
    assert len(store.list_jobs()) == 1
    assert first["idempotency_key"].startswith("turn:agent-a:turn-7:")
    # and after a restart too: the key is on the record
    assert propose(reopen(tmp_path), who, style="the same song")["id"] == first["id"]


def test_a_different_job_in_the_same_turn_is_refused_by_the_turn_cap(tmp_path):
    store = store_at(tmp_path)
    who = agent("agent-a", turn_id="turn-7")
    propose(store, who, style="song one")
    with pytest.raises(MusicJobError) as err:
        propose(store, who, style="song two")
    assert err.value.code == "turn_job_limit"
    # a bigger cap lets it through; another turn is not affected
    assert propose(store, who, style="song two", turn_jobs_max=2)["state"] == "queued"
    store2 = store_at(tmp_path / "other")
    propose(store2, agent("agent-a", turn_id="turn-1"), style="x")
    assert propose(store2, agent("agent-a", turn_id="turn-2"), style="y")["state"] == "queued"


def test_an_agent_request_without_a_turn_gets_no_derived_key(tmp_path):
    store = store_at(tmp_path)
    a = propose(store, agent("agent-a"), style="same")
    b = propose(store, agent("agent-a"), style="same")
    assert a["id"] != b["id"] and a["idempotency_key"] is None


# -- (e) own-only cancel ---------------------------------------------------------------------


def test_an_agent_cannot_cancel_another_agents_job_but_the_person_can(tmp_path):
    store = store_at(tmp_path)
    job = propose(store, agent("agent-a", turn_id="t1"))
    with pytest.raises(MusicJobError) as err:
        store.cancel_job(job["id"], requested_by=agent("agent-b"))
    assert err.value.code == "not_your_job"
    assert store.get_job(job["id"])["state"] == "queued"

    ui_job = queued_job(store, style="ui job")
    with pytest.raises(MusicJobError) as err:
        store.cancel_job(ui_job["id"], requested_by=agent("agent-a"))
    assert err.value.code == "not_your_job"

    # the owner of the job cancels it; the person (no requested_by, or kind person) cancels any
    assert store.cancel_job(job["id"], requested_by=agent("agent-a"))["state"] == "cancelled"
    assert store.cancel_job(ui_job["id"], requested_by={"kind": "person"})["state"] == "cancelled"
    other = propose(store, agent("agent-b", turn_id="t1"), style="b's")
    assert store.cancel_job(other["id"])["state"] == "cancelled"


def test_a_cancel_by_the_wrong_agent_is_refused_even_when_the_job_is_already_cancelled(tmp_path):
    store = store_at(tmp_path)
    job = propose(store, agent("agent-a", turn_id="t1"))
    store.cancel_job(job["id"])
    with pytest.raises(MusicJobError) as err:
        store.cancel_job(job["id"], requested_by=agent("agent-b"))
    assert err.value.code == "not_your_job"


# -- (f) cancel by turn ----------------------------------------------------------------------


def test_cancel_of_a_turn_cancels_the_queued_job_and_leaves_the_running_one(tmp_path):
    store = store_at(tmp_path)
    who = agent("agent-a", turn_id="turn-1")
    running = propose(store, who, style="running one", turn_jobs_max=3)
    store.write_payload(running["id"], "lm.request.json", LM_BODY, endpoint="/lm")
    store.start_job(running["id"])
    queued = propose(store, who, style="queued one", turn_jobs_max=3)
    elsewhere = propose(store, agent("agent-a", turn_id="turn-2"), style="another turn", agent_queue_max=5)
    ui = queued_job(store, style="ui job")

    done = store.cancel_jobs_of_turn("turn-1")
    assert [j["id"] for j in done] == [queued["id"]]
    assert done[0]["state"] == "cancelled"
    assert store.get_job(running["id"])["state"] == "running"
    assert store.get_job(elsewhere["id"])["state"] == "queued"
    assert store.get_job(ui["id"])["state"] == "queued"
    assert store.cancel_jobs_of_turn("turn-1") == []  # nothing left to cancel; not an error
    with pytest.raises(MusicJobError):
        store.cancel_jobs_of_turn("  ")


# -- the command door ------------------------------------------------------------------------


class _Settings:
    def __init__(self, home, values):
        self.dpc_home_dir = home
        self.values = values

    def get(self, section, key, fallback=None):
        return self.values.get((section, key), fallback)


def _service(home, **values):
    service = Service(home)
    service.settings = _Settings(home, {("music", k): v for k, v in values.items()})
    service._music_jobs_instance = store_at(home)
    return service


@pytest.mark.asyncio
async def test_the_propose_command_applies_the_settings_limits_and_the_ui_path_is_unchanged(tmp_path):
    service = _service(tmp_path, agent_queue_max="1", turn_jobs_max="5")
    params = from_text_params(style="a")
    ui = await service.propose_music_job(type="generate_from_text", params=params)
    assert ui["status"] == "ok" and ui["job"]["requested_by"] == {"kind": "person"}

    who = agent("agent-a", turn_id="t1")
    first = await service.propose_music_job(type="generate_from_text", params=from_text_params(style="b"),
                                            requested_by=who)
    assert first["status"] == "ok" and first["job"]["requested_by"] == who
    second = await service.propose_music_job(type="generate_from_text", params=from_text_params(style="c"),
                                             requested_by=who)
    assert second["status"] == "error" and second["code"] == "requester_queue_full"
    bad = await service.propose_music_job(type="generate_from_text", params=params, requested_by="agent")
    assert bad["status"] == "error" and bad["code"] == "invalid_params"

    cancelled = await service.cancel_music_job(job_id=first["job"]["id"], requested_by=agent("agent-b"))
    assert cancelled["status"] == "error" and cancelled["code"] == "not_your_job"
    ok = await service.cancel_music_job(job_id=first["job"]["id"], requested_by=who)
    assert ok["status"] == "ok" and ok["job"]["state"] == "cancelled"
    assert (await service.cancel_music_job(job_id=ui["job"]["id"]))["job"]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_the_defaults_are_two_per_agent_and_one_per_turn_when_the_settings_are_silent(tmp_path):
    service = _service(tmp_path)
    who = agent("agent-a", turn_id="t1")
    assert (await service.propose_music_job(type="generate_from_text", params=from_text_params(style="a"),
                                            requested_by=who))["status"] == "ok"
    turn = await service.propose_music_job(type="generate_from_text", params=from_text_params(style="b"),
                                           requested_by=who)
    assert turn["code"] == "turn_job_limit"
    await service.propose_music_job(type="generate_from_text", params=from_text_params(style="b"),
                                    requested_by=agent("agent-a", turn_id="t2"))
    full = await service.propose_music_job(type="generate_from_text", params=from_text_params(style="c"),
                                           requested_by=agent("agent-a", turn_id="t3"))
    assert full["code"] == "requester_queue_full"


@pytest.mark.asyncio
async def test_a_setting_that_is_not_a_positive_integer_falls_back_to_the_default(tmp_path):
    service = _service(tmp_path, agent_queue_max="lots", turn_jobs_max="0")
    assert service._music_agent_queue_max() == 2 and service._music_turn_jobs_max() == 1


@pytest.mark.asyncio
async def test_the_turn_cancel_command_cancels_queued_and_reports_what_it_cancelled(tmp_path):
    service = _service(tmp_path, turn_jobs_max="3")
    who = agent("agent-a", turn_id="turn-1")
    a = await service.propose_music_job(type="generate_from_text", params=from_text_params(style="a"),
                                        requested_by=who)
    out = await service.cancel_music_jobs_of_turn(turn_id="turn-1")
    assert out["status"] == "ok" and [j["id"] for j in out["cancelled"]] == [a["job"]["id"]]
    assert (await service.cancel_music_jobs_of_turn(turn_id="turn-1"))["cancelled"] == []
    assert (await service.cancel_music_jobs_of_turn(turn_id=""))["code"] == "invalid_params"


# -- (g) a take by id --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_take_resolves_by_id_to_its_file_and_facts_and_carries_no_request(tmp_path):
    service = _service(tmp_path)
    store = service._music_jobs_instance
    flac = tmp_path / "music" / "generated" / "variant_1.flac"
    flac.parent.mkdir(parents=True)
    flac.write_bytes(b"fLaC fake")
    job = running_job(store, lyrics="[verse]\nSECRET LYRIC LINE\n", style="SECRET STYLE")
    take = store.add_take(
        job["id"], variant=1, file_uri=flac.as_uri(), content_hash="sha256:" + "1" * 64, title="SECRET STYLE (v1)",
        seed=42, synth_batch_size=2, synth_batch_index=0, payload="lm.request.json", sample_rate=48000,
        channels=2, duration_s=119.8,
        loudness={"lufs_i": -14.2, "lra": 5.0, "true_peak_dbtp": -1.5, "measured_by": "ffmpeg",
                  "measured_at": "2026-11-24T00:00:00Z"})

    got = await service.get_music_take(take_id=take["id"])
    assert got["status"] == "ok"
    t = got["take"]
    assert t["take_id"] == take["id"] and t["job_id"] == job["id"] and t["job_state"] == "running"
    assert t["state"] == "pending" and t["track_id"] is None
    assert t["duration_s"] == 119.8 and t["loudness"]["lufs_i"] == -14.2
    assert t["file_exists"] is True and t["path"] == str(flac)
    text = json.dumps(got)
    for leak in ("SECRET LYRIC", "SECRET STYLE", "lm.request.json", "params", "lyrics", "prompt", "payload"):
        assert leak not in text, leak

    flac.unlink()
    assert (await service.get_music_take(take_id=take["id"]))["take"]["file_exists"] is False
    missing = await service.get_music_take(take_id="tak_" + "0" * 16)
    assert missing["status"] == "error" and missing["code"] == "not_found"
    assert (await service.get_music_take(take_id=""))["code"] == "invalid_take_id"


def test_the_agent_only_commands_are_coroutines_off_the_ui_allowlist():
    import asyncio

    from dpc_client_core.local_api import ALLOWED_COMMANDS
    from dpc_client_core.music.commands import MUSIC_COMMANDS, MusicCommandsMixin

    for name in ("cancel_music_jobs_of_turn", "get_music_take", "check_music_job"):
        handler = getattr(MusicCommandsMixin, name)
        assert asyncio.iscoroutinefunction(handler) and getattr(handler, "dpc_slow", False)
        assert name not in ALLOWED_COMMANDS and name not in MUSIC_COMMANDS
    assert set(MUSIC_COMMANDS) <= ALLOWED_COMMANDS
