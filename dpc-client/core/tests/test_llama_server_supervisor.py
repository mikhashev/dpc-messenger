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
    async def test_q8_0_steps_down_to_q4_0_on_the_childs_own_oom_verdict(self, monkeypatch):
        # The ladder's lower signal: a rung that the arithmetic clears can
        # still die on a real CUDA out-of-memory (fragmentation, a busier
        # desktop than the reserve models). The child's log tail is the
        # verdict; the next rung launches, and the win is memoised with the
        # free-VRAM level it was earned at.
        import dpc_client_core.managers.llama_server_supervisor as mod

        monkeypatch.setattr(mod, "ensure_binary", lambda cfg: BINARY)
        monkeypatch.setattr(mod, "_free_vram_mib", lambda: 31200)
        monkeypatch.setattr(mod, "_total_vram_mib", lambda: None)
        sup, attempts = self._sup_with_launch([
            LlamaServerError("died", ["0.12.019 E CUDA error: out of memory"]),
            {"total_slots": 4},
        ])
        sup._read_fit_memo = lambda: {}
        written = []
        sup._write_fit_memo = lambda key, t, free: written.append((key, t, free))

        props = await sup.ensure_running()

        assert props == {"total_slots": 4}
        assert attempts == ["q8_0", "q4_0"]
        assert written and written[0][1] == "q4_0" and written[0][2] == 31200

    @pytest.mark.asyncio
    async def test_a_non_oom_failure_is_not_stepped_down(self, monkeypatch):
        import dpc_client_core.managers.llama_server_supervisor as mod

        monkeypatch.setattr(mod, "ensure_binary", lambda cfg: BINARY)
        monkeypatch.setattr(mod, "_free_vram_mib", lambda: 31200)
        monkeypatch.setattr(mod, "_total_vram_mib", lambda: None)
        sup, attempts = self._sup_with_launch([
            LlamaServerError("died", ["exited with 3 before becoming healthy"]),
        ])
        sup._read_fit_memo = lambda: {}

        with pytest.raises(LlamaServerError):
            await sup.ensure_running()
        assert attempts == ["q8_0"]

    @pytest.mark.asyncio
    async def test_the_fit_memo_short_circuits_the_ladder(self, monkeypatch):
        import dpc_client_core.managers.llama_server_supervisor as mod

        monkeypatch.setattr(mod, "ensure_binary", lambda cfg: BINARY)
        monkeypatch.setattr(mod, "_free_vram_mib", lambda: 31200)
        monkeypatch.setattr(mod, "_total_vram_mib", lambda: None)
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
        monkeypatch.setattr(mod, "_total_vram_mib", lambda: None)
        monkeypatch.setattr(mod, "_gguf_mib", lambda path: 10000)
        sup, attempts = self._sup_with_launch([
            LlamaServerError("died", ["E CUDA error: out of memory"]),
            {"total_slots": 4},
        ])
        sup._read_fit_memo = lambda: {sup._fit_key(): {"type": "q8_0", "free_mib": 31200}}
        sup._write_fit_memo = lambda *a: None

        await sup.ensure_running()

        assert attempts == ["q8_0", "q4_0"]

    @pytest.mark.asyncio
    async def test_an_alias_that_names_a_type_gets_exactly_one_attempt(self, monkeypatch):
        import dpc_client_core.managers.llama_server_supervisor as mod

        monkeypatch.setattr(mod, "ensure_binary", lambda cfg: BINARY)
        monkeypatch.setattr(mod, "_total_vram_mib", lambda: None)
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

    def test_the_micro_batch_is_sent_only_when_the_alias_names_one(self):
        """Measured 2026-08-20 on this pin: at a 60 000-token prefill ub 1024 is
        worth +5.6 % over the build default of 512 and 2048 a further +2.8 %,
        while at 512-8192 tokens the same flag moves nothing. Silence here means
        the build's own default, which is what every install had before."""
        cmd = _sup(n_ubatch=1024).build_command(BINARY, 1)
        assert cmd[cmd.index("-ub") + 1] == "1024"
        assert "-ub" not in _sup().build_command(BINARY, 1)

    def test_the_logical_batch_rides_beside_it(self):
        cmd = _sup(n_batch=4096, n_ubatch=2048).build_command(BINARY, 1)
        assert cmd[cmd.index("-b") + 1] == "4096"
        assert cmd[cmd.index("-ub") + 1] == "2048"
        assert "-b" not in _sup().build_command(BINARY, 1)

    def test_the_checkpoint_knobs_are_sent_only_when_named(self):
        """A parked deep conversation weighs 12-16 GB of which only ~2.5 GB is
        attention KV; the rest is context checkpoints at ~585-700 MiB each. The
        count is therefore how many conversations fit the host cache, and it has
        to be nameable. Silence keeps the build's own 32 / 8192."""
        cmd = _sup(ctx_checkpoints=4, checkpoint_min_step=2048).build_command(BINARY, 1)
        assert cmd[cmd.index("--ctx-checkpoints") + 1] == "4"
        assert cmd[cmd.index("--checkpoint-min-step") + 1] == "2048"
        bare = _sup().build_command(BINARY, 1)
        assert "--ctx-checkpoints" not in bare and "--checkpoint-min-step" not in bare

    def test_zero_checkpoints_is_a_choice_not_a_silence(self):
        """`0` disables checkpointing entirely, and a truthiness guard would eat
        it — the same trap `-np 1` fell into once already."""
        cmd = _sup(ctx_checkpoints=0, checkpoint_min_step=0).build_command(BINARY, 1)
        assert cmd[cmd.index("--ctx-checkpoints") + 1] == "0"
        assert cmd[cmd.index("--checkpoint-min-step") + 1] == "0"

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
        # Entered the way the provider enters it — `async with` on the slot
        # itself, not on a coroutine wrapper around it.
        async with sup.call_slot():
            draining = asyncio.create_task(sup.drain(timeout=2.0))
            await asyncio.sleep(0.1)
            with pytest.raises(LlamaServerError, match="draining"):
                async with sup.call_slot():
                    pass
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


