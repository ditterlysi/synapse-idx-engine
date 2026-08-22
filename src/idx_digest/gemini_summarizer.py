from __future__ import annotations

from typing import Any

import httpx

from .config import Settings
from .observability import RunObserver
from .summarizer import OpenRouterSummarizer
from .summary_schemas import SummaryError


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif item is not None:
                parts.append(str(item))
        return "\n".join(parts)
    return "" if content is None else str(content)


def build_gemini_request(openrouter_payload: dict[str, Any]) -> dict[str, Any]:
    """Translate the engine's internal structured-completion request to Gemini REST.

    The existing summarizer owns prompt rendering, retries, auditing, and local
    schema validation. This adapter changes only the upstream transport shape.
    """

    messages = openrouter_payload.get("messages") or []
    system_parts: list[str] = []
    user_parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        text = _message_text(message.get("content")).strip()
        if not text:
            continue
        if str(message.get("role") or "").lower() == "system":
            system_parts.append(text)
        else:
            user_parts.append(text)

    response_format = openrouter_payload.get("response_format") or {}
    json_schema = response_format.get("json_schema") if isinstance(response_format, dict) else None
    schema = json_schema.get("schema") if isinstance(json_schema, dict) else None
    if not isinstance(schema, dict):
        raise SummaryError("Gemini adapter requires a JSON schema")

    max_tokens = int(openrouter_payload.get("max_tokens") or 4000)
    request: dict[str, Any] = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": "\n\n".join(user_parts)}],
            }
        ],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
        },
    }
    if system_parts:
        request["systemInstruction"] = {
            "parts": [{"text": "\n\n".join(system_parts)}]
        }
    return request


class GeminiSummarizer(OpenRouterSummarizer):
    """Direct Gemini Developer API transport with inherited Synapse validation.

    OpenRouterSummarizer remains available for compatibility. We reuse its prompt
    rendering, retry policy, audit persistence, concurrency gates, and strict
    post-response schema validation, while translating only the HTTP request and
    response shapes for Gemini's GenerateContent API.
    """

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        observer: RunObserver | None = None,
    ) -> None:
        api_key = settings.gemini_api_key.strip()
        model = settings.gemini_model.strip()
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required when AI_PROVIDER=gemini")
        if not model:
            raise ValueError("GEMINI_MODEL must not be empty")

        # The parent class expects an OpenRouter-shaped transport configuration.
        # Use an isolated copy so Gemini never mutates caller-owned settings.
        shim = settings.model_copy(
            update={
                "openrouter_api_key": api_key,
                "openrouter_base_url": settings.gemini_base_url,
                "openrouter_model": model,
                "openrouter_provider": "google-gemini",
                "openrouter_allow_fallbacks": False,
                "openrouter_require_parameters": False,
            }
        )

        gemini_client = client or httpx.Client(
            base_url=settings.gemini_base_url.rstrip("/") + "/",
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(120.0, connect=20.0),
            http2=True,
            follow_redirects=True,
        )
        super().__init__(shim, client=gemini_client, observer=observer)
        self._owns_client = client is None
        self.api_model = model
        self.model = f"{model}@google-gemini"

    def _request_non_streaming(self, payload: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        request = build_gemini_request(payload)
        response = self.client.post(
            f"models/{self.api_model}:generateContent",
            json=request,
        )
        if response.is_error:
            raise SummaryError(
                f"Gemini request failed ({response.status_code}): {response.text[:1000]}"
            )

        body = response.json()
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise SummaryError(f"Unexpected Gemini response shape: {body}")
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise SummaryError(f"Unexpected Gemini candidate shape: {candidate!r}")

        content = candidate.get("content") or {}
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise SummaryError(f"Gemini response contained no text parts: {body}")
        text = "".join(
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, dict) and part.get("text") is not None
        )
        if not text.strip():
            raise SummaryError(f"Gemini returned empty structured content: {body}")

        metadata = body.get("usageMetadata") or {}
        usage = {
            "prompt_tokens": metadata.get("promptTokenCount"),
            "completion_tokens": metadata.get("candidatesTokenCount"),
            "total_tokens": metadata.get("totalTokenCount"),
            "thoughts_tokens": metadata.get("thoughtsTokenCount"),
            "finish_reason": candidate.get("finishReason"),
        }
        return text, usage

    def _request_streaming(
        self,
        payload: dict[str, Any],
        *,
        stream_label: str,
        request_id: str | None = None,
        audit_context: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        # Source-neutral Synapse ingestion does not require token streaming. One
        # GenerateContent call preserves the same validation/audit path.
        content, usage = self._request_non_streaming(payload)
        return str(content), usage
