"""chat_post_file posts into the group the agent is answering in (ADR-051 D50 G, Mike #88, #166).

The file goes through the person's group file path (`_send_group_file`): the copy in the group's
`files/`, the sender's signed record, the one signed file description and the chunked offer to
every connected member. Only the author differs: the record is the agent's, as its text posts
are (`sender_type` "agent", the name registered for this node, `agent_owner` this node). Who may
is ADR-053's: the owner asking posts; a foreign asker gets the owner's card, which names the
file, its size and type, and the group. The room axis (ADR-048) refuses a store closed to the
group's audience, before any card. The follow-up 13 allow-list, the per-turn count and the group
file-transfer cap hold in groups as in the 1:1 chat. A take's music block stays on this node's
record: a peer's library has no such entry, so its copy plays as a plain audio file.

Three levels: the service send over the in-process mesh of
test_a_group_file_crosses_a_full_mesh_once (real FileTransferManager and handlers); the tool
through `ToolRegistry.execute` with a recording service; one real agent turn of the 1:1 test's
world in a group.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core import service as service_mod
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.dpc_agent import owner_approval
from dpc_client_core.dpc_agent.agent import (
    ASKER_HUMAN, OTHER_NODE_TURN, OWNER_TURN, AgentConfig, Asker, DpcAgent,
)
from dpc_client_core.dpc_agent.tools import chat_files
from dpc_client_core.dpc_agent.tools import core as tools_core
from dpc_client_core.dpc_agent.tools import music as music_tools
from dpc_client_core.dpc_agent.tools.registry import ToolContext, ToolRegistry
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.service import CoreService
from tests.test_a_file_description_is_signed_by_its_author_and_checked_on_arrival import (  # noqa: F401
    identities,  # the fixture: each node signs with its own key, and knows the others'
)
from tests.test_a_group_file_crosses_a_full_mesh_once import (
    GROUP, _ids, _make_node, _Monitor, _Network, full_mesh, received_files, records,
)

FLAC = b"fLaC\x00\x00\x00\x22" + b"A-TAKE" + b"\x00" * 4000
AGENT_ID = "agent_poster_7c1e0d42"
AGENT_NAME = "Poster"
FRIEND = Asker(ASKER_HUMAN, "Miron", "dpc-node-friend", is_owner=False, verified=True)
MIKE = Asker(ASKER_HUMAN, "Mike", "dpc-node-this", is_owner=True, verified=True)
MUSIC = {"track_id": "trk-1", "take_id": "take-1", "job_id": "job-1"}
FLAC_MIME = mimetypes.guess_type("take.flac")[0]  # audio/flac, or audio/x-flac on Windows


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", tmp_path / "dpc-home")
    return tmp_path


# --- the service: an agent's file goes through the person's group file path --------------------

class _KeepingMonitor(_Monitor):
    """The mesh monitor, keeping every field the sender's record is written with."""

    def add_message(self, role, content, attachments=None, **kw):
        self.message_history.append({"id": kw.get("message_id"), "role": role,
                                     "content": content, "attachments": attachments, **kw})


def _agent_author(node):
    """The node's group lists this agent; the real name check and the real send run on it."""
    svc = node.service
    group = svc.group_manager.get_group(GROUP)
    group.group_id = GROUP
    group.agents = {node.me: [AGENT_ID]}
    group.agent_names = {node.me: {AGENT_ID: AGENT_NAME}}
    for name in ("_names_this_node_may_post_as",):
        setattr(svc, name, types.MethodType(getattr(CoreService, name), svc))
    svc._get_agent_display_name = lambda agent_id: AGENT_NAME if agent_id == AGENT_ID else agent_id
    svc.get_cc_display_name = lambda: "CC"
    svc.mentions = []

    async def mentions(*args, **kw):
        svc.mentions.append((args, kw))

    svc._handle_group_agent_mentions = mentions
    monitors = node.monitors

    def monitor_for(key):
        if key not in monitors:
            d = node.root / key
            d.mkdir(parents=True, exist_ok=True)
            monitors[key] = _KeepingMonitor(d)
        return monitors[key]

    svc._get_or_create_conversation_monitor = monitor_for
    return svc


def _take_file(root, name="take.flac", data=FLAC):
    f = root / "outbox" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


