"""The llamacpp_server provider: the template's dictionary, the budget, the child.

ADR-040 route b2, step 3. These tests pin the thinking dialect without an
engine: the effort words travel verbatim from the model's own dictionary
(`low/medium/xhigh`), the fleet's `high`/`max` are translated onto `xhigh`
loudly, `off` becomes `enable_thinking: false`, and `reasoning_budget_tokens`
rides per-request on top of the alias config — capping the template's default
thinking too, not only the named efforts. The lifecycle half: the client is
lazy behind the supervisor, a reload adopts a live child with unchanged flags,
and close() drains.
"""

import asyncio
import json
from types import SimpleNamespace

from dpc_client_core.providers import llamacpp_server_provider

import pytest

from dpc_client_core.llm_manager import PROVIDER_MAP
from dpc_client_core.managers.llama_server_supervisor import (
    LlamaServerError,
    LlamaServerSupervisor,
)
from dpc_client_core.providers import LlamaServerProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS, _flags_of

GGUF = "D:/models/qwen3.8-27b-Q4_K_M.gguf"


async def _noop_stop():
    return None


def _provider(**overrides):
    config = {"type": "llamacpp_server", "gguf_path": GGUF}
    config.update(overrides)
    return LlamaServerProvider("local_qwen38", config)


class _FakeSupervisor:
    """The three things the provider asks of a supervisor, counted."""

    def __init__(self, port=8123):
        self.port = port
        self.props = {"total_slots": 4}
        self.ensure_calls = 0
        self.slot_enters = 0
        self.drained_with = None

    async def ensure_running(self):
        self.ensure_calls += 1
        return self.props

    def call_slot(self):
        self.slot_enters += 1
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def drain(self, timeout=0.0):
        self.drained_with = timeout


class _FakeCompletions:
    def __init__(self, resp):
        self.resp = resp
        self.bodies = []

    async def create(self, **params):
        self.bodies.append(params)
        return self.resp


def _fake_client(resp):
    completions = _FakeCompletions(resp)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, completions


def _chat_resp(content="ok", reasoning=None, tool_calls=None, usage=None, finish_reason="stop"):
    # `finish_reason` sits on the choice on every real response and the double
    # carried no such field, so a reader of these tests could not tell that
    # the provider was throwing it away.
    msg = SimpleNamespace(
        content=content, reasoning_content=reasoning, tool_calls=tool_calls
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason=finish_reason)],
        usage=usage or SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    _ACTIVE_SUPERVISORS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()


class TestConstruction:
    def test_a_gguf_path_is_required_and_a_missing_one_is_a_valueerror(self):
        # The manager catches ValueError/KeyError on load, so a misconfigured
        # alias is skipped with a log line, not a dead backend.
        with pytest.raises(ValueError, match="gguf_path"):
            LlamaServerProvider("broken", {"type": "llamacpp_server"})

    def test_no_api_key_is_required(self):
        _provider()

    def test_the_type_is_in_the_provider_map(self):
        assert PROVIDER_MAP["llamacpp_server"] is LlamaServerProvider

    def test_the_provider_registers_its_supervisor_by_alias(self):
        p = _provider()
        assert _ACTIVE_SUPERVISORS["local_qwen38"] is p.supervisor

    def test_a_reload_with_unchanged_flags_adopts_the_live_child(self):
        first = _provider(n_ctx=131072)
        # Adoption is for a child that is actually serving; a supervisor that
        # never started has nothing to adopt and is rebuilt freely.
        first.supervisor.props = {"total_slots": 1}
        second = _provider(n_ctx=131072)
        assert second.supervisor is first.supervisor

    def test_adoption_carries_the_new_non_flag_values_onto_the_kept_child(self):
        # `start_timeout_s` never reaches the command line, so a change to it
        # alone leaves the flag tuple equal and the child adopted — which used
        # to mean the typed value went nowhere, because adoption kept the old
        # supervisor object and its old config with it.
        first = _provider(start_timeout_s=300.0)
        first.supervisor.props = {"total_slots": 1}
        flags_before = _flags_of(first.supervisor.config)

        second = _provider(start_timeout_s=900.0)

        assert second.supervisor is first.supervisor, "a non-flag change must not re-load the model"
        assert second.supervisor.config["start_timeout_s"] == 900.0
        assert _flags_of(second.supervisor.config) == flags_before, (
            "re-merging the config must not move anything the child was started with"
        )

    def test_a_reload_with_changed_flags_does_not_adopt(self):
        first = _provider(n_ctx=131072)
        first.supervisor.props = {"total_slots": 1}
        second = _provider(n_ctx=262144)
        assert second.supervisor is not first.supervisor
        assert _ACTIVE_SUPERVISORS["local_qwen38"] is second.supervisor


