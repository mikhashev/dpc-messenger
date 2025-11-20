# Device Context Specification

> **Schema Version:** 1.1
> **Last Updated:** 2025-11-18

## Overview

The Device Context system automatically collects and structures information about a device's hardware, software, and development environment. This specification defines the schema, special instructions block, and interpretation rules for `device_context.json`.

---

## Purpose

Device context enables:
- **Environment-aware AI assistance** - AI can provide platform-specific recommendations
- **Compute resource sharing** - Peers can assess GPU capabilities for model offloading
- **Privacy-preserving collaboration** - Structured sharing with granular firewall controls
- **Intelligent recommendations** - AI maps hardware specs to model compatibility

---

## File Location

```
~/.dpc/device_context.json
```

**On Windows:** `C:\Users\<username>\.dpc\device_context.json`
**On Linux/Mac:** `/home/<username>/.dpc/device_context.json`

---

## Schema Structure

### Top-Level Fields

```json
{
  "schema_version": "1.1",
  "created_at": "ISO 8601 timestamp",
  "last_updated": "ISO 8601 timestamp",
  "collection_timestamp": "ISO 8601 timestamp",
  "hardware": { ... },
  "software": { ... },
  "metadata": { ... },
  "special_instructions": { ... }
}
```

### Hardware Section

Structured hardware specifications:

```json
{
  "hardware": {
    "cpu": {
      "architecture": "AMD64 | ARM64 | x86",
      "processor": "CPU model name",
      "cores_physical": 8,
      "cores_logical": 16
    },
    "memory": {
      "ram_gb": 31.8,              // Exact measurement
      "ram_tier": "32GB",           // Privacy-rounded (16GB, 32GB, 64GB, etc.)
      "total_bytes": 34174423040,
      "comment": "ram_tier rounded to nearest power-of-2 for privacy"
    },
    "gpu": {
      "type": "nvidia | amd | intel | apple | unknown",
      "model": "NVIDIA GeForce RTX 3060",
      "vram_mib": 12288,
      "vram_gb": 12.0,
      "driver_version": "537.34",
      "cuda_version": "12.2",       // NVIDIA only
      "compute_capability": "8.6"   // NVIDIA only
    },
    "storage": {
      "free_gb": 850.5,             // Exact measurement
      "total_gb": 2000.0,
      "free_tier": "500GB+",        // Privacy-rounded (100GB+, 500GB+, 1TB+)
      "used_percent": 57.5,
      "type": "SSD | HDD | NVMe SSD"
    }
  }
}
```

**Privacy Tiers:**
- **RAM Tiers:** 8GB, 16GB, 32GB, 64GB, 128GB, 256GB
- **Storage Tiers:** <100GB, 100GB+, 500GB+, 1TB+, 5TB+

### Software Section

Software environment and development tools:

```json
{
  "software": {
    "os": {
      "family": "Windows | Linux | Darwin",
      "version": "10 | Ubuntu 22.04.3 LTS | 13.5",
      "build": "Build number or kernel version",
      "platform": "Full platform string",
      "architecture": "64bit | 32bit | ARM64"
    },
    "runtime": {
      "python": {
        "version": "3.12.0",
        "major": 3,
        "minor": 12,
        "patch": 0,
        "executable": "/path/to/python"  // ⚠️ PRIVACY: Filter in sharing
      }
    },
    "shell": {
      "type": "bash | powershell | zsh | cmd"
    },
    "dev_tools": {
      "git": "2.40.1",
      "docker": "24.0.7",
      "node": "20.10.0",
      "npm": "installed | 10.2.3",
      "rustc": "1.75.0"
      // ... other tools detected
    },
    "package_managers": [
      "pip", "poetry", "npm", "cargo", "apt", "brew", "winget"
    ],
    "ai_models": [  // ⚠️ OPT-IN ONLY (collect_ai_models=true)
      {
        "name": "llama3.1:8b",
        "provider": "ollama",
        "size_gb": 4.7,
        "context_length": 8192
      }
    ]
  }
}
```

### Metadata Section

Collection metadata and settings:

```json
{
  "metadata": {
    "collector_version": "1.0",
    "auto_collected": true,
    "privacy_level": "detailed | basic"
  }
}
```

### Special Instructions Section (NEW in v1.1)

Interpretation rules for AI systems:

```json
{
  "special_instructions": {
    "interpretation": {
      "privacy_tiers": "...",
      "capability_inference": "...",
      "version_compatibility": "...",
      "platform_specificity": "..."
    },
    "privacy": {
      "sensitive_paths": "...",
      "optional_fields": "...",
      "default_sharing": "..."
    },
    "update_protocol": {
      "auto_refresh": "...",
      "opt_in_features": "...",
      "staleness_check": "..."
    },
    "usage_scenarios": {
      "local_inference": "...",
      "remote_inference": "...",
      "dev_environment": "...",
      "cross_platform": "..."
    }
  }
}
```

