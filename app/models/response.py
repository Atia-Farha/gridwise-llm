"""Pydantic response models for POST /optimize-energy."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.models.directives import BatteryAction, DirectiveType


class DirectiveInterpretationResponse(BaseModel):
    note_index: int
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: dict[str, Any] | None
    explanation: str


class HourlyPlanEntry(BaseModel):
    hour: int = Field(..., ge=0, le=23)
    grid_kwh: float = Field(..., ge=0)
    solar_used_kwh: float = Field(..., ge=0)
    battery_action: BatteryAction
    battery_kwh: float = Field(..., ge=0)
    battery_energy_after_kwh: float = Field(..., ge=0)


class OptimizationResponse(BaseModel):
    scenario_id: str
    directive_interpretation: list[DirectiveInterpretationResponse]
    hourly_plan: list[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str


class HealthResponse(BaseModel):
    status: str = "ok"
