"""A local engine is asked for its tokens and its window; sleep never guesses for it.

Sleep synthesis on the Strata alias sent a 262 410-token prompt into a 261 120
window (2026-11-09): the budget counted with chars/4 (about a third short on
Cyrillic JSON) against the 250 000 of providers.json. Mike's call: for a local
engine the count comes from the engine, the window comes from the engine, one
gate decides (the window less the answer reserve, no margin factor), and when
the engine cannot be asked the sleep stops instead of estimating.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from dpc_client_core.dpc_agent import sleep_pipeline as sleep_mod
from dpc_client_core.dpc_agent.sleep_pipeline import (
    _fit_prompt_on_the_engine,
    _select_findings_on_the_engine,
    _sleep_failure_reason,
    run_sleep,
)
from dpc_client_core.managers import llama_server_supervisor as supervisor_mod
from dpc_client_core.providers.base import AIProvider, EngineCountError
from dpc_client_core.providers.llamacpp_server_provider import (
    _ACTIVE_SUPERVISORS,
    LlamaServerProvider,
)
from dpc_client_core.providers.strata_provider import StrataProvider

PORT = 51234
AGENT = "agent_engine_test"
REPLY = json.dumps({"summary": "s", "morning_brief": {"summary": "brief"}, "sleep_findings": {}})
TEMPLATE_SHARE = 52  # what the fake engines' chat template adds to one user turn


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(supervisor_mod, "DPC_HOME", tmp_path / "dpc-home")
    _ACTIVE_SUPERVISORS.clear()
    sleep_mod._SLEEP_PIPELINE_LOCK = asyncio.Lock()
    monkeypatch.setattr(sleep_mod, "_export_graph_snapshot", lambda *a, **k: None)
    from dpc_client_core.dpc_agent import knowledge_graph
    monkeypatch.setattr(knowledge_graph.KnowledgeGraph, "extract_entities_gliner",
                        lambda self, texts: [], raising=False)
    yield
    _ACTIVE_SUPERVISORS.clear()


def _conversation(tmp_path: Path, sessions: int = 1, monthly: bool = False) -> Path:
    conv = tmp_path / "conversations" / AGENT
    (conv / "archive").mkdir(parents=True)
    rows = []
    for n in range(1, sessions + 1):
        day = f"2026-{7 + n:02d}-01" if monthly else f"2026-10-{n:02d}"
        rows.append({"archive_file": f"a{n}.json", "date": f"{day}T10:00:00", "message_count": 2})
        (conv / "archive" / f"a{n}.json").write_text(json.dumps({"messages": [
            {"role": "user", "content": f"plan the trip {n}"},
            {"role": "assistant", "content": "done"},
        ]}), encoding="utf-8")
    (conv / "digest.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return conv


# --- the providers, built by their own __init__, over a mocked engine -----------------


class _FakeSupervisor:
    """What a provider reads of its child: a port, /props and a live process."""

    def __init__(self, props=None, fail=None):
        self.port = PORT
        self.props = props
        self._proc = SimpleNamespace(returncode=None) if props else None
        self.fail = fail

    async def ensure_running(self):
        if self.fail is not None:
            raise self.fail
        return self.props


def _provider(cls, tmp_path, **kw):
    if cls is StrataProvider:
        config = {"type": "strata", "model": "qwen-strata",
                  "strata_base_config": str(tmp_path / "strata-base.json"),
                  "conversation_cache_mib": 1024}
    else:
        config = {"type": "llamacpp_server", "model": "qwen", "gguf_path": "D:/m/x.gguf"}
    provider = cls("local", config)
    provider.supervisor = _FakeSupervisor(**kw)
    return provider


def _engine_http(monkeypatch, handler, seen_timeouts=None):
    """Every httpx.AsyncClient the providers open goes through `handler`."""
    real = httpx.AsyncClient

    def client(*a, **k):
        if seen_timeouts is not None:
            seen_timeouts.append(k.get("timeout"))
        return real(transport=httpx.MockTransport(handler), **k)

    monkeypatch.setattr(httpx, "AsyncClient", client)


def test_only_a_local_engine_provider_counts_on_its_engine(tmp_path):
    assert AIProvider.counts_on_its_engine(SimpleNamespace()) is False
    assert _provider(LlamaServerProvider, tmp_path).counts_on_its_engine() is True
    assert _provider(StrataProvider, tmp_path).counts_on_its_engine() is True


@pytest.mark.asyncio
async def test_llama_server_text_count_is_tokenize_and_as_prompt_adds_the_template(tmp_path, monkeypatch):
    """One token per character; the engine's template wraps a user turn in 52 more."""
    seen, timeouts = [], []

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path == "/apply-template":
            wrapped = "T" * 20 + body["messages"][0]["content"] + "E" * 32
            return httpx.Response(200, json={"prompt": wrapped})
        return httpx.Response(200, json={"tokens": list(range(len(body["content"])))})

    _engine_http(monkeypatch, handler, timeouts)
    llama = _provider(LlamaServerProvider, tmp_path, props={})

    assert await llama.engine_token_count("Привет") == 6
    assert seen == [("/tokenize", {"content": "Привет"})]
    seen.clear()
    assert await llama.engine_token_count("Привет", as_prompt=True) == 6 + TEMPLATE_SHARE
    assert [path for path, _ in seen] == ["/apply-template", "/tokenize"]
    assert seen[0][1] == {"messages": [{"role": "user", "content": "Привет"}]}
    assert set(timeouts) == {60.0}  # a count is not a generation