class TestDrainingBeforeReplacement:
    """Mike's rule, 2026-08-22: a settings save applies at once, but the child
    that is generating an answer is replaced only after that answer is done."""

    @staticmethod
    def _bare_supervisor(**cfg):
        sup = LlamaServerSupervisor("local_qwen38", {"gguf_path": GGUF, **cfg})
        sup.props = {"total_slots": 1}          # "it is serving"
        return sup

    @pytest.mark.asyncio
    async def test_a_drain_does_not_stop_the_child_while_a_call_is_in_flight(self):
        sup = self._bare_supervisor()
        stopped = []

        async def _fake_stop():
            stopped.append(True)

        sup.stop = _fake_stop

        async with sup.call_slot():
            task = asyncio.create_task(sup.drain(timeout=None))
            await asyncio.sleep(0.6)
            assert stopped == [], "the child was stopped under a running call"
        await asyncio.wait_for(task, timeout=5)
        assert stopped == [True], "the child must stop once the call has finished"

    @pytest.mark.asyncio
    async def test_a_config_reload_drains_the_live_child_instead_of_killing_it(self):
        # The end-to-end shape of the rule, over the wiring rather than the
        # mechanism: a save that changes a command-line flag must leave the
        # answer in progress alone. Testing drain() alone would have passed
        # while `_retire` still called stop().
        first = _provider(n_ctx=131072)
        first.supervisor.props = {"total_slots": 1}
        stopped = []

        async def _fake_stop():
            stopped.append(True)

        first.supervisor.stop = _fake_stop

        async with first.supervisor.call_slot():
            second = _provider(n_ctx=262144)          # a flag moved -> retire
            assert second.supervisor is not first.supervisor
            await asyncio.sleep(0.5)
            assert stopped == [], "a settings save stopped the child mid-generation"
            assert second.supervisor._predecessor is not None, (
                "the successor was not told to wait for the card"
            )

        await asyncio.sleep(0.6)
        assert stopped == [True], "the child must be replaced once the call has finished"

    @pytest.mark.asyncio
    async def test_a_drain_refuses_new_calls_so_the_wait_can_end(self):
        # Without the refusal the wait could be starved forever by fresh work;
        # new calls belong to the successor, which is already in the registry.
        sup = self._bare_supervisor()
        sup.stop = _noop_stop
        await sup.drain(timeout=None)
        with pytest.raises(LlamaServerError, match="draining"):
            sup.call_slot()

    @pytest.mark.asyncio
    async def test_the_successor_does_not_start_until_the_predecessor_let_go(self):
        # The card cannot hold two copies of the model, so "drain the old one"
        # is only safe if the new one waits for the card.
        released = asyncio.Event()

        async def _slow_drain():
            await released.wait()

        successor = self._bare_supervisor()
        successor.props = None                  # nothing running yet
        successor.supersedes(asyncio.create_task(_slow_drain()))

        started = []

        async def _fake_launch(binary):
            started.append(True)
            return {"total_slots": 1}

        successor._launch_auto_kv = _fake_launch
        successor._launch = _fake_launch

        task = asyncio.create_task(successor.ensure_running())
        await asyncio.sleep(0.4)
        assert started == [], "the successor started while the old child still held the card"

        released.set()
        await asyncio.wait_for(task, timeout=5)
        assert started == [True]


class TestTheTemplateDictionary:
    """The words travel as the template spelled them, or not at all."""

    def test_the_templates_own_words_go_verbatim(self):
        for word in ("low", "medium", "xhigh"):
            body = _provider()._build_extra_body(word)
            assert body == {"chat_template_kwargs": {"reasoning_effort": word}}

    def test_xhigh_survives_the_alias_config_untouched(self):
        # The shared normalizer folds xhigh into high for the other providers;
        # here that fold would delete the top of the model's ladder.
        p = _provider(reasoning_effort="xhigh")
        assert p._build_extra_body() == {"chat_template_kwargs": {"reasoning_effort": "xhigh"}}

    def test_the_fleet_words_land_on_xhigh(self):
        # Three levels on the model, five in our header: there is no
        # second-from-top to preserve, so both upper fleet words map up.
        p = _provider()
        for word in ("high", "max"):
            assert p._build_extra_body(word) == {"chat_template_kwargs": {"reasoning_effort": "xhigh"}}

    def test_off_becomes_the_enable_thinking_switch(self):
        # Design-pinned until the cheap ADR-040 probe runs: the template's off
        # path is enable_thinking, not an effort word.
        p = _provider()
        assert p._build_extra_body("off") == {"chat_template_kwargs": {"enable_thinking": False}}

    def test_an_unknown_word_sends_nothing_and_the_template_default_applies(self):
        p = _provider()
        assert p._build_extra_body("ultra") == {}

    def test_nobody_saying_anything_sends_nothing(self):
        assert _provider()._build_extra_body() == {}

    def test_a_per_call_word_beats_the_alias_config(self):
        p = _provider(reasoning_effort="xhigh")
        assert p._build_extra_body("low") == {"chat_template_kwargs": {"reasoning_effort": "low"}}


class TestTheBudget:
    """reasoning_budget_tokens: per-request, over the alias config, capping
    the default thinking too — the 233K empty-answer failure mode."""

    def test_an_alias_budget_rides_top_level_alongside_the_effort(self):
        p = _provider(reasoning_budget_tokens=8000)
        assert p._build_extra_body("medium") == {
            "chat_template_kwargs": {"reasoning_effort": "medium"},
            "reasoning_budget_tokens": 8000,
        }

    def test_a_per_request_budget_beats_the_alias_config(self):
        p = _provider(reasoning_budget_tokens=8000)
        assert p._build_extra_body("medium", reasoning_budget_tokens=4000)["reasoning_budget_tokens"] == 4000

    def test_the_budget_caps_the_template_default_too(self):
        # No effort word anywhere: the template still thinks at its own
        # default (xhigh), and that is the strongest path — it must be the
        # capped one, not the exempt one.
        p = _provider(reasoning_budget_tokens=8000)
        assert p._build_extra_body() == {"reasoning_budget_tokens": 8000}

    def test_off_carries_no_budget(self):
        p = _provider(reasoning_budget_tokens=8000)
        assert p._build_extra_body("off") == {"chat_template_kwargs": {"enable_thinking": False}}


