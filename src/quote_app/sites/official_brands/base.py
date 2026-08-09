from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from urllib.parse import urlsplit

from quote_app.evidence.platform import PlatformEvidenceCapture
from quote_app.evidence.quality import CaptureQualityError
from quote_app.evidence.semantic_state import (
    SemanticStateReader,
    VerifiedSemanticState,
)
from quote_app.sites.catalog import SiteSpec, site_session_family
from quote_app.sites.official_brands.models import (
    ApprovedHostFamily,
    OfficialBusinessState,
    OfficialManualAction,
    OfficialOfferSnapshot,
)
from quote_app.sites.protocol import AdapterObservation, BrowserPage
from quote_app.tasks.models import (
    BusinessOutcome,
    WebsiteChannel,
    WebsiteObservationCheckpoint,
    WebsiteResult,
    WebsiteTask,
    url_contains_credentials,
)
from quote_app.tasks.retry import (
    LoginRequired,
    NonRetryableTechnicalError,
    SecurityVerificationRequired,
)

_LEGACY_REGION_EXCLUSION = "excluded-from-live-official-stability:region"
_LEGACY_STOCK_EXCLUSION = "excluded-from-live-official-stability:stock"
_RECOVERABLE_OUTCOMES = frozenset(
    {
        BusinessOutcome.PRICE_FOUND,
        BusinessOutcome.NO_MODEL,
        BusinessOutcome.CAPACITY_UNAVAILABLE,
        BusinessOutcome.COLOR_UNAVAILABLE,
    }
)


