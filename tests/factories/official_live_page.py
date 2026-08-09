from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class OfficialLiveFrame:
    detail_identity: str
    model_name: str
    capacity: str
    color: str
    price: Decimal
    region: str
    stock_state: str


class ScriptedOfficialLivePage:
    def __init__(
        self,
        url: str,
        frames: list[OfficialLiveFrame],
        *,
        login_required: bool = False,
        security_verification_required: bool = False,
    ) -> None:
        self.url = url
        self.frames = frames
        self.login_required = login_required
        self.security_verification_required = security_verification_required
        self.read_index = 0

    def next_frame(self) -> OfficialLiveFrame:
        if not self.frames:
            raise AssertionError("scripted page requires at least one frame")
        index = min(self.read_index, len(self.frames) - 1)
        self.read_index += 1
        return self.frames[index]
