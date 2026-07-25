from coach.privacy_logger import log_sanitized_cleanup_error, log_sanitized_error


def test_error_logs_never_include_exception_or_health_values(caplog):
    secrets = (
        "raw prompt",
        "calendar title",
        "HRV 65",
        "raw response",
    )
    with caplog.at_level("WARNING"):
        log_sanitized_error("timeout", user_id="user", http_status=504)
        try:
            raise RuntimeError(" ".join(secrets))
        except RuntimeError as exc:
            log_sanitized_cleanup_error("gemini_client", exc)
    combined = caplog.text
    assert "timeout" in combined
    assert "RuntimeError" in combined
    for secret in secrets:
        assert secret not in combined
