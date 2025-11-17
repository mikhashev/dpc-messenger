"""
Device Context Collector

Automatically collects device and system information and stores it
in a separate device_context.json file with structured fields.
Supports Windows, Linux, and macOS.
"""

import platform
import sys
import os
import shutil
import subprocess
import re
import json
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
from pathlib import Path

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

from dpc_protocol.crypto import DPC_HOME_DIR
from dpc_protocol.pcm_core import (
    PersonalContext,
    Topic,
    KnowledgeEntry,
    KnowledgeSource,
)


class DeviceContextCollector:
    """Collects device/system information and generates device_context.json."""

    SCHEMA_VERSION = "1.0"
    COLLECTOR_VERSION = "1.0"

    def __init__(self, settings=None):
        self.settings = settings
        self.device_file = DPC_HOME_DIR / "device_context.json"

    def collect_and_save(self) -> Path:
        """Collect device info and save to device_context.json.

        Returns:
            Path to device_context.json file
        """
        device_context = self._generate_device_context()

        # Write to file
        with open(self.device_file, 'w', encoding='utf-8') as f:
            json.dump(device_context, f, indent=2, ensure_ascii=False)

        return self.device_file

    def _generate_device_context(self) -> Dict[str, Any]:
        """Generate structured device context dictionary."""
        # Load existing file to preserve created_at
        created_at = None
        if self.device_file.exists():
            try:
                with open(self.device_file, 'r', encoding='utf-8') as f:
                    old_data = json.load(f)
                    created_at = old_data.get("created_at")
            except Exception:
                pass

        timestamp = datetime.now(timezone.utc).isoformat()

        device_context = {
            "schema_version": self.SCHEMA_VERSION,
            "created_at": created_at or timestamp,
            "last_updated": timestamp,
            "collection_timestamp": timestamp,
            "hardware": self._collect_hardware(),
            "software": self._collect_software(),
            "metadata": {
                "collector_version": self.COLLECTOR_VERSION,
                "auto_collected": True,
                "privacy_level": "detailed"
            }
        }

        return device_context

    def _collect_hardware(self) -> Dict[str, Any]:
        """Collect hardware information."""
        hardware = {}

        # CPU
        cpu_info = self._collect_cpu_info()
        if cpu_info:
            hardware["cpu"] = cpu_info

        # Memory
        memory_info = self._collect_memory_info()
        if memory_info:
            hardware["memory"] = memory_info

        # GPU
        gpu_info = self._collect_gpu_info()
        if gpu_info:
            hardware["gpu"] = gpu_info

        # Storage
        storage_info = self._collect_storage_info()
        if storage_info:
            hardware["storage"] = storage_info

        return hardware

    def _collect_software(self) -> Dict[str, Any]:
        """Collect software information."""
        software = {}

        # Operating System
        software["os"] = {
            "family": platform.system(),
            "version": platform.release(),
            "build": platform.version(),
            "platform": platform.platform(),
            "architecture": platform.architecture()[0]
        }

        # Runtime
        software["runtime"] = {
            "python": {
                "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "major": sys.version_info.major,
                "minor": sys.version_info.minor,
                "patch": sys.version_info.micro,
                "executable": sys.executable
            }
        }

        # Shell
        shell_type = self._detect_shell()
        if shell_type:
            software["shell"] = {"type": shell_type}

        # Dev tools
        dev_tools = self._detect_dev_tools()
        if dev_tools:
            software["dev_tools"] = dev_tools

        # Package managers
        package_managers = self._detect_package_managers()
        if package_managers:
            software["package_managers"] = package_managers

        # AI models (opt-in)
        if self.settings and self.settings.get('system', 'collect_ai_models', 'false').lower() in ('true', '1', 'yes'):
            ai_models = self._scan_ai_models()
            if ai_models:
                software["ai_models"] = ai_models

        return software

    def _collect_cpu_info(self) -> Optional[Dict[str, Any]]:
        """Collect CPU information."""
        cpu_info = {
            "architecture": platform.machine(),
            "processor": platform.processor(),
        }

        if PSUTIL_AVAILABLE:
            try:
                cpu_info["cores_physical"] = psutil.cpu_count(logical=False)
                cpu_info["cores_logical"] = psutil.cpu_count(logical=True)
            except Exception:
                pass

        return cpu_info if cpu_info else None

    def _collect_memory_info(self) -> Optional[Dict[str, Any]]:
        """Collect memory (RAM) information."""
        if not PSUTIL_AVAILABLE:
            return None

        try:
            vm = psutil.virtual_memory()
            ram_gb = vm.total / (1024**3)

            # Determine tier (rounded to power of 2)
            ram_tiers = [4, 8, 16, 32, 64, 128, 256]
            ram_tier = min(ram_tiers, key=lambda x: abs(x - ram_gb))

            return {
                "ram_gb": round(ram_gb, 2),
                "ram_tier": f"{ram_tier}GB",
                "total_bytes": vm.total,
                "comment": "ram_tier rounded to nearest power-of-2 for privacy"
            }
        except Exception:
            return None

    def _collect_gpu_info(self) -> Optional[Dict[str, Any]]:
        """Collect GPU information (cross-platform)."""
        # Try NVIDIA first (Windows, Linux, macOS)
        nvidia_gpu = self._detect_nvidia_gpu()
        if nvidia_gpu:
            return nvidia_gpu

        # Try AMD (Linux)
        if platform.system() == "Linux":
            amd_gpu = self._detect_amd_gpu()
            if amd_gpu:
                return amd_gpu

        # Try Apple Silicon (macOS)
        if platform.system() == "Darwin":
            apple_gpu = self._detect_apple_gpu()
            if apple_gpu:
                return apple_gpu

        # Try Intel/Generic (Linux)
        if platform.system() == "Linux":
            generic_gpu = self._detect_generic_gpu_linux()
            if generic_gpu:
                return generic_gpu

        return None

    def _detect_nvidia_gpu(self) -> Optional[Dict[str, Any]]:
        """Detect NVIDIA GPU with detailed information."""
        if not shutil.which('nvidia-smi'):
            return None

        try:
            # Query GPU info: name, memory.total, driver_version
            result = subprocess.run(
                ['nvidia-smi', '--query-gpu=name,memory.total,driver_version',
                 '--format=csv,noheader,nounits'],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0:
                # Parse output: "NVIDIA GeForce RTX 3060, 12288, 576.28"
                parts = [p.strip() for p in result.stdout.strip().split(',')]
                if len(parts) >= 3:
                    model = parts[0]
                    vram_mib = int(parts[1])
                    driver_version = parts[2]

                    gpu_info = {
                        "type": "nvidia",
                        "model": model,
                        "vram_mib": vram_mib,
                        "vram_gb": round(vram_mib / 1024, 1),
                        "driver_version": driver_version
                    }

                    # Try to get CUDA version
                    cuda_version = self._detect_cuda_version()
                    if cuda_version:
                        gpu_info["cuda_version"] = cuda_version

                    # Try to get compute capability
                    compute_cap = self._detect_nvidia_compute_capability()
                    if compute_cap:
                        gpu_info["compute_capability"] = compute_cap

                    return gpu_info
        except Exception:
            pass

        return None

    def _detect_cuda_version(self) -> Optional[str]:
        """Detect CUDA version if nvcc is available."""
        if not shutil.which('nvcc'):
            return None

        try:
            result = subprocess.run(
                ['nvcc', '--version'],
                capture_output=True,
                text=True,
                timeout=2
            )
            # Look for "release 12.8" pattern
            match = re.search(r'release\s+(\d+\.\d+)', result.stdout)
            if match:
                return match.group(1)
        except Exception:
            pass

        return None

    def _detect_nvidia_compute_capability(self) -> Optional[str]:
        """Detect NVIDIA compute capability."""
        if not shutil.which('nvidia-smi'):
            return None

        try:
            result = subprocess.run(
                ['nvidia-smi', '--query-gpu=compute_cap', '--format=csv,noheader'],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass

        return None

    def _detect_amd_gpu(self) -> Optional[Dict[str, Any]]:
        """Detect AMD GPU on Linux."""
        if not shutil.which('rocm-smi'):
            return None

        try:
            result = subprocess.run(
                ['rocm-smi', '--showproductname'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                # Parse rocm-smi output
                lines = result.stdout.strip().split('\n')
                for line in lines:
                    if 'GPU' in line and 'Card series' in line:
                        parts = line.split(':')
                        if len(parts) >= 2:
                            return {
                                "type": "amd",
                                "model": parts[1].strip()
                            }
        except Exception:
            pass

        return None

    def _detect_apple_gpu(self) -> Optional[Dict[str, Any]]:
        """Detect Apple GPU on macOS."""
        if platform.machine() == "arm64":
            # Apple Silicon (M1, M2, M3, etc.)
            return {
                "type": "apple_silicon",
                "model": "Apple Silicon GPU (Metal)",
                "api": "Metal"
            }
        else:
            # Intel Mac
            return {
                "type": "intel",
                "model": "Intel Integrated GPU (Metal)",
                "api": "Metal"
            }

    def _detect_generic_gpu_linux(self) -> Optional[Dict[str, Any]]:
        """Detect GPU using lspci on Linux."""
        if not shutil.which('lspci'):
            return None

        try:
            result = subprocess.run(
                ['lspci'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                # Look for VGA or 3D controller
                for line in result.stdout.split('\n'):
                    if 'VGA' in line or '3D controller' in line:
                        # Extract GPU model
                        parts = line.split(':')
                        if len(parts) >= 3:
                            model = parts[2].strip()
                            gpu_type = "unknown"
                            if 'Intel' in model:
                                gpu_type = "intel"
                            elif 'AMD' in model or 'ATI' in model:
                                gpu_type = "amd"
                            elif 'NVIDIA' in model:
                                gpu_type = "nvidia"

                            return {
                                "type": gpu_type,
                                "model": model
                            }
        except Exception:
            pass

        return None

    def _collect_storage_info(self) -> Optional[Dict[str, Any]]:
        """Collect storage (disk) information."""
        if not PSUTIL_AVAILABLE:
            return None

        try:
            disk = psutil.disk_usage('/')
            free_gb = disk.free / (1024**3)
            total_gb = disk.total / (1024**3)

            # Determine free space tier
            if free_gb < 10:
                free_tier = "<10GB"
            elif free_gb < 50:
                free_tier = "10-50GB"
            elif free_gb < 100:
                free_tier = "50-100GB"
            else:
                free_tier = "100GB+"

            storage_info = {
                "free_gb": round(free_gb, 2),
                "total_gb": round(total_gb, 2),
                "free_tier": free_tier,
                "used_percent": disk.percent
            }

            # Try to detect SSD vs HDD (platform-specific)
            disk_type = self._detect_disk_type()
            if disk_type:
                storage_info["type"] = disk_type

            return storage_info
        except Exception:
            return None

    def _detect_disk_type(self) -> Optional[str]:
        """Detect if disk is SSD or HDD (best effort)."""
        system = platform.system()

        if system == "Linux":
            # Check /sys/block for rotational flag
            try:
                # This is a simplified check - would need to map mount point to device
                result = subprocess.run(
                    ['lsblk', '-d', '-o', 'name,rota'],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if result.returncode == 0:
                    # 0 = SSD, 1 = HDD
                    if ' 0' in result.stdout:
                        return "SSD"
                    elif ' 1' in result.stdout:
                        return "HDD"
            except Exception:
                pass
        elif system == "Darwin":
            # macOS - check if APFS (usually SSD)
            try:
                result = subprocess.run(
                    ['diskutil', 'info', '/'],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if 'Solid State' in result.stdout:
                    return "SSD"
            except Exception:
                pass

        # Windows detection is complex, return generic
        if system == "Windows":
            return "SSD/HDD"

        return None

    def _detect_shell(self) -> Optional[str]:
        """Detect the current shell type."""
        shell = os.environ.get('SHELL', '')
        if shell:
            # Unix-like systems
            return Path(shell).name
        else:
            # Windows systems
            if os.environ.get('PSModulePath'):
                return 'powershell'
            else:
                return 'cmd'

    def _detect_dev_tools(self) -> Dict[str, str]:
        """Detect common dev tools (major.minor version only)."""
        tools = {}

        check_list = [
            ('git', ['git', '--version']),
            ('docker', ['docker', '--version']),
            ('node', ['node', '--version']),
            ('npm', ['npm', '--version']),
            ('rustc', ['rustc', '--version']),
            ('cargo', ['cargo', '--version']),
            ('go', ['go', 'version']),
            ('java', ['java', '-version']),
            ('gcc', ['gcc', '--version']),
            ('clang', ['clang', '--version']),
            ('make', ['make', '--version']),
            ('cmake', ['cmake', '--version']),
        ]

        for tool_name, version_cmd in check_list:
            if shutil.which(tool_name):
                try:
                    result = subprocess.run(
                        version_cmd,
                        capture_output=True,
                        text=True,
                        timeout=2
                    )
                    version_str = result.stdout + result.stderr
                    # Extract major.minor only
                    match = re.search(r'(\d+\.\d+)', version_str)
                    if match:
                        tools[tool_name] = match.group(1)
                    else:
                        tools[tool_name] = "installed"
                except Exception:
                    tools[tool_name] = "installed"

        return tools

    def _detect_package_managers(self) -> List[str]:
        """Detect installed package managers."""
        managers = []

        # Python
        if shutil.which('pip'): managers.append('pip')
        if shutil.which('pipenv'): managers.append('pipenv')
        if shutil.which('poetry'): managers.append('poetry')
        if shutil.which('conda'): managers.append('conda')

        # Node.js
        if shutil.which('npm'): managers.append('npm')
        if shutil.which('yarn'): managers.append('yarn')
        if shutil.which('pnpm'): managers.append('pnpm')

        # Rust
        if shutil.which('cargo'): managers.append('cargo')

        # System (Linux)
        if shutil.which('apt'): managers.append('apt')
        elif shutil.which('apt-get'): managers.append('apt-get')
        if shutil.which('yum'): managers.append('yum')
        if shutil.which('dnf'): managers.append('dnf')
        if shutil.which('pacman'): managers.append('pacman')
        if shutil.which('zypper'): managers.append('zypper')

        # System (macOS)
        if shutil.which('brew'): managers.append('brew')
        if shutil.which('port'): managers.append('port')

        # System (Windows)
        if shutil.which('choco'): managers.append('choco')
        if shutil.which('winget'): managers.append('winget')
        if shutil.which('scoop'): managers.append('scoop')

        return managers

    def _scan_ai_models(self) -> Optional[Dict[str, List[str]]]:
        """Scan for locally available AI models (opt-in)."""
        models = {}

        # Ollama
        ollama_models = self._scan_ollama_models()
        if ollama_models:
            models["ollama"] = ollama_models

        # Could add: LM Studio, GPT4All, etc.

        return models if models else None

    def _scan_ollama_models(self) -> List[str]:
        """Scan for locally available Ollama models."""
        if not shutil.which('ollama'):
            return []

        try:
            result = subprocess.run(
                ['ollama', 'list'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                if len(lines) > 1:
                    # Skip header line
                    models = []
                    for line in lines[1:]:
                        parts = line.split()
                        if parts:
                            models.append(parts[0])
                    return models
        except Exception:
            pass

        return []

    def update_personal_context_reference(self, context: PersonalContext) -> PersonalContext:
        """Update personal.json to reference device_context.json.

        Args:
            context: Existing PersonalContext object

        Returns:
            Updated PersonalContext with device_context reference
        """
        timestamp = datetime.now(timezone.utc).isoformat()

        # Add reference in metadata
        if not hasattr(context, 'metadata') or context.metadata is None:
            context.metadata = {}

        if "external_contexts" not in context.metadata:
            context.metadata["external_contexts"] = {}

        context.metadata["external_contexts"]["device_context"] = {
            "file": "device_context.json",
            "schema_version": self.SCHEMA_VERSION,
            "last_updated": timestamp
        }

        # Remove old device_system_info topic if it exists
        if hasattr(context, 'knowledge') and "device_system_info" in context.knowledge:
            # Create a minimal pointer entry
            entry = KnowledgeEntry(
                content=f"Device and system information is stored in device_context.json (structured format)",
                tags=["system", "device", "external-reference", "auto-collected"],
                source=KnowledgeSource(
                    type="import",
                    timestamp=timestamp,
                    sources_cited=["device_context_collector"],
                    confidence_score=1.0,
                ),
                confidence=1.0,
                last_updated=timestamp,
                usage_count=0,
                effectiveness_score=1.0,
            )

            topic = Topic(
                summary="Device context stored in device_context.json",
                entries=[entry],
                key_books=[],
                preferred_authors=[],
                mastery_level="beginner",
                learning_strategies=[],
                version=1,
                created_at=timestamp,
                last_modified=timestamp,
            )

            context.knowledge["device_system_info"] = topic

        return context


# Usage example for testing
if __name__ == "__main__":
    collector = DeviceContextCollector()
    device_file = collector.collect_and_save()

    print(f"=== Device context saved to {device_file} ===")

    # Display collected data
    with open(device_file, 'r', encoding='utf-8') as f:
        device_context = json.load(f)

    print(json.dumps(device_context, indent=2))
