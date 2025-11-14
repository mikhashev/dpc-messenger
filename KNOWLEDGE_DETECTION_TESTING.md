# Knowledge Detection Testing Guide

## Phase 4.2 - ConversationMonitor Testing

### How Knowledge Extraction Works

**Two Modes:**
1. **Automatic Detection** (after 5+ messages, score > 0.7)
2. **Manual Extraction** (click "📚 End Session & Save Knowledge" button)

---

## Expected Flow

### 1. During Conversation
**Backend logs you should see:**
```
[Monitor] Feeding messages to local_ai monitor (buffer size before: 0)
[Monitor] Buffer size after: 2, Score: 0.00
[Monitor] No proposal yet (need 5 messages for auto-detect)
```

**After 5+ messages with substantive content:**
```
[Monitor] Buffer size after: 6, Score: 0.85
[Auto-detect] Knowledge proposal generated for local_ai chat
```

**If auto-detection is OFF:**
```
[Monitor] Auto-detection is OFF - messages not being monitored
```

### 2. Click "End Session & Save Knowledge"
**Backend logs:**
```
[End Session] Attempting manual extraction for local_ai
   Buffer: 4 messages
   Current score: 0.45
✓ Knowledge proposal generated for local_ai
   Topic: Python Programming
   Entries: 3
   Confidence: 0.72
```

**Then UI should show:**
- **KnowledgeCommitDialog modal pops up**
- Summary of extracted knowledge
- Confidence scores
- Alternative viewpoints
- Three buttons: Approve / Request Changes / Reject

### 3. After Approval
**Backend broadcasts:**
```
knowledge_commit_approved event
```

**UI automatically:**
- Calls `get_personal_context` to refresh
- Updates "View Personal Context" with new knowledge

---

## Testing Steps

### Test 1: Manual Extraction (Basic)

1. **Start backend:**
   ```bash
   cd dpc-client/core
   poetry run python run_service.py
   ```

2. **Start frontend:**
   ```bash
   cd dpc-client/ui
   npm run tauri dev
   ```

3. **Have a substantive conversation:**
   - Ask local AI: "What are the main principles of object-oriented programming?"
   - Wait for response
   - Ask: "Can you explain encapsulation with a Python example?"
   - Wait for response

4. **Click "📚 End Session & Save Knowledge" button**

5. **Check backend logs for:**
   ```
   [End Session] Attempting manual extraction for local_ai
      Buffer: 4 messages
   ```

6. **If buffer is 0:**
   - Auto-detection might be OFF
   - Check toggle switch in sidebar
   - Or check backend logs for: `[Monitor] Auto-detection is OFF`

### Test 2: Verify Messages Are Being Monitored

1. **Enable auto-detection** (toggle in sidebar should show "✓ AI is monitoring")

2. **Ask a question and watch backend logs:**
   ```
   [Monitor] Feeding messages to local_ai monitor (buffer size before: 0)
   [Monitor] Buffer size after: 2, Score: 0.00
   ```

3. **If you see no monitoring logs:**
   - Check for: `[Monitor] Auto-detection is OFF`
   - Or: `[Monitor] Query failed (status=ERROR)`

### Test 3: Automatic Detection

1. **Have a substantive conversation (5+ meaningful exchanges):**
   - "Explain Python decorators"
   - "Show me an example with @property"
   - "How do class decorators differ from function decorators?"
   - "Can decorators accept arguments?"
   - "What are some common use cases?"

2. **After message #5, watch for:**
   ```
   [Monitor] Buffer size after: 10, Score: 0.XX
   ```

3. **If score > 0.7:**
   ```
   [Auto-detect] Knowledge proposal generated for local_ai chat
   ```
   **Dialog should pop up automatically!**

### Test 4: Proposal Dialog Interaction

**When dialog appears:**

1. **Review the proposal:**
   - Summary of knowledge
   - Confidence scores (high/medium/low badges)
   - Alternative viewpoints
   - Cultural perspectives
   - Devil's advocate critique

2. **Click "Approve":**
   - Dialog closes
   - Backend broadcasts `knowledge_commit_approved`
   - Personal context auto-refreshes

3. **Click "View Personal Context" button:**
   - Should show new knowledge topic
   - Check "Knowledge" tab
   - See new entries with tags

4. **Click "Reject":**
   - Dialog closes
   - Proposal discarded
   - Chat continues normally

5. **Click "Request Changes":**
   - Add comment
   - Dialog closes
   - Can continue discussion and try again

---

## Troubleshooting

### Problem: No dialog appears after clicking "End Session"

**Check backend logs for:**
```
✗ No proposal generated - buffer was empty or no knowledge detected
   Buffer: 0 messages
```

**Causes:**
1. **Auto-detection is OFF** → Enable toggle in sidebar
2. **No messages in buffer** → Messages weren't monitored during conversation
3. **Buffer was cleared** → This happens after each extraction

**Solution:**
- Start a new conversation
- Make sure toggle shows "✓ AI is monitoring"
- Have at least 1-2 message exchanges
- Try "End Session" again

### Problem: "Buffer was empty" even with auto-detection ON

**Check for errors:**
```
Error in local AI conversation monitoring: [error message]
```

**Common causes:**
1. LLM query failed → Check `status=ERROR` in logs
2. Exception during monitoring → Full traceback will be printed

**Solution:**
- Check if AI queries are working (responses appear in UI)
- Look for Python traceback in backend logs
- Restart backend service

### Problem: Auto-detection never triggers

**Even after 10+ messages:**

1. **Check buffer size:**
   ```
   [Monitor] Buffer size after: 20, Score: 0.35
   ```
   Score is below 0.7 threshold

2. **Content might be too casual:**
   - Small talk won't trigger detection
   - Need substantive information, facts, decisions
   - Multiple perspectives discussed
   - Actionable conclusions

3. **Try manual extraction:**
   - Click "End Session" button
   - Forces extraction regardless of score

### Problem: Personal context doesn't update after approval

**Check:**
1. Backend broadcasts `knowledge_commit_approved`?
2. UI console shows: `"Knowledge commit approved:"`?
3. UI called `get_personal_context`?

**Debug:**
- Open browser DevTools (F12)
- Check Console tab
- Look for WebSocket messages

---

## What Success Looks Like

### Complete Happy Path

1. ✅ Toggle shows "✓ AI is monitoring"
2. ✅ Every message logs buffer size increase
3. ✅ Click "End Session" → Dialog appears
4. ✅ Dialog shows knowledge summary
5. ✅ Click "Approve" → Dialog closes
6. ✅ "View Personal Context" → New knowledge visible
7. ✅ Chat continues normally

---

## File Locations for Debugging

**Backend Service:**
- `dpc-client/core/dpc_client_core/service.py` (lines 1218-1268, 745-802)
- `dpc-client/core/dpc_client_core/conversation_monitor.py`

**Frontend UI:**
- `dpc-client/ui/src/routes/+page.svelte` (lines 196-202, 204-209, 520-522)
- `dpc-client/ui/src/lib/coreService.ts` (lines 140-154)
- `dpc-client/ui/src/lib/components/KnowledgeCommitDialog.svelte`

**Logs:**
- Backend: Terminal running `run_service.py`
- Frontend: Browser DevTools → Console

---

## Next Steps After Testing

Once you verify the flow works:

1. Test with peer-to-peer chats (not just local AI)
2. Test rejection and "Request Changes" buttons
3. Test with different types of content (technical vs casual)
4. Test toggle switch (disable mid-conversation)
5. Verify personal context persists after restart

**Report any issues with:**
- Full backend log output
- Frontend console errors
- Screenshots of UI state
