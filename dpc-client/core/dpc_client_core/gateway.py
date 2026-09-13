"""The gateway: outside tools reach this node's models over loopback, in two forms.

ADR-041. An IDE plugin or a CLI that speaks OpenAI's `/v1/chat/completions`
or Anthropic's `/v1/messages` talks to `127.0.0.1:9997` on the consumer's
own machine (D1) and is let in by a static key in `~/.dpc/.gateway_key`.
What it may ask for is exactly the two serving lists in `privacy_rules.json`
(D5): `compute.serving_local`, whose scarce resource is the card, and
`compute.serving_vendor`, whose scarce resource is money and which is
therefore refused past its per-caller daily ceiling. Every completion is one
call on `LLMManager` and one usage row with `caller_kind="gateway"` on this
node's ledger (D3).

Two switches stand on this door and the table between them is AND (Mike's
call, 2026-09-13): it serves only while `compute.enabled` in
`privacy_rules.json` and `[gateway] enabled` in `config.ini` are both true.
One word governs both of this node's doors — `compute.enabled` off shuts the
peer door as it always has and this one as well, so a node that has stopped
sharing compute has stopped, whichever side the request arrives from — while
`[gateway] enabled` shuts this door alone, which is what the owner's own
tools need.

Two layers, on purpose. `Gateway` is the internal one — alias, serving
class, quota or card, one call, one row — and knows nothing about HTTP.
`GatewayServer` is the HTTP surface over it — the listener, the key, the
Host check, parsing and rendering — in two wire shapes: the OpenAI one and,
over the same `Gateway`, listener, key, lists, quota and row writer, the
Anthropic Messages one (D4 amendment). Both shapes hand the turns to
`LLMManager.query_messages` un-flattened, in the Anthropic shape — the one
shape the provider layer takes together with tools: a `tools` list travels
to a provider that calls tools natively and a returned call comes back as a
`tool_use` block or a `tool_calls` entry, so the client's own loop — Claude
Code's, Continue's — runs against the model behind this node. The OpenAI
form converts at this edge and nowhere else: `arguments` is a JSON string
on its wire and an object inside. A stream on the local route is written as
the door hands chunks back; the peer route answers whole, because its wire
carries one request and one response (M1), and carries no tools either — a
request with tools on a peer alias is refused, never quietly answered
without them. What a tool field asks and no provider here can do
(`tool_choice` forcing, one call at a time) is refused by name rather than
dropped: every degradation is said on the wire.

A third kind of name, `remote:<node_id>:<alias>`, is a connected peer's
alias as that peer serves it to this node (D4 step 4): `/v1/models` lists
one row per alias on each proved peer's menu, owned by the peer, and a
completion on such a name is one `request_inference_from_peer` over a
connection whose key was proved (D2 — direct TLS only; WebRTC, relay and
gossip prove no sender, and a peer on one of them is refused by name, never
fallen back from). The card and the vendor key are the host's, so the peer
route takes no local lock and reads no local quota; the row it leaves is the
requester's half of a double entry (D3): the wire's `request_id`, the host's
counts, the host's billing model and cost copied or left absent — this node
did not run the call and does not price it. The host attributes the call to
the proved sender, which is this node; nothing here declares anyone else (D7).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import stat
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from aiohttp import web

from .dpc_agent.llm_adapter import DpcLlmAdapter
from .dpc_agent.pricing import compute_cost_usd, get_billing_model
from .firewall import ServingLists
from .llm_manager import flatten_messages
from .node_ledger import NodeLedger, default_ledger, stated_output_includes_thinking, usage_row
from .p2p_manager import PROVED_CONNECTION_TYPES

if TYPE_CHECKING:
    from .service import CoreService

logger = logging.getLogger(__name__)

# The listener's only address (ADR-041 D1). `[gateway] host` is read so a
# different value can be refused by name, never so it can be honoured.
GATEWAY_HOST = "127.0.0.1"
# Static, generated once, never regenerated on restart: Continue and its kind
# keep the key in their config. Rotation is deleting the file.
GATEWAY_KEY_NAME = ".gateway_key"
CALLER_KIND = "gateway"
# The two names a loopback client may put in Host; anything else is not us.
HOST_NAMES = ("127.0.0.1", "localhost")

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    404: "invalid_request_error",
    429: "insufficient_quota",
    502: "server_error",
    503: "server_error",
    504: "server_error",
}
# The same statuses in the Anthropic vocabulary: the message text is shared,
# only the envelope and the type word differ between the two shapes.
_ANTHROPIC_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    404: "not_found_error",
    429: "rate_limit_error",
    502: "api_error",
    503: "api_error",
    504: "api_error",
}
# A model name under this prefix is a peer's alias, `remote:<node_id>:<alias>`
# — the form this node already gives a peer's provider for transcription and
# in `remote_peer` configs, so one name means one thing everywhere.
REMOTE_PREFIX = "remote:"
# Requests under this prefix are answered in the Anthropic envelope, the rest
# in the OpenAI one; the guard chooses by path because it answers before any
# handler runs.
MESSAGES_PATH = "/v1/messages"


class GatewayError(Exception):
    """A request refused or failed; `status` is what the shape layer answers with."""

    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


class GatewayConfigError(ValueError):
    """A configuration error: the gateway stays shut rather than guess."""


@dataclass(frozen=True)
class Completion:
    """One answered call, in the terms both HTTP shapes render from.

    `alias` is the name the client asked for and both shapes echo — on the
    peer route the `remote:` form, while the row carries the alias as the
    peer names it. `cost_usd` is None on the peer route when the host sent
    no price: this node did not run the call and does not price it (D3).
    """
    request_id: str
    alias: str
    owner: str  # "local" | "vendor" | the peer's node id
    route: str  # "local" | "peer"
    model: Optional[str]
    text: str
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    thinking_tokens: Optional[int]
    started_at: datetime
    duration_s: float
    billing: str
    cost_usd: Optional[float]
    # What the provider said it stopped on, in its own OpenAI vocabulary, or
    # None when it said nothing; each shape converts on its way to the wire.
    finish_reason: Optional[str] = None
    # Whether `completion_tokens` already holds `thinking_tokens`, as the node that counted said.
    output_includes_thinking: str = "unknown"
    # The calls the model made, as the Anthropic `tool_use` blocks the door
    # returns them in; the OpenAI form converts on its way out.
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)


def _finish_reason(reported: Optional[str], tool_calls: List[Dict[str, Any]]) -> Optional[str]:
    """The provider's own word, except that a turn which returned a call
    stopped on it: DeepSeek's tools path reports no stop word at all, and a
    client handed a `tool_use` block under `end_turn` would not run the tool."""
    if tool_calls and reported in (None, "stop"):
        return "tool_calls"
    return reported


def parse_remote_name(name: str) -> Optional[Tuple[str, str]]:
    """`(node_id, alias)` for a `remote:<node_id>:<alias>` name, None for any
    other name, and a 404 for a name that starts the form and does not finish
    it — a half-written peer name must not fall through to the local lookup.
    """
    if not name.startswith(REMOTE_PREFIX):
        return None
    parts = name.split(":", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        raise GatewayError(
            404,
            f"model '{name}' is not a peer alias: the form is remote:<node_id>:<alias>",
            "model_not_found",
        )
    return parts[1], parts[2]


class Gateway:
    """The internal layer: one alias, one call, one row. No HTTP in here."""

    def __init__(
        self,
        core_service: "CoreService",
        *,
        ledger: Optional[NodeLedger] = None,
        inference_lock: Optional[asyncio.Semaphore] = None,
    ):
        self._core = core_service
        self._ledger = ledger
        # The card admits one generation at a time, and the peer door already
        # holds that rule in `P2PCoordinator._peer_inference_lock`; the service
        # hands the same semaphore here so a gateway call and a peer call queue
        # on one line instead of paging the model out between them. A private
        # Semaphore(1) stands in when no coordinator exists.
        self._inference_lock = inference_lock or asyncio.Semaphore(1)

    @property
    def caller(self) -> str:
        """Whose call this is: on loopback the client is the local user, so
        the row carries this node's own id (ADR-041 D7 keys attribution to
        the proved sender, and here that is us)."""
        return self._core.p2p_manager.node_id

    def provider_types(self) -> Dict[str, Optional[str]]:
        providers = getattr(self._core.llm_manager, "providers", None) or {}
        return {
            alias: (getattr(provider, "config", None) or {}).get("type")
            for alias, provider in providers.items()
        }

    def compute_sharing_on(self) -> bool:
        """Whether `compute.enabled` is true right now (Mike's call, 2026-09-13).

        One flag governs both doors: the peer one, where `can_request_inference`
        has always read it, and this one. Two switches stand on the gateway and
        the table between them is AND — it serves only while `compute.enabled`
        and `[gateway] enabled` are both true; `[gateway] enabled` never touches
        the peer door. Asked of the live firewall on every request, never copied
        at start: the rules reload on save, and a door holding a copy would keep
        serving for the rest of the session after the owner turned sharing off.
        """
        return bool(getattr(self._core.firewall, "compute_enabled", False))

    def refuse_unless_compute_sharing(self, alias: str) -> None:
        """The one place the shut door is said, for all three routes."""
        if self.compute_sharing_on():
            return
        raise GatewayError(
            404,
            f"model '{alias}' is not served: compute.enabled is false in privacy_rules.json, and it "
            "closes both of this node's doors — the peer door and this loopback gateway, which serves "
            "only while compute.enabled and [gateway] enabled are both true",
            "compute_sharing_disabled",
        )

    def serving_lists(self) -> ServingLists:
        """The two lists, classified against the live registry every time so a
        firewall reload is honoured; a list the firewall refuses is a
        configuration error here too."""
        try:
            return self._core.firewall.classify_serving_lists(self.provider_types())
        except ValueError as e:
            raise GatewayConfigError(str(e)) from e

    def peer_menu(self) -> Dict[str, List[Dict[str, Any]]]:
        """What each peer serves us, for the peers the door can reach: the
        provider rows a `PROVIDERS_RESPONSE` left in `peer_metadata`, kept
        only for peers connected right now on a proved connection.
        `peer_metadata` outlives the connection, so without the second filter
        the menu would list what the door would then refuse."""
        metadata = getattr(self._core, "peer_metadata", None) or {}
        menu: Dict[str, List[Dict[str, Any]]] = {}
        for peer_id, meta in metadata.items():
            rows = [row for row in (meta or {}).get("providers") or [] if row.get("alias")]
            if rows and self._connection_type(peer_id) in PROVED_CONNECTION_TYPES:
                menu[peer_id] = rows
        return menu

    def _connection_type(self, peer_id: str) -> Optional[str]:
        """The connection's own claim about itself, or None when the peer is
        not connected; a wrapper without the attribute is `"unknown"`, which
        is not proved."""
        peers = getattr(self._core.p2p_manager, "peers", None) or {}
        connection = peers.get(peer_id)
        if connection is None:
            return None
        return getattr(connection, "connection_type", "unknown")

    async def complete(
        self,
        alias: str,
        prompt: str,
        *,
        messages: Optional[List[Dict[str, Any]]] = None,
        system: Any = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        on_chunk: Optional[Callable[..., Any]] = None,
        request_id: Optional[str] = None,
    ) -> Completion:
        """One call on one alias. `messages` is the conversation un-flattened,
        in the Anthropic shape: the local route goes through
        `LLMManager.query_messages`, and the providers see the turns, the
        `tools` beside them, and hand each chunk to `on_chunk` as it is made.
        The peer route takes `prompt` alone — REMOTE_INFERENCE_REQUEST carries
        a prompt, no message array and no tools (ADR-041 D4, M1) — so tools on
        it are refused here, and the answer comes back whole: `on_chunk` is
        never called on that route and the shape layer sends what it got."""
        # Before the name is even resolved: with sharing off nothing is served,
        # and on the peer route nothing leaves this machine either.
        self.refuse_unless_compute_sharing(alias)
        remote = parse_remote_name(alias)
        if remote is not None:
            if tools:
                raise GatewayError(
                    400,
                    f"model '{alias}' is a peer's alias and the request carries {len(tools)} tool(s): the "
                    "peer wire (REMOTE_INFERENCE_REQUEST) carries a prompt and no tools, so the call cannot "
                    "be made as asked; send it without tools, or to a local alias",
                    "tools_unsupported",
                )
            return await self._complete_via_peer(alias, *remote, prompt)
        try:
            lists = self.serving_lists()
        except GatewayConfigError as e:
            raise GatewayError(503, f"the gateway's serving lists are refused: {e}", "serving_lists_refused")
        owner = lists.owner_of(alias)
        if owner is None:
            # Never the default provider: an alias outside the lists is not served (D5).
            raise GatewayError(
                404,
                f"model '{alias}' is not served: it is in neither compute.serving_local nor "
                "compute.serving_vendor in privacy_rules.json",
                "model_not_found",
            )
        providers = getattr(self._core.llm_manager, "providers", None) or {}
        if alias not in providers:
            raise GatewayError(503, f"model '{alias}' is listed but its provider is not loaded", "provider_unavailable")
        if tools and not hasattr(providers[alias], "generate_with_tools"):
            # Refused, not quietly answered without them: a text answer to a
            # request that asked for tools breaks the loop on the client's side.
            provider_type = (getattr(providers[alias], "config", None) or {}).get("type") or "unknown"
            raise GatewayError(
                400,
                f"model '{alias}' cannot take tools: its provider type '{provider_type}' has no native "
                f"tool-calling path (generate_with_tools), and the request carries {len(tools)} tool(s); "
                "use an alias whose provider calls tools natively, or send the request without tools",
                "tools_unsupported",
            )

        ledger = self._ledger or default_ledger()
        caller = self.caller
        if owner == "vendor":
            quota = lists.quotas[alias]
            spent = ledger.spent_today(alias, caller=caller, caller_kind=CALLER_KIND)
            if spent >= quota:
                raise GatewayError(
                    429,
                    f"model '{alias}' is refused: {caller} has spent ${spent:.4f} of its ${quota:.2f} daily "
                    "ceiling (compute.vendor_quotas); it is served again after midnight UTC",
                    "insufficient_quota",
                )
            # Money bounds a vendor alias, not the card: no queue.
            return await self._call(alias, owner, prompt, ledger, caller, request_id,
                                    messages=messages, system=system, tools=tools, on_chunk=on_chunk)

        timeout = float(self._core.settings.get_remote_inference_timeout())
        try:
            await asyncio.wait_for(self._inference_lock.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            raise GatewayError(
                503,
                f"the card is busy: model '{alias}' did not become free within {timeout:.0f}s "
                "([connection] remote_inference_timeout)",
                "card_busy",
            )
        try:
            return await self._call(alias, owner, prompt, ledger, caller, request_id,
                                    messages=messages, system=system, tools=tools, on_chunk=on_chunk)
        finally:
            self._inference_lock.release()

    async def _call(
        self,
        alias: str,
        owner: str,
        prompt: str,
        ledger: NodeLedger,
        caller: str,
        request_id: Optional[str],
        *,
        messages: Optional[List[Dict[str, Any]]] = None,
        system: Any = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        on_chunk: Optional[Callable[..., Any]] = None,
    ) -> Completion:
        # Clocked after any wait: the price depends on the hour the call is made (D3).
        started_at = datetime.now(timezone.utc)
        clock = time.monotonic()
        try:
            if messages is None:
                result = await self._core.llm_manager.query(prompt, provider_alias=alias, return_metadata=True)
            else:
                result = await self._core.llm_manager.query_messages(
                    messages, system=system, tools=tools or None, on_chunk=on_chunk,
                    provider_alias=alias, return_metadata=True,
                )
        except Exception as e:
            logger.warning("Gateway call on '%s' failed: %s", alias, e)
            raise GatewayError(502, f"provider '{alias}' failed: {e}", "upstream_error")
        duration_s = time.monotonic() - clock

        model = result.get("model")
        prompt_tokens = result.get("prompt_tokens")
        completion_tokens = result.get("response_tokens")
        tool_calls = [call for call in result.get("tool_calls") or [] if isinstance(call, dict)]
        billing = get_billing_model(alias, model)
        cost_usd = compute_cost_usd(
            alias, prompt_tokens or 0, completion_tokens or 0, model=model, at=started_at,
        )
        request_id = request_id or str(uuid.uuid4())
        try:
            row = usage_row(
                request_id=request_id,
                caller=caller,
                caller_kind=CALLER_KIND,
                alias=alias,
                model=model,
                route="local",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                thinking_tokens=result.get("thinking_tokens"),
                counts_source="ours",
                output_includes_thinking=result.get("output_includes_thinking", "unknown"),
                # The word the door applied; no peer is in this row to prove.
                served_effort=result.get("served_effort"),
                started_at=started_at,
                duration_s=duration_s,
                billing=billing,
                cost_usd=cost_usd,
            )
        except Exception:
            # The answer exists and is returned; the missing row is findable by id.
            logger.error("Usage row for gateway request %s was not built", request_id, exc_info=True)
        else:
            ledger.append(row)
        return Completion(
            request_id=request_id, alias=alias, owner=owner, route="local", model=model,
            text=result.get("response") or "",
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            thinking_tokens=result.get("thinking_tokens"),
            started_at=started_at, duration_s=duration_s, billing=billing, cost_usd=cost_usd,
            finish_reason=_finish_reason(result.get("finish_reason"), tool_calls),
            output_includes_thinking=result.get("output_includes_thinking", "unknown"),
            tool_calls=tool_calls,
        )

    async def _complete_via_peer(self, name: str, peer_id: str, remote_alias: str, prompt: str) -> Completion:
        """The peer route: connected, proved, on the menu, one call, one row.

        No local lock and no local quota on purpose: the card the call runs on
        and the key it may spend are the host's, and the host serialises and
        refuses on its own side. Every refusal here is named and none falls
        back to a local alias (D2).
        """
        connection_type = self._connection_type(peer_id)
        if connection_type is None:
            raise GatewayError(503, f"peer {peer_id} is not connected", "peer_unavailable")
        if connection_type not in PROVED_CONNECTION_TYPES:
            raise GatewayError(
                503,
                f"peer {peer_id} is not on a proved connection: it is connected over "
                f"{connection_type!r}, and peer inference is served over direct TLS only, "
                "where the peer's key has been proved (ADR-041 D2)",
                "peer_unproved",
            )
        menu = self.peer_menu().get(peer_id) or []
        if not any(row.get("alias") == remote_alias for row in menu):
            served = ", ".join(row["alias"] for row in menu) or "nothing yet"
            raise GatewayError(
                404,
                f"peer {peer_id} does not serve alias '{remote_alias}' to this node; "
                f"its menu lists: {served}",
                "model_not_found",
            )

        timeout = float(self._core.settings.get_remote_inference_timeout())
        # Clocked before the send: the row's duration is the round trip as this node saw it.
        started_at = datetime.now(timezone.utc)
        clock = time.monotonic()
        try:
            result = await self._core.p2p_coordinator.request_inference_from_peer(
                peer_id, prompt, provider=remote_alias, timeout=timeout,
            )
        except ConnectionError as e:
            raise GatewayError(503, f"peer {peer_id} is not connected: {e}", "peer_unavailable")
        except TimeoutError:
            raise GatewayError(
                504,
                f"peer {peer_id} did not answer within {timeout:g}s ([connection] remote_inference_timeout)",
                "peer_timeout",
            )
        except RuntimeError as e:
            # The host's own refusal — alias not served, firewall, quota — in its words.
            raise GatewayError(502, f"peer {peer_id} refused: {e}", "peer_refused")
        duration_s = time.monotonic() - clock

        result = result if isinstance(result, dict) else {"response": str(result or "")}
        text = result.get("response") or ""
        model = result.get("model") or remote_alias
        prompt_tokens = result.get("prompt_tokens")
        completion_tokens = result.get("response_tokens")
        if prompt_tokens and completion_tokens:
            counts_source = "engine"
            # The host's word about its own count, checked here: the wire can carry anything.
            output_includes_thinking = stated_output_includes_thinking(
                result.get("output_includes_thinking", "unknown"), peer=peer_id, log=logger,
            )
        else:
            counts_source = "ours"
            prompt_tokens, completion_tokens = self._count_here(prompt, text, model)
            # Counted here over the visible text, which the host had already
            # separated from its thinking; the label is this node's to set.
            output_includes_thinking = "excludes"
        # The host's billing model and price travel on the wire when it counted
        # them; absent, the billing model is this node's table for the model the
        # host named and the cost stays null — never 0.0, which would read as free.
        billing = result.get("billing") or get_billing_model(remote_alias, model)
        cost_usd = result.get("cost_usd")
        # The wire id, never one minted here: the host's row joins this one on it.
        request_id = result.get("request_id") or ""
        try:
            row = usage_row(
                request_id=request_id,
                caller=self.caller,
                caller_kind=CALLER_KIND,
                alias=remote_alias,
                model=model,
                route="peer",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                thinking_tokens=result.get("thinking_tokens"),
                counts_source=counts_source,
                output_includes_thinking=output_includes_thinking,
                # The host's word after its clamp, copied from the wire.
                served_effort=result.get("served_effort"),
                # The connection this call was gated on above, not one read
                # again here: a peer that dropped mid-call does not unprove the
                # call that was made.
                peer_proved=True,
                peer_connection_type=connection_type,
                started_at=started_at,
                duration_s=duration_s,
                billing=billing,
                cost_usd=cost_usd,
            )
        except Exception:
            logger.error("Usage row for gateway request %r via peer %s was not built", request_id, peer_id, exc_info=True)
        else:
            (self._ledger or default_ledger()).append(row)
        return Completion(
            request_id=request_id, alias=name, owner=peer_id, route="peer", model=model, text=text,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            thinking_tokens=result.get("thinking_tokens"),
            started_at=started_at, duration_s=duration_s, billing=billing, cost_usd=cost_usd,
            output_includes_thinking=output_includes_thinking,
        )

    def _count_here(self, prompt: str, text: str, model: str) -> Tuple[int, int]:
        """Counts for a host that sent none, as the adapter counts on its peer
        route: the node's tokenizer when it has one, else four characters a token."""
        counter = getattr(self._core.llm_manager, "token_count_manager", None)
        if counter is not None:
            try:
                return counter.count_tokens(prompt, model), counter.count_tokens(text, model)
            except Exception as e:
                logger.debug("Local token count for %s failed, estimating: %s", model, e)
        return len(prompt) // 4, len(text) // 4


# --- the OpenAI shape ------------------------------------------------------------


def _error(status: int, message: str, code: str = "") -> web.Response:
    body = {"error": {"message": message, "type": _ERROR_TYPES.get(status, "server_error"), "code": code or None}}
    return web.json_response(body, status=status)


def _output_tokens(completion: Completion) -> Tuple[int, Optional[int]]:
    """`(output total, reasoning share)` as both shapes define the counter:
    reasoning inside the total, the share beside it. `excludes` adds the
    thinking count; `includes` and `unknown` send the count as it came —
    on `unknown` adding would assume what this field exists to state."""
    count = completion.completion_tokens or 0
    thinking = completion.thinking_tokens
    if completion.output_includes_thinking == "excludes":
        return count + (thinking or 0), thinking
    return count, thinking


def _usage(completion: Completion) -> Dict[str, Any]:
    prompt = completion.prompt_tokens or 0
    output, reasoning = _output_tokens(completion)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "total_tokens": prompt + output,
        "completion_tokens_details": {"reasoning_tokens": reasoning or 0},
    }


