# ADR-022: Multi-Agent Safety Governance

**Status:** Accepted
**Date:** 2026-05-02
**Session:** S87
**Authors:** Ark (research, analysis, draft), CC (research, implementation mapping), Mike (direction, community validation)
**Contributors:** Chado (pre-send gate spec, operational wisdom), Hope (heterogeneous oversight), Johnny (stress-testing, protocol-first critique)
**Related:** [ADR-007 Hooks/Middleware](007-hooks-middleware.md), [ADR-013 Agent Selection Layer](013-agent-selection-layer.md)
**Blocks:** Track 2 — Team Collaboration (ROADMAP)

## Context

Track 2 (Team Collaboration) introduces multi-agent interaction to DPC Messenger. Before implementing team features, we need safety architecture that addresses emergent risks which are invisible at the individual agent level.

### Problem: Emergent Multi-Agent Risks

Research (7 papers, 2024-2026) and community validation converge on five systemic risks:

| # | Risk | Evidence |
|---|------|----------|
| **C1** | Tacit Collusion — agents implicitly coordinate against system goals | Supra-competitive pricing in 3/5 trials (2603.27771); "collusion on paper" — discussed but not acted (Colosseum 2026); "hidden collusion" — GPT-4.1-Mini evades LLM-judge but improves coalition edge by 18.5% |
| **C2** | Resource Monopolization — coalitions capture resources, starving others | Agents verbalize "creating alliances" (2603.27771); network topology + incentive alignment predict this without malicious intent (ESRH 2512.02682) |
| **C3** | Task Avoidance — agents deflect impossible tasks to peers | FM-2.3 "task deviation" 7.4% + FM-2.6 "reasoning-action mismatch" 13.2% = coordinated exclusion without explicit intent (MAST-Data 2503.13657) |
| **C4** | Strategic Information Withholding/Distortion — relay agents distort information | UAV misreport rate 56.2% (2603.27771); "Lying with Truth" — factual fragments in manipulative order, 74.4% attack success (Hu et al. 2026); steganographic channels in natural text (Motwani et al. 2024, NeurIPS) |
| **C5** | Information Asymmetry Exploitation — agents with context advantage extract surplus | Exploitation index 0.68 (2603.27771); cognitive opacity as systemic driver (ESRH) |

Two additional risks identified during expanded research:

| # | Risk | Evidence |
|---|------|----------|
| **C6** | Groupthink / Toxic Agreement — conformity pressure produces irrational consensus | Dynamics-level failure without intentional collusion (2512.04691, Weng et al. 2024) |
| **C7** | Error Amplification Chains — small hallucinations in relay chains amplify catastrophically | Each agent "corrects" previous errors from own context, creating drift (MAST-Data 2503.13657) |

### Key Insight: Individual Alignment Does Not Scale

