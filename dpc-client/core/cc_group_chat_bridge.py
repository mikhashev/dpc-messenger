"""
Group chat bridge — an external agent (Claude Code or any harness) reads a DPC
group chat and posts under the tag it registered. File name is historical.

- READ: history.json from ~/.dpc/conversations/{group_id}/history.json
- WRITE: WebSocket to localhost:9999 (send_group_agent_message command)

Usage:
    python cc_group_chat_bridge.py --list                    # list available groups
    python cc_group_chat_bridge.py --group GROUP_ID --last 5 # show last 5 messages
    python cc_group_chat_bridge.py --group GROUP_ID --send "hello"  # post as this bridge's tag
    python cc_group_chat_bridge.py --group GROUP_ID --mentions      # mentions of that tag
    python cc_group_chat_bridge.py --group GROUP_ID --as TAG --send "hi"  # pick a tag
    python cc_group_chat_bridge.py --group GROUP_ID --listen [--once] [--json]  # push, no cron
    python cc_group_chat_bridge.py --group GROUP_ID --send "hi" --mention-depth 1  # answer at depth

A [MENTION] line or a history line marked [--mention-depth N] is answered at
depth N, so the count two bridges pass each other is not reset to 0 at every
crossing (specs/dptp_v1.md §4.3). The bridge remembers the N it printed for a
mention of its own tag (MENTION_DEPTH_STATE_FILE under DPC_HOME, per group and
tag) and the next --send/--send-file to that group carries it when no
--mention-depth is given; that send consumes it. [SENT] names the depth sent.

@all: a human's @all (outside code) counts as a mention of this bridge; an agent's
does not, as on the backend. --send/--send-file refuse a text carrying @all outside
code, so a bridge can never start a cascade.

Identity: --as, else the tag registered for this node in Group Settings
(metadata.json agents/agent_names), else [agent_chat] cc_display_name.
"""

import json
import os
import re
import sys
import time
import asyncio
import tempfile
import argparse
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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
    """Config fallback: [agent_chat] cc_display_name in config.ini, "CC" when unset."""
    config = _read_config()
    return config.get("agent_chat", "cc_display_name", fallback="CC")


def _configured_display_name() -> str:
    """The name-neutral reader; the key it reads keeps its historical name."""
    return _get_cc_display_name()


def _at_names(names: list) -> str:
    """'@a, @b' for user-visible lines."""
    return ", ".join("@" + n for n in names)


