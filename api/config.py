"""Settings. Every tunable lives here - no magic numbers in module bodies."""

from __future__ import annotations

import os
from datetime import date
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_DSN = "mysql://tbx:tbx@127.0.0.1:3306/tbx_live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- dataset ----
    # Selects api/profiles/*.py, which supplies every domain-specific map:
    # dimensions, metrics, filters, prompt rules, and what the schema does NOT
    # hold. The stand-in profile (vendor_payments) was removed with the move to
    # MySQL - it was PostgreSQL-only and its database no longer exists.
    dataset: Literal["bank_txn"] = "bank_txn"

    # ---- sensitive fields (api/crypto.py) ----
    # HMAC key for tokenizing account_number. Never stored in the database,
    # never logged, never committed. Rotating it invalidates every token, which
    # is correct - they are lookup keys, not stored secrets.
    sensitive_key: str = ""

    # ---- database ----
    database_url: str = _DEFAULT_DSN
    pool_min_size: int = 2
    pool_max_size: int = 10

    # ---- language model (LLM call #1 and #2) ----
    llm_provider: Literal["ollama", "anthropic"] = "ollama"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    ollama_keep_alive: str = "30m"          # avoids the measured ~9s cold reload
    ollama_timeout_s: int = 120
    anthropic_model: str = "claude-haiku-4-5"

    # ---- embeddings (semantic entity resolution, Phase 8) ----
    # nomic-embed-text over sentence-transformers: zero extra dependency, and
    # cos("medical supplies", "Hospital: Clinic/Lab Supplies") = 0.841 vs
    # cos("medical supplies", "Debt Service") = 0.405 on the real vocabulary.
    embed_provider: Literal["ollama", "sbert"] = "ollama"
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768

    # ---- feature flags ----
    enable_semantic: bool = False           # Phase 8. Ship off, enable when proven.

    # ---- ambiguity policy (AMBIGUITY.md) ----
    ambiguity_silent: float = 0.01          # below: answer, say nothing
    ambiguity_disclose: float = 0.10        # below: answer + state assumption
                                            # above: ask the user
    # Second, independent trigger: ask when the ABSOLUTE gap is material even if
    # the percentage is small. Fraction of total spend in the dataset, so it
    # scales with whatever data is loaded. 0.005 of $34.3B = ~$172M.
    ambiguity_material_fraction: float = 0.005

    # ---- time ----
    # Pin "today" to make demos and the eval set reproducible. None = wall clock.
    reference_date: date | None = None

    # ---- limits ----
    # Narration input cap. Deliberately tiny: a 7B model told 'never list more
    # than three' still listed five, so the reliable control is not giving it
    # more than three. The full table is on screen beside the answer anyway.
    max_rows_to_llm: int = 3
    narration_max_tokens: int = 320         # bounds worst-case narration time
    max_rows_to_client: int = 200

    # ---- http ----
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]


settings = Settings()