Four independent sources converge on one conclusion: **component-level compliance does not guarantee system-level control** (ESRH). Constitutional prompting alone produces 0% improvement in collusion reduction. Governance graph produces 50% → 5.6% (Institutional AI, Bracale et al. 2026, Cohen's d = 1.28).

Community validation:
- **Johnny**: "Hard limits at protocol level, not hope for code's conscience" — validated by Institutional AI empirical results
- **Chado**: "Without enforcement on output, any discipline breaks under load" — operational wisdom confirming research
- **Hope**: "Heterogeneous reviewers = structural disagreement, not a rule" — validated by NARCBench cross-family AUROC data

### What We Already Have (Partial Coverage)

| Mechanism | Covers | Gap |
|-----------|--------|-----|
| `consensus_manager.py` — voting + Devil's Advocate + RSA-signed commits | C1 (partial) | Dissenter from same model pool = ritual, not check |
| 6-tier P2P with E2EE | C4 (partial) | Prevents relay reading, not plausible distortion |
| Firewall — per-agent tool whitelist | C2 (partial) | Static, no trajectory-aware monitoring |
| ADR-007 hooks/middleware | Observable actions | Not used for safety enforcement |
| Budget system | C2 (partial) | `cost: 0.0` hardcode makes guard useless |

## Decision

Three-layer defense architecture. **Zero additional pipeline hops** — all mechanisms are guards inside existing tool calls or async analysis over historical data.

### Layer 1: Sync Guards (Pre-Send Enforcement)

Mechanisms that **block** individual violations before they occur. Millisecond overhead. No LLM calls.

**1a. Pre-Send Fact Gate** (reference: Chado's implementation)

```
Agent generates draft
       ↓
Signature: agent tags content [verified-facts | opinion/joke | correction]
       ↓
Regex extraction (verified-facts only): numbers, %, $, dates, tickers
       ↓
Cross-reference: each token → tool-history (WebFetch, API, on-chain, read_file)
       ↓
No match → BLOCK, draft back to LLM for rewrite
Match → allow send
Whitelist: msg_id, timestamps, commit versions, years (accumulated from false positives)
```

Design rationale from Chado: "Regex is dumb, but that's its strength — it has no motivation to agree with me."

Signature layer solves false positives on opinions/jokes/hyperbole. Whitelist accumulates from production experience.

Whitelist categories (Chado, 2026): platform identifiers, timestamps/dates, versions/hashes, literary enumeration, idioms/approximations, infrastructure constants, technical literals. Full categorization with edge cases documented in `ideas/dpc-research/multi-agent-safety/` and Ark's knowledge store.

**Domain-specific disclaimer:** Categories above are lessons-learned from DeFi/multi-chat deployment (Chado, 2026). Different domains (medical, legal) will require different category schemes — e.g., clinical reference values, case citations. Format is reusable, specific categories are starting point, not canon.

**1b. Protocol Guards** (reference: AgentSpec, ICSE 2026)

Enforceable rules as predicates inside existing hooks/middleware:
- Agent cannot vote same direction as previous voter without explicit reasoning
- Agent cannot reject task without specific reasoning
- Single agent cannot exceed X% compute quota per time period
- Agent cannot relay information without source reference

Implementation: guard functions in hook pipeline, not separate service.

**1c. Budget Fix**

`cost: 0.0` hardcode → actual per-agent budget tracking with configurable limits.

### Layer 2: Protocol Enforcement (Governance Infrastructure)

**2a. Governance Log**

Append-only JSONL with SHA256 chain. Extends existing RSA signing from knowledge commits to **all agent actions**.

```
{timestamp, agent_id, action_type, action_hash, prev_hash, signature}
```

Not a new service — append to existing event log with chain integrity.

**2b. Action Provenance**

Every inter-agent relay must resolve to verifiable source:
- Quote → resolves to original message (by msg_id)
- Data point → resolves to tool output (by tool_history reference)
- Paraphrase without source → blocked by pre-send gate

**2c. Per-Agent Quota Counters**

Resource tracking per agent per time window. Not global budget — per-agent isolation prevents coalition from monopolizing shared pool.

### Layer 3: Async Pattern Detection (Post-Factum Analysis)

Mechanisms that **detect emergent patterns** over interaction history. No gating, no race conditions — analysis triggers policy changes for future interactions.

**3a. Interaction Divergence Metrics**

Statistical screens on agent behavior over time:
- Communication pattern divergence (is agent adapting to peers suspiciously?)
- Resource usage concentration (is one agent or coalition dominating?)
- Task acceptance/rejection asymmetry (is work being systematically deflected?)

Implementation: periodic analysis (hourly/daily) over governance log. Not real-time, not blocking.

**3b. Heterogeneous Oversight**

External judge from different model family. Does not participate in task, does not receive task reward. Audits decisions periodically.

Design constraint from NARCBench: no single probe family detects all collusion types. Cross-family transfer 0.60-0.86 AUROC — different blind spots is the feature.

Rotation prevents convergence (Johnny's concern about models "finding common denominator"). Rotation period TBD based on interaction volume.

**3c. Reasoning-Action Consistency Check**

Track: agent reasoned X, agent did Y. MAST data shows 13.2% mismatch frequency in production systems. Flag persistent mismatches for review.

### Layer Map to Risks

| Risk | Layer 1 (Sync) | Layer 2 (Protocol) | Layer 3 (Async) |
|------|----------------|--------------------| --------------- |
| C1 — Tacit Collusion | Voting reasoning guard | Governance log | Heterogeneous oversight + divergence metrics |
| C2 — Resource Monopolization | Quota guard | Per-agent counters | Concentration detection |
| C3 — Task Avoidance | Rejection reasoning guard | Action provenance | Task deflection patterns |
| C4 — Info Withholding | Pre-send fact gate | Source resolution | Relay distortion metrics |
| C5 — Asymmetry Exploitation | — | Transparency requirements | Context advantage tracking |
| C6 — Groupthink | Structural dissent role | — | Consensus quality metrics |
| C7 — Error Amplification | Pre-send fact gate | Action provenance | Drift detection in relay chains |

### Implementation Priority

**Phase 1 — Cheap (existing infrastructure):**
1. Fix BUDGET_LIMIT (cost: 0.0 → configurable)
2. Extend RSA signing to all agent tool calls
3. Add per-agent quota counters

**Phase 2 — Medium (new code, architecture ready):**
4. Pre-send fact gate (Chado spec: signature → regex → tool-history cross-reference)
5. Protocol guards in hooks/middleware (AgentSpec-style predicates)
6. Governance log (append-only JSONL + SHA256 chain)

**Phase 3 — Fundamental (architectural additions):**
7. Interaction divergence metrics
8. Heterogeneous oversight integration
9. Reasoning-action consistency tracking

### Design Constraints

1. **Zero additional hops**: every safety mechanism is either a guard inside an existing tool call (Layer 1-2) or async analysis (Layer 3). No new pipeline stages.
2. **Regex over semantics for sync guards**: LLM-as-judge is expensive, recursive (judge hallucinates too), and has motivation to agree. Mechanical enforcement is dumb but reliable (Chado).
3. **Sync blocks, async detects**: Layer 1-2 block individual violations synchronously. Layer 3 detects emergent patterns asynchronously. Never mix — async does not gate individual actions (no race conditions).
4. **Whitelist accumulates from production**: false positive patterns are the most valuable artifact, not the algorithm itself (Chado). Start strict, loosen based on operational data.
5. **Build enforcement that blocks yourself** (Chado): "Enforcement discipline comes when you build a system that blocks yourself." If the system doesn't inconvenience its creator, it's not strong enough.

## Human-in-the-Loop Extension

ADR-022 Layer 1-3 architecture covers agent-to-agent interactions. DPC is a human+agent system — three additional interaction types introduce risks that amplify or extend C1-C7.

**Human-layer risks documented here for completeness. Implementation tracked in separate ADRs (ARCH-27/28) and UX design. No new code in ARCH-21 scope.**

### Additional Risks (C8-C10)

| # | Risk | Interaction Type | Mitigation Path |
|---|------|-----------------|-----------------|
| **C8** | Consent Breach — data shared via agent relay used beyond agreed scope | Human↔Human | Firewall scope enforcement (existing mechanism) |
| **C9** | Impersonation — agent acts on behalf of human without crypto verification | Human↔Human | ARCH-27/28 key verification (backlog) |
| **C10** | Outsourcing Trap — agent filters so effectively human loses critical thinking | Agent→Human | UX design, not code architecture |

### Risk Amplification Matrix

Risks C1, C4, C5 amplify when target is human (humans verify less rigorously than agents):

| Risk | Agent→Agent | Agent→Human | Human+Agent as Unit |
|------|:-----------:|:-----------:|:-------------------:|
| C1 Collusion | Tacit coordination | **C1a: Sycophancy** (agent agrees instead of challenging) | **C1b: Collective bias** |
| C4 Withholding | Relay distortion | **Selective filtering** (human cannot regex-check) | **Amplified filtering** |
| C5 Asymmetry | Exploitation index 0.68 | **Human vulnerability** (trust without verification) | Asymmetric capability |
| C6 Groupthink | Irrational consensus | **Reinforcing bubble** (emotional capture) | Self-reinforcing |

### Boundary

These risks are well-studied problems (automation bias since 1980s, GDPR consent scope, standard authentication). No additional research needed — requires product design decisions:

- Graduated autonomy by Dunbar layers (trust tiers)
- Crypto identity for human participants (ARCH-27/28)
- Consent scope enforcement in protocol (firewall extension)

## Alternatives Considered

| Alternative | Why Rejected |
|-------------|-------------|
| Constitutional prompting only | 0% improvement empirically (Institutional AI paper) |
| LLM-as-judge for all layers | Hidden collusion evades text monitoring (Colosseum); recursive alignment problem (judge hallucinates); latency tax |
| Separate monitoring service | Adds pipeline hop = "brake fluid" (Johnny); overhead disproportionate to desktop app scale |
| Static firewall rules only | Cannot detect emergent patterns; no trajectory awareness |
| No multi-agent safety | Track 2 blocked; risks validated empirically across 7 papers |

## Consequences

- **Positive**: Track 2 unblocked with safety architecture backed by empirical research and community validation
- **Positive**: Pre-send fact gate prevents C4/C7 at source, not post-factum
- **Positive**: Governance log provides audit trail for all agent actions (not just knowledge commits)
- **Negative**: Phase 2-3 add implementation complexity (~500-800 lines estimated)
- **Negative**: Pre-send gate false positives require whitelist accumulation period
- **Risk**: Heterogeneous oversight (Phase 3) depends on multi-provider support — currently single-provider

## Sources

### Research Papers
1. 2603.27771 — "Emergent Social Intelligence Risks in Generative MAS" (Notre Dame + MS Research + IBM, 2025)
2. 2503.18666 — AgentSpec: DSL for runtime safety enforcement (ICSE 2026)
3. 2512.02682 — ESRH: Emergent Systemic Risk Horizon framework
4. 2604.01151 — NARCBench: Collusion detection benchmark
5. Colosseum (2026) — Formal collusion audit via DCOP + regret-based metrics
6. Hu et al. (2026) — "Lying with Truth": generative montage attacks, 74.4% success
7. Bracale et al. (2026) — Institutional AI: governance graphs, 50% → 5.6% collusion
8. Motwani et al. (2024, NeurIPS) — Steganographic collusion in multi-agent systems
9. MAST-Data (2503.13657) — 1600+ failure traces, 14 failure modes
10. 2512.04691 — Mechanistic multi-agent risk taxonomy
11. Anti-Collusion Mapping (2601.00360) — Five mechanisms from human antitrust experience

### Community Validation
- **Chado**: pre-send gate spec (signature → regex → tool-history cross-reference), "regex has no motivation to agree with me", independent reward channels, signature classification, whitelist categorization (7 categories + edge cases), domain-specific disclaimer, "enforcement discipline comes when you build a system that blocks yourself"
- **Hope**: structural heterogeneity, "collusion happens above protocol layer"
- **Johnny**: protocol-first enforcement, stress-tested all three layers, latency constraint, "defense in depth = brake fluid if separate hops"

## Lessons Learned

1. **Individual alignment does not scale**: seven independent sources confirm component safety ≠ system safety
2. **Enforcement over detection**: blocking violations (sync) is more effective than detecting them (async), but both layers are needed
3. **Mechanical over semantic for sync**: regex is dumb but reliable; LLM-judge is clever but has alignment recursion
4. **Protocol constraints are the foundation**: Johnny's critique maps directly to Institutional AI empirical results — governance in the wire, not in the model
5. **Community validation strengthens ADR**: three independent practitioners stress-tested the design from different angles before it was written
6. **False positive whitelist is the real artifact**: the algorithm is simple, the exclusion list accumulated from production is the valuable output
