"""What a Strata KV snapshot is checked against, what is live to save, and the stop a save leaves
room for (ADR-044, amendment 2026-11-13, points 2, 4 and 6).

- The pre-launch sweep compares the pinned engine version, the model's first shard and the
  configured drafter; drafting auto names no drafter, so it is not compared, and the entry then
  records the drafter the engine reported.
- Slot 0 is live when the pin's `GET /slots` (serve/server.py v0.2.11) gives it
  `n_prompt_tokens` above 0; `[]` (the model unloaded) or 0 is nothing live. Its size is the
  last call's prompt plus reply; no counts is size unknown.
- Before a stop is measured, a save leaves 20 s for the process plus `vram_release_timeout_s`;
  a measured stop already holds the VRAM wait, which runs inside `_stop_child`.
"""

import asyncio
import time

import pytest

from dpc_client_core.managers import kv_snapshots
from dpc_client_core.managers.strata_fetcher import STRATA_ENGINE_VERSION

from tests.test_an_unplugged_kv_snapshots_disk_starts_strata_with_saving_off import (  # noqa: F401
    ALIAS,
    card,
    kv_state,
    saving_strata,
)

BASE_NATIVE = "D:/models/flash-next-00001-of-00002.gguf"  # the fake install's base `--native`


class TestTheIdentity:
    def test_auto_names_the_engine_and_the_model_and_no_drafter(self, tmp_path):
        sup = saving_strata(tmp_path, tmp_path / "kv")
        assert sup._kv_expected_identity() == {"engine_version": STRATA_ENGINE_VERSION, "model": BASE_NATIVE}
        sup.config["drafting"] = "auto"
        assert "drafter" not in sup._kv_expected_identity()

    def test_a_chosen_drafter_and_model_file_are_named(self, tmp_path):
        sup = saving_strata(tmp_path, tmp_path / "kv", drafting="mtp", native_gguf="E:/m/other-00001.gguf")
        assert sup._kv_expected_identity() == {
            "engine_version": STRATA_ENGINE_VERSION, "model": "E:/m/other-00001.gguf", "drafter": "mtp"}

    def test_with_auto_the_entry_records_the_drafter_the_engine_reported(self, tmp_path):
        sup = saving_strata(tmp_path, tmp_path / "kv")
        sup._engine_drafting = "dflash2"
        assert sup._kv_entry_identity()["drafter"] == "dflash2"
        chosen = saving_strata(tmp_path, tmp_path / "kv2", install=tmp_path / "Strata", drafting="mtp")
        chosen._engine_drafting = "dflash2"
        assert chosen._kv_entry_identity()["drafter"] == "mtp"

    @pytest.mark.asyncio
    async def test_the_engine_reports_its_drafter_at_the_start(self, tmp_path):
        sup = saving_strata(tmp_path, tmp_path / "kv")
        real_get = sup._get

        async def get(path):
            body = await real_get(path)
            if path == "/metrics":
                body = {**body, "engine": {**body["engine"], "drafting": "mtp"}}
            return body

        sup._get = get
        await sup.ensure_running()
        assert sup._engine_drafting == "mtp"

    @pytest.mark.asyncio
    async def test_a_changed_drafter_is_swept_before_the_launch(self, tmp_path):
        folder = tmp_path / "kv"
        folder.mkdir()
        sup = saving_strata(tmp_path, folder, drafting="dflash2")
        for name, drafter in (("dpc-kv-01.bin", "mtp"), ("dpc-kv-02.bin", "dflash2")):
            (folder / name).write_bytes(b"\0" * 16)
            entry = kv_snapshots.new_entry(
                file=name, alias=ALIAS, engine="strata",
                identity={"engine_version": STRATA_ENGINE_VERSION, "model": BASE_NATIVE, "drafter": drafter},
                record={"agent_id": "a1", "conversation_id": name}, n_saved=10, nbytes=16, save_ms=1,
                now=time.time())
            assert kv_snapshots.add_entry(folder, entry, cap_bytes=1 << 30)

        await sup.ensure_running()

        assert [e["file"] for e in kv_snapshots.read_index(folder)["entries"]] == ["dpc-kv-02.bin"]
        assert not (folder / "dpc-kv-01.bin").exists()


