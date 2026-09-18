"""LLM Interpreter — calls Gemini 2.5 Flash to interpret operator notes.

The LLM output is intentionally treated as UNTRUSTED until the guardrail
validator has cleared it.  This module only calls the model and returns raw
parsed JSON; it never applies any directive to the optimizer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from google import genai
from google.genai import types

from app.config import settings
from app.models.request import ScenarioRequest
from app.utils.exceptions import LLMInterpretationError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gemini client — initialised once at module load
# ---------------------------------------------------------------------------
_client: genai.Client | None = None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYSTEM_INSTRUCTION = """\
You are an energy operations interpreter for a smart campus microgrid.
Your ONLY job is to analyse each operator note and classify it into exactly
one of the following directive types:

  1. solar_reduction        — reduces usable solar energy during specific hours
  2. minimum_battery_reserve — requires battery energy stays at or above a
                               minimum level during specific hours
  3. no_charge_window       — battery charging is unavailable during specific hours
  4. no_discharge_window    — battery discharging is unavailable during specific hours
  5. max_grid_window        — grid import must not exceed a stated kWh cap during
                               specific hours
  6. no_op                  — the note does NOT affect the 24-hour energy schedule
                               (it is a distractor)

━━━ CRITICAL RULES ━━━
• Return EXACTLY one interpretation object per note, in note_index order 0, 1, … N-1.
• Time windows use WHOLE-HOUR intervals that are START-INCLUSIVE and END-EXCLUSIVE.
    "1 PM to 3 PM"     → hours [13, 14]
    "2 AM until 5 AM"  → hours [2, 3, 4]
    "6 PM to 9 PM"     → hours [18, 19, 20]
    "midnight to 3 AM" → hours [0, 1, 2]
• For solar_reduction, "factor" is the REMAINING usable fraction (not the reduction):
    "80% reduction"        → factor 0.2
    "drops to 25%"         → factor 0.25
    "one-fifth of normal"  → factor 0.2
    "only 30% available"   → factor 0.3
• Hours must be unique integers 0-23 returned in ascending order.
• For no_op: applies must be false and structured_adjustment must be null.
• For every other directive: applies must be true and structured_adjustment
  must contain the required fields.
• Do NOT invent demand, solar, tariff, or battery values not present in the note.
• Do NOT create a directive type that is not in the list above.

━━━ EXAMPLES ━━━

Note: "Solar output will drop to about 20% from 1 PM to 3 PM."
→ { "note_index": 0, "applies": true, "directive_type": "solar_reduction",
    "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
    "explanation": "Solar availability reduced to 20% during stated window." }

Note: "Do not charge the battery between 2 PM and 4 PM."
→ { "note_index": 0, "applies": true, "directive_type": "no_charge_window",
    "structured_adjustment": {"hours": [14, 15]},
    "explanation": "Battery charging blocked during the 2–4 PM window." }

Note: "Keep at least 120 kWh in reserve from 6 PM until 9 PM."
→ { "note_index": 0, "applies": true,
    "directive_type": "minimum_battery_reserve",
    "structured_adjustment": {"hours": [18, 19, 20], "minimum_energy_kwh": 120},
    "explanation": "Minimum 120 kWh battery reserve required from 6–9 PM." }

Note: "Grid import must stay below 80 kWh during peak hours 5 PM to 8 PM."
→ { "note_index": 0, "applies": true, "directive_type": "max_grid_window",
    "structured_adjustment": {"hours": [17, 18, 19], "max_grid_kwh": 80},
    "explanation": "Grid capped at 80 kWh per hour from 5–8 PM." }

Note: "The cafeteria menu changes tomorrow."
→ { "note_index": 0, "applies": false, "directive_type": "no_op",
    "structured_adjustment": null,
    "explanation": "This note does not affect the 24-hour energy schedule." }

Note: "Battery discharging will be blocked from 10 AM to noon for system tests."
→ { "note_index": 0, "applies": true, "directive_type": "no_discharge_window",
    "structured_adjustment": {"hours": [10, 11]},
    "explanation": "Battery discharging blocked during 10 AM–noon maintenance." }
"""


def _build_user_prompt(request: ScenarioRequest) -> str:
    notes_block = "\n".join(
        f'  Note {i}: "{note}"'
        for i, note in enumerate(request.operator_notes)
    )
    return (
        f"Scenario ID: {request.scenario_id}\n"
        f"Battery capacity: {request.battery.capacity_kwh} kWh\n"
        f"Battery base minimum reserve: {request.battery.minimum_energy_kwh} kWh\n"
        f"Number of operator notes: {len(request.operator_notes)}\n\n"
        f"Operator Notes:\n{notes_block}\n\n"
        f"Return a JSON array with exactly {len(request.operator_notes)} "
        f"interpretation object(s), one per note, in note_index order."
    )


# ---------------------------------------------------------------------------
# Gemini response schema — forces structured JSON output
# ---------------------------------------------------------------------------

def _build_response_schema(n_notes: int) -> dict[str, Any]:
    """Build the Gemini response schema for exactly n_notes directives."""
    item_schema = {
        "type": "OBJECT",
        "properties": {
            "note_index": {"type": "INTEGER"},
            "applies": {"type": "BOOLEAN"},
            "directive_type": {
                "type": "STRING",
                "enum": [
                    "solar_reduction",
                    "minimum_battery_reserve",
                    "no_charge_window",
                    "no_discharge_window",
                    "max_grid_window",
                    "no_op",
                ],
            },
            "structured_adjustment": {"type": "OBJECT", "nullable": True},
            "explanation": {"type": "STRING"},
        },
        "required": [
            "note_index",
            "applies",
            "directive_type",
            "structured_adjustment",
            "explanation",
        ],
    }
    return {
        "type": "ARRAY",
        "items": item_schema,
        "minItems": n_notes,
        "maxItems": n_notes,
    }


# ---------------------------------------------------------------------------
# Public interpreter function
# ---------------------------------------------------------------------------

def interpret_notes(request: ScenarioRequest) -> list[dict[str, Any]]:
    """Call Gemini to interpret operator notes.

    Returns a list of raw dicts — NOT yet validated by guardrails.
    Raises LLMInterpretationError on unrecoverable failures.
    """
    client = get_client()
    n_notes = len(request.operator_notes)
    user_prompt = _build_user_prompt(request)

    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=_build_response_schema(n_notes),
                temperature=0.0,   # fully deterministic
                max_output_tokens=2048,
            ),
        )

        raw_text = response.text
        logger.debug("LLM raw response: %s", raw_text)

        parsed: list[dict[str, Any]] = json.loads(raw_text)
        if not isinstance(parsed, list):
            raise LLMInterpretationError(
                f"LLM returned non-list JSON: {type(parsed).__name__}"
            )
        return parsed

    except json.JSONDecodeError as exc:
        logger.error("LLM returned non-JSON output: %s", exc)
        raise LLMInterpretationError(f"LLM JSON parse failure: {exc}") from exc
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
        raise LLMInterpretationError(f"LLM call error: {exc}") from exc
