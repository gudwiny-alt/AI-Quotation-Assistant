from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from quote_app.services.ai_gateway import (
    AIError,
    AISettings,
    generate_analysis,
    load_settings,
    save_settings,
)


SECTION_TITLES = {
    "batch": "AI批次总结",
    "market": "价格情报理解",
    "pricing": "报价方案推理",
    "risk": "稽核异常分析",
}


def _analysis(snapshot_id: str = "snapshot-7") -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "overall_conclusion": "本批次报价可继续人工复核。",
        "management_priorities": [
            {
                "recommendation": "复核价格差异",
                "rationale": "渠道价格存在差异",
                "conditions": "仅作为人工决策参考",
                "evidence_refs": ["quote_rows"],
            }
        ],
        "sections": [
            {
                "id": section_id,
                "title": title,
                "summary": f"{title}摘要",
                "paragraphs": [f"{title}分析内容"],
                "limitations": ["数据仅覆盖当前快照"],
                "evidence_refs": ["quote_rows"],
            }
            for section_id, title in SECTION_TITLES.items()
        ],
    }


def _response(analysis: dict[str, Any] | str | None = None, **extra: Any) -> dict[str, Any]:
    content = (
        json.dumps(analysis or _analysis(), ensure_ascii=False)
        if not isinstance(analysis, str)
        else analysis
    )
    response = {
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
    }
    response.update(extra)
    return response


@contextmanager
def _server(
    response: dict[str, Any], *, status: int = 200, raw: bytes | None = None
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            encoded = raw if raw is not None else json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _payload() -> dict[str, Any]:
    return {"snapshot_id": "snapshot-7", "sources": {"quote_rows": [{"sku": "A", "price": 99}]}}


def test_settings_defaults_readiness_and_secret_repr() -> None:
    settings = AISettings(
        base_url="https://example.test/v1", model="test-model", api_key="super-secret-key"
    )
    assert settings.base_url == "https://example.test/v1"
    assert settings.model == "test-model"
    assert settings.ready is True
    assert "super-secret-key" not in repr(settings)
    assert AISettings().ready is False


def test_settings_round_trip_uses_owner_only_atomic_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "ai.json"
    save_settings(path, AISettings(base_url="https://example.test/v1", model="m", api_key="secret"))
    assert load_settings(path) == AISettings(
        base_url="https://example.test/v1", model="m", api_key="secret"
    )
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert not list(path.parent.glob("*.tmp"))


def test_save_refuses_symlink_without_changing_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "settings.json"
    link.symlink_to(target)
    with pytest.raises(AIError, match="符号链接"):
        save_settings(
            link,
            AISettings(base_url="https://example.test/v1", model="test-model", api_key="secret"),
        )
    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "settings",
    [
        AISettings(base_url="http://example.com/v1", model="test-model", api_key="secret"),
        AISettings(base_url="https://example.com/v1?token=x", model="test-model", api_key="secret"),
        AISettings(model="", api_key="secret"),
    ],
)
def test_save_rejects_invalid_settings(tmp_path: Path, settings: AISettings) -> None:
    with pytest.raises(AIError):
        save_settings(tmp_path / "settings.json", settings)
    assert not (tmp_path / "settings.json").exists()


def test_corrupt_settings_has_safe_error(tmp_path: Path) -> None:
    path = tmp_path / "ai.json"
    path.write_text('{"api_key":"leaked-secret",broken', encoding="utf-8")
    with pytest.raises(AIError) as caught:
        load_settings(path)
    assert "leaked-secret" not in str(caught.value)
    assert "配置" in str(caught.value)


def test_posts_openai_compatible_read_only_request_and_returns_metadata() -> None:
    payload = _payload()
    original = json.loads(json.dumps(payload))
    with _server(_response()) as (base_url, requests):
        result = generate_analysis(
            AISettings(base_url=base_url, model="local-model", api_key="private-key"),
            payload,
            "只分析",
        )
    assert payload == original
    request = requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer private-key"
    body = json.loads(request["body"])
    assert body == {
        "model": "local-model",
        "messages": [
            {"role": "system", "content": "只分析"},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        ],
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    assert result["analysis"] == _analysis()
    assert result["model"] == "local-model"
    assert result["usage"] == {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33}
    assert result["elapsed_seconds"] >= 0
    assert result["generated_at"].endswith("+00:00")


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.com/v1",
        "https://user:password@example.com/v1",
        "https://example.com/v1?token=x",
        "https://example.com/v1#fragment",
        "ftp://example.com/v1",
    ],
)
def test_rejects_unsafe_or_invalid_urls_without_leaking_key(base_url: str) -> None:
    with pytest.raises(AIError) as caught:
        generate_analysis(
            AISettings(base_url=base_url, model="test-model", api_key="private-key"),
            _payload(),
            "prompt",
        )
    assert "private-key" not in str(caught.value)


