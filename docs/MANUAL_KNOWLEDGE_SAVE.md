# Manual Knowledge Save - Implementation Guide

**Status:** Ready for Implementation
**Date:** 2025-11-26
**Prerequisites:** Personal Context v2.0, Commit Integrity System

---

## Overview

Manual Knowledge Save allows users to create knowledge entries directly from chat messages without AI extraction. Users can:
- Save any message as knowledge (solo mode: instant save)
- Propose knowledge for group approval (collaborative mode: requires consensus)
- Edit topic name, summary, tags before saving
- Choose existing topic or create new one

---

## User Flow

### Solo Mode (Single User)
```
1. User types message in chat
2. User clicks "💾 Save as Knowledge" button (appears on message hover)
3. Dialog opens pre-filled with message content
4. User edits/confirms:
   - Topic name (new or existing)
   - Summary
   - Tags
   - Mastery level
5. User clicks "Save"
6. Knowledge saved immediately to personal.json + markdown
7. Success toast notification
```

### Collaborative Mode (Multi-User)
```
1. User A types message in group chat
2. User A clicks "💾 Propose as Knowledge"
3. Dialog opens with message content
4. User A fills out form and clicks "Propose"
5. Proposal sent to all participants via P2P
6. Other users see "📋 Knowledge Proposal" notification
7. Users vote: Approve/Reject/Edit
8. Once consensus reached (all approve OR majority):
   - Knowledge saved to all participants' personal.json
   - Commit created with signatures from all approvers
   - Markdown file created with cryptographic signatures
9. All participants see "✅ Knowledge Added" notification
```

---

## Backend Implementation

### 1. Add Command Handler

**File:** `dpc-client/core/dpc_client_core/service.py`

```python
async def create_manual_knowledge_commit(
    self,
    topic_name: str,
    summary: str,
    content: str,
    tags: List[str],
    mastery_level: str = "beginner",
    conversation_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a manual knowledge commit from user input.

    Args:
        topic_name: Name of topic (new or existing)
        summary: Brief description
        content: Knowledge entry content
        tags: List of tags
        mastery_level: beginner/intermediate/advanced
        conversation_id: For consensus tracking (if in group chat)

    Returns:
        Dict with status and commit_id
    """
    from dpc_protocol.knowledge_commit import KnowledgeCommit, KnowledgeEntry, KnowledgeSource

    try:
        # Check if solo or collaborative mode
        is_collaborative = conversation_id and self.conversation_monitors.get(conversation_id)

        # Create knowledge entry
        entry = KnowledgeEntry(
            content=content,
            tags=tags if tags else ["manual"],
            confidence=1.0,  # User-created = 100% confidence
            last_updated=datetime.utcnow().isoformat(),
            source=KnowledgeSource(
                type="manual_edit",
                conversation_id=conversation_id,
                participants=[self.node_id] if not is_collaborative else [],
                consensus_status="draft" if is_collaborative else "approved",
                approved_by=[self.node_id] if not is_collaborative else []
            )
        )

        # Create commit
        commit = KnowledgeCommit(
            topic=topic_name,
            summary=summary,
            entries=[entry],
            conversation_id=conversation_id,
            mastery_level=mastery_level
        )

        if is_collaborative:
            # Use consensus manager for group approval
            proposal_id = await self.consensus_manager.propose_commit(
                commit,
                conversation_id
            )

            return {
                "status": "proposed",
                "proposal_id": proposal_id,
                "message": "Knowledge proposal sent to participants"
            }

        else:
            # Solo mode: instant save
            await self.consensus_manager._apply_commit(commit)

            return {
                "status": "success",
                "commit_id": commit.commit_id,
                "message": f"Knowledge saved to topic '{topic_name}'"
            }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }
```

### 2. Register Command

**File:** `dpc-client/core/dpc_client_core/local_api.py`

```python
# In handle_command():
elif command == "create_manual_knowledge_commit":
    result = await self.core_service.create_manual_knowledge_commit(
        topic_name=payload.get('topic_name'),
        summary=payload.get('summary'),
        content=payload.get('content'),
        tags=payload.get('tags', []),
        mastery_level=payload.get('mastery_level', 'beginner'),
        conversation_id=payload.get('conversation_id')
    )
```

---

## Frontend Implementation

### 1. Create Dialog Component

**File:** `dpc-client/ui/src/lib/components/ManualKnowledgeDialog.svelte`

