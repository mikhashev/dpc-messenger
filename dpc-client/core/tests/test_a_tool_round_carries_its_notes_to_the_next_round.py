"""A tool-call round keeps the notes that chose the call, and one wire carries them.

Measured in production 2026-10-05 (agent Johnny, 26 rounds): the assistant turn
the loop stored held the sanitized visible text and `tool_calls` and nothing else,
so from round two the model was shown a bare call and its result and re-derived its
plan each round. Seven of the 26 rounds spent the whole note budget and emitted no
visible text at all, at 2.3 to 2.9 minutes each.

What these cover, in the order the value travels:

- the loop keeps the round's notes on the assistant turn under `thinking`, beside
  an unchanged `content`, and never keeps a blank one;
- the final answering round is not stored at all, so its notes cannot leak into
  what the next round or the user reads;
- `_convert_messages_to_anthropic` carries the key as a sibling of `content` — not
  as a `thinking` content block, which DeepSeek's `reasoning_echo` would collect;
- the local llama-server puts it on the wire as `reasoning_content`, and only when
  its alias carries `preserve_reasoning`; the DeepSeek, Z.AI/OpenAI-compatible and
  Ollama converters drop it;
- `_extract_thinking_prefix` still cuts `content` at the first tool-call fence, the
  protection against a model that writes its own `[TOOL RESULT]` sections;
- compaction drops the notes from a round whose results have aged into summaries
  and keeps them on the recent tail;
- the estimate compaction triggers on counts the carried text.
"""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.context import (
    compact_tool_history, compact_tool_history_llm, _KEEP_ASIS_CHARS,
)
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.dpc_agent.loop import _extract_thinking_prefix, run_llm_loop
from dpc_client_core.providers import LlamaServerProvider
from dpc_client_core.providers.base import anthropic_to_openai_messages
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from dpc_client_core.providers.ollama_provider import OllamaProvider
from dpc_client_core.providers.zai_provider import ZaiProvider

NOTES = (
    "The card names loop.py as the only place an assistant turn enters the "
    "history, so read that first and only then the provider converter."
)
GGUF = "D:/models/qwen3.8-27b-Q4_K_M.gguf"
CALL_USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


# --- the loop ------------------------------------------------------------------

class _ToolThenAnswer:
    """Round 1 asks for a tool and reports its notes; round 2 answers. Keeps a
    deep copy of every message list it was handed — the loop mutates the live
    one, so a reference would show only the final state."""

    def __init__(self, thinking=NOTES, content=""):
        self.thinking = thinking
        self.content = content
        self.seen = []

    async def chat(self, messages, **kwargs):
        self.seen.append(copy.deepcopy(messages))
        usage = dict(CALL_USAGE)
        if len(self.seen) == 1:
            msg = {
                "content": self.content,
                "tool_calls": [{
                    "id": "call-1",
                    "function": {"name": "noop", "arguments": "{}"},
                }],
            }
            if self.thinking is not None:
                msg["thinking"] = self.thinking
            return msg, usage
        return {"content": "final answer", "tool_calls": [], "thinking": "last notes"}, usage


class _Tools:
    _ctx = None

    def schemas(self, core_only=False, include_restricted=False):
        return [{"name": "noop"}]

    def get_timeout(self, name):
        return 5

    def execute(self, name, args, ctx=None):
        return "ok"


def _run(tmp_path, monkeypatch, llm):
    monkeypatch.setattr(
        "dpc_client_core.dpc_agent.loop.load_agent_config", lambda _name: {},
    )
    history = [{"role": "user", "content": "hi"}]
    answer, _usage, _trace = asyncio.run(run_llm_loop(
        messages=history,
        tools=_Tools(),
        llm=llm,
        agent_root=tmp_path,
        emit_progress=lambda *a, **k: None,
    ))
    assert answer == "final answer"
    return history


def _assistant_turn(messages):
    turns = [m for m in messages if m.get("role") == "assistant"]
    assert len(turns) == 1, f"expected one stored assistant turn, got {len(turns)}"
    return turns[0]


