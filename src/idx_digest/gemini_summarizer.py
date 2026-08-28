from __future__ import annotations

import re
import threading
import time
import typing

import httpx

from .config import Settings
from .observability import RunObserver
from .provider_gate import ProviderLease
from .summarizer import OpenRouterSummarizer
from .summary_schemas import SummaryError

_RETRY_IN_RE = re.compile(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE)
_DURATION_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)s\s*$", re.IGNORECASE)
_DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
_MAX_RATE_LIMIT_COOLDOWN_SECONDS = 120.0
_DAILY_AI_SESSION_MAX_SECONDS = 2400.0
_DAILY_AI_REQUEST_MAX_SECONDS = 120.0
_DAILY_AI_DEADLINE_FLOOR_SECONDS = 1.0


class GeminiRateLimitError(ValueError):
    """Terminal Gemini 429 after one provider-directed cooldown retry.

    ValueError is intentional: the inherited structured-output retry loop retries
    SummaryError/JSON failures, but a second 429 should stop this disclosure and
    let a later ingestion pass resume it instead of hammering the same quota.
    """

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        super().__init__(message)
        self.status_code = 429
        self.retry_after_seconds = retry_after_seconds


class GeminiRunDeadlineError(ValueError):
    """Intentional daily-run deadline, distinct from an upstream provider failure."""

    run_deadline_exceeded = True
    retryable_provider_failure = False


def _message_text(content: typing.Any) -> str:
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


