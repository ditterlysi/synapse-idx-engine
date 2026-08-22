from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .config import Settings
from .gemini_summarizer import GeminiSummarizer
from .summarizer import OpenRouterSummarizer


SummarizerFactory = Callable[[Settings], Any]


@dataclass(frozen=True)
class AIProviderRuntime:
    """Resolved analysis backend plus a compatibility settings snapshot.

    SourceIngestionRunner still writes provenance from the legacy OpenRouter-named
    model/provider fields. The copied settings snapshot keeps that compatibility
    boundary accurate without mutating the caller's Settings object.
    """

    backend: str
    provider: str
    model: str
    settings: Settings
    summarizer_factory: SummarizerFactory


def ai_provider_issues(settings: Settings, *, context: str) -> list[str]:
    provider = settings.ai_provider.strip().lower()
    if provider == "gemini":
        issues: list[str] = []
        if not settings.gemini_api_key.strip():
            issues.append(f"GEMINI_API_KEY is required for {context} when AI_PROVIDER=gemini")
        if not settings.gemini_model.strip():
            issues.append(f"GEMINI_MODEL is required for {context} when AI_PROVIDER=gemini")
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
        return AIProviderRuntime(
            backend="gemini",
            provider="google-gemini",
            model=model,
            settings=runtime_settings,
            summarizer_factory=GeminiSummarizer,
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
