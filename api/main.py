"""FastAPI app. Lifespan opens the pool, runs migrations, and boots the registry."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.config import settings
from api.db import db, json_column
from api.llm.base import close_model_clients, get_llm
from api.narration import ensure_counterparty
from api.registry import SemanticRegistry
from api.semantic import ensure_semantic_index
from api.routes import ask as ask_routes
from api.routes import ops as ops_routes

log = logging.getLogger("tbx")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    applied = await db.migrate()
    log.info("migrations applied: %s", ", ".join(applied))

    filled = await ensure_counterparty(db)
    if filled:
        log.info("counterparty backfilled for %d transactions", filled)

    # Built on boot rather than by a script somebody has to remember to run.
    # Fingerprinted, so an unchanged vocabulary costs one aggregate query.
    app.state.semantic = await ensure_semantic_index(db)
    for kind, outcome in app.state.semantic.items():
        log.info("semantic index[%s]: %s", kind, outcome)

    app.state.registry = await SemanticRegistry.build(db)
    app.state.pending = {}   # ambiguity_id -> Ambiguity, awaiting a choice
    app.state.llm = get_llm()
    # Pay the model's cold start at boot, not on the first question.
    if hasattr(app.state.llm, "warm"):
        await app.state.llm.warm()

    log.info("ready on %s", settings.database_url.rsplit("/", 1)[-1])
    yield
    await app.state.llm.close()
    await close_model_clients()
    await db.close()


app = FastAPI(
    title="Finsight",
    description="Grounded answers from your ledger. Every figure computed in SQL "
                "and verified against the source rows before it is shown.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_buffering(request, call_next):
    """SSE dies behind buffering proxies. Say so on every response."""
    response = await call_next(request)
    response.headers["X-Accel-Buffering"] = "no"
    return response


app.include_router(ask_routes.router)
app.include_router(ops_routes.router)


@app.get("/api/coverage")
async def coverage():
    r = app.state.registry
    return {
        "earliest": r.earliest, "latest": r.latest,
        "transaction_count": r.transaction_count, "total_paid": str(r.total_paid),
        "currency": "INR", "vendor_count": r.vendor_count,
        "categories": r.categories, "payment_statuses": r.payment_statuses,
        "reconciliation_statuses": r.recon_statuses,
    }


# Thresholds are stated here rather than buried in a dashboard, so the
# definition of "unhealthy" is reviewable. Each bound is set from behaviour this
# build actually exhibited, not from a round number.
THRESHOLDS = {
    "verification_pass_rate":  (">=", 0.95),   # figures traceable to the rows
    "template_fallback_rate":  ("<=", 0.10),   # model could not narrate cleanly
    "repair_rate":             ("<=", 0.10),   # extraction contradicting itself
    "coercion_rate":           ("<=", 0.02),   # two strict failures in a row
    "sanity_correction_rate":  ("<=", 0.15),   # model ignoring the prompt
    "clarify_rate":            ("<=", 0.35),   # above this it is nagging
    "empty_result_rate":       ("<=", 0.10),   # filters resolving to nothing
    # Confidence as the pipeline already labels it - over ANSWERED requests, so
    # refusals do not dilute it. medium_confidence_rate is deliberately absent:
    # it is the residual of the other two, and a threshold on it would fire
    # twice for the same underlying slip.
    "high_confidence_rate":    (">=", 0.85),   # most answers on the clean path
    "low_confidence_rate":     ("<=", 0.10),   # figures that could not be traced
    "error_rate":              ("<=", 0.02),
    "p95_ms":                  ("<=", 20000),
}


@app.get("/api/metrics")
async def metrics(hours: int = 24):
    """Rolling health signals, each with a pass/fail verdict.

    Correctness rates are computed over ANSWERED requests: a clarification or an
    out-of-coverage refusal is correct behaviour and must not dilute them.
    """
    row = await db.fetchone("SELECT * FROM v_health")
    if not row:
        return {"ok": True, "requests": 0, "signals": {}}

    signals, breaches = {}, []
    for name, (op, bound) in THRESHOLDS.items():
        value = row.get(name)
        if value is None:
            signals[name] = {"value": None, "status": "no_data"}
            continue
        value = float(value)
        healthy = value >= bound if op == ">=" else value <= bound
        signals[name] = {"value": value, "threshold": f"{op} {bound}",
                         "status": "ok" if healthy else "BREACH"}
        if not healthy:
            breaches.append(name)

    incidents = await db.fetch(
        "SELECT id, issue, LEFT(question,70) AS question, unverified, "
        "sanity_corrected, total_ms FROM v_incidents LIMIT 10")
    # The client's Incident type declares these as string[]; undecoded they
    # arrive as one long string and render as a run of characters.
    for inc in incidents:
        for key in ("unverified", "sanity_corrected"):
            inc[key] = json_column(inc.get(key), default=[])

    return {
        "ok": not breaches,
        "window_hours": hours,
        "requests": row["requests"],
        "answered": row["answered"],
        "breaches": breaches,
        "signals": signals,
        "latency": {"p50_ms": row["p50_ms"], "p95_ms": row["p95_ms"],
                    "avg_tokens": row["avg_tokens"]},
        "recent_incidents": [dict(i) for i in incidents],
    }


@app.get("/api/suggestions")
async def suggestions():
    """Starter questions for the active dataset. Served rather than hardcoded so
    the chat never offers a question the loaded schema cannot answer."""
    from api.profiles.base import get_profile

    prof = get_profile()
    return {"dataset": prof.name, "label": prof.label,
            "placeholder": prof.placeholder, "suggestions": prof.suggestions}


@app.get("/api/health")
async def health() -> JSONResponse:
    """Phase 0 acceptance test. Checks every external dependency."""
    checks: dict[str, object] = {}

    try:
        # Counted through the profile's fact source, not a hardcoded table name:
        # the stand-in calls it `transactions`, the organizers' schema calls it
        # `transaction`. Hardcoding made health report the database as down on a
        # perfectly healthy dataset, which then blocked the canary preflight.
        from api.profiles.base import get_profile

        prof = get_profile()
        checks["database"] = {
            "ok": True,
            "dataset": prof.name,
            "rows": await db.scalar(f"SELECT COUNT(*) FROM {prof.fact}"),
        }
    except Exception as exc:  # noqa: BLE001 - health must never raise
        checks["database"] = {"ok": False, "error": str(exc)}

    from api.crypto import key_fingerprint

    fp = key_fingerprint()
    checks["sensitive_key"] = {
        "ok": fp != "unset",
        "fingerprint": fp,
        "note": "account_number is tokenized; utr_number arrives pre-encrypted",
    }

    # Was a pg_trgm / pgvector probe. MySQL has neither, and the honest
    # replacement is not a rename: it is a check that the FULLTEXT indexes
    # standing in for trigram search actually exist, plus a flat statement that
    # vector search is unavailable.
    try:
        n_ft = await db.scalar(
            """SELECT COUNT(DISTINCT INDEX_NAME) FROM information_schema.STATISTICS
               WHERE TABLE_SCHEMA = DATABASE() AND INDEX_TYPE = 'FULLTEXT'"""
        )
        checks["fulltext_indexes"] = {
            "ok": bool(n_ft),
            "count": n_ft,
            "note": ("stands in for pg_trgm; matches whole words only, so "
                     "substring hits inside a longer token need LIKE"),
        }
    except Exception as exc:  # noqa: BLE001
        checks["fulltext_indexes"] = {"ok": False, "error": str(exc)}

    # An empty semantic_index with no explanation is the reason "why is it
    # empty?" got asked twice. Whatever the state, this says WHICH state and WHY.
    try:
        rows = await db.fetch(
            """SELECT m.entity_type, m.n_labels, m.embed_model, m.built_at,
                      COUNT(s.id) AS vectors
               FROM semantic_index_meta m
               LEFT JOIN semantic_index s
                      ON s.entity_type = m.entity_type AND s.embedding IS NOT NULL
               GROUP BY m.entity_type, m.n_labels, m.embed_model, m.built_at"""
        )
        checks["semantic_index"] = {
            # Off is a valid, deliberate state - not a fault. A red light for a
            # disabled feature trains everyone to ignore this endpoint.
            "ok": True,
            "enabled": settings.enable_semantic,
            "indexes": {r["entity_type"]: {"labels": r["n_labels"],
                                           "vectors": r["vectors"],
                                           "model": r["embed_model"],
                                           "built_at": str(r["built_at"])}
                        for r in rows},
            "note": ("built on boot from Profile.semantic_sources; searched in "
                     "Python because MySQL 8.4 has no vector type"
                     if settings.enable_semantic else
                     "empty by design: ENABLE_SEMANTIC=false, nothing builds or "
                     "reads it"),
        }
    except Exception as exc:  # noqa: BLE001
        checks["semantic_index"] = {"ok": False, "error": str(exc)}

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{settings.ollama_url}/api/tags")
            names = [m["name"] for m in r.json().get("models", [])]
        checks["ollama"] = {
            "ok": settings.ollama_model in names,
            "models": names,
            "want": settings.ollama_model,
        }
    except Exception as exc:  # noqa: BLE001
        checks["ollama"] = {"ok": False, "error": str(exc)}

    ok = all(isinstance(c, dict) and c.get("ok") for c in checks.values())
    return JSONResponse({"ok": ok, "checks": checks}, status_code=200 if ok else 503)
