"""Tests for the interpreter's schema, parsing, and latency guards.

No network calls: these cover the deterministic parts of the model layer,
which is everything except the model's own judgement.
"""

from __future__ import annotations

import json

import pytest

from app.services import llm_interpreter as interp
from app.utils.constants import ALLOWED_DIRECTIVE_TYPES
from app.utils.exceptions import LLMInterpretationError


class TestStrictSchema:
    """OpenAI strict mode rejects a schema that breaks these rules."""

    def test_every_property_is_required(self):
        item = interp._RESPONSE_SCHEMA["properties"]["interpretations"]["items"]
        assert set(item["required"]) == set(item["properties"])

    def test_additional_properties_disabled(self):
        root = interp._RESPONSE_SCHEMA
        item = root["properties"]["interpretations"]["items"]
        assert root["additionalProperties"] is False
        assert item["additionalProperties"] is False

    def test_directive_enum_matches_the_spec(self):
        item = interp._RESPONSE_SCHEMA["properties"]["interpretations"]["items"]
        assert set(item["properties"]["directive_type"]["enum"]) == ALLOWED_DIRECTIVE_TYPES

    def test_type_specific_numbers_are_nullable(self):
        props = interp._RESPONSE_SCHEMA["properties"]["interpretations"]["items"]["properties"]
        for field in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
            assert props[field]["type"] == ["number", "null"]

    def test_few_shot_example_satisfies_the_schema(self):
        """A malformed example teaches the model the wrong shape."""
        item = interp._RESPONSE_SCHEMA["properties"]["interpretations"]["items"]
        required = set(item["required"])
        for entry in json.loads(interp._FEW_SHOT_ASSISTANT)["interpretations"]:
            assert set(entry) == required
            assert entry["directive_type"] in ALLOWED_DIRECTIVE_TYPES


class TestAdjustmentRebuild:
    """The flat model output is re-nested into the public contract shape."""

    @staticmethod
    def _entry(**over):
        base = {
            "note_index": 0,
            "directive_type": "no_op",
            "hours": [],
            "factor": None,
            "minimum_energy_kwh": None,
            "max_grid_kwh": None,
            "explanation": "x",
        }
        return {**base, **over}

    def test_no_op_yields_null_adjustment(self):
        out = interp._rebuild_structured_adjustment(self._entry())
        assert out["structured_adjustment"] is None

    def test_solar_reduction_keeps_only_factor(self):
        out = interp._rebuild_structured_adjustment(
            self._entry(directive_type="solar_reduction", hours=[12, 13], factor=0.25)
        )
        assert out["structured_adjustment"] == {"hours": [12, 13], "factor": 0.25}

    def test_reserve_keeps_only_minimum_energy(self):
        out = interp._rebuild_structured_adjustment(
            self._entry(
                directive_type="minimum_battery_reserve",
                hours=[18, 19],
                minimum_energy_kwh=100,
                max_grid_kwh=999,  # wrong field for this type; must be dropped
            )
        )
        assert out["structured_adjustment"] == {
            "hours": [18, 19],
            "minimum_energy_kwh": 100,
        }

    def test_window_types_carry_hours_only(self):
        for dtype in ("no_charge_window", "no_discharge_window"):
            out = interp._rebuild_structured_adjustment(
                self._entry(directive_type=dtype, hours=[2, 3], factor=0.5)
            )
            assert out["structured_adjustment"] == {"hours": [2, 3]}

    def test_unknown_type_is_neutralised(self):
        out = interp._rebuild_structured_adjustment(
            self._entry(directive_type="teleport_energy", hours=[1])
        )
        assert out["structured_adjustment"] is None


class TestPayloadParsing:
    def test_parses_wrapped_array(self):
        payload = json.dumps({"interpretations": [{"note_index": 0}]})
        assert interp._to_raw_entries(payload) == [{"note_index": 0}]

    def test_parses_bare_array(self):
        assert interp._to_raw_entries('[{"note_index": 1}]') == [{"note_index": 1}]

    def test_rejects_non_json(self):
        with pytest.raises(LLMInterpretationError):
            interp._to_raw_entries("sorry, I cannot help with that")

    def test_rejects_missing_array(self):
        with pytest.raises(LLMInterpretationError):
            interp._to_raw_entries('{"result": "none"}')

    def test_drops_non_object_entries(self):
        payload = json.dumps({"interpretations": [{"note_index": 0}, "junk", None]})
        assert interp._to_raw_entries(payload) == [{"note_index": 0}]


class TestLatencyBudget:
    """The judge fails any request over 30 s, so the model stage is capped."""

    def test_worst_case_llm_time_stays_under_the_limit(self):
        from app.config import settings

        assert settings.llm_deadline_seconds < 30
        assert settings.openai_timeout_seconds <= settings.llm_deadline_seconds

    def test_missing_api_key_raises_rather_than_hanging(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "openai_api_key", "")
        interp.reset_client()
        with pytest.raises(LLMInterpretationError):
            interp.get_client()
        interp.reset_client()


class TestFatalErrorDetection:
    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_client_errors_are_not_retried(self, status):
        exc = Exception("nope")
        exc.status_code = status
        assert interp._is_fatal(exc) is True

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    def test_transient_errors_are_retried(self, status):
        exc = Exception("try again")
        exc.status_code = status
        assert interp._is_fatal(exc) is False
