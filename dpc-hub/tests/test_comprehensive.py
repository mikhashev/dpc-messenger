"""
Comprehensive Test Suite for DPC Hub Fixes

Tests for:
- Cryptographic node identity validation
- Node registration workflow
- Token blacklist / logout
- Profile endpoints (GET, PUT, DELETE)
- Enhanced WebSocket error handling
- Rate limiting
- Health checks
"""

import pytest
import asyncio
from httpx import AsyncClient
from datetime import datetime, timedelta

# Mock data for testing
VALID_NODE_ID = "dpc-node-8b066c7f3d7e"
VALID_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA2Z3qX0NKZC3m7hNQx5sN
Test public key content here
-----END PUBLIC KEY-----"""

VALID_CERTIFICATE = """-----BEGIN CERTIFICATE-----
MIIDXTCCAkWgAwIBAgIJAKqH3h2wQ3WlMA0GCSqGSIb3DQEBCwUAMEUxCzAJBgNV
Test certificate content here
-----END CERTIFICATE-----"""


# ============================================================================
# CRYPTO VALIDATION TESTS
# ============================================================================

class TestCryptoValidation:
    """Test cryptographic validation functions"""
    
    def test_generate_node_id_from_public_key(self):
        """Test node_id generation is deterministic"""
        from dpc_hub.crypto_validation import generate_node_id_from_public_key
        
        node_id1 = generate_node_id_from_public_key(VALID_PUBLIC_KEY)
        node_id2 = generate_node_id_from_public_key(VALID_PUBLIC_KEY)
        
        # Should be deterministic
        assert node_id1 == node_id2
        
        # Should have correct format
        assert node_id1.startswith("dpc-node-")
        assert len(node_id1) == 25  # "dpc-node-" + 16 hex chars
    
    def test_validate_node_id_format(self):
        """Test node_id format validation"""
        from dpc_hub.crypto_validation import validate_node_id_format
        
        # Valid formats
        assert validate_node_id_format("dpc-node-8b066c7f3d7eb627")
        assert validate_node_id_format("dpc-node-temp-abc123def456")  # Temporary ID
        
        # Invalid formats
        with pytest.raises(ValueError):
            validate_node_id_format("")  # Empty
        
        with pytest.raises(ValueError):
            validate_node_id_format("invalid-format")  # Wrong prefix
        
        with pytest.raises(ValueError):
            validate_node_id_format("dpc-node-short")  # Too short
    
    def test_validate_certificate_invalid(self):
        """Test certificate validation with invalid input"""
        from dpc_hub.crypto_validation import validate_certificate, CryptoValidationError
        
        with pytest.raises(CryptoValidationError):
            validate_certificate("not a certificate")
        
        with pytest.raises(CryptoValidationError):
            validate_certificate("")


# ============================================================================
# TOKEN BLACKLIST TESTS
# ============================================================================

class TestTokenBlacklist:
    """Test token blacklist functionality"""
    
    def test_blacklist_add_and_check(self):
        """Test adding and checking blacklisted tokens"""
        from dpc_hub.token_blacklist import TokenBlacklist
        
        blacklist = TokenBlacklist()
        token = "test_token_123"
        
        # Initially not blacklisted
        assert not blacklist.is_blacklisted(token)
        
        # Add to blacklist
        blacklist.add(token)
        assert blacklist.is_blacklisted(token)
        assert blacklist.size() == 1
    
    def test_blacklist_clear(self):
        """Test clearing the blacklist"""
        from dpc_hub.token_blacklist import TokenBlacklist
        
        blacklist = TokenBlacklist()
        
        # Add multiple tokens
        blacklist.add("token1")
        blacklist.add("token2")
        blacklist.add("token3")
        
        assert blacklist.size() == 3
        
        # Clear all
        blacklist.clear()
        assert blacklist.size() == 0
        assert not blacklist.is_blacklisted("token1")
    
    @pytest.mark.asyncio
    async def test_blacklist_cleanup(self):
        """Test automatic cleanup of old tokens"""
        from dpc_hub.token_blacklist import TokenBlacklist
        
        blacklist = TokenBlacklist(cleanup_interval=1)  # 1 second for testing
        
        # Add a token
        blacklist.add("old_token")
        
        # Manually trigger cleanup (in real code, this runs automatically)
        await blacklist._cleanup_expired()
        
        # Token should still be there (not old enough)
        assert blacklist.is_blacklisted("old_token")


# ============================================================================
# API ENDPOINT TESTS
# ============================================================================

@pytest.mark.asyncio
class TestNodeRegistration:
    """Test node registration endpoint"""
    
    async def test_register_node_id_success(self, authenticated_client):
        """Test successful node registration"""
        response = await authenticated_client.post(
            "/register-node-id",
            json={
                "node_id": VALID_NODE_ID,
                "public_key": VALID_PUBLIC_KEY,
                "certificate": VALID_CERTIFICATE
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data['verified'] == True
        assert data['node_id'] == VALID_NODE_ID
    
    async def test_register_node_id_invalid_format(self, authenticated_client):
        """Test node registration with invalid node_id format"""
        response = await authenticated_client.post(
            "/register-node-id",
            json={
                "node_id": "invalid-format",
                "public_key": VALID_PUBLIC_KEY,
                "certificate": VALID_CERTIFICATE
            }
        )
        
        assert response.status_code == 400
        assert "Invalid" in response.json()['detail']
    
    async def test_register_node_id_duplicate(self, authenticated_client, second_user):
        """Test that duplicate node_id is rejected"""
        # First user registers
        await authenticated_client.post(
            "/register-node-id",
            json={
                "node_id": VALID_NODE_ID,
                "public_key": VALID_PUBLIC_KEY,
                "certificate": VALID_CERTIFICATE
            }
        )
        
        # Second user tries to register same node_id
        response = await second_user.post(
            "/register-node-id",
            json={
                "node_id": VALID_NODE_ID,
                "public_key": VALID_PUBLIC_KEY,
                "certificate": VALID_CERTIFICATE
            }
        )
        
        assert response.status_code == 400
        assert "already registered" in response.json()['detail'].lower()


@pytest.mark.asyncio
class TestProfileEndpoints:
    """Test profile management endpoints"""
    
    async def test_get_own_profile_not_found(self, authenticated_client):
        """Test getting own profile when it doesn't exist"""
        response = await authenticated_client.get("/profile")
        assert response.status_code == 404
    
    async def test_create_and_get_profile(self, authenticated_client):
        """Test creating and retrieving profile"""
        # Create profile
        profile_data = {
            "name": "Test User",
            "description": "Test description",
            "expertise": {"python": 5, "rust": 3}
        }
        
        create_response = await authenticated_client.put(
            "/profile",
            json=profile_data
        )
        assert create_response.status_code == 200
        
        # Get own profile
        get_response = await authenticated_client.get("/profile")
        assert get_response.status_code == 200
        
        data = get_response.json()
        assert data['name'] == "Test User"
        assert data['expertise']['python'] == 5
    
    async def test_delete_profile(self, authenticated_client):
        """Test deleting user account"""
        # Create profile first
        await authenticated_client.put(
            "/profile",
            json={"name": "Test User", "expertise": {}}
        )
        
        # Delete account
        response = await authenticated_client.delete("/profile")
        assert response.status_code == 204
        
        # Verify user is gone (next request should fail authentication)
        get_response = await authenticated_client.get("/profile")
        assert get_response.status_code == 401


