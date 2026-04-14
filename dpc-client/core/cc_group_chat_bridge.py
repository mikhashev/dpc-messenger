"""
CC Group Chat Bridge — read group chat history and send CC responses.

Replaces dpc_group_mcp.py with stateless file + WebSocket approach:
- READ: history.json from ~/.dpc/conversations/{group_id}/history.json
- WRITE: WebSocket to localhost:9999 (send_group_agent_message command)

Usage:
    python cc_group_chat_bridge.py --list                    # list available groups
    python cc_group_chat_bridge.py --group GROUP_ID --last 5 # show last 5 messages
    python cc_group_chat_bridge.py --group GROUP_ID --send "hello"  # send CC response
    python cc_group_chat_bridge.py --group GROUP_ID --mentions      # show @CC mentions
"""

import json
import os
import sys
import asyncio
import argparse
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DPC_HOME = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc"))
CONFIG_PATH = DPC_HOME / "config.ini"


def _read_config():
    """Read config.ini once, return configparser object."""
    import configparser
    config = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        config.read(CONFIG_PATH, encoding="utf-8")
    return config


def _get_cc_display_name() -> str:
    """Read CC display name from config.ini [agent_chat] section."""
    config = _read_config()
    return config.get("agent_chat", "cc_display_name", fallback="CC")


def _get_ws_url() -> str:
    """Read WebSocket URL from config.ini, fallback to default."""
    config = _read_config()
    port = config.get("api", "port", fallback="9999")
    return f"ws://127.0.0.1:{port}"


def list_groups() -> list:
    """List available group chats from ~/.dpc/conversations/."""
    conversations_dir = DPC_HOME / "conversations"
    if not conversations_dir.exists():
        return []
    groups = []
    for d in sorted(conversations_dir.iterdir()):
        if d.is_dir() and (d / "metadata.json").exists():
            try:
                with open(d / "metadata.json", "r", encoding="utf-8") as f:
                    meta = json.load(f)
                groups.append({
                    "group_id": d.name,
                    "name": meta.get("name", d.name),
                    "members": len(meta.get("members", [])),
                })
            except Exception:
                groups.append({"group_id": d.name, "name": d.name, "members": 0})
    return groups


def read_history(group_id: str, last_n: int = None) -> list:
    """Read group chat history from disk."""
    history_path = DPC_HOME / "conversations" / group_id / "history.json"
    if not history_path.exists():
        return []
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        messages = data.get("messages", data) if isinstance(data, dict) else data
        if not isinstance(messages, list):
            return []
        if last_n:
            return messages[-last_n:]
        return messages
    except (json.JSONDecodeError, IOError):
        return []


def find_mentions(messages: list, since_index: int = 0) -> list:
    """Find @CC mentions after since_index. Returns [(index, msg), ...]."""
    cc_name = _get_cc_display_name()
    cc_lower = cc_name.lower()
    mentions = []
    for i, msg in enumerate(messages):
        if i < since_index:
            continue
        content = msg.get("content", "") or msg.get("text", "")
        sender = msg.get("sender_name", "")
        if sender == cc_name:
            continue
        content_lower = content.lower()
        if f"@{cc_lower}" in content_lower or "@сс" in content_lower:
            mentions.append((i, msg))
    return mentions


async def send_group_message(group_id: str, text: str) -> dict:
    """Send CC response to group chat via WebSocket."""
    try:
        import websockets
    except ImportError:
        print("[ERROR] websockets not installed.")
        return {"status": "error", "message": "websockets not installed"}

    import uuid
    cc_name = _get_cc_display_name()
    command = {
        "id": str(uuid.uuid4())[:8],
        "command": "send_group_agent_message",
        "payload": {
            "group_id": group_id,
            "agent_name": cc_name,
            "text": text,
        }
    }

    try:
        async with websockets.connect(_get_ws_url()) as ws:
            await ws.send(json.dumps(command))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                result = json.loads(raw)
                print(f"[SENT] {len(text)} chars → group {group_id}: {result.get('status', '?')}")
                return result
            except asyncio.TimeoutError:
                print(f"[SENT] {len(text)} chars → group {group_id} (no response, timeout)")
                return {"status": "sent"}
    except Exception as e:
        print(f"[ERROR] {e}")
        return {"status": "error", "message": str(e)}


def send_group_message_sync(group_id: str, text: str) -> dict:
    """Sync wrapper for send_group_message."""
    return asyncio.run(send_group_message(group_id, text))


def format_message(i: int, msg: dict) -> str:
    """Format a message for display."""
    sender = msg.get("sender_name", msg.get("sender", "?"))
    content = msg.get("content", "") or msg.get("text", "")
    ts = msg.get("timestamp", "")
    if ts and "T" in ts:
        ts = ts.split("T")[1][:8]
    return f"  [{i}] {ts} {sender}: {content[:200]}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CC Group Chat Bridge")
    parser.add_argument("--list", action="store_true", help="List available groups")
    parser.add_argument("--group", type=str, help="Group ID to interact with")
    parser.add_argument("--last", type=int, default=10, help="Last N messages")
    parser.add_argument("--mentions", action="store_true", help="Show @CC mentions")
    parser.add_argument("--send", type=str, help="Send CC response text")
    args = parser.parse_args()

    if args.list:
        groups = list_groups()
        if not groups:
            print("No groups found.")
        else:
            print(f"Found {len(groups)} group(s):\n")
            for g in groups:
                print(f"  {g['group_id']} — {g['name']} ({g['members']} members)")
        sys.exit(0)

    if not args.group:
        print("Error: --group GROUP_ID required (use --list to see available groups)")
        sys.exit(1)

    if args.send:
        send_group_message_sync(args.group, args.send)
        sys.exit(0)

    messages = read_history(args.group, last_n=args.last)
    print(f"[CC Group Bridge] {len(messages)} messages (last {args.last})\n")

    if args.mentions:
        mentions = find_mentions(messages)
        if not mentions:
            print("No @CC mentions found.")
        else:
            print(f"=== {len(mentions)} @CC mention(s) ===")
            for i, msg in mentions:
                print(format_message(i, msg))
    else:
        for i, msg in enumerate(messages):
            print(format_message(i, msg))
