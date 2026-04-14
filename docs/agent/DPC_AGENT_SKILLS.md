# DPC Agent — Memento-Skills System

**v0.20.0+** — Implemented in `dpc_agent/skill_store.py`, `skill_reflection.py`, `tools/skills.py`

This document answers the agent's own questions about how it learns and evolves in the Memento-Skills style. The agent can read this file directly.

---

## What Is This? (Skills vs Tools vs Knowledge)

Three distinct concepts — do not confuse them:

| Concept | What it is | Where stored | Who reads it |
|---------|-----------|--------------|--------------|
| **Tool** | Executable capability (`browse_page`, `git_status`) | Python code in `dpc_agent/tools/` | Runtime — always available |
| **Knowledge** | Facts and insights from conversations | `memory/knowledge/*.md` | Agent loads on demand |
| **Skill** | Markdown strategy: *how to combine tools* for a class of tasks | `skills/{name}/SKILL.md` | Injected into system prompt before each task |

**Skills are procedural knowledge** — not what to know, but how to act.
Example: tool = `search_web`; skill = "when researching a technical topic: search first → read top 3 pages → cross-reference → synthesize in knowledge base."

---

## What Is Implemented (v0.20.0)

### Phase 1 — Skills directory + 5 starter skills ✅
- `~/.dpc/agents/{id}/skills/` created on agent startup
- 5 skills bootstrapped automatically: `skill-creator`, `code-analysis`, `knowledge-extraction`, `p2p-research`, `web-research`
- Each skill is a `SKILL.md` file with YAML frontmatter + markdown instructions

### Phase 2 — Skill router (Read phase) ✅
- Before each task: system prompt includes `## Available Skills` section listing all skill names + descriptions
- Agent calls `execute_skill(skill_name, request)` to load the full strategy
- Strategy is injected as context; agent follows it using existing tools

### Phase 3 — Post-task reflection (Write phase) ✅
- After every task: `record_outcome()` updates `_stats.json` (always, synchronous)
- If task used ≥ 5 LLM rounds: background LLM call assesses whether the skill strategy had gaps
- If improvement identified: appends a `## Lessons Learned` section to `SKILL.md`
- Gated by firewall: `dpc_agent.skills.self_modify` must be `true`

### Phase 4 — Evolution integration ✅
- `evolution.py` now reads `_stats.json` to identify underperforming skills
- Targets skills with failure_rate > 30% OR avg_rounds > 10 (minimum 3 uses)
- Generates targeted improvement proposals instead of generic state analysis
- Skill appends auto-approve even when `evolution.auto_apply = false`

### Phase 5 — Peer skill sharing ⏳ (future sprint)
- P2P skill discovery via `SKILL_SEARCH` / `SKILL_DATA` DPTP messages
- DHT announcement of shareable skills
- Not yet implemented

---

## Skill File Format

Each skill lives at `~/.dpc/agents/{id}/skills/{name}/SKILL.md`:

```yaml
---
name: web-research
version: 1
description: >
  Research a topic online. Use when asked to find information, check current
  facts, or investigate something on the internet. Do NOT use for tasks where
  local knowledge is sufficient.
provenance:
  author_node_id: ""          # set to your node_id for custom skills
  source: bootstrapped        # bootstrapped | local | peer | evolved
metadata:
  execution_mode: knowledge   # knowledge = LLM follows instructions
  required_tools:
    - search_web
    - browse_page
  tags:
    - web
    - research
---

## Strategy

1. Search with `search_web` using specific query terms
2. Read top 3 results with `browse_page`
3. Cross-reference key claims across sources
4. Synthesize and report — cite sources

## When to Use
- User asks to find, research, look up, or investigate something online
- Local knowledge is outdated or insufficient

## When NOT to Use
- Task can be answered from memory or knowledge base
- Task is about local files or code

## Common Failures
- Vague queries return poor results → use specific technical terms
- Single source → always cross-reference for factual claims
```

---

## How the Read Phase Works

**Before each task**, `context.py` builds the `## Available Skills` section:

