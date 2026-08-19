"""What a call cost is read from one provider and guessed for the rest.

`get_last_usage()` existed on `DeepSeekProvider` alone. Three other providers
built a `usage` dict inside the tools path, returned it inline and exposed no
accessor, and the single reader — `DpcLlmAdapter.chat` — reached for the method
through `hasattr`. So one provider was priced by what the vendor reported and
every other one by an estimate the loop computed for itself.

These tests fix the contract on the base class: every provider can be asked, and
the answer is `None` until a call has been made. ADR-040 names this as the
precondition for the `llamacpp_server` provider, on the grounds that a fourth
private copy is not the risk — a second unread one is.
"""
from typing import Any, Dict, Optional

import pytest

from dpc_client_core.providers.base import AIProvider


class _Bare(AIProvider):
    """A provider that implements nothing beyond the base."""


class TestTheBaseCarriesTheContract:
    def test_every_provider_can_be_asked(self):
        assert hasattr(AIProvider, "get_last_usage")

    def test_the_answer_is_none_before_any_call(self):
        p = _Bare("bare", {"type": "bare"})
        assert p.get_last_usage() is None

    def test_a_recorded_usage_comes_back(self):
        p = _Bare("bare", {"type": "bare"})
        p._record_last_usage({"prompt_tokens": 10, "completion_tokens": 2})
        assert p.get_last_usage() == {"prompt_tokens": 10, "completion_tokens": 2}

    def test_the_stored_usage_is_a_copy(self):
        """A caller that mutates what it was handed must not edit the provider."""
        p = _Bare("bare", {"type": "bare"})
        source = {"prompt_tokens": 10}
        p._record_last_usage(source)
        source["prompt_tokens"] = 999
        p.get_last_usage()["prompt_tokens"] = 111
        assert p.get_last_usage() == {"prompt_tokens": 10}

    def test_recording_nothing_clears_it(self):
        p = _Bare("bare", {"type": "bare"})
        p._record_last_usage({"prompt_tokens": 10})
        p._record_last_usage(None)
        assert p.get_last_usage() is None


class TestTheProvidersThatBuiltItPrivatelyNowReportIt:
    """One test per provider that had a usage dict and no way to ask for it."""

    def test_ollama_records_what_it_logged(self):
        from dpc_client_core.providers.ollama_provider import OllamaProvider

        p = OllamaProvider("local", {"type": "ollama", "model": "qwen3.8:latest"})
        assert p.get_last_usage() is None

        class _Response:
            prompt_eval_count = 111_598
            eval_count = 362
            done_reason = "stop"
            prompt_eval_duration = 138_000_000_000
            eval_duration = 9_100_000_000
            load_duration = 4_000_000_000

        p._log_usage(_Response(), "plain")

        usage = p.get_last_usage()
        assert usage is not None
        assert usage["prompt_tokens"] == 111_598
        assert usage["completion_tokens"] == 362
        assert usage["total_tokens"] == 111_598 + 362

    def test_both_zai_providers_record_where_they_built(self):
        """Read as source: both dicts are built deep inside an async tools call
        that needs a live client, so what is checked here is that the recording
        sits beside the construction — the same reason the adapter test reads
        text rather than running the branch."""
        import inspect

        from dpc_client_core.providers import zai_coding_provider, zai_provider

        for module in (zai_provider, zai_coding_provider):
            source = inspect.getsource(module)
            assert "_record_last_usage(usage)" in source, module.__name__

    def test_deepseek_still_answers_after_the_move(self):
        """DeepSeek had the accessor first; the base must not shadow it."""
        from dpc_client_core.providers.deepseek_provider import DeepSeekProvider

        p = DeepSeekProvider.__new__(DeepSeekProvider)
        p._last_usage = {"prompt_tokens": 5}
        assert p.get_last_usage() == {"prompt_tokens": 5}


class TestTheReaderStopsGuessing:
    def test_the_adapter_no_longer_gates_on_hasattr(self):
        """The guard existed because the method was on one class out of twelve.

        Read as source on purpose: the branch is inside an async path that needs
        a live provider and a live model to reach, so the cheap check that it was
        actually removed is the text.
        """
        import inspect

        from dpc_client_core.dpc_agent import llm_adapter

        source = inspect.getsource(llm_adapter)
        assert 'hasattr(provider, "get_last_usage")' not in source
