"""
DPC Agent — chat_post_file: the agent posts a file into its own 1:1 chat, or into the group
it is answering in (ADR-051 D50 G).

ADR-051 D40, D41 and D43, as ADR-053 changed D43 (Mike #46, #53-#57; Zcode #51 corrections 4-6).
The file is copied, never moved, into the chat's `files/` folder by the same keeper the paperclip
uses (`service.keep_agent_chat_attachment`), and the attachment rides on the turn's
answer record: the tool collects it on `ToolContext.posted_attachments`,
`DpcAgent.process` hands the list to the agent manager, and `_record_answer` writes it.

Limits (D41):
- the owner's requests, or a call the owner approved (ADR-053 `owner_approval_for_others`: the
  registry's gate asks the owner's card when someone else asked for the turn; this module checks
  again, per call, `turn_refusal`; D43's `owner_only` refusal is gone);
- only the agent's own 1:1 chat (`agent_*`) or the group the turn is in: a Telegram or a
  Discord turn is refused with its reason;
- the path is resolved as `read_file` resolves it (`_resolve_file_path`: the "Extended
  path read" switch, the extended paths and the turn's audience gate), and nothing of
  the DPC home outside the agent's own directory and its chats' files is posted, even
  when an extended path opens it (a token or a key would land in a served folder);
- the file-transfer size cap, `file_transfer_cap_mb(rules)`, before anything is copied;
- only a type on the allow-list (follow-up 13; it replaced follow-up 12's deny-list of
  executables): audio (flac, wav, mp3, ogg, m4a), image (png, jpg, webp, gif), pdf, and plain
  text (txt, md, json, csv), each by its extension AND by its first bytes, so a program renamed
  `song.flac` or `notes.txt` is refused (`content_refusal`; the copy is read again after it is made);
- at most MAX_FILES_PER_TURN files per turn, and at most MAX_BYTES_PER_TURN bytes in all
  (follow-up 14, Zcode #176: the count alone let four files of the cap each go out in one turn);
- a file whose bytes are already in the chat's `files/` is not copied again.

In a group (D50 G) the file does not ride on the answer: it goes through the person's group
file path (`service.send_group_agent_file` over `_send_group_file`: the copy in the group's
`files/`, the signed record, the signed file description and the chunked offer to every
connected member), authored by the agent, as a group message of its own sent when the tool
runs. The same limits hold, the size cap being the group's file-transfer cap. The source is
first copied to a temporary file whose content is checked again, so a file changed after the
check is not offered. That copy is read once, whole (follow-up 14, Zcode #176): its type from
its first bytes and its SHA-256 from all of them, and the send refuses a file whose hash is not
that one. On a foreign request the owner's card names the file's name, size and hash, and the
call the owner approved posts only that file: a source changed between the card and the send
is refused. A second post of the same content in one turn is judged by that hash, not by the
path. The room axis is `_resolve_file_path`'s audience gate (ADR-048): a store closed to the
group's audience is refused, and on a foreign request before any card.

A music take is posted by its `take_id` instead of a path (D41, D44): core resolves the id
through `get_music_take` into the take's own file, so the agent needs no access to
`~/.dpc/music/generated/`. The take's own file is not free of the lyrics, though: the sidecar
JSON beside it and its FLAC tags (`DPC_PROVENANCE`) hold the provenance record, whose `lyrics`
field is the lyrics as sent (ADR-051 D44, corrected in follow-up 13), so a posted take carries
its lyrics with it. The
attachment then carries `music: {track_id, take_id, job_id}`, `track_id` being the music
library entry id the player opens (the take's `library_track_id`; null while the take is not
in the library). `path` and `take_id` are given one at a time.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import mimetypes
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from ...music import job_schemas as js
from .core import _dpc_home, _resolve_file_path
from .registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

# D41: at most this many files per turn; the next one is refused.
MAX_FILES_PER_TURN = 4
# And at most this many bytes in all per turn (follow-up 14, Zcode #176): the per-file cap
# times MAX_FILES_PER_TURN would let one turn send 400 MB to every member of a group.
MAX_BYTES_PER_TURN = 200 * 1024 * 1024
# D42 records the value set at build: one copy and two hashes of at most the size cap.
CHAT_POST_FILE_TIMEOUT_SEC = 120
# A caption longer than this is cut: it is a line under a file, not a message.
MAX_CAPTION_CHARS = 500
# The group send (the copy into the group's files, the one hash, the offers), within the
# tool's CHAT_POST_FILE_TIMEOUT_SEC after the checked temporary copy.
GROUP_SEND_TIMEOUT_SEC = 90

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

# The types a file may have to be posted (follow-up 13, Zcode #68/#69 P3: an allow-list instead of
# the executables deny-list). The chat shows a file in its folder, one click from opening it, so
# only types the app itself plays or shows are allowed, and each is checked by its first bytes as
# well as by its suffix: a program renamed `take.flac` or `notes.txt` is refused.
KIND_AUDIO, KIND_IMAGE, KIND_PDF, KIND_TEXT = "audio", "image", "pdf", "text"
ALLOWED_SUFFIXES: Dict[str, str] = {
    ".flac": KIND_AUDIO, ".wav": KIND_AUDIO, ".mp3": KIND_AUDIO, ".ogg": KIND_AUDIO, ".m4a": KIND_AUDIO,
    ".png": KIND_IMAGE, ".jpg": KIND_IMAGE, ".jpeg": KIND_IMAGE, ".webp": KIND_IMAGE, ".gif": KIND_IMAGE,
    ".pdf": KIND_PDF,
    ".txt": KIND_TEXT, ".md": KIND_TEXT, ".markdown": KIND_TEXT, ".json": KIND_TEXT, ".csv": KIND_TEXT,
}
ALLOWED_TYPES_TEXT = ("audio (flac, wav, mp3, ogg, m4a), image (png, jpg, webp, gif), pdf, "
                      "plain text (txt, md, json, csv)")
# How much of the file is read to tell what it is.
_SNIFF_BYTES = 64 * 1024
# How much of a file is read at a time when it is hashed.
_HASH_CHUNK = 1024 * 1024
# What the owner's card for a foreign request to post into a group showed (Zcode #176): the
# file's name, size and SHA-256. Set by the approval check and read by the call it approved,
# in the same context (ToolRegistry.execute runs both); the call posts only that file.
_CARD_BINDING: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "chat_post_file_card_binding", default=None)
# First bytes of programs, for the message and the text check (the allow-list is what refuses).
_PROGRAM_MAGIC = (b"MZ", b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe",
                  b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"#!")


def _suffix_of(name: str) -> str:
    """The last suffix, lower-case, after the trailing dots and spaces Windows drops from a name."""
    return Path(name.rstrip(". ")).suffix.lower()


def sniff_kind(head: bytes, suffix: str = "") -> Optional[str]:
    """What `head`, a file's first bytes, is: "audio", "image", "pdf" or "text" for the
    types on the allow-list, or None. `suffix` only settles m4a (an ISO media file) and text."""
    if head.startswith((b"fLaC", b"OggS", b"ID3")):
        return KIND_AUDIO
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return KIND_AUDIO
    if suffix == ".mp3" and len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0 and (head[1] >> 1) & 3 != 0:
        return KIND_AUDIO  # an MPEG audio frame header, an mp3 with no ID3 tag (a UTF-16 BOM looks like one)
    if head[4:8] == b"ftyp" and suffix == ".m4a":
        return KIND_AUDIO
    if head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")):
        return KIND_IMAGE
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return KIND_IMAGE
    if b"%PDF-" in head[:1024]:
        return KIND_PDF
    if suffix in (".txt", ".md", ".markdown", ".json", ".csv") and _is_plain_text(head):
        return KIND_TEXT
    return None


def _is_plain_text(head: bytes) -> bool:
    """Text a person can read: UTF-8 (with or without a BOM) or UTF-16 with a BOM, no NUL byte in
    UTF-8, and no program signature."""
    import codecs
    if head.startswith(_PROGRAM_MAGIC):
        return False
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            codecs.getincrementaldecoder("utf-16")().decode(head, final=False)
            return True
        except UnicodeDecodeError:
            return False
    if b"\x00" in head:
        return False
    try:
        # final=False: the read may end inside a multi-byte character.
        codecs.getincrementaldecoder("utf-8-sig")().decode(head, final=False)
        return True
    except UnicodeDecodeError:
        return False


def content_refusal(path: Path) -> Optional[str]:
    """Why file `path` is not one of the allowed types, in a sentence, or None when it is.
    The suffix must be on the list and the first bytes must be that type (and the same group:
    a `.txt` that is a PNG is refused too)."""
    suffix_refusal = _suffix_refusal(path.name)
    if suffix_refusal:
        return suffix_refusal
    try:
        with open(path, "rb") as f:
            head = f.read(_SNIFF_BYTES)
    except OSError as e:
        return f"the file could not be read to check its type ({e.__class__.__name__})."
    return head_refusal(path.name, head)


def _suffix_refusal(name: str) -> Optional[str]:
    suffix = _suffix_of(name)
    if suffix in ALLOWED_SUFFIXES:
        return None
    what = f"the extension {suffix}" if suffix else "no extension"
    return f"a file with {what} is not a type that may be posted; only {ALLOWED_TYPES_TEXT} are."


def head_refusal(name: str, head: bytes) -> Optional[str]:
    """`content_refusal` for a file named `name` whose first bytes (up to _SNIFF_BYTES) are
    `head`, already read."""
    suffix_refusal = _suffix_refusal(name)
    if suffix_refusal:
        return suffix_refusal
    suffix = _suffix_of(name)
    expected = ALLOWED_SUFFIXES[suffix]
    found = sniff_kind(head, suffix)
    if found == expected:
        return None
    looks = ("a program" if head.startswith(_PROGRAM_MAGIC) else
             f"{found} data" if found else f"not {expected}")
    return (f"the file is named {name} but its content is {looks}, not {expected}; only "
            f"{ALLOWED_TYPES_TEXT} are posted, and the content must match the name.")


def file_digest(path: Path) -> Tuple[str, int, bytes]:
    """(SHA-256 hex, size in bytes, first _SNIFF_BYTES bytes) of `path`, from one read of the
    whole file, so the type checked and the hash are of the same bytes. Raises OSError."""
    digest = hashlib.sha256()
    head = b""
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            if len(head) < _SNIFF_BYTES:
                head += chunk[:_SNIFF_BYTES - len(head)]
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size, head


def discard_partial_copy(files_dir: Path, before: set, source_name: str) -> int:
    """Remove what a copy of `source_name` left in `files_dir` since `before` (the names that were
    there): the free name the copy claimed, `<name>` or `<stem>_N<suffix>`, nothing else. A copy
    that failed or was cancelled part way must not stay (follow-up 13, Zcode P3). Never raises."""
    import re
    source = Path(source_name)
    pattern = re.compile(re.escape(source.stem) + r"(?:_\d+)?" + re.escape(source.suffix), re.IGNORECASE)
    removed = 0
    try:
        for entry in files_dir.iterdir():
            if entry.name in before or not entry.is_file() or not pattern.fullmatch(entry.name):
                continue
            try:
                entry.unlink()
                removed += 1
            except OSError:
                log.warning("chat_post_file: the partial copy %s was not removed", entry, exc_info=True)
    except OSError:
        log.warning("chat_post_file: %s could not be read to remove a partial copy", files_dir, exc_info=True)
    if removed:
        log.info("chat_post_file: removed %d partial copy of %s", removed, source_name)
    return removed


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

    The registry's gate asks the owner first when someone else asked for the turn
    (ADR-053, `owner_approval_for_others`); this is the second line, per call, plus what
    only this tool knows: the turn must be in the agent's own 1:1 chat or in a group, and
    not come from Telegram or Discord."""
    from ..owner_approval import may_run_owner_tool, turn_asker
    where = place_refusal(ctx)
    if where:
        return where
    if not may_run_owner_tool(ctx):
        return _refusal(f"{turn_asker(ctx).label()} asked for this turn and is not the owner, "
                        f"and the owner did not approve this call.")
    return None


