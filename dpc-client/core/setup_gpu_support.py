#!/usr/bin/env python3
"""
GPU Support Setup Script for DPC Messenger

Automatically detects GPU (NVIDIA/AMD/Apple) and provides instructions
for installing the correct PyTorch version with CUDA/ROCm/MPS support.

This script integrates with Poetry - run it after 'poetry install'.

Usage:
    poetry run python setup_gpu_support.py
"""

import sys
import subprocess
import platform
import os
from pathlib import Path


def run_command(cmd, check=True, capture_output=True):
    """Run a shell command and return result."""
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=check, capture_output=capture_output, text=True)
    return result


def detect_nvidia_gpu():
    """Detect NVIDIA GPU and CUDA version."""
    print("\n🔍 Detecting NVIDIA GPU...")

    # Try nvidia-smi from PATH first
    try:
        result = run_command(["nvidia-smi", "--query-gpu=name,driver_version,cuda_version", "--format=csv,noheader"], check=False)
        if result.returncode == 0:
            gpu_info = result.stdout.strip()
            print(f"✅ Found NVIDIA GPU:\n{gpu_info}")
            return parse_nvidia_smi(gpu_info)
    except FileNotFoundError:
        pass

    # Windows-specific: Check common installation paths
    if platform.system() == "Windows":
        nvidia_paths = [
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
            r"C:\Windows\System32\nvidia-smi.exe",
        ]
        for path in nvidia_paths:
            if Path(path).exists():
                try:
                    result = run_command([path, "--query-gpu=name,driver_version,cuda_version", "--format=csv,noheader"], check=False)
                    if result.returncode == 0:
                        gpu_info = result.stdout.strip()
                        print(f"✅ Found NVIDIA GPU:\n{gpu_info}")
                        return parse_nvidia_smi(gpu_info)
                except Exception:
                    continue

    print("❌ No NVIDIA GPU detected or nvidia-smi not found")
    return None


def parse_nvidia_smi(output):
    """Parse nvidia-smi output and extract GPU info."""
    # Format: "GeForce RTX 3060, 576.28, 12.8"
    parts = output.split(",")
    if len(parts) >= 3:
        cuda_version = parts[2].strip()
        # Map CUDA version to PyTorch CUDA version
        pytorch_cuda = map_cuda_to_pytorch(cuda_version)
        return {
            "type": "nvidia",
            "cuda_version": cuda_version,
            "pytorch_cuda": pytorch_cuda
        }
    return {"type": "nvidia", "cuda_version": "unknown", "pytorch_cuda": "cu124"}


def map_cuda_to_pytorch(cuda_version):
    """Map system CUDA version to available PyTorch CUDA version."""
    # PyTorch provides CUDA builds for: 11.8, 12.1, 12.4
    # Map system CUDA to closest available PyTorch version
    try:
        major, minor = map(int, cuda_version.split("."))
        if major == 11:
            return "cu118"
        elif major == 12:
            if minor < 4:
                return "cu121"
            else:
                return "cu124"
        # Future CUDA versions - use latest available
        return "cu124"
    except (ValueError, IndexError):
        return "cu124"  # Default fallback


def detect_amd_gpu():
    """Detect AMD GPU (ROCm support - Linux only)."""
    if platform.system() != "Linux":
        return None

    print("\n🔍 Detecting AMD GPU...")

    try:
        result = run_command(["rocm-smi", "--showproductname"], check=False)
        if result.returncode == 0:
            print(f"✅ Found AMD GPU (ROCm support available)")
            return {"type": "amd", "rocm": True}
    except FileNotFoundError:
        pass

    return None


def detect_apple_gpu():
    """Detect Apple Silicon GPU (MPS support)."""
    print("\n🔍 Detecting Apple Silicon...")

    if platform.system() != "Darwin":
        return None

    if platform.machine() == "arm64":
        print(f"✅ Found Apple Silicon (M1/M2/M3/M4) - MPS support available")
        return {"type": "apple", "mps": True}

    return None


