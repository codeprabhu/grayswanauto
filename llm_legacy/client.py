from typing import Protocol

class LLMClient(Protocol):
    async def complete(self, messages: list[dict]) -> tuple[str, dict]: ...
