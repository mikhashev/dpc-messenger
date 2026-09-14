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
call, 2026-09-13): this node's own aliases are served only while
`compute.enabled` in `privacy_rules.json` and `[gateway] enabled` in
`config.ini` are both true. `compute.enabled` governs what this node gives —
off, it shuts the peer door as it always has and its own aliases here as well
— while `[gateway] enabled` shuts this door alone, which is what the owner's
own tools need. Neither governs what this node asks for: a `remote:` name is
the peer's door (Mike's call, 2026-09-14), so a node that shares nothing
still calls its peers through its own gateway.

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
on its wire and an object inside. Both routes are handed the turns and the
tools alike: the peer wire carries `messages`, `system` and `tools` beside
the prompt (DPTP v1.7), and a stream is written as the chunks arrive —
REMOTE_INFERENCE_CHUNK frames from the host, the door's own chunks locally.
A peer whose menu row says it has no tool path is refused here by name
before the round trip, and refused again by the host on the wire; neither
answers a request with tools by quietly dropping them. What a tool field
asks and no provider here can do (`tool_choice` forcing, one call at a
time) is refused by name rather than dropped: every degradation is said on
the wire.

Two more things the peer wire under this door already carries now cross it
(D4 amendment, 2026-09-14). **Reasoning effort**: OpenAI's `reasoning_effort`
and the Messages form's `output_config.effort` and `thinking: {type:
disabled}` travel to the provider on the local route and to the host on the
peer route, where the host caps it and names what it served. The vocabulary is
the alias's own: the alias is resolved first, and the word is then checked once
against that alias's words — `reasoning_words` on the peer's menu row,
`declared_reasoning_words` on this node's — as written and never folded onto
the shared scale first, which is what made `xhigh` unreachable on the very
model that named it (live, 2026-09-14). The shared scale `off, low, medium,
high, max` stands in only for an alias whose model named no words, and folds
`xhigh` to `high` there alone. A word that reaches no rung is a 400 listing
that alias's words — one door, one dictionary — and the row then names the rung
the call ran on rather than the word that asked for it. `thinking` enabled or
adaptive without an effort word asks for the alias's effective default — the
rung its menu row advertises (`effective_reasoning_default`) — which is not a
degradation and is said nowhere. **Images**: a `data:` URL in an
OpenAI `image_url` part, or an Anthropic `image` block whose source is
base64, becomes the two fields DPTP §3.4 requires and travels *beside* the
prompt — that is the shape of the peer wire, so an image's position among
the turns is not preserved on either route. What this node will not do it
refuses by name: fetching an `http(s)` URL, an image past
`[vision] max_image_size_mb` (413, the same cap the P2P door enforces),
tools beside an image on the local route, an alias or a peer that says it
has no vision, and a peer alias whose menu row lists the effort words its
model knows and not the one that was asked for.

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
import base64
import binascii
import json
import logging
import os
import secrets
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple

from aiohttp import web
from dpc_protocol.protocol import PeerRefused

from .dpc_agent.llm_adapter import DpcLlmAdapter
from .dpc_agent.pricing import compute_cost_usd, get_billing_model
from .firewall import ServingLists
from .llm_manager import accepts_reasoning_effort, entry_point_for, flatten_messages
from .node_ledger import (
    TARIFF_FIELDS,
    NodeLedger,
    default_ledger,
    stated_output_includes_thinking,
    stated_thinking_source,
    usage_row,
)
from .p2p_manager import PROVED_CONNECTION_TYPES
from .providers.base import (
    REASONING_EFFORTS,
    REASONING_OFF,
    declared_reasoning_words,
    effective_reasoning_default,
    normalize_reasoning_effort,
    reasoning_word_for,
)

if TYPE_CHECKING:
    from .service import CoreService

logger = logging.getLogger(__name__)

# The listener's only address (ADR-041 D1). `[gateway] host` is read so a
# different value can be refused by name, never so it can be honoured.
GATEWAY_HOST = "127.0.0.1"
# Static, generated once, never regenerated on restart: Continue and its kind
# keep the key in their config. Rotation is `rotate_gateway_key`.
GATEWAY_KEY_NAME = ".gateway_key"
CALLER_KIND = "gateway"
# The two names a loopback client may put in Host; anything else is not us.
HOST_NAMES = ("127.0.0.1", "localhost")

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "invalid_request_error",
    413: "invalid_request_error",
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
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    502: "api_error",
    503: "api_error",
    504: "api_error",
}
def write_gateway_key(key_path: Path, key: str) -> None:
    """Write the key so nothing can read a half-written one: temp file, mode
    set before it has the real name, then an atomic `os.replace`. The mode
    call is advisory on Windows, as it is for `.ws_token`."""
    key_path = Path(key_path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(key_path.parent), prefix=".gateway_key.")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(key)
        try:
            os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        except OSError as chmod_err:
            logger.debug("chmod 0o600 on %s skipped: %s", key_path, chmod_err)
        os.replace(temp_path, key_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def new_gateway_key() -> str:
    """The one generator: first start and rotation mint the same kind of secret."""
    return secrets.token_urlsafe(32)


def rotate_gateway_key(key_path: Path) -> str:
    """Write a new key over `key_path` and return it. A listener holding the
    old key in memory is the caller's to update — `GatewayServer.rotate_key`
    does both."""
    key = new_gateway_key()
    write_gateway_key(key_path, key)
    logger.info(
        "Gateway key rotated at %s; a client still configured with the old key is answered 401",
        key_path,
    )
    return key


def mask_gateway_key(key: Optional[str]) -> Optional[str]:
    """`sk-…abcd`: enough to tell two keys apart, never enough to use one.
    None where there is no key, so a card can say which of the two it is."""
    if not key:
        return None
    return f"sk-\u2026{key[-4:]}" if len(key) > 4 else "sk-\u2026"


def client_config_lines(port: int, key: str, aliases: Sequence[str]) -> List[Dict[str, str]]:
    """The paste-ready configuration for the clients this door is for.

    `docs/CONFIGURATION.md` carries this function's output verbatim for the
    two clients it documents, and a test compares them, so the page and the
    button cannot drift. The key is in clear because these lines are pasted
    into another tool's config; `get_gateway_state` is the masked answer.

    `aliases` are the ones the door serves, one Continue entry each and the
    first standing where a form names a single model; with none served the
    lines render `<alias>`, a configuration to fix rather than a blank card.
    """
    base = f"http://{GATEWAY_HOST}:{port}"
    names = [alias for alias in aliases if alias] or ["<alias>"]
    first = names[0]
    entries = [
        f'    "title": "DPC {alias}",\n'
        f'    "provider": "openai",\n'
        f'    "apiBase": "{base}/v1",\n'
        f'    "apiKey": "{key}",\n'
        f'    "model": "{alias}"'
        for alias in names
    ]
    continue_text = '{\n  "models": [{\n' + "\n  }, {\n".join(entries) + "\n  }]\n}"
    return [
        {"client": "continue", "text": continue_text},
        {"client": "cursor", "text": (
            "Settings > Models > OpenAI API Key > Override base URL\n"
            f"Base URL: {base}/v1\n"
            f"API key: {key}\n"
            f"Model: {first}"
        )},
        {"client": "claude_code", "text": (
            f"export ANTHROPIC_BASE_URL={base}\n"
            f"export ANTHROPIC_API_KEY={key}\n"
            f"export ANTHROPIC_MODEL={first}        # the alias name, as in /v1/models"
        )},
        {"client": "curl", "text": (
            f'curl {base}/v1/models -H "Authorization: Bearer {key}"'
        )},
    ]


def vendor_alias_is_priced(alias: str, model: Optional[str]) -> bool:
    """Whether a call on this vendor alias would be written down with a price.

    A daily ceiling is enforced by summing the `cost_usd` of the rows already
    written (`NodeLedger.spent_today`), and `compute_cost_usd` answers 0.0 —
    never an error — for an alias no rate table knows. So an unpriced vendor
    alias is served against a ceiling that reads $0.00 for ever: the meter is
    not slow, it is absent (Ark's review of `11b1de5c`, 2026-09-14). The
    question is asked of the same function that fills the row, so the door and
    the ledger cannot disagree about which aliases have a rate.
    """
    return get_billing_model(alias, model) == "pay_per_use"


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
    peer names it. `cost_usd` is always None on the peer route: this node did
    not run the call and does not price it, and the host's own cost is not on
    the wire (D3, amendment).
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


# A provider type that transcribes and does not chat. `local_whisper` is the
# one this node ships (`llm_manager.PROVIDER_REGISTRY`), and its provider
# answers `generate_response` with `NotImplementedError`; `firewall.py` counts
# it among `LOCAL_PROVIDER_TYPES`, so such an alias may legitimately stand in
# `compute.serving_local` and be offered to a peer for transcription — it is
# simply not a chat model, on either half of this door's menu.
TRANSCRIPTION_ONLY_PROVIDER_TYPES = frozenset({"local_whisper"})


def serves_chat(provider_type: Optional[str]) -> bool:
    """Whether an alias of this provider type can answer a chat completion.

    The type is the only statement either half of the menu makes about what an
    alias does — this node's registry for its own aliases, the peer's own
    `type` field on a `PROVIDERS_RESPONSE` row (`service.build_p2p_provider_info`).
    A type this node has no name for is read as chat rather than hidden: a host
    newer than this node may serve a chat provider we cannot name, and a false
    absence is invisible to the caller while a false presence is now a refusal
    it can read.
    """
    return provider_type not in TRANSCRIPTION_ONLY_PROVIDER_TYPES


def refuse_a_transcription_alias(
    name: str, provider_type: Optional[str], *, served_by: str = "",
) -> None:
    """A transcription-only alias is refused here, by name, before any call.

    Refused with `model_not_found` — the word this door already uses for «not
    on the chat menu», and the same word `/v1/models` now makes true by leaving
    such an alias off it. No new wire word is coined for the case; the reason
    is in the message (ADR-041 D1, amendment 2026-09-14).
    """
    if serves_chat(provider_type):
        return
    says = (f"peer {served_by}'s menu row says its type is '{provider_type}'" if served_by
            else f"this node's registry gives it type '{provider_type}'")
    raise GatewayError(
        404,
        f"model '{name}' transcribes and does not chat: {says}, a provider with no text "
        "generation path, and this door serves chat only. GET /v1/models lists the aliases "
        "that answer a completion and no longer offers this one",
        "model_not_found",
    )


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
        """Whether `compute.enabled` is true right now (Mike's call, 2026-09-14).

        The flag governs what this node *gives*: its peer door, where
        `can_request_inference` reads it, and its own aliases here. Two
        switches stand on those and the table is AND — served only while
        `compute.enabled` and `[gateway] enabled` are both true. It governs
        nothing this node *asks* for: a `remote:<node_id>:<alias>` name is the
        peer's door, guarded by that peer's own flag. Asked of the live
        firewall every request: a door holding a copy would keep serving after
        the owner turned sharing off.
        """
        return bool(getattr(self._core.firewall, "compute_enabled", False))

    def refuse_unless_compute_sharing(self, alias: str) -> None:
        """The one place the shut door is said, for this node's own aliases."""
        if self.compute_sharing_on():
            return
        raise GatewayError(
            404,
            f"model '{alias}' is not served: compute.enabled is false in privacy_rules.json, which "
            "closes this node's own aliases on both of its doors — the peer door and this loopback "
            "gateway, which serves them only while compute.enabled and [gateway] enabled are both "
            "true. A peer's model is reachable from here whatever this flag says: call it by its "
            "remote:<node_id>:<alias> name, which that peer's own flag guards",
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

    def local_row_extras(self, alias: str) -> Dict[str, Any]:
        """`tariff` and `settings` for one of this node's own aliases.

        The same two keys a peer's row carries (DPTP §3.5), so an IDE client
        reads one shape for both kinds of row. The loopback caller is this node
        (`caller`), so the tariff resolved for it is what this node declares for
        the alias — what it would quote a peer, not a bill for a call made here.
        """
        core = self._core
        node_id = getattr(getattr(core, "p2p_manager", None), "node_id", None)
        providers = getattr(getattr(core, "llm_manager", None), "providers", None) or {}
        extras: Dict[str, Any] = {}
        tariff = core.menu_tariff(alias, node_id)
        if tariff is not None:
            extras["tariff"] = tariff
        settings = core.menu_settings(providers.get(alias))
        if settings:
            extras["settings"] = settings
        return extras

    def max_image_bytes(self) -> int:
        """The most one image may weigh, decoded — `[vision] max_image_size_mb`.

        The same setting the P2P door enforces on an image pasted into a chat,
        read here rather than restated: a second number would be a second cap,
        and the one that matters is the one the wire already holds the peer
        route to."""
        return int(self._core.settings.get_vision_max_image_size_mb()) * 1024 * 1024

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
        images: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: Optional[str] = None,
        effort_field: str = "reasoning_effort",
        on_chunk: Optional[Callable[..., Any]] = None,
        request_id: Optional[str] = None,
    ) -> Completion:
        """One call on one alias. `messages` is the conversation un-flattened,
        in the Anthropic shape: the local route goes through
        `LLMManager.query_messages`, and the providers see the turns, the
        `tools` beside them, and hand each chunk to `on_chunk` as it is made.
        The peer route sends the same four beside the prompt (DPTP v1.7) and
        feeds `on_chunk` from the host's REMOTE_INFERENCE_CHUNK frames, so what
        a guest gets there is what a local caller gets. `prompt` still travels
        and still carries the same turns flattened, because an older host reads
        that and nothing else.

        `images` are the wire's image dicts (`base64`, `mime_type` — DPTP
        §3.4), carried beside the prompt on both routes because that is the
        only place the peer wire has for them; on the local route they take
        `LLMManager.query`, whose vision entry point holds no tools, so tools
        beside an image are refused here rather than dropped.
        `reasoning_effort` is the word the client wrote, unfolded: the alias is
        resolved first and the word is then checked against *that* alias's
        vocabulary — the menu row's `reasoning_words` on the peer route, this
        node's `declared_reasoning_words` on the local one, and the shared scale
        only where the alias named no words of its own. What passes travels
        unchanged: to the provider on the local route, to the host on the peer
        route, which caps it and returns what it served. `effort_field` is what
        the client called the field, so a refusal names it back."""
        # The route first: `compute.enabled` is about what this node gives, so
        # it stands in front of this node's own aliases and not in front of a
        # peer's, which the peer's own flag guards.
        remote = parse_remote_name(alias)
        if remote is not None:
            return await self._complete_via_peer(
                alias, *remote, prompt, request_id=request_id,
                messages=messages, system=system, tools=tools,
                images=images, reasoning_effort=reasoning_effort,
                effort_field=effort_field, on_chunk=on_chunk,
            )
        self.refuse_unless_compute_sharing(alias)
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
        # A `local_whisper` alias is a legitimate entry in `compute.serving_local`
        # — the P2P door offers it to peers who hold transcription permission —
        # and it is not a chat model: refused here rather than at the provider,
        # which would fail after the caller had chosen it.
        refuse_a_transcription_alias(alias, (getattr(providers[alias], "config", None) or {}).get("type"))
        if images:
            self._refuse_images_the_alias_cannot_take(alias, providers[alias], images, tools)
        if reasoning_effort is not None:
            # The vocabulary first, the path second: the word this alias knows
            # is what the refusal about the path should name, and what the
            # provider is handed.
            reasoning_effort = _effort_the_alias_knows(
                reasoning_effort, declared_reasoning_words(providers[alias])[0],
                serves=f"model '{alias}'", what=effort_field,
            )
            self._refuse_effort_the_path_cannot_take(
                alias, providers[alias], reasoning_effort,
                tools=bool(tools), streaming=on_chunk is not None, images=bool(images),
            )
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
            self._refuse_an_unpriced_vendor_alias(alias, providers[alias])
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
                                    messages=messages, system=system, tools=tools, on_chunk=on_chunk,
                                    images=images, reasoning_effort=reasoning_effort)

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
                                    messages=messages, system=system, tools=tools, on_chunk=on_chunk,
                                    images=images, reasoning_effort=reasoning_effort)
        finally:
            self._inference_lock.release()

    def _refuse_an_unpriced_vendor_alias(self, alias: str, provider: Any) -> None:
        """A vendor alias this node cannot price is refused, not served free.

        The ceiling is money, and money is counted from the rows; an alias no
        rate table knows writes $0.00 on every row, so its ceiling can never
        be reached and `vendor_quotas` guards nothing. Refused with the word
        the spent ceiling already uses — the cause is the same one, the guest
        may not have this alias's tokens today — and the reason in the text,
        because the wire vocabulary is not extended from here.
        """
        model = getattr(provider, "model", None)
        if vendor_alias_is_priced(alias, model):
            return
        logger.warning(
            "Vendor alias '%s' (model %s) has no rate in this node's pricing tables, so its "
            "spend cannot be counted against compute.vendor_quotas; it is refused rather than "
            "served without a meter", alias, model,
        )
        raise GatewayError(
            429,
            f"model '{alias}' is refused: it is a vendor alias in compute.serving_vendor, and "
            f"this node has no rate for it (model {model!r}), so what it spends cannot be "
            "counted against its daily ceiling in compute.vendor_quotas — an unpriced alias is "
            "refused rather than served against a ceiling that would read $0.00 for ever",
            "insufficient_quota",
        )

    def _refuse_images_the_alias_cannot_take(
        self, alias: str, provider: Any, images: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]],
    ) -> None:
        """An image on this node's own alias: the provider must say it does
        vision, and nothing may ask for tools in the same breath."""
        if tools:
            raise GatewayError(
                400,
                f"the request carries {len(images)} image(s) and {len(tools)} tool(s): vision on this node "
                "goes through generate_with_vision, which takes no tools, so the call cannot be made as "
                "asked; send the images without tools, or the tools without images",
                "tools_unsupported",
            )
        supports_vision = getattr(provider, "supports_vision", None)
        if callable(supports_vision) and not supports_vision():
            model = getattr(provider, "model", "unknown")
            raise GatewayError(
                400,
                f"model '{alias}' ({model}) has no vision path, and the request carries {len(images)} "
                "image(s); send it to a vision-capable alias, or without images",
                "vision_unsupported",
            )

    def _refuse_effort_the_path_cannot_take(
        self, alias: str, provider: Any, effort: str, *, tools: bool, streaming: bool, images: bool,
    ) -> None:
        """The effort word must reach the provider entry point this request will
        actually take: the three entry points did not grow the parameter
        together, and a word the path cannot carry is refused rather than
        dropped. Which words the alias knows was settled before this by
        `_effort_the_alias_knows` — one check, one list."""
        if images:
            path, entry_point = "generate_with_vision", getattr(provider, "generate_with_vision", None)
        else:
            path, entry_point = entry_point_for(provider, tools=tools, streaming=streaming)
        if accepts_reasoning_effort(entry_point):
            return
        provider_type = (getattr(provider, "config", None) or {}).get("type") or "unknown"
        raise GatewayError(
            400,
            f"model '{alias}' cannot be asked for reasoning effort '{effort}' on this request: its "
            f"provider type '{provider_type}' takes no effort on the path this request needs ({path}); "
            "send the request without an effort, or to an alias whose provider takes one there",
            "reasoning_effort_unsupported",
        )

    def _served_effort(self, alias: str, result: Dict[str, Any]) -> Optional[str]:
        """The word the local row names: the rung the call actually ran on.

        The provider's own word first, because it is the only one read off the
        body that was sent. A class that declares no effort channel names no
        rung at all. Otherwise the door answers: the word it passed, resolved
        onto the alias's ladder, or `effective_reasoning_default` where the
        caller asked for nothing — the same helper the alias's menu row quotes
        and the peer door serves, so all three name one rung. None is «not
        knowable here», never `off`.
        """
        reported = result.get("provider_served_effort")
        if reported:
            return reported
        door_word = result.get("served_effort")
        provider = (getattr(self._core.llm_manager, "providers", None) or {}).get(alias)
        if provider is None:
            return door_word
        if declared_reasoning_words(provider)[0] == []:
            # No effort reaches this alias's engine, so nothing here names a
            # rung — least of all the word its configuration carries, which this
            # provider never reads.
            return None
        if door_word is not None:
            return reasoning_word_for(provider, door_word) or door_word
        return effective_reasoning_default(provider)

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
        images: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: Optional[str] = None,
        on_chunk: Optional[Callable[..., Any]] = None,
    ) -> Completion:
        # Clocked after any wait: the price depends on the hour the call is made (D3).
        started_at = datetime.now(timezone.utc)
        clock = time.monotonic()
        effort_kwargs = {"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}
        try:
            if images:
                # `query` is the only door with a vision entry point, and it
                # answers whole: a stream over this route is the one chunk the
                # shape layer writes from the finished text.
                result = await self._core.llm_manager.query(
                    prompt, provider_alias=alias, return_metadata=True, images=images, **effort_kwargs,
                )
            elif messages is None:
                result = await self._core.llm_manager.query(
                    prompt, provider_alias=alias, return_metadata=True, **effort_kwargs,
                )
            else:
                result = await self._core.llm_manager.query_messages(
                    messages, system=system, tools=tools or None, on_chunk=on_chunk,
                    provider_alias=alias, return_metadata=True, reasoning_effort=reasoning_effort,
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
                # The engine's counts where the provider reported any; the
                # door's recount, which never saw the template or an image,
                # only where it did not.
                counts_source=result.get("counts_source", "ours"),
                output_includes_thinking=result.get("output_includes_thinking", "unknown"),
                # Whether that thinking count was counted or estimated, as the
                # door was told; None where nobody said.
                thinking_source=result.get("thinking_source"),
                # The rung this call ran on, not the word that asked for it;
                # no peer is in this row to prove.
                served_effort=self._served_effort(alias, result),
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

    async def _complete_via_peer(
        self,
        name: str,
        peer_id: str,
        remote_alias: str,
        prompt: str,
        *,
        request_id: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        system: Any = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        images: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: Optional[str] = None,
        effort_field: str = "reasoning_effort",
        on_chunk: Optional[Callable[..., Any]] = None,
    ) -> Completion:
        """The peer route: connected, proved, on the menu, one call, one row.

        No local lock and no local quota on purpose: the card the call runs on
        and the key it may spend are the host's, and the host serialises and
        refuses on its own side. Every refusal here is named and none falls
        back to a local alias (D2). The menu row is the peer's own word about
        what its alias can do, so what it denies is refused here rather than
        sent to be dropped on the far side.

        The row is built from the answer and from nothing streamed: chunks
        reach `on_chunk` and are never counted (DPTP §3.4). A host that sends
        none answers whole, and the shape layer writes the one chunk it always
        wrote.
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
        row = next((entry for entry in menu if entry.get("alias") == remote_alias), None)
        if row is None:
            served = ", ".join(entry["alias"] for entry in menu) or "nothing yet"
            raise GatewayError(
                404,
                f"peer {peer_id} does not serve alias '{remote_alias}' to this node; "
                f"its menu lists: {served}",
                "model_not_found",
            )
        # The host offers two kinds of row on one list — the alias it serves for
        # inference and the one it serves for transcription, each behind its own
        # permission (`service.menu_for_peer`). Only the first answers a chat
        # completion, and the row says which it is.
        refuse_a_transcription_alias(name, row.get("type"), served_by=peer_id)
        if images and tools:
            # The host's vision door is `query`, which holds no tools, exactly
            # as this node's own is: the combination is refused on both routes
            # rather than answered without the tools on either.
            raise GatewayError(
                400,
                f"the request carries {len(images)} image(s) and {len(tools)} tool(s): vision on the "
                "peer route travels beside the prompt and reaches the host's vision entry point, which "
                "takes no tools; send the images without tools, or the tools without images",
                "tools_unsupported",
            )
        if tools and not row.get("supports_tools"):
            # An optimisation over the host's own refusal, not a second gate:
            # the round trip is saved, and a row that says nothing is read as a
            # no, because every host that can serve tools says so.
            raise GatewayError(
                400,
                f"peer {peer_id} serves '{remote_alias}' without tools — its menu row does not say "
                f"supports_tools — and the request carries {len(tools)} tool(s); ask that peer for an "
                "alias whose provider calls tools natively, or send the request without tools",
                "tools_unsupported",
            )
        if images and "supports_vision" in row and not row.get("supports_vision"):
            raise GatewayError(
                400,
                f"peer {peer_id} serves '{remote_alias}' without vision — its menu row says "
                f"supports_vision is false — and the request carries {len(images)} image(s); "
                "ask that peer for a vision-capable alias, or send the request without images",
                "vision_unsupported",
            )
        # The menu row is the vocabulary: `reasoning_words` lists the rungs the
        # host's model named, and the word is matched against them as written —
        # the guest chose on this list, and a fold onto the shared scale would
        # refuse the very word the row advertises. Where the row names none,
        # the shared scale stands in. `off` is the foot of every ladder.
        menu_words = row.get("reasoning_words")
        reasoning_effort = _effort_the_alias_knows(
            reasoning_effort, menu_words if isinstance(menu_words, list) else None,
            serves=f"peer {peer_id}'s alias '{remote_alias}'", what=effort_field,
        )

        timeout = float(self._core.settings.get_remote_inference_timeout())
        # Clocked before the send: the row's duration is the round trip as this node saw it.
        started_at = datetime.now(timezone.utc)
        clock = time.monotonic()
        try:
            result = await self._core.p2p_coordinator.request_inference_from_peer(
                peer_id, prompt, provider=remote_alias, images=images or None,
                reasoning_effort=reasoning_effort, timeout=timeout,
                messages=messages or None, system=system or None, tools=tools or None,
                on_chunk=on_chunk, request_id=request_id,
            )
        except ConnectionError as e:
            raise GatewayError(503, f"peer {peer_id} is not connected: {e}", "peer_unavailable")
        except ValueError as e:
            # The frame cap, raised at the origin by `write_message` before a
            # byte left this node (DPTP §2): tools and images together can pass
            # it, and the caller must be told which door it hit.
            raise GatewayError(
                413,
                f"the request to peer {peer_id} does not fit one DPTP frame: {e}",
                "request_too_large",
            )
        except TimeoutError:
            raise GatewayError(
                504,
                f"peer {peer_id} did not answer within {timeout:g}s ([connection] remote_inference_timeout)",
                "peer_timeout",
            )
        except PeerRefused as e:
            # The host's own refusal, and since v1.7 it names why. The word is
            # answered with the status the cause deserves, so that a client can
            # act: its own bad request is a 400 it must fix, a model it cannot
            # have is a 404, a door shut against it is a 403 it must ask a
            # person about, and only a refusal nobody here can place stays the
            # 502 every refusal used to be. The host's own text travels
            # whichever status it lands on, and the code the client reads is
            # the host's own word — one vocabulary across the hop, not two.
            status = {
                # The guest's own request, refused before the host spent anything.
                "invalid_value": 400,
                "tools_unsupported": 400,
                # Not on that peer's menu.
                "model_not_found": 404,
                # The host's door, not the request: a person decides these.
                "not_allowed": 403,
                "identity_unproved": 403,
                "onward_sharing_refused": 403,
                # The host's money, spent for today: the guest may come back
                # tomorrow, which is what 429 says and 502 does not.
                "insufficient_quota": 429,
            }.get(e.code)
            if status is None:
                raise GatewayError(502, f"peer {peer_id} refused: {e}", "peer_refused")
            raise GatewayError(status, f"peer {peer_id} refused: {e}", e.code)
        except RuntimeError as e:
            # A refusal from a host that predates the code, or from any other
            # caller in this tree that still raises the bare error.
            raise GatewayError(502, f"peer {peer_id} refused: {e}", "peer_refused")
        duration_s = time.monotonic() - clock

        result = result if isinstance(result, dict) else {"response": str(result or "")}
        text = result.get("response") or ""
        tool_calls = [call for call in result.get("tool_calls") or [] if isinstance(call, dict)]
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
        # The host's billing model travels on the wire when it counted; absent,
        # it is this node's table for the model the host named. The host's own
        # cost does not travel and is not copied: this node ran nothing and
        # prices nothing, so the row's cost stays null (D3). What it owes is the
        # owner's tariff, copied as one group or not at all.
        billing = result.get("billing") or get_billing_model(remote_alias, model)
        cost_usd = None
        tariff = {name: result.get(name) for name in TARIFF_FIELDS}
        if any(tariff[name] is None for name in TARIFF_FIELDS[:4]):
            tariff = {}
        # The id that went on the wire: the door's where a door minted one, for
        # its client has already seen that id on the first chunk, and the
        # coordinator's otherwise. The host's row joins this one on it.
        echoed = result.get("request_id") or ""
        if request_id and echoed and echoed != request_id:
            logger.warning(
                "Peer %s answered request %s under id %s; the row keeps the id that was sent",
                peer_id, request_id, echoed,
            )
        request_id = request_id or echoed
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
                # The host's word for where its thinking count came from, checked
                # here as its convention is: the wire can carry anything.
                thinking_source=stated_thinking_source(
                    result.get("thinking_source"), peer=peer_id, log=logger,
                ),
                # The host's word after its clamp, copied from the wire.
                served_effort=result.get("served_effort"),
                # Whose card ran it: the alias alone names no host, and two
                # peers serving `ollama_local` would read as one line. With it
                # the reader keys this row `remote:<peer>:<alias>` and can say
                # what is owed and to whom (D3, amendment 2026-09-14).
                served_by=peer_id,
                # The connection this call was gated on above, not one read
                # again here: a peer that dropped mid-call does not unprove the
                # call that was made.
                peer_proved=True,
                peer_connection_type=connection_type,
                started_at=started_at,
                duration_s=duration_s,
                billing=billing,
                cost_usd=cost_usd,
                **tariff,
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
            finish_reason=_finish_reason(result.get("finish_reason"), tool_calls),
            tool_calls=tool_calls,
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


# The whole scale this node knows, said in one place because every refusal
# that lists it must list the same words. It is the ladder of an alias whose
# model named none of its own; where one did, its words are the ladder.
KNOWN_EFFORTS = (REASONING_OFF,) + REASONING_EFFORTS

# What the Messages form calls the field, carried into the door so a refusal
# names the field the client wrote rather than the OpenAI form's.
ANTHROPIC_EFFORT_FIELD = "output_config.effort"


def _effort_word(value: Any, *, what: str) -> str:
    """The word as the client wrote it, refused here only when it is not a word.

    Which words are served is the *alias's* question and this layer has not
    resolved one yet: `xhigh` is a rung of its own on a model whose template
    named it and a synonym for `high` on the shared scale, so folding it here
    refused the top rung of the ladder that has it (live, 2026-09-14), and
    listed the shared scale at a door that would have listed the model's words
    one line later. Nothing is folded and nothing is refused for its meaning
    here; `_effort_the_alias_knows` is the one check, against the one list the
    alias asked for actually has.
    """
    if not isinstance(value, str) or not value.strip():
        raise GatewayError(400, f"'{what}' must be a non-empty string", "invalid_value")
    return value.strip()


def _effort_the_alias_knows(
    word: Optional[str], words: Optional[List[str]], *, serves: str, what: str,
) -> Optional[str]:
    """The rung `word` names on one alias, or a 400 listing that alias's words.

    One check per alias and one list in its refusal. Where the alias's model
    named its own words those are the vocabulary — matched as written, never
    folded onto the shared scale first, because a fold is what made `xhigh`
    unreachable on the model that named it — and the word travels on unchanged.
    Where it named none the shared scale is the ladder, and `xhigh` reads as
    `high` there as it always has. `off` is the foot of every ladder and is
    never checked against the rungs: it is not an amount of thinking, and each
    provider has its own way of saying no.
    """
    if word is None:
        return None
    asked = word.strip().lower()
    if words is not None and not words:
        # An empty vocabulary is the alias saying it serves no effort at all, so
        # `off` is refused here too: a provider with no effort channel has no
        # way of saying no either, and answering `off` would promise a knob
        # nobody turns.
        raise GatewayError(
            400,
            f"'{what}' is {word!r}: {serves} serves no reasoning effort at all — its provider "
            "sends none to its engine — so no word reaches it; send the request without an "
            "effort, or to an alias whose provider takes one",
            "invalid_value",
        )
    if asked == REASONING_OFF:
        return REASONING_OFF
    if words:
        for known in words:
            if isinstance(known, str) and known.strip().lower() == asked:
                return known
        raise GatewayError(
            400,
            f"'{what}' is {word!r}: {serves} serves the efforts {', '.join(str(w) for w in words)} — "
            "the words its own model named — and reaches none of them; ask for one of those, or send "
            "the request without an effort",
            "invalid_value",
        )
    folded = normalize_reasoning_effort(word)
    if folded is None:
        raise GatewayError(
            400,
            f"'{what}' is {word!r}, which {serves} does not know; the words it serves are "
            f"{', '.join(KNOWN_EFFORTS)} (xhigh is read as high)",
            "invalid_value",
        )
    return folded


def _openai_effort(body: Dict[str, Any]) -> Optional[str]:
    """`reasoning_effort` as the client wrote it, or None when the request named
    none — which asks for the alias's own default. Which words the alias serves
    is settled where the alias is, one layer down."""
    value = body.get("reasoning_effort")
    if value is None:
        return None
    return _effort_word(value, what="reasoning_effort")


def _anthropic_effort(body: Dict[str, Any]) -> Optional[str]:
    """The Messages form's two ways of asking, as one word or None.

    `output_config.effort` is the word (platform.claude.com/docs/en/api/messages:
    low, medium, high, xhigh, max), and `thinking: {type: disabled}` is `off`.
    `enabled` and `adaptive` name no depth, so they ask for the alias's own
    default and are read as None — `budget_tokens` is a quantity this node's
    scale cannot express and is not folded into a word. Asking for both a
    disabled `thinking` and an effort is a contradiction the client must
    resolve, not one this door picks a side of.
    """
    thinking = body.get("thinking")
    if thinking is not None:
        if not isinstance(thinking, dict) or thinking.get("type") not in ("enabled", "disabled", "adaptive"):
            raise GatewayError(
                400, "'thinking' must be {type: enabled | disabled | adaptive}", "invalid_request_error",
            )
    config = body.get("output_config")
    if config is not None and not isinstance(config, dict):
        raise GatewayError(400, "'output_config' must be an object", "invalid_request_error")
    asked = (config or {}).get("effort")
    word = None if asked is None else _effort_word(asked, what=ANTHROPIC_EFFORT_FIELD)
    if thinking is not None and thinking["type"] == "disabled":
        if word is not None and word.lower() != REASONING_OFF:
            raise GatewayError(
                400,
                f"'thinking' is disabled and 'output_config.effort' asks for {word!r}: the two contradict "
                "each other, and this node will not choose between them; send one of them",
                "invalid_request_error",
            )
        return REASONING_OFF
    return word


def _image_for_the_door(data: Any, mime_type: Any, *, what: str, max_bytes: int) -> Dict[str, Any]:
    """One image as the two fields DPTP §3.4 requires, or a refusal naming what
    is wrong with it. The cap is the wire's own, checked on the decoded bytes
    before anything is sent anywhere."""
    if not isinstance(mime_type, str) or not mime_type:
        raise GatewayError(400, f"{what} carries no media type", "invalid_request_error")
    if not isinstance(data, str) or not data:
        raise GatewayError(400, f"{what} carries no base64 data", "invalid_request_error")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as e:
        raise GatewayError(400, f"{what} is not base64: {e}", "invalid_request_error")
    if len(raw) > max_bytes:
        raise GatewayError(
            413,
            f"{what} is {len(raw) / (1024 * 1024):.2f} MB, past the "
            f"{max_bytes / (1024 * 1024):.0f} MB this node carries ([vision] max_image_size_mb)",
            "image_too_large",
        )
    return {"base64": data, "mime_type": mime_type}


def _image_from_data_url(url: Any, *, what: str, max_bytes: int) -> Dict[str, Any]:
    """An OpenAI `image_url` as the wire's image dict. Only a `data:` URL: this
    node fetches nothing from the web on a client's behalf, and a link it
    refuses to follow must not look like one it followed."""
    if not isinstance(url, str) or not url:
        raise GatewayError(400, f"{what} carries no 'url'", "invalid_request_error")
    if url.startswith(("http://", "https://")):
        raise GatewayError(
            400,
            f"{what} is a web URL: the gateway fetches nothing from the web, so send the image "
            "as a data:<mime>;base64,<payload> URL",
            "invalid_request_error",
        )
    if not url.startswith("data:") or "," not in url:
        raise GatewayError(
            400, f"{what} is not a data URL; the form is data:<mime>;base64,<payload>", "invalid_request_error",
        )
    header, _, payload = url[len("data:"):].partition(",")
    if not header.endswith(";base64"):
        raise GatewayError(
            400, f"{what} is a data URL that is not base64-encoded; the form is data:<mime>;base64,<payload>",
            "invalid_request_error",
        )
    return _image_for_the_door(payload, header[: -len(";base64")], what=what, max_bytes=max_bytes)


def _image_from_anthropic_source(source: Any, *, what: str, max_bytes: int) -> Dict[str, Any]:
    """An Anthropic `image` block's source as the wire's image dict. `url` and
    `file` are refused for the same reason a web URL is on the other form."""
    if not isinstance(source, dict):
        raise GatewayError(400, f"{what} has no 'source' object", "invalid_request_error")
    kind = source.get("type")
    if kind == "url":
        raise GatewayError(
            400,
            f"{what} has a url source: the gateway fetches nothing from the web, so send the image "
            "as {type: base64, media_type, data}",
            "invalid_request_error",
        )
    if kind != "base64":
        raise GatewayError(
            400,
            f"{what} has a source of type {kind!r}; only {{type: base64, media_type, data}} crosses "
            "the gateway",
            "invalid_request_error",
        )
    return _image_for_the_door(source.get("data"), source.get("media_type"), what=what, max_bytes=max_bytes)


def _text_of(content: Any, *, what: str, images: Optional[List[Dict[str, Any]]] = None,
             max_image_bytes: int = 0) -> str:
    """The text of an OpenAI `content`: a string, `null`, or an array of parts.

    `text` parts make the text. An `image_url` part is taken out of the turn
    and appended to `images`, to travel beside the prompt as the peer wire
    carries it; where `images` is None no image may stand — a system turn and
    a tool result have nowhere to put one — and the refusal says so. Every
    other part type is refused by name.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise GatewayError(400, f"{what} 'content' must be a string, null or an array of parts", "invalid_request_error")
    parts: List[str] = []
    for position, part in enumerate(content):
        if not isinstance(part, dict):
            raise GatewayError(400, f"{what} 'content' parts must be objects", "invalid_request_error")
        kind = part.get("type")
        if kind == "text":
            parts.append(str(part.get("text") or ""))
        elif kind == "image_url":
            if images is None:
                raise GatewayError(
                    400, f"{what} 'content' carries an image; an image crosses the gateway in a user turn only",
                    "invalid_request_error",
                )
            url = (part.get("image_url") or {}).get("url") if isinstance(part.get("image_url"), dict) else None
            images.append(_image_from_data_url(
                url, what=f"{what} content[{position}]", max_bytes=max_image_bytes,
            ))
        else:
            raise GatewayError(
                400,
                f"{what} 'content' carries a part of type {kind!r}; only 'text' and 'image_url' parts "
                "cross the gateway",
                "invalid_request_error",
            )
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


def _openai_messages(messages: Any, *, max_image_bytes: int = 0
                     ) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """The request's `messages` as `(system, turns, images)` in the Anthropic
    shape the door takes, or a 400 saying what is wrong with them.

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
    images: List[Dict[str, Any]] = []
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
                text = _text_of(content, what=what, images=images, max_image_bytes=max_image_bytes)
                turns.append({"role": "user", "content": [{"type": "text", "text": text}]})
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
    return "\n\n".join(p for p in system_parts if p), turns, images


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


def _peer_row_extras(row: Dict[str, Any]) -> Dict[str, Any]:
    """A host's `tariff` and `settings` (DPTP §3.5) carried onto its model row.

    Copied, never recomputed: on a `remote:` row these are the host's own
    statements about its own alias, and an OpenAI client ignores the keys it
    does not know.
    """
    return {key: row[key] for key in ("tariff", "settings") if isinstance(row.get(key), dict)}


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
        key = new_gateway_key()
        write_gateway_key(self.key_path, key)
        logger.info(
            "Gateway key written to %s; rotate it with the rotate_gateway_key command",
            self.key_path,
        )
        return key

    def rotate_key(self) -> str:
        """A new key on disk, then in the running listener: `_guard` compares
        every request against `self._key`, so the old one is 401 on the next
        request with no restart. Returned once, in clear, to be pasted."""
        key = rotate_gateway_key(self.key_path)
        self._key = key
        return key

    @property
    def key_masked(self) -> Optional[str]:
        """The running key masked, or the file's when the listener is down."""
        key = self._key
        if not key:
            try:
                key = self.key_path.read_text(encoding="utf-8").strip()
            except OSError:
                key = ""
        return mask_gateway_key(key)

    async def start(self) -> None:
        # Refused before the port opens: a bad list is a named error at start,
        # not a 503 discovered by the first client (ADR-041 D5, D7).
        self.gateway.serving_lists()
        self._key = self._load_or_create_key()

        # aiohttp's own default body cap is 1 MiB, which would refuse most of
        # the images this door now carries before any handler saw them. Four
        # times the image cap leaves room for base64's extra third and for the
        # conversation beside it; the image itself is still bounded by
        # `[vision] max_image_size_mb`, checked on the decoded bytes.
        app = web.Application(
            middlewares=[self._guard], client_max_size=4 * self.gateway.max_image_bytes(),
        )
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
        """The chat models this door serves, on both halves of the menu.

        Chat models and no others: a transcription-only alias is left off, on
        this node's serving lists and on a peer's rows alike, because a client
        reads this list as the menu it may complete against and a row it cannot
        call is a refusal moved from the door to after the choice.
        """
        data = []
        # The same flag that refuses a completion on this node's own alias
        # keeps it off the menu: a shut door lists nothing it would refuse.
        if self.gateway.compute_sharing_on():
            lists = self.gateway.serving_lists()
            types = self.gateway.provider_types()
            data = [
                {"id": alias, "object": "model", "created": 0, "owned_by": owner,
                 **self.gateway.local_row_extras(alias)}
                for owner, aliases in (("local", lists.local), ("vendor", lists.vendor))
                for alias in aliases
                if serves_chat(types.get(alias))
            ]
        # After the two local lists, each proved peer's menu under the peer's
        # name — listed whatever this node's flag says, since the door those
        # rows stand in is the peer's.
        data.extend(
            {"id": f"{REMOTE_PREFIX}{peer_id}:{row['alias']}", "object": "model", "created": 0,
             "owned_by": peer_id, **_peer_row_extras(row)}
            for peer_id, rows in self.gateway.peer_menu().items()
            for row in rows
            if serves_chat(row.get("type"))
        )
        return web.json_response({"object": "list", "data": data})

    async def _chat_completions(self, request: web.Request) -> web.StreamResponse:
        body = await _json_body(request)
        alias = _alias_of(body)
        system, messages, images = _openai_messages(
            body.get("messages"), max_image_bytes=self.gateway.max_image_bytes(),
        )
        tools = _openai_tools(body)
        effort = _openai_effort(body)
        # The flattened turns both routes carry: what an older host reads on
        # the peer wire, and the emptiness test below.
        prompt = flatten_messages(messages, system)
        if not prompt and not images:
            raise GatewayError(400, "no message carries text", "invalid_request_error")
        # Sampling parameters (temperature, max_tokens, ...) are the alias's own
        # configuration on this node and are not read from the request.
        if body.get("stream"):
            options = body.get("stream_options") if isinstance(body.get("stream_options"), dict) else {}
            return await self._stream(request, alias, prompt, messages, system, tools,
                                      images=images, reasoning_effort=effort,
                                      include_usage=bool(options.get("include_usage")))
        completion = await self.gateway.complete(alias, prompt, messages=messages, system=system, tools=tools,
                                                 images=images, reasoning_effort=effort)
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
        images: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: Optional[str] = None,
        include_usage: bool,
    ) -> web.StreamResponse:
        """One `chat.completion.chunk` per chunk the door hands back, then the
        calls, the stop word and the usage, then `[DONE]`. Where no chunk came
        before the answer — an image, or a host that sends none — the text and
        the stop word share one chunk, as this route wrote before; a call is
        one chunk, since the door hands it back parsed. Every event carries the
        id minted here, which is also the id the call runs under on either
        route — the shape's contract is one id per response, and the ledger
        rows of both nodes join the client on it."""
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
                alias, prompt, messages=messages, system=system, tools=tools, on_chunk=on_chunk,
                images=images, reasoning_effort=reasoning_effort, request_id=request_id,
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
        system, messages, images = _anthropic_request(
            body, max_image_bytes=self.gateway.max_image_bytes(),
        )
        tools = _anthropic_tools(body)
        effort = _anthropic_effort(body)
        # The flattened turns both routes carry: what an older host reads on
        # the peer wire, and the emptiness test below.
        prompt = flatten_messages(messages, system)
        if not prompt and not images:
            raise GatewayError(400, "no message carries text", "invalid_request_error")
        # `max_tokens` is required by the Messages API and read by nobody here:
        # sampling (max_tokens, temperature, top_p, stop_sequences) is the
        # alias's own configuration on this node, as on the OpenAI route —
        # `thinking` excepted, whose `type` is now read as an effort word.
        if body.get("stream"):
            return await self._stream_messages(request, alias, prompt, messages, system, tools,
                                               images=images, reasoning_effort=effort)
        completion = await self.gateway.complete(alias, prompt, messages=messages, system=system, tools=tools,
                                                 images=images, reasoning_effort=effort,
                                                 effort_field=ANTHROPIC_EFFORT_FIELD)
        return web.json_response(_message_json(completion))

    async def _stream_messages(
        self,
        request: web.Request,
        alias: str,
        prompt: str,
        messages: List[Dict[str, Any]],
        system: Any,
        tools: Optional[List[Dict[str, Any]]],
        *,
        images: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: Optional[str] = None,
    ) -> web.StreamResponse:
        """`message_start`, a text block fed by the door's chunks, one `tool_use`
        block per call with its whole input in one `input_json_delta` (the door
        hands the call back parsed), `message_delta` with the counts, `message_stop`.
        A head written before the door has counted says `input_tokens: 0`; the
        cumulative usage on `message_delta` carries the count. Thinking is not
        streamed. `message_start` carries the id minted here whether it leaves
        before the answer or after it, and that is the id the call runs under."""
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
                alias, prompt, messages=messages, system=system, tools=tools, on_chunk=on_chunk,
                images=images, reasoning_effort=reasoning_effort, request_id=minted,
                effort_field=ANTHROPIC_EFFORT_FIELD,
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
            await stream.write(_sse_event(
                _message_head(minted, alias, input_tokens=completion.prompt_tokens or 0)))
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


def _anthropic_request(body: Dict[str, Any], *, max_image_bytes: int = 0
                       ) -> Tuple[Any, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """The request's `system`, `messages` and images, or a 400 saying what is
    wrong with them. Shape only, with one exception: the turns travel to the
    provider un-flattened and whoever cannot take them that way renders them,
    but an `image` block is lifted out of its turn here — every renderer under
    this door drops it, and the wire carries images beside the prompt."""
    system = body.get("system")
    if system is not None and not isinstance(system, (str, list)):
        raise GatewayError(400, "'system' must be a string or an array of text blocks", "invalid_request_error")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError(400, "'messages' must be a non-empty array", "invalid_request_error")
    images: List[Dict[str, Any]] = []
    turns: List[Dict[str, Any]] = []
    for position, message in enumerate(messages):
        if not isinstance(message, dict):
            raise GatewayError(400, "each message must be an object with 'role' and 'content'", "invalid_request_error")
        role = message.get("role")
        if role not in ("user", "assistant"):
            raise GatewayError(
                400, "each message's 'role' must be 'user' or 'assistant'; the system prompt goes in 'system'",
                "invalid_request_error",
            )
        content = message.get("content")
        if not isinstance(content, (str, list)):
            raise GatewayError(
                400, "each message's 'content' must be a string or an array of blocks", "invalid_request_error",
            )
        if isinstance(content, str) or not any(
            isinstance(block, dict) and block.get("type") == "image" for block in content
        ):
            turns.append(message)
            continue
        if role != "user":
            raise GatewayError(
                400, f"messages[{position}] is an assistant turn carrying an image; an image crosses the "
                "gateway in a user turn only",
                "invalid_request_error",
            )
        kept: List[Any] = []
        for index, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "image":
                images.append(_image_from_anthropic_source(
                    block.get("source"), what=f"messages[{position}] content[{index}]",
                    max_bytes=max_image_bytes,
                ))
            else:
                kept.append(block)
        turns.append(dict(message, content=kept))
    return system, turns, images


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
