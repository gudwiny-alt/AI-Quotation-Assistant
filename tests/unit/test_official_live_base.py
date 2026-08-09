from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quote_app.evidence.geometry import CssRect
from quote_app.evidence.quality import CaptureQualityError
from quote_app.sites.catalog import SiteSpec, load_site_catalog, site_session_family
from quote_app.sites.official_brands.base import LiveOfficialAdapterBase
from quote_app.sites.official_brands.models import (
    ApprovedHostFamily,
    OfficialBusinessState,
    OfficialCaptureView,
    OfficialDetailIdentity,
    OfficialManualAction,
    OfficialOfferSnapshot,
)
from quote_app.sites.registry import RegisteredSiteAdapter
from quote_app.tasks.models import (
    BusinessOutcome,
    WebsiteChannel,
    WebsiteObservationCheckpoint,
    WebsiteTask,
)
from quote_app.tasks.retry import (
    LoginRequired,
    NonRetryableTechnicalError,
    SecurityVerificationRequired,
)
from tests.factories.official_live_page import (
    OfficialLiveFrame,
    ScriptedOfficialLivePage,
)


def _xiaomi_spec() -> SiteSpec:
    return next(
        spec
        for spec in load_site_catalog()
        if spec.brand == "小米" and spec.channel is WebsiteChannel.OFFICIAL
    )


def _task(
    *,
    brand: str = "小米",
    channel: WebsiteChannel = WebsiteChannel.OFFICIAL,
) -> WebsiteTask:
    return WebsiteTask(
        task_id="task-xiaomi-1",
        run_id="run-1",
        source_row_number=2,
        output_row_number=2,
        material_code="material-1",
        brand=brand,
        model_name="小米 15",
        ram="12GB",
        storage="256GB",
        color="黑色",
        channel=channel,
    )


class _FixtureLiveAdapter(LiveOfficialAdapterBase):
    approved_host_families = (ApprovedHostFamily("mi.com"),)

    def _manual_action(self, page: object) -> OfficialManualAction | None:
        if getattr(page, "security_verification_required", False):
            return OfficialManualAction.SECURITY_VERIFICATION
        if getattr(page, "login_required", False):
            return OfficialManualAction.LOGIN
        return None

    def _read_business_state(
        self,
        task: WebsiteTask,
        page: ScriptedOfficialLivePage,
    ) -> OfficialBusinessState:
        frame = page.next_frame()
        return OfficialBusinessState.price_found(
            identity=OfficialDetailIdentity(page.url, frame.detail_identity),
            brand=task.brand,
            model_name=frame.model_name,
            capacity=frame.capacity,
            color=frame.color,
            price=frame.price,
        )

    def _observe_validated(
        self,
        task: WebsiteTask,
        page: ScriptedOfficialLivePage,
    ):
        return self.build_observation(task, self._read_business_state(task, page))

    def _resume_validated(
        self,
        task: WebsiteTask,
        page: ScriptedOfficialLivePage,
        checkpoint: WebsiteObservationCheckpoint,
    ):
        return self.build_observation(task, self._read_business_state(task, page))


def _page(*frames: OfficialLiveFrame) -> ScriptedOfficialLivePage:
    return ScriptedOfficialLivePage(
        "https://www.mi.com/shop/buy/detail?product_id=123",
        list(frames),
    )


def _frame(
    *,
    price: str = "3999.00",
    region: str = "福建>福州",
    stock_state: str = "现货",
) -> OfficialLiveFrame:
    return OfficialLiveFrame(
        detail_identity="product-id:123",
        model_name="小米 15",
        capacity="12GB+256GB",
        color="黑色",
        price=Decimal(price),
        region=region,
        stock_state=stock_state,
    )


def test_live_base_satisfies_registry_protocol_and_rejects_direct_execution() -> None:
    adapter = _FixtureLiveAdapter(_xiaomi_spec())

    assert isinstance(adapter, RegisteredSiteAdapter)
    with pytest.raises(NonRetryableTechnicalError) as error:
        adapter.execute(_task(), _page(_frame()), object())  # type: ignore[arg-type]

    assert error.value.code == "ADAPTER_DIRECT_EXECUTION_UNSUPPORTED"


