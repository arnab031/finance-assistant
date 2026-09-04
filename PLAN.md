# Build Plan — 36 Hours, 3 People

TBX / BVP Tech Catalyst — *Build a Finance Assistant That Actually Understands You*

Stack: **Python + FastAPI backend, React frontend, PostgreSQL.**
Dataset: already built — see [README.md](README.md) and [data/DATA_DICTIONARY.md](data/DATA_DICTIONARY.md).

---

## 1. The one decision that determines your score

**Do not build text-to-SQL.** Build **extract → validate → compile → execute → narrate**.

The problem statement is unusually explicit about this:

> *"filter, group, and aggregate the data correctly **before** handing results to the language model, so the model explains a computed result rather than calculating one itself."*

That sentence rules out the obvious architecture. Two options:

|                              | Text-to-SQL                    | **Extract → compile (chosen)**                           |
| ---------------------------- | ------------------------------ | --------------------------------------------------------------- |
| LLM produces                 | Free-form SQL                  | A small typed JSON object                                       |
| Can it emit invalid SQL?     | Yes                            | **No** — SQL is compiled by your Python, never generated |
| Can it hallucinate a number? | Yes                            | **No** — arithmetic happens only in Postgres             |
| Small-model reliability      | Poor — SQL generation is hard | **Good** — constrained extraction is easy                |
| Guardrails attach where?     | After generation, awkwardly    | **Before compute**, on a typed object                     |
| Explainability               | Show raw SQL                   | Show the spec*and* the SQL                                    |

The second column is why a 7B model can win this. You are asking the model to do **extraction**, not **generation** — and extraction into a fixed schema is exactly what small models are good at. That single reframe converts the 20% model-efficiency criterion from a constraint into an advantage.

### The pipeline

```
user question
    │
    ├─▶ LLM call #1 ──▶ QuerySpec (JSON, schema-constrained)
    │                        │
    │                   ┌────▼────────────────────────┐
    │                   │ validate  (pydantic)        │  ← guardrails live here
    │                   │ coverage  (v_data_coverage) │
    │                   │ ambiguity (clarify?)        │
    │                   └────┬────────────────────────┘
    │                        │
    │                   compile to SQL   ← pure Python. No LLM. Always valid.
    │                        │
    │                   execute on Postgres   ← NUMERIC(18,2), exact arithmetic
    │                        │
    │                   result rows + provenance
    │                        │
    └─▶ LLM call #2 ──▶ narration of those rows  ← told explicitly: never compute
                             │
                        numeric provenance check   ← every number must exist in the rows
                             │
                        answer + breakdown table + "how I got this"
```

**Exactly two model calls per question**, both with short prompts. That is your model-efficiency story, and it is measurable.

---

## 2. The contract — freeze this first

`QuerySpec` is the interface between all three workstreams. **Nobody starts real work until it is frozen** (hour 2). Once frozen, the engine, the UI, and the eval harness can be built in parallel against it without anyone blocking.

```python
Dimension = Literal["vendor", "category", "account", "department",
                    "fund", "month", "quarter", "payment_status",
                    "reconciliation_status"]

Metric = Literal["amount_paid", "amount_total", "amount_pending",
                 "amount_retainage", "txn_count", "vendor_count"]

class Period(BaseModel):
    start: date
    end: date                    # half-open: [start, end)

class Filters(BaseModel):
    vendor_ids:  list[str] = []
    departments: list[str] = []
    categories:  list[str] = []
    payment_status:        list[str] = []
    reconciliation_status: list[str] = []
    min_amount: Decimal | None = None

class QuerySpec(BaseModel):
    intent: Literal["aggregate", "list", "compare",
                    "reconcile", "anomaly", "clarify", "unsupported"]
    metric:   Metric = "amount_paid"
    group_by: list[Dimension] = []
    filters:  Filters = Filters()

    # The $1.34B fork. Explicit, never inferred silently. See §4.
    date_basis: Literal["payment_date", "fiscal_year"] = "payment_date"
    period:         Period | None = None
    compare_period: Period | None = None

    sort_desc: bool = True
    limit: int = Field(default=20, le=200)

    # Populated by the extractor for the "how I got this" panel
    reasoning: str = ""
    ambiguities: list[str] = []
```

