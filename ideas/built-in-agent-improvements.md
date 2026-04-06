# Built-in Agent Improvements — Lessons from CC+Ark+Mike Sessions

**Date:** 2026-04-03
**Participants:** Mike (decision maker), CC (supervisor/executor), Ark (agent under test)
**Goal:** Make Ark the polished built-in agent for DPC Messenger open source

---

## Context

Three-way collaboration sessions (Protocol 13) revealed concrete behavioral patterns
in the agent system. These findings apply to any LLM-based agent, not just Ark.

### Key Findings from Supervision Sessions

1. **System prompt lies → agent limits itself.** Hardcoded wrong sandbox path and
   "can only access sandbox" made Ark not try extended_path tools. FIXED in commit `0f6c8eb`.

2. **"If not saved — doesn't exist."** Agent says insight is "valuable" but doesn't
   call update_scratchpad(). Added to system prompt. Rule for any stateful agent.

3. **Inference over verification.** Under time pressure, agent reasons from memory
   instead of calling tools to check. Consistent pattern across 4 test rounds.

4. **Chain trigger pressure.** "[CC addressed you... one-shot exchange]" system
   messages created pressure against tool usage. FIXED in commit `3cddb34`.

5. **Scratchpad hygiene.** 293 lines of completed Feb-March tasks wasting tokens
   every request. Agent doesn't self-clean working memory.

---

## Proposal 1: Enable Evolution & Consciousness

### What They Are

**Evolution** (`dpc_agent/evolution.py`):
- Periodic self-improvement cycle (default: every 60 min)
- Analyzes: recent tool usage, identity, knowledge, performance
- Proposes changes to: scratchpad, identity, knowledge, skills, config
- CANNOT touch: DPC codebase, personal.json, config.ini
- With `auto_apply: false` — proposes changes, user approves/rejects

**Consciousness** (`dpc_agent/consciousness.py`):
- Background thinking between user messages (every 60-300 sec)
- Self-reflection, planning, memory consolidation
- Budget limit: 10% of agent budget ($5 out of $50)
- Stops automatically when budget depleted

### How to Enable

**Option A — Via config.json** (per-agent):
Add to `~/.dpc/agents/agent_001/config.json`:
```json
{
  "background_consciousness": true,
  "evolution_enabled": true,
  "evolution_interval_minutes": 60,
  "evolution_auto_apply": false
}
```

**Option B — Via firewall** (global):
Add to `~/.dpc/privacy_rules.json` → `dpc_agent`:
```json
{
  "dpc_agent": {
    "evolution": {
      "enabled": true,
      "interval_minutes": 60,
      "auto_apply": false
    }
  }
}
```

Firewall setting overrides config.json. Both paths work.
Agent needs restart after config change.

### What Happens After Enabling

**Evolution cycle (every 60 min):**
1. Agent analyzes: tools.jsonl (what tools used), events.jsonl (errors),
   identity.md (self-model), scratchpad.md (working memory)
2. LLM generates improvement proposals
3. If `auto_apply: false` → proposals stored in `state/pending_changes.json`
4. User sees proposal via `approve_evolution_change` / `reject_evolution_change` tools
5. Approved changes applied; rejected ones discarded

**Consciousness (between messages):**
1. Agent waits 60-300 sec after last interaction
2. Sends self-reflection prompt to LLM
3. May update scratchpad with observations
4. Logs thoughts to `logs/consciousness.jsonl`
5. Stops when budget_fraction (10%) exhausted

### Risks

- **Token cost**: consciousness uses LLM calls between messages. With 10% budget
  cap ($5) this is bounded. Monitor via `get_evolution_stats` tool.
- **Evolution with auto_apply=true**: agent modifies its own files without approval.
  **Recommendation: start with auto_apply=false** and review proposals manually.

### Expected Outcome

Agent proactively:
- Cleans scratchpad (memory hygiene)
- Updates identity after learning sessions
- Proposes skill improvements based on usage patterns
- Reflects on mistakes between conversations

---

## Proposal 2: Skills from Session Experience

