---
adr: 027
title: "Markdown Runtime Validation in DPC Backend"
status: proposed
date: 2026-05-15
deciders: [Mike]
consulted: [Ark, CC]
informed: []
depends_on: []
related: [ADR-009, ADR-026]
supersedes: []
session: S121
---

# ADR-027: Markdown Runtime Validation in DPC Backend

## Context and Problem Statement

DPC generates, receives, and edits markdown content through multiple actors: agents write ADRs and knowledge, P2P peers share SKILL.md files, users edit content in the UI. Currently there is no automated validation of markdown structure. Malformed content passes through silently, causing issues downstream (broken rendering, unparseable skills, drift from TEMPLATE.md conventions).

The project has 26 ADRs, multiple SKILL.md files across agents, and plans for knowledge federation (ADR-009). All markdown content benefits from structural validation, but the solution must work for all DPC users without requiring external developer tools.

## Decision Drivers

- **D1: Multi-actor authoring** — agents, users, and P2P peers all produce markdown content
- **D2: No external dependencies for end users** — DPC is a desktop app, users should not need Node.js or dev tools
- **D3: Runtime enforcement** — validation should happen automatically at save/import/write time, not require manual invocation
- **D4: MADR compatibility** — ADR configs should be compatible with MADR 4.0 conventions where practical
- **D5: Extensibility** — same infrastructure covers ADRs, skills, knowledge, and future markdown types

## Decision

Integrate `pymarkdownlnt` (Python port of markdownlint) as a bundled dependency in the DPC backend. Validation runs automatically when markdown content passes through the backend — at save, import, P2P-receive, and agent-write time.

### Rationale

Three options were considered (see below). `pymarkdownlnt` shares the same rule engine as `markdownlint-cli2` but runs natively in Python — already part of the DPC stack. This eliminates the Node.js requirement for end users while maintaining rule compatibility with MADR configs.

Validation happens at the backend layer, not at the UI or CLI layer, ensuring all markdown content is checked regardless of entry point.

## Considered Options

### Option A: markdownlint-cli2 (Node.js only)
- **Pros:** MADR ships compatible config, active ecosystem, standard in JS community
- **Cons:** Requires Node.js runtime, dev-only tool, does not work for end users or runtime validation, adds non-Python dependency to backend

### Option B: pymarkdownlnt (Python, bundled)
- **Pros:** Native Python, bundles with DPC backend, same rules as markdownlint-cli2, works for all users automatically, MADR-compatible configs
- **Cons:** Smaller community than Node version, less pre-built configs available

### Option C: Both layers — Layer 1 (Node.js CI/pre-commit) + Layer 2 (Python runtime)
- **Pros:** Maximum coverage — source repo + runtime
- **Cons:** Two tools to maintain, two config formats. Layer 1 deferred until external contributor onboarding or public-repo PR flow creates observable need — added complexity without current trigger

## Consequences

### Positive
- All markdown content validated automatically regardless of author
- P2P-received skills checked before import — malformed skills flagged
- Template compliance enforced without social pressure
- Single tool, single config format, no Node.js requirement

### Negative
- `pymarkdownlnt` added as dependency (pip install)
- Validation latency on save/import (expected minimal)
- Legacy ADRs 001-005 will produce warnings until excluded via config

## Confirmation

Compliance with this decision can be verified by:
1. `pymarkdownlnt` listed in `pyproject.toml` dependencies
2. Validation module processes markdown on save/import/write triggers
3. Config file `.markdownlint.yml` present in `docs/decisions/` with MADR-compatible rules
4. Running validation against 26 existing ADRs produces expected results (failures for 001-005 excluded, passes for 013+)
5. P2P-received SKILL.md flagged if structurally invalid
6. No Node.js required for validation to work

## Scope

### Implementation files
- `dpc-client/core/dpc_client_core/markdown_lint.py` — validation module
- `dpc-client/core/tests/test_markdown_lint.py` — unit tests for validation module
- `docs/decisions/.markdownlint.yml` — ADR-specific config (MADR baseline)
- `pyproject.toml` — add `pymarkdownlnt` dependency
- Legacy ADRs 001-005 excluded via `!00[1-5]-*.md` glob in config

### Integration points
- `dpc_agent/skill_store.py` — validate SKILL.md on P2P import
- Backend save handlers — validate markdown on user/agent write
- Future: knowledge federation (ADR-009 Phase 3)

## Implementation Status

| Task | Status | Commit |
|------|--------|--------|
| Install pymarkdownlnt + verify | pending | — |
| Create ADR config (.markdownlint.yml) | pending | — |
| Create validation module | pending | — |
| Integrate into save/import paths | pending | — |
| Test against 26 existing ADRs | pending | — |
| Unit tests for validation module | pending | — |

## Open Questions

1. **Block vs warn on validation failure** — should malformed markdown be rejected (block save) or accepted with a warning? Critical for P2P imports (reject broken skills?) vs user editing (warn, don't block). — @Mike
2. **Config location for skills** — SKILL.md files live in `~/.dpc/agents/*/skills/` (not in repo). Inline config shipped with DPC, or per-agent config? — @CC

## References

- [TEMPLATE.md](TEMPLATE.md) — ADR template with MADR 4.0 conventions
- [ADR-009](009-automated-knowledge-extraction.md) — Knowledge extraction (federation dependency)
- [ADR-026](026-public-agent-guardrails.md) — Public agent guardrails
- [MADR 4.0](https://github.com/adr/madr) — Markdown Architectural Decision Records
- [pymarkdownlnt](https://github.com/jackdewinter/pymarkdown) — Python markdown linter
- [VISION.md](../../VISION.md) — C9 (public ADRs, multi-actor design)
- S121 discussion — scale-blindness corrections [37]-[48], Option B selection [78]-[80]

## Authors

| Role | Who | Responsibility |
|------|-----|----------------|
| Analysis | Ark | Research MADR/Crocher, gap analysis, draft ADR |
| Decision | Mike | Tool choice, Option B selection |
| Review | CC | Fact-check, structure review, implementation planning |
