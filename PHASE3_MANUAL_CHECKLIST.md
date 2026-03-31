# Phase 3 Manual Verification Checklist

Pre-merge safety net. Complete ALL items before merging `refactor/grand` → `dev`.
Run after: `npm run check && npm run build` both pass.

---

## Before Each Panel-Extraction Commit

*(Repeat for any future extractions)*

### Chat (core functionality)
- [ ] Send an AI chat message → response appears in ChatMessageList
- [ ] AI response streams incrementally (not all at once)
- [ ] Switch between two AI chats → each chat retains its own history
- [ ] Send a P2P message to a connected peer → message appears, peer receives
- [ ] Paste an image into chat → preview appears, send triggers vision analysis
- [ ] Switch chats while AI is streaming → streaming stops, progress clears

### Draft & Input
- [ ] Type in chat input, switch to another chat, return → draft is preserved
- [ ] Panel resize handle drag → height changes
- [ ] Panel height persists on page reload (localStorage)

### Token Counter
- [ ] After AI response, token counter shows Dialog / Total / Static rows
- [ ] Static row shows non-negative number (Q14 regression check)
- [ ] Token warning banner appears when context window is ≥ 80% full

### Agent
- [ ] Agents appear in sidebar on startup (not empty after connect)
- [ ] Start an agent task → progress bar shows tool name and round number
- [ ] Switch chats mid-agent-stream → progress indicator clears
- [ ] Agent streaming text appears incrementally in the chat window

### Voice
- [ ] Receive a voice message → audio player renders in message list
- [ ] Auto-transcribe toggle is remembered across chat switches
- [ ] Whisper model loading spinner appears (if model not cached)

### Group Chat
- [ ] Type `@` in a group chat → autocomplete dropdown appears
- [ ] Arrow keys navigate autocomplete items
- [ ] Enter/click selects mention and inserts `@name` into input
- [ ] Receive group invite → invite dialog appears
- [ ] Delete a group → UI redirects to local_ai chat

### Session & Knowledge
- [ ] Click "New Session" in an AI chat → session reset proposal sent
- [ ] Knowledge commit proposal from peer → commit dialog opens
- [ ] Token warning toast appears when context > 80%
- [ ] "End Session & Save Knowledge" button triggers extraction

### Persistence
- [ ] Hard refresh page → AI chats restored from localStorage
- [ ] Hard refresh page → agent chats restored from localStorage
- [ ] Hard refresh page → chat histories restored from localStorage
- [ ] Hard refresh page → Telegram chat list restored

### Connection
- [ ] Backend stops and restarts → UI reconnects automatically
- [ ] Connection error toast appears when backend unreachable

---

## Pre-Merge Final Checklist

Run once before the merge commit:

```bash
cd dpc-client/ui
npm run check       # 0 TypeScript errors
npm run build       # Build succeeds
```

```bash
cd dpc-client/core
poetry run pytest -v   # All tests pass (202/202 target)
```

Manual smoke:
- [ ] All items above pass
- [ ] `npm run check` — 0 errors
- [ ] `npm run build` — succeeds
- [ ] Backend tests — all pass
- [ ] No console errors on page load
- [ ] No negative token values anywhere in the UI

---

## Known Non-Blockers (document, don't fix before merge)

- ~~60 `writable<any>` store declarations~~ — **Done** (466b2c4): 54 typed, 6 intentionally `any`
- API Compatibility Layer (WS versioning) not implemented (grand plan line 277) — post-merge
- `+page.svelte` at 1,514 lines vs ~500 target (all panels extracted; remaining is genuine layout-shell coordination)
