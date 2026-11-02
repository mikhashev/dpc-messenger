"""`run_supervised(cr_ends_line=True, on_stderr=...)`: a download's redrawn progress line is read as it is drawn.

A download redraws one line with a carriage return and no newline (`\\r  label: 1.2 / 5.0 GB
(24%)`), so the line-at-a-newline reading of `on_stdout` would hand over the whole run of
them at the next newline, minutes later. With `cr_ends_line` a bare `\\r` ends a line for both
pipes; `on_stderr` is `on_stdout` for the other pipe. Without them nothing changes.
"""

import sys
import textwrap

from dpc_client_core.dpc_agent.tools import process

SCRIPT = textwrap.dedent('''
    import sys, time
    for i in range(1, 4):
        sys.stdout.write("\\r  label: %d.00 / 3.00 GB (%d%%)   " % (i, i * 33))
        sys.stdout.flush()
        time.sleep(0.05)
    sys.stdout.write("\\r\\n  tail without a newline")
    sys.stdout.flush()
    sys.stderr.write("err one\\rerr two\\n")
    sys.stderr.flush()
''')


def _run(tmp_path, **kwargs):
    script = tmp_path / "redraw.py"
    script.write_text(SCRIPT, encoding="utf-8")
    return process.run_supervised([sys.executable, str(script)], launcher="test(redraw)", timeout=60, **kwargs)


def test_each_redrawn_segment_is_a_line_for_both_pipes_with_no_empty_ones(tmp_path):
    out, err = [], []
    run = _run(tmp_path, on_stdout=out.append, on_stderr=err.append, cr_ends_line=True)
    assert run.returncode == 0
    assert [line.strip() for line in out] == [
        "label: 1.00 / 3.00 GB (33%)", "label: 2.00 / 3.00 GB (66%)", "label: 3.00 / 3.00 GB (99%)",
        "tail without a newline"]
    assert err == ["err one", "err two"]


def test_without_the_flag_a_line_ends_at_a_newline_only_and_stderr_is_not_given(tmp_path):
    out = []
    run = _run(tmp_path, on_stdout=out.append)
    assert len(out) == 2, "the three redrawn segments arrive together, at the first newline"
    assert out[0].count("GB") == 3 and out[1].strip() == "tail without a newline"
    assert "err one" in run.stderr


def test_the_whole_text_is_kept_whatever_the_callbacks_saw(tmp_path):
    run = _run(tmp_path, on_stdout=lambda line: None, on_stderr=lambda line: None, cr_ends_line=True)
    assert run.stdout.count("GB") == 3 and "tail without a newline" in run.stdout
    assert run.stderr.replace("\r", "\n").splitlines() == ["err one", "err two"]


def test_a_callback_that_raises_costs_nothing(tmp_path):
    def broken(line):
        raise ValueError("a parser's bug")

    run = _run(tmp_path, on_stdout=broken, on_stderr=broken, cr_ends_line=True)
    assert run.returncode == 0 and "tail without a newline" in run.stdout
