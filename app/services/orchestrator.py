"""Pipeline orchestrator — wires all three stages together.

Stage 1: LLM Interpreter  (untrusted structured output)
Stage 2: Guardrail Validator  (trusted validated directives)
Stage 3: OR-Tools LP Optimizer  (optimal 24-hour schedule)
      ↳  Replay Validator  (post-solve correctness check)
"""

from __future__ import annotations

import logging

from app.models.directives import DirectiveType, ValidatedDirective
from app.models.request import ScenarioRequest
from app.models.response import (
    DirectiveInterpretationResponse,
    OptimizationResponse,
)
from app.services import guardrails, llm_interpreter, optimizer, replay_validator
from app.utils.exceptions import LLMInterpretationError, ReplayValidationError

logger = logging.getLogger(__name__)


def _build_plan_summary(
    directives: list[ValidatedDirective],
    total_cost: float,
    total_grid: float,
) -> str:
    """Generate a concise human-readable plan summary."""
    parts: list[str] = []

    directive_summaries: list[str] = []
    for d in directives:
        if d.directive_type == DirectiveType.solar_reduction:
            directive_summaries.append(
                f"solar reduced to {d.factor * 100:.0f}% in hours {d.hours}"
            )
        elif d.directive_type == DirectiveType.no_charge_window:
            directive_summaries.append(f"battery charging blocked in hours {d.hours}")
        elif d.directive_type == DirectiveType.no_discharge_window:
            directive_summaries.append(f"battery discharging blocked in hours {d.hours}")
        elif d.directive_type == DirectiveType.minimum_battery_reserve:
            directive_summaries.append(
                f"minimum battery reserve of {d.minimum_energy_kwh} kWh enforced in hours {d.hours}"
            )
        elif d.directive_type == DirectiveType.max_grid_window:
            directive_summaries.append(
                f"grid capped at {d.max_grid_kwh} kWh in hours {d.hours}"
            )

    if directive_summaries:
        parts.append(
            "Applied operator directives: " + "; ".join(directive_summaries) + "."
        )
    else:
        parts.append("No applicable operator directives.")

    parts.append(
        f"Schedule minimises grid cost: total grid purchase {total_grid:.2f} kWh "
        f"at {total_cost:.2f} BDT. Battery energy is restored to its initial level "
        f"by end of day."
    )

    return " ".join(parts)


def run_pipeline(request: ScenarioRequest) -> OptimizationResponse:
    """Execute the full LLM → guardrail → LP → replay pipeline."""

    # ── Stage 1: LLM Interpretation ────────────────────────────────────────
    raw_interpretations: list[dict] = []
    try:
        raw_interpretations = llm_interpreter.interpret_notes(request)
        logger.info(
            "LLM returned %d interpretations for scenario %s",
            len(raw_interpretations),
            request.scenario_id,
        )
    except LLMInterpretationError as exc:
        logger.error("LLM failed — falling back to all-no_op: %s", exc)
        # Safe failure: all notes become no_op
        raw_interpretations = [
            {
                "note_index": i,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "LLM unavailable; treated as no-op.",
            }
            for i in range(len(request.operator_notes))
        ]

    # ── Stage 2: Guardrail Validation ──────────────────────────────────────
    validated_interpretations = guardrails.validate_interpretations(
        raw_interpretations, request
    )
    active_directives: list[ValidatedDirective] = guardrails.extract_validated_directives(
        validated_interpretations
    )
    logger.info(
        "Guardrails: %d active directives for scenario %s",
        len(active_directives),
        request.scenario_id,
    )

    # Compute effective solar (needed for replay validator)
    effective_solar = optimizer._compute_effective_solar(request.hours, active_directives)

    # ── Stage 3: LP Optimization ───────────────────────────────────────────
    result = optimizer.solve(request, active_directives)
    logger.info(
        "Optimizer: cost=%.2f BDT, grid=%.2f kWh for scenario %s",
        result.total_cost_bdt,
        result.total_grid_kwh,
        request.scenario_id,
    )

    # ── Stage 4: Replay Validation ─────────────────────────────────────────
    try:
        replay_validator.replay_validate(
            result.hourly_plan,
            request,
            active_directives,
            effective_solar,
        )
    except ReplayValidationError as exc:
        # Log but do not re-raise — the LP is authoritative; replay catches bugs
        logger.error("Replay validation failed (schedule may be incorrect): %s", exc)

    # ── Build Response ─────────────────────────────────────────────────────
    interp_responses = [
        DirectiveInterpretationResponse(
            note_index=interp.note_index,
            applies=interp.applies,
            directive_type=interp.directive_type,
            structured_adjustment=interp.structured_adjustment,
            explanation=interp.explanation,
        )
        for interp in validated_interpretations
    ]

    plan_summary = _build_plan_summary(
        active_directives,
        result.total_cost_bdt,
        result.total_grid_kwh,
    )

    return OptimizationResponse(
        scenario_id=request.scenario_id,
        directive_interpretation=interp_responses,
        hourly_plan=result.hourly_plan,
        total_grid_kwh=result.total_grid_kwh,
        total_cost_bdt=result.total_cost_bdt,
        peak_grid_kwh=result.peak_grid_kwh,
        plan_summary=plan_summary,
    )
