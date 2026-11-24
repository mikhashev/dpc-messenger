"""chat_post_file copies a file into the agent's own 1:1 chat; the answer record carries it.

ADR-051 D40, D41 and D43 (Mike #46, #53-#57); Zcode #51 corrections 4 (an answer record carries
attachments and is written with no text) and 5 (the path goes through `_resolve_file_path`).

The live path: a real `DpcAgentManager.process_message` turn over a real `DpcAgent`, its
`run_llm_loop`, the real tool registry and a real `ContextFirewall` read from a rules file; a
real `ConversationMonitor` writing `history.json` on disk. Only the provider is a double: it
calls `chat_post_file` as scripted, whatever it was offered, and records what it was sent.
ffprobe is stood in for by a fixed length, except where a test says it reads none.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core import service as service_mod
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.dpc_agent.agent import AgentConfig, DpcAgent
from dpc_client_core.dpc_agent.context import _build_capabilities_section, build_llm_messages
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.retrieval import factory as retrieval_factory
from dpc_client_core.dpc_agent.run_gate import get_run_gate
from dpc_client_core.dpc_agent.tools import chat_files
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.llm_manager import LLMManager
from dpc_client_core.managers import agent_manager as am

CID = "agent_poster_7c1e0d42"
ALIAS = "cloud"
THIS_NODE = "dpc-node-this0000000000000000"
FOREIGN_NODE = "dpc-node-foreign0000000000000"
# The flac's bytes carry a phrase no marker would: if it reaches a prompt, the bytes did.
FLAC = b"fLaC\x00\x00\x00\x22" + b"BYTES-OF-THE-TAKE-NEVER-IN-A-PROMPT" + b"\x00" * 64
SECONDS = 12.5
FALLBACK = am._THINKING_FALLBACK


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("DPC_HOME", str(home / ".dpc"))
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", home / ".dpc")
    monkeypatch.setattr(service_mod, "measure_recording_seconds", lambda path: SECONDS)
    monkeypatch.setattr(retrieval_factory, "_derive_embedding_metadata", lambda cfg: ("BAAI/bge-m3", 1024))
    return home


def _names(tools) -> list:
    out = []
    for t in tools or []:
        name = t.get("name") or (t.get("function") or {}).get("name")
        if name:
            out.append(name)
    return out


class _Provider:
    """Each turn: round 1 records what it was sent and makes the scripted chat_post_file calls
    (none: it answers at once); round 2 answers with the scripted text."""

    model = "m"

    def __init__(self):
        self.script = []      # per turn: (list of call args, answer text)
        self.turns = []       # per turn: {"offered", "system", "messages", "after_call"}
        self._round = 0

    def supports_vision(self):
        return False

    def supports_thinking(self):
        return False

    def get_last_usage(self):
        return {}

    async def generate_with_tools(self, messages, tools, system=None, on_chunk=None,
                                  conversation_id=None, **_):
        usage = {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}
        if self._round == 0:
            calls, answer = self.script.pop(0)
            self.turns.append({"offered": _names(tools), "system": str(system or ""),
                               "messages": json.dumps(messages, default=str, ensure_ascii=False),
                               "answer": answer})
            if calls:
                self._round = 1
                raw = [SimpleNamespace(id=f"call-{i}", name="chat_post_file", input=args)
                       for i, args in enumerate(calls)]
                return {"content": "", "tool_calls_raw": raw, "usage": usage}
            return {"content": answer, "usage": usage}
        self._round = 0
        turn = self.turns[-1]
        turn["after_call"] = json.dumps(messages, default=str, ensure_ascii=False)
        return {"content": turn["answer"], "usage": usage}

    async def generate_response(self, prompt, **_):
        return "x"


class _LLM:
    token_count_manager = None
    agent_provider = None
    vision_provider = None
    _alias_for_query = LLMManager._alias_for_query

    def __init__(self, provider):
        self.providers = {ALIAS: provider}
        self.default_provider = ALIAS

    def alias_context_window(self, alias):
        return 128000

    async def query(self, prompt, provider_alias=None, images=None, return_metadata=False, **_):
        return {"response": "x", "provider": ALIAS} if return_metadata else "x"


class _Groups:
    node_id = THIS_NODE

    def __init__(self, *groups):
        self._groups = {g.group_id: g for g in groups}

    def get_group(self, gid):
        return self._groups.get(gid)

    def get_all_groups(self):
        return list(self._groups.values())


GROUPS = _Groups(
    SimpleNamespace(group_id="group-mixed", name="mixed", members=[THIS_NODE, FOREIGN_NODE],
                    is_discord_bridge=False),
    SimpleNamespace(group_id="group-own", name="own", members=[THIS_NODE], is_discord_bridge=False),
)


def _firewall(tmp_path, home, extended_read=True, read_only=None, cap_mb=None) -> ContextFirewall:
    tools = {"chat_post_file": True, "comfyui_submit": True}
    profile = {"tools": tools, "sandbox_extensions": {
        "extended_read_enabled": extended_read,
        "read_only": [str(p) for p in (read_only if read_only is not None else [home / "Music"])],
    }}
    rules = {"dpc_agent": {"enabled": True, "tools": tools}, "agent_profiles": {CID: profile}}
    if cap_mb is not None:
        rules["file_transfer"] = {"max_size_mb": cap_mb}
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    return ContextFirewall(path)


class _World:
    def __init__(self, tmp_path, home, monkeypatch, **firewall_kw):
        self.home = home
        self.provider = _Provider()
        self.events = []
        firewall = _firewall(tmp_path, home, **firewall_kw)
        self.agent = DpcAgent(llm_manager=_LLM(self.provider), config=AgentConfig(max_rounds=5),
                              agent_root=home / ".dpc" / "agents" / CID, provider_alias=ALIAS,
                              firewall=firewall, firewall_profile=CID,
                              service=SimpleNamespace(group_manager=GROUPS, p2p_manager=None))
        self.monitors = {}
        emitter = SimpleNamespace(emit=AsyncMock())
        monkeypatch.setattr(am, "get_event_emitter", lambda: emitter)

        m = am.DpcAgentManager.__new__(am.DpcAgentManager)
        m.agent_id = CID
        m.firewall = firewall
        m.config = {}
        m._agent = self.agent
        m._agents = {}
        m._memory_indexes_initialized = True
        m._agent_display_name = "Poster"
        m._interrupt_events = {}
        m._kill_events = {}
        m._run_gate = get_run_gate(CID)
        m._daily_tokens_used = 0
        m._daily_tokens_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        m._agent_monitors = self.monitors

        async def broadcast(name, payload):
            self.events.append((name, payload))

        m.service = SimpleNamespace(p2p_manager=SimpleNamespace(node_id=THIS_NODE),
                                    local_api=SimpleNamespace(broadcast_event=broadcast))
        m.ensure_started = AsyncMock(return_value=m)
        m._get_or_create_agent_monitor = self.monitor
        m._remember_chat_alias = lambda conv, alias: None
        m.get_session_state = lambda conv: {}
        m._resolve_reasoning_effort = lambda conv, effort: (None, "none")
        m._resolve_context_window = lambda: None
        self.manager = m

    def conv_dir(self, conv=CID) -> Path:
        return self.home / ".dpc" / "conversations" / conv

    def monitor(self, conv):
        if conv not in self.monitors:
            d = self.conv_dir(conv)
            d.mkdir(parents=True, exist_ok=True)
            mon = ConversationMonitor(
                conversation_id=conv,
                participants=[{"node_id": THIS_NODE, "name": "User", "context": "local"},
                              {"node_id": CID, "name": "Poster", "context": "agent"}],
                llm_manager=None,
            )
            mon._get_history_path = lambda: d / "history.json"
            mon._get_conversation_dir = lambda: d
            self.monitors[conv] = mon
        return self.monitors[conv]

    async def turn(self, calls, answer="here it is", conv=CID, source="ui", **kw):
        self.provider.script.append((calls, answer))
        return await asyncio.wait_for(self.manager.process_message(
            message="send me the take", conversation_id=conv, sender_name="Mike",
            message_source=source, **kw), 60)

    def history(self, conv=CID):
        path = self.conv_dir(conv) / "history.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))["messages"]

    def answers(self, conv=CID):
        return [m for m in self.history(conv) if m["role"] == "assistant"]

    def files(self, conv=CID):
        d = self.conv_dir(conv) / "files"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def all_chat_files(self):
        base = self.home / ".dpc" / "conversations"
        return sorted(str(p.relative_to(base)) for p in base.rglob("*")
                      if p.is_file() and p.parent.name == "files") if base.is_dir() else []


def _take(home, name="take.flac", data=FLAC) -> Path:
    f = home / "Music" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


def _sandbox_file(w, name="mix.flac", data=FLAC) -> Path:
    f = w.agent.agent_root / "outputs" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


def _tool_results(w) -> str:
    return w.provider.turns[-1].get("after_call", "")


# --- (a) the record on disk, the broadcast ----------------------------------------------------

@pytest.mark.asyncio
async def test_a_posted_flac_is_copied_into_the_chats_files_and_the_answer_record_on_disk_carries_it(
    tmp_path, home, monkeypatch,
):
    w = _World(tmp_path, home, monkeypatch)
    take = _take(home)

    answer = await w.turn([{"path": str(take), "caption": "the second take"}])

    assert answer == "here it is"
    assert "chat_post_file" in w.provider.turns[0]["offered"]
    (record,) = w.answers()
    assert record["content"] == "here it is"
    (att,) = record["attachments"]
    kept = Path(att["file_path"])
    assert kept.parent == w.conv_dir() / "files"
    assert kept.read_bytes() == FLAC
    assert take.read_bytes() == FLAC, "copied, never moved"
    assert att["type"] == "file" and att["mime_type"] == "audio/flac"
    assert att["filename"] == "take.flac" and att["size_bytes"] == len(FLAC)
    assert att["duration_seconds"] == SECONDS
    assert att["sha256"] == service_mod.file_sha256(take)
    assert att["caption"] == "the second take"
    assert "Posted take.flac" in _tool_results(w)
    pushed = [p for n, p in w.events if n == "agent_history_updated"]
    assert pushed, [n for n, _ in w.events]
    assert pushed[-1]["conversation_id"] == CID
    assert pushed[-1]["messages"][-1]["attachments"] == record["attachments"]


@pytest.mark.asyncio
async def test_a_file_in_the_agents_own_sandbox_is_posted_by_its_relative_path(tmp_path, home, monkeypatch):
    w = _World(tmp_path, home, monkeypatch)
    _sandbox_file(w)

    await w.turn([{"path": "outputs/mix.flac"}])

    (att,) = w.answers()[0]["attachments"]
    assert att["filename"] == "mix.flac" and w.files() == ["mix.flac"]


@pytest.mark.asyncio
async def test_without_ffprobe_the_length_is_zero(tmp_path, home, monkeypatch):
    monkeypatch.setattr(service_mod, "measure_recording_seconds", lambda path: None)
    w = _World(tmp_path, home, monkeypatch)

    await w.turn([{"path": str(_take(home))}])

    assert w.answers()[0]["attachments"][0]["duration_seconds"] == 0


# --- (b) an answer that is only a file --------------------------------------------------------

@pytest.mark.asyncio
async def test_an_answer_with_no_text_but_a_posted_file_is_recorded(tmp_path, home, monkeypatch):
    w = _World(tmp_path, home, monkeypatch)

    # The model's answer is the no-text stand-in, with no thinking and nothing streamed.
    await w.turn([{"path": str(_take(home))}], answer=FALLBACK)

    (record,) = w.answers()
    assert [a["filename"] for a in record["attachments"]] == ["take.flac"]
    assert not [p for n, p in w.events if n == "error_toast"]


def test_record_answer_writes_a_record_with_an_empty_text_and_its_attachments(tmp_path, home, monkeypatch):
    w = _World(tmp_path, home, monkeypatch)
    mon = w.monitor(CID)
    w.agent._last_usage, w.agent._last_trace = {}, {}
    att = {"type": "file", "filename": "a.flac", "mime_type": "audio/flac", "size_mb": 0.0,
           "file_path": str(w.conv_dir() / "files" / "a.flac")}

    w.manager._record_answer(mon, CID, w.agent, "", [], message_source="ui", attachments=[att])

    (record,) = w.answers()
    assert record["content"] == "" and record["attachments"] == [att]


# --- (c) refusals, with no copy -----------------------------------------------------------------

def _link_outside(link: Path, target: Path) -> None:
    """A junction on Windows (no privilege needed), a symlink elsewhere."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.mark.asyncio