---

## Special Instructions Reference

### Interpretation Rules

Guidelines for AI systems to correctly interpret device specifications.

#### `privacy_tiers`

**Rule:** `"ram_tier and free_tier are privacy-rounded values. Use exact ram_gb/free_gb only when precise calculations are required. Always present the tier value to users for privacy."`

**Purpose:** Prevents accidental privacy leaks by defaulting to rounded values.

**Examples:**
- ✅ "You have 32GB of RAM" (uses ram_tier)
- ⚠️ "You have 31.8GB of RAM" (only when calculating buffer sizes)

#### `capability_inference`

**Rule:** `"Map GPU specifications to AI model capabilities using these guidelines: <12GB VRAM → models up to 7B parameters, 12GB VRAM → models up to 13B parameters, 16GB VRAM → models up to 20B parameters, 24GB VRAM → models up to 70B parameters (with quantization). Consider compute_capability for NVIDIA GPUs: >=7.0 for modern frameworks, >=8.0 for advanced features."`

**Purpose:** Enable accurate recommendations for local AI model inference.

**Examples:**
- RTX 3060 (12GB, compute 8.6) → "Can run llama3:13b comfortably"
- RTX 4090 (24GB, compute 8.9) → "Can run llama3:70b with 4-bit quantization"
- GTX 1650 (4GB, compute 7.5) → "Limited to llama3:3b or smaller models"

#### `version_compatibility`

**Rule:** `"Match CUDA version with PyTorch/TensorFlow requirements. CUDA 11.x requires PyTorch <2.0, CUDA 12.x requires PyTorch >=2.0. Warn about potential driver updates if model inference fails with compatibility errors."`

**Purpose:** Prevent "works on my machine" issues with ML frameworks.

**Examples:**
- CUDA 12.2 + PyTorch 1.13 → "Incompatible: upgrade PyTorch to 2.0+"
- CUDA 11.8 + TensorFlow 2.15 → "Compatible"

#### `platform_specificity`

**Rule:** `"Consider os.family when suggesting commands and tools: Windows → PowerShell/cmd/winget, Linux → bash/apt/yum, Darwin (macOS) → zsh/brew. If Windows has WSL detected (check dev_tools for 'wsl'), suggest Linux commands as an option."`

**Purpose:** Provide platform-appropriate instructions without user having to specify their OS.

**Examples:**
- Windows user asks "How to install Docker?" → Suggest Docker Desktop or winget
- Linux user asks same → Suggest `sudo apt install docker.io` or `docker-ce`
- macOS user → Suggest `brew install --cask docker`

### Privacy Rules

Guidelines for sharing device context with peers.

#### `sensitive_paths`

**Rule:** `"Never share or suggest sharing full file paths for executables (python.executable, shell paths, etc.). Only share version numbers and tool names. File paths may reveal usernames, directory structures, or organizational information."`

**Purpose:** Prevent accidental exposure of usernames or internal paths.

**Examples:**
- ❌ Share: `/home/alice/.pyenv/versions/3.12.0/bin/python`
- ✅ Share: `{"python": {"version": "3.12.0", "major": 3, "minor": 12}}`

#### `optional_fields`

**Rule:** `"The ai_models array requires explicit opt-in via collect_ai_models=true in [system] config. If this array is empty or missing, DO NOT assume the user has no AI models installed—they may have disabled collection for privacy. Always check metadata.privacy_level and respect the user's choice."`

**Purpose:** Respect user's choice to hide installed models.

**Examples:**
- `ai_models: []` → "AI model collection disabled (privacy setting)"
- Not asking → "To share installed models, enable collect_ai_models=true"

#### `default_sharing`

**Rule:** `"By default, only software.os and software.dev_tools should be shared with peers unless explicit firewall allow rules exist. Hardware specifications (GPU, RAM, storage) require explicit authorization via device_context.json:hardware.* = allow rules in .dpc_access.json file."`

**Purpose:** Privacy-first sharing model.

**Firewall Examples:**
```json
{
  "nodes": {
    "dpc-node-alice-123": {
      "_comment": "Share all GPU info and software info",
      "device_context.json:hardware.gpu.*": "allow",
      "device_context.json:software.*": "allow"
    },
    "dpc-node-bob-456": {
      "_comment": "Only dev tools",
      "device_context.json:software.dev_tools.*": "allow",
      "device_context.json:hardware.*": "deny"
    }
  }
}
```

### Update Protocol

Guidelines for keeping device context fresh.

#### `auto_refresh`

**Rule:** `"Device context is automatically refreshed on every client startup. If you detect an error related to outdated drivers, missing tools, or changed hardware, suggest the user restart the DPC-Client service to refresh device context. Major updates (OS upgrade, driver update, new GPU) should trigger manual recollection."`

