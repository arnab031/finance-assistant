"""FastAPI app. Lifespan opens the pool, runs migrations, and boots the registry."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.config import settings
from api.db import db
from api.llm.base import get_llm
from api.registry import SemanticRegistry
from api.routes import ask as ask_routes
from api.routes import ops as ops_routes

log = logging.getLogger("tbx")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    applied = await db.migrate()
    log.info("migrations applied: %s", ", ".join(applied))

    app.state.registry = await SemanticRegistry.build(db)
    app.state.pending = {}   # ambiguity_id -> Ambiguity, awaiting a choice
    app.state.llm = get_llm()
    # Pay the model's cold start at boot, not on the first question.
    if hasattr(app.state.llm, "warm"):
        await app.state.llm.warm()

    log.info("ready on %s", settings.database_url.rsplit("/", 1)[-1])
    yield
    await app.state.llm.close()
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
        "currency": "USD", "vendor_count": r.vendor_count,
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


@app.get("/api/health")
async def health() -> JSONResponse:
    """Phase 0 acceptance test. Checks every external dependency."""
    checks: dict[str, object] = {}

    try:
        checks["database"] = {
            "ok": True,
            "transactions": await db.scalar("SELECT COUNT(*) FROM transactions"),
        }
    except Exception as exc:  # noqa: BLE001 - health must never raise
        checks["database"] = {"ok": False, "error": str(exc)}

    for ext in ("pg_trgm", "vector"):
        try:
            v = await db.scalar(
                "SELECT extversion FROM pg_extension WHERE extname = %(n)s", {"n": ext}
            )
            checks[ext] = {"ok": v is not None, "version": v}
        except Exception as exc:  # noqa: BLE001
            checks[ext] = {"ok": False, "error": str(exc)}

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
