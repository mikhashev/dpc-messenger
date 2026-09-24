"""EmbeddingProvider._load_model must set DISABLE_SAFETENSORS_CONVERSION=true
before calling SentenceTransformer(...), otherwise transformers' auto-conversion
thread downloads a second, unused copy of the weights (bge-m3: +2.27 GB) for any
repo that ships only pytorch_model.bin. The env var is read at from_pretrained
call time — this test asserts that timing, not just the end state.
"""

import os

import pytest
import sentence_transformers

from dpc_client_core.dpc_agent import memory


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("DISABLE_SAFETENSORS_CONVERSION", raising=False)
    yield


def test_the_env_var_is_set_before_sentence_transformer_is_called(monkeypatch):
    seen = {}

    def fake_sentence_transformer(*args, **kwargs):
        seen["value"] = os.environ.get("DISABLE_SAFETENSORS_CONVERSION")

        class _FakeModel:
            max_seq_length = 8192

        return _FakeModel()

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", fake_sentence_transformer)

    provider = memory.EmbeddingProvider(model_name="test/safetensors-guard", device="cpu")
    provider._load_model()

    assert seen["value"] == "true"


def test_an_explicit_operator_value_is_respected_even_when_it_would_not_work(monkeypatch, caplog):
    # setdefault must not override an operator's existing choice, but a value
    # other than the exact literal "true" silently does nothing in transformers
    # — that mismatch is worth a warning, not a silent override.
    monkeypatch.setenv("DISABLE_SAFETENSORS_CONVERSION", "1")

    def fake_sentence_transformer(*args, **kwargs):
        class _FakeModel:
            max_seq_length = 8192

        return _FakeModel()

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", fake_sentence_transformer)

    provider = memory.EmbeddingProvider(model_name="test/safetensors-guard-2", device="cpu")
    with caplog.at_level("WARNING", logger=memory.log.name):
        provider._load_model()

    assert os.environ.get("DISABLE_SAFETENSORS_CONVERSION") == "1"
    assert any("DISABLE_SAFETENSORS_CONVERSION" in r.getMessage() for r in caplog.records)
