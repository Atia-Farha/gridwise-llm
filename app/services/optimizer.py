"""OR-Tools LP optimizer for the 24-hour energy scheduling problem.

Formulation:
  Decision variables per hour h ∈ {0…23}:
    grid[h]      ≥ 0   — grid electricity purchased (kWh)
    solar[h]     ≥ 0   — solar energy used (kWh)
    charge[h]    ≥ 0   — battery energy charged (kWh)
    discharge[h] ≥ 0   — battery energy discharged (kWh)

  Derived per hour:
    E[h] = initial_energy + Σ(charge[0..h]) - Σ(discharge[0..h])

  Objective: minimise Σ grid[h] × tariff[h]
  
  Base constraints:
    • Energy balance:  grid[h] + solar[h] + discharge[h] = demand[h] + charge[h]
    • Solar cap:       solar[h] ≤ effective_solar[h]
    • Charge rate:     charge[h] ≤ max_charge_kwh_per_hour
    • Discharge rate:  discharge[h] ≤ max_discharge_kwh_per_hour
    • Battery bounds:  minimum_energy_kwh ≤ E[h] ≤ capacity_kwh
    • End-of-day:      E[23] = initial_energy_kwh

  Directive constraints (applied on top of base):
    • solar_reduction:           effective_solar[h] *= factor
    • minimum_battery_reserve:   E[h] ≥ max(base_min, directive_min)
    • no_charge_window:          charge[h] = 0
    • no_discharge_window:       discharge[h] = 0
    • max_grid_window:           grid[h] ≤ max_grid_kwh

  Anti-simultaneous charge+discharge: tiny penalty ε on (charge[h] + discharge[h])
  in the objective discourages degenerate solutions without changing optimality.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ortools.linear_solver import pywraplp

from app.config import settings
from app.models.directives import BatteryAction, DirectiveType, ValidatedDirective
from app.models.request import BatteryConfig, HourEntry, ScenarioRequest
from app.models.response import HourlyPlanEntry
from app.utils.exceptions import OptimizationError

logger = logging.getLogger(__name__)


@dataclass
class OptimizerResult:
    hourly_plan: list[HourlyPlanEntry]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float


def _compute_effective_solar(
    hours: list[HourEntry],
    directives: list[ValidatedDirective],
) -> dict[int, float]:
    """Apply solar_reduction directives to base solar values."""
    effective = {h.hour: h.solar_kwh for h in hours}
    for d in directives:
        if d.directive_type == DirectiveType.solar_reduction and d.factor is not None:
            for h in d.hours:
                if h in effective:
                    effective[h] = effective[h] * d.factor
    return effective


def solve(
    request: ScenarioRequest,
    directives: list[ValidatedDirective],
) -> OptimizerResult:
    """Solve the LP and return the optimal 24-hour schedule."""
    battery: BatteryConfig = request.battery
    hour_map: dict[int, HourEntry] = {h.hour: h for h in request.hours}
    hours_sorted = sorted(hour_map.keys())

    # Pre-compute effective solar (incorporates solar_reduction directives)
    effective_solar = _compute_effective_solar(request.hours, directives)

    # Build per-hour directive lookups
    min_reserve_override: dict[int, float] = {}   # h → extra minimum kWh
    no_charge_hours: set[int] = set()
    no_discharge_hours: set[int] = set()
    max_grid_override: dict[int, float] = {}       # h → grid cap kWh

    for d in directives:
        if d.directive_type == DirectiveType.minimum_battery_reserve:
            if d.minimum_energy_kwh is not None:
                for h in d.hours:
                    prev = min_reserve_override.get(h, battery.minimum_energy_kwh)
                    min_reserve_override[h] = max(prev, d.minimum_energy_kwh)
        elif d.directive_type == DirectiveType.no_charge_window:
            no_charge_hours.update(d.hours)
        elif d.directive_type == DirectiveType.no_discharge_window:
            no_discharge_hours.update(d.hours)
        elif d.directive_type == DirectiveType.max_grid_window:
            if d.max_grid_kwh is not None:
                for h in d.hours:
                    prev = max_grid_override.get(h, float("inf"))
                    max_grid_override[h] = min(prev, d.max_grid_kwh)

    # ---------------------------------------------------------------------------
    # Create the LP solver
    # ---------------------------------------------------------------------------
    solver = pywraplp.Solver.CreateSolver("GLOP")
    if solver is None:
        raise OptimizationError("Failed to create OR-Tools GLOP solver")

    inf = solver.infinity()
    eps = settings.charge_discharge_penalty

    # Decision variables
    grid: dict[int, pywraplp.Variable] = {}
    solar_used: dict[int, pywraplp.Variable] = {}
    charge: dict[int, pywraplp.Variable] = {}
    discharge: dict[int, pywraplp.Variable] = {}

    for h in hours_sorted:
        # Grid: upper-bound from max_grid_window; default is uncapped
        grid_ub = max_grid_override.get(h, inf)
        grid[h] = solver.NumVar(0.0, grid_ub, f"grid_{h}")

        solar_ub = effective_solar.get(h, 0.0)
        solar_used[h] = solver.NumVar(0.0, solar_ub, f"solar_{h}")

        charge_ub = 0.0 if h in no_charge_hours else battery.max_charge_kwh_per_hour
        charge[h] = solver.NumVar(0.0, charge_ub, f"charge_{h}")

        discharge_ub = 0.0 if h in no_discharge_hours else battery.max_discharge_kwh_per_hour
        discharge[h] = solver.NumVar(0.0, discharge_ub, f"discharge_{h}")

    # ---------------------------------------------------------------------------
    # Battery energy expressions: E[h] tracked as a linear expression
    # We represent E[h] via auxiliary variables constrained to equal
    # E[h-1] + charge[h] - discharge[h], so the solver can reference them
    # in subsequent-hour constraints (e.g., minimum reserve).
    # ---------------------------------------------------------------------------
    E: dict[int, pywraplp.Variable] = {}
    for h in hours_sorted:
        E[h] = solver.NumVar(0.0, battery.capacity_kwh, f"E_{h}")

    # E[h] = E[h-1] + charge[h] - discharge[h]
    for i, h in enumerate(hours_sorted):
        ct = solver.Constraint(0.0, 0.0, f"bat_state_{h}")
        ct.SetCoefficient(E[h], 1.0)
        ct.SetCoefficient(charge[h], -1.0)
        ct.SetCoefficient(discharge[h], 1.0)
        if i == 0:
            ct.SetBounds(battery.initial_energy_kwh, battery.initial_energy_kwh)
        else:
            prev_h = hours_sorted[i - 1]
            ct.SetCoefficient(E[prev_h], -1.0)
            ct.SetBounds(0.0, 0.0)

    # ---------------------------------------------------------------------------
    # Base constraints
    # ---------------------------------------------------------------------------
    for h in hours_sorted:
        demand = hour_map[h].demand_kwh

        # Energy balance: grid + solar_used + discharge = demand + charge
        ct_balance = solver.Constraint(demand, demand, f"balance_{h}")
        ct_balance.SetCoefficient(grid[h], 1.0)
        ct_balance.SetCoefficient(solar_used[h], 1.0)
        ct_balance.SetCoefficient(discharge[h], 1.0)
        ct_balance.SetCoefficient(charge[h], -1.0)

        # Battery minimum energy (base + any directive override)
        active_min = min_reserve_override.get(h, battery.minimum_energy_kwh)
        active_min = max(active_min, battery.minimum_energy_kwh)
        solver.Add(E[h] >= active_min)

    # End-of-day neutrality: E[23] = initial_energy_kwh
    last_h = hours_sorted[-1]
    solver.Add(E[last_h] == battery.initial_energy_kwh)

    # ---------------------------------------------------------------------------
    # Objective: minimise grid cost + tiny charge/discharge penalty
    # ---------------------------------------------------------------------------
    objective = solver.Objective()
    for h in hours_sorted:
        tariff = hour_map[h].tariff_bdt_per_kwh
        objective.SetCoefficient(grid[h], tariff)
        # Anti-simultaneous charge+discharge penalty (negligible vs cost)
        objective.SetCoefficient(charge[h], eps)
        objective.SetCoefficient(discharge[h], eps)
    objective.SetMinimization()

    # ---------------------------------------------------------------------------
    # Solve
    # ---------------------------------------------------------------------------
    status = solver.Solve()
    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        raise OptimizationError(
            f"LP solver returned status {status} — no feasible solution found"
        )

    if status == pywraplp.Solver.FEASIBLE:
        logger.warning("Solver returned FEASIBLE (not OPTIMAL) — solution may be suboptimal")

    # ---------------------------------------------------------------------------
    # Extract hourly plan
    # ---------------------------------------------------------------------------
    plan: list[HourlyPlanEntry] = []
    lp_eps = settings.lp_epsilon

    for h in hours_sorted:
        g_val = max(0.0, grid[h].solution_value())
        s_val = max(0.0, solar_used[h].solution_value())
        c_val = max(0.0, charge[h].solution_value())
        d_val = max(0.0, discharge[h].solution_value())
        e_val = max(0.0, E[h].solution_value())

        # Determine battery action — prefer charge over discharge if both > eps
        # (penalty in objective should prevent simultaneous, but just in case)
        if c_val > lp_eps and c_val >= d_val:
            action = BatteryAction.charge
            kwh = c_val
        elif d_val > lp_eps:
            action = BatteryAction.discharge
            kwh = d_val
        else:
            action = BatteryAction.idle
            kwh = 0.0

        plan.append(
            HourlyPlanEntry(
                hour=h,
                grid_kwh=round(g_val, 6),
                solar_used_kwh=round(s_val, 6),
                battery_action=action,
                battery_kwh=round(kwh, 6),
                battery_energy_after_kwh=round(e_val, 6),
            )
        )

    # Recalculate totals from plan (source of truth per spec §11.3)
    total_grid = sum(e.grid_kwh for e in plan)
    total_cost = sum(
        e.grid_kwh * hour_map[e.hour].tariff_bdt_per_kwh for e in plan
    )
    peak_grid = max(e.grid_kwh for e in plan)

    return OptimizerResult(
        hourly_plan=plan,
        total_grid_kwh=round(total_grid, 2),
        total_cost_bdt=round(total_cost, 2),
        peak_grid_kwh=round(peak_grid, 2),
    )
