# dpc-client/core/dpc_client_core/llm_manager.py

import os
import toml
import asyncio
from pathlib import Path
from typing import Dict, Any

# Import client libraries
from openai import AsyncOpenAI
from anthropic import AsyncAnthropic
import ollama

# --- Abstract Base Class for all Providers ---

class AIProvider:
    """Abstract base class for all AI providers."""
    def __init__(self, alias: str, config: Dict[str, Any]):
        self.alias = alias
        self.config = config
        self.model = config.get("model")

    async def generate_response(self, prompt: str) -> str:
        """Generates a response from the AI model."""
        raise NotImplementedError

# --- Concrete Provider Implementations ---

class OllamaProvider(AIProvider):
    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        self.client = ollama.AsyncClient(host=config.get("host"))

    async def generate_response(self, prompt: str) -> str:
        try:
            message = {'role': 'user', 'content': prompt}
            # Add a timeout to the request
            response = await asyncio.wait_for(
                self.client.chat(model=self.model, messages=[message]),
                timeout=60.0 # 60 second timeout
            )
            return response['message']['content']
        except asyncio.TimeoutError:
            raise RuntimeError(f"Ollama provider '{self.alias}' timed out after 60 seconds.")
        except Exception as e:
            raise RuntimeError(f"Ollama provider '{self.alias}' failed: {e}") from e

class OpenAICompatibleProvider(AIProvider):
    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        api_key = config.get("api_key")
        if not api_key:
            api_key_env = config.get("api_key_env")
            if api_key_env:
                api_key = os.getenv(api_key_env)
        
        if not api_key:
            raise ValueError(f"API key not found for OpenAI compatible provider '{self.alias}'")

        self.client = AsyncOpenAI(base_url=config.get("base_url"), api_key=api_key)

    async def generate_response(self, prompt: str) -> str:
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"OpenAI compatible provider '{self.alias}' failed: {e}") from e

class AnthropicProvider(AIProvider):
    def __init__(self, alias: str, config: Dict[str, Any]):
        super().__init__(alias, config)
        api_key_env = config.get("api_key_env")
        api_key = os.getenv(api_key_env) if api_key_env else None
        
        if not api_key:
            raise ValueError(f"API key environment variable not set for Anthropic provider '{self.alias}'")

        self.client = AsyncAnthropic(api_key=api_key)

    async def generate_response(self, prompt: str) -> str:
        try:
            message = await self.client.messages.create(
                model=self.model,
                max_tokens=2048, # Anthropic requires max_tokens
                messages=[{"role": "user", "content": prompt}],
            )
            return message.content[0].text
        except Exception as e:
            raise RuntimeError(f"Anthropic provider '{self.alias}' failed: {e}") from e


# --- The Manager Class ---

PROVIDER_MAP = {
    "ollama": OllamaProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
}