def _mentions_banner(names: list, n: int) -> str:
    """The --mentions header, naming the tags scanned for."""
    if not n:
        return f"No mentions of {_at_names(names)} found."
    return f"=== {n} mention(s) of {_at_names(names)} ==="


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
    return _registered_tags(group_id) or [_configured_display_name()]


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

    Prefer the slugged directory (backend's active write target). metadata.json is
    the group's identity and exists from the join; history.json only after a message.
    """
    base = DPC_HOME / "conversations"
    for d in sorted(base.iterdir()):
        if d.is_dir() and d.name.startswith(group_id + "-") and (
                (d / "metadata.json").exists() or (d / "history.json").exists()):
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
    """One whole-word regex per name (@tag, not @tag2); Cyrillic @сс only when a name is CC."""
    patterns = [re.compile(r"(?<!\w)@" + re.escape(n) + r"(?!\w)", re.IGNORECASE) for n in names]
    if any(n.lower() == "cc" for n in names):
        patterns.append(re.compile(r"(?<!\w)@сс(?!\w)", re.IGNORECASE))
    return patterns


# Mirrors dpc_client_core.service.CODE_BLOCK_RE / mentions_in (fenced blocks, then
# inline code: a name quoted as code is not a call). Not imported: service.py pulls
# the whole backend in, and this CLI runs standalone under any tag. A test pins the
# pattern to the backend's so the two cannot drift.
_CODE_RE = re.compile(r"```[\s\S]*?```|`[^`\n]+`")
_ALL_RE = re.compile(r"(?<!\w)@all(?!\w)", re.IGNORECASE)


def _has_all(text) -> bool:
    """True when `text` carries @all (any case, whole word) outside code."""
    return bool(_ALL_RE.search(_CODE_RE.sub("", text or "")))


def _is_agent_post(msg: dict) -> bool:
    """The backend's test for an agent sender: signed sender_type, or legacy is_agent."""
    return msg.get("sender_type") == "agent" or bool(msg.get("is_agent"))


def _all_mention(msg: dict) -> bool:
    """A human's @all in `msg`. Only a human may broadcast it — an agent's @all
    wakes nobody on the backend (service.py `mention_all`), so it is no mention here."""
    if _is_agent_post(msg):
        return False
    return _has_all(msg.get("content", "") or msg.get("text", ""))


def find_mentions(messages: list, since_index: int = 0, names: list = None) -> list:
    """Find @<name> mentions after since_index. Returns [(index, msg), ...].

    names defaults to [cc_display_name]; the CLI passes the resolved identity list.
    A human's @all counts as a mention of every name, as on the backend.
    """
    if not names:
        names = [_configured_display_name()]
    patterns = _mention_patterns(names)
    mentions = []
    for i, msg in enumerate(messages):
        if i < since_index:
            continue
        content = msg.get("content", "") or msg.get("text", "")
        sender = msg.get("sender_name", "")
        if sender in names:
            continue
        if any(p.search(content) for p in patterns) or _all_mention(msg):
            mentions.append((i, msg))
    return mentions


def _build_send_command(group_id: str, name: str, text: str, mention_depth: int = 0) -> dict:
    """The send_group_agent_message command; the backend copies agent_name into sender_name.

    `mention_depth` is carried only when > 0, as the backend carries it on the wire.
    """
    import uuid
    payload = {
        "group_id": group_id,
        "agent_name": name,
        "text": text,
    }
    if mention_depth:
        payload["mention_depth"] = mention_depth
    return {
        "id": str(uuid.uuid4())[:8],
        "command": "send_group_agent_message",
        "payload": payload,
    }


def _reply_mention_depth(msg: dict, node_id: str) -> int:
    """The mention_depth a reply to history record `msg` carries (specs/dptp_v1.md §4.3).

    The backend's rule for an agent it wakes: a human's post starts at 0, an
    agent post from this node is inherited, an agent post from another node
    adds one. Reading history, the bridge cannot ask whether the backend would
    have woken anyone at all, so an agent post it would not have woken still
    counts one deeper — the direction that stops a chain rather than resets it.
    """
    if msg.get("sender_type") != "agent":
        return 0
    try:
        depth = max(int(msg.get("mention_depth") or 0), 0)
    except (TypeError, ValueError):
        depth = 0
    if node_id and msg.get("sender_node_id") == node_id:
        return depth
    return depth + 1


# --- the depth this bridge last printed, carried to its next send -------------
#
# A [MENTION] line or a history line for this bridge's tag shows the depth its
# reply must carry; a caller that forgot --mention-depth sent 0 and so reset
# the count at the crossing (Zcode's and Ark's review of 3706cf47).

MENTION_DEPTH_STATE_FILE = "group_bridge_mention_depth.json"

# A remembered depth older than this is ignored. Its age is the age of the
# mention it was printed for — a [MENTION] event's arrival, a history record's
# own timestamp — not of the print, because the cron re-reads the same last 10
# lines every minute and would otherwise keep yesterday's mention fresh for
# ever. An hour is many cron ticks past the answer a mention gets, and bounds
# how long one deep mention can hold the bridge's later replies deep with it.
MENTION_DEPTH_TTL_S = 3600


def _depth_state_path() -> Path:
    """Beside .ws_token, under DPC_HOME."""
    return DPC_HOME / MENTION_DEPTH_STATE_FILE


def _is_depth(value) -> bool:
    """A whole number >= 0; a bool is not one, and neither is 1.5."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _load_depth_state() -> dict:
    """{group_id: {tag_lower: {"depth": N, "at": epoch_s}}}; {} when absent or unreadable."""
    try:
        state = json.loads(_depth_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_depth_state(state: dict) -> None:
    """Temp file + os.replace, so a reader never sees half a file. A failed write
    is said on stderr and does not stop the read or the send that caused it."""
    path = _depth_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        print(f"[WARN] could not record mention_depth in {path}: {e}", file=sys.stderr)


def _fresh_entry(entry, now: float):
    """(depth, at) of an entry, or None when malformed or older than MENTION_DEPTH_TTL_S."""
    if not isinstance(entry, dict) or not _is_depth(entry.get("depth")):
        return None
    at = entry.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or now - at > MENTION_DEPTH_TTL_S:
        return None
    return entry["depth"], at


def _remember_reply_depth(group_id: str, tag: str, depth, at: float = None) -> None:
    """Record that a mention of `tag` in `group_id`, made at `at` (default now),
    is answered at `depth`.

    When several are printed before a send, the largest fresh one is kept: the
    send answers one of them and the bridge cannot know which, and a reply too
    deep only fails to wake a peer agent, where one too shallow restarts the
    chain the bound exists to stop. A 0 adds nothing — an unflagged send
    already carries 0 — so it writes nothing.
    """
    now = time.time()
    at = now if at is None else at
    if not tag or not _is_depth(depth) or depth == 0 or now - at > MENTION_DEPTH_TTL_S:
        return
    state = _load_depth_state()
    per_group = state.get(group_id)
    if not isinstance(per_group, dict):
        per_group = state[group_id] = {}
    key = tag.lower()
    held = _fresh_entry(per_group.get(key), now)
    # Keep the larger depth; between equal ones the later mention, which ages out last.
    new = max(held, (depth, at)) if held else (depth, at)
    if new == held:
        return
    per_group[key] = {"depth": new[0], "at": new[1]}
    _write_depth_state(state)


def _remembered_depth(group_id: str, tag: str) -> int:
    """The fresh depth remembered for `tag` in `group_id`, 0 when none."""
    per_group = _load_depth_state().get(group_id)
    if not isinstance(per_group, dict):
        return 0
    held = _fresh_entry(per_group.get(tag.lower()), time.time())
    return held[0] if held else 0


def _forget_depth(group_id: str, tag: str) -> None:
    """A send consumed it: the next send starts from nothing again."""
    state = _load_depth_state()
    per_group = state.get(group_id)
    if isinstance(per_group, dict) and tag.lower() in per_group:
        del per_group[tag.lower()]
        if not per_group:
            del state[group_id]
        _write_depth_state(state)


def _moment(ts):
    """A history record's ISO timestamp as epoch seconds; naive reads as UTC; None if unreadable."""
    from datetime import datetime, timezone
    if not isinstance(ts, str) or not ts:
        return None
    try:
        moment = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _tags_mentioned(msg: dict, names: list) -> list:
    """Which of `names` a history record mentions, by find_mentions' rule (own posts skipped)."""
    if not find_mentions([msg], names=names):
        return []
    if _all_mention(msg):
        return list(names)
    content = msg.get("content", "") or msg.get("text", "")
    return [n for n in names if any(p.search(content) for p in _mention_patterns([n]))]


def _remember_history_line(group_id: str, msg: dict, names: list, node_id: str) -> None:
    """A printed history record that mentions one of this bridge's tags leaves its hint.
    An unreadable timestamp counts as now — the direction that stops a chain."""
    depth = _reply_mention_depth(msg, node_id)
    if not depth:
        return
    for tag in _tags_mentioned(msg, names):
        _remember_reply_depth(group_id, tag, depth, _moment(msg.get("timestamp")))


def _depth_for_send(group_id: str, tag: str, explicit) -> tuple:
    """(depth, where it came from) for a send: --mention-depth when given, else the
    remembered one, else 0."""
    if explicit is not None:
        return explicit, "--mention-depth"
    remembered = _remembered_depth(group_id, tag)
    return remembered, ("remembered" if remembered else "default")


def _mention_depth_arg(value: str) -> int:
    """argparse type for --mention-depth: a whole number >= 0 in ASCII digits.
    int() alone would also take "1_0", " 1" or "+1"; "1.5" it refuses."""
    if not re.fullmatch(r"[0-9]+", value):
        raise argparse.ArgumentTypeError(f"must be a whole number >= 0, got {value!r}")
    return int(value)


def _send_outcome(reply: dict) -> tuple:
    """(ok, text for the [SENT] line). The local API answers
    {"status": "OK"|"ERROR", "payload": <handler return>}; a refused post is
    {"status": "error", "message": ...} under an OK envelope, success a message_id."""
    if not isinstance(reply, dict):
        return False, "ERROR malformed reply"
    outer, inner = reply.get("status"), reply.get("payload")
    inner_msg = inner.get("message") or inner.get("error") if isinstance(inner, dict) else None
    if outer != "OK":
        return False, f"ERROR {inner_msg or reply.get('message') or outer or '?'}"
    if isinstance(inner, dict) and str(inner.get("status", "")).lower() == "error":
        return False, f"ERROR {inner_msg or '(no message given)'}"
    return True, "OK"


async def _await_reply(ws, command_id: str, deadline: float) -> dict:
    """The frame answering `command_id`. The backend broadcasts events to every
    client, this one included, and the send's own group_text_received arrives
    before the reply — so events, other ids and unparsable frames are skipped.
    Raises asyncio.TimeoutError once `deadline` (loop time) passes."""
    loop = asyncio.get_running_loop()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        try:
            frame = json.loads(raw)
        except ValueError:
            continue
        if isinstance(frame, dict) and "event" not in frame and frame.get("id") == command_id:
            return frame


async def send_group_message(group_id: str, text: str, name: str = None,
                             mention_depth: int = 0) -> dict:
    """Post `text` to the group via WebSocket, as `name` (default: resolved identity),
    at `mention_depth` — the value printed for the mention being answered."""
    # Cascade guard: a bridge never broadcasts @all (docs/GROUP_CHAT.md). Code spans
    # and fenced blocks are exempt, so the tag can be talked about.
    if _has_all(text):
        print("[ERROR] refused: the text contains @all outside code. A bridge never "
              "broadcasts @all (anti-cascade); name the tags, or put `@all` in backticks.")
        return {"status": "error", "message": "@all refused"}

    canonical_id = _resolve_group_id(group_id)

    try:
        import websockets
    except ImportError:
        print("[ERROR] websockets not installed.")
        return {"status": "error", "message": "websockets not installed"}

    if name is None:
        name = _resolve_identity(group_id)
    if name != _configured_display_name():
        print(f"[INFO] posting as {name}")
    command = _build_send_command(canonical_id, name, text, mention_depth)

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
                deadline = asyncio.get_running_loop().time() + 10
                result = await _await_reply(ws, command["id"], deadline)
                _, status = _send_outcome(result)
                print(f"[SENT] {len(text)} chars → group {group_id}: {status} "
                      f"(mention_depth={mention_depth})")
                return result
            except asyncio.TimeoutError:
                print(f"[SENT] {len(text)} chars → group {group_id} (no response, timeout) "
                      f"(mention_depth={mention_depth})")
                return {"status": "sent"}
    except ConnectionRefusedError:
        print("[ERROR] Cannot connect to backend. Is it running?")
        return {"status": "error", "message": "connection refused"}
    except Exception as e:
        print(f"[ERROR] {e}")
        return {"status": "error", "message": str(e)}


def _canonical_group_id(group_id: str) -> str:
    """The canonical group_id from metadata.json, silently; `group_id` when unreadable.

    The --group argument may be a slugged directory name (e.g. group-abc123-dpc-discord)
    but the backend expects the canonical group_id from metadata.json (e.g. group-abc123).
    """
    try:
        metadata_path = _find_group_dir(group_id) / "metadata.json"
        if metadata_path.exists():
            with open(metadata_path, "r", encoding="utf-8") as f:
                return json.load(f).get("group_id", group_id)
    except Exception:
        pass
    return group_id


def _resolve_group_id(group_id: str) -> str:
    """_canonical_group_id, saying so when it differs from what was typed."""
    canonical = _canonical_group_id(group_id)
    if canonical != group_id:
        print(f"[INFO] Resolved {group_id} → {canonical}")
    return canonical


def send_group_message_sync(group_id: str, text: str, name: str = None,
                            mention_depth: int = 0) -> dict:
    """Sync wrapper for send_group_message."""
    return asyncio.run(send_group_message(group_id, text, name, mention_depth))


def _send_exit_code(reply: dict) -> int:
    """CLI exit for a send: 1 on a refused or failed post; a timed-out reply is not a refusal."""
    if isinstance(reply, dict) and reply.get("status") == "sent":
        return 0
    return 0 if _send_outcome(reply)[0] else 1


def _mention_for_me(frame, canonical_group_id: str, names_lower, all_tags: bool = False):
    """Payload of a cc_group_mention frame for this group and one of our tags, else None."""
    if not isinstance(frame, dict) or frame.get("event") != "cc_group_mention":
        return None
    payload = frame.get("payload")
    if not isinstance(payload, dict) or payload.get("group_id") != canonical_group_id:
        return None
    tag = str(payload.get("agent_tag") or "").lower()
    if not all_tags and tag not in names_lower:
        return None
    return payload


def _depth_hint(depth) -> str:
    """' [--mention-depth N]' for a reply that must carry N > 0, else ''."""
    return f" [--mention-depth {depth}]" if depth else ""


def _format_mention(payload: dict, ts: str = None) -> str:
    """One human line per event: first line of the text, cut at 200 chars, and the
    --mention-depth the reply must carry when the backend says it is above 0."""
    import time
    first = (str(payload.get("text") or "").splitlines() or [""])[0]
    return (f"[MENTION] {ts or time.strftime('%H:%M:%S')} {payload.get('sender_name', '?')} "
            f"(@{payload.get('agent_tag', '?')}){_depth_hint(payload.get('mention_depth'))} "
            f"in {payload.get('group_id', '?')}: {first[:200]}")


async def listen_for_mentions(group_id: str, names: list, all_tags: bool = False,
                              as_json: bool = False, once: bool = False) -> int:
    """Print one line per cc_group_mention event addressed to `names`; reconnect with backoff."""
    canonical_id = _resolve_group_id(group_id)
    try:
        import websockets
    except ImportError:
        print("[ERROR] websockets not installed.", file=sys.stderr)
        return 1

    names_lower = [n.lower() for n in names]
    who = "any tag" if all_tags else ", ".join("@" + n for n in names)
    print(f"[LISTEN] listening for {who} in {canonical_id}", file=sys.stderr, flush=True)

    delay = 1
    while True:
        try:
            # Re-read per connect: the backend rotates the token on every start.
            auth_token = (DPC_HOME / ".ws_token").read_text(encoding="utf-8").strip()
            async with websockets.connect(_get_ws_url()) as ws:
                await ws.send(json.dumps({
                    "id": "group-bridge-auth",
                    "command": "auth",
                    "token": auth_token,
                }))
                auth_result = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                if auth_result.get("status") != "OK":
                    print(f"[ERROR] Auth rejected: {auth_result}", file=sys.stderr)
                    return 1
                delay = 1
                while True:
                    try:
                        frame = json.loads(await ws.recv())
                    except ValueError:
                        continue
                    payload = _mention_for_me(frame, canonical_id, names_lower, all_tags)
                    if payload is None:
                        continue
                    print(json.dumps(payload, ensure_ascii=False) if as_json
                          else _format_mention(payload), flush=True)
                    # --all-tags prints other tags' mentions too; only ours leave a hint.
                    tag = str(payload.get("agent_tag") or "")
                    if tag.lower() in names_lower:
                        _remember_reply_depth(canonical_id, tag, payload.get("mention_depth"))
                    if once:
                        return 0
        except Exception as e:  # socket closed, backend down, bad auth frame — all transient
            reason = str(e) or type(e).__name__
            print(f"[LISTEN] reconnecting in {delay}s ({reason})", file=sys.stderr, flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


def listen_sync(group_id: str, names: list, all_tags: bool = False,
                as_json: bool = False, once: bool = False) -> int:
    """Sync wrapper for listen_for_mentions; Ctrl-C ends it with exit 0."""
    try:
        return asyncio.run(listen_for_mentions(group_id, names, all_tags, as_json, once))
    except KeyboardInterrupt:
        print("[LISTEN] stopped", file=sys.stderr, flush=True)
        return 0


def format_message(i: int, msg: dict, node_id: str = None) -> str:
    """Format a message for display. Full content, no truncation. An agent post
    shows the --mention-depth a reply to it must carry, when above 0."""
    sender = msg.get("sender_name", msg.get("sender", "?"))
    content = msg.get("content", "") or msg.get("text", "")
    ts = msg.get("timestamp", "")
    if ts and "T" in ts:
        ts = ts.split("T")[1][:8]
    preview = content.replace("\n", "\n       ")
    hint = _depth_hint(_reply_mention_depth(msg, node_id if node_id is not None else _get_node_id()))
    return f"  [{i + 1}] {ts} {sender}{hint}: {preview}"


def _build_parser() -> argparse.ArgumentParser:
    """The CLI; factored out so tests can read its help."""
    parser = argparse.ArgumentParser(
        prog="cc_group_chat_bridge.py",
        description="Group chat bridge for an external agent: read a group's history, "
                    "scan for mentions of this bridge's tag, post under it")
    parser.add_argument("--list", action="store_true", help="List available groups")
    parser.add_argument("--group", type=str, help="Group ID to interact with")
    parser.add_argument("--last", type=int, default=10, help="Last N messages")
    parser.add_argument("--mentions", action="store_true",
                        help="Show mentions of this bridge's tag")
    parser.add_argument("--send", type=str, help="Send a response as this bridge's name")
    parser.add_argument("--send-file", type=str, dest="send_file",
                        help="Send a response from file as this bridge's name (backtick-safe)")
    parser.add_argument("--mention-depth", type=_mention_depth_arg, default=None,
                        dest="mention_depth", metavar="N",
                        help="With --send/--send-file: the mention_depth the post carries, "
                             "signed. Optional: without it the post carries the largest N "
                             "this bridge printed as [--mention-depth N] on a [MENTION] "
                             "line or a history line for its tag in this group, for a "
                             "mention made in the last hour, else 0; a post that goes out "
                             "consumes it. Given, N (0 too) wins and also consumes it. An "
                             "agent post from another node wakes an agent on its receiver "
                             "only at 0, so a reply at 0 where N was shown can start a loop "
                             "between two bridges. [SENT] names the depth sent")
    parser.add_argument("--as", type=str, dest="as_name", metavar="TAG",
                        help="Post/scan as this tag (default: the tag registered in "
                             "Group Settings, else cc_display_name)")
    parser.add_argument("--listen", action="store_true",
                        help="Block and print each cc_group_mention event for this bridge's "
                             "tag as it arrives (push, no cron); reconnects when the backend "
                             "drops")
    parser.add_argument("--all-tags", action="store_true", dest="all_tags",
                        help="With --listen: print mentions of any tag in the group")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="With --listen: one JSON object per line instead of [MENTION]")
    parser.add_argument("--once", action="store_true",
                        help="With --listen: exit 0 after the first matching mention")
    return parser


def _send_and_consume(group_id: str, text: str, name: str, explicit) -> int:
    """Post at the depth _depth_for_send picks; a post that went out consumes the
    remembered one. A refused or failed post keeps it, so the retry carries it too."""
    canonical = _canonical_group_id(group_id)
    depth, source = _depth_for_send(canonical, name, explicit)
    if source == "remembered":
        print(f"[INFO] mention_depth {depth} remembered from the last mention of @{name} shown")
    code = _send_exit_code(send_group_message_sync(group_id, text, name, depth))
    if code == 0:
        _forget_depth(canonical, name)
    return code


def main(argv=None):
    """The CLI. Exits through sys.exit as it always has; a read returns None."""
    args = _build_parser().parse_args(argv)

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

    if args.listen:
        if args.send or args.send_file or args.mentions:
            print("Error: --listen cannot be combined with --send, --send-file or --mentions",
                  file=sys.stderr)
            sys.exit(2)
        sys.exit(listen_sync(args.group, _identity_names(args.group, args.as_name),
                             args.all_tags, args.as_json, args.once))

    if args.send:
        name = _resolve_identity(args.group, args.as_name)
        sys.exit(_send_and_consume(args.group, args.send, name, args.mention_depth))

    if args.send_file:
        try:
            text = Path(args.send_file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"[ERROR] Cannot read --send-file: {e}", file=sys.stderr)
            sys.exit(1)
        name = _resolve_identity(args.group, args.as_name)
        sys.exit(_send_and_consume(args.group, text, name, args.mention_depth))

    messages = read_history(args.group, last_n=args.last)
    print(f"[CC Group Bridge] {len(messages)} messages (last {args.last})\n")

    node_id = _get_node_id()
    names = _identity_names(args.group, args.as_name)
    canonical = _canonical_group_id(args.group)
    if args.mentions:
        mentions = find_mentions(messages, names=names)
        print(_mentions_banner(names, len(mentions)))
        shown = mentions
    else:
        shown = list(enumerate(messages))
    for i, msg in shown:
        print(format_message(i, msg, node_id))
        _remember_history_line(canonical, msg, names, node_id)


if __name__ == "__main__":
    main()
