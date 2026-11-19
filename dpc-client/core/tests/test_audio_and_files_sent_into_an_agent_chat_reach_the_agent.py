"""A recording or a file sent into a 1:1 agent chat is kept there and reaches the agent.

IN-AN-AGENT-CHAT-THE-MICROPHONE-ONLY-DICTATES-SO-A-HUMMED-MELODY-NEVER-REACHES-THE-AGENT-AS-AUDIO
(ADR-051 Phase 0): in an agent chat the microphone only dictated, the recording
was dropped, and the paperclip was disabled, so a hummed melody never reached
the agent as audio. Now `execute_ai_query` takes an `attachment` in an agent
chat: the recording ("Send as audio") or the picked file is kept in the chat's
`files/`, the person's record carries it before the run, and the agent reads
its marker (name, MIME type, size, where it is kept) in the same turn. A plain
AI chat and a group take no attachment this way, and a message without one is
recorded exactly as before.

Real ConversationMonitor and DpcAgentManager.process_message; the agent's
`process` is a stand-in, as in test_an_image_pasted_into_an_agent_chat_is_kept_in_its_history.
"""

from __future__ import annotations

import base64
import json
import pathlib
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core import service as service_mod
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.dpc_agent.context import TURN_CONTEXT_OPEN, build_llm_messages
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.run_gate import get_run_gate
from dpc_client_core.dpc_agent.tools.core import _resolve_file_path
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.managers import agent_manager as am
from dpc_client_core.service import CoreService

CID = "agent_hum_7244b181"
GROUP = "group-000000000001"
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64


class _Agent:
    def __init__(self):
        self._last_usage, self._last_trace = {}, {}
        self.calls = []

    async def process(self, **kwargs):
        self.calls.append(kwargs)
        self._last_usage = {"model": "stand-in", "provider": "stand-in"}
        return "a melody in D minor"


def _world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", home / ".dpc")
    conv_dir = home / ".dpc" / "conversations" / CID
    conv_dir.mkdir(parents=True)
    monitor = ConversationMonitor(
        conversation_id=CID,
        participants=[{"node_id": "dpc-node-me", "name": "User", "context": "local"},
                      {"node_id": CID, "name": "Hum", "context": "agent"}],
        llm_manager=None,
    )
    monitor._get_history_path = lambda: conv_dir / "history.json"
    monitor._get_conversation_dir = lambda: conv_dir
    emitter = SimpleNamespace(emit=AsyncMock())
    monkeypatch.setattr(am, "get_event_emitter", lambda: emitter)

    agent = _Agent()
    manager = am.DpcAgentManager.__new__(am.DpcAgentManager)
    manager.agent_id = CID
    manager.firewall = MagicMock()
    manager.config = {}
    manager._agent = agent
    manager._agents = {}
    manager._memory_indexes_initialized = True
    manager._agent_display_name = "Hum"
    manager._interrupt_events = {}
    manager._kill_events = {}
    manager._run_gate = get_run_gate(CID)
    manager._daily_tokens_used = 0
    manager._daily_tokens_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    manager._agent_monitors = {CID: monitor}
    events, responses = [], []

    async def broadcast(name, payload):
        events.append((name, payload))

    async def respond(**kwargs):
        responses.append(kwargs)

    manager.service = SimpleNamespace(
        p2p_manager=SimpleNamespace(node_id="dpc-node-me"),
        local_api=SimpleNamespace(broadcast_event=broadcast),
    )
    manager.ensure_started = AsyncMock(return_value=manager)
    manager._get_or_create_agent_monitor = lambda conv: monitor
    manager._get_or_create_agent_for_provider = lambda alias: agent
    manager._remember_chat_alias = lambda conv, alias: None
    manager.get_session_state = lambda conv: {}
    manager._resolve_reasoning_effort = lambda conv, effort: (None, "none")
    manager._resolve_context_window = lambda: None

    provider = SimpleNamespace(_ensure_manager=AsyncMock(return_value=manager),
                               get_manager=lambda conv: manager)
    service = SimpleNamespace(
        llm_manager=SimpleNamespace(providers={"dpc_agent": provider}),
        settings=SimpleNamespace(get=lambda section, key, default=None: default),
        local_api=SimpleNamespace(broadcast_event=broadcast, send_response_to_all=respond),
        p2p_manager=SimpleNamespace(node_id="dpc-node-me", get_display_name=lambda: "Mike"),
        reset_cc_agent_chain=lambda conv: None,
        get_cc_display_name=lambda: "CC",
        _get_agent_display_name=lambda agent_id: "Hum",
        _check_agent_cc_mention=AsyncMock(),
    )
    service._keep_agent_chat_attachment = MethodType(CoreService._keep_agent_chat_attachment, service)
    service._execute_agent_query = MethodType(CoreService._execute_agent_query, service)
    return SimpleNamespace(home=home, conv_dir=conv_dir, monitor=monitor, agent=agent,
                           events=events, responses=responses, service=service)


