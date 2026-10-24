"""A final answer that is a tool call written as text is replaced by a note
saying the action did not run, naming the tool.

Live, 2026-10-25: after a guard stop Ark's reply was 16 481 characters of
DeepSeek DSML — a complete write_file call — stored in history.json as his
answer; the user read "done" and nothing was written. The 2026-07-18 shape was
a lone `{"name": …, "arguments": …}` object (GUARD-FORCED-ANSWER-LOSES-TOOL-
CHANNEL). This project discusses its own tool format, so markup in code, a
single tag named in a sentence, or JSON beside prose stays as it is.
"""

from __future__ import annotations

import asyncio

from dpc_client_core.dpc_agent.guards import ResearchLimitGuard
from dpc_client_core.dpc_agent.hooks import HookRegistry
from dpc_client_core.dpc_agent.loop import (
    _answer_without_unrun_calls,
    _finalize_after_guard_stop,
    _written_tool_calls,
    run_llm_loop,
)

# The shape in Ark's history.json, bars doubled and a space before the word.
ARK_DSML = (
    '<｜｜DSML｜｜ calls>\n'
    '<｜｜DSML｜｜ invoke name="write_file">\n'
    '<｜｜DSML｜｜ parameter name="path" string="true">knowledge/note.md</｜｜DSML｜｜ parameter>\n'
    '<｜｜DSML｜｜ parameter name="content" string="true"># Title\n\nBody.\n</｜｜DSML｜｜ parameter>\n'
    '</｜｜DSML｜｜ invoke>\n'
    '</｜｜DSML｜｜ calls>'
)
# The vendor template's spelling.
VENDOR_DSML = (
    '<｜DSML｜function_calls>\n'
    '<｜DSML｜invoke name="update_scratchpad">\n'
    '<｜DSML｜parameter name="content" string="true">notes</｜DSML｜parameter>\n'
    '</｜DSML｜invoke>\n'
    '</｜DSML｜function_calls>'
)


def _replaced(content):
    trace = {}
    return _answer_without_unrun_calls(content, trace), trace


def test_a_dsml_only_answer_becomes_the_not_executed_note():
    text, trace = _replaced(ARK_DSML)
    assert "DSML" not in text
    assert text.startswith("⚠️ Not done") and "`write_file` was never executed" in text
    assert trace["unexecuted_tool_calls"] == [
        {"tools": ["write_file"], "chars": len(ARK_DSML), "kept_at": None}]


def test_the_vendor_spelling_is_caught_too():
    text, _ = _replaced(VENDOR_DSML)
    assert "`update_scratchpad` was never executed" in text and "DSML" not in text


def test_prose_beside_the_markup_is_kept_under_the_note():
    text, _ = _replaced("Saving the write-up now.\n\n" + ARK_DSML + "\n\nDone.")
    assert "`write_file`" in text and "DSML" not in text
    assert text.endswith("ask again to have it done.\n\nSaving the write-up now.\n\nDone.")


def test_a_call_cut_by_max_tokens_is_cut_to_the_end():
    truncated = "Here it is.\n" + ARK_DSML[:ARK_DSML.index("Body.")]
    names, prose = _written_tool_calls(truncated)
    assert names == ["write_file"] and prose == "Here it is."


def test_a_lone_call_json_becomes_the_note():
    for content in ('{"name": "update_scratchpad", "arguments": {"content": "x"}}',
                    '```json\n{"name": "update_scratchpad", "arguments": {}}\n```'):
        text, _ = _replaced(content)
        assert text.startswith("⚠️ Not done") and "`update_scratchpad`" in text, content


