from __future__ import annotations

from types import SimpleNamespace

import pytest

from idx_digest.ai_fallback import (
    FallbackAwareGeminiSummarizer,
    RetryableAIProviderError,
)
from idx_digest.gemini_summarizer import GeminiSummarizer
from idx_digest.provider_gate import AdaptiveProviderGate


def test_retryable_gemini_fallback_failure_releases_provider_slot(monkeypatch) -> None:
    gate = AdaptiveProviderGate(configured_max=1, enabled=True)
    gate.acquire(request_class="bulk_chunk")
    assert gate.metrics["active"] == 1

    summarizer = object.__new__(FallbackAwareGeminiSummarizer)
    summarizer.provider_gate = gate
    summarizer.settings = SimpleNamespace(llm_concurrency=1)

    error = RetryableAIProviderError("Gemini network failure: timed out")

    def fail_completion(*args, **kwargs):
        raise error

    monkeypatch.setattr(GeminiSummarizer, "_completion_once", fail_completion)

    with pytest.raises(RetryableAIProviderError, match="network failure"):
        summarizer._completion_once(
            "prompt",
            schema_name="test_schema",
            schema={"type": "object"},
            max_tokens=100,
        )

    metrics = gate.metrics
    assert metrics["active"] == 0
    assert metrics["failure_count"] == 1
    assert metrics["transient_failure_events"] == 1