@pytest.mark.asyncio
async def test_an_agents_file_is_the_agents_signed_group_record_and_reaches_a_peer_as_a_group_file(
        home, identities):
    net = _Network()
    author, peer = full_mesh(net, home, 2)
    svc = _agent_author(author)
    identities.signing_as = author.me
    identities.know(author.me, peer.me)

    result = await CoreService.send_group_agent_file(
        svc, GROUP, AGENT_ID, str(_take_file(home)), text="the second take", music=MUSIC)
    await net.settle()

    assert not net.errors, net.errors
    assert result["status"] == "success", result
    assert result["sender_name"] == AGENT_NAME and len(result["transfer_ids"]) == 1
    (rec,) = records(author)
    assert rec["sender_name"] == AGENT_NAME and rec["sender_type"] == "agent"
    assert rec["agent_owner"] == author.me and rec["sender_node_id"] == author.me
    assert rec["content"] == "the second take"
    (att,) = rec["attachments"]
    assert att["music"] == MUSIC and att["mime_type"] == FLAC_MIME
    assert Path(att["file_path"]).read_bytes() == FLAC
    (shown,) = author.api.named("group_file_received")
    assert shown["sender_type"] == "agent" and shown["sender_name"] == AGENT_NAME
    assert shown["sender_node_id"] == author.me and shown["agent_owner"] == author.me
    # The person's offer: one signed description under the record's id, by this node.
    (offer,) = net.offers_to(peer.me)
    payload = offer[3]
    assert payload["message_id"] == result["message_id"] and payload["text"] == "the second take"
    assert payload["file_description"]["sender_node_id"] == author.me
    assert "music" not in json.dumps(payload), "the music block never crosses the wire"
    # The peer has the file as any group file: one copy, one record under the author's id.
    assert received_files(peer) == ["take.flac"]
    (peer_rec,) = records(peer)
    assert peer_rec["id"] == result["message_id"]
    assert "music" not in (peer_rec["attachments"][0])
    assert svc.mentions == [], "the agent's words wake nobody; its answer routes its mentions"


@pytest.mark.asyncio
async def test_a_name_this_node_did_not_register_for_the_group_is_refused_and_nothing_is_kept(home):
    net = _Network()
    me = _ids(1)[0]
    node = _make_node(net, home, me, [], [me])
    svc = _agent_author(node)

    result = await CoreService.send_group_agent_file(svc, GROUP, "agent_stranger_1", str(_take_file(home)))

    assert result["status"] == "error" and "not registered" in result["message"]
    assert records(node) == [] and not (node.root / GROUP / "files").exists()


@pytest.mark.asyncio
async def test_the_persons_group_file_is_still_the_persons_and_its_words_still_wake_agents(home):
    net = _Network()
    me = _ids(1)[0]
    node = _make_node(net, home, me, [], [me])
    svc = _agent_author(node)

    result = await CoreService.send_group_file(svc, GROUP, str(_take_file(home)), text="@Poster listen")

    assert result["status"] == "success" and "sender_name" not in result
    (rec,) = records(node)
    assert rec["sender_type"] == "human" and "agent_owner" not in rec
    (shown,) = node.api.named("group_file_received")
    assert shown["sender_node_id"] == "user" and shown["sender_name"] == "You"
    assert len(svc.mentions) == 1


@pytest.mark.asyncio
async def test_an_agents_file_over_the_group_cap_is_refused_by_the_send_too(home):
    net = _Network()
    me = _ids(1)[0]
    node = _make_node(net, home, me, [], [me])
    svc = _agent_author(node)
    node.manager.firewall.rules["file_transfer"]["max_size_mb"] = 0.001

    result = await CoreService.send_group_agent_file(svc, GROUP, AGENT_ID, str(_take_file(home)))

    assert result["status"] == "error" and "File too large" in result["message"]
    assert records(node) == []


def test_a_takes_music_block_is_never_exported_to_a_peer():
    att = {"type": "file", "filename": "take.flac", "music": dict(MUSIC), "file_path": "x"}
    (out,) = ConversationMonitor._exported_attachments(SimpleNamespace(), [att])
    assert "music" not in out and out["filename"] == "take.flac"
    assert att["music"] == MUSIC, "this node's own record keeps it"


