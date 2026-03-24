"""
Skill Coordinator — Coordinates P2P skill sharing between peers.

Manages async request-response flows for:
  SKILL_SEARCH  → SKILLS_CATALOG  (search peer's shareable skills)
  SKILL_REQUEST → SKILL_DATA      (request full skill content)
  SKILL_OFFER                     (peer proactively pushes a skill)

Also handles saving received skills with peer provenance and firewall checks.

This is Phase 5a of the Memento-Skills Reflective Learning loop.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TIMEOUT_SEARCH = 10.0   # seconds to wait for SKILLS_CATALOG response
_TIMEOUT_REQUEST = 15.0  # seconds to wait for SKILL_DATA response


class SkillCoordinator:
    """Coordinates P2P skill sharing with firewall and provenance enforcement."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.firewall = service.firewall
        self.p2p_manager = service.p2p_manager

        # Pending async request tracking
        self._pending_searches: Dict[str, asyncio.Future] = {}
        self._pending_requests: Dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------------
    # Outbound: search, request, offer
    # ------------------------------------------------------------------

    async def search_peer_skills(
        self,
        peer_id: str,
        tags: Optional[List[str]] = None,
        query: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Ask a connected peer for their shareable skill catalog.

        Returns a list of skill metadata dicts: [{name, description, tags, version}].
        Returns [] on timeout or if peer is not connected.
        """
        if peer_id not in self.p2p_manager.peers:
            logger.debug("search_peer_skills: peer %s not connected", peer_id)
            return []

        request_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.Future()
        self._pending_searches[request_id] = future

        message = {
            "command": "SKILL_SEARCH",
            "payload": {
                "request_id": request_id,
                "tags": tags or [],
                "query": query or "",
            },
        }
        try:
            await self.p2p_manager.send_message_to_peer(peer_id, message)
            result = await asyncio.wait_for(future, timeout=_TIMEOUT_SEARCH)
            return result if isinstance(result, list) else []
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for SKILLS_CATALOG from %s", peer_id)
            return []
        except Exception as e:
            logger.error("Error searching skills from %s: %s", peer_id, e, exc_info=True)
            return []
        finally:
            self._pending_searches.pop(request_id, None)

    async def request_skill_from_peer(
        self,
        peer_id: str,
        skill_name: str,
    ) -> Optional[str]:
        """
        Request the full SKILL.md content for a specific skill from a peer.

        Returns the raw SKILL.md text or None on failure / timeout.
        Firewall `accept_peer_skills` must be True; checked here and again in
        save_received_skill() for defence-in-depth.
        """
        if not self.firewall.get_agent_skill_permission("accept_peer_skills"):
            logger.info("request_skill_from_peer blocked: accept_peer_skills is disabled")
            return None

        if peer_id not in self.p2p_manager.peers:
            logger.debug("request_skill_from_peer: peer %s not connected", peer_id)
            return None

        request_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.Future()
        self._pending_requests[request_id] = future

        message = {
            "command": "SKILL_REQUEST",
            "payload": {
                "request_id": request_id,
                "skill_name": skill_name,
            },
        }
        try:
            await self.p2p_manager.send_message_to_peer(peer_id, message)
            content = await asyncio.wait_for(future, timeout=_TIMEOUT_REQUEST)
            return content if isinstance(content, str) else None
        except asyncio.TimeoutError:
            logger.warning("Timeout waiting for SKILL_DATA '%s' from %s", skill_name, peer_id)
            return None
        except Exception as e:
            logger.error("Error requesting skill '%s' from %s: %s", skill_name, peer_id, e, exc_info=True)
            return None
        finally:
            self._pending_requests.pop(request_id, None)

    async def offer_skill_to_peer(self, peer_id: str, skill_name: str) -> bool:
        """
        Proactively push a shareable skill to a connected peer.

        The source skill must have sharing.shareable == True.
        Returns True if the offer was sent (delivery not guaranteed).
        """
        if peer_id not in self.p2p_manager.peers:
            logger.debug("offer_skill_to_peer: peer %s not connected", peer_id)
            return False

        # Locate agent skill store (may be on any of our running agents)
        skill_store = self._get_agent_skill_store()
        if skill_store is None:
            logger.warning("offer_skill_to_peer: no skill store available")
            return False

        manifest = skill_store.load_manifest(skill_name)
        if manifest is None:
            logger.warning("offer_skill_to_peer: skill '%s' not found", skill_name)
            return False
        if not (manifest.sharing and manifest.sharing.shareable):
            logger.warning("offer_skill_to_peer: skill '%s' is not shareable", skill_name)
            return False

        content = skill_store.load_skill_content(skill_name)
        if content is None:
            return False

        skill_tags = list(manifest.metadata.tags) if manifest.metadata else []
        message = {
            "command": "SKILL_OFFER",
            "payload": {
                "skill_name": skill_name,
                "description": manifest.description or "",
                "tags": skill_tags,
                "version": manifest.version or 1,
                "content": content,
            },
        }
        try:
            await self.p2p_manager.send_message_to_peer(peer_id, message)
            logger.info("Sent SKILL_OFFER '%s' to %s", skill_name, peer_id)
            return True
        except Exception as e:
            logger.error("Error sending skill offer to %s: %s", peer_id, e, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Inbound: resolve pending futures (called by handlers)
    # ------------------------------------------------------------------

    def resolve_catalog(self, request_id: str, skills_meta: List[Dict[str, Any]]) -> None:
        """Resolve a pending search future with catalog data from peer."""
        future = self._pending_searches.get(request_id)
        if future and not future.done():
            future.set_result(skills_meta)

    def resolve_skill_data(self, request_id: str, content: Optional[str]) -> None:
        """Resolve a pending skill request future with SKILL.md content."""
        future = self._pending_requests.get(request_id)
        if future and not future.done():
            future.set_result(content)

    # ------------------------------------------------------------------
    # Inbound: handle SKILL_SEARCH (peer searching our skills)
    # ------------------------------------------------------------------

    async def handle_skill_search(
        self,
        sender_node_id: str,
        request_id: str,
        tags: Optional[List[str]] = None,
        query: Optional[str] = None,
    ) -> None:
        """
        Respond to an incoming SKILL_SEARCH from a peer.

        Finds our shareable skills, filters by tags/query, sends SKILLS_CATALOG.
        """
        skill_store = self._get_agent_skill_store()
        skills: List[Dict[str, Any]] = []
        if skill_store is not None:
            skills = skill_store.list_shareable_skills(tags=tags or None)
            # Optional freetext filter on name/description
            if query:
                q = query.lower()
                skills = [
                    s for s in skills
                    if q in s.get("name", "").lower() or q in s.get("description", "").lower()
                ]

        response = {
            "command": "SKILLS_CATALOG",
            "payload": {
                "request_id": request_id,
                "skills": skills,
            },
        }
        try:
            await self.p2p_manager.send_message_to_peer(sender_node_id, response)
        except Exception as e:
            logger.error("Error sending SKILLS_CATALOG to %s: %s", sender_node_id, e, exc_info=True)

    # ------------------------------------------------------------------
    # Inbound: handle SKILL_REQUEST (peer requesting our skill content)
    # ------------------------------------------------------------------

    async def handle_skill_request(
        self,
        sender_node_id: str,
        request_id: str,
        skill_name: str,
    ) -> None:
        """
        Respond to an incoming SKILL_REQUEST from a peer.

        Checks sharing.shareable before sending content.
        """
        skill_store = self._get_agent_skill_store()
        content: Optional[str] = None

        if skill_store is not None:
            manifest = skill_store.load_manifest(skill_name)
            if manifest and manifest.sharing and manifest.sharing.shareable:
                content = skill_store.load_skill_content(skill_name)

        if content is None:
            logger.info(
                "SKILL_REQUEST denied: skill '%s' not found or not shareable (peer %s)",
                skill_name,
                sender_node_id,
            )
            # Send empty response so peer's future doesn't hang
            content = ""

        response = {
            "command": "SKILL_DATA",
            "payload": {
                "request_id": request_id,
                "skill_name": skill_name,
                "content": content,
            },
        }
        try:
            await self.p2p_manager.send_message_to_peer(sender_node_id, response)
        except Exception as e:
            logger.error("Error sending SKILL_DATA to %s: %s", sender_node_id, e, exc_info=True)

    # ------------------------------------------------------------------
    # Save received skill (called by SkillDataHandler and SkillOfferHandler)
    # ------------------------------------------------------------------

    async def save_received_skill(
        self,
        sender_node_id: str,
        skill_name: str,
        content: str,
    ) -> str:
        """
        Persist a skill received from a peer, enforcing firewall + provenance rules.

        Returns a status string suitable for logging / UI notification.
        """
        if not content:
            return f"⚠️ Received empty content for skill '{skill_name}' from {sender_node_id}"

        if not self.firewall.get_agent_skill_permission("accept_peer_skills"):
            return f"⚠️ Firewall blocks skill receive: 'accept_peer_skills' is disabled"

        skill_store = self._get_agent_skill_store()
        if skill_store is None:
            return "⚠️ No skill store available to save received skill"

        # Don't overwrite locally-authored or bootstrapped skills
        existing = skill_store.load_manifest(skill_name)
        if existing and existing.provenance:
            if existing.provenance.source not in ("peer", "local_agent"):
                return (
                    f"⚠️ Skill '{skill_name}' already exists locally "
                    f"(source: {existing.provenance.source}). Not overwriting."
                )

        # Patch provenance to reflect peer origin
        from .dpc_agent.utils import utc_now_iso
        from .dpc_agent.tools.skills import _patch_frontmatter_field, _patch_sharing_field

        patched = content
        patched = _patch_frontmatter_field(patched, "source", "peer")
        patched = _patch_frontmatter_field(patched, "origin_peer", sender_node_id)
        patched = _patch_frontmatter_field(patched, "created_at", utc_now_iso())
        # Received skills are not shareable by default
        patched = _patch_sharing_field(patched, "shareable", "false")

        try:
            skill_store.save_skill(skill_name, patched)
        except Exception as e:
            return f"⚠️ Failed to save skill '{skill_name}': {e}"

        # Notify UI
        await self._notify_skill_received(sender_node_id, skill_name)
        logger.info("Saved received skill '%s' from peer %s", skill_name, sender_node_id)
        return f"✓ Received skill '{skill_name}' from peer {sender_node_id}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_agent_skill_store(self):
        """
        Return a SkillStore instance for the first running agent, or None.

        Skills are agent-scoped; we use the first running agent as the
        default for P2P skill sharing.
        """
        try:
            agent_manager = getattr(self.service, "agent_manager", None)
            if agent_manager is None:
                return None
            agents = agent_manager._agents
            if not agents:
                return None
            agent = next(iter(agents.values()))
            return getattr(agent, "skill_store", None)
        except Exception as e:
            logger.warning("Could not get agent skill store: %s", e)
            return None

    async def _notify_skill_received(self, sender_node_id: str, skill_name: str) -> None:
        """Broadcast skill_received event to the UI."""
        try:
            local_api = getattr(self.service, "local_api", None)
            if local_api:
                await local_api.broadcast_event(
                    "skill_received",
                    {
                        "sender_node_id": sender_node_id,
                        "skill_name": skill_name,
                    },
                )
        except Exception as e:
            logger.debug("Could not notify UI of skill_received: %s", e)