class TestSamplingAndLabels:
    def test_temperature_is_sent_even_while_thinking(self):
        # Unlike DeepSeek's inert-while-thinking dial, this server honours it
        # (every 2026-08-19 probe ran temp 0 with thinking on).
        body = _provider(temperature=0.0)._build_extra_body("xhigh")
        params = _provider(temperature=0.0)._sampling_params()
        assert "temperature" in params
        assert body["chat_template_kwargs"]["reasoning_effort"] == "xhigh"

    def test_top_p_only_when_configured(self):
        assert "top_p" not in _provider()._sampling_params()
        assert _provider(top_p=0.95)._sampling_params()["top_p"] == 0.95

    def test_top_k_rides_along_because_the_card_prescribes_it(self):
        assert "top_k" not in _provider()._sampling_params()
        assert _provider(top_k=20)._sampling_params()["top_k"] == 20

    def test_an_alias_with_no_sampling_at_all_hears_about_it_once(self, caplog):
        # The 2026-08-19 lesson: a probe ran greedy (temp 0, nothing else set)
        # under a prescription that names four sampling values — and nothing
        # in the log said a default was being used. The line fires once per
        # alias, only while thinking is on, and not once the alias is
        # configured.
        import logging

        p = _provider()
        with caplog.at_level(logging.INFO, logger="dpc_client_core.providers.llamacpp_server_provider"):
            p._sampling_params()
            p._sampling_params()
        advisories = [r for r in caplog.records if "no sampling configured" in r.message]
        assert len(advisories) == 1
        assert "top_k 20" in advisories[0].getMessage()

        configured = _provider(temperature=1.0, top_p=0.95, top_k=20)
        assert configured._sampling_params() == {
            "temperature": 1.0, "top_p": 0.95, "top_k": 20,
        }
        assert configured._sampling_default_logged is False

    def test_the_off_path_never_triggers_the_sampling_advisory(self):
        p = _provider()
        p._sampling_params(reasoning_effort="off")
        assert p._sampling_default_logged is False

    def test_the_effort_label_reads_the_built_body(self):
        p = _provider()
        assert p._effort_label("off", p._build_extra_body("off")) == "off"
        assert p._effort_label("high", p._build_extra_body("high")) == "xhigh"
        assert p._effort_label(None, p._build_extra_body()) == "server-default"


class TestRetryClassification:
    def test_a_template_refusal_is_not_retryable(self):
        # HTTP 500 with a deterministic jinja raise: retrying burns the whole
        # backoff budget to hear the same refusal.
        err = Exception("500 Internal Server Error: raise_exception Invalid reasoning effort")
        assert not LlamaServerProvider._is_retryable(err)

    def test_connection_class_failures_stay_retryable(self):
        assert LlamaServerProvider._is_retryable(Exception("connection error while calling localhost"))
        assert LlamaServerProvider._is_retryable(Exception("request timed out"))


