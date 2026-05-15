# ADR Template

> **Source:** Synthesized from MADR 4.0 (github.com/adr/madr), Crocker's RFC 3 philosophy,
> and 26 DPC project ADRs (ADR-001 through ADR-026).
>
> **Principles:**
> - Crocker (RFC 3) — minimum viable ADR = Context + Decision (+ Rationale subsection). Everything else is optional.
> - MADR 4.0 — structured option analysis, Confirmation, machine-readable metadata.
> - DPC project — Authors with P13 workflow roles, Scope for implementation, live Implementation Status.
>
> **Do NOT create separate template files** (full/minimal/bare). One template, optional sections marked `<!-- optional -->`.

---

## YAML Front Matter

Always present, even for lightweight ADRs. Machine-readable metadata for tooling.

```yaml
---
adr: NNN
title: "Title in imperative mood"
status: proposed | rejected | accepted | deprecated | superseded-by-NNN
date: YYYY-MM-DD
deciders: [Mike]                    # Who makes the final call
consulted: [Ark, CC]                # SMEs who provided input (two-way)
informed: []                        # People to notify post-decision (one-way)
depends_on: [ADR-NNN]               # This ADR requires that one first
related: [ADR-NNN]                  # Cross-references
supersedes: [ADR-NNN]               # If replacing a previous ADR
session: SNN                         # Traceback to source session
<!-- TODO: add `tags` to YAML for tooling-driven conditional validation (e.g. tags: [security] → Confirmation required) -->
---
```

### Status Lifecycle

```
proposed → accepted → deprecated → superseded-by-NNN
proposed → rejected
```

---

## Sections

<!-- required -->
## Context and Problem Statement

Why are we making this decision? Background + problem. One paragraph minimum.

> **Crocker minimum:** If you write nothing else, write this + Decision + Rationale.

<!-- optional -->
## Decision Drivers

Explicit list of forces, constraints, and requirements driving this decision.
Making these visible helps reviewers understand the reasoning without digging through Context.

- **Driver 1:** Description
- **Driver 2:** Description

> **Tip:** If drivers are simple and obvious from Context, you may skip this section.
> For complex or controversial decisions, this section is strongly recommended.

<!-- required -->
## Decision

What we decided. Clear one-liner first, then details.

### Rationale

Why this option over alternatives. Required — the core value of an ADR is captured here.

<!-- optional -->
## Considered Options

List of alternatives evaluated. Brief description of each.

- **Option A:** Description
- **Option B:** Description
- **Option C:** Description

<!-- optional -->
### Pros and Cons of the Options

Per-option detailed analysis. Separate from Considered Options to allow lightweight ADRs
(L1 options list + L2 decision) without the heavy per-option breakdown (L3).

#### Option A

- Good: ...
- Neutral: ...
- Bad: ...

#### Option B

- Good: ...
- Neutral: ...
- Bad: ...

<!-- optional -->
## Consequences

What changes as a result of this decision.

- **Positive:** ...
- **Negative:** ...
- **Neutral:** ...

<!-- conditional: REQUIRED if ADR touches security, auth, data integrity, or breaking changes -->
<!-- optional otherwise -->
## Confirmation

How to verify that the decision was implemented correctly.
This is **compliance**, not progress — it answers "is it right?" not "is it done?".

- [ ] Compliance criterion 1
- [ ] Compliance criterion 2

<!-- optional -->
## Scope

Concrete files, modules, or systems affected by this decision.
Serves as implementation checklist.

- `path/to/file.py` — description of change
- `path/to/other.ts` — description of change

<!-- optional -->
## Implementation Status

Live document. Update as work progresses. Not a snapshot — this section evolves.

| Task | Status | Commit |
|------|--------|--------|
| Task 1 | Done | `abc1234` |
| Task 2 | In Progress | — |
| Task 3 | Pending | — |

<!-- optional -->
## Open Questions

Unresolved issues. Include who is responsible for resolving each.

- **Q1:** Description — @who
- **Q2:** Description — @who

<!-- optional, for P13 multi-agent ADRs -->
## Authors

Workflow roles per Protocol 13. Not a replacement for `deciders/consulted/informed` in YAML —
that's stakeholder communication, this is who did what in the process.

- **Mike** — Decision
- **Ark** — Analysis, Draft
- **CC** — Implementation

<!-- optional -->
## References

- `[ADR-NNN](NNN-title.md)` — related decision
- `knowledge/topic.md` — background knowledge
- `abc1234` — relevant commit
- [External link](https://...) — source material

---

## Quick Reference: What to Fill by Decision Weight

| Weight | Context | Drivers | Decision+Rationale | Options | Pros/Cons | Consequences | Confirmation | Scope | Impl Status |
|--------|---------|---------|-------------------|---------|-----------|-------------|-------------|-------|------------|
| **Light** | required | — | required | — | — | — | — | — | — |
| **Medium** | required | recommended | required | recommended | — | recommended | — | — | — |
| **Heavy** | required | required | required | required | required | required | required | recommended | recommended |
| **Security/Critical** | required | required | required | required | required | required | **REQUIRED** | required | required |
