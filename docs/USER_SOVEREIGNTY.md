# User Sovereignty and Privacy in D-PC Messenger

**Version:** 1.0
**Status:** Core Philosophy Document

---

## Executive Summary

D-PC Messenger is built on a fundamental principle: **You own your data, your identity, and your digital self.** This document outlines our philosophy of user sovereignty and explains how we implement it technically.

**Core Tenets:**
1. **Your data never leaves your control** - Personal knowledge, contacts, and preferences stay on your device
2. **No backdoors, no exceptions** - We cannot access your data even if we wanted to
3. **Portability without compromise** - Transfer your digital identity securely between devices
4. **Transparency by design** - Open protocols, open source, no hidden data collection

---

## The Problem: Digital Feudalism

### How Most Services Work Today

```
┌─────────────────────────────────────────┐
│  Centralized Service (Google, Meta)     │
│  ┌───────────────────────────────────┐  │
│  │ YOUR Data (they control it)       │  │
│  │ - Messages, photos, contacts      │  │
│  │ - Profile, preferences, habits    │  │
│  │ - Social graph, relationships     │  │
│  │ - Location history, behavior      │  │
│  └───────────────────────────────────┘  │
│                                          │
│  They decide:                            │
│  - Who can access your data              │
│  - What happens to it (ads, training AI)│
│  - When to delete your account           │
│  - Where your data is stored             │
└─────────────────────────────────────────┘
           ↓
    You are the product
```

**The Reality:**
- You don't own your conversations - they do
- You can't export your social graph - it's locked in
- You can't control who sees your data - governments, advertisers, hackers
- You can't delete it permanently - backups exist forever
- You can't switch platforms - network effects trap you

This is **digital feudalism**: You work the land (create content), they own everything.

---

## The D-PC Vision: Digital Self-Sovereignty

### How D-PC Works

```
┌─────────────────────────────────────────┐
│  YOUR Device (you control it)           │
│  ┌───────────────────────────────────┐  │
│  │ YOUR Data (.dpc directory)        │  │
│  │ - personal.json (knowledge)       │  │
│  │ - .dpc_access (social graph)      │  │
│  │ - node.key (identity)             │  │
│  │ - providers.toml (preferences)    │  │
│  └───────────────────────────────────┘  │
│                                          │
│  YOU decide:                             │
│  │ ✅ Who can see what (firewall rules) │
│  │ ✅ Where to store backups            │
│  │ ✅ When to share compute power       │
│  │ ✅ Which AI providers to use         │
│  └───────────────────────────────────┘  │
└─────────────────────────────────────────┘
           ↓
    You are the sovereign
```

**Your Rights:**
1. **Right to Privacy**: Your data is encrypted, never seen by servers
2. **Right to Portability**: Transfer your digital self to any device
3. **Right to Deletion**: `rm -rf ~/.dpc/` - it's gone forever
4. **Right to Transparency**: Open source code, auditable protocols
5. **Right to Choose**: Select your own AI providers, storage, contacts

---

## Technical Implementation

### 1. Local-First Data Storage

**Principle:** All sensitive data lives on your device in the `~/.dpc/` directory.

```
~/.dpc/
├── personal.json        # Your knowledge graph (AI's understanding of you)
├── .dpc_access          # Your social graph (who can access what)
├── node.key             # Your cryptographic identity
├── node.crt             # Your certificate
├── providers.toml       # Your AI provider preferences
└── config.ini           # Your settings
```

**What this means:**
- The Hub server **never** sees personal.json or .dpc_access
- Messages are **never** stored on servers (peer-to-peer only)
- Your knowledge belongs to **you**, not a company

**From PRODUCT_VISION.md:**
> "We treat a dialogue not as a stream of messages to be saved, but as a **transaction** whose purpose is to **change the state of collective knowledge**. The outcome of a conversation is not an endless scroll of messages, but a clean, structured, and verified 'Knowledge Commit' that updates the Personal Contexts of the participants."

This is revolutionary: Instead of hoarding your messages forever (surveillance capitalism), DPC **extracts knowledge and discards ephemeral chatter**.

---

### 2. End-to-End Encryption

**Principle:** Messages are encrypted on your device and only decrypted on the recipient's device.

```
Alice's Device                    Bob's Device
┌──────────────┐                 ┌──────────────┐
│ Plaintext    │                 │ Plaintext    │
│ "Hello Bob"  │                 │ "Hello Bob"  │
└──────┬───────┘                 └───────┬──────┘
       │ Encrypt                         │ Decrypt
       ↓                                 ↑
   [Encrypted]                       [Encrypted]
       │                                 ↑
       └────────────[P2P]────────────────┘

       ❌ Hub never sees plaintext
       ❌ No server-side message storage
       ✅ Only Alice and Bob can read
```

