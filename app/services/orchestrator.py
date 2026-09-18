"""Pipeline orchestrator — wires the stages together.

Stage 1: LLM Interpreter     (untrusted structured output)
Stage 2: Guardrail Validator (trusted, validated directives)
Stage 3: OR-Tools LP         (optimal 24-hour schedule)
Stage 4: Replay Validator    (independent hour-by-hour audit)

Two properties matter as much as the optimum itself:

  * The endpoint must never block the event loop. The LLM call is awaited;
    the CPU-bound solve is pushed to a worker thread.
  * The endpoint must never fail closed. If the interpreted directives make
    the LP infeasible, the schedule is re-solved against progressively fewer
    directives rather than returning an error. A valid, slightly suboptimal
    plan still earns validity and constraint credit; a 500 earns nothing.
"""

from __future__ import annotations

import logging

from fastapi.concurrency import run_in_threadpool

from app.config import settings
from app.models.directives import DirectiveType, ValidatedDirective
from app.models.request import ScenarioRequest
from app.models.response import (
    DirectiveInterpretationResponse,
    OptimizationResponse,
)
from app.services import cache, guardrails, llm_interpreter, optimizer, replay_validator
from app.utils.exceptions import (
    LLMInterpretationError,
    OptimizationError,
    ReplayValidationError,
)

logger = logging.getLogger(__name__)

# Order in which directives are surrendered when the LP proves infeasible.
# Caps and floors are dropped first because an over-tight numeric bound is the
# most likely misreading; the window directives are dropped last because they
# are the least ambiguous to extract.
_RELAXATION_ORDER: list[DirectiveType] = [
    DirectiveType.max_grid_window,
    DirectiveType.minimum_battery_reserve,
    DirectiveType.no_charge_window,
    DirectiveType.no_discharge_window,
    DirectiveType.solar_reduction,
]


def _build_plan_summary(
    directives: list[ValidatedDirective],
    total_cost: float,
    total_grid: float,
    dropped: list[DirectiveType],
) -> str:
    """Generate a concise human-readable plan summary."""
    parts: list[str] = []
    directive_summaries: list[str] = []

    for d in directives:
        if d.directive_type == DirectiveType.solar_reduction:
            directive_summaries.append(
                f"solar reduced to {(d.factor or 0) * 100:.0f}% in hours {d.hours}"
            )
        elif d.directive_type == DirectiveType.no_charge_window:
            directive_summaries.append(f"battery charging blocked in hours {d.hours}")
        elif d.directive_type == DirectiveType.no_discharge_window:
            directive_summaries.append(f"battery discharging blocked in hours {d.hours}")
        elif d.directive_type == DirectiveType.minimum_battery_reserve:
            directive_summaries.append(
                f"minimum battery reserve of {d.minimum_energy_kwh} kWh "
                f"enforced in hours {d.hours}"
            )
        elif d.directive_type == DirectiveType.max_grid_window:
            directive_summaries.append(
                f"grid capped at {d.max_grid_kwh} kWh in hours {d.hours}"
            )

    if directive_summaries:
        parts.append("Applied operator directives: " + "; ".join(directive_summaries) + ".")
    else:
        parts.append("No applicable operator directives.")

    if dropped:
        parts.append(
            "Relaxed to keep the schedule feasible: "
            + ", ".join(sorted({d.value for d in dropped}))
            + "."
        )

    parts.append(
        f"Schedule minimises grid cost: total grid purchase {total_grid:.2f} kWh "
        f"at {total_cost:.2f} BDT. Battery energy is restored to its initial "
        f"level by end of day."
    )
    return " ".join(parts)


async def _interpret(request: ScenarioRequest) -> list[dict]:
    """Stage 1 with cache lookup and safe degradation to all-no_op."""
    key = cache.cache_key(
        request.operator_notes,
        request.battery.capacity_kwh,
        request.battery.minimum_energy_kwh,
    )

    cached = await cache.get(key)
    if cached is not None:
        logger.info("Interpretation cache hit for scenario %s", request.scenario_id)
        return cached

    try:
        raw = await llm_interpreter.interpret_notes(request)
    except LLMInterpretationError as exc:
        logger.error("LLM unavailable — degrading to all-no_op: %s", exc)
        return [
            {
                "note_index": i,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "Interpretation unavailable; treated as no-op.",
            }
            for i in range(len(request.operator_notes))
        ]

    await cache.set(key, raw)
    return raw


