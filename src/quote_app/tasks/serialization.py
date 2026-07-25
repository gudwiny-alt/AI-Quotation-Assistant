from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from quote_app.domain.models import QuoteMonth
from quote_app.evidence.models import (
    EvidenceRecord,
    EvidenceRectangle,
    EvidenceState,
)
from quote_app.tasks.models import (
    SCHEMA_VERSION,
    BusinessOutcome,
    InputFingerprint,
    RunRecord,
    RunState,
    TaskState,
    WebsiteChannel,
    WebsiteResult,
    WebsiteTask,
    url_contains_credentials,
)

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
Payload = dict[str, JsonValue]

_FORBIDDEN_CREDENTIAL_KEYS = {
    "password",
    "passwd",
    "cookie",
    "cookies",
    "cookie_value",
    "authorization",
    "authorization_header",
    "auth",
    "auth_token",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_token",
    "bearer_token",
}
_EXPLICIT_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\bauthorization\s*:\s*\S+"),
    re.compile(r"(?i)\b(?:set-cookie|cookie)\s*:\s*[\w.-]+\s*=\s*\S+"),
    re.compile(r"(?i)\bcookie\s*=\s*[\w.-]+\s*=\s*\S+"),
    re.compile(
        r"(?i)\b(?:password|passwd|access_token|refresh_token|id_token|"
        r"api_token|auth_token|token)\s*[:=]\s*\S+"
    ),
)


class PayloadError(ValueError):
    """A stable public error for rejected persisted payloads."""


def to_payload(value: object) -> Payload:
    if isinstance(value, InputFingerprint):
        data = _fingerprint_to_data(value)
    elif isinstance(value, RunRecord):
        if value.schema_version != SCHEMA_VERSION:
            raise PayloadError(f"unsupported schema_version: {value.schema_version}")
        data = _run_to_data(value)
    elif isinstance(value, WebsiteTask):
        data = _task_to_data(value)
    elif isinstance(value, WebsiteResult):
        data = _result_to_data(value)
    elif isinstance(value, EvidenceRecord):
        data = _evidence_to_data(value)
    else:
        raise TypeError(f"unsupported payload type: {type(value).__name__}")

    payload: Payload = {
        "schema_version": SCHEMA_VERSION,
        "payload_type": type(value).__name__,
        "data": data,
    }
    _reject_credentials(payload)
    return payload


def from_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        raise PayloadError("malformed payload")
    _reject_credentials(payload)
    if set(payload) != {"schema_version", "payload_type", "data"}:
        raise PayloadError("malformed payload")
    version = payload.get("schema_version")
    if type(version) is not int:
        raise PayloadError("schema_version must be an integer")
    if version != SCHEMA_VERSION:
        raise PayloadError(f"unsupported schema_version: {version}")
    payload_type = payload.get("payload_type")
    data = payload.get("data")
    if not isinstance(payload_type, str) or not isinstance(data, dict):
        raise PayloadError("malformed payload")

    loaders = {
        "InputFingerprint": _fingerprint_from_data,
        "RunRecord": _run_from_data,
        "WebsiteTask": _task_from_data,
        "WebsiteResult": _result_from_data,
        "EvidenceRecord": _evidence_from_data,
    }
    loader = loaders.get(payload_type)
    if loader is None:
        raise PayloadError(f"unsupported payload_type: {payload_type}")
    try:
        return loader(data)
    except PayloadError:
        raise
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise PayloadError("malformed payload") from exc


def _fingerprint_to_data(value: InputFingerprint) -> dict[str, JsonValue]:
    return {
        "source_role": value.source_role,
        "path": str(value.path),
        "sha256": value.sha256,
        "byte_size": value.byte_size,
        "modified_ns": value.modified_ns,
    }


def _fingerprint_from_data(data: dict[str, Any]) -> InputFingerprint:
    _require_keys(data, {"source_role", "path", "sha256", "byte_size", "modified_ns"})
    return InputFingerprint(
        source_role=_string(data["source_role"]),
        path=Path(_string(data["path"])),
        sha256=_string(data["sha256"]),
        byte_size=_integer(data["byte_size"]),
        modified_ns=_integer(data["modified_ns"]),
    )


