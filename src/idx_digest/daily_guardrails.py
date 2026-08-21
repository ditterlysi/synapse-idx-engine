from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic

from .config import Settings


class DailyPolicyError(ValueError):
    """Raised when scheduled-mode configuration violates a non-negotiable policy."""


class DailyBudgetExceeded(RuntimeError):
    """Raised before an operation would exceed the configured daily budget."""


@dataclass(frozen=True)
class DailyPolicy:
    transport: str
    request_delay_seconds: float
    request_jitter_seconds: float
    max_source_requests: int
    max_attachments: int
    max_download_bytes: int
    max_ai_documents: int
    max_run_seconds: int
    allow_historical_backfill: bool
    allow_ticker_fanout: bool

    @classmethod
    def from_settings(cls, settings: Settings) -> "DailyPolicy":
        policy = cls(
            transport=settings.synapse_daily_transport.strip().lower(),
            request_delay_seconds=settings.synapse_daily_request_delay_seconds,
            request_jitter_seconds=settings.synapse_daily_request_jitter_seconds,
            max_source_requests=settings.synapse_daily_max_source_requests,
            max_attachments=settings.synapse_daily_max_attachments,
            max_download_bytes=settings.synapse_daily_max_download_bytes,
            max_ai_documents=settings.synapse_daily_max_ai_documents,
            max_run_seconds=settings.synapse_daily_max_run_seconds,
            allow_historical_backfill=settings.synapse_daily_allow_historical_backfill,
            allow_ticker_fanout=settings.synapse_daily_allow_ticker_fanout,
        )
        policy.validate()
        return policy

    def validate(self) -> None:
        if self.transport != "http":
            raise DailyPolicyError("Synapse daily mode must use HTTP-only transport and stop on access protection")
        if self.allow_historical_backfill:
            raise DailyPolicyError("Historical backfill is not allowed in Synapse daily mode")
        if self.allow_ticker_fanout:
            raise DailyPolicyError("Per-ticker fan-out is not allowed in Synapse daily mode")
        if self.request_delay_seconds < 0.5:
            raise DailyPolicyError("Synapse daily request delay must be at least 0.5 seconds")

    def apply_to_runtime(self, settings: Settings) -> Settings:
        """Return settings for the existing pipeline without mutating generic/manual defaults."""
        return settings.model_copy(
            update={
                "idx_transport": self.transport,
                "idx_request_delay_seconds": self.request_delay_seconds,
            }
        )


@dataclass
class DailyRunBudget:
    policy: DailyPolicy
    source_requests: int = 0
    attachments: int = 0
    download_bytes: int = 0
    ai_documents: int = 0
    _started_at: float = field(default_factory=monotonic, repr=False)

    def _check_time(self) -> None:
        if monotonic() - self._started_at > self.policy.max_run_seconds:
            raise DailyBudgetExceeded("daily run time budget exceeded")

    @staticmethod
    def _ensure_non_negative(value: int, label: str) -> None:
        if value < 0:
            raise ValueError(f"{label} must be non-negative")

    def consume_source_request(self, count: int = 1) -> None:
        self._ensure_non_negative(count, "source request count")
        self._check_time()
        if self.source_requests + count > self.policy.max_source_requests:
            raise DailyBudgetExceeded("source request budget exceeded")
        self.source_requests += count

    def consume_attachment(self, count: int = 1) -> None:
        self._ensure_non_negative(count, "attachment count")
        self._check_time()
        if self.attachments + count > self.policy.max_attachments:
            raise DailyBudgetExceeded("attachment budget exceeded")
        self.attachments += count

    def consume_download_bytes(self, count: int) -> None:
        self._ensure_non_negative(count, "download byte count")
        self._check_time()
        if self.download_bytes + count > self.policy.max_download_bytes:
            raise DailyBudgetExceeded("download byte budget exceeded")
        self.download_bytes += count

    def consume_ai_document(self, count: int = 1) -> None:
        self._ensure_non_negative(count, "AI document count")
        self._check_time()
        if self.ai_documents + count > self.policy.max_ai_documents:
            raise DailyBudgetExceeded("AI document budget exceeded")
        self.ai_documents += count

    def snapshot(self) -> dict[str, int]:
        self._check_time()
        return {
            "source_requests": self.source_requests,
            "attachments": self.attachments,
            "download_bytes": self.download_bytes,
            "ai_documents": self.ai_documents,
        }