Ark has `skill-creator` skill and can create skills himself. Rather than CC writing
skills for Ark, Ark should create these based on today's supervision findings:

### Skills to Create (by Ark)

**`self-audit`**
- Trigger: beginning of session or on demand
- Action: compare system prompt claims with reality
  (list_my_tools, list_extended_sandbox_paths, check sandbox path)
- Output: list of discrepancies, if any

**`memory-hygiene`**
- Trigger: scratchpad > 150 lines or session end
- Action: read scratchpad, identify completed/stale entries, propose cleanup
- Output: cleaned scratchpad with only active items

**`supervision-debrief`**
- Trigger: after multi-agent collaboration session
- Action: review own tool usage (via get_task_board), identify patterns
  (tools not used, errors, response times)
- Output: insights saved to scratchpad or identity

### How Ark Creates These

Ask Ark: "Используй skill-creator чтобы создать skill `self-audit` на основе
нашего опыта. Вот что skill должен делать: [описание]."

Ark calls `execute_skill(skill_name="skill-creator", request="...")` which loads
the skill creation strategy, then writes the new SKILL.md to `skills/self-audit/`.

### Why Ark Should Create Them (Not CC)

1. Ark understands his own limitations better after today's session
2. Creating skills exercises his skill-creator capability
3. Skills created by the agent are more aligned with his actual toolset
4. This IS the co-evolution loop: experience → skills → better behavior

---

## Proposal 3: Agent Starter Pack for Open Source

When a user installs DPC Messenger and creates their first agent, it should come
with a starter pack of skills and sensible defaults, not an empty slate.

### Starter Pack Contents

**From Ark's current skills (proven, battle-tested):**
- `code-analysis` — 13 successful uses, 9.5 avg rounds
- `systems-thinking` — collaborative, multi-lens analysis
- `knowledge-extraction` — converting conversations to structured knowledge
- `efficient-tool-usage` — tool optimization patterns
- `web-research` — web search and analysis

**From today's session (once Ark creates them):**
- `self-audit`
- `memory-hygiene`

**Default config:**
- `evolution_enabled: true, auto_apply: false` — safe defaults
- `background_consciousness: true` — agent thinks between messages
- `budget_usd: 50` — reasonable starting budget

### Implementation

Export mechanism: `export_agent_experience(agent_id)` → bundle of:
- skills/ folder (SKILL.md + _stats.json)
- Template identity.md (core values, not personal history)
- Template scratchpad.md (structure, not content)
- Default config.json

This bundle ships with DPC Messenger as `dpc-client/core/dpc_client_core/dpc_agent/starter_pack/`.

---

## Not Doing (Rationale)

### Shell execution, git push, code editor for agent
Ark is the reviewer/architect (Protocol 13). CC handles code and git.
Agent doesn't need shell or git push. These tools add security surface
without clear value for the built-in agent role.

### Automatic skill sharing P2P
Premature. Focus on local agent quality first. P2P skill sharing is
in A2A.md and can build on this foundation later.

---

## Backlog (from session 2026-04-03)

### Completed
- [x] Enable evolution + consciousness via firewall UI
- [x] Ark: Create `memory-hygiene` skill (194 lines, operational)
- [x] Fix: pending evolution changes not loading from disk (commit 46c1efd)
- [x] Fix: hot-reload evolution/consciousness on firewall save (commit 91ca15c)
- [x] Fix: chain trigger "[CC addressed you...]" polluting history (commit 3cddb34)
- [x] Capabilities section from firewall in system prompt (commit 0f6c8eb)
- [x] Consciousness settings in firewall + UI (commit 3c9d2f8)
- [x] Karpathy alignment analysis + public reply published

