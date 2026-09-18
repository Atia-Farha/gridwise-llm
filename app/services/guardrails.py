"""Deterministic guardrail validator.

Treats LLM output as UNTRUSTED and applies all 12 validation rules from
the PRD / spec §08.  Returns fully-validated structures that the optimizer
can safely consume.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from app.models.directives import (
    DirectiveInterpretation,
    DirectiveType,
    ValidatedDirective,
)
from app.models.request import ScenarioRequest
from app.utils.constants import ALLOWED_DIRECTIVE_TYPES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Required structured_adjustment fields per directive type
# ---------------------------------------------------------------------------

_REQUIRED_ADJUSTMENT_FIELDS: dict[str, set[str]] = {
    "solar_reduction": {"hours", "factor"},
    "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
    "no_charge_window": {"hours"},
    "no_discharge_window": {"hours"},
    "max_grid_window": {"hours", "max_grid_kwh"},
}


def _safe_hours(raw: Any) -> list[int]:
    """Sanitise a raw hours value into a valid ascending list of unique ints 0-23."""
    if not isinstance(raw, list):
        return []
    cleaned: list[int] = []
    seen: set[int] = set()
    for h in raw:
        try:
            hi = int(h)
        except (TypeError, ValueError):
            continue
        if 0 <= hi <= 23 and hi not in seen:
            cleaned.append(hi)
            seen.add(hi)
    return sorted(cleaned)


def _is_finite_non_negative(v: Any) -> bool:
    try:
        f = float(v)
        return math.isfinite(f) and f >= 0
    except (TypeError, ValueError):
        return False


def _validate_single_raw(
    raw: dict[str, Any],
    note_index: int,
    request: ScenarioRequest,
) -> DirectiveInterpretation:
    """Validate and sanitise one raw LLM interpretation dict.

    Returns a corrected DirectiveInterpretation regardless of how bad the
    LLM output was — by defaulting to no_op on unrecoverable errors.
    """
    explanation: str = str(raw.get("explanation", "")).strip() or "Interpreted by LLM."

    # Rule 4: directive_type must be in allowed set
    raw_type = raw.get("directive_type", "")
    if raw_type not in ALLOWED_DIRECTIVE_TYPES:
        logger.warning(
            "note %d: unknown directive_type %r — defaulting to no_op", note_index, raw_type
        )
        return DirectiveInterpretation(
            note_index=note_index,
            applies=False,
            directive_type=DirectiveType.no_op,
            structured_adjustment=None,
            explanation=explanation,
        )

    dtype = DirectiveType(raw_type)

    # Rule 6: no_op must have null adjustment
    if dtype == DirectiveType.no_op:
        return DirectiveInterpretation(
            note_index=note_index,
            applies=False,
            directive_type=DirectiveType.no_op,
            structured_adjustment=None,
            explanation=explanation,
        )

    # For all non-no_op: validate structured_adjustment
    raw_adj: Any = raw.get("structured_adjustment")

    # Rule 11: adjustment must be a dict with required fields
    if not isinstance(raw_adj, dict):
        logger.warning(
            "note %d: %s missing structured_adjustment dict — defaulting to no_op",
            note_index, dtype,
        )
        return DirectiveInterpretation(
            note_index=note_index,
            applies=False,
            directive_type=DirectiveType.no_op,
            structured_adjustment=None,
            explanation=explanation,
        )

    required_fields = _REQUIRED_ADJUSTMENT_FIELDS.get(dtype.value, set())
    missing = required_fields - raw_adj.keys()
    if missing:
        logger.warning(
            "note %d: %s missing required adjustment fields %s — defaulting to no_op",
            note_index, dtype, missing,
        )
        return DirectiveInterpretation(
            note_index=note_index,
            applies=False,
            directive_type=DirectiveType.no_op,
            structured_adjustment=None,
            explanation=explanation,
        )

    # Rule 7: hours must be unique ints 0-23 in ascending order
    hours = _safe_hours(raw_adj.get("hours", []))

    # Build sanitised adjustment dict
    sanitised_adj: dict[str, Any] = {"hours": hours}

    if dtype == DirectiveType.solar_reduction:
        # Rule 8: factor must be 0 <= factor <= 1
        factor = raw_adj.get("factor", 1.0)
        try:
            factor = float(factor)
        except (TypeError, ValueError):
            factor = 1.0
        factor = max(0.0, min(1.0, factor))
        sanitised_adj["factor"] = factor

    elif dtype == DirectiveType.minimum_battery_reserve:
        # Rule 9: 0 <= minimum_energy_kwh <= capacity_kwh
        raw_min = raw_adj.get("minimum_energy_kwh", 0.0)
        if not _is_finite_non_negative(raw_min):
            raw_min = 0.0
        min_kwh = float(raw_min)
        capacity = request.battery.capacity_kwh
        min_kwh = max(0.0, min(capacity, min_kwh))
        sanitised_adj["minimum_energy_kwh"] = min_kwh

    elif dtype == DirectiveType.max_grid_window:
        # Rule 10: max_grid_kwh must be >= 0, finite
        raw_cap = raw_adj.get("max_grid_kwh", 0.0)
        if not _is_finite_non_negative(raw_cap):
            raw_cap = 0.0
        sanitised_adj["max_grid_kwh"] = float(raw_cap)

    # no_charge_window / no_discharge_window: only hours needed (already added)

    return DirectiveInterpretation(
        note_index=note_index,
        applies=True,
        directive_type=dtype,
        structured_adjustment=sanitised_adj,
        explanation=explanation,
    )


# ---------------------------------------------------------------------------
# Public guardrail function
# ---------------------------------------------------------------------------

def validate_interpretations(
    raw_list: list[dict[str, Any]],
    request: ScenarioRequest,
) -> list[DirectiveInterpretation]:
    """Apply all guardrail rules to the raw LLM output.

    Always returns exactly len(request.operator_notes) interpretations in
    note_index order 0 … N-1, regardless of what the LLM produced.
    """
    n_notes = len(request.operator_notes)
    result: dict[int, DirectiveInterpretation] = {}

    for raw_item in raw_list:
        if not isinstance(raw_item, dict):
            continue

        # Rule 2 & 3: note_index must be a valid unique int
        try:
            idx = int(raw_item.get("note_index", -1))
        except (TypeError, ValueError):
            idx = -1

        if idx < 0 or idx >= n_notes:
            logger.warning("guardrail: invalid note_index %r — skipping", idx)
            continue

        if idx in result:
            logger.warning("guardrail: duplicate note_index %d — keeping first", idx)
            continue

        result[idx] = _validate_single_raw(raw_item, idx, request)

    # Rule 1: pad any missing indices with no_op
    for i in range(n_notes):
        if i not in result:
            logger.warning("guardrail: note_index %d missing — inserting no_op", i)
            result[i] = DirectiveInterpretation(
                note_index=i,
                applies=False,
                directive_type=DirectiveType.no_op,
                structured_adjustment=None,
                explanation="Interpretation not available; treated as no-op.",
            )

    # Return in ascending note_index order
    return [result[i] for i in range(n_notes)]


# ---------------------------------------------------------------------------
# Build validated directives for the optimizer
# ---------------------------------------------------------------------------

def extract_validated_directives(
    interpretations: list[DirectiveInterpretation],
) -> list[ValidatedDirective]:
    """Convert validated DirectiveInterpretation list → ValidatedDirective list
    for consumption by the LP optimizer.  Skips no_op entries.
    """
    directives: list[ValidatedDirective] = []
    for interp in interpretations:
        if not interp.applies or interp.directive_type == DirectiveType.no_op:
            continue
        adj = interp.structured_adjustment or {}
        vd = ValidatedDirective(
            directive_type=interp.directive_type,
            hours=adj.get("hours", []),
            factor=adj.get("factor"),
            minimum_energy_kwh=adj.get("minimum_energy_kwh"),
            max_grid_kwh=adj.get("max_grid_kwh"),
        )
        directives.append(vd)
    return directives