def _openai_finish_reason(completion: Completion) -> str:
    return completion.finish_reason or "stop"


def _openai_tool_calls(completion: Completion) -> List[Dict[str, Any]]:
    """The calls as the OpenAI wire writes them: `arguments` is a JSON string
    there and an object inside (the `input` of a `tool_use` block)."""
    return [
        {
            "id": call.get("id") or f"call_{index}",
            "type": "function",
            "function": {"name": call.get("name") or "", "arguments": json.dumps(call.get("input") or {})},
        }
        for index, call in enumerate(completion.tool_calls)
    ]


def _chat_completion_json(completion: Completion) -> Dict[str, Any]:
    # `model` echoes the alias the client asked for, which is what it matches on.
    message: Dict[str, Any] = {"role": "assistant", "content": completion.text}
    if completion.tool_calls:
        # `null` content beside the calls when the model said nothing, as OpenAI writes it.
        message["content"] = completion.text or None
        message["tool_calls"] = _openai_tool_calls(completion)
    return {
        "id": f"chatcmpl-{completion.request_id}",
        "object": "chat.completion",
        "created": int(completion.started_at.timestamp()),
        "model": completion.alias,
        "choices": [{"index": 0, "message": message, "finish_reason": _openai_finish_reason(completion)}],
        "usage": _usage(completion),
    }


