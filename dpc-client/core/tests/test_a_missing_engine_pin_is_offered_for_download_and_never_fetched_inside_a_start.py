"""A missing llama.cpp pin is offered for download, never fetched inside a start.

Until B2 step 3 a `llamacpp_server` start with no pin on disk called
`ensure_binary` on the owner loop: ~580 MB from GitHub, synchronously, with the
card already claimed — and a peer's call did the same (p2p_coordinator →
provider → supervisor). Now the start refuses before the card, the owner is
asked once through the `model_download_*` consent flow under the name
`llama.cpp`, a peer is never asked for and never fetches, and the download runs
in a thread. No test here reaches the network or the real ~/.dpc.
"""

import asyncio
import threading
from pathlib import Path

import pytest

from dpc_client_core import model_download_service as mds
from dpc_client_core.managers import llama_server_fetcher as fetcher_mod
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers import local_engine_pin_fetcher as pin_mod
from dpc_client_core.managers.llama_server_supervisor import (
    LlamaServerSupervisor,
    PinNotInstalledError,
    serving_a_peer,
)
from dpc_client_core.model_download_service import ModelDownloadService
from dpc_client_core.p2p_coordinator import _engine_unavailable_cause
from dpc_client_core.providers.llamacpp_server_provider import LlamaServerProvider
from dpc_client_core.settings import Settings

ALIAS = "local_qwen38"
NAME = "llama.cpp"


