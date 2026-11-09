"""search_session_archives ranks from a BM25 index beside each archive (ARCHIVE-GROUP-ACCESS
step 2; Mike's call, 2026-11-09, backed by Zcode and Johnny).

One index per conversation archive in `archive/bm25/`, built on the first search, then
brought up to date by adding only the session files no row names yet; a pruned file's
rows go; a store that is missing, unreadable, half-saved or of another tokenizer version
is rebuilt from the session files. The step-1 rule decides first: only the archives this
turn opens, and on a turn in a group with another node only the sessions every current
member witnessed — a hit anywhere else is neither returned nor counted.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent import bm25_index, sleep_pipeline
from dpc_client_core.dpc_agent.index_writer import writer_for
from dpc_client_core.dpc_agent.agent import (
    ALL_MEMORY_LAYERS, OTHER_NODE_TURN, OWNER_TURN, DpcAgent,
)
from dpc_client_core.dpc_agent.tools import archive
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.firewall import ContextFirewall

THIS = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
AGENT = "agent_x"
GROUP = "group-aaaaaaaaaaaa"
FIRST = "2026-11-01T10-00-00_reset_session.json"
SECOND = "2026-11-02T10-00-00_reset_session.json"
THIRD = "2026-11-03T10-00-00_reset_session.json"


def _session(conv_dir: pathlib.Path, name: str, messages, group=None, month="11") -> pathlib.Path:
    folder = conv_dir / "archive" / "2026" / month
    folder.mkdir(parents=True, exist_ok=True)
    data = {"archived_at": "2026-11-01T10:00:00Z", "session_reason": "reset",
            "messages": [m if isinstance(m, dict) else
                         {"role": "user", "sender_name": "Mike", "content": m,
                          "timestamp": "2026-11-01T10:00:00Z"} for m in messages]}
    if group is not None:
        data["group"] = group
    path = folder / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _firewall(tmp, include):
    rules = tmp / "privacy_rules.json"
    rules.write_text(json.dumps({"dpc_agent": {"enabled": True}, "agent_profiles": {
        AGENT: {"history": {"preserve_on_reset": True, "archive_include_groups": include}}}}),
        encoding="utf-8")
    return ContextFirewall(rules)


def _ctx(tmp, conv_dir, conversation_id, cls=OWNER_TURN, include=False):
    layers = ALL_MEMORY_LAYERS if cls == OWNER_TURN else frozenset({"L6"})
    ctx = ToolContext(agent_root=tmp / "agents" / AGENT, firewall=_firewall(tmp, include),
                      message_source="ui" if cls == OWNER_TURN else "group", turn_class=cls,
                      open_layers=layers, conversation_id=conversation_id)
    ctx.conversation_monitor = SimpleNamespace(
        conversation_id=conversation_id, _get_history_path=lambda: conv_dir / "history.json")
    agent = DpcAgent.__new__(DpcAgent)
    agent._firewall_profile = AGENT
    agent._service = SimpleNamespace(group_manager=SimpleNamespace(node_id=THIS))
    ctx._agent = agent
    return ctx


def _search(ctx, query, **kw) -> dict:
    return json.loads(archive.search_session_archives(ctx, query, last_n=50, **kw))


@pytest.fixture
def own(tmp_path):
    conv = tmp_path / "conversations" / AGENT
    _session(conv, FIRST, ["about the harbour once", "nothing to see"])
    _session(conv, SECOND, ["harbour harbour harbour, the harbour again"], month="10")
    return SimpleNamespace(tmp=tmp_path, conv=conv, ctx=_ctx(tmp_path, conv, AGENT))


@pytest.fixture
def read_rows(monkeypatch):
    """The session files the index read, in order."""
    seen: list = []
    real = archive._rows

    def spy(data, filename):
        seen.append(filename)
        return real(data, filename)

    monkeypatch.setattr(archive, "_rows", spy)
    return seen


def _indexed(conv) -> set:
    chunks = json.loads((conv / "archive" / "bm25" / "bm25_chunks.json").read_text(encoding="utf-8"))
    return {m["filename"] for m in chunks}


# -- the index ----------------------------------------------------------------


def test_the_first_search_builds_the_index_beside_the_archive_and_ranks(own, read_rows):
    assert not (own.conv / "archive" / "bm25").exists()
    found = _search(own.ctx, "harbour")
    store = own.conv / "archive" / "bm25"
    assert (store / "bm25_chunks.json").is_file()
    assert json.loads((store / bm25_index.TOKENIZE_FILE).read_text())["tokenize_version"] == \
        bm25_index.TOKENIZE_VERSION
    assert sorted(read_rows) == ["2026/10/" + SECOND, "2026/11/" + FIRST]
    assert [(m["filename"], m["message_index"]) for m in found["matches"]] == [
        (SECOND, 0), (FIRST, 0)]  # four mentions rank above one
    assert found["total_matches"] == 2 and "note" not in found
    scores = [m["score"] for m in found["matches"]]
    assert scores[0] > scores[1] > 0
    first = found["matches"][0]
    assert (first["conversation_id"], first["scope_matched"], first["role"],
            first["sender_name"]) == (AGENT, "content", "user", "Mike")


def test_a_later_search_reads_only_the_session_files_no_row_names(own, read_rows):
    _search(own.ctx, "harbour")
    read_rows.clear()
    assert _search(own.ctx, "harbour")["total_matches"] == 2
    assert read_rows == []
    _session(own.conv, THIRD, ["a harbour in the third"])
    found = _search(own.ctx, "harbour")
    assert read_rows == ["2026/11/" + THIRD]
    assert found["total_matches"] == 3
    assert _indexed(own.conv) == {"2026/10/" + SECOND, "2026/11/" + FIRST, "2026/11/" + THIRD}


def test_a_pruned_session_takes_its_rows_with_it(own):
    _search(own.ctx, "harbour")
    (own.conv / "archive" / "2026" / "10" / SECOND).unlink()
    found = _search(own.ctx, "harbour")
    assert [m["filename"] for m in found["matches"]] == [FIRST]
    assert _indexed(own.conv) == {"2026/11/" + FIRST}


@pytest.mark.parametrize("damage", ["older-tokenizer", "half-saved", "corrupt-chunks",
                                    "corrupt-store", "rows-disagree"])
def test_a_store_it_cannot_trust_is_rebuilt_from_the_session_files(own, read_rows, damage):
    _search(own.ctx, "harbour")
    store = own.conv / "archive" / "bm25"
    if damage == "older-tokenizer":
        (store / bm25_index.TOKENIZE_FILE).write_text(json.dumps({"tokenize_version": 1}))
    elif damage == "half-saved":
        (store / bm25_index.TOKENIZE_FILE).unlink()
    elif damage == "corrupt-chunks":
        (store / "bm25_chunks.json").write_text("{not json", encoding="utf-8")
    elif damage == "corrupt-store":
        for f in (store / "bm25").iterdir():
            f.write_bytes(b"garbage")
    else:
        (store / "bm25_chunks.json").write_text(json.dumps([{"source_file": "x", "filename": "x"}]))
    read_rows.clear()
    found = _search(own.ctx, "harbour")
    assert sorted(read_rows) == ["2026/10/" + SECOND, "2026/11/" + FIRST]
    assert [(m["filename"], m["message_index"]) for m in found["matches"]] == [
        (SECOND, 0), (FIRST, 0)]
    assert "note" not in found
    assert json.loads((store / bm25_index.TOKENIZE_FILE).read_text())["tokenize_version"] == \
        bm25_index.TOKENIZE_VERSION


def test_a_name_glued_to_punctuation_is_found_and_the_snippet_sits_on_it(tmp_path):
    conv = tmp_path / "conversations" / AGENT
    text = "x" * 300 + " thanks (@razzant) for the review " + "y" * 300
    _session(conv, FIRST, [text, "unrelated words"])
    found = _search(_ctx(tmp_path, conv, AGENT), "razzant", snippet_chars=20)
    [match] = found["matches"]
    assert match["score"] > 0
    assert "(@razzant)" in match["snippet"] and match["snippet"].startswith("...")
    assert len(match["snippet"]) < 80


def test_scope_keeps_its_meaning(tmp_path):
    conv = tmp_path / "conversations" / AGENT
    _session(conv, FIRST, [
        {"role": "assistant", "content": "plain reply", "thinking": "consider ouroboros here"},
        {"role": "user", "content": "ouroboros in the open"},
        {"role": "assistant", "content": "ouroboros again", "thinking": "ouroboros twice"},
    ])
    ctx = _ctx(tmp_path, conv, AGENT)
    content = _search(ctx, "ouroboros")
    assert {(m["message_index"], m["scope_matched"]) for m in content["matches"]} == {
        (1, "content"), (2, "content")}
    thinking = _search(ctx, "ouroboros", scope="thinking")
    assert {(m["message_index"], m["scope_matched"]) for m in thinking["matches"]} == {
        (0, "thinking"), (2, "thinking")}
    both = _search(ctx, "ouroboros", scope="both")
    assert sorted(m["message_index"] for m in both["matches"]) == [0, 1, 2]  # once each
    assert both["total_matches"] == 3


# -- the step-1 rule decides first --------------------------------------------


@pytest.fixture
def world(tmp_path):
    base = tmp_path / "conversations"
    _session(base / AGENT, FIRST, ["own harbour"])
    group = base / f"{GROUP}-work"
    group.mkdir(parents=True)
    (group / "metadata.json").write_text(json.dumps({
        "group_id": GROUP, "name": "work", "members": [THIS, PEER],
        "agents": {THIS: [AGENT]}, "version": 3}), encoding="utf-8")
    witnessed = {"group_id": GROUP, "members": [THIS, PEER], "version": 3}
    _session(group, FIRST, ["one harbour witnessed"], witnessed)
    # Would rank first: many mentions, but no recorded members / fewer members.
    _session(group, SECOND, ["harbour " * 20])
    _session(group, THIRD, ["harbour " * 10], {"group_id": GROUP, "members": [THIS], "version": 2})
    return SimpleNamespace(tmp=tmp_path, base=base, group=group)


def test_a_hit_in_a_skipped_session_is_neither_returned_nor_counted(world):
    ctx = _ctx(world.tmp, world.group, GROUP, cls=OTHER_NODE_TURN)
    found = _search(ctx, "harbour")
    assert [(m["filename"], m["snippet"]) for m in found["matches"]] == [
        (FIRST, "one harbour witnessed")]
    assert found["total_matches"] == 1 and found["matches"][0]["score"] > 0
    assert found["note"].startswith("2 session(s) of this group exist but are not shown")
    # The owner reading the same group sees all three, ranked.
    owner = _search(_ctx(world.tmp, world.group, GROUP), "harbour")
    assert [m["filename"] for m in owner["matches"]] == [SECOND, THIRD, FIRST]


def test_owner_with_groups_included_ranks_across_both_archives(world):
    found = _search(_ctx(world.tmp, world.base / AGENT, AGENT, include=True), "harbour")
    assert {(m["conversation_id"], m["filename"]) for m in found["matches"]} == {
        (AGENT, FIRST), (GROUP, FIRST), (GROUP, SECOND), (GROUP, THIRD)}


def test_a_closed_conversation_s_index_is_never_read(world, monkeypatch):
    # The group's index exists from an earlier turn that could read it.
    _search(_ctx(world.tmp, world.group, GROUP), "harbour")
    opened: list = []
    real = archive._index_of
    monkeypatch.setattr(archive, "_index_of", lambda d: opened.append(d) or real(d))
    loaded: list = []
    real_load = bm25_index.BM25Index.load
    monkeypatch.setattr(bm25_index.BM25Index, "load",
                        lambda self: loaded.append(self.index_dir) or real_load(self))

    found = _search(_ctx(world.tmp, world.base / AGENT, AGENT, include=False), "harbour")
    assert [m["conversation_id"] for m in found["matches"]] == [AGENT]
    assert opened == [world.base / AGENT / "archive"]
    assert all(world.group not in p.parents for p in loaded)


def test_a_turn_with_every_session_skipped_reads_no_index(world, monkeypatch):
    for name in (FIRST,):
        _session(world.group, name, ["harbour"], {"group_id": GROUP, "members": [THIS],
                                                  "version": 2})
    opened: list = []
    monkeypatch.setattr(archive, "_index_of", lambda d: opened.append(d))
    found = _search(_ctx(world.tmp, world.group, GROUP, cls=OTHER_NODE_TURN), "harbour")
    assert found["matches"] == [] and found["total_matches"] == 0
    assert opened == [] and not (world.group / "archive" / "bm25").exists()


# -- the substring fallback ---------------------------------------------------


def test_an_index_that_cannot_be_built_falls_back_to_the_substring_and_says_so(own, monkeypatch):
    def broken(_):
        raise OSError("disk says no")
    monkeypatch.setattr(archive, "_index_of", broken)
    found = _search(own.ctx, "harbour")
    assert {m["filename"] for m in found["matches"]} == {FIRST, SECOND}
    assert all("score" not in m for m in found["matches"])
    assert "keyword index could not be built or loaded" in found["note"]


@pytest.mark.parametrize("query,expected", [("(@", [0]), ("arbo", [0, 1]), ("the", [0, 1, 2])],
                         ids=["punctuation-only", "part-of-a-word", "stop-word"])
def test_a_query_bm25_cannot_answer_keeps_the_substring_match(tmp_path, query, expected):
    conv = tmp_path / "conversations" / AGENT
    _session(conv, FIRST, ["the harbour (@razzant)", "the harbour twice", "other"])
    found = _search(_ctx(tmp_path, conv, AGENT), query)
    assert [m["message_index"] for m in found["matches"]] == expected
    assert all("score" not in m for m in found["matches"]) and "note" not in found


# -- every other reader of the archive folder ignores bm25/ -------------------


def test_the_sleep_pipeline_reads_session_files_only(world):
    _search(_ctx(world.tmp, world.group, GROUP), "harbour")
    assert (world.group / "archive" / "bm25" / "bm25_chunks.json").exists()
    digests = sleep_pipeline._collect_group_archive_digests(world.group, AGENT)
    assert sorted(pathlib.Path(d["archive_path"]).name for d in digests) == [FIRST, SECOND, THIRD]


def test_clearing_the_archives_drops_the_index(tmp_path, monkeypatch):
    from dpc_client_core.agent_service import AgentService
    home = tmp_path / "home"
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    conv = home / ".dpc" / "conversations" / AGENT
    _session(conv, FIRST, ["harbour one"])
    _session(conv, SECOND, ["harbour two"])
    _search(_ctx(tmp_path, conv, AGENT), "harbour")
    store = conv / "archive" / "bm25"
    assert store.exists()
    writer = writer_for(store)
    result = asyncio.run(AgentService.clear_session_archives(SimpleNamespace(), AGENT, 1))
    assert result["deleted_count"] == 1
    writer.submit(lambda: None).result(timeout=10)  # the queue is FIFO: the removal ran
    assert not store.exists()
    found = _search(_ctx(tmp_path, conv, AGENT), "harbour")
    assert [m["filename"] for m in found["matches"]] == [SECOND]
