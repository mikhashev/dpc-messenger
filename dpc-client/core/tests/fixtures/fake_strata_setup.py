"""A stand-in for Strata Control's setup.py, copied into a fake Control root as `setup.py`.

It prints the kinds of line the real one does (`=== Step N ===`, `[ok]`, `[!]`, a redrawn
`\r  label: x / y GB (p%)` progress line, `[X]` plus an indented hint), writes `strata-<id>.json`
into STRATA_INSTALL_DIR and records how it was started in `seen.json` beside itself. The
behaviour is picked by `mode.txt` beside it: ok (default), fail, refused, argparse, crash, nofile, hang.
"""
import json
import os
import sys
import time
from pathlib import Path

SHARD_1 = "Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf"
SHARD_2 = "Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00002-of-00002.gguf"

here = Path(__file__).resolve().parent
mode = (here / "mode.txt").read_text().strip() if (here / "mode.txt").exists() else "ok"
argv = sys.argv[1:]
watched = ["PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONNOUSERSITE", "PYTHONUNBUFFERED"]
seen = {
    "argv0": sys.argv[0],
    "argv": argv,
    "cwd": os.getcwd(),
    "strata_env": {k: v for k, v in os.environ.items() if k.upper().startswith("STRATA_")},
    "env": {k: os.environ.get(k) for k in watched},
    "stdin_at_eof": sys.stdin.read() == "",
    "isolated": sys.flags.isolated,
}
(here / "seen.json").write_text(json.dumps(seen), encoding="utf-8")


def say(text=""):
    print(text, flush=True)


def progress(label, have, total, pause=0.04):
    sys.stdout.write("\r  %s: %6.2f / %.2f GB (%d%%)   " % (label, have, total, int(100 * have / total)))
    sys.stdout.flush()
    time.sleep(pause)


def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None


family, model = flag("--family"), flag("--model")
tag = (("" if family == "qwen" else family + "-") + model).lower()

say("Strata - Qwen3.8-Flash-Next on a normal PC (a GPU + system RAM + CPU)")
say()
say("=== Step 1: checking your PC ===")
say("  [ok] NVIDIA GPU: a card")
say("  [!]  little free disk space: 40 GB free for about 90 GB of files")

if mode == "hang":
    time.sleep(120)
if mode == "fail":
    say()
    say("=== Step 2: checking free disk space ===")
    say()
    say("  [X]  not enough free disk space in D:/models: need ~90 GB, 40 GB free")
    say("       use --data-dir on a bigger drive")
    say()
    say("Setup stopped. Fix the item above and run it again - everything already done is kept and skipped.")
    sys.exit(1)
if mode == "refused":
    say()
    say("  [X]  an install or build step (pip) is not allowed here")
    sys.exit(5)
if mode == "argparse":
    sys.stderr.write("usage: setup.py [-h] [--family F]\nsetup.py: error: argument --model: invalid choice: 'x'\n")
    sys.exit(2)
if mode == "crash":
    raise RuntimeError("boom in setup")

say()
say("=== Step 5: downloading Qwen3.8-Flash-Next IQ3_S ===")
for have in (1.0, 2.0, 3.0, 2.0, 4.0):  # 2.0 after 3.0: a download that restarts
    progress(SHARD_1, have, 4.0)
say()
sys.stdout.write("\r  checking %s: 4.00 / 4.00 GB (100%%)   " % SHARD_1)
sys.stdout.flush()
say()
for have in (1.0, 2.0):
    progress(SHARD_2, have, 2.0)
say()
say("  [ok] model files present")
for have in (1.0, 5.0):
    print("  MTP draft layer: %6.2f / 5.00 GB (%d%%)" % (have, int(100 * have / 5.0)), file=sys.stderr, flush=True)
    time.sleep(0.04)
progress("vision encoder", 0.9, 0.9)
say()
if mode != "nofile":
    out = Path(os.environ["STRATA_INSTALL_DIR"])
    out.mkdir(parents=True, exist_ok=True)
    (out / ("strata-%s.json" % tag)).write_text(json.dumps({"exe": "component:strata-engine", "args": []}))
say("All set.")
