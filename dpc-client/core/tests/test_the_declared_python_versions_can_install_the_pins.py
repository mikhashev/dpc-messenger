"""We declared support for an interpreter our own pin could not build on.

`requires-python = ">=3.12,<4"` allows 3.13. `tiktoken>=0.5.1,<0.6.0` resolves
to 0.5.2, which builds through PyO3 0.20.3 — «the configured Python interpreter
version (3.13) is newer than PyO3's maximum supported version (3.12)». Measured
2026-09-02 on a machine that has Rust, so it was never «install a toolchain».

The check is on the two declarations agreeing, because that is what drifted:
one of them moves and nothing notices until somebody on a newer interpreter
tries to install.
"""

import re
import sys
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"

# The first tiktoken release whose PyO3 knows Python 3.13. Below this a 3.13
# install fails at the build step whatever the machine has on it.
FIRST_RELEASE_THAT_BUILDS_ON_313 = (0, 8)


def _text():
    return PYPROJECT.read_text(encoding="utf-8")


def _version(raw):
    return tuple(int(part) for part in re.findall(r"\d+", raw)[:2])


def _requires_python_allows(major, minor):
    spec = re.search(r'requires-python\s*=\s*"([^"]+)"', _text()).group(1)
    lower = re.search(r">=\s*(\d+)\.(\d+)", spec)
    upper = re.search(r"<\s*(\d+)(?:\.(\d+))?", spec)
    want = (major, minor)
    if lower and want < (int(lower.group(1)), int(lower.group(2))):
        return False
    if upper:
        bound = (int(upper.group(1)), int(upper.group(2) or 0))
        if want >= bound:
            return False
    return True


def test_the_declaration_still_allows_the_interpreter_that_broke():
    """If this goes red the fix moved to the other end, and that is fine —
    but then the test below is measuring nothing and should go with it."""
    assert _requires_python_allows(3, 13), (
        "requires-python no longer allows 3.13; drop the pin check with it"
    )


def test_the_tiktoken_ceiling_clears_every_interpreter_we_declare():
    pin = re.search(r'"tiktoken([^"]*)"', _text()).group(1)
    upper = re.search(r"<\s*([\d.]+)", pin)

    assert upper, f"tiktoken pin has no ceiling: {pin!r}"
    assert _version(upper.group(1)) >= FIRST_RELEASE_THAT_BUILDS_ON_313, (
        f"tiktoken ceiling {upper.group(1)} is below {FIRST_RELEASE_THAT_BUILDS_ON_313}, "
        f"so a 3.13 install cannot resolve to a release that builds"
    )


@pytest.mark.skipif(sys.version_info < (3, 13), reason="the interpreter under test is older")
def test_the_installed_tiktoken_actually_imports_here():
    """On 3.13 the proof is not the metadata, it is the import."""
    import tiktoken

    assert tiktoken.get_encoding("cl100k_base").encode("hello world")
