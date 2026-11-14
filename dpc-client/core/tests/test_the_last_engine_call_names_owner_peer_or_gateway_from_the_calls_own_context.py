"""The last engine call is recorded in the provider's `_ensure`, from the call's own context.

ADR-044, amendment of 2026-11-13 (KV snapshots), point 2: one record per alias of the last
call that reached the engine, written in one place for every call on both engines. A peer's
call (no hold, `serving_a_peer`) is a peer's; a hold carrying a gateway client's `caller` is a
gateway's; the owner's hold on this alias is an agent's turn or the owner's chat; no hold (a
background call), a hold on another alias or a description's borrow names no conversation.
Nothing is recorded while the supervisor drains. `_ensure` notes the call; it becomes the record
only when it enters its call slot (`mark_sent`), so the record is what reached the engine last,
and a call cancelled before its slot never becomes it. The record carries the prefix digest of
the call's own wire messages, and the call's token counts reach the record that call wrote, also
when two calls run side by side.

Fakes only: no engine, no network.
"""

import asyncio
from types import SimpleNamespace

import pytest

from dpc_client_core.managers import kv_snapshots as kv
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import (
    a_background_call, a_card_borrow, card_held_for, carrying_card_hold, peer_being_served, serving_a_peer,
)
from dpc_client_core.providers import LlamaServerProvider, StrataProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS

ALIAS = "local_qwen"
GGUF = "D:/models/qwen3.8-27b-Q4_K_M.gguf"


class FakeSupervisor:
    """What `_ensure`, `call_slot` and the restore ask of a supervisor."""

    ENGINE = "llama-server"

    def __init__(self, alias=ALIAS):
        self.alias = alias
        self.port = 8123
        self.props = {"total_slots": 1}
        self.config = {}
        self._draining = False
        self._in_flight = 0
        self._kv_engine_fresh = False
        self.restores = []
        self.restore_answer = (200, {"n_restored": 10})
        self.in_flight_during_restore = None
        # Held at the slot's entry for a peer's call: noted, not yet sent.
        self.peer_gate = None

    async def ensure_running(self):
        return self.props

    def call_slot(self):
        if self._draining:
            raise RuntimeError("draining")
        self._in_flight += 1
        return self

    async def __aenter__(self):
        if self.peer_gate is not None and peer_being_served() is not None:
            await self.peer_gate.wait()
        return self

    async def __aexit__(self, *exc):
        self._in_flight -= 1
        return False

    async def _restore_kv_snapshot(self, entry):
        self.in_flight_during_restore = self._in_flight
        self.restores.append(dict(entry))
        answer = self.restore_answer
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakeCompletions:
    def __init__(self, usage=(11, 7)):
        self.bodies = []
        self.usage = usage
        self.gate = None
        self.on_create = None

    async def create(self, **params):
        self.bodies.append(params)
        if self.on_create is not None:
            self.on_create(params)
        if self.gate is not None:
            await self.gate.wait()
        msg = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None)
        prompt, completion = self.usage
        if params.get("stream"):
            return FakeStream(prompt, completion)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                                  total_tokens=prompt + completion),
        )


class FakeStream:
    """One chunk carrying the text, the finish word and the usage; both stream shapes."""

    def __init__(self, prompt, completion):
        delta = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None)
        self.chunks = [SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                                  total_tokens=prompt + completion),
            model_extra={},
        )]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for chunk in self.chunks:
            yield chunk


def wire(provider, sup=None, completions=None):
    """The provider's real `_ensure` over a fake supervisor and a fake client."""
    sup = sup or FakeSupervisor(provider.alias)
    completions = completions or FakeCompletions()
    provider.supervisor = sup
    provider._openai = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider._port = sup.port
    return sup, completions


def llama(alias=ALIAS):
    return LlamaServerProvider(alias, {"type": "llamacpp_server", "gguf_path": GGUF})


def owner_hold(alias=ALIAS, agent_id=None, conversation_id=None, caller=None):
    """A stand-in hold: the real `_CardHold` gains `caller` in the supervisor piece."""
    return SimpleNamespace(alias=alias, live=True, agent_id=agent_id, conversation_id=conversation_id,
                           borrow=None, caller=caller)


TOOLS_TURN = [{"role": "user", "content": "list the files"}]


