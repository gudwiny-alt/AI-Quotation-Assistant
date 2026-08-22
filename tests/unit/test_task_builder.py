from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from quote_app.domain.models import QuoteMonth, QuoteRow, WebQuery
from quote_app.tasks.builder import (
    SUPPORTED_BRANDS,
    build_website_tasks,
    create_run_record,
)
from quote_app.tasks.models import SCHEMA_VERSION, WebsiteChannel
from quote_app.tasks.serialization import PayloadError


@pytest.fixture
def input_paths(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "base": tmp_path / "1.基础表.xlsx",
        "marketing": tmp_path / "2.营销商品信息查询.xlsx",
        "bop": tmp_path / "3.BOP资源信息表.xlsx",
    }
    for role, path in paths.items():
        path.write_bytes(f"{role}-workbook-v1".encode())
    return paths


def _eligible_row(
    *,
    source_row_number: int = 7,
    material_code: str = "000123",
    brand: str = "小米",
) -> QuoteRow:
    return QuoteRow(
        source_row_number=source_row_number,
        material_code=material_code,
        cells={
            "B": "展示品牌不可用于查询",
            "E": "展示型号不可用于查询",
            "AQ": "展示内存不可用于查询",
            "AR": "展示容量不可用于查询",
            "AS": "展示颜色不可用于查询",
        },
        web_query=WebQuery(
            brand=brand,
            model_name="Xiaomi 17 Pro",
            ram="12GB",
            storage="256GB",
            color="雪山粉",
        ),
    )


def _run(input_paths: dict[str, Path], rows: list[QuoteRow]):
    return create_run_record(
        QuoteMonth(2026, 8),
        input_paths["base"],
        input_paths["marketing"],
        input_paths["bop"],
        input_paths["base"].parent / "output",
        input_paths["base"].parent / "browser-profile",
        rows,
    )


def test_create_run_fingerprints_exact_roles_and_protects_ordered_snapshot(
    input_paths: dict[str, Path],
) -> None:
    rows = [
        _eligible_row(source_row_number=9, material_code="000002"),
        _eligible_row(source_row_number=3, material_code="000001"),
    ]

    run = _run(input_paths, rows)

    assert UUID(run.run_id).version == 4
    assert run.schema_version == SCHEMA_VERSION
    assert run.quote_month == QuoteMonth(2026, 8)
    assert tuple(item.source_role for item in run.input_fingerprints) == (
        "base",
        "marketing",
        "bop",
    )
    for fingerprint in run.input_fingerprints:
        expected_path = input_paths[fingerprint.source_role]
        assert fingerprint.path == expected_path.resolve()
        assert fingerprint.sha256 == hashlib.sha256(expected_path.read_bytes()).hexdigest()
        assert fingerprint.byte_size == expected_path.stat().st_size
        assert fingerprint.modified_ns == expected_path.stat().st_mtime_ns

    assert run.associated_rows_snapshot is not None
    snapshot = json.loads(run.associated_rows_snapshot)
    assert snapshot["schema_version"] == SCHEMA_VERSION
    assert snapshot["snapshot_type"] == "quote_rows"
    assert [row["material_code"] for row in snapshot["rows"]] == ["000002", "000001"]
    assert (
        hashlib.sha256(run.associated_rows_snapshot.encode("utf-8")).hexdigest()
        == run.associated_rows_snapshot_sha256
    )


def test_new_run_has_a_new_identity_and_input_byte_change_changes_fingerprint(
    input_paths: dict[str, Path],
) -> None:
    rows = [_eligible_row()]
    first = _run(input_paths, rows)
    input_paths["marketing"].write_bytes(b"marketing-workbook-v2")
    second = _run(input_paths, rows)

    assert first.run_id != second.run_id
    assert first.input_fingerprints[1].path == second.input_fingerprints[1].path
    assert first.input_fingerprints[1].sha256 != second.input_fingerprints[1].sha256


def test_create_run_snapshot_obeys_credential_rejection_rules(
    input_paths: dict[str, Path],
) -> None:
    row = _eligible_row()
    row.cells["A"] = "password=secret"

    with pytest.raises(PayloadError, match="credential fields are forbidden"):
        _run(input_paths, [row])


def test_create_run_rejects_credentials_inside_web_query(
    input_paths: dict[str, Path],
) -> None:
    row = _eligible_row()
    row.web_query = WebQuery(
        brand="小米",
        model_name="token: secret",
        ram="12GB",
        storage="256GB",
        color="雪山粉",
    )

    with pytest.raises(PayloadError, match="credential fields are forbidden"):
        _run(input_paths, [row])


