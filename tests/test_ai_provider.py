from __future__ import annotations

import pytest

from idx_digest.ai_provider import ai_provider_issues, resolve_ai_provider
from idx_digest.config import Settings
from idx_digest.gemini_summarizer import GeminiSummarizer
from idx_digest.summarizer import OpenRouterSummarizer


def test_gemini_runtime_uses_copy_and_correct_provenance() -> None:
    settings = Settings(
        _env_file=None,
        ai_provider="gemini",
        gemini_api_key="gemini-test-key",
        gemini_model="gemini-3.5-flash-lite",
        openrouter_model="legacy-openrouter-model",
        openrouter_provider="legacy-provider",
    )

    runtime = resolve_ai_provider(settings)

    assert runtime.backend == "gemini"
    assert runtime.provider == "google-gemini"
    assert runtime.model == "gemini-3.5-flash-lite"
    assert runtime.summarizer_factory is GeminiSummarizer
    assert runtime.settings is not settings
    assert runtime.settings.openrouter_model == "gemini-3.5-flash-lite"
    assert runtime.settings.openrouter_provider == "google-gemini"
    assert settings.openrouter_model == "legacy-openrouter-model"
    assert settings.openrouter_provider == "legacy-provider"


def test_openrouter_runtime_preserves_pinned_provider() -> None:
    settings = Settings(
        _env_file=None,
        ai_provider="openrouter",
        openrouter_api_key="openrouter-test-key",
        openrouter_model="model-a",
        openrouter_provider="provider-a",
    )

    runtime = resolve_ai_provider(settings)

    assert runtime.backend == "openrouter"
    assert runtime.provider == "provider-a"
    assert runtime.model == "model-a"
    assert runtime.summarizer_factory is OpenRouterSummarizer
    assert runtime.settings is not settings


def test_provider_validation_requires_only_selected_provider_key() -> None:
    gemini = Settings(
        _env_file=None,
        ai_provider="gemini",
        gemini_api_key="gemini-test-key",
        openrouter_api_key="",
    )
    assert ai_provider_issues(gemini, context="test") == []

    missing_gemini = gemini.model_copy(update={"gemini_api_key": ""})
    assert ai_provider_issues(missing_gemini, context="test") == [
        "GEMINI_API_KEY is required for test when AI_PROVIDER=gemini"
    ]

    openrouter = Settings(
        _env_file=None,
        ai_provider="openrouter",
        gemini_api_key="",
        openrouter_api_key="openrouter-test-key",
    )
    assert ai_provider_issues(openrouter, context="test") == []


def test_unknown_provider_is_rejected() -> None:
    settings = Settings(_env_file=None, ai_provider="unknown")
    assert ai_provider_issues(settings, context="test") == [
        "AI_PROVIDER must be either 'gemini' or 'openrouter'"
    ]
    with pytest.raises(ValueError, match="AI_PROVIDER must be either"):
        resolve_ai_provider(settings)
