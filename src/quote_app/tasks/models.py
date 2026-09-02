from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlparse

from quote_app.domain.models import QuoteMonth
from quote_app.evidence.models import EvidenceRecord, EvidenceState

SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_KNOWN_FORMAL_VALIDATION_CODES = frozenset(
    ("CAPTURE_OK", "CAPTURE_OK_MAC_VISUAL_REVIEW")
)
_FORMAL_EVIDENCE_TECHNICAL_CODES = frozenset(("PRICE_UNAVAILABLE_AFTER_CAPTURE",))


class WebsiteChannel(StrEnum):
    JD = "jd"
    TMALL = "tmall"
    OFFICIAL = "official"


class BusinessOutcome(StrEnum):
    PRICE_FOUND = "price_found"
    NO_MODEL = "no_model"
    CAPACITY_UNAVAILABLE = "capacity_unavailable"
    COLOR_UNAVAILABLE = "color_unavailable"
    SOLD_OUT = "sold_out"


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_LOGIN = "waiting_for_login"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    TECHNICAL_FAILURE = "technical_failure"


class RunState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class InputFingerprint:
    source_role: str
    path: Path
    sha256: str
    byte_size: int
    modified_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_role, str):
            raise ValueError("source_role must be a string")
        if not isinstance(self.path, Path):
            raise ValueError("path must be a Path")
        if not isinstance(self.sha256, str):
            raise ValueError("sha256 must be a string")
        if type(self.byte_size) is not int or type(self.modified_ns) is not int:
            raise ValueError("fingerprint size and mtime must be integers")
        object.__setattr__(self, "path", _normalized_path(self.path))
        if not self.source_role.strip():
            raise ValueError("source_role must not be blank")
        if not _SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        if self.byte_size < 0:
            raise ValueError("byte_size must be non-negative")
        if self.modified_ns < 0:
            raise ValueError("modified_ns must be non-negative")


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    schema_version: int
    quote_month: QuoteMonth
    input_fingerprints: tuple[InputFingerprint, ...]
    output_dir: Path
    browser_profile_dir: Path
    associated_rows_snapshot: str | None
    associated_rows_snapshot_path: Path | None
    associated_rows_snapshot_sha256: str
    quote_path: Path | None
    report_path: Path | None
    state: RunState
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str):
            raise ValueError("run_id must be a string")
        if type(self.schema_version) is not int:
            raise ValueError("schema_version must be an integer")
        if not isinstance(self.quote_month, QuoteMonth):
            raise ValueError("quote_month must be a QuoteMonth")
        if (
            type(self.quote_month.year) is not int
            or type(self.quote_month.month) is not int
        ):
            raise ValueError("quote month year and month must be integers")
        if not isinstance(self.input_fingerprints, tuple | list):
            raise ValueError("input_fingerprints must be a sequence")
        if not all(
            isinstance(fingerprint, InputFingerprint)
            for fingerprint in self.input_fingerprints
        ):
            raise ValueError(
                "input_fingerprints must contain InputFingerprint values"
            )
        if not isinstance(self.output_dir, Path):
            raise ValueError("output_dir must be a Path")
        if not isinstance(self.browser_profile_dir, Path):
            raise ValueError("browser_profile_dir must be a Path")
        if (
            self.associated_rows_snapshot is not None
            and not isinstance(self.associated_rows_snapshot, str)
        ):
            raise ValueError("associated_rows_snapshot must be a string")
        if (
            self.associated_rows_snapshot_path is not None
            and not isinstance(self.associated_rows_snapshot_path, Path)
        ):
            raise ValueError("associated_rows_snapshot_path must be a Path")
        if not isinstance(self.associated_rows_snapshot_sha256, str):
            raise ValueError("associated_rows_snapshot_sha256 must be a string")
        if self.quote_path is not None and not isinstance(self.quote_path, Path):
            raise ValueError("quote_path must be a Path")
        if self.report_path is not None and not isinstance(self.report_path, Path):
            raise ValueError("report_path must be a Path")
        if not isinstance(self.state, RunState):
            raise ValueError("state must be a RunState")
        if not isinstance(self.created_at, datetime) or not isinstance(
            self.updated_at, datetime
        ):
            raise ValueError("run timestamps must be datetime values")

        object.__setattr__(self, "input_fingerprints", tuple(self.input_fingerprints))
        object.__setattr__(self, "output_dir", _normalized_path(self.output_dir))
        object.__setattr__(
            self,
            "browser_profile_dir",
            _normalized_path(self.browser_profile_dir),
        )
        if self.associated_rows_snapshot_path is not None:
            object.__setattr__(
                self,
                "associated_rows_snapshot_path",
                _normalized_path(self.associated_rows_snapshot_path),
            )
        if self.quote_path is not None:
            object.__setattr__(self, "quote_path", _normalized_path(self.quote_path))
        if self.report_path is not None:
            object.__setattr__(self, "report_path", _normalized_path(self.report_path))

        if not self.run_id.strip():
            raise ValueError("run_id must not be blank")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if len(self.input_fingerprints) != 3:
            raise ValueError("run requires exactly three input fingerprints")
        roles = [fingerprint.source_role for fingerprint in self.input_fingerprints]
        if len(set(roles)) != len(roles):
            raise ValueError("input fingerprint source roles must be unique")
        has_inline_snapshot = self.associated_rows_snapshot is not None
        has_snapshot_path = self.associated_rows_snapshot_path is not None
        if has_inline_snapshot == has_snapshot_path:
            raise ValueError("run requires exactly one associated-row snapshot source")
        if not _SHA256_PATTERN.fullmatch(self.associated_rows_snapshot_sha256):
            raise ValueError(
                "associated_rows_snapshot_sha256 must be 64 lowercase hexadecimal characters"
            )
        if not _is_timezone_aware(self.created_at) or not _is_timezone_aware(
            self.updated_at
        ):
            raise ValueError("run timestamps must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")


