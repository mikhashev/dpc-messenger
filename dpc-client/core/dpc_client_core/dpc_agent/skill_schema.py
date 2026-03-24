"""
DPC Agent — Skill Schema.

Dataclass models for SKILL.md frontmatter parsing and validation.
Skills are structured markdown strategy files (not code) — they encode
*how to act* for a class of problems, complementing the knowledge system
that encodes *what to know*.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class SkillExecutionMode:
    KNOWLEDGE = "knowledge"   # Pure markdown — injected as context, LLM follows instructions
    PLAYBOOK = "playbook"     # Has scripts/ subdir — executed in sandbox (future)


@dataclass
class SkillProvenance:
    """Who created this skill and how it came to exist."""
    source: str = "local"          # local | peer | evolved | bootstrapped
    created_at: str = ""
    author_node_id: Optional[str] = None  # cryptographic identity of creator
    author_name: Optional[str] = None     # display name
    parent_skill: Optional[str] = None    # if evolved from another skill
    origin_peer: Optional[str] = None     # if received from a peer, their node_id

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SkillProvenance":
        if not d:
            return cls()
        return cls(
            source=d.get("source", "local"),
            created_at=str(d.get("created_at", "")),
            author_node_id=d.get("author_node_id") or None,
            author_name=d.get("author_name") or None,
            parent_skill=d.get("parent_skill") or None,
            origin_peer=d.get("origin_peer") or None,
        )


@dataclass
class SkillSharing:
    """P2P sharing controls — mirrors the personal context firewall model."""
    shareable: bool = False               # user explicitly opts in to sharing
    shared_with_nodes: List[str] = field(default_factory=list)   # specific node_ids
    shared_with_groups: List[str] = field(default_factory=list)  # groups from privacy_rules
    dht_announced: bool = False           # whether skill is announced to DHT network

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SkillSharing":
        if not d:
            return cls()
        return cls(
            shareable=bool(d.get("shareable", False)),
            shared_with_nodes=list(d.get("shared_with_nodes") or []),
            shared_with_groups=list(d.get("shared_with_groups") or []),
            dht_announced=bool(d.get("dht_announced", False)),
        )


@dataclass
class SkillMetadata:
    """Execution and capability metadata for the skill."""
    execution_mode: str = SkillExecutionMode.KNOWLEDGE
    required_tools: List[str] = field(default_factory=list)        # tools/core.py tools used
    required_permissions: List[str] = field(default_factory=list)  # firewall permission keys
    agent_profiles: List[str] = field(default_factory=lambda: ["default"])
    tags: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SkillMetadata":
        if not d:
            return cls()
        return cls(
            execution_mode=d.get("execution_mode", SkillExecutionMode.KNOWLEDGE),
            required_tools=list(d.get("required_tools") or []),
            required_permissions=list(d.get("required_permissions") or []),
            agent_profiles=list(d.get("agent_profiles") or ["default"]),
            tags=list(d.get("tags") or []),
        )


@dataclass
class SkillManifest:
    """
    Parsed SKILL.md frontmatter.

    The description field is the routing key — the LLM reads all skill
    descriptions and picks which skill to use by name.
    """
    name: str = ""
    version: int = 1
    description: str = ""
    provenance: SkillProvenance = field(default_factory=SkillProvenance)
    sharing: SkillSharing = field(default_factory=SkillSharing)
    metadata: SkillMetadata = field(default_factory=SkillMetadata)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SkillManifest":
        return cls(
            name=str(d.get("name", "")),
            version=int(d.get("version", 1)),
            description=str(d.get("description", "")).strip(),
            provenance=SkillProvenance.from_dict(d.get("provenance") or {}),
            sharing=SkillSharing.from_dict(d.get("sharing") or {}),
            metadata=SkillMetadata.from_dict(d.get("metadata") or {}),
        )
