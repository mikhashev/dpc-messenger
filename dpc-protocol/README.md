# dpc-protocol

Shared protocol library for D-PC Messenger — the part that lives on
both ends of a peer-to-peer connection and defines the wire format.

**License:** LGPL v3

---

## What's in it

| Module | Purpose |
|--------|---------|
| `crypto.py` | Node identity: RSA keys, X.509 self-signed certs, `dpc-node-*` IDs derived from the public key |
| `protocol.py` | DPTP wire format (10-byte ASCII length header + UTF-8 JSON payload), `read_message`/`write_message`, and `create_*` helpers for the built-in message types |
| `pcm_core.py` | Personal Context Model dataclasses — `Profile`, `Topic`, `KnowledgeEntry`, `InstructionBlock`, bias-mitigation fields, etc. |
| `knowledge_commit.py` | Dataclasses for the knowledge-commit flow (`KnowledgeCommitProposal`, `KnowledgeCommit`, `CommitVote`, and the DPTP propose/vote/apply messages) |
| `commit_integrity.py` | Canonical JSON + hash verification for knowledge commits |
| `markdown_manager.py` | Links topics to external markdown files (PCM v2.0) |
| `utils.py` | Small helpers |

For the full DPTP wire-format spec (message types, framing rules,
versioning), see [`specs/dptp_v1.md`](../specs/dptp_v1.md). That file is
the source of truth for the protocol; this library implements it.

---

## Install

### In the monorepo (development)

```bash
cd dpc-protocol
uv sync
```

Both `dpc-client` and `dpc-hub` depend on this package via a local
path in their `pyproject.toml`.

### As a standalone dependency

```bash
uv add dpc-protocol
# or
pip install dpc-protocol
```

### Requirements

- Python 3.12+
- `cryptography` 46.0.3+

---

## Identity files

On first use, the library creates three files under `~/.dpc/`:

- `node.key` — RSA-2048 private key (PEM, unencrypted — protect the directory)
- `node.crt` — self-signed X.509 certificate (10-year validity)
- `node.id` — text file with the node identifier, e.g. `dpc-node-8b066c7f3d7eb627`

Node IDs are deterministic: the same key pair always produces the
same ID. There is no central authority; identity is self-sovereign.

---

## Transport

The protocol is transport-agnostic. `read_message` and `write_message`
take asyncio `StreamReader`/`StreamWriter`, so anything that yields a
reliable byte stream works — TLS sockets, WebRTC data channels, a
localhost pipe for tests.

---

## Tests

```bash
uv run pytest
```

Current test coverage is intentionally narrow — integration tests
live in `dpc-client` and `dpc-hub` where they exercise the wire
format against real sockets.

---

## Related

- [`specs/dptp_v1.md`](../specs/dptp_v1.md) — DPTP wire-format spec
- [`specs/hub_api_v1.md`](../specs/hub_api_v1.md) — Federation Hub API
- [`dpc-client/README.md`](../dpc-client/README.md) — client that uses this library
- [`README.md`](../README.md) — project overview
