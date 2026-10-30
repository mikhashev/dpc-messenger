"""A Hugging Face size read is bounded and kept off the event loop.

`reset_model_download_decline` offers a declined download again, and the offer
is sized first. A model with no measured row is sized by `HfApi.model_info`,
which ran on the event loop with no timeout: while Hugging Face took its time,
every coroutine of the backend waited with it. The read now runs in a thread
with a 20 s timeout, and the handler is a slow_command, off the UI socket's
read loop (Mike's call, 2026-10-30). No test here reaches the network.
"""

import asyncio
import threading

from dpc_client_core import model_download_service as mds
from dpc_client_core.model_download_service import ModelDownloadService
from dpc_client_core.providers import model_sizes
from dpc_client_core.settings import Settings


class _FakeLocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def test_the_offer_sizes_an_unmeasured_model_off_the_event_loop(tmp_path, monkeypatch):
    sized_on = []

    def _size(name, revision=None):
        sized_on.append(threading.get_ident())
        return 1234, "hf_api"

    monkeypatch.setattr(mds, "model_size_bytes", _size)
    monkeypatch.setattr(mds, "hf_cache_path", lambda: str(tmp_path / "hub"))
    service = ModelDownloadService(llm_manager=None, settings=Settings(tmp_path), local_api=_FakeLocalApi())

    async def offer():
        await service.maybe_emit_required(
            "some/unmeasured-model", purpose="p", consequence_if_declined="c",
        )
        return threading.get_ident()

    loop_thread = asyncio.run(offer())

    assert sized_on and sized_on[0] != loop_thread


def test_the_hugging_face_read_carries_a_timeout(monkeypatch):
    seen = {}

    class _Info:
        siblings = []

    class _FakeApi:
        def model_info(self, *a, **k):
            seen.update(k)
            return _Info()

    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda: _FakeApi())

    model_sizes.model_size_bytes("some/unmeasured-model")

    assert seen.get("timeout") == 20