class LLMManager:
    """
    Manages all configured AI providers.
    """
    def __init__(self, config_path: Path = Path.home() / ".dpc" / "providers.toml"):
        self.config_path = config_path
        self.providers: Dict[str, AIProvider] = {}
        self.default_provider: str | None = None
        self._load_providers_from_config()

    def _ensure_config_exists(self):
        """Creates a default providers.toml file if one doesn't exist."""
        if not self.config_path.exists():
            print(f"Warning: Provider config file not found at {self.config_path}.")
            print("Creating a default template with a local Ollama provider...")
            
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            
            default_config = """
# Default AI provider configuration for D-PC.
# You can add more providers here (e.g., for LM Studio, OpenAI, Anthropic).

default_provider = "ollama_local"

[[providers]]
  alias = "ollama_local"
  type = "ollama"
  model = "llama3.1:8b"
  host = "http://127.0.0.1:11434"
"""
            self.config_path.write_text(default_config)
            print(f"Default provider config created at {self.config_path}")

    def _load_providers_from_config(self):
        """Reads the config file and initializes all defined providers."""
        self._ensure_config_exists() # Call the new method
        print(f"Loading AI providers from {self.config_path}...")
        if not self.config_path.exists():
            print(f"Warning: Provider config file not found at {self.config_path}. No providers loaded.")
            return

        try:
            config = toml.load(self.config_path)
            self.default_provider = config.get("default_provider")

            for provider_config in config.get("providers", []):
                alias = provider_config.get("alias")
                provider_type = provider_config.get("type")

                if not alias or not provider_type:
                    print(f"Warning: Skipping invalid provider config: {provider_config}")
                    continue

                if provider_type in PROVIDER_MAP:
                    provider_class = PROVIDER_MAP[provider_type]
                    try:
                        self.providers[alias] = provider_class(alias, provider_config)
                        print(f"  - Successfully loaded provider '{alias}' of type '{provider_type}'.")
                    except (ValueError, KeyError) as e:
                        print(f"  - Error loading provider '{alias}': {e}")
                else:
                    print(f"Warning: Unknown provider type '{provider_type}' for alias '{alias}'.")
            
            if self.default_provider and self.default_provider not in self.providers:
                print(f"Warning: Default provider '{self.default_provider}' not found in loaded providers.")
                self.default_provider = None

        except Exception as e:
            print(f"Error parsing provider config file: {e}")

    def get_active_model_name(self) -> str:
        """
        Returns the name of the currently active AI model.
        
        Returns:
            String like "llama3.1:8b" or None if no model is loaded
        """
        # Use default_provider (not active_provider)
        if not self.default_provider:
            return None
        
        # Get the provider object (not a dict, but an AIProvider instance)
        provider = self.providers.get(self.default_provider)
        if not provider:
            return None
        
        # Get the model name from the provider object
        model = provider.model
        if not model:
            return None
        
        # Get provider type from config
        provider_type = provider.config.get('type', '')
        
        # Format based on provider type
        if provider_type == 'ollama':
            return model  # e.g., "llama3.1:8b"
        elif provider_type == 'openai_compatible':
            return f"OpenAI {model}"
        elif provider_type == 'anthropic':
            return f"Claude {model}"
        else:
            return model

    def find_provider_by_model(self, model_name: str) -> str | None:
        """
        Find a provider alias by model name.

        Args:
            model_name: The model name to search for (e.g., "claude-haiku-4-5")

        Returns:
            Provider alias if found, None otherwise
        """
        for alias, provider in self.providers.items():
            if provider.model == model_name:
                return alias
        return None

    async def query(self, prompt: str, provider_alias: str | None = None, return_metadata: bool = False):
        """
        Routes a query to the specified provider, or the default provider if None.

        Args:
            prompt: The prompt to send to the LLM
            provider_alias: Optional provider alias to use (uses default if None)
            return_metadata: If True, returns dict with 'response', 'provider', 'model'. If False, returns just the response string.

        Returns:
            str if return_metadata=False, dict if return_metadata=True
        """
        alias_to_use = provider_alias or self.default_provider
        if not alias_to_use:
            raise ValueError("No provider specified and no default provider is set.")

        if alias_to_use not in self.providers:
            raise ValueError(f"Provider '{alias_to_use}' is not configured or failed to load.")

        provider = self.providers[alias_to_use]
        print(f"Routing query to provider '{alias_to_use}' with model '{provider.model}'...")
        response = await provider.generate_response(prompt)

        if return_metadata:
            return {
                "response": response,
                "provider": alias_to_use,
                "model": provider.model
            }
        return response

# --- Self-testing block ---
async def main_test():
    print("--- Testing LLMManager ---")
    
    # Create a dummy providers.toml for testing
    dummy_config_content = """
default_provider = "local_ollama"

[[providers]]
  alias = "local_ollama"
  type = "ollama"
  model = "llama3.1:8b"
  host = "http://127.0.0.1:11434"
"""
    config_file = Path("test_providers.toml")
    # We need to place it where the manager expects to find it, or pass the path
    # For simplicity, let's assume it's in the user's home .dpc directory
    dpc_dir = Path.home() / ".dpc"
    dpc_dir.mkdir(exist_ok=True)
    test_config_path = dpc_dir / "providers.toml"
    test_config_path.write_text(dummy_config_content)

    try:
        manager = LLMManager(config_path=test_config_path)
        
        if not manager.providers:
            print("\nNo providers were loaded. Cannot run test query.")
            return

        print("\nTesting query with default provider...")
        response = await manager.query("What is the capital of France?")
        print(f"  -> Response: {response}")

        print("\nTesting query with specified provider...")
        response = await manager.query("What is the capital of Germany?", provider_alias="local_ollama")
        print(f"  -> Response: {response}")

    except Exception as e:
        print(f"\nAn error occurred during testing: {e}")
    finally:
        # Clean up the dummy config
        if test_config_path.exists():
            test_config_path.unlink()
        print("\n--- Test finished ---")

if __name__ == '__main__':
    # To run this test:
    # 1. Make sure Ollama is running.
    # 2. Navigate to `dpc-client/core/`
    # 3. Run: `poetry run python dpc_client_core/llm_manager.py`
    asyncio.run(main_test())