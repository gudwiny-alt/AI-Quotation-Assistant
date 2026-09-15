"""Streaming must retain validated advice, reject truncation, and reveal no reasoning/secrets."""

import io
import json
import socket

import pytest

from quote_app.services.ai_gateway import AIError, AISettings, generate_analysis
from tests.test_ai_gateway import _analysis, _payload


def event(delta=None, *, finish=None, usage=None):
    value = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        value = {"choices": [], "usage": usage}
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\r\n\r\n").encode()


def stream_bytes(*, tail=True, finish="stop"):
    content = json.dumps(_analysis(), ensure_ascii=False)
    raw = b": heartbeat\r\n\r\n" + event({"reasoning_content": "PRIVATE REASONING"})
    for part in (content[:75], content[75:]):
        raw += event({"content": part})
    raw += event(finish=finish) + event(usage={"total_tokens": 35})
    return raw + (b"data: [DONE]\r\n\r\n" if tail else b"")


def fake_transport(monkeypatch, raw, *, delay=0, split=13):
    import quote_app.services.ai_gateway as module

    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    requests = []

    class Response(io.BytesIO):
        headers = {"Content-Type": "text/event-stream; charset=utf-8"}
        status = 200

        def read1(self, n=-1):
            clock[0] += delay
            return super().read(min(n, split))

    class Transport:
        def open(self, request, timeout):
            requests.append(json.loads(request.data))
            return Response(raw)

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_: Transport())
    return requests


def test_stream_assembles_split_utf8_and_usage_with_safe_progress(monkeypatch, tmp_path):
    requests = fake_transport(monkeypatch, stream_bytes())
    progress = []
    log = tmp_path / "ai-requests.jsonl"
    result = generate_analysis(
        AISettings("https://example.test/v1", "model", "SECRET"),
        _payload(),
        "BUSINESS PROMPT",
        on_progress=progress.append,
        diagnostic_path=log,
    )
    assert result["analysis"] == _analysis()
    assert result["usage"] == {"total_tokens": 35}
    assert requests[0]["stream"] is True
    phases = [p["stage"] for p in progress]
    assert "thinking" in phases and "receiving" in phases and phases[-1] == "complete"
    serialized = log.read_text() + json.dumps(progress)
    for secret in ("SECRET", "PRIVATE REASONING", "BUSINESS PROMPT", "example.test", "本批次报价"):
        assert secret not in serialized
    assert log.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "raw,reason",
    [
        (stream_bytes(tail=False), "中断"),
        (stream_bytes(finish="length"), "截断"),
        (
            event({"tool_calls": [{"function": {"name": "write_excel"}}]}) + b"data: [DONE]\n\n",
            "工具",
        ),
        (b'data: {"error":{"message":"SECRET"}}\n\ndata: [DONE]\n\n', "服务"),
        (b"data: nonsense\n\n", "格式"),
    ],
)
def test_incomplete_or_unsafe_stream_never_produces_advice(monkeypatch, raw, reason):
    fake_transport(monkeypatch, raw)
    with pytest.raises(AIError, match=reason) as error:
        generate_analysis(
            AISettings("https://example.test/v1", "m", "SECRET"), _payload(), "prompt"
        )
    assert "SECRET" not in str(error.value)


def test_continuous_generation_can_exceed_old_total_wait(monkeypatch):
    fake_transport(monkeypatch, stream_bytes(), delay=60, split=300)
    result = generate_analysis(
        AISettings("https://example.test/v1", "m", "s"), _payload(), "prompt"
    )
    assert result["elapsed_seconds"] > 300
    assert result["analysis"] == _analysis()


def test_stream_has_total_deadline_even_while_receiving(monkeypatch):
    fake_transport(monkeypatch, stream_bytes(), delay=100, split=100)
    with pytest.raises(AIError, match="总时限"):
        generate_analysis(AISettings("https://example.test/v1", "m", "s"), _payload(), "prompt")


def test_failure_diagnostic_distinguishes_no_headers_and_does_not_log_exception(
    monkeypatch, tmp_path
):
    import urllib.request

    class Transport:
        def open(self, request, timeout):
            raise socket.timeout("SECRET URL key")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Transport())
    log = tmp_path / "ai-requests.jsonl"
    with pytest.raises(AIError, match="响应"):
        generate_analysis(
            AISettings("https://example.test/v1", "m", "SECRET"),
            _payload(),
            "prompt",
            diagnostic_path=log,
        )
    text = log.read_text()
    assert "SECRET" not in text and "example.test" not in text
    assert "timeout" in text and "waiting" in text


def test_diagnostic_symlink_is_not_followed(monkeypatch, tmp_path):
    fake_transport(monkeypatch, stream_bytes())
    target = tmp_path / "keep"
    target.write_text("keep")
    link = tmp_path / "log"
    link.symlink_to(target)
    result = generate_analysis(
        AISettings("https://example.test/v1", "m", "s"), _payload(), "prompt", diagnostic_path=link
    )
    assert result["analysis"] == _analysis()
    assert target.read_text() == "keep"