def get_current_pytorch():
    """Check current PyTorch installation."""
    try:
        import torch
        print(f"\n📦 Current PyTorch: {torch.__version__}")
        print(f"📦 CUDA available: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"📦 CUDA version: {torch.version.cuda}")
            return True, True
        return True, False
    except ImportError:
        print("📦 PyTorch not installed")
        return False, False


def install_pytorch_cuda(cuda_version="cu124"):
    """Install PyTorch with CUDA support using poetry."""
    print(f"\n📥 Installing PyTorch with {cuda_version} support...")

    # For Poetry, we need to use pip with the virtual environment
    # Get the poetry venv python executable
    python_bin = sys.executable

    index_url = f"https://download.pytorch.org/whl/{cuda_version}"

    cmd = [
        python_bin, "-m", "pip", "install", "--upgrade",
        "torch", "torchvision",
        "--index-url", index_url
    ]

    print("\n⚠️  NOTE: This will modify packages in your Poetry virtual environment.")
    print("    Poetry's lock file may become outdated after this operation.")
    response = input("\nContinue? (Y/n): ")

    if response.lower() == 'n':
        print("Installation cancelled.")
        return False

    result = run_command(cmd, check=False)
    if result.returncode == 0:
        print("\n✅ PyTorch with CUDA support installed successfully!")
        print("⚠️  Consider running 'poetry lock --no-update' to update your lock file.")
        return True
    else:
        print(f"\n❌ Failed to install PyTorch with CUDA support")
        print(f"Error: {result.stderr}")
        return False


def show_poetry_instructions(gpu_info):
    """Show Poetry-specific installation instructions."""
    print("\n" + "=" * 70)
    print("PYTORCH CUDA INSTALLATION INSTRUCTIONS")
    print("=" * 70)

    if gpu_info["type"] == "nvidia":
        cuda_version = gpu_info.get("pytorch_cuda", "cu124")
        print(f"\n💡 Detected NVIDIA GPU with CUDA support")
        print(f"\n📋 For Poetry projects, you have two options:\n")

        print("Option 1: Use this script (automated)")
        print(f"  This script will install PyTorch with {cuda_version} support")
        print(f"  using pip within your Poetry virtual environment.\n")

        print("Option 2: Manual installation (recommended for production)")
        print(f"  1. Add to pyproject.toml:")
        print(f"     [[tool.poetry.source]]")
        print(f"     name = \"pytorch-cuda\"")
        print(f"     url = \"https://download.pytorch.org/whl/{cuda_version}\"")
        print(f"     priority = \"explicit\"")
        print(f"\n  2. Then install:")
        print(f"     poetry source add pytorch-cuda https://download.pytorch.org/whl/{cuda_version}")
        print(f"     poetry install --no-cache")
        print()

    elif gpu_info["type"] == "amd":
        print("\n💡 Detected AMD GPU (ROCm support - Linux only)")
        print("\n📋 ROCm installation is more complex:")
        print("  See: https://rocm.docs.amd.com/projects/install-on-linux/en/latest/")
        print()

    elif gpu_info["type"] == "apple":
        print("\n💡 Detected Apple Silicon (M1/M2/M3/M4)")
        print("\n✅ Good news! Standard PyTorch includes MPS (Metal) support.")
        print("  Just run: poetry install")
        print()

    print("=" * 70)


def main():
    """Main setup flow."""
    print("=" * 70)
    print("DPC Messenger - GPU Support Setup (Poetry)")
    print("=" * 70)

    # Check if running in Poetry environment
    in_poetry = os.environ.get("POETRY_ACTIVE") == "1" or "poetry" in sys.prefix.lower()

    if not in_poetry:
        print("⚠️  WARNING: May not be running in Poetry environment!")
        print(f"    sys.prefix: {sys.prefix}")
        response = input("\nContinue anyway? (y/N): ")
        if response.lower() != 'y':
            print("\nExiting. Please run with: poetry run python setup_gpu_support.py")
            return 1

    # Check current PyTorch installation
    pytorch_installed, has_cuda = get_current_pytorch()

    if pytorch_installed and has_cuda:
        print("\n✅ PyTorch with CUDA support already installed!")
        print("   You're ready to use GPU-accelerated Whisper transcription.")
        return 0

    # Detect GPU
    gpu_info = detect_nvidia_gpu()
    if not gpu_info:
        gpu_info = detect_amd_gpu()
    if not gpu_info:
        gpu_info = detect_apple_gpu()

    # Show instructions based on detected GPU
    if gpu_info:
        show_poetry_instructions(gpu_info)

        if gpu_info["type"] == "nvidia":
            response = input("\nRun automated installation now? (Y/n): ")
            if response.lower() != 'n':
                cuda_version = gpu_info.get("pytorch_cuda", "cu124")
                success = install_pytorch_cuda(cuda_version)
                if success:
                    print("\n✅ Setup complete!")
                    print("   Please restart DPC Messenger to use GPU acceleration.")
                    return 0
        elif gpu_info["type"] == "apple":
            print("\n✅ No special installation needed!")
            print("   Standard 'poetry install' includes MPS support.")
            return 0
    else:
        print("\n💡 No GPU detected - CPU-only PyTorch will be used")
        print("   Whisper transcription will be slower but still functional")

    print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
