# ADR 002: LLM Provider Abstract Base Class

**Date:** 2026-03-28
**Status:** Accepted
**Branch:** refactor/grand

## Context

`llm_manager.py` is 3,074 lines containing 5 providers (Ollama, OpenAI, Anthropic, Z.AI/GLM,
LocalWhisper) as inline classes. Adding a new provider requires editing a 3,000-line file.
The VRAM conflict bug (switching Whisper models) was harder to find because all providers
are intermixed.

## Decision

Split `llm_manager.py` into:

```
dpc_client_core/
  llm_manager.py          ← LLMManager (registry + routing, ~300 lines target)
  providers/
    base.py               ← AbstractLLMProvider (ABC)
    ollama_provider.py
    openai_provider.py
    anthropic_provider.py
    zai_provider.py       ← GLM (Z.AI Anthropic-compatible endpoint)
    whisper_provider.py   ← LocalWhisperProvider
```

## Abstract Base Class

```python
class AbstractLLMProvider(ABC):
    @abstractmethod
    async def complete(self, messages: list, **kwargs) -> LLMResponse:
        """Generate completion from message history"""

    @abstractmethod
    async def stream_complete(self, messages: list, **kwargs) -> AsyncIterator[str]:
        """Stream completion tokens"""

    def get_state(self) -> dict:
        """Agent-readable provider state (runtime introspection)"""
        return {
            "provider_type": self.__class__.__name__,
            "model": getattr(self, "model", None),
            "available": self._is_available if hasattr(self, "_is_available") else None
        }
```

## Rationale

- Adding new provider = create one new file, not edit a 3,000-line file
- Each provider is independently testable (mock the ABC)
- `get_state()` on each provider enables runtime diagnostics
- Whisper VRAM conflict fix is cleanly localized to `whisper_provider.py`
