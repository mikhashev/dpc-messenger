"""The memory ceiling is a config.ini key, and the next command reads it.

Until 2026-10-25 the ceiling came from `DPC_SHELL_MEMORY_LIMIT_MB` alone, read
once at import: the owner could neither see the value the running backend held
nor change it without `setx` and a restart. Mike's call, 2026-10-25: a key in
`[system]`, applied at once, with the usual order — environment, then
config.ini, then the built-in 8192.

`_read_memory_ceiling` is imported inside the tests, as in
`test_the_class_and_not_only_the_instance.py`, so a falsification run against
the env-only reader reports each claim on its own.
"""

import logging
import os
import sys

import pytest

from dpc_client_core.dpc_agent.tools import process as process_mod
from dpc_client_core.dpc_agent.tools import shell as shell_tool

PROCESS_LOGGER = "dpc_client_core.dpc_agent.tools.process"

# The grandchild shape the ceiling exists for; 20 MB every 0.2 s.
HUNGRY_GRANDCHILD = (
    'python -c "import subprocess, sys, time; '
    "subprocess.Popen([sys.executable, '-c', "
    "'import time\\nb=[]\\nwhile True:\\n    b.append(bytearray(20*1024*1024))\\n    time.sleep(0.2)']); "
    'time.sleep(300)"'
)


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A DPC home of its own, and no environment override."""
    monkeypatch.delenv("DPC_SHELL_MEMORY_LIMIT_MB", raising=False)
    monkeypatch.delenv("DPC_SYSTEM_SHELL_MEMORY_LIMIT_MB", raising=False)
    monkeypatch.setenv("DPC_HOME", str(tmp_path))
    # Asserted, not set: a value fixed at import is the defect under test.
    assert process_mod._MEMORY_CEILING_MB is None
    assert shell_tool._MEMORY_CEILING_MB is None
    monkeypatch.setattr(process_mod, "_bad_ceilings_announced", set(), raising=False)
    monkeypatch.setattr(process_mod, "_config_cache", (None, None), raising=False)
    return tmp_path


_edits = [0]


def write_config(home, value=None, body=None):
    """Write config.ini as a later edit: its mtime moves even within one tick."""
    path = home / "config.ini"
    if body is None:
        body = "[hub]\nurl = http://localhost:8000\n\n[system]\nauto_collect_device_info = true\n"
        if value is not None:
            body += f"shell_memory_limit_mb = {value}\n"
    path.write_text(body)
    _edits[0] += 1
    stamp = 1_700_000_000_000_000_000 + _edits[0] * 1_000_000_000
    os.utime(path, ns=(stamp, stamp))
    return path


def test_config_beats_the_default(home):
    from dpc_client_core.dpc_agent.tools.process import _read_memory_ceiling
    write_config(home, 1000)
    assert _read_memory_ceiling() == 1000


def test_the_default_holds_when_the_key_is_absent(home):
    from dpc_client_core.dpc_agent.tools.process import _read_memory_ceiling
    write_config(home)
    assert _read_memory_ceiling() == 8192


def test_an_inline_comment_is_not_part_of_the_value(home):
    """Settings reads config.ini with `#` inline comments; so does this."""
    from dpc_client_core.dpc_agent.tools.process import _read_memory_ceiling
    write_config(home, "1500  # roomier for builds")
    assert _read_memory_ceiling() == 1500


def test_the_environment_beats_config(home, monkeypatch):
    from dpc_client_core.dpc_agent.tools.process import _read_memory_ceiling
    write_config(home, 1000)
    monkeypatch.setenv("DPC_SHELL_MEMORY_LIMIT_MB", "2000")
    assert _read_memory_ceiling() == 2000


def test_the_generic_name_beats_config_and_the_old_name_beats_it(home, monkeypatch):
    """`DPC_<SECTION>_<KEY>` is what every other key accepts; the old name is
    what machines already have set, so it stays on top."""
    from dpc_client_core.dpc_agent.tools.process import _read_memory_ceiling
    write_config(home, 1000)
    monkeypatch.setenv("DPC_SYSTEM_SHELL_MEMORY_LIMIT_MB", "3000")
    assert _read_memory_ceiling() == 3000
    monkeypatch.setenv("DPC_SHELL_MEMORY_LIMIT_MB", "2000")
    assert _read_memory_ceiling() == 2000


def test_a_change_to_config_is_seen_by_the_next_read_without_reimport(home):
    from dpc_client_core.dpc_agent.tools.process import _read_memory_ceiling
    write_config(home, 1000)
    assert _read_memory_ceiling() == 1000
    write_config(home, 3000)
    assert _read_memory_ceiling() == 3000
    write_config(home)
    assert _read_memory_ceiling() == 8192, "a removed key falls back to the default"


def test_a_change_to_config_reaches_the_ceiling_a_run_is_held_to(home):
    """The value has to reach its consumer, not only the reader."""
    write_config(home, 1000)
    first = process_mod.run_supervised([sys.executable, "-c", "pass"], launcher="test", timeout=30)
    write_config(home, 3000)
    second = process_mod.run_supervised([sys.executable, "-c", "pass"], launcher="test", timeout=30)
    assert (first.ceiling_mb, second.ceiling_mb) == (1000, 3000)


def test_zero_in_config_turns_the_ceiling_off(home):
    from dpc_client_core.dpc_agent.tools.process import _read_memory_ceiling
    write_config(home, 0)
    assert _read_memory_ceiling() == 0
    run = process_mod.run_supervised([sys.executable, "-c", "pass"], launcher="test", timeout=30)
    assert run.ceiling_mb == 0


def test_a_bad_config_value_falls_back_and_warns_once(home, caplog):
    from dpc_client_core.dpc_agent.tools.process import _read_memory_ceiling
    write_config(home, "lots")
    with caplog.at_level(logging.WARNING, logger=PROCESS_LOGGER):
        assert [_read_memory_ceiling() for _ in range(5)] == [8192] * 5
    warnings = [r for r in caplog.records if "'lots'" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert "shell_memory_limit_mb" in warnings[0].getMessage()

    caplog.clear()
    write_config(home, "more")
    with caplog.at_level(logging.WARNING, logger=PROCESS_LOGGER):
        assert _read_memory_ceiling() == 8192
    assert any("'more'" in r.getMessage() for r in caplog.records), "a new bad value is news"


def test_a_file_that_is_not_ini_never_raises(home):
    from dpc_client_core.dpc_agent.tools.process import _read_memory_ceiling
    write_config(home, body="this is not = an ini file\n[[[\n")
    assert _read_memory_ceiling() == 8192


def test_a_file_caught_mid_write_keeps_the_last_value(home):
    """`Settings.set()` truncates before it writes; a read in between sees an
    empty file, and that must not drop the ceiling to the default."""
    from dpc_client_core.dpc_agent.tools.process import _read_memory_ceiling
    write_config(home, 1000)
    assert _read_memory_ceiling() == 1000
    write_config(home, body="")
    assert _read_memory_ceiling() == 1000
    write_config(home, 3000)
    assert _read_memory_ceiling() == 3000


def test_run_shell_obeys_a_ceiling_set_only_in_config(home):
    """End to end through the shell tool, with nothing patched but the home."""
    write_config(home, 200)
    result = shell_tool._execute_shell_command(HUNGRY_GRANDCHILD, None, 120)
    assert "over the 200 MB ceiling" in result, result[:300]
    assert "shell_memory_limit_mb in [system] of config.ini" in result
