"""Basic package health checks."""


def test_package_can_be_imported() -> None:
    import hermes_v2

    assert hermes_v2 is not None
