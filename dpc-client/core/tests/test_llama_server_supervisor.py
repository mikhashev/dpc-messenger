"""The llama-server supervisor: flags, health, drain and the stop that flushes.

One supervisor owns one `llama-server` child per alias (ADR-040 route b2).
These tests pin the contract without an engine: the flag table, that
`GGML_BACKEND_PATH` reaches the child's environment (its absence once left a
server silently on the CPU for ten minutes), that a start which never becomes
healthy fails with the child's last log lines attached, and that stop asks
the child to stop with a signal that lets it flush — `terminate()` is banned
in that file because on Windows it discards the stdio buffers, which is how
a week of server logs were lost.
"""

import asyncio
import signal
import sys

import pytest

from dpc_client_core.managers.llama_server_supervisor import LlamaServerError, LlamaServerSupervisor

BINARY = "C:/bin/llama-server.exe"
GGUF = "D:/models/qwen3.8-27b-Q4_K_M.gguf"


def _sup(**overrides):
    config = {"gguf_path": GGUF}
    config.update(overrides)
    return LlamaServerSupervisor("local_qwen38", config)


class _FakeProc:
    returncode = None

    def __init__(self):
        self.calls = []

    def send_signal(self, sig):
        self.calls.append(("signal", sig))

    def kill(self):
        self.calls.append(("kill",))

    async def wait(self):
        self.returncode = 0
        self.calls.append(("waited",))
        return 0


class TestFlagAssembly:
    """One row per option the supervisor owns, as the plan requires."""

    def test_model_and_context_and_host_port_are_always_there(self):
        cmd = _sup(n_ctx=131072).build_command(BINARY, 8091)
        assert cmd[0] == BINARY
        assert cmd[1:3] == ["-m", GGUF]
        assert cmd[3:5] == ["-c", "131072"]
        assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
        assert cmd[cmd.index("--port") + 1] == "8091"

    def test_kv_types_map_to_their_flags(self):
        cmd = _sup(cache_type_k="q8_0", cache_type_v="q4_0").build_command(BINARY, 1)
        assert "-ctk" in cmd and cmd[cmd.index("-ctk") + 1] == "q8_0"
        assert "-ctv" in cmd and cmd[cmd.index("-ctv") + 1] == "q4_0"

    def test_gpu_layers_default_to_everything_after_the_split_measurement(self):
        # Without an explicit -ngl the server parked one context entirely on the
        # CPU (0/66) and the drafts at 57-59/66, which cost 11.3 % of
        # prefill; the default makes that loss impossible to forget.
        cmd = _sup().build_command(BINARY, 1)
        assert cmd[cmd.index("-ngl") + 1] == "999"

    def test_kv_defaults_to_q8_after_the_first_live_call_oomed_on_f16(self):
        # Superseded the same evening by the VRAM ladder: None now means the
        # supervisor resolves the rung against free memory at launch. What
        # stays pinned here is that an unconfigured alias launches with SOME
        # quantised cache once the ladder has spoken, never bare f16.
        cmd = _sup().build_command(BINARY, 1, cache_type="q8_0")
        assert cmd[cmd.index("-ctk") + 1] == "q8_0"
        assert cmd[cmd.index("-ctv") + 1] == "q8_0"