async def test_a_directory_link_from_the_sandbox_to_outside_is_refused(tmp_path, home, monkeypatch):
    w = _World(tmp_path, home, monkeypatch)
    outside = home / "Private"
    outside.mkdir(parents=True)
    (outside / "secret.flac").write_bytes(FLAC)
    _link_outside(w.agent.agent_root / "linked", outside)

    await w.turn([{"path": "linked/secret.flac"}])

    assert "SANDBOX_VIOLATION" in _tool_results(w)
    assert w.all_chat_files() == []
    assert not w.answers()[0].get("attachments")


@pytest.mark.asyncio
async def test_a_file_symlink_from_the_sandbox_to_outside_is_refused(tmp_path, home, monkeypatch):
    w = _World(tmp_path, home, monkeypatch)
    secret = home / "Private" / "secret.flac"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(FLAC)
    link = w.agent.agent_root / "outputs" / "secret.flac"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"a file symlink cannot be made here: {e}")

    await w.turn([{"path": "outputs/secret.flac"}])

    assert "SANDBOX_VIOLATION" in _tool_results(w)
    assert w.all_chat_files() == []


@pytest.mark.asyncio
async def test_with_extended_path_read_off_an_absolute_path_is_refused(tmp_path, home, monkeypatch):
    w = _World(tmp_path, home, monkeypatch, extended_read=False)

    await w.turn([{"path": str(_take(home))}])

    assert "Extended path read access is disabled" in _tool_results(w)
    assert w.all_chat_files() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["none", "dpc_home"], ids=["no-extension", "dpc-home-extended"])
