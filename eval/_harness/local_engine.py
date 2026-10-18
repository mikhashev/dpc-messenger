"""The local engines an eval alias can start, and who holds the card.

Two provider types start a DPC-owned child on this machine's one card:
`llamacpp_server` (ADR-040) and `strata` (ADR-044). They are read here, not
listed again: the types that are local by purpose (`firewall.LOCAL_PROVIDER_TYPES`)
and whose provider class is a `LlamaServerProvider`, the class that owns a
supervisor. Ollama and Whisper are local too and start no child of ours.

The card-holder guard of ADR-044 point 4 (`_claim_card`, `CardHeldError`) lives
inside one process. An eval run is its own process, and the engine that holds
the card is usually the DPC service's child, which that guard cannot see from
here. So the eval asks the card itself — nvidia-smi's compute processes, by
name — and applies the supervisor's own exclusion rule to what it finds.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple


# What one llama-server run of the 27B occupies (model plus KV cache, one
# card, one child); the one figure both eval gates import.
LLAMA_SERVER_VRAM_FLOOR_MIB = 26_000

# Strata sizes its expert cache to the VRAM it finds free (`--expert-cache
# auto`, less `--vram-reserve-mib`), so it has no measured floor; the reading is
# reported, never refused on. The occupancy is context, not need.
STRATA_OBSERVED_OCCUPANCY = "30,743 of 32,623 MiB used with Strata running (2026-10-18, live /metrics)"
STRATA_VRAM_NOTE = (
    "Strata adapts its expert cache to the VRAM it finds free (--expert-cache auto, "
    "--vram-reserve-mib), so no minimum is measured and this reading is not a gate; "
    f"for scale, {STRATA_OBSERVED_OCCUPANCY} — occupancy, not need. "
    "A cache too big for the card pages to system memory instead of failing and shows "
    "only as speed: the tell is a small N in the engine's start line \"N MiB of VRAM "
    "free with everything loaded\", the lever --vram-reserve-mib (the alias's "
    "vram_reserve_mib in the providers editor)"
)


class CardUnavailable:
    """`engines_on_the_card`'s answer when nvidia-smi is there but did not answer:
    falsy like an empty list, but not one — an unread card is not a free card."""

    def __init__(self, error: str):
        self.error = error

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"CardUnavailable({self.error!r})"


def vram_floor_mib(provider_type: Optional[str]) -> Optional[int]:
    """Free VRAM an alias of this type needs before a run, or None when its
    engine has no floor to refuse on (Strata, see STRATA_VRAM_NOTE)."""
    if engine_of(provider_type) == "strata":
        return None
    return LLAMA_SERVER_VRAM_FLOOR_MIB


def supervised_types() -> frozenset:
    """Provider types whose alias starts a supervised engine on the card."""
    from dpc_client_core.firewall import LOCAL_PROVIDER_TYPES
    from dpc_client_core.llm_manager import PROVIDER_MAP
    from dpc_client_core.providers.llamacpp_server_provider import LlamaServerProvider
    return frozenset(
        t for t in LOCAL_PROVIDER_TYPES
        if isinstance(PROVIDER_MAP.get(t), type) and issubclass(PROVIDER_MAP[t], LlamaServerProvider)
    )


def supervisor_class(provider_type: Optional[str]):
    """The supervisor class an alias of this type would start, or None."""
    if provider_type not in supervised_types():
        return None
    from dpc_client_core.llm_manager import PROVIDER_MAP
    return PROVIDER_MAP[provider_type].SUPERVISOR_CLASS


def engine_of(provider_type: Optional[str]) -> Optional[str]:
    cls = supervisor_class(provider_type)
    return cls.ENGINE if cls is not None else None


def engine_executables() -> Dict[str, str]:
    """Executable stem -> engine: each supervised engine's own name, and
    Strata's vision encoder (`strata-vision`), which is Strata on the card."""
    from dpc_client_core.llm_manager import PROVIDER_MAP
    stems = {PROVIDER_MAP[t].SUPERVISOR_CLASS.ENGINE: PROVIDER_MAP[t].SUPERVISOR_CLASS.ENGINE
             for t in supervised_types()}
    if "strata" in stems:
        stems["strata-vision"] = "strata"
    return stems


