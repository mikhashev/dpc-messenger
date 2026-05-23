"""DPC Agent — Web Auth introspection tools (ADR-028 T7).

Single tool: `list_auth_domains` returns the domains this agent is
authorized to authenticate to via `browse_page(use_auth=...)`, plus
current cookie status (logged in or not, expiry).

Kept in a dedicated module rather than `browser.py` because:
  - depends on `web_auth.py` (DPAPI/keyring) which `browser.py` doesn't
  - keeps `browser.py` focused on the browse path
  - future auth-introspection tools (`revoke_auth_domain`, etc.) go here

Per-agent scope: returns only the calling agent's whitelist (resolved
via `ctx.agent_root.name`). The agent cannot see other agents' lists
or read the firewall config directly.
"""
from __future__ import annotations

import logging
from typing import List

from .registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)


async def list_auth_domains(ctx: ToolContext) -> str:
    """List the agent's authorized auth domains + current cookie status.

    Args:
        ctx: tool context (agent_root used to derive agent_id;
            dpc_service.firewall used for whitelist lookup;
            web_auth used for status read).

    Returns:
        Plain-text summary, one line per domain, or a helpful message
        when no domains are configured for this agent.
    """
    # See browser.browse_page docstring for the agent_root.name contract.
    agent_id = ctx.agent_root.name

    firewall = None
    dpc_service = getattr(ctx, "dpc_service", None)
    if dpc_service is not None:
        firewall = getattr(dpc_service, "firewall", None)

    if firewall is None:
        return (
            "⚠️ Firewall not available — cannot read web_auth.allowed_domains. "
            "This is a configuration error in DPC core."
        )

    allowed = firewall.get_agent_web_auth_domains(agent_id)
    if not allowed:
        return (
            "No web auth domains configured for this agent. To enable "
            f"authenticated browsing, add domains to privacy_rules.json → "
            f"agent_profiles.{agent_id}.web_auth.allowed_domains, then "
            f"log in via the web-auth UI."
        )

    # Lazy import so the tool module stays importable when web_auth's
    # crypto deps aren't installed (test contexts).
    from dpc_client_core import web_auth

    lines = ["Authorized auth domains for this agent:"]
    for domain in allowed:
        status = web_auth.get_auth_status(agent_id, domain)
        if status.get("has_cookies"):
            expires = status.get("expires")
            if expires is None:
                tail = "session-only cookies"
            else:
                tail = f"cookies expire at unix={expires}"
            lines.append(f"  - {domain}: authenticated, {tail}")
        else:
            lines.append(f"  - {domain}: not logged in (re-login required)")
    return "\n".join(lines)


def get_tools() -> List[ToolEntry]:
    """Export web-auth introspection tools for the registry."""
    return [
        ToolEntry(
            name="list_auth_domains",
            schema={
                "name": "list_auth_domains",
                "description": (
                    "List web domains this agent is authorized to authenticate to "
                    "via browse_page(use_auth=...) and the current cookie status "
                    "for each. Returns only the calling agent's whitelist. Use "
                    "this before calling browse_page with use_auth to discover "
                    "available domains and check whether re-login is needed."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            handler=list_auth_domains,
            timeout_sec=5,
        ),
    ]