class TestTheCallPath:
    @pytest.mark.asyncio
    async def test_generate_response_ensures_the_server_and_records_usage(self):
        p = _provider(reasoning_effort="high")
        sup = _FakeSupervisor()
        p.supervisor = sup
        client, completions = _fake_client(_chat_resp(content="hi", reasoning="thought"))
        async def _ensure():
            return client
        p._ensure = _ensure

        out = await p.generate_response("hello", conversation_id="c1")

        assert out == "hi"
        assert sup.ensure_calls == 1 or sup.slot_enters == 1
        body = completions.bodies[0]
        assert body["extra_body"] == {"chat_template_kwargs": {"reasoning_effort": "xhigh"}}
        assert body["model"].endswith("Q4_K_M")
        assert p.get_last_thinking() == "thought"
        usage = p.get_last_usage()
        assert usage["prompt_tokens"] == 11 and usage["completion_tokens"] == 7

    @pytest.mark.asyncio
    async def test_generate_response_carries_a_per_request_budget(self):
        p = _provider()
        p.supervisor = _FakeSupervisor()
        client, completions = _fake_client(_chat_resp())
        async def _ensure():
            return client
        p._ensure = _ensure

        await p.generate_response("hello", reasoning_budget_tokens=2048)
        assert completions.bodies[0]["extra_body"]["reasoning_budget_tokens"] == 2048

    @pytest.mark.asyncio
    async def test_generate_with_tools_parses_tool_calls_and_skips_cot_padding(self):
        p = _provider()
        p.supervisor = _FakeSupervisor()
        tc = SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="read_file", arguments=json.dumps({"path": "a.md"})),
        )
        client, completions = _fake_client(_chat_resp(content="", tool_calls=[tc]))
        async def _ensure():
            return client
        p._ensure = _ensure

        result = await p.generate_with_tools(
            [{"role": "user", "content": [{"type": "text", "text": "read it"}]}],
            [{"name": "read_file", "description": "", "input_schema": {"type": "object"}}],
            system="be brief",
        )

        assert result["tool_calls_raw"][0].name == "read_file"
        assert result["tool_calls_raw"][0].input == {"path": "a.md"}
        sent = completions.bodies[0]["messages"]
        # The qwen template needs no reasoning_content replay — unlike
        # DeepSeek's HTTP-400 rule, an assistant tool-call message goes out
        # bare.
        assert all("reasoning_content" not in m for m in sent)
        assert sent[0]["role"] == "system" and sent[0]["content"] == "be brief"

    @pytest.mark.asyncio
    async def test_the_streaming_path_enters_the_real_supervisors_slot(self):
        # Johnny's live TypeError (2026-08-19 21:26): `async with call_slot()`
        # exploded because the real supervisor's method was `async def`, so it
        # returned a coroutine instead of the slot. The fakes in these tests
        # had the right shape and never caught it — this one uses the REAL
        # supervisor object, only the OpenAI client stays fake.
        from dpc_client_core.managers.llama_server_supervisor import LlamaServerSupervisor

        p = _provider()
        p.supervisor = LlamaServerSupervisor("local_qwen38", {"gguf_path": GGUF})
        chunks = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(reasoning_content="th", content=None))], usage=None),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(reasoning_content=None, content="hello "))], usage=None),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(reasoning_content=None, content="world"))], usage=None),
            SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=9, completion_tokens=2, total_tokens=11)),
        ]

        class _Stream:
            def __aiter__(self):
                self._iter = iter(chunks)
                return self

            async def __anext__(self):
                try:
                    return next(self._iter)
                except StopIteration:
                    raise StopAsyncIteration

        client, completions = _fake_client(SimpleNamespace(choices=None, usage=None))

        async def fake_create(**params):
            assert params["stream"] is True
            assert params["stream_options"] == {"include_usage": True}
            return _Stream()

        completions.create = fake_create

        async def _ensure():
            return client

        p._ensure = _ensure
        seen = []

        async def on_chunk(text, cid):
            seen.append(text)

        out = await p.generate_response_stream("hi", on_chunk=on_chunk, conversation_id="c1")
        assert out == "hello world"
        assert seen == ["hello ", "world"]
        assert p.get_last_thinking() == "th"
        assert p.get_last_usage()["completion_tokens"] == 2
        assert p.supervisor._in_flight == 0

    @pytest.mark.asyncio
    async def test_every_entry_point_builds_params_the_real_sdk_accepts(self):
        """The contract tests the fakes cannot replace.

        Three live fires in one day (f16 default, the call_slot coroutine, and
        `top_k` as a kwarg at 18:43) were all shape errors against things the
        provider CALLS — and every double in this suite happily accepted the
        wrong shape, because a fake inherits the implementation's assumptions.
        The real AsyncOpenAI client validates kwargs when the call is made,
        before any network: pointed at a dead port, a bad kwarg raises
        TypeError and a good shape raises a connection error. So one dead port
        pins all three entry points against the actual SDK signature."""
        from openai import AsyncOpenAI

        from dpc_client_core.managers.llama_server_supervisor import LlamaServerSupervisor

        # max_retry_seconds=0: a connection error is retryable to the
        # provider, and the contract test must not drive its backoff budget.
        p = _provider(top_k=20, top_p=0.95, temperature=1.0, max_retry_seconds=0)
        p.supervisor = LlamaServerSupervisor("local_qwen38", {"gguf_path": GGUF})
        dead = AsyncOpenAI(api_key="local", base_url="http://127.0.0.1:1/v1", max_retries=0)

        async def _ensure():
            return dead

        p._ensure = _ensure

        import openai

        for attempt in (
            lambda: p.generate_response("hi"),
            lambda: p.generate_response_stream("hi"),
            lambda: p.generate_with_tools(
                [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                [{"name": "t", "description": "", "input_schema": {"type": "object"}}],
            ),
        ):
            # The provider wraps transport failures in its own RuntimeError —
            # the contract under test is that the failure is CONNECTION-class,
            # never a TypeError the SDK raised about our kwargs.
            with pytest.raises((RuntimeError, openai.APIConnectionError)) as ei:
                await attempt()
            assert not isinstance(ei.value.__cause__, TypeError), (
                f"an entry point sends a kwarg the real SDK refuses: {ei.value.__cause__}"
            )
        # The slot was entered and released cleanly on each failed call, and
        # no kwarg survived at top level that the SDK would refuse.
        assert p.supervisor._in_flight == 0

    @pytest.mark.asyncio
    async def test_a_bad_kwarg_fails_the_signature_not_the_connection(self):
        """The guard behind the test above: this is what 18:43 looked like.
        The SDK refuses `top_k` as a kwarg at call time, before any network —
        which is exactly why it must ride in extra_body, and why the contract
        test above can run against a dead port."""
        from openai import AsyncOpenAI

        dead = AsyncOpenAI(api_key="local", base_url="http://127.0.0.1:1/v1", max_retries=0)
        with pytest.raises(TypeError, match="top_k"):
            dead.chat.completions.create(
                model="m", messages=[{"role": "user", "content": "x"}], top_k=20
            )

    @pytest.mark.asyncio
    async def test_top_k_travels_in_extra_body_not_as_an_sdk_kwarg(self):
        p = _provider(top_k=20)
        p.supervisor = _FakeSupervisor()
        client, completions = _fake_client(_chat_resp())

        async def _ensure():
            return client

        p._ensure = _ensure

        await p.generate_response("hello")
        body = completions.bodies[0]
        assert "top_k" not in body
        assert body["extra_body"]["top_k"] == 20

    @pytest.mark.asyncio
    async def test_close_drains_the_child(self):
        p = _provider()
        sup = _FakeSupervisor()
        p.supervisor = sup
        # The registry must point at the supervisor being closed for the
        # deregistration to fire — as it does when nothing swapped it since
        # construction.
        _ACTIVE_SUPERVISORS["local_qwen38"] = sup
        await p.close()
        assert sup.drained_with is not None
        assert "local_qwen38" not in _ACTIVE_SUPERVISORS


class TestVision:
    """Images ride as image_url content blocks on the same OpenAI-compatible
    call; the projector is the alias's mmproj. The wiring follows a live
    probe (2026-08-20): the Ollama projector blob is accepted by the pinned
    server, /props advertises vision and video, and a screenshot was read
    accurately at full 262 144 with q4_0 KV — where the first run also showed
    the template's xhigh default spending a whole small window thinking and
    answering nothing, which is why the vision path defaults thinking off."""

    def test_supports_vision_follows_the_alias_mmproj(self):
        assert not _provider().supports_vision()
        assert _provider(mmproj="mm.gguf").supports_vision()

    def test_supports_vision_follows_a_live_childs_props(self):
        # Byte-faithful to the two recorded /props bodies. The probe child
        # (with --mmproj, 2026-08-20) answered `"modalities": {"vision":
        # true, "video": true, "audio": false}`; the production child without
        # a projector answered the same key with all-false the same night.
        # `modalities` is a TOP-LEVEL dict — the review once read the probe
        # report's extracted printout as a flat body and demanded a reconcile;
        # these fixtures are that reconcile, from the artifacts.
        p = _provider()
        p.supervisor = _FakeSupervisor()
        p.supervisor.props = {
            "total_slots": 4,
            "modalities": {"vision": True, "video": True, "audio": False},
        }
        assert p.supports_vision()

        bare = _provider()
        bare.supervisor = _FakeSupervisor()
        bare.supervisor.props = {
            "total_slots": 4,
            "modalities": {"vision": False, "video": False, "audio": False},
        }
        assert not bare.supports_vision()

    @pytest.mark.asyncio
    async def test_images_ride_as_image_url_blocks(self):
        p = _provider(mmproj="mm.gguf")
        sup = _FakeSupervisor()
        p.supervisor = sup
        client, completions = _fake_client(_chat_resp(content="a providers panel"))

        async def _ensure():
            return client

        p._ensure = _ensure

        out = await p.generate_with_vision(
            "what is this?",
            [
                {"base64": "AAAA"},
                {"base64": "data:image/jpeg;base64,BBBB", "mime_type": "image/jpeg"},
            ],
        )

        assert out == "a providers panel"
        body = completions.bodies[0]
        content = body["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "what is this?"}
        assert content[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        assert content[2] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BBBB"}}
        # A background read, not a reasoning task: thinking off unless asked.
        assert body["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert sup.slot_enters == 1
        assert p._last_usage is not None

    @pytest.mark.asyncio
    async def test_an_explicit_effort_thinks_on_the_vision_path(self):
        p = _provider(mmproj="mm.gguf", reasoning_effort="low")
        p.supervisor = _FakeSupervisor()
        client, completions = _fake_client(_chat_resp(content="ok"))

        async def _ensure():
            return client

        p._ensure = _ensure

        await p.generate_with_vision("what?", [{"base64": "AAAA"}], reasoning_effort="low")

        body = completions.bodies[0]
        assert body["extra_body"]["chat_template_kwargs"] == {"reasoning_effort": "low"}
        assert "enable_thinking" not in body["extra_body"].get("chat_template_kwargs", {})

    @pytest.mark.asyncio
    async def test_an_alias_budget_does_not_re_enable_thinking_on_vision(self):
        # The production alias carries reasoning_budget_tokens=10000, and the
        # first version of the thinking-off default only fired on an EMPTY
        # extra_body — which the alias budget kept non-empty, so every group
        # image would have thought xhigh inside an 8192 window. Caught at
        # review; this test pins the production config exactly.
        p = _provider(mmproj="mm.gguf", reasoning_budget_tokens=10000)
        p.supervisor = _FakeSupervisor()
        client, completions = _fake_client(_chat_resp(content="desc"))

        async def _ensure():
            return client

        p._ensure = _ensure

        await p.generate_with_vision("what?", [{"base64": "AAAA"}])

        body = completions.bodies[0]
        assert body["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
        assert "reasoning_budget_tokens" not in body["extra_body"]

    @pytest.mark.asyncio
    async def test_a_per_call_budget_on_vision_caps_thinking_instead_of_off(self):
        # A caller passing an explicit budget asked for thinking; the cap
        # rides, thinking stays on.
        p = _provider(mmproj="mm.gguf")
        p.supervisor = _FakeSupervisor()
        client, completions = _fake_client(_chat_resp(content="desc"))

        async def _ensure():
            return client

        p._ensure = _ensure

        await p.generate_with_vision(
            "what?", [{"base64": "AAAA"}], reasoning_budget_tokens=2000
        )

        body = completions.bodies[0]
        assert body["extra_body"].get("reasoning_budget_tokens") == 2000
        assert "enable_thinking" not in body["extra_body"].get("chat_template_kwargs", {})


class TestReasoningAccounting:
    """The pinned server leaves completion_tokens_details empty (measured
    live, 2026-08-20: 11.8K chars of thinking, reasoning=0 in usage), so the
    split is estimated from the message body and marked, rather than letting
    the burn history stay blind to the thinking lever it pays for."""

    @pytest.mark.asyncio
    async def test_usage_without_split_estimates_reasoning_from_the_body(self, caplog):
        import logging

        p = _provider()
        p.supervisor = _FakeSupervisor()
        client, completions = _fake_client(_chat_resp(content="answer", reasoning="x" * 400))

        async def _ensure():
            return client

        p._ensure = _ensure

        with caplog.at_level(logging.INFO, logger="dpc_client_core.providers.llamacpp_server_provider"):
            await p.generate_response("q")

        assert p._last_usage["reasoning_tokens"] == 100  # 400 chars / 4
        assert p._last_usage["content_tokens"] == 0  # completion=7, estimate clamps at 0
        assert any("split=estimated" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_native_split_is_never_overwritten(self):
        p = _provider()
        p.supervisor = _FakeSupervisor()
        usage = SimpleNamespace(
            prompt_tokens=10, completion_tokens=100, total_tokens=110,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=50),
        )
        out = p._record_usage(
            usage, path="plain", reasoning_text="y" * 4000,
        )
        assert out["reasoning_tokens"] == 50
        assert out["content_tokens"] == 50

    @pytest.mark.asyncio
    async def test_the_streaming_entry_estimates_too_four_of_four(self, caplog):
        # Johnny's review find: the streaming entry was the one of four that
        # did not pass reasoning_text, and the correct argument is the LOCAL
        # accumulator — self._last_thinking is still None at the usage chunk,
        # which arrives before the post-loop assignment. Without the fold the
        # stream's usage line reads a honest-looking reasoning=0.
        import logging

        p = _provider()
        p.supervisor = _FakeSupervisor()
        chunks = [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(reasoning_content="t" * 400, content=None))], usage=None),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(reasoning_content=None, content="ok"))], usage=None),
            SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=9, completion_tokens=120, total_tokens=129)),
        ]

        class _Stream:
            def __aiter__(self):
                self._iter = iter(chunks)
                return self

            async def __anext__(self):
                try:
                    return next(self._iter)
                except StopIteration:
                    raise StopAsyncIteration

        client, completions = _fake_client(SimpleNamespace(choices=None, usage=None))

        async def fake_create(**params):
            return _Stream()

        completions.create = fake_create

        async def _ensure():
            return client

        p._ensure = _ensure

        with caplog.at_level(logging.INFO, logger="dpc_client_core.providers.llamacpp_server_provider"):
            out = await p.generate_response_stream("hi")

        assert out == "ok"
        assert p._last_usage["reasoning_tokens"] == 100  # 400 chars / 4
        assert p._last_usage["content_tokens"] == 20
        assert any("split=estimated" in r.getMessage() for r in caplog.records)