Three properties make this work:

1. **Every field is a closed set.** A hallucinated dimension fails validation instead of reaching the database.
2. **`date_basis` is mandatory and explicit.** The model must commit to an interpretation, which makes the ambiguity detectable rather than silent.
3. **It serializes.** Show it in the UI verbatim and explainability is free.

---

## 3. The SQL compiler

Pure Python, no LLM, ~150 lines. Maps `QuerySpec` → parameterised SQL.

```python
def compile(spec: QuerySpec) -> tuple[str, dict]:
    metric_sql = {
        "amount_paid":   "SUM(t.amount_paid)",
        "amount_total":  "SUM(t.amount_total)",
        "txn_count":     "COUNT(*)",
        "vendor_count":  "COUNT(DISTINCT t.vendor_id)",
        # ...
    }[spec.metric]

    dims = {
        "vendor":     ("t.vendor_name",    "vendor"),
        "category":   ("t.category_name",  "category"),
        "month":      ("date_trunc('month', t.transaction_date)::date", "month"),
        # ...
    }
    ...
```

Two rules that matter:

- **Date filters compile to range predicates**, never `substr()`. `transaction_date >= %(start)s AND transaction_date < %(end)s` uses `idx_txn_date` (18 ms); the `substr()` form scans all 1M rows (512 ms). Measured — see [README](README.md#notes-for-building-the-assistant).
- **Vendor resolution happens before compile.** Free text → `pg_trgm` similarity lookup against `vendors` → concrete `vendor_ids`. If the top match is weak or there are several close matches, return `intent="clarify"` instead of guessing.

---

## 4. Guardrails — four layers, in order

Accuracy & grounding is 30% of the score, the largest single slice. These four layers are how you earn it.

**Layer 1 — Schema validation.** Pydantic rejects any spec with an unknown dimension, metric, status, or malformed period. Costs nothing, catches the majority of extraction errors.

**Layer 2 — Coverage check.** Query `v_data_coverage` before executing. If the requested period falls outside 2024-09-01 → 2026-08-29, answer *"I don't have data for that period — coverage runs from X to Y"*. **Never return `$0.00`.** Zero and unknown are different answers, and the guardrail question in [data/SAMPLE_QUESTIONS.md](data/SAMPLE_QUESTIONS.md) tests exactly this.

**Layer 3 — Ambiguity detection.** Return `intent="clarify"` when:

- the question says "FY2026" or "last year" and `date_basis` is genuinely ambiguous — the two readings differ by **$1.34 billion** ($16.82B by `fiscal_year` column vs $18.16B by date window)
- a vendor string matches several vendors above threshold
- "spend" could mean `amount_paid` or `amount_total` in context

Asking a good clarifying question scores better than a confident wrong number, and it directly demonstrates the hallucination-guardrail requirement.

**Layer 4 — Numeric provenance check.** *This is the differentiator.* After the narration call, extract every number from the generated text and assert each one appears in the computed result rows (allowing for rounding and formatting). If a number appears that isn't in the data, the model invented it — regenerate once, then fall back to a deterministic template.

```python
def verify(narration: str, rows: list[dict]) -> bool:
    allowed = {norm(v) for r in rows for v in r.values() if is_num(v)}
    return all(norm(n) in allowed for n in extract_numbers(narration))
```

Roughly 30 lines. It converts "we prompted it not to hallucinate" into "hallucinated figures cannot reach the user." Put this in the deck — it is the most demo-able idea in the build.

---

## 5. Timeline

Three workstreams. **E** = Engine, **I** = Interface, **V** = Verification.

| Hours            | E — Engine                                                                                                              | I — Interface                                                                | V — Verification                                                                                        |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| **0–2**   | *All three together:* freeze `QuerySpec`, stub `POST /ask` + `GET /coverage`, repo skeleton, agree JSON envelope |                                                                               |                                                                                                          |
| **2–10**  | Extractor prompt + structured output; SQL compiler for`aggregate`, `list`, `compare`, `reconcile`                | Chat shell, streaming, message state, results table, loading/error states     | Write the 50-question eval set with expected answers**computed in SQL**; wire Ollama + API clients |
| **10–18** | Guardrail layers 1–4; narration prompt; vendor resolution via`pg_trgm`                                                | "How I got this" panel (spec + SQL + row count), CSV export, multi-turn state | Run the bake-off,**pick the model**, draft deck outline                                            |
| **18–26** | Multi-turn spec patching*(pair with I)*                                                                                | Multi-turn UX, clarify-question flow                                          | Run full eval, triage failures, feed fixes back to E                                                     |
| **26–32** | **FEATURE FREEZE at hour 26.** Only bugfixes past this line.                                                       |                                                                               | Bonus: anomaly callouts (SQL already written), confidence signalling. Deck + architecture diagram.       |
| **32–36** | Rehearse demo ×3, record fallback video, finalize README, buffer                                                        |                                                                               |                                                                                                          |

Two non-negotiables: **the contract is frozen at hour 2**, and **features freeze at hour 26**. Hackathons are lost in the last six hours, almost always to something started at hour 28.

### Multi-turn, cheaply

Don't re-parse the whole conversation. Keep the last `QuerySpec` in session state and have call #1 emit a **patch**:

> *"How does that compare to the month before?"* → `{compare_period: {...}}` merged onto the previous spec.

Far more reliable than full re-extraction, and much cheaper in tokens.

---

## 6. Model bake-off

You are undecided — so decide with numbers, and the "short note on model choice" bonus writes itself.

**Candidates**

| Model                 | Access        | Notes                                                                                                                  |
| --------------------- | ------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `claude-haiku-4-5`  | Anthropic API | $1.00 / $5.00 per 1M in/out, 200K context. Strong instruction-following, likely the accuracy ceiling of the cheap tier |
| Qwen2.5-7B-Instruct   | Ollama, local | Very strong at constrained JSON for its size. Free, unlimited iteration                                                |
| Llama-3.1-8B-Instruct | Ollama, local | Baseline open-weight comparison                                                                                        |

**Measure per model, on the same 50 questions**

| Metric                        | Why it matters                                              |
| ----------------------------- | ----------------------------------------------------------- |
| Spec exact-match %            | Isolates extraction quality from narration quality          |
| End-to-end numeric accuracy % | The headline number                                         |
| Guardrail behaviour %         | Did it correctly clarify / refuse on the 10 trap questions? |
| p50 / p95 latency             | "Answers instantly" is in the spec                          |
| Tokens per question           | The efficiency argument                                     |
| $ per 1,000 questions         | Makes it concrete for judges                                |

**Expected result, and the story to tell:** because the model only extracts JSON and narrates pre-computed rows, a 7B local model should land close to Haiku on accuracy. If it does, say so plainly — *"we chose a 7B local model because our architecture made the model's job easy enough that a small one suffices, and here is the measured table proving it."* That is a far stronger answer to the 20% criterion than picking a small model and hoping.

Build behind a one-method interface (`complete(prompt, schema) -> dict`) so switching is a config change, not a refactor.

---

## 7. Eval set — build it at hour 2, not hour 30

50 questions, expected answers computed **in SQL** so the answer key is ground truth, not opinion. The 12 in [data/SAMPLE_QUESTIONS.md](data/SAMPLE_QUESTIONS.md) are already done — extend to 50.

| Category                                             | Count | Graded on             |
| ---------------------------------------------------- | ----- | --------------------- |
| Simple aggregate                                     | 8     | Exact numeric match   |
| Filtered (status / dept / category)                  | 6     | Exact numeric match   |
| Grouped breakdown                                    | 6     | Row set + values      |
| Comparative (MoM, QoQ)                               | 6     | Both periods + delta  |
| Reconciliation                                       | 8     | Exact numeric match   |
| Multi-turn follow-up                                 | 6     | Correct spec patch    |
| **Ambiguous → must clarify**                  | 5     | Behaviour, not number |
| **Out-of-range / unanswerable → must refuse** | 5     | Behaviour, not number |

Those last 10 are worth as much as the other 40. An assistant that confidently answers an unanswerable question fails the single heaviest criterion, and judges test for it deliberately.

---

## 8. Rubric mapping

| Criterion            | Weight        | What earns it here                                                                                              |
| -------------------- | ------------- | --------------------------------------------------------------------------------------------------------------- |
| Accuracy & grounding | **30%** | Deterministic SQL compile; arithmetic only in Postgres`NUMERIC`; 4 guardrail layers; numeric provenance check |
| Model efficiency     | **20%** | 2 short calls per question; extraction not generation; measured bake-off table justifying a small model         |
| NL understanding     | 15%           | Spec extraction quality;`pg_trgm` vendor resolution; multi-turn patching                                      |
| Functionality        | 15%           | 4 intents, breakdowns, CSV export, multi-turn, anomaly callouts                                                 |
| User experience      | 10%           | Streaming chat, breakdown tables, "how I got this" panel, clarify flow                                          |
| Presentation         | 5%            | Deck + architecture diagram + rehearsed demo                                                                    |
| Business impact      | 5%            | Frame as: finance ops stops fielding repeat lookups; every answer is auditable                                  |

---

## 9. Risks

| Risk                                            | Mitigation                                                                                                                                                                                                           |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Organizers' dataset differs from ours** | Highest-probability risk. The loader is already thin — write a new normalizer emitting the same 7 tables and nothing downstream changes. Budget 2 hours. Keep every query behind the schema in the data dictionary. |
| Live demo fails                                 | Record a full run at hour 32 as fallback. Never demo against a cold database.                                                                                                                                        |
| Scope creep past hour 26                        | Hard freeze. Cut list, in order: anomaly callouts → confidence signalling → CSV export → multi-turn.                                                                                                              |
| Small model can't hold the schema               | Use structured output / JSON mode. If quality is short at hour 12, switch to Haiku and keep the local numbers as the comparison table — the bake-off is still a deliverable either way.                             |
| Vendor resolution wrong-matches                 | 48 vendor groups share surface forms. Always show which vendor(s) matched, so a wrong match is visible rather than silent.                                                                                           |

---

## 10. Submission checklist

- [ ] Working prototype — chat UI + FastAPI backend
- [ ] Architecture diagram — the pipeline in §1
- [ ] README with setup instructions
- [ ] Sample questions + produced answers — extend [data/SAMPLE_QUESTIONS.md](data/SAMPLE_QUESTIONS.md) with actual assistant output beside the SQL ground truth
- [ ] Deck — problem, approach, **model choice rationale with the measured table**, demo flow
- [ ] *Good to have:* CSV/Excel export of any breakdown
- [ ] *Bonus:* confidence signalling · model-choice note · anomaly callouts

---

## First three hours, concretely

1. All three read [data/DATA_DICTIONARY.md](data/DATA_DICTIONARY.md) — especially the `fiscal_year` warning and the gotcha list.
2. Freeze `QuerySpec` in `app/schema.py`. Everyone reviews. **No changes after hour 2 without all three agreeing.**
3. Stub `POST /ask` returning a hardcoded response in the final envelope shape, so **I** can build the whole UI against it immediately.
4. **V** writes eval questions 13–50 while **E** and **I** build. Ground truth in SQL, in the repo, from the start.
