"""Optimizer unit tests — validates LP correctness without LLM calls."""

import pytest

from app.models.directives import DirectiveType, ValidatedDirective
from app.models.request import BatteryConfig, HourEntry, ScenarioRequest
from app.services.optimizer import solve
from app.utils.constants import NUMERIC_TOLERANCE

TOL = NUMERIC_TOLERANCE


def _make_scenario(
    demands: list[float],
    solar: list[float],
    tariffs: list[float],
    capacity: float = 200.0,
    initial: float = 100.0,
    min_energy: float = 20.0,
    max_charge: float = 50.0,
    max_discharge: float = 50.0,
) -> ScenarioRequest:
    assert len(demands) == len(solar) == len(tariffs) == 24
    return ScenarioRequest(
        scenario_id="OPT-TEST",
        operator_notes=["test note"],
        hours=[
            HourEntry(hour=h, demand_kwh=demands[h], solar_kwh=solar[h],
                      tariff_bdt_per_kwh=tariffs[h])
            for h in range(24)
        ],
        battery=BatteryConfig(
            capacity_kwh=capacity,
            initial_energy_kwh=initial,
            minimum_energy_kwh=min_energy,
            max_charge_kwh_per_hour=max_charge,
            max_discharge_kwh_per_hour=max_discharge,
        ),
    )


def _flat_scenario(demand: float = 100.0, solar: float = 0.0, tariff: float = 10.0,
                   **kwargs) -> ScenarioRequest:
    return _make_scenario(
        demands=[demand] * 24,
        solar=[solar] * 24,
        tariffs=[tariff] * 24,
        **kwargs,
    )


class TestEnergyBalance:
    def test_balance_holds_every_hour(self):
        req = _flat_scenario(demand=100.0)
        result = solve(req, [])
        for entry in result.hourly_plan:
            h = entry.hour
            charge = entry.battery_kwh if entry.battery_action.value == "charge" else 0.0
            discharge = entry.battery_kwh if entry.battery_action.value == "discharge" else 0.0
            demand = req.hours[h].demand_kwh
            lhs = entry.grid_kwh + entry.solar_used_kwh + discharge
            rhs = demand + charge
            assert abs(lhs - rhs) <= TOL, f"hour {h}: balance violated"


class TestBatteryNeutrality:
    def test_end_of_day_battery_equals_initial(self):
        req = _flat_scenario(demand=100.0)
        result = solve(req, [])
        final = result.hourly_plan[-1].battery_energy_after_kwh
        assert abs(final - req.battery.initial_energy_kwh) <= TOL

    def test_with_diverse_tariffs(self):
        # Cheap hours 0-5, expensive 18-22
        tariffs = [5.0] * 6 + [15.0] * 12 + [30.0] * 5 + [10.0]
        req = _make_scenario(
            demands=[100.0] * 24,
            solar=[0.0] * 24,
            tariffs=tariffs,
        )
        result = solve(req, [])
        final = result.hourly_plan[-1].battery_energy_after_kwh
        assert abs(final - req.battery.initial_energy_kwh) <= TOL


class TestSolarUsed:
    def test_solar_never_exceeds_available(self):
        solar = [0.0] * 6 + [50.0] * 12 + [0.0] * 6
        req = _make_scenario(
            demands=[80.0] * 24,
            solar=solar,
            tariffs=[10.0] * 24,
        )
        result = solve(req, [])
        for entry in result.hourly_plan:
            avail = solar[entry.hour]
            assert entry.solar_used_kwh <= avail + TOL

    def test_solar_used_reduces_grid_cost(self):
        solar = [0.0] * 24
        req_no_solar = _make_scenario([80.0] * 24, solar, [10.0] * 24)
        r_no_solar = solve(req_no_solar, [])

        solar_avail = [0.0] * 6 + [60.0] * 12 + [0.0] * 6
        req_solar = _make_scenario([80.0] * 24, solar_avail, [10.0] * 24)
        r_solar = solve(req_solar, [])

        assert r_solar.total_cost_bdt < r_no_solar.total_cost_bdt