class _FakeLocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty DPC_HOME for the supervisor and both fetchers, and no network."""
    dpc = tmp_path / "dpc-home"
    dpc.mkdir()
    for mod in (base_mod, fetcher_mod, pin_mod):
        monkeypatch.setattr(mod, "DPC_HOME", dpc)

    def _no_network(*a, **kw):
        raise AssertionError("urlopen reached inside the test")

    monkeypatch.setattr(pin_mod.urllib.request, "urlopen", _no_network)
    monkeypatch.setattr(base_mod, "_MODEL_DOWNLOAD_SERVICE", None)
    return dpc


@pytest.fixture
def service(tmp_path, home, monkeypatch):
    svc = ModelDownloadService(llm_manager=None, settings=Settings(tmp_path), local_api=_FakeLocalApi())
    monkeypatch.setattr(base_mod, "_MODEL_DOWNLOAD_SERVICE", svc)
    return svc


@pytest.fixture
def sup(home, monkeypatch):
    """A supervisor whose card claim and launch are recorded, never real."""
    s = LlamaServerSupervisor(ALIAS, {"gguf_path": "D:/m.gguf"})
    s.claims, s.launched = [], []
    real_claim = s._claim_card

    def _claim():
        s.claims.append(1)
        real_claim()

    async def _launch(binary):
        s.launched.append(binary)
        return {"total_slots": 1}

    s._claim_card = _claim
    s._launch_auto_kv = _launch
    return s


def _exact_size():
    return sum(int(a["size"]) for a in fetcher_mod.PLATFORM_ASSETS[fetcher_mod.platform_tag()])


def _refusal(sup):
    with pytest.raises(PinNotInstalledError) as info:
        asyncio.run(sup.ensure_running())
    return info.value


# --- the owner's start asks once and refuses before the card -----------------


def test_the_owners_start_asks_once_and_refuses_before_the_card(sup, service):
    err = _refusal(sup)

    (name, payload), = service.local_api.events
    assert name == "model_download_required"
    assert payload["model_name"] == NAME
    assert payload["size_bytes"] == _exact_size() == fetcher_mod.size_bytes()
    assert payload["size_measured_on"] is None  # the pin's own asset size, not a measurement
    assert payload["on_decline"] == "block"
    assert payload["cache_path"] == str(fetcher_mod.install_root())
    assert payload["provider_alias"] == ALIAS
    assert isinstance(payload["free_bytes"], int)
    assert payload["installed_bytes"] == fetcher_mod.installed_bytes()
    assert payload["installed_bytes"] == fetcher_mod.INSTALLED_BYTES[fetcher_mod.platform_tag()]
    assert err.download_name == NAME
    assert "the owner has been asked" in str(err) and "Do not retry until it has finished" in str(err)
    assert "dialog" not in str(err).lower()
    assert sup.claims == [] and base_mod._CARD_CLAIMS == {}
    assert sup.launched == []

    _refusal(sup)
    assert len(service.local_api.events) == 1, "a second start sends no second event"


def test_a_running_download_is_named_with_its_percent(sup, service):
    service._emitted_this_session.add(NAME)
    service._set_status(NAME, "downloading")
    service._status[NAME]["progress_percent"] = 42.0

    err = _refusal(sup)

    assert "being downloaded now (42% of about" in str(err)
    assert service.local_api.events == []


def test_a_declined_download_says_where_it_can_be_started(sup, service):
    _refusal(sup)
    service.decline(NAME, remember=False)

    err = _refusal(sup)

    assert "the owner has declined; it can be started from the D-PC Messenger app" in str(err)


def test_a_remembered_decline_survives_into_a_fresh_service(sup, service, tmp_path, monkeypatch):
    service.decline(NAME, remember=True)
    fresh = ModelDownloadService(llm_manager=None, settings=Settings(tmp_path), local_api=_FakeLocalApi())
    monkeypatch.setattr(base_mod, "_MODEL_DOWNLOAD_SERVICE", fresh)

    err = _refusal(sup)

    assert "declined" in str(err)
    assert fresh.local_api.events == []


def test_a_restart_can_ask_again_for_the_pin(service, monkeypatch):
    service.decline(NAME, remember=True)
    asked = asyncio.run(service.reset_decline(NAME))
    assert asked is True
    (name, payload), = service.local_api.events
    assert name == "model_download_required" and payload["on_decline"] == "block"


# --- a peer never asks and never fetches ------------------------------------


def test_a_peers_call_neither_asks_nor_fetches(sup, service, monkeypatch):
    monkeypatch.setattr(base_mod, "install_pin", lambda *a: pytest.fail("a peer's call fetched the pin"))

    with serving_a_peer("dpc-node-peer"):
        err = _refusal(sup)

    assert "a peer's request does not fetch it" in str(err)
    assert service.local_api.events == []
    assert sup.claims == []
    wrapped = RuntimeError("llamacpp_server provider failed")
    wrapped.__cause__ = err
    assert _engine_unavailable_cause(wrapped, strata=False)


def test_a_peers_call_does_not_install_even_with_no_service_bound(sup, home, monkeypatch):
    monkeypatch.setattr(base_mod, "install_pin", lambda *a: pytest.fail("a peer's call fetched the pin"))

    with serving_a_peer("dpc-node-peer"):
        err = _refusal(sup)

    assert "a peer's request does not fetch it" in str(err)


# --- no consent service: the install runs in a thread ------------------------


def test_with_no_service_bound_the_install_runs_off_the_loop_thread(sup, home, monkeypatch):
    seen = {}

    def _install(progress=None):
        seen["thread"] = threading.get_ident()
        return Path("C:/bin/llama-server.exe")

    monkeypatch.setattr(base_mod, "install_pin", _install)

    async def run():
        seen["loop"] = threading.get_ident()
        return await sup.ensure_running()

    assert asyncio.run(run()) == {"total_slots": 1}
    assert seen["thread"] != seen["loop"]
    assert sup.launched == [Path("C:/bin/llama-server.exe")]


# --- the download itself ----------------------------------------------------


def test_the_confirmed_download_sends_throttled_progress(service, monkeypatch):
    monkeypatch.setattr(mds, "_free_bytes", lambda path: 10 ** 13)
    total = _exact_size()
    service._clock = lambda: 100.0  # one instant: only the first and the total pass

    def _install(progress=None):
        seen_thread.append(threading.get_ident())
        for i in range(1, 51):
            progress(i * 1000, total)
        progress(total, total)
        return Path("C:/bin/llama-server.exe")

    seen_thread = []
    monkeypatch.setattr(fetcher_mod, "install_pin", _install)

    async def run():
        result = await service.download_model(NAME)
        await asyncio.sleep(0.05)
        return result, threading.get_ident()

    result, loop_thread = asyncio.run(run())

    assert result == {"status": "success", "model_name": NAME}
    assert seen_thread and seen_thread[0] != loop_thread
    names = [n for n, _ in service.local_api.events]
    assert names[0] == "model_download_started" and names[-1] == "model_download_completed"
    progress = [p["bytes_done"] for n, p in service.local_api.events if n == "model_download_progress"]
    assert progress == [1000, total]


def test_too_little_free_space_refuses_the_download(service, monkeypatch):
    size = _exact_size()
    monkeypatch.setattr(mds, "_free_bytes", lambda path: int(size * 1.05))
    monkeypatch.setattr(fetcher_mod, "install_pin", lambda *a: pytest.fail("downloaded without the space"))

    result = asyncio.run(service.download_model(NAME))

    assert result["status"] == "error" and "not enough free disk space" in result["error"]
    assert [n for n, _ in service.local_api.events] == ["model_download_failed"]


def test_space_for_the_unpacked_tree_counts_too(service, monkeypatch):
    # Room for the archives alone is not enough: they unpack beside them.
    size, installed = _exact_size(), fetcher_mod.installed_bytes()
    free = int(size * 1.5)
    assert size * 1.1 < free < (size + installed) * 1.1
    monkeypatch.setattr(mds, "_free_bytes", lambda path: free)
    monkeypatch.setattr(fetcher_mod, "install_pin", lambda *a: pytest.fail("downloaded without the space"))

    result = asyncio.run(service.download_model(NAME))

    assert result["status"] == "error" and "not enough free disk space" in result["error"]


def test_an_unstated_installed_size_is_not_a_known_zero(service, monkeypatch):
    # A module with no installed_bytes (camoufox) is checked against its size alone.
    monkeypatch.delattr(fetcher_mod, "installed_bytes")
    size = _exact_size()
    monkeypatch.setattr(mds, "_free_bytes", lambda path: int(size * 1.1) + 1)
    monkeypatch.setattr(fetcher_mod, "install_pin", lambda progress=None: Path("C:/bin/llama-server.exe"))

    assert asyncio.run(service.maybe_emit_required(
        NAME, purpose="p", consequence_if_declined="c", on_decline="block"
    ))
    assert service.local_api.events[0][1]["installed_bytes"] is None
    assert asyncio.run(service.download_model(NAME))["status"] == "success"


def test_the_download_takes_its_sizes_from_what_the_owner_was_shown(service, monkeypatch):
    asyncio.run(service.maybe_emit_required(NAME, purpose="p", consequence_if_declined="c"))
    monkeypatch.setattr(fetcher_mod, "size_bytes", lambda: pytest.fail("size_bytes read again"))
    monkeypatch.setattr(fetcher_mod, "installed_bytes", lambda: pytest.fail("installed_bytes read again"))
    monkeypatch.setattr(mds, "_free_bytes", lambda path: 10 ** 13)
    monkeypatch.setattr(fetcher_mod, "install_pin", lambda progress=None: Path("C:/bin/llama-server.exe"))

    assert asyncio.run(service.download_model(NAME))["status"] == "success"


def test_the_stored_sizes_decide_the_check(service, monkeypatch):
    # 1651 bytes free is room for the stored 1000 + 500, not for the real pin.
    asyncio.run(service.maybe_emit_required(NAME, purpose="p", consequence_if_declined="c"))
    service._last_required_payload[NAME].update(size_bytes=1000, installed_bytes=500)
    monkeypatch.setattr(mds, "_free_bytes", lambda path: 1651)
    monkeypatch.setattr(fetcher_mod, "install_pin", lambda progress=None: Path("C:/bin/llama-server.exe"))

    assert asyncio.run(service.download_model(NAME))["status"] == "success"


def test_enough_free_space_is_size_plus_installed_times_one_point_one():
    assert mds._short_of_space(1000, 500, 1649)
    assert not mds._short_of_space(1000, 500, 1651)
    assert mds._short_of_space(1000, None, 1099)
    assert not mds._short_of_space(1000, None, 1100)
    assert not mds._short_of_space(None, 500, 1)
    assert not mds._short_of_space(1000, 500, None)
    assert mds._stated_installed_bytes(mds.camoufox_binary) is None


# --- the provider does not retry it -----------------------------------------


def test_the_provider_does_not_retry_a_missing_pin():
    assert LlamaServerProvider._is_retryable(PinNotInstalledError("not installed", NAME)) is False
