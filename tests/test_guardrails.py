"""Unit tests for the guardrail validator — no LLM calls required."""

import pytest

from app.models.directives import DirectiveType
from app.models.request import BatteryConfig, HourEntry, ScenarioRequest
from app.services.guardrails import validate_interpretations


def _make_request(n_notes: int = 1, capacity: float = 500.0) -> ScenarioRequest:
    """Build a minimal valid request for testing."""
    return ScenarioRequest(
        scenario_id="TEST-001",
        operator_notes=[f"Note {i}" for i in range(n_notes)],
        hours=[
            HourEntry(hour=h, demand_kwh=100.0, solar_kwh=0.0, tariff_bdt_per_kwh=10.0)
            for h in range(24)
        ],
        battery=BatteryConfig(
            capacity_kwh=capacity,
            initial_energy_kwh=100.0,
            minimum_energy_kwh=20.0,
            max_charge_kwh_per_hour=50.0,
            max_discharge_kwh_per_hour=50.0,
        ),
    )


class TestGuardrailCount:
    def test_too_few_raw_padded_with_no_op(self):
        req = _make_request(n_notes=2)
        raw = [{"note_index": 0, "applies": False, "directive_type": "no_op",
                "structured_adjustment": None, "explanation": "X"}]
        result = validate_interpretations(raw, req)
        assert len(result) == 2
        assert result[1].directive_type == DirectiveType.no_op
        assert result[1].applies is False

    def test_extra_raw_ignored(self):
        req = _make_request(n_notes=1)
        raw = [
            {"note_index": 0, "applies": False, "directive_type": "no_op",
             "structured_adjustment": None, "explanation": "ok"},
            {"note_index": 5, "applies": True, "directive_type": "max_grid_window",
             "structured_adjustment": {"hours": [0], "max_grid_kwh": 10}, "explanation": "extra"},
        ]
        result = validate_interpretations(raw, req)
        assert len(result) == 1

    def test_duplicate_note_index_keeps_first(self):
        req = _make_request(n_notes=1)
        raw = [
            {"note_index": 0, "applies": False, "directive_type": "no_op",
             "structured_adjustment": None, "explanation": "first"},
            {"note_index": 0, "applies": True, "directive_type": "no_charge_window",
             "structured_adjustment": {"hours": [1]}, "explanation": "duplicate"},
        ]
        result = validate_interpretations(raw, req)
        assert result[0].directive_type == DirectiveType.no_op


class TestGuardrailDirectiveType:
    def test_invalid_type_becomes_no_op(self):
        req = _make_request()
        raw = [{"note_index": 0, "applies": True, "directive_type": "magic_directive",
                "structured_adjustment": {}, "explanation": "bad"}]
        result = validate_interpretations(raw, req)
        assert result[0].directive_type == DirectiveType.no_op

    def test_no_op_applies_forced_false(self):
        req = _make_request()
        raw = [{"note_index": 0, "applies": True, "directive_type": "no_op",
                "structured_adjustment": {"hours": [1]}, "explanation": "x"}]
        result = validate_interpretations(raw, req)
        assert result[0].applies is False
        assert result[0].structured_adjustment is None


class TestGuardrailHours:
    def test_hours_sorted_and_deduped(self):
        req = _make_request()
        raw = [{"note_index": 0, "applies": True, "directive_type": "no_charge_window",
                "structured_adjustment": {"hours": [5, 2, 5, 3]}, "explanation": "x"}]
        result = validate_interpretations(raw, req)
        assert result[0].structured_adjustment["hours"] == [2, 3, 5]

    def test_hours_out_of_range_filtered(self):
        req = _make_request()
        raw = [{"note_index": 0, "applies": True, "directive_type": "no_charge_window",
                "structured_adjustment": {"hours": [-1, 0, 23, 24, 100]}, "explanation": "x"}]
        result = validate_interpretations(raw, req)
        assert result[0].structured_adjustment["hours"] == [0, 23]


class TestGuardrailSolarFactor:
    def test_factor_clamped_above_1(self):
        req = _make_request()
        raw = [{"note_index": 0, "applies": True, "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [10], "factor": 1.5}, "explanation": "x"}]
        result = validate_interpretations(raw, req)
        assert result[0].structured_adjustment["factor"] == 1.0

    def test_factor_clamped_below_0(self):
        req = _make_request()
        raw = [{"note_index": 0, "applies": True, "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [10], "factor": -0.5}, "explanation": "x"}]
        result = validate_interpretations(raw, req)
        assert result[0].structured_adjustment["factor"] == 0.0


class TestGuardrailBatteryReserve:
    def test_reserve_clamped_to_capacity(self):
        req = _make_request(capacity=200.0)
        raw = [{"note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
                "structured_adjustment": {"hours": [18], "minimum_energy_kwh": 300.0},
                "explanation": "x"}]
        result = validate_interpretations(raw, req)
        assert result[0].structured_adjustment["minimum_energy_kwh"] == 200.0

    def test_negative_reserve_clamped_to_zero(self):
        req = _make_request()
        raw = [{"note_index": 0, "applies": True, "directive_type": "minimum_battery_reserve",
                "structured_adjustment": {"hours": [18], "minimum_energy_kwh": -50.0},
                "explanation": "x"}]
        result = validate_interpretations(raw, req)
        assert result[0].structured_adjustment["minimum_energy_kwh"] == 0.0


class TestGuardrailGridCap:
    def test_negative_grid_cap_clamped(self):
        req = _make_request()
        raw = [{"note_index": 0, "applies": True, "directive_type": "max_grid_window",
                "structured_adjustment": {"hours": [10], "max_grid_kwh": -10.0},
                "explanation": "x"}]
        result = validate_interpretations(raw, req)
        assert result[0].structured_adjustment["max_grid_kwh"] == 0.0


class TestGuardrailMissingAdjustment:
    def test_missing_adjustment_becomes_no_op(self):
        req = _make_request()
        raw = [{"note_index": 0, "applies": True, "directive_type": "solar_reduction",
                "structured_adjustment": None, "explanation": "x"}]
        result = validate_interpretations(raw, req)
        assert result[0].directive_type == DirectiveType.no_op

    def test_adjustment_missing_required_field_becomes_no_op(self):
        req = _make_request()
        # solar_reduction missing 'factor'
        raw = [{"note_index": 0, "applies": True, "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [10]}, "explanation": "x"}]
        result = validate_interpretations(raw, req)
        assert result[0].directive_type == DirectiveType.no_op