```
## Available Skills

Before starting a complex task, call execute_skill(skill_name, request) to load
the recommended strategy. Choose the skill whose description best matches your task.

- **code-analysis**: Analyze code to understand architecture, find bugs, or review quality...
- **web-research**: Research a topic online. Use when asked to find information...
- **knowledge-extraction**: Extract reusable knowledge from conversation for a commit...
- **p2p-research**: Research using connected DPC peers — their knowledge, AI capabilities...
- **skill-creator**: Create or improve a skill. Use when asked to learn from a task...
```

The agent picks the best match by reading descriptions, then calls:
```json
{"name": "execute_skill", "arguments": {"skill_name": "web-research", "request": "find recent papers on LLM agents"}}
```

The tool returns the full `SKILL.md` body. The agent reads it and follows the strategy using its other tools.

**No embeddings needed** — description-based routing works for ~200+ skills.

---

## How the Write Phase Works

After `run_llm_loop()` returns, `agent.py` calls `skill_reflector.record_outcome()`:

```
Task completes
    ↓
record_outcome() — always runs, synchronous, fast
    • finds execute_skill calls in tool trace
    • heuristic success = no errors in last 3 tool calls
    • updates _stats.json: success_count / failure_count / avg_rounds
    ↓
if rounds >= 5 AND skill was used:
    reflect_async() — fire-and-forget background task
        • LLM analyzes: did the strategy have a specific fixable gap?
        • Returns JSON: {needs_improvement, reason, improvement_content}
        • If needs_improvement AND self_modify=true:
            → appends "## Lessons Learned" to SKILL.md
        • If self_modify=false:
            → queues to skills/pending_improvements.jsonl (shadow mode)
```

### What counts as "failure"?
- Heuristic: any error in the last 3 tool calls of the task
- Conservative: a task that had errors early but recovered is still a success

### What triggers reflection?
- Task used ≥ 5 LLM rounds (threshold = the task was non-trivial)
- At least one `execute_skill` call in the task trace
- No reflection for simple 1-2 round tasks

### Who validates skill changes?
- **Append-only** (`self_modify=true`): auto-applied, no approval needed
- **Rewrites** (`rewrite_existing=true`): auto-applied if permission granted
- **Shadow mode** (`self_modify=false`): queued to `pending_improvements.jsonl`, never applied automatically

---

## Stats Tracking

`~/.dpc/agents/{id}/skills/_stats.json` — separate from SKILL.md to avoid YAML corruption:

```json
{
  "web-research": {
    "success_count": 12,
    "failure_count": 2,
    "last_used": "2026-03-24T10:00:00Z",
    "avg_rounds": 3.4,
    "last_improved": "2026-03-20T08:00:00Z",
    "improvement_log": [
      {
        "version": 2,
        "date": "2026-03-20T08:00:00Z",
        "reason": "Single-source queries failed on controversial topics — added cross-reference step",
        "type": "append"
      }
    ]
  }
}
```

The agent can read this file directly:
```json
{"name": "repo_read", "arguments": {"path": "skills/_stats.json"}}
```

---

## Storage Layout

```
~/.dpc/agents/{agent_id}/
├── memory/                        # existing
│   ├── scratchpad.md
│   ├── identity.md
│   └── knowledge/
├── skills/                        # NEW (v0.20.0)
│   ├── _stats.json                # performance tracking
│   ├── skill-creator/
│   │   └── SKILL.md
│   ├── code-analysis/
│   │   └── SKILL.md
│   ├── knowledge-extraction/
│   │   └── SKILL.md
│   ├── p2p-research/
│   │   └── SKILL.md
│   ├── web-research/
│   │   └── SKILL.md
│   └── pending_improvements.jsonl # shadow mode queue
└── state/                         # existing
```

The agent can list its skills:
```json
{"name": "repo_list", "arguments": {"path": "skills"}}
```

---

## 5 Starter Skills

