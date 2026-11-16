from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app


def test_metrics_endpoint_is_hidden_when_disabled(monkeypatch):
    monkeypatch.setattr("app.main.settings", Settings(_env_file=None))
    response = TestClient(app).get("/metrics")
    assert response.status_code == 404


def test_metrics_endpoint_requires_bearer_token(monkeypatch):
    monkeypatch.setattr(
        "app.main.settings",
        Settings(
            _env_file=None,
            metrics_enabled=True,
            metrics_bearer_token="m" * 40,
        ),
    )
    response = TestClient(app).get("/metrics")
    assert response.status_code == 401


def test_metrics_endpoint_returns_aggregate_workflow_metrics(monkeypatch):
    token = "m" * 40
    monkeypatch.setattr(
        "app.main.settings",
        Settings(
            _env_file=None,
            metrics_enabled=True,
            metrics_bearer_token=token,
        ),
    )
    response = TestClient(app).get(
        "/metrics",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert "aurora_research_runs_recent" in response.text
    assert "aurora_production_stages_recent" in response.text
    assert "aurora_worker_expired_leases" in response.text
    assert "aurora_production_stage_latency_seconds_avg" in response.text
    assert "aurora_youtube_publish_failures_recent" in response.text
    assert "aurora_worker_failures_recent" in response.text
    assert "aurora_youtube_upload_failures_recent" in response.text
