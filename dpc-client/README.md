# dpc-client

Desktop application: Python backend ([`core/`](core/)) + Tauri/SvelteKit
frontend ([`ui/`](ui/)). This directory is the developer entrypoint —
end-user setup lives in [`../QUICK_START.md`](../QUICK_START.md).

**License:** GPL v3 — see [`../LICENSE.md`](../LICENSE.md)

---

## Prerequisites

- Python 3.12+ (see [`core/pyproject.toml`](core/pyproject.toml) for the exact constraint)
- Node.js 18+ with npm
- Rust — install via [rustup.rs](https://rustup.rs/)

AI provider is configured at runtime in `~/.dpc/providers.json`, not at
install time — see [`../docs/CONFIGURATION.md`](../docs/CONFIGURATION.md).
Options include remote providers (Anthropic, OpenAI, Z.AI, Gemini,
GigaChat, GitHub Models) and [Ollama](https://ollama.ai/) for local
models.

---

## Run in dev mode

Two processes. Open two terminals.

**Backend**

```bash
cd dpc-client/core
poetry install
poetry run python run_service.py
```

On first run the backend creates identity + config files under `~/.dpc/`
(see [`docs/QUICK_START.md`](../QUICK_START.md) for the file list and
[`docs/CONFIGURATION.md`](../docs/CONFIGURATION.md) for the full
reference).

**Frontend**

```bash
cd dpc-client/ui
npm install
npm run tauri dev
```

The frontend talks to the backend over WebSocket at `127.0.0.1:9999`
by default (override in `~/.dpc/config.ini` under `[api] port`).

---

## Build for distribution

```bash
cd dpc-client/ui
npm run tauri build
```

Artifacts land in `ui/src-tauri/target/release/` (plus
`bundle/msi/`, `bundle/dmg/`, `bundle/appimage/` depending on platform).

---

## Test

```bash
cd dpc-client/core
poetry run pytest
poetry run pytest --cov=dpc_client_core
```

Frontend tests (where they exist) live in `ui/` — run `npm test` there.

---

## Where to look next

| Question | Doc |
|----------|-----|
| How do I install and start using it as a user? | [`../QUICK_START.md`](../QUICK_START.md) |
| What goes in `~/.dpc/`? | [`../docs/CONFIGURATION.md`](../docs/CONFIGURATION.md) |
| How does the embedded agent work? | [`../docs/DPC_AGENT_GUIDE.md`](../docs/DPC_AGENT_GUIDE.md) |
| How do I deploy with a Federation Hub? | [`../docs/WEBRTC_SETUP_GUIDE.md`](../docs/WEBRTC_SETUP_GUIDE.md) |
| How does the knowledge commit system work? | [`../docs/KNOWLEDGE_ARCHITECTURE.md`](../docs/KNOWLEDGE_ARCHITECTURE.md) |
| What's in the Python backend package? | [`core/README.md`](core/README.md) |
| What does the wire protocol look like? | [`../specs/dptp_v1.md`](../specs/dptp_v1.md) |
| How do the three of us (Mike + CC + Ark) work? | [`../protocol-13-public.md`](../protocol-13-public.md) |
| Where's the project going? | [`../ROADMAP.md`](../ROADMAP.md) |
