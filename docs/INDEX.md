# D-PC Messenger Documentation Index

> **Version:** 0.10.0 | **Last Updated:** December 8, 2025

Welcome to the D-PC Messenger documentation! This index helps you navigate our comprehensive documentation library (44+ files, 25,000+ lines).

---

## Quick Navigation

### Getting Started
- **[Quick Start Guide](QUICK_START.md)** - 5-minute setup for new users
- **[Configuration Guide](CONFIGURATION.md)** - Complete configuration reference
- **[Release Notes v0.10.0](RELEASE_NOTES_V0_10_0.md)** - Latest release highlights

### Core Concepts
- **[User Sovereignty](USER_SOVEREIGNTY.md)** - Philosophy & privacy principles
- **[Connection Fallback Logic](FALLBACK_LOGIC.md)** - 6-tier hierarchy explained
- **[Knowledge Architecture](KNOWLEDGE_ARCHITECTURE.md)** - Git-like knowledge commits

### Developer Resources
- **[CLAUDE.md](../CLAUDE.md)** - Comprehensive technical reference (developers start here!)
- **[Roadmap](../ROADMAP.md)** - Development phases and timelines
- **[Changelog](../CHANGELOG.md)** - Complete version history

---

## Documentation by Category

### 📚 User Guides (End Users)

**Setup & Configuration:**
- [Quick Start Guide](QUICK_START.md) - 5-minute setup
- [Configuration Guide](CONFIGURATION.md) - All config options
- [Backup & Restore](BACKUP_RESTORE.md) - Data backup procedures
- [Offline Mode](OFFLINE_MODE.md) - Hub-independent operation
- [GitHub Auth Setup](GITHUB_AUTH_SETUP.md) - OAuth configuration

**Using Features:**
- [Remote Inference](REMOTE_INFERENCE.md) - P2P compute sharing
- [Remote AI Provider Selection](REMOTE_AI_PROVIDER_SELECTION.md) - AI provider switching
- [Manual Knowledge Save](MANUAL_KNOWLEDGE_SAVE.md) - Knowledge commit procedures

**Privacy & Security:**
- [User Sovereignty](USER_SOVEREIGNTY.md) - Data sovereignty principles
- [Device Context Spec](DEVICE_CONTEXT_SPEC.md) - Hardware/software info collected
- [Legal Compliance](LEGAL_COMPLIANCE.md) - Legal compliance guidance

---

### 💻 Developer Guides (Contributors)

**Architecture & Design:**
- **[CLAUDE.md](../CLAUDE.md)** - **START HERE!** Comprehensive technical guide
- [Knowledge Architecture](KNOWLEDGE_ARCHITECTURE.md) - Knowledge commit system design (Phases 1-8)
- [Personal Context v2 Implementation](PERSONAL_CONTEXT_V2_IMPLEMENTATION.md) - Modular context system
- [Commit Integrity Implementation](COMMIT_INTEGRITY_IMPLEMENTATION.md) - Cryptographic commits (Phase 8)
- [Fallback Logic](FALLBACK_LOGIC.md) - 6-tier connection strategy

**Integration Guides:**
- [UI Integration Guide](UI_INTEGRATION_GUIDE.md) - Frontend integration with backend
- [WebRTC Integration](README_WEBRTC_INTEGRATION.md) - WebRTC peer connection implementation
- [Knowledge Compacting](KNOWLEDGE_COMPACTING.md) - Knowledge cleanup mechanisms
- [Knowledge Detection Testing](KNOWLEDGE_DETECTION_TESTING.md) - Testing knowledge detection

**Contributing:**
- [CONTRIBUTING.md](../CONTRIBUTING.md) - Contribution guidelines
- [CLA.md](../CLA.md) - Contributor License Agreement

---

### 🚀 Operator Guides (Self-Hosting)

**Deployment:**
- [WebRTC Setup Guide](WEBRTC_SETUP_GUIDE.md) - Production WebRTC deployment
- [Hub TURN Integration](HUB_TURN_INTEGRATION.md) - Hub-level TURN server setup
- [macOS Build](MACOS_BUILD.md) - macOS-specific build instructions

**Operations:**
- [Logging Guide](LOGGING.md) - Debugging and troubleshooting
- [Configuration Guide](CONFIGURATION.md) - Server configuration options

---

### 📜 Specifications (Protocol Developers)

**Protocol Specs:**
- **[DPTP v1](../specs/dptp_v1.md)** - D-PC Transfer Protocol formal specification
- **[Hub API v1](../specs/hub_api_v1.md)** - Federation Hub API specification
- **[Device Context Spec](DEVICE_CONTEXT_SPEC.md)** - Hardware/software schema v1.1

**Library Documentation:**
- **[dpc-protocol README](../dpc-protocol/README.md)** - Protocol library API and usage examples

---

### 📄 Project Documentation

**Vision & Strategy:**
- [VISION.md](../VISION.md) - Business vision, market opportunity, mission statement
- [PRODUCT_VISION.md](../PRODUCT_VISION.md) - Product strategy & technical philosophy
- [Roadmap](../ROADMAP.md) - Development roadmap (Phases 1-3)

**Legal & Licensing:**
- [LICENSE.md](../LICENSE.md) - Multi-license structure (GPL/LGPL/AGPL/CC0)
- [Legal Compliance](LEGAL_COMPLIANCE.md) - Legal compliance guidance
- [CLA.md](../CLA.md) - Contributor License Agreement

