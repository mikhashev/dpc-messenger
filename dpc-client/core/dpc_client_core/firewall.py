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
        
        self.file_groups: Dict[str, List[str]] = {}
        if self.rules.has_section('file_groups'):
            for group_name, files_str in self.rules.items('file_groups'):
                self.file_groups[group_name] = [f.strip() for f in files_str.split(',')]

    def _ensure_file_exists(self):
        """Creates a default, secure .dpc_access file if one doesn't exist."""
        if not self.access_file_path.exists():
            print(f"Warning: Access control file not found at {self.access_file_path}.")
            print("Creating a default, secure template...")
            
            self.access_file_path.parent.mkdir(parents=True, exist_ok=True)
            
            default_rules = """
# D-PC Access Control File
# This file controls who can access your context data.
# By default, all access is denied.

[hub]
# Allow the hub to see your name and description for discovery.
personal.json:profile.name = allow
personal.json:profile.description = allow

# Add rules for friends, colleagues, etc. below.
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

        # 2. TODO: Check for group rules (e.g., [group:colleagues])

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

# --- Self-testing block ---
if __name__ == '__main__':
    dummy_rules = """
[file_groups]
    work = work_*.json
    personal = personal.json

[hub]
    personal.json:profile.name = allow
    work_main.json:skills.python = allow

[ai_scope:work]
    @work:* = allow
    @personal:profile.* = deny

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
    print("✅ Hub tests passed.")

    # Test AI Scope access
    assert firewall.can_access("ai_scope:work", "work_main.json:availability") == True
    assert firewall.can_access("ai_scope:work", "work_project_alpha.json:details") == True
    assert firewall.can_access("ai_scope:work", "personal.json:profile.name") == False # Denied by specific rule
    print("✅ AI Scope tests passed.")

    # Test Node access (specificity)
    assert firewall.can_access("dpc-node-boris-xyz", "personal.json:profile.name") == True
    assert firewall.can_access("dpc-node-boris-xyz", "work_main.json:public_summary") == True
    assert firewall.can_access("dpc-node-boris-xyz", "work_main.json:internal_notes") == False # Denied by specific rule
    print("✅ Node tests passed.")
    
    # Test default deny
    assert firewall.can_access("dpc-node-carol-abc", "personal.json:profile.name") == False
    print("✅ Default deny test passed.")

    test_file.unlink()
    print("\nAll tests passed!")