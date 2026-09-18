"""LLM Interpreter — calls Gemini to interpret operator notes.

Model: configured via GEMINI_MODEL env var (default: gemini-3.6-flash).
Key fix: AFC (Automatic Function Calling) must be explicitly disabled.
Without it, generate_content hangs indefinitely on Flash models.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from google import genai
from google.genai import types

from app.config import settings
from app.models.request import ScenarioRequest
from app.utils.exceptions import LLMInterpretationError

logger = logging.getLogger(__name__)

_MAX_RETRIES = 5

# ---------------------------------------------------------------------------
# Gemini client (singleton)
# ---------------------------------------------------------------------------
_client: genai.Client | None = None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_INSTRUCTION = """\
You are an energy operations interpreter for a smart campus microgrid.
Analyse each operator note and return a JSON array.
Output ONLY the raw JSON array — no markdown fences, no extra text.

DIRECTIVE TYPES (use exactly these strings):
  solar_reduction          - reduces usable solar energy during specific hours
  minimum_battery_reserve  - battery must stay >= minimum kWh during specific hours
  no_charge_window         - battery charging blocked during specific hours
  no_discharge_window      - battery discharging blocked during specific hours
  max_grid_window          - grid import <= cap kWh during specific hours
  no_op                    - note does NOT affect the energy schedule

TIME RULE: hours are START-INCLUSIVE, END-EXCLUSIVE, whole integers 0-23.
  "1 PM to 3 PM"     -> [13, 14]
  "2 AM until 5 AM"  -> [2, 3, 4]
  "midnight to 3 AM" -> [0, 1, 2]
  "noon until 2 PM"  -> [12, 13]

SOLAR FACTOR: factor = REMAINING fraction (not the reduction).
  "80% reduction"  -> factor 0.2
  "drops to 25%"   -> factor 0.25
  "roughly 25%"    -> factor 0.25

RULES:
  no_op  -> applies=false, structured_adjustment=null
  others -> applies=true,  structured_adjustment object with "hours" array

structured_adjustment shapes:
  solar_reduction:         {"hours":[...], "factor": 0.X}
  minimum_battery_reserve: {"hours":[...], "minimum_energy_kwh": N}
  no_charge_window:        {"hours":[...]}
  no_discharge_window:     {"hours":[...]}
  max_grid_window:         {"hours":[...], "max_grid_kwh": N}
  no_op:                   null

EXAMPLE:
Input notes:
  Note 0: "Solar output will drop to about 20% from 1 PM to 3 PM."
  Note 1: "The cafeteria menu changes tomorrow."

Output (exactly this format, nothing else):
[{"note_index":0,"applies":true,"directive_type":"solar_reduction","structured_adjustment":{"hours":[13,14],"factor":0.2},"explanation":"Solar reduced to 20% from 1-3 PM."},{"note_index":1,"applies":false,"directive_type":"no_op","structured_adjustment":null,"explanation":"Irrelevant to energy schedule."}]
"""


def _build_user_prompt(request: ScenarioRequest) -> str:
    notes_block = "\n".join(
        f'Note {i}: "{note}"'
        for i, note in enumerate(request.operator_notes)
    )
    n = len(request.operator_notes)
    return (
        f"Battery capacity: {request.battery.capacity_kwh} kWh | "
        f"Minimum reserve: {request.battery.minimum_energy_kwh} kWh\n\n"
        f"{notes_block}\n\n"
        f"Return a JSON array with exactly {n} element(s). "
        f"Output ONLY valid JSON starting with [ and ending with ]."
    )


# ---------------------------------------------------------------------------
# Generation config — AFC disabled, plain text, no ThinkingConfig
# ---------------------------------------------------------------------------

def _make_config() -> types.GenerateContentConfig:
    """Build the generation config.

    CRITICAL: automatic_function_calling must be disabled.
    When AFC is enabled (the SDK default), generate_content hangs
    indefinitely on Flash models waiting for function call resolution.
    """
    return types.GenerateContentConfig(
        system_instruction=_SYSTEM_INSTRUCTION,
        temperature=0.0,
        max_output_tokens=4096,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            disable=True,
        ),
    )


# ---------------------------------------------------------------------------
# Response text extraction (skips thinking parts if any)
# ---------------------------------------------------------------------------

def _extract_text_from_response(response: Any) -> str | None:
    """Extract non-thought text from a Gemini response."""
    try:
        parts: list[str] = []
        for candidate in (response.candidates or []):
            for part in (getattr(candidate.content, "parts", None) or []):
                if getattr(part, "thought", False):
                    continue  # skip thinking tokens
                t = getattr(part, "text", None)
                if t:
                    parts.append(t)
        result = "".join(parts).strip()
        return result or None
    except Exception:
        pass
    try:
        t = response.text
        return t.strip() if t and t.strip() else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def _extract_json_array(text: str) -> list[dict[str, Any]]:
    """Extract a JSON array from model text using multiple strategies."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    # Strategy 1: full text is a valid JSON array
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    # Strategy 2: find outermost [...] block
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            parsed = json.loads(m.group())
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    # Strategy 3: collect individual {...} objects
    objects = re.findall(r"\{(?:[^{}]|\{[^{}]*\})*\}", text, re.DOTALL)
    if objects:
        try:
            parsed = [json.loads(o) for o in objects]
            if all(isinstance(o, dict) for o in parsed):
                return parsed
        except json.JSONDecodeError:
            pass

    raise LLMInterpretationError(
        f"Could not extract JSON array from response: {text[:400]!r}"
    )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def interpret_notes(request: ScenarioRequest) -> list[dict[str, Any]]:
    """Call Gemini to interpret operator notes into structured directives.

    Retries up to _MAX_RETRIES times with exponential backoff on transient
    errors (503 / high demand / empty output). Raises LLMInterpretationError
    on auth failures or when all retries are exhausted.
    """
    client = get_client()
    user_prompt = _build_user_prompt(request)
    config = _make_config()
    model = settings.gemini_model
    last_exc: Exception | None = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            logger.info("LLM attempt %d/%d model=%s", attempt, _MAX_RETRIES, model)

            response = client.models.generate_content(
                model=model,
                contents=user_prompt,
                config=config,
            )

            raw_text = _extract_text_from_response(response)
            if not raw_text:
                logger.warning("LLM attempt %d: empty response", attempt)
                last_exc = LLMInterpretationError("Empty response text")
                time.sleep(2 ** (attempt - 1))
                continue

            logger.debug("LLM raw: %.600s", raw_text)
            parsed = _extract_json_array(raw_text)
            logger.info("LLM attempt %d: OK — %d item(s)", attempt, len(parsed))
            return parsed

        except LLMInterpretationError:
            raise
        except Exception as exc:
            err_str = str(exc)
            logger.warning("LLM attempt %d exception: %s", attempt, err_str)
            last_exc = exc
            if any(k in err_str.lower() for k in ("permission", "api_key", "401", "403")):
                raise LLMInterpretationError(f"Auth error: {exc}") from exc
            if attempt < _MAX_RETRIES:
                backoff = min(2 ** (attempt - 1), 16)  # 1s, 2s, 4s, 8s, 16s
                logger.info("Backing off %ds before retry", backoff)
                time.sleep(backoff)

    raise LLMInterpretationError(
        f"All {_MAX_RETRIES} attempts failed. Last error: {last_exc}"
    ) from last_exc
