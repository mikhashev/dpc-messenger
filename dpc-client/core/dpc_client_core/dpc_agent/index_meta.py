"""`index_meta.json` has two writers, and this is the rule that keeps them apart.

The file is shared. The vector index owns `header`'s own fields and `chunks`; the
per-agent sync in `agent_manager` owns `file_hashes` and `header.key_format`. Both used
to write the document whole from their own picture of it, so whichever ran last erased
the other's half.

Both directions were observed on 2026-08-12. `file_hashes` gone after a
knowledge-commit reindex, which makes the next start re-embed the whole pool — 19 such
starts on this machine, up to 2256 s of CPU on the node without a GPU. And the reverse:
a `chunks` list read at the beginning of a sync written back on top of the fresh one the
save had just produced. That half is not a cost, it is wrong answers — search maps a row
number to `chunks[i]`, so a short list makes later rows unreachable and earlier rows
point at other documents. The one native-backend agent held 328 vectors against 23
chunks.

So: read the file, change only your own keys, write it back atomically. Whole-document
writes are what did the damage; a merge is the whole fix.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib

log = logging.getLogger(__name__)


def read_meta(path: pathlib.Path) -> dict:
    """The document as stored, or an empty one — never a partial one.

    A missing file and an unreadable file are the same answer on purpose: both mean
    "nothing of yours is in here", and the caller's own keys are about to be written
    anyway. Losing a foreign key this way is possible and is the reason the write is
    atomic — a reader should never meet a half-written document in the first place.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("index meta at %s unreadable, treating as empty: %s", path, e)
        return {}
    return doc if isinstance(doc, dict) else {}


_REPLACE_ATTEMPTS = 20
_REPLACE_PAUSE = 0.05


def replace_when_the_readers_let_go(tmp: pathlib.Path, path: pathlib.Path) -> None:
    """Move the finished file into place, waiting out a reader that has it open.

    On POSIX a rename over an open file is fine — the reader keeps the old inode. On
    Windows it is `PermissionError: [WinError 5]`, and the readers here are the ones
    that matter: recall opens the index on every message. Found by the concurrent-reader
    test, which failed on the very first run of the "trivially atomic" version.

    A reader holds the file for milliseconds, so a second of patience covers it many
    times over, and the wait costs nobody anything — every writer of these files runs on
    the index writer's own thread. If the wait is not enough the exception is raised: the
    save is lost and the stored index stays whole, which the next pass repairs. That is
    the right way round — a lost save is visible in the counts, a torn file is not.
    """
    import time

    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                log.warning("could not replace %s — a reader held it for %.1fs",
                            path.name, _REPLACE_ATTEMPTS * _REPLACE_PAUSE)
                raise
            time.sleep(_REPLACE_PAUSE)


def atomic_write_text(path: pathlib.Path, text: str) -> None:
    """Replace a file in one step, so no reader can see it half-written.

    Every reader of an index file answers a parse failure the same way — `load()`
    catches it and returns False, which reads as "no index" rather than as an error.
    So a torn read is not a crash anybody investigates; it is one turn of recall
    silently returning nothing. The writers are serialised against each other by the
    index writer, but readers are not serialised against anyone: they only read, and
    there is nothing for them to hold.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    replace_when_the_readers_let_go(tmp, path)


def atomic_write_bytes(path: pathlib.Path, write: "callable") -> None:
    """The same, for a writer that insists on producing the file itself.

    `faiss.write_index` takes a path, not a buffer, so it writes into a temporary name
    and the finished file is moved into place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    write(str(tmp))
    replace_when_the_readers_let_go(tmp, path)


def write_meta(path: pathlib.Path, doc: dict) -> None:
    """Replace the meta document in one step. See `atomic_write_text`."""
    atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=2))