@pytest.mark.parametrize("mutation_timing", ["after_data", "at_eof"])
def test_create_run_rejects_input_changed_while_fingerprinting(
    input_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    mutation_timing: str,
) -> None:
    target = input_paths["base"]
    real_open = Path.open

    class MutatingReader:
        def __init__(self, wrapped: Any) -> None:
            self._wrapped = wrapped
            self._mutated = False

        def __enter__(self) -> MutatingReader:
            self._wrapped.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._wrapped.__exit__(*args)

        def fileno(self) -> int:
            return self._wrapped.fileno()

        def read(self, size: int = -1) -> bytes:
            data = self._wrapped.read(size)
            should_mutate = (
                mutation_timing == "after_data"
                and bool(data)
                or mutation_timing == "at_eof"
                and not data
            )
            if should_mutate and not self._mutated:
                self._mutated = True
                descriptor = os.open(target, os.O_WRONLY | os.O_TRUNC)
                try:
                    os.write(descriptor, b"base-workbook-edited-during-fingerprint")
                finally:
                    os.close(descriptor)
            return data

    def wrapped_open(path: Path, *args: object, **kwargs: object) -> Any:
        opened = real_open(path, *args, **kwargs)
        if path == target and args and args[0] == "rb":
            return MutatingReader(opened)
        return opened

    monkeypatch.setattr(Path, "open", wrapped_open)

    with pytest.raises(
        ValueError,
        match="输入文件在读取过程中发生变化，请停止编辑后重试",
    ):
        _run(input_paths, [_eligible_row()])


def test_eligible_row_builds_three_channels_from_web_query_only(
    input_paths: dict[str, Path],
) -> None:
    rows = [_eligible_row()]
    run = _run(input_paths, rows)

    result = build_website_tasks(run, rows)

    assert result.issues == ()
    assert tuple(task.channel for task in result.tasks) == (
        WebsiteChannel.OFFICIAL,
        WebsiteChannel.JD,
        WebsiteChannel.TMALL,
    )
    assert len(result.tasks) == 3
    for task in result.tasks:
        assert task.output_row_number == 2
        assert task.source_row_number == 7
        assert task.material_code == "000123"
        assert task.brand == "小米"
        assert task.model_name == "Xiaomi 17 Pro"
        assert task.ram == "12GB"
        assert task.storage == "256GB"
        assert task.color == "雪山粉"


def test_ai_marker_in_associated_product_name_requires_ai_package_for_each_channel(
    input_paths: dict[str, Path],
) -> None:
    row = _eligible_row(brand="荣耀")
    row.cells["D"] = "HONOR_NLA-AN00_荣耀畅玩80_6GB+128GB_碧空蓝_AI定制合作型_标准版"
    run = _run(input_paths, [row])

    result = build_website_tasks(run, [row])

    assert result.issues == ()
    assert all(task.requires_ai_package for task in result.tasks)


def test_sparse_source_row_does_not_shift_output_row(input_paths: dict[str, Path]) -> None:
    rows = [_eligible_row(source_row_number=27)]
    run = _run(input_paths, rows)

    result = build_website_tasks(run, rows)

    assert {task.output_row_number for task in result.tasks} == {2}
    assert {task.source_row_number for task in result.tasks} == {27}


def test_sparse_source_rows_still_receive_contiguous_output_rows(
    input_paths: dict[str, Path],
) -> None:
    rows = [
        _eligible_row(source_row_number=2, material_code="000001"),
        _eligible_row(source_row_number=19, material_code="000002"),
        _eligible_row(source_row_number=41, material_code="000003"),
    ]
    run = _run(input_paths, rows)

    result = build_website_tasks(run, rows)

    assert [
        (task.source_row_number, task.output_row_number)
        for task in result.tasks
        if task.channel is WebsiteChannel.JD
    ] == [(2, 2), (19, 3), (41, 4)]


def test_multiple_rows_are_built_channel_major_in_input_order(
    input_paths: dict[str, Path],
) -> None:
    rows = [
        _eligible_row(source_row_number=2, material_code="000001"),
        _eligible_row(source_row_number=19, material_code="000002"),
    ]
    run = _run(input_paths, rows)

    result = build_website_tasks(run, rows)

    assert [
        (task.channel, task.output_row_number)
        for task in result.tasks
    ] == [
        (WebsiteChannel.OFFICIAL, 2),
        (WebsiteChannel.OFFICIAL, 3),
        (WebsiteChannel.JD, 2),
        (WebsiteChannel.JD, 3),
        (WebsiteChannel.TMALL, 2),
        (WebsiteChannel.TMALL, 3),
    ]