**Purpose:** Keep AI recommendations accurate after system changes.

**Examples:**
- User reports CUDA error → "Your device context shows CUDA 11.8. Try restarting DPC-Client to detect updated drivers."
- User installed Docker → "Restart the client to detect new dev tools."

#### `opt_in_features`

**Rule:** `"To enable AI model collection (ai_models array), users must set collect_ai_models=true in the [system] section of config.ini. This is disabled by default for privacy. Do not automatically enable this feature."`

**Purpose:** Explicit consent for model inventory.

**Configuration:**
```ini
[system]
auto_collect_device_info = true
collect_hardware_specs = true
collect_dev_tools = true
collect_ai_models = false  # Opt-in required
```

#### `staleness_check`

**Rule:** `"If collection_timestamp is more than 7 days old, recommend restarting the client to refresh device context. For hardware changes (new GPU, RAM upgrade), context will remain stale until next restart."`

**Purpose:** Detect potentially outdated information.

**Example:**
```python
# Check staleness
from datetime import datetime, timedelta
collection_time = datetime.fromisoformat(device_context["collection_timestamp"])
if datetime.now(timezone.utc) - collection_time > timedelta(days=7):
    print("Device context is >7 days old. Restart client to refresh.")
```

### Usage Scenarios

Practical application guidelines for AI assistance.

#### `local_inference`

**Rule:** `"When recommending local AI models, consider both GPU VRAM and the ai_models list (if present). Prioritize models the user already has installed. For new model suggestions, ensure VRAM requirements are met with 20% safety margin for OS overhead."`

**Purpose:** Recommend models that will actually work.

**Examples:**
- User has llama3.1:8b (4.7GB) + RTX 3060 (12GB) → Suggest trying llama3:13b
- User has no models + 4GB GPU → Start with phi-2 (2.7GB) or tinyllama

#### `remote_inference`

**Rule:** `"When suggesting compute offloading to peers, match the peer's GPU VRAM to the model requirements. Check both requester's and peer's device context. If a peer has superior hardware (more VRAM, newer compute capability), suggest offloading. If peer's hardware is similar or inferior, keep inference local."`

**Purpose:** Optimize distributed compute allocation.

**Examples:**
- Alice (RTX 3060, 12GB) + Bob (RTX 4090, 24GB) → "Offload llama3:70b to Bob"
- Alice (RTX 3060, 12GB) + Charlie (GTX 1650, 4GB) → "Run locally, Charlie can't handle it"

#### `dev_environment`

**Rule:** `"When suggesting package installations, check software.package_managers and prioritize the user's available tools. Order of preference: poetry > pip for Python, npm > yarn for JavaScript, cargo for Rust. Provide fallback commands if the preferred manager isn't available."`

**Purpose:** Use the user's existing toolchain.

**Examples:**
- User has `["pip", "poetry"]` → Suggest `poetry add numpy`
- User has only `["pip"]` → Suggest `pip install numpy`
- User has `["apt", "winget"]` → Windows/WSL hybrid, offer both

#### `cross_platform`

**Rule:** `"Detect the user's OS and provide platform-native instructions. For Windows users with WSL (detected via dev_tools), offer both Windows-native and WSL/Linux commands. For macOS, prefer Homebrew when available. For Linux, detect package manager from package_managers list (apt, yum, pacman, etc.)."`

**Purpose:** Eliminate "this command doesn't work on my OS" frustration.

**Examples:**
```python
# Windows 10 user asks "Install Git"
if os_family == "Windows" and "wsl" in dev_tools:
    return """
    Windows: winget install Git.Git
    WSL (Linux): sudo apt install git
    """
elif os_family == "Windows":
    return "winget install Git.Git"

# macOS user
elif os_family == "Darwin" and "brew" in package_managers:
    return "brew install git"
```

---

## Integration with Personal Context Model (PCM)

Device context is stored separately from `personal.json` and referenced via `metadata.external_contexts`:

```json
{
  "metadata": {
    "external_contexts": {
      "device_context": {
        "file": "device_context.json",
        "schema_version": "1.1",
        "last_updated": "2025-01-15T10:30:00.000000"
      }
    }
  },
  "knowledge": {
    "device_system_info": {
      "summary": "Device context stored in device_context.json",
      "entries": [
        {
          "content": "Device and system information is stored in device_context.json (structured format)",
          "tags": ["system", "device", "external-reference", "auto-collected"]
        }
      ]
    }
  }
}
```

---

## AI Prompt Integration

When device context is included in AI prompts, it appears in a separate `<DEVICE_CONTEXT>` block with special instructions as a preamble:

