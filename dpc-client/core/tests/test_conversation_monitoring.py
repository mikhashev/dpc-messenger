"""
Test ConversationMonitor integration (Phase 4.2)
Tests automatic detection, manual extraction, and toggle functionality
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from dpc_client_core.service import CoreService
from dpc_client_core.conversation_monitor import ConversationMonitor, Message as ConvMessage


@pytest.fixture
def core_service():
    """Create a CoreService instance for testing"""
    with patch('dpc_client_core.service.P2PManager'), \
         patch('dpc_client_core.service.HubClient'), \
         patch('dpc_client_core.service.LLMManager'), \
         patch('dpc_client_core.service.LocalApiServer'), \
         patch('dpc_client_core.service.ContextFirewall'), \
         patch('dpc_client_core.service.ConsensusManager'), \
         patch('dpc_client_core.service.PCMCore'):

        service = CoreService()
        service.p2p_manager = MagicMock()
        service.p2p_manager.node_id = "dpc-node-test123"
        service.llm_manager = MagicMock()
        service.llm_manager.query_llm = AsyncMock(return_value={
            "status": "OK",
            "content": "Test AI response",
            "model": "test-model"
        })
        service.local_api = MagicMock()
        service.local_api.broadcast_event = AsyncMock()
        service.consensus_manager = MagicMock()
        service.consensus_manager.propose_commit = AsyncMock()
        service.peer_metadata = {
            "peer123": {"name": "Test Peer"}
        }
        # Mock context_cache to return None (no contexts)
        service.context_cache = MagicMock()
        service.context_cache.local_context = None

        # Sync KnowledgeService with the replaced references so tests see
        # the same mock objects when accessing service or knowledge_service.
        if service.knowledge_service:
            service.knowledge_service.p2p_manager = service.p2p_manager
            service.knowledge_service.peer_metadata = service.peer_metadata
            service.knowledge_service.local_api = service.local_api
            service.knowledge_service.consensus_manager = service.consensus_manager

        return service


class TestConversationMonitorInitialization:
    """Test conversation monitor creation with correct participants"""

    @pytest.mark.asyncio
    async def test_local_ai_monitor_participants(self, core_service):
        """Local AI conversations should have 2 participants (user + DPC Agent)"""
        monitor = core_service._get_or_create_conversation_monitor("local_ai")

        assert monitor is not None
        assert monitor.conversation_id == "local_ai"
        assert len(monitor.participants) == 2
        assert monitor.participants[0]["node_id"] == "dpc-node-test123"
        assert monitor.participants[0]["name"] == "User"
        assert monitor.participants[0]["context"] == "local"
        assert monitor.participants[1]["node_id"] == "local_ai"
        assert monitor.participants[1]["name"] == "DPC Agent"
        assert monitor.participants[1]["context"] == "ai_agent"

    @pytest.mark.asyncio
    async def test_peer_chat_monitor_two_participants(self, core_service):
        """Peer conversations should have 2 participants (user + peer)"""
        monitor = core_service._get_or_create_conversation_monitor("peer123")

        assert monitor is not None
        assert monitor.conversation_id == "peer123"
        assert len(monitor.participants) == 2

        # Check user participant
        user_participant = next((p for p in monitor.participants if p["node_id"] == "dpc-node-test123"), None)
        assert user_participant is not None
        assert user_participant["name"] == "User"
        assert user_participant["context"] == "local"

        # Check peer participant
        peer_participant = next((p for p in monitor.participants if p["node_id"] == "peer123"), None)
        assert peer_participant is not None
        assert peer_participant["name"] == "Test Peer"
        assert peer_participant["context"] == "peer"

    @pytest.mark.asyncio
    async def test_monitor_singleton_per_conversation(self, core_service):
        """Same conversation_id should return same monitor instance"""
        monitor1 = core_service._get_or_create_conversation_monitor("peer123")
        monitor2 = core_service._get_or_create_conversation_monitor("peer123")

        assert monitor1 is monitor2


class TestP2PMessageMonitoring:
    """Test automatic monitoring of P2P messages"""

    @pytest.mark.asyncio
    async def test_p2p_message_feeds_monitor(self, core_service):
        """P2P messages should be fed to conversation monitor"""
        with patch.object(ConversationMonitor, 'on_message', new_callable=AsyncMock) as mock_on_message:
            mock_on_message.return_value = None  # No proposal generated

            # Simulate receiving a P2P message
            message = {
                "command": "SEND_TEXT",
                "payload": {
                    "message_id": "msg123",
                    "text": "Hello, this is a test message"
                }
            }

            await core_service.on_p2p_message_received("peer123", message)

            # Verify monitor was called (would be called if monitor exists)
            # Note: Since we're using real monitor creation, check it was created
            assert "peer123" in core_service.conversation_monitors

class TestManualExtraction:
    """Test manual knowledge extraction via End Session"""

    @pytest.mark.asyncio
    async def test_end_session_creates_proposal(self, core_service):
        """Ending session should force knowledge extraction"""
        # Create a monitor first and add some messages
        monitor = core_service._get_or_create_conversation_monitor("peer123")

        # Add a few messages to the buffer
        from dpc_client_core.conversation_monitor import Message as ConvMessage
        for i in range(3):
            monitor.message_buffer.append(ConvMessage(
                message_id=f"msg{i}",
                conversation_id="peer123",
                sender_node_id="peer123",
                sender_name="Test Peer",
                text=f"Test message {i}",
                timestamp=datetime.now(timezone.utc).isoformat()
            ))

        # Mock the generate proposal to return None (no knowledge detected)
        # This tests the "force" parameter
        with patch.object(monitor, 'generate_commit_proposal', new_callable=AsyncMock) as mock_generate:
            mock_generate.return_value = None  # No proposal

            result = await core_service.end_conversation_session("peer123")

            # Should get success even with no proposal
            assert result["status"] == "success"
            assert "No significant knowledge" in result["message"]

            # Verify force=True was passed (proposed_by and initiated_by are also passed)
            from unittest.mock import ANY
            mock_generate.assert_called_once_with(
                force=True, proposed_by=ANY, initiated_by="user_request"
            )

    @pytest.mark.asyncio
    async def test_end_session_no_knowledge(self, core_service):
        """Ending session with no knowledge should return success but no proposal"""
        with patch.object(ConversationMonitor, 'generate_commit_proposal', new_callable=AsyncMock) as mock_generate:
            mock_generate.return_value = None  # No proposal

            result = await core_service.end_conversation_session("peer123")

            assert result["status"] == "success"
            assert "No significant knowledge" in result["message"]

            # Should not broadcast or propose
            core_service.local_api.broadcast_event.assert_not_called()
            core_service.consensus_manager.propose_commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_end_session_works_for_local_ai(self, core_service):
        """Should be able to manually extract from local AI conversations"""
        with patch.object(ConversationMonitor, 'generate_commit_proposal', new_callable=AsyncMock) as mock_generate:
            mock_generate.return_value = None

            result = await core_service.end_conversation_session("local_ai")

            assert result["status"] == "success"
            # Verify local_ai monitor was created
            assert "local_ai" in core_service.conversation_monitors


class TestIntegrationScenarios:
    """Test complete workflows"""

    @pytest.mark.asyncio
    async def test_complete_peer_conversation_flow(self, core_service):
        """Simulate a complete peer conversation with monitoring"""
        # Simulate 5 messages
        for i in range(5):
            message = {
                "command": "SEND_TEXT",
                "payload": {
                    "message_id": f"msg{i}",
                    "text": f"Message {i} about Python programming"
                }
            }
            await core_service.on_p2p_message_received("peer123", message)

        # Verify monitor exists and has conversation
        monitor = core_service.conversation_monitors.get("peer123")
        assert monitor is not None
        assert monitor.conversation_id == "peer123"
        assert len(monitor.participants) == 2

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