async def test_the_ws_token_is_never_posted(tmp_path, home, monkeypatch, extension):
    # Even an extended path over the whole DPC home does not open it to this tool.
    read_only = [] if extension == "none" else [home / ".dpc"]
    w = _World(tmp_path, home, monkeypatch, read_only=read_only)
    token = home / ".dpc" / ".ws_token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("token-value", encoding="utf-8")

    await w.turn([{"path": str(token)}])

    out = _tool_results(w)
    assert "refused" in out or "SANDBOX_VIOLATION" in out, out
    assert "Posted" not in out
    assert w.all_chat_files() == []


@pytest.mark.asyncio
async def test_a_file_over_the_size_cap_is_refused_before_any_copy(tmp_path, home, monkeypatch):
    w = _World(tmp_path, home, monkeypatch, cap_mb=1)
    big = _take(home, "big.flac", b"fLaC" + b"\x00" * (1024 * 1024 + 10))

    await w.turn([{"path": str(big)}])

    assert "File too large" in _tool_results(w)
    assert w.all_chat_files() == []


@pytest.mark.asyncio
async def test_the_fifth_file_in_a_turn_is_refused(tmp_path, home, monkeypatch):
    w = _World(tmp_path, home, monkeypatch)
    takes = [_take(home, f"take{i}.flac", FLAC + bytes([i])) for i in range(5)]

    await w.turn([{"path": str(t)} for t in takes])

    (record,) = w.answers()
    assert [a["filename"] for a in record["attachments"]] == [f"take{i}.flac" for i in range(4)]
    assert w.files() == [f"take{i}.flac" for i in range(4)]
    assert "already carries 4 files" in _tool_results(w)


