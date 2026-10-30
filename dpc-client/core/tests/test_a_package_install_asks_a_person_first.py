"""A package install, removal or upgrade asks a person before it runs.

No rule matched them, so `pip install`, `python -m ensurepip`, `uv add`,
`npm install`, `winget install` and the rest were Tier 0 and ran without asking.
The path check cannot see where a package manager writes (`--target` passes
it), and the child inherits the backend's PATH, so the package lands in the
owner's interpreter. A GAIA run on another node did `python -m ensurepip` and
then `python -m pip install faster-whisper` that way.

The rules are keyed on the verb, not the program: the read-only half below
holds the same programs at Tier 0. The third test counts the
environment-changing forms the rules still do not cover, so the gap is a
number that moves when the list grows rather than a sentence nobody re-checks.
"""

import pytest

from dpc_client_core.dpc_agent.tools.shell import _validate_command


def tier_of(command: str) -> str:
    verdict = _validate_command(command, None)
    return verdict[0] if verdict else "tier0"


INSTALLS = [
    "pip install requests",
    "pip3 install requests",
    "pip3.12 install requests",
    "pip uninstall -y requests",
    "pip download requests",
    "pip install --target C:/tmp/x requests",
    "pip -q install requests",
    "python -m pip install faster-whisper",
    "py -m pip install requests",
    "python3 -m pip uninstall requests",
    "python -m ensurepip",
    "py -m ensurepip --upgrade",
    "uv pip install numpy",
    "uv pip uninstall numpy",
    "uv add httpx",
    "uv remove httpx",
    "uv --directory proj add httpx",
    "uv tool install ruff",
    "npm i lodash",
    "npm i -g typescript",
    "npm install",
    "npm install --save-dev vitest",
    "npm uninstall lodash",
    "npm ci",
    "npm --prefix web ci",
    "winget install Git.Git",
    "winget upgrade --all",
    "winget upgrade Git.Git",
    "winget uninstall Git.Git",
    "choco install git -y",
    "choco upgrade all",
    "choco uninstall git",
    "conda install numpy",
    "conda remove numpy",
    "mamba install numpy",
    "mamba remove numpy",
    "pipx install black",
    # Inside a chain, the install segment still asks.
    "cd work && pip install requests",
    # A path, quoted or not, or POSIX assignments before the program.
    r"C:\Python312\python.exe -m pip install requests",
    r'"C:\Program Files\nodejs\npm.cmd" install lodash',
    "/usr/bin/python3 -m pip install requests",
    "PIP_INDEX_URL=x pip install requests",
    "py -3.12 -m pip install requests",
]


@pytest.mark.parametrize("command", INSTALLS)
def test_an_install_form_is_tier1(command):
    assert tier_of(command) == "tier1"


def test_the_reason_names_the_rule_that_asked():
    tier, reason = _validate_command("python -m pip install faster-whisper", None)
    assert tier == "tier1"
    assert reason.startswith("Requires approval: ")
    assert "install" in reason


READ_ONLY = [
    "winget list",
    "winget --version",
    "winget upgrade --list",
    "uv pip list",
    "uv pip show numpy",
    "python -m pip list",
    "pip list",
    "pip show requests",
    "npm ls",
    "conda list",
    # Searching for the words, or printing them, installs nothing (Johnny's list,
    # 2026-10-30): the rule matches the command a segment runs, not an argument.
    "findstr pip install shell.py",
    'findstr "pip install" shell.py',
    'grep -n "pip install" shell.py',
    'grep -rn "uv add" .',
    'Select-String -Pattern "npm install" -Path README.md',
    'findstr /s "uv pip install" *.py',
    "echo pip install requests",
    'findstr /n "winget upgrade" notes.txt',
    'git log --grep "pip install"',
    "findstr pip install shell.py | more",
]


@pytest.mark.parametrize("command", READ_ONLY)
def test_a_read_only_form_stays_tier0(command):
    assert tier_of(command) == "tier0"


# Environment-changing forms with no rule yet. Each one that gains a rule
# lowers the number below; the assertion is there so that change is noticed.
NOT_YET_COVERED = [
    "uv sync",
    "uv venv",
    "poetry install",
    "pnpm install",
    "yarn install",
    "bundle install",
    "gem install x",
    "apt install x",
    "apt-get install -y x",
    "brew install x",
    "cargo install x",
    "go install x@latest",
    "npm link",
    "npm rebuild",
    "pip cache purge",
    "conda env create -f e.yml",
    "npx tool",
    "dotnet tool install x",
]
EXPECTED_UNCOVERED = 18


def test_the_uncovered_environment_changing_forms_are_counted():
    uncovered = [c for c in NOT_YET_COVERED if tier_of(c) == "tier0"]
    print(f"\n{len(uncovered)} of {len(NOT_YET_COVERED)} environment-changing forms "
          f"still run without asking: {uncovered}")
    assert len(uncovered) == EXPECTED_UNCOVERED