class TestTheLiveSlot:
    def _sup(self, tmp_path, slots):
        install = tmp_path / "Strata"
        sup = saving_strata(tmp_path, tmp_path / "kv", install=install if install.exists() else None)

        async def read():
            if isinstance(slots, BaseException):
                raise slots
            return slots

        sup.slots = read
        return sup

    def _record(self, prompt, reply):
        kv_snapshots.note_clearable("c1")  # a ConversationMonitor exists for it
        seq = kv_snapshots.note_engine_call(ALIAS, caller_kind="agent", agent_id="a1", conversation_id="c1",
                                            prefix=("ab" * 32, 3), at=time.time())
        kv_snapshots.mark_sent(ALIAS, seq)
        kv_snapshots.note_engine_tokens(ALIAS, seq, prompt, reply)

    @pytest.mark.asyncio
    async def test_an_unloaded_model_or_an_empty_slot_is_nothing_live(self, tmp_path):
        self._record(30_000, 500)
        assert await self._sup(tmp_path, [])._kv_slot_to_save() == (None, None, None)
        held_nothing = [{"id": 0, "n_ctx": 262144, "is_processing": False, "n_prompt_tokens": 0}]
        assert await self._sup(tmp_path, held_nothing)._kv_slot_to_save() == (None, None, None)

    @pytest.mark.asyncio
    async def test_a_held_slot_is_slot_0_sized_by_the_last_calls_prompt_and_reply(self, tmp_path):
        self._record(30_000, 500)
        held = [{"id": 0, "n_ctx": 262144, "is_processing": False, "n_prompt_tokens": 29_000}]
        assert await self._sup(tmp_path, held)._kv_slot_to_save() == (0, 30_500, None)

    @pytest.mark.asyncio
    async def test_a_call_with_no_counts_is_size_unknown(self, tmp_path):
        self._record(None, None)
        held = [{"id": 0, "n_prompt_tokens": 29_000}]
        assert await self._sup(tmp_path, held)._kv_slot_to_save() == (0, None, None)

    @pytest.mark.asyncio
    async def test_a_reload_after_the_last_call_is_not_a_live_conversation(self, tmp_path):
        """The pin keeps `n_prompt_tokens` across an unload and a load, so a model loaded by
        nobody DPC served (the page, an image job) after the last call holds nothing to save."""
        self._record(30_000, 500)
        held = [{"id": 0, "n_prompt_tokens": 29_000}]
        sup = self._sup(tmp_path, held)
        time.sleep(0.05)  # past a tick of Windows' monotonic clock
        sup._kv_loaded_mono = time.monotonic()
        assert await sup._kv_slot_to_save() == (None, None, "the engine was reloaded after the last call")
        time.sleep(0.05)
        self._record(31_000, 200)  # a call sent after the load: live again
        assert await sup._kv_slot_to_save() == (0, 31_200, None)

    @pytest.mark.asyncio
    async def test_the_watcher_notes_a_load_nobody_dpc_served_and_not_a_calls_own(self, tmp_path):
        sup = saving_strata(tmp_path, tmp_path / "kv", model_params={"idle_unload_s": 600},
                            engine_watch_interval_s=0.01)
        await sup.ensure_running()
        self._record(30_000, 500)
        states = iter(["idle", "unloaded", "idle", "unloaded", "idle"])
        polls = []

        async def metrics(path):
            state = next(states, "idle")
            polls.append(state)
            if len(polls) == 5:
                self._record(1_000, 10)  # a call sent between polls 4 and 5 loads it itself
            return {"live": {"state": state}}

        sup._get = metrics
        sup._watch_task = asyncio.get_running_loop().create_task(sup._watch_engine())
        while len(polls) < 3:
            await asyncio.sleep(0.002)
        first_load = sup._kv_loaded_mono
        while len(polls) < 7:
            await asyncio.sleep(0.002)
        await sup.stop()
        assert first_load is not None  # unloaded -> idle with nothing sent: a load nobody served
        assert sup._kv_loaded_mono == first_load  # the call's own load is not one

    @pytest.mark.asyncio
    async def test_slots_that_cannot_be_read_are_a_logged_skip(self, tmp_path):
        slot, tokens, why = await self._sup(tmp_path, OSError("refused"))._kv_slot_to_save()
        assert slot is None and "could not be read" in why


class TestTheStopLeftRoomFor:
    def test_before_a_loaded_stop_is_measured_the_process_and_the_vram_wait(self, tmp_path):
        sup = saving_strata(tmp_path, tmp_path / "kv", vram_release_timeout_s=30.0)
        assert sup._expected_stop_s() == 50.0
        sup._last_stop_s = 1.1  # the base's measure, of any stop: not Strata's
        assert sup._expected_stop_s() == 50.0
        sup._kv_loaded_stop_s = 3.2
        assert sup._expected_stop_s() == 3.2

    @pytest.mark.asyncio
    async def test_a_measured_stop_of_a_loaded_engine_holds_the_vram_wait(self, tmp_path):
        sup = saving_strata(tmp_path, tmp_path / "kv")
        await sup.ensure_running()

        async def slow_release():
            await asyncio.sleep(0.2)
            return True

        sup._verify_vram_released = slow_release
        await sup.stop()

        assert sup._kv_loaded_stop_s >= 0.2
        assert sup._expected_stop_s() == sup._kv_loaded_stop_s

    @pytest.mark.asyncio
    async def test_a_stop_of_an_idle_unloaded_engine_is_never_the_measure(self, tmp_path):
        sup = saving_strata(tmp_path, tmp_path / "kv", vram_release_timeout_s=30.0)
        await sup.ensure_running()
        sup._kv_engine_loaded = False  # the watcher saw it unloaded

        await sup.stop()

        assert sup._kv_loaded_stop_s is None and sup._expected_stop_s() == 50.0


class TestARefusedLaunchKeepsTheRunningLaunchsState:
    @pytest.mark.asyncio
    async def test_a_second_launch_over_a_running_child_resets_nothing(self, tmp_path):
        sup = saving_strata(tmp_path, tmp_path / "kv")
        await sup.ensure_running()
        kv_snapshots.suspend(ALIAS, kv_snapshots.ENGINE_REFUSES_SAVES)

        with pytest.raises(Exception, match="still running"):
            await sup._launch(sup.python_path())

        assert kv_snapshots.off_reason(ALIAS) == kv_snapshots.ENGINE_REFUSES_SAVES
        assert sup._kv_prepared is False and sup._proc is not None