def build_gemini_request(openrouter_payload: dict[str, typing.Any]) -> dict[str, typing.Any]:
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
    request: dict[str, typing.Any] = {
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


def _bounded_retry_seconds(value: float | None) -> float | None:
    if value is None:
        return None
    return min(max(float(value), 0.0), _MAX_RATE_LIMIT_COOLDOWN_SECONDS)


def _retry_after_seconds(response: httpx.Response) -> float:
    """Resolve Gemini's requested cooldown from headers/body with a safe fallback."""

    header = response.headers.get("Retry-After")
    if header:
        try:
            parsed = _bounded_retry_seconds(float(header.strip()))
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed

    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        details = error.get("details") if isinstance(error, dict) else None
        if isinstance(details, list):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                if not str(detail.get("@type") or "").endswith("RetryInfo"):
                    continue
                raw_delay = str(detail.get("retryDelay") or "")
                match = _DURATION_RE.match(raw_delay)
                if match:
                    parsed = _bounded_retry_seconds(float(match.group(1)))
                    if parsed is not None:
                        return parsed

    match = _RETRY_IN_RE.search(response.text or "")
    if match:
        parsed = _bounded_retry_seconds(float(match.group(1)))
        if parsed is not None:
            return parsed
    return _DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS


class GeminiSummarizer(OpenRouterSummarizer):
    """Direct Gemini Developer API transport with inherited Synapse validation.

    OpenRouterSummarizer remains available for compatibility. We reuse its prompt
    rendering, audit persistence, concurrency gates, and strict post-response
    schema validation while translating the HTTP request/response shapes.

    After the first HTTP 429 in a run, Gemini requests are serialized and share a
    provider-directed cooldown. A disclosure gets one cooldown retry. If Gemini
    still returns 429, processing stops for that disclosure so a later ingestion
    pass can resume it without repeatedly burning quota in the same window.

    Scheduled daily runs additionally cap the Gemini phase. This prevents one
    multi-document disclosure from occupying the process until the outer GitHub
    Actions timeout kills it before Synapse run state/checkpoint can be finalized.
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
        self._gemini_rate_limit_lock = threading.RLock()
        self._gemini_rate_limit_serial = threading.Lock()
        self._gemini_rate_limit_until = 0.0
        self._gemini_serialize_after_429 = False
        self._run_deadline_at: float | None = None
        if settings.synapse_daily_enabled:
            self._run_deadline_at = time.monotonic() + min(
                _DAILY_AI_SESSION_MAX_SECONDS,
                max(
                    60.0,
                    float(settings.synapse_daily_max_run_seconds) / 2.0,
                ),
            )

    def _remaining_run_seconds(self) -> float | None:
        deadline = getattr(self, "_run_deadline_at", None)
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= _DAILY_AI_DEADLINE_FLOOR_SECONDS:
            raise GeminiRunDeadlineError(
                "daily Gemini analysis deadline reached; remaining disclosures are deferred"
            )
        return remaining

    def _request_timeout_seconds(self) -> float | None:
        remaining = self._remaining_run_seconds()
        if remaining is None:
            return None
        timeout_seconds = min(
            _DAILY_AI_REQUEST_MAX_SECONDS,
            remaining - _DAILY_AI_DEADLINE_FLOOR_SECONDS,
        )
        if timeout_seconds <= _DAILY_AI_DEADLINE_FLOOR_SECONDS:
            raise GeminiRunDeadlineError(
                "daily Gemini analysis deadline reached before a new request could start"
            )
        return timeout_seconds

    def _completion_once(
        self,
        user_prompt: str,
        *,
        schema_name: str,
        schema: dict[str, typing.Any],
        max_tokens: int,
        stream: bool | None = None,
        audit_context: dict[str, typing.Any] | None = None,
        attempt: int = 1,
    ) -> dict[str, typing.Any]:
        """Release the provider slot when Gemini raises typed ValueErrors.

        The inherited completion path already releases slots for httpx and
        SummaryError failures. Gemini intentionally raises typed ValueErrors for
        terminal rate limits and the daily deadline so those errors bypass the
        structured-output retry loop. Release the acquired provider slot before
        propagating either typed error to the ingestion circuit breaker.
        """

        try:
            return super()._completion_once(
                user_prompt,
                schema_name=schema_name,
                schema=schema,
                max_tokens=max_tokens,
                stream=stream,
                audit_context=audit_context,
                attempt=attempt,
            )
        except (GeminiRateLimitError, GeminiRunDeadlineError) as exc:
            metrics = self.provider_gate.metrics
            current_limit = int(
                metrics.get("current_limit")
                or metrics.get("configured_max")
                or self.settings.llm_concurrency
                or 1
            )
            self.provider_gate.record_failure(
                ProviderLease(
                    waited_seconds=0.0,
                    limit_at_acquire=max(1, current_limit),
                    request_class="priority",
                ),
                status_code=getattr(exc, "status_code", None),
                transient=isinstance(exc, GeminiRateLimitError),
                error=str(exc),
            )
            raise

    def _ensure_rate_limit_state(self) -> None:
        # Some focused unit tests instantiate the adapter with object.__new__.
        if not hasattr(self, "_gemini_rate_limit_lock"):
            self._gemini_rate_limit_lock = threading.RLock()
            self._gemini_rate_limit_serial = threading.Lock()
            self._gemini_rate_limit_until = 0.0
            self._gemini_serialize_after_429 = False

    def _activate_rate_limit_cooldown(self, seconds: float) -> None:
        self._ensure_rate_limit_state()
        delay = _bounded_retry_seconds(seconds)
        if delay is None:
            delay = _DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
        remaining_run = self._remaining_run_seconds()
        if remaining_run is not None and delay >= remaining_run - _DAILY_AI_DEADLINE_FLOOR_SECONDS:
            raise GeminiRunDeadlineError(
                "Gemini cooldown would exceed the daily analysis deadline; retry next run"
            )
        with self._gemini_rate_limit_lock:
            self._gemini_serialize_after_429 = True
            self._gemini_rate_limit_until = max(
                self._gemini_rate_limit_until,
                time.monotonic() + delay,
            )
        observer = getattr(self, "observer", None)
        if observer:
            observer.event(
                "llm",
                "Gemini 429 cooldown activated",
                level="WARNING",
                always=True,
                provider="google-gemini",
                model=self.api_model,
                retry_after_seconds=f"{delay:.2f}",
                serialized_after_rate_limit=True,
            )

    def _wait_for_rate_limit_cooldown(self) -> None:
        self._ensure_rate_limit_state()
        with self._gemini_rate_limit_lock:
            remaining = max(0.0, self._gemini_rate_limit_until - time.monotonic())
        if remaining <= 0:
            self._remaining_run_seconds()
            return
        remaining_run = self._remaining_run_seconds()
        if remaining_run is not None and remaining >= remaining_run - _DAILY_AI_DEADLINE_FLOOR_SECONDS:
            raise GeminiRunDeadlineError(
                "Gemini cooldown would exceed the daily analysis deadline; retry next run"
            )
        time.sleep(remaining)
        self._remaining_run_seconds()

    def _post_generate_content(self, request: dict[str, typing.Any]) -> httpx.Response:
        self._ensure_rate_limit_state()
        with self._gemini_rate_limit_lock:
            serialize = self._gemini_serialize_after_429

        def send() -> httpx.Response:
            self._wait_for_rate_limit_cooldown()
            timeout_seconds = self._request_timeout_seconds()
            try:
                if timeout_seconds is None:
                    return self.client.post(
                        f"models/{self.api_model}:generateContent",
                        json=request,
                    )
                return self.client.post(
                    f"models/{self.api_model}:generateContent",
                    json=request,
                    timeout=httpx.Timeout(
                        timeout_seconds,
                        connect=min(20.0, timeout_seconds),
                    ),
                )
            except httpx.TimeoutException as exc:
                try:
                    self._remaining_run_seconds()
                except GeminiRunDeadlineError as deadline_exc:
                    raise deadline_exc from exc
                raise

        if serialize:
            with self._gemini_rate_limit_serial:
                return send()
        return send()

    def _request_non_streaming(
        self, payload: dict[str, typing.Any]
    ) -> tuple[typing.Any, dict[str, typing.Any]]:
        request = build_gemini_request(payload)
        response: httpx.Response | None = None
        for attempt in range(2):
            self._remaining_run_seconds()
            response = self._post_generate_content(request)
            if response.status_code != 429:
                break
            retry_after = _retry_after_seconds(response)
            self._activate_rate_limit_cooldown(retry_after)
            if attempt == 0:
                continue
            raise GeminiRateLimitError(
                f"Gemini rate limited (429) after cooldown; retry later: {response.text[:1000]}",
                retry_after_seconds=retry_after,
            )

        if response is None:
            raise SummaryError("Gemini request was not sent")
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
        payload: dict[str, typing.Any],
        *,
        stream_label: str,
        request_id: str | None = None,
        audit_context: dict[str, typing.Any] | None = None,
    ) -> tuple[str, dict[str, typing.Any]]:
        # Source-neutral Synapse ingestion does not require token streaming. One
        # GenerateContent call preserves the same validation/audit path.
        content, usage = self._request_non_streaming(payload)
        return str(content), usage
