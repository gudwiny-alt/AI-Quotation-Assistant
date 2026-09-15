from quote_app.desktop_ai import SummaryState


def test_wait_time_uses_actual_request_clock_without_ui_or_business_state(monkeypatch):
    import quote_app.desktop_ai as module

    class ThreadStub:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(module, "Thread", ThreadStub)
    monkeypatch.setattr(module.time, "monotonic", lambda: 1000.0)
    state = SummaryState()
    state.start(None, {"snapshot_id": "one"})
    monkeypatch.setattr(module.time, "monotonic", lambda: 1124.9)
    assert state.elapsed_seconds == 124
    assert state.busy
    state.queue.put((None, "请求超时"))
    assert state.collect()
    assert not state.busy
    state.start(None, {"snapshot_id": "two"})
    assert state.elapsed_seconds == 0


def test_stream_progress_is_consumed_without_finishing_or_replacing_old_result():
    state = SummaryState()
    state.busy = True
    state.result = {"old": "keep"}
    state.progress_queue.put({"stage": "receiving", "answer_chars": 240})
    assert not state.collect()
    assert state.progress["answer_chars"] == 240
    assert state.busy and state.result == {"old": "keep"}
