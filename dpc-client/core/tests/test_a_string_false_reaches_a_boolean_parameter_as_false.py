"""A tool argument is read as the type its schema declares, not as whatever the model sent.

Local models (Warren and Johnny on llama.cpp, 2026-10-21) send booleans as the strings
"True" and "False". The registry passed them straight to the handler, and a handler that
tests truthiness ran "False" as True: list_dir recursed, browser_snapshot returned the raw
tree, and the log recorded a success. The schema held the type all along; the registry
now reads it between alias resolution and the handler call.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from dpc_client_core.dpc_agent.tools import browser
from dpc_client_core.dpc_agent.tools.registry import (
    ToolContext,
    ToolEntry,
    ToolRegistry,
    coerce_args_to_schema,
)


@pytest.fixture
def registry(tmp_path):
    reg = ToolRegistry(agent_root=tmp_path)
    reg.set_context(ToolContext(agent_root=tmp_path))
    return reg


def _probe(registry, properties):
    """Register a tool that records what its handler received."""
    seen = []

    def handler(ctx, **kwargs):
        seen.append(kwargs)
        return "ran"

    registry.register(ToolEntry(
        name="probe",
        schema={"name": "probe", "description": "",
                "parameters": {"type": "object", "properties": properties}},
        handler=handler,
    ))
    return seen


def _run(registry, args):
    return registry.execute("probe", args, ctx=registry._ctx)


def test_a_string_false_reaches_a_boolean_parameter_as_false(registry):
    seen = _probe(registry, {"flag": {"type": "boolean"}})
    assert _run(registry, {"flag": "False"}) == "ran"
    assert seen[-1]["flag"] is False
    assert _run(registry, {"flag": " false "}) == "ran"
    assert seen[-1]["flag"] is False


def test_a_string_true_reaches_a_boolean_parameter_as_true(registry):
    seen = _probe(registry, {"flag": {"type": "boolean"}})
    _run(registry, {"flag": "True"})
    assert seen[-1]["flag"] is True


def test_a_word_that_is_not_a_boolean_is_refused_and_the_handler_never_runs(registry):
    seen = _probe(registry, {"flag": {"type": "boolean"}})
    answer = _run(registry, {"flag": "maybe"})
    assert answer.startswith("⚠️ TOOL_ARG_ERROR (probe)")
    assert "'flag'" in answer and "boolean" in answer and "string" in answer
    assert seen == []


def test_a_one_for_a_boolean_is_refused_rather_than_guessed(registry):
    seen = _probe(registry, {"flag": {"type": "boolean"}})
    assert "TOOL_ARG_ERROR" in _run(registry, {"flag": "1"})
    assert "TOOL_ARG_ERROR" in _run(registry, {"flag": 1})
    assert seen == []


def test_a_numeric_string_reaches_an_integer_parameter_as_an_int(registry):
    seen = _probe(registry, {"limit": {"type": "integer"}, "ratio": {"type": "number"}})
    _run(registry, {"limit": "3", "ratio": "0.5"})
    assert seen[-1] == {"limit": 3, "ratio": 0.5}
    assert type(seen[-1]["limit"]) is int


def test_a_boolean_is_never_turned_into_an_integer(registry):
    seen = _probe(registry, {"limit": {"type": "integer"}})
    answer = _run(registry, {"limit": True})
    assert "TOOL_ARG_ERROR" in answer and "'limit'" in answer and "boolean" in answer
    assert seen == []


def test_a_page_range_for_a_string_parameter_stays_a_string(registry):
    # read_document's own schema: pages is a string, so the look of "3" decides nothing.
    doc = registry._entries["read_document"].schema["parameters"]["properties"]["pages"]
    assert doc["type"] == "string"
    seen = _probe(registry, {"pages": doc})
    _run(registry, {"pages": "1-5"})
    assert seen[-1]["pages"] == "1-5"
    _run(registry, {"pages": "3"})
    assert seen[-1]["pages"] == "3"


def test_a_none_and_an_undeclared_argument_pass_untouched():
    schema = {"parameters": {"properties": {"flag": {"type": "boolean"}, "free": {}}}}
    args = {"flag": None, "free": "False", "extra": "False"}
    out, error = coerce_args_to_schema(schema, args, "t")
    assert error is None and out == args


def test_a_json_string_reaches_an_array_parameter_and_other_strings_are_left_to_the_handler():
    schema = {"parameters": {"properties": {"files": {"type": "array", "items": {"type": "string"}}}}}
    out, error = coerce_args_to_schema(schema, {"files": '["a.py", "b.py"]'}, "git_add")
    assert error is None and out["files"] == ["a.py", "b.py"]
    out, error = coerce_args_to_schema(schema, {"files": "a.py"}, "git_add")
    assert error is None and out["files"] == "a.py"


def test_a_union_with_string_keeps_a_string_as_sent():
    schema = {"parameters": {"properties": {"x": {"anyOf": [{"type": "integer"}, {"type": "string"}]}}}}
    out, error = coerce_args_to_schema(schema, {"x": "7"}, "t")
    assert error is None and out["x"] == "7"


def test_every_coercion_is_logged_once_per_call(registry, caplog):
    _probe(registry, {"flag": {"type": "boolean"}, "limit": {"type": "integer"}})
    with caplog.at_level(logging.INFO, logger="dpc_client_core.dpc_agent.tools.registry"):
        _run(registry, {"flag": "False", "limit": "3"})
    lines = [r.getMessage() for r in caplog.records if "coerced to the schema" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].startswith("Tool probe:")
    assert "flag 'False' (string) -> False (boolean)" in lines[0]
    assert "limit '3' (string) -> 3 (integer)" in lines[0]


def test_list_dir_with_a_string_false_does_not_recurse(registry, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.txt").write_text("x", encoding="utf-8")
    answer = registry.execute("list_dir", {"path": ".", "recursive": "False"}, ctx=registry._ctx)
    assert "sub/" in answer
    assert "inner.txt" not in answer
    deep = registry.execute("list_dir", {"path": ".", "recursive": "True"}, ctx=registry._ctx)
    assert "inner.txt" in deep


def test_browser_screenshot_full_page_true_as_a_string_reaches_the_browser_as_true(
    registry, monkeypatch,
):
    calls = []

    async def fake_run_in_session(session, method_name, *args, **kwargs):
        calls.append((method_name, args))

    monkeypatch.setattr(browser, "_get_session_or_error", lambda agent_id: object())
    monkeypatch.setattr(browser, "_get_session_lock", lambda agent_id: asyncio.Lock())
    monkeypatch.setattr(browser, "_run_in_session", fake_run_in_session)

    answer = registry.execute("browser_screenshot", {"full_page": "True"}, ctx=registry._ctx)
    assert "Saved screenshot" in answer
    assert calls[-1][0] == "screenshot" and calls[-1][1][0] is True

    registry.execute("browser_screenshot", {"full_page": "False"}, ctx=registry._ctx)
    assert calls[-1][1][0] is False
