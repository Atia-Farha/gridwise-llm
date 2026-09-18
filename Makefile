.PHONY: help install run test test-sample eval verify docker-build docker-run compose-up compose-down health sample clean

PY ?= python
PORT ?= 8000
BASE ?= http://localhost:$(PORT)
IMAGE ?= gridwise-llm:latest

help:
	@echo "install       Create .venv and install dependencies"
	@echo "run           Start the API on port $(PORT)"
	@echo "test          Run the offline test suite"
	@echo "eval          Score the live model on the 10 public cases (needs OPENAI_API_KEY)"
	@echo "verify        Score a running deployment: make verify BASE=https://host"
	@echo "health        Call GET /health"
	@echo "sample        POST the first public sample case"
	@echo "docker-build  Build the container image"
	@echo "compose-up    Start the local stack (API + Redis)"

# ── Local development ──────────────────────────────────────────────────────

install:
	$(PY) -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install -r requirements.txt

run:
	uvicorn app.main:app --host 0.0.0.0 --port $(PORT)

dev:
	uvicorn app.main:app --host 0.0.0.0 --port $(PORT) --reload

# ── Testing ────────────────────────────────────────────────────────────────

test:
	pytest tests/ -q

test-sample:
	pytest tests/test_sample_cases.py -v

# Score a running deployment end-to-end. Needs no local key — the service has one.
#   make verify BASE=https://your-host
verify:
	$(PY) scripts/verify_deployment.py $(BASE)

# Live-model accuracy check in-process. The suite above mocks the provider; this does not.
eval:
	$(PY) scripts/eval_interpretation.py

# ── Docker ─────────────────────────────────────────────────────────────────

docker-build:
	docker build -t $(IMAGE) .

docker-run:
	docker run --rm -p $(PORT):8000 -e OPENAI_API_KEY=$$OPENAI_API_KEY $(IMAGE)

compose-up:
	docker compose -f docker-compose.local.yml up --build

compose-down:
	docker compose -f docker-compose.local.yml down

# ── Verification ───────────────────────────────────────────────────────────

health:
	curl -fsS $(BASE)/health && echo

sample:
	@$(PY) -c "import json;print(json.dumps(json.load(open('sample_cases/public_cases.json'))['cases'][0]['input']))" \
		| curl -fsS -X POST $(BASE)/optimize-energy -H 'Content-Type: application/json' -d @- \
		| $(PY) -m json.tool

clean:
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache .coverage htmlcov
	@echo "Cleaned."
