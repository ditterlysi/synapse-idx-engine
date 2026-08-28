from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from idx_digest.config import Settings
from idx_digest.extractors import ExtractionResult
from idx_digest.source_contract import (
    SourceAttachment,
    SourceContractError,
    SourceDisclosure,
    SourceWindowResult,
)
from idx_digest.source_ingestion import SourceIngestionRunner


RUN_ID = "986b5105-f894-4a69-a733-a4e1bcf2cc62"
DISCLOSURE_ID = "70f28dd7-09f2-4936-92c8-01c22d1a1e95"
FILE_ID = "a15b1438-61b0-4bd9-9cf0-5a831d3f531f"


def _settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        synapse_internal_base_url="https://synapse.example",
        synapse_ingestion_secret="test-secret",
        synapse_daily_request_delay_seconds=0.5,
        synapse_daily_request_jitter_seconds=0.0,
    )


def _summary(announcement: dict[str, object]) -> dict[str, object]:
    return {
        "ticker": str(announcement["ticker"]),
        "announcement_id": str(announcement["id2"]),
        "announced_at": str(announcement["announced_at"]),
        "title": str(announcement["title"]),
        "executive_summary": "Emiten menjelaskan rencana ekspansi dan belanja modal.",
        "category": "expansion",
        "material_facts": ["Perusahaan menyampaikan rencana ekspansi."],
        "financial_figures": [{"metric": "Capex", "value": "Rp1 triliun", "period": "2026"}],
        "corporate_actions": [],
        "expansion_projects": ["Belanja modal untuk proyek baru."],
        "management_or_control_changes": [],
        "capital_structure_events": [],
        "listing_or_regulatory_events": [],
        "analytical_scenarios": [
            {
                "classification": "analyst_hypothesis",
                "topic": "Execution",
                "analysis": "Timing remains subject to execution.",
                "basis": ["Management plan"],
                "assumptions": ["Project proceeds"],
                "confidence": "medium",
                "caveats": ["No completion date confirmed"],
            }
        ],
        "dates_and_deadlines": [{"date": "2026-12-31", "event": "Target period"}],
        "risks_or_uncertainties": ["Execution risk."],
        "possible_investor_relevance": ["The plan may affect future capacity."],
        "source_files": [],
        "limitations": [],
    }


class FakeSource:
    source_id = "manual-manifest"

    def __init__(self, result: SourceWindowResult):
        self.result = result
        self.calls = 0

    def collect_window(self, *, start_at: datetime, end_at: datetime) -> SourceWindowResult:
        self.calls += 1
        assert start_at == self.result.requested_start
        assert end_at == self.result.requested_end
        return self.result


class FakeSummarizer:
    latest: "FakeSummarizer | None" = None
    announcement_prompt_version = "announcement-v3"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.closed = False
        self.documents: list[dict[str, object]] = []
        FakeSummarizer.latest = self

    def summarize_document(self, **kwargs):
        self.documents.append(kwargs)
        return {"summary": "Synthetic document summary", "chunk_count": 1}

    def summarize_announcement(self, *, announcement, documents, stream=False):
        assert stream is False
        assert documents
        return _summary(announcement)

    def close(self):
        self.closed = True


class FakeClient:
    latest: "FakeClient | None" = None

    def __init__(self, settings: Settings):
        self.settings = settings
        self.calls: list[tuple[str, object]] = []
        self.final_run_status: str | None = None
        self.final_error_code: str | None = None
        self.coverage_committed = False
        self.analysis = None
        FakeClient.latest = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def create_run(self, request):
        self.calls.append(("create_run", request))
        return SimpleNamespace(run_id=RUN_ID)

    def upsert_disclosures(self, request):
        self.calls.append(("upsert_disclosures", request))
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    idx_announcement_id=item.idx_announcement_id,
                    disclosure_id=DISCLOSURE_ID,
                    created=True,
                )
                for item in request.items
            ]
        )

    def upsert_files(self, disclosure_id, request):
        self.calls.append(("upsert_files", request))
        return SimpleNamespace(
            files=[SimpleNamespace(file_id=FILE_ID, source_url=item.source_url) for item in request.files]
        )

    def update_processing_status(self, disclosure_id, request):
        self.calls.append(("status", request.processing_status))
        return SimpleNamespace(disclosure_id=disclosure_id, processing_status=request.processing_status)

    def commit_analysis(self, disclosure_id, request):
        self.calls.append(("analysis", request))
        self.analysis = request.analysis
        return SimpleNamespace(analysis_id="analysis-id", promoted=True)

    def update_run(self, run_id, request):
        self.calls.append(("update_run", request))
        if request.status:
            self.final_run_status = request.status
            self.final_error_code = request.error_code
        return SimpleNamespace(run_id=run_id, status=request.status, completed_at=request.completed_at)

    def commit_coverage(self, request):
        self.calls.append(("coverage", request))
        self.coverage_committed = True
        return SimpleNamespace(coverage_id="coverage-id", created=True)


