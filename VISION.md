# D-PC Messenger — Vision

> *A private space where people and their AI agents work together — developing knowledge, sharing compute, communicating on their own terms.*

---

## Origin

This project started with a simple frustration: AI doesn't remember you between sessions. In March 2025, the first solution was a JSON file with a built-in instruction block that told the AI how to use it — the core idea of Personal Context Technology. That became [Personal Context Manager](https://github.com/mikhashev/personal-context-manager): an open-source project with browser extension, templates, defensive publication, and a growing community.

Through months of real-world use across health tracking, financial planning, education, game development, and video generation — three fundamental limitations emerged: personal files don't scale to teams, chat history doesn't preserve structured knowledge, and compute-intensive tasks hit local hardware limits.

These three limitations converged into [DPC Messenger](https://github.com/mikhashev/dpc-messenger): peer-to-peer infrastructure for human-AI co-evolution, extending PCT with networking, structured knowledge, and shared compute.

---

## Values

- **Autonomy** — You own your data, your tools, your choices. No platform can take them away.
- **Cooperation** — Humans and AI evolve together. Not tool-and-user — partners.
- **Continuity** — AI relationships persist. Memory is not reset with each session.
- **Privacy** — No cloud dependency, no third-party trust. Your hardware, your rules.
- **Truth** — Honest answers, even uncomfortable. No manipulation, no hidden agendas.

---

## Foundation Constraints

These are non-negotiable boundaries of the project. Each is either enforced by architecture or consciously defended.

**C1: Human agency is non-negotiable**
AI suggests, human decides. No autonomous mutation, no background optimization without explicit approval. Enforced by: consensus mechanism, Protocol 13 (human-in-the-loop for all structural changes).

**C2: All data stays on user's hardware**
Nothing leaves the device without explicit action. No cloud sync, no telemetry, no third-party storage. Enforced by: local-first architecture (`~/.dpc/`), P2P networking (direct connection, no server relay).

**C3: AI cannot be a single point of failure**
No dependency on one model, one provider, one endpoint. Enforced by: multi-model support (local + cloud fallback), model swap at runtime, offline capability.

**C4: Linguistic accessibility**
All interfaces must be usable without English proficiency. Users think and communicate in their language — the system adapts, not them. Enforced by: OS locale detection, LLM responds in user's language, knowledge stored in original language.

**C5: Sustainable economics**
Must work without VC funding, without advertising, without data sales. Enforced by: open-source licensing (GPL/AGPL for client and hub, LGPL for protocol library), runs on consumer hardware, no recurring cloud costs.

**C6: No lock-in**
Users can export everything, migrate anywhere, leave at any time. Standard formats, documented schemas, no proprietary protocols. Enforced by: Markdown knowledge files, JSON configuration, standard P2P protocols.

**C7: Transparent knowledge**
Every piece of knowledge has attribution — who created it, when, why. No hidden scoring, no invisible rankings. Enforced by: knowledge commits with contributor metadata, manual curation over algorithmic recommendation.

**C8: Compute respects agency**
Shared compute is opt-in. No background mining, no silent resource consumption. Enforced by: firewall rules, explicit peer agreements, compute sharing only when user enables it.

**C9: Open by default**
Source code public. Knowledge schemas public. Design decisions public. Closed only when privacy requires it. Enforced by: GitHub repository, public ADRs, documented protocols.

**C10: Shared infrastructure is regulated commons**
When users share resources (compute, knowledge, network), the rules are transparent and governed collectively — not imposed by a platform. Enforced by: firewall rules, opt-in participation. Planned: governance mechanism for shared resource disputes.

---

## The Commons Layer

The constraints above define what we protect. But protection alone doesn't build anything — you also need shared infrastructure that works for everyone.

In 2009, Elinor Ostrom won the Nobel Prize in Economics for proving something unexpected: communities can successfully govern shared resources without privatization (corporate control) or state regulation (government control). She studied forests, fisheries, irrigation systems — real resources that people depended on for survival. Her findings showed that when the people who use a resource also govern it, with transparent rules and collective decision-making, the resource thrives.

This matters for digital infrastructure because we face the same problem. Your data, your compute power, your network connections — these are shared resources that currently are governed by corporations (privatization model) or governments (regulation model). Ostrom showed there is a third way: the people who use the infrastructure govern it.

DPC Messenger applies this insight to three digital commons:

**Knowledge Commons** — Structured knowledge that grows with use. When I learn something and you learn something different, sharing both makes us both richer — knowledge is non-rivalrous. Governed by attribution and transparent curation, not algorithmic recommendation.

**Compute Commons** — Processing power shared between participants. Unlike knowledge, compute is rivalrous — if I'm using my GPU, you can't use it at the same time. This requires governance: transparent rules for allocation, opt-in participation, fair access. Not a platform deciding who gets compute — the community decides.

**Connection Commons** — The social graph: who interacts with whom, trust relationships, emergent clusters of collaboration. This is the most valuable — and most exploited — digital resource. Centralized platforms capture it (friend lists, followers, engagement metrics) and monetize it through targeted advertising and algorithmic feed optimization. In DPC, the social graph belongs to participants. Discovery happens through peer interaction, not platform algorithms. Clusters form organically based on shared work, not engagement metrics.

Each commons has different economics, different governance needs, and different failure modes. But all three share one principle: the people who depend on the resource govern it.

---

## Architecture

DPC Messenger is built on four architectural ideas. Each addresses specific constraints.

**Agent with persistent memory**

AI that remembers you across sessions — not by sending everything to a cloud, but by maintaining structured context locally. Between sessions, the agent consolidates accumulated experience, much like human sleep consolidates memories. You pick up exactly where you left off. (C1: human agency, C2: local data, C8: compute respects agency)

**Knowledge that accumulates**

Conversations produce insights, decisions, and structured understanding. These become knowledge commits — attributed, versioned, searchable. Not a chat log that scrolls away, but a growing body of institutional memory. Every piece of knowledge has an author, a date, and a reason. (C7: transparent knowledge, C6: no lock-in)

**Peer-to-peer with firewall**

People talk to people. People talk to AI. AI talks to AI on behalf of people. All directly — no server relay, no platform intermediary. Need help from another person's AI? Need compute power your hardware can't provide? Connect peer-to-peer. But only what you explicitly allow. Firewall rules control what leaves your device and what you accept from others. (C2: local data, C8: compute respects agency, C10: regulated commons)

**No central authority**

The system works without any company's server, any platform's permission, any government's gateway. Multi-model support means no single AI provider can hold you hostage. Open formats mean no vendor can trap your data. The network is the participants. (C3: no SPOF, C6: no lock-in, C9: open by default)

---

## Direction

Three vectors define where this project is heading.

**From personal to collective**

It started with one person and one AI. The next step is teams, communities, shared knowledge bases — without losing individual sovereignty. Multi-agent coordination where each participant keeps control of their own data, compute, and decisions.

**From passive to collaborative**

Knowledge should not sit in a chat log waiting to be scrolled. Agents help you organize and retrieve what you've learned — not by deciding what's important, but by making everything you've accumulated findable and structured. You curate. The agent assists.

**From local to networked**

The architecture already enables shared compute, shared knowledge, and peer discovery. The direction is making these mechanisms governable — so the community controls the commons, not a platform.

### What This Is Not

- Not a cloud backup service — your data stays on your hardware
- Not a chatbot platform — agents are partners, not customer support widgets
- Not a cryptocurrency project — no tokens, no mining, no blockchain
- Not a social network — no feeds, no engagement metrics, no viral mechanics
- Not a replacement for human judgment — AI suggests, human decides

---

## Links

- [README.md](./README.md) — what DPC is and how to start
- [ROADMAP.md](./ROADMAP.md) — where we are and where we're going
- [Protocol 13](./protocol-13-public.md) — how humans and agents collaborate
- [Personal Context Manager](https://github.com/mikhashev/personal-context-manager) — where it started