def engines_on_the_card(run=subprocess.run):
    """(engine, "name (pid N)") for each compute process on the card that is one
    of the supervised engines, matched on its executable stem exactly, case
    aside and with or without `.exe` (`llama-server`, `strata`, `strata-vision`):
    a prefix would take `strata_helper.exe` or `strategy.exe` for Strata.

    None where there is no nvidia-smi to ask; `CardUnavailable` when there is
    one and it did not answer — never an empty list, which reads as a free
    card. By name, never by MiB: Windows does not attribute VRAM per process
    under WDDM (`used_memory` reads `[N/A]`).
    """
    stems = engine_executables()
    try:
        out = run(["nvidia-smi", "--query-compute-apps=pid,process_name",
                   "--format=csv,noheader,nounits"],
                  capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return None
    except Exception as exc:
        return CardUnavailable(f"{type(exc).__name__}: {exc}")
    found: List[Tuple[str, str]] = []
    for line in (getattr(out, "stdout", "") or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",", 1)]
        if len(parts) != 2:
            continue
        pid, name = parts
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        stem = base[:-4] if base.lower().endswith(".exe") else base
        engine = stems.get(stem.lower())
        if engine is not None:
            found.append((engine, f"{base} (pid {pid})"))
    return found


def card_conflicts(provider_type: Optional[str],
                   holders: Optional[List[Tuple[str, str]]]) -> List[str]:
    """The holders that keep this type's engine off the card, by the supervisor's
    own rule (`LlamaServerSupervisor._excludes`, called with the classes): another
    engine, or any engine when either side takes the card whole (Strata). Two
    llama-server children may share; whether they fit is the VRAM gate's question.
    """
    mine = supervisor_class(provider_type)
    if mine is None or not holders or isinstance(holders, CardUnavailable):
        return []
    from dpc_client_core.llm_manager import PROVIDER_MAP
    from dpc_client_core.managers.llama_server_supervisor import LlamaServerSupervisor
    by_engine = {PROVIDER_MAP[t].SUPERVISOR_CLASS.ENGINE: PROVIDER_MAP[t].SUPERVISOR_CLASS
                 for t in supervised_types()}
    return [who for engine, who in holders
            if LlamaServerSupervisor._excludes(mine, by_engine[engine])]


def card_held_message(provider_type: Optional[str], alias: Optional[str],
                      conflicts: List[str]) -> str:
    """The refusal, worded as `CardHeldError` words it in the client."""
    mine = engine_of(provider_type)
    others = ", ".join(conflicts)
    return (f"{mine}[{alias}] cannot load: the card is held by {others}. "
            f"{mine} and the engine holding the card cannot hold it together and DPC "
            f"does not swap them (ADR-044 point 4). Stop that engine — the DPC service's "
            f"local engine, from the providers settings, or the service itself — then "
            f"run again")


# --- The card at the start of a run, as its result records it ------------------

_READ = object()


def card_stamp(entry: Dict[str, Any], *, free_mib: Any = _READ,
               holders: Any = _READ) -> Optional[Dict[str, Any]]:
    """The card when the run started, for its provenance; None for an alias
    that starts no supervised engine. A reading the caller did not pass is taken.

    `free_vram_mib` None means unread, never 0. `card_holders` is a list (empty:
    read and free) or "unknown" with `card_holders_unread` saying why — an unread
    card is not a free one. No expert-cache size: only the engine's log prints
    it, and the eval makes no engine call."""
    ptype = entry.get("type")
    engine = engine_of(ptype)
    if engine is None:
        return None
    if free_mib is _READ:
        from dpc_client_core.managers.llama_server_supervisor import _free_vram_mib
        free_mib = _free_vram_mib()
    if holders is _READ:
        holders = engines_on_the_card()
    stamp: Dict[str, Any] = {
        "read_at": datetime.now(timezone.utc).isoformat(),
        "free_vram_mib": free_mib,
    }
    if isinstance(holders, CardUnavailable):
        stamp["card_holders"] = "unknown"
        stamp["card_holders_unread"] = f"card holder check unavailable: {holders.error}"
    elif holders is None:
        stamp["card_holders"] = "unknown"
        stamp["card_holders_unread"] = "no nvidia-smi on this machine"
    else:
        stamp["card_holders"] = [who for _engine, who in holders]
    facts: Dict[str, Any] = {"name": engine}
    if engine == "strata":
        facts["tested_version"] = entry.get("tested_version")
        facts["tested_version_date"] = entry.get("tested_version_date")
    else:
        try:
            from dpc_client_core.managers.llama_server_fetcher import LLAMA_CPP_TAG
            facts["pinned_tag"] = LLAMA_CPP_TAG
        except Exception as exc:
            facts["pinned_tag"] = f"[unavailable: {type(exc).__name__}]"
        facts["binary_path_configured"] = entry.get("binary_path")
    facts["version_reported"] = None
    stamp["engine"] = facts
    return stamp