**Implementation:**
- TLS 1.2+ for direct connections (local network)
- WebRTC with DTLS-SRTP for internet connections
- X.509 certificate validation (node identities)
- Perfect forward secrecy (session keys)

**What the Hub sees:**
- Your email address (for OAuth login)
- Your node_id (public identifier, like a username)
- WebRTC signaling data (ICE candidates, SDP offers)

**What the Hub NEVER sees:**
- Message contents
- Personal knowledge (personal.json)
- Contact relationships (.dpc_access)
- Private keys

---

### 3. User-Controlled Encrypted Backups

**The Problem:**
How do you transfer your digital self to a new device without exposing it to cloud providers or creating security vulnerabilities?

**Traditional Solutions (All Flawed):**

| Approach | Problem |
|----------|---------|
| Cloud Sync (Dropbox, Google Drive) | Provider can read your data |
| Password Manager (1Password, LastPass) | Single point of failure, centralized |
| Hardware Wallet (Ledger) | Expensive ($100+), can be lost |
| Blockchain | Transaction fees, public ledger, complexity |

**D-PC Solution: User-Controlled Encrypted Bundle**

```
┌─────────────────────────────────────────────────────┐
│  Step 1: User Creates Backup                        │
│  $ dpc backup create --output ~/my_backup.enc       │
│                                                      │
│  🔐 Enter passphrase: ****************             │
│  🔐 Confirm passphrase: ****************            │
│                                                      │
│  📦 Compressing .dpc directory...                   │
│  🔐 Encrypting with AES-256-GCM...                  │
│  ✅ Backup created: my_backup.enc (2.4 MB)          │
│                                                      │
│  ⚠️  IMPORTANT:                                      │
│     Save this passphrase! Without it, backup is     │
│     PERMANENTLY UNRECOVERABLE (by design).          │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  Step 2: User Stores Backup (Multiple Options)      │
│                                                      │
│  [1] USB Drive (offline, maximum security)          │
│  [2] Cloud Storage (encrypted before upload)        │
│  [3] Hub Storage (encrypted, convenience)           │
│  [4] QR Code (transfer to nearby device)            │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  Step 3: Restore on New Device                      │
│  $ dpc backup restore --input ~/my_backup.enc       │
│                                                      │
│  🔐 Enter passphrase: ****************              │
│                                                      │
│  🔓 Decrypting...                                    │
│  📦 Extracting files...                              │
│  ✅ Restored to ~/.dpc/                              │
│                                                      │
│  Files restored:                                     │
│    ✅ personal.json (your knowledge)                 │
│    ✅ .dpc_access (your contacts)                    │
│    ✅ providers.toml (your preferences)              │
│    ✅ node.key (your identity)                       │
└─────────────────────────────────────────────────────┘
```

**Security Properties:**

1. **Client-Side Encryption**
   - Encryption happens on YOUR device, before any network transmission
   - Passphrase never leaves your device
   - Backup is useless without your passphrase

2. **No Backdoors**
   - We use Argon2id (memory-hard KDF) - even we cannot crack it
   - No "password reset" - if you lose passphrase, data is permanently lost
   - No "master key" - no way for authorities to compel access

3. **Strong Cryptography**
   - AES-256-GCM (authenticated encryption, NIST approved)
   - Argon2id (winner of Password Hashing Competition)
   - Random salt + nonce (unique per backup)
   - HMAC for tamper detection (built into GCM)

4. **User Choice**
   - Store on USB drive (maximum security, offline)
   - Store on cloud (encrypted, convenient)
   - Store on Hub (encrypted, integrated)
   - Transfer via QR code (local network only)

**Contrast with Competitors:**

| Feature | D-PC | WhatsApp | Signal | Telegram |
|---------|------|----------|--------|----------|
| Message E2E Encryption | ✅ Yes | ✅ Yes | ✅ Yes | ⚠️ Optional |
| Knowledge Graph Private | ✅ Yes | ❌ No concept | ❌ No concept | ❌ No concept |
| Encrypted Backups | ✅ User-controlled | ⚠️ Google Drive (if enabled) | ⚠️ iOS only | ❌ Cloud plaintext |
| Server Sees Messages | ❌ Never | ❌ Never | ❌ Never | ✅ Yes (non-secret chats) |
| Backup Portability | ✅ Any device | ⚠️ Same phone number | ⚠️ Same phone number | ✅ Cloud sync |
| Open Source | ✅ Full | ⚠️ Client only | ✅ Full | ⚠️ Client only |
| User Owns Data | ✅ Yes | ⚠️ Depends | ⚠️ Depends | ❌ No |

