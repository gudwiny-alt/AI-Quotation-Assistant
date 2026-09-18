"""Durable restart recovery through the same public pipeline used by the desktop."""
from dataclasses import replace
from decimal import Decimal
import sqlite3

import pytest
from openpyxl import load_workbook

from quote_app.domain.models import QuoteMonth
from quote_app.services.full_pipeline import FullPipelineRequest, run_full_pipeline
from quote_app.services.web_run import WebsiteRunController, WebsiteRunSummary
from quote_app.tasks.models import TaskState, RunState, WebsiteChannel
from quote_app.tasks.repository import SQLiteTaskRepository
from tests.integration.test_full_pipeline_fixture_sites import _input_paths
from tests.factories.web_run_factory import make_business_result, make_technical_result


def interrupted_run(tmp_path, *, waiting=False):
    request = FullPipelineRequest(
        paths=_input_paths(tmp_path), quote_month=QuoteMonth(2026, 8),
        browser_profile_dir=tmp_path / 'profile', database_path=tmp_path / 'tasks.db',
        evidence_dir=tmp_path / 'evidence',
    )
    saved = {}
    def crash(web):
        saved['run_id'], saved['tasks'] = web.run_id, web.tasks
        web.evidence_dir.mkdir(exist_ok=True)
        with SQLiteTaskRepository(web.database_path) as repo:
            for task in web.tasks:
                repo.upsert_task(task)
            first, second, third = web.tasks
            token = repo.start_attempt(first.task_id)
            result = make_business_result(web.evidence_dir, first, price=Decimal('2199'))
            repo.save_result(result, token=token)
            saved['result'] = result
            token = repo.start_attempt(second.task_id)
            if waiting:
                repo.mark_waiting_for_login(second.task_id, token=token, site='tmall')
            for _ in range(3):
                token = repo.start_attempt(third.task_id)
                repo.save_result(make_technical_result(third, code='LAYOUT_CHANGED'), token=token)
        raise RuntimeError('模拟断电')
    with pytest.raises(RuntimeError, match='模拟断电'):
        run_full_pipeline(request, website_runner=crash)
    return request, saved


def test_restart_retains_identity_success_and_exhausted_failures(tmp_path):
    request, saved = interrupted_run(tmp_path)
    first, second, third = saved['tasks']
    original = saved['result'].evidence.path.read_bytes()
    seen = []
    def resume(web):
        assert web.run_id == saved['run_id']
        seen.extend(t.task_id for t in web.tasks)
        assert [t.task_id for t in web.tasks] == [second.task_id]
        with SQLiteTaskRepository(web.database_path) as repo:
            assert repo.load_result(first.task_id) == saved['result']
            token = repo.start_attempt(second.task_id)
            repo.save_result(make_business_result(web.evidence_dir, second), token=token)
        return WebsiteRunSummary(1, 0, 0, ())
    result = run_full_pipeline(replace(request, resume_run_id=saved['run_id']), website_runner=resume)
    assert seen == [second.task_id]
    assert result.website_summary.succeeded == 2
    assert result.website_summary.technical_failure == 1
    assert saved['result'].evidence.path.read_bytes() == original
    with SQLiteTaskRepository(request.database_path) as repo:
        assert len(repo.list_attempts(third.task_id)) == 3
        assert repo.task_generation(first.task_id) == 0
        assert repo.load_run(saved['run_id']).quote_path == result.quote_path
    with sqlite3.connect(request.database_path) as db:
        assert db.execute('SELECT count(*) FROM quotation_runs').fetchone()[0] == 1


def test_restart_waiting_reopens_task_and_can_be_interrupted_again(tmp_path):
    request, saved = interrupted_run(tmp_path, waiting=True)
    second = saved['tasks'][1]
    def crash_again(web):
        with SQLiteTaskRepository(web.database_path) as repo:
            assert repo.task_state(second.task_id) is TaskState.PENDING
            repo.start_attempt(second.task_id)
        raise RuntimeError('再次中断')
    with pytest.raises(RuntimeError, match='再次中断'):
        run_full_pipeline(replace(request, resume_run_id=saved['run_id']), website_runner=crash_again)
    called = []
    def resumed(web):
        called.extend(t.task_id for t in web.tasks)
        return WebsiteRunSummary(0, 0, 0, ())
    run_full_pipeline(replace(request, resume_run_id=saved['run_id']), website_runner=resumed)
    assert called == [second.task_id]


