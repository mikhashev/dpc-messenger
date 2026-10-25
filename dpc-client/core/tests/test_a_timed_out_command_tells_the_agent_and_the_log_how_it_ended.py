"""A command killed at its timeout says how it ended, to the log and to the agent.

The three commands that left the service console raw (2026-10-22, 2026-10-24)
were timeout kills, and dpc-client.log named none of them: `run_supervised`
logged a ceiling kill and not a timeout kill. The agent fared no better — it
read "timed out after 240s" and the first 50 000 of 1 151 755 chars, while the
line that explained everything, `-- More --`, was the last one.

Now the log carries a WARNING with the pid, the launcher, the timeout, what was
captured and the command; and the agent's result carries the captured sizes,
the last lines, where the full captured output is stored, and a sentence when
the output ends at a pager prompt. A run that is not killed reads as before.
"""

from __future__ import annotations

import logging
import sys

import pytest

from dpc_client_core.dpc_agent import loop as loop_mod
from dpc_client_core.dpc_agent.tools import process as process_mod
from dpc_client_core.dpc_agent.tools import shell
from dpc_client_core.dpc_agent.tools.process import (
    SupervisedRun, output_tail, pager_prompt_at_end, run_supervised,
)
from dpc_client_core.dpc_agent.tools.registry import ToolContext

TURN, CALL = "20261024T175851Z-abcdef12", "r7-0"
WINDOWS_MORE_TAIL = "65531\n65532\n65533\n-- More  -- "


def _numbers(n: int) -> str:
    return "".join(f"{i}\n" for i in range(n))


def _fake(monkeypatch, run: SupervisedRun):
    seen = {}

    def fake(cmd, **kwargs):
        seen.update(kwargs, cmd=cmd)
        return run

    monkeypatch.setattr(shell, "run_supervised", fake)
    return seen


# ------------------------------------------------------------ the log

def test_a_timeout_kill_writes_a_warning_like_the_ceiling_kill(caplog):
    caplog.set_level(logging.WARNING, logger=process_mod.__name__)
    run = run_supervised(
        [sys.executable, "-c", "print('started', flush=True); import time; time.sleep(60)"],
        launcher="run_shell", timeout=2, ceiling_mb=0,
    )
    assert run.timed_out
    lines = [r for r in caplog.records
             if r.name == process_mod.__name__ and "ran past its 2s timeout" in r.getMessage()]
    assert len(lines) == 1, [r.getMessage() for r in caplog.records]
    record = lines[0]
    message = record.getMessage()
    assert record.levelno == logging.WARNING
    assert "for run_shell" in message and "pid " in message
    assert run.killed in message
    assert f"{len(run.stdout)} chars of stdout and {len(run.stderr)} of stderr captured" in message
    assert "time.sleep(60)" in message  # the command itself, first 200 chars
    assert "pager prompt" not in message


def test_the_log_names_the_command_not_its_wrapper(caplog):
    caplog.set_level(logging.WARNING, logger=process_mod.__name__)
    run_supervised([sys.executable, "-c", "import time; time.sleep(60)"],
                   launcher="run_shell", timeout=2, ceiling_mb=0, log_as="the agent's own words")
    message = next(r.getMessage() for r in caplog.records if "timeout" in r.getMessage())
    assert message.endswith(": the agent's own words")


def test_a_run_that_ends_in_time_writes_no_warning(caplog):
    caplog.set_level(logging.WARNING, logger=process_mod.__name__)
    run_supervised([sys.executable, "-c", "print(1)"], launcher="run_shell", timeout=30, ceiling_mb=0)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ------------------------------------------------------------ what the agent reads

