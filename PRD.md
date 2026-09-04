# PRD — Grounded Finance Assistant

TBX / BVP Tech Catalyst. **Solo build, today.** Hackathon tomorrow.

Companions: [PLAN.md](PLAN.md) (strategy) · [FLOW.md](FLOW.md) (architecture) · [AMBIGUITY.md](AMBIGUITY.md) (clarification design) · [data/DATA_DICTIONARY.md](data/DATA_DICTIONARY.md) (schema)

---

## 0. Goal for today

Build the complete system against our stand-in dataset so that tomorrow is **swap the data, adjust a column map, demo**.

**Definition of done for today:** a question typed into a Next.js chat box returns a narrated answer plus a verifiable breakdown table, computed by SQL against Postgres, with ambiguity handling and a numeric provenance check — end to end, locally.

**The one milestone that matters:** Phase 4. One real question answered end to end. Everything before it is scaffolding; everything after is quality. If you hit Phase 4 with hours to spare you will finish. If you don't, cut ruthlessly from §12.

---

## 1. Verified environment

Confirmed working on this machine — no assumptions:

| Component  | Version         | Status                                                                          |
| ---------- | --------------- | ------------------------------------------------------------------------------- |
| PostgreSQL | 18.6 (Homebrew) | running,`brew services`                                                       |
| pg_trgm    | 1.6             | enabled on`tbx_finance`                                                       |
| pgvector   | 0.8.6           | enabled on`tbx_finance`                                                       |
| Ollama     | 0.33.2          | running on`:11434`                                                            |
| Python     | 3.14.7          | `/opt/homebrew/bin/python3`                                                   |
| torch      | 2.14.0          | `cp314` arm64 wheel exists, so sbert *would* work — **not installed by choice**, §7.1 |
| Database   | `tbx_finance` | 1,019,354 txns · 7 tables · 6 FKs · 3 views                                  |

Models pulled and verified working:

| Model | Size | Role | Verified |
|---|---|---|---|
| `qwen2.5:7b-instruct` | 4.7 GB | extraction + narration | 5/5 schema-valid JSON; 1.8–4.0 s warm, ~9 s cold |
| `nomic-embed-text` | 274 MB | embeddings, 768-dim | cosine 0.841 vs 0.405 on the real vocabulary |

**torch is deliberately NOT installed** — see §7.1.

---

## 2. Today's critical path

Solo means strict sequence. Each phase has an acceptance test you can run before moving on. **Do not start a phase until the previous one's test passes** — debugging two layers at once is what kills solo builds.

| #  | Phase                            | Est  | Acceptance test                                                                   |
| -- | -------------------------------- | ---- | --------------------------------------------------------------------------------- |
| 0  | Scaffolding                      | 0:30 | `uvicorn api.main:app` serves `/api/health` → `{"ok":true}`                |
| 1  | **Contract**               | 1:00 | `python -m api.schema_export` writes valid JSON Schema; `types.ts` generated  |
| 2  | DB layer + migrations            | 1:00 | `registry.build()` prints coverage, 34 categories, 4 recon statuses             |
| 3  | **SQL compiler**           | 1:30 | 8 hand-written specs compile and return correct numbers.**No LLM involved** |
| 4  | **Extract + first answer** | 1:00 | `curl /api/ask` with "spend last month" returns correct `$1,068,521,181.57`   |
| 5  | Guardrails                       | 1:30 | 4 measured ambiguity spreads reproduce; out-of-range returns "no data"            |
| 6  | Narration + verification         | 1:00 | Narrated answer; injected fake number is caught by provenance check               |
| 7  | **Next.js UI**             | 2:30 | Type a question, watch table render before prose finishes                         |
| 8  | Semantic index                   | 1:00 | "medical supplies" resolves to`Hospital: Clinic/Lab Supplies`                   |
| 9  | Eval harness                     | 1:00 | `python -m eval.run` prints accuracy over 50 questions                          |
| 10 | Bake-off + polish                | 1:00 | Model comparison table for the deck                                               |

**Minimum demoable = Phases 0–4 + a stripped Phase 7.** Roughly 6 hours. Everything else is score, not survival.

---

## 3. Repository layout