class TestTheRoundsNotesStayOnTheTurnThatMadeTheCall:

    def test_the_second_round_is_shown_the_notes_that_chose_the_first_call(
            self, tmp_path, monkeypatch):
        llm = _ToolThenAnswer()
        _run(tmp_path, monkeypatch, llm)
        assert len(llm.seen) == 2, "the fake did not take a second round"
        assert _assistant_turn(llm.seen[1])["thinking"] == NOTES

    def test_the_visible_text_is_unchanged_beside_them(self, tmp_path, monkeypatch):
        llm = _ToolThenAnswer(content="Reading the card first.")
        turn = _assistant_turn(_run(tmp_path, monkeypatch, llm))
        assert turn["content"] == "Reading the card first."
        assert turn["tool_calls"][0]["function"]["name"] == "noop"
        assert turn["thinking"] == NOTES

    def test_a_provider_that_reports_no_notes_stores_no_key(self, tmp_path, monkeypatch):
        turn = _assistant_turn(_run(tmp_path, monkeypatch, _ToolThenAnswer(thinking=None)))
        assert "thinking" not in turn

    @pytest.mark.parametrize("blank", ["", "   \n\t  "])
    def test_blank_notes_are_not_stored(self, tmp_path, monkeypatch, blank):
        """An empty one renders as `<think></think>` every round and shifts the
        cached prefix for nothing."""
        turn = _assistant_turn(_run(tmp_path, monkeypatch, _ToolThenAnswer(thinking=blank)))
        assert "thinking" not in turn

    def test_the_notes_are_stored_stripped(self, tmp_path, monkeypatch):
        turn = _assistant_turn(_run(tmp_path, monkeypatch, _ToolThenAnswer(thinking=f"\n {NOTES} \n")))
        assert turn["thinking"] == NOTES

    def test_the_final_answering_round_is_not_stored_at_all(self, tmp_path, monkeypatch):
        """Only tool-call rounds feed the next round. The last round's notes reach
        neither the next context nor the answer the user reads."""
        history = _run(tmp_path, monkeypatch, _ToolThenAnswer())
        assert [m["role"] for m in history] == ["user", "assistant", "tool"]
        assert not any("last notes" in str(m) for m in history)


class TestTheFenceCutStillProtectsTheVisibleText:
    """GLM-4.7 writes its own `[TOOL RESULT]` sections after the first tool-call
    block; `content` is cut at the fence so they never reach the next round. The
    notes ride in their own key and cannot bring them back."""

    def test_the_sanitiser_cuts_at_the_fence(self):
        content = (
            "I will read the file.\n"
            "```tool_call\n{\"name\": \"read_file\"}\n```\n"
            "[TOOL RESULT: call_00_x]\nC:/Users/someone/secret.md\n"
            "[ASSISTANT]\nDone!\n"
        )
        assert _extract_thinking_prefix(content) == "I will read the file."

    def test_the_stored_turn_is_cut_too_while_the_notes_survive(self, tmp_path, monkeypatch):
        llm = _ToolThenAnswer(
            content="I will read the file.\n```tool_call\n{}\n```\n[TOOL RESULT: x]\nleak\n",
        )
        turn = _assistant_turn(_run(tmp_path, monkeypatch, llm))
        assert turn["content"] == "I will read the file."
        assert "leak" not in turn["content"]
        assert turn["thinking"] == NOTES


# --- the wire ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_registry():
    _ACTIVE_SUPERVISORS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()


class _FakeSupervisor:
    def __init__(self, port=8123):
        self.port = port
        self.props = {"total_slots": 4}

    async def ensure_running(self):
        return self.props

    def call_slot(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeCompletions:
    def __init__(self, resp):
        self.resp = resp
        self.bodies = []

    async def create(self, **params):
        self.bodies.append(params)
        return self.resp


def _chat_resp():
    msg = SimpleNamespace(content="ok", reasoning_content=None, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
    )


def _history_after_one_tool_round(thinking=NOTES):
    """What the loop holds at the start of round 2, in Ouroboros shape."""
    turn = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call-1",
            "function": {"name": "read_file", "arguments": json.dumps({"path": "a.md"})},
        }],
    }
    if thinking is not None:
        turn["thinking"] = thinking
    return [
        {"role": "user", "content": "read it"},
        turn,
        {"role": "tool", "tool_call_id": "call-1", "content": "the file"},
    ]


def _anthropic(thinking=NOTES):
    return DpcLlmAdapter._convert_messages_to_anthropic(
        _history_after_one_tool_round(thinking)
    )


