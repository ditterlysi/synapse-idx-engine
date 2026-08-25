from __future__ import annotations

import re
import threading
from typing import Any, Callable

import httpx

from .cloudflare_summarizer import CLOUDFLARE_PROVIDER, CloudflareWorkersAISummarizer
from .config import Settings
from .gemini_summarizer import GeminiRateLimitError, GeminiSummarizer
from .observability import RunObserver
from .summary_schemas import SummaryError

_RETRYABLE_GEMINI_STATUS_RE = re.compile(r"Gemini request failed \((\d{3})\):")


class RetryableAIProviderError(ValueError):
    """Typed primary-provider failure that is safe to route to a fallback."""

    retryable_provider_failure = True

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def is_retryable_provider_failure(error: BaseException) -> bool:
    if bool(getattr(error, "retryable_provider_failure", False)):
        return True
    status_code = getattr(error, "status_code", None)
    if status_code == 429 or status_code == 408:
        return True
    return isinstance(status_code, int) and status_code >= 500


class FallbackAwareGeminiSummarizer(GeminiSummarizer):
    """Gemini adapter that types transport/provider outages without reclassifying quality errors."""

    def _request_non_streaming(self, payload: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        try:
            return super()._request_non_streaming(payload)
        except GeminiRateLimitError:
            raise
        except httpx.HTTPError as exc:
            raise RetryableAIProviderError(
                f"Gemini network failure: {exc}"
            ) from exc
        except SummaryError as exc:
            match = _RETRYABLE_GEMINI_STATUS_RE.search(str(exc))
            if match:
                status_code = int(match.group(1))
                if status_code == 408 or status_code >= 500:
                    raise RetryableAIProviderError(
                        str(exc), status_code=status_code
                    ) from exc
            raise


SummarizerFactory = Callable[..., Any]


class GeminiCloudflareFallbackSummarizer:
    """Sticky Gemini-primary fallback to Cloudflare Workers AI.

    Only typed provider/network/rate-limit failures activate fallback. Structured
    output validation, malformed JSON, and other quality failures remain on the
    primary path and propagate normally instead of silently changing providers.
    Once activated, Workers AI remains active for the rest of this summarizer
    instance so a run does not repeatedly hammer a provider that is already down.
    """

    def __init__(
        self,
        settings: Settings,
        observer: RunObserver | None = None,
        *,
        primary_factory: SummarizerFactory = FallbackAwareGeminiSummarizer,
        fallback_factory: SummarizerFactory = CloudflareWorkersAISummarizer,
    ) -> None:
        self.settings = settings
        self.observer = observer
        self.primary = primary_factory(settings, observer=observer)
        self.fallback = fallback_factory(settings, observer=observer)
        self._fallback_lock = threading.RLock()
        self._fallback_active = False
        self._fallback_count = 0
        self._fallback_reason: str | None = None

        for attribute in (
            "document_prompt_version",
            "public_expose_document_prompt_version",
            "announcement_prompt_version",
            "company_prompt_version",
        ):
            if hasattr(self.primary, attribute):
                setattr(self, attribute, getattr(self.primary, attribute))

    @property
    def fallback_used(self) -> bool:
        with self._fallback_lock:
            return self._fallback_active

    @property
    def fallback_count(self) -> int:
        with self._fallback_lock:
            return self._fallback_count

    @property
    def fallback_reason(self) -> str | None:
        with self._fallback_lock:
            return self._fallback_reason

    @property
    def effective_provider(self) -> str:
        return CLOUDFLARE_PROVIDER if self.fallback_used else "google-gemini"

    @property
    def effective_model(self) -> str:
        target = self.fallback if self.fallback_used else self.primary
        return str(getattr(target, "api_model", ""))

    @property
    def provider_metrics(self) -> dict[str, Any]:
        return {
            "fallback_used": self.fallback_used,
            "fallback_count": self.fallback_count,
            "fallback_reason": self.fallback_reason,
            "primary": getattr(self.primary, "provider_metrics", {}),
            "fallback": getattr(self.fallback, "provider_metrics", {}),
        }

    def _activate_fallback(self, error: BaseException) -> None:
        with self._fallback_lock:
            if self._fallback_active:
                return
            self._fallback_active = True
            self._fallback_count += 1
            self._fallback_reason = f"{type(error).__name__}: {error}"

        if self.observer:
            self.observer.event(
                "llm",
                "AI provider fallback activated",
                level="WARNING",
                always=True,
                primary_provider="google-gemini",
                primary_model=getattr(self.primary, "api_model", None),
                fallback_provider=CLOUDFLARE_PROVIDER,
                fallback_model=getattr(self.fallback, "api_model", None),
                error_type=type(error).__name__,
                error=str(error)[:500],
            )

    def _call(self, method_name: str, /, *args: Any, **kwargs: Any) -> Any:
        with self._fallback_lock:
            use_fallback = self._fallback_active
        if use_fallback:
            return getattr(self.fallback, method_name)(*args, **kwargs)

        try:
            return getattr(self.primary, method_name)(*args, **kwargs)
        except Exception as exc:
            if not is_retryable_provider_failure(exc):
                raise
            self._activate_fallback(exc)
            return getattr(self.fallback, method_name)(*args, **kwargs)

    def summarize_document(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("summarize_document", *args, **kwargs)

    def summarize_announcement(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("summarize_announcement", *args, **kwargs)

    def summarize_routine_announcement(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("summarize_routine_announcement", *args, **kwargs)

    def summarize_company_window(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("summarize_company_window", *args, **kwargs)

    @staticmethod
    def is_valid_document_summary(payload: dict[str, Any] | None) -> bool:
        return GeminiSummarizer.is_valid_document_summary(payload)

    @staticmethod
    def is_valid_announcement_summary(payload: dict[str, Any] | None) -> bool:
        return GeminiSummarizer.is_valid_announcement_summary(payload)

    @staticmethod
    def is_valid_company_summary(payload: dict[str, Any] | None) -> bool:
        return GeminiSummarizer.is_valid_company_summary(payload)

    def close(self) -> None:
        primary_error: Exception | None = None
        try:
            self.primary.close()
        except Exception as exc:  # pragma: no cover - defensive cleanup
            primary_error = exc
        try:
            self.fallback.close()
        except Exception:
            if primary_error is None:
                raise
        if primary_error is not None:
            raise primary_error
