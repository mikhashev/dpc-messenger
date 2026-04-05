# DPC Messenger - Mac Onboarding Package

**Simplified installation and launch experience for macOS users**

---

## What's Included

This package provides everything needed for non-technical users to get started with DPC Messenger on macOS:

### Files

1. **install-mac.sh** - One-command installer
   - Installs all dependencies (Homebrew, Python, Poetry, Node.js)
   - Clones the repository
   - Sets up the environment
   - Creates desktop shortcut

2. **launch-mac.sh** - Unified launcher
   - Starts backend and frontend in one terminal
   - Automatic dependency checking
   - Clean shutdown with Ctrl+C
   - Colorful status messages

3. **MAC_QUICKSTART.md** - User guide
   - Step-by-step instructions
   - Troubleshooting tips
   - Feature overview

4. **README.md** - This file

---

## Quick Start

### For Users

**Install and run DPC Messenger in two steps:**

1. Open Terminal and run:
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/mikhashev/dpc-messenger/main/onboarding-mac/install-mac.sh)"
   ```

2. Double-click the "DPC Messenger" icon on your desktop

That's it!

### For Developers

To integrate these scripts into the main repository:

1. Copy these files to the repository:
   ```bash
   cp -r onboarding-mac/* ~/dpc-messenger/scripts/
   ```

2. Make scripts executable:
   ```bash
   chmod +x ~/dpc-messenger/scripts/*.sh
   ```

3. Update documentation to reference these scripts

---

## Features

### One-Command Installation

Users don't need to:
- Know what Homebrew, Poetry, or Node.js are
- Understand Python environments
- Manually clone repositories
- Run multiple commands in different terminals

The installer handles everything!

### Unified Launcher

Before this package, users had to:
1. Open Terminal 1: `poetry run python -m dpc_client.core`
2. Open Terminal 2: `cd dpc-client/ui && poetry run npm run dev`

Now:
1. Double-click "DPC Messenger" icon
2. Everything starts automatically!

### Error Handling

- Clear error messages in plain English
- Automatic dependency checking
- Helpful suggestions for fixing issues
- Log files for debugging

---

## System Requirements

- **macOS**: 11.0 (Big Sur) or later
- **RAM**: 4 GB minimum, 8 GB recommended
- **Storage**: 500 MB free space
- **Internet**: Required for installation

---

## Installation Process

### What Gets Installed

1. **Homebrew** - Package manager for macOS
2. **Python 3.12** - Programming language
3. **Poetry** - Python dependency manager
4. **Node.js** - JavaScript runtime
5. **DPC Messenger** - The application

### Installation Time

- First time: 10-15 minutes (downloads all dependencies)
- Updates: 2-3 minutes

### What Gets Created

- `~/dpc-messenger/` - Application directory
- `~/dpc-messenger/logs/` - Log files
- `~/Desktop/DPC Messenger.command` - Desktop shortcut
- `~/.dpc/` - Configuration and data

---

## Usage

### Starting DPC Messenger

**Option 1: Desktop Shortcut (Recommended)**
- Double-click "DPC Messenger" on desktop
- Terminal window opens automatically
- Browser opens with DPC Messenger

**Option 2: Terminal Command**
```bash
~/dpc-messenger/scripts/launch-mac.sh
```

### Stopping DPC Messenger

- Press **Ctrl+C** in the terminal window
- Or close the terminal window

---

## Troubleshooting

### Common Issues

#### "command not found: poetry"

**Cause**: Poetry not in PATH

**Solution**:
```bash
export PATH="$HOME/.local/bin:$PATH"
```

#### "Python 3.12 not found"

**Cause**: Python not installed or wrong version

**Solution**:
```bash
brew install python@3.12
```

#### "Port already in use"

**Cause**: DPC Messenger already running

**Solution**:
- Check for existing DPC Messenger processes
- Or restart your computer

#### Backend fails to start

**Cause**: Missing dependencies or configuration error

**Solution**:
```bash
# Check backend log
cat ~/dpc-messenger/logs/backend.log

# Reinstall dependencies
cd ~/dpc-messenger
poetry install
```

### Getting Help

- Check logs: `~/dpc-messenger/logs/`
- GitHub Issues: https://github.com/mikhashev/dpc-messenger/issues
- Documentation: https://github.com/mikhashev/dpc-messenger

---

## Development

### Testing the Scripts

To test changes to these scripts:

1. **Test installer**:
   ```bash
   bash install-mac.sh
   ```

2. **Test launcher**:
   ```bash
   bash launch-mac.sh
   ```

3. **Check logs**:
   ```bash
   tail -f ~/dpc-messenger/logs/backend.log
   tail -f ~/dpc-messenger/logs/frontend.log
   ```

### Making Changes

When updating scripts:

1. Test on a clean macOS installation
2. Test on both Intel and Apple Silicon Macs
3. Update this README with any changes
4. Update MAC_QUICKSTART.md if user-facing changes

---

## Future Improvements

### Planned Features

- [ ] Native .app bundle (using Tauri)
- [ ] DMG installer with drag-and-drop
- [ ] Auto-update mechanism
- [ ] System tray integration
- [ ] Background service (no terminal window needed)
- [ ] Health check script
- [ ] Uninstall script

### Considerations

#### Tauri App Bundle

The project already has Tauri configured. Future versions could:

1. Build a native .app:
   ```bash
   cd dpc-client/ui
   npm run tauri build
   ```

2. Create a DMG installer
3. Distribute via GitHub Releases

**Benefits**:
- Professional Mac experience
- No terminal window needed
- Easier installation

**Trade-offs**:
- Larger download size
- More complex build process
- Harder to debug for users

---

## License

Same as DPC Messenger:
- GPL v3.0 (Core)
- LGPL v3.0 (Libraries)
- AGPL v3.0 (Network services)
- CC0 1.0 (Documentation)

---

## Contributing

To improve these onboarding scripts:

1. Test on different macOS versions
2. Test on both Intel and Apple Silicon
3. Report issues or submit pull requests
4. Share feedback from non-technical users

---

## Credits

Created by the DPC Messenger team to simplify onboarding for macOS users.

**Version**: 1.0.0  
**Last Updated**: March 22, 2026

---

## Support

- **Documentation**: https://github.com/mikhashev/dpc-messenger
- **Issues**: https://github.com/mikhashev/dpc-messenger/issues
- **Discussions**: https://github.com/mikhashev/dpc-messenger/discussions

---

**Thank you for using DPC Messenger!**

*Building the future of human-AI collaboration, one install at a time.* 🚀
