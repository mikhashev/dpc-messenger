# D-PC Messenger Client

> **Component:** Local Client (Desktop Application)
> **License:** GPL v3
> **Status:** In Development (Core backend service is functional, UI integration is in progress)

This directory contains the source code for the D-PC Messenger's local client. This application is the user's sovereign gateway to the D-PC network. It manages their private data, handles all cryptographic operations, and provides the user interface for all interactions.

## Architecture: A Hybrid "Local Client-Server" Model

To combine the power and low-level networking capabilities of Python with a modern, responsive user interface built with web technologies, the client is designed as two separate but interconnected processes:

1.  **Core Service (Backend):** A persistent, background Python process that acts as the "brain." It handles all the heavy lifting: P2P networking, cryptography, data management, and communication with the Federation Hub and local AI models.
2.  **UI (Frontend):** A lightweight, cross-platform desktop application built with Tauri and SvelteKit. Its sole purpose is to provide a beautiful and intuitive user interface.

These two processes communicate via a local WebSocket API.

```ascii
+----------------------------------------------------------------------+
|                      User's Local Computer                           |
|                                                                      |
| +------------------------------------------------------------------+ |
| | D-PC Messenger (Frontend - Tauri App)                            | |
| |------------------------------------------------------------------| |
| | • Renders the chat UI (SvelteKit)                                | |
| | • Sends user commands to the Core Service                        | |
| | • Listens for events and updates the UI                          | |
| +-------------------------+----------------------------------------+ |
|                           | (Local WebSocket API on ws://127.0.0.1:9999) |
|                           v                                          |
| +-------------------------+----------------------------------------+ |
| | D-PC Core Service (Backend - Python Process)                     | |
| |------------------------------------------------------------------| |
| | • Manages P2P connections (TLS & WebRTC)                         | |
| | • Manages local context files and the `.dpc_access` firewall     | |
| | • Communicates with the Federation Hub                           | |
| | • Communicates with local AI (e.g., Ollama)                      | |
| +------------------------------------------------------------------+ |
|                                                                      |
+----------------------------------------------------------------------+
```

## Development Setup

Setting up the development environment requires configuring both the Python backend and the TypeScript frontend.

### Prerequisites

1.  **Python `3.12+`** and **Poetry `1.2+`**.
2.  **Node.js `18+`** and **npm**.
3.  **Rust:** Required by Tauri. Install via [rustup.rs](https://rustup.rs/).
4.  **(Optional but Recommended) Docker:** For running the PostgreSQL database for the Hub.

### Part 1: Core Service (Backend) Setup

All commands should be run from the `dpc-client/core/` directory.

1.  **Navigate to the Core directory:**
    ```bash
    cd dpc-client/core
    ```

2.  **Install Python dependencies:**
    This will create a local `.venv` and install all necessary libraries.
    ```bash
    poetry install
    ```

3.  **First Run & Configuration:**
    The Core Service manages its configuration in the `~/.dpc/` directory. On the first run, it will automatically create default template files if they are missing. These include:
    *   `~/.dpc/providers.toml`: For configuring AI providers.
    *   `~/.dpc/.dpc_access`: For setting your data sharing rules.
    *   `~/.dpc/personal.json`: Your default personal context file.

### Part 2: UI (Frontend) Setup

All commands should be run from the `dpc-client/ui/` directory.

1.  **Navigate to the UI directory:**
    ```bash
    cd dpc-client/ui
    ```

2.  **Install Node.js dependencies:**
    ```bash
    npm install
    ```

## Running the Application for Development

You will need **two separate terminals** running simultaneously.

**Terminal 1: Start the Backend (Core Service)**

```bash
# Navigate to the core directory
cd dpc-client/core

# Activate the virtual environment
# On Linux/macOS/Git Bash:
source $(poetry env info --path)/bin/activate
# On Windows CMD:
# "%USERPROFILE%\AppData\Local\pypoetry\Cache\virtualenvs\your-env-name\Scripts\activate.bat"

# Run the service
python run_service.py
```
You should see logs indicating that the "Core Service is running...". It will run until you stop it with `Ctrl+C`.

**Terminal 2: Start the Frontend (UI)**

```bash
# Navigate to the UI directory
cd dpc-client/ui

# Run the Tauri development server
npm run tauri dev
```
A native desktop window for the D-PC Messenger should appear. It will automatically try to connect to the Core Service running in Terminal 1.

### Troubleshooting

*   **UI shows "disconnected" or "error":**
    *   Ensure the Core Service (Python process) is running in Terminal 1 **before** you start the UI.
    *   Check the logs in Terminal 1 for any startup errors.
    *   Verify that no other application is using the WebSocket port `9999`.

*   **UI is a white screen:**
    *   This indicates a fatal JavaScript error. Open the DevTools by right-clicking in the app window and selecting "Inspect Element" (or pressing `Ctrl+Shift+I` / `F12`).
    *   Check the **Console** tab in the DevTools for red error messages.

## Current Development Status

-   [✅] **Core Service:** Backend process starts and runs persistently.
-   [✅] **Local API:** WebSocket bridge between UI and Core Service is functional.
-   [✅] **Configuration:** Graceful handling of missing config files on first run.
-   [✅] **P2P Direct Connection:** Direct TLS connection between peers is implemented.
-   [🚧] **UI State Management:** The connection between the UI and the backend is currently unstable and under active debugging.
-   [🔲] **Hub-Assisted Connection (WebRTC):** Designed but not fully implemented.
-   [🔲] **P2P Chat Logic:** Designed but not fully implemented.
-   [🔲] **AI Query Integration:** Partially implemented for local AI.