def test_approved_host_family_accepts_exact_and_subdomain_urls() -> None:
    adapter = _FixtureLiveAdapter(_xiaomi_spec())

    assert adapter.require_approved_url("https://mi.com/product/1") == (
        "https://mi.com/product/1"
    )
    assert adapter.require_approved_url("https://www.mi.com/product/1") == (
        "https://www.mi.com/product/1"
    )
    assert adapter.require_approved_url("https://www.mi.com:443/product/1") == (
        "https://www.mi.com:443/product/1"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://www.mi.com/product/1",
        "ftp://www.mi.com/product/1",
        "https://user:pass@www.mi.com/product/1",
        "https://user@www.mi.com/product/1",
        "https://www.mi.com/product/1?api_key=secret",
        "https://www.mi.com:444/product/1",
        "https://mi.com.evil.test/product/1",
        "https:///missing-host",
    ],
)
def test_approved_url_rejects_insecure_credentialed_or_foreign_urls(
    url: str,
) -> None:
    adapter = _FixtureLiveAdapter(_xiaomi_spec())

    with pytest.raises(ValueError, match="approved HTTPS"):
        adapter.require_approved_url(url)


@pytest.mark.parametrize(
    "task",
    [
        _task(brand="华为"),
        _task(channel=WebsiteChannel.JD),
    ],
)
def test_observe_rejects_task_brand_or_channel_mismatch(task: WebsiteTask) -> None:
    adapter = _FixtureLiveAdapter(_xiaomi_spec())

    with pytest.raises(NonRetryableTechnicalError) as error:
        adapter.observe(task, _page(_frame()))

    assert error.value.code == "OFFICIAL_TASK_MISMATCH"


@pytest.mark.parametrize(
    ("page", "error_type"),
    [
        (
            ScriptedOfficialLivePage(
                "https://www.mi.com/shop",
                [_frame()],
                login_required=True,
            ),
            LoginRequired,
        ),
        (
            ScriptedOfficialLivePage(
                "https://www.mi.com/shop",
                [_frame()],
                security_verification_required=True,
            ),
            SecurityVerificationRequired,
        ),
    ],
)
def test_manual_page_states_raise_distinct_recoverable_actions(
    page: ScriptedOfficialLivePage,
    error_type: type[LoginRequired],
) -> None:
    adapter = _FixtureLiveAdapter(_xiaomi_spec())

    with pytest.raises(error_type) as error:
        adapter.raise_if_manual_action(page)

    assert error.value.site == site_session_family(
        _xiaomi_spec().brand,
        _xiaomi_spec().channel,
    )
    assert error.value.retry_cost == 0


def test_offer_snapshot_stabilizes_after_two_consecutive_equal_reads() -> None:
    adapter = _FixtureLiveAdapter(_xiaomi_spec())
    page = _page(
        _frame(price="3998.00"),
        _frame(price="3999.00"),
        _frame(price="3999.00"),
    )
    sleeps: list[float] = []

    snapshot = adapter.wait_for_stable_offer(
        _task(),
        page,
        max_checks=3,
        interval_seconds=0.2,
        sleeper=sleeps.append,
    )

    assert snapshot.price == Decimal("3999.00")
    assert sleeps == [0.2, 0.2]


def test_region_and_stock_changes_do_not_participate_in_offer_stability() -> None:
    adapter = _FixtureLiveAdapter(_xiaomi_spec())
    page = _page(
        _frame(region="福建>福州", stock_state="现货"),
        _frame(region="广东>深圳", stock_state="暂时缺货"),
    )

    snapshot = adapter.wait_for_stable_offer(
        _task(),
        page,
        max_checks=2,
        interval_seconds=0.1,
        sleeper=lambda _seconds: None,
    )

    assert snapshot == OfficialOfferSnapshot(
        identity=OfficialDetailIdentity(
            "https://www.mi.com/shop/buy/detail?product_id=123",
            "product-id:123",
        ),
        brand="小米",
        model_name="小米 15",
        capacity="12GB+256GB",
        color="黑色",
        price=Decimal("3999.00"),
    )


