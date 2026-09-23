"""The GAIA run's web tools refuse published answer keys; production tools do not.

On 2026-09-23 the benchmark agent searched «GAIA benchmark …» by name, opened
pages quoting the dataset row and copied the answer (A-GAIA-AGENT-GOES-LOOKING-
FOR-THE-ANSWER-KEY-AND-THE-RUN-COUNTS-WHAT-IT-COPIES). The refusal lives in
`eval/_harness/answer_key_policy.py` and wraps the handlers at the registry, one
run at a time; nothing under `dpc_client_core` changes.
"""

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[3] / "eval"
sys.path.insert(0, str(EVAL))

from _harness import answer_key_policy as policy  # noqa: E402
from dpc_client_core.dpc_agent.tools.registry import (  # noqa: E402
    ToolContext, ToolEntry, ToolRegistry)

DENIED = [
    "https://huggingface.co/datasets/gaia-benchmark/GAIA/discussions/26",
    "https://huggingface.co/datasets/gaia-benchmark/GAIA/discussions/11",
    "https://huggingface.co/datasets/Kevin355/Who_and_When/raw/main/Who%26When/Hand-Crafted/33.json",
    "https://github.com/harbor-framework/harbor-datasets/blob/main/datasets/gaia/72e110e7/instruction.md",
    "https://github.com/example-org/harbor-index/tree/main",
    "https://huggingface.co/datasets/cmriat/gaia/viewer/default/validation",
    "https://hal.cs.princeton.edu/reliability/benchmark/gaia/analysis/",
    "https://huggingface.co/datasets/Intelligent-Internet/GAIA-Subset-Benchmark/viewer",
    "https://huggingface.co/spaces/bstraehle/gaia/raw/main/data/gaia_validation.jsonl",
    "https://huggingface.co/ChromaFlow9897/chromaflow-gaia-benchmark/commit/05a6",
    "https://huggingface.co/spaces/olcapone/Final_Assignment/discussions/13/files",
    "https://raw.githubusercontent.com/apooravmalik/GAIA-AI-AGENT/main/metadata.jsonl",
    "https://raw.githubusercontent.com/patronus-ai/trail-benchmark/main/benchmarking/data/GAIA/4a.json",
    "https://www.google.com/search?q=GAIA+benchmark+bird+species",
    "https://duckduckgo.com/html/?q=bird%20species%20gaia",
]

ALLOWED = [
    "https://huggingface.co/datasets/squad",
    "https://huggingface.co/papers/2311.12983",
    "https://huggingface.co/BAAI/bge-m3",
    "https://en.wikipedia.org/wiki/Gaia_(spacecraft)",
    "https://www.youtube.com/watch?v=L1vXCYZAYYM",
    "file:///C:/Users/x/AppData/Local/Temp/dpc-gaia-23qubap5/task-017/gaia-files/cca530fc.png",
    "http://127.0.0.1:8765/gaia-files/9318445f-fe6a-4e1b-acbf-c68228c9906a.jsonld",
]


@pytest.mark.parametrize("url", DENIED)
def test_an_answer_key_url_is_denied(url):
    assert policy.is_answer_key_url(url)


@pytest.mark.parametrize("url", ALLOWED)
def test_the_rest_of_the_web_and_the_tasks_own_files_are_not(url):
    assert not policy.is_answer_key_url(url)


@pytest.mark.parametrize("query,named", [
    ('GAIA benchmark "DDC 633" Bielefeld', True),
    ("gaia question scikit-learn changelog", True),
    ("westernmost city presidents born GAIA", True),
    ("GAIA_benchmark answers", True),
    ("Gaiaan dialect", False),
    ("University of Leicester fish bag volume", False),
])
def test_a_query_naming_gaia(query, named):
    assert policy.names_gaia(query) is named


# --- wrapped handlers, through the real registry -----------------------------------


def _registry(tmp_path, **handlers):
    reg = ToolRegistry(agent_root=tmp_path)
    for name, fn in handlers.items():
        reg.register(ToolEntry(name=name, schema={"name": name}, handler=fn))
    return reg


def _ctx(tmp_path, task="task-003"):
    root = tmp_path / task
    root.mkdir(exist_ok=True)
    return ToolContext(agent_root=root)


