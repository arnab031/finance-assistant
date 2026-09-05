"""Settings. Every tunable lives here - no magic numbers in module bodies."""

from __future__ import annotations

import os
from datetime import date
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_DSN = "mysql://tbx:tbx@127.0.0.1:3306/tbx_live"


def _csv(value: str) -> list[str]:
    """'a, b' -> ['a', 'b'], dropping blanks left by a trailing comma."""
    return [item.strip() for item in value.split(",") if item.strip()]


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
    llm_provider: Literal["ollama", "anthropic", "gemini"] = "ollama"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    ollama_keep_alive: str = "30m"          # avoids the measured ~9s cold reload
    ollama_timeout_s: int = 120
    anthropic_model: str = "claude-haiku-4-5"

    # Models the /ops canary offers, in this order. Declared rather than
    # discovered: the scorecard compares MODELS, and which ones we measure is a
    # decision, not an accident of what happens to be pulled on the box. Leave
    # empty to fall back to offering everything Ollama has.
    #
    # Comma-separated, because .env is edited by hand and a JSON array is not.
    # ollama_model stays the default the app answers chat with; this only
    # widens what a canary run can target.
    eval_models: str = ""

    @property
    def eval_model_names(self) -> list[str]:
        """EVAL_MODELS as a list, always including the configured default so the
        model actually serving chat can never drop out of the picker."""
        names = _csv(self.eval_models)
        if self.ollama_model not in names:
            names.insert(0, self.ollama_model)
        return names

    # Gemma served over the Gemini API. Only two Gemma ids are callable there:
    # gemma-4-26b-a4b-it (26B total, 4B active MoE) and gemma-4-31b-it (dense).
    # The Gemma 3 sizes (1B/4B/12B/27B) are downloadable weights only, so the
    # MoE below is the smallest hosted option by active parameters.
    gemini_api_key: str = ""
    gemini_model: str = "gemma-4-26b-a4b-it"
    gemini_timeout_s: int = 120

    # ---- embeddings (semantic entity resolution, Phase 8) ----
    # nomic-embed-text over sentence-transformers: zero extra dependency, and
    # cos("medical supplies", "Hospital: Clinic/Lab Supplies") = 0.841 vs
    # cos("medical supplies", "Debt Service") = 0.405 on the real vocabulary.
    embed_provider: Literal["ollama", "sbert", "gemini"] = "ollama"
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768

    # ---- analytical replica (api/duck.py) ----
    # DuckDB mirror of the MySQL data, rebuilt from it. Disposable by design:
    # a full rebuild is ~23s, so it lives in /tmp and is never backed up.
    # use_duckdb=False keeps every read on MySQL, which is the fallback if the
    # replica cannot be built (no SELECT access, or the mysql extension is
    # unavailable in a restricted environment).
    use_duckdb: bool = False
    duckdb_path: str = "/tmp/tbx_replica.duckdb"

    # "auto"   - build when missing, refresh when the source has moved, else
    #            reuse. The only setting that is correct on both the first boot
    #            and the hundredth, which is why it is the default: a boolean
    #            forces a choice between "always pay 2 minutes" and "silently
    #            serve stale data", and both are wrong some of the time.
    # "always" - rebuild unconditionally (after a schema change or a backfill).
    # "never"  - reuse whatever is on disk; build only if there is nothing.
    duckdb_refresh: Literal["auto", "always", "never"] = "auto"

    # Monotonic column used to detect new rows and to append them without a
    # full rebuild - an AUTO_INCREMENT id or equivalent. MAX() on it is an
    # index-seek, so the staleness check is O(1) rather than a COUNT(*) that
    # takes minutes on a table this size.
    # Empty = no watermark: staleness falls back to an approximate row count and
    # a change means a FULL rebuild, because without an ordering column there is
    # no way to know which rows are new.
    duckdb_watermark_column: str = ""

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
