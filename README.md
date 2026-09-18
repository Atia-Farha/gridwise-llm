# GridWise LLM — Smart Campus Energy Optimizer

**BUP CSE Fest 2026 Hackathon · Online Preliminary Round**

An HTTP API service that interprets natural-language campus operator notes using
**Google Gemini 3.6 Flash**, validates the extracted directives through
deterministic guardrails, and produces a cost-minimised 24-hour energy schedule
using a **Linear Programming** solver (Google OR-Tools GLOP).

---

## Architecture

```
POST /optimize-energy
         │
         ▼
┌─────────────────────┐
│  1. LLM Interpreter │  Gemini 3.6 Flash — structured JSON output
│     (untrusted)     │  Classifies each operator note into one of:
└────────┬────────────┘  solar_reduction | minimum_battery_reserve |
         │              no_charge_window | no_discharge_window |
         ▼              max_grid_window  | no_op
┌─────────────────────┐
│  2. Guardrail       │  Deterministic validation — all 12 rules:
│     Validator       │  type check, hours sanitisation, factor clamping,
│     (trusted)       │  applies semantics, shape verification, no-op padding
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  3. LP Optimizer    │  OR-Tools GLOP — globally optimal solution
│  (OR-Tools GLOP)    │  Variables: grid[h], solar_used[h], charge[h], discharge[h]
│                     │  Objective: minimise Σ grid[h] × tariff[h]
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  4. Replay          │  Independent hour-by-hour schedule verification
│     Validator       │  Catches any LP/directive discrepancy before response
└────────┬────────────┘
         │
         ▼
      JSON Response
```

---

## Quick Start (Local)

### Prerequisites