def _extractor(path, content_type, settings):
    assert path.exists()
    assert content_type == "text/plain"
    assert settings.app_timezone == "Asia/Jakarta"
    return ExtractionResult(text="Synthetic extracted disclosure text", method="text")


def _window(tmp_path, *, source_url: str | None = None, complete: bool = False) -> SourceWindowResult:
    start = datetime.fromisoformat("2026-08-21T09:00:00+07:00")
    end = datetime.fromisoformat("2026-08-21T11:00:00+07:00")
    attachment_path = tmp_path / "disclosure.txt"
    attachment_path.write_text("Synthetic disclosure body", encoding="utf-8")
    attachment = SourceAttachment(
        filename="disclosure.txt",
        local_path=attachment_path,
        source_url=source_url,
        content_type="text/plain",
    )
    disclosure = SourceDisclosure(
        external_id="manual-example-1",
        ticker="BBRI",
        announced_at=datetime.fromisoformat("2026-08-21T10:00:00+07:00"),
        title="Rencana belanja modal dan ekspansi",
        subject="Ekspansi",
        disclosure_type="MATERIAL_INFORMATION",
        source_url="https://example.com/disclosures/manual-example-1",
        attachments=(attachment,),
    )
    return SourceWindowResult(
        source_id="manual-manifest",
        requested_start=start,
        requested_end=end,
        disclosures=(disclosure,),
        complete=complete,
        coverage_start=start if complete else None,
        coverage_end=end if complete else None,
        diagnostics={"networkAccess": False},
    )


def _runner(settings, source, *, allow_coverage_commit=False, summarizer_factory=FakeSummarizer):
    return SourceIngestionRunner(
        settings,
        source,
        client_factory=FakeClient,
        summarizer_factory=summarizer_factory,
        extractor=_extractor,
        allow_coverage_commit=allow_coverage_commit,
        require_external_id_prefix="manual-",
    )


def test_manual_source_processes_without_fabricating_attachment_url(tmp_path) -> None:
    settings = _settings(tmp_path)
    source = FakeSource(_window(tmp_path))
    result = _runner(settings, source).run_window(
        start_at=source.result.requested_start,
        end_at=source.result.requested_end,
    )
    client = FakeClient.latest
    summarizer = FakeSummarizer.latest
    assert client is not None
    assert summarizer is not None

    assert result.processing_ok is True
    assert result.status == "PARTIAL"
    assert result.source_complete is False
    assert result.coverage_committed is False
    assert result.publish.disclosures_created == 1
    assert result.publish.attachments_staged == 1
    assert result.publish.files_extracted == 1
    assert result.publish.documents_analyzed == 1
    assert result.publish.files_published == 0
    assert result.publish.analyses_completed == 1
    assert result.budget["source_requests"] == 0
    assert result.budget["attachments"] == 1
    assert result.budget["ai_documents"] == 1
    assert client.final_run_status == "PARTIAL"
    assert client.final_error_code == "SOURCE_COVERAGE_UNPROVEN"
    assert client.coverage_committed is False
    assert not any(name == "upsert_files" for name, _payload in client.calls)
    assert client.analysis.impact == "UNCLEAR"
    assert summarizer.documents[0]["source_url"] is None
    assert summarizer.closed is True

    disclosure_call = next(payload for name, payload in client.calls if name == "upsert_disclosures")
    raw_metadata = disclosure_call.items[0].raw_metadata
    assert raw_metadata["sourceId"] == "manual-manifest"
    assert "local_path" not in str(raw_metadata).lower()
    assert str(tmp_path) not in str(raw_metadata)


def test_source_url_publishes_verified_file_metadata(tmp_path) -> None:
    settings = _settings(tmp_path)
    source = FakeSource(_window(tmp_path, source_url="https://example.com/files/disclosure.txt"))
    result = _runner(settings, source).run_window(
        start_at=source.result.requested_start,
        end_at=source.result.requested_end,
    )
    client = FakeClient.latest
    assert client is not None
    assert result.processing_ok is True
    assert result.publish.files_published == 1

    file_call = next(payload for name, payload in client.calls if name == "upsert_files")
    file_item = file_call.files[0]
    assert file_item.source_url == "https://example.com/files/disclosure.txt"
    assert file_item.download_status == "DOWNLOADED"
    assert file_item.extraction_status == "EXTRACTED"
    assert file_item.sha256 is not None and len(file_item.sha256) == 64
    assert file_item.extracted_text_hash is not None and len(file_item.extracted_text_hash) == 64
    assert file_item.extracted_text_ref is None


def test_authoritative_source_can_complete_and_commit_coverage(tmp_path) -> None:
    settings = _settings(tmp_path)
    source = FakeSource(_window(tmp_path, complete=True))
    result = _runner(settings, source, allow_coverage_commit=True).run_window(
        start_at=source.result.requested_start,
        end_at=source.result.requested_end,
    )
    client = FakeClient.latest
    assert client is not None
    assert result.processing_ok is True
    assert result.source_complete is True
    assert result.status == "COMPLETE"
    assert result.coverage_committed is True
    assert client.final_run_status == "COMPLETE"
    assert client.coverage_committed is True