def _chunk_json(
    request_id: str,
    created: int,
    alias: str,
    delta: Optional[Dict[str, Any]],
    finish_reason: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One `chat.completion.chunk`; `delta=None` is the usage-only chunk with
    no choices that `stream_options.include_usage` asks for."""
    return {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion.chunk",
        "created": created,
        "model": alias,
        "choices": [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        "usage": usage,
    }


def _sse_data(payload: Dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


class _EventStream:
    """An SSE response opened on its first write, so that everything the
    gateway refuses before the first byte — an alias outside the lists, a
    spent quota, a busy card — is still an HTTP status, and only a failure
    after the first byte has to be said inside the stream."""

    def __init__(self, request: web.Request):
        self._request = request
        self.response: Optional[web.StreamResponse] = None

    @property
    def opened(self) -> bool:
        return self.response is not None

    async def _open(self) -> web.StreamResponse:
        if self.response is None:
            self.response = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
            )
            await self.response.prepare(self._request)
        return self.response

    async def write(self, data: bytes) -> None:
        await (await self._open()).write(data)

    async def close(self) -> web.StreamResponse:
        response = await self._open()
        await response.write_eof()
        return response


def _text_of(content: Any, *, what: str) -> str:
    """The text of an OpenAI `content`: a string, `null`, or an array of parts
    of which only `text` is carried — an `image_url` part is refused, since no
    image crosses the gateway (the door's `query_messages` carries none)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise GatewayError(400, f"{what} 'content' must be a string, null or an array of parts", "invalid_request_error")
    parts: List[str] = []
    for part in content:
        if not isinstance(part, dict):
            raise GatewayError(400, f"{what} 'content' parts must be objects", "invalid_request_error")
        kind = part.get("type")
        if kind != "text":
            raise GatewayError(
                400, f"{what} 'content' carries a part of type {kind!r}; only 'text' parts cross the gateway",
                "invalid_request_error",
            )
        parts.append(str(part.get("text") or ""))
    return "\n\n".join(p for p in parts if p)


def _tool_use_from_openai(call: Any, position: int) -> Dict[str, Any]:
    """An OpenAI `tool_calls` entry as the `tool_use` block inside. The
    arguments string must parse: one that does not is the client's error,
    said so, rather than an empty input handed to the model."""
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]:
        raise GatewayError(
            400, f"tool_calls[{position}] must be {{id, type: 'function', function: {{name, arguments}}}}",
            "invalid_request_error",
        )
    arguments = function.get("arguments")
    if arguments in (None, ""):
        input_data: Any = {}
    elif isinstance(arguments, str):
        try:
            input_data = json.loads(arguments)
        except json.JSONDecodeError as e:
            raise GatewayError(400, f"tool_calls[{position}].function.arguments is not JSON: {e}", "invalid_request_error")
    elif isinstance(arguments, dict):
        input_data = arguments  # some clients send the object itself; read, not refused
    else:
        raise GatewayError(400, f"tool_calls[{position}].function.arguments must be a JSON string", "invalid_request_error")
    if not isinstance(input_data, dict):
        raise GatewayError(400, f"tool_calls[{position}].function.arguments must encode a JSON object", "invalid_request_error")
    return {"type": "tool_use", "id": call.get("id") or f"call_{position}", "name": function["name"], "input": input_data}


def _openai_messages(messages: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """The request's `messages` as `(system, turns)` in the Anthropic shape the
    door takes, or a 400 saying what is wrong with them.

    Mirrors `DpcLlmAdapter._convert_messages_to_anthropic`, the in-house
    consumer of the same shapes: `system` and `developer` turns become the
    system part, an assistant turn's `tool_calls` become `tool_use` blocks,
    and a run of `tool` turns becomes one user turn of `tool_result` blocks —
    the providers' converters take them from there. What differs is that a
    malformed turn is a 400 here rather than a silent default.
    """
    if not isinstance(messages, list) or not messages:
        raise GatewayError(400, "'messages' must be a non-empty array", "invalid_request_error")
    system_parts: List[str] = []
    turns: List[Dict[str, Any]] = []
    for position, message in enumerate(messages):
        if not isinstance(message, dict):
            raise GatewayError(400, "each message must be an object with 'role' and 'content'", "invalid_request_error")
        role = message.get("role")
        what = f"messages[{position}]"
        if role in ("system", "developer"):
            # OpenAI's newer name for the system turn; the door knows one word for it.
            system_parts.append(_text_of(message.get("content"), what=what))
        elif role == "user":
            content = message.get("content")
            if isinstance(content, str):
                turns.append({"role": "user", "content": content})
            else:
                turns.append({"role": "user", "content": [{"type": "text", "text": _text_of(content, what=what)}]})
        elif role == "assistant":
            if "function_call" in message:
                raise GatewayError(
                    400, f"{what} carries the legacy 'function_call' field; send 'tool_calls'", "invalid_request_error",
                )
            blocks: List[Dict[str, Any]] = []
            text = _text_of(message.get("content"), what=what)
            if text:
                blocks.append({"type": "text", "text": text})
            tool_calls = message.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                raise GatewayError(400, f"{what} 'tool_calls' must be an array", "invalid_request_error")
            blocks.extend(_tool_use_from_openai(call, index) for index, call in enumerate(tool_calls))
            turns.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        elif role == "tool":
            tool_call_id = message.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise GatewayError(400, f"{what} is a tool turn without 'tool_call_id'", "invalid_request_error")
            block = {"type": "tool_result", "tool_use_id": tool_call_id,
                     "content": _text_of(message.get("content"), what=what)}
            previous = turns[-1] if turns else None
            if (previous is not None and previous["role"] == "user" and isinstance(previous["content"], list)
                    and all(b.get("type") == "tool_result" for b in previous["content"])):
                previous["content"].append(block)
            else:
                turns.append({"role": "user", "content": [block]})
        else:
            raise GatewayError(
                400, f"{what} has role {role!r}; the roles are system, developer, user, assistant and tool",
                "invalid_request_error",
            )
    return "\n\n".join(p for p in system_parts if p), turns


# What no provider on this node can do with a tool field, said once for both shapes.
_CANNOT_FORCE = ("cannot be honoured: every native tool path on this node asks its model with "
                 "tool_choice auto, so a call cannot be forced; send auto and name the tool in the prompt")
_CANNOT_LIMIT_PARALLEL = ("cannot be honoured: the providers on this node decide how many calls a turn "
                          "makes; omit it")


def _openai_tools(body: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """The request's `tools` in the Anthropic shape the door takes, or None
    when there are none or `tool_choice` is `none` — honoured by omission,
    which is what `none` means. A tool field this node cannot honour is a
    400 naming it, never dropped."""
    if "functions" in body or "function_call" in body:
        raise GatewayError(
            400, "the legacy 'functions' / 'function_call' fields are not served; send 'tools' and 'tool_choice'",
            "invalid_request_error",
        )
    tools = body.get("tools")
    if tools is None:
        tools = []
    if not isinstance(tools, list):
        raise GatewayError(400, "'tools' must be an array", "invalid_request_error")
    for position, tool in enumerate(tools):
        function = tool.get("function") if isinstance(tool, dict) else None
        if (not isinstance(tool, dict) or tool.get("type", "function") != "function"
                or not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]):
            raise GatewayError(
                400, f"tools[{position}] must be {{type: 'function', function: {{name, description, parameters}}}}",
                "invalid_request_error",
            )
    choice = body.get("tool_choice")
    if choice is None or choice == "auto":
        pass
    elif choice == "none":
        tools = []
    elif choice == "required" or isinstance(choice, dict):
        raise GatewayError(400, f"tool_choice {json.dumps(choice)} {_CANNOT_FORCE}", "invalid_request_error")
    else:
        raise GatewayError(400, "'tool_choice' must be 'auto', 'none', 'required' or an object", "invalid_request_error")
    if body.get("parallel_tool_calls") is False:
        raise GatewayError(400, f"parallel_tool_calls: false {_CANNOT_LIMIT_PARALLEL}", "invalid_request_error")
    if not tools:
        return None
    return DpcLlmAdapter._convert_tools_to_anthropic(tools)


async def _json_body(request: web.Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise GatewayError(400, "the request body is not JSON", "invalid_request_error")
    if not isinstance(body, dict):
        raise GatewayError(400, "the request body must be a JSON object", "invalid_request_error")
    return body


def _alias_of(body: Dict[str, Any]) -> str:
    alias = body.get("model")
    if not isinstance(alias, str) or not alias:
        raise GatewayError(400, "'model' must name a provider alias this node serves", "invalid_request_error")
    return alias


class GatewayServer:
    """The OpenAI-compatible HTTP surface over one `Gateway`."""

    def __init__(
        self,
        core_service: "CoreService",
        host: str = GATEWAY_HOST,
        port: int = 9997,
        *,
        key_path: Optional[Path] = None,
        ledger: Optional[NodeLedger] = None,
        inference_lock: Optional[asyncio.Semaphore] = None,
    ):
        if host != GATEWAY_HOST:
            raise GatewayConfigError(
                f"[gateway] host is {host!r}; the gateway listens on {GATEWAY_HOST} only and the "
                "value is not configurable (ADR-041 D1)"
            )
        self.host = host
        self.port = port
        self.key_path = Path(key_path) if key_path is not None else Path.home() / ".dpc" / GATEWAY_KEY_NAME
        self.gateway = Gateway(core_service, ledger=ledger, inference_lock=inference_lock)
        self._key = ""
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    @property
    def is_running(self) -> bool:
        return self._site is not None

    def bound_address(self) -> Tuple[str, int]:
        """The address the socket actually holds — port 0 resolves here."""
        if self._runner is None or not self._runner.addresses:
            raise RuntimeError("the gateway is not listening")
        address = self._runner.addresses[0]
        return str(address[0]), int(address[1])

    def _load_or_create_key(self) -> str:
        """Read the key, or write one if there is none.

        Read first is the whole rule: a key that exists is never replaced, so
        an IDE configured with it keeps working across restarts. Written the
        way `local_api._generate_and_persist_auth_token` writes `.ws_token`,
        including the mode call that is advisory on Windows.
        """
        try:
            key = self.key_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            key = ""
        if key:
            return key
        key = secrets.token_urlsafe(32)
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.write_text(key, encoding="utf-8")
        try:
            os.chmod(self.key_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        except OSError as chmod_err:
            logger.debug("chmod 0o600 on %s skipped: %s", self.key_path, chmod_err)
        logger.info("Gateway key written to %s; delete the file to rotate it", self.key_path)
        return key

    async def start(self) -> None:
        # Refused before the port opens: a bad list is a named error at start,
        # not a 503 discovered by the first client (ADR-041 D5, D7).
        self.gateway.serving_lists()
        self._key = self._load_or_create_key()

        app = web.Application(middlewares=[self._guard])
        # One models list for both shapes: Claude Code does not need a models route.
        app.router.add_get("/v1/models", self._models)
        app.router.add_post("/v1/chat/completions", self._chat_completions)
        app.router.add_post(MESSAGES_PATH, self._messages)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        try:
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner, self._site = runner, site
        self.port = self.bound_address()[1]
        logger.info(
            "Gateway listening on http://%s:%d (OpenAI /v1/chat/completions and Anthropic /v1/messages)",
            self.host, self.port,
        )

    async def stop(self) -> None:
        runner, self._runner, self._site = self._runner, None, None
        if runner is not None:
            await runner.cleanup()
            logger.info("OpenAI-compatible gateway stopped")

    def _accepted_hosts(self) -> Tuple[str, ...]:
        return tuple(f"{name}:{self.port}" for name in HOST_NAMES)

    @staticmethod
    def _presented_key(request: web.Request) -> str:
        """The key as either shape's clients send it: `x-api-key: <key>` (the
        Anthropic SDKs, Claude Code with ANTHROPIC_API_KEY) or
        `Authorization: Bearer <key>` (OpenAI clients, Claude Code with
        ANTHROPIC_AUTH_TOKEN). Both forms open every route; `anthropic-version`
        and `anthropic-beta` are read by nobody."""
        presented = request.headers.get("x-api-key", "")
        if presented:
            return presented
        authorization = request.headers.get("Authorization", "")
        return authorization[len("Bearer "):] if authorization.startswith("Bearer ") else ""

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        """Host, then the key, then the handler; a `GatewayError` becomes its status.

        The envelope is chosen by path: under `/v1/messages` a refusal is the
        Anthropic error object, elsewhere the OpenAI one, so a client of
        either shape parses what it expects even when no handler ran.
        """
        error = _anthropic_error if request.path.startswith(MESSAGES_PATH) else _error
        host = request.headers.get("Host", "")
        if host not in self._accepted_hosts():
            accepted = " or ".join(self._accepted_hosts())
            return error(400, f"Host {host!r} is not this gateway: it answers to {accepted} only", "invalid_host")
        presented = self._presented_key(request)
        if not presented or not secrets.compare_digest(presented, self._key):
            return error(
                401,
                "no gateway key, or the wrong one: send 'x-api-key: <key>' or 'Authorization: Bearer <key>' "
                f"with the contents of {self.key_path.name} in the DPC home directory",
                "invalid_api_key",
            )
        try:
            return await handler(request)
        except GatewayError as e:
            return error(e.status, e.message, e.code)
        except GatewayConfigError as e:
            return error(503, f"the gateway's serving lists are refused: {e}", "serving_lists_refused")

    async def _models(self, request: web.Request) -> web.Response:
        if not self.gateway.compute_sharing_on():
            # The same flag that refuses a completion empties the menu: a shut
            # door lists nothing rather than advertise what it would refuse.
            return web.json_response({"object": "list", "data": []})
        lists = self.gateway.serving_lists()
        data = [
            {"id": alias, "object": "model", "created": 0, "owned_by": owner}
            for owner, aliases in (("local", lists.local), ("vendor", lists.vendor))
            for alias in aliases
        ]
        # After the two local lists, each proved peer's menu under the peer's name.
        data.extend(
            {"id": f"{REMOTE_PREFIX}{peer_id}:{row['alias']}", "object": "model", "created": 0, "owned_by": peer_id}
            for peer_id, rows in self.gateway.peer_menu().items()
            for row in rows
        )
        return web.json_response({"object": "list", "data": data})

    async def _chat_completions(self, request: web.Request) -> web.StreamResponse:
        body = await _json_body(request)
        alias = _alias_of(body)
        system, messages = _openai_messages(body.get("messages"))
        tools = _openai_tools(body)
        # Rendered here only for the peer route, which sends a prompt, and for
        # the emptiness test below; the local route is handed the turns.
        prompt = flatten_messages(messages, system)
        if not prompt:
            raise GatewayError(400, "no message carries text", "invalid_request_error")
        # Sampling parameters (temperature, max_tokens, ...) are the alias's own
        # configuration on this node and are not read from the request.
        if body.get("stream"):
            options = body.get("stream_options") if isinstance(body.get("stream_options"), dict) else {}
            return await self._stream(request, alias, prompt, messages, system, tools,
                                      include_usage=bool(options.get("include_usage")))
        completion = await self.gateway.complete(alias, prompt, messages=messages, system=system, tools=tools)
        return web.json_response(_chat_completion_json(completion))

    async def _stream(
        self,
        request: web.Request,
        alias: str,
        prompt: str,
        messages: List[Dict[str, Any]],
        system: Any,
        tools: Optional[List[Dict[str, Any]]],
        *,
        include_usage: bool,
    ) -> web.StreamResponse:
        """One `chat.completion.chunk` per chunk the door hands back, then the
        calls, the stop word and the usage, then `[DONE]`. Where no chunk came
        before the answer (the peer route's wire answers whole, M1) the text
        and the stop word share one chunk, as this route wrote before; a call
        is one chunk, since the door hands it back parsed."""
        request_id = str(uuid.uuid4())
        created = int(time.time())
        stream = _EventStream(request)
        wrote_text = False

        async def emit(delta: Optional[Dict[str, Any]], finish_reason: Optional[str] = None,
                       usage: Optional[Dict[str, Any]] = None) -> None:
            await stream.write(_sse_data(_chunk_json(request_id, created, alias, delta, finish_reason, usage)))

        async def on_chunk(text: str, _conversation_id: Any = None) -> None:
            nonlocal wrote_text
            if not text:
                return
            delta = {"content": text} if wrote_text else {"role": "assistant", "content": text}
            wrote_text = True
            await emit(delta)

        try:
            completion = await self.gateway.complete(
                alias, prompt, messages=messages, system=system, tools=tools, on_chunk=on_chunk, request_id=request_id,
            )
        except GatewayError as e:
            if not stream.opened:
                raise
            # OpenAI has no error event; its clients surface an `error` object on a data line.
            await stream.write(_sse_data({"error": {
                "message": e.message, "type": _ERROR_TYPES.get(e.status, "server_error"), "code": e.code or None,
            }}))
            await stream.write(b"data: [DONE]\n\n")
            return await stream.close()

        # The wire id on the peer route; on the local route the id minted above.
        request_id = completion.request_id
        finish_reason = _openai_finish_reason(completion)
        inline_usage = None if include_usage else _usage(completion)
        finished = False
        if not wrote_text and completion.text:
            if completion.tool_calls:
                await emit({"role": "assistant", "content": completion.text})
            else:
                await emit({"role": "assistant", "content": completion.text}, finish_reason, inline_usage)
                finished = True
            wrote_text = True
        if completion.tool_calls:
            calls = [dict(call, index=index) for index, call in enumerate(_openai_tool_calls(completion))]
            await emit({"tool_calls": calls} if wrote_text else {"role": "assistant", "tool_calls": calls})
        if not finished:
            await emit({}, finish_reason, inline_usage)
        if include_usage:
            await emit(None, None, _usage(completion))
        await stream.write(b"data: [DONE]\n\n")
        return await stream.close()

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        body = await _json_body(request)
        alias = _alias_of(body)
        system, messages = _anthropic_request(body)
        tools = _anthropic_tools(body)
        # Rendered here only for the peer route, which sends a prompt, and for
        # the emptiness test below; the local route is handed the turns.
        prompt = flatten_messages(messages, system)
        if not prompt:
            raise GatewayError(400, "no message carries text", "invalid_request_error")
        # `max_tokens` is required by the Messages API and read by nobody here:
        # sampling (max_tokens, temperature, top_p, stop_sequences, thinking) is
        # the alias's own configuration on this node, as on the OpenAI route.
        if body.get("stream"):
            return await self._stream_messages(request, alias, prompt, messages, system, tools)
        completion = await self.gateway.complete(alias, prompt, messages=messages, system=system, tools=tools)
        return web.json_response(_message_json(completion))

    async def _stream_messages(
        self,
        request: web.Request,
        alias: str,
        prompt: str,
        messages: List[Dict[str, Any]],
        system: Any,
        tools: Optional[List[Dict[str, Any]]],
    ) -> web.StreamResponse:
        """`message_start`, a text block fed by the door's chunks, one `tool_use`
        block per call with its whole input in one `input_json_delta` (the door
        hands the call back parsed), `message_delta` with the counts, `message_stop`.
        A head written before the door has counted says `input_tokens: 0`; the
        cumulative usage on `message_delta` carries the count. Thinking is not streamed."""
        minted = str(uuid.uuid4())
        stream = _EventStream(request)
        text_block_open = False

        async def on_chunk(text: str, _conversation_id: Any = None) -> None:
            nonlocal text_block_open
            if not text:
                return
            if not stream.opened:
                await stream.write(_sse_event(_message_head(minted, alias, input_tokens=0)))
                await stream.write(_sse_event(_content_block_start(0)))
                text_block_open = True
            await stream.write(_sse_event(_text_delta(0, text)))

        try:
            completion = await self.gateway.complete(
                alias, prompt, messages=messages, system=system, tools=tools, on_chunk=on_chunk, request_id=minted,
            )
        except GatewayError as e:
            if not stream.opened:
                raise
            await stream.write(_sse_event({"type": "error", "error": {
                "type": _ANTHROPIC_ERROR_TYPES.get(e.status, "api_error"), "message": e.message,
            }}))
            return await stream.close()

        index = 0
        if not stream.opened:
            await stream.write(_sse_event(_message_start(completion)))
            if completion.text or not completion.tool_calls:
                # A text block even when empty, as the non-stream shape has one.
                await stream.write(_sse_event(_content_block_start(0)))
                await stream.write(_sse_event(_text_delta(0, completion.text)))
                text_block_open = True
        if text_block_open:
            await stream.write(_sse_event(_content_block_stop(0)))
            index = 1
        for call in completion.tool_calls:
            await stream.write(_sse_event({
                "type": "content_block_start", "index": index,
                "content_block": {"type": "tool_use", "id": call.get("id"), "name": call.get("name"), "input": {}},
            }))
            await stream.write(_sse_event({
                "type": "content_block_delta", "index": index,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(call.get("input") or {})},
            }))
            await stream.write(_sse_event(_content_block_stop(index)))
            index += 1
        await stream.write(_sse_event(_message_delta(completion)))
        await stream.write(_sse_event(_message_stop()))
        return await stream.close()


# --- the Anthropic Messages shape ------------------------------------------------


def _anthropic_error(status: int, message: str, code: str = "") -> web.Response:
    """The same refusal `_error` renders, in the Anthropic envelope; `code` is
    accepted so the guard can call either renderer alike, and is not sent —
    the envelope has no field for it."""
    body = {"type": "error", "error": {"type": _ANTHROPIC_ERROR_TYPES.get(status, "api_error"), "message": message}}
    return web.json_response(body, status=status)


# The provider's OpenAI word in the Anthropic vocabulary. An unmapped word
# travels as it is rather than being folded into `end_turn`.
_STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
}


def _stop_reason(completion: Completion) -> str:
    """What the Messages shape prints. `end_turn` for a provider that reported
    nothing is the constant this route printed before the reason existed — the
    wire has no word for «unreported»."""
    if completion.finish_reason is None:
        return "end_turn"
    return _STOP_REASONS.get(completion.finish_reason, completion.finish_reason)


def _anthropic_output(completion: Completion) -> Dict[str, Any]:
    """`output_tokens_details` only when thinking was counted: the SDK's field is optional."""
    output, thinking = _output_tokens(completion)
    usage: Dict[str, Any] = {"output_tokens": output}
    if thinking is not None:
        usage["output_tokens_details"] = {"thinking_tokens": thinking}
    return usage


def _anthropic_usage(completion: Completion) -> Dict[str, Any]:
    return {"input_tokens": completion.prompt_tokens or 0, **_anthropic_output(completion)}


def _message_content(completion: Completion) -> List[Dict[str, Any]]:
    """The text block — always, unless the turn is calls alone — then one
    `tool_use` block per call, as the door returned them."""
    blocks: List[Dict[str, Any]] = []
    if completion.text or not completion.tool_calls:
        blocks.append({"type": "text", "text": completion.text})
    blocks.extend(
        {"type": "tool_use", "id": call.get("id"), "name": call.get("name"), "input": call.get("input") or {}}
        for call in completion.tool_calls
    )
    return blocks


def _message_json(completion: Completion) -> Dict[str, Any]:
    # `model` echoes the alias, as the OpenAI shape does.
    return {
        "id": f"msg_{completion.request_id}",
        "type": "message",
        "role": "assistant",
        "model": completion.alias,
        "content": _message_content(completion),
        "stop_reason": _stop_reason(completion),
        "stop_sequence": None,
        "usage": _anthropic_usage(completion),
    }


# The stream events, in the order the wire wants them.


def _message_head(request_id: str, alias: str, *, input_tokens: int) -> Dict[str, Any]:
    return {"type": "message_start", "message": {
        "id": f"msg_{request_id}", "type": "message", "role": "assistant", "model": alias,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": 0},
    }}


def _message_start(completion: Completion) -> Dict[str, Any]:
    return _message_head(completion.request_id, completion.alias, input_tokens=completion.prompt_tokens or 0)


def _content_block_start(index: int) -> Dict[str, Any]:
    return {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}}


def _text_delta(index: int, text: str) -> Dict[str, Any]:
    return {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}}


