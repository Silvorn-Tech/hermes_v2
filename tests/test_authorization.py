"""Tests for require_permission() RBAC enforcement.

Two things are under test:

1. `require_permission()` itself, in isolation, against a throwaway test
   app — never the real `hermes_v2.api.app`. It never touches a real
   database (the session factory is faked).
2. A regression guard over the *real* app: every mutating route
   (POST/PUT/PATCH/DELETE) must either depend on `require_permission()` or
   appear in `_PERMISSION_EXEMPT_MUTATING_ROUTES` with a documented reason.
   This is what catches a future endpoint shipping unprotected.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from hermes_v2.api.app import app as real_app
from hermes_v2.auth.authorization import require_permission

# A real, seeded permission (hermes_v2.auth.seed.PERMISSION_CATALOG) used
# purely as a fixture value in these tests — not a business meaning.
_TEST_PERMISSION = "orders.cancel"


def _build_test_app() -> FastAPI:
    test_app = FastAPI()

    @test_app.post("/test/protected")
    async def protected_endpoint(
        current_user: dict = Depends(require_permission(_TEST_PERMISSION)),
    ) -> dict:
        return {"ok": True, "user": current_user}

    return test_app


@pytest.fixture(autouse=True)
def fake_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """require_permission() tests never touch a real database."""

    class FakeSession:
        def __enter__(self) -> "FakeSession":
            return self

        def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
            return False

    monkeypatch.setattr(
        "hermes_v2.auth.authorization._create_session_factory",
        lambda: FakeSession,
    )


def _user_with_permission(permission_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="user-with-permission",
        email="trader@example.com",
        display_name="Trader",
        roles=[
            SimpleNamespace(
                name="TRADER",
                permissions=[SimpleNamespace(name=permission_name)],
            )
        ],
    )


def test_authenticated_user_with_permission_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "hermes_v2.auth.authorization.get_user_from_session",
        lambda session, token: _user_with_permission(_TEST_PERMISSION),
    )
    client = TestClient(_build_test_app())

    response = client.post("/test/protected", cookies={"hermes_session": "valid"})

    assert response.status_code == 200
    assert response.json()["user"]["email"] == "trader@example.com"


def test_authenticated_user_without_permission_is_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_permission_user = _user_with_permission("orders.read")
    monkeypatch.setattr(
        "hermes_v2.auth.authorization.get_user_from_session",
        lambda session, token: other_permission_user,
    )
    client = TestClient(_build_test_app())

    response = client.post("/test/protected", cookies={"hermes_session": "valid"})

    assert response.status_code == 403
    assert "insufficient" in response.json()["detail"].lower()


def test_authenticated_user_with_no_roles_is_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_roles_user = SimpleNamespace(
        id="user-no-roles",
        email="noroles@example.com",
        display_name=None,
        roles=[],
    )
    monkeypatch.setattr(
        "hermes_v2.auth.authorization.get_user_from_session",
        lambda session, token: no_roles_user,
    )
    client = TestClient(_build_test_app())

    response = client.post("/test/protected", cookies={"hermes_session": "valid"})

    assert response.status_code == 403


def test_missing_session_cookie_is_unauthorized() -> None:
    client = TestClient(_build_test_app())

    response = client.post("/test/protected")

    assert response.status_code == 401


def test_invalid_session_token_is_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hermes_v2.auth.authorization.get_user_from_session",
        lambda session, token: None,
    )
    client = TestClient(_build_test_app())

    response = client.post("/test/protected", cookies={"hermes_session": "bad-token"})

    assert response.status_code == 401


def test_unauthenticated_request_never_reaches_the_permission_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """401 must win over 403 — an anonymous caller learns nothing about
    what permission the endpoint requires."""
    called = {"value": False}

    def fail_if_called(session: object, token: str) -> None:
        called["value"] = True
        return None

    monkeypatch.setattr(
        "hermes_v2.auth.authorization.get_user_from_session", fail_if_called
    )
    client = TestClient(_build_test_app())

    response = client.post("/test/protected")

    assert response.status_code == 401
    assert called["value"] is False


def test_unknown_permission_is_rejected_at_construction_time() -> None:
    """A fictitious permission (not in PERMISSION_CATALOG) must fail
    immediately when the dependency is built, not silently at request
    time — this is what stops an invented name like "bots.pause" from
    ever protecting (or failing to protect) a real endpoint."""
    with pytest.raises(ValueError, match="bots.pause"):
        require_permission("bots.pause")


def test_known_permissions_all_construct_successfully() -> None:
    from hermes_v2.auth.seed import PERMISSION_CATALOG

    for permission_name in PERMISSION_CATALOG:
        require_permission(permission_name)


# --- Regression guard over the real app -----------------------------------

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Every mutating route NOT gated by require_permission() must be listed
# here with a reason. A new mutable endpoint that is neither
# permission-gated nor listed here fails
# test_every_mutating_route_is_permission_gated_or_exempt below.
_PERMISSION_EXEMPT_MUTATING_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/auth/logout"): (
        "Self-service: revokes only the caller's own session. Every "
        "authenticated user may end their own session regardless of "
        "role — gated by authentication (401) alone, not a permission."
    ),
}


def _permission_names_for_route(route: object) -> set[str]:
    """Walk a route's dependency tree for require_permission() markers."""
    names: set[str] = set()
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return names

    stack = [dependant]
    while stack:
        current = stack.pop()
        call = getattr(current, "call", None)
        permission_name = getattr(call, "__hermes_permission__", None)
        if permission_name is not None:
            names.add(permission_name)
        stack.extend(getattr(current, "dependencies", None) or [])
    return names


def test_every_mutating_route_is_permission_gated_or_exempt() -> None:
    unguarded: list[tuple[str, str]] = []

    for route in real_app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or path is None:
            continue

        for method in methods & _MUTATING_METHODS:
            key = (method, path)
            if key in _PERMISSION_EXEMPT_MUTATING_ROUTES:
                continue
            if not _permission_names_for_route(route):
                unguarded.append(key)

    assert unguarded == [], (
        "Mutating routes with no require_permission() dependency and no "
        f"documented exemption in _PERMISSION_EXEMPT_MUTATING_ROUTES: {unguarded}"
    )


def test_exempt_routes_still_exist_in_the_app() -> None:
    """Catches a stale exemption entry left behind after a route is
    renamed or removed — the exemption list should track real routes."""
    real_routes = {
        (method, getattr(route, "path", None))
        for route in real_app.routes
        for method in getattr(route, "methods", None) or set()
    }

    for exempt_key in _PERMISSION_EXEMPT_MUTATING_ROUTES:
        assert exempt_key in real_routes, (
            f"{exempt_key} is listed as exempt but no longer exists as a route"
        )
