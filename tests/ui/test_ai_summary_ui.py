import os
import pytest
from tests.ui.test_desktop_workbench import workbench as base_workbench
from tests.test_review_support import session as base_session
from quote_app.desktop_ai import AISummaryView, SummaryState

pytestmark = pytest.mark.skipif(
    os.environ.get("QUOTE_NATIVE_UI_TESTS") != "1", reason="Requires desktop"
)


@pytest.fixture
def workbench(tmp_path):
    yield from base_workbench.__wrapped__(tmp_path)


@pytest.fixture
def session(tmp_path):
    return base_session.__wrapped__(tmp_path)


def test_summary_navigation_is_readonly_and_does_not_trigger_generation(workbench, session):
    before = session.quote_path.read_bytes()
    workbench._review_session = session
    workbench._review_manual = True
    workbench.show_page("ai_summary")
    workbench.root.update()
    assert isinstance(workbench._review_view, AISummaryView)
    assert "尚未生成" in workbench._review_view.conclusion.cget("text")
    assert workbench.brand_title.cget("text") == "报价协同智能体"
    assert not workbench._ai_state.busy
    workbench.show_page("overview")
    workbench.root.update()
    assert session.quote_path.read_bytes() == before
    assert not session.store_path.exists()


def test_late_reply_is_not_current_after_price_changes(workbench, session):
    workbench._review_session = session
    workbench._review_manual = True
    workbench.show_page("ai_summary")
    page = workbench._review_view
    original = page.snapshot
    workbench._ai_state.snapshot = original
    workbench._ai_state.result = {
        "analysis": {"overall_conclusion": "测试建议", "management_priorities": [], "sections": []},
        "model": "test",
        "generated_at": "now",
        "elapsed_seconds": 1,
        "usage": {},
    }
    page.render()
    session.products[0].values["K"] = "2080"
    page.refresh(force=True)
    assert "已变化" in page.notice.cget("text")
    assert workbench._ai_state.result["analysis"]["overall_conclusion"] == "测试建议"
    assert "测试建议" in page.conclusion.cget("text")


def test_worker_failure_arrives_as_safe_error_without_tk_reference():
    import time

    def fail(*_, **kwargs):
        raise RuntimeError("secret-key-never-show")

    state = SummaryState()
    state.start(None, {"snapshot_id": "x"}, gateway=fail)
    for _ in range(100):
        if state.collect():
            break
        time.sleep(0.01)
    assert not state.busy and "secret" not in state.error
    assert state.error