def in_group(ctx: ToolContext) -> bool:
    """Is this turn in a group chat (the file goes to the group, D50 G)?"""
    return (getattr(ctx, "conversation_id", None) or "").startswith("group-")


def place_refusal(ctx: ToolContext) -> Optional[str]:
    """Why this turn's chat is not one a file is posted into (Telegram, Discord, any chat but
    the agent's own 1:1 and a group), or None. Read before the owner check, so the reason the
    tool can never run here is the one given, and the gate raises no card for it."""
    source = getattr(ctx, "message_source", None)
    where = _REFUSED_SOURCES.get(source or "")
    if where is None and getattr(ctx, "reply_telegram_chat_id", None):
        where = "Telegram"
    if where:
        return _refusal(f"this turn came from {where}; a file is posted only into a chat in DPC "
                        f"(the agent's own chat or a group), from a turn in that chat.")
    conversation_id = getattr(ctx, "conversation_id", None) or ""
    if in_group(ctx):
        return None
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
    if in_group(ctx):
        return _post_into_group(ctx, source, caption, music)
    if not source.is_file():
        return _refusal(f"'{source}' is not a file.")
    bad = content_refusal(source)
    if bad:
        return _refusal(bad)
    max_mb = service_mod.file_transfer_cap_mb(_rules(ctx))
    too_large = service_mod.file_transfer_cap_refusal(source.stat().st_size, max_mb)
    if too_large:
        return _refusal(f"{too_large}.")
    files_dir = _chat_files_dir(ctx, ctx.conversation_id)
    with _post_lock:
        posted = ctx.posted_attachments
        if len(posted) >= MAX_FILES_PER_TURN:
            return _refusal(f"this answer already carries {MAX_FILES_PER_TURN} files, the most "
                            f"one turn may post.")
        over = _turn_bytes_refusal(sum(int(a.get("size_bytes") or 0) for a in posted),
                                   source.stat().st_size)
        if over:
            return _refusal(over)
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
            discard_partial_copy(files_dir, before, source.name)
            return _refusal(f"{e}.")
        except BaseException:
            # CancelledError, a timeout or an ffprobe that died after the bytes were copied: no
            # half file stays in the chat's files/ (the copy removes its own claim, but not what
            # the hashing and measuring after it left).
            discard_partial_copy(files_dir, before, source.name)
            raise
        reused = kept["filename"] in before
        try:
            # The copy is read again, whole: the source may have changed after the check above
            # (its type, and its size: a file that grew between the stat and the copy). The one
            # read gives the copy's type, its size and its SHA-256, like the group path's
            # `_checked_temp_copy`; the size counts toward the turn's byte cap, not the
            # source's earlier st_size (Zcode #184 P4).
            _sha256, copy_size, head = file_digest(Path(kept["file_path"]))
            changed = head_refusal(Path(kept["file_path"]).name, head)
            if not changed:
                changed = _turn_bytes_refusal(
                    sum(int(a.get("size_bytes") or 0) for a in posted), copy_size)
        except OSError as e:
            changed = f"the copy could not be read to check it ({e.__class__.__name__})."
        except BaseException:
            if not reused:
                discard_partial_copy(files_dir, before, source.name)
            raise
        if changed:
            if not reused:
                discard_partial_copy(files_dir, before, source.name)
            return _refusal(changed)
        if int(kept.get("size_bytes") or 0) != copy_size:
            kept["size_bytes"] = copy_size
            kept["size_mb"] = round(copy_size / (1024 * 1024), 2)
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


