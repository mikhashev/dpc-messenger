# dpc_client_core/providers/remote_peer_provider.py

import asyncio
import contextvars
import logging
import time
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from dpc_protocol.protocol import REFUSAL_PROVIDER_LOADING, PeerRefused

from .base import AIProvider

if TYPE_CHECKING:
    from dpc_client_core.service import CoreService

logger = logging.getLogger(__name__)

# How a guest waits for a model its host is still loading (Mike's call
# 2026-10-20): a refusal coded `provider_loading` (DPTP v1.9) is asked
# again every PEER_LOADING_RETRY_EVERY_S, up to PEER_LOADING_WAIT_CAP_S in all.
# Ten seconds: each ask is one small frame and the host answers it at its door
# before anything runs, so asking often costs neither node anything, and a
# shorter step would only add progress lines the person reads as noise. Two
# minutes: the person is watching the chat, and past that a wait reads as a
# hang. It is shorter than a cold Strata start may take (the launcher allows
# `start_timeout_s`, 600 s by default), so a long load ends in a notice and
# the person sends the message again — the load the first request began goes
# on without it. A cold Strata start took 38 s on 2026-11-19, so it is
# answered on the fifth ask.
#
# The wait lives here, at the call every remote-peer alias makes, since
# 2026-11-19; the agent's pinned route uses the same function. Until then only
# that route waited, and a group image's description, a chat on a remote-peer
# alias and an agent on one gave up on the first refusal
# (ONLY-A-PINNED-AGENT-WAITS-OUT-A-HOST-STILL-LOADING-ITS-MODEL).
PEER_LOADING_RETRY_EVERY_S = 10.0
PEER_LOADING_WAIT_CAP_S = 120.0
#: The shortest step a host's own `retry_after_sec` may set, so a host that
#: says 0.01 is not asked in a busy loop.
PEER_LOADING_SHORTEST_STEP_S = 1.0

#: The agent loop's progress line for the call in flight, or None outside one.
#: Set by `run_llm_loop` around its model call, so a wait inside the adapter or
#: this provider shows in the chat where the round's progress already shows,
#: without a new argument on `chat()` that every test double of it would have
#: to learn.
CALL_PROGRESS: contextvars.ContextVar[Optional[Callable[[str], None]]] = contextvars.ContextVar(
    "dpc_agent_call_progress", default=None,
)

# Set while a wait runs, so a wait reached from inside another one's ask asks
# once and leaves the schedule to the outer one: nested waits would multiply
# each other's asks and stretch the ceiling.
_WAITING_OUT_A_LOAD: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "dpc_waiting_out_a_peer_load", default=False,
)


class PeerStillLoading(PeerRefused):
    """The host's model was still loading when the guest's wait ran out.

    A `PeerRefused` (so a `RuntimeError`), carrying the last refusal's words
    and code, plus who was waited for and for how long."""

    def __init__(self, refusal: PeerRefused, host_name: str, waited_sec: float):
        super().__init__(
            str(refusal), code=refusal.code, retry_after_sec=refusal.retry_after_sec,
        )
        self.host_name = host_name
        self.waited_sec = waited_sec


def _host_name(service: Any, peer_id: str) -> str:
    """The host's display name as this node knows it, else its node id."""
    try:
        cached = service.p2p_manager.peer_cache.get_peer(peer_id)
        name = getattr(cached, "display_name", None)
        if isinstance(name, str) and name.strip():
            return name.strip()
    except Exception:
        pass
    return peer_id


def _next_step(refusal: PeerRefused) -> float:
    """The host's own `retry_after_sec` where it gave a usable one, else ours."""
    asked = refusal.retry_after_sec
    if isinstance(asked, (int, float)) and not isinstance(asked, bool) and asked > 0:
        return max(float(asked), PEER_LOADING_SHORTEST_STEP_S)
    return PEER_LOADING_RETRY_EVERY_S


async def ask_peer_waiting_out_a_load(
    ask: Callable[[], Any], host_name: str,
) -> Any:
    """`await ask()`; while the host refuses with `provider_loading`, say so
    on the call's progress line, wait and ask again, up to
    PEER_LOADING_WAIT_CAP_S. The step is the host's `retry_after_sec` where it
    sends one, else PEER_LOADING_RETRY_EVERY_S. Any other refusal is raised at
    once, unretried - `provider_unavailable` covers a card held by the host's
    other engine and a launcher stopped after an engine failure, which no wait
    of ours ends, and a host that predates the code says "loading" in prose
    only, which is never read.

    A cancelled task stops here: the sleep is where it is waiting, and the
    cancellation propagates out of it.

    Reached from inside another wait's ask, it asks once: the outer wait owns
    the schedule."""
    if _WAITING_OUT_A_LOAD.get():
        return await ask()
    token = _WAITING_OUT_A_LOAD.set(True)
    try:
        started = time.monotonic()
        attempt = 0
        while True:
            try:
                return await ask()
            except PeerRefused as e:
                if e.code != REFUSAL_PROVIDER_LOADING:
                    e.host_name = host_name
                    logger.warning("Host %s did not serve the call: %s (code %s)",
                                   host_name, e, e.code or "none")
                    raise
                waited = time.monotonic() - started
                step = _next_step(e)
                if waited + step > PEER_LOADING_WAIT_CAP_S:
                    logger.warning("The model on %s was still loading after %.0f s; stopped waiting",
                                   host_name, waited)
                    raise PeerStillLoading(e, host_name, waited) from None
                attempt += 1
                logger.info("The model on %s is loading; asking again in %.0f s (attempt %d, %.0f s waited)",
                            host_name, step, attempt, waited)
                emit = CALL_PROGRESS.get()
                if emit is not None:
                    try:
                        emit(f"The model on {host_name} is loading, waiting… ({waited:.0f} s)")
                    except Exception as emit_err:
                        logger.debug("Progress line for the peer wait failed: %s", emit_err)
            await asyncio.sleep(step)
    finally:
        _WAITING_OUT_A_LOAD.reset(token)