def test_duplicate_material_codes_remain_independent_and_task_ids_are_stable(
    input_paths: dict[str, Path],
) -> None:
    rows = [
        _eligible_row(source_row_number=7, material_code="000123"),
        _eligible_row(source_row_number=12, material_code="000123"),
    ]
    run = _run(input_paths, rows)

    first = build_website_tasks(run, rows)
    second = build_website_tasks(run, rows)

    assert len(first.tasks) == 6
    assert [task.task_id for task in first.tasks] == [
        task.task_id for task in second.tasks
    ]
    assert {task.output_row_number for task in first.tasks} == {2, 3}
    assert len({task.task_id for task in first.tasks}) == 6


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("brand", None),
        ("model_name", ""),
        ("ram", "　"),
        ("storage", None),
        ("color", "  "),
    ],
)
def test_missing_query_field_returns_machine_readable_issue_without_tasks(
    input_paths: dict[str, Path], field: str, value: str | None
) -> None:
    row = _eligible_row()
    values = {
        "brand": row.web_query.brand,
        "model_name": row.web_query.model_name,
        "ram": row.web_query.ram,
        "storage": row.web_query.storage,
        "color": row.web_query.color,
    }
    values[field] = value
    row.web_query = WebQuery(**values)
    run = _run(input_paths, [row])

    result = build_website_tasks(run, [row])

    assert result.tasks == ()
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.code == "WEB_FIELDS_MISSING"
    assert issue.source_row_number == 7
    assert issue.output_row_number == 2
    assert issue.material_code == "000123"
    assert issue.fields == (field,)
    assert "缺少网站查询字段" in issue.message


def test_unsupported_nonblank_brand_returns_issue_without_tasks(
    input_paths: dict[str, Path],
) -> None:
    row = _eligible_row(brand="不支持品牌")
    run = _run(input_paths, [row])

    result = build_website_tasks(run, [row])

    assert result.tasks == ()
    assert len(result.issues) == 1
    assert result.issues[0].code == "UNSUPPORTED_BRAND"
    assert result.issues[0].fields == ("brand",)
    assert "品牌暂不支持自动查价" in result.issues[0].message
    assert "不支持品牌" in result.issues[0].message


def test_all_seven_normalized_brands_are_supported(input_paths: dict[str, Path]) -> None:
    rows = [
        _eligible_row(
            source_row_number=index + 2,
            material_code=f"{index:06d}",
            brand=brand,
        )
        for index, brand in enumerate(sorted(SUPPORTED_BRANDS), start=1)
    ]
    run = _run(input_paths, rows)

    result = build_website_tasks(run, rows)

    assert result.issues == ()
    assert len(result.tasks) == len(SUPPORTED_BRANDS) * 3


def test_task_identity_changes_across_runs(input_paths: dict[str, Path]) -> None:
    rows = [_eligible_row()]
    first_run = _run(input_paths, rows)
    second_run = _run(input_paths, rows)

    first_ids = {
        task.task_id for task in build_website_tasks(first_run, rows).tasks
    }
    second_ids = {
        task.task_id for task in build_website_tasks(second_run, rows).tasks
    }

    assert first_ids.isdisjoint(second_ids)


def test_builder_rejects_rows_changed_after_run_snapshot(
    input_paths: dict[str, Path],
) -> None:
    rows = [_eligible_row()]
    run = _run(input_paths, rows)
    rows[0].web_query = WebQuery(
        brand="小米",
        model_name="被修改的型号",
        ram="12GB",
        storage="256GB",
        color="雪山粉",
    )

    with pytest.raises(ValueError, match="报价行与运行记录快照不一致"):
        build_website_tasks(run, rows)


def test_builder_rejects_tampered_stored_snapshot_hash(
    input_paths: dict[str, Path],
) -> None:
    rows = [_eligible_row()]
    run = replace(
        _run(input_paths, rows),
        associated_rows_snapshot_sha256="f" * 64,
    )

    with pytest.raises(ValueError, match="运行记录的报价行快照校验失败"):
        build_website_tasks(run, rows)


def test_builder_accepts_verified_snapshot_path_mode(
    input_paths: dict[str, Path],
) -> None:
    rows = [_eligible_row()]
    inline_run = _run(input_paths, rows)
    assert inline_run.associated_rows_snapshot is not None
    snapshot_path = input_paths["base"].parent / "rows.snapshot.json"
    snapshot_path.write_text(
        inline_run.associated_rows_snapshot,
        encoding="utf-8",
    )
    path_run = replace(
        inline_run,
        associated_rows_snapshot=None,
        associated_rows_snapshot_path=snapshot_path,
    )

    result = build_website_tasks(path_run, rows)

    assert len(result.tasks) == 3
    assert result.issues == ()
