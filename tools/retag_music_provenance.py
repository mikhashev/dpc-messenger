"""Re-tag existing generated music files with the full `mp.provenance/1` record.

    cd dpc-client/core
    uv run python ../../tools/retag_music_provenance.py --dry-run ROOT [ROOT ...]
    uv run python ../../tools/retag_music_provenance.py ROOT [ROOT ...]

Each ROOT is a folder (walked for .wav and .flac, derived `level-matched*` copies skipped) or one
file. `--dry-run` builds every record, writes nothing, and reports the sizes. The logic and the
rules (never guess, PCM md5 verified, write once) are in
`dpc_client_core/music/provenance_retag.py`; the record is
`ai-studio/experiments/music-player/docs/PROVENANCE.md`.

Set `PYTHONUTF8=1` on Windows. Exit status 1 when any file is an error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("roots", nargs="+", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="build and report, write nothing")
    ap.add_argument("--dpc-home", type=Path, default=Path.home() / ".dpc",
                    help="where music/jobs/<job>/payload/ is looked for (default ~/.dpc)")
    ap.add_argument("--no-replay-files", action="store_true",
                    help="do not read the yue2 trial's replay request files named in a sidecar")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)

    from dpc_client_core.music import provenance_retag as rt

    results = rt.retag_tree(args.roots, dry_run=args.dry_run, dpc_home=args.dpc_home,
                            use_replay_files=not args.no_replay_files)
    summary = rt.summarize(results)
    if args.json:
        print(json.dumps({"summary": summary, "files": [r.__dict__ for r in results]}, ensure_ascii=False, indent=1))
    else:
        for r in results:
            extra = f" ({r.record_bytes} bytes)" if r.record_bytes else ""
            why = f": {r.reason}" if r.reason else ""
            print(f"{r.status:10} {r.container:4} {r.file}{extra}{why}")
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
