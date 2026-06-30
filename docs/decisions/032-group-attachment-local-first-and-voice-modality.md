---
adr: 032
title: "Local-first publishing for group attachments + voice input modality routing"
status: accepted
date: 2026-06-30
deciders: [Mike]
consulted: [Ark, CC]
informed: []
related: [ADR-023]
session: S227
---

# ADR-032 — Local-first publishing for group attachments + voice input modality routing

> **Accepted (Mike, S227, 2026-06-30).** Two architecturally independent layers in one
> ADR: **Part A** (local-first publishing, affects voice/image/file) and **Part B**
> (voice input modality routing, voice only). Q1/Q3 resolved by Mike; Q2 is an
> implementation detail (UI selector form). **Delivery phased** — voice first, images
> next, files deferred (see Part A). CC — Draft + Implementation; Ark — Review.

---

## Context and Problem Statement

Sending a **voice message** in a group chat surfaced two defects, both confirmed
against the live log (2026-06-30) and the code:

1. **Sender loses their own message when no peers are connected (red #2).** Two real
   sends (`send_group_voice_message`) returned `success` in ~4–13 ms and then
   produced **no** FILE_OFFER / `group_file_received` / history write / transcription.
   The audio file was saved to disk (`voice_20260630_070637.wav`, 315 KB) but never
   appeared in the sender's chat or history. Cause: DHT routing table empty → no
   connected peers → fan-out to nobody (`transfer_ids = []`), and the sender's echo +
   history write are **gated on FILE_COMPLETE**, which never arrives.

2. **Offline members silently miss the attachment (red #1).** Fan-out skips
   non-connected members (`if node_id not in connected_peers: continue`); there is no
   store-and-forward for group attachments.

Code check (Observed) confirmed the pattern is **common to all three group
attachment types**, not voice-specific:

- `send_group_file` ([service.py:5093]), `send_group_image` ([:5135]),
  `send_group_voice_message` ([:5260]) — identical fan-out-to-connected-peers loop.
- Echo + history for all three run through `file_complete_handler` (`direction ==
  "upload"`, `group_id`, dedup) — all gated on FILE_COMPLETE.
- Contrast: **group text** (`send_group_message`) is already local-first — it writes
  history immediately on send (log: "Added message to history" with no roundtrip).

Separately, a **dictation** capability (voice → text into the input box) already
exists in code — `handleTranscribeVoiceMessage` (`ChatPanel.svelte:871`) calls
`transcribe_audio` and inserts the result into `currentInput` — but it is wired
**only** for AI chats (`local_ai` / `ai_*` / `agent_*`). In group/P2P chats the
recorder's Send always attempts to send audio; the user has no dictation option.

Mike's design intent (from the discussion): a user recording voice in a group should
be able to either (i) send it as a voice message, or (ii) use it as voice input that
becomes editable text in the input box — and when the group has no other members,
default to (ii).

## Decision Drivers

- **D1 — sender never loses their own content.** A sent message must persist locally
  and show in the sender's UI regardless of peer connectivity.
- **D2 — dictation is a first-class, always-available option** (Mike [S227]: "даже
  если есть ≥1 другая подключённая нода, должна быть опция голосовой ввод в input").
- **D3 — input mode must be stable**, not flip with transient online/offline state of
  a peer (Ark: store-and-forward is a separate feature, not a reason to change mode).
- **D4 — reuse what exists.** Dictation-to-input is already implemented; the work is
  exposing it, not building transcription.
- **D5 — one general fix beats three special-cases.** The local-first defect is shared
  by voice/image/file; fix it once at the attachment layer.

## Decision

Two independent layers.

### Part A — Local-first publishing for group attachments (voice / image / file)

On send, the message is written to the group history and echoed to the sender's UI
**immediately**, decoupled from FILE_COMPLETE. P2P fan-out becomes **asynchronous
enrichment** (delivery), not a precondition for the sender seeing/keeping the message.
This brings group attachments to parity with group text, which is already local-first.

Store-and-forward for offline members (red #1) is acknowledged but **out of scope** —
a separate future feature.

**Delivery phasing (Mike, S227):** ship per attachment type, not all at once —
**(1) voice first**, **(2) images next**, **(3) files deferred** (large files need
separate size/chunking design before applying local-first echo — "там может быть
размер ого-го"). The Part A mechanism is identical for all three; only the rollout is
staged.

### Part B — Voice input modality routing (voice only)

The same voice recording is routed to one of two sinks:

| Mode | Trigger | Behavior |
|------|---------|----------|
| **voice-to-chat** | user picks "voice message" (offered only when the group has **>1 node**) | audio saved locally → if transcription enabled, transcribe **once on the sender** → message with audio + text published to chat (via Part A); **all participants receive the same transcription text** |
| **voice-to-input** | user picks "dictation" **OR** the chat has **≤1 node** (only you) | audio → whisper → text inserted into the input box; **no audio sent or stored** |

Routing is by **node count (membership), not runtime connectivity** — the input mode
must not flip when a peer goes offline mid-session (D3). Decisions confirmed by Mike
(S227):

- **Boundary = >1 node in the group.** Group with only your node (≤1 node) → default
  voice-to-input. >1 node → user chooses mode explicitly. ("> 1 node", not "has other
  members", so a group whose only other members are node-less bots/services still
  defaults to dictation.)
- **Q1 (transcription side) — RESOLVED:** computed **once on the sender**; every
  participant receives the same text. (Not N receivers each running whisper.)
- **Q3 (1:1 / solo) — RESOLVED:** the "≤1 node → dictation" rule applies to **all
  chats**, not only groups — a 1:1/solo chat with no connected counterpart defaults to
  voice-to-input.
- Treatment: **transcription = primary content, audio = optional enrichment** for those
  who want to listen.

### Rationale

- Part A directly removes the root cause of red #2 (echo coupled to a P2P roundtrip)
  and generalizes the already-correct group-text behavior to attachments (D1, D5).
- Part B trigger uses membership, not connectivity, so the input mode is stable (D3);
  it also dissolves the red #2 edge case for voice — with no members the recording
  becomes editable text and nothing can be "lost". Reuses existing `transcribe_audio`
  → input path (D4).
- The two parts are decoupled: Part A can ship without Part B and vice-versa.

## Considered Options (routing trigger)

- **Option (a) — runtime connectivity:** switch to voice-to-input when no peers are
  *connected right now*. Bad: input mode flips based on transient state; a member
  going offline mid-session changes UI behavior. CC initially leaned here (to avoid a
  dead audio file), but Part A's local-first treatment removes the "dead file" concern,
  so the argument for (a) collapses.
- **Option (b) — node count / membership (CHOSEN):** switch to voice-to-input only when
  the chat has ≤1 node (no other node-bearing participant). Good: stable mode,
  independent of who is online.

## Consequences

- **Positive:** sender always keeps their message (text + attachments) locally; voice
  works usefully even solo; dictation available everywhere; one fix covers
  voice/image/file.
- **Negative / accepted:** offline members still miss group attachments until
  store-and-forward exists (tracked separately); local echo can now show a message
  whose P2P delivery later fails — needs a per-recipient delivery/▾status indication so
  "shown locally" is not mistaken for "delivered".
- **Neutral:** group history may contain attachments that were never delivered to
  anyone (same semantics as a sent text nobody was online to receive).

## Confirmation

How to verify the decision was implemented correctly (compliance, not progress):

- [ ] Send voice / image / file in a group with **no connected peers** → the message
  appears in the sender's chat and group history **immediately** (no FILE_COMPLETE
  dependency).
- [ ] **voice-to-input** mode: recording → text in the input box, with **no audio file
  on disk and no P2P traffic**.
- [ ] **voice-to-chat** with transcription enabled: audio + transcription published;
  transcription computed **once** (on the sender), all participants receive the same
  text — not N independent whisper runs.
- [ ] Chat with **≤1 node** defaults to voice-to-input; group with **>1 node** offers
  the explicit mode choice.

## Scope (implementation checklist — pending Decision)

- `dpc-client/core/.../service.py` — `send_group_file` / `send_group_image` /
  `send_group_voice_message`: write history + broadcast sender echo on send (Part A).
- `dpc-client/core/.../message_handlers/file_complete_handler.py` — stop using
  FILE_COMPLETE as the echo/history trigger for group sends; FILE_COMPLETE becomes
  delivery-status only.
- `dpc-client/ui/.../panels/ChatPanel.svelte` — `handleSendVoiceMessage`: expose
  voice-to-input in group/P2P (reuse `handleTranscribeVoiceMessage`, already wired for
  AI chats at :847/:871); add the mode selector for the >1-node group case; apply the
  ≤1-node → dictation default.
- `dpc-client/ui/.../components/VoiceRecorder.svelte` — mode affordance (see Q2).

## Implementation Status

Phased per Mike's S227 scope decision. Tasks tracked under
`tasks/adr-032-group-attachment-local-first/` (000-overview + 001-008).

| Phase | Task | Status | Commit |
|-------|------|--------|--------|
| 1 | Part A local-first publishing — **voice** | Pending | — |
| 1 | Part B voice modality routing (≤1-node default + >1-node selector) | Pending | — |
| 2 | Part A local-first publishing — **images** | Pending | — |
| 3 | Part A local-first publishing — **files** (after large-file size design) | Deferred | — |

## Open Questions

- **Q1 — transcription side: RESOLVED (Mike, S227)** — sender-side, once; all
  participants get the same text.
- **Q3 — 1:1 / solo: RESOLVED (Mike, S227)** — the ≤1-node → dictation rule applies to
  all chats.
- **Q2 — UI mode selector: UNDER INVESTIGATION.** Mike's steer: model it on the
  existing 1:1 voice UI (where Send is repurposed to transcribe-into-input). Observed
  reference: in AI chats `handleSendVoiceMessage` routes to `handleTranscribeVoiceMessage`
  ([ChatPanel.svelte:847](../../dpc-client/ui/src/lib/panels/ChatPanel.svelte#L847)),
  which calls `transcribe_audio` and inserts the result into `currentInput`
  ([:871](../../dpc-client/ui/src/lib/panels/ChatPanel.svelte#L871)) — a **single Send
  repurposed**, no selector (only one mode applies in AI chats). The new case is the
  **>1-node group**, where both modes are valid and a selector is needed (two buttons /
  long-press / toggle). Decide the selector form during implementation. — @Mike / CC

## Authors

- **Mike** — Decision
- **CC** — Draft, Implementation
- **Ark** — Analysis, Review

## References

- `[ADR-023](023-group-chat-participant-model.md)` — group chat participant model
- `service.py:5093 / :5135 / :5260` — group attachment send paths (Observed)
- `message_handlers/file_complete_handler.py` — FILE_COMPLETE-gated echo (Observed)
- `ChatPanel.svelte:871` — existing dictation-to-input path (Observed)
- log `dpc-client.log` 2026-06-30 — empirical confirmation of red #2 (no-peer send)
