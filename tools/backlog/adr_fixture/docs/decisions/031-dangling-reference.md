---
adr: 031
title: "Depends on a decision that does not exist"
status: proposed
date: 2026-08-11
depends_on: [ADR-900]
related: []
supersedes: []
---

# ADR-031: Depends on a decision that does not exist

## Context and Problem Statement

Must **warn**, not refuse — the same treatment a stale reference gets in the backlog: it is
content pointing at something absent, and the checker points rather than edits.

## Decision

Warn.