def test_common_base_does_not_hard_code_brand_capacity_strategy() -> None:
    adapter = _FixtureLiveAdapter(_xiaomi_spec())
    page = _page(
        OfficialLiveFrame(
            detail_identity="product-id:123",
            model_name="小米 15",
            capacity="256GB",
            color="黑色",
            price=Decimal("3999.00"),
            region="福建>福州",
            stock_state="现货",
        ),
        OfficialLiveFrame(
            detail_identity="product-id:123",
            model_name="小米 15",
            capacity="256GB",
            color="黑色",
            price=Decimal("3999.00"),
            region="福建>福州",
            stock_state="现货",
        ),
    )

    snapshot = adapter.wait_for_stable_offer(
        _task(),
        page,
        max_checks=2,
        interval_seconds=0.1,
        sleeper=lambda _seconds: None,
    )

    assert snapshot.capacity == "256GB"

def test_offer_wait_fails_closed_when_state_never_stabilizes() -> None:
    adapter = _FixtureLiveAdapter(_xiaomi_spec())

    with pytest.raises(CaptureQualityError) as error:
        adapter.wait_for_stable_offer(
            _task(),
            _page(
                _frame(price="3997.00"),
                _frame(price="3998.00"),
                _frame(price="3999.00"),
            ),
            max_checks=3,
            interval_seconds=0.1,
            sleeper=lambda _seconds: None,
        )

    assert error.value.code == "CAPTURE_UNSTABLE"


def test_price_found_observation_uses_explicit_legacy_boundary_mapping() -> None:
    adapter = _FixtureLiveAdapter(_xiaomi_spec())
    state = OfficialBusinessState.price_found(
        identity=OfficialDetailIdentity(
            "https://www.mi.com/shop/buy/detail?product_id=123",
            "product-id:123",
        ),
        brand="小米",
        model_name="小米 15",
        capacity="12GB+256GB",
        color="黑色",
        price=Decimal("3999.00"),
    )

    observation = adapter.build_observation(_task(), state)

    assert observation.outcome is BusinessOutcome.PRICE_FOUND
    assert observation.price == Decimal("3999.00")
    assert observation.semantic_state.current_sku == (
        "official-detail:product-id:123"
    )
    assert observation.semantic_state.region == (
        "excluded-from-live-official-stability:region"
    )
    assert observation.semantic_state.stock_state == (
        "excluded-from-live-official-stability:stock"
    )


@pytest.mark.parametrize(
    ("outcome", "rectangles", "roles"),
    [
        (
            BusinessOutcome.NO_MODEL,
            (
                CssRect(1, 2, 30, 40, "search_keyword"),
                CssRect(3, 4, 50, 60, "result_region"),
            ),
            ("search_keyword", "result_region"),
        ),
        (
            BusinessOutcome.CAPACITY_UNAVAILABLE,
            (CssRect(1, 2, 30, 40, "capacity"),),
            ("capacity",),
        ),
        (
            BusinessOutcome.COLOR_UNAVAILABLE,
            (CssRect(1, 2, 30, 40, "color"),),
            ("color",),
        ),
    ],
)
def test_builds_each_legal_no_observation(
    outcome: BusinessOutcome,
    rectangles: tuple[CssRect, ...],
    roles: tuple[str, ...],
) -> None:
    adapter = _FixtureLiveAdapter(_xiaomi_spec())
    state = OfficialBusinessState.legal_no(
        canonical_url="https://www.mi.com/shop/search?q=xiaomi15",
        brand="小米",
        model_name="小米 15",
        capacity="12GB+256GB",
        color="黑色",
        outcome=outcome,
        capture_view=OfficialCaptureView(rectangles),
        detail_identity=(
            None
            if outcome is BusinessOutcome.NO_MODEL
            else "product-id:123"
        ),
    )

    observation = adapter.build_observation(_task(), state)

    assert observation.outcome is outcome
    assert observation.price is None
    assert tuple(rect.role for rect in observation.css_rectangles) == roles
    assert observation.semantic_state.current_sku == "not-applicable"
    assert observation.semantic_state.region == "not-applicable"
    assert observation.semantic_state.stock_state == "not-applicable"


