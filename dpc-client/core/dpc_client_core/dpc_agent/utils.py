"""
DPC Agent — Shared utilities.

Adapted from Ouroboros utils.py for DPC Messenger integration.
Key changes:
- Added get_agent_root() and ensure_agent_dirs() for ~/.dpc/agent/ storage
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

def get_agent_root(agent_id: Optional[str] = None) -> pathlib.Path:
    """
    Get the agent's storage root directory.

    Args:
        agent_id: Optional agent ID. If provided, returns path to ~/.dpc/agents/{agent_id}/
                  If None, returns path to ~/.dpc/agent/ (legacy/default location)

    All agent files (memory, logs, state, knowledge) are stored here.
    This is sandboxed to prevent the agent from accessing other DPC files.
    """
    if agent_id:
        # Per-agent isolation: ~/.dpc/agents/{agent_id}/
        agent_root = pathlib.Path.home() / ".dpc" / "agents" / agent_id
    else:
        # Legacy/default location: ~/.dpc/agent/
        agent_root = pathlib.Path.home() / ".dpc" / "agent"
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
    ) -> Dict[str, Any]:
        """
        Register a new agent.

        Args:
            agent_id: Unique agent identifier
            name: Human-readable agent name
            provider_alias: AI provider to use
            profile_name: Permission profile name
            instruction_set_name: Instruction set for the agent

        Returns:
            Agent metadata dict
        """
        agent_meta = {
            "agent_id": agent_id,
            "name": name,
            "provider_alias": provider_alias,
            "profile_name": profile_name,
            "created_at": utc_now_iso(),
            "instruction_set_name": instruction_set_name,
        }
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


def generate_agent_id() -> str:
    """Generate a unique agent ID."""
    import uuid
    return f"agent_{uuid.uuid4().hex[:12]}"


def migrate_legacy_agent() -> bool:
    """
    Migrate legacy ~/.dpc/agent/ folder to ~/.dpc/agents/default/.

    Returns:
        True if migration was performed, False if not needed
    """
    legacy_path = pathlib.Path.home() / ".dpc" / "agent"
    new_path = get_agent_root("default")

    # Check if migration is needed
    if not legacy_path.exists():
        return False

    # Check if already migrated
    if new_path.exists():
        log.debug("Legacy agent already migrated")
        return False

    # Perform migration
    try:
        import shutil
        shutil.move(str(legacy_path), str(new_path))
        log.info(f"Migrated legacy agent from {legacy_path} to {new_path}")

        # Register default agent in registry
        registry = AgentRegistry()
        if not registry.get_agent("default"):
            registry.register_agent(
                agent_id="default",
                name="Default Agent",
                provider_alias="dpc_agent",
                profile_name="default",
            )

        return True
    except Exception as e:
        log.error(f"Failed to migrate legacy agent: {e}")
        return False


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
        **extra_config: Additional config fields

    Returns:
        Agent configuration dict
    """
    # Ensure agent directories exist
    ensure_agent_dirs(agent_id)

    # Create config
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

    # Register in global registry
    registry = AgentRegistry()
    registry.register_agent(
        agent_id=agent_id,
        name=name,
        provider_alias=provider_alias,
        profile_name=profile_name,
        instruction_set_name=instruction_set_name,
    )

    log.info(f"Created agent storage for {agent_id} ({name})")
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


def append_jsonl(path: pathlib.Path, obj: Dict[str, Any]) -> None:
    """
    Append a JSON object as a line to a JSONL file (concurrent-safe).

    Uses file-based locking to prevent concurrent write collisions.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
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
