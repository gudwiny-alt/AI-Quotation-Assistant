"""Local settings and a narrowly scoped OpenAI-compatible analysis client."""

from __future__ import annotations

import copy
import http.client
import json
import os
import socket
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from .ai_protocol import AIError, SECTION_TITLES, read_response, output_contract
from .ai_diagnostics import RequestTrace

DEFAULT_BASE_URL = ""
DEFAULT_MODEL = ""
MAX_REQUEST_BYTES = 500 * 1024
# Deep reasoning services can legitimately exceed 90s before returning headers.
RESPONSE_TIMEOUT_SECONDS = 300
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


@dataclass(frozen=True)
class AISettings:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key: str = field(default="", repr=False)

    @property
    def ready(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip() and self.api_key.strip())


def load_settings(path: Path) -> AISettings:
    path = Path(path)
    try:
        if path.is_symlink():
            raise AIError("AI 配置文件不能是符号链接。")
        if not path.exists():
            return AISettings()
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) - {"base_url", "model", "api_key"}:
            raise ValueError
        return AISettings(
            base_url=_required_string(value.get("base_url", DEFAULT_BASE_URL)),
            model=_required_string(value.get("model", DEFAULT_MODEL)),
            api_key=_string(value.get("api_key", "")),
        )
    except AIError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AIError("AI 配置无法读取，请重新保存设置。") from exc


