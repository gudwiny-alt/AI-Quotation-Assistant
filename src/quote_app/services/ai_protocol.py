"""Bounded SSE transport and a JSON text contract for advisory output only."""

import json
import time


class AIError(Exception):
    """Safe user-facing error, never a provider response body."""


SECTION_TITLES = {
    "batch": "AI批次总结",
    "market": "价格情报理解",
    "pricing": "报价方案推理",
    "risk": "稽核异常分析",
}
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_STREAM_BYTES = 4 * 1024 * 1024
TOTAL_TIMEOUT_SECONDS = 600


def output_contract(snapshot_id):
    """Ask for ordinary JSON text; provider schema constraints are intentionally absent."""
    sample = {
        "snapshot_id": snapshot_id,
        "overall_conclusion": "",
        "management_priorities": [
            {"recommendation": "", "rationale": "", "conditions": "", "evidence_refs": []}
        ],
        "sections": [
            {"id": key, "title": title, "summary": "", "paragraphs": [""],
             "limitations": [], "evidence_refs": []}
            for key, title in SECTION_TITLES.items()
        ],
    }
    return (
        "请分析上面提供的当前批次业务数据，直接生成可展示的四项中文业务分析。"
        "请填充以下 JSON 结构中的空字符串，正文每项约200至300字、2至3段；"
        "资料不足时说明具体缺项。不得返回模板、Schema或单个标点，不要输出大量空白。"
        "snapshot_id必须保持以下值，evidence_refs从上面sources的S编号选取，每项最多8个。"
        "只返回最终JSON：" + json.dumps(sample, ensure_ascii=False)
    )


def read_response(response, started, notify):
    """Collect deltas without exposing reasoning; require both stop and DONE downstream."""
    deadline = started + TOTAL_TIMEOUT_SECONDS
    content_type = getattr(response, "headers", {}).get("Content-Type", "").lower()
    streaming = "text/event-stream" in content_type
    raw = bytearray()
    pending = bytearray()
    event_lines = []
    answer = []
    answer_size = 0
    frames = 0
    reasoning_chars = 0
    answer_chars = 0
    usage = {}
    finish = None
    done = False

    def consume(lines):
        nonlocal frames, reasoning_chars, answer_chars, answer_size, usage, finish, done
        data = b"\n".join(line[5:].lstrip(b" ") for line in lines if line.startswith(b"data:"))
        if not data:
            return
        if data.strip() == b"[DONE]":
            done = True
            return
        try:
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError
            if "error" in value:
                raise AIError("模型服务在生成过程中返回错误，本次分析未完成。")
            choices = value.get("choices", [])
            if not isinstance(choices, list):
                raise ValueError
            frames += 1
            if value.get("usage") is not None:
                usage = value["usage"]
            for choice in choices:
                if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                    raise ValueError
                delta = choice.get("delta", {})
                if not isinstance(delta, dict):
                    raise ValueError
                if delta.get("tool_calls") or delta.get("function_call"):
                    raise AIError("模型返回了工具调用，当前分析不允许执行工具。")
                if delta.get("refusal"):
                    raise AIError("模型未提供本次分析，请检查分析范围。")
                thought = delta.get("reasoning_content") or delta.get("reasoning") or ""
                text = delta.get("content") or ""
                if not isinstance(thought, str) or not isinstance(text, str):
                    raise ValueError
                reasoning_chars += len(thought)
                answer_chars += len(text)
                answer_size += len(text.encode("utf-8"))
                if answer_size > MAX_RESPONSE_BYTES:
                    raise AIError("模型响应超过 1MB，请缩小分析范围后重试。")
                if text:
                    answer.append(text)
                if choice.get("finish_reason"):
                    if finish is not None:
                        raise ValueError
                    finish = choice["finish_reason"]
            phase = "receiving" if answer_chars else "thinking" if reasoning_chars else "connected"
            notify(phase, frames=frames, answer_chars=answer_chars, reasoning_chars=reasoning_chars)
        except (ValueError, TypeError, UnicodeError):
            raise AIError("模型流式响应格式无效，本次分析未完成。") from None

    while not done:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AIError("模型分析超过 10 分钟总时限，本次分析未完成。")
        # urllib's HTTPResponse exposes its connected socket here. Bound each read
        # by the remaining total budget as well as the normal idle read timeout.
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(min(300, remaining))
        chunk = response.read1(8192)
        if time.monotonic() >= deadline:
            raise AIError("模型分析超过 10 分钟总时限，本次分析未完成。")
        if not chunk:
            break
        raw.extend(chunk)
        if len(raw) > (MAX_STREAM_BYTES if streaming else MAX_RESPONSE_BYTES):
            raise AIError("模型响应过大，本次分析未完成，请缩小分析范围。")
        if not streaming:
            continue
        pending.extend(chunk)
        while b"\n" in pending and not done:
            line, _, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            line = line.rstrip(b"\r")
            if line:
                event_lines.append(line)
            else:
                consume(event_lines)
                event_lines = []

    if not streaming:
        try:
            return json.loads(raw)
        except (UnicodeError, ValueError):
            raise AIError("模型服务返回的 JSON 无法解析，请重试。") from None
    if not done or finish is None:
        raise AIError("模型流式连接中断，未收到完整结束标记，请重新生成分析。")
    return {
        "choices": [{"finish_reason": finish, "message": {"content": "".join(answer)}}],
        "usage": usage,
    }
