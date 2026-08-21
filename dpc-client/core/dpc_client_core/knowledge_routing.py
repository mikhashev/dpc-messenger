"""Who extracts a conversation's knowledge — and why it is not the text default.

An extraction prompt carries the conversation itself. Routing it to the globally
configured text provider means sending the transcript wherever that provider
lives, and today that is a paid API. So the route is a chain, and silence walks
the chain rather than falling to the global default:

    1. the alias the agent names for its own extraction
    2. the provenance of the conversation — whoever has been answering in it
    3. the cold fallback, for a conversation nobody has answered in yet

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

# The per-agent key, alongside provider_alias / sleep_provider_alias /
# snapshot_summarize_provider / compaction_provider in an agent's config.json.
AGENT_CONFIG_KEY = "knowledge_provider"


class NoKnowledgeProvider(RuntimeError):
    """No step of the chain named a provider.

    Raised rather than resolved, because the one provider always available is
    the global text default — the leak the chain exists to close.
    """


def explicit_provider(conversation_id: str, agent_config: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The alias an agent names for extracting its own conversations.

    Only an agent chat has this step: there the agent *is* the conversation.
    A group has several agents and the monitor is per conversation, so «whose
    field» has no answer and the chain moves on.
    """
    if not conversation_id.startswith("agent_") or not agent_config:
        return None
    alias = (agent_config.get(AGENT_CONFIG_KEY) or "").strip()
    return alias or None


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