def save_settings(path: Path, settings: AISettings) -> None:
    path = Path(path)
    try:
        if not isinstance(settings, AISettings):
            raise AIError("AI 配置格式无效，请重新填写设置。")
        base_url = _required_string(settings.base_url)
        model = _required_string(settings.model)
        api_key = _string(settings.api_key)
        _endpoint(base_url)
        if path.is_symlink():
            raise AIError("AI 配置文件不能是符号链接。")
        if path.exists() and not stat.S_ISREG(path.stat().st_mode):
            raise AIError("AI 配置路径不是普通文件。")
        if not path.parent.exists():
            path.parent.mkdir(parents=True, mode=0o700)
        encoded = json.dumps(
            {"base_url": base_url, "model": model, "api_key": api_key},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, path)
            os.chmod(path, 0o600)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
    except AIError:
        raise
    except (TypeError, ValueError):
        raise AIError("AI 配置格式无效，请重新填写设置。") from None
    except OSError as exc:
        raise AIError("AI 配置无法保存，请检查配置目录权限。") from exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def generate_analysis(
    settings: AISettings,
    payload: dict[str, Any],
    system_prompt: str,
    *,
    on_progress=None,
    diagnostic_path=None,
) -> dict[str, Any]:
    if not settings.ready:
        raise AIError("请先填写模型地址、模型名称和 API 密钥。")
    endpoint = _endpoint(settings.base_url)
    if not isinstance(payload, dict) or not isinstance(payload.get("snapshot_id"), str):
        raise AIError("分析快照格式无效，请重新生成快照。")
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        raise AIError("分析快照缺少来源信息，请重新生成快照。")
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise AIError("系统提示词不能为空。")

    wire_payload, source_aliases = _wire_payload(payload)
    try:
        user_content = json.dumps(
            wire_payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        request_body = json.dumps(
            {
                "model": settings.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                    {"role": "user", "content": output_contract(payload["snapshot_id"])},
                ],
                "stream": True,
                "stream_options": {"include_usage": True},
                "thinking": {"type": "enabled"},
                "max_tokens": 32768,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise AIError("分析快照包含无法序列化的数据，请重新生成快照。") from None
    if len(request_body) > MAX_REQUEST_BYTES:
        raise AIError("分析快照超过 500KB，请缩小批次后重试。")

    request = urllib.request.Request(
        endpoint,
        data=request_body,
        headers={"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    trace = RequestTrace(diagnostic_path, on_progress)
    trace.emit("waiting", request_bytes=len(request_body))
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
            request, timeout=RESPONSE_TIMEOUT_SECONDS
        ) as response:
            trace.emit("connected", http_status=getattr(response, "status", 200))
            envelope = read_response(response, trace.started, trace.emit)
        trace.emit("validating")
        analysis, usage = _validate_envelope(
            envelope, payload["snapshot_id"], set(sources), source_aliases
        )
    except urllib.error.HTTPError as exc:
        trace.emit(
            "failed", error_code="http_error", http_status=exc.code, failed_stage=trace.stage
        )
        if 300 <= exc.code < 400:
            raise AIError("模型服务返回重定向，已为保护密钥阻止请求。") from None
        if exc.code in (401, 403):
            raise AIError("模型服务拒绝访问，请检查 API 密钥。") from None
        if exc.code == 400:
            raise AIError(
                "模型服务不接受本次参数（HTTP 400），请查看诊断记录并核对模型能力。"
            ) from None
        raise AIError(f"模型服务请求失败（HTTP {exc.code}），请稍后重试。") from None
    except (TimeoutError, socket.timeout):
        phase = trace.stage
        trace.emit("failed", error_code="timeout", failed_stage=phase)
        detail = "尚未收到服务端响应" if phase == "waiting" else "接收过程中长时间未收到后续数据"
        raise AIError(
            f"模型服务响应等待超时（单次等待上限 {RESPONSE_TIMEOUT_SECONDS} 秒）：{detail}。"
        ) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        trace.emit("failed", error_code="network_error", failed_stage=trace.stage)
        raise AIError("模型服务连接中断，请检查地址和网络；本次分析未完成。") from None
    except AIError:
        trace.emit("failed", error_code="validation_error", failed_stage=trace.stage)
        raise
    trace.emit("complete")
    return {
        "analysis": analysis,
        "model": settings.model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.monotonic() - trace.started, 3),
        "usage": usage,
        "request_id": trace.request_id,
    }


def _wire_payload(payload):
    """Short, lossless reference IDs avoid requiring models to copy long hashes."""
    aliases = {ref: f"S{index:03d}" for index, ref in enumerate(payload["sources"], 1)}
    wire = copy.deepcopy(payload)

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "ref" and isinstance(child, str) and child in aliases:
                    value[key] = aliases[child]
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(wire)
    wire["sources"] = {aliases[ref]: label for ref, label in wire["sources"].items()}
    return wire, {alias: ref for ref, alias in aliases.items()}


def _endpoint(base_url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(base_url.strip())
        _ = parsed.port
    except (ValueError, AttributeError):
        raise AIError("模型地址无效，请检查设置。") from None
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme not in ({"https"} if not local else {"http", "https"}):
        raise AIError("模型地址必须使用 HTTPS；本机服务可使用 HTTP。")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise AIError("模型地址无效，不能包含账号或密码。")
    if parsed.query or parsed.fragment:
        raise AIError("模型地址不能包含查询参数或片段。")
    path = parsed.path.rstrip("/") + "/chat/completions"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _validate_envelope(
    envelope: Any, snapshot_id: str, known_sources: set[str], source_aliases=None
) -> tuple[dict[str, Any], dict[str, int]]:
    try:
        choice = envelope["choices"][0]
        message = choice["message"]
    except (TypeError, KeyError, IndexError):
        raise AIError("模型响应结构不完整，请重试。") from None
    if not isinstance(choice, dict) or not isinstance(message, dict):
        raise AIError("模型响应结构不完整，请重试。")
    if message.get("tool_calls") or choice.get("finish_reason") == "tool_calls":
        raise AIError("模型返回了工具调用，当前分析不允许执行工具。")
    if choice.get("finish_reason") != "stop":
        if choice.get("finish_reason") == "length":
            raise AIError("模型响应被截断，请缩小分析范围后重试。")
        raise AIError("模型未正常完成响应，请重试。")
    content = message.get("content")
    if not isinstance(content, str):
        raise AIError("模型响应内容不是文本，请重试。")
    if content.lstrip().startswith("```"):
        raise AIError("模型返回了 JSON 代码块，请重试并要求直接返回 JSON。")
    try:
        analysis = json.loads(content)
    except json.JSONDecodeError:
        raise AIError("模型分析不是有效 JSON，请重试。") from None
    if source_aliases and isinstance(analysis, dict):
        for name in ("management_priorities", "sections"):
            entries = analysis.get(name)
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and isinstance(entry.get("evidence_refs"), list):
                        entry["evidence_refs"] = [
                            source_aliases.get(ref, ref) if isinstance(ref, str) else ref
                            for ref in entry["evidence_refs"]
                        ]
    _validate_analysis(analysis, snapshot_id, known_sources)
    usage_value = envelope.get("usage", {})
    if not isinstance(usage_value, dict):
        raise AIError("模型用量信息格式无效，请重试。")
    usage: dict[str, int] = {}
    for name in TOKEN_FIELDS:
        if name not in usage_value:
            continue
        value = usage_value[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AIError("模型用量信息格式无效，请重试。")
        usage[name] = value
    return analysis, usage


def _validate_analysis(value: Any, snapshot_id: str, known_sources: set[str]) -> None:
    if not isinstance(value, dict):
        raise AIError("模型分析结构无效，请重试。")
    allowed = {"snapshot_id", "overall_conclusion", "management_priorities", "sections"}
    if set(value) != allowed:
        raise AIError("模型分析包含未知字段，请重试。")
    if value["snapshot_id"] != snapshot_id:
        raise AIError("模型响应对应的快照已过期，请重新生成分析。")
    _meaningful(value["overall_conclusion"], 300, "总体结论")
    priorities = value["management_priorities"]
    if not isinstance(priorities, list) or len(priorities) > 3:
        raise AIError("模型优先建议格式无效，请重试。")
    for priority in priorities:
        if not isinstance(priority, dict) or set(priority) != {
            "recommendation",
            "rationale",
            "conditions",
            "evidence_refs",
        }:
            raise AIError("模型优先建议结构无效，请重试。")
        for name in ("recommendation", "rationale", "conditions"):
            _meaningful(priority[name], 1500, "优先建议")
        _refs(priority["evidence_refs"], known_sources)
    sections = value["sections"]
    if not isinstance(sections, list) or [
        section.get("id") if isinstance(section, dict) else None for section in sections
    ] != list(SECTION_TITLES):
        raise AIError("模型板块缺失、顺序错误或包含未知板块，请重试。")
    for section in sections:
        if set(section) != {"id", "title", "summary", "paragraphs", "limitations", "evidence_refs"}:
            raise AIError("模型板块结构无效，请重试。")
        if section["title"] != SECTION_TITLES[section["id"]]:
            raise AIError("模型板块标题无效，请重试。")
        _meaningful(section["summary"], 500, "板块摘要")
        paragraphs = section["paragraphs"]
        if not isinstance(paragraphs, list) or not 1 <= len(paragraphs) <= 6:
            raise AIError("模型板块正文格式无效，请重试。")
        for paragraph in paragraphs:
            _meaningful(paragraph, 3000, "板块正文")
        if sum(character.isalnum() for paragraph in paragraphs for character in paragraph) < 30:
            raise AIError("模型板块正文过短，未形成完整业务分析，请重新生成。")
        limitations = section["limitations"]
        if not isinstance(limitations, list) or len(limitations) > 2:
            raise AIError("模型局限说明格式无效，请重试。")
        for limitation in limitations:
            _meaningful(limitation, 1500, "局限说明")
        _refs(section["evidence_refs"], known_sources)


def _refs(value: Any, known_sources: set[str]) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AIError("模型来源引用格式无效，请重试。")
    if any(item not in known_sources for item in value):
        raise AIError("模型引用了快照中不存在的来源，请重新生成分析。")


def _meaningful(value: Any, maximum: int, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AIError(f"模型{label}为空或过长，请重试。")


def _required_string(value: Any) -> str:
    value = _string(value)
    if not value.strip():
        raise ValueError
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError
    return value
