"""Whose engine answers an agent's calls, what kind of model it is, and who pays per token.

Read for the agent's runtime block. The route is resolved the way the LLM adapter
resolves it (per-agent compute_host first, then the global dpc_agent peer_id, then
the local alias chain), so the block describes the call the adapter will make.
Anything that cannot be resolved is printed as "unknown", never as a default.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import urlparse

# The same classes firewall.py uses for the serving lists.
LOCAL_TYPES = frozenset({"ollama", "llamacpp_server", "local_whisper"})
VENDOR_TYPES = frozenset({
    "openai_compatible", "anthropic", "zai", "deepseek", "gemini", "github_models", "gigachat",
})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
REMOTE_PREFIX = "remote:"


def provider_kind(provider_type: Optional[str], base_url: Optional[str] = None) -> str:
    """'self_hosted' (a model on the serving node's own hardware), 'vendor' (a paid API) or 'unknown'.

    Not 'local': `route` already says local-or-peer about who answered, and a
    peer serving its own model would print as route peer, kind local.
    """
    if provider_type in LOCAL_TYPES:
        return "self_hosted"
    if provider_type == "openai_compatible" and base_url:
        host = (urlparse(base_url).hostname or "").lower()
        if host in _LOOPBACK_HOSTS:
            return "self_hosted"
    if provider_type in VENDOR_TYPES:
        return "vendor"
    return "unknown"


def _peer_row_type(peer_metadata: Optional[Dict[str, Any]], node_id: str, alias: Optional[str]) -> Optional[str]:
    rows = ((peer_metadata or {}).get(node_id) or {}).get("providers") or []
    for row in rows:
        if isinstance(row, dict) and row.get("alias") == alias:
            return row.get("type")
    return None


def _peer_facts(alias: Optional[str], node_id: str, peer_metadata) -> Dict[str, Any]:
    kind = provider_kind(_peer_row_type(peer_metadata, node_id, alias))
    return {
        "provider_alias": alias or "unknown",
        "route": "peer",
        "served_by": node_id,
        "provider_kind": kind,
        "tokens_paid_by": "peer",
    }


def _resolve_local_alias(llm_manager: Any, provider_alias: Optional[str]) -> Optional[str]:
    providers = getattr(llm_manager, "providers", None) or {}
    for candidate in (provider_alias,
                      getattr(llm_manager, "agent_provider", None),
                      getattr(llm_manager, "default_provider", None)):
        if candidate and candidate in providers:
            return candidate
    return None


def provider_facts_for(
    llm_manager: Any,
    provider_alias: Optional[str],
    compute_host: str = "",
    peer_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The three facts for the runtime block's budget, plus the alias they are about."""
    providers = getattr(llm_manager, "providers", None) or {}
    if compute_host:
        return _peer_facts(provider_alias, compute_host, peer_metadata)
    if provider_alias and provider_alias.startswith(REMOTE_PREFIX):
        parts = provider_alias.split(":", 2)
        if len(parts) == 3 and parts[1]:
            return _peer_facts(parts[2], parts[1], peer_metadata)
    global_peer = getattr(providers.get("dpc_agent"), "peer_id", None)
    if global_peer:
        remote = getattr(providers.get("dpc_agent"), "remote_provider", None)
        return _peer_facts(remote, global_peer, peer_metadata)

    alias = _resolve_local_alias(llm_manager, provider_alias)
    if alias is None:
        return {"provider_alias": provider_alias or "unknown", "route": "unknown",
                "provider_kind": "unknown", "tokens_paid_by": "unknown"}
    config = getattr(providers[alias], "config", None) or {}
    if config.get("type") == "remote_peer" and config.get("peer_id"):
        return _peer_facts(config.get("provider") or alias, config["peer_id"], peer_metadata)
    kind = provider_kind(config.get("type"), config.get("base_url"))
    paid = {"self_hosted": "nobody", "vendor": "this_node"}.get(kind, "unknown")
    return {"provider_alias": alias, "route": "local", "provider_kind": kind, "tokens_paid_by": paid}
