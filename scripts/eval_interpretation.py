#!/usr/bin/env python3
"""Score the live LLM interpreter against the 10 public sample cases.

This is the only check that exercises the real model on real note wording.
The pytest suite mocks the model so it can run offline and deterministically;
this script deliberately does not, because the 25-point interpretation
category is judged on what the model actually returns.

    python scripts/eval_interpretation.py                 # all 10 cases
    python scripts/eval_interpretation.py --case SAMPLE-03
    python scripts/eval_interpretation.py --model gpt-5.6-terra
    python scripts/eval_interpretation.py --repeat 3      # paraphrase stability

Exit code is 0 only when every field of every note matches ground truth.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOLERANCE = 0.01


def _load_cases() -> list[dict]:
    path = ROOT / "sample_cases" / "public_cases.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def _compare(actual: dict, expected: dict) -> list[str]:
    """Return a list of field-level mismatches for one note."""
    problems: list[str] = []

    if actual.get("directive_type") != expected["directive_type"]:
        problems.append(
            f"type: got {actual.get('directive_type')!r}, "
            f"want {expected['directive_type']!r}"
        )
    if actual.get("applies") != expected["applies"]:
        problems.append(
            f"applies: got {actual.get('applies')}, want {expected['applies']}"
        )

    exp_adj = expected["structured_adjustment"]
    got_adj = actual.get("structured_adjustment")

    if exp_adj is None:
        if got_adj is not None:
            problems.append(f"adjustment: expected null, got {got_adj}")
        return problems

    if not isinstance(got_adj, dict):
        problems.append(f"adjustment: expected an object, got {got_adj!r}")
        return problems

    if got_adj.get("hours") != exp_adj.get("hours"):
        problems.append(
            f"hours: got {got_adj.get('hours')}, want {exp_adj.get('hours')}"
        )

    for field in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
        if field not in exp_adj:
            continue
        want = exp_adj[field]
        got = got_adj.get(field)
        if got is None or abs(float(got) - float(want)) > TOLERANCE:
            problems.append(f"{field}: got {got}, want {want}")

    return problems


async def _run_case(case: dict) -> tuple[int, int, list[str]]:
    from app.models.request import ScenarioRequest
    from app.services import guardrails, llm_interpreter

    request = ScenarioRequest(**case["input"])
    raw = await llm_interpreter.interpret_notes(request)
    validated = guardrails.validate_interpretations(raw, request)

    actual = [
        {
            "note_index": v.note_index,
            "applies": v.applies,
            "directive_type": v.directive_type.value,
            "structured_adjustment": v.structured_adjustment,
        }
        for v in validated
    ]
    expected = case["expected_output"]["directive_interpretation"]

    failures: list[str] = []
    correct = 0
    for i, exp in enumerate(expected):
        got = actual[i] if i < len(actual) else {}
        problems = _compare(got, exp)
        if problems:
            note = case["input"]["operator_notes"][i]
            failures.append(f'  note {i}: "{note[:72]}…"')
            failures.extend(f"      {p}" for p in problems)
        else:
            correct += 1
    return correct, len(expected), failures


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="run a single case id, e.g. SAMPLE-03")
    parser.add_argument("--model", help="override OPENAI_MODEL for this run")
    parser.add_argument("--effort", help="override OPENAI_REASONING_EFFORT")
    parser.add_argument(
        "--repeat", type=int, default=1, help="run each case N times (stability)"
    )
    args = parser.parse_args()

    if args.model:
        os.environ["OPENAI_MODEL"] = args.model
    if args.effort:
        os.environ["OPENAI_REASONING_EFFORT"] = args.effort

    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY is not set.", file=sys.stderr)
        return 2

    from app.config import settings
    from app.services import cache

    # Force real model calls: a cache hit would measure the cache, not the model.
    cache.clear_memory()
    settings.redis_url = ""

    cases = _load_cases()
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"ERROR: no case named {args.case}", file=sys.stderr)
            return 2

    print(f"Model:  {settings.openai_model}")
    print(f"Effort: {settings.openai_reasoning_effort}")
    print(f"Cases:  {len(cases)} × {args.repeat} run(s)\n")

    total_correct = total_notes = 0
    perfect_cases = 0
    latencies: list[float] = []

    for case in cases:
        for run in range(args.repeat):
            cache.clear_memory()
            started = time.monotonic()
            try:
                correct, count, failures = await _run_case(case)
            except Exception as exc:  # noqa: BLE001
                print(f"{case['id']}  ERROR  {type(exc).__name__}: {exc}")
                total_notes += len(case["expected_output"]["directive_interpretation"])
                continue
            elapsed = time.monotonic() - started
            latencies.append(elapsed)

            total_correct += correct
            total_notes += count
            if correct == count:
                perfect_cases += 1

            tag = f"{case['id']}" + (f" #{run + 1}" if args.repeat > 1 else "")
            status = "PASS" if correct == count else "FAIL"
            print(f"{tag:<16} {status}  {correct}/{count} notes  {elapsed:5.2f}s")
            for line in failures:
                print(line)

    runs = len(cases) * args.repeat
    print("\n" + "─" * 58)
    print(f"Notes correct : {total_correct}/{total_notes}")
    print(f"Cases perfect : {perfect_cases}/{runs}")
    if latencies:
        ordered = sorted(latencies)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        print(f"Latency       : mean {sum(latencies)/len(latencies):.2f}s  p95 {p95:.2f}s")
        if p95 > 5:
            print("  ⚠ p95 above 5 s — the rubric's full latency credit needs ≤ 5 s.")
    print("─" * 58)

    if total_correct == total_notes and total_notes:
        print("All public notes interpreted correctly.")
        return 0
    print("Interpretation mismatches found — see the failures above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
