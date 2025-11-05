# D-PC Messenger

> **Status:** 🚧 In Development 🚧 | **Version:** 0.1.0 (Federated MVP)

D-PC Messenger is a proof-of-concept for a transactional, privacy-first messenger designed for the AI era. It is the reference implementation of the **Decentralized Personal Context (D-PC)** protocol.

Our vision is a future where users interact with each other through the medium of local AI assistants. D-PC aims to become the **"SMTP for AI"**: an open, interoperable standard that enables these assistants to securely exchange knowledge and share computational resources on behalf of their users.

This repository contains the active development of our **Federated MVP**, a pragmatic, user-friendly application designed to prove the value of this vision.

---

## 💡 Core Concepts

D-PC is built on three core ideas that differentiate it from traditional platforms:

1.  **Transactional Communication:** We treat conversations not as an endless history to be stored, but as a transaction to produce a result. The outcome of a chat is a "Knowledge Commit"—a structured, verified update to the participants' knowledge bases. We extract the **signal** (knowledge) and discard the **noise** (chatter).

2.  **The Contextual Knowledge Graph:** The network is modeled as a graph where users are nodes and relationships are edges. This allows a user's AI assistant to intelligently traverse their social graph to find relevant knowledge from trusted sources, replacing AI "guesses" with verifiable facts.

3.  **The Decentralized Compute Pool:** Users can securely and privately share their local AI inference capabilities with trusted peers. This democratizes access to powerful AI models, allowing a user with a laptop to "borrow" the GPU of a friend with a powerful desktop, all over an end-to-end encrypted channel.

For a deep dive into the project's philosophy and long-term architecture, please read our [**Whitepaper**](./whitepaper.md).

---

## 🏛️ Architecture: The Pragmatic Path to Decentralization

Based on a critical analysis of the challenges in P2P adoption, we are following a phased "Bridge Strategy":

1.  **Phase 1 (Current): Federated MVP.** We are building a user-friendly desktop application that uses a central "Hub" for discovery and connection brokering. This allows us to deliver value immediately without the UX friction of pure P2P.
2.  **Phase 2: Federation.** We will open the Hub protocol, allowing anyone to run their own interoperable Hubs.
3.  **Phase 3: P2P.** We will integrate a pure P2P mode for users who require maximum sovereignty.

This repository is focused on **Phase 1**.

## 📦 Project Structure (Monorepo)

This repository is a monorepo containing several distinct but related sub-projects:

-   [`dpc-hub/`](./dpc-hub/): The server-side **Federation Hub** (FastAPI, PostgreSQL).
-   [`dpc-client/`](./dpc-client/): The cross-platform **Desktop Messenger Client** (Tauri, SvelteKit, Python).
-   [`dpc-protocol/`](./dpc-protocol/): A shared Python library containing the core data structures and protocol logic.
-   [`specs/`](./specs/): Formal specifications for the D-PC protocols and APIs.

Each sub-project has its own `README.md` with specific setup and development instructions.

---

## 🚀 Getting Started (Running the MVP)

To run the full D-PC Messenger MVP, you will need to run both the Hub and the Client.

### Prerequisites
-   Python `3.12+` & Poetry
-   Node.js `18+` & npm
-   Rust (install via [rustup.rs](https://rustup.rs/))
-   Docker

### Quick Start
1.  **Run the Hub:** Follow the instructions in [`dpc-hub/README.md`](./dpc-hub/README.md).
2.  **Run the Client:** Follow the instructions in [`dpc-client/README.md`](./dpc-client/README.md).

---

## 🤝 Contributing

We are building an open standard and welcome contributions of all kinds!

1.  **Read our Vision:** Start with the [**Whitepaper**](./whitepaper.md) to understand our long-term goals.
2.  **Contributor Agreement:** All code contributions require signing our [**Contributor License Agreement (CLA)**](./CLA.md). This is automatically handled by the CLA Assistant bot on your first pull request.
3.  **Find an Issue:** Check out the [Issues tab](https://github.com/mikhashev/dpc-messenger/issues) to find good first issues.
4.  **Discuss:** For larger changes, please open a [Discussion](https://github.com/mikhashev/dpc-messenger/discussions) first to propose your idea.

## 📜 Licensing

D-PC uses a **Progressive Copyleft** licensing strategy to protect user privacy, ensure the protocol remains open, and enable a sustainable development model.

-   **Messenger Client:** `GPL v3`
-   **Protocol Libraries:** `LGPL v3`
-   **Federation Hub:** `AGPL v3`
-   **Protocol Specifications:** `CC0`

For a detailed explanation of this choice, please see our main [**LICENSE.md**](./LICENSE.md) file. Commercial licenses are also available for enterprises that require proprietary modifications.