---

### 4. No Telemetry, No Analytics

**What D-PC Does NOT Collect:**

❌ Usage statistics
❌ Crash reports
❌ Feature usage metrics
❌ Device fingerprinting
❌ Network analysis
❌ Behavioral tracking
❌ A/B testing data
❌ Marketing analytics

**What D-PC DOES Collect:**

✅ Nothing (by default)
✅ Optional: Self-hosted crash logs (if user enables)
✅ Hub: Email address (for OAuth), node_id (public identifier)

**Comparison:**

```
Traditional App (e.g., Facebook Messenger):
┌──────────────────────────────────────────────────┐
│ Every action is logged:                          │
│ - What you type (before you send it!)           │
│ - Who you message, when, how often               │
│ - What features you use                          │
│ - How long you spend in app                      │
│ - Your contacts, location, device info           │
│ - Cross-app tracking (Facebook, Instagram...)    │
└──────────────────────────────────────────────────┘

D-PC Messenger:
┌──────────────────────────────────────────────────┐
│ No telemetry. Period.                            │
│                                                   │
│ We cannot track you because:                     │
│ 1. Messages are P2P (never touch our servers)   │
│ 2. No analytics SDK integrated                   │
│ 3. Open source (you can verify)                  │
│ 4. Local-first (data stays on your device)      │
└──────────────────────────────────────────────────┘
```

---

### 5. Federation, Not Federation-Washing

**Real Federation:**
- Anyone can run a Hub server (open source)
- Hubs are **signaling servers only** (WebRTC coordination)
- Users choose which Hub to trust
- Switching Hubs doesn't lose your data (local-first)

**What D-PC Hub Sees:**
```json
{
  "email": "alice@example.com",
  "node_id": "dpc-node-abc123",
  "public_key": "MIIBIjANBgkq...",
  "online_status": "connected",
  "signaling_data": { /* WebRTC SDP offers */ }
}
```

**What D-PC Hub NEVER Sees:**
- personal.json (knowledge graph)
- .dpc_access (social graph)
- Message contents
- Private keys
- AI provider credentials

**Compare to "Federated" Services (e.g., Mastodon, Matrix):**
- They see all your posts/messages (server has plaintext)
- They store your social graph (server knows all your relationships)
- Switching servers is complex (data export/import)
- Trust the server admin (no E2E encryption by default)

