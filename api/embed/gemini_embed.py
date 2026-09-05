"""
Embeddings via the Gemini API's embedContent endpoint.

Exists because the Ollama embedder cannot run when Ollama is not installed, and
the semantic index then fails at boot with a bare ConnectError while the app
reports itself healthy. With the language model already on Gemini, keeping
embeddings on a second, absent runtime bought nothing.

DIMENSION IS REQUESTED, NOT ACCEPTED. gemini-embedding-001 returns 3072 floats
by default; `outputDimensionality` truncates server-side. The value is sent
explicitly on every call so the vectors always match settings.embed_dim - a
mismatch here is not a crash, it is a silently wrong cosine against vectors
built by a previous model, which api/semantic.py can only detect via its stored
fingerprint after the fact.
"""

from __future__ import annotations

import logging
from typing import Sequence

import httpx

from api.config import settings

log = logging.getLogger("tbx.embed.gemini")

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# batchEmbedContents caps the number of requests per call; 100 is comfortably
# inside it and keeps a single failure from costing a large batch.
BATCH = 100


class GeminiEmbedder:
    def __init__(self, model: str | None = None) -> None:
        if not settings.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is empty; cannot embed. Set it in .env or set "
                "ENABLE_SEMANTIC=false."
            )
        self.model = model or settings.embed_model
        self.name = f"gemini/{self.model}"
        self.dim = settings.embed_dim
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=180)

    async def encode(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), BATCH):
            chunk = list(texts[start : start + BATCH])
            resp = await self._client.post(
                f"/models/{self.model}:batchEmbedContents",
                headers={"x-goog-api-key": settings.gemini_api_key},
                json={
                    "requests": [
                        {
                            "model": f"models/{self.model}",
                            "content": {"parts": [{"text": t}]},
                            "outputDimensionality": self.dim,
                        }
                        for t in chunk
                    ]
                },
            )
            resp.raise_for_status()
            vectors = [e["values"] for e in resp.json().get("embeddings", [])]
            if vectors and len(vectors[0]) != self.dim:
                raise RuntimeError(
                    f"{self.model} returned {len(vectors[0])}-dim vectors but "
                    f"embed_dim is {self.dim}."
                )
            out.extend(vectors)
        return out

    async def close(self) -> None:
        await self._client.aclose()