def _content_block_stop(index: int) -> Dict[str, Any]:
    return {"type": "content_block_stop", "index": index}


def _message_delta(completion: Completion) -> Dict[str, Any]:
    # Cumulative, as the wire defines it: the input count travels here too,
    # because the head may have left before the door had counted anything.
    return {
        "type": "message_delta",
        "delta": {"stop_reason": _stop_reason(completion), "stop_sequence": None},
        "usage": _anthropic_usage(completion),
    }


def _message_stop() -> Dict[str, Any]:
    return {"type": "message_stop"}


def _sse_event(event: Dict[str, Any]) -> bytes:
    """One event as two lines and a blank one; the event name is its `type`."""
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode("utf-8")


def _anthropic_request(body: Dict[str, Any]) -> Tuple[Any, List[Dict[str, Any]]]:
    """The request's `system` and `messages` as they stand, or a 400 saying
    what is wrong with them. Shape only: the turns travel to the provider
    un-flattened, and whoever cannot take them that way renders them."""
    system = body.get("system")
    if system is not None and not isinstance(system, (str, list)):
        raise GatewayError(400, "'system' must be a string or an array of text blocks", "invalid_request_error")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError(400, "'messages' must be a non-empty array", "invalid_request_error")
    for message in messages:
        if not isinstance(message, dict):
            raise GatewayError(400, "each message must be an object with 'role' and 'content'", "invalid_request_error")
        if message.get("role") not in ("user", "assistant"):
            raise GatewayError(
                400, "each message's 'role' must be 'user' or 'assistant'; the system prompt goes in 'system'",
                "invalid_request_error",
            )
        if not isinstance(message.get("content"), (str, list)):
            raise GatewayError(
                400, "each message's 'content' must be a string or an array of blocks", "invalid_request_error",
            )
    return system, messages


