"""The finished header strip of an agent turn is stored with the agent's message.

`N rounds · M actions   median prefill … | decode …   context …   cache hit
rate …   model` was built in the UI from live progress events and attached to
the message in memory only, so a chat switch, a refresh or a restart lost it
while the tool calls beside it survived. Now the loop builds it once from the
rounds' strips (`speed_summary_of`), the message record keeps it as
`speed_summary` beside `tool_calls`, and the UI shows the stored one.

It is local and unsigned: outside the content hash, the tool-calls digest and
the chain hash, never sent to a peer, never exported in a history sync, not
kept when a peer's record carries one, and not part of what a history record
replays into the prompt.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol import commit_integrity
from dpc_protocol.commit_integrity import CommitSigner
from dpc_protocol.crypto import generate_node_id
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.dpc_agent.context import history_body, history_prefix
from dpc_client_core.dpc_agent.loop import round_progress_payload, run_llm_loop, speed_summary_of
from dpc_client_core.managers.agent_manager import DpcAgentManager
from dpc_client_core.message_handlers.group_handler import GroupTextHandler
from dpc_client_core.service import CoreService

WINDOW = 262144
RESERVE = 16384
CID = "agent_strip"
GROUP = "group-5717"
TOOL_CALLS = [{"tool": "read_file", "input": "{}", "output": "ok", "is_error": False,
               "duration_ms": 3, "round": 1}]


def _strip(round_idx, *, prompt, cached=None, prefill=None, decode=None, model="qwen3"):
    speed = {"model": model, "prefill_tok_s": prefill, "decode_tok_s": decode} if prefill else None
    return round_progress_payload(speed, round_idx=round_idx, prompt_tokens=prompt,
                                  context_window=WINDOW, context_reserve=RESERVE,
                                  cached_tokens=cached)


SUMMARY = speed_summary_of([
    _strip(1, prompt=10000, cached=0, prefill=800.0, decode=40.0),
    _strip(2, prompt=20000, cached=18000, prefill=900.0, decode=42.0),
    _strip(3, prompt=30000, cached=29700, prefill=1000.0, decode=44.0),
])


# --- one definition of the summary -----------------------------------------

class TestTheSummaryOfATurn:

    def test_speeds_are_the_median_over_the_rounds(self):
        assert SUMMARY["prefill_tok_s"] == 900.0
        assert SUMMARY["decode_tok_s"] == 42.0
        assert SUMMARY["total_tok_s"] is None
        assert SUMMARY["median"] is True

    def test_an_even_count_takes_the_upper_median_as_the_header_did(self):
        s = speed_summary_of([_strip(i, prompt=100, prefill=v, decode=v)
                              for i, v in enumerate((10.0, 20.0, 30.0, 40.0), 1)])
        assert s["prefill_tok_s"] == 30.0

    def test_occupancy_is_the_last_rounds(self):
        assert (SUMMARY["context_used"], SUMMARY["context_window"], SUMMARY["context_reserve"]) \
            == (30000, WINDOW, RESERVE)
        assert (SUMMARY["round"], SUMMARY["rounds"], SUMMARY["model"]) == (3, 3, "qwen3")

    def test_the_cache_is_summed_so_the_rate_is_token_weighted(self):
        """A median of the percentages (0, 90, 99) would say 90; the turn reused 80%."""
        assert (SUMMARY["cached_tokens"], SUMMARY["prompt_tokens"]) == (47700, 60000)

    def test_a_round_that_stated_no_split_is_left_out_of_the_sums(self):
        s = speed_summary_of([_strip(1, prompt=5000), _strip(2, prompt=1000, cached=900)])
        assert (s["cached_tokens"], s["prompt_tokens"]) == (900, 1000)
        assert s["context_used"] == 1000

    def test_no_round_stated_a_split_leaves_both_none(self):
        s = speed_summary_of([_strip(1, prompt=5000)])
        assert s["cached_tokens"] is None and s["prompt_tokens"] is None

    def test_a_round_counts_once_although_its_strip_rides_several_events(self):
        one, two = _strip(1, prompt=1000, cached=0, prefill=500.0, decode=10.0), \
            _strip(2, prompt=1000, cached=900, prefill=700.0, decode=30.0)
        s = speed_summary_of([one, one, two, two, two])
        assert s["rounds"] == 2
        assert (s["cached_tokens"], s["prompt_tokens"]) == (900, 2000)
        assert s["prefill_tok_s"] == 700.0  # upper median of two rounds, not of five events

    def test_no_strip_no_summary(self):
        assert speed_summary_of([]) is None
        assert speed_summary_of([None, None]) is None


# --- the loop hands it over, a Kill included -------------------------------

class _Llm:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.started_hanging = asyncio.Event()

    async def chat(self, messages, **kwargs):
        step = self.steps.pop(0)
        if step == "hang":
            self.started_hanging.set()
            await asyncio.sleep(60)
        return step


class _Tools:
    _ctx = None

    def schemas(self, core_only=False, include_restricted=False):
        return [{"name": "probe"}]

    def get_timeout(self, name):
        return 5

    def execute(self, name, args, ctx=None):
        return "probed"


def _usage(prompt, cached):
    return {"prompt_tokens": prompt, "completion_tokens": 10, "total_tokens": prompt + 10,
            "cached_tokens": cached, "prefilled_tokens": prompt - cached, "cost": 0.0}


_ASKS = ({"role": "assistant", "content": "", "tool_calls": [
    {"id": "c1", "type": "function", "function": {"name": "probe", "arguments": "{}"}}]},
    _usage(1000, 0))
_SAYS = ({"role": "assistant", "content": "done", "tool_calls": []}, _usage(1500, 1000))


@pytest.fixture
def _no_agent_config(monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})


def _loop(llm, tmp_path, kill=None):
    return run_llm_loop(
        messages=[{"role": "user", "content": "hi"}], tools=_Tools(), llm=llm,
        agent_root=tmp_path, emit_progress=lambda *a, **k: None, task_id="t1",
        context_window=WINDOW, context_reserve=RESERVE, kill_event=kill,
    )


@pytest.mark.asyncio
async def test_the_loop_puts_the_turns_summary_in_its_trace(tmp_path, _no_agent_config):
    text, _usage_, trace = await _loop(_Llm(_ASKS, _SAYS), tmp_path)
    assert text == "done"
    s = trace["speed_summary"]
    assert (s["rounds"], s["round"]) == (2, 2)
    assert (s["cached_tokens"], s["prompt_tokens"]) == (1000, 2500)
    assert s["context_used"] == 1500


@pytest.mark.asyncio
async def test_a_killed_turn_keeps_the_summary_of_the_rounds_that_ran(tmp_path, _no_agent_config):
    llm, kill = _Llm(_ASKS, "hang"), asyncio.Event()
    task = asyncio.ensure_future(_loop(llm, tmp_path, kill))
    await asyncio.wait_for(llm.started_hanging.wait(), 5)
    kill.set()
    _text, _u, trace = await asyncio.wait_for(task, 5)
    assert trace["turn_end"] == "killed"
    assert trace["speed_summary"]["rounds"] == 1
    assert trace["speed_summary"]["context_used"] == 1000


# --- stored with the message, read back from disk, outside every hash ------

_SIGNER = SimpleNamespace(node_id="dpc-node-me", sign_commit=lambda h: "sig:" + h)


def _monitor(tmp_path, monkeypatch, cid=CID):
    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: True))
    monkeypatch.setattr(ConversationMonitor, "consolidate_split_stores", lambda self: {})
    conv_dir = tmp_path / "conversations" / cid
    conv_dir.mkdir(parents=True, exist_ok=True)
    m = ConversationMonitor(
        conversation_id=cid,
        participants=[{"node_id": "dpc-node-me", "name": "User", "context": "local"},
                      {"node_id": cid, "name": "Ark", "context": "agent"}],
        llm_manager=None,
    )
    m._get_history_path = lambda: conv_dir / "history.json"
    m._get_conversation_dir = lambda: conv_dir
    m._signer = _SIGNER
    return m


def _record_answer(monitor, trace):
    manager = DpcAgentManager.__new__(DpcAgentManager)
    manager.agent_id = CID
    manager._agent_display_name = "Ark"
    agent = SimpleNamespace(_last_usage={}, _last_trace=trace)
    DpcAgentManager._record_answer(manager, monitor, CID, agent, "the answer", [])
    return monitor.get_message_history()[-1]


def test_the_answer_is_saved_with_its_summary_and_read_back_after_a_reload(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    record = _record_answer(monitor, {"accumulated_tool_calls": TOOL_CALLS, "speed_summary": SUMMARY})
    assert record["speed_summary"] == SUMMARY
    assert record["tool_calls"] == TOOL_CALLS

    on_disk = json.loads((tmp_path / "conversations" / CID / "history.json").read_text(encoding="utf-8"))
    assert on_disk["messages"][-1]["speed_summary"] == SUMMARY

    reloaded = _monitor(tmp_path, monkeypatch)
    assert reloaded.load_history()
    assert reloaded.get_message_history()[-1]["speed_summary"] == SUMMARY


def test_a_turn_without_strips_saves_no_field(tmp_path, monkeypatch):
    record = _record_answer(_monitor(tmp_path, monkeypatch), {"accumulated_tool_calls": TOOL_CALLS})
    assert "speed_summary" not in record


def test_the_summary_is_outside_the_signature_the_digest_and_the_chain(tmp_path, monkeypatch):
    """The same message added with and without it: every hash is the same."""
    def add(monitor, summary):
        monitor.add_message("assistant", "the answer", timestamp="2026-11-12T10:00:00+00:00",
                            sender_node_id=CID, sender_name="Ark", message_id="m-1",
                            tool_calls=TOOL_CALLS, speed_summary=summary)
        return monitor.get_message_history()[-1]

    with_it = add(_monitor(tmp_path / "a", monkeypatch), SUMMARY)
    without = add(_monitor(tmp_path / "b", monkeypatch), None)
    assert with_it["speed_summary"] == SUMMARY and "speed_summary" not in without
    for field in ("content_hash", "signature", "tool_calls_digest", "chain_hash"):
        assert with_it[field] == without[field], field
    # And the record still verifies against its own hash after storing it.
    monitor = _monitor(tmp_path / "c", monkeypatch)
    stored = add(monitor, SUMMARY)
    assert monitor._recompute_hash(stored, monitor.conversation_id) == stored["content_hash"]


def test_a_history_sync_does_not_carry_it(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    _record_answer(monitor, {"accumulated_tool_calls": TOOL_CALLS, "speed_summary": SUMMARY})
    exported = monitor.export_history()
    assert exported and all("speed_summary" not in m for m in exported)


def test_a_peers_record_carrying_one_is_stored_without_it(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch, cid=GROUP)
    monitor.merge_history([{"id": "p-1", "role": "user", "content": "from a peer",
                            "timestamp": "2026-11-12T10:00:00+00:00",
                            "sender_node_id": "dpc-node-peer", "speed_summary": SUMMARY}])
    merged = [m for m in monitor.get_message_history() if m.get("id") == "p-1"]
    assert merged and "speed_summary" not in merged[0]


def test_it_is_not_in_what_a_record_replays_into_the_prompt():
    record = {"msg_index": 7, "timestamp": "2026-11-12T10:00:00+00:00", "sender_name": "Ark",
              "role": "assistant", "content": "the answer", "tool_calls": TOOL_CALLS}
    rendered = history_prefix(record) + history_body(record)
    assert history_prefix(dict(record, speed_summary=SUMMARY)) + \
        history_body(dict(record, speed_summary=SUMMARY)) == rendered
    assert "qwen3" not in rendered and "prefill" not in rendered


# --- the group post: to this node's UI, not to the peers --------------------

@pytest.fixture
def author(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node_id = generate_node_id(key.public_key())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    peers = tmp_path / "peers"
    peers.mkdir()
    (peers / f"{node_id}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    return node_id, CommitSigner(node_id, key)


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _Service:
    def __init__(self, node_id, signer, monkeypatch):
        self.node_id = node_id
        self.p2p_manager = SimpleNamespace(node_id=node_id, peers={})
        self.group_manager = self
        self.local_api = _Api()
        self.broadcast = []
        self._processed_message_ids = set()
        self._max_processed_ids = 1000
        monkeypatch.setattr(ConversationMonitor, "save_history", lambda self: True)
        self.monitor = ConversationMonitor(
            conversation_id=GROUP,
            participants=[{"node_id": node_id, "name": "self", "context": "local"}],
            llm_manager=None,
        )
        self.monitor._signer = signer

    def get_group(self, group_id):
        return SimpleNamespace(group_id=GROUP, members=[self.node_id], name="5717",
                               agents={self.node_id: ["agent_001"]},
                               agent_names={self.node_id: {"agent_001": "Ark"}})

    def _get_or_create_conversation_monitor(self, _):
        return self.monitor

    async def _broadcast_to_group(self, group_id, message):
        self.broadcast.append(message)

    llm_manager = SimpleNamespace(alias_context_window=lambda _: 128000,
                                  get_active_model_name=lambda: "m", default_provider="m")

    async def _handle_group_agent_mentions(self, *a, **kw):
        pass

    def _worst_group_agent_context(self, group_id):
        return None

    def _group_agent_context_list(self, group_id):
        return []

    _signature_fields_for = staticmethod(CoreService._signature_fields_for)
    _names_this_node_may_post_as = CoreService._names_this_node_may_post_as

    def _get_agent_display_name(self, agent_id):
        return {"agent_001": "Ark"}.get(agent_id, agent_id)

    def get_cc_display_name(self):
        return "CC"


@pytest.mark.asyncio
async def test_a_group_post_keeps_it_on_the_record_and_the_ui_and_off_the_wire(author, monkeypatch):
    node_id, signer = author
    service = _Service(node_id, signer, monkeypatch)
    await CoreService.send_group_agent_message(service, GROUP, "Ark", "an answer",
                                               tool_calls=TOOL_CALLS, speed_summary=SUMMARY)
    record = service.monitor.get_message_history()[-1]
    assert record["speed_summary"] == SUMMARY

    ui = [p for name, p in service.local_api.events if name == "group_text_received"][-1]
    assert ui["speed_summary"] == SUMMARY

    wire = service.broadcast[-1]["payload"]
    assert "speed_summary" not in wire and "tool_calls" not in wire
    _, verdict, _ = GroupTextHandler(service=None)._authenticate_author(node_id, wire)
    assert verdict == "verified"
