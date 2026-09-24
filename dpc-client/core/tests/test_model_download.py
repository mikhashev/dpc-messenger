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


def test_memory_search_reports_the_missing_model_when_the_index_never_built(monkeypatch):
    """Finding C: `backend.vector.load()` can return False for the same reason
    the except branch used to report — the index never built because the
    embedding model was never downloaded. Before this, that order gave "No
    memory index yet" instead, hiding the real cause."""
    from dpc_client_core.dpc_agent.tools import core as tools_core

    class _FakeVector:
        def load(self):
            return False  # no index — e.g. it never built

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

    class _FakeProvider:
        model_name = "BAAI/bge-m3"

    monkeypatch.setattr(memory, "get_embedding_provider", lambda **kw: _FakeProvider())
    monkeypatch.setattr(model_sizes, "is_model_cached", lambda name: False)

    class _Ctx:
        agent_root = None

    result = tools_core.memory_search(_Ctx(), "hello")
    assert "not downloaded" in result
    assert "BAAI/bge-m3" in result


def test_memory_search_still_returns_text_results_when_the_embedding_model_is_missing(monkeypatch):
    """Finding C also requires BM25/text search to keep working: checking the
    embedding model's cache state up front must not short-circuit the text
    channel."""
    from dpc_client_core.dpc_agent.tools import core as tools_core

    class _Result:
        def __init__(self, chunk_meta, score):
            self.chunk_meta = chunk_meta
            self.score = score

    class _FakeVector:
        def load(self):
            return False

        def search(self, *a, **k):
            raise AssertionError("must not be reached")

    class _FakeText:
        def load(self):
            return True

        def search(self, query, top_k):
            return [_Result({"source_layer": "L5", "source_file": "notes.md"}, 0.9)]

    class _FakeFuser:
        def fuse(self, vector_results, text_results):
            return text_results

    class _FakeBackend:
        vector = _FakeVector()
        text = _FakeText()
        fuser = _FakeFuser()

    import dpc_client_core.dpc_agent.retrieval as retrieval_pkg
    monkeypatch.setattr(retrieval_pkg, "make_backend_for_agent", lambda root: _FakeBackend())

    class _FakeProvider:
        model_name = "BAAI/bge-m3"

    monkeypatch.setattr(memory, "get_embedding_provider", lambda **kw: _FakeProvider())
    monkeypatch.setattr(model_sizes, "is_model_cached", lambda name: False)

    class _Ctx:
        agent_root = None

    result = tools_core.memory_search(_Ctx(), "hello")
    assert "Found 1 results" in result
    assert "notes.md" in result


# --- model_sizes.py: measured_on travels with the size, finding F ----------


def test_model_size_measured_on_returns_the_dated_row():
    assert model_sizes.model_size_measured_on(
        "BAAI/bge-m3", "5617a9f61b028005a4858fdac845db406aefb181"
    ) == "2026-09-24"


def test_model_size_measured_on_follows_the_same_name_only_fallback_as_the_size():
    assert model_sizes.model_size_measured_on("BAAI/bge-m3") == "2026-09-24"


def test_model_size_measured_on_is_none_for_an_unmeasured_model():
    assert model_sizes.model_size_measured_on("some/unmeasured-model") is None


def test_required_payload_carries_size_measured_on(tmp_path):
    svc = _service(tmp_path)
    asyncio.run(svc.maybe_emit_required(
        "BAAI/bge-m3", purpose="test", consequence_if_declined="none",
    ))
    payload = svc.local_api.events[0][1]
    assert payload["size_measured_on"] == "2026-09-24"


# --- dpc_agent/model_download.py: one cache check, not two, finding G ------


def test_is_model_downloaded_delegates_to_the_single_cache_check(monkeypatch):
    from dpc_client_core.dpc_agent import model_download

    calls = []
    monkeypatch.setattr(
        model_sizes, "is_model_cached",
        lambda name: calls.append(name) or True,
    )
    assert model_download.is_model_downloaded("some/model") is True
    assert calls == ["some/model"]


# --- ModelDownloadService.download_model: the allow-list, finding A --------


class _FakeProviderObj:
    def __init__(self, alias, config):
        self.alias = alias
        self.config = config


class _FakeLlmManagerForDownload:
    def __init__(self, providers=None):
        self.providers = providers or {}


def test_download_model_refuses_an_unknown_name_without_touching_the_network(tmp_path, monkeypatch):
    svc = _service(tmp_path)

    def _must_not_be_called(*a, **k):
        raise AssertionError("SentenceTransformer must not be constructed for a refused name")

    monkeypatch.setattr(
        "dpc_client_core.model_download_service.ModelDownloadService.download_embedding_model",
        _must_not_be_called,
    )

    result = asyncio.run(svc.download_model("some-attacker/arbitrary-repo"))
    assert result["status"] == "error"
    assert "is not one this app downloads" in result["error"]


def test_download_model_accepts_a_measured_table_name(tmp_path, monkeypatch):
    svc = _service(tmp_path)
    calls = []

    async def _fake_download(model_name=None):
        calls.append(model_name)
        return {"status": "success", "model_name": model_name}

    monkeypatch.setattr(svc, "download_embedding_model", _fake_download)
    result = asyncio.run(svc.download_model("BAAI/bge-m3"))
    assert result["status"] == "success"
    assert calls == ["BAAI/bge-m3"]