def test_redirect_is_blocked_and_authorization_is_not_forwarded() -> None:
    with _server(_response()) as (destination, destination_requests):

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.send_response(307)
                self.send_header("Location", f"{destination}/chat/completions")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        redirector = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=redirector.serve_forever, daemon=True)
        thread.start()
        try:
            settings = AISettings(
                base_url=f"http://127.0.0.1:{redirector.server_port}/v1",
                model="test-model",
                api_key="private-key",
            )
            with pytest.raises(AIError, match="重定向"):
                generate_analysis(settings, _payload(), "prompt")
        finally:
            redirector.shutdown()
            redirector.server_close()
            thread.join()
    assert destination_requests == []


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (_response("```json\n{}\n```"), "代码块"),
        (_response({**_analysis(), "snapshot_id": "old"}), "快照"),
        (
            _response(
                {
                    **_analysis(),
                    "sections": [
                        *_analysis()["sections"],
                        {
                            "id": "other",
                            "title": "其他",
                            "summary": "x",
                            "paragraphs": ["x"],
                            "limitations": [],
                            "evidence_refs": [],
                        },
                    ],
                }
            ),
            "板块",
        ),
        (
            _response(
                {
                    **_analysis(),
                    "management_priorities": [
                        {**_analysis()["management_priorities"][0], "evidence_refs": ["unknown"]}
                    ],
                }
            ),
            "来源",
        ),
        ({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}, "截断"),
        (
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {"content": "{}", "tool_calls": [{}]},
                    }
                ]
            },
            "工具",
        ),
    ],
)
def test_rejects_malformed_stale_unknown_or_unsafe_model_responses(
    response: dict[str, Any], message: str
) -> None:
    with _server(response) as (base_url, _):
        with pytest.raises(AIError, match=message):
            generate_analysis(
                AISettings(base_url=base_url, model="test-model", api_key="secret"),
                _payload(),
                "prompt",
            )


def test_never_exposes_response_body_or_key_in_http_error() -> None:
    with _server({}, status=401, raw=b"private server detail: api-key-value") as (base_url, _):
        with pytest.raises(AIError) as caught:
            generate_analysis(
                AISettings(base_url=base_url, model="test-model", api_key="api-key-value"),
                _payload(),
                "prompt",
            )
    error = str(caught.value)
    assert "api-key-value" not in error
    assert "private server detail" not in error
    assert "密钥" in error


@pytest.mark.parametrize(
    "response",
    [
        {"choices": [None]},
        {"choices": [{"finish_reason": "stop", "message": None}]},
        {"choices": [{"finish_reason": "stop", "message": []}]},
    ],
)
def test_invalid_choice_and_message_types_raise_safe_error(response: dict[str, Any]) -> None:
    with _server(response) as (base_url, _):
        with pytest.raises(AIError, match="结构"):
            generate_analysis(
                AISettings(base_url=base_url, model="test-model", api_key="secret"),
                _payload(),
                "prompt",
            )


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": True},
        {"prompt_tokens": 1.5},
        {"prompt_tokens": -1},
    ],
)
def test_usage_requires_nonnegative_integers(usage: dict[str, Any]) -> None:
    response = _response()
    response["usage"] = usage
    with _server(response) as (base_url, _):
        with pytest.raises(AIError, match="用量"):
            generate_analysis(
                AISettings(base_url=base_url, model="test-model", api_key="secret"),
                _payload(),
                "prompt",
            )


def test_missing_usage_is_returned_as_unavailable_not_zero() -> None:
    response = _response()
    del response["usage"]
    with _server(response) as (base_url, _):
        result = generate_analysis(
            AISettings(base_url=base_url, model="test-model", api_key="secret"),
            _payload(),
            "prompt",
        )
    assert result["usage"] == {}


def test_new_install_has_no_bundled_provider_or_credentials(tmp_path):
    settings = load_settings(tmp_path / "ai-settings.json")
    assert settings.base_url == settings.model == settings.api_key == ""
    assert not settings.ready


def test_deep_analysis_can_finish_after_the_old_90_second_limit(monkeypatch):
    """The company service completed the unchanged real request at 124 seconds."""
    from io import BytesIO
    import urllib.request

    class DelayedService:
        def open(self, request, timeout):
            if timeout < 124:
                raise TimeoutError("simulated server still generating at 90 seconds")
            return BytesIO(json.dumps(_response()).encode())

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: DelayedService())
    result = generate_analysis(
        AISettings("https://example.test/v1", "model", "secret"), _payload(), "prompt"
    )
    assert result["analysis"]["snapshot_id"] == _payload()["snapshot_id"]


def test_timeout_remains_bounded_and_error_matches_configured_wait(monkeypatch):
    import urllib.request

    class StalledService:
        def open(self, request, timeout):
            assert timeout == 300
            raise TimeoutError("secret must not appear")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: StalledService())
    with pytest.raises(AIError, match="300 秒") as caught:
        generate_analysis(
            AISettings("https://example.test/v1", "model", "secret"), _payload(), "prompt"
        )
    assert "secret" not in str(caught.value)
