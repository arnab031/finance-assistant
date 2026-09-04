# Flow Design

**Review this before I write the PRD.** Decisions needing your sign-off are collected in §9.

Stack: **Next.js (App Router) · FastAPI · PostgreSQL 18 · pg_trgm + pgvector**

---

## 1. Do we need vectors in Postgres?

**Yes — but narrowly, and staged so it's cuttable.**

### The reasoning

The aggregation path must stay deterministic SQL. **Vectors must never produce a number.** The moment an embedding influences an amount, grounding is gone and the 30% criterion goes with it.

But there's a real gap that `pg_trgm` cannot close. Trigram matching is *lexical* — it scores on shared character triples:

| User says | Schema contains | pg_trgm | Embedding |
|---|---|---|---|
| "mckeson" *(typo)* | `MCKESSON CORPORATION` | ✅ strong | ✅ |
| "medical supplies" | `Hospital: Clinic/Lab Supplies` | ❌ **~0.0** | ✅ |
| "the transit agency" | `MTA Municipal Transprtn Agncy` | ❌ ~0.0 | ✅ |
| "IT spending" | `Information Technology` object codes | ❌ weak | ✅ |

"Medical supplies" and "Hospital: Clinic/Lab Supplies" share almost no trigrams. A finance user will absolutely phrase questions that way, and every one of them fails silently today.

So the split is clean:

- **pg_trgm** → lexical / typo / substring matching
- **pgvector** → semantic matching onto closed schema vocabularies
- **Hybrid score** → `0.6 × trigram + 0.4 × cosine`, tuned against the eval set

### What exactly to add

**One auxiliary table. No vector columns on the fact tables.**

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE semantic_index (
    id          BIGSERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,      -- 'vendor' | 'category' | 'account' |
                                    -- 'department' | 'fund' | 'object'
    entity_key  TEXT NOT NULL,      -- the value the SQL compiler filters on
    label       TEXT NOT NULL,      -- human text that was embedded
    aliases     TEXT[] DEFAULT '{}',-- hand-added synonyms
    embedding   vector(384) NOT NULL,
    UNIQUE (entity_type, entity_key)
);

CREATE INDEX idx_sem_type  ON semantic_index (entity_type);
CREATE INDEX idx_sem_trgm  ON semantic_index USING gin (label gin_trgm_ops);
CREATE INDEX idx_sem_vec   ON semantic_index
    USING hnsw (embedding vector_cosine_ops);
```

Why an auxiliary table rather than columns on `transactions`:

- The fact table stays exactly as documented — tomorrow's dataset swap doesn't touch it
- One rebuild command regenerates the whole index from whatever tables exist
- No 1M-row embedding job (we embed ~9,000 distinct labels, not rows)

### Index size — this is small

| Vocabulary | Distinct labels |
|---|---|
| vendors | 7,905 |
| account_name | 615 |
| funds | 338 |
| object_name | 211 |
| departments | 58 |
| category_name | 34 |
| program_name | 10 |
| **Stage 1 total** | **≈ 9,171** |
| *contract_title (stage 3, optional)* | *7,825* |

9,171 × 384 dims ≈ **14 MB**. Build time ~40 s on CPU with `all-MiniLM-L6-v2`. HNSW is honestly overkill at this size — exact search is sub-millisecond — but it costs nothing and looks right.

**Embedding model:** `sentence-transformers/all-MiniLM-L6-v2`, local, 384-dim, ~90 MB, no API key, no per-call cost. Anthropic doesn't ship an embeddings endpoint, so keeping this local also keeps your capped hackathon credits for the two calls that matter.

### Staging — so it's cuttable

| Stage | Scope | Cut if short on time? |
|---|---|---|
| **1** | pg_trgm only. Vendor + exact dimension matching | Never — this is the baseline |
| **2** | pgvector over the 9,171 labels. Hybrid scoring behind `ENABLE_SEMANTIC=true` | **Yes, cuttable.** System works without it |
| **3** | `contract_title` semantic search ("contracts about substation rehab") | Bonus only |

Build stage 1 first and keep stage 2 behind a flag. If tomorrow goes sideways, flip the flag off and everything still works.

---

## 2. System topology

```
┌────────────────────┐         ┌──────────────────────┐        ┌───────────────┐
│  Next.js (App Rtr) │  SSE    │   FastAPI            │  SQL   │  PostgreSQL   │
│  browser           │────────▶│   :8000              │───────▶│  :5432        │
│                    │◀────────│                      │◀───────│               │
│  chat · tables     │  events │  extract · resolve   │        │  7 tables     │
│  provenance panel  │         │  compile · verify    │        │  semantic_idx │
└────────────────────┘         └──────────┬───────────┘        │  query_log    │
                                          │                    └───────────────┘
                                          │ 2 calls/question
                                          ▼
                                 ┌──────────────────┐
                                 │  LLM provider    │
                                 │  Ollama / API    │
                                 └──────────────────┘
