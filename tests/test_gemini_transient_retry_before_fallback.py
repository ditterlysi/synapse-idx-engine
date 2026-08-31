from __future__ import annotations

import httpx
import pytest

from idx_digest.ai_fallback import FallbackAwareGeminiSummarizer, RetryableAIProviderError
from idx_digest.config import Settings


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        gemini_api_key="gemini-test-key",
        gemini_model="gemini-3.5-flash-lite",
    )


def _payload() -> dict[str, object]:
    return {
        "model": "gemini-3.5-flash-lite",
        "messages": [
            {"role": "system", "content": "Return structured facts only."},
            {"role": "user", "content": "Summarize the filing."},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "test_schema",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                    "additionalProperties": False,
                },
            },
        },
        "max_tokens": 1000,
    }


def _success_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {"parts": [{"text": '{"summary":"ok"}'}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 4,
                "totalTokenCount": 14,
            },
        },
    )


def _client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://generativelanguage.googleapis.com/v1beta/",
        transport=httpx.MockTransport(handler),
    )


def test_transient_disconnect_retries_gemini_once_before_fallback(tmp_path, monkeypatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadError("Server disconnected", request=request)
        return _success_response()

    monkeypatch.setattr("idx_digest.ai_fallback.time.sleep", lambda _seconds: None)
    client = _client(handler)
    summarizer = FallbackAwareGeminiSummarizer(_settings(tmp_path), client=client)
    try:
        content, usage = summarizer._request_non_streaming(_payload())
    finally:
        summarizer.close()
        client.close()

    assert calls == 2
    assert content == '{"summary":"ok"}'
    assert usage["total_tokens"] == 14


def test_second_disconnect_becomes_fallback_eligible(tmp_path, monkeypatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadError("Server disconnected", request=request)

    monkeypatch.setattr("idx_digest.ai_fallback.time.sleep", lambda _seconds: None)
    client = _client(handler)
    summarizer = FallbackAwareGeminiSummarizer(_settings(tmp_path), client=client)
    try:
        with pytest.raises(RetryableAIProviderError, match="after one retry"):
            summarizer._request_non_streaming(_payload())
    finally:
        summarizer.close()
        client.close()

    assert calls == 2


def test_gemini_503_retries_once_then_becomes_fallback_eligible(tmp_path, monkeypatch) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="temporary upstream outage")

    monkeypatch.setattr("idx_digest.ai_fallback.time.sleep", lambda _seconds: None)
    client = _client(handler)
    summarizer = FallbackAwareGeminiSummarizer(_settings(tmp_path), client=client)
    try:
        with pytest.raises(RetryableAIProviderError) as exc_info:
            summarizer._request_non_streaming(_payload())
    finally:
        summarizer.close()
        client.close()

    assert calls == 2
    assert exc_info.value.status_code == 503