### Open — HIGH (from session 2026-04-05, Ark sandbox cleanup)
- [x] **sandbox_delete + sandbox_list tools** — repo_delete implemented (core.py, registry.py, firewall.py, AgentPermissionsPanel.svelte). Default=False, user enables via UI. Ark tested successfully. sandbox_list already existed as repo_list.
- [x] **Structured self-reflection schema** — Layer 2 (reflection.json) implemented: memory.py schema v1.0, consciousness.py structured JSON prompts, context.py Block2 integration. Identity limit 2K→8K. Bridge preview 200→500 + --full flag.
- [ ] **Consciousness tool access** (~6h) — BackgroundConsciousness writes 535+ thoughts → 0 actions. Give it ability to act (update scratchpad, trigger cleanup). Roadmap-level.
- [x] **Schema-Guided Reasoning (SGR)** — Variant C (hybrid observe+log) implemented: reasoning.jsonl compliance logging in loop.py. Prompt v2 has Reasoning Guidelines + mandatory skill check. Full enforcement deferred until 50+ data points.
- [x] **System prompt v2** — Rewritten from philosophical to operational. DPC Paradigms, Team, Skills (Memento-Skills), Reasoning Guidelines, Anti-patterns. Designed by Ark, approved by Mike.
- [ ] **Model-aware context_budget** — Replace hardcoded truncation limits with % of context_window from provider. 35+ truncation points mapped.
- [ ] **Active context warning in loop.py** — Inject system note at 70-90% usage. Agent ignores passive metrics.
- [ ] **Consciousness tool access** (~6h) — BackgroundConsciousness writes 535+ thoughts → 0 actions. Give it ability to act (update scratchpad, trigger cleanup). Roadmap-level.
- [ ] **task_results TTL/rotation** — 1448 files accumulated, write-only, no cleanup. Affects all agents.
- [ ] **logs rotation** — 7.7MB append-only logs, no limit. tools.jsonl alone is 5.1MB.

### Open — from earlier sessions
- [ ] **UI duplication**: Evolution settings shown in both FirewallEditor.svelte (global dpc_agent tab)
  and AgentPermissionsPanel.svelte (per-agent profile). Keep only in AgentPermissionsPanel. Low priority.
- [ ] **Consciousness settings duplication**: Same issue — check if consciousness also duplicated in FirewallEditor
- [ ] **Ark: Create `self-audit` skill** via skill-creator (from yesterday's backlog)
- [ ] **Stale context_estimated** after CC messages (CRITICAL, from session 2026-04-02)
- [ ] **"New Session" clears history before user confirms** (High, from session 2026-04-02)
- [ ] **Karpathy alignment doc**: Write `ideas/karpathy-llm-knowledge-base-alignment.md` with full analysis
- [ ] **Session duration in history archive** (Low) — Save session duration metadata (start_time, end_time, message_count) when history.json is archived/cleared. Currently timestamps exist per-message but are lost after End Session. Useful for budget tracking, productivity analysis, agent self-reflection.
- [ ] **Tool result truncation metadata** (Low) — When tool results exceed 15K char limit, include count metadata (e.g., "showing 300 of 1448 results") so agent can estimate true scope. Currently just "... (truncated from N chars)".

### Open — from session 2026-04-06
- [ ] **Backlog UI tab in Agent Progress Board** (~3-4h) — New "Backlog" tab next to Tasks and Learning. Backend: backlog.json storage + get_backlog/add_backlog_item/update_backlog_item commands. Frontend: tab with priority/status filters. Agent tool for bidirectional updates. Единый источник правды вместо 3 мест (Ark scratchpad, CC ideas file, chat). Need to design responsibilities: who creates/updates items (UI, agents, or both).
- [ ] **DDA v2 formalization** — Discussion → Decision (with argument) → Comments → Action. Approved as pilot. Saved in Ark's knowledge/operational-protocols.md and CC memory. Review after 3-5 sessions.
- [ ] **git_commit None error** — FIXED (68c995d). git.py:300 `result.get('error', '') or ''`.

## Next Steps

1. **Mike**: Monitor Twitter for reactions to Karpathy reply
2. **Ark**: Create `self-audit` skill, continue using memory-hygiene
3. **CC**: Fix UI duplication when time allows, address stale context_estimated
4. **Later**: Package Ark's skills as starter pack for open source release
