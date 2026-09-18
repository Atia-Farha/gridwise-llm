"""Pytest configuration and shared fixtures."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="session")
def public_cases() -> dict:
    path = Path(__file__).parent.parent / "sample_cases" / "public_cases.json"
    return json.loads(path.read_text())


@pytest.fixture(scope="session")
def sample_cases(public_cases) -> list[dict]:
    return public_cases["cases"]
