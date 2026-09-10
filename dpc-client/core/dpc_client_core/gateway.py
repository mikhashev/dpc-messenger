"""The OpenAI-compatible gateway: outside tools reach this node's models over loopback.

ADR-041. An IDE plugin or a CLI that speaks OpenAI's `/v1/chat/completions`
talks to `127.0.0.1:9997` on the consumer's own machine (D1) and is let in by
a static key in `~/.dpc/.gateway_key`. What it may ask for is exactly the two
serving lists in `privacy_rules.json` (D5): `compute.serving_local`, whose
scarce resource is the card, and `compute.serving_vendor`, whose scarce
resource is money and which is therefore refused past its per-caller daily
ceiling. Every completion is one `LLMManager.query` and one usage row with
`caller_kind="gateway"` on this node's ledger (D3).

Two layers, on purpose. `Gateway` is the internal one — alias, serving
class, quota or card, one call, one row — and knows nothing about HTTP.
`GatewayServer` is the OpenAI shape over it: the listener, the key, the
Host check, request parsing and response rendering. The Anthropic Messages
form is a second shape over the same `Gateway`, not a second listener.
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
}


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
    """One answered call, in the terms both HTTP shapes render from."""
    request_id: str
    alias: str
    owner: str  # "local" | "vendor"
    model: Optional[str]
    text: str
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    thinking_tokens: Optional[int]
    started_at: datetime
    duration_s: float
    billing: str
    cost_usd: float


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

    async def complete(self, alias: str, prompt: str, *, request_id: Optional[str] = None) -> Completion:
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
            request_id=request_id, alias=alias, owner=owner, model=model,
            text=result.get("response") or "",
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            thinking_tokens=result.get("thinking_tokens"),
            started_at=started_at, duration_s=duration_s, billing=billing, cost_usd=cost_usd,
        )


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
        app.router.add_get("/v1/models", self._models)
        app.router.add_post("/v1/chat/completions", self._chat_completions)
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
        logger.info("OpenAI-compatible gateway listening on http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        runner, self._runner, self._site = self._runner, None, None
        if runner is not None:
            await runner.cleanup()
            logger.info("OpenAI-compatible gateway stopped")

    def _accepted_hosts(self) -> Tuple[str, ...]:
        return tuple(f"{name}:{self.port}" for name in HOST_NAMES)

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        """Host, then the key, then the handler; a `GatewayError` becomes its status."""
        host = request.headers.get("Host", "")
        if host not in self._accepted_hosts():
            accepted = " or ".join(self._accepted_hosts())
            return _error(400, f"Host {host!r} is not this gateway: it answers to {accepted} only", "invalid_host")
        authorization = request.headers.get("Authorization", "")
        presented = authorization[len("Bearer "):] if authorization.startswith("Bearer ") else ""
        if not presented or not secrets.compare_digest(presented, self._key):
            return _error(
                401,
                "no gateway key, or the wrong one: send 'Authorization: Bearer <key>' with the contents "
                f"of {self.key_path.name} in the DPC home directory",
                "invalid_api_key",
            )
        try:
            return await handler(request)
        except GatewayError as e:
            return _error(e.status, e.message, e.code)
        except GatewayConfigError as e:
            return _error(503, f"the gateway's serving lists are refused: {e}", "serving_lists_refused")

    async def _models(self, request: web.Request) -> web.Response:
        lists = self.gateway.serving_lists()
        data = [
            {"id": alias, "object": "model", "created": 0, "owned_by": owner}
            for owner, aliases in (("local", lists.local), ("vendor", lists.vendor))
            for alias in aliases
        ]
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
