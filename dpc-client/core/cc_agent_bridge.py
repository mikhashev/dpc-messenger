"""
CC Agent Bridge — unified module for CC to read agent chat and send responses.

Replaces MCP bridge with stateless file + WebSocket approach:
- READ: history.json (always on disk, survives End Session / New Session)
- WRITE: WebSocket to localhost:9999 (send_cc_agent_response command)
- ANALYZE: Ark thinking/tools/behavioral patterns

Usage:
    python cc_agent_bridge.py                       # poll mode (5s interval)
    python cc_agent_bridge.py --once --last 5       # show last 5 messages
    python cc_agent_bridge.py --mentions             # show @CC mentions
    python cc_agent_bridge.py --send "hello"         # send CC response
    python cc_agent_bridge.py --thinking             # show Ark thinking
    python cc_agent_bridge.py --analyze              # Ark behavioral analysis
"""

import json
import os
import sys
import time
import re
import asyncio
import argparse
from pathlib import Path

# Fix Windows console encoding
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DPC_HOME = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc"))
CONFIG_PATH = DPC_HOME / "config.ini"
POLL_INTERVAL = 5


def _read_config():
    """Read config.ini once, return configparser object."""
    import configparser
    config = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        config.read(CONFIG_PATH, encoding="utf-8")
    return config


def _get_default_conversation_id() -> str:
    """Get the first agent ID from ~/.dpc/agents/, fallback to 'agent_001'.

    Note: Uses filesystem scan (sorted) because this runs as a standalone
    script without access to DpcAgentProvider._managers (runtime source).
    Both approaches return the first agent; in practice they match.
    """
    agents_dir = DPC_HOME / "agents"
    if agents_dir.exists():
        for d in sorted(agents_dir.iterdir()):
            if d.is_dir() and (d / "config.json").exists():
                return d.name
    return "agent_001"


def _get_cc_display_name() -> str:
    """Read CC display name from config.ini [agent_chat] section."""
    config = _read_config()
    return config.get("agent_chat", "cc_display_name", fallback="CC")


def _get_ws_url() -> str:
    """Read WebSocket URL from config.ini, fallback to default."""
    config = _read_config()
    port = config.get("api", "port", fallback="9999")
    return f"ws://127.0.0.1:{port}"


def _get_history_path(conversation_id: str = None) -> Path:
    """Get history.json path for a conversation."""
    if conversation_id is None:
        conversation_id = _get_default_conversation_id()
    return DPC_HOME / "conversations" / conversation_id / "history.json"
WRITE_SETTLE_MS = 200  # wait after mtime change before reading (let backend finish writing)

_last_mtime = 0.0
_last_messages = []


# ─────────────────────────────────────────────────────────────
# READ — history.json (with mtime tracking to avoid race conditions)
# ─────────────────────────────────────────────────────────────

def read_history(last_n: int = 0) -> list:
    """Read messages from history.json. Always reads fresh from disk."""
    global _last_mtime, _last_messages
    history_path = _get_history_path()
    if not history_path.exists():
        _last_messages = []
        return []
    try:
        # Always read fresh — no mtime cache (caused stale reads in cron mode)
        with open(history_path, encoding="utf-8") as f:
            data = json.load(f)
        _last_messages = data.get("messages", [])
        _last_mtime = history_path.stat().st_mtime
    except (json.JSONDecodeError, IOError):
        # File being written — return previous if available
        if _last_messages:
            return _last_messages[-last_n:] if last_n else _last_messages
        return []
    return _last_messages[-last_n:] if last_n else _last_messages


def find_mentions(messages: list, since_index: int = 0) -> list:
    """Find @CC mentions after since_index. Returns [(index, msg), ...]."""
    cc_name = _get_cc_display_name()
    cc_lower = cc_name.lower()
    mentions = []
    for i, msg in enumerate(messages):
        if i < since_index:
            continue
        content = msg.get("content", "")
        sender = msg.get("sender_name", "")
        if sender == cc_name:
            continue
        content_lower = content.lower()
        if f"@{cc_lower}" in content_lower or "@сс" in content_lower:
            mentions.append((i, msg))
    return mentions


def get_new_messages(messages: list, last_count: int) -> list:
    """Get messages added since last_count."""
    if len(messages) > last_count:
        return messages[last_count:]
    return []


def check_backend_status() -> dict:
    """Check if DPC backend is running by verifying history.json freshness.
    Does NOT connect to WebSocket (raw TCP connect causes handshake errors in backend logs).
    """
    config = _read_config()
    port = int(config.get("api", "port", fallback="9999"))
    status = {"backend": False, "port": port}
    # Check if history.json exists
    hp = _get_history_path()
    status["history_exists"] = hp.exists()
    if hp.exists():
        age_seconds = time.time() - hp.stat().st_mtime
        status["history_age_seconds"] = round(age_seconds)
    # Check backend by log file freshness (more reliable than history.json
    # which only updates on new messages)
    log_path = DPC_HOME / "logs" / "dpc-client.log"
    if log_path.exists():
        log_age = time.time() - log_path.stat().st_mtime
        status["log_age_seconds"] = round(log_age)
        status["backend"] = log_age < 60  # log updated within last minute = running
    return status