```

**The browser talks to FastAPI directly** (CORS enabled). No Next.js API proxy.

Rationale: proxying SSE through a Next route handler is a known source of buffering bugs and costs you a debugging hour you don't have. Next.js is purely the UI layer. If you later want to hide the backend, add the proxy then — it's a one-file change.

---

## 3. Backend flow

### The request path

```
POST /api/ask  { question, session_id, thread_id }
 │
 ├─ 0  Load session state ......... previous QuerySpec, settled ambiguities
 │                                   emit: stage "understanding"
 ├─ 1  LLM CALL #1 → QuerySpec .... structured output, JSON-schema constrained
 │                                   follow-up? → emit a PATCH, not a full spec
 │                                   emit: spec
 ├─ 2  Validate ................... pydantic. Fail → 1 repair retry → unsupported
 │                                   emit: stage "checking"
 ├─ 3  Resolve entities ........... trgm (+ vector if enabled) → concrete keys
 │                                   weak/multiple match → feeds step 5
 ├─ 4  Coverage check ............. registry window vs requested period
 │                                   outside → TERMINAL "no data for that period"
 ├─ 5  Ambiguity resolve .......... detect → probe → policy   (see AMBIGUITY.md)
 │                                   clarify → emit: clarify, STOP, await user
 │                                   disclose → carry notes forward
 │                                   emit: stage "querying"
 ├─ 6  Compile .................... QuerySpec → parameterised SQL. Pure Python.
 │                                   emit: sql
 ├─ 7  Execute .................... Postgres. NUMERIC. Exact.
 │                                   emit: rows        ← TABLE RENDERS HERE
 │                                   emit: stage "explaining"
 ├─ 8  LLM CALL #2 → narration .... input is ONLY the result rows
 │                                   emit: token (streamed)
 ├─ 9  Numeric provenance check ... every number in text ∈ rows?
 │                                   fail → regenerate once → template fallback
 │                                   emit: verified
 └─10  Persist + log .............. chat_messages, query_log
                                     emit: done { confidence }
```

Steps 2–7 involve **no model calls**. Steps 3–5 are the guardrail stack.

**Key UX consequence of this ordering:** the breakdown table is emitted at step 7, *before* narration begins. The user sees real numbers while the prose is still streaming. It feels instant, and it visually reinforces that the numbers came from the database rather than the model.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/ask` | Main. `text/event-stream` |
| `POST` | `/api/clarify` | `{thread_id, ambiguity_id, chosen_key}` → re-runs from step 6. **No new extraction call** |
| `GET` | `/api/coverage` | Data window + row counts, for the UI banner |
| `GET` | `/api/suggestions` | Starter questions, seeded from the eval set |
| `GET` | `/api/export/{result_id}` | CSV / XLSX of a computed result |
| `GET` | `/api/thread/{id}` | Replay a conversation |
| `GET` | `/api/health` | Registry loaded, DB reachable, model reachable |

### SSE event protocol — the core contract

This is what the frontend is built against. Freeze it at hour 2 alongside `QuerySpec`.

```jsonc
event: stage     {"stage":"understanding"|"checking"|"querying"|"explaining"}
event: spec      {"intent":"aggregate","metric":"amount_paid", ...}
event: clarify   {"ambiguity_id":"amb_01","message":"...","options":[...],"default":"..."}
event: sql       {"sql":"SELECT ...","params":{...}}
event: rows      {"result_id":"res_01","columns":[...],"rows":[[...]],
                  "row_count":12,"elapsed_ms":18,"truncated":false}
event: token     {"text":"In August 2026 you paid "}
event: verified  {"ok":true,"numbers_checked":5,"unverified":[]}
event: note      {"kind":"assumption","text":"Counted as money paid..."}
event: done      {"message_id":"msg_01","confidence":"high"|"medium"|"low"}
event: error     {"code":"...","message":"...","recoverable":true}
```

Every event is independently renderable — the UI never waits for the full response.

---

## 4. Frontend flow (Next.js)

### Why Next.js here, concretely