@pytest.fixture(autouse=True)
def clean():
    _ACTIVE_SUPERVISORS.clear()
    kv.reset_state()
    yield
    _ACTIVE_SUPERVISORS.clear()
    kv.reset_state()


@pytest.mark.asyncio
async def test_a_peers_call_with_no_hold_is_recorded_as_the_peers():
    p = llama()
    wire(p)
    with serving_a_peer("dpc-node-0123456789abcdef0123456789abcdef"):
        await p.generate_response("hello")
    record = kv.engine_call_of(ALIAS)
    assert record["caller_kind"] == "peer"
    assert record["peer_id"] == "dpc-node-0123456789abcdef0123456789abcdef"
    assert record["conversation_id"] is None and record["agent_id"] is None


@pytest.mark.asyncio
async def test_a_peers_call_inside_an_owners_hold_is_still_the_peers():
    p = llama()
    wire(p)
    with carrying_card_hold(owner_hold(agent_id="a1", conversation_id="c1")), serving_a_peer("dpc-node-x"):
        await p.generate_response("hello")
    assert kv.engine_call_of(ALIAS)["caller_kind"] == "peer"


@pytest.mark.asyncio
async def test_a_gateway_clients_call_is_recorded_as_the_gateways():
    p = llama()
    wire(p)
    with carrying_card_hold(owner_hold(caller="dpc-node-owner")):
        await p.generate_response("hello")
    record = kv.engine_call_of(ALIAS)
    assert record["caller_kind"] == "gateway"
    assert record["conversation_id"] is None
    # The gateway client's id is kept, so a later rule can tell harnesses apart.
    assert record["peer_id"] == "dpc-node-owner"


@pytest.mark.asyncio
async def test_a_gateway_hold_with_an_empty_caller_is_still_the_gateways():
    p = llama()
    wire(p)
    with carrying_card_hold(owner_hold(caller="", conversation_id="c1")):
        await p.generate_response("hello")
    record = kv.engine_call_of(ALIAS)
    assert record["caller_kind"] == "gateway"
    assert record["conversation_id"] is None
    assert record["peer_id"] is None


@pytest.mark.asyncio
async def test_a_real_gateway_hold_is_the_gateways_once_the_hold_carries_its_caller():
    if "caller" not in base_mod._CardHold.__slots__:
        pytest.skip("_CardHold carries no caller yet (supervisor piece P2)")
    p = llama()
    wire(p)
    async with card_held_for(ALIAS, "accepted_request", caller="dsh"):
        await p.generate_response("hello")
    record = kv.engine_call_of(ALIAS)
    assert (record["caller_kind"], record["peer_id"]) == ("gateway", "dsh")


@pytest.mark.asyncio
async def test_an_agents_turn_is_recorded_with_its_agent_and_conversation():
    p = llama()
    wire(p)
    async with card_held_for(ALIAS, "accepted_request", agent_id="agent_7", conversation_id="conv-a"):
        await p.generate_with_tools(TOOLS_TURN, [], system="you are a helper")
    record = kv.engine_call_of(ALIAS)
    assert (record["caller_kind"], record["agent_id"], record["conversation_id"]) == (
        "agent", "agent_7", "conv-a")


