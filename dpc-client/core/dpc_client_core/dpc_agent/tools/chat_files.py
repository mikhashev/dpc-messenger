"""
DPC Agent — chat_post_file: the agent posts a file into its own 1:1 chat.

ADR-051 D40, D41 and D43 (Mike #46, #53-#57; Zcode #51 corrections 4-6). The file is
copied, never moved, into the chat's `files/` folder by the same keeper the paperclip
uses (`service.keep_agent_chat_attachment`), and the attachment rides on the turn's
answer record: the tool collects it on `ToolContext.posted_attachments`,
`DpcAgent.process` hands the list to the agent manager, and `_record_answer` writes it.

Limits (D41):
- only the owner's turns (`owner_only`, checked by the registry; checked again here);
- only the agent's own 1:1 chat (`agent_*`): a group, a Telegram or a Discord turn is
  refused with its reason (posting into groups comes later, decision G);
- the path is resolved as `read_file` resolves it (`_resolve_file_path`: the "Extended
  path read" switch, the extended paths and the turn's audience gate), and nothing of
  the DPC home outside the agent's own directory and its chats' files is posted, even
  when an extended path opens it (a token or a key would land in a served folder);
- the file-transfer size cap, `file_transfer_cap_mb(rules)`, before anything is copied;
- at most MAX_FILES_PER_TURN files per turn;
- a file whose bytes are already in the chat's `files/` is not copied again.

A music take is posted by its `take_id` instead of a path (D41, D44): core resolves the id
through `get_music_take` into the take's own file, so the agent needs no access to
`~/.dpc/music/generated/` (which also holds each job's request, prompts and lyrics). The
attachment then carries `music: {track_id, take_id, job_id}`, `track_id` being the music
library entry id the player opens (the take's `library_track_id`; null while the take is not
in the library). `path` and `take_id` are given one at a time.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...music import job_schemas as js
from .core import _dpc_home, _resolve_file_path
from .registry import ToolContext, ToolEntry, is_owner_turn

log = logging.getLogger(__name__)

# D41: at most this many files per turn; the next one is refused.
MAX_FILES_PER_TURN = 4
# D42 records the value set at build: one copy and two hashes of at most the size cap.
CHAT_POST_FILE_TIMEOUT_SEC = 120
# A caption longer than this is cut: it is a line under a file, not a message.
MAX_CAPTION_CHARS = 500

# Sources whose turns never post a file into the DPC chat (D41): the answer goes back
# to Telegram or Discord, where a copy in the DPC folder would reach nobody.
_REFUSED_SOURCES = {
    "telegram": "Telegram", "telegram_owner": "Telegram", "discord": "Discord",
}

# One turn may run tools side by side: the count, the copy and the append are one step.
_post_lock = threading.Lock()

# A take is posted only when its job is finished (follow-up 12): not queued, running,
# cancelled, failed or unsupported.
POSTABLE_TAKE_JOB_STATES = frozenset({js.REVIEW, js.ACCEPTED})

# File types that run or open something when clicked, never posted (follow-up 12, Zcode #68
# P3): the chat shows a file in its folder, one click from running it. Compared lower-case,
# by the last suffix, after the trailing dots and spaces Windows drops from a name.
REFUSED_EXTENSIONS = frozenset({
    ".exe", ".bat", ".cmd", ".com", ".lnk", ".ps1", ".vbs", ".scr", ".msi", ".sh", ".app", ".dmg",
})


def refused_extension(name: str) -> Optional[str]:
    """The refused suffix of file name `name`, or None."""
    suffix = Path(name.rstrip(". ")).suffix.lower()
    return suffix if suffix in REFUSED_EXTENSIONS else None


def remove_posted_copies(paths: List[str]) -> int:
    """Remove the files `chat_post_file` copied into a chat's `files/` for a turn whose answer
    is never written (the turn failed): with no record nothing shows them, and nothing else
    removes them (follow-up 12, Zcode #68 P3). A file whose bytes were already there (reused)
    is never in `paths`. Returns how many were removed."""
    removed = 0
    for raw in paths or ():
        try:
            Path(raw).unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            log.warning("chat_post_file: the copy %s of a failed turn was not removed", raw, exc_info=True)
    if removed:
        log.info("chat_post_file: removed %d copied file(s) of a turn that failed", removed)
    return removed


def _refusal(text: str) -> str:
    return f"⚠️ chat_post_file refused: {text} Nothing was copied."


def turn_refusal(ctx: ToolContext) -> Optional[str]:
    """Why this turn may not post a file into a chat, in one line, or None when it may.

    The registry refuses a turn that is not the owner's before the body runs
    (`owner_only`); this is the second line, plus what only this tool knows: the turn
    must be in the agent's own 1:1 chat and not come from Telegram or Discord."""
    if not is_owner_turn(ctx):
        cls = getattr(ctx, "turn_class", None) or "unknown"
        return _refusal(f"this turn is not the owner's (class '{cls}').")
    source = getattr(ctx, "message_source", None)
    where = _REFUSED_SOURCES.get(source or "")
    if where is None and getattr(ctx, "reply_telegram_chat_id", None):
        where = "Telegram"
    if where:
        return _refusal(f"this turn came from {where}; a file is posted only into the agent's "
                        f"own chat in DPC, from a turn in that chat.")
    conversation_id = getattr(ctx, "conversation_id", None) or ""
    if conversation_id.startswith("group-"):
        return _refusal("this turn is in a group; posting a file into a group is not available "
                        "yet, only into the agent's own 1:1 chat.")
    if not conversation_id.startswith("agent_") or conversation_id != ctx.agent_root.name:
        return _refusal(f"this turn is not in the agent's own 1:1 chat "
                        f"({conversation_id or 'no chat'}).")
    return None


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def _dpc_home_refusal(ctx: ToolContext, resolved: Path) -> Optional[str]:
    """A refusal for a file of the DPC home outside the agent's own directory and the
    files of its own chats, or None. An extended path may open `~/.dpc` or `~` whole
    (the sandbox has no deny-list), and a copy in a chat's files folder is served."""
    try:
        home = _dpc_home().resolve()
    except OSError:
        # Fail closed (follow-up 12, Zcode #68 P3): a home that cannot be resolved cannot be
        # told apart from the file.
        return _refusal("the DPC home could not be resolved, so the file cannot be checked "
                        "against it.")
    if not _inside(resolved, home):
        return None
    if _inside(resolved, ctx.agent_root.resolve()):
        return None
    from .archive import reads_own_chat_files
    if reads_own_chat_files(ctx, resolved):
        return None
    return _refusal("the file is inside the DPC home, outside the agent's own directory and its "
                    "chats' files; such files (tokens, keys, settings) are never posted.")


def _chat_files_dir(ctx: ToolContext, conversation_id: str) -> Path:
    """The `files/` folder of the chat this turn is in: the monitor's own store when the
    turn carries it, else the store the conversation id resolves to."""
    monitor = getattr(ctx, "conversation_monitor", None)
    if monitor is not None and getattr(monitor, "conversation_id", None) == conversation_id:
        return monitor._get_conversation_dir() / "files"
    from dpc_client_core import conversation_paths
    base = Path.home() / ".dpc" / "conversations"
    return conversation_paths.resolve_store_dir(base, conversation_id) / "files"


def _rules(ctx: ToolContext) -> Dict[str, Any]:
    rules = getattr(getattr(ctx, "firewall", None), "rules", None)
    return rules if isinstance(rules, dict) else {}


def post_file(ctx: ToolContext, source: Path, caption: Optional[str] = None,
              music: Optional[Dict[str, Any]] = None) -> str:
    """Copy `source`, a path already resolved and admitted, into this turn's chat and
    collect its attachment on `ctx.posted_attachments`. The turn checks, the per-turn
    count and the size cap are applied here. `music`, for a take posted by its id, is
    written on the attachment as it is."""
    from dpc_client_core import service as service_mod

    refusal = turn_refusal(ctx)
    if refusal:
        return refusal
    if not source.is_file():
        return _refusal(f"'{source}' is not a file.")
    bad = refused_extension(source.name)
    if bad:
        return _refusal(f"a {bad} file runs or opens something when clicked; such files "
                        f"({', '.join(sorted(REFUSED_EXTENSIONS))}) are never posted.")
    max_mb =service_mod.file_transfer_cap_mb(_rules(ctx))
    too_large = service_mod.file_transfer_cap_refusal(source.stat().st_size, max_mb)
    if too_large:
        return _refusal(f"{too_large}.")
    files_dir = _chat_files_dir(ctx, ctx.conversation_id)
    with _post_lock:
        posted = ctx.posted_attachments
        if len(posted) >= MAX_FILES_PER_TURN:
            return _refusal(f"this answer already carries {MAX_FILES_PER_TURN} files, the most "
                            f"one turn may post.")
        try:
            before = {p.name for p in files_dir.iterdir()} if files_dir.is_dir() else set()
        except OSError:
            before = set()
        try:
            kept = service_mod.keep_agent_chat_attachment(
                files_dir, {"kind": "file", "file_path": str(source)},
                voice_mime_types=[], max_voice_seconds=service_mod.AGENT_CHAT_VOICE_DEFAULT_MAX_SECONDS,
                temp_dir=service_mod.DPC_HOME_DIR / "temp", max_file_mb=max_mb,
                measure_seconds=service_mod.measure_recording_seconds, reuse_same_bytes=True,
            )
        except ValueError as e:
            return _refusal(f"{e}.")
        reused = kept["filename"] in before
        if caption and caption.strip():
            kept["caption"] = " ".join(caption.split())[:MAX_CAPTION_CHARS]
        if music:
            kept["music"] = dict(music)
        if any(a.get("file_path") == kept["file_path"] for a in posted):
            return (f"{kept['filename']} is already attached to this answer; it is attached once. "
                    f"{len(posted)} of {MAX_FILES_PER_TURN} files this turn.")
        posted.append(kept)
        if not reused:
            # Removed again should the turn fail before its answer is written.
            ctx.posted_new_copies.append(kept["file_path"])
        count = len(posted)
    log.info("chat_post_file: %s %s into %s (%s, %s bytes)", "reused" if reused else "copied",
             kept["filename"], ctx.conversation_id, kept["mime_type"], kept["size_bytes"])
    details = [kept["mime_type"], f"{kept['size_mb']} MB"]
    duration = kept.get("duration_seconds")
    if duration:
        details.append(f"{duration:g} s")
    how = ("its bytes were already in this chat's files, so that copy is attached"
           if reused else "copied into this chat's files")
    return (f"Posted {kept['filename']} ({', '.join(details)}): {how}. It is attached to your "
            f"answer in this chat. {count} of {MAX_FILES_PER_TURN} files this turn.")


def _post_take(ctx: ToolContext, take_id: str, caption: Optional[str]) -> str:
    """Post a music take by its id: core gives the take's own file (`get_music_take`, D44),
    so neither `_resolve_file_path` nor the DPC-home rule applies to a path the agent never
    named; every other limit does (post_file)."""
    from .music import SERVICE_CALL_TIMEOUT_S, _service_call, _Unavailable
    try:
        answer = _service_call(ctx, "get_music_take", take_id=take_id)
    except _Unavailable as e:
        return _refusal(f"{e}.")
    except TimeoutError:
        return _refusal(f"the music service did not answer within {int(SERVICE_CALL_TIMEOUT_S)} s.")
    if not isinstance(answer, dict) or answer.get("status") != "ok":
        code = answer.get("code", "error") if isinstance(answer, dict) else "error"
        return _refusal(f"take '{take_id}' could not be read ({code}).")
    take = answer.get("take") or {}
    # Only a finished take the person has not discarded (follow-up 12, Zcode #68 P2): its job is
    # done, the take is not rejected, and its library entry, when it has one, is not hidden.
    job_state = take.get("job_state")
    if job_state not in POSTABLE_TAKE_JOB_STATES:
        return _refusal(f"take '{take_id}' belongs to a job that is {job_state or 'in an unknown state'}; "
                        f"only a take of a finished job ({', '.join(sorted(POSTABLE_TAKE_JOB_STATES))}) "
                        f"is posted.")
    if take.get("state") == "rejected":
        return _refusal(f"take '{take_id}' was discarded by the person.")
    if take.get("library_track_id") and take.get("library_hidden") is not False:
        why = "hidden (discarded)" if take.get("library_hidden") else "not found"
        return _refusal(f"take '{take_id}': its library entry is {why}.")
    if not take.get("path") or not take.get("file_exists"):
        return _refusal(f"take '{take_id}' has no file on this computer.")
    music = {"track_id": take.get("library_track_id") or None,
             "take_id": take.get("take_id") or take_id, "job_id": take.get("job_id")}
    return post_file(ctx, Path(take["path"]), caption, music=music)


def _chat_post_file(ctx: ToolContext, path: Optional[str] = None, caption: Optional[str] = None,
                    take_id: Optional[str] = None) -> str:
    refusal = turn_refusal(ctx)
    if refusal:
        return refusal
    path = path.strip() if isinstance(path, str) else path
    take_id = take_id.strip() if isinstance(take_id, str) else take_id
    if path and take_id:
        return _refusal("give either path or take_id, not both.")
    if take_id:
        return _post_take(ctx, take_id, caption)
    if not path:
        return _refusal("give path (a file) or take_id (a music take).")
    # As read_file resolves it: the extended-read switch, the extended paths and the
    # turn's audience gate. A PermissionError is the registry's SANDBOX_VIOLATION line.
    resolved = _resolve_file_path(ctx, path)
    home_refusal = _dpc_home_refusal(ctx, resolved)
    if home_refusal:
        return home_refusal
    return post_file(ctx, resolved, caption)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="chat_post_file",
            schema={
                "name": "chat_post_file",
                "description": (
                    "Post a file into your own 1:1 chat with the person, attached to your answer "
                    "(an audio file plays in the message). Use it only when the person asks for "
                    "the file. The file is copied, never moved. Limits: at most "
                    f"{MAX_FILES_PER_TURN} files per turn; the file-transfer size cap "
                    "(file_transfer.max_size_mb, 100 MB by default); only in your 1:1 chat in "
                    "DPC (a group, Telegram or Discord turn is refused); the path is resolved as "
                    "read_file resolves it (relative to your sandbox, or an absolute path your "
                    "extended read access reaches). A music take is posted by its take_id "
                    "(from music_check or music_list_takes) instead of a path; give one of the two."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The file: relative to your sandbox, or an absolute "
                                           "path within your extended read access. Not with take_id.",
                        },
                        "take_id": {
                            "type": "string",
                            "description": "A music take's id, instead of path: core finds the "
                                           "take's file, and the message can open it in the player.",
                        },
                        "caption": {
                            "type": "string",
                            "description": "Optional. A short line shown with the file.",
                        },
                    },
                    "required": [],
                },
            },
            handler=_chat_post_file,
            timeout_sec=CHAT_POST_FILE_TIMEOUT_SEC,
            is_core=False,
            default_enabled=False,
            owner_only=True,
        ),
    ]
