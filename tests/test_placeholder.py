"""Placeholder test to verify pytest works."""


def test_placeholder():
    """Basic test to verify pytest discovery and execution."""
    assert True, "Placeholder test should always pass"


def test_import_mailcore():
    """Verify mailcore can be imported."""
    from mailcore.protocols import IMAPConnection, SMTPConnection

    assert IMAPConnection is not None
    assert SMTPConnection is not None
