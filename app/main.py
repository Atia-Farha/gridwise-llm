"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.router import router

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — replaces deprecated on_event
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("GridWise LLM service starting up…")
    try:
        from ortools.linear_solver import pywraplp
        solver = pywraplp.Solver.CreateSolver("GLOP")
        if solver:
            logger.info("OR-Tools GLOP solver pre-warmed successfully.")
    except Exception as exc:
        logger.error("OR-Tools pre-warm failed: %s", exc)

    try:
        from app.services.llm_interpreter import get_client
        get_client()
        logger.info("Gemini client initialised.")
    except Exception as exc:
        logger.error("Gemini client init failed: %s", exc)

    logger.info("GridWise LLM service ready.")
    yield
    # Shutdown (nothing to clean up)

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="GridWise LLM — Smart Campus Energy Optimizer",
    description=(
        "LLM-assisted operator-note interpretation + linear-programming "
        "energy scheduler for the BUP CSE Fest 2026 Hackathon."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.include_router(router)


# ---------------------------------------------------------------------------
# Custom error handlers — never expose secrets or raw stack traces
# ---------------------------------------------------------------------------

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return 422 for structurally valid but semantically invalid requests."""
    def _make_json_safe(obj):
        """Recursively convert any non-JSON-serializable value to string."""
        if isinstance(obj, dict):
            return {k: _make_json_safe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_make_json_safe(v) for v in obj]
        if isinstance(obj, (str, int, float, bool, type(None))):
            return obj
        return str(obj)

    return JSONResponse(
        status_code=422,
        content={"detail": _make_json_safe(exc.errors())},
    )


@app.exception_handler(Exception)
async def general_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    logger.exception("Unhandled exception on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected internal error occurred."},
    )



