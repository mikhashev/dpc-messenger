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
from types import SimpleNamespace
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from .firewall import (
    KNOWLEDGE_EXTRACTION_KEY,
    LOCAL_PROVIDER_TYPES as _LOCAL_TYPES,
    SEND_WITHOUT_ASKING_KEY,
    TRANSCRIPTION_ONLY_PROVIDER_TYPES,
)

# Providers that answer from this machine and can chat: the firewall's local
# types minus the transcription-only ones. `openai_compatible` is deliberately
# absent: its base_url may name LM Studio next door or a service on the other
# side of the world, and this list decides what may see a whole conversation.
LOCAL_PROVIDER_TYPES = _LOCAL_TYPES - TRANSCRIPTION_ONLY_PROVIDER_TYPES

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
                f"which is not a configured provider."
            )
        return configured

    alias = first_local_provider(providers)
    if alias is None:
        # The instruction comes first because the caller shows `str(e)[:100]`
        # in the proposal summary, and the tail is what gets cut.
        raise NoKnowledgeProvider(
            "set [knowledge] cold_fallback_provider: no local provider to extract "
            "with, and the global text provider is not a substitute — the prompt "
            "carries the whole conversation."
        )
    return alias


# ── Where the extraction goes, and whether that needs the owner's yes ────────


# The fields of a `consent_required` answer that name the target; the UI echoes
# all of them back in `extract_consent`, and an entry of a remembered yes
# (privacy_rules.json `knowledge_extraction.send_without_asking`) stores them.
# `url` is the alias's `base_url` (an ollama `host`), null for a peer.
CONSENT_TARGET_FIELDS = ("provider_alias", "provider_type", "provider_kind", "host_scope",
                         "compute_host", "url")

# The screen words of a `provider_kind`, as the UI's question prints a peer's
# (extractionConsent.ts `KIND_WORD`, kept equal by a test); any other kind reads unknown.
KIND_WORD = {"vendor": "cloud", "self_hosted": "on that node's own hardware", "unknown": "unknown"}


class FallbackNeedsConsent(RuntimeError):
    """A failed extraction's retry would go to a target nobody was asked about.

    Raised by the monitor instead of retrying; `target` is what
    `extraction_target` says about the retry, `cause` the primary failure.
    """

    def __init__(self, target: Mapping[str, Any], cause: BaseException):
        self.target = dict(target)
        self.cause = cause
        super().__init__(
            f"the retry would send the conversation to {describe_target(target)}, "
            f"which was not asked about (first attempt: {cause})"
        )


def extraction_target(
    providers: Mapping[str, Any],
    compute_host: Optional[str],
    alias: Optional[str],
    *,
    model: Optional[str] = None,
    own_addresses: Any = None,
    peer_metadata: Optional[Mapping[str, Any]] = None,
) -> dict:
    """The facts of one resolved extraction target, in the words of `provider_facts`.

    A local alias's `host_scope` is that of its URL (`base_url`, or an ollama
    `host`); a local engine with no URL is at its default endpoint, this
    machine, while a vendor SDK's default URL is no place this node can name.
    """
    from .dpc_agent.provider_facts import (
        local_peer_route, provider_facts_for, provider_kind, url_host_scope)

    url = None  # a peer's URL is the peer's business
    via_alias = None
    # A local alias that is itself a route to another node (`remote_peer`, a
    # `dpc_agent` with a peer) is a peer target: the conversation leaves for that
    # node, so the owner is asked about the peer, its alias and its kind.
    route = None if compute_host else local_peer_route(providers.get(alias), alias)
    if compute_host or route:
        peer_alias, peer = (alias, compute_host) if compute_host else route
        facts = provider_facts_for(SimpleNamespace(providers=providers), peer_alias,
                                   peer, peer_metadata)
        ptype, kind, scope = None, facts["provider_kind"], None
        if route:
            via_alias, alias, compute_host = alias, peer_alias, peer
    else:
        config = getattr(providers.get(alias), "config", None) or {}
        ptype = config.get("type")
        kind = provider_kind(ptype, config.get("base_url"))
        url = config.get("base_url") or config.get("host") or None
        if url:
            scope = url_host_scope(url, own_addresses)
        else:
            scope = "this_machine" if kind == "self_hosted" else None
    return {
        "provider_alias": alias,
        "provider_type": ptype,
        "provider_kind": kind,
        "host_scope": scope,
        "compute_host": compute_host or None,
        "url": url,
        "model": model,
        # Where the call really goes: the local alias that routes to the peer.
        # Not one of CONSENT_TARGET_FIELDS; the pin sends through it, as before.
        "via_alias": via_alias,
    }


def proven_on_this_machine(target: Mapping[str, Any]) -> bool:
    """Only a model on this machine: no peer, `self_hosted`, host scope `this_machine`."""
    return (not target.get("compute_host")
            and target.get("provider_kind") == "self_hosted"
            and target.get("host_scope") == "this_machine")