# --- the tool: who may, the room, the allow-list, the cap, the count -----------------------------

class _LocalApi:
    has_clients = True

    async def broadcast_event(self, name, payload):
        pass


class _GroupService:
    """What the tool reaches: the group send, recorded, and the owner's card machinery."""

    def __init__(self, policy="none"):
        self.policy = policy
        self.sent = []
        self.cards = []
        self.local_api = _LocalApi()
        self.p2p_manager = SimpleNamespace(get_display_name=lambda: "Mike", node_id="dpc-node-this")
        self.group_manager = SimpleNamespace(
            get_group=lambda gid: SimpleNamespace(group_id=gid, name="Studio"))

    async def send_group_agent_file(self, group_id, agent_name, file_path, text="", music=None):
        copy = Path(file_path)
        self.sent.append({"group_id": group_id, "agent_name": agent_name, "file_path": file_path,
                          "name": copy.name, "bytes": copy.read_bytes(), "text": text, "music": music})
        return {"status": "success", "saved_locally": True, "file_path": f"/group/files/{copy.name}",
                "message_id": "m1", "msg_index": 3, "size_bytes": copy.stat().st_size,
                "mime_type": "audio/flac", "sender_name": AGENT_NAME,
                "transfer_ids": ["t1"], "offline_members": ["dpc-node-away"]}

    def _get_agent_telegram_bridge(self, agent_id):
        return None

    async def announce_schedule_approval_request(self, **kwargs):
        self.cards.append(kwargs)
        if self.policy in ("approve", "refuse"):
            tools_core.resolve_schedule_approval(kwargs["request_id"], self.policy == "approve")

    async def announce_schedule_approval_closed(self, **kwargs):
        pass


@pytest.fixture
def registry(tmp_path_factory):
    return ToolRegistry(agent_root=tmp_path_factory.mktemp("agent_reg"))


@pytest.fixture(autouse=True)
def short_cards(monkeypatch):
    monkeypatch.setattr(tools_core, "_SCHEDULE_APPROVAL_TTL_SECONDS", 0.3)
    owner_approval._reset_for_tests()


def _ctx(home, service, asker, *, turn_class=OTHER_NODE_TURN, chat="group-studio", rules=None):
    root = home / "agents" / AGENT_ID
    root.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(agent_root=root, message_source="group", turn_class=turn_class,
                      open_layers=frozenset({"L5", "L6", "EXT"}) if turn_class == OWNER_TURN else frozenset(),
                      asker=asker, conversation_id=chat, dpc_service=service)
    ctx._event_loop = asyncio.get_running_loop()  # the card's loop (tools.core)
    ctx.agent_event_loop = asyncio.get_running_loop()
    if rules is not None:
        ctx.firewall = SimpleNamespace(rules=rules)
    return ctx


def _in_root(ctx, rel, data=FLAC):
    f = ctx.agent_root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


async def _run(registry, args, ctx):
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: registry.execute("chat_post_file", args, ctx=ctx))


@pytest.mark.asyncio
async def test_the_owner_asking_in_a_mixed_group_posts_through_the_group_send_as_the_agent(registry, home):
    service = _GroupService()
    ctx = _ctx(home, service, MIKE)
    _in_root(ctx, "outputs/take.flac")

    out = await _run(registry, {"path": "outputs/take.flac", "caption": "  the   take "}, ctx)

    assert service.cards == [], "the owner asked: no card"
    (sent,) = service.sent
    assert sent["group_id"] == "group-studio" and sent["agent_name"] == AGENT_ID
    assert sent["name"] == "take.flac" and sent["bytes"] == FLAC and sent["text"] == "the take"
    assert sent["music"] is None
    assert not Path(sent["file_path"]).exists(), "the checked temporary copy is removed"
    assert Path(sent["file_path"]).parent.parent == service_mod.DPC_HOME_DIR / "temp"
    assert "Posted take.flac (audio/flac" in out and "'Studio' (group-studio)" in out
    assert f"as {AGENT_NAME}" in out and "offered to 1 connected member(s)" in out
    assert "1 member(s) not connected" in out and "1 of 4 files this turn" in out
    assert ctx.posted_attachments == [], "a group post never rides on the answer"


