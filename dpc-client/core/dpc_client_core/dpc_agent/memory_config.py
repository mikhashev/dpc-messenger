"""Memory provider config slot (ADR-010, MEM-3.11).

Per-agent memory configuration: embedding model, active recall toggle,
memory provider for episodic extraction.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class MemoryConfig:
    enabled: bool = False
    embedding_model: str = "aapot/bge-m3-onnx"
    active_recall: bool = True
    max_recall_results: int = 3
    memory_provider: Optional[str] = None
    auto_index: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryConfig":
        known = {k for k in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)


def get_memory_config(agent_config: dict) -> MemoryConfig:
    """Extract memory config from agent's config.json."""
    mem_data = agent_config.get("memory", {})
    return MemoryConfig.from_dict(mem_data)
