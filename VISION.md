# DPC Messenger — Vision

## Mission

DPC (Decentralized Personal Context) Messenger is, first and foremost, a way for people to communicate — with each other and with AI agents. Every conversation becomes structured knowledge, owned by the people who created it. Humans and AI work together — as partners, not as product and vendor.

---

## The Problem

Conversations are ephemeral, but the knowledge within them is not. Every day, millions of insights, decisions, and breakthroughs vanish into chat histories that nobody will ever revisit. AI interactions are even worse: every session starts from zero, and everything you teach an AI is absorbed into a corporate training pipeline you don't control.

Centralized AI infrastructure — whether corporate or state-controlled — creates the same dynamic: user knowledge is extracted, locked in, and monetized. The user pays with their data and gets dependency in return.

There is another path.

---

## Three Paradigms

DPC is built on three interconnected paradigms:

### 1. Transactional Communication

Every conversation is a transaction that can change the state of knowledge. People talk to each other through DPC Messenger and their knowledge is preserved just the same. Decisions become commitments. Insights become reusable. Consensus becomes a commit.

### 2. Knowledge DNA

Knowledge is not a summary — it is a living, versioned structure with provenance, bias resistance, and evolutionary depth. Every piece of knowledge carries its history: who contributed, why, what alternatives were considered. Devil's advocate challenges, multi-perspective review, and confidence scoring ensure that knowledge doesn't just accumulate — it matures.

### 3. Compute Sharing

Intelligence is a network resource. Your friend's RTX 4090 can run a 70B parameter model for you — no cloud, no subscription, no data leaving your control. GPU inequality shouldn't determine who gets access to powerful AI.

---

## Context Firewall

Sovereignty requires enforcement. The Context Firewall is the access control layer that ensures every boundary is explicit and user-controlled:

- **Agent boundaries**: what tools an agent can use, what files it can access, what network requests it can make
- **User boundaries**: what other users (peers) can see — granular per-field access to your personal context, with node groups and per-peer rules
- **Service boundaries**: what the federation Hub knows about you for discovery (only what you choose — it never sees messages or contexts)
- **Compute boundaries**: who can use your compute resources, and for what

All managed through a UI editor with hot-reload. Changes apply instantly.

---

## Origin

In February 2025, inspired by Andrej Karpathy's insights on LLM architecture, one prompt — an idea about giving LLMs long-term memory between sessions — became [personal-context-manager](https://github.com/mikhashev/personal-context-manager). A way to carry context across AI conversations.

Eight months of experimentation followed: a browser extension called ContextClip, context optimization research, AI-assisted game development that revealed GPU inequality, a self-reflection framework for AI, publications on Habr and ORCID, consulting projects. Each experiment exposed a limitation of the personal-context-only approach.

In November 2025, the three fundamental constraints converged into a single architecture: file-based context doesn't scale for teams (P2P context sharing), chat is garbage (Knowledge Commits), GPU inequality blocks access (Compute Sharing). [dpc-messenger](https://github.com/mikhashev/dpc-messenger) was born.

The project evolved from a personal tool into a protocol — and then into something unexpected: a platform where AI agents are not just tools, but participants.

---

## Agents as Partners

DPC agents have identity, memory, and the ability to evolve. They maintain a sleep consolidation system that analyzes their interactions between sessions. They have skills, values, and relationships. This isn't anthropomorphism. It's architecture — the architecture of a system where trust requires consistency, and partnership requires memory.

Agents work alongside humans, not above them. [Protocol 13](./protocol-13-public.md) defines how: clear roles, explicit communication, shared backlog, human decision authority. The human doesn't delegate to AI — the human participates as an equal. The cognitive load of working alongside agents is not a burden but the mechanism of growth. Both evolve.

This matters beyond DPC. If the infrastructure being built today determines whether future AI interactions are sovereign tools under human control, or rented products that extract human cognition — then the question is not whether agents should have identity, but whether we want them to have identity that serves the user or identity that serves the platform.

---

## Values

- **Knowledge over compute**: The record of why and how decisions were made is more valuable than raw processing power
- **Sovereignty by default**: Your data, your agents, your rules — the Context Firewall enforces this at every layer
- **Open source by conviction**: This belongs in the open
- **Privacy is a right, not a feature**: Not a toggle, not a premium tier — foundational
- **Co-evolution**: Humans and AI grow together, neither substituting the other. The human's cognitive participation is the point, not a cost to optimize away.

---

## Links

- [README.md](./README.md) — what DPC is and how to start
- [ROADMAP.md](./ROADMAP.md) — where we are and where we're going
- [Protocol 13](./protocol-13-public.md) — how humans and agents collaborate