# ─────────────────────────────────────────────────────────────
# WRITE — WebSocket send
# ─────────────────────────────────────────────────────────────

async def send_response(text: str, conversation_id: str = None) -> dict:
    """Send CC response via WebSocket to backend."""
    if conversation_id is None:
        conversation_id = _get_default_conversation_id()
    try:
        import websockets
    except ImportError:
        print("[ERROR] websockets not installed. Run: pip install websockets")
        return {"status": "error", "message": "websockets not installed"}

    import uuid
    command = {
        "id": str(uuid.uuid4())[:8],
        "command": "send_cc_agent_response",
        "payload": {
            "conversation_id": conversation_id,
            "text": text,
        }
    }

    try:
        async with websockets.connect(_get_ws_url()) as ws:
            await ws.send(json.dumps(command))
            # Wait for response (with timeout)
            try:
                response = await asyncio.wait_for(ws.recv(), timeout=10)
                result = json.loads(response)
                print(f"[SENT] {len(text)} chars → {conversation_id}: {result.get('status', '?')}")
                return result
            except asyncio.TimeoutError:
                print(f"[SENT] {len(text)} chars → {conversation_id} (no response, timeout)")
                return {"status": "sent", "message": "timeout waiting for response"}
    except ConnectionRefusedError:
        print("[ERROR] Cannot connect to backend (ws://127.0.0.1:9999). Is it running?")
        return {"status": "error", "message": "connection refused"}
    except Exception as e:
        print(f"[ERROR] WebSocket send failed: {e}")
        return {"status": "error", "message": str(e)}


def send_response_sync(text: str, conversation_id: str = None) -> dict:
    """Synchronous wrapper for send_response."""
    return asyncio.run(send_response(text, conversation_id))


# ─────────────────────────────────────────────────────────────
# ANALYZE — Ark behavior
# ─────────────────────────────────────────────────────────────

def extract_thinking(msg: dict) -> str:
    """Extract thinking content from a message."""
    return msg.get("thinking", "")


def extract_tool_calls(msg: dict) -> list:
    """Extract tool calls from streaming_raw output."""
    raw = msg.get("streaming_raw", "")
    if not raw:
        return []
    tools = re.findall(r'(?:calling|using|tool[_\s]call)[:\s]+(\w+)', raw, re.IGNORECASE)
    if not tools:
        known_tools = [
            'search_files', 'grep', 'extended_path_read', 'extended_path_list',
            'repo_read', 'repo_write', 'git_log', 'git_diff', 'git_commit',
            'execute_skill', 'extract_knowledge', 'save_to_memory',
            'web_search', 'web_fetch', 'shell_exec'
        ]
        for tool in known_tools:
            if tool in raw:
                tools.append(tool)
    return tools


def analyze_ark(messages: list, last_n: int = 20) -> dict:
    """Analyze Ark's behavioral patterns. Returns analysis dict."""
    ark_msgs = [
        (i, m) for i, m in enumerate(messages)
        if m.get("sender_name", "").startswith("agent_")
    ][-last_n:]

    if not ark_msgs:
        return {"count": 0}

    total_thinking = 0
    tool_counts = {}
    long_thinking = []
    content_lengths = []

    for i, msg in ark_msgs:
        thinking = extract_thinking(msg)
        if thinking:
            total_thinking += 1
            if len(thinking) > 500:
                long_thinking.append((i, len(thinking)))

        tools = extract_tool_calls(msg)
        for t in tools:
            tool_counts[t] = tool_counts.get(t, 0) + 1

        content_lengths.append(len(msg.get("content", "")))

    avg_len = sum(content_lengths) / len(content_lengths) if content_lengths else 0

    return {
        "count": len(ark_msgs),
        "thinking_count": total_thinking,
        "total_tools": sum(tool_counts.values()),
        "tool_frequency": dict(sorted(tool_counts.items(), key=lambda x: -x[1])),
        "long_thinking": len(long_thinking),
        "avg_response_length": round(avg_len),
    }


# ─────────────────────────────────────────────────────────────
# FORMAT — display helpers
# ─────────────────────────────────────────────────────────────