@dataclass(frozen=True, slots=True)
class WebsiteTask:
    task_id: str
    run_id: str
    source_row_number: int
    output_row_number: int
    material_code: str
    brand: str
    model_name: str
    ram: str
    storage: str
    color: str
    channel: WebsiteChannel
    requires_ai_package: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "run_id",
            "material_code",
            "brand",
            "model_name",
            "ram",
            "storage",
            "color",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ValueError(f"{field_name} must be a string")
            normalized = value.strip()
            if not normalized:
                raise ValueError(f"{field_name} must not be blank")
            object.__setattr__(self, field_name, normalized)
        if (
            type(self.source_row_number) is not int
            or type(self.output_row_number) is not int
        ):
            raise ValueError("task row numbers must be integers")
        if not isinstance(self.channel, WebsiteChannel):
            raise ValueError("channel must be a WebsiteChannel")
        if type(self.requires_ai_package) is not bool:
            raise ValueError("requires_ai_package must be a boolean")
        if self.source_row_number < 2:
            raise ValueError("source_row_number must be at least 2")
        if self.output_row_number < 2:
            raise ValueError("output_row_number must be at least 2")


@dataclass(frozen=True, slots=True)
class WebsiteObservationCheckpoint:
    task_id: str
    outcome: BusinessOutcome
    price: Decimal | None
    url: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str):
            raise ValueError("task_id must be a string")
        if not isinstance(self.outcome, BusinessOutcome):
            raise ValueError("outcome must be a BusinessOutcome")
        if self.price is not None and not isinstance(self.price, Decimal):
            raise ValueError("price must be a Decimal")
        if not isinstance(self.url, str):
            raise ValueError("url must be a string")
        if not isinstance(self.observed_at, datetime):
            raise ValueError("observed_at must be a datetime")
        if not self.task_id.strip():
            raise ValueError("task_id must not be blank")
        if not _is_http_url(self.url) or url_contains_credentials(self.url):
            raise ValueError("observation requires a credential-free HTTP(S) URL")
        if not _is_timezone_aware(self.observed_at):
            raise ValueError("observed_at must be timezone-aware")
        if self.outcome is BusinessOutcome.PRICE_FOUND:
            if self.price is None or not self.price.is_finite() or self.price < 0:
                raise ValueError("PRICE_FOUND requires a non-negative price")
        elif self.price is not None:
            raise ValueError("legal no requires price to be None")