class TestTheBudgetClamp:
    """A budget above the output window never binds - the window ends first
    (measured live: budget 10000 at max_tokens 8192 thought 19 019 chars and
    answered nothing). The clamp keeps thinking inside the window it shares
    with the answer: effective = min(budget, max_tokens - 2048). And the
    clamp reads the SAME max_tokens the request carries - per-call in plain
    and vision, the alias field in tools and stream - because a clamp
    guarding a different window than the wire diverges from the request."""

    def test_the_clamp_cuts_an_alias_budget_to_the_window(self):
        p = _provider(reasoning_budget_tokens=10000)
        body = p._build_extra_body(None, None, effective_max_tokens=8192)
        assert body["reasoning_budget_tokens"] == 6144  # 8192 - 2048

    def test_a_budget_below_the_clamp_passes_untouched(self):
        p = _provider(reasoning_budget_tokens=3000)
        body = p._build_extra_body(None, None, effective_max_tokens=8192)
        assert body["reasoning_budget_tokens"] == 3000

    def test_the_clamp_follows_a_per_call_max_tokens_not_the_field(self):
        # The sharp case from review: the caller narrows the window to 4000
        # per call; a clamp reading the alias field (8192) would send 6144
        # into a 4000-token window - truncation by our own hand.
        p = _provider(reasoning_budget_tokens=10000)
        body = p._build_extra_body(
            "medium", 50000, effective_max_tokens=4000
        )
        assert body["reasoning_budget_tokens"] == 1952  # 4000 - 2048

    @pytest.mark.asyncio
    async def test_the_plain_path_passes_its_wire_max_tokens_to_the_clamp(self):
        p = _provider(reasoning_budget_tokens=10000)
        p.supervisor = _FakeSupervisor()
        client, completions = _fake_client(_chat_resp(content="ok"))

        async def _ensure():
            return client

        p._ensure = _ensure

        await p.generate_response("q", max_tokens=4000)

        body = completions.bodies[0]
        assert body["max_tokens"] == 4000
        assert body["extra_body"]["reasoning_budget_tokens"] == 1952

    @pytest.mark.asyncio
    async def test_the_shape_the_sleep_synthesis_sends_lands_whole(self):
        """The nightly synthesis asks for both at once: the room it reserved
        for the brief, and a cap so the think block cannot spend that room
        first. The run that produced no brief on 2026-08-20 sent neither, took
        the provider default of 8192 and spent 4 921 of it thinking."""
        p = _provider(reasoning_budget_tokens=10000)
        p.supervisor = _FakeSupervisor()
        client, completions = _fake_client(_chat_resp(content="{}"))

        async def _ensure():
            return client

        p._ensure = _ensure

        await p.generate_response("q", max_tokens=16384, reasoning_budget_tokens=4000)

        body = completions.bodies[0]
        assert body["max_tokens"] == 16384
        # Under the clamp (16384 - 2048), so the caller's cap survives intact.
        assert body["extra_body"]["reasoning_budget_tokens"] == 4000

    def test_thinking_off_sends_no_budget_at_all(self):
        p = _provider(reasoning_budget_tokens=10000)
        body = p._build_extra_body("off", None, effective_max_tokens=8192)
        assert "reasoning_budget_tokens" not in body
        assert body["chat_template_kwargs"] == {"enable_thinking": False}

    def test_the_none_and_zero_window_contract(self):
        # None: the caller told us nothing about the window - the clamp stays
        # silent (the request itself then rides the server default). 0: the
        # window is known and degenerate - the clamp fires and the existing
        # budget<=0 fallback lands it at 1, not uncapped (review, Johnny's
        # find sharpened by Ark: truthiness made 0 a silent bypass).
        p = _provider(reasoning_budget_tokens=10000)
        silent = p._build_extra_body(None, None, effective_max_tokens=None)
        assert silent["reasoning_budget_tokens"] == 10000
        degenerate = p._build_extra_body(None, None, effective_max_tokens=0)
        assert degenerate["reasoning_budget_tokens"] == 1


