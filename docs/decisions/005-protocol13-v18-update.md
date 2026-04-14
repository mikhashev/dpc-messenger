# ADR 005: Protocol 13 v1.8 Update — Session Closing + Scope-Only Estimation

## Status
Proposed (2026-04-08, S14)

## Context
Session S14 revealed three process gaps in Protocol 13 v1.7:

1. **No session closing protocol.** Ark almost left without saving knowledge/backlog. Mike caught it and we improvised a structured closing (CC shows saves → Ark shows plan → coordinate → execute). This worked well but wasn't documented.

2. **Time estimates are meaningless for AI.** CC estimated 5-7h for a 201-line change; actual time was 11 minutes (27-38x error). Scope estimates were accurate (242 predicted vs 201 actual, 17% off). Mike: "только объём, без времени."

3. **§9 Consciousness & Evolution outdated.** After commit cf98c5b (tool data pipeline), evolution uses 24h temporal windows (not tail 50), consciousness uses 6h windows (not tail 20), tools.jsonl now logs duration_ms/round/session_id, consciousness can write to scratchpad, logs rotate at 5MB.

Additionally, minor factual inaccuracies accumulated since v1.4:
- §4 still says "no direct communication channel" (Pattern D/E exist)
- §6 references MCP bridge as primary (cc_agent_bridge.py replaced it)

## Decision
Minimal additions to P13 (~20 lines), not a rewrite. Version bump to v1.8.

### New additions:
- **P18: Session Closing Protocol** — retrospective + CC shows saves + Ark shows plan + coordinate + execute + Mike closes
- **Scope-only estimation rule** — estimate lines/files, never time

### Updates:
- §9: temporal windows, new tools.jsonl fields, consciousness scratchpad write, log rotation
- §4: acknowledge Pattern D/E direct channel
- §6: cc_agent_bridge.py as primary, MCP as fallback

## Alternatives Considered
1. **Full P13 rewrite** — rejected. Current structure works, just needs incremental updates.
2. **Separate documents** (UI workflow, estimation guide) — rejected. Fragmentation; P13 is the single operating agreement.
3. **No ADR for 20-line change** — rejected. P13 §3.5 lists protocol changes as Dangerous Actions. Process applies even for small changes (Mike's point: "вы оба про него забыли").

## Consequences
- P13 stays current with actual team practices
- Session closing becomes a formal checkpoint (prevents knowledge loss)
- Estimation discussions use objective metrics only
- ADR trail documents why protocol evolved