```xml
<DEVICE_CONTEXT source="local">
DEVICE CONTEXT INTERPRETATION RULES:

Interpretation Guidelines:
  - privacy_tiers: ram_tier and free_tier are privacy-rounded values...
  - capability_inference: Map GPU VRAM to model sizes...
  - version_compatibility: Match CUDA version with framework requirements...
  - platform_specificity: Consider OS family when suggesting commands...

Privacy Rules:
  - sensitive_paths: Never share full file paths...
  - optional_fields: ai_models requires explicit opt-in...
  - default_sharing: Only software.os and dev_tools by default...

Update Protocol:
  - auto_refresh: Refreshes on client startup...
  - staleness_check: Recommend refresh if >7 days old...

Usage Scenarios:
  - local_inference: Consider GPU VRAM and installed models...
  - remote_inference: Match peer GPU capabilities...
  - dev_environment: Prioritize user's package managers...
  - cross_platform: Provide platform-native instructions...

{
  "schema_version": "1.1",
  "hardware": { ... },
  "software": { ... }
}
</DEVICE_CONTEXT>
```

---

## Configuration

Device context collection is configured in `~/.dpc/config.ini`:

```ini
[system]
auto_collect_device_info = true      # Master toggle (default: true)
collect_hardware_specs = true        # CPU/RAM/GPU/disk (default: true)
collect_dev_tools = true             # Git, Docker, Node, etc. (default: true)
collect_ai_models = false            # Ollama models (default: false, opt-in)
```

**Privacy Levels:**
- **Detailed** (default): Collects all enabled categories
- **Basic**: Only OS family and major dev tools

---

## Firewall Control

Device context sharing is controlled via `~/.dpc/.dpc_access.json`:

```json
{
  "nodes": {
    "dpc-node-alice-123": {
      "_comment": "Alice can see all GPU info for compute sharing",
      "device_context.json:hardware.gpu.*": "allow",
      "device_context.json:hardware.memory.ram_gb": "allow",
      "device_context.json:software.*": "allow"
    },
    "dpc-node-bob-456": {
      "_comment": "Bob can only see development environment",
      "device_context.json:software.dev_tools.*": "allow",
      "device_context.json:software.os.*": "allow",
      "device_context.json:hardware.*": "deny"
    }
  },
  "groups": {
    "trusted_developers": {
      "_comment": "Share dev environment but not hardware specs",
      "device_context.json:software.*": "allow",
      "device_context.json:hardware.*": "deny"
    }
  }
}
```

**Wildcard Rules:**
- `*` at end of path matches all sub-fields
- More specific rules override general rules
- Default: DENY unless explicitly allowed

---

## Version History

### v1.1 (2025-11-18)
- **Added:** `special_instructions` block for AI interpretation
- **Added:** Interpretation rules for privacy tiers and capability inference
- **Added:** Privacy rules for sensitive path filtering
- **Added:** Update protocol for staleness detection
- **Added:** Usage scenarios for platform-specific guidance

### v1.0 (2025-01-15)
- Initial release with hardware/software/metadata structure
- Privacy tiers for RAM and storage
- GPU detection (NVIDIA, AMD, Intel, Apple)
- Development tools detection
- Auto-collection on startup

---

## Examples

### Complete Device Context (v1.1)

See [device_context_example.json](../dpc-client/device_context_example.json) for a complete example with all fields populated.

### Minimal Device Context

```json
{
  "schema_version": "1.1",
  "created_at": "2025-11-18T10:00:00.000000+00:00",
  "last_updated": "2025-11-18T10:00:00.000000+00:00",
  "collection_timestamp": "2025-11-18T10:00:00.000000+00:00",
  "hardware": {
    "cpu": {"architecture": "AMD64", "cores_physical": 4, "cores_logical": 4},
    "memory": {"ram_gb": 15.8, "ram_tier": "16GB"}
  },
  "software": {
    "os": {"family": "Windows", "version": "10", "architecture": "64bit"},
    "runtime": {"python": {"version": "3.12.0", "major": 3, "minor": 12}}
  },
  "metadata": {
    "collector_version": "1.0",
    "auto_collected": true,
    "privacy_level": "basic"
  },
  "special_instructions": {
    "interpretation": {
      "privacy_tiers": "ram_tier is privacy-rounded (16GB). Use for general recommendations.",
      "platform_specificity": "Windows 10 detected. Suggest PowerShell/winget commands."
    },
    "privacy": {
      "sensitive_paths": "Executable paths filtered from sharing.",
      "default_sharing": "Only OS and dev tools shared by default."
    }
  }
}
```

---

## Related Documentation

- [Configuration Guide](CONFIGURATION.md) - Device collection settings
- [Quick Start Guide](QUICK_START.md) - First-time setup
- [CLAUDE.md](../CLAUDE.md) - Architecture overview
- [PCM Specification](https://github.com/personal-context-manager) - Personal Context Model
