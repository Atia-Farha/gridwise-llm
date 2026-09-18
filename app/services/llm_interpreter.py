"""LLM Interpreter — converts operator notes into structured directives.

Provider: OpenAI Responses API.
Model:    configurable via OPENAI_MODEL (default: gpt-5.6-luna).

The model is the *only* component that reads natural language. It is called
with a strict JSON schema so the provider itself guarantees the response
shape — malformed directive types or missing fields become structurally
impossible rather than something guardrails must repair after the fact.

Everything the model returns is still treated as untrusted: values are
re-validated deterministically in `guardrails.py` before any of it reaches
the optimizer.

Latency is bounded on three levels — per-request timeout, bounded attempts,
and an overall wall-clock deadline — so this stage can never consume the
judge's 30 s per-request budget.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from openai import AsyncOpenAI

from app.config import settings
from app.models.request import ScenarioRequest
from app.utils.constants import ALLOWED_DIRECTIVE_TYPES
from app.utils.exceptions import LLMInterpretationError

logger = logging.getLogger(__name__)

# Reasoning models reject an explicit temperature; only send it when the
# configured effort is "none" (non-reasoning path).
_DETERMINISTIC_EFFORTS = {"none"}


# ---------------------------------------------------------------------------
# Client (singleton, async)
# ---------------------------------------------------------------------------
_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """Return the shared AsyncOpenAI client, creating it on first use."""
    global _client
    if _client is None:
        if not settings.openai_api_key:
            raise LLMInterpretationError("OPENAI_API_KEY is not configured")
        _client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.openai_timeout_seconds,
            max_retries=0,  # retries are managed here, under a hard deadline
        )
    return _client


def reset_client() -> None:
    """Drop the cached client (used by tests)."""
    global _client
    _client = None


# ---------------------------------------------------------------------------
# Strict response schema
# ---------------------------------------------------------------------------
# Every field is required and additionalProperties is false, as strict mode
# demands. Type-specific numbers are nullable and flattened onto the entry
# rather than nested in a polymorphic object — this keeps the schema strict-
# mode legal and removes a whole class of shape errors.

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "interpretations": {
            "type": "array",
            "description": "Exactly one entry per operator note, in note order.",
            "items": {
                "type": "object",
                "properties": {
                    "note_index": {
                        "type": "integer",
                        "description": "Zero-based index of the operator note.",
                    },
                    "directive_type": {
                        "type": "string",
                        "enum": sorted(ALLOWED_DIRECTIVE_TYPES),
                    },
                    "hours": {
                        "type": "array",
                        "description": (
                            "Affected hours as unique integers 0-23 in ascending "
                            "order. Empty array for no_op."
                        ),
                        "items": {"type": "integer"},
                    },
                    "factor": {
                        "type": ["number", "null"],
                        "description": (
                            "solar_reduction only: usable fraction of solar that "
                            "REMAINS (0-1). Null for every other directive type."
                        ),
                    },
                    "minimum_energy_kwh": {
                        "type": ["number", "null"],
                        "description": (
                            "minimum_battery_reserve only: required battery energy "
                            "floor in kWh. Null for every other directive type."
                        ),
                    },
                    "max_grid_kwh": {
                        "type": ["number", "null"],
                        "description": (
                            "max_grid_window only: hourly grid import cap in kWh. "
                            "Null for every other directive type."
                        ),
                    },
                    "explanation": {
                        "type": "string",
                        "description": "One short sentence explaining the reading.",
                    },
                },
                "required": [
                    "note_index",
                    "directive_type",
                    "hours",
                    "factor",
                    "minimum_energy_kwh",
                    "max_grid_kwh",
                    "explanation",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["interpretations"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_INSTRUCTION = """\
You interpret campus energy operator notes for a 24-hour microgrid scheduler.
For EVERY note you receive, emit exactly one entry, in the same order.

DIRECTIVE TYPES
  solar_reduction         usable solar is reduced during specific hours
  minimum_battery_reserve battery energy must stay at or above a floor
  no_charge_window        battery CHARGING is unavailable
  no_discharge_window     battery DISCHARGING is unavailable
  max_grid_window         grid import is capped per hour
  no_op                   the note does not affect this 24-hour schedule

TIME WINDOWS — start-inclusive, end-EXCLUSIVE, whole hours 0-23.
This applies to every phrasing: "from X to Y", "from X until Y",
"between X and Y", "during the X-Y window".
  "1 PM to 3 PM"        -> [13, 14]
  "noon until 2 PM"     -> [12, 13]
  "2 AM until 5 AM"     -> [2, 3, 4]
  "between 11 AM and 2 PM" -> [11, 12, 13]
  "6 PM until 10 PM"    -> [18, 19, 20, 21]
  "overnight 10 PM to 1 AM" -> [22, 23, 0]  (then sort ascending: [0, 22, 23])
