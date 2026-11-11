"""Whose engine answers an agent's calls, what kind of model it is, and who pays per token.

Read for the agent's runtime block. The route is resolved the way the LLM adapter
resolves it (per-agent compute_host first, then the global dpc_agent peer_id, then
the local alias chain), so the block describes the call the adapter will make.
Anything that cannot be resolved is printed as "unknown", never as a default.

The words are the node ledger's (ADR-041 D3): `route` and `served_by` are the
usage row's columns, and `provider_alias` is the row's `alias`. On a peer route
the payer follows D3's amendment of 2026-09-13, "the payer is the caller": the
tariff quoted in the peer's menu row decides it. That quote is the last
PROVIDERS_RESPONSE, so the budget is a quote, not a receipt; the ledger row
written at the call is the record.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from dpc_client_core.peer_cache import in_local_networks

# The same classes firewall.py uses for the serving lists.
LOCAL_TYPES = frozenset({"ollama", "llamacpp_server", "strata", "local_whisper"})
VENDOR_TYPES = frozenset({
    "openai_compatible", "anthropic", "zai", "deepseek", "neuraldeep", "gemini", "github_models",
    "gigachat",
})
OPENAI_OWN_HOSTS = ("api.openai.com",)  # as providers/openai_provider.py
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
REMOTE_PREFIX = "remote:"


def provider_kind(provider_type: Optional[str], base_url: Optional[str] = None) -> str:
    """'self_hosted' (a model on the serving node's own hardware), 'vendor' (a paid API) or 'unknown'.

    Not 'local': `route` already says local-or-peer about who answered, and a
    peer serving its own model would print as route peer, kind local.
    """
    if provider_type in LOCAL_TYPES:
        return "self_hosted"
    if provider_type == "openai_compatible":
        # A peer's menu row carries no base_url, and without it a server on
        # the peer's own card cannot be told from a vendor.
        if not base_url:
            return "unknown"
        host = (urlparse(base_url).hostname or "").lower()
        if host in _LOOPBACK_HOSTS:
            return "self_hosted"
    if provider_type in VENDOR_TYPES:
        return "vendor"
    return "unknown"


def _peer_row(peer_metadata: Optional[Dict[str, Any]], node_id: str, alias: Optional[str]) -> Optional[Dict[str, Any]]:
    if not alias:
        return None
    rows = ((peer_metadata or {}).get(node_id) or {}).get("providers") or []
    for row in rows:
        if isinstance(row, dict) and row.get("alias") == alias:
            return row
    return None


# service.MENU_TARIFF_UNIT, the unit a menu row's rates are stated in; a test
# keeps the two equal without importing the service here.
MENU_TARIFF_UNIT = "per_1m_tokens"


def _quote_state(tariff: Any) -> str:
    """'gift' (no tariff key), 'free' (a declared zero), 'paid', or 'unreadable'.

    The producer's rule is that a guest which does not know the unit must not
    price the row, so an unknown unit or unparseable rates are 'unreadable',
    never a gift: read as a gift, a paid call would look free to the agent.
    """
    if tariff is None:
        return "gift"
    if not isinstance(tariff, dict) or tariff.get("unit") != MENU_TARIFF_UNIT:
        return "unreadable"
    try:
        rate_in, rate_out = float(tariff["in"]), float(tariff["out"])
    except (KeyError, TypeError, ValueError):
        return "unreadable"
    if tariff.get("free") or (rate_in == 0 and rate_out == 0):
        return "free"
    return "paid" if rate_in > 0 or rate_out > 0 else "unreadable"


def _peer_payer(row: Optional[Dict[str, Any]], kind: str) -> str:
    """No tariff is the v1 gift and `free` a declared zero: both leave nothing for
    the caller to pay, so the payer is whoever bears the model's own cost."""
    if row is None:
        return "unknown"
    state = _quote_state(row.get("tariff"))
    if state == "paid":
        return "this_node"
    if state == "unreadable":
        return "unknown"
    return {"self_hosted": "nobody", "vendor": "peer"}.get(kind, "unknown")


def _peer_facts(alias: Optional[str], node_id: str, peer_metadata) -> Dict[str, Any]:
    row = _peer_row(peer_metadata, node_id, alias)
    # The host's own word first: it knows which serving list the alias is on,
    # where the type alone cannot tell an `openai_compatible` vendor from a
    # server on the host's card. An older host sends none; the type decides.
    stated = row.get("provider_kind") if row else None
    kind = stated if stated in ("self_hosted", "vendor") else provider_kind(row.get("type") if row else None)
    return {
        "provider_alias": alias or "unknown",
        "route": "peer",
        "served_by": node_id,
        "provider_kind": kind,
        "tokens_paid_by": _peer_payer(row, kind),
    }


def _resolve_local_alias(llm_manager: Any, provider_alias: Optional[str]) -> Optional[str]:
    providers = getattr(llm_manager, "providers", None) or {}
    for candidate in (provider_alias,
                      getattr(llm_manager, "agent_provider", None),
                      getattr(llm_manager, "default_provider", None)):
        if candidate and candidate in providers:
            return candidate
    return None


# Names a person gives a machine on their own network. A name outside this
# list, with a dot, is read as a public DNS name.
_LAN_SUFFIXES = ("local", "lan", "home", "internal", "home.arpa", "localdomain", "intranet", "corp")


def url_host_scope(url: Optional[str], own_addresses: Any = None) -> Optional[str]:
    """Display-only: where the host in a provider's URL is, when that is not the vendor's.

    'this_machine' for `localhost`, a loopback literal or an address this node owns
    (`own_addresses.contains`; no registry means "not mine"), 'lan' for an IP literal
    in a private network (peer_cache.in_local_networks: RFC 1918, ULA; link-local is
    not), 'unknown' for a name that is one label or ends in a LAN suffix, or a host of
    digits and dots that is no address (this node cannot tell what it is), None for
    anything else. Never feeds `provider_kind` or `tokens_paid_by`.
    """
    if not url:
        return None
    text = url if "://" in url else "//" + url
    try:
        host = (urlparse(text).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if not host:
        return None
    if host == "localhost" or host in _LOOPBACK_HOSTS:
        return "this_machine"
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        if host.replace(".", "").isdigit():
            return "unknown"
        if "." not in host or host.endswith(tuple("." + s for s in _LAN_SUFFIXES)):
            return "unknown"
        return None
    if addr.is_loopback:
        return "this_machine"
    try:
        if own_addresses is not None and own_addresses.contains(str(addr)):
            return "this_machine"
    except Exception:
        pass
    return "lan" if in_local_networks(addr) else None


def _unnamed_vendor_host(base_url: Optional[str]) -> Optional[str]:
    """Display-only: the host of an `openai_compatible` URL that names no vendor, else None.

    The type is generic and its URL is typed by the owner, so a public-looking host
    other than OpenAI's own (or none, the SDK default) is not known to be a vendor.
    """
    if not base_url:
        return None
    text = base_url if "://" in base_url else "//" + base_url
    try:
        host = (urlparse(text).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    return host if host and host not in OPENAI_OWN_HOSTS else None


def provider_row_facts(
    row: Dict[str, Any],
    llm_manager: Any,
    peer_metadata: Optional[Dict[str, Any]] = None,
    own_addresses: Any = None,
) -> Dict[str, Any]:
    """`route` and `provider_kind` of one row of the providers list the agent dialog shows.

    `provider_kind` is exactly what `provider_kind(type, base_url)` returns. A local
    row also carries `host_scope`, the value of `url_host_scope` verbatim, when it is
    not None; display only, it never changes the kind. A row of `openai_compatible`
    with no scope and a host that is not OpenAI's carries `base_url_host`, so the dialog can
    say it does not know who answers there.

    A peer's row (`is_remote`, `peer_id`) and a local `remote_peer` or `dpc_agent`
    alias are a peer route; anything else is local and its kind is read from the
    type and base_url, as `provider_facts_for` reads it.
    """
    alias = row.get("alias")
    scope = None
    unnamed_host = None
    if row.get("is_remote") and row.get("peer_id"):
        facts = _peer_facts(alias, row["peer_id"], peer_metadata)
    else:
        provider = (getattr(llm_manager, "providers", None) or {}).get(alias)
        config = getattr(provider, "config", None) or {}
        ptype = config.get("type") or row.get("type")
        if ptype == "remote_peer" and config.get("peer_id"):
            facts = _peer_facts(config.get("provider") or alias, config["peer_id"], peer_metadata)
        elif ptype == "dpc_agent" and getattr(provider, "peer_id", None):
            facts = _peer_facts(getattr(provider, "remote_provider", None), provider.peer_id, peer_metadata)
        else:
            facts = {"route": "local", "provider_kind": provider_kind(ptype, config.get("base_url"))}
            scope = url_host_scope(config.get("base_url") or config.get("host"), own_addresses)
            if scope is None and ptype == "openai_compatible":
                unnamed_host = _unnamed_vendor_host(config.get("base_url"))
    out = {"route": facts["route"], "provider_kind": facts["provider_kind"]}
    if scope is not None:
        out["host_scope"] = scope
    elif unnamed_host is not None:
        out["base_url_host"] = unnamed_host
    return out


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