@pytest.mark.asyncio
async def test_strata_text_count_is_the_text_and_as_prompt_is_the_whole_turn(tmp_path, monkeypatch):
    """Strata has no raw tokenize: it counts the rendered prompt of one user turn, and
    the empty turn's tokens are the template's, so pieces add up to their sum."""
    asked = []

    def handler(request):
        body = json.loads(request.content)
        asked.append((request.url.path, body["messages"][0]["content"]))
        return httpx.Response(200, json={"input_tokens": TEMPLATE_SHARE + len(body["messages"][0]["content"])})

    _engine_http(monkeypatch, handler)
    strata = _provider(StrataProvider, tmp_path, props={})

    assert await strata.engine_token_count("abcd") == 4
    assert await strata.engine_token_count("abcd", as_prompt=True) == 4 + TEMPLATE_SHARE
    assert await strata.engine_token_count("xy") == 2
    assert {path for path, _ in asked} == {"/v1/messages/count_tokens"}
    assert [text for _, text in asked].count("") == 1  # the empty turn is asked once


@pytest.mark.asyncio
async def test_strata_asks_the_empty_turn_again_after_a_restart_or_a_model_change(tmp_path, monkeypatch):
    template = {"share": TEMPLATE_SHARE}
    asked = []

    def handler(request):
        text = json.loads(request.content)["messages"][0]["content"]
        asked.append(text)
        return httpx.Response(200, json={"input_tokens": template["share"] + len(text)})

    _engine_http(monkeypatch, handler)
    strata = _provider(StrataProvider, tmp_path, props={"boot": 1})
    assert await strata.engine_token_count("abcd") == 4

    # The supervisor restarted the child on the same port with another template.
    template["share"] = 80
    strata.supervisor.props = {"boot": 2}
    assert await strata.engine_token_count("abcd") == 4
    assert asked.count("") == 2

    strata.model = "another-model"
    assert await strata.engine_token_count("abcd") == 4
    assert asked.count("") == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", [LlamaServerProvider, StrataProvider])
async def test_the_window_is_the_n_ctx_the_engine_reports(tmp_path, cls):
    props = {"default_generation_settings": {"n_ctx": 261120}}

    assert await _provider(cls, tmp_path, props=props).engine_context_window() == 261120


@pytest.mark.asyncio
async def test_a_window_the_engine_does_not_state_is_a_window_error_not_a_default(tmp_path):
    provider = _provider(LlamaServerProvider, tmp_path, props={"default_generation_settings": {}})

    with pytest.raises(EngineCountError, match="not guessing") as caught:
        await provider.engine_context_window()
    assert caught.value.kind == "window"


@pytest.mark.asyncio
async def test_an_engine_that_cannot_be_started_is_an_error_for_both_questions(tmp_path):
    provider = _provider(LlamaServerProvider, tmp_path, fail=RuntimeError("card held"))

    with pytest.raises(EngineCountError, match="card held") as window:
        await provider.engine_context_window()
    with pytest.raises(EngineCountError, match="card held") as count:
        await provider.engine_token_count("x")
    assert (window.value.kind, count.value.kind) == ("window", "count")


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(404, json={"error": {"message": "not found"}}),
    httpx.Response(200, json={"no": "tokens"}),
    httpx.Response(200, text="not json"),
])
async def test_an_engine_answer_that_is_not_a_count_is_an_error(tmp_path, monkeypatch, response):
    _engine_http(monkeypatch, lambda request: response)

    with pytest.raises(EngineCountError, match="not guessing"):
        await _provider(LlamaServerProvider, tmp_path, props={}).engine_token_count("x")


