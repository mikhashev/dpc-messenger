# DPC Messenger - Mac Quick Start Guide

## Welcome to DPC Messenger!

DPC Messenger is a privacy-first platform for human-AI collaboration. This guide will help you get started on macOS.

---

## System Requirements

- **macOS**: 11.0 (Big Sur) or later
- **RAM**: 4 GB minimum, 8 GB recommended
- **Storage**: 500 MB free space
- **Internet**: Required for installation and AI features

---

## Installation (One Command)

### Option 1: Automatic Installation (Recommended)

Open Terminal and run:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/mikhashev/dpc-messenger/main/scripts/install-mac.sh)"
```

This will automatically install:
- Homebrew (package manager)
- Python 3.12
- Poetry (Python dependency manager)
- Node.js (JavaScript runtime)
- DPC Messenger

### Option 2: Manual Installation

If you prefer manual installation:

1. **Install Homebrew** (if not already installed)
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```

2. **Install dependencies**
   ```bash
   brew install python@3.12 poetry node
   ```

3. **Clone repository**
   ```bash
   git clone https://github.com/mikhashev/dpc-messenger.git ~/dpc-messenger
   cd ~/dpc-messenger
   ```

4. **Install Python dependencies**
   ```bash
   poetry install
   ```

5. **Install frontend dependencies**
   ```bash
   cd dpc-client/ui
   poetry run npm install
   cd ../..
   ```

---

## Starting DPC Messenger

### Option 1: Desktop Shortcut (Easiest)

After installation, you'll have a **"DPC Messenger"** icon on your desktop.

Simply **double-click** it to start!

### Option 2: Terminal Command

Open Terminal and run:

```bash
~/dpc-messenger/scripts/launch-mac.sh
```

### What Happens When You Start?

1. **Backend starts** - This runs in the background
2. **Frontend starts** - A browser window will open automatically
3. **You're ready!** - Create your profile and start collaborating

### Stopping DPC Messenger

- In the terminal window where DPC Messenger is running, press **Ctrl+C**
- Or simply **close the terminal window**

---

## First Time Setup

### 1. Create Your Profile

When DPC Messenger opens in your browser:

1. Click **"Create Profile"**
2. Enter your name
3. (Optional) Add a description
4. Click **"Save"**

### 2. Explore the Interface

- **Left sidebar**: Your conversations and contacts
- **Center**: Chat area
- **Right sidebar**: AI agent context and task board

### 3. Start Your First Conversation

1. Click **"New Chat"** in the left sidebar
2. Type your message
3. Press Enter
4. The AI agent will respond!

---

## Key Features

### 🧠 AI Collaboration
- Chat with AI agents locally (your data never leaves your computer)
- AI agents have persistent memory and learn from conversations
- Background task processing for long-running operations

### 🔒 Privacy-First
- **End-to-end encryption** for all messages
- **Local-first** storage (your data stays on your computer)
- **No cloud dependencies** (unless you choose to use them)

### 📚 Knowledge Management
- Turn conversations into structured knowledge
- Share knowledge with collaborators
- Version-controlled knowledge base

### 👥 Team Collaboration
- Invite collaborators to your chats
- Share AI context between team members
- Peer-to-peer compute sharing (borrow GPU power from friends)

---

## Common Issues

### Issue: "Command not found: poetry"

**Solution:**
```bash
# Restart your terminal or run:
export PATH="$HOME/.local/bin:$PATH"
```

### Issue: "Python 3.12 not found"

**Solution:**
```bash
brew install python@3.12
```

### Issue: "Port already in use"

**Solution:**
- Make sure you don't have DPC Messenger already running
- Check for other apps using port 8765

### Issue: Frontend won't open in browser

**Solution:**
- Manually open: http://localhost:8765
- Check if backend is running (look for "Backend started" message)

---

## Tips for Best Experience

### 1. Keep the Terminal Window Open

DPC Messenger needs the terminal window to stay open. You can minimize it, but don't close it!

### 2. Check Logs if Something Goes Wrong

Logs are saved in:
- `~/dpc-messenger/logs/backend.log`
- `~/dpc-messenger/logs/frontend.log`

### 3. Update Regularly

To get the latest features:

```bash
cd ~/dpc-messenger
git pull origin main
poetry install
cd dpc-client/ui
poetry run npm install
```

---

## Getting Help

### Documentation
- Full documentation: https://github.com/mikhashev/dpc-messenger

### Community
- GitHub Issues: https://github.com/mikhashev/dpc-messenger/issues
- Report bugs or request features

### Keyboard Shortcuts

- **Ctrl+C** - Stop DPC Messenger
- **Cmd+Q** - Quit (in browser)

---

## What's Next?

### Learn More
- Read the [PRODUCT_VISION.md](https://github.com/mikhashev/dpc-messenger/blob/main/PRODUCT_VISION.md) to understand the philosophy
- Explore the [docs/](https://github.com/mikhashev/dpc-messenger/tree/main/docs) folder for advanced features

### Advanced Features
- **Telegram Integration**: Connect your Telegram for mobile access
- **P2P Compute Sharing**: Borrow GPU power from friends for AI tasks
- **Custom AI Agents**: Create specialized agents for different tasks
- **Knowledge Commits**: Turn conversations into permanent knowledge

---

## Privacy Notice

**Your data belongs to you.**

- All messages are stored locally on your computer
- AI agents run locally (no cloud API calls unless you configure them)
- End-to-end encryption for collaborative features
- You can export your data at any time

Location of your data:
- `~/.dpc/` - Configuration and data
- `~/dpc-messenger/` - Application files

---

## License

DPC Messenger is multi-licensed:
- GPL v3.0 (Core)
- LGPL v3.0 (Libraries)
- AGPL v3.0 (Network services)
- CC0 1.0 (Documentation)

See [LICENSE](https://github.com/mikhashev/dpc-messenger/blob/main/LICENSE) for details.

---

## Thank You!

Thank you for trying DPC Messenger. We're building a future where humans and AI collaborate as partners, not as users and tools.

**Welcome to the co-evolution!** 🚀

---

**Version**: 0.20.0  
**Last Updated**: March 22, 2026
