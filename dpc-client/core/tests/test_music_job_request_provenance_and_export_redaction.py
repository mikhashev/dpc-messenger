"""Wave 2 Card 7: a music job keeps trace of the request it came from (`request` + `SpawnOrigin.message_id`),
titles takes/library entries from `request.title`, copies `requested_by` and `request` into
`mp.provenance/1`, and redacts private request/chat fields on export while keeping title/asked/changes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from dpc_client_core.dpc_agent import agent as agent_mod
from dpc_client_core.dpc_agent import loop as loop_mod
from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.tools import core as core_tools
from dpc_client_core.dpc_agent.tools import music as music_tools
from dpc_client_core.dpc_agent.tools.registry import ToolContext, ToolEntry
from dpc_client_core.music import audio_tags as at
from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import jobs as jobs_module
from dpc_client_core.music import provenance as pv
from dpc_client_core.music.export import MusicExporter, read_id3_txxx, read_opus_comments
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.jobs import MusicJobError, MusicJobStore
from dpc_client_core.music.runner import slug_of
from dpc_client_core.process_registry import SPAWN_ORIGIN, SpawnOrigin, spawn_origin
from tests._acestep_helpers import Rig, fake_flac, free_env, from_text, install_fake_engine
from tests._music_job_commands_helpers import Service as BaseService
from tests._music_jobs_helpers import from_text_params, installed_probe, store_at
from tests._yue2_helpers import DualRig, install_both, yue2_env, yue2_params
from tests.test_the_lossy_export_has_an_opus_preset_and_a_true_peak_switch import FakeFfmpeg, Peaks
from tests.test_the_mp3_export_carries_the_source_record_and_the_lyrics_and_old_files_are_re_tagged_by_a_script import (
    _tagged_flac,
)
from tests.test_the_owners_agent_makes_music_through_the_music_tools_with_approval_limits_and_the_self_wait_rule import (
    AGENT,
    BOUND,
    CHAT,
    MusicService,
    Person,
    call,
    make_ctx,
    registry_for,
)
from tests.test_the_owners_agent_proposes_a_region_edit_and_the_card_names_the_region import base_track
from tests.test_the_provenance_record_is_built_per_engine_and_refuses_private_or_oversized_content import (
    ACE_IDENTITY,
    FACTS,
    acestep,
    yue2,
)

DONE = (js.REVIEW, js.FAILED, js.CANCELLED, js.UNSUPPORTED)
NODE = "dpc-node-3f9a1c7e5b2d4a6081c3e5f7a9b1d3f5"


@pytest.fixture(autouse=True)
def _clean_approvals_and_node():
    core_tools._pending_schedule_approvals.clear()
    pv.set_node_id_source(None)
    yield
    core_tools._pending_schedule_approvals.clear()
    pv.set_node_id_source(None)


def _agent_requester(agent_id="agent-a", conv_id="group-secret-room", turn_id="turn-42"):
    return {"kind": "agent", "agent_id": agent_id, "conversation_id": conv_id, "turn_id": turn_id}


def _agent_request(**over):
    req = {
        "message_id": "msg-trigger-abc-123",
        "title": "Neon Rain",
        "text": "Please generate an upbeat synthwave track named Neon Rain (180 s).",
        "asked": {"duration_s": 180, "style": "synthwave"},
        "changes": ["duration_s: 180 -> 30 for local test run"],
    }
    req.update(over)
    return req


# -- 1. SpawnOrigin.message_id & propagation -------------------------------------------------


def test_spawn_origin_accepts_optional_message_id_with_backwards_compatible_default():
    legacy = SpawnOrigin("agent-a", "conv-1", "music_propose_job", 1, turn_id="turn-1")
    assert legacy.message_id == ""
    with_msg = SpawnOrigin("agent-a", "conv-1", "music_propose_job", 1, turn_id="turn-1", message_id="msg-xyz")
    assert with_msg.message_id == "msg-xyz"


@pytest.mark.asyncio
async def test_execute_with_timeout_and_run_llm_loop_forward_message_id_into_spawn_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_mod, "load_agent_config", lambda _n: {})
    root = tmp_path / "agents" / AGENT
    (root / "logs").mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(agent_root=root, turn_class="owner", message_source="ui", conversation_id=CHAT)
    seen = []

    def probe_tool(_ctx):
        o = SPAWN_ORIGIN.get()
        seen.append((o.agent_id, o.conversation_id, o.turn_id, o.message_id))
        return "ok"

    class _Reg:
        _ctx = ctx

        def schemas(self, **_kw):
            return [{"type": "function", "function": {"name": "probe_tool", "parameters": {}}}]

        def get_timeout(self, _name):
            return 5

        def execute(self, name, _args, ctx=None):
            assert name == "probe_tool"
            return probe_tool(ctx)

    answers = [
        ({"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "probe_tool", "arguments": "{}"}}]},
         {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}),
        ({"role": "assistant", "content": "Done.", "tool_calls": []},
         {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}),
    ]

    class _Llm:
        async def chat(self, _messages, **_kw):
            return answers.pop(0)

    text, _usage, _trace = await asyncio.wait_for(loop_mod.run_llm_loop(
        messages=[{"role": "user", "content": "hi"}],
        tools=_Reg(), llm=_Llm(), agent_root=root,
        emit_progress=lambda *a, **k: None,
        task_id="t1", conversation_id=CHAT, message_id="msg-loop-99",
    ), BOUND)
    assert text == "Done."
    assert len(seen) == 1
    assert seen[0][0] == AGENT and seen[0][1] == CHAT and seen[0][2] and seen[0][3] == "msg-loop-99"


@pytest.mark.asyncio
async def test_dpc_agent_process_passes_trigger_and_history_message_id_to_run_llm_loop(tmp_path, monkeypatch):
    root = tmp_path / "agents" / AGENT
    root.mkdir(parents=True, exist_ok=True)
    captured = []

    async def fake_loop(**kwargs):
        captured.append(kwargs.get("message_id"))
        return "ok", {"rounds": 1}, {}

    monkeypatch.setattr(agent_mod, "run_llm_loop", fake_loop)
    monkeypatch.setattr(agent_mod, "recall_off_loop", lambda *a, **k: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(agent_mod, "build_llm_messages", lambda **kw: ([{"role": "user", "content": "go"}], {}))

    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = root
    agent.memory = Memory(root)
    agent.skill_store = agent._embedding_provider = agent._firewall = agent._firewall_profile = agent._service = None
    agent.tools = MagicMock()
    agent.tools._ctx = None
    agent.llm = SimpleNamespace()
    agent.skill_reflector = MagicMock()
    agent.skill_reflector.record_outcome.return_value = []
    agent.config = SimpleNamespace(billing_model="subscription", budget_usd=None, max_rounds=1)
    agent._get_allowed_tools = lambda **kw: set()
    agent._runtime_billing_model = lambda: None
    agent._provider_facts = lambda: {}
    agent._update_usage_stats = lambda **kw: None
    agent._last_usage = {}
    agent._last_trace = {}
    agent.turn_audience = lambda *_a: ("owner", set())
    from dpc_client_core.dpc_agent.agent import asker_for_source
    agent.resolve_asker = lambda a, *_a: a if a is not None else asker_for_source("ui")

    # Explicit trigger_message_id
    await agent.process("make music", CHAT, trigger_message_id="msg-explicit-trig")
    assert captured[-1] == "msg-explicit-trig"

    # 1:1 monitor history fallback
    monitor = SimpleNamespace(get_message_history=lambda: [
        {"role": "assistant", "id": "msg-prev", "content": "ready"},
        {"role": "user", "id": "msg-hist-last", "content": "make music"},
    ])
    await agent.process("make music", CHAT, conversation_monitor=monitor)
    assert captured[-1] == "msg-hist-last"


# -- 2. Request block schema, store, commands & tool -----------------------------------------


def test_check_request_validates_clips_and_rejects_bad_shapes():
    assert js.check_request(None) is None
    assert js.check_request({}) is None

    clean_agent = js.check_request({
        "message_id": "  msg-1 ",
        "title": "  Neon   Rain  \n Song ",
        "text": "  " + ("A" * 5000) + "  ",
        "asked": {"duration_s": 180, "style": "synthwave"},
        "changes": ["  duration   180 -> 30  ", {"field": "style", "to": "synthwave"}],
    })
    assert clean_agent["message_id"] == "msg-1"
    assert clean_agent["title"] == "Neon Rain Song"
    assert len(clean_agent["text"]) <= js.MAX_REQUEST_TEXT
    assert clean_agent["text"].endswith("…")
    assert clean_agent["asked"] == {"duration_s": 180, "style": "synthwave"}
    assert clean_agent["changes"] == ["duration 180 -> 30", {"field": "style", "to": "synthwave"}]

    clean_player = js.check_request({
        "surface": "player",
        "title": "Midnight Drive",
        "form": {"style": "indie rock", "variants": 1, "duration_s": 30},
    })
    assert clean_player == {
        "surface": "player",
        "title": "Midnight Drive",
        "form": {"style": "indie rock", "variants": 1, "duration_s": 30},
    }

    for bad in (
        "not a dict",
        {"bogus": 1},
        {"asked": "not a dict"},
        {"form": ["not", "a", "dict"]},
        {"changes": "not a list"},
        {"changes": ["   "]},
        {"changes": [123]},
        {"title": 123},
        {"text": ["nope"]},
        {"message_id": ""},
        {"surface": ""},
    ):
        with pytest.raises(js.ParamsError) as err:
            js.check_request(bad)
        assert err.value.field == "request"


def test_job_store_persists_request_and_keeps_legacy_jobs_valid(tmp_path):
    store = store_at(tmp_path)
    req = _agent_request()
    with_req = store.create_job(
        "generate_from_text", from_text_params(variants=1),
        requested_by=_agent_requester(), request=req,
    )
    without_req = store.create_job("generate_from_text", from_text_params(variants=1))

    assert with_req["request"] == req
    assert "request" not in without_req

    reopened = store_at(tmp_path)
    assert reopened.get_job(with_req["id"])["request"] == req
    assert "request" not in reopened.get_job(without_req["id"])

    # Invalid request shape is refused cleanly
    with pytest.raises(MusicJobError) as err:
        store.create_job("generate_from_text", from_text_params(), request={"undeclared": 1})
    assert err.value.code == "invalid_params" and err.value.field == "request"


@pytest.mark.asyncio
async def test_commands_propose_and_check_accept_optional_request(tmp_path):
    svc = BaseService(tmp_path)
    svc._music_jobs_instance = store_at(tmp_path)
    player_req = {"surface": "player", "title": "Player Song", "form": {"style": "folk", "variants": 1}}

    checked = await svc.check_music_job(type="generate_from_text", params=from_text_params(variants=1),
                                        request=player_req)
    assert checked["status"] == "ok" and checked["repeat"] is None

    proposed = await svc.propose_music_job(type="generate_from_text", params=from_text_params(variants=1),
                                           request=player_req)
    assert proposed["status"] == "ok"
    assert proposed["job"]["request"] == player_req

    bad = await svc.propose_music_job(type="generate_from_text", params=from_text_params(), request="bad")
    assert bad["status"] == "error" and bad["code"] == "invalid_params" and bad["field"] == "request"


@pytest.mark.asyncio
async def test_music_propose_job_populates_message_id_from_spawn_origin_and_keeps_cross_chat_opaque(tmp_path, monkeypatch):
    home = tmp_path / "dpc"
    install_fake_engine(home)
    free_env(monkeypatch)
    monkeypatch.setattr(music_tools, "POLL_INTERVAL_S", 0.05)

    async with Rig(home, start_runner=False) as rig:
        person = Person(True)
        service = MusicService(home, rig, person)
        ctx = make_ctx(home, service)
        reg = registry_for(ctx)

        # Tool schema documents `request` cleanly
        propose_entry = next(s["function"] for s in reg.schemas() if s["function"]["name"] == "music_propose_job")
        props = propose_entry["parameters"]["properties"]
        assert "request" in props and "title" in props["request"]["description"]

        # Turn with trigger message_id -> auto-populates request.message_id
        tc = {"id": "c-req", "type": "function", "function": {
            "name": "music_propose_job",
            "arguments": json.dumps({
                "params": from_text(variants=1),
                "request": {
                    "title": "Electric Horizon",
                    "text": "Write an upbeat electronic song called Electric Horizon.",
                    "asked": {"duration_s": 90},
                    "changes": ["duration_s: 90 -> 30"],
                },
            }),
        }}
        res = await asyncio.wait_for(loop_mod._execute_with_timeout(
            reg, tc, ctx.agent_root / "logs", reg.get_timeout("music_propose_job"), ctx=ctx,
            agent_id=ctx.agent_root.name, session_id=CHAT, turn_id="turn-req-1",
            message_id="msg-origin-777",
        ), BOUND)
        assert res["result"].startswith("Proposed (approved by the person)."), res["result"]

        [job1] = rig.store.list_jobs()
        assert job1["request"] == {
            "message_id": "msg-origin-777",
            "title": "Electric Horizon",
            "text": "Write an upbeat electronic song called Electric Horizon.",
            "asked": {"duration_s": 90},
            "changes": ["duration_s: 90 -> 30"],
        }

        # Turn with message_id and NO explicit request arg -> still records request.message_id
        tc2 = {"id": "c-min", "type": "function", "function": {
            "name": "music_propose_job",
            "arguments": json.dumps({"params": from_text(variants=1, style="ambient minimal")}),
        }}
        res2 = await asyncio.wait_for(loop_mod._execute_with_timeout(
            reg, tc2, ctx.agent_root / "logs", reg.get_timeout("music_propose_job"), ctx=ctx,
            agent_id=ctx.agent_root.name, session_id=CHAT, turn_id="turn-req-2",
            message_id="msg-origin-888",
        ), BOUND)
        assert res2["result"].startswith("Proposed (approved by the person)."), res2["result"]
        job2 = [j for j in rig.store.list_jobs() if j["id"] != job1["id"]][0]
        assert job2["request"] == {"message_id": "msg-origin-888"}

        # Cross-chat check (P2-6): another chat / foreign turn must never see private job1 request
        from dpc_client_core.dpc_agent.agent import ASKER_HUMAN, Asker
        other_root = home / "agents" / AGENT
        foreign_ctx = ToolContext(
            agent_root=other_root, turn_class="other_node", message_source="group",
            conversation_id="group-foreign", dpc_service=service,
            asker=Asker(ASKER_HUMAN, "Friend", "dpc-node-friend", is_owner=False, verified=True),
        )
        foreign_ctx.agent_event_loop = asyncio.get_running_loop()
        bad_req_foreign = await asyncio.wait_for(loop_mod._execute_with_timeout(
            registry_for(foreign_ctx),
            {"id": "c-for", "type": "function", "function": {
                "name": "music_propose_job",
                "arguments": json.dumps({"params": from_text(variants=1), "request": {"bad_key": 1}}),
            }},
            other_root / "logs", 30, ctx=foreign_ctx,
            agent_id=AGENT, session_id="group-foreign", turn_id="turn-f1", message_id="msg-f1",
        ), BOUND)
        assert bad_req_foreign["result"] == music_tools.PRE_CARD_REFUSAL_FOR_OTHERS
        assert "Electric Horizon" not in bad_req_foreign["result"]


# -- 3 & 4. Take & library title + mp.provenance/1 inclusion (all paths) ---------------------


@pytest.mark.asyncio
async def test_acestep_agent_job_uses_request_title_and_writes_request_into_provenance(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    pv.set_node_id_source(lambda: NODE)

    async with Rig(home) as rig:
        who = _agent_requester(agent_id=AGENT, conv_id="group-studio-secret", turn_id="turn-42")
        req = _agent_request(title="Electric Horizon")
        job = rig.store.create_job(
            "generate_from_text", from_text(variants=1, style="very long style prompt that should not become the title"),
            requested_by=who, request=req,
        )
        # Baseline identical take without requested_by/request: IDs must remain identical!
        base_job = rig.store.create_job(
            "generate_from_text", from_text(variants=1, style="very long style prompt that should not become the title"),
        )
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        base_job = await rig.wait_for(base_job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        assert base_job["state"] == js.REVIEW, base_job["state_reason"]

        [take] = rig.store.list_takes(job["id"])
        assert take["title"] == "Electric Horizon"
        entry = next(e for e in rig.library.list_entries() if e["id"] == take["library_track_id"])
        assert entry["title"] == "Electric Horizon"

        flac = home / "music" / "generated" / job["id"] / "electric-horizon_v1.flac"
        assert flac.is_file()
        rec = json.loads(at.flac_read_comments(flac, max_block_bytes=1 << 20)[pv.TAG])
        assert rec["requested_by"] == who
        assert rec["request"] == req

        side = json.loads(flac.with_suffix(".json").read_text(encoding="utf-8"))
        assert side["requested_by"] == who and side["request"] == req

        [base_take] = rig.store.list_takes(base_job["id"])
        assert "Electric Horizon" not in base_take["title"]
        base_flac = next((home / "music" / "generated" / base_job["id"]).glob("*.flac"))
        base_rec = json.loads(at.flac_read_comments(base_flac, max_block_bytes=1 << 20)[pv.TAG])
        assert (rec["genome_sha256"], rec["intent_sha256"], rec["render_sha256"]) == (
            base_rec["genome_sha256"], base_rec["intent_sha256"], base_rec["render_sha256"]
        )


@pytest.mark.asyncio
async def test_multi_variant_and_cross_platform_unsafe_request_title_slugging(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)

    async with Rig(home) as rig:
        req = {"surface": "player", "title": "CON: Neon / Rain?*", "form": {"variants": 2}}
        job = rig.store.create_job(
            "generate_from_text", from_text(variants=2),
            requested_by={"kind": "person"}, request=req,
        )
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]

        takes = sorted(rig.store.list_takes(job["id"]), key=lambda t: t["variant"])
        assert [t["title"] for t in takes] == ["CON: Neon / Rain?* (v1)", "CON: Neon / Rain?* (v2)"]
        entries = {e["id"]: e for e in rig.library.list_entries()}
        assert entries[takes[0]["library_track_id"]]["title"] == "CON: Neon / Rain?* (v1)"
        assert entries[takes[1]["library_track_id"]]["title"] == "CON: Neon / Rain?* (v2)"

        expected_slug = slug_of("CON: Neon / Rain?*")
        assert (home / "music" / "generated" / job["id"] / f"{expected_slug}_v1.flac").is_file()
        assert (home / "music" / "generated" / job["id"] / f"{expected_slug}_v2.flac").is_file()


@pytest.mark.asyncio
async def test_yue2_job_carries_requested_by_request_and_request_title(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    pv.set_node_id_source(lambda: NODE)

    async with DualRig(home) as rig:
        who = _agent_requester(agent_id="agent-yue", conv_id="group-yue-priv", turn_id="turn-y1")
        req = _agent_request(title="Silver Echoes", message_id="msg-yue-1")
        job = rig.store.create_job(
            "generate_from_text", yue2_params(variants=1),
            requested_by=who, request=req,
        )
        rig.runner.wake()
        job = await rig.wait_for(job["id"], ("review", "failed"))
        assert job["state"] == "review", job["state_reason"]

        [take] = rig.store.list_takes(job["id"])
        assert take["title"] == "Silver Echoes"
        entry = next(e for e in rig.library.list_entries() if e["id"] == take["library_track_id"])
        assert entry["title"] == "Silver Echoes"

        flac = home / "music" / "generated" / job["id"] / "silver-echoes_v1.flac"
        assert flac.is_file()
        rec = json.loads(at.flac_read_comments(flac, max_block_bytes=1 << 20)[pv.TAG])
        assert rec["engine"] == "yue2.cpp"
        assert rec["requested_by"] == who and rec["request"] == req


@pytest.mark.asyncio
async def test_song_cover_and_edit_jobs_honour_request_title_and_provenance(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)

    async with Rig(home) as rig:
        from tests.test_a_region_edit_runs_through_the_runner_into_sibling_takes_with_a_seam_report import (
            fake_decoder,
            write_base,
        )
        base_wav = home / "songs" / "Night Drive.wav"
        base_wav.parent.mkdir(parents=True, exist_ok=True)
        write_base(base_wav, seconds=6.0)
        src_entry = await rig.library.register_generated(
            str(base_wav), job_id="job_" + "1" * 16,
            lineage={"relation": "song_from_own_text", "variant": 1}, title="Night Drive",
        )
        rig.library._entries[src_entry["id"]]["duration_s"] = 6.0
        rig.runner._decode_control = fake_decoder

        src_info = {
            "kind": "local",
            "content_hash": src_entry["content_hash"],
            "duration_s": src_entry.get("duration_s") or 4.0,
            "title": src_entry.get("title") or "Night Drive",
        }

        # Cover job from player with custom title
        cov_req = {"surface": "player", "title": "Acoustic Reimagining", "form": {"mode": "cover"}}
        cov_job = rig.store.create_job(
            "cover",
            {"caption": "acoustic folk", "variants": 1, "engine": "acestep", "lyrics_mode": "none"},
            track_id=src_entry["id"], base_version_id="ver_0", source=src_info,
            requested_by={"kind": "person"}, request=cov_req,
        )
        rig.runner.wake()
        cov_job = await rig.wait_for(cov_job["id"], DONE)
        assert cov_job["state"] == js.REVIEW, cov_job["state_reason"]
        [cov_take] = rig.store.list_takes(cov_job["id"])
        assert cov_take["title"] == "Acoustic Reimagining"
        cov_entry = next(e for e in rig.library.list_entries() if e["id"] == cov_take["library_track_id"])
        assert cov_entry["title"] == "Acoustic Reimagining"
        cov_flac = home / "music" / "generated" / cov_job["id"] / "acoustic-reimagining_v1.flac"
        assert cov_flac.is_file()
        cov_rec = json.loads(at.flac_read_comments(cov_flac, max_block_bytes=1 << 20)[pv.TAG])
        assert cov_rec["requested_by"] == {"kind": "person"} and cov_rec["request"] == cov_req

        # Edit job with agent request + title
        person = Person(True)
        svc = MusicService(home, rig, person)
        edt_req = _agent_request(title="Clean Intro Cut", message_id="msg-edit-1")
        edt_who = _agent_requester(agent_id=AGENT, conv_id="group-edit-secret", turn_id="turn-edit-1")
        cmt = await svc.add_music_comment(track_id=src_entry["id"], start_s=1.0, end_s=2.0,
                                          text="cough here", action="remove_region")
        edt_job = rig.store.create_job(
            "edit", {},
            track_id=src_entry["id"], base_version_id="ver_0", comment_id=cmt["comment"]["id"],
            action="remove_region", region={"t_start": 1.0, "t_end": 2.0}, prompt="cough here",
            source=src_info, requested_by=edt_who, request=edt_req,
        )
        rig.runner.wake()
        edt_job = await rig.wait_for(edt_job["id"], DONE)
        assert edt_job["state"] == js.REVIEW, edt_job["state_reason"]
        [edt_take] = rig.store.list_takes(edt_job["id"])
        assert edt_take["title"] == "Clean Intro Cut"
        edt_entry = next(e for e in rig.library.list_entries() if e["id"] == edt_take["library_track_id"])
        assert edt_entry["title"] == "Clean Intro Cut"
        edt_flac = home / "music" / "generated" / edt_job["id"] / "clean-intro-cut_v1.flac"
        assert edt_flac.is_file()
        edt_rec = json.loads(at.flac_read_comments(edt_flac, max_block_bytes=1 << 20)[pv.TAG])
        assert edt_rec["requested_by"] == edt_who and edt_rec["request"] == edt_req


# -- 5. Export redaction (unit + MP3 + Opus) & validate_embedded ------------------------------


def test_withhold_for_export_and_validate_embedded_protect_private_fields():
    pv.set_node_id_source(lambda: NODE)
    who = _agent_requester(agent_id="agent-studio", conv_id="group-private-chat-id", turn_id="turn-secret-9")
    req = _agent_request(
        message_id="msg-private-id-444",
        title="Neon Rain",
        text="Secret chat message sent privately in room.",
        asked={"duration_s": 180},
        changes=["duration_s: 180 -> 30"],
    )
    rec = acestep(requested_by=who, job_request=req)
    assert rec["requested_by"] == who and rec["request"] == req
    pv.validate_embedded(rec)

    exported = pv.withhold_for_export(rec)
    # Original must stay intact
    assert rec["node_id"] == NODE
    assert rec["requested_by"]["conversation_id"] == "group-private-chat-id"
    assert rec["requested_by"]["turn_id"] == "turn-secret-9"
    assert rec["request"]["message_id"] == "msg-private-id-444"
    assert rec["request"]["text"] == "Secret chat message sent privately in room."

    # Exported copy withholds node_id + conversation_id + turn_id + message_id + text
    assert exported["node_id"] == pv.NODE_ID_WITHHELD
    assert exported["requested_by"] == {
        "kind": "agent",
        "agent_id": "agent-studio",
        "conversation_id": pv.NODE_ID_WITHHELD,
        "turn_id": pv.NODE_ID_WITHHELD,
    }
    assert exported["request"] == {
        "message_id": pv.NODE_ID_WITHHELD,
        "title": "Neon Rain",
        "text": pv.NODE_ID_WITHHELD,
        "asked": {"duration_s": 180},
        "changes": ["duration_s: 180 -> 30"],
    }
    pv.validate_embedded(exported)
    pv.check_privacy(pv.compact_json(exported))

    # Rejects malformed embedded requested_by / request
    for bad in (
        {"schema": pv.SCHEMA, "engine": "acestep.cpp", "requested_by": "bad"},
        {"schema": pv.SCHEMA, "engine": "acestep.cpp", "request": ["bad"]},
        {"schema": pv.SCHEMA, "engine": "acestep.cpp", "request": {"extra_field": 1}},
    ):
        with pytest.raises(ValueError):
            pv.validate_embedded(bad)


@pytest.mark.parametrize("preset,reader", [("mp3-v0", read_id3_txxx), ("opus-160", read_opus_comments)])
def test_mp3_and_opus_exports_redact_private_request_fields_and_keep_title_asked_changes(tmp_path, preset, reader):
    pv.set_node_id_source(lambda: NODE)
    who = _agent_requester(agent_id="agent-studio", conv_id="group-secret-999", turn_id="turn-secret-555")
    req = _agent_request(
        message_id="msg-secret-111",
        title="Neon Rain",
        text="CONFIDENTIAL USER PROMPT BODY SHOULD NEVER LEAVE NODE",
        asked={"duration_s": 180, "style": "synthwave"},
        changes=["duration_s: 180 -> 30"],
    )
    rec, text = pv.finalize(acestep(requested_by=who, job_request=req))
    flac = _tagged_flac(tmp_path, text)
    sidecar = {**rec, "take_id": "tak_0123456789abcdef", "library_track_id": "trk_x", "embedded_in_file": True}

    from dpc_client_core.music.export import ExportSource
    src = ExportSource(
        track_id="trk_x", path=flac, content_hash="md5:" + "ab" * 16, title="Neon Rain",
        artist="Band", true_peak_dbtp=-2.0, true_peak_basis="test",
        take_record=sidecar, origin="generated",
    )
    exporter = MusicExporter(
        tmp_path / f"out_{preset}",
        tools=MediaTools(ffmpeg="ffmpeg", ffprobe=None, runner=FakeFfmpeg()),
        measure=Peaks(-1.5),
    )
    out_path = Path(exporter.export_track(src, preset)["path"])
    tags = reader(out_path)
    prov_raw = tags["DPC_PROVENANCE"]
    for secret in (NODE, "group-secret-999", "turn-secret-555", "msg-secret-111",
                   "CONFIDENTIAL USER PROMPT BODY SHOULD NEVER LEAVE NODE"):
        assert secret not in prov_raw

    doc = json.loads(prov_raw)
    sr = doc["source_record"]
    assert sr["node_id"] == pv.NODE_ID_WITHHELD
    assert sr["requested_by"] == {
        "kind": "agent",
        "agent_id": "agent-studio",
        "conversation_id": pv.NODE_ID_WITHHELD,
        "turn_id": pv.NODE_ID_WITHHELD,
    }
    assert sr["request"] == {
        "message_id": pv.NODE_ID_WITHHELD,
        "title": "Neon Rain",
        "text": pv.NODE_ID_WITHHELD,
        "asked": {"duration_s": 180, "style": "synthwave"},
        "changes": ["duration_s: 180 -> 30"],
    }
    pv.check_privacy(prov_raw)

    # Also verify legacy sidecar-only take export (no embedded tag) redacts `record["take"]`
    flac_plain = _tagged_flac(tmp_path / "plain")
    src_plain = ExportSource(
        track_id="trk_p", path=flac_plain, content_hash="md5:" + "cd" * 16, title="Neon Rain",
        artist="Band", true_peak_dbtp=-2.0, true_peak_basis="test",
        take_record=sidecar, origin="generated",
    )
    out_plain = Path(exporter.export_track(src_plain, preset)["path"])
    plain_raw = reader(out_plain)["DPC_PROVENANCE"]
    for secret in (NODE, "group-secret-999", "turn-secret-555", "msg-secret-111",
                   "CONFIDENTIAL USER PROMPT BODY SHOULD NEVER LEAVE NODE"):
        assert secret not in plain_raw
    plain_take = json.loads(plain_raw)["take"]
    assert plain_take["requested_by"]["conversation_id"] == pv.NODE_ID_WITHHELD
    assert plain_take["request"]["text"] == pv.NODE_ID_WITHHELD
    assert plain_take["request"]["title"] == "Neon Rain"


# -- 6. CC #51 fixes: fallback sidecar redaction, request byte cap + fit_size, message_id ----


def test_withhold_for_export_redacts_node_id_in_requested_by_and_arbitrary_schemas():
    rec = {
        "schema": "dpc.take-sidecar/1",
        "node_id": NODE,
        "requested_by": {
            "kind": "agent",
            "agent_id": "agent-studio",
            "conversation_id": "conv-secret-1",
            "turn_id": "turn-secret-1",
            "node_id": NODE,
        },
        "request": {
            "message_id": "msg-secret-1",
            "text": "Private prompt at /home/user/secret/track.wav",
            "title": "Safe Title",
            "asked": {"style": "jazz"},
            "changes": ["tempo 120 -> 110"],
            "surface": "player",
            "form": {"engine": "yue2"},
        },
    }
    exported = pv.withhold_for_export(rec)
    assert exported["node_id"] == pv.NODE_ID_WITHHELD
    assert exported["requested_by"] == {
        "kind": "agent",
        "agent_id": "agent-studio",
        "conversation_id": pv.NODE_ID_WITHHELD,
        "turn_id": pv.NODE_ID_WITHHELD,
        "node_id": pv.NODE_ID_WITHHELD,
    }
    assert exported["request"] == {
        "message_id": pv.NODE_ID_WITHHELD,
        "text": pv.NODE_ID_WITHHELD,
        "title": "Safe Title",
        "asked": {"style": "jazz"},
        "changes": ["tempo 120 -> 110"],
        "surface": "player",
        "form": {"engine": "yue2"},
    }
    # Original remains untouched
    assert rec["requested_by"]["node_id"] == NODE
    assert rec["request"]["message_id"] == "msg-secret-1"


@pytest.mark.parametrize("preset,reader", [("mp3-v0", read_id3_txxx), ("opus-160", read_opus_comments)])
def test_fallback_take_sidecar_and_unknown_schemas_are_redacted_on_export(tmp_path, preset, reader):
    from dpc_client_core.music.export import ExportSource

    fallback_sidecar = {
        "schema": "dpc.take-sidecar/1",
        "job_id": "job_0123456789abcdef",
        "take_id": "tak_0123456789abcdef",
        "variant": 1,
        "node_id": NODE,
        "requested_by": {
            "kind": "agent",
            "agent_id": "agent-studio",
            "conversation_id": "group-fallback-secret-999",
            "turn_id": "turn-fallback-secret-555",
            "node_id": NODE,
        },
        "request": {
            "message_id": "msg-fallback-secret-111",
            "title": "Fallback Song",
            "text": "CONFIDENTIAL /home/user/music/demo.wav 192.168.1.50 password: secret123",
            "asked": {"duration_s": 180, "style": "synthwave"},
            "changes": ["duration_s: 180 -> 30"],
            "surface": "player",
            "form": {"engine": "yue2"},
        },
    }
    flac_plain = _tagged_flac(tmp_path / f"plain_{preset}")
    src_plain = ExportSource(
        track_id="trk_fb", path=flac_plain, content_hash="md5:" + "ef" * 16, title="Fallback Song",
        artist="Band", true_peak_dbtp=-2.0, true_peak_basis="test",
        take_record=fallback_sidecar, origin="generated",
    )
    exporter = MusicExporter(
        tmp_path / f"out_fb_{preset}",
        tools=MediaTools(ffmpeg="ffmpeg", ffprobe=None, runner=FakeFfmpeg()),
        measure=Peaks(-1.5),
    )
    out_path = Path(exporter.export_track(src_plain, preset)["path"])
    tags = reader(out_path)
    prov_raw = tags["DPC_PROVENANCE"]
    for secret in (
        NODE, "group-fallback-secret-999", "turn-fallback-secret-555", "msg-fallback-secret-111",
        "CONFIDENTIAL", "/home/user/music/demo.wav", "192.168.1.50", "secret123",
    ):
        assert secret not in prov_raw
    pv.check_privacy(prov_raw)

    take_doc = json.loads(prov_raw)["take"]
    assert take_doc["node_id"] == pv.NODE_ID_WITHHELD
    assert take_doc["requested_by"] == {
        "kind": "agent",
        "agent_id": "agent-studio",
        "conversation_id": pv.NODE_ID_WITHHELD,
        "turn_id": pv.NODE_ID_WITHHELD,
        "node_id": pv.NODE_ID_WITHHELD,
    }
    assert take_doc["request"] == {
        "message_id": pv.NODE_ID_WITHHELD,
        "title": "Fallback Song",
        "text": pv.NODE_ID_WITHHELD,
        "asked": {"duration_s": 180, "style": "synthwave"},
        "changes": ["duration_s: 180 -> 30"],
        "surface": "player",
        "form": {"engine": "yue2"},
    }

    # Unknown/future take schema alongside a valid embedded source_record is redacted too
    _rec, text = pv.finalize(acestep(requested_by=_agent_requester(conv_id="conv-emb", turn_id="turn-emb"),
                                     job_request=_agent_request(message_id="msg-emb", text="emb prompt")))
    flac_tagged = _tagged_flac(tmp_path / f"tagged_{preset}", text)
    unknown_take = {**fallback_sidecar, "schema": "dpc.future-take/99"}
    src_unknown = ExportSource(
        track_id="trk_unk", path=flac_tagged, content_hash="md5:" + "11" * 16, title="Fallback Song",
        artist="Band", true_peak_dbtp=-2.0, true_peak_basis="test",
        take_record=unknown_take, origin="generated",
    )
    out_unk = Path(exporter.export_track(src_unknown, preset)["path"])
    unk_raw = reader(out_unk)["DPC_PROVENANCE"]
    for secret in (
        NODE, "group-fallback-secret-999", "turn-fallback-secret-555", "msg-fallback-secret-111",
        "CONFIDENTIAL", "/home/user/music/demo.wav", "192.168.1.50", "secret123",
        "conv-emb", "turn-emb", "msg-emb", "emb prompt",
    ):
        assert secret not in unk_raw
    pv.check_privacy(unk_raw)
    assert json.loads(unk_raw)["take"]["request"]["text"] == pv.NODE_ID_WITHHELD


@pytest.mark.asyncio
async def test_runner_privacy_error_fallback_sidecar_is_redacted_on_mp3_and_opus_export(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)

    async with Rig(home) as rig:
        who = _agent_requester(agent_id=AGENT, conv_id="group-priv-runner", turn_id="turn-priv-runner")
        req = {
            "message_id": "msg-priv-runner",
            "title": "Privacy Fallback Track",
            "text": "Use C:\\Users\\user\\music\\secret.wav and 192.168.1.25",
            "asked": {"style": "ambient"},
            "changes": ["capped duration to 30 s"],
        }
        job = rig.store.create_job(
            "generate_from_text", from_text(variants=1),
            requested_by=who, request=req,
        )
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]

        flac = home / "music" / "generated" / job["id"] / "privacy-fallback-track_v1.flac"
        assert flac.is_file()
        assert pv.TAG not in at.flac_read_comments(flac, max_block_bytes=1 << 20)

        from dpc_client_core.music.export import ExportSource, read_take_sidecar
        side = read_take_sidecar(flac)
        assert side is not None and side["schema"] == "dpc.take-sidecar/1"
        assert side["request"]["text"] == req["text"]

        [take] = rig.store.list_takes(job["id"])
        src = ExportSource(
            track_id=take["library_track_id"], path=flac, content_hash=take["file"]["content_hash"],
            title=take["title"], artist="Band", true_peak_dbtp=-2.0, true_peak_basis="test",
            take_record=side, origin="generated",
        )
        exporter = MusicExporter(
            tmp_path / "out_runner_fb",
            tools=MediaTools(ffmpeg="ffmpeg", ffprobe=None, runner=FakeFfmpeg()),
            measure=Peaks(-1.5),
        )
        for preset, reader in (("mp3-v0", read_id3_txxx), ("opus-160", read_opus_comments)):
            out_path = Path(exporter.export_track(src, preset)["path"])
            prov_raw = reader(out_path)["DPC_PROVENANCE"]
            for secret in (
                "group-priv-runner", "turn-priv-runner", "msg-priv-runner",
                "C:\\\\Users\\\\user", "192.168.1.25", "secret.wav",
            ):
                assert secret not in prov_raw
            pv.check_privacy(prov_raw)
            exported_take = json.loads(prov_raw)["take"]
            assert exported_take["requested_by"]["conversation_id"] == pv.NODE_ID_WITHHELD
            assert exported_take["requested_by"]["turn_id"] == pv.NODE_ID_WITHHELD
            assert exported_take["request"]["message_id"] == pv.NODE_ID_WITHHELD
            assert exported_take["request"]["text"] == pv.NODE_ID_WITHHELD
            assert exported_take["request"]["title"] == "Privacy Fallback Track"
            assert exported_take["request"]["asked"] == {"style": "ambient"}
            assert exported_take["request"]["changes"] == ["capped duration to 30 s"]


def test_check_request_enforces_total_serialized_byte_cap():
    assert hasattr(js, "MAX_REQUEST_BYTES")
    assert 12 * 1024 <= js.MAX_REQUEST_BYTES <= 16 * 1024

    # Individual fields each stay within per-field bounds, but combined JSON exceeds MAX_REQUEST_BYTES (~30 KB)
    big_changes = [{"index": i, "note": "x" * 4000} for i in range(8)]
    with pytest.raises(js.ParamsError) as err:
        js.check_request({"title": "Oversized Request", "changes": big_changes})
    assert err.value.field == "request"

    # String-only changes hitting ~29 KB (each <= MAX_REQUEST_CHANGE_LEN=500, len <= MAX_REQUEST_CHANGES=64)
    many_strings = [f"c{i:02d}-" + ("z" * 450) for i in range(64)]
    with pytest.raises(js.ParamsError) as err2:
        js.check_request({"changes": many_strings})
    assert err2.value.field == "request"

    # A request within MAX_REQUEST_BYTES still passes cleanly
    ok = js.check_request({"title": "Small", "asked": {"style": "jazz"}, "changes": ["short note"]})
    assert ok is not None and ok["title"] == "Small"


def test_fit_size_evicts_request_before_reproducibility_fields():
    # Case 1: acestep audio_codes (~52 KB) + sizable request (~14 KB) > 64 KB
    # Request must be trimmed/dropped first so audio_codes stays intact.
    codes_52k = ",".join(str(10000 + (i % 9000)) for i in range(8800))
    assert 48 * 1024 < len(codes_52k.encode("utf-8")) < 56 * 1024
    big_req = {
        "message_id": "msg-fit-1",
        "title": "Preserve Codes",
        "text": "User asked for synthwave. " + ("a" * 1800),
        "asked": {"style": "synthwave", "duration_s": 180},
        "changes": [{"step": i, "detail": "k" * 1800} for i in range(6)],
    }
    rec = acestep(
        lm_result={"caption": "synthwave", "audio_codes": codes_52k, "lyrics": "la\n"},
        job_request=big_req,
    )
    done, text = pv.finalize(rec)
    assert len(text.encode("utf-8")) <= pv.MAX_BYTES
    assert done["audio_codes"] == codes_52k
    assert "audio_codes" not in done.get("omitted_for_size", {})
    assert "request" in done.get("omitted_for_size", {})
    pv.validate_embedded(done)

    # Case 2: yue2 semantic_tokens + score_abc (~48 KB) + sizable request (~14 KB) > 64 KB
    # Request must be trimmed/dropped first so both semantic_tokens and score_abc stay intact.
    tokens_42k = ",".join(str(20000 + (i % 9000)) for i in range(7200))
    abc_8k = "X:1\nT:Test\nK:C\n" + ("CDEF GABc |\n" * 650)
    yrec = yue2(
        replay={"style": "rock", "abc": abc_8k, "semantic_tokens": tokens_42k, "seed": 42, "lm_seed": 42},
        job_request=big_req,
    )
    ydone, ytext = pv.finalize(yrec)
    assert len(ytext.encode("utf-8")) <= pv.MAX_BYTES
    assert ydone["semantic_tokens"] == tokens_42k
    assert ydone["score_abc"] == abc_8k
    assert "semantic_tokens" not in ydone.get("omitted_for_size", {})
    assert "score_abc" not in ydone.get("omitted_for_size", {})
    assert "request" in ydone.get("omitted_for_size", {})
    pv.validate_embedded(ydone)

    # Case 3: progressive trimming drops `text` before `changes`, and `changes` before `asked`
    prog_rec = acestep(job_request={
        "title": "Progressive",
        "text": "T" * 1200,
        "asked": {"style": "jazz"},
        "changes": ["c1-" + ("x" * 300), "c2-" + ("y" * 300)],
    })
    base_sz = len(pv.serialize(prog_rec).encode("utf-8"))
    # Tight budget that only needs ~1 KB shed: drops `text` (~1.2 KB) while keeping `changes`, `asked`, `title`, and `audio_codes`
    tight_rec = json.loads(json.dumps(prog_rec))
    pv.fit_size(tight_rec, max_bytes=base_sz - 600)
    assert "text" not in tight_rec["request"]
    assert tight_rec["request"]["changes"] == prog_rec["request"]["changes"]
    assert tight_rec["request"]["asked"] == {"style": "jazz"}
    assert tight_rec["audio_codes"] == prog_rec["audio_codes"]


def test_zcode109_probe23a_fit_size_evicts_request_text_while_keeping_small_fields_and_codes():
    small_fields = {
        "title": "Keep Small Fields",
        "asked": {"style": "chamber jazz", "duration_s": 90},
        "surface": "player",
        "form": {"mode": "lyrics", "preset": "standard"},
    }

    # Case A: acestep with audio_codes (~59 KB) + request.text (~7 KB) -> evicting `text` brings record <= MAX_BYTES
    # while `title`, `asked`, `surface`, `form`, and `audio_codes` all survive.
    codes_59k = ",".join(str(10000 + (i % 9000)) for i in range(9800))
    assert 57 * 1024 < len(codes_59k.encode("utf-8")) < 60 * 1024
    ace_rec = acestep(
        lm_result={"caption": "chamber jazz", "audio_codes": codes_59k, "lyrics": "la\n"},
        job_request={**small_fields, "text": "Prompt detail " + ("p" * 7000)},
    )
    assert len(pv.serialize(ace_rec).encode("utf-8")) > pv.MAX_BYTES
    ace_done, ace_text = pv.finalize(ace_rec)
    assert len(ace_text.encode("utf-8")) <= pv.MAX_BYTES
    assert "text" not in ace_done["request"]
    assert "text" in ace_done["omitted_for_size"]["request"]["dropped"]
    for key, val in small_fields.items():
        assert ace_done["request"][key] == val
    assert ace_done["audio_codes"] == codes_59k
    assert "audio_codes" not in ace_done["omitted_for_size"]
    pv.validate_embedded(ace_done)

    # Case B: yue2 with semantic_tokens + score_abc + request.text -> evicting `text` keeps small fields + tokens + score_abc
    tokens_50k = ",".join(str(20000 + (i % 9000)) for i in range(8400))
    abc_7k = "X:1\nT:Test\nK:C\n" + ("CDEF GABc |\n" * 520)
    y_rec = yue2(
        replay={"style": "chamber jazz", "abc": abc_7k, "semantic_tokens": tokens_50k, "seed": 42, "lm_seed": 42},
        job_request={**small_fields, "text": "Prompt detail " + ("q" * 7000)},
    )
    assert len(pv.serialize(y_rec).encode("utf-8")) > pv.MAX_BYTES
    y_done, y_text = pv.finalize(y_rec)
    assert len(y_text.encode("utf-8")) <= pv.MAX_BYTES
    assert "text" not in y_done["request"]
    assert "text" in y_done["omitted_for_size"]["request"]["dropped"]
    for key, val in small_fields.items():
        assert y_done["request"][key] == val
    assert y_done["semantic_tokens"] == tokens_50k
    assert y_done["score_abc"] == abc_7k
    assert "semantic_tokens" not in y_done["omitted_for_size"]
    assert "score_abc" not in y_done["omitted_for_size"]
    pv.validate_embedded(y_done)


def test_music_propose_job_schema_omits_message_id_and_marks_asked_changes_as_summaries():
    entry = next(e for e in music_tools.get_tools() if e.name == "music_propose_job")
    req_schema = entry.schema["parameters"]["properties"]["request"]
    req_props = req_schema["properties"]
    assert "message_id" not in req_props
    for field in ("asked", "changes"):
        desc = req_props[field].get("description", "").lower()
        assert "summar" in desc or "paraphras" in desc
        assert "not verbatim" in desc or "no verbatim" in desc


@pytest.mark.asyncio
async def test_music_propose_job_overwrites_or_strips_model_supplied_message_id(tmp_path, monkeypatch):
    home = tmp_path / "dpc"
    install_fake_engine(home)
    free_env(monkeypatch)
    monkeypatch.setattr(music_tools, "POLL_INTERVAL_S", 0.05)

    async with Rig(home, start_runner=False) as rig:
        person = Person(True)
        service = MusicService(home, rig, person)
        ctx = make_ctx(home, service)
        reg = registry_for(ctx)

        # Case A: SpawnOrigin has message_id -> overwrites forged model-supplied message_id
        tc_overwrite = {"id": "c-spoof-1", "type": "function", "function": {
            "name": "music_propose_job",
            "arguments": json.dumps({
                "params": from_text(variants=1),
                "request": {
                    "title": "Jazz Cut",
                    "asked": {"style": "jazz"},
                    "message_id": "forged-other-chat-msg",
                },
            }),
        }}
        res1 = await asyncio.wait_for(loop_mod._execute_with_timeout(
            reg, tc_overwrite, ctx.agent_root / "logs", reg.get_timeout("music_propose_job"), ctx=ctx,
            agent_id=ctx.agent_root.name, session_id=CHAT, turn_id="turn-spoof-1",
            message_id="real-msg-1",
        ), BOUND)
        assert res1["result"].startswith("Proposed (approved by the person)."), res1["result"]
        [job1] = rig.store.list_jobs()
        assert job1["request"]["message_id"] == "real-msg-1"
        assert job1["request"]["title"] == "Jazz Cut"
        assert job1["request"]["asked"] == {"style": "jazz"}

        # Case B: SpawnOrigin has empty message_id -> strips forged model-supplied message_id
        tc_strip = {"id": "c-spoof-2", "type": "function", "function": {
            "name": "music_propose_job",
            "arguments": json.dumps({
                "params": from_text(variants=1, style="ambient pad"),
                "request": {
                    "title": "Ambient Cut",
                    "asked": {"style": "ambient"},
                    "message_id": "forged-other-chat-msg",
                },
            }),
        }}
        res2 = await asyncio.wait_for(loop_mod._execute_with_timeout(
            reg, tc_strip, ctx.agent_root / "logs", reg.get_timeout("music_propose_job"), ctx=ctx,
            agent_id=ctx.agent_root.name, session_id=CHAT, turn_id="turn-spoof-2",
            message_id="",
        ), BOUND)
        assert res2["result"].startswith("Proposed (approved by the person)."), res2["result"]
        job2 = [j for j in rig.store.list_jobs() if j["id"] != job1["id"]][0]
        assert "message_id" not in job2["request"]
        assert job2["request"]["title"] == "Ambient Cut"
        assert job2["request"]["asked"] == {"style": "ambient"}

        # Case C: SpawnOrigin has empty message_id and model passes ONLY forged message_id -> no request stored
        rig.store.cancel_job(job1["id"])
        tc_strip_only = {"id": "c-spoof-3", "type": "function", "function": {
            "name": "music_propose_job",
            "arguments": json.dumps({
                "params": from_text(variants=1, style="minimal pulse"),
                "request": {"message_id": "forged-only"},
            }),
        }}
        res3 = await asyncio.wait_for(loop_mod._execute_with_timeout(
            reg, tc_strip_only, ctx.agent_root / "logs", reg.get_timeout("music_propose_job"), ctx=ctx,
            agent_id=ctx.agent_root.name, session_id=CHAT, turn_id="turn-spoof-3",
            message_id="",
        ), BOUND)
        assert res3["result"].startswith("Proposed (approved by the person)."), res3["result"]
        job3 = [j for j in rig.store.list_jobs() if j["id"] not in (job1["id"], job2["id"])][0]
        assert "request" not in job3
