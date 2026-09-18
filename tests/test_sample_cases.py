"""Integration tests against all 10 public sample cases.

These tests mock the LLM interpreter and inject the ground-truth directive
interpretations directly, then verify the optimizer produces a schedule that:
  1. Satisfies all ground-truth directives
  2. Matches the expected total_cost_bdt within tolerance
  3. Passes full energy-balance and battery-rule verification
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.utils.constants import NUMERIC_TOLERANCE

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

    with patch(
        "app.services.llm_interpreter.interpret_notes",
        return_value=ground_truth_interps,
    ):
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
        E += charge - discharge
        assert abs(E - entry["battery_energy_after_kwh"]) <= TOL, (
            f"case {case_index} hour {h}: battery state mismatch"
        )
        assert E >= battery["minimum_energy_kwh"] - TOL
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
        assert ratio <= 1.01, (
            f"case {case_index}: cost ratio {ratio:.4f} exceeds 1.01 "
            f"(our={our_cost}, expected={expected_cost})"
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
