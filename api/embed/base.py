"""
Embedding interface.

One method, because the system needs exactly one thing from an embedder: turn
text into vectors. Keeping it this small is why the provider is a config flip.

Vectors are used ONLY for entity resolution - mapping a user's words onto the
closed vocabularies already in the database. They never produce a number. The
moment an embedding could influence an amount, grounding is gone.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    name: str
    dim: int

    async def encode(self, texts: Sequence[str]) -> list[list[float]]: ...
    async def close(self) -> None: ...


_cached: Embedder | None = None


def get_embedder() -> Embedder:
    """Lazily built and cached - sbert in particular is expensive to construct."""
    global _cached
    if _cached is not None:
        return _cached

    from api.config import settings

    if settings.embed_provider == "gemini":
        from api.embed.gemini_embed import GeminiEmbedder

        _cached = GeminiEmbedder()
        return _cached
    if settings.embed_provider == "sbert":
        from api.embed.sbert import SbertEmbedder

        _cached = SbertEmbedder()
    else:
        from api.embed.ollama_embed import OllamaEmbedder

        _cached = OllamaEmbedder()
    return _cached