class LiveOfficialAdapterBase(ABC):
    """Mechanical boundary shared by independent live official adapters."""

    channel = WebsiteChannel.OFFICIAL
    approved_host_families: tuple[ApprovedHostFamily, ...] = ()

    def __init__(self, spec: SiteSpec) -> None:
        if not isinstance(spec, SiteSpec):
            raise TypeError("spec must be a SiteSpec")
        spec.validate_approved()
        if spec.channel is not WebsiteChannel.OFFICIAL:
            raise ValueError("spec channel must be OFFICIAL")
        if not self.approved_host_families:
            raise ValueError("adapter must define approved host families")
        if not all(
            isinstance(family, ApprovedHostFamily)
            for family in self.approved_host_families
        ):
            raise TypeError("approved_host_families must contain host families")
        self._spec = spec
        self.require_approved_url(spec.entry_url)

    @property
    def spec(self) -> SiteSpec:
        return self._spec

    def observe(
        self,
        task: WebsiteTask,
        page: BrowserPage,
    ) -> AdapterObservation:
        self._validate_task(task)
        self.raise_if_manual_action(page)
        return self._observe_validated(task, page)

    def execute(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        capture: PlatformEvidenceCapture,
    ) -> WebsiteResult:
        del task, page, capture
        raise NonRetryableTechnicalError(
            "ADAPTER_DIRECT_EXECUTION_UNSUPPORTED",
            "站点适配器必须通过任务执行器生成正式截图",
        )

    def resume(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        checkpoint: WebsiteObservationCheckpoint,
    ) -> AdapterObservation:
        self._validate_task(task)
        self._validate_checkpoint(task, checkpoint)
        self.raise_if_manual_action(page)
        return self._resume_validated(task, page, checkpoint)

    def require_approved_url(self, url: str) -> str:
        normalized = url.strip() if isinstance(url, str) else ""
        try:
            parsed = urlsplit(normalized)
            hostname = parsed.hostname or ""
            valid = (
                parsed.scheme.lower() == "https"
                and bool(hostname)
                and parsed.port in {None, 443}
                and parsed.username is None
                and parsed.password is None
                and not url_contains_credentials(normalized)
                and any(
                    family.contains(hostname)
                    for family in self.approved_host_families
                )
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("URL must be an approved HTTPS official URL")
        return normalized

    def raise_if_manual_action(self, page: BrowserPage) -> None:
        action = self._manual_action(page)
        site = site_session_family(self.spec.brand, self.spec.channel)
        if action is OfficialManualAction.SECURITY_VERIFICATION:
            raise SecurityVerificationRequired(
                site,
                "官网触发安全验证，请完成后继续当前任务",
            )
        if action is OfficialManualAction.LOGIN:
            raise LoginRequired(
                site,
                "官网需要登录，请完成后继续当前任务",
            )
        if action is not None:
            raise ValueError("manual action must be an OfficialManualAction")

    def wait_for_stable_offer(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        *,
        interval_seconds: float,
        max_checks: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> OfficialOfferSnapshot:
        self._validate_task(task)
        if (
            not isinstance(interval_seconds, int | float)
            or isinstance(interval_seconds, bool)
            or not math.isfinite(interval_seconds)
            or interval_seconds <= 0
        ):
            raise ValueError("interval_seconds must be finite and positive")
        if type(max_checks) is not int or max_checks < 2:
            raise ValueError("max_checks must be at least 2")
        if not callable(sleeper):
            raise ValueError("sleeper must be callable")

        previous = self._validated_offer_snapshot(task, page)
        for _ in range(max_checks - 1):
            sleeper(float(interval_seconds))
            current = self._validated_offer_snapshot(task, page)
            if current == previous:
                return current
            previous = current
        raise CaptureQualityError(
            "CAPTURE_UNSTABLE",
            "live official business state did not stabilize",
        )

    def verified_state_reader(
        self,
        task: WebsiteTask,
        page: BrowserPage,
    ) -> SemanticStateReader:
        self._validate_task(task)

        def reader() -> VerifiedSemanticState:
            state = self._read_business_state(task, page)
            return self.build_observation(task, state).semantic_state

        return reader

    def build_observation(
        self,
        task: WebsiteTask,
        state: OfficialBusinessState,
    ) -> AdapterObservation:
        self._validate_task(task)
        if not isinstance(state, OfficialBusinessState):
            raise TypeError("state must be an OfficialBusinessState")
        url = self.require_approved_url(state.canonical_url)
        if state.brand != task.brand or state.model_name != task.model_name:
            raise NonRetryableTechnicalError(
                "OFFICIAL_STATE_MISMATCH",
                "官网业务状态与当前任务不一致",
            )
        rectangles = state.capture_view.css_rectangles
        if state.outcome is BusinessOutcome.PRICE_FOUND:
            if state.detail_identity is None:
                raise AssertionError("price-found detail identity is required")
            current_sku = f"official-detail:{state.detail_identity}"
            region = _LEGACY_REGION_EXCLUSION
            stock_state = _LEGACY_STOCK_EXCLUSION
        else:
            current_sku = "not-applicable"
            region = "not-applicable"
            stock_state = "not-applicable"
        semantic_state = VerifiedSemanticState(
            canonical_url=url,
            brand=state.brand,
            model_name=state.model_name,
            capacity=state.capacity,
            color=state.color,
            current_sku=current_sku,
            region=region,
            stock_state=stock_state,
            price=state.price,
            outcome=state.outcome,
            css_rectangles=rectangles,
        )
        return AdapterObservation(
            outcome=state.outcome,
            price=state.price,
            url=url,
            css_rectangles=rectangles,
            semantic_state=semantic_state,
        )

    @staticmethod
    def require_same_capture_state(
        before: OfficialOfferSnapshot,
        after: OfficialOfferSnapshot,
    ) -> None:
        if (
            not isinstance(before, OfficialOfferSnapshot)
            or not isinstance(after, OfficialOfferSnapshot)
            or before != after
        ):
            raise CaptureQualityError(
                "CAPTURE_UNSTABLE",
                "live official business state changed during capture",
            )

    def _validate_task(self, task: WebsiteTask) -> None:
        if (
            not isinstance(task, WebsiteTask)
            or task.brand != self.spec.brand
            or task.channel is not WebsiteChannel.OFFICIAL
        ):
            raise NonRetryableTechnicalError(
                "OFFICIAL_TASK_MISMATCH",
                "官网任务品牌或渠道与适配器不一致",
            )

    def _validate_checkpoint(
        self,
        task: WebsiteTask,
        checkpoint: WebsiteObservationCheckpoint,
    ) -> None:
        try:
            valid = (
                isinstance(checkpoint, WebsiteObservationCheckpoint)
                and checkpoint.task_id == task.task_id
                and checkpoint.outcome in _RECOVERABLE_OUTCOMES
            )
            if valid:
                self.require_approved_url(checkpoint.url)
        except ValueError:
            valid = False
        if not valid:
            raise NonRetryableTechnicalError(
                "RECOVERY_INVALID",
                "官网恢复检查点与当前任务不一致",
            )

    def _validated_offer_snapshot(
        self,
        task: WebsiteTask,
        page: BrowserPage,
    ) -> OfficialOfferSnapshot:
        self.raise_if_manual_action(page)
        state = self._read_business_state(task, page)
        self.require_approved_url(state.canonical_url)
        snapshot = state.offer_snapshot()
        if not self._offer_matches_task(task, snapshot):
            raise CaptureQualityError(
                "CAPTURE_UNSTABLE",
                "live official business snapshot does not match the task",
            )
        return snapshot

    def _offer_matches_task(
        self,
        task: WebsiteTask,
        snapshot: OfficialOfferSnapshot,
    ) -> bool:
        """Check shared identity only; brand modules own configuration rules."""
        return (
            snapshot.brand == task.brand
            and snapshot.model_name == task.model_name
        )

    def _manual_action(self, page: BrowserPage) -> OfficialManualAction | None:
        return None

    @abstractmethod
    def _read_business_state(
        self,
        task: WebsiteTask,
        page: BrowserPage,
    ) -> OfficialBusinessState:
        raise NotImplementedError

    @abstractmethod
    def _observe_validated(
        self,
        task: WebsiteTask,
        page: BrowserPage,
    ) -> AdapterObservation:
        raise NotImplementedError

    @abstractmethod
    def _resume_validated(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        checkpoint: WebsiteObservationCheckpoint,
    ) -> AdapterObservation:
        raise NotImplementedError
