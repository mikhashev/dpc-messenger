"""The gateway: outside tools reach this node's models over loopback, in two forms.

ADR-041. An IDE plugin or a CLI that speaks OpenAI's `/v1/chat/completions`
or Anthropic's `/v1/messages` talks to `127.0.0.1:9997` on the consumer's
own machine (D1) and is let in by a static key in `~/.dpc/.gateway_key`.
What it may ask for is exactly the two serving lists in `privacy_rules.json`
(D5): `compute.serving_local`, whose scarce resource is the card, and
`compute.serving_vendor`, whose scarce resource is money and which is
therefore refused past its per-caller daily ceiling. Every completion is one
`LLMManager.query` and one usage row with `caller_kind="gateway"` on this
node's ledger (D3).

Two layers, on purpose. `Gateway` is the internal one — alias, serving
class, quota or card, one call, one row — and knows nothing about HTTP.
`GatewayServer` is the HTTP surface over it — the listener, the key, the
Host check, parsing and rendering — in two wire shapes: the OpenAI one and,
over the same `Gateway`, listener, key, lists, quota and row writer, the
Anthropic Messages one (D4 amendment). Two narrowings are named rather than
discovered (M1): a stream in either shape is the whole answer in one chunk
or one `text_delta`, because `LLMManager.query` has no streaming form; and
`tools` in a Messages request are accepted and ignored — the answer is a
text block with `stop_reason: "end_turn"`, never a `tool_use` block.

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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from aiohttp import web

from .dpc_agent.llm_adapter import messages_to_prompt
from .dpc_agent.pricing import compute_cost_usd, get_billing_model
from .firewall import ServingLists
from .node_ledger import NodeLedger, default_ledger, usage_row
# The one Anthropic-to-OpenAI message converter this module reuses (a
# staticmethod, called without an instance): the providers already carry it,
# and a fourth copy here would drift from the three that exist.
from .providers.ollama_provider import OllamaProvider

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
# The connection types on which the peer's key has been proved (ADR-041 D2).
# `PeerConnection` is direct TLS both ways; the WebRTC, relay, gossip and
# hole-punched wrappers carry other values and are not served.
PROVED_CONNECTION_TYPES = ("direct_tls",)
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

    async def complete(self, alias: str, prompt: str, *, request_id: Optional[str] = None) -> Completion:
        remote = parse_remote_name(alias)
        if remote is not None:
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
            return await self._call(alias, owner, prompt, ledger, caller, request_id)

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
            return await self._call(alias, owner, prompt, ledger, caller, request_id)
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
    ) -> Completion:
        # Clocked after any wait: the price depends on the hour the call is made (D3).
        started_at = datetime.now(timezone.utc)
        clock = time.monotonic()
        try:
            result = await self._core.llm_manager.query(prompt, provider_alias=alias, return_metadata=True)
        except Exception as e:
            logger.warning("Gateway call on '%s' failed: %s", alias, e)
            raise GatewayError(502, f"provider '{alias}' failed: {e}", "upstream_error")
        duration_s = time.monotonic() - clock

        model = result.get("model")
        prompt_tokens = result.get("prompt_tokens")
        completion_tokens = result.get("response_tokens")
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
        else:
            counts_source = "ours"
            prompt_tokens, completion_tokens = self._count_here(prompt, text, model)
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


def _usage(completion: Completion) -> Dict[str, int]:
    prompt = completion.prompt_tokens or 0
    answer = completion.completion_tokens or 0
    return {"prompt_tokens": prompt, "completion_tokens": answer, "total_tokens": prompt + answer}


def _chat_completion_json(completion: Completion) -> Dict[str, Any]:
    # `model` echoes the alias the client asked for, which is what it matches on.
    return {
        "id": f"chatcmpl-{completion.request_id}",
        "object": "chat.completion",
        "created": int(completion.started_at.timestamp()),
        "model": completion.alias,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": completion.text},
            "finish_reason": "stop",
        }],
        "usage": _usage(completion),
    }


def _chat_completion_chunk_json(completion: Completion) -> Dict[str, Any]:
    return {
        "id": f"chatcmpl-{completion.request_id}",
        "object": "chat.completion.chunk",
        "created": int(completion.started_at.timestamp()),
        "model": completion.alias,
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": completion.text},
            "finish_reason": "stop",
        }],
        "usage": _usage(completion),
    }


def _openai_messages(messages: Any) -> List[Dict[str, Any]]:
    """The request's `messages`, or a 400 saying what is wrong with them."""
    if not isinstance(messages, list) or not messages:
        raise GatewayError(400, "'messages' must be a non-empty array", "invalid_request_error")
    out: List[Dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise GatewayError(400, "each message must be an object with 'role' and 'content'", "invalid_request_error")
        # OpenAI's newer name for the system turn; the role markers know one word for it.
        if message.get("role") == "developer":
            message = dict(message, role="system")
        out.append(message)
    return out


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
        try:
            body = await request.json()
        except Exception:
            raise GatewayError(400, "the request body is not JSON", "invalid_request_error")
        if not isinstance(body, dict):
            raise GatewayError(400, "the request body must be a JSON object", "invalid_request_error")
        alias = body.get("model")
        if not isinstance(alias, str) or not alias:
            raise GatewayError(400, "'model' must name a provider alias this node serves", "invalid_request_error")
        prompt = messages_to_prompt(_openai_messages(body.get("messages")))
        if not prompt:
            raise GatewayError(400, "no message carries text", "invalid_request_error")
        # Sampling parameters (temperature, max_tokens, ...) are the alias's own
        # configuration on this node and are not read from the request.
        completion = await self.gateway.complete(alias, prompt)
        if body.get("stream"):
            return await self._stream(request, completion)
        return web.json_response(_chat_completion_json(completion))

    async def _stream(self, request: web.Request, completion: Completion) -> web.StreamResponse:
        # ADR-041 M1: `LLMManager.query` has no streaming form, so the answer is
        # complete before the first byte leaves. The wire shape is still SSE —
        # one chunk carrying the whole text, then [DONE] — so a client that
        # sent stream=true parses what it expects. Token-by-token delivery
        # needs a streaming entry point on LLMManager first.
        response = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await response.prepare(request)
        await response.write(f"data: {json.dumps(_chat_completion_chunk_json(completion))}\n\n".encode("utf-8"))
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except Exception:
            raise GatewayError(400, "the request body is not JSON", "invalid_request_error")
        if not isinstance(body, dict):
            raise GatewayError(400, "the request body must be a JSON object", "invalid_request_error")
        alias = body.get("model")
        if not isinstance(alias, str) or not alias:
            raise GatewayError(400, "'model' must name a provider alias this node serves", "invalid_request_error")
        prompt = messages_to_prompt(_anthropic_messages(body))
        if not prompt:
            raise GatewayError(400, "no message carries text", "invalid_request_error")
        # `max_tokens` is required by the Messages API and read by nobody here:
        # sampling (max_tokens, temperature, top_p, stop_sequences, thinking) is
        # the alias's own configuration on this node, as on the OpenAI route.
        # `tools` and `tool_choice` are accepted and ignored (ADR-041 M1): the
        # answer is a text block, never a `tool_use` block.
        completion = await self.gateway.complete(alias, prompt)
        if body.get("stream"):
            return await self._stream_messages(request, completion)
        return web.json_response(_message_json(completion))

    async def _stream_messages(self, request: web.Request, completion: Completion) -> web.StreamResponse:
        # ADR-041 M1, as in `_stream`: the answer is complete before the first
        # byte leaves, so the six Messages events carry the whole text in one
        # `text_delta`. A client that sent stream=true parses what it expects.
        response = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await response.prepare(request)
        for event in (
            _message_start(completion),
            _content_block_start(),
            _content_block_delta(completion),
            _content_block_stop(),
            _message_delta(completion),
            _message_stop(),
        ):
            await response.write(_sse_event(event))
        await response.write_eof()
        return response


# --- the Anthropic Messages shape ------------------------------------------------


def _anthropic_error(status: int, message: str, code: str = "") -> web.Response:
    """The same refusal `_error` renders, in the Anthropic envelope; `code` is
    accepted so the guard can call either renderer alike, and is not sent —
    the envelope has no field for it."""
    body = {"type": "error", "error": {"type": _ANTHROPIC_ERROR_TYPES.get(status, "api_error"), "message": message}}
    return web.json_response(body, status=status)


def _anthropic_usage(completion: Completion) -> Dict[str, int]:
    return {"input_tokens": completion.prompt_tokens or 0, "output_tokens": completion.completion_tokens or 0}


def _message_json(completion: Completion) -> Dict[str, Any]:
    # `model` echoes the alias, as the OpenAI shape does; one text block, end_turn.
    return {
        "id": f"msg_{completion.request_id}",
        "type": "message",
        "role": "assistant",
        "model": completion.alias,
        "content": [{"type": "text", "text": completion.text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": _anthropic_usage(completion),
    }


# The six stream events, in the order the wire wants them.


def _message_start(completion: Completion) -> Dict[str, Any]:
    message = dict(_message_json(completion), content=[], stop_reason=None)
    message["usage"] = {"input_tokens": completion.prompt_tokens or 0, "output_tokens": 0}
    return {"type": "message_start", "message": message}


def _content_block_start() -> Dict[str, Any]:
    return {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}


def _content_block_delta(completion: Completion) -> Dict[str, Any]:
    return {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": completion.text}}


def _content_block_stop() -> Dict[str, Any]:
    return {"type": "content_block_stop", "index": 0}


def _message_delta(completion: Completion) -> Dict[str, Any]:
    return {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": completion.completion_tokens or 0},
    }


def _message_stop() -> Dict[str, Any]:
    return {"type": "message_stop"}


def _sse_event(event: Dict[str, Any]) -> bytes:
    """One event as two lines and a blank one; the event name is its `type`."""
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode("utf-8")


def _anthropic_messages(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The request's `system` and `messages` as the OpenAI-shaped list the
    adapter flattens, or a 400 saying what is wrong with them.

    Only the shape is checked here. The conversion — a string or block-list
    `system` to one system turn, text blocks to text, `tool_use` to the
    assistant's tool calls, `tool_result` to a tool turn, an `image` block to
    nothing — is the providers' own converter, so the flattened prompt is the
    one the OpenAI route produces for the same conversation.
    """
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
    return OllamaProvider._anthropic_to_openai_messages(system, messages)