# --- the budget over an engine ---------------------------------------------------------


class _Engine:
    """An engine whose tokens are two per character, far from chars/4. `whole` may
    replace the count of an `as_prompt` call."""

    model = "qwen"

    def __init__(self, window=100_000, fail=None, whole=None):
        self.window = window
        self.fail = fail
        self.whole = whole
        self.asked = []

    def counts_on_its_engine(self):
        return True

    async def engine_context_window(self):
        if self.fail:
            raise self.fail
        return self.window

    async def engine_token_count(self, text, *, as_prompt=False):
        if self.fail:
            raise self.fail
        self.asked.append((len(text), as_prompt))
        return self.whole(text) if as_prompt and self.whole else 2 * len(text)


def _findings(n, size=1000):
    return [{"digest_date": f"2026-10-{day:02d}", "summary": "x" * size} for day in range(n, 0, -1)]


@pytest.mark.asyncio
async def test_findings_are_selected_against_the_one_limit_with_no_margin_factor():
    engine = _Engine()
    findings = _findings(30)
    window, overhead = 20_000, 500

    selected, counts = await _select_findings_on_the_engine(findings, engine, window, overhead)

    gate = sleep_mod._engine_prompt_limit(window) - overhead
    assert gate == window - sleep_mod._synthesis_output_reserve(window) - overhead
    assert 1 <= len(selected) < len(findings) and selected[0] is findings[0]
    assert len(counts) == len(selected) and sum(counts) <= gate < sum(counts) + 2 * 1100
    # The cloud budget keeps the 0.85 factor and would have stopped sooner.
    assert sum(counts) > sleep_mod._compute_synthesis_budget(window, overhead)
    assert all(as_prompt is False for _, as_prompt in engine.asked)


@pytest.mark.asyncio
async def test_the_assembled_prompt_is_cut_oldest_first_until_it_fits():
    engine = _Engine()
    findings = _findings(10)

    # 10 findings are 20 000 engine tokens; a 16 000 window leaves 12 000 after the reserve.
    prompt, kept, tokens = await _fit_prompt_on_the_engine(
        findings, lambda k: "".join(f["summary"] for f in k), engine, 16_000)

    assert tokens <= sleep_mod._engine_prompt_limit(16_000)
    assert kept == findings[:len(kept)] and 0 < len(kept) < 10
    assert tokens == 2 * len(prompt)


@pytest.mark.asyncio
async def test_a_prompt_that_fits_is_left_whole():
    engine = _Engine()
    findings = _findings(3)

    _, kept, _ = await _fit_prompt_on_the_engine(
        findings, lambda k: "".join(f["summary"] for f in k), engine, 100_000)

    assert kept == findings and len(engine.asked) == 1


@pytest.mark.asyncio
async def test_one_finding_that_cannot_fit_stops_the_sleep_as_a_fit_error():
    with pytest.raises(EngineCountError, match="answer reserve") as caught:
        await _fit_prompt_on_the_engine(
            _findings(1, size=50_000), lambda k: k[0]["summary"], _Engine(), 20_000)
    assert caught.value.kind == "fit"


# --- run_sleep -----------------------------------------------------------------------------


class _Manager:
    def __init__(self, provider, window_in_providers_json=250_000):
        self.providers = {"engine": provider}
        self.default_provider = "engine"
        self.token_count_manager = None
        self.prompts = []
        self.estimated = []
        self._window = window_in_providers_json

    async def query(self, prompt, provider_alias=None, **kwargs):
        self.prompts.append((prompt, kwargs))
        return REPLY

    def count_tokens(self, text, model):
        self.estimated.append(len(text))
        return len(text) // 4

    def alias_context_window(self, alias):
        return self._window