def _run_to_data(value: RunRecord) -> dict[str, JsonValue]:
    return {
        "run_id": value.run_id,
        "schema_version": value.schema_version,
        "quote_month": {
            "year": value.quote_month.year,
            "month": value.quote_month.month,
        },
        "input_fingerprints": [
            _fingerprint_to_data(fingerprint)
            for fingerprint in value.input_fingerprints
        ],
        "output_dir": str(value.output_dir),
        "browser_profile_dir": str(value.browser_profile_dir),
        "associated_rows_snapshot": value.associated_rows_snapshot,
        "associated_rows_snapshot_path": _path_or_none(
            value.associated_rows_snapshot_path
        ),
        "associated_rows_snapshot_sha256": value.associated_rows_snapshot_sha256,
        "quote_path": _path_or_none(value.quote_path),
        "report_path": _path_or_none(value.report_path),
        "state": value.state.value,
        "created_at": value.created_at.isoformat(),
        "updated_at": value.updated_at.isoformat(),
    }


def _run_from_data(data: dict[str, Any]) -> RunRecord:
    _require_keys(
        data,
        {
            "run_id",
            "schema_version",
            "quote_month",
            "input_fingerprints",
            "output_dir",
            "browser_profile_dir",
            "associated_rows_snapshot",
            "associated_rows_snapshot_path",
            "associated_rows_snapshot_sha256",
            "quote_path",
            "report_path",
            "state",
            "created_at",
            "updated_at",
        },
    )
    quote_month = data["quote_month"]
    if not isinstance(quote_month, dict):
        raise PayloadError("malformed payload")
    _require_keys(quote_month, {"year", "month"})
    fingerprints = data["input_fingerprints"]
    if not isinstance(fingerprints, list):
        raise PayloadError("malformed payload")
    return RunRecord(
        run_id=_string(data["run_id"]),
        schema_version=_integer(data["schema_version"]),
        quote_month=QuoteMonth(
            _integer(quote_month["year"]),
            _integer(quote_month["month"]),
        ),
        input_fingerprints=tuple(
            _fingerprint_from_data(_mapping(item)) for item in fingerprints
        ),
        output_dir=Path(_string(data["output_dir"])),
        browser_profile_dir=Path(_string(data["browser_profile_dir"])),
        associated_rows_snapshot=_optional_string(data["associated_rows_snapshot"]),
        associated_rows_snapshot_path=_optional_path(
            data["associated_rows_snapshot_path"]
        ),
        associated_rows_snapshot_sha256=_string(
            data["associated_rows_snapshot_sha256"]
        ),
        quote_path=_optional_path(data["quote_path"]),
        report_path=_optional_path(data["report_path"]),
        state=RunState(_string(data["state"])),
        created_at=_datetime(data["created_at"]),
        updated_at=_datetime(data["updated_at"]),
    )


def _task_to_data(value: WebsiteTask) -> dict[str, JsonValue]:
    return {
        "task_id": value.task_id,
        "run_id": value.run_id,
        "source_row_number": value.source_row_number,
        "output_row_number": value.output_row_number,
        "material_code": value.material_code,
        "brand": value.brand,
        "model_name": value.model_name,
        "ram": value.ram,
        "storage": value.storage,
        "color": value.color,
        "channel": value.channel.value,
    }


def _task_from_data(data: dict[str, Any]) -> WebsiteTask:
    _require_keys(
        data,
        {
            "task_id",
            "run_id",
            "source_row_number",
            "output_row_number",
            "material_code",
            "brand",
            "model_name",
            "ram",
            "storage",
            "color",
            "channel",
        },
    )
    return WebsiteTask(
        task_id=_string(data["task_id"]),
        run_id=_string(data["run_id"]),
        source_row_number=_integer(data["source_row_number"]),
        output_row_number=_integer(data["output_row_number"]),
        material_code=_string(data["material_code"]),
        brand=_string(data["brand"]),
        model_name=_string(data["model_name"]),
        ram=_string(data["ram"]),
        storage=_string(data["storage"]),
        color=_string(data["color"]),
        channel=WebsiteChannel(_string(data["channel"])),
    )


def _result_to_data(value: WebsiteResult) -> dict[str, JsonValue]:
    return {
        "task_id": value.task_id,
        "state": value.state.value,
        "outcome": value.outcome.value if value.outcome is not None else None,
        "price": str(value.price) if value.price is not None else None,
        "url": value.url,
        "evidence": (
            _evidence_to_data(value.evidence) if value.evidence is not None else None
        ),
        "diagnostic_path": _path_or_none(value.diagnostic_path),
        "error_code": value.error_code,
        "error_message": value.error_message,
    }


