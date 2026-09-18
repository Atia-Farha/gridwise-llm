"""The OpenAPI example must be a request the service actually accepts.

Without an explicit example, Swagger UI synthesises one from the constraints:
it picks the boundary value for `hour` (23) and repeats that entry 24 times to
satisfy minItems, producing a body that fails the uniqueness rule. Anyone
clicking "Try it out" then gets a 422 on their first request.
"""

from __future__ import annotations

from app.models.request import _REQUEST_EXAMPLE, ScenarioRequest
from tests.conftest import no_op_interps, patch_llm


class TestSchemaExample:
    def test_example_is_published_in_the_schema(self, client):
        schema = client.get("/openapi.json").json()
        assert "examples" in schema["components"]["schemas"]["ScenarioRequest"]

    def test_example_passes_validation(self):
        request = ScenarioRequest(**_REQUEST_EXAMPLE)
        assert [h.hour for h in request.hours] == list(range(24))

    def test_example_is_accepted_by_the_endpoint(self, client):
        with patch_llm(no_op_interps(2)):
            resp = client.post("/optimize-energy", json=_REQUEST_EXAMPLE)
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["hourly_plan"]) == 24


class TestDuplicateHoursRejected:
    def test_swagger_style_duplicate_hours_is_rejected(self, client):
        """The body Swagger used to generate: 24 copies of hour 23."""
        bad = {
            **_REQUEST_EXAMPLE,
            "hours": [
                {"hour": 23, "demand_kwh": 0, "solar_kwh": 0, "tariff_bdt_per_kwh": 0}
            ]
            * 24,
        }
        resp = client.post("/optimize-energy", json=bad)
        assert resp.status_code == 422
        assert "unique" in resp.text.lower()


class TestErrorBodyIsControlled:
    def test_validation_errors_do_not_echo_the_payload(self, client):
        """A full 24-hour scenario should not be reflected back on every error."""
        bad = {
            **_REQUEST_EXAMPLE,
            "hours": [
                {"hour": 23, "demand_kwh": 0, "solar_kwh": 0, "tariff_bdt_per_kwh": 0}
            ]
            * 24,
        }
        resp = client.post("/optimize-energy", json=bad)
        payload = resp.json()
        assert "GRID-DEMO-01" not in resp.text, "request payload echoed back"
        for entry in payload["detail"]:
            assert set(entry) == {"type", "loc", "msg"}

    def test_error_body_stays_small(self, client):
        bad = {**_REQUEST_EXAMPLE, "battery": {"capacity_kwh": -5}}
        resp = client.post("/optimize-energy", json=bad)
        assert resp.status_code == 422
        assert len(resp.content) < 2000
