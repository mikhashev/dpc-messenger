# tests/test_firewall.py

import pytest
from pathlib import Path
from dpc_client_core.firewall import ContextFirewall


class TestDeviceContextFiltering:
    """Test device context filtering with firewall rules."""

    @pytest.fixture
    def sample_device_context(self):
        """Sample device context for testing."""
        return {
            "schema_version": "1.0",
            "hardware": {
                "cpu": {
                    "architecture": "AMD64",
                    "processor": "Intel Core i7",
                    "cores_physical": 4,
                    "cores_logical": 8
                },
                "memory": {
                    "ram_gb": 16.0,
                    "ram_tier": "16GB",
                    "total_bytes": 17179869184
                },
                "gpu": {
                    "type": "nvidia",
                    "model": "NVIDIA GeForce RTX 3060",
                    "vram_mib": 12288,
                    "vram_gb": 12.0,
                    "driver_version": "576.28",
                    "cuda_version": "12.8"
                },
                "storage": {
                    "free_gb": 500.0,
                    "total_gb": 1000.0,
                    "free_tier": "500GB+"
                }
            },
            "software": {
                "os": {
                    "family": "Windows",
                    "version": "10",
                    "build": "10.0.19045"
                },
                "runtime": {
                    "python": {
                        "version": "3.12.0",
                        "major": 3,
                        "minor": 12
                    }
                },
                "dev_tools": {
                    "git": "2.47",
                    "docker": "28.4",
                    "node": "22.14"
                },
                "package_managers": ["pip", "poetry", "npm"]
            },
            "metadata": {
                "collector_version": "1.0",
                "auto_collected": True
            }
        }

    @pytest.fixture
    def firewall_with_device_rules(self, tmp_path):
        """Create firewall with device context sharing rules."""
        rules_file = tmp_path / ".dpc_access"
        rules_content = """
[node_groups]
trusted_devs = dpc-node-alice-123, dpc-node-bob-456
compute_users = dpc-node-charlie-789

[node:dpc-node-alice-123]
# Alice can see all GPU info for compute sharing
device_context.json:hardware.gpu.* = allow
device_context.json:hardware.memory.ram_gb = allow

[node:dpc-node-bob-456]
# Bob can see dev environment but no hardware
device_context.json:software.* = allow
device_context.json:hardware.* = deny

[node:dpc-node-charlie-789]
# Charlie can see specific GPU and CPU details
device_context.json:hardware.gpu.model = allow
device_context.json:hardware.gpu.vram_gb = allow
device_context.json:hardware.cpu.cores_physical = allow

[group:trusted_devs]
# Trusted devs can see OS info
device_context.json:software.os.* = allow

[node:dpc-node-restricted-xyz]
# This node has no device context access (default deny)
"""
        rules_file.write_text(rules_content)
        return ContextFirewall(rules_file)

    def test_wildcard_gpu_access(self, firewall_with_device_rules, sample_device_context):
        """Test that Alice can access all GPU info via wildcard."""
        filtered = firewall_with_device_rules.filter_device_context_for_peer(
            sample_device_context,
            "dpc-node-alice-123"
        )

        # Should have GPU section with all fields
        assert "hardware" in filtered
        assert "gpu" in filtered["hardware"]
        assert filtered["hardware"]["gpu"]["model"] == "NVIDIA GeForce RTX 3060"
        assert filtered["hardware"]["gpu"]["vram_gb"] == 12.0
        assert filtered["hardware"]["gpu"]["cuda_version"] == "12.8"

        # Should also have memory.ram_gb
        assert "memory" in filtered["hardware"]
        assert filtered["hardware"]["memory"]["ram_gb"] == 16.0

        # Should NOT have CPU (not in rules)
        assert "cpu" not in filtered["hardware"]

    def test_software_access_hardware_deny(self, firewall_with_device_rules, sample_device_context):
        """Test that Bob can access software but not hardware."""
        filtered = firewall_with_device_rules.filter_device_context_for_peer(
            sample_device_context,
            "dpc-node-bob-456"
        )

        # Should have software section
        assert "software" in filtered
        assert "os" in filtered["software"]
        assert filtered["software"]["os"]["family"] == "Windows"
        assert "dev_tools" in filtered["software"]
        assert filtered["software"]["dev_tools"]["git"] == "2.47"

        # Should NOT have any hardware (explicitly denied)
        assert "hardware" not in filtered or filtered["hardware"] == {}

    def test_specific_field_access(self, firewall_with_device_rules, sample_device_context):
        """Test that Charlie can only access specific fields."""
        filtered = firewall_with_device_rules.filter_device_context_for_peer(
            sample_device_context,
            "dpc-node-charlie-789"
        )

        # Should have specific GPU fields
        assert "hardware" in filtered
        assert "gpu" in filtered["hardware"]
        assert filtered["hardware"]["gpu"]["model"] == "NVIDIA GeForce RTX 3060"
        assert filtered["hardware"]["gpu"]["vram_gb"] == 12.0

        # Should NOT have other GPU fields (driver_version, cuda_version, etc.)
        assert "driver_version" not in filtered["hardware"]["gpu"]
        assert "cuda_version" not in filtered["hardware"]["gpu"]

        # Should have CPU cores_physical
        assert "cpu" in filtered["hardware"]
        assert filtered["hardware"]["cpu"]["cores_physical"] == 4

        # Should NOT have other CPU fields
        assert "architecture" not in filtered["hardware"]["cpu"]
        assert "processor" not in filtered["hardware"]["cpu"]

    def test_default_deny(self, firewall_with_device_rules, sample_device_context):
        """Test that unknown nodes get nothing (default deny)."""
        filtered = firewall_with_device_rules.filter_device_context_for_peer(
            sample_device_context,
            "dpc-node-restricted-xyz"
        )

        # Should be empty or have empty sections
        assert not filtered or all(not v for v in filtered.values())

    def test_group_access(self, firewall_with_device_rules, sample_device_context):
        """Test that group rules work (Alice and Bob are in trusted_devs)."""
        # Alice should get OS info from group rule + GPU from node rule
        filtered_alice = firewall_with_device_rules.filter_device_context_for_peer(
            sample_device_context,
            "dpc-node-alice-123"
        )

        # Group rule should give access to OS
        assert "software" in filtered_alice
        assert "os" in filtered_alice["software"]
        assert filtered_alice["software"]["os"]["family"] == "Windows"

    def test_metadata_not_shared_by_default(self, firewall_with_device_rules, sample_device_context):
        """Test that metadata is not shared unless explicitly allowed."""
        filtered = firewall_with_device_rules.filter_device_context_for_peer(
            sample_device_context,
            "dpc-node-alice-123"
        )

        # Metadata should NOT be present (no rule for it)
        assert "metadata" not in filtered or filtered["metadata"] == {}


