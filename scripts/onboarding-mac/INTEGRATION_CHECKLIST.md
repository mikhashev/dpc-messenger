# DPC Messenger - Mac Onboarding Integration Checklist

**Checklist for integrating Mac onboarding scripts into the main repository**

---

## Overview

This checklist guides the integration of the Mac onboarding package into the main DPC Messenger repository.

---

## Phase 1: File Integration

### 1.1 Copy Files to Repository

- [ ] Create `scripts/` directory in repository root (if not exists)
- [ ] Copy `install-mac.sh` to `scripts/install-mac.sh`
- [ ] Copy `launch-mac.sh` to `scripts/launch-mac.sh`
- [ ] Copy `MAC_QUICKSTART.md` to `docs/MAC_QUICKSTART.md`
- [ ] Make scripts executable:
  ```bash
  chmod +x scripts/install-mac.sh
  chmod +x scripts/launch-mac.sh
  ```

### 1.2 Verify File Structure

Expected structure:
```
dpc-messenger/
├── scripts/
│   ├── install-mac.sh
│   └── launch-mac.sh
├── docs/
│   ├── MAC_QUICKSTART.md
│   └── ...
└── ...
```

---

## Phase 2: Documentation Updates

### 2.1 Update Main README

- [ ] Add "Quick Start for Mac" section to main README.md
- [ ] Include one-command install instruction
- [ ] Link to MAC_QUICKSTART.md for detailed instructions
- [ ] Add macOS badge to README

**Suggested content:**
```markdown
## Quick Start for Mac

**One-command installation:**
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/mikhashev/dpc-messenger/main/scripts/install-mac.sh)"
```

Or see [Mac Quick Start Guide](docs/MAC_QUICKSTART.md) for detailed instructions.
```

### 2.2 Update Installation Documentation

- [ ] Add Mac-specific section to existing installation docs
- [ ] Cross-reference MAC_QUICKSTART.md
- [ ] Include troubleshooting tips

### 2.3 Create GitHub Release Notes

- [ ] Draft release notes for v0.21.0 (or next version)
- [ ] Highlight Mac onboarding improvements
- [ ] Include upgrade instructions

---

## Phase 3: Testing

### 3.1 Test on Clean macOS Installation

**Test Environment:**
- [ ] macOS 11.0 (Big Sur) - Intel
- [ ] macOS 12.0 (Monterey) - Intel
- [ ] macOS 13.0 (Ventura) - Apple Silicon
- [ ] macOS 14.0 (Sonoma) - Apple Silicon

**Test Cases:**
- [ ] Fresh install (no Homebrew, no Python, no Node.js)
- [ ] Install with existing Homebrew
- [ ] Install with existing Python 3.12
- [ ] Install with existing Node.js
- [ ] Update from previous version

### 3.2 Test Installer Script

- [ ] Run `install-mac.sh` from curl command
- [ ] Verify all dependencies installed
- [ ] Verify repository cloned
- [ ] Verify desktop shortcut created
- [ ] Verify virtual environment created
- [ ] Verify all npm packages installed

**Expected outcome:**
- User can run `launch-mac.sh` immediately after install
- No manual steps required

### 3.3 Test Launcher Script

- [ ] Run `launch-mac.sh`
- [ ] Verify backend starts successfully
- [ ] Verify frontend starts successfully
- [ ] Verify browser opens automatically
- [ ] Verify DPC Messenger is functional
- [ ] Test Ctrl+C shutdown
- [ ] Verify clean shutdown (no orphaned processes)

### 3.4 Test Error Handling

- [ ] Test with missing dependencies
- [ ] Test with existing DPC Messenger running
- [ ] Test with network issues
- [ ] Test with insufficient permissions
- [ ] Verify error messages are clear and helpful

### 3.5 Test Documentation

- [ ] Follow MAC_QUICKSTART.md from scratch
- [ ] Verify all steps work as documented
- [ ] Verify troubleshooting tips are accurate
- [ ] Test with non-technical user (if possible)

---

## Phase 4: GitHub Integration

### 4.1 Update Repository

- [ ] Commit all new files
- [ ] Create pull request (if using branches)
- [ ] Update CHANGELOG.md
- [ ] Tag new release (v0.21.0 or similar)

### 4.2 Update GitHub Pages (if applicable)

- [ ] Add Mac installation section to website
- [ ] Add macOS badge to project page
- [ ] Link to MAC_QUICKSTART.md

### 4.3 Create GitHub Release

- [ ] Draft release with Mac onboarding highlights
- [ ] Attach install scripts to release
- [ ] Include upgrade instructions
- [ ] Link to MAC_QUICKSTART.md

---

## Phase 5: Community Communication

### 5.1 Announce Release

- [ ] Update GitHub Discussions
- [ ] Post announcement in relevant communities
- [ ] Share on social media (if applicable)

### 5.2 Gather Feedback

- [ ] Monitor GitHub Issues for Mac-specific problems
- [ ] Collect feedback from non-technical users
- [ ] Track installation success rate

---

## Phase 6: Future Improvements

### 6.1 Short-term (Next Release)

- [ ] Add auto-update mechanism
- [ ] Create uninstall script
- [ ] Add health check script
- [ ] Improve error messages

### 6.2 Medium-term (Future Releases)

- [ ] Build native .app with Tauri
- [ ] Create DMG installer
- [ ] Add system tray integration
- [ ] Background service (no terminal)

### 6.3 Long-term (Roadmap)

- [ ] Code signing for macOS
- [ ] Mac App Store distribution
- [ ] Auto-update from GitHub Releases
- [ ] Integrated bug reporting

---

## Success Criteria

### Integration is complete when:

1. **All files are in place** in the repository
2. **Documentation is updated** with Mac instructions
3. **Testing passes** on all supported macOS versions
4. **GitHub release** is created with Mac onboarding highlights
5. **Community is notified** of the improvements

### Success metrics:

- Installation success rate > 95%
- Average installation time < 15 minutes
- No manual steps required for basic installation
- Clear error messages for all failure modes

---

## Rollback Plan

If issues are discovered after integration:

1. **Immediate**: Update documentation with known issues
2. **Short-term**: Fix critical bugs in patch release
3. **Last resort**: Remove scripts from repository and revert to manual installation

---

## Notes

### Known Limitations

1. Requires internet connection for installation
2. Requires Terminal access (user must run commands)
3. Desktop shortcut requires Desktop directory to exist
4. Not tested on macOS versions older than 11.0

### Dependencies

- Homebrew (installed by script)
- Python 3.12 (installed by script)
- Poetry (installed by script)
- Node.js (installed by script)
- Git (must be pre-installed or installed manually)

### Testing Notes

- Scripts tested on macOS 14.0 (Sonoma) - Apple Silicon
- Not yet tested on Intel Macs
- Not yet tested on older macOS versions

---

## Contact

For questions or issues:
- GitHub Issues: https://github.com/mikhashev/dpc-messenger/issues
- Documentation: https://github.com/mikhashev/dpc-messenger

---

**Version**: 1.0.0  
**Last Updated**: March 22, 2026  
**Status**: Ready for Integration

---

**Checklist completed by**: _______________  
**Date**: _______________  
**Release version**: _______________