def _turn_bytes_refusal(already: int, size: int) -> Optional[str]:
    """Why a file of `size` bytes would take this turn past MAX_BYTES_PER_TURN, or None."""
    if already + size <= MAX_BYTES_PER_TURN:
        return None
    mb = 1024 * 1024
    return (f"this turn already posted {round(already / mb, 2)} MB, and this file "
            f"({round(size / mb, 2)} MB) would take it past {MAX_BYTES_PER_TURN // mb} MB, the "
            f"most one turn may post in all.")


def _group_cap_mb(ctx: ToolContext) -> float:
    """The cap on a file posted into a group: the group file-transfer cap
    (`file_transfer_cap_mb` with the group sender's default) and D41's cap on any posted file
    (its default, 100 MB, is what CHAT_POST_FILE_TIMEOUT_SEC was set for); one value when the
    rules name `file_transfer.max_size_mb`."""
    from dpc_client_core import service as service_mod
    rules = _rules(ctx)
    return min(service_mod.file_transfer_cap_mb(rules),
               service_mod.file_transfer_cap_mb(rules, service_mod.SENDER_FILE_DEFAULT_MAX_MB))


def _group_size_refusal(ctx: ToolContext, size_bytes: int) -> Optional[str]:
    from dpc_client_core import service as service_mod
    max_mb = _group_cap_mb(ctx)
    too_large = service_mod.file_transfer_cap_refusal(size_bytes, max_mb)
    if too_large:
        return (f"{too_large}; a file posted into a group is held to the group file-transfer "
                f"limit of {max_mb:g} MB.")
    return None