- Python 3.12+
- A Google Gemini API key ([get one free at Google AI Studio](https://aistudio.google.com/))

### 1. Clone and set up

```bash
git clone <your-repo-url> gridwise-llm
cd gridwise-llm

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and set your GEMINI_API_KEY
```

Required environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GEMINI_API_KEY` | **Yes** | — | Google Gemini API key |
| `GEMINI_MODEL` | No | `gemini-3.6-flash` | Gemini model name |
| `PORT` | No | `8000` | Server port |

### 3. Run the service

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
# or: make run
```

### 4. Verify health

```bash
curl http://localhost:8000/health
# Expected: {"status":"ok"}
```

### 5. Test with a public sample case

```bash
python3 -c "
import json
cases = json.load(open('sample_cases/public_cases.json'))
print(json.dumps(cases['cases'][0]['input'], indent=2))
" | curl -s -X POST http://localhost:8000/optimize-energy \
  -H 'Content-Type: application/json' \
  -d @- | python3 -m json.tool
```

---

## API Reference

### `GET /health`

Returns service readiness.

```json
{"status": "ok"}
```

### `POST /optimize-energy`

**Request body:**

```json
{
  "scenario_id": "GRID-101",
  "operator_notes": [
    "Solar output will drop to about 20% from 1 PM to 3 PM.",
    "Do not charge the battery between 2 PM and 4 PM.",
    "The cafeteria menu changes tomorrow."
  ],
  "hours": [
    {"hour": 0, "demand_kwh": 180, "solar_kwh": 0, "tariff_bdt_per_kwh": 7},
    "... 22 more hourly entries ...",
    {"hour": 23, "demand_kwh": 200, "solar_kwh": 0, "tariff_bdt_per_kwh": 9}
  ],
  "battery": {
    "capacity_kwh": 500,
    "initial_energy_kwh": 200,
    "minimum_energy_kwh": 50,
    "max_charge_kwh_per_hour": 100,
    "max_discharge_kwh_per_hour": 100
  }
}
```

**Response body:**

```json
{
  "scenario_id": "GRID-101",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
      "explanation": "Solar availability reduced to 20% during stated window."
    },
    {
      "note_index": 1,
      "applies": true,
      "directive_type": "no_charge_window",
      "structured_adjustment": {"hours": [14, 15]},
      "explanation": "Battery charging blocked during 2–4 PM window."
    },
    {
      "note_index": 2,
      "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "This note does not affect today's energy schedule."
    }
  ],
  "hourly_plan": [
    {
      "hour": 0,
      "grid_kwh": 180.0,
      "solar_used_kwh": 0.0,
      "battery_action": "idle",
      "battery_kwh": 0.0,
      "battery_energy_after_kwh": 200.0
    }
  ],
  "total_grid_kwh": 3240.5,
  "total_cost_bdt": 42815.0,
  "peak_grid_kwh": 280.0,
  "plan_summary": "Applied operator directives: solar reduced to 20% in hours [13, 14]; battery charging blocked in hours [14, 15]. Schedule minimises grid cost: total grid purchase 3240.50 kWh at 42815.00 BDT. Battery energy is restored to its initial level by end of day."
}
```

**HTTP status codes:**

| Code | Meaning |
|------|---------|
| `200` | Success |
| `422` | Invalid request body |
| `500` | Internal error (no secrets exposed) |

---

## Supported Directive Types

| Type | Effect | Structured Adjustment |
|------|--------|----------------------|
| `solar_reduction` | Scales usable solar to `factor` fraction | `{"hours": [...], "factor": 0.2}` |
| `minimum_battery_reserve` | Battery must stay ≥ threshold | `{"hours": [...], "minimum_energy_kwh": 120}` |
| `no_charge_window` | Battery charging = 0 | `{"hours": [...]}` |
| `no_discharge_window` | Battery discharging = 0 | `{"hours": [...]}` |
| `max_grid_window` | Grid import ≤ cap | `{"hours": [...], "max_grid_kwh": 80}` |
| `no_op` | No change | `null` |

**Time convention**: start-inclusive, end-exclusive.
`"1 PM to 3 PM"` → `hours: [13, 14]`

**Solar factor**: the *remaining* usable fraction.
`"80% reduction"` → `factor: 0.2`

---

## LLM Role & Guardrails

### LLM Role
Gemini 3.6 Flash interprets each `operator_note` into a structured directive
via a detailed system prompt with few-shot examples. It uses Gemini's native
`response_schema` parameter to force valid JSON output — no regex parsing.

### Guardrails (Deterministic)
LLM output is **always treated as untrusted** until the guardrail validator
passes it. The validator:
- Checks `directive_type` is in the allowed enum
- Enforces `applies = false` for `no_op`, `true` for all others
- Sanitises `hours`: unique integers 0–23, ascending
- Clamps `factor` to [0, 1] for `solar_reduction`
- Clamps `minimum_energy_kwh` to [0, capacity]
- Clamps `max_grid_kwh` to ≥ 0
- Verifies required fields per directive shape
- Pads missing note indices with `no_op`
- Falls back to all-`no_op` on LLM failure (safe failure, no crash)

### Optimizer
Google OR-Tools GLOP (simplex LP solver):
- 4 continuous decision variables per hour: `grid`, `solar_used`, `charge`, `discharge`
- Minimises `Σ grid[h] × tariff[h]` subject to energy balance, battery bounds,
  rate limits, end-of-day neutrality, and all active directive constraints
- Globally optimal (not heuristic)

---

## Running Tests

```bash
# All tests (unit + integration, mocked LLM)
pytest tests/ -v

# Unit tests only (no LLM, no network)
pytest tests/test_health.py tests/test_guardrails.py tests/test_optimizer.py tests/test_api.py -v

# Sample case integration tests (mocked LLM, real optimizer)
pytest tests/test_sample_cases.py -v
```

---

## Docker Fallback

### Build

```bash
docker build -t gridwise-llm:latest .
```

### Run

```bash
docker run -d \
  -p 8000:8000 \
  -e GEMINI_API_KEY=your_key_here \
  --name gridwise \
  gridwise-llm:latest
```

### Verify

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### Pull from registry (submitted image)

```bash
docker pull <REGISTRY>/gridwise-llm:<TAG>
docker run -d -p 8000:8000 -e GEMINI_API_KEY=your_key <REGISTRY>/gridwise-llm:<TAG>
```

> **Note**: The image binds to `0.0.0.0:8000` and contains no baked-in secrets.
> Provide `GEMINI_API_KEY` via `-e` flag only.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `fastapi` | HTTP API framework |
| `uvicorn[standard]` | ASGI server |
| `pydantic` v2 | Request/response schema validation |
| `pydantic-settings` | Environment variable loading |
| `google-genai` | Gemini 3.6 Flash SDK |
| `ortools` | Google OR-Tools GLOP LP solver |
| `python-dotenv` | `.env` file loading |
| `httpx` | Async HTTP client (for tests) |
| `pytest` | Test framework |

---

## Project Structure

```
gridwise-llm/
├── app/
│   ├── main.py               # FastAPI app, startup pre-warming
│   ├── config.py             # Pydantic-settings env config
│   ├── api/
│   │   └── router.py         # GET /health, POST /optimize-energy
│   ├── models/
│   │   ├── directives.py     # DirectiveType enum, ValidatedDirective
│   │   ├── request.py        # ScenarioRequest Pydantic model
│   │   └── response.py       # OptimizationResponse Pydantic model
│   ├── services/
│   │   ├── orchestrator.py   # Pipeline wiring
│   │   ├── llm_interpreter.py# Gemini call + prompt
│   │   ├── guardrails.py     # Deterministic 12-rule validator
│   │   ├── optimizer.py      # OR-Tools LP formulation
│   │   └── replay_validator.py # Post-solve schedule verification
│   └── utils/
│       ├── constants.py
│       └── exceptions.py
├── tests/                    # Unit + integration tests
├── sample_cases/             # 10 public sample cases JSON
├── Dockerfile                # Multi-stage build
├── docker-compose.yml        # Local development
├── requirements.txt
├── .env.example
└── Makefile
```

---

## Known Limitations

- LLM response time depends on Gemini API availability. Target p95 < 3 seconds.
- If the Gemini API is unavailable, all notes fall back to `no_op` (safe degradation).
  The optimizer still produces a valid (unconstrained) schedule.
- The LP solver assumes all organiser scenarios are feasible (per the problem statement).
  Mutually contradictory hard constraints will cause a 500 error.
- Grid export is not supported (solar surplus is curtailed per spec §9.4).

---

## Credits

- **Google OR-Tools** — LP/MILP solver (open source, Apache 2.0)
- **Google Gemini 3.6 Flash** — LLM for operator-note interpretation
- **FastAPI** — Python web framework
- **Pydantic v2** — Data validation
- Built for: **BUP CSE Fest 2026 Hackathon** · GridWise LLM Challenge
