import pytest

from puffo_agent.agent.harness.support.redaction import safe_provider_message


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("Authorization: Basic dXNlcjpwYXNz, request failed", "dXNlcjpwYXNz"),
        ("Authorization=Digest response=deadbeef, denied", "response=deadbeef"),
        ('{"authorization":"Basic dXNlcjpwYXNz","status":401}', "dXNlcjpwYXNz"),
    ],
)
def test_safe_provider_message_redacts_complete_authorization_value(
    message, secret
):
    diagnostic = safe_provider_message(message)

    assert secret not in diagnostic
    assert "[REDACTED]" in diagnostic
