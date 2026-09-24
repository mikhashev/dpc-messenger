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

from .providers.model_sizes import hf_cache_path, is_model_cached, model_size_bytes

logger = logging.getLogger(__name__)

# Re-exported for callers that used to import these from here.
__all__ = ["ModelDownloadService", "hf_cache_path", "is_model_cached"]


class ModelDownloadService:
    def __init__(self, llm_manager: Any, settings: Any, local_api: Any):
        self.llm_manager = llm_manager
        self.settings = settings
        self.local_api = local_api
        self._emitted_this_session: set = set()
        self._status: Dict[str, Dict[str, Any]] = {}
        self._last_required_payload: Dict[str, Dict[str, Any]] = {}

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

    async def reset_decline(self, model_name: str) -> bool:
        self.settings.clear_model_download_declined(model_name)
        self._emitted_this_session.discard(model_name)
        if is_model_cached(model_name):
            self._set_status(model_name, "cached")
            return False
        self._set_status(model_name, "missing")
        payload = self._last_required_payload.get(model_name)
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
        anything else goes through the embedding executor."""
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
