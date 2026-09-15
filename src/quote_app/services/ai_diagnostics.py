"""Local bounded operational events: no prompts, credentials, URLs or model content."""

import json
import os
from pathlib import Path
import stat
from threading import Lock
import time
from datetime import datetime, timezone
from uuid import uuid4

_LOCK = Lock()
_LIMIT = 1024 * 1024
_STAGES = {"waiting", "connected", "thinking", "receiving", "validating", "complete", "failed"}
_NUMBERS = {"request_bytes", "frames", "answer_chars", "reasoning_chars", "http_status"}
_CODES = {"timeout", "http_error", "network_error", "validation_error"}


class RequestTrace:
    def __init__(self, path=None, callback=None):
        self.path = Path(path) if path else None
        self.callback = callback
        self.started = time.monotonic()
        self.request_id = uuid4().hex[:12]
        self.stage = "waiting"
        self.last_log = -10.0
        self.previous = None
        self.last_callback = -1.0

    def emit(self, stage, **values):
        if stage not in _STAGES:
            return
        self.stage = stage
        now = time.monotonic()
        event = {
            "request_id": self.request_id,
            "stage": stage,
            "elapsed_seconds": round(now - self.started, 3),
        }
        event.update(
            {
                k: v
                for k, v in values.items()
                if k in _NUMBERS and type(v) in (int, float) and v >= 0
            }
        )
        if values.get("error_code") in _CODES:
            event["error_code"] = values["error_code"]
        if values.get("failed_stage") in _STAGES:
            event["failed_stage"] = values["failed_stage"]
        changed = stage != self.previous
        if self.callback and (changed or now - self.last_callback >= 0.25):
            try:
                self.callback(dict(event))
            except Exception:
                pass  # UI progress failure must not discard an otherwise valid response.
            self.last_callback = now
        if changed or now - self.last_log >= 10:
            self._write({**event, "time": datetime.now(timezone.utc).isoformat()})
            self.last_log = now
        self.previous = stage

    def _write(self, event):
        if not self.path:
            return
        try:
            with _LOCK:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.path.is_symlink() or (
                    self.path.exists() and not stat.S_ISREG(self.path.stat().st_mode)
                ):
                    return
                if self.path.exists() and self.path.stat().st_size >= _LIMIT:
                    os.replace(self.path, self.path.with_suffix(".previous.jsonl"))
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                )
                with os.fdopen(descriptor, "a", encoding="utf-8") as output:
                    if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
                        return
                    os.fchmod(output.fileno(), 0o600)
                    output.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass  # Diagnostic storage is never a prerequisite for advice.
