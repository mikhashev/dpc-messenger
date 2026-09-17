"""An analysis that crashed must not arrive wearing the analysis header.

`_pre_analyze_image_for_agent` caught everything and returned
`f"[Image analysis failed: {e}]"`. That string is non-empty, so the caller's
empty-value guard never fired and the traceback was announced to the model under
"here is the visual analysis" — the model had no way to tell a description from a
crash report. Sibling of `test_a_failed_look_reads_as_a_failure.py`, which covers
the model answering with nothing; this one covers the call never answering.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter


PIXEL = "iVBORw0KGgoAAAANSUhEUg=="


class _BlindProvider:
    alias = "local_text"
    model = "some-text-model"

    def __init__(self):
        self.prompts = []

    def supports_vision(self):
        return False

    async def generate_response(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return "I could not see the image, so I will not guess."

    def get_last_usage(self):
        return None


def _adapter(error: Exception):
    provider = _BlindProvider()

    async def _query(**kwargs):
        raise error

    adapter = DpcLlmAdapter.__new__(DpcLlmAdapter)
    adapter._llm_manager = SimpleNamespace(
        providers={provider.alias: provider},
        default_provider=provider.alias,
        agent_provider=None,
        query=_query,
    )
    adapter._provider_alias = provider.alias
    adapter._compute_host = ""
    adapter._token_counter = None
    adapter._default_model = None
    return adapter, provider


def _picture_turn():
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is in this picture?"},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{PIXEL}"}},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_a_crash_is_not_announced_as_the_visual_analysis():
    adapter, provider = _adapter(RuntimeError("vision provider is not configured"))

    await adapter.chat(_picture_turn())

    sent = provider.prompts[0].lower()
    assert "here is the visual analysis" not in sent
    assert "the visual analysis failed" in sent
    assert "you have not seen it" in sent


@pytest.mark.asyncio
async def test_the_reason_travels_with_the_notice():
    adapter, provider = _adapter(RuntimeError("vision provider is not configured"))

    await adapter.chat(_picture_turn())

    assert "vision provider is not configured" in provider.prompts[0]


@pytest.mark.asyncio
async def test_the_round_still_completes():
    """An honest notice keeps the round alive; killing it would help nobody."""
    adapter, provider = _adapter(RuntimeError("boom"))

    msg, usage = await adapter.chat(_picture_turn())

    assert msg["content"] == "I could not see the image, so I will not guess."
    assert usage["total_tokens"] >= 0


@pytest.mark.asyncio
async def test_the_users_own_question_survives_the_failure():
    adapter, provider = _adapter(RuntimeError("boom"))

    await adapter.chat(_picture_turn())

    assert "what is in this picture?" in provider.prompts[0]


@pytest.mark.asyncio
async def test_the_failure_reason_never_returns_as_a_description():
    adapter, _ = _adapter(RuntimeError("boom"))

    description, reason = await adapter._pre_analyze_image_for_agent(
        [{"base64": PIXEL, "mime_type": "image/png"}], "what is this?"
    )

    assert description is None
    assert "boom" in reason


def test_the_three_states_of_the_injected_header_stay_distinct():
    messages = [{"role": "user", "content": "what is in this picture?"}]
    inject = DpcLlmAdapter._inject_image_description_into_messages

    described = inject(None, messages, "a red bicycle")[0]["content"].lower()
    silent = inject(None, messages, "")[0]["content"].lower()
    crashed = inject(None, messages, None, "boom")[0]["content"].lower()

    assert "here is the visual analysis" in described
    assert "returned no description" in silent
    assert "the visual analysis failed" in crashed and "boom" in crashed
    assert "here is the visual analysis" not in crashed
