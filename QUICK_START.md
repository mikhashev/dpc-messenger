# D-PC Messenger Quick Start Guide

> **Status:** Alpha | **Platforms:** Windows, Linux, macOS | **Time:** 15-30 minutes

D-PC Messenger is a private space where people and their AI agents work together, build knowledge, and communicate directly — no servers, no cloud.

**This guide:** install tools → clone → run → use app in UI.

Pick your operating system:
- [Windows](#windows)
- [macOS](#macos)
- [Linux](#linux)

---

## Windows

### Step 1: Install tools

```powershell
winget install Python.Python.3.12
pip install poetry
winget install OpenJS.NodeJS.LTS
```

Install Rust from [rustup.rs](https://rustup.rs/) (download and run the installer).

### Step 2: Clone and install

```powershell
git clone https://github.com/mikhashev/dpc-messenger.git
cd dpc-messenger

cd dpc-client/core
poetry install

cd ../ui
npm install
```

### Step 3: Run

Open **two terminals:**

**Terminal 1 — Backend:**

```powershell
cd dpc-client/core
poetry run python run_service.py
```

**Terminal 2 — Frontend:**

```powershell
cd dpc-client/ui
npm run tauri dev
```

A desktop window will open — that's the app.

Your private data is stored in `C:\Users\<YourName>\.dpc\`. See [What gets created](#whats-in-dpc) below for details.

---

## macOS

### Step 1: Install tools

```bash
brew install python@3.12
pip3 install poetry
brew install node
brew install rustup && rustup-init
```

### Step 2: Clone and install

```bash
git clone https://github.com/mikhashev/dpc-messenger.git
cd dpc-messenger

cd dpc-client/core
poetry install

cd ../ui
npm install
```

### Step 3: Run

Open **two terminals:**

**Terminal 1 — Backend:**

```bash
cd dpc-client/core
poetry run python run_service.py
```

**Terminal 2 — Frontend:**

```bash
cd dpc-client/ui
npm run tauri dev
```

A desktop window will open — that's the app.

Your private data is stored in `~/.dpc/`. See [What gets created](#whats-in-dpc) below for details.

---

## Linux

### Step 1: Install tools

```bash
sudo apt install python3.12 python3.12-venv
pip3 install poetry
sudo apt install nodejs npm
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

Voice recording requires:
```bash
sudo apt install libasound2-dev pkg-config libpulse-dev
```

### Step 2: Clone and install

```bash
git clone https://github.com/mikhashev/dpc-messenger.git
cd dpc-messenger

cd dpc-client/core
poetry install

cd ../ui
npm install
```

### Step 3: Run

Open **two terminals:**

**Terminal 1 — Backend:**

```bash
cd dpc-client/core
poetry run python run_service.py
```

**Terminal 2 — Frontend:**

```bash
cd dpc-client/ui
npm run tauri dev
```

A desktop window will open — that's the app. 

Your private data is stored in `~/.dpc/`. See [What gets created](#whats-in-dpc) below for details.
---

## What's in `.dpc`?

Your data is stored in `~/.dpc/` (Windows: `C:\Users\<YourName>\.dpc\`). Here's what gets created on first run:

| File | What it is | How to configure | Example |
|------|------------|-----------------|---------|
| `node.key`, `node.crt`, `node.id` | Your cryptographic identity | Auto-generated, don't edit | — |
| `config.ini` | Ports, timeouts, feature toggles | Edit manually or leave defaults | — |
| `providers.json` | AI provider config (defaults to Ollama) | UI: click **"AI Providers"** in sidebar | [providers.example.json](./dpc-client/providers.example.json) |
| `privacy_rules.json` | Firewall — who can see what | UI: click **"Firewall Rules"** in sidebar | [privacy_rules.example.json](./dpc-client/privacy_rules.example.json) |
| `personal.json` | Your profile and context | UI: click **"Personal Context"** in sidebar | [personal_context_example.json](./dpc-client/personal_context_example.json) |
| `device_context.json` | Your hardware/software info | Auto-collected, no action needed | [device_context_example.json](./dpc-client/device_context_example.json) |

Other folders (`knowledge/`, `conversations/`, `agents/`, `logs/`) are created automatically as you use the app.

---

## Next steps

Once the app is running and you can chat, the embedded AI agent is
probably what you want to explore next — skills, Telegram bridging,
and connecting Claude Code to the same conversation all live there:

**[Read the agent guide → `docs/agent/DPC_AGENT_GUIDE.md`](./docs/agent/DPC_AGENT_GUIDE.md)**

---

<div align="center">

**[Back to README](./README.md)** | **[Documentation](./docs/)**

</div>
