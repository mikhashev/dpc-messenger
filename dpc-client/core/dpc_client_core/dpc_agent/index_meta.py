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


def write_meta(path: pathlib.Path, doc: dict) -> None:
    """Replace the file in one step, so no reader can see it half-written.

    `load()` catches its own parse failure and returns False, which reads as an empty
    index rather than as an error — a torn read is therefore silent recall of nothing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
