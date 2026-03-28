# ADR 003: Frontend Store Strategy (Hybrid Local/Global)

**Date:** 2026-03-28
**Status:** Accepted
**Branch:** refactor/grand

## Context

`+page.svelte` is 219KB with all UI concerns (chat, agents, groups, voice, settings) mixed
in one file. `coreService.ts` is 74KB with 50+ `writable<any>` stores — shapes documented
in comments but not enforced by TypeScript.

## Decision

### Store Strategy: Hybrid (Арх recommendation)

**Global stores** (in `coreService.ts`): ONLY truly cross-panel state
- `connectionStatus` — peer connected/disconnected
- `currentUser` — local node identity
- `providerList` — available AI providers

**Local stores** (per panel component): panel-specific state
- `ChatPanel.svelte`: `messages`, `draft`, `typingIndicator`
- `AgentPanel.svelte`: `agentTasks`, `agentStatus`
- `GroupPanel.svelte`: `groupMembers`, `groupHistory`
- `VoicePanel.svelte`: `recordingState`, `transcriptionStatus`

### File Structure

```
src/routes/+page.svelte            ← Layout shell (~200 lines)
src/lib/panels/
  ChatPanel.svelte                 ← local chatStore
  AgentPanel.svelte                ← local agentStore
  GroupPanel.svelte                ← local groupStore
  VoicePanel.svelte                ← local voiceStore

src/lib/services/                  ← Split coreService.ts by domain
  chatService.ts                   ← Chat WS commands + stores
  agentService.ts                  ← Agent commands + stores
  connectionService.ts             ← Peer/connection state
  settingsService.ts               ← Providers, firewall, context

src/lib/coreService.ts             ← Thin bootstrapper: WS connect + route events
```

### Type Priority

Type the 10 highest-traffic stores first before splitting:
```typescript
// ChatMessage, PeerInfo, AgentTask, ConnectionStatus,
// Provider, GroupInfo, FileTransfer, VoiceMessage,
// KnowledgeCommit, InferenceRequest
```

## Why Hybrid (not pure global stores)

- Pure global: 50+ stores = complexity explosion, hard to trace data flow
- Pure local: cross-panel state (connection status) becomes prop-drilling
- Hybrid: local stores per panel = encapsulation; global = minimal shared state

## Rationale

- Unblocks Phase 2 Team UI (GroupPanel can be added without modifying +page.svelte)
- TypeScript types on stores catch bugs at compile time
- Local stores make components independently testable
