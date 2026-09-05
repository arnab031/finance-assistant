"""
Ollama client.

The load-bearing detail is `format`: Ollama accepts a JSON Schema there and
constrains generation to it via GBNF. That is what makes a 7B model dependable
at extraction - measured 5/5 schema-valid on the first probe. Prompt-only
"return JSON" is not good enough at this size.

`keep_alive` matters too: without it the first call after an idle period pays a
~9s model reload, which was the entire gap between the 9.2s and 1.8s timings in
the baseline probe.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from api.config import settings
from api.llm.base import LLMResult

log = logging.getLogger("tbx.llm.ollama")


class ModelUnavailable(RuntimeError):
    """Ollama returned 404 - the model was never pulled.

    Worth its own type: the generic handler reported "something went wrong
    running that query", which points at the database when the database is fine.
    A 50-question canary against a missing model failed every question in ~42ms
    and recorded a 0% row, which reads as a model that performs terribly rather
    than one that was never called.
    """


async def list_models() -> list[str]:
    """Chat models the daemon has actually pulled.

    Read from Ollama rather than hardcoded so the picker can only offer a model
    that will run. A name that was never pulled is the failure ModelUnavailable
    below describes: every question fails in milliseconds and the run records a
    0% row, which reads as a model that performs terribly rather than one that
    is absent. Filtering the list at the source is cheaper than explaining that
    row afterwards.
    """
    async with httpx.AsyncClient(base_url=settings.ollama_url, timeout=10) as c:
        resp = await c.get("/api/tags")
        resp.raise_for_status()
        names = [m["name"] for m in resp.json().get("models", []) if m.get("name")]

    # The embedding model shares the daemon but cannot answer a chat prompt, so
    # offering it would guarantee a 0% run. Compared on the base name because
    # /api/tags reports "nomic-embed-text:latest" where the setting says
    # "nomic-embed-text".
    embed = settings.embed_model.split(":")[0]
    return sorted(n for n in names if n.split(":")[0] != embed)


class OllamaLLM:
    def __init__(self, model: str | None = None) -> None:
        self.model = model or settings.ollama_model
        self.name = f"ollama/{self.model}"
        self._client = httpx.AsyncClient(
            base_url=settings.ollama_url, timeout=settings.ollama_timeout_s
        )

    async def complete_json(
        self, system: str, user: str, schema: dict[str, Any]
    ) -> LLMResult:
        t0 = time.perf_counter()
        resp = await self._client.post(
            "/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "format": schema,          # JSON Schema, not the string "json"
                "stream": False,
                "keep_alive": settings.ollama_keep_alive,
                "options": {"temperature": 0, "num_ctx": 8192},
            },
        )
        if resp.status_code == 404:
            raise ModelUnavailable(
                f"Model {self.model!r} is not installed. Run: ollama pull {self.model}"
            )
        resp.raise_for_status()
        body = resp.json()
        content = body["message"]["content"]
        elapsed = int((time.perf_counter() - t0) * 1000)

        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            log.warning("ollama returned unparseable JSON: %s", content[:400])
            raise ValueError(f"model did not return JSON: {exc}") from exc

        return LLMResult(
            data=data,
            input_tokens=body.get("prompt_eval_count", 0),
            output_tokens=body.get("eval_count", 0),
            latency_ms=elapsed,
            model=self.name,
            raw=content,
        )

    async def stream_text(self, system: str, user: str) -> AsyncIterator[str]:
        async with self._client.stream(
            "POST",
            "/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": True,
                "keep_alive": settings.ollama_keep_alive,
                "options": {"temperature": 0.1, "num_ctx": 8192,
                            "num_predict": settings.narration_max_tokens},
            },
        ) as resp:
            if resp.status_code == 404:
                raise ModelUnavailable(
                    f"Model {self.model!r} is not installed. "
                    f"Run: ollama pull {self.model}"
                )
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                piece = chunk.get("message", {}).get("content", "")
                if piece:
                    yield piece
                if chunk.get("done"):
                    break

    async def warm(self) -> None:
        """Load the model into memory so the first real question isn't the one
        that pays the cold start."""
        try:
            await self._client.post(
                "/api/chat",
                json={"model": self.model, "messages": [{"role": "user", "content": "hi"}],
                      "stream": False, "keep_alive": settings.ollama_keep_alive,
                      "options": {"num_predict": 1}},
            )
            log.info("warmed %s", self.name)
        except Exception as exc:  # noqa: BLE001 - warming is best-effort
            log.warning("could not warm %s: %s", self.name, exc)

    async def close(self) -> None:
        await self._client.aclose()
