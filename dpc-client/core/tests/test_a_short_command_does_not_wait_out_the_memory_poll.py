"""A command that ends at once must return at once under the memory ceiling.

`run_supervised` joined its memory watcher after `communicate()`, and the
watcher was asleep for a whole poll interval, so every supervised call cost up
to `_MEMORY_POLL_SECONDS` more than the command itself. Measured 2026-09-29 on
`read_document` over a DjVu: 2.02 s added to each djvutxt and djvused call,
about 4 s a page, 4144 ms against 58 ms once the watcher is woken.
"""

import sys
import time

from dpc_client_core.dpc_agent.tools import process as process_tool


def test_a_command_that_ends_at_once_is_not_held_for_a_poll_interval(monkeypatch):
    # A poll far longer than any honest spawn, so the assertion cannot pass by luck.
    monkeypatch.setattr(process_tool, "_MEMORY_POLL_SECONDS", 8.0)

    started = time.perf_counter()
    run = process_tool.run_supervised(
        [sys.executable, "-c", "print('ok')"],
        launcher="test(short command)",
        timeout=30,
        ceiling_mb=4096,
    )
    elapsed = time.perf_counter() - started

    assert run.returncode == 0 and run.stdout.strip() == "ok"
    assert elapsed < 4.0, f"the call waited out the memory poll: {elapsed:.1f} s"
