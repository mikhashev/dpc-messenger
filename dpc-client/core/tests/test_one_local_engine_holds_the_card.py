"""The card-holder check (ADR-044 point 4): Strata and the llama-server child never
hold the card together, the refusal names the other engine, and a person frees it.

No process runs. Both supervisors start through their real `ensure_running` and
`_launch`; the spawn, `/health`, `/props`, `/metrics` and the VRAM reader are
faked at the supervisors' own seams.
"""

import asyncio
from pathlib import Path

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import (
    CardHeldError,
    LlamaServerSupervisor,
    card_holders,
)
from dpc_client_core.managers.strata_supervisor import StrataSupervisor
from dpc_client_core.providers import LlamaServerProvider, StrataProvider
from dpc_client_core.providers.llamacpp_server_provider import (
    _ACTIVE_SUPERVISORS,
    card_holder,
    stop_local_engine,
)

from tests.test_strata_supervisor import _FakeProc, _install

GGUF = "D:/models/qwen3.8-27b-Q4_K_M.gguf"


@pytest.fixture(autouse=True)
def card(tmp_path, monkeypatch):
    home = tmp_path / "dpc-home"
    home.mkdir()
    monkeypatch.setattr(base_mod, "DPC_HOME", home)
    monkeypatch.setattr(base_mod, "ensure_binary", lambda config: Path("llama-server.exe"))
    monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: None)
    # Constant, so a Strata stop finds the card released at once.
    monkeypatch.setattr(base_mod, "_free_vram_mib", lambda: 30000)
    _ACTIVE_SUPERVISORS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()


def _fake_child(sup, health_delay=0.0):
    """Spawn, /health, /props and /metrics of a child that starts and stays up."""
    spawned = []

    async def spawn(cmd, env):
        proc = _FakeProc()
        spawned.append(proc)
        return proc

    async def health():
        await asyncio.sleep(health_delay)
        return True

    async def get(path):
        if path == "/metrics":
            return {"engine": {"version": "0.1.46"}, "live": {"state": "loaded"}}
        return {"total_slots": 1}

    sup._spawn = spawn
    sup._health_ok = health
    sup._get = get
    # The engine watcher is not under test, and a sleeping one outlives the loop.
    sup._start_watch = lambda: None
    return spawned


def _llama(alias="qwen27", **cfg) -> LlamaServerSupervisor:
    sup = LlamaServerSupervisor(alias, {"gguf_path": GGUF, "cache_type_k": "q8_0",
                                        "cache_type_v": "q8_0", **cfg})
    _fake_child(sup)
    return sup


def _strata(tmp_path, alias="flash") -> StrataSupervisor:
    sup = StrataSupervisor(alias, {
        "strata_install_dir": str(_install(tmp_path)),
        "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240,
        "engine_watch_interval_s": 3600,
        "vram_release_timeout_s": 0.05,
    })
    _fake_child(sup)
    return sup