# --- (d) turns that are not the owner's 1:1 chat ------------------------------------------------

NOT_OFFERED = [
    # (source, conversation): the registry's owner_only gate; the schema is not offered
    ("group", "group-mixed"),   # other_node: a group with a foreign node
    ("discord", CID),           # a stranger, even in the agent's own chat
    ("telegram", CID),          # Telegram that is not the owner's private chat
]


@pytest.mark.asyncio
@pytest.mark.parametrize("source,conv", NOT_OFFERED)
async def test_a_turn_that_is_not_the_owners_is_not_offered_the_tool_and_a_forced_call_copies_nothing(
    tmp_path, home, monkeypatch, source, conv,
):
    w = _World(tmp_path, home, monkeypatch)

    await w.turn([{"path": str(_take(home))}], conv=conv, source=source)

    turn = w.provider.turns[0]
    assert "chat_post_file" not in turn["offered"], turn["offered"]
    out = _tool_results(w)
    # The firewall's per-turn list leaves it out first; without that, owner_only refuses it.
    assert ("'chat_post_file' is not in the allowed tools list" in out
            or "runs only on the owner's own turns" in out), out[-1500:]
    assert "Posted" not in out
    assert w.all_chat_files() == []
    assert not any(a.get("attachments") for a in w.answers(CID))


