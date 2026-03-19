# COCOON Project Analysis — Applicability to DPC Messenger

**Source:** https://github.com/TelegramMessenger/cocoon
**License:** Apache-2.0
**Authors:** Telegram engineering team
**Analyzed:** 2026-03-19

---

## What COCOON Is

COCOON (Confidential Compute Open Network) is a **decentralized AI inference marketplace** built by Telegram's engineering team. GPU owners earn TON cryptocurrency by serving LLM inference (via vLLM or SGLang) inside Intel TDX confidential VMs. The central guarantee: inference requests and responses are cryptographically private — not even the server owner running the GPU can read prompts or responses.

This is architecturally adjacent to DPC Messenger's Team Compute Pools feature: team members share GPU compute over P2P connections. The critical difference is the trust model. In DPC's current remote inference MVP (v0.6.1), the compute peer **does** see the assembled prompt (explicitly noted in `REMOTE_INFERENCE.md`). COCOON shows how to make remote inference verifiably private using hardware attestation.

---

## COCOON Architecture in Brief

```
Clients (e.g. Telegram backend)
    ↓  RA-TLS + TON payment
Proxies (TDX-protected, ~10–100)
    ↓  RA-TLS + TON payment
Workers (TDX-protected, ~1000+, one GPU each)
    ↓
vLLM / SGLang inside TDX confidential VM
```

**Three key hardware/software mechanisms:**
1. **Intel TDX** — encrypts GPU VM RAM; host OS/hypervisor cannot read it
2. **RA-TLS** — embeds TDX hardware attestation quotes in self-signed X.509 certificates; peers cryptographically verify that the remote code running is exactly what was expected
3. **SGX+TDX sealed storage** — persistent private key derivation tied to specific hardware + software image without any cloud KMS

---

## What DPC Messenger Can Directly Use

### 1. RA-TLS Certificate Pattern (High Value, Medium Effort)

**What it is:** Instead of a CA-signed TLS certificate that proves DNS identity, RA-TLS uses a self-signed X.509 certificate with an **embedded hardware attestation quote** in a critical custom OID extension. The verifying peer checks: "this code is running in a verified, unmodified TEE environment" rather than just "I trust this CA."

**DPC relevance — direct collision with existing design:**

DPC already issues self-signed X.509 certificates for node identity (`dpc-protocol/crypto.py`, `node.crt`). The node ID is a SHA-256 hash of the RSA public key. The certificate _already_ proves key ownership — RA-TLS extends this to also prove _code integrity_.

**COCOON's X.509 extension structure:**
```
X.509 Certificate (Ed25519, self-signed, 1-day validity)
├── Standard extensions (Key Usage, EKU: serverAuth+clientAuth)
└── Critical custom OID extensions:
    ├── 1.3.6.1.4.1.12345.1  →  TDX_QUOTE (~5–10 KB binary)
    └── 1.3.6.1.4.1.12345.2  →  TDX_USER_CLAIMS (32-byte public key hash)
```

**Anti-replay binding:** The Ed25519 public key is hashed into the TDX quote's `reportdata` field:
```
reportdata = SHA-512(UserClaims{ public_key })
```
This means a stolen quote from VM instance A cannot be attached to a different certificate (different key → different hash → verification fails).

**DPC adaptation path:**

For team members who run inference servers inside TDX VMs, DPC could issue an extended `node.crt` that carries a TEE attestation OID alongside the existing RSA key. The verifying peer (the requestor) would:

1. Accept the existing connection (standard TLS + DPC node identity)
2. Optionally parse the TEE attestation OID if present
3. If present and parsed successfully: mark the connection as `attestation_verified: true`
4. Display a "🔒 Verified TEE" badge in the UI next to the peer

Peers without TDX hardware continue to work exactly as today — the OID is simply absent. No protocol breaking change.

**Impact on remote inference privacy:**

Currently the compute peer sees the assembled prompt. With RA-TLS backing, a requesting peer can verify the inference server is running in a TDX VM where the host literally cannot read plaintext — making DPC's privacy guarantee for remote inference equivalent to local inference.

---

### 2. The `router` Binary Concept (Medium Value, Low Effort to Adapt)

**What it is:** COCOON's `router` is a standalone binary that handles RA-TLS as a transparent SOCKS5 proxy. Inner services (vLLM, the worker runner) connect outbound through SOCKS5; the router does the attestation verification before forwarding. Inbound: accepts TLS from attested parties, forwards to local service port.

```
vLLM (no attestation code) → SOCKS5 → router (does RA-TLS) → attested remote
```

**DPC relevance:** DPC's six-tier connection stack (`ConnectionOrchestrator`) already handles connection routing. A lightweight "attestation sidecar" process — analogous to COCOON's router — would let team members run any inference backend (Ollama, vLLM, LM Studio) without modifying it, while DPC intercepts and verifies attestation at the transport layer.

