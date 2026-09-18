"""FastAPI router — exposes GET /health and POST /optimize-energy."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.models.request import ScenarioRequest
from app.models.response import HealthResponse, OptimizationResponse
from app.services.orchestrator import run_pipeline
from app.utils.exceptions import GridWiseError, OptimizationError

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Readiness endpoint required by the judge harness."""
    return HealthResponse(status="ok")


@router.post("/optimize-energy", response_model=OptimizationResponse)
async def optimize_energy(request: ScenarioRequest) -> OptimizationResponse:
    """Main endpoint: interpret operator notes + produce 24-hour energy schedule."""
    logger.info("Received scenario: %s", request.scenario_id)
    try:
        return run_pipeline(request)
    except OptimizationError as exc:
        logger.error("Optimization failed for %s: %s", request.scenario_id, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Optimization failed — no feasible solution found."},
        )
    except GridWiseError as exc:
        logger.error("Pipeline error for %s: %s", request.scenario_id, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal pipeline error."},
        )
    except Exception as exc:
        logger.exception("Unexpected error for %s: %s", request.scenario_id, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "An unexpected error occurred."},
        )
