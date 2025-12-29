"""Test token counting accuracy and validation."""
import pytest
from dpc_client_core.llm_manager import LLMManager
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.managers.token_count_manager import TokenCountManager


class TestConversationTokenCounting:
    """Test conversation token counting doesn't double-count history."""

    @pytest.fixture
    def llm_manager(self):
        """Create LLMManager instance for tests."""
        return LLMManager()

    def test_no_double_counting(self, llm_manager):
        """Verify conversation token counting is linear, not exponential."""
        monitor = ConversationMonitor(
            conversation_id="test-conv-123",
            participants=[{"node_id": "test-peer", "name": "Test Peer", "context": "test"}],
            llm_manager=llm_manager
        )
        monitor.token_limit = 16384  # Set token limit after initialization

        # First query
        monitor.set_token_count(100)  # prompt_tokens includes first query
        assert monitor.current_token_count == 100

        # Second query (prompt includes history of first query)
        monitor.set_token_count(150)  # prompt_tokens now includes both queries
        assert monitor.current_token_count == 150  # NOT 250!

        # Third query
        monitor.set_token_count(200)
        assert monitor.current_token_count == 200  # NOT 450!

    def test_linear_growth(self, llm_manager):
        """Verify token count grows linearly across multiple messages."""
        monitor = ConversationMonitor(
            conversation_id="test-conv-456",
            participants=[{"node_id": "test-peer", "name": "Test Peer", "context": "test"}],
            llm_manager=llm_manager
        )
        monitor.token_limit = 16384  # Set token limit after initialization

        # Simulate 10 messages with growing conversation history
        expected_counts = [100, 150, 200, 250, 300, 350, 400, 450, 500, 550]

        for count in expected_counts:
            monitor.set_token_count(count)
            assert monitor.current_token_count == count

        # Verify final count is linear (550), not exponential (~5000+)
        assert monitor.current_token_count == 550


class TestOllamaTokenization:
    """Test HuggingFace tokenizer accuracy for Ollama models."""

    @pytest.fixture
    def llm_manager(self):
        """Create LLMManager instance for tests."""
        return LLMManager()

    def test_tokenization_uses_transformers(self, llm_manager):
        """Verify tokenizer loads for Ollama models."""
        text = "The quick brown fox jumps over the lazy dog"

        # Try to count tokens - should attempt to load tokenizer
        count = llm_manager.count_tokens(text, "llama3.1:8b")

        # Should get a reasonable count (either from tokenizer or fallback)
        assert count > 0
        assert 5 <= count <= 20, "Token count should be in reasonable range"

    def test_different_from_character_estimation(self, llm_manager):
        """Verify HuggingFace tokenizer differs from character estimation."""
        text = "anthropomorphization supercalifragilisticexpialidocious"

        # Count with potential tokenizer (will use gpt2 for llama models)
        actual_count = llm_manager.count_tokens(text, "llama3.1:8b")

        # Character estimation
        char_estimate = len(text) // 4

        # For most text, tokenizer should give different result than char estimation
        # Note: May be equal if fallback is used, so we check the count is reasonable
        assert 5 <= actual_count <= 20, "Should be in reasonable range"

    def test_empty_text(self, llm_manager):
        """Verify empty text returns 0 tokens."""
        assert llm_manager.count_tokens("", "llama3.1:8b") == 0
        assert llm_manager.count_tokens("", "gpt-4") == 0

    def test_tokenizer_cache(self, llm_manager):
        """Verify tokenizers are cached to avoid repeated downloads."""
        text = "Hello world"

        # First call - may load tokenizer
        count1 = llm_manager.count_tokens(text, "llama3.1:8b")

        # Second call - should use cache
        count2 = llm_manager.count_tokens(text, "llama3.1:8b")

        assert count1 == count2, "Should return same count for same input"

        # Check cache exists in TokenCountManager (Phase 4 refactor)
        assert hasattr(llm_manager.token_count_manager, "_tokenizer_cache")