def test_wire_source_aliases_are_lossless(monkeypatch):
    import quote_app.services.ai_gateway as module

    captured = []
    original = _payload()
    returned = _analysis()
    for entry in returned["management_priorities"] + returned["sections"]:
        entry["evidence_refs"] = ["S001"]

    class Transport:
        def open(self, request, timeout):
            body = json.loads(request.data)
            captured.append(body)
            return io.BytesIO(
                json.dumps(
                    {
                        "choices": [
                            {"finish_reason": "stop", "message": {"content": json.dumps(returned)}}
                        ]
                    }
                ).encode()
            )

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_: Transport())
    result = generate_analysis(AISettings("https://example.test/v1", "m", "s"), original, "prompt")
    assert result["analysis"] == _analysis()
    assert original == _payload()
    sent = json.loads(captured[0]["messages"][1]["content"])
    assert sent["sources"] == {"S001": [{"sku": "A", "price": 99}]}


def test_chunked_disconnect_is_safe_and_records_failed_stage(monkeypatch, tmp_path):
    import http.client
    import urllib.request

    class Response(io.BytesIO):
        headers = {"Content-Type": "text/event-stream"}
        status = 200

        def read1(self, n=-1):
            raise http.client.IncompleteRead(b"SECRET private response", 100)

    class Transport:
        def open(self, request, timeout):
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Transport())
    path = tmp_path / "log"
    with pytest.raises(AIError, match="中断") as caught:
        generate_analysis(
            AISettings("https://example.test/v1", "m", "SECRET"),
            _payload(),
            "prompt",
            diagnostic_path=path,
        )
    assert "SECRET" not in str(caught.value) + path.read_text()
    assert json.loads(path.read_text().splitlines()[-1])["stage"] == "failed"


def test_fifo_diagnostic_cannot_block_model_request(tmp_path):
    import os
    import subprocess
    import sys

    path = tmp_path / "fifo"
    os.mkfifo(path)
    script = 'from quote_app.services.ai_diagnostics import RequestTrace; import sys; RequestTrace(sys.argv[1]).emit("waiting"); print("finished")'
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=2
    )
    assert result.returncode == 0 and result.stdout.strip() == "finished"


def test_formally_valid_but_empty_business_analysis_is_rejected(monkeypatch):
    import urllib.request

    value = _analysis()
    for section in value["sections"]:
        section["paragraphs"] = ["。"]

    class Transport:
        def open(self, request, timeout):
            return io.BytesIO(
                json.dumps(
                    {
                        "choices": [
                            {"finish_reason": "stop", "message": {"content": json.dumps(value)}}
                        ]
                    }
                ).encode()
            )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Transport())
    with pytest.raises(AIError, match="正文"):
        generate_analysis(AISettings("https://example.test/v1", "m", "s"), _payload(), "prompt")


def test_nested_wire_aliases_keep_original_links_and_reject_unknown():
    from quote_app.services.ai_gateway import _wire_payload, _validate_envelope

    payload = {
        "snapshot_id": "snapshot-7",
        "sources": {"product:long": "产品", "check:long": "检查"},
        "products": [{"ref": "product:long", "channels": [{"ref": "check:long"}]}],
        "checks": [{"ref": "check:long"}],
    }
    original = json.loads(json.dumps(payload))
    wire, aliases = _wire_payload(payload)
    assert wire["products"][0]["ref"] == "S001"
    assert wire["products"][0]["channels"][0]["ref"] == "S002"
    assert wire["checks"][0]["ref"] == "S002"
    assert payload == original
    value = _analysis()
    value["sections"][0]["evidence_refs"] = ["S999"]
    env = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}
    with pytest.raises(AIError, match="来源"):
        _validate_envelope(env, "snapshot-7", {"quote_rows"}, aliases)


def test_oversized_stream_is_bounded(monkeypatch):
    fake_transport(monkeypatch, b"data: " + b"x" * (4 * 1024 * 1024 + 1), split=8192)
    with pytest.raises(AIError, match="过大"):
        generate_analysis(AISettings("https://example.test/v1", "m", "s"), _payload(), "prompt")


def test_diagnostic_rotation_retains_only_bounded_operational_files(tmp_path):
    from quote_app.services.ai_diagnostics import RequestTrace

    path = tmp_path / "ai-requests.jsonl"
    path.write_text("x" * (1024 * 1024))
    RequestTrace(path).emit("waiting", request_bytes=12)
    assert path.stat().st_size < 1024
    assert path.with_suffix(".previous.jsonl").stat().st_size == 1024 * 1024
    assert json.loads(path.read_text())["request_bytes"] == 12


def test_plain_stream_has_explicit_json_contract_without_platform_format(monkeypatch):
    requests = fake_transport(monkeypatch, stream_bytes())
    result = generate_analysis(
        AISettings('https://example.test/v1', 'model', 'secret'), _payload(), 'advisor prompt'
    )
    request = requests[0]
    assert 'response_format' not in request
    assert request['thinking'] == {'type': 'enabled'}
    assert request['max_tokens'] >= 16000  # reasoning and final advice share the output budget
    template = json.loads(request['messages'][-1]['content'].split('只返回最终JSON：', 1)[1])
    assert template['snapshot_id'] == 'snapshot-7'
    assert [s['id'] for s in template['sections']] == ['batch', 'market', 'pricing', 'risk']
    assert result['analysis'] == _analysis()
