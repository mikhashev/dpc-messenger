"""Tests for the model-download consent flow (Mike's call, 2026-09-24):
one event family, a measured/looked-up size instead of a hardcoded number,
and embeddings failing closed like Whisper instead of downloading silently.
"""

import asyncio

import pytest

from dpc_client_core.dpc_agent import memory
from dpc_client_core.model_download_service import ModelDownloadService
from dpc_client_core.providers import model_sizes
from dpc_client_core.providers.base import ModelNotCachedError
from dpc_client_core.settings import Settings


@pytest.fixture(autouse=True)
def _clean_singletons():
    memory._singleton_providers.clear()
    yield
    memory._singleton_providers.clear()


# --- providers/base.py: ModelNotCachedError -------------------------------

def test_size_bytes_derives_download_size_gb_when_not_given():
    e = ModelNotCachedError("m", "/cache", size_bytes=2_293_331_663)
    assert e.download_size_gb == pytest.approx(2.14, abs=0.01)


def test_no_size_at_all_is_none_not_a_guessed_default():
    e = ModelNotCachedError("m", "/cache")
    assert e.size_bytes is None
    assert e.download_size_gb is None


# --- providers/model_sizes.py ---------------------------------------------

def test_table_hit_by_exact_revision():
    size, source = model_sizes.model_size_bytes(
        "BAAI/bge-m3", "5617a9f61b028005a4858fdac845db406aefb181"
    )
    assert size == 2_293_331_663
    assert source == "measured"


def test_a_caller_that_pins_no_revision_still_gets_the_measured_row():
    # The embedding callers pass no revision; before this, their lookup fell
    # through to the whole-repo API sum (4.59 GB with the onnx copy).
    size, source = model_sizes.model_size_bytes("BAAI/bge-m3")
    assert size == 2_293_331_663
    assert source == "measured"


def test_miss_falls_back_to_hf_api(monkeypatch):
    class _Sibling:
        def __init__(self, size):
            self.size = size

    class _Info:
        siblings = [_Sibling(10), _Sibling(20)]

    class _FakeApi:
        def model_info(self, *a, **k):
            return _Info()

    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: _FakeApi())
    size, source = model_sizes.model_size_bytes("some/unmeasured-model")
    assert size == 30
    assert source == "hf_api"


def test_a_miss_offline_answers_none_rather_than_raising(monkeypatch):
    class _FakeApi:
        def model_info(self, *a, **k):
            raise OSError("offline")

    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: _FakeApi())
    size, source = model_sizes.model_size_bytes("some/unreachable-model")
    assert (size, source) == (None, None)


# --- dpc_agent/memory.py: EmbeddingProvider fails closed -------------------

def test_local_files_only_defaults_true():
    p = memory.EmbeddingProvider(model_name="test/fail-closed")
    assert p._local_files_only is True


def test_a_cache_miss_raises_model_not_cached_and_never_retries_without_the_flag(monkeypatch):
    calls = []

    class _FakeST:
        def __init__(self, name, **kwargs):
            calls.append(kwargs)
            if kwargs.get("local_files_only"):
                raise OSError("could not find the files, see offline mode docs")
            raise AssertionError("must not retry without local_files_only")

    import sentence_transformers
    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _FakeST)

    p = memory.EmbeddingProvider(model_name="test/miss", local_files_only=True)
    with pytest.raises(ModelNotCachedError) as exc_info:
        p._load_model()
    assert exc_info.value.model_name == "test/miss"
    assert len(calls) == 1


def test_an_unrelated_oserror_is_not_mistaken_for_a_cache_miss(monkeypatch):
    class _FakeST:
        def __init__(self, name, **kwargs):
            raise OSError("disk is full")

    import sentence_transformers
    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _FakeST)

    p = memory.EmbeddingProvider(model_name="test/diskfull", local_files_only=True)
    with pytest.raises(OSError):
        p._load_model()


def test_the_same_provider_loads_once_the_files_arrive(monkeypatch):
    # After a download the live instance is reused, not replaced: agents hold
    # it, and a second instance would load the model twice.
    import sentence_transformers
    cached = {"yes": False}

    class _FakeST:
        max_seq_length = 8192

        def __init__(self, name, **kw):
            if kw.get("local_files_only") and not cached["yes"]:
                raise OSError("Cannot find the requested files; local_files_only is set")

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", _FakeST)
    p = memory.EmbeddingProvider(model_name="test/arrives-later", device="cpu")
    with pytest.raises(ModelNotCachedError):
        p._load_model()
    cached["yes"] = True
    p._load_model()
    assert p._model is not None


# --- ModelDownloadService: asking policy -----------------------------------