def test_export_only_never_launches_browser_or_changes_waiting_task(tmp_path):
    request, saved = interrupted_run(tmp_path, waiting=True)
    def forbidden(*args):
        pytest.fail('仅导出不得启动浏览器或检查运行权限')
    request = replace(request, resume_run_id=saved['run_id'], export_only=True, runtime_readiness=forbidden)
    first = run_full_pipeline(request, website_runner=forbidden)
    payload = first.quote_path.read_bytes()
    second = run_full_pipeline(request, website_runner=forbidden)
    assert first.quote_path != second.quote_path
    assert first.quote_path.read_bytes() == payload
    book = load_workbook(second.quote_path)
    assert book.active['AK2'].value == 2199
    book.close()
    with SQLiteTaskRepository(request.database_path) as repo:
        assert repo.task_state(saved['tasks'][1].task_id) is TaskState.WAITING_FOR_LOGIN


@pytest.mark.parametrize('damage', ['input', 'evidence'])
def test_validation_blocks_resume_before_browser_and_preserves_saved_price(tmp_path, damage):
    request, saved = interrupted_run(tmp_path)
    target = request.paths.base if damage == 'input' else saved['result'].evidence.path
    target.write_bytes(b'changed')
    with pytest.raises(ValueError, match='输入|证据|截图'):
        run_full_pipeline(replace(request, resume_run_id=saved['run_id']),
                          website_runner=lambda _: pytest.fail('校验失败不得采集'))
    with SQLiteTaskRepository(request.database_path) as repo:
        assert repo.load_result(saved['tasks'][0].task_id) == saved['result']


def test_export_failure_is_retryable_without_collecting_again(tmp_path, monkeypatch):
    from quote_app.services import recovery
    request, saved = interrupted_run(tmp_path)
    request = replace(request, resume_run_id=saved['run_id'], export_only=True)
    original = recovery.write_web_results_to_excel
    def fail(*args):
        raise PermissionError('模拟导出被占用')
    monkeypatch.setattr(recovery, 'write_web_results_to_excel', fail)
    with pytest.raises(PermissionError):
        run_full_pipeline(request)
    with SQLiteTaskRepository(request.database_path) as repo:
        assert repo.load_result(saved['tasks'][0].task_id) == saved['result']
        assert repo.load_run(saved['run_id']).state is not RunState.COMPLETED
    monkeypatch.setattr(recovery, 'write_web_results_to_excel', original)
    assert run_full_pipeline(request).quote_path.exists()


def test_scope_saved_before_first_browser_and_stopped_export_retains_pending(tmp_path):
    request = FullPipelineRequest(
        paths=_input_paths(tmp_path), quote_month=QuoteMonth(2026, 8),
        browser_profile_dir=tmp_path / 'profile', database_path=tmp_path / 'tasks.db',
        evidence_dir=tmp_path / 'evidence', selected_channels=frozenset({WebsiteChannel.TMALL}),
    )
    saved = {}
    def crash(web):
        saved['run_id'] = web.run_id
        raise RuntimeError('before first browser')
    with pytest.raises(RuntimeError):
        run_full_pipeline(request, website_runner=crash)
    controller = WebsiteRunController()
    controller.request_shutdown()
    result = run_full_pipeline(replace(request, resume_run_id=saved['run_id'], controller=controller),
                              website_runner=lambda _: pytest.fail('已停止不得采集'))
    assert result.quote_path.exists()
    with SQLiteTaskRepository(request.database_path) as repo:
        tasks = repo.list_tasks(saved['run_id'])
        assert len(tasks) == 1 and tasks[0].channel is WebsiteChannel.TMALL
        assert repo.task_state(tasks[0].task_id) is TaskState.PENDING
        assert repo.load_run(saved['run_id']).state is RunState.STOPPED