```svelte
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { sendCommand } from '$lib/coreService';

  export let messageContent: string = '';
  export let conversationId: string | null = null;
  export let existingTopics: string[] = [];

  const dispatch = createEventDispatcher();

  let topicName = '';
  let summary = '';
  let tags = '';
  let masteryLevel: 'beginner' | 'intermediate' | 'advanced' = 'beginner';
  let isNewTopic = true;
  let saving = false;

  $: tagsList = tags.split(',').map(t => t.trim()).filter(t => t.length > 0);
  $: canSave = topicName.length > 0 && summary.length > 0 && messageContent.length > 0;

  async function handleSave() {
    if (!canSave) return;

    saving = true;
    try {
      const result = await sendCommand('create_manual_knowledge_commit', {
        topic_name: topicName,
        summary: summary,
        content: messageContent,
        tags: tagsList,
        mastery_level: masteryLevel,
        conversation_id: conversationId
      });

      if (result.status === 'success' || result.status === 'proposed') {
        dispatch('saved', { result });
      } else {
        dispatch('error', { message: result.message });
      }
    } catch (error) {
      dispatch('error', { message: error.message });
    } finally {
      saving = false;
    }
  }

  function handleClose() {
    dispatch('close');
  }
</script>

<dialog open>
  <div class="dialog-header">
    <h2>💾 Save as Knowledge</h2>
    <button class="close-btn" on:click={handleClose}>×</button>
  </div>

  <form on:submit|preventDefault={handleSave}>
    <div class="form-group">
      <label for="topic">Topic Name</label>
      {#if existingTopics.length > 0}
        <div class="topic-selector">
          <label>
            <input type="radio" bind:group={isNewTopic} value={false} />
            Existing Topic
          </label>
          {#if !isNewTopic}
            <select bind:value={topicName}>
              <option value="">Select a topic...</option>
              {#each existingTopics as topic}
                <option value={topic}>{topic}</option>
              {/each}
            </select>
          {/if}
        </div>
        <div class="topic-selector">
          <label>
            <input type="radio" bind:group={isNewTopic} value={true} />
            New Topic
          </label>
        </div>
      {/if}
      {#if isNewTopic || existingTopics.length === 0}
        <input
          type="text"
          id="topic"
          bind:value={topicName}
          placeholder="e.g., Python Best Practices"
          required
        />
      {/if}
    </div>

    <div class="form-group">
      <label for="summary">Summary</label>
      <textarea
        id="summary"
        bind:value={summary}
        placeholder="Brief description of this knowledge"
        rows="2"
        required
      />
    </div>

    <div class="form-group">
      <label for="content">Content</label>
      <textarea
        id="content"
        bind:value={messageContent}
        rows="6"
        required
      />
    </div>

    <div class="form-row">
      <div class="form-group">
        <label for="tags">Tags (comma-separated)</label>
        <input
          type="text"
          id="tags"
          bind:value={tags}
          placeholder="python, best-practices, code-review"
        />
      </div>

      <div class="form-group">
        <label for="mastery">Mastery Level</label>
        <select id="mastery" bind:value={masteryLevel}>
          <option value="beginner">Beginner</option>
          <option value="intermediate">Intermediate</option>
          <option value="advanced">Advanced</option>
        </select>
      </div>
    </div>

    <div class="dialog-footer">
      <button type="button" class="secondary-btn" on:click={handleClose}>
        Cancel
      </button>
      <button type="submit" class="primary-btn" disabled={!canSave || saving}>
        {#if saving}
          Saving...
        {:else if conversationId}
          Propose to Group
        {:else}
          Save
        {/if}
      </button>
    </div>
  </form>
</dialog>

<style>
  dialog {
    border: none;
    border-radius: 8px;
    padding: 0;
    max-width: 600px;
    width: 90%;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
  }

  .dialog-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem 1.5rem;
    border-bottom: 1px solid var(--border-color);
  }

  .dialog-header h2 {
    margin: 0;
    font-size: 1.25rem;
  }

  .close-btn {
    background: none;
    border: none;
    font-size: 2rem;
    cursor: pointer;
    color: var(--text-secondary);
  }

  form {
    padding: 1.5rem;
  }

  .form-group {
    margin-bottom: 1rem;
  }

  .form-row {
    display: grid;
    grid-template-columns: 2fr 1fr;
    gap: 1rem;
  }

  label {
    display: block;
    margin-bottom: 0.5rem;
    font-weight: 500;
  }

  input[type="text"],
  select,
  textarea {
    width: 100%;
    padding: 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    font-family: inherit;
  }

  textarea {
    resize: vertical;
  }

  .topic-selector {
    margin-bottom: 0.5rem;
  }

  .topic-selector label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .dialog-footer {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border-color);
  }

  button {
    padding: 0.5rem 1rem;
    border-radius: 4px;
    cursor: pointer;
    font-weight: 500;
  }

  .primary-btn {
    background-color: var(--primary-color);
    color: white;
    border: none;
  }

  .primary-btn:hover:not(:disabled) {
    opacity: 0.9;
  }

  .primary-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .secondary-btn {
    background-color: transparent;
    border: 1px solid var(--border-color);
  }

  .secondary-btn:hover {
    background-color: var(--hover-color);
  }
</style>
```