def test_the_agent_reads_the_kill_the_sizes_the_last_lines_and_the_stored_path(monkeypatch, tmp_path):
    stdout = _numbers(65531) + WINDOWS_MORE_TAIL
    _fake(monkeypatch, SupervisedRun(timed_out=True, returncode=1, stdout=stdout,
                                     killed="the command and its descendants were killed"))

    text = shell._execute_shell_command("tok | more +0", None, 240, store_at=(tmp_path, TURN, CALL))
    head = text[:loop_mod.TOOL_RESULT_CHAR_CAP]  # what survives the loop's own cut

    assert head.startswith("Error: command timed out after 240s - the command and its descendants were killed.")
    assert f"Captured before the kill: stdout {len(stdout):,} chars in 65,535 lines, stderr empty." in head
    assert "[last 20 lines of stdout]" in head
    assert "65533\n-- More  --" in head
    stored = f"state/tool_results/{TURN}/{CALL}.stdout.txt"
    assert f"Full captured output: stdout at {stored} (read_file reads it)." in head
    assert (tmp_path / stored).read_text(encoding="utf-8") == stdout


def test_a_pager_prompt_at_the_end_adds_the_sentence(monkeypatch, tmp_path):
    _fake(monkeypatch, SupervisedRun(timed_out=True, stdout=_numbers(100) + WINDOWS_MORE_TAIL,
                                     killed="the command and its descendants were killed"))
    text = shell._execute_shell_command("x | more", None, 90, store_at=(tmp_path, TURN, CALL))
    assert ("The output ends at a pager prompt ('-- More  --'): the command was waiting for a key, "
            "and run_shell has no keyboard - drop the `| more` (or other pager) and read the stored "
            "output instead.") in text


def test_without_a_pager_prompt_there_is_no_pager_sentence(monkeypatch, tmp_path):
    _fake(monkeypatch, SupervisedRun(timed_out=True, stdout=_numbers(500),
                                     killed="the command and its descendants were killed"))
    text = shell._execute_shell_command("x", None, 90, store_at=(tmp_path, TURN, CALL))
    assert "pager prompt" not in text
    assert "[last 20 lines of stdout]\n480\n" in text


def test_with_nowhere_to_store_the_result_says_nothing_was_stored(monkeypatch):
    _fake(monkeypatch, SupervisedRun(timed_out=True, stdout=_numbers(100) + WINDOWS_MORE_TAIL,
                                     killed="the command and its descendants were killed"))
    text = shell._execute_shell_command("x | more", None, 90)
    assert "Full captured output" not in text
    assert "drop the `| more` (or other pager) and run it again without it." in text


def test_stderr_is_stored_and_tailed_beside_stdout(monkeypatch, tmp_path):
    err = "".join(f"warn {i}\n" for i in range(300))
    _fake(monkeypatch, SupervisedRun(timed_out=True, stdout="", stderr=err,
                                     killed="the command and its descendants were killed"))
    text = shell._execute_shell_command("x", None, 90, store_at=(tmp_path, TURN, CALL))
    assert f"stdout empty, stderr {len(err):,} chars in 300 lines." in text
    assert f"stderr at state/tool_results/{TURN}/{CALL}.stderr.txt" in text
    assert "[last 20 lines of stderr]\nwarn 280\n" in text


def test_run_shell_takes_the_address_the_loop_put_on_its_context(monkeypatch, tmp_path):
    _fake(monkeypatch, SupervisedRun(timed_out=True, stdout=_numbers(100),
                                     killed="the command and its descendants were killed"))
    ctx = ToolContext(agent_root=tmp_path)
    ctx.tool_output_turn, ctx.tool_output_name = TURN, CALL
    monkeypatch.setattr(shell, "_validate_command", lambda *a, **k: None)
    text = shell.run_shell(ctx, "x", timeout=90)
    assert f"stdout at state/tool_results/{TURN}/{CALL}.stdout.txt" in text


# ------------------------------------------------------------ a normal run is unchanged

@pytest.mark.parametrize("run, expected", [
    (SupervisedRun(returncode=0, stdout="hello\n"), "hello\n"),
    (SupervisedRun(returncode=0), "(no output)"),
    (SupervisedRun(returncode=3, stdout="a\n", stderr="b\n"), "a\n\n[stderr]\nb\n\n[exit code: 3]"),
])
def test_a_run_that_was_not_killed_reads_as_before(monkeypatch, tmp_path, run, expected):
    _fake(monkeypatch, run)
    assert shell._execute_shell_command("x", None, 30, store_at=(tmp_path, TURN, CALL)) == expected
    assert not (tmp_path / "state").exists(), "a normal run stores nothing beside its result"