class TestTheKvLadder:
    """The KV type is chosen by what the card can hold, not by a constant."""

    def _sup_with_launch(self, outcomes):
        sup = _sup()
        attempts = []

        async def fake_launch(binary, cache_type=None):
            attempts.append(cache_type)
            outcome = outcomes[len(attempts) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        sup._launch = fake_launch
        return sup, attempts

    @pytest.mark.asyncio
    async def test_f16_steps_down_to_q8_0_on_the_childs_own_oom_verdict(self, monkeypatch):
        # The 18:09 live call: f16 KV at 262K died with CUDA out-of-memory on
        # a card where q8_0 fills 31.9 of 32 GB. The child's log tail is the
        # verdict; the next rung launches, and the win is memoised with the
        # free-VRAM level it was earned at.
        import dpc_client_core.managers.llama_server_supervisor as mod

        monkeypatch.setattr(mod, "ensure_binary", lambda cfg: BINARY)
        monkeypatch.setattr(mod, "_free_vram_mib", lambda: 31200)
        sup, attempts = self._sup_with_launch([
            LlamaServerError("died", ["0.12.019 E CUDA error: out of memory"]),
            {"total_slots": 4},
        ])
        sup._read_fit_memo = lambda: {}
        written = []
        sup._write_fit_memo = lambda key, t, free: written.append((key, t, free))

        props = await sup.ensure_running()

        assert props == {"total_slots": 4}
        assert attempts == ["f16", "q8_0"]
        assert written and written[0][1] == "q8_0" and written[0][2] == 31200

    @pytest.mark.asyncio
    async def test_a_non_oom_failure_is_not_stepped_down(self, monkeypatch):
        import dpc_client_core.managers.llama_server_supervisor as mod

        monkeypatch.setattr(mod, "ensure_binary", lambda cfg: BINARY)
        monkeypatch.setattr(mod, "_free_vram_mib", lambda: 31200)
        sup, attempts = self._sup_with_launch([
            LlamaServerError("died", ["exited with 3 before becoming healthy"]),
        ])
        sup._read_fit_memo = lambda: {}

        with pytest.raises(LlamaServerError):
            await sup.ensure_running()
        assert attempts == ["f16"]

    @pytest.mark.asyncio
    async def test_the_fit_memo_short_circuits_the_ladder(self, monkeypatch):
        import dpc_client_core.managers.llama_server_supervisor as mod

        monkeypatch.setattr(mod, "ensure_binary", lambda cfg: BINARY)
        monkeypatch.setattr(mod, "_free_vram_mib", lambda: 31200)
        sup, attempts = self._sup_with_launch([{"total_slots": 4}])
        sup._read_fit_memo = lambda: {sup._fit_key(): {"type": "q8_0", "free_mib": 28000}}
        rewritten = []
        sup._write_fit_memo = lambda *a: rewritten.append(a)

        await sup.ensure_running()

        assert attempts == ["q8_0"]
        assert rewritten == []

    @pytest.mark.asyncio
    async def test_a_busier_card_re_runs_the_ladder_instead_of_trusting_the_memo(self, monkeypatch):
        import dpc_client_core.managers.llama_server_supervisor as mod

        monkeypatch.setattr(mod, "ensure_binary", lambda cfg: BINARY)
        monkeypatch.setattr(mod, "_free_vram_mib", lambda: 20000)
        monkeypatch.setattr(mod, "_gguf_mib", lambda path: 10000)
        sup, attempts = self._sup_with_launch([
            LlamaServerError("died", ["E CUDA error: out of memory"]),
            LlamaServerError("died", ["E CUDA error: out of memory"]),
            {"total_slots": 4},
        ])
        sup._read_fit_memo = lambda: {sup._fit_key(): {"type": "q8_0", "free_mib": 31200}}
        sup._write_fit_memo = lambda *a: None

        await sup.ensure_running()

        assert attempts == ["f16", "q8_0", "q4_0"]

    @pytest.mark.asyncio
    async def test_an_alias_that_names_a_type_gets_exactly_one_attempt(self, monkeypatch):
        import dpc_client_core.managers.llama_server_supervisor as mod

        monkeypatch.setattr(mod, "ensure_binary", lambda cfg: BINARY)
        sup, attempts = self._sup_with_launch([{"total_slots": 4}])
        sup.config["cache_type_k"] = "q4_0"

        await sup.ensure_running()

        assert attempts == [None]

    @pytest.mark.asyncio
    async def test_a_card_that_cannot_hold_even_the_model_fails_fast(self, monkeypatch):
        import dpc_client_core.managers.llama_server_supervisor as mod

        monkeypatch.setattr(mod, "ensure_binary", lambda cfg: BINARY)
        monkeypatch.setattr(mod, "_free_vram_mib", lambda: 8000)
        monkeypatch.setattr(mod, "_gguf_mib", lambda path: 17000)
        sup, attempts = self._sup_with_launch([])

        with pytest.raises(LlamaServerError, match="no KV type will fit"):
            await sup.ensure_running()
        assert attempts == []

    def test_the_oom_verdict_reader(self):
        from dpc_client_core.managers.llama_server_supervisor import _looks_like_oom

        assert _looks_like_oom(["0.12.019 E CUDA error: out of memory"])
        assert _looks_like_oom(["ggml_backend_cuda_host_malloc: failed to allocate 1 MiB"])
        assert not _looks_like_oom(["exited with 3221226505 before becoming healthy"])
        assert not _looks_like_oom([])

    def test_gpu_layers_and_flash_attn_and_mmproj(self):
        cmd = _sup(n_gpu_layers=999, flash_attn=True, mmproj="mm.gguf").build_command(BINARY, 1)
        assert cmd[cmd.index("-ngl") + 1] == "999"
        assert "--flash-attn" in cmd
        assert cmd[cmd.index("--mmproj") + 1] == "mm.gguf"

    def test_speculation_defaults_to_mtp_depth_three_the_measured_value(self):
        cmd = _sup().build_command(BINARY, 1)
        assert cmd[cmd.index("--spec-type") + 1] == "draft-mtp"
        assert cmd[cmd.index("--spec-draft-n-max") + 1] == "3"

    def test_parallel_slots_come_with_unified_kv_and_one_slot_comes_alone(self):
        cmd = _sup(n_parallel=2).build_command(BINARY, 1)
        assert cmd[cmd.index("-np") + 1] == "2" and "--kv-unified" in cmd
        solo = _sup().build_command(BINARY, 1)
        assert "-np" not in solo and "--kv-unified" not in solo

    def test_cache_ram_and_slot_save_path(self):
        cmd = _sup(cache_ram_mib=24576, slot_save_path="D:/kv").build_command(BINARY, 1)
        assert cmd[cmd.index("--cache-ram") + 1] == "24576"
        assert cmd[cmd.index("--slot-save-path") + 1] == "D:/kv"

    def test_jinja_is_on_by_default_and_can_be_refused(self):
        assert "--jinja" in _sup().build_command(BINARY, 1)
        assert "--jinja" not in _sup(jinja=False).build_command(BINARY, 1)

    def test_extra_args_go_last_as_an_escape_hatch(self):
        cmd = _sup(extra_args=["--overarch", "x"]).build_command(BINARY, 1)
        assert cmd[-1] == "x" and cmd[-2] == "--overarch"


class TestTheEnvironment:
    def test_ggml_backend_path_reaches_the_child(self, tmp_path):
        (tmp_path / "ggml-cuda.dll").write_bytes(b"")
        env = _sup().build_env(tmp_path / "llama-server.exe")
        assert env["GGML_BACKEND_PATH"].endswith("ggml-cuda.dll")

    def test_no_backend_found_leaves_the_environment_alone(self, tmp_path):
        assert "GGML_BACKEND_PATH" not in _sup().build_env(tmp_path / "llama-server.exe")


class TestStopAndDrain:
    @pytest.mark.asyncio
    async def test_stop_signals_the_group_and_never_terminates(self):
        sup = _sup()
        proc = _FakeProc()
        sup._proc = proc
        await sup.stop()
        signals = [c for c in proc.calls if c[0] == "signal"]
        expected = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
        assert signals == [("signal", expected)]
        assert not any(c[0] == "kill" for c in proc.calls)
        assert ("waited",) in proc.calls

    @pytest.mark.asyncio
    async def test_a_dead_process_is_not_signalled_again(self):
        sup = _sup()
        proc = _FakeProc()
        proc.returncode = 1
        sup._proc = proc
        await sup.stop()
        assert proc.calls == []

    @pytest.mark.asyncio
    async def test_drain_refuses_new_work_and_waits_for_the_in_flight_call(self):
        sup = _sup()
        sup._proc = _FakeProc()
        slot = await sup.call_slot()
        draining = asyncio.create_task(sup.drain(timeout=2.0))
        await asyncio.sleep(0.1)
        with pytest.raises(LlamaServerError, match="draining"):
            await sup.call_slot()
        async with slot:
            await asyncio.sleep(0.1)
        await draining
        assert sup._draining and sup._in_flight == 0


class TestStart:
    @pytest.mark.asyncio
    async def test_a_start_that_never_becomes_healthy_raises_with_the_log_tail(
        self, tmp_path, monkeypatch
    ):
        from pathlib import Path

        from dpc_client_core.managers import llama_server_supervisor as mod

        monkeypatch.setattr(mod, "DPC_HOME", tmp_path)
        monkeypatch.setattr(mod, "ensure_binary", lambda config: Path(BINARY))
        sup = _sup(start_timeout_s=0.3)
        monkeypatch.setattr(sup, "_spawn", lambda cmd, env: _ret(_FakeProc()))
        monkeypatch.setattr(sup, "_health_ok", lambda: _ret(False))

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "llama-server-local_qwen38.log").write_bytes(
            b"common_init: loading model\nfailed to load\n"
        )

        with pytest.raises(LlamaServerError) as excinfo:
            await sup.ensure_running()
        assert "did not become healthy" in str(excinfo.value)
        assert "failed to load" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_child_that_dies_before_health_reports_its_exit(self, tmp_path, monkeypatch):
        from pathlib import Path

        from dpc_client_core.managers import llama_server_supervisor as mod

        monkeypatch.setattr(mod, "DPC_HOME", tmp_path)
        monkeypatch.setattr(mod, "ensure_binary", lambda config: Path(BINARY))
        sup = _sup(start_timeout_s=30.0)
        dead = _FakeProc()
        dead.returncode = 127
        monkeypatch.setattr(sup, "_spawn", lambda cmd, env: _ret(dead))
        monkeypatch.setattr(sup, "_health_ok", lambda: _ret(True))

        with pytest.raises(LlamaServerError, match="exited with 127"):
            await sup.ensure_running()


async def _ret(value):
    return value
