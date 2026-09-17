---
adr: 040
title: "A superseded decision pointing at a number that is not there"
status: superseded-by-999
date: 2026-09-01
deciders: [Mike]
consulted: [CC]
informed: []
depends_on: []
related: []
supersedes: []
---

# ADR-040: A superseded decision pointing at a number that is not there

## Context and Problem Statement

Must be refused. Every other reference in the front matter is resolved; the number
inside the status was the one nobody looked up, so the escape from invariant I1 could
point at nothing and still silence the check.

## Decision

Refuse.
