"""A batch that does not fit should shrink, not crash the indexing pass.

Batch size times sequence length is not under our control — documents arrive whole and
the machine may be sharing its VRAM with a resident speech or chat model — so any fixed
default has to survive a device that says no.
"""
import sys
import types

import numpy as np
import pytest

from dpc_client_core.dpc_agent.memory import (
    EmbeddingProvider,
    _is_out_of_memory,
    _release_device_memory,
)


class _FakeModel:
    """Refuses any batch larger than `capacity`, the way an allocator does.

    Returns an ndarray because that is what sentence-transformers returns and the
    caller relies on `.tolist()`.
    """

    def __init__(self, capacity, exc=None):
        self.capacity = capacity
        self.calls = []
        self._exc = exc or RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")

    def encode(self, texts, normalize_embeddings=True):
        self.calls.append(len(texts))
        if len(texts) > self.capacity:
            raise self._exc
        return np.array([[float(len(t))] for t in texts], dtype=np.float32)


def _provider(model):
    p = EmbeddingProvider.__new__(EmbeddingProvider)
    p._model = model
    return p


def test_oom_is_recognised_by_message_not_type():
    assert _is_out_of_memory(RuntimeError("CUDA out of memory. Tried to allocate 2 GiB"))
    assert _is_out_of_memory(MemoryError())
    assert not _is_out_of_memory(ValueError("bad input"))


def test_batch_is_halved_until_it_fits():
    model = _FakeModel(capacity=2)
    out = _provider(model)._encode_within_memory(["a", "bb", "ccc", "dddd", "e", "ff", "g", "hh"])
    assert len(out) == 8
    assert max(model.calls) == 8  # tried the whole batch first
    assert all(n <= 2 for n in model.calls if n <= 2)


def test_results_keep_their_order_after_splitting():
    model = _FakeModel(capacity=1)
    texts = ["a", "bb", "ccc", "dddd"]
    out = _provider(model)._encode_within_memory(texts)
    assert out == [[1.0], [2.0], [3.0], [4.0]]


def test_single_text_that_still_fails_is_raised():
    """Halving cannot rescue one document; swallowing it would leave a silent hole."""
    model = _FakeModel(capacity=0)
    with pytest.raises(RuntimeError):
        _provider(model)._encode_within_memory(["only one"])


def test_unrelated_error_is_not_retried():
    model = _FakeModel(capacity=0, exc=ValueError("tokenizer exploded"))
    with pytest.raises(ValueError):
        _provider(model)._encode_within_memory(["a", "b", "c", "d"])
    assert model.calls == [4]


def test_batch_that_fits_is_encoded_in_one_call():
    model = _FakeModel(capacity=100)
    _provider(model)._encode_within_memory(["a", "b", "c"])
    assert model.calls == [3]


# --- releasing the allocator's cache before a retry ---


def _fake_torch(freed, *, cuda=True, mps=True, cuda_raises=False, has_mps_module=True):
    """A torch whose accelerators can be present, absent, or broken independently."""
    torch = types.ModuleType("torch")

    def _cuda_empty():
        if cuda_raises:
            raise RuntimeError("driver said no")
        freed.append("cuda")

    torch.cuda = types.SimpleNamespace(
        is_available=lambda: cuda, empty_cache=_cuda_empty
    )
    torch.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(is_available=lambda: mps)
    )
    if has_mps_module:
        torch.mps = types.SimpleNamespace(empty_cache=lambda: freed.append("mps"))
    return torch


def test_both_accelerators_get_their_cache_back(monkeypatch):
    """Apple Silicon shares that memory with the system, so skipping it costs more."""
    freed = []
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(freed))
    _release_device_memory()
    assert freed == ["cuda", "mps"]


def test_only_the_present_accelerator_is_asked(monkeypatch):
    freed = []
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(freed, cuda=False))
    _release_device_memory()
    assert freed == ["mps"]


def test_one_failing_accelerator_does_not_block_the_other(monkeypatch):
    freed = []
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(freed, cuda_raises=True))
    _release_device_memory()
    assert freed == ["mps"]


def test_torch_too_old_for_mps_is_not_an_error(monkeypatch):
    freed = []
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(freed, has_mps_module=False))
    _release_device_memory()
    assert freed == ["cuda"]


def test_no_torch_at_all_is_not_an_error(monkeypatch):
    """The caller is already on its way to a retry; this is best-effort only."""
    monkeypatch.setitem(sys.modules, "torch", None)
    _release_device_memory()
