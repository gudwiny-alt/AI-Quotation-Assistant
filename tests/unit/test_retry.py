from __future__ import annotations

import threading
import time

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from quote_app.tasks.retry import (
    CaptureError,
    CoordinateConversionError,
    ForegroundControlError,
    LayoutRecognitionError,
    LoginRequired,
    NonRetryableTechnicalError,
    RetryPolicy,
    RETRYABLE_ERROR_CODES,
    RetryableTechnicalError,
    SchedulerControl,
    SecurityVerificationRequired,
    classify_attempt_error,
)


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (PlaywrightTimeoutError("page timed out"), "PLAYWRIGHT_TIMEOUT"),
        (
            PlaywrightError("Page.goto: net::ERR_CONNECTION_RESET"),
            "NETWORK_ERROR",
        ),
        (ConnectionError("connection reset"), "NETWORK_ERROR"),
        (LayoutRecognitionError("price card missing"), "LAYOUT_CHANGED"),
        (CoordinateConversionError("screen origin unavailable"), "COORDINATE_FAILED"),
        (ForegroundControlError("wrong foreground window"), "FOREGROUND_FAILED"),
        (CaptureError("screen recording denied"), "CAPTURE_FAILED"),
    ],
)
def test_browser_failures_receive_stable_retryable_codes(
    error: BaseException,
    expected_code: str,
) -> None:
    classified = classify_attempt_error(error)

    assert isinstance(classified, RetryableTechnicalError)
    assert classified.code == expected_code
    assert classified.retry_cost == 1


def test_explicit_non_retryable_error_keeps_its_stable_code() -> None:
    error = NonRetryableTechnicalError("SITE_NOT_SUPPORTED", "站点暂不支持")

    assert classify_attempt_error(error) is error
    assert error.retry_cost == 1


@pytest.mark.parametrize(
    "error",
    [
        LoginRequired("天猫", "登录后才能查看价格"),
        SecurityVerificationRequired("天猫", "需要人工完成安全验证"),
    ],
)
def test_login_and_security_verification_do_not_consume_retry_budget(
    error: BaseException,
) -> None:
    classified = classify_attempt_error(error)

    assert classified is error
    assert classified.retry_cost == 0


def test_login_site_rejects_credential_shaped_text() -> None:
    with pytest.raises(ValueError, match="credentials"):
        LoginRequired("token=secret", "需要登录")


def test_default_policy_allows_initial_attempt_plus_two_technical_retries() -> None:
    policy = RetryPolicy()

    assert policy.can_start_technical_attempt(0)
    assert policy.can_start_technical_attempt(1)
    assert policy.can_start_technical_attempt(2)
    assert not policy.can_start_technical_attempt(3)


@pytest.mark.parametrize(
    "delay",
    [float("nan"), float("inf"), float("-inf")],
)
def test_retry_delay_must_be_finite(delay: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        RetryPolicy(retry_delay_seconds=delay)


def test_sensitive_or_malformed_technical_code_is_rejected() -> None:
    with pytest.raises(ValueError, match="code"):
        RetryableTechnicalError("token=secret", "网络连接失败")


def test_only_transient_capture_codes_are_in_persisted_retry_allowlist() -> None:
    assert {
        "CAPTURE_BLANK",
        "CAPTURE_UNREADABLE",
        "CAPTURE_OBSCURED",
        "CAPTURE_UNSTABLE",
        "CAPTURE_GEOMETRY",
    } <= RETRYABLE_ERROR_CODES
    assert {
        "EVIDENCE_MISSING",
        "EVIDENCE_HASH_MISMATCH",
    } <= RETRYABLE_ERROR_CODES
    assert {
        "CAPTURE_PERMISSION",
        "CAPTURE_ENVIRONMENT",
    }.isdisjoint(RETRYABLE_ERROR_CODES)


@pytest.mark.parametrize("control_action", ["pause", "stop"])
def test_retry_delay_is_promptly_interrupted_by_pause_or_stop(
    control_action: str,
) -> None:
    control = SchedulerControl()
    policy = RetryPolicy(retry_delay_seconds=5.0)

    timer = threading.Timer(
        0.05,
        control.request_pause
        if control_action == "pause"
        else control.request_stop,
    )
    started = time.monotonic()
    timer.start()
    try:
        completed = policy.wait_before_retry(control)
    finally:
        timer.cancel()

    assert not completed
    assert time.monotonic() - started < 0.5
