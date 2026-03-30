# Phase 3 Panel Extraction — Manual Verification Checklist

Pre-commit gate for DOM-dependent behaviour that cannot run in Vitest node environment.
Check off each item before committing the relevant step.

---

## Before committing Step 4 (ChatPanel.svelte)

Chat input, send routing, image handling, resize, context toggle.

- [ ] Send an AI chat message → response appears in chat window
- [ ] Send a P2P message to a connected peer → message appears on both sides
- [ ] Send a message via Telegram-linked chat → appears in Telegram
- [ ] Send a message in a group chat → appears for all members
- [ ] Paste an image → preview appears in input area, send works, AI vision response returns
- [ ] Switch chats → draft text is preserved in the previous chat, restored on return
- [ ] Panel resize handle drag → chat window height changes, persists on reload
- [ ] Token warning banner appears when context window is >80% full
- [ ] Integrity warning banner appears (if warnings exist at startup)
- [ ] Context toggle checkbox checked → context included in AI query
- [ ] Context toggle checkbox unchecked → context not sent

---

## Before committing Step 5 (AgentPanel.svelte)

Agent progress display and streaming text.

- [ ] Start an agent task → progress bar shows tool name and round number
- [ ] Progress clears when task completes (agent_progress_clear event)
- [ ] Switch chats mid-stream → progress indicator clears for the new chat
- [ ] Agent streaming text → appears incrementally in chat, not all at once
- [ ] Agent streaming "Raw" section expands to show tool calls
- [ ] Thinking block appears for agents that emit it

---

## Before committing Step 6 (VoicePanel.svelte)

Voice recording, transcription, Whisper model loading.

- [ ] Receive a voice message → audio player appears in chat
- [ ] Auto-transcribe enabled: transcription text auto-attaches below voice message
- [ ] Auto-transcribe disabled: no transcription attached
- [ ] Toggle auto-transcribe in settings → toggle persists on chat switch
- [ ] Whisper model loading spinner shows during preload
- [ ] Whisper model load error message shows on failure
- [ ] Retroactive transcription (click Transcribe on old voice message) → text attaches

---

## Before committing Step 7 (GroupPanel.svelte)

Mention autocomplete, group invite, group deletion.

- [ ] Type @ in group chat input → autocomplete dropdown appears with member names
- [ ] Arrow keys navigate autocomplete items (up/down)
- [ ] Enter or click selects a mention → @name inserted into input
- [ ] Escape closes autocomplete without inserting
- [ ] Receive a group invite → dialog appears with accept/decline buttons
- [ ] Accept group invite → group appears in sidebar
- [ ] Decline group invite → dialog closes, group does not appear
- [ ] Delete a group (as creator) → UI redirects to local_ai chat
- [ ] Leave a group (as member) → group removed from sidebar

---

## Before committing Step 8 (+page.svelte layout shell reduction)

Full smoke test — all integration paths.

- [ ] Backend service starts, WebSocket on port 9999 responds
- [ ] UI connects, peer list populates in sidebar
- [ ] AI query executes against local provider (Ollama)
- [ ] AI query executes against remote peer (remote inference)
- [ ] File transfer completes end-to-end (send + receive + hash verified)
- [ ] Agent starts, executes tool calls, responds in chat
- [ ] Group: @mention autocomplete, send group message, invite member, delete group
- [ ] Voice: record, send, transcription attaches to message
- [ ] Telegram: send/receive message via Telegram bridge
- [ ] Knowledge commit proposal flow (propose → vote → result)
- [ ] New session proposal flow (propose → vote → history cleared)
- [ ] Page refresh → AI chats and histories restored from localStorage