class TestTheAdmissionArithmetic:
    """The 2026-08-19 spill: WDDM does not refuse an allocation, it pages it
    to shared memory — so an f16 rung "fit" at 262 144 by putting 5.2 GiB
    into system RAM, prefill collapsed from 784 to 47-51 tok/s, and the memo
    recorded the poisoned rung. The fit decision moved from the child's OOM
    verdict to arithmetic done BEFORE the launch: weights + KV bytes for the
    rung + fixed overhead + desktop reserve against physical VRAM."""

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

    def test_the_auto_ladder_has_no_rung_above_q8_0(self):
        from dpc_client_core.managers.llama_server_supervisor import KV_LADDER
        assert KV_LADDER == ("q8_0", "q4_0")

    def test_an_explicit_single_slot_reaches_the_child(self):
        # The old `> 1` guard ate -np 1: the config said 1 while the server
        # ran its own 4 and nobody could see the difference.
        cmd = _sup(n_parallel=1).build_command(BINARY, 1)
        assert cmd[cmd.index("-np") + 1] == "1"
        assert "--kv-unified" not in cmd  # unified == split at one slot

    def test_unset_slots_leave_the_server_its_own_choice(self):
        assert "-np" not in _sup().build_command(BINARY, 1)

    def test_the_kv_formula_matches_the_loader_once_draft_layers_are_excluded(self):
        # 16 live attention layers x 1024 kv-width on the production model;
        # the loader reported 8704 MiB for q8_0 at 262 144 and the formula
        # lands on it exactly. Before the draft filter it counted the MTP
        # block too (17 layers, +6 %) — safe, but unexplained until the load
        # log marked blk.64.attn_* unused.
        from dpc_client_core.managers.llama_server_supervisor import _kv_cache_mib
        assert _kv_cache_mib(16, 1024, 262144, "q8_0") == 8704

    def test_the_gguf_reader_takes_the_kv_axis_not_the_model_axis(self, tmp_path):
        # attn_k is 2-D (n_embd, n_kv_heads x head_dim); the KV width is the
        # SECOND dim. A reader taking the first would oversize the cache by
        # the GQA ratio (5120/1024 here) and refuse rungs that fit.
        import struct

        def tensor(buf, name, dims):
            buf += struct.pack("<Q", len(name)) + name
            buf += struct.pack("<I", len(dims))
            for d in dims:
                buf += struct.pack("<Q", d)
            buf += struct.pack("<I", 0) + struct.pack("<Q", 0)

        buf = bytearray()
        buf += b"GGUF" + struct.pack("<I", 3)
        buf += struct.pack("<QQ", 3, 0)  # three tensors, no metadata
        tensor(buf, b"blk.3.attn_k.weight", (5120, 1024))
        tensor(buf, b"blk.64.attn_k.weight", (5120, 1024))
        tensor(buf, b"blk.64.nextn.eh_proj.weight", (5120, 1024))
        gguf = tmp_path / "m.gguf"
        gguf.write_bytes(bytes(buf))

        from dpc_client_core.managers.llama_server_supervisor import _gguf_attention_kv_dims
        # blk.64 carries nextn tensors: it is the MTP draft layer, its attn_k
        # is marked unused by the loader, and no KV is allocated for it.
        assert _gguf_attention_kv_dims(str(gguf)) == (1, 1024)

    @pytest.mark.asyncio
    async def test_a_rung_the_arithmetic_refuses_is_never_launched(self, monkeypatch):
        # 28 GiB card -> 26624 MiB budget after the reserve. q8_0 predicts
        # 16031 + 9248 + 4608 = 29887 (refused), q4_0 predicts 16031 + 4896
        # + 4608 = 25535 (admitted). Only q4_0 is launched — the refusal
        # happens without a child process, so WDDM is never asked to hold
        # what does not fit.
        import dpc_client_core.managers.llama_server_supervisor as mod

        monkeypatch.setattr(mod, "ensure_binary", lambda cfg: BINARY)
        monkeypatch.setattr(mod, "_free_vram_mib", lambda: 26000)
        monkeypatch.setattr(mod, "_total_vram_mib", lambda: 28672)
        monkeypatch.setattr(mod, "_gguf_mib", lambda path: 16031)
        monkeypatch.setattr(mod, "_gguf_attention_kv_dims", lambda path: (17, 1024))
        sup, attempts = self._sup_with_launch([{"total_slots": 4}])
        sup._read_fit_memo = lambda: {}
        # The memo writer MUST be patched: it writes to the real install path,
        # and the first version of this test left a fake "q4_0 @ 26000" in the
        # production file — the next backend start read it as a hit and served
        # q4_0 instead of running the ladder.
        sup._write_fit_memo = lambda *a: None

        props = await sup.ensure_running()

        assert props == {"total_slots": 4}
        assert attempts == ["q4_0"]

    @pytest.mark.asyncio
    async def test_a_poisoned_memo_the_arithmetic_refuses_falls_to_the_ladder(self, monkeypatch):
        # The 21:27 state, in miniature: the memo says a rung that no longer
        # clears the arithmetic. It must not be trusted into a launch.
        import dpc_client_core.managers.llama_server_supervisor as mod

        monkeypatch.setattr(mod, "ensure_binary", lambda cfg: BINARY)
        monkeypatch.setattr(mod, "_free_vram_mib", lambda: 30000)
        monkeypatch.setattr(mod, "_total_vram_mib", lambda: 28672)
        monkeypatch.setattr(mod, "_gguf_mib", lambda path: 16031)
        monkeypatch.setattr(mod, "_gguf_attention_kv_dims", lambda path: (17, 1024))
        sup, attempts = self._sup_with_launch([{"total_slots": 4}])
        sup._read_fit_memo = lambda: {sup._fit_key(): {"type": "q8_0", "free_mib": 28000}}
        rewritten = []
        sup._write_fit_memo = lambda *a: rewritten.append(a)

        await sup.ensure_running()

        assert attempts == ["q4_0"]
        assert rewritten and rewritten[0][1] == "q4_0"

    @pytest.mark.asyncio
    async def test_a_card_that_cannot_hold_any_rung_fails_without_launching(self, monkeypatch):
        import dpc_client_core.managers.llama_server_supervisor as mod

        monkeypatch.setattr(mod, "ensure_binary", lambda cfg: BINARY)
        monkeypatch.setattr(mod, "_free_vram_mib", lambda: 26000)
        monkeypatch.setattr(mod, "_total_vram_mib", lambda: 28672)
        monkeypatch.setattr(mod, "_gguf_mib", lambda path: 20000)
        monkeypatch.setattr(mod, "_gguf_attention_kv_dims", lambda path: (17, 1024))
        sup, attempts = self._sup_with_launch([])

        with pytest.raises(LlamaServerError, match="no KV rung fits"):
            await sup.ensure_running()
        assert attempts == []

    @pytest.mark.asyncio
    async def test_an_explicit_type_overrides_the_arithmetic_with_a_warning(self, monkeypatch, caplog):
        # The alias owns the consequence: f16 that the arithmetic refuses is
        # still loaded when named explicitly, and the log says so.
        import dpc_client_core.managers.llama_server_supervisor as mod

        monkeypatch.setattr(mod, "ensure_binary", lambda cfg: BINARY)
        monkeypatch.setattr(mod, "_free_vram_mib", lambda: 30000)
        monkeypatch.setattr(mod, "_total_vram_mib", lambda: 28672)
        monkeypatch.setattr(mod, "_gguf_mib", lambda path: 16031)
        monkeypatch.setattr(mod, "_gguf_attention_kv_dims", lambda path: (17, 1024))
        sup, attempts = self._sup_with_launch([{"total_slots": 4}])
        sup.config["cache_type_k"] = "f16"
        sup.config["cache_type_v"] = "f16"

        import logging
        with caplog.at_level(logging.WARNING, logger="dpc_client_core.managers.llama_server_supervisor"):
            props = await sup.ensure_running()

        assert props == {"total_slots": 4}
        assert attempts == [None]
        assert any("overrides arithmetic" in r.message for r in caplog.records)


