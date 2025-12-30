# Frontend Refactoring Testing Checklist

**Date**: 2024-12-30
**Refactoring**: Extracted 5 components from +page.svelte (4,608 → 2,742 lines)

## Component Extraction Summary

| Component | Lines | Status |
|-----------|-------|--------|
| ChatPanel.svelte | 159 | ✓ Extracted |
| SessionControls.svelte | 166 | ✓ Extracted |
| FileTransferUI.svelte | 295 | ✓ Extracted |
| ProviderSelector.svelte | 205 | ✓ Extracted |
| Sidebar.svelte | 1,108 | ✓ Extracted |
| **Total Reduction** | **1,866 lines (40%)** | ✓ Complete |

## Pre-Test Verification

- [x] TypeScript check passes (0 errors, 0 warnings)
- [x] Build completes successfully
- [x] Backend service running (ports 8888, 9999)
- [x] Frontend dev server running (port 1420)
- [x] All component files present

## Test Categories

### 1. Sidebar Component Tests

#### Connection Status
- [ ] Connection status badge displays correctly (connected/connecting/disconnected)
- [ ] Reconnect button appears when disconnected
- [ ] Reconnect button triggers reconnection

#### Node Information
- [ ] Node ID displays correctly
- [ ] Copy node ID button works
- [ ] DPC URIs section displays local network addresses
- [ ] DPC URIs section displays external addresses (if available)
- [ ] Hub status shows correct connection state

#### Authentication (Hub Mode)
- [ ] Google login button visible when Hub disconnected
- [ ] GitHub login button visible when Hub disconnected
- [ ] OAuth login flow works for both providers
- [ ] Login buttons hidden when Hub connected

#### Peer Discovery
- [ ] Cached peers list displays correctly
- [ ] Online peers from Hub display with names
- [ ] Peer count badges show correct numbers

#### Personal Context Actions
- [ ] "View Personal Context" button opens context viewer
- [ ] "Edit Instructions" button opens instructions editor
- [ ] "Firewall Rules" button opens firewall editor
- [ ] "AI Providers" button opens providers editor

#### Auto-Knowledge Detection
- [ ] Toggle switch displays current state
- [ ] Clicking toggle updates state
- [ ] State persists across page reloads

#### Connect to Peer
- [ ] Peer input field accepts text
- [ ] Connection methods help text displays
- [ ] Connecting to peer via DPC URI works
- [ ] Connecting to peer via Node ID works

#### Chat List
- [ ] AI chats display with correct icons
- [ ] P2P peer chats display with correct icons
- [ ] Active chat highlighted correctly
- [ ] Unread message badges display on inactive chats
- [ ] Clicking chat switches active conversation
- [ ] "+" button adds new AI chat
- [ ] Delete button (×) removes AI chats
- [ ] Disconnect button appears for P2P chats

### 2. ProviderSelector Component Tests

#### AI Host Selection
- [ ] "Local" option always available
- [ ] Remote peers appear in dropdown when connected
- [ ] Peer names display correctly (name | node_id)
- [ ] Selecting remote peer updates provider lists

#### Text Provider Selection
- [ ] Local providers display with "(local)" suffix
- [ ] Remote providers display with "(remote)" suffix when remote host selected
- [ ] Provider selection persists when switching chats
- [ ] Default provider auto-selected on first load

#### Vision Provider Selection
- [ ] Only vision-capable providers appear
- [ ] Local vision providers display correctly
- [ ] Remote vision providers display when available
- [ ] Vision provider selection persists

### 3. SessionControls Component Tests

#### Token Counter (AI Chats Only)
- [ ] Token counter displays for AI chats
- [ ] Token counter shows used/limit correctly
- [ ] Percentage displays correctly
- [ ] Warning color appears at 80%+ usage
- [ ] Counter hidden for P2P chats

#### Session Actions
- [ ] "New Session" button creates new chat session
- [ ] "New Session" clears conversation history
- [ ] "End Session & Save Knowledge" button appears
- [ ] End session disabled when peer offline (P2P only)
- [ ] End session tooltip explains requirement
- [ ] End session triggers knowledge extraction

#### Markdown Toggle (AI Chats Only)
- [ ] Markdown toggle button displays for AI chats
- [ ] Button shows "Markdown" when enabled
- [ ] Button shows "Text" when disabled
- [ ] Toggle switches rendering mode
- [ ] State persists across sessions

### 4. ChatPanel Component Tests

#### Message Display
- [ ] User messages display with correct styling
- [ ] Assistant messages display with correct styling
- [ ] Message timestamps display correctly
- [ ] Sender names display for P2P chats
- [ ] Message bubbles align correctly (user right, assistant left)

#### Message Rendering
- [ ] Markdown rendering works when enabled
- [ ] Code blocks render with syntax highlighting
- [ ] Links are clickable
- [ ] Plain text displays when markdown disabled

#### Attachments
- [ ] Image attachments display inline
- [ ] File attachments show with download button
- [ ] File metadata displays (name, size)
- [ ] Clicking images opens full-size view

#### Scroll Behavior
- [ ] Auto-scroll to bottom on new message
- [ ] Manual scroll preserves position
- [ ] Scroll-to-bottom button appears when scrolled up

### 5. FileTransferUI Component Tests

#### Image Preview (Clipboard Paste)
- [ ] Pasting image creates preview chip
- [ ] Preview thumbnail displays correctly
- [ ] Preview shows filename and size
- [ ] Remove button (×) clears preview
- [ ] Preview persists until sent or cleared

#### File Offer Dialog (Incoming)
- [ ] Dialog appears for incoming file offers
- [ ] File details display correctly (name, size, sender)
- [ ] "Accept" button starts download
- [ ] "Reject" button declines transfer
- [ ] Dialog closes after action

