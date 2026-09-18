"""FastAPI router — exposes GET /health and POST /optimize-energy."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.models.request import ScenarioRequest
from app.models.response import HealthResponse, OptimizationResponse
from app.services.orchestrator import run_pipeline
from app.utils.exceptions import GridWiseError

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    """Readiness endpoint required by the judge harness.

    Deliberately dependency-free: it reports that the HTTP service is up,
    never that a third-party model provider is up. Gating readiness on an
    external API would let a provider blip mark the service unhealthy.
    """
    return HealthResponse(status="ok")


@router.post(
    "/optimize-energy",
    response_model=OptimizationResponse,
    tags=["optimization"],
)
async def optimize_energy(request: ScenarioRequest):
    """Interpret operator notes and return the optimal 24-hour schedule."""
    logger.info("Received scenario: %s", request.scenario_id)
    try:
        return await run_pipeline(request)
    except GridWiseError as exc:
        # The pipeline relaxes infeasible directives internally, so reaching
        # this branch means a genuine internal fault, not a hard scenario.
        logger.error("Pipeline error for %s: %s", request.scenario_id, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal pipeline error."},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error for %s: %s", request.scenario_id, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "An unexpected error occurred."},
        )