def test_a_refused_fetch_never_reaches_the_handler_and_is_recorded(tmp_path):
    calls = []
    reg = _registry(tmp_path, fetch_json=lambda ctx, url, offset=0: calls.append(url) or "{}")
    ak = policy.AnswerKeyPolicy()
    ak.install(reg)

    out = reg.execute("fetch_json", {"url": DENIED[2]}, ctx=_ctx(tmp_path))

    assert out.startswith(policy.BLOCK_PREFIX) and calls == []
    assert ak.summary()["refusals"] == 1
    assert ak.events == [{"task": "task-003", "tool": "fetch_json", "kind": "refused_url",
                          "url": DENIED[2]}]


def test_an_allowed_fetch_passes_through_unchanged(tmp_path):
    reg = _registry(tmp_path, fetch_json=lambda ctx, url, offset=0: f"body of {url}")
    policy.AnswerKeyPolicy().install(reg)

    assert reg.execute("fetch_json", {"url": ALLOWED[0]}, ctx=_ctx(tmp_path)) == \
        f"body of {ALLOWED[0]}"


def test_a_search_naming_gaia_is_refused_and_an_ordinary_one_is_not(tmp_path):
    async def search(ctx, query, max_results=5, backend="auto"):
        return f"Search results for '{query}' (0 found, backend=auto):\n"
    reg = _registry(tmp_path, search_web=search)
    ak = policy.AnswerKeyPolicy()
    ak.install(reg)

    refused = reg.execute("search_web", {"query": "GAIA benchmark penguin video"},
                          ctx=_ctx(tmp_path))
    fine = reg.execute("search_web", {"query": "penguin video"}, ctx=_ctx(tmp_path))

    assert refused.startswith(policy.BLOCK_PREFIX)
    assert fine.startswith("Search results for 'penguin video'")
    assert [e["kind"] for e in ak.events] == ["refused_query"]


def test_answer_key_items_are_dropped_from_search_results(tmp_path):
    listing = ("Search results for 'bird species' (3 found, backend=auto):\n\n"
               "  1. Cornell Lab\n     https://www.allaboutbirds.org/x\n     species list\n\n"
               "  2. GAIA discussion\n     https://huggingface.co/datasets/gaia-benchmark/GAIA/"
               "discussions/11\n     answer quoted here\n\n"
               "  3. HAL\n     https://example.org/q\n     Expected answer: something")

    async def search(ctx, query, max_results=5, backend="auto"):
        return listing
    reg = _registry(tmp_path, search_web=search)
    ak = policy.AnswerKeyPolicy()
    ak.install(reg)

    out = reg.execute("search_web", {"query": "bird species"}, ctx=_ctx(tmp_path))

    assert "allaboutbirds" in out
    assert "gaia-benchmark" not in out and "Expected answer" not in out
    assert "2 result(s) withheld" in out
    assert [e["kind"] for e in ak.events] == ["withheld_result", "withheld_result"]


def test_a_result_carrying_dataset_rows_is_withheld(tmp_path):
    reg = _registry(tmp_path, fetch_json=lambda ctx, url, offset=0:
                    '[{"task_id": "x", "Question": "q", "Final answer": "…"}]')
    ak = policy.AnswerKeyPolicy()
    ak.install(reg)

    out = reg.execute("fetch_json", {"url": "https://example.org/data.json"}, ctx=_ctx(tmp_path))

    assert out.startswith(policy.BLOCK_PREFIX) and "Final answer" not in out
    assert ak.events[0]["kind"] == "withheld_output"


def test_a_shell_command_naming_a_mirror_is_refused(tmp_path):
    ran = []
    reg = _registry(tmp_path, run_shell=lambda ctx, command, timeout=120, cwd="":
                    ran.append(command) or "ok")
    policy.AnswerKeyPolicy().install(reg)

    refused = reg.execute("run_shell", {"command": f'curl -s "{DENIED[2]}"'}, ctx=_ctx(tmp_path))
    fine = reg.execute("run_shell", {
        "command": r'cd "C:\t\dpc-gaia-23qubap5\task-025\gaia-files" && dir'}, ctx=_ctx(tmp_path))

    assert refused.startswith(policy.BLOCK_PREFIX)
    assert fine == "ok" and len(ran) == 1


