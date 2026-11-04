"""The tree kill must not need the console of the process that asks for it.

With its console host gone a process keeps running, but a console program it starts
with that console inherited dies at start with 0xC0000142. `taskkill` was started
so: the ceiling fired, the kill never ran, and one allocator reached 27.6 GiB.
The host killed here is the one the test's own child got.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the console host is Windows'")

CORE = Path(__file__).resolve().parents[1]

CHILD = textwrap.dedent("""
    import json, subprocess, sys, time
    sys.path.insert(0, sys.argv[1])
    import psutil
    from dpc_client_core.dpc_agent.tools.process import CREATE_NO_WINDOW, _kill_process_tree

    def pythons(root):
        return [p for p in root.children(recursive=True) if p.name().lower() == "python.exe"]

    me = psutil.Process()
    hosts = [c for p in (me, me.parent()) for c in p.children() if c.name().lower() == "conhost.exe"]
    for host in hosts:
        host.kill()
    psutil.wait_procs(hosts, timeout=5)
    bare = subprocess.run(["taskkill", "/?"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    tree = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time; "
         "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); time.sleep(30)"],
        creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
    )
    root = psutil.Process(tree.pid)
    deadline = time.monotonic() + 20
    while len(pythons(root)) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    procs = [root] + pythons(root)
    said = _kill_process_tree(tree)
    gone, alive = psutil.wait_procs(procs, timeout=10)
    with open(sys.argv[2], "w") as fh:
        json.dump({"hosts": [h.pid for h in hosts], "bare_rc": bare.returncode, "said": said,
                   "tree": len(procs), "alive": [p.pid for p in alive]}, fh)
""")


def test_the_tree_dies_and_the_sentence_says_so_with_the_console_host_gone(tmp_path):
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    out = tmp_path / "report.json"

    done = subprocess.run(
        [sys.executable, str(script), str(CORE), str(out)],
        creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=90,
    )

    assert out.exists(), done.stderr[-800:]
    report = json.loads(out.read_text())
    assert report["hosts"], "the child had no console host to lose: this test proved nothing"
    assert report["bare_rc"] == 0xC0000142, "the premise: an inheriting child dies at start"
    assert report["tree"] == 4, f"the launcher, its python, and their child's two: {report}"
    assert report["alive"] == [], f"survived the kill: {report}"
    assert report["said"].startswith("the command and the descendants its parent links reach were killed"), report
