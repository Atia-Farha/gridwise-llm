"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.router import router
from app.config import settings

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warm up slow first-call paths so the first judge request isn't the one
    that pays for them. Every step is best-effort: startup must not fail."""
    logger.info("GridWise service starting up…")

    try:
        from ortools.linear_solver import pywraplp

        if pywraplp.Solver.CreateSolver("GLOP"):
            logger.info("OR-Tools GLOP solver pre-warmed.")
    except Exception as exc:  # noqa: BLE001
        logger.error("OR-Tools pre-warm failed: %s", type(exc).__name__)

    if settings.openai_api_key:
        try:
            from app.services.llm_interpreter import get_client

            get_client()
            logger.info("OpenAI client initialised (model=%s).", settings.openai_model)
        except Exception as exc:  # noqa: BLE001
            logger.error("OpenAI client init failed: %s", type(exc).__name__)
    else:
        logger.warning(
            "OPENAI_API_KEY is not set — operator notes will degrade to no_op."
        )

    logger.info("GridWise service ready.")
    yield

    from app.services import cache

    await cache.close()
    logger.info("GridWise service stopped.")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="GridWise LLM — Smart Campus Energy Optimizer",
    description=(
        "LLM-assisted operator-note interpretation and linear-programming "
        "energy scheduler for the BUP CSE Fest 2026 Hackathon."
    ),
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.include_router(router)


# ---------------------------------------------------------------------------
# Error handlers — never expose secrets or raw stack traces
# ---------------------------------------------------------------------------

def _json_safe(obj):
    """Recursively convert non-JSON-serialisable values to strings."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    return str(obj)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Split malformed syntax from well-formed-but-invalid content.

    The Problem Statement (§6.1) reserves 400 for malformed JSON and a
    structurally invalid request, and 422 for a well-formed request that
    fails semantic validation. FastAPI returns 422 for both by default, so
    unparseable bodies are re-mapped here.
    """
    errors = exc.errors()
    malformed = any(e.get("type") == "json_invalid" for e in errors)

    # Keep only what identifies the problem. Pydantic's default entry carries
    # an `input` field holding the entire offending payload, which echoes a
    # full 24-hour scenario back on every validation failure.
    detail = [
        {
            "type": e.get("type"),
            "loc": list(e.get("loc", [])),
            "msg": e.get("msg"),
        }
        for e in errors
    ]
    return JSONResponse(
        status_code=400 if malformed else 422,
        content={"detail": _json_safe(detail)},
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected internal error occurred."},
    )
