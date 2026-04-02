#!/usr/bin/env python3
"""
DPC Group Chat MCP Server

Bridges DPC Messenger group chat <-> Claude Code session so CC can
participate live in @mention-triggered group conversations.

Architecture:
  - Background thread connects to ws://localhost:9999 (DPC local API)
  - Queues incoming cc_group_mention events in a thread-safe deque
  - Exposes two tools to Claude Code:
      dpc_read_group_messages()  — drain the queue and return pending @CC messages
      dpc_send_group_message()   — call send_group_agent_message on the DPC backend
"""

import asyncio
import json
import logging
import threading
import time
from collections import deque

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("dpc-group-mcp")

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

app = Server("dpc-group-chat")


def _ws_thread_fn() -> None:
    """Background thread: run a separate event loop for WebSocket listener."""
    import asyncio

    async def _listen():
        import websockets
        while True:
            try:
                async with websockets.connect(DPC_WS) as ws:
                    log.warning("Connected to DPC WebSocket at %s", DPC_WS)
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
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
                await asyncio.sleep(3)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_listen())


def _send_ws_command(command: str, payload: dict, timeout: float = 10.0) -> dict:
    """Send a command via a short-lived WebSocket connection (thread-safe)."""
    import websockets

    async def _do():
        async with websockets.connect(DPC_WS) as ws:
            cmd_id = f"cc-mcp-{int(time.time()*1000)}"
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
                if msg.get("event") == "cc_group_mention":
                    _pending.append(msg.get("payload", msg))
            return {"status": "error", "message": "Timeout"}

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_do())
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        loop.close()


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

        result = await asyncio.get_event_loop().run_in_executor(
            None, _send_ws_command, "send_group_agent_message",
            {"group_id": group_id, "agent_name": "CC", "text": text},
        )
        status = result.get("status", "?")
        return [TextContent(type="text", text=f"Sent ({status}).")]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main() -> None:
    # Start WebSocket listener in a daemon thread (separate event loop)
    t = threading.Thread(target=_ws_thread_fn, daemon=True)
    t.start()

    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
