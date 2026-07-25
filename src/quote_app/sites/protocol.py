from __future__ import annotations

from typing import Protocol, runtime_checkable

from quote_app.evidence.platform import PlatformEvidenceCapture
from quote_app.tasks.models import WebsiteChannel, WebsiteResult, WebsiteTask


@runtime_checkable
class BrowserPage(Protocol):
    """Minimal browser-page surface shared by site adapters."""

    @property
    def url(self) -> str: ...


@runtime_checkable
class SiteAdapter(Protocol):
    """Stable boundary between a website task and its completed result."""

    channel: WebsiteChannel

    def execute(
        self,
        task: WebsiteTask,
        page: BrowserPage,
        capture: PlatformEvidenceCapture,
    ) -> WebsiteResult: ...