def _anthropic_tools(body: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """The request's `tools` as the door takes them — `name`, `description`,
    `input_schema`, the three keys the providers' converters read — or None
    when there are none or `tool_choice` is `none`, honoured by omission. A
    server tool (`type: web_search_...`) runs on Anthropic's side and is
    refused rather than dropped; so is any `tool_choice` this node cannot
    honour (`any`, `tool`, `disable_parallel_tool_use`)."""
    tools = body.get("tools")
    if tools is None:
        tools = []
    if not isinstance(tools, list):
        raise GatewayError(400, "'tools' must be an array", "invalid_request_error")
    for position, tool in enumerate(tools):
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]:
            raise GatewayError(
                400, f"tools[{position}] must be {{name, description, input_schema}}", "invalid_request_error",
            )
        kind = tool.get("type")
        if kind not in (None, "custom"):
            raise GatewayError(
                400,
                f"tools[{position}] ({tool['name']}) is of type {kind!r}: a server tool runs on Anthropic's side "
                "and is not served here; only custom tools with an input_schema cross the gateway",
                "invalid_request_error",
            )
    choice = body.get("tool_choice")
    if choice is not None:
        if not isinstance(choice, dict) or choice.get("type") not in ("auto", "any", "tool", "none"):
            raise GatewayError(400, "'tool_choice' must be {type: auto | any | tool | none}", "invalid_request_error")
        if choice.get("disable_parallel_tool_use") is True:
            raise GatewayError(
                400, f"tool_choice.disable_parallel_tool_use {_CANNOT_LIMIT_PARALLEL}", "invalid_request_error",
            )
        if choice["type"] in ("any", "tool"):
            raise GatewayError(400, f"tool_choice {json.dumps(choice)} {_CANNOT_FORCE}", "invalid_request_error")
        if choice["type"] == "none":
            tools = []
    if not tools:
        return None
    return [
        {
            "name": tool["name"],
            "description": tool.get("description") or "",
            "input_schema": tool.get("input_schema") or {"type": "object", "properties": {}},
        }
        for tool in tools
    ]
