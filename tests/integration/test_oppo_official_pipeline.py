from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook

from quote_app.domain.models import InputPaths, QuoteMonth
from quote_app.services.full_pipeline import FullPipelineRequest, run_full_pipeline
from quote_app.services.web_run import WebsiteRunRequest, WebsiteRunSummary
from quote_app.tasks.models import WebsiteChannel
from tests.contract.test_official_oppo_live import (
    _OppoFixturePage,
    _set_detail_product,
    _set_result_cards,
)
from tests.factories.workbook_factory import save_workbook
from tests.integration.test_xiaomi_official_pipeline import (
    _FormalCapture,
    _image_anchors,
    _row,
    _run_real_runner,
    _headers,
)


OPPO_ROWS = (
    ("OPPO-A5M", "OPPO A5m 5G", "8GB", "256GB", "钻石白"),
    ("OPPO-A6T", "OPPO A6t", "6GB", "128GB", "墨竹黑"),
)


def _oppo_inputs(tmp_path: Path, *, rows: int = 1) -> InputPaths:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    materials = tuple(f"OPPO-{index}" for index in range(1, rows + 1))
    base = save_workbook(
        inputs / "base.xlsx",
        _headers(
            13,
            A="集团一级库物料编码",
            C="2026年3月结算报价（元/台）",
            D="2026年7月结算报价（元/台）",
        ),
        [
            _row(13, A=material, B=f"经理{index}", C=2199, D=1999)
            for index, material in enumerate(materials, start=1)
        ],
    )
    marketing = save_workbook(
        inputs / "marketing.xlsx",
        _headers(45, I="物料编码"),
        [
            _row(
                45,
                B="智能手机",
                C="欧珀",
                E="OPPO A6 5G",
                I=material,
                M="5G手机",
                V="2026-01-01",
                X=2199,
                AQ="12GB",
                AR="256GB",
                AS="蓝海浮光",
            )
            for material in materials
        ],
    )
    bop = save_workbook(
        inputs / "bop.xlsx",
        _headers(26, K="集团一级库编码"),
        [_row(26, K=material, Y="12+256", Z="已配置") for material in materials],
    )
    return InputPaths(base, marketing, bop, tmp_path / "outputs")


def _oppo_model_only_inputs(tmp_path: Path) -> InputPaths:
    inputs = tmp_path / "model-only-inputs"
    inputs.mkdir()
    base = save_workbook(
        inputs / "base.xlsx",
        _headers(
            13,
            A="集团一级库物料编码",
            C="2026年3月结算报价（元/台）",
            D="2026年7月结算报价（元/台）",
        ),
        [
            _row(13, A=material, B=f"经理{index}", C=2199, D=1999)
            for index, (material, _model, _ram, _storage, _color) in enumerate(
                OPPO_ROWS, start=1
            )
        ],
    )
    marketing = save_workbook(
        inputs / "marketing.xlsx",
        _headers(45, I="物料编码"),
        [
            _row(
                45,
                B="智能手机",
                C="欧珀",
                E=model,
                I=material,
                M="5G手机",
                V="2026-01-01",
                X=2199,
                AQ=ram,
                AR=storage,
                AS=color,
            )
            for material, model, ram, storage, color in OPPO_ROWS
        ],
    )
    bop = save_workbook(
        inputs / "bop.xlsx",
        _headers(26, K="集团一级库编码"),
        [
            _row(
                26,
                K=material,
                Y=f"{ram.removesuffix('GB')}+{storage.removesuffix('GB')}",
                Z="已配置",
            )
            for material, _model, ram, storage, _color in OPPO_ROWS
        ],
    )
    return InputPaths(base, marketing, bop, tmp_path / "outputs")


class _OppoScenarioPage(_OppoFixturePage):
    def __init__(self) -> None:
        super().__init__("normal.html")
        self.search_keywords: list[str] = []

    def activate_results(self) -> None:
        keyword = self.locator('[data-oppo-role="search-input"]').input_value()
        self.search_keywords.append(keyword)
        super().activate_results()


class _OppoModelOnlyScenarioPage(_OppoScenarioPage):
    def activate_results(self) -> None:
        keyword = self.locator('[data-oppo-role="search-input"]').input_value()
        if keyword == "OPPO A5m 5G":
            _set_result_cards(
                self,
                (("OPPO A5m 水晶粉 6GB+128GB", "/cn/web/products/38672.html?us=search"),),
            )
            _set_detail_product(
                self,
                model_name="OPPO A5m",
                capacity="8GB+256GB",
                color="钻石白",
            )
        elif keyword == "OPPO A6t":
            _set_result_cards(
                self,
                (("OPPO A6t 青出于蓝 6GB+128GB", "/cn/web/products/41956.html?us=search"),),
            )
            _set_detail_product(
                self,
                model_name="OPPO A6t",
                capacity="6GB+128GB",
                color="墨竹黑",
            )
        super().activate_results()


