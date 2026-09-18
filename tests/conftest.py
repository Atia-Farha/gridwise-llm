"""Pytest configuration and shared fixtures.

The model provider is always mocked here. The suite must run offline, be
deterministic, and cost nothing; the live model is measured separately by
`scripts/eval_interpretation.py`.
"""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")
os.environ.setdefault("REDIS_URL", "")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services import cache  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Keep cached interpretations from leaking between tests."""
    cache.clear_memory()
    yield
    cache.clear_memory()


@pytest.fixture(scope="session")
def public_cases() -> dict:
    path = Path(__file__).parent.parent / "sample_cases" / "public_cases.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def sample_cases(public_cases) -> list[dict]:
    return public_cases["cases"]


@contextmanager
def patch_llm(interpretations: list[dict] | None = None, *, error: Exception | None = None):
    """Patch the async interpreter with a fixed result or a raised error."""
    mock = AsyncMock(
        side_effect=error,
        return_value=interpretations if error is None else None,
    )
    with patch("app.services.llm_interpreter.interpret_notes", mock):
        yield mock


def no_op_interps(count: int) -> list[dict]:
    """Build `count` no_op interpretation entries."""
    return [
        {
            "note_index": i,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": "Irrelevant note.",
        }
        for i in range(count)
    ]