@pytest.mark.asyncio
async def test_sleep_on_a_local_engine_sizes_the_prompt_by_the_engine_and_not_by_estimate(tmp_path):
    engine = _Engine(window=300_000)
    manager = _Manager(engine)

    result = await run_sleep(_conversation(tmp_path), manager, provider_alias="engine")

    assert result["status"] == "completed", result
    assert manager.estimated == []                      # chars/4 was not consulted
    synthesis, kwargs = manager.prompts[-1]
    assert (len(synthesis), True) in engine.asked        # the sent prompt was counted whole
    # The reserve is taken from the engine's window, not providers.json's 250 000.
    assert kwargs["max_tokens"] == sleep_mod._synthesis_output_reserve(300_000)


@pytest.mark.asyncio
async def test_the_brief_records_the_whole_prompt_after_the_trim_against_the_one_limit(tmp_path):
    """Three sessions in three months are 36 050 engine tokens on a 40 000 window (limit
    36 000), raw and as month summaries alike: the oldest month goes, and what the brief
    says is the post-trim prompt, not the pre-trim sum."""
    def whole(text):
        return 12_000 * len(re.findall(r"^(?:Session \d+|Month \d{4}-\d{2}) \(", text, re.M)) + 50

    engine = _Engine(window=40_000, whole=whole)
    manager = _Manager(engine)

    result = await run_sleep(
        _conversation(tmp_path, sessions=3, monthly=True), manager, provider_alias="engine")

    assert result["status"] == "completed", result
    info = result["morning_brief"]["synthesis_budget"]
    assert info["total_findings"] == 3 and info["included_findings"] == 2
    assert info["budget_tokens_available"] == 36_000      # the one limit, no 0.85
    assert info["prompt_tokens_on_engine"] == 24_050
    assert info["budget_tokens_used"] == 24_050
    assert 0 < info["findings_tokens_on_engine"] < info["budget_tokens_used"]
    assert info["month_summaries"]["summaries_left_out"] == 1


@pytest.mark.asyncio
async def test_sleep_on_an_engine_that_cannot_be_asked_leaves_the_state_awake_with_the_error(tmp_path):
    conv = _conversation(tmp_path)
    engine = _Engine(fail=EngineCountError("'engine': not running; not guessing", kind="window"))
    manager = _Manager(engine)

    result = await run_sleep(conv, manager, provider_alias="engine")

    assert result["status"] == "error"
    assert "did not report its window" in result["reason"]
    state = json.loads((conv / sleep_mod.SLEEP_STATE_FILE).read_text(encoding="utf-8"))
    assert state["status"] == "awake"
    assert "not guessing" in state["last_error"] and state["error_at"]
    # The per-session analysis ran before the budget; the synthesis (the one with an
    # answer ceiling) was never sent.
    assert manager.estimated == []
    assert not any("max_tokens" in kwargs for _, kwargs in manager.prompts)


@pytest.mark.asyncio
async def test_sleep_on_a_provider_with_no_engine_counts_as_before(tmp_path):
    manager = _Manager(SimpleNamespace(model="cloud-model"), window_in_providers_json=32_000)

    result = await run_sleep(_conversation(tmp_path), manager, provider_alias="engine")

    assert result["status"] == "completed", result
    assert manager.estimated
    info = result["morning_brief"]["synthesis_budget"]
    assert "prompt_tokens_on_engine" not in info
    # The estimate path keeps the 0.85 factor: its budget is under the engine's one limit.
    assert info["budget_tokens_available"] < 32_000 - sleep_mod._synthesis_output_reserve(32_000)


@pytest.mark.parametrize("kind, words", [
    ("window", "did not report its window"),
    ("count", "could not count the prompt's tokens"),
    ("fit", "does not fit"),
])
def test_each_engine_failure_has_its_own_plain_clause(kind, words):
    exc = EngineCountError("raw", kind=kind)

    reason = _sleep_failure_reason(exc, str(exc), alias="flash", synthesis_dump=None,
                                   synthesis_unparsable=False)

    assert words in reason and "flash" in reason and "EngineCountError" not in reason


def test_a_card_refusal_behind_the_count_error_speaks_in_its_own_words():
    refusal = supervisor_mod.CardHeldError("the card is held by other", "other", "strata")
    exc = EngineCountError("wrapped", kind="window")
    exc.__cause__ = refusal

    reason = _sleep_failure_reason(exc, str(exc), alias="flash", synthesis_dump=None,
                                   synthesis_unparsable=False)

    assert "the card is held by other" in reason
