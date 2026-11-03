"""No test may start a child that only a working guard can stop.

Each allocator in the suite comes from `bounded_allocator`. This scans the tests
for the shape that took 27.6 GiB: a loop with no end around an allocation.
"""

import re
import sys
from pathlib import Path

import pytest

from dpc_client_core.dpc_agent.tools.process import run_supervised
from tests.bounded_allocator import LIVES_AT_MOST_S, MOST_MB, TIMEOUT_S, allocator, hungry_grandchild

TESTS = Path(__file__).resolve().parent

ENDLESS = r"(?:while\s+True\b|for\s+\w+\s+in\s+itertools\.count\()"
ALLOCATES = r"(?:bytearray\(\s*\d|bytes\(\s*\d|\*\s*1024\s*\*\s*1024)"
# Near enough to be the loop's body, written out or held in a string. A
# comprehension names its count after the allocation, so that one reads backwards.
ENDLESS_ALLOCATOR = re.compile(
    rf"{ENDLESS}.{{0,80}}?{ALLOCATES}|{ALLOCATES}.{{0,80}}?for\s+\w+\s+in\s+itertools\.count\(", re.S,
)


def _sources():
    return [p for p in TESTS.rglob("*.py") if p != Path(__file__).resolve()]


def test_no_test_holds_a_loop_with_no_end_around_an_allocation():
    found = [
        f"{path.relative_to(TESTS)}:{text.count(chr(10), 0, hit.start()) + 1}"
        for path in _sources()
        for text in [path.read_text(encoding="utf-8", errors="replace")]
        for hit in ENDLESS_ALLOCATOR.finditer(text)
    ]
    assert found == [], f"use tests/bounded_allocator.py instead: {found}"
    assert len(_sources()) > 500, "the scan must have looked at the suite"


@pytest.mark.parametrize("shape", [
    "'import time\\nb=[]\\nwhile True:\\n    b.append(bytearray(20*1024*1024))\\n    time.sleep(0.2)'",
    '"while True:\\n"\n    "    b.append(bytearray(20 * 1024 * 1024))\\n"',
    "[(b.append(bytearray(20 * 1024 * 1024)), time.sleep(0.2)) for _ in itertools.count()]",
    "while True:\n    b.append(b'x' * 50 * 1024 * 1024)",
    "for _ in itertools.count():\n    b.append(bytes(10000000))",
])
def test_the_scan_sees_each_shape_it_is_for(shape):
    assert ENDLESS_ALLOCATOR.search(shape), shape


@pytest.mark.parametrize("shape", [
    "while True:\n    message = await _next(reader)\n    if message is None:\n        break",
    "buf = bytearray()\nbuf += b'GGUF'",
    "ticks = itertools.count()\nbody = b'x' * (3 * 1024 * 1024)",
    allocator(),
    hungry_grandchild(),
])
def test_the_scan_leaves_what_is_not_that_shape_alone(shape):
    assert not ENDLESS_ALLOCATOR.search(shape), shape


def test_the_program_says_its_bound():
    assert f"+ {LIVES_AT_MOST_S};" in allocator()
    steps = int(re.search(r"range\((\d+)\)", allocator()).group(1))
    assert 0 < steps * 20 <= MOST_MB <= 1024, "at most 1 GiB, by the program's own count"
    assert TIMEOUT_S < LIVES_AT_MOST_S, "or a dead guard would read as a child that stopped by itself"


def test_the_program_ends_by_itself_with_nothing_watching_it():
    run = run_supervised(
        [sys.executable, "-c", allocator(most_mb=40, lives_s=1)],
        launcher="test(ends by itself)", timeout=TIMEOUT_S, ceiling_mb=0,
    )
    assert run.returncode == 0, run.stderr
    assert not run.timed_out
