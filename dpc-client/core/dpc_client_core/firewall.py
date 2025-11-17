# dpc-client/core/dpc_client_core/firewall.py

import configparser
from pathlib import Path
from typing import List, Dict
import fnmatch

from dpc_protocol.pcm_core import PersonalContext # For wildcard matching

class ContextFirewall:
    """
    Parses and evaluates .dpc_access rules to control access to context data.
    """
    def __init__(self, access_file_path: Path):
        self.access_file_path = access_file_path
        self._ensure_file_exists() # Call the new method
        
        # We explicitly tell the parser to only use '=' as a delimiter.
        self.rules = configparser.ConfigParser(
            allow_no_value=True,
            delimiters=('=',) 
        )

        self.rules.optionxform = str # Make parser case-sensitive
        self.rules.read(access_file_path)

        # Parse file groups (aliases for groups of files)
        self.file_groups: Dict[str, List[str]] = {}
        if self.rules.has_section('file_groups'):
            for group_name, files_str in self.rules.items('file_groups'):
                self.file_groups[group_name] = [f.strip() for f in files_str.split(',')]

        # Parse node groups (which nodes belong to which groups)
        # Format: colleagues = dpc-node-alice-123, dpc-node-bob-456
        self.node_groups: Dict[str, List[str]] = {}
        if self.rules.has_section('node_groups'):
            for group_name, nodes_str in self.rules.items('node_groups'):
                self.node_groups[group_name] = [n.strip() for n in nodes_str.split(',')]

        # Parse compute sharing settings
        self._parse_compute_settings()

    def _parse_compute_settings(self):
        """Parse compute sharing settings from the config file."""
        self.compute_enabled = False
        self.compute_allowed_nodes: List[str] = []
        self.compute_allowed_groups: List[str] = []
        self.compute_allowed_models: List[str] = []

        if self.rules.has_section('compute'):
            # Check if compute sharing is enabled
            if self.rules.has_option('compute', 'enabled'):
                self.compute_enabled = self.rules.getboolean('compute', 'enabled')

            # Parse allowed nodes
            if self.rules.has_option('compute', 'allow_nodes'):
                nodes_str = self.rules.get('compute', 'allow_nodes')
                self.compute_allowed_nodes = [n.strip() for n in nodes_str.split(',') if n.strip()]

            # Parse allowed groups
            if self.rules.has_option('compute', 'allow_groups'):
                groups_str = self.rules.get('compute', 'allow_groups')
                self.compute_allowed_groups = [g.strip() for g in groups_str.split(',') if g.strip()]

            # Parse allowed models (empty = all models allowed)
            if self.rules.has_option('compute', 'allowed_models'):
                models_str = self.rules.get('compute', 'allowed_models')
                self.compute_allowed_models = [m.strip() for m in models_str.split(',') if m.strip()]

    def _ensure_file_exists(self):
        """Creates a default, secure .dpc_access file if one doesn't exist."""
        if not self.access_file_path.exists():
            print(f"Warning: Access control file not found at {self.access_file_path}.")
            print("Creating a default, secure template...")

            self.access_file_path.parent.mkdir(parents=True, exist_ok=True)

            default_rules = """
# D-PC Access Control File
# This file controls who can access your context data and compute resources.
# By default, all access is denied.

[hub]
# Allow the hub to see your name and description for discovery.
personal.json:profile.name = allow
personal.json:profile.description = allow

# Define node groups (which nodes belong to which groups)
# [node_groups]
# colleagues = dpc-node-alice-123, dpc-node-bob-456
# friends = dpc-node-charlie-789

# Define access rules for groups
# [group:colleagues]
# work_main.json:availability = allow
# work_main.json:skills.* = allow

# Compute sharing settings (Remote Inference)
# [compute]
# enabled = false
# allow_groups = friends
# allow_nodes = dpc-node-alice-123
# allowed_models = llama3.1:8b, llama3-70b

# Add rules for specific nodes below.
# Example for a friend:
# [node:dpc-node-friend-id-here]
# personal.json:profile.* = allow
# personal.json:name = allow
# personal.json:bio = allow
# personal.json:skills = allow
"""

            self.access_file_path.write_text(default_rules)
            print(f"Default access control file created at {self.access_file_path}")

    def _get_rule_for_resource(self, section: str, resource_path: str) -> str | None:
        """
        Finds the most specific rule for a given resource in a section,
        handling file groups and wildcards correctly.
        """
        if not self.rules.has_section(section):
            return None

        parts = resource_path.split(':', 1)
        if len(parts) != 2:
            return None # Invalid resource path format
        
        target_filename, target_json_path = parts

        # Get all rules for the section
        section_rules = self.rules.items(section)

        best_match_rule = None
        best_match_specificity = -1

        for pattern, value in section_rules:
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
            if node_id in node_list:
                groups.append(group_name)
        return groups

    def can_access(self, requester_identity: str, resource_path: str) -> bool:
        """
        Checks if a requester has access to a specific resource path.
        The order of precedence is: Node > Group > Hub / AI Scope > Default (deny).
        """
        # 1. Check for a specific node rule (e.g., [node:dpc-node-boris-xyz])
        node_section = f"node:{requester_identity}"
        rule = self._get_rule_for_resource(node_section, resource_path)
        if rule:
            return rule.lower() == 'allow'

        # 2. Check for group rules (e.g., [group:colleagues])
        # Get all groups this node belongs to
        groups = self._get_groups_for_node(requester_identity)
        for group_name in groups:
            group_section = f"group:{group_name}"
            rule = self._get_rule_for_resource(group_section, resource_path)
            if rule:
                return rule.lower() == 'allow'

        # 3. Check for a hub rule or AI scope rule
        if requester_identity == "hub" or requester_identity.startswith("ai_scope:"):
            rule = self._get_rule_for_resource(requester_identity, resource_path)
            if rule:
                return rule.lower() == 'allow'

        # 4. Default to deny if no specific allow rule is found
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
        from dataclasses import asdict, fields
        from copy import deepcopy

        # Convert context to dict for manipulation
        context_dict = asdict(context)
        filtered_dict = {}

        # Check each field against firewall rules
        for field in fields(context):
            field_name = field.name
            field_value = context_dict.get(field_name)

            # Skip if field is None or empty
            if field_value is None:
                filtered_dict[field_name] = None
                continue

            # Check if peer can access this field
            resource_path = f"personal.json:{field_name}"

            if self.can_access(peer_id, resource_path):
                # Peer has access to this field
                filtered_dict[field_name] = deepcopy(field_value)
            else:
                # Check for wildcard access (e.g., personal.json:*)
                wildcard_path = "personal.json:*"
                if self.can_access(peer_id, wildcard_path):
                    filtered_dict[field_name] = deepcopy(field_value)
                else:
                    # No access - set to None or empty value
                    if isinstance(field_value, list):
                        filtered_dict[field_name] = []
                    elif isinstance(field_value, dict):
                        filtered_dict[field_name] = {}
                    else:
                        filtered_dict[field_name] = None

        # Create new PersonalContext from filtered dict
        from dpc_protocol.pcm_core import PersonalContext
        return PersonalContext(**filtered_dict)

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
        from copy import deepcopy

        def filter_nested_dict(data: Dict, path_prefix: str) -> Dict:
            """Recursively filter nested dict based on firewall rules."""
            if not isinstance(data, dict):
                return data

            filtered = {}
            for key, value in data.items():
                current_path = f"{path_prefix}.{key}" if path_prefix else key
                resource_path = f"device_context.json:{current_path}"

                # Check if peer has access to this specific path
                if self.can_access(peer_id, resource_path):
                    if isinstance(value, dict):
                        # Allow access - but still recursively filter in case there are deny rules below
                        filtered[key] = filter_nested_dict(value, current_path)
                        # If nothing was allowed in the subtree, use the whole value
                        if not filtered[key] and value:
                            filtered[key] = deepcopy(value)
                    else:
                        # Leaf node - allow access
                        filtered[key] = deepcopy(value)
                else:
                    # Check for wildcard access (e.g., device_context.json:hardware.gpu.*)
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

            # Check if peer has access to entire section
            if self.can_access(peer_id, resource_path):
                filtered_context[section_name] = deepcopy(section_data)
            else:
                # Check for wildcard access to section
                wildcard_path = f"device_context.json:{section_name}.*"
                if self.can_access(peer_id, wildcard_path):
                    filtered_context[section_name] = deepcopy(section_data)
                else:
                    # Recursively filter section contents
                    filtered_section = filter_nested_dict(section_data, section_name)
                    if filtered_section:  # Only include if not empty
                        filtered_context[section_name] = filtered_section

        return filtered_context

