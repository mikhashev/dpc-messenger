"""Handlers for P2P skill sharing commands (Phase 5a Memento-Skills)."""

from typing import Any, Dict, List, Optional

from . import MessageHandler


class SkillSearchHandler(MessageHandler):
    """Handles SKILL_SEARCH — peer searching our shareable skills."""

    @property
    def command_name(self) -> str:
        return "SKILL_SEARCH"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        request_id = payload.get("request_id", "")
        tags: List[str] = payload.get("tags") or []
        query: str = payload.get("query") or ""
        await self.service.skill_coordinator.handle_skill_search(
            sender_node_id,
            request_id=request_id,
            tags=tags if tags else None,
            query=query if query else None,
        )
        return None


class SkillsCatalogHandler(MessageHandler):
    """Handles SKILLS_CATALOG — peer's response to our SKILL_SEARCH."""

    @property
    def command_name(self) -> str:
        return "SKILLS_CATALOG"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        request_id = payload.get("request_id", "")
        skills = payload.get("skills") or []
        self.service.skill_coordinator.resolve_catalog(request_id, skills)
        return None


class SkillRequestHandler(MessageHandler):
    """Handles SKILL_REQUEST — peer requesting full content of one of our skills."""

    @property
    def command_name(self) -> str:
        return "SKILL_REQUEST"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        request_id = payload.get("request_id", "")
        skill_name = payload.get("skill_name", "")
        await self.service.skill_coordinator.handle_skill_request(
            sender_node_id,
            request_id=request_id,
            skill_name=skill_name,
        )
        return None


class SkillDataHandler(MessageHandler):
    """Handles SKILL_DATA — peer's response to our SKILL_REQUEST."""

    @property
    def command_name(self) -> str:
        return "SKILL_DATA"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        request_id = payload.get("request_id", "")
        skill_name = payload.get("skill_name", "")
        content: str = payload.get("content") or ""

        # Resolve the pending Future first (unblocks request_skill_from_peer caller)
        self.service.skill_coordinator.resolve_skill_data(request_id, content or None)

        # Also persist the skill if content was provided
        if content:
            status = await self.service.skill_coordinator.save_received_skill(
                sender_node_id, skill_name, content
            )
            self.logger.info("SKILL_DATA '%s' from %s: %s", skill_name, sender_node_id, status)
        return None


class SkillOfferHandler(MessageHandler):
    """Handles SKILL_OFFER — peer proactively pushing a skill to us."""

    @property
    def command_name(self) -> str:
        return "SKILL_OFFER"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        skill_name = payload.get("skill_name", "")
        content: str = payload.get("content") or ""

        if not skill_name or not content:
            self.logger.warning(
                "SKILL_OFFER from %s missing skill_name or content", sender_node_id
            )
            return None

        status = await self.service.skill_coordinator.save_received_skill(
            sender_node_id, skill_name, content
        )
        self.logger.info("SKILL_OFFER '%s' from %s: %s", skill_name, sender_node_id, status)
        return None
