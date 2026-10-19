# dpc_client_core/providers/strata_provider.py
"""
StrataProvider — provider type `strata` (ADR-044): a DPC-owned Strata launcher.

Strata accepts the dialect `LlamaServerProvider` already sends, so this class
changes only what is Strata's own: the child, the keys that decide a restart,
where the effort words come from, and vision, answered from the live launcher
or, while none runs, from the start it would make.
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

from ..managers.commit_memory import read_free_commit
from ..managers.llama_server_supervisor import effort_dictionary_of
from ..managers.strata_supervisor import (
    StrataSupervisor, peer_being_served, reasoning_effort_error, strata_chat_template,
    strata_config_errors,
)
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
    "conversation_cache_slots", "max_context", "resident_budget_gib", "vram_reserve_mib",
    "vision", "control_vector",
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

    def __init__(self, alias: str, config: Dict[str, Any]):
        # Whether the call that went out with no effort word has been reported.
        self._no_effort_word_warned = False
        # Whether an owner's word the template refuses has been reported.
        self._dropped_word_warned = False
        super().__init__(alias, config)
        # The save refuses such a word (`strata_config_errors`); an alias saved
        # before that check still loads, and says so here.
        word_error = reasoning_effort_error(config)
        if word_error:
            logger.warning("strata '%s': %s", alias, word_error)
        # The start smoke's thinking call runs what a tools call of this alias
        # sends (ADR-044 point 5). An adopted supervisor takes the newest alias's.
        self.supervisor.smoke_effort = self._tools_call_template_kwargs

    def _tools_call_template_kwargs(self) -> Optional[Dict[str, Any]]:
        """`chat_template_kwargs` of an agent-round tools call with no per-call word."""
        return self._build_extra_body(effective_max_tokens=self.max_tokens).get("chat_template_kwargs")

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
            template = strata_chat_template(config)
        except (OSError, ValueError, TypeError, AttributeError) as e:
            logger.info(
                "strata '%s': chat template unreadable from the base config (%s) — "
                "effort words fall back to the table", self.alias, e,
            )
            return None
        return effort_dictionary_of(template)

    def _build_extra_body(
        self,
        reasoning_effort: Optional[str] = None,
        reasoning_budget_tokens: Optional[int] = None,
        effective_max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """The base dialect with an effort word always on the wire.

        Strata fills an effort-less request from its own shared Chat settings
        or config (`Service.with_shared`), which DPC does not own; so a call
        that would send no word sends the template's default, the rung
        `_served_effort` records for it."""
        body = super()._build_extra_body(
            reasoning_effort, reasoning_budget_tokens, effective_max_tokens=effective_max_tokens,
        )
        if "chat_template_kwargs" not in body:
            word = self._template_default
            if word not in self._template_efforts:
                word = "xhigh" if "xhigh" in self._template_efforts else None
            owner = self._reasoning_effort_raw if reasoning_effort is None else reasoning_effort
            owner = str(owner or "").strip().lower()
            if owner and not self._dropped_word_warned:
                # The owner named a word and the template refused it; what goes
                # out instead is a word nobody chose (Ark's and Zcode's review).
                self._dropped_word_warned = True
                logger.warning(
                    "strata '%s': effort '%s' is not a word the chat template accepts "
                    "(%s, from the %s) — sending %s in its place",
                    self.alias, owner, "/".join(self._template_efforts),
                    self._template_efforts_source, word or "no word",
                )
            if word:
                body["chat_template_kwargs"] = {"reasoning_effort": word}
            elif not self._no_effort_word_warned:
                # The hole this method closes stays open here: no template
                # default among the words, and no `xhigh` to fall back on.
                self._no_effort_word_warned = True
                logger.warning(
                    "strata '%s': no effort word to send — the template's default (%s) is not "
                    "among its words (%s, from the %s) and neither is xhigh; Strata fills "
                    "these calls from its own shared settings, and the usage row cannot name the rung",
                    self.alias, self._template_default or "unset",
                    "/".join(self._template_efforts), self._template_efforts_source,
                )
        return body

    def supports_vision(self) -> bool:
        """`/props` `modalities.vision` of a running launcher; otherwise what the
        start config would say (ADR-044 point 8).

        A stopped launcher answers from the start it would make, so an image
        routed here starts it on demand, as the base's mmproj answer does; the
        decision is `build_start_config`'s own (`start_reads_images`)."""
        sup = self.supervisor
        if sup is None:
            return False
        proc = getattr(sup, "_proc", None)
        if proc is not None and proc.returncode is None and sup.props:
            modal = sup.props.get("modalities") or {}
            return bool(isinstance(modal, dict) and modal.get("vision"))
        return sup.start_reads_images()

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
    """Who holds the card, and every Strata alias's supervisor state. Read-only.

    `free_commit` is the host's GlobalMemoryStatusEx reading (commit_memory),
    None off Windows: the conversation cache's hint names it (ADR-044)."""
    strata = [
        await strata_status(sup, metrics_timeout_s)
        for sup in list(_ACTIVE_SUPERVISORS.values())
        if isinstance(sup, StrataSupervisor)
    ]
    return {"card_holder": card_holder(), "strata": strata, "free_commit": read_free_commit()}


def strata_supervisor_of(alias: str) -> Optional[StrataSupervisor]:
    sup = _ACTIVE_SUPERVISORS.get(alias)
    return sup if isinstance(sup, StrataSupervisor) else None