def test_complete_attestation_stays_partial_without_coverage_authorization(tmp_path) -> None:
    settings = _settings(tmp_path)
    source = FakeSource(_window(tmp_path, complete=True))
    result = _runner(settings, source, allow_coverage_commit=False).run_window(
        start_at=source.result.requested_start,
        end_at=source.result.requested_end,
    )
    client = FakeClient.latest
    assert client is not None
    assert result.processing_ok is True
    assert result.status == "PARTIAL"
    assert result.coverage_committed is False
    assert client.final_error_code == "SOURCE_COVERAGE_NOT_AUTHORIZED"
    assert client.coverage_committed is False


def test_manual_external_id_namespace_is_enforced(tmp_path) -> None:
    settings = _settings(tmp_path)
    result = _window(tmp_path)
    disclosure = SourceDisclosure(
        external_id="IDX-REAL-LIKE-ID",
        ticker=result.disclosures[0].ticker,
        announced_at=result.disclosures[0].announced_at,
        title=result.disclosures[0].title,
        attachments=result.disclosures[0].attachments,
    )
    bad_result = SourceWindowResult(
        source_id=result.source_id,
        requested_start=result.requested_start,
        requested_end=result.requested_end,
        disclosures=(disclosure,),
        diagnostics=result.diagnostics,
    )
    source = FakeSource(bad_result)

    with pytest.raises(SourceContractError, match="must start with 'manual-'"):
        _runner(settings, source).run_window(
            start_at=source.result.requested_start,
            end_at=source.result.requested_end,
        )
    client = FakeClient.latest
    assert client is not None
    assert client.final_run_status == "FAILED"
    assert client.final_error_code == "SOURCE_RUN_FAILED"


class FakeRateLimitError(ValueError):
    status_code = 429


class FakeRateLimitSummarizer(FakeSummarizer):
    announcement_calls = 0

    def summarize_announcement(self, *, announcement, documents, stream=False):
        type(self).announcement_calls += 1
        raise FakeRateLimitError("Gemini rate limited after provider cooldown")


def _two_disclosure_window(tmp_path) -> SourceWindowResult:
    base = _window(tmp_path)
    second_path = tmp_path / "disclosure-2.txt"
    second_path.write_text("Second synthetic disclosure body", encoding="utf-8")
    second = SourceDisclosure(
        external_id="manual-example-2",
        ticker="BMRI",
        announced_at=datetime.fromisoformat("2026-08-21T10:30:00+07:00"),
        title="Second disclosure",
        source_url="https://example.com/disclosures/manual-example-2",
        attachments=(
            SourceAttachment(
                filename="disclosure-2.txt",
                local_path=second_path,
                content_type="text/plain",
            ),
        ),
    )
    return SourceWindowResult(
        source_id=base.source_id,
        requested_start=base.requested_start,
        requested_end=base.requested_end,
        disclosures=(base.disclosures[0], second),
        diagnostics=base.diagnostics,
    )


def test_ai_rate_limit_trips_run_circuit_breaker(tmp_path) -> None:
    settings = _settings(tmp_path)
    source = FakeSource(_two_disclosure_window(tmp_path))
    FakeRateLimitSummarizer.announcement_calls = 0

    result = _runner(
        settings,
        source,
        summarizer_factory=FakeRateLimitSummarizer,
    ).run_window(
        start_at=source.result.requested_start,
        end_at=source.result.requested_end,
    )
    client = FakeClient.latest
    assert client is not None

    assert result.processing_ok is False
    assert result.publish.partial_disclosures == 2
    assert result.publish.ai_rate_limit_deferred == 1
    assert result.publish.files_extracted == 1
    assert FakeRateLimitSummarizer.announcement_calls == 1
    assert client.final_run_status == "PARTIAL"
    assert client.final_error_code == "AI_RATE_LIMITED"


class FakeRunDeadlineError(ValueError):
    run_deadline_exceeded = True


class FakeRunDeadlineSummarizer(FakeSummarizer):
    announcement_calls = 0

    def summarize_announcement(self, *, announcement, documents, stream=False):
        type(self).announcement_calls += 1
        raise FakeRunDeadlineError("daily AI analysis deadline reached")


def test_ai_run_deadline_defers_remaining_disclosures(tmp_path) -> None:
    settings = _settings(tmp_path)
    source = FakeSource(_two_disclosure_window(tmp_path))
    FakeRunDeadlineSummarizer.announcement_calls = 0

    result = _runner(
        settings,
        source,
        summarizer_factory=FakeRunDeadlineSummarizer,
    ).run_window(
        start_at=source.result.requested_start,
        end_at=source.result.requested_end,
    )
    client = FakeClient.latest
    assert client is not None

    assert result.processing_ok is False
    assert result.publish.partial_disclosures == 2
    assert result.publish.ai_run_deadline_deferred == 1
    assert result.publish.files_extracted == 1
    assert FakeRunDeadlineSummarizer.announcement_calls == 1
    assert client.final_run_status == "PARTIAL"
    assert client.final_error_code == "AI_RUN_DEADLINE"
