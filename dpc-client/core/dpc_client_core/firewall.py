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
        logger.debug("Compute sharing settings updated: enabled=%s, allowed_nodes=%d, allowed_groups=%d, allowed_models=%d",
                     self.compute_enabled, len(self.compute_allowed_nodes),
                     len(self.compute_allowed_groups), len(self.compute_allowed_models))

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

    def _parse_dpc_agent_settings(self):
        """Parse DPC agent settings from the config."""
        dpc_agent = self.rules.get('dpc_agent', {})
        self.dpc_agent_enabled = dpc_agent.get('enabled', True)
        self.dpc_agent_personal_context_access = dpc_agent.get('personal_context_access', True)
        self.dpc_agent_device_context_access = dpc_agent.get('device_context_access', True)
        self.dpc_agent_knowledge_access = dpc_agent.get('knowledge_access', 'read_only')

        # All available tools with their default values.
        # True = enabled by default, False = disabled by default (security).
        # When adding a new tool: add it here with default enabled/disabled.
        all_tools_defaults = {
            # File operations (unified read_file/write_file — S31)
            'read_file': True,   # Read from sandbox or firewall-checked extended paths
            'write_file': False,  # Write to sandbox or firewall-checked extended paths
            'repo_list': True,
            'repo_delete': False,  # Can delete files/directories in sandbox
            'drive_list': False,
            'search_files': True,  # Safe - read-only search
            'search_in_file': True,  # Safe - read-only search
            # Extended sandbox (v0.16.0+)
            'extended_path_list': False,
            'list_extended_sandbox_paths': True,  # Safe - just lists config
            # Memory/identity
            'update_scratchpad': True,
            'update_identity': True,
            'chat_history': True,
            'deduplicate_identity': True,
            # Knowledge
            'knowledge_read': True,
            'knowledge_write': False,  # Controlled by knowledge_access
            'knowledge_list': True,
            'get_task_board': True,  # Read task history + learning progress
            # Memento-Skills tools (v0.20.0+)
            'execute_skill': True,  # Load skill strategy by name
            # Inter-agent skill sharing tools (v0.21.0+)
            'list_local_agents': True,  # Read-only: list registered agents
            'list_agent_skills': True,  # Read-only: list another agent's skills
            'import_skill_from_agent': False,  # Opt-in: needs accept_peer_skills
            # Self-introspection tools
            'list_my_tools': True,  # Read-only: list own available tools
            'list_my_skills': True,  # Read-only: list own installed skills
            # DPC integration
            'get_dpc_context': True,
            # Web tools
            'browse_page': True,
            'fetch_json': True,
            'extract_links': True,
            'check_url': True,
            'search_web': True,
            # Review tools (safe, analysis only)
            'self_review': True,
            'request_critique': True,
            'compare_approaches': True,
            'quality_checklist': True,
            'consensus_check': True,
            # Git tools (read-only)
            'git_status': False,
            'git_diff': False,
            'git_log': False,
            'git_branch': False,
            # Git tools (modify files / history)
            'git_add': False,
            'git_commit': False,
            'git_init': False,
            'git_checkout': False,
            'git_merge': False,
            'git_tag': False,
            'git_reset': False,
            'git_snapshot': False,
            'repo_commit_push': False,  # Can push to remote
            # Restricted tools (security sensitive)
            'run_shell': False,
            'claude_code_edit': False,
            # Task queue tools (v0.16.0+)
            'schedule_task': True,  # Safe, just scheduling
            'get_task_status': True,  # Read-only
            # Evolution tools (v0.16.0+)
            'pause_evolution': True,  # Control, doesn't modify files
            'resume_evolution': True,  # Control, doesn't modify files
            'get_evolution_stats': True,  # Read-only
            'approve_evolution_change': False,  # Modifies files
            'reject_evolution_change': True,  # Safe, just removes pending change
            # Messaging tools (v0.18.0+)
            'send_user_message': True,  # Agent-initiated Telegram messages
            # Task type management tools (v0.18.0+)
            'register_task_type': True,
            'list_task_types': True,  # Read-only
            'unregister_task_type': True,
            # Session archive tools (v0.22.0+ - read-only access to own history)
            'read_session_archive': True,  # Read session summaries
            'read_session_detail': True,  # Read session messages
        }

        # Parse tool permissions from config, using defaults for missing tools
        # Legacy tool name mapping (S31: 6 tools merged into read_file/write_file)
        _legacy_tool_map = {
            'read_file': ['repo_read', 'extended_path_read', 'drive_read'],
            'write_file': ['repo_write_commit', 'extended_path_write', 'drive_write'],
        }
        tools = dpc_agent.get('tools', {})
        self.dpc_agent_tools: Dict[str, bool] = {}
        for tool_name, default_enabled in all_tools_defaults.items():
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
        # Extended path access gates (S31 — UI checkboxes)
        self.extended_read_enabled: bool = sandbox_extensions.get('extended_read_enabled', True)
        self.extended_write_enabled: bool = sandbox_extensions.get('extended_write_enabled', False)

        # Validate and normalize paths
        self.sandbox_read_only_paths = [self._normalize_path(p) for p in self.sandbox_read_only_paths if p]
        self.sandbox_read_write_paths = [self._normalize_path(p) for p in self.sandbox_read_write_paths if p]

        # Parse evolution settings (v0.17.0+)
        evolution = dpc_agent.get('evolution', {})
        self.evolution_enabled = evolution.get('enabled', False)
        self.evolution_interval_minutes = evolution.get('interval_minutes', 60)
        self.evolution_auto_apply = evolution.get('auto_apply', False)

        # Parse consciousness settings (v0.23.0+)
        consciousness = dpc_agent.get('consciousness', {})
        self.consciousness_enabled = consciousness.get('enabled', False)
        self.consciousness_think_interval_min = consciousness.get('think_interval_min', 60)
        self.consciousness_think_interval_max = consciousness.get('think_interval_max', 300)
        self.consciousness_budget_fraction = consciousness.get('budget_fraction', 0.1)

        # Parse history settings (v0.22.0+)
        history = dpc_agent.get('history', {})
        self.history_preserve_on_reset = history.get('preserve_on_reset', True)
        self.history_max_archived_sessions = max(1, min(200, int(history.get('max_archived_sessions', 40))))

        logger.debug("DPC Agent settings updated: enabled=%s, personal=%s, device=%s, knowledge=%s, tools_count=%d, sandbox_extensions=%d, evolution=%s, consciousness=%s",
                     self.dpc_agent_enabled,
                     self.dpc_agent_personal_context_access,
                     self.dpc_agent_device_context_access,
                     self.dpc_agent_knowledge_access,
                     len([t for t in self.dpc_agent_tools.values() if t]),
                     len(self.sandbox_read_only_paths) + len(self.sandbox_read_write_paths),
                     self.evolution_enabled,
                     self.consciousness_enabled)

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
            *keys: Sequence of dict keys to traverse (e.g. 'evolution', 'enabled')
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
            if allowed_path and (
                normalized == allowed_path
                or normalized.startswith(allowed_path + os.sep)
                or normalized.startswith(allowed_path + "/")
            ):
                return True

        # If write is required, read_only paths are not sufficient
        if require_write:
            return False

        # Check read_only paths
        for allowed_path in ro_paths:
            if allowed_path and (
                normalized == allowed_path
                or normalized.startswith(allowed_path + os.sep)
                or normalized.startswith(allowed_path + "/")
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

        if context_type == 'personal':
            return bool(self._get_profile_or_global(
                profile_name, 'personal_context_access',
                default=self.dpc_agent_personal_context_access))
        elif context_type == 'device':
            return bool(self._get_profile_or_global(
                profile_name, 'device_context_access',
                default=self.dpc_agent_device_context_access))
        elif context_type == 'knowledge':
            ka = self._get_profile_or_global(
                profile_name, 'knowledge_access',
                default=self.dpc_agent_knowledge_access)
            return ka != 'none'

        return False

    def can_agent_write_knowledge(self, profile_name: Optional[str] = None) -> bool:
        """Check if the agent can write to knowledge base."""
        if not self.dpc_agent_enabled:
            return False
        ka = self._get_profile_or_global(
            profile_name, 'knowledge_access',
            default=self.dpc_agent_knowledge_access)
        return ka == 'read_write'

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

        # Override: knowledge_write requires read_write access
        if self.dpc_agent_knowledge_access != 'read_write':
            allowed.discard('knowledge_write')

        # Override: import_skill_from_agent requires accept_peer_skills
        if not self.get_agent_skill_permission('accept_peer_skills'):
            allowed.discard('import_skill_from_agent')

        return allowed

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
                "knowledge_access": self.dpc_agent_knowledge_access,
                "evolution_enabled": self.evolution_enabled,
                "consciousness_enabled": self.consciousness_enabled,
            },
            "archive_access": True,  # read_session_archive is a core tool, always available
        }

    def get_allowed_agent_tools_for_profile(self, profile_name: str) -> Set[str]:
        """
        Get allowed tools for a specific agent profile.

        Uses global dpc_agent tools as the baseline, then applies per-profile overrides.
        Also enforces per-profile knowledge_access, personal_context_access, and skills
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

        # Per-profile overrides mirroring get_allowed_agent_tools()
        personal_access = profile.get('personal_context_access', self.dpc_agent_personal_context_access)
        if not personal_access:
            allowed.discard('get_dpc_context')

        knowledge_access = profile.get('knowledge_access', self.dpc_agent_knowledge_access)
        if knowledge_access != 'read_write':
            allowed.discard('knowledge_write')

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
                'personal_context_access': True,
                'device_context_access': True,
                'knowledge_access': 'read_only',
                'tools': {
                    'read_file': True,
                    'write_file': False,
                    'repo_list': True,
                    'repo_delete': False,
                    'update_scratchpad': True,
                    'browse_page': True,
                    'search_web': True,
                    'search_files': True,
                    'search_in_file': True,
                },
                'evolution': {
                    'enabled': False,
                    'interval_minutes': 60,
                    'auto_apply': False,
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
                    "allowed_models": ["llama3.1:8b", "llama3:70b"]
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
                    "_comment": "DPC Agent permissions - Control what the embedded AI agent can access",
                    "enabled": True,
                    "personal_context_access": True,
                    "device_context_access": True,
                    "knowledge_access": "read_only",
                    "evolution": {
                        "_comment": "Evolution settings - autonomous self-modification within sandbox",
                        "enabled": False,
                        "interval_minutes": 60,
                        "auto_apply": False
                    },
                    "tools": {
                        "_comment": "Enable/disable individual tools. True=allowed, False=blocked",
                        "read_file": True,
                        "write_file": False,
                        "repo_list": True,
                        "repo_delete": False,
                        "drive_list": False,
                        "update_scratchpad": True,
                        "update_identity": True,
                        "chat_history": True,
                        "knowledge_read": True,
                        "knowledge_write": False,
                        "knowledge_list": True,
                        "get_task_board": True,
                        "get_dpc_context": True,
                        "browse_page": True,
                        "fetch_json": True,
                        "extract_links": True,
                        "check_url": True,
                        "search_web": True,
                        "self_review": True,
                        "request_critique": True,
                        "compare_approaches": True,
                        "quality_checklist": True,
                        "consensus_check": True,
                        "git_status": False,
                        "git_diff": False,
                        "git_log": False,
                        "git_add": False,
                        "git_commit": False,
                        "git_branch": False,
                        "git_init": False,
                        "git_checkout": False,
                        "git_merge": False,
                        "git_tag": False,
                        "git_reset": False,
                        "git_snapshot": False,
                        "repo_commit_push": False,
                        "run_shell": False,
                        "claude_code_edit": False,
                        "_comment_task": "Task queue tools - safe scheduling and status checks",
                        "schedule_task": True,
                        "get_task_status": True,
                        "_comment_evolution": "Evolution tools - control agent self-modification",
                        "pause_evolution": True,
                        "resume_evolution": True,
                        "get_evolution_stats": True,
                        "approve_evolution_change": False,
                        "reject_evolution_change": True,
                        "_comment_skills": "Skill and introspection tools",
                        "execute_skill": True,
                        "list_local_agents": True,
                        "list_agent_skills": True,
                        "import_skill_from_agent": False,
                        "list_my_tools": True,
                        "list_my_skills": True
                    }
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

    def can_request_inference(self, requester_node_id: str, model: str = None) -> bool:
        """
        Checks if a peer can request remote inference on this node.

        Args:
            requester_node_id: The node_id of the requesting peer
            model: Optional model name to check if allowed

        Returns:
            True if the peer can request inference (and use the specified model if provided)
        """
        # Check if compute sharing is enabled
        if not self.compute_enabled:
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

                    if 'knowledge_access' in dpc_agent:
                        valid_access = ['none', 'read_only', 'read_write']
                        if dpc_agent['knowledge_access'] not in valid_access:
                            errors.append(f"'dpc_agent.knowledge_access' must be one of: {valid_access}")

                    if 'tools' in dpc_agent:
                        tools = dpc_agent['tools']
                        if not isinstance(tools, dict):
                            errors.append("'dpc_agent.tools' must be a dictionary")
                        else:
                            # All valid tool names
                            valid_tools = {
                                # File operations (unified S31)
                                'read_file', 'write_file', 'repo_list', 'repo_delete',
                                'drive_list',
                                # Memory/identity
                                'update_scratchpad', 'update_identity', 'chat_history',
                                # Knowledge
                                'knowledge_read', 'knowledge_write', 'knowledge_list',
                                'get_task_board',
                                # DPC integration
                                'get_dpc_context',
                                # Web tools
                                'browse_page', 'fetch_json', 'extract_links', 'check_url', 'search_web',
                                # Review tools
                                'self_review', 'request_critique', 'compare_approaches', 'quality_checklist', 'consensus_check',
                                # Git tools
                                'git_status', 'git_diff', 'git_log', 'git_add', 'git_commit', 'git_branch', 'git_init',
                                'git_checkout', 'git_merge', 'git_tag', 'git_reset', 'git_snapshot',
                                'repo_commit_push',
                                # Restricted tools
                                'run_shell', 'claude_code_edit',
                                # Task queue tools (v0.16.0+)
                                'schedule_task', 'get_task_status',
                                # Evolution tools (v0.16.0+)
                                'pause_evolution', 'resume_evolution', 'get_evolution_stats',
                                'approve_evolution_change', 'reject_evolution_change',
                                # Search tools (v0.16.0+)
                                'search_files', 'search_in_file',
                                # Extended sandbox tools (v0.16.0+ — read/write merged into read_file/write_file S31)
                                'extended_path_list',
                                'list_extended_sandbox_paths',
                                # Messaging tools (v0.18.0+)
                                'send_user_message',
                                # Knowledge tools (v0.18.0+)
                                'deduplicate_identity',
                                # Task type management tools (v0.18.0+)
                                'register_task_type', 'list_task_types', 'unregister_task_type',
                                # Memento-Skills tools (v0.20.0+)
                                'execute_skill',
                                # Inter-agent skill sharing tools (v0.21.0+)
                                'list_local_agents', 'list_agent_skills', 'import_skill_from_agent',
                                # Self-introspection tools
                                'list_my_tools', 'list_my_skills',
                                # Legacy aliases (S31 migration — accepted but mapped to read_file/write_file)
                                'repo_read', 'repo_write_commit', 'drive_read', 'drive_write',
                                'extended_path_read', 'extended_path_write',
                            }
                            for tool_name, tool_enabled in tools.items():
                                if tool_name.startswith('_'):
                                    continue  # Skip comments
                                if tool_name not in valid_tools:
                                    errors.append(f"Unknown tool in dpc_agent.tools: '{tool_name}'")
                                if not isinstance(tool_enabled, bool):
                                    errors.append(f"'dpc_agent.tools.{tool_name}' must be a boolean")

                    # Validate evolution settings (v0.17.0+)
                    if 'evolution' in dpc_agent:
                        evolution = dpc_agent['evolution']
                        if not isinstance(evolution, dict):
                            errors.append("'dpc_agent.evolution' must be a dictionary")
                        else:
                            if 'enabled' in evolution and not isinstance(evolution['enabled'], bool):
                                errors.append("'dpc_agent.evolution.enabled' must be a boolean")
                            if 'interval_minutes' in evolution:
                                if not isinstance(evolution['interval_minutes'], int):
                                    errors.append("'dpc_agent.evolution.interval_minutes' must be an integer")
                                elif evolution['interval_minutes'] < 1:
                                    errors.append("'dpc_agent.evolution.interval_minutes' must be at least 1")
                            if 'auto_apply' in evolution and not isinstance(evolution['auto_apply'], bool):
                                errors.append("'dpc_agent.evolution.auto_apply' must be a boolean")

                    # Validate consciousness settings (v0.23.0+)
                    if 'consciousness' in dpc_agent:
                        consciousness = dpc_agent['consciousness']
                        if not isinstance(consciousness, dict):
                            errors.append("'dpc_agent.consciousness' must be a dictionary")
                        else:
                            if 'enabled' in consciousness and not isinstance(consciousness['enabled'], bool):
                                errors.append("'dpc_agent.consciousness.enabled' must be a boolean")
                            if 'think_interval_min' in consciousness:
                                if not isinstance(consciousness['think_interval_min'], int):
                                    errors.append("'dpc_agent.consciousness.think_interval_min' must be an integer")
                                elif consciousness['think_interval_min'] < 10:
                                    errors.append("'dpc_agent.consciousness.think_interval_min' must be at least 10")
                            if 'think_interval_max' in consciousness:
                                if not isinstance(consciousness['think_interval_max'], int):
                                    errors.append("'dpc_agent.consciousness.think_interval_max' must be an integer")
                            if 'budget_fraction' in consciousness:
                                val = consciousness['budget_fraction']
                                if not isinstance(val, (int, float)) or val <= 0 or val > 1:
                                    errors.append("'dpc_agent.consciousness.budget_fraction' must be a number between 0 and 1")

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
                            if 'knowledge_access' in profile_config:
                                valid_access = ['none', 'read_only', 'read_write']
                                if profile_config['knowledge_access'] not in valid_access:
                                    errors.append(f"'agent_profiles.{profile_name}.knowledge_access' must be one of: {valid_access}")
                            # Validate tools
                            if 'tools' in profile_config:
                                tools = profile_config['tools']
                                if not isinstance(tools, dict):
                                    errors.append(f"'agent_profiles.{profile_name}.tools' must be a dictionary")
                                else:
                                    # Use the same valid tools as dpc_agent
                                    valid_tools = {
                                        'read_file', 'write_file', 'repo_list', 'repo_delete',
                                        'drive_list',
                                        'update_scratchpad', 'update_identity', 'chat_history',
                                        'knowledge_read', 'knowledge_write', 'knowledge_list',
                                        'get_task_board',
                                        'get_dpc_context',
                                        'browse_page', 'fetch_json', 'extract_links', 'check_url', 'search_web',
                                        'self_review', 'request_critique', 'compare_approaches', 'quality_checklist', 'consensus_check',
                                        'git_status', 'git_diff', 'git_log', 'git_add', 'git_commit', 'git_branch', 'git_init',
                                        'git_checkout', 'git_merge', 'git_tag', 'git_reset', 'git_snapshot',
                                        'repo_commit_push',
                                        'run_shell', 'claude_code_edit',
                                        'schedule_task', 'get_task_status',
                                        'pause_evolution', 'resume_evolution', 'get_evolution_stats',
                                        'approve_evolution_change', 'reject_evolution_change',
                                        'search_files', 'search_in_file',
                                        'extended_path_list',
                                        'list_extended_sandbox_paths',
                                        'send_user_message',
                                        'deduplicate_identity',
                                        'register_task_type', 'list_task_types', 'unregister_task_type',
                                        # Memento-Skills tools (v0.20.0+)
                                        'execute_skill',
                                        # Inter-agent skill sharing tools (v0.21.0+)
                                        'list_local_agents', 'list_agent_skills', 'import_skill_from_agent',
                                        # Self-introspection tools
                                        'list_my_tools', 'list_my_skills',
                                        # Legacy aliases (S31 migration)
                                        'repo_read', 'repo_write_commit', 'drive_read', 'drive_write',
                                        'extended_path_read', 'extended_path_write',
                                    }
                                    for tool_name, tool_enabled in tools.items():
                                        if tool_name.startswith('_'):
                                            continue  # Skip comments
                                        if tool_name not in valid_tools:
                                            errors.append(f"Unknown tool in agent_profiles.{profile_name}.tools: '{tool_name}'")
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
