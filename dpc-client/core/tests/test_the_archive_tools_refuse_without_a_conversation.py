"""Without a conversation the session-archive tools refuse instead of reading
agent_001's archive, which was the fallback folder for every agent."""

import builtins
import json
import pathlib

import pytest

from dpc_client_core.dpc_agent.tools import archive
from dpc_client_core.dpc_agent.tools.registry import ToolContext

SECRET = "agent_001 private line"
SESSION = "2026-11-01T10-00-00_reset_session.json"


def _session(archive_dir: pathlib.Path, text: str) -> None:
    month = archive_dir / "2026" / "11"
    month.mkdir(parents=True)
    (month / SESSION).write_text(json.dumps({
        "archived_at": "2026-11-01T10:00:00Z",
        "messages": [{"role": "user", "content": text, "timestamp": "2026-11-01T10:00:00Z"}],
    }), encoding="utf-8")


@pytest.fixture
def agent_001_archive(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    archive_dir = home / ".dpc" / "conversations" / "agent_001" / "archive"
    _session(archive_dir, SECRET)
    return archive_dir


def _watch(monkeypatch, root: pathlib.Path) -> list:
    """Every open, existence check or walk that lands under `root`."""
    touched: list = []
    real_open, real_exists, real_rglob = builtins.open, pathlib.Path.exists, pathlib.Path.rglob

    def under(p) -> bool:
        try:
            return pathlib.Path(p).resolve().is_relative_to(root.resolve())
        except (TypeError, ValueError, OSError):
            return False

    def watched_open(file, *a, **k):
        if under(file):
            touched.append(("open", file))
        return real_open(file, *a, **k)

    def watched_exists(self, *a, **k):
        if under(self):
            touched.append(("exists", self))
        return real_exists(self, *a, **k)

    def watched_rglob(self, *a, **k):
        if under(self):
            touched.append(("rglob", self))
        return real_rglob(self, *a, **k)

    monkeypatch.setattr(builtins, "open", watched_open)
    monkeypatch.setattr(pathlib.Path, "exists", watched_exists)
    monkeypatch.setattr(pathlib.Path, "rglob", watched_rglob)
    return touched


CALLS = [
    lambda ctx: archive.read_session_archive(ctx),
    lambda ctx: archive.read_session_detail(ctx, SESSION),
    lambda ctx: archive.search_session_archives(ctx, "private"),
]


@pytest.mark.parametrize("call", CALLS, ids=["archive", "detail", "search"])
def test_without_a_monitor_each_tool_refuses_and_reads_nothing(call, agent_001_archive, tmp_path, monkeypatch):
    ctx = ToolContext(agent_root=tmp_path / "agent_other")
    touched = _watch(monkeypatch, agent_001_archive.parent)

    answer = call(ctx)

    assert answer == "No conversation context; cannot read session archives."
    assert touched == []


class _Monitor:
    def __init__(self, history: pathlib.Path):
        self._history = history

    def _get_history_path(self) -> pathlib.Path:
        return self._history


def test_with_a_monitor_the_tools_read_that_conversations_archive(agent_001_archive, tmp_path):
    own = tmp_path / "conversations" / "agent_x"
    _session(own / "archive", "own line about the queue")
    ctx = ToolContext(agent_root=tmp_path / "agent_x",
                      conversation_monitor=_Monitor(own / "history.json"))

    listed = json.loads(archive.read_session_archive(ctx))
    assert listed["total_archives"] == 1
    assert listed["sessions"][0]["user_message_previews"] == ["own line about the queue"]

    detail = json.loads(archive.read_session_detail(ctx, SESSION))
    assert detail["messages"][0]["content"] == "own line about the queue"

    found = json.loads(archive.search_session_archives(ctx, "queue"))
    assert found["total_matches"] == 1 and found["matches"][0]["filename"] == SESSION
    assert json.loads(archive.search_session_archives(ctx, "private"))["total_matches"] == 0
