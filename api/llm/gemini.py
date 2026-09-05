"""
Gemma over the Gemini API (Google AI Studio).

Why this file exists at all: Ollama's `format` parameter constrains generation
with a GBNF grammar, so a 7B model is *structurally incapable* of emitting an
invalid QuerySpec. A hosted API has no such knob, and the nearest equivalent is
`responseJsonSchema`. Everything careful below is about getting as close to that
guarantee as a remote endpoint allows.

Two things about Gemma-on-Gemini that are not in the docs and cost real time:

  1. `responseMimeType` ALONE IS IGNORED by Gemma. The mime type only takes
     effect when `responseJsonSchema` is sent with it. Send just the mime type
     and you get prose back, which fails json.loads and burns the repair retry
     in api/extract.py for no reason.

  2. GEMMA MAY REJECT `systemInstruction`. Gemma's chat template has no system
     role, and the API surfaces that as a 400 rather than ignoring it. The
     Gemini models accept it happily, so the same code path works for one and
     not the other. `_send` detects that specific failure once, then falls back
     to prepending the system text to the user turn and remembers the choice for
     the process lifetime - so the cost is one wasted call per boot, not one per
     question.

Unlike Ollama there is no keep_alive and no cold start: the model is already
resident on Google's side, so the ~9s first-call penalty documented in
api/llm/ollama.py does not apply here.
"""

from __future__ import annotations

import json
import re
import logging
import time
from typing import Any, AsyncIterator

import httpx

from api.config import settings
from api.llm.base import LLMResult

log = logging.getLogger("tbx.llm.gemini")

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class MissingAPIKey(RuntimeError):
    """No key configured. Raised at construction, not at first question.

    Deliberately eager: the alternative is a 500 on the user's first real
    question, which reads as a broken pipeline rather than an unset variable.
    """


class ModelUnavailable(RuntimeError):
    """404 from the API - the model id is wrong or not available to this key.

    Mirrors api/llm/ollama.py:ModelUnavailable so api/routes/ask.py can report
    "the model is not reachable" instead of blaming the database.
    """


