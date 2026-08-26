from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from idx_digest.ai_fallback import (
    GeminiCloudflareFallbackSummarizer,
    RetryableAIProviderError,
)
from idx_digest.cloudflare_summarizer import (
    CLOUDFLARE_PROVIDER,
    CloudflareRateLimitError,
    CloudflareWorkersAISummarizer,
    build_cloudflare_request,
)
from idx_digest.config import Settings
from idx_digest.extractors import ExtractionResult
from idx_digest.source_contract import SourceAttachment, SourceDisclosure, SourceWindowResult
from idx_digest.source_ingestion import SourceIngestionRunner
from idx_digest.summary_schemas import SummaryError


def _settings(tmp_path=None) -> Settings:
    data_dir = (tmp_path / "data") if tmp_path is not None else "./data-test-ai-fallback"
    return Settings(
        _env_file=None,
        data_dir=data_dir,
        gemini_api_key="gemini-test-key",
        gemini_model="gemini-3.5-flash-lite",
        cloudflare_ai_account_id="account-id",
        cloudflare_ai_api_token="cf-test-token",
        cloudflare_ai_model="@cf/zai-org/glm-4.7-flash",
        synapse_internal_base_url="https://synapse.example",
        synapse_ingestion_secret="test-secret",
        synapse_daily_request_delay_seconds=0.5,
        synapse_daily_request_jitter_seconds=0.0,
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
        "temperature": 0.1,
        "provider": {"only": ["google-gemini"]},
        "reasoning": {"enabled": False},
    }


def test_cloudflare_request_removes_openrouter_routing_and_flattens_schema() -> None:
    request = build_cloudflare_request(_payload())

    assert "provider" not in request
    assert "reasoning" not in request
    assert request["model"] == "gemini-3.5-flash-lite"
    response_format = request["response_format"]
    assert isinstance(response_format, dict)
    assert response_format["type"] == "json_schema"
    schema = response_format["json_schema"]
    assert isinstance(schema, dict)
    assert schema["required"] == ["summary"]
    assert "name" not in schema
    assert "strict" not in schema


def test_cloudflare_openai_response_maps_to_summarizer_contract(tmp_path) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": '{"summary":"ok"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
            },
        )

    client = httpx.Client(
        base_url="https://api.cloudflare.com/client/v4/accounts/account-id/ai/v1/",
        headers={"Authorization": "Bearer cf-test-token"},
        transport=httpx.MockTransport(handler),
    )
    summarizer = CloudflareWorkersAISummarizer(_settings(tmp_path), client=client)

    content, usage = summarizer._request_non_streaming(_payload())

    assert content == '{"summary":"ok"}'
    assert captured["path"].endswith("/ai/v1/chat/completions")
    assert captured["authorization"] == "Bearer cf-test-token"
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 4
    assert usage["total_tokens"] == 14
    assert usage["finish_reason"] == "stop"
    summarizer.close()
    client.close()


class _StubProvider:
    def __init__(self, settings, observer=None, *, result=None, error=None, model="stub-model"):
        self.settings = settings
        self.observer = observer
        self.result = result
        self.error = error
        self.api_model = model
        self.calls: list[str] = []
        self.closed = False
        self.announcement_prompt_version = "announcement-v3"

    def _run(self, method: str):
        self.calls.append(method)
        if self.error is not None:
            raise self.error
        return self.result

    def summarize_document(self, **_kwargs):
        return self._run("document")

    def summarize_announcement(self, **_kwargs):
        return self._run("announcement")

    def summarize_routine_announcement(self, **_kwargs):
        return self._run("routine")

    def summarize_company_window(self, **_kwargs):
        return self._run("company")

    def close(self):
        self.closed = True


def _wrapper(settings, *, primary_error=None, fallback_error=None):
    primary_holder: dict[str, _StubProvider] = {}
    fallback_holder: dict[str, _StubProvider] = {}

    def primary_factory(runtime_settings, observer=None):
        provider = _StubProvider(
            runtime_settings,
            observer,
            result={"provider": "gemini"},
            error=primary_error,
            model="gemini-3.5-flash-lite",
        )
        primary_holder["provider"] = provider
        return provider

    def fallback_factory(runtime_settings, observer=None):
        provider = _StubProvider(
            runtime_settings,
            observer,
            result={"provider": "cloudflare"},
            error=fallback_error,
            model="@cf/zai-org/glm-4.7-flash",
        )
        fallback_holder["provider"] = provider
        return provider

    wrapper = GeminiCloudflareFallbackSummarizer(
        settings,
        primary_factory=primary_factory,
        fallback_factory=fallback_factory,
    )
    return wrapper, primary_holder["provider"], fallback_holder["provider"]