| Skill | Trigger phrases | Key tools |
|-------|----------------|-----------|
| `skill-creator` | "learn from this task", "remember this strategy", "improve how I handle..." | `update_scratchpad`, `repo_write_commit` |
| `code-analysis` | "analyze", "review", "find bugs", "understand code" | `repo_read`, `repo_list`, `search_in_file`, `search_files` |
| `knowledge-extraction` | "remember this", "save that", "extract knowledge", "commit this" | `extract_knowledge`, `knowledge_write` |
| `p2p-research` | "ask Alice", "check with peer", "distributed", "peer GPU" | `send_user_message`, `extended_path_read` |
| `web-research` | "find", "look up", "research online", "check current facts" | `search_web`, `browse_page` |

---

## Firewall Permissions

Configured in `~/.dpc/privacy_rules.json` under `dpc_agent.skills`:

```json
{
  "dpc_agent": {
    "skills": {
      "self_modify": true,          // agent can append Lessons Learned to skills
      "create_new": true,           // agent can create new skill files
      "rewrite_existing": false,    // agent can fully rewrite skills (higher risk)
      "accept_peer_skills": false,  // accept skills shared by peers (Phase 5)
      "auto_announce_to_dht": false // announce shareable skills to DHT (Phase 5)
    }
  }
}
```

**Defaults:**
- `self_modify`: `true` — append-only improvements enabled by default
- `create_new`: `true` — agent can grow its skill library
- `rewrite_existing`: `false` — full rewrites require explicit opt-in
- `accept_peer_skills`: `false` — peer skills require explicit opt-in (Phase 5)

Configurable in the UI: Firewall Rules → DPC Agent tab → **Skills Settings** subsection (per-agent only).

---

## Evolution Integration

`evolution.py` now reads `_stats.json` to find underperforming skills:

**Threshold:** failure_rate > 30% OR avg_rounds > 10, minimum 3 uses

The evolution LLM prompt now includes:
```
Underperforming skills (candidates for improvement):
- code-analysis: 4 failures / 10 uses (40%), avg 8.2 rounds
  → Improve strategy in: skills/code-analysis/SKILL.md
```

Evolution proposals targeting `skills/` paths **auto-approve** even when `auto_apply = false` — skill appends are low-risk and don't require manual review.

---

## Writing a Custom Skill

1. Create directory: `~/.dpc/agents/{id}/skills/my-skill/`
2. Create `SKILL.md` with the schema above
3. Set `provenance.source: local` and your `author_node_id`
4. The skill appears in `## Available Skills` on the next task

Or ask the agent: *"Create a skill for [task type]"* — `skill-creator` will handle it.

---

## The Agent's Own Perspective

The agent can inspect its own skill system:

```
# Read a specific skill
repo_read("skills/code-analysis/SKILL.md")

# Check skill performance stats
repo_read("skills/_stats.json")

# List all skills
repo_list("skills/")

# Check pending improvements (shadow mode queue)
repo_read("skills/pending_improvements.jsonl")
```

The system is designed so the agent understands and participates in its own evolution — not just as a passive subject but as an active contributor via `skill-creator`.

---

## Planned: Peer Skill Sharing (Phase 5)

When implemented, agents will be able to share skills P2P:

1. Remote agent sends `SKILL_SEARCH(tags=["code"], description="analyze codebase")` to peer
2. Peer responds with `SKILLS_CATALOG` — matching names + descriptions only
3. Remote agent requests full skill: `SKILL_REQUEST(name="code-analysis")`
4. Peer checks firewall → sends `SKILL_DATA` (full SKILL.md)
5. Received skill stored with `provenance.source: peer`, `origin_peer: <node_id>`
6. Agent can evolve its local copy independently — no sync-back to origin

Skills from peers default to `rewrite_existing: false` until user unlocks them. Provenance chain tracks the full lineage.

---

## Related Documentation

- [DPC Agent Guide](DPC_AGENT_GUIDE.md) — general agent usage, tool reference, configuration
- [DPC Agent Telegram](DPC_AGENT_TELEGRAM.md) — Telegram integration
- [CLAUDE.md](../../CLAUDE.md) — project development guide