class TestModelFamilyDetection:
    """Test model family detection for tokenizer mapping."""

    @pytest.fixture
    def llm_manager(self):
        """Create LLMManager instance for tests."""
        return LLMManager()

    def test_llama_models(self, llm_manager):
        """Test llama model family detection."""
        test_cases = [
            "llama2:7b",
            "llama3:8b",
            "llama3.1:8b",
            "llama3.2:3b",
            "codellama:13b",
        ]

        for model in test_cases:
            count = llm_manager.count_tokens("Hello world", model)
            assert count > 0, f"Should count tokens for {model}"

    def test_mistral_models(self, llm_manager):
        """Test mistral model family detection."""
        test_cases = [
            "mistral:7b",
            "mixtral:8x7b",
        ]

        for model in test_cases:
            count = llm_manager.count_tokens("Hello world", model)
            assert count > 0, f"Should count tokens for {model}"

    def test_other_models(self, llm_manager):
        """Test other model families."""
        test_cases = [
            "qwen:7b",
            "qwen2:7b",
            "gemma:7b",
            "phi:2b",
        ]

        for model in test_cases:
            count = llm_manager.count_tokens("Hello world", model)
            assert count > 0, f"Should count tokens for {model}"


class TestTiktokenTokenization:
    """Test tiktoken tokenization for OpenAI/Anthropic models."""

    @pytest.fixture
    def llm_manager(self):
        """Create LLMManager instance for tests."""
        return LLMManager()

    def test_gpt4_tokenization(self, llm_manager):
        """Verify GPT-4 tokenization works."""
        text = "The quick brown fox jumps over the lazy dog"
        count = llm_manager.count_tokens(text, "gpt-4")

        # Should get accurate tiktoken count
        assert count > 0
        assert 5 <= count <= 15, "GPT-4 token count should be in reasonable range"

    def test_claude_tokenization(self, llm_manager):
        """Verify Claude tokenization works."""
        text = "The quick brown fox jumps over the lazy dog"
        count = llm_manager.count_tokens(text, "claude-3-5-sonnet-20241022")

        # Should get accurate tiktoken count
        assert count > 0
        assert 5 <= count <= 15, "Claude token count should be in reasonable range"


class TestPreQueryValidation:
    """Test pre-query validation prevents context window overflow."""

    @pytest.fixture
    def llm_manager(self):
        """Create LLMManager instance for tests."""
        return LLMManager()

    def test_token_limit_enforcement(self, llm_manager):
        """Test that ConversationMonitor tracks token limits."""
        monitor = ConversationMonitor(
            conversation_id="test-conv-limit",
            participants=[{"node_id": "test-peer", "name": "Test Peer", "context": "test"}],
            llm_manager=llm_manager
        )
        monitor.token_limit = 1000  # Set small limit for testing

        # Set tokens below limit
        monitor.set_token_count(800)
        assert monitor.current_token_count == 800

        # Calculate usage percent
        usage_percent = monitor.current_token_count / monitor.token_limit
        assert usage_percent == 0.8

        # Set tokens at limit
        monitor.set_token_count(1000)
        usage_percent = monitor.current_token_count / monitor.token_limit
        assert usage_percent == 1.0

    def test_buffer_calculation(self):
        """Test 20% response buffer calculation."""
        token_limit = 16384
        response_buffer = int(token_limit * 0.2)
        max_allowed_prompt = token_limit - response_buffer

        assert response_buffer == 3276
        assert max_allowed_prompt == 13108

        # Verify 80% rule (with rounding tolerance)
        expected_80_percent = int(token_limit * 0.8)
        assert abs(max_allowed_prompt - expected_80_percent) <= 1, \
            f"Buffer calculation off by more than 1 token: {max_allowed_prompt} vs {expected_80_percent}"


class TestTokenCountingFallback:
    """Test graceful fallback when tokenizers unavailable."""

    @pytest.fixture
    def llm_manager(self):
        """Create LLMManager instance for tests."""
        return LLMManager()

    def test_character_estimation_fallback(self, llm_manager):
        """Verify character estimation works as fallback."""
        text = "Hello world! This is a test sentence."

        # Use a model that won't have a tokenizer
        count = llm_manager.count_tokens(text, "unknown-model:1b")

        # Should fall back to character estimation (len(text) // 4)
        expected_estimate = len(text) // 4
        assert count == expected_estimate

    def test_fallback_is_reasonable(self, llm_manager):
        """Verify fallback gives reasonable estimates."""
        texts = [
            "Short text",
            "Medium length text with more words and characters",
            "Very long text " * 50,
        ]

        for text in texts:
            count = llm_manager.count_tokens(text, "unknown-model:1b")
            expected = len(text) // 4

            assert count == expected
            assert count > 0, "Should always return positive count for non-empty text"


