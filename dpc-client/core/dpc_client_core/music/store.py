"""The music library on disk: `<DPC home>/music/` (SPEC §6 "Library and playlists").

```
<DPC home>/music/
  library.json     [mp.library_entry/1]
  folders.json     [mp.music_folder/1]
  playlists.json   [mp.playlist/1]   user lists only; smart lists are rules, computed on read
  files/           imported single files (not written by this change)
  recordings/      recordings, flow 23 (not written by this change)
```

Each file is a JSON array, replaced whole in one step (a temp file in the same folder,
fsynced, then `os.replace`), so a crash leaves the old file or the new one. A file that
does not parse is moved aside as `<name>.corrupt-<UTC time>` and the list starts empty; a
record that fails its schema is kept aside in `<name>.rejected-<UTC time>` rather than
dropped, so nothing the person had is lost silently.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List

from .schemas import SchemaError

logger = logging.getLogger(__name__)

MUSIC_DIR_NAME = "music"
LIBRARY_FILE = "library.json"
FOLDERS_FILE = "folders.json"
PLAYLISTS_FILE = "playlists.json"
FILES_DIR = "files"
RECORDINGS_DIR = "recordings"

_REPLACE_TRIES = 20
_REPLACE_RETRY_S = 0.05


def music_dir_of(dpc_home_dir: Path) -> Path:
    return Path(dpc_home_dir) / MUSIC_DIR_NAME


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def atomic_write_json(path: Path, data: Any) -> None:
    """Replace `path` with `data` as JSON in one step.

    Windows refuses `os.replace` while another process holds the target open, so the
    replace is retried briefly, as `conversation_monitor.atomic_write_text` does."""
    text = json.dumps(data, ensure_ascii=False, indent=1)
    atomic_write_text(path, text)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(_REPLACE_RETRY_S)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_records(path: Path, validate: Callable[[Any], None]) -> List[dict]:
    """The valid records of one list file; [] when it does not exist."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"expected a JSON array, got {type(data).__name__}")
    except (ValueError, UnicodeDecodeError) as exc:
        aside = path.with_name(f"{path.name}.corrupt-{_stamp()}")
        logger.error("music: %s does not parse (%s); moved aside to %s and starting empty", path, exc, aside.name)
        os.replace(path, aside)
        return []
    good: List[dict] = []
    rejected: List[Any] = []
    for record in data:
        try:
            validate(record)
        except SchemaError as exc:
            logger.warning("music: %s: a record fails its schema and is kept aside: %s", path.name, exc)
            rejected.append(record)
            continue
        good.append(record)
    if rejected:
        atomic_write_json(path.with_name(f"{path.name}.rejected-{_stamp()}"), rejected)
    return good