@pytest.mark.asyncio
async def test_a_take_posted_into_a_group_carries_its_music_block_to_the_send(registry, home, monkeypatch):
    service = _GroupService()
    ctx = _ctx(home, service, MIKE)
    take = _in_root(ctx, "../../music/generated/take_1.flac")

    def fake_service_call(c, method, **kwargs):
        assert method == "get_music_take" and kwargs == {"take_id": "take-1"}
        return {"status": "ok", "take": {"take_id": "take-1", "job_id": "job-1", "job_state": "review",
                                         "state": "review", "library_track_id": "trk-1",
                                         "library_hidden": False, "path": str(take), "file_exists": True}}

    monkeypatch.setattr(music_tools, "_service_call", fake_service_call)
    out = await _run(registry, {"take_id": "take-1"}, ctx)

    (sent,) = service.sent
    assert sent["music"] == MUSIC and sent["name"] == "take_1.flac"
    assert "Posted take_1.flac" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,posted", [("approve", True), ("refuse", False)])
async def test_a_foreign_asker_gets_the_owners_card_naming_the_file_size_type_and_group(
        registry, home, policy, posted):
    service = _GroupService(policy)
    ctx = _ctx(home, service, FRIEND)
    _in_root(ctx, "outputs/take.flac")

    out = await _run(registry, {"path": "outputs/take.flac", "caption": "for you"}, ctx)

    (card,) = service.cards
    assert card["task_type"] == "owner_approval" and card["tool"] == "chat_post_file"
    assert card["asker"]["name"] == "Miron"
    details = card["details"]
    assert "take.flac" in details and f"MB ({len(FLAC)} bytes)" in details and FLAC_MIME in details
    assert "'Studio' (group-studio)" in details and "for you" in details
    assert bool(service.sent) is posted
    assert ("Posted take.flac" in out) is posted


@pytest.mark.asyncio
@pytest.mark.parametrize("asker", [MIKE, FRIEND], ids=["owner", "foreign"])
async def test_a_store_closed_to_the_groups_audience_is_refused_before_any_card(registry, home, asker):
    service = _GroupService("approve")
    ctx = _ctx(home, service, asker)
    _in_root(ctx, "memory/notes.md", b"the owner's private notes")

    out = await _run(registry, {"path": "memory/notes.md"}, ctx)

    assert "private memory" in out, out
    assert service.sent == [] and service.cards == []


@pytest.mark.asyncio
async def test_the_allow_list_by_content_holds_in_a_group(registry, home):
    service = _GroupService()
    ctx = _ctx(home, service, MIKE)
    _in_root(ctx, "outputs/song.flac", b"MZ\x90\x00" + b"\x00" * 64)
    _in_root(ctx, "outputs/tool.exe", b"MZ\x90\x00")

    renamed = await _run(registry, {"path": "outputs/song.flac"}, ctx)
    program = await _run(registry, {"path": "outputs/tool.exe"}, ctx)

    assert "chat_post_file refused" in renamed and "a program" in renamed
    assert "chat_post_file refused" in program and "not a type that may be posted" in program
    assert service.sent == []


@pytest.mark.asyncio
async def test_a_file_over_the_group_transfer_limit_is_refused_with_the_limit_named(registry, home):
    service = _GroupService()
    ctx = _ctx(home, service, MIKE, rules={"file_transfer": {"max_size_mb": 0.001}})
    _in_root(ctx, "outputs/take.flac")

    out = await _run(registry, {"path": "outputs/take.flac"}, ctx)

    assert "File too large" in out and "group file-transfer limit of 0.001 MB" in out
    assert service.sent == []


def test_the_group_cap_is_the_file_transfer_cap_and_never_above_d41s_default():
    ctx = SimpleNamespace(firewall=SimpleNamespace(rules={}))
    assert chat_files._group_cap_mb(ctx) == service_mod.AGENT_CHAT_FILE_DEFAULT_MAX_MB
    ctx.firewall.rules = {"file_transfer": {"max_size_mb": 250}}
    assert chat_files._group_cap_mb(ctx) == 250


