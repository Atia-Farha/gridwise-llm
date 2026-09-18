"""Tests for POST /optimize-energy request/response schema validation."""

from tests.conftest import no_op_interps, patch_llm


VALID_REQUEST = {
    "scenario_id": "TEST-001",
    "operator_notes": ["The cafeteria menu changes tomorrow."],
    "hours": [
        {"hour": h, "demand_kwh": 100.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 10.0}
        for h in range(24)
    ],
    "battery": {
        "capacity_kwh": 200.0,
        "initial_energy_kwh": 80.0,
        "minimum_energy_kwh": 20.0,
        "max_charge_kwh_per_hour": 50.0,
        "max_discharge_kwh_per_hour": 50.0,
    },
}


class TestRequestValidation:
    def test_malformed_json_returns_400(self, client):
        """Problem Statement 6.1 reserves 400 for unparseable bodies."""
        resp = client.post(
            "/optimize-energy",
            content="not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_well_formed_but_invalid_returns_422(self, client):
        """A parseable body that fails semantic validation stays 422."""
        resp = client.post("/optimize-energy", json={"scenario_id": "X"})
        assert resp.status_code == 422

    def test_missing_scenario_id_returns_422(self, client):
        bad = dict(VALID_REQUEST)
        del bad["scenario_id"]  # type: ignore[misc]
        resp = client.post("/optimize-energy", json=bad)
        assert resp.status_code == 422

    def test_empty_notes_returns_422(self, client):
        bad = {**VALID_REQUEST, "operator_notes": []}
        resp = client.post("/optimize-energy", json=bad)
        assert resp.status_code == 422

    def test_too_many_notes_returns_422(self, client):
        bad = {**VALID_REQUEST, "operator_notes": ["a", "b", "c", "d"]}
        resp = client.post("/optimize-energy", json=bad)
        assert resp.status_code == 422

    def test_wrong_number_of_hours_returns_422(self, client):
        bad = {**VALID_REQUEST, "hours": VALID_REQUEST["hours"][:20]}
        resp = client.post("/optimize-energy", json=bad)
        assert resp.status_code == 422

    def test_duplicate_hour_entries_returns_422(self, client):
        hours = [
            {"hour": h % 23, "demand_kwh": 100.0, "solar_kwh": 0.0,
             "tariff_bdt_per_kwh": 10.0}
            for h in range(24)
        ]
        bad = {**VALID_REQUEST, "hours": hours}
        resp = client.post("/optimize-energy", json=bad)
        assert resp.status_code == 422

    def test_negative_demand_returns_422(self, client):
        hours = [
            {"hour": h, "demand_kwh": -10.0 if h == 5 else 100.0,
             "solar_kwh": 0.0, "tariff_bdt_per_kwh": 10.0}
            for h in range(24)
        ]
        bad = {**VALID_REQUEST, "hours": hours}
        resp = client.post("/optimize-energy", json=bad)
        assert resp.status_code == 422

    def test_initial_energy_exceeds_capacity_returns_422(self, client):
        bad_battery = {**VALID_REQUEST["battery"], "initial_energy_kwh": 999.0}
        bad = {**VALID_REQUEST, "battery": bad_battery}
        resp = client.post("/optimize-energy", json=bad)
        assert resp.status_code == 422


class TestResponseSchema:
    def test_response_echoes_scenario_id(self, client):
        """Run with mocked LLM to avoid real API calls in schema tests."""
        with patch_llm(no_op_interps(1)):
            resp = client.post("/optimize-energy", json=VALID_REQUEST)

        assert resp.status_code == 200
        data = resp.json()
        assert data["scenario_id"] == "TEST-001"

    def test_response_has_all_required_fields(self, client):
        with patch_llm(no_op_interps(1)):
            resp = client.post("/optimize-energy", json=VALID_REQUEST)

        assert resp.status_code == 200
        data = resp.json()
        required_fields = {
            "scenario_id", "directive_interpretation", "hourly_plan",
            "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh", "plan_summary",
        }
        assert required_fields.issubset(data.keys())

    def test_hourly_plan_has_24_entries(self, client):
        with patch_llm(no_op_interps(1)):
            resp = client.post("/optimize-energy", json=VALID_REQUEST)

        assert resp.status_code == 200
        assert len(resp.json()["hourly_plan"]) == 24

    def test_directive_interpretation_count_matches_notes(self, client):
        req = {**VALID_REQUEST, "operator_notes": ["Note A", "Note B"]}
        with patch_llm(no_op_interps(2)):
            resp = client.post("/optimize-energy", json=req)

        assert resp.status_code == 200
        assert len(resp.json()["directive_interpretation"]) == 2