Clock conversion: midnight/12 AM = 0, noon/12 PM = 12, 1 PM = 13, 11 PM = 23.
A bare "13:00 to 15:00" means [13, 14].
If a note gives no time range but clearly applies all day, use all 24 hours.

SOLAR FACTOR — `factor` is the fraction that REMAINS, never the loss.
  "drops to 25%" / "roughly 25% of forecast"      -> 0.25
  "80% reduction" / "down by 80%"                 -> 0.20
  "about half the forecast" / "drop by half"      -> 0.50
  "one-fifth of normal output"                    -> 0.20
  "panels offline" / "no solar"                   -> 0.0

BATTERY RESERVE — `minimum_energy_kwh` is absolute kWh.
When a note gives a percentage, multiply it by the battery capacity stated
in the user message. "at least 50% of capacity" with a 200 kWh battery -> 100.

GRID CAP — `max_grid_kwh` is the per-hour ceiling.
  "must not exceed 155 kWh", "stay at or below 190 kWh",
  "limit of 180 kWh of grid import" -> that number.

CHARGE vs DISCHARGE — read carefully, they are different directives.
  "charger isolated", "charging circuit unavailable", "do not charge"
      -> no_charge_window
  "must not discharge", "no battery output", "do not draw from the battery"
      -> no_discharge_window

no_op — use it for anything that does not change today's energy schedule:
administrative notices, deadlines, room bookings, menus, events, or works
scheduled for a different day/week/month. Do NOT invent a directive to make
an irrelevant note fit. Prefer no_op when a note names no actionable energy
constraint for today.

FIELD DISCIPLINE
  solar_reduction         -> hours + factor;              others null
  minimum_battery_reserve -> hours + minimum_energy_kwh;  others null
  no_charge_window        -> hours only;                  all numbers null
  no_discharge_window     -> hours only;                  all numbers null
  max_grid_window         -> hours + max_grid_kwh;        others null
  no_op                   -> hours = [];                  all numbers null
Never invent demand, tariff, solar or battery parameters. Never emit a
directive type outside the list above. `hours` must always be unique
integers 0-23 sorted ascending.
"""

_FEW_SHOT_USER = """\
Battery: capacity 200 kWh, base minimum reserve 40 kWh.

Note 0: "Facilities will wash the rooftop panels from noon until 2 PM; treat usable solar as roughly 25% of forecast."
Note 1: "Keep at least 50% of battery capacity in reserve from 6 PM until 9 PM."
Note 2: "The sports office moved next month's registration deadline."\
"""

_FEW_SHOT_ASSISTANT = json.dumps(
    {
        "interpretations": [
            {
                "note_index": 0,
                "directive_type": "solar_reduction",
                "hours": [12, 13],
                "factor": 0.25,
                "minimum_energy_kwh": None,
                "max_grid_kwh": None,
                "explanation": "Panel cleaning leaves 25% of forecast solar for hours 12-13.",
            },
            {
                "note_index": 1,
                "directive_type": "minimum_battery_reserve",
                "hours": [18, 19, 20],
                "factor": None,
                "minimum_energy_kwh": 100.0,
                "max_grid_kwh": None,
                "explanation": "50% of the 200 kWh battery is a 100 kWh floor for hours 18-20.",
            },
            {
                "note_index": 2,
                "directive_type": "no_op",
                "hours": [],
                "factor": None,
                "minimum_energy_kwh": None,
                "max_grid_kwh": None,
                "explanation": "An administrative deadline does not affect today's schedule.",
            },
        ]
    },
    separators=(",", ":"),
)


def _build_user_prompt(request: ScenarioRequest) -> str:
    notes_block = "\n".join(
        f'Note {i}: "{note}"' for i, note in enumerate(request.operator_notes)
    )
    n = len(request.operator_notes)
    return (
        f"Battery: capacity {request.battery.capacity_kwh} kWh, "
        f"base minimum reserve {request.battery.minimum_energy_kwh} kWh.\n\n"
        f"{notes_block}\n\n"
        f"Return exactly {n} interpretation entr{'y' if n == 1 else 'ies'}, "
        f"one per note, with note_index 0..{n - 1}."
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _extract_output_text(response: Any) -> str | None:
    """Pull the assistant's text out of a Responses API result."""
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    # Fallback: walk the output items, skipping reasoning blocks.
    chunks: list[str] = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) == "reasoning":
            continue
        for part in getattr(item, "content", None) or []:
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str):
                chunks.append(part_text)
    joined = "".join(chunks).strip()
    return joined or None