@pytest.mark.asyncio
async def test_the_owners_chat_is_recorded_with_its_conversation_only():
    p = llama()
    wire(p)
    async with card_held_for(ALIAS, "accepted_request", conversation_id="chat-1"):
        await p.generate_response("hello")
    record = kv.engine_call_of(ALIAS)
    assert (record["caller_kind"], record["agent_id"], record["conversation_id"]) == (
        kv.OWNER_CHAT_KIND, None, "chat-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("context", ["no hold", "other alias", "borrow", "background", "ended hold"])
async def test_a_call_that_is_no_owners_turn_on_this_alias_names_no_conversation(context):
    p = llama()
    wire(p)
    if context == "no hold":
        await p.generate_response("hello")
    elif context == "other alias":
        with carrying_card_hold(owner_hold(alias="another", agent_id="a1", conversation_id="c1")):
            await p.generate_response("hello")
    elif context == "borrow":
        with carrying_card_hold(owner_hold(agent_id="a1", conversation_id="c1")), a_card_borrow():
            await p.generate_response("describe the picture")
    elif context == "background":
        with carrying_card_hold(owner_hold(agent_id="a1", conversation_id="c1")), \
                a_background_call("skill_reflection"):
            await p.generate_response("reflect")
    else:
        hold = owner_hold(agent_id="a1", conversation_id="c1")
        hold.live = False
        with carrying_card_hold(hold):
            await p.generate_response("hello")
    record = kv.engine_call_of(ALIAS)
    assert record is not None
    assert record["caller_kind"] is None
    assert record["conversation_id"] is None and record["agent_id"] is None


@pytest.mark.asyncio
async def test_a_hold_on_another_alias_that_borrowed_this_one_names_no_conversation():
    p = llama()
    wire(p)
    hold = owner_hold(alias="own", agent_id="a1", conversation_id="c1")
    hold.borrow = SimpleNamespace(alias=ALIAS)
    with carrying_card_hold(hold):
        await p.generate_response("describe the picture")
    assert kv.engine_call_of(ALIAS)["caller_kind"] is None


@pytest.mark.asyncio
async def test_nothing_is_recorded_while_the_supervisor_drains():
    p = llama()
    sup, _ = wire(p)
    sup._draining = True
    sup._kv_engine_fresh = True
    client = await p._ensure_for([{"role": "user", "content": "hello"}])
    assert client is p._openai
    assert kv.engine_call_of(ALIAS) is None
    assert sup.restores == []
    assert sup._kv_engine_fresh is True


@pytest.mark.asyncio
async def test_the_record_carries_the_digest_of_the_calls_own_wire_messages():
    p = llama()
    _, completions = wire(p)
    async with card_held_for(ALIAS, "accepted_request", agent_id="a1", conversation_id="c1"):
        await p.generate_with_tools(TOOLS_TURN, [], system="you are a helper")
    sent = completions.bodies[0]["messages"]
    assert sent[0]["role"] == "system"
    record = kv.engine_call_of(ALIAS)
    assert (record["prefix_sha256"], record["prefix_messages"]) == kv.prefix_digest(sent)


@pytest.mark.asyncio
async def test_every_call_path_passes_its_wire_messages_and_marks_the_call_sent_before_it_goes(monkeypatch):
    p = llama()
    sup, completions = wire(p)
    seen, events = [], []
    real_note, real_sent = kv.note_engine_call, kv.mark_sent

    def note(alias, **kw):
        seen.append(kw["prefix"])
        return real_note(alias, **kw)

    def sent(alias, seq):
        events.append(("sent", seq, sup._in_flight))
        return real_sent(alias, seq)

    monkeypatch.setattr(kv, "note_engine_call", note)
    monkeypatch.setattr(kv, "mark_sent", sent)
    completions.on_create = lambda params: events.append(("create",))
    await p.generate_response("plain")
    await p.generate_with_vision("what is it", [{"mime_type": "image/png", "base64": "iVBORw0KGgo="}])
    await p.generate_with_tools(TOOLS_TURN, [], system="s")
    await p.generate_with_tools(TOOLS_TURN, [], system="s", on_reasoning=_no_reasoning)

    async def ignore(text, cid):
        return None

    await p.generate_response_stream("streamed", on_chunk=ignore)
    assert seen == [kv.prefix_digest(body["messages"]) for body in completions.bodies]
    # Each call: marked sent inside its slot (one call in flight), then sent.
    assert [e[0] for e in events] == ["sent", "create"] * 5
    assert all(e[2] == 1 for e in events if e[0] == "sent")


async def _no_reasoning(text, cid):
    return None


@pytest.mark.asyncio
async def test_the_record_is_the_call_sent_last_not_the_call_noted_last():
    p = llama()
    sup, _ = wire(p)
    sup.peer_gate = asyncio.Event()
    with serving_a_peer("dpc-node-peer"):
        peer = asyncio.ensure_future(p.generate_response("peer prompt"))
    while not kv._PENDING.get(ALIAS):
        await asyncio.sleep(0)
    assert kv.engine_call_of(ALIAS) is None  # noted, not sent
    async with card_held_for(ALIAS, "accepted_request", agent_id="a1", conversation_id="c1"):
        await p.generate_response("owner prompt")
    assert kv.engine_call_of(ALIAS)["caller_kind"] == "agent"
    sup.peer_gate.set()
    await peer
    record = kv.engine_call_of(ALIAS)
    assert record["caller_kind"] == "peer" and record["peer_id"] == "dpc-node-peer"


@pytest.mark.asyncio
async def test_a_call_cancelled_before_its_slot_never_becomes_the_record():
    p = llama()
    sup, completions = wire(p)
    async with card_held_for(ALIAS, "accepted_request", agent_id="a1", conversation_id="c1"):
        await p.generate_response("owner prompt")
    sup.peer_gate = asyncio.Event()
    with serving_a_peer("dpc-node-peer"):
        peer = asyncio.ensure_future(p.generate_response("peer prompt"))
    while not kv._PENDING.get(ALIAS):
        await asyncio.sleep(0)
    peer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await peer
    assert kv.engine_call_of(ALIAS)["caller_kind"] == "agent"
    assert len(completions.bodies) == 1


@pytest.mark.asyncio
async def test_the_calls_tokens_reach_the_record_it_wrote(monkeypatch):
    p = llama()
    wire(p, completions=FakeCompletions(usage=(1200, 40)))
    tokens = []
    monkeypatch.setattr(kv, "note_engine_call", lambda alias, **kw: 41)
    monkeypatch.setattr(kv, "note_engine_tokens", lambda *a: tokens.append(a))
    await p.generate_response("hello")
    assert tokens == [(ALIAS, 41, 1200, 40)]


@pytest.mark.asyncio
async def test_two_calls_side_by_side_each_hand_their_tokens_to_their_own_record(monkeypatch):
    p = llama()
    completions = FakeCompletions()
    wire(p, completions=completions)
    seqs = iter([1, 2])
    tokens = []
    monkeypatch.setattr(kv, "note_engine_call", lambda alias, **kw: next(seqs))
    monkeypatch.setattr(kv, "note_engine_tokens", lambda *a: tokens.append(a))
    completions.gate = asyncio.Event()
    first = asyncio.ensure_future(p.generate_response("first"))
    while len(completions.bodies) < 1:
        await asyncio.sleep(0)
    second = asyncio.ensure_future(p.generate_response("second, longer"))
    while len(completions.bodies) < 2:
        await asyncio.sleep(0)
    completions.gate.set()
    await asyncio.gather(first, second)
    # Both ran under seq 1 and 2 in their own tasks; neither read the other's.
    assert sorted(t[1] for t in tokens) == [1, 2]


@pytest.mark.asyncio
async def test_a_record_function_that_returns_no_number_hands_no_tokens(monkeypatch):
    p = llama()
    wire(p)
    tokens = []
    monkeypatch.setattr(kv, "note_engine_call", lambda alias, **kw: True)
    monkeypatch.setattr(kv, "note_engine_tokens", lambda *a: tokens.append(a))
    await p.generate_response("hello")
    assert tokens == []


@pytest.mark.asyncio
async def test_a_record_that_fails_never_fails_the_call(monkeypatch):
    p = llama()
    wire(p)

    def boom(alias, **kw):
        raise RuntimeError("index on an unplugged disk")

    monkeypatch.setattr(kv, "note_engine_call", boom)
    assert await p.generate_response("hello") == "ok"


@pytest.mark.asyncio
async def test_strata_records_through_its_own_ensure_with_the_wire_messages(tmp_path, monkeypatch):
    from tests.test_strata_supervisor import _install

    monkeypatch.setattr(base_mod, "DPC_HOME", tmp_path / "dpc-home")
    p = StrataProvider("flash", {
        "type": "strata", "alias": "flash", "model": "flash-next",
        "strata_install_dir": str(_install(tmp_path)), "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240,
    })
    sup, completions = wire(p, sup=FakeSupervisor("flash"))
    sup.ENGINE = "strata"
    async with card_held_for("flash", "accepted_request", agent_id="a1", conversation_id="c1"):
        await p.generate_with_tools(TOOLS_TURN, [], system="s")
    with serving_a_peer("dpc-node-y"):
        await p.generate_response("peer prompt")
    record = kv.engine_call_of("flash")
    assert record["caller_kind"] == "peer"
    assert (record["prefix_sha256"], record["prefix_messages"]) == kv.prefix_digest(
        completions.bodies[-1]["messages"])