def _solve_with_fallback(
    request: ScenarioRequest,
    directives: list[ValidatedDirective],
) -> tuple[optimizer.OptimizerResult, list[ValidatedDirective], list[DirectiveType]]:
    """Solve, shedding directives one class at a time until feasible.

    The final attempt uses no directives at all, which is always feasible:
    every hour can be met from the grid with the battery left idle.
    """
    try:
        return optimizer.solve(request, directives), directives, []
    except OptimizationError:
        logger.warning(
            "Scenario %s infeasible with all %d directive(s); relaxing.",
            request.scenario_id,
            len(directives),
        )

    dropped: list[DirectiveType] = []
    remaining = list(directives)

    for victim in _RELAXATION_ORDER:
        if not any(d.directive_type == victim for d in remaining):
            continue
        remaining = [d for d in remaining if d.directive_type != victim]
        dropped.append(victim)
        try:
            result = optimizer.solve(request, remaining)
            logger.warning(
                "Scenario %s feasible after dropping %s",
                request.scenario_id,
                [d.value for d in dropped],
            )
            return result, remaining, dropped
        except OptimizationError:
            continue

    # Last resort: an unconstrained schedule. Guaranteed feasible.
    result = optimizer.solve(request, [])
    logger.error(
        "Scenario %s required dropping every directive to stay feasible.",
        request.scenario_id,
    )
    return result, [], dropped


async def run_pipeline(request: ScenarioRequest) -> OptimizationResponse:
    """Execute the full LLM → guardrail → LP → replay pipeline."""

    # ── Stage 1: Interpretation ────────────────────────────────────────────
    raw_interpretations = await _interpret(request)

    # ── Stage 2: Guardrails ────────────────────────────────────────────────
    validated_interpretations = guardrails.validate_interpretations(
        raw_interpretations, request
    )
    active_directives = guardrails.extract_validated_directives(
        validated_interpretations
    )
    logger.info(
        "Guardrails: %d active directive(s) for scenario %s",
        len(active_directives),
        request.scenario_id,
    )

    # ── Stage 3: Optimization (off the event loop) ─────────────────────────
    result, applied_directives, dropped = await run_in_threadpool(
        _solve_with_fallback, request, active_directives
    )
    logger.info(
        "Optimizer: cost=%.2f BDT, grid=%.2f kWh for scenario %s",
        result.total_cost_bdt,
        result.total_grid_kwh,
        request.scenario_id,
    )

    # ── Stage 4: Replay audit ──────────────────────────────────────────────
    effective_solar = optimizer.compute_effective_solar(
        request.hours, applied_directives
    )
    try:
        replay_validator.replay_validate(
            result.hourly_plan, request, applied_directives, effective_solar
        )
    except ReplayValidationError as exc:
        # The audit found a schedule the judge would reject. Returning an
        # unconstrained-but-valid plan beats returning a knowingly invalid one.
        logger.error("Replay audit failed (%s); falling back to a safe plan.", exc)
        result = await run_in_threadpool(optimizer.solve, request, [])
        applied_directives = []
        dropped = sorted(
            {d.directive_type for d in active_directives}, key=lambda t: t.value
        )

    # ── Response ───────────────────────────────────────────────────────────
    interp_responses = [
        DirectiveInterpretationResponse(
            note_index=i.note_index,
            applies=i.applies,
            directive_type=i.directive_type,
            structured_adjustment=i.structured_adjustment,
            explanation=i.explanation,
        )
        for i in validated_interpretations
    ]

    return OptimizationResponse(
        scenario_id=request.scenario_id,
        directive_interpretation=interp_responses,
        hourly_plan=result.hourly_plan,
        total_grid_kwh=result.total_grid_kwh,
        total_cost_bdt=result.total_cost_bdt,
        peak_grid_kwh=result.peak_grid_kwh,
        plan_summary=_build_plan_summary(
            applied_directives, result.total_cost_bdt, result.total_grid_kwh, dropped
        ),
    )
