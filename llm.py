"""
LLM Client for connecting to model providers.
Supports OpenAI-compatible APIs (OpenRouter, NVIDIA, Ollama, etc.)
"""

import os
import asyncio
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
import httpx
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    """Configuration for the LLM client."""
    model: str = "nvidia/nemotron-3-ultra-550b-a55b"
    base_url: str = "https://integrate.api.nvidia.com/v1"
    api_key: str = ""
    max_tokens: int = 4096
    temperature: float = 0.7
    timeout: int = 60
    max_retries: int = 2
    max_response_length: int = 8192
    provider: str = "nvidia"  # "nvidia", "openrouter", "ollama", "custom"


class LLMClient:
    """Client for interacting with LLM providers via OpenAI-compatible API."""

    def __init__(self, config: LLMConfig = None):
        self.config = config or LLMConfig()
        
        # Get API key from environment if not provided
        if not self.config.api_key:
            if self.config.provider == "nvidia":
                self.config.api_key = os.environ.get("NVIDIA_API_KEY") or ""
            elif self.config.provider == "openrouter":
                self.config.api_key = os.environ.get("OPENROUTER_API_KEY") or ""
            elif self.config.provider == "ollama":
                self.config.api_key = "ollama"  # Ollama doesn't need real API key
            else:
                self.config.api_key = os.environ.get("NVIDIA_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""
        
        if not self.config.api_key and self.config.provider != "ollama":
            logger.warning("No API key found. Set NVIDIA_API_KEY or OPENROUTER_API_KEY environment variable.")
        
        # Create HTTP client
        http_client = httpx.AsyncClient(
            verify=False,  # Disable SSL verification for self-signed certs
            timeout=self.config.timeout,
        )
        
        # For Ollama, use localhost
        if self.config.provider == "ollama" and self.config.base_url == "https://integrate.api.nvidia.com/v1":
            self.config.base_url = "http://localhost:11434/v1"
            self.config.api_key = "ollama"
        
        self.client = AsyncOpenAI(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            timeout=self.config.timeout,
            http_client=httpx.AsyncClient(
                verify=False,
                timeout=self.config.timeout,
            ),
        )
        
        logger.info(f"LLM Client initialized: {self.config.model} @ {self.config.base_url} (provider: {self.config.provider})")

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(2),
        retry=retry_if_exception_type((asyncio.TimeoutError, ConnectionError)),
        reraise=True
    )
    async def chat(
        self,
        messages: List[Dict[str, str]],
        system_prompt: str = None,
        max_tokens: int = None,
        temperature: float = None,
    ) -> str:
        """
        Send messages to the LLM and get a response.
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            system_prompt: Optional system prompt to prepend
            max_tokens: Override max tokens for this request
            temperature: Override temperature for this request
            
        Returns:
            The generated response text
        """
        # Prepare messages
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        try:
            response = await self.client.chat.completions.create(
                model=self.config.model,
                messages=full_messages,
                max_tokens=max_tokens or self.config.max_tokens,
                temperature=temperature if temperature is not None else self.config.temperature,
            )
            
            content = response.choices[0].message.content
            
            # Handle None content
            if content is None:
                content = ""
            
            # Enforce maximum response length
            if len(content) > self.config.max_response_length:
                content = content[:self.config.max_response_length]
                logger.warning(f"Response truncated to {self.config.max_response_length} characters")
            
            logger.info(f"LLM response received ({len(content)} chars)")
            return content
            
        except asyncio.TimeoutError:
            logger.error("LLM request timed out")
            raise
        except Exception as e:
            logger.error(f"LLM request failed: {e}")
            raise

    async def chat_simple(self, prompt: str, system_prompt: str = None) -> str:
        """Simple chat with a single user prompt."""
        return await self.chat(
            messages=[{"role": "user", "content": prompt}],
            system_prompt=system_prompt
        )

    async def close(self):
        """Close the client connections."""
        await self.client.close()


# Global singleton instance
_llm_client: Optional[LLMClient] = None


def get_llm_client(config: LLMConfig = None) -> LLMClient:
    """Get or create the global LLM client instance."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient(config)
    return _llm_client


async def close_llm_client():
    """Close the global LLM client."""
    global _llm_client
    if _llm_client:
        await _llm_client.close()
        _llm_client = None


def create_ollama_client(model: str = "llama3.1:70b", base_url: str = "http://localhost:11434/v1") -> LLMClient:
    """Create an LLM client configured for Ollama."""
    config = LLMConfig(
        model=model,
        base_url=base_url,
        provider="ollama",
        timeout=120,
        max_tokens=4096,
        temperature=0.7,
    )
    return LLMClient(config)


def create_nvidia_client(model: str = "nvidia/nemotron-3-ultra-550b-a55b", api_key: str = None) -> LLMClient:
    """Create an LLM client configured for NVIDIA API."""
    config = LLMConfig(
        model=model,
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=api_key or os.environ.get("NVIDIA_API_KEY"),
        provider="nvidia",
        timeout=60,
        max_tokens=4096,
        temperature=0.7,
    )
    return LLMClient(config)


def create_openrouter_client(model: str = "anthropic/claude-3.5-sonnet", api_key: str = None) -> LLMClient:
    """Create an LLM client configured for OpenRouter."""
    config = LLMConfig(
        model=model,
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key or os.environ.get("OPENROUTER_API_KEY"),
        provider="openrouter",
        timeout=60,
        max_tokens=4096,
        temperature=0.7,
    )
    return LLMClient(config)


async def main():
    """Test the LLM client."""
    print("=" * 50)
    print("LLM Client Test")
    print("=" * 50)

    # Check for API key
    api_key = os.environ.get("NVIDIA_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: No API key found!")
        print("Set NVIDIA_API_KEY or OPENROUTER_API_KEY environment variable.")
        return

    config = LLMConfig(
        model="nvidia/nemotron-3-ultra-550b-a55b",
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=api_key,
        provider="nvidia",
    )

    client = LLMClient(config)

    try:
        # Test simple message
        print("\n[Test] Sending test message...")
        response = await client.chat_simple(
            prompt="Say 'Hello, World!' and nothing else.",
            system_prompt="You are a helpful assistant. Keep responses brief."
        )
        print(f"[Test] Response: {response}")

        # Test conversation
        print("\n[Test] Testing conversation...")
        response = await client.chat(
            messages=[
                {"role": "user", "content": "What is 2+2?"},
                {"role": "assistant", "content": "4"},
                {"role": "user", "content": "What is 4+4?"},
            ],
            system_prompt="You are a helpful assistant. Answer briefly."
        )
        print(f"[Test] Response: {response}")

        print("\n[Test] All tests passed!")

    except Exception as e:
        print(f"[Test] Error: {e}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())