class TestEngineTimings:
    """The supervisor reads the child's own print_timing lines for the exact
    prefill/decode split — the engine's numbers, so non-streaming callers
    (the agents' tools path) get real phases instead of a blended total."""

    def test_parses_the_last_task_block(self, tmp_path):
        import dpc_client_core.managers.llama_server_supervisor as mod
        sup = _sup()
        log = tmp_path / "llama-server-llama.cpp.log"
        log.write_text(
            "13.27.8 I slot print_timing: id 3 | task 2434 | prompt eval time =    2537.07 ms /  1033 tokens (    2.46 ms per token,   407.16 tokens per second)\n"
            "13.27.8 I slot print_timing: id 3 | task 2434 |        eval time =   20142.44 ms /   671 tokens (   30.06 ms per token,    33.26 tokens per second)\n"
            "13.55.1 I slot print_timing: id 3 | task 2717 | prompt eval time =    3693.67 ms /  1621 tokens (    2.28 ms per token,   438.86 tokens per second)\n"
            "13.55.1 I slot print_timing: id 3 | task 2717 |        eval time =   23196.43 ms /   757 tokens (   30.68 ms per token,    32.59 tokens per second)\n",
            encoding="utf-8",
        )
        sup._log_path = log
        timings = sup.last_task_timings()
        assert timings["prefill_tok_s"] == 438      # the LAST block, not the first
        assert timings["decode_tok_s"] == 32
        assert timings["engine_prompt_tokens"] == 1621
        assert timings["engine_gen_tokens"] == 757

    def test_no_timings_means_none_not_an_estimate(self, tmp_path):
        sup = _sup()
        log = tmp_path / "empty.log"
        log.write_text("nothing here\n", encoding="utf-8")
        sup._log_path = log
        assert sup.last_task_timings() is None