def test_successful_generation_and_local_key_settings(workbench, session, tmp_path, monkeypatch):
    import time
    from quote_app.services.ai_gateway import AISettings, save_settings
    from quote_app.desktop_ai import settings_path, show_ai_settings
    from quote_app.services.ai_summary import SECTION_TITLES

    workbench._review_session = session
    workbench._review_manual = True
    save_settings(
        settings_path(workbench),
        AISettings(
            base_url="https://example.test/v1", model="test-model", api_key="private-key-ui-test"
        ),
    )
    workbench.show_page("ai_summary")
    page = workbench._review_view
    state = workbench._ai_state

    def fake(settings, snapshot, _prompt, **kwargs):
        return {
            "analysis": {
                "snapshot_id": snapshot["snapshot_id"],
                "overall_conclusion": "本批次应重点关注采购条件与报价空间，结合有效证据安排后续报价。",
                "management_priorities": [
                    {
                        "recommendation": "优先关注采购条件",
                        "rationale": "已知上限约束可报价空间，应结合采购条件判断。",
                        "conditions": "数量与费用尚未提供。",
                        "evidence_refs": ["batch"],
                    }
                ],
                "sections": [
                    {
                        "id": key,
                        "title": title,
                        "summary": "从整批数据理解经营取舍与复核重点。",
                        "paragraphs": [
                            "现有数据能够支撑报价规则比较；建议结合采购条件和渠道覆盖评估经营取舍。",
                            "对于缺少足够依据的结论，保留不确定性，并把有限复核资源用于关键事项。",
                        ],
                        "limitations": ["未提供销量及费用。"],
                        "evidence_refs": ["batch"],
                    }
                    for key, title in zip(("batch", "market", "pricing", "risk"), SECTION_TITLES)
                ],
            },
            "model": "测试服务（界面验证）",
            "generated_at": "本机测试",
            "elapsed_seconds": 0.1,
            "usage": {},
        }

    state.start(
        AISettings(
            base_url="https://example.test/v1", model="test-model", api_key="private-key-ui-test"
        ),
        page.snapshot,
        gateway=fake,
    )
    for _ in range(60):
        workbench.root.update()
        if state.result:
            break
        time.sleep(0.01)
    assert state.result and not state.busy
    assert "本批次应重点" in page.conclusion.cget("text")
    assert len(page.tiles) == 4
    assert "private-key-ui-test" not in page._source_text()
    assert "检查" in page._source_text()
    dialog = show_ai_settings(workbench)
    workbench.root.update()

    def walk(widget):
        yield widget
        for child in widget.winfo_children():
            yield from walk(child)

    fields = [w for w in walk(dialog) if w.winfo_class() == "Entry"]
    assert any(w.cget("show") == "•" and w.get() == "" for w in fields)
    dialog.destroy()
    workbench.root.deiconify()
    workbench.root.geometry("1430x900+0+35")
    workbench.root.update()
    if os.environ.get("QUOTE_AI_CAPTURE"):
        import Quartz
        import subprocess

        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
        )
        target = next(
            w
            for w in windows
            if w.get("kCGWindowOwnerPID") == os.getpid()
            and w.get("kCGWindowLayer") == 0
            and w.get("kCGWindowBounds", {}).get("Width", 0) > 900
        )
        subprocess.run(
            [
                "/usr/sbin/screencapture",
                "-x",
                "-l",
                str(target["kCGWindowNumber"]),
                os.environ["QUOTE_AI_CAPTURE"],
            ],
            check=True,
        )


@pytest.mark.parametrize("geometry", ["1000x720", "1430x900"])
def test_ai_responsive_cards_and_scroll_reach_footer(workbench, session, geometry):
    workbench._review_session = session
    workbench._review_manual = True
    workbench.show_page("ai_summary")
    root = workbench.root
    root.deiconify()
    root.geometry(geometry)
    root.update()
    page = workbench._review_view
    width = page.content.winfo_width()
    for tile in page.tiles + page.priority_tiles:
        assert tile.winfo_x() + tile.winfo_width() <= width + 2
    canvas = page.content._workbench_scroll_canvas
    canvas.yview_moveto(1)
    root.update()
    assert (
        page.footer.winfo_rooty() + page.footer.winfo_height()
        <= canvas.winfo_rooty() + canvas.winfo_height() + 2
    )


def test_waiting_notice_displays_elapsed_time_without_claiming_completion(workbench, session):
    import time

    workbench._review_session = session
    workbench._review_manual = True
    workbench.show_page("ai_summary")
    state = workbench._ai_state
    state.busy = True
    state.started_at = time.monotonic() - 124
    page = workbench._review_view
    page._status()
    assert "2 分 4 秒" in page.notice.cget("text")
    assert "正在等待" in page.notice.cget("text")
    assert workbench.start_button.cget("state") == "disabled"
    state.busy = False
    page._status()
    assert "正在等待" not in page.notice.cget("text")


def test_stream_status_shows_actual_reception_and_keeps_generation_disabled(workbench, session):
    workbench._review_session = session
    workbench._review_manual = True
    workbench.show_page("ai_summary")
    state = workbench._ai_state
    state.busy = True
    state.progress = {"stage": "thinking", "reasoning_chars": 123}
    page = workbench._review_view
    page._status()
    assert "模型正在分析" in page.notice.cget("text")
    state.progress = {"stage": "receiving", "answer_chars": 456}
    page._status()
    assert "456" in page.notice.cget("text") and "接收" in page.notice.cget("text")
    assert workbench.start_button.cget("state") == "disabled"