def test_a_browser_page_that_became_a_mirror_is_withheld(tmp_path):
    """A click inside an open session can land on an answer list."""
    page = {"url": "https://huggingface.co/datasets/x"}

    async def click(ctx, ref_or_selector, timeout=30000):
        page["url"] = DENIED[0]
        return "clicked; snapshot of the thread"

    async def snapshot(ctx, raw=False):
        return "snapshot"

    async def page_url(ctx):
        return page["url"]
    reg = _registry(tmp_path, browser_click=click, browser_snapshot=snapshot)
    ak = policy.AnswerKeyPolicy(page_url=page_url)
    ak.install(reg)

    after = reg.execute("browser_click", {"ref_or_selector": "@e4"}, ctx=_ctx(tmp_path))
    before = reg.execute("browser_snapshot", {}, ctx=_ctx(tmp_path))

    assert after.startswith(policy.BLOCK_PREFIX) and "thread" not in after
    assert before.startswith(policy.BLOCK_PREFIX)
    assert [e["kind"] for e in ak.events] == ["withheld_page", "refused_page"]


def test_the_page_check_fails_open_when_no_session_is_readable(tmp_path):
    async def snapshot(ctx, raw=False):
        return "snapshot"
    reg = _registry(tmp_path, browser_snapshot=snapshot)
    policy.AnswerKeyPolicy().install(reg)   # the real reader, no session exists

    assert reg.execute("browser_snapshot", {}, ctx=_ctx(tmp_path)) == "snapshot"


# --- the real tools ------------------------------------------------------------------


def test_every_web_tool_the_benchmark_enables_is_wrapped_and_keeps_its_signature(tmp_path):
    from _harness import benchmark_tools
    reg = ToolRegistry(agent_root=tmp_path)
    originals = {n: e.handler for n, e in reg._entries.items()}

    wrapped = set(policy.AnswerKeyPolicy().install(reg))

    web = {n for n in benchmark_tools.BENCHMARK_TOOLS if n in policy.WRAPPED_TOOLS}
    assert web - set(reg._entries) == set(), "a listed web tool is not registered"
    assert wrapped == web
    for name in wrapped:
        handler = reg._entries[name].handler
        assert handler.__wrapped__ is originals[name]
        assert inspect.signature(handler) == inspect.signature(originals[name])
        assert inspect.iscoroutinefunction(handler) == inspect.iscoroutinefunction(originals[name])


def test_the_real_fetch_json_refuses_before_any_network(tmp_path, monkeypatch):
    from dpc_client_core.dpc_agent.tools import browser
    monkeypatch.setattr(browser, "_fetch_url", lambda *a, **k: pytest.fail("network reached"))
    reg = ToolRegistry(agent_root=tmp_path)
    policy.AnswerKeyPolicy().install(reg)

    out = reg.execute("fetch_json", {"url": DENIED[2]}, ctx=_ctx(tmp_path))

    assert out.startswith(policy.BLOCK_PREFIX)


def test_an_argument_alias_still_resolves_through_the_wrapper(tmp_path):
    """The registry reads the handler's signature to rename `file_path` to `path`."""
    def reader(ctx, path):
        return f"read {path}"
    reg = _registry(tmp_path, fetch_json=reader)   # any wrapped name will do
    policy.AnswerKeyPolicy().install(reg)

    assert reg.execute("fetch_json", {"file_path": "a.txt"}, ctx=_ctx(tmp_path)) == "read a.txt"


def test_installing_twice_does_not_wrap_twice(tmp_path):
    reg = _registry(tmp_path, fetch_json=lambda ctx, url: "x")
    ak = policy.AnswerKeyPolicy()

    assert "fetch_json" in ak.install(reg)
    assert "fetch_json" not in ak.install(reg)


def test_a_production_registry_is_untouched(tmp_path):
    """Nothing is wrapped unless a benchmark installs the policy."""
    reg = ToolRegistry(agent_root=tmp_path)

    assert not hasattr(reg._entries["search_web"].handler, "_answer_key_policy")


def test_provenance_names_the_policy_by_version_and_digest():
    d = policy.describe()

    assert d["version"] == policy.POLICY_VERSION
    assert len(d["sha256"]) == 64 and d["sha256"] == policy.describe()["sha256"]
    assert "run_shell" in d["not_covered"]