def test_prose_about_the_tool_format_is_left_alone():
    kept = [
        # A tag quoted in backticks, and a whole block in a fence.
        'The model wrote `<｜DSML｜invoke name="write_file">` instead of calling it.',
        "Ark's reply looked like this:\n\n```\n" + ARK_DSML + "\n```\n\nThat is the bug.",
        # One tag named in a sentence, with no structure behind it.
        'It opened with <｜DSML｜invoke name="write_file"> and nothing else followed.',
        # JSON beside a sentence is an example, not a call.
        'A call looks like {"name": "read_file", "arguments": {"path": "a"}} on the wire.',
        '{"name": "read_file", "arguments": {}, "note": "three keys is not a call"}',
        "Plain answer with no markup at all.",
    ]
    for content in kept:
        text, trace = _replaced(content)
        assert text == content and trace == {}, content


# --- where an answer is accepted ------------------------------------------------

class _Scripted:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append(kwargs.get("tool_choice"))
        return self.answers.pop(0), {"prompt_tokens": 1, "completion_tokens": 1}


def _hooks():
    hooks = HookRegistry()
    guard = ResearchLimitGuard()
    hooks.register(guard)
    hooks.last_triggered = guard
    return hooks


TOOLS = [{"type": "function", "function": {"name": "write_file", "parameters": {}}}]


def test_the_guard_stop_retry_answering_in_dsml_stores_the_note():
    """The live sequence with no dispatch to run the call: tool call, retry, DSML."""
    llm = _Scripted(
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "w", "function": {"name": "write_file", "arguments": "{}"}}]},
        {"role": "assistant", "content": ARK_DSML},
    )
    text, _, trace = asyncio.run(_finalize_after_guard_stop(
        _hooks(), [{"role": "user", "content": "q"}], llm, None, "c", {}, {},
        fallback_reason="stopped", tools=TOOLS,
    ))
    assert llm.calls == [None, "none"]
    assert text.startswith("⚠️ Not done") and "`write_file`" in text and "DSML" not in text
    assert trace["unexecuted_tool_calls"][0]["tools"] == ["write_file"]


class _Tools:
    _ctx = None

    def schemas(self, core_only=False, include_restricted=False):
        return TOOLS

    def get_timeout(self, name):
        return 5

    def execute(self, name, args, ctx=None):
        return "ok"


def test_an_ordinary_final_answer_in_dsml_stores_the_note(tmp_path, monkeypatch):
    """Not only after a guard: the claim "done" is the same wherever it lands."""
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})

    class _Llm:
        async def chat(self, messages, **kwargs):
            return {"content": ARK_DSML}, {"prompt_tokens": 1, "completion_tokens": 1}

    text, _, _ = asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "save it"}], tools=_Tools(), llm=_Llm(),
        agent_root=tmp_path, emit_progress=lambda *a, **k: None, task_id="t",
    ))
    assert text.startswith("⚠️ Not done") and "`write_file`" in text and "DSML" not in text


# --- the replaced text is kept, so the note is not a silent loss ---------------

def _final_answer(tmp_path, monkeypatch, content):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})

    class _Llm:
        async def chat(self, messages, **kwargs):
            return {"content": content}, {"prompt_tokens": 1, "completion_tokens": 1}

    text, _, trace = asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "save it"}], tools=_Tools(), llm=_Llm(),
        agent_root=tmp_path, emit_progress=lambda *a, **k: None, task_id="t",
    ))
    return text, trace


def test_the_replaced_text_is_kept_byte_for_byte_and_the_note_names_where(tmp_path, monkeypatch):
    text, trace = _final_answer(tmp_path, monkeypatch, ARK_DSML)
    kept_at = trace["unexecuted_tool_calls"][0]["kept_at"]
    assert kept_at.startswith("state/tool_results/") and kept_at.endswith("/unexecuted-1.txt")
    assert "`write_file`" in text and f"kept at `{kept_at}`" in text
    assert (tmp_path / kept_at).read_bytes() == ARK_DSML.encode("utf-8")


def test_a_failed_keep_still_gives_the_note(tmp_path, monkeypatch):
    (tmp_path / "state").write_text("a file where the store's directory should be")
    text, trace = _final_answer(tmp_path, monkeypatch, ARK_DSML)
    assert text.startswith("⚠️ Not done") and "`write_file`" in text and "kept at" not in text
    assert trace["unexecuted_tool_calls"][0]["kept_at"] is None