def test_download_model_accepts_a_configured_local_whisper_model(tmp_path):
    provider = _FakeProviderObj("my_whisper", {"type": "local_whisper", "model": "openai/whisper-tiny"})
    svc = ModelDownloadService(
        llm_manager=_FakeLlmManagerForDownload({"my_whisper": provider}),
        settings=Settings(tmp_path),
        local_api=_FakeLocalApi(),
    )
    assert "openai/whisper-tiny" in svc._allowed_model_names()


def test_download_model_accepts_a_model_already_announced_this_session(tmp_path):
    svc = _service(tmp_path)
    asyncio.run(svc.maybe_emit_required("some/one-off-model", purpose="p", consequence_if_declined="c"))
    assert "some/one-off-model" in svc._allowed_model_names()


def test_download_model_accepts_an_agent_profiles_embedding_model(tmp_path):
    class _FakeFirewall:
        def list_agent_profiles(self):
            return ["researcher"]

        def get_agent_profile_settings(self, name):
            return {"memory": {"embedding_model": "some-org/custom-embedder"}}

    svc = ModelDownloadService(
        llm_manager=None, settings=Settings(tmp_path), local_api=_FakeLocalApi(),
        firewall=_FakeFirewall(),
    )
    assert "some-org/custom-embedder" in svc._allowed_model_names()


# --- ModelDownloadService.reset_decline after a restart, finding B ---------


def test_reset_decline_rebuilds_the_payload_from_the_known_registry_after_a_restart(tmp_path, monkeypatch):
    """A fresh service (empty `_last_required_payload`, as after a process
    restart) with only the persisted decline flag set must still be able to
    re-ask, for a model this service knows about."""
    svc = _service(tmp_path)
    svc.settings.set_model_download_declined("BAAI/bge-m3")
    monkeypatch.setattr(
        "dpc_client_core.model_download_service.is_model_cached", lambda name: False
    )

    assert svc._last_required_payload == {}  # nothing asked yet this process
    re_asked = asyncio.run(svc.reset_decline("BAAI/bge-m3"))

    assert re_asked is True
    assert svc.settings.get_model_download_declined("BAAI/bge-m3") is False
    names = [name for name, _ in svc.local_api.events]
    assert names.count("model_download_required") == 1
    payload = svc.local_api.events[0][1]
    assert payload["purpose"]  # non-empty: came from the registry, not an empty prior ask


def test_reset_decline_still_declines_to_re_ask_for_a_name_it_has_never_seen(tmp_path, monkeypatch):
    svc = _service(tmp_path)
    svc.settings.set_model_download_declined("never-asked-about")
    monkeypatch.setattr(
        "dpc_client_core.model_download_service.is_model_cached", lambda name: False
    )
    re_asked = asyncio.run(svc.reset_decline("never-asked-about"))
    assert re_asked is False
    assert svc.local_api.events == []


# --- service.py transcribe_audio / telegram_coordinator.py, finding D ------


def test_telegram_branches_on_the_model_not_cached_field_not_the_wording(tmp_path):
    """The branch must survive a reworded message: it reads the structural
    `model_not_cached` field `transcribe_audio` returns, not the text
    (finding D, 2026-09-24). Exercises the real
    `TelegramBridge._transcribe_voice_and_notify`, not a reimplementation."""
    from dpc_client_core.coordinators.telegram_coordinator import TelegramBridge

    sent = []

    class _FakeTelegram:
        async def send_message(self, chat_id, text):
            sent.append(text)

    class _FakeService:
        async def transcribe_audio(self, **kwargs):
            return {
                "status": "error",
                "error": "a completely reworded sentence about the model",
                "model_not_cached": "openai/whisper-large-v3-turbo",
            }

    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge.telegram = _FakeTelegram()
    bridge.service = _FakeService()

    voice_path = tmp_path / "v.ogg"
    voice_path.write_bytes(b"fake audio")

    text, provider = asyncio.run(
        bridge._transcribe_voice_and_notify("chat1", voice_path, "whisper1")
    )

    assert text is None and provider is None
    assert sent == ["⚠️ a completely reworded sentence about the model"]


def test_telegram_does_not_mistake_an_unrelated_error_for_a_missing_model(tmp_path):
    """Falsifier for finding D: an ordinary error dict (no `model_not_cached`)
    must fall to the generic branch, capped and prefixed, not the
    model-not-cached one."""
    from dpc_client_core.coordinators.telegram_coordinator import TelegramBridge

    sent = []

    class _FakeTelegram:
        async def send_message(self, chat_id, text):
            sent.append(text)

    class _FakeService:
        async def transcribe_audio(self, **kwargs):
            return {"status": "error", "error": "network unreachable"}

    bridge = TelegramBridge.__new__(TelegramBridge)
    bridge.telegram = _FakeTelegram()
    bridge.service = _FakeService()

    voice_path = tmp_path / "v.ogg"
    voice_path.write_bytes(b"fake audio")

    asyncio.run(bridge._transcribe_voice_and_notify("chat1", voice_path, "whisper1"))

    assert sent == ["⚠️ Transcription failed: network unreachable"]