**D-PC is different:**
- Hub is dumb relay (only WebRTC signaling)
- Your data never touches the Hub
- Switching Hubs is trivial (just change config.ini)
- Zero-trust architecture (even malicious Hub can't read your data)

---

## Privacy Threat Model

### What We Protect Against

✅ **Honest-but-Curious Server**
- Scenario: Hub operator wants to snoop
- Protection: E2E encryption, Hub never sees plaintext

✅ **Compelled Disclosure**
- Scenario: Government demands user data
- Protection: We don't have it (local-first storage)

✅ **Database Breach**
- Scenario: Hub database stolen by hackers
- Protection: Only public identifiers exposed (email, node_id)

✅ **Man-in-the-Middle**
- Scenario: ISP/government intercepts traffic
- Protection: TLS/DTLS encryption, certificate validation

✅ **Device Theft**
- Scenario: Laptop stolen
- Protection: Encrypted backups with strong passphrase

✅ **Cloud Provider Snooping**
- Scenario: Dropbox reads your backup
- Protection: Client-side encryption before upload

✅ **Insider Threat**
- Scenario: Rogue D-PC employee
- Protection: No access to user data (we don't store it)

### What We DON'T Protect Against (Yet)

⚠️ **Device Compromise**
- Scenario: Malware on your device
- Limitation: If your device is compromised, game over
- Future: Hardware wallet integration for key storage

⚠️ **Quantum Computing**
- Scenario: Future quantum computers break RSA
- Limitation: Current crypto (RSA-2048) vulnerable to quantum attacks
- Future: Post-quantum cryptography (e.g., Kyber, Dilithium)

⚠️ **Passphrase Weakness**
- Scenario: User chooses "password123"
- Limitation: Argon2id is slow, but weak passphrase = weak encryption
- Mitigation: UI warns about weak passphrases, suggests strong ones

⚠️ **Rubber-Hose Cryptanalysis**
- Scenario: $5 wrench attack (physical coercion)
- Limitation: No technical solution
- Philosophy: We give you the tools; you decide the risk model

---

## Comparison with Existing Solutions

### vs. Traditional Messengers (WhatsApp, Telegram)

| Aspect | WhatsApp | Telegram | D-PC |
|--------|----------|----------|------|
| E2E Encryption | ✅ Yes | ⚠️ Optional | ✅ Always |
| Message Storage | 🏢 Server (encrypted) | 🏢 Server (plaintext) | 💻 Never stored |
| Knowledge Extraction | ❌ No | ❌ No | ✅ Yes (PCM) |
| Backup Encryption | ⚠️ Google Drive | ❌ Cloud plaintext | ✅ User passphrase |
| Open Source | ⚠️ Client only | ⚠️ Client only | ✅ Full stack |
| Metadata Collection | ⚠️ Yes | ⚠️ Yes | ❌ Minimal |
| User Owns Data | ❌ No | ❌ No | ✅ Yes |

### vs. Decentralized Platforms (Matrix, XMPP)

| Aspect | Matrix | XMPP | D-PC |
|--------|--------|------|------|
| Federation | ✅ Yes | ✅ Yes | ✅ Yes |
| Server Sees Messages | ✅ Yes (plaintext) | ✅ Yes (plaintext) | ❌ No (P2P) |
| E2E Encryption | ⚠️ Olm (opt-in) | ⚠️ OMEMO (extension) | ✅ Built-in |
| Server Storage | 🏢 All messages | 🏢 All messages | 💻 Local only |
| AI Integration | ❌ No | ❌ No | ✅ Core feature |
| Compute Sharing | ❌ No | ❌ No | ✅ Novel feature |

### vs. Blockchain Messengers (Status, Session)

| Aspect | Status | Session | D-PC |
|--------|--------|---------|------|
| Decentralization | ✅ Ethereum-based | ✅ Lokinet | ⚠️ Federated hubs |
| Transaction Costs | ❌ Gas fees | ✅ Free | ✅ Free |
| Scalability | ⚠️ Limited | ✅ Good | ✅ Unlimited (P2P) |
| Complexity | ❌ High (crypto wallet) | ⚠️ Medium | ✅ Low (familiar UX) |
| AI Integration | ❌ No | ❌ No | ✅ Yes |
| Knowledge Extraction | ❌ No | ❌ No | ✅ Yes |

### vs. Privacy-First Messengers (Signal)

| Aspect | Signal | D-PC |
|--------|--------|------|
| E2E Encryption | ✅ Best-in-class | ✅ Strong |
| Open Source | ✅ Yes | ✅ Yes |
| Metadata Protection | ✅ Sealed Sender | ⚠️ Hub knows online status |
| Server Storage | 🏢 Minimal | 💻 None |
| Backup | ⚠️ iOS only, encrypted | ✅ Cross-platform, user-controlled |
| Knowledge Management | ❌ No | ✅ Core feature |
| Compute Sharing | ❌ No | ✅ Novel feature |
| Federation | ❌ Centralized | ✅ Yes |

**Bottom Line:** D-PC combines Signal's privacy, Matrix's federation, and adds AI-powered knowledge management + compute sharing.

---

## The Philosophy: Cypherpunk Principles

D-PC is built on the foundational ideas of the cypherpunk movement:

1. **"Privacy is necessary for an open society"** (Eric Hughes, 1993)
   - Implementation: E2E encryption, local-first data, no telemetry

2. **"Code is speech"** (Bernstein v. United States, 1996)
   - Implementation: Open source, auditable protocols

3. **"Don't trust, verify"** (Bitcoin ethos)
   - Implementation: Open protocols, reproducible builds, cryptographic verification

4. **"Information wants to be free"** (Stewart Brand, 1984)
   - Implementation: Open protocols (DPTP, PCS), LGPL-licensed libraries

5. **"The Net interprets censorship as damage and routes around it"** (John Gilmore, 1993)
   - Implementation: P2P architecture, federation, no central authority

---

## User Experience: Privacy Without Friction

**The Challenge:** Most privacy tools are hard to use (PGP email, Tor Browser, etc.).

**D-PC's Approach:** Privacy by default, convenience without compromise.

### Example: Backing Up Your Digital Self

**Bad UX (Traditional Privacy Tools):**
```
1. Generate PGP keypair (complicated)
2. Export private key to file
3. Encrypt file with GPG (command line)
4. Upload to cloud manually
5. Remember 30-character encryption passphrase
6. Pray you don't lose it
```

**Good UX (D-PC):**
```
1. Click "Backup" button in UI
2. Enter memorable passphrase (app suggests strong one)
3. Choose storage location (USB, cloud, Hub)
4. Done! Backup is encrypted automatically
```

**Restoration:**
```
1. Install D-PC on new device
2. Click "Restore from Backup"
3. Select backup file
4. Enter passphrase
5. Done! Knowledge, contacts, identity restored
```

### Example: Sharing Knowledge with a Friend

**Bad UX (Traditional Approach):**
```
1. Export your notes to file
2. Manually redact sensitive parts
3. Upload to shared folder
4. Hope they have the right permissions
5. No control after sharing
```

**Good UX (D-PC):**
```
1. Add friend: "Bob" (dpc-node-bob123)
2. Edit .dpc_access.json firewall:
   {
     "nodes": {
       "dpc-node-bob123": {
         "personal.json:profile.*": "allow",
         "work_notes.json:project_alpha.*": "allow"
       }
     }
   }
3. Done! Bob can only access what you specified
4. Revoke anytime by removing the rule
```

---

## Roadmap: Future Enhancements

### Phase 1 (v0.6): Encrypted Local Backup ✅
- User-controlled passphrase encryption
- Save to USB, cloud, or Hub
- Restore on new device

### Phase 2 (v0.7): Hub-Assisted Backup
- Optional encrypted upload to Hub
- Automatic periodic backups
- Version history (differential backups)

### Phase 3 (v0.8): Social Recovery (Shamir Sharing)
- Split backup into N shares
- Distribute to trusted contacts
- Recover with M-of-N shares (e.g., 3 of 5)

### Phase 4 (v1.0): Hardware Security
- Hardware wallet integration (Ledger, YubiKey)
- TPM-based key storage (Windows, Linux)
- Secure Enclave (macOS, iOS)

### Phase 5 (v1.1): Advanced Privacy
- Onion routing (Tor integration)
- Traffic analysis resistance
- Metadata minimization (noise injection)

### Phase 6 (v1.2): Post-Quantum Cryptography
- Kyber (key encapsulation)
- Dilithium (signatures)
- Migration from RSA to quantum-resistant algorithms

### Phase 7 (v2.0): Decentralized Identity (DID)
- W3C DID standard support
- Self-sovereign identity (SSI)
- Verifiable credentials

---

## Governance: Community-Driven

**D-PC Protocol:** LGPL v3 (copyleft, ensures derivatives stay open)
**Hub Server:** AGPL v3 (network copyleft, prevents proprietary cloud services)
**Client:** GPL v3 (user freedom, no lock-in)

**What This Means:**
- No single company can control D-PC
- Anyone can fork, modify, improve
- Proprietary versions must share source code
- Commercial use allowed (with GPL compliance)

**Development Model:**
- Public roadmap (GitHub)
- Open RFCs for protocol changes
- Community voting on major features
- Transparent security audits

---

## Conclusion: You Are the Sovereign

In the D-PC ecosystem:

✅ You control your data (local-first storage)
✅ You control your identity (cryptographic keys)
✅ You control your privacy (encrypted backups)
✅ You control your network (choose your Hub)
✅ You control your AI (choose your providers)

**This is not just a technical implementation - it's a philosophy.**

We believe:
- Privacy is a human right, not a luxury
- Users should own their digital selves
- Convenience and security are not mutually exclusive
- Open protocols beat closed platforms

**The question is not "Can we build privacy-respecting tools?"**
**The question is "Will users choose freedom over convenience?"**

D-PC's answer: You shouldn't have to choose. You can have both.

---

## Learn More

- [Business Vision](../VISION.md)
- [Product Vision](../PRODUCT_VISION.md)
- [Technical Architecture](../CLAUDE.md)
- [Backup Implementation](./BACKUP_RESTORE.md)
- [Firewall Rules](./.dpc_access.example)
- [Protocol Specification](../specs/DPTP.md)

**Questions? Concerns? Contributions?**
Join our community: [GitHub Discussions](https://github.com/mikhashev/dpc-messenger/discussions)

---

**Remember:** With great power comes great responsibility. D-PC gives you sovereignty, but sovereignty means **you** are responsible for your data. Keep your passphrases safe. Back up regularly. Think before you share.

**You are the sovereign. Act like it.**
