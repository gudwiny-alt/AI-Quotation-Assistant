from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass


class AttemptError(RuntimeError):
    """A classified failure produced by one browser attempt."""

    retry_cost: int


class LoginRequired(AttemptError):
    retry_cost = 0

    def __init__(self, site: str, reason: str) -> None:
        self.site = _required_text(site, "site")
        if contains_explicit_credentials(self.site):
            raise ValueError("site must not contain credentials")
        self.reason = _required_text(reason, "reason")
        super().__init__(self.reason)


class SecurityVerificationRequired(LoginRequired):
    """A CAPTCHA or risk-control page that requires manual user action."""


class TechnicalError(AttemptError):
    retry_cost = 1

    def __init__(self, code: str, message: str) -> None:
        self.code = _required_text(code, "code")
        if not _TECHNICAL_CODE_PATTERN.fullmatch(self.code):
            raise ValueError("code must be an uppercase stable identifier")
        self.message = _required_text(message, "message")
        super().__init__(self.message)


class RetryableTechnicalError(TechnicalError):
    """A technical failure that may be attempted again within the budget."""


class NonRetryableTechnicalError(TechnicalError):
    """A technical failure that should be persisted without another attempt."""


_SAFE_LAYOUT_STAGES = frozenset(
    {
        "京东店铺页",
        "京东搜索页",
        "京东商品详情页",
        "天猫店铺页",
        "天猫搜索页",
        "天猫商品详情页",
    }
)


class LayoutRecognitionError(RuntimeError):
    """The expected product-page layout could not be recognized."""

    def __init__(self, message: str, *, stage: str | None = None) -> None:
        if stage is not None and stage not in _SAFE_LAYOUT_STAGES:
            raise ValueError("layout stage must be an approved safe label")
        self.stage = stage
        super().__init__(message)


class CoordinateConversionError(RuntimeError):
    """DOM coordinates could not be converted to screen coordinates."""


class ForegroundControlError(RuntimeError):
    """The managed browser could not be made or kept foreground."""


class CaptureError(RuntimeError):
    """Operating-system evidence capture failed."""


_RETRYABLE_CLASSIFICATIONS: tuple[
    tuple[type[BaseException], str, str],
    ...,
] = (
    (TimeoutError, "PLAYWRIGHT_TIMEOUT", "网页操作等待超时"),
    (ConnectionError, "NETWORK_ERROR", "网站网络连接失败"),
    (LayoutRecognitionError, "LAYOUT_CHANGED", "网页结构无法识别"),
    (CoordinateConversionError, "COORDINATE_FAILED", "截图坐标换算失败"),
    (ForegroundControlError, "FOREGROUND_FAILED", "浏览器前台窗口控制失败"),
    (CaptureError, "CAPTURE_FAILED", "屏幕证据截图失败"),
)

_RETRYABLE_CAPTURE_CODES = frozenset(
    {
        "CAPTURE_BLANK",
        "CAPTURE_UNREADABLE",
        "CAPTURE_OBSCURED",
        "CAPTURE_UNSTABLE",
        "CAPTURE_GEOMETRY",
    }
)
_RETRYABLE_EVIDENCE_AUDIT_CODES = frozenset(
    {
        "EVIDENCE_MISSING",
        "EVIDENCE_HASH_MISMATCH",
    }
)
RETRYABLE_ERROR_CODES = (
    frozenset(
        code for _error_type, code, _message in _RETRYABLE_CLASSIFICATIONS
    )
    | _RETRYABLE_CAPTURE_CODES
    | _RETRYABLE_EVIDENCE_AUDIT_CODES
)
_STABLE_ERROR_MESSAGES = {
    code: message for _error_type, code, message in _RETRYABLE_CLASSIFICATIONS
}
_NON_BUDGET_ATTEMPT_CODES = frozenset(
    {
        "LOGIN_REQUIRED",
        "SECURITY_VERIFICATION_REQUIRED",
        "PROCESS_INTERRUPTED",
        "TASK_PAUSED",
    }
)
_EXPLICIT_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\bauthorization\s*:\s*\S+"),
    re.compile(r"(?i)\b(?:set-cookie|cookie)\s*:\s*\S+"),
    re.compile(
        r"(?i)\b(?:password|passwd|access_token|refresh_token|id_token|"
        r"api_token|auth_token|token)\s*[:=]\s*\S+"
    ),
)
_TECHNICAL_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def classify_attempt_error(error: BaseException) -> AttemptError:
    if isinstance(error, AttemptError):
        return error
    if _looks_like_playwright_timeout(error):
        return RetryableTechnicalError(
            "PLAYWRIGHT_TIMEOUT",
            "网页操作等待超时",
        )
    if _looks_like_playwright_network_error(error):
        return RetryableTechnicalError("NETWORK_ERROR", "网站网络连接失败")
    if isinstance(error, LayoutRecognitionError) and error.stage is not None:
        return RetryableTechnicalError(
            "LAYOUT_CHANGED",
            f"网页结构无法识别（{error.stage}）",
        )
    for error_type, code, message in _RETRYABLE_CLASSIFICATIONS:
        if isinstance(error, error_type):
            return RetryableTechnicalError(code, message)
    if isinstance(error, OSError):
        return RetryableTechnicalError("NETWORK_ERROR", "网站网络连接失败")
    return NonRetryableTechnicalError(
        "UNEXPECTED_BROWSER_ERROR",
        _unexpected_error_message(error),
    )