def _recording(w, name="voice_rec.wav", data=WAV):
    """What the desktop recorder leaves: a WAV in ~/.dpc/temp."""
    temp = w.home / ".dpc" / "temp"
    temp.mkdir(parents=True, exist_ok=True)
    f = temp / name
    f.write_bytes(data)
    return f


def _picked(w, name="notes.pdf", data=b"%PDF-1.4 hello"):
    """A file the person picked somewhere outside DPC."""
    f = w.home / "Documents" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


async def _send(w, prompt="", conversation_id=CID, provider="dpc_agent", attachment=None):
    payload = {"conversation_id": conversation_id}
    if attachment is not None:
        payload["attachment"] = attachment
    return await CoreService.execute_ai_query(
        w.service, command_id="cmd-1", prompt=prompt, provider=provider, **payload)


def _history(w):
    path = w.conv_dir / "history.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["messages"]


def _users(w):
    return [m for m in _history(w) if m["role"] == "user"]


def _voice(path, duration=20):
    return {"kind": "voice", "file_path": str(path), "mime_type": "audio/wav", "duration_seconds": duration}


# --- a recording sent as audio ---------------------------------------------------

@pytest.mark.asyncio
async def test_a_recording_sent_as_audio_is_kept_in_the_chats_files_and_reaches_the_agent_once(
    tmp_path, signing_identity, monkeypatch,
):
    w = _world(tmp_path, monkeypatch)
    rec = _recording(w)

    await _send(w, attachment=_voice(rec))

    (user,) = _users(w)
    (att,) = user["attachments"]
    kept = Path(att["file_path"])
    assert kept.parent == w.conv_dir / "files"
    assert kept.read_bytes() == WAV
    assert att["type"] == "file" and att["mime_type"] == "audio/wav"
    assert att["filename"] == kept.name and kept.name.startswith("voice_") and kept.suffix == ".wav"
    assert att["size_bytes"] == len(WAV) and att["voice_metadata"]["duration_seconds"] == 20
    # moved out of the recorder's temp folder, not copied: nothing left behind
    assert not rec.exists()
    # no words: the record's text is the line a group file gets, which the bubble hides
    assert user["content"] == f"Sent file: {kept.name} ({att['size_mb']} MB)"
    assert len(w.agent.calls) == 1
    assert w.agent.calls[0]["message"] == user["content"]
    assert [r["status"] for r in w.responses] == ["OK"]


@pytest.mark.asyncio
async def test_the_confirmation_carries_the_kept_attachment_for_the_bubble(
    tmp_path, signing_identity, monkeypatch,
):
    w = _world(tmp_path, monkeypatch)

    await _send(w, attachment=_voice(_recording(w)))

    (confirmed,) = [p for n, p in w.events if n == "user_message_confirmed"]
    (user,) = _users(w)
    assert confirmed["command_id"] == "cmd-1"
    assert confirmed["message_id"] == user["id"]
    assert confirmed["attachments"] == user["attachments"]


@pytest.mark.asyncio
async def test_words_typed_with_the_recording_are_the_records_text(
    tmp_path, signing_identity, monkeypatch,
):
    w = _world(tmp_path, monkeypatch)

    await _send(w, prompt="listen, I came up with a melody", attachment=_voice(_recording(w)))

    (user,) = _users(w)
    assert user["content"] == "listen, I came up with a melody"
    assert len(user["attachments"]) == 1


@pytest.mark.asyncio
async def test_a_browser_recording_given_as_base64_is_kept_with_its_own_format(
    tmp_path, signing_identity, monkeypatch,
):
    w = _world(tmp_path, monkeypatch)
    webm = b"\x1aE\xdf\xa3 webm bytes"

    await _send(w, attachment={"kind": "voice", "audio_base64": base64.b64encode(webm).decode(),
                               "mime_type": "audio/webm;codecs=opus", "duration_seconds": 3})

    (att,) = _users(w)[0]["attachments"]
    assert att["mime_type"] == "audio/webm" and att["filename"].endswith(".webm")
    assert Path(att["file_path"]).read_bytes() == webm