class RemotePeerProvider(AIProvider):
    """
    Remote peer inference provider.

    This provider enables using a remote peer's AI model for inference,
    allowing users to leverage more powerful models on other machines
    or share compute resources within trusted peer groups.

    Configuration example (~/.dpc/providers.json):
    {
        "alias": "remote_alice_llama70b",
        "type": "remote_peer",
        "peer_id": "dpc-node-alice-123",
        "model": "llama3:70b",
        "provider": "ollama_text",  // Optional: remote provider alias
        "timeout": 60,
        "context_window": 131072
    }

    Security: Remote inference requests are controlled by the firewall
    in privacy_rules.json under the 'compute' section. The remote peer
    must have compute sharing enabled and the requester whitelisted.
    """

    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)

        # Remote peer configuration
        self.peer_id = config.get("peer_id")
        self.model = config.get("model")  # Model to request on remote peer
        self.remote_provider = config.get("provider")  # Optional: specific provider on remote
        # 1200 s: the host's own DEFAULT_TIMEOUT_SECONDS (900) plus overhead.
        # At 60 s this provider abandoned nearly every real generation while
        # the host kept producing tokens for a caller that had left.
        self.timeout = config.get("timeout", 1200.0)

        if not self.peer_id:
            raise ValueError(f"RemotePeerProvider '{alias}' requires 'peer_id' in config")

        # CoreService reference (injected by LLMManager)
        self._service = None

        logger.info(f"RemotePeerProvider '{alias}' initialized (peer={self.peer_id}, "
                   f"model={self.model}, timeout={self.timeout}s)")

    def set_service(self, service: "CoreService") -> None:
        """
        Inject CoreService reference for remote inference.

        Called by CoreService during initialization.
        """
        self._service = service
        logger.debug(f"RemotePeerProvider '{self.alias}': CoreService injected")

    async def generate_response(
        self,
        prompt: str,
        conversation_id: str = None,
        images: list = None,
        **kwargs
    ) -> str:
        """
        Generate response using remote peer's AI model.

        Args:
            prompt: The prompt to send to the remote peer
            conversation_id: Optional conversation ID (not used for remote)
            images: Optional list of images for vision queries
            **kwargs: Additional arguments (ignored for remote)

        Returns:
            Response text from the remote peer's model

        Raises:
            RuntimeError: If CoreService not injected or remote inference fails
        """
        if not self._service:
            raise RuntimeError(f"RemotePeerProvider '{self.alias}': CoreService not injected")

        logger.info(f"RemotePeerProvider '{self.alias}': Requesting inference from peer {self.peer_id}")

        service = self._service
        try:
            # A host still loading the model is waited out here, so every
            # caller of the alias gets the same wait.
            result = await ask_peer_waiting_out_a_load(
                lambda: service._request_inference_from_peer(
                    peer_id=self.peer_id,
                    prompt=prompt,
                    model=self.model,
                    provider=self.remote_provider,
                    images=images,
                    timeout=self.timeout,
                ),
                _host_name(service, self.peer_id),
            )
        except PeerRefused:
            # The host's words and code, unwrapped: a caller tells a load that
            # outlasted the wait (`PeerStillLoading`) from a shut door by them.
            # Still a `RuntimeError`, as the wrap below is.
            raise
        except asyncio.TimeoutError:
            raise RuntimeError(f"Remote inference to peer {self.peer_id} timed out after {self.timeout}s")
        except Exception as e:
            logger.error(f"RemotePeerProvider '{self.alias}': Remote inference failed: {e}")
            raise RuntimeError(f"Remote inference failed: {e}")
        # The guest's handler settles a served request with a dict, the text
        # beside its counts; every caller of a provider reads text.
        if isinstance(result, dict):
            return result.get("response") or ""
        return result

    async def generate_with_vision(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        **kwargs
    ) -> str:
        """Vision entry point for llm_manager; the DPTP request carries images."""
        return await self.generate_response(prompt, images=images, **kwargs)

    def supports_vision(self) -> bool:
        """Whether the peer's own provider row for this alias claims vision.

        Unknown is answered as no: llm_manager refuses a no with a message
        naming the provider, while a yes it cannot honour becomes a swallowed
        failure that reaches the model as though it were an analysis.
        """
        if not self._service:
            logger.debug(
                f"RemotePeerProvider '{self.alias}': no vision claim: CoreService not injected"
            )
            return False

        if not self.remote_provider:
            logger.debug(
                f"RemotePeerProvider '{self.alias}': no vision claim: "
                f"config names no provider alias on peer {self.peer_id}"
            )
            return False

        peer_providers = getattr(self._service, "peer_metadata", {}).get(
            self.peer_id, {}
        ).get("providers", [])
        for p in peer_providers:
            if p.get("alias") == self.remote_provider:
                return bool(p.get("supports_vision"))

        logger.debug(
            f"RemotePeerProvider '{self.alias}': no vision claim: provider "
            f"'{self.remote_provider}' not among the {len(peer_providers)} "
            f"advertised by peer {self.peer_id}"
        )
        return False