def test_primary_success_does_not_call_cloudflare(tmp_path) -> None:
    settings = _settings(tmp_path)
    wrapper, primary, fallback = _wrapper(settings)

    result = wrapper.summarize_document()

    assert result == {"provider": "gemini"}
    assert primary.calls == ["document"]
    assert fallback.calls == []
    assert wrapper.fallback_used is False
    assert settings.openrouter_provider != CLOUDFLARE_PROVIDER
    wrapper.close()


def test_retryable_primary_failure_switches_once_and_stays_on_cloudflare(tmp_path) -> None:
    settings = _settings(tmp_path)
    wrapper, primary, fallback = _wrapper(
        settings,
        primary_error=RetryableAIProviderError("Gemini unavailable", status_code=503),
    )

    first = wrapper.summarize_document()
    second = wrapper.summarize_announcement()

    assert first == {"provider": "cloudflare"}
    assert second == {"provider": "cloudflare"}
    assert primary.calls == ["document"]
    assert fallback.calls == ["document", "announcement"]
    assert wrapper.fallback_used is True
    assert wrapper.fallback_count == 1
    assert wrapper.effective_provider == CLOUDFLARE_PROVIDER
    assert wrapper.effective_model == "@cf/zai-org/glm-4.7-flash"
    assert settings.openrouter_provider == CLOUDFLARE_PROVIDER
    assert settings.openrouter_model == "@cf/zai-org/glm-4.7-flash"
    wrapper.close()


def test_quality_or_schema_failure_does_not_fallback(tmp_path) -> None:
    settings = _settings(tmp_path)
    wrapper, primary, fallback = _wrapper(
        settings,
        primary_error=SummaryError("structured output failed local schema validation"),
    )

    with pytest.raises(SummaryError, match="schema validation"):
        wrapper.summarize_document()

    assert primary.calls == ["document"]
    assert fallback.calls == []
    assert wrapper.fallback_used is False
    wrapper.close()


def test_cloudflare_rate_limit_propagates_for_existing_partial_defer_logic(tmp_path) -> None:
    settings = _settings(tmp_path)
    wrapper, primary, fallback = _wrapper(
        settings,
        primary_error=RetryableAIProviderError("Gemini quota", status_code=429),
        fallback_error=CloudflareRateLimitError("Cloudflare free quota exhausted"),
    )

    with pytest.raises(CloudflareRateLimitError) as exc_info:
        wrapper.summarize_document()

    assert exc_info.value.status_code == 429
    assert primary.calls == ["document"]
    assert fallback.calls == ["document"]
    assert wrapper.fallback_used is True
    wrapper.close()


RUN_ID = "986b5105-f894-4a69-a733-a4e1bcf2cc62"
DISCLOSURE_ID = "70f28dd7-09f2-4936-92c8-01c22d1a1e95"


class _Source:
    source_id = "manual-manifest"

    def __init__(self, result):
        self.result = result

    def collect_window(self, *, start_at, end_at):
        assert start_at == self.result.requested_start
        assert end_at == self.result.requested_end
        return self.result


class _Client:
    latest = None

    def __init__(self, settings):
        self.settings = settings
        self.analysis_requests = []
        self.final_error_code = None
        _Client.latest = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def create_run(self, _request):
        return SimpleNamespace(run_id=RUN_ID)

    def upsert_disclosures(self, request):
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    idx_announcement_id=item.idx_announcement_id,
                    disclosure_id=DISCLOSURE_ID,
                    created=True,
                    processing_status="DISCOVERED",
                )
                for item in request.items
            ]
        )

    def upsert_files(self, _disclosure_id, request):
        return SimpleNamespace(files=[] if not request.files else [SimpleNamespace()] * len(request.files))

    def update_processing_status(self, disclosure_id, request):
        return SimpleNamespace(disclosure_id=disclosure_id, processing_status=request.processing_status)

    def commit_analysis(self, _disclosure_id, request):
        self.analysis_requests.append(request)
        return SimpleNamespace(analysis_id="analysis-id", promoted=True)

    def update_run(self, run_id, request):
        self.final_error_code = request.error_code
        return SimpleNamespace(run_id=run_id, status=request.status, completed_at=request.completed_at)

    def commit_coverage(self, _request):
        raise AssertionError("non-authoritative test source must not commit coverage")