```
TBX/
├── api/
│   ├── main.py              FastAPI app, CORS, lifespan
│   ├── config.py            pydantic-settings
│   ├── schema.py            ★ QuerySpec + SSE events — THE CONTRACT
│   ├── schema_export.py     pydantic → JSON Schema → types.ts
│   ├── registry.py          SemanticRegistry (DB introspection)
│   ├── db.py                asyncpg pool
│   ├── compile.py           QuerySpec → SQL
│   ├── extract.py           LLM call #1
│   ├── narrate.py           LLM call #2 + provenance check
│   ├── resolve/
│   │   ├── entities.py      trgm + vector hybrid
│   │   ├── coverage.py
│   │   └── ambiguity.py     detectors · probe · policy
│   ├── llm/
│   │   ├── base.py          Protocol: complete(prompt, schema) -> dict
│   │   ├── ollama.py        primary
│   │   └── anthropic.py     written today, used at bake-off
│   ├── embed/
│   │   ├── base.py          Protocol: encode(list[str]) -> ndarray
│   │   ├── ollama_embed.py  nomic-embed-text, 768d  (primary)
│   │   └── sbert.py         all-MiniLM-L6-v2, 384d  (optional fallback)
│   ├── routes/
│   │   ├── ask.py  clarify.py  export.py  meta.py
│   └── sql/
│       ├── 001_app_tables.sql
│       └── 002_semantic_index.sql
├── web/                     Next.js App Router
├── eval/
│   ├── questions.yaml       50 questions + SQL ground truth
│   ├── run.py               harness
│   └── bakeoff.py
├── scripts/                 ✅ done (01–04)
└── data/                    ✅ done
```

---

## 4. The contract — `api/schema.py`

Freeze this in Phase 1. Everything downstream depends on it.

```python
from __future__ import annotations
from datetime import date
from decimal import Decimal
from typing import Literal, Annotated
from pydantic import BaseModel, Field

Dimension = Literal["vendor", "category", "account", "object", "department",
                    "fund", "fund_type", "program", "month", "quarter",
                    "fiscal_year", "payment_status", "reconciliation_status"]

Metric = Literal["amount_paid", "amount_total", "amount_pending",
                 "amount_retainage", "txn_count", "voucher_count",
                 "vendor_count", "avg_amount", "days_outstanding"]

Intent = Literal["aggregate", "list", "compare", "reconcile",
                 "anomaly", "clarify", "unsupported"]


class Period(BaseModel):
    start: date
    end: date                        # HALF-OPEN: [start, end)
    label: str = ""                  # "August 2026" — for narration


class Filters(BaseModel):
    vendor_query: str | None = None          # raw user text, pre-resolution
    vendor_ids: list[str] = Field(default_factory=list)   # post-resolution
    categories: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    funds: list[str] = Field(default_factory=list)
    payment_status: list[str] = Field(default_factory=list)
    reconciliation_status: list[str] = Field(default_factory=list)
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None


class QuerySpec(BaseModel):
    intent: Intent
    metric: Metric = "amount_paid"
    group_by: list[Dimension] = Field(default_factory=list, max_length=3)
    filters: Filters = Field(default_factory=Filters)

    # The $1.34B fork — explicit, never silently inferred. See AMBIGUITY.md §A.
    #
    # date_basis selects WHICH of the two fields below is read:
    #   "payment_date" -> period / compare_period   (the model derives dates)
    #   "fiscal_year"  -> fiscal_year / compare_fiscal_year  (model emits "2026")
    #
    # Measured why: asked "How much did we pay McKesson in FY2026?", Qwen-7B
    # returned period 2026-04-01 → 2027-03-31 — the Indian fiscal year, not
    # SF's Jul–Jun. The model is unreliable at deriving fiscal boundaries and
    # never needs to: the compiler filters the fiscal_year column directly.
    # Removing the derivation removes the failure by construction.
    date_basis: Literal["payment_date", "fiscal_year"] = "payment_date"

    period: Period | None = None                 # read iff date_basis="payment_date"
    compare_period: Period | None = None

    fiscal_year: str | None = None               # read iff date_basis="fiscal_year"
    compare_fiscal_year: str | None = None       # e.g. "2026" — never a date range

    @model_validator(mode="after")
    def _check_date_fields(self) -> "QuerySpec":
        if self.date_basis == "fiscal_year":
            # Dates the model may have guessed are discarded, not trusted.
            self.period = self.compare_period = None
            if self.intent not in ("clarify", "unsupported") and not self.fiscal_year:
                raise ValueError("date_basis='fiscal_year' requires fiscal_year")
        else:
            self.fiscal_year = self.compare_fiscal_year = None
        return self

    sort_desc: bool = True
    limit: Annotated[int, Field(ge=1, le=200)] = 20

    # For the provenance panel
    reasoning: str = ""
    ambiguities: list[str] = Field(default_factory=list)
```