@dataclass(frozen=True, slots=True)
class WebsiteResult:
    task_id: str
    state: TaskState
    outcome: BusinessOutcome | None
    price: Decimal | None
    url: str | None
    evidence: EvidenceRecord | None
    diagnostic_path: Path | None
    error_code: str | None
    error_message: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str):
            raise ValueError("task_id must be a string")
        if not isinstance(self.state, TaskState):
            raise ValueError("state must be a TaskState")
        if self.outcome is not None and not isinstance(
            self.outcome, BusinessOutcome
        ):
            raise ValueError("outcome must be a BusinessOutcome")
        if self.price is not None and not isinstance(self.price, Decimal):
            raise ValueError("price must be a Decimal")
        if self.url is not None and not isinstance(self.url, str):
            raise ValueError("url must be a string")
        if self.evidence is not None and not isinstance(
            self.evidence, EvidenceRecord
        ):
            raise ValueError("evidence must be an EvidenceRecord")
        if self.diagnostic_path is not None and not isinstance(
            self.diagnostic_path, Path
        ):
            raise ValueError("diagnostic_path must be a Path")
        for field_name in ("error_code", "error_message"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field_name} must be a string")
            if value is not None:
                object.__setattr__(self, field_name, value.strip())

        if not self.task_id.strip():
            raise ValueError("task_id must not be blank")
        if self.url is not None and url_contains_credentials(self.url):
            raise ValueError("URL must not contain credentials")
        for diagnostic_text in (self.error_code, self.error_message):
            if (
                diagnostic_text is not None
                and _contains_explicit_credentials(diagnostic_text)
            ):
                raise ValueError("diagnostic text must not contain credentials")
        if self.diagnostic_path is not None:
            object.__setattr__(
                self,
                "diagnostic_path",
                _normalized_path(self.diagnostic_path),
            )

        if self.state not in {TaskState.SUCCEEDED, TaskState.TECHNICAL_FAILURE}:
            raise ValueError("WebsiteResult state must be completed")
        if self.state is TaskState.TECHNICAL_FAILURE:
            self._validate_technical_failure()
            return
        self._validate_business_success()

    def _validate_technical_failure(self) -> None:
        if not self.error_code or not self.error_message:
            raise ValueError("technical failure requires error code and message")
        if self.outcome is not None or self.price is not None:
            raise ValueError("technical failure cannot carry a business outcome or price")
        if self.evidence is None:
            return
        if self.error_code not in _FORMAL_EVIDENCE_TECHNICAL_CODES:
            raise ValueError("technical failure cannot carry formal evidence")
        if not _is_http_url(self.url):
            raise ValueError("partial formal evidence requires a URL")
        if self.diagnostic_path is not None:
            raise ValueError("partial formal evidence cannot be diagnostic evidence")
        if self.evidence.validation_code not in _KNOWN_FORMAL_VALIDATION_CODES:
            raise ValueError("partial formal evidence must be validated")

    def _validate_business_success(self) -> None:
        if self.outcome is None:
            raise ValueError("business success requires an outcome")
        if not _is_http_url(self.url):
            raise ValueError("business success requires a URL")
        if (
            self.evidence is None
            or self.evidence.validation_code not in _KNOWN_FORMAL_VALIDATION_CODES
        ):
            raise ValueError(
                "business success requires validated evidence or known formal beta evidence"
            )
        if self.error_code is not None or self.error_message is not None:
            raise ValueError("business success cannot carry a technical error")

        expected_state = _OUTCOME_EVIDENCE_STATES[self.outcome]
        if self.evidence.state is not expected_state:
            raise ValueError("evidence state does not match outcome")

        if self.outcome is BusinessOutcome.PRICE_FOUND:
            if self.price is None or not self.price.is_finite() or self.price < 0:
                raise ValueError("PRICE_FOUND requires a non-negative price")
        elif self.price is not None:
            raise ValueError("legal no requires price to be None")

        if self.outcome is BusinessOutcome.SOLD_OUT:
            roles = {annotation.role for annotation in self.evidence.annotations}
            if "stock_status" not in roles:
                raise ValueError("SOLD_OUT evidence requires stock_status annotation")


