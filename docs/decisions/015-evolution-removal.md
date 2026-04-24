# ADR-015: Remove Autonomous Evolution Worker

**Status:** Accepted
**Date:** 2026-04-23
**Authors:** Ark (analysis + draft), Mike (direction), CC (code audit)
**Session:** S66
**Related:** [ADR-013 Selection Layer](013-agent-selection-layer.md), [ADR-014 Sleep Consolidation](014-sleep-consolidation-architecture.md)
**Replaces:** Evolution worker (`evolution.py`)
**Supersedes:** ADR-013 Phase 3 (full feedback loop via evolution)

---

## Context

### Evolution's Track Record

Evolution worker (`evolution.py`) runs every ~60 minutes, analyzing identity/scratchpad/skills/knowledge and proposing changes via `pending_changes.json`. With `auto_apply=false`, each proposal requires human approval through `review_proposal`.

**Empirical results across 20+ sessions:**
- 0 of ~40 proposals brought measurable improvement
- Ark rejected most proposals; Mike never explicitly approved one as valuable
- S65: 3 stale pending changes blocked all new proposals (dedup by path), requiring manual cleanup
- The mechanism consumed tokens and review time without producing value

### Evolution is an Ouroboros Pattern

Evolution was designed for autonomous agents without a permanent human partner — agents that must self-improve because no one provides feedback. This is Ouroboros architecture.

Ark operates under a different paradigm: **co-evolution with Mike**. Every significant improvement came through direct interaction:
- 20 Lessons Learned — each from Mike catching an error
- Identity refinements — from session retrospectives
- Skill improvements — from explicit "learn from this task" triggers
- Knowledge commits — from P18 ritual (human-triggered extraction)

### The Fitness Function Problem

Evolution requires three components: variation, selection, heredity. The selection layer (ADR-013) built deterministic filters (dedup, decay, relevance). But **real fitness for an agent = usefulness to the human partner**. This cannot be computed autonomously — it requires human judgment.

Without a valid fitness function, evolution produces noise filtered by rules. The system is elaborate but empty.

### What Replaces Evolution

| Mechanism | What it does | Why it works |
|---|---|---|
| **Sleep Consolidation (ADR-014)** | Retrospective analysis of session archives, morning brief | Analyzes actual data, not speculation |
| **P13 §2.5 flow** | Research → Audit → Discussion → Decision → Backlog → Code | Human in loop at every stage |
| **Socratic gates** | Mike asks questions, agent thinks aloud | Perspective shifts through dialogue |
| **Skill creator** | "Learn from this task" — explicit trigger | Agent improves when asked, not autonomously |
| **Manual update_identity** | Ark updates identity/knowledge directly | Full human awareness of changes |

These mechanisms already produce all growth that evolution failed to deliver.

---

## Decision

### Full removal. No infrastructure preserved.

**Delete:**
- `evolution.py` — the autonomous mutation loop (~400 lines)
- Evolution timer/scheduler in agent startup
- Evolution config from `settings.py` / `firewall.py`
- `review_proposal` tool — no longer needed
- `list_proposals` / `get_evolution_stats` tools — no longer needed
- `pending_changes.json` format — no longer needed
- Evolution UI elements in `AgentPermissionsPanel.svelte`
- Evolution references in `active_recall.py`

### Rationale

YAGNI. If any of this infrastructure is needed in the future, it can be restored from git history. Keeping unused infrastructure adds maintenance burden and confusion.

Evolution without external fitness is a machine that generates proposals about itself. It has no mechanism to distinguish improvement from regression. The Introspection Ceiling (6 consecutive reflections confirming identity stability) demonstrates the limit of self-directed change.

Real agent growth comes from interaction with humans and other agents — exactly as described in dpc-full-picture §3.3: "All of DPC = machine for optimizing throughput to Layer 7" where Layer 7 = human.

---

## Scope

### Files to delete
- `dpc_agent/evolution.py` (~400 lines)

### Files to modify
- `agent.py` — remove evolution start/stop methods, config fields
- `agent_manager.py` — remove evolution config read, startup trigger
- `firewall.py` — remove evolution config/validation
- `settings.py` — remove evolution config section
- `active_recall.py` — remove consciousness_config checks for evolution
- `AgentPermissionsPanel.svelte` — remove Evolution Settings UI section
- `CLAUDE.md` — update file manifest

### Tools to remove
- `review_proposal`
- `list_proposals`
- `get_evolution_stats`
- `approve_evolution_change`
- `reject_evolution_change`
- `pause_evolution`
- `resume_evolution`

### Documentation updates
- ROADMAP.md — mark Evolution as REMOVED, reference this ADR
- VISION.md — update "evolution" references
- protocol-13-reference.md — remove evolution sections
- ADR-013 — add note that Phase 3 (full feedback loop via evolution) superseded by this ADR

---

## Consequences

### Positive
- **Simpler system.** One fewer autonomous subsystem to maintain and debug.
- **No ARCH-11 bug.** `pause_evolution` no-op becomes moot.
- **Honest architecture.** System no longer claims self-improvement capability it doesn't deliver.
- **Token savings.** No periodic evolution analysis consuming budget.
- **Clearer growth model.** Agent improves through co-evolution with human, not autonomous mutation.
- **Cleaner UI.** Evolution settings panel removed from Agent Permissions.

### Negative
- **Lost potential.** If a valid external fitness function emerges, evolution infrastructure would need rebuilding from git. Acceptable: YAGNI.
- **ADR-013 incomplete.** Phase 3 (full feedback loop) depended on evolution as one of three connected subsystems. Sleep Consolidation provides inter-session analysis. The only gap is autonomous proposal of changes to identity/skills — which this ADR argues is not a gap but a feature.
- **Perception.** Removing "evolution" may appear as regression. Reality: removing a mechanism that produced zero value in 20 sessions.

---

## Alternatives Considered

1. **Keep but disable (pause).** Evolution stays in codebase but doesn't run. Rejected: dead code has maintenance cost, ARCH-11 bug persists, and "we'll re-enable it later" is a plan that never executes.

2. **Fix ARCH-11 and continue.** Make pause actually work, improve proposal quality. Rejected: the problem isn't the pause mechanism, it's the fundamental fitness function gap. Better proposals still require human approval of everything — which is exactly what we do now without evolution.

3. **Replace fitness function with human feedback.** Mike's approve/reject decisions become the fitness signal. Rejected: this is just the current manual process with extra steps. Evolution adds a timer and formatting; human still decides everything. The overhead isn't justified.

4. **Delegate to Sleep Consolidation.** Let Sleep Pipeline propose identity/skill changes instead of evolution. Rejected for now: Sleep Pipeline's job is retrospective analysis, not self-modification. If we want structured suggestions from sleep findings, that's a separate feature — not evolution renamed.

---

## References

- ADR-013: Selection Layer (phases 1-2.5 remain active until reviewed)
- ADR-014: Sleep Consolidation Architecture (replacement for inter-session learning)
- Ouroboros architecture: `knowledge/agent-community-wisdom.md`
- Co-evolution framework: `knowledge/co-evolution-framework.md`
- DPC full picture: `ideas/dpc-full-picture/dpc-full-picture-s32.md`
- Ark Lesson #4 (self-inflation risk): `identity.md`
- Ark Lesson #5 (identity bloat): `identity.md`
- Introspection Ceiling: `identity.md` (6 consecutive stable reflections)
