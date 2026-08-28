from __future__ import annotations

from types import SimpleNamespace

import pytest

from idx_digest import cloudflare_summarizer as cloudflare_module
from idx_digest.ai_fallback import is_retryable_provider_failure
from idx_digest.cloudflare_summarizer import (
    CloudflareProviderError,
    CloudflareRunDeadlineError,
    CloudflareWorkersAISummarizer,
)
from idx_digest.provider_gate import AdaptiveProviderGate
from idx_digest.summarizer import OpenRouterSummarizer


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


@pytest.mark.parametrize(
    "error",
    [
        CloudflareRunDeadlineError("daily deadline"),
        CloudflareProviderError("provider unavailable", status_code=503),
    ],
)
def test_typed_cloudflare_failure_releases_provider_slot(monkeypatch, error) -> None:
    gate = AdaptiveProviderGate(configured_max=1, enabled=True)
    gate.acquire(request_class="priority")
    assert gate.metrics["active"] == 1

    summarizer = object.__new__(CloudflareWorkersAISummarizer)
    summarizer.provider_gate = gate
    summarizer.settings = SimpleNamespace(llm_concurrency=1)

    def fail_completion(*args, **kwargs):
        raise error

    monkeypatch.setattr(OpenRouterSummarizer, "_completion_once", fail_completion)

    with pytest.raises(type(error), match=str(error)):
        summarizer._completion_once(
            "prompt",
            schema_name="test_schema",
            schema={"type": "object"},
            max_tokens=100,
        )

    assert gate.metrics["active"] == 0