**Release Information:**
- [Changelog](../CHANGELOG.md) - Version history (v0.1.0 - v0.10.0)
- [Release Notes v0.10.0](RELEASE_NOTES_V0_10_0.md) - Latest release
- [Release Notes v0.9.0](RELEASE_NOTES_V0_9_0.md) - Previous release
- [FIXES_SUMMARY.md](../FIXES_SUMMARY.md) - Bug fixes and improvements

---

## Documentation by Package

### Client Documentation
- **Root**: [README.md](../README.md) - Project overview
- **Client Package**: [dpc-client/README.md](../dpc-client/README.md) - Client architecture
- **Client Core**: [dpc-client/core/README.md](../dpc-client/core/README.md) - Core service quick reference
- **Server Configuration**: [SERVER_CONFIGURATION.md](SERVER_CONFIGURATION.md) - STUN/TURN server config
- **TURN Setup**: [TURN_SETUP.md](TURN_SETUP.md) - TURN credentials setup
- **DTLS Research**: [DTLS_RESEARCH.md](DTLS_RESEARCH.md) - DTLS library research notes
- **Client UI**: [dpc-client/ui/README.md](../dpc-client/ui/README.md) - UI reference

### Hub Documentation
- **Hub Package**: [dpc-hub/README.md](../dpc-hub/README.md) - Hub server setup and deployment

### Protocol Library Documentation
- **Protocol Package**: [dpc-protocol/README.md](../dpc-protocol/README.md) - Protocol library API and examples

---

## Search by Topic

### Connection & Networking
- [Fallback Logic](FALLBACK_LOGIC.md) - 6-tier connection strategies
- [WebRTC Setup Guide](WEBRTC_SETUP_GUIDE.md) - Production WebRTC
- [Hub TURN Integration](HUB_TURN_INTEGRATION.md) - TURN server setup
- [Offline Mode](OFFLINE_MODE.md) - Hub-independent operation
- [DPTP v1 Spec](../specs/dptp_v1.md) - Protocol specification

### AI & Knowledge
- [Knowledge Architecture](KNOWLEDGE_ARCHITECTURE.md) - Knowledge commit system
- [Knowledge Compacting](KNOWLEDGE_COMPACTING.md) - Cleanup mechanisms
- [Remote Inference](REMOTE_INFERENCE.md) - P2P compute sharing
- [Remote AI Provider Selection](REMOTE_AI_PROVIDER_SELECTION.md) - Provider switching

### Privacy & Security
- [User Sovereignty](USER_SOVEREIGNTY.md) - Privacy principles
- [Device Context Spec](DEVICE_CONTEXT_SPEC.md) - Hardware/software collection
- [Backup & Restore](BACKUP_RESTORE.md) - Encrypted backups
- [Commit Integrity Implementation](COMMIT_INTEGRITY_IMPLEMENTATION.md) - Cryptographic integrity

### Configuration
- [Configuration Guide](CONFIGURATION.md) - Complete reference
- [GitHub Auth Setup](GITHUB_AUTH_SETUP.md) - OAuth setup
- [Logging Guide](LOGGING.md) - Logging configuration

### Development
- [CLAUDE.md](../CLAUDE.md) - Developer technical guide
- [UI Integration Guide](UI_INTEGRATION_GUIDE.md) - Frontend integration
- [Roadmap](../ROADMAP.md) - Development phases
- [CONTRIBUTING.md](../CONTRIBUTING.md) - How to contribute

---

## Version-Specific Documentation

### Current Version (v0.10.0)
- [Release Notes v0.10.0](RELEASE_NOTES_V0_10_0.md) - Phase 6 completion
- [Fallback Logic](FALLBACK_LOGIC.md) - 6-tier connection hierarchy
- [Configuration Guide](CONFIGURATION.md) - Updated for v0.10.0

### Previous Versions
- [Release Notes v0.9.0](RELEASE_NOTES_V0_9_0.md) - DHT implementation
- [Changelog](../CHANGELOG.md) - Full version history

---

## Frequently Accessed Documents

**Top 10 Most Useful Docs:**

1. **[CLAUDE.md](../CLAUDE.md)** - Complete technical reference (developers)
2. **[Quick Start Guide](QUICK_START.md)** - 5-minute setup (new users)
3. **[Configuration Guide](CONFIGURATION.md)** - All config options
4. **[Fallback Logic](FALLBACK_LOGIC.md)** - Connection strategies explained
5. **[Knowledge Architecture](KNOWLEDGE_ARCHITECTURE.md)** - Knowledge system design
6. **[DPTP v1](../specs/dptp_v1.md)** - Protocol specification
7. **[User Sovereignty](USER_SOVEREIGNTY.md)** - Privacy principles
8. **[Remote Inference](REMOTE_INFERENCE.md)** - P2P compute sharing
9. **[Roadmap](../ROADMAP.md)** - Development phases
10. **[Changelog](../CHANGELOG.md)** - Version history

---

## Documentation Statistics

**Total Documentation:**
- **Root Docs:** 10 files, 5,865 lines
- **docs/ Directory:** 24 files, 16,147 lines
- **Package Docs:** 7 files, 2,310 lines
- **Specifications:** 3 files, 848 lines
- **Grand Total:** 44 files, 25,170 lines

**Recent Updates (December 2025):**
- Phase 6 completion documentation
- Connection orchestrator integration
- v0.10.0 release notes
- Updated configuration guide

---

## Need Help?

- **Documentation Issues:** [GitHub Issues](https://github.com/your-org/dpc-messenger/issues)
- **General Questions:** See [README.md](../README.md) for contact info
- **Contributing:** See [CONTRIBUTING.md](../CONTRIBUTING.md)

---

**D-PC Messenger** - Privacy-first, peer-to-peer messaging with collaborative AI intelligence.