@pytest.mark.asyncio
async def test_a_recording_in_an_unlisted_format_is_refused_before_anything_is_recorded(
    tmp_path, signing_identity, monkeypatch,
):
    w = _world(tmp_path, monkeypatch)
    rec = _recording(w, "take.flac")

    await _send(w, attachment={"kind": "voice", "file_path": str(rec), "mime_type": "audio/flac"})

    assert [r["status"] for r in w.responses] == ["ERROR"]
    assert "Unsupported audio format" in w.responses[0]["payload"]["message"]
    assert _history(w) == [] and w.agent.calls == []
    assert rec.exists(), "the recording is not lost on a refusal"
    assert list((w.conv_dir / "files").glob("*")) == []


@pytest.mark.asyncio
async def test_a_recording_longer_than_the_voice_limit_is_refused(
    tmp_path, signing_identity, monkeypatch,
):
    w = _world(tmp_path, monkeypatch)
    rec = _recording(w)

    await _send(w, attachment=_voice(rec, duration=301))

    assert [r["status"] for r in w.responses] == ["ERROR"]
    assert "too long" in w.responses[0]["payload"]["message"]
    assert _history(w) == [] and w.agent.calls == [] and rec.exists()


# --- a file picked with the paperclip ------------------------------------------

@pytest.mark.asyncio
async def test_a_picked_file_is_copied_into_the_chats_files_and_reaches_the_agent_once(
    tmp_path, signing_identity, monkeypatch,
):
    w = _world(tmp_path, monkeypatch)
    pdf = _picked(w)

    await _send(w, prompt="summarise this", attachment={"kind": "file", "file_path": str(pdf)})

    (user,) = _users(w)
    (att,) = user["attachments"]
    kept = Path(att["file_path"])
    assert kept == w.conv_dir / "files" / "notes.pdf"
    assert kept.read_bytes() == pdf.read_bytes()
    assert pdf.exists(), "the person's own file stays where it was"
    assert att["type"] == "file" and att["mime_type"] == "application/pdf"
    assert user["content"] == "summarise this"
    assert len(w.agent.calls) == 1