class TestTheSpeedPayload:
    """The live counter beside Stop: exact prefill/decode split only where a
    phase boundary exists (streaming — first chunk arrival); non-streaming
    calls carry total throughput rather than a fabricated split."""

    def test_streaming_gets_the_exact_split_from_first_chunk(self):
        from dpc_client_core.providers.llamacpp_server_provider import LlamaServerProvider
        s = LlamaServerProvider._speed_payload(100000, 2000, 150.0, 125.0, "llama.cpp", "qwen")
        assert s["prefill_tok_s"] == 800      # 100000 / 125
        assert s["decode_tok_s"] == 80        # 2000 / 25
        assert s["total_tok_s"] == 680        # 102000 / 150

    def test_non_streaming_carries_total_only(self):
        from dpc_client_core.providers.llamacpp_server_provider import LlamaServerProvider
        s = LlamaServerProvider._speed_payload(90000, 2000, 160.0, None, "llama.cpp", "qwen")
        assert "prefill_tok_s" not in s and "decode_tok_s" not in s
        assert s["total_tok_s"] == 575

    def test_zero_elapsed_is_no_counter_at_all(self):
        from dpc_client_core.providers.llamacpp_server_provider import LlamaServerProvider
        assert LlamaServerProvider._speed_payload(1, 1, 0, None, "a", "m") == {}


