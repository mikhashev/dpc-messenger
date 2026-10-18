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
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ..managers.llama_server_supervisor import effort_dictionary_of
from ..managers.strata_supervisor import StrataSupervisor, peer_being_served, strata_config_errors
from .llamacpp_server_provider import _ACTIVE_SUPERVISORS, LlamaServerProvider, card_holder

logger = logging.getLogger(__name__)

# How long one /metrics read answers the peer policy, so a burst of peer
# requests reads the launcher once rather than once each.
PEER_METRICS_TTL_S = 3.0
# How long after the owner's call ends its conversation counts as live: the
# engine parks at the end of the turn and the launcher learns it from the
# engine's log, so a /metrics read just after the answer can miss the park.
OWNER_PARK_GRACE_S = 3.0

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

    @classmethod
    def _is_retryable(cls, error: Exception) -> bool:
        """A call served to a peer is never retried here: the engine is the
        owner's, and a peer told 503 comes back on its own (ADR-044 point 9)."""
        if peer_being_served() is not None:
            return False
        return super()._is_retryable(error)

    async def peer_refusal(self) -> Optional[str]:
        """Why a peer may not be served on this alias now, or None (ADR-044 point 9).

        Starting, stopped, failed or draining: refused, and a peer never starts
        the launcher. Running: refused while our own conversations may hold the
        conversation cache. Strata keys the cache by token prefix and reports
        it only in aggregate (`/metrics` `conversation_cache` parked / bytes,
        no owner per entry), so ownership is DPC's own count: the owner's calls
        on this launcher since it started. The rule —
          - an owner call running now, or ended under OWNER_PARK_GRACE_S ago: refused;
          - no owner call since the start: served (whatever is parked is a peer's);
          - otherwise served only when the cache is off, or holds nothing
            (parked == 0 and bytes == 0); refused when /metrics cannot be read.
        So a peer is never served while a conversation of ours can be parked,
        and a peer's park can therefore never evict one (Strata evicts oldest
        first when its budget or slots run out). It is conservative: once the
        owner has used the launcher, a peer waits until nothing is parked or
        the launcher restarts.

        A None answer records the owner's call count it was given on; the
        peer's slot refuses if that count has moved by the time the call
        reaches the engine (`StrataSupervisor.peer_slot_refusal`)."""
        sup = self.supervisor
        counted = sup.owner_calls
        refusal = await self._peer_refusal_now()
        sup.peer_cleared_at_owner_calls = counted if refusal is None else None
        return refusal

    async def _peer_refusal_now(self) -> Optional[str]:
        sup = self.supervisor
        state = _strata_state(sup)
        if state == "starting":
            return f"'{self.alias}' is loading (the Strata launcher is starting); try again shortly"
        if state == "draining":
            return f"'{self.alias}' is stopping (the Strata launcher is draining)"
        if state in ("stopped", "failed"):
            return (
                f"'{self.alias}' is not running; this node's owner starts it, a peer's request does not"
            )
        if sup.owner_in_flight > 0:
            return f"'{self.alias}' is serving this node's owner now; its conversation cache is not shared meanwhile"
        if sup.owner_calls == 0:
            return None
        now = time.monotonic()
        if sup.owner_last_end is not None and now - sup.owner_last_end < OWNER_PARK_GRACE_S:
            return f"'{self.alias}' has just served this node's owner; its conversation cache is not shared meanwhile"
        cache = await self._peer_cache_view(now)
        if cache is None:
            return f"'{self.alias}': the conversation cache cannot be read, so it is not shared"
        if cache.get("enabled") is False:
            return None
        if cache.get("parked") is None and cache.get("bytes") is None:
            return f"'{self.alias}': the launcher reports no conversation cache state, so it is not shared"
        parked, held = cache.get("parked") or 0, cache.get("bytes") or 0
        if parked > 0 or held > 0:
            budget = cache.get("budget_mib")
            return (
                f"'{self.alias}': this node's own conversations hold the conversation cache "
                f"({parked} parked, {int(held) >> 20} MiB"
                f"{f' of {budget} MiB' if budget is not None else ''})"
            )
        return None

    async def _peer_cache_view(self, now: float) -> Optional[Dict[str, Any]]:
        """`conversation_cache` of a recent /metrics read, or None when unreadable.

        A read older than the owner's last call is not reused."""
        sup = self.supervisor
        cached = sup.peer_metrics_read
        if cached is not None:
            at, body = cached
            fresh = now - at < PEER_METRICS_TTL_S
            after_owner = sup.owner_last_end is None or at >= sup.owner_last_end
            if fresh and after_owner:
                return body
        try:
            metrics = await asyncio.wait_for(sup._get("/metrics"), PEER_METRICS_TTL_S)
            body = metrics_summary(metrics if isinstance(metrics, dict) else {})["conversation_cache"]
        except Exception as e:
            logger.info("strata '%s': /metrics unreadable for the peer policy: %s", self.alias, e)
            body = None
        sup.peer_metrics_read = (time.monotonic(), body) if body is not None else None
        return body

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
