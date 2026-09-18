"""Tests for GET /health, /docs, /redoc, and /openapi.json."""


def test_health_returns_200(client):
    resp = client.get("/health")
    assert resp.status_code == 200


def test_health_body(client):
    resp = client.get("/health")
    assert resp.json() == {"status": "ok"}


def test_swagger_docs_returns_200(client):
    resp = client.get("/docs")
    assert resp.status_code == 200


def test_redoc_returns_200(client):
    resp = client.get("/redoc")
    assert resp.status_code == 200


def test_openapi_json_returns_200(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert "paths" in resp.json()