@pytest.mark.asyncio
class TestAuthenticationEndpoints:
    """Test authentication and logout"""
    
    async def test_logout(self, authenticated_client, auth_token):
        """Test logout endpoint"""
        response = await authenticated_client.post("/logout")
        
        assert response.status_code == 200
        data = response.json()
        assert "logout" in data['message'].lower()
        
        # Verify token is blacklisted
        get_response = await authenticated_client.get("/profile")
        assert get_response.status_code == 401
    
    async def test_get_current_user(self, authenticated_client):
        """Test /users/me endpoint"""
        response = await authenticated_client.get("/users/me")
        
        assert response.status_code == 200
        data = response.json()
        assert 'email' in data
        assert 'node_id' in data


@pytest.mark.asyncio
class TestHealthCheck:
    """Test health check endpoint"""
    
    async def test_health_check(self, client):
        """Test health check returns status"""
        response = await client.get("/health")
        
        assert response.status_code == 200
        data = response.json()
        
        assert 'status' in data
        assert 'version' in data
        assert 'database' in data
        assert 'websocket_connections' in data
        assert 'uptime_seconds' in data
        
        # Check database is healthy
        assert data['database'] == 'healthy'


@pytest.mark.asyncio
class TestRateLimiting:
    """Test rate limiting"""
    
    async def test_rate_limit_search(self, authenticated_client):
        """Test that rate limiting works on search endpoint"""
        # Make many requests quickly
        responses = []
        for i in range(35):  # More than 30/minute limit
            response = await authenticated_client.get(
                "/discovery/search?q=python&min_level=1"
            )
            responses.append(response)
        
        # At least one should be rate limited
        status_codes = [r.status_code for r in responses]
        assert 429 in status_codes  # Too Many Requests