def _result_from_data(data: dict[str, Any]) -> WebsiteResult:
    _require_keys(
        data,
        {
            "task_id",
            "state",
            "outcome",
            "price",
            "url",
            "evidence",
            "diagnostic_path",
            "error_code",
            "error_message",
        },
    )
    raw_outcome = data["outcome"]
    raw_price = data["price"]
    raw_evidence = data["evidence"]
    return WebsiteResult(
        task_id=_string(data["task_id"]),
        state=TaskState(_string(data["state"])),
        outcome=(
            BusinessOutcome(_string(raw_outcome))
            if raw_outcome is not None
            else None
        ),
        price=Decimal(_string(raw_price)) if raw_price is not None else None,
        url=_optional_string(data["url"]),
        evidence=(
            _evidence_from_data(_mapping(raw_evidence))
            if raw_evidence is not None
            else None
        ),
        diagnostic_path=_optional_path(data["diagnostic_path"]),
        error_code=_optional_string(data["error_code"]),
        error_message=_optional_string(data["error_message"]),
    )


def _evidence_to_data(value: EvidenceRecord) -> dict[str, JsonValue]:
    return {
        "state": value.state.value,
        "path": str(value.path),
        "sha256": value.sha256,
        "pixel_width": value.pixel_width,
        "pixel_height": value.pixel_height,
        "captured_at": value.captured_at.isoformat(),
        "validation_code": value.validation_code,
        "annotations": [
            {
                "role": annotation.role,
                "x": annotation.x,
                "y": annotation.y,
                "width": annotation.width,
                "height": annotation.height,
            }
            for annotation in value.annotations
        ],
    }


def _evidence_from_data(data: dict[str, Any]) -> EvidenceRecord:
    _require_keys(
        data,
        {
            "state",
            "path",
            "sha256",
            "pixel_width",
            "pixel_height",
            "captured_at",
            "validation_code",
            "annotations",
        },
    )
    annotations = data["annotations"]
    if not isinstance(annotations, list):
        raise PayloadError("malformed payload")
    return EvidenceRecord(
        state=EvidenceState(_string(data["state"])),
        path=Path(_string(data["path"])),
        sha256=_string(data["sha256"]),
        pixel_width=_integer(data["pixel_width"]),
        pixel_height=_integer(data["pixel_height"]),
        captured_at=_datetime(data["captured_at"]),
        validation_code=_string(data["validation_code"]),
        annotations=tuple(
            _rectangle_from_data(_mapping(annotation))
            for annotation in annotations
        ),
    )


def _rectangle_from_data(data: dict[str, Any]) -> EvidenceRectangle:
    _require_keys(data, {"role", "x", "y", "width", "height"})
    return EvidenceRectangle(
        role=_string(data["role"]),
        x=_integer(data["x"]),
        y=_integer(data["y"]),
        width=_integer(data["width"]),
        height=_integer(data["height"]),
    )


def _reject_credentials(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            if _is_credential_key(normalized_key):
                raise PayloadError("credential fields are forbidden")
            if key == "associated_rows_snapshot" and isinstance(item, str):
                _reject_snapshot_credentials(item)
            elif isinstance(item, str) and _contains_explicit_credentials(item):
                raise PayloadError("credential fields are forbidden")
            _reject_credentials(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _reject_credentials(item)


def _reject_snapshot_credentials(snapshot: str) -> None:
    try:
        parsed = json.loads(snapshot)
    except json.JSONDecodeError as exc:
        raise PayloadError("malformed payload") from exc
    _reject_credentials(parsed)


def _is_credential_key(normalized_key: str) -> bool:
    if normalized_key in _FORBIDDEN_CREDENTIAL_KEYS:
        return True
    return normalized_key.endswith(
        ("_password", "_cookie", "_authorization", "_token")
    )


def _contains_explicit_credentials(value: str) -> bool:
    if any(pattern.search(value) for pattern in _EXPLICIT_CREDENTIAL_PATTERNS):
        return True
    return url_contains_credentials(value)


def _require_keys(data: dict[str, Any], required: set[str]) -> None:
    if set(data) != required:
        raise PayloadError("malformed payload")


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PayloadError("malformed payload")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise PayloadError("malformed payload")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _string(value)


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PayloadError("malformed payload")
    return value


def _datetime(value: object) -> datetime:
    return datetime.fromisoformat(_string(value))


def _optional_path(value: object) -> Path | None:
    if value is None:
        return None
    return Path(_string(value))


def _path_or_none(value: Path | None) -> str | None:
    return str(value) if value is not None else None
