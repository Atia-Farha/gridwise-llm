"""Tests for GET /health."""


def test_health_returns_200(client):
    resp = client.get("/health")
    assert resp.status_code == 200


def test_health_body(client):
    resp = client.get("/health")
    assert resp.json() == {"status": "ok"}
