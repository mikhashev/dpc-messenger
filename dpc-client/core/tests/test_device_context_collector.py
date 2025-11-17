# tests/test_device_context_collector.py

import pytest
import json
from pathlib import Path
from unittest.mock import Mock, patch
from dpc_client_core.device_context_collector import DeviceContextCollector


class TestDeviceContextCollector:
    """Test device context collection and special instructions generation."""

    @pytest.fixture
    def collector(self, tmp_path):
        """Create a DeviceContextCollector instance with temporary directory."""
        mock_settings = Mock()
        collector = DeviceContextCollector(settings=mock_settings)
        # Override device_file to use temp directory
        collector.device_file = tmp_path / "device_context.json"
        return collector

    def test_schema_version_is_1_1(self, collector):
        """Test that schema version is 1.1."""
        assert collector.SCHEMA_VERSION == "1.1"

    def test_special_instructions_structure(self, collector):
        """Test that special instructions have the correct structure."""
        instructions = collector._generate_special_instructions()

        # Verify top-level keys
        assert "interpretation" in instructions
        assert "privacy" in instructions
        assert "update_protocol" in instructions
        assert "usage_scenarios" in instructions

    def test_interpretation_instructions(self, collector):
        """Test interpretation section contains required fields."""
        instructions = collector._generate_special_instructions()
        interpretation = instructions["interpretation"]

        # Verify required keys
        assert "privacy_tiers" in interpretation
        assert "capability_inference" in interpretation
        assert "version_compatibility" in interpretation
        assert "platform_specificity" in interpretation

        # Verify values are non-empty strings
        assert isinstance(interpretation["privacy_tiers"], str)
        assert len(interpretation["privacy_tiers"]) > 0
        assert "ram_tier" in interpretation["privacy_tiers"]

        assert isinstance(interpretation["capability_inference"], str)
        assert "VRAM" in interpretation["capability_inference"]
        assert "GPU" in interpretation["capability_inference"]

    def test_privacy_instructions(self, collector):
        """Test privacy section contains required fields."""
        instructions = collector._generate_special_instructions()
        privacy = instructions["privacy"]

        # Verify required keys
        assert "sensitive_paths" in privacy
        assert "optional_fields" in privacy
        assert "default_sharing" in privacy

        # Verify content
        assert "executable" in privacy["sensitive_paths"].lower()
        assert "ai_models" in privacy["optional_fields"]
        assert "hardware" in privacy["default_sharing"]

    def test_update_protocol_instructions(self, collector):
        """Test update protocol section contains required fields."""
        instructions = collector._generate_special_instructions()
        update_protocol = instructions["update_protocol"]

        # Verify required keys
        assert "auto_refresh" in update_protocol
        assert "opt_in_features" in update_protocol
        assert "staleness_check" in update_protocol

        # Verify content
        assert "startup" in update_protocol["auto_refresh"]
        assert "collect_ai_models" in update_protocol["opt_in_features"]
        assert "7 days" in update_protocol["staleness_check"]

    def test_usage_scenarios_instructions(self, collector):
        """Test usage scenarios section contains required fields."""
        instructions = collector._generate_special_instructions()
        usage_scenarios = instructions["usage_scenarios"]

        # Verify required keys
        assert "local_inference" in usage_scenarios
        assert "remote_inference" in usage_scenarios
        assert "dev_environment" in usage_scenarios
        assert "cross_platform" in usage_scenarios

        # Verify content
        assert "VRAM" in usage_scenarios["local_inference"]
        assert "peer" in usage_scenarios["remote_inference"].lower()
        assert "package_managers" in usage_scenarios["dev_environment"]
        assert "Windows" in usage_scenarios["cross_platform"]

    @patch('dpc_client_core.device_context_collector.DeviceContextCollector._collect_hardware')
    @patch('dpc_client_core.device_context_collector.DeviceContextCollector._collect_software')
    def test_device_context_includes_special_instructions(self, mock_software, mock_hardware, collector):
        """Test that generated device context includes special_instructions field."""
        # Mock hardware and software collection
        mock_hardware.return_value = {
            "cpu": {"architecture": "AMD64", "cores_physical": 4, "cores_logical": 4},
            "memory": {"ram_gb": 16.0, "ram_tier": "16GB"}
        }
        mock_software.return_value = {
            "os": {"family": "Windows", "version": "10", "architecture": "64bit"}
        }

        device_context = collector._generate_device_context()

        # Verify special_instructions field exists
        assert "special_instructions" in device_context
        assert device_context["schema_version"] == "1.1"

        # Verify structure
        instructions = device_context["special_instructions"]
        assert "interpretation" in instructions
        assert "privacy" in instructions
        assert "update_protocol" in instructions
        assert "usage_scenarios" in instructions

    @patch('dpc_client_core.device_context_collector.DeviceContextCollector._collect_hardware')
    @patch('dpc_client_core.device_context_collector.DeviceContextCollector._collect_software')
    def test_collect_and_save_writes_valid_json(self, mock_software, mock_hardware, collector):
        """Test that collect_and_save writes valid JSON with special_instructions."""
        # Mock hardware and software collection
        mock_hardware.return_value = {
            "cpu": {"architecture": "AMD64"},
            "memory": {"ram_gb": 16.0, "ram_tier": "16GB"}
        }
        mock_software.return_value = {
            "os": {"family": "Linux", "version": "Ubuntu 22.04"}
        }

        # Collect and save
        result_path = collector.collect_and_save()

        # Verify file exists
        assert result_path.exists()

        # Verify valid JSON
        with open(result_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Verify schema version and special instructions
        assert data["schema_version"] == "1.1"
        assert "special_instructions" in data
        assert "interpretation" in data["special_instructions"]
        assert "privacy" in data["special_instructions"]

    def test_capability_inference_mentions_model_sizes(self, collector):
        """Test that capability_inference mentions specific model sizes."""
        instructions = collector._generate_special_instructions()
        capability_text = instructions["interpretation"]["capability_inference"]

        # Should mention common VRAM tiers and model sizes
        assert "12GB" in capability_text
        assert "24GB" in capability_text
        assert "7B" in capability_text or "13B" in capability_text
        assert "parameters" in capability_text

    def test_platform_specificity_mentions_all_platforms(self, collector):
        """Test that platform_specificity mentions Windows, Linux, and macOS."""
        instructions = collector._generate_special_instructions()
        platform_text = instructions["interpretation"]["platform_specificity"]

        assert "Windows" in platform_text
        assert "Linux" in platform_text
        assert "Darwin" in platform_text or "macOS" in platform_text

    def test_privacy_default_sharing_mentions_firewall(self, collector):
        """Test that default_sharing mentions firewall rules."""
        instructions = collector._generate_special_instructions()
        sharing_text = instructions["privacy"]["default_sharing"]

        assert ".dpc_access" in sharing_text or "firewall" in sharing_text.lower()
        assert "hardware" in sharing_text
        assert "allow" in sharing_text

    def test_special_instructions_are_serializable(self, collector):
        """Test that special_instructions can be JSON serialized."""
        instructions = collector._generate_special_instructions()

        # Should not raise exception
        json_string = json.dumps(instructions, indent=2, ensure_ascii=False)

        # Should be able to deserialize
        deserialized = json.loads(json_string)
        assert deserialized == instructions


class TestSpecialInstructionsContent:
    """Test specific content requirements for special instructions."""

    @pytest.fixture
    def instructions(self):
        """Get special instructions for testing."""
        collector = DeviceContextCollector()
        return collector._generate_special_instructions()

    def test_cuda_version_compatibility_mentioned(self, instructions):
        """Test that CUDA version compatibility is mentioned."""
        version_compat = instructions["interpretation"]["version_compatibility"]
        assert "CUDA" in version_compat
        assert "PyTorch" in version_compat or "TensorFlow" in version_compat

    def test_privacy_tiers_explanation(self, instructions):
        """Test that privacy tiers are explained."""
        privacy_tiers = instructions["interpretation"]["privacy_tiers"]
        assert "privacy" in privacy_tiers.lower()
        assert "rounded" in privacy_tiers.lower() or "tier" in privacy_tiers.lower()

    def test_staleness_check_timeframe(self, instructions):
        """Test that staleness check mentions a specific timeframe."""
        staleness = instructions["update_protocol"]["staleness_check"]
        assert "7 days" in staleness or "week" in staleness.lower()

    def test_local_inference_vram_margin(self, instructions):
        """Test that local_inference mentions safety margin for VRAM."""
        local_inf = instructions["usage_scenarios"]["local_inference"]
        assert "VRAM" in local_inf
        assert "margin" in local_inf.lower() or "overhead" in local_inf.lower()

    def test_cross_platform_wsl_detection(self, instructions):
        """Test that cross_platform mentions WSL detection."""
        cross_platform = instructions["usage_scenarios"]["cross_platform"]
        assert "WSL" in cross_platform
        assert "Windows" in cross_platform

    def test_opt_in_default_disabled(self, instructions):
        """Test that opt_in_features explains default disabled state."""
        opt_in = instructions["update_protocol"]["opt_in_features"]
        assert "default" in opt_in.lower()
        assert "privacy" in opt_in.lower()
        assert "collect_ai_models" in opt_in
