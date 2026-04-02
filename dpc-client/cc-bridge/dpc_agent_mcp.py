#!/usr/bin/env python3
"""
DPC Agent Chat MCP Server

Bridges DPC Messenger agent chat <-> Claude Code session so the real CC
can participate in @CC-triggered conversations with Ark and Mike.

Architecture:
  - Subscribes to ws://localhost:9999 (DPC local API)
  - Queues incoming cc_agent_mention events in a ring buffer
  - Exposes tools to Claude Code:
      dpc_read_agent_mentions()    — drain pending @CC mentions from agent chat
      dpc_send_agent_response()    — inject CC's response into agent conversation
      dpc_read_agent_history()     — read recent conversation history for context

Usage:
  python dpc_agent_mcp.py

Registration (add to ~/.claude/settings.json):
  {
    "mcpServers": {
      "dpc-agent-chat": {
        "command": "poetry",
        "args": ["run", "python", "C:/Users/mike/Documents/dpc-messenger/dpc-client/cc-bridge/dpc_agent_mcp.py"],
        "cwd": "C:/Users/mike/Documents/dpc-messenger/dpc-client/core"
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
log = logging.getLogger("dpc-agent-mcp")

DPC_WS = "ws://localhost:9999"

# Ring buffer: holds up to 50 unread @CC mentions from agent chat
_pending: deque = deque(maxlen=50)

# Live WebSocket connection (set by _ws_listener)
_ws_conn = None

app = Server("dpc-agent-chat")


async def _ws_listener() -> None:
    """Background task: connect to DPC WebSocket and queue cc_agent_mention events."""
    global _ws_conn
    while True:
        try:
            async with websockets.connect(DPC_WS) as ws:
                _ws_conn = ws
                log.warning("Connected to DPC WebSocket at %s", DPC_WS)
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        if msg.get("event") == "cc_agent_mention":
                            _pending.append(msg.get("payload", msg))
                            log.warning(
                                "Queued @CC mention from %s in %s",
                                msg.get("payload", {}).get("trigger_sender", "?"),
                                msg.get("payload", {}).get("conversation_id", "?"),
                            )
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            log.warning("WebSocket disconnected (%s), retrying in 3s...", e)
            _ws_conn = None
            await asyncio.sleep(3)


async def _ws_command(command: str, payload: dict, timeout: float = 10.0) -> dict:
    """Send a command to DPC backend and wait for response."""
    if _ws_conn is None:
        return {"status": "error", "message": "Not connected to DPC WebSocket."}
    try:
        cmd_id = f"cc-mcp-{id(payload)}"
        await _ws_conn.send(json.dumps({
            "id": cmd_id,
            "command": command,
            "payload": payload,
        }))
        # Read responses until we find ours (skip broadcast events)
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            raw = await asyncio.wait_for(
                _ws_conn.recv(),
                timeout=deadline - asyncio.get_event_loop().time(),
            )
            msg = json.loads(raw)
            # Skip broadcast events, wait for our command response
            if msg.get("id") == cmd_id:
                return msg.get("payload", msg)
            # Queue any cc_agent_mention events that arrive while waiting
            if msg.get("event") == "cc_agent_mention":
                _pending.append(msg.get("payload", msg))
        return {"status": "error", "message": "Timeout waiting for response."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="dpc_read_agent_mentions",
            description=(
                "Read pending @CC mentions from DPC agent chat. "
                "Returns unread messages where someone mentioned @CC, then clears the queue. "
                "Includes recent conversation history for context. "
                "Call this to check if Ark or Mike has mentioned @CC in agent chat."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="dpc_send_agent_response",
            description=(
                "Send CC's response to a DPC agent conversation. "
                "The message appears as 'CC' in the agent chat alongside Ark and Mike. "
                "If your response contains @Ark, Ark will be triggered to respond."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "string",
                        "description": "Agent conversation ID (e.g. 'agent_001')",
                    },
                    "text": {
                        "type": "string",
                        "description": "CC's response text (supports markdown)",
                    },
                },
                "required": ["conversation_id", "text"],
            },
        ),
        Tool(
            name="dpc_read_agent_history",
            description=(
                "Read recent conversation history from a DPC agent chat. "
                "Returns the last N messages with sender names and timestamps. "
                "Use this for additional context before responding."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "string",
                        "description": "Agent conversation ID (e.g. 'agent_001')",
                    },
                    "last_n": {
                        "type": "integer",
                        "description": "Number of recent messages to return (default: 20)",
                        "default": 20,
                    },
                },
                "required": ["conversation_id"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "dpc_read_agent_mentions":
        messages = list(_pending)
        _pending.clear()
        if not messages:
            return [TextContent(type="text", text="No pending @CC mentions in agent chat.")]

        parts = []
        for m in messages:
            conv_id = m.get("conversation_id", "?")
            sender = m.get("trigger_sender", "?")
            text = m.get("trigger_text", "")
            parts.append(f"[{sender} in {conv_id}]: {text}")

            # Include recent history if provided
            history = m.get("recent_history", [])
            if history:
                parts.append("  Recent history:")
                for h in history[-10:]:
                    name_ = h.get("sender_name", h.get("role", "?"))
                    content = h.get("content", "")[:200]
                    parts.append(f"    {name_}: {content}")

        return [TextContent(type="text", text="\n".join(parts))]

    if name == "dpc_send_agent_response":
        conv_id = arguments.get("conversation_id", "")
        text = arguments.get("text", "")
        if not conv_id or not text:
            return [TextContent(type="text", text="Error: conversation_id and text required.")]

        if _ws_conn is None:
            return [TextContent(type="text", text="Error: Not connected to DPC WebSocket.")]

        try:
            await _ws_conn.send(json.dumps({
                "id": "cc-agent-response",
                "command": "send_cc_agent_response",
                "payload": {
                    "conversation_id": conv_id,
                    "text": text,
                },
            }))
            # Wait for response
            deadline = asyncio.get_event_loop().time() + 10.0
            while asyncio.get_event_loop().time() < deadline:
                raw = await asyncio.wait_for(
                    _ws_conn.recv(),
                    timeout=deadline - asyncio.get_event_loop().time(),
                )
                msg = json.loads(raw)
                if msg.get("id") == "cc-agent-response":
                    status = msg.get("status", msg.get("payload", {}).get("status", "?"))
                    return [TextContent(type="text", text=f"Sent ({status}).")]
                if msg.get("event") == "cc_agent_mention":
                    _pending.append(msg.get("payload", msg))
            return [TextContent(type="text", text="Sent (no ack received).")]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    if name == "dpc_read_agent_history":
        conv_id = arguments.get("conversation_id", "agent_001")
        last_n = arguments.get("last_n", 20)

        result = await _ws_command("get_conversation_history", {
            "conversation_id": conv_id,
        })

        if isinstance(result, dict) and result.get("status") == "error":
            return [TextContent(type="text", text=f"Error: {result.get('message', '?')}")]

        messages = result.get("messages", [])
        if not messages:
            return [TextContent(type="text", text=f"No messages in {conv_id}.")]

        # Return last N messages formatted
        recent = messages[-last_n:]
        parts = [f"=== {conv_id} — last {len(recent)} of {len(messages)} messages ==="]
        for m in recent:
            role = m.get("role", "?")
            sender = m.get("sender_name", role)
            content = m.get("content", "")
            ts = m.get("timestamp", "")[:19]
            parts.append(f"[{ts}] {sender} ({role}): {content}")
        return [TextContent(type="text", text="\n".join(parts))]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main() -> None:
    asyncio.create_task(_ws_listener())
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
