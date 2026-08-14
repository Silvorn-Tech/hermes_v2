"""Basic package health checks."""

from fastapi.testclient import TestClient

from hermes_v2.api.app import app


def test_package_can_be_imported() -> None:
    import hermes_v2

    assert hermes_v2 is not None


def test_health_endpoint_returns_ok() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