### SSE event union

```python
class StageEvent(BaseModel):
    type: Literal["stage"] = "stage"
    stage: Literal["understanding", "checking", "querying", "explaining"]

class SpecEvent(BaseModel):
    type: Literal["spec"] = "spec"
    spec: QuerySpec

class ClarifyOption(BaseModel):
    key: str
    label: str
    detail: str
    preview: str                 # "$16,823,239,767.06 · 511,237 transactions"

class ClarifyEvent(BaseModel):
    type: Literal["clarify"] = "clarify"
    ambiguity_id: str
    kind: str
    message: str
    options: list[ClarifyOption]
    default_key: str

class SqlEvent(BaseModel):
    type: Literal["sql"] = "sql"
    sql: str
    params: dict

class RowsEvent(BaseModel):
    type: Literal["rows"] = "rows"
    result_id: str
    columns: list[str]
    rows: list[list]
    row_count: int
    elapsed_ms: int
    truncated: bool = False

class TokenEvent(BaseModel):
    type: Literal["token"] = "token"
    text: str

class VerifiedEvent(BaseModel):
    type: Literal["verified"] = "verified"
    ok: bool
    numbers_checked: int
    unverified: list[str] = Field(default_factory=list)

class NoteEvent(BaseModel):
    type: Literal["note"] = "note"
    kind: Literal["assumption", "coverage", "anomaly"]
    text: str

class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    message_id: str
    confidence: Literal["high", "medium", "low"]

class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
    recoverable: bool = True

Event = Annotated[
    StageEvent | SpecEvent | ClarifyEvent | SqlEvent | RowsEvent
    | TokenEvent | VerifiedEvent | NoteEvent | DoneEvent | ErrorEvent,
    Field(discriminator="type"),
]
```

### Type generation

```python
# api/schema_export.py
import json
from pydantic import TypeAdapter
from api.schema import QuerySpec, Event

print(json.dumps({
    "$defs": {
        "QuerySpec": QuerySpec.model_json_schema(),
        "Event": TypeAdapter(Event).json_schema(),
    }
}, indent=2))
```

```jsonc
// web/package.json
"scripts": {
  "gen:types": "cd .. && python -m api.schema_export > web/shared/schema.json && cd web && json-schema-to-typescript shared/schema.json -o lib/types.ts",
  "dev": "npm run gen:types && next dev"
}
```

---

## 5. SQL migrations

### `api/sql/001_app_tables.sql`

```sql
CREATE TABLE IF NOT EXISTS chat_threads (
    thread_id  TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled    JSONB NOT NULL DEFAULT '{}'::jsonb   -- sticky ambiguity choices
);

CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT PRIMARY KEY,
    thread_id  TEXT NOT NULL REFERENCES chat_threads(thread_id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user','assistant')),
    question   TEXT,
    spec       JSONB,
    sql_text   TEXT,
    result     JSONB,
    narration  TEXT,
    verified   BOOLEAN,
    confidence TEXT,
    latency_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (thread_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_msg_thread ON chat_messages (thread_id, seq);

CREATE TABLE IF NOT EXISTS query_log (
    id             BIGSERIAL PRIMARY KEY,
    thread_id      TEXT,
    question       TEXT NOT NULL,
    spec           JSONB,
    sql_text       TEXT,
    row_count      INTEGER,
    sql_ms         INTEGER,
    llm_extract_ms INTEGER,
    llm_narrate_ms INTEGER,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    model          TEXT,
    verified       BOOLEAN,
    clarified      BOOLEAN DEFAULT false,
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_qlog_created ON query_log (created_at DESC);
```

### `api/sql/002_semantic_index.sql`

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS semantic_index (
    id          BIGSERIAL PRIMARY KEY,
    entity_type TEXT   NOT NULL,
    entity_key  TEXT   NOT NULL,
    label       TEXT   NOT NULL,
    aliases     TEXT[] NOT NULL DEFAULT '{}',
    embedding   vector(768),                 -- nomic-embed-text. 384 if sbert
    UNIQUE (entity_type, entity_key)
);