async def _post(system, messages, **config):
    """The messages the local provider would actually put on the wire."""
    provider = LlamaServerProvider("local_qwen38", {
        "type": "llamacpp_server", "gguf_path": GGUF, **config,
    })
    provider.supervisor = _FakeSupervisor()
    completions = _FakeCompletions(_chat_resp())
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    async def _ensure():
        return client

    provider._ensure = _ensure
    await provider.generate_with_tools(
        messages,
        [{"name": "read_file", "description": "", "input_schema": {"type": "object"}}],
        system=system,
    )
    return completions.bodies[0]["messages"]


async def _sent_by_llama_server(thinking=NOTES, **config):
    system, messages = _anthropic(thinking)
    return await _post(system, messages, **config)


def _assistant_on_the_wire(sent):
    turns = [m for m in sent if m.get("role") == "assistant"]
    assert len(turns) == 1
    return turns[0]


class TestOnlyTheLocalServerPutsThemOnTheWire:

    def test_the_anthropic_turn_carries_them_as_a_sibling_not_a_block(self):
        """A `thinking` content block would be collected by DeepSeek's
        `reasoning_echo` and reach a paid wire."""
        _system, messages = _anthropic()
        turn = [m for m in messages if m.get("role") == "assistant"][0]
        assert turn["thinking"] == NOTES
        assert all(b.get("type") != "thinking" for b in turn["content"])

    def test_an_assistant_turn_with_no_tool_calls_does_not_carry_them(self):
        """The final answering round is never replayed; if one ever reached the
        converter it would still not be dressed for the wire."""
        _system, messages = DpcLlmAdapter._convert_messages_to_anthropic([
            {"role": "assistant", "content": "done", "thinking": NOTES},
        ])
        assert "thinking" not in messages[0]

    @pytest.mark.asyncio
    async def test_the_flag_on_sends_the_notes_as_reasoning_content(self):
        sent = await _sent_by_llama_server(preserve_reasoning=True)
        assert _assistant_on_the_wire(sent)["reasoning_content"] == NOTES

    def test_the_loops_own_round_two_history_reaches_the_wire(self, tmp_path, monkeypatch):
        """End to end rather than from a hand-built history: a value that exists
        and never reaches its consumer is this repo's most frequent defect."""
        llm = _ToolThenAnswer()
        _run(tmp_path, monkeypatch, llm)
        system, messages = DpcLlmAdapter._convert_messages_to_anthropic(llm.seen[1])
        sent = asyncio.run(_post(system, messages, preserve_reasoning=True))
        assert _assistant_on_the_wire(sent)["reasoning_content"] == NOTES

    @pytest.mark.asyncio
    async def test_the_default_sends_nothing(self):
        sent = await _sent_by_llama_server()
        assert all("reasoning_content" not in m for m in sent)

    @pytest.mark.asyncio
    async def test_the_flag_off_sends_nothing(self):
        sent = await _sent_by_llama_server(preserve_reasoning=False)
        assert all("reasoning_content" not in m for m in sent)

    @pytest.mark.asyncio
    async def test_a_round_that_kept_no_notes_sends_nothing_with_the_flag_on(self):
        sent = await _sent_by_llama_server(thinking=None, preserve_reasoning=True)
        assert all("reasoning_content" not in m for m in sent)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", ["", "   "])
    async def test_blank_notes_send_nothing_with_the_flag_on(self, blank):
        sent = await _sent_by_llama_server(thinking=blank, preserve_reasoning=True)
        assert all("reasoning_content" not in m for m in sent)


class TestTheOtherConvertersIgnoreTheKey:
    """The key is dropped structurally: each converter rebuilds the assistant turn
    from `content` and `tool_calls`, so no provider has to remember to strip it."""

    def test_the_deepseek_converter_pads_as_before_and_never_echoes_the_notes(self):
        _system, messages = _anthropic()
        out = DeepSeekProvider._anthropic_to_openai_messages("", messages, reasoning_echo=True)
        turn = [m for m in out if m.get("role") == "assistant"][0]
        assert turn["reasoning_content"] == " "
        assert NOTES not in json.dumps(out)

    def test_the_openai_compatible_converter_sends_no_reasoning_at_all(self):
        _system, messages = _anthropic()
        out = ZaiProvider._anthropic_to_openai_messages("", messages)
        assert all("reasoning_content" not in m for m in out)
        assert "thinking" not in json.dumps(out)
        assert NOTES not in json.dumps(out)

    def test_the_ollama_native_converter_drops_it(self):
        _system, messages = _anthropic()
        out = OllamaProvider._anthropic_to_openai_messages("", messages)
        assert NOTES not in json.dumps(out)

    def test_the_shared_converter_defaults_to_not_preserving(self):
        _system, messages = _anthropic()
        out = anthropic_to_openai_messages("", messages)
        assert all("reasoning_content" not in m for m in out)