@pytest.mark.asyncio
@pytest.mark.parametrize("source,conv,why", [
    ("telegram_owner", CID, "came from Telegram"),
    ("group", "group-own", "in a group"),
])
async def test_an_owner_turn_outside_the_dpc_one_to_one_chat_is_refused_with_its_reason(
    tmp_path, home, monkeypatch, source, conv, why,
):
    w = _World(tmp_path, home, monkeypatch)
    kw = {"telegram_chat_id": "5"} if source == "telegram_owner" else {}

    await w.turn([{"path": str(_take(home))}], conv=conv, source=source, **kw)

    assert why in _tool_results(w)
    assert w.all_chat_files() == []


@pytest.mark.asyncio
async def test_a_turn_that_is_not_the_owners_names_owner_only_tools_as_such_not_as_firewall_disabled(
    tmp_path, home, monkeypatch,
):
    w = _World(tmp_path, home, monkeypatch)

    await w.turn([], conv="group-mixed", source="group")

    system = w.provider.turns[0]["system"] + w.provider.turns[0]["messages"]
    owner_line = next(line for line in system.split("\\n") if "Owner-only, not available in this turn" in line)
    assert "chat_post_file" in owner_line and "comfyui_submit" in owner_line
    disabled = [line for line in system.split("\\n") if "Disabled by firewall" in line]
    assert not any("chat_post_file" in line or "comfyui_submit" in line for line in disabled), disabled


def test_the_capabilities_section_of_an_owner_turn_has_no_owner_only_line(tmp_path):
    text = _build_capabilities_section(tmp_path, {"read_file", "chat_post_file"},
                                       {"read_file": True, "chat_post_file": True, "run_shell": False})
    assert "Owner-only" not in text and "run_shell" in text


# --- (e) the next turn sees the marker, never the bytes ------------------------------------------

@pytest.mark.asyncio
async def test_the_next_turns_prompt_holds_the_files_marker_and_not_its_bytes(tmp_path, home, monkeypatch):
    w = _World(tmp_path, home, monkeypatch)
    await w.turn([{"path": str(_take(home))}])

    await w.turn([], answer="it was in D minor")

    prompt = w.provider.turns[1]["messages"]
    assert "[file: take.flac, audio/flac" in prompt, prompt[-2000:]
    assert f"{SECONDS:g} s" in prompt
    assert "BYTES-OF-THE-TAKE" not in prompt and "fLaC" not in prompt


