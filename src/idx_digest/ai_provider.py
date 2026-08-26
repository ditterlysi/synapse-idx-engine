from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .ai_fallback import GeminiCloudflareFallbackSummarizer
from .cloudflare_summarizer import CLOUDFLARE_PROVIDER
from .config import Settings
from .gemini_summarizer import GeminiSummarizer
from .summarizer import OpenRouterSummarizer

SummarizerFactory = Callable[[Settings], Any]


@dataclass(frozen=True)
class AIProviderRuntime:
    """Resolved analysis backend plus a compatibility settings snapshot.

    SourceIngestionRunner still writes primary provenance from legacy
    OpenRouter-named settings. Fallback-aware summarizers can override the final
    analysis provider/model at commit time when a fallback actually supplies the
    validated result.
    """

    backend: str
    provider: str
    model: str
    settings: Settings
    summarizer_factory: SummarizerFactory
    fallback_provider: str | None = None
    fallback_model: str | None = None


def _cloudflare_fallback_issues(settings: Settings, *, context: str) -> list[str]:
    account_id = settings.cloudflare_ai_account_id.strip()
    api_token = settings.cloudflare_ai_api_token.strip()
    if bool(account_id) != bool(api_token):
        return [
            "CLOUDFLARE_AI_ACCOUNT_ID and CLOUDFLARE_AI_API_TOKEN must both be set "
            f"to enable Gemini fallback for {context}"
        ]
    if account_id and api_token and not settings.cloudflare_ai_model.strip():
        return [f"CLOUDFLARE_AI_MODEL is required for {context} when Gemini fallback is enabled"]
    return []


def ai_provider_issues(settings: Settings, *, context: str) -> list[str]:
    provider = settings.ai_provider.strip().lower()
    if provider == "gemini":
        issues: list[str] = []
        if not settings.gemini_api_key.strip():
            issues.append(f"GEMINI_API_KEY is required for {context} when AI_PROVIDER=gemini")
        if not settings.gemini_model.strip():
            issues.append(f"GEMINI_MODEL is required for {context} when AI_PROVIDER=gemini")
        issues.extend(_cloudflare_fallback_issues(settings, context=context))
        return issues
    if provider == "openrouter":
        issues = []
        if not settings.openrouter_api_key.strip():
            issues.append(f"OPENROUTER_API_KEY is required for {context} when AI_PROVIDER=openrouter")
        if not settings.openrouter_model.strip():
            issues.append(f"OPENROUTER_MODEL is required for {context} when AI_PROVIDER=openrouter")
        if not settings.openrouter_provider.strip():
            issues.append(f"OPENROUTER_PROVIDER is required for {context} when AI_PROVIDER=openrouter")
        return issues
    return ["AI_PROVIDER must be either 'gemini' or 'openrouter'"]


def resolve_ai_provider(settings: Settings) -> AIProviderRuntime:
    """Resolve the configured backend without changing caller-owned settings."""

    backend = settings.ai_provider.strip().lower()
    if backend == "gemini":
        model = settings.gemini_model.strip()
        if not model:
            raise ValueError("GEMINI_MODEL must not be empty when AI_PROVIDER=gemini")
        runtime_settings = settings.model_copy(
            update={
                "openrouter_model": model,
                "openrouter_provider": "google-gemini",
            }
        )
        fallback_enabled = runtime_settings.cloudflare_ai_configured
        fallback_model = runtime_settings.cloudflare_ai_model.strip() if fallback_enabled else None
        if fallback_enabled and not fallback_model:
            raise ValueError("CLOUDFLARE_AI_MODEL must not be empty when Gemini fallback is enabled")
        return AIProviderRuntime(
            backend="gemini",
            provider="google-gemini",
            model=model,
            settings=runtime_settings,
            summarizer_factory=(
                GeminiCloudflareFallbackSummarizer if fallback_enabled else GeminiSummarizer
            ),
            fallback_provider=CLOUDFLARE_PROVIDER if fallback_enabled else None,
            fallback_model=fallback_model,
        )

    if backend == "openrouter":
        model = settings.openrouter_model.strip()
        provider = settings.openrouter_provider.strip()
        if not model:
            raise ValueError("OPENROUTER_MODEL must not be empty when AI_PROVIDER=openrouter")
        if not provider:
            raise ValueError("OPENROUTER_PROVIDER must not be empty when AI_PROVIDER=openrouter")
        return AIProviderRuntime(
            backend="openrouter",
            provider=provider,
            model=model,
            settings=settings.model_copy(),
            summarizer_factory=OpenRouterSummarizer,
        )

    raise ValueError("AI_PROVIDER must be either 'gemini' or 'openrouter'")
