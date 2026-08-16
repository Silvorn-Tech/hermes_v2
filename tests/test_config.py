"""Tests for hermes_v2.config — the shared HERMES_ALLOWED_ORIGINS source of
truth for both CORS and the trading Origin-header CSRF check."""

from __future__ import annotations

import pytest

from hermes_v2.config import configured_allowed_origins


def test_unset_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_ALLOWED_ORIGINS", raising=False)
    assert configured_allowed_origins() == []


def test_splits_and_strips_comma_separated_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "HERMES_ALLOWED_ORIGINS",
        " https://app.example.com , https://romeo.example.com ",
    )
    assert configured_allowed_origins() == [
        "https://app.example.com",
        "https://romeo.example.com",
    ]


def test_cors_middleware_and_origin_check_read_the_same_function() -> None:
    """The concrete regression guard: if a future edit reintroduces a
    second, separately-maintained copy of this parsing logic in either
    module, this test fails instead of the two silently diverging."""
    import hermes_v2.api.app as app_module
    import hermes_v2.trading.origin_check as origin_check_module
    from hermes_v2.config import configured_allowed_origins as canonical

    assert app_module._configured_allowed_origins is canonical
    assert origin_check_module.configured_allowed_origins is canonical