def test_an_answer_record_with_no_text_is_replayed_as_its_marker(tmp_path, home):
    root = home / ".dpc" / "agents" / CID
    history = [
        {"id": "u1", "role": "user", "content": "send me the take"},
        {"id": "a1", "role": "assistant", "content": "", "attachments": [{
            "type": "file", "filename": "take.flac", "mime_type": "audio/flac", "size_mb": 0.0,
            "file_path": str(home / ".dpc" / "conversations" / CID / "files" / "take.flac")}]},
    ]
    messages, _ = build_llm_messages(
        agent_root=root, memory=Memory(agent_root=root), task={"id": CID, "type": "chat", "text": "and?"},
        conversation_history=history, recall_results=[],
    )
    replayed = [m for m in messages if m["role"] == "assistant"]
    assert replayed and "[file: take.flac, audio/flac" in str(replayed[-1]["content"])


# --- (f) the same bytes are kept once -------------------------------------------------------------

@pytest.mark.asyncio
async def test_posting_the_same_file_twice_keeps_one_copy(tmp_path, home, monkeypatch):
    w = _World(tmp_path, home, monkeypatch)
    take = _take(home)

    await w.turn([{"path": str(take)}])
    await w.turn([{"path": str(take)}, {"path": str(take)}])

    assert w.files() == ["take.flac"]
    first, second = w.answers()
    assert len(second["attachments"]) == 1, "attached once to one answer"
    assert first["attachments"][0]["file_path"] == second["attachments"][0]["file_path"]
    assert first["attachments"][0]["sha256"] == second["attachments"][0]["sha256"]


@pytest.mark.asyncio
async def test_a_file_the_person_sent_into_the_chat_is_posted_back_without_a_second_copy(
    tmp_path, home, monkeypatch,
):
    w = _World(tmp_path, home, monkeypatch)
    sent = w.conv_dir() / "files" / "hum.flac"
    sent.parent.mkdir(parents=True, exist_ok=True)
    sent.write_bytes(FLAC)

    await w.turn([{"path": str(sent)}])

    assert w.files() == ["hum.flac"]
    assert w.answers()[0]["attachments"][0]["file_path"] == str(sent)


# --- the keeper ---------------------------------------------------------------------------------

@pytest.mark.parametrize("name,mime", [
    ("a.flac", "audio/flac"), ("a.FLAC", "audio/flac"), ("a.wav", "audio/wav"), ("a.mp3", "audio/mpeg"),
    ("a.ogg", "audio/ogg"), ("a.m4a", "audio/mp4"),
])
def test_audio_files_get_their_mime_type_by_suffix_on_every_platform(tmp_path, monkeypatch, name, mime):
    # What the Windows registry answers through mimetypes; the table must win over it.
    monkeypatch.setattr("mimetypes.guess_type", lambda *a, **k: ("audio/x-flac", None))
    src = tmp_path / name
    src.write_bytes(b"\x00" * 8)
    kept = service_mod.keep_agent_chat_attachment(
        tmp_path / "files", {"kind": "file", "file_path": str(src)}, voice_mime_types=[],
        max_voice_seconds=300, temp_dir=tmp_path / "temp", measure_seconds=lambda p: None)
    assert kept["mime_type"] == mime and kept["duration_seconds"] == 0


def test_the_paperclip_copy_is_not_deduplicated(tmp_path):
    src = tmp_path / "a.pdf"
    src.write_bytes(b"%PDF-1.4")
    keep = lambda: service_mod.keep_agent_chat_attachment(  # noqa: E731
        tmp_path / "files", {"kind": "file", "file_path": str(src)}, voice_mime_types=[],
        max_voice_seconds=300, temp_dir=tmp_path / "temp", measure_seconds=lambda p: None)
    first, second = keep(), keep()
    assert first["file_path"] != second["file_path"] and first["sha256"] == second["sha256"]
    assert "duration_seconds" not in first, "a PDF has no length"


def test_the_tool_is_owner_only_and_off_by_default():
    (entry,) = chat_files.get_tools()
    assert entry.name == "chat_post_file" and entry.owner_only and not entry.default_enabled