_OUTCOME_EVIDENCE_STATES = {
    BusinessOutcome.PRICE_FOUND: EvidenceState.NORMAL,
    BusinessOutcome.NO_MODEL: EvidenceState.NO_MODEL,
    BusinessOutcome.CAPACITY_UNAVAILABLE: EvidenceState.CAPACITY_UNAVAILABLE,
    BusinessOutcome.COLOR_UNAVAILABLE: EvidenceState.COLOR_UNAVAILABLE,
    BusinessOutcome.SOLD_OUT: EvidenceState.SOLD_OUT,
}


def _normalized_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _is_http_url(value: str | None) -> bool:
    if value is None:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


_SENSITIVE_QUERY_KEYS = {
    "password",
    "passwd",
    "cookie",
    "cookie_value",
    "authorization",
    "authorization_header",
    "auth",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_token",
    "auth_token",
    "client_secret",
    "api_key",
    "apikey",
    "session",
    "session_id",
    "session_key",
    "session_token",
    "credential",
    "credentials",
    "credential_id",
}
_SENSITIVE_QUERY_KEY_SUFFIXES = (
    "_token",
    "_secret",
    "_credential",
    "_credentials",
    "_session",
    "_session_id",
    "_session_key",
)
_EXPLICIT_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\bauthorization\s*:\s*\S+"),
    re.compile(r"(?i)\b(?:set-cookie|cookie)\s*:\s*[\w.-]+\s*=\s*\S+"),
    re.compile(r"(?i)\bcookie\s*=\s*[\w.-]+\s*=\s*\S+"),
    re.compile(
        r"(?i)\b(?:password|passwd|access_token|refresh_token|id_token|"
        r"api_token|auth_token|token)\s*[:=]\s*\S+"
    ),
)


def url_contains_credentials(value: str) -> bool:
    """Return whether an HTTP(S) URL exposes credential material."""

    parsed = urlparse(value)
    if parsed.username is not None or parsed.password is not None:
        return True
    if any(
        _is_sensitive_credential_name(key)
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    ):
        return True
    return _url_component_has_credentials(parsed.path) or (
        _url_component_has_credentials(parsed.fragment)
    )


def _url_component_has_credentials(value: str) -> bool:
    decoded = unquote(value)
    for segment in decoded.replace(":", "/").split("/"):
        key = segment.split("=", 1)[0]
        if _is_sensitive_credential_name(key):
            return True
    return False


def _is_sensitive_credential_name(value: str) -> bool:
    normalized = value
    for _ in range(2):
        decoded = unquote(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    normalized = normalized.strip().lower().replace("-", "_")
    return (
        normalized in _SENSITIVE_QUERY_KEYS
        or normalized.endswith(_SENSITIVE_QUERY_KEY_SUFFIXES)
    )


def _contains_explicit_credentials(value: str) -> bool:
    return any(pattern.search(value) for pattern in _EXPLICIT_CREDENTIAL_PATTERNS)


def _is_timezone_aware(value: datetime) -> bool:
    if value.tzinfo is None:
        return False
    try:
        return value.utcoffset() is not None
    except (OverflowError, ValueError):
        return False
