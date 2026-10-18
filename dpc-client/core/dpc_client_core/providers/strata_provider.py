# dpc_client_core/providers/strata_provider.py
"""
StrataProvider — provider type `strata` (ADR-044): a DPC-owned Strata launcher.

Strata accepts the dialect `LlamaServerProvider` already sends, so this class
changes only what is Strata's own: the child, the keys that decide a restart,
where the effort words come from, and vision, answered from the live launcher.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from ..managers.llama_server_supervisor import effort_dictionary_of
from ..managers.strata_supervisor import StrataSupervisor, strata_config_errors
from .llamacpp_server_provider import _ACTIVE_SUPERVISORS, LlamaServerProvider, card_holder

logger = logging.getLogger(__name__)

# The keys that change what the launcher would be started with; a reload that
# leaves them equal adopts the running launcher. An edit inside the base
# config file itself is not seen here and takes a restart by hand.
_STRATA_FLAG_KEYS = (
    "strata_install_dir", "strata_base_config", "conversation_cache_mib",
    "conversation_cache_slots", "max_context", "vision", "control_vector",
    "model_params", "extra_args",
    # Model-file overrides (ADR-044 point 7): each replaces a base config value.
    "native_gguf", "ple_gguf", "pack_dir", "mtp_dir", "expert_profile", "mmproj",
    "control_vector_file",
)


class StrataProvider(LlamaServerProvider):
    """Local Strata behind the OpenAI-compatible face, its launcher DPC's child."""

    RETRY_LABEL = "strata"
    SUPERVISOR_CLASS = StrataSupervisor
    # Strata reads no `reasoning_budget_message`, so none is sent unless an alias asks.
    DEFAULT_REASONING_BUDGET_MESSAGE = ""

    @classmethod
    def _check_config(cls, alias: str, config: Dict[str, Any]) -> None:
        """The alias's own keys, without the disk: a path checked here would drop
        the alias from the loaded set whenever a drive is not mounted. The save
        checks the paths too (`strata_config_errors(check_paths=True)`)."""
        errors = strata_config_errors(config)
        if errors:
            raise ValueError(f"strata provider '{alias}' " + "; ".join(errors))

    @classmethod
    def _flag_signature(cls, config: Dict[str, Any]) -> tuple:
        merged = StrataSupervisor.merge_config(config)
        return tuple(
            json.dumps(merged.get(k), sort_keys=True, default=str) for k in _STRATA_FLAG_KEYS
        )

    def _read_effort_dictionary(self, config: Dict[str, Any]):
        """The template's effort words, from `<tokenizer>/chat_template.jinja` of the base config.

        A failure leaves the fallback table, as for a GGUF without a template."""
        try:
            install = Path(str(config.get("strata_install_dir") or ""))
            base = Path(str(config.get("strata_base_config") or ""))
            if not base.is_absolute():
                base = install / base
            cfg = json.loads(base.read_text(encoding="utf-8-sig"))
            tokenizer = Path(str(cfg.get("tokenizer") or ""))
            if not tokenizer.is_absolute():
                tokenizer = Path(str(cfg.get("cwd") or install)) / tokenizer
            template = (tokenizer / "chat_template.jinja").read_text(encoding="utf-8")
        except (OSError, ValueError, TypeError, AttributeError) as e:
            logger.info(
                "strata '%s': chat template unreadable from the base config (%s) — "
                "effort words fall back to the table", self.alias, e,
            )
            return None
        return effort_dictionary_of(template)

    def supports_vision(self) -> bool:
        """`/props` `modalities.vision` of a running launcher; False when none runs.

        No fallback to a configured projector, unlike the base (ADR-044 point 8)."""
        sup = self.supervisor
        proc = getattr(sup, "_proc", None)
        if sup is None or proc is None or proc.returncode is not None or not sup.props:
            return False
        modal = sup.props.get("modalities") or {}
        return bool(isinstance(modal, dict) and modal.get("vision"))

    def _model_name(self) -> str:
        return self.model or "strata"

    def engine_failure(self) -> Optional[Dict[str, Any]]:
        return getattr(self.supervisor, "failure", None)

    def reset_engine_failure(self) -> Optional[Dict[str, Any]]:
        """Let the next call start the launcher again after an engine death."""
        return self.supervisor.reset_failure()

    def engine_status(self) -> Dict[str, Any]:
        sup = self.supervisor
        return {
            "engine_version": sup.engine_version,
            "tested_version": sup.config.get("tested_version"),
            "tested_version_date": sup.config.get("tested_version_date"),
            "start_config": str(sup.start_config_used) if sup.start_config_used else None,
            "failure": sup.failure,
            "vram_released": sup.vram_released,
            "last_stop_vram": dict(sup.last_stop_vram),
        }