class TestTheRefusal:
    @pytest.mark.asyncio
    async def test_llama_server_is_refused_while_strata_holds_the_card(self, tmp_path):
        strata = _strata(tmp_path)
        await strata.ensure_running()
        llama = _llama()

        with pytest.raises(CardHeldError) as excinfo:
            await llama.ensure_running()

        message = str(excinfo.value)
        assert "strata alias 'flash'" in message
        assert "held" in message and "Stop 'flash'" in message
        assert excinfo.value.holder_alias == "flash" and excinfo.value.holder_engine == "strata"
        assert llama._proc is None
        assert [h["alias"] for h in card_holders()] == ["flash"]

    @pytest.mark.asyncio
    async def test_strata_is_refused_while_llama_server_holds_the_card(self, tmp_path):
        llama = _llama()
        await llama.ensure_running()
        strata = _strata(tmp_path)

        with pytest.raises(CardHeldError) as excinfo:
            await strata.ensure_running()

        assert "llama-server alias 'qwen27'" in str(excinfo.value)
        assert excinfo.value.holder_engine == "llama-server"
        assert strata._proc is None
        assert strata.start_config_used is None, "a refused start wrote a start config"

    @pytest.mark.asyncio
    async def test_a_retired_child_still_draining_holds_the_card(self, tmp_path):
        llama = _llama("old 27b")
        await llama.ensure_running()
        released = asyncio.Event()

        async def _slow_idle():
            await released.wait()

        llama._wait_idle = _slow_idle
        draining = asyncio.create_task(llama.drain(timeout=None))
        await asyncio.sleep(0)

        with pytest.raises(CardHeldError, match=r"'old 27b' \(draining\)"):
            await _strata(tmp_path).ensure_running()
        draining.cancel()

    @pytest.mark.asyncio
    async def test_the_kv_ladder_names_the_holder_not_the_free_vram(self, tmp_path, monkeypatch):
        # With Strata loaded the card is nearly full, and the ladder's own
        # refusal would blame the model's size; the check runs before it.
        await _strata(tmp_path).ensure_running()
        monkeypatch.setattr(base_mod, "_free_vram_mib", lambda: 2000)
        monkeypatch.setattr(base_mod, "_gguf_mib", lambda path: 15000)
        llama = LlamaServerSupervisor("qwen27", {"gguf_path": GGUF})
        _fake_child(llama)

        with pytest.raises(CardHeldError, match="strata alias 'flash'"):
            await llama.ensure_running()

    @pytest.mark.asyncio
    async def test_a_child_kept_off_the_gpu_starts_beside_strata(self, tmp_path):
        await _strata(tmp_path).ensure_running()
        ocr = _llama("unlimited_ocr", n_gpu_layers=0,
                     extra_args=["--device", "none", "--no-mmproj-offload"])
        assert await ocr.ensure_running() == {"total_slots": 1}
        assert [h["alias"] for h in card_holders()] == ["flash"]

    @pytest.mark.asyncio
    async def test_two_llama_server_aliases_are_left_to_the_arithmetic_as_before(self):
        await _llama("qwen27").ensure_running()
        assert await _llama("bonsai").ensure_running() == {"total_slots": 1}
        assert sorted(h["alias"] for h in card_holders()) == ["bonsai", "qwen27"]

    @pytest.mark.asyncio
    async def test_a_second_strata_alias_is_refused_too(self, tmp_path):
        await _strata(tmp_path / "a", "flash").ensure_running()
        with pytest.raises(CardHeldError, match="strata alias 'flash'"):
            await _strata(tmp_path / "b", "flash parked").ensure_running()

    @pytest.mark.asyncio
    async def test_a_failed_start_lets_go_of_the_card(self, tmp_path):
        llama = _llama()

        async def health():
            return False

        llama._health_ok = health
        llama.config["start_timeout_s"] = 0.05
        with pytest.raises(base_mod.LlamaServerError, match="did not become healthy"):
            await llama.ensure_running()

        assert card_holders() == []
        assert await _strata(tmp_path).ensure_running()


class TestTheHandOff:
    @pytest.mark.asyncio
    async def test_the_same_alias_predecessor_hand_off_still_works(self):
        first = _llama("qwen27")
        await first.ensure_running()
        second = _llama("qwen27", n_ctx=262144)
        second.supersedes(asyncio.ensure_future(first.drain(timeout=None)))

        assert await second.ensure_running() == {"total_slots": 1}
        assert first._proc is None
        assert [(h["alias"], h["state"]) for h in card_holders()] == [("qwen27", "running")]

    @pytest.mark.asyncio
    async def test_an_alias_whose_type_changed_waits_for_its_own_predecessor(self, tmp_path):
        # The same name moving from llama-server to strata is a hand-off, not a
        # conflict: the predecessor's drain is awaited and then the card is free.
        llama = _llama("local")
        await llama.ensure_running()
        strata = _strata(tmp_path, "local")
        strata.supersedes(asyncio.ensure_future(llama.drain(timeout=None)))

        assert await strata.ensure_running() == {"total_slots": 1}
        assert [(h["alias"], h["engine"]) for h in card_holders()] == [("local", "strata")]