def consumes_technical_budget(error_code: str | None) -> bool:
    return error_code is not None and error_code not in _NON_BUDGET_ATTEMPT_CODES


def credential_free_error_message(code: str, message: str) -> str:
    if not contains_explicit_credentials(message):
        return message
    return _STABLE_ERROR_MESSAGES.get(code, "浏览器任务发生技术错误")


def contains_explicit_credentials(value: str) -> bool:
    return any(pattern.search(value) for pattern in _EXPLICIT_CREDENTIAL_PATTERNS)


def _unexpected_error_message(error: BaseException) -> str:
    """Keep a bounded diagnostic hint without ever publishing credentials."""
    fallback = "浏览器任务发生未识别的技术错误"
    detail = " ".join(str(error).split())
    if not detail or contains_explicit_credentials(detail):
        return fallback
    return f"{fallback}（{type(error).__name__}：{detail[:240]}）"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    technical_retries: int = 2
    retry_delay_seconds: float = 1.0

    def __post_init__(self) -> None:
        if type(self.technical_retries) is not int or self.technical_retries < 0:
            raise ValueError("technical_retries must be a non-negative integer")
        if (
            not isinstance(self.retry_delay_seconds, int | float)
            or isinstance(self.retry_delay_seconds, bool)
            or not math.isfinite(self.retry_delay_seconds)
            or self.retry_delay_seconds < 0
        ):
            raise ValueError(
                "retry_delay_seconds must be finite and non-negative"
            )

    @property
    def maximum_technical_attempts(self) -> int:
        return self.technical_retries + 1

    def can_start_technical_attempt(
        self,
        completed_technical_attempts: int,
    ) -> bool:
        if (
            type(completed_technical_attempts) is not int
            or completed_technical_attempts < 0
        ):
            raise ValueError(
                "completed_technical_attempts must be a non-negative integer"
            )
        return completed_technical_attempts < self.maximum_technical_attempts

    def wait_before_retry(self, control: SchedulerControl) -> bool:
        if not isinstance(control, SchedulerControl):
            raise ValueError("control must be a SchedulerControl")
        return control.wait_for_automated_work(self.retry_delay_seconds)


class SchedulerControl:
    """Thread-safe pause, stop, and active manual-login control."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._paused = False
        self._stopped = False
        self._manual_site: str | None = None

    @property
    def paused(self) -> bool:
        with self._condition:
            return self._paused

    @property
    def stopped(self) -> bool:
        with self._condition:
            return self._stopped

    @property
    def manual_site(self) -> str | None:
        with self._condition:
            return self._manual_site

    @property
    def automated_work_allowed(self) -> bool:
        with self._condition:
            return self._automated_work_allowed_unlocked()

    def request_pause(self) -> None:
        with self._condition:
            self._paused = True
            self._condition.notify_all()

    def resume(self) -> None:
        with self._condition:
            if self._stopped:
                return
            self._paused = False
            self._condition.notify_all()

    def request_stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()

    def enter_manual_login(self, site: str) -> None:
        normalized_site = _required_text(site, "site")
        if contains_explicit_credentials(normalized_site):
            raise ValueError("site must not contain credentials")
        with self._condition:
            if self._manual_site is not None and self._manual_site != normalized_site:
                raise ValueError("已有其他站点处于人工登录模式")
            self._manual_site = normalized_site
            self._condition.notify_all()

    def confirm_manual_login(self, site: str) -> None:
        normalized_site = _required_text(site, "site")
        with self._condition:
            if self._manual_site != normalized_site:
                raise ValueError("确认的站点与当前人工登录站点不一致")
            self._manual_site = None
            self._condition.notify_all()

    def cancel_manual_login(self, site: str) -> bool:
        normalized_site = _required_text(site, "site")
        with self._condition:
            if self._manual_site != normalized_site:
                return False
            self._manual_site = None
            self._condition.notify_all()
            return True

    def wait_for_automated_work(self, delay_seconds: float) -> bool:
        with self._condition:
            if not self._automated_work_allowed_unlocked():
                return False
            if delay_seconds > 0:
                self._condition.wait_for(
                    lambda: not self._automated_work_allowed_unlocked(),
                    timeout=delay_seconds,
                )
            return self._automated_work_allowed_unlocked()

    def _automated_work_allowed_unlocked(self) -> bool:
        return not self._paused and not self._stopped and self._manual_site is None


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value.strip()


def _looks_like_playwright_timeout(error: BaseException) -> bool:
    error_type = type(error)
    return (
        error_type.__name__ == "TimeoutError"
        and error_type.__module__.startswith("playwright")
    )


def _looks_like_playwright_network_error(error: BaseException) -> bool:
    if not type(error).__module__.startswith("playwright"):
        return False
    normalized_message = str(error).upper()
    return any(
        marker in normalized_message
        for marker in (
            "NET::ERR_",
            "NS_ERROR_",
            "ERR_CONNECTION",
            "ERR_NETWORK",
            "ERR_NAME_NOT_RESOLVED",
        )
    )