# ============================================================================
# WEBSOCKET TESTS
# ============================================================================

@pytest.mark.asyncio
class TestWebSocket:
    """Test WebSocket signaling"""
    
    async def test_websocket_authentication(self, ws_client, auth_token):
        """Test WebSocket connection with authentication"""
        async with ws_client.websocket_connect(
            f"/ws/signal?token={auth_token}"
        ) as websocket:
            # Should receive auth_ok message
            message = await websocket.receive_json()
            assert message['type'] == 'auth_ok'
    
    async def test_websocket_invalid_token(self, ws_client):
        """Test WebSocket rejects invalid token"""
        with pytest.raises(Exception):  # Connection should be rejected
            async with ws_client.websocket_connect(
                "/ws/signal?token=invalid_token"
            ) as websocket:
                pass
    
    async def test_websocket_signal_relay(self, ws_client, auth_tokens):
        """Test signal relay between two peers"""
        token1, token2 = auth_tokens
        
        async with ws_client.websocket_connect(
            f"/ws/signal?token={token1}"
        ) as ws1:
            async with ws_client.websocket_connect(
                f"/ws/signal?token={token2}"
            ) as ws2:
                # Get auth confirmations
                await ws1.receive_json()
                msg2 = await ws2.receive_json()
                node2_id = msg2['node_id']
                
                # Send signal from ws1 to ws2
                await ws1.send_json({
                    "type": "signal",
                    "target_node_id": node2_id,
                    "payload": {"sdp": "test_offer"}
                })
                
                # ws2 should receive the signal
                received = await ws2.receive_json()
                assert received['type'] == 'signal'
                assert received['payload']['sdp'] == 'test_offer'
                assert 'sender_node_id' in received
    
    async def test_websocket_error_handling(self, ws_client, auth_token):
        """Test WebSocket error handling"""
        async with ws_client.websocket_connect(
            f"/ws/signal?token={auth_token}"
        ) as websocket:
            await websocket.receive_json()  # auth_ok
            
            # Send signal to non-existent peer
            await websocket.send_json({
                "type": "signal",
                "target_node_id": "dpc-node-nonexistent",
                "payload": {"test": "data"}
            })
            
            # Should receive error
            error = await websocket.receive_json()
            assert error['type'] == 'error'
            assert 'not connected' in error['message'].lower()


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
async def client():
    """Create test client"""
    from dpc_hub.main import app
    from httpx import AsyncClient
    
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def auth_token(client):
    """Create authenticated user and return token"""
    # This would use the actual OAuth flow or create a test token
    # Implementation depends on test setup
    pass


@pytest.fixture
async def authenticated_client(client, auth_token):
    """Create authenticated client with bearer token"""
    client.headers = {"Authorization": f"Bearer {auth_token}"}
    return client


@pytest.fixture
async def ws_client():
    """Create WebSocket test client"""
    from starlette.testclient import TestClient
    from dpc_hub.main import app
    
    with TestClient(app) as client:
        yield client


if __name__ == "__main__":
    pytest.main([__file__, "-v"])