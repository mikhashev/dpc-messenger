"""The stop a swap makes is `swap` in the process registry, not `ui_stop`.

Seen in the log 2026-11-04: `process ended: pid N launcher strata reason stopped stop_kind ui_stop`
for a swap nobody pressed. The stop was real and `DEFAULT_STOP` named who asked, wrongly. Every
swap (the idle swap, the card queue, a borrow and its way back) stops the holder in one place,
`_stop_for_the_swap`, so the word is put down there; a person's Stop engine, a settings save and
the rest stay `ui_stop`. Real supervisors on the fakes of the swap tests; the registry's
`note_stop_sent` is the seam, and the fake children are given a pid so the announcement is made.
"""

import ast
import asyncio
import inspect

import pytest

from dpc_client_core import process_registry as procreg
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import stop_by_hand

from tests.test_an_owners_start_swaps_out_an_idle_local_engine_that_holds_the_card import (  # noqa: F401
    _running_pair,
    settings,
    swap_on,
)
from tests.test_one_local_engine_holds_the_card import card  # noqa: F401

PIDS = iter(range(7_950_001, 7_951_000))


@pytest.fixture
def said(monkeypatch):
    """Every stop the supervisors announce to the registry, as (reason, stop_kind)."""
    announced = []
    registry = procreg.get_registry()
    real = registry.note_stop_sent

    def spy(pid, reason, stop=None):
        announced.append((reason, stop))
        return real(pid, reason, stop)

    monkeypatch.setattr(registry, "note_stop_sent", spy)
    return announced


def _with_a_pid(supervisor):
    supervisor._proc.pid = next(PIDS)
    return supervisor


@pytest.mark.asyncio
@pytest.mark.parametrize("holder_kind", ["llama", "strata"])
async def test_an_idle_swap_stops_the_holder_as_a_swap(tmp_path, said, holder_kind):
    holder, new = await _running_pair(tmp_path, holder_kind)
    _with_a_pid(holder)

    await new.ensure_running()

    assert said == [("stopped", "swap")]


@pytest.mark.asyncio
async def test_a_request_that_waited_in_the_card_queue_for_a_busy_holder_stops_it_as_a_swap(tmp_path, said):
    holder, new = await _running_pair(tmp_path, "strata")
    _with_a_pid(holder)
    slot = holder.call_slot()
    await slot.__aenter__()  # a generation in flight: the holder is busy and is not touched
    start = asyncio.ensure_future(new.ensure_running())
    await asyncio.sleep(0.1)
    assert said == [] and not start.done()

    await slot.__aexit__(None, None, None)
    await asyncio.wait_for(start, 10)

    assert said == [("stopped", "swap")]


@pytest.mark.asyncio
async def test_a_persons_stop_engine_is_still_a_ui_stop(tmp_path, said):
    holder, _new = await _running_pair(tmp_path, "llama")
    _with_a_pid(holder)

    await stop_by_hand(holder)

    assert said == [("stopped", "ui_stop")]


@pytest.mark.asyncio
async def test_a_settings_save_that_drains_the_child_is_still_a_ui_stop(tmp_path, said):
    holder, _new = await _running_pair(tmp_path, "llama")
    _with_a_pid(holder)

    await holder.drain(timeout=None)

    assert said == [("stopped", "ui_stop")]


@pytest.mark.asyncio
async def test_a_stop_already_announced_by_a_death_keeps_its_word_through_a_swap(tmp_path, said):
    holder, _new = await _running_pair(tmp_path, "strata")
    _with_a_pid(holder)
    holder._stop_why = "engine_died"  # `_stop_shielded` put it down before the swap's stop arrived

    await stop_by_hand(holder, why="swap")

    assert said == [("stopped", "engine_died")]


@pytest.mark.asyncio
async def test_a_swap_stop_of_a_child_already_gone_leaves_no_word_for_the_next_stop(tmp_path, said):
    holder, _new = await _running_pair(tmp_path, "llama")
    holder._proc.returncode = 1  # it died on its own before the swap reached it
    holder._proc.pid = next(PIDS)

    await stop_by_hand(holder, why="swap")
    assert said == [] and holder._stop_why is None

    await holder.ensure_running()  # the alias serves again, and a person stops it
    _with_a_pid(holder)
    await stop_by_hand(holder)
    assert said == [("stopped", "ui_stop")]


def test_the_word_is_one_the_registry_accepts_and_a_person_s_default_is_still_ui_stop():
    assert "swap" in procreg.STOP_VALUES
    assert base_mod.LlamaServerSupervisor.DEFAULT_STOP == "ui_stop"


def test_every_stop_the_supervisor_makes_for_a_swap_names_the_swap():
    """The idle swap, the card queue's request, a borrow and its way back all stop the holder in
    `_stop_for_the_swap`: a second call to `stop_by_hand` in the supervisor that names no word
    would read `ui_stop` again."""
    tree = ast.parse(inspect.getsource(base_mod))
    calls = [
        (node, scope.name)
        for scope in ast.walk(tree) if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(scope)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "stop_by_hand"
    ]
    assert {name for _, name in calls} == {"_stop_for_the_swap"}
    for node, _name in calls:
        assert [k.value.value for k in node.keywords if k.arg == "why"] == ["swap"]