def _to_raw_entries(payload: str) -> list[dict[str, Any]]:
    """Parse the JSON payload into the flat entry list guardrails expect."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise LLMInterpretationError(f"Model returned non-JSON output: {exc}") from exc

    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("interpretations")
    else:
        entries = None

    if not isinstance(entries, list):
        raise LLMInterpretationError("Model output missing 'interpretations' array")

    return [e for e in entries if isinstance(e, dict)]


def _rebuild_structured_adjustment(entry: dict[str, Any]) -> dict[str, Any]:
    """Re-nest the flattened model output into the response-schema shape.

    The flat schema is easier for the model to satisfy; the public API
    contract requires a nested `structured_adjustment`. Guardrails still
    re-validate every value after this translation.
    """
    dtype = entry.get("directive_type")
    hours = entry.get("hours") or []

    if dtype == "no_op" or dtype not in ALLOWED_DIRECTIVE_TYPES:
        adjustment: dict[str, Any] | None = None
    else:
        adjustment = {"hours": hours}
        if dtype == "solar_reduction":
            adjustment["factor"] = entry.get("factor")
        elif dtype == "minimum_battery_reserve":
            adjustment["minimum_energy_kwh"] = entry.get("minimum_energy_kwh")
        elif dtype == "max_grid_window":
            adjustment["max_grid_kwh"] = entry.get("max_grid_kwh")

    return {
        "note_index": entry.get("note_index"),
        "directive_type": dtype,
        "structured_adjustment": adjustment,
        "explanation": entry.get("explanation", ""),
    }


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def interpret_notes(request: ScenarioRequest) -> list[dict[str, Any]]:
    """Interpret operator notes into raw structured directives.

    Retries transient provider errors under a wall-clock deadline. Raises
    LLMInterpretationError once the deadline or attempt budget is spent so
    the caller can fall back safely.
    """
    client = get_client()
    started = time.monotonic()
    deadline = started + settings.llm_deadline_seconds
    last_error: Exception | None = None

    kwargs: dict[str, Any] = {
        "model": settings.openai_model,
        "input": [
            {"role": "system", "content": _SYSTEM_INSTRUCTION},
            {"role": "user", "content": _FEW_SHOT_USER},
            {"role": "assistant", "content": _FEW_SHOT_ASSISTANT},
            {"role": "user", "content": _build_user_prompt(request)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "operator_note_interpretation",
                "strict": True,
                "schema": _RESPONSE_SCHEMA,
            }
        },
    }
    effort = settings.openai_reasoning_effort
    if effort in _DETERMINISTIC_EFFORTS:
        kwargs["temperature"] = 0.0
    else:
        kwargs["reasoning"] = {"effort": effort}

    for attempt in range(1, settings.openai_max_attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            logger.warning("LLM deadline exhausted before attempt %d", attempt)
            break

        try:
            response = await asyncio.wait_for(
                client.responses.create(**kwargs),
                timeout=min(settings.openai_timeout_seconds, remaining),
            )
            payload = _extract_output_text(response)
            if not payload:
                raise LLMInterpretationError("Model returned an empty response")

            entries = _to_raw_entries(payload)
            logger.info(
                "LLM attempt %d/%d ok — %d entr(y/ies) in %.2fs",
                attempt,
                settings.openai_max_attempts,
                len(entries),
                time.monotonic() - started,
            )
            return [_rebuild_structured_adjustment(e) for e in entries]

        except asyncio.TimeoutError as exc:
            last_error = exc
            logger.warning("LLM attempt %d timed out", attempt)
        except Exception as exc:  # noqa: BLE001 — provider errors are opaque
            last_error = exc
            # Never log the exception body: it can echo request content.
            logger.warning(
                "LLM attempt %d failed: %s", attempt, type(exc).__name__
            )
            if _is_fatal(exc):
                break

        # Short, deadline-aware backoff. Long exponential sleeps would eat
        # the judge's per-request budget and turn a retry into a timeout.
        backoff = min(0.5 * (2 ** (attempt - 1)), 2.0)
        if time.monotonic() + backoff >= deadline:
            break
        await asyncio.sleep(backoff)

    raise LLMInterpretationError(
        f"Interpretation failed after {settings.openai_max_attempts} attempt(s): "
        f"{type(last_error).__name__ if last_error else 'deadline exceeded'}"
    )


def _is_fatal(exc: Exception) -> bool:
    """True for errors that retrying cannot fix (auth, bad request)."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in {400, 401, 403, 404}:
        return True
    return isinstance(exc, LLMInterpretationError)
