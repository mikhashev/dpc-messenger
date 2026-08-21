"""Who extracts a conversation's knowledge — and why it is not the text default.

An extraction prompt carries the conversation itself. Routing it to the globally
configured text provider means sending the transcript wherever that provider
lives, and today that is a paid API. So the route is a chain, and silence walks
the chain rather than falling to the global default:

    1. the alias chosen for extraction on the providers screen
    2. the provenance of the conversation — whoever has been answering in it
    3. the cold fallback, for a conversation nobody has answered in yet

Step 1 is a global role beside default / vision / voice / agent, not a field on
an agent. Extraction is not an agent's action: the tool was removed (ADR-009),
the triggers are a human's button and /endsession, and the result lands in the
user's own knowledge base. In an agent's chat the agent's own model is already
what step 2 resolves to, so a per-agent field said the same thing twice.

Step 2 is what makes this different from `sleep_provider_alias` and its two
siblings, which resolve `agent_config.get(...) or None` straight into
`default_provider`. That is honest for them: those tasks have no provenance.
Extraction has one.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

# Providers that answer from this machine. `openai_compatible` is deliberately
# absent: its base_url may name LM Studio next door or a service on the other
# side of the world, and this list decides what may see a whole conversation.
LOCAL_PROVIDER_TYPES = frozenset({"ollama", "llamacpp_server"})

# The role's key in providers.json, beside default_provider / vision_provider /
# voice_provider / agent_provider.
ROLE_KEY = "knowledge_provider"


class NoKnowledgeProvider(RuntimeError):
    """No step of the chain named a provider.

    Raised rather than resolved, because the one provider always available is
    the global text default — the leak the chain exists to close.
    """


def chosen_provider(alias: Optional[str], providers: Mapping[str, Any]) -> Optional[str]:
    """The alias chosen for extraction, when one is chosen and still exists.

    An alias that has been deleted or renamed away resolves to nothing rather
    than to an error: the chain simply moves on to the conversation's own
    provenance, which is what silence means anyway.
    """
    alias = (alias or "").strip()
    if not alias or alias not in providers:
        return None
    return alias


def first_local_provider(providers: Mapping[str, Any]) -> Optional[str]:
    """The first alias in the registry that answers from this machine."""
    for alias, provider in providers.items():
        config = getattr(provider, "config", None) or {}
        if config.get("type") in LOCAL_PROVIDER_TYPES:
            return alias
    return None


def cold_fallback(providers: Mapping[str, Any], configured: str = "") -> str:
    """What extracts a conversation with no provenance.

    A configured alias wins and is not second-guessed — naming a paid provider
    here is a choice, not an accident. Otherwise the first local provider, and
    if there is none, nothing: the caller refuses rather than reaching for the
    global default.
    """
    configured = (configured or "").strip()
    if configured:
        if configured not in providers:
            raise NoKnowledgeProvider(
                f"[knowledge] cold_fallback_provider names '{configured}', "
                f"which is not a configured provider"
            )
        return configured

    alias = first_local_provider(providers)
    if alias is None:
        raise NoKnowledgeProvider(
            "no local provider is configured, and knowledge extraction will not "
            "fall back to the global text provider: the prompt carries the whole "
            "conversation. Name one in [knowledge] cold_fallback_provider to "
            "choose deliberately."
        )
    return alias
