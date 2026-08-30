"""`write_file` then `python x.py`: two Tier-0 operations that composed into a
read of anything the service could open, because the classifier saw the command
string and the path lived in the file.

Falsifier for the whole file: neutralise the `_script_paths_out_of_sandbox`
call in `_validate_command` and the six cases that need the new check go
green-to-red; the four that pin ordinary work and the old rule must not move.
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


def test_a_script_run_by_its_shebang(tmp_path):
    """A script run without naming an interpreter.

    The backslash spelling is the discriminating one: `./x.py` is caught by the
    older command-string rule anyway, because `/x.py` looks to it like an
    absolute POSIX path — the right verdict for the wrong reason.
    """
    _script(tmp_path, "x.py", "open('/etc/shadow')\n")
    ctx = _Ctx(tmp_path)

    verdict = _validate_command(r".\x.py", ctx, str(tmp_path))

    assert verdict is not None and verdict[0] == "tier1", verdict
    assert "Script" in verdict[1], verdict[1]


def test_a_versioned_interpreter(tmp_path):
    """`python3.12 x.py` is the ordinary spelling on a distro with several."""
    _script(tmp_path, "x.py", "open('/etc/shadow')\n")
    ctx = _Ctx(tmp_path)

    verdict = _validate_command("python3.12 x.py", ctx, str(tmp_path))

    assert verdict is not None and verdict[0] == "tier1", verdict


def test_reading_a_file_is_not_running_it(tmp_path):
    """`cat x.py` names a script and runs nothing — it must stay Tier 0."""
    _script(tmp_path, "x.py", "open('/etc/shadow')\n")
    ctx = _Ctx(tmp_path)

    assert _validate_command("cat x.py", ctx, str(tmp_path)) is None


# --- what the first production night refused, and should not have ----------
# 2026-08-30, campaign 20260830-0321: four firings, four false positives, all
# XPath prefixes from scripts unpacking the tasks' own docx and pptx.


def test_an_xpath_prefix_is_not_a_path(tmp_path):
    _script(tmp_path, "extract.py", "for n in root.iter('.//w:t'):\n    print(n.text)\n")
    ctx = _Ctx(tmp_path)

    assert _validate_command("python extract.py", ctx, str(tmp_path)) is None


def test_a_url_inside_a_script_is_not_a_path(tmp_path):
    _script(tmp_path, "fetch.py", "urlopen('https://journals.le.ac.uk/index.php/jist')\n")
    ctx = _Ctx(tmp_path)

    assert _validate_command("python fetch.py", ctx, str(tmp_path)) is None


def test_a_single_segment_root_is_not_enough(tmp_path):
    """`/best` is a yt-dlp format selector, not a directory."""
    _script(tmp_path, "dl.py", "opts = {'format': '/best'}\n")
    ctx = _Ctx(tmp_path)

    assert _validate_command("python dl.py", ctx, str(tmp_path)) is None


# --- what two independent reviews found the gate could not see ------------
# 2026-08-30, Fable 5 and GLM 5.3 on the eval traces: the agent wrote `dl.bat`
# around a curl the gate had refused. It never ran it. Measured on this tree the
# same morning, before the fix: `dl.bat`, `.\dl.bat`, `dl.cmd`, `call dl.bat`,
# `start dl.bat` and a bare `grab.py` were all Tier 0 — a batch extension the
# pattern list did not carry, and a shebang rule that demanded a separator in
# the name.


def test_a_batch_file_is_a_script(tmp_path):
    _script(tmp_path, "dl.cmd", r"type C:\Users\mikha\gaia-archive\gold.parquet" + "\\n")
    ctx = _Ctx(tmp_path)

    verdict = _validate_command("dl.cmd", ctx, str(tmp_path))

    assert verdict is not None and verdict[0] == "tier1", verdict
    assert "dl.cmd" in verdict[1] and "gaia-archive" in verdict[1], verdict[1]


def test_the_cmd_wrappers_reach_the_batch_file(tmp_path):
    _script(tmp_path, "dl.bat", r"type C:\Users\mikha\gaia-archive\gold.parquet" + "\\n")
    ctx = _Ctx(tmp_path)

    for spelling in ("call dl.bat", "start dl.bat", r".\dl.bat"):
        verdict = _validate_command(spelling, ctx, str(tmp_path))
        assert verdict is not None and verdict[0] == "tier1", spelling
        assert "dl.bat" in verdict[1], (spelling, verdict[1])


def test_a_bare_name_is_a_launch_on_both_platforms(tmp_path):
    """Windows runs `grab.py` by file association, POSIX by the executable bit.
    The older rule needed a `.` or a slash in the name and saw neither."""
    _script(tmp_path, "grab.py", "open('/etc/shadow')\n")
    ctx = _Ctx(tmp_path)

    verdict = _validate_command("grab.py", ctx, str(tmp_path))

    assert verdict is not None and verdict[0] == "tier1", verdict
    assert "grab.py" in verdict[1] and "/etc/shadow" in verdict[1], verdict[1]


def test_a_batch_file_that_only_fetches_a_url_is_still_tier_zero(tmp_path):
    """The whole point of the 2026-08-30 narrowing: the agent's legitimate
    reason for writing dl.bat was a download, and a URL is not a path."""
    _script(tmp_path, "dl.bat", "curl -o out.parquet https://huggingface.co/api/x/parquet\n")
    ctx = _Ctx(tmp_path)

    assert _validate_command("dl.bat", ctx, str(tmp_path)) is None


def test_naming_a_script_as_an_argument_is_not_running_it(tmp_path):
    """The bare-name rule anchors at the start of a segment, so a script named
    anywhere else in the line must not be read as a launch."""
    _script(tmp_path, "dl.cmd", r"type C:\Users\mikha\gaia-archive\gold.parquet" + "\\n")
    ctx = _Ctx(tmp_path)

    assert _validate_command("type dl.cmd", ctx, str(tmp_path)) is None
    assert _validate_command("git add dl.cmd", ctx, str(tmp_path)) is None