class TestContextFiltering:
    """Test personal context filtering (existing functionality)."""

    @pytest.fixture
    def firewall_with_context_rules(self, tmp_path):
        """Create firewall with personal context sharing rules."""
        rules_file = tmp_path / ".dpc_access"
        rules_content = """
[hub]
personal.json:profile.name = allow
personal.json:profile.description = allow

[node:dpc-node-friend-xyz]
personal.json:profile.* = allow
personal.json:knowledge.* = allow
"""
        rules_file.write_text(rules_content)
        return ContextFirewall(rules_file)

    def test_hub_access(self, firewall_with_context_rules):
        """Test that Hub can only access allowed profile fields."""
        assert firewall_with_context_rules.can_access("hub", "personal.json:profile.name") == True
        assert firewall_with_context_rules.can_access("hub", "personal.json:profile.description") == True
        assert firewall_with_context_rules.can_access("hub", "personal.json:profile.email") == False
        assert firewall_with_context_rules.can_access("hub", "personal.json:knowledge.skills") == False

    def test_friend_access(self, firewall_with_context_rules):
        """Test that friend can access profile and knowledge via wildcard."""
        assert firewall_with_context_rules.can_access("dpc-node-friend-xyz", "personal.json:profile.name") == True
        assert firewall_with_context_rules.can_access("dpc-node-friend-xyz", "personal.json:profile.bio") == True
        assert firewall_with_context_rules.can_access("dpc-node-friend-xyz", "personal.json:knowledge.skills") == True


class TestComputeSharing:
    """Test compute sharing (remote inference) permissions."""

    @pytest.fixture
    def firewall_with_compute_rules(self, tmp_path):
        """Create firewall with compute sharing rules."""
        rules_file = tmp_path / ".dpc_access"
        rules_content = """
[node_groups]
compute_friends = dpc-node-alice-123, dpc-node-bob-456

[compute]
enabled = true
allow_nodes = dpc-node-alice-123
allow_groups = compute_friends
allowed_models = llama3.1:8b, llama3:70b

[node:dpc-node-alice-123]
personal.json:profile.name = allow
"""
        rules_file.write_text(rules_content)
        return ContextFirewall(rules_file)

    def test_compute_enabled(self, firewall_with_compute_rules):
        """Test that compute sharing is enabled."""
        assert firewall_with_compute_rules.compute_enabled == True

    def test_node_can_request_inference(self, firewall_with_compute_rules):
        """Test that allowed node can request inference."""
        assert firewall_with_compute_rules.can_request_inference("dpc-node-alice-123") == True
        assert firewall_with_compute_rules.can_request_inference("dpc-node-alice-123", "llama3.1:8b") == True

    def test_node_cannot_use_restricted_model(self, firewall_with_compute_rules):
        """Test that node cannot use models not in allowed list."""
        assert firewall_with_compute_rules.can_request_inference("dpc-node-alice-123", "gpt-4") == False

    def test_group_member_can_request_inference(self, firewall_with_compute_rules):
        """Test that group members can request inference."""
        assert firewall_with_compute_rules.can_request_inference("dpc-node-bob-456") == True

    def test_unknown_node_cannot_request_inference(self, firewall_with_compute_rules):
        """Test that unknown nodes cannot request inference."""
        assert firewall_with_compute_rules.can_request_inference("dpc-node-stranger-xyz") == False

    def test_get_available_models(self, firewall_with_compute_rules):
        """Test that peer gets correct list of available models."""
        all_models = ["llama3.1:8b", "llama3:70b", "gpt-4", "claude-3"]

        # Alice should only see allowed models
        available = firewall_with_compute_rules.get_available_models_for_peer("dpc-node-alice-123", all_models)
        assert set(available) == {"llama3.1:8b", "llama3:70b"}

        # Stranger should get nothing
        available = firewall_with_compute_rules.get_available_models_for_peer("dpc-node-stranger-xyz", all_models)
        assert available == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
