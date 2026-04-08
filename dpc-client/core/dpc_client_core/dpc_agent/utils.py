"""
DPC Agent — Shared utilities.

Adapted from Ouroboros utils.py for DPC Messenger integration.
Key changes:
- Added get_agent_root() and ensure_agent_dirs() for ~/.dpc/agents/{id}/ storage
- Removed Google Drive / Colab references
- No OpenRouter pricing functions (DPC handles pricing separately)

This module has zero dependencies on other dpc_agent modules.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import pathlib
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent Storage
# ---------------------------------------------------------------------------

def get_agent_root(agent_id: str) -> pathlib.Path:
    """
    Get the agent's storage root directory: ~/.dpc/agents/{agent_id}/

    All agent files (memory, logs, state, knowledge) are stored here.
    This is sandboxed to prevent the agent from accessing other DPC files.
    """
    agent_root = pathlib.Path.home() / ".dpc" / "agents" / agent_id
    agent_root.mkdir(parents=True, exist_ok=True)
    return agent_root


def get_agents_base_dir() -> pathlib.Path:
    """Get the base directory for all agents (~/.dpc/agents/)."""
    base_dir = pathlib.Path.home() / ".dpc" / "agents"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


def ensure_agent_dirs(agent_id: Optional[str] = None) -> None:
    """
    Ensure all agent subdirectories exist.

    Args:
        agent_id: Optional agent ID for per-agent storage
    """
    root = get_agent_root(agent_id)
    (root / "memory").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    (root / "state").mkdir(exist_ok=True)
    (root / "knowledge").mkdir(exist_ok=True)
    (root / "task_results").mkdir(exist_ok=True)
    (root / "logs" / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "skills").mkdir(exist_ok=True)


# Default .gitignore for agent sandbox repos
_AGENT_GITIGNORE = """\
# Task results (ephemeral outputs, not persistent knowledge)
task_results/
# Logs (too large, too frequent)
logs/
# Runtime state (changes every operation)
state/state.json
# Voice recordings (large binary files)
voice/
# Temporary files
*.tmp
temp_read.py
"""


def init_agent_git_repo(agent_id: str) -> None:
    """
    Initialize a local git repo in the agent sandbox.

    Sets up hooks-disabled config, writes .gitignore, and creates the
    initial commit. Called once during agent creation. Safe to call on
    an already-initialized repo (no-op).

    Args:
        agent_id: Agent identifier (used for git user config)
    """
    root = get_agent_root(agent_id)

    # No-op if already initialized
    if (root / ".git").exists():
        return

    # Shared empty hooks directory — disables all git hook execution
    # on both Unix and Windows (Git for Windows understands forward slashes)
    no_hooks_dir = get_agents_base_dir() / ".no_hooks"
    no_hooks_dir.mkdir(exist_ok=True)

    try:
        subprocess.run(["git", "init"], cwd=str(root), capture_output=True, timeout=30, check=False)

        # Disable hooks, set a stable local identity
        for cmd in [
            ["git", "config", "core.hooksPath", str(no_hooks_dir)],
            ["git", "config", "user.name", agent_id],
            ["git", "config", "user.email", f"{agent_id}@dpc-local"],
        ]:
            subprocess.run(cmd, cwd=str(root), capture_output=True, timeout=10, check=False)

        # Write .gitignore
        gitignore_path = root / ".gitignore"
        if not gitignore_path.exists():
            gitignore_path.write_text(_AGENT_GITIGNORE, encoding="utf-8")

        # Initial commit capturing all bootstrapped files
        subprocess.run(["git", "add", "-A"], cwd=str(root), capture_output=True, timeout=30, check=False)
        subprocess.run(
            ["git", "commit", "-m", "chore: initial agent state"],
            cwd=str(root), capture_output=True, timeout=30, check=False,
        )

        log.info(f"Initialized git repo for agent {agent_id}")

    except Exception as e:
        log.warning(f"Failed to initialize git repo for agent {agent_id}: {e}")


def auto_commit_agent_change(agent_root: pathlib.Path, message: str) -> None:
    """
    Best-effort auto-commit all changes in the agent sandbox.

    Only runs if a .git repo exists. Logs a warning on failure but never
    raises — callers must not depend on this succeeding.

    Args:
        agent_root: Agent sandbox root (e.g. ~/.dpc/agents/agent_001/)
        message: Commit message (use prefix convention: knowledge:, identity:, etc.)
    """
    if not (agent_root / ".git").exists():
        return

    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(agent_root), capture_output=True, timeout=30, check=False,
        )
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(agent_root), capture_output=True, text=True, timeout=30, check=False,
        )
        combined = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0 and "nothing to commit" not in combined.lower():
            log.warning(f"Auto-commit failed for {agent_root.name}: {result.stderr.strip()}")
    except Exception as e:
        log.warning(f"Auto-commit failed for {agent_root.name}: {e}")


# ---------------------------------------------------------------------------
# Agent Registry
# ---------------------------------------------------------------------------

class AgentRegistry:
    """
    Manages the agent registry stored in ~/.dpc/agents/_registry.json.

    The registry tracks all created agents with their metadata,
    provider selections, and permission profiles.
    """

    REGISTRY_VERSION = 1
    REGISTRY_FILE = "_registry.json"

    def __init__(self):
        self._registry_path = get_agents_base_dir() / self.REGISTRY_FILE
        self._registry = self._load_registry()

    def _load_registry(self) -> Dict[str, Any]:
        """Load registry from disk or create default."""
        if self._registry_path.exists():
            try:
                return json.loads(self._registry_path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning(f"Failed to load agent registry: {e}, creating new")

        return {
            "version": self.REGISTRY_VERSION,
            "agents": {}
        }

    def _save_registry(self) -> None:
        """Save registry to disk."""
        self._registry_path.parent.mkdir(parents=True, exist_ok=True)
        self._registry_path.write_text(
            json.dumps(self._registry, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    def list_agents(self) -> List[Dict[str, Any]]:
        """List all registered agents."""
        return list(self._registry.get("agents", {}).values())

    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get agent metadata by ID."""
        return self._registry.get("agents", {}).get(agent_id)

    def register_agent(
        self,
        agent_id: str,
        name: str,
        provider_alias: str = "dpc_agent",
        profile_name: str = "default",
        instruction_set_name: str = "general",
        telegram_chat_id: Optional[str] = None,
        telegram_enabled: bool = False,
    ) -> Dict[str, Any]:
        """
        Register a new agent.

        Args:
            agent_id: Unique agent identifier
            name: Human-readable agent name
            provider_alias: AI provider to use
            profile_name: Permission profile name
            instruction_set_name: Instruction set for the agent
            telegram_chat_id: Optional Telegram chat ID for linking
            telegram_enabled: Whether Telegram integration is enabled

        Returns:
            Agent metadata dict
        """
        # Validate telegram_chat_id format if provided
        if telegram_chat_id is not None:
            if not isinstance(telegram_chat_id, str):
                raise ValueError("telegram_chat_id must be a string")
            # Telegram chat IDs should be numeric (can be negative for groups)
            if not telegram_chat_id.lstrip('-').isdigit():
                raise ValueError("telegram_chat_id must be a numeric string")

        agent_meta = {
            "agent_id": agent_id,
            "name": name,
            "provider_alias": provider_alias,
            "profile_name": profile_name,
            "created_at": utc_now_iso(),
            "instruction_set_name": instruction_set_name,
            "telegram_enabled": telegram_enabled,
        }

        # Only add telegram_chat_id if provided
        if telegram_chat_id is not None:
            agent_meta["telegram_chat_id"] = telegram_chat_id
            agent_meta["telegram_linked_at"] = utc_now_iso()

        self._registry["agents"][agent_id] = agent_meta
        self._save_registry()
        log.info(f"Registered agent: {agent_id} ({name})")
        return agent_meta

    def unregister_agent(self, agent_id: str) -> bool:
        """
        Remove agent from registry.

        Args:
            agent_id: Agent to remove

        Returns:
            True if agent was removed, False if not found
        """
        if agent_id in self._registry.get("agents", {}):
            del self._registry["agents"][agent_id]
            self._save_registry()
            log.info(f"Unregistered agent: {agent_id}")
            return True
        return False

    def update_agent(self, agent_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Update agent metadata.

        Args:
            agent_id: Agent to update
            updates: Fields to update

        Returns:
            Updated agent metadata or None if not found
        """
        if agent_id not in self._registry.get("agents", {}):
            return None

        self._registry["agents"][agent_id].update(updates)
        self._registry["agents"][agent_id]["updated_at"] = utc_now_iso()
        self._save_registry()
        return self._registry["agents"][agent_id]

    def link_agent_to_telegram(
        self,
        agent_id: str,
        bot_token: str,
        chat_ids: List[str],
        event_filter: Optional[List[str]] = None,
        max_events_per_minute: int = 20,
        cooldown_seconds: float = 3.0,
        transcription_enabled: bool = True,
        unified_conversation: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Link an agent to Telegram with full configuration.

        Args:
            agent_id: Agent to link
            bot_token: Telegram bot token
            chat_ids: List of Telegram chat IDs (numeric strings)
            event_filter: List of event types to forward (None = default filter)
            max_events_per_minute: Maximum events to send per minute
            cooldown_seconds: Minimum time between same-type events
            transcription_enabled: Enable voice message transcription
            unified_conversation: When True, Telegram messages share conversation history
                                  with the DPC chat UI (use agent_id as conversation_id)

        Returns:
            Updated agent metadata or None if not found

        Raises:
            ValueError: If bot_token or chat_ids format is invalid
        """
        # Validate bot_token format
        if not isinstance(bot_token, str):
            raise ValueError("bot_token must be a string")
        if not bot_token:
            raise ValueError("bot_token cannot be empty")

        # Validate chat_ids format
        if not isinstance(chat_ids, list) or not chat_ids:
            raise ValueError("chat_ids must be a non-empty list")
        for chat_id in chat_ids:
            if not isinstance(chat_id, str):
                raise ValueError("chat_ids must contain only strings")
            if not chat_id.lstrip('-').isdigit():
                raise ValueError(f"chat_id '{chat_id}' must be a numeric string")

        # Validate event_filter if provided
        if event_filter is not None:
            if not isinstance(event_filter, list):
                raise ValueError("event_filter must be a list")
            # Validate event types are strings
            for event_type in event_filter:
                if not isinstance(event_type, str):
                    raise ValueError("event_filter must contain only strings")

        # Build Telegram config
        telegram_config = {
            "telegram_enabled": True,
            "telegram_bot_token": bot_token,
            "telegram_allowed_chat_ids": chat_ids,
            "telegram_max_events_per_minute": max_events_per_minute,
            "telegram_cooldown_seconds": cooldown_seconds,
            "telegram_transcription_enabled": transcription_enabled,
            "telegram_unified_conversation": unified_conversation,
            "telegram_linked_at": utc_now_iso()
        }

        # Add event_filter if provided, otherwise use default
        if event_filter is not None:
            telegram_config["telegram_event_filter"] = event_filter
        else:
            telegram_config["telegram_event_filter"] = self._default_telegram_event_filter()

        return self.update_agent(agent_id, telegram_config)

    def _default_telegram_event_filter(self) -> List[str]:
        """
        Get default Telegram event filter - important events only.

        Returns:
            List of event type names to forward to Telegram
        """
        return [
            # Tasks
            "task_started",
            "task_completed",
            "task_failed",
            # Evolution
            "evolution_cycle_completed",
            "code_modified",
            # Budget warnings
            "budget_warning",
            "rate_limit_hit",
            # Agent-initiated messages
            "agent_message",
        ]

    def unlink_agent_from_telegram(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """
        Remove Telegram linkage for an agent (removes all Telegram config).

        Args:
            agent_id: Agent to unlink

        Returns:
            Updated agent metadata or None if not found
        """
        agent = self.get_agent(agent_id)
        if not agent:
            return None

        # Set telegram_enabled to False and remove all telegram_* fields
        updates = {
            "telegram_enabled": False,
            "telegram_bot_token": None,
            "telegram_allowed_chat_ids": None,
            "telegram_event_filter": None,
            "telegram_max_events_per_minute": None,
            "telegram_cooldown_seconds": None,
            "telegram_transcription_enabled": None,
            "telegram_chat_id": None,  # Legacy field
            "telegram_linked_at": None,
        }

        return self.update_agent(agent_id, updates)

    def get_agent_linked_chat(self, agent_id: str) -> Optional[str]:
        """
        Get the telegram_chat_id for an agent.

        Args:
            agent_id: Agent to query

        Returns:
            Telegram chat ID or None if not linked
        """
        agent = self.get_agent(agent_id)
        if not agent:
            return None

        return agent.get("telegram_chat_id") if agent.get("telegram_enabled") else None

    def list_linked_agents(self) -> List[Dict[str, Any]]:
        """
        List all agents with Telegram links.

        Returns:
            List of agent metadata dicts with telegram_enabled=True
        """
        all_agents = self.list_agents()
        return [agent for agent in all_agents if agent.get("telegram_enabled", False)]


def create_name_slug(name: str, max_length: int = 20) -> str:
    """
    Create a filesystem-safe slug from an agent name.

    Args:
        name: Human-readable agent name
        max_length: Maximum slug length (default 20 chars)

    Returns:
        Lowercase slug with only alphanumeric and underscores.
        Example: "My Cool Agent!" -> "my_cool_agent"
    """
    # Convert to lowercase
    slug = name.lower()
    # Replace spaces and special chars with underscores
    slug = re.sub(r'[^a-z0-9]+', '_', slug)
    # Remove leading/trailing underscores
    slug = slug.strip('_')
    # Collapse multiple underscores
    slug = re.sub(r'_+', '_', slug)
    # Truncate to max length
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip('_')
    # Fallback if empty
    return slug or 'agent'


def generate_agent_id(name: str = "") -> str:
    """
    Generate a unique agent ID with an optional name slug.

    Args:
        name: Optional human-readable name to include in the ID

    Returns:
        Agent ID in format: agent_{name_slug}_{uuid_short}
        Example: agent_my_agent_abc12345
        Falls back to agent_{uuid_short} if no name provided
    """
    import uuid
    uuid_short = uuid.uuid4().hex[:8]

    if name:
        slug = create_name_slug(name)
        return f"agent_{slug}_{uuid_short}"
    else:
        return f"agent_{uuid_short}"


def migrate_global_telegram_to_agents() -> Dict[str, Any]:
    """
    Migrate global [dpc_agent_telegram] config to per-agent config.

    Reads from ~/.dpc/config.ini [dpc_agent_telegram] section
    and copies to each agent in _registry.json.

    Returns:
        Dict with migration status and details:
        {
            "status": "success" | "skipped" | "error",
            "migrated_count": int,
            "migrated_agents": List[str],
            "failed_count": int,
            "failed": List[Dict],
            "reason": str (if skipped)
        }
    """
    try:
        # Import Settings here to avoid circular import
        from ..settings import Settings
        from pathlib import Path

        settings = Settings(Path.home() / ".dpc")
        global_config = settings.get_dpc_agent_telegram_config()

        # Check if global config is enabled
        if not global_config.get("enabled", False):
            return {
                "status": "skipped",
                "reason": "Global Telegram config not enabled",
                "migrated_count": 0,
                "migrated_agents": [],
                "failed_count": 0,
                "failed": [],
            }

        # Get all agents from registry
        registry = AgentRegistry()
        agents = registry.list_agents()

        if not agents:
            return {
                "status": "skipped",
                "reason": "No agents found in registry",
                "migrated_count": 0,
                "migrated_agents": [],
                "failed_count": 0,
                "failed": [],
            }

        # Extract global config values
        bot_token = global_config.get("bot_token", "")
        allowed_chat_ids = global_config.get("allowed_chat_ids", [])
        event_filter = global_config.get("event_filter")
        max_events_per_minute = global_config.get("max_events_per_minute", 20)
        cooldown_seconds = global_config.get("cooldown_seconds", 3.0)
        transcription_enabled = global_config.get("transcription_enabled", True)

        # Validate required fields
        if not bot_token:
            return {
                "status": "error",
                "reason": "Global config missing bot_token",
                "migrated_count": 0,
                "migrated_agents": [],
                "failed_count": 0,
                "failed": [],
            }

        if not allowed_chat_ids:
            return {
                "status": "error",
                "reason": "Global config missing allowed_chat_ids",
                "migrated_count": 0,
                "migrated_agents": [],
                "failed_count": 0,
                "failed": [],
            }

        # Migrate each agent
        migrated = []
        failed = []

        for agent in agents:
            try:
                agent_id = agent["agent_id"]
                registry.link_agent_to_telegram(
                    agent_id=agent_id,
                    bot_token=bot_token,
                    chat_ids=allowed_chat_ids,
                    event_filter=event_filter,
                    max_events_per_minute=max_events_per_minute,
                    cooldown_seconds=cooldown_seconds,
                    transcription_enabled=transcription_enabled,
                )
                migrated.append(agent_id)
                log.info(f"Migrated Telegram config to agent: {agent_id}")
            except Exception as e:
                failed.append({
                    "agent_id": agent["agent_id"],
                    "error": str(e)
                })
                log.error(f"Failed to migrate Telegram config for agent {agent['agent_id']}: {e}")

        return {
            "status": "success",
            "migrated_count": len(migrated),
            "migrated_agents": migrated,
            "failed_count": len(failed),
            "failed": failed,
        }

    except Exception as e:
        log.error(f"Error migrating global Telegram config: {e}", exc_info=True)
        return {
            "status": "error",
            "reason": str(e),
            "migrated_count": 0,
            "migrated_agents": [],
            "failed_count": 0,
            "failed": [],
        }


def get_agent_config_path(agent_id: str) -> pathlib.Path:
    """Get the path to an agent's config.json file."""
    return get_agent_root(agent_id) / "config.json"


def load_agent_config(agent_id: str) -> Dict[str, Any]:
    """
    Load agent-specific configuration from ~/.dpc/agents/{agent_id}/config.json.

    Returns empty dict if config doesn't exist.
    """
    config_path = get_agent_config_path(agent_id)
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"Failed to load agent config for {agent_id}: {e}")
    return {}


def save_agent_config(agent_id: str, config: Dict[str, Any]) -> None:
    """
    Save agent configuration to ~/.dpc/agents/{agent_id}/config.json.
    """
    config_path = get_agent_config_path(agent_id)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    log.debug(f"Saved config for agent {agent_id}")


def create_agent_storage(
    agent_id: str,
    name: str,
    provider_alias: str = "dpc_agent",
    profile_name: str = "default",
    instruction_set_name: str = "general",
    budget_usd: float = 50.0,
    max_rounds: int = 200,
    telegram_chat_id: Optional[str] = None,
    telegram_enabled: bool = False,
    **extra_config
) -> Dict[str, Any]:
    """
    Create a new agent with isolated storage and configuration.

    Args:
        agent_id: Unique agent identifier
        name: Human-readable agent name
        provider_alias: AI provider to use
        profile_name: Permission profile name
        instruction_set_name: Instruction set for the agent
        budget_usd: Budget limit in USD
        max_rounds: Maximum LLM rounds per task
        telegram_chat_id: Optional Telegram chat ID for linking
        telegram_enabled: Whether Telegram integration is enabled
        **extra_config: Additional config fields

    Returns:
        Agent configuration dict
    """
    # Ensure agent directories exist
    ensure_agent_dirs(agent_id)

    # Initialize default memory files (scratchpad.md, identity.md, dialogue_summary.md, scratchpad_journal.jsonl)
    # This ensures all agents have the same default files from creation, not just on first use
    from .memory import Memory
    memory = Memory(agent_root=get_agent_root(agent_id))
    memory.ensure_files()

    # Bootstrap starter skills (5 strategy files in skills/)
    from .skill_store import SkillStore
    skill_store = SkillStore(agent_root=get_agent_root(agent_id))
    skill_store.ensure_starter_skills()

    # Create config
    # Note: git repo initialized AFTER all files are bootstrapped so the
    # initial commit captures the complete starting state.
    config = {
        "agent_id": agent_id,
        "name": name,
        "provider_alias": provider_alias,
        "profile_name": profile_name,
        "instruction_set_name": instruction_set_name,
        "created_at": utc_now_iso(),
        "budget_usd": budget_usd,
        "max_rounds": max_rounds,
        **extra_config
    }

    # Save config
    save_agent_config(agent_id, config)

    # Initialize git repo now that all files are in place
    init_agent_git_repo(agent_id)

    # Register in global registry with Telegram support
    registry = AgentRegistry()
    registry.register_agent(
        agent_id=agent_id,
        name=name,
        provider_alias=provider_alias,
        profile_name=profile_name,
        instruction_set_name=instruction_set_name,
        telegram_chat_id=telegram_chat_id,
        telegram_enabled=telegram_enabled,
    )

    log.info(f"Created agent storage for {agent_id} ({name})")
    if telegram_chat_id:
        log.info(f"Agent {agent_id} linked to Telegram chat {telegram_chat_id}")
    return config


def delete_agent_storage(agent_id: str) -> bool:
    """
    Delete an agent's storage folder and unregister it.

    Args:
        agent_id: Agent to delete

    Returns:
        True if deleted, False if not found
    """
    import shutil

    agent_path = get_agent_root(agent_id)

    if not agent_path.exists():
        return False

    # Don't allow deleting the default agent's folder
    if agent_id == "default":
        log.warning("Cannot delete default agent storage")
        return False

    try:
        shutil.rmtree(agent_path)

        # Unregister from registry
        registry = AgentRegistry()
        registry.unregister_agent(agent_id)

        log.info(f"Deleted agent storage for {agent_id}")
        return True
    except Exception as e:
        log.error(f"Failed to delete agent storage for {agent_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    """Return current UTC time in ISO 8601 format."""
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def sha256_text(s: str) -> str:
    """Return SHA256 hash of string."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def read_text(path: pathlib.Path) -> str:
    """Read text file with UTF-8 encoding."""
    return path.read_text(encoding="utf-8")


def write_text(path: pathlib.Path, content: str) -> None:
    """Write text file with UTF-8 encoding, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


_JSONL_MAX_BYTES = 5 * 1024 * 1024  # 5 MB rotation threshold


def append_jsonl(path: pathlib.Path, obj: Dict[str, Any]) -> None:
    """
    Append a JSON object as a line to a JSONL file (concurrent-safe).

    Uses file-based locking to prevent concurrent write collisions.
    Rotates when file exceeds 5 MB (renames to .1, starts fresh).
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Rotate if file exceeds size limit
    try:
        if path.exists() and path.stat().st_size > _JSONL_MAX_BYTES:
            rotated = path.with_suffix(path.suffix + ".1")
            if rotated.exists():
                rotated.unlink()
            path.rename(rotated)
    except Exception:
        log.debug("append_jsonl: rotation failed for %s", path, exc_info=True)

    line = json.dumps(obj, ensure_ascii=False)
    data = (line + "\n").encode("utf-8")

    lock_timeout_sec = 2.0
    lock_stale_sec = 10.0
    lock_sleep_sec = 0.01
    write_retries = 3
    retry_sleep_base_sec = 0.01

    path_hash = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    lock_path = path.parent / f".append_jsonl_{path_hash}.lock"
    lock_fd = None
    lock_acquired = False

    try:
        start = time.time()
        while time.time() - start < lock_timeout_sec:
            try:
                lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                lock_acquired = True
                break
            except FileExistsError:
                try:
                    stat = lock_path.stat()
                    if time.time() - stat.st_mtime > lock_stale_sec:
                        lock_path.unlink()
                        continue
                except Exception:
                    log.debug("Failed to read lock stat during lock acquisition retry", exc_info=True)
                time.sleep(lock_sleep_sec)
            except Exception:
                log.debug("Failed to acquire file lock for jsonl append", exc_info=True)
                break

        for attempt in range(write_retries):
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
                return
            except Exception:
                if attempt < write_retries - 1:
                    time.sleep(retry_sleep_base_sec * (2 ** attempt))

        # Fallback to standard file operations
        for attempt in range(write_retries):
            try:
                with path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                return
            except Exception:
                if attempt < write_retries - 1:
                    time.sleep(retry_sleep_base_sec * (2 ** attempt))

    except Exception:
        log.warning("append_jsonl: all write attempts failed for %s", path, exc_info=True)
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except Exception:
                log.debug("Failed to close lock fd after jsonl append", exc_info=True)
        if lock_acquired:
            try:
                lock_path.unlink()
            except Exception:
                log.debug("Failed to unlink lock file after jsonl append", exc_info=True)


# ---------------------------------------------------------------------------
# Path Safety (Sandbox Enforcement)
# ---------------------------------------------------------------------------

def safe_relpath(p: str) -> str:
    """
    Sanitize a relative path to prevent directory traversal.

    Raises ValueError if path contains ".." components.
    """
    p = p.replace("\\", "/").lstrip("/")
    if ".." in pathlib.PurePosixPath(p).parts:
        raise ValueError("Path traversal is not allowed.")
    return p


def is_path_in_sandbox(path: pathlib.Path, sandbox_root: pathlib.Path) -> bool:
    """
    Check if a path is within the sandbox root.

    Args:
        path: Path to check
        sandbox_root: The allowed root directory

    Returns:
        True if path is within sandbox, False otherwise
    """
    try:
        resolved_path = path.resolve()
        resolved_root = sandbox_root.resolve()
        return str(resolved_path).startswith(str(resolved_root))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Text Helpers
# ---------------------------------------------------------------------------

def truncate_for_log(s: str, max_chars: int = 4000) -> str:
    """Truncate string for logging, keeping start and end."""
    if len(s) <= max_chars:
        return s
    return s[: max_chars // 2] + "\n...\n" + s[-max_chars // 2:]


def clip_text(text: str, max_chars: int) -> str:
    """Clip text to max_chars, keeping start and end with truncation marker."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    half = max(200, max_chars // 2)
    return text[:half] + "\n...(truncated)...\n" + text[-half:]


def short(s: Any, n: int = 120) -> str:
    """Convert to string and truncate to n characters."""
    t = str(s or "")
    return t[:n] + "..." if len(t) > n else t


def estimate_tokens(text: str) -> int:
    """Rough token estimate using chars/4 heuristic."""
    return max(1, (len(str(text or "")) + 3) // 4)


# ---------------------------------------------------------------------------
# Subprocess
# ---------------------------------------------------------------------------

def run_cmd(cmd: List[str], cwd: Optional[pathlib.Path] = None, timeout: int = 30) -> str:
    """
    Run a command and return stdout.

    Raises RuntimeError if command fails.
    """
    res = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n\nSTDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}"
        )
    return res.stdout.strip()


# ---------------------------------------------------------------------------
# Git Helpers
# ---------------------------------------------------------------------------

def get_git_info(repo_dir: pathlib.Path) -> tuple[str, str]:
    """
    Best-effort retrieval of (git_branch, git_sha).

    Returns empty strings if not in a git repo or on error.
    """
    branch = ""
    sha = ""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=2,
        )
        if r.returncode == 0:
            branch = r.stdout.strip()
    except Exception:
        log.debug("Failed to get git branch", exc_info=True)

    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=2,
        )
        if r.returncode == 0:
            sha = r.stdout.strip()
    except Exception:
        log.debug("Failed to get git SHA", exc_info=True)

    return branch, sha


# ---------------------------------------------------------------------------
# Sanitization Helpers (for logging)
# ---------------------------------------------------------------------------

_SECRET_KEYS = frozenset([
    "token", "api_key", "apikey", "authorization", "secret", "password", "passwd", "passphrase",
])

# Patterns that indicate leaked secrets in tool output
_SECRET_PATTERNS = re.compile(
    r'ghp_[A-Za-z0-9]{30,}'       # GitHub personal access token
    r'|sk-ant-[A-Za-z0-9\-]{30,}' # Anthropic API key
    r'|sk-or-[A-Za-z0-9\-]{30,}'  # OpenRouter API key
    r'|gsk_[A-Za-z0-9]{30,}'      # Groq API key
    r'|sk-[A-Za-z0-9]{40,}'       # OpenAI API key
    r'|\b[0-9]{8,}:[A-Za-z0-9_\-]{30,}\b'  # Telegram bot token
)


def sanitize_task_for_event(
    task: Dict[str, Any], logs_dir: pathlib.Path, threshold: int = 4000,
) -> Dict[str, Any]:
    """
    Sanitize task dict for event logging.

    - Truncates large text
    - Strips base64 images
    - Persists full text to file if truncated
    """
    try:
        sanitized = task.copy()

        # Strip all keys ending with _base64 (images, etc.)
        keys_to_strip = [k for k in sanitized.keys() if k.endswith("_base64")]
        for key in keys_to_strip:
            value = sanitized.pop(key)
            sanitized[f"{key}_present"] = True
            if isinstance(value, str):
                sanitized[f"{key}_len"] = len(value)

        text = task.get("text")
        if not isinstance(text, str):
            return sanitized

        text_len = len(text)
        text_hash = sha256_text(text)
        sanitized["text_len"] = text_len
        sanitized["text_sha256"] = text_hash

        if text_len > threshold:
            sanitized["text"] = truncate_for_log(text, threshold)
            sanitized["text_truncated"] = True
            try:
                task_id = task.get("id")
                filename = f"task_{task_id}.txt" if task_id else f"task_{text_hash[:12]}.txt"
                full_path = logs_dir / "tasks" / filename
                write_text(full_path, text)
                sanitized["text_full_path"] = f"tasks/{filename}"
            except Exception:
                log.debug("Failed to persist full task text", exc_info=True)
        else:
            sanitized["text_truncated"] = False

        return sanitized
    except Exception:
        return task


def sanitize_tool_result_for_log(result: str) -> str:
    """Redact potential secrets from tool result before logging."""
    if not isinstance(result, str) or len(result) < 20:
        return result
    return _SECRET_PATTERNS.sub("***REDACTED***", result)


def sanitize_tool_args_for_log(
    fn_name: str, args: Dict[str, Any], threshold: int = 3000,
) -> Dict[str, Any]:
    """Sanitize tool arguments for logging: redact secrets, truncate large fields."""

    def _sanitize_value(key: str, value: Any, depth: int) -> Any:
        if depth > 3:
            return {"_depth_limit": True}
        if key.lower() in _SECRET_KEYS:
            return "*** REDACTED ***"
        if isinstance(value, str) and len(value) > threshold:
            return {
                key: truncate_for_log(value, threshold),
                f"{key}_len": len(value),
                f"{key}_sha256": sha256_text(value),
                f"{key}_truncated": True,
            }
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return {k: _sanitize_value(k, v, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            sanitized = [_sanitize_value(key, item, depth + 1) for item in value[:50]]
            if len(value) > 50:
                sanitized.append({"_truncated": f"... {len(value) - 50} more items"})
            return sanitized
        try:
            json.dumps(value, ensure_ascii=False)
            return value
        except (TypeError, ValueError):
            log.debug("Failed to JSON serialize value in sanitize_tool_args", exc_info=True)
            return {"_repr": repr(value)}

    try:
        return {k: _sanitize_value(k, v, 0) for k, v in args.items()}
    except Exception:
        log.debug("Failed to sanitize tool arguments for logging", exc_info=True)
        try:
            return json.loads(json.dumps(args, ensure_ascii=False, default=str))
        except Exception:
            log.debug("Tool argument sanitization failed completely", exc_info=True)
            return {"_error": "sanitization_failed"}
