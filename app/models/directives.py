"""Directive type definitions, enums, and structured adjustment models."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class DirectiveType(str, Enum):
    solar_reduction = "solar_reduction"
    minimum_battery_reserve = "minimum_battery_reserve"
    no_charge_window = "no_charge_window"
    no_discharge_window = "no_discharge_window"
    max_grid_window = "max_grid_window"
    no_op = "no_op"


class BatteryAction(str, Enum):
    charge = "charge"
    discharge = "discharge"
    idle = "idle"


# ---------------------------------------------------------------------------
# Structured adjustment shapes — used internally after guardrail validation
# ---------------------------------------------------------------------------

class SolarReductionAdjustment(BaseModel):
    hours: list[int]
    factor: float  # 0..1 — the remaining usable fraction


class MinimumBatteryReserveAdjustment(BaseModel):
    hours: list[int]
    minimum_energy_kwh: float


class NoChargeWindowAdjustment(BaseModel):
    hours: list[int]


class NoDischargeWindowAdjustment(BaseModel):
    hours: list[int]


class MaxGridWindowAdjustment(BaseModel):
    hours: list[int]
    max_grid_kwh: float


# ---------------------------------------------------------------------------
# Raw directive interpretation entry (from LLM or response)
# ---------------------------------------------------------------------------

class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: dict[str, Any] | None
    explanation: str

    @model_validator(mode="after")
    def validate_applies_semantics(self) -> "DirectiveInterpretation":
        if self.directive_type == DirectiveType.no_op:
            self.applies = False
            self.structured_adjustment = None
        else:
            self.applies = True
        return self


# ---------------------------------------------------------------------------
# Validated directive — post-guardrail, used by the optimizer
# ---------------------------------------------------------------------------

class ValidatedDirective(BaseModel):
    """A fully-validated directive ready to be injected into the LP."""
    directive_type: DirectiveType
    hours: list[int] = Field(default_factory=list)
    factor: float | None = None                   # solar_reduction
    minimum_energy_kwh: float | None = None       # minimum_battery_reserve
    max_grid_kwh: float | None = None             # max_grid_window
