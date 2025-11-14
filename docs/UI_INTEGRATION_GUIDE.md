# UI Integration Guide - Knowledge Architecture

**Status:** Components created, ready for integration
**Phase:** UI Integration (Next Step after Backend Implementation)

---

## Overview

This guide shows how to integrate the Knowledge Architecture UI components into the DPC Messenger frontend.

### Components Created

1. ✅ **KnowledgeCommitDialog.svelte** - Approve/reject knowledge commits
2. ✅ **ContextViewer.svelte** - View/manage personal context
3. ⏳ **@-mention autocomplete** - To be added to chat input
4. ⏳ **CommitHistory viewer** - Integrated into ContextViewer
5. ⏳ **EffectivenessDashboard** - Future enhancement

---

## Component Locations

```
dpc-client/ui/src/lib/components/
├── KnowledgeCommitDialog.svelte   [NEW]
└── ContextViewer.svelte            [NEW]
```

---

## Integration Steps

### 1. Add Backend Event Handlers

**File: `dpc-client/ui/src/lib/coreService.ts`**

Add new event types and handlers:

```typescript
// Add to message handler
export function handleCoreMessage(data: any) {
  // ... existing handlers ...

  // NEW: Handle knowledge commit proposals
  if (data.command === 'KNOWLEDGE_COMMIT_PROPOSED') {
    knowledgeCommitProposal.set(data.payload);
  }

  // NEW: Handle commit approval notifications
  if (data.command === 'KNOWLEDGE_COMMIT_APPROVED') {
    // Refresh context view
    sendCommand('get_personal_context', {});
  }
}

// NEW: Add knowledge commit stores
import { writable } from 'svelte/store';
export const knowledgeCommitProposal = writable(null);
export const personalContext = writable(null);
```

### 2. Integrate into Main UI

**File: `dpc-client/ui/src/routes/+page.svelte`**

```svelte
<script lang="ts">
  import KnowledgeCommitDialog from '$lib/components/KnowledgeCommitDialog.svelte';
  import ContextViewer from '$lib/components/ContextViewer.svelte';
  import { knowledgeCommitProposal, personalContext, sendCommand } from '$lib/coreService';

  // UI state
  let showContextViewer = false;
  let showCommitDialog = false;

  // Subscribe to stores
  $: if ($knowledgeCommitProposal) {
    showCommitDialog = true;
  }

  // Load personal context
  async function loadPersonalContext() {
    sendCommand('get_personal_context', {});
    showContextViewer = true;
  }

  // Handle commit vote
  function handleCommitVote(event) {
    const { proposal_id, vote, comment } = event.detail;
    sendCommand('vote_knowledge_commit', {
      proposal_id,
      vote,
      comment
    });
    showCommitDialog = false;
  }

  // Handle commit dialog close
  function closeCommitDialog() {
    showCommitDialog = false;
    $knowledgeCommitProposal = null;
  }
</script>

<!-- Add to template -->
<div class="sidebar">
  <!-- ... existing sidebar content ... -->

  <!-- NEW: Context viewer button -->
  <button on:click={loadPersonalContext}>
    View Personal Context
  </button>
</div>

<!-- NEW: Knowledge commit dialog -->
<KnowledgeCommitDialog
  bind:open={showCommitDialog}
  proposal={$knowledgeCommitProposal}
  on:vote={handleCommitVote}
  on:close={closeCommitDialog}
/>

<!-- NEW: Context viewer -->
<ContextViewer
  bind:open={showContextViewer}
  context={$personalContext}
  on:close={() => showContextViewer = false}
/>
```

### 3. Add Backend Commands

**File: `dpc-client/core/dpc_client_core/local_api.py`**

Add new command handlers:

```python
# Add to handle_command method

async def handle_command(self, command: str, payload: dict, request_id: str):
    # ... existing commands ...

    elif command == "get_personal_context":
        # Load and return personal context
        from dpc_protocol.pcm_core import PCMCore
        pcm_core = PCMCore()
        context = pcm_core.load_context()

        await self.send_response(request_id, {
            "status": "success",
            "context": asdict(context)
        })

    elif command == "vote_knowledge_commit":
        # Handle vote on knowledge commit
        proposal_id = payload.get('proposal_id')
        vote = payload.get('vote')
        comment = payload.get('comment')

        # Forward to ConsensusManager
        if hasattr(self.core_service, 'consensus_manager'):
            await self.core_service.consensus_manager.cast_vote(
                proposal_id=proposal_id,
                vote=vote,
                comment=comment,
                broadcast_func=self.core_service.broadcast_to_peers
            )

            await self.send_response(request_id, {
                "status": "success",
                "message": "Vote cast successfully"
            })
```

### 4. Wire Up Conversation Monitor

**File: `dpc-client/core/dpc_client_core/service.py`**

Initialize conversation monitoring for group chats:

```python
from .conversation_monitor import ConversationMonitor
from .consensus_manager import ConsensusManager

class CoreService:
    def __init__(self):
        # ... existing init ...

        # NEW: Initialize consensus manager
        self.consensus_manager = ConsensusManager(
            node_id=self.p2p_manager.node_id,
            pcm_core=PCMCore(),
            vote_timeout_minutes=10
        )

        # NEW: Conversation monitors (per group chat)
        self.conversation_monitors = {}  # conversation_id -> ConversationMonitor

    async def handle_text_message(self, message, sender_id, conversation_id):
        # ... existing message handling ...

        # NEW: Feed message to conversation monitor
        if conversation_id in self.conversation_monitors:
            monitor = self.conversation_monitors[conversation_id]

            # Create Message object
            from conversation_monitor import Message
            msg_obj = Message(
                message_id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                sender_node_id=sender_id,
                sender_name=self.get_peer_name(sender_id),
                text=message['text'],
                timestamp=datetime.utcnow().isoformat()
            )

            # Check for knowledge commit proposal
            proposal = await monitor.on_message(msg_obj)

            if proposal:
                # Broadcast to UI
                await self.local_api_server.broadcast({
                    "command": "KNOWLEDGE_COMMIT_PROPOSED",
                    "payload": proposal.to_dict()
                })

                # Start voting session
                await self.consensus_manager.propose_commit(
                    proposal=proposal,
                    broadcast_func=self.broadcast_to_peers
                )
```

---

## Event Flow

```
1. User sends message in group chat
   └─> ConversationMonitor analyzes messages

2. Monitor detects knowledge-worthy content (score > 0.7)
   └─> Generates KnowledgeCommitProposal
       └─> Broadcast to UI: KNOWLEDGE_COMMIT_PROPOSED
           └─> KnowledgeCommitDialog opens

3. User votes (approve/reject/request_changes)
   └─> Frontend sends: vote_knowledge_commit
       └─> ConsensusManager.cast_vote()
           └─> Broadcast vote to peers

4. All participants vote
   └─> ConsensusManager finalizes
       └─> If approved: Apply commit to personal.json
           └─> Broadcast to UI: KNOWLEDGE_COMMIT_APPROVED
               └─> Show success notification
```

---

## Example: Full Integration

**Minimal working example:**

```svelte
<!-- +page.svelte -->
<script lang="ts">
  import KnowledgeCommitDialog from '$lib/components/KnowledgeCommitDialog.svelte';
  import ContextViewer from '$lib/components/ContextViewer.svelte';
  import { sendCommand } from '$lib/coreService';

  let proposal = null;
  let context = null;
  let showCommitDialog = false;
  let showContextViewer = false;

  // Listen for commit proposals from backend
  async function setupEventListeners() {
    // In coreService.ts, add:
    // onKnowledgeCommitProposed((data) => {
    //   proposal = data;
    //   showCommitDialog = true;
    // });
  }

  function handleVote(event) {
    sendCommand('vote_knowledge_commit', event.detail);
    showCommitDialog = false;
  }

  function viewContext() {
    sendCommand('get_personal_context', {}).then((response) => {
      context = response.context;
      showContextViewer = true;
    });
  }
</script>

<button on:click={viewContext}>My Context</button>

<KnowledgeCommitDialog
  bind:open={showCommitDialog}
  {proposal}
  on:vote={handleVote}
  on:close={() => showCommitDialog = false}
/>

<ContextViewer
  bind:open={showContextViewer}
  {context}
  on:close={() => showContextViewer = false}
/>
```