#### Send File Confirmation Dialog
- [ ] Dialog appears when sending file
- [ ] File details display (name, recipient)
- [ ] File size displays during preparation
- [ ] Preparation progress bar animates
- [ ] Phase labels update (hashing → chunks)
- [ ] "Send" button disabled during prep
- [ ] "Cancel" button aborts send

#### Active Transfers Panel
- [ ] Panel appears in bottom-right when transfers active
- [ ] Transfer items show filename and direction (↑/↓)
- [ ] Progress bars animate correctly
- [ ] Percentage displays accurately
- [ ] Cancel button (×) aborts transfer
- [ ] Panel disappears when all transfers complete

#### File Transfer Toast
- [ ] Toast notifications appear for file events
- [ ] Toast auto-dismisses after 5 seconds
- [ ] Toast manually dismissible
- [ ] Toast message content is clear

### 6. Main Page Coordinator Tests

#### State Management
- [ ] Active chat selection propagates to all components
- [ ] Provider selection updates across components
- [ ] Connection status updates all affected UI
- [ ] Unread counts update in sidebar

#### Event Delegation
- [ ] Sidebar events trigger correct main page handlers
- [ ] Component events properly bubble up
- [ ] State changes propagate down to components

#### WebSocket Connection
- [ ] WebSocket connects on page load
- [ ] Connection status updates in real-time
- [ ] Backend events update UI correctly
- [ ] Reconnection works after disconnect

### 7. Context Settings Panel Tests

#### Personal Context Toggle
- [ ] Checkbox displays current state
- [ ] Clicking checkbox updates state
- [ ] "Updated" badge appears when context changed
- [ ] Warning appears when unchecked

#### AI Scope Selector
- [ ] Dropdown appears when context enabled
- [ ] Available scopes load from backend
- [ ] "Full Access" option always available
- [ ] Selection updates prompt context
- [ ] Hint text updates based on selection

#### Instruction Set Selector
- [ ] Dropdown appears for AI chats with context enabled
- [ ] Instruction sets load from backend
- [ ] Default set marked with ⭐
- [ ] "None" option available
- [ ] Selection applies to AI behavior

#### Peer Context Selector
- [ ] Section appears when peers connected
- [ ] Checkboxes display for each peer
- [ ] Peer display names show correctly
- [ ] "Updated" badges appear for changed contexts
- [ ] Selection count updates
- [ ] Multiple peers can be selected

### 8. Message Input Tests

#### Textarea Behavior
- [ ] Textarea accepts input
- [ ] Enter key sends message (without Shift)
- [ ] Shift+Enter creates new line
- [ ] Textarea expands with content
- [ ] Placeholder text updates based on state

#### Paste Detection
- [ ] Pasting image creates preview
- [ ] Pasting text inserts normally
- [ ] Multiple paste operations work

#### Send Button
- [ ] Enabled when connected with text input
- [ ] Enabled when image preview present (even without text)
- [ ] Disabled when disconnected
- [ ] Disabled when loading
- [ ] Disabled when context window full
- [ ] Disabled when peer offline (P2P only)
- [ ] Button text changes to "Sending..." during send

#### File Attachment Button
- [ ] Button opens file picker
- [ ] Only enabled for P2P chats
- [ ] Disabled for AI chats
- [ ] Disabled when peer offline
- [ ] Tooltip explains P2P-only limitation

### 9. Styling & Responsiveness Tests

#### Component Styling
- [ ] All components use scoped CSS correctly
- [ ] No style bleeding between components
- [ ] Dark mode works (if implemented)
- [ ] Colors and fonts consistent

#### Layout
- [ ] Grid layout works correctly
- [ ] Sidebar width appropriate
- [ ] Chat panel fills available space
- [ ] Components stack properly on small screens

#### Responsive Design
- [ ] Components adapt to window resize
- [ ] Text wraps appropriately
- [ ] Buttons remain accessible
- [ ] No horizontal scrolling

### 10. Advanced Feature Tests

#### Knowledge Commit Workflow
- [ ] "End Session" triggers knowledge detection
- [ ] Knowledge commit dialog appears
- [ ] Voting works for multi-party commits
- [ ] Devil's advocate mechanism functions
- [ ] Commits save to personal.json

#### Firewall Rules
- [ ] Firewall editor opens from sidebar
- [ ] Rules can be edited
- [ ] Changes apply immediately
- [ ] UI reloads affected data (scopes dropdown)

#### Remote Inference (if peer supports)
- [ ] Remote provider selection works
- [ ] Remote queries execute correctly
- [ ] Responses display properly
- [ ] Token counting works

#### File Transfer (P2P)
- [ ] Sending files works
- [ ] Receiving files works
- [ ] Chunked transfer for large files
- [ ] Hash verification succeeds
- [ ] Cancel works at any stage
- [ ] Progress tracking accurate

## Regression Checklist

### No Breaking Changes
- [ ] All existing features still work
- [ ] No console errors in browser
- [ ] No TypeScript errors
- [ ] No network errors in Network tab
- [ ] WebSocket connection stable

### Performance
- [ ] Page load time acceptable
- [ ] No noticeable lag when switching chats
- [ ] Message rendering smooth
- [ ] File transfers don't block UI

### Data Integrity
- [ ] Conversation history preserved
- [ ] Context data intact
- [ ] Firewall rules unchanged
- [ ] Provider configs working

## Testing Notes

**Tested By**: _______________
**Date**: _______________
**Browser**: _______________
**Version**: _______________

**Issues Found**:

**Notes**:
