"""Remove the desktop recorder's abandoned WAVs from `~/.dpc/temp`.

NOTHING-CLEANS-THE-DPC-TEMP-FOLDER-SO-ABANDONED-RECORDINGS-PILE-UP: the recorder
(`ui/src-tauri/src/audio_recorder.rs`) writes `voice_<unix seconds>.wav` into
`~/.dpc/temp`; the UI removes it after a send or a discard and core moves it out
for an agent chat (`keep_agent_chat_attachment`), but a recording core never
confirmed, a discard whose remove failed, or a crash mid-send stays forever.

Only that name is removed. `~/.dpc/temp` is not DPC's alone: agents use it as a
working folder (scripts, notes, downloaded PDFs, probe output an ADR cites), and
the UI keeps a Telegram screenshot there that the chat history then points at.
A sweep of every old file would delete those. Only regular files at the top
level are considered; a symlink is never followed or removed and a directory is
never entered.
"""

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The recorder's file name (audio_recorder.rs: format!("voice_{}.wav", timestamp)).
RECORDING_NAME_RE = re.compile(r"voice_\d+\.wav")
# A recording younger than this is never removed: it may still be in a send.
ABANDONED_RECORDING_AGE_S = 24 * 60 * 60


def sweep_abandoned_recordings(temp_dir: Path, max_age_s: float = ABANDONED_RECORDING_AGE_S,
                               now: Optional[float] = None) -> int:
    """Remove recorder WAVs in `temp_dir` last modified more than `max_age_s` ago.

    Blocking (run it with asyncio.to_thread). Never raises for a file: one held
    open (PermissionError on Windows), gone meanwhile, or otherwise refused is
    skipped. Answers how many were removed.
    """
    now = time.time() if now is None else now
    removed = 0
    try:
        entries = list(os.scandir(temp_dir))
    except FileNotFoundError:
        return 0
    except OSError as e:
        logger.warning("Could not list %s for abandoned recordings: %s", temp_dir, e)
        return 0
    for entry in entries:
        if not RECORDING_NAME_RE.fullmatch(entry.name):
            continue
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            if now - entry.stat(follow_symlinks=False).st_mtime <= max_age_s:
                continue
            os.unlink(entry.path)
        except OSError as e:
            logger.debug("Abandoned recording %s left in place: %s", entry.path, e)
            continue
        removed += 1
    logger.info("Removed %d abandoned recording(s) older than %.0f h from %s",
                removed, max_age_s / 3600, temp_dir)
    return removed
