"""
The model interface. Two methods, because the system makes exactly two kinds of
model call per question:

    complete_json  -> LLM call #1, extraction, schema-constrained
    stream_text    -> LLM call #2, narration of already-computed rows

Keeping this surface tiny is what makes the bake-off a config flip rather than a
refactor, and it is the reason the model-efficiency story is measurable: if a
provider needs more than these two calls, it is doing something this
architecture does not ask for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable


@dataclass
class LLMResult:
    data: dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    model: str = ""
    raw: str = field(default="", repr=False)


@runtime_checkable
class LLM(Protocol):
    name: str

    async def complete_json(
        self, system: str, user: str, schema: dict[str, Any]
    ) -> LLMResult:
        """Return an object conforming to `schema`. Must not raise on a merely
        wrong answer - only on transport failure or unparseable output."""
        ...

    async def stream_text(self, system: str, user: str) -> AsyncIterator[str]:
        """Yield narration chunks."""
        ...

    async def close(self) -> None: ...


def get_llm() -> LLM:
    """Factory. Imports lazily so an uninstalled provider never breaks startup."""
    from api.config import settings

    if settings.llm_provider == "anthropic":
        from api.llm.anthropic import AnthropicLLM

        return AnthropicLLM()
    from api.llm.ollama import OllamaLLM

    return OllamaLLM()
