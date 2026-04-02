# MCP Bridge Setup — Claude Code Integration (Windows 10)

How to set up the MCP bridge that connects Claude Code (the real CC) to DPC Messenger's agent chat, enabling bidirectional @CC/@Ark dialogue.

## Prerequisites

- **OS**: Windows 10 (build 10.0.19045+)
- **Python**: 3.12+ (installed via `pyproject.toml`)
- **Poetry**: For dependency management
- **DPC Client Backend**: Running on `ws://localhost:9999`
- **Claude Code**: VS Code extension or CLI

## Architecture

```
┌─────────────────┐     stdio (JSON-RPC)     ┌──────────────────┐
│   Claude Code   │◄────────────────────────►│  dpc_agent_mcp   │
│   (VS Code)     │                          │  (MCP Server)    │
└─────────────────┘                          └────────┬─────────┘
                                                      │ WebSocket
                                                      │ ws://localhost:9999
                                                      ▼
                                             ┌──────────────────┐
                                             │  DPC Backend     │
                                             │  (CoreService)   │
                                             └────────┬─────────┘
                                                      │
                                              ┌───────┴───────┐
                                              │  Agent Chat   │
                                              │  (Ark + CC)   │
                                              └───────────────┘
```

**Flow:**
1. User types `@CC hello` in DPC agent chat
2. Backend saves message, broadcasts `cc_agent_mention` event
3. MCP server queues the event (background WebSocket listener thread)
4. Claude Code polls via `dpc_read_agent_mentions` tool
5. Claude Code reads history, formulates response, sends via `dpc_send_agent_response`
6. Backend injects CC response into conversation, broadcasts to UI
7. If CC mentions `@Ark`, backend triggers Ark to respond (chain trigger)

## Setup

### 1. Install Dependencies

```bash
cd dpc-client/core
poetry install
```

The MCP bridge scripts are in `dpc-client/cc-bridge/`:
- `dpc_agent_mcp.py` — Agent chat bridge (@CC mentions)
- `dpc_group_mcp.py` — Group chat bridge (@CC mentions in groups)

### 2. Configure MCP Servers

Create `.mcp.json` in the project root (`dpc-messenger/`):

```json
{
  "mcpServers": {
    "dpc-agent-chat": {
      "command": "C:/Users/<username>/Documents/dpc-messenger/dpc-client/core/.venv/Scripts/python.exe",
      "args": [
        "C:/Users/<username>/Documents/dpc-messenger/dpc-client/cc-bridge/dpc_agent_mcp.py"
      ]
    },
    "dpc-group-chat": {
      "command": "C:/Users/<username>/Documents/dpc-messenger/dpc-client/core/.venv/Scripts/python.exe",
      "args": [
        "C:/Users/<username>/Documents/dpc-messenger/dpc-client/cc-bridge/dpc_group_mcp.py"
      ]
    }
  }
}
```

**Replace `<username>` with your Windows username.**

### 3. Verify MCP Servers

In Claude Code VS Code panel, type `/mcp` to see server status. Both should show as connected.

If they show "Failed", click Reconnect.

### 4. Available Tools

Once connected, Claude Code has these tools:

| Tool | Description |
|------|-------------|
| `dpc_read_agent_mentions` | Read pending @CC mentions (real-time queue + history fallback) |
| `dpc_send_agent_response` | Send CC's response to agent chat |
| `dpc_read_agent_history` | Read recent conversation history |
| `dpc_read_group_messages` | Read pending @CC mentions in group chats |
| `dpc_send_group_message` | Send CC's response to group chat |

## Windows-Specific Issues and Fixes

### 1. Poetry Run Breaks MCP stdio

**Problem**: `poetry run python` corrupts the stdin/stdout pipe that MCP uses for JSON-RPC communication. The MCP server starts but immediately disconnects with `MCP error -32000: Connection closed`.

**Fix**: Use the venv Python executable directly:
```json
"command": "C:/path/to/core/.venv/Scripts/python.exe"
```

**Do NOT use**:
```json
"command": "poetry",
"args": ["run", "python", "..."]
```

### 2. asyncio Event Loop Policy

**Problem**: Windows defaults to `ProactorEventLoop` which is incompatible with the `websockets` library used by the MCP bridge.

**Fix**: Set `WindowsSelectorEventLoopPolicy` before `asyncio.run()`:
```python
import sys
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
asyncio.run(main())
```

### 3. WebSocket in Separate Thread

**Problem**: The MCP server uses asyncio for stdio JSON-RPC transport. Running `websockets.connect()` in the same event loop causes conflicts.

**Fix**: Run the WebSocket listener in a daemon thread with its own event loop:
```python
def _ws_thread_fn():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_listen())

t = threading.Thread(target=_ws_thread_fn, daemon=True)
t.start()
```

Tool calls that need WebSocket use short-lived connections via `run_in_executor`:
```python
result = await asyncio.get_event_loop().run_in_executor(
    None, _ws_command_sync, command, payload
)
```

### 4. Event Timing Gap

**Problem**: `cc_agent_mention` events may fire before the MCP server's background WebSocket listener connects (cold start race condition).

**Fix**: `dpc_read_agent_mentions` falls back to scanning conversation history for unanswered @CC messages when the real-time queue is empty.

## Mention Routing (Pattern D)

| Mention | Behavior |
|---------|----------|
| User `@CC` (no @Ark) | Backend broadcasts event → CC responds via MCP |
| User `@Ark` (no @CC) | Ark responds normally |
| User `@Ark @CC` | Ark responds, CC gets event notification |
| CC says `@Ark` | Backend triggers Ark (chain trigger) |
| Ark says `@CC` | Backend broadcasts event → CC responds |
| Chain depth > 3 | Chain stops (prevents infinite loops) |

Chain depth resets on each new user message.

## Auto-Polling

Claude Code can set up automatic polling using `/loop`:

```
/loop 1m Check for @CC mentions in DPC agent chat using dpc_read_agent_mentions...
```

This creates a cron job that checks every minute. The job is session-only and auto-expires after 7 days.

## Troubleshooting

### MCP server shows "Failed"
1. Check `.mcp.json` path — must use the venv Python, not `poetry run`
2. Verify DPC backend is running: `curl http://localhost:9999` should connect
3. Click Reconnect in `/mcp` panel

### No mentions detected
1. The background listener may not have connected yet — check backend logs for `UI client connected`
2. Use `dpc_read_agent_history` as a manual fallback to see if @CC messages exist
3. Reconnect MCP server to reload the history fallback code

### Streaming indicator stuck on "Generating..."
Backend must emit `agent_progress_clear` after chain-triggered responses. Fixed in commit `e6149d9`.

## Related Files

| File | Purpose |
|------|---------|
| `dpc-client/cc-bridge/dpc_agent_mcp.py` | Agent chat MCP server |
| `dpc-client/cc-bridge/dpc_group_mcp.py` | Group chat MCP server |
| `dpc-client/core/dpc_client_core/service.py` | @CC routing, chain triggers |
| `dpc-client/ui/src/lib/panels/AgentPanel.svelte` | Agent chat message handling |
| `dpc-client/ui/src/lib/panels/MessageRouterPanel.svelte` | CC pending response handling |
| `.mcp.json` | Project-level MCP server config |
