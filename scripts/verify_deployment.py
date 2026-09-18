#!/usr/bin/env python3
"""End-to-end verification of a running GridWise deployment.

Unlike `eval_interpretation.py`, which imports the app and needs a local
OPENAI_API_KEY, this drives a live URL over HTTP. The deployed service holds
the key, so this is the practical way to score the full pipeline — the model's
interpretation *and* the schedule it produces — exactly as a judge would.

For every public case it checks:
  * one interpretation entry per note, in note_index order
  * directive_type, applies, hours and numeric values against ground truth
  * the returned plan replayed hour by hour: energy balance, effective solar,
    battery bounds and rate limits, directive compliance, end-of-day neutrality
  * reported totals recomputed from hourly_plan
  * cost ratio against the reference optimum

    python scripts/verify_deployment.py https://your-host
    python scripts/verify_deployment.py http://localhost:8000 --case SAMPLE-03

Exit code 0 only when every case passes every check.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows consoles default to cp1252; keep printing safe on any terminal.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
TOL = 0.01

# Some proxies/WAFs reject the default python-urllib agent with a 403.
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "gridwise-verify/1.0",
}


def _load_cases() -> list[dict]:
    path = ROOT / "sample_cases" / "public_cases.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def _post(base: str, payload: dict, timeout: float) -> tuple[int, dict, float]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/optimize-energy",
        data=body,
        headers=_HEADERS,
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read()), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return exc.code, {"detail": exc.read().decode("utf-8", "replace")[:200]}, (
            time.monotonic() - started
        )


# ---------------------------------------------------------------------------
# Interpretation scoring
# ---------------------------------------------------------------------------

def check_interpretation(got: list[dict], want: list[dict]) -> list[str]:
    problems: list[str] = []

    if len(got) != len(want):
        return [f"expected {len(want)} entries, got {len(got)}"]
    if [e.get("note_index") for e in got] != list(range(len(want))):
        problems.append(
            f"note_index order wrong: {[e.get('note_index') for e in got]}"
        )

    for i, exp in enumerate(want):
        g = got[i]
        prefix = f"note {i}"

        if g.get("directive_type") != exp["directive_type"]:
            problems.append(
                f"{prefix} type: got {g.get('directive_type')!r}, "
                f"want {exp['directive_type']!r}"
            )
        if g.get("applies") != exp["applies"]:
            problems.append(
                f"{prefix} applies: got {g.get('applies')}, want {exp['applies']}"
            )

        exp_adj, got_adj = exp["structured_adjustment"], g.get("structured_adjustment")
        if exp_adj is None:
            if got_adj is not None:
                problems.append(f"{prefix} adjustment: expected null, got {got_adj}")
            continue
        if not isinstance(got_adj, dict):
            problems.append(f"{prefix} adjustment: expected object, got {got_adj!r}")
            continue
        if got_adj.get("hours") != exp_adj.get("hours"):
            problems.append(
                f"{prefix} hours: got {got_adj.get('hours')}, want {exp_adj['hours']}"
            )
        for field in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
            if field not in exp_adj:
                continue
            g_val = got_adj.get(field)
            if g_val is None or abs(float(g_val) - float(exp_adj[field])) > TOL:
                problems.append(
                    f"{prefix} {field}: got {g_val}, want {exp_adj[field]}"
                )
    return problems


# ---------------------------------------------------------------------------
# Schedule replay — the same checks the judge applies
# ---------------------------------------------------------------------------

def check_schedule(resp: dict, case_input: dict, want: list[dict]) -> list[str]:
    problems: list[str] = []
    plan = resp.get("hourly_plan") or []
    battery = case_input["battery"]
    hour_map = {h["hour"]: h for h in case_input["hours"]}

    if sorted(e["hour"] for e in plan) != list(range(24)):
        return ["hourly_plan must contain exactly hours 0-23"]

    # Ground-truth directives, not the team's reported reading.
    eff_solar = {h: hour_map[h]["solar_kwh"] for h in range(24)}
    floor = {h: battery["minimum_energy_kwh"] for h in range(24)}
    grid_cap: dict[int, float] = {}
    no_charge: set[int] = set()
    no_discharge: set[int] = set()
    for d in want:
        adj = d["structured_adjustment"]
        if not adj:
            continue
        for h in adj["hours"]:
            t = d["directive_type"]
            if t == "solar_reduction":
                eff_solar[h] = hour_map[h]["solar_kwh"] * adj["factor"]
            elif t == "minimum_battery_reserve":
                floor[h] = max(floor[h], adj["minimum_energy_kwh"])
            elif t == "max_grid_window":
                grid_cap[h] = min(grid_cap.get(h, float("inf")), adj["max_grid_kwh"])
            elif t == "no_charge_window":
                no_charge.add(h)
            elif t == "no_discharge_window":
                no_discharge.add(h)

    energy = battery["initial_energy_kwh"]
    for entry in sorted(plan, key=lambda e: e["hour"]):
        h = entry["hour"]
        action = entry["battery_action"]
        mag = entry["battery_kwh"]
        charge = mag if action == "charge" else 0.0
        discharge = mag if action == "discharge" else 0.0

        if action not in ("charge", "discharge", "idle"):
            problems.append(f"h{h}: bad battery_action {action!r}")
        if action == "idle" and mag > TOL:
            problems.append(f"h{h}: idle with battery_kwh={mag}")
        for field in ("grid_kwh", "solar_used_kwh", "battery_kwh"):
            if entry[field] < -TOL:
                problems.append(f"h{h}: negative {field}={entry[field]}")

        lhs = entry["grid_kwh"] + entry["solar_used_kwh"] + discharge
        rhs = hour_map[h]["demand_kwh"] + charge
        if abs(lhs - rhs) > TOL:
            problems.append(f"h{h}: energy balance {lhs:.3f} != {rhs:.3f}")
        if entry["solar_used_kwh"] > eff_solar[h] + TOL:
            problems.append(
                f"h{h}: solar {entry['solar_used_kwh']} > effective {eff_solar[h]}"
            )
        if charge > battery["max_charge_kwh_per_hour"] + TOL:
            problems.append(f"h{h}: charge {charge} over rate limit")
        if discharge > battery["max_discharge_kwh_per_hour"] + TOL:
            problems.append(f"h{h}: discharge {discharge} over rate limit")
        if h in no_charge and charge > TOL:
            problems.append(f"h{h}: no_charge_window violated (charge={charge})")
        if h in no_discharge and discharge > TOL:
            problems.append(f"h{h}: no_discharge_window violated (discharge={discharge})")
        if h in grid_cap and entry["grid_kwh"] > grid_cap[h] + TOL:
            problems.append(
                f"h{h}: max_grid_window violated ({entry['grid_kwh']} > {grid_cap[h]})"
            )

        energy += charge - discharge
        if abs(energy - entry["battery_energy_after_kwh"]) > TOL:
            problems.append(
                f"h{h}: battery_energy_after {entry['battery_energy_after_kwh']} "
                f"!= replayed {energy:.3f}"
            )
        if energy < floor[h] - TOL:
            problems.append(f"h{h}: battery {energy:.2f} below floor {floor[h]}")
        if energy > battery["capacity_kwh"] + TOL:
            problems.append(f"h{h}: battery {energy:.2f} over capacity")

    if abs(energy - battery["initial_energy_kwh"]) > TOL:
        problems.append(
            f"end-of-day battery {energy:.2f} != initial {battery['initial_energy_kwh']}"
        )

    # Reported totals must match a recomputation from hourly_plan.
    grid = sum(e["grid_kwh"] for e in plan)
    cost = sum(e["grid_kwh"] * hour_map[e["hour"]]["tariff_bdt_per_kwh"] for e in plan)
    peak = max(e["grid_kwh"] for e in plan)
    for name, recalc in (
        ("total_grid_kwh", grid),
        ("total_cost_bdt", cost),
        ("peak_grid_kwh", peak),
    ):
        if abs(resp.get(name, 0) - recalc) > TOL:
            problems.append(f"{name}={resp.get(name)} != recomputed {recalc:.2f}")

    if resp.get("scenario_id") != case_input["scenario_id"]:
        problems.append("scenario_id not echoed")
    if not str(resp.get("plan_summary", "")).strip():
        problems.append("plan_summary empty")

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("base_url", help="e.g. https://your-host or http://localhost:8000")
    ap.add_argument("--case", help="run a single case id")
    ap.add_argument("--timeout", type=float, default=30.0, help="per-request timeout")
    args = ap.parse_args()

    cases = _load_cases()
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"no case named {args.case}", file=sys.stderr)
            return 2

    print(f"Target: {args.base_url}")
    try:
        health_req = urllib.request.Request(
            args.base_url.rstrip("/") + "/health", headers=_HEADERS
        )
        with urllib.request.urlopen(health_req, timeout=args.timeout) as r:
            print(f"Health: {r.status} {json.loads(r.read())}\n")
    except Exception as exc:  # noqa: BLE001
        print(f"Health check failed: {exc}", file=sys.stderr)
        return 2

    notes_ok = notes_total = 0
    cases_interp_ok = cases_valid = 0
    latencies: list[float] = []
    ratios: list[float] = []
    failures: list[str] = []

    print(f"{'case':<11} {'interp':>8} {'sched':>7} {'ratio':>8} {'time':>7}")
    print("-" * 46)

    for case in cases:
        want = case["expected_output"]["directive_interpretation"]
        notes_total += len(want)

        status, resp, elapsed = _post(args.base_url, case["input"], args.timeout)
        latencies.append(elapsed)

        if status != 200:
            failures.append(f"{case['id']}: HTTP {status} — {resp.get('detail')}")
            print(f"{case['id']:<11} {'HTTP ' + str(status):>8}")
            continue

        got = resp.get("directive_interpretation") or []
        iproblems = check_interpretation(got, want)
        sproblems = check_schedule(resp, case["input"], want)

        per_note_ok = sum(
            1
            for i in range(len(want))
            if not any(p.startswith(f"note {i} ") for p in iproblems)
        )
        notes_ok += per_note_ok
        if not iproblems:
            cases_interp_ok += 1
        if not sproblems:
            cases_valid += 1

        ref = case["expected_output"]["total_cost_bdt"]
        ours = resp.get("total_cost_bdt", 0.0)
        ratio = (ours / ref) if ref > TOL else 1.0
        if not sproblems:
            ratios.append(min(1.0, ref / ours) if ours > TOL else 1.0)

        print(
            f"{case['id']:<11} {per_note_ok}/{len(want):<6} "
            f"{'OK' if not sproblems else 'FAIL':>7} {ratio:>8.5f} {elapsed:>6.2f}s"
        )
        for p in iproblems:
            failures.append(f"{case['id']} interp: {p}")
            print(f"    interp: {p}")
        for p in sproblems[:8]:
            failures.append(f"{case['id']} schedule: {p}")
            print(f"    schedule: {p}")

    n = len(cases)
    ordered = sorted(latencies)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] if ordered else 0.0
    quality = (sum(ratios) / len(ratios)) if ratios else 0.0

    print("-" * 46)
    print(f"Notes correct        : {notes_ok}/{notes_total}")
    print(f"Cases interp-perfect : {cases_interp_ok}/{n}")
    print(f"Cases schedule-valid : {cases_valid}/{n}")
    print(f"Cost quality (avg)   : {quality:.5f}  -> {10 * quality:.2f}/10 pts")
    print(f"Latency              : mean {sum(latencies)/len(latencies):.2f}s  p95 {p95:.2f}s")
    if p95 > 5:
        print("  ! p95 above 5s — full latency credit needs <= 5s")
    print("-" * 46)

    if failures:
        print(f"\n{len(failures)} problem(s) found.")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
