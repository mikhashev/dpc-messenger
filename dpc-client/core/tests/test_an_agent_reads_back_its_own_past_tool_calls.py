"""An agent reads back its own past tool calls, in this session and in its archives
(AN-AGENT-CANNOT-SEE-ITS-OWN-PAST-TOOL-CALLS-IN-THIS-SESSION-OR-IN-ARCHIVES, follow-up 13).

history.json and session archives keep each assistant message's `tool_calls` (tool,
input, output, round, round_text). chat_history, read_session_archive and
read_session_detail show them on `include_tool_calls`, every args and output cut to a
fixed size with the cut stated; search_session_archives matches tool names and
arguments under scope `tool_calls` (and `all`). Mike decided: no audience restriction,
the calls never leave the owner node.
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

from dpc_client_core.dpc_agent.agent import ALL_MEMORY_LAYERS, OWNER_TURN, DpcAgent
from dpc_client_core.dpc_agent.tools import archive
from dpc_client_core.dpc_agent.tools._tool_calls_view import (
    ARGS_CHARS, CALLS_PER_MESSAGE, OUTPUT_CHARS,
)
from dpc_client_core.dpc_agent.tools.core import chat_history
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.firewall import ContextFirewall

THIS = "dpc-node-" + "a" * 32
AGENT = "agent_x"
FILE = "2026-11-01T10-00-00_reset_session.json"


def _call(tool, args, output, round_idx=0, **kw):
    return {"tool": tool, "input": json.dumps(args), "output": output,
            "is_error": False, "duration_ms": 5, "round": round_idx,
            "round_text": "thinking about it", **kw}


MESSAGES = [
    {"role": "user", "sender_name": "Mike", "content": "check the service",
     "timestamp": "2026-11-01T10:00:00Z"},
    {"role": "assistant", "sender_name": "X", "content": "Done, it looks fine.",
     "timestamp": "2026-11-01T10:00:10Z",
     "tool_calls": [
         _call("read_file", {"path": "dpc_client_core/service.py"}, "line one\nline two", 0),
         _call("run_shell", {"command": "git status"}, "E" * 2000, 1, is_error=True),
     ]},
    {"role": "user", "sender_name": "Mike", "content": "and the harbour?",
     "timestamp": "2026-11-01T10:01:00Z"},
]


def _ctx(tmp, conv_dir):
    rules = tmp / "privacy_rules.json"
    rules.write_text(json.dumps({"dpc_agent": {"enabled": True}, "agent_profiles": {
        AGENT: {"history": {"preserve_on_reset": True}}}}), encoding="utf-8")
    ctx = ToolContext(agent_root=tmp / "agents" / AGENT, firewall=ContextFirewall(rules),
                      message_source="ui", turn_class=OWNER_TURN,
                      open_layers=ALL_MEMORY_LAYERS, conversation_id=AGENT)
    ctx.conversation_monitor = SimpleNamespace(
        conversation_id=AGENT, _get_history_path=lambda: conv_dir / "history.json",
        message_history=list(MESSAGES))
    agent = DpcAgent.__new__(DpcAgent)
    agent._firewall_profile = AGENT
    agent._service = SimpleNamespace(group_manager=SimpleNamespace(node_id=THIS))
    ctx._agent = agent
    return ctx


def _archived(tmp_path: pathlib.Path, messages=MESSAGES):
    conv = tmp_path / "conversations" / AGENT
    folder = conv / "archive" / "2026" / "11"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / FILE).write_text(json.dumps({
        "archived_at": "2026-11-01T10:02:00Z", "session_reason": "reset",
        "messages": messages}), encoding="utf-8")
    return conv


def test_chat_history_leaves_tool_calls_out_unless_asked(tmp_path):
    out = chat_history(_ctx(tmp_path, tmp_path), include_tool_calls=False)
    assert "[TOOL" not in out
    assert "read_file" not in out


def test_chat_history_shows_each_call_with_name_args_output_and_round(tmp_path):
    out = chat_history(_ctx(tmp_path, tmp_path), include_tool_calls=True)
    assert '[TOOL r0] read_file({"path": "dpc_client_core/service.py"}) -> line one' in out
    assert "[TOOL r1 ERROR] run_shell(" in out
    # The round's reasoning is not repeated per call.
    assert "thinking about it" not in out


def test_a_long_output_is_cut_and_the_cut_is_stated(tmp_path):
    out = chat_history(_ctx(tmp_path, tmp_path), include_tool_calls=True)
    line = next(ln for ln in out.splitlines() if "run_shell" in ln)
    assert "E" * OUTPUT_CHARS in line
    assert "E" * (OUTPUT_CHARS + 1) not in line
    assert f"[{2000 - OUTPUT_CHARS} more chars cut]" in line


def test_a_message_with_many_calls_shows_the_first_ones_and_counts_the_rest(tmp_path):
    ctx = _ctx(tmp_path, tmp_path)
    many = [_call("list_dir", {"path": f"d{i}"}, "x", i) for i in range(CALLS_PER_MESSAGE + 5)]
    long_args = _call("write_file", {"content": "A" * 5000}, "ok", 0)
    ctx.conversation_monitor.message_history = [
        {"role": "assistant", "content": "a", "tool_calls": many},
        {"role": "assistant", "content": "b", "tool_calls": [long_args]},
    ]
    out = chat_history(ctx, include_tool_calls=True)
    assert out.count("] list_dir(") == CALLS_PER_MESSAGE
    assert "5 more tool calls in this message not shown" in out
    write_line = next(ln for ln in out.splitlines() if "write_file" in ln)
    assert "more chars cut]" in write_line
    assert len(write_line) < ARGS_CHARS + OUTPUT_CHARS + 200


def test_chat_history_schema_offers_the_flag():
    from dpc_client_core.dpc_agent.tools import core
    entry = next(e for e in core.get_tools() if e.name == "chat_history")
    assert "include_tool_calls" in entry.schema["parameters"]["properties"]


def test_read_session_detail_carries_bounded_tool_calls_on_the_flag(tmp_path):
    conv = _archived(tmp_path)
    ctx = _ctx(tmp_path, conv)
    plain = json.loads(archive.read_session_detail(ctx, FILE))
    assert all("tool_calls" not in m for m in plain["messages"])

    full = json.loads(archive.read_session_detail(ctx, FILE, include_tool_calls=True))
    calls = full["messages"][1]["tool_calls"]
    assert [c["name"] for c in calls] == ["read_file", "run_shell"]
    assert calls[0] == {"name": "read_file",
                        "args": '{"path": "dpc_client_core/service.py"}',
                        "output": "line one\nline two", "round": 0}
    assert calls[1]["is_error"] is True
    assert calls[1]["output"].endswith(f"[{2000 - OUTPUT_CHARS} more chars cut]")
    assert "tool_calls" not in full["messages"][0]


def test_read_session_archive_counts_tool_calls_per_name_on_the_flag(tmp_path):
    conv = _archived(tmp_path)
    ctx = _ctx(tmp_path, conv)
    plain = json.loads(archive.read_session_archive(ctx))
    assert "tool_calls" not in plain["sessions"][0]
    summary = json.loads(archive.read_session_archive(ctx, include_tool_calls=True))
    assert summary["sessions"][0]["tool_calls"] == {
        "count": 2, "by_name": {"read_file": 1, "run_shell": 1}}


def test_search_finds_where_a_file_was_read_by_tool_name_and_arguments(tmp_path):
    conv = _archived(tmp_path)
    ctx = _ctx(tmp_path, conv)
    found = json.loads(archive.search_session_archives(ctx, "read_file service.py",
                                                       scope="tool_calls"))
    assert found["total_matches"] == 1
    hit = found["matches"][0]
    assert (hit["message_index"], hit["scope_matched"]) == (1, "tool_calls")
    assert "service.py" in hit["snippet"]
    # Outputs are not searched: they are what the call got, not what it did.
    assert json.loads(archive.search_session_archives(
        ctx, "EEEE", scope="tool_calls"))["total_matches"] == 0


def test_content_scope_does_not_match_tool_arguments_and_all_does(tmp_path):
    conv = _archived(tmp_path)
    ctx = _ctx(tmp_path, conv)
    assert json.loads(archive.search_session_archives(
        ctx, "git status", scope="content"))["total_matches"] == 0
    hits = json.loads(archive.search_session_archives(ctx, "git status", scope="all"))
    assert [m["scope_matched"] for m in hits["matches"]] == ["tool_calls"]


def test_an_index_built_before_tool_calls_were_indexed_is_rebuilt(tmp_path):
    conv = _archived(tmp_path)
    ctx = _ctx(tmp_path, conv)
    archive.search_session_archives(ctx, "harbour")
    rows_file = conv / "archive" / archive.INDEX_DIR / archive.ROWS_FILE
    assert json.loads(rows_file.read_text(encoding="utf-8")) == {
        "rows_version": archive.ROWS_VERSION}
    # A store from before this change has no rows file and no tool_calls rows.
    rows_file.unlink()
    found = json.loads(archive.search_session_archives(ctx, "git status", scope="tool_calls"))
    assert found["total_matches"] == 1
    assert rows_file.exists()


def test_the_substring_fallback_reads_tool_calls_as_names_and_arguments(tmp_path):
    conv = _archived(tmp_path)
    ctx = _ctx(tmp_path, conv)
    # "/" alone keeps no word, so the substring match answers.
    found = json.loads(archive.search_session_archives(ctx, "e/s", scope="tool_calls"))
    assert found["total_matches"] == 1
    assert found["matches"][0]["scope_matched"] == "tool_calls"