def _extractor(_path, _content_type, _settings):
    return ExtractionResult(text="Synthetic disclosure text", method="text")


def _announcement_summary(announcement):
    return {
        "ticker": announcement["ticker"],
        "announcement_id": announcement["id2"],
        "announced_at": announcement["announced_at"],
        "title": announcement["title"],
        "executive_summary": "Perusahaan menyampaikan informasi material.",
        "category": "other",
        "material_facts": ["Informasi material disampaikan."],
        "financial_figures": [],
        "corporate_actions": [],
        "expansion_projects": [],
        "management_or_control_changes": [],
        "capital_structure_events": [],
        "listing_or_regulatory_events": [],
        "analytical_scenarios": [],
        "dates_and_deadlines": [],
        "risks_or_uncertainties": [],
        "possible_investor_relevance": [],
        "source_files": [],
        "limitations": [],
    }


def test_source_ingestion_commits_cloudflare_provenance_once_after_fallback(tmp_path) -> None:
    settings = _settings(tmp_path).model_copy(
        update={
            "openrouter_model": "gemini-3.5-flash-lite",
            "openrouter_provider": "google-gemini",
        }
    )
    primary_holder = {}
    fallback_holder = {}

    class Primary(_StubProvider):
        def summarize_document(self, **_kwargs):
            self.calls.append("document")
            raise RetryableAIProviderError("Gemini unavailable", status_code=503)

    class Fallback(_StubProvider):
        def summarize_document(self, **_kwargs):
            self.calls.append("document")
            return {"summary": "fallback document", "chunk_count": 1}

        def summarize_announcement(self, *, announcement, **_kwargs):
            self.calls.append("announcement")
            return _announcement_summary(announcement)

    def primary_factory(runtime_settings, observer=None):
        provider = Primary(runtime_settings, observer, model="gemini-3.5-flash-lite")
        primary_holder["provider"] = provider
        return provider

    def fallback_factory(runtime_settings, observer=None):
        provider = Fallback(runtime_settings, observer, model="@cf/zai-org/glm-4.7-flash")
        fallback_holder["provider"] = provider
        return provider

    def summarizer_factory(runtime_settings):
        return GeminiCloudflareFallbackSummarizer(
            runtime_settings,
            primary_factory=primary_factory,
            fallback_factory=fallback_factory,
        )

    start = datetime.fromisoformat("2026-08-21T09:00:00+07:00")
    end = datetime.fromisoformat("2026-08-21T11:00:00+07:00")
    attachment_path = tmp_path / "disclosure.txt"
    attachment_path.write_text("Synthetic disclosure body", encoding="utf-8")
    disclosure = SourceDisclosure(
        external_id="manual-example-1",
        ticker="BBRI",
        announced_at=datetime.fromisoformat("2026-08-21T10:00:00+07:00"),
        title="Informasi material",
        source_url="https://example.com/disclosure/1",
        attachments=(
            SourceAttachment(
                filename="disclosure.txt",
                local_path=attachment_path,
                content_type="text/plain",
            ),
        ),
    )
    source = _Source(
        SourceWindowResult(
            source_id="manual-manifest",
            requested_start=start,
            requested_end=end,
            disclosures=(disclosure,),
            complete=False,
            diagnostics={"networkAccess": False},
        )
    )

    result = SourceIngestionRunner(
        settings,
        source,
        client_factory=_Client,
        summarizer_factory=summarizer_factory,
        extractor=_extractor,
        allow_coverage_commit=False,
        require_external_id_prefix="manual-",
    ).run_window(start_at=start, end_at=end)

    client = _Client.latest
    assert client is not None
    assert result.processing_ok is True
    assert result.publish.analyses_completed == 1
    assert len(client.analysis_requests) == 1
    request = client.analysis_requests[0]
    assert request.provider == CLOUDFLARE_PROVIDER
    assert request.model == "@cf/zai-org/glm-4.7-flash"
    assert primary_holder["provider"].calls == ["document"]
    assert fallback_holder["provider"].calls == ["document", "announcement"]