def test_actual_process_exit_during_recovery_keeps_next_restart_possible(tmp_path):
    import subprocess
    import sys
    from tests.integration.test_checkpoint_recovery import _subprocess_environment
    request, saved = interrupted_run(tmp_path)
    script = '''
import os, sys
from pathlib import Path
from quote_app.domain.models import InputPaths, QuoteMonth
from quote_app.services.full_pipeline import FullPipelineRequest, run_full_pipeline
from quote_app.tasks.repository import SQLiteTaskRepository
p = Path(sys.argv[1])
r = FullPipelineRequest(InputPaths(p,p,p,p.parent), QuoteMonth(2026,8),
                        p.parent/'profile', p, p.parent/'evidence', resume_run_id=sys.argv[2])
def power_loss(web):
    with SQLiteTaskRepository(web.database_path) as repo:
        repo.start_attempt(web.tasks[0].task_id)
        os._exit(23)
run_full_pipeline(r, website_runner=power_loss)
'''
    for _ in range(2):
        child = subprocess.run([sys.executable, '-c', script, str(request.database_path), saved['run_id']],
                               env=_subprocess_environment(), capture_output=True, timeout=30)
        assert child.returncode == 23, child.stderr.decode()
    result = run_full_pipeline(replace(request, resume_run_id=saved['run_id'], export_only=True))
    assert result.quote_path.exists()
    with SQLiteTaskRepository(request.database_path) as repo:
        assert repo.load_result(saved['tasks'][0].task_id) == saved['result']
        assert len(repo.list_attempts(saved['tasks'][2].task_id)) == 3
        assert repo.task_state(saved['tasks'][1].task_id) is TaskState.PENDING


def test_export_with_missing_input_or_evidence_preserves_database_and_reports_problem(tmp_path):
    request, saved = interrupted_run(tmp_path)
    request.paths.base.unlink()
    saved['result'].evidence.path.unlink()
    output = run_full_pipeline(replace(request, resume_run_id=saved['run_id'], export_only=True))
    assert output.report_path.exists()
    assert any(i.code == 'RECOVERY_VALIDATION' for r in output.rows for i in r.issues)
    with SQLiteTaskRepository(request.database_path) as repo:
        assert repo.load_result(saved['tasks'][0].task_id) == saved['result']


def test_stop_during_recovery_keeps_saved_success_and_unstarted_work(tmp_path):
    request, saved = interrupted_run(tmp_path)
    controller = WebsiteRunController()
    def stop_after_one_step(web):
        web.controller.request_shutdown()
        return WebsiteRunSummary(0, 0, 0, ())
    result = run_full_pipeline(replace(request, resume_run_id=saved['run_id'], controller=controller),
                              website_runner=stop_after_one_step)
    with SQLiteTaskRepository(request.database_path) as repo:
        assert repo.load_run(saved['run_id']).state is RunState.STOPPED
        assert repo.task_state(saved['tasks'][1].task_id) is TaskState.PENDING
        assert repo.load_result(saved['tasks'][0].task_id) == saved['result']
    assert result.quote_path.exists()


def test_paused_task_and_original_scope_are_recovered(tmp_path):
    request, saved = interrupted_run(tmp_path)
    task = saved['tasks'][1]
    with SQLiteTaskRepository(request.database_path) as repo:
        repo.pause_task(task.task_id)
    seen = []
    def resume(web):
        seen.extend(web.tasks)
        with SQLiteTaskRepository(web.database_path) as repo:
            assert repo.task_state(task.task_id) is TaskState.PENDING
        return WebsiteRunSummary(0, 0, 0, ())
    run_full_pipeline(replace(request, resume_run_id=saved['run_id'], selected_channels=frozenset({WebsiteChannel.JD})),
                      website_runner=resume)
    assert seen == [task]


def test_legacy_batch_does_not_invent_unregistered_channel_scope(tmp_path):
    from quote_app.services.recovery import inspect_recovery
    request, saved = interrupted_run(tmp_path)
    with sqlite3.connect(request.database_path) as db:
        db.execute('DELETE FROM quotation_recovery_context')
    plan = inspect_recovery(request.database_path, saved['run_id'])
    assert '旧版批次' in plan.summary
    called = []
    def resume(web):
        called.extend(web.tasks)
        return WebsiteRunSummary(0, 0, 0, ())
    run_full_pipeline(replace(request, resume_run_id=saved['run_id']), website_runner=resume)
    assert called == [saved['tasks'][1]]