App Router, but **almost everything is a client component** — this is a live streaming chat, not a content site. You're using Next for routing, bundling, and deployment ergonomics rather than SSR. That's a fine reason; just don't fight it by trying to server-render the chat.

The one genuinely useful server piece: `app/page.tsx` server-fetches `/api/coverage` so the coverage banner and starter questions are in the first paint. Everything after that is client-side streaming.

### Structure

```
app/
  layout.tsx                 root, theme, fonts
  page.tsx                   SERVER: fetch coverage + suggestions → <Chat/>
  globals.css

components/
  chat/
    Chat.tsx                 CLIENT: owns thread state, SSE lifecycle
    MessageList.tsx          virtualised if long
    Composer.tsx             input, submit, stop, suggestion chips
    StageIndicator.tsx       understanding → checking → querying → explaining
  answer/
    AnswerCard.tsx           narration + confidence chip + notes
    BreakdownTable.tsx       sortable, sticky header, tabular-nums
    ClarifyCard.tsx          ← the two-button moment, with real numbers
    ProvenancePanel.tsx      collapsible: spec JSON · SQL · rows · timing
    ExportButton.tsx
    VerifiedBadge.tsx        "5 figures traced to source"
  coverage/
    CoverageBanner.tsx       "Data covers 2024-09-01 → 2026-08-29"

lib/
  sse.ts                     POST + ReadableStream reader (NOT EventSource)
  api.ts                     typed client
  types.ts                   GENERATED from pydantic — never hand-written
  format.ts                  currency, dates, tabular numerals
  store.ts                   Zustand: threads, messages, pending clarify
```

### The streaming client

`EventSource` cannot do POST, so use `fetch` + `ReadableStream`:

```ts
export async function* streamAsk(body: AskRequest, signal: AbortSignal) {
  const res = await fetch(`${API}/api/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) throw new ApiError(res.status);

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += value;
    const frames = buf.split("\n\n");
    buf = frames.pop() ?? "";
    for (const f of frames) yield parseSSE(f);   // → typed union
  }
}
```

Consumed by a reducer whose actions *are* the SSE event union — so a new backend event type surfaces as a TypeScript exhaustiveness error rather than silent breakage.

### Render sequence the user actually sees

```
0.0s   ▸ "Understanding your question…"
0.4s   ▸ spec arrives    → provenance panel populates (collapsed)
0.5s   ▸ "Checking coverage…"
0.7s   ▸ EITHER clarify card (stop, await click)
             OR "Running query…"
0.8s   ▸ sql arrives     → provenance panel shows SQL
0.9s   ▸ rows arrive     → TABLE RENDERS, numbers on screen
1.0s   ▸ narration streams in above the table
1.8s   ▸ verified badge  → "5 figures traced to source"
```

Numbers on screen at ~0.9 s. The prose catching up afterwards is what sells "instant".

### Type generation — do this, it prevents a classic failure

```bash
python -m app.schema_export > shared/schema.json   # pydantic → JSON Schema
npx json-schema-to-typescript shared/schema.json -o web/lib/types.ts
```

Wire it into `npm run dev`. With three people on a 36-hour clock, a silently drifted `QuerySpec` between backend and frontend is the single most likely integration bug, and this eliminates the category.

---

## 5. Additional Postgres objects

Beyond the 7 dataset tables:

```sql
-- Entity resolution (§1)
CREATE TABLE semantic_index (...);

-- Conversation persistence. Doubles as the "sample questions and answers"
-- submission deliverable — export it at the end instead of writing it by hand.
CREATE TABLE chat_threads (
    thread_id   TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ DEFAULT now(),
    settled     JSONB DEFAULT '{}'      -- sticky ambiguity resolutions
);