# --- compaction ----------------------------------------------------------------

def _rounds(n, notes=NOTES, big=True):
    msgs = []
    for r in range(n):
        msgs.append({
            "role": "assistant", "content": "", "tool_calls": [{"id": f"t{r}"}],
            "thinking": f"{notes} ({r})",
        })
        size = _KEEP_ASIS_CHARS + 500 if big else 50
        msgs.append({"role": "tool", "tool_call_id": f"t{r}", "content": "X" * size})
    return msgs


class _FakeLLM:
    async def query(self, prompt, provider_alias=None, **kwargs):
        return "SUMMARY"


def _carried(messages):
    return [m.get("thinking") for m in messages if m.get("role") == "assistant"]


class TestCompactionDropsThemWithTheEvidenceTheyReasonedAbout:
    """Decision: an aged-out round loses its notes, the recent tail keeps them.
    Reasoning about a tool result that is now a 200-character summary is worse
    than none, and the notes are the largest thing on the turn."""

    def test_the_deterministic_compactor_strips_the_aged_rounds_only(self):
        out = compact_tool_history(_rounds(10), keep_recent=6)
        carried = _carried(out)
        assert carried[:4] == [None, None, None, None]
        assert all(c and c.startswith(NOTES) for c in carried[4:])

    def test_the_deterministic_compactor_leaves_a_short_history_alone(self):
        msgs = _rounds(4)
        assert compact_tool_history(msgs, keep_recent=6) is msgs

    def test_the_llm_compactor_strips_the_aged_rounds_only(self):
        out = asyncio.run(
            compact_tool_history_llm(_rounds(10), _FakeLLM(), "flash", keep_recent=6)
        )
        carried = _carried(out)
        assert carried[:4] == [None, None, None, None]
        assert all(c and c.startswith(NOTES) for c in carried[4:])

    def test_the_llm_compactor_does_not_touch_the_rest_of_an_aged_turn(self):
        out = asyncio.run(
            compact_tool_history_llm(_rounds(10), _FakeLLM(), "flash", keep_recent=6)
        )
        aged = [m for m in out if m.get("role") == "assistant"][0]
        assert aged["tool_calls"] == [{"id": "t0"}]


# --- the number compaction triggers on -----------------------------------------

class _NoUsageProvider:
    """A provider that reports no usage, so the adapter has to estimate."""

    async def generate_with_tools(self, **kwargs):
        return {"content": "hello", "tool_calls_raw": [], "thinking": ""}


def _adapter():
    mgr = SimpleNamespace(token_count_manager=None, providers={})
    return DpcLlmAdapter(mgr, provider_alias="test")


TOOLS = [{"function": {"name": "t", "description": "", "parameters": {}}}]


class TestTheEstimateCountsTheCarriedText:
    """`last_prompt_tokens` is what compaction and the context guard trigger on.
    Where the server counts it, the carried notes are already inside the number;
    where the adapter estimates, it has to count them too or compaction fires late."""

    def test_the_estimate_grows_with_the_carried_notes(self):
        bare = _history_after_one_tool_round(thinking=None)
        carried = _history_after_one_tool_round(thinking="N" * 4000)
        _m, bare_usage = asyncio.run(
            _adapter()._chat_native_tools(_NoUsageProvider(), bare, TOOLS))
        _m, carried_usage = asyncio.run(
            _adapter()._chat_native_tools(_NoUsageProvider(), carried, TOOLS))
        assert carried_usage["prompt_tokens"] - bare_usage["prompt_tokens"] == 1000

    def test_the_token_counter_branch_sees_them_too(self):
        """That branch counts `str(anthropic_messages)`, so the notes have to be
        inside the converted list and not only in the Ouroboros one."""
        _system, messages = _anthropic()
        assert NOTES in str(messages)
