"""
Token Count Manager - Token Counting and Context Window Validation

Manages token counting and context window overflow prevention:
- Accurate tokenization for all AI providers (OpenAI, Anthropic, Ollama)
- HuggingFace transformers for Ollama models
- tiktoken for OpenAI/Anthropic models
- Character estimation fallback
- Pre-query validation with response buffer
- Tokenizer caching for performance

Extracted from llm_manager.py and service.py for better separation of concerns (v0.12.1 refactor)
Prevents token double-counting bug and enables accurate context window management.
"""

import logging
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# Check tiktoken availability at module level
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    logger.warning("tiktoken not available - will use character estimation for OpenAI/Anthropic models")


class TokenCountManager:
    """Manages token counting and context window validation.

    Responsibilities:
    - Count tokens using appropriate tokenizer (tiktoken, HuggingFace, char estimation)
    - Cache tokenizers to avoid repeated downloads
    - Validate prompts fit in context window with response buffer
    - Calculate conversation token usage (prevent double-counting)
    - Map Ollama model families to tokenizers

    Design Pattern:
    - Follows existing manager pattern (PromptManager, FileTransferManager, etc.)
    - Single Responsibility: All token counting logic in one place
    - Easier Testing: Isolated unit tests for token calculations
    - Bug Prevention: Centralized logic prevents double-counting errors
    """

    # Map Ollama model families to HuggingFace tokenizers for accurate token counting
    # NOTE: Using publicly accessible models to avoid gated repository access issues
    OLLAMA_TOKENIZER_MAP = {
        # Llama family - use GPT-2 tokenizer (public, similar BPE tokenization)
        "llama": "gpt2",
        "llama2": "gpt2",
        "llama3": "gpt2",
        "llama3.1": "gpt2",
        "llama3.2": "gpt2",
        "codellama": "gpt2",
        # Mistral family - use public Instruct variant
        "mistral": "mistralai/Mistral-7B-Instruct-v0.2",
        "mixtral": "mistralai/Mistral-7B-Instruct-v0.2",
        # Qwen family - more-specific prefixes must precede shorter ones (startswith matching)
        "qwen3": "Qwen/Qwen2.5-7B",    # Qwen3/3.5 uses Qwen2.5 tokenizer; no trust_remote_code
        "qwen2.5": "Qwen/Qwen2.5-7B",
        "qwen2": "Qwen/Qwen2-7B",
        "qwen": "Qwen/Qwen2-7B",        # Legacy Qwen1 → Qwen2 tokenizer (no trust_remote_code)
        # Gemma - use smaller public variant
        "gemma": "google/gemma-2b",
        # Phi - publicly accessible
        "phi": "microsoft/phi-2",
    }

    # Cache tokenizers by HF model name so different Ollama models that share
    # the same underlying tokenizer (e.g. qwen3:4b and qwen3.5:9b both use
    # Qwen/Qwen2.5-7B) load only once.
    _hf_tokenizer_cache: dict = {}

    def __init__(self):
        """Initialize TokenCountManager.

        Sets up tokenizer cache for HuggingFace models.
        """
        self._tokenizer_cache: Dict[str, Any] = {}  # Cache HuggingFace tokenizers by model name
        # Repos that could not be loaded. Only successes were remembered before,
        # so a model with no tokenizer retried from_pretrained() and printed the
        # same warning on every single count — three times in one millisecond in
        # the log of 2026-08-14 01:46:12. With the fetch added, retrying would
        # also mean re-attempting a download per call, which is worse than the
        # noise: one attempt per repo per process is the whole intent.
        self._tokenizer_failures: set = set()

    def count_tokens(self, text: str, model: str) -> int:
        """Count tokens in text for a given model.

        Uses appropriate tokenizer based on model:
        - tiktoken for OpenAI/Anthropic (accurate BPE)
        - HuggingFace transformers for Ollama (accurate model-specific)
        - Character estimation fallback (4 chars ≈ 1 token)

        Args:
            text: The text to count tokens for
            model: The model name (e.g., "gpt-4", "llama3.1:8b")

        Returns:
            Token count
        """
        if not text:
            return 0

        # Try tiktoken for OpenAI/Anthropic models
        if TIKTOKEN_AVAILABLE:
            try:
                if "gpt-4" in model or "gpt-3.5" in model:
                    encoding = tiktoken.encoding_for_model(model.split()[0] if " " in model else model)
                    return len(encoding.encode(text))
                elif "claude" in model:
                    encoding = tiktoken.get_encoding("cl100k_base")
                    return len(encoding.encode(text))
            except Exception as e:
                logger.warning("tiktoken failed for '%s': %s", model, e)

        # Try HuggingFace tokenizer for Ollama models
        if ":" in model:  # Ollama models use "model:tag" format
            tokenizer = self._get_tokenizer_for_ollama(model)
            if tokenizer:
                try:
                    tokens = tokenizer.encode(text, add_special_tokens=False)
                    count = len(tokens)
                    logger.debug("Accurate token count for %s: %d tokens", model, count)
                    return count
                except Exception as e:
                    logger.warning("Tokenizer encode failed for %s: %s", model, e)

        # Fallback: Character estimation
        logger.debug("Using character estimation for %s", model)
        return len(text) // 4

    def validate_prompt(
        self,
        prompt: str,
        model: str,
        context_window: int,
        buffer_percent: float = 0.2
    ) -> Tuple[bool, Optional[str]]:
        """Check if prompt fits in context window with response buffer.

        This is PRE-QUERY validation to prevent context window overflow
        BEFORE sending to LLM. Reserves buffer space for AI response.

        Args:
            prompt: The assembled prompt to validate
            model: Model name for token counting
            context_window: Total context window size (e.g., 16384)
            buffer_percent: % of window to reserve for response (default 0.2 = 20%)

        Returns:
            Tuple of (is_valid, error_message)
            - (True, None) if prompt fits
            - (False, error_message) if prompt too large
        """
        if context_window <= 0:
            return (True, None)  # No limit enforced

        # Count tokens in the prompt
        prompt_tokens = self.count_tokens(prompt, model)

        # Reserve buffer for AI response
        response_buffer_tokens = int(context_window * buffer_percent)
        max_allowed_prompt_tokens = context_window - response_buffer_tokens

        # Check if prompt fits (minus buffer)
        if prompt_tokens > max_allowed_prompt_tokens:
            error_msg = (
                f"Prompt too large: {prompt_tokens:,} tokens "
                f"(limit: {max_allowed_prompt_tokens:,} after reserving {int(buffer_percent * 100)}% for response).\n\n"
                f"Context window: {context_window:,} tokens total.\n\n"
                f"Suggestions:\n"
                f"  1. Disable context checkboxes to reduce token usage\n"
                f"  2. End session to save knowledge and clear history\n"
                f"  3. Use a model with larger context window"
            )
            return (False, error_msg)

        logger.debug(
            "Pre-query validation passed: %d prompt tokens + %d buffer < %d limit",
            prompt_tokens, response_buffer_tokens, context_window
        )
        return (True, None)

    def get_conversation_usage(
        self,
        prompt_tokens: int,
        response_tokens: int,
        message_count: int
    ) -> Dict[str, int]:
        """Calculate conversation token usage (prevents double-counting).

        IMPORTANT: prompt_tokens already includes full conversation history,
        so we don't add response_tokens to it (that would double-count).

        Args:
            prompt_tokens: Tokens in the assembled prompt (includes history)
            response_tokens: Tokens in the AI response
            message_count: Number of messages in conversation

        Returns:
            Dict with usage metrics:
            - current_prompt_size: Size of current prompt (includes all history)
            - latest_response_tokens: Size of latest AI response
            - message_count: Number of messages in conversation
        """
        return {
            "current_prompt_size": prompt_tokens,
            "latest_response_tokens": response_tokens,
            "message_count": message_count
        }

    async def warm_tokenizers(self, models) -> None:
        """Load the tokenizers these models need, off the event loop, once.

        Everything that counts tokens does so while answering somebody, and
        that is on the loop — where fetching is refused, correctly. Left at
        that, a tokenizer that is missing stays missing for the life of the
        process however long the network is up. Startup is the one moment
        with nobody waiting, so the fetch belongs here: a worker thread, one
        pass over the models this install actually has, and silence when they
        are already cached.
        """
        import asyncio

        # First match wins, exactly as the lookup does at count time. A set
        # comprehension over every matching prefix looks equivalent and is not:
        # "qwen3-vl" starts with both "qwen3" and "qwen", so it would fetch the
        # Qwen2 tokenizer as well — a download for a repo no count will ever ask
        # for. The test caught it; the two rules have to be the same rule.
        repos = set()
        for model in models:
            family = str(model or "").split(":")[0].lower()
            for prefix, repo in self.OLLAMA_TOKENIZER_MAP.items():
                if family.startswith(prefix):
                    repos.add(repo)
                    break
        if not repos:
            return

        def _load():
            for repo in sorted(repos):
                if repo in self._hf_tokenizer_cache or repo in self._tokenizer_failures:
                    continue
                try:
                    from transformers import AutoTokenizer

                    self._hf_tokenizer_cache[repo] = AutoTokenizer.from_pretrained(
                        repo, local_files_only=True
                    )
                except Exception:
                    tokenizer = self._fetch_tokenizer(repo, "startup")
                    if tokenizer is None:
                        self._tokenizer_failures.add(repo)
                    else:
                        self._hf_tokenizer_cache[repo] = tokenizer

        await asyncio.get_running_loop().run_in_executor(None, _load)

    def _fetch_tokenizer(self, hf_model: str, model: str):
        """Download a tokenizer, but only where the wait harms nobody.

        A tokenizer is metadata — a few megabytes, not the weights — so fetching
        one that is missing beats counting characters forever. Two conditions,
        both about who pays for the wait. There must be no event loop under us:
        this is called from the loop as well as from worker threads, and a
        synchronous download on the loop stops Telegram, the WebSocket and P2P
        for its duration. And the process must not be in offline mode, which is
        a decision somebody made deliberately — `[hf] offline_mode`, or the
        startup check finding every declared model already cached — and not one
        to quietly overrule.
        """
        import asyncio
        import os

        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        offline = os.environ.get("HF_HUB_OFFLINE") == "1"

        if on_loop or offline:
            logger.warning(
                "Tokenizer %s is not cached, so token counts for %s are estimated at "
                "four characters each — about a fifth low on Russian. Not fetching it "
                "here: %s.",
                hf_model, model,
                "this call is on the event loop" if on_loop
                else "the process is in offline mode",
            )
            return None

        try:
            from transformers import AutoTokenizer

            logger.info("Fetching tokenizer %s for %s (a few MB, once)", hf_model, model)
            tokenizer = AutoTokenizer.from_pretrained(hf_model)
            logger.info(
                "Tokenizer %s cached — token counts for %s are exact from here on",
                hf_model, model,
            )
            return tokenizer
        except Exception as exc:
            logger.warning(
                "Tokenizer %s could not be fetched (%s) — counting characters for %s",
                hf_model, exc, model,
            )
            return None

    def _get_tokenizer_for_ollama(self, model: str) -> Optional[Any]:
        """Get HuggingFace tokenizer for Ollama model.

        Caches tokenizers to avoid repeated downloads. Maps model families
        (llama, mistral, etc.) to public HuggingFace tokenizers.

        Args:
            model: Ollama model name (e.g., "llama3.1:8b")

        Returns:
            Tokenizer object or None if unavailable
        """
        # Check per-model cache first
        if model in self._tokenizer_cache:
            return self._tokenizer_cache[model]

        try:
            from transformers import AutoTokenizer

            model_family = model.split(":")[0].lower()

            for family, hf_model in self.OLLAMA_TOKENIZER_MAP.items():
                if model_family.startswith(family):
                    # Reuse already-loaded tokenizer for the same HF model
                    if hf_model in self._tokenizer_failures:
                        return None
                    if hf_model in self._hf_tokenizer_cache:
                        tokenizer = self._hf_tokenizer_cache[hf_model]
                        self._tokenizer_cache[model] = tokenizer
                        logger.info("Reusing cached tokenizer for %s: %s", model, hf_model)
                        return tokenizer

                    # The cache first, always: from_pretrained() is synchronous
                    # and a download on the event loop would stop Telegram, the
                    # WebSocket and P2P until the HTTP request finishes.
                    try:
                        logger.info("Loading tokenizer for %s: %s (local cache)", model, hf_model)
                        tokenizer = AutoTokenizer.from_pretrained(hf_model, local_files_only=True)
                    except Exception:
                        tokenizer = self._fetch_tokenizer(hf_model, model)
                        if tokenizer is None:
                            self._tokenizer_failures.add(hf_model)
                            return None

                    self._hf_tokenizer_cache[hf_model] = tokenizer
                    self._tokenizer_cache[model] = tokenizer
                    return tokenizer

            logger.warning("No tokenizer mapping for model family: %s", model_family)
            return None

        except ImportError:
            logger.warning("transformers library not available - using character estimation")
            return None
        except Exception as e:
            logger.warning("Failed to load tokenizer for %s: %s", model, e)
            return None