---

## Testing Checklist

### Backend Integration
- [ ] `get_personal_context` command returns context
- [ ] `vote_knowledge_commit` command works
- [ ] ConversationMonitor detects knowledge in messages
- [ ] ConsensusManager coordinates voting
- [ ] Commits are applied to personal.json

### Frontend Integration
- [ ] KnowledgeCommitDialog opens when proposal received
- [ ] Can approve/reject/request changes
- [ ] ContextViewer displays all tabs correctly
- [ ] Profile, knowledge, instructions, history all show
- [ ] Voting triggers backend command
- [ ] Success notifications appear

### End-to-End
- [ ] Group chat conversation triggers proposal
- [ ] All participants see dialog
- [ ] Voting completes successfully
- [ ] Commit appears in personal.json
- [ ] Context viewer shows new knowledge
- [ ] Markdown files created if configured

---

## Next Steps

1. **Immediate (Phase 1):**
   - ✅ Wire up `get_personal_context` command
   - ✅ Wire up `vote_knowledge_commit` command
   - ✅ Test dialogs with mock data

2. **Short-term (Phase 2):**
   - Add ConversationMonitor to group chats
   - Connect ConsensusManager voting
   - Test with 2+ users in group chat

3. **Future Enhancements:**
   - @-mention autocomplete in chat input
   - Effectiveness dashboard
   - Spaced repetition reminders
   - Markdown file viewer/editor
   - Knowledge graph visualization

---

## Quick Start Commands

```bash
# Start backend
cd dpc-client/core
poetry run python run_service.py

# Start frontend (separate terminal)
cd dpc-client/ui
npm run tauri dev

# Test with mock data (in browser console)
window.dispatchEvent(new CustomEvent('knowledge-commit-proposed', {
  detail: {
    proposal_id: 'test-123',
    topic: 'test_topic',
    summary: 'Test knowledge commit',
    entries: [
      {
        content: 'This is test knowledge',
        tags: ['test'],
        confidence: 0.95,
        cultural_specific: false,
        requires_context: [],
        alternative_viewpoints: ['Alternative view 1']
      }
    ],
    participants: ['alice', 'bob'],
    cultural_perspectives: ['Western', 'Eastern'],
    alternatives: [],
    devil_advocate: 'This might not be accurate in all contexts',
    avg_confidence: 0.95
  }
}));
```

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    Frontend (SvelteKit)                 │
├─────────────────────────────────────────────────────────┤
│  +page.svelte                                           │
│  ├─ KnowledgeCommitDialog (approve/reject)             │
│  └─ ContextViewer (view/manage context)                │
└──────────────────┬──────────────────────────────────────┘
                   │ WebSocket (localhost:9999)
┌──────────────────┴──────────────────────────────────────┐
│              Backend (Python - CoreService)             │
├─────────────────────────────────────────────────────────┤
│  local_api.py (WebSocket server)                        │
│  ├─ get_personal_context → PCMCore.load_context()      │
│  └─ vote_knowledge_commit → ConsensusManager.cast_vote()│
│                                                          │
│  service.py (Main orchestrator)                         │
│  ├─ ConversationMonitor (detect knowledge)             │
│  ├─ ConsensusManager (coordinate voting)               │
│  └─ PCMCore (read/write personal.json)                 │
└─────────────────────────────────────────────────────────┘
```

---

## Support

- **Documentation:** See `docs/KNOWLEDGE_ARCHITECTURE.md`
- **Examples:** See `dpc-protocol/*/if __name__ == '__main__'` blocks
- **Tests:** Run `poetry run pytest tests/test_pcm_compatibility.py`

**Status: Ready for integration!** 🚀
