"""Where an AI chat lives on this node: its record and its history, in ~/.dpc.

Until 2026-10-19 the desktop UI kept the list of AI chats, the Telegram chats
and the full text of AI chat histories in the WebView's localStorage, outside
~/.dpc, and put them back at start without asking the backend — so deleting
~/.dpc left chats and message text behind
(THE-UI-KEEPS-CHATS-AND-AI-CHAT-HISTORIES-IN-THE-WEBVIEW-OUTSIDE-DOT-DPC).
Mike's call that day, Variant A: the backend is the only owner of chats and
their histories; the WebView keeps window preferences only.

An AI chat's history is the monitor's `history.json`, written by the same
writer as an agent's or a group's. Its record — the name, provider and
instruction set the sidebar shows — is `metadata.json` beside it, the file a
group keeps its own record in. A Telegram chat's existence is the bridge's
conversation link; its record holds only the name the sidebar shows.

This module owns that record and the one-time import of what the old UI had
stored. The import adds and never replaces: a record already on disk keeps its
values, and a history already on disk keeps every message it holds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import conversation_paths

logger = logging.getLogger(__name__)

AI_CHAT_PREFIX = "ai_"
TELEGRAM_CHAT_PREFIX = "telegram-"
RECORD_FILE = "metadata.json"

# What the old UI kept per chat in `dpc-ai-chats`, and nothing else: a record
# is read back into the sidebar, so a field that is not on this list cannot
# travel through it.
RECORD_FIELDS = (
    "name", "provider", "instruction_set_name", "profile_name", "llm_provider", "compute_host",
)
_MAX_FIELD_LENGTH = 256

# Set on a record written while the chat's history could not be (persistence
# off for the chat) — the chat the import answered "deferred": the history is
# still in the UI's old store, and an import with persistence on brings it over
# and removes this mark. Not a RECORD_FIELD:
# the UI never reads it, it only keeps the chat listed meanwhile.
HISTORY_DEFERRED = "history_deferred"
# The mark's first name, on disk from 2026-10-19 to 2026-10-20; the import
# removes it with the new one so no record keeps a mark nothing reads.
_HISTORY_DEFERRED_FIRST_NAME = "history_import_pending"

# The ids the UI mints (`ai_chat_` + 8 hex) and Telegram's numeric chat ids,
# which are negative for groups. Narrower than `is_safe_conversation_id` on
# purpose: an import takes a folder name from a browser store. No hyphen in an
# AI chat id: `ai_x-y` would read as chat `ai_x` with the folder slug `y` and
# take over that chat's folder (conversation_paths.is_store_dir_name).
_AI_CHAT_ID = re.compile(r"^ai_[A-Za-z0-9_]{1,64}$")
_TELEGRAM_CHAT_ID = re.compile(r"^telegram--?[0-9]{1,20}$")


def conversations_dir() -> Path:
    return Path.home() / ".dpc" / "conversations"


def is_ai_chat_id(conversation_id: Any) -> bool:
    return isinstance(conversation_id, str) and bool(_AI_CHAT_ID.match(conversation_id))


def is_telegram_chat_id(conversation_id: Any) -> bool:
    return isinstance(conversation_id, str) and bool(_TELEGRAM_CHAT_ID.match(conversation_id))


def clean_record(raw: Any) -> Optional[Dict[str, str]]:
    """The record fields of `raw` that are non-empty strings; None if `raw` is no dict."""
    if not isinstance(raw, dict):
        return None
    return {
        key: raw[key][:_MAX_FIELD_LENGTH]
        for key in RECORD_FIELDS
        if isinstance(raw.get(key), str) and raw[key]
    }


def _store_dir(conversation_id: str, base: Optional[Path]) -> Path:
    return conversation_paths.resolve_store_dir(base or conversations_dir(), conversation_id)


def read_record(conversation_id: str, base: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    path = _store_dir(conversation_id, base) / RECORD_FILE
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Unreadable chat record %s: %s", path, e)
        return None
    return data if isinstance(data, dict) else None


def write_record(
    conversation_id: str,
    fields: Dict[str, str],
    base: Optional[Path] = None,
    *,
    keep_existing: bool = False,
    drop: Tuple[str, ...] = (),
) -> str:
    """Merge `fields` into the chat's record; "written" or "kept".

    With `keep_existing` a value already on disk wins — the import's rule. A
    temp file and `os.replace`, because the record is what puts a chat in the
    sidebar and a half-written one would hide it. `drop` names keys to remove.
    """
    store = _store_dir(conversation_id, base)
    current = read_record(conversation_id, base) or {}
    merged = dict(current)
    for key, value in fields.items():
        if keep_existing and current.get(key):
            continue
        merged[key] = value
    for key in drop:
        merged.pop(key, None)
    merged["conversation_id"] = conversation_id
    merged.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    if merged == current:
        return "kept"
    store.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=store, prefix=".metadata-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
        os.replace(tmp, store / RECORD_FILE)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return "written"


def list_ai_chats(base: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every AI chat stored on this node, with its record, oldest first.

    A folder with a history and no record is listed too, without a name: the
    record is written when the chat is created, and a chat whose record write
    failed must not vanish while its history is still there.
    """
    base = base or conversations_dir()
    if not base.exists():
        return []
    chats: Dict[str, Dict[str, Any]] = {}
    for folder in sorted(base.iterdir(), key=lambda d: d.name.lower()):
        if not folder.is_dir() or not folder.name.startswith(AI_CHAT_PREFIX):
            continue
        record: Dict[str, Any] = {}
        if (folder / RECORD_FILE).exists():
            try:
                with open(folder / RECORD_FILE, encoding="utf-8") as f:
                    loaded = json.load(f)
                record = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Unreadable chat record in %s: %s", folder, e)
        elif not (folder / "history.json").exists():
            continue
        conversation_id = record.get("conversation_id") or folder.name
        if not is_ai_chat_id(conversation_id) or conversation_id in chats:
            continue
        if not conversation_paths.is_store_dir_name(folder.name, conversation_id):
            continue
        # A split store is one chat: list it from the folder every reader uses.
        if _store_dir(conversation_id, base).name.lower() != folder.name.lower():
            continue
        entry = {"conversation_id": conversation_id, **(clean_record(record) or {})}
        created_at = record.get("created_at")
        if created_at is not None and not isinstance(created_at, str):
            # One hand-edited record must not take the whole sidebar down with
            # the sort below: the chat is listed, undated.
            logger.warning("Chat record in %s has a created_at that is not a string (%r); listing it undated",
                           folder, created_at)
            created_at = None
        entry["created_at"] = created_at
        chats[conversation_id] = entry
    return sorted(chats.values(), key=lambda c: (c.get("created_at") or "", c["conversation_id"]))