class _FakeLocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def _service(tmp_path):
    return ModelDownloadService(
        llm_manager=None, settings=Settings(tmp_path), local_api=_FakeLocalApi()
    )


def test_required_fires_once_per_session(tmp_path):
    svc = _service(tmp_path)

    async def run():
        first = await svc.maybe_emit_required(
            "BAAI/bge-m3", purpose="test", consequence_if_declined="none"
        )
        second = await svc.maybe_emit_required(
            "BAAI/bge-m3", purpose="test", consequence_if_declined="none"
        )
        return first, second

    first, second = asyncio.run(run())
    assert first is True
    assert second is False
    assert len(svc.local_api.events) == 1
    assert svc.local_api.events[0][0] == "model_download_required"


def test_required_payload_carries_the_measured_size(tmp_path):
    svc = _service(tmp_path)
    asyncio.run(svc.maybe_emit_required(
        "BAAI/bge-m3", purpose="test", consequence_if_declined="none",
    ))
    payload = svc.local_api.events[0][1]
    assert payload["size_bytes"] == 2_293_331_663
    assert payload["size_source"] == "measured"


def test_declined_with_remember_persists_and_suppresses(tmp_path):
    svc = _service(tmp_path)
    svc.decline("m1", remember=True)
    assert svc.settings.get_model_download_declined("m1") is True

    asked = asyncio.run(svc.maybe_emit_required("m1", purpose="p", consequence_if_declined="c"))
    assert asked is False
    assert svc.local_api.events == []


def test_cancel_without_remember_is_not_persisted(tmp_path):
    svc = _service(tmp_path)
    svc.decline("m2", remember=False)
    assert svc.settings.get_model_download_declined("m2") is False
    assert svc.get_status("m2")["state"] == "declined"


def test_reset_decline_clears_the_persisted_flag(tmp_path, monkeypatch):
    svc = _service(tmp_path)
    svc.settings.set_model_download_declined("m3")
    assert svc.settings.get_model_download_declined("m3") is True

    monkeypatch.setattr(
        "dpc_client_core.model_download_service.is_model_cached", lambda name: False
    )
    asyncio.run(svc.reset_decline("m3"))
    assert svc.settings.get_model_download_declined("m3") is False


def test_reset_decline_re_emits_required_when_still_missing(tmp_path, monkeypatch):
    svc = _service(tmp_path)
    monkeypatch.setattr(
        "dpc_client_core.model_download_service.is_model_cached", lambda name: False
    )

    async def run():
        await svc.maybe_emit_required("m4", purpose="p", consequence_if_declined="c")
        svc.decline("m4", remember=True)
        return await svc.reset_decline("m4")

    re_asked = asyncio.run(run())
    assert re_asked is True
    assert svc.settings.get_model_download_declined("m4") is False
    names = [name for name, _ in svc.local_api.events]
    assert names.count("model_download_required") == 2


def test_status_query_reports_missing_by_default(tmp_path):
    svc = _service(tmp_path)
    assert svc.get_status("never-seen")["state"] == "missing"


# --- settings.py: [model_downloads] ----------------------------------------

def test_settings_round_trip_declined_flag(tmp_path):
    s = Settings(tmp_path)
    assert s.get_model_download_declined("BAAI/bge-m3") is False
    s.set_model_download_declined("BAAI/bge-m3")
    assert s.get_model_download_declined("BAAI/bge-m3") is True
    s.clear_model_download_declined("BAAI/bge-m3")
    assert s.get_model_download_declined("BAAI/bge-m3") is False


def test_clearing_an_unset_key_does_not_raise(tmp_path):
    s = Settings(tmp_path)
    s.clear_model_download_declined("never-set")  # must not raise


# --- Call-site degradation --------------------------------------------------

def test_memory_search_reports_the_missing_model_without_raising(monkeypatch):
    from dpc_client_core.dpc_agent.tools import core as tools_core

    class _FakeVector:
        def load(self):
            return True

        def search(self, *a, **k):
            raise AssertionError("must not be reached")

    class _FakeText:
        def load(self):
            return False

    class _FakeBackend:
        vector = _FakeVector()
        text = _FakeText()

    import dpc_client_core.dpc_agent.retrieval as retrieval_pkg
    monkeypatch.setattr(retrieval_pkg, "make_backend_for_agent", lambda root: _FakeBackend())

    def _raise_not_cached(**kwargs):
        raise ModelNotCachedError("BAAI/bge-m3", "/cache")

    monkeypatch.setattr(memory, "get_embedding_provider", _raise_not_cached)

    class _Ctx:
        agent_root = None

    result = tools_core.memory_search(_Ctx(), "hello")
    assert "not downloaded" in result
    assert "BAAI/bge-m3" in result
