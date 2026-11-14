"""The swap notice counts the live conversation (ADR-044, amendment 2026-11-13, point 10):
saved, with its size and time, or lost with the reason in the log line's words; and
`last_swap.live_saved` carries the same outcome. Nothing live, or saving off, says nothing
new: `live_saved` is null and the notice is today's.
"""

import asyncio
import logging
import re
from types import SimpleNamespace

import pytest

from dpc_client_core.managers import kv_snapshots
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS

from tests.test_a_swap_saves_the_owners_live_conversation_before_the_stop import (  # noqa: F401
    ALIAS,
    FakeEngine,
    card,
    kv_state,
    note_call,
    saving_pair,
    settings,
    swap_on,
)
from tests.test_one_local_engine_holds_the_card import _llama, _strata


def _swap_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("swapped ")]


@pytest.fixture
def toasts():
    sent = []

    class Api:
        async def broadcast_event(self, name, payload):
            sent.append(payload["message"])

    base_mod.bind_model_download_service(SimpleNamespace(local_api=Api()))
    return sent


@pytest.mark.asyncio
async def test_a_saved_live_conversation_is_said_with_its_size_and_time(tmp_path, caplog, toasts):
    holder, new, fake, _folder = await saving_pair(tmp_path, size=250_000_000)
    note_call(agent="a1")

    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        await new.ensure_running()
    await asyncio.gather(*list(base_mod._SWAP_TOASTS))

    line, = _swap_lines(caplog)
    assert re.fullmatch(
        r"swapped 'qwen27' out for 'flash'; the live conversation of agent 'a1' saved \(0\.2 GB, \d+\.\d s\)",
        line,
    ), line
    assert toasts == [line]
    saved = base_mod.last_swap()["live_saved"]
    assert set(saved) == {"outcome", "bytes", "ms", "reason"}
    assert (saved["outcome"], saved["bytes"]) == (kv_snapshots.SAVED, 250_000_000)


@pytest.mark.asyncio
async def test_a_live_conversation_not_saved_is_said_to_be_lost_with_the_reason(tmp_path, caplog):
    holder, new, fake, _folder = await saving_pair(tmp_path, status=415, body="Unsupported Media Type")
    note_call(conversation="chat-1")

    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        await new.ensure_running()

    saved = base_mod.last_swap()["live_saved"]
    assert saved["outcome"] == kv_snapshots.TRANSPORT and saved["bytes"] is None
    assert _swap_lines(caplog) == [
        f"swapped 'qwen27' out for 'flash'; the live conversation of agent 'a1' is lost, not saved: {saved['reason']}"
    ]
    assert "(415)" in saved["reason"]


@pytest.mark.asyncio
async def test_a_skipped_save_is_said_in_the_skips_words(tmp_path, caplog):
    holder, new, fake, _folder = await saving_pair(tmp_path)
    note_call(kind="gateway", agent=None, conversation=None)

    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        await new.ensure_running()

    assert _swap_lines(caplog) == [
        "swapped 'qwen27' out for 'flash'; the live conversation is lost, not saved: "
        "the last call was a gateway client's"
    ]
    assert base_mod.last_swap()["live_saved"]["outcome"] == base_mod._KV_SKIPPED


@pytest.mark.asyncio
async def test_an_engine_holding_nothing_live_says_nothing_new(tmp_path, caplog):
    holder, new, fake, _folder = await saving_pair(
        tmp_path, slots=[{"id": 0, "n_prompt_tokens": 0}, {"id": 1, "n_prompt_tokens": 0}])
    note_call()

    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        await new.ensure_running()

    assert _swap_lines(caplog) == ["swapped 'qwen27' out for 'flash'"]
    assert base_mod.last_swap()["live_saved"] is None and fake.calls == []


@pytest.mark.asyncio
async def test_with_saving_off_the_notice_is_todays(tmp_path, caplog):
    holder, new = _llama(ALIAS), _strata(tmp_path, "flash")
    await holder.ensure_running()
    _ACTIVE_SUPERVISORS[holder.alias] = holder
    _ACTIVE_SUPERVISORS[new.alias] = new
    fake = FakeEngine(holder, tmp_path)
    note_call()

    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        await new.ensure_running()

    assert _swap_lines(caplog) == ["swapped 'qwen27' out for 'flash'"]
    assert base_mod.last_swap()["live_saved"] is None and fake.slot_reads == 0


@pytest.mark.asyncio
async def test_the_live_part_comes_before_the_parked_part(tmp_path, caplog):
    holder, new = _strata(tmp_path, "flash"), _llama(ALIAS)
    note_call(alias="flash", agent="a1")
    saved = {"outcome": kv_snapshots.SAVED, "bytes": 4_000_000_000, "ms": 2_100, "reason": "saved"}

    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        new._note_swap(holder, parked=1, live_saved=saved)
        new._note_swap(holder, gave_up=True, parked=1, live_saved=saved)

    assert _swap_lines(caplog) == [
        "swapped 'flash' out for 'qwen27'; the live conversation of agent 'a1' saved (4.0 GB, 2.1 s); "
        "its parked conversations are dropped"
    ]
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("stopped ")] == [
        "stopped 'flash' to free the card for 'qwen27', which stopped waiting; the live conversation "
        "of agent 'a1' saved (4.0 GB, 2.1 s); its parked conversations are dropped"
    ]
    assert base_mod.last_swap()["live_saved"] == saved