### 2. Add Button to Chat Interface

**File:** `dpc-client/ui/src/routes/+page.svelte`

```svelte
<!-- Add to message display area -->
<div class="message" class:user={msg.role === 'user'} class:assistant={msg.role === 'assistant'}>
  <div class="message-content">
    {@html formatMessage(msg.content)}
  </div>

  <!-- New: Save as Knowledge button -->
  {#if msg.role === 'user' || msg.role === 'assistant'}
    <button
      class="save-knowledge-btn"
      on:click={() => openManualKnowledgeDialog(msg.content)}
      title="Save as knowledge"
    >
      💾
    </button>
  {/if}
</div>

<!-- Add dialog -->
{#if showManualKnowledgeDialog}
  <ManualKnowledgeDialog
    messageContent={selectedMessageContent}
    conversationId={activeChatId}
    existingTopics={existingTopicNames}
    on:saved={handleKnowledgeSaved}
    on:error={handleKnowledgeError}
    on:close={() => showManualKnowledgeDialog = false}
  />
{/if}

<script>
  import ManualKnowledgeDialog from '$lib/components/ManualKnowledgeDialog.svelte';

  let showManualKnowledgeDialog = false;
  let selectedMessageContent = '';
  let existingTopicNames: string[] = [];

  function openManualKnowledgeDialog(content: string) {
    selectedMessageContent = content;
    showManualKnowledgeDialog = true;

    // Load existing topic names from personal context
    sendCommand('get_personal_context', {}).then(result => {
      if (result.status === 'success') {
        existingTopicNames = Object.keys(result.context.knowledge);
      }
    });
  }

  function handleKnowledgeSaved(event) {
    const { result } = event.detail;
    showManualKnowledgeDialog = false;

    if (result.status === 'success') {
      addToastNotification(`Knowledge saved to "${result.commit_id}"`, 'success');
    } else if (result.status === 'proposed') {
      addToastNotification('Knowledge proposal sent to participants', 'info');
    }
  }

  function handleKnowledgeError(event) {
    const { message } = event.detail;
    addToastNotification(`Error: ${message}`, 'error');
  }
</script>

<style>
  .save-knowledge-btn {
    opacity: 0;
    position: absolute;
    right: 0.5rem;
    top: 0.5rem;
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 4px;
    padding: 0.25rem 0.5rem;
    cursor: pointer;
    transition: opacity 0.2s;
  }

  .message:hover .save-knowledge-btn {
    opacity: 1;
  }

  .save-knowledge-btn:hover {
    background: var(--hover-color);
  }
</style>
```

---

## Consensus Integration

The feature uses the existing ConsensusManager for group approval:

1. **Proposal Phase**: `consensus_manager.propose_commit()` sends proposal to all participants
2. **Voting Phase**: Participants vote via existing voting UI
3. **Apply Phase**: Once consensus reached, `_apply_commit()` saves to all participants

No changes needed to ConsensusManager - it already handles the workflow.

---

## Testing Plan

### Solo Mode
1. Send a message in chat
2. Click "💾 Save as Knowledge"
3. Fill out form, click "Save"
4. Verify knowledge appears in personal.json
5. Verify markdown file created in ~/.dpc/knowledge/
6. Verify toast notification shown

### Collaborative Mode
1. Connect two clients (Alice & Bob)
2. Alice sends message in group chat
3. Alice clicks "💾 Propose as Knowledge"
4. Bob sees proposal notification
5. Bob votes "Approve"
6. Both see "✅ Knowledge Added" notification
7. Verify both have identical knowledge + markdown files
8. Verify commit has signatures from both

---

## Implementation Checklist

- [ ] Add `create_manual_knowledge_commit()` to service.py
- [ ] Register command in local_api.py
- [ ] Create ManualKnowledgeDialog.svelte component
- [ ] Add save button to message display
- [ ] Test solo mode workflow
- [ ] Test collaborative mode workflow
- [ ] Update CHANGELOG.md
- [ ] Update user documentation

---

**End of Implementation Plan**
