# dpc-client/core/dpc_client_core/firewall.py

import json
import logging
import os
from pathlib import Path
from typing import List, Dict, Tuple, Any, Optional, Set
import fnmatch
from copy import deepcopy

from dpc_protocol.pcm_core import PersonalContext  # For wildcard matching

logger = logging.getLogger(__name__)

# Suffixes of keys that live inside a `tools` block but are NOT tools: they are
# per-tool settings named `<tool>_<setting>`. `_group_allowed` is a convention
# open to every tool (agent.py builds the key from the tool name), so matching a
# fixed pair of names would silently mis-classify `browse_page_group_allowed`
# and friends — and the prune below would then delete them as unknown tools.
#
# Every place that walks a tools dict has to skip these: before this lived in
# one place the validator skipped two of them by name while the tools map
# coerced them to booleans, so the same key was metadata in one reader and a
# pseudo-tool in another.
TOOL_SETTING_SUFFIXES = ('_group_allowed', '_tier1_whitelist')

# Where those settings live once migrated out of `tools`:
# tool_settings: {"run_shell": {"group_allowed": bool, "tier1_whitelist": [...]}}
TOOL_SETTINGS_KEY = 'tool_settings'


def _is_tool_key(name: str) -> bool:
    """True for keys in a `tools` block that name an actual tool."""
    return not name.startswith('_') and not name.endswith(TOOL_SETTING_SUFFIXES)


# Tool names that are no longer registered but are still accepted in config:
# older keys that the loader maps onto the tools that replaced them.
LEGACY_TOOL_ALIASES = frozenset({
    'repo_read', 'repo_write_commit', 'drive_read', 'drive_write',
    'extended_path_read', 'extended_path_write',
    'repo_list', 'drive_list', 'extended_path_list',
})

_known_tool_names_cache: Optional[Set[str]] = None


def _known_tool_names() -> Optional[Set[str]]:
    """Tool names the validator will recognise, straight from the registry.

    This used to be a hand-maintained set in the validator — a third copy
    of "which tools exist", after ToolEntry and the config itself. It fell
    behind: on the live config it called 24 registered tools "unknown …
    may be from older config" (every browser_*, every comfyui_*, the
    session-archive readers), 244 warnings in one startup, advising the
    reader to treat live permissions as leftovers.

    Returns None when the registry cannot be trusted — unreadable, or a
    module failed to import — because then "not in the registry" says
    nothing about the name and the warning would be noise again.
    """
    global _known_tool_names_cache
    if _known_tool_names_cache is not None:
        return _known_tool_names_cache
    try:
        from .dpc_agent.tools.registry import ToolRegistry
        registry = ToolRegistry()
        if registry.load_failures:
            return None
        _known_tool_names_cache = set(registry._entries) | set(LEGACY_TOOL_ALIASES)
        return _known_tool_names_cache
    except Exception as e:
        logger.error("Cannot read tool registry for validation: %s", e, exc_info=True)
        return None


def _split_tool_setting(name: str) -> Optional[Tuple[str, str]]:
    """`run_shell_group_allowed` -> ('run_shell', 'group_allowed')."""
    for suffix in TOOL_SETTING_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)], suffix.lstrip('_')
    return None


