"""Post-solve replay validator.

Independently replays the returned hourly_plan hour-by-hour and verifies
every GridWise rule plus every active directive constraint.  Raises
ReplayValidationError if any check fails.

This mirrors what the judge harness does, so catching it here lets us
return a 500 with a controlled message rather than silently returning a
bad schedule.
"""

from __future__ import annotations

import logging

from app.models.directives import BatteryAction, DirectiveType, ValidatedDirective
from app.models.request import BatteryConfig, ScenarioRequest
from app.models.response import HourlyPlanEntry
from app.utils.constants import NUMERIC_TOLERANCE
from app.utils.exceptions import ReplayValidationError

logger = logging.getLogger(__name__)

TOL = NUMERIC_TOLERANCE  # 0.01 kWh / BDT per spec §11.5


def _active_min_reserve(
    hour: int,
    base_min: float,
    directives: list[ValidatedDirective],
) -> float:
    result = base_min
    for d in directives:
        if (
            d.directive_type == DirectiveType.minimum_battery_reserve
            and hour in d.hours
            and d.minimum_energy_kwh is not None
        ):
            result = max(result, d.minimum_energy_kwh)
    return result


def replay_validate(
    plan: list[HourlyPlanEntry],
    request: ScenarioRequest,
    directives: list[ValidatedDirective],
    effective_solar: dict[int, float],
) -> None:
    """Replay the schedule and raise ReplayValidationError on any violation."""
    battery: BatteryConfig = request.battery
    hour_map = {h.hour: h for h in request.hours}

    # Mirrors the optimizer: a scenario starting below its stated minimum is
    # held to its starting level, since end-of-day neutrality pins E[23] there.
    base_floor = min(battery.minimum_energy_kwh, battery.initial_energy_kwh)

    E = battery.initial_energy_kwh  # running battery state

    for entry in plan:
        h = entry.hour
        demand = hour_map[h].demand_kwh
        eff_solar = effective_solar.get(h, 0.0)

        charge_kwh = entry.battery_kwh if entry.battery_action == BatteryAction.charge else 0.0
        discharge_kwh = entry.battery_kwh if entry.battery_action == BatteryAction.discharge else 0.0

        # 1. idle implies battery_kwh == 0
        if entry.battery_action == BatteryAction.idle and entry.battery_kwh > TOL:
            raise ReplayValidationError(
                f"hour {h}: battery_action=idle but battery_kwh={entry.battery_kwh}"
            )

        # 2. Energy balance: grid + solar_used + discharge = demand + charge
        lhs = entry.grid_kwh + entry.solar_used_kwh + discharge_kwh
        rhs = demand + charge_kwh
        if abs(lhs - rhs) > TOL:
            raise ReplayValidationError(
                f"hour {h}: energy balance violation — "
                f"lhs={lhs:.4f} rhs={rhs:.4f} diff={abs(lhs-rhs):.4f}"
            )

        # 3. Solar usage ≤ effective solar
        if entry.solar_used_kwh > eff_solar + TOL:
            raise ReplayValidationError(
                f"hour {h}: solar_used_kwh={entry.solar_used_kwh} "
                f"exceeds effective_solar={eff_solar}"
            )

        # 4. Non-negative values
        if entry.grid_kwh < -TOL:
            raise ReplayValidationError(f"hour {h}: negative grid_kwh={entry.grid_kwh}")
        if entry.solar_used_kwh < -TOL:
            raise ReplayValidationError(
                f"hour {h}: negative solar_used_kwh={entry.solar_used_kwh}"
            )
        if entry.battery_kwh < -TOL:
            raise ReplayValidationError(
                f"hour {h}: negative battery_kwh={entry.battery_kwh}"
            )

        # 5. Charge/discharge rate limits
        if charge_kwh > battery.max_charge_kwh_per_hour + TOL:
            raise ReplayValidationError(
                f"hour {h}: charge {charge_kwh} exceeds max {battery.max_charge_kwh_per_hour}"
            )
        if discharge_kwh > battery.max_discharge_kwh_per_hour + TOL:
            raise ReplayValidationError(
                f"hour {h}: discharge {discharge_kwh} exceeds max {battery.max_discharge_kwh_per_hour}"
            )

        # 6. Update battery state
        E += charge_kwh - discharge_kwh

        # 7. Battery bounds
        active_min = _active_min_reserve(h, base_floor, directives)
        if E < active_min - TOL:
            raise ReplayValidationError(
                f"hour {h}: battery_energy {E:.4f} below minimum {active_min}"
            )
        if E > battery.capacity_kwh + TOL:
            raise ReplayValidationError(
                f"hour {h}: battery_energy {E:.4f} exceeds capacity {battery.capacity_kwh}"
            )

        # 8. battery_energy_after_kwh reported value must match
        if abs(E - entry.battery_energy_after_kwh) > TOL:
            raise ReplayValidationError(
                f"hour {h}: battery_energy_after_kwh={entry.battery_energy_after_kwh} "
                f"but computed={E:.4f}"
            )

        # 9. Directive checks
        for d in directives:
            if h not in d.hours:
                continue
            if d.directive_type == DirectiveType.no_charge_window:
                if charge_kwh > TOL:
                    raise ReplayValidationError(
                        f"hour {h}: no_charge_window violated — charge={charge_kwh}"
                    )
            elif d.directive_type == DirectiveType.no_discharge_window:
                if discharge_kwh > TOL:
                    raise ReplayValidationError(
                        f"hour {h}: no_discharge_window violated — discharge={discharge_kwh}"
                    )
            elif (
                d.directive_type == DirectiveType.max_grid_window
                and d.max_grid_kwh is not None
            ):
                if entry.grid_kwh > d.max_grid_kwh + TOL:
                    raise ReplayValidationError(
                        f"hour {h}: max_grid_window violated — "
                        f"grid={entry.grid_kwh} cap={d.max_grid_kwh}"
                    )

    # 10. End-of-day battery neutrality
    if abs(E - battery.initial_energy_kwh) > TOL:
        raise ReplayValidationError(
            f"end-of-day battery neutrality violated — "
            f"final={E:.4f} initial={battery.initial_energy_kwh}"
        )

    logger.debug("Replay validation passed.")