def _group_name(ctx: ToolContext) -> str:
    """The group as a card or a line names it: its name and id."""
    group_id = getattr(ctx, "conversation_id", None) or ""
    name = ""
    manager = getattr(getattr(ctx, "dpc_service", None), "group_manager", None)
    try:
        group = manager.get_group(group_id) if manager is not None else None
        name = (getattr(group, "name", "") or "") if group is not None else ""
    except Exception:
        name = ""
    name = name or getattr(ctx, "conversation_title", None) or ""
    return f"'{name}' ({group_id})" if name and name != group_id else group_id


def _caption_text(caption: Optional[str]) -> str:
    return " ".join(caption.split())[:MAX_CAPTION_CHARS] if caption and caption.strip() else ""


def _checked_temp_copy(
        source: Path) -> Tuple[Optional[Path], Optional[Tuple[str, int]], Optional[str]]:
    """(a copy of `source` under its own name in a fresh folder of the DPC temp dir, (its
    SHA-256, its size), None), or (None, None, refusal). The copy is what the group is sent,
    and its content is checked again: the source may change after the first check. Its type
    and its hash come from one read of the whole copy (Zcode #176), and the send checks the
    file it offers against that hash."""
    from dpc_client_core import service as service_mod
    folder = Path(service_mod.DPC_HOME_DIR) / "temp" / f"chat_post_file_{uuid.uuid4().hex[:12]}"
    try:
        folder.mkdir(parents=True, exist_ok=False)
        copy = folder / source.name
        shutil.copyfile(source, copy)
        sha256, size, head = file_digest(copy)
    except OSError as e:
        shutil.rmtree(folder, ignore_errors=True)
        return None, None, _refusal(
            f"the file could not be copied to be checked ({e.__class__.__name__}).")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    changed = head_refusal(copy.name, head)
    if changed:
        shutil.rmtree(folder, ignore_errors=True)
        return None, None, _refusal(changed)
    return copy, (sha256, size), None


