"""
DPC Agent - the card status tool.

`card_status` answers in one read-only call what the Agent map shows the owner: the GPU's free and
total VRAM, who holds the card, who waits for it and the processes the driver lists on it
(ADR-046 point 8, ADR-052). It reads what the UI's `list_processes` and `local_engine_status`
read, through the same cached readers, and adds no nvidia-smi call beyond them.

The card queue is the owner's alone (ADR-052 L7): a turn that is not the owner's (a Telegram stranger, a
group with another node) is refused before anything is read, by the gate the file tools use
(`tools.core._not_owner`). Discord never reaches it: it is not in EXTERNAL_SOURCE_ALLOWED_TOOLS.

Every NVIDIA-dependent value is None without nvidia-smi; the answer still carries the holders and the
queue. The owner's queue rows name conversations; those never leave through this tool.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

# Keys of the owner's queue rows that stay with the owner: the chats a request runs for.
_PRIVATE_QUEUE_KEYS = ("conversation_id",)

_NO_NVIDIA_NOTE = (
    "No NVIDIA signal: nvidia-smi is missing or did not answer, so the GPU processes are unknown "
    "(None), not absent."
)
_WDDM_NOTE = (
    "used_memory_mib is None because nvidia-smi reports [N/A] per process under WDDM (the Windows "
    "display driver model); the card's total use is vram_total_mib minus vram_free_mib."
)


_VRAM_NOTE = (
    "vram_free_mib and vram_total_mib are nvidia-smi memory.free and memory.total. Under WDDM free is "
    "not an allocation wall: the driver can page to system RAM (measured on this card: nvidia-smi free "
    "1108 MiB while torch mem_get_info free was ~30 GB). This reading is no proof that a load fits."
)


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _holders() -> Optional[List[Dict[str, Any]]]:
    from ...managers import llama_server_supervisor as engine

    try:
        return engine.card_holders()
    except Exception as e:  # a status read must not fail the turn
        log.warning("card_status: card holders unreadable: %s", e)
        return None


def _queue() -> Optional[List[Dict[str, Any]]]:
    from ...managers import llama_server_supervisor as engine

    try:
        rows = engine.card_queue()
    except Exception as e:
        log.warning("card_status: card queue unreadable: %s", e)
        return None
    return [
        {"position": position, **{k: v for k, v in row.items() if k not in _PRIVATE_QUEUE_KEYS}}
        for position, row in enumerate(rows, start=1)
    ]


_NOT_THE_OWNERS = (
    "⚠️ The card status is not read on this turn: it is for the owner's own turns "
    "(it names who holds the card and who waits for it)."
)


def _card_status(ctx: "ToolContext") -> str:
    """The card's VRAM, holders, queue and GPU processes as JSON (read-only, cached 30 s)."""
    from .core import _not_owner

    if _not_owner(ctx) is not None:
        return _NOT_THE_OWNERS
    from ...managers.commit_memory import (
        gpu_apps_reading_age, read_gpu_compute_apps, read_vram_mib, vram_reading_age,
    )

    try:
        free, total = read_vram_mib()
    except Exception as e:
        log.warning("card_status: VRAM unreadable: %s", e)
        free = total = None
    try:
        apps = read_gpu_compute_apps()
    except Exception as e:
        log.warning("card_status: GPU processes unreadable: %s", e)
        apps = None
    vram_at, vram_age = vram_reading_age()
    apps_at, apps_age = gpu_apps_reading_age()
    note = None
    if apps is None:
        note = _NO_NVIDIA_NOTE
    elif apps and all(app["used_memory_mib"] is None for app in apps):
        note = _WDDM_NOTE
    return json.dumps(
        {
            "vram_free_mib": free,
            "vram_total_mib": total,
            "vram_read_at": _iso(vram_at),
            "vram_age_s": vram_age,
            "vram_note": _VRAM_NOTE,
            "holders": _holders(),
            "queue": _queue(),
            "gpu_processes": apps,
            "gpu_processes_read_at": _iso(apps_at),
            "gpu_processes_age_s": apps_age,
            "gpu_processes_note": note,
        },
        ensure_ascii=False, default=str,
    )


def get_tools() -> List["ToolEntry"]:
    from .registry import ToolEntry

    return [
        ToolEntry(
            name="card_status",
            schema={
                "name": "card_status",
                "description": (
                    "Read-only status of the GPU card in one call: vram_free_mib and vram_total_mib, "
                    "the holders of the card (alias, engine, kind, state, busy, eta_s), the card queue "
                    "in line order, and the GPU compute processes (pid, process_name, used_memory_mib; "
                    "None where the driver gives no figure, the note says why). Values are None without "
                    "an NVIDIA GPU. Readings are cached for 30 s; vram_age_s and gpu_processes_age_s say how "
                    "old they are. It does NOT answer per-process VRAM under WDDM (Windows: always None), "
                    "and vram_free_mib is no proof that a load fits (the driver can page to system RAM)."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            handler=_card_status,
            is_core=True,
            timeout_sec=30,
            default_enabled=True,
        ),
    ]
