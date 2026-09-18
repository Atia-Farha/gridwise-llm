.PHONY: install run test test-unit test-sample docker-build docker-run docker-up clean

# ── Local development ──────────────────────────────────────────────────────

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt

run:
	.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# ── Testing ────────────────────────────────────────────────────────────────

test:
	.venv/bin/pytest tests/ -v

test-unit:
	.venv/bin/pytest tests/test_health.py tests/test_guardrails.py tests/test_optimizer.py tests/test_api.py -v

test-sample:
	.venv/bin/pytest tests/test_sample_cases.py -v

# ── Docker ─────────────────────────────────────────────────────────────────

docker-build:
	docker build -t gridwise-llm:latest .

docker-run:
	docker run -d \
		-p 8000:8000 \
		-e GEMINI_API_KEY=$${GEMINI_API_KEY} \
		--name gridwise \
		gridwise-llm:latest

docker-up:
	docker compose up --build

docker-stop:
	docker compose down

# ── Utilities ──────────────────────────────────────────────────────────────

health:
	curl -s http://localhost:8000/health | python3 -m json.tool

sample-01:
	@python3 -c " \
import json, sys; \
cases = json.load(open('sample_cases/public_cases.json')); \
print(json.dumps(cases['cases'][0]['input'])) \
" | curl -s -X POST http://localhost:8000/optimize-energy \
		-H 'Content-Type: application/json' \
		-d @- | python3 -m json.tool

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
	find . -name "*.pyc" -delete 2>/dev/null; \
	rm -rf .pytest_cache .coverage htmlcov; \
	echo "Cleaned."