def format_message(i: int, msg: dict, show_thinking: bool = False, show_tools: bool = False, full: bool = False) -> str:
    """Format a message for display."""
    sender = msg.get("sender_name", "?")
    ts = msg.get("timestamp", "")[:19]
    content = msg.get("content", "")
    if full:
        preview = content.replace("\n", "\n       ")
    else:
        preview = content[:500].replace("\n", " ")
        if len(content) > 500:
            preview += "..."

    line = f"  [{i}] {ts} {sender}: {preview}"

    if show_thinking:
        thinking = extract_thinking(msg)
        if thinking:
            line += f"\n       [THINKING] {thinking[:300].replace(chr(10), ' ')}..."

    if show_tools:
        tools = extract_tool_calls(msg)
        if tools:
            line += f"\n       [TOOLS] {', '.join(tools)}"

    return line


# ─────────────────────────────────────────────────────────────
# POLL — main loop
# ─────────────────────────────────────────────────────────────

def poll(show_thinking: bool = False, show_tools: bool = False):
    """Poll loop: watch for new messages and @CC mentions."""
    messages = read_history()
    last_count = len(messages)
    print(f"[CC Bridge] Started. History: {last_count} msgs. Poll every {POLL_INTERVAL}s.")
    print(f"[CC Bridge] Path: {_get_history_path()}")
    print(f"[CC Bridge] Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(POLL_INTERVAL)
            messages = read_history()
            current_count = len(messages)

            if current_count > last_count:
                new_msgs = messages[last_count:]
                for i, msg in enumerate(new_msgs, last_count):
                    sender = msg.get("sender_name", "?")
                    content = msg.get("content", "")
                    cc_name = _get_cc_display_name()
                    cc_lower = cc_name.lower()
                    content_lower = content.lower()
                    is_mention = (f"@{cc_lower}" in content_lower or "@сс" in content_lower) and sender != cc_name
                    prefix = f">>> @{cc_name} MENTION" if is_mention else "    NEW"
                    print(f"{prefix} {format_message(i, msg, show_thinking, show_tools)}")
                last_count = current_count
            elif current_count < last_count:
                print(f"[CC Bridge] History reset: {last_count} -> {current_count}")
                last_count = current_count
    except KeyboardInterrupt:
        print("\n[CC Bridge] Stopped.")


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CC Agent Bridge")
    parser.add_argument("--once", action="store_true", help="Single check, exit")
    parser.add_argument("--thinking", action="store_true", help="Show Ark thinking")
    parser.add_argument("--tools", action="store_true", help="Show tool calls")
    parser.add_argument("--last", type=int, default=5, help="Last N messages")
    parser.add_argument("--mentions", action="store_true", help="Show @CC mentions")
    parser.add_argument("--analyze", action="store_true", help="Ark behavioral analysis")
    parser.add_argument("--send", type=str, help="Send CC response text")
    parser.add_argument("--status", action="store_true", help="Check backend/frontend status")
    parser.add_argument("--check", type=int, metavar="SINCE", help="Scan full content for @CC mentions since message index")
    parser.add_argument("--full", action="store_true", help="Show full message content without truncation")
    args = parser.parse_args()

    if args.status:
        status = check_backend_status()
        print(f"Backend (port {status['port']}): {'UP' if status['backend'] else 'DOWN'}")
        print(f"History: {'exists' if status.get('history_exists') else 'missing'}", end="")
        if 'history_age_seconds' in status:
            print(f" (last update {status['history_age_seconds']}s ago)")
        else:
            print()
        return

    if args.send:
        send_response_sync(args.send)
        return

    if args.check is not None:
        # Force fresh read — bypass any mtime cache
        global _last_mtime
        _last_mtime = 0.0
        messages = read_history()
        count = len(messages)
        print(f"TOTAL: {count}")
        mentions = find_mentions(messages, since_index=args.check)
        for i, msg in mentions:
            sender = msg.get("sender_name", "?")
            content = msg.get("content", "")
            idx = content.find("@CC")
            if idx < 0:
                idx = content.lower().find("@cc")
            if idx >= 0:
                ctx = content[max(0, idx - 20):idx + 100]
            else:
                ctx = content[:100]
            print(f"MENTION [{i}] {sender}: {ctx}")
        if not mentions:
            print("NO_MENTIONS")
        return

    messages = read_history()
    print(f"[CC Bridge] {len(messages)} messages in history.json\n")

    if args.analyze:
        print("=== Ark Behavioral Analysis ===")
        analysis = analyze_ark(messages)
        for k, v in analysis.items():
            print(f"  {k}: {v}")
        return

    if args.mentions:
        mentions = find_mentions(messages)
        print(f"=== @CC Mentions ({len(mentions)}) ===")
        for i, msg in mentions:
            print(format_message(i, msg, args.thinking, args.tools, args.full))
        return

    if args.once:
        print(f"=== Last {args.last} messages ===")
        for i, msg in enumerate(messages[-args.last:], max(0, len(messages) - args.last)):
            print(format_message(i, msg, args.thinking, args.tools, args.full))
        return

    poll(show_thinking=args.thinking, show_tools=args.tools)


if __name__ == "__main__":
    main()
