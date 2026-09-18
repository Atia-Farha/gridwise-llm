"""Integration tests against all 10 public sample cases.

These tests mock the LLM interpreter and inject the ground-truth directive
interpretations directly, then verify the optimizer produces a schedule that:
  1. Satisfies all ground-truth directives
  2. Matches the expected total_cost_bdt within tolerance
  3. Passes full energy-balance and battery-rule verification
"""

from __future__ import annotations

import pytest

from app.utils.constants import NUMERIC_TOLERANCE
from tests.conftest import patch_llm

TOL = NUMERIC_TOLERANCE


def _expected_raw_interps(case: dict) -> list[dict]:
    """Convert expected_output directive_interpretation to raw LLM format."""
    return [
        {
            "note_index": d["note_index"],
            "applies": d["applies"],
            "directive_type": d["directive_type"],
            "structured_adjustment": d["structured_adjustment"],
            "explanation": d["explanation"],
        }
        for d in case["expected_output"]["directive_interpretation"]
    ]


@pytest.mark.parametrize(
    "case_index",
    list(range(10)),
    ids=[f"SAMPLE-{i+1:02d}" for i in range(10)],
)
def test_sample_case_optimizer(client, sample_cases, case_index):
    """For each sample case: inject ground-truth directives, verify schedule validity."""
    case = sample_cases[case_index]
    req_body = case["input"]
    expected = case["expected_output"]

    ground_truth_interps = _expected_raw_interps(case)

    with patch_llm(ground_truth_interps):
        resp = client.post("/optimize-energy", json=req_body)

    assert resp.status_code == 200, f"Non-200 response: {resp.text}"
    data = resp.json()

    # ── Schema checks ──────────────────────────────────────────────────────
    assert data["scenario_id"] == expected["scenario_id"]
    assert len(data["hourly_plan"]) == 24
    assert len(data["directive_interpretation"]) == len(expected["directive_interpretation"])

    # ── Directive interpretation checks ───────────────────────────────────
    for i, di in enumerate(data["directive_interpretation"]):
        expected_di = expected["directive_interpretation"][i]
        assert di["note_index"] == expected_di["note_index"], f"case {case_index} note_index mismatch"
        assert di["directive_type"] == expected_di["directive_type"], (
            f"case {case_index} directive_type mismatch: got {di['directive_type']} "
            f"expected {expected_di['directive_type']}"
        )
        assert di["applies"] == expected_di["applies"]
        if not di["applies"]:
            assert di["structured_adjustment"] is None

    # ── Hourly plan validity ───────────────────────────────────────────────
    battery = req_body["battery"]
    hour_map = {h["hour"]: h for h in req_body["hours"]}

    # Build effective solar (apply solar_reduction directives)
    effective_solar = {h: hour_map[h]["solar_kwh"] for h in range(24)}
    for di in data["directive_interpretation"]:
        if di["directive_type"] == "solar_reduction" and di["applies"]:
            factor = di["structured_adjustment"]["factor"]
            for h in di["structured_adjustment"]["hours"]:
                effective_solar[h] = hour_map[h]["solar_kwh"] * factor

    # Per-hour floor, including any reserve raised by a directive. Checking
    # only the base minimum would let a minimum_battery_reserve violation pass.
    floor = {h: battery["minimum_energy_kwh"] for h in range(24)}
    grid_cap: dict[int, float] = {}
    no_charge: set[int] = set()
    no_discharge: set[int] = set()
    for di in data["directive_interpretation"]:
        if not di["applies"]:
            continue
        adj = di["structured_adjustment"] or {}
        for h in adj.get("hours", []):
            if di["directive_type"] == "minimum_battery_reserve":
                floor[h] = max(floor[h], adj["minimum_energy_kwh"])
            elif di["directive_type"] == "max_grid_window":
                grid_cap[h] = min(grid_cap.get(h, float("inf")), adj["max_grid_kwh"])
            elif di["directive_type"] == "no_charge_window":
                no_charge.add(h)
            elif di["directive_type"] == "no_discharge_window":
                no_discharge.add(h)

    E = battery["initial_energy_kwh"]
    for entry in data["hourly_plan"]:
        h = entry["hour"]
        demand = hour_map[h]["demand_kwh"]
        charge = entry["battery_kwh"] if entry["battery_action"] == "charge" else 0.0
        discharge = entry["battery_kwh"] if entry["battery_action"] == "discharge" else 0.0

        # Energy balance
        lhs = entry["grid_kwh"] + entry["solar_used_kwh"] + discharge
        rhs = demand + charge
        assert abs(lhs - rhs) <= TOL, (
            f"case {case_index} hour {h}: energy balance violated "
            f"lhs={lhs:.4f} rhs={rhs:.4f}"
        )

        # Solar cap
        assert entry["solar_used_kwh"] <= effective_solar[h] + TOL, (
            f"case {case_index} hour {h}: solar overuse"
        )

        # Battery state
        # Rate limits
        assert charge <= battery["max_charge_kwh_per_hour"] + TOL
        assert discharge <= battery["max_discharge_kwh_per_hour"] + TOL

        # Directive application
        assert h not in no_charge or charge <= TOL, (
            f"case {case_index} hour {h}: no_charge_window violated"
        )
        assert h not in no_discharge or discharge <= TOL, (
            f"case {case_index} hour {h}: no_discharge_window violated"
        )
        if h in grid_cap:
            assert entry["grid_kwh"] <= grid_cap[h] + TOL, (
                f"case {case_index} hour {h}: max_grid_window violated"
            )

        E += charge - discharge
        assert abs(E - entry["battery_energy_after_kwh"]) <= TOL, (
            f"case {case_index} hour {h}: battery state mismatch"
        )
        assert E >= floor[h] - TOL, (
            f"case {case_index} hour {h}: battery {E} below required floor {floor[h]}"
        )
        assert E <= battery["capacity_kwh"] + TOL

    # End-of-day neutrality
    assert abs(E - battery["initial_energy_kwh"]) <= TOL, (
        f"case {case_index}: end-of-day battery not neutral"
    )

    # ── Cost check — our cost should not exceed expected by more than 1% ──
    expected_cost = expected["total_cost_bdt"]
    our_cost = data["total_cost_bdt"]
    # We should be within 1% of optimal (LP is globally optimal if directives
    # are correctly applied; slight difference may occur from floating point)
    if expected_cost > TOL:
        ratio = our_cost / expected_cost
        assert ratio <= 1.0001, (
            f"case {case_index}: cost ratio {ratio:.6f} is above the reference "
            f"optimum (our={our_cost}, expected={expected_cost})"
        )

    # ── Recalculated totals match reported totals ─────────────────────────
    recalc_grid = sum(e["grid_kwh"] for e in data["hourly_plan"])
    recalc_cost = sum(
        e["grid_kwh"] * hour_map[e["hour"]]["tariff_bdt_per_kwh"]
        for e in data["hourly_plan"]
    )
    recalc_peak = max(e["grid_kwh"] for e in data["hourly_plan"])

    assert abs(data["total_grid_kwh"] - round(recalc_grid, 2)) <= TOL
    assert abs(data["total_cost_bdt"] - round(recalc_cost, 2)) <= TOL
    assert abs(data["peak_grid_kwh"] - round(recalc_peak, 2)) <= TOL