class _GroupSendUnavailable(Exception):
    """No service loop to send the group file on (a test, a probe)."""


def _send_into_group(ctx: ToolContext, file_path: Path, text: str,
                     music: Optional[Dict[str, Any]], sha256: str, size: int) -> Dict[str, Any]:
    """`send_group_agent_file` on the service loop, held to the checked copy's hash and size;
    raises _GroupSendUnavailable or TimeoutError."""
    service = getattr(ctx, "dpc_service", None)
    loop = getattr(ctx, "agent_event_loop", None)
    send = getattr(service, "send_group_agent_file", None) if service is not None else None
    if send is None or loop is None or loop.is_closed():
        raise _GroupSendUnavailable("the group file send is not available in this context")
    future = asyncio.run_coroutine_threadsafe(send(
        group_id=ctx.conversation_id, agent_name=ctx.agent_root.name, file_path=str(file_path),
        text=text, music=dict(music) if music else None, expected_sha256=sha256,
        expected_size=size), loop)
    try:
        return future.result(timeout=GROUP_SEND_TIMEOUT_SEC)
    except BaseException:
        future.cancel()
        raise


def _post_into_group(ctx: ToolContext, source: Path, caption: Optional[str],
                     music: Optional[Dict[str, Any]]) -> str:
    """Post `source`, already resolved and admitted, into the group this turn is in (D50 G):
    the allow-list, the group size cap and the per-turn count, then the checked temporary
    copy goes through the person's group file path, authored by this agent."""
    if not source.is_file():
        return _refusal(f"'{source}' is not a file.")
    bad = content_refusal(source)
    if bad:
        return _refusal(bad)
    too_large = _group_size_refusal(ctx, source.stat().st_size)
    if too_large:
        return _refusal(too_large)
    # The card this call was approved on, read once (a later call of the turn has its own).
    card = _CARD_BINDING.get()
    _CARD_BINDING.set(None)
    # The slot is held from here while the file is checked and sent; it is named by the
    # content's hash once that is known (Zcode #176: not by the path).
    slot = [f"pending:{uuid.uuid4().hex}"]
    with _post_lock:
        posted = ctx.posted_group_files
        if len(posted) >= MAX_FILES_PER_TURN:
            return _refusal(f"this turn already posted {MAX_FILES_PER_TURN} files, the most one "
                            f"turn may post.")
        posted.append(slot[0])

    def release() -> None:
        with _post_lock:
            if slot[0] in ctx.posted_group_files:
                ctx.posted_group_files.remove(slot[0])
            ctx.posted_group_bytes.pop(slot[0], None)

    copy = None
    try:
        copy, digest, refusal = _checked_temp_copy(source)
        if refusal:
            release()
            return refusal
        sha256, size = digest
        too_large = _group_size_refusal(ctx, size)
        if too_large:
            release()
            return _refusal(too_large)
        unbound = _card_mismatch(ctx, card, source.name, size, sha256)
        if unbound:
            release()
            return _refusal(unbound)
        key = f"sha256:{sha256}"
        with _post_lock:
            posted = ctx.posted_group_files
            if key in posted:
                posted.remove(slot[0])
                return (f"{source.name} was already posted into this group in this turn (the "
                        f"same content); it is posted once. {len(posted)} of "
                        f"{MAX_FILES_PER_TURN} files this turn.")
            over = _turn_bytes_refusal(sum(ctx.posted_group_bytes.values()), size)
            if over:
                posted.remove(slot[0])
                return _refusal(over)
            posted[posted.index(slot[0])] = key
            slot[0] = key
            ctx.posted_group_bytes[key] = size
        try:
            answer = _send_into_group(ctx, copy, _caption_text(caption), music, sha256, size)
        except _GroupSendUnavailable as e:
            release()
            return _refusal(f"{e}.")
        except TimeoutError:
            # The send may still finish: the slot stays taken, so a retry cannot post twice.
            return (f"⚠️ chat_post_file: the group send of {source.name} did not answer within "
                    f"{GROUP_SEND_TIMEOUT_SEC} s; it may still appear in the group. Do not post it "
                    f"again in this turn.")
        except BaseException:
            release()
            raise
    finally:
        if copy is not None:
            shutil.rmtree(copy.parent, ignore_errors=True)
    if not isinstance(answer, dict) or answer.get("status") != "success":
        release()
        message = answer.get("message", "error") if isinstance(answer, dict) else str(answer)
        return f"⚠️ chat_post_file refused: the group send refused the file: {message}"
    with _post_lock:
        count = len(ctx.posted_group_files)
    offered = len(answer.get("transfer_ids") or [])
    offline = len(answer.get("offline_members") or [])
    size_mb = round(int(answer.get("size_bytes") or 0) / (1024 * 1024), 2)
    who = answer.get("sender_name") or ctx.agent_root.name
    log.info("chat_post_file: %s posted %s into group %s (%s, %s bytes), offered to %d member(s)",
             who, source.name, ctx.conversation_id, answer.get("mime_type"),
             answer.get("size_bytes"), offered)
    reach = f"offered to {offered} connected member(s)"
    if offline:
        reach += f"; {offline} member(s) not connected were not offered it"
    name = Path(answer.get("file_path") or source.name).name
    return (f"Posted {name} ({answer.get('mime_type')}, {size_mb} MB) into the group "
            f"{_group_name(ctx)} as {who}: kept in the group's files and {reach}. It is a group "
            f"message of its own, already shown; it is not attached to your answer. "
            f"{count} of {MAX_FILES_PER_TURN} files this turn.")


