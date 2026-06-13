"""ADR-031: per-reader role derivation + trigger dedup by message_id."""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from dpc_client_core.dpc_agent.context import derive_history_role, build_llm_messages
from dpc_client_core.dpc_agent.agent import select_prior_history


READER = {
    "agent_id": "agent_001",
    "display_name": "Ark",
    "node_id": "dpc-node-aaa",
}


def _agent_row(name, owner="dpc-node-aaa", **extra):
    return {"role": "user", "content": "hi", "sender_type": "agent",
            "sender_name": name, "agent_owner": owner, **extra}


class TestDeriveHistoryRoleGroup:
    def test_own_message_is_assistant(self):
        assert derive_history_role(_agent_row("Ark"), READER, is_group=True) == "assistant"

    def test_other_agent_same_node_is_user(self):
        assert derive_history_role(_agent_row("Warren"), READER, is_group=True) == "user"

    def test_remote_agent_same_name_is_user(self):
        row = _agent_row("Ark", owner="dpc-node-bbb")
        assert derive_history_role(row, READER, is_group=True) == "user"

    def test_agent_id_accepted_in_owner_slot(self):
        row = _agent_row("Ark", owner="agent_001")
        assert derive_history_role(row, READER, is_group=True) == "assistant"

    def test_human_is_user_even_with_name_collision(self):
        row = {"role": "user", "content": "x", "sender_type": "human", "sender_name": "Ark"}
        assert derive_history_role(row, READER, is_group=True) == "user"

    def test_agent_without_owner_never_assistant_in_group(self):
        row = _agent_row("Ark", owner=None)
        assert derive_history_role(row, READER, is_group=True) == "user"

    def test_name_match_without_sender_type_stays_user_in_group(self):
        row = {"role": "user", "content": "x", "sender_name": "Ark"}
        assert derive_history_role(row, READER, is_group=True) == "user"

    def test_system_record_excluded(self):
        row = {"role": "user", "content": "x", "sender_type": "system", "sender_name": "sys"}
        assert derive_history_role(row, READER, is_group=True) is None

    def test_stored_role_ignored_for_groups(self):
        row = {"role": "assistant", "content": "x", "sender_type": "human", "sender_name": "Mike"}
        assert derive_history_role(row, READER, is_group=True) == "user"

    def test_identity_less_assistant_row_in_group_stays_user(self):
        assert derive_history_role({"role": "assistant", "content": "x"}, READER, True) == "user"


class TestDeriveHistoryRoleOneToOne:
    def test_legacy_assistant_write_matches_by_name(self):
        row = {"role": "assistant", "content": "x", "sender_name": "Ark",
               "sender_node_id": "agent_001"}
        assert derive_history_role(row, READER, is_group=False) == "assistant"

    def test_user_row_is_user(self):
        row = {"role": "user", "content": "x", "sender_name": "User",
               "sender_node_id": "dpc-node-aaa"}
        assert derive_history_role(row, READER, is_group=False) == "user"

    def test_identity_less_row_falls_back_to_stored_role(self):
        assert derive_history_role({"role": "assistant", "content": "x"}, READER, False) == "assistant"
        assert derive_history_role({"role": "user", "content": "x"}, READER, False) == "user"

    def test_peer_row_becomes_user_turn(self):
        row = {"role": "peer", "content": "x", "sender_name": "Alice",
               "sender_node_id": "dpc-node-bbb"}
        assert derive_history_role(row, READER, is_group=False) == "user"

    def test_degeneracy_derived_equals_stored_for_real_1to1_history(self):
        history = [
            {"role": "user", "content": "q1", "sender_name": "User", "sender_node_id": "dpc-node-aaa"},
            {"role": "assistant", "content": "a1", "sender_name": "Ark", "sender_node_id": "agent_001"},
            {"role": "user", "content": "q2", "sender_name": "mike (Telegram)", "sender_node_id": "dpc-node-aaa"},
            {"role": "assistant", "content": "a2", "sender_name": "Ark", "sender_node_id": "agent_001"},
        ]
        for row in history:
            assert derive_history_role(row, READER, is_group=False) == row["role"]


