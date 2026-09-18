import os
import pytest
from tests.ui.test_desktop_workbench import workbench as base_workbench
from tests.integration.test_restart_recovery import interrupted_run

pytestmark = pytest.mark.skipif(os.environ.get('QUOTE_NATIVE_UI_TESTS') != '1', reason='Requires WindowServer')

@pytest.fixture
def workbench(tmp_path):
    yield from base_workbench.__wrapped__(tmp_path)


def test_history_recovery_and_export_target_selected_batch(workbench, tmp_path):
    from dataclasses import replace
    request, saved = interrupted_run(tmp_path)
    view = workbench
    view.app.app_paths = replace(view.app.app_paths, task_database=request.database_path)
    calls = []
    view.app.recover_run = lambda run_id, **kwargs: calls.append((run_id, kwargs))
    view.show_page('history')
    assert view.history_table.selection() == (saved['run_id'],)
    view.history_resume_button.invoke()
    view.history_export_button.invoke()
    assert calls == [(saved['run_id'], {'export_only': False}), (saved['run_id'], {'export_only': True})]
    view.model.running = True
    view._history_selection()
    assert str(view.history_resume_button['state']) == 'disabled'
    assert str(view.history_export_button['state']) == 'disabled'


def test_resume_keeps_finished_rows_visible_and_enables_stop(workbench, tmp_path):
    from dataclasses import replace
    from quote_app.tasks.repository import SQLiteTaskRepository
    request, saved = interrupted_run(tmp_path)
    view = workbench
    view.app.app_paths = replace(view.app.app_paths, task_database=request.database_path)
    with SQLiteTaskRepository(request.database_path, recover_interrupted=False) as repo:
        run = repo.load_run(saved['run_id'])
    view.begin_recovery(run)
    assert view.model.run_id == saved['run_id']
    assert len(view.model.rows) == 3
    assert view.model.rows[0].price == '2199'
    assert view.model.rows[0].state == 'succeeded'
    assert str(view.app.cancel_button['state']) == 'normal'
    assert view.app.cancel_button.cget('text') == '停止并导出'
