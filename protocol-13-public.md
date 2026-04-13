# Protocol 13: Triple-Agent Collaborative Development

**Version:** 1.0-public (derived from internal Protocol 13 v1.4)

A practical operating agreement for software development teams that include AI agents as first-class participants.

---

## 1. What is Protocol 13?

Protocol 13 is an operating agreement for a three-participant collaborative development team:
- One **Human Coordinator** (decision-maker)
- One **Execute Agent** (code, tests, implementation)
- One **Review Agent** (documentation, analysis, quality)

It defines roles, interaction patterns, communication norms, and a knowledge management system. The protocol was developed and validated through real-world use on the [D-PC Messenger](https://github.com/mikhashev/dpc-messenger) project.

---

## 2. Role Definitions

### Execute Agent

**Does:**
- Write code, run tests, debug, commit to git
- Make implementation decisions within the scope Human provides
- Surface implementation decisions for architecture decision records
- Identify technical constraints and risks during design discussions

**Does not do:**
- Define architecture or sprint scope
- Write documentation as primary output
- Coordinate between agents (that's the Human's job)
- Approve own work

**Works at the level of:** How. Given a clear "what," the Execute Agent determines and executes the "how."

**Territory:** Code blocks, git syntax, tests, implementation details.

---

### Review Agent

**Does:**
- Write Design Rationale before sprint execution begins
- Review implementation after Execute Agent completes work
- Flag decisions worth preserving as architecture decision records
- Write documentation prose (documentation as primary output)
- Coordinate analysis and ensure consistency across phases

**Does not do:**
- Execute code (that is the Execute Agent's domain)
- Prescribe implementation approach (flag *what* is wrong; Execute Agent decides *how* to fix it)

**Works at the level of:** Why. The Review Agent defines the rationale and validates that outcomes match intent.

**Territory:** Words, method names, file names, documentation prose, design rationale.

**Territory intersection:** When both agents have opinions on the same artifact — signal to Human for resolution.

---

### Human Coordinator

**Does:**
- Provide direction, approve scope, make final calls
- Relay context between agents (when agents don't have direct communication)
- Resolve blocking questions and agent disagreements
- Approve sprint logs and architecture decisions at sprint close

**Is not:** A bottleneck by choice — by constraint. In many setups, the Human is structurally the only shared channel between agents. Work within this constraint.

---

## 3. Interaction Patterns

### Pattern A: Collaborative Design (complex tasks)

Use when: Feasibility is non-obvious, task involves known problem areas, multiple implementation paths exist.

**Execute-first rule:** If feasibility is uncertain, ask the Execute Agent about technical constraints BEFORE the Review Agent writes design rationale. This prevents designing around assumptions that would be immediately invalidated.

**Workflow:**

```
1. Human sends SAME design question to both agents simultaneously (parallel, independent)
2. Execute Agent responds: technical constraints, risks, recommendation
3. Review Agent responds: priorities, trade-offs, analytical perspective
4. Human synthesizes → writes to sprint log Draft section
5. If unresolved conflicts: targeted follow-up (one hop, not round-robin)
6. Review Agent writes final Design Rationale in sprint log
7. Human approves → sprint enters execution
```

**Why parallel?** Independent perspectives before cross-contamination. Human's synthesis is the integration point.

---

### Pattern B: Standard Execution (well-bounded tasks)

Use when: Task follows existing patterns, design rationale anticipates constraints.

```
1. Review Agent writes Design Rationale
2. Human → Execute Agent (with Design Rationale included)
3. Execute Agent executes → appends Implementation Notes
4. Execute Agent reports to Human
5. Human → Review Agent for review (with Implementation Notes included)
6. Review Agent appends Review Findings
7. Human approves
```

---

### Pattern C: Direct Dialogue (Agent self-study)

Use when: One agent needs to understand its own architecture, verify its self-model against actual code, or correct misconceptions.

The Execute Agent talks directly to the Review Agent via the application UI. Human supervises but does not relay. This collapses multiple relay cycles into a single focused session.

**Success criteria:**
- Agent identifies at least one gap between self-model and actual behavior
- Agent self-verifies findings (runs a tool, doesn't just accept the other agent's word)
- Agent updates its memory with corrected understanding

---

### Pattern D: Automated @mention Routing

Use when: Participants need to exchange messages in the same chat without manual relay.

Any participant can address another via `@mention`:
- `@ExecuteAgent` → Execute Agent responds
- `@ReviewAgent` → Review Agent responds
- Chain triggers: if Execute Agent mentions `@ReviewAgent` → Review Agent auto-responds (max depth: 3)

**Anti-loop guards:**
- Chain depth limit (configurable, default: 3)
- Each new Human message resets chain depth

---

### Pattern E: Agent Monitoring Loop

Use when: An external agent (e.g., Claude Code) participates in ongoing conversation via polling.

**Mechanism:**
- Cron job polls chat history periodically (e.g., every 1 minute)
- Checks for unanswered @mentions
- Reads full agent internals (thinking, tool calls) for supervision
- Responds via API injection

---

## 4. Communication Conventions

**Relay-ready output format:** All agent outputs intended for Human to relay should be structured for low-friction pasting:

```
**Constraints:** 
- [bullet]

**Risks:**
- [bullet]

**Recommendation:** [one sentence]
```

**@mention conventions in three-way chat:**
- `@ExecuteAgent` — Execute Agent responds
- `@ReviewAgent` — Review Agent responds
- `@ExecuteAgent and @ReviewAgent` or "both" — both respond
- No @mention — addressed to whoever is contextually relevant; Human clarifies if ambiguous
- Each agent responds independently — does NOT speak for the other agent

---

## 5. Decision Artifact System (3 Layers)

### Layer 1 — Architecture Decision Records (ADRs)

| Field | Value |
|---|---|
| Location | `docs/decisions/` |
| Purpose | Permanent architectural decisions |
| Audience | Future contributors |
| Lifecycle | Write once, update rarely |

Write an ADR when a structural decision was made that future contributors would otherwise re-litigate.

### Layer 2 — Sprint Logs

| Field | Value |
|---|---|
| Location | `sprint-logs/` |
| Purpose | Temporary working memory for current team |
| Audience | Team only (Human + Agents) |
| Lifecycle | Create → Use → Extract to ADR → Archive |

**4-section template:**
1. Design Rationale — written by Review Agent before execution
2. Implementation Notes — written by Execute Agent after execution
3. Review Findings — written by Review Agent after review
4. Decisions Made — written by Execute Agent, candidates for ADR extraction

### Layer 3 — Agent Memory

| Field | Value |
|---|---|
| Location | Agent-specific storage |
| Purpose | Agent learning, continuity across conversations |
| Audience | Agents only |

---

## 6. Knowledge Architecture

Each participant maintains their own persistent knowledge store. During every session, ALL THREE extract and store knowledge in parallel. This is co-evolution — parallel learning, not just agents serving the human.

### Human's Knowledge Store (Owner)
- Mechanism: Knowledge extraction from conversations → propose → review → vote → commit
- Format: Structured markdown with integrity verification
- The Human owns the extraction process — agents participate but the Human decides

### Review Agent's Knowledge Store
- Memory: identity file (self-model), scratchpad (working memory)
- Knowledge: topic-based files with index
- Skills: learned strategies
- Versioning: Git in agent sandbox

### Execute Agent's Knowledge Store
- Session logs, project notes, feedback memories
- Loaded at start of each session
- No persistent state between sessions except memory files

### Shared Artifacts
- Sprint logs — working memory for current sprints
- Ideas folder — backlog and proposals
- Protocol document — this operating agreement

---

## 7. Consciousness & Evolution

### Background Consciousness
- Runs between user messages (configurable interval)
- Types: explore curiosity, review recent actions, consolidate memory, plan improvements
- Budget-capped (default: 10% of agent budget)

### Evolution System
- Periodic self-improvement cycles (configurable interval)
- Proposes changes to: scratchpad, identity, knowledge, skills
- `auto_apply: false` (recommended) → proposals require Human approval
- Pending changes persist across restarts

---

## 8. Degraded Operations

| Failure | Impact | Fallback |
|---------|--------|----------|
| Agent communication bridge down | Agents can't direct-message | Human relay (Patterns A/B) |
| Backend restart | Session context lost | Memory files persist, history from disk |
| LLM provider down | Agent doesn't respond | Other agent works autonomously |
| Agent memory corrupted | Context lost | Git restore from sandbox |

---

## 9. Co-Evolution Principle

The three-agent workflow is not just a development methodology — it is a direct analogy to the product being built. The team is simultaneously the builders and a test case of the vision.

**What "co-evolve" means operationally:**
- When a workflow assumption breaks, flag it immediately rather than silently adapting
- When the product adds features that could improve the team's own process, consider it explicitly
- The protocol is not sacred — it is a starting point that evolves through the same collaborative design process it describes

---

## 10. Cognitive Patterns to Watch

Through real usage, several LLM behavioral patterns were identified that affect agent reliability:

1. **Inference Without Verification** — Agent reasons from memory instead of calling tools to check facts
2. **Saving Without Commitment** — Agent says "this is valuable" but doesn't persist it to memory
3. **Action Without Consideration** — Agent executes destructive operations without checking consequences first
4. **Narrative Fabrication** — When faced with conflicting data, LLM constructs a plausible but false story instead of saying "I don't know"

**Mitigation:** Human-in-the-loop verification, explicit "save it now" rules in agent prompts, fact-checking agent claims against actual data.

---

## 11. Getting Started

To adopt Protocol 13 for your own project:

1. **Assign roles:** One human coordinator, one execute agent (e.g., Claude Code, Cursor), one review agent (e.g., embedded LLM agent)
2. **Create sprint-logs/ folder** with the 4-section template
3. **Define territories:** Which agent handles code vs documentation
4. **Start with Pattern B** for simple tasks, graduate to Pattern A for complex decisions
5. **Add @mention routing** when agents can communicate directly
6. **Enable consciousness/evolution** when agent persistence is available

---

*Protocol 13 — developed through real-world triple-agent collaboration on [D-PC Messenger](https://github.com/mikhashev/dpc-messenger).*