# ------------------------------------------------------------ the pieces

@pytest.mark.parametrize("tail", [
    "-- More  -- ", "-- More (12%) --", "--More--", "--More--(42%)", ":", "(END)", "\x1b[7m(END)\x1b[27m",
])
def test_the_three_pagers_prompts_are_recognised(tail):
    assert pager_prompt_at_end("line 1\nline 2\n" + tail) is not None


@pytest.mark.parametrize("text", ["", "done\n", "ratio: 3:1\n", "-- More is less --x", "see :\n more"])
def test_ordinary_endings_are_not_a_pager_prompt(text):
    assert pager_prompt_at_end(text) is None


def test_the_tail_is_bounded_by_lines_and_by_chars():
    assert output_tail(_numbers(100)) == "\n".join(str(i) for i in range(80, 100))
    assert len(output_tail("x" * 50_000)) == process_mod.TAIL_CHARS


def test_the_store_puts_the_streams_beside_the_calls_own_result(tmp_path):
    own = loop_mod._store_tool_output(tmp_path, TURN, 7, 0, "result")
    beside = loop_mod.store_beside_call(tmp_path, TURN, CALL, "stdout", "full")
    assert own == f"state/tool_results/{TURN}/r7-0.txt"
    assert beside == f"state/tool_results/{TURN}/r7-0.stdout.txt"
    assert loop_mod.store_beside_call(tmp_path, None, CALL, "stdout", "full") is None


# ------------------------------------------------------------ the loop names the call

def test_the_loop_gives_each_call_the_address_it_stores_that_call_under(tmp_path, monkeypatch):
    """What the tool reads off its context is the address of its own result file."""
    import asyncio

    class _Provider:
        _llm_manager = None

        def __init__(self):
            self.n = 0

        async def chat(self, messages, tools=None, reasoning_effort=None, **kwargs):
            self.n += 1
            usage = {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 0}
            if self.n > 2:
                return {"role": "assistant", "content": "done"}, usage
            calls = [{"id": f"c{self.n}-{i}", "function": {"name": "run_shell", "arguments": {"command": "x"}}}
                     for i in range(2)]
            return {"role": "assistant", "content": "", "tool_calls": calls}, usage

    class _Tools:
        _ctx = ToolContext(agent_root=tmp_path)

        def schemas(self, core_only=False, include_restricted=False):
            return [{"type": "function", "function": {"name": "run_shell", "parameters": {}}}]

        def get_timeout(self, name):
            return 10

    seen = {}

    async def fake_execute(tools, tc, logs_dir, timeout, task_id, ctx=None, **kwargs):
        seen[tc["id"]] = (ctx.tool_output_turn, ctx.tool_output_name)
        return {"result": "out", "is_error": False, "fn_name": "run_shell", "tool_call_id": tc["id"],
                "args_for_log": {}, "duration_ms": 1}

    monkeypatch.setattr(loop_mod, "load_agent_config", lambda _name: {"context_window": 10_000_000})
    monkeypatch.setattr(loop_mod, "_execute_with_timeout", fake_execute)
    asyncio.run(loop_mod.run_llm_loop(
        messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
        tools=_Tools(), llm=_Provider(), agent_root=tmp_path, emit_progress=lambda *a, **k: None,
        conversation_id="conv",
    ))

    stored = {p.relative_to(tmp_path).as_posix() for p in (tmp_path / loop_mod.TOOL_OUTPUT_DIR).rglob("*.txt")}
    named = {f"{loop_mod.TOOL_OUTPUT_DIR}/{turn}/{name}.txt" for turn, name in seen.values()}
    assert len(seen) == 4 and len(named) == 4
    assert named == stored