def test_no_model_requires_keyword_and_result_region_in_new_boundary() -> None:
    with pytest.raises(ValueError, match="rectangle roles"):
        OfficialBusinessState.legal_no(
            canonical_url="https://www.mi.com/shop/search?q=xiaomi15",
            brand="小米",
            model_name="小米 15",
            capacity="12GB+256GB",
            color="黑色",
            outcome=BusinessOutcome.NO_MODEL,
            capture_view=OfficialCaptureView(
                (CssRect(3, 4, 50, 60, "result_region"),)
            ),
            detail_identity=None,
        )


def test_common_boundary_never_produces_sold_out_outcome() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        OfficialBusinessState.legal_no(
            canonical_url="https://www.mi.com/shop/buy/detail?product_id=123",
            brand="小米",
            model_name="小米 15",
            capacity="12GB+256GB",
            color="黑色",
            outcome=BusinessOutcome.SOLD_OUT,
            capture_view=OfficialCaptureView(
                (CssRect(1, 2, 30, 40, "stock_status"),)
            ),
            detail_identity="product-id:123",
        )


def test_capture_state_comparison_uses_only_business_snapshot() -> None:
    before = OfficialOfferSnapshot(
        identity=OfficialDetailIdentity(
            "https://www.mi.com/shop/buy/detail?product_id=123",
            "product-id:123",
        ),
        brand="小米",
        model_name="小米 15",
        capacity="12GB+256GB",
        color="黑色",
        price=Decimal("3999.00"),
    )

    LiveOfficialAdapterBase.require_same_capture_state(before, before)
    with pytest.raises(CaptureQualityError, match="business state changed"):
        LiveOfficialAdapterBase.require_same_capture_state(
            before,
            OfficialOfferSnapshot(
                identity=before.identity,
                brand=before.brand,
                model_name=before.model_name,
                capacity=before.capacity,
                color=before.color,
                price=Decimal("3998.00"),
            ),
        )


@pytest.mark.parametrize(
    "checkpoint",
    [
        WebsiteObservationCheckpoint(
            task_id="another-task",
            outcome=BusinessOutcome.PRICE_FOUND,
            price=Decimal("3999.00"),
            url="https://www.mi.com/shop/buy/detail?product_id=123",
            observed_at=datetime.now(timezone.utc),
        ),
        WebsiteObservationCheckpoint(
            task_id="task-xiaomi-1",
            outcome=BusinessOutcome.PRICE_FOUND,
            price=Decimal("3999.00"),
            url="https://example.com/product/123",
            observed_at=datetime.now(timezone.utc),
        ),
    ],
)
def test_resume_rejects_mismatched_task_or_checkpoint(checkpoint) -> None:
    adapter = _FixtureLiveAdapter(_xiaomi_spec())

    with pytest.raises(NonRetryableTechnicalError) as error:
        adapter.resume(_task(), _page(_frame()), checkpoint)

    assert error.value.code == "RECOVERY_INVALID"


def test_resume_rejects_unsupported_checkpoint_before_page_read() -> None:
    adapter = _FixtureLiveAdapter(_xiaomi_spec())
    page = _page(_frame())
    checkpoint = WebsiteObservationCheckpoint(
        task_id="task-xiaomi-1",
        outcome=BusinessOutcome.SOLD_OUT,
        price=None,
        url="https://www.mi.com/shop/buy/detail?product_id=123",
        observed_at=datetime.now(timezone.utc),
    )

    with pytest.raises(NonRetryableTechnicalError) as error:
        adapter.resume(_task(), page, checkpoint)

    assert error.value.code == "RECOVERY_INVALID"
    assert page.read_index == 0
