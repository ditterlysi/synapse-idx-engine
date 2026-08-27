from __future__ import annotations

import threading

import pytest

from idx_digest import gemini_summarizer as gemini_module
from idx_digest.ai_fallback import is_retryable_provider_failure
from idx_digest.gemini_summarizer import GeminiRunDeadlineError, GeminiSummarizer


def _summarizer(*, deadline: float) -> GeminiSummarizer:
    summarizer = object.__new__(GeminiSummarizer)
    summarizer._run_deadline_at = deadline
    summarizer._gemini_rate_limit_lock = threading.RLock()
    summarizer._gemini_rate_limit_serial = threading.Lock()
    summarizer._gemini_rate_limit_until = 0.0
    summarizer._gemini_serialize_after_429 = False
    return summarizer


def test_daily_deadline_is_not_treated_as_provider_failure(monkeypatch) -> None:
    monkeypatch.setattr(gemini_module.time, "monotonic", lambda: 100.0)
    summarizer = _summarizer(deadline=100.5)

    with pytest.raises(GeminiRunDeadlineError, match="deadline"):
        summarizer._remaining_run_seconds()

    error = GeminiRunDeadlineError("daily deadline")
    assert error.run_deadline_exceeded is True
    assert error.retryable_provider_failure is False
    assert is_retryable_provider_failure(error) is False


def test_rate_limit_cooldown_cannot_sleep_past_daily_deadline(monkeypatch) -> None:
    monkeypatch.setattr(gemini_module.time, "monotonic", lambda: 100.0)
    summarizer = _summarizer(deadline=110.0)
    summarizer._gemini_rate_limit_until = 120.0

    with pytest.raises(GeminiRunDeadlineError, match="cooldown"):
        summarizer._wait_for_rate_limit_cooldown()


def test_request_timeout_is_capped_by_remaining_daily_session(monkeypatch) -> None:
    monkeypatch.setattr(gemini_module.time, "monotonic", lambda: 100.0)
    summarizer = _summarizer(deadline=130.0)

    assert summarizer._request_timeout_seconds() == 29.0
