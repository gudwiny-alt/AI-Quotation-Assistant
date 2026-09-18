"""Resume an original durable run without resetting results or retry budgets."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from quote_app.domain.models import InputPaths, Issue, QuoteRow, WebQuery
from quote_app.evidence.models import MacCapturePolicy
from quote_app.evidence.validation import read_validated_evidence
from quote_app.services.web_run import WebsiteRunRequest, WebsiteRunSnapshot, _summarize
from quote_app.services.web_to_excel import WebToExcelRequest, write_web_results_to_excel
from quote_app.tasks.builder import build_website_tasks
from quote_app.tasks.models import RunRecord, RunState, TaskState, WebsiteTask
from quote_app.tasks.repository import SQLiteTaskRepository
from quote_app.tasks.retry import RETRYABLE_ERROR_CODES, RetryPolicy, consumes_technical_budget


@dataclass(frozen=True)
class RecoveryPlan:
    run: RunRecord
    rows: tuple[QuoteRow, ...]
    tasks: tuple[WebsiteTask, ...]
    context: dict | None
    pending: int
    succeeded: int
    failed: int
    input_issues: tuple[str, ...]
    evidence_issues: tuple[str, ...]

    @property
    def paths(self) -> InputPaths:
        sources = {item.source_role: item.path for item in self.run.input_fingerprints}
        return InputPaths(sources['base'], sources['marketing'], sources['bop'], self.run.output_dir)

    @property
    def summary(self) -> str:
        text = (f"原批次：{self.run.run_id}\n"
                f"已完成 {self.succeeded} 项 · 可恢复 {self.pending} 项 · 技术失败 {self.failed} 项\n"
                "已完成的价格和截图保留；耗尽重试次数及不可重试的失败项不会重跑。")
        if self.context is None:
            text += "\n旧版批次未保存完整执行范围，只恢复已登记任务，不推测或扩展原范围。"
        if self.input_issues or self.evidence_issues:
            text += "\n校验提示：\n" + '\n'.join((*self.input_issues, *self.evidence_issues))
        return text


def _snapshot_rows(run: RunRecord) -> tuple[QuoteRow, ...]:
    raw = (run.associated_rows_snapshot.encode('utf-8')
           if run.associated_rows_snapshot is not None
           else run.associated_rows_snapshot_path.read_bytes())
    if hashlib.sha256(raw).hexdigest() != run.associated_rows_snapshot_sha256:
        raise ValueError('原批次报价快照校验失败，无法恢复或导出')
    data = json.loads(raw)
    if data.get('schema_version') != 1 or data.get('snapshot_type') != 'quote_rows':
        raise ValueError('原批次报价快照格式不受支持')
    parsers = {'decimal': Decimal, 'datetime': datetime.fromisoformat,
               'date': date.fromisoformat, 'time': time.fromisoformat}
    def value(item):
        if not isinstance(item, dict):
            return item
        return parsers[item['value_type']](item['value'])
    rows = tuple(QuoteRow(
        source_row_number=item['source_row_number'], material_code=item['material_code'],
        cells={column: value(cell) for column, cell in item['cells'].items()},
        issues=[Issue(**issue) for issue in item['issues']], web_query=WebQuery(**item['web_query']),
    ) for item in data['rows'])
    # Validate typed reconstruction against the exact original snapshot as well.
    build_website_tasks(run, rows)
    return rows


def _can_resume(repository: SQLiteTaskRepository, task: WebsiteTask) -> bool:
    state = repository.task_state(task.task_id)
    if state is TaskState.SUCCEEDED:
        return False
    attempts = repository.list_attempts(task.task_id)
    generation = repository.task_generation(task.task_id)
    budget = sum(consumes_technical_budget(attempt.error_code) for attempt in attempts
                 if attempt.generation == generation)
    if not RetryPolicy().can_start_technical_attempt(budget):
        return False
    if state is TaskState.TECHNICAL_FAILURE:
        result = repository.load_result(task.task_id)
        return result is not None and result.error_code in RETRYABLE_ERROR_CODES
    return True


def inspect_recovery(database_path: Path, run_id: str,
                     *, fallback_policy=MacCapturePolicy.STRICT) -> RecoveryPlan:
    if not database_path.is_file():
        raise ValueError('尚无已保存任务')
    # Inspection must not recover RUNNING or invalidate successful results.
    with SQLiteTaskRepository(database_path, recover_interrupted=False) as repo:
        run = repo.load_run(run_id)
        rows = _snapshot_rows(run)
        context = repo.recovery_context(run_id)
        tasks = repo.list_tasks(run_id)
        expected = {t.task_id: t for t in build_website_tasks(run, rows).tasks}
        if any(expected.get(task.task_id) != task for task in tasks):
            raise ValueError('原批次任务与商品快照不一致，不能恢复')
        if context is not None and (
            context.get('version') != 1 or set(context['task_ids']) != {t.task_id for t in tasks}
        ):
            raise ValueError('原批次执行范围不完整，不能恢复')
        input_issues = []
        labels = {'base': '基础报价表', 'marketing': '营销商品表', 'bop': '资源表'}
        for fingerprint in run.input_fingerprints:
            try:
                with fingerprint.path.open('rb') as stream:
                    valid = hashlib.file_digest(stream, 'sha256').hexdigest() == fingerprint.sha256
            except OSError:
                valid = False
            if not valid:
                input_issues.append(f"输入文件缺失或已变化：{labels.get(fingerprint.source_role, fingerprint.source_role)}（{fingerprint.path}）")
        policy = MacCapturePolicy(context['capture_acceptance_policy']) if context else fallback_policy
        evidence_issues = []
        for task in tasks:
            result = repo.load_result(task.task_id)
            if result and result.evidence:
                audit = read_validated_evidence(result.evidence, capture_acceptance_policy=policy)
                if not audit.is_valid:
                    evidence_issues.append(f'截图证据缺失或校验失败：{task.model_name} / {task.channel.value}')
        return RecoveryPlan(
            run, rows, tasks, context,
            sum(_can_resume(repo, t) for t in tasks),
            sum(repo.task_state(t.task_id) is TaskState.SUCCEEDED for t in tasks),
            sum(repo.task_state(t.task_id) is TaskState.TECHNICAL_FAILURE for t in tasks),
            tuple(input_issues), tuple(evidence_issues),
        )


def finish_run_status(database_path, run_id, controller, quote_path, report_path):
    with SQLiteTaskRepository(database_path, recover_interrupted=False) as repo:
        states = [repo.task_state(task.task_id) for task in repo.list_tasks(run_id)]
        if controller is not None and controller.stopped:
            state = RunState.STOPPED
        elif any(state in {TaskState.PENDING, TaskState.RUNNING, TaskState.WAITING_FOR_LOGIN,
                           TaskState.PAUSED} for state in states):
            state = RunState.PAUSED
        elif TaskState.TECHNICAL_FAILURE in states:
            state = RunState.FAILED
        else:
            state = RunState.COMPLETED
        repo.update_run_status(run_id, state, quote_path=quote_path, report_path=report_path)


def recover_full_pipeline(request, *, website_runner):
    from quote_app.services.full_pipeline import (
        FullPipelineResult, RuntimeReadinessError, _apply_task_build_issues, _ui_event_sink_for_tasks,
    )
    from quote_app.services.incremental_publication import IncrementalExcelPublisher, CoalescingPublicationWorker
    plan = inspect_recovery(request.database_path, request.resume_run_id,
                            fallback_policy=request.capture_acceptance_policy)
    if not request.export_only and (plan.input_issues or plan.evidence_issues):
        raise ValueError('恢复前校验未通过：' + '；'.join((*plan.input_issues, *plan.evidence_issues))
                         + '。请还原原文件，或选择“仅导出已有成果”。')
    if not request.export_only and request.runtime_readiness is not None and plan.pending:
        readiness = request.runtime_readiness()
        if not readiness.ready:
            raise RuntimeReadinessError(readiness)
    context = plan.context or {}
    policy = MacCapturePolicy(context.get('capture_acceptance_policy', request.capture_acceptance_policy.value))
    template = Path(context.get('template_path', request.template_path))
    if not template.is_file():
        # The app may have been moved or updated since the original run.
        template = request.template_path
    rows = list(plan.rows)
    _apply_task_build_issues(rows, build_website_tasks(plan.run, rows).issues)
    for message in (*plan.input_issues, *plan.evidence_issues):
        for row in rows:
            row.issues.append(Issue('RECOVERY_VALIDATION', message, False, row.source_row_number))
    # Every recovery/export has an independent folder; original files are never overwritten.
    output_dir = plan.run.output_dir / f"恢复成果-{datetime.now():%Y%m%d-%H%M%S-%f}-{uuid4().hex[:6]}"
    output_dir.mkdir(parents=True, exist_ok=False)
    publisher = IncrementalExcelPublisher(
        quote_month=plan.run.quote_month, rows=tuple(rows), tasks=plan.tasks,
        output_dir=output_dir, template_path=template, input_paths=plan.paths,
        capture_acceptance_policy=policy,
    )
    def snapshot():
        with SQLiteTaskRepository(request.database_path, recover_interrupted=False) as repo:
            return WebsiteRunSnapshot(
                tuple(o for t in plan.tasks if (o := repo.load_observation(t.task_id)) is not None),
                tuple(r for t in plan.tasks if (r := repo.load_result(t.task_id)) is not None),
                frozenset(t.task_id for t in plan.tasks
                          if repo.task_state(t.task_id) is TaskState.WAITING_FOR_LOGIN),
            )
    worker = None
    try:
        if not request.export_only:
            with SQLiteTaskRepository(request.database_path) as repo:
                eligible = tuple(t for t in plan.tasks if _can_resume(repo, t))
                for task in eligible:
                    state = repo.task_state(task.task_id)
                    if state is TaskState.PAUSED:
                        repo.resume_task(task.task_id)
                    elif state is TaskState.WAITING_FOR_LOGIN:
                        # Reopen its own channel page; the website will request verification again if needed.
                        repo.requeue_exact_waiting_task(plan.run.run_id, task, repo.waiting_site(task.task_id),
                                                       expected_token=repo.waiting_attempt_token(task.task_id))
                repo.update_run_status(plan.run.run_id, RunState.RUNNING)
            publisher.publish(snapshot())
            worker = CoalescingPublicationWorker(publisher, task_count=len(plan.tasks))
            def checkpoint(_partial):
                # Called while the runner owns the database lock. Its request contains all
                # tasks for projection, including retained successes and failed tasks.
                worker.submit(_partial)
                if _partial.waiting_task_ids:
                    worker.flush()
            if eligible and not (request.controller and request.controller.stopped):
                website_runner(WebsiteRunRequest(
                    run_id=plan.run.run_id, tasks=eligible,
                    profile_dir=plan.run.browser_profile_dir,
                    evidence_dir=Path(context.get('evidence_dir', request.evidence_dir)),
                    database_path=request.database_path, controller=request.controller,
                    event_sink=_ui_event_sink_for_tasks(plan.tasks, request.event_sink),
                    checkpoint_sink=checkpoint, snapshot_tasks=plan.tasks,
                ))
            worker.submit(snapshot())
            worker.close()
            worker = None
        final = snapshot()
        output = write_web_results_to_excel(WebToExcelRequest(
            quote_month=plan.run.quote_month, rows=tuple(rows), tasks=plan.tasks,
            results=final.results, observations=final.observations,
            waiting_task_ids=final.waiting_task_ids, output_dir=output_dir,
            template_path=template, input_paths=plan.paths, capture_acceptance_policy=policy,
        ))
        finish_run_status(request.database_path, plan.run.run_id, request.controller,
                          output.quote_path, output.report_path)
        with SQLiteTaskRepository(request.database_path, recover_interrupted=False) as repo:
            summary = _summarize(WebsiteRunRequest(
                run_id=plan.run.run_id, tasks=plan.tasks, profile_dir=plan.run.browser_profile_dir,
                evidence_dir=request.evidence_dir, database_path=request.database_path,
            ), repo, final.results, capture_acceptance_policy=policy)
        publisher.discard_checkpoints_after_final_output(
            final_quote_path=output.quote_path, final_report_path=output.report_path)
        return FullPipelineResult(output.quote_path, output.report_path, output.summary, output.rows, summary)
    except BaseException:
        if worker is not None:
            worker.close(suppress_error=True)
        with SQLiteTaskRepository(request.database_path, recover_interrupted=False) as repo:
            repo.update_run_status(plan.run.run_id, RunState.FAILED)
        raise