def _card_mismatch(ctx: ToolContext, card: Optional[Dict[str, Any]], name: str, size: int,
                   sha256: str) -> Optional[str]:
    """Why this call may not post the file it checked, or None (Zcode #176): a call the owner
    approved for someone else posts only the file the card showed, by name, size and hash. The
    owner's own call carries no card. A foreign call with no card behind it is refused."""
    from ..owner_approval import asker_is_owner
    if asker_is_owner(ctx):
        return None
    if not card:
        return "the owner's card for this call named no file, so nothing it did not show is sent."
    if card.get("name") == name and card.get("size") == size and card.get("sha256") == sha256:
        return None
    return (f"the file is not the one the owner's card showed ({card.get('name')}, "
            f"{card.get('size')} bytes, sha256 {str(card.get('sha256'))[:16]}...): it changed "
            f"after the card, so the approval does not cover it.")


def _take_source(ctx: ToolContext, take_id: str) -> Union[str, Tuple[Path, Dict[str, Any]]]:
    """(the take's own file, its music block), or the refusal: core resolves the id through
    `get_music_take` (D44), and only a finished take the person has not discarded is posted."""
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
    return Path(take["path"]), music


def _post_take(ctx: ToolContext, take_id: str, caption: Optional[str]) -> str:
    """Post a music take by its id: core gives the take's own file (`get_music_take`, D44),
    so neither `_resolve_file_path` nor the DPC-home rule applies to a path the agent never
    named; every other limit does (post_file)."""
    found = _take_source(ctx, take_id)
    if isinstance(found, str):
        return found
    source, music = found
    return post_file(ctx, source, caption, music=music)


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
                    "Post a file into the chat you are answering in: in your own 1:1 chat it is "
                    "attached to your answer (an audio file plays in the message); in a group it "
                    "is sent to every member as a group file message of its own, under your name, "
                    "right away. Use it only when the person asks for the file. The file is "
                    "copied, never moved. Limits: at most "
                    f"{MAX_FILES_PER_TURN} files and {MAX_BYTES_PER_TURN // (1024 * 1024)} MB in all "
                    "per turn; the file-transfer size cap "
                    "(file_transfer.max_size_mb, 100 MB by default); only in DPC (a Telegram or "
                    "Discord turn is refused); in a group, a file from a store the group may not "
                    "see (your private memory, for one) is refused; the path is resolved as "
                    "read_file resolves it (relative to your sandbox, or an absolute path your "
                    "extended read access reaches). Only these file types are posted, checked by their content as "
                    f"well as their name: {ALLOWED_TYPES_TEXT}; anything else is refused. A music take is posted by its take_id "
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
            owner_approval_for_others=True,  # ADR-053: was owner_only (ADR-051 D43)
        ),
    ]