class TestTheReasonTheModelStoppedReachesTheCaller:
    """`finish_reason` decides between two repairs and was read by nobody.

    A synthesis that ends at the ceiling and one that ends because the model
    was done look identical in the usage line, and they need opposite fixes.
    The value sits on `choices[0]` of every response; these pin that it
    reaches `get_last_usage()` on all four entry points, because a signal
    wired on three of four is a signal nobody can trust.
    """

    @staticmethod
    def _wire(p, client):
        async def _ensure():
            return client
        p._ensure = _ensure

    @pytest.mark.asyncio
    async def test_plain_carries_it(self):
        p = _provider()
        p.supervisor = _FakeSupervisor()
        client, _ = _fake_client(_chat_resp(finish_reason="length"))
        self._wire(p, client)
        await p.generate_response("hi")
        usage = p.get_last_usage()
        assert usage["finish_reason"] == "length"
        assert usage["max_tokens"] == p.max_tokens

    @pytest.mark.asyncio
    async def test_tools_carries_it(self):
        p = _provider()
        p.supervisor = _FakeSupervisor()
        client, _ = _fake_client(_chat_resp(content="", tool_calls=[], finish_reason="length"))
        self._wire(p, client)
        await p.generate_with_tools(
            [{"role": "user", "content": [{"type": "text", "text": "read it"}]}],
            [{"name": "read_file", "description": "", "input_schema": {"type": "object"}}],
        )
        usage = p.get_last_usage()
        assert usage["finish_reason"] == "length"
        assert usage["max_tokens"] == p.max_tokens

    @pytest.mark.asyncio
    async def test_vision_carries_it(self):
        p = _provider(mmproj="D:/models/mmproj.gguf")
        p.supervisor = _FakeSupervisor()
        client, _ = _fake_client(_chat_resp(finish_reason="length"))
        self._wire(p, client)
        await p.generate_with_vision(
            "what is this", [{"base64": "AAA", "mime_type": "image/png"}]
        )
        usage = p.get_last_usage()
        assert usage["finish_reason"] == "length"
        assert usage["max_tokens"] == p.max_tokens

    @pytest.mark.asyncio
    async def test_streaming_carries_it_from_the_chunk_before_the_usage_chunk(self):
        """The terminal usage chunk has no choices, so the reason arrives one
        chunk earlier — reading it off the usage chunk would always be None."""
        p = _provider()
        p.supervisor = _FakeSupervisor()
        chunks = [
            SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(reasoning_content=None, content="hi"),
                    finish_reason=None)],
                usage=None),
            SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(reasoning_content=None, content=""),
                    finish_reason="length")],
                usage=None),
            SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(prompt_tokens=9, completion_tokens=2, total_tokens=11)),
        ]

        class _Stream:
            def __aiter__(self):
                async def gen():
                    for c in chunks:
                        yield c
                return gen()

        class _Completions:
            async def create(self, **params):
                return _Stream()

        self._wire(p, SimpleNamespace(chat=SimpleNamespace(completions=_Completions())))
        await p.generate_response_stream("hi")
        usage = p.get_last_usage()
        assert usage["finish_reason"] == "length"
        assert usage["max_tokens"] == p.max_tokens

    @pytest.mark.asyncio
    async def test_the_log_line_says_it(self, caplog):
        import logging
        p = _provider()
        p.supervisor = _FakeSupervisor()
        client, _ = _fake_client(_chat_resp(finish_reason="length"))
        self._wire(p, client)
        with caplog.at_level(
            logging.INFO, logger="dpc_client_core.providers.llamacpp_server_provider"
        ):
            await p.generate_response("hi")
        assert any("finish=length" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_response_without_the_field_records_no_key_rather_than_a_none(self):
        """Absent is not a value: a key carrying None would read as a measured
        'no reason' to anything that checks the trigger."""
        p = _provider()
        p.supervisor = _FakeSupervisor()
        bare = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="ok", reasoning_content=None, tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )
        client, _ = _fake_client(bare)
        self._wire(p, client)
        await p.generate_response("hi")
        assert "finish_reason" not in p.get_last_usage()


# --- the retried stream used to deliver the answer twice ----------------------

class TestTheRetriedStream:
    """The retry branch of `generate_response_stream`, which no test entered.

    Inherited from DeepSeekProvider's shape and carrying the same defect: `_call`
    streams every token through `on_chunk` and returns the accumulated text, and
    the branch used to send that return value through `on_chunk` again. The
    consumer saw the answer twice; `agent_manager` then wrote the doubled text
    into conversation history, because `_raw` no longer matched `response`.
    """

    @staticmethod
    def _chunks():
        def token(text):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(reasoning_content=None, content=text),
                    finish_reason=None)],
                usage=None)
        return [
            token("he"),
            token("llo"),
            SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(prompt_tokens=9, completion_tokens=2, total_tokens=11)),
        ]

    @pytest.mark.asyncio
    async def test_each_token_arrives_exactly_once_after_a_retry(self, monkeypatch):
        from dpc_client_core.providers import deepseek_provider as retry_home

        p = _provider()
        p.supervisor = _FakeSupervisor()
        # `_retry_with_backoff` is inherited from DeepSeekProvider and sleeps 3s
        # before the first retry. The wait is not the behaviour under test.
        async def _no_sleep(_seconds):
            return None
        monkeypatch.setattr(retry_home.asyncio, "sleep", _no_sleep)

        chunks = self._chunks()

        class _Stream:
            def __aiter__(self):
                async def gen():
                    for c in chunks:
                        yield c
                return gen()

        class _FlakyCompletions:
            def __init__(self):
                self.calls = 0

            async def create(self, **params):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("HTTP 503 service unavailable")
                return _Stream()

        completions = _FlakyCompletions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        async def _ensure():
            return client
        p._ensure = _ensure

        seen = []

        async def on_chunk(text, conv_id):
            seen.append(text)

        out = await p.generate_response_stream("hi", on_chunk, "conv-1")

        assert completions.calls == 2, "the retry has to have happened"
        assert out == "hello"
        assert seen == ["he", "llo"], "the full text must not follow the tokens"


class TestTheStreamCarriesAnEffort:
    """The plain path two methods up already read the caller's effort while the
    stream passed a literal `None` — the same gap DeepSeek had, inherited with
    the shape it was built from."""

    @pytest.mark.asyncio
    async def test_the_stream_sends_the_callers_effort(self):
        p = _provider()
        p.supervisor = _FakeSupervisor()

        chunks = [
            SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(reasoning_content=None, content="hi"),
                    finish_reason=None)],
                usage=None),
            SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1, total_tokens=4)),
        ]

        class _Stream:
            def __aiter__(self):
                async def gen():
                    for c in chunks:
                        yield c
                return gen()

        seen_params = {}

        class _Completions:
            async def create(self, **params):
                seen_params.update(params)
                return _Stream()

        client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))

        async def _ensure():
            return client
        p._ensure = _ensure

        await p.generate_response_stream("hi", None, "conv-1", reasoning_effort="low")

        # llama-server takes the level through the chat template, not at the top
        # level of extra_body — the assertion is written against the shape the
        # provider actually builds rather than the one DeepSeek uses.
        assert seen_params["extra_body"]["chat_template_kwargs"]["reasoning_effort"] == "low"