class TestDirectiveConstraints:
    def test_no_charge_window_respected(self):
        req = _flat_scenario(demand=100.0, tariff=5.0)
        directive = ValidatedDirective(
            directive_type=DirectiveType.no_charge_window,
            hours=list(range(24)),  # block charging all day
        )
        result = solve(req, [directive])
        for entry in result.hourly_plan:
            if entry.battery_action.value == "charge":
                assert entry.battery_kwh <= TOL

    def test_no_discharge_window_respected(self):
        req = _flat_scenario(demand=100.0, tariff=20.0)
        directive = ValidatedDirective(
            directive_type=DirectiveType.no_discharge_window,
            hours=list(range(24)),
        )
        result = solve(req, [directive])
        for entry in result.hourly_plan:
            if entry.battery_action.value == "discharge":
                assert entry.battery_kwh <= TOL

    def test_solar_reduction_reduces_effective_solar(self):
        solar_kwh = [0.0] * 6 + [100.0] * 12 + [0.0] * 6
        req = _make_scenario([80.0] * 24, solar_kwh, [10.0] * 24)
        directive = ValidatedDirective(
            directive_type=DirectiveType.solar_reduction,
            hours=[10, 11, 12],
            factor=0.2,
        )
        result = solve(req, [directive])
        for entry in result.hourly_plan:
            if entry.hour in [10, 11, 12]:
                # effective solar = 100 * 0.2 = 20 kWh
                assert entry.solar_used_kwh <= 20.0 + TOL

    def test_minimum_battery_reserve_respected(self):
        req = _flat_scenario(demand=100.0, tariff=20.0, initial=150.0, capacity=200.0,
                              min_energy=20.0)
        directive = ValidatedDirective(
            directive_type=DirectiveType.minimum_battery_reserve,
            hours=[18, 19, 20],
            minimum_energy_kwh=100.0,
        )
        result = solve(req, [directive])
        for entry in result.hourly_plan:
            if entry.hour in [18, 19, 20]:
                assert entry.battery_energy_after_kwh >= 100.0 - TOL

    def test_max_grid_window_respected(self):
        # Grid capped at 50 kWh in hours 0-2. Demand is 80 kWh.
        # Battery starts at 150 kWh and can discharge up to 50 kWh/hour
        # to cover the 30 kWh gap. End-of-day neutrality requires
        # charging back those 3×30=90 kWh in uncapped hours.
        req = _make_scenario(
            demands=[80.0] * 24,
            solar=[0.0] * 24,
            tariffs=[10.0] * 24,
            capacity=300.0,
            initial=150.0,
            min_energy=20.0,
            max_charge=50.0,
            max_discharge=50.0,
        )
        directive = ValidatedDirective(
            directive_type=DirectiveType.max_grid_window,
            hours=[0, 1, 2],
            max_grid_kwh=50.0,
        )
        result = solve(req, [directive])
        for entry in result.hourly_plan:
            if entry.hour in [0, 1, 2]:
                assert entry.grid_kwh <= 50.0 + TOL


class TestRecalculatedTotals:
    def test_totals_match_plan(self):
        req = _flat_scenario(demand=100.0)
        result = solve(req, [])
        plan = result.hourly_plan
        tariff_map = {h.hour: h.tariff_bdt_per_kwh for h in req.hours}

        expected_grid = sum(e.grid_kwh for e in plan)
        expected_cost = sum(e.grid_kwh * tariff_map[e.hour] for e in plan)
        expected_peak = max(e.grid_kwh for e in plan)

        assert abs(result.total_grid_kwh - round(expected_grid, 2)) <= TOL
        assert abs(result.total_cost_bdt - round(expected_cost, 2)) <= TOL
        assert abs(result.peak_grid_kwh - round(expected_peak, 2)) <= TOL
