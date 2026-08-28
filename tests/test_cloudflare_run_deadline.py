from __future__ import annotations

import pytest

from idx_digest import cloudflare_summarizer as cloudflare_module
from idx_digest.ai_fallback import is_retryable_provider_failure
from idx_digest.cloudflare_summarizer import (
    CloudflareRunDeadlineError,
    CloudflareWorkersAISummarizer,
)


def _summarizer(*, deadline: float) -> CloudflareWorkersAISummarizer:
    summarizer = object.__new__(CloudflareWorkersAISummarizer)
    summarizer._run_deadline_at = deadline
    return summarizer


def test_daily_deadline_is_not_treated_as_provider_failure(monkeypatch) -> None:
    monkeypatch.setattr(cloudflare_module.time, "monotonic", lambda: 100.0)
    summarizer = _summarizer(deadline=100.5)

    with pytest.raises(CloudflareRunDeadlineError, match="deadline"):
        summarizer._remaining_run_seconds()

    error = CloudflareRunDeadlineError("daily deadline")
    assert error.run_deadline_exceeded is True
    assert error.retryable_provider_failure is False
    assert is_retryable_provider_failure(error) is False


def test_request_timeout_is_capped_by_remaining_daily_session(monkeypatch) -> None:
    monkeypatch.setattr(cloudflare_module.time, "monotonic", lambda: 100.0)
    summarizer = _summarizer(deadline=130.0)

    assert summarizer._request_timeout_seconds() == 29.0


def test_request_timeout_never_exceeds_cloudflare_cap(monkeypatch) -> None:
    monkeypatch.setattr(cloudflare_module.time, "monotonic", lambda: 100.0)
    summarizer = _summarizer(deadline=1000.0)

    assert summarizer._request_timeout_seconds() == 90.0