CREATE TABLE chat_messages (
    message_id  TEXT PRIMARY KEY,
    thread_id   TEXT REFERENCES chat_threads(thread_id),
    role        TEXT,                   -- 'user' | 'assistant'
    question    TEXT,
    spec        JSONB,
    sql         TEXT,
    result      JSONB,
    narration   TEXT,
    verified    BOOLEAN,
    confidence  TEXT,
    latency_ms  INTEGER,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Every query. Feeds the deck's accuracy/latency numbers and the eval harness.
CREATE TABLE query_log (
    id            BIGSERIAL PRIMARY KEY,
    thread_id     TEXT,
    question      TEXT,
    spec          JSONB,
    sql           TEXT,
    row_count     INTEGER,
    sql_ms        INTEGER,
    llm_extract_ms INTEGER,
    llm_narrate_ms INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    model         TEXT,
    verified      BOOLEAN,
    clarified     BOOLEAN,
    created_at    TIMESTAMPTZ DEFAULT now()
);
```

`query_log` is worth more than it looks: at hour 30 it's where "p50 latency 840 ms, 98% of figures verified, $0.004 per question" comes from for the deck — measured, not asserted.

**Optional speed win:** materialise `v_monthly_spend` as `mv_monthly_spend` with indexes. Data is static during the hackathon, so no refresh logic is needed. Only do this if profiling shows the view is slow — don't pre-optimise.

---

## 6. Repository layout

```
TBX/
  data/  scripts/               ← done
  PLAN.md  AMBIGUITY.md  FLOW.md  PRD.md

  api/                          FastAPI
    main.py                     app, CORS, lifespan (registry boot)
    schema.py                   QuerySpec ← THE CONTRACT
    registry.py                 SemanticRegistry (DB introspection)
    extract.py                  LLM call #1
    resolve/
      entities.py               trgm + vector hybrid
      ambiguity.py              detectors + probe + policy
      coverage.py
    compile.py                  QuerySpec → SQL
    execute.py                  asyncpg pool
    narrate.py                  LLM call #2 + provenance check
    llm/
      base.py                   complete(prompt, schema) -> dict
      ollama.py  anthropic.py
    routes/
      ask.py  clarify.py  export.py  meta.py
    sql/                        migrations: semantic_index, chat_*, query_log

  web/                          Next.js
    app/  components/  lib/

  eval/
    questions.yaml              50 questions + SQL ground truth
    run.py                      harness
    bakeoff.py                  model comparison table

  shared/schema.json            generated, gitignored
```

---

## 7. Build order for today

Dependency-ordered. Steps 1–3 unblock everyone else.

| # | Task | Why first |
|---|---|---|
| 1 | `schema.py` — freeze `QuerySpec` | Everything depends on it |
| 2 | SSE event union + `schema_export` → `types.ts` | Frontend unblocked |
| 3 | `main.py` with `/api/ask` returning a **canned** event stream | Frontend builds against real streaming immediately |
| 4 | `registry.py` — introspect the live DB | Proves dataset-independence today |
| 5 | `compile.py` + `execute.py` | Real numbers flowing |
| 6 | `extract.py` + Ollama client | First end-to-end answer |
| 7 | Frontend: Chat, streaming, table, provenance panel | Parallel with 4–6 |
| 8 | `ambiguity.py` + fixtures pinning the 4 measured spreads | The differentiator |
| 9 | `narrate.py` + provenance check | Closes the grounding loop |
| 10 | pgvector stage 2 behind the flag | Cuttable |
| 11 | `eval/` harness + bake-off | Feeds the deck |

**Milestone to hit today:** step 6 — one real question answered end to end against Postgres. Everything after that is quality, and quality is much easier to add tomorrow than a working spine.

---

## 8. What changes tomorrow when the real dataset lands

| Component | Change |
|---|---|
| `scripts/02_normalize.py` | New normalizer emitting the same 7 CSVs |
| `registry.py` | **Nothing** — introspects whatever is there |
| `schema.py` | Dimension/Metric literals may gain or lose members |
| `compile.py` | Column-name map only |
| `resolve/ambiguity.py` | **Nothing** — detectors are registry-driven |
| `semantic_index` | One rebuild command |
| Frontend | **Nothing** — contract unchanged |

Budget: **2 hours.** Everything else is dataset-independent by construction.

---

## 9. Decisions I need you to confirm

1. **pgvector: yes, staged** — stage 1 trgm-only, stage 2 vectors behind a flag. Or do you want to skip vectors entirely and rely on trgm + LLM-side synonym expansion?

2. **Browser → FastAPI directly**, no Next.js proxy. Agreed? (Simpler streaming; the tradeoff is the backend URL is public.)

3. **Local embeddings** via `sentence-transformers` (~90 MB download, no API cost). Fine, or would you rather not add the Python ML dependency?

4. **Postgres for session state** (`chat_threads` / `chat_messages`) rather than in-memory. Costs ~20 lines and gives you the sample-questions deliverable as a free export. Agreed?

5. **Model for today's build** — I'd default to Ollama + Qwen2.5-7B-Instruct so you can iterate free and unmetered, with the Anthropic client written but unused until the bake-off. Do you have Ollama installed, or should the primary path be the API?

6. **Team split** — you said 2–3 people earlier but "I'll build it" now. Is today solo? That changes the build order (I'd drop the parallel-workstream framing and sequence it as one critical path).

Confirm or correct these six and I'll write the full PRD.