class TestTheUsageLineSaysHowMuchOfThePromptWasReused:
    """The prompt cache had no success signal at all until 2026-08-30.

    llama.cpp warns on a failed load (`server-context.cpp:284`, b10566) and
    saves at TRACE, so the production log read «two loads, ever, both
    refusals» over 74 starts — a meter that can only report bad news. The
    engine's own `prompt eval time = ... / N tokens` says what it actually
    re-evaluated; against what we sent, that is the reuse, and the supervisor
    already parsed it and threw it away.
    """

    def _usage(self, prompt_tokens, engine_prompt_tokens):
        p = _provider()
        p.supervisor = _FakeSupervisor()
        p.supervisor.last_task_timings = lambda: {
            "prefill_tok_s": 800, "decode_tok_s": 40,
            "engine_prompt_tokens": engine_prompt_tokens, "engine_gen_tokens": 20,
        }
        usage = SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=20, total_tokens=prompt_tokens + 20,
            completion_tokens_details=None,
        )
        return p._record_usage(usage, path="tools", elapsed_s=10.0)

    def test_a_warm_turn_is_reported_as_reuse(self):
        out = self._usage(150_000, 4_000)
        assert out["prefilled_tokens"] == 4_000
        assert out["cached_tokens"] == 146_000

    def test_a_cold_turn_reports_no_reuse(self):
        out = self._usage(150_000, 150_000)
        assert out["cached_tokens"] == 0

    def test_the_line_carries_the_percentage(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="dpc_client_core.providers.llamacpp_server_provider"):
            self._usage(150_000, 4_000)
        assert any("prefilled=4000 of 150000 (reuse=97.3%)" in r.getMessage()
                   for r in caplog.records)

    def test_a_tokeniser_disagreement_never_prints_a_negative_reuse(self):
        """The two counts come from two tokenisers — the API's and the
        engine's — so the engine can report more than we think we sent."""
        assert self._usage(1_000, 1_010)["cached_tokens"] == 0

    def test_no_timings_leaves_the_line_as_it_was(self, caplog):
        import logging

        p = _provider()
        p.supervisor = _FakeSupervisor()
        usage = SimpleNamespace(
            prompt_tokens=100, completion_tokens=20, total_tokens=120,
            completion_tokens_details=None,
        )
        with caplog.at_level(logging.INFO, logger="dpc_client_core.providers.llamacpp_server_provider"):
            out = p._record_usage(usage, path="plain", elapsed_s=1.0)
        assert "cached_tokens" not in out
        assert not any("prefilled=" in r.getMessage() for r in caplog.records)


class TestTheResponseIsTheSourceOfItsOwnTimings:
    """The child states its per-request numbers in the body it returns.

    Measured against the running server, through the same AsyncOpenAI client
    this provider uses: `resp.model_extra["timings"]` carries prompt_n,
    prompt_per_second, predicted_n, predicted_per_second, cache_n and the
    draft counters. The log scrape they were taken from cannot say which task
    a block belongs to, which is the whole of the attribution problem.
    """

    SERVER = {
        "cache_n": 50, "prompt_n": 4, "prompt_ms": 1.2, "prompt_per_second": 171.7,
        "predicted_n": 20, "predicted_ms": 620.0, "predicted_per_second": 32.6,
        "draft_n": 10, "draft_n_accepted": 7,
    }

    def _provider_with_log_timings(self):
        p = _provider()
        p.supervisor = _FakeSupervisor()
        p.supervisor.last_task_timings = lambda: {
            "prefill_tok_s": 111, "decode_tok_s": 11,
            "engine_prompt_tokens": 1, "engine_gen_tokens": 1,
        }
        return p

    def _usage(self, prompt_tokens=54, completion_tokens=20):
        return SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            completion_tokens_details=None,
        )

    def test_the_fields_map_onto_this_files_vocabulary(self):
        out = llamacpp_server_provider._timings_from_response(self.SERVER)

        assert out["prefill_tok_s"] == 171
        assert out["decode_tok_s"] == 32
        assert out["engine_prompt_tokens"] == 4
        assert out["engine_cached_tokens"] == 50
        assert out["draft_acceptance"] == 0.7

    def test_a_response_without_timings_is_not_a_source(self):
        assert llamacpp_server_provider._timings_from_response(None) is None
        assert llamacpp_server_provider._timings_from_response({}) is None
        assert llamacpp_server_provider._timings_from_response({"cache_n": 3}) is None

    def test_the_response_wins_over_the_shared_log(self):
        p = self._provider_with_log_timings()

        out = p._record_usage(
            self._usage(), path="tools", elapsed_s=10.0, engine_timings=self.SERVER
        )

        assert out["speed"]["prefill_tok_s"] == 171, "the log's 111 was preferred"
        assert out["speed"]["decode_tok_s"] == 32

    def test_the_reuse_is_stated_rather_than_subtracted(self):
        """54 sent, 4 evaluated, 50 reused — the server says all three."""
        p = self._provider_with_log_timings()

        out = p._record_usage(
            self._usage(prompt_tokens=54), path="tools", elapsed_s=10.0,
            engine_timings=self.SERVER,
        )

        assert out["prefilled_tokens"] == 4
        assert out["cached_tokens"] == 50

    def test_without_response_timings_the_log_is_still_read(self):
        """Non-regression: a build that does not send them keeps the old path."""
        p = self._provider_with_log_timings()

        out = p._record_usage(self._usage(), path="tools", elapsed_s=10.0)

        assert out["speed"]["prefill_tok_s"] == 111