@pytest.mark.asyncio
async def test_the_same_name_sent_twice_is_two_files(tmp_path, signing_identity, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    song = _picked(w, "song.mp3", b"ID3" + b"\x00" * 100)

    await _send(w, attachment={"kind": "file", "file_path": str(song)})
    await _send(w, attachment={"kind": "file", "file_path": str(song)})

    paths = [u["attachments"][0]["file_path"] for u in _users(w)]
    assert len(set(paths)) == 2 and paths[1].endswith("song_1.mp3")
    assert all(Path(p).is_file() for p in paths)


@pytest.mark.asyncio
async def test_a_missing_file_is_refused_and_nothing_is_recorded(tmp_path, signing_identity, monkeypatch):
    w = _world(tmp_path, monkeypatch)

    await _send(w, attachment={"kind": "file", "file_path": str(w.home / "nowhere.mp3")})

    assert [r["status"] for r in w.responses] == ["ERROR"]
    assert _history(w) == [] and w.agent.calls == []


# --- what the agent reads --------------------------------------------------------

def _context_of_the_turn(w):
    """The person's message as the agent's turn renders it. On the 1:1 path the
    record is the last one when the run starts (agent.py picks it so); the stand-in
    agent's answer is recorded after it, so it is the last user record here."""
    root = w.home / ".dpc" / "agents" / CID
    Memory(root).ensure_files()
    record = [m for m in w.monitor.get_message_history() if m["role"] == "user"][-1]
    task = {"id": CID, "type": "chat", "text": record["content"], "trigger_record": record}
    reader = {"agent_id": CID, "display_name": "Hum", "node_id": "dpc-node-me"}
    messages = build_llm_messages(agent_root=root, memory=Memory(root), task=task,
                                  conversation_history=[], reader_identity=reader,
                                  active_recall_enabled=False)[0]
    return messages[-1]["content"].split("\n\n" + TURN_CONTEXT_OPEN)[0]


@pytest.mark.asyncio
async def test_the_agents_turn_carries_the_marker_with_a_path_that_exists_verbatim(
    tmp_path, signing_identity, monkeypatch,
):
    w = _world(tmp_path, monkeypatch)

    await _send(w, attachment=_voice(_recording(w)))

    (att,) = _users(w)[0]["attachments"]
    content = _context_of_the_turn(w)
    marker = f"[file: {att['filename']}, audio/wav, {att['size_mb']} MB, 20 s; kept at "
    assert marker in content
    where = content.split(marker, 1)[1].split(" under the DPC home", 1)[0]
    assert where == f"conversations/{CID}/files/{att['filename']}"
    assert (w.home / ".dpc" / where).is_file()
    assert str(w.home) not in content, "the home path does not travel in the prompt"


@pytest.mark.asyncio
async def test_a_picked_files_marker_names_it_and_its_type(tmp_path, signing_identity, monkeypatch):
    w = _world(tmp_path, monkeypatch)

    await _send(w, attachment={"kind": "file", "file_path": str(_picked(w))})

    content = _context_of_the_turn(w)
    assert f"[file: notes.pdf, application/pdf, " in content
    assert f"kept at conversations/{CID}/files/notes.pdf under the DPC home" in content


@pytest.mark.asyncio
async def test_the_agents_file_tools_may_open_what_was_sent_into_its_own_chat(
    tmp_path, signing_identity, monkeypatch,
):
    # reads_own_chat_files (Mike's call, 2026-11-11): no profile entry needed for
    # the agent's own 1:1 chat files; nothing is widened here, only checked.
    w = _world(tmp_path, monkeypatch)
    await _send(w, attachment=_voice(_recording(w)))
    (att,) = _users(w)[0]["attachments"]

    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"dpc_agent": {"enabled": True}}), encoding="utf-8")
    root = w.home / ".dpc" / "agents" / CID
    root.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(agent_root=root, firewall=ContextFirewall(rules), message_source="ui")
    ctx._agent = SimpleNamespace(_firewall_profile=CID, _this_node_id=lambda: "dpc-node-me")

    kept = Path(att["file_path"])
    assert _resolve_file_path(ctx, str(kept)) == kept.resolve()
    with pytest.raises(PermissionError):
        _resolve_file_path(ctx, str(kept), require_write=True)


# --- what did not change ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_message_without_an_attachment_is_recorded_as_before(
    tmp_path, signing_identity, monkeypatch,
):
    # Dictation puts the words in the input box; what is sent is a plain message.
    w = _world(tmp_path, monkeypatch)

    await _send(w, prompt="the words I dictated")

    (user,) = _users(w)
    assert user["content"] == "the words I dictated"
    assert not user.get("attachments")
    (confirmed,) = [p for n, p in w.events if n == "user_message_confirmed"]
    assert "attachments" not in confirmed
    assert not (w.conv_dir / "files").exists()
    assert len(w.agent.calls) == 1


@pytest.mark.asyncio
async def test_a_group_takes_no_attachment_this_way(tmp_path, signing_identity, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    rec = _recording(w)

    await _send(w, conversation_id=GROUP, attachment=_voice(rec))

    assert [r["status"] for r in w.responses] == ["ERROR"]
    assert w.agent.calls == [] and _history(w) == [] and rec.exists()
    assert not (w.conv_dir / "files").exists()


@pytest.mark.asyncio
async def test_the_keeper_refuses_a_group_even_when_called_directly(tmp_path, signing_identity, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    manager = w.service.llm_manager.providers["dpc_agent"].get_manager(CID)

    with pytest.raises(ValueError):
        await w.service._keep_agent_chat_attachment(manager, GROUP, _voice(_recording(w)))


@pytest.mark.asyncio
async def test_a_plain_ai_chat_takes_no_attachment(tmp_path, signing_identity, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    rec = _recording(w)

    await _send(w, conversation_id="ai_chat_1", provider="ollama", attachment=_voice(rec))

    assert [r["status"] for r in w.responses] == ["ERROR"]
    assert w.agent.calls == [] and rec.exists()


def test_an_unknown_kind_is_refused(tmp_path):
    with pytest.raises(ValueError):
        service_mod.keep_agent_chat_attachment(
            tmp_path / "files", {"kind": "image", "file_path": "x"},
            voice_mime_types=["audio/wav"], max_voice_seconds=300, temp_dir=tmp_path / "temp")