class TestSelectPriorHistory:
    HISTORY = [
        {"id": "m1", "content": "a"},
        {"id": "m2", "content": "b"},
        {"id": "m3", "content": "c"},
    ]

    def test_positional_slice_without_trigger_id(self):
        assert select_prior_history(self.HISTORY, None) == self.HISTORY[:-1]

    def test_dedup_by_trigger_id(self):
        result = select_prior_history(self.HISTORY, "m3")
        assert [m["id"] for m in result] == ["m1", "m2"]

    def test_mid_invoke_message_is_kept(self):
        history = self.HISTORY + [{"id": "m4", "content": "d"}]
        result = select_prior_history(history, "m3")
        assert [m["id"] for m in result] == ["m1", "m2", "m4"]

    def test_single_message_history(self):
        assert select_prior_history([{"id": "m1", "content": "a"}], None) is None
        assert select_prior_history([{"id": "m1", "content": "a"}], "m1") == []


class TestGroupHistoryOwnership:
    @pytest.fixture()
    def fake_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
        return tmp_path

    def _monitor(self, conv_id):
        from dpc_client_core.conversation_monitor import ConversationMonitor
        return ConversationMonitor(
            conversation_id=conv_id,
            participants=[{"node_id": "dpc-node-aaa", "name": "Mike", "context": ""}],
            llm_manager=None,
        )

    def test_readonly_consumer_does_not_clobber_writer(self, fake_home):
        writer = self._monitor("group-own")
        writer.add_message(role="user", content="m1", message_id="a",
                           sender_name="Mike", sender_type="human")
        writer.save_history()

        reader = self._monitor("group-own")
        reader.load_history()

        writer.add_message(role="user", content="m2", message_id="b",
                           sender_name="Ark", sender_type="agent", agent_owner="dpc-node-aaa")
        writer.save_history()

        reader.load_history()
        assert [m["id"] for m in reader.get_message_history()] == ["a", "b"]

    def test_on_message_persists_identity_fields(self, fake_home):
        import asyncio
        from dpc_client_core.conversation_monitor import Message as ConvMessage

        monitor = self._monitor("group-own2")
        asyncio.run(monitor.on_message(ConvMessage(
            message_id="x1",
            conversation_id="group-own2",
            sender_node_id="dpc-node-remote",
            sender_name="Warren",
            text="hi",
            timestamp="2026-06-10T12:00:00Z",
            sender_type="agent",
            agent_owner="dpc-node-remote",
        )))
        monitor.save_history()

        fresh = self._monitor("group-own2")
        fresh.load_history()
        row = fresh.get_message_history()[-1]
        assert row["sender_type"] == "agent"
        assert row["agent_owner"] == "dpc-node-remote"
        assert row["id"] == "x1"


class TestBuildLlmMessagesDerivation:
    @pytest.fixture()
    def agent_root(self, tmp_path):
        return tmp_path / "agent_001"

    def _build(self, agent_root, conversation_id, history, reader=READER):
        from dpc_client_core.dpc_agent.memory import Memory
        messages, _ = build_llm_messages(
            agent_root=agent_root,
            memory=Memory(agent_root),
            task={"id": conversation_id, "type": "chat", "text": "current question"},
            conversation_history=history,
            reader_identity=reader,
        )
        return [m for m in messages if m["role"] != "system"]

    def test_group_alternation_per_reader(self, agent_root):
        history = [
            {"role": "user", "content": "q", "sender_type": "human", "sender_name": "Mike",
             "msg_index": 1},
            _agent_row("Ark", content="my answer", msg_index=2),
            _agent_row("Warren", content="other answer", msg_index=3),
        ]
        turns = self._build(agent_root, "group-test123", history)
        # 3 history turns + current user message
        assert [t["role"] for t in turns] == ["user", "assistant", "user", "user"]
        assert "Warren" in turns[2]["content"]

    def test_1to1_payload_equivalent_to_stored_roles(self, agent_root):
        history = [
            {"role": "user", "content": "q1", "sender_name": "User", "msg_index": 1},
            {"role": "assistant", "content": "a1", "sender_name": "Ark", "msg_index": 2},
        ]
        derived = self._build(agent_root, "agent_001", history)
        legacy = self._build(agent_root, "agent_001", history, reader=None)
        assert derived == legacy
