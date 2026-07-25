from enum import StrEnum


class RowStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
