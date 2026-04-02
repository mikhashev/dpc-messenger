#!/usr/bin/env python3
"""
DPC Group Chat MCP Server

Bridges DPC Messenger group chat <-> Claude Code session so CC can
participate live in @mention-triggered group conversations.

Architecture:
  - Subscribes to ws://localhost:9999 (DPC local API)
  - Queues incoming cc_group_mention events in a ring buffer
  - Exposes two tools to Claude Code:
      dpc_read_group_messages()  — drain the queue and return pending @CC messages
      dpc_send_group_message()   — call send_group_agent_message on the DPC backend

Usage:
  python dpc_group_mcp.py

Registration (add to ~/.claude/settings.json):
  {
    "mcpServers": {
      "dpc-group-chat": {
        "command": "python",
        "args": ["C:/Users/mike/Documents/dpc-messenger/dpc-client/cc-bridge/dpc_group_mcp.py"]
      }
    }
  }
"""

import asyncio
import json
import logging
from collections import deque

import websockets
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("dpc-group-mcp")

DPC_WS = "ws://localhost:9999"

# Ring buffer: holds up to 50 unread @CC mentions
_pending: deque = deque(maxlen=50)

# Live WebSocket connection (set by _ws_listener, used by dpc_send_group_message)
_ws_conn = None

app = Server("dpc-group-chat")


async def _ws_listener() -> None:
    """Background task: connect to DPC WebSocket and queue cc_group_mention events."""
    global _ws_conn
    while True:
        try:
            async with websockets.connect(DPC_WS) as ws:
                _ws_conn = ws
                log.warning("Connected to DPC WebSocket at %s", DPC_WS)
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        # DPC local_api broadcasts events as {"event": "...", "payload": {...}}
                        if msg.get("event") == "cc_group_mention":
                            _pending.append(msg.get("payload", msg))
                            log.warning(
                                "Queued @CC mention from %s in group %s",
                                msg.get("payload", {}).get("sender_name", "?"),
                                msg.get("payload", {}).get("group_id", "?"),
                            )
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            log.warning("WebSocket disconnected (%s), retrying in 3s...", e)
            _ws_conn = None
            await asyncio.sleep(3)


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="dpc_read_group_messages",
            description=(
                "Read pending @CC mentions from the DPC group chat. "
                "Returns all unread messages waiting for CC's response, then clears the queue. "
                "Call this to check if Mike (or Ark) has mentioned @CC in a group."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="dpc_send_group_message",
            description=(
                "Send CC's response to a DPC group chat. "
                "The message appears attributed to 'CC' in the group UI."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "group_id": {
                        "type": "string",
                        "description": "Group ID to send to (from dpc_read_group_messages)",
                    },
                    "text": {
                        "type": "string",
                        "description": "Message text to send",
                    },
                },
                "required": ["group_id", "text"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "dpc_read_group_messages":
        messages = list(_pending)
        _pending.clear()
        if not messages:
            return [TextContent(type="text", text="No pending @CC mentions.")]
        lines = [
            f"[{m.get('sender_name', '?')} in {m.get('group_id', '?')}]: {m.get('text', '')}"
            for m in messages
        ]
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "dpc_send_group_message":
        group_id = arguments.get("group_id", "")
        text = arguments.get("text", "")
        if not group_id or not text:
            return [TextContent(type="text", text="Error: group_id and text are required.")]

        if _ws_conn is None:
            return [TextContent(type="text", text="Error: Not connected to DPC WebSocket.")]

        try:
            await _ws_conn.send(json.dumps({
                "id": "cc-response",
                "command": "send_group_agent_message",
                "payload": {
                    "group_id": group_id,
                    "agent_name": "CC",
                    "text": text,
                },
            }))
            return [TextContent(type="text", text="Sent.")]
        except Exception as e:
            return [TextContent(type="text", text=f"Error sending message: {e}")]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main() -> None:
    # Start WebSocket listener as background task
    asyncio.create_task(_ws_listener())

    # Run MCP server over stdio (Claude Code connects via subprocess)
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
