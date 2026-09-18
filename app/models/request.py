"""Pydantic request models for POST /optimize-energy."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class HourEntry(BaseModel):
    hour: int = Field(..., ge=0, le=23)
    demand_kwh: float = Field(..., ge=0)
    solar_kwh: float = Field(..., ge=0)
    tariff_bdt_per_kwh: float = Field(..., ge=0)


class BatteryConfig(BaseModel):
    capacity_kwh: float = Field(..., gt=0)
    initial_energy_kwh: float = Field(..., ge=0)
    minimum_energy_kwh: float = Field(..., ge=0)
    max_charge_kwh_per_hour: float = Field(..., ge=0)
    max_discharge_kwh_per_hour: float = Field(..., ge=0)

    @model_validator(mode="after")
    def validate_battery_energy_bounds(self) -> "BatteryConfig":
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError(
                f"initial_energy_kwh ({self.initial_energy_kwh}) "
                f"exceeds capacity_kwh ({self.capacity_kwh})"
            )
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError(
                f"minimum_energy_kwh ({self.minimum_energy_kwh}) "
                f"exceeds capacity_kwh ({self.capacity_kwh})"
            )
        if self.initial_energy_kwh < self.minimum_energy_kwh:
            raise ValueError(
                f"initial_energy_kwh ({self.initial_energy_kwh}) "
                f"is below minimum_energy_kwh ({self.minimum_energy_kwh})"
            )
        return self


class ScenarioRequest(BaseModel):
    scenario_id: str = Field(..., min_length=1)
    operator_notes: list[str] = Field(..., min_length=1, max_length=3)
    hours: list[HourEntry] = Field(..., min_length=24, max_length=24)
    battery: BatteryConfig

    @model_validator(mode="after")
    def validate_notes_non_empty(self) -> "ScenarioRequest":
        for i, note in enumerate(self.operator_notes):
            if not note.strip():
                raise ValueError(f"operator_notes[{i}] must be a non-empty string")
        return self

    @model_validator(mode="after")
    def validate_hours_unique_and_ordered(self) -> "ScenarioRequest":
        hour_values = [h.hour for h in self.hours]
        if sorted(hour_values) != list(range(24)):
            raise ValueError(
                "hours must contain exactly 24 unique entries for hours 0 through 23"
            )
        return self
