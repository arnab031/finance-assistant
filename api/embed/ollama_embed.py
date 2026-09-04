"""
Embeddings via Ollama's /api/embed.

Chosen over sentence-transformers because Ollama is already running for the
language model, so this adds no dependency at all - no torch, no second ML
stack. Measured on the real vocabulary:

    cos("medical supplies", "Hospital: Clinic/Lab Supplies") = 0.841
    cos("medical supplies", "Debt Service")                  = 0.405

pg_trgm scores ~0.0 on that first pair - no shared trigrams - which is the
entire reason this layer exists.
"""

from __future__ import annotations

import logging
from typing import Sequence

import httpx

from api.config import settings

log = logging.getLogger("tbx.embed.ollama")

BATCH = 64


class OllamaEmbedder:
    def __init__(self, model: str | None = None) -> None:
        self.model = model or settings.embed_model
        self.name = f"ollama/{self.model}"
        self.dim = settings.embed_dim
        self._client = httpx.AsyncClient(base_url=settings.ollama_url, timeout=180)

    async def encode(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), BATCH):
            chunk = list(texts[start : start + BATCH])
            resp = await self._client.post(
                "/api/embed",
                json={
                    "model": self.model,
                    "input": chunk,
                    "keep_alive": settings.ollama_keep_alive,
                },
            )
            resp.raise_for_status()
            vectors = resp.json()["embeddings"]
            if vectors and len(vectors[0]) != self.dim:
                raise RuntimeError(
                    f"{self.model} returned {len(vectors[0])}-dim vectors but "
                    f"embed_dim is {self.dim}. Update config AND the vector(n) "
                    f"column in api/sql/002_semantic_index.sql - they must agree."
                )
            out.extend(vectors)
        return out

    async def close(self) -> None:
        await self._client.aclose()