class _OppoSession:
    def __init__(self, page: _OppoScenarioPage | None = None) -> None:
        self.page = page or _OppoScenarioPage()

    def page_for(self, _site_family: str) -> _OppoScenarioPage:
        return self.page


def _full_request(paths: InputPaths, tmp_path: Path) -> FullPipelineRequest:
    return FullPipelineRequest(
        paths=paths,
        quote_month=QuoteMonth(2026, 8),
        browser_profile_dir=tmp_path / "profile",
        database_path=tmp_path / "tasks.sqlite3",
        evidence_dir=tmp_path / "evidence",
        selected_brand="欧珀",
        selected_channels=frozenset({WebsiteChannel.OFFICIAL}),
    )


def test_oppo_price_and_capture_reach_excel_ak_and_an(tmp_path: Path) -> None:
    capture = _FormalCapture()

    def website_runner(request: WebsiteRunRequest) -> WebsiteRunSummary:
        return _run_real_runner(
            request,
            capture=capture,
            session=_OppoSession(),  # type: ignore[arg-type]
        )

    result = run_full_pipeline(
        _full_request(_oppo_inputs(tmp_path), tmp_path),
        website_runner=website_runner,
    )

    quote = load_workbook(result.quote_path, data_only=False)
    try:
        sheet = quote["5G手机"]
        assert sheet["AK2"].value == 1899
        assert _image_anchors(sheet) == {"AN2"}
    finally:
        quote.close()
    assert len(capture.requests) == 1


def test_oppo_capture_failure_keeps_price_in_excel(tmp_path: Path) -> None:
    result = run_full_pipeline(
        _full_request(_oppo_inputs(tmp_path), tmp_path),
        website_runner=lambda request: _run_real_runner(
            request,
            capture=_FormalCapture(fail=True),
            session=_OppoSession(),  # type: ignore[arg-type]
        ),
    )

    quote = load_workbook(result.quote_path, data_only=False)
    try:
        sheet = quote["5G手机"]
        assert sheet["AK2"].value == 1899
        assert "AN2" not in _image_anchors(sheet)
    finally:
        quote.close()


def test_oppo_two_rows_keep_base_order_and_complete_once_each(tmp_path: Path) -> None:
    page = _OppoScenarioPage()
    result = run_full_pipeline(
        _full_request(_oppo_inputs(tmp_path, rows=2), tmp_path),
        website_runner=lambda request: _run_real_runner(
            request,
            capture=_FormalCapture(),
            session=_OppoSession(page),  # type: ignore[arg-type]
        ),
    )

    assert [row.material_code for row in result.rows] == ["OPPO-1", "OPPO-2"]
    assert page.search_keywords == ["OPPO A6 5G", "OPPO A6 5G"]
    assert result.summary.total_rows == 2
    assert result.summary.completed_rows == 2
    assert result.summary.failed_rows == 0
    quote = load_workbook(result.quote_path, data_only=False)
    try:
        sheet = quote["5G手机"]
        assert [sheet["AK2"].value, sheet["AK3"].value] == [1899, 1899]
        assert _image_anchors(sheet) == {"AN2", "AN3"}
    finally:
        quote.close()


def test_oppo_a5m_a6t_keep_input_order_and_write_ak_an(tmp_path: Path) -> None:
    page = _OppoModelOnlyScenarioPage()
    session = _OppoSession(page)
    result = run_full_pipeline(
        _full_request(_oppo_model_only_inputs(tmp_path), tmp_path),
        website_runner=lambda request: _run_real_runner(
            request,
            capture=_FormalCapture(),
            session=session,  # type: ignore[arg-type]
        ),
    )

    assert [row.material_code for row in result.rows] == ["OPPO-A5M", "OPPO-A6T"]
    assert page.search_keywords == ["OPPO A5m 5G", "OPPO A6t"]
    quote = load_workbook(result.quote_path, data_only=False)
    try:
        sheet = quote["5G手机"]
        assert [sheet["AK2"].value, sheet["AK3"].value] == [1899, 1899]
        assert _image_anchors(sheet) == {"AN2", "AN3"}
    finally:
        quote.close()