@pytest.mark.asyncio
async def test_the_fifth_file_and_a_second_post_of_one_file_in_a_turn_are_refused(registry, home):
    service = _GroupService()
    ctx = _ctx(home, service, MIKE)
    for i in range(5):
        _in_root(ctx, f"outputs/take{i}.flac", FLAC + bytes([i]))

    outs = [await _run(registry, {"path": f"outputs/take{i}.flac"}, ctx) for i in (0, 0, 1, 2, 3, 4)]

    assert "already posted into this group in this turn" in outs[1]
    assert "already posted 4 files" in outs[5]
    assert [s["name"] for s in service.sent] == [f"take{i}.flac" for i in range(4)]


@pytest.mark.asyncio
async def test_a_send_the_group_refuses_frees_its_slot_and_says_why(registry, home):
    service = _GroupService()

    async def refused(**kw):
        return {"status": "error", "message": "agent name not registered for this node in this group: x"}

    service.send_group_agent_file = refused
    ctx = _ctx(home, service, MIKE)
    _in_root(ctx, "outputs/take.flac")

    out = await _run(registry, {"path": "outputs/take.flac"}, ctx)

    assert "the group send refused the file: agent name not registered" in out
    assert ctx.posted_group_files == []
    assert list((service_mod.DPC_HOME_DIR / "temp").iterdir()) == [], "no temporary copy is left"


@pytest.mark.asyncio
async def test_telegram_and_discord_are_still_refused_and_no_group_send_is_tried(registry, home):
    service = _GroupService()
    ctx = _ctx(home, service, MIKE)
    ctx.message_source = "telegram_owner"
    ctx.reply_telegram_chat_id = "5"
    _in_root(ctx, "outputs/take.flac")

    out = await _run(registry, {"path": "outputs/take.flac"}, ctx)

    assert "came from Telegram" in out and service.sent == []


def test_group_allowed_off_withholds_the_tool_in_groups_only(tmp_path):
    tools = {"chat_post_file": True}
    profile = {"tools": tools, "tool_settings": {"chat_post_file": {"group_allowed": False}}}
    rules = {"dpc_agent": {"enabled": True, "tools": tools}, "agent_profiles": {AGENT_ID: profile}}
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    agent = DpcAgent.__new__(DpcAgent)
    agent._firewall = ContextFirewall(path)
    agent._firewall_profile = AGENT_ID

    assert "chat_post_file" not in agent._firewall_allowed_tools("group", "group-studio")
    assert "chat_post_file" in agent._firewall_allowed_tools("ui", AGENT_ID)


# --- one real agent turn in a group ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_owner_turn_in_a_group_posts_the_file_through_the_agents_service(tmp_path, monkeypatch):
    from tests.test_chat_post_file_copies_a_file_into_the_agents_own_chat_and_the_answer_carries_it import (
        CID, GROUPS, THIS_NODE, _take, _tool_results, _World,
    )
    # The 1:1 test's `home` fixture, inline: a fixture cannot be imported by name.
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("DPC_HOME", str(home / ".dpc"))
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", home / ".dpc")
    from dpc_client_core.dpc_agent.retrieval import factory as retrieval_factory
    monkeypatch.setattr(retrieval_factory, "_derive_embedding_metadata", lambda cfg: ("BAAI/bge-m3", 1024))
    w = _World(tmp_path, home, monkeypatch)
    sent = []

    async def send_group_agent_file(group_id, agent_name, file_path, text="", music=None):
        sent.append((group_id, agent_name, Path(file_path).name, Path(file_path).read_bytes(), text))
        return {"status": "success", "file_path": file_path, "size_bytes": 10, "mime_type": "audio/flac",
                "sender_name": "Poster", "transfer_ids": [], "offline_members": []}

    w.agent._service = SimpleNamespace(group_manager=GROUPS, p2p_manager=None,
                                       send_group_agent_file=send_group_agent_file)
    owner = Asker(ASKER_HUMAN, "Mike", THIS_NODE, is_owner=True, verified=True)
    take = _take(home)

    await w.turn([{"path": str(take), "caption": "here"}], conv="group-own", source="group", asker=owner)

    assert sent == [("group-own", CID, "take.flac", take.read_bytes(), "here")]
    assert "Posted take.flac" in _tool_results(w) and "into the group" in _tool_results(w)
    assert w.all_chat_files() == [], "nothing is copied into a chat's files by the tool itself"