def _strata_state(sup: StrataSupervisor) -> str:
    proc = sup._proc
    if sup.failure is not None:
        return "failed"
    if proc is not None and proc.returncode is None:
        if sup._draining:
            return "draining"
        return "running" if sup.props else "starting"
    return "stopped"


def metrics_summary(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """The part of the launcher's `/metrics` the providers UI shows (ADR-044 point 7)."""
    engine = metrics.get("engine") if isinstance(metrics.get("engine"), dict) else {}
    cache = metrics.get("conversation_cache") if isinstance(metrics.get("conversation_cache"), dict) else {}
    hw = metrics.get("hardware") if isinstance(metrics.get("hardware"), dict) else {}
    live = metrics.get("live") if isinstance(metrics.get("live"), dict) else {}
    total, used = hw.get("gpu_mem_total"), hw.get("gpu_mem_used")
    free_mib = None
    if isinstance(total, (int, float)) and isinstance(used, (int, float)) and total >= used:
        free_mib = int(total - used) >> 20
    return {
        "live_state": live.get("state"),
        "max_context": engine.get("max_context"),
        "images": engine.get("images"),
        "vram_free_mib": free_mib,
        "conversation_cache": {
            k: cache.get(k) for k in (
                "enabled", "budget_mib", "slots", "parked", "bytes", "parks", "restores", "evictions",
            )
        },
    }


async def strata_status(sup: StrataSupervisor, metrics_timeout_s: float = 3.0) -> Dict[str, Any]:
    """One Strata alias as the UI shows it. Reads `/metrics` of a running
    launcher only; never starts one."""
    state = _strata_state(sup)
    tested = sup.config.get("tested_version")
    version = sup.engine_version
    failure = dict(sup.failure) if sup.failure else None
    out: Dict[str, Any] = {
        "alias": sup.alias,
        "engine": sup.ENGINE,
        "state": state,
        "port": sup.port if state in ("running", "draining", "starting") else None,
        "engine_version": version,
        "tested_version": tested,
        "tested_version_date": sup.config.get("tested_version_date"),
        "version_matches": None if not (version and tested) else str(tested).strip() == version,
        "failure": failure,
        "vram_released": sup.vram_released,
        "last_stop_vram": dict(sup.last_stop_vram),
        "start_config": str(sup.start_config_used) if sup.start_config_used else None,
        "metrics": None,
        "metrics_error": None,
    }
    if state in ("running", "draining"):
        try:
            metrics = await asyncio.wait_for(sup._get("/metrics"), metrics_timeout_s)
            out["metrics"] = metrics_summary(metrics if isinstance(metrics, dict) else {})
        except Exception as e:  # a status read must not fail the panel
            out["metrics_error"] = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    return out


async def local_engine_status(metrics_timeout_s: float = 3.0) -> Dict[str, Any]:
    """Who holds the card, and every Strata alias's supervisor state. Read-only."""
    strata = [
        await strata_status(sup, metrics_timeout_s)
        for sup in list(_ACTIVE_SUPERVISORS.values())
        if isinstance(sup, StrataSupervisor)
    ]
    return {"card_holder": card_holder(), "strata": strata}


def strata_supervisor_of(alias: str) -> Optional[StrataSupervisor]:
    sup = _ACTIVE_SUPERVISORS.get(alias)
    return sup if isinstance(sup, StrataSupervisor) else None
