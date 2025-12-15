# Chat History Sync & Mutual New Session - Design Document

## Feature 1: Chat History Sync on Reconnect

### Problem Statement
When a user closes their app and later reconnects, their in-memory chat history is lost. If the peer kept the app open, they still have the full conversation history. We need to sync the history from the peer who stayed online.

### Use Case
```
Scenario:
1. Alice and Bob are chatting
2. Alice sends 10 messages, Bob sends 10 messages
3. Alice closes app (loses in-memory history)
4. Bob keeps app open (retains full history)
5. Alice reopens app and reconnects to Bob
6. Alice's chat window is empty!

Expected Behavior:
- Alice should request chat history from Bob
- Bob should send recent conversation history
- Alice's chat window should restore the conversation
```

### Protocol Design

#### Command: REQUEST_CHAT_HISTORY
**Sent by:** Reconnecting peer (who lost history)
**Sent to:** Peer who stayed online

```json
{
  "command": "REQUEST_CHAT_HISTORY",
  "payload": {
    "conversation_id": "dpc-node-...",
    "max_messages": 100,
    "request_id": "uuid"
  }
}
```

#### Command: CHAT_HISTORY_RESPONSE
**Sent by:** Peer with history
**Sent to:** Requesting peer

```json
{
  "command": "CHAT_HISTORY_RESPONSE",
  "payload": {
    "conversation_id": "dpc-node-...",
    "request_id": "uuid",
    "messages": [
      {
        "sender": "user" | "assistant" | "peer",
        "text": "message content",
        "timestamp": "ISO8601",
        "attachments": []
      }
    ],
    "total_count": 20
  }
}
```

### Implementation Steps

**Backend:**
1. Add `REQUEST_CHAT_HISTORY` handler
   - Receives request from peer
   - Gets conversation history from ConversationMonitor
   - Sends CHAT_HISTORY_RESPONSE

2. Add `CHAT_HISTORY_RESPONSE` handler
   - Receives history from peer
   - Restores messages to local ConversationMonitor
   - Broadcasts to UI for display

3. Add ConversationMonitor.export_history() method
   - Returns recent N messages in serializable format

4. Add ConversationMonitor.import_history() method
   - Accepts messages and adds to history

5. Detect reconnection and auto-request history
   - When peer reconnects, check if local history is empty
   - If empty, automatically send REQUEST_CHAT_HISTORY

**Frontend:**
- Receive `history_restored` event
- Update chatHistories store with restored messages
- Scroll to bottom and show success toast

### Edge Cases
- What if both peers have partial history? (merge by timestamp)
- What if histories conflict? (trust peer's version)
- What if no peer has history? (start fresh)
- History size limit? (default: 100 messages)

---

## Feature 2: Mutual "New Session" Approval

### Problem Statement
Currently, "New Session" only clears the local user's chat. The peer still sees old messages, creating an asymmetric state. We need mutual approval before clearing both chats.

### Use Case
```
Current Behavior:
1. Alice clicks "New Session"
2. Alice's chat clears
3. Bob still sees all old messages
4. Asymmetric state - confusing!

Desired Behavior:
1. Alice clicks "New Session"
2. Bob receives proposal: "Alice wants to start a new session"
3. Bob can approve or reject
4. If approved: Both chats clear simultaneously
5. If rejected: Both keep current conversation
```

### Protocol Design

#### Command: PROPOSE_NEW_SESSION
**Sent by:** User initiating new session
**Sent to:** Peer

```json
{
  "command": "PROPOSE_NEW_SESSION",
  "payload": {
    "proposal_id": "session-uuid",
    "initiator_node_id": "dpc-node-...",
    "conversation_id": "dpc-node-...",
    "timestamp": "ISO8601"
  }
}
```

#### Command: VOTE_NEW_SESSION
**Sent by:** Peer voting on proposal
**Sent to:** Initiator

```json
{
  "command": "VOTE_NEW_SESSION",
  "payload": {
    "proposal_id": "session-uuid",
    "vote": "approve" | "reject",
    "voter_node_id": "dpc-node-..."
  }
}
```

#### Command: NEW_SESSION_RESULT
**Sent by:** Initiator (or both as broadcast)
**Sent to:** All participants

```json
{
  "command": "NEW_SESSION_RESULT",
  "payload": {
    "proposal_id": "session-uuid",
    "result": "approved" | "rejected" | "timeout",
    "clear_history": true | false
  }
}
```

### Implementation Steps

**Backend:**
1. Add NewSessionProposalManager
   - Tracks active proposals (proposal_id → proposal data)
   - Timeout mechanism (60 seconds default)
   - Vote counting (requires unanimous approval)

2. Add `PROPOSE_NEW_SESSION` handler
   - Creates proposal
   - Broadcasts to peer
   - Starts timeout timer

3. Add `VOTE_NEW_SESSION` handler
   - Records vote
   - If all votes received, broadcast result
   - Clear history if approved

4. Add `NEW_SESSION_RESULT` handler
   - Clears local chat history if approved
   - Broadcasts to UI
   - Resets ConversationMonitor

**Frontend:**
1. Add NewSessionDialog component (similar to KnowledgeCommitDialog)
   - Shows proposal details
   - Approve/Reject buttons

2. Update handleNewChat() function
   - Send PROPOSE_NEW_SESSION instead of clearing immediately
   - Show "Waiting for approval..." dialog

3. Handle events:
   - `new_session_proposed` → Show dialog to peer
   - `new_session_result` → Clear chat if approved, show toast

### Edge Cases
- Timeout: 60 seconds, auto-reject if no response
- Peer offline: Disable "New Session" button (like End Session)
- Multiple proposals: Queue or reject new proposals while one is pending
- Rejection: Show toast "New session rejected by peer"

---

## Implementation Priority
1. **Chat History Sync** (Foundation for better reconnection UX)
2. **Mutual New Session** (Improves collaborative experience)

## Testing Plan
- Test reconnection with empty history
- Test reconnection with partial history on both sides
- Test New Session approval flow
- Test New Session rejection flow
- Test timeout scenarios
- Test offline peer scenarios