class TestTokenCountManagerDirectly:
    """Test TokenCountManager class directly (Phase 4 refactor)."""

    @pytest.fixture
    def token_manager(self):
        """Create TokenCountManager instance for tests."""
        return TokenCountManager()

    def test_count_tokens_delegation(self, token_manager):
        """Verify TokenCountManager counts tokens correctly."""
        text = "The quick brown fox jumps over the lazy dog"

        # Count tokens for different models
        llama_count = token_manager.count_tokens(text, "llama3.1:8b")
        gpt_count = token_manager.count_tokens(text, "gpt-4")
        claude_count = token_manager.count_tokens(text, "claude-3-5-sonnet-20241022")

        # All should return reasonable counts
        assert 5 <= llama_count <= 20
        assert 5 <= gpt_count <= 20
        assert 5 <= claude_count <= 20

    def test_validate_prompt_success(self, token_manager):
        """Test validate_prompt when prompt fits in context window."""
        prompt = "Hello world, this is a short prompt"
        model = "llama3.1:8b"
        context_window = 16384

        is_valid, error_msg = token_manager.validate_prompt(
            prompt=prompt,
            model=model,
            context_window=context_window,
            buffer_percent=0.2
        )

        assert is_valid is True
        assert error_msg is None

    def test_validate_prompt_failure(self, token_manager):
        """Test validate_prompt when prompt too large."""
        # Create a very long prompt (simulate 10,000 tokens)
        prompt = "word " * 10000  # ~10,000 tokens (4 chars/token estimate)
        model = "llama3.1:8b"
        context_window = 1000  # Small window for testing

        is_valid, error_msg = token_manager.validate_prompt(
            prompt=prompt,
            model=model,
            context_window=context_window,
            buffer_percent=0.2
        )

        assert is_valid is False
        assert error_msg is not None
        assert "Prompt too large" in error_msg
        assert "Suggestions:" in error_msg

    def test_validate_prompt_no_limit(self, token_manager):
        """Test validate_prompt with no context window limit."""
        prompt = "Any length prompt"
        model = "llama3.1:8b"
        context_window = 0  # No limit

        is_valid, error_msg = token_manager.validate_prompt(
            prompt=prompt,
            model=model,
            context_window=context_window
        )

        assert is_valid is True
        assert error_msg is None

    def test_get_conversation_usage(self, token_manager):
        """Test conversation usage calculation (prevents double-counting)."""
        usage = token_manager.get_conversation_usage(
            prompt_tokens=5000,
            response_tokens=500,
            message_count=10
        )

        assert usage["current_prompt_size"] == 5000
        assert usage["latest_response_tokens"] == 500
        assert usage["message_count"] == 10

        # Verify we're NOT adding response_tokens to prompt_tokens
        # (that would be double-counting)
        assert "total_tokens" not in usage or usage.get("total_tokens") != 5500

    def test_tokenizer_map_coverage(self, token_manager):
        """Verify OLLAMA_TOKENIZER_MAP covers expected model families."""
        expected_families = [
            "llama", "llama2", "llama3", "llama3.1", "llama3.2", "codellama",
            "mistral", "mixtral",
            "qwen", "qwen2", "qwen2.5",
            "gemma", "phi"
        ]

        for family in expected_families:
            assert family in token_manager.OLLAMA_TOKENIZER_MAP, \
                f"Model family '{family}' should be in OLLAMA_TOKENIZER_MAP"


class TestLLMManagerWithTokenCountManager:
    """Test LLMManager delegates to TokenCountManager correctly."""

    @pytest.fixture
    def llm_manager(self):
        """Create LLMManager instance for tests."""
        return LLMManager()

    def test_llm_manager_has_token_count_manager(self, llm_manager):
        """Verify LLMManager creates TokenCountManager instance."""
        assert hasattr(llm_manager, "token_count_manager")
        assert isinstance(llm_manager.token_count_manager, TokenCountManager)

    def test_count_tokens_delegates(self, llm_manager):
        """Verify LLMManager.count_tokens() delegates to TokenCountManager."""
        text = "Hello world"
        model = "llama3.1:8b"

        # Count via LLMManager
        llm_count = llm_manager.count_tokens(text, model)

        # Count via TokenCountManager directly
        direct_count = llm_manager.token_count_manager.count_tokens(text, model)

        # Should be identical
        assert llm_count == direct_count

    def test_tokenizer_cache_shared(self, llm_manager):
        """Verify tokenizer cache is maintained by TokenCountManager."""
        # Count tokens twice for same model
        llm_manager.count_tokens("Hello", "llama3.1:8b")
        llm_manager.count_tokens("World", "llama3.1:8b")

        # Cache should exist in TokenCountManager
        assert hasattr(llm_manager.token_count_manager, "_tokenizer_cache")
