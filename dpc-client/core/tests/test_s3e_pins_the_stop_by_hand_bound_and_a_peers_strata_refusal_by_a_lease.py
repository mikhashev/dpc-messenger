"""Two of Zcode's open items on the S2 code (group #22, from #84 of 2026-11-22), closed in slice S3e.

(iii) `_stop_local_engine` on a lease alias: no caller passes a `timeout`, so `let_go_by_hand` waits up
to `LEASE_STOP_BY_HAND_S` (120 s) inside the call. The UI's command timeout for `stop_local_engine`
must outlast it, or the UI cuts a stop the backend still runs. Pinned, as the carrier chain.

(iv) `_card_held_error` / `_why_a_peer_may_not_swap` are the two places a holder's name can reach a
refusal text, and Strata's peer start gate (`strata_provider._peer_start_refusal`) reads both. The P2P
door asks that gate before it marks the call as a peer's (`_local_engine_refusal` runs outside
`serving_a_peer`), so `_card_held_error` answered with the owner's words: a card lease holding the card
reached the foreign node as "the card is held by whisper alias 'whisper:turbo'". A peer's refusal names
no lease holder (ADR-052 L3, L13; ADR-044 point 7); the gate now asks under the peer mark.
"""

import asyncio
import re
from pathlib import Path

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import LEASE_STOP_BY_HAND_S, serving_a_peer
from dpc_client_core.providers import StrataProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS

from tests._card_lease_helpers import BOUND, fast, hold_open, until, whisper  # noqa: F401
from tests.test_strata_supervisor import _install

CORE_SERVICE_TS = Path(__file__).resolve().parents[2] / "ui" / "src" / "lib" / "coreService.ts"
PEER = "peer-1"


def test_iii_the_uis_stop_outlasts_the_backends_stop_by_hand_bound():
    source = CORE_SERVICE_TS.read_text(encoding="utf-8")
    found = re.search(r"command === 'stop_local_engine'\)\s*\{[^}]*?timeout = (\d+);", source)
    assert found, "the stop_local_engine branch of sendCommand's timeouts"
    assert int(found.group(1)) > LEASE_STOP_BY_HAND_S * 1000 == 120_000


@pytest.fixture
def stopped_strata(tmp_path, monkeypatch):
    monkeypatch.setattr(base_mod, "DPC_HOME", tmp_path / "dpc-home")
    monkeypatch.setattr(base_mod, "_free_vram_mib", lambda **_: 32000)
    monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: 32623)
    _ACTIVE_SUPERVISORS.clear()
    p = StrataProvider("flash", {
        "type": "strata", "alias": "flash", "model": "qwen3.8-flash-next-iq3_xxs",
        "strata_install_dir": str(_install(tmp_path)), "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240, "context_window": 200000,
    })
    started = []

    async def ensure_running():
        started.append(True)
        return {}

    p.supervisor.ensure_running = ensure_running
    yield p, started
    _ACTIVE_SUPERVISORS.clear()


@pytest.mark.asyncio
@pytest.mark.usefixtures("fast")
@pytest.mark.parametrize("marked", [False, True], ids=["as the P2P door asks", "under the peer mark"])
@pytest.mark.parametrize("use", [False, True], ids=["idle lease", "busy lease"])
async def test_iv_a_peers_strata_refusal_by_a_lease_names_no_holder(stopped_strata, marked, use):
    p, started = stopped_strata
    holder = whisper("turbo")
    over = asyncio.Event()
    task = None
    if use:
        task = asyncio.create_task(hold_open(holder.lease, "dictation", over))
        await until(lambda: holder.lease.granted)
    else:
        await asyncio.wait_for(holder.lease.acquire("dictation", wait_s=BOUND), BOUND)
    base_mod.bind_local_engine_settings(None)  # swap off: the owner's switch decides, not a test default

    if marked:
        with serving_a_peer(PEER):
            reason = await asyncio.wait_for(p.peer_refusal(PEER), BOUND)
    else:
        reason = await asyncio.wait_for(p.peer_refusal(PEER), BOUND)

    assert "this node's GPU is busy with another job right now" in reason
    assert "whisper" not in reason.lower() and "turbo" not in reason
    assert started == []
    over.set()
    if task is not None:
        await asyncio.wait_for(task, BOUND)
    holder.lease.release("test")