def _iso_from_ui_timestamp(value: Any) -> Optional[str]:
    """The old UI stored milliseconds since the epoch; ISO strings pass through."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return value
        except ValueError:
            return None
    return None


def message_from_ui(
    conversation_id: str, msg: Any, previous_timestamp: Optional[str]
) -> Optional[Dict[str, Any]]:
    """One message as the old UI stored it, as `add_message` arguments.

    None for what is not dialogue: an error notice (`system`, or an `ai`
    bubble marked `isError`) and a placeholder whose answer was still in
    flight (`commandId`). A message
    with no id gets one derived from its content, so a second import of the
    same store finds it already there.
    """
    if not isinstance(msg, dict) or msg.get("commandId") or msg.get("isError"):
        return None
    sender = msg.get("sender")
    if sender == "user":
        role = "user"
    elif sender in ("ai", "cc") or (isinstance(sender, str) and sender.startswith("agent_")):
        role = "assistant"
    else:
        return None
    text = msg.get("text")
    if not isinstance(text, str):
        return None
    timestamp = _iso_from_ui_timestamp(msg.get("timestamp")) or previous_timestamp
    message_id = msg.get("id")
    if not isinstance(message_id, str) or not message_id or len(message_id) > 128:
        digest = hashlib.sha256(
            f"{conversation_id}|{role}|{timestamp}|{text}".encode("utf-8")
        ).hexdigest()[:16]
        message_id = f"import-{digest}"
    sender_name = msg.get("senderName") if isinstance(msg.get("senderName"), str) else None
    if role == "assistant" and not sender_name and isinstance(msg.get("model"), str):
        sender_name = f"AI ({msg['model']})"
    out: Dict[str, Any] = {
        "role": role,
        "content": text,
        "timestamp": timestamp,
        "message_id": message_id,
    }
    if sender_name:
        out["sender_name"] = sender_name
    if isinstance(msg.get("thinking"), str) and msg["thinking"]:
        out["thinking"] = msg["thinking"]
    attachments = [a for a in (msg.get("attachments") or []) if isinstance(a, dict)] \
        if isinstance(msg.get("attachments"), list) else []
    if attachments:
        out["attachments"] = attachments
    return out


# Which chats the import has already brought over, kept outside the chats'
# folders on purpose: a reset archives a chat's history and a delete removes its
# folder, and neither may be undone by the next import of a UI store that still
# holds the chat (an import whose other chats failed is asked again).
IMPORT_LEDGER = "ui_chat_import.json"


def _ledger_path(base: Optional[Path]) -> Path:
    return (base or conversations_dir()).parent / IMPORT_LEDGER


# Present from the first start that found no ~/.dpc: a person who deletes or
# renames ~/.dpc is asking for a clean install, and whatever the old UI still
# holds in the WebView is not to be carried into it. A file of its own, not a
# field of the ledger: the ledger can be moved aside as corrupt, and the fact
# that this home was born empty must outlive that. It is read by presence, so
# it needs no parsing and a restart before the UI connects still answers "fresh".
FRESH_INSTALL_MARKER = "ui_chat_import_fresh_install"


def _fresh_marker_path(base: Optional[Path]) -> Path:
    return (base or conversations_dir()).parent / FRESH_INSTALL_MARKER


def is_fresh_install(base: Optional[Path] = None) -> bool:
    """True when this ~/.dpc did not exist when the backend first started in it."""
    return _fresh_marker_path(base).exists()


def note_fresh_install(home: Optional[Path] = None) -> bool:
    """Called once per start, before anything creates ~/.dpc; True if it is fresh.

    If `home` does not exist it is created here and the marker written, so that
    every later start — the directory exists by then — still answers the same.
    `run_service.py` calls it ahead of the single-instance lock, which is the
    first thing that creates the directory. Never raises: a start must not fail
    on this, and a marker that could not be written means the import runs as it
    always has.
    """
    home = home or conversations_dir().parent
    try:
        if home.exists():
            return is_fresh_install(home / "conversations")
        home.mkdir(parents=True)
        (home / FRESH_INSTALL_MARKER).write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        # Logging is not configured yet when run_service calls this, so say it twice.
        note = f"{home} did not exist: a fresh install; the old UI store will not be imported"
        print("[startup] " + note)
        logger.info(note)
        return True
    except OSError as e:
        logger.warning("Could not record that %s is a fresh install: %s", home, e)
        return False


def _read_ledger(base: Optional[Path]) -> Dict[str, Any]:
    """The chats the import has brought over; an unreadable ledger is empty.

    A ledger that cannot be parsed would otherwise fail every chat on every
    attempt until someone repaired the file by hand. It is moved aside, as a
    torn history.json is (`ConversationMonitor.load_history`), and the import
    starts from an empty one. If it cannot be moved it is left alone and the
    error escapes: the caller fails the chat rather than write over it.
    """
    path = _ledger_path(base)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        imported = data.get("imported") if isinstance(data, dict) else None
        if not isinstance(imported, dict):
            raise ValueError(f"{path} is not an import ledger")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        aside = path.with_name(
            f"{path.name}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        )
        try:
            path.rename(aside)
        except OSError as move_error:
            logger.error("Unreadable import ledger %s (%s) and could not move it aside: %s; "
                         "not importing over it", path, e, move_error)
            raise
        logger.warning("Unreadable import ledger %s (%s) moved aside to %s; starting empty",
                       path, e, aside)
        return {}
    return imported


def _mark_imported(conversation_id: str, base: Optional[Path]) -> None:
    from .conversation_monitor import atomic_write_text
    imported = _read_ledger(base)
    imported[conversation_id] = datetime.now(timezone.utc).isoformat()
    atomic_write_text(_ledger_path(base), json.dumps({"imported": imported}, indent=2))


def import_chat(
    conversation_id: Any,
    record: Any,
    messages: Any,
    get_monitor: Callable[[str], Any],
    base: Optional[Path] = None,
    *,
    partial: bool = False,
) -> Dict[str, Any]:
    """Bring one chat the old UI stored into ~/.dpc; never overwrite.

    The answer is per chat. "imported", "kept", "skipped", "refused" and
    "discarded" are final and the UI drops the chat from its store; "deferred"
    and "failed" keep it there to be asked again. "discarded" is the answer on
    a fresh install (`note_fresh_install`): nothing is imported, nothing is
    written, and the chat is told to go. Everything is validated before anything
    is written, so a refused chat leaves no folder behind.

    A chat is imported once. The ledger is written on the last part of it
    (`partial` marks the parts of a history too large for one message), and
    a chat in the ledger is answered "kept" without a look at the disk — so a
    reset or a delete since then stands.
    """
    result: Dict[str, Any] = {"conversation_id": conversation_id}
    if is_fresh_install(base):
        return {**result, "status": "discarded", "reason": "fresh install", "messages_added": 0}
    if not isinstance(conversation_id, str) or not conversation_paths.is_safe_conversation_id(conversation_id):
        return {**result, "status": "refused", "reason": "not a conversation id"}
    if conversation_id.startswith("agent_"):
        # The agent's own history.json has been its only source since March;
        # the UI's copy was a cache of it.
        return {**result, "status": "skipped", "reason": "an agent keeps its own history in ~/.dpc"}
    if not (is_ai_chat_id(conversation_id) or is_telegram_chat_id(conversation_id)):
        return {**result, "status": "refused", "reason": "not an AI chat or Telegram chat id"}
    fields = clean_record(record) if record is not None else {}
    if fields is None:
        return {**result, "status": "refused", "reason": "the chat record is not an object"}
    if messages is not None and not isinstance(messages, list):
        return {**result, "status": "refused", "reason": "the history is not a list"}

    added = 0
    skipped = 0
    try:
        if conversation_id in _read_ledger(base):
            return {**result, "status": "kept", "reason": "imported before; a reset or delete since stands",
                    "messages_added": 0}

        if is_telegram_chat_id(conversation_id):
            # The old UI never stored a Telegram history, and the link that
            # makes a Telegram chat exist is the bridge's, so only the name
            # comes across.
            fields = {k: v for k, v in fields.items() if k in ("name", "provider")}
            written = write_record(conversation_id, fields, base, keep_existing=True) if fields else "kept"
        else:
            if messages:
                monitor = get_monitor(conversation_id)
                if not monitor.persist_history:
                    # The history is not written, and the answer is not final
                    # either: this may be the only copy, so the UI keeps it
                    # until the chat persists again. The record is written, so
                    # the chat is listed meanwhile instead of vanishing from a
                    # sidebar that no longer reads the old store.
                    write_record(conversation_id, {**fields, HISTORY_DEFERRED: True},
                                 base, keep_existing=True)
                    return {**result, "status": "deferred",
                            "reason": "history persistence is off for this chat"}
                have = set(monitor.message_ids) | {m.get("id") for m in monitor.message_history}
                had_history = bool(monitor.message_history)
                previous: Optional[str] = None
                for raw in messages:
                    converted = message_from_ui(conversation_id, raw, previous)
                    if converted is None:
                        skipped += 1
                        continue
                    previous = converted["timestamp"] or previous
                    if converted["message_id"] in have:
                        continue
                    have.add(converted["message_id"])
                    monitor.add_message(**converted)
                    added += 1
                if added:
                    if had_history:
                        # Reached by a later part of a large history, or when
                        # the chat was written to before the import finished:
                        # everything goes back into time order and the chain
                        # is rebuilt. No stored message is changed or dropped.
                        from .conversation_monitor import rechain
                        monitor.message_history.sort(key=lambda m: m.get("timestamp") or "")
                        rechain(monitor.message_history)
                    if not monitor.save_history():
                        return {**result, "status": "failed", "reason": "could not write history.json"}
            written = write_record(conversation_id, fields, base, keep_existing=True,
                                   drop=(HISTORY_DEFERRED, _HISTORY_DEFERRED_FIRST_NAME) if messages else ())
        if not partial:
            _mark_imported(conversation_id, base)
    except Exception as e:  # noqa: BLE001 — reported per chat, retried next start
        logger.error("Import of %s failed: %s", conversation_id, e, exc_info=True)
        return {**result, "status": "failed", "reason": str(e)}

    status = "imported" if (added or written == "written") else "kept"
    return {**result, "status": status, "record": written,
            "messages_added": added, "messages_skipped": skipped}