class GeminiLLM:
    def __init__(self, model: str | None = None) -> None:
        if not settings.gemini_api_key:
            raise MissingAPIKey(
                "GEMINI_API_KEY is empty. Get a key at "
                "https://aistudio.google.com/apikey and set it in .env"
            )
        self.model = model or settings.gemini_model
        self.name = f"gemini/{self.model}"
        # None = not yet probed. Set to False the first time a system role is
        # rejected, and never retried after that.
        self._supports_system: bool | None = None
        self._client = httpx.AsyncClient(
            base_url=BASE_URL, timeout=settings.gemini_timeout_s
        )

    # ---- request construction -------------------------------------------

    def _body(self, system: str, user: str, gen: dict[str, Any]) -> dict[str, Any]:
        """Build a request, folding `system` in whichever way this model takes."""
        if self._supports_system is False:
            # Gemma has no system turn. Prepending keeps the instructions ahead
            # of the few-shot block, which is the ordering extract.py assumes.
            return {
                "contents": [{"role": "user", "parts": [{"text": f"{system}\n\n{user}"}]}],
                "generationConfig": gen,
            }
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen,
        }

    async def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        # Key as a HEADER, never a query param: httpx logs the full request
        # URL at INFO, so ?key=... writes the secret into the uvicorn log on
        # every single call. The header form is equivalent to the API and
        # invisible to the logger.
        resp = await self._client.post(
            path, headers={"x-goog-api-key": settings.gemini_api_key}, json=body
        )
        if resp.status_code == 404:
            raise ModelUnavailable(
                f"Model {self.model!r} was not found. Callable Gemma ids on this "
                f"API are gemma-4-26b-a4b-it and gemma-4-31b-it."
            )
        return resp

    @staticmethod
    def _rejects_system(resp: httpx.Response) -> bool:
        """True when a 400 is specifically about the system role.

        Narrow on purpose: a blanket 'retry any 400 without systemInstruction'
        would silently paper over schema errors, which are the failures we most
        need to see.
        """
        if resp.status_code != 400:
            return False
        blob = resp.text.lower()
        return "systeminstruction" in blob or "system_instruction" in blob

    async def _send(self, path: str, system: str, user: str, gen: dict[str, Any]):
        """POST, downgrading to a prepended system prompt once if required."""
        resp = await self._post(path, self._body(system, user, gen))
        if self._supports_system is None:
            if self._rejects_system(resp):
                log.info("%s rejects systemInstruction; prepending instead", self.model)
                self._supports_system = False
                resp = await self._post(path, self._body(system, user, gen))
            else:
                self._supports_system = resp.status_code < 400
        return resp

    # ---- LLM protocol ----------------------------------------------------

    async def complete_json(
        self, system: str, user: str, schema: dict[str, Any]
    ) -> LLMResult:
        t0 = time.perf_counter()
        resp = await self._send(
            f"/models/{self.model}:generateContent",
            system,
            user,
            {
                "temperature": 0,
                # BOTH keys are required. See the module docstring - the mime
                # type on its own is ignored by Gemma.
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        elapsed = int((time.perf_counter() - t0) * 1000)

        content = _first_text(body)
        if content is None:
            # A blocked or empty candidate is a transport-shaped failure, not a
            # wrong answer, so it must raise rather than return an empty spec.
            raise ValueError(
                f"no text in response (finishReason="
                f"{_finish_reason(body)!r})"
            )

        try:
            data = _loads_lenient(content)
        except json.JSONDecodeError as exc:
            log.warning("gemini returned unparseable JSON: %s", content[:400])
            raise ValueError(f"model did not return JSON: {exc}") from exc

        usage = body.get("usageMetadata") or {}
        return LLMResult(
            data=data,
            input_tokens=usage.get("promptTokenCount", 0),
            output_tokens=usage.get("candidatesTokenCount", 0),
            latency_ms=elapsed,
            model=self.name,
            raw=content,
        )

    async def stream_text(self, system: str, user: str) -> AsyncIterator[str]:
        gen = {
            "temperature": 0.1,
            "maxOutputTokens": settings.narration_max_tokens,
        }
        # The streaming endpoint cannot reuse _send: httpx streams must be
        # consumed inside their context manager, so the retry is inlined.
        for attempt in (1, 2):
            body = self._body(system, user, gen)
            async with self._client.stream(
                "POST",
                f"/models/{self.model}:streamGenerateContent",
                params={"alt": "sse"},
                headers={"x-goog-api-key": settings.gemini_api_key},
                json=body,
            ) as resp:
                if resp.status_code == 404:
                    raise ModelUnavailable(f"Model {self.model!r} was not found.")
                if resp.status_code >= 400:
                    await resp.aread()
                    if attempt == 1 and self._supports_system is None \
                            and self._rejects_system(resp):
                        log.info("%s rejects systemInstruction; prepending",
                                 self.model)
                        self._supports_system = False
                        continue
                    resp.raise_for_status()
                self._supports_system = self._supports_system is not False
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    piece = _first_text(chunk)
                    if piece:
                        yield piece
                return

    async def warm(self) -> None:
        """No-op, kept for interface parity with OllamaLLM.

        There is no local model to page in, so warming would spend a real API
        call to save nothing.
        """
        return

    async def close(self) -> None:
        await self._client.aclose()


# --------------------------------------------------------------------------
# Response shape helpers. Every field here is optional in the API's own schema,
# so each hop is guarded - a blocked prompt returns a candidate with no parts.
# --------------------------------------------------------------------------


def _loads_lenient(content: str) -> Any:
    """json.loads, tolerating the two things Gemma actually emits.

    MEASURED, not defensive programming. Asked for {"metric": ...} against a
    two-value enum while the prompt pushed hard for a third value, the model
    returned a valid object followed by a bare ``` fence:

        {"metric": "debit_amount"}\\n```

    which json.loads rejects with "Extra data: line 2 column 1". That single
    character would consume extract.py's one repair retry and can turn a
    perfectly good spec into an ExtractionFailed.

    It also proves the schema is NOT grammar-enforced here. Ollama's `format`
    compiles to GBNF and the sampler physically cannot emit a token past the
    closing brace; Gemini's responseJsonSchema only steers. So this function is
    the difference between a guarantee and a strong tendency, and it is why the
    validator in api/schema.py stays the real contract.
    """
    s = content.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # raw_decode stops at the end of the first well-formed value and
        # ignores whatever follows it.
        start = s.find("{")
        if start == -1:
            raise
        obj, _end = json.JSONDecoder().raw_decode(s[start:])
        log.info("stripped trailing content after JSON object")
        return obj


def _first_text(body: dict[str, Any]) -> str | None:
    for cand in body.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            text = part.get("text")
            if text:
                return text
    return None


def _finish_reason(body: dict[str, Any]) -> str | None:
    for cand in body.get("candidates") or []:
        if cand.get("finishReason"):
            return cand["finishReason"]
    return (body.get("promptFeedback") or {}).get("blockReason")
