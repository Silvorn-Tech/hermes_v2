"""Basic package health checks."""

import importlib
import sys

from fastapi.testclient import TestClient


def test_package_can_be_imported() -> None:
    import hermes_v2

    assert hermes_v2 is not None


def test_app_imports_without_database_url(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    sys.modules.pop("hermes_v2.api.app", None)
    sys.modules.pop("hermes_v2.auth.oauth", None)

    app_module = importlib.import_module("hermes_v2.api.app")

    assert app_module.app is not None


def test_health_endpoint_returns_ok(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    sys.modules.pop("hermes_v2.api.app", None)
    sys.modules.pop("hermes_v2.auth.oauth", None)

    app_module = importlib.import_module("hermes_v2.api.app")
    client = TestClient(app_module.app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
