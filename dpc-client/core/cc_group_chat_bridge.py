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
    python cc_group_chat_bridge.py --group GROUP_ID --as CC_mike --send "hi"  # pick a tag

Identity: --as, else the tag registered for this node in Group Settings
(metadata.json agents/agent_names), else [agent_chat] cc_display_name.
"""

import json
import os
import re
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


def _get_node_id() -> str:
    """This node's id from ~/.dpc/node.id, "" if absent."""
    try:
        return (DPC_HOME / "node.id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _registered_tags(group_id: str) -> list:
    """External tags this node registered in the group's metadata.json (display names)."""
    node_id = _get_node_id()
    if not node_id:
        return []
    try:
        with open(_find_group_dir(group_id) / "metadata.json", "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    names = meta.get("agent_names", {}).get(node_id, {}) or {}
    tags = []
    for entry in meta.get("agents", {}).get(node_id, []) or []:
        if isinstance(entry, str) and entry.startswith("ext:"):
            tags.append(names.get(entry) or entry[len("ext:"):])
    return tags


def _identity_names(group_id: str, override=None) -> list:
    """Names this bridge answers to: --as, else registered tags, else cc_display_name."""
    if override:
        return [override]
    return _registered_tags(group_id) or [_get_cc_display_name()]


def _resolve_identity(group_id: str, override=None) -> str:
    """The single name to post under; exits 2 when several tags need --as to pick one."""
    names = _identity_names(group_id, override)
    if len(names) > 1:
        print(f"[ERROR] several tags registered on this node for {group_id}: "
              f"{', '.join(names)} — pass --as")
        sys.exit(2)
    return names[0]


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


def _find_group_dir(group_id: str) -> Path:
    """Find the conversation directory for a group, with or without slug suffix.

    The backend may store group history in either:
      ~/.dpc/conversations/{group_id}/history.json           (no display name)
      ~/.dpc/conversations/{group_id}-{slug}/history.json    (with display name)

    Prefer the slugged directory (backend's active write target).
    """
    base = DPC_HOME / "conversations"
    for d in sorted(base.iterdir()):
        if d.is_dir() and d.name.startswith(group_id + "-") and (d / "history.json").exists():
            return d
    return base / group_id


def read_history(group_id: str, last_n: int = None) -> list:
    """Read group chat history from disk."""
    history_path = _find_group_dir(group_id) / "history.json"
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


def _mention_patterns(names: list) -> list:
    """One whole-word regex per name (@CC_mike, not @CC_mike2); Cyrillic @сс only for CC."""
    patterns = [re.compile(r"(?<!\w)@" + re.escape(n) + r"(?!\w)", re.IGNORECASE) for n in names]
    if any(n.lower() == "cc" for n in names):
        patterns.append(re.compile(r"(?<!\w)@сс(?!\w)", re.IGNORECASE))
    return patterns


def find_mentions(messages: list, since_index: int = 0, names: list = None) -> list:
    """Find @<name> mentions after since_index. Returns [(index, msg), ...].

    names defaults to [cc_display_name]; the CLI passes the resolved identity list.
    """
    if not names:
        names = [_get_cc_display_name()]
    patterns = _mention_patterns(names)
    mentions = []
    for i, msg in enumerate(messages):
        if i < since_index:
            continue
        content = msg.get("content", "") or msg.get("text", "")
        sender = msg.get("sender_name", "")
        if sender in names:
            continue
        if any(p.search(content) for p in patterns):
            mentions.append((i, msg))
    return mentions


def _build_send_command(group_id: str, name: str, text: str) -> dict:
    """The send_group_agent_message command; the backend copies agent_name into sender_name."""
    import uuid
    return {
        "id": str(uuid.uuid4())[:8],
        "command": "send_group_agent_message",
        "payload": {
            "group_id": group_id,
            "agent_name": name,
            "text": text,
        }
    }


async def send_group_message(group_id: str, text: str, name: str = None) -> dict:
    """Send CC response to group chat via WebSocket, as `name` (default: resolved identity)."""
    canonical_id = _resolve_group_id(group_id)

    try:
        import websockets
    except ImportError:
        print("[ERROR] websockets not installed.")
        return {"status": "error", "message": "websockets not installed"}

    if name is None:
        name = _resolve_identity(group_id)
    if name != _get_cc_display_name():
        print(f"[INFO] posting as {name}")
    command = _build_send_command(canonical_id, name, text)

    ws_token_path = DPC_HOME / ".ws_token"
    try:
        auth_token = ws_token_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        print(f"[ERROR] Cannot read auth token at {ws_token_path}: {e}")
        return {"status": "error", "message": "no auth token"}

    try:
        async with websockets.connect(_get_ws_url()) as ws:
            await ws.send(json.dumps({
                "id": "group-bridge-auth",
                "command": "auth",
                "token": auth_token,
            }))
            try:
                auth_resp = await asyncio.wait_for(ws.recv(), timeout=5)
                auth_result = json.loads(auth_resp)
                if auth_result.get("status") != "OK":
                    print(f"[ERROR] Auth rejected: {auth_result}")
                    return {"status": "error", "message": "auth rejected"}
            except asyncio.TimeoutError:
                print("[ERROR] Auth response timeout")
                return {"status": "error", "message": "auth timeout"}

            await ws.send(json.dumps(command))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                result = json.loads(raw)
                print(f"[SENT] {len(text)} chars → group {group_id}: {result.get('status', '?')}")
                return result
            except asyncio.TimeoutError:
                print(f"[SENT] {len(text)} chars → group {group_id} (no response, timeout)")
                return {"status": "sent"}
    except ConnectionRefusedError:
        print("[ERROR] Cannot connect to backend. Is it running?")
        return {"status": "error", "message": "connection refused"}
    except Exception as e:
        print(f"[ERROR] {e}")
        return {"status": "error", "message": str(e)}


def _resolve_group_id(group_id: str) -> str:
    """Resolve canonical group_id from metadata.json.

    The --group argument may be a slugged directory name (e.g. group-abc123-dpc-discord)
    but the backend expects the canonical group_id from metadata.json (e.g. group-abc123).
    """
    group_dir = _find_group_dir(group_id)
    metadata_path = group_dir / "metadata.json"
    if metadata_path.exists():
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            canonical = meta.get("group_id", group_id)
            if canonical != group_id:
                print(f"[INFO] Resolved {group_id} → {canonical}")
            return canonical
        except Exception:
            pass
    return group_id


def send_group_message_sync(group_id: str, text: str, name: str = None) -> dict:
    """Sync wrapper for send_group_message."""
    return asyncio.run(send_group_message(group_id, text, name))


def format_message(i: int, msg: dict) -> str:
    """Format a message for display. Full content, no truncation."""
    sender = msg.get("sender_name", msg.get("sender", "?"))
    content = msg.get("content", "") or msg.get("text", "")
    ts = msg.get("timestamp", "")
    if ts and "T" in ts:
        ts = ts.split("T")[1][:8]
    preview = content.replace("\n", "\n       ")
    return f"  [{i + 1}] {ts} {sender}: {preview}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CC Group Chat Bridge")
    parser.add_argument("--list", action="store_true", help="List available groups")
    parser.add_argument("--group", type=str, help="Group ID to interact with")
    parser.add_argument("--last", type=int, default=10, help="Last N messages")
    parser.add_argument("--mentions", action="store_true", help="Show @CC mentions")
    parser.add_argument("--send", type=str, help="Send CC response text")
    parser.add_argument("--send-file", type=str, dest="send_file",
                        help="Send CC response from file (backtick-safe)")
    parser.add_argument("--as", type=str, dest="as_name", metavar="TAG",
                        help="Post/scan as this tag (default: the tag registered in "
                             "Group Settings, else cc_display_name)")
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
        name = _resolve_identity(args.group, args.as_name)
        send_group_message_sync(args.group, args.send, name)
        sys.exit(0)

    if args.send_file:
        try:
            text = Path(args.send_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"[ERROR] Cannot read --send-file: {e}", file=sys.stderr)
            sys.exit(1)
        name = _resolve_identity(args.group, args.as_name)
        send_group_message_sync(args.group, text, name)
        sys.exit(0)

    messages = read_history(args.group, last_n=args.last)
    print(f"[CC Group Bridge] {len(messages)} messages (last {args.last})\n")

    if args.mentions:
        mentions = find_mentions(messages, names=_identity_names(args.group, args.as_name))
        if not mentions:
            print("No @CC mentions found.")
        else:
            print(f"=== {len(mentions)} @CC mention(s) ===")
            for i, msg in mentions:
                print(format_message(i, msg))
    else:
        for i, msg in enumerate(messages):
            print(format_message(i, msg))
