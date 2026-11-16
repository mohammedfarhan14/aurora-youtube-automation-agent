from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_root_endpoint() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["application"] == "Aurora Studio"
    assert response.json()["status"] == "running"


def test_liveness_endpoint() -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_endpoint() -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_returns_503_when_database_is_unavailable(
    monkeypatch,
) -> None:
    def raise_database_error():
        raise RuntimeError("Database is unavailable.")

    monkeypatch.setattr("app.main.get_engine", raise_database_error)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "Database is unavailable."}