def stamp_reported_version(stamp: Optional[Dict[str, Any]], alias: Optional[str]) -> None:
    """Fill `engine.version_reported` from the alias's live supervisor (Strata's
    `engine_version`, else `/props` build_info), with no engine call. Call it
    before the run's shutdown, which drops the supervisor."""
    if not stamp or not alias:
        return
    try:
        from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
        sup = _ACTIVE_SUPERVISORS.get(alias)
        if sup is None:
            return
        version = getattr(sup, "engine_version", None)
        if not version:
            build = (getattr(sup, "props", None) or {}).get("build_info")
            version = build if isinstance(build, str) and build.strip() else None
        stamp["engine"]["version_reported"] = version
    except Exception:
        pass  # a stamp must not cost a run


# --- Strata: what a start would use, read without starting it ------------------

def strata_start_facts(entry: Dict[str, Any]) -> Dict[str, Any]:
    """What a `strata` alias would start, without starting it: the launcher's
    python and script, the base config, the alias's own errors with the paths
    checked (the save's check), the model files of the start config DPC would
    write, and the tested version. Errors are collected, never raised.
    """
    from dpc_client_core.managers.llama_server_supervisor import LlamaServerError
    from dpc_client_core.managers.strata_supervisor import (
        MODEL_FILE_FLAGS, StrataSupervisor, _flag_value, strata_config_errors)
    alias = str(entry.get("alias") or "strata")
    facts: Dict[str, Any] = {
        "config_errors": strata_config_errors(entry, check_paths=True),
        "tested_version": entry.get("tested_version"),
        "tested_version_date": entry.get("tested_version_date"),
        "model_files": {},
    }
    sup = StrataSupervisor(alias, entry)
    try:
        facts["install"] = str(sup.install_dir())
        facts["python"] = str(sup.python_path())
        facts["python_exists"] = sup.python_path().is_file()
        facts["server_script"] = str(sup.server_script())
        facts["server_script_exists"] = sup.server_script().is_file()
        facts["base_config"] = str(sup.base_config_path())
    except LlamaServerError as exc:
        facts["start_error"] = str(exc)
        return facts
    try:
        # Pure apart from reading the base config; nothing is written.
        cfg = sup.build_start_config(0)
    except LlamaServerError as exc:
        facts["start_error"] = str(exc)
        return facts
    args = [str(a) for a in cfg.get("args") or []]
    files = {key: _flag_value(args, flag) for key, flag in MODEL_FILE_FLAGS.items()}
    vision = cfg.get("vision") if isinstance(cfg.get("vision"), dict) else {}
    files["mmproj"] = vision.get("mmproj")
    facts["model_files"] = {k: v for k, v in files.items() if v}
    facts["tokenizer"] = cfg.get("tokenizer")
    return facts


def strata_effort_words(entry: Dict[str, Any]):
    """(words, default) from the tokenizer's chat template the base config names,
    by the provider's own reader; None when unreadable (the provider then uses
    its fallback table). Called unbound with a stand-in: the reader uses the
    instance for its log line only."""
    from dpc_client_core.providers.strata_provider import StrataProvider
    stand_in = SimpleNamespace(alias=str(entry.get("alias") or "strata"))
    return StrataProvider._read_effort_dictionary(stand_in, entry)


def path_record(value: str) -> Dict[str, Any]:
    """Path, kind and size of one model file or folder; no digest (see callers)."""
    p = Path(str(value))
    if p.is_file():
        return {"path": str(p), "kind": "file", "size": p.stat().st_size}
    if p.is_dir():
        return {"path": str(p), "kind": "folder"}
    return {"path": str(p), "error": "does not exist"}
