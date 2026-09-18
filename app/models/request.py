"""Pydantic request models for POST /optimize-energy."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
        # A battery sitting below its policy floor is unusual but physically
        # real, and end-of-day neutrality pins the final state to that same
        # level. The optimizer honours the starting level as the floor rather
        # than rejecting the scenario outright, so it is deliberately not
        # validated here — rejecting it would forfeit an otherwise solvable case.
        return self


# A realistic, known-good scenario used as the OpenAPI example.
#
# Without an explicit example, Swagger UI synthesises one from the schema: it
# takes the boundary value for `hour` (23, from le=23) and repeats that single
# entry 24 times to satisfy minItems. The result fails the uniqueness rule, so
# anyone clicking "Try it out" gets a 422 on their very first request. Shipping
# a valid example makes the interactive docs work out of the box.
_EXAMPLE_HOURS: list[tuple[int, float, float, float]] = [
    (0, 90, 0, 6), (1, 85, 0, 6), (2, 80, 0, 5), (3, 80, 0, 5),
    (4, 85, 0, 5), (5, 95, 0, 6), (6, 110, 5, 8), (7, 130, 20, 10),
    (8, 150, 50, 12), (9, 165, 90, 14), (10, 175, 130, 16), (11, 180, 160, 16),
    (12, 185, 180, 15), (13, 180, 170, 14), (14, 170, 140, 13), (15, 165, 90, 14),
    (16, 170, 45, 18), (17, 185, 10, 22), (18, 205, 0, 28), (19, 215, 0, 30),
    (20, 205, 0, 26), (21, 175, 0, 18), (22, 135, 0, 10), (23, 105, 0, 7),
]

_REQUEST_EXAMPLE = {
    "scenario_id": "GRID-DEMO-01",
    "operator_notes": [
        "Facilities will wash the rooftop solar panels from noon until 2 PM. "
        "During cleaning, usable solar should be treated as roughly 25% of the forecast.",
        "The sports office moved next month's registration deadline.",
    ],
    "hours": [
        {
            "hour": hour,
            "demand_kwh": demand,
            "solar_kwh": solar,
            "tariff_bdt_per_kwh": tariff,
        }
        for hour, demand, solar, tariff in _EXAMPLE_HOURS
    ],
    "battery": {
        "capacity_kwh": 220,
        "initial_energy_kwh": 110,
        "minimum_energy_kwh": 40,
        "max_charge_kwh_per_hour": 50,
        "max_discharge_kwh_per_hour": 50,
    },
}


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [_REQUEST_EXAMPLE]})

    scenario_id: str = Field(..., min_length=1)
    operator_notes: list[str] = Field(..., min_length=1, max_length=3)
    hours: list[HourEntry] = Field(
        ...,
        min_length=24,
        max_length=24,
        description="Exactly 24 entries, one per hour, covering 0 through 23 with no duplicates.",
    )
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