class ContextFirewall:
    """
    Parses and evaluates privacy_rules.json to control access to context data.
    """
    def __init__(self, access_file_path: Path):
        self.access_file_path = access_file_path
        self._ensure_file_exists()
        self._load_rules()

    def _load_rules(self):
        """Load and parse rules from JSON file."""
        try:
            rules_text = self.access_file_path.read_text()
            self.rules: Dict[str, Any] = json.loads(rules_text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in firewall rules: {e}")
        except Exception as e:
            raise ValueError(f"Failed to load firewall rules: {e}")

        # Parse file groups (aliases for groups of files) — skip _comment keys
        self.file_groups: Dict[str, List[str]] = {
            k: v for k, v in self.rules.get('file_groups', {}).items()
            if not k.startswith('_') and isinstance(v, list)
        }

        # Parse node groups (which nodes belong to which groups) — skip _comment keys
        self.node_groups: Dict[str, List[str]] = {
            k: v for k, v in self.rules.get('node_groups', {}).items()
            if not k.startswith('_') and isinstance(v, list)
        }

        # Parse compute sharing settings
        self._parse_compute_settings()

        # Parse transcription sharing settings
        self._parse_transcription_settings()

        # Parse notification settings
        self._parse_notification_settings()

        # Parse DPC agent settings
        self._parse_dpc_agent_settings()

    def _parse_compute_settings(self):
        """Parse compute sharing settings from the config."""
        compute = self.rules.get('compute', {})
        self.compute_enabled = compute.get('enabled', False)
        self.compute_allowed_nodes: List[str] = compute.get('allow_nodes', [])
        self.compute_allowed_groups: List[str] = compute.get('allow_groups', [])
        self.compute_allowed_models: List[str] = compute.get('allowed_models', [])
        # The one alias this node serves peers from. The peer used to name the
        # provider itself, which meant a peer could point us at a paid alias;
        # the host designates it instead. Unset means we serve nobody — the
        # opposite of `allowed_models`, where empty means all, so it is said out
        # loud here rather than left to be discovered from behaviour.
        self.compute_serving_alias: Optional[str] = compute.get('serving_alias') or None
        logger.debug("Compute sharing settings updated: enabled=%s, allowed_nodes=%d, allowed_groups=%d, allowed_models=%d, serving_alias=%s",
                     self.compute_enabled, len(self.compute_allowed_nodes),
                     len(self.compute_allowed_groups), len(self.compute_allowed_models),
                     self.compute_serving_alias)
        if self.compute_enabled and not self.compute_serving_alias:
            logger.warning(
                "Compute sharing is enabled but no compute.serving_alias is set — "
                "peer inference requests will be refused. Name the alias this node "
                "should serve peers from in privacy_rules.json under compute.serving_alias."
            )

    def _parse_transcription_settings(self):
        """Parse transcription sharing settings from the config."""
        transcription = self.rules.get('transcription', {})
        self.transcription_enabled = transcription.get('enabled', False)
        self.transcription_allowed_nodes: List[str] = transcription.get('allow_nodes', [])
        self.transcription_allowed_groups: List[str] = transcription.get('allow_groups', [])
        self.transcription_allowed_models: List[str] = transcription.get('allowed_models', [])
        logger.debug("Transcription sharing settings updated: enabled=%s, allowed_nodes=%d, allowed_groups=%d, allowed_models=%d",
                     self.transcription_enabled, len(self.transcription_allowed_nodes),
                     len(self.transcription_allowed_groups), len(self.transcription_allowed_models))

    def _parse_notification_settings(self):
        """Parse notification settings from the config."""
        notifications = self.rules.get('notifications', {})
        self.notifications_enabled = notifications.get('enabled', True)
        self.notification_events: Dict[str, bool] = notifications.get('events', {
            'new_message': True,
            'file_offer': True,
            'file_complete': True,
            'file_cancelled': True,
            'knowledge_proposal': True,
            'knowledge_result': True,
            'session_proposal': True,
            'session_result': True,
            'connection_status': False
        })
        logger.debug("Notification settings updated: enabled=%s, events=%s",
                     self.notifications_enabled, self.notification_events)

    def _get_registered_tool_defaults(self) -> Tuple[Dict[str, bool], int]:
        """Read canonical tool defaults from the ToolEntry registry.

        Single source of truth for `default_enabled` per tool: each
        ToolEntry declares it explicitly at registration. Replaces the
        old hardcoded `all_tools_defaults` dict that used to drift from
        the actual registered tools (e.g. popup_* tools added without
        firewall defaults — see AGENT-TOOL-FIREWALL-DEFAULT-DRIFT).

        Returns (defaults, load_failures) — the second value is the
        number of tool modules that did not import. It travels with the
        defaults rather than on `self` so the prune below cannot be
        called without it: adding keys is safe while that number is
        unknown-but-nonzero, removing them is not.
        """
        try:
            from .dpc_agent.tools.registry import ToolRegistry
            registry = ToolRegistry()
            defaults = {
                entry.name: entry.default_enabled
                for entry in registry._entries.values()
            }
            return defaults, len(registry.load_failures)
        except Exception as e:
            logger.error("Failed to load ToolRegistry defaults: %s", e, exc_info=True)
            # Registry unreadable: report a failure so nothing gets pruned.
            return {}, 1

    def _migrate_tool_settings_out_of_tools(self) -> bool:
        """Move `<tool>_<setting>` keys from `tools` into `tool_settings`.

        `tools` is a map of tool name -> allowed. Settings of a tool were
        stored in the same dict under a compound name, so every reader had
        to know which names were not tools — and they disagreed. After this
        migration a `tools` block contains tool names and nothing else.

        Existing values are moved, never rewritten; a value already present
        at the destination wins and the stale copy is dropped. Returns True
        iff self.rules was modified.
        """
        modified = False

        def migrate_block(container: Dict[str, Any], where: str) -> None:
            nonlocal modified
            tools = container.get('tools')
            if not isinstance(tools, dict):
                return
            for name in [k for k in tools if not k.startswith('_') and not _is_tool_key(k)]:
                split = _split_tool_setting(name)
                if split is None:
                    continue
                tool, setting = split
                value = tools.pop(name)
                settings = container.setdefault(TOOL_SETTINGS_KEY, {}).setdefault(tool, {})
                if setting not in settings:
                    settings[setting] = value
                modified = True
                logger.info("Moved %s.%s -> %s.%s.%s.%s",
                            where, name, where, TOOL_SETTINGS_KEY, tool, setting)

        dpc_agent = self.rules.get('dpc_agent')
        if isinstance(dpc_agent, dict):
            migrate_block(dpc_agent, 'dpc_agent')

        for profile_name, profile in self.rules.get('agent_profiles', {}).items():
            if isinstance(profile, dict):
                migrate_block(profile, f'agent_profiles.{profile_name}')

        return modified

    def get_tool_setting(self, tool_name: str, setting: str,
                         profile_name: Optional[str] = None, default=None,
                         inherit_global: bool = False):
        """Read a per-tool setting (`group_allowed`, `tier1_whitelist`).

        `inherit_global` is **off** by default, and deliberately so: both
        current readers (the group-chat gate on run_shell and the Tier 1
        shell whitelist) have always looked at the agent's own profile and
        nowhere else. Turning inheritance on here would hand every agent
        without a profile the global `run_shell_group_allowed: true` — a
        loosening of the shell gate, not a refactor. Whether these settings
        *should* inherit is a decision, and it is not this migration's.

        Falls back to the pre-migration location (`tools.<tool>_<setting>`)
        so a hand-edited or externally restored file still answers correctly.
        """
        compound = f"{tool_name}_{setting}"

        def read(container: Optional[Dict[str, Any]]):
            if not isinstance(container, dict):
                return None
            settings = container.get(TOOL_SETTINGS_KEY, {})
            if isinstance(settings, dict):
                per_tool = settings.get(tool_name, {})
                if isinstance(per_tool, dict) and setting in per_tool:
                    return per_tool[setting]
            tools = container.get('tools', {})
            if isinstance(tools, dict) and compound in tools:
                return tools[compound]
            return None

        if profile_name:
            value = read(self.rules.get('agent_profiles', {}).get(profile_name))
            if value is not None:
                return value
        if not inherit_global:
            return default
        value = read(self.rules.get('dpc_agent'))
        return default if value is None else value

    def _prune_dead_tool_keys(self, registry_defaults: Dict[str, bool],
                              load_failures: int) -> bool:
        """Drop keys from every `tools` block that name no registered tool.

        Counterpart to seeding: that one only ever adds, so names that
        left the code stayed in privacy_rules.json forever
        (`claude_code_edit`, `repo_commit_push`, `extract_links`,
        `transcribe_audio` — the last one a WebSocket command that was
        never an agent tool at all).

        Refuses to run when any tool module failed to import: in that
        state "absent from the registry" means "we could not see it",
        and pruning would silently delete the user's settings for real
        tools. `load_failures` is a required argument rather than state
        on `self` on purpose — a caller cannot reach the deleting branch
        without saying how complete its picture is. Comments
        (`_`-prefixed) and per-tool settings are kept.

        Returns True iff self.rules was modified.
        """
        if not registry_defaults:
            return False
        if load_failures:
            logger.warning(
                "Skipping dead tool key prune: %d tool module(s) failed to load, "
                "so the registry is not a complete list of existing tools",
                load_failures,
            )
            return False

        modified = False

        def prune_block(tools: Dict[str, Any], where: str) -> None:
            nonlocal modified
            for name in [k for k in tools if _is_tool_key(k) and k not in registry_defaults]:
                del tools[name]
                modified = True
                logger.info("Removed dead tool key from %s: %s", where, name)

        dpc_agent = self.rules.get('dpc_agent', {})
        if isinstance(dpc_agent.get('tools'), dict):
            prune_block(dpc_agent['tools'], 'dpc_agent.tools')

        for profile_name, profile in self.rules.get('agent_profiles', {}).items():
            if isinstance(profile, dict) and isinstance(profile.get('tools'), dict):
                prune_block(profile['tools'], f'agent_profiles.{profile_name}.tools')

        return modified

    def _seed_missing_tools_into_rules(
        self, registry_defaults: Optional[Dict[str, bool]] = None
    ) -> bool:
        """Auto-add tools from ToolRegistry that are absent from privacy_rules.

        Walks the global `dpc_agent.tools` block and every
        `agent_profiles.<id>.tools` override; for any tool present in
        the registry but missing from that dict, injects the canonical
        default from ToolEntry.default_enabled. Existing user values
        are NEVER touched — only the *missing* keys are seeded.

        Per-agent profiles are only seeded when they already define a
        `tools` block (the user has opted into per-agent overrides);
        no profile or tools block is auto-created.

        Returns True iff self.rules was modified, signalling the caller
        to persist the file. Drives the closing fix for the dual-source
        drift (old code maintained two dicts in this file that fell out
        of sync; now only the registry decides which keys exist).
        """
        if registry_defaults is None:
            registry_defaults, _ = self._get_registered_tool_defaults()
        if not registry_defaults:
            return False
        modified = False

        dpc_agent = self.rules.setdefault('dpc_agent', {})
        tools = dpc_agent.setdefault('tools', {})
        for name, default in registry_defaults.items():
            if name not in tools:
                tools[name] = default
                modified = True
                logger.info(
                    "Seeded missing tool default into dpc_agent.tools: %s=%s",
                    name, default,
                )

        for profile_name, profile in self.rules.get('agent_profiles', {}).items():
            if not isinstance(profile, dict):
                continue
            profile_tools = profile.get('tools')
            if profile_tools is None:
                continue
            for name, default in registry_defaults.items():
                if name not in profile_tools:
                    profile_tools[name] = default
                    modified = True
                    logger.info(
                        "Seeded missing tool default into agent_profiles.%s.tools: %s=%s",
                        profile_name, name, default,
                    )

        return modified

    def _parse_dpc_agent_settings(self):
        """Parse DPC agent settings from the config."""
        # First, reconcile the stored tools blocks with the registry in both
        # directions: seed tools that landed in ToolRegistry after this
        # privacy_rules.json was created, and drop keys naming tools that no
        # longer exist. Single source of truth: ToolEntry.default_enabled.
        # One registry load feeds both, so the prune sees the same picture —
        # including whether any module failed to import.
        registry_defaults, load_failures = self._get_registered_tool_defaults()
        changed = self._migrate_tool_settings_out_of_tools()
        changed = self._seed_missing_tools_into_rules(registry_defaults) or changed
        changed = self._prune_dead_tool_keys(registry_defaults, load_failures) or changed
        if changed:
            try:
                self.access_file_path.write_text(json.dumps(self.rules, indent=2))
                logger.info("Persisted reconciled tool keys to %s", self.access_file_path)
            except Exception as e:
                logger.error("Failed to persist reconciled tool keys: %s", e, exc_info=True)

        dpc_agent = self.rules.get('dpc_agent', {})
        self.dpc_agent_enabled = dpc_agent.get('enabled', True)
        self.dpc_agent_personal_context_access = dpc_agent.get('personal_context_access', False)
        self.dpc_agent_device_context_access = dpc_agent.get('device_context_access', False)
        self.dpc_agent_human_knowledge_access = dpc_agent.get('human_knowledge_access', True)

        # Parse tool permissions from config — defaults come from the
        # registry (ToolEntry.default_enabled) and were seeded above.
        # Legacy tool name mapping (S31: 6 tools merged into read_file/write_file; S149: 3 list tools merged into list_dir)
        _legacy_tool_map = {
            'read_file': ['repo_read', 'extended_path_read', 'drive_read'],
            'write_file': ['repo_write_commit', 'extended_path_write', 'drive_write'],
            'list_dir': ['repo_list', 'extended_path_list', 'drive_list'],
        }
        registry_defaults, _ = self._get_registered_tool_defaults()
        tools = dpc_agent.get('tools', {})
        self.dpc_agent_tools: Dict[str, bool] = {}
        for tool_name, default_enabled in registry_defaults.items():
            if tool_name in tools:
                self.dpc_agent_tools[tool_name] = tools.get(tool_name, default_enabled)
            elif tool_name in _legacy_tool_map:
                # Check if any legacy name is present in config
                legacy_val = None
                for legacy_name in _legacy_tool_map[tool_name]:
                    if legacy_name in tools:
                        legacy_val = tools[legacy_name]
                        break
                self.dpc_agent_tools[tool_name] = legacy_val if legacy_val is not None else default_enabled
            else:
                self.dpc_agent_tools[tool_name] = default_enabled

        # Parse sandbox extensions (v0.16.0+ - custom paths outside default sandbox)
        sandbox_extensions = dpc_agent.get('sandbox_extensions', {})
        self.sandbox_read_only_paths: List[str] = sandbox_extensions.get('read_only', [])
        self.sandbox_read_write_paths: List[str] = sandbox_extensions.get('read_write', [])
        # Extended path access gates (S31 UI checkboxes) — per-agent resolution
        # lives in get_extended_{read,write}_enabled(profile_name). Flat attributes
        # were removed in S110 (FIREWALL-EXT-WRITE-PROFILE) to prevent accidental
        # reads that bypass the per-agent profile.

        # Validate and normalize paths
        self.sandbox_read_only_paths = [self._normalize_path(p) for p in self.sandbox_read_only_paths if p]
        self.sandbox_read_write_paths = [self._normalize_path(p) for p in self.sandbox_read_write_paths if p]

        # Parse history settings (v0.22.0+)
        history = dpc_agent.get('history', {})
        self.history_preserve_on_reset = history.get('preserve_on_reset', True)
        self.history_max_archived_sessions = max(0, int(history.get('max_archived_sessions', 0)))

        logger.debug("DPC Agent settings updated: enabled=%s, personal=%s, device=%s, knowledge=%s, tools_count=%d, sandbox_extensions=%d",
                     self.dpc_agent_enabled,
                     self.dpc_agent_personal_context_access,
                     self.dpc_agent_device_context_access,
                     self.dpc_agent_human_knowledge_access,
                     len([t for t in self.dpc_agent_tools.values() if t]),
                     len(self.sandbox_read_only_paths) + len(self.sandbox_read_write_paths))

    def _normalize_path(self, path_str: str) -> str:
        """Normalize a path string for comparison."""
        try:
            p = Path(path_str).expanduser().resolve()
            return str(p)
        except Exception:
            logger.warning(f"Invalid path in sandbox_extensions: {path_str}")
            return ""

    def _get_profile_or_global(self, profile_name: Optional[str], *keys, default=None):
        """
        Read a value from a per-agent profile if present, else from the global dpc_agent section.

        Args:
            profile_name: Agent profile key (typically agent_id), or None for global-only
            *keys: Sequence of dict keys to traverse (e.g. 'history', 'preserve_on_reset')
            default: Value returned when key is absent everywhere
        """
        # Try per-agent profile first
        if profile_name:
            profile = self.rules.get('agent_profiles', {}).get(profile_name)
            if profile is not None:
                val = profile
                for k in keys:
                    if isinstance(val, dict):
                        val = val.get(k)
                    else:
                        val = None
                        break
                if val is not None:
                    return val
        # Fall back to global dpc_agent
        val = self.rules.get('dpc_agent', {})
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
        return val if val is not None else default

    def get_history_settings(self, profile_name: Optional[str] = None) -> tuple:
        """Return (preserve_on_reset, max_archived_sessions) for an agent profile,
        falling back to global dpc_agent.history when the profile has no override.

        Profile-name that does not match any known agent falls back cleanly to
        global — safe to pass peer/group conversation ids.
        """
        preserve = self._get_profile_or_global(
            profile_name, 'history', 'preserve_on_reset',
            default=self.history_preserve_on_reset)
        max_sessions = self._get_profile_or_global(
            profile_name, 'history', 'max_archived_sessions',
            default=self.history_max_archived_sessions)
        return bool(preserve), max(0, int(max_sessions))

    def get_extended_write_enabled(self, profile_name: Optional[str] = None) -> bool:
        """Per-agent extended path write gate (sandbox_extensions.extended_write_enabled).

        Mirrors S104 isolation pattern: per-agent profile takes precedence,
        falls back to global dpc_agent block, defaults to False.
        """
        val = self._get_profile_or_global(
            profile_name, 'sandbox_extensions', 'extended_write_enabled', default=False
        )
        return bool(val)

    def get_extended_read_enabled(self, profile_name: Optional[str] = None) -> bool:
        """Per-agent extended path read gate. Defaults to True (read is permissive)."""
        val = self._get_profile_or_global(
            profile_name, 'sandbox_extensions', 'extended_read_enabled', default=True
        )
        return bool(val)

    def get_agent_enabled(self, profile_name: Optional[str] = None) -> bool:
        """Per-agent enabled flag with global fallback.

        Mirrors S110 isolation pattern: per-agent profile takes precedence,
        falls back to global dpc_agent block.
        """
        val = self._get_profile_or_global(
            profile_name, 'enabled', default=True
        )
        return bool(val)

    def get_sandbox_read_only_paths(self, profile_name: Optional[str] = None) -> List[str]:
        """Per-agent sandbox read-only paths with global fallback.

        Per-agent profile sandbox_extensions.read_only takes precedence, falls back
        to the parsed global self.sandbox_read_only_paths.
        """
        val = self._get_profile_or_global(
            profile_name, 'sandbox_extensions', 'read_only', default=None
        )
        if val is None:
            return list(self.sandbox_read_only_paths)
        return [self._normalize_path(p) for p in val if p]

    def get_sandbox_read_write_paths(self, profile_name: Optional[str] = None) -> List[str]:
        """Per-agent sandbox read-write paths with global fallback."""
        val = self._get_profile_or_global(
            profile_name, 'sandbox_extensions', 'read_write', default=None
        )
        if val is None:
            return list(self.sandbox_read_write_paths)
        return [self._normalize_path(p) for p in val if p]

    def get_agent_tools_map(self, profile_name: Optional[str] = None) -> Dict[str, bool]:
        """Per-agent tools map with profile overrides merged onto global defaults.

        Returns the full Dict[str, bool] of tool -> enabled (unlike
        get_allowed_agent_tools_for_profile which returns the Set of enabled
        names). Used for transparency / capability sections that need to show
        the full set of tools and their state, not just the enabled ones.
        """
        merged = dict(self.dpc_agent_tools)
        if not profile_name:
            return merged
        profile = self.get_agent_profile_settings(profile_name)
        if not profile:
            return merged
        profile_tools = profile.get('tools', {})
        for tool_name, enabled in profile_tools.items():
            if _is_tool_key(tool_name):
                merged[tool_name] = bool(enabled)
        return merged

    def is_extended_path_allowed(self, path: str, require_write: bool = False,
                                 profile_name: Optional[str] = None) -> bool:
        """
        Check if a path is in the extended sandbox (outside ~/.dpc/agent/).

        Args:
            path: Path to check
            require_write: If True, check for write access; if False, read access is sufficient
            profile_name: Per-agent profile key; when set, per-agent sandbox_extensions are used

        Returns:
            True if the path is allowed for the requested access level
        """
        if not self.dpc_agent_enabled:
            return False

        try:
            normalized = str(Path(path).expanduser().resolve())
        except Exception:
            return False

        # Resolve sandbox paths: per-agent profile overrides global
        if profile_name:
            sandbox = self._get_profile_or_global(profile_name, 'sandbox_extensions', default={})
            rw_paths = [self._normalize_path(p) for p in sandbox.get('read_write', []) if p]
            ro_paths = [self._normalize_path(p) for p in sandbox.get('read_only', []) if p]
        else:
            rw_paths = self.sandbox_read_write_paths
            ro_paths = self.sandbox_read_only_paths

        # Check read_write paths first (they also allow read)
        for allowed_path in rw_paths:
            prefix = allowed_path.rstrip(os.sep).rstrip("/")
            if allowed_path and (
                normalized == allowed_path
                or normalized.startswith(prefix + os.sep)
                or normalized.startswith(prefix + "/")
            ):
                return True

        # If write is required, read_only paths are not sufficient
        if require_write:
            return False

        # Check read_only paths
        for allowed_path in ro_paths:
            prefix = allowed_path.rstrip(os.sep).rstrip("/")
            if allowed_path and (
                normalized == allowed_path
                or normalized.startswith(prefix + os.sep)
                or normalized.startswith(prefix + "/")
            ):
                return True

        return False

    def get_extended_paths(self, profile_name: Optional[str] = None) -> Dict[str, List[str]]:
        """Get all extended sandbox paths, optionally scoped to a per-agent profile."""
        if profile_name:
            sandbox = self._get_profile_or_global(profile_name, 'sandbox_extensions', default={})
            return {
                'read_only': [self._normalize_path(p) for p in sandbox.get('read_only', []) if p],
                'read_write': [self._normalize_path(p) for p in sandbox.get('read_write', []) if p],
            }
        return {
            'read_only': self.sandbox_read_only_paths,
            'read_write': self.sandbox_read_write_paths,
        }

    def can_agent_access_context(self, context_type: str,
                                  profile_name: Optional[str] = None) -> bool:
        """
        Check if the DPC agent can access a specific context type.

        Args:
            context_type: Type of context ('personal', 'device', 'knowledge')
            profile_name: Per-agent profile key; when set, per-agent settings override global

        Returns:
            True if agent can access this context type
        """
        if not self.dpc_agent_enabled:
            return False

        if profile_name is None:
            logger.warning("can_agent_access_context('%s') called without profile_name — using global config", context_type)

        if context_type == 'personal':
            return bool(self._get_profile_or_global(
                profile_name, 'personal_context_access',
                default=self.dpc_agent_personal_context_access))
        elif context_type == 'device':
            return bool(self._get_profile_or_global(
                profile_name, 'device_context_access',
                default=self.dpc_agent_device_context_access))
        elif context_type == 'knowledge':
            return bool(self._get_profile_or_global(
                profile_name, 'human_knowledge_access',
                default=self.dpc_agent_human_knowledge_access))

        return False

    def get_agent_skill_permission(self, operation: str,
                                    profile_name: Optional[str] = None) -> bool:
        """
        Check if agent has permission for a skill self-modification operation.

        Args:
            operation: One of: 'self_modify', 'create_new', 'rewrite_existing',
                       'accept_peer_skills', 'auto_announce_to_dht'
            profile_name: Per-agent profile key; when set, per-agent skills settings are used

        Returns:
            True if permitted, False otherwise (defaults to False = safe)
        """
        if not self.dpc_agent_enabled:
            return False
        global_skills = self.rules.get('dpc_agent', {}).get('skills', {})
        global_val = bool(global_skills.get(operation, False))
        if profile_name:
            profile = self.rules.get('agent_profiles', {}).get(profile_name)
            if profile is not None:
                skills = profile.get('skills', {})
                if operation in skills:
                    return bool(skills[operation])
        return global_val

    def get_allowed_agent_tools(self) -> set:
        """
        Get the set of tools the agent is allowed to use.

        Returns:
            Set of allowed tool names based on firewall configuration
        """
        if not self.dpc_agent_enabled:
            return set()

        allowed = set()

        # Add tools that are enabled in configuration
        for tool_name, is_enabled in self.dpc_agent_tools.items():
            if is_enabled:
                allowed.add(tool_name)

        # Override: get_dpc_context requires personal_context_access
        if not self.dpc_agent_personal_context_access:
            allowed.discard('get_dpc_context')

        # Override: import_skill_from_agent requires accept_peer_skills
        if not self.get_agent_skill_permission('accept_peer_skills'):
            allowed.discard('import_skill_from_agent')

        return allowed

    def get_browser_headed(self, agent_id: str) -> bool:
        """ADR-029 Task 002 — return the agent's `browser.headed` toggle.

        Default `True` (visible Firefox window) for desktop deployment;
        `False` opt-in for server / CI where no display is available."""
        value = self._get_profile_or_global(
            agent_id, 'browser', 'headed', default=True
        )
        return bool(value)

    def list_agent_profiles(self) -> List[str]:
        """
        List available agent permission profiles.

        Returns:
            List of profile names (e.g., ['default', 'coding_assistant', 'restricted'])
        """
        return list(self.rules.get('agent_profiles', {}).keys())

    def get_agent_profile_settings(self, profile_name: str) -> Optional[Dict[str, Any]]:
        """
        Get settings for a specific agent profile.

        Args:
            profile_name: Name of the profile to load

        Returns:
            Dict with profile settings, or None if profile not found
        """
        profiles = self.rules.get('agent_profiles', {})
        if profile_name in profiles:
            return profiles[profile_name].copy()
        return None

    def get_agent_web_auth_domains(self, agent_id: str) -> list:
        """Return the list of allowed web-auth domains for an agent profile."""
        profile = self.rules.get('agent_profiles', {}).get(agent_id, {})
        return profile.get('web_auth', {}).get('allowed_domains', [])

    def get_agent_permissions_summary(self, agent_id: str = "agent_001") -> Dict[str, Any]:
        """
        Get a complete permissions summary for an agent — for UI transparency.

        Returns all access paths, tools, and capabilities so the user can see
        exactly what the agent has access to.

        Args:
            agent_id: Agent identifier (e.g., "agent_001")

        Returns:
            Dict with sandbox_paths, tools, capabilities, and archive_access
        """
        # Determine which tool set to use (per-profile or global)
        allowed_tools = self.get_allowed_agent_tools_for_profile(agent_id)

        # Categorize tools
        from .dpc_agent.tools.registry import CORE_TOOL_NAMES, RESTRICTED_TOOL_NAMES
        core_enabled = sorted(allowed_tools & CORE_TOOL_NAMES)
        restricted_enabled = sorted(allowed_tools & RESTRICTED_TOOL_NAMES)
        other_enabled = sorted(allowed_tools - CORE_TOOL_NAMES - RESTRICTED_TOOL_NAMES)

        return {
            "agent_id": agent_id,
            "enabled": self.dpc_agent_enabled,
            "sandbox_paths": {
                "agent_root": str(Path.home() / ".dpc" / "agents" / agent_id),
                "read_only": self.sandbox_read_only_paths,
                "read_write": self.sandbox_read_write_paths,
            },
            "tools": {
                "core_enabled": core_enabled,
                "core_total": len(CORE_TOOL_NAMES),
                "restricted_enabled": restricted_enabled,
                "other_enabled": other_enabled,
            },
            "capabilities": {
                "personal_context_access": self.dpc_agent_personal_context_access,
                "device_context_access": self.dpc_agent_device_context_access,
                "human_knowledge_access": self.dpc_agent_human_knowledge_access,
            },
            "archive_access": True,  # read_session_archive is a core tool, always available
        }

    def get_allowed_agent_tools_for_profile(self, profile_name: str) -> Set[str]:
        """
        Get allowed tools for a specific agent profile.

        Uses global dpc_agent tools as the baseline, then applies per-profile overrides.
        Also enforces per-profile human_knowledge_access, personal_context_access, and skills
        overrides (same logic as get_allowed_agent_tools() but scoped to the profile).

        Args:
            profile_name: Agent profile key (typically agent_id)

        Returns:
            Set of allowed tool names based on per-agent profile configuration
        """
        profile = self.get_agent_profile_settings(profile_name)
        if not profile:
            # No per-agent profile — fall back to global settings
            return self.get_allowed_agent_tools()

        if not profile.get('enabled', self.dpc_agent_enabled):
            return set()

        # Start from global tool defaults, then override with per-profile values
        profile_tools = profile.get('tools', {})
        allowed = set()
        for tool_name, global_enabled in self.dpc_agent_tools.items():
            if bool(profile_tools.get(tool_name, global_enabled)):
                allowed.add(tool_name)
        for tool_name, enabled in profile_tools.items():
            if _is_tool_key(tool_name) and bool(enabled):
                allowed.add(tool_name)

        # Per-profile overrides mirroring get_allowed_agent_tools()
        personal_access = profile.get('personal_context_access', self.dpc_agent_personal_context_access)
        if not personal_access:
            allowed.discard('get_dpc_context')

        profile_skills = profile.get('skills', {})
        accept_peer = profile_skills.get(
            'accept_peer_skills',
            bool(self.rules.get('dpc_agent', {}).get('skills', {}).get('accept_peer_skills', False))
        )
        if not accept_peer:
            allowed.discard('import_skill_from_agent')

        return allowed

    def create_agent_profile(self, profile_name: str, copy_from_global: bool = True) -> bool:
        """
        Create a new agent profile with default settings.

        Args:
            profile_name: Name for the new profile (typically agent_id)
            copy_from_global: If True, copy settings from dpc_agent; otherwise use safe defaults

        Returns:
            True if profile was created, False if it already exists
        """
        import json

        # Check if profile already exists
        if 'agent_profiles' not in self.rules:
            self.rules['agent_profiles'] = {}
        if profile_name in self.rules['agent_profiles']:
            return False  # Already exists

        if copy_from_global and 'dpc_agent' in self.rules:
            # Copy from global dpc_agent settings
            import copy
            self.rules['agent_profiles'][profile_name] = copy.deepcopy(self.rules['dpc_agent'])
        else:
            # Create with safe defaults
            self.rules['agent_profiles'][profile_name] = {
                'enabled': True,
                'personal_context_access': False,
                'device_context_access': False,
                'human_knowledge_access': True,
                'tools': {
                    'read_file': True,
                    'write_file': False,
                    'list_dir': True,
                    'repo_delete': False,
                    'update_scratchpad': True,
                    'browse_page': True,
                    'search_web': True,
                    'search_files': True,
                    'search_in_file': True,
                },
            }

        # Save to file
        rules_text = json.dumps(self.rules, indent=2)
        self.access_file_path.write_text(rules_text)
        logger.info("Created agent profile: %s", profile_name)
        return True

    def _ensure_file_exists(self):
        """Creates a default, secure privacy_rules.json file if one doesn't exist."""
        if not self.access_file_path.exists():
            logger.warning("Access control file not found at %s - creating a default, secure template", self.access_file_path)

            self.access_file_path.parent.mkdir(parents=True, exist_ok=True)

            # Default privacy_rules.json template
            # IMPORTANT: If you add new sections here that need to be displayed in the UI,
            # follow the pattern established for 'ai_scopes':
            # 1. Add event broadcast in save_firewall_rules() (service.py) - already exists as 'firewall_rules_updated'
            # 2. Create a writable store in coreService.ts (e.g., export const firewallRulesUpdated)
            # 3. Add event handler in coreService.ts message listener to update the store
            # 4. In UI component (+page.svelte or other), create load function with guard flag
            # 5. Add reactive statement to reload data when $firewallRulesUpdated changes
            # This ensures UI stays in sync with privacy_rules.json without requiring page refresh.
            # Example: AI scopes dropdown reloads immediately after user saves firewall rules.
            default_rules = {
                "_comment": "D-PC Access Control File - This file controls who can access your context data and compute resources. By default, all access is denied. Replace example node IDs (dpc-node-alice-123, etc.) with actual node IDs from your peers.",
                "hub": {
                    "_comment": "What the Hub can see for peer discovery (minimal by default)",
                    "personal.json:profile.name": "allow",
                    "personal.json:profile.description": "allow"
                },
                "node_groups": {
                    "_comment": "Define which nodes belong to which groups. Add your peers' node IDs here (copy from their URI or HELLO handshake).",
                    "friends": [],
                    "colleagues": [],
                    "family": []
                },
                "file_groups": {
                    "_comment": "Define aliases for groups of context files (supports wildcards)",
                    "work": ["work_*.json", "projects.json"],
                    "personal": ["personal.json", "hobbies.json"]
                },
                "compute": {
                    "_comment": "Compute sharing settings - Allow peers to run AI inference on your GPU/CPU",
                    "enabled": False,
                    "allow_groups": [],
                    "allow_nodes": [],
                    "_allowed_models": "Empty = every model. Since the host designates serving_alias, this list can only refuse a peer that names a model; it never chooses one. A non-empty list that does not contain the serving alias's own model makes this node advertise nothing and refuse everything.",
                    "allowed_models": [],
                    "_serving_alias": "The one provider alias peers are served from; a peer naming any other is refused. Empty = share nothing (the opposite of allowed_models, where empty = all).",
                    "serving_alias": None
                },
                "transcription": {
                    "_comment": "Transcription sharing settings - Allow peers to use your Whisper model for voice transcription",
                    "enabled": False,
                    "allow_groups": [],
                    "allow_nodes": [],
                    "allowed_models": ["openai/whisper-large-v3", "openai/whisper-medium"]
                },
                "nodes": {
                    "_comment": "Per-node access rules - Most specific, overrides group rules. Add entries like: \"dpc-node-xxxx\": {\"personal.json:profile.*\": \"allow\"}"
                },
                "groups": {
                    "_comment": "Per-group access rules - Applied to all nodes in the group",
                    "friends": {
                        "personal.json:profile.*": "allow",
                        "personal.json:knowledge.*": "allow"
                    },
                    "colleagues": {
                        "personal.json:profile.name": "allow",
                        "personal.json:profile.description": "allow",
                        "personal.json:knowledge.professional_skills.*": "allow"
                    },
                    "family": {
                        "personal.json:*": "allow"
                    }
                },
                "ai_scopes": {
                    "_comment": "AI Scope Filtering - Control what your LOCAL AI can access. NEW in v0.12.1: Field-level filtering for device_context.json",
                    "_examples": "Supports file groups (@work) and field-level filtering (device_context.json:hardware.gpu.*)",
                    "work": {
                        "_comment": "Work mode - Work files + hardware specs",
                        "@work:*": "allow",
                        "personal.json:knowledge.work_projects.*": "allow",
                        "device_context.json:hardware.gpu.*": "allow",
                        "device_context.json:software.dev_tools.*": "allow"
                    },
                    "personal": {
                        "_comment": "Personal mode - Personal files, hide GPU specs",
                        "@personal:*": "allow",
                        "@work:*": "deny",
                        "device_context.json:hardware.gpu.*": "deny",
                        "device_context.json:software.os.*": "allow"
                    },
                    "basic": {
                        "_comment": "Basic mode - Profile only, no hardware",
                        "personal.json:profile.name": "allow",
                        "personal.json:profile.description": "allow",
                        "device_context.json:hardware.*": "deny",
                        "device_context.json:software.os.*": "allow"
                    }
                },
                "device_sharing": {
                    "_comment": "Device context sharing rules - Control what hardware/software info peers can see",
                    "colleagues": {
                        "device_context.json:software.os.*": "allow",
                        "device_context.json:software.dev_tools.*": "allow"
                    },
                    "friends": {
                        "device_context.json:hardware.gpu.*": "allow",
                        "device_context.json:hardware.cpu.*": "allow",
                        "device_context.json:software.*": "allow"
                    }
                },
                "notifications": {
                    "_comment": "Desktop notification settings - When to show system notifications (app in background)",
                    "enabled": True,
                    "events": {
                        "new_message": True,
                        "file_offer": True,
                        "file_complete": True,
                        "file_cancelled": True,
                        "knowledge_proposal": True,
                        "knowledge_result": True,
                        "session_proposal": True,
                        "session_result": True,
                        "connection_status": False
                    }
                },
                "dpc_agent": {
                    "_comment": "DPC Agent permissions - Control what the embedded AI agent can access. Tools dict is populated automatically from the ToolEntry registry on first load (see _seed_missing_tools_into_rules) — single source of truth is ToolEntry.default_enabled per registration.",
                    "enabled": True,
                    "personal_context_access": False,
                    "device_context_access": False,
                    "human_knowledge_access": True,
                    "tools": {}
                },
                "file_transfer": {
                    "_comment": "File transfer permissions (v0.11.0+). Configure per-group or per-node settings.",
                    "groups": {
                        "friends": {
                            "file_transfer.allow": "allow",
                            "file_transfer.max_size_mb": 100,
                            "file_transfer.allowed_mime_types": ["*"]
                        },
                        "colleagues": {
                            "file_transfer.allow": "allow",
                            "file_transfer.max_size_mb": 50,
                            "file_transfer.allowed_mime_types": ["image/*", "application/pdf", "text/*"]
                        }
                    },
                    "nodes": {
                        "dpc-node-example-abc123": {
                            "file_transfer.allow": "allow",
                            "file_transfer.max_size_mb": 500,
                            "file_transfer.allowed_mime_types": ["*"]
                        }
                    }
                },
                "image_transfer": {
                    "_comment": "Screenshot/image transfer settings (P2P clipboard paste). Controls auto-accept behavior, size limits, and storage for pasted images.",
                    "auto_accept_threshold_mb": 25,
                    "allowed_sources": ["clipboard", "file", "camera"],
                    "max_size_mb": 100,
                    "save_screenshots_to_disk": False
                }
            }

            self.access_file_path.write_text(json.dumps(default_rules, indent=2))
            logger.info("Default access control file created at %s", self.access_file_path)

    def _get_rule_for_resource(self, section_type: str, section_key: str, resource_path: str) -> str | None:
        """
        Finds the most specific rule for a given resource in a section,
        handling file groups and wildcards correctly.

        Args:
            section_type: Type of section ('hub', 'nodes', 'groups', 'ai_scopes', 'device_sharing')
            section_key: Key within the section (e.g., node_id, group_name, or empty for hub)
            resource_path: Resource path to check (e.g., "personal.json:profile.name")
        """
        # Get the rules dict for this section
        if section_type == 'hub':
            section_rules = self.rules.get('hub', {})
        elif section_type == 'nodes':
            section_rules = self.rules.get('nodes', {}).get(section_key, {})
        elif section_type == 'groups':
            section_rules = self.rules.get('groups', {}).get(section_key, {})
        elif section_type == 'ai_scopes':
            section_rules = self.rules.get('ai_scopes', {}).get(section_key, {})
        elif section_type == 'device_sharing':
            section_rules = self.rules.get('device_sharing', {}).get(section_key, {})
        else:
            return None

        if not section_rules:
            return None

        parts = resource_path.split(':', 1)
        if len(parts) != 2:
            return None  # Invalid resource path format

        target_filename, target_json_path = parts

        best_match_rule = None
        best_match_specificity = -1

        for pattern, value in section_rules.items():
            # Skip comment fields
            if pattern.startswith('_'):
                continue

            pattern_parts = pattern.split(':', 1)
            if len(pattern_parts) != 2:
                continue

            file_pattern, path_pattern = pattern_parts

            # 1. Check if the file pattern matches
            file_matched = False
            if file_pattern.startswith('@'):
                group_name = file_pattern[1:]
                if group_name in self.file_groups:
                    for group_file_pattern in self.file_groups[group_name]:
                        if fnmatch.fnmatch(target_filename, group_file_pattern):
                            file_matched = True
                            break
            elif fnmatch.fnmatch(target_filename, file_pattern):
                file_matched = True

            if not file_matched:
                continue

            # 2. Check if the path pattern matches and find the most specific one
            if fnmatch.fnmatch(target_json_path, path_pattern):
                # Calculate specificity: longer pattern without wildcards is more specific
                specificity = len(path_pattern.replace('*', ''))
                if specificity > best_match_specificity:
                    best_match_specificity = specificity
                    best_match_rule = value

        return best_match_rule

    def _get_groups_for_node(self, node_id: str) -> List[str]:
        """
        Returns a list of group names that the given node_id belongs to.
        """
        groups = []
        for group_name, node_list in self.node_groups.items():
            # Skip comment fields
            if group_name.startswith('_'):
                continue
            if node_id in node_list:
                groups.append(group_name)
        return groups

    def can_access(self, requester_identity: str, resource_path: str) -> bool:
        """
        Checks if a requester has access to a specific resource path.
        The order of precedence is: Node > Group > Hub / AI Scope / Device Sharing > Default (deny).
        """
        # 1. Check for a specific node rule
        if requester_identity.startswith('dpc-node-'):
            rule = self._get_rule_for_resource('nodes', requester_identity, resource_path)
            if rule:
                return rule.lower() == 'allow'

        # 2. Check for group rules (deny-wins: if any group denies, access is denied)
        # Get all groups this node belongs to
        if requester_identity.startswith('dpc-node-'):
            groups = self._get_groups_for_node(requester_identity)
            group_rules = [
                self._get_rule_for_resource('groups', gn, resource_path)
                for gn in groups
            ]
            group_rules = [r.lower() for r in group_rules if r]
            if 'deny' in group_rules:
                return False
            if 'allow' in group_rules:
                return True

        # 3. Check for hub rule
        if requester_identity == "hub":
            rule = self._get_rule_for_resource('hub', '', resource_path)
            if rule:
                return rule.lower() == 'allow'

        # 4. Check for AI scope rule
        if requester_identity.startswith("ai_scope:"):
            scope_name = requester_identity[9:]  # Remove "ai_scope:" prefix
            rule = self._get_rule_for_resource('ai_scopes', scope_name, resource_path)
            if rule:
                return rule.lower() == 'allow'

        # 5. Check for device sharing rule
        if requester_identity.startswith("device_sharing:"):
            sharing_scope = requester_identity[15:]  # Remove "device_sharing:" prefix
            rule = self._get_rule_for_resource('device_sharing', sharing_scope, resource_path)
            if rule:
                return rule.lower() == 'allow'

        # 6. Default to deny if no specific allow rule is found
        return False

    def filter_context_for_peer(self, context: PersonalContext, peer_id: str, query: str = None) -> PersonalContext:
        """
        Filters a PersonalContext based on firewall rules for a specific peer.
        Returns a new PersonalContext with only allowed fields.

        Args:
            context: The full PersonalContext to filter
            peer_id: The node_id of the requesting peer
            query: Optional query string (for context-aware filtering)

        Returns:
            Filtered PersonalContext with only allowed fields
        """
        from dataclasses import fields

        # Create a filtered copy by checking each field
        filtered_kwargs = {}

        # Check each field against firewall rules
        for field in fields(context):
            field_name = field.name
            field_value = getattr(context, field_name)

            # Skip if field is None or empty
            if field_value is None:
                filtered_kwargs[field_name] = None
                continue

            # Check if peer can access this field
            resource_path = f"personal.json:{field_name}"

            # Check for specific rule first (node > group)
            specific_rule = None
            # Try node-specific rule
            if peer_id.startswith('dpc-node-'):
                specific_rule = self._get_rule_for_resource('nodes', peer_id, resource_path)
                # Try group rules if no node rule (deny-wins across groups)
                if not specific_rule:
                    groups = self._get_groups_for_node(peer_id)
                    group_rules = [
                        self._get_rule_for_resource('groups', gn, resource_path)
                        for gn in groups
                    ]
                    group_rules = [r.lower() for r in group_rules if r]
                    if 'deny' in group_rules:
                        specific_rule = 'deny'
                    elif 'allow' in group_rules:
                        specific_rule = 'allow'

            # If there's a specific rule (allow or deny), use it - don't fall back to wildcard
            if specific_rule:
                if specific_rule.lower() == 'allow':
                    filtered_kwargs[field_name] = deepcopy(field_value)
                else:
                    # Specific deny - set to None or empty value
                    if isinstance(field_value, list):
                        filtered_kwargs[field_name] = []
                    elif isinstance(field_value, dict):
                        filtered_kwargs[field_name] = {}
                    else:
                        filtered_kwargs[field_name] = None
            else:
                # No specific rule - check for wildcard access
                wildcard_path = "personal.json:*"
                if self.can_access(peer_id, wildcard_path):
                    filtered_kwargs[field_name] = deepcopy(field_value)
                else:
                    # No access - set to None or empty value
                    if isinstance(field_value, list):
                        filtered_kwargs[field_name] = []
                    elif isinstance(field_value, dict):
                        filtered_kwargs[field_name] = {}
                    else:
                        filtered_kwargs[field_name] = None

        # Create new PersonalContext from filtered fields
        # This preserves the original dataclass instances (InstructionBlock, etc.)
        return PersonalContext(**filtered_kwargs)

    def filter_personal_context_for_ai_scope(self, context: 'PersonalContext', scope_name: str) -> 'PersonalContext':
        """
        Filters personal context based on AI scope rules, removing fields that the AI scope cannot access.

        Args:
            context: The PersonalContext object to filter
            scope_name: The AI scope name (e.g., "work", "personal")

        Returns:
            Filtered PersonalContext with only allowed fields
        """
        from dataclasses import fields

        # Build the requester identity for AI scope
        requester_identity = f"ai_scope:{scope_name}"

        # Create a filtered copy by checking each field
        filtered_kwargs = {}

        # Check each field against firewall rules
        for field in fields(context):
            field_name = field.name
            field_value = getattr(context, field_name)

            # Skip if field is None or empty
            if field_value is None:
                filtered_kwargs[field_name] = None
                continue

            # Check if AI scope can access this field
            resource_path = f"personal.json:{field_name}"

            # Get the specific rule first (if it exists)
            specific_rule = self._get_rule_for_resource('ai_scopes', scope_name, resource_path)

            # If there's a specific rule (allow or deny), use it - don't fall back to wildcard
            if specific_rule:
                if specific_rule.lower() == 'allow':
                    filtered_kwargs[field_name] = deepcopy(field_value)
                else:
                    # Specific deny - set to None or empty value
                    if isinstance(field_value, list):
                        filtered_kwargs[field_name] = []
                    elif isinstance(field_value, dict):
                        filtered_kwargs[field_name] = {}
                    else:
                        filtered_kwargs[field_name] = None
            else:
                # No specific rule - check for wildcard access
                wildcard_path = "personal.json:*"
                if self.can_access(requester_identity, wildcard_path):
                    filtered_kwargs[field_name] = deepcopy(field_value)
                else:
                    # No access - set to None or empty value
                    if isinstance(field_value, list):
                        filtered_kwargs[field_name] = []
                    elif isinstance(field_value, dict):
                        filtered_kwargs[field_name] = {}
                    else:
                        filtered_kwargs[field_name] = None

        # Create new PersonalContext from filtered fields
        # This preserves the original dataclass instances (InstructionBlock, etc.)
        return PersonalContext(**filtered_kwargs)

    def filter_device_context_for_ai_scope(self, device_context: Dict, scope_name: str) -> Dict:
        """
        Filters device context based on AI scope rules, removing fields that the AI scope cannot access.

        Args:
            device_context: The device context dict to filter
            scope_name: The AI scope name (e.g., "work", "personal")

        Returns:
            Filtered device context dict with only allowed fields
        """
        def filter_nested_dict(data: Dict, path_prefix: str) -> Dict:
            """Recursively filter nested dict based on AI scope rules."""
            if not isinstance(data, dict):
                return data

            filtered = {}
            for key, value in data.items():
                current_path = f"{path_prefix}.{key}" if path_prefix else key
                resource_path = f"device_context.json:{current_path}"

                # Build the requester identity for AI scope
                requester_identity = f"ai_scope:{scope_name}"

                # Check for specific rule first
                specific_rule = self._get_rule_for_resource('ai_scopes', scope_name, resource_path)

                # If there's a specific rule, use it - don't fall back to wildcard
                if specific_rule:
                    logger.debug(f"AI Scope filter: Specific rule for {resource_path}: {specific_rule}")
                    if specific_rule.lower() == 'allow':
                        if isinstance(value, dict):
                            # Allow access - but still recursively filter in case there are deny rules below
                            filtered[key] = filter_nested_dict(value, current_path)
                            # If nothing was allowed in the subtree, use the whole value
                            if not filtered[key] and value:
                                filtered[key] = deepcopy(value)
                        else:
                            # Leaf node - allow access
                            filtered[key] = deepcopy(value)
                    # else: specific deny - don't include this key
                else:
                    # No specific rule - check for wildcard access
                    wildcard_path = f"device_context.json:{current_path}.*"
                    has_wildcard_access = self.can_access(requester_identity, wildcard_path)
                    logger.debug(f"AI Scope filter: Checking wildcard {wildcard_path}: {has_wildcard_access}")
                    if has_wildcard_access:
                        if isinstance(value, dict):
                            # Has wildcard access - include the whole subtree
                            filtered[key] = deepcopy(value)
                        else:
                            filtered[key] = deepcopy(value)
                    elif isinstance(value, dict):
                        # No direct access, but might have access to nested fields
                        nested_filtered = filter_nested_dict(value, current_path)
                        if nested_filtered:  # Only include if not empty
                            filtered[key] = nested_filtered

            return filtered

        # Start filtering from root level
        return filter_nested_dict(device_context, "")

    def can_request_inference(self, requester_node_id: str, model: str = None,
                              provider: str = None) -> bool:
        """
        Checks if a peer can request remote inference on this node.

        Args:
            requester_node_id: The node_id of the requesting peer
            model: Optional model name to check if allowed
            provider: Optional provider alias the peer named. Only the alias
                this node designates in `compute.serving_alias` is accepted;
                anything else is refused, including when no model is given.

        Returns:
            True if the peer can request inference (and use the specified model if provided)
        """
        # Check if compute sharing is enabled
        if not self.compute_enabled:
            return False

        # A peer may ask, but it does not choose what we run. Before D4-0 the
        # alias travelled straight from the wire to the router while this check
        # was handed only the model, so `provider: "deepseek_pro"` with no model
        # passed a check that had nothing to look at. Note this refuses the
        # named alias even when serving_alias is unset — nothing designated,
        # nothing served.
        if provider is not None and provider != self.compute_serving_alias:
            logger.warning(
                "Compute denied for %s: named provider '%s' is not this node's serving alias (%s)",
                requester_node_id, provider, self.compute_serving_alias or "unset",
            )
            return False

        # Check if requester is in allowed nodes list
        if requester_node_id in self.compute_allowed_nodes:
            # Node is explicitly allowed, check model if specified
            if model and self.compute_allowed_models:
                return model in self.compute_allowed_models
            return True

        # Check if requester is in any allowed group
        requester_groups = self._get_groups_for_node(requester_node_id)
        for group in requester_groups:
            if group in self.compute_allowed_groups:
                # Node is in an allowed group, check model if specified
                if model and self.compute_allowed_models:
                    return model in self.compute_allowed_models
                return True

        # Not authorized
        return False

    def can_request_transcription(self, requester_node_id: str, model: str = None) -> bool:
        """
        Checks if a peer can request remote transcription on this node.

        Args:
            requester_node_id: The node_id of the requesting peer
            model: Optional model name to check if allowed

        Returns:
            True if the peer can request transcription (and use the specified model if provided)
        """
        # Check if transcription sharing is enabled
        if not self.transcription_enabled:
            return False

        # Check if requester is in allowed nodes list
        if requester_node_id in self.transcription_allowed_nodes:
            # Node is explicitly allowed, check model if specified
            if model and self.transcription_allowed_models:
                return model in self.transcription_allowed_models
            return True

        # Check if requester is in any allowed group
        requester_groups = self._get_groups_for_node(requester_node_id)
        for group in requester_groups:
            if group in self.transcription_allowed_groups:
                # Node is in an allowed group, check model if specified
                if model and self.transcription_allowed_models:
                    return model in self.transcription_allowed_models
                return True

        # Not authorized
        return False

    def get_available_models_for_peer(self, requester_node_id: str, all_models: List[str]) -> List[str]:
        """
        Returns the list of models that a peer is allowed to use.

        Args:
            requester_node_id: The node_id of the requesting peer
            all_models: List of all available models on this node

        Returns:
            List of model names the peer can use (empty if no access)
        """
        if not self.can_request_inference(requester_node_id):
            return []

        # If no specific models are restricted, return all available models
        if not self.compute_allowed_models:
            return all_models

        # Return intersection of allowed models and available models
        return [m for m in all_models if m in self.compute_allowed_models]

    def filter_device_context_for_peer(self, device_context: Dict, peer_id: str) -> Dict:
        """
        Filters device context based on firewall rules for a specific peer.
        Returns a new dict with only allowed fields.

        Args:
            device_context: The full device context dict to filter
            peer_id: The node_id of the requesting peer

        Returns:
            Filtered device context dict with only allowed fields
        """
        def filter_nested_dict(data: Dict, path_prefix: str) -> Dict:
            """Recursively filter nested dict based on firewall rules."""
            if not isinstance(data, dict):
                return data

            filtered = {}
            for key, value in data.items():
                current_path = f"{path_prefix}.{key}" if path_prefix else key
                resource_path = f"device_context.json:{current_path}"

                # Check for specific rule first (node > group)
                specific_rule = None
                if peer_id.startswith('dpc-node-'):
                    specific_rule = self._get_rule_for_resource('nodes', peer_id, resource_path)
                    if not specific_rule:
                        groups = self._get_groups_for_node(peer_id)
                        group_rules = [
                            self._get_rule_for_resource('groups', gn, resource_path)
                            for gn in groups
                        ]
                        group_rules = [r.lower() for r in group_rules if r]
                        if 'deny' in group_rules:
                            specific_rule = 'deny'
                        elif 'allow' in group_rules:
                            specific_rule = 'allow'

                # If there's a specific rule, use it - don't fall back to wildcard
                if specific_rule:
                    if specific_rule.lower() == 'allow':
                        if isinstance(value, dict):
                            # Allow access - but still recursively filter in case there are deny rules below
                            filtered[key] = filter_nested_dict(value, current_path)
                            # If nothing was allowed in the subtree, use the whole value
                            if not filtered[key] and value:
                                filtered[key] = deepcopy(value)
                        else:
                            # Leaf node - allow access
                            filtered[key] = deepcopy(value)
                    # else: specific deny - don't include this key
                else:
                    # No specific rule - check for wildcard access
                    wildcard_path = f"device_context.json:{current_path}.*"
                    if self.can_access(peer_id, wildcard_path):
                        # Allow access to all sub-fields
                        filtered[key] = deepcopy(value)
                    elif isinstance(value, dict):
                        # No direct access, but might have access to nested fields
                        nested_filtered = filter_nested_dict(value, current_path)
                        if nested_filtered:  # Only include if not empty
                            filtered[key] = nested_filtered

            return filtered

        # Filter top-level sections (hardware, software, metadata)
        filtered_context = {}
        for section_name, section_data in device_context.items():
            # Skip non-dict values at top level
            if not isinstance(section_data, dict):
                # Check if this top-level field is allowed
                resource_path = f"device_context.json:{section_name}"
                if self.can_access(peer_id, resource_path):
                    filtered_context[section_name] = deepcopy(section_data)
                continue

            resource_path = f"device_context.json:{section_name}"

            # Check for specific rule first (node > group)
            specific_rule = None
            if peer_id.startswith('dpc-node-'):
                specific_rule = self._get_rule_for_resource('nodes', peer_id, resource_path)
                if not specific_rule:
                    groups = self._get_groups_for_node(peer_id)
                    group_rules = [
                        self._get_rule_for_resource('groups', gn, resource_path)
                        for gn in groups
                    ]
                    group_rules = [r.lower() for r in group_rules if r]
                    if 'deny' in group_rules:
                        specific_rule = 'deny'
                    elif 'allow' in group_rules:
                        specific_rule = 'allow'

            # If there's a specific rule, use it - don't fall back to wildcard
            if specific_rule:
                if specific_rule.lower() == 'allow':
                    # Specific allow for entire section - but still recurse to check for deny rules within
                    filtered_section = filter_nested_dict(section_data, section_name)
                    if filtered_section:
                        filtered_context[section_name] = filtered_section
                    elif section_data:
                        # If recursion filtered everything out but section has data, respect that
                        pass
                # else: specific deny - don't include this section
            else:
                # No specific rule - check for wildcard access to section
                wildcard_path = f"device_context.json:{section_name}.*"
                if self.can_access(peer_id, wildcard_path):
                    # Wildcard allows - but still recurse to check for specific deny rules within
                    filtered_section = filter_nested_dict(section_data, section_name)
                    if filtered_section:
                        filtered_context[section_name] = filtered_section
                else:
                    # Recursively filter section contents
                    filtered_section = filter_nested_dict(section_data, section_name)
                    if filtered_section:  # Only include if not empty
                        filtered_context[section_name] = filtered_section

        return filtered_context

    @staticmethod
    def validate_config(config_dict: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        Validate firewall configuration without applying it.

        Args:
            config_dict: The JSON configuration dict to validate

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        try:
            # Validate top-level structure
            valid_top_level_keys = ['hub', 'node_groups', 'file_groups', 'compute', 'transcription', 'nodes', 'groups', 'ai_scopes', 'device_sharing', 'file_transfer', 'image_transfer', 'notifications', 'dpc_agent', 'agent_profiles', '_comment']

            for key in config_dict.keys():
                if key not in valid_top_level_keys:
                    errors.append(f"Unknown top-level key: '{key}'")

            # Validate hub section
            if 'hub' in config_dict:
                if not isinstance(config_dict['hub'], dict):
                    errors.append("'hub' section must be a dictionary")
                else:
                    for resource_path, action in config_dict['hub'].items():
                        if resource_path.startswith('_'):
                            continue  # Skip comments
                        if action not in ['allow', 'deny']:
                            errors.append(f"Invalid action in hub: '{resource_path} = {action}' (should be 'allow' or 'deny')")

            # Validate node_groups section
            if 'node_groups' in config_dict:
                if not isinstance(config_dict['node_groups'], dict):
                    errors.append("'node_groups' section must be a dictionary")
                else:
                    for group_name, node_list in config_dict['node_groups'].items():
                        if group_name.startswith('_'):
                            continue  # Skip comments
                        if not isinstance(node_list, list):
                            errors.append(f"Node group '{group_name}' must be a list of node IDs")
                        else:
                            for node_id in node_list:
                                if not node_id.startswith('dpc-node-'):
                                    errors.append(f"Invalid node ID in group '{group_name}': '{node_id}' (should start with 'dpc-node-')")

            # Validate file_groups section
            if 'file_groups' in config_dict:
                if not isinstance(config_dict['file_groups'], dict):
                    errors.append("'file_groups' section must be a dictionary")
                else:
                    for group_name, file_list in config_dict['file_groups'].items():
                        if group_name.startswith('_'):
                            continue  # Skip comments
                        if not isinstance(file_list, list):
                            errors.append(f"File group '{group_name}' must be a list of file patterns")

            # Validate compute section
            if 'compute' in config_dict:
                compute = config_dict['compute']
                if not isinstance(compute, dict):
                    errors.append("'compute' section must be a dictionary")
                else:
                    if 'enabled' in compute and not isinstance(compute['enabled'], bool):
                        errors.append("'compute.enabled' must be a boolean (true or false)")

                    if 'allow_nodes' in compute and not isinstance(compute['allow_nodes'], list):
                        errors.append("'compute.allow_nodes' must be a list")

                    if 'allow_groups' in compute and not isinstance(compute['allow_groups'], list):
                        errors.append("'compute.allow_groups' must be a list")

                    if 'allowed_models' in compute and not isinstance(compute['allowed_models'], list):
                        errors.append("'compute.allowed_models' must be a list")

                    if 'serving_alias' in compute and compute['serving_alias'] is not None \
                            and not isinstance(compute['serving_alias'], str):
                        errors.append("'compute.serving_alias' must be a provider alias (a string) or null")

            # Validate transcription section
            if 'transcription' in config_dict:
                transcription = config_dict['transcription']
                if not isinstance(transcription, dict):
                    errors.append("'transcription' section must be a dictionary")
                else:
                    if 'enabled' in transcription and not isinstance(transcription['enabled'], bool):
                        errors.append("'transcription.enabled' must be a boolean (true or false)")

                    if 'allow_nodes' in transcription and not isinstance(transcription['allow_nodes'], list):
                        errors.append("'transcription.allow_nodes' must be a list")

                    if 'allow_groups' in transcription and not isinstance(transcription['allow_groups'], list):
                        errors.append("'transcription.allow_groups' must be a list")

                    if 'allowed_models' in transcription and not isinstance(transcription['allowed_models'], list):
                        errors.append("'transcription.allowed_models' must be a list")

            # Validate nodes section
            if 'nodes' in config_dict:
                if not isinstance(config_dict['nodes'], dict):
                    errors.append("'nodes' section must be a dictionary")
                else:
                    for node_id, rules in config_dict['nodes'].items():
                        if node_id.startswith('_'):
                            continue  # Skip comments
                        if not node_id.startswith('dpc-node-'):
                            errors.append(f"Invalid node ID: '{node_id}' (should start with 'dpc-node-')")
                        if not isinstance(rules, dict):
                            errors.append(f"Rules for node '{node_id}' must be a dictionary")
                        else:
                            for resource_path, action in rules.items():
                                if resource_path.startswith('_'):  # Skip nested comment fields
                                    continue
                                if action not in ['allow', 'deny']:
                                    errors.append(f"Invalid action for node '{node_id}': '{resource_path} = {action}' (should be 'allow' or 'deny')")

            # Validate groups section
            if 'groups' in config_dict:
                if not isinstance(config_dict['groups'], dict):
                    errors.append("'groups' section must be a dictionary")
                else:
                    for group_name, rules in config_dict['groups'].items():
                        if group_name.startswith('_'):
                            continue  # Skip comments
                        if not isinstance(rules, dict):
                            errors.append(f"Rules for group '{group_name}' must be a dictionary")
                        else:
                            for resource_path, action in rules.items():
                                if resource_path.startswith('_'):  # Skip nested comment fields
                                    continue
                                if action not in ['allow', 'deny']:
                                    errors.append(f"Invalid action for group '{group_name}': '{resource_path} = {action}' (should be 'allow' or 'deny')")

            # Validate ai_scopes section
            if 'ai_scopes' in config_dict:
                if not isinstance(config_dict['ai_scopes'], dict):
                    errors.append("'ai_scopes' section must be a dictionary")
                else:
                    for scope_name, rules in config_dict['ai_scopes'].items():
                        if scope_name.startswith('_'):
                            continue  # Skip comments
                        if not isinstance(rules, dict):
                            errors.append(f"Rules for AI scope '{scope_name}' must be a dictionary")
                        else:
                            for resource_path, action in rules.items():
                                if resource_path.startswith('_'):  # Skip nested comment fields
                                    continue
                                if action not in ['allow', 'deny']:
                                    errors.append(f"Invalid action for AI scope '{scope_name}': '{resource_path} = {action}' (should be 'allow' or 'deny')")

            # Validate device_sharing section
            if 'device_sharing' in config_dict:
                if not isinstance(config_dict['device_sharing'], dict):
                    errors.append("'device_sharing' section must be a dictionary")
                else:
                    for sharing_scope, rules in config_dict['device_sharing'].items():
                        if sharing_scope.startswith('_'):
                            continue  # Skip comments
                        if not isinstance(rules, dict):
                            errors.append(f"Rules for device sharing scope '{sharing_scope}' must be a dictionary")
                        else:
                            for resource_path, action in rules.items():
                                if resource_path.startswith('_'):  # Skip nested comment fields
                                    continue
                                if action not in ['allow', 'deny']:
                                    errors.append(f"Invalid action for device sharing scope '{sharing_scope}': '{resource_path} = {action}' (should be 'allow' or 'deny')")

            # Validate file_transfer section
            if 'file_transfer' in config_dict:
                file_transfer = config_dict['file_transfer']
                if not isinstance(file_transfer, dict):
                    errors.append("'file_transfer' section must be a dictionary")
                else:
                    if 'allow_nodes' in file_transfer:
                        if not isinstance(file_transfer['allow_nodes'], list):
                            errors.append("'file_transfer.allow_nodes' must be a list")
                        else:
                            for node_id in file_transfer['allow_nodes']:
                                if not node_id.startswith('dpc-node-'):
                                    errors.append(f"Invalid node ID in file_transfer.allow_nodes: '{node_id}' (should start with 'dpc-node-')")

                    if 'allow_groups' in file_transfer:
                        if not isinstance(file_transfer['allow_groups'], list):
                            errors.append("'file_transfer.allow_groups' must be a list")

                    if 'max_size_mb' in file_transfer:
                        if not isinstance(file_transfer['max_size_mb'], (int, float)):
                            errors.append("'file_transfer.max_size_mb' must be a number")
                        elif file_transfer['max_size_mb'] <= 0:
                            errors.append("'file_transfer.max_size_mb' must be greater than 0")

                    if 'allowed_mime_types' in file_transfer:
                        if not isinstance(file_transfer['allowed_mime_types'], list):
                            errors.append("'file_transfer.allowed_mime_types' must be a list")

            # Validate notifications section
            if 'notifications' in config_dict:
                notifications = config_dict['notifications']
                if not isinstance(notifications, dict):
                    errors.append("'notifications' section must be a dictionary")
                else:
                    if 'enabled' in notifications and not isinstance(notifications['enabled'], bool):
                        errors.append("'notifications.enabled' must be a boolean (true or false)")

                    if 'events' in notifications:
                        if not isinstance(notifications['events'], dict):
                            errors.append("'notifications.events' must be a dictionary")
                        else:
                            for event_name, enabled in notifications['events'].items():
                                if not isinstance(enabled, bool):
                                    errors.append(f"'notifications.events.{event_name}' must be a boolean (true or false)")

            # Validate dpc_agent section
            if 'dpc_agent' in config_dict:
                dpc_agent = config_dict['dpc_agent']
                if not isinstance(dpc_agent, dict):
                    errors.append("'dpc_agent' section must be a dictionary")
                else:
                    if 'enabled' in dpc_agent and not isinstance(dpc_agent['enabled'], bool):
                        errors.append("'dpc_agent.enabled' must be a boolean (true or false)")

                    if 'personal_context_access' in dpc_agent and not isinstance(dpc_agent['personal_context_access'], bool):
                        errors.append("'dpc_agent.personal_context_access' must be a boolean")

                    if 'device_context_access' in dpc_agent and not isinstance(dpc_agent['device_context_access'], bool):
                        errors.append("'dpc_agent.device_context_access' must be a boolean")

                    if 'human_knowledge_access' in dpc_agent and not isinstance(dpc_agent['human_knowledge_access'], bool):
                        errors.append("'dpc_agent.human_knowledge_access' must be a boolean")

                    if 'tools' in dpc_agent:
                        tools = dpc_agent['tools']
                        if not isinstance(tools, dict):
                            errors.append("'dpc_agent.tools' must be a dictionary")
                        else:
                            # Which names exist is the registry's business, not a
                            # list maintained here (see _known_tool_names).
                            valid_tools = _known_tool_names()
                            for tool_name, tool_enabled in tools.items():
                                if not _is_tool_key(tool_name):
                                    continue  # Comments and run_shell metadata
                                if valid_tools is not None and tool_name not in valid_tools:
                                    logger.warning("No registered tool named '%s' — key in dpc_agent.tools is ignored", tool_name)
                                if not isinstance(tool_enabled, bool):
                                    errors.append(f"'dpc_agent.tools.{tool_name}' must be a boolean")

                    # Validate skills settings (v0.20.0+)
                    if 'skills' in dpc_agent:
                        skills = dpc_agent['skills']
                        if not isinstance(skills, dict):
                            errors.append("'dpc_agent.skills' must be a dictionary")
                        else:
                            bool_fields = ('self_modify', 'create_new', 'rewrite_existing',
                                           'accept_peer_skills', 'auto_announce_to_dht')
                            for field in bool_fields:
                                if field in skills and not isinstance(skills[field], bool):
                                    errors.append(f"'dpc_agent.skills.{field}' must be a boolean")

            # Validate agent_profiles section (v0.19.0+)
            if 'agent_profiles' in config_dict:
                agent_profiles = config_dict['agent_profiles']
                if not isinstance(agent_profiles, dict):
                    errors.append("'agent_profiles' section must be a dictionary")
                else:
                    for profile_name, profile_config in agent_profiles.items():
                        if profile_name.startswith('_'):
                            continue  # Skip comments
                        if not isinstance(profile_config, dict):
                            errors.append(f"Agent profile '{profile_name}' must be a dictionary")
                        else:
                            # Validate profile fields (inherit from dpc_agent structure)
                            if 'enabled' in profile_config and not isinstance(profile_config['enabled'], bool):
                                errors.append(f"'agent_profiles.{profile_name}.enabled' must be a boolean")
                            if 'personal_context_access' in profile_config and not isinstance(profile_config['personal_context_access'], bool):
                                errors.append(f"'agent_profiles.{profile_name}.personal_context_access' must be a boolean")
                            if 'device_context_access' in profile_config and not isinstance(profile_config['device_context_access'], bool):
                                errors.append(f"'agent_profiles.{profile_name}.device_context_access' must be a boolean")
                            if 'human_knowledge_access' in profile_config and not isinstance(profile_config['human_knowledge_access'], bool):
                                errors.append(f"'agent_profiles.{profile_name}.human_knowledge_access' must be a boolean")
                            # Validate tools
                            if 'tools' in profile_config:
                                tools = profile_config['tools']
                                if not isinstance(tools, dict):
                                    errors.append(f"'agent_profiles.{profile_name}.tools' must be a dictionary")
                                else:
                                    # Same source as dpc_agent above: the registry.
                                    valid_tools = _known_tool_names()
                                    for tool_name, tool_enabled in tools.items():
                                        if not _is_tool_key(tool_name):
                                            continue  # Comments and run_shell metadata
                                        if valid_tools is not None and tool_name not in valid_tools:
                                            logger.warning("No registered tool named '%s' — key in agent_profiles.%s.tools is ignored", tool_name, profile_name)
                                        if not isinstance(tool_enabled, bool):
                                            errors.append(f"'agent_profiles.{profile_name}.tools.{tool_name}' must be a boolean")

            # Validate image_transfer section
            if 'image_transfer' in config_dict:
                img_transfer = config_dict['image_transfer']
                if not isinstance(img_transfer, dict):
                    errors.append("'image_transfer' section must be a dictionary")
                else:
                    # Validate auto_accept_threshold_mb
                    if 'auto_accept_threshold_mb' in img_transfer:
                        threshold = img_transfer['auto_accept_threshold_mb']
                        if not isinstance(threshold, (int, float)):
                            errors.append("'image_transfer.auto_accept_threshold_mb' must be a number")
                        elif threshold < 0:
                            errors.append("'image_transfer.auto_accept_threshold_mb' must be non-negative (0 or greater)")

                    # Validate allowed_sources
                    if 'allowed_sources' in img_transfer:
                        sources = img_transfer['allowed_sources']
                        if not isinstance(sources, list):
                            errors.append("'image_transfer.allowed_sources' must be a list")
                        else:
                            valid_sources = {"clipboard", "file", "camera"}
                            for source in sources:
                                if source not in valid_sources:
                                    errors.append(f"Invalid source '{source}' in image_transfer.allowed_sources (valid options: {valid_sources})")

                    # Validate max_size_mb
                    if 'max_size_mb' in img_transfer:
                        max_size = img_transfer['max_size_mb']
                        if not isinstance(max_size, (int, float)):
                            errors.append("'image_transfer.max_size_mb' must be a number")
                        elif max_size <= 0:
                            errors.append("'image_transfer.max_size_mb' must be positive (greater than 0)")

                    # Validate save_screenshots_to_disk
                    if 'save_screenshots_to_disk' in img_transfer:
                        save_to_disk = img_transfer['save_screenshots_to_disk']
                        if not isinstance(save_to_disk, bool):
                            errors.append("'image_transfer.save_screenshots_to_disk' must be a boolean (true or false)")

        except Exception as e:
            errors.append(f"Validation error: {str(e)}")

        return (len(errors) == 0, errors)

    def reload(self) -> Tuple[bool, str]:
        """
        Reload firewall rules from disk.

        Returns:
            Tuple of (success, message)
        """
        try:
            # Validate the file first
            config_text = self.access_file_path.read_text()
            try:
                config_dict = json.loads(config_text)
            except json.JSONDecodeError as e:
                return (False, f"Firewall reload failed - invalid JSON: {str(e)}")

            is_valid, errors = self.validate_config(config_dict)

            if not is_valid:
                error_msg = "Firewall reload failed - validation errors:\n" + "\n".join(errors)
                return (False, error_msg)

            # Re-load the rules
            logger.info("Reloading firewall rules from disk")
            self._load_rules()
            logger.info("Firewall rules reloaded successfully")

            return (True, "Firewall rules reloaded successfully")

        except Exception as e:
            return (False, f"Firewall reload failed: {str(e)}")

    def get_rules_as_dict(self) -> Dict[str, Any]:
        """Read raw rules from disk as a JSON dict."""
        config_text = self.access_file_path.read_text()
        return json.loads(config_text)

    @staticmethod
    def find_missing_sandbox_paths(config_dict: Dict[str, Any]) -> List[str]:
        """Report extended sandbox paths that do not exist on disk.

        Non-existent paths are legal (a path may be created later), so this
        never blocks a save — but a typo silently produces a dead rule that
        only surfaces when an agent is denied. Surfacing it at save time is
        what makes the difference.

        Path handling matches _normalize_path: pathlib expanduser, no manual
        separator logic, so it behaves the same on Windows and POSIX.
        """
        warnings: List[str] = []

        def scan(section: Dict[str, Any], label: str) -> None:
            sandbox = section.get('sandbox_extensions')
            if not isinstance(sandbox, dict):
                return
            for kind in ('read_only', 'read_write'):
                for raw in sandbox.get(kind, []) or []:
                    if not raw:
                        continue
                    try:
                        if not Path(raw).expanduser().exists():
                            warnings.append(f"{label}.{kind}: path does not exist — {raw}")
                    except (OSError, ValueError):
                        warnings.append(f"{label}.{kind}: invalid path — {raw}")

        dpc_agent = config_dict.get('dpc_agent')
        if isinstance(dpc_agent, dict):
            scan(dpc_agent, 'dpc_agent.sandbox_extensions')

        for profile_name, profile in (config_dict.get('agent_profiles') or {}).items():
            if isinstance(profile, dict):
                scan(profile, f"agent_profiles.{profile_name}.sandbox_extensions")

        return warnings

    def _repair_indexed_paths(self, rules_dict: Dict[str, Any],
                              guess_renames: bool = False) -> List[Tuple[str, str]]:
        """Re-attach index flags to the access paths they belong to, in place.

        The UI writes an index flag as a copy of the access-path string, so editing a
        path strands the old spelling in `indexed_paths` where it matches nothing and
        the root stops being indexed without a word. Repairing on save keeps the file
        honest; `collect_extended_files` repairs the same way at read time, so an
        unsaved config still indexes correctly.
        """
        from dpc_client_core.dpc_agent.extended_paths_index import reconcile_indexed_paths

        scopes: List[Tuple[str, Dict[str, Any]]] = []
        global_sandbox = (rules_dict.get('dpc_agent') or {}).get('sandbox_extensions')
        if isinstance(global_sandbox, dict):
            scopes.append(("dpc_agent", global_sandbox))
        for name, profile in (rules_dict.get('agent_profiles') or {}).items():
            sandbox = (profile or {}).get('sandbox_extensions')
            if isinstance(sandbox, dict):
                scopes.append((name, sandbox))

        report: List[Tuple[str, str]] = []
        for scope, sandbox in scopes:
            indexed = sandbox.get('indexed_paths')
            if not isinstance(indexed, list) or not indexed:
                continue
            repaired, changes = reconcile_indexed_paths(sandbox, indexed, guess_renames)
            if changes:
                sandbox['indexed_paths'] = repaired
                report.extend((scope, line) for line in changes)
        return report

    def save_rules_from_dict(self, rules_dict: Dict[str, Any]) -> Tuple[bool, str, List[str]]:
        """Validate, write, and reload rules from a dict.

        Rolls back file on failed reload so disk and runtime stay consistent.

        Returns:
            (success, message, errors)
        """
        is_valid, errors = self.validate_config(rules_dict)
        if not is_valid:
            return (False, "Validation failed", errors)

        path_warnings = self.find_missing_sandbox_paths(rules_dict)
        for warning in path_warnings:
            logger.warning("Firewall save: %s", warning)

        for scope, line in self._repair_indexed_paths(rules_dict):
            logger.warning("Firewall save: %s: %s", scope, line)

        backup = self.access_file_path.read_text() if self.access_file_path.exists() else None

        rules_text = json.dumps(rules_dict, indent=2)
        self.access_file_path.write_text(rules_text)

        success, message = self.reload()
        if not success and backup is not None:
            self.access_file_path.write_text(backup)
            logger.warning("Rolled back firewall rules after failed reload")

        if success and path_warnings:
            message = f"{message} — {len(path_warnings)} path(s) do not exist:\n" + "\n".join(path_warnings)

        return (success, message, [])


# --- Self-testing block ---
if __name__ == '__main__':
    dummy_rules = {
        "file_groups": {
            "work": ["work_*.json"],
            "personal": ["personal.json"]
        },
        "node_groups": {
            "colleagues": ["dpc-node-alice-123", "dpc-node-bob-456"],
            "friends": ["dpc-node-boris-xyz"]
        },
        "hub": {
            "personal.json:profile.name": "allow",
            "work_main.json:skills.python": "allow"
        },
        "ai_scopes": {
            "work": {
                "@work:*": "allow",
                "@personal:profile.*": "deny"
            }
        },
        "groups": {
            "colleagues": {
                "work_main.json:availability": "allow",
                "work_main.json:skills.*": "allow"
            }
        },
        "nodes": {
            "dpc-node-boris-xyz": {
                "personal.json:*": "allow",
                "work_main.json:public_summary": "allow",
                "work_main.json:internal_notes": "deny"
            }
        }
    }

    test_file = Path("test_access.json")
    test_file.write_text(json.dumps(dummy_rules, indent=2))

    firewall = ContextFirewall(test_file)

    print("--- Testing Firewall Logic (JSON version) ---")

    # Test Hub access
    assert firewall.can_access("hub", "personal.json:profile.name") == True
    assert firewall.can_access("hub", "personal.json:profile.age") == False
    print("[PASS] Hub tests passed.")

    # Test AI Scope access
    assert firewall.can_access("ai_scope:work", "work_main.json:availability") == True
    assert firewall.can_access("ai_scope:work", "work_project_alpha.json:details") == True
    assert firewall.can_access("ai_scope:work", "personal.json:profile.name") == False  # Denied by specific rule
    print("[PASS] AI Scope tests passed.")

    # Test Node access (specificity)
    assert firewall.can_access("dpc-node-boris-xyz", "personal.json:profile.name") == True
    assert firewall.can_access("dpc-node-boris-xyz", "work_main.json:public_summary") == True
    assert firewall.can_access("dpc-node-boris-xyz", "work_main.json:internal_notes") == False  # Denied by specific rule
    print("[PASS] Node tests passed.")

    # Test Group access
    assert firewall.can_access("dpc-node-alice-123", "work_main.json:availability") == True
    assert firewall.can_access("dpc-node-alice-123", "work_main.json:skills.python") == True
    assert firewall.can_access("dpc-node-bob-456", "work_main.json:skills.javascript") == True
    assert firewall.can_access("dpc-node-alice-123", "personal.json:profile.name") == False  # No access to personal
    print("[PASS] Group tests passed.")

    # Test default deny
    assert firewall.can_access("dpc-node-carol-abc", "personal.json:profile.name") == False
    print("[PASS] Default deny test passed.")

    test_file.unlink()
    print("\nAll tests passed!")