def remembered_entry(target: Mapping[str, Any]) -> dict:
    """The record a remembered yes stores: the target's `CONSENT_TARGET_FIELDS`."""
    return {f: target.get(f) or None for f in CONSENT_TARGET_FIELDS}


def _remembered_identity(target: Mapping[str, Any]) -> tuple:
    """What a remembered yes is compared on.

    A peer target: its alias and its node id. Its kind is the peer menu's word
    of the moment (`self_hosted` after a HELLO, `unknown` after a restart), and
    its type, host scope and URL are the peer's business, so none of them may
    decide whether the owner's yes still holds. A local alias: every field, the
    URL included, so the same alias moved to another host, type or kind asks again.
    """
    if target.get("compute_host"):
        return (target.get("provider_alias") or None, target.get("compute_host"))
    return tuple(target.get(f) or None for f in CONSENT_TARGET_FIELDS)


def is_remembered(target: Mapping[str, Any], remembered: Any) -> bool:
    """Whether the owner chose "don't ask again" for this target (`_remembered_identity`)."""
    if not isinstance(remembered, (list, tuple)):
        return False
    wanted = _remembered_identity(target)
    return any(isinstance(e, Mapping) and _remembered_identity(e) == wanted for e in remembered)


def add_remembered(rules: dict, target: Mapping[str, Any]) -> bool:
    """Put `target`'s entry into `rules`' remembered list, in place; False when it is there already."""
    section = rules.get(KNOWLEDGE_EXTRACTION_KEY)
    if not isinstance(section, dict):
        section = rules[KNOWLEDGE_EXTRACTION_KEY] = {}
    entries = section.get(SEND_WITHOUT_ASKING_KEY)
    if not isinstance(entries, list):
        entries = section[SEND_WITHOUT_ASKING_KEY] = []
    if is_remembered(target, entries):
        return False
    entries.append(remembered_entry(target))
    return True


def may_send_unasked(target: Mapping[str, Any], remembered: Any) -> bool:
    """Provable on this machine, or a yes the owner chose to keep for this target."""
    return proven_on_this_machine(target) or is_remembered(target, remembered)


def same_target(a: Any, b: Mapping[str, Any]) -> bool:
    """Whether `a` (what the owner was asked about) names the target `b`.

    The ruler of a remembered yes (`_remembered_identity`), so the echo and the
    entry it writes agree: a peer is its alias and its node id, a local alias
    every field, the URL included (the same alias moved to another host, even
    in the same host scope, is another question).
    """
    if not isinstance(a, Mapping):
        return False
    return _remembered_identity(a) == _remembered_identity(b)


def describe_target(target: Mapping[str, Any], peer_name: Optional[str] = None) -> str:
    """One line for a log or a refusal, in the words of the UI's question.

    `peer_name` is the display name of `compute_host`, when the caller knows
    one; without it the id is shortened as the UI shortens it.
    """
    host = target.get("compute_host")
    if host:
        # A retry names no alias but sends a model; with neither, the peer picks.
        model = target.get("provider_alias") or target.get("model") or "its default model"
        kind = KIND_WORD.get(target.get("provider_kind") or "unknown", "unknown")
        return f"{model} on peer {peer_name or host[:20]} ({kind})"
    alias = target.get("provider_alias") or "an unnamed model"
    where = _url_host(target.get("url"))
    name = f"{alias} at {where}" if where else alias
    ptype, pkind, scope = (target.get("provider_type"), target.get("provider_kind"),
                           target.get("host_scope"))
    if not (ptype or pkind or scope):
        return name  # a bare stub names nothing to bracket; a real target always has a kind
    # The brackets are the question's (extractionConsent.ts `targetWords`, kept in step by a
    # test): where the URL's host is outranks the type's kind.
    if scope == "lan":
        place = "a machine on your network"
    elif scope == "unknown":
        place = "a host this node cannot place"
    elif scope == "this_machine":
        place = "a server at an address of this machine"
    elif pkind == "self_hosted":
        place = "another machine"
    else:
        place = KIND_WORD.get(pkind or "unknown", "unknown")
    return f"{name} ({ptype}, {place})" if ptype else f"{name} ({place})"


# The ports a browser's `URL.host` leaves out, by scheme.
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21}


def _url_host(url: Any) -> Optional[str]:
    """The host of a target's URL as the UI prints it (the browser's `URL.host`).

    The host name, an IPv6 address in brackets, and the port only when it is not
    the scheme's default; never the user info. A value that is no URL is shown
    as stored. Printing only: a remembered yes compares the stored `url` whole.
    """
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlparse(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return url
    if not parts.netloc or not host:
        return url
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS.get(parts.scheme.lower()):
        return f"{host}:{port}"
    return host
