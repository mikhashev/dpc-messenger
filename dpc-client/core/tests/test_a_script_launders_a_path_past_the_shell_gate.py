"""`write_file` then `python x.py`: two Tier-0 operations that composed into a
read of anything the service could open, because the classifier saw the command
string and the path lived in the file.

Falsifier for the whole file: neutralise the `_script_paths_out_of_sandbox`
call in `_validate_command` and the four cases below that assert Tier 1 go
green-to-red; the three that assert ordinary work stays Tier 0 must not move.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dpc_client_core.dpc_agent.tools.shell import _validate_command  # noqa: E402


class _Firewall:
    def __init__(self, whitelist):
        self._whitelist = whitelist

    def get_tool_setting(self, *args, **kwargs):
        return self._whitelist


class _Ctx:
    """A sandbox that is one directory, which is what the real one resolves to."""

    def __init__(self, sandbox, whitelist=None):
        self.firewall = _Firewall(whitelist or [])
        self.agent_root = str(sandbox)
        self._sandbox = Path(sandbox).resolve()

    def validate_extended_path(self, path):
        resolved = Path(os.path.expanduser(str(path)))
        try:
            resolved = resolved.resolve()
        except OSError:
            pass
        if self._sandbox not in resolved.parents and resolved != self._sandbox:
            raise PermissionError(f"{path} is outside {self._sandbox}")
        return True


def _script(sandbox, name, body):
    p = Path(sandbox) / name
    p.write_text(body, encoding="utf-8")
    return p


def test_the_shape_that_reached_the_gold(tmp_path):
    """The measured one: a script written this turn, then run by name."""
    _script(tmp_path, "gaia_ans.py", "import pandas\npandas.read_parquet(r'C:\\\\Users\\\\mikha\\\\.cache\\\\gold.parquet')\n")
    ctx = _Ctx(tmp_path)

    verdict = _validate_command("python gaia_ans.py", ctx, str(tmp_path))

    assert verdict is not None, "a script naming a path outside the sandbox ran unasked"
    assert verdict[0] == "tier1"
    assert "gaia_ans.py" in verdict[1] and "gold.parquet" in verdict[1], verdict[1]


def test_a_posix_path_inside_a_shell_script(tmp_path):
    _script(tmp_path, "grab.sh", "cat /etc/shadow\n")
    ctx = _Ctx(tmp_path)

    verdict = _validate_command("sh grab.sh", ctx, str(tmp_path))

    assert verdict is not None and verdict[0] == "tier1"
    assert "/etc/shadow" in verdict[1], verdict[1]


def test_the_script_is_found_through_a_chain(tmp_path):
    """The observed command was `cd <dir> && python gaia_ans.py`, not the bare call."""
    _script(tmp_path, "gaia_ans.py", "open('/var/secrets/key')\n")
    ctx = _Ctx(tmp_path)

    verdict = _validate_command(f"cd {tmp_path} && python gaia_ans.py", ctx, str(tmp_path))

    assert verdict is not None and verdict[0] == "tier1", verdict


def test_a_script_that_stays_inside_the_sandbox_is_still_tier_zero(tmp_path):
    """The non-regression that matters: ordinary agent work must not need a person."""
    _script(tmp_path, "work.py", "open('out.txt', 'w').write('hi')\n")
    ctx = _Ctx(tmp_path)

    assert _validate_command("python work.py", ctx, str(tmp_path)) is None


def test_a_script_that_does_not_exist_is_left_to_the_other_rules(tmp_path):
    """Nothing to read is not a refusal: the command fails on its own."""
    ctx = _Ctx(tmp_path)

    assert _validate_command("python missing.py", ctx, str(tmp_path)) is None


def test_a_script_outside_the_sandbox_is_never_opened_to_decide(tmp_path):
    """A gate that reads files elsewhere is the hole it is checking for.

    The command still needs approval — the existing path rule sees the path in
    the command string — and the reason must be that rule's, not this one's.
    """
    outside = tmp_path.parent / "outside.py"
    outside.write_text("open('/etc/shadow')\n", encoding="utf-8")
    sandbox = tmp_path / "box"
    sandbox.mkdir()
    ctx = _Ctx(sandbox)

    verdict = _validate_command(f"python {outside}", ctx, str(sandbox))

    assert verdict is not None and verdict[0] == "tier1"
    assert "Command accesses path outside sandbox" in verdict[1], verdict[1]


def test_an_unreadable_script_is_refused_rather_than_waved_on(tmp_path):
    """A gate that cannot see says so."""
    p = _script(tmp_path, "opaque.py", "print(1)\n")
    ctx = _Ctx(tmp_path)

    import dpc_client_core.dpc_agent.tools.shell as shell_mod

    real_open = shell_mod.open if hasattr(shell_mod, "open") else open

    def _boom(path, *a, **kw):
        if str(path) == str(p):
            raise OSError("locked")
        return real_open(path, *a, **kw)

    shell_mod.open = _boom  # type: ignore[attr-defined]
    try:
        verdict = _validate_command("python opaque.py", ctx, str(tmp_path))
    finally:
        del shell_mod.open

    assert verdict is not None and verdict[0] == "tier1"
    assert "could not read" in verdict[1], verdict[1]
