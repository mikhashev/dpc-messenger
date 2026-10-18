# dpc_client_core/providers/strata_provider.py
"""
StrataProvider — provider type `strata` (ADR-044): a DPC-owned Strata launcher.

Strata accepts the dialect `LlamaServerProvider` already sends, so this class
changes only what is Strata's own: the child, the keys that decide a restart,
where the effort words come from, and vision, answered from the live launcher.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from ..managers.llama_server_supervisor import effort_dictionary_of
from ..managers.strata_supervisor import StrataSupervisor
from .llamacpp_server_provider import LlamaServerProvider

logger = logging.getLogger(__name__)

# The keys that change what the launcher would be started with; a reload that
# leaves them equal adopts the running launcher. An edit inside the base
# config file itself is not seen here and takes a restart by hand.
_STRATA_FLAG_KEYS = (
    "strata_install_dir", "strata_base_config", "conversation_cache_mib",
    "conversation_cache_slots", "max_context", "vision", "control_vector",
    "model_params", "extra_args",
)


class StrataProvider(LlamaServerProvider):
    """Local Strata behind the OpenAI-compatible face, its launcher DPC's child."""

    RETRY_LABEL = "strata"
    SUPERVISOR_CLASS = StrataSupervisor
    # Strata reads no `reasoning_budget_message`, so none is sent unless an alias asks.
    DEFAULT_REASONING_BUDGET_MESSAGE = ""

    @classmethod
    def _check_config(cls, alias: str, config: Dict[str, Any]) -> None:
        missing = [k for k in ("strata_install_dir", "strata_base_config") if not config.get(k)]
        if missing:
            raise ValueError(
                f"strata provider '{alias}' needs {' and '.join(missing)}: the Strata "
                "root (serve/server.py, .venv) and an existing Strata run config to "
                "start from"
            )

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