This is also the pattern used by llm-d (Kubernetes) and service mesh proxies (Envoy). COCOON shows a clean minimal C++ reference for a non-HTTP version.

---

### 3. dm-verity Model Integrity (High Value for Trustless Compute Sharing)

**What it is:** AI models are too large for direct TEE measurement registers. COCOON's solution:

1. `scripts/build-model` downloads the model from HuggingFace, creates a reproducible tar archive, generates a dm-verity hash
2. The verity hash is embedded in the kernel command line (measured into `RTMR[2]`)
3. The model name string carries the hash: `model@commit:verity_hash`
4. The host cannot tamper with model data — any modification causes I/O errors inside the VM

**DPC adaptation for `COMPUTE_ADVERTISE`:**

The `PeerCapabilityAd` struct (planned for Phase 2.3) currently carries:
```python
available_models: List[str]   # e.g. ["llama3:8b", "llama3:70b"]
```

It could carry:
```python
available_models: List[ModelDescriptor]

@dataclass
class ModelDescriptor:
    alias: str               # "llama3:70b"
    model_id: str            # "meta-llama/Llama-3.1-70B-Instruct"
    commit: str              # HuggingFace commit SHA
    verity_hash: str | None  # SHA-256 of model archive (COCOON format), None if unverified
    tee_attested: bool       # whether running in verified TEE
```

Requestors that care about model integrity can filter by `verity_hash` or `tee_attested`. Requestors that just want "any 70B model" ignore these fields. Zero friction for teams that don't have TDX hardware.

---

### 4. Proof-of-Work Connection Rate Limiting (Low Effort, Useful for Hub)

**What it is:** Before accepting a new TLS connection, the server sends a PoW challenge:
```
challenge = { difficulty_bits: int, salt: bytes[16] }
response: find nonce such that SHA256(salt || nonce) has <difficulty_bits leading zero bits
```

This is transport-agnostic (works over any byte stream) and requires no state on the server side — just verify the hash.

**DPC relevance:** The Hub (`dpc-hub`) is a public-facing WebSocket server. It currently has no rate-limiting beyond FastAPI's defaults. A configurable PoW challenge on the `/ws/signal` WebSocket upgrade would cost legitimate clients <50ms of CPU while making Sybil-scale connection floods 1000× more expensive.

This is simpler than token-based rate limiting and has no per-IP state to manage (important for Tor/VPN users).

Implementation is ~100 lines of Python + a matching ~50 lines of JS/TypeScript in the hub client.

---

### 5. Measurement Register Design Pattern (Architectural Insight)

**What it is:** COCOON repurposes TDX's user-available `RTMR[3]` register to measure the application's static config directory:

```
RTMR[3] extended with: SHA256(config_directory_contents)
RTMR[3] extended with: "cocoon:tdx_inited"
```

This means the TEE attestation covers not just the OS/firmware/kernel but also the application configuration — a worker running a different model or with different parameters produces a different attestation quote.

**DPC insight:** This pattern is relevant for DPC's device context and personal context system. If a future DPC node runs inside a TDX VM, the personal context or firewall rules file hash could be measured into RTMR[3] during startup. A requesting peer could verify: "not only is this running trusted DPC code, but it's running with the specific firewall configuration I expect." This is especially interesting for enterprise deployments where an organization wants to prove to auditors that the DPC node's access control configuration is exactly what was reviewed.

---

## What DPC Cannot Directly Reuse

### TON Blockchain Payments

COCOON's smart contract system handles payment micropayments per inference request. DPC Messenger has no payment layer and adding one is a major product decision, not a technical one. The TON integration code is deep in the codebase and tightly coupled to the COCOON-specific trust model.

**What to note instead:** The lazy batching pattern (commit to blockchain at ~50% stake consumed, not per-request) is a good general principle for any eventual DPC billing feature. Avoids per-request overhead while maintaining fraud-resistance guarantees.

### TDX+SGX Sealed Storage

The SGX+TDX hybrid key derivation (using `sgx_verify_report2()` to prove co-location and derive a persistent key) is elegant but requires both an Intel TDX-capable CPU **and** an SGX enclave running on the same physical host. Almost no team member will have this.

TDX 2.0's native `TDG.MR.KEY.GET` will eventually make this unnecessary, but that's 2027+ hardware.

**DPC already handles this differently:** node keys (`node.key`, `node.crt`) are stored in `~/.dpc/` and protected by OS file permissions. This is appropriate for DPC's threat model (trusted local OS, untrusted network).

### H100+ Hardware Requirement

COCOON explicitly rejects consumer GPUs. Confidential Computing mode requires NVIDIA's CC-mode VBIOS, which is only available on H100 and newer data center GPUs (and requires requesting from NVIDIA support). The RTX 3090/4090 cards that most DPC team members would contribute cannot run COCOON's worker.

This does not block the RA-TLS certificate pattern or dm-verity model hashes — those are independent of whether the GPU itself is in CC mode.

---

## Applicability Summary