# ADR-053: chat_post_file asks the owner when someone else asked for the turn (it was
# owner_only, ADR-051 D43). The card names the file or the take.
def _chat_post_file_approval(ctx: ToolContext, args: Dict[str, Any]):
    from ..owner_approval import Ask, Exempt, Refuse, clip
    if place_refusal(ctx):
        return Exempt("a chat the tool never posts into; it refuses with its reason")
    caption = str(args.get("caption") or "")
    _CARD_BINDING.set(None)
    if in_group(ctx):
        # D50 G: the card says what reaches every member: the file, its size and type, the group.
        described = _group_post_card(ctx, args)
        if described.startswith("⚠️"):
            return Refuse(described)
        return Ask(clip(described + (f", with the caption: {caption}" if caption else "")))
    what = (f"take {args['take_id']}" if args.get("take_id")
            else f"the file {args.get('path') or '(none named)'}")
    return Ask(clip(f"post {what} into the chat" + (f", with the caption: {caption}" if caption else "")))


def _group_post_card(ctx: ToolContext, args: Dict[str, Any]) -> str:
    """"What exactly" for a foreign request to post into a group, or the refusal the tool would
    give anyway (then no card is raised): a store closed to the group's audience, a type off
    the allow-list, a file over the group cap, a take that is not postable."""
    path = args.get("path").strip() if isinstance(args.get("path"), str) else args.get("path")
    take_id = args.get("take_id").strip() if isinstance(args.get("take_id"), str) else args.get("take_id")
    if path and take_id:
        return _refusal("give either path or take_id, not both.")
    if take_id:
        found = _take_source(ctx, take_id)
        if isinstance(found, str):
            return found
        source, _music = found
        what = f"take {take_id}, the file {source.name}"
    elif path:
        try:
            source = _resolve_file_path(ctx, path)
        except PermissionError as e:
            return _refusal(f"{e}.")
        home_refusal = _dpc_home_refusal(ctx, source)
        if home_refusal:
            return home_refusal
        what = f"the file {source.name} ({source})"
    else:
        return _refusal("give path (a file) or take_id (a music take).")
    if not source.is_file():
        return _refusal(f"'{source}' is not a file.")
    bad = content_refusal(source)
    if bad:
        return _refusal(bad)
    too_large = _group_size_refusal(ctx, source.stat().st_size)
    if too_large:
        return _refusal(too_large)
    try:
        sha256, size, head = file_digest(source)
    except OSError as e:
        return _refusal(f"the file could not be read to describe it ({e.__class__.__name__}).")
    bad = head_refusal(source.name, head)
    if bad:
        return _refusal(bad)
    too_large = _group_size_refusal(ctx, size)
    if too_large:
        return _refusal(too_large)
    # What the card shows is what the approved call may post (Zcode #176).
    _CARD_BINDING.set({"name": source.name, "size": size, "sha256": sha256})
    mime = mimetypes.guess_type(str(source))[0] or "application/octet-stream"
    return (f"post {what}, {round(size / (1024 * 1024), 2)} MB ({size} bytes), {mime}, sha256 "
            f"{sha256[:16]}, into the group {_group_name(ctx)}, where every member receives it")


_get_chat_file_tools_unflagged = get_tools


def get_tools() -> List[ToolEntry]:  # noqa: F811
    from .registry import mark_owner_approval
    return mark_owner_approval(_get_chat_file_tools_unflagged(),
                               {"chat_post_file": _chat_post_file_approval})
