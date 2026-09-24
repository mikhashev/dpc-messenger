# dpc_client_core/model_download_service.py
"""ModelDownloadService: the generic model-download consent flow (2026-09-24).
One event family for any model; `required` fires at most once per (session,
model); only "don't remind me" persists, to config.ini [model_downloads].
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

from .providers.model_sizes import (
    hf_cache_path,
    is_model_cached,
    known_model_names,
    model_size_bytes,
    model_size_measured_on,
)

logger = logging.getLogger(__name__)

# Re-exported for callers that used to import these from here.
__all__ = ["ModelDownloadService", "hf_cache_path", "is_model_cached"]


# A model this service already knows how to ask for again after a restart, when
# the caller that first asked (agent.py, service.py's transcribe_audio) is not
# around to ask a second time — reset_decline below builds the re-ask payload
# from here rather than from `_last_required_payload`, which is process memory
# and is empty on a fresh process (owner-accepted finding B, 2026-09-24). The
# Whisper row carries no provider_alias: it is filled in per lookup, because the
# alias is a fact about the configured provider, not about the model name.
_KNOWN_CONSENT_MODELS: Dict[str, Dict[str, str]] = {
    "BAAI/bge-m3": {
        "purpose": "Agent memory search (Active Recall, memory_search)",
        "consequence_if_declined": "Memory search stays unavailable until downloaded.",
        "on_decline": "degrade",
    },
    "openai/whisper-large-v3-turbo": {
        "purpose": "Voice transcription",
        "consequence_if_declined": "Voice messages will not be transcribed.",
        "on_decline": "degrade",
    },
}


class ModelDownloadService:
    def __init__(self, llm_manager: Any, settings: Any, local_api: Any, firewall: Any = None):
        self.llm_manager = llm_manager
        self.settings = settings
        self.local_api = local_api
        # Optional: only used to read agent_profiles.*.memory.embedding_model for
        # the download allow-list (finding A). None in tests that build this
        # service without a firewall — the allow-list then falls back to the
        # measured-size table, configured Whisper models, and the default
        # embedding model, which is every model a firewall-less caller can name.
        self.firewall = firewall
        self._emitted_this_session: set = set()
        self._status: Dict[str, Dict[str, Any]] = {}
        self._last_required_payload: Dict[str, Dict[str, Any]] = {}

    def _allowed_model_names(self) -> set:
        """Every model name `download_model` may fetch: the measured-size
        table, the Whisper model of every configured `local_whisper` provider,
        the default embedding model, every agent profile's configured
        embedding model, and — the backstop for a name this session already
        asked the user about by some other path — everything `required` has
        already been broadcast for. Anything else is refused before any
        network call (owner-accepted finding A, 2026-09-24): `download_model`
        used to fall through to a SentenceTransformer fetch of any HF repo id
        a caller named over the local API.
        """
        from .dpc_agent.memory import DEFAULT_EMBEDDING_MODEL

        allowed = set(known_model_names())
        allowed.add(DEFAULT_EMBEDDING_MODEL)
        allowed |= self._emitted_this_session

        if self.llm_manager is not None:
            for p in self.llm_manager.providers.values():
                if p.config.get("type") == "local_whisper":
                    model = p.config.get("model")
                    if model:
                        allowed.add(model)

        if self.firewall is not None:
            try:
                from .dpc_agent.memory_config import get_memory_config

                for profile_name in self.firewall.list_agent_profiles():
                    profile = self.firewall.get_agent_profile_settings(profile_name) or {}
                    mem_cfg = get_memory_config(profile)
                    if mem_cfg.embedding_model:
                        allowed.add(mem_cfg.embedding_model)
            except Exception:
                logger.debug("Could not enumerate agent profiles for the download allow-list", exc_info=True)

        return allowed

    def get_status(self, model_name: str) -> Dict[str, Any]:
        return dict(self._status.get(model_name) or {"state": "missing"})

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        return {k: dict(v) for k, v in self._status.items()}

    def _set_status(self, model_name: str, state: str, error: Optional[str] = None) -> None:
        entry: Dict[str, Any] = {"state": state}
        if error is not None:
            entry["error"] = error
        self._status[model_name] = entry

    async def maybe_emit_required(
        self,
        model_name: str,
        *,
        revision: Optional[str] = None,
        purpose: str,
        consequence_if_declined: str,
        on_decline: str = "degrade",
        provider_alias: Optional[str] = None,
    ) -> bool:
        size_bytes, size_source = model_size_bytes(model_name, revision)
        payload = {
            "model_name": model_name,
            "revision": revision,
            "purpose": purpose,
            "consequence_if_declined": consequence_if_declined,
            "on_decline": on_decline,
            "size_bytes": size_bytes,
            "size_source": size_source,
            # ISO date the size row was measured, or null — a UI that does not
            # read this field is unaffected, it only gains one (finding F).
            "size_measured_on": model_size_measured_on(model_name, revision),
            "cache_path": hf_cache_path(),
            "provider_alias": provider_alias,
        }
        self._last_required_payload[model_name] = payload

        if self.settings.get_model_download_declined(model_name):
            self._set_status(model_name, "declined")
            return False
        if model_name in self._emitted_this_session:
            return False

        self._emitted_this_session.add(model_name)
        self._set_status(model_name, "missing")
        try:
            await self.local_api.broadcast_event("model_download_required", payload)
        except Exception:
            logger.debug("broadcast_event(model_download_required) failed", exc_info=True)
        return True

    def decline(self, model_name: str, remember: bool) -> None:
        if remember:
            self.settings.set_model_download_declined(model_name)
        self._set_status(model_name, "declined")

    def _provider_alias_for(self, model_name: str) -> Optional[str]:
        """The alias of the configured `local_whisper` provider serving
        `model_name`, or None — mirrors the lookup `download_model` does."""
        if self.llm_manager is None:
            return None
        for alias, p in self.llm_manager.providers.items():
            if p.config.get("type") == "local_whisper" and p.config.get("model") == model_name:
                return alias
        return None

    def _known_payload(self, model_name: str) -> Optional[Dict[str, Any]]:
        """The re-ask payload for `model_name` from the registry of models this
        service knows how to ask about, rather than from `_last_required_payload`
        (which is empty on a fresh process — finding B, 2026-09-24: a decline
        persisted to config.ini survives a restart, but the in-memory payload it
        was built from does not, so `reset_decline` used to return False and emit
        nothing for a model nobody had asked about yet this session)."""
        entry = _KNOWN_CONSENT_MODELS.get(model_name)
        if entry is None:
            return None
        return {
            "revision": None,
            "purpose": entry["purpose"],
            "consequence_if_declined": entry["consequence_if_declined"],
            "on_decline": entry.get("on_decline", "degrade"),
            "provider_alias": self._provider_alias_for(model_name),
        }

    async def reset_decline(self, model_name: str) -> bool:
        self.settings.clear_model_download_declined(model_name)
        self._emitted_this_session.discard(model_name)
        if is_model_cached(model_name):
            self._set_status(model_name, "cached")
            return False
        self._set_status(model_name, "missing")
        payload = self._last_required_payload.get(model_name) or self._known_payload(model_name)
        if payload is None:
            return False
        return await self.maybe_emit_required(
            model_name,
            revision=payload.get("revision"),
            purpose=payload.get("purpose", ""),
            consequence_if_declined=payload.get("consequence_if_declined", ""),
            on_decline=payload.get("on_decline", "degrade"),
            provider_alias=payload.get("provider_alias"),
        )

    async def emit_started(self, model_name: str) -> None:
        self._set_status(model_name, "downloading")
        try:
            await self.local_api.broadcast_event("model_download_started", {"model_name": model_name})
        except Exception:
            logger.debug("broadcast_event(model_download_started) failed", exc_info=True)

    async def emit_completed(self, model_name: str) -> None:
        self._set_status(model_name, "cached")
        try:
            await self.local_api.broadcast_event("model_download_completed", {"model_name": model_name})
        except Exception:
            logger.debug("broadcast_event(model_download_completed) failed", exc_info=True)

    async def emit_failed(self, model_name: str, error: str) -> None:
        self._set_status(model_name, "failed", error=error)
        try:
            await self.local_api.broadcast_event(
                "model_download_failed", {"model_name": model_name, "error": error}
            )
        except Exception:
            logger.debug("broadcast_event(model_download_failed) failed", exc_info=True)

    async def download_embedding_model(self, model_name: Optional[str] = None) -> Dict[str, Any]:
        """Mirrors voice_service.download_whisper_model: download in a thread.
        The live provider keeps its instance and loads on its next call — a
        reset would leave agents holding the old one and load the model twice."""
        from .dpc_agent.memory import DEFAULT_EMBEDDING_MODEL

        model_name = model_name or DEFAULT_EMBEDDING_MODEL
        await self.emit_started(model_name)

        def _download():
            os.environ.setdefault("DISABLE_SAFETENSORS_CONVERSION", "true")
            from sentence_transformers import SentenceTransformer
            # On the CPU and dropped at once: this call is for the files, and
            # the same loader fetches exactly the set the size table measured.
            SentenceTransformer(model_name, device="cpu", local_files_only=False)

        try:
            await asyncio.to_thread(_download)
        except Exception as e:
            logger.error("Embedding model download failed: %s", e, exc_info=True)
            await self.emit_failed(model_name, str(e))
            return {"status": "error", "error": str(e)}

        await self.emit_completed(model_name)
        return {"status": "success", "model_name": model_name}

    async def download_model(self, model_name: str, provider_alias: Optional[str] = None) -> Dict[str, Any]:
        """Dispatch: a named Whisper provider downloads through itself,
        anything else goes through the embedding executor.

        Refuses first, before any network call, for a name that is not on the
        allow-list (finding A, 2026-09-24): this method used to fall through to
        `download_embedding_model` for any name at all, which made it a
        SentenceTransformer fetch of any HF repo id a caller could send over
        the local API.
        """
        if model_name not in self._allowed_model_names():
            logger.warning("download_model refused: %r is not an allowed model", model_name)
            return {"status": "error", "error": f"model '{model_name}' is not one this app downloads"}

        target_provider = None
        if provider_alias and self.llm_manager and provider_alias in self.llm_manager.providers:
            target_provider = self.llm_manager.providers[provider_alias]
        elif self.llm_manager:
            for p in self.llm_manager.providers.values():
                if p.config.get("type") == "local_whisper" and p.config.get("model") == model_name:
                    target_provider = p
                    break

        if target_provider is not None and target_provider.config.get("type") == "local_whisper":
            if not hasattr(target_provider, "download_model_async"):
                return {"status": "error", "error": f"Provider '{target_provider.alias}' doesn't support model download"}
            await self.emit_started(model_name)
            result = await target_provider.download_model_async()
            if result.get("success"):
                await self.emit_completed(model_name)
                return {"status": "success", "model_name": model_name}
            await self.emit_failed(model_name, result.get("message", "download failed"))
            return {"status": "error", "error": result.get("message")}

        return await self.download_embedding_model(model_name)
