"""Robustness tests — the failure paths the judge harness probes.

These cover the behaviours that turn a single bad interpretation into a lost
case: infeasible directives, provider outages, malformed model output, and
plans that would not survive the judge's own replay.
"""

from __future__ import annotations

import pytest

from app.models.directives import DirectiveType, ValidatedDirective
from app.models.request import ScenarioRequest
from app.services import optimizer, replay_validator
from app.utils.exceptions import LLMInterpretationError
from tests.conftest import no_op_interps, patch_llm

TOL = 0.01


def _request(case: dict) -> ScenarioRequest:
    return ScenarioRequest(**case["input"])


def _assert_valid_plan(data: dict, body: dict) -> None:
    """Every hard GridWise rule, checked against the returned plan."""
    battery = body["battery"]
    hour_map = {h["hour"]: h for h in body["hours"]}
    assert len(data["hourly_plan"]) == 24

    energy = battery["initial_energy_kwh"]
    for entry in data["hourly_plan"]:
        h = entry["hour"]
        charge = entry["battery_kwh"] if entry["battery_action"] == "charge" else 0.0
        discharge = entry["battery_kwh"] if entry["battery_action"] == "discharge" else 0.0

        assert entry["grid_kwh"] >= -TOL
        assert entry["solar_used_kwh"] <= hour_map[h]["solar_kwh"] + TOL
        assert (
            abs(
                entry["grid_kwh"] + entry["solar_used_kwh"] + discharge
                - hour_map[h]["demand_kwh"] - charge
            )
            <= TOL
        ), f"hour {h}: energy balance violated"

        energy += charge - discharge
        assert abs(energy - entry["battery_energy_after_kwh"]) <= TOL
        assert energy <= battery["capacity_kwh"] + TOL

    assert abs(energy - battery["initial_energy_kwh"]) <= TOL, "battery not neutral"


class TestInfeasibleDirectives:
    """An over-tight directive must degrade, never 500.

    A 500 forfeits interpretation, application, optimisation and reliability
    credit at once. A relaxed but valid plan keeps most of it.
    """

    def test_impossible_grid_cap_still_returns_valid_plan(self, client, sample_cases):
        body = sample_cases[0]["input"]
        interps = [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "max_grid_window",
                "structured_adjustment": {"hours": list(range(24)), "max_grid_kwh": 0},
                "explanation": "Over-tight cap.",
            },
            *no_op_interps(2)[1:],
        ]
        with patch_llm(interps):
            resp = client.post("/optimize-energy", json=body)

        assert resp.status_code == 200, resp.text
        _assert_valid_plan(resp.json(), body)

    def test_impossible_reserve_still_returns_valid_plan(self, client, sample_cases):
        body = sample_cases[0]["input"]
        capacity = body["battery"]["capacity_kwh"]
        interps = [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "minimum_battery_reserve",
                "structured_adjustment": {
                    "hours": list(range(24)),
                    "minimum_energy_kwh": capacity,
                },
                "explanation": "Unreachable reserve.",
            },
            *no_op_interps(2)[1:],
        ]
        with patch_llm(interps):
            resp = client.post("/optimize-energy", json=body)

        assert resp.status_code == 200, resp.text
        _assert_valid_plan(resp.json(), body)

    def test_summary_reports_the_relaxation(self, client, sample_cases):
        body = sample_cases[0]["input"]
        interps = [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "max_grid_window",
                "structured_adjustment": {"hours": list(range(24)), "max_grid_kwh": 0},
                "explanation": "Over-tight cap.",
            },
            *no_op_interps(2)[1:],
        ]
        with patch_llm(interps):
            data = client.post("/optimize-energy", json=body).json()
        assert "relaxed" in data["plan_summary"].lower()


class TestProviderFailure:
    def test_provider_outage_degrades_to_no_op(self, client, sample_cases):
        body = sample_cases[1]["input"]
        with patch_llm(error=LLMInterpretationError("provider down")):
            resp = client.post("/optimize-energy", json=body)

        assert resp.status_code == 200
        data = resp.json()
        assert [d["directive_type"] for d in data["directive_interpretation"]] == ["no_op"]
        assert data["directive_interpretation"][0]["applies"] is False
        _assert_valid_plan(data, body)

    def test_garbage_model_output_is_contained(self, client, sample_cases):
        body = sample_cases[1]["input"]
        interps = [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "teleport_energy",
                "structured_adjustment": {"nonsense": True},
                "explanation": "Invented directive.",
            }
        ]
        with patch_llm(interps):
            resp = client.post("/optimize-energy", json=body)

        assert resp.status_code == 200
        entry = resp.json()["directive_interpretation"][0]
        assert entry["directive_type"] == "no_op"
        assert entry["structured_adjustment"] is None

    def test_no_secret_leaks_in_error_body(self, client, sample_cases):
        body = sample_cases[0]["input"]
        with patch_llm(error=LLMInterpretationError("key sk-secret-value rejected")):
            resp = client.post("/optimize-energy", json=body)
        assert "sk-secret-value" not in resp.text


class TestPlanConsistency:
    @pytest.mark.parametrize("case_index", range(10))
    def test_battery_action_is_never_ambiguous(self, sample_cases, case_index):
        """Charge and discharge must never both be reported in one hour."""
        case = sample_cases[case_index]
        request = _request(case)
        directives = [
            ValidatedDirective(
                directive_type=DirectiveType(d["directive_type"]),
                hours=(d["structured_adjustment"] or {}).get("hours", []),
                factor=(d["structured_adjustment"] or {}).get("factor"),
                minimum_energy_kwh=(d["structured_adjustment"] or {}).get(
                    "minimum_energy_kwh"
                ),
                max_grid_kwh=(d["structured_adjustment"] or {}).get("max_grid_kwh"),
            )
            for d in case["expected_output"]["directive_interpretation"]
            if d["directive_type"] != "no_op"
        ]
        result = optimizer.solve(request, directives)

        for entry in result.hourly_plan:
            if entry.battery_action.value == "idle":
                assert entry.battery_kwh == 0.0
            else:
                assert entry.battery_kwh > 0.0

        replay_validator.replay_validate(
            result.hourly_plan,
            request,
            directives,
            optimizer.compute_effective_solar(request.hours, directives),
        )


class TestDegenerateBattery:
    def test_battery_starting_below_minimum_is_solvable(self, client, sample_cases):
        """Rejecting this scenario would forfeit an otherwise solvable case."""
        body = {
            **sample_cases[1]["input"],
            "battery": {
                **sample_cases[1]["input"]["battery"],
                "initial_energy_kwh": 20.0,
                "minimum_energy_kwh": 30.0,
            },
        }
        with patch_llm(no_op_interps(1)):
            resp = client.post("/optimize-energy", json=body)

        assert resp.status_code == 200, resp.text
        _assert_valid_plan(resp.json(), body)
