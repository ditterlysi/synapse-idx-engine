from __future__ import annotations

import json

import httpx
import pytest

from idx_digest.gemini_summarizer import (
    GeminiRateLimitError,
    GeminiSummarizer,
    build_gemini_request,
)


def _openrouter_payload() -> dict[str, object]:
    return {
        "model": "ignored-by-gemini-adapter",
        "messages": [
            {"role": "system", "content": "Return verified structured facts only."},
            {"role": "user", "content": "Summarize this disclosure."},
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
        "max_tokens": 1234,
        "temperature": 0.1,
        "provider": {"only": ["some-provider"]},
        "reasoning": {"enabled": False},
    }


def _bare_summarizer(client: httpx.Client) -> GeminiSummarizer:
    summarizer = object.__new__(GeminiSummarizer)
    summarizer.client = client
    summarizer.api_model = "gemini-3.5-flash-lite"
    summarizer.observer = None
    return summarizer


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


def test_build_gemini_request_uses_generate_content_structured_output() -> None:
    request = build_gemini_request(_openrouter_payload())

    assert request["systemInstruction"]["parts"][0]["text"] == "Return verified structured facts only."
    assert request["contents"][0]["parts"][0]["text"] == "Summarize this disclosure."
    config = request["generationConfig"]
    assert config["maxOutputTokens"] == 1234
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"]["required"] == ["summary"]
    assert "responseFormat" not in config
    assert "temperature" not in config
    assert "provider" not in request
    assert "reasoning" not in request


def test_gemini_response_maps_back_to_existing_summarizer_contract() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return _success_response()

    client = httpx.Client(
        base_url="https://generativelanguage.googleapis.com/v1beta/",
        transport=httpx.MockTransport(handler),
    )
    summarizer = _bare_summarizer(client)

    content, usage = summarizer._request_non_streaming(_openrouter_payload())

    assert content == '{"summary":"ok"}'
    assert captured["path"].endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert captured["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert captured["body"]["generationConfig"]["responseJsonSchema"]["required"] == ["summary"]
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 4
    assert usage["total_tokens"] == 14
    assert usage["finish_reason"] == "STOP"
    client.close()


def test_gemini_429_obeys_cooldown_then_retries_once() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"error": {"code": 429, "message": "quota exceeded"}},
            )
        return _success_response()

    client = httpx.Client(
        base_url="https://generativelanguage.googleapis.com/v1beta/",
        transport=httpx.MockTransport(handler),
    )
    summarizer = _bare_summarizer(client)

    content, _usage = summarizer._request_non_streaming(_openrouter_payload())

    assert content == '{"summary":"ok"}'
    assert calls == 2
    assert summarizer._gemini_serialize_after_429 is True
    client.close()


def test_gemini_429_stops_after_one_cooldown_retry() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "0"},
            json={"error": {"code": 429, "message": "quota exceeded"}},
        )

    client = httpx.Client(
        base_url="https://generativelanguage.googleapis.com/v1beta/",
        transport=httpx.MockTransport(handler),
    )
    summarizer = _bare_summarizer(client)

    with pytest.raises(GeminiRateLimitError) as exc_info:
        summarizer._request_non_streaming(_openrouter_payload())

    assert exc_info.value.status_code == 429
    assert calls == 2
    assert summarizer._gemini_serialize_after_429 is True
    client.close()
