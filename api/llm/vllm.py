"""
vLLM client (OpenAI-compatible wire format, local Metal/MLX backend on port 9000).

Mirrors OllamaLLM's contract exactly: `response_format.json_schema` is vLLM's
equivalent of Ollama's `format` field - both compile the schema into a grammar
(xgrammar here) and constrain generation to it, so the same "schema-valid on
the first try" guarantee holds. `temperature: 0` is set explicitly per-call
because the model's own generation_config.json ships top_k/top_p defaults that
the launch flag `--generation-config vllm` only overrides at the server level,
not per-request.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

from api.config import settings
from api.llm.base import LLMResult

log = logging.getLogger("tbx.llm.vllm")


class VLLMLLM:
    def __init__(self, model: str | None = None) -> None:
        self.model = model or settings.vllm_model
        self.name = f"vllm/{self.model}"
        self._client = httpx.AsyncClient(
            base_url=settings.vllm_url, timeout=settings.vllm_timeout_s
        )

    async def complete_json(
        self, system: str, user: str, schema: dict[str, Any]
    ) -> LLMResult:
        t0 = time.perf_counter()
        resp = await self._client.post(
            "/chat/completions",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "query_spec", "schema": schema},
                },
            },
        )
        resp.raise_for_status()
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
        elapsed = int((time.perf_counter() - t0) * 1000)

        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            log.warning("vllm returned unparseable JSON: %s", content[:400])
            raise ValueError(f"model did not return JSON: {exc}") from exc

        usage = body.get("usage", {})
        return LLMResult(
            data=data,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            latency_ms=elapsed,
            model=self.name,
            raw=content,
        )

    async def stream_text(self, system: str, user: str) -> AsyncIterator[str]:
        async with self._client.stream(
            "POST",
            "/chat/completions",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.1,
                "max_tokens": settings.narration_max_tokens,
                "stream": True,
            },
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                piece = chunk["choices"][0].get("delta", {}).get("content", "")
                if piece:
                    yield piece

    async def warm(self) -> None:
        """Compile the extraction grammar and load weights before the first
        real question - first-call schema compilation otherwise lands in a
        demo's latency."""
        try:
            await self._client.post(
                "/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                },
            )
            log.info("warmed %s", self.name)
        except Exception as exc:  # noqa: BLE001 - warming is best-effort
            log.warning("could not warm %s: %s", self.name, exc)

    async def close(self) -> None:
        await self._client.aclose()