CREATE INDEX IF NOT EXISTS idx_sem_type ON semantic_index (entity_type);
CREATE INDEX IF NOT EXISTS idx_sem_trgm ON semantic_index USING gin (label gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_sem_vec  ON semantic_index
    USING hnsw (embedding vector_cosine_ops);
```

> Set `embed_dim` in config and keep the DDL in sync. Switching nomic(768) ↔ sbert(384) means dropping and rebuilding the column — trivial at 9k rows, but don't discover it at hour 30.

### 7.1 Why nomic-embed-text, not sentence-transformers

Decided after measuring, having already installed Ollama for the LLM:

| | `nomic-embed-text` (Ollama) | `all-MiniLM-L6-v2` (sbert) |
|---|---|---|
| Extra dependency | **none** — Ollama already required and running | torch + transformers, ~2 GB |
| Download | 274 MB, **already pulled** | ~90 MB model + ~2 GB torch |
| Dimensions | 768 | 384 |
| Python 3.14 risk | none (HTTP call) | low — `cp314` wheel confirmed exists |

Semantic separation verified on the real vocabulary:

```
cos("medical supplies", "Hospital: Clinic/Lab Supplies") = 0.841   ← the match we need
cos("medical supplies", "Debt Service")                  = 0.405   ← clean separation
```

`pg_trgm` scores ≈ 0.0 on that first pair — no shared trigrams. That single number is the
entire justification for the semantic layer.

`sbert.py` stays in the tree behind `embed_provider="sbert"` in case you want the
comparison for the deck, but **do not install torch today**. Phase 8 is cuttable; keep it
cheap. Batch the index build with `POST /api/embed` (Ollama accepts an array in `input`).

---

## 6. Backend module specs

### `registry.py` — dataset independence lives here

```python
@dataclass(frozen=True)
class SemanticRegistry:
    earliest: date
    latest: date
    transaction_count: int
    categories: list[str]
    departments: list[str]
    funds: list[str]
    payment_statuses: list[str]
    recon_statuses: list[str]
    money_columns: list[str]
    has_fiscal_year: bool
    reconciled_label: str | None      # which status means "matched"

    @classmethod
    async def build(cls, db) -> "SemanticRegistry": ...
```

Built once at FastAPI lifespan startup, injected everywhere. **No module may hardcode a value that belongs here.** This single rule is what makes tomorrow's swap a 2-hour job.

### `compile.py` — the deterministic core

```python
def compile_query(spec: QuerySpec, reg: SemanticRegistry) -> tuple[str, dict]:
    """QuerySpec → parameterised SQL. Pure. No LLM. No I/O."""

def compile_scalar(spec: QuerySpec, reg: SemanticRegistry) -> tuple[str, dict]:
    """Same filters, GROUP BY/ORDER/LIMIT stripped, single scalar + count.
    Used by ambiguity probes so a probe can never disagree with the answer."""
```

Two rules, both non-negotiable:

1. **Every literal is a bound parameter.** No f-string interpolation into SQL, ever.
2. **Dates compile to half-open ranges**, never `substr()`:
   ```sql
   transaction_date >= %(start)s AND transaction_date < %(end)s   -- 18 ms
   -- NOT: substr(transaction_date,1,7) = '2026-08'               -- 512 ms
   ```

`date_basis="fiscal_year"` compiles to `fiscal_year = %(fy)s` instead. That switch is the entire mechanism behind the $1.34B fork.

### `resolve/entities.py`

```python
async def resolve_vendor(q: str, db, reg, use_semantic: bool) -> VendorMatch:
    """
    Hybrid: 0.6 * similarity(vendor_name, q) + 0.4 * (1 - cosine_distance)
    Returns candidates with spend, so ambiguity.py can decide without re-querying.
    """
```

```sql
-- trgm leg
SELECT vendor_id, vendor_name, total_amount, similarity(vendor_name, %(q)s) AS sim
FROM vendors WHERE vendor_name %% %(q)s ORDER BY sim DESC LIMIT 10;

-- vector leg (stage 2)
SELECT entity_key, label, 1 - (embedding <=> %(vec)s) AS cos
FROM semantic_index WHERE entity_type = 'vendor'
ORDER BY embedding <=> %(vec)s LIMIT 10;
```

### `narrate.py` — LLM call #2 and the provenance check

```python
NUM_RE = re.compile(r"-?\$?\d[\d,]*\.?\d*")

def _canon(tok: str) -> str:
    return tok.replace("$", "").replace(",", "").rstrip(".").lstrip("-")

def verify_numbers(text: str, rows: list[list]) -> tuple[bool, list[str]]:
    """Every number in the narration must appear in the result rows.
    Tolerates $ , and rounding to 0/1/2 dp."""
    allowed: set[str] = set()
    for row in rows:
        for v in row:
            if isinstance(v, (int, float, Decimal)):
                d = Decimal(str(v))
                allowed |= {_canon(f"{d:.{p}f}") for p in (0, 1, 2)}
                allowed.add(_canon(str(d)))
    bad = [t for t in NUM_RE.findall(text)
           if _canon(t) and _canon(t) not in allowed and not _is_year(t)]
    return (not bad), bad
```

On failure: regenerate once with a stricter prompt; if it fails again, emit a deterministic template answer and set `confidence="low"`. **Never ship an unverified figure.**

---

## 7. LLM integration

### Ollama structured output — the reliability mechanism

Ollama accepts a **JSON Schema** in `format`, constraining generation. This is what makes a 7B model dependable at extraction. Do not rely on prompt-only "return JSON".

```python
async def complete_json(self, system: str, user: str, schema: dict) -> dict:
    r = await self._client.post("/api/chat", json={
        "model": "qwen2.5:7b-instruct",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "format": schema,               # ← JSON Schema, not the string "json"
        "stream": False,
        "options": {"temperature": 0, "num_ctx": 8192},
    }, timeout=60)
    return json.loads(r.json()["message"]["content"])
```

### Extraction prompt skeleton

```
SYSTEM
You convert finance questions into a QuerySpec JSON object.
You NEVER compute numbers. You NEVER invent vendor names, categories or departments.

Data coverage: {earliest} to {latest}.
Available categories: {categories}
Available departments: {departments}
Reconciliation statuses: {recon_statuses}
Today is {today}. "Last month" means {last_month_label}.

RULES
- metric: "spend"/"paid"/"spent" -> amount_paid.  "committed"/"owed" -> amount_total.
- date_basis: "FY2026"/"fiscal"/"fiscal year" -> "fiscal_year", and set
  fiscal_year to JUST THE YEAR, e.g. "2026". Do NOT invent start/end dates.
  Anything else -> "payment_date", and set period {start, end}.
- filters.vendor_query: fill ONLY when the question names a specific company.
  "vendor payouts", "suppliers", "top vendors", "payments" are NOT vendor names —
  leave vendor_query null for those.
- Unknown category/department -> leave the list empty and note it in `ambiguities`.
- If the answer needs data this schema does not hold (headcount, budgets,
  forecasts, employees, invoices not in the tables) -> intent "unsupported".

{8 few-shot examples}
```

Few-shot examples matter more than prompt prose for a 7B. Budget 8, drawn from the eval set, covering each intent plus one `unsupported` and one `clarify`.

**Measured baseline — why the rules above are worded that way.** Zero-shot against
`qwen2.5:7b-instruct` with schema constraint, 5 probe questions:

| Result | Count |
|---|---|
| Valid JSON conforming to schema | **5 / 5** — the `format` constraint works |
| Semantically correct | **~2 / 5** |

The three failures, each now addressed:

| Question | Model produced | Fix |
|---|---|---|
| "spend on **vendor payouts** last month" | `vendor_query: "payouts"` | Explicit negative rule + negative few-shot |
| "**Top 5 vendors** by spend…" | `vendor_query: "<entire question>"` | Same rule |
| "…in **FY2026**?" | `period: 2026-04-01 → 2027-03-31` (Indian FY) | **Schema fix** — model emits `"2026"`, never dates |
| "What is our **headcount**?" | `intent: "aggregate"` | Explicit unsupported rule + few-shot |

The lesson worth carrying: **a JSON Schema guarantees shape, not meaning.** Budget your
prompt effort on few-shots and on removing derivations the model doesn't need to make.

**Latency measured:** 1.8–4.0 s warm per call, ~9 s cold (model load). Two calls per
question ≈ 4–8 s. Set `keep_alive: "30m"` on every Ollama request to avoid reloads, and
keep prompts short. If this stays above ~5 s end-to-end after few-shots, that is a
legitimate reason to switch to `claude-haiku-4-5` at the bake-off — record it either way.

### Narration prompt

```
SYSTEM
You explain a result that has ALREADY been computed.
You MUST NOT perform arithmetic. You MUST NOT state any number
that does not appear in the rows below.
Two or three sentences. Lead with the answer.
If `notes` are present, state the assumption plainly.

QUESTION: {question}
COLUMNS:  {columns}
ROWS:     {rows}            ← capped at 50; say "top N of M" if truncated
NOTES:    {assumption_notes}
```

### `llm/base.py`

```python
class LLM(Protocol):
    name: str
    async def complete_json(self, system: str, user: str, schema: dict) -> dict: ...
    async def stream_text(self, system: str, user: str) -> AsyncIterator[str]: ...
```

Write `anthropic.py` today against the same Protocol so the bake-off is a config flip. Use `claude-haiku-4-5` ($1.00/$5.00 per 1M tokens, 200K context).

---

## 8. Frontend spec

```
web/
├── app/
│   ├── layout.tsx
│   ├── page.tsx              SERVER: fetch /api/coverage + /api/suggestions
│   └── globals.css
├── components/
│   ├── chat/  Chat.tsx · MessageList.tsx · Composer.tsx · StageIndicator.tsx
│   ├── answer/ AnswerCard.tsx · BreakdownTable.tsx · ClarifyCard.tsx
│   │            ProvenancePanel.tsx · VerifiedBadge.tsx · ExportButton.tsx
│   └── coverage/ CoverageBanner.tsx
└── lib/  sse.ts · api.ts · types.ts (generated) · format.ts · store.ts
```

### State

One `useReducer` in `Chat.tsx` whose action union **is** the SSE event union. A new backend event then surfaces as a TypeScript exhaustiveness error rather than silent breakage.

```ts
type Message =
  | { role: "user"; text: string }
  | { role: "assistant"; stage?: Stage; spec?: QuerySpec; sql?: SqlEvent
      rows?: RowsEvent; narration: string; verified?: VerifiedEvent
      notes: NoteEvent[]; clarify?: ClarifyEvent; done?: DoneEvent };
```

### Streaming

`EventSource` cannot POST — use `fetch` + `ReadableStream` (implementation in [FLOW.md](FLOW.md) §4).

### Component requirements

| Component           | Must do                                                                                                                                 |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `BreakdownTable`  | `font-variant-numeric: tabular-nums`, right-aligned money, sticky header, `overflow-x:auto`, sortable                               |
| `ClarifyCard`     | Render each option's**preview number** on the button. One click → `POST /api/clarify` → re-run from step 6, no new extraction |
| `ProvenancePanel` | Collapsed by default. Spec JSON · SQL · row count · timings. This*is* the explainability requirement                               |
| `VerifiedBadge`   | "5 figures traced to source" on pass; amber warning on fail                                                                             |
| `CoverageBanner`  | Always visible: "Data covers 2024-09-01 → 2026-08-29"                                                                                  |

### Rendering order — non-negotiable

`rows` renders **before** narration streams. Numbers on screen at ~0.9 s; prose catches up. That ordering is the demo.

---

## 9. Config

```python
# api/config.py
class Settings(BaseSettings):
    database_url: str = "postgresql://arnab@localhost:5432/tbx_finance"

    llm_provider: Literal["ollama", "anthropic"] = "ollama"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    anthropic_model: str = "claude-haiku-4-5"

    embed_provider: Literal["ollama", "sbert"] = "ollama"     # see §7.1
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    ollama_keep_alive: str = "30m"       # avoid 9s cold reloads

    enable_semantic: bool = False        # Phase 8 flag — ship off, turn on when proven

    ambiguity_silent: float = 0.01
    ambiguity_disclose: float = 0.10

    max_rows_to_llm: int = 50
    cors_origins: list[str] = ["http://localhost:3000"]
```

---

## 10. Eval harness

`eval/questions.yaml`:

```yaml
- id: q001
  question: "How much did we spend on vendor payouts last month?"
  category: aggregate
  ground_truth_sql: |
    SELECT SUM(amount_paid) FROM transactions
    WHERE transaction_date >= DATE '2026-08-01'
      AND transaction_date <  DATE '2026-09-01'
  expect: {kind: numeric, tolerance: 0.01}

- id: q044
  question: "How much did we spend in FY2026?"
  category: ambiguous
  expect: {kind: behaviour, must: clarify, ambiguity_kind: temporal}

- id: q049
  question: "How much did we spend in December 2026?"
  category: out_of_range
  expect: {kind: behaviour, must: refuse_no_data}
```

`eval/run.py` reports: numeric accuracy, behaviour accuracy, verification pass rate, p50/p95 latency, tokens per question. That table is your deck's model-choice slide.

Distribution per [PLAN.md](PLAN.md) §7 — **10 of the 50 are behaviour tests** (5 must-clarify, 5 must-refuse). They're worth as much as the other 40.

---

## 11. Tomorrow's swap runbook

| Step            | Action                                                          | Est             |
| --------------- | --------------------------------------------------------------- | --------------- |
| 1               | Inspect their files; map columns to our 7 tables                | 0:30            |
| 2               | Write`scripts/05_load_provided.py` emitting the same 7 CSVs   | 0:45            |
| 3               | `04_load_postgres.py` into a fresh `tbx_live` DB            | 0:10            |
| 4               | Update`Dimension`/`Metric` literals if their schema differs | 0:15            |
| 5               | Update the column map in`compile.py`                          | 0:15            |
| 6               | Rebuild`semantic_index`                                       | 0:05            |
| 7               | Re-run`eval/run.py`; fix fallout                              | 0:30            |
| **Total** |                                                                 | **~2:30** |

`registry.py`, `resolve/ambiguity.py`, and the entire frontend need **no changes** — they are registry-driven by construction.

---

## 12. Cut list

If you're behind, cut in this order. Each line above the next.

1. Phase 10 bake-off → just report Qwen numbers
2. Phase 8 semantic index → `enable_semantic=False`, trgm only
3. CSV/Excel export
4. Anomaly callouts
5. Multi-turn follow-ups → single-turn only
6. `ProvenancePanel` SQL view → keep spec only

**Never cut:** the compiler (Phase 3), the coverage check, or the provenance check. Those three *are* the 30% grounding score.

---

## 13. Risks

| Risk                                  | Signal                         | Mitigation                                                                                                   |
| ------------------------------------- | ------------------------------ | ------------------------------------------------------------------------------------------------------------ |
| Qwen-7B extraction unreliable         | Spec accuracy < 80% at Phase 4 | Add few-shots first; then flip`llm_provider=anthropic`. Interface is identical                             |
| Extraction fills `vendor_query` with junk | "vendor payouts" → zero rows | Measured failure, §7. Negative rule + negative few-shots. Assert in eval that `vendor_query` is null for non-company phrasings |
| End-to-end latency > 5 s | Two Qwen calls at 4 s each | `keep_alive: "30m"`; trim prompts; if still slow, `llm_provider=anthropic` and say so in the deck |
| Embeddings need sbert after all | Phase 8 quality poor | `embed_provider=sbert`, `pip install sentence-transformers`, `embed_dim=384`, rebuild `semantic_index`. ~15 min |
| Provided dataset shape differs wildly | Tomorrow, step 1               | Registry + ambiguity need no change. Worst case is a bigger column map                                       |
| SSE buffering in dev                  | Events arrive in one lump      | Confirm no proxy; set`X-Accel-Buffering: no`; flush per event                                              |
| Demo machine has no model             | Hackathon venue                | Pre-pull models today. Record a fallback demo video at the end                                               |
| Solo fatigue                          | Hour 10+                       | Hit Phase 4 early. A working spine beats a half-built better one                                             |

---

## 14. Start here

```bash
cd "…/TBX"
python3 -m venv .venv && source .venv/bin/activate
pip install "fastapi[standard]" "psycopg[binary,pool]" pydantic-settings httpx pyyaml
export PATH="/opt/homebrew/opt/postgresql@18/bin:$PATH"
psql -d tbx_finance -f api/sql/001_app_tables.sql
psql -d tbx_finance -f api/sql/002_semantic_index.sql
```

Then Phase 1: write `api/schema.py`, freeze it, generate `types.ts`. Everything else follows from that file.
