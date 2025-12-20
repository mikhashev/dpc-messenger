# dpc-client/core/dpc_client_core/firewall.py

import json
import logging
from pathlib import Path
from typing import List, Dict, Tuple, Any
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
        self._migrate_from_old_filename()
        self._ensure_file_exists()
        self._load_rules()

    def _migrate_from_old_filename(self):
        """Migrate from old .dpc_access.json to new privacy_rules.json filename."""
        old_path = self.access_file_path.parent / ".dpc_access.json"

        # Only migrate if old file exists and new file doesn't
        if old_path.exists() and not self.access_file_path.exists():
            logger.info("Migrating %s to %s", old_path.name, self.access_file_path.name)
            try:
                old_path.rename(self.access_file_path)
                logger.info("Migration successful")
            except Exception as e:
                logger.error("Error migrating privacy rules file: %s", e, exc_info=True)

    def _load_rules(self):
        """Load and parse rules from JSON file."""
        try:
            rules_text = self.access_file_path.read_text()
            self.rules: Dict[str, Any] = json.loads(rules_text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in firewall rules: {e}")
        except Exception as e:
            raise ValueError(f"Failed to load firewall rules: {e}")

        # Parse file groups (aliases for groups of files)
        self.file_groups: Dict[str, List[str]] = self.rules.get('file_groups', {})

        # Parse node groups (which nodes belong to which groups)
        self.node_groups: Dict[str, List[str]] = self.rules.get('node_groups', {})

        # Parse compute sharing settings
        self._parse_compute_settings()

        # Parse notification settings
        self._parse_notification_settings()

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
                "_comment": "D-PC Access Control File - This file controls who can access your context data and compute resources. By default, all access is denied.",
                "hub": {
                    "personal.json:profile.name": "allow",
                    "personal.json:profile.description": "allow"
                },
                "node_groups": {
                    "_comment": "Define which nodes belong to which groups",
                    "_example_colleagues": ["dpc-node-alice-123", "dpc-node-bob-456"],
                    "_example_friends": ["dpc-node-charlie-789"]
                },
                "file_groups": {
                    "_comment": "Define aliases for groups of files",
                    "_example_work": ["work_*.json"],
                    "_example_personal": ["personal.json"]
                },
                "compute": {
                    "_comment": "Compute sharing settings (Remote Inference)",
                    "enabled": False,
                    "allow_groups": [],
                    "allow_nodes": [],
                    "allowed_models": []
                },
                "nodes": {
                    "_comment": "Access rules for specific nodes",
                    "_example_dpc-node-friend-id-here": {
                        "personal.json:profile.*": "allow",
                        "personal.json:name": "allow",
                        "personal.json:bio": "allow"
                    }
                },
                "groups": {
                    "_comment": "Access rules for groups of nodes",
                    "_example_colleagues": {
                        "work_main.json:availability": "allow",
                        "work_main.json:skills.*": "allow"
                    }
                },
                "ai_scopes": {
                    "_comment": "Access rules for AI scopes",
                    "_example_work": {
                        "@work:*": "allow"
                    }
                },
                "device_sharing": {
                    "_comment": "Device context sharing rules",
                    "_example_basic": {
                        "device_context.json:hardware.gpu.*": "allow"
                    }
                },
                "notifications": {
                    "_comment": "Desktop notification settings",
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
                "file_transfer": {
                    "_comment": "File transfer permissions (v0.11.0+)",
                    "groups": {},
                    "nodes": {}
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

        # 2. Check for group rules
        # Get all groups this node belongs to
        if requester_identity.startswith('dpc-node-'):
            groups = self._get_groups_for_node(requester_identity)
            for group_name in groups:
                rule = self._get_rule_for_resource('groups', group_name, resource_path)
                if rule:
                    return rule.lower() == 'allow'

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
                # Try group rules if no node rule
                if not specific_rule:
                    groups = self._get_groups_for_node(peer_id)
                    for group_name in groups:
                        specific_rule = self._get_rule_for_resource('groups', group_name, resource_path)
                        if specific_rule:
                            break

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
                        for group_name in groups:
                            specific_rule = self._get_rule_for_resource('groups', group_name, resource_path)
                            if specific_rule:
                                break

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
                    for group_name in groups:
                        specific_rule = self._get_rule_for_resource('groups', group_name, resource_path)
                        if specific_rule:
                            break

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
            valid_top_level_keys = ['hub', 'node_groups', 'file_groups', 'compute', 'nodes', 'groups', 'ai_scopes', 'device_sharing', 'file_transfer', 'notifications', '_comment']

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
