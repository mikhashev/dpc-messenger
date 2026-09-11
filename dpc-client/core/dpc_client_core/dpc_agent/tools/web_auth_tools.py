"""DPC Agent — Web Auth introspection tools (ADR-028 T7).

Single tool: `list_auth_domains` returns the domains this agent is
authorized to authenticate to via `browse_page(use_auth=...)`, plus
current cookie status (logged in or not, expiry).

Kept in a dedicated module rather than `browser.py` because:
  - depends on `web_auth.py` (DPAPI/keyring) which `browser.py` doesn't
  - keeps `browser.py` focused on the browse path
  - future auth-introspection tools go here

Per-agent scope: returns only the calling agent's own vault entries
(resolved via `ctx.agent_root.name`). The agent cannot see another
agent's vault.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time as _time
from typing import List

from .registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)


async def list_auth_domains(ctx: ToolContext) -> str:
    """List the agent's authorized auth domains + current cookie status.

    Args:
        ctx: tool context (agent_root used to derive agent_id;
            web_auth vault used for the listing and status read).

    Returns:
        Plain-text summary, one line per domain, or a helpful message
        when the agent's vault holds no domains yet.
    """
    # Contract: ctx.agent_root is ~/.dpc/agents/{agent_id}/ (see
    # dpc_agent.utils.get_agent_root). Last path component IS the
    # agent_id. If the agent storage layout changes, this derivation
    # must move to a helper there — track via grep on `agent_root.name`.
    # (Same contract as browser.browse_page use_auth path.)
    agent_id = ctx.agent_root.name

    # Lazy import so the tool module stays importable when web_auth's
    # crypto deps aren't installed (test contexts).
    from dpc_client_core import web_auth

    rows = web_auth.list_domains(agent_id)
    if not rows:
        return (
            "No stored sessions in this agent's vault yet. Open the site in "
            "a visible window — browse_page(url=..., use_auth=\"example.com\", "
            "keep_open=true) — and have the person sign in there; what they "
            "sign into is stored as they do it."
        )

    lines = ["Sites with cookies stored for this agent:"]
    for row in rows:
        domain = row["domain"]
        status = web_auth.get_auth_status(agent_id, domain)
        if status.get("has_cookies"):
            expires = status.get("expires")
            if expires is None:
                tail = "session-only cookies"
            else:
                # Two things the raw epoch could not do, and the tool's own
                # description promises both: say whether re-login is needed,
                # and say it in something an agent can act on. Observed
                # 2026-08-24: `cookies expire at unix=1785787087.833` for a
                # jar whose earliest cookie had expired three weeks earlier,
                # printed beside the word "authenticated".
                when = _dt.datetime.fromtimestamp(
                    expires, _dt.timezone.utc,
                ).strftime("%Y-%m-%d %H:%M UTC")
                days = abs(expires - _time.time()) / 86400
                if expires < _time.time():
                    # `expires` is the EARLIEST cookie in the jar, so a past
                    # value means one cookie is stale — not that the session
                    # is dead. Anything stronger would be a claim wider than
                    # what get_auth_status measures.
                    tail = (
                        f"earliest cookie expired {days:.0f} day(s) ago "
                        f"({when}) — re-login may be needed"
                    )
                else:
                    tail = f"earliest cookie expires in {days:.0f} day(s) ({when})"
            lines.append(f"  - {domain}: cookies present, {tail}")
        else:
            lines.append(f"  - {domain}: no cookies stored")
    return "\n".join(lines)


def get_tools() -> List[ToolEntry]:
    """Export web-auth introspection tools for the registry."""
    return [
        ToolEntry(
            name="list_auth_domains",
            schema={
                "name": "list_auth_domains",
                "description": (
                    "List the web sites this agent has cookies stored for, and "
                    "whether those cookies are still fresh. Use it before "
                    "browse_page(use_auth=...) to see which sites a background "
                    "fetch can reach. Freshness is not liveness — only the site "
                    "can say whether a session still works, and a visible "
                    "window (keep_open=true) is where that is found out."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            },
            handler=list_auth_domains,
            timeout_sec=5,
            default_enabled=True,
        ),
    ]