class TestTheRace:
    @pytest.mark.asyncio
    async def test_two_engines_started_at_once_exactly_one_wins(self, tmp_path):
        llama = _llama()
        strata = _strata(tmp_path)
        _fake_child(llama, health_delay=0.05)
        _fake_child(strata, health_delay=0.05)

        results = await asyncio.gather(
            llama.ensure_running(), strata.ensure_running(), return_exceptions=True
        )

        won = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, CardHeldError)]
        assert len(won) == 1 and len(refused) == 1, results
        assert len(card_holders()) == 1


class TestTheManualSwitch:
    @pytest.mark.asyncio
    async def test_card_holder_reports_the_alias_and_engine(self, tmp_path):
        assert card_holder() is None
        p = StrataProvider("flash", {
            "type": "strata", "strata_install_dir": str(_install(tmp_path)),
            "strata_base_config": "strata-base.json", "conversation_cache_mib": 10240,
            "engine_watch_interval_s": 3600, "vram_release_timeout_s": 0.05,
        })
        _fake_child(p.supervisor)
        await p.supervisor.ensure_running()

        holder = card_holder()
        assert holder["alias"] == "flash" and holder["engine"] == "strata"
        assert holder["state"] == "running" and holder["port"] == p.supervisor.port

    @pytest.mark.asyncio
    async def test_after_stopping_the_holder_the_other_engine_starts(self, tmp_path):
        strata_p = StrataProvider("flash", {
            "type": "strata", "strata_install_dir": str(_install(tmp_path)),
            "strata_base_config": "strata-base.json", "conversation_cache_mib": 10240,
            "engine_watch_interval_s": 3600, "vram_release_timeout_s": 0.05,
        })
        llama_p = LlamaServerProvider("qwen27", {
            "type": "llamacpp_server", "gguf_path": GGUF,
            "cache_type_k": "q8_0", "cache_type_v": "q8_0",
        })
        _fake_child(strata_p.supervisor)
        _fake_child(llama_p.supervisor)
        await strata_p.supervisor.ensure_running()
        with pytest.raises(CardHeldError):
            await llama_p.supervisor.ensure_running()

        assert await stop_local_engine("flash") is True

        assert card_holder() is None
        assert strata_p.supervisor._proc is None
        assert await llama_p.supervisor.ensure_running() == {"total_slots": 1}
        assert card_holder()["alias"] == "qwen27"
        # The stopped alias is still configured: it refuses only while the card
        # is held, not because it was stopped.
        with pytest.raises(CardHeldError, match="llama-server alias 'qwen27'"):
            await strata_p.supervisor.ensure_running()

    @pytest.mark.asyncio
    async def test_an_unknown_alias_stops_nothing(self):
        assert await stop_local_engine("nobody") is False


class TestNotRetried:
    def test_the_refusal_is_not_retryable_whatever_the_alias_is_called(self):
        # Alias names are free text, and the text test retries on "timeout".
        err = CardHeldError(
            "llama-server[q] cannot load: the card is held by strata alias 'no-timeout flash'",
            "no-timeout flash", "strata",
        )
        assert LlamaServerProvider._is_retryable(err) is False
        assert StrataProvider._is_retryable(err) is False

    @pytest.mark.asyncio
    async def test_the_agent_gets_the_refusal_once_naming_the_other_engine(self, tmp_path):
        await _strata(tmp_path).ensure_running()
        p = LlamaServerProvider("qwen27", {
            "type": "llamacpp_server", "gguf_path": GGUF,
            "cache_type_k": "q8_0", "cache_type_v": "q8_0",
        })
        _fake_child(p.supervisor)

        async def no_retry(fn, err):
            pytest.fail(f"the card-held refusal was retried: {err}")

        p._retry_with_backoff = no_retry
        with pytest.raises(RuntimeError, match="strata alias 'flash'") as excinfo:
            await p.generate_response("hello")
        assert isinstance(excinfo.value.__cause__, CardHeldError)
