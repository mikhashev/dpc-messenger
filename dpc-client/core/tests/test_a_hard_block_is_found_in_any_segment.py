"""A dangerous segment must be found wherever it sits in the command.

ADR-030 states it as a confirmed property — «Pipe chain splitting catches
dangerous commands in any segment position» (`030-run-shell-safety-guardrails.md:148`).
It did not hold. The classifier walked segments and returned on the first
pattern of *any* tier that matched, so a Tier-1 hit early in the line ended the
scan before a later segment was examined for a hard block at all:

    sudo ls && rm -rf /   ->  tier1   "Requires approval: sudo"

Found by Fable 5 in the eval-harness review, reproduced by CC, traced through
the source by Ark. It is a production defect: the eval only made it visible by
auto-approving what it mislabelled.
"""

import pytest

from dpc_client_core.dpc_agent.tools.shell import _split_segments, _validate_command


def tier_of(command: str):
    verdict = _validate_command(command, None)
    return verdict[0] if verdict else "tier0"


def reason_of(command: str) -> str:
    verdict = _validate_command(command, None)
    return verdict[1] if verdict else ""


# --- the defect itself ------------------------------------------------------

@pytest.mark.parametrize("command", [
    "sudo ls && rm -rf /",
    "git reset --hard && rm -rf /",
    'python -c "pass" && shutdown /s',
    "sudo echo hi | rm -rf /",
    "sudo echo hi ; mkfs.ext4 /dev/sda1",
])
def test_a_tier1_match_earlier_in_the_line_does_not_hide_a_hard_block(command):
    assert tier_of(command) == "tier2", (
        "the hard block sits in a later segment and must still be found"
    )


@pytest.mark.parametrize("command,tier", [
    ("python script.py && rm -rf /", "tier2"),   # nothing matches first — was already right
    ("rm -rf /", "tier2"),                       # on its own
    ("sudo ls", "tier1"),                        # a soft one alone stays soft
    ("git reset --hard", "tier1"),
    ("echo hi", "tier0"),                        # and an innocent command stays free
])
def test_the_cases_that_already_worked_still_do(command, tier):
    assert tier_of(command) == tier


# --- the reason a person reads ---------------------------------------------

def test_the_reason_names_every_match_not_only_the_first():
    """`Requires approval: sudo` for a command whose second half was the part
    that mattered primes the reader for the wrong thing."""
    reason = reason_of('sudo git reset --hard')

    assert "sudo" in reason
    assert "reset" in reason, "the second dangerous pattern must be named too"


def test_one_match_still_reads_as_one_reason():
    reason = reason_of("sudo ls")

    assert reason.startswith("Requires approval: ")
    assert ";" not in reason, "no spurious separator when there is nothing to join"


# --- the separator set ------------------------------------------------------

def test_a_newline_separates_segments():
    """It did not, so a two-line command was one segment on every platform."""
    assert _split_segments("sudo ls\nrm -rf /") == ["sudo ls", "rm -rf /"]
    assert _split_segments("a\r\nb") == ["a", "b"]


def test_a_two_line_command_is_still_blocked():
    assert tier_of("sudo ls\nrm -rf /") == "tier2"


@pytest.mark.parametrize("command,expected", [
    ("a && b", ["a", "b"]),
    ("a || b", ["a", "b"]),
    ("a | b", ["a", "b"]),
    ("a ; b", ["a", "b"]),
    ("a & b", ["a", "b"]),
])
def test_every_separator_still_splits_and_leaves_no_empty_pieces(command, expected):
    """`&&` used to split into empty fragments because `[|;&]` matched first."""
    assert _split_segments(command) == expected
