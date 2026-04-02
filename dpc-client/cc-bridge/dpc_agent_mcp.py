#!/usr/bin/env python3
"""
DPC Agent Chat MCP Server

Bridges DPC Messenger agent chat <-> Claude Code session so the real CC
can participate in @CC-triggered conversations with Ark and Mike.

Architecture:
  - Background thread listens to ws://localhost:9999 for cc_agent_mention events
  - Queues mentions in a thread-safe deque
  - Tool calls use short-lived WebSocket connections (separate event loops)
  - Exposes tools to Claude Code:
      dpc_read_agent_mentions()    — drain pending @CC mentions from agent chat
      dpc_send_agent_response()    — inject CC's response into agent conversation
      dpc_read_agent_history()     — read recent conversation history for context
"""

import asyncio
import json
import logging
import sys
import threading
import time
from collections import deque

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("dpc-agent-mcp")

def _get_dpc_ws_url() -> str:
    """Read WebSocket URL from config.ini, fallback to default."""
    import configparser
    from pathlib import Path
    config = configparser.ConfigParser()
    config_path = Path.home() / ".dpc" / "config.ini"
    if config_path.exists():
        config.read(config_path, encoding="utf-8")
    host = config.get("api", "host", fallback="127.0.0.1")
    port = config.get("api", "port", fallback="9999")
    return f"ws://{host}:{port}"

DPC_WS = _get_dpc_ws_url()

# Thread-safe ring buffer: holds up to 50 unread @CC mentions
_pending: deque = deque(maxlen=50)

app = Server("dpc-agent-chat")


# ---------------------------------------------------------------------------
# Background WebSocket listener (runs in a daemon thread with its own loop)
# ---------------------------------------------------------------------------

def _ws_thread_fn() -> None:
    """Connect to DPC WebSocket and queue cc_agent_mention events forever."""
    import websockets

    async def _listen():
        while True:
            try:
                async with websockets.connect(DPC_WS) as ws:
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
                await asyncio.sleep(3)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_listen())
    except Exception as e:
        log.warning("WebSocket thread error: %s", e)


# ---------------------------------------------------------------------------
# Sync helpers for tool calls (each creates a short-lived connection)
# ---------------------------------------------------------------------------

def _ws_command_sync(command: str, payload: dict, timeout: float = 10.0) -> dict:
    """Send a command via a short-lived WebSocket connection."""
    import websockets

    async def _do():
        async with websockets.connect(DPC_WS) as ws:
            cmd_id = f"cc-mcp-{int(time.time() * 1000)}"
            await ws.send(json.dumps({
                "id": cmd_id,
                "command": command,
                "payload": payload,
            }))
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                raw = await asyncio.wait_for(
                    ws.recv(),
                    timeout=deadline - asyncio.get_event_loop().time(),
                )
                msg = json.loads(raw)
                if msg.get("id") == cmd_id:
                    return msg.get("payload", msg)
                if msg.get("event") == "cc_agent_mention":
                    _pending.append(msg.get("payload", msg))
            return {"status": "error", "message": "Timeout"}

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_do())
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="dpc_read_agent_mentions",
            description=(
                "Read pending @CC mentions from DPC agent chat. "
                "Returns unread messages where someone mentioned @CC, then clears the queue. "
                "The background listener auto-queues mentions in real time. "
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
                    "thinking_limit": {
                        "type": "integer",
                        "description": "Max chars for thinking/raw output per message (default: 500, 0 = no limit)",
                        "default": 500,
                    },
                },
                "required": ["conversation_id"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "dpc_read_agent_mentions":
        # First check the real-time queue (background listener)
        messages = list(_pending)
        _pending.clear()

        if messages:
            parts = []
            for m in messages:
                conv_id = m.get("conversation_id", "?")
                sender = m.get("trigger_sender", "?")
                text = m.get("trigger_text", "")
                parts.append(f"[{sender} in {conv_id}]: {text}")
                history = m.get("recent_history", [])
                if history:
                    parts.append("  Recent history:")
                    for h in history[-10:]:
                        n = h.get("sender_name", h.get("role", "?"))
                        c = h.get("content", "")[:200]
                        parts.append(f"    {n}: {c}")
            return [TextContent(type="text", text="\n".join(parts))]

        # Fallback: scan conversation history for unanswered @CC mentions
        # (catches events that fired before the listener connected)
        result = await asyncio.get_event_loop().run_in_executor(
            None, _ws_command_sync, "get_conversation_history",
            {"conversation_id": "agent_001"},
        )
        hist_messages = result.get("messages", [])
        if not hist_messages:
            return [TextContent(type="text", text="No pending @CC mentions in agent chat.")]

        # Find unanswered @CC mentions: any @CC message after the last CC response
        last_cc_idx = -1
        for i, m in enumerate(hist_messages):
            if m.get("sender_node_id") == "cc":
                last_cc_idx = i

        unanswered = []
        for m in hist_messages[last_cc_idx + 1:]:
            content = m.get("content", "")
            if "@CC" in content or "@cc" in content:
                unanswered.append(m)

        if not unanswered:
            return [TextContent(type="text", text="No pending @CC mentions in agent chat.")]

        parts = []
        for m in unanswered:
            sender = m.get("sender_name", "?")
            text = m.get("content", "")
            parts.append(f"[{sender} in agent_001]: {text}")
        return [TextContent(type="text", text="\n".join(parts))]

    if name == "dpc_send_agent_response":
        conv_id = arguments.get("conversation_id", "")
        text = arguments.get("text", "")
        if not conv_id or not text:
            return [TextContent(type="text", text="Error: conversation_id and text required.")]

        result = await asyncio.get_event_loop().run_in_executor(
            None, _ws_command_sync, "send_cc_agent_response",
            {"conversation_id": conv_id, "text": text},
        )
        status = result.get("status", "?")
        return [TextContent(type="text", text=f"Sent ({status}).")]

    if name == "dpc_read_agent_history":
        conv_id = arguments.get("conversation_id", "agent_001")
        last_n = arguments.get("last_n", 20)
        thinking_limit = arguments.get("thinking_limit", 500)

        result = await asyncio.get_event_loop().run_in_executor(
            None, _ws_command_sync, "get_conversation_history",
            {"conversation_id": conv_id},
        )

        if isinstance(result, dict) and result.get("status") == "error":
            return [TextContent(type="text", text=f"Error: {result.get('message', '?')}")]

        messages = result.get("messages", [])
        if not messages:
            return [TextContent(type="text", text=f"No messages in {conv_id}.")]

        recent = messages[-last_n:]
        parts = [f"=== {conv_id} — last {len(recent)} of {len(messages)} messages ==="]
        for m in recent:
            role = m.get("role", "?")
            sender = m.get("sender_name", role)
            content = m.get("content", "")
            ts = m.get("timestamp", "")[:19]
            thinking = m.get("thinking", "")
            streaming_raw = m.get("streaming_raw", "")
            parts.append(f"[{ts}] {sender} ({role}): {content}")
            if thinking:
                t = thinking if thinking_limit == 0 else thinking[:thinking_limit]
                parts.append(f"  [Thinking]: {t}")
            if streaming_raw:
                r = streaming_raw if thinking_limit == 0 else streaming_raw[:thinking_limit]
                parts.append(f"  [Raw output]: {r}")
        return [TextContent(type="text", text="\n".join(parts))]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    # Start WebSocket listener in daemon thread (won't block MCP shutdown)
    t = threading.Thread(target=_ws_thread_fn, daemon=True)
    t.start()

    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
