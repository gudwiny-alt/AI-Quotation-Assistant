from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeAlias

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)

WORKER_EVENT_SCHEMA_VERSION = 1
_EVENT_NAMES = frozenset(
    {
        "progress",
        "waiting_for_login",
        "result",
        "technical_failure",
    }
)
_FORBIDDEN_KEYS = frozenset(
    {
        "password",
        "passwd",
        "cookie",
        "cookies",
        "cookie_value",
        "authorization",
        "authorization_header",
        "token",
        "access_token",
        "refresh_token",
        "auth_token",
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


@dataclass(frozen=True, slots=True)
class WorkerEvent:
    event: str
    run_id: str
    task_id: str
    data: Mapping[str, object]
    schema_version: int = WORKER_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.event not in _EVENT_NAMES:
            raise ValueError("unsupported worker event")
        for field_name in ("run_id", "task_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be blank")
        if (
            type(self.schema_version) is not int
            or self.schema_version != WORKER_EVENT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported worker event schema version")
        if (
            not isinstance(self.data, dict)
            or not _is_json_safe(self.data)
            or _contains_credentials(self.data)
        ):
            raise ValueError(
                "worker event data must be JSON-safe and credential-free"
            )
        object.__setattr__(self, "data", _freeze_mapping(self.data))

    def to_payload(self) -> dict[str, JsonValue]:
        thawed_data = _thaw_mapping(self.data)
        if not _is_json_safe(thawed_data) or _contains_credentials(thawed_data):
            raise ValueError(
                "worker event data must be JSON-safe and credential-free"
            )
        return {
            "schema_version": self.schema_version,
            "event": self.event,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "data": thawed_data,
        }


def _is_json_safe(value: object) -> bool:
    if value is None or isinstance(value, bool | int | str):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_safe(item) for item in value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _is_json_safe(item)
            for key, item in value.items()
        )
    return False


def _contains_credentials(value: object) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _EXPLICIT_CREDENTIAL_PATTERNS)
    if isinstance(value, list):
        return any(_contains_credentials(item) for item in value)
    if isinstance(value, Mapping):
        return any(
            _normalized_key(key) in _FORBIDDEN_KEYS
            or _contains_credentials(item)
            for key, item in value.items()
        )
    return False


def _normalized_key(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(
        {key: _freeze_value(item) for key, item in value.items()}
    )


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def _thaw_mapping(value: Mapping[str, object]) -> dict[str, JsonValue]:
    return {key: _thaw_value(item) for key, item in value.items()}


def _thaw_value(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return _thaw_mapping(value)
    if isinstance(value, tuple | list):
        return [_thaw_value(item) for item in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    raise ValueError("worker event data must be JSON-safe and credential-free")