# --- Self-testing block ---
if __name__ == '__main__':
    dummy_rules = """
[file_groups]
    work = work_*.json
    personal = personal.json

[node_groups]
    colleagues = dpc-node-alice-123, dpc-node-bob-456
    friends = dpc-node-boris-xyz

[hub]
    personal.json:profile.name = allow
    work_main.json:skills.python = allow

[ai_scope:work]
    @work:* = allow
    @personal:profile.* = deny

[group:colleagues]
    work_main.json:availability = allow
    work_main.json:skills.* = allow

[node:dpc-node-boris-xyz]
    personal.json:* = allow
    work_main.json:public_summary = allow
    work_main.json:internal_notes = deny
"""
    test_file = Path("test_access.ini")
    test_file.write_text(dummy_rules)

    firewall = ContextFirewall(test_file)

    print("--- Testing Firewall Logic (v2) ---")
    
    # Test Hub access
    assert firewall.can_access("hub", "personal.json:profile.name") == True
    assert firewall.can_access("hub", "personal.json:profile.age") == False
    print("[PASS] Hub tests passed.")

    # Test AI Scope access
    assert firewall.can_access("ai_scope:work", "work_main.json:availability") == True
    assert firewall.can_access("ai_scope:work", "work_project_alpha.json:details") == True
    assert firewall.can_access("ai_scope:work", "personal.json:profile.name") == False # Denied by specific rule
    print("[PASS] AI Scope tests passed.")

    # Test Node access (specificity)
    assert firewall.can_access("dpc-node-boris-xyz", "personal.json:profile.name") == True
    assert firewall.can_access("dpc-node-boris-xyz", "work_main.json:public_summary") == True
    assert firewall.can_access("dpc-node-boris-xyz", "work_main.json:internal_notes") == False # Denied by specific rule
    print("[PASS] Node tests passed.")

    # Test Group access (NEW!)
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