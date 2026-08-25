from __future__ import annotations

import typing

import httpx

from .config import Settings
from .observability import RunObserver
from .summarizer import OpenRouterSummarizer
from .summary_schemas import SummaryError

CLOUDFLARE_PROVIDER = "cloudflare-workers-ai"


class CloudflareProviderError(ValueError):
    """Terminal Workers AI provider failure for one analysis attempt."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class CloudflareRateLimitError(CloudflareProviderError):
    """Workers AI free-tier/account rate limit that should defer later work."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=429)


def build_cloudflare_request(payload: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Translate the engine request into Workers AI OpenAI-compatible JSON mode.

    The parent summarizer adds OpenRouter-only routing fields. Workers AI does not
    need those fields, while its JSON mode accepts the schema directly under
    response_format.json_schema.
    """

    request = dict(payload)
    request.pop("provider", None)
    request.pop("reasoning", None)

    response_format = request.get("response_format")
    if isinstance(response_format, dict):
        json_schema = response_format.get("json_schema")
        if isinstance(json_schema, dict):
            nested_schema = json_schema.get("schema")
            if isinstance(nested_schema, dict):
                request["response_format"] = {
                    "type": "json_schema",
                    "json_schema": nested_schema,
                }
    return request


class CloudflareWorkersAISummarizer(OpenRouterSummarizer):
    """Workers AI transport that reuses Synapse prompts and local validation."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        observer: RunObserver | None = None,
    ) -> None:
        account_id = settings.cloudflare_ai_account_id.strip()
        api_token = settings.cloudflare_ai_api_token.strip()
        model = settings.cloudflare_ai_model.strip()
        if not account_id:
            raise ValueError("CLOUDFLARE_AI_ACCOUNT_ID is required for Workers AI fallback")
        if not api_token:
            raise ValueError("CLOUDFLARE_AI_API_TOKEN is required for Workers AI fallback")
        if not model:
            raise ValueError("CLOUDFLARE_AI_MODEL must not be empty")

        base_url = settings.cloudflare_ai_base_url.rstrip("/") + f"/{account_id}/ai/v1"
        shim = settings.model_copy(
            update={
                "openrouter_api_key": api_token,
                "openrouter_base_url": base_url,
                "openrouter_model": model,
                "openrouter_provider": CLOUDFLARE_PROVIDER,
                "openrouter_allow_fallbacks": False,
                "openrouter_require_parameters": False,
                "openrouter_http_referer": "",
            }
        )
        super().__init__(shim, client=client, observer=observer)
        self.api_model = model
        self.model = f"{model}@{CLOUDFLARE_PROVIDER}"

    def _request_non_streaming(
        self, payload: dict[str, typing.Any]
    ) -> tuple[typing.Any, dict[str, typing.Any]]:
        request = build_cloudflare_request(payload)
        # Never trust a caller-provided model value at this transport boundary.
        # The configured Cloudflare-hosted model is pinned for every fallback call.
        request["model"] = self.api_model
        try:
            response = self.client.post("chat/completions", json=request)
        except httpx.HTTPError as exc:
            raise CloudflareProviderError(
                f"Cloudflare Workers AI network failure: {exc}"
            ) from exc

        if response.status_code == 429:
            raise CloudflareRateLimitError(
                f"Cloudflare Workers AI rate limited (429): {response.text[:1000]}"
            )
        if response.status_code == 408 or response.status_code >= 500:
            raise CloudflareProviderError(
                f"Cloudflare Workers AI unavailable ({response.status_code}): "
                f"{response.text[:1000]}",
                status_code=response.status_code,
            )
        if response.is_error:
            raise CloudflareProviderError(
                f"Cloudflare Workers AI request failed ({response.status_code}): "
                f"{response.text[:1000]}",
                status_code=response.status_code,
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise SummaryError("Cloudflare Workers AI returned invalid JSON") from exc
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SummaryError(
                f"Unexpected Cloudflare Workers AI response shape: {body}"
            ) from exc

        usage = dict(body.get("usage") or {})
        usage["finish_reason"] = choice.get("finish_reason")
        return content, usage

    def _request_streaming(
        self,
        payload: dict[str, typing.Any],
        *,
        stream_label: str,
        request_id: str | None = None,
        audit_context: dict[str, typing.Any] | None = None,
    ) -> tuple[str, dict[str, typing.Any]]:
        # Production source ingestion uses stream=False. Keeping fallback requests
        # non-streaming gives one deterministic structured response for validation.
        content, usage = self._request_non_streaming(payload)
        return str(content), usage
