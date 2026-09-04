"""
sentence-transformers fallback. Not installed by default.

Kept behind embed_provider="sbert" so the comparison is available for the deck
without forcing a ~2 GB torch download on anyone who does not want it. If you
switch, set embed_dim=384 and rebuild the index - the vector(n) column must match.
"""

from __future__ import annotations

import asyncio
from typing import Sequence

from api.config import settings


class SbertEmbedder:
    def __init__(self, model: str | None = None) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                'sentence-transformers is not installed. Either run '
                '`pip install sentence-transformers` or set EMBED_PROVIDER=ollama '
                '(the default, no extra dependency).'
            ) from exc

        self.model_name = model or settings.embed_model
        self._model = SentenceTransformer(self.model_name)
        self.name = f"sbert/{self.model_name}"
        self.dim = int(self._model.get_sentence_embedding_dimension())

    async def encode(self, texts: Sequence[str]) -> list[list[float]]:
        # Sync library; keep the event loop free.
        return await asyncio.to_thread(
            lambda: self._model.encode(list(texts), normalize_embeddings=True).tolist()
        )

    async def close(self) -> None:
        return None