| COCOON Component | DPC Use Case | Effort | Priority |
|---|---|---|---|
| **RA-TLS X.509 OID pattern** | Verifiable privacy for remote inference | Medium | Phase 3 |
| **Certificate-to-quote binding** | Anti-replay for attested node certs | Low (design follows) | Phase 3 |
| **dm-verity model hash in COMPUTE_ADVERTISE** | Trustless model identity for team pools | Low | Phase 2.3 |
| **`router` transparent RA-TLS proxy concept** | Attestation sidecar for existing inference backends | Medium | Phase 3 |
| **PoW connection rate limiting** | Hub DoS mitigation | Low | Phase 2.x |
| **RTMR[3] config measurement pattern** | Enterprise firewall rule auditability | Low (design only) | Phase 4 |
| **Full TDX VM infrastructure** | Premium "verified TEE" tier for high-trust teams | High | Phase 4 |
| **TON smart contract payments** | N/A — no payment layer planned | N/A | Not planned |
| **SGX sealed storage** | Not needed (OS file permissions sufficient) | N/A | Not planned |

---

## Recommended Near-Term Action: Model Hash in COMPUTE_ADVERTISE

The lowest-effort, highest-leverage change is adding an **optional** `model_commit` and `model_verity_hash` field to the planned `COMPUTE_ADVERTISE` protocol message (Phase 2.3).

```python
@dataclass
class PeerCapabilityAd:
    node_id: str
    team_ids: List[str]
    available_models: List[ModelDescriptor]   # replaces List[str]
    free_vram_gb: float
    queue_depth: int
    avg_latency_ms: int
    timestamp: float
    tee_attested: bool = False        # future: backed by RA-TLS cert

@dataclass
class ModelDescriptor:
    alias: str                        # display name ("llama3:70b")
    model_id: str | None              # HuggingFace ID if known
    commit: str | None                # HuggingFace commit SHA
    verity_hash: str | None           # sha256:<hex> of model archive
    tee_attested: bool = False        # model integrity verified by TEE
```

This is backward-compatible (existing ads with `available_models: List[str]` can be auto-converted to `ModelDescriptor(alias=s)`). It sets up the schema for Phase 3 TEE attestation without requiring any TEE hardware today.

---

## Longer-Term: RA-TLS as a DPC Privacy Upgrade

DPC's core privacy promise — "your context never leaves your device" — currently has a gap: remote inference. The assembled prompt (which includes your personal context) is sent to the compute peer over the encrypted P2P channel, but the peer's application layer can read it.

COCOON's RA-TLS pattern closes this gap:

```
Before (current):
  Bob's context → assembled prompt → encrypted TLS → Anna's vLLM [Anna can log this]

After (with RA-TLS):
  Bob's context → assembled prompt → encrypted TLS → Anna's TDX VM → vLLM [Anna cannot read RAM]
  + Bob verifies Anna's X.509 cert carries a TDX quote proving her inference server is unmodified
```

For DPC to implement this end-to-end:

1. **Anna side:** run vLLM inside a TDX-capable VM with a minimal COCOON-style wrapper (the `cocoon-init` + `gen-cert --tdx` portion, without the TON payment layer)
2. **Anna's DPC client:** include the TDX-backed certificate OID in her node's advertised capability
3. **Bob's DPC client:** when selecting compute host in `ProviderSelector.svelte`, show "🔒 TEE-verified" badge for attestation-capable peers; optionally require TEE attestation for high-sensitivity queries

The Apache-2.0 license allows DPC (GPL v3 client) to read and adapt the RA-TLS verification code. The relevant files are:
- `tee/cocoon/tdx.cpp` / `tdx.h` — quote parsing, policy verification
- `tee/cocoon/router.cpp` — transparent proxy architecture
- `docs/ra_tls.md` — design rationale

---

## References

- **COCOON repository:** https://github.com/TelegramMessenger/cocoon (Apache-2.0)
- **COCOON docs:** `docs/ra_tls.md`, `docs/tdx.md`, `docs/seal.md` (in repo)
- **Intel TDX specification:** https://www.intel.com/content/www/us/en/developer/tools/trust-domain-extensions/overview.html
- **RA-TLS draft standard:** draft-fossati-tls-attestation (IETF TLS WG)
- **dm-verity:** https://www.kernel.org/doc/html/latest/admin-guide/device-mapper/verity.html
- **NVIDIA Confidential Computing:** https://www.nvidia.com/en-us/data-center/solutions/confidential-computing/
- **LMCache** (cross-session KV cache sharing): https://docs.lmcache.ai — complementary to COCOON for multi-user serving
- **DPC internal:**
  - `docs/REMOTE_INFERENCE.md` — current MVP (the gap COCOON closes)
  - `docs/TEAM_COMPUTE_POOLS.md` — Phase 2.3 design (add ModelDescriptor here)
  - `dpc-protocol/dpc_protocol/crypto.py` — existing X.509 certificate generation (extend with TEE OIDs)
