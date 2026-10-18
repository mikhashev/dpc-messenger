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
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple


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


def engines_on_the_card(run=subprocess.run) -> Optional[List[Tuple[str, str]]]:
    """(engine, "name (pid N)") for each compute process on the card that is one
    of the supervised engines, matched on its file name (`llama-server.exe`,
    `strata.exe`, `strata-vision.exe`). None where there is no nvidia-smi to ask.

    By name, never by MiB: Windows does not attribute VRAM per process under
    WDDM (`used_memory` reads `[N/A]`). An unreadable answer is an empty list —
    a held card then shows as short VRAM, which the VRAM gate still catches.
    """
    from dpc_client_core.llm_manager import PROVIDER_MAP
    engines = sorted({PROVIDER_MAP[t].SUPERVISOR_CLASS.ENGINE for t in supervised_types()},
                     key=len, reverse=True)
    try:
        out = run(["nvidia-smi", "--query-compute-apps=pid,process_name",
                   "--format=csv,noheader,nounits"],
                  capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return None
    except Exception:
        return []
    found: List[Tuple[str, str]] = []
    for line in (getattr(out, "stdout", "") or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",", 1)]
        if len(parts) != 2:
            continue
        pid, name = parts
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        engine = next((e for e in engines if base.lower().startswith(e)), None)
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
    if mine is None or not holders:
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
